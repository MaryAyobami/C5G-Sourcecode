"""Resource utilization and TEE overhead measurement."""

import csv
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

NF_PATTERNS = {
    'amf': 'open5gs-amf',
    'smf': 'open5gs-smf',
    'upf': 'open5gs-upf',
    'udm': 'open5gs-udm',
    'ausf': 'open5gs-ausf',
    'pcf': 'open5gs-pcf',
    'nrf': 'open5gs-nrf',
    'scp': 'open5gs-scp',
}


_SNAP_SCRIPT = """\
import json, subprocess, time

PATTERNS = {patterns}
IFACES = set({ifaces})

def _net():
    nets = {{}}
    for ln in open('/proc/net/dev'):
        if ':' not in ln: continue
        iface, data = ln.split(':', 1)
        iface = iface.strip()
        if iface == 'lo' or (IFACES and iface not in IFACES): continue
        f = data.split()
        if len(f) >= 9:
            nets[iface] = (int(f[0]), int(f[8]))
    return nets

def _pid(pat):
    r = subprocess.run(['pgrep', '-f', pat], capture_output=True, text=True)
    pids = r.stdout.strip().split()
    if not pids: return None
    if len(pids) == 1: return pids[0]
    for pid in pids:
        try:
            if 'loader' in open(f'/proc/{{pid}}/cmdline').read(): return pid
        except Exception: pass
    return pids[-1]

def _proc(pid):
    st = open(f'/proc/{{pid}}/stat').read().split()
    cpu = int(st[13]) + int(st[14])
    s = {{}}
    for ln in open(f'/proc/{{pid}}/status'):
        k, *v = ln.split()
        s[k.rstrip(':')] = v[0] if v else '0'
    return {{
        'cpu': cpu,
        'rss': int(s.get('VmRSS', 0)),
        'vol': int(s.get('voluntary_ctxt_switches', 0)),
        'nvol': int(s.get('nonvoluntary_ctxt_switches', 0)),
    }}

nfs = {{}}; pids = {{}}
for name, pat in PATTERNS.items():
    pid = _pid(pat)
    if pid:
        try: nfs[name] = _proc(pid); pids[name] = pid
        except Exception: pass

print(json.dumps({{'ts': time.time(), 'net': _net(), 'nfs': nfs, 'pids': pids}}))
"""


def _ssh_cmd(host, user, key):
    if host in ('127.0.0.1', 'localhost', '192.0.2.10'):
        return None
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=10', '-o', 'IdentitiesOnly=yes']
    if key:
        cmd += ['-i', key]
    cmd += [f'{user}@{host}']
    return cmd


def _take_snapshot(host, user, key, nfs, ifaces):
    script = _SNAP_SCRIPT.format(
        patterns=repr({k: NF_PATTERNS[k] for k in nfs if k in NF_PATTERNS}),
        ifaces=repr(sorted(set(ifaces or []))),
    )
    ssh = _ssh_cmd(host, user, key)
    if ssh is None:
        cmd = ['sudo', 'python3', '-']
    else:
        cmd = ssh + ['sudo python3 -']
    try:
        r = subprocess.run(cmd, input=script.encode(),
                           capture_output=True, timeout=10)
        line = r.stdout.strip().decode()
        if line:
            return json.loads(line)
    except Exception:
        pass
    return None


