import re
import time
import threading
from queue import Queue, Empty

from .base import UERANSIMBase, _stream_to_queue
from .cp_registration import _parse_ts


class CPServiceRequestExperiment(UERANSIMBase):
    def __init__(self, host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                 nr_cli, ssh_key=None, timeout=30, cooldown=5):
        super().__init__(host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                         ssh_key, timeout, cooldown)
        self.nr_cli = nr_cli  

    # nr-cli helpers
    def _cli_output(self, node, command):
        """Run: nr-cli <node> -e '<command>' and return stdout as a string."""
        proc = self._ssh(f'sudo {self.nr_cli} {node} -e "{command}"')
        out  = proc.stdout.read().decode('utf-8', errors='replace')
        proc.wait()
        return out.strip()

    def _discover_nodes(self):
        """Return (gnb_name, ue_name) by parsing 'nr-cli --dump' output."""
        proc = self._ssh(f'sudo {self.nr_cli} --dump')
        out  = proc.stdout.read().decode('utf-8', errors='replace')
        proc.wait()
        gnb_name, ue_name = None, None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('UERANSIM-gnb-'):
                gnb_name = line
            elif line.startswith('imsi-'):
                ue_name = line
        return gnb_name, ue_name

    def _get_ue_id(self, gnb_name):
        """Return UE's numeric ID on gNB from 'ue-list' YAML output."""
        out = self._cli_output(gnb_name, 'ue-list')
        m   = re.search(r'ue-id:\s*(\d+)', out)
        return int(m.group(1)) if m else None

    # Queue/log helpers
    def _watch(self, q, buf, marker, deadline):
        """Read lines from q into buf until marker found or deadline expires.

        Returns the log timestamp of the matching line, or None on timeout.
        """
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                print(f'    {line}')
                buf.append(line)
                if marker in line:
                    return _parse_ts(line)
            except Empty:
                pass
        return None

    # Per-iteration measurement
    def _run_iteration(self, n, q, buf, gnb_name, ue_name):
        """Run one Service Request cycle. Returns a metrics dict."""

        # Re-query UE ID each iteration - context is re-created after each ue-release
        ue_id = self._get_ue_id(gnb_name)
        if ue_id is None:
            print(f'  [iter {n}] UE not found in gNB ue-list - skipping')
            return {'iteration': n, 'success': False,
                    'service_request_ms': None, 'cm_idle_to_connected_ms': None}

        # force CM-IDLE via gNB context release
        print(f'  [iter {n}] ue-release {ue_id} -> CM-IDLE')
        self._ssh(f'sudo {self.nr_cli} {gnb_name} -e "ue-release {ue_id}"').wait()

        ts_cm_idle = self._watch(q, buf, 'UE switches to state [CM-IDLE]',
                                 time.time() + self.timeout)
        if ts_cm_idle is None:
            print(f'  [iter {n}] timeout waiting for CM-IDLE')
            return {'iteration': n, 'success': False,
                    'service_request_ms': None, 'cm_idle_to_connected_ms': None}

        # trigger Service Request via uplink data on uesimtun0
        # ping immediately sets IDLE-UPLINK-DATA-PENDING
        print(f'  [iter {n}] ping uesimtun0 -> Service Request')
        self._ssh('ping -c 1 -W 2 -I uesimtun0 8.8.8.8 > /dev/null 2>&1; true').wait()

        # watch for SR-INITIATED then Service Accept
        ts_sr_init = None
        ts_sr_done = None
        deadline   = time.time() + self.timeout

        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                print(f'    {line}')
                buf.append(line)
                if ts_sr_init is None and 'MM-SERVICE-REQUEST-INITIATED' in line:
                    ts_sr_init = _parse_ts(line)
                if 'Service Accept received' in line:
                    ts_sr_done = _parse_ts(line)
                    break
            except Empty:
                pass

        if ts_sr_done is None:
            print(f'  [iter {n}] timeout waiting for Service Accept')
            return {'iteration': n, 'success': False,
                    'service_request_ms': None, 'cm_idle_to_connected_ms': None}

        # Release PSI[1]; UERANSIM auto-re-establishes it from sessions config
        self._ssh(f'sudo {self.nr_cli} {ue_name} -e "ps-release-all"').wait()
        time.sleep(self.cooldown)

        def ms(a, b):
            return round((b - a).total_seconds() * 1000, 3) if a and b else None

        return {
            'iteration':               n,
            'success':                 True,
            'service_request_ms':      ms(ts_sr_init, ts_sr_done),
            'cm_idle_to_connected_ms': ms(ts_cm_idle, ts_sr_done),
        }

    # Entry point
    def run(self, iterations=10, warmstart=True):
        """Start gNB + UE, run N service request iterations, return results list."""
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(1)

        gnb_proc = self._start_gnb()
        ue_proc  = None
        results  = []

        try:
            # Start UE - it stays alive for all iterations
            ue_proc = self._ssh(f'sudo {self.ue_binary} -c {self.ue_config}')
            q   = Queue()
            buf = []
            threading.Thread(target=_stream_to_queue,
                             args=(ue_proc.stdout, q), daemon=True).start()

            # Wait for initial attach (registration + PDU session)
            print('  [ue] waiting for initial attach...')
            if self._watch(q, buf, 'Connection setup for PDU session',
                           time.time() + self.timeout) is None:
                raise TimeoutError('UE initial attach timed out')
            print('  [ue] initial attach complete')

            # Discover gNB and UE node names for nr-cli
            gnb_name, ue_name = self._discover_nodes()
            if not gnb_name or not ue_name:
                raise RuntimeError(
                    f'Node discovery failed - gnb={gnb_name}, ue={ue_name}')
            print(f'  [nodes] gnb={gnb_name}  ue={ue_name}')

            # Optional warmstart (not recorded)
            if warmstart:
                print('\n  [warmstart] running one warmstart service request...')
                self._run_iteration(0, q, buf, gnb_name, ue_name)

            # Measured iterations
            for i in range(1, iterations + 1):
                print(f'\n  [iter {i}/{iterations}]')
                try:
                    r = self._run_iteration(i, q, buf, gnb_name, ue_name)
                    results.append(r)
                    print(f'  service_request={r["service_request_ms"]}ms  '
                          f'cm_idle_to_connected={r["cm_idle_to_connected_ms"]}ms')
                except Exception as e:
                    print(f'  [error] {e}')
                    results.append({'iteration': i, 'success': False,
                                    'service_request_ms': None,
                                    'cm_idle_to_connected_ms': None})

        finally:
            self._ssh('sudo pkill -f nr-ue; sudo pkill -f nr-gnb; true').wait()
            if ue_proc:
                ue_proc.kill()
                ue_proc.wait()
            gnb_proc.kill()
            gnb_proc.wait()

        return results
