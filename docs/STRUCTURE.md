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
| `ops/` | **写侧**。往游戏进程里注入 JS，合成鼠标事件序列（悬停→按下→拖动→松开，信息流与真人等价）。`ops/inject.py` 是执行器，`agent.js.tpl` 是注入的 JS | base, vendor, kardsmem |
| `semantics/` | **牌是什么意思**。把游戏自己的蓝图字节码在外部 VM 里空跑：`effectvm`（效果摘要）、`triggers`（别的牌看到它进场/死亡/攻击时的触发链）、`legality`（动作合法性）、`cardprobe`、`choosespawn`（三选一）、`forecast`（预报预测） | base, kardsmem |
| `sim/` | 盘面模拟（单位、结算、抽牌链、提示/挂起）。**纯逻辑**，规则照原版移植（`docs/SIM-FIDELITY.md`） | base, kardsmem（只用 RNG） |
| `evaluation/` | 盘面价值（只依赖 `sim` 的数据结构） | sim |
| `policy/` | 搜索与应答：候选展开、`answer` / `forced` / `plan`；`boardeval.py` 是对 sim/evaluation/policy 的兼容门面 | sim, evaluation, kardsmem, base |
| `agent/` | **命令层**：`AgentSession` 把读侧、写侧、合法性收成**一组动词**（出牌/攻击/选择…），加上盘面编号与渲染（`view`）、预检（`precheck`）、统一结果类型 | base, vendor, kardsmem, ops, semantics |
| `learn/` | 离线学习：特征、模型、训练（torch，独立 venv，可选）。主环境不需要 | base |
| `player/` | **玩家**。`rule`（规则策略 V2，决策）、`loop`（在线回路：每步 观察→决策→执行→回读）、`play`（把二者接起来的入口）、`record`（只读录制）、`hotreload`、`play_guard`（对局进行中绝不点开始） | agent, policy, sim, evaluation, semantics, ops, kardsmem, learn |
| `interfaces/` | 给人 / LLM 用的入口：交互 `shell`、`mcp` server。只做适配 | agent |
| `gui/` | 控制面板（tkinter）：开始/停止、状态、日志、检测更新；通过 `control.json` 与常驻监听器通信 | agent, player, kardsmem, base |
| `tools/` | 一次性脚本（标定、探针、内存 dump、常驻监听器 `live_session.py`）。可依赖任何包，**不被任何包依赖** | 任意 |
| `tests/` | 离线测试（`python tests/run_all.py`）；不需要游戏 | 任意 |

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

## 约定

* 包内部互相引用用**绝对路径**（`from semantics import effectvm`），不用相对 import。
* **不在模块顶层改 `sys.path`**。入口脚本（`tools/*`、`tests/*`）在自己头部插入仓库根；包内代码不做这件事。
* 运行期文件（日志、缓存、面板状态）一律经 `base.paths`；默认落在 `<仓库>/data/`（开发布局落在伞仓库旁的 `kards-data/`）。
* 新增一个顶层包 = 先在 `tests/test_arch_rules.py::LAYERS` 里登记它允许依赖谁。
* `if __name__ == "__main__":` 里的 import（自检 / 命令行）不受依赖表约束。
