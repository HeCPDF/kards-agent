#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合结束的**消费点**测试：`boardeval.sim_turn_end` + `rule._d` 的接线。

口径（用户 2026-10-02）：eval = "打出这一步后游戏里会发生什么，就模拟什么" —— 候选终态其实是
"我停手、这一回合真正结束"时的盘面，所以要按 BP `ExecuteEndOfTurnQueue` 做两件事：
  ① 我方每张牌的 `OnEndOfTurn`（0x19）预计算后果（`Sim.event_fx["turn_end"]`）结算掉；
  ② 最后 `RemoveBuffsEndOfTurn`：`AddAttackUntilEndOfTurn` 的临时攻清零。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import evaluation                                              # noqa: E402
from policy.boardeval import H, U                               # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), **kw):
    return B.Sim({u.id: u for u in units}, {"local": 20, "enemy": 20}, 5.0, {}, **kw)


def main():
    # ---- ① 0x19 后果结算 ----
    s = mk([U(1, "local", "frontline", 2, 2, 2, "infantry")],
           event_fx={"turn_end": {1: {"kredit": 2, "damage_hq": 1}}})
    e = B.sim_turn_end(s)
    chk("sim_turn_end：结算该牌自己的 0x19 后果（+2 指挥点、对敌 HQ 1 伤）",
        e.kredits == 7.0 and e.hq["enemy"] == 19, "kred=%s hq=%s" % (e.kredits, e.hq["enemy"]))
    chk("没有 turn_end 表 ⇒ 状态不变（Hq/指挥点都不动）",
        (lambda x: x.kredits == 5.0 and x.hq["enemy"] == 20)(
            B.sim_turn_end(mk([U(1, "local", "frontline", 2, 2, 2, "infantry")]))))
    chk("只作用于表里那张牌（别的单位不受影响）",
        (lambda x: x.units[2].atk == 3)(B.sim_turn_end(
            mk([U(1, "local", "frontline", 2, 2, 2, "infantry"),
                U(2, "local", "frontline", 3, 3, 3, "infantry")],
               event_fx={"turn_end": {1: {"buff": [1, 0]}}}))[0] if False else
            B.sim_turn_end(mk([U(1, "local", "frontline", 2, 2, 2, "infantry"),
                               U(2, "local", "frontline", 3, 3, 3, "infantry")],
                              event_fx={"turn_end": {1: {"buff": [1, 0]}}}))))

    # ---- ② RemoveBuffsEndOfTurn：临时攻清零 ----
    t = mk([U(1, "local", "frontline", 2, 2, 2, "infantry")])
    B._apply_eff(t, {"attack_turn": 2}, 1)
    chk("前置：临时攻确实加上了（atk=4, atk_turn=2）", t.units[1].atk == 4 and t.units[1].atk_turn == 2)
    e2 = B.sim_turn_end(t)
    chk("sim_turn_end：临时攻清零（atk 回到 2、atk_turn=0）",
        e2.units[1].atk == 2 and e2.units[1].atk_turn == 0, "atk=%s" % e2.units[1].atk)

    # ---- 幂等：同一份终态结算两次不会翻倍 ----
    e3 = B.sim_turn_end(e2)
    chk("幂等：对已结算过的状态再调一次 ⇒ 不变（指挥点/临时攻都不再动）",
        e3.kredits == e2.kredits == 5.0 and e3.units[1].atk == 2 and e3.units[1].atk_turn == 0,
        "kred=%s" % e3.kredits)

    # ---- 换数据答案要变 ----
    big = B.sim_turn_end(mk([U(1, "local", "frontline", 2, 2, 2, "infantry")],
                            event_fx={"turn_end": {1: {"kredit": 5}}}))
    chk("换数据答案跟着变：+5 vs +2 指挥点", big.kredits == 10.0 and e.kredits == 7.0)

    # ---- 评估口径：终态含"回合结束" ⇒ Δ 与不含时不相等 ----
    base = mk([U(1, "local", "frontline", 2, 2, 2, "infantry")],
              event_fx={"turn_end": {1: {"kredit": 2}}})
    after = base.copy()
    d_with = evaluation.delta(base, B.sim_turn_end(after))
    d_without = evaluation.delta(base, after)
    chk("Δ 把回合结束算进去（+2 指挥点 ⇒ Δ 更大；kred_w=0 时相等也算通过）",
        d_with >= d_without, "%.3f vs %.3f" % (d_with, d_without))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
