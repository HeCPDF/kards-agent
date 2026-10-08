#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`GetAdjacentCards`：支援线 `单位|单位|HQ` 的相邻集必须包含 HQ（A12 回归，离线假盘面）。

背景（2026-10-03 用户实机）：对面支援线是 `Ki-43-IIb | Ki-43-IIb | HQ`，BOMBING RAID 打了
最左边那只；**打中间那只**还会额外对 HQ 溅射 2 点（相邻两张 = 左单位 + HQ）。评估若把 HQ
排除在"支援排"之外，中间这只就少算 2 点总部伤害 ⇒ 两个目标同分、错误地选了最左那只。
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from _cards import ME, OPP, mk_card                                  # noqa: E402
import semantics.effectvm as EV                                      # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def call(hooks, args):
    return hooks["GetAdjacentCards"](None, None, None, args, None)


def main():
    hq = mk_card(900, side=OPP, location="hq", name="HQ", ptr=0x3000)
    a = mk_card(1, side=OPP, location="back", name="A", ptr=0x3001)
    b = mk_card(2, side=OPP, location="back", name="B", ptr=0x3002)
    a.obj.locationNumber, b.obj.locationNumber, hq.obj.locationNumber = 0, 1, 2
    st = SimpleNamespace(cards=[a, b, hq], turn=3, my_side=ME)
    h = EV.make_read_hooks(st, ME)
    chk("最左单位(slot0) 的相邻 = [中间单位]", call(h, [0x3001, False]) == [0x3002])
    chk("中间单位(slot1) 的相邻 = [左单位, HQ]（含 HQ！）",
        call(h, [0x3002, False]) == [0x3001, 0x3000], str(call(h, [0x3002, False])))
    chk("HQ(slot2) 的相邻 = [中间单位]", call(h, [0x3000, False]) == [0x3002])
    # HQ 在左、单位在右（先手视角）同样要成立
    hq2 = mk_card(901, side=ME, location="hq", name="HQ2", ptr=0x3010)
    c = mk_card(3, side=ME, location="back", name="C", ptr=0x3011)
    hq2.obj.locationNumber, c.obj.locationNumber = 0, 1
    st2 = SimpleNamespace(cards=[hq2, c], turn=3, my_side=ME)
    h2 = EV.make_read_hooks(st2, ME)
    chk("HQ(slot0) 的相邻 = [右侧单位]", call(h2, [0x3010, False]) == [0x3011])
    chk("右侧单位(slot1) 的相邻 = [HQ]", call(h2, [0x3011, False]) == [0x3010])
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

