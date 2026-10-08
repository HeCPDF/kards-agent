# SIM-FIDELITY —— `sim/` 照原版移植的原则与对照表（2026-10-03，用户定）

> **用户原话**：“sim 的各种数据结构和函数最好按照 IDA 或 FModel 的逆向结果来。如果全部按原版实现改，这类问题便绝不会存在。”
> 起因：支援线已满时脚本去打 PLAN WEST（“往支援线加两张 LEGIONS”），sim 无条件生成一个**通用 2/2**，估出 +5.38，实际空打。
> 根子不是少了一个 `if`，而是 `sim` 里的规则是**凭印象手写的近似**，不是**原版代码的移植**。

## 1. 原则

1. **移植，不是设计。** `sim` 里每一条规则、每个常量、每个数据结构，都必须能指到一份**原版出处**：
   FModel 导出的 BP（`reverse-data/exports-1.60.27292.launcher-only-decompiled-BP/…`，函数名 + 行号）或 IDA 里的原生函数（地址）。
   先读原版，再写 Python；不是先写 Python 再找依据。
2. **出处写在代码旁边。** 约定的注释写法：`# 原版：BP_CardFunctions::SpawnCardToBoard（BP_CardFunctions.cpp:19212）`。没有这行的规则 = 待审计。
3. **不编数。** 原版里读不到 / 没移植的部分：记缺口、价值 0，**不**按“估一个像样的值”顶替（同类教训）。
   通用 2/2、`front_n < 5` 这类常数都属于“编的数”。
4. **数据结构跟着原版的字段走。** `sim` 的状态量对应游戏里的字段（`GameState` / `BaseCardObject` 的字段名与偏移见 SDK dump），
   名字、取值范围、谁在谁的 `TArray` 里都尽量一致；做不到一致的（比如我们的 `U.row` 把支援线叫 `back`）在这里登记映射。
5. **来源优先级**：IDA / 导出 BP 字节码（一手） > 运行时读到的游戏数据 > 规则百科 / 经验（二手，只能当线索，不能当依据）。
6. **能直接跑原版就别重写**：能用 VM 跑原版字节码的（`effectvm` / `kardsmem.vm`），sim 只管**状态变更**，不重写判据（“判据负责挑，游戏负责判”）。

## 2. 对照表（现状审计，2026-10-03）

状态：**忠实**＝逐行对过原版；**部分**＝关键分支对过、仍有近似；**近似**＝手写、依据是二手资料；**缺**＝没建模。

