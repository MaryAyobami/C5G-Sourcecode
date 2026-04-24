# COnfidential5G

Reproducibility artifact for running Open5GS 5G core network functions (NFs)
inside Intel SGX enclaves and TDX confidential VMs. The artifact provides
Gramine manifests for each NF, install scripts and Zellij layouts for the
three deployment models, an attestation layer that issues measurement-bound
TLS identities to each NF, and the framework used to measure performance.

Three deployment models are supported:

- `baseline`: all NFs run natively on a single host.
- `sgx`: all NFs run in Gramine-SGX enclaves; the AMF runs in a TDX VM.
- `hybrid`: AMF and UPF each run in their own TDX VM; the remaining NFs run
  in SGX on the SGX host.

## What this artifact includes

| Path | Contents |
| --- | --- |
| `manifests/` | Gramine manifest sources (`*.manifest.toml`) for each NF and the broker |
| `scripts/` | Install, build, and launch scripts; Zellij layouts for each deployment |
| `attestation/` | Cert broker, NF wrapper, signed measurement registry, operator dashboard |
| `evaluation/` | Experiment orchestrator, per-metric modules, analysis notebooks, example result CSVs |

Generated files (`.manifest`, `.manifest.sgx`, `.sig`,) are
not shipped. They are produced by the build scripts below.

## Prerequisites

Use the development environment described in the paper, or an equivalent
setup. Install the Python packages listed in `requirements.txt`.

## One-time setup

```bash
# from the artifact root
sudo bash scripts/install_gramine_sgx.sh
sudo bash scripts/install_open5gs.sh
sudo bash scripts/install_ueransim.sh
bash scripts/build_all.sh             # build each NF manifest.sgx + sig
```

Before running experiments, copy `evaluation/config/servers.ini.example` to
`evaluation/config/servers.ini` and fill in SSH details for your testbed.
Also edit `evaluation/config/deployments.ini` to replace the placeholder IPs
with your own host addresses.

## Running an NF

Start each NF manually in its own shell. The examples below show how to
bring up the NRF; repeat for `amf`, `smf`, `upf`, `ausf`, `udm`, `udr`,
`pcf`, `nrf`, `nssf`, `bsf`, `scp`, `sepp` by swapping the NF name.

Native:

```bash
sudo /usr/local/bin/open5gs-nrfd -c /usr/local/etc/open5gs/nrf.yaml
```
Or 

````bash
sudo systemctl status open5gs-nrfd
````
Gramine-SGX:

```bash
cd manifests
sudo gramine-sgx open5gs-nrf
```

Gramine-SGX with an attestation-bound certificate:

```bash
# broker must be running first
(cd manifests && sudo gramine-sgx broker) &

cd manifests
ISSUER_URL=http://127.0.0.1:8443 \
  python3 ../attestation/wrapper/nf_wrapper.py \
    --nf nrf --tee SGX \
    --sig open5gs-nrf.sig \
    -- gramine-sgx open5gs-nrf
```

TDX VM (for AMF and UPF in the hybrid deployment): launch the binary the same way as the native
case.

For convenience, two Zellij layouts start every NF at once, with the
correct dependency ordering and sleeps:

```bash
zellij --layout scripts/open5gs-native.kdl   # baseline
zellij --layout scripts/open5gs-layout.kdl   # sgx
```

With UERANSIM running against the core, 1000 subscribers are preprovisioned
in MongoDB (`imsi-999700000000001` through `imsi-999700000001000`).

## Attestation layer

The attestation layer issues short-lived X.509 certificates whose
MRENCLAVE or MRTD is bound into two custom extensions. The broker runs
inside a Gramine-SGX enclave and uses a signed measurement registry pinned
as a trusted file. See `attestation/README.md` for the trust model and
operator setup.

## Reproducing the paper figures

The evaluation framework is driven by `evaluation/orchestrator.py` via the
Makefile. Each experiment writes a CSV into `evaluation/results/`. The
notebooks in `evaluation/analysis/notebooks/` read these CSVs and render the
figures.

The paper uses five figures:

| Figure | Notebook | Output file |
| --- | --- | --- |
| cp registration + service request | `cp_performance.ipynb` | `images/cp_reg_and_sr.png` |
| NF processing time | `cp_performance.ipynb` | `images/cp_nf_processing.png` |
| NF startup latency | `cp_performance.ipynb` | `images/cp_startup_latency.png` |
| UP scalability (ping) | `up_performance.ipynb` | `images/up_scalability_ping.png` |
| Resource CPU | `resource.ipynb` | `analysis/images/resource_cpu.png` |

Example CSVs from our runs are included under `evaluation/results/` so the
notebooks render without re-running any experiments. Fresh CSVs from the
Make targets below will be picked up automatically.

To regenerate all experiment CSVs:

```bash
cd evaluation
make cp-registration DEPLOYMENT=baseline ITERATIONS=30
make cp-registration DEPLOYMENT=sgx      ITERATIONS=30
make cp-registration DEPLOYMENT=hybrid   ITERATIONS=30

make cp-service-request DEPLOYMENT=baseline ITERATIONS=30
make cp-service-request DEPLOYMENT=sgx      ITERATIONS=30
make cp-service-request DEPLOYMENT=hybrid   ITERATIONS=30

make cp-registration-nf DEPLOYMENT=baseline ITERATIONS=30
make cp-registration-nf DEPLOYMENT=sgx      ITERATIONS=30
make cp-registration-nf DEPLOYMENT=hybrid   ITERATIONS=30

make up-scalability-ping DEPLOYMENT=baseline CONCURRENCY=50 ROUNDS=5
make up-scalability-ping DEPLOYMENT=sgx      CONCURRENCY=50 ROUNDS=5
make up-scalability-ping DEPLOYMENT=hybrid   CONCURRENCY=50 ROUNDS=5

make resource-monitor DEPLOYMENT=baseline DURATION=120 INTERVAL=1
make resource-monitor DEPLOYMENT=sgx      DURATION=120 PERF=1
make resource-monitor DEPLOYMENT=hybrid   DURATION=120 PERF=1
```

Example CSVs from our run are included under `evaluation/results/` so the
notebooks render without a full re-run. To render the figures:

```bash
jupyter nbconvert --to notebook --execute \
    evaluation/analysis/notebooks/cp_performance.ipynb \
    evaluation/analysis/notebooks/up_performance.ipynb \
    evaluation/analysis/notebooks/resource.ipynb \
    --inplace
```
