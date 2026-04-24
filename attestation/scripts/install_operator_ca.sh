#!/usr/bin/env bash
# Install operator CA into Open5GS TLS dir as ca.crt.
# Open5GS configs already reference /usr/local/etc/open5gs/tls/ca.crt for both
# server verify_client_cacert and client cacert, so no yaml edit is required.
#
# Idempotent. Keeps a timestamped backup of any previous ca.crt.

set -euo pipefail

OPERATOR_CA="${OPERATOR_CA:-$(cd "$(dirname "$0")/.." && pwd)/ca/operator_ca.crt}"
TLS_DIR="${TLS_DIR:-/usr/local/etc/open5gs/tls}"
DEST="$TLS_DIR/ca.crt"

[[ -f "$OPERATOR_CA" ]] || { echo "missing operator CA: $OPERATOR_CA" >&2; exit 1; }
[[ -d "$TLS_DIR" ]]    || { echo "missing tls dir: $TLS_DIR" >&2; exit 1; }

if [[ -f "$DEST" ]]; then
  if cmp -s "$OPERATOR_CA" "$DEST"; then
    echo "operator CA already installed at $DEST"
    exit 0
  fi
  bak="$DEST.$(date +%s).bak"
  cp "$DEST" "$bak"
  echo "backed up existing ca.crt -> $bak"
fi

cp "$OPERATOR_CA" "$DEST"
chmod 644 "$DEST"
echo "installed operator CA -> $DEST"
echo
echo "Every NF yaml already references this path under:"
echo "  sbi.default.tls.server.verify_client_cacert"
echo "  sbi.default.tls.client.cacert"
echo "No yaml edit required."
