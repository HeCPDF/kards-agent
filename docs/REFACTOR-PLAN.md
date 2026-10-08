# 重构总计划：从“记录再解释”到“无头游戏”

> 2026-10-03 立；2026-10-08 整理为“目标 + 当前状态 + 设计决策”。起因：board 要用原版数据结构；sim 要照原版实现；agent/ops 职责含混；结构太乱。
> 本文是**目标架构 + 阶段 + 验收**。每阶段结束：离线测试全绿 + 实机训练局能打完一整局（不允许“重构完 bot 不会打了”）。

## 0. 现状的根本问题（不是目录乱，是模型乱）

1. **同一件事有三套数据结构**：游戏的 `UBaseCardObject` → board 的 `Card`（字符串化的派生视图）→ sim 的 `U/H`（又一套）。
   每跨一层就翻译一次，每次翻译都是“编数”的机会（PLAN WEST 空打、MONSOON 当成 +2 都出在翻译里）。
2. **效果走“字典黑话”**：`effectvm` 把游戏的原语调用压成 `{"buff":…,"buff_ids":…,"damage_aoe_ids":…}`，`sim` 再用一堆
   `if e.get("xxx")` 解释回来。词汇表是我们自造的（`test_effect_keys_consumed` 靠测试勉强守着），语义漂移不可避免。
3. **sim 是“近似”**：规则凭印象手写（`SIM-FIDELITY.md` 的“近似”列），不是原版代码的移植。
4. **巨型单文件**：`ops/inject.py` 6.4k 行、`player/rule.py` 3.3k、`semantics/triggers.py` 2.1k、`semantics/effectvm.py` 1.5k、
   `kardsmem/board.py` 1.2k、`kardsmem/cli.py`；职责靠注释区分。
5. **构建/版本数据有两份表**（`build.py` 与 `board.py`），靠自检对齐；RVA 可以运行时扫，却仍有版本→表的登记制。
6. **脚本堆**：`tools/` 40+ 个一次性脚本，和生产代码只隔一个目录。

## 1. 目标架构

```
base/        路径、Win 窗口/截图（无内部依赖）
kardsmem/    读侧：进程内存、UE 反射、Kismet 字节码反汇编与外部 VM、RVA 扫描、版本识别
             └ 产出 **gamemodel.GameState**（原版结构）
engine/      ★ 无头游戏：GameState 的可复制副本 + 原版原生函数的 Python 移植 + 触发分发
  state.py     GameState / BaseCardObject 的 clone（写时复制）、撤销栈
  natives/     BP_CardFunctions / BattleUtilityFunctions 的函数，一个函数一个出处注释（ChangeAttack、DamageCard、
               DestroyCard、MoveCardToFrontline、SpawnCardToBoard、DrawCard…），按族分文件
  triggers.py  CardFunctionTriggers 分发（ExecuteOn*：谁先谁后、互斥、停止传播）——照 BP 的顺序
  scripts.py   用 kardsmem.vm 在 engine 状态上**直接执行**卡牌蓝图字节码（原生调用路由到 natives/）
  rules.py     CanAttack / CanPlayFromHand / CanMoveCardToLocation（VM 判或移植）
  rng.py       FRandomStream（已有）
ops/         写侧：进程内合成输入（recipes/ 手势配方、inject 运行时、js 模板）
agent/       命令层：一组动词（读 + 问合法 + 做）
policy/      搜索与评估：在 engine 状态上展开、评分（evaluation 并入）
player/      玩家：策略、在线回路、录制
interfaces/  shell / MCP        gui/ 面板        tools/ 一次性脚本（devtools）
```

**关键变化**：`sim` + `semantics` 的“记录→字典→解释”被 `engine` 取代。卡牌脚本（蓝图字节码）在 `engine.scripts` 里跑，
它调用的原生函数（`ChangeAttack(card, amount, changeType)` …）**直接是 engine 里的移植实现**，作用在 `GameState` 副本上。
不再有 `buff`/`buff_ids`/`retreat_ids` 这类中间词汇；也就没有“词汇没人消费 / 消费错了”这类 bug。

