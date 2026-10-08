#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.scripts_sinks_more —— 直跑 sink 表（后半：控制权 / 牌库 / 回合 / 杂项 / 老兵 / 生成 / 转换 / 限制 / 未建模 / 待决 / 终批 / 协力）。

P6 自 `engine/scripts.py` 原样拆出（纯搬移）；`engine.scripts` 汇总各表成 `DEFAULT_SINKS`。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from engine.scripts_ctx import DirectCtx, write_out


# ---------------------------------------------------------------------------
# 控制权 / 弃牌（第十七刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_take_control(ctx: DirectCtx, vm, frame, obj, args, e):
    """`TakeControlOfEnemyUnit(card, instigatorID, …)`（录制键 `steal`）。

    规则本体在 `engine.natives.board.apply_take_control`（第十七刀**从 `sim` 下沉**、实现一字不差 ✓）
    ⇒ sink 只解析目标 + 扇 `("steal", uid)` 事件（离场/入场钩子 0x2E/0x8/… 交调用方）+ 如实记缺口。
    """
    from engine.natives.board import apply_take_control                   # noqa: PLC0415
    u = ctx.unit_of(args[0] if args else None)
    if u is None:
        ctx.gaps.append("TakeControlOfEnemyUnit：目标指针换不出场上的单位（未结算）")
        return None
    apply_take_control(ctx.state, u, me=ctx.my_side)
    ctx.applied.append(("steal", u.id))
    ctx.fire("steal", u.id)
    ctx.gaps.append("steal：我方后排满的分支 / hasActivePincerEffect / CardLocationMoved 未建模")
    return None