| 模块 / 规则 | 原版出处（待对照的函数） | 状态 | 已知偏差 / 备注 |
|---|---|---|---|
| 每排容量（`row_full`） | `BP_GameState_Battle::FetchCardsByLocation`（手牌 9；支援线含总部 5；前线 5，`IsFrontlineLimited` 时 2） | **忠实**（2026-10-03） | `IsFrontlineLimited` 的真值未读，`front_limited` 恒 False |
| 生成单位 | `BP_CardFunctions::SpawnCardToBoard`（排满且无指定 id ⇒ 不生成；前线被占 ⇒ 拒绝） | **忠实**（2026-10-03） | 生成单位的**关键词**没读；`enterPlayOnTurn`、`OnEnterPlay(3)`、`GiveAlpineBonus` 后续钩子未建模 |
| 部署合法性 | `BattleUtilityFunctions::CanMoveCardToLocation`（满且非指令 ⇒ 不能）+ 各卡 `CanPlayFromHand` | 部分 | 搜索只查“排满”；其余交给游戏裁决（动作照发） |
| 攻击合法性 `can_hit_unit` / `can_hit_hq` / `_rows_ok` | `cardsCheckFunctions::CanAttack`（BP `:185-204` `not_enough_range`、`:206-242` 护卫、`:244-296` `fighter_protecting`；外部 VM 版见 `semantics/legality.py`） | **忠实**（2026-10-04） | 已从"规则百科"换成**原版规则**：① `not_enough_range` = `攻击者不在前线 ∧ 目标不在前线 ∧ range < 2` ⇒ 打不了（`U.rng` 从快照 `BaseCardObject::range @0x78` 读，SDK `kards_classes.hpp:2439`）；② 护卫检查对 **`IsBomber() || IsArtillery()`** 放行（旧实现只豁免炮兵 ⇒ 轰炸机被误拒）；③ `fighter_protecting` **只有轰炸机**查、且看**被攻击那一行**（旧实现看"敌方后排"⇒ 行取错）。**未建模（如实记）**：HQ 的 `location_has_smokescreen`、未揭示隐蔽战斗机的豁免、`IsThereGameplayRestriction("cant_attack_with_ground_units")`、`cant_be_attacked_by_unit`/`CanSelectAsTarget` 的自定义能力保护；"这个单位还能不能动"那一族（`unit_is_pinned`/`deployment_sickness`/`has_already_attacked`/`no_attack_left`/`not_enough_kredits`）在**动作生成**里判。`U.rng is None`（老 fixture）才退回旧表，实机快照总带 range。测试 `tests/test_attack_legality.py`（14 项，每条判据两个不同输入的对照） |
| 攻击结算 `sim_attack` | `AttackCard`(`:16771`) → **`CalculateDamageDealt`**(`:14809-15167`) → `ExecuteAttackCard`(`:17133+`) → `DamageCard`/`ApplyDamageToCard` | **战斗路径已按原版**（2026-10-04） | 真链路已定（`ExecuteAttackCard` 的两个伤害都是**入参**，逻辑在 `CalculateDamageDealt`）。已按原版落地：**伏击**（`:14868-14949`，含 `¬hasBeenAttackedThisTurn`、`getHasImmune`、重甲计入致死判据）、**报复豁免**（`:14955-15005`，看**攻击方**类型：炮兵/轰炸机不吃还击，轰炸机在防守方是战斗机/防空时照常吃）、**`lethal`**（`:15076-15095`，扣完重甲的伤害 >0 ∧ 目标非总部 ⇒ 直接斩杀）、**`CantLoseShock`**（不带才摘冲击）、**`isImmune`**（`:14823-14836` 免疫 ⇒ 吃 0 伤害且不死；战斗与效果伤害都走 `engine/natives/damage.py::deal_damage` 的免疫早退）、**`excess`**（`:16489-16498` 溢出转打敌方总部）、**重甲**（`:15033-15047`，只作用于战斗伤害 ✓ 与 `deal_damage(engage=True)` 一致）。**未建模（如实记）**：`antiair` 类型、HQ 的免疫/烟幕（`s.hq` 是纯数字）、`BP_Logic` 预览路径才用的 `applyBeforeAttackBuffs`（真实战斗调用点 `:17065/:17075` 传 `false` ⇒ **不影响**） |
| 伤害与死亡 `deal_damage` / `_apply_death` | `DamageCard` → `DestroyCard`（`OnDestroyed` 0x27 等） | 部分 | 死亡链效果预计算在 `death_fx`，其余已按 BP 读过（见 `sim/effects.py` 注释）。★ 2026-10-03：**防御降到 ≤0 ⇒ 摧毁**已接（原版 `ChangeDefense` 的 `Label_984`/`Label_1995`，三条防御通道走同一个 `_def_delta`；升防御不摧毁）；`OnAfterDefenseIsSet` + `trigger 0x6`、`maxDefense` 抬高仍未接 |
| 移动上线 `sim_move` | `MoveCardToFrontline`(`BP_CardFunctions:17560-17629`) / `PayMovementCost`(`:19873-19898`) / `CanMoveCardToLocation` / `ExecuteOnCardLocationMoved` | **已按原版**（2026-10-04） | ① 费用 = **`getTotalOperationCost()`**（`:17617-17619`；适配器 `opc` 同源），且**先过 `PayMovementCost` 的"够不够"闸门**（`kredits < cost` ⇒ **移动整个不发生**，`:19883-19888` / `:17612`）—— 旧行"上线费用是手写"**不准确**：扣费口径本来就一致，缺的只是闸门，现已补；② `CanMoveAndAttackInTheSameTurn()` 假 ⇒ `attackLeft = 0`（步兵移动后不能攻击），真 ⇒ `movementLeft -= 1`（装甲 / `move_attack` 伪关键词）✓ 已有；③ 前线容量按 `row_full` ✓；④ 上线钩子走 `event_fx["move"]`（0x32）✓。**未建模（如实记）**：`CardLocationMoved` 的通知、`OperationKreditsSpent` 的旁观者钩子（评估用不着）。测试 `tests/test_move_cost.py` 4 项 |
| 回合结束 `sim_turn_end` / 回合开始 `sim_turn_start` | `ExecuteEndOfTurnEvents`(`BP_CardFunctions:13081`) + `ExecuteEndOfTurnQueue`(`:13129`)、`RemoveBuffsEndOfTurn`(`:21680`)、回合开始流程 `BP_Logic:10010-10077` | **已按原版**（2026-10-04） | ① 回合结束：先 0x19 钩子（`ExecuteEndOfTurnEvents` → Queue），**最后**清 buff（`RemoveBuffsEndOfTurn` 在 Queue 内 `:13247`）；而"要清的 buff"全导出**只有一种** —— 唯一的登记点 `AddAttackUntilEndOfTurn`(`:2992`) → `AddBuffsToRemoveEndOfTurn(0x0, …)`(`:3006`) ⇒ 清**临时攻**（我们的 `atk_turn` 就是它）✓。② 回合开始（**新增** `sim/engine.py::sim_turn_start`）：槽 +1（`CanSideGainKreditSlots`(`BP_Logic:7999`) ∧ `slot < MaxKreditsConst`）→ `SetKreditsAndKreditSlots(side, slot, slot, 0)`(`BP_CardFunctions:19543`，夹 `[0, getMaxPossibleKredits()]`) ⇒ **指挥点补满到槽数** → `ExecuteBeforeStartOfTurnEvents` → `CanSideDrawCards` 抽 1（**第 1 回合不抽**，`:6318-6336`）→ `GiveMobilizeBonus` → `ExecuteStartOfTurnEvents(turn)`。③ 抽牌/疲劳**早就是原生移植**（`engine/chain.py::draw_chain`、`engine/natives/damage.py::apply_fatigue`）⇒ 旧行"按经验"的说法**过时**。**未建模（如实记）**：`GiveMobilizeBonus`、`KreditCheckAndAutoBanIfNeeded`(`:10065`)、`MaxKreditsConst` 的真值（运行时属性 `@0x4B4`，退到 `kredit_slot_max` 缺省 12）、③⑥ 钩子的后果（rule 侧目前只预计算了 `turn_end`）。测试 `tests/test_turn_start.py` 5 项 |
| 抽牌链 `draw_chain` | `DrawCard` / `DrawCardFromDeck` / `ApplyFatigueDamage` | 部分（按 BP 移植，注释有出处，未在本次审计里复核） | 见 `sim/chain.py` 注释 |
| 疲劳 `apply_fatigue` | `BP_CardFunctions::ApplyFatigueDamage` | 部分（同上） | |
| autoplay 队列 | `GameState::AddAutoPlayCards` / `ExecuteAutoPlayCards` | 部分（按 BP 机制） | 带交互的 autoplay 牌无实例，未专门建模 |
| 压制 | `SuppressMultipleUnits`（BP@7456） | **忠实**（读过 BP） | `sim/effects.py` |
| 战斗 / 打捞 | `MakeCardsFight` / `SalvageMultipleUnits` | 部分 | 战斗：`NATIVE-COVERAGE` 标注"BP 体未读，待核"，而 `engine/natives/damage.py` 写"2026-10-03 读过" ⇒ **口径冲突、未核**（`P3-NATIVE-PORT.md` R7）；打捞：按注释出处，未逐行复核 |
| 指挥点夹取 | `getMaxPossibleKredits`（GS+0x338） | **忠实** | |
| 偷牌 / 转化 / 老兵 / 治疗 | `ChangeUnitOwnership` / `ConvertCard` / `MakeVeteran` / `FullyHealCard` | 部分 | 转化的后续钩子（0x22 / `OnEnterPlay(5)`）未建模，已记缺口 |
| 群体 buff | `ChangeAttack/Defense` 逐张调用（SCORCHING SUN 2 等） | **忠实**（2026-10-03，`buff_ids`） | |
| 卡面数值改动 + `EChangeType` | `BP_CardFunctions::ChangeAttack`（:10981）/ `ChangeDefense`（:11215）/ `ChangeHeavyArmor`（:10320）/ `ChangeOperationCost`（:8892）；`EChangeType`（SDK dump） | **部分**（2026-10-03 P3：四函数逐行读完；"基础字段 + 独立 buff 字段"落地；`SetValue` 折算 buff-aware；**修掉一条活死代码**） | 修掉：① `SetValue` 会抹掉挂着的 buff（现在 `d = clamp(n)+buff−cur` ⇒ 5 攻+50 buff 设成 2 得 **52**）；② `set_attack_buff`/`set_kredit_buff` 以前**记缺口丢掉**、现在如实消费；③ **`Card.total_attack`/`total_operation_cost` 曾是方法** ⇒ `cur_stats` 读不到 ⇒ `attack`/`opcost` 的 SetValue 分支**一直静默丢效果**（改成 property + 抽出 `_fill_cur_stats` 并测真路径）。**激活后行为会变，必须实机复核**。另修：重甲 `SetValue` 现按 **`[0,3]`** 夹（R12）、三个 `getTotal*` 补 **`clamp(0,99)`**（R13：原来 90 攻 + 50 buff 读成 **140**，游戏是 99）、**手牌费用 `ChangeKreditCost` 从"产出但没人消费"接上**（R14：改发逐卡字典 `cost_ids` ⇒ `Hand.cost`；其 `5`/`6-9` 按原版只发通知、不改值）、**普通加减通道也按原版夹总量**（R11：`buff`/`buff_ids`/`attack_turn`/`opcost` 夹 `[0,99]`、`armor` 夹 `[0,3]`；修前 90 攻 +50 读成 140）。未做：`ChangeDefense`/`ChangeOperationCost`/`ChangeHeavyArmor` 未接线；**R11** 总量未夹 `[0,99]`；**R12** 重甲/费用的 `SetValue` 走通用兜底被当增量记 —— 见 `docs/P3-NATIVE-PORT.md` |
| 三选一 / 预报 | `selectCardToDraw` / `GetChooseSpawnCards` / `Forecast` / `Array_ShuffleFromStream` | **忠实**（候选由原版字节码 + 活种子算出） | |
| 手牌上限 / 弃牌 | `FetchCardsByLocation(手牌)` 9；`DiscardCard…` | 部分 | 抽牌超上限的处理已按 `draw_chain`；其它入手牌路径（生成、转化）逐个对照 |
| 单位价值 / 评估 | —— 不是原版规则，是**评估层**（`evaluation/`） | 不适用 | 评估层允许自己的启发式，但不得进 `sim` |

