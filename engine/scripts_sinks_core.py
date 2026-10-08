#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.scripts_sinks_core —— 直跑 sink 表（前半：资源 / 状态 / 数值 / 生命周期 / 伤害 / 抽牌 / 撤退 / 关键词 / 手牌 / AOE / 能力 / 税 / 揭示 / 叶写入 / 棋盘）。

P6 自 `engine/scripts.py` 原样拆出（纯搬移）；`engine.scripts` 汇总各表成 `DEFAULT_SINKS`。
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from engine.scripts_ctx import DirectCtx, write_out


# ---------------------------------------------------------------------------
# 资源族 sinks（第一刀）
# ---------------------------------------------------------------------------
def _kredits(ctx: DirectCtx, side, delta: int) -> None:
    """`ChangeKreditsBySide(side, delta, instigatorID)` 的等价物（夹取见 `engine.natives.kredits`）。

    我方走 `change_kredits`（绝对值 + 夹 `[0, kredit_max]` ✓）；对方我们只有增量累加器
    （`opp_kredits`，与 `sim._res_eff` 同一口径 ✓）。
    """
    from engine.natives.kredits import change_kredits
    if ctx.is_mine(side):
        change_kredits(ctx.state, delta)
    else:
        ctx.state.opp_kredits = getattr(ctx.state, "opp_kredits", 0) + delta
    ctx.applied.append(("kredits", "mine" if ctx.is_mine(side) else "opp", delta))


def _slots(ctx: DirectCtx, side, delta: int) -> None:
    """`ChangeKreditSlotsBySide(side, delta, giverID)`：我方走 `change_slots`，对方记增量。"""
    from engine.natives.kredits import change_slots
    if ctx.is_mine(side):
        change_slots(ctx.state, delta)
    else:
        ctx.state.opp_slots = getattr(ctx.state, "opp_slots", 0) + delta
    ctx.applied.append(("slots", "mine" if ctx.is_mine(side) else "opp", delta))


def _sink_give_kredits(ctx, vm, frame, obj, args, e):
    """`GiveKreditsBySide(side, kredits, instigatorID, &qqq)`（`:12394-12401`）。"""
    side = args[0] if len(args) > 0 else None
    amount = args[1] if len(args) > 1 else 0
    _kredits(ctx, side, int(amount or 0))
    write_out(frame, e, 3, False)                            # 原版 `qqq = false`（:12398）
    return None


def _sink_gain_slot(ctx, vm, frame, obj, args, e):
    """`GainKreditSlot(cardGivingKreditSlot, side)`（`:2324`）：`ChangeKreditSlotsBySide(side, +1, cardID)`。"""
    side = args[1] if len(args) > 1 else None
    _slots(ctx, side, 1)
    return None


def _sink_lose_slot(ctx, vm, frame, obj, args, e):
    """`LoseKreditSlot(side)`（`:2387`）。"""
    side = args[0] if len(args) > 0 else None
    _slots(ctx, side, -1)
    return None


def _sink_change_slots_by_side(ctx, vm, frame, obj, args, e):
    """`ChangeKreditSlotsBySide(sideToChange, slotChangeAmount, instigatorID)`（`:19504`，第十三刀）。

    ★ 它是**增量**语义（`slotChangeAmount` 就在参数里）—— 与 `setKreditSlotBySide`（绝对值）**不是一回事**：
    绝对值那条对"对方"只能记缺口（我们没有对方的当前槽），而增量这条对方也能算（`opp_slots` 是增量累加器 ✓）。
    ★ 位次：`side` 在 0、`delta` 在 1（录制表 `("slot", 1, None, 0)` 的第 2 项是**数值位**，别当成 side 位）。
    """
    side = args[0] if args else None
    delta = args[1] if len(args) > 1 else 0
    _slots(ctx, side, delta)
    return None


def _sink_set_slot(ctx, vm, frame, obj, args, e):
    """`setKreditSlotBySide(side, newValue)`：**绝对值**语义。

    我方有绝对值 ⇒ 换算成增量直跑；对方没有绝对值（`opp_slots` 是增量累加器）⇒
    **记缺口、不改状态**（调用方据此回退录制路径），绝不猜对方的当前槽。
    """
    side = args[0] if len(args) > 0 else None
    new_value = args[1] if len(args) > 1 else None
    if new_value is None:
        ctx.gaps.append("setKreditSlotBySide：newValue 读不出，未改槽")
        return None
    if not ctx.is_mine(side):
        ctx.gaps.append("setKreditSlotBySide(对方)：对方槽的**绝对值**我们没建模（只有增量累加器）"
                        "⇒ 不猜、回退录制路径")
        return None
    cur = float(getattr(ctx.state, "slots", 0) or 0)
    _slots(ctx, side, int(float(new_value) - cur))
    return None


#: 已迁到直跑的动词 → sink。**没在这张表里的动词仍走录制路径**（`native_hooks` 的 fallback）。
RESOURCE_SINKS: Dict[str, Callable[..., Any]] = {
    "GiveKreditsBySide": _sink_give_kredits,
    "ChangeKreditsBySide": _sink_give_kredits,   # 同族直接调用点（`:12396`）：同样的加/减
    "GainKreditSlot": _sink_gain_slot,
    "LoseKreditSlot": _sink_lose_slot,
    "setKreditSlotBySide": _sink_set_slot,
    "ChangeKreditSlotsBySide": _sink_change_slots_by_side,   # 第十三刀：**增量**语义的 by-side 变体
}


# ---------------------------------------------------------------------------
# 状态族 sinks（第二刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_pin(ctx: DirectCtx, vm, frame, obj, args, e):
    """`PinUnit(card, …)`：置 `pinned`（原版 `PinUnit` 只动 `receivedAbilities`/`pinnedTurns`，
    见 `engine/effectvm.py:95` 的说明）。事件 0x3D 记进 `ctx.events`，**不丢**。"""
    ptr0 = args[0] if args else None
    if ctx.hq_side_of(ptr0) is not None:
        # 原版 `PinUnit`（`BP_CardFunctions.cpp:998-1001`）：`!IsUnit(card)` ⇒ 直接 return —— 总部不是单位，**合法 no-op**（不是缺口）。
        return None
    u = ctx.unit_arg(ptr0)
    if u is None:
        ctx.gaps.append("PinUnit：目标指针换不出场上的单位（未结算）")
        return None
    u.pinned = True
    ctx.applied.append(("pin", u.id))
    ctx.fire("pin", u.id)
    return None


