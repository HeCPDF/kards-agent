#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fname_live.py —— 只读解析 KARDS 运行进程里的 UE5 FNamePool，把 FName 还原成字符串。

为什么之前失败
==============
之前扫描用的是 UE4 的 FNameEntry 头格式（`0E 00 "UObject"`），UE5 的头是
`bIsWide:1 | LowercaseProbeHash:5 | Len:10`，`Len` 不在最低位，所以整库零命中。
Dumper-7 自己也不硬编码：它在 `Dumper/Engine/Private/Unreal/NameArray.cpp` 里
**运行时探测** 字符串偏移、stride 和长度位移。本文件是那段逻辑的 Python 移植。

关键事实（来自 Dumper-7 源码 + Dumpspace/OffsetsInfo.json）
==========================================================
- `GNames` **不 deref**：FNamePool 对象本身就在 `base + 0x090E2E28`。
- pool 结构：`[int32 MaxChunkIndex][int32 ByteCursor][void* Chunks[]]`，具体偏移由扫描确定。
- 每块（chunk）里是连续的 FNameEntry；`index = (chunkIdx << blockBits) | inChunkEntryIdx`。
- entry 头 2 或 4 字节（stride 2/4），字符串紧跟头（可能是 UTF-16）。

只读：OpenProcess(QUERY_INFORMATION|VM_READ) + ReadProcessMemory，无写入/注入。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import struct
import sys

RVA_GNAMES = 0x090E2E28
RVA_GWORLD = 0x08F625B0
RVA_GOBJECTS = 0x091FF4E0
IMAGE_SIZE = 0x9CC8000
EXE_MD5 = "395e470f06837f6e60ce5c53c6df2a22"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x2, 0x8, 0x10
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE), ("szModule", wt.WCHAR * 256),
                ("szExePath", wt.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.CloseHandle.argtypes = [wt.HANDLE]


def find_process(exe="kards-Win64-Shipping.exe"):
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("mbcs", "replace").lower() == exe.lower():
                return e.th32ProcessID
            ok = _k32.Process32Next(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return None


def module_base(pid, exe="kards-Win64-Shipping.exe"):
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return None, None
    try:
        m = _MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(m)
        ok = _k32.Module32FirstW(snap, ctypes.byref(m))
        while ok:
            if m.szModule.lower() == exe.lower():
                return m.modBaseAddr, m.modBaseSize
            ok = _k32.Module32NextW(snap, ctypes.byref(m))
    finally:
        _k32.CloseHandle(snap)
    return None, None


class Mem:
    def __init__(self, pid, base):
        self.pid, self.base = pid, base
        self.h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise RuntimeError("OpenProcess(%d) err=%d" % (pid, ctypes.get_last_error()))
        self._buf = ctypes.create_string_buffer(0x20000)

    def read(self, addr, n):
        if not addr or n <= 0 or n > len(self._buf):
            return None
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), self._buf, n, ctypes.byref(got))
        if not ok or got.value != n:
            return None
        return self._buf.raw[:n]

    def u8(self, a):
        b = self.read(a, 1)
        return b[0] if b else None

    def u16(self, a):
        b = self.read(a, 2)
        return struct.unpack("<H", b)[0] if b else None

    def i32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<i", b)[0] if b else None

    def u32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<I", b)[0] if b else None

    def ptr(self, a):
        b = self.read(a, 8)
        if not b:
            return 0
        p = struct.unpack("<Q", b)[0]
        return p if PTR_MIN <= p < PTR_MAX else 0

    def hexdump(self, addr, n, label=""):
        b = self.read(addr, n)
        if not b:
            print("  <%s> unreadable @0x%X" % (label, addr))
            return
        for off in range(0, len(b), 16):
            row = b[off:off + 16]
            hexs = " ".join("%02X" % c for c in row)
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
            print("  %08X  %-47s  %s" % (off, hexs, txt))


