# `kards-agent/tools/` —— 取材 / 标定 / 探针工具

> 2026-09-22 从 `reverse-data/tools/` **搬进这里**：自动化相关的东西一律归 `kards-agent/`。
> 全部脚本 `import _bootstrap` 就接好路径（**不要再写 `D:\` 绝对路径**）。

两个入口在上一层：读 **`python -m kardsmem <cmd>`**，动鼠标 **`python ops.py <cmd>`**。

## 内存侧（读，全部只读）

| 文件 | 用途 |
|---|---|
| `mem_probe.py` | Stage-0 只读探针：构建校验 + kredits/slots/盘面 + 自带 `--selftest`（合成目标，不需要游戏） |
| `mem_find.py` | 进程内字节特征搜索（定位 FNamePool 之类） |
| `mdmp.py` | 最小 minidump 解析：`Memory64ListStream` → VA→文件偏移表 |
| `dumpmem.py` | 把 minidump 当成一个**只读内存源**挂进 `kardsmem`（进程没了也能分析） |
| `mulliganprobe.py` | 换牌勾选位的内存 diff 定位（基数/差分两趟） |
| `mulverify.py` | 用两份 minidump 核对 `BP_HandCard_C::shouldDiscard`（三条判据） |
| `pickdump.py` | 选择界面一开就把**完整卡表**打下来（挑 `pick_candidates` 的空档） |
| `pickwatch.py` | 守选择界面的**正证据**：三条判据一非零就落 JSON + 截图 |
| `pe_tools.py` | PE 解析：RVA→文件偏移、SizeOfImage、vtable 检查（`kardsmem/exes.py` 调它） |

## 像素 / 截图 / 坐标标定（找坐标用，**不**用来判身份）

| 文件 | 用途 |
|---|---|
| `crop.py` / `crop_grid.py` / `grid.py` | 裁剪放大 / 加刻度网格（人眼读坐标） |
| `find_tpl.py` | 在整帧里全屏匹配 `../ui_templates/*.png` |
| `field.py` / `field_raw.py` | 上游 OCR 的卡框坐标 × 内存槽位配对（**只作核对**） |
| `hover_probe.py` / `drag_probe.py` | 悬停/拖拽取证（"部署当回合不能动"的提示语就是这么读到的） |
| `pick_cards.py` | 从帧里量候选卡列（备用像素法；身份要读内存） |
| `release.py` / `reset_input.py` | 清掉卡住的鼠标拖拽状态 |

## 规矩

1. 新脚本写在**这里**，`import _bootstrap` 拿路径；证据/截图仍落 `reverse-data/logs|shots`。
2. 一次性探针用完 → 结论写进 `reverse-data/reports/` 与 docstring → 文件进 `../_archive/`。
3. 改动后跑：`cd D:\Kards\kards-agent && python -m kardsmem selftest`。
