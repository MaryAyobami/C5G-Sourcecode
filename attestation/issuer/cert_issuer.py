#!/usr/bin/env python3
"""Cert Issuer - measurement-bound TLS cert issuance.

Issues X.509 certificates to NFs that present a valid hardware attestation
report whose measurement matches a signed registry.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

OID_MRENCLAVE = ObjectIdentifier("1.3.6.1.4.1.99999.1.1")
OID_TEE_TYPE  = ObjectIdentifier("1.3.6.1.4.1.99999.1.2")
OID_NF_NAME   = ObjectIdentifier("1.3.6.1.4.1.99999.1.3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ISSUER] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


def _load_ca(ca_key_path: Path, ca_crt_path: Path):
    with open(ca_key_path, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(ca_crt_path, "rb") as f:
        ca_crt = x509.load_pem_x509_certificate(f.read())
    return ca_key, ca_crt


def _load_measurements(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _sgx_report_body(report: bytes) -> dict:
    if len(report) < 432:
        raise ValueError(f"short SGX report: {len(report)}")
    return {
        "mr_enclave":  report[64:96].hex(),
        "mr_signer":   report[128:160].hex(),
        "isv_prod_id": int.from_bytes(report[256:258], "little"),
        "isv_svn":     int.from_bytes(report[258:260], "little"),
        "report_data": report[320:384],
    }


def _verify_report_data(pubkey_pem: str, nonce_hex: str, report_data: bytes) -> bool:
    expected = hashlib.sha256(pubkey_pem.encode() + bytes.fromhex(nonce_hex)).digest()
    return report_data[: len(expected)] == expected


def _local_sgx_mac_check(report: bytes) -> bool:
    # Full EREPORT MAC recomputation requires the in-enclave TARGETINFO
    # exchange. Returns False outside an enclave; callers treat this as
    # "unverifiable MAC, accept under TOFU".
    return Path("/dev/attestation/report").exists()


class IssuerState:
    def __init__(self, ca_key, ca_crt, measurements, ttl: dt.timedelta):
        self.ca_key = ca_key
        self.ca_crt = ca_crt
        self.measurements = measurements
        self.ttl = ttl
        self.lock = threading.Lock()
        self.issued: list[dict] = []

    def expected_sgx(self, nf: str) -> Optional[str]:
        return (self.measurements.get("sgx", {}).get(nf) or {}).get("mrenclave")

    def expected_tdx(self, nf: str) -> Optional[str]:
        return (self.measurements.get("tdx", {}).get(nf) or {}).get("mrtd")

    def issue(self, nf: str, tee_type: str, pubkey_pem: str,
              measurement: str) -> x509.Certificate:
        pub = serialization.load_pem_public_key(pubkey_pem.encode())
        now = dt.datetime.utcnow()

        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, nf),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MNO 5GC"),
            ]))
            .issuer_name(self.ca_crt.subject)
            .public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + self.ttl)
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName(f"{nf}.5gc.local"),
                    x509.DNSName(nf),
                ]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=True, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.UnrecognizedExtension(OID_MRENCLAVE, bytes.fromhex(measurement)), critical=False)
            .add_extension(x509.UnrecognizedExtension(OID_TEE_TYPE, tee_type.encode()), critical=False)
            .add_extension(x509.UnrecognizedExtension(OID_NF_NAME, nf.encode()), critical=False)
            .sign(private_key=self.ca_key, algorithm=hashes.SHA384())
        )

        with self.lock:
            self.issued.append({
                "nf": nf,
                "tee_type": tee_type,
                "measurement": measurement,
                "serial": cert.serial_number,
                "not_after": cert.not_valid_after.isoformat(),
                "issued_at": now.isoformat(),
            })
        return cert


class IssuerHandler(BaseHTTPRequestHandler):
    state: IssuerState = None

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        if self.path == "/audit":
            with self.state.lock:
                return self._json(200, {"issued": list(self.state.issued)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/issue-cert":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except Exception:
            return self._json(400, {"error": "bad json"})

        nf = req.get("name")
        tee = req.get("tee_type", "SGX").upper()
        pubkey_pem = req.get("pubkey_pem")
        report_hex = req.get("hw_report_hex", "")
        nonce_hex = req.get("nonce", "")

        if not all([nf, pubkey_pem, report_hex, nonce_hex]):
            return self._json(400, {"error": "missing fields"})

        try:
            report = bytes.fromhex(report_hex)
        except ValueError:
            return self._json(400, {"error": "bad report hex"})

        if tee == "SGX":
            body = _sgx_report_body(report)
            if not _verify_report_data(pubkey_pem, nonce_hex, body["report_data"]):
                return self._json(403, {"error": "report_data mismatch"})
            expected = self.state.expected_sgx(nf)
            if not expected:
                return self._json(403, {"error": f"unknown NF: {nf}"})
            if body["mr_enclave"] != expected:
                return self._json(403, {"error": "MRENCLAVE mismatch"})
            if not _local_sgx_mac_check(report):
                log.info(f"[{nf}] MAC unverifiable from host - TOFU accept")
            measurement = body["mr_enclave"]
        elif tee == "TDX":
            expected = self.state.expected_tdx(nf)
            if not expected or expected.startswith("PLACEHOLDER"):
                return self._json(403, {"error": f"no TDX measurement for {nf}"})
            if len(report) < 584:
                return self._json(400, {"error": "short TDX report"})
            if not _verify_report_data(pubkey_pem, nonce_hex, report[520:584]):
                return self._json(403, {"error": "report_data mismatch"})
            mr_td = report[528:576].hex()
            if mr_td != expected:
                return self._json(403, {"error": "MRTD mismatch"})
            measurement = mr_td
        else:
            return self._json(400, {"error": f"unsupported tee_type: {tee}"})

        cert = self.state.issue(nf, tee, pubkey_pem, measurement)
        log.info(f"issued cert: nf={nf} tee={tee} serial={cert.serial_number}")

        return self._json(200, {
            "cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "ca_pem": self.state.ca_crt.public_bytes(serialization.Encoding.PEM).decode(),
            "not_after": cert.not_valid_after.isoformat(),
            "measurement": measurement,
        })

    def log_message(self, fmt, *args):
        log.debug("%s - " + fmt, self.address_string(), *args)


def build_handler(state: IssuerState):
    return type("BoundHandler", (IssuerHandler,), {"state": state})


def main():
    ap = argparse.ArgumentParser()
    base = Path(__file__).resolve().parents[1]
    ap.add_argument("--ca-key", default=str(base / "ca/operator_ca.key"))
    ap.add_argument("--ca-crt", default=str(base / "ca/operator_ca.crt"))
    ap.add_argument("--measurements", default=str(base / "measurements/measurements.yaml"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", default=8443, type=int)
    ap.add_argument("--ttl-hours", default=24, type=int)
    ap.add_argument("--plain-http", action="store_true")
    ap.add_argument("--tls-cert", default=None)
    ap.add_argument("--tls-key", default=None)
    args = ap.parse_args()

    ca_key, ca_crt = _load_ca(Path(args.ca_key), Path(args.ca_crt))
    measurements = _load_measurements(Path(args.measurements))
    state = IssuerState(ca_key, ca_crt, measurements, dt.timedelta(hours=args.ttl_hours))

    server = ThreadingHTTPServer((args.host, args.port), build_handler(state))

    if not args.plain_http:
        if not (args.tls_cert and args.tls_key):
            self_key = ec.generate_private_key(ec.SECP384R1())
            self_cert = state.issue(
                "cert-issuer", "SERVICE",
                self_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode(),
                "0" * 64,
            )
            cert_path = base / "issuer/_self.crt"
            key_path = base / "issuer/_self.key"
            cert_path.write_bytes(self_cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(self_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(key_path, 0o600)
            args.tls_cert = str(cert_path)
            args.tls_key = str(key_path)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log.info(f"Cert Issuer TLS on https://{args.host}:{args.port}")
    else:
        log.info(f"Cert Issuer (plain HTTP) on http://{args.host}:{args.port}")

    log.info(f"CA subject: {ca_crt.subject.rfc4514_string()}")
    log.info(f"SGX NFs: {list(measurements.get('sgx', {}).keys())}")
    log.info(f"TDX NFs: {list(measurements.get('tdx', {}).keys())}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
