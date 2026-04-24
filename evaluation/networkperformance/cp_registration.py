import re
import time
import threading
from datetime import datetime
from queue import Queue, Empty

from .base import UERANSIMBase, _stream_to_queue  

TIMESTAMP_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]')

MARKERS = {
    'ue_start':  None,
    'reg_start': 'Sending Initial Registration',
    'reg_end':   'Initial Registration is successful',
    'pdu_start': 'Sending PDU Session Establishment Request',
    'pdu_end':   'PDU Session establishment is successful',
    'tun_up':    'Connection setup for PDU session',
}


def _parse_ts(line):
    m = TIMESTAMP_RE.search(line)
    return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S.%f') if m else None


def parse_ue_timestamps(lines):
    """Return absolute start and TUN-up timestamps for signaling rate calculation.

    These come directly from UERANSIM log lines so they are free from
    Python/SSH wall-clock overhead and timeout inflation.
    """
    ue_start_ts = None
    tun_up_ts   = None
    for line in lines:
        ts = _parse_ts(line)
        if ts is None:
            continue
        if ue_start_ts is None:
            ue_start_ts = ts
        if MARKERS['tun_up'] in line:
            tun_up_ts = ts
    return {'ue_start_ts': ue_start_ts, 'tun_up_ts': tun_up_ts}


def parse_ue_log(lines):
    found = {}
    for line in lines:
        ts = _parse_ts(line)
        if ts is None:
            continue
        if 'ue_start' not in found:
            found['ue_start'] = ts
        for key, text in MARKERS.items():
            if key not in found and text and text in line:
                found[key] = ts

    def ms(a, b):
        if a in found and b in found:
            return round((found[b] - found[a]).total_seconds() * 1000, 3)
        return None

    return {
        'reg_time_ms':     ms('reg_start', 'reg_end'),
        'pdu_time_ms':     ms('pdu_start', 'pdu_end'),
        'total_attach_ms': ms('ue_start', 'tun_up'),
    }


class CPRegistrationExperiment(UERANSIMBase):

    def __init__(self, *args, collector=None, imsi=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.collector = collector
        self.imsi      = imsi   # e.g. 'imsi-999700000000001'

    def _run_ue(self, n):
        if self.collector:
            self.collector.start()
            time.sleep(0.5)  # wait for SSH tail to be established before UE fires first NAS message

        proc = self._ssh(f'sudo {self.ue_binary} -c {self.ue_config}')
        q = Queue()
        threading.Thread(target=_stream_to_queue, args=(proc.stdout, q), daemon=True).start()

        ue_lines = []
        success = False
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                print(f'    {line}')
                ue_lines.append(line)
                if 'Connection setup for PDU session' in line:
                    success = True
                    break
            except Empty:
                pass

        nf_lines = self.collector.stop() if self.collector else []

        if not success:
            print(f'  [warn] iteration {n}: UE did not attach within {self.timeout}s')

        self._ssh('sudo pkill -f nr-ue; true').wait()
        proc.kill()
        proc.wait()
        time.sleep(self.cooldown)

        metrics = parse_ue_log(ue_lines)
        metrics['iteration'] = n
        metrics['success']   = success

        if self.collector and self.imsi:
            nf = self.collector.extract_registration(nf_lines, self.imsi)
            metrics['_nf'] = nf
            if hasattr(self.collector, 'extract_iface'):
                metrics['_iface'] = self.collector.extract_iface(nf_lines, self.imsi)

        return metrics

    def run(self, iterations=10, warmstart=True):
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(1)

        gnb_proc = self._start_gnb()
        results  = []

        try:
            if warmstart:
                print(f'\n  [warmstart] running one warmstart attach...')
                self._run_ue(0)

            for i in range(1, iterations + 1):
                print(f'\n  [ue]  iteration {i}/{iterations}...')
                try:
                    result = self._run_ue(i)
                    results.append(result)
                    print(f'  reg={result["reg_time_ms"]}ms  '
                          f'pdu={result["pdu_time_ms"]}ms  '
                          f'total={result["total_attach_ms"]}ms')
                except Exception as e:
                    print(f'  [error] {e}')
                    results.append({
                        'iteration': i, 'success': False,
                        'reg_time_ms': None, 'pdu_time_ms': None, 'total_attach_ms': None,
                    })
        finally:
            self._ssh('sudo pkill -f nr-gnb; true').wait()
            gnb_proc.kill()
            gnb_proc.wait()

        return results
