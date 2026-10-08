# -*- coding: utf-8 -*-
"""engine.natives.stats —— 卡面数值改动一族（攻/防/行动费/重甲）的 `EChangeType` 语义。

**P3 第一族**（`docs/REFACTOR-PLAN.md` §2 的 P3；工单见 `docs/P3-NATIVE-PORT.md`）。
原版出处（FModel 导出的**一手**产物，1.60.27292.launcher）：

  * 原版：`BP_CardFunctions::ChangeAttack`（`BP_CardFunctions.cpp:10981`）
  * 原版：`BP_CardFunctions::ChangeDefense`（`BP_CardFunctions.cpp:11215`）
  * 原版：`BP_CardFunctions::ChangeOperationCost`（`BP_CardFunctions.cpp:8892`）
  * 原版：`BP_CardFunctions::ChangeHeavyArmor`（`BP_CardFunctions.cpp:10320`）
  * 原版：`enum class EChangeType : uint8`（SDK dump 1.60.27292 `CppSDK/SDK/BattleUtilityFunctions_parameters.hpp`）

`ChangeAttack`（`:10981-11212`）的分支表 —— 逐行读过，行号是导出文件的行号：

| changeType | 分支 | 原版怎么写 | 设值? | 夹取 [0,99]? | after 事件? |
|---|---|---|---|---|---|
| 0 tempBuffGive | `:11128-11149` | `attackBuff += amount`（**独立字段**） | 否 | **否** | 否 |
| 1 permBuff | `:11095-11126` | `attack = clamp(attack + amount, 0, 99)` | 否 | 是 | 否 |
| 2 SetValue | `:11068-11093` | `attack = clamp(amount, 0, 99)` | **是** | 是 | 否 |
| 3 Suppress | 同 SetValue（都 `goto Label_855`） | 同上 | **是** | 是 | 否 |
| 4 tempBuffRemove | `:11185-11208` | `attackBuff += amountRemoved`（`ChangeBuffsFromCards` 给的值） | 否 | **否** | 否 |
| 5 veteranSet | 同 SetValue | `attack = clamp(amount, 0, 99)` | **是** | 是 | 否 |
| 6-9 customAdd/customRemove/combatModify/notUsed | `:11151-11183` | **不改数值** | — | — | **是（唯一入口）** |

两个容易搞错的点（**从原版读出来**，不是推测）：

1. **`ExecuteAfterChangeAttackEvents` 全文件只有一个调用点（`:11174`），只从 default 分支（6-9）进**
   ⇒ changeType 0..5 这些**常规**改动**不**触发"数值变化通知"。（被调函数体见 `:11950`：未压制 ⇒
   自己 `OnAfterChangeAttack`；压制 ⇒ `FetchAllCardsWithEventTrigger(0x5)` 逐张 `OnAfterOtherCardChangeAttack`。）
   ★ **证据强度（别当铁证）**：FModel 反编译的 ubergraph **控制流是有损的**（同类教训：
   "控制流变 goto 汤、个别节点会丢"）—— 那 5 条分支都写 `valueChanged = true; return;`，而函数体开头
   另有一个 `Label_434: if (!valueChanged) …` 在等它，两者**不可能同时成立** ⇒ "分支是否真的就地 return"
   还需要**第二来源**（IDA 读 Kismet 字节码）才能定案。本条只作**待核结论**，不据此改行为（R4）。
   ★ **触发号（已核对仓库自己的报告，别再凭记忆写）**：`0x04` 是**攻击后**（`OnAfterOtherCardAttacks`，
   `ATTACK-HOOKS-1.60.md:855`）；"数值变化通知"是
   **`0x05/0x06/0x07/0x10/0x11/0x2C/0x2D/0x09/0x0A`**（同报告 `:867`，标**未接**）——与本文件读到的
   `FetchAllCardsWithEventTrigger(0x5)`（攻，`:11961`）、`0x6`（防，`:11357`）、`0xA`/`0x9`（行动费，
   `:8950`/`:9072`）对得上。**我先前在提交信息里写的 `0x04`/`0x05` 是错的口径，已按此更正。**
2. `tempBuffGive`/`tempBuffRemove` 写的是**独立的 `attackBuff` 字段**
   （`getAndDecryptAttackBuff`/`setAndEncryptAttackBuff`，`:11141-11145`、`:11200-11204`）而且**不夹取**；
   `permBuff`/SetValue 家族写**总值**（`setAndEncryptAttack`）且夹取 `[0,99]`。

### `ChangeDefense`（`:11215-11480`）：**与 ChangeAttack 不同构 —— 别照抄**

switch（`:11271-11316`）：`0/4/6/7/9 → Label_3734`、`1 → Label_1995`、**`2/3/5 → Label_984`**、
`8 → Label_5740`。

★ **`Label_3734` 是"拒绝"分支**（`:11527-11532`）：记日志 `change type incorrect for "Change Defense"`
+ `qqq=false` + 直接 return ⇒ **什么都不做**，所以**防御根本没有 0/4 的 buff 通道**（与 `ChangeAttack` 不同）。
`Label_5740`（`ct=8`）是**空 `return`**（`:11692-11693`）⇒ 也是什么都不做。
（★ 2026-10-03 更正：先前只写了"0/4/6/7/9 → Label_3734"、没说那个 label 是什么；
也没注意到 `ChangeDefense` 的函数体其实到 `:11696` 才结束 —— 记的行号区间 `(11215, 11480)` 是错的，
而当时的守卫只检查"末尾没越到隔壁"，没检查"末尾就是函数末尾"，所以漏了。）

* `Label_984`（2/3/5）**无条件**写 `setAndEncryptDefense(clamp(amount,0,99))` + `maxDefense = clamp(amount)`
  （`:11319-11327`）——**没有** ChangeAttack 那种"已经相等就 return"的早退。然后：
  * `getTotalDefense() == 0` ⇒ **`DestroyCard`**（`:11338-11347`）：防御被设成 0 的卡**直接离场**；
  * `changeType == 2` ⇒ `OnAfterDefenseIsSet()` + `FetchAllCardsWithEventTrigger(0x6)` 循环（`:11350-11363`）。
* `Label_1995`（1 permBuff）自己再分两支（`:11382-11473`）：
  * `amount < 0` ⇒ `setAndEncryptDefense(总防 + amount)`，若总防 **≤ 0 ⇒ `DestroyCard`**（`:11396-11419`）；
  * `amount > 0` ⇒ 夹取区间是 **`[1,99]`（下限是 1、不是 0！** `:11439`），且总防超过 `maxDefense`
    时把 `maxDefense` 抬上去（`:11443-11452`）。

### `ChangeOperationCost`（`:8892-9188`）

前置闸门：卡无效 / `instigatorID ≤ 0` ⇒ 记日志返回（`:8896-8904`）；**`IsUnrevealedCovertCard` ⇒ 静默 return**
（`:8906-8911`）。随后 `ChangeBuffsFromCards(..., 0x3, ...)` 取 `amountRemoved`（`:8979`）。

switch（`:8986-9016`）：`0 → Label_1487`、`1 → Label_1251`、**`2/3/5 → Label_2747`**、`4 → Label_2952`、
`6-9 → Label_3168`（`:9186-9187` **什么都不做**）。

* `Label_1251`（1）：`amount == 0` ⇒ no-op；否则 `operationCost = clamp(op + amount, 0, 99)`（`:9018-9037`）。
* `Label_1487`（0）：`operationCostBuff += amount`，**不夹取**（`:9039-9056`）。
* `Label_2747`（2/3/5）：`operationCost == amount` ⇒ no-op；否则 `operationCost = clamp(amount,0,99)`
  且置 **`runResetEvents = true`**（`:9144-9163`；该标志 `:8929` 初始化、`:9069` 才被读）。
* `Label_2952`（4）：`operationCostBuff += amountRemoved`，**不夹取**（`:9165-9184`）。

★ 行动费的 "buff" 也是**独立字段** `operationCostBuff`（0/4 写它，1/2/3/5 写 `operationCost`）——
我们只有"一个总量"，与 R3 同类缺口。

★ **`setAndEncryptDefense` 是"双重身份"的叶子**：`ChangeDefense` 调它，**`ApplyDamageToCard` 也调它**
（伤害扣防那一步，`:16461`，见 `engine/triggers.py` 的同处注释）。所以"把录到的 `setAndEncryptDefense`
一律记成 `set_defense`"**拿不到** `ChangeDefense` 的"设成 0 ⇒ 摧毁"语义（那是 BP 层的后续，不在叶子里）；
反过来若无脑给 `set_defense` 加"0 ⇒ 摧毁"，又会和伤害链**重复结算**。⇒ 新缺口 R9，**先不动**。

### `ChangeHeavyArmor`（`:10320-10507`）

前置闸门：卡无效 / `instigatorID ≤ 0` ⇒ 记日志 `"invalid card for [Change Heavy Armor]"`（`:10343`）；
**`IsUnrevealedCovertCard` ⇒ `qqq=false` 返回**（`:10332-10340`）。随后
`ChangeBuffsFromCards(..., 0x4, ...)` 取 `amountRemoved`（`:10386`）。

switch（`:10393-10423`）：`0 → Label_1244`、`1 → Label_1008`、**`2/3/5 → Label_1433`**、`4 → Label_1627`、
**`6-9` ⇒ 直接 `return`（`:10423`：不改数值、也**不发**任何事件 —— 与 `ChangeAttack` 的 6-9 不同！）**

* `Label_1008`（1）：`amount == 0` ⇒ no-op；否则 `heavyArmor = clamp(heavyArmor + amount, 0, **3**)`（`:10436-10440`）。
* `Label_1244`（0）：`heavyArmorBuff += amount`，**不夹取**（`:10456-10459`）。
* `Label_1433`（2/3/5）：`heavyArmor == amount` ⇒ no-op；否则 `heavyArmor = clamp(amount, 0, **3**)`（`:10465-10478`）。
* `Label_1627`（4）：`heavyArmorBuff += amountRemoved`，**不夹取**（`:10484-10499`）。

事件：`NotifyAddHeavyArmor` + `ExecuteOnOtherCardsAbilitiesChanged(cardToChangeRef)`（`:10379-10381`）
—— **0x1D 能力变化**那一条，我们已在 `triggers.run_abilities_changed` 里建模（见 A5-③）。

★ **重甲的独立 buff 字段是第三个**（`attackBuff` / `operationCostBuff` / `heavyArmorBuff`）：三者同构，
但我们只有一个"总量" ⇒ 同 R3/R10。

★ 我们现有实现（`engine.effectvm.to_effects`）把 0..5 一律折成"总攻/防的增量"，只有 `SetValue` 家族
换算成"目标值 − 当前值"。因为**总量 = attack + attackBuff**，这个折法在总量上等价；差别只在：
  * `tempBuffGive`/`tempBuffRemove` 的**不夹取**没实现（消费侧对总量夹了 [0,99]，见
    `sim/engine.py` 的 `set_*` 分支）——**R3 缺口**，见 `docs/P3-NATIVE-PORT.md`；
  * 6-9 的 after 事件链没建模（数值不动这一点与原版一致）。
* `ChangeOperationCost`（`:8892-9188`）**已逐行读完**（分支表见上），但**仍不接线**（R1）：它多一个
  `isBuff` 入参、开头有 `IsUnrevealedCovertCard` 静默早退、且 2/3/5 分支会置 `runResetEvents`（触发 0x09）
  —— 这些语义要落到 `sim` 才有意义，而 `sim` 现在只有"一个总量"字段（R3/R9 同族）。
* `ChangeDefense`（`:11215-11480`）**已逐行读完**（分支表见上），同样**不接线**（R2）：它比 `ChangeAttack`
  多两条"防御归零 ⇒ 摧毁"和"permBuff 下限是 1"的语义，而 `set_defense` 这个键**分不清**调用方是
  `ChangeDefense` 还是 `ApplyDamageToCard`（R9）。

不编数：`ChangeBuffsFromCards` 的 `amountRemoved` 怎么算、重甲的 `clamp(0,3)` 之外的东西、
after 事件链本身，都**没有**在这里假装实现。

依赖方向（`engine/natives/__init__.py`）：本模块**不 import `engine` 的其它模块，也不 import `sim`**，
所以 `effectvm` / `sim.engine` 都能安全地引它，不产生环。
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# EChangeType —— 原版枚举的数值就是游戏里的数（别改）
# ---------------------------------------------------------------------------
TEMP_BUFF_GIVE = 0
PERM_BUFF = 1
SET_VALUE = 2
SUPPRESS = 3
TEMP_BUFF_REMOVE = 4
VETERAN_SET = 5
CUSTOM_ADD = 6
CUSTOM_REMOVE = 7
COMBAT_MODIFY = 8
NOT_USED = 9

#: 给日志/缺口文本用的原名（与 SDK dump 逐字一致）
NAMES = {TEMP_BUFF_GIVE: "tempBuffGive", PERM_BUFF: "permBuff", SET_VALUE: "SetValue",
         SUPPRESS: "Suppress", TEMP_BUFF_REMOVE: "tempBuffRemove", VETERAN_SET: "veteranSet",
         CUSTOM_ADD: "customAdd", CUSTOM_REMOVE: "customRemove", COMBAT_MODIFY: "combatModify",
         NOT_USED: "notUsed"}

STAT_MIN, STAT_MAX = 0, 99      # 原版：ChangeAttack 的 clamp 写法 ((v<0)?0:((v>99)?99:v))（:11081/:11087/:11120）
ARMOR_MIN, ARMOR_MAX = 0, 3     # 原版：getTotalHeavyArmor 是 clamp(0,3)（NATIVE-COVERAGE §14.2 #14，IDA）

#: 原版：ChangeAttack 的 switch 把 2/3/5 都送进 `Label_855`（`setAndEncryptAttack(clamp(amount))`）
SET_VALUE_FAMILY = frozenset((SET_VALUE, SUPPRESS, VETERAN_SET))
#: ★ **`ChangeKreditCost` 是例外**（`BP_CardFunctions.cpp:10120-10146`）：只有 **2/3** 送 `Label_758`
#:   （`setAndEncryptKredit(clamp(amount,0,99))`）；**`5` 与 `6-9` 落到默认 `Label_1614`**
#:   —— 那里 `getTotalKreditCost` + `NotifySetKreditCost`，然后（发起者就是这张牌时）
#:   `FetchAllCardsWithEventTrigger(0x2D)` 逐张 `OnOtherCardKreditCostChanged`：**不改数值、只发通知**
#:   （0x2D 属"数值变化通知"族，见 `ATTACK-HOOKS-1.60.md:867`）。别照抄 `ChangeAttack` 的集合。
KREDIT_COST_SET_FAMILY = frozenset((SET_VALUE, SUPPRESS))
#: 原版：0/4 走 `get/setAndEncryptAttackBuff`（独立 buff 字段）
BUFF_FIELD_FAMILY = frozenset((TEMP_BUFF_GIVE, TEMP_BUFF_REMOVE))
#: 原版：`Label_1990` 之后的 default 分支（6/7/8/9）—— 不改数值，只发 after 事件
AFTER_EVENT_FAMILY = frozenset((CUSTOM_ADD, CUSTOM_REMOVE, COMBAT_MODIFY, NOT_USED))

# ---------------------------------------------------------------------------
# 三条 BP 函数的 switch 表（**读导出件读出来的规格**，行号写在右边）
#   放成数据是有意的：`tests/test_native_stats.py` 会拿它去**核对导出件的行号**，
#   免得"出处注释"是随手写的（P3 的护栏要求每个移植都带出处，但没人在验出处是真的）。
# ---------------------------------------------------------------------------
#: 原版：`ChangeAttack` 的 switch（`BP_CardFunctions.cpp:11036-11066`）
ATTACK_SWITCH = {TEMP_BUFF_GIVE: "Label_1722", PERM_BUFF: "Label_1224", SET_VALUE: "Label_855",
                 SUPPRESS: "Label_855", TEMP_BUFF_REMOVE: "Label_2415", VETERAN_SET: "Label_855",
                 CUSTOM_ADD: "Label_1990", CUSTOM_REMOVE: "Label_1990", COMBAT_MODIFY: "Label_1990",
                 NOT_USED: "Label_1990"}
#: 原版：`ChangeDefense` 的 switch（`:11271-11316`）—— 注意 6/7/9 也送 `Label_3734`（与 Attack 不同）
DEFENSE_SWITCH = {TEMP_BUFF_GIVE: "Label_3734", PERM_BUFF: "Label_1995", SET_VALUE: "Label_984",
                  SUPPRESS: "Label_984", TEMP_BUFF_REMOVE: "Label_3734", VETERAN_SET: "Label_984",
                  CUSTOM_ADD: "Label_3734", CUSTOM_REMOVE: "Label_3734", COMBAT_MODIFY: "Label_5740",
                  NOT_USED: "Label_3734"}
#: 原版：`ChangeOperationCost` 的 switch（`:8986-9016`）
OPCOST_SWITCH = {TEMP_BUFF_GIVE: "Label_1487", PERM_BUFF: "Label_1251", SET_VALUE: "Label_2747",
                 SUPPRESS: "Label_2747", TEMP_BUFF_REMOVE: "Label_2952", VETERAN_SET: "Label_2747",
                 CUSTOM_ADD: "Label_3168", CUSTOM_REMOVE: "Label_3168", COMBAT_MODIFY: "Label_3168",
                 NOT_USED: "Label_3168"}
#: 原版：`ChangeHeavyArmor` 的 switch（`:10393-10423`）—— **6-9 直接 return**（连事件都没有）
HEAVY_SWITCH = {TEMP_BUFF_GIVE: "Label_1244", PERM_BUFF: "Label_1008", SET_VALUE: "Label_1433",
                SUPPRESS: "Label_1433", TEMP_BUFF_REMOVE: "Label_1627", VETERAN_SET: "Label_1433",
                CUSTOM_ADD: "return", CUSTOM_REMOVE: "return", COMBAT_MODIFY: "return", NOT_USED: "return"}

#: 三条函数在导出件里的**函数体行号区间**（`(起, 止)`，止 = 下一个 `public void` 之前）。
#: 测试用它把"我引的行号"框在正确的函数里 —— 引到隔壁函数去是很典型的出处错误。
BP_FUNC_RANGES = {"ChangeAttack": (10981, 11212), "ChangeDefense": (11215, 11696),
                  "ChangeOperationCost": (8892, 9188), "ChangeHeavyArmor": (10320, 10507),
                  "CanCardBeBuffed": (23631, 23704)}

#: 原版 `BP_CardFunctions::CanCardBeBuffed`（`:23631-23704`，**蓝图函数**逐行读过）：
#: 先看 `IsUnrevealedCovertCard`（= `hasCovert@0x1EA ∧ ¬isRevealed@0x33B`，IDA 定案）——
#: **不是**"未揭示的隐蔽牌" ⇒ `CanBeBuffed = true`（`Label_730`，`:23690-23693`）；
#: **是** ⇒ 按 `Card->location` 分派：`{1,2,3,4,9}` → `Label_762`（true，`:23700-23703`）、
#: `{0,5,6,7,8}` → `Label_746`（false，`:23695-23698`）、**其它值原版不写出参**
#: （调用方初始化为 0 ⇒ 按 false，`:23688` 那个裸 `return`）。
#: 用 `ECardLocation` 说人话：**未揭示的隐蔽牌只有在牌库/手牌里才可被 buff**；
#: 在总部/前线（场上）·弃牌堆·`NotAvailable` 都不可。
CARD_BE_BUFFED_LOCATIONS = frozenset((1, 2, 3, 4, 9))     # Deck_Left/Right、Hand_Left/Right、Deck


def can_card_be_buffed(unrevealed_covert: bool, location) -> bool:
    """`CanCardBeBuffed` 的纯函数版（单一来源；`effectvm._fill_cur_stats` 用它喂门）。

    `ChangeAttack`（`:10990`）/`ChangeDefense`（`:11222`）在它为 false 时 `goto Label_472`/`Label_466`
    = `qqq = false; return` ⇒ **什么都不改**（`:11023-11025` / `:11258-11260`）。
    `ChangeKreditCost` 只在 `skipCovertCheck == false` 时过这道门（签名第 5 参）。
    """
    if not unrevealed_covert:
        return True
    return location in CARD_BE_BUFFED_LOCATIONS


def clamp_stat(v) -> int:
    """攻/防/行动费的夹取。原版：`((v<0)?0:((v>99)?99:v))`（`BP_CardFunctions.cpp:11081/:11087/:11120`）。"""
    v = int(v)
    return STAT_MIN if v < STAT_MIN else (STAT_MAX if v > STAT_MAX else v)


def clamp_armor(v) -> int:
    """重甲单独一套夹取：`getTotalHeavyArmor` 是 `clamp(0,3)`（NATIVE-COVERAGE §14.2 #14，IDA 逐行）。"""
    v = int(v)
    return ARMOR_MIN if v < ARMOR_MIN else (ARMOR_MAX if v > ARMOR_MAX else v)


