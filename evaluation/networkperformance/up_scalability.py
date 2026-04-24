import re
import json
import statistics
import subprocess
import time
import threading
from queue import Queue, Empty

from .base import UERANSIMBase, _stream_to_queue

_LOCAL_HOSTS = ('127.0.0.1', 'localhost', '192.0.2.10')

_RTT_RE  = re.compile(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms')
_LOSS_RE = re.compile(r'([\d.]+)% packet loss')


def _parse_ping(text):
    rtt  = _RTT_RE.search(text)
    loss = _LOSS_RE.search(text)
    return {
        'rtt_min_ms':  float(rtt.group(1))  if rtt  else None,
        'rtt_avg_ms':  float(rtt.group(2))  if rtt  else None,
        'rtt_max_ms':  float(rtt.group(3))  if rtt  else None,
        'rtt_mdev_ms': float(rtt.group(4))  if rtt  else None,
        'loss_pct':    float(loss.group(1)) if loss else None,
    }


class UPScalabilityExperiment(UERANSIMBase):

    def __init__(self, host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                 imsi_base, ping_target,
                 ssh_key=None, timeout=60, cooldown=2, batch_size=10,
                 iperf3_server=None, upf_host=None, upf_user=None, upf_key=None):
        super().__init__(host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                         ssh_key, timeout, cooldown)
        self.imsi_base     = str(imsi_base)
        self.ping_target   = ping_target
        self.batch_size    = batch_size
        self.iperf3_server = iperf3_server
        self.upf_host      = upf_host
        self.upf_user      = upf_user
        self.upf_key       = upf_key

    def _start_ues(self, concurrency):
        # Launch UEs in sequential batches - wait for each batch to fully attach
        # before starting the next, avoiding gNB SIB overload and TUN race conditions.
        n_batches  = (concurrency + self.batch_size - 1) // self.batch_size
        imsi_base  = int(self.imsi_base)
        procs      = []
        n_attached = 0

        for b in range(n_batches):
            start = b * self.batch_size
            count = min(self.batch_size, concurrency - start)
            imsi  = imsi_base + start
            cmd   = (f'sudo {self.ue_binary} -c {self.ue_config} '
                     f'-n {count} -i imsi-{imsi}')
            proc  = self._ssh(cmd)
            q     = Queue()
            threading.Thread(target=_stream_to_queue,
                             args=(proc.stdout, q), daemon=True).start()
            procs.append(proc)
            time.sleep(1)  # let gNB SIB broadcast settle before UEs scan
            print(f'  [ues] batch {b+1}/{n_batches}: {count} UEs at imsi-{imsi}')

            # Wait for all UEs in this batch before launching the next
            batch_done = 0
            deadline   = time.time() + max(self.timeout, count * 10)
            while time.time() < deadline and batch_done < count:
                try:
                    line = q.get_nowait()
                    # cell-selection failures are transient - UERANSIM retries automatically
                    if 'cell selection' in line.lower():
                        continue
                    if any(w in line.lower() for w in ('error', 'fail', 'denied', 'reject')):
                        print(f'    {line}')
                    if 'Connection setup for PDU session' in line:
                        batch_done += 1
                        n_attached += 1
                        print(f'    [ues] {n_attached}/{concurrency} attached')
                except Empty:
                    pass
                time.sleep(0.05)

            if batch_done < count:
                print(f'  [ues] WARNING: {batch_done}/{count} attached in batch {b+1}')

        print(f'  [ues] {n_attached}/{concurrency} total attached')
        return procs, n_attached

    def _ping_concurrent(self, concurrency, size, count, interval):
        results = [None] * concurrency

        def run(idx):
            cmd = (f'sudo ping -c {count} -i {interval} -s {size} '
                   f'-I uesimtun{idx} {self.ping_target} 2>&1')
            out = self._ssh(cmd).stdout.read().decode()
            results[idx] = _parse_ping(out)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(concurrency)]
        for t in threads: t.start()
        for t in threads: t.join()
        return results

    def _agg_ping(self, concurrency, round_num, size, per_ue):
        avgs   = [r['rtt_avg_ms']  for r in per_ue if r and r['rtt_avg_ms']  is not None]
        mdevs  = [r['rtt_mdev_ms'] for r in per_ue if r and r['rtt_mdev_ms'] is not None]
        losses = [r['loss_pct']    for r in per_ue if r and r['loss_pct']    is not None]
        return {
            'concurrency':   concurrency,
            'round':         round_num,
            'size_bytes':    size,
            'mean_rtt_ms':   round(statistics.mean(avgs), 3)   if avgs   else None,
            'min_ue_rtt_ms': round(min(avgs), 3)               if avgs   else None,
            'max_ue_rtt_ms': round(max(avgs), 3)               if avgs   else None,
            'p95_ue_rtt_ms': _percentile(avgs, 0.95),
            'p99_ue_rtt_ms': _percentile(avgs, 0.99),
            'mean_mdev_ms':  round(statistics.mean(mdevs), 3)  if mdevs  else None,
            'mean_loss_pct': round(statistics.mean(losses), 3) if losses else None,
        }

    def run(self, concurrency, rounds=5, ping_count=2000,
            ping_sizes=(1400,), ping_interval=0.01, monitor=None):
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(2)
        self._ssh(
            "ip link show | grep -o 'uesimtun[0-9]*' | xargs -r sudo ip link delete 2>/dev/null; true"
        ).wait()

        gnb_proc = self._start_gnb()
        ue_procs = []
        try:
            if monitor:
                monitor.mark_phase('registration')
            ue_procs, n_attached = self._start_ues(concurrency)
            if n_attached == 0:
                raise RuntimeError('No UEs attached')
            if monitor:
                monitor.mark_phase('ping')

            pps_per_ue = round(1 / ping_interval)
            print(f'  [ping load]  {pps_per_ue} pps/UE x {n_attached} UEs'
                  f' = {n_attached * pps_per_ue} pps total'
                  f'  |  {round(ping_count * ping_interval, 1)}s per measurement')
            results = []
            for r in range(1, rounds + 1):
                print(f'\n  [up-scalability-ping] round {r}/{rounds}, N={n_attached}')
                for size in ping_sizes:
                    per_ue = self._ping_concurrent(n_attached, size, ping_count, ping_interval)
                    agg    = self._agg_ping(n_attached, r, size, per_ue)
                    results.append(agg)
                    print(f'    size={size}B  mean={agg["mean_rtt_ms"]}ms  '
                          f'worst_ue={agg["max_ue_rtt_ms"]}ms  '
                          f'mdev={agg["mean_mdev_ms"]}ms  loss={agg["mean_loss_pct"]}%')
                time.sleep(self.cooldown)
            return results
        finally:
            self._ssh('sudo pkill -f nr-ue; sudo pkill -f nr-gnb; true').wait()
            for p in ue_procs:
                p.kill()
                p.wait()
            gnb_proc.kill()
            gnb_proc.wait()


    # throughput scalability helpers

    def _upf_run(self, cmd):
        """Run a shell command on the UPF host (local or remote)."""
        if self.upf_host in _LOCAL_HOSTS:
            return subprocess.Popen(cmd, shell=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ssh = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes',
               '-o', 'IdentitiesOnly=yes']
        if self.upf_key:
            ssh += ['-i', self.upf_key]
        ssh += [f'{self.upf_user}@{self.upf_host}', cmd]
        return subprocess.Popen(ssh, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _start_iperf3_servers(self, n, base_port=5201):
        ports = ' '.join(str(base_port + i) for i in range(n))
        self._upf_run(f'for p in {ports}; do iperf3 -s -p $p -D; done').wait()

    def _stop_iperf3_servers(self):
        self._upf_run('pkill -f "iperf3 -s" 2>/dev/null; true').wait()

    def _get_ue_ips(self, n):
        cmd = (f'for i in $(seq 0 {n-1}); do '
               f'ip -4 addr show uesimtun$i 2>/dev/null | awk \'/inet /{{print $2}}\' | cut -d/ -f1; '
               f'done')
        out = self._ssh(cmd).stdout.read().decode().strip().split('\n')
        return [ip.strip() or None for ip in out]

    def _iperf3_concurrent(self, ips, base_port, duration):
        results = [None] * len(ips)

        def run(idx, ip):
            if ip is None:
                return
            cmd = (f'iperf3 -c {self.iperf3_server} -p {base_port + idx} '
                   f'-B {ip} -t {duration} -R -J 2>&1')
            out = self._ssh(cmd).stdout.read().decode()
            try:
                bps = json.loads(out)['end']['sum_received']['bits_per_second']
                results[idx] = bps
            except Exception:
                results[idx] = None

        threads = [threading.Thread(target=run, args=(i, ips[i])) for i in range(len(ips))]
        for t in threads: t.start()
        for t in threads: t.join()
        return results

    def _agg_tp(self, bps_list, round_num, concurrency):
        valid = [b for b in bps_list if b is not None]
        return {
            'round':       round_num,
            'concurrency': concurrency,
            'n_success':   len(valid),
            'total_bps':   sum(valid) if valid else None,
            'mean_ue_bps': statistics.mean(valid) if valid else None,
            'min_ue_bps':  min(valid) if valid else None,
            'max_ue_bps':  max(valid) if valid else None,
            'p95_ue_bps':  _percentile(valid, 0.95),
        }

    def run_throughput(self, concurrency, rounds=5, duration=30,
                       base_port=5201, monitor=None):
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(2)
        self._ssh(
            "ip link show | grep -o 'uesimtun[0-9]*' | xargs -r sudo ip link delete 2>/dev/null; true"
        ).wait()
        self._stop_iperf3_servers()
        self._start_iperf3_servers(concurrency, base_port)

        gnb_proc = self._start_gnb()
        ue_procs = []
        try:
            if monitor: monitor.mark_phase('registration')
            ue_procs, n_attached = self._start_ues(concurrency)
            if n_attached == 0:
                raise RuntimeError('No UEs attached')

            ips = self._get_ue_ips(n_attached)
            print(f'  [ue ips] {ips}')

            if monitor: monitor.mark_phase('throughput')
            pprint = lambda a: (
                f'  total={a["total_bps"]/1e6:.1f} Mbps'
                f'  mean_ue={a["mean_ue_bps"]/1e6:.1f} Mbps'
                f'  ok={a["n_success"]}/{concurrency}'
            ) if a['total_bps'] else f'  ok={a["n_success"]}/{concurrency}'

            results = []
            for r in range(1, rounds + 1):
                print(f'\n  [up-scalability-throughput] round {r}/{rounds}, N={n_attached}')
                bps  = self._iperf3_concurrent(ips, base_port, duration)
                agg  = self._agg_tp(bps, r, n_attached)
                results.append(agg)
                print(pprint(agg))
                time.sleep(self.cooldown)
            return results
        finally:
            self._stop_iperf3_servers()
            self._ssh('sudo pkill -f nr-ue; sudo pkill -f nr-gnb; true').wait()
            for p in ue_procs:
                p.kill(); p.wait()
            gnb_proc.kill(); gnb_proc.wait()


def _percentile(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 3)
