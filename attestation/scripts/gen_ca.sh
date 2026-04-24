#!/usr/bin/env bash
set -euo pipefail

CA_DIR="$(cd "$(dirname "$0")/.." && pwd)/ca"
mkdir -p "$CA_DIR"
cd "$CA_DIR"

if [[ -f operator_ca.key ]]; then
  echo "CA already exists at $CA_DIR. Refusing to overwrite." >&2
  exit 1
fi

openssl ecparam -name secp384r1 -genkey -noout -out operator_ca.key
chmod 600 operator_ca.key

openssl req -new -x509 -days 3650 -key operator_ca.key -out operator_ca.crt \
  -subj "/CN=Open5GS Operator CA/O=MNO/OU=Confidential 5G Core" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:1" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

echo "CA written:"
echo "  key: $CA_DIR/operator_ca.key  (KEEP OFFLINE)"
echo "  crt: $CA_DIR/operator_ca.crt  (distribute to all NFs)"
