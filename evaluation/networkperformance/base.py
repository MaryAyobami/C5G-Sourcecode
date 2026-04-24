import time
import threading
import subprocess
from queue import Queue, Empty


def _stream_to_queue(stream, queue):
    for line in iter(stream.readline, b''):
        queue.put(line.decode('utf-8', errors='replace').rstrip())


class UERANSIMBase:
    """Shared SSH infrastructure for all UERANSIM experiments."""

    def __init__(self, host, user, gnb_binary, gnb_config, ue_binary, ue_config,
                 ssh_key=None, timeout=30, cooldown=5):
        self.host       = host
        self.user       = user
        self.gnb_binary = gnb_binary
        self.gnb_config = gnb_config
        self.ue_binary  = ue_binary
        self.ue_config  = ue_config
        self.ssh_key    = ssh_key
        self.timeout    = timeout
        self.cooldown   = cooldown

    def _ssh(self, remote_cmd):
        cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes',
               '-o', 'IdentitiesOnly=yes']
        if self.ssh_key:
            cmd += ['-i', self.ssh_key]
        cmd += [f'{self.user}@{self.host}', remote_cmd]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _wait_for(self, queue, marker, deadline, lines_buf=None):
        while time.time() < deadline:
            try:
                line = queue.get(timeout=0.3)
                print(f'    {line}')
                if lines_buf is not None:
                    lines_buf.append(line)
                if marker in line:
                    return True
            except Empty:
                pass
        return False

    def _start_gnb(self):
        proc = self._ssh(f'sudo {self.gnb_binary} -c {self.gnb_config}')
        q = Queue()
        threading.Thread(target=_stream_to_queue, args=(proc.stdout, q), daemon=True).start()
        print('  [gnb] waiting for NG Setup...')
        if not self._wait_for(q, 'NG Setup procedure is successful', time.time() + self.timeout):
            proc.kill()
            proc.wait()
            raise TimeoutError('gNB NG Setup timed out')
        return proc
