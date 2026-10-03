#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""当回合常驻的“别的牌进场时”效果（JUNGLE FEVER：之后进场的己方单位 +2 攻 + 闪击）。

原版（card_event_sunny3_jungle_fever3.cpp）：`OnOtherCardEnterPlay` 守卫 `enterPlayOnTurn==本回合 ∧ 进场牌是己方在场单位`，
然后 `ChangeAttack(+2)` + `GiveBlitz`。sim：出这张指令登记 `enter_mods`，之后部署/生成的己方单位吃到，回合结束清空。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
from policy.boardeval import H                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    fever = {"buff": [2, 0], "give": ["blitz"]}
    hand = {1: H(1, "FEVER", 4, "order", 0, 0, (), {"enter_mod": fever}),
            2: H(2, "INF", 2, "infantry", 3, 3, (), {})}
    s = B.Sim({}, {"local": 20, "enemy": 20}, 8.0, hand, 8.0)
    before = B.sim_deploy(s, -5002, 3, 3, 2, "infantry", (), hand_id=2)
    chk("没打 FEVER：部署的单位原样（3/3、有部署病）", (before.units[-5002].atk, before.units[-5002].sick) == (3, True))
    s1 = B.sim_order(s, 1)
    chk("打出 FEVER 登记常驻效果，本身无其它变化", len(s1.enter_mods) == 1 and not s1.units and s1.kredits == 4)
    s2 = B.sim_deploy(s1, -5002, 3, 3, 2, "infantry", (), hand_id=2)
    u = s2.units[-5002]
    chk("之后部署的单位 +2 攻 + 闪击（无部署病）", (u.atk, u.sick, "blitz" in u.kw) == (5, False, True), str((u.atk, u.sick, sorted(u.kw))))
    chk("原状态不被改", len(s.enter_mods) == 0 and len(s1.enter_mods) == 1)
    s3 = B.sim_turn_end(s2)
    chk("回合结束清空常驻效果", s3.enter_mods == [])
    # 生成的己方单位同样吃到（OnOtherCardEnterPlay 对所有进场的牌触发）
    sp = B.Sim({}, {"local": 20, "enemy": 20}, 8.0, {}, 8.0, spawn_stats=lambda n: {"atk": 2, "dfn": 2, "cost": 2, "typ": "infantry", "kw": ()})
    sp.enter_mods = [fever]
    B._apply_eff(sp, {"spawn": 1, "spawn_cards": [{"name": "card_unit_x", "row": "back", "mine": True}]}, None)
    uu = next(iter(sp.units.values()))
    chk("生成的己方单位也吃到", uu.atk == 4 and "blitz" in uu.kw)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
