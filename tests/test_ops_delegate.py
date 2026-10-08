#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ops.conn.delegate_target`：UE5 `FScriptDelegate`（弱引用+FName）解析 —— 假内存离线测试。

背景（2026-10-03 主界面模式切换）：按钮 `OnClicked` 多播里订阅的才是真正生效的处理器，
所以要把"订阅者对象 + 函数名"读出来。弱引用是 `{ObjectIndex@+0, Serial@+4}`，必须拿
`FUObjectItem.SerialNumber`（`+0x10`）核对（GC 会复用槽位）；FName 在 `+8`。
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import inject as OI                                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class FakeM:
    def __init__(self, mem):
        self.mem, self.pid, self.base = mem, 1, 1

    def u32(self, a):
        return struct.unpack_from("<I", self.mem, a)[0]

    def i32(self, a):
        return struct.unpack_from("<i", self.mem, a)[0]

    def ptr_or_zero(self, a):
        return self.u32(a)


class FakeOA:
    """`FUObjectItem` 步长 0x18：Object@+0、Serial@+0x10。"""

    def __init__(self, mem, objs, serials):
        self.mem, self.objs, self.serials = mem, objs, serials

    def count(self):
        return len(self.objs)

    def item_addr(self, i):
        return 0x1000 + i * 0x18

    def at(self, i):
        return self.objs[i]


class FakePool:
    def __init__(self, names):
        self.names = names

    def fname(self, index, number=0):
        return self.names.get((index, number))


class FakeApi:
    """`peek(obj_hex, off, kind)` —— 供 `delegate_num`（它走 `self.peek` → `api()`）。"""

    def __init__(self, mem):
        self.mem = mem

    def peek(self, obj_hex, off, kind):
        a = int(obj_hex, 16) + off
        if kind == "s32":
            return struct.unpack_from("<i", self.mem, a)[0]
        if kind == "u8":
            return self.mem[a]
        return None


def mk_inj(mem, objs, serials, names):
    inj = OI.Injector.__new__(OI.Injector)          # 不走 __init__：不 attach、不碰游戏
    inj.m, inj.oa, inj.pool = FakeM(mem), FakeOA(mem, objs, serials), FakePool(names)
    inj._api = FakeApi(mem)
    return inj


def write_delegate(mem, obj, off, entries):
    """在 `obj+off` 写一个多播数组：Data 指针 + Num；entries=[(oi, osn, fi, fn), ...]。"""
    data = 0x3000
    struct.pack_into("<Q", mem, obj + off, data)
    struct.pack_into("<i", mem, obj + off + 8, len(entries))
    for i, (oi, osn, fi, fn) in enumerate(entries):
        struct.pack_into("<IIII", mem, data + 16 * i, oi, osn, fi, fn)


def main():
    mem = bytearray(0x4000)
    mem[0x1000 + 3 * 0x18 + 0x10:0x1000 + 3 * 0x18 + 0x14] = struct.pack("<I", 0x77)   # item[3].Serial
    inj = mk_inj(mem, objs=[0, 0, 0, 0x2000], serials=None,
                 names={(5, 0): "BndEvt__WBP_NUI_Playbar_Training_..._5_onClicked__DelegateSignature"})
    write_delegate(mem, 0x2000, 0x380, [(3, 0x77, 5, 0)])
    got = inj.delegate_target(0x2000, 0x380)
    chk("订阅者解析：弱引用(3, 0x77) ⇒ (0x2000, 函数名)",
        got == (0x2000, "BndEvt__WBP_NUI_Playbar_Training_..._5_onClicked__DelegateSignature"), str(got))

    write_delegate(mem, 0x2000, 0x380, [(3, 0x78, 5, 0)])
    chk("序号不匹配（槽位已被复用）⇒ None", inj.delegate_target(0x2000, 0x380) is None)

    write_delegate(mem, 0x2000, 0x380, [])
    chk("空多播 ⇒ None", inj.delegate_target(0x2000, 0x380) is None)

    write_delegate(mem, 0x2000, 0x380, [(99, 0x77, 5, 0)])
    chk("弱引用索引越界 ⇒ None", inj.delegate_target(0x2000, 0x380) is None)

    write_delegate(mem, 0x2000, 0x380, [(3, 0x77, 9, 9)])
    chk("FName 查不到 ⇒ None", inj.delegate_target(0x2000, 0x380) is None)

    write_delegate(mem, 0x2000, 0x380, [(3, 0x77, 5, 0)])
    chk("delegate_num 读 Num", inj.delegate_num(0x2000, 0x380) == 1)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

