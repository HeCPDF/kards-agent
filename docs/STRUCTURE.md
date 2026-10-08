# 仓库结构与依赖方向

> 2026-10-03 整理。之前 `agent/` 是个大杂烩（效果分析、玩家、命令层、MCP 全在一起），顶层还散着十几个脚本和 63 个测试。
> 现在每个包只干一件事，**依赖只许向下**，由 `tests/test_arch_rules.py` 强制（`LAYERS` 表）。
> 想看实际依赖：`python tools/depgraph.py`（加 `--dot` 出 Graphviz）。

## 分层

```
            ┌───────────── 入口 ─────────────┐
 L5         │ gui/   interfaces/   tools/    │   面板 / shell+MCP / 一次性脚本
            └───────────────┬────────────────┘
 L4                    player/                     出牌的玩家：规则策略、在线回路、录制、热重载、开局闸门
                           │
 L3          agent/                  learn/        命令层（一组动词）        离线学习（torch，可选）
                │                      │
 L2   ops/   semantics/   sim → evaluation → policy
        │         │              │
 L1          kardsmem/                              读侧：进程内存 / UE 反射 / Kismet VM / 盘面快照 / RVA 扫描
                │
 L0        base/        vendor/                     路径 / 第三方原语（窗口、截图、模板匹配）
```

## 每个包是什么

| 包 | 一句话 | 依赖 |
|---|---|---|
| `base/` | 运行期路径（`paths`）与仓库定位（`workspace`）。**没有任何内部依赖** | — |
| `vendor/` | 上游 OCR-Kards-Auto 搬来的窗口 / 鼠标 / 模板原语（GPL-3.0，见 NOTICE） | — |
| `kardsmem/` | **读侧**。读游戏进程内存；UE 反射链；Kismet 字节码反汇编与外部 VM；盘面快照（`board.py` 的 `BoardState`）；版本识别（`version.py`）、RVA 扫描（`rvascan.py`） | base, vendor |
| `engine/` | **无头游戏**（`REFACTOR-PLAN.md` P2+）：`state.py`（U/H/Sim/State+常量，单一来源）、`dispatch.py`（事件队列/分发器）、`chain.py`（抽牌链/autoplay）、`adapter.py`（快照→`Sim`，`from_cards`）、`triggers.py`（触发分发/钩子顺序，自 semantics 端口）、`effectvm.py`（录制 VM，自 semantics 端口；P4 起降级为记录模式；P6 拆出 `effectvm_tables.py` 词汇表 + `effectvm_selftest.py` 离线自检，`effectvm` 再导出）、`scripts.py`（**直跑**：`native_hooks` 装配 + `DEFAULT_SINKS` + 对账 `reconcile_*`，并再导出下列件；P4）、`scripts_ctx.py`（`DirectCtx`/`write_out`）、`scripts_sinks_core.py` / `scripts_sinks_more.py`（各动词族 sink 表，P6 自 scripts.py 拆出）、`calls.py`（类型化调用重放 `apply_calls`；P5）、`natives/`（原版原生移植：abilities/board/bond/cards/damage/kredits/stats/status，一个族一个文件、逐条出处注释）；之后在 `GameState` 副本上直接跑卡牌蓝图字节码。`sim/*` 正在逐个变成 re-export 薄壳 | base, kardsmem；P6：`triggers.py` 另拆 `triggers_specs.py`（钩子规格表/常量）+ `triggers_families.py`（钩子族 `run_*`/`*_effects`，加载后写回 `engine.triggers`，旧 import 路径不变；热重载顺序 specs→triggers→families） |
| `ops/` | **写侧**。往游戏进程里注入 JS，合成鼠标事件序列（悬停→按下→拖动→松开，信息流与真人等价）。`ops/inject.py` 是门面（执行器按职责拆在 `ops/{conn,world,gesture,query,play,choices,flow,cli}.py`，见下表），`agent.js.tpl` 是注入的 JS | base, vendor, kardsmem |
| `semantics/` | **牌是什么意思**。把游戏自己的蓝图字节码在外部 VM 里空跑：`legality`（动作合法性）、`cardprobe`、`choosespawn`（三选一）、`forecast`（预报预测）、`shadow`（P5 影子对账用的 engine 件再导出：`apply_calls` / 状态快照；`player` 不许直接 import `engine`，经这里转）；`effectvm`/`triggers` 已端口进 `engine/`（P2），这里是**同一模块对象**的兼容别名 | base, kardsmem, engine |
| `sim/` | 盘面模拟（单位、结算、抽牌链、提示/挂起）。真实现正逐刀端口进 `engine/`——`state`/`dispatch`/`chain`/`adapter` 现在**只是 re-export 壳**；`engine.py`（规则主体）留到 P4/P5 再搬。**纯逻辑**，规则照原版移植（`docs/SIM-FIDELITY.md`） | base, kardsmem（只用 RNG）, engine |
| `evaluation/` | 盘面价值（只依赖 `sim` 的数据结构） | sim |
| `policy/` | 搜索与应答：候选展开、`answer` / `forced` / `plan`；`boardeval.py` 是对 sim/evaluation/policy 的兼容门面 | sim, evaluation, kardsmem, base |
| `agent/` | **命令层**：`AgentSession` 把读侧、写侧、合法性收成**一组动词**（出牌/攻击/选择…），加上盘面编号与渲染（`view`）、预检（`precheck`）、统一结果类型 | base, vendor, kardsmem, ops, semantics |
| `learn/` | 离线学习：特征、模型、训练（torch，独立 venv，可选）。主环境不需要 | base |
| `player/` | **玩家**。`rule`（规则策略 V2，决策）、`loop`（在线回路：每步 观察→决策→执行→回读）、`play`（把二者接起来的入口）、`record`（只读录制）、`hotreload`、`play_guard`（对局进行中绝不点开始） | agent, policy, sim, evaluation, semantics, ops, kardsmem, learn |
| `interfaces/` | 给人 / LLM 用的入口：交互 `shell`、`mcp` server。只做适配 | agent |
| `gui/` | 控制面板（tkinter）：开始/停止、状态、日志、检测更新；通过 `control.json` 与常驻监听器通信 | agent, player, kardsmem, base |
| `tools/` | 一次性脚本（标定、探针、内存 dump、常驻监听器 `live_session.py`）。可依赖任何包，**不被任何包依赖** | 任意 |
| `tests/` | 离线测试（`python tests/run_all.py`）；不需要游戏 | 任意 |

