"""场上"别的牌看到某张牌进场"的钩子 —— PLAN 阶段 5 第一版（`OnOtherCardEnterPlay`）。

游戏怎么做的（1.60 导出 + 之前 CARD-PLAY-HOOKS 报告）：
    * 触发时机：某张牌**进场**（EOnEnterPlayMethod）
          1 = OnPlayedFromHand（从手牌打出）
          2 = OnAddedFromHand（从手牌加入）
          3 = OnSpawn（生成）
          4 = OnReveal（揭示）
          5 = OnConverted（转化）
    * 谁被触发：场上**每一张定义了 `OnOtherCardEnterPlay` 覆写**的牌（不含进场那张自己）；
    * 顺序：`FetchAllCardsWithEventTrigger` 的顺序（我们近似成**快照里的卡序**，即创建顺序）；
    * RNG：**同一个 Action 共用一条 `cardsRandomStream`** —— 所以自己的钩子先跑，
      再按顺序跑别人的钩子，`Stream` 对象一路传下去，抽取数累加。

边界（PLAN §5.2 明确）：**指令类（本回合触发）做；反制（敌方回合触发）不做**。
本模块只做"读 + 空跑"（外部 Kismet VM），不注入、不写游戏内存。

用法：
    from semantics import triggers
    triggers.selftest()                                   # 离线（不需要游戏）
    triggers.find_cards(km, st)                           # 场上有哪些牌挂了钩子
    triggers.run_enter_play(km, st, card, method=1, stream=s)   # 跑一遍，拿效果摘要
    triggers.run_attack_hooks(km, st, atk, dfd, cost=1)         # 一次攻击上全部影响胜负的钩子（顺序=字节码）
    triggers.build_attack_fx(km, st, pairs, ...)                # → boardeval.from_cards(attack_fx=...)
    # 攻击链的逐钩子规格（形参顺序/出参/槽位/默认实现/证据）：ATTACK_HOOK_SPECS；详见 ATTACK-HOOKS-1.60.md
"""
from __future__ import annotations

from typing import Optional

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


class TriggerCache:
    """按类缓存 `OnOtherCardEnterPlay` 的 UFunction 指针（没有覆写 ⇒ 记 None，别反复找）。"""

    def __init__(self, km):
        self.km = km
        self._fn: dict = {}

    def fn_of_class(self, uc: int, hook: str = HOOK) -> int:
        key = (uc, hook)
        if key not in self._fn:
            from kardsmem import kismet
            try:
                f = kismet.find_function(self.km, uc, hook, inherited=False)
            except Exception:                                     # noqa: BLE001
                f = None
            self._fn[key] = f or 0
        return self._fn[key]


def _registry_order(cards) -> list:
    """`FetchAllCardsWithEventTrigger` 的顺序 = 触发表 TSet 元素数组顺序 = 卡牌**创建**顺序
    （本局内表只加不删，见 CARD-PLAY-HOOKS §0.2）= `AllCardsInBattle` 的枚举顺序。
    `board_api` 的 `st.cards` 是按 (side, location, slot, uid) 重新排过序的，不是这个顺序；
    读取器若在 `card.raw["enum_idx"]` 里留了枚举下标（BOARD-QUERY-NATIVES §6.1），这里按它排；
    缺任何一张的下标 ⇒ 保持快照顺序（如实退化，不猜）。"""
    cs = list(cards or [])
    idx = [(getattr(c, "raw", None) or {}).get("enum_idx") for c in cs]
    if cs and all(isinstance(i, int) for i in idx):
        return [c for _i, c in sorted(zip(idx, cs), key=lambda t: t[0])]
    return cs


# `suppressionExceptionTriggers`（卡面静态默认值；`BP_GameState_Battle::FetchAllCardsWithEventTrigger` 用它放过
# 被压制的牌）：1.60 全卡池只有 10 张卡写了，**触发号只有 OnEndOfTurn/OnStartofTurn/OnBeforeStartOfTurn/
# OnOtherCardDrawnFromDeck**，没有任何攻击链触发号；运行时也没有任何蓝图写它（grep 全导出）。
# 键 = 卡内部名，值 = 钩子名集合（钩子名与触发号枚举名一致）。
SUPPRESSION_EXCEPTIONS = {
    "card_unit_gordon_highlanders": {"OnOtherCardDrawnFromDeck"},
    "card_unit_infantry_regiment_36": {"OnEndOfTurn"},
    "card_unit_panther_a": {"OnStartofTurn"},
    "card_unit_kurmark_aufklarungs": {"OnEndOfTurn"},
    "card_unit_3rd_kure_snlf": {"OnEndOfTurn"},
    "card_unit_danuta": {"OnEndOfTurn"},
    "card_unit_92nd_naval_brigade": {"OnStartofTurn"},
    "card_unit_99th_kholm": {"OnStartofTurn"},
    "card_unit_kv_85": {"OnStartofTurn"},
    "card_unit_1st_marines": {"OnStartofTurn"},
    "card_unit_a20_havoc": {"OnEndOfTurn"},
}


def _suppression_excepted(c, hook: str) -> bool:
    """这张牌挂了 `hook` 且该触发号在它的 `suppressionExceptionTriggers` 里 ⇒ 被压制也照跑。

    ★ 2026-10-02（测试抓到的真 bug）：表里的键名沿用了**枚举写法**（`OnStartofTurn`/`OnEndofTurn`），
      而钩子名是蓝图写法（`OnStartOfTurn`/`OnEndOfTurn`）—— 大小写不一致 ⇒ 这 11 张例外牌
      **从来没生效过**（被压制时它们的回合钩子被丢）。⇒ 大小写不敏感比较。
    """
    nm = str(getattr(c, "fname", None) or getattr(c, "name", None) or "").lower()
    return any(h.lower() == hook.lower() for h in SUPPRESSION_EXCEPTIONS.get(nm, ()))


def _trigger_of(hook: str):
    """钩子名 → 触发号（注册表的键）；不明 ⇒ None。"""
    if hook == HOOK:
        return TRIGGER_ID
    spec = ATTACK_HOOK_SPECS.get(hook) or {}
    t = spec.get("trigger")
    return t if isinstance(t, int) else None


def find_cards(km, st, exclude=(), cache: Optional[TriggerCache] = None,
               hook: str = HOOK, sides=("local", "enemy"), on_board_only: bool = True,
               skip_suppressed: bool = False) -> list:
    """挂了 `hook` 那张覆写的牌 → `[(card, fn_ptr)]`，按触发表顺序（见 `_registry_order`）。

    `exclude`：要排除的卡指针（进场的那张自己）；
    `sides`：默认**双方都看**（对方反制要算进来）；`on_board_only=False` 时连手牌等位置一起看；
    `skip_suppressed`：`FetchAllCardsWithEventTrigger` 会丢掉 `isSuppressed` 的牌，除非该触发号在它的
    `suppressionExceptionTriggers` 里（`SUPPRESSION_EXCEPTIONS`，攻击链触发号上没有任何例外）。攻击链一律传 True。
    传了 `cache` 时顺带缓存 `指针 → 类`（一次决策里同一批牌要问几十个钩子，别每次都读内存）。
    """
    from kardsmem.objects import ObjectArray
    tc = cache or TriggerCache(km)
    oa = ObjectArray(km)
    memo = getattr(tc, "_cls", None)
    if memo is None:
        memo = tc._cls = {}
    # ★ 2026-10-02（`+hooks` 恒空的真因）：`exclude` 以前直接 `set()`，而调用方
    #   `rule._play_hooks_triggers` 传的是 `board_api.Card`（dataclass ⇒ 不可哈希）
    #   ⇒ 每次调用当场 `TypeError: unhashable type: 'Card'`。统一按 `_ptr_of` 归一成
    #   指针，既修 bug 也让"传卡对象"这种自然写法不再炸。
    skip = {p for p in (_ptr_of(x) for x in (exclude or ())) if p}
    out = []
    # ★ 游戏自己的注册表（`st.registry`＝`CardFunctionTriggers`）是"此刻哪些牌挂了这个触发"的权威：
    #   脚本可能是**中途接手**的，不能靠"从头看到了什么"。它也包含不在场的牌（弃牌堆里刚打出的指令、
    #   牌库里的天气牌……）——游戏的 `FetchAllCardsWithEventTrigger` 就是按它迭代、由每张牌自己的钩子体判活。
    #   顺序 = 注册表元素顺序。注册表读不出 / 触发号不明 ⇒ 退回按快照扫（旧行为）。
    trig = _trigger_of(hook)
    reg = getattr(st, "registry", None)
    if reg is not None and trig is not None:
        byid = {getattr(c, "card_id", None): c for c in (getattr(st, "cards", None) or ())}
        cand = [byid[i] for i in (reg.get(trig) or ()) if i in byid]
    else:
        cand = _registry_order(getattr(st, "cards", None))
        reg = None
    for c in cand:
        if skip_suppressed and getattr(c, "is_suppressed", None) and not _suppression_excepted(c, hook):
            continue
        if reg is None and on_board_only and getattr(c, "location", None) not in ("frontline", "back"):
            continue
        if sides is not None and getattr(c, "side", None) not in sides:
            continue
        p = (getattr(c, "raw", None) or {}).get("ptr")
        if not p or p in skip:
            continue
        uc = memo.get(p)
        if uc is None:
            uc = memo[p] = oa.class_of(p) or 0
        if not uc:
            continue
        f = tc.fn_of_class(uc, hook)
        if f:
            out.append((c, f))
    return out


def run_enter_play(km, st, entered, method: int = 1, stream=None,
                   my_side=None, read_hooks=None, exclude=(), budget_s: float = 1.0,
                   cache: Optional[TriggerCache] = None) -> dict:
    """跑一遍"某张牌进场"触发的所有钩子。

    `entered`：进场那张卡（快照里的 Card 对象，或它的指针）。
    `stream`：**同一条** `kardsmem.rng.Stream`（跨钩子共用；不传 = 种子未知，随机点枚举，不伪造确定结果）。
    返回 `{"hits": [{"name","ptr","eff","stopped","spread"}], "draws": n, "cards": m}`。
    """
    from semantics import effectvm as EV
    ptr = entered if isinstance(entered, int) else (getattr(entered, "raw", None) or {}).get("ptr")
    # ★ 种子未知时别拿 `Stream(0)` 伪造"确定"的随机结果（它会让 Recorder 走 exact 分支，按种子 0 的 LCG 给出
    #   一个看起来确定、实际是编的结果）；`None` ⇒ 随机点按"枚举/机会节点"记（不预测种子，见 effectvm 头注释）。
    st_stream = stream
    d0 = getattr(st_stream, "draws", 0)
    hits = []
    for c, _fn in find_cards(km, st, exclude=list(exclude) + ([ptr] if ptr else []), cache=cache):
        cp = (getattr(c, "raw", None) or {}).get("ptr")
        try:
            r = EV.record_effects(km, cp, ptr or 0, bool(ptr), HOOK, my_side,
                                  None, read_hooks=read_hooks, args={"cardPlayed": ptr or 0,
                                                                     "Method": int(method)},
                                  timeout_s=budget_s, rng_stream=st_stream,
                                  # ★ 2026-10-02：盘面级原语（IsSideActive…）要靠 BoardState；
                                  #   不传的话钩子会在第一条 IsSideActive 上如实停下（= 白跑）。
                                  board=st, my_seat=my_side)
        except Exception as e:                                    # noqa: BLE001
            r = {"eff": {}, "stopped": "%s: %s" % (type(e).__name__, str(e)[:60])}
        eff = dict(r.get("eff") or {})
        if eff or r.get("stopped"):
            hits.append({"name": getattr(c, "name", "?"), "ptr": cp, "eff": eff,
                         "stopped": r.get("stopped"),
                         "spread": bool(eff.pop("uncertain", None))})
    return {"hits": hits, "cards": len(hits),
            "draws": getattr(st_stream, "draws", 0) - d0}


def _run_hook(km, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
              st=None):
    """跑一张牌上的一个钩子 → (eff, stopped)。`c` 可以是卡对象，也可以是它的指针。"""
    from semantics import effectvm as EV
    cp = c if isinstance(c, int) else (getattr(c, "raw", None) or {}).get("ptr")
    r = EV.record_effects(km, cp, 0, False, hook, my_side, None, read_hooks=read_hooks,
                          args=dict(args), timeout_s=budget_s, rng_stream=stream,
                          # ★ 2026-10-02：同 `_run_hook_ex` —— 盘面级原语（IsSideActive…）
                          #   需要 BoardState；少了它，`+hooks` 族会在第一条 IsSideActive
                          #   上如实停住（实机探针 probe_hooks_deep2 抓到）。
                          board=st, my_seat=my_side)
    return dict(r.get("eff") or {}), r.get("stopped")


