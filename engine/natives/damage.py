# -*- coding: utf-8 -*-
"""engine.natives.damage —— 伤害/阵亡一族：受伤原语、疲劳、MakeCardsFight。

从 `sim/effects.py`、`sim/chain.py` **原样搬来**（行为一字不改），`sim` 侧保留旧名转调。
这些函数只认鸭子类型（`state.units` / `state.hq` / `state.fatigue` / `state.me`），不 import `sim`。

缺口文本里的 `sim.*` 前缀**先保留**（日志与测试按它匹配），等 `sim` 退役再统一改前缀。

出处：原版：BP_CardFunctions::ApplyFatigueDamage / MakeCardsFight（:9755）；`deal_damage` 是
     `boardeval._dmg_unit` 的原样下沉（战斗/效果伤害共用的"扣防 + 阵亡回调"原语）。
"""
from __future__ import annotations

FIGHT_GAP = ("sim.fight：MakeCardsFight 已按 BP 建模（互相受对方 atk、无重甲、受击方免疫⇒0）；"
             "改伤钩子仅在预计算 `fight_dmg` 时才算（否则按裸 atk）；ApplyDamageToCard 的受伤钩子/excess 未建模")


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
    if to_a and state.units.get(a_id) is a:      # 第一击已把 a 打离场（只有 a is b 的退化对打会发生）⇒ 不再打一个已不在场的单位（否则死亡链会扇两遍）
        deal_damage(state, a, to_a)
    return [FIGHT_GAP]


def apply_fatigue(state) -> None:
    """`BP_CardFunctions::ApplyFatigueDamage` 的一次结算（空库抽牌 / 协力未满足）：
    伤害 = **当前**疲劳计数（打在**己方**总部上），然后计数 +1（BP 里 increment 在伤害之后）。

    ★ 2026-10-02 从 `boardeval._fatigue_once` 原样搬进 sim/（规则下沉；boardeval 转调，行为不变，
    由 `test_draw_chain.py` 钉住）；P2 再端口进 engine（`sim.chain.apply_fatigue` 转调）。
    """
    dmg = int(state.fatigue)
    state.fatigue += 1
    if dmg > 0:
        state.hq[state.me] = state.hq.get(state.me, 0) - dmg


def deal_damage(state, unit, dmg, *, engage: bool = False, on_death=None) -> None:
    """**单位受伤/阵亡**的基础原语（战斗伤害与效果伤害共用）。

    语义（原 `boardeval._dmg_unit`，一字不改）：
      * `engage=True`（**战斗**伤害）⇒ 先扣重甲：`dmg = max(dmg - armor, 0)`；
      * 防御 `-= max(dmg, 0)`；`dfn <= 0` ⇒ **立即离场**，并回调 `on_death(state, id)`
        （死亡的触发链 —— `OnDestroyed`(0x27)/0x18/打捞等 —— 由调用方注入，因为那一段
        还要跑 L1 钩子；这里只负责"谁掉血、谁离场"这条状态变更）。
      * ★ 2026-10-04（P3 ⑦，**免疫单位不吃伤害** —— 用户当场纠正）：`unit.immune` 为真 ⇒
        **直接返回**（伤害 0、不阵亡）。原版三处一致：`CalculateDamageDealt`(`:14823-14836`)
        开头就是 `_damageRecieverCard->getHasImmune()` ⇒ `damage = 0;
        doesDamageRecieverDie = false; wasShockAttack = false; return`；
        `ExecuteOnDealDamageAddDamageAfterCalc`(`:14568-14575`) ⇒ `finalDamage = 0`；
        群体伤害(`:15698-15703`) 干脆**跳过**免疫目标（连 0x26 触发都不走）。
        —— 我先前只把 `isImmune` 当成"伏击门的一个条件"用，是**读漏了这三处**。

    ★ 2026-10-02 从 `boardeval._dmg_unit` 迁进 sim/（§5 步骤 3 规则下沉）；P2 端口进 engine。
    """
    if getattr(unit, "immune", False):
        return
    if engage:
        dmg = max(dmg - getattr(unit, "armor", 0), 0)
    unit.dfn -= max(dmg, 0)
    if unit.dfn <= 0:
        state.units.pop(unit.id, None)
        if on_death is not None:
            on_death(state, unit.id)


def excess_split(state, dealer, target, dmg):
    """原版 **`excess`** 自定义能力（`ExecuteAttackCard` `:17218-17244`；2026-10-04 只读反汇编定案）。

    ★ 2026-10-04（P4 第六刀）：**从 `sim/engine.py` 下沉到 engine**（规则下沉，§5 步骤 3）——
    实现一字未改；`sim` 侧保留 `_excess_split` 这个名字转调（调用点不用改）。

    攻击者带 `excess` ∧ 防守方是**单位** ∧ 伤害 > 防守方总防 ⇒
      * 打防守方的伤害**封顶到它的总防**（`finalDamageToDefender = getTotalDefense()`）；
      * 溢出的那份打到**攻击者对面那一方的总部**（`ApplyDamageToCard` `:16489-16498` 的
        `DamageCard(GetLocationCardBySide(对面), ExcessDamage, 来源, false, false, false)`）。

    ★ 反编译的坑（弯路 #4）：FModel 导出里 excess 拆分之后写的是 `goto Label_3496`，
      而那个 label 是"摧毁/终局"段，看起来**跳过了伤害应用**。只读反汇编
      （`probe_excess_asm.py`，`ExecuteAttackCard` 623 行）证明它其实**直接落下**
      到 `provideKeysAndFrameCount → getTotalDefense → Subtract_IntInt → setAndEncryptDefense`
      ⇒ 伤害照常落在防守方身上。这里就是照那个真实控制流实现的。
    ★ 局限（如实记）：溢出的那份走**总部直减**（与 `sim` 其它总部伤害同一口径），
      没有跑 `DamageCard` 的钩子链（对总部的 `OnReceiveDamage` 一类不建模）。
    ★ 免疫单位不吃伤害（`CalculateDamageDealt` `:14823-14836`）⇒ 直接原样返回、不做溢出 ✓。

    返回**真正该打在防守方身上**的伤害（没触发 excess 时原样返回）。
    """
    if dmg is None or target is None or getattr(target, "immune", False) \
            or "excess" not in getattr(dealer, "ab", ()):
        return dmg
    d = max(float(dmg), 0.0)
    dfn = float(getattr(target, "dfn", 0) or 0)
    if d <= dfn:
        return dmg
    other = state.opp if dealer.side == state.me else state.me
    state.hq[other] = state.hq.get(other, 0) - (d - dfn)          # 溢出 → 对面总部
    return dfn

