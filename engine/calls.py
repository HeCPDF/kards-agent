#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.calls —— **类型化状态变更的重放**（P5 正题 / TODO 的 M5 核心件）。

为什么需要它：
    P4 之后，一次出牌的动词效果可以**直跑**（`engine.scripts.native_hooks`）成"状态变更 + 一串
    `ctx.applied` 条目"。但搜索里**不可能每个节点都跑一次 VM**（VM 约 0.4 s vs 上千节点）⇒
    正确的形态是：**把一次出牌编译成一串「类型化调用」**（`("change_attack", uid, 3, ct)`），
    模拟时**只重放这串** —— 不跑 VM、不解释"效果字典" ✓。

与"效果字典"（`effectvm.to_effects`）的根本区别：
    * 字典是**键值约定**：键一多就没人能保证语义一致 ✗（历史上真出过"产出但没人消费"的键）；
    * 调用是**动词 + 实参**，重放走的是**与 sink 同一批原生函数**（`engine.natives.*`）⇒
      语义只有一处来源 ✓。

诚实边界（v1）：
    * 只重放 `_REPLAY` 表里的 kind；**表外的 kind 记一条明确的缺口**（`("gap", kind, ...)` 进
      `result.gaps`），**绝不静默跳过** ✗ —— 静默跳过 = 把效果丢掉，正是本重构要消灭的那类 bug。
    * 表里每一行都有**三角验证**测试（`tests/test_calls_replay.py`）：同一个动词分别走
      ① 直跑（改 state_b）与 ② 直跑→取 `applied`→`apply_calls` 重放到新 state（改 state_c），
      两条路的终态快照必须**一致** ✓ ⇒ 表里的实现一旦与 sink 漂移，测试当场红 ✓。
    * 与 `engine.scripts` 同一条分层纪律：本模块**只碰状态**（`engine.state` 的数据结构 + `engine.natives`），
      不 import `sim`、不 import `policy` ✓（`tests/test_arch_rules.py` 把关）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 重放结果：`applied` = 真重放了的 kind 列表；`gaps` = **没重放**的（表外 / 缺前置 / 参数不全）。
class ReplayResult:
    __slots__ = ("applied", "gaps", "popped", "pending", "removed", "fanned", "on_death")

    def __init__(self):
        self.applied: List[tuple] = []
        self.gaps: List[str] = []
        #: **挂起的玩家提示**（预报/三选一/选手牌）：不改状态，由**调用方**（`sim.engine.run` 的 Prompt 流程）展开；
        #: 形状 `(提示名, 牌 id)`。与 `gaps` 的区别 —— 这不是『我们不会重放』，而是『这一步本来就要停下来问玩家』：
        #: 调用方有候选表就挂起展开，没有才记缺口（和字典路 `_apply_eff` 的 `forecast`/`choose_spawn` 标记同一口径）。
        self.pending: List[tuple] = []
        #: 本次重放里**已经移场**的 id —— `ctx.applied` 是**日志**：同一次离场可能留两条
        #: （例：`DestroyCard` 的 sink 先记 `("destroy_card", uid)` 再记 `("destroy", uid, why)` ✓）
        #: ⇒ 第二条不该被当成"单位不在场上"的缺口 ✗（那是假缺口）。
        self.popped: set = set()
        #: 真的被本次重放移出场的 id / 已经跑过死亡链扇出的 id（`on_death` 注入时用，防同一次死亡扇两遍）。
        self.removed: set = set()
        self.fanned: set = set()
        #: 死亡链扇出回调 `(state, uid)`（**调用方注入**，`apply_calls(on_death=…)`）：单位**已离场**之后调用，
        #: 与直跑 `DirectCtx.destroy/died` 里的 `ctx.on_death` 同一语义 ⇒ 重放与直跑在『死亡后果』上一致。
        self.on_death = None

    def __repr__(self) -> str:
        return "ReplayResult(applied=%d, gaps=%d)" % (len(self.applied), len(self.gaps))


def _unit(state, uid):
    return (getattr(state, "units", {}) or {}).get(uid)


def _pop(state, uid, kind, res) -> None:
    if uid in res.popped:                       # 本次重放里已经移过场 ⇒ 同一个离场的第二条日志，不是缺口 ✓
        return
    if (getattr(state, "units", {}) or {}).pop(uid, None) is None:
        res.gaps.append("%s：%s 不在场上（未重放）" % (kind, uid))
    else:
        res.removed.add(uid)
    res.popped.add(uid)


def _fan_death(state, res, uid) -> None:
    """单位**已离场**之后跑死亡链扇出（调用方注入的 `on_death`；没注入 ⇒ 什么都不做，与旧行为一致）。一次死亡只扇一遍。"""
    if res.on_death is None or uid in res.fanned:
        return
    res.fanned.add(uid)
    res.on_death(state, uid)