def is_set_value(change_type) -> bool:
    """这个 `EChangeType` 是"**设成** n"还是"加减 n"。

    原版：`ChangeAttack` 的 switch 里 2/3/5 都 `goto Label_855`（写的是 `clamp(amount)` 这个**绝对值**）。
    读不出（`None`/未知/缺参）⇒ `False`：当加减处理（与旧实现 `_is_set_change` 一致，**行为不变**）。
    """
    return change_type in SET_VALUE_FAMILY


def writes_buff_field(change_type) -> bool:
    """原版：0/4 写的是**独立的 `attackBuff` 字段**，不是总值（`:11128-11149` / `:11185-11208`）。"""
    return change_type in BUFF_FIELD_FAMILY


def clamp_applies(change_type) -> bool:
    """原版：只有 `permBuff`(1) 与 SetValue 家族(2/3/5) 夹取 `[0,99]`；`tempBuffGive/Remove`(0/4) 直接 `+=`。"""
    return change_type == PERM_BUFF or change_type in SET_VALUE_FAMILY


def fires_after_change_events(change_type) -> bool:
    """是否走 `ExecuteAfterChangeAttackEvents`。

    原版：该函数**全文件唯一调用点**在 `:11174`，只有 default 分支（6-9）能到 ⇒ 0..5 一律不触发。
    """
    return change_type in AFTER_EVENT_FAMILY


