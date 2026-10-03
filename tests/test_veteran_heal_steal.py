#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老兵 / 治疗 / 偷牌 的钩子段（2026-10-03，BP_CardFunctions 逐行读过）：顺序、形参、否决/压制语义 + boardeval 消费。

* MakeVeteran@7127：未压制才 `OnBecomingVeteran()`；0x20 逐张 `OnOtherCardBecomingVeteran(card)`；
* FullyHealCard@701：0xC `OnBeforeFullyRepaired(card,&stop)`（真 ⇒ 中止）→ 0x2C `OnOtherCardFullyRepaired(card,amount)`
  （`cardID != card`）→ 未压制才 `OnFullyRepaired(amount)`；
* ChangeUnitOwnership@18087：Before-Leave（自己未压制 + 0x2E 非自己）→ After-Leave（自己未压制 + 0x8 非自己）→ OnEnterPlay(0) + 0x2B 非自己。
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
    def __init__(self, name, p, cid=0, sup=False, side="enemy"):
        self.name, self.card_id, self.location, self.side = name, cid, "frontline", side
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


OVR = {1001: {"OnBecomingVeteran", "OnFullyRepaired", "OnLeaveBoardOrOwner", "OnAfterLeaveBoard", "OnEnterPlay",
              "OnOtherCardBecomingVeteran", "OnOtherCardFullyRepaired", "OnOtherCardLeaveBoardOrOwner",
              "OnAfterOtherCardLeaveBoardOrOwner", "OnOtherCardEnterPlay", "OnBeforeFullyRepaired"},
       1002: {"OnOtherCardBecomingVeteran", "OnOtherCardFullyRepaired", "OnOtherCardLeaveBoardOrOwner",
              "OnAfterOtherCardLeaveBoardOrOwner", "OnOtherCardEnterPlay"},
       1003: {"OnBeforeFullyRepaired"}}


class _TC(TR.TriggerCache):
    def __init__(self):
        self._fn = {}

    def fn_of_class(self, uc, hook=TR.HOOK):
        return 7 if hook in OVR.get(uc, ()) else 0


