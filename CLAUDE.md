# KARDS 自动化框架 —— 给接手的 agent

## 红线（用户给的权威前提，不要越）

**只读内存 + 模拟鼠标。**

- 只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`
- **不注入、不 WriteProcessMemory、不远程线程、不 hook、不在游戏进程里执行任何代码**
- 理由：客户端没有反作弊，服务端对上传数据做行为判定 —— **红线是「写」**

推论：蓝图字节码可以**读出来在 Python 里解释**，但求值器必须是纯的
（`EX_Let*` 只能写影子堆，绝不回写游戏内存）。

## 两条框架级硬约束

1. **读侧严格只读**
2. **读与执行解耦** —— 同一个调用方，`mem` / `ocr` 后端可换
   （`board_api.py` 已做到；执行侧的 `ActionSink` 抽象**还没做**，是欠项）

## 查一个字段/一段逻辑，按这个顺序

**① grep SDK dump → ② 反射链现算 → ③ 读字节码 → ④ 字节 diff（最后手段）**

1. **Dumper-7 的 SDK dump**：`reverse-data/sdk/<build>/CppSDK/SDK/<类名>_classes.hpp`
   **逐字段写着偏移**，蓝图生成类也有。原生类（`UWidget::Visibility // 0x00DC`）、
   `TUObjectArray`、`FField`/`FProperty`、全部枚举也都在里面。
   ```bash
   grep -rn "shouldDiscard" reverse-data/sdk/1.58.27125.Steam/CppSDK/SDK/
   ```
   局限：只对**那一个构建**成立；`__MDKClassSize` ≠ 运行时 `PropertiesSize`。
2. **反射链**（`kardsmem/props.py`）：运行时按 `FProperty` 链算偏移，跨构建自洽。
3. **Kismet 字节码**（`kardsmem/kismet.py`）：逻辑的权威来源。
4. **字节 diff**：只在前三条都答不了时用。

> 血的教训：换牌标记折腾了一整轮字节 diff、还提交过一条错误结论
> （`+0x924` —— 其实是 `MulliganHoverTimeline__Direction`，悬停动画的播放方向），
> 而 `BP_HandCard_classes.hpp:100` 一直躺着 `bool shouldDiscard; // 0x0980`。
> **一条 grep 就完了。**

## 这个仓库的特点：几乎每个「未知」都已经写在某个文件里

本项目手上有**异常齐全的静态资料**：Dumper-7 的 SDK dump（逐字段偏移，蓝图类也有）、
FModel 的 uasset + 反编译、UE 5.6 引擎源码、`.usmap`、`.idmap`、运行时字节码。

⇒ **遇到不知道的东西，先花两分钟 grep 这四个地方，再考虑动手查。**
本会话大部分弯路都是「没查就开干」。

## 走过的弯路（别重走）

1. **字节 diff 找换牌标记**，折腾一整轮、提交过错误结论，而 SDK 里写着
   `bool shouldDiscard; // 0x0980`。→ 见上面的查找顺序。
2. **派 subagent 去 IDA 验 `FField`/`FProperty` 偏移**，跑了 14 分钟，
   结论与 Dumper-7 的 `Basic.hpp` 一字不差 —— 那文件本来就在仓库里。
   → **派人/开工具之前，先确认答案不在已有资料里。**
3. **照抄「原版 UE 的布局」**：以为 `FFieldVariant Owner` 是 16 字节，
   结果 `0x10` 之后整体错位 8，290 个属性的链只走出 8 个、名字全是乱码。
   → **这是个 fork（`++UE5+Release-5.6-Fork-kards`），任何原版偏移都要先验证。**
   自检判据：属性必须全部落在 `PropertiesSize` 内、名字必须全部解析得出。
4. **选错了抽象层**：先去扩展「解析 FModel 伪 C++」的 VM，而正确的层是
   **运行时 Kismet 字节码**（`kismet.py`，全游戏 10861 个函数零失败）。
   伪 C++ 是二手的：类型抹成 `class U*`、控制流变 goto 汤、个别节点会丢。
   → **优先用一手产物。**
5. **说了「正在动手」然后就结束了回合**，用户不得不问「你在干嘛」。
   → **别宣布意图就停下；要么这一回合就做，要么直说还没做。**
6. **在被污染的数据上调参**：预报面板盖住前线那一排，OCR 把面板上的卡采了进去，
   据此改了 pitch。→ 采样工具要**先自检环境**（`rowcalib.sample` 现在会拒采）。
7. **heredoc 反斜杠被吞**，一个会话里踩了四次 —— 第四次就发生在往这份文件里
   写这一条的时候，反斜杠加数字被 shell 当成转义、变成了不可见控制字符。
   → **写文件一律用 Edit/Write 工具，不要用 shell heredoc 写含反斜杠的内容。**

