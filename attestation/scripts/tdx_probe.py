#!/usr/bin/env python3
"""Emit the MRTD of the current TDX guest.

Calls the Linux /dev/tdx_guest TDX_CMD_GET_REPORT0 ioctl to obtain a
1024-byte TDREPORT0 and prints MRTD (48 bytes) from offset 528.
Intended to be run inside the TD.
"""
from __future__ import annotations

import ctypes
import fcntl
import secrets
import sys
from pathlib import Path

TDX_DEV = Path("/dev/tdx_guest")
TDX_CMD_GET_REPORT0 = 0xC4405401  # _IOWR('T', 1, struct tdx_report_req)
MRTD_OFFSET = 528
MRTD_LEN = 48


class TdxReportReq(ctypes.Structure):
    _fields_ = [
        ("reportdata", ctypes.c_uint8 * 64),
        ("tdreport",   ctypes.c_uint8 * 1024),
    ]


def fetch_tdreport() -> bytes:
    if not TDX_DEV.exists():
        sys.exit(f"{TDX_DEV} not present; is this a TDX guest?")
    req = TdxReportReq()
    for i, b in enumerate(secrets.token_bytes(64)):
        req.reportdata[i] = b
    with open(TDX_DEV, "r+b", buffering=0) as fh:
        fcntl.ioctl(fh, TDX_CMD_GET_REPORT0, req, True)
    return bytes(req.tdreport)


def main():
    rpt = fetch_tdreport()
    if len(rpt) < MRTD_OFFSET + MRTD_LEN:
        sys.exit(f"short report: {len(rpt)} bytes")
    print(rpt[MRTD_OFFSET : MRTD_OFFSET + MRTD_LEN].hex())


if __name__ == "__main__":
    main()