def touches_stats(change_type) -> bool:
    """6-9 不动数值（原版 `:11151-11183` 只发事件）⇒ `False`。"""
    return not fires_after_change_events(change_type)


# ---------------------------------------------------------------------------
# verb 级原生（P4 第三刀，2026-10-04）：**值语义** —— 作用在"基础值 + 独立 buff 字段"上
# ---------------------------------------------------------------------------
def _change_two_field(unit, total_attr: str, buff_attr: str, amount, change_type, clamp) -> bool:
    """`Change{Attack,OperationCost,HeavyArmor}` 共用的两字段改值（原版三处同构）。

    * `0/4`（`writes_buff_field`）：**buff 字段** `+= amount`（**不夹**），总量 = `clamp(基础 + buff)`；
    * `1`（`permBuff`）：`amount == 0` ⇒ no-op；否则 **基础** = `clamp(基础 + amount)`；
    * `2/3/5`（set 家族）：**基础** = `clamp(amount)`；
    * `6-9`：**不改数值**（原版 `touches_stats` 为假；`ChangeHeavyArmor` 更是直接 return）。

    返回"总量是否真的变了"（原版值没变的分支是**有序 no-op**：不触发后续事件）。
    """
    if not touches_stats(change_type):
        return False
    total = int(getattr(unit, total_attr, 0) or 0)
    buff = int(getattr(unit, buff_attr, 0) or 0)
    base = total - buff
    if writes_buff_field(change_type):
        buff = buff + int(amount or 0)                 # 不夹（原版 0/4 直接 +=）
    elif is_set_value(change_type):
        base = clamp(amount or 0)
    else:                                              # permBuff(1)
        if int(amount or 0) == 0:
            return False
        base = clamp(base + int(amount or 0))
    new = clamp(base + buff)                           # 总量永远是 `clamp(基础 + buff)`（原版 getTotal*）
    setattr(unit, buff_attr, buff)
    changed = new != total
    setattr(unit, total_attr, new)
    return changed