def _sink_discard_from_hand(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DiscardCardFromHand(card, instigatorID, …)`（录制键 `discard_ids`）。

    与 `sim:743-744` 同口径：**己方手牌里有这张就移走**（敌方手牌不建模 ⇒ 记缺口）。
    ★ 实参是 **card ID**（原版 `DiscardCardFromHand(int cardID, int discarderID, …)`，`BP_CardFunctions.cpp:3271`；
      调用点传的都是 `x->cardID`，如 `card_event_iron_victory.cpp`）——**不是指针**：以前这里经 `ptr_ids` 换 id，
      真实 id 恒换不出 ⇒ 弃牌从来没结算（对账 IRON VICTORY 340 条缺口；与字典路 `discard_ids` 原样用 id 一致）。
    """
    cid = args[0] if args else None
    if not isinstance(cid, int) or isinstance(cid, bool):
        ctx.gaps.append("DiscardCardFromHand：card_id 读不出（%r，未结算）" % (cid,))
        return None
    if cid in (getattr(ctx.state, "hand", {}) or {}):
        (ctx.state.hand or {}).pop(cid, None)
        ctx.applied.append(("discard", cid))
        write_out(frame, e, 4, True)                         # 出参 `success=true`（`:3394`）：脚本靠它计数（IRON VICTORY）
        return None
    ctx.gaps.append("DiscardCardFromHand：%s 不在我方手牌（敌方手牌不建模）⇒ 未结算" % cid)
    if cid > 0:
        write_out(frame, e, 4, True)                         # 与录制路同口径：正 id 记成功（敌方手牌我们看不到）
    return None


#: 控制权 / 弃牌族已迁动词（第十七刀）。
CONTROL_SINKS: Dict[str, Callable[..., Any]] = {
    "TakeControlOfEnemyUnit": _sink_take_control,
    "DiscardCardFromHand": _sink_discard_from_hand,
}


# ---------------------------------------------------------------------------
# 牌库顶 / 抽牌 / 群体防御（第十九刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_defense_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`AddDefenseToMultipleCards(const TArray<int>*& receiverIDs, int amount, int giverCardID, int& qqq)`
    （`:6065`）。

    ★ **数组里是 card ID**（签名逐字；**不是**指针 —— 第十八刀撤回了那个错判）⇒ 走 `ctx.unit_arg` 解析。
    效果照 `sim:756-763` 同口径：`dfn += amount`；`amount < 0` 且降到位（≤0）⇒ **摧毁**。
    """
    arr = args[0] if args else None
    amt = args[1] if len(args) > 1 else 0
    for cid in (arr if isinstance(arr, (list, tuple)) else []):
        u = ctx.unit_arg(cid)
        if u is None:
            ctx.gaps.append("AddDefenseToMultipleCards：%r 换不出场上的单位（未结算）" % (cid,))
            continue
        u.dfn = float(u.dfn) + float(amt or 0)
        ctx.applied.append(("defense_aoe", u.id, amt))
        if float(amt or 0) < 0 and float(u.dfn) <= 0:
            ctx.destroy(u.id, why="AddDefenseToMultipleCards 降到位")
    return None


def _deck_top_one(ctx: DirectCtx, cid, pos) -> None:
    """把一张**手牌**放回己方牌库的 `pos`（`MoveCardToTopOfDeck`，`:17880` → `AddCardToDeckBySide(side, id, true, positionFromTop)`；
    0 = 牌顶）。实现在 `engine.natives.deck.move_hand_to_deck`（字典路/重放共用）。场上单位（GROUNDED / HMS BELFAST）走
    `engine.natives.board.unit_to_deck_top`（离场 + 回牌库）。"""
    hand = getattr(ctx.state, "hand", {}) or {}
    if cid not in hand:
        u = ctx.unit_by_id(cid)
        if u is not None:
            from engine.natives.board import unit_to_deck_top           # noqa: PLC0415
            ctx.gaps += unit_to_deck_top(ctx.state, cid, pos) or []
            ctx.applied.append(("unit_to_deck", cid, pos))
            return
    from engine.natives.deck import move_hand_to_deck                  # noqa: PLC0415
    idx = move_hand_to_deck(ctx.state, cid, pos, gaps=ctx.gaps)
    if idx is not None:
        ctx.applied.append(("to_deck", cid, idx))


def _sink_move_to_deck_top(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MoveCardToTopOfOwnersDeck(int cardID, int instigatorID, int positionFromTop, int& qqq)`（`:3415`）。

    ★ 首参是 **card ID**（签名逐字），不是指针。
    """
    cid = args[0] if args else None
    pos = args[2] if len(args) > 2 else None
    _deck_top_one(ctx, cid, pos)
    return None


def _sink_move_multi_to_deck_top(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MoveMultipleCardsToTopOfOwnersDeck(const TArray<int>*& cardIDs, …)`（`:14112`）★ 数组是 **ID**。"""
    arr = args[0] if args else None
    pos = args[2] if len(args) > 2 else None
    for cid in (arr if isinstance(arr, (list, tuple)) else []):
        _deck_top_one(ctx, cid, pos)
    return None


def _sink_spawn_in_hand(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SpawnCardinHandbySide(ESideEnum side, FName card_name, int spawnerID, bool CardSeen, bool SkipDrawAnimation,
    bool fromOppositeSide, FString newCardText, FText*& campaignName, EFactionEnum salvageFaction, int& spawnedCardID)`
    （`:6371`；出参 `spawnedCardID` 在下标 **9**）。

    原版：`spawnerID > 0` 才干活；`CreateCard(位置=手牌)` ⇒ 我方手里**多一张新牌**，**不碰牌库、不是抽牌**
    （以前这里调注入的抽牌链 ⇒ 把牌库顶抽进手牌，牌库少一张 ✗）。新牌按名字造（`engine.natives.deck.spawn_in_hand`），
    新牌 id 写进出参（IRON VICTORY 之后 `GetCardFromID(spawnedCardID)` → `ChangeOperationCost`）；对方 ⇒ 旧路不记 ⇒ **记缺口**（不猜）。
    """
    from engine.natives.deck import spawn_in_hand                      # noqa: PLC0415
    side = ctx.seat_of(args[0] if args else None)
    name = args[1] if len(args) > 1 else None
    spawner = args[2] if len(args) > 2 else 0
    if side is not None and side != ctx.my_side:
        # 对方手牌：模拟里只有张数（`opp_cards`，同字典路 `opp_gain_cards`）。新牌发一个只用来认句柄的 id（`engine/spawned.py`），
        # 之后脚本对它的 `ChangeKreditCost` / `JSON_SetInt` 没有可写的状态 ⇒ 由对应 sink 如实记缺口。
        if not isinstance(name, str) or not name or name == "None":
            ctx.gaps.append("SpawnCardinHandbySide：卡名读不出（未生成）")
            return None
        if not isinstance(spawner, int) or spawner <= 0:
            return None
        ctx.state.opp_cards = int(getattr(ctx.state, "opp_cards", 0) or 0) + 1
        ocid = ctx.spawned.next_opp_id()
        ctx.spawned.register(ocid, name, int(side), "opp_hand")
        write_out(frame, e, 9, ocid)
        ctx.applied.append(("opp_gain_hand", name))
        return None
    if side != ctx.my_side:
        ctx.gaps.append("SpawnCardinHandbySide：side 读不出（未生成）")
        return None
    if not isinstance(name, str) or not name or name == "None":
        ctx.gaps.append("SpawnCardinHandbySide：卡名读不出（未生成）")
        return None
    if not isinstance(spawner, int) or spawner <= 0:
        return None                                                      # 原版 `spawnerID > 0` 才干活（`:6384`）
    cid = spawn_in_hand(ctx.state, name, gaps=ctx.gaps)
    if cid is not None:
        ctx.spawned.register(cid, name, int(side), "hand")
    write_out(frame, e, 9, cid if cid is not None else 0)             # 没造出来（手牌满）⇒ 出参 0（原版默认值），别留 None 让后面的 `GetCardFromID` 崩
    if cid is None:
        return None
    ctx.applied.append(("gain_hand", cid, name))
    ctx.gaps.append("SpawnCardinHandbySide：新牌的效果/ExecuteOnSpawnedInHandEvents 未建模（面板读得到才用，效果留空）")
    return None


def _sink_draw_cards_by_side(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DrawCardsFromDeckBySide(int instigatorID, ESideEnum side, int numCards, bool cardSeen,
    bool OpponentDraw, TArray<int>& ids, float delay)`（位次：side 在 **1**、张数在 **2**）。

    我方 ⇒ 抽 `numCards`（注入链 ✓）；对方 ⇒ `state.opp_cards += numCards`（与旧路 `opp_draw` 同口径 ✓）。
    """
    n = int(args[2] or 0) if len(args) > 2 else 0
    side = ctx.seat_of(args[1] if len(args) > 1 else None)
    if side == ctx.my_side:
        if ctx.on_draw is None:
            ctx.gaps.append("DrawCardsFromDeckBySide：调用方没注入抽牌链（`on_draw`）—— 不猜、不结算")
            return None
        before = set((getattr(ctx.state, "hand", {}) or {}))
        ctx.on_draw(ctx.state, n)
        ctx.applied.append(("draw", n))
        # 出参 `cardsIDs`（下标 5）：本次**抽到手里的牌 id**（按抽牌顺序）。原版 `cardSeen=false` 才把 `drawnCards` 写进出参
        # （`BP_CardFunctions.cpp:6499-6545`，`Label_365`/`Label_539`），`cardSeen=true` 走 `NotifyCardsSeen` 且出参留空。IJN AKAGI：`affectedCards = cardsIDs`
        # 之后对每张 `ChangeKreditCost(-2)`；不写出参 ⇒ 数组恒空 ⇒ "费用 -2" 整段不执行（旧缺陷）。
        # 抽到的是匿名牌（牌库不可知，负 id）时 `cardsIDs[0] > 0` 不成立 ⇒ 脚本也会跳过——与原版"真 id 才 > 0"一致的近似。
        if not (len(args) > 3 and args[3]):
            write_out(frame, e, 5, [k for k in (getattr(ctx.state, "hand", {}) or {}) if k not in before])
        else:
            write_out(frame, e, 5, [])
    else:
        ctx.state.opp_cards = int(getattr(ctx.state, "opp_cards", 0) or 0) + n
        ctx.applied.append(("opp_draw", n))
    return None


def _sink_draw_specific(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DrawSpecificCardFromDeckBySide(int instigatorID, int cardID, ESideEnum side, bool cardSeen)`
    （位次：cardID 在 1、side 在 **2**）—— 抽**指定的那一张**（`BP_CardFunctions.cpp:6308`：牌库里有 ⇒ 挪到牌顶再
    `DrawTopCardFromDeck`；没有 ⇒ 什么都不做）。

    以前这里无视 cardID、直接抽牌库顶（DEFEND THE NATION"抽牌库里第一张非法国牌"抽成了第一张）✗ ⇒ 现在先 `pull_specific_to_top`。
    对方 ⇒ `opp_cards += 1`（旧口径，对方牌库不建模）。"""
    from engine.natives.deck import pull_specific_to_top               # noqa: PLC0415
    side = ctx.seat_of(args[2] if len(args) > 2 else None)
    cid = args[1] if len(args) > 1 else None
    if side == ctx.my_side:
        if ctx.on_draw is None:
            ctx.gaps.append("DrawSpecificCardFromDeckBySide：调用方没注入抽牌链（`on_draw`）—— 不猜、不结算")
            return None
        got = pull_specific_to_top(ctx.state, cid, gaps=ctx.gaps)
        if got is False:
            return None                                                  # 不在牌库 ⇒ 原版 `return`，不抽
        ctx.on_draw(ctx.state, 1)
        ctx.applied.append(("draw_specific", cid) if got else ("draw", 1))
    else:
        ctx.state.opp_cards = int(getattr(ctx.state, "opp_cards", 0) or 0) + 1
        ctx.applied.append(("opp_draw", 1))
    return None


#: 牌库顶 / 抽牌 / 群体防御已迁动词（第十九刀）。
DECK_SINKS: Dict[str, Callable[..., Any]] = {
    "AddDefenseToMultipleCards": _sink_defense_multiple,
    "MoveCardToTopOfOwnersDeck": _sink_move_to_deck_top,
    "MoveMultipleCardsToTopOfOwnersDeck": _sink_move_multi_to_deck_top,
    "SpawnCardInHandBySide": _sink_spawn_in_hand,
    "DrawCardsFromDeckBySide": _sink_draw_cards_by_side,
    "DrawSpecificCardFromDeckBySide": _sink_draw_specific,
}



# ---------------------------------------------------------------------------
# 行动权 / 本回合加攻 / 收缴（第二十刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_add_attack_until_eot(ctx: DirectCtx, vm, frame, obj, args, e):
    """`AddAttackUntilEndOfTurn(class UBaseCardObject* card, int instigatorID, int attackToAdd)`
    —— **首参是指针**（签名逐字）⇒ 走 `unit_arg`（两向解析 ✓）。

    与 `sim:683-685` 同口径：总量 `atk = clamp_stat(atk + n)`，**另记** `atk_turn += n`
    （回合结束会消失的那部分，见 `unit_value` 的簿记）。
    """
    u = ctx.unit_arg(args[0] if args else None)
    n = int(args[2] or 0) if len(args) > 2 else 0
    if u is None:
        ctx.gaps.append("AddAttackUntilEndOfTurn：目标指针/ID 换不出场上的单位（未结算）")
        return None
    from engine.natives.stats import clamp_stat                             # noqa: PLC0415
    u.atk = clamp_stat(int(getattr(u, "atk", 0) or 0) + n)
    u.atk_turn = int(getattr(u, "atk_turn", 0) or 0) + n
    ctx.applied.append(("attack_turn", u.id, n))
    return None


def _sink_force_end_turn(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ForceEndTurn()`（`BP_CardFunctions::ForceEndTurn`，无参）—— 行动权交给**对方**
    （录制侧 `END_TURN_VERBS` 的语义 ✓，`effectvm:150-156`）。"""
    other = getattr(ctx.state, "opp", None) if ctx.my_side == getattr(ctx.state, "me", None) \
        else getattr(ctx.state, "me", None)
    ctx.state.playing_side = other
    ctx.applied.append(("force_end_turn", other))
    return None


def _side_sink_sd(ctx: DirectCtx, args):
    """`SetPlayingSide(side)` / `SetActiveSide(side)` / `SwitchPlayingSide(side)`：`side` 在 **0**。"""
    sd = args[0] if args else None
    seat = ctx.seat_of(sd)
    if seat is None:
        ctx.gaps.append("行动权：side=%r 读不出（未结算）" % (sd,))
        return None
    ctx.state.playing_side = seat
    ctx.applied.append(("playing_side", seat))
    return None


def _sink_salvage_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SalvageMultipleUnits(const TArray<int>*& cardsToSalvage, int instigatorID, bool& qqq)`
    （`:163-168` 的注释 + 规则本体）★ **数组里是 card ID**（签名逐字）。

    规则走已端口的 `engine.natives.cards.apply_salvage`（`sim.effects` 只是转发 ✓）——
    手牌上限用 `engine.natives.board.HAND_CAP`（游戏自己的常量 ✓，不需要调用方再喂）。
    """
    from engine.natives.board import HAND_CAP                               # noqa: PLC0415
    from engine.natives.cards import apply_salvage                          # noqa: PLC0415
    arr = args[0] if args else None
    for cid in (arr if isinstance(arr, (list, tuple)) else []):
        run = apply_salvage(ctx.state, cid, hand_cap=HAND_CAP)
        ctx.applied.append(("salvage", cid))
        for g in (getattr(run, "gaps", None) or ()):
            ctx.gaps.append("salvage：" + str(g))
    return None


#: 行动权 / 本回合加攻 / 收缴已迁动词（第二十刀）。
TURN_SINKS: Dict[str, Callable[..., Any]] = {
    "AddAttackUntilEndOfTurn": _sink_add_attack_until_eot,
    "ForceEndTurn": _sink_force_end_turn,
    "SetPlayingSide": lambda ctx, vm, frame, obj, args, e: _side_sink_sd(ctx, args),
    "SetActiveSide": lambda ctx, vm, frame, obj, args, e: _side_sink_sd(ctx, args),
    "SwitchPlayingSide": lambda ctx, vm, frame, obj, args, e: _side_sink_sd(ctx, args),
    "SalvageMultipleUnits": _sink_salvage_multiple,
}



# ---------------------------------------------------------------------------
# 偷进牌库 / 分出胜负 / 回合倒数 / 随机弃牌（第二十一刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_steal_to_deck(ctx: DirectCtx, vm, frame, obj, args, e):
    """`StealCardFromBoardToDeck(int cardID, int instigatorID, ESideEnum deckSide, int& qqq)`
    ★ **cardID 是 ID**（签名逐字）、`deckSide` 在 **2**。

    与 `sim:846-853` 同口径：`deckSide == 我方 ∧ 牌库已知` ⇒ 同名复制进**己方牌库**（带模板）；
    **无论** deckSide 是哪边，目标都**离场** ✓（原版是"偷走"）。敌方牌序我们读不到 ⇒ 记缺口。
    """
    cid = args[0] if args else None
    deck_side = ctx.seat_of(args[2]) if len(args) > 2 else None
    u = ctx.unit_arg(cid)
    if u is None:
        ctx.gaps.append("StealCardFromBoardToDeck：%r 换不出场上的单位（未结算）" % (cid,))
        return None
    if deck_side == ctx.my_side and getattr(ctx.state, "deck_known", False):
        tpl = (getattr(ctx.state, "card_templates", {}) or {}).get(u.id)
        # ★ 同 `restrictions` 那个坑：别写 `(getattr(...) or []).append(...)` —— 空列表 falsy ⇒ append 丢 ✗
        if not isinstance(getattr(ctx.state, "deck", None), list):
            ctx.state.deck = []
        ctx.state.deck.append(u.id)
        if tpl is not None:
            ctx.dict_of("deck_cards").setdefault(u.id, tpl)
        ctx.applied.append(("steal_to_deck", u.id))
    elif deck_side != ctx.my_side:
        ctx.gaps.append("StealCardFromBoardToDeck：进**对方**牌库未建模（牌序读不到）∈ 只做离场")
    else:
        ctx.gaps.append("StealCardFromBoardToDeck：牌库顺序未知（`deck_known=False`）⇒ 只做离场")
    ctx.leave_board(u.id, why="StealCardFromBoardToDeck")
    return None


def _sink_end_match(ctx: DirectCtx, vm, frame, obj, args, e):
    """`EndMatch(ESideEnum winnerSide, float delay)`（`effectvm:809` 的 `end_match`）

    与 `sim:733-734` 同口径：赢家的对面总部**清零**（直接分出胜负）；`delay` 是表现层，不建模。
    """
    seat = ctx.seat_of(args[0] if args else None)
    if seat not in (getattr(ctx.state, "me", None), getattr(ctx.state, "opp", None)):
        ctx.gaps.append("EndMatch：winnerSide=%r 读不出（未结算）" % (args[0] if args else None,))
        return None
    loser = ctx.state.opp if seat == ctx.state.me else ctx.state.me
    ctx.state.hq[loser] = 0
    ctx.applied.append(("end_match", seat))
    ctx.fire("end_match", loser)
    return None


def _sink_set_countdown(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SetCountdown(int cardID, int value, bool& qqq)` ★ **cardID 是 ID**（签名逐字）。

    ★ **回合倒数机制未建模**（`U` 里没有该字段、`sim`/`policy` 也没有消费者 —— 第十六刀实测过）
    ⇒ 这里**只记缺口**，不假装写了一个没人读的字段（也不编一个字段出来）。
    """
    cid = args[0] if args else None
    u = ctx.unit_arg(cid)
    if u is None:
        ctx.gaps.append("SetCountdown：%r 换不出场上的单位（未结算）" % (cid,))
        return None
    ctx.gaps.append("SetCountdown：回合倒数机制未建模（card=%r value=%r）—— 只记缺口"
                    % (cid, args[1] if len(args) > 1 else None))
    return None


def _sink_discard_random(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DiscardRandomCardFromHand(ESideEnum side, int discarderID, int& discardedCardID)`。

    ★ 它是**随机点**（录制侧 `RANDOM_VERBS`：只记机会节点并污染其后记录）⇒ 直跑不能凭空选一张
    （那就是编）⇒ 记缺口，把"随机"如实交回调用方/录制侧。
    """
    ctx.gaps.append("DiscardRandomCardFromHand：随机弃牌未建模（随机点；不能替游戏选一张）")
    return None


def _sink_give_random_kw(ctx: DirectCtx, vm, frame, obj, args, e):
    """`GiveRandomCombatKeyword(int cardID, int instigatorID, ECombatKeyword& keywordGiven, bool& success)`
    （`BP_CardFunctions.cpp:22683`；候选集合/选取规则逐行说明见 `engine.natives.combat_kw`）。

    首参是 **card ID**。候选 = 位置/兵种过滤后的 7 个关键词剔除目标现有的；用**牌局随机流**（`ctx.rng`，同 `RandomIntFromRangeWithStream`）
    取 `[0, n-1]`；没有活流（`ctx.rng` 为 None）⇒ 走调用方注入的 `ctx.pick`（录制器的枚举节点，影子对账逐分支比较用），两者都没有 ⇒ 记缺口、不猜。
    动作 = 对应的 `Give<关键词>` sink / `ChangeHeavyArmor(+1, permBuff)`（记 `give_kw` / `change_heavy`，重放表本来就有）。
    出参 `success`（下标 3）；`keywordGiven`（下标 2）枚举值 1..7（失败 0）。"""
    from engine.natives.combat_kw import COMBAT_KEYWORD_ORDER, existing_combat_keywords, valid_combat_keywords   # noqa: PLC0415
    from engine.scripts_sinks_core import GIVE_SINKS                                                           # noqa: PLC0415
    from engine.natives.stats import change_heavy_armor                                                          # noqa: PLC0415
    cid = args[0] if args else None
    if not (isinstance(cid, int) and cid > 0) and not (isinstance(cid, int) and ctx.unit_by_id(cid) is not None):
        write_out(frame, e, 2, 0)
        write_out(frame, e, 3, False)
        return None                                                      # `GetCardFromID` 无效 ⇒ `success=false`（`:22688`）
    u = ctx.unit_by_id(cid)
    if u is None:
        ctx.gaps.append("GiveRandomCombatKeyword：目标 id %r 不是场上单位（手牌/牌库上的随机关键词未建模）⇒ 未结算" % (cid,))
        return None
    ptr = ctx.ptr_of_id(cid)
    g = ctx.gate_of(ptr) if ptr is not None else None
    if g is None or "buffable" not in g:
        ctx.gaps.append("GiveRandomCombatKeyword：缺门数据 `buffable`（不猜、不改状态）")
        return None
    if not g["buffable"]:                                                # `CanCardBeBuffed` 为假（`:22692`）
        write_out(frame, e, 2, 0)
        write_out(frame, e, 3, False)
        return None
    valid = valid_combat_keywords(existing_combat_keywords(getattr(u, "kw", ()), getattr(u, "armor", 0)),
                                  in_frontline=(u.row == "frontline"), is_fighter=(u.typ == "fighter"),
                                  has_guard=("guard" in (getattr(u, "kw", ()) or ())))
    if not valid:
        write_out(frame, e, 2, 0)
        write_out(frame, e, 3, False)
        return None                                                      # `Set_Length > 0` 为假（`Label_1895`）
    n = len(valid)
    if ctx.rng is not None:
        idx = ctx.rng.wrapper_int(0, n - 1)
    elif ctx.pick is not None:
        idx = ctx.pick("GiveRandomCombatKeyword", n)
    else:
        ctx.gaps.append("GiveRandomCombatKeyword：没有牌局随机流（`ctx.rng`）也没有枚举回调 ⇒ 选不出（不猜）")
        return None
    kw = valid[idx]
    inst = args[1] if len(args) > 1 else 0
    write_out(frame, e, 2, COMBAT_KEYWORD_ORDER.index(kw) + 1)
    write_out(frame, e, 3, True)
    if kw == "heavyarmor":
        ctx.applied.append(("change_heavy", u.id, 1, 1))
        if change_heavy_armor(u, 1, 1):
            ctx.fire("abilities_changed", u.id)
    else:
        GIVE_SINKS["Give" + kw.capitalize()](ctx, vm, frame, obj, [u.id, inst], e)
    return None


#: 偷进牌库 / 分出胜负 / 回合倒数 / 随机弃牌（第二十一刀）。
MISC_SINKS: Dict[str, Callable[..., Any]] = {
    "GiveRandomCombatKeyword": _sink_give_random_kw,
    "StealCardFromBoardToDeck": _sink_steal_to_deck,
    "EndMatch": _sink_end_match,
    "SetCountdown": _sink_set_countdown,
    "DiscardRandomCardFromHand": _sink_discard_random,
}


def _sink_make_veteran(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MakeVeteran(class UBaseCardObject* card, int& qqq)`（`:7127`）★ **首参是指针**。

    规则本体在 `engine.natives.stats.apply_veteran`（第二十二刀从 `sim` 下沉、实现一字不差 ✓）
    ⇒ sink 只解析目标 + 取 `vet` 载荷 + 扇 `("veteran", uid)`（OnBecomingVeteran + 0x20）。
    ★ `vet` 载荷（`<名>_vet` 静态卡的**绝对值**）在录制期是用**存活视图**算的
      （`effectvm._veteran_payload`）⇒ 直跑由调用方放 `ctx.gates[uid]["vet"]`（同一个算法、同一份数据）。
      没给 ⇒ 规则内会"只打标记 + 记缺口"（不编数值 ✓）。
    """
    from engine.natives.stats import apply_veteran                         # noqa: PLC0415
    u = ctx.unit_arg(args[0] if args else None)
    if u is None:
        ctx.gaps.append("MakeVeteran：目标指针/ID 换不出场上的单位（未结算）")
        return None
    vet = (ctx.gates.get(u.id) or {}).get("vet") if ctx.gates else None
    was = "veteran" in (getattr(u, "kw", ()) or ())
    ctx.gaps += apply_veteran(u, vet)
    ctx.applied.append(("veteran", u.id, isinstance(vet, dict)))
    if not was and "veteran" in (getattr(u, "kw", ()) or ()):
        ctx.fire("veteran", u.id)                        # 0x20（扇出交调用方）
    return None


#: 老兵（第二十二刀）。
VETERAN_SINKS: Dict[str, Callable[..., Any]] = {
    "MakeVeteran": _sink_make_veteran,
}


# ---------------------------------------------------------------------------
# 生成到棋盘（第二十三刀，2026-10-04）
# ---------------------------------------------------------------------------
def _spawn_one(ctx: DirectCtx, name, side, row_):
    """生成一张牌到 `row_`（`SpawnCardInFrontline`/`SpawnCardonBattlefield`/`SpawnMultipleCardsOnBattlefield` 共用）。
    → `(状态, uid)`：`"ok"`；`"full"`（原版判定不过 ⇒ 不生成，合法 no-op）；`"gap"`（面板读不到，已记缺口）。"""
    from engine.natives.board import can_spawn_card                   # noqa: PLC0415
    if not can_spawn_card(ctx.state, side, row_):
        return "full", None
    stat = ctx.spawn_stat(name) if ctx.spawn_stat else None
    if not stat:
        ctx.gaps.append("spawn：%s 的面板读不到，生成的单位没法入模拟（不编）" % name)
        return "gap", None
    ctx.state.tmp_seq = int(getattr(ctx.state, "tmp_seq", 0) or 0) + 1
    uid = -(2000 + ctx.state.tmp_seq) if side == ctx.my_side else -(2100 + ctx.state.tmp_seq)
    from engine.state import U                                         # noqa: PLC0415
    ctx.state.units[uid] = U(uid, side, row_, stat.get("atk", 0), stat.get("dfn", 0),
                             stat.get("cost", 0), stat.get("typ", "infantry"),
                             stat.get("kw", ()), sick=True)
    ctx.spawned.register(uid, name, int(side), "board", row_)          # `GetCardFromID(spawnedCardID)` 认得它（engine/spawned.py）
    # 载荷带全（side/row/面板）⇒ `engine.calls` 的重放不用再问卡库（"信息不全就不猜"的前提补齐了）
    ctx.applied.append(("spawn", uid, name, side, row_, stat.get("atk", 0), stat.get("dfn", 0),
                        stat.get("cost", 0), stat.get("typ", "infantry"), tuple(stat.get("kw", ()) or ())))
    ctx.fire("spawn", uid)                             # 进场钩子（`ExecuteOnEnterPlayEvents` 一类）交调用方
    return "ok", uid


def _spawn_sink(name_idx: int, side_idx: int, row: str, front_idx: Optional[int] = None, out_idx: Optional[int] = None):
    """生成 `SpawnCardInFrontline` / `SpawnCardonBattlefield` 的 sink（两条包装的**位次不同**）：
       * `SpawnCardInFrontline(FName card_name, ESideEnum side, …)` ⇒ name@0、side@1、落**前线**；
       * `SpawnCardonBattlefield(ESideEnum side, bool Frontline, FName card_name, …)` ⇒ side@0、name@2、落**支援线**。
       与 `sim:479-497` 同口径：`can_spawn_card` 为假（排满 / 前线被对方占）⇒ **不生成**（合法 no-op）；
       面板查不到 ⇒ **记缺口、不生成**（不编）；生成出来的单位 `sick=True`、id 用负号流水（与 `sim` 同一套 ✓）。
       ★ 原版生成后还要跑 `ExecuteOnEnterPlayEvents` 一类（`sim` 里是 `enter_mods`）—— 那是调用方的事 ⇒
         这里扇 `("spawn", uid)` 事件把"新单位进场"交出去（不假装跑过那些钩子）。
    """
    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        row_ = row
        if front_idx is not None:           # `SpawnCardonBattlefield(side, Frontline, …)`：Frontline 真 ⇒ 前线（0x7），假 ⇒ 支援线
            row_ = "frontline" if (args[front_idx] if len(args) > front_idx else False) else "back"
        name = args[name_idx] if len(args) > name_idx else None
        side = ctx.seat_of(args[side_idx] if len(args) > side_idx else None)
        if side is None or not isinstance(name, str) or not name or name == "None":
            ctx.gaps.append("spawn：卡名/side 读不出（未生成）")
            if out_idx is not None:
                write_out(frame, e, out_idx, 0)                      # `spawnedCardID` 保持原版默认 0（没生成）
            return None
        _st, _uid = _spawn_one(ctx, name, side, row_)
        if out_idx is not None:
            # 出参 `spawnedCardID`（BP 默认 0 = 没生成）：ENCIRCLEMENT 紧接着 `GiveBlitz(spawnedCardID, …)`（card_event_encirclement.cpp:Label_10）
            write_out(frame, e, out_idx, _uid if _uid is not None else 0)
        return None
    return _sink


def _sink_spawn_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SpawnMultipleCardsOnBattlefield(ESideEnum side, bool Frontline, const TArray<FName>*& cardNames, int spawnerID,
    bool giveBlitz, TArray<int>& spawnedCardIDs, bool makeVeteran)`（BP_CardFunctions.cpp:8407-8610，逐行读过）。

    原版：`cardNames` 非空 且 `spawnerID > 0` 才干活（否则出参 = 空数组）；逐名生成到 `Frontline ? 前线(0x7) : 该方支援线`，
    **某一次发现该排已满 ⇒ break**（不是 continue），每张 `enterPlayOnTurn = 当前回合`；全部生成后对每张：
    `OnOtherCardCreatedAlterCard`/`ExecuteOnEnterPlayEvents`（调用方的钩子层，这里扇 `spawn` 事件）、`giveBlitz` ⇒
    `GiveBlitz(新牌 id, spawnerID)`、有 alpine ⇒ `GiveAlpineBonus`（**未建模** ⇒ 记缺口）。
    出参 `spawnedCardIDs` = 新牌的 **id**（PARACHUTE ASSAULT 之后逐张 `GiveShock(id)`）⇒ 写进第 6 个 kid。
    `makeVeteran` 为真 ⇒ 记缺口（没有老兵载荷）。"""
    from engine.scripts_sinks_core import GIVE_SINKS                    # noqa: PLC0415
    names = args[2] if len(args) > 2 and isinstance(args[2], (list, tuple)) else []
    spawner = args[3] if len(args) > 3 else 0
    side = ctx.seat_of(args[0] if args else None)
    ids: list = []
    if not names or not isinstance(spawner, int) or spawner <= 0:
        write_out(frame, e, 5, ids)
        return None
    if side is None:
        ctx.gaps.append("SpawnMultipleCardsOnBattlefield：side 读不出（未生成）")
        write_out(frame, e, 5, ids)
        return None
    row_ = "frontline" if (len(args) > 1 and args[1]) else "back"
    for nm in names:
        if not isinstance(nm, str) or not nm or nm == "None":
            ctx.gaps.append("spawn：卡名读不出（未生成）")
            continue
        st_, uid = _spawn_one(ctx, nm, side, row_)
        if st_ == "full":
            break                                         # 原版 `Temp_bool_True_if_break_was_hit`
        if uid is not None:
            ids.append(uid)
    write_out(frame, e, 5, ids)
    if len(args) > 6 and args[6]:
        ctx.gaps.append("SpawnMultipleCardsOnBattlefield：makeVeteran 未建模（不编）")
    if len(args) > 4 and args[4]:
        for uid in ids:
            GIVE_SINKS["GiveBlitz"](ctx, vm, frame, obj, [uid, spawner], e)
    for uid in ids:
        u = ctx.state.units.get(uid)
        if u is not None and "alpine" in (getattr(u, "kw", ()) or ()):
            ctx.gaps.append("SpawnMultipleCardsOnBattlefield：新牌带 alpine ⇒ GiveAlpineBonus 未建模")
    return None


#: 生成到棋盘已迁动词（第二十三刀）。
SPAWN_SINKS: Dict[str, Callable[..., Any]] = {
    "SpawnCardInFrontline": _spawn_sink(0, 1, "frontline", None, 5),
    "SpawnCardOnBattlefield": _spawn_sink(2, 0, "back", 1, 10),
    "SpawnMultipleCardsOnBattlefield": _sink_spawn_multiple,
}


# ---------------------------------------------------------------------------
# 转化（第二十四刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_convert(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ConvertCard(const TArray<int>*& cardIDs, int instigatorID, FName convertToCardName,
    int convertIntoCardID, bool skipTrigger, …)` ★ **数组是 ID**（签名逐字）。

    与 `sim:968-1029`（`_apply_convert`）**同一套落点**，但**不使用效果字典**（P4 的方向）：
      * **在场** ⇒ 旧牌离场（扇 `("convert_old", uid)`，钩子交调用方）→ 在同侧同行造新牌（出厂数值 + `sick=True`）
        → 新牌在前线则去掉 `smokescreen`（原版 `:998`/`:12686`）；
      * **手牌/牌库** ⇒ 同位置换新牌（新模板用出厂数值；**新牌的效果留空并记缺口** ✓）；
      * 最后扇 `("convert", None)`（`OnOtherCardConverted(0x22)`/`ExecuteOnEnterPlayEvents` 的后果由调用方按
        预计算的 fx 跑 —— 那是 L1 钩子层的事 ✓）。
    ★ 载荷 `ctx.payload["convert"]`（目标卡数值 `atk/dfn/cost/typ/kw`、`ids`、`name`、`skip_trigger`）在录制期
      由 `effectvm._convert_payload` 用**存活视图**算出 ⇒ 直跑由调用方喂；**没喂 ⇒ 记缺口、不猜**（不编数值 ✓）。
    """
    cv = (ctx.payload or {}).get("convert")
    if not isinstance(cv, dict):
        ctx.gaps.append("convert：没拿到目标卡数值（`ctx.payload['convert']` 缺）⇒ 不结算（不编）")
        return None
    ids = list(cv.get("ids") or [])
    if not ids:
        ctx.gaps.append("convert：载荷里没有待转化的卡 id（不结算）")
        return None
    from engine.natives.convert import convert_args, convert_one            # noqa: PLC0415
    for old in ids:
        kind, nid, gap = convert_one(ctx.state, old, cv, on_old=lambda uid: ctx.fire("convert_old", uid))   # 旧牌离场钩子（此刻还在场）
        if kind is None:
            ctx.gaps.append(gap)
            continue
        # 载荷带全（新牌出厂数值）⇒ `engine.calls` 重放不用再问载荷（同 `spawn` 条目的做法）
        ctx.applied.append(("convert_" + kind, old, nid) + convert_args(cv))
        if kind == "board":
            ctx.fire("convert_new", nid)
        elif gap:
            ctx.gaps.append(gap)
    ctx.fire("convert", None)                                              # 0x22（整次转化一轮）
    if cv.get("skip_trigger"):
        ctx.applied.append(("convert_skip_trigger", True))
    return None


#: 转化（第二十四刀）。
CONVERT_SINKS: Dict[str, Callable[..., Any]] = {"ConvertCard": _sink_convert}


# ---------------------------------------------------------------------------
# 游戏限制 / 情报计数（第二十五刀，2026-10-04）：真有消费者的做真 sink
# ---------------------------------------------------------------------------
def _sink_add_restriction(ctx: DirectCtx, vm, frame, obj, args, e):
    """`AddGameplayRestriction(ESideEnum side, EGameplayRestrictions type, int cardID, int turnsToLast)`
    ⇒ 与 `sim:807-808` 同口径（影子列表 `s.restrictions`），条目形状与录制侧**逐字一致**：
    `{"side", "type", "turns"}`（`effectvm` 的 `restriction_add` ✓）。
    """
    seat = ctx.seat_of(args[0] if args else None)
    if seat is None:
        ctx.gaps.append("AddGameplayRestriction：side=%r 读不出（未结算）" % (args[0] if args else None,))
        return None
    entry = {"side": seat, "type": int(args[1]) if len(args) > 1 else 0,
             "turns": int(args[3]) if len(args) > 3 else 0}
    # ★ 别写 `(getattr(...) or [])` —— 空列表是 falsy，`[] or []` 会造一个**新列表**、append 就丢了 ✗
    if getattr(ctx.state, "restrictions", None) is None:
        ctx.state.restrictions = []
    ctx.state.restrictions.append(dict(entry))
    ctx.applied.append(("restriction_add", entry["side"], entry["type"], entry["turns"]))
    return None


def _sink_remove_restriction(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RemoveGameplayRestriction(ESideEnum side, EGameplayRestrictions type, int cardID, bool removeAll, …)`
    ⇒ 与 `sim:809-811` 同口径：**按 side+type** 移除（形状同录制侧 ✓）。"""
    seat = ctx.seat_of(args[0] if args else None)
    if seat is None:
        ctx.gaps.append("RemoveGameplayRestriction：side=%r 读不出（未结算）" % (args[0] if args else None,))
        return None
    ty = int(args[1]) if len(args) > 1 else 0
    cur = list(getattr(ctx.state, "restrictions", None) or [])
    ctx.state.restrictions = [x for x in cur
                              if not (x.get("side") == seat and x.get("type") == ty)]
    ctx.applied.append(("restriction_remove", seat, ty))
    return None


def _sink_seen_by_cipher(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SetCardsSeenByCipher(int numberOfCardsSeen, int instigatorID, ESideEnum side, int& qqq)`
    ⇒ 录制侧把它记成 `intel_seen`（`effectvm:906`），消费者是 **rule 层**的 `_intel_triggers`
    （跑 0x1C 触发；`effectvm:210` 的注释 ✓）⇒ 直跑把它记成**事件**交调用方（不在 engine 里跑触发）。
    """
    n = int(args[0]) if args and isinstance(args[0], (int, float)) else 0
    seat = ctx.seat_of(args[2]) if len(args) > 2 else None
    if n <= 0:
        return None                                     # 录制侧同口径：`n > 0` 才记
    if seat is None:
        ctx.gaps.append("SetCardsSeenByCipher：side=%r 读不出（未结算）" % (args[2] if len(args) > 2 else None,))
        return None
    ctx.applied.append(("intel_seen", n, seat))
    ctx.fire("intel_seen", n)
    return None


#: 真有消费者的限制族（第二十五刀）。
RESTRICTION_SINKS: Dict[str, Callable[..., Any]] = {
    "AddGameplayRestriction": _sink_add_restriction,
    "RemoveGameplayRestriction": _sink_remove_restriction,
    "SetCardsSeenByCipher": _sink_seen_by_cipher,
}


# ---------------------------------------------------------------------------
# 明确"未建模"的动词（第二十五刀）：**显式记缺口**，比落到录制侧悄悄过去强
# ---------------------------------------------------------------------------
#: 这些键在 `sim/`、`policy/`、`engine/` 里**实测零消费者**（2026-10-04 逐个 grep 过，不是"我记得"）：
#:   `gameplay_effect` / `gameplay_tag` / `custom_name`（`sim/` 0 处）/ `effect_type` /
#:   `extra_play` / `card_attributes` / `copy_data` / `create_copy`。
#: ⇒ 直跑给它们显式记一条缺口（**不编一个没人读的字段/效果** ✓），也让"缺口清单"完整 ——
#: 这正是 P4 收尾条件里"`effectvm` 降级为缺口审计"要的样子 ✓。
_UNMODELED_REASON = {
    "CustomName1Add": "自定义名 1（sim/policy/engine 里零消费者 ⇒ 未建模；rule 读的是游戏内存里的属性）",
    "CustomName1Remove": "自定义名 1（同上）",
    "CustomName2Add": "自定义名 2（同上）",
    "CustomName2Remove": "自定义名 2（同上）",
    "ApplyGameplayEffect": "GameplayEffect 应用（`gameplay_effect` 键零消费者 ⇒ 未建模）",
    "RemoveGameplayEffectByInstigator": "按来源移除 GameplayEffect（同上）",
    "RemoveGameplayTag": "移除 GameplayTag（`gameplay_tag` 键零消费者 ⇒ 未建模）",
    "SetExtraPlayTriggers": "额外出牌触发（`extra_play` 键零消费者 ⇒ 未建模）",
    "UpdateExtraPlayTriggers": "额外出牌触发（同上）",
    "ResetCardAttributes": "重置卡属性（`card_attributes` 键零消费者 ⇒ 未建模）",
    "CopyData": "拷贝数据（`copy_data` 键零消费者 ⇒ 未建模）",
    "CreateCopy": "创建副本（`create_copy` 键零消费者 ⇒ 未建模）",
}


def _unmodeled_sink(verb: str, reason: str):
    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        ctx.gaps.append("%s：%s" % (verb, reason))
        return None
    return _sink


#: 明确未建模族（第二十五刀）。
UNMODELED_SINKS: Dict[str, Callable[..., Any]] = {
    v: _unmodeled_sink(v, r) for v, r in _UNMODELED_REASON.items()}


# ---------------------------------------------------------------------------
# 抉择/预报的"挂起标记"（第二十六刀，2026-10-04）
# ---------------------------------------------------------------------------
#: ★ 量出来的结论（2026-10-04）：这三个动词**不是状态变更**，它们只是"弹出一个待玩家/待预测的选择"——
#:   消费者在**别处**：`selectCardToDraw` 的候选由 `semantics/choosespawn.py` 按活种子预测（`sim` 挂起成
#:   `select_card_to_draw`）；`Forecast` 的 9 条路径由 `rule._hand_eff` 展开；`selectTargetFromHand` 的后果在
#:   之后的 `OnHandTargetSelected`。⇒ 直跑（以及 `sim`）在**拿不到预测/后果**时**如实记缺口、绝不编一个分支** ✗；
#:   同时把"这里挂了一个选择"作为**事件**扇给调用方 ✓（它才知道该选哪个）。
#:   `WhichChooseOne`（CHOICE_VERBS）**不在**这里：它走录制侧的**分支枚举**（`Recorder.choice` +
#:   `enumerate_effects`），直跑不该拦它 ✓。
def _pending_sink(kind: str, gap: str, cid_from: str = "card"):
    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        cid = args[0] if args and isinstance(args[0], (int, float)) else None
        ctx.applied.append((kind, cid))
        ctx.fire(kind, cid)                      # 挂起标记 ⇒ 调用方（rule/预测）处理 ✓
        ctx.gaps.append(gap)
        return None
    return _sink


#: 抉择/预报挂起族（第二十六刀）。
PENDING_SINKS: Dict[str, Callable[..., Any]] = {
    "selectCardToDraw": _pending_sink(
        "choose_spawn_pending",
        "choose_spawn：三选一加入手牌（selectCardToDraw）的候选未展开（没有可用的种子预测）"),
    "Forecast": _pending_sink(
        "forecast_pending",
        "forecast：预报选哪张（两层 3×3）未展开（没有可用的种子预测）"),
    "selectTargetFromHand": _pending_sink(
        "hand_target_pending",
        "hand_target：部署后要选一张手牌（OnHandTargetSelected 的后果）未建模"),
}


# ---------------------------------------------------------------------------
# 收尾：绝对值指挥点 / 洗牌 / 往牌库塞牌（第二十七刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_set_kredit_by_side(ctx: DirectCtx, vm, frame, obj, args, e):
    """`setKreditBySide(side, newValue)`（录制键 `kredit_set`，数值在 **1**、side 在 **0**）。

    我方有绝对值 ⇒ 直接写（`sim` 没有 `kredit_set` 消费者：录制侧是把它折算成**增量**再进 `kredit` ✓）；
    对方没有绝对值（我们只有增量视角）⇒ **记缺口、不改状态**（与 `setKreditSlotBySide(对方)` 同一口径 ✓）。
    """
    seat = ctx.seat_of(args[0] if args else None)
    val = args[1] if len(args) > 1 else None
    if seat is None or not isinstance(val, (int, float)):
        ctx.gaps.append("setKreditBySide：side/newValue 读不出（未结算）")
        return None
    if seat != ctx.my_side:
        ctx.gaps.append("setKreditBySide：**对方**的指挥点绝对值我们不知道（只有增量视角）⇒ 不猜")
        return None
    ctx.state.kredits = float(val)
    ctx.applied.append(("kredit_set", seat, val))
    return None


def _sink_shuffle_deck(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ShuffleDeckBySide(ESideEnum sideToShuffle, bool skipSubAction, int instigatorID, bool& qqq)`
    ⇒ 与 `sim:849-856` 同口径：洗的是**己方牌库的 id 列表**，用**牌局随机流**（`Stream.shuffle` =
    正向 Fisher-Yates ✓）；**没注入流** ⇒ 记缺口 + 把牌库标成未知（`deck=[], deck_known=False`，同 `sim` ✓）。
    记 `("deck_shuffle", 座位, 洗后牌序)`：重放照记下的牌序还原（不依赖重放侧的随机流位置）。
    """
    from engine.natives.deck import shuffle_deck                       # noqa: PLC0415
    seat = ctx.seat_of(args[0] if args else None)
    if seat is None:
        ctx.gaps.append("ShuffleDeckBySide：side=%r 读不出（未结算）" % (args[0] if args else None,))
        return None
    if seat != ctx.my_side:
        ctx.gaps.append("ShuffleDeckBySide：洗**对方**牌库未建模（牌序读不到）")
        return None
    ok = shuffle_deck(ctx.state, ctx.rng, gaps=ctx.gaps)
    if ok:
        ctx.applied.append(("deck_shuffle", seat, tuple(ctx.state.deck)))
    return None


def _sink_spawn_in_deck(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SpawnCardInDeckBySide(ESideEnum side, FName card_name, int spawnerID, int numberOfCards,
    EFactionEnum salvageFaction, bool hideFromOpponent, bool bottom, bool shuffle,
    …, bool randomWithoutShuffle, …)` ⇒ 与 `sim:826-848` 同口径。

    ★ 每张牌**都要抽一次** `RandomIntFromRangeWithStream(0, len)`（BP 在循环里**无条件**调用，
      结果只在 `RandomWithoutShuffle` 时用作插入位置）—— 不抽就会让**之后的洗牌/抽牌跟游戏错位** ✗。
    ★ **卡模板/数值原版要读静态卡表**（那是调用方的事）⇒ 这里造**中性占位模板**（费用 2、`order`、
      `eff` 留空）并**如实记缺口**（不编面板 ✓）。
    """
    from engine.natives.deck import shuffle_deck, spawn_in_deck        # noqa: PLC0415
    seat = ctx.seat_of(args[0] if args else None)
    name = args[1] if len(args) > 1 else None
    spawner = args[2] if len(args) > 2 else 0
    n = int(args[3]) if len(args) > 3 and isinstance(args[3], (int, float)) else 0
    bottom = bool(args[6]) if len(args) > 6 else False
    do_shuffle = bool(args[7]) if len(args) > 7 else False
    random_wo_shuffle = bool(args[9]) if len(args) > 9 else False         # 签名位次：…, shuffle@7, SkipDrawAnimation@8, RandomWithoutShuffle@9, ids@10
    if seat is None or not isinstance(name, str) or not name or n <= 0:
        ctx.gaps.append("SpawnCardInDeckBySide：side/卡名/张数读不出（未结算）")
        return None
    if isinstance(spawner, int) and spawner <= 0:
        return None                                                      # 原版 `spawnerID > 0` 才干活（`:6650`）
    if seat != ctx.my_side:
        ctx.gaps.append("SpawnCardInDeckBySide：往**对方**牌库塞牌未建模（牌序读不到）")
        return None
    if not getattr(ctx.state, "deck_known", False):
        ctx.gaps.append("SpawnCardInDeckBySide：牌库未知 ⇒ 只记缺口（同 `sim`）")
        return None
    placed = spawn_in_deck(ctx.state, name, n, bottom=bottom, random_wo_shuffle=random_wo_shuffle, rng=ctx.rng, gaps=ctx.gaps)
    for cid, idx in placed:
        ctx.applied.append(("deck_add", cid, name, idx))
    if do_shuffle:
        if shuffle_deck(ctx.state, ctx.rng, gaps=ctx.gaps):
            ctx.applied.append(("deck_shuffle", seat, tuple(ctx.state.deck)))
    ctx.gaps.append("SpawnCardInDeckBySide：新牌的**效果**要读它自己的字节码（调用方的事）⇒ 面板读得到才用，效果留空并记缺口")
    return None


# ---------------------------------------------------------------------------
# 协力族 sink（2026-10-06）：`RemoveBond`（RATIONING 分支 0 的叶子）
# ---------------------------------------------------------------------------
def _sink_remove_bond(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RemoveBond(cardID, instigatorID, &qqq)`（`BP_CardFunctions.cpp:23020-23032`）：给手牌写自定义能力 `bond_removed`
    （⇒ `HasBond` 变假）。★ 第一个实参是**卡 ID**（不是指针）。出参 `qqq`（下标 2）恒写 0（`:23030`）。

    目标不在手牌（RATIONING 只遍历手牌，正常不会发生）⇒ 记缺口、不改状态。已移除过 ⇒ 幂等（不重复记 `applied`）。
    """
    from engine.natives.bond import remove_bond                          # noqa: PLC0415
    cid = args[0] if args else None
    write_out(frame, e, 2, 0)
    if cid not in (getattr(ctx.state, "hand", {}) or {}):
        ctx.gaps.append("RemoveBond：cardID=%r 不在手牌里（未结算）" % (cid,))
        return None
    if remove_bond(ctx.state, cid):
        ctx.applied.append(("remove_bond", cid))
    return None


#: 协力族已迁动词。
BOND_SINKS: Dict[str, Callable[..., Any]] = {
    "RemoveBond": _sink_remove_bond,
}

#: 收尾族（第二十七刀）：绝对值指挥点 / 洗牌 / 往牌库塞牌 / 单卡收缴。
FINAL_SINKS: Dict[str, Callable[..., Any]] = {
    "setKreditBySide": _sink_set_kredit_by_side,
    "ShuffleDeckBySide": _sink_shuffle_deck,
    "SpawnCardInDeckBySide": _sink_spawn_in_deck,
    "SalvageUnit": _sink_salvage_multiple,                   # 与 `SalvageMultipleUnits` **同一套**（它俩都在 SALVAGE_VERBS）
}