def run_play_hooks(km, st, played, method: int = 1, stream=None, my_side=None,
                   read_hooks=None, exclude=(), budget_s: float = 1.0,
                   cache: Optional[TriggerCache] = None, include_counter: bool = True) -> dict:
    """打出 `played` 这张牌时，**游戏会让别的牌跑的那些钩子**全跑一遍（双方都看）。

    覆盖：`OnOtherCardEnterPlay`（进场）+ 反制族（`OnBeforeOtherCardPlayedFromHand` /
    `OnOtherCardPlayedFromHand` / `OnCounterMeasureTriggered`）—— 用户 2026-10-02：
    "对方反制对友方操作的影响" 也要算，而反制的条件/效果同样在蓝图里，跑它就行。
    RNG 与其它钩子**共用同一条流**（`stream`）。
    返回 `{"hits": [{"hook","name","ptr","side","eff","stopped"}], "draws": n}`。
    """
    ptr = played if isinstance(played, int) else (getattr(played, "raw", None) or {}).get("ptr")
    # ★ 种子未知时别拿 `Stream(0)` 伪造"确定"的随机结果（它会让 Recorder 走 exact 分支，按种子 0 的 LCG 给出
    #   一个看起来确定、实际是编的结果）；`None` ⇒ 随机点按"枚举/机会节点"记（不预测种子，见 effectvm 头注释）。
    st_stream = stream
    d0 = getattr(st_stream, "draws", 0)
    hooks = ([HOOK] + list(COUNTER_HOOKS)) if include_counter else [HOOK]
    hits = []
    for hook in hooks:
        if hook == HOOK:
            a = {"cardPlayed": ptr or 0, "Method": int(method)}
        elif hook == "OnCounterMeasureTriggered":
            a = {"countermeasureTriggering": ptr or 0, "qqq": False}
        else:
            a = {"cardPlayed": ptr or 0}
        # ★ 2026-10-02（TODO A10 第二条 / THE POMPADOURS）：打出卡触发族**不按位置过滤** ——
        #   游戏的 `FetchAllCardsWithEventTrigger` 把挂了触发号的实例都给出来，
        #   由**钩子自己的判据**决定要不要生效：`THE POMPADOURS` 这类"在手牌中生效"的牌
        #   在 `OnOtherCardPlayedFromHand` 里判 `IsLocatedInHand` ⇒ 以前
        #   `on_board_only=True` 会把它们整类漏掉（HQ +1 防御、揭示自己都丢）。
        for c, _fn in find_cards(km, st, exclude=list(exclude) + ([ptr] if ptr else []),
                                 cache=cache, hook=hook, on_board_only=False):
            eff, stopped = _run_hook(km, c, hook, a, st_stream, my_side, read_hooks, budget_s,
                                     st=st)
            if eff or stopped:
                hits.append({"hook": hook, "name": getattr(c, "name", "?"),
                             "ptr": (getattr(c, "raw", None) or {}).get("ptr"),
                             "side": getattr(c, "side", None), "eff": eff, "stopped": stopped})
    return {"hits": hits, "draws": getattr(st_stream, "draws", 0) - d0, "cards": len(hits)}


def _ptr_of(x):
    """卡对象/指针 → 指针；None/0 ⇒ 0。"""
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    if isinstance(x, dict):
        return _ptr_of(x.get("ptr"))
    return (getattr(x, "raw", None) or {}).get("ptr") or 0


def _run_hook_ex(km, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
                 hq_own=(), hq_enemy=(), st=None) -> dict:
    """跑一张牌上的一个钩子，**并带回出参/记录**（`stopAttack`/`newDefender`/`damageToAdd` 这类会改流程的值）。

    就是 `effectvm.record_effects`（2026-10-02 起它把 `vm.run` 的出参、是否真的跑了、读不到的状态一并带回）。
    返回 `{"ran": 有没有覆写并跑了, "eff", "stopped", "out": {形参名: 值}, "records", "chance", "gaps"}`。
    """
    from semantics import effectvm as EV
    return EV.record_effects(km, _ptr_of(c), 0, False, hook, my_side, None, read_hooks=read_hooks,
                             args=dict(args), hq_own=hq_own, hq_enemy=hq_enemy, timeout_s=budget_s,
                             rng_stream=stream,
                             board=st, my_seat=my_side)   # 盘面级原语（IsSideActive…）要用


# ---- 卡面数值/关键词的读法（缺了就按 0/False，如实标在报告里）----------------------------------
_UNIT_TYPES = frozenset(("infantry", "tank", "artillery", "fighter", "bomber", "antiair", "antitank",
                         "tankdestroyer"))


def _kw_default(c) -> set:
    out = set()
    for k in (getattr(c, "keywords", None) or ()):
        k = str(k).lower()
        out.add(k[4:] if k.startswith("has_") else k)
    return out


def _n(x, d=0) -> int:
    try:
        return d if x is None else int(x)
    except (TypeError, ValueError):
        return d


def _atk(c) -> int:
    f = getattr(c, "total_attack", None)
    return _n(f() if callable(f) else getattr(c, "attack", None))


def _dfn(c) -> int:
    return _n(getattr(c, "defense", None))


def _armor(c) -> int:
    raw = getattr(c, "raw", None) or {}
    return _n(raw.get("total_heavy_armor", getattr(c, "heavy_armor", 0)))


def _ability(c, kw_of, name: str) -> bool:
    """关键词 / 被贴的自定义能力（如 lethal、excess、immune）。读不到 ⇒ False。"""
    if name in kw_of(c):
        return True
    raw = getattr(c, "raw", None) or {}
    for a in (raw.get("received_abilities") or ()):
        if str((a.get("ability") if isinstance(a, dict) else a) or "").lower() == name:
            return True
    return False


def ordered_gotchas(cards) -> list:
    """`BP_CardFunctions::GetActiveGotchasOrdered`（字节码已验证）：所有 `IsGotcha ∧ gotchaActivated>0` 的牌
    （**不看位置**），按 key 升序：`card_event_ultra` 的 key = -(cardID+1000000)（最先，cardID 大的更先），
    `card_event_interception` 的 key = -cardID，其余 key = `gotchaActivated`（激活次序号）；
    次序号撞车的按遇到顺序给备用 key 100,101,…。返回 Card 列表。"""
    keyed, backup, used = [], 100, set()
    for c in _registry_order(cards):
        if getattr(c, "card_type", None) != "gotcha" or not (getattr(c, "gotcha_activated", 0) or 0) > 0:
            continue
        nm = str(getattr(c, "fname", None) or getattr(c, "name", None) or "").lower()
        cid = getattr(c, "card_id", 0) or 0
        if nm == "card_event_ultra":
            k = -(cid + 1000000)
        elif nm == "card_event_interception":
            k = -cid
        else:
            k = c.gotcha_activated
            if k in used:
                k, backup = backup, backup + 1
        used.add(k)
        keyed.append((k, c))
    return [c for _k, c in sorted(keyed, key=lambda t: t[0])]


