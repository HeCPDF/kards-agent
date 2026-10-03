#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压制/定住族（0x3A / 0x3D）测试。

规格 = 本构建 BP 导出（逐行读过）：
  * `SuppressUnit(cardID, instigatorID, &qqq)` @7446 是 `SuppressMultipleUnits([cardID], …)` 的薄包装；
  * `SuppressMultipleUnits` @7456 在"新压制"分支里 RemoveGuard/Fury/Blitz/Immune/Alpine/Ambush/
    Mobilize/Smokescreen/Salvage/Shock、重甲清零、清 customName1/2、清指向税、effectType=0，
    然后 fetch **0x3A** 逐张 `OnOtherCardSuppressed(_card)`；
  * `PinUnit(cardID, instigatorID)` @971 只动 receivedAbilities/pinnedTurns，然后 fetch **0x3D**
    逐张 `OnOtherUnitPinned(_card)`。
⇒ 两个动词**必须分开**（以前摘要键都是 "pin"，0x3A 根本选不出来）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import semantics.effectvm as EV                                    # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from policy.boardeval import U                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), **kw):
    return B.Sim({u.id: u for u in units}, {"local": 20, "enemy": 20}, 5.0, {}, **kw)


def eff_of(*records):
    r = EV.Recorder()
    r.records = [{"verb": v, "args": list(a), "tainted": False} for v, a in records]
    return EV.to_effects(r)


