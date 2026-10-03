# TODO（2026-10-01）

# ★ 当前待办总览（2026-10-02 整理；**本节是唯一的待办看板，下面是按时间追加的历史日志**）
> 规则：新增/完成事项**先改本表**（状态 / 负责人 / 指针），细节写进指针指向的章节或报告；日志只追加，不当待办读。状态：**待做 / 进行中 / 阻塞 / 完成（验证）**。

| # | 事项 | 状态 | 负责 | 指针 |
|---|---|---|---|---|
| A1 | **盘外评估实现**（9 项）：指挥点夹取到 24、`vm.py` 虚函数默认接线与 `Map_Find`、`card_targets` EnumCompare、`boardeval` 13 个未消费键、同回合计价、静态卡提供者、补落盘断言、摧毁链去重、补接未接触发、实机验证 | 待做（DS 执行版 goal） | DS | `EVAL-ARCHITECTURE.md` §1.1；`reverse-data/reports/report/EVAL-NATIVES-AND-HOOKS-1.60.md` §8 |
| A2 | 实机验证两个疑点：手牌指令牌加密记录 `mult==0` 时 `getTotalDefense` 应为 99；`rule._death_fx` 修复后在"有 OnDestroyed 覆写的单位阵亡"局面非空 | 待做 | DS | `NATIVE-SPEC-GAPS-1.60.md` §12；提交 `5ab7149` |
| A3 | **钩子顺序与互斥专项调研**（多张反制谁先/能否同时、`GetStopFurtherActions` 各链位置、顺序敏感卡清单、触发表只加不删） | 待决定是否派 subagent | Claude | `EVAL-ARCHITECTURE.md` §7 |
| A4 | 94 个缺口函数的手写规格 | **完成**（待 DS 实现时用断言验证） | — | `NATIVE-SPEC-GAPS-1.60.md` |
| A5 | 实机尚未出现的四个标记 `+intel/+deckchg/+shuffled/+hooks` 凑组合验证 | 待做 | DS | §3i-8 |
| B1 | **延迟 ≤35 s/回合（软验收）**：预报 pick 走预测 + 按帧等待、`t_decide` 尖峰先分段计时/ cProfile 再决定、`exec` 尖峰（`end` 20 s、`move_up` 15 s）查因 | 待做 | DS/Claude | §3b-now；§3b' |
| B2 | **降帧**：步骤 0（只读）读 `t.IdleWhenNotForeground` 与 `bSmoothFrameRate`、前后台各测 `gtTicks`；再按需方案 A/B/C | 待做（需一局） | Claude | `BACKGROUND-THROTTLE-1.60.md` |
| B3 | 开局物理鼠标脚本改走注入 / 状态轮询；`nextmatch.py` "画面一闪仍在主页面"；遵守弯路 #40（先 `end_of_match_continue`） | 待做 | DS | `_nn_scratch/frame_waits_report.md`；§3c |
| C1 | **frida Temp 泄漏修复实机验证**：重启监听器后看 `D:\Kards\_frida_tmp` 与用户 Temp 是否不再增长；收尾脚本加 `sweep_frida_tmp(0)`；开局前磁盘检查 | 待验证 | Claude/DS | `INCIDENT-2026-10-02-frida-temp-leak.md` |
| C2 | **热重载后必须重启监听器**（零参 `super()` 问题）；根治：`hotreload.py` 改写旧类 `__class__` cell 或禁止含零参 `super()` 的类热重载 | 待做 | DS | CLAUDE.md 弯路 #39 |
| C3 | 崩溃相关：`obj_alive` 实机验证、ProcDump 收紧（不抓 frida 的第一次机会 AV）、旧转储清理；CLI `watch` 默认含 0x4000/termination（DS 的工具，只提请求） | 待做 | DS | §3b'' |
| C4 | 跨局缓存（座位/Logic）修复实机验证（0 个"不在我方手牌"、换牌 `exec=True`） | 待验证 | DS | §3i-6 |
| D1 | **OPS 重构**（拆 `ops/inject.py`、手势配方化、Result 类型、lint 测试） | 长期，等 A1 稳定后排 | — | `OPS-ARCHITECTURE.md` |
| D2 | 回放一致性测试台 | 可选 | — | `EVAL-ARCHITECTURE.md` §3.5 |
| E1 | IDA 未确认项（`MakeCardsFight` 等函数体、`GetStopFurtherActions` 语义、`MoveReason`、`+0xC08` 写者…） | 待做（按需） | subagent | `NATIVE-COVERAGE-1.60.md` §15.8；`NATIVE-SPEC-GAPS-1.60.md` §12 |
| E2 | 文档维护：`PLAN.md` 状态表与回执台账补行（`14bdf77`、`69a7877`、`3c7f2ef`、`5ab7149` 等） | 待做 | Claude | `PLAN.md` |
| — | **已完成（验证）**：攻击静默吞掉修复（30/30，窗外+后台）；拐棍删除；随机流种子定位；预报三选一预测（实机全中）与洗牌型预测；9 路评估接入 `choose_pick`；BySide 族与读侧纠错；攻击链钩子按 IDA 对齐（离线，未实机）；多份 IDA 手写规格 | 完成 | — | PLAN.md 回执台账；§3h/§3i |

> 说明：下面各节的"待做/待验证"字样大多是**写作当时**的状态，可能已过期；以本表为准，不一致时以本表加指针里的最新报告为准。已知需要清理的：§3b⁷ 出现两次（后一个是旧结论）、§3c/§4 夹在中间、§1 与 §3j 重复。

---
# 历史日志（按时间追加，勿当待办读）


## 1. 原生函数补全（subagent 审计给出的实现建议）
对象：`kardsmem/cardnatives.py` / `cards.py` / `kismetlib.py` / `agent/effectvm.make_read_hooks`。
构建：1.60.27292.launcher（SizeOfImage 0x9CC4000）。

> **现状（2026-10-02）**：本节由 §3j + `NATIVE-COVERAGE-1.60.md` 取代（普查/规格/伪代码/复核结论）；
> BySide 族已入库（`69a7877`）。**仍欠**：`vm.py` 虚函数默认值接线（GetPlayFromHandDamage 等 21 个）、
> `Map_Find` 出参约定、`card_targets.py` 的 EnumCompare 反向 —— 见该报告 §14.5。

- **关键词 = 标志位 OR 被贴能力**：`getHas*` 是 flag 字节 与 `receivedAbilitiesFromCards` 的或。
  （`hasShock@0x1E9` 对被赋予的冲击恒为 0。bot 侧已改走游戏 getter，VM 侧仍要补。）
- **IsPinned**：读 `pinnedTurns@0x27C`（>0）。
- **getTotalOperationCost**：行动费 + buff，下限 0（buff 在 `operationCostBuff@0xB4`）。
- **getMaxPossibleKredits**：现在错误地返回槽数；真值是全局上限，在 GameState+0x338（数值待读）。
  `getKreditSlotBySide` = 当前槽数（存储混淆：((enc^key@0x334)-add)/mul，结构体 +0x364 左 / +0x378 右）；
  `GetMaxKreditsBySide`（BP）同值；`setKreditSlotBySide` 不夹；BP `ChangeKreditSlotsBySide` 夹到 [0, getMaxPossibleKredits]。
- **HasCantAttack 系列**：靠 `customName1`。
- **getHasGameplayTag**：要含父标签和被赋予能力。
- **IsGotcha**：只有 type 11。
- **缺的原生函数**：IsVeteran、HasBond、HasCustomAbilityFromCard、IsDamaged、IsExile、HasMovementLeft、
  IsWeatherCard、各 buff getter、ShouldGotchaTrigger 算法。
- **Round** 用四舍五入（half-up）。**IsLocatedInDeck** 取 {1,2}。
- 收尾：加“换一张不同数据的卡，答案要跟着变”的断言（CLAUDE.md 弯路 #11），不要只测跑得动。
- 其它 VM 缺口：SEABORNE INVASION 的“生成两张步兵”没记录；TASK FORCE 44 / A FEW GOOD MEN / WAR BONDS /
  HEATWAVE / OVERCAST / COUNTER STRIKE 读不全或超时（`warm()` 6 s 上限是否够，需验证）。
- 反制（gotcha）建模：暂缓；保持“已激活不重复列出”。

## 2. 接入游戏随机流
依据：`memory/project_rng_stream_2026_10_01.md`（IDA，1.60）。

> **现状（2026-10-02）**：种子定位与 LCG 已实机验证（§3i）；预报/洗牌型预测已解出并实机命中（§3i-2/3i-3）；
> 9 路评估已落地（§3i-4）。剩余是"把预测接得更省时"（§3b-now 路径 1）与天气效果覆盖（§3i-4/3i-5）。

- 随机数是标准 UE `FRandomStream {InitialSeed@+0, Seed@+4}`：`Seed = Seed*196314165 + 907633515 (mod 2^32)`；
  `frac = asfloat((Seed>>9)|0x3F800000) - 1.0f`（float32）；`value = (int)(frac*n)`。
- 状态在 `ABP_CardFunctions_C` 单例：`cardsRandomStream @+0x2B8`（Seed@+0x2BC）；`encryptionStream @+0x2C8` 是另一条，别混。
- BP 包装 `RandomIntFromRangeWithStream` 在 1.58 每次多耗 d+2 次抽取（1.60 未验证，可对 `BP` 导出核对）。
- 只有 `MatchController+0x7A8` 非零时才会 `SetRandomStreamWithActionID` 重置种子。洗牌 = 正向 Fisher-Yates，N 次抽取。
- 步骤：(a) 实机验证：读已知效果前后 Seed，核对 LCG 推进次数与 +0x7A8 条件；(b) 在 VM 里实现以活种子为起点的
  `RandomIntegerInRangeFromStream` 等原生函数，让随机结果可精确预测；(c) 保留枚举分支作为兜底。