def _delta(host_label, s0, s1):
    dt = max(s1['ts'] - s0['ts'], 0.001)
    HERTZ = os.sysconf('SC_CLK_TCK')
    row = {}

    # Network I/O (ogstun)
    n0, n1 = s0.get('net', {}), s1.get('net', {})
    for iface in set(list(n0) + list(n1)):
        rx0, tx0 = n0.get(iface, (0, 0))
        rx1, tx1 = n1.get(iface, (0, 0))
        row[f'{host_label}_{iface}_rx_kbps'] = round((rx1 - rx0) / dt / 1024, 1)
        row[f'{host_label}_{iface}_tx_kbps'] = round((tx1 - tx0) / dt / 1024, 1)

    # Per-NF process metrics
    for nf, p1 in s1.get('nfs', {}).items():
        p0 = s0.get('nfs', {}).get(nf, {})
        dcpu = p1['cpu'] - p0.get('cpu', p1['cpu'])
        row[f'{nf}_cpu_pct'] = round(dcpu / HERTZ / dt * 100, 2)
        row[f'{nf}_rss_mb'] = round(p1['rss'] / 1024, 1)
        row[f'{nf}_ctx_sw'] = (
            (p1['vol'] - p0.get('vol', p1['vol'])) +
            (p1['nvol'] - p0.get('nvol', p1['nvol']))
        )

    # TDX VM exits from KVM host debugfs (optional)
    k0, k1 = s0.get('kvm'), s1.get('kvm')
    if k0 and k1:
        def _rate(field):
            return round((k1.get(field, 0) - k0.get(field, 0)) / dt, 1)
        row[f'{host_label}_vmexit_rate'] = _rate('exits')
        row[f'{host_label}_vmexit_io_rate'] = _rate('io_exits')
        row[f'{host_label}_vmexit_irq_rate'] = _rate('irq_exits')
        row[f'{host_label}_vmexit_halt_rate'] = _rate('halt_exits')
        row[f'{host_label}_vmexit_hypercall_rate'] = _rate('hypercalls')

    return row


class PerfMonitor:
    """Runs perf stat per NF and parses IPC and cache_miss_pct."""

    def __init__(self):
        self._procs = {}
        self._output = {}
        self._start_ts = {}
        self._events = defaultdict(list)

    def start(self, host, user, key, nf_pids):
        """
        nf_pids: dict of {nf_name: pid_str}
        Starts one perf stat process per NF in background.
        """
        ssh = _ssh_cmd(host, user, key)
        for nf, pid in nf_pids.items():
            cmd_str = (f'sudo perf stat -p {pid}'
                       f' -e cycles,instructions,cache-misses,cache-references'
                       f' -I 1000 --field-separator , --no-big-num 2>&1')
            if ssh is None:
                cmd = ['bash', '-c', cmd_str]
            else:
                cmd = ssh + [cmd_str]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            self._procs[nf] = proc
            self._output[nf] = []
            self._start_ts[nf] = time.time()
            self._events[nf].clear()
            threading.Thread(target=self._read, args=(nf, proc.stdout),
                             daemon=True).start()

    def _read(self, nf, stream):
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', errors='replace').rstrip()
            if line:
                self._output[nf].append(line)
                parsed = self._parse_line(line)
                if parsed:
                    rel_ts, event, value = parsed
                    abs_ts = self._start_ts.get(nf, time.time()) + rel_ts
                    self._events[nf].append((abs_ts, event, value))

    def stop(self):
        """Kill perf processes. Returns {nf: {'ipc': float, 'cache_miss_pct': float}}."""
        for proc in self._procs.values():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
        time.sleep(0.3)
        return {nf: self._build_result(nf)
                for nf in self._procs.keys()}

    def _parse_line(self, line):
        """
        Parse one perf -I CSV line into (relative_ts_s, event, count_value).
        Keeps logic intentionally simple and robust to minor format variation.
        """
        parts = [p.strip() for p in line.split(',')]
        if not parts:
            return None
        try:
            rel_ts = float(parts[0])
        except Exception:
            return None

        events = ('cycles', 'instructions', 'cache-misses', 'cache-references')
        event_idx = None
        event = None
        for i, token in enumerate(parts[1:], 1):
            base = token.split(':', 1)[0]
            if base in events:
                event_idx = i
                event = base
                break
        if event_idx is None:
            return None

        # Value is usually immediately left of event; allow tiny fallback search.
        for j in (event_idx - 1, event_idx - 2, event_idx + 1, event_idx + 2):
            if j <= 0 or j >= len(parts):
                continue
            tok = parts[j].replace(',', '').strip()
            if not tok or tok.startswith('<'):
                continue
            try:
                value = float(tok)
                if value >= 0:
                    return (rel_ts, event, value)
            except ValueError:
                continue
        return None

    def _build_result(self, nf):
        points = self._events.get(nf, [])
        if not points:
            return {}

        # Aggregate event counters per interval timestamp.
        by_ts = {}
        for abs_ts, event, value in points:
            key = round(abs_ts, 3)
            bucket = by_ts.setdefault(key, {'ts': key})
            bucket[event] = value

        samples = []
        for key in sorted(by_ts.keys()):
            b = by_ts[key]
            cyc = b.get('cycles')
            ins = b.get('instructions')
            refs = b.get('cache-references')
            miss = b.get('cache-misses')

            sample = {'ts': b['ts']}
            has_metric = False
            if cyc and cyc > 0 and ins is not None:
                sample['ipc'] = round(ins / cyc, 3)
                has_metric = True
            if refs and refs > 0 and miss is not None:
                sample['cache_miss_pct'] = round(miss / refs * 100, 2)
                has_metric = True
            if has_metric:
                samples.append(sample)

        if not samples:
            return {}

        ipc_vals = [s['ipc'] for s in samples if s.get('ipc') is not None]
        cm_vals = [s['cache_miss_pct'] for s in samples
                   if s.get('cache_miss_pct') is not None]
        return {
            'ipc': round(sum(ipc_vals) / len(ipc_vals), 3) if ipc_vals else None,
            'cache_miss_pct': round(sum(cm_vals) / len(cm_vals), 2) if cm_vals else None,
            'samples': samples,
        }


