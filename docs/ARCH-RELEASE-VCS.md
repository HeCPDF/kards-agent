# 架构调整：运行时扫描 RVA，去掉版本表（为 release / VCS 服务）

> 2026-10-03 用户定调：**调用者指定版本不合理；RVA 可以在运行时花时间扫描并生成。**

## 1. 问题
* 现状：`build.py` 的 `BUILDS`/`RVA_FALLBACK`、`build_tables.json`、`kardsmem.board._BUILD_TABLE`、`KARDS_BUILD`、
  `version.VERSION_TO_BUILD` —— 全是"版本 → RVA"的**手工登记**，三处各抄一份，新版本一来就要改代码、改数据、重新提交。
* 对 release：发布物里夹着只对某几个版本有效的表；对 VCS：每个游戏更新都产生一次"登记表"提交，三份表还会漂。
* 调用者（脚本/GUI/CLI）不该知道也不该选版本：游戏在跑，它自己就是真值。

## 2. 目标形态
```
游戏进程 ──(只读)──► version.py  读 ProjectVersion（身份，仅用作缓存键/日志）
        └─(只读)──► rvascan.py   按结构形状扫出 GObjects / FNamePool / GWorld / ProcessEvent
                                  │
                      缓存 %LOCALAPPDATA%\kards-agent\rva-cache\<版本>.json（不进 VCS）
                                  ▼
                       build.RVA（运行时才解析；没有游戏 ⇒ 访问时报 NoTarget，而不是 import 时就炸）
```
* **不再有**：`KARDS_BUILD`、`VERSION_TO_BUILD`、`build_tables.json`、`kardsmem.board._BUILD_TABLE`、`RVA_FALLBACK`、`BUILDS`（选择用途）。
* **留下的只有回归基准**：`tests/fixtures/golden_rva.json`（旧表里 SDK dump + IDA 得到的真值），只被测试和 `exes`/`buildsrc`
  这类离线工具读，**不参与运行时选择**。扫描结果必须与基准逐项一致才算 scanner 可信。
* 结构体字段偏移（`OFF_*`）不在这次范围：它们来自 SDK dump / 反射，另议。

## 3. 扫描判据（版本无关的"形状"，见 `kardsmem/rvascan.py` 头注释）
| 名字 | 形状 |
|---|---|
| `FNamePool` | `.data` 里 `P+0x10`（Blocks[0]）指向 64 KiB 对齐块、块首条目 = `None`（len=4、非宽）；`P+8/P+0xC` 数值合理 |
| `GObjects` | `FUObjectArray` 头：Max ≥ Num ≥ 1000、NumChunks = ceil(Num/65536)；前 16 个对象 ≥8 个 vtable 落模块内、ClassPrivate 是用户态指针 |
| `GWorld` | GObjects 里类名 `World` 的实例（排除 `Default__World`）；`.data` 里**恰好一个**槽存着它 |
| `ProcessEvent` | 采样对象 vtable[0x4C]，≥90% 指向同一函数 |
任何一项"找不到/不唯一" ⇒ 抛 `ScanError`，**不猜**。

## 4. 缓存与复验
键 = 游戏自报版本（`ProjectVersion`）+ 镜像大小（防同版本号不同二进制）；加载后毫秒级复验
（`None` 在池首、GObjects 头自洽、GWorld 槽指向 World），不过就重扫（约几秒~十几秒，只发生在首次/换版本）。

## 5. 分阶段
| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | `rvascan.py` + 离线测试（合成内存） | **已做**（`test_rvascan.py`） |
| S2 | 对**真游戏**扫描，与 `golden_rva.json` 三个版本逐项对（需要游戏在跑；launcher 1.60 先） | 待游戏 |
| S3 | `build.RVA` 改惰性解析；`names/objects/world/proc/ops.inject` 去掉 import 时取 RVA；删 `KARDS_BUILD`/`VERSION_TO_BUILD`/表 | 待 S2 |
| S4 | `build_tables.json`/`BUILDS` 迁成 `tests/fixtures/golden_rva.json`；`.gitignore` 缓存目录；`exes.py`/`buildsrc.py` 改读基准 | 待 S3 |
| S5 | GUI/CLI 去掉版本显示以外的一切构建逻辑；`verify` 打印"来源 cache/scan + 用时" | 待 S3 |

