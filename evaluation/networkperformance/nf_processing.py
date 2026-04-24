import re
import subprocess
import threading
import time
from queue import Queue, Empty

NF_LOGS = {
    'amf': 'amf.log',
    'ausf': 'ausf.log',
    'udm': 'udm.log',
    'udr': 'udr.log',
    'smf': 'smf.log',
    'upf': 'upf.log',
    'pcf': 'pcf.log',
    'nrf': 'nrf.log',
    'scp': 'scp.log',
}

# Example line: 03/11 03:32:08.290: [gmm] INFO: [NF_TIMING] AMF_REGISTRATION imsi=imsi-999700000000001 duration_ms=488.391
_LINE_RE = re.compile(r'\[NF_TIMING\]\s+(\S+)\s+(.*)')
_KV_RE = re.compile(r'(\w+)=([\w.\-:]+)')
_NF_TS_RE = re.compile(r'^(\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})')


def _parse_log_ts(line):
    from datetime import datetime, date
    m = _NF_TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(f'{date.today().year}/{m.group(1)}', '%Y/%m/%d %H:%M:%S.%f')


def _ts_diff_ms(ts_a, ts_b):
    if ts_a is None or ts_b is None:
        return None
    return round((ts_b - ts_a).total_seconds() * 1000, 3)


def parse_nf_timing_line(line):
    """Parse one [NF_TIMING] log line. Returns dict with 'event' + all key=value fields, or None."""
    m = _LINE_RE.search(line)
    if not m:
        return None
    event = m.group(1)
    fields = {'event': event}
    for key, val in _KV_RE.findall(m.group(2)):
        try:
            fields[key] = float(val)
        except ValueError:
            fields[key] = val
    return fields


class NFTimingCollector:
    """Tail NF logs over SSH and collect [NF_TIMING] lines."""

    def __init__(self, targets):
        self.targets = targets
        self._procs = []
        self._queue = Queue()
        self._threads = []

    _LOCAL_IPS = {'127.0.0.1', '192.0.2.10'}

    def _ssh_tail(self, host, user, key, log_dir, nfs):
        """Start 'tail -F' for the given NF logs - local subprocess or SSH."""
        log_files = [f'{log_dir}/{NF_LOGS[nf]}' for nf in nfs if nf in NF_LOGS]
        if not log_files:
            return None
        if host in self._LOCAL_IPS:
            cmd = ['sudo', 'tail', '-F', '-n', '0'] + log_files
        else:
            ssh = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'IdentitiesOnly=yes']
            if key:
                ssh += ['-i', key]
            cmd = ssh + [f'{user}@{host}', f'sudo tail -F -n 0 {" ".join(log_files)}']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t = threading.Thread(target=self._read, args=(proc.stdout,), daemon=True)
        t.start()
        return proc, t

    def _read(self, stream):
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', errors='replace').rstrip()
            if '[NF_TIMING]' in line:
                self._queue.put(line)

    def start(self):
        self._procs = []
        self._threads = []
        for t in self.targets:
            result = self._ssh_tail(t['host'], t['user'], t.get('key'),
                                    t['log_dir'], t['nfs'])
            if result:
                proc, thread = result
                self._procs.append(proc)
                self._threads.append(thread)

    def stop(self):
        """Kill tail processes and return all collected [NF_TIMING] lines."""
        for proc in self._procs:
            proc.kill()
            proc.wait()
        # drain remaining lines
        time.sleep(0.2)
        lines = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except Empty:
                break
        self._procs = []
        self._threads = []
        return lines

    def extract_registration(self, lines, imsi):
        """Per-NF processing and waiting times for one UE registration."""
        all_ev = [e for e in (parse_nf_timing_line(l) for l in lines) if e]
        imsi_ev = [e for e in all_ev
                   if str(e.get('imsi', '')) == imsi
                   or str(e.get('imsi', '')).startswith('suci-')]

        def get(evname, field):
            for e in imsi_ev:
                if e['event'] == evname and field in e:
                    return e[field]
            return None

        def sub(a, b):
            if a is None or b is None:
                return None
            return max(0.0, round(a - b, 3))

        def avg(evname):
            vals = [e['duration_ms'] for e in all_ev
                    if e['event'] == evname and 'duration_ms' in e]
            return round(sum(vals) / len(vals), 3) if vals else None

        upf = next((e for e in all_ev if e['event'] == 'UPF_N4_SESSION_PROCESSING'), None)

        return {
            'amf_processing_ms': get('AMF_REG_PROCESSING', 'processing_ms'),
            'amf_waiting_ms': get('AMF_REG_PROCESSING', 'wait_ms'),
            'ausf_processing_ms': get('AUSF_AUTH_PROCESSING', 'processing_ms'),
            'ausf_waiting_ms': sub(get('AUSF_AUTHENTICATION', 'duration_ms'),
                                   get('AUSF_AUTH_PROCESSING', 'processing_ms')),
            'udm_processing_ms': get('UDM_UEAU_PROCESSING', 'processing_ms'),
            'udm_waiting_ms': sub(get('UDM_UEAU', 'duration_ms'),
                                  get('UDM_UEAU_PROCESSING', 'processing_ms')),
            'udr_processing_ms': get('UDR_DB_PROCESSING', 'processing_ms'),
            'smf_processing_ms': get('SMF_PDU_SESSION_PROCESSING', 'processing_ms'),
            'pcf_processing_ms': get('PCF_SM_POLICY_PROCESSING', 'processing_ms'),
            'pcf_waiting_ms': sub(get('PCF_SM_POLICY_CREATE', 'duration_ms'),
                                  get('PCF_SM_POLICY_PROCESSING', 'processing_ms')),
            'upf_processing_ms': upf.get('processing_ms') if upf else None,
            'scp_forward_ms': avg('SCP_MSG_FORWARD'),
        }

    def extract_upf_session(self, lines):
        """UPF N4 session processing time (used by up_performance)."""
        events = [parse_nf_timing_line(l) for l in lines]
        proc = [e for e in events if e and e['event'] == 'UPF_N4_SESSION_PROCESSING']
        return {'upf_processing_ms': proc[-1].get('processing_ms') if proc else None}

    def extract_nrf_scp(self, lines):
        """avg/min/max over NRF_NF_DISCOVER and SCP forwarding events."""
        events = [parse_nf_timing_line(l) for l in lines]

        def stats(evname):
            vals = [e['duration_ms'] for e in events
                    if e and e['event'] == evname and 'duration_ms' in e]
            if not vals:
                return {'count': 0, 'avg_ms': None, 'min_ms': None, 'max_ms': None}
            return {
                'count': len(vals),
                'avg_ms': round(sum(vals) / len(vals), 3),
                'min_ms': round(min(vals), 3),
                'max_ms': round(max(vals), 3),
            }

        return {
            'nrf_register': stats('NRF_NF_REGISTER'),
            'nrf_discover': stats('NRF_NF_DISCOVER'),
            'scp_forward': stats('SCP_MSG_FORWARD'),
            'scp_upstream': stats('SCP_NRF_UPSTREAM'),
            'scp_downstream': stats('SCP_DOWNSTREAM'),
        }