# ---------------------------------------------------------------------------
# 各 kind 的重放（**逐行镜像** engine/scripts.py 里对应 sink 的状态变更）
# ---------------------------------------------------------------------------
def _k_kredits(state, res, side, delta):
    from engine.natives.kredits import change_kredits                     # noqa: PLC0415
    if side == "mine":
        change_kredits(state, delta)
    else:
        state.opp_kredits = getattr(state, "opp_kredits", 0) + delta
    res.applied.append(("kredits", side, delta))


def _k_slots(state, res, side, delta):
    from engine.natives.kredits import change_slots                      # noqa: PLC0415
    if side == "mine":
        change_slots(state, delta)
    else:
        state.opp_slots = getattr(state, "opp_slots", 0) + delta
    res.applied.append(("slots", side, delta))


def _k_kredit_set(state, res, seat, val):
    """`setKreditBySide` 的 sink 对方侧是**记缺口、不改状态** ⇒ 重放同口径（不猜）✓。"""
    if seat != getattr(state, "me", None):
        res.gaps.append("kredit_set：对方绝对值不知道（同 sink 口径，不猜）")
        return
    state.kredits = float(val)
    res.applied.append(("kredit_set", seat, val))


def _k_attack_turn(state, res, uid, n):
    from engine.natives.stats import clamp_stat                          # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("attack_turn：%s 不在场上" % (uid,))
        return
    u.atk = clamp_stat(int(getattr(u, "atk", 0) or 0) + int(n or 0))
    u.atk_turn = int(getattr(u, "atk_turn", 0) or 0) + int(n or 0)
    res.applied.append(("attack_turn", uid, n))


def _k_spawn(state, res, uid, name, side, row, atk, dfn, cost, typ, kw):
    """`SpawnCardOnBattlefield` / `SpawnCardInFrontline` 的 sink（`scripts_sinks_more._spawn_sink`）逐行镜像：
    载荷带着 side/row/面板，所以不需要再查卡库。`can_spawn_card` 的判定在 sink 里做过了（只有真生成了才会记这条）。"""
    from engine.state import U                                           # noqa: PLC0415
    state.tmp_seq = int(getattr(state, "tmp_seq", 0) or 0) + 1
    state.units[uid] = U(uid, side, row, atk, dfn, cost, typ, tuple(kw or ()), sick=True)
    res.applied.append(("spawn", uid, name, side, row, atk, dfn, cost, typ, tuple(kw or ())))


def _k_deck_add(state, res, cid, name, idx):
    """`("deck_add", 新牌 id, 卡名, 插入下标)`：`SpawnCardInDeckBySide` sink 逐张记的结果（随机位置已算成确定下标）。
    重放 = 同一个 `make_card` 造牌 + 插进同一下标（**不重抽随机流** ⇒ 与重放侧的流位置无关）。"""
    from engine.natives.deck import make_card                           # noqa: PLC0415
    if not getattr(state, "deck_known", False):
        res.gaps.append("deck_add：牌库未知（deck_known=False）⇒ 未重放")
        return
    tpl = make_card(state, cid, name, getattr(state, "hold_v", None))
    deck = state.deck
    deck.insert(max(0, min(int(idx), len(deck))), cid)
    state.deck_cards[cid] = tpl
    state.card_templates[cid] = tpl
    state.tmp_seq = max(int(getattr(state, "tmp_seq", 0) or 0), -int(cid) - 3000)
    res.applied.append(("deck_add", cid, name, idx))


def _k_deck_shuffle(state, res, seat, perm=None):
    """`("deck_shuffle", 座位, 洗后牌序)`：照记下的牌序还原（同一多重集才认；否则记缺口）。没有牌序（旧记法）⇒ 缺口。"""
    if perm is None:
        res.gaps.append("deck_shuffle：记录里没有洗后牌序（旧记法）⇒ 未重放")
        return
    if sorted(map(str, state.deck)) != sorted(map(str, perm)):
        res.gaps.append("deck_shuffle：洗后牌序与当前牌库不是同一批牌 ⇒ 未重放")
        return
    state.deck = list(perm)
    res.applied.append(("deck_shuffle", seat, tuple(perm)))


def _k_to_deck(state, res, cid, idx=0):
    """`("to_deck", 手牌 id[, 下标])`：`MoveCardToTopOfOwnersDeck` sink（手牌 → 牌库下标）。"""
    from engine.natives.deck import move_hand_to_deck                   # noqa: PLC0415
    got = move_hand_to_deck(state, cid, idx, gaps=res.gaps)
    if got is not None:
        res.applied.append(("to_deck", cid, got))


