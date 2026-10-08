# 重构总计划：从“记录再解释”到“无头游戏”

> 2026-10-03。起因：用户连续指出——board 要用原版数据结构；sim 要照原版实现；agent/ops 职责含混；结构太乱；“重构规模可能还小了”。
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
interfaces/  shell / MCP        gui/ 面板        learn/ 离线学习        tools/ 一次性脚本（devtools）
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
* **不编数**：读不出 ⇒ `None` + 缺口，沿用 `CLAUDE.md` 弯路 #38。
* **实机回归**：P1/P2/P4/P5 结束各跑一局训练局（`gui` 的 autoplay），对比 `rule-live-*.jsonl` 的 `t_decide`、问题数、动作质量。
* **监听器**：改了 `player.rule` 等被热重载的模块后，**重启监听器**再开局（`C2` 已修，但仍以重启为准）。
* **回滚**：每阶段打 tag（`refactor-P1` …），出问题可整阶段回退。

## 4. 立即要做的（P1 收尾 → P2 开头）

1. `sim.adapter.from_cards` 读 `card.obj` + `game.mySide`（保留旧路径给尚未迁移的假卡，标记为待删）。
2. `semantics/*`、`policy/*`、`player/rule.py`、`agent/view.py`、`interfaces/*`、`learn/schema.py` 逐个迁；每迁一个包就删它对派生视图的依赖。
3. 假卡（测试里的 `_C`/`C` 一类）统一换成 `kardsmem.gamemodel.BaseCardObject` 的工厂 `tests/_cards.py`。
4. 开 `engine/` 包：先把 `sim/state.py` 的容量常量、`row_full`、`SpawnCardToBoard` 端口搬进 `engine/natives/board.py`，`sim` 引用它。

## 5. 进度（2026-10-03，DS）

* **P2 已落十刀**（每刀独立提交、相关测试全绿；全量 74 个 0 失败）：
  1. `engine/natives/board.py`（容量/`row_full`/`SpawnCardToBoard`；容量单一来源 `gamemodel`）
  2. `engine/natives/{damage,status}.py`（`deal_damage`/`apply_fight`/`apply_fatigue`/`apply_suppress`）
  3. `engine/natives/kredits.py`（指挥点/槽夹取）
  4. `engine/state.py`（`U/H/Sim/State` + 常量；`sim.state` 变壳）
  5. `engine/dispatch.py`（事件队列/分发器；`sim.dispatch` 变壳；热重载清单补 engine 模块并排在 sim 壳之前）
  6. `engine/chain.py`（抽牌链/autoplay 冲刷；`DRAW_CAP` 单一来源 `engine.state`）
  7. `engine/natives/cards.py`（收缴；`sim/effects.py` 成纯壳）
  8. `engine/triggers.py`（触发分发语义；`semantics/triggers.py` 成**同一模块对象**别名 —— 测试会直接给
     `_run_hook_ex` 等打补丁，逐名 re-export 会把补丁打在壳上）
  9. `engine/effectvm.py`（录制 VM；`engine.triggers` 的 6 处惰性 import 需要它，engine 不许反向依赖
     semantics ⇒ 两者同刀；`semantics/effectvm.py` 成同一模块对象别名）
  10. `engine/adapter.py`（L0→L2：快照牌 → `Sim` 的 `from_cards`；`sim/adapter.py` 成 re-export 壳，
      旧 `import sim.adapter` / `BE.from_cards` 不变；`boardeval` 门面取的仍是同一函数对象）
* **护栏**：`tests/test_arch_rules.py` 登记 `engine:{base,kardsmem}`、`semantics:+engine`、`sim:+engine`
  （别名 import 方向）+ 两条 `semantics.X is engine.X` 断言 + `sim.adapter.from_cards is engine.adapter.from_cards`；
  `tests/test_sim_source_tags.py` 棘轮扩到 `engine/`（基线 52、合计 67 处，只升）；
  `test_effect_keys_consumed` 按 `engine.effectvm.__file__` 解析。
* **下一步**：**P3 规则函数全量移植** —— 工单见 **`docs/P3-NATIVE-PORT.md`**（2026-10-03 立）→ P4 `scripts` 直跑。
  ★ P3 的**口径修正**（工单 §0）：计划书点名的 `ChangeAttack`/`DamageCard`/`DestroyCard`/`MoveCardToFrontline`
  是 **BP 动词**，在 `NATIVE-COVERAGE-1.60.md` 里 0–1 命中 ⇒ **顺序不能由那份报告的频次表生成**
  （报告频次榜首的是读器，且 `cardnatives` 已逐条复核过）。
  * **P3 已落第一刀**（2026-10-03）：`engine/natives/stats.py` —— `EChangeType` 十个具名常量 + `clamp_stat`/`clamp_armor`
    + `is_set_value`/`writes_buff_field`/`clamp_applies`/`fires_after_change_events`；接线 `effectvm._is_set_change`
    与 `sim/engine.py` 的三个 `set_*` 夹取（**行为零变化**）；`tests/test_native_stats.py`；全量 **75 个 0 失败**。
    出处棘轮随之抬高（`engine/natives/stats.py` 24 处；合计 67 → **94**、基线 52 → **76**）。
  * **P3 第二刀**（2026-10-03，同族补完）：`ChangeDefense`（`:11215-11480`）与 `ChangeOperationCost`（`:8892-9188`）
    **逐行读完**，两条分支表 + 三条 `switch` 表（`ATTACK_SWITCH`/`DEFENSE_SWITCH`/`OPCOST_SWITCH`）进 `stats.py`；
    **仍不接线**（缺口 R1/R2/R3/R9，见工单）。新增**出处核对测试**：拿导出件逐条验"引的行号真的含有我声称的东西"，
    发布导出（无 `reverse-data`）时如实 SKIP —— 它当场抓到我一个错引用（`DestroyCard` 在 `:11347`、我写成 `:11348`）。
    ★ **口径更正**：先前注释/提交信息里的"数值变化通知 = `0x04/0x05`" **`0x04` 是错的**（`0x04` = 攻击后
    `OnAfterOtherCardAttacks`）；正确族是 `0x05/0x06/0x07/0x10/0x11/0x2C/0x2D/0x09/0x0A`
    （`ATTACK-HOOKS-1.60.md:855`/`:867`，标**未接**），测试已钉住。
    ★ 另记：FModel 反编译的 ubergraph **控制流有损**（弯路 #4）⇒"分支是否就地 return"仍待第二来源（IDA 读字节码）。
  * **顺带**：`sim/engine.py` 清掉 `_unit_value` 的重复定义与 `_evaluate`（引用从未定义的 `_evaluator`、无调用方）。
