#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.scripts —— **卡牌蓝图字节码在 engine 状态上直跑**（P4 第一刀，2026-10-04）。

目标架构（`docs/REFACTOR-PLAN.md:25-41` 原文）：
    卡牌脚本（蓝图字节码）在 `engine.scripts` 里跑，它调用的原生函数
    （`ChangeAttack(card, amount, changeType)` …）**直接是 engine 里的移植实现**，作用在 `GameState` 副本上。
    不再有 `buff`/`buff_ids`/`retreat_ids` 这类中间词汇；也就没有"词汇没人消费 / 消费错了"这类 bug。

**缝**（不新造机制）：`kardsmem.vm.VM(session, get_field=…, card_natives=…, hooks={FuncName: fn(vm, frame, obj, args, e)})`，
`vm.py:530` 分发。录制模式填的是 `effectvm.Recorder.hooks()`（`effectvm.py:462`：把调用记进 `rec.records`，
之后 `to_effects` 出字典、`sim._apply_eff` 解释）；**直跑模式填这里**：每个动词直接调 `engine.natives.*`、
**当场改状态**，没有字典。

**迁移策略（本刀起）**：逐族把动词从"录制"切到"直跑"，两条路**可以并存** ——
`native_hooks(...)` 只覆盖已迁的动词，其余（`fallback`）仍交给 `rec.hooks()` 的录制路径。
每迁一族就要有一次**对账**（P4 退出条件之一）：同一输入下"直跑后的状态"与
"录制→`to_effects`→`_apply_eff` 后的状态"逐卡比对一致（本模块 `reconcile_one` + 测试）。

第一刀迁的是**资源族**（最好对账、原生实现早就有）：
  * `GiveKreditsBySide(side, kredits, instigatorID, &qqq)`（`BP_CardFunctions.cpp:12394`：`ChangeKreditsBySide` + `qqq=false`）
  * `GainKreditSlot(cardGivingKreditSlot, side)`（`:2324`：`ChangeKreditSlotsBySide(side, +1, cardID)`）
  * `LoseKreditSlot(side)`（`:2387`：`ChangeKreditSlotsBySide(side, -1, …)`）
  * `setKreditSlotBySide(side, newValue)`：**绝对值**语义 —— 只有"我方"能直跑（状态里有绝对值 `slots`）；
    对方侧我们只记增量（`opp_slots` 是增量累加器）⇒ 该情形**如实记缺口并回退录制路径**，不猜。

未迁的（其余全部动词）：**仍走录制路径** —— 本刀不删任何东西，`effectvm`/`to_effects` 原样保留，
`test_effect_keys_consumed` 也先留着（P4 收尾时才退役）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

# P6：DirectCtx / sink 表已拆到 scripts_ctx / scripts_sinks_core / scripts_sinks_more（纯搬移），这里再导出，
# 保持 `engine.scripts.<名>` 路径不变（tests / 影子 / 热重载都从这里取）。
from engine.scripts_ctx import DirectCtx, write_out                 # noqa: F401
from engine.scripts_sinks_core import (                    # noqa: F401
    _GIVE_KW,
    _REMOVE_KW,
    _kredits,
    _slots,
    _sink_give_kredits,
    _sink_gain_slot,
    _sink_lose_slot,
    _sink_change_slots_by_side,
    _sink_set_slot,
    RESOURCE_SINKS,
    _sink_pin,
    _sink_unpin,
    _suppress_one,
    _sink_suppress_unit,
    _sink_suppress_multi,
    STATUS_SINKS,
    _gate,
    _sink_change_attack,
    _sink_change_opcost,
    _sink_change_heavy,
    STATS_SINKS,
    _sink_change_defense,
    _sink_destroy_card,
    LIFECYCLE_SINKS,
    _sink_damage_card,
    DAMAGE_SINKS,
    _sink_draw_top,
    DRAW_SINKS,
    _sink_make_card_retreat,
    RETREAT_SINKS,
    _kw_tables,
    _give_sink,
    _remove_sink,
    GIVE_SINKS,
    REMOVE_SINKS,
    _sink_change_kredit_cost,
    HAND_SINKS,
    _aoe_units,
    _sink_destroy_multiple,
    _sink_remove_multiple,
    _sink_damage_multiple,
    AOE_SINKS,
    _sink_custom_ability_add,
    _sink_custom_ability_remove,
    ABILITY_SINKS,
    _sink_add_kredits_tax,
    TAX_SINKS,
    _sink_reveal_card,
    _sink_move_unit_to_owners_hand,
    REVEAL_SINKS,
    _setenc_sink,
    _setenc_tables,
    SETENC_SINKS,
    _sink_make_cards_fight,
    _sink_fully_heal,
    _sink_remove_card_from_board,
    _sink_move_to_frontline,
    _sink_reset_unit_operations,
    _sink_changed_pinned_turns,
    BOARD_SINKS,
)
from engine.scripts_sinks_more import (                    # noqa: F401
    _sink_take_control,
    _sink_discard_from_hand,
    CONTROL_SINKS,
    _sink_defense_multiple,
    _deck_top_one,
    _sink_move_to_deck_top,
    _sink_move_multi_to_deck_top,
    _sink_spawn_in_hand,
    _sink_draw_cards_by_side,
    _sink_draw_specific,
    DECK_SINKS,
    _sink_add_attack_until_eot,
    _sink_force_end_turn,
    _side_sink_sd,
    _sink_salvage_multiple,
    TURN_SINKS,
    _sink_steal_to_deck,
    _sink_end_match,
    _sink_set_countdown,
    _sink_discard_random,
    MISC_SINKS,
    _sink_make_veteran,
    VETERAN_SINKS,
    _spawn_sink,
    SPAWN_SINKS,
    _sink_convert,
    CONVERT_SINKS,
    _sink_add_restriction,
    _sink_remove_restriction,
    _sink_seen_by_cipher,
    RESTRICTION_SINKS,
    _UNMODELED_REASON,
    _unmodeled_sink,
    UNMODELED_SINKS,
    _pending_sink,
    PENDING_SINKS,
    _sink_set_kredit_by_side,
    _sink_shuffle_deck,
    _sink_spawn_in_deck,
    _sink_remove_bond,
    BOND_SINKS,
    FINAL_SINKS,
)