def _k_gain_hand(state, res, cid, name):
    """`("gain_hand", 新牌 id, 卡名)`：`SpawnCardinHandbySide` sink（手里多一张新牌，**不是抽牌**）。"""
    from engine.natives.deck import make_card                           # noqa: PLC0415
    if len(state.hand) >= _hand_cap():
        res.gaps.append("gain_hand：手牌已满 ⇒ 未重放")
        return
    tpl = make_card(state, cid, name, getattr(state, "hold_v", None))
    state.hand[cid] = tpl
    state.card_templates[cid] = tpl
    state.cap_used = max(int(getattr(state, "cap_used", 0) or 0), -int(cid) - 1000)
    res.applied.append(("gain_hand", cid, name))


def _k_discard(state, res, cid):
    """`("discard", 手牌 id)`：`DiscardCardFromHand` sink（`scripts_sinks_more._sink_discard_from_hand`）——己方手牌里有就移走。"""
    if (getattr(state, "hand", None) or {}).pop(cid, None) is None:
        res.gaps.append("discard：%s 不在我方手牌（未重放）" % (cid,))
        return
    res.applied.append(("discard", cid))


def _hand_cap() -> int:
    from engine.natives.board import HAND_CAP                           # noqa: PLC0415
    return HAND_CAP


def _stat_replay(attr_fn, kind):
    """`change_attack` / `change_operation_cost` / `change_heavy` / `change_defense` 的通用重放。"""
    def _impl(state, res, uid, amount, ct):
        u = _unit(state, uid)
        if u is None:
            res.gaps.append("%s：%s 不在场上" % (kind, uid))
            return
        attr_fn(u, amount, ct)
        res.applied.append((kind, uid, amount, ct))
    return _impl


def _k_destroy(state, res, uid, why=""):
    _pop(state, uid, "destroy", res)
    res.applied.append(("destroy", uid, why))
    if uid in res.removed:                       # 直跑 `ctx.destroy`：移场 + 记事件 + 死亡链扇出（同序）
        _fan_death(state, res, uid)


def _k_leave_board(state, res, uid, why=""):
    _pop(state, uid, "leave_board", res)
    res.applied.append(("leave_board", uid, why))


def _k_retreat(state, res, uid, why=""):
    _pop(state, uid, "retreat", res)
    res.applied.append(("retreat", uid, why))


def _k_destroy_card(state, res, uid):
    """`DestroyCard` 的 sink 记的第一条（`("destroy_card", uid)`）；随后还有 `("destroy", uid, why)`。
    两条**指同一次离场** ⇒ 靠 `res.popped` 去掉第二条的假缺口 ✓。"""
    _pop(state, uid, "destroy_card", res)
    res.applied.append(("destroy_card", uid))


def _k_died(state, res, uid, why=""):
    """**事件 only**（`deal_damage` 自己已经 pop 过 ⇒ 这里再 pop 会造假缺口 ✗）。"""
    res.applied.append(("died", uid, why))


def _k_pin(state, res, uid):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("pin：%s 不在场上" % (uid,))
        return
    u.pinned = True
    res.applied.append(("pin", uid))


def _k_unpin(state, res, uid):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("unpin：%s 不在场上" % (uid,))
        return
    u.pinned = False
    res.applied.append(("unpin", uid))


def _k_suppress(state, res, uid):
    from engine.natives.status import apply_suppress                     # noqa: PLC0415
    if _unit(state, uid) is None:
        res.gaps.append("suppress：%s 不在场上" % (uid,))
        return
    res.gaps += list(apply_suppress(state, uid) or ())      # 它自己会把"没建模的那部分"如实返回 ✓
    res.applied.append(("suppress", uid))


def _k_move_front(state, res, uid):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("move_front：%s 不在场上" % (uid,))
        return
    if u.row == "back" and getattr(state, "front_owner", None) != getattr(state, "opp", None):
        u.row = "frontline"
    res.applied.append(("move_front", uid))


def _k_reset_ops(state, res, uid):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("reset_ops：%s 不在场上" % (uid,))
        return
    u.attacks_left = 2 if "fury" in (getattr(u, "kw", ()) or ()) else 1
    u.acted, u.moved = False, False
    res.applied.append(("reset_ops", uid))


def _k_damage_card(state, res, uid, amount):
    from engine.natives.damage import deal_damage                        # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("damage_card：%s 不在场上" % (uid,))
        return
    deal_damage(state, u, int(amount or 0), engage=False,
                on_death=lambda st, x: (res.applied.append(("died", x, "DamageCard 伤害致死")),
                                        res.removed.add(x), _fan_death(st, res, x)))
    res.applied.append(("damage_card", uid, amount))