# --------------------------------------------------------------------------
# FNamePool（Dumper-7 NameArray::InitializeNamePool 的移植）
# --------------------------------------------------------------------------
class FNamePool:
    def __init__(self, m: Mem):
        self.m = m
        self.addr = m.base + RVA_GNAMES
        self.header_size = None
        self.stride = None
        self.shift = None
        self.chunks_start = None
        self.max_chunk = None
        self.byte_cursor = None
        self.chunks = []
        self.ok = False

    def find_layout(self, verbose=True):
        m = self.m
        pool = self.addr
        raw = m.read(pool, 0x40)
        if not raw:
            print("!! pool 头读不出来 @0x%X" % pool)
            return False
        if verbose:
            print("FNamePool @ 0x%X  (base+0x%X)" % (pool, RVA_GNAMES))
            m.hexdump(pool, 0x40, "pool head")

        found = None
        for i in range(0x0, 0x20, 4):
            cand = m.i32(pool + i)
            if not cand or cand <= 0 or cand > 0x10000:
                continue
            nz = 0
            first = None
            since = 0
            off = i + 8 + (i % 8)
            for j in range(0, 0x10000, 8):
                p = m.ptr(pool + off + j)
                if p:
                    nz += 1
                    since = 0
                    if first is None:
                        first = off + j
                else:
                    since += 1
                    if since == 0x500:
                        break
            if verbose:
                print("  i=0x%X cand=%-8d nonnull=%-6d %s" %
                      (i, cand, nz, "<== MATCH" if cand == nz - 1 else ""))
            if cand == nz - 1:
                found = (i, first, cand)
                break
        if not found:
            print("!! 没找到 MaxChunkIndex（布局或加密不符合预期）")
            return False
        self.max_chunk, self.chunks_start, self.byte_cursor = found[2], found[1], found[0] + 4
        self.chunks = []
        for c in range(self.max_chunk + 1):
            self.chunks.append(m.ptr(pool + self.chunks_start + c * 8))
        if verbose:
            print("  MaxChunkIndex=%d  ByteCursor@+0x%X  ChunksStart@+0x%X" %
                  (self.max_chunk, self.byte_cursor, self.chunks_start))
            print("  chunks: " + ", ".join("0x%X" % c for c in self.chunks[:6]) +
                  (" ..." if len(self.chunks) > 6 else ""))
        if not self.chunks[0]:
            print("!! chunks[0] 为空")
            return False

        ch0 = self.chunks[0]
        head = m.read(ch0, 0x2000)
        if not head:
            print("!! chunks[0] 不可读")
            return False

        pos_none = head.find(b"None")
        pos_core = head.find(b"/Script/CoreUObject")
        if verbose:
            print("  chunks[0]=0x%X  'None'@+0x%X  '/Script/CoreUObject'@+0x%X" %
                  (ch0, pos_none, pos_core))
        if pos_none < 0 or pos_core < 0:
            print("!! 前两块里没有 None / CoreUObject —— 不是普通 FNamePool")
            return False
        self.header_size = pos_none
        self.stride = 2 if pos_none == 2 else 4

        pos_bp = head.find(b"ByteProperty")
        start = pos_bp - self.header_size
        if start > 0 and head[start - self.stride:start - self.stride + self.stride] != b"\x00" * self.stride:
            pass
        for shift in range(1, 17):
            h_none = struct.unpack_from("<H", head, pos_none - self.header_size)[0]
            h_bp = struct.unpack_from("<H", head, start)[0]
            if (h_none >> shift) == 4 and (h_bp >> shift) == 12:
                self.shift = shift
                break
        if verbose:
            print("  header_size=0x%X  stride=0x%X  len_shift=%s" %
                  (self.header_size, self.stride, self.shift))
        self.ok = self.shift is not None
        return self.ok

    def entry_addr(self, idx, bits):
        chunk = self.chunks[idx >> bits]
        off = (idx & ((1 << bits) - 1)) * self.stride
        return chunk + off

    def name_at(self, idx, bits=16, depth=0):
        if depth > 3:
            return None
        if idx < 0 or (idx >> bits) > self.max_chunk:
            return None
        e = self.entry_addr(idx, bits)
        head = self.m.read(e, self.header_size)
        if not head:
            return None
        h = struct.unpack("<H", head)[0]
        ln = h >> self.shift
        if ln == 0:
            nxt = self.m.i32(e + self.header_size)
            num = self.m.i32(e + self.header_size + 4)
            base = self.name_at(nxt, bits, depth + 1)
            if base is None:
                return None
            return base + (("_%d" % (num - 1)) if num and num > 0 else "")
        if ln > 0x400:
            return None
        if h & 1:
            b = self.m.read(e + self.header_size, ln * 2)
            if not b:
                return None
            s = b.decode("utf-16-le", "replace")
        else:
            b = self.m.read(e + self.header_size, ln)
            if not b:
                return None
            s = b.decode("latin-1")
        return s

    def fname(self, comp_idx, number=0):
        s = self.name_at(comp_idx, 16)
        if s is None:
            return None
        if number and number > 0:
            return "%s_%d" % (s, number - 1)
        return s


def collect_names(m, arr, num, cls_off, name_off, limit=400, bits=16):
    """遍历 GObjects 收集 (obj, class, nameIdx, number)。"""
    out = []
    for i in range(min(num, limit * 8)):
        p = m.ptr(arr + 16 * i)
        if not p:
            continue
        cls = m.ptr(p + cls_off)
        nm = m.u32(p + name_off)
        num2 = m.u32(p + name_off + 4)
        if cls is None or nm is None:
            continue
        out.append((p, cls, nm, num2))
        if len(out) >= limit:
            break
    return out


