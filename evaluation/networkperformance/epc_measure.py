"""epc_measure.py - Measure actual EPC allocation per SGX NF.

Uses kprobe on sgx_encl_add_page to count pages committed at enclave init.
In SGX1, all pages are pre-allocated at EINIT, so this equals the true
physical memory footprint (in EPC or encrypted DRAM).

Usage:
    python3 -m networkperformance.epc_measure [--nfs nrf scp smf ...]
    python3 -m networkperformance.epc_measure --nfs nrf smf udm

Output: results/epc_allocation_sgx.csv  (nf, pages, epc_mb)
"""

import csv
import os
import re
import signal
import subprocess
import time

MANIFESTS_DIR = '/opt/open5gs-gramine/manifests'
NFS = ['nrf', 'scp', 'udr', 'udm', 'ausf', 'pcf', 'smf', 'amf', 'upf']
INIT_WAIT = 30  # seconds to wait for enclave EINIT to complete


def _loader_pid(nf):
    r = subprocess.run(['pgrep', '-f', f'open5gs-{nf}'],
                       capture_output=True, text=True)
    candidates = []
    for pid in r.stdout.strip().split():
        try:
            if 'loader' in open(f'/proc/{pid}/cmdline').read():
                candidates.append(int(pid))
        except Exception:
            pass
    # Return highest PID - the most recently started loader
    return str(max(candidates)) if candidates else None


def _start_nf(nf):
    return subprocess.Popen(
        ['bash', '-c', f'cd {MANIFESTS_DIR} && gramine-sgx open5gs-{nf}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def measure_nf(nf):
    # Start bpftrace BEFORE the NF so probe is attached during enclave init
    bpf_script = ('kprobe:sgx_encl_add_page { @pages[pid]++; }'
                  ' interval:s:3 { print(@pages); }')
    bpf = subprocess.Popen(
        ['bpftrace', '-e', bpf_script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # wait for probe attach

    # Kill any existing instances, then start fresh
    subprocess.run(['pkill', '-9', '-f', f'open5gs-{nf}'], capture_output=True)
    time.sleep(2)

    print(f'  [{nf}] starting enclave (waiting {INIT_WAIT}s)...', flush=True)
    nf_proc = _start_nf(nf)
    time.sleep(INIT_WAIT)

    loader = _loader_pid(nf)
    # SIGINT triggers graceful bpftrace shutdown which flushes buffered output
    bpf.send_signal(signal.SIGINT)
    try:
        out, _ = bpf.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        bpf.kill()
        out, _ = bpf.communicate()

    counts = {}
    for line in out.decode('utf-8', errors='replace').splitlines():
        m = re.match(r'@pages\[(\d+)\]:\s*(\d+)', line)
        if m:
            counts[m.group(1)] = int(m.group(2))

    pages = counts.get(loader, 0) if loader else 0
    epc_mb = round(pages * 4 / 1024, 1)
    print(f'  [{nf}] loader={loader}  pages={pages}  epc={epc_mb} MB', flush=True)

    nf_proc.kill()
    nf_proc.wait()
    subprocess.run(['pkill', '-9', '-f', f'open5gs-{nf}'], capture_output=True)
    time.sleep(2)

    return {'nf': nf, 'pages': pages, 'epc_mb': epc_mb}


def run(nfs=None, output_csv='results/epc_allocation_sgx.csv'):
    nfs = nfs or NFS
    os.makedirs('results', exist_ok=True)

    print(f'[epc_measure] {len(nfs)} NFs: {nfs}', flush=True)
    print('[epc_measure] each NF will be briefly stopped and restarted\n', flush=True)

    rows = []
    for nf in nfs:
        try:
            rows.append(measure_nf(nf))
        except Exception as e:
            print(f'  [{nf}] ERROR: {e}')
            rows.append({'nf': nf, 'pages': None, 'epc_mb': None})

    with open(output_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['nf', 'pages', 'epc_mb'])
        w.writeheader()
        w.writerows(rows)

    print(f'\n[epc_measure] -> {output_csv}')
    print('Summary:')
    for r in rows:
        print(f"  {r['nf']:8s}  {r['epc_mb']} MB")


if __name__ == '__main__':
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument('--nfs', nargs='+', default=NFS)
    p.add_argument('--output', default='results/epc_allocation_sgx.csv')
    p.add_argument('--log', default=None)
    args = p.parse_args()
    if args.log:
        import io
        log = open(args.log, 'w')
        sys.stdout = io.TextIOWrapper(log.buffer, write_through=True)
        sys.stderr = sys.stdout
    run(nfs=args.nfs, output_csv=args.output)
