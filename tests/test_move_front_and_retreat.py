#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离开前线（0x31）/ 撤退（0x36）两族的顺序与"叫停"测试。

规格 = 本构建 BP 导出（逐行读过）：
  * `ExecuteOnCardMoveFromFrontline(cardMoved)`@14281：未压制才跑自己的 `OnMoveFromFrontline()`（无参）
    → fetch 0x31 逐张 `OnOtherCardMoveFromFrontline(cardMoved)`，**cardID==自己 的跳过**；
  * `ApplyMakeCardRetreat`@16567：未压制才跑 `OnBeforeRetreat(&stopAction)`（true ⇒ 直接 return）
    → fetch 0x36 逐张 `OnOtherCardRetreat(card,&stopAction)`（**不排除自己**；true ⇒ 记日志并 return）。
评估侧：`{"retreat": True}` 时先结算 `event_fx["retreat"][卡 id]` 再让单位离场。
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


class _C:
    def __init__(self, name, p, cid=0, sup=False, loc="frontline", side="local"):
        self.name, self.card_id, self.location, self.side = name, cid, loc, side
        self.is_suppressed = sup
        self.raw = {"ptr": p, "custom_name1": ""}
        self.keywords = []
        self.card_type = "infantry"


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
    def __init__(self):
        self._fn = {}

    def fn_of_class(self, uc, hook=TR.HOOK):
        m = {1001: {"OnMoveFromFrontline", "OnBeforeRetreat"},
             1002: {"OnOtherCardMoveFromFrontline"},
             1003: {"OnOtherCardRetreat"},
             1004: {"OnOtherCardMoveFromFrontline", "OnOtherCardRetreat"}}
        return 7 if hook in m.get(uc, ()) else 0


