import re
import time
import threading
import statistics
from queue import Queue, Empty

from .base import UERANSIMBase, _stream_to_queue
from .cp_registration import parse_ue_log, parse_ue_timestamps

_IMSI_LOG_RE = re.compile(r'\[(\d+)\|')


def _percentile(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


class CPScalabilityExperiment(UERANSIMBase):

    def __init__(self, host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                 imsi_base, ssh_key=None, timeout=30, cooldown=5):
        super().__init__(host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                         ssh_key, timeout, cooldown)
        self.imsi_base = str(imsi_base)

    def _imsi_for(self, index):
        return str(int(self.imsi_base) + index)

    def _run_single_ue(self):
        """Launch one UE (base IMSI) for recovery probes. Returns metrics dict."""
        proc = self._ssh(f'sudo {self.ue_binary} -c {self.ue_config}')
        q = Queue()
        threading.Thread(target=_stream_to_queue, args=(proc.stdout, q), daemon=True).start()

        ue_lines = []
        success  = False
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                ue_lines.append(line)
                if 'Connection setup for PDU session' in line:
                    success = True
                    break
            except Empty:
                pass

        self._ssh('sudo pkill -9 -f nr-ue 2>/dev/null; true').wait()
        proc.kill()
        proc.wait()

        if not success:
            print(f'  [ue-single] FAILED - last lines:')
            for line in ue_lines[-10:]:
                print(f'    {line}')
            if not ue_lines:
                print(f'    (no output captured)')

        metrics = parse_ue_log(ue_lines)
        metrics['success'] = success
        return metrics

    def _run_concurrent_batch(self, concurrency):
        """Launch N UEs in a single nr-ue process (-n flag). Returns (results_list, elapsed_seconds)  """
        imsis        = [self._imsi_for(i) for i in range(concurrency)]
        per_ue_lines = {imsi: [] for imsi in imsis}
        success_set  = set()
        imsi_set     = set(imsis)

        cmd  = (f'sudo {self.ue_binary} -c {self.ue_config} '
                f'-n {concurrency} -i imsi-{self.imsi_base}')
        proc = self._ssh(cmd)
        q    = Queue()
        threading.Thread(target=_stream_to_queue, args=(proc.stdout, q), daemon=True).start()

        t_start  = time.time()
        deadline = t_start + self.timeout

        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                if concurrency == 1:
                    # UERANSIM uses single-UE log format with no IMSI prefix
                    per_ue_lines[imsis[0]].append(line)
                    if 'Connection setup for PDU session' in line:
                        success_set.add(imsis[0])
                else:
                    m = _IMSI_LOG_RE.search(line)
                    if m:
                        imsi = m.group(1)
                        if imsi in imsi_set:
                            per_ue_lines[imsi].append(line)
                            if 'Connection setup for PDU session' in line:
                                success_set.add(imsi)
                if len(success_set) == concurrency:
                    break
            except Empty:
                pass

        elapsed = time.time() - t_start

        self._ssh('sudo pkill -9 -f nr-ue 2>/dev/null; true').wait()
        proc.kill()
        proc.wait()

        results = []
        for i, imsi in enumerate(imsis):
            lines   = per_ue_lines[imsi]
            success = imsi in success_set

            if not success:
                print(f'  [ue-{i}] FAILED (imsi-{imsi}) - last lines:')
                for line in lines[-10:]:
                    print(f'    {line}')
                if not lines:
                    print(f'    (no output captured for this UE)')

            metrics = parse_ue_log(lines)
            metrics.update(parse_ue_timestamps(lines))
            metrics['success'] = success
            results.append(metrics)

        time.sleep(self.cooldown)
        return results, elapsed

    def _aggregate(self, concurrency, round_num, per_ue):
        success_list = [r for r in per_ue if r['success']]
        n_success    = len(success_list)
        n_fail       = concurrency - n_success
        reg_times    = [r['reg_time_ms'] for r in success_list if r['reg_time_ms'] is not None]

        def p(pct):
            return round(_percentile(reg_times, pct), 3) if reg_times else None

        success_rate = n_success / concurrency if concurrency else 0.0

        # Signaling rate: procedures completed per second, measured from UERANSIM log timestamps
        
        start_times = [r['ue_start_ts'] for r in per_ue       if r.get('ue_start_ts')]
        end_times   = [r['tun_up_ts']   for r in success_list if r.get('tun_up_ts')]

        if start_times and end_times:
            window_s  = (max(end_times) - min(start_times)).total_seconds()
            reg_per_s = round(n_success / window_s, 3) if window_s > 0 else None
        else:
            reg_per_s = None

        return {
            'round':          round_num,
            'concurrency':    concurrency,
            'success_count':  n_success,
            'fail_count':     n_fail,
            'success_rate':   round(success_rate, 4),
            'min_reg_ms':     round(min(reg_times), 3)               if reg_times else None,
            'avg_reg_ms':     round(statistics.mean(reg_times), 3)   if reg_times else None,
            'stddev_reg_ms':  round(statistics.pstdev(reg_times), 3) if len(reg_times) > 1 else None,
            'p50_reg_ms':     p(50),
            'p95_reg_ms':     p(95),
            'p99_reg_ms':     p(99),
            'max_reg_ms':     round(max(reg_times), 3)               if reg_times else None,
            'reg_per_s':      reg_per_s,
        }

    def run_concurrent(self, concurrency, rounds=5):
        """Launch N UEs simultaneously for each round."""
        results = []
        for r in range(1, rounds + 1):
            print(f'\n  [scalability] round {r}/{rounds}, concurrency={concurrency}')
            per_ue, _ = self._run_concurrent_batch(concurrency)
            agg = self._aggregate(concurrency, r, per_ue)
            results.append(agg)
            print(f'  success={agg["success_count"]}/{concurrency}  '
                  f'avg_reg={agg["avg_reg_ms"]}ms  '
                  f'reg_per_s={agg["reg_per_s"]}')
        return results

    def run_breaking_point(self, max_concurrency):
        """Sweep N=1,2,4,8,... until success_rate<95%."""
        levels = []
        n = 1
        while n < max_concurrency:
            levels.append(n)
            n *= 2
        levels.append(max_concurrency)
        levels = sorted(set(levels))

        results = []

        for n in levels:
            print(f'\n  [breaking-point] concurrency={n}')
            per_ue, elapsed = self._run_concurrent_batch(n)
            agg = self._aggregate(n, 1, per_ue)
            results.append(agg)
            print(f'  success_rate={agg["success_rate"]:.1%}  avg_reg={agg["avg_reg_ms"]}ms  '
                  f'reg_per_s={agg["reg_per_s"]}')

            if agg['success_rate'] < 0.95:
                print(f'  [breaking-point] success rate below 95% - stopping at N={n}')
                break

        return results

    def run_recovery(self, concurrency, baseline_ms=None):
        """Overload with N UEs, then probe a single UE every 5s until normal latency."""
        print(f'\n  [recovery] overloading with {concurrency} concurrent UEs...')
        per_ue, _ = self._run_concurrent_batch(concurrency)
        overload_end = time.time()

        n_success = sum(1 for r in per_ue if r['success'])
        overload_success_rate = round(n_success / concurrency, 4) if concurrency else 0.0
        threshold = (2 * baseline_ms) if baseline_ms else None

        print(f'  [recovery] overload done ({n_success}/{concurrency} succeeded). '
              f'Probing single UE every 5s...')

        recovery_ms = None
        for probe in range(1, 21):
            time.sleep(5)
            print(f'  [recovery] probe #{probe}...')
            result = self._run_single_ue()
            if result['success']:
                reg_ms = result['reg_time_ms']
                if threshold is None or (reg_ms is not None and reg_ms <= threshold):
                    recovery_ms = round((time.time() - overload_end) * 1000, 1)
                    print(f'  [recovery] recovered in {recovery_ms}ms (reg={reg_ms}ms)')
                    break
                else:
                    print(f'  [recovery] attached but slow: {reg_ms}ms > threshold {threshold}ms')
            else:
                print(f'  [recovery] probe #{probe} failed to attach')

        if recovery_ms is None:
            print(f'  [recovery] system did not recover within 20 probes')

        return {
            'concurrency':           concurrency,
            'recovery_ms':           recovery_ms,
            'overload_success_rate': overload_success_rate,
        }

    def run(self, concurrency=None, rounds=5, breaking_point=False,
            recovery=False, max_concurrency=None, baseline_ms=None):
        """Start gNB, run the requested mode, tear down gNB."""
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(1)

        gnb_proc = self._start_gnb()
        try:
            if breaking_point:
                return self.run_breaking_point(max_concurrency or concurrency or 1)
            elif recovery:
                return [self.run_recovery(concurrency, baseline_ms)]
            else:
                return self.run_concurrent(concurrency, rounds)
        finally:
            self._ssh('sudo pkill -f nr-gnb; true').wait()
            gnb_proc.kill()
            gnb_proc.wait()
