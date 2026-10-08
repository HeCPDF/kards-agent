"""`engine.triggers` 的"各钩子族"跑法与效果摘要（P6 拆分，2026-10-06：原样从 `engine/triggers.py` 搬出）。

死亡链 / 移前线 / 抽牌 / 目标事件 / 回合族 / 揭示 / 老兵 / 治疗 / 夺取 / 转化 / 能力变化 / 战斗伤害 /
伤害一张牌 / 离开前线 / 撤退。底层 `_Chain`、`find_cards`、`_run_hook_ex` 等仍在 `engine/triggers.py`——
**测试会给那些模块属性打补丁**（`TR._run_hook_ex = fake`），所以本文件的函数只经 `_Chain`/`_mk_chain`
间接用它们、不自己持有。

加载方式：`engine.triggers` 末尾 `from . import triggers_families` 触发本模块执行；本模块末尾把
`FAMILY_NAMES` 写回 `engine.triggers` 的命名空间（热重载单独重载本模块时同样会刷新 `engine.triggers` 上的名字）。
"""
from __future__ import annotations

import sys as _sys
from typing import Optional

from .triggers import (  # noqa: E402  —— 核心层（_Chain / find_cards / 钩子规格表 …）仍在 triggers / triggers_specs
    LEAVE_METHOD_CONVERT,
    MOSQUITO_SKIP,
    MOVE_REASON_NAME,
    PIN_HOOK_SPECS,
    SUPPRESS_HOOK_SPECS,
    TURN_HOOK_SPECS,
    TriggerCache,
    _Chain,
    _atk,
    _kw_default,
    _loc_num,
    _merge_eff,
    _ptr_of,
    merge_hits,
    ordered_gotchas,
)

# --------------------------------------------------------------------------- 死亡链
def run_death_chain(km, st, victim, killer=None, stream=None, my_side=None, read_hooks=None,
                    budget_s: float = 0.8, cache: Optional[TriggerCache] = None, include_own: bool = True,
                    kw_of=None, hq_own=(), hq_enemy=(), in_combat: bool = True,
                    enum_random: bool = True) -> dict:
    """**一张牌死亡时**的完整摧毁序列（`ExecuteAttackCard` 的摧毁段 / `ApplyRemoveCardFromBoard` 的同一段）：
    BeforeDestroyed(自己) + 0x0F 旁观者 → BeforeLeave(自己) + 0x2E → `CardLocationMoved`(自己) + 0x2F →
    AfterLeave(自己) + 0x08 → `ExecuteOnCardDestroyedFunction`：自己的 `OnDestroyed`（+0x18 倍增重放）→
    0x27 `OnOtherCardDestroyed` 旁观者（排除自己）→ 打捞 → 自己的 `OnAfterDestroyed`。
    `killer` 传**真值**（击杀者的卡对象/指针；不知道才传 None/0）。返回 `{"hits","order","draws"}`。"""
    d0 = getattr(stream, "draws", 0) if stream is not None else 0
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of or _kw_default,
                hq_own, hq_enemy, enum_random=enum_random, in_combat=in_combat)
    ch.bucket = "death"
    loc = _loc_num(ch.card(victim))
    ch.before_destroyed(victim, killer)
    ch.leave_board(victim)
    ch.location_moved(victim, loc)
    ch.after_leave(victim, loc)
    ch.destroyed_fn(victim, killer, {_ptr_of(victim)})
    return {"hits": ch.hits, "order": ch.order,
            "draws": (getattr(stream, "draws", 0) - d0) if stream is not None else 0}