def run(fn, cards, card, stops=()):
    """跑 run_move_from_frontline / run_retreat；`stops` 里的名字会让该钩子的 out.stopAction=True。"""
    calls = []

    def fake(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw):
        nm = getattr(c, "name", "?")
        calls.append((h, nm, dict(args)))
        out = {"stopAction": True} if nm in stops else {}
        return {"ran": True, "eff": {}, "out": out, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = fn(_K(), _S(cards), card, cache=_TC(), read_hooks={}, my_side=1,
                 hq_own=(), hq_enemy=(), enum_random=False)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old
    return res, calls


def main():
    me = _C("ME", 1, 11)                       # 自己：两个"自己的"钩子都覆写
    w31 = _C("W31", 2, 12)                     # 只看 0x31
    w36 = _C("W36", 3, 13)                     # 只看 0x36
    both = _C("BOTH", 4, 14)                   # 两条旁观者钩子都覆写

    # ---------------- 0x31 离开前线 ----------------
    res31, calls31 = run(TR.run_move_from_frontline, [me, w31, both], me)
    seq = [(h, n) for h, n, _a in calls31]
    chk("0x31 顺序：自己 OnMoveFromFrontline（无参）→ 0x31 旁观者（排除自己）",
        seq == [("OnMoveFromFrontline", "ME"), ("OnOtherCardMoveFromFrontline", "W31"),
                ("OnOtherCardMoveFromFrontline", "BOTH")], str(seq))
    chk("0x31 自己那条无参、旁观者带 cardMoved=自己",
        calls31[0][2] == {} and calls31[1][2] == {"cardMoved": 1}, str(calls31[:2]))
    chk("0x31 meta 如实标 GetStopFurtherActions 未建模",
        "未建模" in res31["meta"]["stop_semantics"] and res31["meta"]["own_ran"] is True,
        str(res31["meta"]))
    sup31 = _C("SUP", 1, 15, sup=True)
    _r, calls31b = run(TR.run_move_from_frontline, [sup31, w31], sup31)
    chk("0x31 被压制 ⇒ 自己那条不跑，0x31 照跑",
        [h for h, _n, _a in calls31b] == ["OnOtherCardMoveFromFrontline"], str(calls31b))

    # ---------------- 0x36 撤退 ----------------
    res36, calls36 = run(TR.run_retreat, [me, w36, both], me)
    chk("0x36 顺序：自己 OnBeforeRetreat → 0x36 列表（不排除自己，但自己没覆写 0x36）",
        [(h, n) for h, n, _a in calls36] == [("OnBeforeRetreat", "ME"),
                                             ("OnOtherCardRetreat", "W36"),
                                             ("OnOtherCardRetreat", "BOTH")], str(calls36))
    chk("0x36 形参 cardRetreated=被撤退那张、meta 无叫停",
        calls36[1][2] == {"cardRetreated": 1} and res36["meta"]["stopped_by"] is None,
        str(res36["meta"]))

    # 自己那条叫停 ⇒ 后面 0x36 一次都不问（BP：直接 return）
    res_stop1, calls_stop1 = run(TR.run_retreat, [me, w36], me, stops=("ME",))
    chk("自己的 OnBeforeRetreat 返回 stopAction ⇒ 0x36 不再问（BP 直接 return）",
        [(h, n) for h, n, _a in calls_stop1] == [("OnBeforeRetreat", "ME")]
        and res_stop1["meta"]["stopped_by"] == "OnBeforeRetreat", str(res_stop1["meta"]))

    # 0x36 某张叫停 ⇒ 后面的不再问
    res_stop2, calls_stop2 = run(TR.run_retreat, [me, w36, both], me, stops=("W36",))
    chk("0x36 某张返回 stopAction ⇒ 中止（后面的不再问）",
        [(h, n) for h, n, _a in calls_stop2] == [("OnBeforeRetreat", "ME"),
                                                 ("OnOtherCardRetreat", "W36")]
        and res_stop2["meta"]["stopped_by"].startswith("OnOtherCardRetreat"), str(res_stop2["meta"]))

    # ---------------- 规格表 ----------------
    chk("触发号登记：0x31 / 0x36",
        TR.MOVE_FRONT_HOOK_SPECS["OnOtherCardMoveFromFrontline"]["trigger"] == 0x31
        and TR.RETREAT_HOOK_SPECS["OnOtherCardRetreat"]["trigger"] == 0x36)
    chk("两个自己那条都是 own=True、无触发号",
        TR.MOVE_FRONT_HOOK_SPECS["OnMoveFromFrontline"]["own"] is True
        and TR.MOVE_FRONT_HOOK_SPECS["OnMoveFromFrontline"]["trigger"] is None
        and TR.RETREAT_HOOK_SPECS["OnBeforeRetreat"]["own"] is True)
    chk("出参登记：0x36 两条都有 stopAction",
        TR.RETREAT_HOOK_SPECS["OnBeforeRetreat"]["outs"] == ("stopAction",)
        and TR.RETREAT_HOOK_SPECS["OnOtherCardRetreat"]["outs"] == ("stopAction",))

    # ---------------- 评估侧：撤退前结算 0x36 后果 ----------------
    u = U(9, "enemy", "frontline", 2, 2, 2, "infantry")
    s = B.Sim({9: u}, {"local": 20, "enemy": 20}, 5.0, {}, event_fx={"retreat": {9: {"kredit": 2}}})
    B._apply_eff(s, {"retreat": True}, 9)
    chk("撤退：单位离场 + 0x36 后果结算（+2 指挥点）",
        9 not in s.units and s.kredits == 7.0, "kred=%s" % s.kredits)
    chk("event_fx 登记了 retreat 种类", "retreat" in B.EVENT_FX_KINDS)
    chk("换数据答案要变：没有 retreat 后果表 ⇒ 只离场不加费",
        (lambda s2: (B._apply_eff(s2, {"retreat": True}, 9), 9 not in s2.units
                     and s2.kredits == 5.0)[1])(
            B.Sim({9: U(9, "enemy", "frontline", 2, 2, 2, "infantry")},
                  {"local": 20, "enemy": 20}, 5.0, {})))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
