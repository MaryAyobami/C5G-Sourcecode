import re
import subprocess
from datetime import datetime, date

from .nf_processing import parse_nf_timing_line, build_collector as _build_nf_collector

# Timestamp at start of Open5GS log lines: MM/DD HH:MM:SS.mmm
_NF_TS_RE = re.compile(r'^(\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})')
_RTT_RE = re.compile(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms')


def _parse_log_ts(line):
    """Parse MM/DD HH:MM:SS.mmm from the start of an Open5GS log line."""
    m = _NF_TS_RE.match(line)
    if not m:
        return None
    year = date.today().year
    return datetime.strptime(f'{year}/{m.group(1)}', '%Y/%m/%d %H:%M:%S.%f')


def _ts_diff_ms(ts_a, ts_b):
    if ts_a is None or ts_b is None:
        return None
    return round((ts_b - ts_a).total_seconds() * 1000, 3)


def _run_ping(target, count, ssh_host=None, ssh_user=None, ssh_key=None, ssh_password=None):
    """
    Run ping to target, optionally via SSH.
    Returns avg RTT in ms, or None on failure.
    """
    _LOCAL = {None, '127.0.0.1', '192.0.2.10'}
    ping_cmd = f'ping -c {count} -i 0.2 -W 2 {target} 2>&1'
    try:
        if ssh_host in _LOCAL:
            proc = subprocess.run(
                ['ping', '-c', str(count), '-i', '0.2', '-W', '2', target],
                capture_output=True, text=True, timeout=count * 2 + 10,
            )
            out = proc.stdout
        else:
            if ssh_password:
                cmd = ['sshpass', '-p', ssh_password,
                       'ssh', '-o', 'StrictHostKeyChecking=no',
                       f'{ssh_user}@{ssh_host}', ping_cmd]
            else:
                base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes']
                if ssh_key:
                    base += ['-i', ssh_key]
                cmd = base + [f'{ssh_user}@{ssh_host}', ping_cmd]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=count * 2 + 15)
            out = proc.stdout
        m = _RTT_RE.search(out)
        return float(m.group(1)) if m else None
    except Exception as e:
        print(f'  [ping] error: {e}')
        return None


