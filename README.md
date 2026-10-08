# kards-agent

**KARDS 自动对局框架**：只读进程内存拿实时盘面，在游戏进程内**合成**与真人等价的鼠标事件来出牌，
上层是一个规则 bot（带盘外模拟 + 评估 + 搜索）。

> English summary — A Windows-only framework for automating the card game KARDS. It reads the live
> game state through read-only process-memory access (UE reflection + a Kismet bytecode interpreter) and
> performs actions by synthesizing the *full* mouse event sequence (hover → press → drag → release) inside the
> game process, so what the server/opponent sees is indistinguishable from a human using a mouse. On top of
> that sit an off-board simulator, an evaluator and a search-based rule bot.
> **Training/AI matches only. Never PvP.** GPL-3.0; originally forked from
> [OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto). No game files are included.

## 红线（先读这个）

1. **信息流与真人等价**：发给服务端/对手的东西，必须和真人用鼠标正常操作产生的完全一致。
   输入侧按真实鼠标的**完整事件序列**重演（悬停 → 悬停转发 → 按下 → 起拖 → 拖动 → 落地/松开），**不跳步**。
2. **读侧只读**：读状态只用 `ReadProcessMemory`；进程内的注入调用只允许“无副作用的只读查询”（例如问游戏自己 `CanAttack`）。
3. **只打训练 / AI 局，不碰 PvP。** 开局闸门（`player/play_guard.py`）和面板都围绕这一条；
   对手 `player_id` 为非负数即真人对局，不得启动。
4. 不改游戏文件、不绕过反作弊/风控、不分发游戏资源。

架构与判据原则见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 系统要求

* Windows 10/11（x64）。读内存、注入、窗口截图都用 Win32 API，不支持其它系统。
* 已安装的 KARDS 客户端（Steam 或官方 launcher）。目前适配 1.60 系列；换版本会在运行时扫描 RVA 并缓存到
  `%LOCALAPPDATA%\kards-agent\rva-cache\`，新版本的兼容性以实机为准。
* Python 3.12+（CI 跑 3.12 与 3.14；开发环境 3.14），需带 `tkinter`（控制面板）。
* 依赖仅 `frida`、`numpy`（见 `requirements.txt`）。

离线部分（模拟、VM、评估、全部测试）**不需要游戏**；上线跑局才需要游戏在运行。

## 安装

```powershell
git clone <本仓库>            # 或解压 dist\kards-agent-<版本>.zip
cd kards-agent
.\install.ps1                 # 建 .venv、装 requirements.txt、跑离线自检
```

手动也行：`python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`。

## 怎么跑

| 想做什么 | 命令 |
|---|---|
| 控制面板（推荐入口：开始/停止、局数、状态、决策历史、日志） | `python -m gui.app`（免装 Python 的 exe 包见 Releases） |
| 常驻监听器（先开游戏再开它；面板也会替你拉起） | `python tools\live_session.py`（= `run_listener.bat`） |
| 离线测试（不需要游戏） | `run_tests.bat [关键字]`（= `python tests\run_all.py`） |
| 读侧自检 / 读一眼盘面 | `python -m kardsmem selftest`、`python -m kardsmem --help` |
| 交互式 shell / MCP server（给人或 LLM 用） | `python -m interfaces.shell`、`python -m interfaces.mcp` |

监听器通过 `data\live\live_cmd.txt`（追加一行 = 一条命令）与 `live_log.txt`（结果）通信；
面板通过 `control.json` 控制它。**不要在对局进行中强杀监听器**（会让游戏崩溃），用面板的“停止”或 `quit` 命令。

## 目录结构与依赖方向

每个包只干一件事，依赖只许向下，由 `tests/test_arch_rules.py` 强制：

```
L5  gui/  interfaces/  tools/        入口：面板 / shell+MCP / 一次性脚本
L4  player/                          玩家：规则策略、在线回路、录制、开局闸门
L3  agent/                          命令层（一组动词）
L2  ops/  semantics/  sim → evaluation → policy
L1  kardsmem/                        读侧：内存 / UE 反射 / Kismet VM / 盘面 / RVA 扫描
L0  base/                            路径、版本、Win32 封装
```

各包的职责与“`agent/` 和 `ops/` 有什么区别”等常见问题见 [docs/STRUCTURE.md](docs/STRUCTURE.md)；
总纲在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，重构路线在 [docs/REFACTOR-PLAN.md](docs/REFACTOR-PLAN.md)。

## 数据落盘位置

运行期文件一律经 `base/paths.py`，**不进 git**：

| 位置 | 内容 |
|---|---|
| `<仓库>\data\` | 默认根目录。环境变量 `KARDS_DATA_DIR` 可改；开发布局（仓库旁有 `reverse-data\`）落在 `..\kards-data\` |
| `data\live\` | 监听器命令/日志通道、热重载开关 |
| `data\nn\logs\` | 决策日志（每步一行 JSONL） |
| `data\recordings\` | 只读录制 |
| `data\gui\` | 面板状态、版本缓存 |
| `data\api\` | 官方卡表缓存（“没进预备”的判据） |
| `%LOCALAPPDATA%\kards-agent\rva-cache\` | 运行时扫描出的 RVA 缓存 |

## 版本、更新日志与发布

* 版本号：`base/version.py`；变更见 [CHANGELOG.md](CHANGELOG.md)。
* 打包：`python tools\make_release.py`——对**已提交**内容 `git archive` 出 `dist\kards-agent-<版本>.zip`，
  并在干净目录里跑一遍离线测试（`--dry-run` 只看不写，`--no-venv` 用当前解释器）。
* 发布：`python tools/build_exe.py` 构建免装 Python 的面板包（PyInstaller），解压后双击 `kards-agent.exe`；
  发布物只含面板包，源码包由 GitHub 自动生成。

## 许可与鸣谢

GPL-3.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。本项目最初 fork 自
[OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto)（yumehanab1，GPL-3.0），
早期借用了其窗口/模板原语；这些文件现已移除，但思路和起步工作归功于上游。

## 免责声明

本项目为个人研究/学习用途，**与 KARDS 及其发行方无任何关联，也未获其认可**。使用自动化工具可能违反游戏的服务条款，
并可能导致账号受限，风险自负。作者不对因使用本软件造成的任何损失负责。请只在训练/AI 对局中使用，不要用于 PvP。
本仓库不包含任何游戏文件、逆向导出物或账号信息。
