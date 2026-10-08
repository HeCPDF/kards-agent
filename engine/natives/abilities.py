# -*- coding: utf-8 -*-
"""engine.natives.abilities —— **自定义能力**一族（`CustomAbilityAdd` / `CustomAbilityRemove`）。

出处：`BP_CardFunctions.cpp:5758`（Add）/ `:5874`（Remove），逐行读过。

原版模型（两个映射，别混）：
  * `receivedAbilities`（名字 → **计数**）：`HasCustomAbility(name) = receivedAbilities[name] > 0`
    ⇒ 这就是规则侧读的那个（`excess`/`lethal`/`cantRetreat`/`CantLoseShock` 全仓都是 `in u.ab` ✓）。
  * `receivedAbilitiesFromCards`（名字 → **giver**）：`CustomAbilityRemove(..., RemoveAllGivers)`
    按 giver 删，删到没有 giver 才真的没有这个能力。
    ⇒ 我们只建模**计数**（够 `HasCustomAbility` 用）；**giver 级账没建模**，由调用方如实记缺口 ✓。
"""
from __future__ import annotations


def custom_ability_add(unit, name) -> dict:
    """`CustomAbilityAdd(ability, cardID, giverID, skipBuffTexts, skipSubAction…)`（`:5758`）的值语义。

    ★ 签名里 `ability` 在**前**、`cardID` 在**后**（`:5758`），而且 `cardID` 是**卡 ID 不是指针**
      —— 与 `DamageCard` 的 `damagerCardID` 同一个坑（第八刀那次踩过）。
    门（`CanCardBeBuffed`，`:5769-5772`）不在本函数里：由 sink 判（门数据来自调用方 ✓）。
    返回 `{"changed", "present_before"}`（`present_before` 用来决定要不要广播"能力已变"）。
    """
    ab = unit.ab if isinstance(getattr(unit, "ab", None), dict) else {}
    cur = int(ab.get(name, 0))
    ab[str(name)] = cur + 1
    unit.ab = ab
    return {"changed": True, "present_before": cur > 0}


def custom_ability_remove(unit, name, remove_all: bool = False) -> dict:
    """`CustomAbilityRemove(ability, cardID, giverID, RemoveAllGivers, &qqq)`（`:5874`）的值语义。

    `RemoveAllGivers=false` ⇒ 原版删的是"这个 giver 那一份" ⇒ 计数 −1，**减到 0 才真的没有**这个能力 ✓
    （`RemoveAllGivers=true` ⇒ 直接清掉这一项）。
    返回 `{"changed", "present_before", "gone"}`。
    """
    ab = unit.ab if isinstance(getattr(unit, "ab", None), dict) else {}
    key = str(name)
    cur = int(ab.get(key, 0))
    if cur <= 0:
        return {"changed": False, "present_before": False, "gone": True}
    if remove_all or cur == 1:
        ab.pop(key, None)
    else:
        ab[key] = cur - 1
    unit.ab = ab
    return {"changed": True, "present_before": True, "gone": key not in ab}


# ---------------------------------------------------------------------------
# giver 级账（2026-10-07，ECHELON 一族）
# ---------------------------------------------------------------------------
def grant_ability(state, uid, name, giver) -> dict:
    """`CustomAbilityAdd(ability, cardID, giverID, …)` 的**完整**值语义：计数 +1（`receivedAbilities`）
    **并**记下 giver（`receivedAbilitiesFromCards`）→ `state.grants[(giver, name)]` = 被授予单位 id 集合。

    为什么 giver 级账要进状态：`HasCustomAbilityFromCard(name, giverID)` 问的是"**这个能力是不是这张卡给的**"
    ——ECHELON 授予 `"trigger"` 后，它自己的 `OnAfterOtherCardAttacks` 只对**它授予过**的单位生效；
    只有计数答不了（别的牌也可能授予同名能力）。三条路（字典路 `_apply_eff` / 直跑 sink / 重放 `apply_calls`）
    都经本函数 ⇒ 语义只有一处。`giver is None`（读不出）⇒ 只记计数、**不记 giver**（如实：问不了"谁给的"）。
    """
    u = (getattr(state, "units", {}) or {}).get(uid)
    if u is None:
        return {"changed": False, "giver_known": giver is not None}
    r = custom_ability_add(u, str(name))
    if giver is not None:
        g = getattr(state, "grants", None)
        if g is None:
            state.grants = g = {}
        g.setdefault((giver, str(name)), set()).add(uid)
    r["giver_known"] = giver is not None
    return r


def has_grant(state, uid, name, giver) -> bool:
    """`HasCustomAbilityFromCard(name, giverID)`：单位 `uid` 身上有没有 `giver` 授予的 `name`。"""
    return uid in ((getattr(state, "grants", None) or {}).get((giver, str(name))) or ())
