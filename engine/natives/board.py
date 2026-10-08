# -*- coding: utf-8 -*-
"""engine.natives.board —— 棋盘一族原生函数的移植（每排容量 / 排满判定 / SpawnCardToBoard）。

只认协议、不 import `sim`（依赖方向 kardsmem → engine → sim）：调用方给的对象要有
`units`（{id: 单位}）、`front_limited`、`front_owner`、`me`、`opp`；单位要有 `row`
（"frontline" / "back"）与 `side`（`ESide`）。这样 `sim` 可以先用它，等 P2 把
`engine.state` 建起来再换成 engine 自己的状态。

容量常量**只有一份**：`kardsmem/gamemodel.py`（S1 模型，`FetchCardsByLocation` 用的就是它）。
"""
from __future__ import annotations

from kardsmem.gamemodel import FRONTLINE_CAP, FRONTLINE_CAP_LIMITED, HAND_CAP, SUPPORT_CAP

# 原版：BP_GameState_Battle::FetchCardsByLocation（BP_GameState_Battle.cpp:1567）——支援线的 5 个位
# **含总部卡本身**（HQ 与单位同属 Board_HQLeft/Right），所以支援线上的**单位**最多 4。
SUPPORT_UNITS_CAP = SUPPORT_CAP - 1

__all__ = ["HAND_CAP", "SUPPORT_CAP", "SUPPORT_UNITS_CAP", "FRONTLINE_CAP", "FRONTLINE_CAP_LIMITED",
           "row_full", "can_spawn_card", "apply_take_control", "unit_to_deck_top"]


def row_full(s, side, row) -> bool:
    """原版：BP_GameState_Battle::FetchCardsByLocation 的 `isLocationFull`（BP_GameState_Battle.cpp:1567，1.60 导出）——
    前线双方**共用** 5 个位（`IsFrontlineLimited()` 为真时 2）；支援线按"含总部 5 张"⇒ 单位 4、分边计。"""
    if row == "frontline":
        cap = FRONTLINE_CAP_LIMITED if getattr(s, "front_limited", False) else FRONTLINE_CAP
        return sum(1 for u in s.units.values() if u.row == "frontline") >= cap
    return sum(1 for u in s.units.values() if u.row == "back" and u.side == side) >= SUPPORT_UNITS_CAP


def can_spawn_card(s, side, row) -> bool:
    """原版：BP_CardFunctions::SpawnCardToBoard（BP_CardFunctions.cpp:19214，1.60 导出）——
    目标排满 ⇒ 不生成（没有指定卡 id 时）；前线还被对方占着（`FrontlineOwner` 不是本方）⇒ 拒绝。
    生成面板（攻/防/关键词）由调用方按静态卡表给，engine 不管；`caller` 拿不到面板时记缺口。"""
    if row_full(s, side, row):
        return False
    if row == "frontline" and s.front_owner == (s.opp if side == s.me else s.me):
        return False
    return True


def apply_take_control(state, unit, *, me) -> None:
    """`TakeControlOfEnemyUnit` → `ChangeUnitOwnership`（BP_CardFunctions.cpp:18087，2026-10-03 读过）。

    ★ 2026-10-04（P4 第十七刀）：**从 `sim/engine.py:627-643` 下沉到 engine**（规则下沉，§5 步骤 3），
    实现与那段一字不差；`sim` 侧改成转调（规则只写一处），`_event_fx_apply(s, "steal", …)` 的**扇出留在调用方**。

    原版：`side = 我方`、`underEnemyControl = true`、`movementLeft = 1`、`attackLeft = 1`（有 `fury` 则 2）
    ⇒ **偷来当回合就能动/能攻**（旧实现写 `sick=True` 是错的）。
    落点：原在**前线**且前线 >1 张 ⇒ 去我方**后排**；前线只有它 ⇒ 留在前线；原在后排 ⇒ 我方后排。
    """
    if unit.row == "frontline":
        n_front = sum(1 for u in state.units.values() if u.row == "frontline")
        if n_front > 1:
            unit.row = "back"
    unit.side = me
    unit.sick = False
    unit.moved = False
    unit.acted = False
    unit.attacks_left = 2 if "fury" in (getattr(unit, "kw", ()) or ()) else 1



def unit_to_deck_top(state, uid, pos=0) -> list:
    """`MoveCardToTopOfOwnersDeck(cardID, …, positionFromTop)` 的**场上单位**分支（BP_CardFunctions.cpp:3415 → `MoveCardToTopOfDeck`
    :17880）：单位**离场（不算摧毁）**，回到**原主人**（`originalSide`）牌库的 `positionFromTop`。

    模拟只建模**我方**牌库（敌方牌序读不到）：离场的是我方单位且牌库顺序已知 ⇒ 按 `pos` 插进 `state.deck`（越界夹到两端；模板沿用
    `card_templates`，有就挂进 `deck_cards`，同 `StealCardFromBoardToDeck` 的口径）；对方的单位只做离场、如实记缺口。
    ★ 这里的 `u.side` 是**当前控制方**，原版取的是 `originalSide`（被偷过的单位会不同）——状态里没有 originalSide ⇒ 如实记缺口。
    → 缺口列表（调用方并进自己的 gaps）。单位不在场 ⇒ 返回 None（调用方自己记『换不出场上的单位』）。"""
    u = (getattr(state, "units", {}) or {}).pop(uid, None)
    if u is None:
        return None
    gaps = ["牌库顶：单位回牌库按**当前控制方**判断主人（原版取 originalSide；被偷过的单位会不同，状态里没有该字段）"]
    if u.side != getattr(state, "me", None):
        gaps.append("牌库顶：单位回**对方**牌库未建模（牌序读不到）⇒ 只做离场")
        return gaps
    if not getattr(state, "deck_known", False) or not isinstance(getattr(state, "deck", None), list):
        gaps.append("牌库顶：牌库顺序未知（`deck_known=False`）⇒ 只做离场")
        return gaps
    n = len(state.deck)
    state.deck.insert(max(0, min(int(pos or 0), n)), uid)
    tpl = (getattr(state, "card_templates", {}) or {}).get(uid)
    if tpl is not None:
        if not isinstance(getattr(state, "deck_cards", None), dict):
            state.deck_cards = {}
        state.deck_cards.setdefault(uid, tpl)
    return gaps