## 6. 风险
* 形状判据在**别的 UE 版本/游戏**上可能多解（所以要求"恰好一个"，否则报错而不是挑一个）。
* 扫描只能在游戏跑起来、数据已初始化之后做（`None` 条目、World 实例都要运行时才有）；没有游戏时 `RVA` 不可用——
  离线工具必须显式用基准文件。
* `scan_process` 目前为每个对象读类名找 `World`，**耗时未实测**（S2 要测并按需优化）。

## 7. 2026-10-03 补充决定（用户）
### 7.1 RVA：随包释放种子表 + 缺了就扫 + 按版本.分支持久化；**只向后兼容**
查找顺序：**用户缓存**（`%LOCALAPPDATA%\kards-agent\rva-cache\<版本>.json`）→ **随包的种子表**（已发布版本的真值，`golden_rva.json` 的子集，加载前同样 `quick_verify`）→ **扫描**（`rvascan`）→ 写回缓存。
* "只能向后兼容、不能向前兼容"：种子表只能覆盖**已经发布**的版本；未来版本我们不可能预知，所以**新版本一律走扫描**，扫描失败就明确报错，不拿旧版本的表凑合。
* 版本字符串只当缓存键/日志，**不再由调用者指定**。

### 7.2 扫描算法对齐 Dumper-7（它的算法久经考验，别自己发明）
| 项 | Dumper-7 的做法 | 我们的现状 / 要做 |
|---|---|---|
| GObjects | 先扫 `.data`，找不到再扫模块所有节；Chunked 判据：`1≤NumChunks≤0x14`、`6≤MaxChunks≤0x5FF`、`Num>0x800`、`Max>0x10000`、`Max%16==0`、`floor(Num/每块)+1==NumChunks`；再逐 4 字节探 `FUObjectItem` 大小、第 5 个对象的索引==5 | 判据已照搬（`find_gobjects_in_blob`）；**待加**：`.data` 找不到时扫所有非代码节；第 5 个对象索引校验；FUObjectItem 大小探测 |
| FNamePool | **代码路径**：找 `lea rcx,[rip+X]; call`（`48 8D 0D ?? ?? ?? ?? E8`）→ 80 字节内有 `InitializeSRWLock` 调用 → 672 字节内引用字符串 `"ByteProperty"` → 解析 `lea` 得 pool；再用 `None` 与 `/Script/CoreUObject` 条目判 FNameEntry 头 2/4 字节、`FNameBlockOffsetBits`(~14/16) | 我们是**数据路径**（`.data` 里 Blocks[0] 指向首条 `None`）；**待加**：代码路径作为交叉验证/兜底，`/Script/CoreUObject` 条目校验块位数 |
| GWorld | 遍历 GObjects 找 UWorld 实例 → 在内存里找存着该地址的对齐指针；多个结果用"Sleep 后再比"的时间启发式过滤 | 一致；**待加**：多槽时的过滤（建议：优先被 `.text` 里 RIP 相对引用最多的槽，或 Sleep 后值仍等于当前 World 的槽） |
| ProcessEvent | 扫 vtable 各槽函数里的 `F7 ?? <FunctionFlags偏移> 00 00 00 00 04 00 00`（FUNC_Native 0x400 检测）找下标；兜底：字符串 `L"Accessed None"` 后的下一个函数 | 我们现在**采样 vtable[0x4C]**；**待加**：按 Dumper-7 的字节模式确认 0x4C，别把下标当常量 |
Dumper-7 源码在线（github.com/Encryqed/Dumper-7，`Dumper/Engine/Private/Unreal/{ObjectArray,NameArray}.cpp`、`OffsetFinder/Offsets.cpp`）；本机只有它的 `Dumper-7.dll` 与产物。