__all__ = ["DirectCtx", "write_out", "RESOURCE_SINKS", "native_hooks", "reconcile_one"]


#: 直跑**默认**覆盖的动词（… + 第二十七刀）。
DEFAULT_SINKS = dict(RESOURCE_SINKS, **STATUS_SINKS, **STATS_SINKS, **LIFECYCLE_SINKS,
                     **DAMAGE_SINKS, **DRAW_SINKS, **RETREAT_SINKS,
                     **GIVE_SINKS, **REMOVE_SINKS, **HAND_SINKS, **AOE_SINKS, **ABILITY_SINKS,
                     **TAX_SINKS, **REVEAL_SINKS, **SETENC_SINKS, **BOARD_SINKS, **CONTROL_SINKS,
                     **DECK_SINKS, **TURN_SINKS, **MISC_SINKS, **VETERAN_SINKS, **SPAWN_SINKS,
                     **CONVERT_SINKS, **RESTRICTION_SINKS, **UNMODELED_SINKS, **PENDING_SINKS,
                     **FINAL_SINKS, **BOND_SINKS)


# ---------------------------------------------------------------------------
# 缝：hooks 表 + 对账
# ---------------------------------------------------------------------------
def native_hooks(state, *, ptr_ids: Optional[dict] = None, my_side=None,
                 fallback: Optional[dict] = None, sinks: Optional[dict] = None,
                 gates: Optional[dict] = None, on_death=None, on_draw=None,
                 on_event=None, hq_card_ids: Optional[dict] = None, spawn_stat=None,
                 payload: Optional[dict] = None, rng=None,
                 hq_ptr_sides: Optional[dict] = None) -> dict:
    """建**直跑**的 `vm.hooks` 表：已迁动词 → 直接改状态；其余交给 `fallback`（录制路径）。

    `fallback` 传 `rec.hooks()`（`effectvm.py:462`）即可让未迁动词保持旧行为 —— 这就是
    "两条路并存、逐族切换"的落地方式；**不传 fallback 时**未迁动词就是无操作（只适合单元测试）。
    """
    ctx = DirectCtx(state, ptr_ids=ptr_ids, my_side=my_side, gates=gates, on_death=on_death,
                    on_draw=on_draw, on_event=on_event, hq_card_ids=hq_card_ids,
                    spawn_stat=spawn_stat, payload=payload, rng=rng, hq_ptr_sides=hq_ptr_sides)
    table = dict(DEFAULT_SINKS if sinks is None else sinks)
    out: dict = {}

    def _wrap(fn):
        def _h(vm, frame, obj, args, e):
            return fn(ctx, vm, frame, obj, args, e)
        return _h

    for verb, fn in table.items():
        out[verb] = _wrap(fn)
    if fallback:
        for verb, fn in fallback.items():
            out.setdefault(verb, fn)                         # 已迁的优先，未迁的回退
    out["__ctx__"] = ctx                                     # 给调用方取 `ctx.applied`/`ctx.gaps`
    return out