def change_attack(unit, amount, change_type) -> bool:
    """`ChangeAttack(card, instigatorID, amount, changeType, skipAction, &qqq)` 的**值语义**（`:10981-11212`）。

    门（`CanCardBeBuffed`，`:10990`）与事件族**不在这里** —— 门由调用方判（`scripts` 的 sink 用
    `cur_stats["buffable"]`，与记录路径同一份 `can_card_be_buffed` ✓）。
    """
    return _change_two_field(unit, "atk", "atk_buff", amount, change_type, clamp_stat)


def change_operation_cost(unit, amount, change_type) -> bool:
    """`ChangeOperationCost(card, instigatorID, amount, changeType, isBuff, skipAction, skipAddToBattlelog)`
    的值语义（`:8892-9188`；2/3/5 设值写**基础**、`:9155`）。"""
    return _change_two_field(unit, "opc", "opc_buff", amount, change_type, clamp_stat)


def change_hand_operation_cost(card, amount, change_type) -> Optional[bool]:
    """`ChangeOperationCost` 作用在**手牌**上（IRON VICTORY：新补的 T-34 行动费设为 1；`:8892-9188` 同一函数，值语义同单位）。

    手牌快照不带行动费 ⇒ `card.opc` 为 None 表示"基础值不知道"：
      * 设值家族（2/3/5）：基础 = `clamp(amount)`，不需要旧值 ⇒ 可结算；
      * 加减（0/1/4）：要旧值 ⇒ **做不了 ⇒ 返回 None**（调用方记缺口，不猜）；
      * 6-9：不改数值 ⇒ False。
    → 总量是否变了（True/False）；None = 做不了。"""
    if not touches_stats(change_type):
        return False
    if getattr(card, "opc", None) is None:
        if not is_set_value(change_type):
            return None
        card.opc, card.opc_buff = clamp_stat(amount or 0), 0
        return True
    return _change_two_field(card, "opc", "opc_buff", amount, change_type, clamp_stat)