## 3. 后台模拟鼠标的时间刻问题
现象：游戏窗口失焦时板卡攻击大量被吞；前台 22/22 成功；失焦 + 悬停 1.0 s 也 9/9（测试仍在进行）。
曾经以前失焦也能打 ⇒ 某个回归只在失焦时暴露。

- 目前假设（**未验证**）：失焦时 UE 降帧，`SETTLE_HOVER` 0.5 s 与按下/拖动之间的 0.06~0.08 s 期间游戏一帧没跑，
  悬停转发、箭头 `overCard` 更新没发生 ⇒ 松手提交被静默跳过。
- 已做（临时）：`SETTLE_HOVER` 回 1.0；`prime_arrow` 后等待 0.8 → 1.0；`nn.py._ensure_foreground()` 已写但目前被 `if False` 关掉。
- 待做：
  1. 收尾这轮测试（12 次），记录失焦+1.0 s 的成功率。
  2. 对照：失焦 + 0.5 s，确认 0.5 是否就是回归。
  3. 失焦时实测帧间隔（游戏线程调度一个来回的耗时；前台约 18.7 ms）。
  4. 若成立：把等待改为**按游戏帧数**（或等 `ReceiveTick` 计数前进 N 次），而不是固定秒数；用户要求的 0.5 s 手感可保留为**前台**值。
  5. 决定 `_ensure_foreground` 去留（会打断用户在别的窗口操作）；悬停 `leave=True`（Exit + DestroyShowCaseCard）是否必要也要判。
  6. 落地诊断：`_attack_once` 已加只读 `diag`（`PlayState/bEndTurnQueued/myTurnHasStarted/CanPlayCard`）写进 `out["diag"]`；
     监控里 `diag` 目前全是空——失败时再看；本轮 0 次失败。
- 其它时序相关点：`wait_queue_idle`、`_receipt` 3 s 等待、`mulligan` exec ≈12 s、结束回合 exec 0.5–4 s、
  数据侧“right after a prior action: hand actor not found (0.03 s)”拒绝。

### 3 的进展（2026-10-01 夜，**未实机验证**）
- 用户确认：失焦降帧 ⇒ 固定短停留不够，这个解释基本有定论。
- 测试记录：前台 22/22；失焦+悬停 0.5 s 约 2/25（早前几局）；失焦+悬停 1.0 s（前台是 Claude 窗口）10/10 后这局结束。
- 已改代码（重启 watcher 后才生效）：`ops/inject.py`
  - JS/CModule：PC `ReceiveTick` 在游戏线程每进一次 `job[12]++`；导出 `gtSetTickFn` / `gtTicks`；
    `_gt_install` 始终设 tick 函数（与 `KARDS_GT_FILTER` 无关）。
  - Python：`Injector.settle(seconds, frames, cap=4.0)` = 墙钟至少 `seconds` 且 PC tick 至少前进 `frames`；
    手势里所有 `time.sleep(SETTLE_*)` 都换成 `self.settle(...)`（悬停 6 帧，其余 2 帧）。
  - `SETTLE_HOVER` 已改回 0.5（用户要的手感；降帧由帧数兜底）。`prime_arrow` 后等待 0.8 → 1.0。
  - `_attack_once` 加了只读 `diag`（`PlayState` 等，写进结果 `diag`）。
- `agent/nn.py`：`_ensure_foreground()` 写好但被 `if False and ...` 关掉，等验证是否需要。
- **下一局要做的验证**：重启 watcher ⇒ 把游戏放后台 ⇒ 打 12 次攻击，要求成功率 ≈ 前台；
  同时看 `gt_ticks` 在失焦时的增速（帧率）。通过后删掉 `_ensure_foreground` 或保持关闭，并更新记忆。

## 3b. 单回合耗时目标（用户 2026-10-01：压到 ≈35 s 以内，对真人局就够用；实力"勉强过关"）

### 3b-now. 2026-10-02 凌晨实测（机器人接手用户那局，T40/T42 超标）——**目标未达**

PLAN 阶段 2 硬指标：**单回合 ≤35 s（连续 3 局、≥90% 回合达标）**；部署 ≤3 s。

实测（`rule-live-20261002-003041.jsonl`，接手局）：T40 **62.2 s**、T41 9.4 s、T42 **112.6 s**、T43 13.5 s
⇒ 4 个可量回合里 2 个超标（50%，要求 ≥90% 达标）。

**时间去哪了（逐项测出来的，不是猜）**：
| 项 | 实测 | 说明 |
|---|---|---|
| `t_decide`（束搜索 + VM 效果枚举） | **3.2–9.7 s/步** | T42 十个动作里每步都在 3–10 s |
| `t_exec`（注入 + 回执） | 1.0–3.9 s/步 | 攻击类偏 3.7–3.9 s |
| 预报 `pick` 一次 | **5–7 s** | T40 有 6 次 pick ≈ 40 s；其中一次"候选未就绪"**干等 8.9 s** |
| 每步无归属间隔 | 5–6 s | Δ 减掉 snap/decide/pre/exec 之后剩下的（等动画/轮询） |

**压时间的三条路（按性价比）**：
1. **预报 pick 走预测**（今晚已具备）：候选 actor ≤3 s 渲染完（用户实测）⇒ 面板一开就按预测的
   位置点，不再等"候选可读"；`nn.py` 的 `hold` 分支与 `layer_gone` 重试都要改成**按帧等待 + 3 s 上限**，
   并记录实际等待时长。**预计省 ~3–5 s/次 pick**（T40 那种一回合 4–6 次 pick 的场景最明显）。
2. `t_decide`：VM 预算已限 3 s/回合，但**束搜索本身** 3–10 s ⇒ 降 `beam/branch/depth` 或加缓存
   （**这是拿棋力换速度，需要用户拍板**）。
3. `t_exec` 的 `receipt/after` 等待 + 每步 5–6 s 无归属间隔 ⇒ 先加分段计时把它钉出来（谁在等）。
实测（失焦、帧数等待版）：简单回合 1–15 s；2 次攻击的回合 34–37 s；上一局 4–6 个动作 35–66 s。
- 回合 7 用了 **121 s**：两张 OVERCAST（两阶段预报选牌）≈100 s —— 每次 `pick` 执行 ≈5.8 s 且连续 3 次报"回执滞后"，
  再加 ≈40 次 1.3 s 的"没有可执行的动作"空轮询（候选牌异步加载未就绪）≈50 s。**这是最大的一项。**
- 其它杠杆：攻击里空拖预热（`prime_arrow`）每回合只做一次；回执等待在动作流出现后立刻返回（现在固定最多等 3 s）；
  快照/预检合并；悬停 0.5 s + 帧数兜底（已改）。
- 验收：连续 3 局，90% 以上的回合 ≤35 s。

## 3b'. 耗时/失败的实测结论（2026-10-01 夜）
- 攻击新路径（单次拖拽、等箭头+帧数，`KARDS_ATTACK_PRIME=1` 退回旧路径）：失焦时 exec ≈3.6 s（旧 ≈6 s），成功率见下；
  唯一失败/类似失败都出现在"本回合刚上线（move_up）"附近。
- **上线被拒的原因（已改，待验证）**：`OnActorMouseUp` 用 `GetTargetArrowTargetCard` 读箭头 `overCardID` 覆盖 `cardUnderCursor`，
  箭头上残留上一次攻击的目标（41）⇒ 移动被当成攻击。`drag_release(arrow_length_gate=True)` 现在同一次 JS 里把 owner 箭头的
  `overCardID/overCard` 清零。验证：重启后看 `cursor_state.cardUnderCursor` 应为 0，且 move_up 成功率。
- 部署 ≈1.85 s（预检 ≈1.0 + 拖拽 ≈0.87 + 回执 0）；预检（`precheck_play` + `game_can_play_from_hand`）可合并/缓存再省 ≈0.5–1 s。
- **5th RANGERS 部署 ≈7.3 s**：回执 5.4 s（稳定复现）——游戏自己晚写 `XActionPlayCardFromHand`，不是固定等待；
  要提前返回得先确认这段时间面板/状态（`pick_pending`/`card_being_played_from_hand`）是否已就绪。
- 选牌（`pick_choice`）≈3.0 s：扫候选 0.7 + 点击 1.7 + 回执 0 + 事后扫 0.6；前后两次候选扫描可合并。
  曾出现一次 60 s（OVERCAST 链，未复现，已加分段计时 `x_timing`）。
- 回合开始后紧接的出牌被拒（提示"友方回合/友方失去了前线"，exec 0.03 s）且没有重试——需要"提示横幅期内等一下再发/重试一次"。

## 3b''. 新崩溃（2026-10-01 09:49，转储 `kards-data/crashdumps/…_094945.dmp`）
- 预报第二层选牌点击时，注入的 `ProcessEvent` 在游戏线程上 AV：游戏模块 RVA `0x159CECE`（= `UObject::ProcessEvent+0x2DE`，
  `ProcessEvent` RVA `0x159CBF0`），访问地址 `0x1e3200c98`（垃圾虚表/已释放对象，同 0x1e3020030 一族），
  调用栈是 frida→runOnGT→callPE。随后游戏**卡死**（进程还在、画面不动；用户手动关掉的，不是自己崩溃退出）。
- 解释（强推断）：被点的候选 actor 已被销毁/回收（第一层的 actor 在第二层弹出时被回收），`pick_candidates` 读到了陈旧指针。
- 已改（未实机验证）：`Injector.obj_alive(obj)`（InternalIndex→`FUObjectItem`，Object 指回自己、无 Garbage 0x200000 /
  BeginDestroyed|FinishDestroyed）；`click_actor` 入口和 `pick_choice` 过滤候选都先判活。**obj_alive 的偏移还没核对**
  （只读脚本里 PC 取到 0 —— 游戏已退出，没法验证）：下次开局先用只读脚本对"普通对象/Garbage 对象"各抽样检验。
