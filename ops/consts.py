# -*- coding: utf-8 -*-
"""ops/consts.py —— 备注偏移常量、ECardLocationEnum、拖拽各步停顿（SETTLE_*）、注入 JS 的渲染入口（`render_js`）。

由 `ops/inject.py`（P6/D1 拆分）搬出，内容与原来逐字相同。
"""
from __future__ import annotations

from kardsmem import build as B

# ---------------------------------------------------------------------------
# 备注常量（**只在解析失败时当兜底**；正常路径走反射链现算）
# 来源：reverse-data/sdk/1.60.27292.Steam/CppSDK/SDK/BP_BaseCard_classes.hpp
# ---------------------------------------------------------------------------
NOTE_OFF_CARD_UNDER_CURSOR = 0x03EC      # int32  cardUnderCursor
NOTE_OFF_LOCATION_NUMBER = 0x0664        # int32  LocationNumberUnderCursor
NOTE_OFF_LOCATION_ENUM = 0x0668          # u8     LocationUnderCursor (ECardLocationEnum)
NOTE_OFF_ROW_UNDER_CURSOR = 0x0669       # bool   RowUnderCursor
NOTE_OFF_HUD_END_TURN_BTN = 0x05D0       # ptr    BP_Widget_Battle_HUD_PC_C::EndTurnButton
NOTE_OFF_ACTOR_CARDID = 0x03C8           # int32  ABP_BaseCard_C::CardID（actor 上，规格 §5）
NOTE_OFF_ACTOR_SELF_BASECARD = 0x0808    # ptr    ABP_BaseCard_C::selfBaseCardRef（actor → 卡对象）
# ptr `UBaseCardObject::targetOverride`（`kards_classes.hpp:2583`）——**带目标出牌要写的就是它**：
# `BP_CardFunctions_C::GetTargetedCard` 先读它、再读 `currentTarget`（1.60 没这个字段）；
# 游戏自己探测目标时也写它（`BP_HandCard.cpp:4783`）。
NOTE_OFF_CARDOBJ_TARGET_OVERRIDE = 0x0548
NOTE_OFF_ARROW_OVER_CARD_ID = 0x0320     # int32  BP_targetArrowRVX_C::overCardID
# ptr `BP_targetArrowRVX_C::overCard`（UBaseCardObject*）—— 提交口有时读它而不是 overCardID
NOTE_OFF_ARROW_OVER_CARD = 0x0328
# —— PC 上**鼠标**拖拽用的 5 个字段（`ida-attack-report.md` §2；`SelectedCard` 是手柄字段，别碰）——
NOTE_OFF_PC_MOUSE_OVER_ACTOR = 0x07F0    # ptr  mouseOverActor
NOTE_OFF_PC_OLD_MOUSE_OVER_ACTOR = 0x07F8  # ptr  oldMouseOverActor（游戏每帧尾部落盘=上一帧的 mouseOverActor）
NOTE_OFF_PC_LEFT_BUTTON_DOWN = 0x0800    # bool leftButtonDown
NOTE_OFF_PC_MOUSE_DOWN_ACTOR = 0x0808    # ptr  MouseDownActor
NOTE_OFF_PC_DRAG_ACTOR = 0x0848          # ptr  dragActor
NOTE_OFF_PC_DRAG_STARTED = 0x0868        # bool dragStarted
# cursor 组：`cursor` dict 的 key → 被拖 actor 上的字段名
CURSOR_FIELD = {"card_under_cursor": "cardUnderCursor",
                "location_number": "LocationNumberUnderCursor",
                "location_enum": "LocationUnderCursor",
                "row": "RowUnderCursor"}
