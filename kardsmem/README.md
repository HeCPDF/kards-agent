# `kardsmem` —— KARDS 内存侧（只读）工具链

> 这是 `D:\Kards\reverse-data\tools\` 里**唯一**该用的内存读取入口。
> 以前散在 18 个一次性探针里的逻辑已并入本包，旧文件移到 `tools/_archive/`
> （见 `_archive/MANIFEST.md` 的"旧 → 新"对照）。

## 红线（用户给的权威前提）

**只读内存 + 模拟鼠标。** 本包只做前半句，且只用：

```
OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ) + ReadProcessMemory
```

**没有**注入、`WriteProcessMemory`、远程线程、hook。执行侧（鼠标）不在本包内，
见 `tools/ops.py` / `tools/ctl.py`。

## 60 秒上手

```python
from kardsmem import attach

s = attach()                       # 校验构建指纹；不匹配直接抛 BuildMismatch
st = s.snapshot()                  # 归一化盘面（board_api mem 后端）
print(st.turn, len(st.hand("local")), st.hq["local"].defense)
print(s.deck("local")[:3])         # 物理牌库（含同名多份）
print(s.gs().kredits())            # 指挥点/槽位（原子读 + 解密）
print(s.names_pool().card_asset_name(st.cards[0].raw["ptr"]))   # FName 资产名
print(s.rendered()[:3])            # 屏幕上摆着的每一张卡（不读图）
print(s.pick())                    # 是不是在等我选牌
s.close()
```

一样的东西走 CLI：

```powershell
cd D:\Kards\reverse-data\tools
python -m kardsmem selftest      # 离线断言（含 board_api 的 34 项）+ 实机探测
python -m kardsmem verify        # pid/基址/md5 + 定位链 + 与规格 JSON 的一致性
python -m kardsmem exes          # ★ 本机每份 exe 的指纹 + 私服 URL 补丁状态（"为什么对不上"）
python -m kardsmem state         # 盘面：手牌/场上/弃牌/牌库/HQ/指挥点/回合
python -m kardsmem cards --raw   # 每张卡的**全部原始字段**（含效果文本、加密当前值）
python -m kardsmem deck local    # 物理牌库（同名牌多份在这里才看得见）
python -m kardsmem names         # FName 池 + 实机卡牌资产名
python -m kardsmem effects [ID]  # ★ 逐实例"被贴的效果"（实时，不是模板值）
python -m kardsmem rendered      # 屏幕上摆着的每一张卡（不读图）
python -m kardsmem pick          # 是不是在等我选牌（抉择/预报/部署后选目标）
python -m kardsmem dump --out D:\Kards\reverse-data\logs\snapshot.json
```

命令全表见 `python -m kardsmem --help`；每个子命令都支持 `--json`。


## 分层（每层只依赖下一层）

| 模块 | 行数 | 职责 |
|---|---|---|
| `build.py` | 249 | **唯一真源**：多份同名 exe 的指纹 + 本/旧 build 的 RVA 表 + `kards-offsets.json` 一致性校验 |
| `proc.py` | 319 | 只读接入：进程/模块枚举、`Session`、`MemRO`（定长读 / **原子读** / u16,u64 / hexdump） |
| `world.py` | 189 | `GWorld → UWorld / GameState / Board / PlayerController / Level Actors`（全靠 `PropertiesSize` 认类） |
| `gs.py` | 250 | GameState 字段 + 侧值块原子解密 + **物理牌库 id** + 静态卡表 + 重连表 |
| `names.py` | 874 | FNamePool（RVA `0x0911B9C0`）→ 字符串；FText 明文回退 |
| `cards.py` | 428 | 盘面卡：`AllCardsInBattle`（24B 步长）枚举 + 全部字段原始读数 + 加密解密 |
| `rendered.py` | 503 | `ABP_BaseCard_C` 家族 actor：**屏幕上摆着的每一张卡**（不读图） |
| `pick.py` | 334 | 选择界面状态（`chooseOneActive` 等）+ 候选聚合 |
| `snapshot.py` | 192 | 全部聚合成一份 JSON 快照（任何一层失败只进 `notes`，不炸整体） |
| `exes.py` | 250 | 本机每份 `kards-Win64-Shipping.exe` 的指纹 + **私服 URL 补丁状态**（"为什么 md5 对不上"） |
| `cli.py` | 500 | `python -m kardsmem <cmd>` |

（合计 ~4,150 行 / 174 KB。）

**为什么底层不重复实现**：`OCR-Kards-Auto/src/board_api.py` 是
`OpenProcess/ReadProcessMemory` 和"归一化盘面（`Card`/`BoardState`，mem/ocr 双后端）"
的**唯一**实现在。本包继承/调用它，只补它缺的几层（FName、屏幕卡、选择界面、牌库）。
`kardsmem selftest` 的 B 段会断言两边的常量仍然一致，防止两层再次漂移。

## ★ exe 身份：md5 对不上 ≠ 构建对不上

本机有 **5 个** 名字含 `shipping` 的 exe 文件（3 棵安装树 + 1 个 `.orig-backup`
+ `kards-Win64-Shipping2.exe`），其中 **2 份被私服 URL 补丁**改过
（`D:\Kards\server\tools\patch_exe.py`：把 `https://kards.live.1939api.com[/config]`
换成 `http://127.0.0.1:5231/` + NUL 填充）。

