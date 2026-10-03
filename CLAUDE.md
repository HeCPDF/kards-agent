# KARDS 自动化框架 —— 给接手的 agent

## 红线（用户给的权威前提，不要越）

★ **2026-09-25 更新，取代了下面「旧红线」**——用户明确授权放宽，理由和边界都问清楚了：

> **判据只认一条：发给服务端/对手的信息流必须跟真人用鼠标正常操作产生的完全等价。**
> 中间用什么手段实现（进程外只读内存 vs 进程内注入执行代码）不再是红线管的范围，
> 只要"网络上看到的东西"跟真人在玩没有区别。

拆成读/写两半：

- **读侧**：允许进程内注入代码做**只读查询调用**（比如直接调用游戏自己的
  `CanPlayFromHand`/`CanAttack` 蓝图函数本身，而不是在进程外用 `kardsmem/vm.py`
  重新解释一遍字节码）——前提是这些调用**没有副作用、不改变对局/游戏状态**，
  纯查询、纯读。★ 这不是"默认就该这么干"：进程外只读内存（`ReadProcessMemory`）
  依然是第一选择，注入式只读调用只在**进程外读不到、或者答案要现算才有**（比如
  字节码解释器卡在某个节点、或者某个值游戏里是调函数现算的，内存里根本没有对应
  字段）时才值得投入，不是拿来替代现有的 `kardsmem`。
- **写侧/输入侧**：允许注入代码触发输入（不再局限于物理鼠标移动+点击），但注入
  的代码必须**完整模拟真实鼠标会产生的事件序列**——先悬停再点击，悬停停留时长、
  移动轨迹这些都要有，**不能跳过悬停直接调用点击之后触发的最终动作函数**。原因
  很具体：悬停信息会被转发给对手（卡面高亮之类的可见效果），跳过悬停直接触发
  动作，服务端/对手看到的行为模式就跟真人玩家不一致了，这就不再"完全合法"。

⇒ 旧红线的理由（"客户端没有反作弊，服务端对上传数据做行为判定"）**没有变**，
变的是保证这件事的手段——原来是"不写内存/不注入"这个一刀切的机制性禁令，现在
收窄成直接检查目标本身："发出去的东西合不合法"，不再管"用什么手段查/怎么触发
输入"。

★★ **2026-09-27 更新：两条"允许做"都已经做完了，物理鼠标那套已归档**：
- **输入侧 = 进程内合成事件**（`ops/inject.py`）。它按真实鼠标的**完整事件序列**重演
  （悬停 → `MouseHoverDispatch` 转发 → 按下 → 起拖 → 拖动 tick → 落地/松开），**不跳步**、
  不挪真实光标、跟窗口焦点无关。物理鼠标实现 `ops.py` 已 `git mv` 到
  `_archive/ops_mouse.py`（**停用**），`agent/` 三个前端全部改走 `ops.inject`。
- **判据侧**：`ops.inject` 直接问游戏自己（`CanPlayFromHand`/`CanAttack`/`CanIDoAnything`
  /`CanSelectAsTarget` …）——进程外 Kismet 解释器（`kardsmem/vm.py`）降级为**兜底**，
  只在注入侧不健康/问不出来时用。**只读查询**（`can_move_to` 那类）凡是要写字段才答得出来的，
  一律**不问并如实报"问不出来"**（别把"必然 False"当判据，也别伪造拖拽态字段）。
- 进程外的 Kismet 字节码解释器（`kardsmem/vm.py`）仍保留（它是**纯**的、跨构建的兜底，
  也是 `semantics/legality.py` 的来源），但它不再是唯一/默认的判据来源。

★ 已作废的旧说法（留档）：下面曾写着"输入侧目前仍然全部走物理鼠标""新红线只是打开一扇门、
没有改变现有代码的任何行为" —— 2026-09-26/27 已全部走完（`ops.inject` 实测通过换牌/出牌/
两种指向/攻击/上线/抉择/手牌目标/结束回合/inspect 悬停，见 handoff §22.13/§22.14）。

`git tag redline-readonly-mem-sim-mouse`（提交 `d3d874a`）标记着旧红线（只读内存 +
模拟鼠标，禁止任何进程内代码执行）下的最后状态，需要对比/回退时看那个 tag。

<details>
<summary>旧红线原文（2026-09-25 前，留档不删）</summary>

**只读内存 + 模拟鼠标。**

- 只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`
- **不注入、不 WriteProcessMemory、不远程线程、不 hook、不在游戏进程里执行任何代码**
- 理由：客户端没有反作弊，服务端对上传数据做行为判定 —— **红线是「写」**

推论：蓝图字节码可以**读出来在 Python 里解释**，但求值器必须是纯的
（`EX_Let*` 只能写影子堆，绝不回写游戏内存）。

</details>

## ★ Agent 使用真鼠标/键盘与"置前窗口"的规则（2026-10-02，用户定）

**起因**：脚本连点劫持了真实鼠标，用户连给 Agent 发消息都做不到，且是停不下来的后台任务（弯路 #43）。**闸门设在 Agent 的行为上，不改代码**（用户："在 Agent 处设闸门就够了"）。
1. **默认不点击、不动真实鼠标/键盘。**
2. **只有两种情况允许 Agent 点击**：① 用户**明确说明处于无人值守**；② 用户**知情并明确允许**。其它情况一律先问。
3. **允许时也必须带 timeout**（硬超时，即使脚本自己有终止条件也要再加一层），不得无限期运行。
4. 判断标准：任何可能让用户失去对电脑控制、且不能被用户立刻停止的操作，在没有上述授权时都不做。
5. 合成输入（`ops.inject` 进程内事件）不受此限，因为不碰真实光标。
6. **把游戏窗口移到前台（置前、Alt 键技巧、`SetForegroundWindow`、`front2.py` 之类）与动真鼠标同等重要，同样受上面 1–4 条约束**：**尽量不置前**；只有"确实需要点击"或"某些必须前台的测试"时才可以，且**必须先征得用户同意**、带 timeout；用户说明无人值守或知情并允许才算同意。
7. **截图不需要前台**：用现有工具链（`shot.py`、`vendor/win.py::capture_client_bgr`，PrintWindow/抓取客户区），**不要为了截图而置前**。如果截到的图是黑屏/被遮挡，**告诉用户**，不要自作主张置前。

## 这个项目要做的四件事

1. **高层 API：从内存读游戏实时状态**（`kardsmem`）
2. **高层 API：操纵客户端**（`ops/inject.py`，进程内合成事件；旧的物理鼠标 `ops.py` 已归档）
3. （future）MCP server
4. （future）NN

框架级硬约束：**发给服务端/对手的信息流必须跟真人操作等价**（见上面红线段落）。
★ 2026-09-27：**这条已经落地** —— 执行侧 = `ops.inject`（合成事件序列，不挪真实光标），
命令层 = `agent/`（三前端共用 `AgentSession`），读侧 = `kardsmem`/`kardsmem.board`（只读）。

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
  （`ops_mouse.ROW_SPECS`，rank = 行内按 `locationNumber` 的密集名次，n = 行内卡数，**含总部**）。
  ★ 2026-09-27：这一段是**已归档的物理鼠标实现**的模型；执行侧换成合成事件后**动作不依赖屏幕坐标**
  （落点由游戏自己的出参决定）。留着它是为了 `rowcalib.py` 标定与 OCR 对账（`_archive/ops_mouse.py`）。
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
9. **假设"游戏没有这个字段"，然后自己推算**。把「被守护」当成要按相邻关系算出来的量
   （"同方同行、slot 相差 1 有守护单位"），写进了规格和代码 —— 而
   `kards_classes.hpp` 里就摆着 `bool isBeingGuarded; // 0x0288`，三个构建同偏移。
   推算值还会在"守护被某张卡临时赋予/移除"时和游戏对不上（`GiveGuard` 带 `instigatorID`，
   说明它是带来源的状态）。→ 这是第 1 条的同一个错误换了个方向：
   **不只是"查不到再动手"，是"断言某个字段不存在"之前必须先 grep。**
   顺带查出 `CARD_U8` 还漏了 9 个标记位（规格 §11.1 F3a）。
10. **heredoc 反斜杠被吞**，一个会话里踩了四次 —— 第四次就发生在往这份文件里
   写这一条的时候，反斜杠加数字被 shell 当成转义、变成了不可见控制字符。
   → **写文件一律用 Edit/Write 工具，不要用 shell heredoc 写含反斜杠的内容。**
11. **`--coverage`/`selftest` 只证明"跑得动"，不证明"答案对"。** `card_targets.py`
   的 `out()` 函数签名写错（多了个从没用过的形参），导致 `simple()` 注册的全部
   20 个原语不管卡面数据是什么都回填 `True`——`IsUnit(order卡)` 和
   `IsUnit(infantry卡)` 给的是同一个答案。工具本身的"18/20 覆盖率"指标从来
   没检测出这个问题，因为它只统计"有没有缺原语导致跑不下去"，从不比较
   "换一张不同的卡，答案会不会跟着变"。2026-09-24 加新原语时顺手用两张
   *不同* 数据的合成卡对比结果才挖出来。**教训：给一个求值器/判据写"通过率"
   之类的自检时，光让它跑完不够，要拿至少两组会导致不同结论的输入去对比，
   不然一个"永远返回同一个值"的坏实现也能拿到 100% 覆盖率。**
12. **"字典里写着读不到"不等于真的读不到。** `cardnatives.py` 的 `NOT_READABLE`
   是纯文档、不接调度，2026-09-24 一查发现里面至少 4 条理由
   （`getHasGameplayTag`/`HasCustomAbility`/`isBuffedByCard`/
   `getCardsBuffedByThisCard`/`getTotalHeavyArmor`）是没查 `kardsmem.board`/
   `cards.py` 就写的——字段早就在别处被读出来了，只是没人接进 `PRIMS`。
   和第 9 条（被守护那次）是同一类错误：**断言"没有"之前必须先 grep，
   写文档字典也不例外**。
13. **打开 IDA 库前先核对是不是同一个构建。** 为查 `GetChooseSpawnCards`
   打开了 `1.57.26586.launcher` 的 `.i64`，分析完才想起来核对——当时活着
   的进程其实是 `1.58.27125.launcher`（`python -m kardsmem exes` 一查就知道
   `SizeOfImage` 不一样）。同一个函数在两个构建里地址不一定对得上；
   按函数名搜的结论（"没有第二个同名符号"）大概率还成立，但涉及**具体地址**
   （比如"读一次虚表槽位喂给 IDA"）的结论就必须先核对构建，不能想当然。
