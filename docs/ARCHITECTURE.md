# KARDS 自动化：总架构（2026-10-03）

> 本文是**总纲**：把 `EVAL-ARCHITECTURE.md`（评估/模拟）、`OPS-ARCHITECTURE.md`（执行）、`ARCH-RELEASE-VCS.md`（版本/RVA/发布）串成一个整体，
> 并**补上原先各文档里都没有写清的几件事**：①"一步操作"怎么切分（行动 / 决策 / 提示）；②模拟遇到"需要我们做选择"的点怎么办；
> ③模拟的"稳态"边界在哪；④执行侧与策略侧怎么交接选择；⑤版本兼容。与旧文档冲突处**以本文为准**，旧文档随后按本文修订（见 §9）。
> 架构负责人：Claude；规则侧实现：DS（见 `TODO.md`）。

## 1. 术语（全项目统一，文档与代码都用这套词）
| 术语 | 含义 | 例 |
|---|---|---|
| **输入事件** | 进程内合成的最小输入：悬停、按下、拖动 tick、松开、点按钮 | OPS L0–L2 的事，策略层不可见 |
| **动词（Verb）** | OPS 对外的一个游戏操作 | `play_card` / `attack` / `move_to_front` / `end_turn` / `answer_prompt(…)` |
| **提示（Prompt）** | 游戏在某个动作之后（或系统事件时）**挂起等待玩家选择**的交互 | 抉择（选第几个）、选牌（预报两层 / 好人寥寥三选一）、选手牌（放回牌库顶）、选目标（箭头）、换牌 |
| **触发（Trigger）** | 一个决策的起点：**我们的动词**（打出/移动/攻击…），或**系统事件**（回合开始钩子、autoplay 冲刷） | `play_card(RANGERS)`、`OnStartOfTurn(PBY CATALINA)` |
| **决策（Decision）** | **规划的基本单位** = 一个触发 + 其后**全部提示的答案路径**，直到游戏安静（稳态） | "打出 RANGERS 并选 +4/+4"；"打出预报牌，第一层选 rain、第二层选 medium" |
| **路径（Path）** | 一个决策里依次对各提示的答案 `(a1, a2, …)`；无提示的决策路径为空 | `()` / `(1,)` / `(rain, medium)` |
| **回合计划** | 一串决策（束搜索的深度） | 3 步 |
| **稳态** | 事件队列空 ∧ autoplay 待打出队列已冲刷 ∧ 没有悬挂的提示 | 一个决策的终点 |
> 原则（用户 2026-10-03）：**行动 = 动作 + 其后的全部选择**。移动、攻击也一样（"攻击敌方总部时预报"= 一个决策，不是两个）。

## 2. 总分层（只能向下依赖；`test_arch_rules.py` 机械检查其中 L2/L3/L4 的方向）
```
GUI（gui/）                           只写控制文件 / 读状态与日志；不碰游戏
L5 编排（player/rule.py、agent/nn.py Loop、agent/session.py）
     ── 一回合怎么跑：取快照 → policy 出决策 → 执行（Verb → 遇到 Prompt 就问 policy.answer → OPS.answer）→ 校验 → 留痕
L4 策略（policy/）                     触发枚举、路径展开（≤24 叶 + 剪枝）、束搜索、强制决策（系统引发的提示）、ActionPlan
L3 评估（evaluation/）                 纯价值：value / delta / 概率分支聚合 / 延迟项估值；不含规则
L2 模拟（sim/）                        状态 State、事件队列/分发、规则引擎 engine、可挂起的 run（遇提示挂起）
L1 原语与钩子（kardsmem 的 VM/natives、agent/{effectvm,triggers,cardprobe}）  把游戏蓝图/原生"读出来"并产出事件/状态变更
L0 状态读取与版本（kardsmem、kardsmem.board、version、rvascan）        只读内存 → 快照；RVA/版本识别
——————————————————————————————————————
OPS（ops/ ← 目前是 ops/inject.py）    执行侧垂直栈：L0 传输 … L3 动词（见 OPS-ARCHITECTURE.md）；只被 L5 调用
```
- **策略（L4）是唯一同时认识模拟与评估的层，也是唯一决定"要不要这么打"的层。**
- **OPS 不决策**；它只执行 L5 给的动词，并把"动作之后出现了什么提示"如实回报。
- 兼容门面：`policy/boardeval.py` 只是旧名门面 + 自检（2026-10-03 已把实现物理迁入 `sim/` `evaluation/` `policy/`）。