| 判据 | 说明什么 | 用途 |
|---|---|---|
| **SizeOfImage**（PE 头 = 进程 toolhelp 的 module size） | 是哪份**构建** | **决定偏移表能不能用**；URL 补丁不动节、不改代码 ⇒ **RVA 全不变** |
| **md5** | 字节级是否一致 | 只说明"有没有被动过"（补丁版 md5 必然不同） |
| **URL 字面量 / `.orig-backup` / 同目录同大小的未补丁孪生** | 是否补丁版 / 原件指纹 | `python -m kardsmem exes` |

`Session.attach()` 因此**放行**"同构建 + URL 补丁版"（`info.patched=True`，检查项里记 `url_patched`），
但**另一个构建仍然拒绝**（抛 `BuildMismatch`）。完整指纹表见
`reverse-data/reports/EXE-IDENTITY.md`。

> ⚠ **枚举 exe 别用精确文件名**：本模块早先用 `if "kards-Win64-Shipping.exe" in name` 过滤，
> **整个漏掉了 `kards-Win64-Shipping2.exe`**（default 树里那份补丁版）。现在按"名字含 `shipping`"
> 子串匹配，并支持 `.orig-backup`/`.bak`；`python -m kardsmem exes --all` 会把目录里其它 `.exe` 也列出来。

## 本包补上的能力（相对 board_api）

| 能力 | 位置 | 证据 |
|---|---|---|
| **FName（R5）** | `names.py` | 池在 `base+0x0911B9C0`；实测解析出 `card_unit_104th_infantry_regiment` / `card_unit_p40_warhawk` / `card_event_usace` |
| **卡牌效果文本（R6）** | `cards.read_raw()["text"]` | 实测 `THE ANZAC SPIRIT` → `"Deal 2 damage to all enemies. Draw 2 cards. Gain 2 extra kredit slots."` |
| **屏幕上每张卡（R3）** | `rendered.py` | 结算页 16 张屏幕卡 16/16 与盘面 `card_id` 匹配（手牌 11 + 场上 5） |
| **选择界面判据** | `pick.py` | `ABP_Board_C+0x0AC9` / `+0x0C21`、`ABP_PlayerController_C+0x9B4/0x9B8/0x9C8` |
| **物理牌库（同名多份）** | `cards.deck_cards()` | `DeckCardIDs_Left/Right` 实测是 `TArray<int32>`＝运行时 CardID，22/22、23/23 全部命中；`15th ENGINEERS`×2、`USS YORKTOWN`×2、`GUNSHIP MISSION`×2 |
| **可指向目标（R8，近似）** | `cards.target_candidates()` | ⚠ 只把读得到的判据（位置/压制/税）用上；权威谓词在 exe 的 `0x4A7F910` 等 |

## ★ 实时效果：每张卡**此刻被贴了什么**（不是模板值）

模板字段看不出"实际税"：`KreditsTax_AsEnemyTarget@0x88` 只是卡面基数。
游戏用 **`cardFunction->AddKreditsTax(卡, ±N, 给效果的卡ID)`** 把税"贴"到别的卡上，
并同时 `CustomAbilityAdd("passive", 卡, 给效果的卡ID)` 记录来源。
（实证：`card_event_order_of_the_day`（芬兰「动员令」ORDER OF THE DAY，3 费）给两张 SISSI 各贴 +1 税；
`card_event_grim_day` 给全体贴 +2 并在结束时 `AddKreditsTax(卡, -2)` + `CustomAbilityRemove` 撤销。）

这些都在 `UBaseCardObject` 的**逐实例容器**里，布局来自 Dumper-7 SDK：

