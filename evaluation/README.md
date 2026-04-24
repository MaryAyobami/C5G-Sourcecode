# Evaluation Framework

Measures Open5GS control-plane and user-plane performance across three
deployment models:

- `baseline`: all NFs run natively on a single host.
- `sgx`: all NFs run in Gramine-SGX enclaves; AMF runs in a TDX VM.
- `hybrid`: AMF and UPF each run in their own TDX VM; remaining NFs run in
  SGX on the SGX host.

The framework also captures per-NF processing times, interface-level
latencies (N2, N3, N4, SBI), and per-NF resource consumption with optional
`perf stat` and bpftrace-based TEE overhead counters.

## Layout

```
orchestrator.py                   CLI entry point
Makefile                          convenience targets
config/
  deployments.ini                 NF IPs per deployment + UERANSIM settings
  servers.ini.example             SSH connection template; copy to servers.ini
networkperformance/
  base.py                         SSH helper
  cp_registration.py              single-UE registration latency
  cp_scalability.py               concurrent UE scalability
  cp_service_request.py           NAS Service Request latency
  up_performance.py               TCP/UDP/HTTP user plane
  up_scalability.py               user plane under N concurrent UEs
  overhead_monitor.py             resource + TEE overhead
  nf_processing.py                NF_TIMING log collection
  interface_latency.py            N2/N3/N4/SBI interface latencies
analysis/notebooks/               Jupyter notebooks for plotting
results/                          output CSVs
```

## Configuration

1. Copy `config/servers.ini.example` to `config/servers.ini` and fill in SSH
   user, host, and key path for each machine in your testbed.
2. Edit `config/deployments.ini` to replace the placeholder IPs with the
   addresses of your SGX host, TDX VMs, and UERANSIM host.

The example files use RFC 5737 documentation IPs. They are not routable.

## Running experiments

Entry point: `python3 orchestrator.py <metric> --deployment <baseline|sgx|hybrid> [options]`.

Common Make targets:

```bash
make cp-registration DEPLOYMENT=baseline ITERATIONS=30
make cp-registration DEPLOYMENT=sgx      ITERATIONS=30 WARMSTART=1

make cp-scalability DEPLOYMENT=baseline CONCURRENCY=100 ROUNDS=5
make breaking-point DEPLOYMENT=sgx

make cp-service-request DEPLOYMENT=baseline ITERATIONS=30

make up-performance         DEPLOYMENT=baseline ITERATIONS=30 IPERF3_DUR=30 UDP_BW=100M
make up-scalability-ping    DEPLOYMENT=baseline CONCURRENCY=50 ROUNDS=5
make up-scalability-probe   DEPLOYMENT=baseline CONCURRENCY=50 ROUNDS=5

make resource-monitor DEPLOYMENT=baseline DURATION=120 INTERVAL=1
make resource-monitor DEPLOYMENT=sgx      DURATION=120 PERF=1 BPF=1

make cp-registration-overhead DEPLOYMENT=baseline ITERATIONS=30
make cp-registration-overhead DEPLOYMENT=sgx      PERF=1 BPF=1

make cp-registration-nf  DEPLOYMENT=baseline ITERATIONS=30
make up-performance-nf   DEPLOYMENT=baseline ITERATIONS=30

make cp-registration-iface DEPLOYMENT=baseline ITERATIONS=30
make up-performance-iface  DEPLOYMENT=sgx      ITERATIONS=30

make clean
```

## Output CSVs

Files are written as `results/<experiment>_<deployment>[_<mode>].csv`. If a
file already exists, the orchestrator appends `_2`, `_3`, and so on.

Key columns per experiment:

- `cp-registration`: iteration, success, reg_time_ms, pdu_time_ms, total_attach_ms
- `cp-service-request`: iteration, success, service_request_ms, cm_idle_to_connected_ms
- `cp-scalability`: round, concurrency, success_count, fail_count, success_rate, avg_reg_ms, p95_reg_ms, reg_per_s
- `up-performance`: TCP/UDP/HTTP throughput, jitter, loss, retransmits per direction
- `up-scalability-ping`: concurrency, round, size_bytes, mean_rtt_ms, p95_ue_rtt_ms, mean_loss_pct
- `resource-monitor`: per-NF CPU, RSS, ctx_sw, disk, network, IPC, cache-miss, OCALL rate; host-level steal, mem, pagefault

Companion CSVs:

- `*_nf.csv`: per-NF processing time from NF_TIMING logs
- `*_iface.csv`: per-interface latencies (N2, N3, N4, SBI)

## UERANSIM

- gNB binary: `/opt/UERANSIM/build/nr-gnb`
- UE binary: `/opt/UERANSIM/build/nr-ue`
- 1000 subscribers provisioned in MongoDB: `imsi-999700000000001` through `imsi-999700000001000`

Subscriber security parameters (default Open5GS test values): k, opc, amf
values are the published defaults; replace them in MongoDB before running on
any non-test network.

## NF timing instrumentation

The Open5GS fork referenced from `scripts/install_open5gs.sh` is instrumented
with `[NF_TIMING]` log lines. When running on TDX VMs, the same instrumented
binaries must be present on each VM at `/opt/open5gs-instrumented/`.

Log format: `MM/DD HH:MM:SS.mmm: [nf] INFO: [NF_TIMING] EVENT imsi=... duration_ms=X.XXX`
