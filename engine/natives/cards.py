# -*- coding: utf-8 -*-
"""engine.natives.cards —— 卡牌一族的原生移植：收缴（`SalvageMultipleUnits` 的一张）。

原版：BP_CardFunctions::SalvageMultipleUnits / CreateCard（`IsLocationFull` 满则跳过；
      `ApplySalvageChanges` 把非老兵复制改成 1/1、费用 min(kredits,3)）。
从 `sim/effects.py` 原样端口（P2，2026-10-03）；只认鸭子类型，不 import `sim`。
"""
from __future__ import annotations

from engine.state import H


def apply_salvage(state, cid, *, hand_cap: int = 9) -> bool:
    """`SalvageMultipleUnits(cardsToSalvage[], instigatorID, …)` 的**一张**（收缴）。

    BP：`IsLocationFull(GetHandLocationBySide(instigator 的 side))` 满 ⇒ 跳过；否则
    `CreateCard(..., handLocation, spawnCardInHand=true, cardSeen=true, isSalvaged=true)` ——
    `ApplySalvageChanges` 把（非老兵的）复制改成 **attack=1 / defense=1 / kredits=min(kredits,3)**，
    卡的蓝图逻辑（效果/关键词）原样保留。

    ★ 2026-10-02 从 `boardeval._salvage_one` 原样迁进 sim/（§5 步骤 3 规则下沉）；
      顺手把 `hand_cap`（**手牌上限是规则、但那个常数原先取自评估权重表 `W`**）改成参数，
      由调用方传入 —— 不 import 权重（§3.1 依赖方向）。返回是否真的创建了复制。
    """
    if len(state.hand) >= hand_cap:
        return False                               # 手牌满：游戏里不创建（BP 的 IsLocationFull 分支）
    h = state.card_templates.get(cid)
    if h is None:
        # 认不出模板（罕见）：按 1/1 单位估，别当作普通指令牌（收缴目标一定是单位）
        h = H(cid, "?", 1, "infantry", 1, 1)
    else:
        h = H(h.id, h.name, min(int(h.cost or 0), 3), h.typ, 1, 1, h.kw, h.eff)
    state.cap_used += 1
    state.hand[-(1000 + state.cap_used)] = h
    return True