* **P1 剩余**：S3'（其余字段别名 `card_id→obj.CardID` 等）与 S4（删派生视图）。★ 2026-10-03 复核：
  文中曾写「已有别名棘轮护栏（`tests/test_field_alias_ratchet.py`，子代理产出）」——**该说法不成立**：
  文件不在工作区，`git log --all` 与全历史 `ls-tree` 里也从未出现过（不是"后来被移除"，是从来没进过库）。
  ✅ **已补上**（2026-10-03）：`tests/test_field_alias_ratchet.py` —— 别名集合**从 `Card` 类现读**（新加别名自动进账）、
  AST 统计（注释里提到不算）、`EXCLUDED` 每项都写理由且核对"没有名字掉出账本"；
  **基线 263 处**（`card_id` 207 / `kredit_cost` 16 / `can_act` 6…，与"约 270 处"的说法对得上），**只许减**。
  `--measure` 打印分布，S3' 每迁一批就把基线改小；S4 删别名时它会直接报"豁免已不存在"。
* **P6 剩余**：`player/rule.py`（3.3k 行）、`kardsmem/cli.py`、`kardsmem/board.py` 未拆。

### 5.1 2026-10-03 复核：三笔提交重排（撤销越界的 codex subagent 提交）

* **问题**：23:13–23:16 的三笔（`5e7741b` P7-S3b、`8337310` P7-S3b 续、`9d39bf2` P2 第八刀）不是按"一笔一件事"落的
  —— `5e7741b` 把 `semantics/{triggers,effectvm}.py → engine/` 的**重命名**混进了 P7 提交，而 `semantics/*` 的
  兼容别名两笔之后才补 ⇒ `5e7741b`、`8337310` 两笔**中间态是坏的**：`ModuleNotFoundError: No module named
  'semantics.triggers'`，各 **32 个测试失败**（`git archive` 到临时目录实跑复现）。
* **处置**：撤销后按"一笔一件事、各自全绿"重排为
  `05342f4`（P2 第八刀，重命名+别名同刀、原子）→ `80b0a77`（P7-S3b）→ `d826a85`（P7-S3b 续）。
  三笔分别 **73 / 74 / 74 全绿**；**最终树与被撤销的 `9d39bf2` 逐字节一致**（`git diff 9d39bf2 HEAD` 为空，
  即内容零丢失、零新增）。原提交保留在标签 `reverted-codex-20261003` 供对照。
* **同批复核出的文档缺陷**：① `test_field_alias_ratchet.py` 是**虚构护栏**（见上）；② TODO 的 H1b 行引用会失效的旧 SHA；
  ③ `kardsmem/build.py::apply` 的注释说 `RESTART_NEEDED` 只剩"frida 已注入"，而代码判据是"`ops.inject` 已加载"
  —— 已改成与代码一致的保守口径。
* **教训**：`git mv` 与被它影响的 import/别名**必须同一笔提交**；跨笔落地时中间态要么全绿、要么根本不该存在。

### 5.2 2026-10-04 更新（**当前状态；本节是主计划书的最新口径**）

> §5 上一节之后的所有进展都写进了 `P3-NATIVE-PORT.md`（P3 工单）/ `TODO.md`（看板）/ `PLAN.md`（回执台账），
> 主计划书一度落后 —— 本节把它补回来。**权威以那三份为准，本节只做汇总。**

**P2 完成（十刀）**：`sim/*` 全是壳、`engine/*` 是本体；护栏 = `tests/test_arch_rules.py`（别名方向 + 同一模块对象断言）
+ `tests/test_sim_source_tags.py` 棘轮（合计 **121** 处、基线 **114**，只升不降）。
小尾巴：棘轮文件还没按 §3 改成 `test_engine_source_tags.py`。

**P3 已落十刀**（2026-10-03 23:37 → 2026-10-04 00:24，每刀独立提交 + 全绿）：① 族的四个函数
（`ChangeAttack`/`ChangeDefense`/`ChangeOperationCost`/`ChangeHeavyArmor`）**4/4 逐行读完**，外加 `ChangeKreditCost`
全文读完。期间**修掉 6 个真 bug**（每个都有探针或实机证据，没有一处是"顺手改"）：

1. **活死代码**：`Card.total_attack`/`total_operation_cost` 是**方法**，`getattr` + `isinstance` 把它们滤掉
   ⇒ `cur_stats` 里从来没有 `attack`/`opcost` ⇒ `SetValue` 分支**静默丢效果**（改 `@property` + 抽 `_fill_cur_stats`）。
2. **`SetValue` 抹掉 buff**：原版是"基础字段 + **独立** buff 字段"的两字段模型（`R10`：5 攻 + 50 buff 后"设为 2" ⇒ **52**，旧实现给 2）。
3. **手牌费用无人消费**（`R14`）：`ChangeKreditCost` 的效果键是**标量**且**全仓库没有消费者** ⇒ 改手牌费用**完全没建模**
   （改发逐卡字典 `cost_ids` → `Hand.cost`；★ 该函数的 set 家族**只有 2/3**，`5` 与 `6-9` 走默认分支**只发通知不改值**，与四个兄弟不同）。
4. **两条读侧口径不一致**（`R13`）：`gamemodel.getTotal*` 不夹 `[0,99]` ⇒ 90 攻 + 50 buff 被读成 **140**（游戏 99）。
5. **减防不摧毁**（`R2` 的摧毁半）：`ChangeDefense` 两条改值分支都带 `DestroyCard`；现在三条防御通道统一走 `_def_delta`，
   降到 **≤0 就离场**。★ **行为变更（单位会因减防真死）⇒ 必须实机复核**。
