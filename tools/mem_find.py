#!/usr/bin/env python3
"""在目标进程里搜一个字节模式，用来定位 FNamePool 的块布局。

FNameEntry 的形态是 `u16 Header` + 名字字节（Header = len<<1 | bIsWide）。
所以搜 ASCII 名字本体就能找到 entry，再往回看 Header、往前找块边界。

用法: mem_find.py <ASCII名字> [最多扫多少MB] [最长几秒]
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/ 与 vendor/
from board_api import MEM_BUILD, _Mem, _find_pid  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
CHUNK = 1 << 20


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MBI), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t


def main():
    arg = sys.argv[1]
    if arg.startswith("hex:"):
        pat = bytes.fromhex(arg[4:])
    else:
        pat = arg.encode()
    max_mb = float(sys.argv[2]) if len(sys.argv) > 2 else 3072.0
    max_s = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0

    pid = _find_pid(MEM_BUILD["module"])
    m = _Mem(pid)
    t0 = time.time()
    scanned = 0
    hits = []
    addr = 0x10000
    limit = int(max_mb * 1024 * 1024)
    while addr < 0x7FFFFFFFFFFF and scanned < limit and time.time() - t0 < max_s:
        mbi = MBI()
        got = k32.VirtualQueryEx(m.h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if got != ctypes.sizeof(mbi):
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0x1000
        if mbi.State == MEM_COMMIT and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS)):
            off = 0
            while off < size and scanned < limit:
                n = int(min(CHUNK, size - off))
                buf = m.read(base + off, n)
                if buf:
                    scanned += len(buf)
                    i = buf.find(pat)
                    while i != -1:
                        hits.append(base + off + i)
                        i = buf.find(pat, i + 1)
                        if len(hits) > 40:
                            break
                off += n
                if len(hits) > 40:
                    break
        addr = base + size
        if len(hits) > 40:
            break

    print("pattern=%r  scanned=%.1f MB  %.1fs  hits=%d" % (
        pat.decode("ascii", "replace"), scanned / 1048576, time.time() - t0, len(hits)))
    for h in hits[:12]:
        raw = m.read(h - 8, 48)
        print("  0x%X  前8字节=%s  上下文=%r" % (
            h, raw[:8].hex(" ") if raw else "-",
            raw.decode("ascii", "replace") if raw else "-"))
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
