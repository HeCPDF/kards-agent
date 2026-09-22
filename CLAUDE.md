# KARDS 自动化框架 —— 给接手的 agent

## 红线（用户给的权威前提，不要越）

**只读内存 + 模拟鼠标。**

- 只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`
- **不注入、不 WriteProcessMemory、不远程线程、不 hook、不在游戏进程里执行任何代码**
- 理由：客户端没有反作弊，服务端对上传数据做行为判定 —— **红线是「写」**

推论：蓝图字节码可以**读出来在 Python 里解释**，但求值器必须是纯的
（`EX_Let*` 只能写影子堆，绝不回写游戏内存）。

## 这个项目要做的四件事

1. **高层 API：从内存读游戏实时状态**（`kardsmem`）
2. **高层 API：用鼠标操纵客户端**（`ops.py`）
3. （future）MCP server
4. （future）NN

框架级硬约束只剩一条：**读侧严格只读**。

> 「读/执行后端可换（mem ↔ ocr）」这条**已撤销**。OCR 能做到的有限，
> 它一直只是**核对手段**，而且最终证明与内存完全对得上 ⇒ 内存是唯一权威。
> `board.read_field` 那条 OCR 读盘面的路留着但是可选：上游没 checkout 时
> `ops.B is None`，不影响任何功能。
> 反过来，**手牌扇形的 x 坐标内存里没有**（纯客户端排版），那一块必须走像素 ——
> 用的就是上游的边缘检测算法（`vendor/handedge.py`）。

## 判据负责「挑」，游戏负责「判」

进程外重算合法性（`CanPlayFromHand` / `CanAttack` 那一套）**必须做** ——
没有它就没法在几十个候选里挑出该做什么。但它**一定有缺漏**：
卡池两千张、效果互相叠、还有一层原生代码我们读不到。

⇒ **判据不足时仍然把动作发出去**，让游戏自己裁决，再读它的回执
（`kardsmem.notify`，规格 §7.6f）。
**绝不要用外部判据去否决动作** —— 那只会把它算不明白的正确招法永久屏蔽掉。

游戏的提示文本不止讲「为什么不行」：抽牌、疲劳伤害、回合开始都走同一条通道，
对双方公示。所以它其实是一条**事件流**，而不是「错误信息」。
「有提示」≠「动作失败」，要看文本内容分类。
提示**短命**（淡入淡出后 widget 就销毁），只能轮询，事后捞不到。

## 坐标模型：场上和手牌不是一回事

* **场上三行**是居中等距：`x = center + (rank - (n-1)/2) * pitch`
  （`ops.ROW_SPECS`，rank = 行内按 `locationNumber` 的密集名次，n = 行内卡数，**含总部**）。
* **手牌不是。** 扇形是叠着的：前面每张只露一条、**最后一张完全露出**，
  所以末位间距（83~109px）远大于内侧（46~69px），且 n 越大内侧越挤。
  ⇒ 只能标定，用 `config/hand_layout.json` 按**张数**索引，取 `probes[i]`。
  ★ 表项**必须按张数取**。曾用 `left_edge` 最接近去匹配，9 张手牌配到 5 张的表项，
    出牌 x 差了 140px。张数是从内存读的权威值，**别让像素盖过它**。
  ★ 也别拿 `detect_left_edge` 去平移整排：实测它给的左缘(413)和真实卡面左缘(355)
    差得远，平移会把落点推到隔壁那张牌上（实测把 KINGFISHER 当 TASK FORCE 打了出去）。

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
7. **把「进程外判据」当成否决权**。判据缺漏是必然的，拿它挡动作＝把算不明白的
   正确招法永久屏蔽。正确做法见上面「判据负责挑，游戏负责判」。
8. **把游戏的提示通道当成「错误信息」**。它其实是对双方的公示流
   （抽牌、疲劳伤害、回合开始都走它），把它当错误处理会既漏事件又误判失败。
9. **heredoc 反斜杠被吞**，一个会话里踩了四次 —— 第四次就发生在往这份文件里
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
- **只搬了用得上的一小块**，都在 `vendor/`（带来源头注释，尽量别改）：

  | vendor | 干什么 | 为什么留着 |
  |---|---|---|
  | `win.py` | 窗口 / DPI / 客户区坐标 / 截图 / 置前 | 目标②的地基 |
  | `actions.py` | 鼠标原语 | 同上 |
  | `deploy.py` | `drag_deploy` 拖拽出牌手势 | 同上 |
  | `ui_state.py` `cv_io.py` | 模板匹配、界面分类 | 开局/菜单流程（`startmatch.py`） |
  | `handedge.py` | 手牌扇形左右边缘 | **内存里没有这个量** |

  外加 `config/`（18 个模板 + 8 个状态）和 `ui_templates/`。
  ⇒ **运行时完全不依赖上游那棵工作树**（`KARDS_OCR_ROOT=/nonexistent` 下已验证）。
  **上游对齐版本：`576aa19`（v0.1.8_beta）**；怎么更新、每个模块谁在用、上游那批新东西里
  哪几样值得接，都写在 `vendor/README.md`。其中**窗口消息输入**（`actions.INPUT_MODE="message"`,
  `PostMessage`/`SendMessage`，不移动真实光标）能治"窗口没焦点点击被吃掉"的老毛病 —— 但
  **用户 2026-09-22 定调：它将来要被「基于内存的实现」替换，是过渡手段，别把架构压在它上面**；
  目前我们**还没接**（用的仍是 vendor 默认的物理光标路径）。
- 上游其余部分我们**不用**：官网卡表 json、自动打牌状态机、手牌扫描那整条 OCR 链。
  （OCR 读盘面只在**核对**时可选走一下，见 `tools/field.py`；内存才是权威。）
- **`D:\Kards\OCR-Kards-Auto/` 是别人的工作树，一个字都不要改**（已恢复到 `origin/main`）。
  要动它之前先 `cd OCR-Kards-Auto && git status` 确认干净。
- `_archive/` 是被 `ops.py` 取代的一次性脚本和那个残废的自动打牌，留档不维护；
  `_archive/mem-era/` 是更早的内存探针（功能已并入 `kardsmem`）。
- `D:\Kards` 这个仓库**不是**自动化专用：还有 `client/`、`server/`、`ue-project/`、
  `game-installs/`，以及 `reverse-data/`（逆向资料与取材工具）。
  **自动化 + mem 工具一律放在 `kards-agent/` 下**，别再撒出去。

## 目录

| 路径 | 是什么 |
|---|---|
| `kards-agent/kardsmem/` | **读侧**（唯一入口）。`world/cards/gs/names/props/objects/kismet/pick` |
| `kards-agent/ops.py` | **执行侧**（会动鼠标） |
| `kards-agent/board_api.py` | 盘面模型 + 数据源（`mem` 是权威，`ocr` 只作核对）—— **本项目自己的代码** |
| `kards-agent/tools/` | 取材/标定工具（`mem_probe` `dumpmem` `mulverify` `pickwatch` `grid` `crop` `field` `pe_tools` …）。脚本里 `import _bootstrap` 就接好路径 |
| `kards-agent/vendor/` | 从上游搬来的**六个**在用的模块（GPL-3.0，逐个查过引用） |
| `kards-agent/config/` `ui_templates/` | 模板表与模板图（自足） |
| `kards-agent/_archive/` `_archive/mem-era/` | 被取代的一次性脚本（含旧的内存探针），留档不维护 |
| `reverse-data/reports/spec/KARDS-AUTOMATION.md` | **主规格**，先读它 |
| `reverse-data/tools/` | **只剩**逆向/静态/第三方工具（`idmap_lookup` `search_exports` `u4pak` `FModel.exe` …）—— 自动化与 mem 工具**不在这里** |
| `reverse-data/sdk/<build>/` | Dumper-7 导出（SDK / Dumpspace / .usmap / .idmap） |
| `reverse-data/exports-<build>/` | FModel 导出的 uasset + 反编译伪 C++ |
| `H:\EpicGames\UE_5.6` | UE 引擎源码（opcode 表、`ScriptDisassembler.cpp`、`KismetMathLibrary`） |

自检：`cd kards-agent && python -m kardsmem selftest`

## 环境坑

- Windows + PowerShell/Git Bash。**heredoc 里的反斜杠会被吞**（`\2026` → 控制字符，
  `\\n` → 真换行）。改文件优先用 Edit 工具，或用 `chr()` 拼接。
- 同名 `kards-Win64-Shipping.exe` 磁盘上有多份，**按 SizeOfImage 认构建**：
  它直接决定 RVA 有没有效。md5 只说明这份副本有没有被本地改动过，**不作否决**
  （同构建的两份副本 md5 本来就可以不同，见 `kardsmem/exes.py`）。
- **窗口没焦点会把鼠标点击吃掉**：事件发出去了、游戏没反应、内存零变化，
  看起来和「坐标错了」一模一样。焦点已经收进 `ops.hwnd()`，但
  `mull_auto.py` / `do_mulligan.py` 还没修。
- **选择界面开着时，出牌/移动/攻击全部无效**，而且面板可能被翻页收到屏幕右侧
  （看不见但 `chooseOneActive` 仍为 1）。

## 发布纪律（2026-09-22 那次事故换来的，别重犯）

**`kards-agent/` 不是独立仓库**，它是伞仓库 `D:\Kards` 的一个子树
（`git -C D:\Kards\kards-agent rev-parse --show-toplevel` → `D:/Kards`）。
在它里面跑 `git push` 推的是**整个伞仓库** —— `reverse-data/`、`game-installs/` 会一起上公网。
2026-09-22 就是这么泄的（2507 个跟踪文件 / 1.55 GiB，2 分 50 秒后才发现，见
`reverse-data/reports/report/INCIDENT-2026-09-22-public-push.md`）。

公开 = **只推拆分出来的那一个分支**：

```powershell
git -C D:\Kards subtree split -P kards-agent -b publish/kards-agent
# 这两个 tree 必须相等，不等就停手
git -C D:\Kards rev-parse 'publish/kards-agent^{tree}'
git -C D:\Kards rev-parse master:kards-agent
git -C D:\Kards push https://github.com/HeCPDF/kards-agent 'publish/kards-agent:master'
```

伞仓库 `D:\Kards` **不要**挂指向公开库的 remote。

## 现在做到哪、下一步做什么（2026-09-22 本轮收尾时的状态）

**先读这个**：`reverse-data/reports/spec/KARDS-AUTOMATION.md` §10 交付状态 + §11 未解决（**唯一权威清单，别在别处抄一份**）。

**已交付**
- 读侧：R1 场上卡 / R2 双方手牌 / R4 弃牌堆 / R5 FName / R7 当前属性 / R9 位置 / R10 临时 CardID ✅；
  R3 候选牌 ⚠ 半；**R8 可指向目标 ❌**（唯一正路：把 `cardsCheckFunctions.cpp` + 438 个
  `CanPlayFromHand` 覆写移植成 Python，`card_targets.py` 已 18/20）。
- 执行侧：E1 出牌 / E2 指向 / E3 部署后选目标 / E4 攻击 / E5 上线 / E6 结束回合 / E7 抉择点选 /
  E8 换牌 / E10 回读判成败 ✅；E9 投降坐标已标定（规格 §7.6e），**还没封进 `ops.py`**。
- 自检：`cd D:\Kards\kards-agent && python -m kardsmem selftest` → 全 PASS（含 board_api 30 项 +
  与 `kards-offsets.json` 的一致性 + 行模型 7 组）。**改完代码必须跑这个。**

**本轮新增但还没实机验证**（当时游戏没开，只过了离线断言）
`kismet.py`（Kismet 字节码反汇编）· `objects.py`（GUObjectArray 遍历）· `props.py`（按名字的反射链）
· `notify.py`（读游戏提示文本＝动作回执）· `tools/` 里搬过来的那批脚本。
⇒ 下次游戏在跑时，按这个顺序验：`python -m kardsmem notify` → `tools/mulverify.py <before.DMP> <after.DMP>`
→ `tools/pickwatch.py` → `tools/mem_probe.py --selftest`。

**P0（按优先级）**
1. `kardsmem.notify` 接进 `ops.py` 的动作回执：动作发出后开 1~2s poll 窗口，把提示文本连同回读结果一起返回（规格 §7.6f）。
2. 提示轮询的采样率：提示淡入淡出后 widget 就销毁，间隔太大会整条漏掉 ⇒ 实测存活时长。
3. **前线行标定样本全部重采**：旧 133 行被预报面板污染作废（隔离在 `logs/rowcalib.jsonl.suspect-20260922`）；`rowcalib.sample` 已加"选择界面开着就拒采"的护栏。
4. `keepOrder=true` 的 10 张候选 → 屏幕第几个：坐标已标定，缺的是运行时按名字读卡对象上的 `_forecastOptions` 这类 TArray（有反射链了，不用再 diff）。
5. `keepOrder=false` 的 26 张：抽样/排序在**原生代码**里 ⇒ 进 IDA 找 `GetChooseSpawnCards` 的调用方。

**判据的用法**：进程外重算合法性只用来**排序和挑选**，**绝不用来否决**动作 —— 缺漏是必然的
（卡池两千张、效果互相叠、还有原生代码）。动作发出去，让游戏裁决，再读 `notify` 拿权威理由。

## 近期大事（2026-09-22，读一次就够）

1. **公开库事故**：在 `kards-agent/` 里裸跑 `git push` 把**整个伞仓库**（2507 个文件 / 1.55 GiB）推上了公开库，
   约 2 分 50 秒后转 private、随即删库重建，现在只发 `kards-agent` 子树。
   留档：`reverse-data/reports/report/INCIDENT-2026-09-22-public-push.md`；规矩写在本文件「发布纪律」。
2. **大扫除**：kardsmem 里的**私服改造细节全清**（判据改成「SizeOfImage 决定 RVA 有效性，md5 只作信息」）；
   自动化/mem 工具**全部归位 `kards-agent/`**（`tools/` + `_archive/mem-era/`）；
   `reverse-data/tools/` 只剩逆向/静态/第三方。偏移记录并入 `kards-offsets.json` 的 `build.data`
   （照 Dumper-7 `OffsetsInfo.json` 格式），版本命名按 `GAME-VERSIONS.md` 的 `<版本号>.<渠道>`。
3. **上游对齐到 `576aa19`**（v0.1.8_beta）：`vendor/actions.py`、`vendor/deploy.py` 已更新；
   上游新增的**窗口消息输入**（不移动真实光标）与 `fallback_drop` 还没接，见 `vendor/README.md`。
4. **撤销了一条"硬约束"**：「读/执行后端可换（mem↔ocr）」不再是约束 —— OCR 只是核对手段，
   内存是唯一权威。唯一硬约束＝**读侧只读**。

## 会话与文档

| 想干什么 | 去哪 |
|---|---|
| 接续任务 | 本文件 → `reverse-data/reports/spec/KARDS-AUTOMATION.md`（主规格）→ 其 §11 |
| 读 exe 指纹 / 为什么 md5 对不上 | `reverse-data/reports/ledger/EXE-IDENTITY.md`、`python -m kardsmem exes` |
| 版本 ↔ dump ↔ 导出 | `reverse-data/reports/ledger/GAME-VERSIONS.md`（命名规则 `<版本号>.<渠道>`） |
| 游戏规则原文 | `reverse-data/reports/spec/KARDS-RULES-ENCYCLOPEDIA.md` |
| 哪些卡自动化做不了 | `reverse-data/reports/ledger/CARD-AUTOMATION-COVERAGE.md` |
| 坐标/读图账本 | `reverse-data/reports/ledger/VISION-AND-COORDINATES.md` |
| 读 Claude/DSH 的会话日志（zstd jsonl → 可读 markdown） | `_session_claude/extract_claude.py`（Claude Code）· `_session_extract/extract.py`（DSH） |
| 发布（**只发 kards-agent 子树**） | 见下面「发布纪律」 |

## 工作方式

- 用户在旁边看着屏幕。**开对局/动鼠标前先确认**。
- 自动脚本是未来计划，**不能是简单启发式** —— 别在现有那个残废的 `playturn` 上投入。
  收集大量数据时才用它，平时用户自己手打。
