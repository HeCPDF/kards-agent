#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""semantics.effectvm —— 用外部 Kismet VM 把一张牌的钩子**空跑一遍**，截获「写」类调用做效果摘要。

为什么不再只靠静态数动词（用户 2026-09-30）：
    字节码里出现某个动词 ≠ 这一次会发生（条件在分支里）；数量除非是字节码里的常量，
    否则静态读不出来。VM 用**当前真实局面**把分支走一遍：条件按真实状态成立与否，
    数量按真实状态求值，写类调用被钩子截住、只记录、不执行（红线：读侧只读）。

设计
----
* 读类原语/游戏自有查询：VM 照常执行（`make_get_field` 反射链读内存 + `CardNatives`）。
* 写类动词（`DamageCard`/`GiveKreditsBySide`/`GiveBlitz`/…）：钩子里**记录**
  `(动词, 已求值的实参)`，返回 None。签名来自 SDK（`BP_CardFunctions_classes.hpp`），
  所以实参下标是 API 词汇，不是逐张牌的表。
* **随机**：按真随机对待，**不预测种子**（用户 2026-09-30：RNG 报告讲的是旧版本的重播种机制，
  现行版本又更新过；SpyRing.md 描述的是错误重播种后的表现，别参考）。做法是**枚举**：
  随机原语（`RandomIntFromRangeWithStream`/`GetRandomCard`/`GiveRandomCombatKeyword`）
  在钩子里**按指定结果返回**，对每个可能结果各空跑一次（`enumerate_effects`），各分支等概率，
  调用方取期望。能枚举的：整数范围、数组随机元素、随机战斗关键词（7 个）。
  不能枚举的随机（域未知）只记机会节点并「污染」之后的记录（归入 `uncertain`）。
  VM 自己的随机原语保持拒绝（不替游戏掷骰子）。
* 局限：分支之后依赖「随机结果已经写进游戏状态」的判断（例如 FORGED IN FIRE 的「现在有
  2 个以上关键词就抽一张」）VM 读的是**未更新的真实状态**，会判错；这类牌的枚举结果只是近似。
* 目标：`GetTargetedCard` 钩子喂「当前指向的是谁」。带目标的牌调用方对每个候选目标各跑一次。

诚实的限制
----------
* **没有在真进程上验证过**（写这一版时游戏没开）。VM 覆盖不到的原语会让运行在中途
  `stopped`——此时只保留已记录到的部分，并如实标 `complete=False`；调用方退回静态常量摘要。