- 同时：ProcDump 对 frida 自己处理的第一次机会 AV 也落了 3.3 GB 的转储（8 份约 26 GB，D 盘剩 ~62 GB）——要清理/收紧。

## 3d. 预报选牌评估（用户 2026-10-01）
- 预报（OVERCAST 一类，两层三选一）：第一层 3 张天气、第二层各 3 张 ⇒ **共 9 种结果**，评估时应对 9 个结果各估价，选**最优那一条路径**
  （现在 `规则2：三选一/二选一` 一律点 index 0，没有评估）。

> **现状（2026-10-02）**：已落地 —— 候选用活种子预测（§3i-2/3i-3），第一层走 9 路评估（`meta.forecast_eval`，
> `pick_eval_src=forecast_9path`，§3i-4）。已知缺口 = 天气牌效果取不全（§3i-4/§3i-5 + NATIVE-COVERAGE §14.5）。
- 做法：用候选 actor 的 `Name_0`/`indexOfCardInDeck`（`pick.forecast_candidates`）拿到每层候选的卡名；
  天气卡在回合开始才生效（`OnStartOfTurn`，见 CLAUDE.md 弯路 #26），评估要看它们的开局效果（VM 读 `OnStartOfTurn`/`weatherCardChosen`）。
- 注意第二层候选依赖第一层的选择，要在点第一层之前就能列出 9 种组合（读 `GetChooseSpawnCards` 的两级结果）。

### 3d 补充（用户 2026-10-01："预报就是抽取"）
- 预报候选是**用卡牌随机流抽出来的**（实机：种子 1650674124 → 3038477212 恰隔 16 步，期间出过 OVERCAST）。
  ⇒ 用 `kardsmem/rng.py` + 活种子，可以在打出预报牌**之前**就算出两层候选（9 个结果），不用等候选弹出；
  同时省掉"候选加载 6~30 s"的空轮询。
- 待查：`GetChooseSpawnCards`（天气三张各自的 BP）里怎么抽——`Array_ShuffleFromStream` 还是 `RandomIntFromRangeWithStream`、抽几次、
  第二层是否在第一层选定后才抽（抽取顺序决定能否提前预测第二层）。用 1.60 BP 导出读，再用实机对照（读种子→预测→比对弹出的候选）。

### 3d 线索（已读 1.60 BP，2026-10-01）
- OVERCAST 本身**没有** `GetChooseSpawnCards`：`OnPlayedFromHand` = 双方各抽 1、双方各 +1 指挥点槽、再 `cardFunction->Forecast(this)`。
  `BP_CardFunctions::Forecast` 之后才弹两层选牌——**先读 `Forecast` 的 BP**，看它怎么拿到第一层候选。
- 天气牌（`card_event_sunny1_blue_sky` 等）的 `GetChooseSpawnCards`：先 `GetAllActiveStaticCards(true,true)` 按标签 `subtype.sunny` + `subtype.heavyWeather/…` 分到
  `_lightWeatherCards/_mediumWeatherCards/_heavyWeatherCards` 三个池，然后**依次**对 轻→中→重 各做一次
  `RandomIntFromRangeWithStream(0, len-1)`（每次 d+2 次抽取）取一张，写进 `_forecastOptions`；之后复用 `_forecastOptions`。
  ⇒ 候选 = 三次包装抽取的结果；**池的顺序**取决于 `GetAllActiveStaticCards` 的返回顺序（要读内存里那个列表）。
- 实验脚本：scratchpad `forecast_try.py`（对手牌里的天气牌跑 `GetChooseSpawnCards`，用活种子，看出参 `cards`）；OVERCAST 上找不到该函数，得换成第一层真正用的那张牌。

### 3d 进展（2026-10-01 夜）
- 已读 `Forecast`（`BP_CardFunctions.cpp:23707`）：第一层**固定** = `card_event_sunny1_blue_sky` / `card_event_rain1_mist` / `card_event_storm1_gale`；
  `GetAllActiveStaticCards` = `GameStateRef->GetAllStaticCardsSortedByName`（**按名字排序**）再按 cardSet/保留/黑名单过滤。
- 已写 `semantics/forecast.py::predict_layer2(km, 第一层卡对象指针, seed)`（VM 跑 `GetChooseSpawnCards`，未实机验证，只过导入）。
- 卡在：第一层候选 actor（`ABP_ChooseCardToSpawn_C`）只给 `Name_0`（内部名），**没有静态卡对象指针**；VM 要跑的是那张牌对象的 `GetChooseSpawnCards`
  （需要 `cardFunction`、`_lightWeatherCards` 等成员）。出路二选一：
  ① 从 GameState 的静态牌表（按名字排序的那个数组）里按名字取到 `card_event_*` 的静态对象指针，再喂 VM；
  ② 直接在 Python 里复刻：池 = 静态牌表里带 `subtype.sunny/rain/storm` + 轻/中/重标签的牌（保持名字序），
     依次 `Stream.wrapper_int(0, len-1)` 三次，不跑 VM。② 更稳，需要读牌表 + 标签（`getHasGameplayTag` 已有读法）。
- 验证：种子在 OVERCAST 打出到第二层弹出之间的变化（实测 16/18/19 步 = 三次 wrapper 的 3·(d+2) 之和，范围 9~36，吻合）。

### 3b‴. 攻击偶发失败的新线索（2026-10-01 夜，用户 2026-09 就怀疑过）
- **真实光标停在游戏窗口内**：`clk.py`（点开始/投降后的 `park_cursor`）把光标停在窗口里（实测 (1673,1077) ∈ 窗口 1200–2240×285–1092）；
  一次失败里 `after.mouseOverActor` 非零（别的失败里都是 0）。真实鼠标在窗口内会让游戏按真鼠标改写 PC 悬停链。
  已做：clk.py 改停到 (200,700)；`nn.py::_park_cursor_outside()` 每个动作前若光标在窗口内就挪到窗口外。待验证：挪出后失败率是否降到 ~0。
- 失败不是"慢结算"：失败后再多等 7 s（共 10 s）仍无 `XActionAttackCard`；`queueIsRunning` 提交后为 False。
- 游戏窗口当前 1024×768，位置随重启变化——点击坐标要按当前窗口截图换算。

### 3b⁗. 用户观察（2026-10-01 夜）：真鼠标放在总部上，攻击就能打
- 说明提交时有某个状态依赖"真鼠标悬停在目标上"：游戏每帧按真光标重算悬停，我们合成的 `OnActorMouseEnter(目标)` 会被下一帧覆盖成"离开"。
  之前把光标挪出窗口的做法**未必对**（`nn.py::_park_cursor_outside` 现在默认关，`KARDS_PARK_CURSOR=1` 才开）。
- 已加诊断（下局生效）：`drag_release(pre_commit=…)` 在提交前只读快照箭头 actor 的全部标量字段 + PC 悬停状态（`_attack_probe`），
  记进日志 `extra.x_pre_commit`；每步记 `extra.cursor_in_window`、攻击重试 `attempt`。对比成功/失败时箭头哪个字段不同
  （候选：箭头自己的"悬停有效/可攻击"布尔、`overCard` 有效标志）。
- 若找到字段：像 `overCardID` 一样在提交的同一次 JS 执行里写它；找不到再考虑"提交前把真实光标移到目标屏幕位置"（要从 actor 位置投影到屏幕）。

### 3b⁵. 结论修正（用户 2026-10-01 夜："移出鼠标后一直能打，目标是移出鼠标后一直能打"）
- 回看：**光标在窗口外**时攻击一直成功（22/22、12/12、12/12、快速路径 5/5，都是 keepfg 的 (200,700)）；
  **光标在窗口内**（clk.py 停放位置，或用户把真鼠标放在总部上）才有成簇失败/个别成功。
  ⇒ 目标 = 光标在窗口外也**永远**能打。`nn.py::_park_cursor_outside()` 现在默认**开**（每个动作前保证光标在窗口外）。
- 注意：用户自己动鼠标会把光标带进窗口；那段时间失败是预期的（真鼠标悬停会重算 PC 悬停链）。
- 下局验证：全程光标在窗口外，期望攻击 100% 成功；若仍有失败，看 `x_pre_commit`（箭头字段）。

## 3e. VM 跑"其它牌挂的触发钩子"（用户 2026-10-01 要求；设计已想好，**未实现**）

> **现状（2026-10-02）**：`semantics/triggers.py` 已落地 —— 0x2B 进场族 + 反制三兄弟 + **攻击族
> （0x0D/0x04，`88fcde6`，CARD-PLAY-HOOKS §6 顺序）**；**尚未接进 eval**（对"将落场的手牌"用探针跑钩子 →
> 折进 sim 的攻击后效），见 §3i-8"下一步"。本节其余设计（`side_hits` 目标表达等）仍未做。
- 现状：`record_effects` 只跑**被打出这张牌自己**的钩子（`OnPlayedFromHand`/`OnDeployed`/`OnAfterAttack`）；场上别的牌挂的触发（例：某个热浪
  "本回合单位部署时，对随机目标造成等同其攻击力的伤害"；COUNTER STRIKE 的 `OnOtherCardAttacks`）评估时看不到。
- 范围（用户口径）：**指令类（本回合触发）要做；反制是敌方回合触发，不好处理，先不做。**
- 游戏的分发方式（1.60 BP 已读）：`BP_CardFunctions::Execute…Events` 里 `FetchAllCardsWithEventTrigger(<触发号>)` 取出场上挂了该触发的牌，
  逐个调 `OnOther…`：部署/入场 = `ExecuteOnEnterPlayEvents`（触发号 `0x2B`，对每个**不是入场牌自己**的牌调 `OnOtherCardEnterPlay(cardPlayed, method)`，
  `EOnEnterPlayMethod`：1 OnPlayedFromHand / 2 OnAddedFromHand / 3 OnSpawn / 4 OnReveal / 5 OnConverted）；
  攻击后 = `ExecuteOnAfterAttackEvents`；受伤/移动/出牌后等见 `BP_CardFunctions.cpp`（`ExecuteOn*`/`Execute*Events`）。
