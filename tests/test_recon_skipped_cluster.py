#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跳过簇修复的离线回归（2026-10-06）：占位卡 IsUnit / 哨兵 kredit / 结构体 Map 值读法 / 空数组 GetRandomCard。"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from kardsmem.cardnatives import CardNatives                      # noqa: E402
from kardsmem.gamemodel import ESide                              # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _B:
    our_turn = True


def main():
    cn = CardNatives(_B(), my_seat=ESide.left)
    # 占位卡（Side Effect Holder）：type_enum 0、card_type None ⇒ IsUnit 为假而不是"读不到"
    ph = {"card_type": None, "type_enum": 0}
    chk("占位卡 IsUnit ⇒ False", cn.call("IsUnit", ph) is False)
    chk("占位卡 IsGroundUnit ⇒ False", cn.call("IsGroundUnit", ph) is False)
    try:
        cn.call("IsUnit", {"card_type": None})
        chk("type_enum 也读不到 ⇒ 仍抛", False)
    except Exception as ex:                                       # noqa: BLE001
        chk("type_enum 也读不到 ⇒ 仍抛", "card_type" in str(ex), str(ex)[:50])
    # 记录 mult==0 的哨兵：kredit/kreditBuff = +100 ⇒ 总费用夹到 99
    z = {"card_type": None, "type_enum": 0,
         "records_raw": {"kredit": (0, 0, 0, 0, 0), "kreditBuff": (0, 0, 0, 0, 0)}}
    chk("哨兵记录 getTotalKreditCost ⇒ 99", cn.call("getTotalKreditCost", z) == 99)
    chk("有 total_kredit_cost 就直接用", cn.call("getTotalKreditCost", {"total_kredit_cost": 3}) == 3)

    # canplay 的 TArray<int32> / FString 读法
    import canplay as CP

    class M:
        def __init__(self):
            self.b = {}

        def read_exact(self, a, n):
            return self.b.get(a)

        read = read_exact

        def ptr_or_zero(self, a):
            d = self.b.get(a)
            return struct.unpack("<Q", d[:8])[0] if d else 0

        def i32(self, a):
            d = self.b.get(a)
            return struct.unpack("<i", d[:4])[0] if d else 0
    m = M()
    m.b[0x100] = struct.pack("<QiI", 0x200, 2, 2)
    m.b[0x200] = struct.pack("<ii", 7, 9)
    m.b[0x200 + 0] = struct.pack("<ii", 7, 9)
    # i32(data + i*4)
    m.b[0x204] = struct.pack("<i", 9)
    chk("TArray<int32> 读出 [7,9]", CP._read_int32_array(m, 0x100) == [7, 9])
    m.b[0x300] = struct.pack("<QiI", 0, 0, 0)
    chk("空 TArray ⇒ []", CP._read_int32_array(m, 0x300) == [])

    # 空数组 GetRandomCard：nullptr、不是随机点
    from engine.effectvm import Recorder

    class _E:
        class _K:
            def __init__(self, n):
                self.args = {"prop": n}
        kids = [_K("a"), _K("b"), _K("out")]

    class _F:
        locals = {}
    r = Recorder(1, 2)
    fr = _F()
    fr.locals = {}
    r.hook("GetRandomCard")(None, fr, None, [[], False, None], _E())
    chk("GetRandomCard 空数组 ⇒ out=0、无机会节点、不污染", fr.locals.get("out") == 0 and not r.chance and not r.tainted)
    return 1 if fails else 0


if __name__ == "__main__":
    rc = main()
    print("失败 %d 项" % fails)
    sys.exit(rc)