class _Chain:
    """一次攻击（`BP_CardFunctions::AttackCard`）的钩子链。各阶段与字节码一一对应，见 `run_attack_hooks`。"""

    def __init__(self, km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of,
                 hq_own=(), hq_enemy=(), enum_random=False, in_combat=True):
        self.km, self.st, self.stream, self.cache = km, st, stream, cache
        self.enum_random, self.in_combat = enum_random, in_combat
        self.my_side, self.read_hooks, self.budget_s = my_side, read_hooks, budget_s
        self.include_own, self.kw_of = include_own, kw_of
        self.hq_own, self.hq_enemy = tuple(hq_own), tuple(hq_enemy)
        self.hits, self.order = [], []
        self.bucket = "before"
        cs = list(getattr(st, "cards", None) or [])
        self.by_ptr = {_ptr_of(c): c for c in cs if _ptr_of(c)}

    # ---- 基础设施 ----
    def card(self, x):
        return x if (x is not None and not isinstance(x, int)) else self.by_ptr.get(_ptr_of(x))

    def suppressed(self, c) -> bool:
        c = self.card(c)
        return bool(getattr(c, "is_suppressed", None)) if c is not None else False

    def call(self, hook, c, args) -> dict:
        r = _run_hook_ex(self.km, c, hook, args, self.stream, self.my_side, self.read_hooks,
                         self.budget_s, self.hq_own, self.hq_enemy, st=self.st)
        if r.get("ran") is False and not r.get("eff") and not r.get("out"):
            return r                         # 没覆写 ⇒ 空操作（自己的钩子大多如此），不记
        if (self.enum_random and self.stream is None and r.get("nodes")
                and not ATTACK_HOOK_SPECS.get(hook, {}).get("outs")):
            r = self._enumerate(hook, c, args, r)
        cc = self.card(c)
        nm = getattr(cc, "name", None) or "?"
        self.order.append((hook, nm))
        self.hits.append({"hook": hook, "name": nm, "ptr": _ptr_of(c), "side": getattr(cc, "side", None),
                          "bucket": self.bucket, "eff": r.get("eff") or {}, "stopped": r.get("stopped"),
                          "out": dict(r.get("out") or {}), "records": list(r.get("records") or []),
                          "chance": list(r.get("chance") or []), "gaps": list(r.get("gaps") or [])})
        return r

    def _enumerate(self, hook, c, args, r) -> dict:
        """钩子里有**可枚举的随机点**且种子未知 ⇒ 对各随机结果各空跑一遍，效果摘要取期望分支
        `{"outcomes": [(权重, eff)…]}`（弯路 #38 的纪律：随机按真随机对待，不预测种子；与
        `rule._after_hq` 同一做法）。枚举失败 ⇒ 保留单路径结果并带 `chance` 标记。
        ★ 只对"没有出参"的钩子枚举（出参会在枚举里丢失）；枚举后 `records` 清空（效果只在 `eff.outcomes`）。"""
        try:
            from semantics import effectvm as EV
            en = EV.enumerate_effects(self.km, _ptr_of(c), 0, False, hook=hook, my_side=self.my_side,
                                      read_hooks=self.read_hooks, args=dict(args), hq_own=self.hq_own,
                                      hq_enemy=self.hq_enemy, budget_s=self.budget_s)
            outs = [(w, dict(e)) for w, e in (en.get("outcomes") or [])]
            if len(outs) > 1:
                r = dict(r, eff={"outcomes": outs}, records=[])
            elif len(outs) == 1:
                r = dict(r, eff=outs[0][1], records=r.get("records") or [])
        except Exception:                                         # noqa: BLE001
            pass
        return r

    def others(self, hook, exclude=()):
        return find_cards(self.km, self.st, exclude=[x for x in exclude if x], cache=self.cache, hook=hook,
                          on_board_only=False, sides=None, skip_suppressed=True)

    def mark(self, tag):
        self.order.append((tag, ""))

    @staticmethod
    def outv(r, name, default=None):
        o = r.get("out") or {}
        return o[name] if name in o and o[name] is not None else default

    # ---- 伤害管线（CalculateDamageDealt 里的钩子）----
    def add_damage(self, dealer, receiver, damage, from_attack, from_fight, is_def_dmg) -> int:
        """`ExecuteOnDealDamageAddDamage`：自己的 OnCardDealDamage_ModifyDamageDealt（未被压制）→ 0x25
        OnOtherCardDealDamageAddDamage（登记的牌全问，不排除自己）→ reRun 的再问一遍 → 夹到 [0,99]。"""
        dp, rp = _ptr_of(dealer), _ptr_of(receiver)
        calc = damage
        if not self.suppressed(dealer):
            r = self.call("OnCardDealDamage_ModifyDamageDealt", dealer,
                          {"toCard": rp, "Damage": damage, "fromAttack": from_attack, "fromFight": from_fight})
            calc = _n(self.outv(r, "newDamage"), damage)           # 原生默认：回显 Damage
        rerun = []
        for c, _fn in self.others("OnOtherCardDealDamageAddDamage"):
            r = self.call("OnOtherCardDealDamageAddDamage", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": calc, "fromAttack": from_attack,
                           "isDefenderDamage": is_def_dmg})
            calc += _n(self.outv(r, "damageToAdd"), 0)
            if self.outv(r, "reRunAtEnd"):
                rerun.append(c)
        for c in rerun:
            r = self.call("OnOtherCardDealDamageAddDamage", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": calc, "fromAttack": from_attack,
                           "isDefenderDamage": is_def_dmg})
            calc = _n(self.outv(r, "damageToAdd"), 0) + calc
        return max(0, min(99, calc))

    def after_calc(self, to_card, dealer, amount, is_combat, is_attacking, is_redirected) -> int:
        """`ExecuteOnDealDamageAddDamageAfterCalc`：免疫 ⇒ 0；自己的 OnDealDamageAddDamageAfterCalc（未被压制）
        → 0x26（排除伤害来源自己；`card_event_national_fire_service` 推迟到最后；回 stopAdding 即 break）。"""
        dp, rp = _ptr_of(dealer), _ptr_of(to_card)
        if _ability(self.card(to_card) or to_card, self.kw_of, "immune"):
            return 0
        tmp = amount
        if not self.suppressed(dealer):
            r = self.call("OnDealDamageAddDamageAfterCalc", dealer,
                          {"toCard": rp, "Damage": amount, "fromAttack": is_combat,
                           "isAttacker": is_attacking, "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + amount, 0)
        run_after, stopped = [], False
        for c, _fn in self.others("OnOtherCardDealDamageAddDamageAfterCalc"):
            if stopped:
                break
            if getattr(self.card(c), "name", None) == "card_event_national_fire_service":
                run_after.append(c)
                continue
            if _ptr_of(c) == dp:
                continue
            r = self.call("OnOtherCardDealDamageAddDamageAfterCalc", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": tmp, "fromAttack": is_combat,
                           "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + tmp, 0)
            stopped = bool(self.outv(r, "stopAdding"))
        for c in run_after:
            r = self.call("OnOtherCardDealDamageAddDamageAfterCalc", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": tmp, "fromAttack": is_combat,
                           "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + tmp, 0)
        return max(tmp, 0)

    def calc_dealt(self, dealer, receiver, is_attacker) -> dict:
        """`CalculateDamageDealt(dealer, receiver, damageDealerIsAttacker, ignoreAmbush=False,
        ignoreHeavyArmor=False, applyBeforeAttackBuffs=False)` 的移植（真攻击就是这组参数）。
        ★ applyBeforeAttackBuffs=False ⇒ GetBeforeAttack*/BeforeAttackDamage 这 4 个"预览用"的 getter **不会被调**；
          GetPassiveDefenseBuff 只在伏击判定里用（对 receiver 的减伤因 SelectInt 选了 0 而不生效）。"""
        kw = self.kw_of
        dc, rc_ = self.card(dealer) or dealer, self.card(receiver) or receiver
        res = {"damage": 0, "dies": False, "killed_before": False, "shock": False}
        if _ability(rc_, kw, "immune"):
            return res
        dcalc = self.add_damage(dealer, receiver, _atk(dc), True, False, not is_attacker)
        rcalc = self.add_damage(receiver, dealer, _atk(rc_), True, False, is_attacker)
        typ_d, typ_r = getattr(dc, "card_type", None), getattr(rc_, "card_type", None)
        # 伏击：只有"攻击方打在没被打过的伏击单位上"，且不是炮兵/轰炸机(无防空/战斗机时)/冲击/受击方是轰炸机
        if ("ambush" in kw(rc_) and is_attacker and not getattr(rc_, "has_been_attacked_this_turn", False)
                and not _ability(dc, kw, "immune") and typ_d != "artillery"
                and not (typ_d == "bomber" and typ_r not in ("antiair", "fighter"))
                and not (typ_r == "bomber" or "shock" in kw(dc))):
            passive = 0
            if typ_d in _UNIT_TYPES:
                rp_ = self.call("GetPassiveDefenseBuff", dealer, {"incomingDamage": rcalc})
                passive = _n(self.outv(rp_, "amount"), 0)
            if rcalc >= _dfn(dc) + _armor(dc) + passive:
                dcalc = 0
        if not is_attacker and typ_r == "artillery":
            dcalc = 0
        if not is_attacker and typ_d == "bomber":
            dcalc = 0
        if not is_attacker and typ_r == "bomber" and typ_d not in ("fighter", "antiair"):
            dcalc = 0
        if typ_d in _UNIT_TYPES and "shock" in kw(rc_) and not is_attacker:
            dcalc = 0
            res["shock"] = True
        if dcalc > 0:
            dcalc = max(dcalc - _armor(rc_), 0)
        dies = dcalc >= _dfn(rc_)
        is_loc = getattr(rc_, "location", None) == "hq" or typ_r == "location"
        if not dies and _ability(dc, kw, "lethal") and not is_loc and dcalc > 0:
            dies = True
        res.update(damage=dcalc, dies=dies)
        return res

    # ---- 受击 / 幸存 / 造成伤害 ----
    def receive_damage(self, receiver, from_card, amount):
        """`ExecuteBeforeReceiveDamage(cardToReceiveDamage, cardToDealDamage, amount, isCombat=True)`。"""
        rp, fp = _ptr_of(receiver), _ptr_of(from_card)
        if self.include_own and rp and not self.suppressed(receiver):
            self.call("OnReceiveDamage", receiver, {"fromCard": fp, "Damage": amount})
        for c, _fn in self.others("OnOtherCardReceiveDamage", exclude=[rp]):
            self.call("OnOtherCardReceiveDamage", c,
                      {"fromCard": fp, "toCard": rp, "fromAttack": True, "Damage": amount})

    def survived(self, surviving, combatted):
        """`ExecuteOnSurvivedCombatEvents`：自己的 OnSurvivedCombat（未被压制）→ 0x3B（排除幸存者）。"""
        sp, cp = _ptr_of(surviving), _ptr_of(combatted)
        if self.include_own and sp and not self.suppressed(surviving):
            self.call("OnSurvivedCombat", surviving, {"cardCombated": cp})
        for c, _fn in self.others("OnOtherCardSurvivedCombat", exclude=[sp]):
            self.call("OnOtherCardSurvivedCombat", c, {"survivor": sp, "cardCombated": cp})

    def deal_damage_effects(self, to_card, dealer, damage, counter):
        """`ExecuteOnCardDealDamageEffects(toCard, damageDealer, damage, isCombat=True, CounterDamage, False)`：
        伤害来源自己的 OnCardDealDamage（未被压制）→ 0x24（排除来源自己 = cardsDone）。
        （前面的"反制先手"部分——`GetActiveGotchasOrdered`——未实现，见报告。）"""
        tp, dp = _ptr_of(to_card), _ptr_of(dealer)
        done = []
        # 反制先手：`GetActiveGotchasOrdered` 里每张已激活的反制先收 `OnOtherCardDealDamage`（不看登记表、
        # 不看压制），并计入 cardsDone ⇒ 后面的 0x24 不再问它们（`GetStopFurtherActions` 为真则整段中止，
        # 我们不模拟反制"叫停"，未实现）
        for g in ordered_gotchas(getattr(self.st, "cards", None)):
            gp = _ptr_of(g)
            if gp and gp != dp:
                self.call("OnOtherCardDealDamage", g,
                          {"cardDealingDamage": dp, "toCard": tp, "Damage": damage, "isCombatDamage": True,
                           "CounterDamage": counter, "isRedirected": False})
                done.append(gp)
        if self.include_own and dp and not self.suppressed(dealer):
            self.call("OnCardDealDamage", dealer,
                      {"toCard": tp, "Damage": damage, "isCombatDamage": True, "CounterDamage": counter,
                       "isRedirected": False})
        for c, _fn in self.others("OnOtherCardDealDamage", exclude=[dp] + done):
            self.call("OnOtherCardDealDamage", c,
                      {"cardDealingDamage": dp, "toCard": tp, "Damage": damage, "isCombatDamage": True,
                       "CounterDamage": counter, "isRedirected": False})

    # ---- 摧毁链 ----
    def before_destroyed(self, victim, killer):
        """`ExecuteOnBeforeOtherCardDestroyed(cardDestroyedID, AttackerID, False, DestroyedInCombat=True)`。"""
        vp, kp = _ptr_of(victim), _ptr_of(killer)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnBeforeDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})
        for c, _fn in self.others("OnBeforeOtherCardDestroyed", exclude=[vp]):
            self.call("OnBeforeOtherCardDestroyed", c,
                      {"cardDestroyed": vp, "killer": kp, "TriggerNotDestroyed": False,
                       "destroyedInCombat": self.in_combat})

    def leave_board(self, victim, old_loc_on_board=True):
        """`ExecuteOnBeforeLeaveBoardOrOwnerEvents(card, NewLocation=8(弃牌堆), OldLocation, method=1(摧毁))`：
        只有 OldLocation 在 5/6/7（场上）才触发。自己的 OnLeaveBoardOrOwner → 0x2E。"""
        vp = _ptr_of(victim)
        if not old_loc_on_board:
            return
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnLeaveBoardOrOwner", victim, {"goingToLocation": 8, "leavePlayMethod": 1})
        for c, _fn in self.others("OnOtherCardLeaveBoardOrOwner", exclude=[vp]):
            self.call("OnOtherCardLeaveBoardOrOwner", c,
                      {"cardLeaving": vp, "goingToLocation": 8, "Method": 1})

    def location_moved(self, victim, old_loc):
        """`CardLocationMoved(..., newLocation=8, reason=Destroyed)` → `ExecuteOnCardLocationMoved`：
        自己的 OnCardLocationMoved → 0x2F OnOtherCardLocationMoved（排除自己）。"""
        vp = _ptr_of(victim)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnCardLocationMoved", victim, {"OldLocation": old_loc, "NewLocation": 8,
                                                       "ChangeOwner": False, "MoveReason": "Destroyed"})
        for c, _fn in self.others("OnOtherCardLocationMoved", exclude=[vp]):
            self.call("OnOtherCardLocationMoved", c,
                      {"cardMoved": vp, "OldLocation": old_loc, "NewLocation": 8, "ChangeOwner": False,
                       "MoveReason": "Destroyed"})

    def after_leave(self, victim, old_loc, old_loc_on_board=True):
        """`ExecuteOnAfterLeaveBoardOrOwnerEvents`：自己的 OnAfterLeaveBoard → 0x08 OnAfterOtherCardLeaveBoardOrOwner。"""
        vp = _ptr_of(victim)
        if not old_loc_on_board:
            return
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnAfterLeaveBoard", victim, {"goingToLocation": 8})
        for c, _fn in self.others("OnAfterOtherCardLeaveBoardOrOwner", exclude=[vp]):
            self.call("OnAfterOtherCardLeaveBoardOrOwner", c, {"cardLeaving": vp, "OldLocation": old_loc})

    def destroyed_fn(self, victim, killer, all_destroyed):
        """`ExecuteOnCardDestroyedFunction(card, location, killer, allCardsGettingDestroyed, destroyedInCombat=True)`：
        自己的 OnDestroyed（未被压制、有摧毁效果、没 StopDestructionEffect）→ 0x27 OnOtherCardDestroyed
        （排除自己；`selfIsAlsoGettingDestroyed` = 对方是否也在这批里）→ 自己的 OnAfterDestroyed。
        ★ 未实现：0x18 摧毁效果倍增（`ExecuteOnDestructionEffectTriggered`，只有 4 张卡）、`hasSalvage` 打捞。"""
        vp, kp = _ptr_of(victim), _ptr_of(killer)
        v = self.card(victim)
        loc = getattr(v, "location", None)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})
            # 摧毁效果倍增（0x18）：Σ 各登记牌的 OnDestructionEffectTriggered 的 TriggerMultiple；
            # 倍数 m>0 ⇒ 再以 TriggerNotDestroyed=True 重放 OnDestroyed，每次重放后重算 m，直到 i>m
            i, mult = 1, self.destruction_multiple(victim, kp, all_destroyed)
            while mult > 0 and i <= mult and i <= 8:                   # 8 = 防跑飞的硬上限
                self.call("OnDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": True})
                mult = self.destruction_multiple(victim, kp, all_destroyed)
                i += 1
        for c, _fn in self.others("OnOtherCardDestroyed", exclude=[vp]):
            self.call("OnOtherCardDestroyed", c,
                      {"cardDestroyed": vp, "killer": kp, "TriggerNotDestroyed": False,
                       "destroyedLocation": {"frontline": 7, "back": 6, "hq": 5}.get(loc, 7),
                       "selfIsAlsoGettingDestroyed": _ptr_of(c) in all_destroyed, "destroyedInCombat": self.in_combat})
        self.salvage(victim, killer)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnAfterDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})

    def destruction_multiple(self, victim, kp, all_destroyed) -> int:
        """`ExecuteOnDestructionEffectTriggered(cardTriggered, instigatorID, ..., skipSuppressCheck=False)`：
        对 0x18 登记牌（被压制的丢掉）逐个调 `OnDestructionEffectTriggered(cardTriggered, instigatorID,
        SelfAlsoDestroyed, &TriggerMultiple)`，把 TriggerMultiple 加起来（4 张卡覆写）。"""
        vp = _ptr_of(victim)
        total = 0
        for c, _fn in self.others("OnDestructionEffectTriggered"):
            r = self.call("OnDestructionEffectTriggered", c,
                          {"cardTriggered": vp, "instigatorID": _n(getattr(self.card(kp), "card_id", 0)),
                           "SelfAlsoDestroyed": _ptr_of(c) in all_destroyed})
            total += _n(self.outv(r, "TriggerMultiple"), 0)
        return total

    def salvage(self, victim, killer):
        """打捞：击杀者 `hasSalvage` ∧ 其阵营在行动 ∧ 被摧毁者是敌方 ∧ 没有 `cantBeSalvaged` ⇒
        `SalvageMultipleUnits([victim], killer)`（把被摧毁单位的一张副本给击杀者方手牌）。
        记成 `salvage_ids:[被摧毁单位 card_id]`（boardeval 的 `_salvage_one`：1/1、费用≤3 的副本进手牌）。"""
        k, v = self.card(killer), self.card(victim)
        if k is None or v is None:
            return
        kw = self.kw_of
        if ("salvage" in kw(k) or _ability(k, kw, "salvage")) and getattr(k, "side", None) != getattr(v, "side", None) \
                and not _ability(v, kw, "cantbesalvaged"):
            self.hits.append({"hook": "<salvage>", "name": getattr(k, "name", "?"), "ptr": _ptr_of(k),
                              "side": getattr(k, "side", None), "bucket": self.bucket, "eff": {"salvage_ids": [getattr(v, "card_id", None)]},
                              "stopped": None, "out": {}, "records": [], "chance": [], "gaps": []})
            self.order.append(("<salvage>", getattr(v, "name", "?")))

    def destroy_combat(self, att, dfd, att_dies, def_dies):
        """`ExecuteAttackCard` 末尾的摧毁序列。双方都死：先防守方后攻击方，逐阶段交错
        （BeforeDestroyed×2 → BeforeLeave×2 → Moved×2 → AfterLeave×2 → DestroyedFn×2）；
        只死一个：BeforeDestroyed → `ApplyRemoveCardFromBoard`（BeforeLeave → Moved → AfterLeave → DestroyedFn）。"""
        ap, dp = _ptr_of(att), _ptr_of(dfd)
        loc = lambda c: {"frontline": 7, "back": 6, "hq": 5}.get(getattr(self.card(c), "location", None), 7)  # noqa: E731
        if att_dies and def_dies:
            self.bucket = "def_destroyed"
            self.before_destroyed(dfd, att)
            self.bucket = "att_destroyed"
            self.before_destroyed(att, dfd)
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.leave_board(who)
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.location_moved(who, loc(who))
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.after_leave(who, loc(who))
            for who, killer, bk in ((dfd, att, "def_destroyed"), (att, dfd, "att_destroyed")):
                self.bucket = bk
                self.destroyed_fn(who, killer, {ap, dp})
            return
        for who, killer, dies, bk in ((att, dfd, att_dies, "att_destroyed"), (dfd, att, def_dies, "def_destroyed")):
            if not dies:
                continue
            self.bucket = bk
            self.before_destroyed(who, killer)
            self.leave_board(who)
            self.location_moved(who, loc(who))
            self.after_leave(who, loc(who))
            self.destroyed_fn(who, killer, {_ptr_of(who)})