6. **天气族假缺口**（10-04）：`GetOppositeSide` 的卡是 **receiver、不是 kid**（只读反汇编定案），`hk_seat` 把 receiver 丢了
   ⇒ 天气族 7 变体 + IJN SHINANO 这类"主路径要推敌方"的卡**效果根本没算**（`0ae86d9` 已修 + 4 项新测试）。

另修 `R11`（普通加减通道补夹 `[0,99]`、重甲 `[0,3]`）、`R12`、`R15`（`ChangeDefense` 的 `Label_3734` 是**拒绝分支**
⇒ "buff 通道压到 0"这个场景原版不可能发生），并把出处守卫收紧成"**止行必须紧贴函数末尾**"（当场抓出 `ChangeDefense` 的行号区间记错）。

**P3 当前状态：① 族 4/4 读完 + 接线（含摧毁/费用）｜② 部分｜③–⑩ 待做｜⑪ 只做过对账。**
**★ 2026-10-04（DS 复核并撤回一条过期结论）**：本节原写"P3 硬验收未达：`SIM-FIDELITY.md` 仍是 3 条「近似」（⑥ 攻击合法性 / ⑦ 攻击结算 / ⑧ 回合）" —— **实测已不成立** ✗：`SIM-FIDELITY.md` 表里这三行的**状态列**现在是
「`攻击合法性` → **忠实**（2026-10-04）」、「`攻击结算` → **战斗路径已按原版**（2026-10-04）」、
「`回合结束/开始` → **已按原版**（2026-10-04）」，另有 `移动上线` 也是 **已按原版** ✓ ⇒ **"近似 → 0"这条硬验收按表已达成** ✓（表里现在没有一行的状态是"近似"）。
**但**仍有 **9 行是「部分」**（部署合法性 / 伤害与死亡 / 抽牌链 / 疲劳 / autoplay 队列 / 战斗·打捞 / 偷牌·转化·老兵·治疗 / 卡面数值改动+`EChangeType` / 手牌上限·弃牌；
"部分" = 关键分支对过、仍有近似），各自备注里写着未复核或已知偏差 ⇒ **P3 的真实剩余是"把「部分」逐行升到「忠实」"**，而不是"清零近似" ✓。
（数一遍：`SIM-FIDELITY.md` 表里状态列恰为「部分」的有 9 行 —— 这是 2026-10-04 用脚本按第 3 列统计的，不是目测 ✓。）
未闭环缺口（工单 §4）：R1（`ChangeOperationCost` 接线，**前置 R3 残 + R9**）、R2（`OnAfterDefenseIsSet` + trigger 0x6）、
R3 残（`operationCostBuff`/`heavyArmorBuff` 还没有状态字段）、R4（6-9 通知族，挂起等真实用例）、R5（`ChangeBuffsFromCards` 未读）、
R7（`MakeCardsFight` 要运行时字节码定案）、R8（文档名对不上）、R9（`setAndEncryptDefense` 双重身份）、R10b/R11 残项。
**已闭环**：R10、R12、R13、R14、R15。

**P4 已开始（第一刀，2026-10-04）**：`engine/scripts.py` 建起来了 —— 直跑缝 = `kardsmem.vm.VM(..., hooks=…)`
（`vm.py:530`），录制模式填 `Recorder.hooks()`，直跑模式填 `native_hooks(state)`：已迁动词**直接调 `natives/` 改状态**、
**没有字典**；未迁动词经 `fallback` 仍走录制路径 ⇒ **两条路并存、逐族切换**。第一刀迁的是**资源族**
（`GiveKreditsBySide`/`ChangeKreditsBySide`/`GainKreditSlot`/`LoseKreditSlot`/`setKreditSlotBySide`），
并且立了**对账**（`reconcile_one`：同一实参下"录制→`to_effects`→`_apply_eff`"与"直跑 sink"逐字段比对；
`tests/test_scripts_direct.py` 13 项，含三条对账全 `diff={}`）。★ 第一版把 `sim` 的 `_apply_eff`
写死在 `engine/scripts.py` 里 ⇒ **被 `test_arch_rules` 当场抓到**（`('engine/scripts.py', ['sim'])`）⇒
改成由调用方注入（engine 层不许认识 sim）。

**P4 第二刀（2026-10-04，状态族）**：`STATUS_SINKS`（`PinUnit`/`RemovePin`/`SuppressUnit`/`SuppressMultipleUnits`
→ `engine.natives.status.apply_suppress`）；直跑用 `ctx.events` 显式记**事件扇出**（0x3D/0x3A）以免丢。
★ **对账当场揪出一个真 bug**：`destroy_aoe`/`pin_aoe`/`suppress_aoe`/`remove_aoe`/`to_deck_aoe` 的实参是**卡指针**，
而 `to_effects` 原样存进 `*_aoe_ids`、消费端却当 **card_id** 用 ⇒ **群体摧毁/定住/压制/移出/放回牌库在实机上从来没结算过**
（同一输入下直跑压了两张、旧路一字未变）—— 这正是本重构要消灭的"中间词汇消费错了"那一类 bug。
已按 `MakeCardRetreat` 的同一口径修（指针→card_id，换不出记缺口），旧测试里掩盖它的断言已更正 + 加对照。

**P4 第三刀（2026-10-04，数值族）**：`engine/natives/stats.py` 补上 **verb 级原生**
（`change_attack`/`change_operation_cost`/`change_heavy_armor`，共用一个"基础值 + 独立 buff 字段"的实现，
分支/夹取都照已读的 switch 表与 `can_card_be_buffed`）；`engine/state.py`+`engine/adapter.py` 补
`U.opc_buff`/`U.armor_buff` —— **P3 时绕过去的 R3 残项，直跑绕不过去**（原版写的是**基础值**，
必须能算 `基础 = 总量 − buff`，`:9155`/`:10465`）。`engine/scripts.py` 加 `STATS_SINKS` + `DirectCtx.gates`：
**门**（`CanCardBeBuffed` `:10990`/`IsUnrevealedCovertCard` `:8906`/`:10332`）要的是**卡**属性，
`Sim` 里没有 ⇒ 由调用方喂（取不到就**记缺口、不改状态**，不猜）；成功路径**不动出参** `qqq`
（BP 里它只在门为假时被赋 `false`，`:11019`/`:11024` —— 不猜 True）。对账显示：**总量**两边一致，
而直跑**多做**了对 `atk_buff`/`opc_buff`/`armor_buff` 的维护（旧路只有总量增量）。全量 **88 个、失败 0**。

