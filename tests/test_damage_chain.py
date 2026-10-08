#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A5-②：效果伤害链（`DamageCard` BP_CardFunctions.cpp:895 → `ApplyDamageToCard` :16375）与对打（`MakeCardsFight` :9755）的
受伤钩子——顺序 / 形参 / 互斥 + `Sim.event_fx["damage"]` 消费。假 VM（真 VM 与实机未验）。

原文顺序（见 `triggers.run_damage_card` 注释）：
  AddDamage[自己 ModifyDamageDealt → 0x25] → AfterCalc[免疫 ⇒ 0；自己 → 0x26] → BeforeReceiveDamage[final>0：自己 → 0x34，isCombat=假]
  → （扣防，sim 侧）→ ExecuteOnCardDealDamageEffects[自己 OnCardDealDamage → 0x24，isCombatDamage=假]。
  `isRedirected` 真 ⇒ 跳过 AddDamage 一步。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _hookfake import ME, OPP, Checker, card, names, run          # noqa: E402
from policy.search import wire_sim as _wire_sim                    # noqa: E402
_wire_sim()                        # ★ 门面已删（P5）：wiring 的唯一接缝（原来靠 import 门面的副作用）
import semantics.triggers as TR                                    # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402
from sim.state import Sim, U                                       # noqa: E402

chk = Checker()


def ovr(d):
    return {1000 + k: set(v) for k, v in d.items()}


def behave(h, c, args):
    if h == "OnCardDealDamage_ModifyDamageDealt":
        return {"out": {"newDamage": args["Damage"] + 1}}
    if h == "OnOtherCardDealDamageAddDamage":
        return {"out": {"damageToAdd": 2, "reRunAtEnd": False}}
    if h == "OnDealDamageAddDamageAfterCalc":
        return {"out": {"damageToAdd": -1}}
    if h == "OnOtherCardDealDamageAddDamageAfterCalc":
        return {"out": {"damageToAdd": 0, "stopAdding": False}}
    return {}


