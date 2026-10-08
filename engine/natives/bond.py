# -*- coding: utf-8 -*-
"""engine.natives.bond —— 协力（Bond，Homefront）一族：`HasBond` / `activeBondFactions` / 打出时检查 /
`GiveBond` / `RemoveBond`。

出处（BP 导出 1.60：`reverse-data/exports-1.60.27292.launcher-only-decompiled-BP/kards/Content/`，逐行读过；
1.58 导出同形）：

  * **检查发生在"打出时"，不是回合开始**：全仓只有 3 处 `ApplyFatigueDamage` 调用点，`fromBond=true` 只有一处 ——
    `BP_CardFunctions::CardPlayedFromHand`（`BP_CardFunctions.cpp:18682-18697`）。另两处 `fromBond=false`：空库抽牌
    （`:12979`）与 `BP_OnlineMatch.cpp:17328`（服务端动作回放）。回合开始流程（`BP_Logic::StartTurnBySide` `:9981-10005`、
    `ExecuteStartOfTurnEvents` `:11795`）里**没有**任何 `HasBond`。⇒ 早先的说法"手牌里带 Bond 的牌在回合开始
    扣总部"**不成立**（已更正）；回合开始只做一件事：重算 `activeBondFactions`。
  * `activeBondFactions` = `BP_GameState_Battle.cpp:2529-2576` `SetActiveBondsAtStartOfTurn(side)`：先 `Clear`，再对
    `GetAllCardInBattle()` 每张牌：`side 相等 ∧ IsLocatedOnBoard ∧ IsUnit ∧ ¬IsUnrevealedCovertCard` ⇒ 把它的 `faction` 加进集合。
    调用点只有 `BP_Logic.cpp:3550`（本地先手流程）与 `:10001`（`StartTurnBySide`）——**回合中不更新**（新部署的单位不算）；
    `BP_GameState_Battle.cpp:262` 是整局重置时清空。集合**属于当前行动方**（参数 `side`），不是永远属于我方。
  * `HasBond` = `getHasGameplayTag("ability.bond") ∧ ¬HasCustomAbility("bond_removed")`（IDA 0x144AFA…；
    `kardsmem/cardnatives.py:1300` 的 `PRIMS["HasBond"]` 已复核；`BP_CardFunctions.cpp:18682` 是调用点）。
  * 伤害量：`ApplyFatigueDamage(side, fromBond, &destroyed)`（`BP_CardFunctions.cpp:20185-20262`）= 当前 `FatigueDamage[side]`
    （`BP_GameState_Battle.cpp:1956`），总部 `defense -= 该值`，`destroyed = (总防 <= 0)`，**然后** `IncrementFatigueDamageBySide`
    （`:1992`）—— **与空库抽牌共用同一个计数**（`fromBond` 只影响通知文本与 `OnFatigueDamage` 钩子形参）。
    ⇒ 本仓的 `engine.natives.damage.apply_fatigue` 就是它的值语义。`destroyed` 为真 ⇒ `HQ_DestroyedEndMatch(对方)` 并
    **立刻 return**（`:18693-18700`），**这张牌自己的 `OnPlayedFromHand` 效果不再执行**。
  * `RemoveBond(cardID, instigatorID)`（`:23020-23032`）= `CustomAbilityAdd("bond_removed", cardID, instigatorID, …)`
    + 去 `visual.highlight` 标签 + 广播 `OnBondVisualsUpdated`；`GiveBond`（`:22908-23010`）= 若 `HasBond` 已真则 return，
    否则 `AddCustomGameplayTag("ability.bond")` + `CustomAbilityRemove("bond_removed", …, RemoveAll=true)` + 通知。
  * RATIONING（`Blueprints/Cards/USA/Homefront/events/card_event_rationing.cpp`）：`OnPlayedFromHand` → `WhichChooseOne`；
    分支 0 = 遍历 `GetCardsInHandBySide(side)`，每张 `HasBond` 为真的调 `RemoveBond(card, 本牌 ID)`；分支 1 =
    `ChangeDefense(己方总部, 本牌 ID, +4, 0x1, false)`。★ 反编译的 ubergraph 控制流有损（`RemoveBond` 之后是 `goto Label_838; return`，
    显然循环要继续 —— 弯路 #4），"遍历全部手牌"按卡面文本"Remove Bond from **all** cards in your hand"取。

本包不 import `sim`（依赖方向：kardsmem → engine → sim）：这些函数只认鸭子类型（`state.hand: {id: H}` / `state.units` /
`state.bond_factions` / `state.me`）。读不出 ⇒ **不下结论 + 记缺口**，不编数。
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

#: 自定义能力名（`RemoveBond` 写、`GiveBond` 清）。原版：`BP_CardFunctions.cpp:22971` / `:23022`。
BOND_REMOVED = "bond_removed"


def has_bond(card) -> bool:
    """原版：`UBaseCardObject::HasBond` = 带 `ability.bond` 标签 ∧ ¬`HasCustomAbility("bond_removed")`
    （`kardsmem/cardnatives.py:1300`，IDA 复核；调用点 `BP_CardFunctions.cpp:18682`）。

    `card` 是手牌 `H`（`.bond` / `.bond_removed`）。"""
    return bool(getattr(card, "bond", False)) and not bool(getattr(card, "bond_removed", False))


def active_bond_factions(cards: Iterable, side, *, is_unit: Optional[Callable] = None) -> set:
    """原版：`BP_GameState_Battle::SetActiveBondsAtStartOfTurn(side)` 的**读侧**（`BP_GameState_Battle.cpp:2529-2576`）。

    `cards` 是带 `.obj`（`BaseCardObject`）的快照牌；`side` 是 `ESide`。条件 = `obj.side == side ∧ IsLocatedOnBoard ∧ IsUnit
    ∧ ¬IsUnrevealedCovertCard`（`hasCovert ∧ ¬isRevealed`，`kardsmem/cardnatives.py:575`）；集合元素是 `int(obj.faction)`。
    `is_unit(card)` 可由调用方覆盖（`player.rule` 的类型判断多一层静态卡表兜底）；缺省用 `obj.IsUnit()`。
    `faction` 读不出的牌**跳过**（调用方若要严格可自查）。
    """
    out = set()
    for c in cards:
        o = c.obj
        if o is None or o.side != side:
            continue
        if not o.IsLocatedOnBoard():
            continue
        if not (is_unit(c) if is_unit is not None else o.IsUnit()):
            continue
        if bool(o.hasCovert) and not bool(o.isRevealed):          # IsUnrevealedCovertCard
            continue
        if o.faction is not None:
            out.add(int(o.faction))
    return out


def set_active_bonds_at_start_of_turn(state, side=None) -> None:
    """原版：`SetActiveBondsAtStartOfTurn(side)`（`BP_GameState_Battle.cpp:2529-2576`）在 **Sim** 上的写侧：
    先清空，再收 `side` 方场上单位的国家。

    Sim 的 `units` 只含单位（不含总部/手牌），且未揭示隐蔽 ⇒ 关键词里仍有 `covert`（揭示会去掉它，见 `sim.engine`）。
    任何一个单位的 `faction` 读不出 ⇒ 集合不完整 ⇒ **整体置 `None`（未知）** 并记缺口：宁可不下结论，
    也不让"漏了某国"被当成"协力未满足"去多扣一次疲劳。"""
    side = state.me if side is None else side
    out, unknown = set(), []
    for u in (state.units or {}).values():
        if u.side != side or "covert" in u.kw:
            continue
        if u.faction is None:
            unknown.append(u.id)
        else:
            out.add(int(u.faction))
    if unknown:
        state.bond_factions = None
        state.gaps.append("bond：回合开始重算 activeBondFactions 时 %d 个单位的 faction 读不出（%s）⇒ 集合未知，"
                          "协力牌打出时不下结论" % (len(unknown), unknown[:5]))
    else:
        state.bond_factions = out


def bond_check_on_play(state, card) -> bool:
    """原版：`CardPlayedFromHand` 里的协力检查（`BP_CardFunctions.cpp:18682-18700`）：

        `HasBond(card) ∧ ¬activeBondFactions.Contains(card.faction)` ⇒ `ApplyFatigueDamage(card.side, true)`；
        `destroyed` ⇒ `HQ_DestroyedEndMatch` 并 return（牌自己的 `OnPlayedFromHand` 不再执行）。

    `card` 是**刚被打出**的手牌 `H`（调用方在 `hand.pop` 之前取到它）。返回 `True` = 己方总部已被疲劳打爆
    （调用方据此**不再结算这张牌的效果**）。

    * `state.bond_factions is None`（读不出）⇒ **不下结论**：不扣、记缺口；
    * 国家读不出（`faction is None`）而集合已知 ⇒ 按"不在集合里"算（沿用重构前 `_eff_bond` 的口径，**待复核**：
      原版 `Set_Contains(set, faction)` 里 faction 是枚举值，静态卡不会缺 faction，实机快照总是有）；
    * 与空库抽牌共用疲劳计数：直接调 `engine.natives.damage.apply_fatigue`（伤害 = 当前计数，随后 +1）。
    ★ 未揭示隐蔽牌：原版 `:18652-18660` 先判 `IsUnrevealedCovertCard` 走另一条（`OnCovertCardPlayedFromHand`），
      反编译的 goto 汤里看不出那条是否也经过协力检查 ⇒ 这里**不区分**（与重构前一致），见 TODO。
    """
    if not has_bond(card):
        return False
    bf = getattr(state, "bond_factions", None)
    if bf is None:
        state.gaps.append("bond：activeBondFactions 未知 ⇒ 打出协力牌 %s 的疲劳伤害不下结论" % getattr(card, "name", "?"))
        return False
    f = getattr(card, "faction", None)
    if f is not None and int(f) in bf:
        return False
    from engine.natives.damage import apply_fatigue
    apply_fatigue(state)
    return state.hq.get(state.me, 0) <= 0


def give_bond(state, card_id) -> bool:
    """原版：`GiveBond(cardID, instigatorID)`（`BP_CardFunctions.cpp:22908-23010`）的**手牌**值语义：
    已 `HasBond` ⇒ 不动；否则带上 `ability.bond` 并清掉 `bond_removed`。返回是否改了。

    ★ 没建模：`instigatorID` 级的来源账（`receivedAbilitiesFromCards["ability.bond"]`）与 `ChangeBuffsFromCards` 的
      buff 文本记录 —— 只影响显示/按来源撤销，不影响 `HasBond` 的真值。
    """
    h = (state.hand or {}).get(card_id)
    if h is None or has_bond(h):
        return False
    h2 = h.copy()
    h2.bond, h2.bond_removed = True, False
    state.hand[card_id] = h2
    return True


def remove_bond(state, card_id) -> bool:
    """原版：`RemoveBond(cardID, instigatorID)`（`BP_CardFunctions.cpp:23020-23032`）的手牌值语义：
    写自定义能力 `bond_removed`（⇒ `HasBond` 变假）。返回是否改了（目标不在手牌/本来就移除过 ⇒ False）。

    门：`CustomAbilityAdd` 先过 `CanCardBeBuffed`（`:23622-23680`）——手牌（location 3/4）上无论是否未揭示隐蔽都放行，
    所以手牌上没有门要判。**写时复制**（`Sim.copy` 不复制 `H`）。"""
    h = (state.hand or {}).get(card_id)
    if h is None or h.bond_removed:
        return False
    h2 = h.copy()
    h2.bond_removed = True
    state.hand[card_id] = h2
    return True


def remove_bond_from_hand(state) -> list:
    """RATIONING 分支 0：对己方手牌里每张 `HasBond` 的牌 `RemoveBond`（`card_event_rationing.cpp`：`GetCardsInHandBySide(side)`
    → `HasBond` → `RemoveBond(card, 本牌)`）。返回被移除协力的牌 id 列表。"""
    done = []
    for cid in list((state.hand or {}).keys()):
        h = state.hand[cid]
        if has_bond(h) and remove_bond(state, cid):
            done.append(cid)
    return done