14. **"它返回了 False，跟屏上那条提示一致" ⇒ 就断言因果。**
   `BattleUtilityFunctions_C::CanMoveCardToLocation` 在"0 指挥点"时回 `False`，
   而屏幕提示正好是"指挥点数不足" ⇒ 于是 spec 和 handoff 都写上了"**它看指挥点**"。
   2026-09-25 把它**全量反汇编**之后：**一条指挥点检查都没有**。它回 False 是因为
   它读的 `PlayerController->SelectedCard`（"当前正在拖的那张卡"，`// 0x0950`）
   在预检阶段是 `None`，第一个 `IsValid` 就不过。
   **教训：「函数返回了什么」和「它为什么返回这个」是两件事**；要断言后者，唯一的一手
   证据是读它的字节码（`_nn_scratch/dump_lib_fn.py`），或者设计一个能**区分两个假设**的
   对照实验（"有指挥点但没拖拽时它回什么？"）。单一观测 + 一个恰好吻合的解释 = 弯路。
   （和证据标准里"单一观测对得上不算证据"是同一条，这次栽在**控制流**上而不是字段语义上。）
15. **拿 `class_named()` 去 dump 函数库里的函数。** `BattleUtilityFunctions_C` 在进程里
   有**两份同名 `UClass`**，`class_named()` 给的那份自身只有 1 个 UFunction
   ⇒ dump 出来是"找不到 `CanMoveCardToLocation`"，看起来像"游戏根本没这个函数"。
   函数库（`*_C` 库）和卡类一律用 `ops.inject._bp_lib_fn(class, fn)`——它的判据是
   "**这个类自己的 UFunction 列表里有这个函数**"。同理别用 `class_named()` 验证
   "某个类有没有某个函数"。
16. **把"函数名"当成了它的行为。** `BP_targetArrowRVX_C::spectatorArrowNewTarget` ——
   名字读起来是"把箭头瞄到新目标"，于是 handoff 里写上了"它会把头部平面搬到目标卡的世界坐标"。
   2026-09-25 把它的字节码 dump 出来：**只有 4 条**（`overCardID = 参数`）——
   它**只写一个 int 字段、根本不碰几何**。箭头头部是
   `UpdateArrowTransform → ThrottledMouseLocation(GetMousePosition())` 按**真实鼠标**算的，
   我们不动真实鼠标 ⇒ 屏幕上箭头就是"没指到目标"（**用户当场看出来的**）。
   攻击真正失败的原因是**漏了"悬停目标 actor"这一跳**：真实玩家是"拖着箭头移到目标卡上方
   再松手"，目标 actor 会收到 `OnActorMouseEnter`；补上之后合法攻击**第一次就成**
   （同一对以前要试十几次，看起来像"卡住"）。
   ⇒ **函数名不是证据**；断言"这个函数干嘛"就去读它的字节码（`_nn_scratch/dump_any_fn.py`）。
   顺带一条更值钱的：**"目标"在不同链路里存在不同字段** —— 板卡攻击读
   `箭头->overCardID`（`BP_Logic::GetTargetArrowTargetCard`），手牌带目标出牌读
   `卡对象->targetOverride // 0x0548`（`BP_CardFunctions::GetTargetedCard`，
   游戏自己探测目标时也写它）。**照抄一条路的做法到另一条上会白忙。**
17. **把"松手"当成了所有出牌的通用提交口。** 2026-09-25 用户当场纠正：
   > "你发什么 EndDrag 是有问题的。部署时是 EndDrag，选择目标不是 Drag 为什么 EndDrag"

   两阶段"带目标出牌"里 `OnActorEndDrag` 只是**阶段一（拖到落点部署）**的口；
   **选目标是单击**，提交在 `BP_Logic::GlobalMouseUp` →`GlobalMouseUpBattle`：
   `PlaceHandCard(cardBeingPlayedFromHandLocNum, targetedCard->cardID)`
   （读 `箭头->overCardID` + `卡对象->targetOverride`；后者真鼠标下由
   `BP_targetArrowRVX::ReceiveTick` **每帧**写）。我先前的三种写法
   （`EndDrag`／目标 actor 的 `OnActorMouseUp`／目标 actor 的 down+up）**全在错的口上**。
   **教训**：ubergraph 里"扫这一支调了哪些函数"只能证明**这条支路存在**，不能证明
   "所有情况都走它" —— 同一个 handler 按条件分叉到不同 label（`BP_HandCard.cpp:1198-1217`：
   单位+有目标 ⇒ 只摆位+箭头，走 `Label_14483`；否则才 `Label_15772` `PlaceHandCard`）。
   要看**分叉条件**，函数名清单不是证据（同 #16）。
   还有一条元教训：**"物理鼠标能用"最容易掩盖机制理解错误** —— 物理鼠标版被用户删掉
   （"不要真实鼠标"）之后，正解才浮出来：合成点击一次就成
   （`confirm 6 66` → `XActionPlayCardFromHand[6,1,66]` + `ZActionDamageCard 66 destroyed=1`）。
18. **`__WorldContext` 传 0。**（2026-09-26 用户定调：**"大部分检测类函数都要传
   `__WorldContext`。如果它收 `__WorldContext` 就不要传 0。"**）

   `cardsCheckFunctions_C::CanAttack` 我一直把 `__WorldContext` 传 0，还在
   `game_can_select_as_target` 旁边写了句"`CanAttack` 那边传 0 是 OK 的（它指挥点是显式入参）"
   —— **没验证过，而且是错的**。症状很隐蔽：`canattack 10 55` 回
   `not_enough_kredits_to_target`（"打这个目标要多花指挥点"），于是我把一整个回合的攻击全跳过了。
   反证（用户当场指出）：`kardsmem cards --raw` 显示**全盘指向税都是 0**、GUARD 的
   `operation_cost` 就是 3，而我 **7 点**却被告知"不够"。
   ⇒ 默认传活着的 **`BP_Logic_C`**（跟 `CanSelectAsTarget` 一样）之后，同一对
   **立刻 `can=True`**。
   **纪律**：注入侧调任何带 `__WorldContext` 的函数，**一律传活的 Logic/PC/actor**，
   绝不传 0；断言"这个函数不靠上下文"之前先做**能区分两个假设**的对照实验。
   审计范围（当时全查了一遍）：注入侧只有 3 个函数库调用 ——
   `CanAttack`（已修）/ `CanSelectAsTarget`（本来就传 Logic）/
   `CanMoveCardToLocation`（传 actor，非 0）。
19. **"提交被静默跳过"的第二个根因：`IsTargetArrowLengthValid()`。**
   攻击的动作流一条都不进，先前的解释是"漏了悬停目标"。**还有一道闸门**：
   `BP_BoardCard_C::OnActorMouseUp`（entry 17498）在真正落地前跑
   `IsTargetArrowLengthValid()` = `targetArrowFinalLength > 3000`（桌面）/5000（移动端）
   （1.58 导出 `BP_BoardCard.cpp:4836`）。
   那个 double 是**箭头**按 `GetArrowLength()` = `|arrowHeaderPlane − 箭头原点|²`
   算出来的（`:1855`），并在**箭头销毁那一刻**写回攻击者板卡（`BP_targetArrowRVX.cpp:955`）。
   ⇒ **只写板卡上那个字段没用**（我写 4000，回读还是 136.9 —— 被销毁那一步覆盖）；
   必须让 `GetArrowLength()` 自己算出来就是大的 ⇒ 在同一次 JS 执行里
   调 `arrowHeaderPlane->K2_SetRelativeLocation(4000,0,0)` 把头部平面搬远
   （**纯客户端几何**，不动真实鼠标、不影响发给服务端的信息流），
   然后紧接着落地。实测 `arrow_len_after=1440000`、`len_valid_after=1`、
   `XActionAttackCard` 进动作流（`defenderDestroyed=1`）。
   新原语：JS `writeThenCalls(writes, calls)`（"写字段 + 带参/空参调用"混合，一次执行）。
   ★ 这与 #17 是同一族：**客户端闸门读的值，真鼠标是"做动作"做出来的，我们是"写"出来的**，
   而**写必须和消费在同一次 JS 执行里**（中间插一帧就被 tick 重算）。
20. **`except Exception: pass` 把"代码 bug"伪装成"内存里没有"。**（2026-09-26）
   `card_title()` 恒回 `None`（"内部名 → 卡面标题"读不出来），我按"FText 读法不对"查了一轮
   —— **方向全错**。两个真因都在代码里：
   - `OFF_CARD_TITLE` 这个**模块常量根本没定义**，函数体里引用它 ⇒ `NameError`，
     被外层 `except Exception: pass` 吞掉 ⇒ 外面看到的就是"内存里读不到标题"。
   - `class_named(name) + 0x110` **错的**：`class_named()`（=`scan_classes`，按
     `UObject::Class` 的**名字**匹配）返回的是"**该类的某个对象**"（含 CDO），**不是 UClass**；
     `+0x110` 落在普通字段上，实测恒 0。正确跳法是**两跳**：
     该类对象 →（`UObject::Class`）它的 UClass →（`UClass::ClassDefaultObject // 0x110`）CDO。
     修完实机 4/4：`THUNDERSTORM` / `TROPICAL STORM` / `CYCLONE` / `SENDAI REGIMENT`。
   **纪律**：① 结论旁边凡是写"内存里没有/读不到"，先确认代码路径真的跑到了（宽泛 `except`
   会把 bug 说成环境）；② **函数名/变量名不是语义**（`class_named` 不返回 UClass ——
   跟 #16「函数名不是证据」同一条）。
   顺带：用户口径 **"卡类/CDO 运行时不会动 ⇒ 按类名找对象就行"**，所以标题走 CDO、**不扫实例**。
21. **把两种语义塞进同一个动词名 —— "带目标出牌"其实有两种。**（2026-09-27）
   2026-09-26 用户就点过：「**语义上，那个不是部署。虽然好像交互逻辑类似。**」我当时只把它
   记成待办，直到 2026-09-27 实测 `FOR FREEDOM`（指向性**指令**）才发现两条链的提交时机
   根本不同：**指令**在松手那一次就成交（`BP_HandCard.cpp:1204 !IsUnit` →
   `AttemptToPlayFinal` 读 `targetOverride` ⇒ `PlaceHandCard`），**单位**松手只做视觉落位
   + 生成箭头、必须再点一次目标。而两者**共用**同一个"待点目标"态和同一个提交口
   `BP_Logic::GlobalMouseUp`。
   ⇒ 教训：**动词名要按"这条链到底在做什么"取，不能按"我看过的第一个用例"取**
   （旧名 `confirm_deploy_target` 把"部署"当成了全部语义，于是 HIDDEN PLANS 的"指向
   一张隐蔽单位卡"被硬塞进"部署"）。现在的分层：`play_order_on_unit`（指令）/
   `deploy_unit_with_target`（单位）/ `select_unit_target`（点选单位当目标）；**旧名删除、
   不留别名** —— 留着别名下一个人还会照旧名理解。
22. **判据里混进"旁证" ⇒ 假阳性，而且报出来的"成功"跟用户看到的屏幕相反。**（2026-09-27）
   `pick_choice` 的 `ok` 曾经是 `命中动作 or 有任何 action or 候选集合变了 or 选中翻转了`。
   实机：选项层点了 index=1，代码报 `候选 2→1 / outcome=advanced`（⇒ ok=True），
   而用户当场说「**没pick到**」—— 那次动作流其实是**空的**，`ok` 是被"候选集合变了"
   这条**旁证**撑起来的。
   ⇒ 纪律：**"成没成"只认动作流里"这一步被消费了"的那条**（这里是
   `XActionCardToDrawSelected` / `ZActionSelectCardToDrawPending` /
   `XActionPlayCardFromHand{chooseOneIndex>=0}`）；集合变化、选中翻转、界面还开着
   一律只当**旁证**返回，不参与 `ok`，并在结果里写清 `criterion="action_stream"`。
   （与「动作成没成看动作流，不看副作用」是同一条，这次栽在"把副作用当成第二判据"上。）
