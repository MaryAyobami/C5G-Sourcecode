import json
import time
import threading
from queue import Queue

from .base import UERANSIMBase, _stream_to_queue


def _mbps(bps):
    return round(bps / 1e6, 2) if bps else None


def _parse_tcp(data, reverse=False):
    end  = data.get('end', {})
    sent = end.get('sum_sent', {})
    recv = end.get('sum_received', {})
    bps  = recv.get('bits_per_second') if reverse else sent.get('bits_per_second')
    return {
        'mbps':        _mbps(bps),
        'retransmits': sent.get('retransmits'),
    }


def _parse_udp(data):
    s = data.get('end', {}).get('sum', {})
    return {
        'mbps':      _mbps(s.get('bits_per_second')),
        'jitter_ms': round(s['jitter_ms'], 3) if s.get('jitter_ms') is not None else None,
        'loss_pct':  round(s['lost_percent'], 3) if s.get('lost_percent') is not None else None,
        'packets':   s.get('packets'),
    }


class UPPerformanceExperiment(UERANSIMBase):

    def __init__(self, host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                 iperf3_server, iperf3_port, http_url,
                 ssh_key=None, timeout=60, cooldown=2, collector=None):
        super().__init__(host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                         ssh_key, timeout, cooldown)
        self.iperf3_server = iperf3_server
        self.iperf3_port   = int(iperf3_port)
        self.http_url      = http_url
        self.collector     = collector

    # Measurement helpers
    def _ue_ip(self):
        out = self._ssh(
            "ip -4 addr show uesimtun0 | awk '/inet /{print $2}' | cut -d/ -f1"
        ).stdout.read().decode().strip()
        return out or None

    def _iperf3_tcp(self, ue_ip, duration, reverse=False):
        flags = '-R' if reverse else ''
        cmd   = (f'timeout {duration + 15} '
                 f'iperf3 -c {self.iperf3_server} -p {self.iperf3_port} '
                 f'-B {ue_ip} -t {duration} --json {flags} 2>&1')
        out = self._ssh(cmd).stdout.read().decode()
        try:
            return _parse_tcp(json.loads(out), reverse=reverse)
        except (json.JSONDecodeError, KeyError):
            print(f'  [iperf3-tcp] parse error: {out[:120]}')
            return {'mbps': None, 'retransmits': None}

    def _iperf3_udp(self, ue_ip, duration, bandwidth, reverse=False, length=None):
        flags = '-R' if reverse else ''
        lflag = f'-l {length}' if length else ''
        cmd   = (f'timeout {duration + 15} '
                 f'iperf3 -u -c {self.iperf3_server} -p {self.iperf3_port} '
                 f'-B {ue_ip} -t {duration} -b {bandwidth} {lflag} '
                 f'--json {flags} 2>&1')
        out = self._ssh(cmd).stdout.read().decode()
        try:
            return _parse_udp(json.loads(out))
        except (json.JSONDecodeError, KeyError):
            print(f'  [iperf3-udp] parse error: {out[:120]}')
            return {'mbps': None, 'jitter_ms': None, 'loss_pct': None, 'packets': None}

    def _http(self):
        cmd = (f'curl -o /dev/null -s '
               f'--connect-timeout 5 --max-time 30 --fail '
               f'-w "%{{speed_download}} %{{time_total}} %{{time_starttransfer}}" '
               f'--interface uesimtun0 {self.http_url} 2>&1')
        out = self._ssh(cmd).stdout.read().decode().strip().split()
        try:
            return {
                'http_dl_mbps': round(float(out[0]) * 8 / 1e6, 2),
                'http_time_s':  round(float(out[1]), 3),
                'http_ttfb_s':  round(float(out[2]), 3),
            }
        except (IndexError, ValueError):
            return {'http_dl_mbps': None, 'http_time_s': None, 'http_ttfb_s': None}

    def _http_upload(self):
        # PUT a 500MB file to the upload server on port 8081 of the same host
        base = self.http_url.rsplit('/', 1)[0].replace(':8080', ':8081')
        upload_url = f'{base}/upload'
        cmd = (f'curl -X PUT --upload-file /tmp/uptestfile -o /dev/null -s '
               f'--connect-timeout 5 --max-time 30 --fail '
               f'-w "%{{speed_upload}} %{{time_total}}" '
               f'--interface uesimtun0 {upload_url} 2>&1')
        out = self._ssh(cmd).stdout.read().decode().strip().split()
        try:
            return {
                'http_ul_mbps':   round(float(out[0]) * 8 / 1e6, 2),
                'http_ul_time_s': round(float(out[1]), 3),
            }
        except (IndexError, ValueError):
            return {'http_ul_mbps': None, 'http_ul_time_s': None}

    def run_ramp(self, rates_ul, rates_dl, iperf3_duration=20):
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(2)
        self._ssh(
            "ip link show | grep -o 'uesimtun[0-9]*' | xargs -r sudo ip link delete 2>/dev/null; true"
        ).wait()

        gnb_proc = self._start_gnb()
        ue_proc = None
        results = []
        try:
            ue_proc = self._ssh(f'sudo {self.ue_binary} -c {self.ue_config}')
            q = Queue()
            threading.Thread(target=_stream_to_queue, args=(ue_proc.stdout, q), daemon=True).start()
            if not self._wait_for(q, 'Connection setup for PDU session', time.time() + self.timeout):
                raise TimeoutError('UE attach timed out')
            ue_ip = self._ue_ip()
            print(f'  [ue] IP: {ue_ip}')

            for direction, rates in [('UL', rates_ul), ('DL', rates_dl)]:
                reverse = (direction == 'DL')
                for mbps in rates:
                    bw = f'{mbps}M'
                    print(f'\n  [{direction} @ {bw}]')
                    r = self._iperf3_udp(ue_ip, iperf3_duration, bw, reverse=reverse)
                    results.append({
                        'direction':     direction,
                        'target_mbps':   mbps,
                        'achieved_mbps': r['mbps'],
                        'jitter_ms':     r['jitter_ms'],
                        'loss_pct':      r['loss_pct'],
                    })
                    print(f'    achieved={r["mbps"]} Mbps  loss={r["loss_pct"]}%  jitter={r["jitter_ms"]}ms')
                    time.sleep(3)
        finally:
            self._ssh('sudo pkill -f nr-ue; sudo pkill -f nr-gnb; true').wait()
            if ue_proc:
                ue_proc.kill(); ue_proc.wait()
            gnb_proc.kill(); gnb_proc.wait()
        return results

    def run(self, iterations=5, iperf3_duration=10, udp_bandwidth='100M', http_only=False):
        self._ssh('sudo pkill -f nr-gnb; sudo pkill -f nr-ue; true').wait()
        time.sleep(2)
        # Remove any stale uesimtun interfaces left over from previous runs.
        self._ssh(
            "ip link show | grep -o 'uesimtun[0-9]*' | xargs -r sudo ip link delete 2>/dev/null; true"
        ).wait()

        # Ensure upload test file exists on client host (one-time per run)
        self._ssh(
            "test -f /tmp/uptestfile || sudo dd if=/dev/urandom of=/tmp/uptestfile bs=1M count=500 status=none"
        ).wait()

        gnb_proc = self._start_gnb()
        ue_proc  = None
        results  = []
        nf_attach = {}

        try:
            if self.collector:
                self.collector.start()

            ue_proc = self._ssh(f'sudo {self.ue_binary} -c {self.ue_config}')
            q       = Queue()
            threading.Thread(target=_stream_to_queue,
                             args=(ue_proc.stdout, q), daemon=True).start()

            print('  [ue] waiting for attach...')
            if not self._wait_for(q, 'Connection setup for PDU session',
                                  time.time() + self.timeout):
                raise TimeoutError('UE attach timed out')
            print('  [ue] attached')

            if self.collector:
                nf_lines  = self.collector.stop()
                nf_attach = self.collector.extract_upf_session(nf_lines)
                if hasattr(self.collector, 'extract_iface'):
                    nf_attach.update(self.collector.extract_iface(nf_lines, ''))

            ue_ip = self._ue_ip()
            print(f'  [ue] IP: {ue_ip}')

            for i in range(1, iterations + 1):
                print(f'\n  [iter {i}/{iterations}]')
                row = {'iteration': i}

                if not http_only:
                    # TCP single stream
                    ul = self._iperf3_tcp(ue_ip, iperf3_duration, reverse=False)
                    time.sleep(1)
                    dl = self._iperf3_tcp(ue_ip, iperf3_duration, reverse=True)
                    row['tcp_ul_mbps']        = ul['mbps']
                    row['tcp_dl_mbps']        = dl['mbps']
                    row['tcp_ul_retransmits'] = ul['retransmits']
                    row['tcp_dl_retransmits'] = dl['retransmits']
                    print(f'    TCP  UL={ul["mbps"]}Mbps  DL={dl["mbps"]}Mbps')

                    # UDP at configured UDP_BW both directions. Server tuning (8MB socket
                    # window + kernel buffers) required for reverse-mode high-rate stability.
                    time.sleep(1)
                    uul = self._iperf3_udp(ue_ip, iperf3_duration, udp_bandwidth, reverse=False)
                    time.sleep(1)
                    udl = self._iperf3_udp(ue_ip, iperf3_duration, udp_bandwidth, reverse=True)
                    row['udp_ul_mbps']      = uul['mbps']
                    row['udp_ul_jitter_ms'] = uul['jitter_ms']
                    row['udp_ul_loss_pct']  = uul['loss_pct']
                    row['udp_dl_mbps']      = udl['mbps']
                    row['udp_dl_jitter_ms'] = udl['jitter_ms']
                    row['udp_dl_loss_pct']  = udl['loss_pct']
                    print(f'    UDP  UL={uul["mbps"]}Mbps jitter={uul["jitter_ms"]}ms loss={uul["loss_pct"]}%  '
                          f'DL={udl["mbps"]}Mbps jitter={udl["jitter_ms"]}ms loss={udl["loss_pct"]}%')

                    # Small-packet UDP DL - stresses per-packet path (TEE OCALL cost).
                    # Capped at 10M (~9.7k pps at 128B): higher rates crash iperf3 server
                    # with 'select: Bad file descriptor' and poison the next iteration.
                    time.sleep(2)
                    usm = self._iperf3_udp(ue_ip, iperf3_duration, '10M',
                                           reverse=True, length=128)
                    pps = round((usm['packets'] or 0) / iperf3_duration) if usm['packets'] else None
                    row['udp_small_dl_mbps']      = usm['mbps']
                    row['udp_small_dl_jitter_ms'] = usm['jitter_ms']
                    row['udp_small_dl_loss_pct']  = usm['loss_pct']
                    row['udp_small_dl_pps']       = pps
                    print(f'    UDP-128B DL={usm["mbps"]}Mbps pps={pps} loss={usm["loss_pct"]}%')
                    time.sleep(3)  # let iperf3 server drain before HTTP

                # HTTP download speed via curl
                http = self._http()
                row['http_dl_mbps'] = http['http_dl_mbps']
                row['http_time_s']  = http['http_time_s']
                row['http_ttfb_s']  = http['http_ttfb_s']
                print(f'    HTTP DL={http["http_dl_mbps"]}Mbps  '
                      f't={http["http_time_s"]}s  ttfb={http["http_ttfb_s"]}s')

                # HTTP upload speed via curl PUT
                time.sleep(1)
                http_ul = self._http_upload()
                row['http_ul_mbps']   = http_ul['http_ul_mbps']
                row['http_ul_time_s'] = http_ul['http_ul_time_s']
                print(f'    HTTP UL={http_ul["http_ul_mbps"]}Mbps  '
                      f't={http_ul["http_ul_time_s"]}s')

                if nf_attach:
                    row['_nf'] = nf_attach
                results.append(row)
                time.sleep(self.cooldown)

        finally:
            self._ssh('sudo pkill -f nr-ue; sudo pkill -f nr-gnb; true').wait()
            if ue_proc:
                ue_proc.kill()
                ue_proc.wait()
            gnb_proc.kill()
            gnb_proc.wait()

        return results
