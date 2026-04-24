#!/usr/bin/env python3
"""NF Wrapper - obtains a measurement-bound TLS identity for each NF.

Generates a fresh keypair, requests an attestation report from the TEE (or
synthesises one from the signed manifest in bootstrap mode), fetches an
X.509 certificate from the broker, installs it, then execs the NF binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WRAPPER:%(nf)s] %(levelname)s: %(message)s",
)


class NFWrapper:
    def __init__(self, nf: str, tee_type: str, issuer_url: str,
                 ca_bundle: Path, tls_out_dir: Path, exec_cmd: list[str],
                 rotate_interval: int, sig_path: Path | None = None):
        self.nf = nf
        self.tee = tee_type.upper()
        self.issuer_url = issuer_url.rstrip("/")
        self.ca_bundle = ca_bundle
        self.tls_dir = tls_out_dir
        self.exec_cmd = exec_cmd
        self.rotate_interval = rotate_interval
        self.sig_path = sig_path
        self.log = logging.LoggerAdapter(
            logging.getLogger("nf_wrapper"), {"nf": nf}
        )
        self._stop = threading.Event()
        self._child: subprocess.Popen | None = None

    def _read_sgx_report(self, report_data: bytes) -> bytes:
        urd = Path("/dev/attestation/user_report_data")
        rpt = Path("/dev/attestation/report")
        if urd.exists() and rpt.exists():
            urd.write_bytes(report_data.ljust(64, b"\x00")[:64])
            return rpt.read_bytes()
        if self.sig_path is None:
            raise RuntimeError("no /dev/attestation and no --sig path provided")
        return self._synth_report_from_sig(report_data)

    def _synth_report_from_sig(self, report_data: bytes) -> bytes:
        # Bootstrap: host-side wrapper synthesises a report from the signed
        # manifest. MRENCLAVE comes from the .sig file (the operator's
        # expected measurement), not from a live EREPORT.
        out = subprocess.check_output(
            ["gramine-sgx-sigstruct-view", str(self.sig_path)], text=True
        )
        mr_enclave = re.search(r"mr_enclave:\s*([0-9a-f]+)", out).group(1)
        mr_signer = re.search(r"mr_signer:\s*([0-9a-f]+)", out).group(1)
        rpt = bytearray(432)
        rpt[64:96]   = bytes.fromhex(mr_enclave)
        rpt[128:160] = bytes.fromhex(mr_signer)
        rpt[320:384] = report_data.ljust(64, b"\x00")[:64]
        self.log.info("bootstrap mode: report synthesized from sig file")
        return bytes(rpt)

    def _gen_keypair(self) -> tuple[bytes, str]:
        priv = ec.generate_private_key(ec.SECP384R1())
        pub_pem = priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        priv_pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return priv_pem, pub_pem

    def _request_cert(self) -> tuple[bytes, str, str]:
        priv_pem, pub_pem = self._gen_keypair()
        nonce = os.urandom(32)
        binding = hashlib.sha256(pub_pem.encode() + nonce).digest()
        report = self._read_sgx_report(binding)

        payload = json.dumps({
            "name": self.nf,
            "tee_type": self.tee,
            "pubkey_pem": pub_pem,
            "hw_report_hex": report.hex(),
            "nonce": nonce.hex(),
        }).encode()

        ctx = ssl.create_default_context()
        if self.issuer_url.startswith("https://"):
            ctx.load_verify_locations(str(self.ca_bundle))
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
        req = urllib.request.Request(
            f"{self.issuer_url}/issue-cert",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = json.loads(resp.read())
        return priv_pem, body["cert_pem"], body["ca_pem"]

    def _install(self, priv_pem: bytes, cert_pem: str, ca_pem: str):
        self.tls_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.tls_dir / f"{self.nf}.key"
        crt_path = self.tls_dir / f"{self.nf}.crt"
        ca_path = self.tls_dir / "operator_ca.crt"

        tmp_key = key_path.with_suffix(".key.tmp")
        tmp_crt = crt_path.with_suffix(".crt.tmp")
        tmp_key.write_bytes(priv_pem)
        os.chmod(tmp_key, 0o600)
        tmp_crt.write_text(cert_pem)
        os.replace(tmp_key, key_path)
        os.replace(tmp_crt, crt_path)
        ca_path.write_text(ca_pem)
        self.log.info(f"installed cert: {crt_path}")

    def _rotation_loop(self):
        # Open5GS NFs terminate on SIGHUP, so rotation only writes new
        # cert/key to disk; the running NF keeps its in-memory cert and
        # picks up the new one at its next planned restart.
        while not self._stop.wait(self.rotate_interval):
            try:
                t0 = time.monotonic_ns()
                self.log.info("rotating cert")
                priv_pem, cert_pem, ca_pem = self._request_cert()
                self._install(priv_pem, cert_pem, ca_pem)
                dt_ms = (time.monotonic_ns() - t0) / 1e6
                print(f"[TIMING] nf={self.nf} step=rotation_ms ms={dt_ms:.2f}", flush=True)
            except Exception as e:
                self.log.error(f"rotation failed: {e}")

    def run(self):
        self.log.info(f"starting wrapper tee={self.tee} issuer={self.issuer_url}")
        t_start = time.monotonic_ns()
        priv_pem, cert_pem, ca_pem = self._request_cert()
        t_cert = time.monotonic_ns()
        self._install(priv_pem, cert_pem, ca_pem)
        t_install = time.monotonic_ns()
        print(f"[TIMING] nf={self.nf} step=cert_fetch ms={(t_cert - t_start)/1e6:.2f}", flush=True)
        print(f"[TIMING] nf={self.nf} step=cert_install ms={(t_install - t_cert)/1e6:.2f}", flush=True)
        print(f"[TIMING] nf={self.nf} step=attestation_total_ms ms={(t_install - t_start)/1e6:.2f}", flush=True)

        threading.Thread(target=self._rotation_loop, daemon=True).start()

        self.log.info(f"exec -> {' '.join(self.exec_cmd)}")
        self._child = subprocess.Popen(self.exec_cmd)

        def _forward(signum, _frame):
            if self._child:
                self._child.send_signal(signum)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _forward)

        rc = self._child.wait()
        self._stop.set()
        sys.exit(rc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nf", required=True)
    ap.add_argument("--tee", default="SGX", choices=["SGX", "TDX"])
    ap.add_argument("--issuer-url", default=os.getenv("ISSUER_URL", "http://127.0.0.1:8443"))
    ap.add_argument("--ca-bundle", default=os.getenv(
        "OPERATOR_CA",
        "/opt/open5gs-gramine/attestation/ca/operator_ca.crt",
    ))
    ap.add_argument("--tls-dir", default="/usr/local/etc/open5gs/tls")
    ap.add_argument("--rotate-seconds", type=int, default=12 * 60 * 60)
    ap.add_argument("--sig", default=None,
                    help="path to .sig file for bootstrap when /dev/attestation absent")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="command to exec after cert install")
    args = ap.parse_args()

    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if not args.cmd:
        print("error: no exec command supplied", file=sys.stderr)
        sys.exit(2)

    NFWrapper(
        nf=args.nf,
        tee_type=args.tee,
        issuer_url=args.issuer_url,
        ca_bundle=Path(args.ca_bundle),
        tls_out_dir=Path(args.tls_dir),
        exec_cmd=args.cmd,
        rotate_interval=args.rotate_seconds,
        sig_path=Path(args.sig) if args.sig else None,
    ).run()


if __name__ == "__main__":
    main()