23. **"只读查询"里偷偷写字段 ⇒ 拿到的答案本身是假的。**（2026-09-27）
   `can_move_to(simulate_drag=True)` 为了问 `CanMoveCardToLocation`，要临时把
   `PC->SelectedCard` 指到那张卡上 —— 那是**手柄/拖拽态字段**（跟 #17-#19 那批"伪造
   鼠标中间态"同族，`click-crash-report.md` 的 use-after-free 就是这么来的）；而且
   **没有拖拽态时这个函数恒回 False**，那个 False 根本不是判据。
   ⇒ 现在的做法：**默认什么都不写**，并**如实回 `reason="cannot_ask_without_drag"`**
   （不是 `can=False`）；`move_to_front` 干脆不问 —— 合法与否让游戏在**提交那一刻**判，
   被拒的理由从提示通道（`game_hints`）捞。要诊断必须**显式**传 `simulate_drag=True`，
   结果里标出 `writes_gamepad_field`。
   **元教训**：写一个"读"之前先问"它答得出来是靠我写了什么吗？"——靠写才答得出来的答案，
   跟伪造状态是一回事。
24. **把"静态卡数据"当成"卡面显示值" ⇒ 显示会跟屏幕对不上。**（2026-09-27，用户点破）
   我提议 `board`/`inspect` 的字段（类型/阵营/花费/行动费/效果文本）从 **CDO / 静态导出**
   读，用户当场否掉：「**不能走 cdo。比如即使是手牌也是可以被贴膜的，被减花费的。**」
   去读 UI 本体（`BP_Widget_HandCardTextV2.cpp`）才发现**它逐条读的是运行时卡对象上的
   getter**：`getTotalAttack/getTotalDefense/getTotalKreditCost/getTotalOperationCost/
   getTotalHeavyArmor`、`getCountdownValue`、`hasActiveCountdownEffect`、`IsVeteran`、
   `HasCustomAbility`、`text`（可被 `newCardText` 覆盖）、`title` **或** `campaignName`、
   `type/faction/cardSet/rarity`、`helpTextBubbleTypes`；被贴效果走
   `getbuffsFromCardsAsJsonString()` / `buffsFromCards` / `receivedAbilitiesFromCards`
   （两张表**都带来源**）。关键词旗标（`hasGuard`…）也是**运行时**能被
   `GiveGuard/GiveAmbush` 改写的。**只有图标/材质才是静态**（`staticCardFunctions`）。
   ⇒ 落地 `ops.inject.card_totals(card_id)`（注入式只读调那批 getter，返回
   `source="game:UBaseCardObject::getTotal*"`）+ `kardsmem.board` 补
   `operationCostBuff@0xB4`（**以前漏了它** ⇒ 被减行动费的卡显示成原价）、
   `faction@0x7C`/`rarity@0x11C`/`cardSet@0x130`、`hasBeenAttackedThisTurn@0x280`。
   **纪律**：凡是"屏幕上会显示"的量，先问一句"UI 是读字段还是调 getter"，然后**照做**；
   静态导出只配给美术资源和"这张牌是哪个模板"这类真静态的东西。
25. **只差一个词的字段对，语义正好相反 —— 主动 vs 被动。**（2026-09-27，用户当场纠正）
   我口头把"伏击判据用的那个旗标"说成 `has_attacked_this_turn`，用户一句"**不是，这能一样？？？
   被动和主动的区别**"点破：

   | 字段 | 偏移 | 语义（**带主语**） | 该用在哪 |
   |---|---|---|---|
   | `hasAttackedThisTurn` | `0x281` | **这张单位本回合攻击过**（主动） | `can_act`（本回合攻击过 ⇒ 不能再攻击） |
   | `hasBeenAttackedThisTurn` | `0x280` | **这张单位本回合被攻击过**（被动） | 伏击（"每回合首次被攻击"才触发 ⇒ 被动） |

   代码里两处**用对了**（`kardsmem.board` 的 `can_act` 用主动、`view._marks` 的伏击用被动），
   但我在对话里把两个混成一个 ⇒ 记录、注释、结论一律**写清主语**（谁打谁）。
   **元教训**：相邻字段名只差 `Been`/`ed` 这类词缀时，别靠"读起来差不多"理解语义 ——
   SDK dump 里它们是**相邻两行**，grep 一次就能把两个都看到；写结论时把两个都列出来比对。
26. **"选定"不等于"进了手牌" —— autoplay 卡会把结果延后到回合开始。**（2026-09-27，用户纠正）
   我写"预报第二段选完 ⇒ `SpawnCardInHandBySide` 进手牌"，用户纠正："**第二段调用
   `selectCardToDraw`。但是那几张 autoplay 覆写了 `OnHandTargetSelected` 被运行。
   选择的卡没进手牌而是写进变量，回合开始时再根据情况将其 spawn 到手里。**"
   逐行核实（`card_event_sunny1_blue_sky.cpp`，三张天气卡同构）：
   * `:83 OnPlayedFromHand` → `:32 selectCardToDraw(cardID, false, isEffect=true)`
     → 内部调它自己覆写的 `:93 GetChooseSpawnCards`
   * 选中 → `:71 OnHandTargetSelected(...)` → `:36-41 **weatherCardChosen = GetCardFromID(...)**`
     （**只写成员变量，不进任何区域**）
   * `:61/:43-55 OnStartOfTurn` 才 `SpawnCardInHandBySide(side, weatherCardChosen->name, …)`
     并置空变量；卡上还带 `GameplayTags={autoplay}` + `usedTriggers={OnStartofTurn}`
     + `customName1="startofturn0"`。
   ⇒ 两条纪律：① **别用"手牌里多没多一张"判成败**（既违反"看动作流"，这里还会因时序误判）；
   ② 想读"现在挂着哪个待定选择"，直接读那张卡对象上的成员变量（`weatherCardChosen` 等，
   反射链可读）。**元教训**：效果链的**完成时刻**要单独确认 —— "选中了"和"生效了"之间可能隔一个回合。
27. **把"第 i 格"当成站位 —— 真人给的是"空隙"。**（2026-09-27，用户对着屏幕纠正）
   我实现 `slot` 时按"支承线内的 `locationNumber`"理解，实机把 108 装甲掷弹兵团打到了**最右**，
   用户："**你实际上把 108 部署在了最右侧。** 原：4h, hq, 游骑兵。现在 4h, hq, 游骑兵，108.
   一般真人部署时，将单位拖拽到所需的位置：**两个单位中间，或最右最左侧，放手**。
   场面上的渲染会把单位挪开一些。最终部署时显示最终渲染。"
   去读 `BP_Board.cpp` 才看清换算链：`FindCardLocationUnderCursor`（纯出参，读真实鼠标 →
   射线命中那一排）→ `FindLocationNumberForCoordinates(coords, loc)`，后者遍历该排每张卡、
   取「**中心 X 在光标左边**的卡的最大 `cardLocationNumber`」再 `+1`。
   ⇒ **要写进 `LocationNumberUnderCursor` 的是「空隙左边那些卡的最大列号 + 1」，不是格号**；
   游戏再按"比它小的在你左边"插进去并密集重排（稠密行上两者相等 —— 正是这一点**掩盖**了区别）。
   * 落地：`slot` 统一成**空隙序号**（0=最左 … n=最右，即"拖到谁和谁之间"），
     `gap_request_key()` 负责换算，`gap_number()` 直接问**游戏自己**
     （`BP_Board_C::FindLocationNumberForCoordinates`，只读出参）并跟解析式对账，
     CLI `gaps`/`gaptop` 只读复现。
   * 实机端到端验证（两次，覆盖部署与上线）：
     ① 后排 `[0:瑟堡(hq), 1:游骑兵营]` 上 `unit 27 1`（插到两者**之间**）⇒ 回执
     `locationNumber:1` + 渲染 `[0:瑟堡, 1:394th, 2:游骑兵营]`（游骑兵被挤到 2）✓
     ② 前线 `[0:4H, 1:108gen]` 上 `front 25 0`（**最左边**）⇒ `XActionMoveCardToLine{25,0}`
     + `ZActionMoveCardToNewLocation{newLocationNumber:0, moveReason:Advance_playerMove}`，
     渲染 `[0:游骑兵营, 1:4H, 2:108gen]` ✓（空隙 0 与 play_card 共用同一换算函数）
   * 元教训：**别把"用户描述动作的方式"翻译成"我以为的参数语义"** —— 用户说的是"拖到两个单位中间"，
     那就是**空隙**；而且**参数语义要去读游戏把动作换算成量的那一步**（这里
     `FindLocationNumberForCoordinates`），而不是自己给"第几格"下定义。附带：
     `_free_slot()` 扫第一个空列号，在稠密行上等于"追加到最右"，所以"参数被吞"看起来像"能跑"。
28. **"动作流为空"≠"没成" —— 有的步骤的回执在**下一步**。**（2026-09-27，实机踩到）
   保密计划（HIDDEN PLANS 16，抉择+指向）：`play 16` 松手后动作流**空**（抉择面板这才打开）；
   `choose 1 choose_one`（点第二个选项）动作流**也空** —— 但这不是失败：
   `chooseOneActive` 1→0（面板关了）+ 游戏进了"待点目标"态（`cardBeingPlayedFromHand=16`、
   `arrows=1`）。**真回执在点目标那一下**：
   `ZActionPlayCardFromHand{cardID:16, targetCardID:27, chooseOneIndex:1}` +
   `ZActionRevealCard{27}` + 两组 `ZActionGainAttack/Defense`。
   ⇒ 两条纪律：① **"动作流为空" = "这一步没产生动作"，不等于"动作失败"** —— 先看游戏自己的
   状态（面板开着没 / 待点目标态 / 待定点还在不在），把"这一步本来就不产生动作"和"被拒"分开；
   ② **但旁证仍然不许进 `ok`**（#22 不变）：`pick_choice` 只把它们报成
   `awaiting_target`/`playing_from_hand`，`ok` 仍只认动作流。
   **判据可以"往后挪一步"，不可以"降级成旁证"。**
   * 落地：两步流程只在**注入侧**实现一份 —— `ops.inject.choose_one_with_target(index, target)`：
     闸门是游戏自己的 `card_being_played_from_hand`；没有它但有选项回执 ⇒ `resolved_at_option`；
     都没有 ⇒ `reason="option_not_consumed"` 且**不点目标**。`ok` 只认**第二步**的动作流。
     `AgentSession.choose_one_with_target` 纯转发；CLI `chooseone <i> <target>`。
     ★ 用户点破的场景：我实机是**手动分两步**才走通的 ⇒ 合并动词的旧写法（`ok=False` 就 return）
     是**没测出来的 bug**。现在有离线回归 `test_choose_verbs.py`（18 项，假 `self`）钉住状态机：
     "合并动词能走通"必须有**自己的**测试，不能靠"我手动分步走通了"充当证据。