# bool BP_Board_C::isSelectingHandTarget —— 游戏自己的"某张手牌正在选目标"标志
# （`BattleUtilityFunctions::HandCardIsSelectingTarget` 读的就是它；板卡按下流开头
#  靠它早退）。只读，用来判"现在是不是在等点目标"。
NOTE_OFF_BOARD_SELECTING_HAND_TARGET = 0x0C21
# ptr BP_Board_C::selectHandTargetWidget —— 等选**手牌**目标时的确认面板
# （`UConfirmHandTargetButton_Widget_C`；Construct 里把 Board 的
#  `isSelectingHandTarget/chooseOneActive` 置 true 并把自己挂上去）
NOTE_OFF_BOARD_SELECT_HAND_WIDGET = 0x0C28
NOTE_OFF_HTGT_WIDGET_CARD_ID = 0x0394    # int32 `cardBeingPlayed`（在等选目标的源卡 CardID）
NOTE_OFF_HTGT_WIDGET_TARGET = 0x0390     # int32 `targetCardID`（玩家点中的那张手牌）
NOTE_OFF_HTGT_WIDGET_CARD_OBJ = 0x03B8   # ptr `_cardBeingPlayedObject`（源卡对象）
# double BP_BoardCard_C::targetArrowFinalLength @0x0BD8 —— 箭头销毁时由箭头自己写，
# `IsTargetArrowLengthValid()` 拿它跟 3000（桌面）/5000（移动端）比。
NOTE_OFF_ARROW_FINAL_LENGTH = 0x0BD8
NOTE_OFF_ARROW_FROM_CARD = 0x0330        # ptr    BP_targetArrowRVX_C::fromCard（箭头 ← 攻击者的卡对象）
NOTE_OFF_ARROW_HEAD_PLANE = 0x02F8       # ptr    BP_targetArrowRVX_C::arrowHeaderPlane（头部平面组件）
# ★ 2026-09-25 加：两阶段"带目标出牌"的**权威状态**。
#   `BP_Logic_C::cardBeingPlayedFromHand` = 正在打的那张手牌的 CardID（0 = 没有）；
#   阶段一（拖到我方支援线松手）写上它 + 落点，**阶段二点完目标才归 0**
#   （1.58 导出 `BP_Logic.cpp:9356`）。⇒ 它是"现在是不是在等选目标"的权威判据。
NOTE_OFF_LOGIC_PLAYING_FROM_HAND = 0x06F8  # int32 BP_Logic_C::cardBeingPlayedFromHand
NOTE_OFF_LOGIC_PLAYING_LOC = 0x06FC        # int32 BP_Logic_C::cardBeingPlayedFromHandLocNum
# ★ 2026-09-26：**"当前待选的候选集"的权威记录**（用户要求"必须能确定当前待选的是哪三张"）。
#   `BP_Logic_C::onlineMatch //0x0810` → `BP_OnlineMatch_C::selectCardToDrawPending //0x08F0`
#   是 `TMap<int32, FString>`：key = 触发卡 id，value = `a;b;c` 候选内部名串。
#   它 == 动作流 `ZActionSelectCardToDrawPending{cardBeingPlayed, spawnCards}` 的内存本体。
NOTE_OFF_LOGIC_ONLINE_MATCH = 0x0810
NOTE_OFF_OM_SELECT_CARD_TO_DRAW = 0x08F0
# ptr `UClass::ClassDefaultObject // 0x110`（`CoreUObject_classes.hpp` 核实）
# —— 卡类 CDO 上有 `title` FText（@0x58）⇒ "标题表内存里能读"就是这么读。
NOTE_OFF_UCLASS_CDO = 0x0110
# —— "当前对局挂了什么"的三条原语（用户 2026-09-26："不扫，而是查找当前对局挂的到底是"）——
# `UWorld::Levels // 0x01C8`（`TArray<ULevel*>`，Engine_classes.hpp:14220）
NOTE_OFF_WORLD_LEVELS = 0x01C8
# `ULevel::Actors // 0x00A0`（`TArray<AActor*>`，Engine dump 原文："THIS IS THE ARRAY YOU'RE LOOKING FOR!"）
NOTE_OFF_ULEVEL_ACTORS = 0x00A0
# `BP_Board_C::BattleHUD // 0x0AA0` —— 实测这一跳直接就是当前对局的 HUD（1 跳，零扫描）
NOTE_OFF_BOARD_BATTLE_HUD = 0x0AA0
# `BP_Widget_Battle_HUD_C::BPWidgetBattleHUDPC // 0x0470`（少数版本要多这一跳）
NOTE_OFF_HUDCONTAINER_HUDPC = 0x0470
# FText `BP_BaseCard_C::title`（`BP_BaseCard_classes.hpp`；`board_api` 读活卡也是这个偏移）。
# ★ 用户 2026-09-26：**卡类/CDO 运行时不动** ⇒ 按类名拿 CDO 直接读，不用扫实例。
NOTE_OFF_CARD_TITLE = 0x0058
NOTE_OFF_HANDCARD_SHOULD_DISCARD = 0x0980   # bool  BP_HandCard_C::shouldDiscard（换牌标记）
NOTE_OFF_LOGIC_PREGAMESTATE = 0x05D0     # u8     BP_Logic_C::PreGameState (E_PreGameStates)
NOTE_OFF_LOGIC_MY_MULLIGAN_DONE = 0x0925  # bool  BP_Logic_C::myMulliganDone
NOTE_OFF_SETTINGS_SURRENDER_BTN = 0x03F0  # ptr   Battle_Settings_Widget_C::EndMatch_Surrender
NOTE_OFF_DECK_SHOWING_STARTING_HAND = 0x03F8  # bool BP_Deck_C::showingStartingHand（换牌窗口的真判据）
NOTE_OFF_DECK_FOR_ENEMY = 0x0478             # bool BP_Deck_C::deckForEnemy（True=对手的那份）
NOTE_OFF_WIDGET_VISIBILITY = 0x00DC      # u8     UWidget::Visibility（0=Visible 1=Collapsed 2=Hidden 3/4=HitTestInvisible）
NOTE_OFF_EOM_CLICKTHROUGH = 0x03D0       # ptr    W_EndOfMatch_C::ClickThrough（全屏"点击继续"）
NOTE_OFF_EOM_STEP = 0x0488               # u8     W_EndOfMatch_C::step (E_EndOfMatchStep 0..8)
NOTE_OFF_PLAYBAR_TRAINING = 0x0350       # ptr    WBP_NUI_Playbar_C::TrainingButton（模式列表起点）
NOTE_OFF_SIDEBAR_PLAY_BTN = 0x0398       # ptr    W_DeckSelectedSideBar_C::PlayButton（"开始"）
NOTE_OFF_DECKBTN_NAME = 0x03C0           # ptr    W_MatchDeckSelectionDeckButton_C::deckName (UTextBlock*)
NOTE_OFF_DECKBTN_REASON = 0x0360     # ptr    W_MatchDeckSelectionDeckButton_C::InvalidDeckReasonText (URichTextBlock*)
NOTE_OFF_DECKBTN_BUTTON = 0x03C8         # ptr    W_MatchDeckSelectionDeckButton_C::deckButton (UButton*)
NOTE_OFF_TEXTBLOCK_TEXT = 0x0188         # FText  UTextBlock::Text（原生类，跨构建较稳；规格 §7.6f）
NOTE_OFF_SIDEBAR_RANK_TOGGLE = 0x0380    # ptr    W_DeckSelectedSideBar_C::RankedCasualToggle（排位/休闲）
NOTE_OFF_WIDGET_ENABLED_BYTE = 0x00D9    # u8     UWidget::bIsEnabled 所在字节（BitIndex 0x02）
NOTE_OFF_WIDGET_ENABLED_MASK = 0x04      #        ↑ 那一位的掩码（1<<2）
NOTE_OFF_PLAYBAR_WORLD = 0x0348          # ptr    WBP_NUI_Playbar_C::WorldChampionshipButton
NOTE_OFF_PLAYBAR_TOURNAMENT = 0x0358     # ptr    WBP_NUI_Playbar_C::TournamentButton
NOTE_OFF_PLAYBAR_SKIRMISH = 0x0360       # ptr    WBP_NUI_Playbar_C::SkirmishButton
NOTE_OFF_PLAYBAR_TEXTAREA = 0x0348       # ptr    WBP_NUI_MasterPlaybarButton_C::TextArea (UTextBlock*)
NOTE_OFF_PLAYBAR_INNER_BTN = 0x0368      # ptr    WBP_NUI_MasterPlaybarButton_C::Button (UButton*)
NOTE_OFF_PLAYBAR_BUTTON_ONCLICKED = 0x0380  # TArray<FScriptDelegate> WBP_NUI_MasterPlaybarButton_C::OnClicked
                                            # （2026-10-03 实测：订阅者是 playbar 的 ..._onClicked 处理器；见 flow.select_mode_by_label）
