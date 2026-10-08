# OPS（执行侧 / 注入）架构要求

日期：2026-10-02（2026-10-03 按总纲修订）　　状态：**标准草案，待用户确认**。
> ★ **先读 `ARCHITECTURE.md`（总纲）**：其中 §3（操作怎么切分）、§7（OPS 与策略的接口）补上了本文原先没写的**提示（Prompt）交接**；冲突处以总纲为准（总纲 §9）。关联：`EVAL-ARCHITECTURE.md`（评估/模拟侧）、`PLAN.md`。
OPS = `ops/inject.py` 及其周边（frida 管道、游戏线程调度、手势、动词、菜单流程、校验），即"把决定变成游戏里的真实输入"的那一层。

> **旧文档说明**：先前的 OPS 文档（`reverse-data/reports/report/OPS-INJECT-HANDOFF.md` 等）已严重过期，**以本文为准**；旧文档只可当历史记录，其中的函数名/流程/偏移结论使用前需对照当前代码与 `CLAUDE.md`。

## 0. 现状与问题（事实）
> ★ **2026-10-04 状态**：本节第 1 条描述的是**迁移前**的单文件（6885 行）；实际进度见 §7 末尾的
> 「已完成到第几步」（步骤 0–4 已完成，`ops/inject.py` 现为 **144 行门面** + `ops/` 下 **14 个 `.py`**）。
- `ops/inject.py` 单文件 **6885 行、约 423 KB、184 个函数**，只有一个 `Injector` 类（`class Injector` @924）；约 600 行 JS/CModule 以字符串内嵌在 Python 里（`JS = r"""` @329）。传输、反射缓存、手势、动词、菜单流程、诊断混在一起。
- 已被事故证明必须靠纪律才不出错的点：跨线程调用蓝图崩游戏（#30）、失焦降帧吞输入（#29）、旁证混进成败判据（#22/#28）、"只读"查询偷偷写字段（#23）、提交口选错（#17/#19/#21）、`__WorldContext` 传 0（#18）、每次 attach 泄漏 43 MB Temp（#41）、陈旧指针（`obj_alive`）。这些现在散落在代码注释和项目规则里，**没有被架构强制**。
- 前端层 `agent/session.py`（795 行）已是较干净的命令层；`agent/nn.py`（1226 行）是循环与策略胶水。

## 1. 最高约束（不可协商）
1. **红线 = 信息流等价**：发给服务端/对手的信息必须与真人鼠标正常操作等价——悬停、停留、按下、拖动 tick、松开的**完整事件序列**，不得跳过悬停直接调用终点函数。
2. **OPS 只执行，不决策**：不含评估、不含"该不该打"的判断；判据只"挑"不"否决"，让游戏自己裁决，再读回执（"判据负责挑，游戏负责判"）。
3. **成败只认动作流**：副作用（手牌少一张、候选集合变化、界面开着）只能当旁证，不得进 `ok`。
4. **不动真实鼠标、不依赖窗口焦点**；全部输入为进程内合成事件。
5. **Agent 不得擅自使用真鼠标/键盘**：仅在用户说明无人值守、或知情并允许时才可点击，且必须带 timeout；见 项目规则（闸门在 Agent 行为上，不在代码里）。；**置前游戏窗口与真鼠标同等对待**（尽量不置前，仅在需要点击或必须前台的测试时、先征得同意）；截图用现有工具链，不需要前台。

