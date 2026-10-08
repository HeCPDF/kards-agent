#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽牌链迁移的特征测试（"迁移前后行为不变"，`EVAL-ARCHITECTURE.md` §1.1 补充 4）。

迁移：`sim.engine._res_eff` 里的 `while` 抽牌队列 → `sim/chain.py::draw_chain`
（事件队列驱动，FIFO；再抽只入队、其余效果交给 `_apply_eff`）。
本测试同时钉：
  * 已知牌顶 / 空库疲劳 / 未知牌序 三条路径的结果与旧实现一致；
  * 抵抗式"抽到时再抽"链的**抽牌顺序 = FIFO**（A、B、C 而不是 A、C、B）；
  * 上限截断（`DRAW_CAP`）如实记缺口；
  * "换数据答案要变"（换牌顶 ⇒ 手牌/持有价值跟着变）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
import sim.chain as C                                          # noqa: E402
from engine.state import DRAW_CAP, H, Sim, U                    # noqa: E402
from evaluation.value import W, hold_value                      # noqa: E402
from sim.engine import _apply_eff, _fatigue_once, _res_eff      # noqa: E402

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
    unit = U(1, ME, "back", 2, 2, 2, "infantry")

    # 1) 牌顶已知 ⇒ 抽到的是那张真牌（持有价值 = 它自己的 _hold）
    top = H(7, "TOP CARD", 2, "order", eff={"_hold": 5.0})
    s = mk([unit], deck=[7], deck_cards={7: top})
    _res_eff(s, {"draw": 1})
    got = list(s.hand.values())
    chk("牌顶已知：抽到那张牌本身（_hold=5.0）",
        len(got) == 1 and got[0].name == "TOP CARD" and hold_value(got[0]) == 5.0
        and s.hq[ME] == 20 and s.draws_done == 1, "hq=%s" % s.hq[ME])

    # 2) 空库（牌序已知）⇒ 游戏自己的疲劳规则：伤害=当前计数，然后 +1
    s2 = mk([unit], deck=[7], deck_cards={7: top}, fatigue=0)
    _res_eff(s2, {"draw": 2})
    chk("空库第 2 抽吃疲劳（计数 0 ⇒ 0 伤，计数→1）",
        s2.hq[ME] == 20 and s2.fatigue == 1 and len(s2.hand) == 1,
        "hq=%s fatigue=%s" % (s2.hq[ME], s2.fatigue))
    s3 = mk([unit], deck=[], fatigue=3)
    _res_eff(s3, {"draw": 2})
    chk("空库连抽：疲劳伤害随计数递增（3 → 4）",
        s3.hq[ME] == 20 - 3 - 4 and s3.fatigue == 5 and not s3.hand,
        "hq=%s fatigue=%s" % (s3.hq[ME], s3.fatigue))

    # 3) 牌序读不到 ⇒ 匿名牌（不假装知道）
    s4 = mk([unit])
    _res_eff(s4, {"draw": 2})
    chk("牌序未知 ⇒ 2 张匿名牌（持有价值 = draw_v）",
        len(s4.hand) == 2 and all(hold_value(h) == W["draw_v"] for h in s4.hand.values()),
        str([hold_value(h) for h in s4.hand.values()]))

    # 4) 抵抗式"抽到时再抽"：抽牌顺序必须与旧实现一致 = **FIFO**
    #    deck = [A(再抽1), B, C]；抽 2 张 ⇒ A、B、C（旧 while 队列的语义）
    a = H(11, "A", 1, "order", eff={"_on_draw": {"draw": 1, "kredit": 1}})
    b = H(12, "B", 1, "order", eff={})
    c = H(13, "C", 1, "order", eff={})
    s5 = mk([unit], deck=[11, 12, 13], deck_cards={11: a, 12: b, 13: c})
    _res_eff(s5, {"draw": 2})
    names = [h.name for h in s5.hand.values()]
    chk("抽到时再抽：顺序 = FIFO（A、B、C）且只再抽 1 张",
        names == ["A", "B", "C"] and s5.draws_done == 3 and not s5.deck,
        "names=%s draws=%d" % (names, s5.draws_done))
    chk("抽到时效果里**非抽牌部分**照常生效（kredit +1）",
        s5.kredits == 6.0 and s5.hq[ME] == 20, "kred=%s" % s5.kredits)
    chk("再抽的那 1 张没有被算两遍", len(s5.hand) == 3, str(len(s5.hand)))

    # 5) 上限：截断要如实记缺口（旧实现是静默丢）
    s6 = mk([unit], deck=[], fatigue=0)
    run = C.draw_chain(s6, 2, apply_effect=_apply_eff, hand_cap=W["hand_cap"],
                       anon_hold=W["draw_v"], cap=1)
    chk("cap 触顶 ⇒ Run 带缺口且 complete=False",
        (not run.complete) and any("触顶" in g for g in run.gaps), str(run.gaps))
    chk("cap 触顶时不多抽（draws_done 停在 cap）", s6.draws_done == 1, str(s6.draws_done))

    # 6) 单一常量来源：engine.state.DRAW_CAP 与 sim 的 cap 不许漂
    chk("DRAW_CAP 两处同值（防漂）", DRAW_CAP == C.DRAW_CAP,
        "%s vs %s" % (DRAW_CAP, C.DRAW_CAP))

    # 7) 换数据答案要变：换牌顶 ⇒ 手牌持有价值跟着变
    cheap = H(21, "CHEAP", 1, "order", eff={"_hold": 1.0})
    rich = H(22, "RICH", 1, "order", eff={"_hold": 9.0})
    x1 = mk([unit], deck=[21], deck_cards={21: cheap})
    x2 = mk([unit], deck=[22], deck_cards={22: rich})
    _res_eff(x1, {"draw": 1})
    _res_eff(x2, {"draw": 1})
    v1 = sum(hold_value(h) for h in x1.hand.values())
    v2 = sum(hold_value(h) for h in x2.hand.values())
    chk("换牌顶答案跟着变（1.0 vs 9.0）", (v1, v2) == (1.0, 9.0), "%s vs %s" % (v1, v2))

    # 8) 疲劳规则本体在 sim（`sim.engine` 只留旧名转调）
    y = mk([unit], fatigue=4)
    C.apply_fatigue(y)
    _fatigue_once(y)
    chk("apply_fatigue 与旧 _fatigue_once 同结果（4 伤→计数5，再 5 伤→计数6）",
        y.fatigue == 6 and y.hq[ME] == 20 - 4 - 5, "fatigue=%s hq=%s" % (y.fatigue, y.hq[ME]))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