- 做法：新 `semantics/triggers.py`：对每个动作，枚举场上（及相关区域）**定义了对应 `OnOther…` 覆写**的牌（`kismet.find_function(uc,name,inherited=False)`，按类缓存），
  用被动作牌作参数跑它们的钩子，记录到的效果并入该动作的收益。第一版只做 `OnOtherCardEnterPlay`（部署单位）。
- ★ **RNG 在同一个 Action 里可能被推进多次**（用户提醒）：整个动作共用**一个** `rng.Stream`：先跑自己的钩子，再按触发顺序跑别的牌的钩子，
  抽取数累加；顺序 = 游戏 `FetchAllCardsWithEventTrigger` 的顺序（还没查清，先按卡牌 id 升序，并在结果里标注"顺序是近似"）。
  现在 `record_effects` 每次都新建 `Stream(seed)`，要改成可传入**同一个**流（`rng_stream=`）。
- 效果摘要的局限：现有 `eff` 只有一个"目标"概念；"对随机目标造成 N 点伤害"要能表达**具体受击的牌**——需要新键（例如
  `side_hits: [{target_id, damage}]`），并让 `boardeval._apply_eff` 认它；随机目标用 `GetRandomCard`（有种子时是确定的）。
- 验证：先离线用合成场面+假卡跑通共享流顺序；实机用热浪类天气牌 + 单位部署，对照游戏结算的受击目标。

### 3b⁶. 攻击失败的直接证据（2026-10-01，`x_pre_commit` 对照）——**已被 3b⁷ 推翻（mouseOverActor 不是因果）**
- 提交前快照：成功那次 `PC.mouseOverActor` = 目标（非零）；失败那次 = **0**。箭头上其余字段（overCardID/overCard/lastOverCardID…）两次相同。
  ⇒ 真鼠标不在目标上时，游戏帧 tick 把我们合成的悬停清掉；提交时"悬停在目标上"不成立 ⇒ 被吞。真鼠标放在总部上就能打也是这个原因。
- 改动：`drag_release` 在提交的同一次 JS 执行里把 `PC.mouseOverActor` 写回 `hover_other`（目标 actor），记 `out['pc_hover_rewritten']`。
- 验证：下局攻击成功率应 ~100%（光标在外也一样）；失败时看 `x_pre_commit.pc.mouseOverActor` 是否已是目标。
- 同类风险：所有"悬停→停留→提交"的手势（出牌带目标、选目标、选手牌目标…）都可能被同样的 tick 清掉；这次只改了攻击，其它等数据再说。

### 3d 更正（用户 2026-10-01）：预报选牌**必须按当前场面评估**，不是"花费越低越好"也不是"第一个"
- 现状原因：`rule.choose_pick` 的打分 `w_cost*花费 + w_stat*(攻+防)`；预报候选行（`BP_ChooseCardToSpawn_C`，只有 `Name_0` 内部名）没有花费/攻防，全部按 0 算，
  严格大于只保留第一个 ⇒ 每次都选 index 0（与场面无关，"第一个最便宜"只是巧合）。
- 要做：对每个候选（第一层 sunny1/rain1/storm1；第二层按种子预测出来的 3 张）读它的效果（VM 跑钩子；天气牌是**下回合开始**时 `OnStartOfTurn` 才生效，
  见 CLAUDE.md 弯路 #26），套到当前我方/敌方场面（单位类型、数量、前线归属）上用 `boardeval` 估值，选最优路径（两层 = 最多 9 条）。
- 候选牌花费读法备查：`UFunctionLibrary::GetStaticKredits(FName cardName, int32* kredits)`（SDK `kards_classes.hpp:513`），需把候选名转成 `FName`。
- 先决：`semantics/forecast.py`（种子预测第二层）+ 第 3e 节的钩子触发模拟。

### 3d 补充（用户 2026-10-01）：天气效果的"贴膜"语义
- 有几个天气效果是**本回合**给单位"贴膜"（临时修饰，**部分作用于敌我双方**）。因此评估预报选项时要看：这些贴膜会让**交换（打单位）**变亏还是变赚；
  **打总部**不受影响甚至更有利 ⇒ 选项的价值要随"本回合我们打算怎么打"变化（天气生效那回合应偏向打总部、少换子）。
- 做法：把天气牌效果读成"本回合对双方单位的 buff/debuff"（VM 读钩子 + 现有 `buffsFromCards` 贴膜表，见 CLAUDE.md 弯路 #24），
  `boardeval` 里模拟攻击时把这些修饰算进去（攻防变化 → 交换结果），再比较各选项下的最优走法价值。

### 3b⁷. 攻击静默吞掉：IDA 结论（2026-10-01，`reverse-data/reports/report/ATTACK-SILENT-DROP-1.60.md`）——**已改，待实机验证**
- 根因（读码推断，未实测）：`PlaceBoardCard` 要求 `LocationUnderCursor∈{5,6,7}` 且 `RowUnderCursor=true`，否则**静默 return**。桌面端只有 `OnActorDragTick`
  按 `PC.mousePositionVec2D` 写这两项；光标在窗外时 `GetMousePosition` 失败，PC 字段停在旧值 ⇒ Location=0/Row=false ⇒ 攻击被丢。真光标停在目标上则 5/6/7 + true ⇒ 能打。
- `mouseOverActor` 写入、`BoardCardUnderTargetArrow` 写入、`targetArrowFinalLength=4000` 均**非因果/冗余**（可留可删）。
- 已改：`ops/inject.py:4832` `_attack_once` 传 `cursor={"card_under_cursor":t,"location_enum":7,"row":1}`（与 MouseUp 同一次 writeThenCalls）。
- 待做：① 热重载 ops.inject 后，**光标在窗外 + 游戏后台**连打 ≥12 次攻击，统计成功率；② 提交前读 `PC+0x828`(mousePositionVec2D)、`atk+0x668/0x669`，确认窗外时是旧值；
  ③ 若仍失败，查 `_attack_probe`/`x_pre_commit` 与 `AttemptToAttack`（1.60 未复核）、是否残留第二支箭头（`OnActorMouseDown` 不清旧箭头）；
  ④ 成功后把 `_park_cursor_outside`/`_cursor_to_target`/多余的 hover 写入清理掉；同类手势（带目标出牌、选目标）是否也依赖 Location/Row 一并查。

## 3f. 本轮新增待办（2026-10-01 晚）
- 开局流程仍是物理鼠标（`startmatch.py`、`do_mulligan.py`、`mull_auto.py`、`hand_calib.py`、`vendor/actions.py`）：无帧计数、固定 sleep。
  按 `_nn_scratch/frame_waits_report.md` 建议：改走注入的 widget 点击 / 点击前窗口置前 / 轮询状态代替固定等待；注意与 `_park_cursor_outside` 冲突（物理点击需要光标进窗口）。
- 检查其它 Agent 改的 `nn.py::_settle()`（转发 `precheck.call_write("settle",…)`）是否拖慢每步；对比改前每回合耗时（目标 ≤35 s）。
- 热重载（`player/hotreload.py`，flag `_nn_scratch/hot_reload.txt`）只重载 Python；JS/CModule 改动仍要重启监听器。实机用法与坑见测试文档 `_nn_scratch/prompt_attack_test.md`。
- `pick_candidates` 关卡 actor 快速路径：实测耗时 + 刚生成的候选是否可见，仍未验证（用户质疑过"236 个 actor 0.00 s"）。
- 预报评估（3d）、触发钩子（3e）、原生审计（1）、RNG 实战校验（2）、开局首回合 `drag_success=0`、mulligan 9–12 s、第 2 回合结束回合 4 s：均未动。

## 3j. 原生函数全量普查结果（2026-10-02，两个 IDA subagent；会话 f773083b）→ 取代 §1 的"缺的原生函数"清单
报告：`reverse-data/reports/report/NATIVE-COVERAGE-1.60.md`（普查 + 规格 + 伪代码 + 12 条断言）、`BOARD-QUERY-NATIVES-1.60.md`（场上卡枚举一族）；数据 `_nn_scratch/census2/`。
- 普查：3044 个 cpp、8926 个被调用名；卡/CardFunctions/Logic 里 2823 个蓝图（VM 直接跑）、328 原生 + 23 虚原生事件；
  `UBaseCardObject` 原生 131 个被调用，54 个有同名实现（**只按名字对，未验正确性**）、77 个没有（850 个调用点）。
- 事实更正：Dumpspace 的 RVA 对；`.idmap` 的 exec 名整体错位一个函数，其 `UBaseCardObject_VFT`(0x147E468A8) 是错的；
  真 vtable = **0x147E18D38**（构造器 0x144ABB9B0）。24 个虚原生事件的默认实现都已反编译（都是"回显输入/置 0"）。
- **场上卡枚举一族全是蓝图**（VM 直接跑，之前"未实现"疑是解析到 Stub 空桩，待复核）：`GetCardsOnBoardBySide/GetAllUnitsOnBoard/…`；
  过滤 = 当前防御>0 ∧ 在场 ∧ side ∧ covert 过滤 ∧（unitsOnly⇒IsUnit）；**含 HQ**，不含手牌/牌库；顺序 = `AllCardsInBattle`（TMap）稀疏数组下标升序 = 创建顺序。
  `GetRandomCard`：空表不抽；1 个元素仍抽 1 次；有 `AlwaysSelectedAsRandom` 牌时只在其中抽 1 次。