## 证据标准

- **单一观测对得上不算证据。** 先问「这个字段的**取值结构**和该语义的**结构**对不对得上」。
  换牌标记可多选 ⇒ 一个「全局最多一个 0」的字段，光凭这点就该否掉。
- 同类错误犯过两次：`0x3C8 = mySide`、`0x924 = 换牌标记`。
- **类身份只认 `UClass::PropertiesSize`**，绝不认 `*_VFT` 之类的名字。
- 转储是静止的，但**它抓在哪一帧你控制不了** —— 动画期间抓的转储会让你把
  「还在飞」读成「未标记」。
- 结论错了就**撤回并留档**（写清误判来历 + 教训），不要悄悄改掉。

## 代码约定

- 注释写**为什么**，尤其是踩过的坑；结论旁边标明证据来源和强度
- **不硬编码运行时算得出来的偏移**；确实要记的，写成「给人看的备注」常量并注明
- 每次改完代码跑 `cd kards-agent && python -m kardsmem selftest`
- 提交信息用中文，讲清楚「做了什么 + 为什么 + 证据」

## 这是什么项目、边界在哪

`kards-agent/` 是**基于内存 + OCR 的 KARDS 自动化**，2026-09-22 从散落状态收拢成独立目录。

- 它 **fork 自** [OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto)
  （yumehanab1，GPL-3.0）⇒ **本项目同样是 GPL-3.0**，见 `LICENSE`。
- **`D:\Kards\OCR-Kards-Auto/` 是别人的工作树，一个字都不要改。**
  它提供 `win` / `actions` / `ui_state` 这些原语和模板图基线；
  我们自己加的状态和模板放 `kards-agent/config/` 当 **overlay**，
  启动时由 `board_api.py` 盖上去。这样上游随时能 `git pull`。
  改之前先 `cd OCR-Kards-Auto && git status` 确认它是干净的。
- `D:\Kards` 这个仓库**不是**自动化专用：还有 `client/`、`server/`、`ue-project/`、
  `game-installs/`，以及 `reverse-data/`（逆向资料与取材工具）。别把自动化的东西撒出去。

## 目录

| 路径 | 是什么 |
|---|---|
| `kards-agent/kardsmem/` | **读侧**（唯一入口）。`world/cards/gs/names/props/objects/kismet/pick` |
| `kards-agent/ops.py` | **执行侧**（会动鼠标） |
| `kards-agent/board_api.py` | mem / ocr 双后端（硬约束②的落点） |
| `kards-agent/config/` | 盖在上游之上的状态/模板 overlay |
| `reverse-data/reports/KARDS-AUTOMATION.md` | **主规格**，先读它 |
| `reverse-data/tools/` | 逆向/取材工具（`dumpmem` `mdmp` `mulverify` `idmap_lookup` `FModel.exe` …）。要用 `kardsmem` 就 `import _agentpath` |
| `reverse-data/sdk/<build>/` | Dumper-7 导出（SDK / Dumpspace / .usmap / .idmap） |
| `reverse-data/exports-<build>/` | FModel 导出的 uasset + 反编译伪 C++ |
| `H:\EpicGames\UE_5.6` | UE 引擎源码（opcode 表、`ScriptDisassembler.cpp`、`KismetMathLibrary`） |

自检：`cd kards-agent && python -m kardsmem selftest`

## 环境坑

- Windows + PowerShell/Git Bash。**heredoc 里的反斜杠会被吞**（`\2026` → 控制字符，
  `\\n` → 真换行）。改文件优先用 Edit 工具，或用 `chr()` 拼接。
- 同名 `kards-Win64-Shipping.exe` 磁盘上有多份，**按 SizeOfImage 认构建**，不认 md5
  （私服 URL 补丁不改 RVA 但改 md5）。见 `reports/EXE-IDENTITY.md`。
- **窗口没焦点会把鼠标点击吃掉**：事件发出去了、游戏没反应、内存零变化，
  看起来和「坐标错了」一模一样。焦点已经收进 `ops.hwnd()`，但
  `mull_auto.py` / `do_mulligan.py` 还没修。
- **选择界面开着时，出牌/移动/攻击全部无效**，而且面板可能被翻页收到屏幕右侧
  （看不见但 `chooseOneActive` 仍为 1）。

## 工作方式

- 用户在旁边看着屏幕。**开对局/动鼠标前先确认**。
- 自动脚本是未来计划，**不能是简单启发式** —— 别在现有那个残废的 `playturn` 上投入。
  收集大量数据时才用它，平时用户自己手打。
