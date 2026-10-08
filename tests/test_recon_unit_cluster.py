#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线对账『单位簇 / 重放缺口簇』（2026-10-06 `--orders all` 之后）的回归：

A 路（`effectvm.to_effects` → `sim.engine._apply_eff`）这几处**逐张记账**键：`damage_ids` / `give_ids` / `pin_ids` / `heal_ids` /
`fights` / 刚生成单位的临时 id（`GiveBlitz(spawnedCardID)` 并回 spawn_cards）/ `DamageCard` 的出参 `targetDestroyed`；
B/C 路：`engine.calls` 新增的 `fight` / `convert_*` / `steal` / `deck_add` / `deck_order` / `unit_to_deck`（与 sink 同一原生，三角验证）。
每条断言都对着 BP 原文（引用见各处注释），不是照抄实现。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import calls as CALLS                                    # noqa: E402
from engine import scripts as SC                                     # noqa: E402
from engine import state as S                                        # noqa: E402
from engine.effectvm import Recorder, to_effects                     # noqa: E402

ME, OPP = 1, 2
P1, P2 = 0x7001, 0x7002
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def call(rec, verb, *args, frame=None, e=None):
    return rec.hook(verb)(None, frame, None, list(args), e)


def out_frame(idx, prop="out"):
    e = types.SimpleNamespace(kids=[types.SimpleNamespace(args={"prop": prop})] * (idx + 1))
    return types.SimpleNamespace(locals={}), e


def mk(units=(), kredits=5):
    s = S.Sim({}, {ME: 20, OPP: 20}, kredits, {}, my_side=ME, slots=3, kredit_max=24)
    for u in units:
        s.units[u.id] = u
    s.playing_side = ME
    return s


def unit(uid, side=ME, atk=3, dfn=4, row="frontline", kw=()):
    return S.U(uid, side, row, atk, dfn, 1, "infantry", kw=kw)


def tri(verb, args, mk_state, *, ptr_ids=None, payload=None, rng=None, spawn_stat=None):
    """直跑 ≡ 直跑→applied→重放（同 `test_calls_replay.tri`）；返回 (ok, why, applied)。"""
    s1 = mk_state()
    h1 = SC.native_hooks(s1, ptr_ids=ptr_ids or {}, my_side=ME, payload=payload, rng=rng, spawn_stat=spawn_stat)
    ctx = h1["__ctx__"]
    h1[verb](None, None, None, list(args), None)
    applied = list(ctx.applied)
    if not applied:
        return False, "直跑没产出 applied gaps=%s" % (ctx.gaps[:2],), applied
    s2 = mk_state()
    res = CALLS.apply_calls(s2, applied)
    a, b = SC._state_snapshot(s1), SC._state_snapshot(s2)
    diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
    extra = [g for g in res.gaps if g not in ctx.gaps]
    return (not diff and not extra), "diff=%s extra_gaps=%s applied=%s" % (diff, extra[:2], applied[:3]), applied