class InterfaceLatencyCollector:
    """Per-interface latencies (N2/N3/N4/SBI). Duck-types NFTimingCollector."""

    def __init__(self, nf_collector, gnb_ip,
                 amf_host, amf_user, amf_key, amf_password,
                 upf_host, upf_user, upf_key, upf_password,
                 ping_count=20):
        self._col = nf_collector
        self.gnb_ip = gnb_ip
        self.amf_host = amf_host
        self.amf_user = amf_user
        self.amf_key = amf_key
        self.amf_pass = amf_password
        self.upf_host = upf_host
        self.upf_user = upf_user
        self.upf_key = upf_key
        self.upf_pass = upf_password
        self.ping_count = ping_count

    def start(self):
        self._col.start()

    def stop(self):
        return self._col.stop()

    def extract_registration(self, lines, imsi):
        return self._col.extract_registration(lines, imsi)

    def extract_upf_session(self, lines):
        return self._col.extract_upf_session(lines)

    def ping_n2_path(self):
        """ICMP ping from AMF host to gNB host - N2 path latency proxy."""
        print(f'  [iface] pinging N2 path: {self.amf_host} -> {self.gnb_ip}')
        return _run_ping(self.gnb_ip, self.ping_count,
                         ssh_host=self.amf_host,
                         ssh_user=self.amf_user,
                         ssh_key=self.amf_key,
                         ssh_password=self.amf_pass)

    def ping_n3_path(self):
        """ICMP ping from UPF host to gNB host - N3 path latency proxy."""
        print(f'  [iface] pinging N3 path: {self.upf_host} -> {self.gnb_ip}')
        return _run_ping(self.gnb_ip, self.ping_count,
                         ssh_host=self.upf_host,
                         ssh_user=self.upf_user,
                         ssh_key=self.upf_key,
                         ssh_password=self.upf_pass)

    def extract_iface(self, lines, imsi):
        """Extract interface latency metrics from NF_TIMING log lines.

        Pass '' for imsi to skip IMSI filtering. N2 RTTs use CLOCK_MONOTONIC
        mono_us when available, falling back to log ts (1ms) for older binaries.
        """
        ev_ts = {}
        ev_mono = {}
        ev_ms = {}

        for line in lines:
            p = parse_nf_timing_line(line)
            if not p:
                continue
            ev = p['event']
            line_imsi = str(p.get('imsi', ''))
            # Auth events (AMF_AUTH_*) fire before IMSI resolution and carry
            # a SUCI value - allow suci-* through so N2 auth RTT is captured.
            if imsi and line_imsi and line_imsi != imsi and not line_imsi.startswith('suci-'):
                continue
            if ev not in ev_ts:
                ev_ts[ev] = _parse_log_ts(line)
            if ev not in ev_mono and 'mono_us' in p:
                try:
                    ev_mono[ev] = int(p['mono_us'])
                except (ValueError, TypeError):
                    pass
            if ev not in ev_ms and 'duration_ms' in p:
                ev_ms[ev] = p['duration_ms']

        def mono_diff_ms(ev_a, ev_b):
            a = ev_mono.get(ev_a)
            b = ev_mono.get(ev_b)
            if a is None or b is None:
                return None
            return round((b - a) / 1000.0, 3)

        def ts_rtt(start_ev, end_ev):
            return _ts_diff_ms(ev_ts.get(start_ev), ev_ts.get(end_ev))

        n2_auth_rtt_ms = mono_diff_ms('AMF_AUTH_REQ_SENT', 'AMF_AUTH_RESP_RECV')
        n2_secmode_rtt_ms = mono_diff_ms('AMF_SEC_MODE_CMD_SENT', 'AMF_SEC_MODE_COMPLETE')
        if n2_auth_rtt_ms is None:
            n2_auth_rtt_ms = ts_rtt('AMF_AUTH_REQ_SENT', 'AMF_AUTH_RESP_RECV')
        if n2_secmode_rtt_ms is None:
            n2_secmode_rtt_ms = ts_rtt('AMF_SEC_MODE_CMD_SENT', 'AMF_SEC_MODE_COMPLETE')

        return {
            'n2_auth_ms': n2_auth_rtt_ms,
            'n2_security_ms': n2_secmode_rtt_ms,
            'n4_session_ms': ev_ms.get('UPF_N4_SESSION_ESTAB'),
            'sbi_ausf_ms': ev_ms.get('AUSF_AUTHENTICATION'),
            'sbi_udm_ms': ev_ms.get('UDM_UEAU'),
            'sbi_smf_ms': ev_ms.get('SMF_PDU_SESSION_CREATE'),
            'sbi_pcf_ms': ev_ms.get('PCF_SM_POLICY_CREATE'),
        }


def build_iface_collector(deployment, cfg, srv, ping_count=20):
    """Build an InterfaceLatencyCollector for the given deployment."""
    dep = cfg[deployment]
    gnb_ip = srv['ueransim']['host']   # 192.0.2.20

    def resolve(raw):
        return '192.0.2.10' if raw.startswith('127.') else raw

    def find_server(ip):
        for sec in srv.sections():
            s = srv[sec]
            if s.get('host') == ip or s.get('nf_ip') == ip:
                return s
        return {}

    amf_ip = resolve(dep.get('amf', '192.0.2.10'))
    upf_ip = resolve(dep.get('upf', '192.0.2.10'))
    amf_srv = find_server(amf_ip)
    upf_srv = find_server(upf_ip)

    nf_col = _build_nf_collector(deployment, cfg, srv)

    return InterfaceLatencyCollector(
        nf_collector=nf_col,
        gnb_ip=gnb_ip,
        amf_host=amf_ip,
        amf_user=amf_srv.get('user', 'operator'),
        amf_key=amf_srv.get('key'),
        amf_password=amf_srv.get('password'),
        upf_host=upf_ip,
        upf_user=upf_srv.get('user', 'operator'),
        upf_key=upf_srv.get('key'),
        upf_password=upf_srv.get('password'),
        ping_count=ping_count,
    )