* 只挑不判：这里的结果只用来**估值**；出不出、能不能，一律问游戏。
"""
from __future__ import annotations

from typing import Optional

from kardsmem.kismetlib import Unimplemented

# ---------------------------------------------------------------------------
# API 词汇（来自 SDK 的函数签名；args 下标 = 声明的入参次序，出参不计）
#   动词 → (效果键, 数量所在的实参下标 或 None, 作用对象所在的实参下标 或 None)
# ---------------------------------------------------------------------------
COUNT_VERBS = {
    # GiveKreditsBySide(side, kredits, instigatorID, &qqq)
    "GiveKreditsBySide": ("kredit", 1, None, 0),
    # LoseKreditSlot(side)
    "LoseKreditSlot": ("slot_loss", None, None, 0),
    # setKreditSlotBySide(side, newValue)：写槽的绝对值；增量 = newValue − 当前槽（to_effects 里算）
    "setKreditSlotBySide": ("slot_set", 1, None, 0),
    # GainKreditSlot(cardGivingKreditSlot, side)：+1 槽。★ 直接钩住它，别让 VM 跑进里面的
    # ChangeKreditSlotsBySide → setKreditSlotBySide(绝对值)：绝对值减 `st.slots` 的差分里两个量
    # 不是同一个东西（实机 NZANS「获得 1 个额外槽」被算成 -1）。side 在参数下标 1。
    "GainKreditSlot": ("slot", None, None, 1),
    # ChangeKreditSlotsBySide(side, delta, giverID)：增量就在参数里
    "ChangeKreditSlotsBySide": ("slot", 1, None, 0),
    # DrawCardsFromDeckBySide(instigatorID, side, NumCards, cardSeen, OpponentDraw, &ids, delay)
    "DrawCardsFromDeckBySide": ("draw", 2, None, 1),
    # DamageCard(card, amount, damagerCardID, isRedirected, fromFight, isFightDefenderDamage, &destroyed)
    "DamageCard": ("damage", 1, 0, None),
    # DamageMultipleCards(receiverIDs[], amount, damagerCardID, &destroyed[])
    "DamageMultipleCards": ("damage_aoe", 1, 0, None),
    # ChangeAttack/Defense/HeavyArmor/KreditCost(card, instigatorID, amount, ChangeType, ...)
    "ChangeAttack": ("attack", 2, 0, None),
    "ChangeDefense": ("defense", 2, 0, None),
    "ChangeHeavyArmor": ("armor", 2, 0, None),
    "ChangeKreditCost": ("cost", 2, 0, None),
    "ChangeOperationCost": ("opcost", 2, 0, None),
    # AddAttackUntilEndOfTurn(card, instigatorID, attackToAdd)
    "AddAttackUntilEndOfTurn": ("attack_turn", 2, 0, None),
    # SpawnCardOnBattlefield/InFrontline(side, ..., card_name, ...)
    "SpawnCardOnBattlefield": ("spawn", None, None, 0),
    "SpawnCardInFrontline": ("spawn", None, None, 0),
    "SpawnCardInHandBySide": ("gain_cards", None, None, 0),
    "SetCountdown": ("countdown", 1, 0, None),
    # ★ 2026-10-02 BySide 族（IDA：setKreditBySide 0x144B3C660 写 GameState 加密记录；ChangeKreditsBySide 是 BP 包装）
    # ChangeKreditsBySide(sideToChange, kreditsAmount, instigatorID)：增量就在参数里 ⇒ 同 GiveKreditsBySide
    "ChangeKreditsBySide": ("kredit", 1, None, 0),
    # setKreditBySide(sideToSet, newKredit, key1, key2, FrameCount)：**绝对值**，增量 = newKredit − 当前指挥点
    "setKreditBySide": ("kredit_set", 1, None, 0),
    # DrawSpecificCardFromDeckBySide(instigatorID, CardID, side, cardSeen)：抽指定的 1 张
    "DrawSpecificCardFromDeckBySide": ("draw", None, None, 2),
}
FLAG_VERBS = {
    "DestroyCard": ("destroy", 0), "DestroyMultipleCards": ("destroy_aoe", None),
    # ★ 2026-10-02（§8-8 压制族）：`PinUnit` 与 `SuppressUnit` **不是一回事** ——
    #   `SuppressMultipleUnits`（BP @7456）在"新压制"分支里会 RemoveGuard/Fury/Blitz/Immune/
    #   Alpine/Ambush/Mobilize/Smokescreen/Salvage/Shock、重甲清零、清 customName1/2、清指向税，
    #   而 `PinUnit`（@971）只动 receivedAbilities/pinnedTurns。两个触发号也不同（0x3A / 0x3D）
    #   ⇒ 摘要键必须分开（以前都记成 "pin"，压制族钩子根本选不对）。
    "PinUnit": ("pin", 0), "SuppressUnit": ("suppress", 0), "RemovePin": ("unpin", 0),
    "MakeCardRetreat": ("retreat", None), "MoveUnitFromBoardToOwnersHand": ("retreat", 0),
    "FullyHealCard": ("heal_unit", 0), "TakeControlOfEnemyUnit": ("steal", 0),
    "MakeVeteran": ("veteran", 0), "ConvertCard": ("convert", None),
    "RemoveGuard": ("remove_guard", 0), "RemoveBlitz": ("remove_blitz", 0),
    # ★ 攻击链钩子里出现的、会改变对局状态的动词（ATTACK-HOOKS-1.60.md §6）：失去关键词 / 离场 / 结束对局
    "RemoveImmune": ("remove_immune", 0), "RemoveFury": ("remove_fury", 0),
    "RemoveAmbush": ("remove_ambush", 0), "RemoveSmokescreen": ("remove_smokescreen", 0),
    "RemoveAlpine": ("remove_alpine", 0), "RemoveSalvage": ("remove_salvage", 0),
    "RemoveShock": ("remove_shock", 0), "RemoveMobilize": ("remove_mobilize", 0),
    "RemoveCardFromBoard": ("remove_unit", 0),            # 离场但不算"被摧毁"（不触发 OnDestroyed）
    "MoveCardToTopOfOwnersDeck": ("to_deck", 0),
    # selectTargetFromHand(cardSelectingHandTarget, &qqq)：体内只有 `CardFunctionsNotifier->
    # NotifySelectHandTargetPending`（等玩家点手牌，纯表现），VM 里 Context 到 Notifier 会停
    # （175th INFANTRY REGIMENT / 所有"选手牌"部署）。真正的后果在之后的 `OnHandTargetSelected`
    # ⇒ 这里只记"有一次手牌选择待做"，boardeval 如实记缺口，不编选了哪张。
    "selectTargetFromHand": ("hand_target_pending", None),
    # Forecast(cardTriggeringForecast, &qqq)（BP_CardFunctions@23707）：体内只有
    # `NotifySelectCardToDrawPending(cardID, false, [蓝天,薄雾,狂风], false)` —— 弹出预报第一层三选一。
    # 记成 `forecast` 标记；`rule._hand_eff` 会用活种子预测的 9 条路径把它展开成 `outcomes`（取最好）。
    "Forecast": ("forecast", None),
    # selectCardToDraw(cardID, markAsSeen, isEffect, &drawnCardID)（BP_CardFunctions）：弹出"三选一加入手牌"
    # （CRUISER SCOUTS / SOUL OF OLD JAPAN / 好人寥寥…），候选来自这张牌自己的 `GetChooseSpawnCards`。
    # 记成 `choose_spawn` 标记；候选由 `semantics/choosespawn.py` 按活种子预测、sim 挂起成单层 `select_card_to_draw` 提示。
    "selectCardToDraw": ("choose_spawn", None),
    # MoveUnitFromSupportToFrontLine(card, instigatorID, &qqq)：把支援线单位推上前线
    #   —— BP：前线归属 == 目标对侧 ⇒ 拒绝（前线被对方占）；自己前线满 ⇒ 拒绝；否则 MoveCardToFrontline。
    "MoveUnitFromSupportToFrontLine": ("move_front", 0),
    "EndMatch": ("end_match", None),                       # EndMatch(winnerSide, delay)
    "ResetUnitOperations": ("reset_ops", 0),               # 这张单位的行动次数重置（可以再动/再打一次）
    # 多目标版本（实参 0 是一组卡 id；目标清单在 `rec.records` 里，semantics/triggers.py::hit_effects 把它们拆成逐牌效果）
    "SuppressMultipleUnits": ("suppress_aoe", None), "RemoveMultipleCardsFromBoard": ("remove_aoe", None),
    "MoveMultipleCardsToTopOfOwnersDeck": ("to_deck_aoe", None),
    "MakeCardsFight": ("fight", None),                     # MakeCardsFight(unitThisSide, unitOppositeSide, instigatorID)
}
GIVE_KW = {"GiveBlitz": "blitz", "GiveFury": "fury", "GiveShock": "shock",
           "GiveGuard": "guard", "GiveAmbush": "ambush", "GiveSmokescreen": "smokescreen",
           "GiveImmune": "immune", "GiveAlpine": "alpine", "GiveSalvage": "salvage",
           "GiveMobilize": "mobilize"}
# customJson 的读写（`BP_CardFunctions::JSON_*`，底下是 BlueprintJsonLibrary 原生，VM 里没有）。
# 我们在 Recorder 里放一份**影子 customJson**：写进影子、读从影子读；影子的初值惰性地从活内存读
# （`kardsmem.cards.read_custom_json_raw`，只解得出 bool；int/string 初值读不出 ⇒ 记进 `rec.gaps`）。
JSON_SET = frozenset(("JSON_SetBool", "JSON_SetInt", "JSON_SetString"))
JSON_ARR_SET = frozenset(("JSON_SetBoolArray", "JSON_SetIntArray", "JSON_SetStringArray"))
JSON_ARR_ADD = frozenset(("JSON_AddToBoolArray", "JSON_AddToIntArray", "JSON_AddToStringArray"))
JSON_ARR_DEL = frozenset(("JSON_RemoveFromBoolArray", "JSON_RemoveFromIntArray", "JSON_RemoveFromStringArray"))
JSON_GET = frozenset(("JSON_GetBool", "JSON_GetInt", "JSON_GetString"))
JSON_ARR_GET = frozenset(("JSON_GetBoolArray", "JSON_GetIntArray", "JSON_GetStringArray"))
JSON_VERBS = (JSON_SET | JSON_ARR_SET | JSON_ARR_ADD | JSON_ARR_DEL | JSON_GET | JSON_ARR_GET
              | frozenset(("JSON_Clear",)))
# 改"当前行动方"：SetPlayingSide / SetActiveSide（AkardsGameState/AkardsGameMode 原生）与 BP 包装 SwitchPlayingSide。
# 参数 0 = 新的座位号；记成 eff["playing_side"] = "local"|"enemy"，由 boardeval 的 `_apply_eff` 消费。
SIDE_VERBS = frozenset(("SetPlayingSide", "SetActiveSide", "SwitchPlayingSide"))
# 强制结束当前回合：`BP_CardFunctions::ForceEndTurn()`（久留米联队"Destroyed: End the turn"、
# BANZAI CHARGE / REPEL THE ATTACK / PROTECT THE POCKET / CALM BEFORE THE STORM 等）。
# ★ 2026-10-02（用户："比如久留米联队"）：它**不进** `CanEndTurn` —— 体内只是
#   `CardFunctionsNotifier->NotifyForceEndTurn(true)`；对我们 eval 的语义 =
#   "当前回合立刻结束 ⇒ 之后没有我方动作"，所以记成 `playing_side="enemy"`
#   （与 SetPlayingSide 同一个消费点：boardeval.gen_actions 随即不再产生我方动作）。
END_TURN_VERBS = frozenset(("ForceEndTurn",))
# 洗**整副牌**：`BP_CardFunctions::ShuffleDeckBySide(sideToShuffle, skipSubAction, instigatorID, &qqq)`
#   —— 体内就是 `Array_ShuffleFromStream(GetDeckBySide(side), cardsRandomStream)` + 写回。
# 记成 `eff["deck_shuffle"] = "local"|"enemy"`；boardeval 用 **同一套** 洗牌
# （`kardsmem.rng.Stream.shuffle`，正向 Fisher-Yates、N 次抽取）在模拟牌库上复算，
# 后续抽牌就按洗后的牌序（用户 2026-10-02："洗牌并抽一，就按模拟洗完后抽到的那张算"）。
DECK_VERBS = frozenset(("ShuffleDeckBySide",))
# 收缴：`SalvageMultipleUnits(cardsToSalvage[], instigatorID, &qqq)`。
#   BP：instigator 的 side 取 `GetHandLocationBySide`；手牌 location 满 ⇒ 跳过；
#   否则对每张 CreateCard(handLocation, spawnCardInHand=true) + `ApplySalvageChanges`
#   ⇒ 非老兵复制 = **攻/防 1、费用 min(费用,3)**，蓝图逻辑（效果/关键词）原样保留。
# 记成 `eff["salvage_ids"] = [被收缴的卡 id]`；boardeval 用 `card_templates` 造 1/1 复制进手牌。
SALVAGE_VERBS = frozenset(("SalvageMultipleUnits", "SalvageUnit"))
# 压制回合数：`ChangedPinnedTurns(cardID, instigatorID, turnsToChange, &qqq)`
#   —— BP：`pinnedTurns = clamp(pinnedTurns + turnsToChange, 0, 5)`。正 = 压更久，负 = 减少/解除。
PIN_VERBS = frozenset(("ChangedPinnedTurns",))
# 把场上牌洗进某方牌库：`StealCardFromBoardToDeck(cardID, instigatorID, deckSide, &qqq)`
#   —— BP：`ApplyRemoveCardFromBoard`(目标离场) → `CreateCard(deckSide, 同名, deckLocation)`
#   → `AddCardToDeckBySide(deckSide, 新id, true, -1)` → `ShuffleDeckBySide(deckSide)`。
STEAL_VERBS = frozenset(("StealCardFromBoardToDeck",))
# 往牌库里塞牌：`SpawnCardInDeckBySide(side, card_name, spawnerID, numberOfCards, salvageFaction,
#   HideFromOpponent, bottom, shuffle, SkipDrawAnimation, RandomWithoutShuffle, &ids)`
#   —— BP：每张 CreateCard(deckLocation) → `AddCardToDeckBySide(side, id, addToTop=!bottom, SelectInt)`
#   （SelectInt 默认 -1 = addToTop/尾部；RandomWithoutShuffle 时 = RandomIntFromRangeWithStream(0, len)）；
#   循环里**每张都无条件抽一次 RandomIntFromRangeWithStream(0, len)**（即使结果不用）——
#   这步随机消耗必须复刻，否则之后的洗牌/抽牌会跟游戏的种子错位。循环结束后 shuffle ⇒ ShuffleDeckBySide。
SPAWN_DECK_VERBS = frozenset(("SpawnCardInDeckBySide",))
# 揭示：`RevealCard(cardID, instigatorID, &qqq)` —— BP：`isRevealed = true; hasCovert = false`。
#   ★ 只记**状态变化**：BP 之后还会跑一串揭示钩子 —— 未被压制时 `cardRevealed->OnCardRevealed()`、
#     `FetchAllCardsWithEventTrigger(0x37)` 逐个 `OnOtherCardRevealed(cardRevealed)`、
#     `cardRevealed->OnEnterPlay(0x4)`、`FetchAllCardsWithEventTrigger(0x2B)` 逐个
#     `OnOtherCardEnterPlay(cardRevealed, 0x4)`。这些属于 triggers 钩子族，
#     由 triggers 侧补（TODO §3g-3；subagent 撞限额，2026-10-02 16:11 恢复后处理），本模块不假装跑过。
REVEAL_VERBS = frozenset(("RevealCard",))
# 群体加防：`AddDefenseToMultipleCards(receiverIDs[], amount, giverCardID, &qqq)`
#   amount ≥ 0 ⇒ 每张走 `ChangeDefense(+amount)` 管线；amount < 0 ⇒ `setAndEncryptDefense(总防+amount)`
#   （把总防御**设成**当前值减 |amount|，≤0 的由末尾 `DestroyMultipleCards` 摧毁）。
DEFENSE_AOE_VERBS = frozenset(("AddDefenseToMultipleCards",))
# 指向税：`AddKreditsTax(card, costToAdd, instigatorID, &qqq)`
#   —— BP：`card->KreditsTax_AsEnemyTarget = max(0, KreditsTax_AsEnemyTarget + costToAdd)`。
#   语义 = "这张牌被敌方指向时，敌方的操作/出牌费用 +tax" ⇒ 加在**己方**单位上对对手不利（有利我们）。
TAX_VERBS = frozenset(("AddKreditsTax",))
# 对局限制：`AddGameplayRestriction(side, type, cardID, turnsToLast)` /
#   `RemoveGameplayRestriction(side, type, cardID, removeAll, &qqq)`
#   —— GameState 的 `GameplayRestrictionEffects` 数组（结构体 4 字段）。type 枚举（从 BP 调用点反推）：
#     0=cannotDrawCardAtTurnStart, 1=cannotKreditSlotAtTurnStart, 2=cannotPlayOrders,
#     3=cannotDeployUnits, 4=cannotAttackWithGroundUnits, 5=cannotDiscardAnyCardFromHand。
RESTRICTION_VERBS = frozenset(("AddGameplayRestriction", "RemoveGameplayRestriction"))
# 情报触发源：`SetCardsSeenByCipher(numberOfCardsSeen, instigatorID, side, &qqq)`
#   —— BP：遍历所有带 trigger 0x1C 的卡调 `OnIntelTriggered(instigatorCard, intelValue)`，
#   再把对方 N 张手牌标 cardSeen（＝看手牌，估值不建模）。
#   ★ 触发源**不止 `cipher>0` 的卡**：CRUISER SCOUTS（`n=3`）、STRETCH THE LINE
#   （`n=场上 LEGION 数量`）是在自己的 `OnPlayedFromHand` 里直接调它的 —— 用户 2026-10-02
#   实机点破："这个就是 `_intel_triggers` 的例子啊"。
#   记成 `eff["intel_seen"] = n`，由 `rule._intel_triggers` 用它跑 0x1C 触发。
INTEL_VERBS = frozenset(("SetCardsSeenByCipher",))
# 弃手牌：DiscardCardFromHand(CardID, discarderID, ...)：记 eff["discard_ids"]（卡 id 列表）；己方手牌里有就从手牌移除。
DISCARD_VERBS = frozenset(("DiscardCardFromHand",))
# 胜负相关的**写**原生，但 boardeval 没有对应的状态量（卡面数值写入走 BP 动词 ChangeAttack 等已被钩住，
# 这些是它们底下的叶子；自定义名/游戏标签/复制/额外触发是之后卡牌判据用的标记）：
# 只记进 `rec.records`（诊断 + 将来消费），不进效果摘要，**不执行**（红线：读侧只读）。
RECORD_ONLY = frozenset(("setAndEncryptAttack", "setAndEncryptAttackBuff", "setAndEncryptDefense",
                         "setAndEncryptKredit", "setAndEncryptKreditBuff",
                         "CustomName1Add", "CustomName1Remove", "CustomName2Add", "CustomName2Remove",
                         "ApplyGameplayEffect", "RemoveGameplayEffectByInstigator",
                         "CopyData", "CreateCopy", "ResetCardAttributes",
                         "SetExtraPlayTriggers", "UpdateExtraPlayTriggers", "RemoveGameplayTag"))
# 随机：只记机会节点，并污染其后的记录
RANDOM_VERBS = frozenset(("GiveRandomCombatKeyword", "GetRandomCard", "DiscardRandomCardFromHand",
                          "RandomIntFromRangeWithStream", "RandomIntegerInRange",
                          "RandomIntegerInRangeFromStream", "RandomInteger", "RandomBool",
                          "RandomFloat", "RandomFloatInRange", "Array_Random", "Array_Shuffle",
                          "Array_ShuffleFromStream"))
# 只关心不记录的（表现/日志类），拦下来防止 VM 去执行它们
IGNORE_VERBS = frozenset(("ShowNotification", "AddToBattleLog", "AppendText", "PersistCustomFields",
                          "CustomAbilityAdd", "CustomAbilityRemove",
                          "OnAfterExtraKreditSlotGain",      # 界面通知，没有对局效果
                          # ★ 战役/教程/成就进度与界面文字：与对局胜负无关（用户 2026-10-02 的范围判据）
                          "ShowCampaignMessage", "UpdateCampaignStarStatus", "GiveStarForCampaign",
                          "IncrementObjectiveCounter", "SetObjectiveCounter", "ShowTutorialMessage",
                          "AddNumberToText",
                          # 反制被触发：本身只是把那张反制翻出来的簿记（效果在它自己的钩子体里，
                          # 那些写类动词照常被记录）
                          "GotchaTriggered",
                          # 情报：BP `AddIntelToCard(cardID, instigatorID, amount, &qqq)` 只把
                          # `cipher` clamp(0..9)，自身没有钩子调用。★ **暂时按空实现**（拦下不让
                          # VM 执行）—— 但情报**不是**"对 eval 无意义"：用户 2026-10-02 指出
                          # "情报牌使用时有时可以有作用（日波情报流：使用情报牌时 …）"，
                          # 那些效果在情报相关的 triggers 钩子族里。等钩子侧接好后再改成
                          # 记录 cipher 变化并交给钩子（TODO §3g-3）。
                          "AddIntelToCard"))

# 玩家自己的选择（不是随机）：抉择卡 `WhichChooseOne` 的分支枚举 Card_0/Card_1。
CHOICE_VERBS = frozenset(("WhichChooseOne",))

ALL_HOOKED = (set(COUNT_VERBS) | set(FLAG_VERBS) | set(GIVE_KW) | set(RANDOM_VERBS)
              | set(IGNORE_VERBS) | set(CHOICE_VERBS) | set(JSON_VERBS)
              | set(SIDE_VERBS) | set(END_TURN_VERBS) | set(DECK_VERBS)
              | set(SALVAGE_VERBS) | set(PIN_VERBS) | set(STEAL_VERBS)
              | set(SPAWN_DECK_VERBS) | set(REVEAL_VERBS) | set(DEFENSE_AOE_VERBS)
              | set(TAX_VERBS) | set(RESTRICTION_VERBS)
              | set(INTEL_VERBS)
              | set(DISCARD_VERBS) | set(RECORD_ONLY))

# 加密写入叶子（NATIVE-SPEC-GAPS §5，IDA 0x144B156E0/15770/158B0/15940/159D0）：
#   `setAndEncryptX(newX, key1, key2, FrameCount)` —— 把 `newX`（**明文新值**）写进卡的加密记录。
#   语义与前缀映射：attack/attackBuff/defense/kredit/kreditBuff。
#   我们在 Recorder 里留一份**影子**（不碰游戏内存）：同一次 VM 空跑里，后续的
#   `getAndDecryptX/getTotalX` 必须先看到影子值（卡牌的"贴膜/满血"等绕过上层 BP 动词、
#   直接调叶子的路径就靠它）。
SET_ENC_FIELDS = {"setAndEncryptAttack": "attack", "setAndEncryptAttackBuff": "attackBuff",
                  "setAndEncryptDefense": "defense", "setAndEncryptKredit": "kredit",
                  "setAndEncryptKreditBuff": "kreditBuff"}
_SET_ENC_EFFECT_KEY = {"attack": "set_attack", "attackBuff": "set_attack_buff",
                       "defense": "set_defense", "kredit": "set_kredit",
                       "kreditBuff": "set_kredit_buff"}


COMBAT_KEYWORDS = ("ambush", "blitz", "fury", "guard", "heavyarmor", "shock", "smokescreen")
_INT_RANGE = frozenset(("RandomIntFromRangeWithStream", "RandomIntegerInRange",
                        "RandomIntegerInRangeFromStream"))
ENUM_CAP = 12          # 单个随机点最多枚举多少个结果（更大的域只取前 ENUM_CAP 个）


class Recorder:
    """收集一次 VM 空跑期间被截获的写类调用；随机原语按 `forced` 指定的结果返回。

    `forced`：按遇到随机点的先后给出每个点选第几个结果的下标列表；缺省（None）= 探查模式，
    每个点都选 0 号结果并把它的域大小记进 `nodes`，供 `enumerate_effects` 展开。
    """

    def __init__(self, card_ptr: int = 0, target_ptr: int = 0, forced=None):
        self.card_ptr, self.target_ptr = card_ptr, target_ptr
        self.forced = list(forced) if forced is not None else None
        self.records: list = []          # [{verb, args, tainted}]
        self.tainted = False             # 出现过「域未知的随机」⇒ 其后的记录不确定
        self.chance: list = []           # 机会节点的动词名
        self.choice = False              # 出现过「玩家选项」节点 ⇒ 分支取 max 而非期望
        self.nodes: list = []            # 可枚举的随机点 [{"verb","size"}]
        self.stream = None               # kardsmem.rng.Stream：给了活种子 ⇒ 随机点按游戏的 LCG 给**确定**结果，不枚举
        self.exact = False               # 本次空跑里是否用过确定随机
        self.hq_own = frozenset()        # 己方/敌方总部卡指针：对总部的 ChangeDefense 记成回血/伤害
        self.hq_enemy = frozenset()
        self.json = {}                   # 影子 customJson：{(卡, 变量名): 值}（写进这里、读从这里；不回写游戏）
        # ★ 组 D：加密写入叶子的**影子**：{卡指针: {记录名: 明文新值}}；读侧 `_dec_rec/_stat`
        #   经 view 里的 `shadow_stats` 先看到它（同一次空跑内 写→读 一致）。
        self.shadow_stats = {}
        self.cur_stats = {}              # {卡指针: {"attack"/"defense"/"opcost": 当前值}}（SetValue 型改数值要换成增量）
        self.ptr_ids = {}                # {卡指针: card_id}（view 解析时顺带填，给 to_effects 出键用）
        self.json_loader = None          # 惰性读活内存里该卡的 bool 标记：fn(卡) -> {名: bool}
        self._json_loaded = set()
        self.gaps = []                   # 读不到的状态（如 int 型 customJson 的初值）：[说明]

    # ---- 随机点的取值 ----
    def _pick(self, verb: str, size: int) -> int:
        size = max(1, min(size, ENUM_CAP))
        k = len(self.nodes)
        self.nodes.append({"verb": verb, "size": size})
        if self.forced is None or k >= len(self.forced):
            return 0
        return max(0, min(self.forced[k], size - 1))

    @staticmethod
    def _write_out(frame, e, idx: int, value) -> None:
        """把值写进调用点第 idx 个 kid 指向的局部变量（出参）。"""
        try:
            name = e.kids[idx].args.get("prop")
            if name and frame is not None:
                frame.locals[name] = value
        except Exception:                                         # noqa: BLE001
            pass

    def _json_init(self, card) -> None:
        """第一次碰这张卡的 customJson 时，把活内存里读得到的 bool 标记装进影子。"""
        if card in self._json_loaded:
            return
        self._json_loaded.add(card)
        if self.json_loader is None:
            return
        try:
            for k, v in (self.json_loader(card) or {}).items():
                self.json.setdefault((card, k), v)
        except Exception:                                         # noqa: BLE001
            self.gaps.append("customJson 初值读不出 (card=%r)" % (card,))

    def _json_hook(self, name, frame, args, e):
        """JSON_* 的影子实现（见 JSON_VERBS 的说明）。出参下标：Set*/Add*/Remove*/Clear 的 found 是最后一个；
        Get* 的 (value, found) 是最后两个。"""
        card = args[0] if args else None
        var = args[1] if len(args) > 1 else None
        key = (card, var)
        n = len(e.kids)
        self._json_init(card)
        if name in JSON_GET or name in JSON_ARR_GET:
            found = key in self.json
            default = [] if name in JSON_ARR_GET else (False if name == "JSON_GetBool"
                                                       else (0 if name == "JSON_GetInt" else ""))
            if not found and name != "JSON_GetBool":
                self.gaps.append("customJson 键 %r 的初值未知（int/string 读不出）" % (var,))
            self._write_out(frame, e, n - 2, list(self.json[key]) if found and name in JSON_ARR_GET
                            else (self.json[key] if found else default))
            self._write_out(frame, e, n - 1, found)
            return None
        if name == "JSON_Clear":
            self.json.pop(key, None)
        elif name in JSON_SET:
            self.json[key] = args[2] if len(args) > 2 else None
        elif name in JSON_ARR_SET:
            self.json[key] = list(args[2]) if len(args) > 2 and args[2] is not None else []
        elif name in JSON_ARR_ADD:
            self.json[key] = list(self.json.get(key) or []) + [args[2] if len(args) > 2 else None]
        elif name in JSON_ARR_DEL:
            cur = list(self.json.get(key) or [])
            if len(args) > 2 and args[2] in cur:
                cur.remove(args[2])
            self.json[key] = cur
        self._write_out(frame, e, n - 1, True)
        return None

    def hook(self, name: str):
        def fn(vm, frame, obj, args, e):
            if name in JSON_VERBS:
                return self._json_hook(name, frame, args, e)
            if name in IGNORE_VERBS:
                return None
            if name in CHOICE_VERBS:
                # 「哪个选项」是玩家选的，不是随机：枚举两个选项（Card_0/Card_1），
                # 调用方对各分支取 **max**（我们自己会选好的那个），不是取平均。
                v = self._pick(name, 2)
                self.choice = True
                self._write_out(frame, e, 0, v)
                return v
            st = self.stream
            if st is not None:
                # ★ 确定随机（2026-10-01，`kardsmem/rng.py`）：只接**走 cardsRandomStream** 的几个原语；
                #   其它随机（FMath 全局流等）仍然枚举/污染。
                if name == "RandomIntFromRangeWithStream":
                    lo = args[0] if len(args) > 0 and isinstance(args[0], int) else 0
                    hi = args[1] if len(args) > 1 and isinstance(args[1], int) else lo
                    v = st.wrapper_int(lo, hi)
                    self.exact = True
                    self._write_out(frame, e, 2, v)
                    return v
                if name == "RandomIntegerInRangeFromStream":
                    # Kismet 库版：args = (Stream, Min, Max)；流参数是游戏自己的 cardsRandomStream/encryptionStream，
                    # 只有前者我们知道种子 —— 参数里的 Stream 读不到身份，保守起见仍枚举
                    pass
                if name == "GetRandomCard":
                    arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
                    if arr:
                        v = arr[st.wrapper_int(0, len(arr) - 1)]
                        self.exact = True
                        self._write_out(frame, e, 2, v)
                        return None
            if name in _INT_RANGE:
                lo = args[0] if len(args) > 0 and isinstance(args[0], int) else 0
                hi = args[1] if len(args) > 1 and isinstance(args[1], int) else lo
                v = lo + self._pick(name, hi - lo + 1)
                self.chance.append(name)
                self._write_out(frame, e, 2, v)              # BP 包装 (min,max,&out) 的出参
                return v                                      # Kismet 库版直接返回值
            if name == "GetRandomCard":
                arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
                if arr:
                    v = arr[self._pick(name, len(arr))]
                    self.chance.append(name)
                    self._write_out(frame, e, 2, v)
                    return None
            if name == "GiveRandomCombatKeyword":
                kw = COMBAT_KEYWORDS[self._pick(name, len(COMBAT_KEYWORDS))]
                self.chance.append(name)
                self.records.append({"verb": "Give" + kw.capitalize(), "args": list(args),
                                     "tainted": self.tainted, "random_kw": kw})
                return None
            if name in RANDOM_VERBS:
                self.chance.append(name)                      # 域未知：只记，且污染其后记录
                self.tainted = True
                return None
            if name in SET_ENC_FIELDS:
                # 组 D（§5）：`setAndEncryptX(newX, key1, key2, FrameCount)` —— newX 是明文新值。
                # 记进影子（读侧先看它）+ 照旧进 records（日志/追溯）。不碰游戏内存。
                v = args[0] if args and isinstance(args[0], (int, float)) else None
                if obj and v is not None:
                    self.shadow_stats.setdefault(obj, {})[SET_ENC_FIELDS[name]] = int(v)
                self.records.append({"verb": name, "args": list(args), "tainted": self.tainted})
                return None
            if name == "ConvertCard":
                rec_ = {"verb": name, "args": list(args), "tainted": self.tainted}
                try:
                    rec_["conv"] = _convert_payload(vm, args)
                except Exception as ex:                                  # noqa: BLE001
                    self.gaps.append("ConvertCard：取不到目标卡数值（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
                self.records.append(rec_)
                return None
            if name == "MakeVeteran":
                rec_ = {"verb": name, "args": list(args), "tainted": self.tainted}
                try:
                    rec_["vet"] = _veteran_payload(vm, args[0] if args else 0)
                except Exception as ex:                                  # noqa: BLE001
                    self.gaps.append("MakeVeteran：取不到静态卡数值（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
                self.records.append(rec_)
                return None
            self.records.append({"verb": name, "args": list(args), "tainted": self.tainted})
            return None
        return fn

    def hooks(self) -> dict:
        h = {n: self.hook(n) for n in ALL_HOOKED}
        # 随机战斗关键词落成的动词名（GiveHeavyarmor 没有对应引擎动词 ⇒ 折成重甲 +1）
        return h


def _veteran_payload(vm, card_ptr):
    """`MakeVeteran`（BP_CardFunctions@7127）要用的数值，**在录制时**用活卡视图 + `<名>_vet` 静态卡算出来。

    BP 对 attack/defense/operationCost/heavyArmor 各调一次 `ChangeX(card, id, 静态卡的值, veteranSet(5))`，
    `veteranSet` 与 `SetValue` 同一分支 ⇒ **设为绝对值**（防御同时置 maxDefense），buff 不动；
    然后把 ambush/blitz/fury/mobilize/shock/smokescreen/destruction 直接赋成静态卡的值，guard 同理（除非卡自带
    `guard` 自定义能力）。这里只取数，规则落在 `boardeval._apply_eff`。没有 `_vet` 静态卡 ⇒ BP 门槛不过 ⇒ 返回 None。
    """
    cn = getattr(vm, "cn", None)
    if cn is None or not card_ptr:
        return None
    v = cn._view(card_ptr)                                  # noqa: SLF001
    name = v.get("class_fname") or v.get("name")
    if not name:
        raise Unimplemented("MakeVeteran：读不到卡的类名")
    if not cn.static_card:
        raise Unimplemented("MakeVeteran：没有静态卡表")
    vet = cn.static_card(str(name).lower() + "_vet")
    if vet is None:
        return None
    fl = (vet.get("keyword_flags") or {})
    return {
        "atk_to": vet.get("attack_plain"), "atk_from": v.get("attack"),
        "dfn_to": vet.get("defense_plain"),
        "opc_to": vet.get("operation_cost"), "opc_from": v.get("operation_cost"),
        "armor_to": vet.get("total_heavy_armor"), "armor_buff": v.get("heavy_armor_buff") or 0,
        "kw": {k: bool(fl.get("has_" + k)) for k in ("ambush", "blitz", "fury", "mobilize", "shock",
                                                      "smokescreen", "guard")},
    }


def _convert_payload(vm, args):
    """`ConvertCard(cardIDs[], instigatorID, convertToCardName, convertIntoCardID, skipTrigger, &newCardIDs)`
    （BP_CardFunctions@12611）：旧牌离场（不算摧毁）→ 同位置同序号 `CreateCard(目标名)`。
    录制时用静态卡表取新牌的出厂数值；`convertIntoCardID>0`（按某张在场牌的名字转）读不到名字就不结算。"""
    cn = getattr(vm, "cn", None)
    ids = args[0] if args and isinstance(args[0], (list, tuple)) else None
    to_name = args[2] if len(args) > 2 else None
    into_id = args[3] if len(args) > 3 else 0
    if ids is None or cn is None or not cn.static_card:
        raise Unimplemented("ConvertCard：缺 cardIDs / 静态卡表")
    if isinstance(into_id, int) and into_id > 0:
        raise Unimplemented("ConvertCard：convertIntoCardID>0（按在场牌的名字转）暂未支持")
    if not isinstance(to_name, str) or not to_name:
        raise Unimplemented("ConvertCard：目标卡名读不出（%r）" % (to_name,))
    st = cn.static_card(to_name.lower())
    if st is None:
        raise Unimplemented("ConvertCard：静态卡表里没有 %r" % (to_name,))
    fl = (st.get("keyword_flags") or {})
    return {"ids": [int(x) for x in ids if isinstance(x, (int, float))], "name": to_name,
            "atk": st.get("attack_plain"), "dfn": st.get("defense_plain"),
            "cost": st.get("kredits_plain"), "opc": st.get("operation_cost"),
            "armor": st.get("total_heavy_armor") or 0, "typ": st.get("card_type"),
            "kw": sorted(k for k in ("ambush", "blitz", "fury", "mobilize", "shock", "smokescreen", "guard", "alpine")
                         if fl.get("has_" + k))}


def _who(ptr, rec: "Recorder") -> str:
    if ptr and ptr == rec.target_ptr:
        return "target"
    if ptr and ptr == rec.card_ptr:
        return "self"
    return "other"


def to_effects(rec: Recorder, my_side=None, slots=None, kredits=None) -> dict:
    """录到的调用 → 效果摘要（键的含义见 `boardeval` 的 EFFECT KEYS）。

    确定的写入 `eff`，被随机污染之后的写入 `eff["uncertain"]`，机会节点写入 `eff["chance"]`。
    """
    eff: dict = {}
    unc: dict = {}
    for r in rec.records:
        dst = unc if r["tainted"] else eff
        verb, args = r["verb"], r["args"]

        def arg(i):
            return args[i] if i is not None and i < len(args) else None

        if verb in COUNT_VERBS:
            key, ni, wi, si = COUNT_VERBS[verb]
            n = arg(ni)
            n = n if isinstance(n, (int, float)) else 1
            side = arg(si)
            mine = _is_mine(my_side, side)
            if verb in ("SpawnCardOnBattlefield", "SpawnCardInFrontline"):
                # 生成的是**哪张牌、落在哪一排、谁的**：sim 要按原版 `SpawnCardToBoard` 结算（排满 ⇒ 不生成；用卡自己的面板）。
                #   SpawnCardOnBattlefield(side, _, card_name, …) → 支援线；SpawnCardInFrontline(card_name, side, …) → 前线。
                _nm, _sd = (arg(2), arg(0)) if verb == "SpawnCardOnBattlefield" else (arg(0), arg(1))
                if isinstance(_nm, str) and _nm not in ("", "None"):
                    dst.setdefault("spawn_cards", []).append(
                        {"name": _nm, "row": "back" if verb == "SpawnCardOnBattlefield" else "frontline",
                         "mine": _is_mine(my_side, _sd)})
                if verb == "SpawnCardInFrontline":
                    side = _sd
                    mine = _is_mine(my_side, _sd)
            if key in ("kredit", "draw", "gain_cards", "spawn") and not mine:
                dst["opp_" + key] = dst.get("opp_" + key, 0) + n
            elif key == "slot_set":
                cur = (slots or {}).get("local" if mine else "enemy")
                if cur is not None and isinstance(n, (int, float)):
                    dst["slot" if mine else "opp_slot"] = dst.get("slot" if mine else "opp_slot", 0) + (n - cur)
            elif key == "kredit_set":
                # 绝对值 → 增量（要当前指挥点；不知道就不猜，只留记录）
                cur = (kredits or {}).get("local" if mine else "enemy")
                if cur is not None and isinstance(n, (int, float)):
                    k2 = "kredit" if mine else "opp_kredit"
                    dst[k2] = dst.get(k2, 0) + (n - cur)
            elif key == "slot_loss":
                dst["slot"] = dst.get("slot", 0) + (-1 if mine else 0)
                if not mine:
                    dst["opp_slot"] = dst.get("opp_slot", 0) - 1
            elif key == "defense" and arg(0) and arg(0) in rec.hq_own:
                dst["heal_hq"] = dst.get("heal_hq", 0) + n          # 给己方总部加防御 = 回血
            elif key in ("attack", "defense", "opcost") and _is_set_change(arg(3)) and isinstance(n, (int, float)):
                # 原版 `ChangeAttack/Defense/OperationCost(…, changeType)`：SetValue(2)/Suppress(3)/veteranSet(5)
                # 是**设成 n**，不是 +n（MONSOON RAIN 2 “把所有单位的攻/防/行动费设为 2”）。换成增量 = n - 当前值。
                cur = (rec.cur_stats.get(arg(0)) or {}).get(key) if isinstance(arg(0), int) else None
                if cur is None:
                    rec.gaps.append("%s 为 SetValue 型但读不到目标当前值（ptr=%s）" % (verb, arg(0)))
                else:
                    d = n - cur
                    if key == "opcost":
                        dst["opcost"] = dst.get("opcost", 0) + d
                    else:
                        b = dst.setdefault("buff", [0, 0])
                        b[0 if key == "attack" else 1] += d
                        tcid = rec.ptr_ids.get(arg(0))
                        if tcid is not None:
                            row = dst.setdefault("buff_ids", {}).setdefault(int(tcid), [0, 0])
                            row[0 if key == "attack" else 1] += d
            elif key in ("attack", "defense"):
                b = dst.setdefault("buff", [0, 0])
                b[0 if key == "attack" else 1] += n
                # 逐张记账：群体 buff（"给你所有单位 +3+2"）要知道**每张**牌各加多少；标量 `buff` 只够单目标
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is not None and isinstance(n, (int, float)):
                    bi = dst.setdefault("buff_ids", {})
                    row = bi.setdefault(int(tcid), [0, 0])
                    row[0 if key == "attack" else 1] += n
            elif key == "damage":
                tgt = arg(wi)
                # ★ 2026-10-02：`DamageCard` 的目标可能是**总部卡**（7th SCOTTISH BORDERERS /
                #   "对敌方 HQ 造成 N 伤"那类）。以前一律记成 `damage`（当单位伤害），
                #   于是打总部的效果被算成"打一个敌方单位"。用 hq_own/hq_enemy 指针集区分。
                if tgt and tgt in rec.hq_enemy:
                    dst["damage_hq"] = dst.get("damage_hq", 0) + n
                elif tgt and tgt in rec.hq_own:
                    # §15.5 拆键：打**己方 HQ** ⇒ damage_own_hq（boardeval 里 hq[LOCAL] -= n）；
                    # 打"自己这张牌"（刚部署的牌）⇒ self_damage（该单位此刻不在 sim 里，丢弃）。
                    dst["damage_own_hq"] = dst.get("damage_own_hq", 0) + n
                else:
                    w = _who(tgt, rec)
                    if w == "self":
                        dst["self_damage"] = dst.get("self_damage", 0) + n
                    else:
                        dst["damage"] = max(dst.get("damage", 0), n)
            elif key == "damage_aoe":
                # ★ NATIVE-COVERAGE §15.5：群体伤害保留 id 列表（TArray<int32> 的卡 id，
                #   与 defense_aoe_ids 同口径），boardeval 才能逐张结算。
                ids = arg(wi)
                if isinstance(ids, (list, tuple)):
                    dst["damage_aoe_ids"] = [int(x) for x in ids
                                             if isinstance(x, (int, float))]
                dst[key] = dst.get(key, 0) + (n if key not in ("spawn", "gain_cards")
                                              else (n if n and n > 1 else 1))
            else:
                dst[key] = dst.get(key, 0) + (n if key not in ("spawn", "gain_cards") else (n if n and n > 1 else 1))
        elif verb in FLAG_VERBS:
            key, wi = FLAG_VERBS[verb]
            if key == "heal_unit":
                key = "heal"
            if key == "fight":
                # ★ NATIVE-COVERAGE §15.5：`MakeCardsFight(unitThisSide, unitOppositeSide,
                #   instigatorID)` 的**前两个实参是卡指针**（不是 card_id）⇒ 用 view 顺带填的
                #   `rec.ptr_ids`（与 set_* 出键同一张表）换 id；换不出就**别假装**，
                #   保留布尔并记缺口（boardeval 侧只会记 gap、不结算）。
                ids = []
                for i in (0, 1):
                    p = arg(i)
                    cid = rec.ptr_ids.get(p) if isinstance(p, int) else None
                    if cid is not None:
                        ids.append(int(cid))
                if len(ids) == 2:
                    dst["fight"] = ids
                    continue
                rec.gaps.append("MakeCardsFight：两个卡指针换不出 card_id（ptr_ids 缺），只记布尔")
            if key == "retreat" and isinstance(arg(0), (list, tuple)):
                # MakeCardRetreat(Cards[], …)：对**一组**牌的群体撤退（SEABORNE INVASION：敌方前线所有单位）
                # 数组里的是**具体哪几张**（TROPICAL STORM 2：“撤退一个随机敌方单位”只有 1 张，随机已由活种子定了）——
                # 换成 card_id 记下来，sim 只撤这几张。
                _rids = [rec.ptr_ids.get(p) for p in arg(0) if isinstance(p, int)]
                if _rids and all(x is not None for x in _rids) and len(_rids) == len(arg(0)):
                    dst["retreat_ids"] = [int(x) for x in _rids]
                else:
                    # 不再退回“敌方前线全撤”的兜底（那是编的）：换不出 id 就如实记缺口，sim 不结算
                    rec.gaps.append("MakeCardRetreat：数组里的卡指针换不出 card_id（ptr_ids 缺），撤退未结算")
                continue
            if key in ("destroy_aoe", "pin_aoe", "suppress_aoe", "remove_aoe", "to_deck_aoe") \
                    and isinstance(arg(0), (list, tuple)):
                # ★ NATIVE-COVERAGE §15.5：群体动词保留 id 列表（TArray<int32>），
                #   boardeval 才能逐张 pop/pin/放回牌库；`to_deck` 另记 positionFromTop。
                dst[key + "_ids"] = [int(x) for x in arg(0)
                                     if isinstance(x, (int, float))]
                if key == "to_deck_aoe":
                    pos = arg(2)
                    if isinstance(pos, (int, float)):
                        dst["to_deck_position"] = int(pos)
            dst[key] = True
            if key == "convert" and r.get("conv"):
                dst[key] = dict(r["conv"])
            if key == "veteran" and r.get("vet"):
                dst[key] = dict(r["vet"])               # 带数值：boardeval 按 BP 改写（无数值 ⇒ 只打标记+缺口）
            if key == "end_match":                      # EndMatch(winnerSide, delay)：谁赢
                sd = arg(0)
                if sd in (1, 2):
                    dst[key] = "local" if _is_mine(my_side, sd) else "enemy"
        elif verb in SIDE_VERBS:
            sd = arg(0)
            if sd in (1, 2):
                dst["playing_side"] = "local" if _is_mine(my_side, sd) else "enemy"
        elif verb in END_TURN_VERBS:
            # 强制结束当前回合 ⇒ 行动权交给对方（boardeval 之后不再产生我方动作）
            dst["playing_side"] = "enemy"
        elif verb in DECK_VERBS:
            sd = arg(0)
            if verb == "ShuffleDeckBySide" and sd in (1, 2):
                dst["deck_shuffle"] = "local" if _is_mine(my_side, sd) else "enemy"
                # ★ 2026-10-02：`ShuffleDeckBySide(side, skipSubAction, instigatorID, &qqq)` ——
                #   BP 里 **只有 `skipSubAction=true`** 才 `FetchAllCardsWithEventTrigger(0x16)`
                #   → `OnDeckShuffled`（SABAE REGIMENT 那类"洗牌时"的钩子）；false 只发
                #   NotifyNewDeck。卡牌自己洗大多传 false，`SpawnCardInDeckBySide(shuffle=true)`
                #   传 true ⇒ 记下这个标志给 rule 的 0x16 触发用（以游戏实现为准）。
                sk = arg(1)
                if isinstance(sk, (bool, int)):
                    dst["deck_shuffle_skip"] = bool(sk)
        elif verb in SALVAGE_VERBS:
            # TArray 实参在 VM 里是 list；单元素版本（如果有）也可能直接给一个 id。
            ids = arg(0)
            if isinstance(ids, (list, tuple)):
                got = [int(x) for x in ids if isinstance(x, (int, float)) and int(x) > 0]
            elif isinstance(ids, (int, float)) and int(ids) > 0:
                got = [int(ids)]
            else:
                got = []
            if got:
                dst.setdefault("salvage_ids", []).extend(got)
        elif verb in PIN_VERBS:
            n = arg(2)                                # ChangedPinnedTurns(cardID, instigatorID, turnsToChange)
            if isinstance(n, (int, float)) and n:
                dst["pin_turns"] = int(n)
        elif verb in STEAL_VERBS:
            sd = arg(2)                               # StealCardFromBoardToDeck(cardID, instigatorID, deckSide)
            if sd in (1, 2):
                side = "local" if _is_mine(my_side, sd) else "enemy"
                dst["steal_to_deck"] = side
                dst["deck_shuffle"] = side            # BP 末尾必然 ShuffleDeckBySide
        elif verb in SPAWN_DECK_VERBS:
            # SpawnCardInDeckBySide(side, card_name, spawnerID, n, salvageFaction,
            #                       HideFromOpponent, bottom, shuffle, SkipDrawAnimation, RandomWithoutShuffle, &ids)
            sd, n = arg(0), arg(3)
            if sd in (1, 2) and isinstance(n, (int, float)) and n > 0:
                dst["deck_add"] = int(n)
                dst["deck_add_side"] = "local" if _is_mine(my_side, sd) else "enemy"
                nm = arg(1)
                if isinstance(nm, str) and nm:
                    dst["deck_add_name"] = nm
                dst["deck_add_bottom"] = bool(arg(6))
                dst["deck_add_shuffle"] = bool(arg(7))
                dst["deck_add_wo_shuffle"] = bool(arg(9))
                # ★ 2026-10-02（`+shuffled` 一直不亮的真因之一）：`shuffle=true` 时 BP 内部
                #   走的是 `ShuffleDeckBySide(side, /*skipSubAction=*/true, …)` ⇒ 会
                #   `FetchAllCardsWithEventTrigger(0x16)`。以前这里只记 `deck_add_shuffle`，
                #   而 `rule._deck_shuffled_triggers` 的闸门看的是 `deck_shuffle` +
                #   `deck_shuffle_skip` ⇒ 塞牌并洗这条**永远不触发**（DUG IN 这种卡白打）。
                #   以游戏实现为准：shuffle=true ⇒ 同时记成一次 skip 洗牌。
                if bool(arg(7)):
                    dst["deck_shuffle"] = ("local" if _is_mine(my_side, sd)
                                           else "enemy")
                    dst["deck_shuffle_skip"] = True
        elif verb in REVEAL_VERBS:
            dst["reveal"] = True
        elif verb in DEFENSE_AOE_VERBS:
            # AddDefenseToMultipleCards(receiverIDs[], amount, giverCardID, &qqq)
            ids, amt = arg(0), arg(1)
            if isinstance(ids, (list, tuple)) and isinstance(amt, (int, float)):
                dst["defense_aoe"] = int(amt)
                dst["defense_aoe_ids"] = [int(x) for x in ids if isinstance(x, (int, float))]
        elif verb in TAX_VERBS:
            n = arg(1)                                # AddKreditsTax(card, costToAdd, instigatorID, &qqq)
            if isinstance(n, (int, float)) and n:
                dst["kredits_tax"] = dst.get("kredits_tax", 0) + int(n)
        elif verb in RESTRICTION_VERBS:
            # Add/RemoveGameplayRestriction(side, type, cardID[, removeAll], [&qqq])
            sd, ty, turns = arg(0), arg(1), arg(3)
            if sd in (1, 2) and isinstance(ty, (int, float)):
                side = "local" if _is_mine(my_side, sd) else "enemy"
                entry = {"side": side, "type": int(ty),
                         "turns": int(turns) if isinstance(turns, (int, float)) else 0}
                key = "restriction_add" if verb == "AddGameplayRestriction" else "restriction_remove"
                dst.setdefault(key, []).append(entry)
        elif verb in INTEL_VERBS:
            # SetCardsSeenByCipher(numberOfCardsSeen, instigatorID, side, &qqq)
            n = arg(0)
            if isinstance(n, (int, float)) and n > 0:
                dst["intel_seen"] = int(n)
        elif verb in DISCARD_VERBS:
            if arg(0) is not None:
                dst.setdefault("discard_ids", []).append(arg(0))
        elif verb in GIVE_KW:
            dst.setdefault("give", []).append(GIVE_KW[verb])
        elif verb == "GiveHeavyarmor":
            dst["armor"] = dst.get("armor", 0) + 1
    # ★ 组 D（§5）：加密写入叶子的**绝对值**效果 → `set_attack/set_attack_buff/set_defense/
    #   set_kredit/set_kredit_buff = {card_id: v}`，由 boardeval 消费（clamp 在消费侧做）。
    if rec.shadow_stats:
        for ptr, fields in rec.shadow_stats.items():
            cid = rec.ptr_ids.get(ptr)
            if cid is None:
                rec.gaps.append("setAndEncrypt*：影子写入了未知卡指针 %#x 的记录，无法定位 card_id" % ptr)
                continue
            for fname, val in fields.items():
                key = _SET_ENC_EFFECT_KEY.get(fname)
                if key:
                    dst2 = eff if not rec.tainted else unc
                    dst2.setdefault(key, {})[cid] = int(val)
    if unc:
        eff["uncertain"] = unc
    if rec.chance:
        eff["chance"] = sorted(set(rec.chance))
    return eff


def _is_mine(my_side, side) -> bool:
    """统一的“这一边是不是我方”判据（to_effects / 读钩子共用）：就是和 `my_side` 比，**不做兜底**——
    座位不可能读不出；被评估的静态卡/临时手牌 side=0 时，由调用方用 `field_overrides` 喂真实座位。"""
    return my_side is None or side == my_side


def _is_set_change(change_type) -> bool:
    """`EChangeType`：SetValue=2、Suppress=3、veteranSet=5 是“设成 n”；其余（permBuff=1、tempBuffGive=0…）是加减。"""
    return change_type in (2, 3, 5)


def record_effects(km, card_ptr: int, target_ptr: int = 0, has_target: bool = False,
                   hook: str = "OnPlayedFromHand", my_side=None, forced=None,
                   read_hooks: Optional[dict] = None, slots=None, args: Optional[dict] = None,
                   hq_own=(), hq_enemy=(), timeout_s: Optional[float] = None,
                   rng_seed: Optional[int] = None, rng_stream=None, kredits=None,
                   board=None, my_seat=None, field_overrides: Optional[dict] = None) -> dict:
    """在真进程的内存上把 `card_ptr` 这张牌的 `hook` 空跑一遍。

    `field_overrides`：`{(对象指针, 字段名): 值}`——空跑时这些字段按给定值读（只读覆盖，不写游戏）。用来把
    **静态卡/手牌**当成"此刻已经打出"来跑它的常驻钩子（例：JUNGLE FEVER 守卫 `enterPlayOnTurn==本回合`、`side==进场单位的 side`）。

    `args`：钩子的形参（按名字），例如 `OnAfterAttack` 要 {"defenderCard": 敌方总部指针,
    "wasShockAttack": False, "attackCost": 1}。`hq_own`/`hq_enemy`：总部卡指针，用来把
    「给己方总部 +防御」记成 heal_hq。

    返回 {"eff": 效果摘要, "records": [...], "stopped": None|原因, "complete": bool,
          "chance": [...]}。VM 不可用/出错 ⇒ {"eff": {}, "stopped": "...", "complete": False}。
    """
    out = {"eff": {}, "records": [], "stopped": None, "complete": False, "chance": [],
           "nodes": [], "out": {}, "ran": False, "gaps": []}
    try:
        import os
        import sys
        tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from canplay import hook_targeted, make_get_field, make_view       # noqa: PLC0415
        from kardsmem import kismet
        from kardsmem.cardnatives import CardNatives
        from kardsmem.objects import ObjectArray
        from kardsmem.vm import VM
        uc = ObjectArray(km).class_of(card_ptr)
        fn = None
        # hook=None ⇒ 按「打出/部署」钩子的常见名字依次找第一个存在的
        for h in ((hook,) if hook else ("OnPlayedFromHand", "OnDeployed", "OnDeploy")):
            fn = kismet.find_function(km, uc, h, inherited=False) if uc else None
            if fn:
                break
        if not fn:
            out["stopped"] = "这张牌没有 %s 覆写" % (hook or "打出/部署钩子")
            return out
        rec = Recorder(card_ptr, target_ptr, forced)

        def _json_loader(card, _km=km):
            from kardsmem import cards as _cards                   # noqa: PLC0415
            return (_cards.read_custom_json_raw(_km, card) or {}).get("entries") or {}
        rec.json_loader = _json_loader
        # ★ PLAN 阶段 5：同一个 Action 里"自己的钩子 + 别人挂的触发钩子"必须**共用同一条流**
        #   （游戏里就是顺序消耗同一条 cardsRandomStream）。传 `rng_stream` 进来即复用，
        #   抽取次数会累加在同一个对象上。
        if rng_stream is not None:
            rec.stream = rng_stream
        elif rng_seed is not None:
            from kardsmem.rng import Stream
            rec.stream = Stream(rng_seed)
        rec.hq_own, rec.hq_enemy = frozenset(x for x in hq_own if x), frozenset(x for x in hq_enemy if x)
        # 快照里的卡：预填 指针→card_id（群体效果遍历到的牌不一定被 view 读过，`buff_ids` 之类要靠它出键）
        for _c in (getattr(board, "cards", None) or ()):
            _p = (getattr(_c, "raw", None) or {}).get("ptr")
            if _p and getattr(_c, "card_id", None) is not None:
                rec.ptr_ids.setdefault(_p, _c.card_id)
                _st = {}
                for _k, _a in (("attack", "total_attack"), ("defense", "total_defense"), ("opcost", "total_operation_cost")):
                    _v = getattr(_c, _a, None)
                    if _v is None and _k == "attack":
                        _v = getattr(_c, "attack", None)
                    if _v is None and _k == "defense":
                        _v = getattr(_c, "defense", None)
                    if isinstance(_v, (int, float)):
                        _st[_k] = _v
                rec.cur_stats[_p] = _st
        hooks = rec.hooks()
        hooks["GetTargetedCard"] = hook_targeted(bool(has_target), target_ptr)
        for nm_, fn_ in (read_hooks or {}).items():          # 调用方给的只读原语（如读指挥点槽）
            hooks[nm_] = fn_
        _base_view = make_view(km)

        def _shadow_view(ptr, _rec=rec, _base=_base_view):
            """view 包装：填 `ptr_ids`（给 set_* 效果出 card_id）+ 注入 `shadow_stats`
            （同一次空跑里 setAndEncrypt* 写过的字段，读侧要先看到）。"""
            v = _base(ptr)
            if isinstance(v, dict):
                cid = v.get("card_id")
                if cid is not None:
                    _rec.ptr_ids[ptr] = cid
                sh = _rec.shadow_stats.get(ptr)
                if sh:
                    v = dict(v)
                    v["shadow_stats"] = dict(sh)
            return v

        cn = CardNatives(None, view=_shadow_view, ks=km)
        # ★ 2026-10-02（§8-9 标记排查）：盘面级原语需要 `BoardState` + `my_seat`
        #   （`IsSideActive` / `getKreditBySide` 没有就抛 Unimplemented ⇒ 整条钩子链断）。
        #   实测：`CRUISER SCOUTS` 的链曾停在 `IsSideActive`（TARNOW 类卡同理）——
        #   调用方（rule/triggers）把快照传进来即可。缺省 None = 保持旧行为（如实抛）。
        if board is not None:
            cn.board = board
        if my_seat is not None:
            cn.my_seat = my_seat
        # ★ §8-5 / NATIVE-COVERAGE §15.6：注入静态卡表提供者（`GetStatic*` / `getHasVeteranUpgrade`
        #   的地基）。读不到表时提供者是空的 ⇒ `_static()` 会如实抛 Unimplemented（缺口），
        #   不会退回"默认卡字段"。
        try:
            from kardsmem.gs import make_static_card_provider
            prov = make_static_card_provider(km)
            cn.static_card = prov
            cn.static_cards = getattr(prov, "names", None) or frozenset()
        except Exception as _e:                               # noqa: BLE001
            rec.gaps.append("静态卡表提供者构建失败：%s: %s" % (type(_e).__name__, _e))
        _gf = make_get_field(km)
        if field_overrides:
            _ov = dict(field_overrides)

            def _gf_ov(obj, name, _base=_gf, _ov=_ov):
                if (obj, name) in _ov:
                    return _ov[(obj, name)]
                return _base(obj, name)
            _gf = _gf_ov
        vm = VM(km, get_field=_gf, card_natives=cn, hooks=hooks)
        if timeout_s:
            import time as _t
            vm.deadline = _t.time() + float(timeout_s)     # 超时如实停下，不无限拖延一步决策
        if has_target and target_ptr and not args and (hook in (None, "OnPlayedFromHand")):
            # 带目标打出：事件形参 `targetCard` 就是被点的目标（BP 里 `cardFunction->GetAdjacentCards(Event_targetCard…)`
            # 这类直接读它，不只是 `GetTargetedCard()`）。
            args = {"targetCard": target_ptr}
        r = vm.run(fn, self_obj=card_ptr, args=args)
        out["stopped"] = r.get("stopped")
        # ★ 钩子的出参（`stopAttack`/`AttackedAndStopped`/`newDefender` 这类会改流程的值）：
        #   以前被丢掉，攻击链（semantics/triggers.py::run_attack_hooks）要靠它决定"被吞/换目标"。
        out["out"] = dict(r.get("out") or {})
        out["ran"] = True
        out["gaps"] = list(rec.gaps)
        out["records"] = rec.records
        out["chance"] = list(rec.chance)
        out["nodes"] = list(rec.nodes)
        out["choice"] = rec.choice
        out["exact"] = rec.exact
        out["draws"] = rec.stream.draws if rec.stream is not None else 0
        _kr = kredits if kredits is not None else getattr((read_hooks or {}).get("getKreditBySide"), "cur_kredits", None)
        out["eff"] = to_effects(rec, my_side, slots, _kr)
        out["complete"] = out["stopped"] is None
    except Exception as ex:                                       # noqa: BLE001
        out["stopped"] = "%s: %s" % (type(ex).__name__, ex)
    return out


def _combos(sizes: list, cap: int) -> list:
    """所有结果组合的下标列表；组合数超过 cap 时只取前 cap 个（确定性截断）。"""
    out = [[]]
    for n in sizes:
        out = [c + [i] for c in out for i in range(n)]
        if len(out) > cap * 4:
            out = out[:cap * 4]
    return out[:cap]


def _generic_out(frame, e, value) -> None:
    """把值写进调用点里**最后一个**局部变量 kid（Kismet 把出参压在调用点末尾）。"""
    try:
        for k in reversed(e.kids):
            if k.op in ("LocalVariable", "LocalOutVariable") and k.args.get("prop"):
                frame.locals[k.args["prop"]] = value
                return
    except Exception:                                             # noqa: BLE001
        pass


def make_read_hooks(st, my_side=None) -> dict:
    """当前局面 → 只读原语的钩子（指挥点槽/指挥点/回合数）。这些是游戏里的原生函数、
    没有字节码，VM 读不了，就由调用方按快照喂。返回值和出参两种用法都兼容。"""
    def side_of(args):
        v = args[0] if args else None
        # 被评估的往往是**静态卡对象**（side=0，不属于任何一方；真游戏里这张牌此刻在我手里）⇒ 读不出座位就按我方算
        # （与 `_opposite` 同一约定）。否则"给你所有单位 +3+2"一类会去遍历**敌方**场面。
        return "local" if _is_mine(my_side, v) else "enemy"

    def hk(table_name, getter):
        def fn(vm, frame, obj, args, e):
            val = getter(side_of(args))
            _generic_out(frame, e, val)
            return val
        return fn
    slots = getattr(st, "slots", None) or {}
    kred = getattr(st, "kredits", None) or {}
    # 场上卡查询：`CardFunctionsStub.GetAllUnitsOnBoard` / `GetCardsOnBoardBySide` 是**原生函数**，
    # VM 没有它们的字节码 ⇒ 以前一跑到这里就 "Unimplemented"（热浪2/骄阳3 卡死在这），
    # 效果枚举不出来，只能退化成常量。这里按快照喂。实测签名（1.60 导出）：
    #   GetAllUnitsOnBoard(bool includeCovertCards, TArray<UBaseCardObject*>& cards)
    #   GetCardsOnBoardBySide(ESideEnum side, bool unitsOnly, bool includeCovertCards, TArray<...>& cards)
    _unit_types = ("infantry", "tank", "artillery", "fighter", "bomber",
                   "antiair", "antitank", "tankdestroyer")

    def board_cards(side=None, units_only=False, include_covert=True, any_defense=False):
        res = []
        for c in (getattr(st, "cards", None) or []):
            # ★ 2026-10-02 IDA 普查（Claude，`BOARD-QUERY-NATIVES-1.60.md`）：真实的
            #   `GetCardsOnBoardBySide` / `GetAllUnitsOnBoard` 语义 =
            #   **当前防御>0 ∧ 在场(Location 5..7，HQ 也算) ∧ side** ∧ covert 过滤
            #   ∧ (unitsOnly ⇒ IsUnit 类型 3..10)；**不含手牌/牌库**；
            #   顺序 = `AllCardsInBattle` 稀疏数组下标升序 = 卡牌创建顺序（我们按快照顺序喂 ✓）。
            if getattr(c, "location", None) not in ("frontline", "back", "hq"):
                continue
            if side is not None and getattr(c, "side", None) != side:
                continue
            if not any_defense and (getattr(c, "defense", 0) or 0) <= 0:   # 防御≤0 的不算"在场"
                continue
            if units_only and getattr(c, "card_type", None) not in _unit_types:
                continue
            # ★ covert 判据（IDA 定案）：`IsUnrevealedCovertCard = hasCovert@0x1EA && !isRevealed@0x33B`
            #   —— 不看 side，敌我对称。以前我用 keywords 里有没有 covert **单独**判定，不可靠
            #   （隐蔽可被揭示）；现在两个条件都要满足。
            if not include_covert:
                kws = getattr(c, "keywords", None) or ()
                if isinstance(kws, dict):
                    kws = list(kws)
                if (any(str(k).lower().endswith("covert") for k in kws)
                        and getattr(c, "is_revealed", True) is False):
                    continue
            p = (getattr(c, "raw", None) or {}).get("ptr")
            if p:
                res.append(p)
        return res

    def _row_of(c):
        """原版的"位置"枚举：前线 = Board_Frontline；总部卡和支援线单位**同属** Board_HQLeft/Right（总部只是行里的一张牌，
        `locationNumber` 随左边插入的单位后移）。快照把总部叫 `hq`、单位叫 `back`，邻接要把它们当同一排。"""
        loc = getattr(c, "location", None)
        return "support" if loc in ("hq", "back") else loc

    def adjacent_cards(ptr, include_covert):
        me = next((c for c in (getattr(st, "cards", None) or ())
                   if (getattr(c, "raw", None) or {}).get("ptr") == ptr), None)
        if me is None or _row_of(me) not in ("frontline", "support"):
            return []
        n = getattr(me, "slot", None)
        if not isinstance(n, int):
            return []
        res = []
        for want in (n - 1, n + 1):
            for c in (getattr(st, "cards", None) or ()):
                if _row_of(c) != _row_of(me) or getattr(c, "slot", None) != want:
                    continue
                if _row_of(me) == "support" and getattr(c, "side", None) != me.side:
                    continue
                if not include_covert:
                    kws = getattr(c, "keywords", None) or ()
                    if isinstance(kws, dict):
                        kws = list(kws)
                    if any(str(k).lower().endswith("covert") for k in kws) and getattr(c, "is_revealed", True) is False:
                        continue
                p = (getattr(c, "raw", None) or {}).get("ptr")
                if p:
                    res.append(p)
                break
        return res

    def hk_cards(getter):
        def fn(vm, frame, obj, args, e):
            val = getter(args)
            _generic_out(frame, e, val)
            return val
        return fn

    # `GetOppositeSide(card)`：字节码常拿它推"敌方"（例：rain2_deluge2 = 全体敌方 -1 攻）。
    # 我们评估的是**静态卡对象**（`side_enum=0`，不属于任何一方）⇒ cardnatives 会以
    # "认不出的 side=0"停下。真游戏里这张牌此刻在我手里（我方座位），所以兜底：
    # 先按快照查这张卡的座位；查不到（静态卡）就按**我方座位**算。
    my_seat = getattr(st, "my_side_raw", None) or my_side

    def seat_for(ptr):
        for c in (getattr(st, "cards", None) or []):
            if (getattr(c, "raw", None) or {}).get("ptr") == ptr:
                return (getattr(c, "raw", None) or {}).get("side_enum")
        return None

    def hk_seat(fn):
        def hook(vm, frame, obj, args, e):
            val = fn(args)
            _generic_out(frame, e, val)
            return val
        return hook

    def _opposite(args):
        p = args[0] if args else 0
        s = seat_for(p) if p else None
        if s not in (1, 2):
            s = my_seat
        return None if s not in (1, 2) else (2 if s == 1 else 1)

    def hand_cards(side):
        return [(getattr(c, "raw", None) or {}).get("ptr")
                for c in (getattr(st, "cards", None) or [])
                if getattr(c, "location", None) == "hand" and getattr(c, "side", None) == side
                and (getattr(c, "raw", None) or {}).get("ptr")]

    def _max_kred(_args):
        """`getMaxPossibleKredits` 的真值：快照的 `max_possible_kredits`（GS+0x338）。
        读不到就抛（旧实现拿槽数顶替 —— 与 IDA 不符，2026-10-02 用户确认）。"""
        v = getattr(st, "max_possible_kredits", None)
        if not isinstance(v, int):
            raise Unimplemented("max_possible_kredits 快照没读到（GameState+0x338）")
        return int(v)

    out = {
        "getKreditSlotBySide": hk("slot", lambda sd: int(slots.get(sd) or 0)),
        # ★ 2026-10-02（IDA 0x144B3C010 / 用户）：这两个都读 **GameState+0x338** 的全局上限，
        #   **不是槽数**（旧映射 `hk("slot", …)` 是错的）。快照没读到就抛，不猜。
        "GetMaxKreditsBySide": hk_seat(_max_kred),
        "getMaxPossibleKredits": hk_seat(_max_kred),
        "GetKreditsBySide": hk("kred", lambda sd: int(kred.get(sd) or 0)),
        "getKreditBySide": hk("kred", lambda sd: int(kred.get(sd) or 0)),
        "GetAllUnitsOnBoard": hk_cards(
            lambda a: board_cards(None, True, bool(a[0]) if a else True)),
        # `GetAllCardsOnBoard(includeCovert, &cards)`（BP_CardFunctions@457）：只要 IsLocatedOnBoard + covert
        # 过滤，**无防御/side/IsUnit 判据**（card_event_echelon 靠它给全场单位挂 trigger）。
        "GetAllCardsOnBoard": hk_cards(
            lambda a: board_cards(None, False, bool(a[0]) if a else True, any_defense=True)),
        "GetCardsOnBoardBySide": hk_cards(
            lambda a: board_cards(side_of(a), bool(a[1]) if len(a) > 1 else False,
                                  bool(a[2]) if len(a) > 2 else True)),
        # `BP_CardFunctions::GetAdjacentCards(card, includeCovert, &adjacent)`（BP@577，2026-10-03 读过字节码）：
        # `FetchCardsByLocation(card->location)` 里 locationNumber == 自己 ±1 的牌（先 -1 后 +1，各一张），
        # 隐蔽未揭示的牌除非 includeCovert 否则不算。后排每方各一排、前线双方共用 ⇒ 后排要同 side。
        "GetAdjacentCards": hk_cards(lambda a: adjacent_cards(a[0] if a else 0, bool(a[1]) if len(a) > 1 else False)),
        "GetOppositeSide": hk_seat(_opposite),
        # 手牌查询（同上是原生）：`storm2_thunderstorm2`（雷暴2）这类效果要用它
        "GetCardsInHandBySide": hk_cards(lambda a: hand_cards(side_of(a))),
        "GetCardsInHandBySideOrdered": hk_cards(lambda a: hand_cards(side_of(a))),
    }
    out["getKreditBySide"].cur_kredits = kred     # setKreditBySide（绝对值）要靠它换算增量
    turn = getattr(st, "turn", None)
    if turn is not None:
        out["GetTurnNumber"] = lambda vm, frame, obj, args, e: (_generic_out(frame, e, int(turn)) or int(turn))
    # ★ 2026-10-02（A8 核实）：`UKismetNodeHelperLibrary::GetEnumeratorUserFriendlyName(UEnum*, int32)`
    #   以前只有 `semantics/legality.py` 的 VM 提供，**effectvm 的求值链没有** ⇒ 卡（如 KYOTO REGIMENT）
    #   一旦问枚举名就整条停。这里补上：只认 `ETypeEnum`（表和 legality.ETYPE 同源，
    #   `KardsCore_structs.hpp:82`），别的枚举如实抛 Unimplemented（不猜）。
    _ETYPE = ("NotAvailable", "location", "order", "tank", "fighter", "bomber", "infantry",
              "artillery", "antiair", "antitank", "tankdestroyer", "gotcha", "wildcard")

    def _enum_user_friendly(vm, frame, obj, args, e):
        from kardsmem.kismetlib import Unimplemented
        nm = args[0] if args else None
        val = args[1] if len(args) > 1 else None
        if isinstance(nm, int) and nm:
            try:
                from kardsmem.objects import ObjectArray
                nm = ObjectArray(vm.s).pool().fname_of(nm)
            except Exception:                                     # noqa: BLE001
                nm = None
        if nm != "ETypeEnum":
            raise Unimplemented("GetEnumeratorUserFriendlyName：只认得 ETypeEnum，这个是 %r" % nm)
        try:
            return _ETYPE[int(val)]
        except (TypeError, ValueError, IndexError):
            raise Unimplemented("ETypeEnum 取值 %r 超出 0..%d" % (val, len(_ETYPE) - 1))

    out["GetEnumeratorUserFriendlyName"] = _enum_user_friendly
    return out


def enumerate_effects(km, card_ptr: int, target_ptr: int = 0, has_target: bool = False,
                      hook: str = "OnPlayedFromHand", my_side=None, cap: int = 8,
                      runner=None, read_hooks: Optional[dict] = None, slots=None,
                      args: Optional[dict] = None, hq_own=(), hq_enemy=(),
                      budget_s: Optional[float] = None, rng_seed: Optional[int] = None,
                      board=None, my_seat=None, field_overrides: Optional[dict] = None) -> dict:
    """把随机结果**列出来**：先探查有几个可枚举的随机点，再对每种结果组合各空跑一次。

    返回 {"outcomes": [(权重, 效果摘要), ...], "complete": bool, "stopped": ..., "runs": n}。
    没有随机点 ⇒ 只有一个权重 1.0 的分支。各分支等概率（游戏里这些随机都是均匀的整数范围/
    数组随机元素/随机关键词）。`runner` 供测试注入（签名同 `record_effects`）。
    """
    run = runner or record_effects
    kw = {}
    if read_hooks:
        kw["read_hooks"] = read_hooks
    if slots:
        kw["slots"] = slots
    import time as _t
    t_end = (_t.time() + budget_s) if budget_s else None
    if budget_s:
        kw["timeout_s"] = budget_s
    if args:
        kw["args"] = args
    if hq_own or hq_enemy:
        kw["hq_own"], kw["hq_enemy"] = hq_own, hq_enemy
    if rng_seed is not None:
        kw["rng_seed"] = rng_seed                           # 活种子：能确定的随机点直接给出确定结果
    if board is not None:
        kw["board"] = board                                 # 盘面级原语（IsSideActive 等）要用
    if my_seat is not None:
        kw["my_seat"] = my_seat
    if field_overrides:
        kw["field_overrides"] = field_overrides
    first = run(km, card_ptr, target_ptr, has_target, hook, my_side, None, **kw)
    res = {"outcomes": [], "complete": first.get("complete", False),
           "stopped": first.get("stopped"), "runs": 1, "choice": bool(first.get("choice")),
           "exact": bool(first.get("exact")), "draws": first.get("draws", 0)}
    nodes = first.get("nodes") or []
    if not nodes:
        res["outcomes"] = [(1.0, first.get("eff") or {})]
        return res
    combos = _combos([n["size"] for n in nodes], cap)
    outs = []
    for c in combos:
        if t_end is not None and _t.time() > t_end and outs:
            res["complete"] = False                          # 预算用完：只用已跑出的分支，不再展开
            res["stopped"] = res.get("stopped") or "枚举超时（预算已用完）"
            break
        if t_end is not None:
            kw["timeout_s"] = max(0.1, t_end - _t.time())
        r = first if all(i == 0 for i in c) else run(km, card_ptr, target_ptr, has_target,
                                                     hook, my_side, c, **kw)
        res["runs"] += 0 if r is first else 1
        outs.append(r.get("eff") or {})
        res["complete"] = res["complete"] and r.get("complete", False)
    w = 1.0 / len(outs)
    res["outcomes"] = [(w, e) for e in outs]
    return res


# ---------------------------------------------------------------------------
# 离线自检（不碰游戏）：钩子记录/污染/摘要
# ---------------------------------------------------------------------------
def selftest() -> int:
    fails = 0

    def chk(name, ok, extra=""):
        nonlocal fails
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        fails += 0 if ok else 1

    def call(rec, verb, *args):
        return rec.hook(verb)(None, None, None, list(args), None)

    TARGET, SELF = 0x1000, 0x2000

    r = Recorder(SELF, TARGET)
    call(r, "DamageCard", TARGET, 2, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("伤害：数量取自已求值实参", e.get("damage") == 2, str(e))

    r = Recorder(SELF, TARGET)
    r.hq_enemy = frozenset([0x3000])
    call(r, "DamageCard", 0x3000, 3, SELF, False, False, False, None)
    e = to_effects(r, my_side=1)
    chk("DamageCard(敌方 HQ 卡) ⇒ damage_hq（不再算成打单位）",
        e.get("damage_hq") == 3 and "damage" not in e, str(e))

    r = Recorder(SELF, TARGET)
    call(r, "SetCardsSeenByCipher", 3, SELF, 1, None)
    chk("情报触发源：SetCardsSeenByCipher(3) ⇒ intel_seen=3",
        to_effects(r, my_side=1).get("intel_seen") == 3)

    r = Recorder(SELF, TARGET)
    call(r, "GiveKreditsBySide", 1, 1, SELF, None)
    call(r, "GiveKreditsBySide", 2, 1, SELF, None)
    e = to_effects(r, my_side=1)
    chk("生产：给己方 +1，给对方的另记", e.get("kredit") == 1 and e.get("opp_kredit") == 1, str(e))

    r = Recorder(SELF, TARGET)
    call(r, "DrawCardsFromDeckBySide", SELF, 1, 1, True, False, None, 0.0)
    chk("计划：抽 1", to_effects(r, 1).get("draw") == 1)

    r = Recorder(SELF, TARGET)
    call(r, "LoseKreditSlot", 1)
    chk("丢槽：我方 -1 槽", to_effects(r, 1).get("slot") == -1)

    r = Recorder(SELF, TARGET)
    call(r, "ForceEndTurn")
    chk("强制结束回合（ForceEndTurn，久留米联队那类）⇒ playing_side=enemy",
        to_effects(r, 1).get("playing_side") == "enemy")

    r = Recorder(SELF, TARGET)
    call(r, "ShuffleDeckBySide", 1, False, SELF, None)
    chk("洗牌：side=1（我）⇒ deck_shuffle=local",
        to_effects(r, 1).get("deck_shuffle") == "local")
    r2 = Recorder(SELF, TARGET)
    call(r2, "ShuffleDeckBySide", 2, False, SELF, None)
    chk("洗牌：side=2（对方）⇒ deck_shuffle=enemy",
        to_effects(r2, 1).get("deck_shuffle") == "enemy")

    r = Recorder(SELF, TARGET)
    call(r, "SalvageMultipleUnits", [5, 6], SELF, False)
    chk("收缴：SalvageMultipleUnits([5,6]) ⇒ salvage_ids=[5,6]",
        to_effects(r, 1).get("salvage_ids") == [5, 6])

    r = Recorder(SELF, TARGET)
    call(r, "GiveMobilize", TARGET, SELF, None)
    chk("动员：GiveMobilize ⇒ give 含 mobilize",
        "mobilize" in (to_effects(r, 1).get("give") or []))

    r = Recorder(SELF, TARGET)
    call(r, "ChangedPinnedTurns", TARGET, SELF, 2, None)
    chk("压制回合数：ChangedPinnedTurns(+2) ⇒ pin_turns=2",
        to_effects(r, 1).get("pin_turns") == 2)

    r = Recorder(SELF, TARGET)
    call(r, "StealCardFromBoardToDeck", TARGET, SELF, 1, None)
    chk("洗入牌库：StealCardFromBoardToDeck(deckSide=1) ⇒ steal_to_deck=local + deck_shuffle=local",
        to_effects(r, 1).get("steal_to_deck") == "local"
        and to_effects(r, 1).get("deck_shuffle") == "local")

    r = Recorder(SELF, TARGET)
    call(r, "SpawnCardInDeckBySide", 1, "NEWCARD", SELF, 2, 0, False, False, True, False, False, None)
    e = to_effects(r, 1)
    chk("塞牌：SpawnCardInDeckBySide(2 张, shuffle) ⇒ deck_add=2 + local + shuffle",
        e.get("deck_add") == 2 and e.get("deck_add_side") == "local"
        and e.get("deck_add_shuffle") is True and e.get("deck_add_name") == "NEWCARD",
        str({k: v for k, v in e.items() if k.startswith("deck_add")}))
    # ★ 2026-10-02：`shuffle=true` 等同于一次 `ShuffleDeckBySide(skipSubAction=true)`
    #   ⇒ 0x16 触发族（`+shuffled`）的闸门必须能看见（以前只记 deck_add_shuffle）。
    chk("塞牌并洗(shuffle=true) ⇒ 同时给 deck_shuffle + deck_shuffle_skip",
        e.get("deck_shuffle") == "local" and e.get("deck_shuffle_skip") is True,
        "shuffle=%r skip=%r" % (e.get("deck_shuffle"), e.get("deck_shuffle_skip")))

    r = Recorder(SELF, TARGET)
    call(r, "SpawnCardInDeckBySide", 1, "NEWCARD", SELF, 2, 0, False, False, False, False, False,
         None)
    e2 = to_effects(r, 1)
    chk("塞牌不洗(shuffle=false) ⇒ 不给 deck_shuffle_skip",
        e2.get("deck_shuffle_skip") is not True,
        "skip=%r" % (e2.get("deck_shuffle_skip"),))

    r = Recorder(SELF, TARGET)
    call(r, "MoveUnitFromSupportToFrontLine", TARGET, SELF, None)
    chk("推上前线：MoveUnitFromSupportToFrontLine ⇒ move_front",
        to_effects(r, 1).get("move_front") is True)

    r = Recorder(SELF, TARGET)
    call(r, "RevealCard", TARGET, SELF, None)
    chk("揭示：RevealCard ⇒ reveal=True", to_effects(r, 1).get("reveal") is True)

    r = Recorder(SELF, TARGET)
    call(r, "AddDefenseToMultipleCards", [5, 6], 2, SELF, None)
    e = to_effects(r, 1)
    chk("群体加防：AddDefenseToMultipleCards([5,6], +2) ⇒ defense_aoe=2 + ids",
        e.get("defense_aoe") == 2 and e.get("defense_aoe_ids") == [5, 6], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "AddKreditsTax", TARGET, 3, SELF, None)
    chk("指向税：AddKreditsTax(+3) ⇒ kredits_tax=3", to_effects(r, 1).get("kredits_tax") == 3)

    r = Recorder(SELF, TARGET)
    call(r, "AddGameplayRestriction", 1, 2, 0, 3)
    e = to_effects(r, 1)
    chk("对局限制：AddGameplayRestriction(side=1, type=2=不能打指令, 3 回合) ⇒ restriction_add",
        e.get("restriction_add") == [{"side": "local", "type": 2, "turns": 3}], str(e))
    r = Recorder(SELF, TARGET)
    call(r, "RemoveGameplayRestriction", 2, 3, 0, False, None)
    e = to_effects(r, 1)
    chk("对局限制：RemoveGameplayRestriction(side=2, type=3) ⇒ restriction_remove（enemy）",
        e.get("restriction_remove") == [{"side": "enemy", "type": 3, "turns": 0}], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "GiveBlitz", TARGET, SELF)
    call(r, "GiveFury", TARGET, SELF)
    chk("贴关键词：记录给了哪些", to_effects(r, 1).get("give") == ["blitz", "fury"])

    r = Recorder(SELF, TARGET)
    call(r, "PinUnit", TARGET, SELF)
    call(r, "RemovePin", 0x3000, None)
    e = to_effects(r, 1)
    chk("压制/解除压制", e.get("pin") is True and e.get("unpin") is True)

    # ---- 随机：枚举而不是预测 ----
    class _E:                                    # 假的调用点表达式：kids[i].args["prop"] = 出参变量名
        def __init__(self, props):
            self.kids = [type("K", (), {"args": {"prop": p}})() for p in props]

    class _F:
        locals: dict = {}

    r = Recorder(SELF, TARGET)
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    chk("随机整数：探查模式选 0 号，记域大小 3", v == 0 and r.nodes == [{"verb": "RandomIntFromRangeWithStream", "size": 3}]
        and fr.locals.get("out") == 0)
    r = Recorder(SELF, TARGET, forced=[2])
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    chk("随机整数：按指定结果返回并写出参", v == 2 and fr.locals.get("out") == 2)

    # ---- 确定随机（给了活种子）----
    from kardsmem.rng import Stream as _S
    r = Recorder(SELF, TARGET)
    r.stream = _S(4242)
    fr = _F(); fr.locals = {}
    v = r.hook("RandomIntFromRangeWithStream")(None, fr, None, [0, 2, None], _E(["a", "b", "out"]))
    ref = _S(4242).wrapper_int(0, 2)
    chk("确定随机：包装原语按种子给出确定结果、不记枚举点", v == ref and r.nodes == [] and r.exact
        and fr.locals.get("out") == ref and r.stream.draws >= 3)
    r = Recorder(SELF, TARGET)
    r.stream = _S(77)
    fr = _F(); fr.locals = {}
    r.hook("GetRandomCard")(None, fr, None, [[11, 22, 33, 44], False, None], _E(["c", "s", "out"]))
    chk("确定随机：GetRandomCard 取 cards[wrapper(0,n-1)]",
        fr.locals.get("out") == [11, 22, 33, 44][_S(77).wrapper_int(0, 3)] and r.nodes == [])

    r = Recorder(SELF, TARGET, forced=[3])
    fr = _F(); fr.locals = {}
    r.hook("GetRandomCard")(None, fr, None, [[11, 22, 33, 44], False, None], _E(["c", "s", "out"]))
    chk("随机选牌：从数组里按指定下标取", fr.locals.get("out") == 44)

    r = Recorder(SELF, TARGET, forced=[1])
    r.hook("GiveRandomCombatKeyword")(None, None, None, [TARGET, SELF, None, None], _E([]))
    e = to_effects(r, 1)
    chk("随机关键词：落成指定的关键词", e.get("give") == ["blitz"], str(e))

    r = Recorder(SELF, TARGET)
    call(r, "DamageCard", TARGET, 1, SELF)
    call(r, "DiscardRandomCardFromHand", 1, SELF, None)          # 域未知的随机
    call(r, "DrawCardsFromDeckBySide", SELF, 1, 1, True, False, None, 0.0)
    e = to_effects(r, 1)
    chk("域未知的随机：记机会节点，其后的效果归入 uncertain",
        e.get("chance") == ["DiscardRandomCardFromHand"] and "draw" not in e
        and e.get("uncertain", {}).get("draw") == 1 and e.get("damage") == 1, str(e))

    # 枚举：CONVOY ATTACK 型「对目标造成 0-2 点」→ 3 个分支，期望伤害 1
    def fake_run(km, card, tgt, has_t, hook, side, forced, read_hooks=None, slots=None):
        rr = Recorder(card, tgt, forced)
        fr2 = _F(); fr2.locals = {}
        v2 = rr.hook("RandomIntFromRangeWithStream")(None, fr2, None, [0, 2, None], _E(["a", "b", "out"]))
        rr.hook("DamageCard")(None, fr2, None, [tgt, v2, card], _E([]))
        return {"eff": to_effects(rr, side), "nodes": list(rr.nodes), "complete": True, "stopped": None}
    en = enumerate_effects(None, SELF, TARGET, True, runner=fake_run)
    dmg = [e2.get("damage", 0) for _, e2 in en["outcomes"]]
    chk("枚举：0-2 点伤害 ⇒ 3 个等概率分支", sorted(dmg) == [0, 1, 2] and abs(sum(w for w, _ in en["outcomes"]) - 1) < 1e-9,
        str(en))
    chk("枚举：期望伤害 = 1", abs(sum(w * e2.get("damage", 0) for w, e2 in en["outcomes"]) - 1.0) < 1e-9)

    def fake_run_none(km, card, tgt, has_t, hook, side, forced, read_hooks=None, slots=None):
        rr = Recorder(card, tgt, forced)
        rr.hook("GiveKreditsBySide")(None, None, None, [1, 1, card, None], _E([]))
        return {"eff": to_effects(rr, side), "nodes": [], "complete": True, "stopped": None}
    en0 = enumerate_effects(None, SELF, 0, False, my_side=1, runner=fake_run_none)
    chk("枚举：没有随机点 ⇒ 单分支权重 1", len(en0["outcomes"]) == 1 and en0["outcomes"][0][0] == 1.0
        and en0["outcomes"][0][1].get("kredit") == 1)
    chk("组合展开：两个随机点 3×2 = 6 个分支", len(_combos([3, 2], 8)) == 6 and len(_combos([12, 12], 8)) == 8)

    r = Recorder(SELF, TARGET)
    call(r, "ShowNotification", "x")
    call(r, "AddToBattleLog", "y")
    call(r, "AddIntelToCard", 5, SELF, 2, None)     # 情报：空实现（只影响看敌方手牌的 UI）
    chk("表现/日志类不记录", not r.records and not r.tainted)
    chk("钩子表覆盖随机原语", "RandomIntFromRangeWithStream" in ALL_HOOKED
        and "GiveRandomCombatKeyword" in ALL_HOOKED)

    r = record_effects(None, 0)                                   # 没有进程 ⇒ 说清楚，不崩
    chk("VM 不可用时如实报 stopped", r["complete"] is False and r["stopped"])
    return fails


if __name__ == "__main__":
    import sys
    print("semantics.effectvm 离线自检")
    n = selftest()
    print("失败 %d 项" % n)
    sys.exit(1 if n else 0)