def _sink_unpin(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RemovePin(card, …)`：清 `pinned`（旧路径没有事件扇出 ⇒ 这里也不发）。"""
    u = ctx.unit_of(args[0] if args else None)
    if u is None:
        ctx.gaps.append("RemovePin：目标指针换不出场上的单位（未结算）")
        return None
    u.pinned = False
    ctx.applied.append(("unpin", u.id))
    return None


def _suppress_one(ctx: DirectCtx, ptr) -> None:
    """对一张牌执行压制（走 `engine.natives.status.apply_suppress` 这份唯一的规则实现）。"""
    from engine.natives.status import apply_suppress                  # noqa: PLC0415
    u = ctx.unit_arg(ptr)
    if u is None:
        ctx.gaps.append("suppress：目标指针换不出场上的单位（未结算）")
        return
    ctx.gaps += list(apply_suppress(ctx.state, u.id))                 # 它自己会把"没建模的那部分"如实返回
    ctx.applied.append(("suppress", u.id))
    ctx.fire("suppress", u.id)                                        # 0x3A


def _sink_suppress_unit(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SuppressUnit(card, …)`。"""
    _suppress_one(ctx, args[0] if args else None)
    return None


def _sink_suppress_multi(ctx: DirectCtx, vm, frame, obj, args, e):
    """`SuppressMultipleUnits(Cards[], …)`：逐张压制（原版在"新压制"分支里还会清 guard/fury/…，
    那些由 `apply_suppress` 的 `SUPPRESS_STRIP` 统一处理 ✓）。"""
    arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
    for ptr in arr:
        _suppress_one(ctx, ptr)
    return None


#: 状态族已迁动词（第二刀）。
STATUS_SINKS: Dict[str, Callable[..., Any]] = {
    "PinUnit": _sink_pin,
    "RemovePin": _sink_unpin,
    "SuppressUnit": _sink_suppress_unit,
    "SuppressMultipleUnits": _sink_suppress_multi,
}



# ---------------------------------------------------------------------------
# 数值族 sinks（第三刀，2026-10-04）
# ---------------------------------------------------------------------------
def _gate(ctx: DirectCtx, ptr, verb: str, key: str):
    """取门数据；取不到 ⇒ 记缺口并返回 None（sink 据此**不改状态**、交回退的录制路径）。"""
    g = ctx.gate_of(ptr)
    if g is None or key not in g:
        ctx.gaps.append("%s：缺门数据 `%s`（不猜、不改状态）" % (verb, key))
        return None
    return g.get(key)


def _sink_change_attack(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangeAttack(card, instigatorID, amount, changeType, skipAction, &qqq)`（`:10981-11212`）。

    门 = `CanCardBeBuffed`（`:10990-10993`）；门为假 ⇒ `qqq = false; return`（`:11023-11025`），
    **成功路径不动出参**（`qqq` 在函数体里只被赋 `false`，`:11019`/`:11024` ✓ —— 不猜 True）。
    """
    from engine.natives.stats import change_attack                       # noqa: PLC0415
    ptr = args[0] if args else None
    hq_side = ctx.hq_side_of(ptr)
    if hq_side is not None:
        # 总部牌：`ChangeAttack`（`:10981-11212`）对它与单位同一条路径（无 HQ 特判，`CanCardBeBuffed` 对非隐蔽牌恒真），
        # 会改总部牌自己的 attack 字段；但总部不会攻击、`Sim` 不跟踪总部攻击力 ⇒ 没有可写的状态。
        # 记一条 `change_attack_hq`（重放表里是**无状态**标记，不是缺口）。
        ctx.applied.append(("change_attack_hq", hq_side, args[2] if len(args) > 2 else 0,
                            args[3] if len(args) > 3 else None))
        return None
    u = ctx.unit_of(ptr)
    if u is None:
        ctx.gaps.append("ChangeAttack：目标指针换不出场上的单位（未结算）")
        return None
    ok = _gate(ctx, ptr, "ChangeAttack", "buffable")
    if ok is None:
        return None
    if not ok:
        write_out(frame, e, 5, False)                                    # 原版 `:11024`
        return None
    ctx.applied.append(("change_attack", u.id, args[2] if len(args) > 2 else 0,
                        args[3] if len(args) > 3 else None))
    change_attack(u, args[2] if len(args) > 2 else 0, args[3] if len(args) > 3 else None)
    return None


def _sink_change_opcost(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangeOperationCost(card, instigatorID, amount, changeType, isBuff, skipAction, skipAddToBattlelog)`
    （`:8892-9188`）：门 = `IsUnrevealedCovertCard` ⇒ 为真就**静默 return**（`:8906-8911`，无出参）。"""
    from engine.natives.stats import change_operation_cost               # noqa: PLC0415
    ptr = args[0] if args else None
    u = ctx.unit_of(ptr)
    if u is None:
        hc = ctx.hand_card_of(ptr)                                       # 手牌上的牌（IRON VICTORY：新补的 T-34 行动费设为 1）
        if hc is not None:
            return _opcost_on_hand(ctx, ptr, hc, args)
        info = ctx.spawned.info_of_handle(ptr)
        if info is not None and info.get("where") == "opp_hand":
            ctx.gaps.append("ChangeOperationCost：目标是刚进对方手牌的新牌（对方手牌只有张数、没有逐张状态）⇒ 未结算")
            return None
        ctx.gaps.append("ChangeOperationCost：目标指针换不出场上的单位（未结算）")
        return None
    covert = _gate(ctx, ptr, "ChangeOperationCost", "unrevealed_covert")
    if covert is None:
        return None
    if covert:
        return None                                                      # 原版静默不改 ✓
    ctx.applied.append(("change_opcost", u.id, args[2] if len(args) > 2 else 0,
                        args[3] if len(args) > 3 else None))       # ★ 带上 change type（重放要用 ✓，三角验证抓到的）
    change_operation_cost(u, args[2] if len(args) > 2 else 0, args[3] if len(args) > 3 else None)
    return None


def _opcost_on_hand(ctx: DirectCtx, ptr, card, args):
    """`ChangeOperationCost` 的目标是手牌里的牌：门同单位（未揭示隐蔽 ⇒ 静默不改），值语义见 `stats.change_hand_operation_cost`。"""
    from engine.natives.stats import change_hand_operation_cost            # noqa: PLC0415
    covert = _gate(ctx, ptr, "ChangeOperationCost", "unrevealed_covert")
    if covert is None or covert:
        return None
    amt, ct = (args[2] if len(args) > 2 else 0), (args[3] if len(args) > 3 else None)
    r = change_hand_operation_cost(card, amt, ct)
    if r is None:
        ctx.gaps.append("ChangeOperationCost：手牌的行动费基础值未知，加减型改动算不了（只有设值型能结算）")
        return None
    ctx.applied.append(("hand_opcost", card.id, amt, ct))
    return None


def _sink_change_heavy(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangeHeavyArmor(card, instigatorID, amount, changeType, skipAction, &qqq)`（`:10320-10507`）：
    门 = `IsUnrevealedCovertCard`（`:10332-10340`，`qqq = false` + return ✓）；夹取 `[0,3]`。"""
    from engine.natives.stats import change_heavy_armor                  # noqa: PLC0415
    ptr = args[0] if args else None
    u = ctx.unit_of(ptr)
    if u is None:
        ctx.gaps.append("ChangeHeavyArmor：目标指针换不出场上的单位（未结算）")
        return None
    covert = _gate(ctx, ptr, "ChangeHeavyArmor", "unrevealed_covert")
    if covert is None:
        return None
    if covert:
        write_out(frame, e, 5, False)                                    # 原版 `:10338-10340`
        return None
    ctx.applied.append(("change_heavy", u.id, args[2] if len(args) > 2 else 0,
                        args[3] if len(args) > 3 else None))       # ★ 带上 change type（重放要用 ✓）
    change_heavy_armor(u, args[2] if len(args) > 2 else 0, args[3] if len(args) > 3 else None)
    return None


#: 数值族已迁动词（第三刀）。
STATS_SINKS: Dict[str, Callable[..., Any]] = {
    "ChangeAttack": _sink_change_attack,
    "ChangeOperationCost": _sink_change_opcost,
    "ChangeHeavyArmor": _sink_change_heavy,
}


def _sink_change_defense(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangeDefense(card, instigatorID, amount, changeType, skipAction, &qqq)`（`:11215-11696`，第四刀）。

    门 = `CanCardBeBuffed`（`:11222-11225`，假 ⇒ `qqq=false` + return，`:11258`）；
    值语义走 `natives.stats.change_defense`（含"≤0 ⇒ 摧毁"与 `ct==2` 的 `OnAfterDefenseIsSet`+0x6）。
    """
    from engine.natives.stats import change_defense                      # noqa: PLC0415
    ptr = args[0] if args else None
    hq_side = ctx.hq_side_of(ptr)
    if hq_side is not None:
        # 总部牌：原版函数体对总部与单位**同一条路径**（无 HQ 特判）；`CanCardBeBuffed` 对非隐蔽牌恒真 ⇒ 不要门数据。
        # 前置：`instigatorID > 0`（`:11221`，否则记日志 + `qqq=false`）与 `总防 > 0`（`:11230`，否则 `Label_3831` return）。
        from engine.natives.stats import change_defense_hq               # noqa: PLC0415
        inst = args[1] if len(args) > 1 else None
        if (isinstance(inst, int) and inst <= 0) or ctx.state.hq.get(hq_side, 0) <= 0:
            write_out(frame, e, 5, False)
            return None
        amt = args[2] if len(args) > 2 else 0
        ct = args[3] if len(args) > 3 else None
        r = change_defense_hq(ctx.state, hq_side, amt, ct)
        if r.get("rejected"):
            write_out(frame, e, 5, False)                                # `Label_3734`/`Label_5740`
            return None
        ctx.applied.append(("change_defense_hq", hq_side, amt, ct))
        if r.get("after_set"):
            # `ct==2`：`OnAfterDefenseIsSet` + trigger 0x6 的事件扇出是按**单位 id** 的；总部没有 id 键 ⇒ 如实记缺口，不猜。
            ctx.gaps.append("ChangeDefense：总部 SetValue 的 OnAfterDefenseIsSet(0x6) 扇出未建模")
        # 总防 <= 0 ⇒ 原版 `DestroyCard(总部)` = 对局结束（终局链是调用方的事；`state.hq` 已写对）。
        return None
    u = ctx.unit_of(ptr)
    if u is None:
        ctx.gaps.append("ChangeDefense：目标指针换不出场上的单位（未结算）")
        return None
    ok = _gate(ctx, ptr, "ChangeDefense", "buffable")
    if ok is None:
        return None
    if not ok:
        write_out(frame, e, 5, False)                                    # 原版 `:11258`
        return None
    r = change_defense(u, args[2] if len(args) > 2 else 0, args[3] if len(args) > 3 else None)
    if r.get("rejected"):
        write_out(frame, e, 5, False)                                    # `Label_3734`/`Label_5740`
        return None
    ctx.applied.append(("change_defense", u.id, args[2] if len(args) > 2 else 0,
                        args[3] if len(args) > 3 else None))
    if r.get("destroy"):
        ctx.destroy(u.id, why="ChangeDefense 写完后总量 <= 0")
    if r.get("after_set"):
        ctx.fire("after_defense_set", u.id)                              # `OnAfterDefenseIsSet` + 0x6
    return None


STATS_SINKS["ChangeDefense"] = _sink_change_defense


# ---------------------------------------------------------------------------
# 生命周期族 sinks（第五刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_destroy_card(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DestroyCard(card, destroyerCard)`（`BP_CardFunctions.cpp:853-884`，逐行读）。

    原版顺序：`!IsValid(card)` ⇒ **直接 return**；否则 `ExecuteOnBeforeOtherCardDestroyed(cardID, destroyerID,…)`
    + `NotifyDestroyUnit(...)`（战报/UI，非状态）**先**发（`:877-879`，那时卡还在场），
    **再** `ApplyRemoveCardFromBoard(...)`（`:882`，真正的离场 + 其后的死亡链）。

    ⇒ 直跑照这个顺序：先记 `("before_other_card_destroyed", uid)` 事件，再 `ctx.destroy`（移场 + `("destroy", uid)`）。
      死亡链（0x27 + `death_fx`）仍由调用方扇出（第四刀定的边界 ✓）。
    """
    ptr = args[0] if args else None
    if ctx.hq_side_of(ptr) is not None:
        # 目标是**总部牌**：原版 `DestroyCard` 不看是不是单位（`:853-884`），会走 `ApplyRemoveCardFromBoard(总部)` ⇒ 对局结局
        # （终局链），`Sim` 没建模 ⇒ 如实记**专门**的缺口（不是"指针认不出"，也不假装 no-op / 不猜成 hq=0）。
        ctx.gaps.append("DestroyCard：目标是总部牌（原版摧毁总部 = 对局结局，未建模）")
        return None
    u = ctx.unit_of(ptr)
    if u is None:
        # 认不出指针 ≠ 原版的 `!IsValid`（卡是有效的、只是我们没这张的映射）⇒ 如实记缺口，不假装 return。
        ctx.gaps.append("DestroyCard：目标指针换不出场上的单位（未结算）")
        return None
    ctx.applied.append(("destroy_card", u.id))
    ctx.fire("before_other_card_destroyed", u.id)        # 原版 `:877`（在离场**之前**）
    ctx.destroy(u.id, why="DestroyCard")


#: 生命周期族已迁动词（第五刀）。
LIFECYCLE_SINKS: Dict[str, Callable[..., Any]] = {
    "DestroyCard": _sink_destroy_card,
}



# ---------------------------------------------------------------------------
# 伤害族 sinks（第六刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_damage_card(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DamageCard(card, amount, damagerCardID, isRedirected, fromFight, isFightDefenderDamage, &targetDestroyed)`
    （`BP_CardFunctions.cpp:895-955`，逐行读过）。

    ★ 这个动词与前面几族**不同**：它的函数体**含钩子链**
      （`ExecuteOnDealDamageAddDamage` → `ExecuteOnDealDamageAddDamageAfterCalc` → `ApplyDamageToCard`）
      ⇒ 直跑只做**状态那一段**（`ApplyDamageToCard` 的 excess + 扣防 + `<=0` 摧毁），
      钩子/受伤通知交调用方（与第四/五刀同一边界：直跑 = 状态变更 + 事件表）。
    ★ 两种"没有目标"要分开（同第五刀的分寸）：**在 `ptr_ids` 里但不在场上** = 原版
      `IsLocatedOnBoard` 为假 ⇒ **合法 no-op**（`:922` 的 `Label_425`，只打一行日志）；
      **认不出指针** ⇒ 如实记缺口（不假装 no-op）。
    """
    from engine.natives.damage import deal_damage, excess_split          # noqa: PLC0415
    ptr = args[0] if args else None
    amount = args[1] if len(args) > 1 else 0
    hq_side = ctx.hq_side_of(ptr)
    if hq_side is not None:
        # 总部牌：`IsLocatedOnBoard` 为真（Board_HQLeft/Right ∈ 在场位置，`cardnatives._is_on_board`）⇒ 走 `ApplyDamageToCard`
        # （`BP_CardFunctions.cpp:16375+`）的普通路径：`setAndEncryptDefense(总防 - 伤害)`（`:16443` 前后，`Label_985`）；
        # 总部没有护甲/溢出（`excess` 只对 `toCard.IsUnit`）；摧毁判定（总防 <= 0 ⇒ 对局结束）是调用方/终局链的事。
        # 与旧路 `damage_hq` → `s.hq[s.opp] -= n` 等价。
        dmg_hq = max(int(amount or 0), 0)
        ctx.state.hq[hq_side] = ctx.state.hq.get(hq_side, 0) - dmg_hq
        ctx.applied.append(("damage_hq", hq_side, dmg_hq))
        write_out(frame, e, 6, ctx.state.hq[hq_side] <= 0)
        return None
    known = ctx.cid_of(ptr) is not None
    u = ctx.unit_of(ptr)
    if u is None:
        if not known:
            ctx.gaps.append("DamageCard：目标指针换不出 card_id（未结算）")
        return None                                                     # 已知但不在场 ⇒ 原版就是什么都不做
    # `damagerCardID` 是**卡 ID**（签名就叫 ID，不是指针）⇒ 直接按 id 查；查不到 ⇒ 没有 excess 来源
    dealer = getattr(ctx.state, "units", {}).get(args[2]) if len(args) > 2 else None
    dmg = excess_split(ctx.state, dealer, u, amount)
    # `deal_damage` 会**自己 pop** 再回调 ⇒ 默认回调用 `died`（只记事件，不重复 pop；见 `died` 的注释）
    on_death = lambda st, uid: ctx.died(uid, why="DamageCard 伤害致死")
    deal_damage(ctx.state, u, dmg, engage=False, on_death=on_death)
    ctx.applied.append(("damage_card", u.id, int(amount or 0)))
    # 出参 `targetDestroyed`：原版 `_isDestroyed = (getTotalDefense() <= 0)`（`:16468-16472`）
    write_out(frame, e, 6, u.id not in ctx.state.units)
    return None


#: 伤害族已迁动词（第六刀）。
DAMAGE_SINKS: Dict[str, Callable[..., Any]] = {
    "DamageCard": _sink_damage_card,
}



# ---------------------------------------------------------------------------
# 抽牌/牌库族 sinks（第七刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_draw_top(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DrawTopCardFromDeck(deckSide, instigatorID, opponentDraw, cardSeen, …)`（`:12958`，第七刀）。

    ★ 这个动词**效果字典里一个字都没有**（全仓 grep：`effectvm` 里 0 次）⇒ 旧路上它是靠
      **VM 解释这个动词的函数体**、指望函数体内部的别的原语（如 `SpawnCardInHandBySide`）被钩子接住
      才可能被记成 `draw`/`gain_cards`。直跑把这一步**一步接住**：不再依赖"解释函数体 + 内部原语恰好有钩子"。
      （★ 撤回一句写过头的：我先前写"以前是静默丢的"——**不成立**：VM 会解释函数体，效果可能仍被内层
        钩子记到；能确定的只是"字典里没有这个动词"。）

    链路：`deckSide` 是我方 ⇒ 调用方注入的抽牌链（通常是 `engine.chain.draw_chain`，它就在
      `engine` 层 ✓，不需要认识 `sim`）；对方抽牌 ⇒ 我们只有"已知牌数"这一档（与 `_res_eff`
      对 `not mine` 的既有口径一致）⇒ **记缺口、不猜**。
    ★ 未建模：抽到的那张**卡对象出参**（`:19741` 的 scry 路径会用它）—— 我们的状态没有"牌库卡对象的
      句柄"，如实记一条缺口（不假装写了）。
    """
    sd = args[0] if args else None
    side = ctx.seat_of(sd) if sd is not None else None
    if side is None or side != ctx.my_side:
        ctx.gaps.append("DrawTopCardFromDeck：非我方抽牌（side=%r，只有已知牌数这一档）" % (sd,))
        return None
    if ctx.on_draw is None:
        ctx.gaps.append("DrawTopCardFromDeck：调用方没注入抽牌链（`on_draw`）—— 不猜、不结算")
        return None
    ctx.applied.append(("draw", 1))
    ctx.on_draw(ctx.state, 1)              # 调用方按 `engine.chain.draw_chain` 跑
    ctx.fire("draw", None)                 # 抽牌事件（`event_fx["draw"]` 的扇出交调用方）
    ctx.gaps.append("DrawTopCardFromDeck：抽到的卡对象出参未建模（scry 路径会用它）")
    return None


#: 抽牌/牌库族已迁动词（第七刀）。
DRAW_SINKS: Dict[str, Callable[..., Any]] = {
    "DrawTopCardFromDeck": _sink_draw_top,
}



# ---------------------------------------------------------------------------
# 撤退族 sinks（第八刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_make_card_retreat(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MakeCardRetreat(cards[], instigatorID)`（`:1709-1754`，逐行读过）。

    原版：逐张 `HasCustomAbility("cantRetreat")` 为真 ⇒ **跳过**（`:1727-1732`）；
    其余先按支援线/前线分两批（`:1734`/`:1742`/`:1754`）调 `ApplyMakeCardRetreat`。
    直跑：跳过带 `cantRetreat` 的（能力集合就是 `U.ab` = 原版 `receivedAbilities` ✓）→
    **先扇 `("retreat", uid)` 事件（那时还在场）→ 再移场**（顺序与 `_apply_eff` 的既有口径一致 ✓）。
    ★ 撤退**不是**摧毁 ⇒ 走 `ctx.retreat`（只发撤退事件），**不发** 0x27 死亡链。
    """
    arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
    if not arr:
        ctx.gaps.append("MakeCardRetreat：实参不是卡数组（未结算）")
        return None
    for ptr in arr:
        u = ctx.unit_of(ptr)
        if u is None:
            ctx.gaps.append("MakeCardRetreat：数组里的卡指针换不出场上的单位（未结算）")
            continue
        if "cantRetreat" in (getattr(u, "ab", ()) or ()):       # `HasCustomAbility("cantRetreat")`
            ctx.applied.append(("retreat_skipped", u.id))
            continue
        ctx.retreat(u.id, why="MakeCardRetreat")
    return None


#: 撤退族已迁动词（第八刀）。
RETREAT_SINKS: Dict[str, Callable[..., Any]] = {
    "MakeCardRetreat": _sink_make_card_retreat,
}



# ---------------------------------------------------------------------------
# 关键词授予/移除族 sinks（第九刀，2026-10-04）
# ---------------------------------------------------------------------------
#: 授予动词 → 关键词（与 `engine.effectvm.GIVE_KW` 同一张表；直接 import 会绕一圈，
#: 这里在模块加载时从它取，**单一来源**）。
def _kw_tables():
    from engine.effectvm import GIVE_KW                                # noqa: PLC0415
    remove_kw = {"Remove" + k[4:]: kw for k, kw in GIVE_KW.items()}    # GiveX → RemoveX
    return dict(GIVE_KW), remove_kw


def _give_sink(kw: str):
    """生成 `Give<关键词>` 的 sink（状态：`kw` 集合 + `blitz`/`fury` 的两条附带规则）。

    规则出处（与 `sim/engine.py:603-613` **同一口径**，直跑必须与它一致才能对账）：
      * `k in t.kw` 原先**没有** ⇒ 广播"能力已变"（原版 0x1D：只在原先没有时才广播 ✓）；
      * `blitz` ⇒ `sick = False`（:609）；`fury` ⇒ `attacks_left = max(现有, 未行动则 2 否则 1)`（:611）。
    ★ `u.ab`（原版 `receivedAbilities` 的原始映射）这里不写：那是**适配器**读出来的原始能力表，
      规则侧的统一口径是 `kw`（`engine`/`sim` 都读它）—— 写 `ab` 会造出第二份真相。
    """
    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        # ★ 目标实参是**卡 ID**（BP 签名 `GiveGuard/GiveFury/GiveBlitz(int cardID, …)`、`GiveShock(int cardID, int instigatorID, …)`，
        #   BP_CardFunctions.cpp:1886/2001/2124/9839）⇒ `unit_arg`（id 优先、再认指针）；原先只认指针，PARACHUTE ASSAULT 里
        #   `GiveShock(spawnedCardIDs[i])` 传的是刚生成那张的 id ⇒ 永远换不出单位。
        if args and args[0] == 0 and not isinstance(args[0], bool):
            # `cardID == 0`（例：`SpawnCardOnBattlefield` 没生成出来 ⇒ 出参 `spawnedCardID` 保持 0）：原版 `GetCardFromID(0)`
            # 无效 ⇒ 只打日志（"Give… must have a valid target"）、**不改任何状态** ⇒ 合法 no-op，不是缺口。
            return None
        u = ctx.unit_arg(args[0] if args else None)
        if u is None:
            ctx.gaps.append("Give%s：目标指针换不出场上的单位（未结算）" % kw.capitalize())
            return None
        had = kw in (getattr(u, "kw", ()) or ())
        u.kw = set(getattr(u, "kw", ()) or ()) | {kw}
        if kw == "blitz":
            u.sick = False
        if kw == "fury":
            u.attacks_left = max(int(getattr(u, "attacks_left", 0) or 0),
                                 2 if not getattr(u, "acted", False) else 1)
        ctx.applied.append(("give_kw", u.id, kw))
        if not had:                                  # 原版：只在原先没有时才广播 0x1D
            ctx.fire("abilities_changed", u.id)
        return None
    return _sink


def _remove_sink(kw: str):
    """生成 `Remove<关键词>` 的 sink（状态：从 `kw` 里删 + "能力已变"事件）。

    与 `sim/engine.py:696-705` 同口径：**原先有**才广播"能力已变"；
    ★ 原版例外（`:705` 的注释 + `_abilities_changed`）：`immune`/`alpine`/`salvage` **不广播** 0x1D。
    """
    NO_BROADCAST = ("immune", "alpine", "salvage")

    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        u = ctx.unit_arg(args[0] if args else None)      # `RemoveX(int cardID, …)` 同样收 ID
        if u is None:
            ctx.gaps.append("Remove%s：目标指针换不出场上的单位（未结算）" % kw.capitalize())
            return None
        had = kw in (getattr(u, "kw", ()) or ())
        u.kw = set(getattr(u, "kw", ()) or ()) - {kw}
        ctx.applied.append(("remove_kw", u.id, kw))
        if had and kw not in NO_BROADCAST:
            ctx.fire("abilities_changed", u.id)
        return None
    return _sink


_GIVE_KW, _REMOVE_KW = _kw_tables()

#: 关键词授予族（第九刀）：10 个动词各一个 sink（动词名不进 sink 签名 ⇒ 用工厂绑死关键词）。
GIVE_SINKS: Dict[str, Callable[..., Any]] = {v: _give_sink(k) for v, k in _GIVE_KW.items()}

#: 关键词移除族（第九刀）：10 个动词。
REMOVE_SINKS: Dict[str, Callable[..., Any]] = {v: _remove_sink(k) for v, k in _REMOVE_KW.items()}



# ---------------------------------------------------------------------------
# 手牌族 sinks（第十刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_change_kredit_cost(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangeKreditCost(card, instigatorID, amount, changeType, isBuff, skipAction, skipAddToBattlelog)`
    （`BP_CardFunctions.cpp:10067-10146`，第十刀）。

    目标 = **手牌那张牌**（走 `ctx.hand_card_of`，不是 `unit_of` —— 这正是这个键很久没有消费者的原因：
    旧模型只认"场上 target"，手牌不是 target，效果就被整个丢掉，`effectvm:679-684` 记着这段）。
    门：只有 `skipCovertCheck`（签名第 5 参，index 4）为**假**时才查 `IsUnrevealedCovertCard` ✓；
    值语义走 `natives.stats.change_kredit_cost`（集合家族只有 2/3；5 与 6-9 只发通知不改数值 ✓）。
    """
    from engine.natives.stats import change_kredit_cost                # noqa: PLC0415
    ptr = args[0] if args else None
    amount = args[2] if len(args) > 2 else 0
    ct = args[3] if len(args) > 3 else None
    cid = ctx.cid_of(ptr)
    card = ctx.hand_card_of(ptr)
    if card is None:
        _sp = ctx.spawned.info_of_handle(ptr)
        if _sp is not None and _sp.get("where") == "opp_hand":
            ctx.gaps.append("ChangeKreditCost：目标是刚进对方手牌的新牌（对方手牌只有张数、没有逐张状态）⇒ 未结算")
            return None
        ctx.gaps.append("ChangeKreditCost：%s（未结算）"
                        % ("目标指针认不出 card_id" if cid is None else "手里/牌库里没有这张牌"))
        return None
    if len(args) > 4 and not args[4]:                    # `skipCovertCheck == false` 才过门
        covert = _gate(ctx, ptr, "ChangeKreditCost", "unrevealed_covert")
        if covert is None:
            return None
        if covert:
            return None                                  # 未揭示的隐蔽牌 ⇒ 静默不改（原版）
    r = change_kredit_cost(card, amount, ct)
    ctx.applied.append(("kredit_cost", cid, amount, ct))
    if r.get("notify"):
        ctx.fire("kredit_cost_changed", cid)             # `Label_1614`：`NotifySetKreditCost` + 0x2D
    return None


#: 手牌族已迁动词（第十刀）。
HAND_SINKS: Dict[str, Callable[..., Any]] = {
    "ChangeKreditCost": _sink_change_kredit_cost,
}



# ---------------------------------------------------------------------------
# AOE 族 sinks（第十一刀，2026-10-04）
# ---------------------------------------------------------------------------
def _aoe_units(ctx: DirectCtx, arr, verb: str) -> list:
    """数组（卡指针）→ `[unit]`；认不出的**逐条记缺口**（不猜、不跳过了事）。"""
    out = []
    for ptr in (arr if isinstance(arr, (list, tuple)) else []):
        u = ctx.unit_arg(ptr)
        if u is None:
            ctx.gaps.append("%s：数组里的卡指针换不出场上的单位（未结算）" % verb)
            continue
        out.append(u)
    return out


def _sink_destroy_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DestroyMultipleCards(cards[], …)`（→ 旧路 `destroy_aoe` + `destroy_aoe_ids`）。

    逐张走 `ctx.destroy`（移场 + `("destroy", uid)` 事件 ⇒ 死亡链由调用方扇出，第四刀定的边界）。
    """
    for u in _aoe_units(ctx, args[0] if args else None, "DestroyMultipleCards"):
        ctx.destroy(u.id, why="DestroyMultipleCards")
    return None


def _sink_remove_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RemoveMultipleCardsFromBoard(cards[], …)`（→ 旧路 `remove_aoe`）：**离场但不算被摧毁**。

    `sim` 的既有口径："离场但不算被摧毁 ⇒ 不触发 `death_fx`" ⇒ 走 `ctx.leave_board`（只 pop、不发事件）。
    多发一条 `("destroy", uid)` 会让调用方跑一整套死亡效果（行为就变了）。
    """
    for u in _aoe_units(ctx, args[0] if args else None, "RemoveMultipleCardsFromBoard"):
        ctx.leave_board(u.id, why="RemoveMultipleCardsFromBoard")
    return None


def _sink_damage_multiple(ctx: DirectCtx, vm, frame, obj, args, e):
    """`DamageMultipleCards(receiverIDs[], amount, instigatorID, …)`（→ 旧路 `damage_aoe`）。

    实参位次照录制表 `("damage_aoe", 1, 0, None)`：**数组在 0、数值在 1** ✓。
    ★ 旧路会把"打**敌方总部**"的那个 card_id 拆成 `damage_hq`（`effectvm:728`）⇒ 直跑同样认得：
      先查 `ctx.hq_card_ids`（调用方喂 `{card_id: 座位}`），命中就打总部；否则当单位。
    """
    from engine.natives.damage import deal_damage                        # noqa: PLC0415
    arr = args[0] if args else None
    amt = args[1] if len(args) > 1 else 0
    on_death = lambda st, uid: ctx.died(uid, why="DamageMultipleCards")
    for ptr in (arr if isinstance(arr, (list, tuple)) else []):
        cid = ctx.cid_of(ptr)
        side = ctx.hq_side_of(ptr)                       # 数组元素是 card id（旧口径）或指针，两向都认
        if side is not None:                                             # 打总部（原版的 damage_hq 拆分）
            ctx.state.hq[side] = ctx.state.hq.get(side, 0) - float(amt or 0)
            ctx.applied.append(("damage_hq", side, amt))
            continue
        u = ctx.unit_arg(ptr)
        if u is None:
            ctx.gaps.append("DamageMultipleCards：卡指针既不是场上单位、也不在 hq_card_ids（未结算）")
            continue
        deal_damage(ctx.state, u, amt, engage=False, on_death=on_death)
        ctx.applied.append(("damage_aoe", u.id, amt))
    return None


#: AOE 族已迁动词（第十一刀）。
AOE_SINKS: Dict[str, Callable[..., Any]] = {
    "DestroyMultipleCards": _sink_destroy_multiple,
    "RemoveMultipleCardsFromBoard": _sink_remove_multiple,
    "DamageMultipleCards": _sink_damage_multiple,
}



# ---------------------------------------------------------------------------
# 自定义能力族 sinks（第十二刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_custom_ability_add(ctx: DirectCtx, vm, frame, obj, args, e):
    """`CustomAbilityAdd(ability, cardID, giverID, skipBuffTexts, skipSubAction…)`（`:5758`）。

    ★ 实参位次：**`ability` 在 0、`cardID` 在 1**（`cardID` 是 **ID 不是指针** ⇒ 走 `unit_by_id`，
      与 `DamageCard` 的 `damagerCardID` 同一个坑）。门 = `CanCardBeBuffed`（`:5769-5772`）。
    ★ 不写出参、不发"能力已变"事件：`CustomAbility*` **不在** `ExecuteOnOtherCardsAbilitiesChanged`
      的调用点里（`state.py:50-53` 那张已核对的清单只有 Give/Remove 关键词）⇒ 发了反而是编的 ✓。
    """
    from engine.natives.abilities import grant_ability                   # noqa: PLC0415
    name = args[0] if args else None
    cid = args[1] if len(args) > 1 else None
    giver = args[2] if len(args) > 2 and isinstance(args[2], int) else None   # `giverID`（授予者卡 ID；ECHELON 一族靠它认"谁给的"）
    u = ctx.unit_by_id(cid)
    if u is None or name is None:
        ctx.gaps.append("CustomAbilityAdd：cardID=%r 不在场上或 ability 为空（未结算）" % (cid,))
        return None
    ok = _gate(ctx, cid, "CustomAbilityAdd", "buffable")
    if ok is None:
        return None
    if not ok:
        return None                                          # 原版 `:5771-5772` 假 ⇒ 直接不改
    grant_ability(ctx.state, u.id, name, giver)              # 计数 +1 并记 giver 账（`state.grants`）
    ctx.applied.append(("custom_ability_add", u.id, str(name), giver))
    return None


def _sink_custom_ability_remove(ctx: DirectCtx, vm, frame, obj, args, e):
    """`CustomAbilityRemove(ability, cardID, giverID, RemoveAllGivers, &qqq)`（`:5874`）。

    ★ **giver 级账没建模**：原版删的是"这个 giver 那一份"（`receivedAbilitiesFromCards`），
      我们只有计数 ⇒ 用 `RemoveAllGivers`/计数 −1 近似，**如实记缺口** ✓。
    """
    from engine.natives.abilities import custom_ability_remove           # noqa: PLC0415
    name = args[0] if args else None
    cid = args[1] if len(args) > 1 else None
    remove_all = bool(args[3]) if len(args) > 3 else False
    u = ctx.unit_by_id(cid)
    if u is None or name is None:
        write_out(frame, e, 4, False)                        # 卡无效 ⇒ 原版 `:5882` 的假路径
        ctx.gaps.append("CustomAbilityRemove：cardID=%r 不在场上或 ability 为空（未结算）" % (cid,))
        return None
    r = custom_ability_remove(u, name, remove_all=remove_all)
    ctx.applied.append(("custom_ability_remove", u.id, str(name), remove_all, r.get("gone")))
    ctx.gaps.append("CustomAbilityRemove：giver 级账（`receivedAbilitiesFromCards`）未建模，"
                    "只按计数增减（`RemoveAllGivers`=%s）" % remove_all)
    return None


#: 自定义能力族已迁动词（第十二刀）。
ABILITY_SINKS: Dict[str, Callable[..., Any]] = {
    "CustomAbilityAdd": _sink_custom_ability_add,
    "CustomAbilityRemove": _sink_custom_ability_remove,
}


# ---------------------------------------------------------------------------
# 指向税（第十三刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_add_kredits_tax(ctx: DirectCtx, vm, frame, obj, args, e):
    """`AddKreditsTax(card, costToAdd, instigatorID, &qqq)`（`:7301-7329`，第十三刀）。

    原版：`card->KreditsTax_AsEnemyTarget = max(现值 + costToAdd, **0**)`（`:7308-7312`）；
    `qqq` **两条路径都写 false**（`:7322` 正常路径 / `:7329` 卡无效路径）⇒ 照做，不猜 True。
    与 `sim` 同口径：`t.tax = max(0, t.tax + n)`（`sim/engine.py:720-722`）。
    """
    ptr = args[0] if args else None
    amount = args[1] if len(args) > 1 else 0
    u = ctx.unit_of(ptr)
    if u is None:
        write_out(frame, e, 3, False)                        # 卡无效 ⇒ 原版 `:7329`
        ctx.gaps.append("AddKreditsTax：目标指针换不出场上的单位（未结算）")
        return None
    u.tax = max(0, int(getattr(u, "tax", 0) or 0) + int(amount or 0))
    ctx.applied.append(("kredits_tax", u.id, int(amount or 0)))
    write_out(frame, e, 3, False)                            # 原版两条路都写 false ✓
    return None


#: 指向税已迁动词（第十三刀）。
TAX_SINKS: Dict[str, Callable[..., Any]] = {
    "AddKreditsTax": _sink_add_kredits_tax,
}


# ---------------------------------------------------------------------------
# 揭示族 + 回手（第十四刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_reveal_card(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RevealCard(cardID, instigatorID, int& qqq)`（`:9191-9235`，逐行读过）。

    原版：`cardRevealed->isRevealed = true`（`:9197`）、`hasCovert = false`（`:9199`）、
    `UpdateGuarded(card->location)`（`:9201`），随后 trigger **0x37** 逐张 `OnOtherCardRevealed`（`:9214-9232`）。
    直跑：`cardID` 是**卡 ID 不是指针**（走 `unit_by_id`）；"隐蔽"在我们的模型里是 `kw` 里的 `covert`
    ⇒ 摘掉它 ✓；`UpdateGuarded` 的**重算没建模**（我们的 `guarded` 是适配器读的静态值）⇒ 如实记缺口；
    0x37 走 `("reveal", uid)` 事件交调用方扇出 ✓。
    ★ 出参 `qqq` 是 `int&`（`:9191`）、在函数体可见窗口里**没有被写** ⇒ **不猜、不写出参**。
    """
    cid = args[0] if args else None
    u = ctx.unit_by_id(cid)
    if u is None:
        ctx.gaps.append("RevealCard：cardID=%r 不在场上（未结算）" % (cid,))
        return None
    had = "covert" in (getattr(u, "kw", ()) or ())
    u.kw = set(getattr(u, "kw", ()) or ()) - {"covert"}
    ctx.applied.append(("reveal", u.id, had))
    ctx.gaps.append("RevealCard：`UpdateGuarded` 的守卫重算未建模（`guarded` 仍是适配器读的静态值）")
    ctx.fire("reveal", u.id)                                 # 0x37（扇出交调用方）
    return None


def _sink_move_unit_to_owners_hand(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MoveUnitFromBoardToOwnersHand(card, instigatorID)`（`:1093+`）。

    门 = `card->IsUnit() && card->IsLocatedOnBoard()`（`:1095-1102`，**两者都真**才走）；
    然后 `GetHandLocationBySide(card->originalSide)` 把它移回**原主**的手牌。
    直跑：状态效果 = 离场（走 `ctx.retreat`：移场 + 撤退事件 ✓）；★ **"手牌里多一张"我们没建模**
    （`Sim.hand` 只是"已知手牌"的模板表）⇒ 如实记缺口，不假装塞了一张进去。
    门不通过 = 原版的合法 no-op（**不是缺口**）；认不出指针才是缺口（同第五刀的分寸）。
    """
    ptr = args[0] if args else None
    u = ctx.unit_of(ptr)
    if u is None:
        if ctx.cid_of(ptr) is None:
            ctx.gaps.append("MoveUnitFromBoardToOwnersHand：目标指针换不出 card_id（未结算）")
        return None                                          # 已知但不在场 ⇒ 原版门不通过、什么都不做
    ctx.retreat(u.id, why="MoveUnitFromBoardToOwnersHand")
    ctx.gaps.append("MoveUnitFromBoardToOwnersHand：『进原主手牌』未建模（只做了离场/撤退登记）")
    return None


#: 揭示族已迁动词（第十四刀）。
REVEAL_SINKS: Dict[str, Callable[..., Any]] = {
    "RevealCard": _sink_reveal_card,
}

# 第十四刀：单卡回手**也是撤退语义** ⇒ 挂进第八刀那张表（定义之后才注册，别在上面写死以免 NameError）。
RETREAT_SINKS["MoveUnitFromBoardToOwnersHand"] = _sink_move_unit_to_owners_hand


# ---------------------------------------------------------------------------
# `setAndEncrypt*` 叶写入族 sinks（第十五刀，2026-10-04）
# ---------------------------------------------------------------------------
def _setenc_sink(field: str):
    """生成 `setAndEncryptX` 的 sink：**原生叶写入** ⇒ 直接把值写进那个字段（**没有影子**）。

    录制侧这批动词是 `RECORD_ONLY`（`effectvm:217-218`）：先写**影子**、事后由 `to_effects`
    换算成增量/绝对值写法；直跑没有影子（P4 要的正是这个 ✓）⇒ 直接写字段。
    目标可能是**场上单位**，也可能是**手牌/牌库里的牌**（`kredit`/`kreditBuff` 就是手牌的）⇒ 两条解析都试。
    """
    def _sink(ctx: DirectCtx, vm, frame, obj, args, e):
        from engine.natives.stats import set_encrypted_field           # noqa: PLC0415
        ptr = args[0] if args else None
        value = args[1] if len(args) > 1 else None
        card = ctx.unit_of(ptr) or ctx.hand_card_of(ptr)
        if card is None:
            ctx.gaps.append("setAndEncrypt(%s)：卡指针换不出场上单位/手牌（未结算）" % field)
            return None
        ctx.applied.append(("set_enc", getattr(card, "id", None), field, value))
        set_encrypted_field(card, field, value)
        return None
    return _sink


def _setenc_tables():
    from engine.effectvm import SET_ENC_FIELDS                        # noqa: PLC0415
    return dict(SET_ENC_FIELDS)

#: 叶写入族已迁动词（第十五刀）：5 个（攻/攻 buff/防/费/费 buff），字段表与 `effectvm` **同一份**。
SETENC_SINKS: Dict[str, Callable[..., Any]] = {v: _setenc_sink(f) for v, f in _setenc_tables().items()}



# ---------------------------------------------------------------------------
# 战斗/治疗/离场/前线/重置/定住回合 族 sinks（第十六刀，2026-10-04）
# ---------------------------------------------------------------------------
def _sink_make_cards_fight(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MakeCardsFight(unitThisSide, unitOppositeSide, instigatorID)`（`BP_CardFunctions:9755`）。

    规则本身在 `engine.natives.damage.apply_fight`（P2/P3 已端口 ✓）⇒ 这里只做**指针 → card_id** 的解析
    与回调注入（`deal_damage` 用 `natives.damage.deal_damage`，死亡链走 `ctx.on_death`/`ctx.died` ✓）。
    """
    from engine.natives.damage import apply_fight, deal_damage               # noqa: PLC0415
    a_id = ctx.cid_of(args[0] if args else None)
    b_id = ctx.cid_of(args[1] if len(args) > 1 else None)
    if a_id is None or b_id is None or a_id in (None,) or b_id in (None,):
        ctx.gaps.append("MakeCardsFight：两个卡指针换不出 card_id（未结算）")
        return None
    on_death = lambda st, uid: ctx.died(uid, why="MakeCardsFight")

    def _dd(state, unit, dmg):
        deal_damage(state, unit, dmg, engage=False, on_death=on_death)

    run = apply_fight(ctx.state, a_id, b_id, deal_damage=_dd, kill=on_death)
    ctx.applied.append(("fight", a_id, b_id))
    ctx.gaps += list(run or [])
    return None


def _sink_fully_heal(ctx: DirectCtx, vm, frame, obj, args, e):
    """`FullyHealCard(card, instigatorID)`（`BP_CardFunctions:701`，与 `sim:618` 同口径）。

    门槛 = `当前总防御 > 0 ∧ maxDefense − 防御 > 0`；通过后 `defense = maxDefense`。
    ★ 原版 `OnBeforeFullyRepaired`(**0xC**) 可否决 ⇒ 预计算的否决结果由调用方放 `ctx.gates[uid]["heal_vetoed"]`；
      没提供就按"不否决"处理并**如实记缺口**（不假装跑过那个钩子）。
    `0x2C`/`OnFullyRepaired` 的后续钩子同样未建模 ⇒ 记缺口。
    """
    u = ctx.unit_of(args[0] if args else None)
    if u is None:
        ctx.gaps.append("FullyHealCard：目标指针换不出场上的单位（未结算）")
        return None
    mdef = float(getattr(u, "mdef", 0) or 0)
    if not (float(u.dfn) > 0 and mdef - float(u.dfn) > 0):
        return None                                          # 门槛不过 ⇒ 什么都不做（合法 no-op）
    g = ctx.gates.get(u.id) or ctx.gates.get(args[0] if args else None) or {}
    if g.get("heal_vetoed") is True:
        return None                                          # 0xC 否决
    if "heal_vetoed" not in g:
        ctx.gaps.append("FullyHealCard：`OnBeforeFullyRepaired`(0xC) 的否决结果没提供 ⇒ 按不否决处理")
    u.dfn = mdef
    ctx.applied.append(("heal_unit", u.id, mdef))
    ctx.gaps.append("FullyHealCard：0x2C / `OnFullyRepaired` 的后续钩子未建模")
    return None


def _sink_remove_card_from_board(ctx: DirectCtx, vm, frame, obj, args, e):
    """`RemoveCardFromBoard(card, …)`（录制键 `remove_unit`）：**离场但不算被摧毁**。

    `sim:737` 同口径：只 `units.pop`、不触发 `OnDestroyed` ⇒ 走 `ctx.leave_board`（只 pop、零事件）。
    """
    ptr = args[0] if args else None
    u = ctx.unit_arg(ptr)
    if u is None:
        if ctx.cid_of(ptr) is None:
            ctx.gaps.append("RemoveCardFromBoard：目标指针换不出 card_id（未结算）")
        return None
    ctx.leave_board(u.id, why="RemoveCardFromBoard")
    return None


def _sink_move_to_frontline(ctx: DirectCtx, vm, frame, obj, args, e):
    """`MoveUnitFromSupportToFrontLine(card, instigatorID)`（录制键 `move_front`，`sim:855` 同口径）。

    效果导致的移动：**不花行动费、不算"移动过"**；只在 `row == "back"` 且**前线不属于对方**时生效。
    """
    u = ctx.unit_of(args[0] if args else None)
    if u is None:
        ctx.gaps.append("MoveUnitFromSupportToFrontLine：目标指针换不出场上的单位（未结算）")
        return None
    if u.row == "back" and getattr(ctx.state, "front_owner", None) != getattr(ctx.state, "opp", None):
        u.row = "frontline"
        ctx.applied.append(("move_front", u.id))
    return None


def _sink_reset_unit_operations(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ResetUnitOperations(card, …)`（录制键 `reset_ops`，`sim:725-727` 同口径）：

    `attacks_left = max(现有, fury 则 2 否则 1)`；`acted = moved = False`（可以再动/再打一次）。
    """
    u = ctx.unit_arg(args[0] if args else None)
    if u is None:
        ctx.gaps.append("ResetUnitOperations：目标指针换不出场上的单位（未结算）")
        return None
    u.attacks_left = max(int(getattr(u, "attacks_left", 0) or 0),
                         2 if "fury" in (getattr(u, "kw", ()) or ()) else 1)
    u.acted, u.moved = False, False
    ctx.applied.append(("reset_ops", u.id))
    return None


def _sink_changed_pinned_turns(ctx: DirectCtx, vm, frame, obj, args, e):
    """`ChangedPinnedTurns(card, instigatorID, turns)`（录制键 `pin_turns`，`sim:706-714` 同口径）：
    `turns > 0` ⇒ 定住；`turns < 0` ⇒ 解除。`0` ⇒ 什么都不做（`if e.get("pin_turns")` 为假）。"""
    u = ctx.unit_arg(args[0] if args else None)
    if u is None:
        ctx.gaps.append("ChangedPinnedTurns：目标指针换不出场上的单位（未结算）")
        return None
    n = int(args[2]) if len(args) > 2 else 0
    if n > 0:
        u.pinned = True
    elif n < 0:
        u.pinned = False
    ctx.applied.append(("pin_turns", u.id, n))
    if n > 0:
        ctx.fire("pin", u.id)                                # 与 `PinUnit` 同一条 0x3D
    return None


#: 第十六刀已迁动词（战斗/治疗/离场/前线/重置/定住回合）。
BOARD_SINKS: Dict[str, Callable[..., Any]] = {
    "MakeCardsFight": _sink_make_cards_fight,
    "FullyHealCard": _sink_fully_heal,
    "RemoveCardFromBoard": _sink_remove_card_from_board,
    "MoveUnitFromSupportToFrontLine": _sink_move_to_frontline,
    "ResetUnitOperations": _sink_reset_unit_operations,
    "ChangedPinnedTurns": _sink_changed_pinned_turns,
}
