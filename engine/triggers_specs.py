"""`engine.triggers` 的数据表 / 纯函数（P6 拆分，2026-10-06：原样从 `engine/triggers.py` 搬出）。

钩子规格表（`*_HOOK_SPECS`）、方法号、原生默认、`method_of`——全是常量与纯函数，不依赖 `triggers` 的运行时。
`engine.triggers` 把这里的名字全部再导出（旧 import 路径不变）。
"""
from __future__ import annotations

# EOnEnterPlayMethod（语义见文件头）
METHODS = {1: "OnPlayedFromHand", 2: "OnAddedFromHand", 3: "OnSpawn",
           4: "OnReveal", 5: "OnConverted"}
TRIGGER_ID = 0x2B                     # OnOtherCardEnterPlay 的触发号（CARD-PLAY-HOOKS 报告）
HOOK = "OnOtherCardEnterPlay"

# 作用在"别的牌身上"的钩子（自己打出一张牌时，场上/对方那些牌要看的那几个）。
# 用户 2026-10-02："对方反制对友方操作的影响"也要算 —— 反制就是这一族里的一个，
# 它的触发条件与效果同样写在蓝图里：**枚举该触发的钩子 + 跑字节码**即可，别在 eval 里手写规则。
HOOKS = {
    HOOK: ("cardPlayed", "Method"),                       # 进场（带方法号）
    "OnBeforeOtherCardPlayedFromHand": ("cardPlayed",),    # 出牌前（反制常挂这里）
    "OnOtherCardPlayedFromHand": ("cardPlayed",),          # 出牌后
    "OnCounterMeasureTriggered": ("countermeasureTriggering", "qqq"),   # 反制自身被触发
}
COUNTER_HOOKS = ("OnBeforeOtherCardPlayedFromHand", "OnOtherCardPlayedFromHand",
                 "OnCounterMeasureTriggered")

