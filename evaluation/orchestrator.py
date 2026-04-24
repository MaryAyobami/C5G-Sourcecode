#!/usr/bin/env python3
import argparse
import configparser
import csv
import os
import time

from networkperformance.cp_registration import CPRegistrationExperiment
from networkperformance.cp_scalability import CPScalabilityExperiment
from networkperformance.cp_service_request import CPServiceRequestExperiment
from networkperformance.up_performance import UPPerformanceExperiment
from networkperformance.up_scalability import UPScalabilityExperiment
from networkperformance.overhead_monitor import OverheadMonitor, build_overhead_monitor
from networkperformance.nf_processing import build_collector
from networkperformance.interface_latency import build_iface_collector

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config', 'deployments.ini')
SERVERS_FILE = os.path.join(os.path.dirname(__file__), 'config', 'servers.ini')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), 'results')
_UPF_SERVER = {'baseline': 'sgx_local', 'sgx': 'sgx_local', 'hybrid': 'tdx_upf'}


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg


def load_servers():
    srv = configparser.ConfigParser()
    srv.read(SERVERS_FILE)
    return srv


def _ip_to_server(ip, srv):
    """Map an NF IP address to a servers.ini section name."""
    if ip.startswith('127.'):
        return 'sgx_local'
    for section in srv.sections():
        s = srv[section]
        if s.get('host') == ip or s.get('nf_ip') == ip:
            return section
    return None




def _next_available_path(path):
    """Return path, or append _N before extension if it already exists."""
    if not os.path.exists(path):
        return path

    stem, ext = os.path.splitext(path)
    idx = 2
    while True:
        candidate = f'{stem}_{idx}{ext}'
        if not os.path.exists(candidate):
            return candidate
        idx += 1


def _csv_path(name, deployment, suffix=''):
    tag = f'_{suffix}' if suffix else ''
    base = os.path.join(RESULTS_DIR, f'{name}_{deployment}{tag}.csv')
    return _next_available_path(base)