def _state_snapshot(state) -> dict:
    """把"直跑/旧路都可能改到"的状态抽成一份可逐字段比对的快照（对账用）。

    覆盖：指挥点/槽（双方）+ 每个单位的 `pinned`/关键词集合/重甲/剩余攻击次数/指向税 —
    这些都是已迁两族会动的字段；将来迁更多族就往这里加字段（**对账要看得见差异**，所以是快照不是布尔）。
    """
    snap = {f: getattr(state, f, None) for f in ("kredits", "slots", "opp_kredits", "opp_slots")}
    snap["playing_side"] = getattr(state, "playing_side", None)
    snap["opp_cards"] = getattr(state, "opp_cards", None)
    snap["hq"] = tuple(sorted((getattr(state, "hq", {}) or {}).items(), key=lambda kv: str(kv[0])))
    _deck = getattr(state, "deck", None)
    snap["deck"] = tuple(_deck) if isinstance(_deck, (list, tuple)) else _deck   # 牌库顺序也是状态（偷进牌库/放回顶会动它）
    _restr = getattr(state, "restrictions", None)
    snap["restrictions"] = tuple(sorted((tuple(sorted((k, str(v)) for k, v in (r or {}).items()))
                                         for r in (_restr or ()))))               # 游戏限制（影子列表）
    # giver 级账（`CustomAbilityAdd` 的授予者；ECHELON 一族）：三条路必须记同一份
    snap["grants"] = tuple(sorted(((str(k), tuple(sorted(v, key=str))) for k, v in (getattr(state, "grants", None) or {}).items())))
    for uid, u in sorted((getattr(state, "units", {}) or {}).items(), key=lambda kv: str(kv[0])):
        snap["ab%s" % uid] = tuple(sorted((getattr(u, "ab", None) or {}).items(), key=lambda kv: str(kv[0])))
        snap["u%s" % uid] = (getattr(u, "pinned", None), tuple(sorted(getattr(u, "kw", ()) or ())),
                             getattr(u, "atk", None), getattr(u, "dfn", None),
                             getattr(u, "opc", None), getattr(u, "armor", None),
                             getattr(u, "attacks_left", None), getattr(u, "tax", None),
                             getattr(u, "side", None), getattr(u, "row", None),
                             getattr(u, "sick", None), getattr(u, "acted", None),
                             getattr(u, "moved", None), getattr(u, "atk_turn", None))
    for cid, h in sorted((getattr(state, "hand", {}) or {}).items(), key=lambda kv: str(kv[0])):
        snap["h%s" % cid] = (getattr(h, "cost", None), getattr(h, "cost_buff", None), getattr(h, "opc", None))
    return snap


def reconcile_one(verb: str, args: list, *, make_state, to_effects, apply_eff,
                  ptr_ids=None, my_side=None, target=None, gates=None, record_extra=None,
                  on_draw=None, spawn_stat=None, payload=None):
    """**对账**（P4 退出条件的雏形）：同一个动词、同一组实参，

    * A 路（旧）：`to_effects(录制记录)` → `apply_eff(state_A, eff, target)`
    * B 路（新）：直跑 sink 改 `state_B`（并记事件）

    返回 `(state_a, state_b, diff)`；`diff` 为空 = 两条路一致。调用方自己给 `make_state`
    （造一份同构的初始状态）与 `to_effects`/`apply_eff` —— **对账必须能看见"哪两个量不一样"**，
    所以 diff 是结构化字段而不是布尔。

    ★ 为什么这两个函数是**入参**而不是在这里 import：`engine` 层**不许认识 `sim`**
      （`docs/STRUCTURE.md` 的分层表 + `tests/test_arch_rules.py`；本模块第一版写死了
      `from sim.engine import _apply_eff`，被架构测试当场抓到 ⇒ 改成注入）。
      实际调用方是测试（`tests/test_scripts_direct.py`）与将来的 `policy/`。
    """
    state_a, state_b = make_state(), make_state()
    # A 路：造一条录制的记录 → 字典 → 解释
    from engine.effectvm import Recorder                          # noqa: PLC0415
    rec = Recorder(0, 0, None)
    rec.cur_stats = {}
    rec.ptr_ids = dict(ptr_ids or {})          # id 型动词（`*_aoe_ids`）要它才出得出 card_id
    # 旧路的门也是从 `cur_stats` 读的（`_fill_cur_stats` 在真路径上填）⇒ 对账时按同一份门数据喂
    # （否则 A 路会被门挡掉、B 路放过，diff 出来的是"门数据缺失"而不是两条路的差异）。
    for _ptr, _g in (gates or {}).items():
        rec.cur_stats[_ptr] = dict(_g)
    rec.records.append({"verb": verb, "args": list(args), "tainted": False,
                        **dict(record_extra or {})})       # 有的载荷挂在**记录**上（如 `MakeVeteran` 的 `vet`）
    eff = to_effects(rec, my_side=my_side)
    if eff:
        apply_eff(state_a, dict(eff, target=target), target)
    # B 路：直跑
    table = {verb: DEFAULT_SINKS[verb]} if verb in DEFAULT_SINKS else {}
    hooks = native_hooks(state_b, ptr_ids=ptr_ids, my_side=my_side, sinks=table, gates=gates,
                         on_draw=on_draw, spawn_stat=spawn_stat, payload=payload)
    hooks[verb](None, None, None, list(args), None)
    snap_a, snap_b = _state_snapshot(state_a), _state_snapshot(state_b)
    diff = {k: (snap_a.get(k), snap_b.get(k)) for k in set(snap_a) | set(snap_b)
            if snap_a.get(k) != snap_b.get(k)}
    return state_a, state_b, diff