def change_heavy_armor(unit, amount, change_type) -> bool:
    """`ChangeHeavyArmor(card, instigatorID, amount, changeType, skipAction, &qqq)` 的值语义
    （`:10320-10507`；夹取是 `[0,3]`，不是 `[0,99]`）。"""
    return _change_two_field(unit, "armor", "armor_buff", amount, change_type, clamp_armor)


#: `ChangeDefense` 里"拒绝"的 changeType：0/4/6/7/9 都去 `Label_3734`（记日志 + `qqq=false` + return），
#: 8 去 `Label_5740`（空 return）⇒ **防御没有 buff 通道**（P3 的 R15 结论，`:11527-11532`）。
DEFENSE_REJECT = frozenset((TEMP_BUFF_GIVE, TEMP_BUFF_REMOVE, CUSTOM_ADD, CUSTOM_REMOVE,
                            COMBAT_MODIFY, NOT_USED))


def change_defense(unit, amount, change_type) -> dict:
    """`ChangeDefense(card, instigatorID, amount, changeType, skipAction, &qqq)` 的**值语义**
    （`:11215-11696`，逐行读过）。返回 `{"changed", "destroy", "after_set", "rejected"}`。

    分支（与 `ChangeAttack` **不同构** —— set 家族**没有早退**、且带摧毁）：
      * `0/4/6/7/9`（`Label_3734`）与 `8`（`Label_5740`）：**拒绝/空 return**，什么都不改（`:11527`）；
      * `1 permBuff`：`amount == 0` ⇒ no-op；**`amount < 0` 时 `dfn = 总量 + amount`（原版不夹！**
        `:11388-11394`）且 `mdef += amount`，写完 `总量 <= 0` ⇒ **摧毁**（`:11396-11419`）；
        `amount > 0` 时 `dfn = clamp(总量 + amount, **1**, 99)`（下限是 1，`:11439`）；
      * `2/3/5`：`dfn = clamp(amount, 0, 99)`，写完 `总量 == 0` ⇒ **摧毁**（`:11338-11347`）；
        `changeType == 2` 另发 `OnAfterDefenseIsSet` + trigger `0x6`（`:11350-11357`）—— 由 sink 记事件。
    """
    out = {"changed": False, "destroy": False, "after_set": False, "rejected": True}
    if change_type in DEFENSE_REJECT:
        return out
    out["rejected"] = False
    amt = int(amount or 0)
    cur = int(getattr(unit, "dfn", 0) or 0)
    if change_type == PERM_BUFF:
        if amt == 0:
            return out
        if amt < 0:
            new = cur + amt                                  # 原版不夹（`:11394`）
            unit.dfn = new
            unit.mdef = int(getattr(unit, "mdef", 0) or 0) + amt   # `maxDefense += amount`（`:11405`）
            out["changed"] = new != cur
            out["destroy"] = new <= 0
            return out
        new = cur + amt
        new = 1 if new < 1 else (99 if new > 99 else new)     # 正分支下限 **1**（`:11439`）
        unit.dfn = new
        out["changed"] = new != cur
        return out
    # 2/3/5：set 家族（无早退）
    new = clamp_stat(amt)
    unit.dfn = new
    out["changed"] = new != cur
    out["destroy"] = new == 0
    out["after_set"] = (change_type == SET_VALUE)
    return out