def _k_damage_aoe(state, res, uid, amount):
    """`DamageMultipleCards` 对**一个**场上单位的伤害（sink 每个目标记一条 `("damage_aoe", uid, amt)`）。

    与 sink 同一行：`deal_damage(state, u, amt, engage=False, ...)`；死亡只记事件（`deal_damage` 已 pop）。
    打到总部的那份 sink 记的是 `("damage_hq", 座位, amt)`（走 `_k_damage_hq`），不会出现在这里 ✓。
    原版：`DamageMultipleCards`（`BP_CardFunctions` 群体伤害循环；免疫单位由 `deal_damage` 自己处理）。"""
    from engine.natives.damage import deal_damage                        # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("damage_aoe：%s 不在场上" % (uid,))
        return
    deal_damage(state, u, amount, engage=False,
                on_death=lambda st, x: (res.applied.append(("died", x, "DamageMultipleCards")),
                                        res.removed.add(x), _fan_death(st, res, x)))
    res.applied.append(("damage_aoe", uid, amount))


def _k_heal_unit(state, res, uid, mdef):
    """`FullyHealCard` 通过门槛后的结果：`defense = maxDefense`（sink 记 `("heal_unit", uid, mdef)`，
    `mdef` 就是写进去的那个值 ⇒ 重放不需要再算门槛/否决 ✓；门槛与 0xC 否决已在直跑时判过）。"""
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("heal_unit：%s 不在场上" % (uid,))
        return
    u.dfn = mdef
    res.applied.append(("heal_unit", uid, mdef))


def _k_damage_hq(state, res, side, amount):
    """总部吃伤害（`DamageCard` 的目标是总部牌；与 sink 同一行：`hq[座位] -= 伤害`）。"""
    state.hq[side] = state.hq.get(side, 0) - max(int(amount or 0), 0)
    res.applied.append(("damage_hq", side, amount))


def _k_change_defense_hq(state, res, side, amount, ct):
    """`ChangeDefense` 打在总部牌上（与 sink 同一函数 `natives.stats.change_defense_hq`）。"""
    from engine.natives.stats import change_defense_hq                   # noqa: PLC0415
    change_defense_hq(state, side, amount, ct)
    res.applied.append(("change_defense_hq", side, amount, ct))


def _k_reveal(state, res, uid, had=None):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("reveal：%s 不在场上" % (uid,))
        return
    u.kw = set(getattr(u, "kw", ()) or ()) - {"covert"}
    res.applied.append(("reveal", uid, had))


def _k_kredits_tax(state, res, uid, amount):
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("kredits_tax：%s 不在场上" % (uid,))
        return
    u.tax = max(0, int(getattr(u, "tax", 0) or 0) + int(amount or 0))
    res.applied.append(("kredits_tax", uid, amount))


def _k_restriction_add(state, res, side, ty, turns):
    """★ `turns` **必须**在条目里（否则重放要凭空写一个 0 ✗ = 静默写错；宁可记缺口 ✓）。
    为此 `engine/scripts.py` 的 sink 已把 `turns` 记进 `applied` ✓（三角验证当场抓到的）。"""
    if getattr(state, "restrictions", None) is None:
        state.restrictions = []
    state.restrictions.append({"side": side, "type": ty, "turns": turns})
    res.applied.append(("restriction_add", side, ty, turns))


def _k_restriction_remove(state, res, side, ty):
    cur = list(getattr(state, "restrictions", None) or [])
    state.restrictions = [x for x in cur if not (x.get("side") == side and x.get("type") == ty)]
    res.applied.append(("restriction_remove", side, ty))


def _k_playing_side(state, res, seat):
    state.playing_side = seat
    res.applied.append(("playing_side", seat))


def _k_force_end_turn(state, res, other):
    state.playing_side = other
    res.applied.append(("force_end_turn", other))


def _k_end_match(state, res, seat):
    loser = state.opp if seat == state.me else state.me
    state.hq[loser] = 0
    res.applied.append(("end_match", seat))


def _k_set_enc(state, res, cid, field, value):
    from engine.natives.stats import set_encrypted_field                 # noqa: PLC0415
    card = (getattr(state, "hand", {}) or {}).get(cid) or _unit(state, cid)
    if card is None:
        res.gaps.append("set_enc：%s 既不在手牌也不在场上（未重放）" % (cid,))
        return
    set_encrypted_field(card, field, value)
    res.applied.append(("set_enc", cid, field, value))


