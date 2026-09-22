#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fname_blocks.py —— 定位 FNamePool 的所有 chunk 块，并反查存这些块地址的数组。

block 依据（本 build 实测）：
  FNameEntry 头 2 字节 = (Len<<6) | (Hash<<1) | bIsWide，字符串紧跟其后（窄=ASCII，宽=UTF-16）。
  块分配 0x20000 字节 → 65536 个 entry → FNameBlockOffsetBits = 16。
  index = (blockIdx << 16) | (byteOffset >> 1)
"""
from __future__ import annotations

import ctypes
import struct
import sys

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
import fname_live as F
import fname_find as FF

STRIDE = 2
SHIFT = 6


def entry_at(m, e):
    """严格解析一条 FNameEntry，返回字符串或 None。"""
    h = m.u16(e)
    if h is None:
        return None
    ln = h >> SHIFT
    if ln <= 0 or ln > 0x100:
        return None
    if h & 1:
        raw = m.read(e + STRIDE, ln * 2)
        if not raw:
            return None
        s = raw.decode("utf-16-le", "replace")
    else:
        raw = m.read(e + STRIDE, ln)
        if not raw:
            return None
        if any(c < 32 or c > 126 for c in raw):
            return None
        s = raw.decode("latin-1")
    if not s or s[0] < " ":
        return None
    return s


def walk(m, a, want=8):
    p = a
    names = []
    for _ in range(want):
        s = entry_at(m, p)
        if s is None:
            return names
        names.append(s)
        p += STRIDE + len(s) * (2 if m.u16(p) & 1 else 1)
    return names


def main():
    pid = F.find_process()
    base, size = F.module_base(pid)
    m = F.Mem(pid, base)
    print("pid=%d base=0x%X" % (pid, base))

    known = 0x16D4E660000
    print("对照 chunk0 0x%X -> %r" % (known, walk(m, known, 4)))

    cands = []
    for rb, rs, pr in FF.regions(m.h):
        a = (rb + 0x1FFFF) & ~0x1FFFF
        while a + 0x100 < rb + rs:
            names = walk(m, a, 8)
            if len(names) >= 7:
                cands.append((a, names[:4]))
            a += 0x20000
    print("块候选 %d 个:" % len(cands))
    for a, nm in cands[:20]:
        print("   0x%X  %s" % (a, nm))

    bases = {a for a, _ in cands}
    print("\n-- 反查：谁存着这些块地址 --")
    found = []
    for rb, rs, pr in FF.regions(m.h):
        o = 0
        while o < rs:
            n = min(1 << 20, rs - o)
            buf = m.read(rb + o, n)
            if buf:
                for k in range(0, len(buf) - 8, 8):
                    v = struct.unpack_from("<Q", buf, k)[0]
                    if v in bases:
                        found.append((rb + o + k, v))
            o += n
    print("命中 %d 处" % len(found))
    for a, v in found[:10]:
        print("   @0x%X -> 0x%X" % (a, v))
    if found:
        a0 = min(a for a, _ in found)
        print("   数组上下文 @0x%X:" % a0)
        m.hexdump(a0 - 0x20, 0x80, "arr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
