#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽到族（0x2A 旁观者段）测试：`triggers.run_drawn_from_deck` + `draw_chain` 的接线。

规格：本构建导出的 `BP_CardFunctions::ExecuteOnDrawnFromDeck`（@13783，逐行读过）——
抽到那张自己的 `OnCardDrawnFromDeck` 先跑 → 0x2A 列表里 **cardID != DrawnCardID** 的逐个
`OnOtherCardDrawnFromDeck(DrawnCardID, StartOfTurnDraw, DrawnSide)`（自己跳过）。
评估侧：抽到那张自己的钩子由既有 `_on_draw` 摘要承担；本轮补旁观者，效果按"抽到的卡 id"预计算。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from policy.boardeval import H, U                               # noqa: E402
from sim.chain import draw_chain                               # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _C:
    def __init__(self, name, loc, p, cid, side="local", sup=False):
        self.name, self.location, self.side, self.card_id = name, loc, side, cid
        self.is_suppressed = sup
        self.raw = {"ptr": p}
        self.keywords = []


class _S:
    def __init__(self, cards):
        self.cards = cards


class _K:
    pass


class _OA:
    def __init__(self, km):
        pass

    def class_of(self, p):
        return 1000 + p


class _TC(TR.TriggerCache):
    """类 1002/1004 有 0x2A；类 1001 没有。"""

    def __init__(self):
        self._fn = {}

    def fn_of_class(self, uc, hook=TR.HOOK):
        return 7 if (uc in (1002, 1004) and hook == "OnOtherCardDrawnFromDeck") else 0


def run_drawn(cards, drawn_id):
    calls = []

    def fake(km, c, hook, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw):
        calls.append((hook, getattr(c, "name", "?"), dict(args)))
        return {"ran": True, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old_hook = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = TR.run_drawn_from_deck(_K(), _S(cards), drawn_id, start_of_turn_draw=True,
                                     drawn_side=1, cache=_TC(), read_hooks={}, my_side=1,
                                     hq_own=(), hq_enemy=(),
                                     enum_random=False)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old_hook
    return res, calls


def main():
    # ---------------- 旁观者段的顺序 / 自己跳过 ----------------
    drawn = _C("DRAWN", "hand", 5, 77)              # 抽到的那张自己（在 0x2A 名单里也要跳过）
    watcher1 = _C("W1", "frontline", 2, 11)
    watcher2 = _C("W2", "hand", 4, 12)
    res, calls = run_drawn([drawn, watcher1, watcher2], 77)
    chk("0x2A：只问旁观者、抽到的那张自己跳过（cardID==DrawnCardID）",
        [c[1] for c in calls] == ["W1", "W2"], str(calls))
    chk("形参名与 BP 一致（drawnCardID/StartOfTurnDraw/drawnSide）",
        calls[0][2] == {"drawnCardID": 77, "StartOfTurnDraw": True, "drawnSide": 1}, str(calls[0][2]))
    chk("手牌里的旁观者也看（fetch 不看位置）", "W2" in [c[1] for c in calls])
    chk("触发号 0x2A 登记在规格里", TR.DRAW_HOOK_SPECS["OnOtherCardDrawnFromDeck"]["trigger"] == 0x2A)
    chk("自己那条是 own=True（评估里由 _on_draw 承担，见 docstring）",
        TR.DRAW_HOOK_SPECS["OnCardDrawnFromDeck"]["own"] is True
        and TR.DRAW_HOOK_SPECS["OnCardDrawnFromDeck"]["trigger"] is None)

    # 换一张被抽的牌 ⇒ 传进钩子的 id 跟着变（不是写死）
    _res2, calls2 = run_drawn([watcher1], 12)
    chk("换数据答案要变：drawnCardID 跟着换", calls2[0][2]["drawnCardID"] == 12, str(calls2[0][2]))

    # ---------------- draw_chain 的接线（纯函数） ----------------
    top = H(7, "TOP", 1, "order", eff={})
    def mk(**kw):
        return B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")},
                     {"local": 20, "enemy": 20}, 5.0, {}, deck=[7], deck_cards={7: top}, **kw)

    s0 = mk()
    B._res_eff(s0, {"draw": 1})
    chk("没有 draw_fx ⇒ 抽牌行为不变", next(iter(s0.hand.values())).name == "TOP" and s0.kredits == 5.0)

    s1 = mk(event_fx={"draw": {7: {"kredit": 1}}})
    B._res_eff(s1, {"draw": 1})
    chk("旁观者效果结算（抽到 7 ⇒ 指挥点 +1）", s1.kredits == 6.0, "kred=%s" % s1.kredits)

    s2 = mk(event_fx={"draw": {8: {"kredit": 1}}})          # 键对不上抽到的那张
    B._res_eff(s2, {"draw": 1})
    chk("键对不上 ⇒ 不生效（只有抽到 7 时才算）", s2.kredits == 5.0, "kred=%s" % s2.kredits)

    # 旁观者也会"再抽"：只入队、不双算
    a = H(11, "A", 1, "order", eff={})
    b = H(12, "B", 1, "order", eff={})
    s3 = B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")}, {"local": 20, "enemy": 20}, 5.0, {},
               deck=[11, 12], deck_cards={11: a, 12: b},
               event_fx={"draw": {11: {"draw": 1}, 12: {"draw": 1}}})
    run = draw_chain(s3, 1, apply_effect=B._apply_eff, hand_cap=B.W["hand_cap"],
                     anon_hold=B.W["draw_v"], bystander_fx=s3.event_fx["draw"].get)
    names = [h.name for h in s3.hand.values()]
    # 链条：抽 A → A 的旁观者"再抽 1" → 抽 B → B 的旁观者也"再抽 1" → 牌库空 ⇒ 疲劳（0 伤、计数→1）
    chk("旁观者的'再抽 1 张'入队，连到牌库空 ⇒ 疲劳（不双算、不死循环）",
        names == ["A", "B"] and s3.draws_done == 3 and s3.fatigue == 1 and run.complete,
        "names=%s draws=%s fatigue=%s" % (names, s3.draws_done, s3.fatigue))
    chk("旁观者效果不与自己那条混算（_on_draw 为空时才只有旁观者）",
        all(h.eff.get("_on_draw") is None for h in s3.hand.values()))

    # 自己先、旁观者后：两条都改指挥点，顺序不影响结果；用"再抽"验顺序（自己先入队）
    a2 = H(21, "A2", 1, "order", eff={"_on_draw": {"draw": 1}})
    b2 = H(22, "B2", 1, "order", eff={})
    s4 = B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")}, {"local": 20, "enemy": 20}, 5.0, {},
               deck=[21, 22], deck_cards={21: a2, 22: b2}, event_fx={"draw": {21: {"kredit": 2}}})
    B._res_eff(s4, {"draw": 1})
    chk("自己的 _on_draw 与旁观者都生效（再抽 1 + 指挥点 +2）",
        len(s4.hand) == 2 and s4.kredits == 7.0, "hand=%d kred=%s" % (len(s4.hand), s4.kredits))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
