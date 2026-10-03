#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.effects —— 写侧规则（"某个动词到底改了什么状态"）的 L2 实现。

规矩（`EVAL-ARCHITECTURE.md` §3.1/§3.3）：
  * 规则只写在这里（或 `sim/` 的其它模块），**评估侧不许再实现一遍**；
  * 每条规则的出处写在函数上（报告章节 + IDA 地址/未读就如实写"未读"）；
  * 语义没证据 ⇒ 只记缺口，**不动状态**（§3.3 缺口协议）。
"""
from __future__ import annotations

from .state import H

FIGHT_GAP = ("sim.fight：MakeCardsFight 已按 BP 建模（互相受对方 atk、无重甲、受击方免疫⇒0）；"
             "改伤钩子仅在预计算 `fight_dmg` 时才算（否则按裸 atk）；ApplyDamageToCard 的受伤钩子/excess 未建模")

# ---- 压制（`SuppressMultipleUnits`，本构建 BP 导出 @7456）----
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


def apply_fight(state, a_id, b_id, *, deal_damage, kill=None, damages=None) -> list:
    """`MakeCardsFight(unitThisSide, unitOppositeSide, instigatorID)`（BP_CardFunctions@9755，2026-10-03 读过）。

    BP 是一个 Sequence：① 对方在场 ⇒ 算 己方→对方 伤害；② 己方在场 ⇒ 算 对方→己方 伤害；③ 两边 `ApplyDamageToCard`。
    （FModel 反编译把 Sequence 拆成了互相 goto 的片段，看起来像"只打一边"，实为两边都打。）
    每个方向的伤害 = `ExecuteOnDealDamageAddDamage`（改伤钩子）→ `ExecuteOnDealDamageAddDamageAfterCalc`
    （**受击方免疫 ⇒ 0**，否则攻击方 `OnDealDamageAddDamageAfterCalc` 与 0x26 钩子再改）。**没有重甲扣减**
    （重甲在 `CalculateDamageDealt`，这里不经过它）。两边的伤害先一起算出来再打（一方死了也不改变另一方该受多少）。
    `deal_damage(state, unit, dmg)` 与 `kill` 由调用方注入：现在分别是 `boardeval._dmg_unit` / `boardeval._apply_death`。
    """
    a, b = state.units.get(a_id), state.units.get(b_id)
    if a is None or b is None:
        return ["sim.fight：%s/%s 不在场上，未结算" % (a_id, b_id)]
    if damages is not None:                       # 已过改伤钩子（triggers.fight_damage 预计算）
        to_b, to_a = max(int(damages[0]), 0), max(int(damages[1]), 0)
    else:
        to_b = 0 if "immune" in b.kw else max(int(a.atk), 0)
        to_a = 0 if "immune" in a.kw else max(int(b.atk), 0)
    if to_b:
        deal_damage(state, b, to_b)
    if to_a:
        deal_damage(state, a, to_a)
    return [FIGHT_GAP]


def apply_suppress(state, uid) -> list:
    """压制一张单位（`SuppressMultipleUnits` 的"新压制"效果）。

    只改 sim 的 U 里存在的字段：关键词剥掉 `SUPPRESS_STRIP`、重甲清零、指向税清零、
    置压制位。返回缺口清单（有一条：没建模的那部分）。目标不在场上 ⇒ 记一条缺口、不动状态
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


def apply_salvage(state, cid, *, hand_cap: int = 9) -> bool:
    """`SalvageMultipleUnits(cardsToSalvage[], instigatorID, …)` 的**一张**（收缴）。

    BP：`IsLocationFull(GetHandLocationBySide(instigator 的 side))` 满 ⇒ 跳过；否则
    `CreateCard(..., handLocation, spawnCardInHand=true, cardSeen=true, isSalvaged=true)` ——
    `ApplySalvageChanges` 把（非老兵的）复制改成 **attack=1 / defense=1 / kredits=min(kredits,3)**，
    卡的蓝图逻辑（效果/关键词）原样保留。

    ★ 2026-10-02 从 `boardeval._salvage_one` 原样迁进 sim/（§5 步骤 3 规则下沉）；
      顺手把 `hand_cap`（**手牌上限是规则、但那个常数原先取自评估权重表 `W`**）改成参数，
      由调用方传入 —— sim 不 import 权重（§3.1 依赖方向）。返回是否真的创建了复制。
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


def deal_damage(state, unit, dmg, *, engage: bool = False, on_death=None) -> None:
    """**单位受伤/阵亡**的基础原语（战斗伤害与效果伤害共用）。

    语义（原 `boardeval._dmg_unit`，一字不改）：
      * `engage=True`（**战斗**伤害）⇒ 先扣重甲：`dmg = max(dmg - armor, 0)`；
      * 防御 `-= max(dmg, 0)`；`dfn <= 0` ⇒ **立即离场**，并回调 `on_death(state, id)`
        （死亡的触发链 —— `OnDestroyed`(0x27)/0x18/打捞等 —— 由调用方注入，因为那一段
        还要跑 L1 钩子；sim 只负责"谁掉血、谁离场"这条状态变更）。

    ★ 2026-10-02 从 `boardeval._dmg_unit` 迁进 sim/（§5 步骤 3 规则下沉）：伤害/阵亡是
      **写侧原语**，按架构该落在模拟侧；`boardeval._dmg_unit` 保留旧名转调。
    """
    if engage:
        dmg = max(dmg - getattr(unit, "armor", 0), 0)
    unit.dfn -= max(dmg, 0)
    if unit.dfn <= 0:
        state.units.pop(unit.id, None)
        if on_death is not None:
            on_death(state, unit.id)