- **我们现有代码的错（按优先级）**：
  1. `effectvm.board_cards` 漏 HQ、缺 defense>0；`kardsmem/board.py:917 sorted(...)` 丢了真实顺序（影响热浪类随机目标）。**优先改成让 VM 跑蓝图，只补原生叶子**。
  2. `GetPlayFromHandDamage`（256 次调用，129 张卡覆写）：默认 damage=0，**必须先查覆写再回退默认**，不能放进 `cardnatives._TABLE`（会盖住覆写）。
  3. `CanBeTargetted` 恒 true、无卡覆写；我们的 `APPROX` 比游戏更严，去掉；smokescreen/covert 在蓝图里判。
  4. `EnumCompareSide/CardLocation/Faction/Type`（约 270 处）：`Branches = (a != b)`；VM 缺，`card_targets.py` 疑似写反（待双阵营用例验证）。
  5. `getTotalOperationCost = max(0, +0xB0 + +0xB4)`；`getTotalKreditCost = clamp(kredit+buff, 0, 99)`（我们缺下限 0）。
  6. `IsTank/IsFighter/IsBomber/IsInfantry/IsArtillery` 在 `customName1` 含 `isAlso*` 时也为真（意大利骑兵）；`CustomName1/2HasAttribute` 按 `;` 分段整段匹配（我们遇非空就抛）。
  7. `getHasBlitz/Guard/Fury/Ambush/Shock/Alpine/Smokescreen` = flag OR `receivedAbilitiesFromCards[name]` 非空；`getHasImmune` = `isImmune@0x289` OR 同上（我们缺）。
  8. `HasCampaignUpgrade` = `id in activeUpgrades`(TArray@0x328)，PvP 恒空；24 个 `Campaign*` 写状态，VM 里当 no-op。
  9. `getAttackTempBuffAmount/getKreditTempBuffAmount` 读 `buffsFromCards[instigator].BuffMap["attack_tempBuffGive"/"kredit_tempBuffGive"]`；`GetCombatKeywords` 顺序 1,2,3,4,6,7，再加 5（重甲>0）。
  10. **kismetlib 大小写**：字节码调的是小写 `abs/round/max/min`，我们的键是 `Abs/Round/Max/Min`，约 100 处全部落空。
  另：`hand_cards` 没排除三张预报牌。
- 仍未确认：`Map_Values` exec、`IsUnit` 内的哈希、`SortCardsByLocationNumber`、`Campaign*`/`CustomName*Add/Remove`/`CreateCopy`/`CopyData`/`setAndEncrypt*`/`ApplyGameplayEffect`、
  若干"类比推断"的 getter；`CustomNameNHasAttribute` 是否大小写不敏感。无副作用约束：`WhichChooseOne`(0x11D) 与 `OnSuppressed` 默认有副作用，只能记录不能执行。
- 做法：按 `NATIVE-COVERAGE-1.60.md` 的 PRIMS 风格伪代码落地；每个新原语配"换一张不同数据的卡、答案要跟着变"的断言；`selftest` 两个构建都过。

## 3c. 已知 bug（先不修，用户 2026-10-01 指出）
- `nextmatch.py`（`end_of_match_continue`）点击后画面**一闪**，但仍停在主页面（没进卡组页/没开局）；有时反而已经自动开了一局（上一局投降后观察到）。
  待查：`end_of_match_continue` 的步骤序列（step 0→1→7）、结算页"继续"之后的真实落点、是否会触发自动再来一局。

## 4. 杂项
- 更新记忆：游戏线程调度、A/B 结果、前台/失焦对攻击的影响、ProcDump 启动顺序、Alt 置前技巧。
- 让 DeepSeek 的 CLI `watch` 默认包含 0x4000 与 termination（我的 API 脚本已这样做）。
- NZANS 旧 AV 崩溃未解释（可能与非游戏线程调用有关）。

### 3b⁷. 攻击修复实机验证（DeepSeek 2026-10-01，停放=开）

- 训练局（AI，对手 player_id=-2040；光标全程窗口外、游戏后台）：攻击 **8 次成功 6 次（75%）**；其中窗口外 4 次成功 4 次。
- 报告：`_nn_scratch/attack_test_report.md`；脚本：`_nn_scratch/attack_test_report.py`。
- 写入点：`ops/inject.py::_attack_once` 提交批次 `cursor={card_under_cursor, location_enum=7, row=1}`。

## 3g. 效果估值排期（用户 2026-10-01 晚定的优先级）

**先不做（只留档）**
- **召唤类 yield = min(声明张数, 实际空位) + "先腾位再召唤" 的顺序搜索**（§3d/§3f 的"4K 白花 + 少拉一张"）：
  理想解是"把一整个友方回合的动作自由组合、搜完再提交"，但那对 RPC 的要求太高（每步都要注入调用）⇒ **放弃整回合联合搜索**；
  将来若做，至少要同时满足两件事：① yield 依赖**当前（模拟后）盘面空位**（`ops.inject._free_slot()` / `gap_number` / `deploy_location_number` 已有读法）；
  ② 顺序可表达（`move_up` 先于召唤）。用户 2026-10-01：第一张 PARACHUTE ASSAULT 拉 2 张"合情合理"，但先上线能多拉一张。

**先做（按此顺序，收益/难度都递减）**
1. **随机流（§2）—— 收益最大、最难，先啃**：
   - 读 `ABP_CardFunctions_C::cardsRandomStream @+0x2B8` 活种子（`Seed@+0x2BC`）；LCG `Seed = Seed*196314165 + 907633515 (mod 2^32)`；
     包装 `RandomIntFromRangeWithStream` 每次 `d+2` 次抽取（§3d 实测 16/18/19 = 3·(d+2) 吻合）。
   - 验证三步：(a) 同一张 3 选 1 前后各读一次种子，核对推进步数；(b) **面板弹出之前**预测三个候选，再和实际候选名比对；
     (c) 不中先查池顺序（`GetAllAvailableStaticCards` 按名字排序 + 标签过滤，见 §3d 线索）。
   - 工具：`kardsmem/rng.py`（已有 `Stream`）+ `_nn_scratch/forecast_try.py` 的思路。
2. **3 选 1 评估（§3d 更正）**—— 依赖 1：用预测出的 9 条路径 × 效果 VM × `boardeval` 在当前场面估值，取代"点第一个"
   （实测日志里 `规则2：三选一/二选一 → <内部名>` 就是没评估）。
3. **本回合 buff 的消费模型**—— 价值 = 同回合内**后续**攻击的 delta；"贴完到回合结束没有攻击"记 0
   （实机：T17/T23 的 HEATWAVE 贴完就 end；T19 第二张 PARACHUTE ASSAULT 记 +2.99 却什么都没做）。

**判据留痕**：每一步都要在 `rule-live-*.jsonl` 里写下"预测 vs 实际"和每个候选的估值（预测不中就靠它复盘）。

### 3g-2. **eval 的口径（用户 2026-10-02 定义，实施时别跑偏）**

> "eval 要求只是把打出这一步的结果在**盘外再算一遍**就行。除去友方反制比较特殊外，
>  其它都按『在盘外再算一遍』来考虑。"

- **牌库/疲劳/洗牌已升格为一等状态**（用户 2026-10-02 追加口径："游戏里此时会发生什么，我们就模拟什么"）：
  * 权威结构 = `GameState.DeckCardIDs_Left/Right`（id 数组，index 0 = 牌顶；`BP_Deck_C` 只是表现层）。
    模拟 = `Sim.deck`（id 列表，可洗/可插/可删）+ `Sim.deck_cards`（id→模板）。
  * 抽牌按真实牌序抽（按抽到那张自己的持有价值 + 它自己的 `_on_draw`）；**空库继续抽 =
    `ApplyFatigueDamage`**（伤害 = 当前疲劳计数，然后计数 +1；初值取 `BoardState.fatigue`）。
  * 洗牌 = `kardsmem.rng.Stream.shuffle`（正向 Fisher-Yates），`ShuffleDeckBySide` 用它复算；
    没有种子 ⇒ 之后牌序标记为未知（不按旧牌序假装能抽）。
  * 收缴（`SalvageMultipleUnits`）= 手牌未满时进一张 1/1、费用 min(费用,3) 的复制（保留类型/关键词）。
  * 协力（Bond）= `HasBond ∧ faction ∉ activeBondFactions` ⇒ 打出时吃一次疲劳伤害；
    `activeBondFactions` = 我方回合开始时场上单位（非未揭示隐蔽）的国家集合，按 `st.turn` 缓存快照。
  * 改牌库的动词（`StealCardFromBoardToDeck` / `SpawnCardInDeckBySide`）直接改模拟牌库，
    并**复刻 BP 循环里的随机消耗**（`SpawnCardInDeckBySide` 每张无条件抽一次 RandomIntFromRangeWithStream）。
- 抽到"哪张"的来源：`kardsmem.gs.deck_ids`（保序 ⇒ 牌库顶）；读不到才退回匿名牌 `draw_v`。
- **友方反制**（在敌方回合触发的那些）是例外：不在"本回合盘外再算一遍"的范围内，单独处理。

#### 3g-3. 牌库族落地（DeepSeek 2026-10-02）

##### 触发链接线（DeepSeek 2026-10-02 下午）——**第一版已接，深化交给 Claude/subagent（按 IDA）**

已接进 eval 的触发链（`_hand_eff` 里合并进 `unit_eff`，`eff_src` 带标记）：

| 触发 | 钩子 | 标记 | 覆盖 | 提交 |
|---|---|---|---|---|
| 情报 | 0x1C `OnIntelTriggered`（cipher>0 或 `SetCardsSeenByCipher`） | `+intel` | LEGION/THUNDER DIVISION/NAKAJIMA/7th SCOTTISH（含伤害兜底）/Inniskilling/16th Tarnow | `baf7978` `7d65b1d` |
| 牌库变化 | 0x3 `OnAfterDeckChanged` | `+deckchg` | LOVAT SCOUTS / RM ROMA / BETASOM | `f98ef07` |
| 洗牌 | 0x16 `OnDeckShuffled`（仅 skipSubAction=true） | `+shuffled` | **SABAE REGIMENT**（1 伤×场上单位数兜底） | `ad3ea43` |
| 打出手牌 | `OnOtherCardEnterPlay`+反制族（`triggers.run_play_hooks`） | `+hooks` | **5th SASEBO SNLF**（Navy +1/+1）等；敌方反制暂不算 | `a296d12` |
| 死亡 | 0x27 `OnDestroyed`（`death_fx` 预计算） | —（boardeval 内结算） | **5th SASEBO 的"洗牌+抽 1"** 等 78 张覆写卡的自身效果 | `9783d87` |