## 2. 分层与依赖方向（只能向下依赖）
```
L4 会话/前端（agent/session.py、shell、mcp、nn 循环）   ── 唯一动词集合，纯转发 + 结果归一
L3 动词（play_card / attack / move_to_front / pick_choice / end_turn / mulligan / surrender / 菜单流程）
                                                       ── 每个动词 = 预检(只读) → 手势 → 校验(动作流) → 结构化结果
L2 手势（hover / click_actor / drag_release / arrow_attack / press_button）
                                                       ── 声明式"事件序列配方"，含停留帧数、提交口
L1 原语（call_ufunction / read_field / write_fields_then_call / settle / obj_alive / find_actors / spawn watch）
L0 传输（frida attach、JS/CModule、游戏线程调度器、RPC、重连/清理、Temp 目录）
旁路：只读查询（Can*、card_totals、gap_number …）与诊断（dump_props、probe）放独立模块，不得与写手势共享"临时写字段"的能力
```
- 下层不知道上层；动词不得直接碰 frida 对象，手势不得拼 JS 字符串，一律经 L1 原语。
- 读（查询）与写（手势）**物理分离**：查询模块没有写字段的能力（编译期/导入期即不可用），从架构上杜绝 #23。

## 3. 必须由架构强制的规则（把弯路变成约束）
| 规则 | 来源 | 强制方式 |
|---|---|---|
| 所有 `ProcessEvent`/`callRawArr` 只在**游戏线程**执行 | #30 | L0 唯一出口；L1 原语没有"直接 pe" 的 API；4 s 无人接手撤回报错 |
| 所有等待按**游戏帧数**（`settle(seconds, frames)`），墙钟只作下限 | #29 | 手势配方里"停留"只能写 `Hold(seconds, frames)`；lint：L2/L3 内禁止裸 `time.sleep` |
| 注入调用的 `__WorldContext` 一律传活的 Logic/PC/actor | #18 | L1 `call_ufunction` 默认注入 world context，传 0 需显式 `unsafe_world=True` 并在日志标红 |
| 写与消费必须在**同一次 JS 执行**（中间插一帧会被 tick 重算） | #19 | 提供 `write_then_call(writes, calls)` 原语；手势里"写字段 + 提交"必须用它 |
| 提交口按链路取，不通用"松手" | #16/#17/#21 | 每个手势配方显式声明 `commit_port`（BP 函数/entry）与规格出处；动词名按"链路做什么"命名 |
| 成败只认动作流 | #22/#28 | 结果类型里 `ok` 只能由 `Receipt(action_stream)` 构造；旁证放 `evidence` 字段，类型上不能影响 `ok` |
| 同一时间只允许一路写 | 项目规则 | L0 单飞锁（single-flight）；并发写请求排队或明确拒绝 |
| 不重放陈旧点击 | memory：no repeat stale clicks | 点击前必须重新定位目标 + `obj_alive`；UI 改变后的点击一律重新扫描 |
| 陈旧指针防御 | #5 崩溃分析 | 所有 `click_actor`/候选点击入口先 `obj_alive` |
| attach 一次、常驻、可清理 | #31/#41 | L0 管理唯一 frida 会话；专用 TEMP；`sweep_frida_tmp`；禁止动词层 `attach` |
| 偏移不硬编码 | 代码约定 | 一律经运行时 RVA 扫描结果/反射链（`kardsmem/version.py` + `rvascan.py`，2026-10-03 取代 `build_tables.json`/`KARDS_BUILD`）；扫不出就报错、不猜 |
| 错误可见 | #20 | 禁止宽泛 `except: pass` 吞错；失败进结构化 `reason`/`evidence` |

## 4. 手势与动词的规格化
- **手势 = 数据**：`Gesture(name, steps=[Hover(actor, hold=(0.5s, 6f)), Down(...), DragTick(n, ...), Up(commit=...)], commit_port, preconditions, postchecks)`。每个手势有一页规格：对应的真鼠标事件序列（可由录制的真人轨迹校对）、哪些步骤会被转发给对手（悬停高亮等）、提交口与读取的字段。
- **动词 = 配方组合**：`预检(只读 Can*) → 手势(s) → 回执校验(动作流/状态机) → Result`。
- **统一结果类型**：`Result{ok, criterion, receipt, reason(枚举), evidence, timing{分段}, attempts}`；`reason` 用枚举（`awaiting_choice`、`actor_not_alive`、`layer_gone`、`rejected_by_game(text)`、`panel_open`…），字符串仅作展示。
- **多步动词是状态机**（抉择+指向、两阶段出牌、预报两层）：状态机单独成模块并配离线测试（沿用 `test_choose_verbs.py` 的假 `self` 模式），"合并动词能走通"必须有自己的测试。
- **菜单/开局流程**（开始、选模式、选卡组、结算继续、投降）同样按"配方 + 轮询状态"写，**不用固定 sleep**；`end_of_match_continue → 回卡组页 → list_decks 闸门 → press_play` 的顺序编码进流程，不能被调用方跳过（#40）。