def score_bits(pool, m, objs, bits):
    good = 0
    total = 0
    for (_p, _c, nm, num2) in objs:
        s = pool.name_at(nm, bits)
        if s is None:
            total += 1
            continue
        total += 1
        if s and all(32 <= ord(ch) < 127 for ch in s) and len(s) >= 2:
            good += 1
    return (good, total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300, help="GObjects 采样对象数")
    ap.add_argument("--resolve", type=int, nargs="*", default=None, help="按 FName index 解析")
    ap.add_argument("--dump-entries", type=int, default=0, help="顺序打印前 N 个池条目")
    ap.add_argument("--card", action="store_true", help="读当前对局卡牌的 Name_0 FName")
    ap.add_argument("--exe", default="kards-Win64-Shipping.exe")
    a = ap.parse_args()

    pid = find_process(a.exe)
    if not pid:
        print("找不到进程 %s" % a.exe)
        return 2
    base, size = module_base(pid, a.exe)
    print("pid=%d base=0x%X SizeOfImage=0x%X" % (pid, base, size))
    if size != IMAGE_SIZE:
        print("!! SizeOfImage 不匹配（期望 0x%X）—— 构建不对，停止" % IMAGE_SIZE)
        return 2
    m = Mem(pid, base)

    pool = FNamePool(m)
    if not pool.find_layout():
        return 1

    # ---- 用 GObjects 的类名/对象名做一次全局校验，同时校准 blockBits ----
    gobj = m.base + RVA_GOBJECTS
    arr = m.ptr(gobj)
    num = m.i32(gobj + 8)
    print("GObjects @0x%X  Array=0x%X  NumElements=%d" % (gobj, arr, num or -1))
    if not arr or not num:
        return 1
    objs = collect_names(m, arr, num, 0x10, 0x18, limit=a.limit)
    print("采样对象 %d 个" % len(objs))
    best = None
    for bits in (12, 13, 14, 15, 16):
        good, total = score_bits(pool, m, objs, bits)
        print("  blockBits=%-3d 可解码 %d/%d" % (bits, good, total))
        if best is None or good > best[1]:
            best = (bits, good, total)
    print("=> 采用 blockBits=%d (可解码 %d/%d)" % best)
    bits = best[0]

    print("\n---- 采样对象名字 ----")
    for (p, cls, nm, num2) in objs[:40]:
        cn = pool.fname(m.u32(cls + 0x18), m.u32(cls + 0x1C)) if cls else None
        print("  0x%012X  classIdx=%-8d %-40s  nameIdx=%-8d %s" %
              (p, m.u32(cls + 0x18) if cls else -1, cn or "?", nm, pool.name_at(nm, bits) or "?"))

    if a.resolve:
        print("\n---- 指定 index ----")
        for i in a.resolve:
            print("  %d -> %s" % (i, pool.name_at(i, bits)))

    if a.dump_entries:
        print("\n---- 池条目 0..%d ----" % a.dump_entries)
        for i in range(a.dump_entries):
            print("  [%d] %s" % (i, pool.name_at(i, bits)))

    if a.card:
        world = m.ptr(m.base + RVA_GWORLD)
        gs = m.ptr(world + 0x1B0) if world else 0
        print("\n---- 对局 ----")
        print("  GWorld=0x%X GameState=0x%X" % (world, gs))
        if gs:
            cls = m.ptr(gs + 0x10)
            psize = m.i32(cls + 0x58) if cls else None
            print("  GameState class=0x%X PropertiesSize=%s name=%s" %
                  (cls, psize, pool.fname(m.u32(cls + 0x18), m.u32(cls + 0x1C)) if cls else "?"))
            ao = m.ptr(gs + 0x538)
            an = m.i32(gs + 0x538 + 8)
            print("  AllCardsInBattle Data=0x%X Num=%s" % (ao, an))
            if ao and an:
                for k in range(min(an, 40)):
                    el = ao + 24 * k
                    card = m.ptr(el + 8)
                    if not card:
                        continue
                    ccls = m.ptr(card + 0x10)
                    cname = pool.fname(m.u32(ccls + 0x18), m.u32(ccls + 0x1C)) if ccls else None
                    prop = m.u32(card + 0x50)
                    pnum = m.u32(card + 0x54)
                    loc = m.u8(card + 0x275)
                    side = m.u8(card + 0x276)
                    print("  #%02d card=0x%012X class=%-34s Name_0 idx=%-8d num=%d -> %-32s loc=%s side=%s" %
                          (k, card, cname or "?", prop or -1, pnum or 0,
                           pool.fname(prop, pnum) or "?", loc, side))
    return 0


if __name__ == "__main__":
    sys.exit(main())
