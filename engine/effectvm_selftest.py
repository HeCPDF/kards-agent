#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.effectvm_selftest —— `engine.effectvm` 的离线自检（不碰游戏）：钩子记录/污染/摘要。

P6 自 `engine/effectvm.py` 原样拆出；`engine.effectvm.selftest()` 转调这里，`python -m engine.effectvm` 照旧。
"""
from __future__ import annotations

from kardsmem.gamemodel import ESide  # noqa: F401


def selftest() -> int:
    from engine.effectvm import (ALL_HOOKED, Recorder, _combos, enumerate_effects,   # noqa: PLC0415
                                 record_effects, to_effects)
    fails = 0

    def chk(name, ok, extra=""):
        nonlocal fails
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        fails += 0 if ok else 1

    def call(rec, verb, *args):
        return rec.hook(verb)(None, None, None, list(args), None)

    TARGET, SELF = 0x1000, 0x2000

    r = Recorder(SELF, TARGET)
    call(r, "DamageCard", TARGET, 2, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("伤害：数量取自已求值实参", e.get("damage") == 2, str(e))

    r = Recorder(SELF, TARGET)
    r.hq_enemy = frozenset([0x3000])
    call(r, "DamageCard", 0x3000, 3, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("DamageCard(敌方 HQ 卡) ⇒ damage_hq（不再算成打单位）",
        e.get("damage_hq") == 3 and "damage" not in e, str(e))

    r = Recorder(SELF, TARGET)
    r.hq_enemy = frozenset([0x3000])
    r.ptr_ids[0x3000] = 77
    call(r, "DamageMultipleCards", [77, 88], 2, SELF, None)
    e = to_effects(r, my_side=1)
    chk("DamageMultipleCards 打到敌方 HQ 的 card_id ⇒ damage_hq（不再留在 damage_aoe_ids）",
        e.get("damage_hq") == 2 and e.get("damage_aoe_ids") == [88] and e.get("damage_aoe") == 2, str(e))

    r = Recorder(SELF, TARGET)
    r.hq_own = frozenset([0x3100])
    r.ptr_ids[0x3100] = 79
    call(r, "DamageMultipleCards", [79], 1, SELF, None)
    chk("DamageMultipleCards 打到己方 HQ 的 card_id ⇒ damage_own_hq",
        to_effects(r, my_side=1).get("damage_own_hq") == 1)

    r = Recorder(SELF, TARGET)
    r.hq_enemy = frozenset([0x3000])
    call(r, "ChangeDefense", 0x3000, SELF, 2, 1, False, None)
    e = to_effects(r, my_side=1)
    chk("ChangeDefense(敌方 HQ 卡, +2, PermBuff) ⇒ heal_opp_hq=2（原版对总部无特判；以前静默丢）",
        e.get("heal_opp_hq") == 2 and "heal_hq" not in e, str(e))

    r = Recorder(SELF, TARGET)
    call(r, "GainKreditSlot", SELF, 1, None)                 # 给 side=1（我方）
    call(r, "GainKreditSlot", SELF, 2, None)                 # 给 side=2（对方）
    e = to_effects(r, my_side=1)
    chk("GainKreditSlot 给对方的那一份 ⇒ opp_slot（不再并进我方 slot）",
        e.get("slot") == 1 and e.get("opp_slot") == 1, str(e))

    r = Recorder(SELF, TARGET)
    call(r, "SetCardsSeenByCipher", 3, SELF, 1, None)
    chk("情报触发源：SetCardsSeenByCipher(3) ⇒ intel_seen=3",
        to_effects(r, my_side=1).get("intel_seen") == 3)

    r = Recorder(SELF, TARGET)
    call(r, "GiveKreditsBySide", 1, 1, SELF, None)
    call(r, "GiveKreditsBySide", 2, 1, SELF, None)
    e = to_effects(r, my_side=1)
    chk("生产：给己方 +1，给对方的另记", e.get("kredit") == 1 and e.get("opp_kredit") == 1, str(e))

    r = Recorder(SELF, TARGET)
    call(r, "DrawCardsFromDeckBySide", SELF, 1, 1, True, False, None, 0.0)
    chk("计划：抽 1", to_effects(r, 1).get("draw") == 1)

    r = Recorder(SELF, TARGET)
    call(r, "LoseKreditSlot", 1)
    chk("丢槽：我方 -1 槽", to_effects(r, 1).get("slot") == -1)

    r = Recorder(SELF, TARGET)
    call(r, "ForceEndTurn")
    chk("强制结束回合（ForceEndTurn，久留米联队那类）⇒ playing_side=对方座位",
        to_effects(r, 1).get("playing_side") == ESide.right)

    r = Recorder(SELF, TARGET)
    call(r, "ShuffleDeckBySide", 1, False, SELF, None)
    chk("洗牌：side=1（我）⇒ deck_shuffle=我方座位",
        to_effects(r, 1).get("deck_shuffle") == ESide.left)
    r2 = Recorder(SELF, TARGET)
    call(r2, "ShuffleDeckBySide", 2, False, SELF, None)
    chk("洗牌：side=2（对方）⇒ deck_shuffle=对方座位",
        to_effects(r2, 1).get("deck_shuffle") == ESide.right)

    r = Recorder(SELF, TARGET)
    call(r, "SalvageMultipleUnits", [5, 6], SELF, False)
    chk("收缴：SalvageMultipleUnits([5,6]) ⇒ salvage_ids=[5,6]",
        to_effects(r, 1).get("salvage_ids") == [5, 6])

    r = Recorder(SELF, TARGET)
    call(r, "GiveMobilize", TARGET, SELF, None)
    chk("动员：GiveMobilize ⇒ give 含 mobilize",
        "mobilize" in (to_effects(r, 1).get("give") or []))

    r = Recorder(SELF, TARGET)
    call(r, "ChangedPinnedTurns", TARGET, SELF, 2, None)
    chk("压制回合数：ChangedPinnedTurns(+2) ⇒ pin_turns=2",
        to_effects(r, 1).get("pin_turns") == 2)

    r = Recorder(SELF, TARGET)
    call(r, "StealCardFromBoardToDeck", TARGET, SELF, 1, None)
    chk("洗入牌库：StealCardFromBoardToDeck(deckSide=1) ⇒ steal_to_deck=local + deck_shuffle=我方座位",
        to_effects(r, 1).get("steal_to_deck") == ESide.left
        and to_effects(r, 1).get("deck_shuffle") == ESide.left)

    r = Recorder(SELF, TARGET)
    call(r, "SpawnCardInDeckBySide", 1, "NEWCARD", SELF, 2, 0, False, False, True, False, False, None)
    e = to_effects(r, 1)
    chk("塞牌：SpawnCardInDeckBySide(2 张, shuffle) ⇒ deck_add=2 + local + shuffle",
        e.get("deck_add") == 2 and e.get("deck_add_side") == ESide.left
        and e.get("deck_add_shuffle") is True and e.get("deck_add_name") == "NEWCARD",
        str({k: v for k, v in e.items() if k.startswith("deck_add")}))
    # ★ 2026-10-02：`shuffle=true` 等同于一次 `ShuffleDeckBySide(skipSubAction=true)`
    #   ⇒ 0x16 触发族（`+shuffled`）的闸门必须能看见（以前只记 deck_add_shuffle）。
    chk("塞牌并洗(shuffle=true) ⇒ 同时给 deck_shuffle + deck_shuffle_skip",
        e.get("deck_shuffle") == ESide.left and e.get("deck_shuffle_skip") is True,
        "shuffle=%r skip=%r" % (e.get("deck_shuffle"), e.get("deck_shuffle_skip")))

    r = Recorder(SELF, TARGET)
    call(r, "SpawnCardInDeckBySide", 1, "NEWCARD", SELF, 2, 0, False, False, False, False, False,
         None)
    e2 = to_effects(r, 1)
    chk("塞牌不洗(shuffle=false) ⇒ 不给 deck_shuffle_skip",
        e2.get("deck_shuffle_skip") is not True,
        "skip=%r" % (e2.get("deck_shuffle_skip"),))

    r = Recorder(SELF, TARGET)
    call(r, "MoveUnitFromSupportToFrontLine", TARGET, SELF, None)
    chk("推上前线：MoveUnitFromSupportToFrontLine ⇒ move_front",
        to_effects(r, 1).get("move_front") is True)

    r = Recorder(SELF, TARGET)
    call(r, "RevealCard", TARGET, SELF, None)
    chk("揭示：RevealCard ⇒ reveal=True", to_effects(r, 1).get("reveal") is True)

    r = Recorder(SELF, TARGET)
    call(r, "AddDefenseToMultipleCards", [5, 6], 2, SELF, None)
    e = to_effects(r, 1)
    chk("群体加防：AddDefenseToMultipleCards([5,6], +2) ⇒ defense_aoe=2 + ids",
        e.get("defense_aoe") == 2 and e.get("defense_aoe_ids") == [5, 6], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "AddKreditsTax", TARGET, 3, SELF, None)
    chk("指向税：AddKreditsTax(+3) ⇒ kredits_tax=3", to_effects(r, 1).get("kredits_tax") == 3)

    r = Recorder(SELF, TARGET)
    call(r, "AddGameplayRestriction", 1, 2, 0, 3)
    e = to_effects(r, 1)
    chk("对局限制：AddGameplayRestriction(side=1, type=2=不能打指令, 3 回合) ⇒ restriction_add",
        e.get("restriction_add") == [{"side": ESide.left, "type": 2, "turns": 3}], str(e))
    r = Recorder(SELF, TARGET)
    call(r, "RemoveGameplayRestriction", 2, 3, 0, False, None)
    e = to_effects(r, 1)
    chk("对局限制：RemoveGameplayRestriction(side=2, type=3) ⇒ restriction_remove（enemy）",
        e.get("restriction_remove") == [{"side": ESide.right, "type": 3, "turns": 0}], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "GiveBlitz", TARGET, SELF)
    call(r, "GiveFury", TARGET, SELF)
    chk("贴关键词：记录给了哪些", to_effects(r, 1).get("give") == ["blitz", "fury"])

    r = Recorder(SELF, TARGET)
    call(r, "PinUnit", TARGET, SELF)
    call(r, "RemovePin", 0x3000, None)
    e = to_effects(r, 1)
    chk("压制/解除压制", e.get("pin") is True and e.get("unpin") is True)

    # ---- 随机：枚举而不是预测 ----
    class _E:                                    # 假的调用点表达式：kids[i].args["prop"] = 出参变量名
        def __init__(self, props):
            self.kids = [type("K", (), {"args": {"prop": p}})() for p in props]

    class _F:
        locals: dict = {}

    r = Recorder(SELF, TARGET)
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    chk("随机整数：探查模式选 0 号，记域大小 3", v == 0 and r.nodes == [{"verb": "RandomIntFromRangeWithStream", "size": 3}]
        and fr.locals.get("out") == 0)
    r = Recorder(SELF, TARGET, forced=[2])
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    chk("随机整数：按指定结果返回并写出参", v == 2 and fr.locals.get("out") == 2)

    # ---- 确定随机（给了活种子）----
    from kardsmem.rng import Stream as _S
    r = Recorder(SELF, TARGET)
    r.stream = _S(4242)
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    ref = _S(4242).wrapper_int(0, 2)
    chk("确定随机：包装原语按种子给出确定结果、不记枚举点", v == ref and r.nodes == [] and r.exact
        and fr.locals.get("out") == ref and r.stream.draws >= 3)
    r = Recorder(SELF, TARGET)
    r.stream = _S(77)
    fr = _F(); fr.locals = {}
    r.hook("GetRandomCard")(None, fr, None, [[11, 22, 33, 44], False, None], _E(["c", "s", "out"]))
    chk("确定随机：GetRandomCard 取 cards[wrapper(0,n-1)]",
        fr.locals.get("out") == [11, 22, 33, 44][_S(77).wrapper_int(0, 3)] and r.nodes == [])

    r = Recorder(SELF, TARGET, forced=[3])
    fr = _F(); fr.locals = {}
    r.hook("GetRandomCard")(None, fr, None, [[11, 22, 33, 44], False, None], _E(["c", "s", "out"]))
    chk("随机选牌：从数组里按指定下标取", fr.locals.get("out") == 44)

    r = Recorder(SELF, TARGET, forced=[1])
    r.hook("GiveRandomCombatKeyword")(None, None, None, [TARGET, SELF, None, None], _E([]))
    e = to_effects(r, 1)
    chk("随机关键词：落成指定的关键词", e.get("give") == ["blitz"], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "DamageCard", TARGET, 1, SELF)
    call(r, "DiscardRandomCardFromHand", 1, SELF, None)          # 域未知的随机
    call(r, "DrawCardsFromDeckBySide", SELF, 1, 1, True, False, None, 0.0)
    e = to_effects(r, 1)
    chk("域未知的随机：记机会节点，其后的效果归入 uncertain",
        e.get("chance") == ["DiscardRandomCardFromHand"] and "draw" not in e
        and e.get("uncertain", {}).get("draw") == 1 and e.get("damage") == 1, str(e))

    # 枚举：CONVOY ATTACK 型「对目标造成 0-2 点」→ 3 个分支，期望伤害 1
    def fake_run(km, card, tgt, has_t, hook, side, forced, read_hooks=None, slots=None):
        rr = Recorder(card, tgt, forced)
        fr2 = _F(); fr2.locals = {}
        v2 = rr.hook("RandomIntFromRangeWithStream")(None, fr2, None, [0, 2, None], _E(["a", "b", "out"]))
        rr.hook("DamageCard")(None, fr2, None, [tgt, v2, card], _E([]))
        return {"eff": to_effects(rr, side), "nodes": list(rr.nodes), "complete": True, "stopped": None}
    en = enumerate_effects(None, SELF, TARGET, True, my_side=1, runner=fake_run)
    dmg = [e2.get("damage", 0) for _, e2 in en["outcomes"]]
    chk("枚举：0-2 点伤害 ⇒ 3 个等概率分支", sorted(dmg) == [0, 1, 2] and abs(sum(w for w, _ in en["outcomes"]) - 1) < 1e-9,
        str(en))
    chk("枚举：期望伤害 = 1", abs(sum(w * e2.get("damage", 0) for w, e2 in en["outcomes"]) - 1.0) < 1e-9)

    def fake_run_none(km, card, tgt, has_t, hook, side, forced, read_hooks=None, slots=None):
        rr = Recorder(card, tgt, forced)
        rr.hook("GiveKreditsBySide")(None, None, None, [1, 1, card, None], _E([]))
        return {"eff": to_effects(rr, side), "nodes": [], "complete": True, "stopped": None}
    en0 = enumerate_effects(None, SELF, 0, False, my_side=1, runner=fake_run_none)
    chk("枚举：没有随机点 ⇒ 单分支权重 1", len(en0["outcomes"]) == 1 and en0["outcomes"][0][0] == 1.0
        and en0["outcomes"][0][1].get("kredit") == 1)
    chk("组合展开：两个随机点 3×2 = 6 个分支", len(_combos([3, 2], 8)) == 6 and len(_combos([12, 12], 8)) == 8)

    r = Recorder(SELF, TARGET)
    call(r, "ShowNotification", "x")
    call(r, "AddToBattleLog", "y")
    call(r, "AddIntelToCard", 5, SELF, 2, None)     # 情报：空实现（只影响看敌方手牌的 UI）
    chk("表现/日志类不记录", not r.records and not r.tainted)
    chk("钩子表覆盖随机原语", "RandomIntFromRangeWithStream" in ALL_HOOKED
        and "GiveRandomCombatKeyword" in ALL_HOOKED)

    r = record_effects(None, 0)                                   # 没有进程 ⇒ 说清楚，不崩
    chk("VM 不可用时如实报 stopped", r["complete"] is False and r["stopped"])
    return fails
