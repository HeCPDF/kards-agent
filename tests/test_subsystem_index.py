#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`kardsmem.subsystems` 一次性索引 + `GetGameInstanceSubsystem` 原语回归（离线，桩掉进程）。

钉住三件事：
  ① "类→实例"索引只收名字含 subsystem 的类，并登记到**整条父类链**（BP 可能传父类）；
  ② 换数据答案要变（父类链改了 ⇒ 命中项跟着变；非子系统类查不到 ⇒ 0）；
  ③ 原语把 `(ks, Class)` 原样交给索引，缺进程会话时**如实抛** Unimplemented。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import kardsmem.objects as O                                   # noqa: E402
from kardsmem import cardnatives as CN                         # noqa: E402
from kardsmem import subsystems as S                           # noqa: E402
from kardsmem.kismetlib import Unimplemented                   # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _M:
    def __init__(self, supers):
        self.supers = supers

    def ptr_or_zero(self, addr):
        return self.supers.get(addr, 0)


class _Pool:
    def fname_of(self, c):
        return {0x10: "FooSubsystem", 0x20: "BarActor"}.get(c, "")


class _OA:
    def __init__(self, ks=None):
        pass

    def iter_objects(self):
        return [0x100, 0x200]

    def class_of(self, p):
        return 0x10 if p == 0x100 else 0x20

    def pool(self):
        return _Pool()


class _Ks:
    def __init__(self, supers, pid=1):
        self.m = _M(supers)
        self.pid = pid


def main():
    O.ObjectArray = _OA
    S._INDEX.clear()
    ks = _Ks({0x10 + 0x40: 0x30})             # SuperStruct 在 class+0x40 ⇒ FooSubsystem 的父类 = 0x30
    idx = S.build_index(ks)
    chk("索引命中子系统类", idx.get(0x10) == 0x100, repr({hex(k): hex(v) for k, v in idx.items()}))
    chk("父类也登记（BP 可能传父类）", idx.get(0x30) == 0x100, repr(idx.get(0x30)))
    chk("非子系统类不入索引", 0x20 not in idx, repr(idx.get(0x20)))
    chk("find 走缓存命中", S.find(ks, 0x10) == 0x100)
    chk("find 未命中 ⇒ 0", S.find(ks, 0x99) == 0)
    chk("info 报告条目数", (S.info(ks) or {}).get("entries") == len(idx), repr(S.info(ks)))

    # 换数据：父类链变了（新建进程 pid=2）⇒ 父类不再命中
    S._INDEX.clear()
    ks2 = _Ks({}, pid=2)
    idx2 = S.build_index(ks2)
    chk("父类链为空 ⇒ 只登记自身类", 0x30 not in idx2 and idx2.get(0x10) == 0x100,
        repr({hex(k): hex(v) for k, v in idx2.items()}))

    # ★ 2026-10-02（A9 真因）：按**继承链**判 subsystem，而不是按类名 ——
    #   `BP_OnlineMatch_C` 这个名字里没有 "subsystem"，但它是 GameInstanceSubsystem 的后代；
    #   以前按名字筛 ⇒ 每次 `GetGameInstanceSubsystem` 都未命中、触发全量重建（实测 3.25 s）。
    S._INDEX.clear()

    class _Pool2:
        def fname_of(self, c):
            return {0x10: "BP_OnlineMatch_C", 0x20: "BarActor",
                    0x40: "GameInstanceSubsystem"}.get(c, "")

    class _OA2(_OA):
        def pool(self):
            return _Pool2()

    O.ObjectArray = _OA2
    ks3 = _Ks({0x10 + 0x40: 0x40}, pid=3)      # BP_OnlineMatch_C → GameInstanceSubsystem
    idx3 = S.build_index(ks3)
    chk("名字里没有 subsystem、但继承链有 ⇒ 仍入索引",
        idx3.get(0x10) == 0x100 and idx3.get(0x40) == 0x100, repr({hex(k): hex(v) for k, v in idx3.items()}))
    chk("非 subsystem 继承链仍不入索引", 0x20 not in idx3)
    O.ObjectArray = _OA

    # 原语：把 (ks, cls) 交给索引；缺 ks 如实抛
    calls = {}
    real_find = S.find
    S.find = lambda ks_, cls_, **kw: (calls.update({"ks": ks_, "cls": cls_}) or 0x777)
    try:
        cn = CN.CardNatives(ks=_Ks({}, pid=3))
        got = CN._get_game_instance_subsystem(cn, 0, 0x10)
        chk("原语把 Class 交给索引并返回它", got == 0x777 and calls.get("cls") == 0x10, repr(got))
    finally:
        S.find = real_find
    try:
        CN._get_game_instance_subsystem(CN.CardNatives(), 0, 0x10)
        chk("缺 ks ⇒ 抛 Unimplemented", False, "没抛")
    except Unimplemented:
        chk("缺 ks ⇒ 抛 Unimplemented", True)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