def main():
    d = card(50, "D", "hand", ME)
    t = card(60, "T", "frontline", OPP)
    w = {i: card(i, "W%d" % i, "back", OPP) for i in (61, 62, 63, 64)}
    cards = [d, t] + list(w.values())
    O = ovr({50: {"OnCardDealDamage_ModifyDamageDealt", "OnDealDamageAddDamageAfterCalc", "OnCardDealDamage"},
             60: {"OnReceiveDamage"}, 61: {"OnOtherCardDealDamageAddDamage"},
             62: {"OnOtherCardDealDamageAddDamageAfterCalc"}, 63: {"OnOtherCardReceiveDamage"},
             64: {"OnOtherCardDealDamage"}})

    # ---- 全链：顺序 + 形参 + 最终伤害 ----
    r, calls = run(TR.run_damage_card, cards, O, t, 3, d, behave=behave, from_fight=False)
    seq = names(calls)
    want = [("OnCardDealDamage_ModifyDamageDealt", "D"), ("OnOtherCardDealDamageAddDamage", "W61"),
            ("OnDealDamageAddDamageAfterCalc", "D"), ("OnOtherCardDealDamageAddDamageAfterCalc", "W62"),
            ("OnReceiveDamage", "T"), ("OnOtherCardReceiveDamage", "W63"),
            ("OnCardDealDamage", "D"), ("OnOtherCardDealDamage", "W64")]
    chk("顺序：自己改伤 → 0x25 → 自己 AfterCalc → 0x26 → 自己受伤 → 0x34 → 自己造成伤害 → 0x24", seq == want, str(seq))
    chk("最终伤害 = ((3→自己+1=4)+0x25 的 +2=6, 再自己 AfterCalc −1 =5, 0x26 +0) = 5", r["final"] == 5 and r["meta"]["pre"] == 6,
        str((r["final"], r["meta"])))
    a = {h: ar for h, _n, ar, _k in calls}
    chk("形参：改伤 fromAttack=假/fromFight；0x25 isDefenderDamage=假；AfterCalc isAttacker=假/isRedirected=假",
        a["OnCardDealDamage_ModifyDamageDealt"] == {"toCard": 60, "Damage": 3, "fromAttack": False, "fromFight": False}
        and a["OnOtherCardDealDamageAddDamage"] == {"cardDealingDamage": 50, "toCard": 60, "Damage": 4,
                                                    "fromAttack": False, "isDefenderDamage": False}
        and a["OnDealDamageAddDamageAfterCalc"] == {"toCard": 60, "Damage": 6, "fromAttack": False,
                                                    "isAttacker": False, "isRedirected": False}
        and a["OnOtherCardDealDamageAddDamageAfterCalc"] == {"cardDealingDamage": 50, "toCard": 60, "Damage": 5,
                                                             "fromAttack": False, "isRedirected": False}, str(a))
    chk("形参：受伤 (fromCard, Damage=final)；0x34 fromAttack=假（非战斗）；造成伤害 isCombatDamage=假/CounterDamage=假/isRedirected=假",
        a["OnReceiveDamage"] == {"fromCard": 50, "Damage": 5}
        and a["OnOtherCardReceiveDamage"] == {"fromCard": 50, "toCard": 60, "fromAttack": False, "Damage": 5}
        and a["OnCardDealDamage"] == {"toCard": 60, "Damage": 5, "isCombatDamage": False, "CounterDamage": False,
                                      "isRedirected": False}
        and a["OnOtherCardDealDamage"] == {"cardDealingDamage": 50, "toCard": 60, "Damage": 5, "isCombatDamage": False,
                                           "CounterDamage": False, "isRedirected": False}, str(a))
    # ---- 互斥 / 边界 ----
    r2, calls2 = run(TR.run_damage_card, cards, O, t, 3, d, behave=behave, is_redirected=True)
    s2 = names(calls2)
    chk("isRedirected ⇒ 跳过改伤一步（ModifyDamageDealt/0x25 不跑），AfterCalc 的 isRedirected=真，基数就是 3",
        ("OnCardDealDamage_ModifyDamageDealt", "D") not in s2 and ("OnOtherCardDealDamageAddDamage", "W61") not in s2
        and [x for x in calls2 if x[0] == "OnDealDamageAddDamageAfterCalc"][0][2]["isRedirected"] is True
        and [x for x in calls2 if x[0] == "OnDealDamageAddDamageAfterCalc"][0][2]["Damage"] == 3 and r2["final"] == 2, str(s2))
    t_im = card(60, "T", "frontline", OPP)
    t_im.raw["received_abilities"] = [{"ability": "immune", "givers": [1]}]
    r3, calls3 = run(TR.run_damage_card, [d, t_im] + list(w.values()), O, t_im, 3, d, behave=behave)
    s3 = names(calls3)
    chk("受击方免疫 ⇒ AfterCalc 直接 0（自己的 AfterCalc/0x26 不跑）、final=0，不发受伤通知", r3["final"] == 0
        and ("OnDealDamageAddDamageAfterCalc", "D") not in s3 and ("OnReceiveDamage", "T") not in s3
        and ("OnOtherCardReceiveDamage", "W63") not in s3, str(s3))
    chk("final==0 时造成伤害通知是否仍调用：按报告字面调用，并在 notes 里标注『未证实』",
        ("OnCardDealDamage", "D") in s3 and any("final==0" in n for n in r3["notes"]), str(r3["notes"]))
    hand_t = card(60, "T", "hand", OPP)
    r4, calls4 = run(TR.run_damage_card, [d, hand_t], O, hand_t, 3, d, behave=behave)
    chk("目标不在场 ⇒ 什么都不发生（final=0，无任何钩子）", r4["final"] == 0 and calls4 == [], str(names(calls4)))
    r5, calls5 = run(TR.run_damage_card, cards, O, t, 3, None, behave=behave)
    chk("伤害来源拿不到指针 ⇒ 不算（final=None + notes），不拿别的牌冒充", r5["final"] is None and calls5 == [], str(r5["notes"]))
    sup = card(60, "T", "frontline", OPP, is_suppressed=True)
    r6, calls6 = run(TR.run_damage_card, [d, sup] + list(w.values()), O, sup, 3, d, behave=behave)
    s6 = names(calls6)
    chk("被压制的受击方：自己的 OnReceiveDamage 不跑，0x34 照跑（ExecuteBeforeReceiveDamage 只门自己那条）",
        ("OnReceiveDamage", "T") not in s6 and ("OnOtherCardReceiveDamage", "W63") in s6, str(s6))
    dx = card(50, "D", "hand", ME)
    dx.raw["received_abilities"] = [{"ability": "excess", "givers": [1]}]
    r7, _c7 = run(TR.run_damage_card, [dx, t] + list(w.values()), O, t, 3, dx, behave=behave)
    chk("dealer 带 excess ⇒ 溢出打总部的分支未建模，notes 如实记缺口", any("excess" in n for n in r7["notes"]), str(r7["notes"]))

    # ---- damage_card_fx 聚合：桶 calc/recv/dealt ----
    def beh2(h, c, args):
        base = behave(h, c, args)
        amt = {"OnOtherCardDealDamageAddDamage": 1, "OnOtherCardReceiveDamage": 2, "OnOtherCardDealDamage": 4}.get(h)
        if amt:
            base = dict(base, records=[{"verb": "GiveKreditsBySide", "args": [int(ME), amt, 0], "tainted": False}])
        return base
    fx, _c = run(TR.damage_card_fx, cards, O, t, 3, d, behave=beh2)
    chk("damage_card_fx：final=5，0x25→calc 桶、0x34→recv 桶、0x24→dealt 桶",
        fx["final"] == 5 and fx["buckets"].get("calc", {}).get("kredit") == 1
        and fx["buckets"].get("recv", {}).get("kredit") == 2 and fx["buckets"].get("dealt", {}).get("kredit") == 4, str(fx))

    # ---- 对打 MakeCardsFight：改伤 → 两次 ApplyDamageToCard（b 先、a 后）----
    a_ = card(70, "A", "frontline", ME, attack=3)
    b_ = card(71, "B", "frontline", OPP, attack=2)
    OF = ovr({70: {"OnCardDealDamage_ModifyDamageDealt", "OnReceiveDamage", "OnCardDealDamage"},
              71: {"OnCardDealDamage_ModifyDamageDealt", "OnReceiveDamage", "OnCardDealDamage"}})

    def beh3(h, c, args):
        if h == "OnCardDealDamage_ModifyDamageDealt":
            return {"out": {"newDamage": args["Damage"]}}
        return {}
    fr, calls8 = run(TR.fight_damage, [a_, b_], OF, a_, b_, behave=beh3)
    s8 = names(calls8)
    chk("对打：先算两个方向的改伤，再 ApplyDamageToCard(b←a) 的 受伤→造成伤害，最后 (a←b) 的",
        s8 == [("OnCardDealDamage_ModifyDamageDealt", "A"), ("OnCardDealDamage_ModifyDamageDealt", "B"),
               ("OnReceiveDamage", "B"), ("OnCardDealDamage", "A"), ("OnReceiveDamage", "A"), ("OnCardDealDamage", "B")]
        and (fr["to_b"], fr["to_a"]) == (3, 2), str(s8))
    a8 = [(h, n, ar) for h, n, ar, _k in calls8]
    chk("对打：b 受伤 (fromCard=a, Damage=3)、a 造成伤害 isCombatDamage=假；a 受伤 (fromCard=b, Damage=2)",
        a8[2][2] == {"fromCard": 70, "Damage": 3} and a8[3][2]["isCombatDamage"] is False and a8[3][2]["toCard"] == 71
        and a8[4][2] == {"fromCard": 71, "Damage": 2} and a8[5][2]["toCard"] == 70, str(a8[2:]))
    _fr, calls9 = run(TR.fight_damage, [a_, b_], OF, a_, b_, behave=beh3, apply_hooks=False)
    chk("apply_hooks=False ⇒ 旧行为：只算改伤、不跑受伤链", [h for h, _n in names(calls9)] ==
        ["OnCardDealDamage_ModifyDamageDealt"] * 2, str(names(calls9)))

    def beh4(h, c, args):
        recs = {("OnReceiveDamage", "B"): 1, ("OnCardDealDamage", "A"): 2, ("OnReceiveDamage", "A"): 4,
                ("OnCardDealDamage", "B"): 8}
        if (h, c.name) in recs:
            return {"records": [{"verb": "GiveKreditsBySide", "args": [int(ME), recs[(h, c.name)], 0], "tainted": False}]}
        return beh3(h, c, args)
    ffx, _c = run(TR.fight_damage_fx, [a_, b_], OF, a_, b_, behave=beh4)
    bk = {k: v.get("kredit") for k, v in ffx["buckets"].items()}
    chk("fight_damage_fx：四个桶 fight_b_recv/fight_b_dealt/fight_a_recv/fight_a_dealt", bk == {
        "fight_b_recv": 1, "fight_b_dealt": 2, "fight_a_recv": 4, "fight_a_dealt": 8} and (ffx["to_b"], ffx["to_a"]) == (3, 2), str(bk))

    # ---- sim 消费 ----
    def mk(units, ef):
        return Sim({u.id: u for u in units}, {ME: 20, OPP: 20}, 5.0, {}, event_fx=ef, my_side=ME)

    fxd = {"final": 5, "amount": 3, "gaps": ["g1"],
           "buckets": {"calc": {"eff": {"damage_own_hq": 1}},
                       "recv": {"units": [(60, {"buff": [0, 2]})]},          # 受伤通知先 +2 防
                       "dealt": {"eff": {"damage_own_hq": 4}}}}
    s = mk([U(60, OPP, "frontline", 1, 4, 1, "infantry")], {"damage": {(50, 60): fxd}})
    _apply_eff(s, {"damage": 3}, 60, src=50)
    chk("sim：按 final=5（不是裸 3）扣防；受伤桶在扣防**之前**（防 4+2=6 → 剩 1，没死；先扣再加会死）",
        60 in s.units and s.units[60].dfn == 1, str(s.units.get(60) and s.units[60].dfn))
    chk("sim：calc/dealt 桶都结算（己方总部 -1 -4），gaps 并入", s.hq[ME] == 15 and "damage：g1" in s.gaps, str((s.hq, s.gaps)))
    s = mk([U(60, OPP, "frontline", 1, 4, 1, "infantry")], {"damage": {(50, 60): dict(fxd, final=0)}})
    _apply_eff(s, {"damage": 3}, 60, src=50)
    chk("sim：final=0（免疫/被减光）⇒ 不扣防、不发受伤桶，造成伤害桶仍结算", s.units[60].dfn == 4 and s.hq[ME] == 15, str((s.units[60].dfn, s.hq)))
    s = mk([U(60, OPP, "frontline", 1, 4, 1, "infantry")], {"damage": {(50, 60): fxd}})
    _apply_eff(s, {"damage": 4}, 60, src=50)
    chk("sim：预计算的基数(3)与本次效果(4)对不上 ⇒ 按裸数值结算并记缺口", 60 not in s.units
        and any("对不上" in g for g in s.gaps), str(s.gaps))
    s = mk([U(60, OPP, "frontline", 1, 4, 1, "infantry")], {"damage": {(50, 60): fxd}})
    _apply_eff(s, {"damage": 3}, 60)
    chk("sim：没给来源牌 id ⇒ 裸数值（3 点，没死）", s.units[60].dfn == 1 and s.hq[ME] == 20, str(s.units[60].dfn))
    s = mk([U(60, OPP, "frontline", 1, 4, 1, "infantry")], {"damage": {(50, 60): {"final": None, "gaps": ["拿不到指针"]}}})
    _apply_eff(s, {"damage": 3}, 60, src=50)
    chk("sim：预计算失败(final=None) ⇒ 裸数值 + 缺口", s.units[60].dfn == 1 and "damage：拿不到指针" in s.gaps, str(s.gaps))
    # 对打
    fxf = {"to_b": 4, "to_a": 1, "gaps": [], "buckets": {"fight_b_recv": {"eff": {"damage_own_hq": 1}},
                                                         "fight_b_dealt": {"eff": {"damage_own_hq": 2}},
                                                         "fight_a_recv": {"eff": {"damage_own_hq": 4}},
                                                         "fight_a_dealt": {"eff": {"damage_own_hq": 8}}}}
    s = Sim({1: U(1, ME, "frontline", 3, 5, 3, "infantry"), 2: U(2, OPP, "frontline", 2, 5, 2, "infantry")},
            {ME: 20, OPP: 20}, 5.0, {}, event_fx={"damage": {("fight", 1, 2): fxf}}, my_side=ME)
    _apply_eff(s, {"fight": [1, 2], "fight_dmg": (4, 1)}, None)
    chk("sim 对打：b(2 号)受 4、a(1 号)受 1，四个桶都结算（总部 -15）", s.units[2].dfn == 1 and s.units[1].dfn == 4
        and s.hq[ME] == 5, str((s.units[2].dfn, s.units[1].dfn, s.hq)))
    s = Sim({1: U(1, ME, "frontline", 3, 5, 3, "infantry"), 2: U(2, OPP, "frontline", 2, 5, 2, "infantry")},
            {ME: 20, OPP: 20}, 5.0, {}, event_fx={"damage": {("fight", 1, 2): fxf}}, my_side=ME)
    _apply_eff(s, {"fight": [1, 2], "fight_dmg": (9, 1)}, None)
    chk("sim 对打：预计算伤害与本次不符 ⇒ 受伤桶不结算 + 缺口", s.hq[ME] == 20 and any("不符" in g for g in s.gaps), str(s.gaps))
    print("失败 %d 项" % chk.fails)
    return chk.fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