def run(fn, cards, *a, stop_by=None, **kw):
    calls = []

    def fake(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw2):
        calls.append((h, getattr(c, "name", "?"), dict(args)))
        out = {"stopAction": True} if (stop_by and h == "OnBeforeFullyRepaired" and c.name == stop_by) else {}
        return {"ran": True, "eff": {}, "out": out, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = fn(_K(), _S(cards), *a, cache=_TC(), read_hooks={}, my_side=1, hq_own=(), hq_enemy=(),
                 enum_random=False, **kw)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old
    return res, calls


def main():
    me, w, w3 = _C("ME", 1, 11), _C("W", 2, 12), _C("W3", 3, 13)
    # ---- 老兵 ----
    _r, calls = run(TR.run_veteran, [me, w], me)
    chk("老兵：自己 OnBecomingVeteran → 0x20（含自己、含旁观者）",
        [(h, n) for h, n, _a in calls] == [("OnBecomingVeteran", "ME"), ("OnOtherCardBecomingVeteran", "ME"),
                                           ("OnOtherCardBecomingVeteran", "W")], str(calls))
    sup = _C("SUP", 1, 14, sup=True)
    _r, calls = run(TR.run_veteran, [sup, w], sup)
    chk("老兵：被压制 ⇒ 自己的 OnBecomingVeteran 不跑，且 fetch 0x20 丢掉被压制的牌（只剩旁观者）",
        [(h, n) for h, n, _a in calls] == [("OnOtherCardBecomingVeteran", "W")], str(calls))
    chk("老兵：0x20 形参 cardBecomingVeteran", calls[0][2] == {"cardBecomingVeteran": 1}, str(calls[0][2]))

    # ---- 治疗 ----
    r, calls = run(TR.run_heal, [me, w, w3], me, 3)
    seq = [(h, n) for h, n, _a in calls]
    chk("治疗：0xC（覆写者各一次）→ 0x2C（排除自己）→ 自己 OnFullyRepaired",
        seq == [("OnBeforeFullyRepaired", "ME"), ("OnBeforeFullyRepaired", "W3"),
                ("OnOtherCardFullyRepaired", "W"), ("OnFullyRepaired", "ME")], str(seq))
    chk("治疗：形参 cardRepaired/amount", calls[2][2] == {"cardRepaired": 1, "amount": 3}
        and calls[3][2] == {"amount": 3}, str(calls))
    r, calls = run(TR.run_heal, [me, w, w3], me, 3, stop_by="W3")
    chk("治疗：0xC 有人返回 stopAction ⇒ 否决，后面的 0x2C/自己的都不跑",
        r["meta"]["vetoed"] and [h for h, _n, _a in calls] == ["OnBeforeFullyRepaired", "OnBeforeFullyRepaired"],
        str(calls))

    # ---- 偷牌 ----
    _r, calls = run(TR.run_steal, [me, w], me, 5, 6)
    seq = [(h, n) for h, n, _a in calls]
    chk("偷牌：离场(自己→0x2E 非自己) → 离场后(自己→0x8 非自己) → OnEnterPlay(0)+0x2B 非自己",
        seq == [("OnLeaveBoardOrOwner", "ME"), ("OnOtherCardLeaveBoardOrOwner", "W"),
                ("OnAfterLeaveBoard", "ME"), ("OnAfterOtherCardLeaveBoardOrOwner", "W"),
                ("OnEnterPlay", "ME"), ("OnOtherCardEnterPlay", "W")], str(seq))
    chk("偷牌：method=5 / Method=0 / oldLocation",
        calls[1][2]["method"] == 5 and calls[4][2] == {"Method": 0} and calls[3][2]["oldLocation"] == 6,
        str(calls))

    # ---- 转化（ConvertCard：OnEnterPlay(5)+0x2B，再 0x22）----
    OVR[1002] = OVR[1002] | {"OnOtherCardConverted"}
    _r, calls = run(TR.run_convert, [me, w], me, [31], [11], "NEW", 9)
    seq = [(h, n) for h, n, _a in calls]
    chk("转化：新牌自己 OnEnterPlay(5) → 0x2B 非自己(Method=5) → 0x22 非新牌",
        seq == [("OnEnterPlay", "ME"), ("OnOtherCardEnterPlay", "W"), ("OnOtherCardConverted", "W")], str(seq))
    chk("转化：形参 Method=5 / 新旧 id / 名字 / 发起者",
        calls[0][2] == {"Method": 5} and calls[1][2]["Method"] == 5
        and calls[2][2] == {"oldCardIDs": [31], "newCardIDs": [11], "newCardName": "NEW", "instigatorID": 9}, str(calls))
    _r, calls = run(TR.run_convert, [me, w], me, [31], [11], "NEW", 9, skip_trigger=True)
    chk("转化：skipTrigger ⇒ 不跑 0x22", [h for h, _n, _a in calls] == ["OnEnterPlay", "OnOtherCardEnterPlay"], str(calls))

    # ---- 战斗伤害过改伤钩子 ----
    class _D(_C):
        def __init__(self, name, p, cid, atk):
            super().__init__(name, p, cid)
            self.attack = atk
    fa, fb = _D("FA", 1, 21, 3), _D("FB", 2, 22, 2)
    import kardsmem.objects as _oa
    OVR[1001] = set(OVR[1001]) | {"OnOtherCardDealDamageAddDamage"}
    calls2 = []

    def fake2(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0, hq_own=(), hq_enemy=(), **kw2):
        calls2.append((h, c.name, dict(args)))
        out = {"damageToAdd": 1} if h == "OnOtherCardDealDamageAddDamage" else {}
        return {"ran": True, "eff": {}, "out": out, "records": [], "gaps": [], "stopped": None}
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake2
    try:
        fr = TR.fight_damage(_K(), _S([fa, fb]), fa, fb, cache=_TC(), read_hooks={}, my_side=1,
                             hq_own=(), hq_enemy=(), enum_random=False)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old
    chk("战斗：改伤钩子参与（fromFight=True，每个方向 +1）⇒ a→b=3+? b→a=2+?",
        fr["to_b"] > 3 and fr["to_a"] > 2 and all(a.get("fromFight", True) for h, _n, a in calls2 if "Modify" in h),
        str((fr["to_b"], fr["to_a"], [h for h, _n, _a in calls2][:4])))
    stf = mk_fight = None
    # ---- boardeval 消费 ----
    def mk(units, **kw):
        return B.Sim({u.id: u for u in units}, {"local": 20, "enemy": 20}, 5.0, {}, **kw)

    st = mk([U(1, "enemy", "frontline", 3, 3, 3, "infantry", mdef=5), U(2, "local", "back", 1, 1, 1, "infantry")],
            event_fx={"steal": {1: {"damage_own_hq": 2}}})
    B._apply_eff(st, {"steal": True}, 1)
    chk("偷牌：event_fx['steal'] 后果被结算（己方 HQ -2）", st.hq["local"] == 18, str(st.hq))
    st2 = mk([U(1, "enemy", "frontline", 3, 3, 3, "infantry", mdef=5)], event_fx={"heal": {1: {"heal_vetoed": True}}})
    B._apply_eff(st2, {"heal": True}, 1)
    chk("治疗：event_fx['heal'].heal_vetoed ⇒ 治疗不生效", st2.units[1].dfn == 3.0, str(st2.units[1].dfn))
    st3 = mk([U(1, "enemy", "frontline", 3, 3, 3, "infantry", mdef=5)], event_fx={"heal": {1: {"damage_own_hq": 1}}})
    B._apply_eff(st3, {"heal": True}, 1)
    chk("治疗：没被否决 ⇒ 满血并结算 0x2C/自己的后果", st3.units[1].dfn == 5.0 and st3.hq["local"] == 19,
        str((st3.units[1].dfn, st3.hq)))
    sf = B.Sim({1: U(1, "local", "frontline", 3, 5, 3, "infantry"), 2: U(2, "enemy", "frontline", 2, 5, 2, "infantry")},
               {"local": 20, "enemy": 20}, 5.0, {})
    B._apply_eff(sf, {"fight": [1, 2], "fight_dmg": (4, 1)}, None)
    chk("战斗：fight_dmg 优先于裸 atk（1 号受 1、2 号受 4）", sf.units[1].dfn == 4 and sf.units[2].dfn == 1,
        str((sf.units[1].dfn, sf.units[2].dfn)))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