**留给 Claude/subagent 深化的部分**（按 IDA 为准）：
1. **死亡链只做了"自己的 `OnDestroyed`"**：旁观者 `OnOtherCardDestroyed`（69 张覆写）、
   `OnBeforeDestroyed`/`OnLeaveBoardOrOwner`/`OnCardLocationMoved`/`OnAfterDestroyed` 的完整
   序列（`triggers._Chain.destroy_combat` 已实现，但没接进 boardeval 的死亡路径）；
   `death_fx` 现在给 `killer` 传 0（"被谁杀"语义丢失）。
2. **`attack_fx` 与 `death_fx` 的去重**：若把 `triggers.to_fx` 的摧毁 bucket 接进 `sim_attack`，
   会和 `death_fx` 的 `OnDestroyed` 重复结算，需要合并策略。
3. triggers.py 里已标注未实现：0x18 摧毁效果倍增（4 张卡）、`hasSalvage` 打捞。
4. 实机验证：以上标记（`+intel`/`+deckchg`/`+shuffled`/`+hooks`）还没在实战日志里出现过，
   需要一局里凑出对应组合（服务器进程 2026-10-02 下午已验证 reload 生效）。

- **实机验证环境（2026-10-02 下午，游戏已开）**：
  * 本机现在跑的是 **Xsolla launcher 渠道**（`D:\Program Files\KARDS - The WWII Card Game\...`，
    image 0x9CC4000 / md5 7c6a83c7…）⇒ `python -m kardsmem …` 必须带
    `KARDS_BUILD=launcher_default`，否则 `attach()` 按 Steam 表校验直接拒（BuildMismatch）。
  * 大厅/设置页时读到的是"空盘面"：`deck_ids` 空、无手牌、`card_functions` 是 seed=0 的空壳实例
    —— 属正常，不是读取坏了；对局数据要进棋盘才有。
  * `screen.py` 修了两个 bug（`SRC` 未定义、config/templates 路径）。
  * **抓图新招**：UE 窗口被遮挡时，`PrintWindow` 用 `PW_CLIENTONLY|PW_RENDERFULLCONTENT (0x3)`
    能拿到真实画面（原来只用 0x1 ⇒ 全白 ⇒ 退化成屏幕抓取 ⇒ 抓到盖在上面的别的窗口）。
    验证脚本见 `_nn_scratch/verify_deck_bond.py`（只读）。
- **实机验证结果（2026-10-02 14:14–14:23，整局自动打完）**：✅
  * 牌库：`_deck_state` 实机返回 34 张 id + 34 张模板（TASK FORCE 44 / OVERCAST / WAR BONDS …），
    保序、id→模板对得上（`verify_rule_deck.json`）。
  * tag 读取：`read_gameplay_tags` 实测读到 `ability.forecast`、`subtype.navy` ⇒ bond 的判据路径可用
    （本局牌组无 bond/salvage 卡，`scan_special_cards.json` 三项全空，**bond/salvage/spawn-deck/情报
    的实机验证待换牌组**）。
  * 随机流：`card_functions` 活实例 seed=1273592647（非空壳）。
  * 整局 `play_only.py` 接手：111 步、T28 致命一击（+960）打完，无崩溃；预报 9 路径评估在跑
    （日志 `forecast_eval`）。已知 VM 缺口照旧（TASK FORCE 44 / USS YORKTOWN / OVERCAST 等）。
  * 踩坑：常驻监听器里的 frida script 会失效（`script has been destroyed`）⇒ 必须**正常重启
    监听器**（写 `quit` 等它自己退，再 Start-Process 起新的）并带 `KARDS_BUILD=launcher_default`；
    重启后 precheck.available()=True 才继续。多出来的 `kards.exe`（启动器，SizeOfImage 0x3F000）
    会让构建检查报"未知"，属正常。

- 提交 `5cca574`（牌库模拟：真实牌序/疲劳/洗牌）、`6addf83`（收缴+协力）、
  `5aec954`（动员/压制回合数/洗入牌库/塞牌）。规则自检：`policy.boardeval` / `player.rule` /
  `semantics.effectvm` 全过 + 10 个 `test_*.py` 全过。
- 实机待验证：bond 卡的 `ability.bond` tag 读取、salvage 卡的 `salvage_ids` 实参形态
  （VM 里 TArray 为 list）、`SpawnCardInDeckBySide` 的位置参数与种子推进。
- **剩余动词也补完（`6f61c5d` + 本笔）**：`MoveUnitFromSupportToFrontLine`（推上前线）、
  `RevealCard`（去 covert）、`AddDefenseToMultipleCards`（按 id 数组逐张 ±防，负数击毁）、
  `AddKreditsTax`（`KreditsTax_AsEnemyTarget`，按 side 计价值）、
  `Add/RemoveGameplayRestriction`（影子列表；类型表 0..5 从 BP 调用点反推；
  `gen_actions` 对我方 cannotPlayOrders(2)/cannotDeployUnits(3)/cannotAttackWithGroundUnits(4) 生效）。
  **`AddIntelToCard` 暂时空实现**（加进 `effectvm.IGNORE_VERBS` 拦下不让 VM 执行）——
  ★ 待定项：BP 自身只把 `cipher` clamp(0..9)、没有钩子调用，但**情报牌使用时可以有实际作用**
  （用户 2026-10-02 例："日波情报流，使用情报牌时 …"），那些效果在情报相关 triggers 钩子族里。
  等钩子侧（subagent）接好后，这里要从"拦下"改成"记录 cipher 变化 + 交给钩子"。
- ⏳ **`RevealCard` 的钩子链还没跑**（只落了状态变化）：BP 在 `isRevealed=true; hasCovert=false`
  之后还会 —— 目标未被压制时 `OnCardRevealed()`、`FetchAllCardsWithEventTrigger(0x37)` 逐个
  `OnOtherCardRevealed(cardRevealed)`、`OnEnterPlay(0x4)`、`FetchAllCardsWithEventTrigger(0x2B)`
  逐个 `OnOtherCardEnterPlay(cardRevealed, 0x4)`。归 triggers 钩子族，
  由 subagent 处理（撞限额，2026-10-02 16:11 自动恢复）。

### 3h. 已落地（DeepSeek 2026-10-01 夜，按 `PLAN.md` 阶段 0 任务 2）

- ✅ **两条拐棍已删除**：`agent/nn.py` 里 `_park_cursor_outside()` 与 `_cursor_to_target()` 的**函数体 + 调用点**
  以及 `KARDS_PARK_CURSOR` / `KARDS_ATTACK_CURSOR_ON_TARGET` 两个开关全部移除（只留一行注释说明为什么不再用真鼠标）；
  `cursor_in_window` 日志字段**保留**（识别"用户介入"）。
  依据：`_nn_scratch/attack_test_report_nopark.md` —— 拐棍全关 + 后台 + 光标窗外 **16/16 成功**。
  离线回归：`import ops.inject,agent.nn,player.rule` ✅、`kardsmem selftest`（默认 + launcher_default）✅、`test_choose_verbs.py` ✅。
  ✅ **实机回归已过**（2026-10-01 夜两局：6/6、8/8，全部 `cursor_in_window=false`，成功率 100%）——
  PLAN 阶段 0 验收达标；累计 **30/30**（上一局 nopark 16/16 也在内）。
- 下一步按 `PLAN.md` 顺序：阶段 1（随机流，见 §3i）→ 阶段 2（延迟）→ 阶段 3 → 阶段 4/5；阶段 6 离线可并行。

### 3b⁸. 攻击修复实机验证（DeepSeek 2026-10-01，★完全不动真鼠标）

三局累计 **攻击 30 次成功 30 次（100%）**，全部在光标窗口外 + 游戏后台：

- 16/16 —— 拐棍全关的第一局（`_nn_scratch/attack_test_report_nopark.md`）。
- 6/6 —— 本夜第 1 局（`_nn_scratch/attack_test_report_run2.md`，22:23–22:29）。
- 8/8 —— 本夜第 2 局（`_nn_scratch/rng_test_report.md` §3，22:33–22:43）。

历史对照：修复前同一场景 0~20%；唯一两次失败发生在**用户介入**（`cursor_in_window=true`）时。
写入点：`ops/inject.py::_attack_once` 提交批次 `cursor={card_under_cursor, location_enum=7, row=1}`。

### 3i. 随机流实机校验（DeepSeek 2026-10-01 夜，外部只读采样）——✅ 种子拿到了

- **踩坑（重要）**：`Logic → spawnCardFunctions(*Out)` 返回的是**空壳实例**（`InitialSeed=0`、seed 恒 0）。
  同一时刻进程里有 **两个** `BP_CardFunctions_C`：活的那个 `InitialSeed=1272165493`（match_id 播种）、
  seed 非 0；空壳的 `InitialSeed=0`。**判活判据 = 关卡 actor 里 `InitialSeed != 0`**（`Locator(m,base).actors()`，
  156 个 actor，读一次 0.00 s；别用 `spawnCardFunctions`，也别扫全量 GObjects）。
- 采样方式：**外部只读**（`kardsmem` ReadProcessMemory，20 Hz，`_nn_scratch/ext_sample.py`），
  不碰 frida、不抢 RPC（注入侧后台线程采样会把 script 抢崩 —— 这是上一版的教训）。
