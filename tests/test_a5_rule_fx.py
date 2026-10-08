#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A5：`player.rule` 里 `_convert_fx / _damage_fx / _abilities_fx / _finish_event_fx` 的接线（假 VM，真 `Sim`）。

钉住：
  * 表的键与 sim 消费端一致——convert：`(发起牌 id, 旧牌 id 元组, 目标名)`；damage：`(来源牌 id, 目标 id)` / `("fight", a, b)`；
    abilities：`(单位 id, "+/-关键词")` + `("gap",)`；
  * 只对候选效果里真有的转化 / 伤害 / 关键词变化算（没有就是空表，零开销）；
  * 场上没有任何牌覆写 0x1D ⇒ abilities 表为空；
  * 单族算不出（抛异常）⇒ 该表为空并写进 `fx_meta[kind]["error"]`，不拖垮别的表；
  * 端到端：`_finish_event_fx` 后 `sim.event_fx` 里的表能被 `sim.engine` 消费。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _hookfake import ME, OPP, TC, Checker, K, card, patched      # noqa: E402
import player.rule as R                                            # noqa: E402
from policy.search import wire_sim as _wire_sim                    # noqa: E402
_wire_sim()                        # ★ 门面已删（P5）：wiring 的唯一接缝（原来靠 import 门面的副作用）
from sim.engine import _apply_eff                                  # noqa: E402
from sim.state import H, Sim, U                                    # noqa: E402

chk = Checker()


class St:
    def __init__(self, cards):
        self.cards, self.my_side, self.other_side, self.turn = cards, ME, OPP, 5


def ovr(d):
    return {1000 + k: set(v) for k, v in d.items()}


def mk_pol(O):
    pol = R.RuleV2.__new__(R.RuleV2)                 # 不走 __init__（要真会话/表）
    pol.P = dict(R.PARAMS, use_vm=True)
    pol.fx_meta = {}
    pol._km = lambda: K()
    pol._trig_cache = lambda st: TC(O)
    pol._trig_stream = lambda: None
    pol._hq_ptrs = lambda seat: ()
    pol._kw = lambda c: frozenset(c.keywords)
    return pol


def rec(n):
    return {"records": [{"verb": "GiveKreditsBySide", "args": [int(ME), n, 0], "tainted": False}]}