## 4.5 提示（Prompt）与计划的交接（2026-10-03 新增，总纲 §3/§7）
- 动词的"一次执行"= **手势序列 + 回执校验**；动作之后游戏可能**挂起提示**（抉择、预报两层选牌、选手牌、选目标、换牌……）。
- OPS 新增**只读** `observe_prompt()` → `PromptInfo{kind, options[], source_card, layer, awaiting}`（来源：`chooseOneActive`/`selectCardToDrawPending`/`isSelectingHandTarget`/`cardBeingPlayedFromHand` 等游戏自己的旗标，**不写字段**）；以及 `answer_prompt(kind, choice)`，各 kind 复用现有动词（`pick_choice` / `choose_one_with_target` / `select_hand_target` / 选目标点击 / `mulligan_*`）。
- `Result` 新增 `prompt`（动作之后出现的提示）——只作 `evidence`，**不进 `ok`**（#22/#28：旁证不得决定成败）；每个 `answer_prompt` 自己有回执（动作流里"这一步被消费了"）。
- **OPS 无状态、不记计划**：要答什么由 L5/策略（`policy.answer`）给；OPS 不得为了"补全流程"自己选。系统引发的提示（回合开始钩子、autoplay 冲刷）也走同一个 `observe_prompt → policy.answer → answer_prompt` 回路。
- 多步动词（抉择+指向、两阶段出牌、预报两层）因此退化为**观察→回答→校验**的小状态机；"选什么"不再写死在动词里（旧的 `choose_one_with_target(index, target)` 保留为"已知答案"的快捷形式）。

## 5. 可维护性标准
1. **拆文件**：`ops/` 包，目标单文件 ≤ 800 行：`transport.py`（+ `agent.js`、`cmodule.c/.js` 独立文件，不再内嵌字符串）、`reflect.py`（类/函数/偏移缓存与跨脚本缓存）、`primitives.py`、`gestures/*.py`、`verbs/*.py`、`menu.py`、`queries.py`（只读）、`diag.py`。`ops/inject.py` 保留为**兼容门面**（`Injector` 的公共方法转发），迁移期不破坏 `agent/` 与脚本的调用。
   > ★ 2026-10-04 实际落地的名字：单文件按**职责**拆成 `conn.py`（传输+反射缓存+连接）、`world.py`、`gesture.py`、`query.py`（**只读查询**，即本条的 `queries.py`）、`diag.py`（**写型诊断**）、`play.py`、`choices.py`、`flow.py`、`cli.py`，外加 `primitives.py`（L1）、`consts.py` / `support.py`。单文件上限由 `tests/test_ops_rules.py` 的棘轮把关（当前 1500 行，§5 的 800 行是**目标**、尚未达到：`flow.py` 1060 / `query.py` 1008 / `play.py` 996 行是下一步的活）。