- 对账结果（本夜第 2 局，6759 条采样）：从 `InitialSeed=1272165493` 起，种子位置 74 → 105 → 127 → 142 → 156，
  **4 次变化全部是 LCG 上的推进**（31/22/15/14 步；15 = 3×(3+2)，与 `RandomIntFromRangeWithStream` 包装吻合）。
  ⇒ 整场就是一条从未重置的 LCG 序列，`bUseTurnSwitchValidation` 在本局**是关闭的**（重播种没发生）。
- 3 选 1 的 `pick` 本身**不消费**随机数（窗口内种子不变）—— 预报的候选是在**面板弹出时**就已经抽好的，
  预测必须在打开面板**之前**算。
- 工具：`_nn_scratch/ext_sample.py`（采样）、`rng_head.py`（会话内开局+定位）、`rng_report.py`（对账）、
  `pl_run2.py`（编排）。报告：`_nn_scratch/rng_test_report.md`。

#### 3i-2. 预报预测：实机全量验证通过（当晚追加）——✅ 阶段 4 的"预测"这一半可以动了

- **活实例判据要再加一条**：上一局的实例**换局后不会立刻销毁**，它也有非 0 的 `InitialSeed` ⇒
  只看"非 0"会锁到已经冻住的旧流（症状：seed 一直不变，22:55 那局就是这么废掉的）。
  最终判据 = 关卡 actor → 播种过 → **`InternalIndex(+0xC)` 最大**（game 每局新建实例，索引单调递增；
  实测当次 149360 vs 上一局残留 128630），缓存 5 s 复核。代码：`kardsmem/rng.py::card_functions_ptr`。
- **用户手动局全量验证**（`_nn_scratch/forecast_validation_report.md` + `forecast_manual_rain_20261001.png`）：
  打 TASK FORCE 44 前读种子 `4196626795` → 预测 rain 分支 = 暴雨/骤雨/季风雨（抽取 28）→
  面板实际显示 **逐张、同序、连效果文本都一致**；面板出现后种子 `2626744231` = **正好 28 步**。
  ⇒ **三档 + UI 顺序 + 抽取数** 三件全部坐实（此前只验到 light 档）。
- ★ **别按名字比对**（用户点破）：同一(天气×花费)有 **3 个同名、效果完全不同**的变体
  （DELUGE = 随机分6防 / 全体敌方-1攻 / 抑制+抽牌）。预测返回的是**具体变体指针**没问题，
  但下游评估必须按变体算效果。核对法 = BP 导出的 `FText Text` + locres 中文，逐张比屏幕文本。
- 工具：`kardsmem/forecast_pool.py`（池 + 预测，`--live/--seed/--selftest`）、
  `_nn_scratch/forecast_validator.py`（在线校验：对象数组差分找候选 actor，不能用 4 s 的
  `pick.choose_candidates()` 轮询——面板只开 ~2 s）、`forecast_backtest.py`（事后回测）。
- **下一步（阶段 4 的剩余部分）**：把 9 条路径接进 `rule.choose_pick`（每条 × 当前场面估值，
  取代"点第一个"），顺带消掉预报空轮询 ≈50 s。

#### 3i-3. `keepOrder=false` 三选一（好人寥寥那类）：**预测模型已解出**（2026-10-02 凌晨）

- 机制（BP 导出已核对）：`GetChooseSpawnCards` 给出**整个池** → 原生
  `Array_ShuffleFromStream(池, cardsRandomStream)`（正向 Fisher-Yates、**N 次抽取 = 池大小**）→
  取**前 3 张**（顺序即屏幕顺序）。
- ★ **池 = 游戏官方 API 当前卡表（= 没"进预备"的那批）∩ 该卡自己的过滤 ∩ 静态卡表卡集过滤**。
  "进预备"是轮换的 ⇒ **必须实时问游戏 API**（`herokuapi.kards.com/graphql` 的 `cards` 默认只返回未进预备的卡），
  别用任何离线卡库快照。好人寥寥本账号当前池 = **18 张**。
- 证据：两次实机出牌各跳 **正好 18 步**（20 Hz 种子记录），且用这 18 张算出的洗牌结果与两次面板
  **6 个位置约束逐个对上**（`_nn_scratch/choose_spawn_predict.py --selftest` 2/2 PASS）。
  早期错在只按静态卡表筛（32 张）—— 少了"预备"这一步。
- 工具：`_nn_scratch/kards_api_reserved.py`（实时拉合法卡）、`choose_spawn_predict.py`（预测 + 自检）、
  `watch_choose_spawn.py`（20 Hz 种子 + 面板记录，用来抓现场）；文档 `_nn_scratch/choose_spawn_findings.md`。
- ✅ **已接进 `rule.choose_pick` 留痕**（2026-10-02）：面板出现时用"**回退 N 步**"的种子复算洗牌，
  与屏幕三张逐张比 → `meta.shuffle_pick_check = {pool, pre_seed, pred, actual, hit, ok}`。
  只读：静态卡表 + 官方 API 的**本地缓存**（对局里不发网络请求；预备名单轮换时跑
  `python _nn_scratch\kards_api_reserved.py --refresh` 刷新）。
  ★ **9 选 1 评估仅限预报**（用户 2026-10-02）：洗牌型只留痕、不评估。

#### 3i-4. 三选一**评估**（2026-10-02 凌晨）——✅ 已落地，但天气牌效果仍是缺口

- **为什么要分两种做法（用户 2026-10-02 讲清的两条）**：
  ① 所有连续三选一里，**只有预报的第一层会改变第二层的候选**（选天气类型 ⇒ 决定那三张变体）；
  ② **也只有预报的连续三选一不是同一张卡触发的**（第一层是天气类型卡，第二层是它抽出来的天气牌）。
  ⇒ 其余连续三选一彼此独立 ⇒ 各自评三张即可；只有预报要做"3 类型 × 3 变体 = 9 条路径"的前瞻。
- **实现**：`score_candidates()` 把候选当**临时手牌**塞进 `boardeval` 模拟（单位 `sim_deploy`、
  指令 `sim_order`；带目标的指令**逐对目标**枚举取最大 delta），选分数最高；
  预报第一层走 `fc_best_type()`（每条路径取三个变体的最优值）。日志：
  `meta.pick_eval` / `meta.forecast_eval`（+ `pick_eval_src=boardeval|forecast_9path`）。
- ★ **已知缺口（如实记）**：天气预报牌那几张 `_hand_eff()` 取不到效果
  （`eff_src=<卡名>: "gap"`，`gaps` 记 "VM/静态都没取到效果"），只落到常量兜底 `_est`
  ⇒ 同费变体同分、时间类型选择退化成稳定 tie-break。缺口已写进 `meta.forecast_eval_gaps`；
  真正的修法是补 VM 原语（见 §1 / §3d 的缺口清单）。

#### 3i-5. VM 缺口逐层补（2026-10-02 凌晨）

按"跑一遍 → 看停在哪个函数 → 补它"的顺序推进，`semantics/effectvm.py::make_read_hooks` 新增：
- `GetAllUnitsOnBoard(bool includeCovertCards, &cards)` / `GetCardsOnBoardBySide(side, unitsOnly, includeCovertCards, &cards)`
  —— 按快照喂（含隐蔽过滤：**隐蔽看 `keywords`，不是 `is_revealed`** —— 实测全场 `is_revealed=False`，
  它表示"本局还没被翻出来"，不是隐蔽）。
- `GetOppositeSide(card)` —— 字节码常拿它推"敌方"（例：rain2_deluge2 = 全体敌方 -1 攻）；
  我们评估的是静态卡对象（`side_enum=0`）⇒ 兜底：先按快照查该卡座位，查不到按**我方座位**算。
- `GetCardsInHandBySide` / `...Ordered` —— 手牌查询。
效果：`rain2_deluge2` 从"啥都记不到"变成 `{"buff": [-3, 0]}`（对面 3 个单位 × -1 攻 ✓），
9 路评估里 **2K 档已经按效果算**（-0.72，因为减攻值不回 2 费）。

**剩余缺口（3 类，按可行动性排序）**：
1. **AOE/集合目标语义没建模**（5 张：`rain2_deluge2`/`rain3_torrential_rain2`/`storm3_tropical_storm2`/
   `sunny3_jungle_fever2`/`OVERCAST`）：VM 把"对 N 张牌各改一次"聚合成一个数量（`buff:[-3,0]`），
   丢了"作用在谁身上"，而 `_hand_eff` 的保守闸门又会把它当"需要目标却没目标"丢掉 ⇒ 记成 gap。
   要补的是 `boardeval` 的效果 schema（例如 `side_hits`/`apply_to: enemy_units`）。
2. **"没有打出/部署钩子覆写" 3 张**（BIG RED ONE / 162nd REGIMENT / COUNTER STRIKE）——
   价值本来就在攻防/关键词上，不算真缺口（评估只少了它的部署效果）。
3. **两张 6K 跑了但没记到东西**（`rain4_monsoon_rain3`、`sunny4_scorching_sun3`）——待查。
   `THE AMERICAN GUARD` / `storm4_cyclone3` 则停在更深一层（字段读不到 / 0x4F）。

#### 3i-6. C/D 两连局与"跨局缓存"事故（2026-10-02 凌晨；修复已落地，离线回归过，**待实机**）

**两局结果**（编排：`_nn_scratch/cd_play_shutdown.py`，01:26:40 关游戏 + 关机；UECC 无新目录）：

| 局 | 日志 | 结果 |
|---|---|---|
| C | `rule-live-20261002-011021.jsonl` | ✅ 有效：68 步 / 11 个动作**全部执行成功**（含换牌 `mulligan exec=True`，`t_exec=12.5 s`）；我方回合中位 17.4 s / 最长 25.2 s；攻击 0（无攻击候选） |
| D | `rule-live-20261002-011415.jsonl` | ❌ **不算**：全程 0 出牌 —— 316 步里只有 13 次 `end` 成功，其余 87 个动作全被拒，错误清一色 `card X 不在我方手牌`（目标型停在 phase1）；换牌被闸门拒（`['友方回合']`）；我方回合中位 20.4 s / 最长 29.3 s（cd_play 报的 3 次 >45 s 全是**对方回合** 48.2/47.5/54.9 s） |

