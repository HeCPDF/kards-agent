#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`card_targets.py::make_natives()` 与游戏真值的对齐断言（离线，不需要游戏）。

依据 `NATIVE-COVERAGE-1.60.md` §14.2 #2/#5/#6/#21 与 §7#6：类型 getter 的 `isAlso*`、
`getTotalOperationCost` 的 buff/下限、`getTotalKreditCost` 的下限、EnumCompare 的 0/1 语义。
每条都换一张卡（换数据），答案要跟着变（CLAUDE.md 弯路 #11）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys

sys.path.insert(0, r"D:\Kards\kards-agent")

from tools.card_targets import make_natives                              # noqa: E402

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


class FakeVM:
    def __init__(self, card):
        self.card = card
        self.env = {}


def call(N, name, card, vals=None, outvar="CallFunc_Out"):
    vm = FakeVM(card)
    ret = N[name](vm, [outvar], list(vals or []))
    return ret, vm.env.get(outvar)


def card(**kw):
    c = {"card_type": "infantry", "faction": "USA", "rarity": "Common",
         "attack": 2, "defense": 2, "kredit_cost": 2, "kredit": 2,
         "operation_cost": 0, "operation_cost_buff": 0, "custom_name1": "",
         "is_suppressed": False, "is_reserved": False}
    c.update(kw)
    return c


def main():
    N = make_natives()

    print("== A. EnumCompare*：0=Equal / 1=NotEqual（旧写法是反的） ==")
    chk("EnumCompareType(坦克, Tank) = 0", call(N, "UFunctionLibrary::EnumCompareType",
                                                card(card_type="tank"), [None, "ETypeEnum::tank"]),
        (0, 0))
    chk("EnumCompareType(步兵, Tank) = 1", call(N, "UFunctionLibrary::EnumCompareType",
                                                card(card_type="infantry"), [None, "ETypeEnum::tank"]),
        (1, 1))
    chk("EnumCompareFaction(USA, USA) = 0", call(N, "UFunctionLibrary::EnumCompareFaction",
                                                 card(faction="USA"), [None, "EFactionEnum::USA"]),
        (0, 0))
    chk("EnumCompareRarity(Common, Limited) = 1", call(N, "UFunctionLibrary::EnumCompareRarity",
                                                       card(rarity="Common"), [None, "Limited"]),
        (1, 1))

    print("== B. 类型 getter 的 isAlso*（意大利骑兵 / MakeCountAsTank） ==")
    cav = card(custom_name1="isAlsoTank;CanMoveAndAttackInTheSameTurn")
    chk("IsTank(步兵+isAlsoTank)", call(N, "IsTank", cav)[0], True)
    chk("IsTank(纯步兵)", call(N, "IsTank", card())[0], False)
    chk("IsTank(坦克)", call(N, "IsTank", card(card_type="tank"))[0], True)
    chk("IsFighter(同一张骑兵卡) 不受影响", call(N, "IsFighter", cav)[0], False)
    chk("IsFighter(fighter+isAlsoFighter)", call(N, "IsFighter",
                                                 card(card_type="fighter",
                                                      custom_name1="isAlsoFighter"))[0], True)
    chk("前缀不算属性（整段相等）", call(N, "IsTank", card(custom_name1="isAlsoTa"))[0], False)

    print("== C. 行动费 / 费用（buff 与上下限） ==")
    chk("getTotalOperationCost(2,-5) = 0", call(N, "getTotalOperationCost",
                                                card(operation_cost=2, operation_cost_buff=-5))[0], 0)
    chk("getTotalOperationCost(2,+3) = 5", call(N, "getTotalOperationCost",
                                                card(operation_cost=2, operation_cost_buff=3))[0], 5)
    chk("getTotalOperationCost(2,无 buff 字段) = 2",
        call(N, "getTotalOperationCost", {"card_type": "infantry", "operation_cost": 2})[0], 2)
    chk("getTotalKreditCost(-2) = 0", call(N, "getTotalKreditCost", card(kredit_cost=-2))[0], 0)
    chk("getTotalKreditCost(150) = 99", call(N, "getTotalKreditCost", card(kredit_cost=150))[0], 99)
    chk("getTotalKreditCost(5) = 5", call(N, "getTotalKreditCost", card(kredit_cost=5))[0], 5)

    print("%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