NOTE_OFF_UBUTTON_ONCLICKED = 0x0538      # TArray<FScriptDelegate>  UButton::OnClicked（多播，+8=Num）
NOTE_OFF_MATCHDATA_CHOSEN_DECK_ID = 0x02B4  # int32  BP_MatchData_C::chosenDeckID（真正生效的牌组）
# ---- 二选一 / 三选一候选（2026-09-25；SDK dump 逐字段，见下面 pick_candidates 的注释）----
# 三选一/预报子选项：`ABP_ChooseCardToSpawn_C`（`BP_ChooseCardToSpawn_classes.hpp`）
NOTE_OFF_SPAWN_INDEX = 0x0878          # int32 indexOfCardInDeck（ExposeOnSpawn）⇒ 屏幕次序
NOTE_OFF_SPAWN_TRIGGER = 0x0880        # ABP_HandCard_C* cardBeingPlayed（ExposeOnSpawn）
NOTE_OFF_SPAWN_NAME = 0x095C           # FName Name_0（候选卡的内部名，如 card_event_rain1_mist）
NOTE_OFF_SPAWN_SELECTED = 0x0964       # bool SelectedCard
NOTE_OFF_SPAWN_TRIGGER_ID = 0x0968     # int32 cardBeingPlayedID（触发者的 CardID）
NOTE_OFF_SPAWN_OWNED = 0x096C          # bool isOwnedByMe
# ★ 2026-09-27：`bool isEffect // 0x980`。**同一个 actor 类**下的两段预报靠它区分：
#     isEffect=0 ⇒ 预报第一段（三个天气模板）/ 普通三选一
#     isEffect=1 ⇒ 预报第二段（2K/4K/6K）等 effect 选择，提交串是 `effectSelected:<名>`
#   （提交串那一侧见 `BP_ChooseCardToSpawn.cpp:204`；`isEffect` 由 `selectCardToDraw(isEffect)`
#    一路带下来。）以前判"哪一层在等"只能靠 `SelectedCard` 启发式 —— 有了它才是确定的。
NOTE_OFF_SPAWN_IS_EFFECT = 0x0980      # bool isEffect
# 二选一：`ABP_ChooseOneCard_C`（`BP_ChooseOneCard_classes.hpp`）
#   ★ 旧文档写"二选一没有可靠的 index 字段、只能算屏幕坐标"——**那是没查 SDK**。
#     它自己就有 `chooseOneIndex // 0x09A0`（int32，ExposeOnSpawn），一条 grep 就在。
NOTE_OFF_ONE_INDEX = 0x09A0            # int32 chooseOneIndex（ExposeOnSpawn）⇒ 屏幕次序
NOTE_OFF_ONE_NEEDS_TARGET = 0x09A4     # bool needsTarget（选完还要再点目标）
NOTE_OFF_ONE_IS_BEING_PLAYED = 0x09B0  # bool isBeingPlayed
NOTE_OFF_ONE_SPAWN_CARD = 0x08B8       # UBaseCardObject* SpawnCard（这个分支会生成哪张卡）
NOTE_OFF_ONE_CARD = 0x08C0             # UBaseCardObject* _card
NOTE_OFF_ONE_MOTHER = 0x09B8           # ABP_HandCard_C* _motherCard（ExposeOnSpawn）= 触发它的手牌
NOTE_OFF_ONE_CLICKABLE = 0x09C1        # bool Clickable
# 触发卡（`UBaseCardObject`）上的二选一选项文本数组（`kards_classes.hpp:2465`）
NOTE_OFF_CARDOBJ_CHOOSE_ONE_CARDS = 0x0138   # TArray<FChooseOneCardStruct>(文本 FText@+0 + flag@+0x10)
PICK_STRUCT_SIZE = 0x18
# 卡自己的 `CanPlayFromHand` 里"**缺指向目标**"这一类理由码（实机见过前两个）：
#   预检阶段没有箭头 ⇒ 指向类卡必然报这些 ⇒ 不能拿它当否决，要报"需要目标"让调用方处理。
NEEDS_TARGET_REASONS = frozenset((
    "enemy_unit",                  # HENSCHEL HS 129：要选一个敌方单位
    "enemy_in_supply_line",        # GUNSHIP MISSION：要选一个**敌方支援线**单位
    "must_target_unit", "no_target", "needs_target", "enemy_unit_in_supply_line",
))

