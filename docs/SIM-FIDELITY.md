# SIM-FIDELITY —— `sim/` 照原版移植的原则与对照表（2026-10-03，用户定）

> **用户原话**：“sim 的各种数据结构和函数最好按照 IDA 或 FModel 的逆向结果来。如果全部按原版实现改，这类问题便绝不会存在。”
> 起因：支援线已满时脚本去打 PLAN WEST（“往支援线加两张 LEGIONS”），sim 无条件生成一个**通用 2/2**，估出 +5.38，实际空打。
> 根子不是少了一个 `if`，而是 `sim` 里的规则是**凭印象手写的近似**，不是**原版代码的移植**。

## 1. 原则

1. **移植，不是设计。** `sim` 里每一条规则、每个常量、每个数据结构，都必须能指到一份**原版出处**：
   FModel 导出的 BP（`reverse-data/exports-1.60.27292.launcher-only-decompiled-BP/…`，函数名 + 行号）或 IDA 里的原生函数（地址）。
   先读原版，再写 Python；不是先写 Python 再找依据。
2. **出处写在代码旁边。** 约定的注释写法：`# 原版：BP_CardFunctions::SpawnCardToBoard（BP_CardFunctions.cpp:19212）`。没有这行的规则 = 待审计。
3. **不编数。** 原版里读不到 / 没移植的部分：记缺口、价值 0，**不**按“估一个像样的值”顶替（CLAUDE.md 弯路 #38）。
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
| 攻击合法性 `can_hit_unit` / `can_hit_hq` / `_rows_ok` | `cardsCheckFunctions::CanAttack`（外部 VM 已能跑，见 `semantics/legality.py`） | **近似** | 现在是“规则百科 + OCR 上游用户确认”的手写表（`_rows_ok`）；应改为移植 `CanAttack` 的分支或直接复用 VM 判据 |
| 攻击结算 `sim_attack` | `BP_CardFunctions::ExecuteAttack` / `DamageCard` / `ApplyDamageToCard`（含重甲、冲击、伏击、反击、`OnAfterAttack`…） | **近似** | 无钩子管线时按“近似结算”（伏击/冲击/反击手写）；有钩子管线（`attack_fx`）时用 VM 给的最终伤害 |
| 伤害与死亡 `deal_damage` / `_apply_death` | `DamageCard` → `DestroyCard`（`OnDestroyed` 0x27 等） | 部分 | 死亡链效果预计算在 `death_fx`，其余已按 BP 读过（见 `sim/effects.py` 注释） |
| 移动上线 `sim_move` | `MoveCardToFrontline` / `CanMoveCardToLocation` / `ExecuteOnCardLocationMoved` | 部分 | 前线容量已按 `row_full`；上线费用、步兵移动后不能攻击等是手写 |
| 回合结束 / 开始 `sim_turn_end` | `EndTurn` / `StartTurn` / `ExecuteOnEndOfTurn` / `OnStartofTurn` 触发族 | 近似 | 指挥点补给、抽牌、疲劳按经验；`pending_cards` / 回合开始钩子按事件处理 |
| 抽牌链 `draw_chain` | `DrawCard` / `DrawCardFromDeck` / `ApplyFatigueDamage` | 部分（按 BP 移植，注释有出处，未在本次审计里复核） | 见 `sim/chain.py` 注释 |
| 疲劳 `apply_fatigue` | `BP_CardFunctions::ApplyFatigueDamage` | 部分（同上） | |
| autoplay 队列 | `GameState::AddAutoPlayCards` / `ExecuteAutoPlayCards` | 部分（按 BP 机制） | 带交互的 autoplay 牌无实例，未专门建模 |
| 压制 | `SuppressMultipleUnits`（BP@7456） | **忠实**（读过 BP） | `sim/effects.py` |
| 战斗 / 打捞 | `MakeCardsFight` / `SalvageCard…` | 部分 | 战斗：`NATIVE-COVERAGE` 标注“BP 体未读，待核”，实现按报告口径；打捞：按注释出处，未逐行复核 |
| 指挥点夹取 | `getMaxPossibleKredits`（GS+0x338） | **忠实** | |
| 偷牌 / 转化 / 老兵 / 治疗 | `ChangeUnitOwnership` / `ConvertCard` / `MakeVeteran` / `FullyHealCard` | 部分 | 转化的后续钩子（0x22 / `OnEnterPlay(5)`）未建模，已记缺口 |
| 群体 buff | `ChangeAttack/Defense` 逐张调用（SCORCHING SUN 2 等） | **忠实**（2026-10-03，`buff_ids`） | |
| 三选一 / 预报 | `selectCardToDraw` / `GetChooseSpawnCards` / `Forecast` / `Array_ShuffleFromStream` | **忠实**（候选由原版字节码 + 活种子算出） | |
| 手牌上限 / 弃牌 | `FetchCardsByLocation(手牌)` 9；`DiscardCard…` | 部分 | 抽牌超上限的处理已按 `draw_chain`；其它入手牌路径（生成、转化）逐个对照 |
| 单位价值 / 评估 | —— 不是原版规则，是**评估层**（`evaluation/`） | 不适用 | 评估层允许自己的启发式，但不得进 `sim` |

## 3. 迁移顺序（按“编数的危害 × 出现频率”）

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
