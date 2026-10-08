#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同回合计价的断言（汇总报告 §8 第 4 项 / TODO A1⑤）。

口径（`EVAL-ARCHITECTURE.md` §3.4 + 弯路 #38）：
  * **重复牌**（效果已存在于场上同一状态）⇒ Δ≈0 记 0；
  * **取不到效果**（缺口）⇒ 价值 0，**不按费用估**（不许"贵所以有价值"）；
  * 有真实状态变化（比如给一个还没有该关键词的单位贴上关键词）⇒ Δ > 0。

实现上这三条是 `ΔV = V(终态) − V(起点)` 的**自然结果**，不需要评估侧另写规则
（评估侧的规则越少越好，§3.1）。本测试的作用是把它**钉住**：
谁要是往评估里加了"按费用补价值"这类东西，这里会红。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
import evaluation                                              # noqa: E402
from engine.state import Sim, U                                 # noqa: E402
from sim.engine import _apply_eff, sim_attack                   # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), hand=(), kred=5.0, hq=(20, 20), **kw):
    return Sim({u.id: u for u in units}, {ME: hq[0], OPP: hq[1]}, kred,
                 {c.id: c for c in hand}, **kw, my_side=ME)


def main():
    # ---- 1) 重复牌：单位已经有这个关键词 ⇒ Δ == 0 ----
    #    （用 `guard`：它在 KW_SCORE 里、本身有价值 0.5 ⇒ 这条断言才有信息量）
    st = mk([U(1, ME, "frontline", 3, 3, 3, "infantry", kw=("guard",))])
    end = st.copy()
    _apply_eff(end, {"give": ["guard"]}, 1)
    d = evaluation.delta(st, end)
    chk("重复关键词（已有 guard 再给 guard）⇒ Δ == 0", d == 0.0, "%.4f" % d)
    chk("重复关键词不产生缺口", not end.gaps, str(end.gaps))

    # ---- 2) 真变化：给一个没有该关键词的单位 ⇒ Δ > 0（换数据答案跟着变）----
    st2 = mk([U(1, ME, "frontline", 3, 3, 3, "infantry")])
    end2 = st2.copy()
    _apply_eff(end2, {"give": ["guard"]}, 1)
    d2 = evaluation.delta(st2, end2)
    chk("同一效果、换成没有 guard 的单位 ⇒ Δ > 0（= kw_guard 0.5）", d2 > 0, "%.4f" % d2)

    # ---- 3) 取不到效果（缺口）⇒ 价值 0，不按费用估 ----
    st3 = mk([U(1, ME, "frontline", 3, 3, 3, "infantry")])
    end3 = st3.copy()
    end3.kredits = 0.0                                   # 假设这是一张 6 费牌打出去（费用已扣）
    _apply_eff(end3, {"convert": True}, 1)             # ConvertCard 未确认 ⇒ 只记缺口
    d3 = evaluation.delta(st3, end3)
    chk("缺口效果（convert）⇒ 价值仍是 0（不因'费用高'补价值）",
        d3 == 0.0 and any("ConvertCard" in g for g in end3.gaps), "%.4f / %s" % (d3, end3.gaps))

    # ---- 4) 空效果（VM 跑完但什么都没有）⇒ Δ == 0，且不算缺口 ----
    st4 = mk([U(1, ME, "frontline", 3, 3, 3, "infantry")])
    end4 = st4.copy()
    chk("空效果 ⇒ Δ == 0", evaluation.delta(st4, end4) == 0.0)

    # ---- 5) fight 的近似结算**记缺口**，但价值只按真实状态变化算 ----
    st5 = mk([U(1, ME, "frontline", 3, 3, 3, "infantry"),
              U(2, OPP, "frontline", 2, 2, 2, "infantry")])
    end5 = st5.copy()
    _apply_eff(end5, {"fight": [1, 2]}, None)
    d5 = evaluation.delta(st5, end5)
    chk("fight 换掉一个 2/2 ⇒ Δ > 0 且带缺口", d5 > 0 and end5.gaps, "%.4f" % d5)

    # ---- 6) 同一份效果作用两次：第二次 Δ == 0（幂等的那类）----
    st6 = mk([U(1, ME, "frontline", 3, 3, 3, "infantry")])
    a = st6.copy()
    _apply_eff(a, {"give": ["guard"]}, 1)
    b = a.copy()
    _apply_eff(b, {"give": ["guard"]}, 1)
    chk("给同一个关键词第二次 ⇒ 第二次 Δ == 0", evaluation.delta(a, b) == 0.0)

    # ---- 7) 热浪（AddAttackUntilEndOfTurn）：**本回合还能打才算价值**（用户 2026-10-02 报的场景）----
    #   还能打（attacks_left=1）⇒ 临时 +1 攻有分；已经打完（attacks_left=0，acted=True）⇒ 0 分。
    fresh = mk([U(1, ME, "frontline", 2, 2, 2, "infantry")])
    e_fresh = fresh.copy()
    _apply_eff(e_fresh, {"attack_turn": 1}, 1)
    d_fresh = evaluation.delta(fresh, e_fresh)
    chk("热浪：单位**还能打**时，本回合 +1 攻有正价值（%.3f > 0）" % d_fresh, d_fresh > 0)
    chk("  且模拟里真的加到了 atk 上（本回合的攻击吃得到）", e_fresh.units[1].atk == 3)
    chk("  临时量被单独记住（atk_turn=1）", e_fresh.units[1].atk_turn == 1)

    done = mk([U(1, ME, "frontline", 2, 2, 2, "infantry", acted=True)])
    e_done = done.copy()
    _apply_eff(e_done, {"attack_turn": 1}, 1)
    d_done = evaluation.delta(done, e_done)
    chk("热浪：单位**本回合已经打完**时，本回合 +1 攻 = 0 价值（贴膜无后续攻击记 0）",
        d_done == 0.0, "%.4f" % d_done)

    pinned = mk([U(1, ME, "frontline", 2, 2, 2, "infantry", pinned=True)])
    e_pin = pinned.copy()
    _apply_eff(e_pin, {"attack_turn": 2}, 1)
    chk("热浪：被压制的单位 ⇒ 临时攻击力同样 0 价值",
        evaluation.delta(pinned, e_pin) == 0.0)

    # 换数据答案要变：+1 vs +3 攻（还能打时）
    e3 = fresh.copy()
    _apply_eff(e3, {"attack_turn": 3}, 1)
    chk("换数据答案要变：临时 +3 攻的 Δ 大于 +1 攻",
        evaluation.delta(fresh, e3) > d_fresh)

    # ---- 8) 顺序：**先贴后打 > 先打后贴**（用户 2026-10-02："先打后贴也是不合适的行为"；
    #         例外是"贴完就打不了"——那由模拟里 '贴完 unit 还能不能动' 自然处理）----
    foe = U(9, OPP, "frontline", 5, 5, 5, "infantry")
    order_base = mk([U(1, ME, "frontline", 2, 2, 2, "infantry"), foe])
    buff_first = order_base.copy()
    _apply_eff(buff_first, {"attack_turn": 2}, 1)
    buff_first = sim_attack(buff_first, 1, 9)
    atk_first = sim_attack(order_base.copy(), 1, 9)
    _apply_eff(atk_first, {"attack_turn": 2}, 1)
    d_buff_first = evaluation.delta(order_base, buff_first)
    d_atk_first = evaluation.delta(order_base, atk_first)
    chk("先贴后打 > 先打后贴（%.3f > %.3f）" % (d_buff_first, d_atk_first),
        d_buff_first > d_atk_first)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