def run_attack_hooks(km, st, attacker, defender, damage=None, cost: int = 0,
                     stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                     cache: Optional[TriggerCache] = None, include_own: bool = True,
                     damage_to_attacker=None, shock: bool = False,
                     include_damage_hooks: bool = True, kw_of=None,
                     hq_own=(), hq_enemy=(), enum_random: bool = False) -> dict:
    """跑一遍"一次攻击"会触发的**全部影响胜负的钩子**，顺序与 `BP_CardFunctions::AttackCard` →
    `ExecuteAttackCard` → `ExecuteOnAfterAttackEvents` 的**字节码**一致（ATTACK-HOOKS-1.60.md §3；
    1.58 uexp 反汇编，1.60 的 FModel 标签偏移逐一吻合）：

      ① 0x1E `OnOtherCardAttackSwitchTarget(cardAttacking, oldDefender, &newDefender)`：**所有**登记的牌
         （含攻击者自己，不排除）按表顺序问；第一个回 `newDefender != 当前防守方` 的牌生效并**立刻 break**。
      ② 0x1F `OnOtherCardAttacks(cardAttacking, defenderCard, &stopAttack, &AttackedAndStopped)`：登记的牌
         （**排除攻击者**）**全部**问一遍（不提前 break）；任一回 stopAttack ⇒ StopAttack；
         任一回 AttackedAndStopped ⇒ tmp。
      ③ StopAttack ⇒ `success=true` 直接结束 —— **不付指挥点、不触发任何后续钩子**。
      ④ **付指挥点**（`ChangeKreditsBySide(-costToPay)`）—— 在 OnBeforeAttack **之前**（字节码 @2764 定案）。
      ⑤ 攻击者自己的 `OnBeforeAttack(defenderCard)`（未被压制）→ 0x0D `OnBeforeOtherCardAttacks(cardAttacking,
         defenderCard)`（排除攻击者）→ RemoveSmokescreen。
      ⑥ tmp(AttackedAndStopped)：防守方置空、伤害 0；`ExecuteStoppedAttack` = `OnAttackStopped()`；跳到 ⑩。
      ⑦ 伤害：两次 `CalculateDamageDealt`（每次内部各跑两遍 `ExecuteOnDealDamageAddDamage`：自己的
         ModifyDamageDealt → 0x25）；再两次 `ExecuteOnDealDamageAddDamageAfterCalc`（自己的 → 0x26）。
         给定 `damage`/`damage_to_attacker`（最终值）则跳过整条伤害管线。
      ⑧ `ExecuteAttackCard`：受击通知（伤害>0 且受击方未被压制；防守方先）`OnReceiveDamage`/0x34；
         `excess` 溢出伤害打总部；幸存通知（防守方是单位时）`OnSurvivedCombat`/0x3B；造成伤害通知
         `OnCardDealDamage`/0x24（先打防守方的、再反击的）；防守方是总部且被摧毁 ⇒ 对局结束（不再往下）；
         摧毁链（BeforeDestroyed/0x0F → BeforeLeave/0x2E → Moved/0x2F → AfterLeave/0x08 → OnDestroyed/0x27/
         OnAfterDestroyed）。
      ⑨ `ExecuteOnAfterAttackEvents`：自己的 `OnAfterAttack(defenderCard, wasShockAttack, attackCost)`（未被压制）
         → 0x04 `OnAfterOtherCardAttacks(defenderCard, attackerCard, damageToDefender, attackCost)`（排除攻击者）。
         ★ 形参顺序是 (defender, attacker)，与 ①②⑤ 相反。
      ⑩ `ExecuteOnOperationKreditsSpent`：自己的 `OnOperationKreditsSpent(kreditsSpent)` → 0x44
         `OnOtherCardOperationKreditsSpent(cardOperated, kreditsSpent)`（排除攻击者）。

    双方的牌都枚举（注册表不按阵营/位置过滤，钩子自己 gate）；被压制的牌被 `FetchAllCardsWithEventTrigger`
    丢掉（`suppressionExceptionTriggers` 例外**未确认**）；所有钩子共用**同一条** `stream`
    （`None` ⇒ 种子未知：随机点按"枚举/机会节点"处理，不伪造确定结果）。

    返回 `{"hits", "order", "defender", "switched", "stop", "attacked_and_stopped", "paid", "outcome",
           "damage", "damage_to_attacker", "attacker_destroyed", "defender_destroyed", "match_end",
           "excess", "draws", "cards"}`；`outcome` ∈ {"stopped", "consumed", "resolved", "match_end"}；
    `order` = 实际执行的 (钩子名, 卡名) 序列（`<pay>` = 扣指挥点的时点）；`hits` = 其中每次钩子的记录
    （带 `bucket`/`records`，供 `to_fx` 转成 boardeval 能消费的效果）。
    """
    ap = _ptr_of(attacker)
    dp = _ptr_of(defender)
    d0 = getattr(stream, "draws", 0) if stream is not None else 0
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of or _kw_default,
                hq_own, hq_enemy, enum_random=enum_random)
    atk_card = ch.card(attacker) or attacker
    kw = ch.kw_of

    def result(outcome, dptr, switched, stop, tmp, paid, **more):
        r = {"hits": ch.hits, "order": ch.order, "defender": dptr, "switched": switched, "stop": stop,
             "attacked_and_stopped": tmp, "paid": paid, "outcome": outcome, "damage": 0,
             "damage_to_attacker": 0, "attacker_destroyed": False, "defender_destroyed": False,
             "match_end": None, "excess": 0, "explicit_damage": damage is not None,
             "draws": (getattr(stream, "draws", 0) - d0) if stream is not None else 0}
        r.update(more)
        r["cards"] = len(ch.hits)
        return r

    # ① 换目标（0x1E）：不排除攻击者；第一个换了的生效并 break
    ch.bucket = "before"
    switched = False
    for c, _fn in ch.others("OnOtherCardAttackSwitchTarget"):
        r = ch.call("OnOtherCardAttackSwitchTarget", c, {"cardAttacking": ap, "oldDefender": dp})
        nd = _ptr_of((r.get("out") or {}).get("newDefender"))
        if nd and nd != dp:
            dp, switched = nd, True
            break
    dfd_card = ch.card(dp) if (switched or isinstance(defender, int)) else defender
    # ② 吞攻击（0x1F）：排除攻击者；全部问完（不提前 break）
    stop = tmp = False
    for c, _fn in ch.others("OnOtherCardAttacks", exclude=[ap]):
        r = ch.call("OnOtherCardAttacks", c, {"cardAttacking": ap, "defenderCard": dp})
        o = r.get("out") or {}
        stop = stop or bool(o.get("stopAttack"))
        tmp = tmp or bool(o.get("AttackedAndStopped"))
    # ③ StopAttack：直接结束，不付费、不触发后续
    if stop:
        return result("stopped", dp, switched, True, tmp, 0)
    # ④ 付指挥点（在 OnBeforeAttack 之前）
    paid = int(cost)
    ch.order.append(("<pay>", str(paid)))
    # ⑤ 攻击前：自己的 OnBeforeAttack → 0x0D
    if include_own and ap and not ch.suppressed(atk_card):
        ch.call("OnBeforeAttack", attacker, {"defenderCard": dp})
    for c, _fn in ch.others("OnBeforeOtherCardAttacks", exclude=[ap]):
        ch.call("OnBeforeOtherCardAttacks", c, {"cardAttacking": ap, "defenderCard": dp})
    # ⑥ 攻击被吞：OnAttackStopped，然后只剩"指挥点已花"通知
    ch.bucket = "after"
    if tmp:
        if include_own and ap:
            ch.call("OnAttackStopped", attacker, {})
        _kredits_spent(ch, attacker, ap, paid, include_own)
        return result("consumed", 0, switched, False, True, paid)
    # ⑦ 伤害
    ch.bucket = "mid"
    explicit = damage is not None
    if explicit:
        dmg_def = int(damage)
        dmg_att = int(damage_to_attacker or 0)
    else:
        calc_a = ch.calc_dealt(attacker, dfd_card if dfd_card is not None else dp, True)
        calc_d = ch.calc_dealt(dfd_card if dfd_card is not None else dp, attacker, False)
        dmg_def = ch.after_calc(dfd_card if dfd_card is not None else dp, attacker, calc_a["damage"],
                                True, True, False)
        dmg_att = ch.after_calc(attacker, dfd_card if dfd_card is not None else dp, calc_d["damage"],
                                True, False, False)
        shock = shock or calc_d["shock"]
    dmg_final = dmg_def           # `AttackCard.damageToDefenderFinal`：传给 0x04 的是这个值（excess 夹值之前）
    # ⑧ ExecuteAttackCard
    def_total, att_total = _dfn(dfd_card) if dfd_card is not None else 0, _dfn(atk_card)
    dcard = dfd_card if dfd_card is not None else dp
    d_is_loc = (getattr(dfd_card, "location", None) == "hq" or getattr(dfd_card, "card_type", None) == "location"
                or dp in set(ch.hq_enemy) | set(ch.hq_own))
    d_is_unit = getattr(dfd_card, "card_type", None) in _UNIT_TYPES
    excess = 0
    if include_damage_hooks:
        if dmg_def > 0 and not ch.suppressed(dcard):
            ch.receive_damage(dcard, attacker, dmg_def)
        if dmg_att > 0 and not ch.suppressed(atk_card):
            ch.receive_damage(attacker, dcard, dmg_att)
    if _ability(atk_card, kw, "excess") and d_is_unit and def_total and dmg_def > def_total:
        excess = dmg_def - def_total
        dmg_def = def_total
        ch.hits.append({"hook": "<excess>", "name": getattr(atk_card, "name", "?"), "ptr": ap, "side": None,
                        "bucket": "mid", "eff": {"damage_hq": excess}, "stopped": None, "out": {},
                        "records": [], "chance": [], "gaps": []})
        ch.order.append(("<excess>", str(excess)))
    lethal_a = _ability(atk_card, kw, "lethal")
    lethal_d = _ability(dcard, kw, "lethal")
    def_dies = (bool(def_total) and dmg_def >= def_total) or (dmg_def > 0 and lethal_a and not d_is_loc)
    att_dies = (bool(att_total) and dmg_att >= att_total) or (dmg_att > 0 and lethal_d)
    if include_damage_hooks:
        if d_is_unit:
            if not att_dies:
                ch.survived(attacker, dcard)
            if not def_dies:
                ch.survived(dcard, attacker)
        if dmg_def > 0:
            ch.deal_damage_effects(dcard, attacker, dmg_def, False)
        if dmg_att > 0:
            ch.deal_damage_effects(attacker, dcard, dmg_att, True)
    if def_dies and d_is_loc:                       # 总部被打死：对局结束，后面的什么都不再发生
        winner = "local" if (getattr(atk_card, "side", None) == "local") else "enemy"
        return result("match_end", dp, switched, False, False, paid, damage=dmg_def, damage_to_attacker=dmg_att,
                      attacker_destroyed=att_dies, defender_destroyed=True, match_end=winner, excess=excess)
    if include_damage_hooks:
        ch.destroy_combat(atk_card, dcard, att_dies, def_dies)
    # ⑨ 攻击后：自己的 OnAfterAttack → 0x04
    ch.bucket = "after"
    if include_own and ap and not ch.suppressed(atk_card):
        ch.call("OnAfterAttack", attacker, {"defenderCard": dp, "wasShockAttack": bool(shock),
                                            "attackCost": int(cost)})
    for c, _fn in ch.others("OnAfterOtherCardAttacks", exclude=[ap]):
        ch.call("OnAfterOtherCardAttacks", c, {"defenderCard": dp, "attackerCard": ap,
                                               "damageToDefender": int(dmg_final), "attackCost": int(cost)})
    # ⑩ 指挥点已花通知
    _kredits_spent(ch, attacker, ap, paid, include_own)
    return result("resolved", dp, switched, False, False, paid, damage=dmg_def, damage_to_attacker=dmg_att,
                  attacker_destroyed=att_dies, defender_destroyed=def_dies, excess=excess)


