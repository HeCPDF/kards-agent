# -*- coding: utf-8 -*-
"""engine.natives.status —— 状态类原生函数：压制（`SuppressMultipleUnits`）。

从 `sim/effects.py` 原样搬来（行为一字不改），`sim` 侧保留旧名转调；只认鸭子类型，不 import `sim`。
"""
from __future__ import annotations

# ---- 压制（`SuppressMultipleUnits`，本构建 BP 导出 @7456）----
# 原版：BP_CardFunctions::SuppressMultipleUnits（BP_CardFunctions.cpp:7456，2026-10-03 读过）
# "新压制"分支（Label_1358）逐行读过：RemoveGuard/Fury/Blitz/Immune/Alpine/Ambush/
#   Mobilize/Smokescreen/Salvage/Shock、`ChangeHeavyArmor(...,0,...)`、hasDestruction=false、
#   exileNation=0、移除夹击、`customName1/2 = "None"`、`KreditsTax_AsEnemyTarget = 0`、
#   effectType=0，只把 customJson 里的 suppressionException / veteran 保留下来。
# ⇒ 我们**建模到 sim 的 U 上有的那些字段**（关键词、重甲、指向税、压制位），
#   customName1/2、effectType、customJson 的保留语义没有建模 —— 如实记缺口，不编造。
SUPPRESS_STRIP = ("guard", "fury", "blitz", "immune", "alpine", "ambush",
                  "mobilize", "smokescreen", "salvage", "shock")
SUPPRESS_GAP = ("sim.suppress：压制会清空关键词/重甲/指向税（已建模）；customName1/2→None、"
                "effectType=0、customJson 只留 suppressionException/veteran **未建模**（缺口）")


def apply_suppress(state, uid) -> list:
    """压制一张单位（`SuppressMultipleUnits` 的"新压制"效果）。

    只改状态里存在的字段：关键词剥掉 `SUPPRESS_STRIP`、重甲清零、指向税清零、
    置压制位、不能再攻击。返回缺口清单（有一条：没建模的那部分）。目标不在场上 ⇒ 记一条缺口、不动状态
    （BP 里 `IsLocatedOnBoard` 为假就直接跳过）。
    """
    u = state.units.get(uid)
    if u is None:
        return ["sim.suppress：%s 不在场上（未结算）" % uid]
    u.kw = {k for k in u.kw if k not in SUPPRESS_STRIP}
    u.armor = 0
    u.tax = 0
    u.pinned = True
    # 压制把"本回合还能不能动"也一并处理掉（BP 里 isSuppressed 一置位，CanAct 就走不了）
    u.attacks_left = 0
    return [SUPPRESS_GAP]