**P4 第四刀（2026-10-04，防御族）**：`natives.stats.change_defense`（与 `ChangeAttack` **不同构**：`0/4/6/7/9`+`8`
是**拒绝/空 return**、`1` 的负分支**不夹**且 `mdef += amount`、正分支夹取下限是 **1**、`2/3/5` 写完 `==0` ⇒ **摧毁**、
`ct==2` 发 `OnAfterDefenseIsSet`+`0x6`）；`engine/scripts.py` 加 `DirectCtx.destroy` + `_sink_change_defense`。
**直跑的边界定清楚了**：`ctx.destroy` 只做"移场 + 记 `("destroy", uid)` 事件"，**死亡触发链（0x27 + `death_fx`）
交调用方**按既有 `event_fx`/`death_fx` 扇出 —— 直跑负责"状态变更 + 事件表"，不重跑钩子层。
对账：`ct=2 设为 0` 两条路都把单位打掉，`diff={}` ✓。全量 **88 个、失败 0**。

**P4 第五刀（2026-10-04，生命周期族）**：`DestroyCard`（`:853-884`）是**薄函数** —— `!IsValid` ⇒ return；
否则两个事件（`ExecuteOnBeforeOtherCardDestroyed` + `NotifyDestroyUnit`）**先**发、再 `ApplyRemoveCardFromBoard`。
直跑照序记 `("before_other_card_destroyed", uid)` → `ctx.destroy`（移场 + `("destroy", uid)`）。
★ 语义分寸：原版 `!IsValid`（卡无效 ⇒ 合法 no-op）与"我们认不出指针"**不是一回事** ⇒ 后者**记缺口**，
不假装 no-op（否则静默漏一次真摧毁）。对账两条路都打掉 ✓。

**P4 第六刀（2026-10-04，伤害族）**：`excess` 的规则**下沉到 engine**（`natives.damage.excess_split`，
实现一字未改；`sim` 的 `_excess_split` 变转调 ⇒ **规则只写一处**）；`DAMAGE_SINKS["DamageCard"]` 只做
`ApplyDamageToCard` 的**状态那一段**（excess + 扣防 + `<=0` 摧毁 + 出参 `targetDestroyed`），
钩子链交调用方（该动词的函数体含钩子链，不能像薄函数那样整段拦）。
★ 两处"静默失效"被测试当场抓住：① 第 3 参 `damagerCardID` 是**卡 ID** 不是指针（按指针查 ⇒ `excess` 永不触发）；
② `deal_damage` 自己 pop 后再回调 `on_death` ⇒ 默认回调若再 pop 会记**假缺口**（新增 `ctx.died` 与 `ctx.destroy` 分开）。
对账 `diff={}` ✓。

**P4 第七刀（2026-10-04，抽牌族）**：`DRAW_SINKS["DrawTopCardFromDeck"]` —— 这个动词在**效果字典里一个字都没有**
（`effectvm` 全仓 0 次），旧路靠"VM 解释它的函数体 + 内层原语恰好被钩子接住"；直跑一步接住
（我方 ⇒ 注入的 `engine.chain.draw_chain`，它就在 engine 层 ✓；对方 ⇒ 记缺口；抽到的卡对象出参 ⇒ 记缺口）。
★ 同时**撤回**我先前写的"以前静默丢了" —— 不成立（VM 会解释函数体），能确定的只是"字典里没这个动词"。

**P4 第八刀（2026-10-04，撤退族）**：`RETREAT_SINKS["MakeCardRetreat"]` —— 跳过 `cantRetreat`（`:1727`），
其余走 `ctx.retreat`。★ 两条**必须分开**的语义：撤退 ≠ 摧毁（不发 0x27 死亡链）；
0x36 必须在**离场前**扇（`sim` 既有口径）⇒ 新增 `ctx.on_event`（注入 ⇒ 当场扇），
并用"给了/不给"两个不同结论的输入作判据。

**P4 第九刀（2026-10-04，关键词授予/移除族，20 个动词）**：`GIVE_SINKS` + `REMOVE_SINKS`（工厂按关键词生成，
表来自 `effectvm.GIVE_KW` 单一来源）。规则与 `sim` 同口径并标出处：0x1D **只在状态真变了才广播**、
`immune/alpine/salvage` 的移除**不广播**、`GiveBlitz ⇒ sick=False`、`GiveFury ⇒ attacks_left`。
★ 不写 `u.ab`（原始能力表）—— 规则侧统一读 `kw`，写两份就是两个真相。对账 `diff={}` ✓；
`DEFAULT_SINKS` 覆盖动词数 17→**37**。

**P4 第十刀（2026-10-04，手牌族）**：`HAND_SINKS["ChangeKreditCost"]` + `natives.stats.change_kredit_cost`
—— 目标在**手牌**（`ctx.hand_card_of`），旧模型只认"场上 target" ⇒ 这个键**一直没有消费者、效果被整个丢掉**
（`effectvm:679-684`）。★ 集合家族**只有 2/3**（5 与 6-9 落 `Label_1614`：只通知、不改数值 —— 与 `ChangeAttack` 不同）；
门只在 `skipCovertCheck` 为假时查。对账：两条路手牌费用都到 6，`diff={}` ✓。

