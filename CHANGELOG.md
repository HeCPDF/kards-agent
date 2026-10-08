# 更新日志

按里程碑提炼，不逐条抄 `git log`。版本号见 `base/version.py`。日期为提交日期（2026）。

## [0.2.2] — 2026-10-08 修复启动脚本与前线格数

* **修复**：0.2.1 的 `start_gui.bat` 在用户机器上解析错乱（UTF-8 中文 + LF 行尾 + `if (...)` 块里的括号，cmd.exe 把文本当命令执行）。改为纯 ASCII、`goto` 结构；新增 `.gitattributes`，`*.bat`/`*.cmd`/`*.ps1` 一律 CRLF（`git archive` 导出也是 CRLF）；快速开始文档改名 `GUI-QUICKSTART.md`。另：`start_gui.bat` 调 `run_gui.bat` 改用 `%~dp0` 全路径（设了 `NoDefaultCurrentDirectoryInExePath` 的机器上，裸名字不会在当前目录找）。
* **修复（实机）**：`ops/query.py::_free_front_slot` 把前线写死成“我方 4 格”，而原版前线是双方共用 5 位（受限时 2）⇒ 前线有 4 张时误报“满了”，一局里 `move_up` 被自己拒了 4 次；改按真容量算，补 `test_free_front_slot.py`。
* **修复（实机）**：回合首步指挥点等待的放行条件由“槽数 == 应有值”改为“槽数 ≥ 应有值”（卡牌可额外加槽，等号判据在后手第 12 回合起连续超时 2 s）；陈旧读数仍被挡住。

## [0.2.1] — 2026-10-08 免装 Python 的面板包

* **免装 Python 的 Windows 面板包**（PyInstaller onedir，`tools/build_exe.py`）：解压后双击 `kards-agent.exe` 开控制面板，"启动监听器"由同一个 exe 以 `--listener` 起；`--selfcheck` 离线自检（不碰游戏）。入口 `tools/gui_main.py`；冻结态的数据目录 = `<exe 目录>\data`。冻结监听器已实机验证：能 attach 到运行中的游戏、命令通道可用、`quit` 正常退出。另提供 Python 版精简 GUI 包（`start_gui.bat` 一键启动，首次自动建 venv）。面板包内的子进程（powershell/git/探针）统一 `CREATE_NO_WINDOW`，不再闪黑窗。

## [0.2.0] — 2026-10-08 重构与实机打磨

* **引擎本体化**：规则在 `engine/`（动词直跑、类型化调用重放、natives 移植）；`sim/`、`semantics/` 变薄壳；P5 影子对账（字典路 / 直跑 / 调用重放三方对账，离线 338 张指令一致）作为安全网。
* **延迟效果登记表**（`engine/deferred.py`）：打出后仍布防的钩子（被攻击后/回合开始/回合结束…）按事件触发，不再被记成"已知为空"；`docs/DEFERRED-EFFECTS-CENSUS.md` 盘点 180 张留钩子的指令，未接的如实记缺口。
* **延迟**：单次决策里 VM 空跑共享读取（`kardsmem/readscope.py`）+ 卡视图块读，`build_sim_s` 中位数 3.9 s → ~0.5 s；攻击闸门复用快照；回合首步指挥点等待改按游戏口径自适应。
* **修复（均来自实机）**：打总部保真（总部重甲/免疫）、单位被定住（`pinnedTurns`）不再白提移动、KAR 无可选手牌的幽灵"选手牌目标"提示（读侧与动作闸门都识别，且不再因看门狗投降）、自动开局从每日任务弹窗走完整流程。
* **已知限制**：只适配 1.60 launcher/Steam 实测；搜索不模拟对手回合；幽灵提示状态下游戏本体是否接受动作尚未实机证实；`TODO.md` 有完整未决清单。

## 2026-10-04 ~ 10-05 · 重构推进（未发版，随下个版本发布）

* **engine 本体化**：`sim/*`、`semantics/*` 变成壳，规则在 `engine/`；动词直跑（`engine/scripts.py`，101/147 个动词）、类型化调用重放（`engine/calls.py`）。
* **P5 影子对账**：搜索后对手里指令做『字典路 / 直跑 / 调用重放』三方对账，只记录进 `probe["shadow"]`，不影响决策；用于逐路径安全切换。
* **修复**：`getTotalAttack` 原生漏 `attackBuff`（58th INFANTRY REGIMENT 有冲击 +2 攻被读成 1 攻 ⇒ 2nd RAIDING BRIGADE 把它当合法目标）；
  天气族假缺口（`GetOppositeSide` 读错参数位）；减防到 0 现在会摧毁；SetValue 不再抹 buff。
* **P6 拆大文件（纯搬移）**：`engine/scripts.py` → `scripts_ctx` / `scripts_sinks_core` / `scripts_sinks_more`；`engine/effectvm.py` → `effectvm_tables` / `effectvm_selftest`；原模块路径与导出不变。
* **整理**：Card 字段别名读侧 177 → 20（直接读 `obj`）；开发机硬编码路径改走 `base.paths`；`policy/boardeval` 门面删除。
* **P6 拆文件**：`engine/triggers.py`(2404 行) 拆成 `triggers`(核心) + `triggers_specs`(数据表) + `triggers_families`(钩子族)，`engine.triggers` / `semantics.triggers` 名字与补丁点不变。
* **整理（P6）**：`tools/crashdump.py` 拆成 `_crashdump_*.py` 五个模块（CLI / 导入路径不变）；`cardnatives` 自检搬到 `cardnatives_selftest.py`，两者都 ≤1500 行。
* **已知问题**：重启监听器后的第一局曾崩游戏一次（未复现，见 `TODO.md` 10-05 补记）。

