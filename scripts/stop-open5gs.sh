#!/bin/bash
set -euo pipefail

# Stop both native Open5GS daemons and Gramine SGX wrappers/loaders.
# Use [o]pen5gs in regexes to avoid matching this pkill command itself.
sudo pkill -9 -f '[g]ramine-sgx [o]pen5gs-|/gramine/sgx/loader .*[o]pen5gs-' || true
sudo pkill -9 -f '/usr/local/bin/[o]pen5gs-[a-z0-9-]*d' || true

echo "Remaining Open5GS listeners on 7777-7785:"
sudo ss -ltnp | rg ':(7777|7778|7779|7780|7781|7782|7783|7784|7785)\b' || true