def _k_custom_ability_add(state, res, uid, name, giver=None):
    """`("custom_ability_add", uid, name[, giver])`：与直跑 sink 同走 `grant_ability`（计数 + giver 账）。"""
    from engine.natives.abilities import grant_ability                   # noqa: PLC0415
    if _unit(state, uid) is None:
        res.gaps.append("custom_ability*：%s 不在场上" % (uid,))
        return
    grant_ability(state, uid, str(name), giver)
    res.applied.append(("custom_ability_add", uid, str(name), giver))


def _k_custom_ability(state, res, uid, name, remove=False, remove_all=True):
    from engine.natives.abilities import custom_ability_add, custom_ability_remove   # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("custom_ability*：%s 不在场上" % (uid,))
        return
    if remove:
        custom_ability_remove(u, str(name), remove_all)
        res.applied.append(("custom_ability_remove", uid, str(name)))
    else:
        custom_ability_add(u, str(name))
        res.applied.append(("custom_ability_add", uid, str(name)))


def _k_remove_bond(state, res, cid):
    """`remove_bond(card_id)`：镜像 `RemoveBond` sink（`engine.natives.bond.remove_bond`，手牌；写时复制）。"""
    from engine.natives.bond import remove_bond                          # noqa: PLC0415
    if remove_bond(state, cid):
        res.applied.append(("remove_bond", cid))
    elif cid not in (getattr(state, "hand", {}) or {}):
        res.gaps.append("remove_bond：%s 不在手牌里（未重放）" % (cid,))


def _k_opp_draw(state, res, n):
    """`("opp_draw", n)`：直跑 sink（`DrawCardsFromDeckBySide` / `DrawSpecificCardFromDeckBySide` 的**对方侧**，
    `engine.scripts_sinks_more`）唯一碰的状态就是 `state.opp_cards += n`（对方手牌数记账；对方真实牌库/手牌内容
    Sim 不跟踪）⇒ 逐行镜像，与字典路 `sim.engine._apply_eff` 末行 `s.opp_cards += opp_draw + opp_gain_cards` 同口径 ✓。
    原版：`DrawCardsFromDeckBySide`（BP_CardFunctions.cpp:6499，逐张调 `DrawTopCardFromDeck`，对方 side ⇒ 对方牌库顶进
    **对方**手牌）、`DrawTopCardFromDeck`（:12958；牌库空 ⇒ 疲劳、手牌满 ⇒ 烧牌，这两种边界 Sim 没有对方牌库/手牌容量，
    sink 与字典路都不建模 ⇒ 重放同口径，不猜）。张数不是整数 ⇒ 抛 TypeError ⇒ `apply_calls` 记缺口。"""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("opp_draw 的张数必须是整数: %r" % (n,))
    state.opp_cards = int(getattr(state, "opp_cards", 0) or 0) + n
    res.applied.append(("opp_draw", n))


def _k_opp_gain_hand(state, res, name=None):
    """`("opp_gain_hand", 卡名)`：`SpawnCardinHandbySide` 的**对方侧**（`scripts_sinks_more._sink_spawn_in_hand`）：对方手牌只有张数 ⇒
    `opp_cards += 1`（与字典路 `opp_gain_cards` 同口径）。"""
    state.opp_cards = int(getattr(state, "opp_cards", 0) or 0) + 1
    res.applied.append(("opp_gain_hand", name))


def _k_kredit_cost(state, res, cid, amount, ct):
    """`("kredit_cost", 手牌 id, 数值, changeType)`：`ChangeKreditCost` sink（目标是手牌/牌库里的牌）。"""
    from engine.natives.stats import change_kredit_cost                  # noqa: PLC0415
    card = (getattr(state, "hand", {}) or {}).get(cid) or (getattr(state, "deck_cards", {}) or {}).get(cid)
    if card is None:
        res.gaps.append("kredit_cost：%s 不在手牌/牌库（未重放）" % (cid,))
        return
    change_kredit_cost(card, amount, ct)
    res.applied.append(("kredit_cost", cid, amount, ct))


def _k_hand_opcost(state, res, cid, amount, ct):
    """`("hand_opcost", 手牌 id, 数值, changeType)`：`ChangeOperationCost` 作用在手牌上（`stats.change_hand_operation_cost`）。"""
    from engine.natives.stats import change_hand_operation_cost          # noqa: PLC0415
    card = (getattr(state, "hand", {}) or {}).get(cid)
    if card is None:
        res.gaps.append("hand_opcost：%s 不在手牌（未重放）" % (cid,))
        return
    change_hand_operation_cost(card, amount, ct)
    res.applied.append(("hand_opcost", cid, amount, ct))