## 2. 阶段（每阶段独立可交付，顺序固定）

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P0 整理**（已做大半） | 分层包、依赖方向测试、去上游遗留、路径统一、发布清理 | `tests/run_all.py` 全绿；`docs/STRUCTURE.md` |
| **P1 原版数据模型** | S1 ✓ gamemodel；S2 ✓ 读侧产出；S3 ✓ 座位改 `ESide`+`mySide`、位置改 `obj` 谓词（`docs/SEAT-MIGRATION.md`）；S3' 其余字段别名→`obj.xxx`；S4 删别名 | 无代码再读 `Card.side="local"` 字符串；假卡全换 `BaseCardObject`；`BOARD-MODEL.md` |
| **P2 engine 骨架** | `engine.state`（clone/undo）、`engine.triggers`、`engine.natives` 的**第一批**（现有 sim 里已移植过的：容量、SpawnCardToBoard、抽牌链、疲劳、压制、指挥点）；`sim` 变成 engine 的薄壳 | 现有 sim 测试通过；`test_sim_source_tags` 棘轮转到 engine，数量只升 |
| **P3 natives 全量移植** | 按“被卡牌脚本实际调用的频次”从高到低移植：ChangeAttack/Defense/OperationCost（含 EChangeType）、DamageCard/ApplyDamageToCard、DestroyCard、MoveCardToFrontline、ConvertCard、MakeCardRetreat、TakeControl… | 每个 native 有“原版：函数名（文件:行）”注释；`SIM-FIDELITY.md` 表里“近似”清零 |
| **P4 scripts 直跑** | `engine.scripts`：VM 在 engine 状态上直接执行 `OnPlayedFromHand` 等钩子；`effectvm` 降级为“记录模式”（仅用于覆盖缺口审计），`to_effects` 字典词汇表删除 | `test_effect_keys_consumed` 退役；同一批卡牌的直跑结果与旧记录路径逐卡对账一致（对账脚本） |
| **P5 搜索迁移** | `policy` 直接在 engine 状态上搜索（clone + apply 动作 = 调原生函数）；评估并入；`boardeval` 门面删除 | 实机训练局胜率/决策耗时不退化（B1 同口径） |
| **P6 巨型文件拆分** | `ops/inject.py` → `ops/{runtime,gestures,verbs/*}.py`（D1）；`player/rule.py` → `player/strategy/*`；`kardsmem/cli.py`、`board.py` 拆读写/自检 | 单文件 ≤ 1500 行（生成物除外）；`ops/` 的 lint 棘轮继续 |
| **P7 构建自包含** | RVA 只走“用户缓存 → 种子表复验 → 扫描”（S3/H2）；删 `VERSION_TO_BUILD` 与双份表；`KARDS_BUILD` 仅作调试覆盖 | 未登记版本也能起来；`build_tables.json` 只是种子 |
| **P8 发布** | README / CHANGELOG / requirements / install & 运行脚本 / `make_release.py` / CI；干净导出的离线测试；按“发布纪律”只推子树 | 空目录里 `pip install -r requirements.txt` + `python tests/run_all.py` 全绿 |

## 3. 做法与护栏

* **每个 PR 一个阶段内的一件事**，提交信息写“阶段-要点”；不跨阶段混改。
* **测试先行**：迁移前先加“同源 / 等价”测试（旧路径与新路径对同一输入输出一致），再删旧路径。
* **出处棘轮**：engine 里每个移植的函数/分支必须带 `原版：<BP 函数>（文件:行）`，数量只升不降（`tests/test_sim_source_tags.py` → `test_engine_source_tags.py`）。
* **不编数**：读不出 ⇒ `None` + 缺口，沿用 同类教训。
* **实机回归**：P1/P2/P4/P5 结束各跑一局训练局（`gui` 的 autoplay），对比 `rule-live-*.jsonl` 的 `t_decide`、问题数、动作质量。
* **监听器**：改了 `player.rule` 等被热重载的模块后，**重启监听器**再开局（`C2` 已修，但仍以重启为准）。

## 4. 当前状态（2026-10-08）