### 7.3 游戏机制随版本出现 ⇒ 代码要按**能力**分支，不按版本号硬编码
实例（用户 2026-10-03）：
* **洗牌钩子**（`OnDeckShuffled`，SABAE REGIMENT 这类）是 **Naval Warfare（海战）** 版本才有；
* **Bond（协力）整套机制**（含"手里带 Bond 的牌回合开始扣总部士气"）是 **Homefront（国土阵线）** 版本才有；
* 更早只有"**疲劳伤害**"一种说法，且**只在无牌可抽时抽牌才触发**；Bond 带来的是第二种来源（`ApplyFatigueDamage(fromBond)`）。
⇒ 评估/模拟（`sim/`、`boardeval`、`triggers`）里凡是"某机制可能不存在"的规则，都走**能力探测**：问游戏自己（反射：BP 函数/形参/标签是否存在，如 `ShuffleDeckBySide` 的 `skipSubAction`、`ability.bond`、
`ApplyFatigueDamage` 的 `fromBond`），缺了就按**旧规则**跑。能力表集中在 `kardsmem/capabilities.py`（待建），每项写明"哪个版本引入、判据是什么"；
版本号只作文档，不作判据（版本号会随渠道/分支漂）。

### 7.4 一步行动 = 含中间三选一；预报天然拆成 9 个行动
用户的看法：**一步行动包括处理中间的三选一**，eval 必须考虑"选哪个"；预报（两层、每层 3 选 1）天然是 **9 个行动**。
现状：打出预报牌 = 1 个行动（效果用 `outcomes_mode="max"` 取 9 路最好），选哪个是**事后**在 `choose_pick` 里用 `fc_best_type` 的 9 路前瞻重新算——决策与执行是两次独立计算，可能不一致。
目标：`gen_actions` 把预报牌展开成 9 个行动 `(card, pick1, pick2)`，各自带自己的分支效果去 eval；选中后把 `(pick1, pick2)` 作为**计划**交给执行侧，`choose_pick` 照计划点（计划落空/随机偏离 ⇒ 重算并记 meta）。其它"打出后弹三选一"的牌同理（行动里带选项）。

### 7.5 总原则（用户 2026-10-03）：**行动 = 动作 + 其后的全部选择**
抉择牌：打出 + 选第几个；需要选目标/选手牌的牌：打出 + 之后选什么；预报：打出 + 两层选择（9 个行动）；autoplay 的三选一同理。
行动空间里的一个元素是一条**选择路径** `A(kind, card, target, path)`；eval 对每条路径带它自己的分支效果打分；执行侧照路径点；
选择节点的来源统一走 VM（choice 节点、`GetChooseSpawnCards` 候选、合法性问游戏），autoplay 的"待生效选择"进 `sim` 事件队列在下回合开始结算。
这样 §7.3（能力探测）也顺：游戏版本里没有的机制不会产生对应路径，不需要版本判断。

### 7.6 向前 / 向后兼容策略
**定义**：向后兼容 = 支持**更旧**的游戏版本（机制可能不存在、规则可能不同）；向前兼容 = 面对**更新**的版本（出现我们没见过的机制/改过的语义）——后者**不可能保证正确，只能做到"识别出来、安全降级、不编造"**。

| 机制 | 管什么 | 做法 |
|---|---|---|
| A. 能力探测（§7.3） | 旧版本：机制**不存在** | 反射问游戏：函数/形参/标签在不在；不在 ⇒ 走旧规则。行动路径（§7.5）由 VM 节点枚举产生，机制不存在就根本不产生对应路径，不用版本判断 |
| B. **语义指纹** | 新旧版本：机制存在但**改了行为** | 每条我们转写的规则（`sim/effects.py` 里 MakeVeteran/FullyHealCard/ChangeUnitOwnership/MakeCardsFight/ConvertCard…，以及各钩子 runner）登记它依据的 BP 函数 + 转写当时的**字节码哈希**（`kismet.script_of` 读出，版本无关）。运行时对同名函数重算哈希：一致 ⇒ 规则有效；不一致 ⇒ 该规则**降级为缺口**（`gaps`，不编造），并在日志/面板标"规则可能过期"。旧版本若有不同哈希，可登记**变体**（`variants[哈希] = 实现`）——按证据选，不按版本号 |
| C. 未知 ⇒ 降级不编造 | 新版本：**没见过**的原语/函数/卡 | 沿用缺口协议：VM 停在未实现原语 ⇒ 该效果记缺口、价值 0；选择路径生成器遇到枚举不了的节点 ⇒ 不展开（退回"只打出，选择交给游戏默认/事后保守选"）。"判据负责挑，游戏负责判"不变：动作照发，由游戏裁决 |
| D. 版本验证等级 | 提醒人 | `version.py` 读出的版本若**高于**最近一次"验证通过"的版本（`tests/fixtures/validated.json` 记录：版本、日期、哪些扫描/回归通过）⇒ 面板显示"未验证版本"，自动对局需要显式确认；扫描出的 RVA 仍可用，但规则只信指纹一致的那部分 |
| E. 新版本到来的流程 | 运维 | ① 扫 RVA（自动）→ ② 跑静态预测工具（`probe_deck_gap_forecast2.py`）列出未实现原语 → ③ 指纹比对列出"语义变了的规则" → ④ 人工看这两张清单、补实现/登记变体 → ⑤ 更新 `validated.json` |