def main():
    # ---------------- effectvm：三个动词各出各的键 ----------------
    chk("SuppressUnit ⇒ suppress（不是 pin）", eff_of(("SuppressUnit", [11, 7, None])).get("suppress") is True)
    chk("PinUnit ⇒ pin", eff_of(("PinUnit", [11, 7])).get("pin") is True)
    e3 = eff_of(("SuppressMultipleUnits", [[11, 22], 7, None]))
    chk("SuppressMultipleUnits ⇒ suppress_aoe + id 列表",
        e3.get("suppress_aoe") is True and e3.get("suppress_aoe_ids") == [11, 22], str(e3))

    # ---------------- boardeval：压制 ≠ 定住 ----------------
    pin_u = U(1, "enemy", "frontline", 3, 3, 3, "infantry", kw=("guard", "fury"), opc=1)
    pin_u.armor, pin_u.tax = 2, 3
    s1 = mk([pin_u])
    B._apply_eff(s1, {"pin": True}, 1)
    u1 = s1.units[1]
    chk("定住：只置压制位，关键词/重甲/指向税不动",
        u1.pinned and set(u1.kw) >= {"guard", "fury"} and u1.armor == 2 and u1.tax == 3
        and not s1.gaps, str(s1.gaps))

    sup_u = U(1, "enemy", "frontline", 3, 3, 3, "infantry", kw=("guard", "fury", "shock"), opc=1)
    sup_u.armor, sup_u.tax, sup_u.attacks_left = 2, 3, 2
    s2 = mk([sup_u])
    B._apply_eff(s2, {"suppress": True}, 1)
    u2 = s2.units[1]
    chk("压制：清关键词/重甲/指向税 + 置压制位 + 不能再动",
        u2.pinned and not ({"guard", "fury", "shock"} & set(u2.kw)) and u2.armor == 0
        and u2.tax == 0 and u2.attacks_left == 0)
    chk("压制如实记缺口（customName1/2、effectType、customJson 未建模）",
        any("suppress" in g and "未建模" in g for g in s2.gaps), str(s2.gaps))
    chk("压制后不能行动（can_act False）", not u2.can_act())

    # 换数据答案跟着变：没关键词的单位压完只是压制
    plain = U(1, "enemy", "frontline", 3, 3, 3, "infantry")
    s3 = mk([plain])
    B._apply_eff(s3, {"suppress": True}, 1)
    chk("换数据答案要变：无关键词单位压制 ⇒ 只置位", not s3.units[1].kw and s3.units[1].pinned)

    # ---------------- 事件钩子后果（event_fx）接线 ----------------
    a = U(1, "enemy", "frontline", 3, 3, 3, "infantry", kw=("guard",))
    b = U(2, "enemy", "frontline", 2, 2, 2, "infantry", kw=("fury",))
    s4 = mk([a, b], event_fx={"suppress": {1: {"kredit": 1}, 2: {"kredit": 2}}})
    B._apply_eff(s4, {"suppress": True}, 1)
    B._apply_eff(s4, {"suppress": True}, 2)
    chk("0x3A 后果按被压制的卡结算（1 号 +1、2 号 +2 指挥点）",
        s4.kredits == 8.0, "kred=%s" % s4.kredits)

    s5 = mk([a, b], event_fx={"pin": {2: {"buff": [3, 0]}}})
    B._apply_eff(s5, {"pin": True}, 2)
    chk("0x3D 后果只对命中的卡生效（2 号 +3 攻）", s5.units[2].atk == 5 and s5.units[1].atk == 3)

    # 群体压制：逐张走同一条路
    c1, c2 = (U(5, "enemy", "frontline", 3, 3, 3, "infantry", kw=("guard",)),
              U(6, "enemy", "frontline", 3, 3, 3, "infantry", kw=("shock",)))
    s6 = mk([c1, c2], event_fx={"suppress": {5: {"kredit": 1}, 6: {"kredit": 1}}})
    B._apply_eff(s6, {"suppress_aoe": True, "suppress_aoe_ids": [5, 6]}, None)
    chk("群体压制：两张都被清关键词 + 各自后果都结算",
        s6.units[5].pinned and s6.units[6].pinned and not s6.units[5].kw and not s6.units[6].kw
        and s6.kredits == 7.0, "kred=%s" % s6.kredits)

    # ---------------- triggers：0x3A / 0x3D 的列表与形参 ----------------
    class _C:
        def __init__(self, name, p, cid, side="local", sup=False):
            self.name, self.side, self.card_id, self.location = name, side, cid, "frontline"
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
        def __init__(self):
            self._fn = {}

        def fn_of_class(self, uc, hook=TR.HOOK):
            m = {1002: {"OnOtherCardSuppressed", "OnOtherUnitPinned"},
                 1003: {"OnOtherUnitPinned"},
                 1004: {"OnOtherCardSuppressed", "OnOtherUnitPinned"}}
            return 7 if hook in m.get(uc, ()) else 0

    def run(hook, moved, cards):
        calls = []

        def fake(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
                 hq_own=(), hq_enemy=(), **kw):
            calls.append((h, getattr(c, "name", "?"), dict(args)))
            return {"ran": True, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}

        import kardsmem.objects as _oa
        old_oa, old = _oa.ObjectArray, TR._run_hook_ex
        _oa.ObjectArray, TR._run_hook_ex = _OA, fake
        try:
            TR.run_target_event(_K(), _S(cards), hook, moved, cache=_TC(), read_hooks={},
                                my_side=1, hq_own=(), hq_enemy=(), enum_random=False)
        finally:
            _oa.ObjectArray, TR._run_hook_ex = old_oa, old
        return calls

    victim = _C("V", 5, 77)
    watcher = _C("W", 2, 11)
    calls = run("OnOtherCardSuppressed", victim, [victim, watcher])
    chk("0x3A：形参名是 card、列表按触发表序", calls == [("OnOtherCardSuppressed", "W", {"card": 5})],
        str(calls))
    calls2 = run("OnOtherUnitPinned", victim, [victim, watcher])
    chk("0x3D：同样是 card 形参", calls2 == [("OnOtherUnitPinned", "W", {"card": 5})], str(calls2))
    calls3 = run("OnOtherCardSuppressed", victim, [victim, _C("S", 4, 12, sup=True)])
    chk("被压制的旁观者不进 0x3A 列表（不在例外表）", calls3 == [], str(calls3))
    try:
        TR.run_target_event(_K(), _S([]), "OnSomethingElse", victim)
        chk("未登记的族必须报错", False, "静默通过")
    except KeyError:
        chk("未登记的族必须报错", True)
    chk("规格表登记了触发号 0x3A / 0x3D",
        TR.SUPPRESS_HOOK_SPECS["OnOtherCardSuppressed"]["trigger"] == 0x3A
        and TR.PIN_HOOK_SPECS["OnOtherUnitPinned"]["trigger"] == 0x3D)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
