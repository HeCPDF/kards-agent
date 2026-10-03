#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上线族（0x32）测试：`triggers.run_move_frontline` 的顺序 + `sim_move` 的效果结算。

规格：本构建导出的 `BP_CardFunctions::ExecuteOnMoveToFrontlineCardEffects`（@15297，逐行读过）
—— 反制先手（`GetActiveGotchasOrdered`）→ 自己 `OnMoveToFrontline`（**未压制**才跑）→
隐蔽未揭示 ⇒ 0x32 段整段跳过 → 0x32 列表（`cardsDone` 去重）。

两半都测：
  * 顺序/去重/隐蔽/压制：把 `triggers._run_hook_ex` 换成记录器（不碰游戏、不碰 VM）；
  * 效果落地：`Sim.move_fx` + `sim_move`（纯函数），并钉"换数据答案要变"。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from policy.boardeval import U                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


# ------------------------------------------------------------------ 假进程（顺序测试用）
class _C:
    def __init__(self, name, loc, p, side="local", typ="infantry", gotcha=0, raw=None, sup=False):
        self.name, self.location, self.side, self.card_type = name, loc, side, typ
        self.gotcha_activated = gotcha
        self.is_suppressed = sup
        self.raw = {"ptr": p}
        if raw:
            self.raw.update(raw)
        self.keywords = []


class _S:
    def __init__(self, cards):
        self.cards = cards


class _K:
    pass


class _OA:                              # 假 ObjectArray：类 = 1000 + 指针
    def __init__(self, km):
        pass

    def class_of(self, p):
        return 1000 + p


class _TC(TR.TriggerCache):              # 假缓存：类 1001 两个钩子都有，1002 只有 0x32，1003 只有 0x32
    def __init__(self):
        self._fn = {}

    def fn_of_class(self, uc, hook=TR.HOOK):
        m = {1001: {"OnMoveToFrontline", "OnOtherCardMoveToFrontline"},
             1002: {"OnOtherCardMoveToFrontline"},
             1003: {"OnOtherCardMoveToFrontline"}}
        return 7 if hook in m.get(uc, ()) else 0