def _kredits_spent(ch, operated, op, amount, include_own):
    """`ExecuteOnOperationKreditsSpent(cardOperated, kreditsSpent)`：自己的（不查压制）→ 0x44（排除自己）。"""
    if include_own and op:
        ch.call("OnOperationKreditsSpent", operated, {"kreditsSpent": int(amount)})
    for c, _fn in ch.others("OnOtherCardOperationKreditsSpent", exclude=[op]):
        ch.call("OnOtherCardOperationKreditsSpent", c, {"cardOperated": op, "kreditsSpent": int(amount)})


# --------------------------------------------------------------------------- 转成 boardeval 能吃的效果
# 作用在**某张具体的牌**上的动词（实参 0 = 那张牌）；其它（指挥点/抽牌/槽/召唤…）是全局效果。
def _unit_scoped_verbs() -> frozenset:
    from semantics import effectvm as EV
    s = {v for v, t in EV.COUNT_VERBS.items() if t[2] == 0}
    s |= {v for v, t in EV.FLAG_VERBS.items() if t[1] == 0}
    s |= set(EV.GIVE_KW)
    return frozenset(s)


def _merge_eff(a: dict, b: dict) -> dict:
    """把两份效果摘要叠加：数相加、bool 取或、list 拼接、buff 逐项加、chance 取并。"""
    out = dict(a)
    for k, v in (b or {}).items():
        if k == "uncertain":
            continue
        if k not in out:
            out[k] = list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)
        elif k == "buff":
            out[k] = [out[k][0] + v[0], out[k][1] + v[1]]
        elif isinstance(v, bool):
            out[k] = bool(out[k]) or v
        elif isinstance(v, (int, float)) and isinstance(out[k], (int, float)):
            out[k] = out[k] + v
        elif isinstance(v, list) and isinstance(out[k], list):
            out[k] = (out[k] + [x for x in v if x not in out[k]]) if k in ("give", "chance") else out[k] + v
    return out


# 多目标动词：实参 0 是一组卡(id 或对象)，逐个拆成"那张牌上的单牌效果"（boardeval 逐牌消费）
_MULTI = {
    "DamageMultipleCards": lambda a: {"damage": _n(a[1])} if len(a) > 1 else None,
    "DestroyMultipleCards": lambda a: {"destroy": True},
    "SuppressMultipleUnits": lambda a: {"pin": True},
    "RemoveMultipleCardsFromBoard": lambda a: {"remove_unit": True},
    "MoveMultipleCardsToTopOfOwnersDeck": lambda a: {"to_deck": True},
    "AddDefenseToMultipleCards": lambda a: {"buff": [0, _n(a[1])]} if len(a) > 1 else None,
    "MakeCardRetreat": lambda a: {"retreat": True},
}


def hit_effects(hit: dict, st, my_side=None, hq_own=(), hq_enemy=()) -> tuple:
    """一次钩子记录 → `(全局效果 dict, {card_id: 该牌上的效果 dict})`。

    按记录里每条写类调用的**目标**拆开：目标是某张场上牌 ⇒ 记到那张牌（buff/伤害/压制/给关键词/…）；
    目标是总部 ⇒ `damage_hq` / `heal_hq`；其余（指挥点/抽牌/槽/召唤/…）⇒ 全局。
    目标认不出（比如随机枚举没落定）⇒ 记进全局并丢掉。`uncertain`（被随机污染的记录）不要。"""
    from semantics import effectvm as EV
    scoped = _unit_scoped_verbs()
    ptr2c = {_ptr_of(c): c for c in (getattr(st, "cards", None) or []) if _ptr_of(c)}
    id2c = {getattr(c, "card_id", None): c for c in ptr2c.values() if getattr(c, "card_id", None) is not None}
    hq_o, hq_e = set(hq_own), set(hq_enemy)
    glob_recs, per = [], {}
    extra = {}
    for rec in hit.get("records") or ():
        verb, args = rec.get("verb"), rec.get("args") or []
        if verb in _MULTI and args and isinstance(args[0], (list, tuple)):
            if not rec.get("tainted"):
                piece = _MULTI[verb](args)
                for t in args[0]:
                    tc = ptr2c.get(t) or id2c.get(t)
                    if piece and tc is not None and getattr(tc, "card_id", None) is not None:
                        per.setdefault(tc.card_id, []).append({"_piece": piece})
            continue
        if verb == "MakeCardsFight" and len(args) >= 2 and not rec.get("tainted"):
            a_, b_ = (ptr2c.get(args[0]) or id2c.get(args[0])), (ptr2c.get(args[1]) or id2c.get(args[1]))
            if a_ is not None and b_ is not None:
                per.setdefault(a_.card_id, []).append({"_piece": {"damage": _atk(b_)}})
                per.setdefault(b_.card_id, []).append({"_piece": {"damage": _atk(a_)}})
            continue
        if verb in scoped and args:
            t = args[0]
            tc = ptr2c.get(t) or id2c.get(t)
            is_hq = getattr(tc, "location", None) == "hq"
            if t in hq_e or (is_hq and getattr(tc, "side", None) == "enemy"):
                if verb == "DamageCard" and len(args) > 1:
                    extra["damage_hq"] = extra.get("damage_hq", 0) + _n(args[1])
                continue
            if t in hq_o or is_hq:
                if verb == "DamageCard" and len(args) > 1:
                    extra["heal_hq"] = extra.get("heal_hq", 0) - _n(args[1])
                elif verb == "ChangeDefense" and len(args) > 2:
                    extra["heal_hq"] = extra.get("heal_hq", 0) + _n(args[2])
                continue
            if tc is not None and getattr(tc, "card_id", None) is not None:
                per.setdefault(tc.card_id, []).append(rec)
                continue
        glob_recs.append(rec)

    def eff_of(recs):
        r = EV.Recorder(0, 0, None)
        r.records = [x for x in recs if "_piece" not in x]
        r.chance = list(hit.get("chance") or [])
        e = EV.to_effects(r, my_side, None)
        e.pop("uncertain", None)
        for x in recs:                                  # 多目标/对打 展开出来的单牌效果片段
            if "_piece" in x:
                e = _merge_eff(e, x["_piece"])
        return e
    g = _merge_eff(eff_of(glob_recs), extra)
    return g, {cid: eff_of(rs) for cid, rs in per.items()}


_DAMAGE_PIPELINE_HOOKS = frozenset(("OnCardDealDamage_ModifyDamageDealt", "OnOtherCardDealDamageAddDamage",
                                    "OnDealDamageAddDamageAfterCalc", "OnOtherCardDealDamageAddDamageAfterCalc",
                                    "GetPassiveDefenseBuff"))
_DESTROY_BUCKETS = ("def_destroyed", "att_destroyed")


def hit_bucket_effects(hit: dict, st, my_side=None, hq_own=(), hq_enemy=()) -> tuple:
    """一次钩子记录 → `(全局效果, {card_id: 逐牌效果})`，在 `hit_effects` 之上再处理两种特殊记录：
    `<excess>`/`<salvage>` 这类合成命中（效果直接写在 `hit["eff"]` 里）、以及随机枚举过的命中
    （`records` 已清空，效果只在 `hit["eff"]["outcomes"]`）。"""
    g, per = hit_effects(hit, st, my_side, hq_own, hq_enemy)
    if hit.get("hook") in ("<excess>", "<salvage>") or (hit.get("eff") and not hit.get("records")):
        g = _merge_eff(g, hit.get("eff") or {})
    return g, per


def merge_hits(hits, st, my_side=None, hq_own=(), hq_enemy=(), skip_buckets=()) -> dict:
    """把一串命中按 bucket 合并：`{bucket: {"eff": 全局, "units": {card_id: eff}}}`。"""
    buckets = {}
    for h in hits or ():
        if h.get("bucket", "after") in skip_buckets:
            continue
        g, per = hit_bucket_effects(h, st, my_side, hq_own, hq_enemy)
        b = buckets.setdefault(h.get("bucket", "after"), {"eff": {}, "units": {}})
        b["eff"] = _merge_eff(b["eff"], g)
        for k, e in per.items():
            b["units"][k] = _merge_eff(b["units"].get(k, {}), e)
    return buckets


def to_fx(res: dict, st, my_side=None, hq_own=(), hq_enemy=(), dedupe_death: bool = True) -> dict:
    """`run_attack_hooks` 的结果 → boardeval 的 `attack_fx` 条目（格式见 `boardeval.Sim` 的注释）。

    `dedupe_death=True`（缺省）：**摧毁链上的钩子（OnDestroyed/0x27/…）不进 `def_destroyed`/`att_destroyed`
    桶**——`Sim.death_fx`（`rule._death_fx`，按 `death_effects` 预计算）已经在单位死亡时结算它们，
    再放进桶里会**双算**。伤害数值只在"钩子改写了伤害"或调用方显式给了伤害时才输出（否则让
    boardeval 用自己的结算，别拿本模块的移植去覆盖它）。"""
    by_ptr = {_ptr_of(c): c for c in (getattr(st, "cards", None) or []) if _ptr_of(c)}

    def cid(p):
        c = by_ptr.get(p)
        if c is None:
            return None
        return "hq" if getattr(c, "location", None) == "hq" else getattr(c, "card_id", None)
    own_after = any(h["hook"] == "OnAfterAttack" for h in (res.get("hits") or ()))
    buckets = merge_hits(res.get("hits"), st, my_side, hq_own, hq_enemy,
                         skip_buckets=_DESTROY_BUCKETS if dedupe_death else ())
    use_dmg = bool(res.get("explicit_damage", True)) or any(
        h["hook"] in _DAMAGE_PIPELINE_HOOKS for h in (res.get("hits") or ()))
    out = {"stop": bool(res.get("stop")), "consumed": bool(res.get("attacked_and_stopped")) and not res.get("stop"),
           "switch_to": cid(res["defender"]) if res.get("switched") else None, "paid": res.get("paid"),
           "dmg_def": res.get("damage") if use_dmg else None, "dmg_att": res.get("damage_to_attacker") if use_dmg else None,
           "def_dies": res.get("defender_destroyed") if use_dmg else None,
           "att_dies": res.get("attacker_destroyed") if use_dmg else None,
           "match_end": res.get("match_end"), "own_after": own_after,
           "buckets": {k: {"eff": v["eff"], "units": [(u, e) for u, e in v["units"].items() if e]}
                       for k, v in buckets.items()}}
    return out


_CHAIN_HOOK_TRIGGER_NAMES = None


def present_hooks(km, st, hooks, cache: Optional[TriggerCache] = None) -> set:
    """`hooks` 里**场上/手牌/牌库任一张牌覆写了**的那些（一次性预检：没有覆写就不必跑整条链）。
    走 `find_cards` 的 `指针→类` 与 `类→函数` 两层缓存。"""
    tc = cache or TriggerCache(km)
    out = set()
    for h in hooks:
        if find_cards(km, st, cache=tc, hook=h, on_board_only=False, sides=None):
            out.add(h)
    return out