| 阶段 | 状态 | 剩余 |
|---|---|---|
| P0 整理 | 基本完成：分层包、依赖方向测试、路径统一、旧代码清理 | `tools/` 里仍有较多一次性脚本 |
| P1 原版数据模型 | S1/S2/S3 完成（`gamemodel`、读侧产出、座位改 `ESide`+`mySide`） | **S3'**（其余字段别名 → `obj.xxx`）、**S4**（删派生视图）未做 |
| P2 engine 骨架 | 完成：`engine.state` / `triggers` / `natives` 首批，`sim/*` 只剩兼容壳 | — |
| P3 natives 全量移植 | 进行中：伤害 / 状态 / 指挥点 / 协力等已移植；工单见 `docs/P3-NATIVE-PORT.md` | 工单里“部分”各行升级为“忠实”；⑪ 族 |
| P4 scripts 直跑 | 大半：`engine.scripts` 直跑缝与状态族 sink 已落，对账已做 | 个别钩子（如 BLITZKRIEG/DIVE BOMBING/PATTON 的回合钩子）VM 抛 `TypeError`，记为缺口 |
| P5 搜索迁移 | 影子对账（A/B/C）与离线批量对账已落；随机/抉择口径见 §5.7 | **搜索本身尚未切到 engine**，`boardeval` 门面未删；位置状态见 §5.4 |
| P6 巨型文件拆分 | `ops/inject.py` 已按职责拆开 | `player/rule.py`（~3.3k 行）、`kardsmem/cli.py`、`kardsmem/board.py` 未拆 |
| P7 构建自包含 | 离线部分完成（缓存 → 种子表 → 扫描；`build_tables.json` 单份） | 其他版本的实机验证（S2） |
| P8 发布 | 面板 exe 包与 `make_release` 已可用 | 整理完成后重新发布 |

## 5. 设计决策

### 5.4 架构需求（用户 2026-10-06）：**选择单位进场位置 / 上线位置**必须能被新架构覆盖

现状（实测）：`engine.state.U` 只有 `row`（`"frontline"/"back"`），**没有槽位下标**；`guarded` 是个布尔、不是由相邻关系算出来的；
搜索的动作 `deploy/move` 没有位置参数（执行侧 `ops.play` 的 `placement` 一律 `first_free_slot`）。但**位置在原版里是有语义的**：
* 守护（Guard）是**相邻**语义（"与守护单位相邻的非守护单位无法被轰炸机和炮兵以外的单位攻击"）⇒ 攻击合法性依赖支援线上的相邻关系；
* `GetAdjacentCards`（效果伤害/加成的相邻溅射，VM 里已有 `adjacent_cards`）、协力（bond）、各类『相邻』卡；
* 上线（move 到前线）也有落点（前线被占 ⇒ 拒绝；见 SIM-FIDELITY 的移动行）。
**要求**（做进 P5，不另起炉灶）：
1. **状态**：`U` 增加槽位（支援线下标 / 前线序位），来源 = 原版 `locationNumber`；快照→`Sim` 适配器读它；`guarded` 改成由相邻关系**现算**（不再是输入）。
2. **动作带位置**：`deploy`/`move` 的动作与 `Prompt` 路径里有『选位置』这一步（与『选目标』同级的选择），执行侧把位置传给 `ops.play`（现在固定 `first_free_slot`，`placement` 字段已能回报请求位置）。
3. **搜索**：位置只在**有区别时**才枚举（场上有守护/相邻效果的牌、手里有相邻效果牌、被相邻依赖的目标）；否则用规范位置，避免分支爆炸（与『不降束宽』的拍板兼容——靠剪枝不靠降束宽）。
4. **规则来源**：位置合法性/落点规则从原版移植（`GameState` 里放置相关函数，**待在 BP 导出里定位**，不猜）；空位判定、前线占用、`PayMovementCost` 已有部分沿用。
5. **验收**：影子对账里加『位置敏感』的牌（带相邻效果、守护相邻的攻击合法性）；实机上用不同落点打出同一张牌，sim 预测的攻击合法性与游戏的 `CanAttack` 一致。