def _k_unit_to_deck(state, res, uid, pos=0):
    """`("unit_to_deck", uid, pos)`：`MoveCardToTopOfOwnersDeck` 的场上单位分支，与 sink 同一个原生
    （`engine.natives.board.unit_to_deck_top`：离场 + 我方牌库按 `pos` 插入；缺口原样并回）。"""
    from engine.natives.board import unit_to_deck_top                    # noqa: PLC0415
    g = unit_to_deck_top(state, uid, pos)
    if g is None:
        res.gaps.append("unit_to_deck：%s 不在场上" % (uid,))
        return
    res.gaps += g
    res.removed.add(uid)
    res.popped.add(uid)
    res.applied.append(("unit_to_deck", uid, pos))


def _k_fight(state, res, a_id, b_id):
    """`MakeCardsFight` 的 sink（`scripts_sinks_core._sink_make_cards_fight`）逐行镜像：同一个原生 `natives.damage.apply_fight`
    （互相受对方 atk、无重甲、免疫 ⇒ 0；一方不在场 ⇒ 记缺口不结算），死亡走 `deal_damage` 回调 ⇒ 记 `died` + 扇出死亡链。
    `apply_fight` 返回的说明性缺口（`FIGHT_GAP`）原样并进 `res.gaps`（与直跑的 `ctx.gaps += run` 同口径）。"""
    from engine.natives.damage import apply_fight, deal_damage            # noqa: PLC0415

    def _on_death(st, x):
        res.applied.append(("died", x, "MakeCardsFight"))
        res.removed.add(x)
        _fan_death(st, res, x)

    def _dd(st, unit, dmg):
        deal_damage(st, unit, dmg, engage=False, on_death=_on_death)

    res.gaps += list(apply_fight(state, a_id, b_id, deal_damage=_dd, kill=_on_death) or [])
    res.applied.append(("fight", a_id, b_id))


def _k_convert(kind):
    """`("convert_board"|"convert_hand"|"convert_deck", 旧 id, 新 id, <载荷>)`：与 `ConvertCard` sink 同一个原生
    （`engine.natives.convert.convert_one`），载荷由条目自带 ⇒ 不用再问卡库。落点/新 id 对不上直跑记录 ⇒ 记缺口（不静默）。"""
    def _impl(state, res, old, nid, *payload):
        from engine.natives.convert import convert_from_args, convert_one      # noqa: PLC0415
        got_kind, got_nid, gap = convert_one(state, old, convert_from_args(*payload))
        if got_kind != kind:
            res.gaps.append("convert_%s：%s 重放时落点是 %s（直跑记录是 %s）" % (kind, old, got_kind, kind))
            return
        if got_nid != nid:
            res.gaps.append("convert_%s：新牌 id 重放得 %s、直跑记录 %s" % (kind, got_nid, nid))
        if gap and kind != "board":
            res.gaps.append(gap)
        res.applied.append(("convert_" + kind, old, nid) + tuple(payload))
    return _impl


def _k_steal(state, res, uid):
    """`TakeControlOfEnemyUnit` 的 sink（`scripts_sinks_more._sink_take_control`）逐行镜像：同一个原生
    `natives.board.apply_take_control`；说明性缺口原样并进 `res.gaps`。"""
    from engine.natives.board import apply_take_control                   # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("steal：%s 不在场上" % (uid,))
        return
    apply_take_control(state, u, me=state.me)
    res.applied.append(("steal", uid))
    res.gaps.append("steal：我方后排满的分支 / hasActivePincerEffect / CardLocationMoved 未建模")


def _k_deck_add(state, res, cid, name, idx=0):
    """`SpawnCardInDeckBySide` 的 sink（`_sink_spawn_in_deck`）每张牌一条 `("deck_add", cid, 名, 实际插入下标)`：
    中性占位模板（费用 2、`order`、eff 空）+ 按记录的下标插进己方牌库。随机位置/洗牌是随机流的产物 ⇒ 记结果、不重算；
    洗牌结果由紧随其后的 `("deck_order", …)` 写回。"""
    from engine.state import H                                           # noqa: PLC0415
    if not isinstance(getattr(state, "deck", None), list):
        state.deck = []
    state.deck.insert(min(len(state.deck), int(idx or 0)), cid)
    tpl = H(cid, name, 2, "order", {})
    if not isinstance(getattr(state, "deck_cards", None), dict):
        state.deck_cards = {}
    if not isinstance(getattr(state, "card_templates", None), dict):
        state.card_templates = {}
    state.deck_cards[cid] = tpl
    state.card_templates[cid] = tpl
    state.tmp_seq = max(int(getattr(state, "tmp_seq", 0) or 0), -3000 - int(cid))   # sink 里 cid = -(3000 + tmp_seq)
    res.applied.append(("deck_add", cid, name, idx))