## [0.1.0] — 2026-10-03 首个可发布整理

* 新增 `README.md`、`CHANGELOG.md`、`requirements*.txt`、`install.ps1`、`run_gui.bat` / `run_listener.bat` / `run_tests.bat`、
  `tools/make_release.py`（`git archive` 导出已提交内容并在干净目录跑离线测试）、`.github/workflows/ci.yml`、`base/version.py`。
* 修复发布缺陷：5 个测试硬编码了开发机路径 `sys.path.insert(0, r"D:\Kards\kards-agent")`，在干净导出里会悄悄测到别处的代码；已删除。
* `NOTICE` 改为鸣谢式说明（上游文件已移除）；`.gitignore` 补虚拟环境等条目。

## 2026-10 上旬 · 结构整理与架构落地（10-02 ~ 10-03）

* **物理拆分与依赖方向**：`boardeval` 拆成 `sim/`（盘外模拟）、`evaluation/`（估值）、`policy/`（搜索/应答）；
  `agent/` 大杂烩按职责拆开（`player/`、`interfaces/`、`semantics/`、`learn/`、`gui/`、`base/`）；
  `tests/test_arch_rules.py` 强制“依赖只许向下”。
* **座位与数据结构**：座位统一为游戏原版 `ESide + mySide`，去掉自造的 local/enemy；新增 `kardsmem/gamemodel.py`
  （移植原版枚举与 `GameState` 取牌函数）；Card 只剩原版对象。
* **决策模型**：行动 = 动作 + 其后全部选择（路径），抉择/预报两层/手牌目标/三选一池都走同一套可挂起的决策；
  强制决策节点、24 路径剪枝、执行侧照路径点。
* **运行时版本与 RVA**：按进程内存里的 `ProjectVersion` 识别游戏版本；`kardsmem/rvascan.py` 运行时扫描 RVA（对齐 Dumper-7 判据）
  并缓存，launcher 1.60 实机通过；只读 pak 读取器 `tools/pakread.py`。
* **控制面板 `gui/`**（`run_gui.bat`）：局数/自动开下一局/每局回合上限/回避牌名、监听器启停（优雅退出）、
  决策历史、实时状态与日志着色、检测更新；识别并等待“上一局残留读数”。
* **评估正确性**：天气/预报九路径评估、指令不再“编造价值”、压制/定住/回合族/牌库变化/情报触发等钩子、
  老兵/治疗/偷牌/转化接入、前线上限用真值（FrontlineLimiters）、撤退去掉隐蔽兜底。
* **清理**：移除跟踪的截图/草稿/运行期缓存；运行期路径统一到 `base/paths.py`（默认 `<仓库>/data/`）；
  移除上游遗留（OCR 盘面后端、`vendor/`、模板与坐标表、物理鼠标与模板匹配工具）。

## 2026-10 初 · 稳定性与随机流（10-01 ~ 10-02）

* **攻击被静默吞掉**修复：提交批次显式写 `LocationUnderCursor` / `RowUnderCursor`，三局 30/30 成功；
  删掉两条“把光标移出窗口”的拐棍；失焦降帧问题改为按游戏帧数计停留。
* **跨线程调用蓝图 ⇒ 随机崩溃**定案：所有注入的 `ProcessEvent` 改在游戏线程（PC `ReceiveTick`）上执行。
* **随机流**：定位 `BP_CardFunctions` 的 `FRandomStream`，20 Hz 外部只读采样；预报（三选一）与“好人寥寥”洗牌型三选一预测，
  实机命中；“池 = 官方 API 当前卡表”而非离线快照。
* **跨局缓存**：座位/Logic 缓存绑定到当前对局对象，换局自动失效（修复连局第二局 0 出牌）。
* **原生函数补全**：IDA 普查后补齐一批 `BattleUtilityFunctions` / 卡牌原生读写、容器原语、静态卡提供者（2019 张）。
* 开局流程三态化（对局中 / 结算页 / 牌组页）与开局硬闸门；`tools/crashdump.py` 入库。

## 2026-09 下旬 · 从读内存到进程内合成输入（09-22 ~ 09-28）

* **09-22**：`kards-agent/` 从散落状态收拢为独立目录（fork 自 OCR-Kards-Auto，GPL-3.0）；发布事故后立“发布纪律”（只推拆分的子树）。
* **09-23**：`kardsmem` 读侧成形（UE 反射链按名字算偏移、`GUObjectArray` 遍历、Kismet 字节码反汇编 10861 个函数零失败、
  游戏提示文本事件流、本地化表）；MCP server（零依赖 stdio JSON-RPC）。
* **09-24**：外部 Kismet 解释器 + 卡牌目标合法性（`CanPlayFromHand` / `CanAttack` 的 Python 移植）；
  “判据负责挑、游戏负责判”原则确立。
* **09-25**：红线放宽为“信息流与真人等价”：读侧允许进程内只读查询，输入侧允许进程内合成事件（必须含完整悬停序列）。
* **09-26 ~ 09-27**：`ops/inject.py` 完成执行侧（出牌/两种指向/上线/攻击/抉择/换牌/结束回合/投降/悬停 inspect）；
  物理鼠标实现归档；`agent/` 命令层成为三个前端的唯一动词集合；部署位置改按“空隙”语义。
* **09-28**：保密计划（抉择 + 指向）两步流程实机首验。

## 2026-09-21 · 起点

* 初始化 Kards 伞仓库（逆向资料、取材工具、自动化代码同仓；自动化部分后来拆为 `kards-agent/`）。
