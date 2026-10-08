# -*- coding: utf-8 -*-
"""engine.natives.combat_kw —— `GiveRandomCombatKeyword` 的**候选集合**（直跑 sink / 字典路录制钩子 / 影子对账枚举共用的单一来源）。

原版：`BP_CardFunctions::GiveRandomCombatKeyword(int cardID, int instigatorID, ECombatKeyword& keywordGiven, bool& success)`
（1.58 导出 `BP_CardFunctions.cpp:22683-22930`，逐行读过；FModel 反编译控制流有损，下面按 label 还原）：

  1. `GetCardFromID(cardID)` 无效 或 `CanCardBeBuffed` 为假 ⇒ `keywordGiven=0; success=false; return`（`:22688-22695`、`Label_959`）；
  2. 对 `ECombatKeyword` 的 8 个枚举值 i=0..7（`Label_211` 循环，`:22700-22735`）：值 `v` 不进候选当且仅当
        `v == 0(None)`  或  `v == 7(Smokescreen) ∧ (卡在前线(location==0x7) ∨ IsFighter ∨ getHasGuard)`
     （`BooleanOR_2 = (BooleanAND ∨ v==0)`，真 ⇒ `Label_1931` 跳过；假 ⇒ `Label_2005` `Set_Add(validKeywords, v)`）；
     所以候选 = {1 Ambush, 2 Blitz, 3 Fury, 4 Guard, 5 HeavyArmor, 6 Shock, 7 Smokescreen}，7 视位置/兵种剔除；
  3. `CardToGive->GetCombatKeywords(&keywords)`（卡**现有**的战斗关键词，含 `getTotalHeavyArmor > 0` 的重甲）⇒
     `Set_RemoveItems(validKeywords, keywords)`（`Label_995`，`:22762-22764`）；剩 0 个 ⇒ `success=false; return`（`Label_1895`）；
  4. `Set_ToArray(validKeywords)`（按枚举序：集合是按 1..7 顺序 `Set_Add` 的，`RemoveItems` 只留空槽）⇒
     `RandomIntFromRangeWithStream(0, 长度-1)`（**牌局随机流** `cardsRandomStream`）⇒ 取该下标的关键词（`:22775-22785`）；
  5. 关键词 → 动作（`:22787-22830`）：1 `GiveAmbush`、2 `GiveBlitz`、3 `GiveFury`、4 `GiveGuard`、
     5 `ChangeHeavyArmor(card, instigator, +1, permBuff(0x1), false)`、6 `GiveShock`、7 `GiveSmokescreen`。

字典路旧口径"7 个关键词均匀选 1 个"与它不是同一个分布：不剔除已有关键词、不看位置/兵种/守护、也不看 CanCardBeBuffed。
"""
from __future__ import annotations

from typing import Iterable, List

__all__ = ["COMBAT_KEYWORD_ORDER", "valid_combat_keywords", "existing_combat_keywords"]

#: `ECombatKeyword` 的 1..7（0 = None 恒被剔除），**枚举序**——候选数组的顺序就是它（见上面第 4 步）。
COMBAT_KEYWORD_ORDER = ("ambush", "blitz", "fury", "guard", "heavyarmor", "shock", "smokescreen")


def existing_combat_keywords(kw: Iterable[str], armor: int) -> set:
    """卡现有的战斗关键词（`GetCombatKeywords`：6 个关键词旗 + `getTotalHeavyArmor > 0` ⇒ heavyarmor）。"""
    ks = set(kw or ()) & set(COMBAT_KEYWORD_ORDER)
    ks.discard("heavyarmor")
    if int(armor or 0) > 0:
        ks.add("heavyarmor")
    return ks


def valid_combat_keywords(existing: set, *, in_frontline: bool, is_fighter: bool, has_guard: bool) -> List[str]:
    """候选关键词（枚举序）；空 ⇒ 原版 `success=false`、什么都不给。"""
    out = []
    for k in COMBAT_KEYWORD_ORDER:
        if k == "smokescreen" and (in_frontline or is_fighter or has_guard):
            continue
        if k in existing:
            continue
        out.append(k)
    return out
