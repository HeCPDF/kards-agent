#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.effectvm_tables —— `effectvm` 的 API 词汇表（动词 → 效果键/实参下标、各动词族集合、`ALL_HOOKED`、`SET_ENC_FIELDS` …）。

P6 自 `engine/effectvm.py` 原样拆出（纯搬移，注释/证据原样保留）；`engine.effectvm` 再导出，路径不变。
"""
from __future__ import annotations

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
    # 多目标版本（实参 0 是一组卡 id；目标清单在 `rec.records` 里，engine/triggers.py::hit_effects 把它们拆成逐牌效果）
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
# 授予自定义能力：`CustomAbilityAdd(ability, cardID, giverID, …)`（2026-10-07 起**记录**，不再整个吞掉）。
#   记成 `eff["ability_grants"] = [[能力名, 被授予单位 id, 授予者 id|None]…]`；sim 消费时走 `natives.abilities.grant_ability`
#   （计数 + giver 账 `Sim.grants`）。ECHELON 一族的"延迟钩子认谁"靠它：钩子体里 `HasCustomAbilityFromCard("trigger", 授予者id)`。
ABILITY_VERBS = frozenset(("CustomAbilityAdd",))
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
                          "CustomAbilityRemove",
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

# SpawnMultipleCardsOnBattlefield(side, Frontline, cardNames[], spawnerID, giveBlitz, &spawnedCardIDs, makeVeteran)
# （BP_CardFunctions.cpp:8407，PARACHUTE ASSAULT 用）：体内走 `CreateCard`（`GetObjectClass`+`SpawnObject`，离线 VM 没有）⇒ 必须直接钩住。
SPAWN_MULTI_VERBS = frozenset(("SpawnMultipleCardsOnBattlefield",))
# 单张生成的**出参位次**（`spawnedCardID` 是签名最后一项；BP_CardFunctions.cpp:1121 / :6417）：录制 VM 往它写临时 id，
# 紧随其后的 `GiveBlitz(spawnedCardID, …)` 才能并回对应的 spawn_cards 条目（ENCIRCLEMENT）。
SPAWN_ONE_OUT = {"SpawnCardInFrontline": 5, "SpawnCardOnBattlefield": 10}

ALL_HOOKED = (set(SPAWN_MULTI_VERBS) | set(COUNT_VERBS) | set(FLAG_VERBS) | set(GIVE_KW) | set(RANDOM_VERBS)
              | set(IGNORE_VERBS) | set(CHOICE_VERBS) | set(JSON_VERBS)
              | set(SIDE_VERBS) | set(END_TURN_VERBS) | set(DECK_VERBS)
              | set(SALVAGE_VERBS) | set(PIN_VERBS) | set(STEAL_VERBS)
              | set(SPAWN_DECK_VERBS) | set(REVEAL_VERBS) | set(DEFENSE_AOE_VERBS)
              | set(TAX_VERBS) | set(RESTRICTION_VERBS)
              | set(INTEL_VERBS)
              | set(DISCARD_VERBS) | set(RECORD_ONLY) | set(ABILITY_VERBS))

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