def build_attack_fx(km, st, pairs, my_side=None, read_hooks=None, budget_s: float = 1.0, stream=None,
                    kw_of=None, hq_own=(), hq_enemy=(), cache=None, total_budget_s: float = 6.0,
                    enum_random: bool = True) -> dict:
    """给一批候选攻击 `[(attacker_card, defender_card_or_"hq_card", cost)]` 各跑一遍钩子链，
    产出 `{(攻击者 card_id, 目标 card_id | "hq"): fx}`，直接喂 `boardeval.from_cards(attack_fx=...)`。
    只收"有后果"的（被吞/换目标/有任何钩子记录）；没后果的不进表（boardeval 走自己的结算）。
    预检：整盘没有任何牌覆写 `ATTACK_HOOK_SPECS` 里的钩子 ⇒ 直接返回 `{}`；总预算 `total_budget_s` 用完后
    剩下的对子**不再算**（并在返回的 `fx_meta` 里记 `skipped`——不编造）。
    返回值是 dict 子类实例，多一个 `.meta`：`{"pairs","computed","skipped","hooked","elapsed_s","draws"}`。"""
    import time as _t
    tc = cache or TriggerCache(km)
    t0 = _t.time()
    out = _FxTable()
    meta = {"pairs": len(pairs), "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0, "draws": 0}
    out.meta = meta
    present = present_hooks(km, st, ATTACK_HOOK_SPECS, tc)
    meta["hooked"] = sorted(present)
    if not present:
        return out
    for a, d, cost in pairs:
        if _t.time() - t0 > total_budget_s:
            meta["skipped"] += 1
            continue
        r = run_attack_hooks(km, st, a, d, cost=cost, stream=stream, my_side=my_side, read_hooks=read_hooks,
                             budget_s=budget_s, kw_of=kw_of, hq_own=hq_own, hq_enemy=hq_enemy, cache=tc,
                             enum_random=enum_random)
        meta["computed"] += 1
        meta["draws"] += r.get("draws", 0)
        fx = to_fx(r, st, my_side, hq_own, hq_enemy)
        dst = "hq" if getattr(d, "location", None) == "hq" else getattr(d, "card_id", None)
        if r["hits"] or fx["stop"] or fx["consumed"] or fx["switch_to"]:
            out[(getattr(a, "card_id", None), dst)] = fx
    meta["elapsed_s"] = round(_t.time() - t0, 3)
    return out


class _FxTable(dict):
    """`build_attack_fx` 的返回值：就是 dict，外加 `.meta`（留痕：算了几对/跳过几对/哪些钩子在场/耗时）。"""
    meta: dict = {}


# --------------------------------------------------------------------------- 死亡链
def run_death_chain(km, st, victim, killer=None, stream=None, my_side=None, read_hooks=None,
                    budget_s: float = 0.8, cache: Optional[TriggerCache] = None, include_own: bool = True,
                    kw_of=None, hq_own=(), hq_enemy=(), in_combat: bool = True,
                    enum_random: bool = True) -> dict:
    """**一张牌死亡时**的完整摧毁序列（`ExecuteAttackCard` 的摧毁段 / `ApplyRemoveCardFromBoard` 的同一段）：
    BeforeDestroyed(自己) + 0x0F 旁观者 → BeforeLeave(自己) + 0x2E → `CardLocationMoved`(自己) + 0x2F →
    AfterLeave(自己) + 0x08 → `ExecuteOnCardDestroyedFunction`：自己的 `OnDestroyed`（+0x18 倍增重放）→
    0x27 `OnOtherCardDestroyed` 旁观者（排除自己）→ 打捞 → 自己的 `OnAfterDestroyed`。
    `killer` 传**真值**（击杀者的卡对象/指针；不知道才传 None/0）。返回 `{"hits","order","draws"}`。"""
    d0 = getattr(stream, "draws", 0) if stream is not None else 0
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of or _kw_default,
                hq_own, hq_enemy, enum_random=enum_random, in_combat=in_combat)
    ch.bucket = "death"
    loc = {"frontline": 7, "back": 6, "hq": 5}.get(getattr(ch.card(victim), "location", None), 7)
    ch.before_destroyed(victim, killer)
    ch.leave_board(victim)
    ch.location_moved(victim, loc)
    ch.after_leave(victim, loc)
    ch.destroyed_fn(victim, killer, {_ptr_of(victim)})
    return {"hits": ch.hits, "order": ch.order,
            "draws": (getattr(stream, "draws", 0) - d0) if stream is not None else 0}


