#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本里**刚生成的牌**能被后续原生解析（2026-10-06，CARRIER BATTLE / HELL ON WHEELS / RALLY CALEDONIA / IRON VICTORY）
+ `GiveRandomCombatKeyword` 的直跑 sink / 候选集合（FORGED IN FIRE）。

原版依据（1.58.27125 导出）：
  * `BP_CardFunctions.cpp:377` `GetCardFromID(id)` → `BP_GameState_Battle.cpp:1537` `FetchCardFromCardID`：id <= 0 ⇒ nullptr，否则查 `AllCardsInBattle`；
  * `card_event_rally_caledonia.cpp`：`SpawnCardOnBattlefield(…,&spawnedCardID)` → `GetCardFromID` → `ChangeAttack/ChangeDefense(tempCard, count)`；
  * `card_event_iron_victory.cpp`：`SpawnCardInHandBySide(…,&id)` → `ChangeOperationCost(card, 1, SetValue)`；
  * `card_event_carrier_battle.cpp`：对方手牌 `SpawnCardInHandBySide` → `ChangeKreditCost(spawnedCard)` / `JSON_SetInt(spawnedCard)`；
  * `BP_CardFunctions.cpp:22683` `GiveRandomCombatKeyword`（候选集合见 `engine/natives/combat_kw.py`）。
三条路：A = 字典路（Recorder → to_effects → sim._apply_eff）、B = 直跑 sink、C = 重放（engine.calls.apply_calls）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engine.effectvm as EV                                       # noqa: E402
from engine import calls as CALLS                                  # noqa: E402
from engine import scripts as SC                                   # noqa: E402
from engine import state as S                                      # noqa: E402
from engine.natives import combat_kw as CK                         # noqa: E402
from engine.spawned import SpawnedCards                            # noqa: E402
from kardsmem.kismetlib import FALLTHROUGH, is_fallthrough         # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402

ME, OPP = 1, 2
fails = 0
_STAT = {"atk": 1, "dfn": 1, "cost": 1, "typ": "infantry", "kw": ()}


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))
    if not ok:
        fails += 1


class _Kid:
    def __init__(self, prop):
        self.args = {"prop": prop}


class _E:
    def __init__(self, n=12):
        self.kids = [_Kid("p%d" % i) for i in range(n)]


class _Fr:
    def __init__(self):
        self.locals = {}


def mk(**kw):
    s = S.Sim({}, {ME: 20, OPP: 20}, 5, {}, my_side=ME, slots=3, kredit_max=24, spawn_stats=lambda n: _STAT, **kw)
    s.opp_slots = 0
    return s


GATE = {"buffable": True, "unrevealed_covert": False}


def _direct(state):
    rec = EV.Recorder(0, 0, None)
    rec.spawn_gate = lambda info: dict(GATE)
    hooks, ctx = EV._direct_hooks(state, rec, rec.hooks(), ME, lambda n: _STAT)
    return rec, hooks, ctx


def _spawn_args(name="card_unit_routed_troops", side=ME):
    # SpawnCardOnBattlefield(side, Frontline, name, spawnerID, text, bool, -1, 0, False, False, &spawnedCardID)
    return [side, False, name, 50001, "", True, -1, 0, False, False, None]


