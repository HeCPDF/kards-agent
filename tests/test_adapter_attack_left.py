#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器读 `attackLeft`（2026-10-07 实机）：游戏的 `CanCardDoAnything` 只说"还能做点什么"，已攻击过/移动过的单位
`attackLeft=0` ⇒ sim 里 `attacks_left=0`、不再被搜索当成还能打的单位（否则闸门一次次回 `no_attack_left`，每次问 ~4 s）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _cards import MY_SIDE, mk_card                              # noqa: E402
from engine.adapter import from_cards                            # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))


def unit(cid, attack_left):
    c = mk_card(cid, "local", "frontline", "infantry", 3, 3, 2, 1, "U%d" % cid)
    c.obj.attackLeft = attack_left
    return c


def main():
    sim = from_cards([unit(1, 0), unit(2, 1), unit(3, 2)], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    chk("attackLeft=0（已攻击/已移动）⇒ attacks_left=0、can_act 为假", sim.units[1].attacks_left == 0 and not sim.units[1].can_act())
    chk("attackLeft=1 ⇒ attacks_left=1、能动", sim.units[2].attacks_left == 1 and sim.units[2].can_act())
    chk("attackLeft=2（奋战首击后/奋战未击）⇒ 如实 2", sim.units[3].attacks_left == 2)
    c = mk_card(4, "local", "frontline", "infantry", 3, 3, 2, 1, "U4")        # 没有 attackLeft（读不到）⇒ 沿用默认
    sim = from_cards([c], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    chk("attackLeft 读不到 ⇒ 沿用旧默认（1）", sim.units[4].attacks_left == 1)
    c = mk_card(5, "local", "frontline", "infantry", 3, 3, 2, 1, "U5")        # pinned_turns>0（IsPinned）⇒ sim.pinned
    c.raw = dict(getattr(c, "raw", None) or {}, pinned_turns=2)
    c2 = mk_card(6, "local", "frontline", "infantry", 3, 3, 2, 1, "U6")
    c2.raw = dict(getattr(c2, "raw", None) or {}, pinned_turns=0)
    sim = from_cards([c, c2], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    chk("pinned_turns>0 ⇒ sim 里 pinned（不再白提 move_up）", sim.units[5].pinned is True and sim.units[6].pinned is False)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