# ECardLocationEnum（规格 §4.11）
LOC_BOARD_HQLEFT = 5                     # 整个后排（HQ + 支援线），left 侧
LOC_BOARD_HQRIGHT = 6                    # 同上，right 侧
LOC_BOARD_FRONTLINE = 7

HUD_CLASS = "BP_Widget_Battle_HUD_PC_C"
MODULE = "kards-Win64-Shipping.exe"

# 拖拽生命周期里每一步之间的停顿：真实鼠标不会是一次调用连着一次调用，
# 留出跟真人同量级的时间，别让引擎看到"零耗时"的事件序列。
# ★ 悬停要**够久**：展示卡（`BP_BaseCard_C::myShowCaseCard` 那个 actor）是悬停时才
#   Spawn 的，卡面美术/数值是**异步加载**的 —— 实测 1.2s 时抓屏还是空白的半成品，
#   6s 时才是完整卡面。悬停太短，屏幕上那张大卡就是"没渲染好"的样子，跟真人不一样。
# ★ 2026-10-01 用户："通常情况下，除了 inspect 的 hover 要几秒左右，其它都是 0.5 s 差不多就行了，
#   鼠标滑过那种。抉择时也是。" ⇒ 攻击/移动/点目标/抉择点选共用的悬停 1.0 → 0.5 s
#   （出牌 0.35 s 与 inspect 1.5 s 不变）。展示卡异步加载没渲染完的"半成品"观感，
#   用户接受：真人"滑过"也是这样。悬停仍然**必须有**（转发给对手），下限 0.3 s 不变。
# ★ 2026-10-01 临时回 1.0：失焦时攻击被吃掉，怀疑 0.5 s 在降帧下不够（测试中）
SETTLE_HOVER = 1.0
SETTLE_AFTER_DISPATCH = 0.12
SETTLE_AFTER_DOWN = 0.06
SETTLE_AFTER_TICK = 0.08
# ★ 2026-09-26 用户："可以略微降低。" ⇒ 基准整体下调 ~30%：
#   hover 1.5→1.0、dispatch 0.15→0.12、down 0.08→0.06、tick 0.10→0.08
#   一次手势 1.91 s → 1.32 s；两阶段出牌 ≈ 5.47 s → 3.76 s。
# ⚠ 上面那条"展示卡异步加载"的约束**依然成立**（1.2s 可能是空白半成品、6s 才是完整卡面），
#   所以 hover 没有降到 0.4~0.5 s —— 再往下要先拿 `myShowCaseCard` 的
#   `bIsReadyToRender // 0x0820` 实测出"多长才渲染好"，别凭感觉调。
# `KARDS_SETTLE_SCALE` 可整体缩放（调试/想看清 hover 时调大）；
# **悬停下限 0.3 s**（红线：不能"跳过悬停"）—— 只夹 hover；
# 其它三个是"引擎事件之间的最小间隔"，下限只保证**不是零耗时**（0.02 s）。
SETTLE_FLOOR = 0.3
SETTLE_FLOOR_MINOR = 0.02