def build_collector(deployment, cfg, srv):
    """Build an NFTimingCollector from deployments.ini + servers.ini."""
    dep = cfg[deployment]

    nf_host_map = {
        'amf': dep.get('amf'),
        'ausf': dep.get('ausf'),
        'udm': dep.get('udm'),
        'udr': dep.get('udr'),
        'smf': dep.get('smf'),
        'upf': dep.get('upf'),
        'pcf': dep.get('pcf'),
        'nrf': dep.get('nrf'),
        'scp': dep.get('scp'),
    }

    # Group NFs by host IP
    ip_to_nfs = {}
    for nf, ip in nf_host_map.items():
        if not ip:
            continue
        # loopback means sgx_local
        if ip.startswith('127.'):
            ip = '192.0.2.10'
        ip_to_nfs.setdefault(ip, []).append(nf)

    # Find server config for each IP
    def find_server(ip):
        for section in srv.sections():
            s = srv[section]
            if s.get('host') == ip or s.get('nf_ip') == ip:
                return s
        return None

    _LOCAL_IPS = {'127.0.0.1', '192.0.2.10'}

    targets = []
    for ip, nfs in ip_to_nfs.items():
        s = find_server(ip)
        if not s:
            continue
        # sgx_local: Gramine remaps the configured log path to a different host path.
        if ip in _LOCAL_IPS:
            log_dir = dep.get('log_dir') or s.get('log_dir', '/var/local/log/open5gs')
        else:
            log_dir = s.get('log_dir') or dep.get('log_dir', '/var/local/log/open5gs')
        targets.append({
            'host': ip,
            'user': s['user'],
            'key': s.get('key'),
            'log_dir': log_dir,
            'nfs': nfs,
        })

    return NFTimingCollector(targets)