class GramineOcallMonitor:
    """SGX enclave entry/exit and AEX rates via bpftrace uprobes on the Gramine loader."""

    # Uprobes must attach via curtask->tgid, not LOADER_PID: Gramine spawns one
    # host-PAL thread per enclave thread, and a PID filter drops worker ioctls.
    _LOADER = '/usr/lib/x86_64-linux-gnu/gramine/sgx/loader'

    # bpftrace script.
    # Prints "EENTER_RATE <nf> <n>" and "AEX_RATE <nf> <n>" every interval.
    _BPF_TMPL = """\
interval:s:{interval} {{
{rate_lines}
}}
{probe_lines}
"""

    def __init__(self):
        self._proc = None
        self._samples = defaultdict(list)
        self._thread = None

    def start(self, host, user, key, nf_pids, interval=1):
        """nf_pids: {nf_name: pid_str}  (pid_str is the loader TGID)"""
        if not nf_pids:
            return

        probe_lines = []
        rate_lines = []
        for nf, pid in nf_pids.items():
            tgid_filter = f'pid == {pid}'
            probe_lines += [
                f'uprobe:{self._LOADER}:sgx_profile_sample_ocall_inner /{tgid_filter}/'
                f' {{ @eenter_{nf}++; }}',
                f'uprobe:{self._LOADER}:sgx_profile_sample_ocall_outer /{tgid_filter}/'
                f' {{ @eexit_{nf}++; }}',
                f'uprobe:{self._LOADER}:sgx_profile_sample_aex /{tgid_filter}/'
                f' {{ @aex_{nf}++; }}',
            ]
            rate_lines += [
                f'  printf("EENTER_RATE {nf} %lld\\n", @eenter_{nf});'
                f' @eenter_{nf} = 0;',
                f'  printf("EEXIT_RATE {nf} %lld\\n", @eexit_{nf});'
                f' @eexit_{nf} = 0;',
                f'  printf("AEX_RATE {nf} %lld\\n", @aex_{nf});'
                f' @aex_{nf} = 0;',
            ]

        script = self._BPF_TMPL.format(
            interval=interval,
            probe_lines='\n'.join(probe_lines),
            rate_lines='\n'.join(rate_lines),
        )

        ssh = _ssh_cmd(host, user, key)
        if ssh is None:
            cmd = ['sudo', 'bpftrace', '-e', script]
        else:
            safe = script.replace("'", "'\\''")
            cmd = ssh + [f"sudo bpftrace -e '{safe}'"]

        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            self._samples.clear()
            self._thread = threading.Thread(
                target=self._read, args=(self._proc.stdout,), daemon=True)
            self._thread.start()
        except Exception as e:
            print(f'  [gramine] bpftrace failed to start: {e}')
            self._proc = None

    def _read(self, stream):
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', errors='replace').strip()
            # "EENTER_RATE smf 142"  or  "EEXIT_RATE smf 139"  or  "AEX_RATE smf 3"
            m = re.match(r'(EENTER|EEXIT|AEX)_RATE (\w+) (\d+)', line)
            if not m:
                if line and 'Attaching' not in line:
                    print(f'  [bpftrace] {line}')
                continue
            kind = m.group(1).lower() + '_rate'   # 'eenter_rate' or 'aex_rate'
            nf, val = m.group(2), int(m.group(3))
            ts = round(time.time(), 3)
            # Merge EENTER and AEX into the same per-second sample bucket.
            if self._samples[nf] and abs(self._samples[nf][-1]['ts'] - ts) < 1.5:
                self._samples[nf][-1][kind] = val
            else:
                self._samples[nf].append({'ts': ts, kind: val})

    def stop(self):
        """Returns {nf: {'eenter_rate_avg', 'aex_rate_avg', 'samples'}}."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
        time.sleep(0.2)
        result = {}
        for nf, samples in self._samples.items():
            eenter = [s['eenter_rate'] for s in samples if 'eenter_rate' in s]
            eexit = [s['eexit_rate'] for s in samples if 'eexit_rate' in s]
            aex = [s['aex_rate'] for s in samples if 'aex_rate' in s]
            if eenter or eexit or aex:
                result[nf] = {
                    'eenter_rate_avg': round(sum(eenter) / len(eenter), 1) if eenter else None,
                    'eexit_rate_avg': round(sum(eexit) / len(eexit), 1) if eexit else None,
                    'aex_rate_avg': round(sum(aex) / len(aex), 1) if aex else None,
                    'samples': samples,
                }
        return result


class KvmExitMonitor:
    """VM-exit counters from KVM host debugfs for TDX guests."""

    _VM_PATTERNS = {
        'tdx_amf': ['tdx-amf', 'tdx_amf', 'amf', 'td2'],
        'tdx_upf': ['tdx-upf', 'tdx_upf', 'upf'],
    }

    def __init__(self, host, user, key, target_labels):
        self._host = host
        self._user = user
        self._key = key
        self._labels = [l for l in target_labels if l in self._VM_PATTERNS]
        self._paths = {}
        self._discovery_done = False

    def _run(self, cmd_str, timeout=5):
        ssh = _ssh_cmd(self._host, self._user, self._key)
        if ssh is None:
            cmd = ['bash', '-lc', cmd_str]
        else:
            cmd = ssh + [cmd_str]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return ''

    def _discover_paths(self):
        if not self._labels:
            self._discovery_done = True
            return

        dirs_out = self._run("sudo -n bash -lc 'ls -d /sys/kernel/debug/kvm/*-* 2>/dev/null'")
        qemu_out = self._run("ps -eo pid,args | grep -Ei 'qemu|qemu-system|kvm' | grep -v grep")
        if not dirs_out or not qemu_out:
            self._discovery_done = True
            return

        kvm_dirs = [d.strip() for d in dirs_out.splitlines() if d.strip()]
        qemu_lines = [ln.strip() for ln in qemu_out.splitlines() if ln.strip()]

        for label in self._labels:
            pats = self._VM_PATTERNS.get(label, [])
            for ln in qemu_lines:
                low = ln.lower()
                if not any(p in low for p in pats):
                    continue
                parts = ln.split(maxsplit=1)
                if not parts:
                    continue
                pid = parts[0]
                path = next((d for d in kvm_dirs if f'/{pid}-' in d), None)
                if path:
                    self._paths[label] = path
                    break
        self._discovery_done = True

    def snapshot(self):
        """
        Returns {label: {'exits','io_exits','irq_exits','halt_exits','hypercalls'}}.
        Missing/unreadable labels are omitted.
        """
        if not self._discovery_done:
            self._discover_paths()
        if not self._paths:
            return {}

        out = {}
        for label, path in self._paths.items():
            cmd = (
                f"sudo -n cat {path}/exits {path}/io_exits {path}/irq_exits "
                f"{path}/halt_exits {path}/hypercalls 2>/dev/null"
            )
            raw = self._run(cmd)
            if not raw:
                continue
            vals = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(vals) < 5:
                continue
            try:
                out[label] = {
                    'exits': int(vals[0]),
                    'io_exits': int(vals[1]),
                    'irq_exits': int(vals[2]),
                    'halt_exits': int(vals[3]),
                    'hypercalls': int(vals[4]),
                }
            except ValueError:
                continue
        return out


class OverheadMonitor:
    """Combines /proc snapshots, perf stat, and bpftrace into per-second overhead rows."""

    def __init__(self, targets, use_perf=False, use_bpf=False, kvm_host=None):
        self._targets = targets
        self._use_perf = use_perf
        self._use_bpf = use_bpf
        self._rows = []
        self._stop_evt = threading.Event()
        self._thread = None
        self._phase = None
        self._perf = PerfMonitor() if use_perf else None
        self._bpf = GramineOcallMonitor() if use_bpf else None
        self._kvm = None
        self._perf_snap = {}
        self._bpf_snap = {}

        if kvm_host and kvm_host.get('host') and kvm_host.get('user'):
            tdx_labels = [t['host_label'] for t in targets
                          if t.get('host_label') in ('tdx_amf', 'tdx_upf')]
            if tdx_labels:
                self._kvm = KvmExitMonitor(
                    host=kvm_host['host'],
                    user=kvm_host['user'],
                    key=kvm_host.get('key'),
                    target_labels=tdx_labels,
                )

    def mark_phase(self, name):
        self._phase = name

    def snapshot(self):
        """Parallel /proc snapshot from all hosts. Returns opaque dict."""
        out = {}
        with ThreadPoolExecutor(max_workers=max(len(self._targets), 1)) as ex:
            futs = {
                ex.submit(_take_snapshot,
                          t['host'], t['user'], t.get('key'),
                          t['nfs'], t.get('ifaces', [])): t['host_label']
                for t in self._targets
            }
            for f in as_completed(futs):
                label = futs[f]
                snap  = f.result()
                if snap:
                    out[label] = snap
        if self._kvm and out:
            kvm_snap = self._kvm.snapshot()
            for label, counters in kvm_snap.items():
                if label in out:
                    out[label]['kvm'] = counters
        return out

    def delta(self, snap0, snap1):
        """Compute deltas between two snapshots. Returns flat metric dict."""
        row = {}
        for label in snap1:
            if label in snap0:
                row.update(_delta(label, snap0[label], snap1[label]))
        return row

    def start(self, interval=1):
        """Start background sampling. Optionally starts perf + bpftrace."""
        self._rows.clear()
        self._stop_evt.clear()

        # Start perf and bpftrace on first snapshot to get PIDs
        if self._use_perf or self._use_bpf:
            init_snap = self.snapshot()
            for label, snap in init_snap.items():
                pids = snap.get('pids', {})
                if not pids:
                    continue
                t = next((t for t in self._targets if t['host_label'] == label), None)
                if not t:
                    continue
                if self._use_perf:
                    self._perf.start(t['host'], t['user'], t.get('key'), pids)
                if self._use_bpf and t.get('is_sgx'):
                    self._bpf.start(t['host'], t['user'], t.get('key'),
                                    pids, interval=interval)
            self._prev_snap = init_snap
        else:
            self._prev_snap = self.snapshot()

        labels = [t['host_label'] for t in self._targets]
        modes = []
        if self._use_perf: modes.append('perf-stat')
        if self._use_bpf: modes.append('bpftrace-enclave')
        if self._kvm: modes.append('kvm-vmexit-host')
        print(f'  [overhead] hosts: {labels}  extras: {modes or ["proc-only"]}')

        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self, output_csv):
        """Stop sampling, collect perf/bpf results, write single CSV."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=10)

        try:
            perf_result = self._perf.stop() if self._use_perf else {}
        except Exception as e:
            print(f'  [overhead] perf stop failed: {e}')
            perf_result = {}
        try:
            bpf_result = self._bpf.stop() if self._use_bpf else {}
        except Exception as e:
            print(f'  [overhead] bpf stop failed: {e}')
            bpf_result = {}

        # Merge perf + bpf time-series into rows by timestamp.
        self._merge_time_series(
            self._rows,
            perf_result,
            {'ipc': 'ipc', 'cache_miss_pct': 'cache_miss_pct'}
        )
        self._merge_time_series(
            self._rows,
            bpf_result,
            {'eenter_rate': 'eenter_rate', 'eexit_rate': 'eexit_rate',
             'aex_rate': 'aex_rate'}
        )

        _write_csv(self._rows, output_csv)

    def _merge_time_series(self, rows, result_by_nf, field_map):
        """
        For each NF, assign latest sample at-or-before row['ts'].
        Falls back to run-average if no samples were parsed.
        """
        if not rows:
            return
        for nf, data in result_by_nf.items():
            samples = sorted(data.get('samples', []), key=lambda s: s.get('ts', 0))
            if not samples:
                for out_field, avg_field in field_map.items():
                    avg = data.get(avg_field)
                    if avg is None:
                        continue
                    for row in rows:
                        row[f'{nf}_{out_field}'] = avg
                continue

            idx = 0
            for row in rows:
                rts = row.get('ts')
                if rts is None:
                    continue
                while idx + 1 < len(samples) and samples[idx + 1].get('ts', 0) <= rts:
                    idx += 1
                sample = samples[idx]
                if sample.get('ts', 0) > rts:
                    continue
                for out_field, sample_field in field_map.items():
                    val = sample.get(sample_field)
                    if val is not None:
                        row[f'{nf}_{out_field}'] = val

    def _loop(self, interval):
        prev = self._prev_snap
        while not self._stop_evt.wait(timeout=interval):
            curr = self.snapshot()
            row = self.delta(prev, curr)
            if row:
                first_snap = next(iter(curr.values()), {})
                row['ts'] = round(first_snap.get('ts', time.time()), 3)
                if self._phase is not None:
                    row['phase'] = self._phase
                self._rows.append(row)
            prev = curr


