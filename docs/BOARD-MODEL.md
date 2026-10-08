# board 数据结构：照游戏原版

> 用户 2026-10-03：“board 得使用原版一样的数据结构。”
> 原则同 `SIM-FIDELITY.md`：**名字、取值、容器都照游戏**，我们自己的派生量单独命名、明说是派生。

## 对照

| 游戏里 | Python（`kardsmem/gamemodel.py`） | 备注 |
|---|---|---|
| `ESideEnum` left=1 / right=2 | `ESide` | 游戏里**没有** local / enemy；本地是哪边由 `mySide` 决定 |
| `ECardLocationEnum` Hand_Left=3 … Board_Frontline=7, Discard=8 | `ECardLocation` | 支援线（含 HQ）= `Board_HQLeft/Right`；HQ 卡的 `Type` 是 `location` |
| `ETypeEnum` / `EFactionEnum` / `ERarityEnum` | `EType` / `EFaction` / `ERarity` | 取值逐字照 SDK |
| `UBaseCardObject`（`CardID`、`Location`、`side`、`locationNumber`、`attack`/`attackBuff`/`defense`、`kredits`/`kreditsBuff`、`operationCost`/`operationCostBuff`、`movementLeft`/`attackLeft`、`hasBlitz`…） | `BaseCardObject`（字段名同名） | `kredits` 是**基础值**，总费用用 `getTotalKredits()`（同 BP 的 `getTotal*`） |
| `ABP_GameState_Battle_C`（`currentTurn`、`startingSide`、`isLocalClientTurn`、`FrontlineOwner`、`FrontlineLimiters`、`CardFunctionTriggers`、`AllCardsInBattle`…） | `GameState`（字段名同名） | `AllCardsInBattle` 保持游戏里的枚举顺序（= 创建顺序） |
| BP 函数 `FetchCardsByLocation` / `GetCardFromID` / `IsFrontlineLimited` / `GetAllCardsOnBoard` … | 同名方法 | 容量（手牌 9、支援线含 HQ 5、前线 5/2）在这里 |
| `UBaseCardObject::IsUnit / IsAirUnit / IsGroundUnit / getTotalAttack …` | 同名方法 | |

读不出的字段是 `None`——不编。

## 现状与迁移（分四步，每步测试全绿再进下一步）

* **S1 ✓ 模型**：`kardsmem/gamemodel.py`（枚举 / `BaseCardObject` / `GameState` + 函数移植）。`tests/test_gamemodel.py`。
* **S2 ✓ 读侧产出**：`board.snapshot()` 同时产出原版结构：`BoardState.game`（`GameState`）与每张 `Card.obj`（`BaseCardObject`）。
  原有的归一化字段（`Card.side="local"/"enemy"`、`Card.location="hand"/"frontline"/"back"/"hq"`、`card_type` 字符串、`kredit_cost`…）
  **降级为派生视图**，由同一次读内存得到；合成目标自检核对两者同源。
* **S3 迁移消费者**（按依赖从底向上；每个包迁完删掉它对派生视图的依赖）：
  1. `sim/adapter.py`：`from_cards` 改成读 `card.obj` + `game.mySide`（`ESide` / `ECardLocation` / `EType`），容量判断改用 `GameState.FetchCardsByLocation` 同一套常量；
  2. `semantics/*`（legality / triggers / effectvm）：`.side` / `.location` / `.card_id` → `obj.side` / `obj.Location` / `obj.CardID`；
  3. `policy/*`、`player/rule.py`（34 处 `.side`/`.location`）、`agent/view.py`、`interfaces/*`、`learn/schema.py`；
  4. 测试里的假卡（`_C` / `C` 一类）改成 `BaseCardObject`。
  替换用谓词，不用字符串比较：`obj.side == st.game.mySide`（我方）、`obj.Location in (ECardLocation.Hand_Left, ECardLocation.Hand_Right)`（手牌）、
  `obj.Location == ECardLocation.Board_Frontline`、`obj.Location in (Board_HQLeft, Board_HQRight) and obj.Type != EType.location`（支援线单位）。
* **S4 删视图**：`Card` 的派生字段与 `BoardState` 的 `kredits[LOCAL]` 一类派生容器删掉；`BoardState.cards` 直接是 `game.AllCardsInBattle.values()`；
  “本地 / 对方”只在 `sim` 内部（它自己的 `U.side`）存在，由 adapter 按 `mySide` 翻译一次。

## 不变量（测试守着）

* `st.game` 与派生视图同源（`kardsmem.board --selftest`）；
* `AllCardsInBattle` 顺序不被重排（热浪一类 `GetRandomCard` 依赖它）；
* 容量常量只有一份（`gamemodel`），`sim.state` 的 `HAND_CAP` 等引用它。