**P4 第十一刀（2026-10-04，AOE 族）**：`AOE_SINKS`（`DestroyMultipleCards` / `RemoveMultipleCardsFromBoard` /
`DamageMultipleCards`）+ `_aoe_units`。★ 三处口径：摧毁（发 `("destroy", uid)`）vs **移出**（`leave_board`：
只 pop、**零**事件 —— "离场但不算被摧毁 ⇒ 不触发 `death_fx`"）；`DamageMultipleCards` 的**数组在 0、数值在 1**
（录制表 `("damage_aoe", 1, 0, None)`）；数组里混着**敌方总部卡 id** 时打总部（`ctx.hq_card_ids`，
与 `effectvm:728` 的 `damage_hq` 拆分同口径）。对账 `diff={}` ✓。

**P4 第十二刀（2026-10-04，自定义能力族）**：`ABILITY_SINKS`（`CustomAbilityAdd`/`CustomAbilityRemove`）+
新模块 `engine/natives/abilities.py`；**`U.ab` 从 `frozenset` 改成原版的计数映射** `{能力名: 次数}`
（`HasCustomAbility = receivedAbilities[name] > 0`；消费端全是 `in` ⇒ 语义不变，但"减到 0 才消失"能表达了）。
★ 实参坑：`ability` 在 0、**`cardID` 在 1 且是 ID 不是指针**（`ctx.unit_by_id`；与 `DamageCard` 同一族坑）。
★ `CustomAbility*` 不在 0x1D 广播的调用点清单里 ⇒ **不发事件、不写出参**（发了就是编的）。
giver 级账未建模 ⇒ 如实记缺口。录制侧把这两个动词整个忽略 ⇒ 对账是刻意不对称。

**P4 第十五刀（2026-10-04，`setAndEncrypt*` 叶写入族，5 个）**：录制侧这批是 `RECORD_ONLY`（先写**影子**、事后换算）⇒ 直跑**没有影子**、直接写字段（P4 要拆的正是这块）。三条语义：
字段是**基础值**（总量 = `clamp(基础+buff)`；防御直接写）、**叶写入自己不夹**、
★ `setAndEncryptDefense(0)` **不摧毁**（摧毁是调用点的规则）。
**P4 覆盖账（第一次算清）**：登记 147 / 已直跑 **51** / 剩余 **96** = 非效果助手 **33**（IGNORE/JSON：
日志·文本·表现·JSON，**不需要**直跑）+ **效果动词 63**（真正剩下的工作）。
⇒ P4 的退出条件（删 `to_effects` 词汇表、退役 `test_effect_keys_consumed`）要等这 63 个切完再谈；
每一步都保持"两条路并存、逐族切换 + 对账"。

**★★ P4 第十八刀（2026-10-04）：撤回前两轮的"真 bug 修复"——那是我自己的测试夹具制造的假象**
（见 `PLAN.md` 同名工单行、`CLAUDE.md` 弯路 #47）。要点：
* **撤回**：`*_aoe_ids`（第一轮）与 `salvage_ids`/`discard_ids`（第十七刀）的"指针→card_id 映射"。
  依据是 **BP 签名与真实调用点**：这些数组/参数**本来就是 card ID**（`const TArray<int>*&`；
  `SuppressUnit` 的实现是 `MakeArray_Array = [ cardID ]; SuppressMultipleUnits(…)`）。映射会把真 id
  映成 `None` ⇒ 记缺口、**跳过效果** ⇒ 我引入的是**回归**。现已恢复原样透传。
  （对照：`MakeCardRetreat(const TArray<UBaseCardObject*>*& cards,…)` **是指针数组** ⇒ 它那份映射是对的 ✓。）
* **另一方向也错过**：`PinUnit`/`SuppressUnit`/`RemoveCardFromBoard`/`ResetUnitOperations`/`ChangedPinnedTurns`
  收的是 **ID**，我的 sink 按指针解析 ✗ ⇒ 新增 `DirectCtx.unit_arg`（**id 优先、再按指针**）并换掉 7 处。
* **元教训**：**手写夹具是最弱的证据**。用夹具"发现"的 bug，先对 BP 签名/真实调用点核实元素语义，
  再决定修不修；否则会在"测试全绿"的掩护下把正确实现改成回归。

**P4 收尾条件（2026-10-04 实测现状；`tests/test_to_effects_ratchet.py` 钉住）**

动词迁移已完：登记 **147** / 有直跑决策 **101** / 剩余 **13** 全是"非 sink 形状"
（12 随机·流：由 `Recorder.stream` + `kardsmem.rng.Stream` 与 VM 机会节点处理；1 抉择枚举：`WhichChooseOne`
走 `Recorder.choice` + `enumerate_effects`）。`to_effects` / `record_effects` 的**生产**调用点实测只有 5 个文件：

| 文件 | 角色 | 收尾动作 |
|---|---|---|
| `engine/effectvm.py` | 旧路径主入口（`record_effects` 出 eff）+ 自检 | 最后才动：降级为"未迁动词兜底 + 缺口来源" |
| `engine/triggers.py` | 触发层：钩子后果的**预计算** | 切到直跑后应消失 |
| `engine/scripts.py` | **对账基线**（`reconcile_one` 的 A 路） | **故意留着**，不删 ✓ |
| `player/rule.py` | 规则侧（`record_effects`） | 切直连 engine 状态后应消失（与 P5 合并做） |
| `semantics/choosespawn.py` | 候选预测（`record_effects`） | 同上 |

⇒ **退出条件**：把 `triggers`/`rule`/`choosespawn` 切到 `engine.scripts` 的直跑 ⇒ 名单缩到 `effectvm` + `scripts`
两个 ⇒ 那时才谈 ① 删 `to_effects` 词汇表（`dst["k"] = …` 那一大堆）② 退役
`tests/test_effect_keys_consumed.py`（**现在仍必须在**：活路径还在产出 eff 键，它替我们盯着"产出但没人消费"的键 ✓）。
**棘轮**：新增生产调用点会直接 FAIL —— 防止又有人往旧路径上加东西（P4 的方向是单向的 ✓）。

**删 `boardeval` 门面（P5 第一步）—— 实测的代价与做法（2026-10-04）**