def _settle(v: float, floor: float) -> float:
    """按 `KARDS_SETTLE_SCALE` 缩放并夹住下限。`floor` 由调用方给：
    悬停用 `SETTLE_FLOOR`（红线），微停顿用 `SETTLE_FLOOR_MINOR`。
    ⚠ 早先的写法对所有四个都套了 0.3 ⇒ 微停顿被抬到 0.3、手势反而从 1.91 s 变成 2.20 s。
    """
    import os as _os
    try:
        s = float(_os.environ.get("KARDS_SETTLE_SCALE", "1") or 1)
    except ValueError:                                         # noqa: BLE001
        s = 1.0
    return max(floor, v * s)


SETTLE_HOVER = _settle(SETTLE_HOVER, SETTLE_FLOOR)
SETTLE_AFTER_DISPATCH = _settle(SETTLE_AFTER_DISPATCH, SETTLE_FLOOR_MINOR)
SETTLE_AFTER_DOWN = _settle(SETTLE_AFTER_DOWN, SETTLE_FLOOR_MINOR)
SETTLE_AFTER_TICK = _settle(SETTLE_AFTER_TICK, SETTLE_FLOOR_MINOR)

# ★ 2026-09-26 用户区分了两种悬停（**语义不同，时长就该不同**）：
#   * `SETTLE_HOVER_INSPECT`（1.5 s）—— **刻意看牌**（`inspect`）：友方手牌 / 场上卡都 1.5 s；
#   * `SETTLE_HOVER_PLAY`（0.35 s）—— **出牌时的悬停**：那是"鼠标经过这张牌"然后拖出去，
#     不是停下来读它 ⇒ 可以短得多（短，但**必须有** —— 悬停会被转发给对手，属信息流的一部分）。
SETTLE_HOVER_INSPECT = _settle(1.5, SETTLE_FLOOR)
SETTLE_HOVER_PLAY = _settle(0.35, SETTLE_FLOOR_MINOR)