def death_effects(km, st, victim, killer=None, **kw) -> dict:
    """`run_death_chain` → 一份 `Sim.death_fx[victim_card_id]` 能吃的效果摘要（`boardeval._apply_unit_eff` 语义：
    逐牌效果放进 `unit_eff:[(card_id, eff)]`，其余是全局效果）。没有任何命中 ⇒ `{}`。"""
    res = run_death_chain(km, st, victim, killer, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 上线链（0x32）
def _unrevealed_covert(c) -> bool:
    """`IsUnrevealedCovertCard` = `hasCovert` ∧ `!isRevealed`（NATIVE-COVERAGE 板查询族）。

    优先读 `raw`（`kardsmem.cards` 直接读的 `has_covert`/`is_revealed` 字节，权威）；
    只有 `keywords` 时退回关键词判断 —— 用户 2026-10-02 说过 `keywords` 里的 covert 不可靠，
    所以那只是兜底（真值以 raw 为准）。
    """
    raw = getattr(c, "raw", None) or {}
    fl = raw.get("keyword_flags") or {}
    has = bool(fl["has_covert"]) if "has_covert" in fl else \
        ("covert" in (getattr(c, "keywords", None) or []))
    rev = raw.get("is_revealed")
    if rev is None:
        rev = getattr(c, "is_revealed", False)
    return bool(has) and not bool(rev)


def run_move_frontline(km, st, moved, force_move: bool = False, move_cost: int = 0, stream=None,
                       my_side=None, read_hooks=None, budget_s: float = 0.8,
                       cache: Optional[TriggerCache] = None, include_own: bool = True,
                       kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """**一张牌上线时**的钩子序列（0x32）：反制先手 → 自己 `OnMoveToFrontline` → 0x32 列表。

    逐行对着本构建的 `BP_CardFunctions::ExecuteOnMoveToFrontlineCardEffects`（导出 @15297）实现，
    顺序/去重/隐蔽跳过都按字节码；`GetStopFurtherActions` 的**状态**读不到（`effectvm` 不拦截它）
    ⇒ 不做"猜出来的中止"，只把这件事写进返回的 `meta`（不编造）。
    返回 `{"hits", "order", "meta"}`；`hits[*].bucket` 全是 `"move"`。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "move"
    mp = _ptr_of(moved)
    args = {"cardMoved": mp, "forceMove": bool(force_move), "moveCost": int(move_cost)}
    cards = list(getattr(st, "cards", None) or [])
    done = set()
    # ① 反制先手（顺序 = ordered_gotchas；`cardsDone` 记下它们，后面 0x32 不再问）
    for c in ordered_gotchas(cards):
        ch.call("OnOtherCardMoveToFrontline", c, args)
        done.add(_ptr_of(c))
    # ② 自己的钩子（未压制才跑 —— BP 里 `if (!cardMoved->isSuppressed)`）
    if not ch.suppressed(moved):
        ch.call("OnMoveToFrontline", moved, {"forceMove": bool(force_move),
                                             "moveCost": int(move_cost)})
    done.add(mp)
    # ③ 0x32 列表：隐蔽未揭示 ⇒ 整段跳过（字节码 goto 1887）；`cardsDone` 去重
    if not _unrevealed_covert(ch.card(moved)):
        for c, _fn in ch.others("OnOtherCardMoveToFrontline", exclude=()):
            if _ptr_of(c) in done:
                continue
            ch.call("OnOtherCardMoveToFrontline", c, args)
            done.add(_ptr_of(c))
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"stop_semantics": "GetStopFurtherActions 未建模（读不到谁置位）",
                     "covert_skip": _unrevealed_covert(ch.card(moved))}}


def move_effects(km, st, moved, **kw) -> dict:
    """`run_move_frontline` → 一份 `Sim.move_fx[moved_card_id]` 能吃的效果摘要
    （`boardeval._apply_unit_eff` 语义：逐牌效果进 `unit_eff`，其余是全局效果）。没有命中 ⇒ `{}`。"""
    res = run_move_frontline(km, st, moved, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 抽到链（0x2A 旁观者段）
def run_drawn_from_deck(km, st, drawn_card_id: int, start_of_turn_draw: bool = False,
                        drawn_side: Optional[int] = None, stream=None, my_side=None,
                        read_hooks=None, budget_s: float = 0.8,
                        cache: Optional[TriggerCache] = None, include_own: bool = True,
                        kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """**一张牌被抽到**时的 0x2A 旁观者段（`ExecuteOnDrawnFromDeck`@13783）。

    只跑旁观者：抽到那张**自己**的 `OnCardDrawnFromDeck` 由评估侧的 `_on_draw` 摘要承担
    （那条是既有实现，本轮不动）；这里补的是"**别人看到有人抽牌**"的反应。
    列表里 `cardID == drawn_card_id` 的牌**跳过**（字节码 @13819-13824）。
    形参按名字传（`drawnCardID` / `StartOfTurnDraw` / `drawnSide`），名字写错 = VM 静默丢参。
    返回 `{"hits", "order"}`（`hits[*].bucket == "drawn"`）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "drawn"
    args = {"drawnCardID": int(drawn_card_id),
            "StartOfTurnDraw": bool(start_of_turn_draw),
            "drawnSide": int(drawn_side) if drawn_side is not None else None}
    for c, _fn in ch.others("OnOtherCardDrawnFromDeck", exclude=()):
        if int(getattr(c, "card_id", 0) or 0) == int(drawn_card_id):
            continue                              # 抽到的那张自己：字节码里显式跳过
        ch.call("OnOtherCardDrawnFromDeck", c, args)
    return {"hits": ch.hits, "order": ch.order}


def drawn_effects(km, st, drawn_card_id: int, **kw) -> dict:
    """`run_drawn_from_deck` → 一份 `Sim.draw_fx[drawn_card_id]` 能吃的效果摘要
    （逐牌效果进 `unit_eff`，其余是全局效果）。没有命中 ⇒ `{}`。"""
    res = run_drawn_from_deck(km, st, drawn_card_id, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def run_target_event(km, st, hook: str, card, stream=None, my_side=None, read_hooks=None,
                     budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                     include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                     enum_random: bool = True) -> dict:
    """**"以某张牌为对象"的单钩子族**（0x3A 被压制 / 0x3D 被定住）：按触发表序逐张问。

    两族的字节码形状相同（fetch 列表 → 逐张 `hook(card)`），差别只在触发号/钩子名/形参名，
    所以合成一个 runner（形参名由 `hook` 在 `SUPPRESS_HOOK_SPECS`/`PIN_HOOK_SPECS` 里查）。
    `hits[*].bucket == "target"`。返回 `{"hits", "order"}`。
    """
    from_specs = {**SUPPRESS_HOOK_SPECS, **PIN_HOOK_SPECS}
    sp = from_specs.get(hook)
    if not sp:
        raise KeyError("run_target_event 只认登记的族：%s" % sorted(from_specs))
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "target"
    ptr = _ptr_of(card)
    args = {sp["params"][0]: ptr}
    for c, _fn in ch.others(hook, exclude=()):
        ch.call(hook, c, args)
    return {"hits": ch.hits, "order": ch.order}


def _target_effects(km, st, hook: str, card, **kw) -> dict:
    res = run_target_event(km, st, hook, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def suppress_effects(km, st, card, **kw) -> dict:
    """0x3A `OnOtherCardSuppressed` → `Sim.event_fx["suppress"][card_id]` 能吃的效果摘要。"""
    return _target_effects(km, st, "OnOtherCardSuppressed", card, **kw)


def pin_effects(km, st, card, **kw) -> dict:
    """0x3D `OnOtherUnitPinned` → `Sim.event_fx["pin"][card_id]` 能吃的效果摘要。"""
    return _target_effects(km, st, "OnOtherUnitPinned", card, **kw)


# --------------------------------------------------------------------------- 回合族（0x14/0x40/0x19）
def has_custom_name1_attr(c, attr: str) -> bool:
    """`CustomName1HasAttribute(attr)`：`custom_name1` 按 `;` 分段、**整段匹配**（大小写不敏感）。

    口径见 `NATIVE-COVERAGE-1.60.md` §15（"CustomName1/2HasAttribute 按 ; 分段整段匹配"）。
    读的是 `kardsmem.cards.read_raw` 放进 `raw["custom_name1"]` 的 FName；读不到 ⇒ False。
    """
    raw = getattr(c, "raw", None) or {}
    cn = raw.get("custom_name1")
    return any(seg.strip().lower() == attr.lower() for seg in str(cn or "").split(";") if seg.strip())


def run_turn_family(km, st, hook: str, turn_number=None, stream=None, my_side=None,
                    read_hooks=None, budget_s: float = 1.0,
                    cache: Optional[TriggerCache] = None, include_own: bool = True,
                    kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True,
                    only_card=None) -> dict:
    """回合族（0x14 / 0x40 / 0x19）的钩子清单，顺序**照 BP 导出**（见 `TURN_HOOK_SPECS` 的 ev）。

    * 0x14：单趟、无参；
    * 0x40：三趟（`startofturn0` → `startofturn1` → 其余）；
    * 0x19：即时 → `endofturn1` → `endofturn2`；`card_unit_mosquito_*` 四张跳过。
    ★ 递归（这一趟里新登记进来的牌再跑一遍）在**静态快照**上是空转 ⇒ 不实现、在 `meta` 里写明；
      `RemoveBuffsEndOfTurn` 也不在这里（收尾规则属于 Sim，不属于钩子清单）。
    `only_card`：只问这一张（给"逐卡预计算"用，`rule._turn_end_fx` 就靠它把每张牌自己的
      0x19 效果存进 `Sim.event_fx["turn_end"]`）。
    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "turn"`）。
    """
    sp = TURN_HOOK_SPECS.get(hook)
    if not sp:
        raise KeyError("run_turn_family 只认 %s，收到 %r" % (sorted(TURN_HOOK_SPECS), hook))
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "turn"
    args = {sp["params"][0]: int(turn_number)} if sp["params"] else {}
    cards = [c for c, _fn in ch.others(hook, exclude=())]
    if only_card is not None:
        cards = [c for c in cards if _ptr_of(c) == _ptr_of(only_card)]
    if hook == "OnStartOfTurn":
        passes = (("startofturn0", True), ("startofturn1", True), (None, False))
        for attr, want in passes:
            for c in cards:
                if (has_custom_name1_attr(c, attr) if attr else
                        not (has_custom_name1_attr(c, "startofturn0")
                             or has_custom_name1_attr(c, "startofturn1"))):
                    ch.call(hook, c, args)
    elif hook == "OnEndOfTurn":
        skipped = []
        for c in cards:
            nm = str(getattr(c, "name", "") or "").lower()
            if nm in MOSQUITO_SKIP:
                skipped.append(nm)                      # BP：这四张"新登记"的跳过
                continue
            if not (has_custom_name1_attr(c, "endofturn1")
                    or has_custom_name1_attr(c, "endofturn2")):
                ch.call(hook, c, args)                  # ① 即时
        for attr in ("endofturn1", "endofturn2"):       # ② ③ 延迟的两趟
            for c in cards:
                if str(getattr(c, "name", "") or "").lower() in MOSQUITO_SKIP:
                    continue
                if has_custom_name1_attr(c, attr):
                    ch.call(hook, c, args)
    else:                                                # 0x14：单趟
        for c in cards:
            ch.call(hook, c, args)
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"hook": hook, "cards": len(cards),
                     "recursion": "递归（这一趟新登记的牌再跑）在静态快照下空转",
                     "remove_buffs": "RemoveBuffsEndOfTurn 不在钩子清单里（属 Sim 收尾）"}}


def _turn_effects(km, st, hook: str, **kw) -> dict:
    res = run_turn_family(km, st, hook, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def before_start_of_turn_effects(km, st, **kw) -> dict:
    """0x14 `OnBeforeStartOfTurn` 的效果摘要（回合开始前）。"""
    return _turn_effects(km, st, "OnBeforeStartOfTurn", **kw)


def start_of_turn_effects(km, st, **kw) -> dict:
    """0x40 `OnStartOfTurn` 的效果摘要（回合开始；三趟顺序）。"""
    return _turn_effects(km, st, "OnStartOfTurn", **kw)


def end_of_turn_effects(km, st, **kw) -> dict:
    """0x19 `OnEndOfTurn` 的效果摘要（回合结束；即时→1→2）。"""
    return _turn_effects(km, st, "OnEndOfTurn", **kw)


# --------------------------------------------------------------------------- 揭示族（0x37）
def run_reveal(km, st, revealed, stream=None, my_side=None, read_hooks=None,
               budget_s: float = 1.0, cache: Optional[TriggerCache] = None,
               include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
               enum_random: bool = True, with_enter_play: bool = True) -> dict:
    """`RevealCard`（BP @9191）的钩子段，顺序照字节码（见 `REVEAL_HOOK_SPECS` 的 ev）。

    自己的 `OnCardRevealed()`（**未被压制**才跑）→ 0x37 `OnOtherCardRevealed(cardRevealed)` →
    （`with_enter_play=True` 时）`OnEnterPlay(4)`（自己）+ 0x2B `OnOtherCardEnterPlay(cardRevealed, 4)`。
    ★ 0x2B 那段**不排除自己**（字节码里 fetch 后直接逐个调；和 `ExecuteOnEnterPlayEvents` 里
      "skip the card itself" 不同）—— 这里按字节码走，并把这点写进 `meta`。
    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "reveal"`）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "reveal"
    ptr = _ptr_of(revealed)
    own_ran = False
    if not ch.suppressed(revealed):                       # BP：!isSuppressed 才跑自己那条
        ch.call("OnCardRevealed", revealed, {})
        own_ran = True
    n37 = 0
    for c, _fn in ch.others("OnOtherCardRevealed", exclude=()):
        ch.call("OnOtherCardRevealed", c, {"cardBeingRevealed": ptr})
        n37 += 1
    n2b = 0
    if with_enter_play:
        ch.call("OnEnterPlay", revealed, {"Method": 4})   # 自己的 OnEnterPlay(4)
        for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=()):
            ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": ptr, "Method": 4})
            n2b += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own_ran, "n_0x37": n37, "n_0x2b": n2b,
                     "note": "0x2B 段按字节码不排除自己"}}


def reveal_effects(km, st, revealed, **kw) -> dict:
    """0x37 揭示族 → `Sim.event_fx["reveal"][card_id]` 能吃的效果摘要。"""
    res = run_reveal(km, st, revealed, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 老兵 / 治疗 / 易主（2026-10-03，照 BP 逐行读过）
def _fam_effects(res, st, kw) -> dict:
    """`run_*` 的 `hits` → `Sim.event_fx[kind][card_id]` 能吃的效果摘要（与 reveal_effects 同一收口）。"""
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def _mk_chain(km, st, bucket, stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
              hq_own, hq_enemy, enum_random):
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = bucket
    return ch


def run_veteran(km, st, card, stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                cache: Optional[TriggerCache] = None, include_own: bool = True, kw_of=None,
                hq_own=(), hq_enemy=(), enum_random: bool = True, view_overrides=None) -> dict:
    """`MakeVeteran`（BP_CardFunctions@7127）尾段的钩子：未压制才跑自己的 `OnBecomingVeteran()`；
    fetch **0x20** 逐张 `OnOtherCardBecomingVeteran(card)`（不排除自己）；最后
    `ExecuteOnOtherCardsAbilitiesChanged(card)`（:7280，fetch 0x1D，A5-③ 已建模）。
    `view_overrides`（可选）：升级后的关键词视图（`{牌指针: {has_<kw>: 值}}`，由 `_vet` 静态卡给出）——原版是**先改旗标再跑
    全部钩子**，所以整条链（自己的 OnBecomingVeteran、0x20、0x1D）都应当读到升级后的值；不给 ⇒ 读快照里升级前的值
    （`meta.abilities_view="pre_change"`）。"""
    ch = _mk_chain(km, st, "veteran", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ch.view_overrides = dict(view_overrides or {})
    own = False
    if not ch.suppressed(card):
        ch.call("OnBecomingVeteran", card, {})
        own = True
    n = 0
    for c, _fn in ch.others("OnOtherCardBecomingVeteran", exclude=()):
        ch.call("OnOtherCardBecomingVeteran", c, {"cardBecomingVeteran": _ptr_of(card)})
        n += 1
    n1d = ch.abilities_changed(card)                                  # 原版：MakeVeteran 末尾（BP_CardFunctions.cpp:7280）
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own, "n_0x20": n, "n_0x1d": n1d,
                     "abilities_view": "post_change" if view_overrides else "pre_change"}}


def veteran_effects(km, st, card, **kw) -> dict:
    return _fam_effects(run_veteran(km, st, card, **kw), st, kw)


def run_heal(km, st, card, to_heal: int, stream=None, my_side=None, read_hooks=None,
             budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
             kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`FullyHealCard`（BP_CardFunctions@701）：fetch **0xC** 逐张 `OnBeforeFullyRepaired(card, &stopAction)`，
    某张返回 stopAction 真 ⇒ 治疗中止（`meta.vetoed`）；否则防御设为最大防御后，fetch **0x2C** 逐张
    `OnOtherCardFullyRepaired(card, toHeal)`（`cardID != card` 才调）；最后**未压制**才 `card.OnFullyRepaired(toHeal)`。"""
    ch = _mk_chain(km, st, "heal", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ptr = _ptr_of(card)
    vetoed = False
    for c, _fn in ch.others("OnBeforeFullyRepaired", exclude=()):
        r = ch.call("OnBeforeFullyRepaired", c, {"cardRepaired": ptr, "stopAction": False})
        if (r.get("out") or {}).get("stopAction"):
            vetoed = True
            break
    n = 0
    if not vetoed:
        for c, _fn in ch.others("OnOtherCardFullyRepaired", exclude=[ptr]):
            ch.call("OnOtherCardFullyRepaired", c, {"cardRepaired": ptr, "amount": int(to_heal)})
            n += 1
        if not ch.suppressed(card):
            ch.call("OnFullyRepaired", card, {"amount": int(to_heal)})
    return {"hits": ch.hits, "order": ch.order, "meta": {"vetoed": vetoed, "n_0x2c": n}}


def heal_effects(km, st, card, to_heal: int, **kw) -> dict:
    res = run_heal(km, st, card, to_heal, **kw)
    eff = _fam_effects(res, st, kw)
    if res["meta"]["vetoed"]:
        eff["heal_vetoed"] = True          # boardeval：被否决 ⇒ 这次治疗不生效
    return eff


def run_steal(km, st, card, new_loc: int, old_loc: int, stream=None, my_side=None, read_hooks=None,
              budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
              kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`ChangeUnitOwnership`（BP_CardFunctions@18087，偷牌）的钩子段：
    ① `ExecuteOnBeforeLeaveBoardOrOwnerEvents(card, new, old, 5)`：未压制才自己的 `OnLeaveBoardOrOwner(new, 5)`，
       fetch **0x2E** 逐张（`cardID != card`）`OnOtherCardLeaveBoardOrOwner(card, new, 5)`；
    ② 换边（状态变更，sim 侧做）；
    ③ `ExecuteOnAfterLeaveBoardOrOwnerEvents(card, new, old)`：未压制才自己的 `OnAfterLeaveBoard(new)`，
       fetch **0x8** 逐张（`cardID != card`）`OnAfterOtherCardLeaveBoardOrOwner(card, old)`；
    ④ `ExecuteOnEnterPlayEvents(card, 0)`：自己的 `OnEnterPlay(0)` + 0x2B 逐张 `OnOtherCardEnterPlay(card, 0)`（排除自己）。
    ★ 钩子是在**换边前后各一次**调用，但这里是静态快照，换边前后的条件（谁是"我方"）读的是快照；
      未建模：`hasActivePincerEffect` 分支、`CardLocationMoved`。"""
    ch = _mk_chain(km, st, "steal", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ptr = _ptr_of(card)
    if not ch.suppressed(card):
        ch.call("OnLeaveBoardOrOwner", card, {"goingToLocation": int(new_loc), "leavePlayMethod": 5})
    for c, _fn in ch.others("OnOtherCardLeaveBoardOrOwner", exclude=[ptr]):
        ch.call("OnOtherCardLeaveBoardOrOwner", c, {"cardLeaving": ptr, "goingToLocation": int(new_loc),
                                                    "method": 5})
    if not ch.suppressed(card):
        ch.call("OnAfterLeaveBoard", card, {"goingToLocation": int(new_loc)})
    for c, _fn in ch.others("OnAfterOtherCardLeaveBoardOrOwner", exclude=[ptr]):
        ch.call("OnAfterOtherCardLeaveBoardOrOwner", c, {"cardLeaving": ptr, "oldLocation": int(old_loc)})
    ch.call("OnEnterPlay", card, {"Method": 0})
    for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=[ptr]):
        ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": ptr, "Method": 0})
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"unmodeled": "hasActivePincerEffect 分支 / CardLocationMoved"}}


def steal_effects(km, st, card, new_loc: int = 5, old_loc: int = 6, **kw) -> dict:
    return _fam_effects(run_steal(km, st, card, new_loc, old_loc, **kw), st, kw)


def run_convert(km, st, new_card, old_ids, new_ids, convert_to_name: str = "", instigator: int = 0,
                skip_trigger: bool = False, old_cards=None, stream=None, my_side=None, read_hooks=None,
                budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
                kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`ConvertCard(cardIDs, instigatorID, convertToCardName, convertIntoCardID, skipTrigger, &newCardIDs)`
    （BP_CardFunctions.cpp:12611，转化）的钩子段，**顺序照原文**（桶名 = `ch.bucket`）：

      第一轮，逐张旧牌（`IsLocation` 的跳过）——旧牌**在场**才有离场钩子（`ApplyRemoveCardFromBoard(id, instigator,
      destroyed=false, inCombat=false, skipAddAction=true, converting=true)`，:12737 → :18002）：        [桶 `convert_old:<旧牌 id>`]
        ① `ExecuteOnBeforeLeaveBoardOrOwnerEvents(id, 8, 旧位置, method=6)`：自己 `OnLeaveBoardOrOwner(8, 6)`（未压制）+ 0x2E 非自己；
        ② `CardLocationMoved(id, instigator, 旧位置, 8, …, reason=0xD "Convert", changeOwner=false)` → `ExecuteOnCardLocationMoved`：
           自己 `OnCardLocationMoved` + 0x2F 非自己（旧位置在 5/6/7 才发，:17678）；
        ③ `ExecuteOnAfterLeaveBoardOrOwnerEvents(id, 8, 旧位置)`：自己 `OnAfterLeaveBoard(8)` + 0x8 非自己；
        然后 `CreateCard(…)` 造新牌；牌库/手牌/场上三种落点的差别在 sim 侧（牌库：`ExecuteOnAfterDeckChanged`；手牌：
        `ExecuteOnSpawnedInHandEvents`；都：新牌 `ExecuteOnCardLocationMoved(新 id, 0, 位置, false, 0xD)`）。
      第二轮，逐张新牌：在前线先 `RemoveSmokescreen`（状态，sim 侧）；**在场**才 `ExecuteOnEnterPlayEvents(新牌, 5)`：
        自己 `OnEnterPlay(Method=5)`（未压制）+ fetch **0x2B** 逐张 `OnOtherCardEnterPlay(新牌, 5)`（排除新牌自己）。 [桶 `convert_new`]
      最后 `skipTrigger` 假时：fetch **0x22** 逐张 `OnOtherCardConverted(oldCardIDs, newCardIDs, newCardName, instigatorID)`
        （`cardID ∈ newCardIDs` 的跳过；被压制的牌被 fetch 丢掉）。                                       [桶 `convert_after`]

    `new_card`：新牌实例（单张/列表/None）。**新牌是 `CreateCard` 才造出来的，评估时通常还不存在**——没有实例就**不跑**
    新牌自己的 `OnEnterPlay(5)` / 0x2B / `OnCardLocationMoved`，记进 `meta.gaps`（不拿旧牌或 CDO 冒充：座位/位置/数值都会错）。
    `old_cards`：被转化的旧牌（Card 或指针；在场的才会发离场钩子）；不给 ⇒ 不跑旧牌离场族（`meta.old_leave=False`）。
    ★ 未证实（`meta.unverified`）：① `IsActionProcess` 门闸的极性——ConvertCard 在 `!IsActionProcess` 分支里调用
      `ExecuteOnEnterPlayEvents`，而后者内部只在 `IsActionProcess` 为真时才跑钩子（:13582）；0x22 一段没有这个内层门闸。
      其它「执行钩子」的函数（Pin/Retreat/BeforeReceiveDamage…）都是 `IsActionProcess` 真才跑，ConvertCard 与它们方向相反，
      两种可能：反编译对 JumpIfNot 的方向有误 / 动作队列里 ConvertCard 会被两个阶段各调一次。这里照「钩子会跑」算，与老兵/偷牌一致。
      ② 数组形参（旧/新 id）以 Python 列表传入，VM 读数组的行为实机未验。
    （已澄清：0x22 循环里 `call; return;` 的 `return` 是 Sequence/循环的 pop 回 `Label_3985` 自增，与 `ExecuteOnSurvivedCombatEvents`
      :13455 同构 ⇒ **逐张都调**，不是只调第一张。）"""
    ch = _mk_chain(km, st, "convert_after", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    news = [] if new_card is None else (list(new_card) if isinstance(new_card, (list, tuple)) else [new_card])
    gaps, n_old = [], 0
    if old_cards:
        for i, old in enumerate(old_cards):
            oc = ch.card(old) or old
            obj = getattr(oc, "obj", None)
            if obj is not None and obj.IsLocation():
                continue                                          # 原版：IsLocation(isIt) ⇒ 跳过（:12641）
            if obj is not None and not obj.IsLocatedOnBoard():
                continue                                          # 离场钩子只发在场的牌（OldLocation ∈ 5/6/7）
            oid = getattr(oc, "card_id", None)
            old_loc = _loc_num(oc)
            ch.bucket = "convert_old:%s" % (oid,)
            ch.leave_board(oc, True, method=LEAVE_METHOD_CONVERT)                 # 原版：ApplyRemoveCardFromBoard:18028
            ch.location_moved(oc, old_loc, reason=MOVE_REASON_NAME[0xD])         # 原版：ApplyRemoveCardFromBoard:18043
            ch.after_leave(oc, old_loc)                                          # 原版：ApplyRemoveCardFromBoard:18053
            n_old += 1
    if news:
        for nc in news:
            ch.bucket = "convert_new"
            nptr = _ptr_of(nc)
            if not ch.suppressed(nc):
                ch.call("OnEnterPlay", nc, {"Method": 5})                      # 原版：ExecuteOnEnterPlayEvents:13615
            for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=[nptr]):
                ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": nptr, "Method": 5})   # 原版：:13629
    else:
        gaps.append("convert：新牌实例不存在（CreateCard 才造）⇒ 新牌自己的 OnEnterPlay(5)、0x2B OnOtherCardEnterPlay(新牌,5)、"
                    "新牌的 OnCardLocationMoved/0x2F 未算（不拿旧牌/CDO 冒充）")
    n22 = 0
    ch.bucket = "convert_after"
    if not skip_trigger:
        new_set = {int(x) for x in (new_ids or ())}
        for c, _fn in ch.others("OnOtherCardConverted", exclude=[]):
            if getattr(c, "card_id", None) in new_set:
                continue
            ch.call("OnOtherCardConverted", c, {"oldCardIDs": [int(x) for x in (old_ids or ())],
                                                "newCardIDs": [int(x) for x in (new_ids or ())],
                                                "newCardName": str(convert_to_name),
                                                "instigatorID": int(instigator)})   # 原版：ConvertCard:12890
            n22 += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"n_0x22": n22, "n_old_leave": n_old, "old_leave": old_cards is not None,
                     "new_hooks": bool(news), "gaps": gaps,
                     "unverified": ["IsActionProcess 门闸极性（ConvertCard 与 ExecuteOnEnterPlayEvents 方向相反）",
                                    "数组形参（旧/新 id）的 VM 读法"]}}


def _bucket_effs(res: dict, st, kw: dict) -> dict:
    """`run_*` 的 hits 按**桶**分开 → `{桶名: 效果摘要(含 unit_eff)}`（`_fam_effects` 的分桶版；空桶不出）。"""
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    out = {}
    for name, b in bk.items():
        eff = dict(b["eff"])
        units = [(k, e) for k, e in b["units"].items() if e]
        if units:
            eff["unit_eff"] = units
        if eff:
            out[name] = eff
    return out


def convert_effects(km, st, new_card, old_ids, new_ids, **kw) -> dict:
    return _fam_effects(run_convert(km, st, new_card, old_ids, new_ids, **kw), st, kw)


def convert_fx(km, st, old_ids, new_ids, convert_to_name: str = "", instigator: int = 0, skip_trigger: bool = False,
               old_cards=None, new_card=None, **kw) -> dict:
    """`Sim.event_fx["convert"][(instigator, tuple(旧 id), 目标名)]` 的值：
    `{"old": {旧牌 id: 离场钩子后果}, "new": 新牌进场钩子后果, "after": 0x22 后果, "gaps": [...], "unverified": [...]}`。
    sim 消费顺序：逐张旧牌（先 `old[id]`，再换牌）→ `new`（有实例才有）→ `after`。"""
    res = run_convert(km, st, new_card, old_ids, new_ids, convert_to_name, instigator, skip_trigger,
                      old_cards=old_cards, **kw)
    be = _bucket_effs(res, st, kw)
    old = {}
    for name, eff in be.items():
        if name.startswith("convert_old:"):
            try:
                old[int(name.split(":", 1)[1])] = eff
            except ValueError:
                pass
    gaps = list(res["meta"]["gaps"])
    for h in res["hits"]:
        for g in h.get("gaps") or ():
            if g not in gaps:
                gaps.append(g)
    return {"old": old, "new": be.get("convert_new") or {}, "after": be.get("convert_after") or {},
            "gaps": gaps, "unverified": list(res["meta"]["unverified"]), "meta": dict(res["meta"])}


# --------------------------------------------------------------------------- 能力变化（0x1D）
def run_abilities_changed(km, st, card, stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                          cache: Optional[TriggerCache] = None, include_own: bool = True, kw_of=None,
                          hq_own=(), hq_enemy=(), enum_random: bool = True, view_overrides=None) -> dict:
    """`ExecuteOnOtherCardsAbilitiesChanged(CardChanging)`（BP_CardFunctions.cpp:20885）：fetch **0x1D** 逐张
    `OnOtherCardAbilitiesChanged(CardChanging)`，不排除自己、无其它条件。

    `view_overrides={被改牌指针: {视图键: 值}}`：钩子读被改牌的关键词时看到的是**改变之后**的值（调用点都是先改旗标
    再广播，见 `ABILITIES_CALLERS`）；不给 ⇒ 读快照里改变前的值（`meta.view="pre_change"`，条件判据会反）。"""
    ch = _mk_chain(km, st, "abilities", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ch.view_overrides = dict(view_overrides or {})
    n = ch.abilities_changed(card)
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"n_0x1d": n, "view": "post_change" if view_overrides else "pre_change"}}


def abilities_changed_effects(km, st, card, **kw) -> dict:
    """0x1D 能力变化族 → 效果摘要（`Sim.event_fx["abilities"][key]` 可吃）。"""
    return _fam_effects(run_abilities_changed(km, st, card, **kw), st, kw)


# 视图（`CARDS.read_raw` 的 dict）里关键词的两处来源：`keywords`（`has_<kw>` 旗标位）与 `received_abilities`（带给予者的能力表）；
# `getHas<kw>` 原生 = 旗标 OR 能力表非空（IDA，见 cardnatives._kwfn）。
def abilities_view_override(card, kw: str, now: bool) -> dict:
    """被改牌在「kw 被赋予(now=True)/被移除(now=False)」**之后**的视图覆盖（A5-③）。
    移除 ⇒ 旗标位去掉 + 能力表里摘掉 kw（按「被移除的就是唯一给予者」算；多给予者时游戏里移除一个后可能仍有，
    调用方应在 notes 里说明——sim 的 U 本来也只存布尔）。"""
    ptr = _ptr_of(card)
    if not ptr:
        return {}
    if now:
        return {ptr: {"keywords_add": ["has_" + kw]}}
    return {ptr: {"keywords_remove": ["has_" + kw, kw], "received_remove": [kw]}}


def fight_damage(km, st, a, b, stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                 cache: Optional[TriggerCache] = None, include_own: bool = True, kw_of=None,
                 hq_own=(), hq_enemy=(), enum_random: bool = True, apply_hooks: bool = True) -> dict:
    """`MakeCardsFight(unitThisSide=a, unitOppositeSide=b)`（BP_CardFunctions@9755）：

      ① 两个方向的伤害，**过改伤钩子**（Sequence：两边都先算好）：
         `ExecuteOnDealDamageAddDamage(dealer, target, atk, fromAttack=False, fromFight=True, False)`
         → `ExecuteOnDealDamageAddDamageAfterCalc(target, dealer, calc, False, False, False)`（受击方免疫 ⇒ 0）；
      ② 然后两次 `ApplyDamageToCard`（:9805/:9807）：先 `(b ← a, to_b, False, isFightDefenderDamage=False)`，
         再 `(a ← b, to_a, False, True)`；每次的钩子段见 `_Chain.apply_damage`（受伤通知 → 造成伤害通知）。

    `apply_hooks=False` ⇒ 只算 ①（旧行为）。返回 `{"to_b","to_a","hits","order","notes"}`；② 的钩子在桶
    `fight_b_recv/fight_b_dealt/fight_a_recv/fight_a_dealt`（`fight_damage_fx` 把它们整理成 sim 能吃的形式）。
    未建模：`ApplyDamageToCard` 的 excess、扣防之后的死亡链（`death_fx` 管）、`RemoveMobilize`（见 `apply_damage` 注释）。"""
    ch = _mk_chain(km, st, "fight_calc", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ca, cb = ch.card(a) or a, ch.card(b) or b
    calc_b = ch.add_damage(a, b, _atk(ca), False, True, False)
    to_b = ch.after_calc(b, a, calc_b, False, False, False)
    calc_a = ch.add_damage(b, a, _atk(cb), False, True, False)
    to_a = ch.after_calc(a, b, calc_a, False, False, False)
    if apply_hooks:
        ch.apply_damage(b, a, to_b, False, prefix="fight_b_")          # 原版：MakeCardsFight → ApplyDamageToCard:9805
        ch.apply_damage(a, b, to_a, False, prefix="fight_a_")          # 原版：MakeCardsFight → ApplyDamageToCard:9807
    return {"to_b": int(to_b), "to_a": int(to_a), "hits": ch.hits, "order": ch.order, "notes": list(ch.notes)}


def fight_damage_fx(km, st, a, b, **kw) -> dict:
    """`fight_damage` → `Sim.event_fx["damage"][("fight", a_id, b_id)]` 的值：
    `{"to_b","to_a","buckets":{桶: 效果摘要},"gaps":[...]}`（桶顺序由 sim 消费：b 受伤前 → 扣 b → b 之后 → a 受伤前 → 扣 a → a 之后）。"""
    res = fight_damage(km, st, a, b, **kw)
    return {"to_b": res["to_b"], "to_a": res["to_a"], "buckets": _bucket_effs(res, st, kw),
            "gaps": _hit_gaps(res) + list(res["notes"])}


def _hit_gaps(res: dict) -> list:
    out = []
    for h in res.get("hits") or ():
        for g in h.get("gaps") or ():
            if g not in out:
                out.append(g)
    return out


def run_damage_card(km, st, card, amount, dealer, is_redirected: bool = False, from_fight: bool = False,
                    is_fight_defender: bool = False, stream=None, my_side=None, read_hooks=None,
                    budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
                    kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`DamageCard(card, amount, damagerCardID, isRedirected, fromFight, isFightDefenderDamage, &targetDestroyed)`
    （BP_CardFunctions.cpp:895，效果伤害）的钩子段，顺序照原文：

      ① `card` 无效 / 不在场 ⇒ 什么都不发生（`targetDestroyed=false`，:911 之前）；
      ② `!isRedirected` ⇒ `ExecuteOnDealDamageAddDamage(dealer, card, amount, fromAttack=False, fromFight, False)`
         （自己的 `OnCardDealDamage_ModifyDamageDealt` → 0x25 → reRun，夹到 [0,99]）；否则直接用 `amount`；  [桶 `calc`]
      ③ `ExecuteOnDealDamageAddDamageAfterCalc(card, dealer, tmp, False, False, isRedirected)`
         （受击方免疫 ⇒ 0；自己的 `OnDealDamageAddDamageAfterCalc` → 0x26，`stopAdding` 即停）；            [桶 `calc`]
      ④ `ApplyDamageToCard(card, dealer, final, isRedirected, isFightDefenderDamage)` 的钩子段（`_Chain.apply_damage`）：
         受伤通知（final>0）→ [扣防，sim 侧] → 造成伤害通知。                                              [桶 `recv`/`dealt`]
    返回 `{"final", "hits", "order", "notes", "meta"}`；`final` 才是 sim 该扣的伤害（可能 ≠ amount）。
    ★ `dealer` 必须是快照里的牌（拿得到指针）；拿不到 ⇒ `final=None` 并记 notes，**不算**。"""
    ch = _mk_chain(km, st, "calc", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    tgt = ch.card(card) or card
    dlr = ch.card(dealer) or dealer
    obj = getattr(tgt, "obj", None)
    if not _ptr_of(dlr) or not _ptr_of(tgt):
        ch.notes.append("DamageCard：伤害来源/目标拿不到指针，伤害链未算")
        return {"final": None, "hits": ch.hits, "order": ch.order, "notes": list(ch.notes),
                "meta": {"computed": False}}
    if obj is not None and obj.IsLocatedOnBoard() is False:
        return {"final": 0, "hits": ch.hits, "order": ch.order, "notes": ["DamageCard：目标不在场 ⇒ 无事发生"],
                "meta": {"computed": True, "not_on_board": True}}
    tmp = int(amount)
    if not is_redirected:
        tmp = ch.add_damage(dlr, tgt, tmp, False, bool(from_fight), False)     # 原版：DamageCard:946
    final = ch.after_calc(tgt, dlr, tmp, False, False, bool(is_redirected))    # 原版：DamageCard:917
    ch.apply_damage(tgt, dlr, final, bool(is_redirected))                      # 原版：DamageCard:919 → ApplyDamageToCard
    return {"final": int(final), "hits": ch.hits, "order": ch.order, "notes": list(ch.notes),
            "meta": {"computed": True, "pre": tmp}}


def damage_card_fx(km, st, card, amount, dealer, **kw) -> dict:
    """`run_damage_card` → `Sim.event_fx["damage"][(dealer_id, target_id)]` 的值：
    `{"final", "buckets": {桶: 效果摘要}, "gaps": [...]}`；算不了（`final is None`）⇒ `{"final": None, "gaps": [...]}`。"""
    kw2 = {k: v for k, v in kw.items() if k in ("is_redirected", "from_fight", "is_fight_defender")}
    kw3 = {k: v for k, v in kw.items() if k not in kw2}
    res = run_damage_card(km, st, card, amount, dealer, **kw2, **kw3)
    return {"final": res["final"], "buckets": _bucket_effs(res, st, kw3) if res["final"] is not None else {},
            "gaps": _hit_gaps(res) + list(res["notes"])}


# --------------------------------------------------------------------------- 离开前线（0x31）/ 撤退（0x36）
def run_move_from_frontline(km, st, card, stream=None, my_side=None, read_hooks=None,
                            budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                            include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                            enum_random: bool = True) -> dict:
    """`ExecuteOnCardMoveFromFrontline(cardMoved)`（BP @14281）：自己（未压制）→ 0x31 列表（排除自己）。

    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "move_front"`）。
    `meta.stop_semantics` 如实标注 `GetStopFurtherActions` 未建模（§7 未查第 1 条）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "move_front"
    ptr = _ptr_of(card)
    own_ran = False
    if not ch.suppressed(card):
        ch.call("OnMoveFromFrontline", card, {})
        own_ran = True
    n = 0
    for c, _fn in ch.others("OnOtherCardMoveFromFrontline", exclude=()):
        if _ptr_of(c) == ptr:                                # 字节码：cardID == 自己 ⇒ 跳过
            continue
        ch.call("OnOtherCardMoveFromFrontline", c, {"cardMoved": ptr})
        n += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own_ran, "n_0x31": n,
                     "stop_semantics": "GetStopFurtherActions 未建模（读不到谁置位）"}}


def move_from_frontline_effects(km, st, card, **kw) -> dict:
    """0x31 离开前线族 → 效果摘要（`Sim.event_fx["move_front"][card_id]` 可吃）。"""
    res = run_move_from_frontline(km, st, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def run_retreat(km, st, card, stream=None, my_side=None, read_hooks=None,
                budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                enum_random: bool = True) -> dict:
    """`ApplyMakeCardRetreat`（BP @16567）的钩子段：自己 `OnBeforeRetreat(&stopAction)`（未压制）→
    0x36 列表逐张 `OnOtherCardRetreat(card,&stopAction)`（**不排除自己**）。

    任何一步 `stopAction=true` ⇒ **中止**（BP：自己的那条直接 return；0x36 那条打日志后 return），
    记进 `meta["stopped_by"]`（这是原版规则行为，不是缺口）。返回 `{"hits","order","meta"}`。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "retreat"
    ptr = _ptr_of(card)
    meta = {"own_ran": False, "n_0x36": 0, "stopped_by": None}
    if not ch.suppressed(card):
        r = ch.call("OnBeforeRetreat", card, {})
        meta["own_ran"] = True
        if ch.outv(r, "stopAction"):
            meta["stopped_by"] = "OnBeforeRetreat"
            return {"hits": ch.hits, "order": ch.order, "meta": meta}
    for c, _fn in ch.others("OnOtherCardRetreat", exclude=()):
        r = ch.call("OnOtherCardRetreat", c, {"cardRetreated": ptr})
        meta["n_0x36"] += 1
        if ch.outv(r, "stopAction"):
            meta["stopped_by"] = "OnOtherCardRetreat:%s" % (getattr(c, "name", "?"),)
            break
    return {"hits": ch.hits, "order": ch.order, "meta": meta}


def retreat_effects(km, st, card, **kw) -> dict:
    """0x36 撤退族 → 效果摘要（`Sim.event_fx["retreat"][card_id]` 可吃）。"""
    res = run_retreat(km, st, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 写回 engine.triggers 的命名空间
# 本模块的函数在 `engine.triggers` 里原名可见（`TR.run_death_chain` …）；热重载单独重载本模块时也会刷新那边。
FAMILY_NAMES = (
    'run_death_chain',
    'death_effects',
    '_unrevealed_covert',
    'run_move_frontline',
    'move_effects',
    'run_drawn_from_deck',
    'drawn_effects',
    'run_target_event',
    '_target_effects',
    'suppress_effects',
    'pin_effects',
    'has_custom_name1_attr',
    'run_turn_family',
    '_turn_effects',
    'before_start_of_turn_effects',
    'start_of_turn_effects',
    'end_of_turn_effects',
    'run_reveal',
    'reveal_effects',
    '_fam_effects',
    '_mk_chain',
    'run_veteran',
    'veteran_effects',
    'run_heal',
    'heal_effects',
    'run_steal',
    'steal_effects',
    'run_convert',
    '_bucket_effs',
    'convert_effects',
    'convert_fx',
    'run_abilities_changed',
    'abilities_changed_effects',
    'abilities_view_override',
    'fight_damage',
    'fight_damage_fx',
    '_hit_gaps',
    'run_damage_card',
    'damage_card_fx',
    'run_move_from_frontline',
    'move_from_frontline_effects',
    'run_retreat',
    'retreat_effects',
)
_T = _sys.modules["engine.triggers"]
for _n in FAMILY_NAMES:
    setattr(_T, _n, globals()[_n])
