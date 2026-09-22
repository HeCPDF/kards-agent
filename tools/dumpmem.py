# -*- coding: utf-8 -*-
"""把 minidump 当成一个只读内存源挂进 kardsmem。

读侧的底层接口只要求一个 `read(addr, size)`，所以换后端是干净的：
进程活着就读进程，进程没了就读转储 —— 上层的世界/卡牌解析逻辑一行不用改。
"""
import bisect
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  —— 接上 kards-agent/ 与 vendor/
from mdmp import ranges, streams

MODULE_LIST = 4


def modules(path):
    """→ [(base, size, name)]"""
    st = streams(path)
    if MODULE_LIST not in st:
        return []
    _sz, rva = st[MODULE_LIST]
    with open(path, "rb") as f:
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(108 * n)
        out = []
        for i in range(n):
            base, size, _ck, _ts, name_rva = struct.unpack_from("<QIIII", raw, 108 * i)
            f.seek(name_rva)
            ln = struct.unpack("<I", f.read(4))[0]
            nm = f.read(ln).decode("utf-16-le", "replace")
            out.append((base, size, nm))
        return out


class DumpMem:
    """只读：从 minidump 的内存区段里取字节。接口与 board_api._Mem 一致。"""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.r = ranges(path)
        self.starts = [x[0] for x in self.r]

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def read(self, addr, size):
        if not addr or size <= 0:
            return None
        i = bisect.bisect_right(self.starts, addr) - 1
        if i < 0:
            return None
        va, sz, off = self.r[i]
        if addr + size > va + sz:
            return None                      # 跨区段不拼接：宁可读不出，不给撕裂的数据
        self.f.seek(off + (addr - va))
        d = self.f.read(size)
        return d if len(d) == size else None

    # -- 与 _Mem / MemRO 相同的便捷读 --
    def read_exact(self, a, n):
        d = self.read(a, n)
        return d if (d and len(d) == n) else None

    def atomic(self, a, n, tries=1, gap=0.0):
        return self.read_exact(a, n)         # 转储是静止的，原子读就是普通读

    def u8(self, a):
        d = self.read(a, 1)
        return d[0] if d else None

    def u16(self, a):
        d = self.read_exact(a, 2)
        return struct.unpack("<H", d)[0] if d else None

    def i16(self, a):
        d = self.read_exact(a, 2)
        return struct.unpack("<h", d)[0] if d else None

    def i32(self, a):
        d = self.read_exact(a, 4)
        return struct.unpack("<i", d)[0] if d else None

    def u32(self, a):
        d = self.read_exact(a, 4)
        return struct.unpack("<I", d)[0] if d else None

    def u64(self, a):
        d = self.read_exact(a, 8)
        return struct.unpack("<Q", d)[0] if d else None

    def i64(self, a):
        d = self.read_exact(a, 8)
        return struct.unpack("<q", d)[0] if d else None

    def ptr(self, a):
        d = self.read_exact(a, 8)
        if not d:
            return None
        v = struct.unpack("<Q", d)[0]
        return v if 0x10000 <= v <= 0x7FFFFFFFFFFF else None

    def ptr_or_zero(self, a):
        return self.ptr(a) or 0

    def blob(self, a, n):
        return self.read(a, n)


class DumpSession:
    """够 kardsmem.world / kardsmem.cards 用的最小 session：只要 .m 和 .base。"""

    def __init__(self, path, module="kards-Win64-Shipping.exe"):
        self.m = DumpMem(path)
        self.base = 0
        for b, s, nm in modules(path):
            if nm.lower().endswith(module.lower()):
                self.base = b
                break
        if not self.base:
            raise SystemExit("转储里找不到模块 %s" % module)
        self.pid = 0

    def names_pool(self):
        """转储里的 FNamePool 一样能用：池是进程堆里的普通数据，不是活对象。"""
        from kardsmem.names import FNamePool
        return FNamePool(self.m, self.base)

    def close(self):
        self.m.close()


if __name__ == "__main__":
    for p in sys.argv[1:]:
        s = DumpSession(p)
        print("%s\n   模块基址 %#x" % (os.path.basename(p), s.base))
        from kardsmem.world import Locator
        loc = Locator(s.m, s.base)
        print("   GWorld=%#x  gamestate=%#x" % (s.m.ptr(s.base + 0x0) or 0, loc.gamestate or 0))
        s.close()