## 3. 操作的切分（本文新增，原文档未写）
一个回合 = 若干**决策**；一个决策 = **触发 + 路径**；执行侧把它切成：
```
Verb（一次手势序列，校验动作流回执）
  └─ 动作之后游戏可能挂起 Prompt ──► OPS 只读报告 Prompt（kind/候选/来源牌）──► L5 问 policy.answer(prompt, state)
        └─ policy 按"计划路径"的下一项答（计划落空/随机偏离 ⇒ 重算并记 meta）──► OPS.answer_prompt(kind, choice)
              └─ 可能又挂起下一个 Prompt（预报第二层）…… 直到稳态
```
- **成败判据**：主动作的成败只认动作流回执；Prompt 的出现/消失只是**旁证**（不进 `ok`）。每个 `answer_prompt` 自己也有回执（动作流里"这一步被消费了"）。
- **提示的来源有两类**：①**动作引发**（我们的动词）——进路径，**搜索时就定好**；②**系统引发**（回合开始 `OnStartOfTurn` 钩子、autoplay 抽到/塞进手牌后在动作边界冲刷）——**强制决策**：起点不是我们的动作，弹出时当场用同一套枚举（`policy.forced`）。
- **autoplay 按原版**：带 `autoplay` 的牌被抽到/塞进手牌 ⇒ `AddAutoPlayCards` **入队**；在每个玩家动作处理完之后（`BP_PlayerMoves` 7 处）与回合/匹配流程里（`BP_OnlineMatch::ExecuteAutoPlayCards`）**冲刷**：免费 `PlayCardFromHand`，其交互在那个边界上弹出。（详见 `ARCH-RELEASE-VCS.md` §7.10。）
- 预报/三选一/抉择/选手牌/选目标/换牌：都是 **Prompt 的不同 kind**，OPS 侧各有 `answer` 动词（沿用现有 `pick_choice` / `choose_one_with_target` / `select_hand_target` / 选目标点击 / `mulligan_*`）。

## 4. 模拟（L2）的新契约：可挂起的运行
```
run(state, trigger) -> Done(state_end, trace, gaps, complete)         # 稳态
                     | Suspended(prompt, resume)                      # 遇到需要"我们做选择"的点
```
- **随机由模拟层直接按种子算出结果**（用户 2026-10-03）：凡依赖随机流的地方（抽牌顺序、洗牌、三选一/预报的候选、`RandomIntFromRange…`、随机目标……），`sim` 用活种子推进 `Stream`，**直接产出确定的结果**（消费步数与原版一致）；**评估层不看见随机**，没有 `Chance` 结果、没有概率分支聚合。
- **读不到种子时**（用户 2026-10-03）：**记缺口，并换一个随机种子**，让模拟照算法和这个种子给出确定的结果（评估层照常只见确定状态）。假种子由 L5/适配层提供（`rule._rng_seed()`：每回合固定一个、`probe.gaps["__rng_seed__"]` 写明"随机相关结果仅供参考"），`sim` 自己永远是"给了种子就照算"。旧代码里 `outcomes` 随机分支取期望、`chance_v` 加成是过渡，随 M1 清理。
- **Prompt = 我们的选择**（取最好）。候选如果依赖随机（如池洗牌取前 3），由 `sim` 先按种子算出**确定的候选列表**再挂起；预报第二层的候选依赖第一层的选择与当前种子位置，同理由 `sim` 在 `resume` 时按种子位置算出。
- Prompt 的候选来自游戏自己的数据：抉择分支（VM 枚举）、`GetChooseSpawnCards`（池洗牌取前 3，种子已知则确定）、`Forecast` 的固定三张、手牌/目标合法性（问游戏 `Can*`/`IsValidHandTarget`）。
- 策略对 `Suspended` 逐个候选 `resume`（copy-on-write 复制），得到每条路径的终态——**这就是"行动 = 动作 + 选择路径"在模拟层的实现**。
- **稳态边界**：一个决策的模拟止于稳态；**不跨对手回合**。跨回合的后果（预报选中的牌下回合开始才 spawn 进手牌、`kredit_next`、`OnStartOfTurn` 效果）记入 `State.pending`，**由评估估值**（约定口径写在 `evaluation/`，默认按"下回合开始、盘面不变"折算并在 meta 标明）。
- `State` 新增（2026-10-03 已实现）：`autoplay_queue`（待打出队列）、`pending_cards`（跨回合待进手牌的牌）、`forecast`（预报候选表，按种子预测的确定结果）、`hand_target_fx`（手牌目标候选表）。**挂起的提示不存进 State**，只存在于 `Suspended` 里（状态在挂起点前已复制）；`trace`（事件轨迹）仍待做。
- **随机**：唯一入口 `Stream`（`kardsmem/rng.py`），整条链共用；种子/随机流机制自最旧版本传下来、改动极保守，视为稳定底座，只留轻量自检遥测。