29. **把"相关的另一个变量"当成原因。**（2026-10-01）攻击大面积"被拒"（动作流为空、`CanAttack` 却 can=True、
   提交停在 `mouse_up`）。先后怀疑了：游戏线程调度、真实鼠标在窗口里、PC 的 `wasLastTouchTap`……
   A/B（三种调度模式同局轮换）证明调度无关；最后发现**窗口在前台 22/22 成功，失焦约 2/25**。
   用户当场质疑"必须前台那我用模拟鼠标干什么？以前是通的"——对：前台只是相关，真因是**失焦时 UE 降帧，
   我刚把停留从 1.0 s 缩到 0.5 s、按下/拖动间隔只剩 0.06~0.08 s，这些间隔里游戏一帧都没跑**
   （悬停转发、箭头 `overCard` 更新都没发生 ⇒ 松手提交被静默跳过）。恢复 1.0 s 后失焦 10/10。
   ⇒ 纪律：① **改了"时序"就要在"失焦/后台"条件下回归**，不能只在前台测；② 模拟输入的"停留"
   应该按**游戏帧数**算（`Injector.settle(seconds, frames)`，PC `ReceiveTick` 计数），不是只按墙钟；
   ③ 用户质疑"为什么必须 X"往往是在指出**你把相关当因果**。
30. **跨线程调用蓝图 ⇒ 随机崩溃。**（2026-09-30 转储定案）注入的 `ProcessEvent` 原来跑在 frida 线程上，
   与游戏线程并发改 `UWorld+0x460`（`OnActorSpawned` 多播委托列表，无锁），留下悬空项，之后压实时崩
   （`kards+0x120F1B6`/`0x11D37E6`/`0x11AD577`，虚表指针垃圾 0x1e3020030）。现在所有含 `callPE|callRawArrImpl|pe(`
   的 RPC 都经 CModule `Interceptor` 在**游戏线程**（PC `ReceiveTick` 入口）上执行；4 s 没人接手就撤回报错。
   ⇒ 新增注入动作一律走这条；别在 frida 线程上直接 `pe(...)`。
31. **常驻监听器被 `player.play.play` 占住时收不到命令。** 要中途停局只能走 UI（齿轮 → 投降），不能发命令；
   重启监听器用 `quit`（可以），但**不要强杀监听器/ProcDump**（游戏会崩）；ProcDump 用 CTRL_BREAK 停
   （`_nn_scratch/stop_pd.py`）；`-w` 会让游戏在创建时退出 ⇒ 先开游戏再开监听器。
   物理点击要先前台（Alt 键事件 + `SetForegroundWindow`），否则被 Claude 窗口吃掉。★ 但置前本身受"Agent 使用真鼠标/键盘与置前窗口的规则"约束：先征得用户同意、带 timeout。
32. **把"相关"当"因果"的第 3 次：攻击被吞掉的真因是"光标在窗外时 `Location/Row` 是旧值"**，
   `mouseOverActor` 只是相关（§3b⁶ 的结论曾被 3b⁷ 推翻）。修复 = 提交批次里显式写
   `LocationUnderCursor=7` + `RowUnderCursor=1`（`ops/inject.py::_attack_once`）；三局 30/30 成功。
   ★ **用户观察（2026-10-02）**：补写这两个值之后，**真鼠标在不在窗口内都不再影响脚本操作**（此前"鼠标移出窗口才稳"的条件作废）。我们自己的 30/30 是在"窗外 + 后台"条件下测的，**窗内的成功率没有单独统计**，以用户观察为准；测试不再要求把光标移出窗口。
33. **`Logic→spawnCardFunctions(*Out)` 返回的不是对局在用的那个 `BP_CardFunctions_C`**（2026-10-01 夜实测）：
   同一时刻进程里有**两个**实例 —— 它返回的那个 `InitialSeed=0`、`seed` 恒 0（空壳），
   真正在用的是关卡 actor 里 `InitialSeed != 0` 的那个。判活 = **关卡 actor + `InitialSeed != 0`
   + `InternalIndex(+0xC)` 最大**（第二条是当晚第二次踩出来的：**上一局的实例换局后不会立刻销毁**，
   也有非 0 种子，只看"非 0"会锁到已经冻住的旧流 —— 症状是"seed 一直不变"；
   game 每局新建实例、索引单调递增，实测 149360 vs 残留 128630）。
   （`Locator(m,base).actors()`，156 个 actor，0.00 s）；别用 `spawnCardFunctions`，也别扫全量 GObjects。
   ⇒ 读随机流别再走"函数返回"这条路。实现：`kardsmem/rng.py::card_functions_ptr`（缓存 5 s 复核）。
34. **随机流采样别在注入侧开线程**：Python 采样线程和 `player.play.play` 抢 frida RPC 会把 script 抢崩
   （`script has been destroyed`）。**外部只读采样**（`kardsmem` ReadProcessMemory，20 Hz）零干扰，
   实测 20.0 Hz / 间隔中位 50.6 ms / 6759 条不断流。监听器就绪判定还要注意：`live_session.py`
   启动时会重建 `live_cmd.txt`，而 `live_log.txt` 是**追加**的 —— 从整文件尾部找
   `precheck.available() = True` 会命中上一局的旧行，命令会在文件重建时被吃掉（22:22 踩过）。
35. **"三选一"的候选是"池先洗牌再取前 3"，而池子会被『进预备』改变 —— 必须实时问游戏 API**
   （2026-10-02 定案）。`keepOrder=false` 那一类（好人寥寥等）：原生
   `Array_ShuffleFromStream(池, cardsRandomStream)`（正向 Fisher-Yates、抽 **N = 池大小** 次）→ 取前 3。
   池 = **游戏官方 API 当前卡表（默认只返回"没进预备"的卡）** ∩ 该卡自己的过滤（阵营/稀有度/是否单位）
   ∩ 静态卡表的卡集过滤。**别用离线卡库快照**（预备名单轮换，快照会过时）；
   也别只按静态卡表筛（那会多出"进预备"的卡，池子就错了）。
   自检法：出牌前后看种子**跳几步**——跳 N 步 ⇒ 池 = N 张（好人寥寥本账号 18）。工具见
   `_nn_scratch/{kards_api_reserved,choose_spawn_predict,watch_choose_spawn}.py`。
36. **PvP 里我们可能在 2 号座位 —— 任何"座位 1 = 我方"的写法都会整段失效**
   （2026-10-02 实机，休闲局，用户："怎么也秒空过了？始终如此"）：
   `kardsmem/cards.py::read_raw()` 的 `side` 原来用**静态** `SIDE_ENUM`（座位 1 = local）
   ⇒ 我们在 2 号座位时**整只手牌被判成 `enemy`** ⇒ 旧 `pick.hand_card_actors()` 把它们全丢掉
   ⇒ `ops.inject.hand_actor()` 永远找不到自己的手牌 ⇒ 每个出牌/部署动作都在预检被
   "card X 不在我方手牌"挡掉 ⇒ 搜索有候选但**全部 `exec=False`**，只能结束回合（"秒过"）。
   训练局一直是 1 号座位，所以只在 PvP 暴露。修法：side 一律按 **`MatchLog.my_side()`
   （`Logic.mySide`，本局座位，1/2）** 判（`cards._side_name/_my_seat`；**缓存必须跨局失效**，见 #37）；
   静态表只当"读不到座位"时的兜底。**新写任何读侧/判侧代码都照这个来。**
37. **"一局内不会变" ≠ "进程内不会变" —— 常驻监听器跨局复用同一个 session/MatchLog 时，
   "对局级"字段（座位/Logic/OnlineMatch）的缓存必须跨局失效**（2026-10-02 C/D 两连局实机，
   D 局整局 0 出牌）：D 局 316 步里只有 13 次 `end` 成功，其余 87 个动作全被拒、清一色
   `card X 不在我方手牌`；而规则侧（kardsmem.board 回合奇偶那条读法）看得见 9 张手牌 ⇒ 两边视图分叉。
   三个缓存点：① `kardsmem/cards.py::_my_seat` 把座位缓存在 **session 对象**上、写一次永不失效；
   ② `kardsmem/matchlog.py::forget()` 漏清 `_my_side_cache`；③ `_logic()/_my_side_cache` 命中前
   不验新鲜度（长寿命 MatchLog 跨局继续用上一局的 Logic 指针/座位）。修法（2026-10-02）：
   座位缓存跟随 MatchLog（`my_side()` 命中前先 `is_stale()`，换局自动 forget+重定位）；
   `forget()` 两个都清；`locate()` 失效即丢弃重找；`AgentSession.legality()` 座位变了重建。
   离线回归 `test_seat_cache.py`（22 项）+ `selftest` 两构建。
   **规律：凡"每局会变"的字段，缓存生命周期绑定"当前对局对象"，命中前用便宜判据
   （`is_stale()` / 单例指针变化）验一次；把值挂在 session/进程级对象上就是事故。**
38. **评估不能"编造价值"：取不到效果 ≠ 按费用给一个估算**（2026-10-02 实机，用户两连报）：
   ① "指令的评估有问题。6费空打+3+2"——`rule._hand_eff` 对取不到效果摘要的指令给
   `_est = order_mult × cost`（6 费 = +5.4，扣手牌持有价值 2.16 仍 **+3.24**）⇒ 搜索/9 路预报
   把"打了什么都不做"的 6 费牌当正收益（实机：`sunny4_scorching_sun*` 恒 +3.24、连选 4 次，
   而那局**手里和场上都没有空军**，卡的效果根本无从发生）。
   ② "游骑兵，脚本还是不选 +4+4"——部署抉择（5th RANGERS："行动花费 0" / "+4+4"）的候选不是卡、
   `score_candidates()` 查不到 ⇒ 旧启发式全 0 分 ⇒ **永远点第一个**。
   修法：`PARAMS["unknown_order_est"]=0.0`（不编造；打出去只剩丢手牌的负收益 ⇒ 不空打）；
   二选一改为**首选 VM 枚举出的抉择分支**（用户点破："取不到效果摘要 是不可能的"——实机
   `card_unit_5th_rangers` = `choice=True, outcomes=[{"opcost":0},{"buff":[4,4]}]`，顺序=屏幕 index），
   文字匹配只作 VM 不可用时的兜底；`meta.pick_eval_src` 写 `vm`/`label`。
   规律：**效果摘要取不到就记 gap 并给 0，绝不按费用"估"一个正数**；但先分清——
   `complete=True 且效果 {}` 是"**已知的空**"（例：6K 骄阳在没有空军时就是不产生效果），
   不是缺口；真缺口的判据是 VM **没跑完**（停在未实现的原语/超时）。