现状：`policy/boardeval.py` 已只是**兼容门面**（496 行 = 10 行 import + `importlib` 把 `engine.state`/
`evaluation.value`/`sim.engine`/`policy.search`/`engine.adapter` 的公开名复制进自己的 globals ✓ + `selftest` ✓）。
**生产**消费者只有 `player/rule.py`，用 13 个符号：`W`/`delta`/`from_cards`/`sim_deploy`/`_has_key`/`H`/
`sim_attack`/`sim_turn_end`/`search`/`sim_move`/`sim_order`/`U`/`unit_value`。

删它的代价（实测）：
1. **三种 import 形式**：`import policy.boardeval as X`（24 个测试）、`from policy.boardeval import …`（19 个）、
   `from policy import boardeval`（3 个）⇒ 机械迁移脚本必须覆盖三种；
2. **~8 处"门面身份断言"**（`sim.H` 就是 `boardeval.H`、`evaluation.value ≡ boardeval.evaluate`、
   `DEFAULT_WEIGHTS is boardeval.W` …）—— 它们是 P2 过渡期的**护栏**，要**随门面一起删**（不是改 import 能了事）；
3. 门面还做了两处 **wiring**：`_engine.WEIGHTS = W`、`_engine.set_unit_valuer(unit_value)`
   —— 必须搬到新入口，否则 `sim` 的选目标启发式**没接上** ✗。

★ 2026-10-04 试过一版机械迁移脚本（`_nn_scratch/migrate_boardeval.py`：按 `hasattr` 找真家 + 短别名替换）：
只改到 2 个文件、跑出 **6 个测试失败** ✗ ⇒ **已回滚**（树保持全绿 ✓）。⇒ **删门面要分步**：
① 先搬 wiring → ② 再逐种 import 形式迁消费者 → ③ 最后删身份断言与文件。这三步各自可独立验证（每步全量 ✓）。

**P5 正题：把 `rule`/`triggers` 切到直跑（实测的切换点，2026-10-04）**

要切换的"记录 → 解释 eff 字典"调用点（实测）：

| 文件 | 点 | 说明 |
|---|---|---|
| `engine/triggers.py` | `record_effects` ×3（:524/:547/:617）+ `to_effects` ×1（:1349） | 钩子后果的**预计算** |
| `player/rule.py` | `record_effects` ×4+（:561 进场钩子、:1383、:3038/:3057 手牌目标…） | 规则侧按 eff 改状态 |

**为什么切换必须发生在消费者侧**：`record_effects` 只产出 eff 字典（**没有 `Sim`** ✓），而直跑
（`engine.scripts.native_hooks`）要一个**活状态**才能改 ✓ ⇒ 切换 = 在每个消费者点把
「`record_effects` → `to_effects` → 按键改状态」换成「`vm.run(hooks=native_hooks(state, …))` 直接改状态」 ✓。

**第一小片（建议）**：`rule.py:561` 的那族（`OnOtherCardEnterPlay` 进场钩子）—— 一个钩子族、一个入口、
可用**动作级对账**（同初始状态 + 同一张牌，旧路 vs 直跑，比对终态快照）钉住 ✓；
`engine/scripts.reconcile_one` 已经是"单词级"的对账，动作级可以复用它的快照口径 ✓。

★ **读代码后修正的一处认识（2026-10-04）**：切换**不是**"把 `record_effects` 调用换掉"那么简单 ——
`rule.py:573` 把算出来的 `eff` **塞进卡对象**（`cards_out[id] = BE.H(id, 名字, …, eff)` ✓），
之后**出牌时**由 `sim._apply_eff(H.eff)` **重放这份摘要**。⇒ 直跑切换的真实形状是：
**出牌那一刻**，把"重放预先算好的摘要"换成"对**活状态**跑 VM + `native_hooks` 直钩" ✓
（这正是"policy 直接在 engine 状态上展开"的字面意思 ✓）。⇒ 切片方式随之改变：
按**出牌路径**（部署 / 指令 / 上线 / 攻击 / 钩子触发）逐个切，而不是按动词 ✓；
每个切片都要"同一初始状态 + 同一张牌，旧路 vs 直跑比终态"✓。

**删 eff 词汇表的实际代价（2026-10-04 实测，`_nn_scratch/probe_eff_keys.py` 只读探针）**

`effectvm` 一共产出 **36 个 eff 键**，账是**平的**：**35 个有消费者** + **1 个有据可查的丢弃**
（`self_damage` —— 代码里写着"该单位此刻不在 sim 里，丢弃" ✓）。消费者的分布就是删除的代价：

| 消费者 | 键数（大致） | 说明 |
|---|---|---|
| `sim/engine.py` 的 `_apply_eff` | 绝大多数（30+） | **就是那个大块**：删词汇表 = 删它所有键分支（前提是这些动词已直跑 ✓ 101 个已迁） |
| `player/rule.py` + `policy/search.py` | 9（`buff`/`buff_ids`/`damage`/`damage_hq`/`fight`/`give`/`opcost`/`intel_seen`/`deck_shuffle_skip`） | 规则侧读 eff ⇒ **切直连 engine 状态后消失**（与 P5 同一件事） |
| `engine/triggers.py` | 3（`damage`/`damage_hq`/`heal_hq`） | 钩子后果预计算 ⇒ 切直跑后消失 |
| `engine/scripts.py` | 1（`pin_turns` 的**对账读数**） | **故意留着**（对账基线 ✓） |

⇒ 结论：**删词汇表 ≈ 删 `sim._apply_eff` 的键分支 + 规则侧的键读取**，而这**正是 P5 的内容**
（policy 直接在 engine 状态上展开、删 `boardeval`）⇒ **P4 收尾与 P5 应当合并做**（分开做会返工 ✓）。
★ 探针的一个方法论提醒：正则的**变量名**收太窄会造"假孤儿"（我先只匹配 `e[...]`，把 `rule` 侧用 `eff` 读的
`intel_seen`/`deck_shuffle_skip` 误判成"没人读" ✗）⇒ **扫"有没有消费者"时，变量名要放宽再人工核**。