def main():
    # ---------------- 转化 ----------------
    order = card(40, "ORDER", "hand", ME)
    old = card(8, "OLD", "frontline", OPP)
    w = card(20, "W", "back", ME)
    cards = [order, old, w]
    O = ovr({8: {"OnLeaveBoardOrOwner"}, 20: {"OnOtherCardLeaveBoardOrOwner", "OnOtherCardConverted"}})
    cv = {"ids": [8], "name": "NEWNAME", "atk": 1, "dfn": 1, "cost": 1, "opc": 1, "typ": "infantry", "kw": [],
          "instigator": 40, "skip_trigger": False}
    sim = Sim({8: U(8, OPP, "frontline", 3, 3, 3, "infantry")}, {ME: 20, OPP: 20}, 5.0,
              {40: H(40, "ORDER", 3, "order", eff={"convert": cv})}, my_side=ME)

    def beh(h, c, a):
        return rec({"OnOtherCardLeaveBoardOrOwner": 2, "OnOtherCardConverted": 3}.get(h, 0)) if h in (
            "OnOtherCardLeaveBoardOrOwner", "OnOtherCardConverted") else {}
    pol = mk_pol(O)
    with patched(O, beh):
        pol._finish_event_fx(sim, St(cards))
    tbl = sim.event_fx["convert"]
    key = (40, (8,), "NEWNAME")
    chk("convert：键 = (发起牌, 旧牌元组, 目标名)；旧牌离场后果按旧牌 id 分开、0x22 后果在 after", list(tbl) == [key]
        and tbl[key]["old"].get(8, {}).get("kredit") == 2 and tbl[key]["after"].get("kredit") == 3, str(tbl))
    chk("convert：没有新牌实例 ⇒ 缺口写进该条", any("新牌实例不存在" in g for g in tbl[key]["gaps"]), str(tbl[key]["gaps"]))
    s = sim.copy()
    _apply_eff(s, {"convert": dict(cv)}, None)
    chk("convert：端到端——sim 消费这张表（旧牌后果 +2 指挥点；0x22 后果 +3），旧牌换成新牌", s.kredits == 10.0
        and 8 not in s.units and len(s.units) == 1 and any("新牌实例不存在" in g for g in s.gaps), str((s.kredits, s.gaps)))
    sim_nocv = Sim({8: U(8, OPP, "frontline", 3, 3, 3, "infantry")}, {ME: 20, OPP: 20}, 5.0,
                   {40: H(40, "ORDER", 3, "order", eff={"damage_hq": 1})}, my_side=ME)
    with patched(O, beh):
        pol._finish_event_fx(sim_nocv, St(cards))
    chk("没有转化效果 ⇒ convert 表为空；没有伤害 pair/关键词变化 ⇒ damage / abilities 表为空",
        sim_nocv.event_fx["convert"] == {} and sim_nocv.event_fx["damage"] == {} and sim_nocv.event_fx["abilities"] == {})

    # ---------------- 效果伤害 ----------------
    d = card(50, "D", "hand", ME)
    t = card(60, "T", "frontline", OPP)
    wd = card(61, "WD", "back", OPP)
    O2 = ovr({50: {"OnCardDealDamage_ModifyDamageDealt"}, 61: {"OnOtherCardDealDamage"}})

    def beh2(h, c, a):
        if h == "OnCardDealDamage_ModifyDamageDealt":
            return {"out": {"newDamage": a["Damage"] + 2}}
        if h == "OnOtherCardDealDamage":
            return rec(7)
        return {}
    sim2 = Sim({60: U(60, OPP, "frontline", 1, 9, 1, "infantry")}, {ME: 20, OPP: 20}, 5.0,
               {50: H(50, "D", 2, "order")}, legal={50: [60]}, pair_eff={(50, 60): {"damage": 3}}, my_side=ME)
    pol2 = mk_pol(O2)
    with patched(O2, beh2):
        pol2._finish_event_fx(sim2, St([d, t, wd]))
    fx = sim2.event_fx["damage"].get((50, 60))
    chk("damage：键 = (来源牌, 目标)；final = 3+2 = 5；基数 amount=3 随条目存下（sim 用它防错配）",
        fx is not None and fx["final"] == 5 and fx["amount"] == 3 and fx["buckets"].get("dealt", {}).get("kredit") == 7, str(fx))
    chk("damage：伤害来源是『打出的牌』的假定如实写进 gaps", any("damagerCardID" in g for g in fx["gaps"]), str(fx["gaps"]))
    s = sim2.copy()
    _apply_eff(s, {"damage": 3}, 60, src=50)
    chk("damage：端到端——sim 按 5 点扣防（9→4），造成伤害桶 +7 指挥点（5→12）", s.units[60].dfn == 4 and s.kredits == 12.0,
        str((s.units[60].dfn, s.kredits)))
    # 对打：_attach_fight_dmg 留下的表被并进来
    sim3 = Sim({70: U(70, ME, "frontline", 3, 5, 1, "infantry"), 71: U(71, OPP, "frontline", 2, 5, 1, "infantry")},
               {ME: 20, OPP: 20}, 5.0, {50: H(50, "D", 2, "order", eff={"fight": [70, 71], "fight_dmg": (3, 2)})}, my_side=ME)
    pol3 = mk_pol({})
    pol3.__dict__["_fightfx_tbl"] = {("fight", 70, 71): {"to_b": 3, "to_a": 2, "buckets": {}, "gaps": []},
                                     ("fight", 1, 2): {"to_b": 0, "to_a": 0, "buckets": {}, "gaps": []}}
    with patched({}, None):
        pol3._finish_event_fx(sim3, St([card(70, "A", "frontline", ME), card(71, "B", "frontline", OPP)]))
    chk("damage：对打表只并进本盘面候选里出现的那一对", list(sim3.event_fx["damage"]) == [("fight", 70, 71)],
        str(list(sim3.event_fx["damage"])))

    # ---------------- 能力变化 ----------------
    x = card(80, "X", "frontline", ME)
    l1 = card(81, "L1", "back", OPP)
    O4 = ovr({81: {"OnOtherCardAbilitiesChanged"}})
    seen = []

    def beh4(h, c, a):
        seen.append(a)
        return rec(5)
    sim4 = Sim({80: U(80, ME, "frontline", 2, 2, 1, "infantry")}, {ME: 20, OPP: 20}, 5.0,
               {41: H(41, "GIVE", 1, "order", eff={"give": ["guard", "immune"], "remove_blitz": True, "remove_immune": True})},
               my_side=ME)
    pol4 = mk_pol(O4)
    with patched(O4, beh4):
        pol4._finish_event_fx(sim4, St([x, l1]))
    tb = sim4.event_fx["abilities"]
    chk("abilities：只对候选里出现的 (方向, 关键词) 算，且是原版会广播的那 7 个（immune 不算；单位没有 blitz ⇒ 移除不广播）",
        set(k for k in tb if k != ("gap",)) == {(80, "+guard")} and tb[(80, "+guard")].get("kredit") == 5, str(tb))
    chk("abilities：键里带「缺口」条目（移除按唯一给予者算）", tb.get(("gap",)) and "唯一给予者" in tb[("gap",)][-1], str(tb.get(("gap",))))
    s = sim4.copy()
    _apply_eff(s, {"give": ["guard"]}, 80)
    chk("abilities：端到端——赋予 guard ⇒ 0x1D 后果 +5 指挥点", s.kredits == 10.0, str(s.kredits))
    pol5 = mk_pol({})
    with patched({}, beh4):
        pol5._finish_event_fx(sim4, St([x, l1]))
    chk("abilities：场上没有任何牌覆写 0x1D ⇒ 空表（零开销）", sim4.event_fx["abilities"] == {})

    # ---------------- 单族出错不拖垮别的表 ----------------
    pol6 = mk_pol(O)

    def boom(st, sim_):
        raise RuntimeError("故意炸")
    pol6._damage_fx = boom
    sim6 = Sim({8: U(8, OPP, "frontline", 3, 3, 3, "infantry")}, {ME: 20, OPP: 20}, 5.0,
               {40: H(40, "ORDER", 3, "order", eff={"convert": cv})}, my_side=ME)
    with patched(O, beh):
        pol6._finish_event_fx(sim6, St(cards))
    chk("某一族抛异常 ⇒ 该表为空 + fx_meta 记 error；convert 表照常", sim6.event_fx["damage"] == {}
        and "故意炸" in pol6.fx_meta["damage"]["error"] and key in sim6.event_fx["convert"], str(pol6.fx_meta.get("damage")))
    pol7 = mk_pol(O)
    pol7.P["use_vm"] = False
    sim7 = Sim({8: U(8, OPP, "frontline", 3, 3, 3, "infantry")}, {ME: 20, OPP: 20}, 5.0,
               {40: H(40, "ORDER", 3, "order", eff={"convert": cv})}, my_side=ME)
    pol7._finish_event_fx(sim7, St(cards))
    chk("VM 关闭 ⇒ 三张表都是空表（sim 记缺口，不编）", all(sim7.event_fx[k] == {} for k in ("convert", "damage", "abilities")))
    print("失败 %d 项" % chk.fails)
    return chk.fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