39. **热重载会打断零参 `super()` —— 重载过的类"新建实例"会炸**（2026-10-02 实机）：
   11:44:27 热重载 `player.rule`（`hotreload.py` 把新方法并回旧类）之后，**当前实例**继续跑没事；
   但 11:48:05 开下一局时 `player.play.play` 新建 `RuleV2` → `TypeError: super(type, obj): obj
   (instance of RuleV2) is not an instance or subtype of type (RuleV2)` —— 新函数对象里的
   零参 `super()` 用 **新类** 做起点，而实例挂在 **旧类** 上。⇒ 热重载之后要**重启监听器**
   （或让 Loop 重建 policy）再开下一局；别以为"这一局还能跑"就代表重载成功。
   （要根治得在 `hotreload.py` 里把旧类的 `__class__` cell 也改写，或禁止对含零参 `super()`
   的类做热重载。）

40. **开局前必须先 `end_of_match_continue`，不能直接 `press_play`**（2026-10-02 实机，用户点破：
   "你还没有 end_of_match_continue 就 press_play？？？"）：上一局结束后客户端**仍带着残留状态**，
   跳过"回牌组页"直接注入 `press_play` 会开出一局**"我方 T1 打完、对手（服务端 AI）的回合永远不来"**
   的死局 —— matchlog 只剩 `XStartOfGame` + 我方 `Start/EndOfTurn` 共 3 条、`my_turn_has_started=False`、
   服务端不响应（AI 本身不会卡）。正确流程 = `_nn_scratch/open_next.py`：
   ① 循环 `end_of_match_continue` 直到 `left_screen=True`；② `settle`；③ `list_decks` 闸门；
   ④ `press_play`；⑤ `player.play.play` 接手。**即使只读检查说"已经在牌组页"，也要先走 ①。**

42. **对局进行中绝不能 `press_play`（会 0xC0000005 崩溃）**（2026-10-02 事故）：`open_next`
   在一局**尚未结束**时（`end_of_match_continue` 全失败、`left/after` 全 None），只凭
   `list_decks` 非空就点了"开始" ⇒ 游戏随后
   `EXCEPTION_ACCESS_VIOLATION reading address 0xffffffffffffffff` 崩溃（用户手动打完才崩）。
   ⇒ 硬闸门 = **只读 `kardsmem.gs.GameState.match_active`**：`True`/`None`（读不到）**一律拒绝**；
   "离开结算页"≠安全、"牌组列表读得到"≠安全。所有点开始的脚本
   （`open_next.py` / `start_ranked_now.py` / `continue_ranked.py`）统一走
   `_nn_scratch/play_guard.py::safe_to_start(sess, decks)`。
   ★ **闸门第一版判据写错了**：用了 `Locator.in_battle` —— 那个在**主菜单也为真**
     （`kardsmem/gs.py` 的注释白纸黑字："战斗 GameState 类已加载。**主菜单也为真** —— 不是「在对局中」"），
     结果是"一局都开不了"（主菜单也判成对局中）。判「真的在一局里」**只认 `GameState.match_active`**
     （指挥点加密记录解得出 ⇒ `kredits` 非 None）。同一个进程里 `kardsmem selftest` 的 F 段
     （`match_active=False` + "[INFO] 不在对局中"）就是现成的对照观测。

43. **禁用坐标点击蒙界面：`startmatch.py` 已删；过渡态不许点；控件里"按钮/文字/内层 UButton"各自可能挂事件**
   （2026-10-02 事故，用户："不准用那个乱点一气" / "你点击有问题。可能是其中文本有点击事件，可能是按钮有点击事件"）：
   `startmatch.py --to mulligan --mode versus` 在 `deck_select` 判到后点模式 + 点"开始"，
   紧接着分类器返回 `state=None`（界面正在过渡）——旧版把 `None` 并进 `deck_select` 分支又点一遍，
   连点 5 次（`(1159,625)`/`(1163,644)`×2/`(1163,651)`×2），**随后用户把客户端关掉了——这是用户的主动操作，不是崩溃**（真鼠标被连点占住，用户连给 Agent 发消息都做不到，只能强关夺回控制权；所以没有崩溃转储和 Windows 崩溃事件，launcher 14 秒后拉起了新进程）。
   ★ 真正的危害 = **脚本劫持了真鼠标，让用户失去对电脑的控制**——这正是"不准用坐标点击/动真鼠标前先问用户"的根本理由。
   ⇒ **用户 2026-10-02 指令：`startmatch.py` 直接删掉**（已删；同类的 `_nn_scratch/click_start_mouse.py`
   一起删，都靠坐标蒙）。`vendor/actions.click` 只在"确实需要物理鼠标"时用，且**动鼠标前先问用户**。
   ① 过渡态（`state=None`）只许等，不许点；② 要"点"，就**注入调游戏自己绑定的句柄**
   （`ops.inject.press_play` / `select_mode_by_label` / `_live_deck_sidebar` 的做法）——
   `squareKardsButton_Widget_C` 这种控件里 `ButtonText`(UTextBlock@0x360) / `Button_0`(UButton@0x370) /
   `OnClicked`(多播@0x390) 各自可能挂事件，坐标落在谁身上取决于布局。
   **本客户端四个页面（用户 2026-10-02 当面指认）**：① **主页** = 顶上金翅膀 logo；
   ② **开始** = 对战模式列表 + 牌组页（右侧"排位/休闲"页签 + 开始按钮）；③ **卡牌** = 收藏/卡组编辑；
   ④ **商店**。★ `play_btn` 模板在**每一页**都能匹配到（左侧导航恒显）⇒
   `config/states.json` 的 `main_menu` 判据在这个构建上**不可信**，别拿它当"在主页"的证据。

44. **空跑 VM 不能拿静态 CDO 当 `self`：运行期子对象（`cardFunction`）是空的，虚调用会退回卡类 ⇒ 报出"函数不存在"的假缺口**
   （2026-10-02，`+deckchg` 预检）：用 `AllStaticCardsSortedByName` 里的 LOVAT SCOUTS CDO 跑
   `OnAfterDeckChanged`，链停在 `Unimplemented @0x2C Context: '函数 GetDeckByside'`，
   而 `NATIVE-COVERAGE-1.60 §12` 说它是**蓝图、有字节码**。反汇编
   （`_nn_scratch/probe_lovat_ubergraph.py`）显示调用是
   `Context(InstanceVariable cardFunction) → LocalVirtualFunction(GetDeckByside)`；
   实测 **CDO 的 `cardFunction=0`**、真实实例的 `cardFunction` 非空，它的类上确实有
   `GetDeckByside`（`0x15c2758a500`，`has_bytecode=true`；名字就是小写 s，别"顺手改对"）。
   ⇒ 要空跑带"卡功能对象"的钩子，必须用**真实实例**；CDO 只适合看字段默认值。
   同批的教训：三条标记族（`+intel`/`+hooks`/`+shuffled`）此前"恒空"都是被 `except` 吞掉的
   `TypeError`/`BoardState` 缺口（`6836c6f`/`146d4b5`）——**链的空结果必须能解释**，
   所以诊断字段（`intel_dbg.tried`、`marker_err`）比"没触发"这三个字重要。

41. **每次 `frida.attach` 都在 Temp 里留一份 ≈43 MB 的 `frida-<hash>`（agent 解压），进程被杀/崩溃不清理。**（2026-10-02，用户用 WizTree 点破：
   9/25 起 612 份 ≈ 25.8 GB 吃光用户 Temp，C 盘只剩 15.8 GB）
   我当时只把"短命注入进程"当成崩溃问题（弯路 #31），没算过它的**磁盘代价**。⇒ ① `ops.inject._frida_tmp_setup()` 在 `import frida` 之前把本进程
   TEMP/TMP 指到 `D:\Kards\_frida_tmp`（`KARDS_FRIDA_TMP` 可改）；② `sweep_frida_tmp()` 在 attach 前、`close()` 里 detach 后清**没被占用**的旧目录
   （agent dll 能以追加方式打开 ⇒ 没人在用；占用中的跳过）；③ 注入类脚本一律走常驻监听器，别为了"问一下"另起进程；④ 做任何会反复起进程的东西前先算"每次留下什么"。

## 当前状态（2026-10-03 更新；细节看 `PLAN.md` §0 与 `TODO.md`）

- **规则 bot**：`player/rule.py`(RuleV2) + `policy/boardeval.py`(搜索) + `semantics/effectvm.py` + `kardsmem/vm.py`（外部 Kismet VM，
  带缓存/截止时间）。常驻监听器 `_nn_scratch/live_session.py`（命令文件通道），日志 `kards-data/nn/logs/rule-live-*.jsonl`
  （含 `t_snap/t_decide/t_exec`、`atk_loc/tgt_loc/atk_kw/tgt_kw` 等逐步诊断）。
- **盘外评估/模拟分层**：`sim/`（模拟：状态/事件队列/规则）与 `evaluation/`（评估：纯估值）分开，依赖方向由 `test_arch_rules.py` 把关；
  `boardeval.py` 是过渡层。标准见 `EVAL-ARCHITECTURE.md`；OPS 侧标准见 `OPS-ARCHITECTURE.md`。
- **控制面板**：`gui/`（`run_gui.bat`）——自动对局编排、决策历史、监听器启停、版本识别；面板只写控制文件，不碰游戏。
- **版本与 RVA**：运行中游戏的 `版本号.分支` 直接读进程内存（`kardsmem/version.py`）；**目标架构**是运行时扫描 RVA + 缓存 + 随包种子表、
  去掉手工版本表（`ARCH-RELEASE-VCS.md`，S1 的 `kardsmem/rvascan.py` 已做，S2 需游戏）。
- **总原则（2026-10-03）**：行动 = 动作 + 其后全部选择；机制按**能力**探测分支（旧版本无该机制就走旧规则），新版本靠**语义指纹**降级为缺口；
  种子/随机流机制自最旧版本传下来、改动极保守，视为稳定底座（`ARCH-RELEASE-VCS.md` §7）。
- **已知的版本相关机制**：洗牌钩子（海战起）、Bond 协力整套（国土阵线起）、更早只有"无牌可抽才疲劳伤害"。
- **只打训练/AI 局**（对手 player_id 为负）；开局前必须目视确认"训练模式"高亮。
- **关键词读游戏自己的 getter**（`getHas*`），不读 flag 字节（被赋予的冲击 flag=0）。
- **待办清单**：`kards-agent/TODO.md`（现行看板；规范见本文"待办（TODO）与文档维护规范"；旧版 `TODO-ARCHIVE-2026-10-02.md`）；
  记忆：`memory/project_bot_session_2026_10_01.md`、`project_rng_stream_2026_10_01.md`、`project_crash_analysis_2026_09_30.md`。
- **崩溃转储工具** `tools/crashdump.py` / `tools/CRASHDUMP.md` 由 DeepSeek 维护（ProcDump 引擎），**别改**；输出 `kards-data/crashdumps/`。
- **失焦注意**：任何改了等待时长的提交，都要在游戏后台的条件下打 ≥12 次攻击验证。

## 反制（gotcha / Countermeasure）与其它 2026-09-26 实机结论

