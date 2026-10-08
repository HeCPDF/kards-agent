# 座位迁移：去掉 local / enemy，统一用游戏的 ESide + mySide

> 用户 2026-10-03：“游戏自身是通过 mySide 和座位枚举值（先手和后手两个 int）来分的。为什么用 enemy local？还需要每次识别一下。”
> 核实：游戏里本地玩家的座位是 `BP_Logic_C::mySide`（1/2，与 `ESide` 同值；`kardsmem/matchlog.py::MatchLog.my_side()` 读它），
> 另有 GameState 回合奇偶反推（`board.read_my_side`）。`ABP_Board_C+0x3C8` 已证伪，不是 mySide。

## 契约（所有层共用）

1. **座位 = `kardsmem.gamemodel.ESide`**（`left=1`、`right=2`，IntEnum，所以和 int 1/2 可互换比较、可作 dict 键）。
   不再有字符串 `"local"` / `"enemy"`，不再有常量 `LOCAL` / `ENEMY`。
2. **“我方”只是一个谓词**：`card.side == st.my_side`（`BoardState.my_side` / `GameState.mySide` / `Sim.me`）。
   `my_side` 每局读一次，随快照带着；读不出就是 `None`——要“按我方取牌”的函数在 `None` 时**抛 `ValueError`**，不兜底、不默认 1。
   对方 = `gamemodel.other_side(x)`（`BoardState.other_side` / `Sim.opp`）。
3. **BoardState**（`kardsmem/board.py`，已改完）：`my_side: ESide|None`；`kredits/slots/slots_lost/fatigue/hq` 键是 `ESide`；
   `frontline_owner: ESide|None`；`hand()/board()/support()/field_units()/discard()` 参数是 `ESide`（缺省 = 我方）。
   `my_side_raw` 是只读别名（int）。**`our_side` 已删。**
4. **Card**（已改完）：只剩 `uid / obj / fname / raw`。`obj` 是原版 `BaseCardObject`。`card.side` 是 `ESide`（= `obj.side`）。
   **`card.location` 已删**：位置看 `obj.Location`（`ECardLocation`）或谓词 `obj.InHand() / InFrontline() / InSupportLine() / IsHQ() / IsFieldUnit() / InDiscard()`。
   其余旧字段（`card_id/name/card_type/attack/defense/kredit_cost/operation_cost/is_suppressed/keywords/...`）保留为 `obj` 的只读别名，
   `card.card_type` 是 `EType.name`（`"infantry"` …）。**这些别名是过渡，新代码优先直接写 `obj.xxx`。**
5. **sim**（`Sim`/`U`/`H`）：座位也是 `ESide`；`Sim.my_side` 在构造时给（`from_cards(my_side=...)` 已传），属性 `Sim.me` / `Sim.opp`。
   `U.row` 仍是 sim 内部的 `"frontline"/"back"`（那是 sim 的行概念，不是座位），`U.typ` 仍是类型名字符串。
6. **效果字典里带座位的值**（`end_match`、`playing_side`、`steal_to_deck`、`deck_shuffle`、`deck_add_side`、restrictions 的 `side`…）：
   由 `effectvm.to_effects(my_side=…)` 直接产出**真实座位 `ESide`**，engine 与 `sim.me` 比较；`"mine": True/False` 这类相对施法者的布尔不变。
7. **VM / cardnatives 边界**：游戏字节码拿到的座位本来就是 1/2 的 ESideEnum；凡是为了喂 VM 而把座位翻成 `"local"/"enemy"` 的地方，
   改成直接传 `ESide`。`kardsmem/cardnatives.py` 里的 `F(c,"side")=="local"` 一类按 `board.my_side` 比。
8. **面向人的显示**（面板、shell、MCP、日志）：可以显示“我方/对方”字样，但那只是**渲染**：由 `side == my_side` 现算，
   不存、不回传、不参与比较；落盘的 JSON 里座位写 `1/2` 与 `my_side`。
9. 守护：`tests/test_arch_rules.py` 加 AST 检查——任何 `.side == "local"/"enemy"`、`LOCAL`/`ENEMY` 名字、`.location ==`/`in ("hand",…)`
   的残留都算失败（迁移完成后启用；迁移期间用 `tools/seat_audit.py` 列出残留）。

## 顺序（自底向上，每一层测试全绿再开下一层）

| 层 | 内容 |
|---|---|
| ✅ L1a | `kardsmem/gamemodel.py`、`kardsmem/board.py` |
| L1b | `kardsmem/` 其余（cards / gs / cardnatives / pick / cli / snapshot / proc / matchlog / __init__） |
| L2a | `sim/` `evaluation/` `policy/`（含 boardeval 自检里的 LOCAL/ENEMY） |
| L2b | `semantics/`（含效果字典的座位值、legality） |
| L3  | `agent/` `learn/`（schema/encode/baselines） |
| L4  | `player/`（rule 最大：~120 处） |
| L5  | `interfaces/` `gui/` `tools/` `tests/` |

## 状态（2026-10-03）

全部层已迁完：`tests/test_seat_rules.py`（AST 检查）0 残留，`tests/run_all.py` 65 个全绿。
* 原版同名函数已进 `gamemodel`：`GameState.GetClientSide / GetOpponentSide / GetPlayingSide / IsClientPlaying`、`BaseCardObject.GetOppositeSide`
  （`BP_CardFunctions` 的 GetClientSide 等只是转调 `GameStateRef->…`；`GetOppositeSide` 是 `UBaseCardObject` 的原生方法，座位不是 1/2 时不写出参）。
* 允许名单（`ALLOW_TEXT`）：VM 变量种类 `ref.kind == "local"`、效果『目标阵营关系词』（`"target": "enemy"/"friend"`，相对施法者，不是座位）。
* 旧录制（`kards-data/nn/logs/policy-*.jsonl` 里字符串座位的样本）读不了：`learn.dataset` 记 `legacy_string_seats` 跳过。
* 静态卡（side=0）读不出座位时不再默认按我方算：`effectvm` 抛 Unimplemented（记缺口），要用 `field_overrides` 喂真实座位。
