#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""揭示族（0x37 + 自己那条）测试。

规格 = 本构建 BP 导出 `RevealCard(cardID, instigatorID, &qqq)`@9191（逐行读过）：
  `isRevealed=true` / `hasCovert=false` / `UpdateGuarded(location)` → NotifyRevealCard（表现，忽略）
  → **未被压制**才 `OnCardRevealed()`（无参）→ fetch 0x37 逐张 `OnOtherCardRevealed(cardRevealed)`
  → `OnEnterPlay(4)`（自己）→ fetch 0x2B 逐张 `OnOtherCardEnterPlay(cardRevealed, 4)`。
评估侧：`{"reveal": True}` 把 covert 去掉（既有行为）并结算 `event_fx["reveal"][卡 id]` 的后果。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from engine.state import EVENT_FX_KINDS, Sim, U                    # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _C:
    def __init__(self, name, p, cid=0, sup=False):
        self.name, self.card_id, self.location, self.side = name, cid, "frontline", "enemy"
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
        # 真实一点：被揭示那张只覆写"自己"的两条；旁观者各自只覆写一条
        m = {1001: {"OnCardRevealed", "OnEnterPlay"},
             1002: {"OnOtherCardRevealed"},
             1003: {"OnOtherCardEnterPlay"}}
        return 7 if hook in m.get(uc, ()) else 0


def run(cards, revealed, with_enter_play=True):
    calls = []

    def fake(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw):
        calls.append((h, getattr(c, "name", "?"), dict(args)))
        return {"ran": True, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = TR.run_reveal(_K(), _S(cards), revealed, cache=_TC(), read_hooks={}, my_side=1,
                            hq_own=(), hq_enemy=(), enum_random=False,
                            with_enter_play=with_enter_play)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old
    return res, calls


def main():
    # 被揭示的那张（自己覆写 OnCardRevealed/OnEnterPlay）+ 两个旁观者（分别覆写 0x37/0x2B）
    me = _C("ME", 1, 11)
    watcher37 = _C("W37", 2, 12)
    watcher2b = _C("W2B", 3, 13)
    res, calls = run([me, watcher37, watcher2b], me)
    seq = [(h, n) for h, n, _a in calls]
    chk("顺序：自己 OnCardRevealed → 0x37 → 自己 OnEnterPlay(4) → 0x2B",
        seq == [("OnCardRevealed", "ME"), ("OnOtherCardRevealed", "W37"),
                ("OnEnterPlay", "ME"), ("OnOtherCardEnterPlay", "W2B")], str(seq))
    chk("0x37 形参名 cardBeingRevealed + 指向被揭示那张",
        calls[1][2] == {"cardBeingRevealed": 1}, str(calls[1][2]))
    chk("0x2B 形参 Method=4、cardPlayed 指向被揭示那张",
        calls[3][2] == {"cardPlayed": 1, "Method": 4}, str(calls[3][2]))
    chk("自己的 OnEnterPlay(4) 也传 Method=4", calls[2][2] == {"Method": 4}, str(calls[2][2]))
    chk("meta 如实记 0x2B 不排除自己", "不排除自己" in res["meta"]["note"], str(res["meta"]))

    # 被压制：自己的那条不跑，0x37/0x2B 照跑（BP 只 gate 自己那条）
    sup = _C("SUP", 1, 14, sup=True)
    _r2, calls2 = run([sup, watcher37, watcher2b], sup)
    chk("被压制 ⇒ 自己 OnCardRevealed 不跑，0x37/0x2B 照跑",
        [h for h, _n, _a in calls2] == ["OnOtherCardRevealed", "OnEnterPlay",
                                        "OnOtherCardEnterPlay"], str(calls2))

    # with_enter_play=False 只跑前半段
    _r3, calls3 = run([me, watcher37, watcher2b], me, with_enter_play=False)
    chk("with_enter_play=False ⇒ 只跑 OnCardRevealed + 0x37",
        [h for h, _n, _a in calls3] == ["OnCardRevealed", "OnOtherCardRevealed"], str(calls3))

    # 规格表
    chk("触发号 0x37 登记、自己那条 own=True 且无触发号",
        TR.REVEAL_HOOK_SPECS["OnOtherCardRevealed"]["trigger"] == 0x37
        and TR.REVEAL_HOOK_SPECS["OnCardRevealed"]["own"] is True
        and TR.REVEAL_HOOK_SPECS["OnCardRevealed"]["trigger"] is None)
    chk("形参表与卡 cpp 签名一致（OnOtherCardRevealed(cardBeingRevealed)）",
        TR.REVEAL_HOOK_SPECS["OnOtherCardRevealed"]["params"] == ("cardBeingRevealed",))

    # ---------------- 评估侧：去 covert + 结算 0x37 后果 ----------------
    u1 = U(1, OPP, "frontline", 2, 2, 2, "infantry", kw=("covert",))
    s1 = Sim({1: u1}, {ME: 20, OPP: 20}, 5.0, {}, event_fx={"reveal": {1: {"kredit": 2}}}, my_side=ME)
    _apply_eff(s1, {"reveal": True}, 1)
    chk("揭示：covert 去掉 + 0x37 后果结算（+2 指挥点）",
        "covert" not in s1.units[1].kw and s1.kredits == 7.0, "kred=%s" % s1.kredits)
    s2 = Sim({1: U(1, OPP, "frontline", 2, 2, 2, "infantry", kw=("covert",))},
               {ME: 20, OPP: 20}, 5.0, my_side=ME)
    _apply_eff(s2, {"reveal": True}, 1)
    chk("没有 reveal 后果表 ⇒ 行为与旧版一致（只去 covert）",
        "covert" not in s2.units[1].kw and s2.kredits == 5.0)
    chk("event_fx 登记了 reveal 种类", "reveal" in EVENT_FX_KINDS, str(EVENT_FX_KINDS))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