- **更正一条早先的错误笔记**：先前写"CARELESS TALK 这类 gotcha 拖出去只会产生假回执
  （卡回手、指挥点退回）"——**错的**。gotcha = **反制**（`text_HelpBubbles.cpp:52`：
  `Countermeasure` / "Hidden effect that can only trigger from hand on next enemy turn"）。
  `play_card` 把它拖出去**就是"在手里激活"**：动作流是
  `XActionPlayCardFromHand {cardID, location: **Hand_Left**}` + 扣费
  （费用对敌人隐藏：`BP_VisualController::kreditsHiddenInGotchas`），卡**留在手里**、
  `gotchaActivated > 0`。实机：CARELESS TALK(1 费)="敌方部署单位时对其造成 3 点伤害"、
  NIGHT HUNTERS(4 费)="敌方部署单位时摧毁它并抽一张"（对面一个 3/2 因此消失）。
  ★ `BP_HandCard::ToggleGotcha` 是**开关** ⇒ **已激活的反制不要重复 play**（会取消激活）。
- **不能直接部署到前线**：`play_card(location="front")` 写了 `LocationUnderCursor=7`，
  游戏仍把单位放进**支援线**（实机 8/8 落在 back）。上前线只能走 `move_to_front`，
  而且**移动要付操作费**（GUARD 移前线扣 3 点）、**步兵移动后本回合不能攻击**
  （`no_attack_left`）；规则百科："坦克能在同一回合移动并攻击"。
- **"选择手牌当目标"**（`selectTargetOnPlayedFromHand`，如 THE AMERICAN GUARD
  "Choose a card in hand. Convert the top card of your deck into it."）：
  点手牌 = `BP_HandCard::OnActorClicked`（entry 30474）→ `selectHandTarget()`（会给对手发
  `toggle_select_hand_target;<id>`）；再点**确认按钮**
  （`ConfirmHandTargetButton_Widget::BndEvt__StatButton_...onClicked`，entry 2468）
  → `handTargetSelected` 移动队列 → 源卡 `OnHandTargetSelected`。判据是同源的
  `源卡.IsValidHandTarget(候选手牌)`。落地在 `hand_target_pending/hand_target_legal/
  select_hand_target`（会话命令 `htgt`/`htlegal`/`hsel`）。
- **Bond**（Homefront）：`ability.bond`；**手中带 Bond 的牌在回合开始对你的总部造成疲劳伤害**
  （`ApplyFatigueDamage(fromBond=True)`，提示"Your card with Bond deals {damage} Morale damage
  to your HQ."）⇒ 要么打出去、要么用 RATIONING（"Remove Bond from cards in your hand"）之类清掉。

## 证据标准

- **单一观测对得上不算证据。** 先问「这个字段的**取值结构**和该语义的**结构**对不对得上」。
  换牌标记可多选 ⇒ 一个「全局最多一个 0」的字段，光凭这点就该否掉。
- 同类错误犯过两次：`0x3C8 = mySide`、`0x924 = 换牌标记`。
- **类身份只认 `UClass::PropertiesSize`**，绝不认 `*_VFT` 之类的名字。
- 转储是静止的，但**它抓在哪一帧你控制不了** —— 动画期间抓的转储会让你把
  「还在飞」读成「未标记」。
- 结论错了就**撤回并留档**（写清误判来历 + 教训），不要悄悄改掉。

## 代码约定

- **★ 生产代码不得放在、也不得依赖 `_nn_scratch/`**（2026-10-03，用户：“为什么这些没进 kards-agent 目录！！为什么这些不清理一下？”）。
  `_nn_scratch/` 只放一次性探针；一个脚本一旦被别的东西依赖（监听器、开局闸门、API 缓存…），当天就搬进 `kards-agent/`
  （`tools/` 或 `agent/`）。运行期文件（监听器命令/日志通道、API 缓存、热重载开关）的位置**只**由 `agent/paths.py` 给，落在
  `kards-data/`（`.gitignore` 里）；`test_no_scratch_dep.py` 用 AST 把关。已搬：`tools/live_session.py`（常驻监听器）、
  `player/play_guard.py`（开局硬闸门，旧探针里的 `import play_guard` 走 `_nn_scratch/play_guard.py` 垫片）、
  `tools/kards_api_reserved.py` + `tools/choose_spawn_predict.py`、API 缓存 → `kards-data/api/`。
  **监听器通道**现在是 `kards-data/live/live_cmd.txt`（命令）/`live_log.txt`（结果）。
- 注释写**为什么**，尤其是踩过的坑；结论旁边标明证据来源和强度
- **不硬编码运行时算得出来的偏移**；确实要记的，写成「给人看的备注」常量并注明
- **★ 偏移表必须可换、且从 SDK dump + exe 里生成（2026-09-25 用户定的架构）**：
  本机同时躺着多份 `kards-Win64-Shipping.exe`（Steam 1.60.27292 `SizeOfImage 0x9CC8000`、
  launcher 渠道同版本号另一份二进制 `0x9CC4000`、1.57 的……），RVA 各不相同。
  ⇒ 脚本**只写偏移的名字，不写数值**；数值统一由 `python -m kardsmem.buildsrc` 从
  **SDK dump（`Dumpspace/OffsetsInfo.json`）+ exe 本身**（`FName::AppendString` 里的
  `lea r8,[rip+…]` 派生 FNamePool、`.data` 扫 UWorld 派生 GWorld）生成到
  `kardsmem/build_tables.json`，`kardsmem/build.py` 与 `kardsmem/board.py` 都从它读。
  ★ **2026-10-03 起按"运行中游戏自报的版本号.分支"选表**（已知版本 ↔ RVA 表一一对应）：
  `kardsmem/version.py` 只读扫游戏进程内存里的 `ProjectVersion`（`Kards 1.60.27292.launcher`，
  IDA 证实它来自 `GConfig` 的 `GeneralProjectSettings.ProjectVersion`，≥2 处且唯一值才采信，约 3 s，
  按 (pid, 进程创建时间) 缓存），再按 `VERSION_TO_BUILD` 得到表键。选择优先级：
  **`KARDS_BUILD` 显式覆盖 > 运行中游戏的版本 > 兜底 `current`**，`build.py`（`CURRENT/BUILD_SOURCE`）
  与 `kardsmem/board.py`（`_BUILD_KEY`）用**同一个**选择函数。`SizeOfImage`/md5 不再是放行条件，只作信息。
  新版本要先在 `VERSION_TO_BUILD` 登记它的表，否则 attach 报"版本未登记"（不猜）。
  `--check` 验一致性；`python -m kardsmem selftest` 两个构建都要过。
  ★ **选表只在进程启动 import 时做一次，运行中不切**：`build.py` 和 `kardsmem/board.py` 各有一张表，
  运行中改会让两边混用偏移（读数静默错位）。对不上就报错并提示重启
  （`agent/precheck.py::build_check`、`proc.Session.attach` 都按版本判）。
  静态读各安装树版本：`tools/pakread.py::install_version`（pak 主 key 硬编码为默认 `DEFAULT_MAIN_KEY`，
  不对再试 FModel 的值与 AESDumpster 候选）；`reverse-data/tools/usmaps/kards-<日期>-*.usmap` 里的日期是
  版本发布时间，其中旧日期那几份对应 1.54/1.55，**忽略**。
- 每次改完代码跑 `cd kards-agent && python -m kardsmem selftest`
  ★ 再加一条**不需要游戏**的：`python test_choose_verbs.py`（"抉择 + 指向"两步状态机的离线回归，
  18 项；"合并动词能走通"必须有它自己的测试，不能拿"手动分步走通了"当证据，见弯路 #28）
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
| `ui_state.py` `cv_io.py` | 模板匹配、界面分类 | （`startmatch.py` 已删；分类只作参考——`play_btn` 每页都匹配，见弯路 #43） |
  | `handedge.py` | 手牌扇形左右边缘 | **内存里没有这个量** |

  外加 `config/`（18 个模板 + 8 个状态）和 `ui_templates/`。
  ⇒ **运行时完全不依赖上游那棵工作树**（`KARDS_OCR_ROOT=/nonexistent` 下已验证）。
  **上游对齐版本：`576aa19`（v0.1.8_beta）**；怎么更新、每个模块谁在用、上游那批新东西里
  哪几样值得接，都写在 `vendor/README.md`。其中**窗口消息输入**（`actions.INPUT_MODE="message"`,
  `PostMessage`/`SendMessage`，不移动真实光标）本来能治"窗口没焦点点击被吃掉"的老毛病，
  ★ **2026-09-23 实机测过，走不通，别再花时间接它**：用 `dev/click_probe.py` 那套逻辑
  对真实 Kards 窗口发了一套完整点击（`WM_MOUSEMOVE`×2 + `WM_LBUTTONDOWN` + `WM_LBUTTONUP`），
  分别测了**窗口在后台**（不置前、真实光标全程不动、前台窗口全程是别的程序）和**窗口在前台**
  两种情况，牌组选择界面（点了未选中的那张卡组）**两种情况下都零反应**（前后两帧截图
  `cv2.absdiff` 均值 ~0.00002，肉眼也看不出变化）；紧接着同一个坐标改用**物理点击**
  （`actions.click`，真的挪光标），**一次就选中了**，说明坐标和测试流程都没问题。
  ⇒ **Kards 不走 Win32 消息队列收鼠标输入**（大概率是 UE 走 RawInput 直接读设备），
  跟游戏在不在前台无关。这条路对 Kards 死了，`ops.py` 继续用物理光标，
  真正"不抢鼠标"只剩 vendor 文档里说的那条：换一个 Windows 会话/虚拟机。
  ⚠ **2026-09-23 更正**：这里先前写着「用户定调：它将来要被『基于内存的实现』替换」，
  是**把输出侧的话错安到了输入侧**。用户澄清：**输出**（从游戏读状态）将来一律走内存；
  **输入侧仍然是模拟鼠标，不会也不能走内存** —— 那就是写内存，直接踩红线。
  窗口消息输入的去留是独立问题，别把它和"输出走内存"绑在一起。
- 上游其余部分我们**不用**：官网卡表 json、自动打牌状态机、手牌扫描那整条 OCR 链。
  （OCR 读盘面只在**核对**时可选走一下，见 `tools/field.py`；内存才是权威。）
- **`D:\Kards\OCR-Kards-Auto/` 是别人的工作树，一个字都不要改**（已恢复到 `origin/main`）。
  要动它之前先 `cd OCR-Kards-Auto && git status` 确认干净。
- `_archive/` 是被取代的一次性脚本，留档不维护；★ 2026-09-27 起 **`_archive/ops_mouse.py`
  = 旧的物理鼠标执行侧**（原名 `ops.py`，已停用；`rowcalib.py`/`test_rowmodel.py` 仍按
  归档名引用它的行模型）。`_archive/mem-era/` 是更早的内存探针（功能已并入 `kardsmem`）。
- `D:\Kards` 这个仓库**不是**自动化专用：还有 `client/`、`server/`、`ue-project/`、
  `game-installs/`，以及 `reverse-data/`（逆向资料与取材工具）。
  **自动化 + mem 工具一律放在 `kards-agent/` 下**，别再撒出去。