## 5. 评估（L3）
- `value(state)` / `delta(start, end, ctx)`；`value_pending(state)`（延迟项）。**评估层不接触随机**：没有概率分支聚合，输入永远是确定的状态（随机已由 `sim` 按种子算掉）。
- 评估**看得到**"挂起中的状态"（用于决策内部剪枝：先给各前缀打粗分再展开），但**不认识提示本身**。
- 规则一律不在这一层（夹取、死亡、触发……）；旧代码里残留的耦合点（`sim.engine` 通过注入口调 `evaluate`/`unit_value`、`WEIGHTS`）是过渡，随"返回概率分支"重构删除。

## 6. 策略（L4）
- `policy/search.py`：触发枚举 `gen_actions` + 路径展开 `expand_paths`（暂与搜索同文件，路径展开独立成 `paths.py` 待做）（**每决策 ≤ 24 条叶路径 + 剪枝**：粗评分排序，同牌路径进束前先剪，**束宽不动**）+ 束搜索。
- `policy/forced.py`（待建）：系统引发的提示（PBY CATALINA 抽 2 放 1、回合开始预报、autoplay）→ `answer(prompt, state)`：枚举候选 → 逐个评估 → 取最好。
- `policy/plan.py`（待建）：`ActionPlan(trigger, path, meta)`；L5 执行时把它交给 `answer`。
- 选择类函数（`choose_pick` / `choose_hand_target` / `choose_one`…）**只是 `policy.answer` 的不同 kind**，不再各算各的（现状里它们各自重算，是已知的不一致来源）。

## 7. 执行（OPS）——与策略的接口（补充）
- OPS 只提供：动词（执行 + 回执）、**只读的 `observe_prompt()`**（kind、候选列表、来源牌、层数）、`answer_prompt(kind, choice)`。
- `Result` 里新增 `prompt`（动作之后出现的提示，作 `evidence`，**不进 `ok`**）与 `timing`；多步动词仍是状态机，但"要答什么"由 L5/策略决定，OPS 不记计划。
- OPS 保持**无状态、不决策**；计划（路径）只存在于 L5/策略。

## 8. 版本、构建与兼容（摘要，细则见 `ARCH-RELEASE-VCS.md`）
- 版本串：读进程内存里的 `ProjectVersion`（`kardsmem/version.py`）；**调用者不指定版本**。
- RVA：运行时扫描（判据对齐 Dumper-7）→ 按版本.分支缓存 → 随包种子表只覆盖已发布版本（只向后兼容）；`kardsmem/rvascan.py` S1 已做。
- 兼容策略**非常克制**：缺机制按旧规则走（能探测就探测）、未知记缺口；语义指纹/验证等级可选；不要求为兼容写测试。

## 9. 与原架构文档的冲突与缺失（逐条处理）
| # | 原文档怎么说 | 现在发现的问题 | 决定 |
|---|---|---|---|
| 1 | `EVAL` §3.3：行动是"一步操作 → 连锁到稳态 → 终态" | 没写**中途要做选择**怎么办；"一步操作"没定义边界 | 引入 **决策 / 提示 / 路径 / 稳态**（§1、§3、§4）；`EVAL` 增补 §3.8 |
| 2 | `EVAL` §3.3/§3.4：`Outcome.branches` = 概率分支，评估处理期望 | 我们的**选择**和自然的**随机**被塞进同一个 `outcomes`（靠 `outcomes_mode=max` 区分）；随机在评估侧聚合 | **选择** = `Suspended(prompt)`（策略取最好）；**随机** = `sim` 按种子直接算出确定结果，评估不见随机；读不到种子 ⇒ 不改状态+缺口；`outcomes_mode` 与随机分支期望为过渡 |
| 3 | `EVAL` §3.3/§5：效果摘要字典不作为长期形态 | 现实里仍是主通道，新增的路径效果也走字典 | 维持目标（`sim/ops.py` 类型化状态变更，字典仅给日志），**迁移排在 prompt 模型之后**；过渡期新增键必须过 `test_effect_keys_consumed` |
| 4 | `EVAL` §3.1：L4 = `rule.py` + 搜索 | 搜索/路径展开/强制决策/计划需要自己的包 | `policy/`（已建，实现迁入）；`rule.py` 退为 L5 编排 |
| 5 | `EVAL` §3.3：事件队列到稳态 | 没写 **autoplay 待打出队列**与"动作边界冲刷"、**跨回合延迟项** | 并入 §3、§4；`State` 增 `autoplay_queue/pending` |
| 6 | `OPS` §2：OPS 只执行不决策 | 没写**提示**如何交接：谁来答 | §7：OPS 只读报告提示 + `answer_prompt`；答案由 L5/策略给 |
| 7 | `OPS` §4：多步动词是状态机 | 状态机里"选什么"各动词自己决定 | 状态机只负责**观察→回答→校验**；"选什么"来自 `policy.answer` |
| 8 | `OPS` §3：偏移经 `build_tables.json`/`KARDS_BUILD` | 已被运行时版本识别 + RVA 扫描取代 | 两份文档对应条目作废（见 `ARCH-RELEASE-VCS.md`） |
| 9 | `EVAL` §3.5.9：目录 `sim/{…}`、`eval/`、`policy/` | 现状已落成 `sim/{state,engine,adapter,chain,dispatch,effects}`、`evaluation/`、`policy/` | 以现状为准（`evaluation` 不叫 `eval`，避免遮蔽内建名） |
| 10 | `EVAL` §3.4：概率分支处理放评估 | 与 §2 一致，但没写**延迟项估值**与**挂起态估值** | §5 补上 |