**P5 未开始**：`engine/effectvm.py` 仍是录制 VM（P4 收尾才降级为缺口审计）；`tests/test_effect_keys_consumed.py` 仍在；
`policy/*` 仍 `from sim.engine|state`，`policy/boardeval.py`（496 行）未删。

**P1 剩余**：S3'（字段别名迁移，棘轮基线 **263** 处，只许减）、S4（删派生视图，须在 S3' 之后）。

**P6 剩余**（`ops/inject.py` 已在 `782c1d8` 拆成 126 行门面 + mixin 模块）：仍超 1500 行的有
`player/rule.py` **3276**、`engine/triggers.py` 2152、`engine/effectvm.py` 1538。
★ 顺序建议：`player/rule.py` 的拆分**放在 P4/P5 之后** —— P4/P5 要重写它的主体，先拆等于拆两遍（计划原本也是 P6 在 P5 之后，别提前）。

**P7 剩余**：S2 对其它版本真进程实测（**需那些版本的游戏在跑**）；S4（表迁 `tests/fixtures/golden_rva.json`、缓存目录进 `.gitignore`）。
**P8 剩余**：`make_release` **不带 `--overlay`** 对 HEAD 复跑 + **由用户手工 subtree split 推送**。

**实机对账（2026-10-04 00:28 训练局，130 步 / 392.9 s / 无异常）**：与 22:49 那局同口径比，`t_decide` 均值 1.17→**0.78 s**、
`eff_src` vm/gap 219/119、**gap 种类没有新增**（3 vs 4）、真失败与旧局同类 ⇒ 本轮"激活"类改动**没有引入回归**。
**仍待实机**：天气族 `eff_src` 从 `gap` 翻成 `vm` 的端到端确认（`0ae86d9` 已修、**只读直评**已过：两张卡 `stopped=None`）。

**完成度**（2026-10-04 盘点；口径 = P0–P8 九阶段等权 × 各阶段退出条件达成面）：**≈51%**；
按剩余工作量口径只有 **30–35%**（P4/P5 两块架构级工作还没动）。

### 5.3 2026-10-05 Claude 接手审核（**比 5.2 更新；DS 已关闭，Claude 接续**）

审核对象：`908fdb1..af4c76f`（约 80 笔提交，104 个文件）。实测：全量 **96 个 0 失败**、`test_seat_rules` 0 残留、
`kardsmem.board --selftest` PASS、`player.loop --selftest`（nn venv）PASS、tracked 工作区干净。**未做**：实机（游戏没开）。
状态订正（5.2 已过期的几条）：
* **P4 动词迁移完成**（101/147，剩 13 个都不是 sink 形状）；`engine/calls.py`（类型化调用重放）+ `reconcile_run` 已落。
* **P5 已开始**：`policy/boardeval.py` 门面**已删**（步骤①②③）；`rule.py` 已脱离门面。**未做**：把出牌路径切到「直跑→存 call list→搜索只重放」、
  删 `to_effects`/`_apply_eff` 键分支/`test_effect_keys_consumed`、`policy` 仍 `from sim.*`。
* **P1**：S3' 棘轮 263→177；S4 未做。**P6**：`rule.py` 3537、`engine/triggers.py` 2404、`engine/scripts.py` 2128、`engine/effectvm.py` 1733 仍超 1500。
* DS 自己两次撤回结论（`1f0ef1d`、`1e65c27`）= 它会把夹具造成的假象当真 bug；以后『真 bug』必须有探针或实机证据才记。
* **下一步（P5 主体）**：按出牌路径逐个切（部署→指令→上线→攻击→钩子触发），每片用 `reconcile_run` 要求 diff={}，过不了就不改、记缺口。

**P5 影子对账（2026-10-05，已落、行为零变化）**：`record_effects(direct_state=…)` 是默认关闭的直跑接缝；`rule._shadow_check` 在每次搜索后对手里的指令
做三方对账（A=字典路 `_apply_eff`；B=对 Sim 副本直跑；C=B 记下的 `ctx.applied` 经 `engine.calls.apply_calls` 重放），差异进 `probe["shadow"]`
（`diff_ab` 字典路≠直跑 / `diff_bc` 直跑≠重放 / `skipped` 随机或没跑完）。第一轮实机（2 局 ~390 个 (牌,目标) 比对）：**没有无法解释的分歧**，
每处差异都对应直跑侧的显式缺口——`DUG IN`（缺 `spawn_stat`）、`ORP GENERAL HALLER`（缺 `convert` 载荷）、`DELUGE`/`COMMAND FAILURE`（缺抽牌链）
——三项都已在接缝里补上（调用点现算载荷 / 注入 `draw_chain`），**待下一局实机确认分歧消失**；`CRUISER SCOUTS` 只剩 `forecast_pending` 标记不可重放（按设计记缺口）。
**切换条件（还没满足）**：某类出牌路径在多局实机里 `diff_ab`/`diff_bc` 都为空，才把该路径的搜索从字典路切到调用重放；切一片提交一片。
**同日实机发现并修的真 bug**：`getTotalAttack` 原生漏 `attackBuff`（58th「有冲击 +2 攻」读成 1 攻 ⇒ 2nd RAIDING BRIGADE 目标门放行，游戏拒绝），`85e1f89`。
**同日未定因的事故**：重启监听器后第一局游戏崩溃一次（TODO 补记 13:42），未复现。

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

### 5.5 2026-10-06 P5 影子对账第三轮（额外对局 #1，英军 112 动作）
237 次 (牌,目标) 比对，只出现两张牌、**全部有已知原因**：`KANGAROO TRANSPORT`（给英军步兵闪击+1重甲）字典路≠直跑 215/215（单位属性元组差；
实机 diff 当时被旧模块截成 80 字符，看不全，**下一局 300 字符版再看**，不猜）；`HOME GUARD` 直跑≠重放 22/22（重放表缺 `gain_cards`）
⇒ 已修：`apply_calls(..., on_draw=)` 由调用方注入抽牌链（engine 不 import sim），影子 C 路已接，待实机确认。
前两轮的 DUG IN/ORP HALLER/DELUGE/COMMAND FAILURE/FOR FREEDOM/USACE/SUPPLY SHIPMENT 分歧在本局未再出现。
**仍未满足切换条件**（出牌路径多局 diff_ab/diff_bc 为空）；下一步：KANGAROO 定因 → 再多局覆盖更多牌型（本局手牌只覆盖 2 种）→ 切第一片（指令类）。

