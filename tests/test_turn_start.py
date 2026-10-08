#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**回合开始**（P3 ⑧，2026-10-04）—— 原版 `BP_Logic` 的回合开始流程 `:10010-10077`。

顺序（逐行读过，行号见 `sim/engine.py::sim_turn_start` 的 docstring）：
  ① 指挥点槽 +1（`CanSideGainKreditSlots`(`BP_Logic:7999`) ∧ `slot < MaxKreditsConst`）
  ② `SetKreditsAndKreditSlots(side, slot, slot, 0)`（`BP_CardFunctions:19543`）⇒ **指挥点补满到槽数**
  ③ `ExecuteBeforeStartOfTurnEvents()`  ④ `CanSideDrawCards` ⇒ 抽 1（**第 1 回合不抽**，`:6318-6336`）
  ⑤ `GiveMobilizeBonus()`  ⑥ `ExecuteStartOfTurnEvents(turnNumber)`

以前 `SIM-FIDELITY:33` 写的是"指挥点补给、抽牌、疲劳**按经验**"—— 抽牌/疲劳其实早就是原生移植
（`engine/chain.py::draw_chain`、`engine/natives/damage.py::apply_fatigue`），只有**补给**这一条没落。
每个判据都给两个会产生不同结论的输入（弯路 #11）。
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


def mk(slots=3, kredits=0, fatigue=0, deck=()):
    return S.Sim({}, {ME: 20, OPP: 20}, kredits, {}, my_side=ME, slots=slots,
                 fatigue=fatigue, deck=list(deck))


def main():
    # ① 槽 +1、指挥点补满到槽数
    s = mk(slots=3, kredits=0)
    s2 = E.sim_turn_start(s, turn_number=2)
    chk("槽 3→4、指挥点 0→**4**（补满到槽数，不是 +1 点）",
        s2.slots == 4 and s2.kredits == 4, "slots=%s kredits=%s" % (s2.slots, s2.kredits))

    # ② 对照：槽已在上限
    s = mk(slots=12, kredits=5)
    s2 = E.sim_turn_start(s, turn_number=2)
    chk("槽到上限 12 ⇒ 不再 +1；指挥点仍补满到 12",
        s2.slots == 12 and s2.kredits == 12, "slots=%s kredits=%s" % (s2.slots, s2.kredits))

    # ③ 中途用掉指挥点：回合开始**补满**（不是加固定值）；同时槽照常 +1
    s = mk(slots=6, kredits=1)
    s2 = E.sim_turn_start(s, turn_number=2)
    chk("槽 6→7、手上只剩 1 点 ⇒ 回合开始**补满到 7**（`SetKreditsAndKreditSlots(side, slot, slot)`；"
        "不是「+1 点」）",
        s2.slots == 7 and s2.kredits == 7, "slots=%s kredits=%s" % (s2.slots, s2.kredits))

    # ④ 第 1 回合**不抽牌**（`CanSideDrawCards`）；第 2 回合抽（空库 ⇒ 走疲劳链）
    s1 = mk(slots=1, kredits=0, fatigue=0)
    t1 = E.sim_turn_start(s1, turn_number=1)
    chk("第 1 回合：**不抽牌**（空库也不会走疲劳链）⇒ 疲劳计数仍是 0",
        getattr(t1, "fatigue", None) == 0, "fatigue=%s" % getattr(t1, "fatigue", None))
    s2_ = mk(slots=1, kredits=0, fatigue=0)
    t2 = E.sim_turn_start(s2_, turn_number=2)
    chk("第 2 回合：抽 1 张（空库 ⇒ 疲劳链跑了一次，计数 0→1）",
        getattr(t2, "fatigue", None) == 1, "fatigue=%s" % getattr(t2, "fatigue", None))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