2. **JS 单独成文件并可测**：JS/CModule 抽出为独立文件，带最小的离线语法/接口检查；Python 与 JS 之间的 RPC 方法表集中声明（名称、参数、是否需游戏线程），自动生成 Python 端 stub。
3. **公共接口小而稳**：对上只暴露动词与查询；内部原语/手势不得被 `agent/` 直接 import。
4. **配置集中**：环境变量与参数（`SETTLE_*`、`KARDS_*`）集中在 `ops/config.py` 一处，带默认值与说明。
5. **热重载友好**：模块可 `importlib.reload`（类方法并回旧类的约定已有），新增实例属性用 `getattr(self, 'x', 默认)`；JS 改动需重启监听器，文档注明。
6. **可观测**：每个动词输出分段计时（预检/手势/等待回执/事后扫描）、手势关键帧快照（提交前快照）、尝试次数；日志字段稳定、可被统计脚本消费（`x_timing`、`x_pre_commit` 现已有）。
7. **性能预算（软验收，长期目标）**：部署 ≤ 3 s、攻击 ≤ ~5 s、单回合 ≤ 35 s 作为**软指标**，不阻塞合入；每个动词声明预算并在结果里标超限，持续优化。
8. **文档—代码—测试**：每个动词/手势 = 一页规格（链路、事件序列、提交口、回执、风险）+ 实现 + 离线状态机测试 + 实机冒烟项；既有教训要么变成架构规则（§3）要么变成测试。

## 6. 测试与验收
1. **离线**（不需要游戏）：状态机假 `self` 测试、结果类型约束测试（旁证不能令 `ok=True`）、lint（L2/L3 无裸 `sleep`、无直接 `pe`、无宽泛 `except`）、RPC 表一致性。
2. **手势等价性（用户 2026-10-02 简化）**：游戏**没有鼠标路径检测**，所以不需要录制真人轨迹；只需保证**发给服务端的内容与真人一致**——悬停转发（`MouseHoverDispatch`/高亮）、按下/拖动/落点/提交等事件**一个不少、顺序正确**。用事件序列断言（离线，假 `self`）即可，无需黄金轨迹录制。
3. **实机冒烟矩阵**（常驻监听器，游戏后台、真鼠标在窗外）：出牌（单位/指令/带目标/抉择/抉择+指向）、部署站位（空隙序号）、上线、攻击（≥12 次）、选牌/预报两层、换牌、结束回合、开局/续局/投降流程。验收：成功率 ≥ 95%（攻击目标 100%）、无崩溃、无卡死、无 Temp 泄漏。
4. **故障注入**：窗口失焦/降帧、陈旧指针、面板意外打开、对局切换（跨局缓存，#37）——每项有预期降级行为（拒绝/等待/重试，不盲点）。
5. **回归指标**：每次改动后记录各动词耗时分布与成功率，进回执台账。

## 7. 迁移路线（不推倒重来，每步可回滚）
> 2026-10-03：在下面步骤 1 之前插入 **步骤 0：`Result.prompt` + `observe_prompt/answer_prompt` 接口**（总纲 M4），因为策略侧的"决策=触发+路径"依赖它。
1. 先写**架构规则的 lint 与结果类型**（不改行为）：禁裸 sleep/宽泛 except/直接 pe 的检查，`Result` 类型，现有动词包一层适配。
2. 抽出 **JS/CModule 为独立文件** + RPC 方法表；`Injector` 行为不变。
3. 抽 **L1 原语**（`settle`、`write_then_call`、`obj_alive`、`find_actors`）为独立模块，`Injector` 改为调用它们。
4. **查询模块分离**（`Can*`、`card_totals`、`gap_number` …），移除其写字段能力；`simulate_drag` 之类诊断独立到 `diag.py`。
5. **手势配方化**：先迁 `hover/click_actor/drag_release`，再迁攻击，其余动词逐个迁移；每迁一个跑手势等价性与冒烟。
6. **动词与菜单流程**按状态机迁移，补离线测试。
7. 保留 `ops/inject.py` 门面至少一个版本周期，最后再决定是否移除。
每步完成标准 = 离线测试全过 + 实机冒烟矩阵不退化 + 回执台账登记。