| 偏移 | 类型 | 含义 |
|---|---|---|
| `0x00B8` | `TMap<int32, FCardBuffData>` | **`buffsFromCards`**：谁给这张卡贴了什么（元素 0x70：key=给者 CardID；值 `{FText cardName; TMap<FString,int32> BuffMap}`） |
| `0x0108` | `TArray<int32>` | `cardsBuffedByThisCard`：这张卡贴过谁 |
| `0x0208` | `TMap<FString, FCardsGivingAbility>` | **`receivedAbilitiesFromCards`**：贴过来的"能力"（元素 0x28：key=能力名如 `"passive"`；值 `{TArray<int32> CardsGivingAbility}`） |
| `0x0504`/`0x050C` | `FName` | `customName1/2`（另一套"贴标记"：`CustomName1Add/HasAttribute/Remove`） |
| `0x0518` | `FBlueprintJsonObject` | `customJson`（`JSON_Get/Set/AddTo*Array` 的逐实例簿记）⚠ **不是 FText**，以前当文本读是错的 |

```powershell
python -m kardsmem effects              # 列出所有带实时效果的卡
python -m kardsmem effects <CardID|UID> # 单卡：谁贴的、贴了什么、实际税估计
```

> ★ **被抑制（Suppressed）**—— **百科原文**：「被抑制的卡牌**失去所有关键词和效果**，
> 同时**重置其攻击力、最大防御力以及行动花费**」。实机 A/B 证据（2026-09-21，
> **同一张 CHURCHILL Mk IV 的两个实例**：场上被抑制 vs 牌库里未抑制）：
>
> | 读到的 | id=74（场上，**被抑制**） | id=73（牌库，未抑制） |
> |---|---|---|
> | `is_suppressed` @0x33A | **True** | False |
> | `keywords` / `keyword_flags` | **`[]` / `{}`** ← `has_guard` 被**清零** | `['has_guard']` / `{has_guard:1}` |
> | `buffs_from_cards` | **`← giver=27 DURESS {'Suppress': 0}`** | 无 |
> | a/d / cost | 2/7 / 5（不变） | 2/7 / 5 |
>
> ⇒ **抑制是"字段级"的**：`is_suppressed=1` **且模板关键词全部被清 0**
> （所以按 `keywords` 判 guard/blitz/… 会自然正确，但要知道**被抑制的卡读出来就是"没有关键词"**）；
> 来源会留一条 `buffsFromCards[giver] = {cardName, {'Suppress': 0}}` —— 值是 **0**，
> 说明 `Suppress` 是**标记型**条目（存在即"被 X 抑制过"），**不要拿它的数值当强度**。
> `effects_active` 因此为 False：算实际指向税/buff 时必须先看它。
> （另一批 `BuffMap {}` + giver `-1` 的条目出现在**压制/pin** 与过期 buff 上，**不是**抑制的特征 —— 别混。）
>
> ⚠ **压制（pin）是另一回事**（百科）：「被压制的单位**不能移动或攻击**，于所有者下个回合结束时移除」。
> 内存里记为 `receivedAbilitiesFromCards['pinned']` + `buffsFromCards[…]['combat_pinned']`；
> `ops.py` 的 `act_attack/act_move` 会先查它并拒绝。规则全文见 `reports/KARDS-RULES-ENCYCLOPEDIA.md`。

**指向判据另有一个进程外工具**：`tools/card_targets.py`（把 20 个 `IsValidHandTarget` 蓝图覆写
搬出进程执行，已 18/20 可跑）。见 `reports/TARGETING-EXTERNAL-EVAL.md`。

## ★ 卡级限制与「能不能打 / 能不能动」（用户逐条补充，2026-09-21）

游戏里有**卡牌级**的"不能做某事"标记，落点三处（本包都能读）：
① 卡面 `FName customName1/2`（静态）② `receivedAbilitiesFromCards`（运行时 `CustomAbilityAdd`）
③ `buffsFromCards` 的键。

| 标记 | 含义 | 导出出现次数 |
|---|---|---|
| `cantAttack` | 不能攻击 | 3 |
| `cantAttack:location` | **不能攻击总部** | 12 |
| `cantMove` | **不能移动** | 7 |
| `cantRetreat` | 不能撤退 | 37 |
| `cantBePinned` / `cant_be_pinned` | 不能被压制 | 14 + 4 |
| `cantBeSuppressed` | 不能被抑制 | 12 |
| `cantBeAttackedBy:ground` | 不能被地面单位攻击 | 6 |
| `cantBeTargetedByEnemyOrder` | 不能被敌方指令指定 | 8 |
| `ignoreCantAttack_location` / `canTargetCovert` | 无视"不能打总部" / 可指定隐蔽单位 | 11 / 5 |

原生判据：`HasCantAttack` / `HasCantAttackType("air"|"ground"|<类型>)` / `HasCantBeAttackedBy(unitType)`；
wrapper = `Content\Library\cardsCheckFunctions.cpp::CanAttack`。

