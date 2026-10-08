#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上线后能否同回合攻击：照原版 `CanMoveAndAttackInTheSameTurn`（装甲单位 或 customName1 带该属性）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _cards import ME, OPP                              # noqa: E402
from sim.state import Sim, U                                   # noqa: E402
from sim import engine as E                                           # noqa: E402
from player.rule import RuleV2                                         # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += (not ok)


def after_move(typ, kw=()):
    s = Sim({1: U(1, ME, "back", 3, 3, 3, typ, kw)}, {ME: 20, OPP: 20}, 5.0, {}, 5.0, my_side=ME)
    return E.sim_move(s, 1).units[1]


chk("步兵上线后不能攻击", after_move("infantry").attacks_left == 0)
chk("坦克上线后仍可攻击", after_move("tank").attacks_left == 1)
chk("掷弹兵/骑兵类（move_attack 属性）上线后仍可攻击", after_move("infantry", ("move_attack",)).attacks_left == 1)


class C:
    keywords = []
    raw = {"custom_name1": "isAlsoTank;CanMoveAndAttackInTheSameTurn"}


class D:
    keywords = []
    raw = {"custom_name1": "isAlsoTank"}


chk("rule 从 customName1 识别 move_attack", RuleV2._move_attack_kw(C) == {"move_attack"})
chk("没有该属性则不加", RuleV2._move_attack_kw(D) == frozenset())
print("失败 %d 项" % fails)
sys.exit(1 if fails else 0)