def main():
    # ---- 软钩子 / 登记表 ----
    chk("FALLTHROUGH 认得出（含热重载后的同名类）", is_fallthrough(FALLTHROUGH) and not is_fallthrough(None))
    reg = SpawnedCards()
    h1 = reg.register(-2001, "card_unit_a", 1, "board", "back")
    chk("登记表：句柄 ↔ id 双向、同 id 复用句柄", reg.handle_of(-2001) == h1 and reg.info_of_handle(h1)["name"] == "card_unit_a"
        and reg.register(-2001, "card_unit_a", 1, "board", "back") == h1 and reg.handle_of(5) is None)

    # ---- B：直跑 spawn → FetchCardFromCardID(软钩子) → ChangeAttack/ChangeDefense(句柄) ----
    sB = mk()
    recB, hB, ctxB = _direct(sB)
    fr = _Fr()
    hB["SpawnCardOnBattlefield"](None, fr, None, _spawn_args(), _E(11))
    uid = fr.locals["p10"]
    chk("直跑：生成单位、出参 spawnedCardID 是它的 id", uid in sB.units and uid < 0, str(sB.units))
    fr2 = _Fr()
    r = hB["FetchCardFromCardID"](None, fr2, None, [uid, None], _E(2))
    hdl = fr2.locals["p1"]
    chk("软钩子：新牌 id ⇒ 合成句柄；ptr_ids/门数据登记", not is_fallthrough(r) and recB.ptr_ids.get(hdl) == uid
        and recB.cur_stats.get(hdl) == GATE, "%s %s" % (r, recB.ptr_ids))
    chk("软钩子：真牌 id（不在登记表）⇒ FALLTHROUGH（交还游戏字节码）", is_fallthrough(hB["FetchCardFromCardID"](None, _Fr(), None, [77, None], _E(2))))
    chk("软钩子：id<=0 且未登记 ⇒ FALLTHROUGH（BP 自己回 nullptr）", is_fallthrough(hB["FetchCardFromCardID"](None, _Fr(), None, [0, None], _E(2))))
    hB["ChangeAttack"](None, _Fr(), None, [hdl, 50001, 3, 1, False, None], _E(6))
    hB["ChangeDefense"](None, _Fr(), None, [hdl, 50001, 2, 1, False, None], _E(6))
    uB = sB.units[uid]
    chk("直跑：对句柄 ChangeAttack/ChangeDefense 落到新单位上（1/1 → 4/3），无缺口", (uB.atk, uB.dfn) == (4, 3) and not ctxB.gaps,
        "%s %s" % ((uB.atk, uB.dfn), ctxB.gaps))

    # ---- A：同一串调用走录制器 → to_effects → _apply_eff ----
    recA = EV.Recorder(0, 0, None)
    recA.spawn_gate = lambda info: dict(GATE)
    hA = recA.hooks()
    frA = _Fr()
    hA["SpawnCardOnBattlefield"](None, frA, None, _spawn_args(), _E(11))
    tid = frA.locals["p10"]
    frA2 = _Fr()
    hA["FetchCardFromCardID"](None, frA2, None, [tid, None], _E(2))
    hdlA = frA2.locals["p1"]
    hA["ChangeAttack"](None, _Fr(), None, [hdlA, 50001, 3, 1, False, None], _E(6))
    hA["ChangeDefense"](None, _Fr(), None, [hdlA, 50001, 2, 1, False, None], _E(6))
    effA = EV.to_effects(recA, my_side=ME)
    chk("字典路：对句柄的改动并回 spawn_cards[i]['ops']，不再套到选定目标（没有 buff 标量）",
        effA.get("spawn_cards") and effA["spawn_cards"][0].get("ops") == [("attack", 3, 1), ("defense", 2, 1)] and "buff" not in effA,
        str(effA))
    sA = mk()
    _apply_eff(sA, dict(effA), None)
    uA = list(sA.units.values())[0]
    chk("A ≡ B：新单位的攻防一致（4/3）", (uA.atk, uA.dfn) == (4, 3), str((uA.atk, uA.dfn)))

    # ---- C：重放 ----
    sC = mk()
    res = CALLS.apply_calls(sC, list(ctxB.applied))
    a, c = SC._state_snapshot(sB), SC._state_snapshot(sC)
    chk("C ≡ B（spawn + change_attack + change_defense 重放）", a == c and not res.gaps,
        "%s %s" % ({k: (a.get(k), c.get(k)) for k in set(a) | set(c) if a.get(k) != c.get(k)}, res.gaps))

    # ---- 对方手牌：CARRIER BATTLE ----
    sB2 = mk()
    recB2, hB2, ctxB2 = _direct(sB2)
    fr = _Fr()
    # SpawnCardInHandBySide(side, name, spawnerID, seen, skipAnim, fromOpp, text, campaign, faction, &spawnedCardID)
    hB2["SpawnCardInHandBySide"](None, fr, None, [OPP, "card_event_carrier_battle", 50000, True, False, False, "", None, 0, None], _E(10))
    oid = fr.locals["p9"]
    chk("直跑：对方手牌 ⇒ opp_cards+1、出参是可解析的 id（不再是 None ⇒ LetBool TypeError）", sB2.opp_cards == 1 and isinstance(oid, int) and oid < 0
        and ("opp_gain_hand", "card_event_carrier_battle") in ctxB2.applied, str((sB2.opp_cards, oid, ctxB2.applied)))
    fr = _Fr()
    hB2["FetchCardFromCardID"](None, fr, None, [oid, None], _E(2))
    hdl2 = fr.locals["p1"]
    hB2["ChangeKreditCost"](None, _Fr(), None, [hdl2, 50000, 1, 1, False, None], _E(7))
    chk("直跑：对方手牌新牌的 ChangeKreditCost 如实记缺口（没有逐张状态），不是静默/崩", any("对方手牌" in g for g in ctxB2.gaps), str(ctxB2.gaps))
    sC2 = mk()
    CALLS.apply_calls(sC2, list(ctxB2.applied))
    chk("C ≡ B（对方手牌 opp_cards）", sC2.opp_cards == 1)

    # ---- 我方手牌：IRON VICTORY（行动费设为 1） ----
    sB3 = mk()
    recB3, hB3, ctxB3 = _direct(sB3)
    fr = _Fr()
    hB3["SpawnCardInHandBySide"](None, fr, None, [ME, "card_unit_t_34", 50002, True, False, False, "", None, 0, None], _E(10))
    cid = fr.locals["p9"]
    fr = _Fr()
    hB3["FetchCardFromCardID"](None, fr, None, [cid, None], _E(2))
    hdl3 = fr.locals["p1"]
    hB3["ChangeOperationCost"](None, _Fr(), None, [hdl3, 50002, 1, 2, False, False, False], _E(7))
    chk("直跑：手牌新牌 ChangeOperationCost(SetValue 1) ⇒ H.opc = 1", sB3.hand[cid].opc == 1 and ("hand_opcost", cid, 1, 2) in ctxB3.applied,
        str((getattr(sB3.hand.get(cid), "opc", "?"), ctxB3.applied)))
    sC3 = mk()
    res = CALLS.apply_calls(sC3, list(ctxB3.applied))
    chk("C ≡ B（gain_hand + hand_opcost）", SC._state_snapshot(sB3) == SC._state_snapshot(sC3) and not res.gaps, str(res.gaps))
    recA3 = EV.Recorder(0, 0, None)
    recA3.spawn_gate = lambda info: dict(GATE)
    hA3 = recA3.hooks()
    fr = _Fr()
    hA3["SpawnCardInHandBySide"](None, fr, None, [ME, "card_unit_t_34", 50002, True, False, False, "", None, 0, None], _E(10))
    fr2 = _Fr()
    hA3["FetchCardFromCardID"](None, fr2, None, [fr.locals["p9"], None], _E(2))
    hA3["ChangeOperationCost"](None, _Fr(), None, [fr2.locals["p1"], 50002, 1, 2, False, False, False], _E(7))
    eff3 = EV.to_effects(recA3, my_side=ME)
    sA3 = mk()
    _apply_eff(sA3, dict(eff3), None)
    chk("A ≡ B：手牌新牌行动费一致", [h.opc for h in sA3.hand.values()] == [1] and SC._state_snapshot(sA3) == SC._state_snapshot(sB3),
        str((eff3, SC._state_snapshot(sA3), SC._state_snapshot(sB3))))

    # ---- 卡名 None（URAL FACTORIES 候选池为空）：不生成，也不凭空造 2/2 ----
    recN = EV.Recorder(0, 0, None)
    recN.hooks()["SpawnCardOnBattlefield"](None, _Fr(), None, _spawn_args("None"), _E(11))
    effN = EV.to_effects(recN, my_side=ME)
    sN = mk()
    _apply_eff(sN, dict(effN), None)
    chk("卡名为 None ⇒ 字典路不生成任何单位（旧：generic 2/2）", not sN.units and not effN.get("spawn"), str(effN))

    # ---- GiveRandomCombatKeyword：候选集合 ----
    V = CK.valid_combat_keywords
    chk("候选：后排步兵无关键词 ⇒ 7 个全有", V(set(), in_frontline=False, is_fighter=False, has_guard=False) == list(CK.COMBAT_KEYWORD_ORDER))
    chk("候选：前线 / 战斗机 / 守护 ⇒ 去掉烟幕",
        all("smokescreen" not in V(set(), in_frontline=f, is_fighter=g, has_guard=h)
            for f, g, h in ((True, False, False), (False, True, False), (False, False, True))))
    chk("候选：剔除现有关键词（含 armor>0 的重甲）",
        V(CK.existing_combat_keywords({"blitz", "guard", "veteran"}, 1), in_frontline=False, is_fighter=False, has_guard=True)
        == ["ambush", "fury", "shock"])

    def unit_state():
        s = mk()
        s.units[11] = S.U(11, ME, "back", 2, 2, 2, "infantry", ("blitz",))
        return s

    class _Rng:
        def __init__(self, k):
            self.k = k

        def wrapper_int(self, lo, hi):
            return min(self.k, hi)

    sK = unit_state()
    hK = SC.native_hooks(sK, my_side=ME, rng=_Rng(1), gates={0x500: dict(GATE)}, ptr_ids={0x500: 11})
    frK = _Fr()
    hK["GiveRandomCombatKeyword"](None, frK, None, [11, 50000, None, None], _E(4))
    # 候选（后排、有 blitz）= ambush, fury, guard, heavyarmor, shock, smokescreen；流给 1 ⇒ fury
    chk("直跑：用牌局随机流从过滤后的候选里选（流=1 ⇒ fury）、出参 success", "fury" in sK.units[11].kw and frK.locals["p3"] is True
        and ("give_kw", 11, "fury") in hK["__ctx__"].applied, str((sK.units[11].kw, frK.locals)))
    sK2 = unit_state()
    pick_log = []
    hK2 = SC.native_hooks(sK2, my_side=ME, gates={0x500: dict(GATE)}, ptr_ids={0x500: 11})
    hK2["__ctx__"].pick = lambda verb, n: (pick_log.append((verb, n)), 3)[1]
    hK2["GiveRandomCombatKeyword"](None, _Fr(), None, [11, 50000, None, None], _E(4))
    chk("直跑：没有活流 ⇒ 走枚举回调（域大小 = 过滤后候选数 6）、选第 3 个 = heavyarmor ⇒ 重甲 +1",
        pick_log == [("GiveRandomCombatKeyword", 6)] and sK2.units[11].armor == 1, str((pick_log, sK2.units[11].armor)))
    sK3 = unit_state()
    hK3 = SC.native_hooks(sK3, my_side=ME, rng=_Rng(0), gates={0x500: {"buffable": False}}, ptr_ids={0x500: 11})
    frK3 = _Fr()
    hK3["GiveRandomCombatKeyword"](None, frK3, None, [11, 50000, None, None], _E(4))
    chk("直跑：CanCardBeBuffed 为假 ⇒ 什么都不给、success=false", sK3.units[11].kw == frozenset({"blitz"}) and frK3.locals["p3"] is False)
    sKc = unit_state()
    resK = CALLS.apply_calls(sKc, list(hK["__ctx__"].applied))
    chk("C ≡ B（随机关键词重放）", "fury" in sKc.units[11].kw and not resK.gaps, str(resK.gaps))
    # A 路（录制钩子）与 B 用同一个候选集合：登记成同大小的随机点 / 有活流时确定地选
    class _CN:
        def _view(self, ptr):
            return {"location_enum": 5}

        def call(self, name, ptr):
            return {"GetCombatKeywords": ([2], 1), "IsFighter": False, "getHasGuard": False}[name]

    class _VM:
        cn = _CN()

    recR = EV.Recorder(0, 0, None)
    recR.ptr_ids[0x500] = 11
    recR.cur_stats[0x500] = dict(GATE)
    frR = _Fr()
    recR.hook("GiveRandomCombatKeyword")(_VM(), frR, None, [11, 50000, None, None], _E(4))
    chk("A：无活流 ⇒ 随机点域大小 = 6（与 B 同）、记成 chance；第 0 个 = ambush",
        recR.nodes == [{"verb": "GiveRandomCombatKeyword", "size": 6}] and recR.chance == ["GiveRandomCombatKeyword"]
        and recR.records[-1]["verb"] == "GiveAmbush" and frR.locals["p3"] is True, str((recR.nodes, recR.records)))
    recR2 = EV.Recorder(0, 0, [3])
    recR2.ptr_ids[0x500] = 11
    recR2.cur_stats[0x500] = dict(GATE)
    recR2.hook("GiveRandomCombatKeyword")(_VM(), _Fr(), None, [11, 50000, None, None], _E(4))
    chk("A：forced=[3] ⇒ 第 3 个候选 = heavyarmor（与 B 的枚举回调同序）", recR2.records[-1]["random_kw"] == "heavyarmor")
    recR3 = EV.Recorder(0, 0, None)
    recR3.stream = _Rng(2)
    recR3.ptr_ids[0x500] = 11
    recR3.cur_stats[0x500] = dict(GATE)
    recR3.hook("GiveRandomCombatKeyword")(_VM(), _Fr(), None, [11, 50000, None, None], _E(4))
    chk("A：有活流 ⇒ 确定地选（流=2 ⇒ guard）、不登记随机点", recR3.records[-1]["random_kw"] == "guard" and not recR3.nodes and recR3.exact)
    recR4 = EV.Recorder(0, 0, None)
    recR4.ptr_ids[0x500] = 11
    recR4.cur_stats[0x500] = {"buffable": False}
    recR4.hook("GiveRandomCombatKeyword")(_VM(), _Fr(), None, [11, 50000, None, None], _E(4))
    chk("A：CanCardBeBuffed 为假 ⇒ 没有候选、什么都不记", not recR4.records and not recR4.nodes)
    print("fails=%d" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