## 目录

| 路径 | 是什么 |
|---|---|
| `kards-agent/kardsmem/` | **读侧**（唯一入口）。`world/cards/gs/names/props/objects/kismet/pick` |
| `kards-agent/ops/inject.py` | **执行侧（唯一）**：合成事件序列（出牌/指向/上线/攻击/抉择/换牌/结束回合） |
| `kards-agent/agent/` | **命令层**：`session.py`（唯一动词集合）+ `shell.py`/`mcp.py`（前端）+ `record.py`（只读录制） |
| `kards-agent/_archive/ops_mouse.py` | 旧的物理鼠标执行侧（**已停用/归档**，2026-09-27） |
| `kards-agent/kardsmem/board.py` | 盘面模型 + 数据源（`mem` 是权威，`ocr` 只作核对）—— **本项目自己的代码** |
| `kards-agent/tools/` | 取材/标定工具（`mem_probe` `dumpmem` `mulverify` `pickwatch` `grid` `crop` `field` `pe_tools` …）。脚本里 `import _bootstrap` 就接好路径 |
| `kards-agent/sim/` `evaluation/` | 盘外**模拟**（状态/事件队列/规则下沉）与**评估**（纯估值）；`boardeval.py` 为过渡层；规则见 `EVAL-ARCHITECTURE.md` |
| `kards-agent/tools/live_session.py` `player/play_guard.py` `agent/paths.py` | 常驻监听器 / 开局硬闸门 / 运行期文件位置表 |
| `kards-agent/gui/` | 控制面板（`run_gui.bat`）：`control`/`autoplay`/`history`/`watcher`/`app`；`gameversion.py` 为兼容壳 |
| `kards-agent/kardsmem/version.py` `rvascan.py` | 版本识别（读进程内存里的 ProjectVersion）/ 运行时 RVA 扫描（第一阶段）；方案 `ARCH-RELEASE-VCS.md` |
| `kards-agent/tools/pakread.py` | UE5 pak v11 只读读取器（静态读 `DefaultGame.ini` 等；AES key 默认硬编码） |
| `kards-agent/ARCHITECTURE.md`（**总纲，先读**）`{PLAN,TODO,EVAL-ARCHITECTURE,OPS-ARCHITECTURE,ARCH-RELEASE-VCS,TODO-ARCHIVE-2026-10-02}.md` | 计划/看板/评估架构标准/OPS 架构标准/发布与 VCS 架构/旧 TODO 归档 |
| `D:/Kards/tools_ida/` | IDA 批量标注 exec thunk→真函数（`label_exec_thunks.py`、`run_label.py`、`exec_thunks.csv`）；idalib 无头运行；**应用后 IDB 已改名 2958 条，备份 `.i64.bak`** |
| `kards-agent/vendor/` | 从上游搬来的**六个**在用的模块（GPL-3.0，逐个查过引用） |
| `kards-agent/config/` `ui_templates/` | 模板表与模板图（自足） |
| `kards-agent/_archive/` `_archive/mem-era/` | 被取代的一次性脚本（含旧的内存探针），留档不维护 |
| `reverse-data/reports/spec/KARDS-AUTOMATION.md` | 主规格（**历史，1.58/1.59 时代**；现行 1.60 事实看 `reports/report/*-1.60.md` + 本文件 + `TODO.md`） |
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
- **窗口没焦点会把鼠标点击吃掉**（**仅对已归档的物理鼠标实现成立**）：事件发出去了、
  游戏没反应、内存零变化，看起来和「坐标错了」一模一样。★ 2026-09-27 之后执行侧走
  `ops.inject`（进程内合成事件），**跟窗口焦点无关** —— 这条只留给读 `_archive/ops_mouse.py`
  的旧工具。
- **选择界面开着时，出牌/移动/攻击全部无效**，而且面板可能被翻页收到屏幕右侧
  （看不见但 `chooseOneActive` 仍为 1）。
- **同一时间只允许一路在动（合成事件也算）。** 2026-09-24 无人值守那轮踩的：手写了个
  自动过牌的脚本扔进后台跑，同时又手动发了几条点击/拖拽——两路同时拖牌，游戏 UI 卡进
  一个奇怪的"两张同名卡并排放大"的界面、`chooseOneActive=1` 卡住不动，好几分钟没反应。
  `ops.inject.pick_pending()` + `pick_choice(0)` 能解开（底层没坏，只是 UI 状态被两路输入
  搅乱了），但**教训是别犯**：要跑后台脚本，就不要再手动/并行发别的动作。
  ★ 换成合成事件之后**不抢鼠标了**，但"两路写同一个客户端状态"这件事照旧危险。

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

## 现在做到哪、下一步做什么（⚠ 历史段：2026-09-22 本轮收尾时的状态，**已过期**；现状看上面"当前状态"、`PLAN.md` §0、`TODO.md`）

**先读这个**：`kards-agent/TODO.md`（状态/待办）+ 本文件（红线/弯路）+ `reverse-data/reports/report/*-1.60.md`
（1.60 的 IDA/BP 事实）。`reports/spec/KARDS-AUTOMATION.md` 已标注为**历史文档**（1.58/1.59），只作背景。

**已交付**
- 读侧：R1 场上卡 / R2 双方手牌 / R4 弃牌堆 / R5 FName / R7 当前属性 / R9 位置 / R10 临时 CardID ✅；
  R3 候选牌 ⚠ 半；**R8 可指向目标 ❌**（唯一正路：把 `cardsCheckFunctions.cpp` + 438 个
  `CanPlayFromHand` 覆写移植成 Python，`card_targets.py` 已 18/20）。
- 执行侧：E1 出牌 / E2 指向 / E3 部署后选目标 / E4 攻击 / E5 上线 / E6 结束回合 / E7 抉择点选 /
  E8 换牌 / E10 回读判成败 ✅；E9 投降 ✅（`ops.inject.surrender(confirm=…)`，游戏没有二次确认）。
  ★ 2026-09-27：执行侧全部收进 **`ops/inject.py`**（旧 `ops.py` 已归档），逐条对齐表见
  handoff §22.14.6。
- 自检：`cd D:\Kards\kards-agent && python -m kardsmem selftest` → 全 PASS（含 kardsmem.board 30 项 +
  与 `kards-offsets.json` 的一致性 + 行模型 7 组）。**改完代码必须跑这个。**

**本轮新增但还没实机验证**（当时游戏没开，只过了离线断言）
`kismet.py`（Kismet 字节码反汇编）· `objects.py`（GUObjectArray 遍历）· `props.py`（按名字的反射链）
· `notify.py`（读游戏提示文本＝动作回执）· `tools/` 里搬过来的那批脚本。
⇒ 下次游戏在跑时，按这个顺序验：`tools/notifywatch.py --bench`
（★ 原先这里写的是 `python -m kardsmem notify` —— **那个子命令不存在**，已改）
→ `tools/mulverify.py <before.DMP> <after.DMP>`
→ `tools/pickwatch.py` → `tools/mem_probe.py --selftest`。

**P0 在哪看**：规格 §11（`§11.1 前端与命令层` + `§11.2 读侧/执行侧/判据`）。
**不要在这里抄一份** —— 以前抄过，当场就开始过期。那里也写了排序原则。

**目标形态（2026-09-23 用户定调）**：三个前端 —— **交互式 shell / MCP server / NN 策略循环**，
它们要的动词完全一样，所以先有一层 `agent/`（命令层），三个前端薄薄地套上去。
动作一律委托 **`ops/inject.py`**（进程内合成事件，不再动真实鼠标），状态一律走 `kardsmem`（只读），
命令层自己两样都不碰。

**2026-09-23 下午已交付**（实机验证过，不是纸面）：

```bash
cd D:\Kards\kards-agent && python -m interfaces.shell
```

| 模块 | 干什么 |
|---|---|
| `agent/session.py` | 唯一的动词集合（三个前端共用） |
| `agent/view.py` | 盘面渲染 + 短号（`h1`/`m2`/`e3`/`hq`/`ehq`）+ inspect + 历史 |
| `interfaces/shell.py` | 交互 REPL（前端①） |
| `semantics/legality.py` | 进程外跑 `CanAttack`（只挑不判） |
| `kardsmem/matchlog.py` | **对局动作流** ＝「历史」和「动作回执」的来源（§7.6g） |
| `kardsmem/locres.py` | 本地化表 → 英文转中文（**内存里读出来的一律是英文**） |

★ 三条已被实机验证的关键结论：
1. **动作成没成看动作流，不看副作用。**「手牌有没有少一张」是拿副作用猜因果。
2. **动作流回答"成没成"，提示回答"为什么没成"**（拒绝理由不是 action，不进动作流）。
3. **提示轮询要用槽位差分**（比 `FUObjectItem` 字节）。只扫"新增的那一段"会漏 ——
   UE 在 GC 后复用数组**中间**的空闲槽位。实测 200Hz。

**判据的用法**：进程外重算合法性只用来**排序和挑选**，**绝不用来否决**动作 —— 缺漏是必然的
（卡池两千张、效果互相叠、还有原生代码）。动作发出去，让游戏裁决，再读 `notify` 拿权威理由。

**2026-09-24 凌晨（无人值守，~3h）已交付**——两局真实 training 对局全程实测，
不是离线断言。这轮的主题是**把 §11 清单里"看起来缺"的东西一个个查实**：

- **`CanAttack` 裸 return 读法修正**：全函数唯一没显式赋值的两条出口都是
  "没有更多限制了"（不是"没跑到终点"），改完后 `semantics/legality.py` 的
  "不知道"从 5/15 明显收窄。
- **`ops.act_attack`/`act_move` 补齐 `force` 越过口**（§7.6f 的核心纪律，之前
  只有费用检查有、pin/guard/deployment_sickness/target_blockers 都没有）——
  实机验证过：不带 force 拦得住，带 force 真的越过并让游戏自己拒绝。
  `interfaces/shell.py`/`interfaces/mcp.py` 两个前端都补了对应的透传（各修一处漏传）。
- **`interfaces/mcp.py` 真机连通测出一个协议级 bug**：`ops.py` 的裸 `print()`
  混进 JSON-RPC 的 stdout，任何真客户端一读就炸——这是 `interfaces.mcp` 本轮
  才第一次真正被连起来测（之前只过了导入检查）。已用 `redirect_stdout` 修。
- **`failReason`/被贴效果的能力名改走 `Game.locres` 权威链**（不再是手写猜的
  中文），新增 `kardsmem.locres.namespace_zh()` 通用查法。
- **补齐 3 个原语**：`getHasGameplayTag`（`FGameplayTagContainer@0x30`）、
  `getTotalHeavyArmor`、`isBuffedByCard`/`getCardsBuffedByThisCard`——
  全部是"字段其实早就有人读了，`cardnatives.NOT_READABLE` 没查就写了理由"
  这个模式，教训见下面弯路 #12。
- **`card_targets.py` 挖出一个更大的 bug**：`out()` 函数签名多写了个没用过的
  形参，导致 `simple()` 注册的**全部 20 个原语**不管卡数据是什么都回填
  `True`——`--coverage` 只查"跑不跑得动"从不查"答案对不对"，这条一直没暴露。