class _HqDefense:
    """总部牌的防御视图：`state.hq[座位]` 就是总防，`mdef` 不跟踪（鸭子类型给 `change_defense` 用）。"""
    __slots__ = ("dfn", "mdef")

    def __init__(self, total):
        self.dfn = int(total or 0)
        self.mdef = 0


def change_defense_hq(state, side, amount, change_type) -> dict:
    """`ChangeDefense` 打在**总部牌**上（`BP_CardFunctions.cpp:11215-11696`：函数体对总部与单位**同一条路径**，
    没有任何 HQ 特判；`CanCardBeBuffed`（`:23622`）对非隐蔽牌恒真 ⇒ 门不拦）。

    总部的总防 = `state.hq[side]`；值语义与单位完全相同（`change_defense`：永久加成正数夹 `[1,99]`、负数不夹、
    set 家族夹 `[0,99]`），结果写回 `state.hq[side]`。`destroy` = 总防 <= 0（原版走 `DestroyCard(总部)`，
    即对局结束，由调用方/终局链处理 —— 这里只把防御写对）。返回 `change_defense` 的结果字典。
    """
    shim = _HqDefense(state.hq.get(side, 0))
    r = change_defense(shim, amount, change_type)
    if not r.get("rejected"):
        state.hq[side] = shim.dfn
    return r