def _k_deck_order(state, res, order):
    """`("deck_order", 新牌序)`：洗牌的**结果**（`ctx.rng.shuffle` 的产物）照写——重放没有那条随机流。"""
    state.deck = list(order)
    res.applied.append(("deck_order", tuple(order)))


def _k_give_kw(state, res, uid, kw):
    """`Give<关键词>` 的 sink（`engine.scripts_sinks_core._give_sink`）逐行镜像：`kw` 集合加一项；`blitz` ⇒ `sick=False`；
    `fury` ⇒ `attacks_left = max(现有, 未行动 2 否则 1)`（0x1D 广播只是事件扇出，不改状态）。"""
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("give_kw：%s 不在场上" % (uid,))
        return
    u.kw = set(getattr(u, "kw", ()) or ()) | {kw}
    if kw == "blitz":
        u.sick = False
    if kw == "fury":
        u.attacks_left = max(int(getattr(u, "attacks_left", 0) or 0), 2 if not getattr(u, "acted", False) else 1)
    res.applied.append(("give_kw", uid, kw))


def _k_steal(state, res, uid):
    """`("steal", uid)`：镜像 `TakeControlOfEnemyUnit` 的 sink（`engine.scripts_sinks_more._sink_take_control`）——
    规则本体同一个函数 `engine.natives.board.apply_take_control(state, u, me=我方座位)`；离场/入场钩子（0x2E/0x8/…）是调用方
    事件扇出（sink 的 `ctx.fire("steal")`），不改这里的状态。"""
    from engine.natives.board import apply_take_control                  # noqa: PLC0415
    u = _unit(state, uid)
    if u is None:
        res.gaps.append("steal：%s 不在场上" % (uid,))
        return
    apply_take_control(state, u, me=state.me)
    res.applied.append(("steal", uid))


#: kind → 重放函数（**只列"能逐行镜像 sink"的**；表外一律记缺口 ✓）
_REPLAY: Dict[str, Any] = {
    "kredits": _k_kredits,
    "slots": _k_slots,
    "kredit_set": _k_kredit_set,
    "attack_turn": _k_attack_turn,
    "change_attack": _stat_replay(lambda u, a, ct: __import__(
        "engine.natives.stats", fromlist=["x"]).change_attack(u, a, ct), "change_attack"),
    "change_defense": _stat_replay(lambda u, a, ct: __import__(
        "engine.natives.stats", fromlist=["x"]).change_defense(u, a, ct), "change_defense"),
    "change_heavy": _stat_replay(lambda u, a, ct: __import__(
        "engine.natives.stats", fromlist=["x"]).change_heavy_armor(u, a, ct), "change_heavy"),
    "change_opcost": _stat_replay(lambda u, a, ct: __import__(
        "engine.natives.stats", fromlist=["x"]).change_operation_cost(u, a, ct), "change_opcost"),
    "destroy": _k_destroy,
    "destroy_card": _k_destroy_card,
    "leave_board": _k_leave_board,
    "retreat": _k_retreat,
    "died": _k_died,
    "pin": _k_pin,
    "unpin": _k_unpin,
    "suppress": _k_suppress,
    "move_front": _k_move_front,
    "reset_ops": _k_reset_ops,
    "damage_card": _k_damage_card,
    "damage_hq": _k_damage_hq,
    "damage_aoe": _k_damage_aoe,
    "heal_unit": _k_heal_unit,
    "spawn": _k_spawn,
    "change_defense_hq": _k_change_defense_hq,
    "reveal": _k_reveal,
    "kredits_tax": _k_kredits_tax,
    "restriction_add": _k_restriction_add,
    "restriction_remove": _k_restriction_remove,
    "playing_side": _k_playing_side,
    "force_end_turn": _k_force_end_turn,
    "end_match": _k_end_match,
    "set_enc": _k_set_enc,
    "custom_ability_add": _k_custom_ability_add,
    "remove_bond": _k_remove_bond,
    "opp_draw": _k_opp_draw,
    "deck_add": _k_deck_add,
    "deck_shuffle": _k_deck_shuffle,
    "to_deck": _k_to_deck,
    "gain_hand": _k_gain_hand,
    "opp_gain_hand": _k_opp_gain_hand,
    "kredit_cost": _k_kredit_cost,
    "hand_opcost": _k_hand_opcost,
    "discard": _k_discard,
    "give_kw": _k_give_kw,
    "steal": _k_steal,
    "deck_add": _k_deck_add,
    "deck_order": _k_deck_order,
    "convert_board": _k_convert("board"),
    "convert_hand": _k_convert("hand"),
    "convert_deck": _k_convert("deck"),
    "fight": _k_fight,
    "unit_to_deck": _k_unit_to_deck,
    "custom_ability_remove": lambda state, res, uid, name, remove_all=True, *rest: _k_custom_ability(
        state, res, uid, name, remove=True, remove_all=remove_all),
}