def run_chain(cards, moved, monkey):
    """换掉 `_run_hook_ex` 跑一遍 `run_move_frontline`，返回 (res, 调用记录)。"""
    calls = []

    def fake(km, c, hook, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw):
        calls.append((hook, getattr(c, "name", "?")))
        return {"ran": True, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old_hook = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = TR.run_move_frontline(_K(), _S(cards), moved, cache=_TC(), read_hooks={},
                                    my_side=1, hq_own=(), hq_enemy=(), enum_random=False)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old_hook
    return res, calls


def main():
    # ---------------- 顺序 / 去重 / 隐蔽 / 压制 ----------------
    mover = _C("A", "back", 1)                       # 自己（两个钩子都有）
    other = _C("B", "frontline", 2)                  # 0x32 旁观者
    gotcha = _C("G", "hand", 3, typ="gotcha", gotcha=5)   # 已挂上的反制（hand 也算）
    res, calls = run_chain([mover, other, gotcha], mover, None)
    chk("顺序：反制先手 → 自己 → 0x32 旁观者",
        calls == [("OnOtherCardMoveToFrontline", "G"), ("OnMoveToFrontline", "A"),
                  ("OnOtherCardMoveToFrontline", "B")], str(calls))
    chk("自己不会在 0x32 段被问第二遍（cardsDone 去重）",
        [c for c in calls if c[1] == "A"] == [("OnMoveToFrontline", "A")], str(calls))
    chk("meta 如实标注 StopFurtherActions 未建模",
        "stop_semantics" in res.get("meta", {}), str(res.get("meta")))
    chk("meta 里 covert_skip=False（正常单位）", res["meta"].get("covert_skip") is False)

    # 隐蔽未揭示 ⇒ 0x32 段整段跳过（自己的钩子照跑）
    covert = _C("A", "back", 1, raw={"keyword_flags": {"has_covert": True}, "is_revealed": False})
    res2, calls2 = run_chain([covert, other, gotcha], covert, None)
    chk("隐蔽未揭示：0x32 段整段跳过（只剩反制 + 自己）",
        calls2 == [("OnOtherCardMoveToFrontline", "G"), ("OnMoveToFrontline", "A")], str(calls2))
    chk("meta covert_skip=True", res2["meta"].get("covert_skip") is True)

    # 已揭示的隐蔽牌 ⇒ 不跳过
    revealed = _C("A", "back", 1, raw={"keyword_flags": {"has_covert": True}, "is_revealed": True})
    _res3, calls3 = run_chain([revealed, other], revealed, None)
    chk("隐蔽但已揭示 ⇒ 不跳过", [c[0] for c in calls3] == ["OnMoveToFrontline",
                                                          "OnOtherCardMoveToFrontline"], str(calls3))

    # 被压制 ⇒ 自己的钩子不跑（BP：!isSuppressed 才调）
    sup = _C("A", "back", 1, sup=True)
    _res4, calls4 = run_chain([sup, other], sup, None)
    chk("被压制：自己的 OnMoveToFrontline 不跑，0x32 照问",
        calls4 == [("OnOtherCardMoveToFrontline", "B")], str(calls4))

    # 被压制的旁观者：`FetchAllCardsWithEventTrigger` 会丢（除非在例外表里）
    sup_other = _C("B", "frontline", 2, sup=True)
    _res5, calls5 = run_chain([mover, sup_other], mover, None)
    chk("被压制的旁观者不进 0x32 列表（不在例外表）",
        calls5 == [("OnMoveToFrontline", "A")], str(calls5))

    # ---------------- 效果落地：Sim.move_fx + sim_move ----------------
    base = B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")},
                 {"local": 20, "enemy": 20}, 5.0, {}, event_fx={"move": {1: {"buff": [2, 0]}}})
    after = B.sim_move(base, 1)
    u = after.units[1]
    chk("上线：位置变前线 + 支付行动费 + 触发效果（+2 攻）",
        u.row == "frontline" and after.kredits == 4.0 and u.atk == 4,
        "atk=%s kred=%s（行动费 opc=1）" % (u.atk, after.kredits))
    chk("没有 move_fx 的同类局面 ⇒ 只有位移，没有加成",
        B.sim_move(B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")},
                         {"local": 20, "enemy": 20}, 5.0), 1).units[1].atk == 2)
    chk("换数据答案要变：+2/+0 vs +0/+0", after.units[1].atk == 4 and
        B.sim_move(B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")},
                         {"local": 20, "enemy": 20}, 5.0, {}, event_fx={"move": {1: {}}}), 1).units[1].atk == 2)
    chk("move_fx 只作用于对应的那张牌",
        B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry"),
               2: U(2, "local", "back", 2, 2, 2, "infantry")},
              {"local": 20, "enemy": 20}, 5.0, {}, event_fx={"move": {2: {"buff": [3, 0]}}}).units[1].atk == 2)
    chk("copy() 带 event_fx（move）",
        base.copy().event_fx["move"] == {1: {"buff": [2, 0]}})

    # 全局效果（抽牌）也走同一条路
    top = B.H(9, "TOP", 1, "order", eff={"_hold": 3.0})
    st = B.Sim({1: U(1, "local", "back", 2, 2, 2, "infantry")}, {"local": 20, "enemy": 20}, 5.0, {},
               deck=[9], deck_cards={9: top}, event_fx={"move": {1: {"draw": 1}}})
    chk("上线触发的全局效果（抽 1 张）也结算",
        len(B.sim_move(st, 1).hand) == 1)

    # ---------------- 常量/规格 ----------------
    chk("触发号 0x32 登记在规格里", TR.MOVE_HOOK_SPECS["OnOtherCardMoveToFrontline"]["trigger"] == 0x32)
    chk("自己的钩子不是触发号（own=True）", TR.MOVE_HOOK_SPECS["OnMoveToFrontline"]["own"] is True)
    chk("形参表与 BP 签名一致",
        TR.MOVE_HOOKS["OnOtherCardMoveToFrontline"] == ("cardMoved", "forceMove", "moveCost")
        and TR.MOVE_HOOKS["OnMoveToFrontline"] == ("forceMove", "moveCost"))
    # event_fx 的键必须登记在册（拼错名字不许静默无效）
    try:
        B.Sim({}, {"local": 20, "enemy": 20}, 5.0, {}, event_fx={"moves": {1: {}}})
        chk("未登记的 event_fx 种类必须报错", False, "静默通过了")
    except ValueError as e:
        chk("未登记的 event_fx 种类必须报错", True, str(e)[:30] + "…")
    chk("已登记的两种在册", set(B.EVENT_FX_KINDS) >= {"move", "draw"}, str(B.EVENT_FX_KINDS))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
