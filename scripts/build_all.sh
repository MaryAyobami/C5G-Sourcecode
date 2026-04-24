#!/bin/bash
# Build and sign every Open5GS Gramine manifest.
set -e

ARTIFACT_ROOT="${ARTIFACT_ROOT:-/opt/open5gs-gramine}"
MANIFESTS_DIR="$ARTIFACT_ROOT/manifests"

cd "$MANIFESTS_DIR" || { echo "manifests dir not found: $MANIFESTS_DIR" >&2; exit 1; }

COMPONENTS="nrf scp sepp udr udm ausf pcf smf nssf bsf upf-nonblocking"

for comp in $COMPONENTS; do
    echo "[$comp] manifest"
    gramine-manifest "open5gs-${comp}.manifest.toml" "open5gs-${comp}.manifest"
    echo "[$comp] sgx-sign"
    gramine-sgx-sign --manifest "open5gs-${comp}.manifest" --output "open5gs-${comp}.manifest.sgx"
done

echo "manifests built and signed"
ls -lh *.manifest *.manifest.sgx *.sig 2>/dev/null | grep -v ".toml" || true

# Regenerate the signed measurement registry so the broker validates the
# freshly built enclaves.
BUILD_MEASUREMENTS="$ARTIFACT_ROOT/attestation/scripts/build_measurements.sh"
if [ -x "$BUILD_MEASUREMENTS" ]; then
    bash "$BUILD_MEASUREMENTS" \
        && echo "measurements.yaml updated" \
        || echo "warning: measurements.yaml update failed"
fi