### 已完成到第几步（事实，2026-10-04）
| 步骤 | 状态 | 落地 | 判据 |
|---|---|---|---|
| 0 `Result.prompt` + `observe_prompt/answer_prompt` | ✅ | `agent/ops_result.py`、`agent/promptinfo.py` | `tests/test_ops_rules.py`（Result/PromptInfo 段） |
| 1 架构规则 lint + `Result` 类型（不改行为） | ✅ | 同上 | `tests/test_ops_rules.py`（sleep/except 棘轮 + Result） |
| 2 JS/CModule 抽成独立文件 + RPC 表 | ✅ | `ops/agent.js.tpl` + `ops/consts.render_js()`（attach 期渲染） | `test_ops_rules.py`（模板占位符、无 import 期烤 JS） |
| 3 抽 **L1 原语**为独立模块 | ✅ | `ops/primitives.py`（读 12 / 写 7 个原语，**只有它**发写类 RPC）；`conn/world/gesture/play/flow/query` 改为调用它 | `tests/test_ops_primitives.py`（RPC 名与编码逐字对比）+ `test_ops_rules.py`（写 RPC 只在 primitives.py、RPC 名必须真有 JS 导出） |
| 4 **查询模块分离**、移除其写字段能力 | ✅ | 只读查询在 `ops/query.py`（从 `primitives` 只 import 只读原语）；写型诊断（`simulate_drag`、`probe_canplay_targeted`）搬到 `ops/diag.py` | `tests/test_ops_diag.py` + `test_ops_rules.py`（AST 判定：query.py 代码里无写原语、不 import diag） |
| 5 手势配方化（hover/click_actor/drag_release → 攻击 → 其余） | ⬜ 未开始 | — | — |
| 6 动词与菜单流程按状态机迁移 + 离线测试 | ⬜ 未开始 | — | — |
| 7 门面去留 | ⬜ 保留中 | `ops/inject.py` = 144 行门面 | `test_ops_rules.py`（门面导出 + 「Injector 方法全部来自 mixin」） |

★ 步骤 3/4 的**实机冒烟矩阵尚未复跑**（本轮只做离线验证）："离线测试全过"这一半达到了，
「实机冒烟不退化 + 回执台账登记」那一半仍待有游戏时补（见 §6.3）。
★ 如实记一条**拆分前就存在**的坏调用（本轮**未**修，见 `tests/test_ops_rules.py::KNOWN_BROKEN_RPC`）：
`ops/gesture.py` 的 `self.api().call_raw_writes(...)` 在 JS 侧没有对应导出（写型只有 `writeCallRaw`）
⇒ 每次必然抛进 `except`、`consumed` 恒 `None`，实际走的是"没被消费"的兜底分支。

## 8. 与其它层的接口
- **向 eval/sim**：OPS 不依赖它们；只提供只读状态读取（经 `kardsmem`）与合法性查询（`Can*`，仅排序用）。
- **向 nn/rule**：只通过 `AgentSession` 动词；结果的 `evidence/timing` 供策略留痕。
- **与崩溃监视**：不改 `tools/crashdump.py`；OPS 只保证退出路径干净（detach + 清理 Temp）。

## 9. 已定与待定（用户 2026-10-02）
- 已定：手势等价性只要求"发给服务端的内容与真人一致"（悬停等事件齐全），不录轨迹（游戏无鼠标路径检测）。
- 已定：性能预算为**软验收**、长期目标。
- 已定（我替用户决定，因其"不知道"）：**lint 规则放进测试里**（`test_ops_rules.py`，随现有离线测试清单一起跑：`import`、`kardsmem selftest`、`test_choose_verbs` …），**不用 pre-commit 钩子**——理由：本项目有多个 Agent 在不同会话里提交，钩子装在本机 `.git` 里不会随仓库传播也不强制，而测试清单已是各方共同遵守的落地流程（`PLAN.md` §1.2）；测试可检查 L2/L3 无裸 `time.sleep`、无直接 `pe(`、无宽泛 `except: pass`、`Result.ok` 只能由动作流回执构造。
- 待定：`ops/` 目录名与"门面保留"策略（默认按 §5、§7 执行）。
