PYTHON      = python3
ITERATIONS  = 3
DEPLOYMENT  = baseline
CONCURRENCY = 1000
ROUNDS      = 5
PING_COUNT    = 2000
PING_INTERVAL = 0.01
SCALABILITY_PING_INTERVAL = 0.01
SCALABILITY_PING_SIZES    = 1400
BATCH_SIZE    = 20
IPERF3_DUR    = 60
UDP_BW      = 500M
DURATION    = 1024
INTERVAL    = 1

# set PERF=1 to enable perf stat hardware counters (IPC, cache-miss)
# set BPF=1  to enable bpftrace OCALL counting (SGX/hybrid only)
_PERF_FLAG = $(if $(PERF),--perf,)
_BPF_FLAG  = $(if $(BPF),--bpf,)

.PHONY: run cp-registration cp-registration-nf cp-registration-iface \
        cp-registration-overhead cp-registration-overhead-full \
        cp-scalability breaking-point cp-service-request \
        up-performance up-performance-nf up-performance-iface up-performance-overhead up-ramp \
        up-scalability-ping up-scalability-ping-overhead \
        resource-monitor clean help

run: cp-registration

cp-registration:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,)

cp-registration-nf:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,) \
		--nf-timing

cp-registration-iface:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,) \
		--interface-latency

cp-registration-nf-iface:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,) \
		--nf-timing \
		--interface-latency \
		$(if $(OVERHEAD),--overhead,) \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

cp-registration-overhead:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,) \
		--overhead \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

cp-registration-overhead-full:
	$(PYTHON) orchestrator.py cp-registration \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,) \
		--overhead --perf --bpf


cp-scalability:
	$(PYTHON) orchestrator.py cp-scalability \
		--deployment $(DEPLOYMENT) \
		--concurrency $(CONCURRENCY) \
		--rounds $(ROUNDS) \
		$(if $(OVERHEAD),--overhead,) \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

breaking-point:
	$(PYTHON) orchestrator.py cp-scalability \
		--deployment $(DEPLOYMENT) \
		--breaking-point \
		$(if $(MAX_CONCURRENCY),--max-concurrency $(MAX_CONCURRENCY),)

cp-service-request:
	$(PYTHON) orchestrator.py cp-service-request \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		$(if $(WARMSTART),--warmstart,)

up-performance:
	$(PYTHON) orchestrator.py up-performance \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		--iperf3-duration $(IPERF3_DUR) \
		--udp-bandwidth $(UDP_BW) \
		$(if $(HTTP_ONLY),--http-only,)

up-ramp:
	$(PYTHON) orchestrator.py up-ramp \
		--deployment $(DEPLOYMENT) \
		--iperf3-duration $(IPERF3_DUR) \
		$(if $(RAMP_UL),--ramp-ul $(RAMP_UL),) \
		$(if $(RAMP_DL),--ramp-dl $(RAMP_DL),)

up-performance-nf:
	$(PYTHON) orchestrator.py up-performance \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		--iperf3-duration $(IPERF3_DUR) \
		--udp-bandwidth $(UDP_BW) \
		$(if $(HTTP_ONLY),--http-only,) \
		--nf-timing

up-performance-iface:
	$(PYTHON) orchestrator.py up-performance \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		--iperf3-duration $(IPERF3_DUR) \
		--udp-bandwidth $(UDP_BW) \
		$(if $(HTTP_ONLY),--http-only,) \
		--interface-latency

up-performance-overhead:
	$(PYTHON) orchestrator.py up-performance \
		--deployment $(DEPLOYMENT) \
		--iterations $(ITERATIONS) \
		--iperf3-duration $(IPERF3_DUR) \
		--udp-bandwidth $(UDP_BW) \
		$(if $(HTTP_ONLY),--http-only,) \
		--overhead \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

up-scalability-ping:
	$(PYTHON) orchestrator.py up-scalability \
		--deployment $(DEPLOYMENT) \
		--concurrency $(CONCURRENCY) \
		--rounds $(ROUNDS) \
		--ping-count $(PING_COUNT) \
		--ping-interval $(SCALABILITY_PING_INTERVAL) \
		--ping-sizes $(SCALABILITY_PING_SIZES) \
		--batch-size $(BATCH_SIZE)

up-scalability-ping-overhead:
	$(PYTHON) orchestrator.py up-scalability \
		--deployment $(DEPLOYMENT) \
		--concurrency $(CONCURRENCY) \
		--rounds $(ROUNDS) \
		--ping-count $(PING_COUNT) \
		--ping-interval $(SCALABILITY_PING_INTERVAL) \
		--ping-sizes $(SCALABILITY_PING_SIZES) \
		--batch-size $(BATCH_SIZE) \
		--overhead \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