def change_kredit_cost(card, amount, change_type) -> dict:
    """`ChangeKreditCost(card, instigatorID, amount, changeType, isBuff, skipAction, skipAddToBattlelog)`
    的值语义（`BP_CardFunctions.cpp:10067-10146`）。返回 `{"changed", "notify", "set_family"}`。

    ★ **这个动词的集合家族与 `ChangeAttack` 不同**（P3 已读全，`:10120-10146`）：
      只有 **2/3** 送 `Label_758`（`setAndEncryptKredit(clamp(amount,0,99))`）；
      **`5` 与 `6-9` 落到默认 `Label_1614`** —— 那里只 `getTotalKreditCost` + `NotifySetKreditCost`
      +（发起者就是这张牌时）trigger `0x2D` ⇒ **不改数值、只发通知**（别照抄 `ChangeAttack` 的集合 ✗）。
      其余（`0/1/4`）与攻/防同构：`0/4` 写**独立 buff 字段**、`1` 写**基础值**，总量恒
      `clamp(基础 + buff, 0, 99)`（原版 `getTotalKreditCost`，IDA `0x144B15020`）。

    目标通常是**手牌那张牌**（`effectvm:679`）；`card` 只要求有 `cost`/`cost_buff` 两个字段（鸭子类型）。
    """
    out = {"changed": False, "notify": False, "set_family": False}
    amt = int(amount or 0)
    total = int(getattr(card, "cost", 0) or 0)
    buff = int(getattr(card, "cost_buff", 0) or 0)
    base = total - buff
    if change_type in KREDIT_COST_SET_FAMILY:            # 只有 2/3
        base = clamp_stat(amt)
        out["set_family"] = True
    elif change_type in SET_VALUE_FAMILY or change_type in AFTER_EVENT_FAMILY:
        out["notify"] = True                             # 5 与 6-9：`Label_1614`，不改数值
        return out
    elif writes_buff_field(change_type):                 # 0/4：buff 字段（不夹）
        buff = buff + amt
    elif change_type == PERM_BUFF:                       # 1：基础值
        if amt == 0:
            return out
        base = clamp_stat(base + amt)
    else:                                                # 未知 changeType：只通知、不改数值（保守）
        out["notify"] = True
        return out
    new = clamp_stat(base + buff)
    card.cost_buff = buff
    out["changed"] = new != total
    card.cost = new
    return out


def set_encrypted_field(card, field, value) -> None:
    """`setAndEncryptX(newX, key1, key2, FrameCount)`（**原生叶写入**）的值语义 = 直接把值写进那个字段。

    这批动词在录制侧是 `RECORD_ONLY`（`effectvm:217-218`）：录制走"**影子**写入 + 事后换算成增量"，
    而直跑**没有影子**（P4 要的正是这个）⇒ 直接写字段 ✓。字段名沿用 `effectvm.SET_ENC_FIELDS`
    那一套（`attack`/`attackBuff`/`defense`/`kredit`/`kreditBuff`）—— 与我们的 `U.atk/dfn/cost` 名字不同，
    所以这里做一次映射（映射表只有一份，在 `effectvm`）。

    ★ 这些字段是**基础值**（原版 `getAndDecryptX` 读的就是它）：
      攻/费的**总量** = `clamp(基础 + buff)`（原版 `getTotalAttack`/`getTotalKreditCost`）；
      防御/重甲没有独立 buff 字段 ⇒ 直接写总量。
    ★ **叶写入自己不夹**：夹取发生在**调用点**（如 `ChangeAttack` 的 set 家族送的是 `clamp(amount)`）✓。
    """
    if field == "attack":
        base = int(value or 0)
        card.atk = clamp_stat(base + int(getattr(card, "atk_buff", 0) or 0))
    elif field == "attackBuff":
        new_buff = int(value or 0)
        base = int(getattr(card, "atk", 0) or 0) - int(getattr(card, "atk_buff", 0) or 0)
        card.atk_buff = new_buff
        card.atk = clamp_stat(base + new_buff)
    elif field == "defense":
        card.dfn = float(value or 0)
    elif field == "kredit":
        base = int(value or 0)
        card.cost = clamp_stat(base + int(getattr(card, "cost_buff", 0) or 0))
    elif field == "kreditBuff":
        new_buff = int(value or 0)
        base = int(getattr(card, "cost", 0) or 0) - int(getattr(card, "cost_buff", 0) or 0)
        card.cost_buff = new_buff
        card.cost = clamp_stat(base + new_buff)
    else:
        raise ValueError("set_encrypted_field：未知字段 %r（见 effectvm.SET_ENC_FIELDS）" % (field,))