**根因：跨局缓存三连**（live_session 常驻 ⇒ C/D 共用同一个 session / MatchLog 对象）：

1. `kardsmem/cards.py::_my_seat` 把座位缓存在 **session 对象**（`session._my_seat_cache`）上，写一次永不失效 ⇒ D 局沿用 C 局的座位；座位不符时我方手牌整批被判 `enemy`。规则侧 kardsmem.board 走回合奇偶那条读法正常 ⇒ 两边视图分叉（规则看得见 9 张手牌、注入侧一张都找不到）。
2. `kardsmem/matchlog.py::forget()` 漏清 `_my_side_cache`（只清了 `_logic_cache`）。
3. `_logic()` / `my_side()` 命中缓存前不验新鲜度 ⇒ 长寿命 MatchLog（`AgentSession.log`、`kardsmem.board._logic_matchlog` 兜底、`Injector._ml`）跨局继续用上一局的 Logic 指针/座位。

**修法（已完成；未碰 `op_inject`）**：

- `kardsmem/cards.py::_my_seat`：座位缓存跟随"这一局的 MatchLog"（每 session 一个 `_matchlog_for_seat`；`my_side()` 命中前先 `is_stale()`，换局自动 forget+重定位），不再挂在 session 上。
- `kardsmem/matchlog.py`：`forget()` 清 `_my_side_cache`；`my_side()/_logic()` 命中缓存前先 `is_stale()`；`locate()` 发现缓存对象失效就丢弃重找；`is_stale()` 改用 `_count_raw()`（避免 `locate↔is_stale` 自递归）。
- `agent/session.py::legality()`：`Legality` 按座位构造（`my_seat=`），现在**座位变了就重建**（读不出 None 时不动缓存，避免换牌窗口抖动）。
- 离线回执：新增 `test_seat_cache.py`（**22 项全过**，含"同局内不误伤缓存、换局必须重读、`forget` 两清、`pin` 换对象两清、`locate` 不把旧对象当结论"）；`python -m kardsmem selftest` 两构建 PASS；`test_choose_verbs.py` PASS；`import ops.inject, agent.nn, player.rule` ✅。
- **待实机验证**（下一局）：① 换局后 0 个 `card X 不在我方手牌`；② 换牌 `exec=True`；③ 开一局时把 `Logic.mySide` / 缓存值 / `deckForEnemy` 三个读数写进日志对照（真出问题能一眼看出是哪条链错）。

#### 3i-7. 评估修复：不再编造效果价值 + 文本型抉择按选项估值（2026-10-02 上午，用户点名）

用户两连报（排位局 `rule-live-20261002-111957/112934` 实况）：
① "指令的评估有问题。6费空打+3+2"；"为什么选 6K 骄阳（友方空军行动花费为 0 且攻击时对敌方总部造成
等同攻击力的伤害），明明友方手牌中和场上都没空军"；② "游骑兵，脚本还是不选 +4+4"。

**根因（都在 `player/rule.py`）**：

- `_hand_eff()` 对**取不到效果摘要**的指令给 `_est = order_mult × cost` 的"保守估值"——它是编造的：
  6 费 = 5.4，扣掉手牌持有价值 0.36×6 = 2.16 ⇒ 净 **+3.24** ⇒ "打了什么都不做"的牌被当成正收益。
  实机证据：9 路预报评估里 `card_event_sunny4_scorching_sun*` **恒 +3.24**（连选 4 次），
  搜索里 `SCORCHING SUN` 6K 也被同一条公式推着出（+3.24 / +3.74 / +5.65）。
- 部署抉择（5th RANGERS：**"Set operation cost to 0." vs "+4+4"**）的候选**不是卡**（label 是选项文字），
  `score_candidates()` 查不到 ⇒ 旧启发式对它们全是 0 分 ⇒ **永远点第一个**（行动花费 0）。

**修法**：

- 新增 `PARAMS["unknown_order_est"] = 0.0`：取不到效果摘要（VM/静态都没跑出来，或"范围/随机目标"没建模）
  的指令**不再编造价值**；打出去只剩"白丢一张手牌"的负收益 ⇒ 不会被选中。
  （旧贪心兜底 `_play_cands` 未动——它只在搜索内部异常时才走；真正的修法是补效果覆盖，见 §3i-4/§3j。）
- 新增 `RuleV2._text_option_score()`：文本型二选一按**选项文字**对触发单位估值 ——
  "+4+4" → 身体价值 4.2（本回合还能动再 +1.0 行动价值）；"operation cost … 0" → 行动费×0.6；
  估值写进 `meta.pick_eval`（`pick_eval_src="label"`）。认不出的选项返回 None（不猜）。

**回执（离线全过 + 热重载进对局）**：

- `python -m player.rule` **0 失败**（新增断言：未知指令不被当正收益 / 6 费未知指令不压过能打的攻击 /
  "+4+4 压过行动费归零"5.2 vs 2.4 / `meta.pick_eval_src="label"`；旧"增益指令"用例改用**有量**的效果摘要）。
- 只读实机核对（当前对局快照 + static-only）：三个天气变体 `sunny2_heatwave2 / sunny3_jungle_fever3 /
  sunny4_scorching_sun3` = **-0.72 / -1.44 / -2.16**（旧值 -0.72 / **+2.16** / **+3.24**）。
- `test_choose_verbs.py`、`python -m kardsmem selftest`（默认 + launcher_default）全 PASS；
- 11:34:05 **热重载成功**（`_nn_scratch/hot_reload.log`），修复已进当前对局。
- 待观察：后续预报 9 路 / 部署抉择的 `meta.pick_eval`（应不再出现 6K 恒 +3.24；5th RANGERS 选 +4+4）。

**更正（用户 2026-10-02："取不到效果摘要 是不可能的。本会话先前还查过这个问题。"）**：

- 只读复跑证实（`_nn_scratch/probe_effect_gaps.py`）：`card_unit_5th_rangers` 的
  `enumerate_effects` = `choice=True, outcomes=[{"opcost": 0}, {"buff": [4, 4]}]`
  （**顺序 = 蓝图分支顺序 = 屏幕 chooseOneIndex**）⇒ 二选一的分支效果**本来就是可得的**，
  不该只靠选项文字猜。现在 `_option_score()` **首选 VM 分支**（实机只读读数：2.4 vs 4.2 ⇒ +4/+4 胜），
  文字匹配只作 VM 不可用时的兜底；`meta.pick_eval_src` 如实写 `vm` / `label`。
- 6K 骄阳 `card_event_sunny4_scorching_sun3`：`complete=True、效果 {}` —— 这**不是缺口**，
  是 VM 跑完了、当前盘面没有空军（正是用户说的"明明没有空军"）⇒ 现在按**已知的空**处理
  （`eff_src="vm(空)"`，不记 gap、不编造价值）。⚠ 补记：给它加一个合成空军再跑仍是 `{}`，
  说明这张牌的"免费行动+攻总部"记录路径还没覆盖到（属 §3i-4 的覆盖工作，另记）。
- 用户第二点（"对攻防/行动花费的评估考虑的因素太少"）：选项估值不再用"费用+身材"的旧启发式，
  改为**按效果折算**（buff[4,4] → 0.6×4+0.45×4=4.2，本回合能行动再 +0.25×4；`opcost:0` →
  行动费×0.6），并且只有可行动/需要该行动时才兑现对应价值。
- 11:44:27 **第二次热重载成功**（`player.rule` 并回 86 个类属性）。

#### 3i-8. Trigger 全覆盖（按 `CARD-PLAY-HOOKS-1.60.md` 的注册表；2026-10-02 下午）

用户 2026-10-02："我们有一个逆向过所有未覆盖Trigger的报告" —— 即
`reverse-data/reports/report/CARD-PLAY-HOOKS-1.60.md`（§2 是完整的 **trigger id → hook → dispatcher**
表，60+ 项；§6 是 `AttackCard` 的精确顺序）。我们此前只覆盖了 0x2B 进场族 + 反制三兄弟。

- ✅ **攻击族已落地**（`semantics/triggers.py`，离线自检通过）：`ATTACK_HOOKS` +
  `run_attack_hooks()`，顺序按报告 §6：攻击者自己 `OnBeforeAttack` → 0x0D `OnBeforeOtherCardAttacks`
  → 攻击者自己 `OnAfterAttack` → **0x04 `OnAfterOtherCardAttacks(defenderCard, attackerCard,
  damageToDefender, attackCost)`**（实参名来自 1.60 导出蓝图签名；6K 骄阳"空军攻击额外打总部"
  就写在这个钩子里）。`find_cards` 按报告 §0.2 不做位置/阵营过滤（钩子自己 gate）。
- ⏳ **下一步（eval 接线）**：对"将要落场的手牌"（如 6K 骄阳），用场上的空军当探针跑一次 0x04，
  把"每次攻击的额外效果"折算成 sim 的攻击后效（束搜索里"先打天气牌 → 空军打脸"的组合就可见了）；
  没有空军时探针拿不到效果 ⇒ 卡片价值 0（正确）。
- ⏳ 其余未覆盖族（按表挑与我们对局有关的）：0x1F `OnOtherCardAttacks`（可 stop）/ 0x1E 换目标 /
  0x24-0x26 伤害修正 / 0x34 受击 / 0x27 死亡 / 0x19 回合结束 / 0x40 回合开始 …
- ⚠ 工具链事故（同一天）：11:44 热重载后，11:48 开下一局时 `RuleV2.__init__` 的零参 `super()` 炸
  （`TypeError: super(type, obj) …`），连锁停止。**热重载后要重启监听器再开新局**（CLAUDE.md #39）。