def _save_nf_csv(results, output_csv):
    """Extract _nf dicts from results and save to a companion _nf.csv."""
    nf_rows = [{'iteration': r['iteration'], **r['_nf']}
               for r in results if r.get('_nf')]
    if not nf_rows:
        return
    nf_path = output_csv.replace('.csv', '_nf.csv')
    fields = list(nf_rows[0].keys())
    with open(nf_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(nf_rows)
    print(f'NF timing saved to: {nf_path}')



def _save_iface_csv(results, output_csv, extra=None):
    """Extract _iface dicts from results and save to a companion _iface.csv."""
    rows = [{'iteration': r['iteration'], **r['_iface']}
            for r in results if r.get('_iface')]
    if extra:
        for row in rows:
            row.update(extra)
    if not rows:
        return
    iface_path = output_csv.replace('.csv', '_iface.csv')
    fields = list(rows[0].keys())
    with open(iface_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    print(f'Interface latency saved to: {iface_path}')


# cp-registration

def run_cp_registration(deployment, iterations, warmstart, output_csv,
                        nf_timing=False, interface_latency=False,
                        overhead=False, use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()
    ueransim = cfg['ueransim']

    if interface_latency:
        collector = build_iface_collector(deployment, cfg, srv)
    elif nf_timing:
        collector = build_collector(deployment, cfg, srv)
    else:
        collector = None
    imsi = f'imsi-{ueransim["ue_imsi_base"]}' if (nf_timing or interface_latency) else None

    experiment = CPRegistrationExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 30)),
        cooldown=int(ueransim.get('cooldown', 5)),
        collector=collector,
        imsi=imsi,
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    if output_csv is None:
        output_csv = _csv_path('cp_registration', deployment)

    fields = ['iteration', 'success', 'reg_time_ms', 'pdu_time_ms', 'total_attach_ms']

    print(f'Experiment : cp-registration')
    print(f'Deployment : {deployment}')
    print(f'Warmup     : {warmstart}')
    print(f'Iterations : {iterations}')
    print(f'Output     : {output_csv}')
    print(f'Host       : {ueransim["user"]}@{ueransim["host"]}')
    print()

    if overhead:
        monitor = build_overhead_monitor(deployment, cfg, srv,
                                         use_perf=use_perf, use_bpf=use_bpf)
        monitor.start(interval=1)

    results = experiment.run(iterations, warmstart)

    if overhead:
        monitor.stop(output_csv.replace('.csv', '_overhead.csv'))

    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    print(f'Results saved to: {output_csv}')

    if nf_timing:
        _save_nf_csv(results, output_csv)

    if interface_latency:
        n2_ping = collector.ping_n2_path()
        n3_ping = collector.ping_n3_path()
        print(f'N2 path ping: {n2_ping}ms  N3 path ping: {n3_ping}ms')
        _save_iface_csv(results, output_csv,
                        extra={'n2_ping_ms': n2_ping, 'n3_ping_ms': n3_ping})



# cp-scalability

def run_cp_scalability(deployment, concurrency, rounds, breaking_point, recovery,
                       max_concurrency, baseline_ms, output_csv,
                       overhead=False, use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()
    ueransim = cfg['ueransim']

    ue_max_count = int(ueransim.get('ue_max_count', 20))
    if max_concurrency is None:
        max_concurrency = ue_max_count

    experiment = CPScalabilityExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        imsi_base=ueransim['ue_imsi_base'],
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 30)),
        cooldown=int(ueransim.get('cooldown', 5)),
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)

    scalability_fields = [
        'round', 'concurrency', 'success_count', 'fail_count', 'success_rate',
        'min_reg_ms', 'avg_reg_ms', 'stddev_reg_ms',
        'p50_reg_ms', 'p95_reg_ms', 'p99_reg_ms', 'max_reg_ms',
        'reg_per_s',
    ]

    if breaking_point:
        mode = 'breaking_point'
        fields = scalability_fields
    elif recovery:
        mode = f'recovery_{concurrency}'
        fields = ['concurrency', 'recovery_ms', 'overload_success_rate']
    else:
        mode = f'concurrent_{concurrency}'
        fields = scalability_fields

    if output_csv is None:
        output_csv = _csv_path('cp_scalability', deployment, mode)

    print(f'Experiment      : cp-scalability')
    print(f'Deployment      : {deployment}')
    print(f'Mode            : {mode}')
    if not breaking_point:
        print(f'Concurrency     : {concurrency}')
    if breaking_point:
        print(f'Max concurrency : {max_concurrency}')
    if not breaking_point and not recovery:
        print(f'Rounds          : {rounds}')
    print(f'Output          : {output_csv}')
    print(f'Host            : {ueransim["user"]}@{ueransim["host"]}')
    print()

    if overhead:
        monitor = build_overhead_monitor(deployment, cfg, srv,
                                         use_perf=use_perf, use_bpf=use_bpf)
        monitor.start(interval=1)

    results = experiment.run(
        concurrency=concurrency,
        rounds=rounds,
        breaking_point=breaking_point,
        recovery=recovery,
        max_concurrency=max_concurrency,
        baseline_ms=baseline_ms,
    )

    if overhead:
        monitor.stop(output_csv.replace('.csv', '_overhead.csv'))

    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    print(f'Results saved to: {output_csv}')


# cp-service-request

