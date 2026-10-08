# `tools/` —— 入口脚本与取材 / 诊断工具

脚本里 `import _bootstrap` 接好 `sys.path`（不要写绝对路径）。读侧入口在上一层：`python -m kardsmem <cmd>`。
崩溃转储工具另见 [CRASHDUMP.md](CRASHDUMP.md)。

## 运行与打包

| 文件 | 用途 |
|---|---|
| `live_session.py` | 常驻监听器：一个进程、一次 attach，按命令文件逐条执行（面板的“启动监听器”就是起它） |
| `gui_main.py` | 打包版 `kards-agent.exe` 的唯一入口（面板 / `--listener`）；开发布局下也能直接跑 |
| `gui_closure.py` | 求面板运行所需代码的 import 闭包（供 `build_exe.py` 与 `gui_main.py` 自检） |
| `build_exe.py` | 把面板打成免装 Python、免联网的 Windows x64 PyInstaller 包（onedir） |
| `make_release.py` | 从已提交内容导出、打包，并在干净目录里跑离线测试 |

## 内存 / 读侧工具（只读）

| 文件 | 用途 |
|---|---|
| `mem_probe.py` | 只读探针：构建校验 + kredits/槽位/盘面，自带 `--selftest` |
| `mem_find.py` | 进程内字节特征搜索 |
| `info.py` | 给 card_id，打印它所有能读到的字段 |
| `notifywatch.py` | 游戏提示文本的实机取材 |
| `pickdump.py` / `pickwatch.py` / `pick_cards.py` | 选择界面的候选卡取证 |
| `kards_api_reserved.py` / `choose_spawn_predict.py` | 官方 API “预备”状态 / 三选一候选预测 |
| `effects_live_hand.py` | 对手牌逐张 VM 空跑，打印效果摘要 |
| `canplay.py` / `card_targets.py` / `card_coverage.py` | 离线跑卡牌判据 / 可指向性 / 自动化可行性普查 |
| `census_deferred.py` | 延迟 / 常驻效果普查 → `docs/DEFERRED-EFFECTS-CENSUS.md` |
| `rvascan_live.py` | 对运行中的游戏做 RVA 扫描并与种子表对账 |
| `pakread.py` | UE5 pak（v10/v11）只读读取器 |
| `pe_tools.py` | PE 解析辅助 |

## 转储与离线分析

| 文件 | 用途 |
|---|---|
| `crashdump.py`（`_crashdump_*.py`） | 崩溃时自动落完整内存转储（常驻旁观，只读） |
| `mdmp.py` / `dumpmem.py` | 解析 minidump / 把转储挂成只读内存源 |
| `mulverify.py` | 用两份转储核对换牌标记字段 |
| `reconcile_batch.py` | 影子对账的离线批量版（不需要游戏在跑） |
| `bench_search_sim.py` | `RuleV2._search_sim` 的离线计时与等价对拍 |

## 其他

`depgraph.py`（包间依赖图）、`shot.py`（后台抓游戏客户区截图，不需要窗口在前台）。

## 约定

新脚本放这里；一次性探针不要放这里，放仓库外的临时目录，用完即弃。改动后跑 `python tests/run_all.py`。