up-scalability-throughput:
	$(PYTHON) orchestrator.py up-scalability \
		--deployment $(DEPLOYMENT) \
		--concurrency $(CONCURRENCY) \
		--rounds $(ROUNDS) \
		--iperf3-duration $(IPERF3_DUR) \
		--batch-size $(BATCH_SIZE) \
		--scalability-mode throughput

up-scalability-throughput-overhead:
	$(PYTHON) orchestrator.py up-scalability \
		--deployment $(DEPLOYMENT) \
		--concurrency $(CONCURRENCY) \
		--rounds $(ROUNDS) \
		--iperf3-duration $(IPERF3_DUR) \
		--batch-size $(BATCH_SIZE) \
		--scalability-mode throughput \
		--overhead \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)

resource-monitor:
	$(PYTHON) orchestrator.py resource-monitor \
		--deployment $(DEPLOYMENT) \
		--duration $(DURATION) \
		--interval $(INTERVAL) \
		$(_PERF_FLAG) \
		$(_BPF_FLAG)
	@echo "Output: results/resource_$(DEPLOYMENT)[_<N>].csv  (single flat CSV)"

clean:
	rm -rf results/

help:
	@echo "Usage:"
	@echo "  make run                                                   # cp-registration, baseline"
	@echo "  make run DEPLOYMENT=sgx ITERATIONS=30                      # sgx, 30 iterations"
	@echo ""
	@echo "  make cp-registration DEPLOYMENT=baseline ITERATIONS=30    # registration latency"
	@echo "  make cp-registration-nf DEPLOYMENT=baseline ITERATIONS=30      # + NF processing times (_nf.csv)"
	@echo "  make cp-registration-iface DEPLOYMENT=baseline ITERATIONS=30   # + interface latency (_iface.csv)"
	@echo "  make cp-registration-nf-iface DEPLOYMENT=baseline ITERATIONS=30           # + NF timing + interface latency"
	@echo "  make cp-registration-nf-iface DEPLOYMENT=hybrid ITERATIONS=30 OVERHEAD=1  # + NF timing + iface + overhead"
	@echo "  make cp-registration-nf-iface DEPLOYMENT=hybrid OVERHEAD=1 PERF=1 BPF=1   # all measurements"
	@echo ""
	@echo "  make cp-scalability CONCURRENCY=10                         # 10 concurrent UEs, 5 rounds"
	@echo ""
	@echo "  make cp-service-request WARMSTART=1                        # service request with warmstart"
	@echo ""
	@echo "  make up-performance                                        # TCP+UDP+HTTP, 30 iters"
	@echo "  make up-performance-nf DEPLOYMENT=sgx                     # + NF processing times (_nf.csv)"
	@echo "  make up-performance-iface DEPLOYMENT=hybrid               # + interface latency (_iface.csv)"
	@echo "  make up-performance IPERF3_DUR=60 UDP_BW=500M              # longer test, higher UDP load"
	@echo "  make up-performance-overhead DEPLOYMENT=sgx               # + CPU/mem/TEE overhead (_overhead.csv)"
	@echo "  make up-performance-overhead DEPLOYMENT=sgx PERF=1 BPF=1 # all layers"
	@echo ""
	@echo "  make up-scalability-ping CONCURRENCY=10                    # concurrent ping, N=10"
	@echo "  make up-scalability-ping-overhead CONCURRENCY=50 DEPLOYMENT=sgx PERF=1 BPF=1  # + overhead"
	@echo ""
	@echo "  make cp-registration-overhead DEPLOYMENT=baseline ITERATIONS=30  # + CPU/mem/TEE overhead (_overhead.csv)"
	@echo "  make cp-registration-overhead DEPLOYMENT=sgx PERF=1            # + perf stat (IPC, cache-miss)"
	@echo "  make cp-registration-overhead DEPLOYMENT=sgx PERF=1 BPF=1      # + perf stat + bpftrace OCALLs"
	@echo "  make cp-registration-overhead-full DEPLOYMENT=sgx              # all layers (proc + perf + bpf)"
	@echo ""
	@echo "  make resource-monitor DEPLOYMENT=baseline DURATION=60     # standalone: sample all NFs for 60s"
	@echo "  make resource-monitor DEPLOYMENT=sgx DURATION=120          # standalone during a load test"
	@echo "  make resource-monitor DEPLOYMENT=sgx DURATION=120 PERF=1 BPF=1  # all layers"
	@echo ""
	@echo "  make clean                                                 # remove results/"