def death_effects(km, st, victim, killer=None, **kw) -> dict:
    """`run_death_chain` → 一份 `Sim.death_fx[victim_card_id]` 能吃的效果摘要（`boardeval._apply_unit_eff` 语义：
    逐牌效果放进 `unit_eff:[(card_id, eff)]`，其余是全局效果）。没有任何命中 ⇒ `{}`。"""
    res = run_death_chain(km, st, victim, killer, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 上线链（0x32）
def _unrevealed_covert(c) -> bool:
    """`IsUnrevealedCovertCard` = `hasCovert` ∧ `!isRevealed`（NATIVE-COVERAGE 板查询族）。

    优先读 `raw`（`kardsmem.cards` 直接读的 `has_covert`/`is_revealed` 字节，权威）；
    只有 `keywords` 时退回关键词判断 —— 用户 2026-10-02 说过 `keywords` 里的 covert 不可靠，
    所以那只是兜底（真值以 raw 为准）。
    """
    raw = getattr(c, "raw", None) or {}
    fl = raw.get("keyword_flags") or {}
    has = bool(fl["has_covert"]) if "has_covert" in fl else \
        ("covert" in (getattr(c, "keywords", None) or []))
    rev = raw.get("is_revealed")
    if rev is None:
        rev = getattr(c, "is_revealed", False)
    return bool(has) and not bool(rev)


def run_move_frontline(km, st, moved, force_move: bool = False, move_cost: int = 0, stream=None,
                       my_side=None, read_hooks=None, budget_s: float = 0.8,
                       cache: Optional[TriggerCache] = None, include_own: bool = True,
                       kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """**一张牌上线时**的钩子序列（0x32）：反制先手 → 自己 `OnMoveToFrontline` → 0x32 列表。

    逐行对着本构建的 `BP_CardFunctions::ExecuteOnMoveToFrontlineCardEffects`（导出 @15297）实现，
    顺序/去重/隐蔽跳过都按字节码；`GetStopFurtherActions` 的**状态**读不到（`effectvm` 不拦截它）
    ⇒ 不做"猜出来的中止"，只把这件事写进返回的 `meta`（不编造）。
    返回 `{"hits", "order", "meta"}`；`hits[*].bucket` 全是 `"move"`。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "move"
    mp = _ptr_of(moved)
    args = {"cardMoved": mp, "forceMove": bool(force_move), "moveCost": int(move_cost)}
    cards = list(getattr(st, "cards", None) or [])
    done = set()
    # ① 反制先手（顺序 = ordered_gotchas；`cardsDone` 记下它们，后面 0x32 不再问）
    for c in ordered_gotchas(cards):
        ch.call("OnOtherCardMoveToFrontline", c, args)
        done.add(_ptr_of(c))
    # ② 自己的钩子（未压制才跑 —— BP 里 `if (!cardMoved->isSuppressed)`）
    if not ch.suppressed(moved):
        ch.call("OnMoveToFrontline", moved, {"forceMove": bool(force_move),
                                             "moveCost": int(move_cost)})
    done.add(mp)
    # ③ 0x32 列表：隐蔽未揭示 ⇒ 整段跳过（字节码 goto 1887）；`cardsDone` 去重
    if not _unrevealed_covert(ch.card(moved)):
        for c, _fn in ch.others("OnOtherCardMoveToFrontline", exclude=()):
            if _ptr_of(c) in done:
                continue
            ch.call("OnOtherCardMoveToFrontline", c, args)
            done.add(_ptr_of(c))
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"stop_semantics": "GetStopFurtherActions 未建模（读不到谁置位）",
                     "covert_skip": _unrevealed_covert(ch.card(moved))}}


def move_effects(km, st, moved, **kw) -> dict:
    """`run_move_frontline` → 一份 `Sim.move_fx[moved_card_id]` 能吃的效果摘要
    （`boardeval._apply_unit_eff` 语义：逐牌效果进 `unit_eff`，其余是全局效果）。没有命中 ⇒ `{}`。"""
    res = run_move_frontline(km, st, moved, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 抽到链（0x2A 旁观者段）
def run_drawn_from_deck(km, st, drawn_card_id: int, start_of_turn_draw: bool = False,
                        drawn_side: Optional[int] = None, stream=None, my_side=None,
                        read_hooks=None, budget_s: float = 0.8,
                        cache: Optional[TriggerCache] = None, include_own: bool = True,
                        kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """**一张牌被抽到**时的 0x2A 旁观者段（`ExecuteOnDrawnFromDeck`@13783）。

    只跑旁观者：抽到那张**自己**的 `OnCardDrawnFromDeck` 由评估侧的 `_on_draw` 摘要承担
    （那条是既有实现，本轮不动）；这里补的是"**别人看到有人抽牌**"的反应。
    列表里 `cardID == drawn_card_id` 的牌**跳过**（字节码 @13819-13824）。
    形参按名字传（`drawnCardID` / `StartOfTurnDraw` / `drawnSide`），名字写错 = VM 静默丢参。
    返回 `{"hits", "order"}`（`hits[*].bucket == "drawn"`）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "drawn"
    args = {"drawnCardID": int(drawn_card_id),
            "StartOfTurnDraw": bool(start_of_turn_draw),
            "drawnSide": int(drawn_side) if drawn_side is not None else None}
    for c, _fn in ch.others("OnOtherCardDrawnFromDeck", exclude=()):
        if int(getattr(c, "card_id", 0) or 0) == int(drawn_card_id):
            continue                              # 抽到的那张自己：字节码里显式跳过
        ch.call("OnOtherCardDrawnFromDeck", c, args)
    return {"hits": ch.hits, "order": ch.order}


def drawn_effects(km, st, drawn_card_id: int, **kw) -> dict:
    """`run_drawn_from_deck` → 一份 `Sim.draw_fx[drawn_card_id]` 能吃的效果摘要
    （逐牌效果进 `unit_eff`，其余是全局效果）。没有命中 ⇒ `{}`。"""
    res = run_drawn_from_deck(km, st, drawn_card_id, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def run_target_event(km, st, hook: str, card, stream=None, my_side=None, read_hooks=None,
                     budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                     include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                     enum_random: bool = True) -> dict:
    """**"以某张牌为对象"的单钩子族**（0x3A 被压制 / 0x3D 被定住）：按触发表序逐张问。

    两族的字节码形状相同（fetch 列表 → 逐张 `hook(card)`），差别只在触发号/钩子名/形参名，
    所以合成一个 runner（形参名由 `hook` 在 `SUPPRESS_HOOK_SPECS`/`PIN_HOOK_SPECS` 里查）。
    `hits[*].bucket == "target"`。返回 `{"hits", "order"}`。
    """
    from_specs = {**SUPPRESS_HOOK_SPECS, **PIN_HOOK_SPECS}
    sp = from_specs.get(hook)
    if not sp:
        raise KeyError("run_target_event 只认登记的族：%s" % sorted(from_specs))
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "target"
    ptr = _ptr_of(card)
    args = {sp["params"][0]: ptr}
    for c, _fn in ch.others(hook, exclude=()):
        ch.call(hook, c, args)
    return {"hits": ch.hits, "order": ch.order}


def _target_effects(km, st, hook: str, card, **kw) -> dict:
    res = run_target_event(km, st, hook, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def suppress_effects(km, st, card, **kw) -> dict:
    """0x3A `OnOtherCardSuppressed` → `Sim.event_fx["suppress"][card_id]` 能吃的效果摘要。"""
    return _target_effects(km, st, "OnOtherCardSuppressed", card, **kw)


def pin_effects(km, st, card, **kw) -> dict:
    """0x3D `OnOtherUnitPinned` → `Sim.event_fx["pin"][card_id]` 能吃的效果摘要。"""
    return _target_effects(km, st, "OnOtherUnitPinned", card, **kw)


# --------------------------------------------------------------------------- 回合族（0x14/0x40/0x19）
def has_custom_name1_attr(c, attr: str) -> bool:
    """`CustomName1HasAttribute(attr)`：`custom_name1` 按 `;` 分段、**整段匹配**（大小写不敏感）。

    口径见 `NATIVE-COVERAGE-1.60.md` §15（"CustomName1/2HasAttribute 按 ; 分段整段匹配"）。
    读的是 `kardsmem.cards.read_raw` 放进 `raw["custom_name1"]` 的 FName；读不到 ⇒ False。
    """
    raw = getattr(c, "raw", None) or {}
    cn = raw.get("custom_name1")
    return any(seg.strip().lower() == attr.lower() for seg in str(cn or "").split(";") if seg.strip())


def run_turn_family(km, st, hook: str, turn_number=None, stream=None, my_side=None,
                    read_hooks=None, budget_s: float = 1.0,
                    cache: Optional[TriggerCache] = None, include_own: bool = True,
                    kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True,
                    only_card=None) -> dict:
    """回合族（0x14 / 0x40 / 0x19）的钩子清单，顺序**照 BP 导出**（见 `TURN_HOOK_SPECS` 的 ev）。

    * 0x14：单趟、无参；
    * 0x40：三趟（`startofturn0` → `startofturn1` → 其余）；
    * 0x19：即时 → `endofturn1` → `endofturn2`；`card_unit_mosquito_*` 四张跳过。
    ★ 递归（这一趟里新登记进来的牌再跑一遍）在**静态快照**上是空转 ⇒ 不实现、在 `meta` 里写明；
      `RemoveBuffsEndOfTurn` 也不在这里（收尾规则属于 Sim，不属于钩子清单）。
    `only_card`：只问这一张（给"逐卡预计算"用，`rule._turn_end_fx` 就靠它把每张牌自己的
      0x19 效果存进 `Sim.event_fx["turn_end"]`）。
    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "turn"`）。
    """
    sp = TURN_HOOK_SPECS.get(hook)
    if not sp:
        raise KeyError("run_turn_family 只认 %s，收到 %r" % (sorted(TURN_HOOK_SPECS), hook))
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "turn"
    args = {sp["params"][0]: int(turn_number)} if sp["params"] else {}
    cards = [c for c, _fn in ch.others(hook, exclude=())]
    if only_card is not None:
        cards = [c for c in cards if _ptr_of(c) == _ptr_of(only_card)]
    if hook == "OnStartOfTurn":
        passes = (("startofturn0", True), ("startofturn1", True), (None, False))
        for attr, want in passes:
            for c in cards:
                if (has_custom_name1_attr(c, attr) if attr else
                        not (has_custom_name1_attr(c, "startofturn0")
                             or has_custom_name1_attr(c, "startofturn1"))):
                    ch.call(hook, c, args)
    elif hook == "OnEndOfTurn":
        skipped = []
        for c in cards:
            nm = str(getattr(c, "name", "") or "").lower()
            if nm in MOSQUITO_SKIP:
                skipped.append(nm)                      # BP：这四张"新登记"的跳过
                continue
            if not (has_custom_name1_attr(c, "endofturn1")
                    or has_custom_name1_attr(c, "endofturn2")):
                ch.call(hook, c, args)                  # ① 即时
        for attr in ("endofturn1", "endofturn2"):       # ② ③ 延迟的两趟
            for c in cards:
                if str(getattr(c, "name", "") or "").lower() in MOSQUITO_SKIP:
                    continue
                if has_custom_name1_attr(c, attr):
                    ch.call(hook, c, args)
    else:                                                # 0x14：单趟
        for c in cards:
            ch.call(hook, c, args)
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"hook": hook, "cards": len(cards),
                     "recursion": "递归（这一趟新登记的牌再跑）在静态快照下空转",
                     "remove_buffs": "RemoveBuffsEndOfTurn 不在钩子清单里（属 Sim 收尾）"}}


def _turn_effects(km, st, hook: str, **kw) -> dict:
    res = run_turn_family(km, st, hook, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def before_start_of_turn_effects(km, st, **kw) -> dict:
    """0x14 `OnBeforeStartOfTurn` 的效果摘要（回合开始前）。"""
    return _turn_effects(km, st, "OnBeforeStartOfTurn", **kw)


def start_of_turn_effects(km, st, **kw) -> dict:
    """0x40 `OnStartOfTurn` 的效果摘要（回合开始；三趟顺序）。"""
    return _turn_effects(km, st, "OnStartOfTurn", **kw)


def end_of_turn_effects(km, st, **kw) -> dict:
    """0x19 `OnEndOfTurn` 的效果摘要（回合结束；即时→1→2）。"""
    return _turn_effects(km, st, "OnEndOfTurn", **kw)


# --------------------------------------------------------------------------- 揭示族（0x37）
def run_reveal(km, st, revealed, stream=None, my_side=None, read_hooks=None,
               budget_s: float = 1.0, cache: Optional[TriggerCache] = None,
               include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
               enum_random: bool = True, with_enter_play: bool = True) -> dict:
    """`RevealCard`（BP @9191）的钩子段，顺序照字节码（见 `REVEAL_HOOK_SPECS` 的 ev）。

    自己的 `OnCardRevealed()`（**未被压制**才跑）→ 0x37 `OnOtherCardRevealed(cardRevealed)` →
    （`with_enter_play=True` 时）`OnEnterPlay(4)`（自己）+ 0x2B `OnOtherCardEnterPlay(cardRevealed, 4)`。
    ★ 0x2B 那段**不排除自己**（字节码里 fetch 后直接逐个调；和 `ExecuteOnEnterPlayEvents` 里
      "skip the card itself" 不同）—— 这里按字节码走，并把这点写进 `meta`。
    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "reveal"`）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "reveal"
    ptr = _ptr_of(revealed)
    own_ran = False
    if not ch.suppressed(revealed):                       # BP：!isSuppressed 才跑自己那条
        ch.call("OnCardRevealed", revealed, {})
        own_ran = True
    n37 = 0
    for c, _fn in ch.others("OnOtherCardRevealed", exclude=()):
        ch.call("OnOtherCardRevealed", c, {"cardBeingRevealed": ptr})
        n37 += 1
    n2b = 0
    if with_enter_play:
        ch.call("OnEnterPlay", revealed, {"Method": 4})   # 自己的 OnEnterPlay(4)
        for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=()):
            ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": ptr, "Method": 4})
            n2b += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own_ran, "n_0x37": n37, "n_0x2b": n2b,
                     "note": "0x2B 段按字节码不排除自己"}}


def reveal_effects(km, st, revealed, **kw) -> dict:
    """0x37 揭示族 → `Sim.event_fx["reveal"][card_id]` 能吃的效果摘要。"""
    res = run_reveal(km, st, revealed, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- 老兵 / 治疗 / 易主（2026-10-03，照 BP 逐行读过）
def _fam_effects(res, st, kw) -> dict:
    """`run_*` 的 `hits` → `Sim.event_fx[kind][card_id]` 能吃的效果摘要（与 reveal_effects 同一收口）。"""
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def _mk_chain(km, st, bucket, stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
              hq_own, hq_enemy, enum_random):
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = bucket
    return ch


def run_veteran(km, st, card, stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                cache: Optional[TriggerCache] = None, include_own: bool = True, kw_of=None,
                hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`MakeVeteran`（BP_CardFunctions@7127）尾段的钩子：未压制才跑自己的 `OnBecomingVeteran()`；
    fetch **0x20** 逐张 `OnOtherCardBecomingVeteran(card)`（不排除自己）；最后
    `ExecuteOnOtherCardsAbilitiesChanged(card)`（能力变化族，**未建模**，写进 meta）。"""
    ch = _mk_chain(km, st, "veteran", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    own = False
    if not ch.suppressed(card):
        ch.call("OnBecomingVeteran", card, {})
        own = True
    n = 0
    for c, _fn in ch.others("OnOtherCardBecomingVeteran", exclude=()):
        ch.call("OnOtherCardBecomingVeteran", c, {"cardBecomingVeteran": _ptr_of(card)})
        n += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own, "n_0x20": n,
                     "unmodeled": "ExecuteOnOtherCardsAbilitiesChanged(能力变化族)"}}


def veteran_effects(km, st, card, **kw) -> dict:
    return _fam_effects(run_veteran(km, st, card, **kw), st, kw)


def run_heal(km, st, card, to_heal: int, stream=None, my_side=None, read_hooks=None,
             budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
             kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`FullyHealCard`（BP_CardFunctions@701）：fetch **0xC** 逐张 `OnBeforeFullyRepaired(card, &stopAction)`，
    某张返回 stopAction 真 ⇒ 治疗中止（`meta.vetoed`）；否则防御设为最大防御后，fetch **0x2C** 逐张
    `OnOtherCardFullyRepaired(card, toHeal)`（`cardID != card` 才调）；最后**未压制**才 `card.OnFullyRepaired(toHeal)`。"""
    ch = _mk_chain(km, st, "heal", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ptr = _ptr_of(card)
    vetoed = False
    for c, _fn in ch.others("OnBeforeFullyRepaired", exclude=()):
        r = ch.call("OnBeforeFullyRepaired", c, {"cardRepaired": ptr, "stopAction": False})
        if (r.get("out") or {}).get("stopAction"):
            vetoed = True
            break
    n = 0
    if not vetoed:
        for c, _fn in ch.others("OnOtherCardFullyRepaired", exclude=[ptr]):
            ch.call("OnOtherCardFullyRepaired", c, {"cardRepaired": ptr, "amount": int(to_heal)})
            n += 1
        if not ch.suppressed(card):
            ch.call("OnFullyRepaired", card, {"amount": int(to_heal)})
    return {"hits": ch.hits, "order": ch.order, "meta": {"vetoed": vetoed, "n_0x2c": n}}


def heal_effects(km, st, card, to_heal: int, **kw) -> dict:
    res = run_heal(km, st, card, to_heal, **kw)
    eff = _fam_effects(res, st, kw)
    if res["meta"]["vetoed"]:
        eff["heal_vetoed"] = True          # boardeval：被否决 ⇒ 这次治疗不生效
    return eff


def run_steal(km, st, card, new_loc: int, old_loc: int, stream=None, my_side=None, read_hooks=None,
              budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
              kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`ChangeUnitOwnership`（BP_CardFunctions@18087，偷牌）的钩子段：
    ① `ExecuteOnBeforeLeaveBoardOrOwnerEvents(card, new, old, 5)`：未压制才自己的 `OnLeaveBoardOrOwner(new, 5)`，
       fetch **0x2E** 逐张（`cardID != card`）`OnOtherCardLeaveBoardOrOwner(card, new, 5)`；
    ② 换边（状态变更，sim 侧做）；
    ③ `ExecuteOnAfterLeaveBoardOrOwnerEvents(card, new, old)`：未压制才自己的 `OnAfterLeaveBoard(new)`，
       fetch **0x8** 逐张（`cardID != card`）`OnAfterOtherCardLeaveBoardOrOwner(card, old)`；
    ④ `ExecuteOnEnterPlayEvents(card, 0)`：自己的 `OnEnterPlay(0)` + 0x2B 逐张 `OnOtherCardEnterPlay(card, 0)`（排除自己）。
    ★ 钩子是在**换边前后各一次**调用，但这里是静态快照，换边前后的条件（谁是"我方"）读的是快照；
      未建模：`hasActivePincerEffect` 分支、`CardLocationMoved`。"""
    ch = _mk_chain(km, st, "steal", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ptr = _ptr_of(card)
    if not ch.suppressed(card):
        ch.call("OnLeaveBoardOrOwner", card, {"goingToLocation": int(new_loc), "leavePlayMethod": 5})
    for c, _fn in ch.others("OnOtherCardLeaveBoardOrOwner", exclude=[ptr]):
        ch.call("OnOtherCardLeaveBoardOrOwner", c, {"cardLeaving": ptr, "goingToLocation": int(new_loc),
                                                    "method": 5})
    if not ch.suppressed(card):
        ch.call("OnAfterLeaveBoard", card, {"goingToLocation": int(new_loc)})
    for c, _fn in ch.others("OnAfterOtherCardLeaveBoardOrOwner", exclude=[ptr]):
        ch.call("OnAfterOtherCardLeaveBoardOrOwner", c, {"cardLeaving": ptr, "oldLocation": int(old_loc)})
    ch.call("OnEnterPlay", card, {"Method": 0})
    for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=[ptr]):
        ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": ptr, "Method": 0})
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"unmodeled": "hasActivePincerEffect 分支 / CardLocationMoved"}}


def steal_effects(km, st, card, new_loc: int = 5, old_loc: int = 6, **kw) -> dict:
    return _fam_effects(run_steal(km, st, card, new_loc, old_loc, **kw), st, kw)


def run_convert(km, st, new_card, old_ids, new_ids, convert_to_name: str = "", instigator: int = 0,
                skip_trigger: bool = False, stream=None, my_side=None, read_hooks=None,
                budget_s: float = 1.0, cache: Optional[TriggerCache] = None, include_own: bool = True,
                kw_of=None, hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`ConvertCard`（BP_CardFunctions.cpp:12611，转化）的钩子段（对**新牌**，在场上才跑）：
    ① `ExecuteOnEnterPlayEvents(newCard, 5)`：自己的 `OnEnterPlay(Method=5)` + fetch **0x2B** 逐张
       `OnOtherCardEnterPlay(newCard, 5)`（排除自己）；
    ② `skipTrigger` 假时：fetch **0x22** 逐张 `OnOtherCardConverted(oldCardIDs, newCardIDs, newCardName, instigatorID)`
       （`cardID ∈ newCardIDs` 的跳过）。
    ★ 未证实：反编译里 0x22 那段在第一次调用后直接 `return`——疑似弹栈回循环（逐张都调），这里按“每张都调”；
      `meta.unverified` 如实标注。数组形参（旧/新 id）以 Python 列表传入，VM 读数组的行为实机未验。
      同 `run_steal`：只在假 VM 下验过顺序/形参。"""
    ch = _mk_chain(km, st, "convert", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ptr = _ptr_of(new_card)
    ch.call("OnEnterPlay", new_card, {"Method": 5})
    for c, _fn in ch.others("OnOtherCardEnterPlay", exclude=[ptr]):
        ch.call("OnOtherCardEnterPlay", c, {"cardPlayed": ptr, "Method": 5})
    n22 = 0
    if not skip_trigger:
        new_set = {int(x) for x in (new_ids or ())}
        for c, _fn in ch.others("OnOtherCardConverted", exclude=[]):
            if getattr(c, "card_id", None) in new_set:
                continue
            ch.call("OnOtherCardConverted", c, {"oldCardIDs": [int(x) for x in (old_ids or ())],
                                                "newCardIDs": [int(x) for x in (new_ids or ())],
                                                "newCardName": str(convert_to_name),
                                                "instigatorID": int(instigator)})
            n22 += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"n_0x22": n22,
                     "unverified": "0x22 是否逐张都调（反编译首次调用后 return）；数组形参的 VM 读法"}}


def convert_effects(km, st, new_card, old_ids, new_ids, **kw) -> dict:
    return _fam_effects(run_convert(km, st, new_card, old_ids, new_ids, **kw), st, kw)


def fight_damage(km, st, a, b, stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                 cache: Optional[TriggerCache] = None, include_own: bool = True, kw_of=None,
                 hq_own=(), hq_enemy=(), enum_random: bool = True) -> dict:
    """`MakeCardsFight(unitThisSide=a, unitOppositeSide=b)`（BP_CardFunctions@9755）的两个方向的伤害，
    **过改伤钩子**：`ExecuteOnDealDamageAddDamage(dealer, target, atk, fromAttack=False, fromFight=True, False)`
    → `ExecuteOnDealDamageAddDamageAfterCalc(target, dealer, calc, False, False, False)`（受击方免疫 ⇒ 0）。
    a→b 与 b→a 各算一次（Sequence，两边先算后一起打）。返回 `{"to_b", "to_a", "hits", "order"}`。
    未建模：`ApplyDamageToCard` 里的受伤钩子链（BeforeReceiveDamage / excess …）。"""
    ch = _mk_chain(km, st, "fight", stream, my_side, read_hooks, budget_s, cache, include_own, kw_of,
                   hq_own, hq_enemy, enum_random)
    ca, cb = ch.card(a) or a, ch.card(b) or b
    calc_b = ch.add_damage(a, b, _atk(ca), False, True, False)
    to_b = ch.after_calc(b, a, calc_b, False, False, False)
    calc_a = ch.add_damage(b, a, _atk(cb), False, True, False)
    to_a = ch.after_calc(a, b, calc_a, False, False, False)
    return {"to_b": int(to_b), "to_a": int(to_a), "hits": ch.hits, "order": ch.order}


# --------------------------------------------------------------------------- 离开前线（0x31）/ 撤退（0x36）
def run_move_from_frontline(km, st, card, stream=None, my_side=None, read_hooks=None,
                            budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                            include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                            enum_random: bool = True) -> dict:
    """`ExecuteOnCardMoveFromFrontline(cardMoved)`（BP @14281）：自己（未压制）→ 0x31 列表（排除自己）。

    返回 `{"hits", "order", "meta"}`（`hits[*].bucket == "move_front"`）。
    `meta.stop_semantics` 如实标注 `GetStopFurtherActions` 未建模（§7 未查第 1 条）。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "move_front"
    ptr = _ptr_of(card)
    own_ran = False
    if not ch.suppressed(card):
        ch.call("OnMoveFromFrontline", card, {})
        own_ran = True
    n = 0
    for c, _fn in ch.others("OnOtherCardMoveFromFrontline", exclude=()):
        if _ptr_of(c) == ptr:                                # 字节码：cardID == 自己 ⇒ 跳过
            continue
        ch.call("OnOtherCardMoveFromFrontline", c, {"cardMoved": ptr})
        n += 1
    return {"hits": ch.hits, "order": ch.order,
            "meta": {"own_ran": own_ran, "n_0x31": n,
                     "stop_semantics": "GetStopFurtherActions 未建模（读不到谁置位）"}}


def move_from_frontline_effects(km, st, card, **kw) -> dict:
    """0x31 离开前线族 → 效果摘要（`Sim.event_fx["move_front"][card_id]` 可吃）。"""
    res = run_move_from_frontline(km, st, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


def run_retreat(km, st, card, stream=None, my_side=None, read_hooks=None,
                budget_s: float = 0.8, cache: Optional[TriggerCache] = None,
                include_own: bool = True, kw_of=None, hq_own=(), hq_enemy=(),
                enum_random: bool = True) -> dict:
    """`ApplyMakeCardRetreat`（BP @16567）的钩子段：自己 `OnBeforeRetreat(&stopAction)`（未压制）→
    0x36 列表逐张 `OnOtherCardRetreat(card,&stopAction)`（**不排除自己**）。

    任何一步 `stopAction=true` ⇒ **中止**（BP：自己的那条直接 return；0x36 那条打日志后 return），
    记进 `meta["stopped_by"]`（这是原版规则行为，不是缺口）。返回 `{"hits","order","meta"}`。
    """
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own,
                kw_of or _kw_default, hq_own, hq_enemy, enum_random=enum_random)
    ch.bucket = "retreat"
    ptr = _ptr_of(card)
    meta = {"own_ran": False, "n_0x36": 0, "stopped_by": None}
    if not ch.suppressed(card):
        r = ch.call("OnBeforeRetreat", card, {})
        meta["own_ran"] = True
        if ch.outv(r, "stopAction"):
            meta["stopped_by"] = "OnBeforeRetreat"
            return {"hits": ch.hits, "order": ch.order, "meta": meta}
    for c, _fn in ch.others("OnOtherCardRetreat", exclude=()):
        r = ch.call("OnOtherCardRetreat", c, {"cardRetreated": ptr})
        meta["n_0x36"] += 1
        if ch.outv(r, "stopAction"):
            meta["stopped_by"] = "OnOtherCardRetreat:%s" % (getattr(c, "name", "?"),)
            break
    return {"hits": ch.hits, "order": ch.order, "meta": meta}


def retreat_effects(km, st, card, **kw) -> dict:
    """0x36 撤退族 → 效果摘要（`Sim.event_fx["retreat"][card_id]` 可吃）。"""
    res = run_retreat(km, st, card, **kw)
    bk = merge_hits(res["hits"], st, kw.get("my_side"), kw.get("hq_own", ()), kw.get("hq_enemy", ()))
    eff, units = {}, {}
    for b in bk.values():
        eff = _merge_eff(eff, b["eff"])
        for k, e in b["units"].items():
            units[k] = _merge_eff(units.get(k, {}), e)
    if units:
        eff["unit_eff"] = [(k, e) for k, e in units.items() if e]
    return eff


# --------------------------------------------------------------------------- selftest（离线）
def selftest() -> int:
    bad = 0

    def chk(name, ok):
        nonlocal bad
        print(("PASS " if ok else "FAIL ") + name)
        bad += 0 if ok else 1

    chk("method_of：打出=1 / 生成=3 / 揭示=4 / 转化=5",
        [method_of(x) for x in ("play_unit", "spawn", "reveal", "convert", "???")]
        == [1, 3, 4, 5, 1])
    chk("METHODS 覆盖 1..5", sorted(METHODS) == [1, 2, 3, 4, 5])

    class _FakeOA:
        def __init__(self, km):
            pass

        def class_of(self, p):
            return 1000 + p

    class _K:                       # 假 km：让 kismet.find_function 走缓存
        pass

    # 假 cache：只有类 1002 有钩子
    class _TC(TriggerCache):
        def __init__(self):
            self._fn = {}

        def fn_of_class(self, uc, hook=HOOK):
            return 7 if (uc == 1002 and hook == HOOK) else 0

    class _C:
        def __init__(self, name, loc, p, side="local"):
            self.name, self.location, self.raw, self.side = name, loc, {"ptr": p}, side
            self.is_suppressed = False

    st = type("S", (), {})()
    st.cards = [_C("A", "frontline", 1), _C("B", "back", 2, "enemy"),
                _C("C", "hand", 3), _C("D", "frontline", 4)]

    import sys as _sys
    import types as _types
    import kardsmem.objects as _oa
    old = _oa.ObjectArray
    _oa.ObjectArray = _FakeOA
    try:
        got = find_cards(_K(), st, cache=_TC())
        got_ex = find_cards(_K(), st, exclude=[2], cache=_TC())
    finally:
        _oa.ObjectArray = old
    chk("find_cards：只收场上且挂了钩子的（B 命中，手牌/无钩子不算）",
        [c.name for c, _ in got] == ["B"])
    chk("find_cards：exclude 生效", [c.name for c, _ in got_ex] == [])
    chk("HOOKS 覆盖进场 + 反制三兄弟",
        set(HOOKS) == {HOOK, "OnBeforeOtherCardPlayedFromHand",
                       "OnOtherCardPlayedFromHand", "OnCounterMeasureTriggered"})
    chk("反制族单独成表", tuple(COUNTER_HOOKS) == ("OnBeforeOtherCardPlayedFromHand",
                                               "OnOtherCardPlayedFromHand",
                                               "OnCounterMeasureTriggered"))
    chk("攻击族已登记：触发号 → 形参表（0x04 是 (defender, attacker)，其余是 (attacker, defender)）",
        {"OnOtherCardAttacks", "OnBeforeOtherCardAttacks", "OnAfterOtherCardAttacks",
         "OnOtherCardAttackSwitchTarget", "OnOtherCardReceiveDamage",
         "OnOtherCardOperationKreditsSpent"} <= set(ATTACK_HOOKS)
        and ATTACK_HOOKS["OnAfterOtherCardAttacks"] == ("defenderCard", "attackerCard",
                                                        "damageToDefender", "attackCost")
        and ATTACK_HOOKS["OnBeforeOtherCardAttacks"] == ("cardAttacking", "defenderCard")
        and ATTACK_HOOKS["OnOtherCardAttacks"][0] == "cardAttacking"
        and ATTACK_HOOK_SPECS["OnAfterAttack"]["params"] == ("defenderCard", "wasShockAttack", "attackCost"))
    chk("触发号：0x04/0x0D/0x1E/0x1F/0x34/0x44",
        [ATTACK_HOOK_SPECS[h]["trigger"] for h in ("OnAfterOtherCardAttacks", "OnBeforeOtherCardAttacks",
                                                   "OnOtherCardAttackSwitchTarget", "OnOtherCardAttacks",
                                                   "OnOtherCardReceiveDamage",
                                                   "OnOtherCardOperationKreditsSpent")]
        == [0x04, 0x0D, 0x1E, 0x1F, 0x34, 0x44])
    chk("原生默认：0x1F 两个 false；0x1E newDefender=oldDefender（换 oldDefender 答案跟着变）；"
        "ImplementableEvent 没有原生默认",
        native_default("OnOtherCardAttacks", {}) == {"stopAttack": False, "AttackedAndStopped": False}
        and native_default("OnOtherCardAttackSwitchTarget", {"oldDefender": 5}) == {"newDefender": 5}
        and native_default("OnOtherCardAttackSwitchTarget", {"oldDefender": 9}) == {"newDefender": 9}
        and native_default("OnAfterAttack", {}) == {})
    # 攻击族顺序（字节码）：[自己 OnBeforeAttack → 0x0D → 自己 OnAfterAttack → 0x04 → 自己/0x44 花费通知]
    calls = []

    def _fake_ex(km2, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
                 hq_own=(), hq_enemy=(), st=None):
        calls.append((hook, dict(args)))
        return {"ran": True, "eff": {"damage_hq": 2}, "stopped": None, "out": {}, "records": []}

    _g = globals()
    old_find, old_run = find_cards, _run_hook_ex
    _g["find_cards"] = lambda *a, **k: [(_C("W", "frontline", 9), 7)]
    _g["_run_hook_ex"] = _fake_ex
    try:
        atk, dfd = _C("ATK", "frontline", 1), _C("DEF", "frontline", 2)
        r = run_attack_hooks(_K(), st, atk, dfd, damage=3, cost=1)
    finally:
        _g["find_cards"], _g["_run_hook_ex"] = old_find, old_run
    hs = [h for h, _a in calls]
    chk("攻击族顺序：OnBeforeAttack → 0x0D → OnAfterAttack → 0x04 → 花费通知(自己→0x44)",
        [h for h in hs if h in ("OnBeforeAttack", "OnBeforeOtherCardAttacks", "OnAfterAttack",
                                "OnAfterOtherCardAttacks", "OnOperationKreditsSpent",
                                "OnOtherCardOperationKreditsSpent")]
        == ["OnBeforeAttack", "OnBeforeOtherCardAttacks", "OnAfterAttack", "OnAfterOtherCardAttacks",
            "OnOperationKreditsSpent", "OnOtherCardOperationKreditsSpent"])
    a04 = [a for h, a in calls if h == "OnAfterOtherCardAttacks"][0]
    chk("0x04 实参：defenderCard=防守方、attackerCard=攻击方（不能反）、damageToDefender=3、attackCost=1",
        a04 == {"defenderCard": 2, "attackerCard": 1, "damageToDefender": 3, "attackCost": 1}
        and r["outcome"] == "resolved" and r["paid"] == 1)
    a0d = [a for h, a in calls if h == "OnBeforeOtherCardAttacks"][0]
    chk("0x0D 实参名是 cardAttacking/defenderCard（旧版写成 attackerCard ⇒ VM 收不到参数）",
        a0d == {"cardAttacking": 1, "defenderCard": 2})
    print("\n%d 项失败" % bad)
    return bad


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(1 if selftest() else 0)
