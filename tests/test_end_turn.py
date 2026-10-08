#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ForceEndTurn`（强制结束当前回合）的离线断言（不需要游戏）。

触发者（1.60 导出）：久留米联队（`card_unit_kurume_regiment`，"Destruction: End the turn."）、
`card_event_banzai_charge` / `card_event_repel_the_attack` / `card_event_protect_the_pocket` /
`card_event_calm_before_the_storm`。
链路：卡 → `cardFunction->ForceEndTurn()` → `CardFunctionsNotifier->NotifyForceEndTurn(true)`
（**不经过 `CanEndTurn`**）⇒ eval 语义 = 行动权交给对方 ⇒ `playing_side="enemy"`。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys


from _cards import MY_SIDE, ME, OPP, mk_card                      # noqa: E402
from engine.adapter import from_cards                     # noqa: E402
from policy.search import gen_actions                     # noqa: E402
from sim.engine import _apply_eff                         # noqa: E402
from semantics.effectvm import Recorder, to_effects          # noqa: E402

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def C():
    """最小卡对象（够 from_cards 建一个"后排步兵"）。"""
    return mk_card(1, "local", "back", "infantry", 2, 2, 2, 1, "X")


def main():
    print("== A. 记录层：ForceEndTurn ⇒ playing_side=enemy ==")
    r = Recorder(0x2000, 0x1000)
    r.hook("ForceEndTurn")(None, None, None, [], None)
    e = to_effects(r, my_side=1)
    chk("ForceEndTurn 记成 playing_side=对方座位", e.get("playing_side"), OPP)

    print("== B. 消费层：playing_side=enemy ⇒ 我方不再有动作（sim.engine.gen_actions） ==")
    sim = from_cards([C()], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    chk("基线：后排步兵至少有一个动作（上线）", len(gen_actions(sim)) >= 1, True)
    _apply_eff(sim, {"playing_side": OPP}, None)
    chk("结束回合后 gen_actions 为空", gen_actions(sim), [])
    sim2 = from_cards([C()], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    _apply_eff(sim2, {"playing_side": ME}, None)
    chk("playing_side=local 不受影响", len(gen_actions(sim2)) >= 1, True)

    print("%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
