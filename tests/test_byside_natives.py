#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BySide 族原语的断言（离线）。每条都换 side / 换局面，答案必须跟着变（同类教训）。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
from kardsmem.cardnatives import APPROX, CardNatives
from kardsmem.kismetlib import Unimplemented


class B:
    def __init__(self, our_turn=True, kredits=None, slots=None, max_kredits=None):
        self.our_turn = our_turn
        # 座位迁移：BoardState.kredits / slots 的键是 ESide（绝对座位 1/2），不是 local/enemy
        self.kredits = kredits or {1: 5, 2: 3}
        self.slots = slots or {1: 7, 2: 4}
        self.max_possible_kredits = max_kredits      # None = 快照没读到（应抛）


bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-52s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def main():
    cn = CardNatives(B(), my_seat=1)
    call = lambda n, *a: cn.call(n, None, *a)          # noqa: E731
    # ---- 纯映射：换 side，答案跟着变 ----
    for s, w in ((1, 2), (2, 1), (0, 0)):
        chk("GetTheOtherSide(%d)" % s, call("GetTheOtherSide", s, 0), w)
    for s, w in ((1, 5), (2, 6), (0, 0)):
        chk("GetSupportLineBySide(%d)" % s, call("GetSupportLineBySide", s, 0), w)
    for s, w in ((1, 3), (2, 4), (0, 0)):
        chk("GetHandLocationBySide(%d)" % s, call("GetHandLocationBySide", s, 0), w)
    for s, w in ((1, 1), (2, 2), (0, 0)):
        chk("GetDeckLocationBySide(%d)" % s, call("GetDeckLocationBySide", s, 0), w)
    # ---- 依赖本地座位：换 my_seat，答案跟着变 ----
    for seat, cl, op, hand_c, hand_o in ((1, 1, 2, 3, 4), (2, 2, 1, 4, 3)):
        c2 = CardNatives(B(), my_seat=seat)
        g = lambda n: c2.call(n, None, 0)              # noqa: E731
        chk("seat=%d GetClientSide" % seat, g("GetClientSide"), cl)
        chk("seat=%d GetOpponentSide" % seat, g("GetOpponentSide"), op)
        chk("seat=%d GetClientSideHandLocation" % seat, g("GetClientSideHandLocation"), hand_c)
        chk("seat=%d GetOpponentSideHandLocation" % seat, g("GetOpponentSideHandLocation"), hand_o)
    # ---- 轮到谁：换 our_turn / my_seat ----
    for ot, seat, playing in ((True, 1, 1), (False, 1, 2), (True, 2, 2), (False, 2, 1)):
        c2 = CardNatives(B(our_turn=ot), my_seat=seat)
        chk("our_turn=%s seat=%d GetPlayingSide" % (ot, seat), c2.call("GetPlayingSide", None, 0), playing)
        chk("our_turn=%s seat=%d GetActiveSide" % (ot, seat), c2.call("GetActiveSide", None, 0), playing)
        chk("our_turn=%s IsClientPlaying" % ot, c2.call("IsClientPlaying", None, 0), ot)
    # ---- 指挥点：换 side / 换局面 ----
    chk("getKreditBySide(1) seat=1", call("getKreditBySide", 1), 5)
    chk("getKreditBySide(2) seat=1", call("getKreditBySide", 2), 3)
    # 绝对座位：my_seat 换了，side=1 的指挥点不变（以前 local/enemy 时会跟着翻）
    chk("getKreditBySide(1) seat=2（与 my_seat 无关）",
        CardNatives(B(), my_seat=2).call("getKreditBySide", None, 1), 5)
    chk("getKreditBySide(0) => -100", call("getKreditBySide", 0), -100)
    chk("getKreditSlotBySide(1)", call("getKreditSlotBySide", 1), 7)
    chk("getKreditSlotBySide(2)", call("getKreditSlotBySide", 2), 4)
    chk("getKreditSlotBySide(0) => -100", call("getKreditSlotBySide", 0), -100)
    # ---- getMaxPossibleKredits：真值 = GS+0x338（**不是槽数**）----
    for mk in (7, 12):
        chk("getMaxPossibleKredits(快照=%d)" % mk,
            CardNatives(B(max_kredits=mk), my_seat=1).call("getMaxPossibleKredits", None), mk)
    # ---- effectvm 的读 hook 也读同一字段（旧映射拿槽数顶替）----
    import types as _types
    from semantics import effectvm as _EV
    st_hook = _types.SimpleNamespace(kredits={"local": 5, "enemy": 3},
                                     slots={"local": 7, "enemy": 4},
                                     max_possible_kredits=9, cards=[])
    hooks = _EV.make_read_hooks(st_hook, 1)
    f_hook = _types.SimpleNamespace(locals={})
    e_hook = _types.SimpleNamespace(kids=[_types.SimpleNamespace(
        op="LocalOutVariable", args={"prop": "V"})])
    got = hooks["getMaxPossibleKredits"](None, f_hook, None, [], e_hook)
    chk("effectvm hook getMaxPossibleKredits = 9（不是槽数 7）", (got, f_hook.locals.get("V")), (9, 9))
    hooks2 = _EV.make_read_hooks(_types.SimpleNamespace(
        kredits={"local": 5, "enemy": 3}, slots={"local": 7, "enemy": 4}, cards=[]), 1)
    try:
        hooks2["getMaxPossibleKredits"](None, _types.SimpleNamespace(locals={}), None, [], e_hook)
        chk("effectvm hook 缺字段应抛", "没抛", "抛")
    except Unimplemented:
        chk("effectvm hook 缺字段抛 Unimplemented", True, True)
    # ---- CanEndTurn：默认 True 的**近似**（只有 HUD/Logic 两个调用点，不在卡牌链上）----
    chk("CanEndTurn 默认 True", cn.call("CanEndTurn", None), True)
    chk("CanEndTurn 登记为近似", "CanEndTurn" in APPROX, True)
    chk("arity CanEndTurn()", CardNatives.arity("CanEndTurn"), 0)
    chk("换局面 getKreditBySide",
        CardNatives(B(kredits={1: 9, 2: 1}), my_seat=1).call("getKreditBySide", None, 2), 1)
    # ---- GetOppositeSide：换卡的座位 ----
    for raw, w in ((1, 2), (2, 1), (0, 0)):
        chk("GetOppositeSide(side_enum=%d)" % raw, cn.call("GetOppositeSide", {"side_enum": raw}), w)
    # ---- 缺信息必须抛，不能猜 ----
    for label, f in (("缺 my_seat", lambda: CardNatives(B()).call("GetClientSide", None, 0)),
                     ("缺 BoardState", lambda: CardNatives(None, my_seat=1).call("IsClientPlaying", None, 0)),
                     ("max kredits 未读", lambda: cn.call("getMaxPossibleKredits", None))):
        try:
            f()
            chk(label + " 应抛", "没抛", "抛")
        except Unimplemented:
            chk(label + " 抛 Unimplemented", True, True)
    # ---- arity：VM 的回退启发式（无 UFunction 时按前 n 个入参切）==> 不能把 wc 吞成出参 ----
    chk("arity GetSupportLineBySide(side,wc)", CardNatives.arity("GetSupportLineBySide"), 2)
    chk("arity getKreditBySide(side)", CardNatives.arity("getKreditBySide"), 1)
    print("test_byside_natives: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