def apply_veteran(card, vet) -> list:
    """`MakeVeteran(class UBaseCardObject* card, int& qqq)`（`BP_CardFunctions.cpp:7127`，逐行读过）的**规则本体**。

    ★ 2026-10-04（P4 第二十二刀）：**从 `sim/engine.py:638-675` 下沉到 engine**（规则下沉 §5 步骤 3），
    实现与那段一字不差；`sim` 侧改成转调（规则只写一处），`_event_fx_apply(s, "veteran", …)` 的**扇出留在调用方**。

    门槛：**在场 ∧ 有 `_vet` 静态卡（⇒ `vet` 不是 None）∧ 还不是老兵（`veteran ∉ kw`）∧ 总防 > 0**。
    通过后：`attack/defense/operationCost/heavyArmor` 按 `_vet` 的**绝对值**改写
    （防御同时置 `maxDefense`；buff 字段不动），关键词直接赋成 `_vet` 的值；三条附带规则：
    `blitz ⇒ sick=False`、新获得 `fury` 且"还有攻击次数（或已行动）" ⇒ `attacks_left += 1`、
    获得 `guard` ⇒ 去掉 `smokescreen`。

    `vet` 载荷（`atk_to/atk_from`、`dfn_to`、`opc_to/opc_from`、`armor_to/armor_buff`、`kw` 表）
    在**录制期**由注入视图 + `<名>_vet` 静态卡算出（`engine.effectvm._veteran_payload`）——
    直跑由**调用方**用同一算法喂（存活视图是它的事 ✓）；`vet=None` ⇒ 只打标记 + 记缺口。
    返回缺口清单。
    """
    gaps: list = []
    if "veteran" in (getattr(card, "kw", ()) or ()) or float(getattr(card, "dfn", 0) or 0) <= 0:
        return gaps
    card.kw = set(getattr(card, "kw", ()) or ()) | {"veteran"}
    if not isinstance(vet, dict):
        gaps.append("veteran：没取到 `_vet` 静态卡数值（只打标记，数值改写未结算）")
        return gaps
    if vet.get("atk_to") is not None and vet.get("atk_from") is not None:
        card.atk = int(card.atk) + int(vet["atk_to"]) - int(vet["atk_from"])
    if vet.get("dfn_to") is not None:
        card.dfn = float(vet["dfn_to"])
        card.mdef = float(vet["dfn_to"])
    if vet.get("opc_to") is not None and vet.get("opc_from") is not None:
        card.opc = max(int(card.opc) + int(vet["opc_to"]) - int(vet["opc_from"]), 0)
    if vet.get("armor_to") is not None:
        card.armor = min(3, max(0, int(vet["armor_to"]) + int(vet.get("armor_buff") or 0)))
    had_fury = "fury" in (getattr(card, "kw", ()) or ())
    for kwn, on in (vet.get("kw") or {}).items():
        if kwn == "guard":
            continue                       # guard 另算（卡自带 guard 自定义能力时不改，旧实现读不到）
        card.kw = (set(card.kw) | {kwn}) if on else (set(card.kw) - {kwn})
    if (vet.get("kw") or {}).get("guard") is not None:
        g_on = bool(vet["kw"]["guard"])
        card.kw = (set(card.kw) | {"guard"}) if g_on else (set(card.kw) - {"guard"})
        if g_on:
            card.kw = set(card.kw) - {"smokescreen"}      # BP：获得 guard ⇒ RemoveSmokescreen
    if "blitz" in (getattr(card, "kw", ()) or ()):
        card.sick = False
    if (vet.get("kw") or {}).get("fury") and not had_fury \
            and (int(getattr(card, "attacks_left", 0) or 0) > 0 or getattr(card, "acted", False)):
        card.attacks_left = int(getattr(card, "attacks_left", 0) or 0) + 1
    gaps.append("veteran：guard 的『卡自带 guard 自定义能力则不改』、destruction、"
                "ExecuteOnOtherCardsAbilitiesChanged 未建模")
    return gaps
