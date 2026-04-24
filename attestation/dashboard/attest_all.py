#!/usr/bin/env python3
"""Operator-scoped remote attestation dashboard.

Opens a TLS connection to each NF, reads its certificate, and verifies the
measurement extension against the signed registry. Exits non-zero if any NF
fails.
"""
from __future__ import annotations

import argparse
import socket
import ssl
import sys
from pathlib import Path

import yaml
from cryptography import x509

OID_MRENCLAVE = "1.3.6.1.4.1.99999.1.1"

OK = "\033[32mOK\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


def fetch_cert(host: str, port: int, ca_bundle: Path, timeout: float = 5.0) -> x509.Certificate:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(str(ca_bundle))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    with socket.create_connection((host, port), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            der = ss.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der)


def extract_ext(cert: x509.Certificate, oid: str) -> bytes | None:
    for ext in cert.extensions:
        if ext.oid.dotted_string == oid:
            return ext.value.value
    return None


def attest_one(nf: str, host: str, port: int, expected: str, ca_bundle: Path) -> bool:
    try:
        cert = fetch_cert(host, port, ca_bundle)
    except Exception as e:
        print(f"{FAIL} {nf:8s} cannot connect: {e}")
        return False

    ext = extract_ext(cert, OID_MRENCLAVE)
    if ext is None:
        print(f"{FAIL} {nf:8s} cert has no MRENCLAVE extension")
        return False

    actual = ext.hex()
    match = actual == expected
    verdict = OK if match else FAIL
    expires = cert.not_valid_after.isoformat()
    print(f"{verdict} {nf:8s} measurement={'OK' if match else 'MISMATCH'} expires={expires}")
    if not match:
        print(f"         got:      {actual}")
        print(f"         expected: {expected}")
    return match


def main():
    base = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser()
    ap.add_argument("--measurements", default=str(base / "measurements/measurements.yaml"))
    ap.add_argument("--ca-bundle", default=str(base / "ca/operator_ca.crt"))
    ap.add_argument("--inventory", default=str(base / "dashboard/inventory.yaml"))
    args = ap.parse_args()

    measurements = yaml.safe_load(Path(args.measurements).read_text())
    inv = yaml.safe_load(Path(args.inventory).read_text())

    all_ok = True
    for nf, target in inv.get("nfs", {}).items():
        host = target["host"]
        port = int(target["port"])
        tee = target.get("tee", "sgx").lower()
        expected = (measurements.get(tee, {}).get(nf) or {}).get(
            "mrenclave" if tee == "sgx" else "mrtd"
        )
        if not expected:
            print(f"{WARN} {nf:8s} no expected measurement in manifest")
            continue
        ok = attest_one(nf, host, port, expected, Path(args.ca_bundle))
        all_ok &= ok

    print()
    print("verdict:", OK if all_ok else FAIL)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