**底线**：新版本上，所有我们**不确定**的地方都表现为"缺口"，缺口只会让评估变保守，不会让它凭空编出收益；动作最终由游戏裁决，信息流等价的红线不受影响。

### 7.7 补充决定（用户 2026-10-03，后）
* **选择路径适用于所有行动**，不只是"打出"：移动、攻击也会触发钩子而弹出三选一（例："攻击敌方总部时预报"）。凡是一个行动在其钩子链里会产生交互，就把"该行动 + 之后每个选择"合并成**一个决策**（一条路径）。攻击/移动的钩子链（`run_attack_hooks`、`run_move_frontline` 等）要把选择节点记下来，和打出牌的 VM 选择节点走同一套枚举。
* **分支预算：每个决策最多 24 条末端路径 + 剪枝**（作者自定）：展开时先用便宜的粗评分给所有路径排序，超过 24 条就剪掉最低分的；同一张牌的路径在进入束搜索前先按粗评分去重/剪枝，避免挤占束（束宽不动）。
* **兼容策略从简**（取代 §7.6 的"严格"版）：向前/向后兼容**非常克制**，**不要求为此写测试、也不要求测试通过**。保留的只有：缺了机制按旧规则走（能探测就探测，不强求）；未知一律走缺口；语义指纹、验证等级、变体登记**降为可选**，需要时再做。
* **区分两种"选择起点"**：
  1. 行动引发的选择：起点是我们的行动（打出/移动/攻击）——进选择路径，搜索时就定好、执行照点；
  2. **系统引发的选择**：起点不是我们的行动——例如 autoplay 牌在**回合开始抽到**，自动触发并需要交互。这种是"强制决策节点"：到了才有，来不及在搜索里预定；弹出时用同一套枚举（VM 分支 + 评估取最好）当场选。模拟里抽牌链遇到 autoplay 牌时，也应作为一个我们自己做的选择节点（取最好）。
* **autoplay 的"待生效选择"到底指什么**（澄清我之前的措辞）：预报链里，第二层选中后牌**不进手牌**，只写进 `weatherCardChosen` 变量（弯路 #26）；要到**下个回合开始**时 `OnStartOfTurn` 才把它 spawn 进手牌。那个时刻**没有交互**，只是延迟的获得——所以评估预报牌的价值时，这张牌的收益是"下回合开始才到手"，不是立刻。它与上一条的"回合开始抽到 autoplay 牌"是两回事。

### 7.8 更正与补充（用户 2026-10-03，最后）
* **关于 §7.7 的第 2 类"系统引发的选择"（再次更正，用户补充）**：需要交互的 autoplay 牌**目前只有三张中立指令**（`card_event_sunny1_blue_sky` / `rain1_mist` / `storm1_gale` = 预报第一阶段的三张），**按规则不能出现在卡组里、也不能在回合开始触发**。
  但是：① **"回合开始时预报"这类情况可能存在**（预报本身由很多牌的效果触发：`COASTWATCHERS`/`RAAF WALRUS` 的部署、`TASK FORCE 44`、`CRUISER SCOUTS`，换成 `OnStartOfTurn` 触发就是回合开始预报）；② **未来可能新增能置入卡组的 autoplay 牌**。
  ⇒ "系统引发的强制决策节点"这个设计**保留**（通用机制，不为单卡写特例）；现在只是**很少触发**，不为它优先投入。预报的触发源（部署/指令/钩子/回合开始）都走同一套"行动 + 选择路径"或"强制决策节点"。