| API | 回答 |
|---|---|
| `cards.restriction_flags(eff)` | 这张卡身上有哪些 `cant*` 标记（长标记优先） |
| `cards.action_blockers(session, card, turn)` | **能不能攻击/移动 + 理由**：部署当回合（`enterPlayOnTurn`+`has_blitz`）→ 压制(pin) → 卡级限制 |
| `cards.target_blockers(session, defender, attacker_type=, attacker_ptr=)` | **能不能打这个目标**：烟幕 / 隐蔽 / `cantBeAttackedBy` / **免疫(damage_blocked)** |
| `cards.read_cards_giving_immunity(session, ptr)` | 谁给了免疫（`cardsGivingImmunity@0x290`） |

★ **防御侧三条**（百科原文，`target_blockers` 已实现）：
- **烟幕**：`has_smokescreen` ⇒ 「烟幕单位无法被敌方单位攻击」（它**移动/攻击后**才失去烟幕）
- **隐蔽**：揭示前不能被指定，除非攻击方带 `canTargetCovert`
- **免疫**：`isImmune@0x289` + `cardsGivingImmunity@0x290` + `immune` buff ⇒ **可打但不掉血**
  —— `target_blockers()['damage_blocked']`，`ops.py` 会打 ⚠ 警告，**别把"没掉血"当失败**
- ⚠ **被抑制时卡级限制一并失效**（抑制＝失去所有关键词和效果）；被抑制的目标反而**放行**

## 屏幕卡的两个子类（本次新查到的位置判据）

`ABP_BaseCard_C` 家族（`find_actors(0x830)` 一个不漏）按类大小分成：

| 类 | PropertiesSize | 实测对应 |
|---|---|---|
| `BP_HandCard_C` | `0x0E38` (3640) | 手牌：11/11 全在 `hand` |
| `BP_BoardCard_C` | `0x0D30` (3376) | 场上/总部：5/5 全在 `back`/`frontline`/`hq` |

`rendered_cards()` 每张都带 `class_name`，可以据此决定点击坐标该走"手牌扇形"还是"场上"。

## 已知坑（本包内部已处理，外部调用要小心）

1. **必须原子读**：`gs+0x330..0x390`（X/Y 每帧重随机化）与 `card+0x568..0x5E0`（5 条记录）。
   用 `MemRO.atomic()`；分字段读会撕裂。
2. **`AllCardsInBattle` 元素 24 字节**（不是 16），指针在 `+0x08`；`ArrayNum` 含墓碑。
3. **`board_api._Mem.ptr()` 把"值 == 0"也返回 `None`** ⇒ 分不清"空数组"和"读不出"。
   要区分就用 `read_exact(a, 8)` + 自己解包（`pick.py` 对 `ChooseOneCardTargets` 就是这么做的）。
4. **明文 `attack@0x6C / defense@0x74 / kredits@0x80` 是模板值**，当前值只在加密记录里。
5. **`movementLeft@0x268 / attackLeft@0x26C` 的语义（用户 2026-09-21 权威）**：**把 Fury(奋战) 算在内**（奋战单位=2），**但不含 Blitz** ⇒ 不能用它们判「当回合能不能动」；判据是 `enterPlayOnTurn@0x270` + **`has_blitz@0x1DA`**（闪击要自己读，计数器里没有）。计数器仍可用于「还剩几次」。**另：移动单向（支援阵线 → 前线）**，把前线单位往支援阵线拖会被拒（百科「撤退」条；SDK 只有 `OnMoveToFrontline`）。
   权威判据是 `enterPlayOnTurn@0x270` vs `BoardState.turn`（闪击例外）→ `cards.can_act_now()`。
6. **`.idmap` 的 `*_VFT` 不能用来比实例 vptr** ⇒ 一律用 `PropertiesSize` 沿 `SuperStruct` 认类。
7. **多份同名 exe**：`Session` 默认校验 md5 + SizeOfImage；**md5 不同但 SizeOfImage 相同**时再探测私服 URL 补丁，确认后放行（`patched=True`）；只有 SizeOfImage 不同才抛
   `BuildMismatch`，**不要**绕过它读偏移（读出来全是垃圾）。诊断用 `python -m kardsmem procs`。
8. `locations 5/6` = **整个后排**（HQ + 支援线），HQ 是 `Type == 1` 的那张。
9. 弃牌堆里阵亡单位的加密 `defense` 是**负值**。

## 环境

- Python 3.14（`mss/cv2/numpy/pywin32` 有，**没有** `rapidocr_onnxruntime`）。
- `KARDS_SRC` 环境变量可覆盖 `board_api.py` 所在目录（默认
  `D:\Kards\OCR-Kards-Auto\src`）；`KARDS_WORKSPACE` 覆盖工作区根。
- 游戏/Steam 若以管理员运行，非管理员进程连只读句柄都拿不到（`OpenProcess` 失败）。
