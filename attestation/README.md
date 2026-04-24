# Attestation for Open5GS on Gramine

Measurement-bound TLS identity for SGX and TDX NFs. The broker is designed
to run inside a Gramine-SGX enclave; each NF obtains a short-lived X.509
certificate whose MRENCLAVE/MRTD is verified before it can join the core
network. No Open5GS source change.

## Layout

```
attestation/
├── ca/                            operator root CA (offline)
├── measurements/                  signed registry of expected measurements
├── issuer/cert_issuer.py          broker: verifies reports, issues certs
├── wrapper/nf_wrapper.py          per-NF startup shim
├── dashboard/attest_all.py        operator-scoped remote attestation
├── dashboard/inventory.yaml       NF host/port table
└── scripts/
    ├── gen_ca.sh                  generate operator CA (once)
    ├── build_measurements.sh      build + sign measurements.yaml from .sig
    ├── install_operator_ca.sh     install operator CA into Open5GS tls dir
    └── build_broker.sh            build + sign broker Gramine-SGX enclave
```

## One-time setup

```bash
bash scripts/gen_ca.sh                 # generate operator CA
bash scripts/build_measurements.sh     # extract MRENCLAVEs into signed registry
sudo bash scripts/install_operator_ca.sh
bash scripts/build_broker.sh           # produce broker.manifest.sgx + broker.sig
```

## Runtime

```bash
# Broker in Gramine-SGX enclave
(cd ../manifests && sudo gramine-sgx broker) &

# NF under the wrapper (example: NRF)
ISSUER_URL=http://127.0.0.1:8443 \
  python3 wrapper/nf_wrapper.py --nf nrf --tee SGX -- \
  gramine-sgx open5gs-nrf

# Operator-scoped remote attestation
python3 dashboard/attest_all.py
```

## Trust model

- Operator root CA is the single trust anchor; its private key stays offline.
- Broker runs inside a Gramine-SGX enclave; its signing key and the signed
  measurement registry are pinned as `sgx.trusted_files` and bound into the
  broker's MRENCLAVE.
- Broker public key is included as a trusted file in each SGX NF's manifest,
  binding it into the NF's MRENCLAVE.
- Certificates are short-lived (24 h default); the wrapper writes new
  certs to disk on rotation. Open5GS NFs do not support runtime TLS reload,
  so the running NF retains its in-memory cert until its next planned
  restart.
- Third-party cryptographic attestation is out of scope; it requires Intel
  DCAP, which this testbed does not have.