* **真正要考虑的回合开始操作**：带交互的 `OnStartOfTurn`，例如 **PBY CATALINA**（美国，Breakthrough；`card_unit_pby_catalina`）："回合开始抽 2 张，然后把手里一张放回牌库顶"。BP 链：`OnStartOfTurn`（在场且己方回合）→ `DrawCardsFromDeckBySide(…,2,…)` → `selectTargetFromHand`（弹出选手牌）→ `OnHandTargetSelected` → `MoveCardToTopOfOwnersDeck(选中的牌)`。
  这是**强制决策节点**（起点是回合开始，不是我们的行动）：用同一套枚举——对每张可选手牌，用 VM 跑 `OnHandTargetSelected` 得到效果、交评估取最好。
  它也影响搜索：放回牌库顶的那张就是**下回合确定会抽到**的牌（牌库顶已知），评估牌库/抽牌预测时要用。
* **现状里的真问题**：`rule.choose_hand_target` 现在是 `max(legal, key=self.worth)`——**永远选"最值钱"的那张**。对"放回牌库顶"（PBY CATALINA、175th INFANTRY REGIMENT）这类提示是**反的**（应该放"现在最没用/最想再抽一次"的）。要改成：按提示对应的 `OnHandTargetSelected` 效果逐张评估（效果带 `to_deck` ⇒ 放回顶；评估"手里少这张 + 下回合必抽到它"）；拿不到效果才退回旧启发式。

### 7.9 系统引发的决策：两种拉起方式（用户 2026-10-03）
回合开始（或任何非我方行动的时刻）弹出交互，来源只有两种，都进"强制决策节点"：
1. **`OnStartOfTurn` 钩子拉起**：在场的牌在回合开始触发效果，效果里要交互（PBY CATALINA 的抽 2 放 1；回合开始预报）。
2. **autoplay 拉起**：回合开始（或任何时候）**抽到/进手牌**的牌若带 `autoplay`，**进手牌就跑 `OnPlayedFromHand`**——抽牌这个动作本身会把"打出"带出来，从而弹交互。现在只有三张中立预报指令带这种交互且不可入组，但机制是通用的，未来可入组的 autoplay 牌会走这条。
共同点：起点不是我们的行动 ⇒ 搜索里无法预定，弹出时当场枚举（VM 分支 / 手牌目标候选）+ 评估取最好；`sim` 的抽牌链（`draw_chain`）遇到 autoplay 牌时同样要产生"我们自己做的选择节点"（取最好），而不是只记效果。

### 7.10 autoplay：**按原版**——是"待打出队列"，不是"进手牌钩子"（用户 2026-10-03 更正，BP 已读）
效果上可类比"进手牌就跑 PlayedFromHand"，但**游戏的实际路径不同**，sim 必须照原版：
* **入队**：`BP_CardFunctions::DrawTopCardFromDeck`（@13057 附近）抽到带 `autoplay` 标签的牌、`CreateCard`（@20119/20155，`spawnCardInHand` 且牌在手牌里）塞进手牌的 autoplay 牌 ⇒ `GameState->AddAutoPlayCards(cardID)`——只是**进一个队列**，此刻不打出。
* **出队（在动作边界上冲刷）**：`BP_OnlineMatch::ExecuteAutoPlayCards`（@26726）与 `BP_PlayerMoves` 里每种玩家动作处理完之后（@389/409/454/494/613/673/776 共 7 处）以及移动队列处理的收尾（@844–893）：对队列里每张牌：`CreateAction_PlayCardFromHand_Start(card, 0x8, 0, 0)` → `PlayCardFromHand(card, instigator=自己, 0,0,0, true)` → `CreateAction_PlayCardFromHand_End`，最后 `ClearAutoPlayCards()`。
* **打出时不付费**：`PlayCardFromHand`（@18446）里带 `autoplay` 标签的牌**跳过 `PayCardCost`**。
* 所以 autoplay 牌是**在"抽到/塞进手牌"之后的下一个动作边界**作为一次独立的"打出"动作被自动打出，它引发的交互发生在那个边界上。
sim 的建模：`sim` 状态里加一个 `autoplay_queue`；抽牌/塞牌来源（`draw_chain`、`SpawnCardInHandBySide`…）遇到 `autoplay` 牌 ⇒ **入队**；在"行动处理完"这个点（每个模拟行动之后、回合开始抽牌之后）**冲刷队列**——对每张牌跑一次免费的 `OnPlayedFromHand`（其交互按 §7.5/§7.9 处理），再清空。