def reconcile_run(calls, *, make_state, to_effects, apply_eff, ptr_ids=None, my_side=None,
                  gates=None, payload=None, on_draw=None, spawn_stat=None, rng=None,
                  record_extra=None):
    """**动作级对账**（P5 正题的安全网）：把**一串**动词调用分别走两条路，比对终态。

    与 `reconcile_one` 的区别 —— 也就是它必须单独存在的原因 —— 是**顺序**：

    * A 路（旧）逐条 `to_effects(该条记录)` → `apply_eff(state_A, eff, target)`；
      ★ 每条都要用**当时的** `slots`/`kredits` 去折算增量（`to_effects(rec, my_side, slots, kredits)`），
        否则第 2 条起的增量会按**初始值**算 ⇒ diff 出来的是"折算口径错"而不是两条路的差异 ✗
        ⇒ 本函数每步从 `state_a` **现读** ✓。
    * B 路（新）逐条直跑 sink 改 `state_B`（同一份 `native_hooks` 绑定，逐条调用 ✓）。

    `calls` = `[(verb, args, target_or_None), …]`；`record_extra` 可以是 `{verb: {...}}`（同一条载荷给同名词）
    或与 `calls` 等长的 list ✓（载荷挂在**记录**上的动词，如 `MakeVeteran` 的 `vet`、`ConvertCard` 的 `conv`）。

    返回 `(state_a, state_b, diff, per_step)`：`diff` 是**终态**差异；`per_step[i]` 是第 i 条调用**之后**
    的差异 ⇒ 能定位"**从第几步开始分叉**" ✓（只跑一条时两者等价 ✓）。

    ★ 与 `reconcile_one` 同一条分层纪律：`to_effects`/`apply_eff`/`make_state` 都是**入参**
      （`engine` 不许认识 `sim`，见 `docs/STRUCTURE.md` 与 `tests/test_arch_rules.py`）。
    """
    state_a, state_b = make_state(), make_state()
    from engine.effectvm import Recorder                                     # noqa: PLC0415
    hooks = native_hooks(state_b, ptr_ids=ptr_ids, my_side=my_side, gates=gates,
                         on_draw=on_draw, spawn_stat=spawn_stat, payload=payload, rng=rng)
    per_step: list = []
    for i, item in enumerate(calls):
        verb, args, target = (list(item) + [None, None, None])[:3]
        extra = record_extra
        if isinstance(record_extra, (list, tuple)):
            extra = record_extra[i] if i < len(record_extra) else None
        elif isinstance(record_extra, dict) and verb in record_extra:
            extra = record_extra[verb]
        # ---- A 路（该步：造记录 → 出字典 → 解释；增量按**当前**资源折算）----
        rec = Recorder(0, 0, None)
        rec.cur_stats = {}
        rec.ptr_ids = dict(ptr_ids or {})
        for _ptr, _g in (gates or {}).items():
            rec.cur_stats[_ptr] = dict(_g)
        rec.records.append({"verb": verb, "args": list(args), "tainted": False,
                            **dict(extra or {})})
        _slots = {getattr(state_a, "me", None): getattr(state_a, "slots", None),
                  getattr(state_a, "opp", None): getattr(state_a, "opp_slots", None)}
        _kr = {getattr(state_a, "me", None): getattr(state_a, "kredits", None),
               getattr(state_a, "opp", None): getattr(state_a, "opp_kredits", None)}
        eff = to_effects(rec, my_side=my_side, slots=_slots, kredits=_kr)
        if eff:
            apply_eff(state_a, dict(eff, target=target), target)
        # ---- B 路（该步：直跑）----
        if verb in hooks and callable(hooks.get(verb)):
            hooks[verb](None, None, None, list(args), None)
        else:
            hooks["__ctx__"].gaps.append("reconcile_run：%s 不在直跑表里（该步只有 A 路）" % verb)
        snap_a, snap_b = _state_snapshot(state_a), _state_snapshot(state_b)
        per_step.append({k: (snap_a.get(k), snap_b.get(k)) for k in set(snap_a) | set(snap_b)
                         if snap_a.get(k) != snap_b.get(k)})
    return state_a, state_b, per_step[-1] if per_step else {}, per_step
