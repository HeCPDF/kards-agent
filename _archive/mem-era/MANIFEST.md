# `_archive/` —— 被取代的一次性探针（2026-09-21 整理）

这些文件**没有删**：里面每一行都是当时的取证过程，报告里的结论要能追溯到它们。
但它们**不该再被日常使用** —— 功能已并入 `tools/kardsmem/`（内存侧统一工具链），
或者它们本身就是**已知坏的**（见下表）。

> 想跑归档里的脚本：`cd D:\Kards\reverse-data\tools; python _archive\<文件>.py`
> 归档内的互相 import（`fname_find` → `fname_live`）在同目录下仍然可用。

## 一、被 `kardsmem` 取代的（功能等价或更强）

| 归档文件 | 当年的用途 | 现在用 |
|---|---|---|
| `board_dump.py` | 枚举全卡 + 解密 5 条记录 | `python -m kardsmem cards --raw` |
| `card_dump.py` | 单卡明文字段 + 暴力找加密记录 | `python -m kardsmem cards --raw <UID\|card_id>` |
| `gs_dump.py` | GameState 原始字节 + stride 扫描 | `python -m kardsmem gs --full` |
| `map_keys.py` | 查 `AllCardsInBattle` 的 key 语义 | 已确认 **key = 运行时 CardID**（`cards.py` 文档 + `--raw` 里的 `map_key`） |
| `tax_check.py` | 验证 `KreditsTax_AsEnemyTarget@0x88` | `python -m kardsmem cards --raw`（字段 `kredits_tax_as_enemy_target`） |
| `row_order.py` | 纯内存的行/列位置图 | `python -m kardsmem cards --rows` |
| `hand_detail.py` | 手牌细节（费/是否需目标/类型） | `python -m kardsmem hand local` |
| `cards_by_location.py` | 按 location 分组 | `python -m kardsmem state` / `cards --rows` |
| `wait_turn.py` | 轮询等轮到自己 | `python -m kardsmem state`（轮询），或执行侧 `ops.py next` |
| `pickprobe.py` | 判断"是不是在等我选牌" | `python -m kardsmem pick`（同一批判据，另加候选聚合） |
| `pickfind2.py` | 列出屏幕上每一张卡（`ABP_BaseCard_C`） | `python -m kardsmem rendered` |
| `pickfind.py` | 在 Board/PC 上扫"元素是卡指针的 TArray" | **未命中**（思路被 `pickfind2.py` 的 actor 方案取代）；留作参考 |

## 二、已知坏的（**别拿来改，直接看 `kardsmem`**）

| 归档文件 | 坏在哪 |
|---|---|
| `fname_live.py` | ① `RVA_GNAMES = 0x090E2E28` 是**兜底常量**，不是名字池；真池 `0x0911B9C0`。② `Mem.read()` 对 `n > 0x20000` **静默返回 None**（所有大块扫描的"0 命中"负结论因此作废）。 |
| `fname_find.py` | `scan_for()` 里留了 `# placeholder` 和桩 `MemProxy`，**跑不通**；不过它的字节特征思路（chunk0 头 `1E 01 'None'`）是找到 chunk0 的那一步。 |
| `fname_blocks.py` | 硬编码 `known = 0x16D4E660000`（ASLR 后的绝对值，重启即失效）；依赖上一条的坏函数。 |
| `fname_pool.py` | 用 UE4 布局（`h >> 1`）并 deref `GNames` ⇒ 整个作废；它打印的 "module base" 其实是堆地址。 |
| `dbg_edges.py` / `dbg_hand_probes.py` | 手牌扇形**像素**标定的一轮尝试（悬停亮度判定）。结论是"不要用 `detect_left_edge` 平移布局表"（会把落点推到隔壁那张牌上）—— 结论已进 `NEXT-SESSION-CONTEXT.md` §4.3。 |

## 三、FName 这件事的最终归宿

`kardsmem/names.py` 已经是 FName 的**唯一**实现（RVA `0x0911B9C0`、`Blocks[]` 内联在 `pool+0x10`、
`block_bits=16`、无读上限）。上面 4 个 `fname_*.py` 的历史价值只剩"当初怎么错的"。

```
python -m kardsmem names            # 池探测 + name_at(0/1/2) + 实机卡牌资产名
python -m kardsmem verify           # 构建指纹 + 定位链 + 与规格 JSON 的一致性
```
