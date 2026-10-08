#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**上线（移动）**：费用与禁令（P3 ⑨，2026-10-04）—— 照原版 `MoveCardToFrontline` 逐行核。

原版 `BP_CardFunctions::MoveCardToFrontline`（`:17560-17629`）：
  * `:17609-17619` 先 `PayMovementCost(cardID)`（本体 `:19873-19898`）：
    `operationCost = card->getTotalOperationCost()`；**`kredits < operationCost` ⇒ 移动不发生**
    （`:17612-17613` return）；够才 `ChangeKreditsBySide(side, -cost)` + 记 `OperationKreditsSpent`
    ⇒ **移动费 = 总行动费**；
  * `:17590-17598` `CanMoveAndAttackInTheSameTurn()` 为假 ⇒ `attackLeft = 0`（步兵移动后不能攻击），
    为真 ⇒ `movementLeft -= 1`（装甲/带该能力的单位能移动后攻击）；
  * `:17603-17605` `ExecuteOnMoveToFrontlineCardEffects` + `ExecuteOnOperationKreditsSpent`（钩子）。

我们 `sim_move` 的 `s.kredits -= u.opc`（`u.opc` 来自适配器的 `o.getTotalOperationCost()`）与
`can_move_and_attack` 判据本来就对 ✓，**缺的是 `PayMovementCost` 的"够不够"闸门**（以前不够也照移、
指挥点变负）。每个判据都给两个不同结论的输入（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import state as S                                        # noqa: E402
import sim.engine as E                                               # noqa: E402

fails = 0
ME, OPP = 1, 2


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(kredits=3, opc=3, typ="infantry", row="back"):
    u = S.U(1, ME, row, 2, 5, 1, typ, opc=opc, rng=2)
    return S.Sim({1: u}, {ME: 20, OPP: 20}, kredits, {}, my_side=ME)


def main():
    # ① 够钱 ⇒ 移动 + 扣费 = 总行动费
    s = E.sim_move(mk(kredits=3, opc=3), 1)
    chk("kredits 3 == 行动费 3 ⇒ 移动成功、扣到 0",
        1 in s.units and s.units[1].row == "frontline" and s.kredits == 0,
        "row=%s kredits=%s" % (s.units[1].row if 1 in s.units else None, s.kredits))

    # ② 不够钱 ⇒ **移动不发生**（原版 `PayMovementCost` :19883-19888）
    s = E.sim_move(mk(kredits=2, opc=3), 1)
    chk("kredits 2 < 行动费 3 ⇒ **不移动**、指挥点不变（`PayMovementCost` 失败即 return）",
        1 in s.units and s.units[1].row == "back" and s.kredits == 2,
        "row=%s kredits=%s" % (s.units[1].row if 1 in s.units else None, s.kredits))

    # ③ 步兵（不能移动后攻击）⇒ attackLeft = 0；装甲 ⇒ 不受影响
    s = E.sim_move(mk(kredits=3, opc=0, typ="infantry"), 1)
    chk("步兵移动后 ⇒ `attacks_left = 0`", s.units[1].attacks_left == 0,
        "attacks_left=%s" % s.units[1].attacks_left)
    s = E.sim_move(mk(kredits=3, opc=0, typ="tank"), 1)
    chk("装甲（`CanMoveAndAttackInTheSameTurn` 真）⇒ 移动后**仍能攻击**",
        s.units[1].attacks_left == 1, "attacks_left=%s" % s.units[1].attacks_left)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
