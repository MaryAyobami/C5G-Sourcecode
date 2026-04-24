#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST_DIR="$REPO_DIR/manifests"
ATTEST_DIR="$REPO_DIR/attestation"

[[ -f "$ATTEST_DIR/ca/operator_ca.key" ]] || {
  echo "missing operator CA; run gen_ca.sh first" >&2; exit 1; }
[[ -f "$ATTEST_DIR/measurements/measurements.yaml" ]] || {
  echo "missing measurements.yaml; run build_measurements.sh first" >&2; exit 1; }

cd "$MANIFEST_DIR"
gramine-manifest broker.manifest.toml broker.manifest
gramine-sgx-sign --manifest broker.manifest --output broker.manifest.sgx

echo "Broker MRENCLAVE:"
gramine-sgx-sigstruct-view broker.sig | grep mr_enclave
echo
echo "Run with: sudo gramine-sgx broker"