def run_cp_service_request(deployment, iterations, warmstart, output_csv,
                           overhead=False, use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()
    ueransim = cfg['ueransim']

    nr_cli = os.path.join(os.path.dirname(ueransim['gnb_binary']), 'nr-cli')

    experiment = CPServiceRequestExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        nr_cli=nr_cli,
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 30)),
        cooldown=int(ueransim.get('cooldown', 5)),
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    if output_csv is None:
        output_csv = _csv_path('cp_service_request', deployment)

    fields = ['iteration', 'success', 'service_request_ms', 'cm_idle_to_connected_ms']

    print(f'Experiment : cp-service-request')
    print(f'Deployment : {deployment}')
    print(f'Warmup     : {warmstart}')
    print(f'Iterations : {iterations}')
    print(f'Output     : {output_csv}')
    print(f'Host       : {ueransim["user"]}@{ueransim["host"]}')
    print()

    if overhead:
        monitor = build_overhead_monitor(deployment, cfg, srv,
                                         use_perf=use_perf, use_bpf=use_bpf)
        monitor.start(interval=1)

    results = experiment.run(iterations, warmstart)

    if overhead:
        monitor.stop(output_csv.replace('.csv', '_overhead.csv'))

    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    print(f'Results saved to: {output_csv}')


# up-performance
def run_up_performance(deployment, iterations, output_csv,
                       iperf3_duration,
                       udp_bandwidth, http_only=False, nf_timing=False,
                       interface_latency=False,
                       overhead=False, use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()
    ueransim = cfg['ueransim']

    if interface_latency:
        collector = build_iface_collector(deployment, cfg, srv)
    elif nf_timing:
        collector = build_collector(deployment, cfg, srv)
    else:
        collector = None

    experiment = UPPerformanceExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        iperf3_server=ueransim['iperf3_server'],
        iperf3_port=ueransim.get('iperf3_port', '5201'),
        http_url=ueransim['http_url'],
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 60)),
        cooldown=int(ueransim.get('cooldown', 2)),
        collector=collector,
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    if output_csv is None:
        output_csv = _csv_path('up_performance', deployment)

    fields = (['iteration'] +
              ['tcp_ul_mbps', 'tcp_dl_mbps', 'tcp_ul_retransmits', 'tcp_dl_retransmits',
               'udp_ul_mbps', 'udp_ul_jitter_ms', 'udp_ul_loss_pct',
               'udp_dl_mbps', 'udp_dl_jitter_ms', 'udp_dl_loss_pct',
               'udp_small_dl_mbps', 'udp_small_dl_jitter_ms',
               'udp_small_dl_loss_pct', 'udp_small_dl_pps',
               'http_dl_mbps', 'http_time_s', 'http_ttfb_s',
               'http_ul_mbps', 'http_ul_time_s'])

    print(f'Experiment    : up-performance')
    print(f'Deployment    : {deployment}')
    print(f'Iterations    : {iterations}')
    print(f'iperf3 server : {ueransim["iperf3_server"]}  duration={iperf3_duration}s')
    print(f'UDP bandwidth : {udp_bandwidth}')
    print(f'HTTP URL      : {ueransim["http_url"]}')
    print(f'Output        : {output_csv}')
    print()

    if overhead:
        monitor = build_overhead_monitor(deployment, cfg, srv,
                                         use_perf=use_perf, use_bpf=use_bpf)
        monitor.start(interval=1)

    results = experiment.run(iterations, iperf3_duration, udp_bandwidth,
                             http_only=http_only)

    if overhead:
        monitor.stop(output_csv.replace('.csv', '_overhead.csv'))

    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    print(f'Results saved to: {output_csv}')

    if nf_timing:
        _save_nf_csv(results, output_csv)

    if interface_latency:
        n2_ping = collector.ping_n2_path()
        n3_ping = collector.ping_n3_path()
        print(f'N2 path ping: {n2_ping}ms  N3 path ping: {n3_ping}ms')
        _save_iface_csv(results, output_csv,
                        extra={'n2_ping_ms': n2_ping, 'n3_ping_ms': n3_ping})