def _write_csv(rows, path):
    if not rows:
        print('  [overhead] no data collected')
        return
    all_keys = sorted({k for row in rows for k in row.keys()})
    meta_cols = [c for c in ('ts', 'phase') if c in all_keys]
    nf_cols = sorted(k for k in all_keys if any(
        k.startswith(nf + '_') for nf in NF_PATTERNS))
    sys_cols = sorted(k for k in all_keys if k not in nf_cols
                      and k not in ('ts', 'phase'))

    nf_path = path.replace('.csv', '_nf.csv')
    with open(nf_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=meta_cols + nf_cols, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)
    print(f'  [overhead] {len(rows)} rows -> {nf_path}')

    sys_path = path.replace('.csv', '_sys.csv')
    with open(sys_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=meta_cols + sys_cols, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)
    print(f'  [overhead] {len(rows)} rows -> {sys_path}')


def build_overhead_monitor(deployment, cfg, srv, use_perf=False, use_bpf=False):
    """Build an OverheadMonitor from deployments.ini + servers.ini."""
    dep = cfg[deployment]
    server_nfs = {}

    for nf in NF_PATTERNS:
        ip = dep.get(nf)
        if not ip:
            continue
        if ip.startswith('127.'):
            ip = srv['sgx_local'].get('nf_ip', '192.0.2.10')
        for s in srv.sections():
            if srv[s].get('host') == ip or srv[s].get('nf_ip') == ip:
                server_nfs.setdefault(s, []).append(nf)
                break

    is_sgx_dep = deployment in ('sgx', 'hybrid')
    targets = []
    for section, nfs in server_nfs.items():
        s = srv[section]
        targets.append({
            'host': s['host'],
            'user': s['user'],
            'key': s.get('key'),
            'host_label': section,
            'nfs': nfs,
            'ifaces': ([s['network_interface']] if s.get('network_interface') else []) + ['ogstun'],
            'is_sgx': (section == 'sgx_local') and is_sgx_dep,
        })

    kvm_host = None
    if srv.has_section('kvm_host'):
        k = srv['kvm_host']
        if k.get('host') and k.get('user'):
            kvm_host = {
                'host': k.get('host'),
                'user': k.get('user'),
                'key': k.get('key'),
            }

    return OverheadMonitor(
        targets, use_perf=use_perf, use_bpf=use_bpf, kvm_host=kvm_host)
