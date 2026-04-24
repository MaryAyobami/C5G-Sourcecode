#!/usr/bin/env bash
# Fetch real MRTD from each TDX VM and update measurements.yaml.
#
# Expects passwordless SSH to the TDX VM users. The TDX probe script is
# copied to /tmp on the guest and executed under sudo (configfs outblob
# requires root).

set -euo pipefail

ATTEST_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBE="$ATTEST_DIR/scripts/tdx_probe.py"
MEAS="$ATTEST_DIR/measurements/measurements.yaml"
CA_KEY="$ATTEST_DIR/ca/operator_ca.key"

# nf_name  ssh_target
TARGETS=(
  "amf  tdx@198.51.100.11"
  "upf  tdx@198.51.100.12"
)

[[ -f "$PROBE" ]] || { echo "missing $PROBE" >&2; exit 1; }
[[ -f "$MEAS"  ]] || { echo "missing $MEAS"  >&2; exit 1; }

for entry in "${TARGETS[@]}"; do
  read -r nf target <<<"$entry"
  echo "[$nf] fetching MRTD from $target..."
  scp -o ConnectTimeout=5 -o BatchMode=yes "$PROBE" "$target:/tmp/tdx_probe.py" >/dev/null
  mrtd=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$target" \
         "sudo python3 /tmp/tdx_probe.py" | tr -d '\r\n ')
  if [[ ${#mrtd} -ne 96 ]]; then
    echo "[$nf] unexpected MRTD length ${#mrtd}: $mrtd" >&2
    continue
  fi
  python3 - "$MEAS" "$nf" "$mrtd" <<'PY'
import re, sys
path, nf, mrtd = sys.argv[1:]
s = open(path).read()
pat = re.compile(rf'(  {nf}:\s*\n    mrtd:\s*")[^"]+(")')
if not pat.search(s):
    sys.exit(f'no tdx block for {nf} in measurements.yaml')
open(path, 'w').write(pat.sub(rf'\g<1>{mrtd}\g<2>', s))
PY
  echo "[$nf] MRTD=$mrtd"
done

if [[ -f "$CA_KEY" ]]; then
  openssl dgst -sha256 -sign "$CA_KEY" -out "${MEAS}.sig" "$MEAS"
  echo "re-signed -> ${MEAS}.sig"
fi