## `ops/` 模块表（2026-10-03，D1：`inject.py` 6400 行按职责拆开，行为逐字不变）

`ops.inject` 现在只是**兼容门面**：`class Injector(ConnMixin, WorldMixin, GestureMixin, QueryMixin, PlayMixin, ChoiceMixin, FlowMixin, SelftestMixin)`，
并 re-export 旧的全部模块级名字（`from ops.inject import Injector, JS, SETTLE_HOVER, NOTE_OFF_*, sweep_frida_tmp, …` 照旧可用）。
方法体原样搬家，只在 `ops/` 内部互相依赖（再往下只依赖 base / kardsmem）。

| 模块 | 内容 | 主要方法 / 名字 |
|---|---|---|
| `inject.py` | 门面 + `Injector` 组合 + `python -m ops.inject` 入口 | — |
| `consts.py` | 备注偏移 `NOTE_OFF_*`、`LOC_*`、`SETTLE_*`（拖拽各步停顿）、注入的 `JS` | 常量 |
| `support.py` | 灰态开关 `play_gray_guard_on`、frida 临时目录（`sweep_frida_tmp`）、跨脚本缓存、`_action_has_card` | 模块级函数 |
| `conn.py` `ConnMixin` | Frida 管道、游戏线程调度、`settle`、反射缓存（类/函数/偏移）、字段读写、原始调用 | `__init__ api close settle fn off find_fn call_* poke peek write_and_call call_raw_* instances_of_class scan_classes _bp_lib_fn card_title` |
| `world.py` `WorldMixin` | 座位与单例发现（HUD/棋盘/逻辑/牌库）、对局级缓存失效、动作流与提示 | `my_side hud_actor board_actor logic_actor deck_actor matchlog notify_texts _receipt find_card world_levels level_actors` |
| `gesture.py` `GestureMixin` | 手势原语：拖拽生命周期、点击、悬停、目标箭头、屏幕坐标 | `drag_release click_actor pc_drag_* hover_* aim_arrow remove_target_arrow arrow_* board_card_screen_pos click_board_card` |
| `query.py` `QueryMixin` | 合法性与只读查询（向游戏本体问 `Can*`）、空隙/落点换算、关键词与显示值、目标规格 | `game_can_* preflight precheck_play can_move_to can_act_now gap_number card_totals card_keywords resolve_target pick_target hand_target_legal` |
| `play.py` `PlayMixin` | 出牌 / 部署 / 上线 / 攻击 / 结束回合 | `play_card play_card_* select_target select_unit_target move_to_front attack_card end_of_turn wait_our_turn` |
| `choices.py` `ChoiceMixin` | 选择与抉择：二选一 / 三选一 / 牌库选牌 / 手牌目标 | `pick_choice pick_layers pick_pending pick_candidates choose_card choose_one_with_target pending_summary select_hand_target hand_target_pending` |
| `flow.py` `FlowMixin` | 换牌、投降、结算页、模式与牌组页、开局 | `mulligan* surrender end_of_match_continue list_decks select_deck select_mode press_play quick_start` |
| `cli.py` `SelftestMixin` | `selftest`（只解析、不动手）与命令行 `main` | `selftest main` |