## 9.4 sim 照原版移植（用户 2026-10-03）
`sim/` 的数据结构与规则函数一律**照 IDA / FModel 的逆向结果移植**，每条规则带原版出处（BP 函数 + 行号 / IDA 地址），不凭印象手写近似；读不到的记缺口、不编数。
原则、对照表（逐模块的原版出处与忠实度）、迁移顺序、出处注释棘轮见 **`SIM-FIDELITY.md`**。评估层（`evaluation/`）不得向 `sim` 注入规则。

## 9.5 接手原则：此刻生效的钩子必须从游戏读（用户 2026-10-03）
脚本可能是**中途接手**一局，不是从头跑的。⇒ `sim` 的初始状态里"哪些钩子在起作用"**不能靠历史推断**，必须读游戏自己的注册表：
* `BoardState.registry` = `BP_GameState_Battle::CardFunctionTriggers`（GS+0x588，`{触发号: [card_id…]}`，TSet 元素顺序 = 游戏迭代顺序）。它包含**不在场**但仍挂钩子的牌（弃牌堆里刚打出的指令、牌库里的天气牌……），每张牌由自己的钩子体判活（例：JUNGLE FEVER 看 `enterPlayOnTurn==本回合`）。
* `semantics.triggers.find_cards` 优先按注册表迭代；读不出才退回按快照扫（旧行为）。注册表读不出 ≠ 没有钩子，要进缺口。
* 同理其它"当回合状态"（天气是否已打、已累计的行动费、待生效的 pending 选择）也都要从游戏字段读，不从动作历史重放。

## 10. 迁移顺序（架构类，先后有依赖）——状态截至 2026-10-03
| 序 | 步骤 | 状态 |
|---|---|---|
| M0 | 物理拆分：`boardeval` → `sim/` `evaluation/` `policy/`（行为不变），依赖方向测试桥清空 | **已做** |
| M1 | **Prompt 模型 + 可挂起 run**：`sim/prompt.py`（`Option/Prompt/Done/Suspended/resolve/leaves`）；`sim.engine.{prompt_of,run,apply(path)}`；已接**抉择、预报两层（打出/部署/攻击/上线触发都算）、手牌目标**；`sim` 不再对分支取期望/取最好、不再注入 `evaluate`、去掉 `chance_v`；读不到种子由 L5 换随机种子+缺口 | **已做**（`tests`：`test_action_paths.py`、`test_arch_rules.py`） |
| M2 | `State.autoplay_queue`/`pending_cards`/`forecast`/`hand_target_fx`；动作边界冲刷 autoplay（`sim.chain.flush_autoplay`，免费打出）；回合开始强制决策（PBY 等）走同一个手牌目标入口（`policy.forced` + `rule.choose_hand_target`） | **已做**（autoplay 里的**交互**在冲刷点弹出 = 强制决策，目前无可入组的牌，未专门建模） |
| M3 | `policy/{plan,answer,forced}.py`：计划存取、"照计划点"统一入口（抉择/预报/手牌目标）、强制决策；`rule.choose_*` 只做分发 | **已做**（`policy.answer` 只含"照计划"一半；"重算"一半仍在 `rule` 里，是同一入口的两半） |
| M4 | OPS：`agent/promptinfo.py`（`pending()` → `PromptInfo`）、`AgentSession.observe_prompt()`、`agent/ops_result.py`（`Result`/`Receipt`/`adapt`）、`test_ops_rules.py`（lint 棘轮 + 类型约束） | **已做**（动词尚未逐个改返回 `Result`，现用 `adapt` 包） |
| M5 | `sim/ops.py` 类型化状态变更，替换效果摘要字典（Recorder 直接产出） | 待做（大工程；过渡期新增键必须过 `test_effect_keys_consumed`） |
| M6 | RVA 扫描 S2–S4、去掉手工版本表 | S1 已做；S2 需游戏 |
| M7 | OPS 包化：步骤 2（JS 抽文件）**已做**（`ops/agent.js.tpl`，逐字节一致）；步骤 3–7（L1 原语、查询分离、手势配方化、动词状态机、门面）待做 | 部分 |