# ---- 攻击族（ATTACK-HOOKS-1.60.md：蓝图 AttackCard 字节码 + IDA 虚表逐条核实）----
# 每个钩子一条：形参**顺序与名字**（按名字传给 VM，名字写错 = 静默丢参）、出参、触发号、
# 来源强度。★ 同族钩子的 (attacker, defender) 顺序**不一致**，这是最容易写反的地方：
#   OnOtherCardAttacks / OnBeforeOtherCardAttacks / SwitchTarget：(cardAttacking, defender…)
#   OnAfterOtherCardAttacks：(defenderCard, attackerCard, damageToDefender, attackCost) ← 反过来
# kind: "native" = BlueprintNativeEvent（有 vtable 槽 + 原生默认）；"impl" = BlueprintImplementableEvent
#       （没有原生函数体，没覆写 = 什么都不发生，出参保持 0/null）。
ATTACK_HOOK_SPECS = {
    # 名字: dict(trigger=触发号|None, own=是不是"自己"钩子, params=形参(按序), outs=出参, kind, slot, ev=证据)
    "OnOtherCardAttackSwitchTarget": dict(
        trigger=0x1E, params=("cardAttacking", "oldDefender"), outs=("newDefender",),
        kind="native", slot=808, default_addr=0x144B045E0, ev="IDA(thunk 0x144A85010 -> vtable+808；默认 newDef=oldDef)"),
    "OnOtherCardAttacks": dict(
        trigger=0x1F, params=("cardAttacking", "defenderCard"), outs=("stopAttack", "AttackedAndStopped"),
        kind="native", slot=792, default_addr=0x144B045F0, ev="IDA(thunk 0x144A85150 -> vtable+792；默认两个 false)"),
    "OnBeforeAttack": dict(
        trigger=None, own=True, params=("defenderCard",), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项=ImplementableEvent) + 字节码 AttackCard@3590"),
    "OnBeforeOtherCardAttacks": dict(
        trigger=0x0D, params=("cardAttacking", "defenderCard"), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 AttackCard@3382"),
    "OnAfterAttack": dict(
        trigger=None, own=True, params=("defenderCard", "wasShockAttack", "attackCost"), outs=(), kind="impl",
        slot=None, ev="IDA(无 exec 表项) + 字节码 ExecuteOnAfterAttackEvents@440"),
    "OnAfterOtherCardAttacks": dict(
        trigger=0x04, params=("defenderCard", "attackerCard", "damageToDefender", "attackCost"), outs=(),
        kind="impl", slot=None, ev="IDA(无 exec 表项) + 字节码 ExecuteOnAfterAttackEvents@641"),
    "OnAttackStopped": dict(
        trigger=None, own=True, params=(), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + FModel ExecuteStoppedAttack / 字节码"),
    "OnReceiveDamage": dict(
        trigger=None, own=True, params=("fromCard", "Damage"), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteBeforeReceiveDamage@687"),
    "OnOtherCardReceiveDamage": dict(
        trigger=0x34, params=("fromCard", "toCard", "fromAttack", "Damage"), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteBeforeReceiveDamage@879"),
    "OnOperationKreditsSpent": dict(
        trigger=None, own=True, params=("kreditsSpent",), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteOnOperationKreditsSpent@38"),
    "OnOtherCardOperationKreditsSpent": dict(
        trigger=0x44, params=("cardOperated", "kreditsSpent"), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteOnOperationKreditsSpent@459；usedTriggers 里的枚举名是"
           " OnOtherCardOperationKreditSpent(少个 s)"),
    # ---- 伤害管线（CalculateDamageDealt / ExecuteOnDealDamageAddDamage* 里调的钩子）----
    "OnCardDealDamage_ModifyDamageDealt": dict(
        trigger=None, own=True, params=("toCard", "Damage", "fromAttack", "fromFight"), outs=("newDamage",),
        kind="native", slot=736, default_addr=0x144B045C0,
        ev="IDA 已验证(3 个 xref，exec 表项 0x147e19970；槽/默认体见 NATIVE-COVERAGE §3：默认 newDamage=Damage) + 字节码 ExecuteOnDealDamageAddDamage@692"),
    "OnOtherCardDealDamageAddDamage": dict(
        trigger=0x25, params=("cardDealingDamage", "toCard", "Damage", "fromAttack", "isDefenderDamage"),
        outs=("damageToAdd", "reRunAtEnd"), kind="native", slot=768, default_addr=0x144B04600,
        ev="IDA 已验证(3 个 xref，exec 表项 0x147e199b0；槽/默认体见 NATIVE-COVERAGE §3：默认两出参清零) + 字节码 ExecuteOnDealDamageAddDamage@382"),
    "OnDealDamageAddDamageAfterCalc": dict(
        trigger=None, own=True, params=("toCard", "Damage", "fromAttack", "isAttacker", "isRedirected"),
        outs=("damageToAdd",), kind="native", slot=784, default_addr=0x144B045D0,
        ev="IDA 已验证(3 个 xref，exec 表项 0x147e19980；槽/默认体见 NATIVE-COVERAGE §3) + 字节码 ExecuteOnDealDamageAddDamageAfterCalc@742"),
    "OnOtherCardDealDamageAddDamageAfterCalc": dict(
        trigger=0x26, params=("cardDealingDamage", "toCard", "Damage", "fromAttack", "isRedirected"),
        outs=("damageToAdd", "stopAdding"), kind="native", slot=776, default_addr=0x144B04600,
        ev="IDA 已验证(3 个 xref，exec 表项 0x147e199c0；槽/默认体见 NATIVE-COVERAGE §3) + 字节码 ExecuteOnDealDamageAddDamageAfterCalc@1219/1748"),
    "GetPassiveDefenseBuff": dict(
        trigger=None, own=True, params=("incomingDamage",), outs=("amount",), kind="native", slot=856,
        default_addr=0x1415A3F50, ev="IDA 已验证(3 个 xref，exec 表项 0x147e195c0；槽/默认体见 NATIVE-COVERAGE §3：默认 amount=0) + 字节码 CalculateDamageDealt@1653；"
                                     "incomingDamage 实参取值未确认(我们传对方的计算伤害)"),
    "GetBeforeAttackAttackBuff": dict(
        trigger=None, own=True, params=("asAttacker",), outs=("amount",), kind="native", slot=840,
        default_addr=0x1415A3F50, ev="IDA 已验证(3 个 xref，exec 表项 0x147e19410；槽/默认体见 NATIVE-COVERAGE §3) + 字节码 CalculateDamageDealt@4023；真攻击传 "
                                     "applyBeforeAttackBuffs=False ⇒ 不会被调（UI 预览才调）"),
    "GetBeforeAttackDefenseBuff": dict(
        trigger=None, own=True, params=("asAttacker",), outs=("amount",), kind="native", slot=848,
        default_addr=0x1415A3F50, ev="同上"),
    "GetBeforeAttackDamage": dict(
        trigger=None, own=True, params=("asAttacker",), outs=("amount",), kind="native", slot=864,
        default_addr=0x1415A3F50, ev="同上"),
    # ---- 造成/幸存/摧毁链 ----
    "OnCardDealDamage": dict(
        trigger=None, own=True, params=("toCard", "Damage", "isCombatDamage", "CounterDamage", "isRedirected"),
        outs=("qqq",), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardDealDamageEffects@1198"),
    "OnOtherCardDealDamage": dict(
        trigger=0x24, params=("cardDealingDamage", "toCard", "Damage", "isCombatDamage", "CounterDamage",
                              "isRedirected"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardDealDamageEffects@980"),
    "OnSurvivedCombat": dict(
        trigger=None, own=True, params=("cardCombated",), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteOnSurvivedCombatEvents@549"),
    "OnOtherCardSurvivedCombat": dict(
        trigger=0x3B, params=("survivor", "cardCombated"), outs=(), kind="impl", slot=None,
        ev="IDA(无 exec 表项) + 字节码 ExecuteOnSurvivedCombatEvents"),
    "OnBeforeDestroyed": dict(
        trigger=None, own=True, params=("killer", "TriggerNotDestroyed"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnBeforeOtherCardDestroyed@520"),
    "OnBeforeOtherCardDestroyed": dict(
        trigger=0x0F, params=("cardDestroyed", "killer", "TriggerNotDestroyed", "destroyedInCombat"), outs=(),
        kind="impl", slot=None, ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnBeforeOtherCardDestroyed"),
    "OnLeaveBoardOrOwner": dict(
        trigger=None, own=True, params=("goingToLocation", "leavePlayMethod"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnBeforeLeaveBoardOrOwnerEvents@845"),
    "OnOtherCardLeaveBoardOrOwner": dict(
        trigger=0x2E, params=("cardLeaving", "goingToLocation", "Method"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnBeforeLeaveBoardOrOwnerEvents"),
    "OnCardLocationMoved": dict(
        trigger=None, own=True, params=("OldLocation", "NewLocation", "ChangeOwner", "MoveReason"), outs=(),
        kind="impl", slot=None, ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardLocationMoved@515；MoveReason 是字符串"),
    "OnOtherCardLocationMoved": dict(
        trigger=0x2F, params=("cardMoved", "OldLocation", "NewLocation", "ChangeOwner", "MoveReason"),
        outs=(), kind="impl", slot=None, ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardLocationMoved@771"),
    "OnAfterLeaveBoard": dict(
        trigger=None, own=True, params=("goingToLocation",), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnAfterLeaveBoardOrOwnerEvents@836"),
    "OnAfterOtherCardLeaveBoardOrOwner": dict(
        trigger=0x08, params=("cardLeaving", "OldLocation"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnAfterLeaveBoardOrOwnerEvents"),
    "OnDestroyed": dict(
        trigger=None, own=True, params=("killer", "TriggerNotDestroyed"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardDestroyedFunction@667/893"),
    "OnOtherCardDestroyed": dict(
        trigger=0x27, params=("cardDestroyed", "killer", "TriggerNotDestroyed", "destroyedLocation",
                              "selfIsAlsoGettingDestroyed", "destroyedInCombat"), outs=(), kind="impl",
        slot=None, ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardDestroyedFunction@1461/1856"),
    "OnAfterDestroyed": dict(
        trigger=None, own=True, params=("killer", "TriggerNotDestroyed"), outs=(), kind="impl", slot=None,
        ev="IDA 已验证(名字串只有 2 个 xref、无 exec 表项 ⇒ 非原生) + 字节码 ExecuteOnCardDestroyedFunction@2636"),
    "OnDestructionEffectTriggered": dict(
        trigger=0x18, params=("cardTriggered", "instigatorID", "SelfAlsoDestroyed"), outs=("TriggerMultiple",),
        kind="impl", slot=None, ev="IDA 已验证(2 个 xref、无 exec 表项)；★ run_attack_hooks 未实现（摧毁效果倍增，4 张卡）"),
}
# 兼容旧名：触发号 → 形参表（旧版里 0x0D 的形参名写成 attackerCard，是错的 ⇒ VM 收不到参数）
ATTACK_HOOKS = {h: sp["params"] for h, sp in ATTACK_HOOK_SPECS.items() if sp["trigger"] is not None}
ATTACK_OWN_HOOKS = tuple(h for h, sp in ATTACK_HOOK_SPECS.items() if sp.get("own"))

# ---- 上线族（0x32）：`BP_CardFunctions::ExecuteOnMoveToFrontlineCardEffects(cardMoved, forceMove,
#      moveCost)`（本构建导出的 BP 反编译 @15297，逐行读过）----
#   顺序：`SetStopFurtherActions(false)` → **反制先手**（`GetActiveGotchasOrdered`，每张调用前先查
#   `GetStopFurtherActions`）→ 再查一次 → `cardMoved->location == 7` 才继续 →
#   **未被压制**才跑自己的 `OnMoveToFrontline(forceMove, moveCost)` → `cardsDone += 自己` →
#   `IsUnrevealedCovertCard(cardMoved)` 为真 ⇒ **0x32 段整段跳过** →
#   0x32 列表（按触发表序，`cardsDone` 去重 = 反制已经算过的跳过）。
#   ★ 未建模：`GetStopFurtherActions` 的**状态**（它不在 `effectvm` 的拦截表里，读不到谁置位）
#     ⇒ 实现在 `run_move_frontline` 的返回 meta 里如实标注（EVAL-ARCHITECTURE §7 未查第 1 条）。
MOVE_HOOK_SPECS = {
    "OnMoveToFrontline": dict(
        trigger=None, own=True, params=("forceMove", "moveCost"), outs=(), kind="impl", slot=None,
        ev="BP ExecuteOnMoveToFrontlineCardEffects@15297；自己（未压制才调）"),
    "OnOtherCardMoveToFrontline": dict(
        trigger=0x32, params=("cardMoved", "forceMove", "moveCost"), outs=(), kind="impl", slot=None,
        ev="同上 0x32 段；cardsDone 去重（反制已经算过的跳过）；隐蔽未揭示 ⇒ 整段跳过"),
}
MOVE_HOOKS = {h: sp["params"] for h, sp in MOVE_HOOK_SPECS.items()}
MOVE_ALL_HOOKS = ("OnMoveToFrontline", "OnOtherCardMoveToFrontline")

# ---- 抽到族（0x2A）：`BP_CardFunctions::ExecuteOnDrawnFromDeck(DrawnCardID, DrawnSide,
#      StartOfTurnDraw, &newlyDrawnCard)`（本构建导出 @13783，逐行读过）----
#   顺序：`GetCardFromID(DrawnCardID)` → 抽到那张自己的 `OnCardDrawnFromDeck(StartOfTurnDraw, DrawnSide)`
#   → `FetchAllCardsWithEventTrigger(0x2A)` 列表里 **cardID != DrawnCardID** 的逐个
#   `OnOtherCardDrawnFromDeck(DrawnCardID, StartOfTurnDraw, DrawnSide)`（抽到那张自己跳过）。
#   ★ 抽到那张**自己**的钩子在评估里走 `_on_draw`（模板摘要，手写源）—— 本轮补的是**旁观者**这一段。
DRAW_HOOK_SPECS = {
    "OnCardDrawnFromDeck": dict(
        trigger=None, own=True, params=("StartOfTurnDraw", "DrawnSide"), outs=(), kind="impl", slot=None,
        ev="BP ExecuteOnDrawnFromDeck@13799（抽到那张自己的钩子；评估里由 _on_draw 摘要承担）"),
    "OnOtherCardDrawnFromDeck": dict(
        trigger=0x2A, params=("drawnCardID", "StartOfTurnDraw", "drawnSide"), outs=(), kind="impl", slot=None,
        ev="BP ExecuteOnDrawnFromDeck@13801-13846；列表里 cardID == 抽到的那张 ⇒ 跳过（自己已经跑过）"),
}
DRAW_ALL_HOOKS = ("OnCardDrawnFromDeck", "OnOtherCardDrawnFromDeck")

# ---- 压制/定住族（0x3A / 0x3D）：都是"以某张牌为对象"的单钩子族，顺序 = 触发表序 ----
#   0x3A `OnOtherCardSuppressed(card)`：`SuppressMultipleUnits`（本构建 BP 导出 @7456，
#        新压制与已压制两条路最后都落到同一段 fetch 0x3A 上）。
#   0x3D `OnOtherUnitPinned(card)`：`PinUnit`（@971-1100，`pinnedTurns = max(pinnedTurns, 3|2)`
#        之后 fetch 0x3D）。
#   两族的列表都由 `FetchAllCardsWithEventTrigger` 出 ⇒ 被压制的牌默认被丢掉
#   （`suppressionExceptionTriggers` 里没有这两个触发号），与 `find_cards(skip_suppressed=True)` 一致。
SUPPRESS_HOOK_SPECS = {
    "OnOtherCardSuppressed": dict(trigger=0x3A, params=("card",), outs=(), kind="impl", slot=None,
                                  ev="BP SuppressMultipleUnits@7456-7530；每张 OnOtherCardSuppressed(_card)"),
}
PIN_HOOK_SPECS = {
    "OnOtherUnitPinned": dict(trigger=0x3D, params=("card",), outs=(), kind="impl", slot=None,
                              ev="BP PinUnit@971-1100；每张 OnOtherUnitPinned(_card)"),
}

# ---- 回合族（0x14 / 0x40 / 0x19）：本构建 BP 导出逐行读过 ----
#   0x14 `OnBeforeStartOfTurn()`：`ExecuteBeforeStartOfTurnEvents`@13709 —— 0x14 列表**单趟**、**无参**。
#   0x40 `OnStartOfTurn(turnNumber)`：`ExecuteStartOfTurnEvents`@11795 —— **三趟**：
#        CustomName1 含 `startofturn0` → 含 `startofturn1` → 其余（列表 = 触发表序）。
#   0x19 `OnEndOfTurn(turnNumber)`：`ExecuteEndOfTurnEvents`@13081 → `ExecuteEndOfTurnQueue`@13142：
#        即时（不含 endofturn1/2）→ `endofturn1` → `endofturn2`；之后**重新 fetch**，把这一趟里
#        新登记进来的牌再跑一遍（`RecursionLoop` ≤ 5），但 `card_unit_mosquito_*` 四张**跳过**；
#        最后 `RemoveBuffsEndOfTurn()`（`AddAttackUntilEndOfTurn` 的临时攻在本回合钩子之后才消失）。
#   ★ 静态快照下"新登记"不会发生 ⇒ 递归在预计算里是空转（docstring 写明）；`RemoveBuffsEndOfTurn`
#     属于"回合结束的收尾规则"，本 runner **不做**（那是 Sim 的状态收尾，留给回合结算那一步）。
TURN_HOOK_SPECS = {
    "OnBeforeStartOfTurn": dict(trigger=0x14, params=(), outs=(), kind="impl", slot=None,
                                ev="BP ExecuteBeforeStartOfTurnEvents@13709（0x14 列表单趟、无参）"),
    "OnStartOfTurn": dict(trigger=0x40, params=("turnnumber",), outs=(), kind="impl", slot=None,
                          ev="BP ExecuteStartOfTurnEvents@11795（三趟：startofturn0 → startofturn1 → 其余）"),
    "OnEndOfTurn": dict(trigger=0x19, params=("turnnumber",), outs=(), kind="impl", slot=None,
                        ev="BP ExecuteEndOfTurnEvents@13081 + ExecuteEndOfTurnQueue@13142"
                           "（即时 → endofturn1 → endofturn2；递归≤5；mosquito 四张跳过；RemoveBuffs 最后）"),
}
TURN_ALL_HOOKS = tuple(TURN_HOOK_SPECS)
MOSQUITO_SKIP = ("card_unit_mosquito_fighter", "card_unit_mosquito_fighter_bal",
                 "card_unit_mosquito_bomber", "card_unit_mosquito_bomber_bal")

# ---- 揭示族（0x37 + 自己那条）：`BP_CardFunctions::RevealCard(cardID, instigatorID, &qqq)`@9191 ----
#   顺序（逐行读过）：`isRevealed=true`、`hasCovert=false`、`UpdateGuarded(location)` →
#   `NotifyRevealCard`（通知，表现层，忽略）→ **未被压制**才 `cardRevealed->OnCardRevealed()` →
#   fetch **0x37** 逐张 `OnOtherCardRevealed(cardRevealed)` → `cardRevealed->OnEnterPlay(4)` →
#   fetch **0x2B** 逐张 `OnOtherCardEnterPlay(cardRevealed, 4)`（这一段就是进场族，`method=4`）。
REVEAL_HOOK_SPECS = {
    "OnCardRevealed": dict(trigger=None, own=True, params=(), outs=(), kind="impl", slot=None,
                           ev="BP RevealCard@9191：自己的 OnCardRevealed()（未被压制才跑）、无参"),
    "OnOtherCardRevealed": dict(trigger=0x37, params=("cardBeingRevealed",), outs=(), kind="impl",
                                slot=None,
                                ev="BP RevealCard@9191：fetch 0x37 → 逐张 OnOtherCardRevealed(cardRevealed)"),
}

# ---- 离开前线族（0x31）：`BP_CardFunctions::ExecuteOnCardMoveFromFrontline(cardMoved)`@14281 ----
#   顺序（逐行读过）：IsActionProcess 闸门 → **未压制**才 `cardMoved->OnMoveFromFrontline()`（无参）
#   → fetch **0x31** 逐张：cardID == 自己 的**跳过**；每张前查 `GetStopFurtherActions`（未建模，见 §7）
#   → `OnOtherCardMoveFromFrontline(cardMoved)`。
MOVE_FRONT_HOOK_SPECS = {
    "OnMoveFromFrontline": dict(trigger=None, own=True, params=(), outs=(), kind="impl", slot=None,
                                ev="BP ExecuteOnCardMoveFromFrontline@14281（未压制才跑自己的，无参）"),
    "OnOtherCardMoveFromFrontline": dict(trigger=0x31, params=("cardMoved",), outs=(), kind="impl",
                                         slot=None,
                                         ev="同上：fetch 0x31 → 逐张 OnOtherCardMoveFromFrontline(cardMoved)，"
                                            "cardID==自己 的跳过"),
}

# ---- 撤退族（0x36）：`BP_CardFunctions::ApplyMakeCardRetreat(cards, instigatorID)`@16567 ----
#   逐张（location ∈ {5,6,7}）：IsActionProcess → **未压制**才 `OnBeforeRetreat(&stopAction)`；
#   `stopAction` 真 ⇒ **直接 return**（撤退中止）→ fetch **0x36** 逐张
#   `OnOtherCardRetreat(tmpCard, &stopAction)`（**不排除自己**）；某张返回 stopAction 真 ⇒
#   客户端打日志 "ON OTHER RETREAT STOP ACTION NOT IMPLEMENTED YET" 并 **return**。
RETREAT_HOOK_SPECS = {
    "OnBeforeRetreat": dict(trigger=None, own=True, params=(), outs=("stopAction",), kind="impl",
                            slot=None,
                            ev="BP ApplyMakeCardRetreat@16751（未压制才跑；stopAction=true ⇒ 整次撤退中止）"),
    "OnOtherCardRetreat": dict(trigger=0x36, params=("cardRetreated",), outs=("stopAction",),
                               kind="impl", slot=None,
                               ev="BP ApplyMakeCardRetreat@16661-16681（不排除自己；true ⇒ 记日志并 return）"),
}

# ---- 转化族（0x22）：`BP_CardFunctions::ConvertCard(cardIDs, instigatorID, convertToCardName, convertIntoCardID,
#      skipTrigger, &newCardIDs)`（BP_CardFunctions.cpp:12611，本构建导出逐行读过）----
#   顺序（见 `run_convert`）：旧牌（在场的）`ApplyRemoveCardFromBoard(converting=true)` → 新牌 `CreateCard` →
#   新牌 `ExecuteOnEnterPlayEvents(5)`（在场才跑）→ `skipTrigger` 假时 fetch 0x22 逐张 `OnOtherCardConverted`。
#   只有 3 张牌覆写 0x22（BERGMANN BATTALION / 312th NOVGOROD / 51st RIFLE BRIGADE）；它们只用 `instigatorID` 判阵营。
CONVERT_HOOK_SPECS = {
    "OnOtherCardConverted": dict(trigger=0x22, params=("oldCardIDs", "newCardIDs", "newCardName", "instigatorID"),
                                 outs=(), kind="impl", slot=None,
                                 ev="BP ConvertCard:12890；cardID∈newCardIDs 的跳过；SDK card_unit_bergmann_battalion_classes.hpp 形参名"),
}
# `ConvertCard` 里旧牌离场用的 EOnLeavePlayMethod / ECardMoveReason 值（`ApplyRemoveCardFromBoard`，:18028/:18043）：
#   destroyed ? 1 : converting ? 6 : 2；移动原因 converting ? 0xD("Convert") : destroyed ? 0xA("Destroyed") : 0xB("Removed")
#   （CardMoveReason.uexp 的显示名表：值 10=Destroyed 11=Removed 12=Uncategorized 13=Convert，已对过）。
LEAVE_METHOD_CONVERT = 6
MOVE_REASON_NAME = {0xA: "Destroyed", 0xB: "Removed", 0xD: "Convert"}

# ---- 能力变化族（0x1D）：`ExecuteOnOtherCardsAbilitiesChanged(CardChanging)`（BP_CardFunctions.cpp:20885）----
#   fetch 0x1D 逐张 `OnOtherCardAbilitiesChanged(CardChanging)`（**不排除自己**，没有任何额外条件）。
#   调用点（原文）见 `ABILITIES_CALLERS`：每个都带自己的前置条件，sim 侧按条件消费。
ABILITIES_HOOK_SPECS = {
    "OnOtherCardAbilitiesChanged": dict(trigger=0x1D, params=("cardChanging",), outs=(), kind="impl", slot=None,
                                        ev="BP ExecuteOnOtherCardsAbilitiesChanged:20885；SDK card_unit_kv_1s_classes.hpp 形参名"),
}
# 原文里确实调用 ExecuteOnOtherCardsAbilitiesChanged 的函数 → 条件（BP_CardFunctions.cpp 行号）。
#   Give* 只在「原先没有」时才改旗标并广播；Remove* 只在「移除后确实没了」时广播（RemoveAmbush 例外：移除成功就广播）。
ABILITIES_CALLERS = {
    "GiveSmokescreen": "原先无 smokescreen（:1860）", "GiveGuard": "原先无 guard（:1970）",
    "GiveFury": "原先无 fury（:2080）", "GiveBlitz": "有效 getHasBlitz 与调用前不同（:2198）",
    "GiveAmbush": "原先无 ambush（:2298）", "GiveMobilize": "原先无 mobilize（:3501）",
    "GiveShock": "原先无 shock（:9906）",
    "RemoveSmokescreen": "移除后 getHasSmokescreen 为假（:1349；之前还有 0x30 OnOtherCardLoseSmokescreen）",
    "RemoveGuard": "移除后 hasGuard 为假（:1524）", "RemoveAmbush": "移除成功（:1644）",
    "RemoveFury": "移除成功（:2931；无 fury 时先修 attackLeft，有没有剩余来源都广播）",
    "RemoveMobilize": "hasMobilize 置假（:3641）",
    "RemoveBlitz": "移除成功后（:4200；blitz 有效值变了才先发 0x21 OnOtherCardBlitzChanged，广播本身无此条件）",
    "RemoveShock": "移除成功（:10037）",
    "ChangeHeavyArmor": "valueChanged ∧ 非 skipAction ∧ 不在牌库（:10381）",
    "MakeVeteran": "末尾，0x20 之后（:7280）", "MakeCountAsTank": "加完 isAlsoTank 等之后（:12072）",
    "GiveBond": "（:22966）",
}
# sim 的关键词名 → 原文里调用点所属的 Give*/Remove*（sim 里 immune/alpine/salvage 的赋予/移除**不在**上表 ⇒ 不广播）
ABILITIES_KW = ("smokescreen", "guard", "fury", "blitz", "ambush", "mobilize", "shock")

# 原生默认（BlueprintNativeEvent 没覆写时的行为；出参先被 thunk 清零，默认实现只写这里列的）。
# ImplementableEvent 没有原生默认：没覆写 = 空操作、出参保持 0/null ⇒ 不进这张表。
# 可直接粘进 VM 原生表（`cardnatives` / `vm.hooks`）的等价写法见 ATTACK-HOOKS-1.60.md §5。
NATIVE_DEFAULTS = {
    "OnOtherCardAttacks": lambda a: {"stopAttack": False, "AttackedAndStopped": False},
    "OnOtherCardAttackSwitchTarget": lambda a: {"newDefender": a.get("oldDefender")},
}


def native_default(hook: str, args: dict) -> dict:
    """某个钩子的原生默认出参（没有原生默认的钩子 ⇒ {}）。"""
    f = NATIVE_DEFAULTS.get(hook)
    return dict(f(args)) if f else {}


def method_of(kind: str) -> int:
    """我们动作流里的 kind → EOnEnterPlayMethod。认不出 ⇒ 1（从手牌打出，最常见）。"""
    k = (kind or "").lower()
    if k in ("play", "play_unit", "play_event", "order", "hand"):
        return 1
    if k in ("spawn", "summon", "create"):
        return 3
    if k in ("reveal",):
        return 4
    if k in ("convert", "transform"):
        return 5
    return 1
