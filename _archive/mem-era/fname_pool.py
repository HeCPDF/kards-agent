#!/usr/bin/env python3
"""三步反推 FNamePool 的布局，然后解出 FName。

1) 搜 `08 00 "None"` —— UE 里 FName idx 0 就是 "None"，它的 entry 就是块 0 基址；
2) 在内存里搜"哪个 8 字节等于块 0 基址" —— 那就是 `Blocks[0]` 的位置，
   减去 GNames 偏移就得到 `Blocks` 在池子里的偏移；
3) 用 (idx>>16, (idx&0xFFFF)*2) 解任意 FName。

只读。
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import MEM_BUILD, RVA_GNAMES, _Mem, _find_pid  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS = 0x1000, 0x100, 0x01
CHUNK = 1 << 20


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t


def regions(m, lo=0x10000, hi=0x7FFFFFFFFFFF):
    addr = lo
    while addr < hi:
        mbi = MBI()
        if k32.VirtualQueryEx(m.h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)) != ctypes.sizeof(mbi):
            return
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0x1000
        if (mbi.State == MEM_COMMIT
                and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))
                and mbi.Protect != 0):
            yield base, size
        addr = base + size


def scan(m, pat, cap_s=60.0, want=8):
    t0, hits, scanned = time.time(), [], 0
    for base, size in regions(m):
        off = 0
        while off < size and time.time() - t0 < cap_s:
            n = int(min(CHUNK, size - off))
            buf = m.read(base + off, n)
            if buf:
                scanned += len(buf)
                i = buf.find(pat)
                while i != -1:
                    hits.append(base + off + i)
                    i = buf.find(pat, i + 1)
                    if len(hits) >= want:
                        return hits, scanned, time.time() - t0
            off += n
    return hits, scanned, time.time() - t0


def main():
    pid = _find_pid(MEM_BUILD["module"])
    m = _Mem(pid)
    base = None
    for b, s in regions(m):
        if s > 0x00800000:            # 主模块镜像
            base = b
            break
    print("module base = 0x%X" % (base or 0))
    cell = base + RVA_GNAMES

    print("\n[1] 搜 FName idx0 的 entry（08 00 'None'）...")
    hits, scanned, dt = scan(m, b"\x08\x00None", want=4)
    print("    扫了 %.0f MB %.1fs，命中 %d：%s" % (
        scanned / 1048576, dt, len(hits), [hex(h) for h in hits]))
    if not hits:
        m.close()
        return 1
    block0 = hits[0]
    # 往回找块边界不必要：块 0 基址 = 该 entry 地址 - 0
    print("    假定 块0 基址 = 0x%X" % block0)

    print("\n[2] 搜谁存着指向 块0 的指针（= Blocks[0]）...")
    pat = struct.pack("<Q", block0)
    hits2, scanned2, dt2 = scan(m, pat, want=8)
    print("    扫了 %.0f MB %.1fs，命中 %d：%s" % (
        scanned2 / 1048576, dt2, len(hits2), [hex(h) for h in hits2]))
    off_blocks = [(h - cell) for h in hits2]
    print("    相对 GNames cell 的偏移: %s" % [hex(o) for o in off_blocks])

    if not off_blocks:
        print("    ✗ 没找到 Blocks 数组")
        m.close()
        return 1
    blk_off = off_blocks[0]
    pool = m.ptr(cell)
    print("    *(cell) = 0x%s" % (hex(pool) if pool else "None"))

    def resolve(idx, pool_ptr):
        blocks = m.ptr(pool_ptr + blk_off)
        if not blocks:
            return None
        b = m.ptr(blocks + (idx >> 16) * 8)
        if not b:
            return None
        e = b + (idx & 0xFFFF) * 2
        raw = m.read(e, 4)
        if not raw:
            return None
        h = struct.unpack_from("<H", raw, 0)[0]
        wide, ln = h & 1, h >> 1
        if not (0 < ln <= 200):
            return None
        buf = m.read(e + 2, ln * (2 if wide else 1))
        try:
            return buf.decode("utf-16-le" if wide else "ascii", "replace")
        except Exception:
            return None

    for pool_ptr in (pool, cell):
        if not pool_ptr:
            continue
        print("\n[3] 用 pool=0x%X, Blocks@+0x%X 解 idx:" % (pool_ptr, blk_off))
        for idx in (0, 1, 2, 593173, 735836, 865053, 859216):
            print("    %-8d -> %r" % (idx, resolve(idx, pool_ptr)))
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
