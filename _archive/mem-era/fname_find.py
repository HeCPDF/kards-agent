#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fname_find.py —— 用字节特征在进程里定位 FNamePool 的 chunk，并反查 Blocks 数组。

思路：不再假设 pool 结构体布局。chunk 里第一个 entry 一定是 "None"，
紧接着 "ByteProperty"，这个字节序列（允许 stride 对齐填充）在进程里几乎是唯一的。
找到 chunk0 之后，再全进程搜"谁的 qword 等于 chunk0 地址"，那个位置就是 Blocks[]。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import sys

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
import fname_live as F

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__a2", wt.DWORD)]


_k32 = F._k32
_k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t


def regions(h, lo=0x10000, hi=0x7FFFFFFFFFFF):
    addr = lo
    while addr < hi:
        b = MBI()
        got = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(b), ctypes.sizeof(b))
        if not got:
            break
        base = b.BaseAddress or 0
        size = b.RegionSize or 0
        if b.State == MEM_COMMIT and not (b.Protect & (PAGE_GUARD | PAGE_NOACCESS)) and size:
            yield base, size, b.Protect
        addr = base + size


def scan_for(h, needle, chunk=1 << 20, overlap=64, align=None, window=None):
    """在已提交内存里找 needle（bytes），yield (addr, region_base)。"""
    for base, size, prot in regions(h):
        if window and not (window[0] <= base < window[1]):
            continue
        o = 0
        while o < size:
            n = min(chunk, size - o)
            buf = F.Mem.read.__get__(MemProxy(h))  # placeholder, replaced below
            o += n
    return


class MemProxy:
    def __init__(self, h):
        self.h = h
        self.raw = None


def main():
    pid = F.find_process()
    base, size = F.module_base(pid)
    m = F.Mem(pid, base)
    print("pid=%d base=0x%X" % (pid, base))

    # ---------- pass 1: 找 "None" 紧跟 "ByteProperty" ----------
    cands = []
    total = 0
    for rbase, rsize, prot in regions(m.h):
        total += rsize
        o = 0
        while o < rsize:
            n = min(1 << 20, rsize - o)
            buf = m.read(rbase + o, n)
            if buf:
                k = buf.find(b"None")
                while k >= 0:
                    j = buf.find(b"ByteProperty", k + 4, k + 4 + 24)
                    if j >= 0:
                        cands.append(rbase + o + k)
                    k = buf.find(b"None", k + 1)
            o += n
    print("扫描 %.1f MB 已提交内存，候选 chunk0 = %d" % (total / 1048576.0, len(cands)))

    good = []
    for a in cands:
        for hsz in (2, 4):
            e0 = a - hsz
            hd = m.u16(e0)
            if hd is None:
                continue
            for shift in (1, 6):
                if hd >> shift != 4:
                    continue
                # 走一遍 entry，看能不能连续解析
                p = e0
                n_ok = 0
                bad = 0
                while bad < 8 and n_ok < 200000:
                    hh = m.u16(p)
                    if hh is None:
                        break
                    ln = hh >> shift
                    if ln == 0 or ln > 0x200:
                        bad += 1
                        p += (2 if hsz == 2 else 4)
                        continue
                    sb = m.read(p + hsz, ln if not (hh & 1) else ln * 2)
                    if not sb:
                        break
                    if not (hh & 1):
                        if any(c < 32 or c > 126 for c in sb):
                            bad += 1
                            p += (2 if hsz == 2 else 4)
                            continue
                    n_ok += 1
                    bad = 0
                    p += hsz + (ln if not (hh & 1) else ln * 2)
                    if (p - e0) % (2 if hsz == 2 else 4):
                        p += (2 if hsz == 2 else 4) - ((p - e0) % (2 if hsz == 2 else 4))
                if n_ok >= 8:
                    good.append((e0, hsz, shift, n_ok))
    print("可解析的 chunk0 候选 = %d" % len(good))
    for e0, hsz, shift, n_ok in good[:10]:
        print("  chunk0=0x%X  header=%d  stride=%d  shift=%d  连续 entry≈%d  head=%r" %
              (e0, hsz, hsz, shift, n_ok, m.read(e0, 48)))
    if not good:
        print("!! 没找到普通（明文）FNamePool chunk —— 可能名字池被加密/压缩")
        return 1

    # ---------- pass 2: 找谁存着 chunk0 的地址 ----------
    targets = {g[0]: g for g in good}
    print("\n-- 搜索指向 chunk0 的 qword --")
    ptr_hits = []
    for rbase, rsize, prot in regions(m.h):
        o = 0
        while o < rsize:
            n = min(1 << 20, rsize - o)
            buf = m.read(rbase + o, n)
            if buf:
                for t in targets:
                    pat = struct.pack("<Q", t)
                    k = buf.find(pat)
                    while k >= 0:
                        ptr_hits.append((rbase + o + k, t))
                        k = buf.find(pat, k + 1)
            o += n
    print("命中 %d 处" % len(ptr_hits))
    for a, t in ptr_hits[:20]:
        print("  @0x%X 保存 chunk0=0x%X   周围：" % (a, t))
        m.hexdump(a - 0x30, 0x70, "ctx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