### 5.6 2026-10-06 P5 影子对账离线批量版（`tools/reconcile_batch.py`，不依赖实机）
**做法**：整进程转储（`kards-data/crashdumps/*.dmp`，`procdump -ma`）当 `kardsmem.Session.m` 的内存后端（`DumpMem`，`MemRO` 子类，VA 连续的相邻区段会拼接）⇒
Kismet VM 的字节码、卡牌字段、`HasBond`/`GetCardsInHandBySide`、RNG 种子、静态卡表全部离线可读；盘面用 `MemoryBoardSource` 同一份代码读出。
对每个转储 × 4 种盘面变体（原样 / 清空场上单位 / 只留我方 / 只留对方）把**每张指令牌放进我方手牌**，用**真的** `RuleV2._search_sim` + `_shadow_check` 做 A/B/C。
只读转储，运行期 `agent.precheck.call_*` 换成调用即抛；报告 `docs/RECONCILE-BATCH-REPORT.json`；测试 `tests/test_reconcile_batch.py`（合成 minidump、聚合、假 VM 端到端）。
**首轮结果**（26 个有盘面的转储 × 4 变体，392 s）：42 张指令，same 13 / diff_ab 21 / diff_bc 6 / skipped 2（转储只碰到 338 张官方指令里的 37 张）。分歧都落在**直跑侧/重放侧的显式缺口**或待定因项：
* **直跑 sink 不认总部指针**（`ChangeDefense`/`DamageCard`/`DestroyCard`/`DamageMultipleCards`/`ChangeAttack` 的目标是 HQ ⇒「目标指针换不出场上的单位」未结算）⇒ 字典路 `heal_hq/damage_hq` 有、直跑没有：FORTIFICATION、NZANS、WE CAN DO IT、NAVAL POWER、GUNSHIP MISSION、STRATEGIC BOMBING、THE ANZAC SPIRIT、FOR THE KING、AIR SUPERIORITY、Z SPECIAL UNIT 等 diff_ab 的主因；
* **重放表缺 `draw` / `heal_unit` / `damage_aoe`**（diff_bc）：BLACKOUT、CONVOY HX 175、DELUGE、GATHERING STORM、FOR FREEDOM（draw）；IMPERIAL ORDER（heal_unit）；EAGLE CLAWS（damage_aoe）；
* **待定因**：USS YORKTOWN、DEATH FROM ABOVE、HEATWAVE 的 `unit` 差异（直跑生成单位而字典路没有）；OVERCAST 槽位 `slots/opp_slots`（字典路我方 +2，直跑 +1/对方 +1）；TASK FORCE 44 的预报缺口；
* skipped：NAVAL BATTLE（随机/抉择）、PARACHUTE ASSAULT（VM 停在 `GetObjectClass`/`unitsToSpawn`）、SEABORNE INVASION/SURPRISE ATTACK 部分变体（`cards_reserve_changes` 空对象、`IsPinned` 在 None 上）。
**顺带发现**：`rule._search_sim` 只给**带目标**的牌填 `pair_eff`，`_shadow_check` 遍历的就是它 ⇒ **不带目标的指令实机影子从来没比过**（解释了 5.5 里“本局手牌只覆盖 2 种”）；批量工具补 `(cid, None)` 条目（取 `H.eff`）进同一个 `_shadow_check`，rule.py 未改。
**夹具教训**：第一版给了 99 点指挥点（超 `kredit_max`=24）⇒ A 路夹到 24、B 路不夹，造出一堆假 `kredits` 差异；改成固定 10 点后全部消失（同 CLAUDE.md 弯路 #47：夹具造出来的“分歧”先查夹具）。
**局限**：转储是静止的一帧，只覆盖该局两副牌里出现过的牌；没有 `hand_target/forecast/choose_spawn` 的**选择路径**对账；玩法合法性（`can_play`）离线一律放行。要覆盖其余 ~300 张指令 ⇒ 需要更多局的整进程转储（或把静态卡类实例化进盘面：现 VM 以真实卡实例为 `self`，CDO 会读空 `cardFunction`，见弯路 #44）。

### 5.6b 2026-10-06 离线对账全覆盖（`--orders all`，338 张官方指令，1 个 dump × as_dump）
同 290 / diff_ab 25 / diff_bc 9 / 跳过 37（比对 same 716、ab 40、bc 24、跳 67）。从首轮（42 张）的 13 同 → 现在**八成以上一致**。
剩余分簇：单位差异 14（ADMIRAL YAMAMOTO/BREAKTHROUGH/CADET NURSE CORPS/COUNTERATTACK/ENCIRCLEMENT…）、重放缺口 9、牌库差异 6、
手牌差异 5（EDGE OF THE EMPIRE/IJN AKAGI/IRON VICTORY/REDEPLOYMENT/THE TIDE TURNS）、总部 4、跳过 37（选择/随机 18 + 缺原生 VM 停点）。报告 `docs/RECONCILE-ALL338-REPORT.json`。
**簇 2（单位簇 + 重放缺口簇，2026-10-06）**：本簇 24 张 diff_ab 14 / diff_bc 9 → **全部 same**（`--only` 14 s 复跑）。判定与逐张证据见 `TODO.md` 同日条目；要点：
A 不忠实的是『逐张记账』（`damage_ids`/`give_ids`/`pin_ids`/`heal_ids`/`fights`、刚生成单位的出参临时 id、`DamageCard` 的 `targetDestroyed` 出参）；
B/C 补的是重放表（`fight`/`convert_*`/`steal`/`deck_add`/`deck_order`/`unit_to_deck`，本体抽成 `engine/natives/{convert,board}.py` 由 sink 与重放共用）。
新工具开关 `tools/reconcile_batch.py --only "牌A,牌B"`。测试 `tests/test_recon_unit_cluster.py`。

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