def main():
    TARGET, SELF = 0x1000, 0x2000

    # ============ A 路：to_effects 的逐张记账键 ============
    # RUSH（card_event_rush）：对一个友方单位 1 伤 + 对随机敌方单位 4 伤 —— 后一次不是选定目标，不能被标量 `damage` 吞掉
    r = Recorder(SELF, TARGET)
    r.ptr_ids.update({TARGET: 11, 0xB: 12})
    call(r, "DamageCard", TARGET, 1, SELF, False, False, False, None)
    call(r, "DamageCard", 0xB, 4, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("DamageCard：选定目标 ⇒ 标量 damage；认得出 id 的**非**选定目标 ⇒ damage_ids（RUSH）",
        e.get("damage") == 1 and e.get("damage_ids") == {12: 4}, str(e))
    # MEN OF STEEL（"你的单位 +1 重甲"，无选定目标）：标量 armor 套不到每个单位 ⇒ armor_ids 逐张；sim 逐张结算
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 11, 0xB: 12})
    call(r, "ChangeHeavyArmor", 0xA, SELF, 1, 0, False, False, False)
    call(r, "ChangeHeavyArmor", 0xB, SELF, 1, 0, False, False, False)
    e = to_effects(r, my_side=1)
    chk("ChangeHeavyArmor ×N 无选定目标 ⇒ armor_ids 逐张（MEN OF STEEL）", e.get("armor_ids") == {11: 1, 12: 1}, str(e))
    import sim.engine as _SE
    from sim.state import Sim as _Sim, U as _U
    _s = _Sim({}, {1: 20, 2: 20}, 5.0, {}, my_side=1)
    for _i in (11, 12):
        _s.units[_i] = _U(_i, 1, "frontline", 3, 4, 1, "infantry")
    _SE._apply_eff(_s, dict(e), None, src=None)
    chk("sim：armor_ids 逐张 +1 重甲（两个单位都加，标量不重复套）",
        _s.units[11].armor == 1 and _s.units[12].armor == 1, "%s %s" % (_s.units[11].armor, _s.units[12].armor))
    # RASPUTITSA（"每个单位受等于其行动费的伤害"）：无目标、数值各不相同
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 11, 0xB: 12})
    call(r, "DamageCard", 0xA, 3, SELF, False, False, False, None)
    call(r, "DamageCard", 0xB, 1, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("DamageCard ×N 无选定目标 ⇒ damage_ids 逐张（RASPUTITSA），不再有标量 damage",
        e.get("damage_ids") == {11: 3, 12: 1} and "damage" not in e, str(e))
    r = Recorder(SELF, 0)
    call(r, "DamageCard", 0xC, 2, SELF, False, False, False, None)               # 指针认不出 id ⇒ 旧口径（标量）
    chk("DamageCard 目标指针认不出 id ⇒ 退回旧的标量 damage（不静默丢）", to_effects(r, my_side=1).get("damage") == 2)

    # DamageCard 出参 targetDestroyed（BREAKTHROUGH：`if (!targetDestroyed) return;` 之后才给所有友方单位 -1 行动费；
    # BP card_event_breakthrough.cpp:Label_403；`_isDestroyed = 总防 <= 0` BP_CardFunctions.cpp:16468-16472）
    for dfn, amt, want in ((3, 3, True), (4, 3, False), (3, 5, True)):
        r = Recorder(SELF, TARGET)
        r.cur_stats[TARGET] = {"defense": dfn}
        fr, ee = out_frame(6, "targetDestroyed")
        call(r, "DamageCard", TARGET, amt, SELF, False, False, False, None, frame=fr, e=ee)
        chk("录制路写 DamageCard 出参 targetDestroyed：总防 %d 吃 %d ⇒ %s" % (dfn, amt, want),
            fr.locals.get("targetDestroyed") is want, str(fr.locals))
    r = Recorder(SELF, TARGET)
    fr, ee = out_frame(6, "targetDestroyed")
    call(r, "DamageCard", TARGET, 3, SELF, False, False, False, None, frame=fr, e=ee)
    chk("读不到目标当前防御 ⇒ 不写出参（不猜）", "targetDestroyed" not in fr.locals)

    # ADMIRAL YAMAMOTO：所有友方空军 +1 攻 + 闪击 —— `GiveBlitz(int cardID, …)` 收 **ID**
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 7})
    call(r, "GiveBlitz", 7, SELF)
    e = to_effects(r, my_side=1)
    chk("GiveBlitz(非选定目标的 id) ⇒ give_ids（不再套到选定目标上）",
        e.get("give_ids") == {7: ["blitz"]} and "give" not in e, str(e))
    r = Recorder(SELF, TARGET)
    r.ptr_ids.update({TARGET: 11})
    call(r, "GiveBlitz", 11, SELF)
    chk("GiveBlitz(选定目标的 id) ⇒ 仍是标量 give", to_effects(r, my_side=1).get("give") == ["blitz"])
    # HEL：`GiveImmune(己方总部卡 id, 目标 id)`（card_event_hel.cpp:Label_603）——授予的是**总部**
    r = Recorder(SELF, TARGET)
    r.hq_own = frozenset([0x3000])
    r.ptr_ids.update({TARGET: 11, 0x3000: 1})
    call(r, "GiveImmune", 1, 11, None)
    e = to_effects(r, my_side=1)
    chk("GiveImmune(总部卡 id) ⇒ 不给目标单位 immune（HEL 旧口径套错对象），并记缺口",
        "give" not in e and "give_ids" not in e and any("总部" in g for g in r.gaps), "%s gaps=%s" % (e, r.gaps))

    # ENCIRCLEMENT：`SpawnCardOnBattlefield(..., &spawnedCardID)` 后紧跟 `GiveBlitz(spawnedCardID)`
    r = Recorder(SELF, TARGET)
    r.ptr_ids.update({TARGET: 11})
    fr, ee = out_frame(10, "spawnedCardID")
    call(r, "SpawnCardOnBattlefield", 1, True, "card_unit_x", SELF, None, True, -1, 0, False, False, None, frame=fr, e=ee)
    tmp = fr.locals.get("spawnedCardID")
    call(r, "GiveBlitz", tmp, SELF)
    e = to_effects(r, my_side=1)
    sc = (e.get("spawn_cards") or [{}])[0]
    chk("单张生成的出参 spawnedCardID 得到临时 id，GiveBlitz(它) 并回 spawn_cards.kw_add（不再给选定目标）",
        isinstance(tmp, int) and tmp < 0 and sc.get("kw_add") == ["blitz"] and "give" not in e and "give_ids" not in e, str(e))

    # MONTY / EXHAUST ALL OPTIONS：定住目标**及相邻单位**（`PinUnit(int cardID, …)` 收 ID）
    r = Recorder(SELF, TARGET)
    r.ptr_ids.update({TARGET: 11, 0xB: 12})
    call(r, "PinUnit", 11, SELF)
    call(r, "PinUnit", 12, SELF)
    e = to_effects(r, my_side=1)
    chk("PinUnit ×2：选定目标 ⇒ pin；另一个 ⇒ pin_ids", e.get("pin") is True and e.get("pin_ids") == [12], str(e))
    # CADET NURSE CORPS：完全修复所有友方单位（`FullyHealCard(UBaseCardObject* card, …)` 收**指针**）
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 11, 0xB: 12})
    call(r, "FullyHealCard", 0xA, SELF, None)
    call(r, "FullyHealCard", 0xB, SELF, None)
    e = to_effects(r, my_side=1)
    chk("FullyHealCard ×2（无选定目标）⇒ heal_ids 逐张", e.get("heal_ids") == [11, 12] and "heal" not in e, str(e))

    # COUNTERATTACK <german>：每个友方单位各对一个随机敌方单位（MakeCardsFight ×N）
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 11, 0xB: 7, 0xC: 55})
    call(r, "MakeCardsFight", 0xA, 0xC, SELF, None)
    call(r, "MakeCardsFight", 0xB, 0xC, SELF, None)
    e = to_effects(r, my_side=1)
    chk("MakeCardsFight ×2 ⇒ fights 有序对列表（fight 仍是最后一对）",
        e.get("fights") == [[11, 55], [7, 55]] and e.get("fight") == [7, 55], str(e))
    r = Recorder(SELF, 0)
    r.ptr_ids.update({0xA: 11, 0xC: 55})
    call(r, "MakeCardsFight", 0xA, 0xC, SELF, None)
    chk("MakeCardsFight ×1 ⇒ 没有 fights（旧形状不变）", "fights" not in to_effects(r, my_side=1))

    # ---- 消费侧（sim.engine._apply_eff）----
    import sim.engine as en
    s = mk([unit(11, ME, 3, 4), unit(12, OPP, 2, 4), unit(13, OPP, 2, 9)])
    en._apply_eff(s, {"damage_ids": {12: 4, 13: 3}}, None)
    chk("_apply_eff(damage_ids)：逐张结算（12 被打死、13 掉血）", 12 not in s.units and s.units[13].dfn == 6)
    s = mk([unit(11, ME, 3, 3), unit(12, ME, 5, 2)])
    s.units[11].dfn, s.units[12].dfn = 1, 1
    s.units[11].mdef, s.units[12].mdef = 4, 5
    en._apply_eff(s, {"heal_ids": [11, 12], "buff": [2, 2], "buff_ids": {11: [1, 1], 12: [1, 1]}}, None)
    chk("_apply_eff(heal_ids + buff_ids)：先修复到 maxDefense 再 +1+1（BP 顺序，CADET NURSE CORPS）",
        s.units[11].dfn == 5 and s.units[12].dfn == 6, "%s %s" % (s.units[11].dfn, s.units[12].dfn))
    s = mk([unit(11, ME), unit(12, ME)])
    s.units[11].sick = True
    en._apply_eff(s, {"give_ids": {11: ["blitz"]}, "pin_ids": [12]}, None)
    chk("_apply_eff(give_ids/pin_ids)：11 得闪击（sick=False）、12 被定住",
        "blitz" in s.units[11].kw and s.units[11].sick is False and s.units[12].pinned is True)
    s = mk([unit(11, ME, 6, 4), unit(7, ME, 5, 3), unit(55, OPP, 2, 1)])
    en._apply_eff(s, {"fight": [7, 55], "fights": [[11, 55], [7, 55]], "fight_dmgs": [(6, 2), (5, 2)]}, None)
    chk("_apply_eff(fights)：按序逐对——第一对打死 55，第二对因 55 不在场不结算（7 不受伤）",
        55 not in s.units and s.units[11].dfn == 2 and s.units[7].dfn == 3,
        "11:%s 7:%s" % (s.units[11].dfn, s.units[7].dfn))

    # ============ B/C 路：新重放 kind 的三角验证 ============
    # MakeCardsFight（含退化的 a is b：第一击把它打离场后不再打已不在场的单位，死亡链只扇一遍）
    mk_f = lambda: mk([unit(11, ME, 3, 4), unit(12, OPP, 2, 3)])                # noqa: E731
    ok, why, ap = tri("MakeCardsFight", [P1, P2, 7, None], mk_f, ptr_ids={P1: 11, P2: 12})
    chk("三角验证 `MakeCardsFight` 直跑 ≡ 重放", ok, why)
    mk_self = lambda: mk([unit(55, OPP, 2, 1)])                                  # noqa: E731
    ok, why, ap = tri("MakeCardsFight", [P2, P2, 7, None], mk_self, ptr_ids={P2: 55})
    chk("三角验证 `MakeCardsFight` 对自己：直跑 ≡ 重放，且只死一次（不二次扇死亡链）",
        ok and sum(1 for c in ap if c[0] == "died") == 1, why)

    # ConvertCard 三种落点（在场/手牌/牌库）
    CV = {"ids": [11], "name": "NEW CARD", "atk": 7, "dfn": 8, "cost": 4, "opc": 2, "armor": 1, "typ": "infantry",
          "kw": ["guard", "smokescreen"], "instigator": 7, "skip_trigger": False}
    mk_b = lambda: mk([unit(11, ME, 3, 4, row="frontline")])                      # noqa: E731
    ok, why, ap = tri("ConvertCard", [[11], 7, "NEW CARD", 0, False], mk_b, payload={"convert": CV})
    chk("三角验证 `ConvertCard`（在场）直跑 ≡ 重放（新牌同侧同行、出厂数值、前线去 smokescreen）",
        ok and any(c[0] == "convert_board" for c in ap), why)

    def mk_h():
        s = mk()
        s.hand[21] = S.H(21, "OLD", 3, "infantry", 2, 2, (), {})
        return s
    cv_h = dict(CV, ids=[21])
    ok, why, ap = tri("ConvertCard", [[21], 7, "NEW CARD", 0, False], mk_h, payload={"convert": cv_h})
    chk("三角验证 `ConvertCard`（手牌）直跑 ≡ 重放", ok and any(c[0] == "convert_hand" for c in ap), why)

    def mk_d():
        s = mk()
        s.deck, s.deck_known = [31, 32, 33], True
        s.deck_cards = {}
        return s
    cv_d = dict(CV, ids=[32])
    ok, why, ap = tri("ConvertCard", [[32], 7, "NEW CARD", 0, False], mk_d, payload={"convert": cv_d})
    chk("三角验证 `ConvertCard`（牌库）直跑 ≡ 重放（同下标换牌）", ok and any(c[0] == "convert_deck" for c in ap), why)

    # TakeControlOfEnemyUnit（MINORITY RECRUITS）
    mk_s = lambda: mk([unit(12, OPP, 2, 3, row="back")])                          # noqa: E731
    ok, why, ap = tri("TakeControlOfEnemyUnit", [P2, 7, None], mk_s, ptr_ids={P2: 12})
    chk("三角验证 `TakeControlOfEnemyUnit` 直跑 ≡ 重放（steal）", ok and any(c[0] == "steal" for c in ap), why)

    # MoveCardToTopOfOwnersDeck 的场上单位分支（GROUNDED / HMS BELFAST）
    def mk_u():
        s = mk([unit(11, ME, 3, 4), unit(12, OPP, 2, 3)])
        s.deck, s.deck_known = [31, 32], True
        s.deck_cards = {}
        return s
    ok, why, ap = tri("MoveCardToTopOfOwnersDeck", [11, 7, 0, None], mk_u)
    chk("三角验证 `MoveCardToTopOfOwnersDeck`（我方单位）直跑 ≡ 重放：离场 + 牌库顶", ok and any(c[0] == "unit_to_deck" for c in ap), why)
    s = mk_u()
    h = SC.native_hooks(s, my_side=ME)
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [11, 7, 0, None], None)
    chk("我方单位回牌库顶：离场 + deck[0]==11", 11 not in s.units and s.deck[0] == 11, str(s.deck))
    s = mk_u()
    h = SC.native_hooks(s, my_side=ME)
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [12, 7, 0, None], None)
    chk("对方单位：只离场、我方牌库不变（敌方牌序读不到）", 12 not in s.units and s.deck == [31, 32])

    # SpawnCardInDeckBySide（FOG OF WAR）：随机位置/洗牌的结果记进 applied，重放照写
    class FakeRng:
        def __init__(self):
            self.n = 0

        def wrapper_int(self, lo, hi):
            self.n += 1
            return hi

        def shuffle(self, lst):
            return list(reversed(lst))
    mk_ds = lambda: (lambda s: (setattr(s, "deck", [31, 32, 33]), setattr(s, "deck_known", True),     # noqa: E731
                                setattr(s, "deck_cards", {}), s)[-1])(mk())
    ok, why, ap = tri("SpawnCardInDeckBySide", [1, "CARD_X", 7, 2, 0, False, False, True, False, False, None], mk_ds, rng=FakeRng())
    chk("三角验证 `SpawnCardInDeckBySide`（2 张 + 洗牌）直跑 ≡ 重放（deck_add 记下标、deck_order 记洗牌结果）",
        ok and [c[0] for c in ap].count("deck_add") == 2 and any(c[0] in ("deck_order", "deck_shuffle") for c in ap), why)
    ok, why, ap = tri("SpawnCardInDeckBySide", [1, "CARD_X", 7, 1, 0, False, False, False, False, True, None], mk_ds, rng=FakeRng())
    chk("三角验证 `SpawnCardInDeckBySide`（RandomWithoutShuffle=第 10 位实参）直跑 ≡ 重放——随机位置按记录的下标重放",
        ok, why)

    # SpawnCardOnBattlefield 出参 ⇒ GiveBlitz(spawnedCardID)（ENCIRCLEMENT；card_event_encirclement.cpp:Label_10）
    s = mk()
    s.front_owner = ME
    h = SC.native_hooks(s, my_side=ME, spawn_stat=lambda n: {"atk": 2, "dfn": 2, "cost": 1, "typ": "infantry", "kw": ()})
    fr, ee = out_frame(10, "spawnedCardID")
    h["SpawnCardOnBattlefield"](None, fr, None, [1, True, "card_unit_x", 7, None, True, -1, 0, False, False, None], ee)
    nid = fr.locals.get("spawnedCardID")
    h["GiveBlitz"](None, None, None, [nid, 7], None)
    chk("SpawnCardOnBattlefield 写出参 spawnedCardID；GiveBlitz(它) 给的是刚生成的那张（sick=False、blitz）",
        nid in s.units and "blitz" in s.units[nid].kw and s.units[nid].sick is False, "nid=%s units=%s" % (nid, list(s.units)))
    s = mk()
    h = SC.native_hooks(s, my_side=ME)                                     # 没面板 ⇒ 没生成 ⇒ 出参 0
    fr, ee = out_frame(10, "spawnedCardID")
    h["SpawnCardOnBattlefield"](None, fr, None, [1, True, "card_unit_x", 7, None, True, -1, 0, False, False, None], ee)
    h["GiveBlitz"](None, None, None, [fr.locals.get("spawnedCardID"), 7], None)
    gaps = h["__ctx__"].gaps
    chk("没生成 ⇒ 出参 0 ⇒ GiveBlitz(0) 是合法 no-op（不记『换不出场上的单位』假缺口）",
        fr.locals.get("spawnedCardID") == 0 and not any("GiveBlitz" in g for g in gaps), str(gaps))

    print("\n失败 %d 项" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