### 5.7 2026-10-06 用户拍板：随机与抉择的评估口径（P5 评估/搜索）
* **随机结果不视作分支**：直接用牌局随机流（FRandomStream，种子来自游戏）跑出具体结果；`outcomes` 枚举 / `shadow_branch_cap` 逐分支比较只是读不到种子时的权宜。
* **抉择类（二选一）拆成独立动作**，同三选一（choose-spawn）：每个选项一个候选，不是一个带分支的合并评估。
* 随机池是否排除预备卡看卡自己的 BP（`GetAllActiveStaticCards` 自带排除；卡没排除就不排）。

**落地（2026-10-06，P5 搜索/对账侧）**
* **盘点（改前）**：搜索侧其实早就是这个口径——`_vm_eff` 把 `rng_seed` 传给 `enumerate_effects`（有流 ⇒ `RandomIntFromRangeWithStream`/`GetRandomCard`/`GiveRandomCombatKeyword` 给确定结果、不进 `nodes`），
  抉择在 `sim.engine.prompt_of/run` 挂成 `choose_one` 提示、`policy.search.expand_paths` 展开成**每个选项一个行动**（`A.path=(i,)`、各带自己的效果；`rule._act_of` 把路径交给 `PlanStore`，执行侧 `policy.answer.plan_choose_one` 照点，
  现有的 `pick` + `choose_one_with_target` 动词，没有新增 ops 动词）；`_apply_eff` 对残留的 `outcomes`/`chance` 只应用公共部分并记缺口（M1：评估层不见随机、不取期望）。
  **唯一还把"随机/抉择"当分支合并比较的是影子对账 `_shadow_check`**（`nodes` + `_combos` + `outcomes` 一一对应、`shadow_branch_cap` 封顶；抉择与随机混在同一套组合里、一张牌只记一个"最差"结论）。
* **改后**：`_shadow_check` 经 `rule._shadow_branches` 分类——① 有种子：随机不枚举，A/B 同种子（B 的直跑 sink 与录制器共用同一个 `Stream`，抽取顺序一致；C 重放的是 B 已定的结果）只比一个具体结果；
  ② 抉择：每个选项一个**独立条目**（`[选项i]`，多个抉择点 `[选项i,j]`，缓存键尾带选择路径，不受 `shadow_branch_cap` 限制，与搜索里的行动一一对应）；
  ③ 有种子却仍有没接进流的随机点（`r["chance"]` 非空）⇒ skipped、原因点名动词，**不枚举成分支假装比过**；④ 只有读不到种子（`use_rng=False`）才退回旧枚举（`[随机分支i]`，cap 封顶）。
  结果带 `rng_exact`/`choice_actions`/`enum_fallback` 计数。离线工具：读转储里的牌局流种子（`kardsmem.rng.read_seed` 在离线内存后端同样可用），读不到才用**固定**假种子并在报告 `runs[].rng_fake` 标明；
  报告新增 `cards[*].rng_exact/choice_options` 与 `summary.cards_with_rng_exact/choice_option_entries`。测试 `tests/test_random_choice_split.py`、`tests/test_shadow_check.py`。
* **仍待**：`DiscardRandomCardFromHand`（KRIEGSMARINE/MOLOTOV COCKTAIL/WOLFPACK）A/B 都只记缺口（Sim 不跟踪对方手牌身份，无法替游戏选一张）；**探针实测**（338 张指令 × 同一转储、有种子）：直跑里**没有任何残留随机点**，只剩 17 张牌的 `WhichChooseOne` 抉择节点（CALL TO THE COLONIES/CARRIER COVER/FORM BATTLE LINE/PLAN D/MEN OF STEEL/PLANNED ATTACK/RATIONING/SHOCK TACTICS/SPARS/STRATEGIC FOCUS/US WEATHER BUREAU 与各 RESEARCH）——随机全部按牌局流算成确定结果；
  VM 里没按流实现的随机原语（`RandomIntegerInRange*`/`Array_Random*`/`Array_ShuffleFromStream`）目前没有牌触到，万一触到按上面 ③ 记 skipped（不是静默通过）；
  `enumerate_effects` 在**无种子**且抉择+随机混合时仍把两者展成同一组 `outcomes`（`outcomes_mode=max` 会把随机结果当选项）——只在无种子兜底路径上出现，实机 `_rng_seed()` 读不到时用假种子，不会走到。