约定：新方法按职责放进对应 mixin；单文件 ≤ 1500 行；`tests/test_ops_rules.py` 的 lint 棘轮按文件登记（新增文件要登记基线）；
热重载清单（`player/hotreload.py::DEFAULT_MODULES`）里 mixin 模块排在 `ops.inject` **之前**。

## 常被问到的区别

**`agent/` 与 `ops/` 有什么区别？**
`ops/` 是**手**：只管“怎么把一个动作做进游戏客户端里”（注入、合成事件、回读判成败），不知道什么是好棋。
`agent/` 是**一组动词**：把“看（kardsmem）”、“问这步合法吗（semantics）”、“做（ops）”拼成 `play_card / attack / pick …`，
不含任何决策。想让一个玩家上场，写在 `player/`。

**为什么 MCP / shell 不在 `agent/` 里？**
它们是**适配器**（把动词暴露成 JSON-RPC 或命令行），和命令层不是一回事，放进 `interfaces/`，依赖方向就一目了然：
`interfaces → agent`，命令层本身不知道有 MCP。

**为什么没有 SDK 仓库？**
运行时不需要 SDK dump：字段偏移走 UE 反射链现算（`kardsmem/props.py`），全局 RVA 靠版本表 / 运行时扫描
（`kardsmem/rvascan.py`，结果落用户缓存）。`reverse-data/sdk/` 只是开发期拿来重新生成 `kardsmem/build_tables.json`
的输入（`kardsmem/buildsrc.py`），不进发布。

## P6 拆文件登记：`tools/crashdump*` 与 `kardsmem/cardnatives*`（零逻辑改动）

* `tools/crashdump.py`：仅留模块文档 + CLI（`main`），并把下列模块的所有名字（含下划线私有名）重新导出；
  `python tools/crashdump.py ...` 与 `import crashdump` 的用法不变。依赖链单向：
  `_crashdump_base`（常量 / ctypes 结构体 / 进程枚举 / minidump 写入 / 白名单）→ `_crashdump_mdmp`（MiniDump 解析 / cdb 分析 / 堆与委托扫描 / sidecar）
  → `_crashdump_watch`（`Watcher`）、`_crashdump_procdump`（ProcDump 引擎 + `dump_now`）→ `_crashdump_selftest`（`start_watch` / 自测 / `verify_full_dump`）。
* `kardsmem/cardnatives_selftest.py`：`cardnatives.selftest()` 的合成卡自检本体（入口名不变，`cardnatives.selftest` 转调）。

## 约定

* 包内部互相引用用**绝对路径**（`from semantics import effectvm`），不用相对 import。
* **不在模块顶层改 `sys.path`**。入口脚本（`tools/*`、`tests/*`）在自己头部插入仓库根；包内代码不做这件事。
* 运行期文件（日志、缓存、面板状态）一律经 `base.paths`；默认落在 `<仓库>/data/`（开发布局落在伞仓库旁的 `kards-data/`）。
* 新增一个顶层包 = 先在 `tests/test_arch_rules.py::LAYERS` 里登记它允许依赖谁。
* `if __name__ == "__main__":` 里的 import（自检 / 命令行）不受依赖表约束。