# up-ramp: UDP bandwidth ramp (capacity knee discovery)
def run_up_ramp(deployment, output_csv, iperf3_duration, rates_ul, rates_dl):
    cfg = load_config()
    ueransim = cfg['ueransim']

    experiment = UPPerformanceExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        iperf3_server=ueransim['iperf3_server'],
        iperf3_port=ueransim.get('iperf3_port', '5201'),
        http_url=ueransim['http_url'],
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 60)),
        cooldown=int(ueransim.get('cooldown', 2)),
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    if output_csv is None:
        output_csv = _csv_path('up_ramp', deployment)

    print(f'Experiment    : up-ramp')
    print(f'Deployment    : {deployment}')
    print(f'UL rates      : {rates_ul} Mbps')
    print(f'DL rates      : {rates_dl} Mbps')
    print(f'Duration/step : {iperf3_duration}s')
    print(f'Output        : {output_csv}\n')

    results = experiment.run_ramp(rates_ul, rates_dl, iperf3_duration)

    fields = ['direction', 'target_mbps', 'achieved_mbps', 'jitter_ms', 'loss_pct']
    with open(output_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    print(f'Results saved to: {output_csv}')


# up-scalability

def run_up_scalability(deployment, concurrency, rounds, output_csv,
                       ping_count, ping_sizes, ping_interval, batch_size,
                       iperf3_duration=30, mode='ping',
                       overhead=False, use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()
    ueransim = cfg['ueransim']
    upf_srv = srv[_UPF_SERVER.get(deployment, 'sgx_local')]

    experiment = UPScalabilityExperiment(
        host=ueransim['host'],
        user=ueransim['user'],
        gnb_binary=ueransim['gnb_binary'],
        gnb_config=ueransim['gnb_config'],
        ue_binary=ueransim['ue_binary'],
        ue_config=ueransim['ue_config'],
        imsi_base=ueransim['ue_imsi_base'],
        ping_target=ueransim['ping_target'],
        ssh_key=ueransim.get('ssh_key', None),
        timeout=int(ueransim.get('timeout', 60)),
        cooldown=int(ueransim.get('cooldown', 2)),
        batch_size=batch_size,
        iperf3_server=ueransim['iperf3_server'],
        upf_host=upf_srv['host'],
        upf_user=upf_srv['user'],
        upf_key=upf_srv.get('key'),
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)

    if mode == 'throughput':
        if output_csv is None:
            output_csv = _csv_path('up_scalability', deployment, f'throughput_{concurrency}')
        fields = ['round', 'concurrency', 'n_success',
                  'total_bps', 'mean_ue_bps', 'min_ue_bps', 'max_ue_bps', 'p95_ue_bps']
        print(f'Experiment    : up-scalability-throughput')
    else:
        if output_csv is None:
            output_csv = _csv_path('up_scalability', deployment, f'ping_{concurrency}')
        fields = ['concurrency', 'round', 'size_bytes',
                  'mean_rtt_ms', 'min_ue_rtt_ms', 'max_ue_rtt_ms', 'p95_ue_rtt_ms', 'p99_ue_rtt_ms',
                  'mean_mdev_ms', 'mean_loss_pct']
        print(f'Experiment    : up-scalability-ping')

    print(f'Deployment    : {deployment}')
    print(f'Concurrency   : {concurrency}')
    print(f'Rounds        : {rounds}')
    print(f'Output        : {output_csv}')
    print()

    monitor = None
    if overhead:
        monitor = build_overhead_monitor(deployment, cfg, srv,
                                         use_perf=use_perf, use_bpf=use_bpf)
        monitor.start(interval=1)

    if mode == 'throughput':
        results = experiment.run_throughput(concurrency, rounds, iperf3_duration,
                                            monitor=monitor)
    else:
        results = experiment.run(concurrency, rounds, ping_count, ping_sizes, ping_interval,
                                 monitor=monitor)

    if overhead:
        monitor.stop(output_csv.replace('.csv', '_overhead.csv'))

    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    print(f'Results saved to: {output_csv}')


# resource-monitor

def run_resource_monitor(deployment, duration, interval, output_csv,
                         use_perf=False, use_bpf=False):
    cfg = load_config()
    srv = load_servers()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    if output_csv is None:
        output_csv = _csv_path('resource', deployment)

    monitor = build_overhead_monitor(deployment, cfg, srv,
                                      use_perf=use_perf, use_bpf=use_bpf)

    print(f'Experiment : resource-monitor')
    print(f'Deployment : {deployment}')
    print(f'Duration   : {duration}s  Interval: {interval}s')
    print(f'Output     : {output_csv}')
    print()

    monitor.start(interval=interval)
    print(f'  Monitoring for {duration}s ... (Ctrl+C to stop early)')
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        print('\n  [interrupted]')
    finally:
        monitor.stop(output_csv)


# CLI
def main():
    parser = argparse.ArgumentParser(description='5G evaluation orchestrator')
    parser.add_argument('metric',
                        choices=['cp-registration', 'cp-scalability', 'cp-service-request',
                                 'up-performance', 'up-ramp', 'up-scalability',
                                 'resource-monitor'],
                        help='Metric to measure')
    parser.add_argument('--deployment',
                        choices=['baseline', 'sgx', 'hybrid'],
                        default='baseline')
    parser.add_argument('--iterations',
                        type=int,
                        default=10)
    parser.add_argument('--warmstart',
                        action='store_true',
                        default=False)
    parser.add_argument('--nf-timing',
                        action='store_true',
                        default=False,
                        help='Collect NF processing times and save to a companion _nf.csv')
    parser.add_argument('--interface-latency',
                        action='store_true',
                        default=False,
                        help='Measure interface latencies (N2/N3/N4/SBI) and save to _iface.csv')
    parser.add_argument('--overhead',
                        action='store_true',
                        default=False,
                        help='Sample CPU/memory/TEE overhead during the experiment '
                             '(1s interval, saves to _overhead.csv)')
    parser.add_argument('--perf',
                        action='store_true',
                        default=False,
                        help='Enable perf stat hardware counters (IPC, cache-miss) '
                             'per NF - requires perf on target hosts (use with --overhead)')
    parser.add_argument('--bpf',
                        action='store_true',
                        default=False,
                        help='Enable bpftrace OCALL counting for Gramine-SGX NFs '
                             '(use with --overhead; SGX/hybrid deployments only)')
    parser.add_argument('--output',
                        default=None,
                        help='Output CSV path (auto-generated if not set)')

    # cp-scalability options
    parser.add_argument('--concurrency',
                        type=int,
                        default=None,
                        help='Number of concurrent UEs (fixed concurrency or recovery mode)')
    parser.add_argument('--rounds',
                        type=int,
                        default=5,
                        help='Rounds per concurrency level (default: 5)')
    parser.add_argument('--breaking-point',
                        action='store_true',
                        default=False,
                        help='Sweep concurrency levels until system breaks')
    parser.add_argument('--recovery-time',
                        action='store_true',
                        default=False,
                        help='Measure recovery time after overload (requires --concurrency)')
    parser.add_argument('--max-concurrency',
                        type=int,
                        default=None,
                        help='Ceiling for breaking-point sweep (default: ue_max_count from config)')
    parser.add_argument('--baseline-ms',
                        type=float,
                        default=None,
                        help='Baseline reg latency for recovery threshold (2x baseline)')

    # up-performance / up-scalability options
    parser.add_argument('--ping-count',
                        type=int,
                        default=1000,
                        help='Packets per ping measurement (default: 1000)')
    parser.add_argument('--ping-sizes',
                        nargs='+',
                        type=int,
                        default=[56, 512, 1024, 1400],
                        help='Packet sizes for ping (default: 56 512 1024 1400)')
    parser.add_argument('--iperf3-duration',
                        type=int,
                        default=30,
                        help='iperf3 test duration in seconds (default: 30)')
    parser.add_argument('--udp-bandwidth',
                        default='100M',
                        help='UDP send bandwidth for iperf3 (default: 100M)')
    parser.add_argument('--ramp-ul',
                        default='500,700,900',
                        help='up-ramp: comma-separated UL target rates in Mbps')
    parser.add_argument('--ramp-dl',
                        default='500,700,900',
                        help='up-ramp: comma-separated DL target rates in Mbps (iperf3 may crash >~700)')
    parser.add_argument('--http-only',
                        action='store_true',
                        default=False,
                        help='Skip ping/TCP/UDP, run HTTP download test only')
    parser.add_argument('--ping-interval',
                        type=float,
                        default=0.01,
                        help='Seconds between ping packets per UE for scalability ping '
                             '(default: 0.01 = 100 pps/UE; requires sudo on remote host)')
    parser.add_argument('--batch-size',
                        type=int,
                        default=10,
                        help='UEs per sequential batch (default: 10)')
    parser.add_argument('--scalability-mode',
                        choices=['ping', 'throughput'],
                        default='ping',
                        help='up-scalability mode: ping latency or iperf3 throughput (default: ping)')


    # resource-monitor options
    parser.add_argument('--duration',
                        type=int,
                        default=60,
                        help='Monitoring duration in seconds (default: 60)')
    parser.add_argument('--interval',
                        type=int,
                        default=1,
                        help='Sampling interval in seconds (default: 1)')
    args = parser.parse_args()

    if args.metric == 'cp-registration':
        run_cp_registration(args.deployment, args.iterations, args.warmstart, args.output,
                            nf_timing=args.nf_timing,
                            interface_latency=args.interface_latency,
                            overhead=args.overhead,
                            use_perf=args.perf,
                            use_bpf=args.bpf)

    elif args.metric == 'cp-service-request':
        run_cp_service_request(args.deployment, args.iterations, args.warmstart, args.output,
                               overhead=args.overhead,
                               use_perf=args.perf,
                               use_bpf=args.bpf)

    elif args.metric == 'cp-scalability':
        if not args.breaking_point and args.concurrency is None:
            parser.error('cp-scalability requires --concurrency N or --breaking-point')
        if args.recovery_time and args.concurrency is None:
            parser.error('--recovery-time requires --concurrency N')
        run_cp_scalability(
            deployment=args.deployment,
            concurrency=args.concurrency,
            rounds=args.rounds,
            breaking_point=args.breaking_point,
            recovery=args.recovery_time,
            max_concurrency=args.max_concurrency,
            baseline_ms=args.baseline_ms,
            output_csv=args.output,
            overhead=args.overhead,
            use_perf=args.perf,
            use_bpf=args.bpf,
        )

    elif args.metric == 'up-performance':
        run_up_performance(
            deployment=args.deployment,
            iterations=args.iterations,
            output_csv=args.output,
            iperf3_duration=args.iperf3_duration,
            udp_bandwidth=args.udp_bandwidth,
            http_only=args.http_only,
            nf_timing=args.nf_timing,
            interface_latency=args.interface_latency,
            overhead=args.overhead,
            use_perf=args.perf,
            use_bpf=args.bpf,
        )

    elif args.metric == 'up-ramp':
        run_up_ramp(
            deployment=args.deployment,
            output_csv=args.output,
            iperf3_duration=args.iperf3_duration,
            rates_ul=[int(x) for x in args.ramp_ul.split(',') if x.strip()],
            rates_dl=[int(x) for x in args.ramp_dl.split(',') if x.strip()],
        )

    elif args.metric == 'up-scalability':
        if args.concurrency is None:
            parser.error('up-scalability requires --concurrency N')
        run_up_scalability(
            deployment=args.deployment,
            concurrency=args.concurrency,
            rounds=args.rounds,
            output_csv=args.output,
            ping_count=args.ping_count,
            ping_sizes=tuple(args.ping_sizes),
            ping_interval=args.ping_interval,
            batch_size=args.batch_size,
            iperf3_duration=args.iperf3_duration,
            mode=args.scalability_mode,
            overhead=args.overhead,
            use_perf=args.perf,
            use_bpf=args.bpf,
        )

    elif args.metric == 'resource-monitor':
        run_resource_monitor(
            deployment=args.deployment,
            duration=args.duration,
            interval=args.interval,
            output_csv=args.output,
            use_perf=args.perf,
            use_bpf=args.bpf,
        )

if __name__ == '__main__':
    main()