## 3. 迁移顺序（按“编数的危害 × 出现频率”）

> ★ 2026-10-03：**具体工单（顺序、原版行号、缺口编号 R1…）已移到 `docs/P3-NATIVE-PORT.md`**；
> 本节只保留原则性次序。注意该工单 §0 的口径修正：计划书 P3 点名的那些是 **BP 动词**，
> 不在原生普查报告里，**顺序不能由那份报告的频次表生成**。

1. **攻击合法性**：把 `_rows_ok` / `can_hit_*` 换成 `CanAttack` 的移植（或直接用 VM 的 `Legality`）。
2. **攻击结算**：照 `ExecuteAttack` → `DamageCard` 的 BP 逐行移植（重甲、冲击、伏击、反击、`OnAfterAttack`、狂怒），去掉“近似结算”分支。
3. **回合开始 / 结束**：照 `StartTurn` / `EndTurn` 与各触发号的顺序。
4. **移动 / 上线费用与限制**。
5. **其余效果键**逐个对照（`_apply_eff` 里每个 `e.get(...)` 分支都要有原版出处注释）。
6. 加一条 lint（`test_sim_source_tags.py`）：`sim/engine.py` 里每个 `_apply_eff` 分支与规则函数必须带“原版：”出处注释，数量只许升不许降（棘轮，同 `test_ops_rules.py`）。

## 4. 工作方式

* 动 `sim/` 之前先查对照表对应行的原版函数，读完再改；改完更新这张表的状态与偏差列。
* 发现手写近似造成错误（像 PLAN WEST 这次）：**不要只补那一个 `if`**——回到表里把整行升级成“忠实”。
* 评估层（`evaluation/`）永远不得向 `sim` 注入规则；规则只来自原版。
