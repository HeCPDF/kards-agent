# 游戏机制事实（实机与字节码证据）

> 2026-09-26 起的实机结论，持续补充。来源均为游戏自己的蓝图字节码或实机观测。

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
  ★ **幽灵提示**（2026-10-07，静态 BP 证据，详见 TODO.md 同日条）：`isSelectingHandTarget`+`chooseOneActive` 在 `AddSubActionSelectHandTargetPending`
  （`BP_OnlineMatch.cpp:15596`，打出源卡时**无条件**置位）和 widget `Construct`（`ConfirmHandTargetButton_Widget.cpp:199`）置位，
  只有 widget `Destruct`（`:247`）复位 ⇒ widget 没走到 Destruct 就悬挂（`pick_pending` 是同一对旗标）。无合法目标时 Construct 自己跳过（`:216-226`，`doNext` 先置 `pendingKill=true`）。
  读侧：`ghost_reason`（`pendingKill`/销毁标志）或候选全不合法 ⇒ `hand_target.pending=False`、同源 `pick_pending` 一并摘掉；`IsInViewport` 仅诊断，**未实机验证**。
- **Bond**（Homefront）：`ability.bond`；**检查发生在「打出时」，不是回合开始**（2026-10-06 逐行读 BP 更正；旧说法"手中带 Bond 的牌在回合开始扣总部"**不成立**）：
  `BP_CardFunctions::CardPlayedFromHand`（1.60 `:18682-18700`）里 `HasBond(card) ∧ ¬activeBondFactions.Contains(card.faction)` ⇒
  `ApplyFatigueDamage(side, fromBond=true)`（当前疲劳计数的伤害、计数 +1，与空库抽牌共用；全导出 `fromBond=true` 只此一处调用点）；
  总部打爆 ⇒ 牌的效果不再执行。`activeBondFactions` 由 `SetActiveBondsAtStartOfTurn(side)`（`BP_GameState_Battle.cpp:2529`，
  `BP_Logic::StartTurnBySide :10001` 调）在**回合开始**重算 = 行动方场上（非未揭示隐蔽的）单位的国家集合，回合中不更新。
  提示"Your card with Bond deals {damage} Morale damage to your HQ."。⇒ 要么先有同国单位在场、要么用 RATIONING
  （"Choose one — Remove Bond from all cards in your hand / HQ +4 defense"）之类清掉：`RemoveBond` 写自定义能力 `bond_removed`。
  已移植：`engine/natives/bond.py`（`has_bond`/`active_bond_factions`/`bond_check_on_play`/`give_bond`/`remove_bond`），
  `sim` 的 deploy/order 路径原生走检查（旧效果键 `bond_fatigue` 已删），测试 `tests/test_bond_native.py`。
