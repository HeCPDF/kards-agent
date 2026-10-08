#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.state —— 无头游戏的状态（纯数据 + 廉价拷贝）。

2026-10-03 P2：从 `sim/state.py` **原样端口**（以前是 `policy.boardeval` 的转调壳）：`U`（单位）、
`H`（手牌）、`Sim`（一局盘面）与常量。没有任何规则、没有任何价值判断；规则在 `engine/natives/`
（逐步端口）与 `sim/engine.py`（未迁部分），价值在 `evaluation/value.py`，搜索在 `policy/search.py`。
`State` 保留为 `Sim` 的子类（`copy()` 返回 `State`），供 `sim.simulate` 等使用。
`sim/state.py` 现在只是 re-export 本模块的兼容壳，旧 import 不变。
"""
from __future__ import annotations

from typing import Iterable, Optional

from kardsmem.gamemodel import ESide, other_side

# P2：容量常量只有一份（`kardsmem/gamemodel.py` 的 `FetchCardsByLocation` 容量），经 `engine.natives.board`
# re-export；这里保持旧名字给尚未迁移的调用方（`sim.engine` / `policy.search` / 测试）。
from engine.natives.board import (FRONTLINE_CAP, FRONTLINE_CAP_LIMITED, HAND_CAP,  # noqa: E402,F401
                                  SUPPORT_UNITS_CAP)

GROUND = frozenset(("infantry", "tank"))
NO_RETALIATION = frozenset(("artillery", "bomber"))
UNIT_TYPES = frozenset(("infantry", "tank", "artillery", "fighter", "bomber"))



# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
# ★ 每排的容量——原版 `BP_GameState_Battle::FetchCardsByLocation`（BP_GameState_Battle.cpp:1567，1.60 导出）：
#   手牌 9；**支援线 5 张（含总部卡本身 ⇒ 单位最多 4）**；前线 5（`IsFrontlineLimited()` 为真时 2；
#   本模拟的 `Sim.front_limited` 默认 False，rule 读得到再设）。`sim` 的 `units` 不含总部。
#   这四个常量本身已收进 `engine.natives.board`（P2，单一来源 = `kardsmem/gamemodel.py`），上面 import 是 re-export。
DRAW_CAP = 50        # 一次动作里最多模拟多少次抽牌（抵抗那种"连抽几十张"的链，防跑飞）——沙箱上限，非原版常量

# 预计算钩子后果表（`Sim.event_fx`）里允许的事件种类。加一族 ⇒ 在这里登记 + 在
# `triggers` 里有对应的 runner + 有消费点（现在是 move 的 `sim_move` 与 draw 的 `draw_chain`）。
EVENT_FX_KINDS = ("move", "draw", "suppress", "pin", "reveal", "retreat", "move_front",
                  "turn_end", "turn_start", "veteran", "heal", "steal",
                  "convert", "damage", "abilities", "armed_spec", "armed")
# 最后两种（2026-10-07，延迟/常驻效果登记表，见 `engine/deferred.py`）：
#   "armed_spec" `{手牌 id: {"hooks": (钩子名…), "grants": ((能力名, 单位 id)…)}}`（rule 按牌的 `On*` 覆写给；sim 在打出时据此布防）
#   "armed"      `{(来源牌 id, 钩子名, 事件主体 id|None): 钩子后果桶}`——桶形状同 `attack_fx` 的桶（整份效果摘要，`_fx_apply` 直接吃）；
#                由 rule 用 VM 把**留下来的钩子**对每个候选事件主体空跑得到（VM 当事件的权威，sim 不手写任何一张牌的规则）。
# 后三种（A5，2026-10-03）的键和值**不是** `{卡 id: eff}`：
#   "convert"   键 `(instigator_id, tuple(旧牌 id), 目标卡名)` → `{"old": {旧牌 id: eff}, "new": eff, "after": eff, "gaps": [...]}`
#               （`triggers.convert_fx`；原版 ConvertCard，BP_CardFunctions.cpp:12611）
#   "damage"    键 `(伤害来源牌 id, 目标 id)`（效果伤害，`triggers.damage_card_fx`）或 `("fight", a_id, b_id)`（MakeCardsFight，
#               `triggers.fight_damage_fx`）→ `{"final"/"to_b"/"to_a", "amount", "buckets": {桶: eff}, "gaps": [...]}`
#               （原版 DamageCard:895 → ApplyDamageToCard:16375）
#   "abilities" 键 `(单位 id, "+关键词" | "-关键词")` → eff（`triggers.abilities_changed_effects`，钩子读的是改变**之后**的关键词视图）；
#               特殊键 `("gap",)` → `[缺口文本]`（预算用尽 / 读不出视图）。（原版 ExecuteOnOtherCardsAbilitiesChanged:20885）
# 原版：ExecuteOnOtherCardsAbilitiesChanged 的调用点里，关键词的赋予/移除只有这几个会广播（BP_CardFunctions.cpp，
#   GiveSmokescreen:1860/GiveGuard:1970/GiveFury:2080/GiveBlitz:2198/GiveAmbush:2298/GiveMobilize:3501/GiveShock:9906 与对应的 Remove*）；
#   immune/alpine/salvage 的赋予/移除不在其中 ⇒ 不广播。
ABILITIES_KEYWORDS = ("smokescreen", "guard", "fury", "blitz", "ambush", "mobilize", "shock")


def _ab_counts(ab) -> dict:
    """把能力表归一成**计数映射** `{能力名: 次数}`（第十二刀，2026-10-04）。

    为什么从 `frozenset` 改成计数映射：原版 `HasCustomAbility(name) = receivedAbilities[name] > 0`
    （**计数**，不是布尔）—— `CustomAbilityAdd`/`CustomAbilityRemove` 按 giver 增减，减到 0 才"没有这个能力"。
    而消费端（`engine`/`sim`/`policy` 全仓）读的全是 `"excess" in u.ab` 这类**成员判断**
    ⇒ 计数映射的 `in` 语义与原来**完全一致**，但多出"减到 0 才消失"这个原版语义（`frozenset` 表达不了 ✗）。
    入参可以是映射（适配器/复制）也可以是可迭代的名字集合（测试/旧调用）。
    """
    if isinstance(ab, dict):
        return {k: int(v) for k, v in ab.items() if int(v) > 0}
    return {k: 1 for k in (ab or ())}



class U:
    """模拟里的一个单位（可变；靠 copy 复制）。"""
    __slots__ = ("id", "side", "row", "atk", "dfn", "cost", "typ", "kw", "acted", "opc",
                 "sick", "attacks_left", "pinned", "moved", "armor", "guarded", "aa_hq", "tax",
                 "mdef", "atk_turn", "atk_buff", "ab", "rng", "been_attacked", "immune",
                 "opc_buff", "armor_buff", "faction")

    def __init__(self, id, side, row, atk, dfn, cost, typ, kw=(), acted=False, opc=1,
                 sick=False, attacks_left=None, pinned=False, moved=False, armor=0,
                 guarded=False, aa_hq=None, tax=0, mdef=None, atk_buff=0, ab=(), rng=None,
                 been_attacked=False, immune=False, opc_buff=0, armor_buff=0, faction=None):
        self.id, self.side, self.row = id, side, row
        # 国家（原版 `BaseCardObject.faction`，`EFactionEnum` 的整数值；读不出 = None）。
        # 唯一消费者：`engine.natives.bond.set_active_bonds_at_start_of_turn`（`activeBondFactions` 的来源）。
        self.faction = None if faction is None else int(faction)
        self.atk, self.dfn, self.cost, self.typ = atk, dfn, cost, typ
        self.kw = frozenset(kw)
        self.opc, self.sick, self.pinned, self.moved = opc, sick, pinned, moved
        self.armor, self.guarded = armor, guarded
        # ★ P3 第一族（R3/R10，2026-10-03）：原版的攻是**两个字段** —— `attack`（基础值）
        #   与 `attackBuff`（临时/永久 buff 的累加器，`ChangeAttack` 的 0/4 分支写它、
        #   且**不夹取**，`:11128-11149` / `:11185-11208`）。`atk` 仍然是我们唯一被读的**总量**
        #   （= 原版 `getTotalAttack()`），`atk_buff` 单独记原版那个累加器：
        #   不变量 = `atk == clamp(基础值 + atk_buff, 0, 99)`。
        #   为什么必须分开：原版 `SetValue` 只写 `attack` 字段 —— 5 攻单位吃过 `+50` tempBuff 后
        #   （buff=50、总攻 55）再被 `MONSOON RAIN` 式"设为 2"，原版总攻是 `clamp(2+50)=52`，
        #   而我们若只有一个总量就会抹成 2（把 50 点 buff 丢了）。
        self.atk_buff = int(atk_buff or 0)
        # ★ P3 ③（2026-10-04）：**运行时自定义能力**（原版 `receivedAbilitiesFromCards` @0x208，
        #   `CustomAbilityAdd/Remove` 写它；`kardsmem/cards.py:292` 读它）。
        #   目前只有一处消费者 —— `excess`（原版 `ExecuteAttackCard` `:17218`，只读反汇编定案：
        #   攻击者带 `excess` 时，**超过目标总防**的那部分伤害转打敌方总部）。
        #   与 `kw`（关键词）分开存：能力不是关键词。
        self.ab = _ab_counts(ab)
        # ★ P3 ⑥（2026-10-04）：`range`（原版 `BaseCardObject::range @0x78`，SDK
        #   `kards_classes.hpp:2439`）。判据在 `cardsCheckFunctions::CanAttack`（`:185-204`）：
        #   **攻击者不在前线 ∧ 目标不在前线 ∧ range < 2 ⇒ `not_enough_range`（打不了）**。
        #   `None` = 读不到（老 fixture）⇒ sim 退回旧的"兵种百科"近似（见 `sim._rows_ok`）；
        #   实机快照**总是**带这个字段（`board._read_card` 从 0x78 读）。
        self.rng = None if rng is None else int(rng)
        # ★ P3 ⑦（2026-10-04）：`hasBeenAttackedThisTurn`（快照 `@0x280`）—— 原版**伏击**的
        #   必要条件之一（`CalculateDamageDealt` `:14881-14887`：`¬receiver.hasBeenAttackedThisTurn`
        #   才走伏击），也用于"每回合首次被攻击"一类触发。
        self.been_attacked = bool(been_attacked)
        # ★ P3 ⑦：`isImmune @0x289`（SDK `kards_classes.hpp:2514`）—— 原版伏击的必要条件之一
        #   （`CalculateDamageDealt` `:14877`：`dealer.getHasImmune()` 为真 ⇒ 不走伏击）。
        self.immune = bool(immune)
        # ★ P4 第三刀（2026-10-04）：**两字段模型补齐行动费/重甲的 buff 字段**（P3 R3 的残项）。
        #   原版 `operationCost`/`operationCostBuff`（`:8892+`，0/4 写 buff、1 与 2/3/5 写基础）
        #   与 `heavyArmor`/`heavyArmorBuff`（`:10320+`，同形）都是**两个字段**；`U.opc`/`U.armor`
        #   存的是**总量**（适配器读 `getTotalOperationCost()`/`getTotalHeavyArmor()` ✓）。
        #   直跑要算"基础 = 总量 − buff" ⇒ 这两个字段必须有；P3 时用"只发总量增量"绕过了，
        #   现在直跑要按原版写**基础值**，绕不过去了。
        self.opc_buff = int(opc_buff or 0)
        self.armor_buff = int(armor_buff or 0)
        # 「攻击结算后、防守方是总部」触发的效果（OnAfterAttack：如 DINGO 抽 1 + 己方总部 +1），
        # 攻击力为 0 也照样触发 ⇒ 0 攻单位打总部也可能有收益
        self.aa_hq = aa_hq
        # acted 沿用旧接口：True ⇒ 本回合已不能再动
        self.attacks_left = (2 if "fury" in self.kw else 1) if attacks_left is None else attacks_left
        self.acted = acted
        # `KreditsTax_AsEnemyTarget`：**敌方**指向这张牌时多付的指挥点（AddKreditsTax）。
        self.tax = int(tax or 0)
        # 满血线（`maxDefense`）：`FullyHealCard` 用 `t.dfn = t.mdef`。
        self.mdef = float(mdef if mdef is not None else max(dfn or 0, 0))
        # ★ 2026-10-02（用户："热浪 = 本回合 +1 攻 / -1 行动费，直到回合结束；脚本留到结束回合前
        #   最后打出＝白给"）：`AddAttackUntilEndOfTurn`（effectvm 的 `attack_turn`）是**本回合
        #   临时**攻击力。模拟里照旧加到 `atk` 上（这样本回合的攻击真的能吃到它），但**单独记一份**
        #   `atk_turn`，让评估侧知道"这部分到回合结束会消失"（§8-4：贴膜无后续攻击 ⇒ 记 0）。
        self.atk_turn = 0
        # ★ 2026-10-02：`_atk_turn`? 见上；`atk_turn` 之外没有别的"回合结束会消失"的量。
        if acted:
            self.attacks_left = 0

    def copy(self) -> "U":
        c = U(self.id, self.side, self.row, self.atk, self.dfn, self.cost, self.typ,
              self.kw, self.acted, self.opc, self.sick, self.attacks_left, self.pinned,
              self.moved, self.armor, self.guarded, self.aa_hq, self.tax, self.mdef,
              self.atk_buff, self.ab, self.rng, self.been_attacked, self.immune,
              self.opc_buff, self.armor_buff, self.faction)
        c.atk_turn = self.atk_turn
        return c

    def can_act(self) -> bool:
        return (not self.sick or "blitz" in self.kw) and not self.pinned and self.attacks_left > 0


class H:
    """手牌里的一张牌（模拟用）。`eff` 见下方 EFFECT KEYS。"""
    __slots__ = ("id", "name", "cost", "typ", "atk", "dfn", "kw", "eff", "cost_buff",
                 "faction", "bond", "bond_removed", "opc", "opc_buff")

    def __init__(self, id, name, cost, typ, atk=0, dfn=0, kw=(), eff=None, cost_buff=0,
                 faction=None, bond=False, bond_removed=False, opc=None, opc_buff=0):
        self.id, self.name, self.cost, self.typ = id, name, cost, typ
        self.atk, self.dfn, self.kw, self.eff = atk, dfn, frozenset(kw), dict(eff or {})
        # ★ P3 第一族（R3）：手牌的费用也是两个字段（`kredit` + `kreditBuff`，原版
        #   `getTotalKreditCost = clamp(kredit + kreditBuff, 0, 99)`，IDA 0x144B15020）。
        #   `cost` 仍是总量；`cost_buff` 单独记那个累加器（`set_kredit_buff` 写它）。
        self.cost_buff = int(cost_buff or 0)
        # ★ 协力（Bond，2026-10-06）：见 `engine/natives/bond.py`。
        #   `faction`  = `BaseCardObject.faction`（`EFactionEnum` 整数值；读不出 None）；
        #   `bond`     = 牌带 `ability.bond` 标签（自带或 `GiveBond` 给的）；
        #   `bond_removed` = 自定义能力 `"bond_removed"` 在身上（`RemoveBond` 写的，`GiveBond` 清的）。
        #   原版 `HasBond = bond ∧ ¬bond_removed`（`engine.natives.bond.has_bond`）。
        self.faction = None if faction is None else int(faction)
        self.bond = bool(bond)
        self.bond_removed = bool(bond_removed)
        # 手牌的**行动费**（`getTotalOperationCost`）：手牌快照不带它（`None` = 没被脚本改过 ⇒ 部署时按默认），
        # 只有脚本对手牌调 `ChangeOperationCost`（IRON VICTORY：新补的 T-34 行动费设为 1）才写；部署时落到 `U.opc`。
        self.opc = None if opc is None else int(opc)
        self.opc_buff = int(opc_buff or 0)

    def copy(self) -> "H":
        """浅拷贝（`kw` 不可变、`eff` 复制一层）—— 写手牌字段的原生（`remove_bond`…）**必须先拷再写**：
        `Sim.copy()` 只复制 `hand` 字典、不复制 `H`，原地改会泄漏到兄弟分支。"""
        return H(self.id, self.name, self.cost, self.typ, self.atk, self.dfn, self.kw, self.eff,
                 self.cost_buff, self.faction, self.bond, self.bond_removed, self.opc, self.opc_buff)

    def is_unit(self) -> bool:
        return self.typ in UNIT_TYPES


# EFFECT KEYS（都是可选的；缺省 = 不知道/没有）：
#   target   "enemy" | "friend" | "any" | None      需要选目标时的目标阵营
#   damage   n      对目标单位造成 n 点伤害        damage_hq  n  对目标总部
#   destroy  True   消灭目标单位                     retreat    True  目标单位撤退回手牌
#   pin      True   压制目标                         unpin      True  解除压制
#   give     [kw]   给目标友方单位关键词             buff   (a, d)   给目标 +a/+d
#   heal_hq  n      我方总部回复                     heal_opp_hq  n  敌方总部加防（`ChangeDefense(敌方总部)`，可负）
#   kredit   n      立刻 +n 指挥点（本回合有效）     kredit_next  n  下一友方回合开始时的变化量（可负）
#   slot     n      指挥点槽变化                     draw   n   抽 n 张
#   spawn    n      战场上加 n 个（估值用）          gain_cards  n  往手牌里加 n 张（估值用）


# ATTACK FX（`engine/triggers.py::to_fx` 产出，`Sim.attack_fx[(攻击者id, 目标id|"hq")]`）：
#   一次攻击上**所有钩子**（0x1E 换目标 / 0x1F 吞攻击 / OnBefore* / 伤害管线 / 受击·幸存·摧毁链 /
#   OnAfterAttack·0x04 / 花费通知）的后果，由外部 VM 把钩子体空跑出来。字段：
#     stop        True ⇒ 攻击被吞(StopAttack)：不付指挥点、不耗行动、什么都不发生（搜索里也不再生成这一步）
#     consumed    True ⇒ AttackedAndStopped：付指挥点、耗一次行动，但**不结算伤害**
#     switch_to   换了防守方：新目标 id | "hq"
#     paid        实付指挥点（含目标税）；缺省 = 攻击者的 opc
#     dmg_def / dmg_att / def_dies / att_dies   钩子管线算出的最终伤害与是否阵亡（有就覆盖本模块自己的近似）
#     own_after   攻击者自己的 OnAfterAttack 已经在 buckets["after"] 里 ⇒ 不再叠 `aa_hq`
#     buckets     {"before"|"mid"|"after"|"def_destroyed"|"att_destroyed": {"eff": 全局效果摘要,
#                  "units": [(card_id, 该牌上的效果摘要)]}}，按攻击链里发生的先后应用
#   缺省空 dict ⇒ 完全走本模块原有的近似结算（行为不变）。
class Sim:
    """场面快照：单位表 + 总部 + 指挥点/槽/待生效 + 手牌。"""

    def __init__(self, units: dict, hq: dict, kredits: float = 0.0, hand: Optional[dict] = None,
                 slots: float = 0.0, pending: Optional[list] = None, hq_guarded: bool = False,
                 front_owner: Optional[ESide] = None, legal: Optional[dict] = None,
                 pair_eff: Optional[dict] = None, deck_left: int = -1, fatigue: int = 0,
                 deck: Optional[list] = None, deck_cards: Optional[dict] = None,
                 attack_fx: Optional[dict] = None,
                 rng_seed: Optional[int] = None, card_templates: Optional[dict] = None,
                 restrictions: Optional[list] = None, death_fx: Optional[dict] = None,
                 kredit_max: float = 24.0, event_fx: Optional[dict] = None,
                 forecast: Optional[dict] = None, hand_target_fx: Optional[dict] = None,
                 spawn_pick: Optional[dict] = None, spawn_stats=None, front_limited: bool = False,
                 my_side: Optional[ESide] = None):
        if my_side is None:
            raise ValueError("Sim 需要 my_side（本地玩家的座位）；座位用游戏的 ESide，没有 local/enemy")
        # 座位：`units[*].side`、`hq`、`front_owner`、`restrictions[*]["side"]`、`playing_side` 都是游戏的 ESide。
        # “我方” = `me`（= 本局 mySide），“对方” = `opp`；搜索只替 `me` 行动。
        self.me, self.opp = ESide(my_side), other_side(ESide(my_side))
        self.units, self.hq, self.kredits = units, hq, kredits
        # ★ 2026-10-02（EVAL-NATIVES §8-1）：游戏把指挥点/槽都夹到 `[0, max]`
        #   （`getMaxPossibleKredits` = GameState+0x338，构造器默认 24）。`_res_eff` 的
        #   己方 kredits/slots 都按它夹取；`opp_*` 是增量、支出不夹。
        self.kredit_max = float(kredit_max if kredit_max is not None else 24.0)
        self.attack_fx = attack_fx if attack_fx is not None else {}
        self.hand = hand if hand is not None else {}
        self.slots = slots
        self.pending = list(pending or [])          # [(几个我方回合后, 指挥点变化量)]
        self.hq_guarded = hq_guarded                # 敌方总部当前是否被守护
        self.front_owner = front_owner
        self.cap_used = 0
        self.bonus = 0.0        # 无法模拟的效果的估值（效果摘要缺失时用 `_est`）
        # 带目标的手牌：游戏自己的判断函数说「能指向谁」——{手牌id: [目标id | "hq" | None]}，
        # 每个 (牌, 目标) 各自是一条独立走法；{(手牌id, 目标): 效果摘要} 是这一对空跑出的效果。
        self.legal = legal if legal is not None else {}
        self.pair_eff = pair_eff if pair_eff is not None else {}
        self.hq_armor: dict = {}   # {side: 总部重甲 0..3}（BP CalculateDamageDealt：战斗伤害先扣重甲）
        self.hq_immune: dict = {}  # {side: 总部免疫 ⇒ 伤害 0}
        self.hq_known = True     # 读不到敌方总部牌时为 False ⇒ 不列「打总部」（避免对不存在的目标出手）
        # ★ 疲劳（2026-10-02 用户最终口径：**按游戏自己的规则模拟**）：
        #   BP `ApplyFatigueDamage` = 对总部造成 **当前疲劳计数** 点伤害，然后计数 +1。
        #   `fatigue` 的初值取快照 `BoardState.fatigue[side]`（游戏里的实时计数）。
        #   `deck_left` 保留给"读不到牌序"的旧路径；`-1 = 未知`。
        self.deck_left = int(deck_left or 0)
        self.fatigue = int(fatigue or 0)
        # ★ 2026-10-02（用户）：**牌库的结构照原版** —— 权威存储是 GameState 上的
        #   `DeckCardIDs_Left/Right`（`TArray<int32>`，index 0 = 牌顶；`gs.deck_ids()` 读的就是它），
        #   `BP_Deck_C` 只是表现层（抽牌动画 / `CardsPulledFromDeckBeforeBeingPutIntoHand`）。
        #   ⇒ 模拟里第一等状态 = `deck`（**id 列表**：可洗、可插、可删，和 `SetDeckBySide` 同构），
        #     `deck_cards` 只是 `id → 牌模板 H` 的取值表。
        #   `deck=None` = **读不出来（未知）** ⇒ 退回旧行为；`deck=[]` = 明确知道牌库是空的。
        self.deck = list(deck or [])
        self.deck_cards = dict(deck_cards or {})
        self.deck_known = deck is not None
        # ★ 卡 id → 牌模板 `H`（全盘：手牌/场上都收）—— 收缴（salvage）要拿被收缴目标的
        #   名字/费用/关键词/效果来造 1/1 复制；`deck_cards` 只管牌库那几张。
        self.card_templates = dict(card_templates or {})
        # GameplayRestrictionEffects 的影子：[{"side","type","turns"}]（AddGameplayRestriction）。
        #   语义见 effectvm.RESTRICTION_VERBS 的类型表；只对我方受禁的动作做真实约束。
        self.restrictions = [dict(r) for r in (restrictions or [])]
        # ★ 死亡触发（`OnDestroyed` 0x27）预计算结果：{卡 id: eff}（rule 跑 VM 得到）。
        #   第一版只含"死亡单位**自己**的覆写"（5th SASEBO 的洗牌+抽 1 之类）；
        #   旁观者响应（OnOtherCardDestroyed ×69 张）成本×N，暂不算。
        self.death_fx = dict(death_fx or {})
        # ★ **预计算的钩子后果表**（rule 跑 VM 得到）：`{事件种类: {卡 id: eff}}`。
        #   同一张表服务所有"某事件发生时别的牌会怎样"的触发族，避免每加一族就多一个字段：
        #     "move"    上线（0x32）—— 规则在 `triggers.move_effects`，`sim_move` 结算
        #     "draw"    别人抽到牌（0x2A）—— `triggers.drawn_effects`，`draw_chain` 每抽一张查一次
        #   （种类登记在 `EVENT_FX_KINDS`；没登记的键会被拒，防止拼错名字静默无效。）
        self.event_fx = {}
        for _k, _v in (event_fx or {}).items():
            if _k not in EVENT_FX_KINDS:
                raise ValueError("未知的 event_fx 种类 %r（可选 %s）" % (_k, EVENT_FX_KINDS))
            self.event_fx[_k] = dict(_v or {})
        # 洗牌用的随机流（`cardsRandomStream` 的当前种子；读不到 = None ⇒ 洗完后牌序未知）
        try:
            from kardsmem.rng import Stream
            self.rng = Stream(int(rng_seed)) if rng_seed is not None else None
        except Exception:                                         # noqa: BLE001
            self.rng = None
        # 抽牌递归：抽到的牌自己可能带"抽到时"效果（抵抗这类 autoplay 卡会**再抽**）
        #   ⇒ 用队列而不是 for 循环；上限 DRAW_CAP（游戏里也就几十张）。
        self.draws_done = 0
        # ★ 2026-10-02：BySide 族的写类原生（setKreditBySide/ChangeKreditsBySide/SetPlayingSide…）在 effectvm 里记成
        #   opp_kredit / opp_slot / playing_side（`_res_eff` / `_apply_eff` 消费）。
        self.opp_kredits = 0.0       # 对手指挥点的变化量（累计）
        self.opp_slots = 0.0         # 对手指挥点槽的变化量（累计）
        self.playing_side = None     # None=未变；opp=这一步之后行动权交给了对方 ⇒ 我方没有后续动作
        self.tmp_seq = 0             # 模拟里"新造出来的牌"的负 id 计数（塞进牌库的匿名牌）
        self.hold_v = None           # 匿名牌/新造牌的持有价值（评估侧的 `WEIGHTS["draw_v"]`；engine 不 import 权重表，由 rule 建 Sim 后填）
        # §15.5：对手抽牌/加手牌（opp_draw/opp_gain_cards）累计，evaluate 里扣分（对手资源变多）。
        self.opp_cards = 0
        # 组 D/诚实协议：模拟器消费不了的效果（如只改 attack_buff 而 U 只存总量）记这里，
        # 不硬塞进总量、也不编造价值（调用方可把它并进 probe.gaps）。
        self.gaps = []
        self.turn_ended = False          # `sim_turn_end` 幂等标记（见函数注释）
        # ★ 2026-10-03（M1，总纲 §4）：
        #   `forecast`：预报两层提示的**确定候选表** `{天气: {变体: (内部名, 价值提示)}}`——候选由种子预测（rule/forecast_pool
        #   在建 Sim 时算好，**随机已被算成确定结果**）；价值提示由策略侧预估，只是数据，sim 不解释它。None = 没有（预报只能记缺口）。
        #   `pending_cards`：跨回合待生效的"下回合开始进手牌的牌"`[(内部名, 价值提示)]`（预报选中的牌写进变量、下回合开始才 spawn，
        #   弯路 #26）；由评估的 `value_pending` 估值。
        self.forecast = {w: dict(v) for w, v in (forecast or {}).items()} if forecast else None
        self.pending_cards = []
        # ★ autoplay 待打出队列（原版：抽到/塞进手牌的 `autoplay` 牌 ⇒ `GameState::AddAutoPlayCards` 入队，
        #   在动作边界（`ExecuteAutoPlayCards`）冲刷：免费 `PlayCardFromHand`，其交互在那个边界上弹出）。元素 `(card_id, 效果摘要)`。
        self.autoplay_queue = []
        # ★ 手牌目标提示（`selectTargetFromHand` → 玩家点一张手牌 → 发起牌的 `OnHandTargetSelected`）的**确定候选表**
        #   `{发起牌 id: {候选手牌 id: 该候选被选中时的效果摘要}}`；效果由策略/规则侧用 VM 对每个候选空跑 `OnHandTargetSelected`
        #   得到（175th / PBY CATALINA 的效果里是 `to_deck`）。None/缺 = 没有 ⇒ 只能记缺口。
        self.hand_target_fx = {k: dict(v) for k, v in (hand_target_fx or {}).items()} if hand_target_fx else None
        # ★ "三选一加入手牌"（`selectCardToDraw`：CRUISER SCOUTS / SOUL OF OLD JAPAN…）的**确定候选表**
        #   `{发起牌 id: [{"name","atk","dfn","cost","typ"}×≤3]}`——候选由种子预测（`semantics/choosespawn.py`，随机已算成确定结果）。
        #   None/缺 = 没有 ⇒ 该提示只能记缺口。
        self.spawn_pick = dict(spawn_pick) if spawn_pick else None
        # 生成单位（SpawnCardOnBattlefield/InFrontline）用卡自己的面板：`spawn_stats(内部名) -> {"atk","dfn","cost","typ","kw"}|None`
        #（由 rule 按静态卡表给）；查不到 ⇒ 不编面板，记缺口。`front_limited` = 原版 `IsFrontlineLimited()`（前线只容 2 张）。
        self.spawn_stats = spawn_stats
        # 本回合常驻的"别的牌进场时"效果（JUNGLE FEVER：之后进场的己方单位 +2 攻 + 闪击）。元素 = 对**每个进场单位**生效的效果摘要，
        # 由 VM 空跑该牌的 `OnOtherCardEnterPlay` 得到（`enterPlayOnTurn==本回合` 的守卫按"已打出"覆盖）；回合结束清空。
        self.enter_mods = []
        # ★ 延迟/常驻效果登记表（2026-10-07，`engine/deferred.py`；ECHELON："Your units get: Gets +1+1 if another of your units is attacked."）：
        #   `armed`  = 已布防的留下来的钩子 `[Armed]`（来源牌、钩子名、座位、是否一次性）——指令**打出之后**才有；
        #   `grants` = giver 级账 `{(授予者卡 id, 能力名): {被授予的单位 id}}`（原版 `receivedAbilitiesFromCards`，
        #              `HasCustomAbilityFromCard` 的答案来源；`CustomAbilityAdd` 三条路都经 `natives.abilities.grant_ability` 写它）。
        self.armed = []
        self.grants = {}
        self.front_limited = bool(front_limited)
        # ★ 协力（2026-10-06）：原版 `BP_GameState_Battle.activeBondFactions`（`TSet<EFactionEnum>`）—— **回合开始时**
        #   由 `SetActiveBondsAtStartOfTurn(side)` 整体重算（`BP_GameState_Battle.cpp:2529`），回合中不更新。
        #   `None` = 读不出/未知 ⇒ 协力牌打出时**不下结论**（不扣、不编）；`set()` = 明确知道一个国家都没有。
        self.bond_factions = None

    def copy(self) -> "Sim":
        s = Sim({k: u.copy() for k, u in self.units.items()}, dict(self.hq), self.kredits,
                dict(self.hand), self.slots, list(self.pending), self.hq_guarded,
                self.front_owner, self.legal, self.pair_eff, attack_fx=self.attack_fx, my_side=self.me)
        s.cap_used = self.cap_used
        s.bonus = self.bonus
        s.hq_known = self.hq_known
        s.hq_armor, s.hq_immune = dict(self.hq_armor), dict(self.hq_immune)
        s.deck_left, s.fatigue = self.deck_left, self.fatigue
        s.deck = list(self.deck)
        s.deck_cards = dict(self.deck_cards)
        s.deck_known = self.deck_known
        s.card_templates = dict(self.card_templates)
        s.restrictions = [dict(r) for r in self.restrictions]
        s.death_fx = dict(self.death_fx)
        s.event_fx = {k: dict(v) for k, v in self.event_fx.items()}
        s.kredit_max = self.kredit_max
        s.rng = self.rng.copy() if self.rng is not None else None
        s.draws_done = self.draws_done
        s.opp_kredits, s.opp_slots, s.playing_side = self.opp_kredits, self.opp_slots, self.playing_side
        s.tmp_seq = self.tmp_seq
        s.hold_v = self.hold_v
        s.opp_cards = self.opp_cards
        s.gaps = list(self.gaps)
        s.turn_ended = self.turn_ended
        s.forecast = self.forecast                      # 只读表，共享即可
        s.pending_cards = list(self.pending_cards)
        s.autoplay_queue = list(self.autoplay_queue)
        s.hand_target_fx = self.hand_target_fx
        s.spawn_pick = self.spawn_pick                  # 只读表
        s.spawn_stats, s.front_limited = self.spawn_stats, self.front_limited
        s.enter_mods = list(self.enter_mods)
        s.armed = [a.copy() for a in self.armed]
        s.grants = {k: set(v) for k, v in self.grants.items()}
        s.bond_factions = None if self.bond_factions is None else set(self.bond_factions)
        return s


class State(Sim):
    """一局盘面（`Sim` 的具名子类）。`copy()` 返回 `State`。"""

    def copy(self) -> "State":
        c = Sim.copy(self)
        c.__class__ = State
        return c