#: **明确不重放**（事件/信息类，不改状态）——列出来是为了与"表外未实现"区分开 ✓
_NO_STATE: Dict[str, str] = {
    "intel_seen": "情报计数是给 rule 层跑触发的**事件**，不改 sim 状态",
    "convert_skip_trigger": "只是标记",
    "retreat_skipped": "只是标记",
    "damage_amounts": "只是诊断用的明细",
    "change_attack_hq": "总部牌的攻击力：总部不攻击、Sim 不跟踪 ⇒ 没有状态可写（原版 ChangeAttack 对总部无特判）",
}

#: **挂起提示标记**（直跑的 `_pending_sink` 记下的）：重放时**不改状态**、进 `res.pending` 交调用方（见 `ReplayResult.pending`）。
_PENDING: Dict[str, str] = {
    "forecast_pending": "forecast",
    "choose_spawn_pending": "choose_spawn",
    "hand_target_pending": "hand_target",
}


def apply_calls(state, calls, *, verbose: bool = False, on_draw=None, on_death=None) -> ReplayResult:
    """把一串**类型化调用**（`engine.scripts` 直跑时的 `ctx.applied`）重放到 `state` 上。

    * 表内 kind ⇒ 调**与 sink 同一批原生函数**改状态 ✓；
    * `_NO_STATE` 里的 kind ⇒ 明确"不改状态"，记进 `applied`（不算缺口 ✓）；
    * **其它一律记缺口**（含 `convert_*`/`veteran`/`fight` 这些**信息不全**的
      —— 重放它们需要 sink 之外的载荷（卡面板/`vet`/`conv`/…）⇒ v1 不猜 ✓）。
    """
    res = ReplayResult()
    res.on_death = on_death
    for call in calls or ():
        if not isinstance(call, (list, tuple)) or not call:
            res.gaps.append("apply_calls：条目不是 (kind, …)：%r" % (call,))
            continue
        kind, rest = call[0], tuple(call[1:])
        if kind == "draw_specific":                      # ("draw_specific", cid)：先挪到牌顶、再抽 1 张（同 sink）
            from engine.natives.deck import pull_specific_to_top  # noqa: PLC0415
            if on_draw is None:
                res.gaps.append("apply_calls：draw_specific 需要调用方注入 on_draw（抽牌链）—— 不猜")
                continue
            if pull_specific_to_top(state, rest[0] if rest else None, gaps=res.gaps) is False:
                continue
            on_draw(state, 1)
            res.applied.append((kind,) + rest)
            continue
        if kind in ("gain_cards", "draw"):               # 抽牌链由调用方注入（engine 不 import sim）；没给 ⇒ 缺口
            # ('draw', n)：直跑 sink（`DrawTopCardFromDeck` / `DrawCardsFromDeckBySide` / `DrawSpecificCardFromDeckBySide`）
            # 只在**我方**抽牌时记，载荷恒为 `("draw", n)`，且在 sink 里就是 `ctx.on_draw(state, n)` ⇒ 这里同口径重放 ✓。
            # 对方抽牌记的是 `("opp_draw", n)`（另一个 kind，走 `_k_opp_draw`，只记账 `opp_cards`，不经抽牌链）。
            if on_draw is None:
                res.gaps.append("apply_calls：%s 需要调用方注入 on_draw（抽牌链）—— 不猜" % kind)
            else:
                try:
                    n = int(rest[0]) if rest else 1
                except (TypeError, ValueError):
                    res.gaps.append("apply_calls：%s 的张数对不上: %r" % (kind, call))
                    continue
                on_draw(state, n)
                res.applied.append((kind,) + rest)
            continue
        fn = _REPLAY.get(kind)
        if fn is not None:
            try:
                fn(state, res, *rest)
            except TypeError as ex:                     # 参数形状对不上 ⇒ 记缺口，别静默 ✓
                res.gaps.append("apply_calls：%s 的参数对不上（%s）: %r" % (kind, ex, call))
            continue
        if kind in _PENDING:
            res.applied.append((kind,) + rest)
            res.pending.append((_PENDING[kind],) + rest)
            continue
        if kind in _NO_STATE:
            res.applied.append((kind,) + rest)
            if verbose:
                res.gaps.append("%s：%s（按设计不改状态）" % (kind, _NO_STATE[kind]))
            continue
        res.gaps.append("apply_calls：kind=%r 未在重放表里（v1 不猜）" % (kind,))
    return res