# ★ 2026-10-07 攻击手势里的两次悬停（攻击者 + 目标）单独一档：用户口径 0.5 s（"鼠标滑过"），2026-10-01 因失焦降帧
#   临时抬回 1.0 s；之后 `settle()` 已按帧等（至少 6 个 PC tick）+ 提交批次重写 PC.mouseOverActor，降帧下不再靠
#   墙钟时长兜底 ⇒ 默认 0.5 s（一次攻击省 ~1.0 s）。要退回旧值：环境变量 KARDS_SETTLE_HOVER_ATTACK=1.0。
#   下限仍是 SETTLE_FLOOR（0.3，红线：悬停必须有）。
def _hover_attack_default() -> float:
    import os as _os
    try:
        return float(_os.environ.get("KARDS_SETTLE_HOVER_ATTACK", "0.5") or 0.5)
    except ValueError:                                         # noqa: BLE001
        return 0.5


SETTLE_HOVER_ATTACK = _settle(_hover_attack_default(), SETTLE_FLOOR)

from ops import load_js_template as _load_js_template        # JS 模板已抽到 ops/agent.js.tpl（OPS 文档 §7 步骤 2）


def render_js() -> str:
    """按**调用时**的 `build.RVA` 渲染注入脚本（P7-S3b：不再 import 期烤值）。

    为什么必须调用期：RVA 走"用户缓存 → 种子复验 → 扫描"解析链，值可能在本进程运行期间才确定
    （attach 前才 resolve）。以前这里是模块级 `JS = ... % B.RVA[...]`，import 早于 resolve 就会
    把**占位种子地址**烤进 frida 脚本；`build.apply` 只能记一个 `RESTART_NEEDED` 提醒重启。
    现在 `ConnMixin.api()` 在 create_script 的那一刻取字符串 ⇒ 用的是当次解析结果。
    """
    return _load_js_template() % {"module": MODULE, "stem": MODULE[:-4].lower(),
                                  "pe_rva": B.RVA["UObject_ProcessEvent"],
                                  "gobjects_rva": B.RVA["GObjects"],
                                  "namepool_rva": B.RVA["FNamePool"]}