- **F3b 靠读字节码解决**（`ChangeUnitOwnership`）：`side_enum` 本来就跟着
  控制权走，`underEnemyControl` 只是旁路标记，`kardsmem.board` 不用改。
- **HQ 单列宽度**：找到很扎实的定量证据（`enemy_back` 变窄 ~11px，225 个样本），
  但 `local_back`（63 样本）完全没有这个效应——矛盾没解决，**没有**动
  `ops.ROW_SPECS`/`row_x`（这段代码直接决定鼠标点哪，宁可继续吃残差）。
- `keepOrder=false` 26 张：IDA 静态查过一轮，收窄成"读一次活对象的虚表槽位"，
  但没有再往下做——期间发现打开的 IDA 库其实是**错的构建**（1.57 而不是正在
  跑的 1.58.27125.launcher），详见弯路 #13。

这轮也交了两条新弯路，见上面「走过的弯路」#12（`NOT_READABLE` 字典没查就写）
和 #13（IDA 库构建没核对）。

**2026-09-24 下午（实机，用户在场操作）已交付**——主题是**把 §11.1 清单里
"NN 前该做的"逐条做完**，全部实机正负例验证过，不是只接了线：

- **三选一候选的身份与屏幕次序**（三选一/预报/`keepOrder=false` 类如"好人寥寥"）
  **统一解法**：`ABP_ChooseCardToSpawn_C`（继承 `ABP_BaseCard_C`）这个候选 UI
  actor 的通用类，`indexOfCardInDeck`/`Name_0`/`SelectedCard`/`cardBeingPlayedID`
  四个字段自证候选身份与屏幕位置，对 `keepOrder=true/false` 全部 36 张
  `GetChooseSpawnCards` 卡通用——不需要 `_forecastOptions`（那条路要先解决
  "哪张卡在 resolve" 这层间接，候选 actor 自己就有答案）。落地在
  `kardsmem/pick.py::forecast_candidates()`。**`keepOrder=true`**（OVERCAST→
  天气卡→热浪二级）和 **`keepOrder=false`**（好人寥寥→507th PIR/美国人卫队/
  593联合通信连）两条路都实机打出验证过。
- **R8 大坑：`MapProperty`/`TSet` 真实字节布局**（`kardsmem/containers.py`，新文件）：
  `TSparseArray` 表头恒 56 字节、`TSet`/`TMap` 表头恒 80 字节（都与元素类型无关），
  真正的稀疏判定要读 `AllocationFlags`（`FBitArray`）位图，不能当连续数组硬读。
  布局来自游戏**自带**的 Dumper-7 导出 `UnrealContainers.hpp`（这是个 fork，
  不是通用 UE 源码）。`props.py` 的反射链接上了 `MapProperty` 分支
  （`tools/canplay.py::_read_map`），端到端验证过（`CardFunctionTriggers`
  空表场景返回 `{}`）。
- **移动预检 + 攻击预检重构**：新增 `semantics.legality.Legality.can_move()`
  （游戏没有独立 `CanMove`，复用 `CanAttack`，只信任"这个单位还能不能行动"
  的子集 failReason）；`ops.act_attack` 里四段手写拦截（`can_act_now`/
  `is_pinned`/`target=="hq"` 特判/`target_blockers` 的 blocked 判据）合并成
  一条 `Legality.can_attack` 调用。**实机对称验证**：`DINGO`（Blitz）正例两次
  攻击都成功；`KINGFISHER`（无 Blitz，部署病）负例被拦、`force` 越过后游戏也
  拒绝、提示词跟判据理由一字对应；guard 场景（敌方 97th RIFLES 守护 HQ）下
  打总部被拦、`force` 越过游戏拒绝、直接打 guard 单位本身成功——重构前后
  行为一致，接进了 `interfaces/shell.py::do_front`（新增 `!` 强制语法）和
  `interfaces/mcp.py::t_move`。
- **出牌预检补齐**：新增 `semantics.legality.Legality.can_play_from_hand()`
  （跑该卡自己的 `CanPlayFromHand` 覆写，`target` 给了走 `GetTargetedCard`
  钩子），接进 `interfaces/shell.py::do_play`/`interfaces/mcp.py::t_play`（之前只有
  `ops.act_deploy` 自己的费用检查带 force，跟移动/攻击预检的完成度不对等）。
- **换牌确认判据修正**：`ops.act_mulligan_confirm()` 原先靠"手牌 actor 集合
  有没有变化"判成败，0 张标记时集合合理不变却被误判失败；改用
  `pick.in_mulligan()`（`AllMatchActions` 里有没有出现过
  `XActionStartOfTurn`）True→False 的转变判定。实机测过换牌多选回归
  （标两张、取消一张、`shouldDiscard` 全程读对）。
- **提示文本事件流三分类**：`kardsmem/notify.py` 新增 `classify()`，
  reject/banner/settlement 三分类，reject 类的 90 条权威原因数据驱动自
  `Game.locres` 的 `helpbubbles` namespace（`notify_*`/`must_target_*`/
  `you_cant_*`/`nation_has_no_*` 前缀），不再是手写枚举。
- **`ops.preflight()`**：§7.7 自检五步汇总成一条不动鼠标的命令，实机验证过。
- 两个子代理的产出：`customJson`（`0x518`，真实类型是 `FBlueprintJsonObject`
  不是 FString，之前文档写错了）43 个布尔标记解析（`kardsmem/cards.py`，
  结构验证过、缺实机数据）；`kardsmem/pick.py` 新增基于 `BP_Deck` 权威集合的
  手牌 actor 读法（同样结构验证过、缺实机数据）。

`agent/nn.py`（前端③）依然是 0 行——以上全部是它的前置工作，NN 本身
还没开始设计代码层面的东西（数据从哪来、状态/动作表示、模型结构目前
只在对话里讨论过，没有落成文档，是下一步要写的）。

## 近期大事（2026-09-22，读一次就够）

1. **公开库事故**：在 `kards-agent/` 里裸跑 `git push` 把**整个伞仓库**（2507 个文件 / 1.55 GiB）推上了公开库，
   约 2 分 50 秒后转 private、随即删库重建，现在只发 `kards-agent` 子树。
   留档：`reverse-data/reports/report/INCIDENT-2026-09-22-public-push.md`；规矩写在本文件「发布纪律」。
2. **大扫除**：kardsmem 里的**私服改造细节全清**（判据改成「SizeOfImage 决定 RVA 有效性，md5 只作信息」）；
   自动化/mem 工具**全部归位 `kards-agent/`**（`tools/` + `_archive/mem-era/`）；
   `reverse-data/tools/` 只剩逆向/静态/第三方。偏移记录并入 `kards-offsets.json` 的 `build.data`
   （照 Dumper-7 `OffsetsInfo.json` 格式），版本命名按 `GAME-VERSIONS.md` 的 `<版本号>.<渠道>`。
3. **上游对齐到 `576aa19`**（v0.1.8_beta）：`vendor/actions.py`、`vendor/deploy.py` 已更新；
   上游新增的**窗口消息输入**（不移动真实光标）已实机测过对 Kards 不生效（见上面「工作方式」
   前一节的 2026-09-23 记录）；`fallback_drop` 还没接，见 `vendor/README.md`。
4. **撤销了一条"硬约束"**：「读/执行后端可换（mem↔ocr）」不再是约束 —— OCR 只是核对手段，
   内存是唯一权威。唯一硬约束＝**读侧只读**。

## 会话与文档

| 想干什么 | 去哪 |
|---|---|
| 接续任务 | 本文件 → `kards-agent/TODO.md`（状态）→ `reverse-data/reports/report/*-1.60.md`（1.60 事实；旧主规格已标历史） |
| 读 exe 指纹 / 为什么 md5 对不上 | `reverse-data/reports/ledger/EXE-IDENTITY.md`、`python -m kardsmem exes` |
| 版本 ↔ dump ↔ 导出 | `reverse-data/reports/ledger/GAME-VERSIONS.md`（命名规则 `<版本号>.<渠道>`） |
| 游戏规则原文 | `reverse-data/reports/spec/KARDS-RULES-ENCYCLOPEDIA.md` |
| **NN 设计（前端③）** | `reverse-data/reports/spec/KARDS-NN.md`（数据从哪来 · 可见性 · 状态/动作表示 · 模型 · 评估） |
| 哪些卡自动化做不了 | `reverse-data/reports/ledger/CARD-AUTOMATION-COVERAGE.md` |
| 坐标/读图账本 | `reverse-data/reports/ledger/VISION-AND-COORDINATES.md` |
| 读 Claude/DSH 的会话日志（zstd jsonl → 可读 markdown） | `_session_claude/extract_claude.py`（Claude Code）· `_session_extract/extract.py`（DSH） |
| 发布（**只发 kards-agent 子树**） | 见下面「发布纪律」 |

## 待办（TODO）与文档维护规范（2026-10-02 定）

1. **`kards-agent/TODO.md` 是唯一的待办看板，只放没做完的事**；已完成的内容不留在里面。旧版整份归档在 `TODO-ARCHIVE-2026-10-02.md`（历史日志，**不要当待办读**）；成果留痕走 `PLAN.md` 的"回执台账"和各报告。
2. **每条待办 = 编号 + 事项 + 负责人 + 状态 + 指针**。负责人只有三类：**Claude**（含它派出的 subagent，最终都归它）、**DS**（DeepSeek：实现与实机跑局）、**用户**（拍板/操作）。状态：待做 / 进行中 / 待验证（代码已落地、缺实机）/ 阻塞。
3. **先改看板，再写细节**：新增、改状态、完成，都**先改 `TODO.md`**；细节写进指针指向的报告/文档，不把长篇日志写进看板。
4. **完成的流程**：验证通过 → 在 `PLAN.md` 回执台账登记一行（改动文件 + 证据）→ 从 `TODO.md` 删除该条（不要留"已完成"清单）。没有回执的条目不算完成。
5. **每个提交带上对应的看板更新**（同一提交或紧随其后的提交）；提交信息中文，讲清楚做了什么、为什么、证据，并标明"实机已验证 / 未验证"。
6. **新发现的问题**：先判断是否值得单独成条——是就在看板加一行（负责人/状态/指针），不是就写进相关报告；**不要只在对话里提**。
7. **需要用户拍板的事**放在 `TODO.md` 末尾"待用户拍板"，决定后删除并把结论写进相关文档。
8. 看板条目的指针要指向**仍然存在**的文档；文档改名/归档时同步更新指针。
9. 其它文档（`PLAN.md`、架构文档、报告）里写到"TODO §x"的，**§号指旧版 `TODO-ARCHIVE-2026-10-02.md` 的小节**，现行事项以 `TODO.md` 为准。

## 工作方式

- 用户在旁边看着屏幕。**开对局/动鼠标前先确认**。
- 自动脚本是未来计划，**不能是简单启发式** —— 别在现有那个残废的 `playturn` 上投入。
  收集大量数据时才用它，平时用户自己手打。
