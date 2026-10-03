#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/inject.py —— **执行侧（唯一）**：游戏内合成事件，不碰真实鼠标。

为什么存在
==========
2026-09-25 红线收窄后，判定标准从"不许写内存/不许注入"变成
**"发给服务端/对手的信息流必须跟真人用鼠标操作产生的完全等价"**。
于是有了这条路：进程内直接调用游戏自己的 Blueprint 事件，把"鼠标移动→悬停→按下→
拖拽→落地"这串**真实事件序列**在引擎里重演一遍，完全不挪真实光标。

★ 纪律（不要为了让某个动作"能成"而违反它）
------------------------------------------
1. **不跳步**：每个动作都按真实鼠标的完整序列触发中间事件（悬停 → 悬停转发 →
   按下 → 起拖 → 拖动 tick → 落地/松开），**绝不直接跳到"打出/攻击"那个终点函数**。
   理由很具体：悬停信息会被转发给对手（卡面高亮等可见效果），跳过悬停，
   服务端/对手看到的行为模式就跟真人不一致。
2. **不绕过游戏自己的合法性闸门**：提交那一步（`OnActorEndDrag`）内部还会重跑
   一遍 `AttemptToPlayFinal`，不合法就转 `ClearAndRearrange` 取消 —— 我们**不替它做决定**。
3. **只写"真实鼠标本该产生的中间结果"**：卡牌的落点判定链路
   （`ABP_Board_C::FindCardLocationUnderCursor()`）是**纯出参、零输入**的，内部直接读
   真实 OS 光标做射线检测，没有任何"查哪个坐标"的参数化入口。所以不碰真实鼠标时，
   唯一办法是把它**本会写出的那几个实例变量**（`RowUnderCursor` /
   `LocationUnderCursor` / `LocationNumberUnderCursor` / `cardUnderCursor`）写成真实
  合法的值。写的是"如果鼠标真的移过去会查出来的东西"，不是编造状态。
4. **不伪造回执**：动作成没成一律读 `matchlog`（游戏自己认下来的动作才会进
   `AllMatchActions`）。
   ★ 反向纪律（2026-09-27）：**不写手柄/拖拽态字段**（`PC->SelectedCard`、
   `leftButtonDown`、`SetMouseDownActorManual` …）—— 伪造它们会让游戏自己补发一次
   提交事件，跟我们的清理撞成两次 ⇒ 候选卡 use-after-free（`click-crash-report.md`）。
5. **动作参数一律 `card_id`**（用户 2026-09-27 定调）：不收卡名 —— 场上可能有多个
   同名单位，名字消解不了歧义。`resolve_target` 只额外认"位置规格"（`hq`/`front0`/
   `back1`/`guard0`），它们本来就映射到确定的 card_id。

★★ 2026-09-27：**本文件已经是唯一的执行侧** —— 旧的物理鼠标实现 `ops.py` 已**归档**
为 `_archive/ops_mouse.py`（用户定调："确保和 ops 一样完善即可，然后便可归档 ops（弃用）"）。
归档前的对齐审计（旧 `ops.py` 顶层 48 项 → 本文件）：
  * **动作**：`play_event`/`play_select`/`play_unit`/`play_card_from_hand` → `play_card`
    （无目标）/ `play_card_event_with_target`（**指令**指向，一次成交）/ `play_card_unit_with_target`
    （**单位**两阶段）；`move_card_to_line` → `move_to_front`；`attack_card` → `attack_card`；
    `act_pick` → `pick_choice`（+ `pick_layers` 多层链）；`hand_target_selected` →
    `select_hand_target`；`act_mulligan_toggle`/`act_mulligan_confirm` →
    `mulligan_mark`/`mulligan_confirm`；`surrender`/`end_of_turn`/`wait_our_turn` 同名。
  * **判据**：`can_act_now`（部署病，2026-09-27 补）、`is_pinned`、`preflight`、
    `game_can_*`（直接问游戏本体 —— 旧版没有这一层）。
  * **只读**：`find_card`、`pick_state` → `pick_pending`/`pending_summary`、
    `pick_candidates`、`pick_target`、`notify_texts`、`matchlog`。
  * **不搬的**：像素/坐标层（`screen_map*`/`row_x`/`hand_x_list`/`deploy_slots`/
    `mulligan_x`/`read_field_fresh`/`src`/`snap`/`hwnd`）—— 那是"真实光标要点哪里"才
    需要的量；合成事件路径由**游戏自己的出参**（`LocationUnderCursor` 等字段）表达落点。
    屏幕坐标只在诊断时用（`board_card_screen_pos` = `ProjectWorldLocationToScreen`
    + `base/winapi.py`）。行模型留在归档模块里，`rowcalib.py`/`test_rowmodel.py` 仍按
    归档名（`ops_mouse`）引用它。

地址/偏移**一律运行时解析**（`kismet.find_function` / `props.find_prop` / `ObjectArray`），
下面注释里的数值只是"给人看的备注"。

用法
====
    python ops/inject.py selftest          # 只解析、不动手（先跑这个）
    python ops/inject.py end               # 结束回合
    python ops/inject.py play <card_id>    # 打出（事件/单位自动分流）
    python ops/inject.py pending           # 现在在等什么（只读汇总）
    python ops/inject.py target <spec>     # 目标规格 → card_id（只读）
    python ops/inject.py order <card> <tgt>    # 指向性指令（一次成交）
    python ops/inject.py deploy2 <card> <tgt>  # 单位两阶段（落地 → 点目标）
    python ops/inject.py front <card> [slot]   # 上线/移动
"""
from __future__ import annotations

import atexit
import json
import os
import struct
import time
from typing import Optional


from kardsmem import attach, kismet, props                       # noqa: E402
from kardsmem import build as B                                  # noqa: E402
from kardsmem.objects import ObjectArray                         # noqa: E402
from kardsmem.pick import hand_card_actors, player_controller    # noqa: E402


# ---------------------------------------------------------------- 开局按钮灰态开关
def play_gray_guard_on() -> bool:
    """`press_play` 要不要恢复"灰的就不点"的原判据（默认**关**）。

    ★ 2026-10-03（用户拍板）：默认不拦灰按钮。这行只决定**我们自己**发不发这次
      点击 —— 不改按钮的 `enabled`/视觉状态（界面侧照旧是灰的，UX 不变），也不碰
      服务端校验；对战/休闲的真校验在大厅/服务端（点完会被拒，游戏弹 1~2 s 的浮动
      通知"卡组错误，进入大厅失败"）。开局是否真成立，另由
      `gui.autoplay.verify_start()` 在点击后**只读**判定。
      想临时回到严格模式：设 `KARDS_PLAY_BLOCK_GRAY=1`（每次调用时读，无需重启监听器）。
    """
    return os.environ.get("KARDS_PLAY_BLOCK_GRAY", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- frida 临时目录（2026-10-02 事故）
# 每次 `frida.attach` 都会在 %TEMP% 下解压一份 `frida-<hash>`（≈43 MB 的 agent），进程被杀/崩溃时不清理 ——
# 9/25 起累计 612 份 ≈ 25.8 GB 吃满了用户 Temp。⇒ ① 本进程的 TEMP/TMP 指到专用目录（泄漏也落在那里，不污染用户 Temp）；
# ② attach 前、detach 后清掉**没被占用**的旧目录（agent dll 能以追加方式打开 ⇒ 没人在用；被占用的跳过）。
FRIDA_TMP = os.environ.get("KARDS_FRIDA_TMP", r"D:\Kards\_frida_tmp")


def _frida_tmp_setup() -> None:
    try:
        os.makedirs(FRIDA_TMP, exist_ok=True)
        os.environ["TEMP"] = os.environ["TMP"] = FRIDA_TMP
    except Exception:                                              # noqa: BLE001
        pass


def sweep_frida_tmp(min_age_s: float = 600.0) -> int:
    """删掉专用目录和用户 Temp 里**不在用**的旧 `frida-*` 目录，返回删掉的个数（失败的跳过，从不抛）。"""
    import shutil
    n = 0
    roots = {FRIDA_TMP, os.environ.get("LOCALAPPDATA", "") and os.path.join(os.environ["LOCALAPPDATA"], "Temp")}
    now = time.time()
    for root in roots:
        try:
            names = [x for x in os.listdir(root) if x.startswith("frida-")] if root and os.path.isdir(root) else []
        except Exception:                                          # noqa: BLE001
            continue
        for nm in names:
            d = os.path.join(root, nm)
            try:
                if not os.path.isdir(d) or now - os.path.getmtime(d) < min_age_s:
                    continue
                busy = False
                for dp, _dn, fn in os.walk(d):
                    for f in fn:
                        if f.lower().endswith(".dll"):
                            try:
                                with open(os.path.join(dp, f), "ab"):
                                    pass
                            except OSError:
                                busy = True                          # 被某个进程加载着 ⇒ 在用，整个目录跳过
                                break
                    if busy:
                        break
                if not busy:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 0 if os.path.exists(d) else 1
            except Exception:                                      # noqa: BLE001
                continue
    return n

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

from ops import load_js_template as _load_js_template        # JS 模板已抽到 ops/agent.js.tpl（OPS 文档 §7 步骤 2）

JS = _load_js_template() % {"module": MODULE, "stem": MODULE[:-4].lower(), "pe_rva": B.RVA["UObject_ProcessEvent"],
                            "gobjects_rva": B.RVA["GObjects"], "namepool_rva": B.RVA["FNamePool"]}


# ---------------------------------------------------------------------------
# 跨脚本缓存（2026-09-25：用户指出的真问题——每次 `python -c`/`python ops/inject.py`
# 都是全新进程，`Injector` 实例内的 `_fn_cache`/`_off_cache`/class 缓存一次都没能
# 跨脚本复用，逼着每个短命脚本都从头再扫一遍。
#
# ★ 关键前提：`UClass`/`UFunction` 是**引擎启动时创建、进程存活期内地址不变**的
#   反射元数据（不是每局对局都会重建的东西）——只要**目标游戏进程没重启**
#   （`pid` 不变），这些指针在我们自己这边不同 `python` 进程之间复用是安全的。
#   `字段偏移`（`props.find_prop` 算出来的）更稳，跟指针无关，永远能复用。
#   ⚠ **不缓存实例指针**（手牌/牌组按钮/换牌UI 这些每次进新的一屏/一局都可能是
#   新对象）——只缓存"类→类对象""类+字段名→偏移""类+函数名→UFunction*"这三样，
#   这些是本来就该跨对局稳定的东西。缓存文件里存的 `pid` 跟当前游戏进程一对不上
#   （比如重启过客户端）就整个作废重扫，不会拿旧进程的指针误用到新进程上。
# ---------------------------------------------------------------------------
from base import paths as _paths_oi
_CACHE_PATH = os.path.join(_paths_oi.DATA, "ops_inject_cache.json")          # 运行期缓存放数据目录，不放源码树


def _load_persistent_cache(pid: int) -> Optional[dict]:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                        # noqa: BLE001
        return None
    if data.get("pid") != pid:
        return None
    try:
        fn_cache = {}
        for k, v in data.get("fn_cache", {}).items():
            uc, name, inh = k.split("|", 2)
            fn_cache[(int(uc), name, bool(int(inh)))] = v
        off_cache = {}
        for k, v in data.get("off_cache", {}).items():
            uc, name = k.split("|", 1)
            off_cache[(int(uc), name)] = v
        return {"class_cache": dict(data.get("class_cache", {})),
                "fn_cache": fn_cache, "off_cache": off_cache}
    except Exception:                                        # noqa: BLE001
        return None


def _save_persistent_cache(pid: int, class_cache: dict, fn_cache: dict, off_cache: dict) -> None:
    try:
        enc_fn = {"%d|%s|%d" % (k[0], k[1], int(k[2])): v for k, v in fn_cache.items()}
        enc_off = {"%d|%s" % k: v for k, v in off_cache.items()}
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "class_cache": class_cache,
                      "fn_cache": enc_fn, "off_cache": enc_off}, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception:                                        # noqa: BLE001
        pass


class Injector:
    """一条常驻的 Frida 连接 + 全部地址/偏移的运行时解析。"""

    def __init__(self, session=None, ks=None):
        self.ks = ks or attach(require_build=False)
        self.m = self.ks.m
        self.oa = ObjectArray(self.ks)
        self.pool = self.oa.pool()
        self._frida = None
        self._api = None
        self._logic = None
        self._board = None            # 单例缓存（GetBoard）；见 board_actor()
        self._deck = None
        self._nw = None              # 提示 watcher 缓存（notify_texts）；见 _notify_watcher()
        self._levels = None          # 当前世界全部 level（world_levels）
        self._actors = None          # 当前世界全部 level 的 actor（level_actors）
        self._actors_by_cls: dict = {}
        self._ml = None              # MatchLog 长寿命单例（见 matchlog()：locate 要 4~6 s）
        self._ml_located = False
        self._class_cache: dict = {}
        self._fn_cache: dict = {}
        self._off_cache: dict = {}
        self.notes: list = []
        cached = _load_persistent_cache(self.ks.pid)
        if cached:
            self._class_cache = cached["class_cache"]
            self._fn_cache = cached["fn_cache"]
            self._off_cache = cached["off_cache"]
            self.notes.append("跨脚本缓存命中（pid=%s）：class=%d fn=%d off=%d"
                              % (self.ks.pid, len(self._class_cache),
                                 len(self._fn_cache), len(self._off_cache)))
        atexit.register(self._save_persistent_cache)

    def _save_persistent_cache(self) -> None:
        _save_persistent_cache(self.ks.pid, self._class_cache, self._fn_cache, self._off_cache)

    # ------------------------------------------------------------ frida 管道
    def api(self):
        if self._api is None:
            _frida_tmp_setup()                               # 必须在 import frida 之前：agent 解压到专用目录
            sweep_frida_tmp()                                # 先清掉没人用的旧目录
            import frida                                     # 惰性：只有真要动手才需要
            self._frida = frida.attach(self.ks.pid)
            script = self._frida.create_script(JS)
            script.load()
            self._api = script.exports_sync
            self.notes.append("frida attached pid=%s base=%s pe=%s"
                              % (self.ks.pid, self._api.base(), self._api.pe()))
            self._gt_install()
        return self._api

    def _gt_install(self) -> None:
        """把注入调用切到**游戏线程**上执行（见 JS 里"游戏线程调度"的注释）。
        游戏线程 = 拥有游戏窗口的线程（UE 在游戏线程上建窗）。装不上就如实记到 notes，
        行为退回旧的"在 frida 线程上直接调"。环境变量 `KARDS_GT_DISPATCH=0` 可关掉。"""
        import os
        if os.environ.get("KARDS_GT_DISPATCH", "1") == "0":
            self.notes.append("游戏线程调度：已被 KARDS_GT_DISPATCH=0 关闭")
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            pid = int(self.ks.pid)
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lp):
                p = wintypes.DWORD(0)
                tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(int(tid))
                return True
            user32.EnumWindows(_cb, 0)
            if not found:
                self.notes.append("游戏线程调度：找不到游戏窗口线程，未启用（退回 frida 线程直接调）")
                return
            tid = max(set(found), key=found.count)
            self._api.gt_install(tid)
            # 默认把执行点放到玩家控制器 tick（和真实输入同一阶段）；环境变量 KARDS_GT_FILTER=any 用任意点
            filt_note = "任意 ProcessEvent"
            try:
                pc = player_controller(self.ks)
                f_tick = self.find_fn(self.uclass_of_instance(pc), "ReceiveTick") if pc else 0
                if f_tick:
                    self._api.gt_set_tick_fn(hex(f_tick))        # 帧计数（按帧等待用）
                    if os.environ.get("KARDS_GT_FILTER", "pctick") != "any":
                        self._api.gt_set_filter(hex(f_tick))
                        filt_note = "PC ReceiveTick(0x%x)" % f_tick
            except Exception as e2:                                    # noqa: BLE001
                filt_note = "任意 ProcessEvent（取 PC tick 失败：%s）" % e2
            self.notes.append("游戏线程调度执行点：%s" % filt_note)
            got = self._api.gt_probe()
            ok = int(got) == int(tid)
            self.notes.append("游戏线程调度：已启用 tid=%s，探针在 tid=%s 上执行 %s"
                              % (tid, got, "✓" if ok else "✗（线程对不上！）"))
        except Exception as e:                                   # noqa: BLE001
            self.notes.append("游戏线程调度：安装失败，退回 frida 线程直接调：%s" % e)

    def settle(self, seconds: float, frames: int = 0, cap: float = 4.0) -> None:
        """模拟输入里的"停留"：至少 `seconds` 墙钟，且（有帧计数时）游戏线程至少又跑了 `frames` 个
        PC tick。失焦时 UE 降帧，固定几十/几百毫秒里可能一帧都没跑（2026-10-01 攻击被吞的原因）。
        `cap`：总等待上限（游戏卡住时别无限等）。没有帧计数就退回纯墙钟。"""
        t0 = time.time()
        n0 = None
        if frames > 0:
            try:
                n0 = int(self.api().gt_ticks())
                if n0 < 0:
                    n0 = None
            except Exception:                                      # noqa: BLE001
                n0 = None
        time.sleep(max(0.0, seconds))
        if n0 is None:
            return
        while time.time() - t0 < cap:
            try:
                if (int(self.api().gt_ticks()) - n0) & 0xFFFFFFFF >= frames:
                    return
            except Exception:                                      # noqa: BLE001
                return
            time.sleep(0.02)

    def close(self):
        if self._frida is not None:
            try:
                self._frida.detach()
            except Exception:                                # noqa: BLE001
                pass
            sweep_frida_tmp(min_age_s=0.0)                   # detach 后立刻回收自己留下的（被占用的会自动跳过）
        self._frida, self._api = None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 解析
    def uclass_of_instance(self, obj: int) -> int:
        return self.oa.class_of(obj) or 0

    def class_named(self, name: str) -> int:
        """按类名找**该类的对象**（运行时，不依赖任何写死的地址）——**跨脚本缓存**：
        `UObject` 在目标进程存活期内地址不变，命中缓存就不用扫一遍 GObjects。

        ⚠ 名字骗人：它返回的**不是 UClass 对象**，而是"**class_of 的 fname == name** 的
          某个对象"（`scan_classes(skip_cdo=False)`，所以 CDO 和活实例都算）。
          ⇒ 要 UClass 请用 `self.oa.class_of(结果)`；要 CDO 请用 `class_cdo(name)`。
          要"该类的唯一活实例"（不想要 CDO）用 `instance_of_class(name)`。
        """
        if name in self._class_cache:
            return self._class_cache[name]
        rows = self.scan_classes([name], skip_cdo=False).get(name, [])
        if rows:
            self._class_cache[name] = rows[0]
            return rows[0]
        return 0

    def dump_props(self, obj: int, own_only: bool = False, limit: int = 160) -> dict:
        """只读：把一个 UObject 的反射属性里的**标量**字段读成 {名字: 值}（Bool/整数/浮点/枚举/指针是否为空）。
        用来对比"攻击成功 vs 失败"时箭头 actor 上哪个字段不同（2026-10-01：真鼠标放在目标上就能打）。"""
        from kardsmem import props
        res = {}
        try:
            cls = self.uclass_of_instance(obj)
            rows = props.struct_props(self.ks, cls, self.pool) if own_only else props.class_props(self.ks, cls, self.pool)
            for r in rows[:limit * 4]:
                nm, ty, off, sz = r.get("name"), r.get("type"), r.get("offset"), r.get("size")
                if not nm or off is None or off < 0 or (r.get("array_dim") or 1) != 1:
                    continue
                try:
                    if ty == "BoolProperty":
                        v = self.m.u8(obj + off)
                        v = bool((v or 0) & (r.get("bit_byte_mask") or 1))
                    elif ty in ("IntProperty",):
                        v = self.m.i32(obj + off)
                    elif ty in ("ByteProperty", "EnumProperty") and sz == 1:
                        v = self.m.u8(obj + off)
                    elif ty == "FloatProperty":
                        v = round(struct.unpack("<f", struct.pack("<I", self.m.u32(obj + off) or 0))[0], 3)
                    elif ty == "DoubleProperty":
                        v = round(struct.unpack("<d", struct.pack("<Q", self.m.u64(obj + off) or 0))[0], 3)
                    elif ty in ("ObjectProperty", "ClassProperty", "WeakObjectProperty") and sz == 8:
                        v = "ptr" if (self.m.ptr_or_zero(obj + off) or 0) else "null"
                    else:
                        continue
                except Exception:                                  # noqa: BLE001
                    continue
                res[nm] = v
                if len(res) >= limit:
                    break
        except Exception as e:                                     # noqa: BLE001
            res["__err"] = str(e)
        return res

    def screen_of_card(self, card_id: int) -> dict:
        """目标卡在**游戏视口**里的屏幕坐标（只读）：`K2_GetActorLocation` + `APlayerController::ProjectWorldLocationToScreen`。
        用来把真实光标移到目标上（用户 2026-10-01：真鼠标放在总部上，攻击就能打）。"""
        actor = self.board_actor_of(card_id)
        if not actor:
            return {"ok": False, "error": "场上找不到 card %s 的 actor" % card_id}
        loc = self.actor_location(actor)
        if not loc:
            return {"ok": False, "error": "读不到 actor 位置"}
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f = self.find_fn(pcls, "ProjectWorldLocationToScreen")
        if not f:
            return {"ok": False, "error": "PC 上没有 ProjectWorldLocationToScreen"}
        parms = bytearray(0x30)
        struct.pack_into("<ddd", parms, 0x00, loc[0], loc[1], loc[2])
        parms[0x28] = 0                                            # bPlayerViewportRelative=false：视口像素
        out = self.api().call_raw(hex(pc), hex(f), bytes(parms).hex())
        buf = bytes(out) if out else b""
        if len(buf) < 0x2A:
            return {"ok": False, "error": "投影没返回"}
        x, y = struct.unpack_from("<dd", buf, 0x18)
        return {"ok": bool(buf[0x29]), "x": x, "y": y, "world": loc}

    def obj_alive(self, obj: int) -> bool:
        """这个 UObject 指针现在还是**活的**吗（只读）？

        2026-10-01 崩溃现场（转储 `…094945.dmp`，AV 在 `ProcessEvent+0x2DE`，访问 0x1e3200c98，
        调用栈上是我们的注入调用）：对**已被销毁/回收的候选 actor**（预报第二层点击时第一层的 actor 已被
        GC/销毁）调 `ProcessEvent` ⇒ 读它的垃圾虚表 ⇒ 访问冲突。
        判据：`InternalIndex@+0x0C` → GUObjectArray 的 `FUObjectItem`：`Object` 必须还指回它，
        `Flags@+8` 不带 Garbage(0x200000)，`ObjectFlags@+8` 不带 BeginDestroyed/FinishDestroyed
        (0x8000 / 0x10000)。读不出来一律当**不活**（宁可不点）。"""
        try:
            if not obj or obj & 7:
                return False
            idx = self.m.u32(obj + 0x0C)
            item = self.oa.item_addr(int(idx))
            if not item:
                return False
            if (self.m.ptr_or_zero(item) or 0) != obj:
                return False
            if (self.m.u32(item + 8) or 0) & 0x200000:           # Garbage
                return False
            if (self.m.u32(obj + 0x08) or 0) & (0x8000 | 0x10000):   # BeginDestroyed / FinishDestroyed
                return False
            return True
        except Exception:                                          # noqa: BLE001
            return False

    def instance_of_class(self, name: str, skip_cdo: bool = True) -> int:
        rows = self.scan_classes([name], skip_cdo=skip_cdo).get(name, [])
        return rows[0] if rows else 0

    def find_fn(self, uclass: int, name: str, inherited: bool = True) -> int:
        key = (uclass, name, inherited)
        if key not in self._fn_cache:
            f = kismet.find_function(self.ks, uclass, name, inherited=inherited)
            self._fn_cache[key] = f or 0
        return self._fn_cache[key]

    def fn(self, uclass: int, name: str, inherited: bool = True) -> int:
        f = self.find_fn(uclass, name, inherited)
        if not f:
            raise RuntimeError("UFunction 找不到：%s（类 0x%X）" % (name, uclass))
        return f

    def off(self, uclass: int, name: str, note: int) -> int:
        """反射链现算字段偏移；算不出来退回备注常量并记一笔。"""
        key = (uclass, name)
        if key not in self._off_cache:
            pr = props.find_prop(self.ks, uclass, name, pool=self.pool)
            if pr:
                self._off_cache[key] = pr["offset"]
            else:
                self._off_cache[key] = note
                self.notes.append("字段 %s 反射链没算出来，退回备注 0x%X" % (name, note))
        return self._off_cache[key]

    # ------------------------------------------------------------ 原语
    def call0(self, obj: int, func: int) -> bool:
        return bool(self.api().call0(hex(obj), hex(func)))

    def call_ptr(self, obj: int, func: int, arg_ptr: int) -> int:
        return int(self.api().call_ptr(hex(obj), hex(func), hex(arg_ptr)))

    def call_i32(self, obj: int, func: int, value: int) -> int:
        return int(self.api().call_i32(hex(obj), hex(func), int(value)))

    def poke_and_call0(self, obj: int, off: int, kind: str, value,
                       call_obj: int, call_func: int) -> bool:
        v = hex(int(value)) if kind == "ptr" else value
        return bool(self.api().poke_and_call0(hex(obj), off, kind, v,
                                              hex(call_obj), hex(call_func)))

    def write_and_call(self, writes, call_obj: Optional[int] = None,
                       call_func: Optional[int] = None) -> list:
        """writes = [(obj, off, kind, value), ...]；回读值列表。"""
        spec = [[hex(o), off, k, (hex(int(v)) if k == "ptr" else int(v))]
                for (o, off, k, v) in writes]
        return list(self.api().write_and_call(
            spec, hex(call_obj) if call_obj else None,
            hex(call_func) if call_func else None))

    def poke(self, obj: int, off: int, kind: str, value) -> bool:
        # 指针按字符串传，避开 JS number 的精度边界
        v = hex(int(value)) if kind == "ptr" else value
        return bool(self.api().poke(hex(obj), off, kind, v))

    def peek(self, obj: int, off: int, kind: str):
        return self.api().peek(hex(obj), off, kind)

    # ------------------------------------------------------------ 定位
    def my_side(self) -> Optional[int]:
        """本地玩家的 ESideEnum（1=left / 2=right）。每局都可能不同（规格 §4.12）。"""
        try:
            from kardsmem.gs import GameState
            return GameState(self.ks).my_side
        except Exception:                                    # noqa: BLE001
            return None

    def our_back_enum(self) -> int:
        side = self.my_side()
        if side == 1:
            return LOC_BOARD_HQLEFT
        if side == 2:
            return LOC_BOARD_HQRIGHT
        raise RuntimeError("my_side 读不出，不能猜落点（见规格 §4.12）")

    def hand_actor(self, card_id: int) -> Optional[dict]:
        for r in hand_card_actors(self.ks):
            if r.get("card_id") == card_id:
                return r
        return None

    def widget_in_viewport(self, w: int) -> Optional[bool]:
        """`UUserWidget::IsInViewport()`（原生 const，纯读）——**判断一个 widget 是不是当前这一屏的**。

        ★ 2026-09-25 实机坐实的坑：上一局（对手投降）结束后重开一局，
          `BP_Widget_Battle_HUD_PC_C` 在进程里有 **2 个活实例**：
            0x2317C8BC580  `IsInViewport()`=0  ← 上一局的残留（`hud_actor()` 挑中的就是它）
            0x23183497510  `IsInViewport()`=1  ← 这一局的
          ⇒ `end_of_turn()` 点的是**上一局那个 HUD 的按钮**，动作流零新增、
            回合一直不结束（症状像"注入坏了"或"卡住了"）。
          `GetOwningPlayer()` 两者都指向同一个活 PlayerController（区分不出来），
          `Visibility` 也有别的取值混淆 —— 只有 `IsInViewport()` 干净利落。
        """
        cls = self.uclass_of_instance(w)
        if not cls:
            return None
        f = self.find_fn(cls, "IsInViewport", inherited=True)
        if not f:
            return None
        v = self.call_out_u8(w, f)
        return None if v is None else bool(v)

    def hud_actor(self) -> int:
        """当前这一局的 `BP_Widget_Battle_HUD_PC_C` —— **优先走指针链，不扫 GObjects**。

        ★ 2026-09-26 用户问："能避免多 live 问题吗？不扫，而是查找当前对局挂的到底是。"
        实测答案：**能，1 跳就够**（`_nn_scratch/probe_hud.py`）：
            `GetBoard()`（权威单例）→ `BP_Board_C::BattleHUD // 0x0AA0`
            ⇒ 拿到的对象**就是** `BP_Widget_Battle_HUD_PC_C`（与 GObjects 扫描结果同一个地址）。
        为什么这比扫描**更对**（不只是更快）：扫描会拿到**不属于当前对局**的对象 ——
        实测同一时刻 `BP_targetArrowRVX_C` 扫出 2 个，而 `UWorld::Levels` 的**所有** level 里
        一个都没有（上一局残留、GC 未回收）。HUD 同理曾拿到上一局的残留实例。
        退路：链断了（不在对局/字段为空）才退回扫实例 + `IsInViewport` 过滤，并记一笔 notes。
        """
        board = self.board_actor()
        if board:
            bcls = self.uclass_of_instance(board)
            off = self.off(bcls, "BattleHUD", NOTE_OFF_BOARD_BATTLE_HUD)
            hud = self.m.ptr(board + off) or 0
            if hud:
                cn = self.pool.fname_of(self.uclass_of_instance(hud))
                if cn == HUD_CLASS:
                    return hud
                # 少数版本这一跳给的是容器（`BP_Widget_Battle_HUD_C`），再跳一层拿 PC 版
                off2 = self.off(self.uclass_of_instance(hud), "BPWidgetBattleHUDPC",
                                NOTE_OFF_HUDCONTAINER_HUDPC)
                hud2 = self.m.ptr(hud + off2) or 0
                if hud2:
                    return hud2
        ws = self.instances_of_class(HUD_CLASS)
        if not ws:
            return 0
        self.notes.append("BattleHUD 指针链没给到 %s，退回扫实例（可能是上一局残留）" % HUD_CLASS)
        live = [w for w in ws if self.widget_in_viewport(w) is True]
        if live:
            if len(live) > 1:
                self.notes.append("%s 有 %d 个实例在 viewport 里，取第一个" % (HUD_CLASS, len(live)))
            return live[0]
        self.notes.append("%s 的 IsInViewport 都读不出/都为 0，退回第一个实例（可能是上一局残留）"
                          % HUD_CLASS)
        return ws[0]

    # ------------------------------------------------------------ 当前对局的"挂载点"
    def world_levels(self, refresh: bool = False) -> list:
        """`UWorld::Levels // 0x01C8`（`TArray<ULevel*>`）—— **当前世界的全部 level**。

        实测（对局中）：Num=2 —— level[0] 是 PersistentLevel（191 个 actor：Board/板卡…），
        level[1] 是另一个（24 个：**`BP_Deck_C`×2 就在这里**）。
        ⇒ "当前对局挂了哪些东西"要**遍历全部 level**，只看 PersistentLevel 会漏掉牌库那类。
        """
        if refresh or not hasattr(self, "_levels") or self._levels is None:
            w = self.ks.world or 0
            data = self.m.ptr(w + NOTE_OFF_WORLD_LEVELS) if w else 0
            n = self.m.i32(w + NOTE_OFF_WORLD_LEVELS + 8) if w else 0
            out = []
            if data and n and 0 < n < 256:
                raw = self.m.read(data, n * 8) or b""
                out = [int.from_bytes(raw[i * 8:i * 8 + 8], "little") for i in range(n)]
                out = [p for p in out if 0x10000 <= p <= 0x7FFFFFFFFFFF]
            self._levels = out
        return self._levels

    def level_actors(self, refresh: bool = False) -> list:
        """当前世界**全部 level** 的 actor（一次整读 + 一次批量分类）。

        实测：215 个 actor，整读 ~0.1ms、分类 ~5ms ⇒ **~5ms**；
        对比 `instances_of_class()` 每个类名一次 GObjects 全扫（12 万对象）**0.6~0.9s** ⇒ 约 **150×**。
        ★ 而且**更正确**：这里天然不含"上一局残留但已不属于任何 level"的对象。
        """
        if refresh or not hasattr(self, "_actors") or self._actors is None:
            out, by_cls = [], {}
            for lvl in self.world_levels(refresh=refresh):
                data = self.m.ptr(lvl + NOTE_OFF_ULEVEL_ACTORS) or 0
                n = self.m.i32(lvl + NOTE_OFF_ULEVEL_ACTORS + 8) or 0
                if not data or not n or n < 0 or n > 20000:
                    continue
                raw = self.m.read(data, n * 8) or b""
                for i in range(n):
                    a = int.from_bytes(raw[i * 8:i * 8 + 8], "little")
                    if not (0x10000 <= a <= 0x7FFFFFFFFFFF):
                        continue
                    out.append(a)
                    cn = self.pool.fname_of(self.oa.class_of(a) or 0)
                    if cn:
                        by_cls.setdefault(cn, []).append(a)
            self._actors, self._actors_by_cls = out, by_cls
        return self._actors

    def actors_of_class(self, name: str, refresh: bool = False) -> list:
        """当前世界（全部 level）里某个类的 actor —— 替代 `instances_of_class()` 的热路径。

        `instances_of_class()` 仍保留（开局界面/结算页那些不在对局热路径上，
        而且可能真的挂在非 level 的地方），但**对局热路径一律走这个**。
        """
        self.level_actors(refresh=refresh)
        return list(self._actors_by_cls.get(name, []))

    def cache_scope(self) -> int:
        """当前对局的**作用域标识** —— 用它给"解析出来的指针"做缓存失效。

        取 `GetLogic()`：换一局它必然换指针（实测 2 局之间不同）⇒ 缓存自动失效，
        这就是用户说的"跟踪当前对局挂的到底是"的机制，而不是"每次重新扫"。
        """
        return self.singleton("logic") or 0

    def owned_by_current_match(self, obj: int, refresh: bool = False) -> bool:
        """**归属验证**：这个对象是不是"当前这一局挂着的东西"。

        判据（三条，任一成立即算**已证明属于本局**）：
          ① 它在当前世界**全部 level** 的 actor 列表里；
          ② 它就是当前的单例之一（`world` / `GetLogic` / `GetBoard` / `GetGameState`）；
          ③ 它是**已知指针链**上的对象 —— 实测 `BP_Board_C::BattleHUD`（HUD 是 UUserWidget，
             **不在任何 level 里**，只能靠这一条认）。
        ★ 返回 `False` 只表示"现有证据不足以证明它属于本局"，**不等于**"确定是残留" ——
          要坐实残留，判据是"它不在任何 level、也不在任何链上，而 GC 还没回收它"。
        用途：扫描出来的对象先过这一道，避免把上一局的残留当成当前局面。
        """
        if not obj:
            return False
        if obj in (self.ks.world, self.singleton("logic"), self.singleton("board"),
                   self.singleton("game_state"), self.singleton("session"),
                   self.singleton("level")):
            return True
        if obj in set(self.level_actors(refresh=refresh)):
            return True
        # ③ 已知链：Board→BattleHUD（含少数版本的多一跳容器→BPWidgetBattleHUDPC）
        if self.hud_actor() == obj:
            return True
        board = self.singleton("board")
        if board:
            off = self.off(self.uclass_of_instance(board), "BattleHUD", NOTE_OFF_BOARD_BATTLE_HUD)
            if (self.m.ptr(board + off) or 0) == obj:
                return True
        return False


    def _sync_match(self) -> None:
        """换了一局就把"对局级"缓存全丢掉（`_board`/`_deck`/`_logic`/`_ml`）。

        ★ 2026-09-30 实机（第二局起）：`board_actor()` 把 `_board` 缓存下来就再不刷新，
          第二局读到的是**上一局的棋盘**——`isSelectingHandTarget` 读 False、
          `selectHandTargetWidget` 读 0（`hand_target_pending` 全程报"不在等"，而游戏自己的
          `pick_pending` 明明在等），换牌窗口也判不到（`deck_actor` 同理）⇒ 换牌总超时、
          手牌选择卡死。判据：`GetBoard()`（游戏自己的单例访问器）给的指针变了 ⇒ 换局了。
          1 秒内不重复问（一次注入调用，很便宜）。
        """
        now = time.time()
        if now - getattr(self, "_sync_t", 0.0) < 1.0:
            return
        self._sync_t = now
        cur = self.singleton("board") or 0
        prev = getattr(self, "_sync_board", 0)
        if cur and prev and cur != prev:
            self._board = None
            self._deck = None
            self._logic = None
            self._ml = None
            self.notes.append("换局了（GetBoard %s -> %s）：丢掉 board/deck/logic/matchlog 缓存"
                              % (hex(prev), hex(cur)))
        if cur:
            self._sync_board = cur

    def board_actor(self, refresh: bool = False) -> int:
        """当前这一局的 `BP_Board_C` —— **优先游戏自己的单例访问器**（`GetBoard`）。

        ★ 用户 2026-09-26 定调："不要扫实例（多 live 实例几乎无解），跟踪这样的单例。"
          所以这里先 `UUtilityFunctions_C::GetBoard()`；只有它拿不到（世界没起来/不在对局）
          才退回扫实例，并**记一笔 notes**（让它可见，别静默降级）。
        用来读 `isSelectingHandTarget`（游戏自己的"手牌正在选目标"标志）。
        """
        self._sync_match()
        if refresh or self._board is None:
            got = self.singleton("board") or 0
            if not got:
                ws = self.instances_of_class("BP_Board_C")
                got = ws[0] if ws else 0
                if got:
                    self.notes.append("GetBoard() 没给指针，退回扫实例拿 BP_Board_C")
            self._board = got
        return self._board

    def end_turn_button(self, hud: int) -> int:
        off = self.off(self.uclass_of_instance(hud), "EndTurnButton", NOTE_OFF_HUD_END_TURN_BTN)
        return self.m.ptr(hud + off) or 0

    # ------------------------------------------------------------ 统一拖拽原语
    # ★★ 2026-09-26 重构（依据：`_nn_scratch/ida-attack-report.md` + `history-selectedcard.md`）
    #
    #   证据：`AddMoveToPlayerMoveQueue(sourceCard, actionType, …)` 的全部调用点里，
    #   **出牌 `"playCardFromHand"`（BP_BaseCard.cpp:1012）/ 移动 `"moveCard"`（:1147）/
    #   攻击 `"attackCard"`（:1164）在同一个文件、同一个落地函数里按条件分流**
    #   ⇒ 这不是三条链，是**一条链 + 一个 actionType 参数**。于是分四层：
    #
    #     L0 队列闸门：`BP_PlayerMoves_C::queueIsRunning`（`BP_PlayerMoves.cpp:254`）
    #        —— 队列在跑时动作**只入队不解析、零动作零提示、>30 s 才强制 resume**。
    #        对**所有**入队动作都成立（出牌/移动/攻击/选牌/选目标/状态同步）。
    #     L1 PC 拖拽状态机：`SetMouseDownActorManual(actor)` 一次写 5 个字段
    #        （dragActor/mouseDownActor/leftButtonDown/oldLeftButtonDown/dragStarted），
    #        收尾 `ForceReleaseDrag()` + `ForceEndMouseOver()`。**全通用**。
    #        ⚠ 不要写 `SelectedCard` —— 那是**手柄**字段（读点全在 `IsGamepad` 判定之后），
    #        鼠标拖拽用上面那 5 个；poke 它正是 2026-09-26 把游戏打崩的原因（§22.10/§22.10a）。
    #     L2 被拖对象：cursor 组 `cardUnderCursor 0x3EC` / `LocationNumberUnderCursor 0x664` /
    #        `LocationUnderCursor 0x668` / `RowUnderCursor 0x669`（在**被拖的 actor** 上）；
    #        选目标类动作还要写箭头 `overCardID`（+ 游戏自己的 `spectatorArrowNewTarget`）。
    #     L3 落地口：**按子类分** —— 手牌 `OnActorEndDrag`；板卡 `OnActorMouseUp`；
    #        "选目标"根本不是拖拽，是单击（`BP_Logic::GlobalMouseUp`）。
    def moves_actor(self) -> int:
        """`BP_PlayerMoves_C`（动作队列在这里）。走 level actor 快路径，不扫 GObjects。"""
        rows = self.actors_of_class("BP_PlayerMoves_C")
        return rows[0] if rows else 0

    def queue_running(self):
        """`queueIsRunning`（**按名字反射取偏移，不硬编码**）→ True/False/None(读不出)。

        `BP_PlayerMoves.cpp:254`：`if(!queueIsRunning) ResolvePlayerMoveQueue(); return;`
        ⇒ true 时提交动作**只入队、当场不解析、不产生动作、不发提示**，且不超时。
        这是"同一时刻出牌成功、攻击零新增"的通用解释。
        """
        mv = self.moves_actor()
        if not mv:
            return None
        try:
            from kardsmem import props
            pr = props.find_prop(self.ks, self.uclass_of_instance(mv), "queueIsRunning",
                                 pool=self.pool)
            if not pr:
                self.notes.append("BP_PlayerMoves_C 上没找到 queueIsRunning（反射链）")
                return None
            v = self.m.u8(mv + pr["offset"])
            return None if v is None else bool(v)
        except Exception as e:                                 # noqa: BLE001
            self.notes.append("queue_running 读失败：%s" % e)
            return None

    def wait_queue_idle(self, timeout: float = 4.0, poll: float = 0.1) -> bool:
        """等动作队列空闲（L0 闸门）。空闲返回 True；超时返回 False 并记 `notes`。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            q = self.queue_running()
            if q is False:
                return True
            if q is None:
                return True            # 读不出 ⇒ 不拦（保持旧行为，别拿判据否决动作）
            time.sleep(poll)
        self.notes.append("queueIsRunning 在 %.1fs 内一直为 true（动作可能只入队不解析）" % timeout)
        return False

    def pc_drag_state(self) -> dict:
        """PC 上**鼠标**拖拽用的 5 个字段当前值（只读，排查用）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        out = {"pc": hex(pc)}
        for name, note in (("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR),
                           ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR),
                           ("dragActor", NOTE_OFF_PC_DRAG_ACTOR),
                           ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN),
                           ("dragStarted", NOTE_OFF_PC_DRAG_STARTED)):
            try:
                off = self.off(pcls, name, note)
                v = self.m.ptr(pc + off) if "Actor" in name else self.m.u8(pc + off)
                out[name] = hex(v) if v else 0
            except Exception:                                  # noqa: BLE001
                out[name] = None
        return out

    def _pc_writes(self, actor: int):
        """L1：把 PC 的拖拽 5 字段写成"正在拖 actor"（可塞进同一次 JS 执行）。

        ★ 为什么不只调 `SetMouseDownActorManual`：`OnActorStartDrag` 的 `success` 出参
          **只有一条路径写 true**，我们传 0 ⇒ 游戏读到 false ⇒ PC 会把 `mouseDownActor`
          清空（报告 §5）。所以**在提交那一次执行里再写一遍**最稳。
        """
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        w = []
        for name, note, kind, val in (
                ("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR, "ptr", actor),
                ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR, "ptr", actor),
                ("dragActor", NOTE_OFF_PC_DRAG_ACTOR, "ptr", actor),
                ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN, "u8", 1),
                ("dragStarted", NOTE_OFF_PC_DRAG_STARTED, "u8", 1)):
            try:
                w.append((pc, self.off(pcls, name, note), kind, val))
            except Exception:                                  # noqa: BLE001
                pass
        return w

    def pc_drag_begin(self, actor: int, verbose: bool = False) -> dict:
        """L1：让 PC 进入"正在拖 actor"（调游戏自己的 `SetMouseDownActorManual`）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f = self.find_fn(pcls, "SetMouseDownActorManual")
        out = {"pc": hex(pc), "setter": bool(f)}
        if f:
            out["ret"] = self.call_ptr(pc, f, actor)
        else:
            self.notes.append("PC 上没有 SetMouseDownActorManual ⇒ 只写字段兜底")
        if verbose:
            print("    L1 pc_drag_begin: setter=%s  %s" % (bool(f), out))
        return out

    def pc_clear_hover(self, verbose: bool = False) -> dict:
        """动作收尾：把 PC 的「悬停/按下/拖拽」指针全部写回 0（**只写零，不调用任何函数**）。

        ★ 2026-09-30 崩溃现场（`kards+0x11d37e6` / `+0x11ad577`，多播委托清理里对已释放
          对象做虚调用，虚表指针成了 0x1e3020030 / 0x100000000）+ `click-crash-report.md` §3：
          游戏每帧用 `oldMouseOverActor`（上一帧的 mouseOverActor）和 `mouseOverActor` 比较来发
          进入/离开事件。我们悬停的目标单位一旦被这次动作打死销毁，这两个字段就成了悬空指针，
          下一帧游戏对已释放对象发事件 ⇒ 崩。鼠标不在任何东西上时游戏自己写的就是 0，
          所以写零不是伪造状态。
        ⛔ 不用 `ForceEndMouseOver()`：它会对当前悬停对象发「离开」事件——对象已释放时正好踩雷
          （同 `click_actor` 里不调 `pc_drag_end` 的理由）。
        """
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        writes = []
        for name, note, kind in (
                ("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR, "ptr"),
                ("oldMouseOverActor", NOTE_OFF_PC_OLD_MOUSE_OVER_ACTOR, "ptr"),
                ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR, "ptr"),
                ("dragActor", NOTE_OFF_PC_DRAG_ACTOR, "ptr"),
                ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN, "u8"),
                ("dragStarted", NOTE_OFF_PC_DRAG_STARTED, "u8")):
            try:
                writes.append((pc, self.off(pcls, name, note), kind, 0))
            except Exception:                                  # noqa: BLE001
                pass
        back = self.write_and_call(writes) if writes else []
        out = {"pc": hex(pc), "n": len(writes), "back": back}
        if verbose:
            print("    pc_clear_hover:", out)
        return out

    def pc_drag_end(self, verbose: bool = False) -> dict:
        """L1 收尾：`ForceReleaseDrag()` + `ForceEndMouseOver()`（幂等，清残留）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        out = {"pc": hex(pc)}
        for name in ("ForceReleaseDrag", "ForceEndMouseOver"):
            f = self.find_fn(pcls, name)
            out[name] = bool(f)
            if f:
                try:
                    self.call0(pc, f)
                except Exception as e:                         # noqa: BLE001
                    out[name + "_err"] = str(e)
        if verbose:
            print("    L1 pc_drag_end:", out)
        return out

    def drag_release(self, actor: int, *,
                     cursor: Optional[dict] = None,
                     arrow_target: Optional[int] = None,
                     arrow_length_gate: bool = False,
                     hover_other: Optional[int] = None,
                     commit: str = "end_drag",
                     hover_pause: Optional[float] = None,
                     prime_arrow: bool = False,
                     arrow_setter: bool = True,
                     queue_gate: bool = True,
                     cleanup: bool = True,
                     pre_commit=None,
                     verbose: bool = True) -> dict:
        """★★ **统一的"拖拽并松手"原语**（L0+L1+L2 共用，L3 由 `commit` 指定）。

        * `cursor`：`{"card_under_cursor": id, "location_number": n, "location_enum": e, "row": r}`
          —— 只写给了的项（L2，写在**被拖的 actor** 上）。
        * `arrow_target`：给了就写箭头（`overCardID` + `overCard` + 头部平面搬远），
          并默认先调游戏自己的 `spectatorArrowNewTarget(target)`。
        * `commit`：`"end_drag"`（手牌）/ `"mouse_up"`（板卡）/ `"both"` / `"none"`。
        * `prime_arrow`：没有箭头时空拖一次把它生出来（攻击必需）。
        * `queue_gate`：先等 `queueIsRunning==false`（L0）。
        """
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        out = {"actor": hex(actor), "commit": commit}

        # ★ 2026-09-26 二分开关（首次实机回归排查用）：任一层都能单独关掉，
        #   用来判断"某条拖拽失败"到底是新加的哪一层引入的。
        #     KARDS_DRAG_L0=0 关队列闸门；KARDS_DRAG_L1=0 关 PC 拖拽状态机（退回重构前行为）
        #     KARDS_DRAG_L3=both|mouse_up|end_drag  覆盖落地口
        import os as _os
        l0 = _os.environ.get("KARDS_DRAG_L0", "1") != "0"
        l1 = _os.environ.get("KARDS_DRAG_L1", "0") != "0"   # ★ 默认 **0**：L1 未验证前退回已验证行为
        l3 = _os.environ.get("KARDS_DRAG_L3")
        if l3:
            commit = l3
            out["commit"] = commit

        # ---- L0：队列闸门 ----
        if queue_gate and l0:
            out["queue_idle"] = self.wait_queue_idle()
        # ---- L1 起手：清残留拖拽态 ----
        if cleanup and l1:
            self.pc_drag_end()
        # ---- 箭头预备（攻击：同一个拖拽里刚 Spawn 的箭头游戏不认目标）----
        # ★ 2026-10-01 用户：攻击"主要延迟在箭头出现、在目标上松手"，真人≤5 s、熟练者<3 s。
        #   旧做法 = 先整套空拖一次（悬停+按下+起拖+EndDrag+等 1 s）把箭头"预热"出来，再来一次真拖 ⇒ 两遍手势。
        #   新做法（默认）：只拖一次，起拖后**等箭头 actor 真的出现 + 至少几帧**再悬停目标/落地；
        #   失焦降帧下靠帧数兜底。KARDS_ATTACK_PRIME=1 退回旧的两遍做法。
        fast_prime = prime_arrow and _os.environ.get("KARDS_ATTACK_PRIME", "0") != "1"
        if prime_arrow and not fast_prime and not self.arrow_actors():
            self._drag_lead(actor, cls, pc, pc_cls)
            try:
                self.call0(actor, self.fn(cls, "OnActorEndDrag", inherited=True))
            except Exception:                                  # noqa: BLE001
                pass
            self.settle(1.0, 6)       # KARDS_ATTACK_PRIME=1 的旧两遍做法：等箭头真的生成 ⇒ 按帧等，失焦降帧时不空等
            out["primed"] = True
        # ---- L1 悬停 + 转发（红线：悬停必须发生）----
        self.call0(actor, self.fn(cls, "OnActorMouseEnter", inherited=True))
        self.settle(SETTLE_HOVER if hover_pause is None else hover_pause, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch", inherited=True), actor)
        out["hover_dispatched"] = True
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ---- L1 官方 setter ----
        if l1:
            out["pc_begin"] = self.pc_drag_begin(actor)
        # ---- 按下 / 起拖 / 拖动 tick ----
        self.call0(actor, self.fn(cls, "OnActorMouseDown", inherited=True))
        self.settle(SETTLE_AFTER_DOWN, 2)
        f_start = self.fn(cls, "OnActorStartDrag", inherited=True)
        out["drag_success"] = self.call_ptr(actor, f_start, 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, self.fn(cls, "OnActorDragTick", inherited=True))
        self.settle(SETTLE_AFTER_TICK, 2)
        if fast_prime:
            atk_obj_f = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            t_arrow = time.time()
            while time.time() - t_arrow < 1.5 and not (atk_obj_f and self._arrow_for(atk_obj_f)):
                time.sleep(0.02)
            out["arrow_wait"] = round(time.time() - t_arrow, 3)
            self.settle(0.05, 3)                                   # 箭头生成后至少再走几帧（它自己的 tick）
        # ---- 目标 actor 悬停（真实玩家把箭头拖到目标上方；红线：这一步不能省）----
        if hover_other:
            ocls = self.uclass_of_instance(hover_other)
            f_oe = self.find_fn(ocls, "OnActorMouseEnter")
            if f_oe:
                self.call0(hover_other, f_oe)
                self.settle(SETTLE_HOVER, 6)
            self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch", inherited=True), hover_other)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
            out["hover_other"] = hex(hover_other)
        if pre_commit is not None:                                 # 提交前的只读诊断钩子（不写任何东西）
            try:
                out["pre_commit"] = pre_commit()
            except Exception as e_pc:                              # noqa: BLE001
                out["pre_commit_err"] = str(e_pc)
        # ---- L2：写 cursor + 箭头（与提交同一次 JS 执行）----
        writes = list(self._pc_writes(actor)) if l1 else []     # L1 再写一次（防 StartDrag 清空）
        for key, note in (("card_under_cursor", NOTE_OFF_CARD_UNDER_CURSOR),
                          ("location_number", NOTE_OFF_LOCATION_NUMBER),
                          ("location_enum", NOTE_OFF_LOCATION_ENUM),
                          ("row", NOTE_OFF_ROW_UNDER_CURSOR)):
            if cursor and key in cursor and cursor[key] is not None:
                kind = "s32" if key in ("card_under_cursor", "location_number") else "u8"
                writes.append((actor, self.off(cls, CURSOR_FIELD[key], note), kind,
                               int(cursor[key])))
        calls = []
        # ★ 2026-09-26：**没有目标卡、但要过长度闸门**（移动/上线）——
        #   板卡提交前 `IsTargetArrowLengthValid()` 要求 owner 箭头长度 > 3000，
        #   而 `OnActorMouseUp` 会先销毁箭头（长度在销毁时按 owner 卡回写）
        #   ⇒ 必须把 owner 箭头头部平面搬远 + 写回长度（同一次 JS 执行）。
        if arrow_target is None and arrow_length_gate:
            atk_obj_g = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            own_g = self._arrow_for(atk_obj_g) if atk_obj_g else 0
            out["gate_arrow"] = hex(own_g) if own_g else None
            if own_g:
                # ★ 2026-10-01（回合 9 实机：5th RANGERS 刚攻击完再上线被拒，落地后 `cardUnderCursor` 成了 41）：
                #   `OnActorMouseUp` 会用 `GetTargetArrowTargetCard` 读箭头的 `overCardID` 覆盖 `cardUnderCursor`，
                #   而箭头上还留着上一次攻击的目标 ⇒ 移动被当成"打 41"。移动/上线没有目标 ⇒ 把箭头的目标清零
                #   （同一次 JS 执行里写，和提交原子）。
                try:
                    acls_g = self.uclass_of_instance(own_g)
                    writes.append((own_g, self.off(acls_g, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID), "s32", 0))
                    writes.append((own_g, self.off(acls_g, "overCard", NOTE_OFF_ARROW_OVER_CARD), "ptr", 0))
                    out["gate_arrow_target_cleared"] = True
                except Exception as e_g:                       # noqa: BLE001
                    out["gate_arrow_clear_error"] = str(e_g)
                writes.append((actor, self.off(cls, "targetArrowFinalLength",
                                               NOTE_OFF_ARROW_FINAL_LENGTH), "f64", 4000.0))
                head_g = self.arrow_head_plane(own_g)
                if head_g:
                    f_rel_g = self.find_fn(self.uclass_of_instance(head_g),
                                           "K2_SetRelativeLocation")
                    if f_rel_g:
                        parms_g = bytearray(0x128)
                        struct.pack_into("<ddd", parms_g, 0x00, 4000.0, 0.0, 0.0)
                        calls.append([hex(head_g), hex(f_rel_g), bytes(parms_g).hex()])
                        out["gate_head_moved"] = hex(head_g)
        if arrow_target is not None:
            arrows = self.arrow_actors()
            if arrow_setter and arrows:
                try:
                    self.call_i32(arrows[0], self.fn(self.uclass_of_instance(arrows[0]),
                                                     "spectatorArrowNewTarget"),
                                  int(arrow_target))
                    out["arrow_setter"] = True
                except Exception as e:                         # noqa: BLE001
                    out["arrow_setter_error"] = str(e)
            tgt_actor = self.board_actor_of(arrow_target)
            tgt_obj = self.m.ptr(tgt_actor + NOTE_OFF_ACTOR_SELF_BASECARD) if tgt_actor else 0
            for a in (arrows or []):
                acls = self.uclass_of_instance(a)
                writes.append((a, self.off(acls, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID),
                               "s32", int(arrow_target)))
                if tgt_obj:
                    writes.append((a, self.off(acls, "overCard", NOTE_OFF_ARROW_OVER_CARD),
                                   "ptr", tgt_obj))
                # ★ 2026-10-01：真悬停时箭头自己的 tick 会把 `BoardCardUnderTargetArrow`（`BP_BoardCard_C*`，BP_targetArrowRVX.cpp:56/:776-804）
                #   设成目标的**视觉板卡 actor**；我们只写了 overCardID/overCard，这一项一直是 null。
                #   只在**反射链真的查得到**这个字段时才写（查不到不乱写偏移）。
                if tgt_actor:
                    try:
                        from kardsmem import props as _props
                        _pr = _props.find_prop(self.ks, acls, "BoardCardUnderTargetArrow", pool=self.pool)
                        if _pr and _pr.get("offset"):
                            writes.append((a, int(_pr["offset"]), "ptr", tgt_actor))
                            out["arrow_under_written"] = True
                    except Exception:                              # noqa: BLE001
                        pass
            out["arrows_written"] = len(arrows)
            # ★ 2026-09-26（`drag-chain-report.md` §6）：搬头平面/写长度必须认**owner 卡的那支箭头**。
            #   长度是在箭头销毁时按 **owner 卡**回写的（`BP_targetArrowRVX.cpp:953-955`），
            #   拿 `arrows[0]` 可能搬到别人的箭头 ⇒ `IsTargetArrowLengthValid` 过不了、提交静默失败。
            atk_obj_x = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            owner_arrow = self._arrow_for(atk_obj_x) if atk_obj_x else 0
            head_arrow = owner_arrow or (arrows[0] if arrows else 0)
            out["head_arrow"] = hex(head_arrow) if head_arrow else None
            out["owner_arrow_matched"] = bool(owner_arrow)
            head = self.arrow_head_plane(head_arrow) if head_arrow else 0
            if head:
                f_rel = self.find_fn(self.uclass_of_instance(head), "K2_SetRelativeLocation")
                if f_rel:
                    parms = bytearray(0x128)
                    struct.pack_into("<ddd", parms, 0x00, 4000.0, 0.0, 0.0)
                    calls.append([hex(head), hex(f_rel), bytes(parms).hex()])
                    writes.append((actor, self.off(cls, "targetArrowFinalLength",
                                                   NOTE_OFF_ARROW_FINAL_LENGTH), "f64", 4000.0))
                    out["head_plane_moved"] = hex(head)
        # ★ 2026-10-01（`x_pre_commit` 对照）：攻击**成功**的提交前 PC.mouseOverActor = 目标（合成悬停还挂着），
        #   **失败**的提交前它已是 0 —— 真鼠标不在目标上时，游戏自己的帧 tick 会把它清掉（帧数等待让更多 tick 跑过）。
        #   真鼠标停在目标上就能打正是这个原因 ⇒ 在提交的同一次 JS 执行里把 PC.mouseOverActor 写回目标，
        #   提交时恒为"悬停在目标上"。（动作后 `pc_clear_hover` 仍会清零，不留悬空指针。）
        if hover_other:
            try:
                writes.append((pc, self.off(pc_cls, "mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR),
                               "ptr", int(hover_other)))
                out["pc_hover_rewritten"] = hex(int(hover_other))
            except Exception as e_h:                               # noqa: BLE001
                out["pc_hover_rewrite_err"] = str(e_h)
        # ---- L3：落地口 ----
        # ★ 2026-09-26（`drag-chain-report.md` §1/§2）：落地口**按子类**，而 `BP_BaseCard`
        #   自己的 `OnActorMouseUp` 是**空壳**（BP_BaseCard.cpp:1186-1187）。
        #   `inherited=True` 有可能解析到基类那个空壳 ⇒ 调了等于没调（症状正是
        #   "预检全过、箭头也设了、动作流零新增、无提示"）⇒ **先认 actor 自己类上的实现**。
        f_up = (self.find_fn(cls, "OnActorMouseUp", inherited=False)
                or self.find_fn(cls, "OnActorMouseUp", inherited=True))
        f_end = (self.find_fn(cls, "OnActorEndDrag", inherited=False)
                 or self.find_fn(cls, "OnActorEndDrag", inherited=True))
        out["commit_fns_own_class"] = {
            "mouse_up": bool(self.find_fn(cls, "OnActorMouseUp", inherited=False)),
            "end_drag": bool(self.find_fn(cls, "OnActorEndDrag", inherited=False))}
        seq = []
        if commit == "both":
            seq = [f for f in (f_up, f_end) if f]
        elif commit == "mouse_up":
            seq = [f for f in (f_up,) if f] or [f for f in (f_end,) if f]
        elif commit == "end_drag":
            seq = [f for f in (f_end,) if f] or [f for f in (f_up,) if f]
        for f in seq:
            calls.append([hex(actor), hex(f), ""])
        if cleanup and l1:
            f_rel_pc = self.find_fn(self.uclass_of_instance(pc), "ForceReleaseDrag")
            if f_rel_pc:
                calls.append([hex(pc), hex(f_rel_pc), ""])
        out["commit_calls"] = [hex(f) for f in seq]
        out["layers"] = {"L0": l0, "L1": l1, "L3": commit}
        jw = [[hex(int(o)), int(off), kind, (hex(int(v)) if kind == "ptr" else int(v))]
              for (o, off, kind, v) in writes]
        if jw or calls:
            out["writeback"] = self.api().writeThenCalls(jw, calls)
        out["after"] = self.pc_drag_state()
        if verbose:
            print("    drag_release commit=%s seq=%s cursor=%s arrow=%s queue_idle=%s"
                  % (commit, out["commit_calls"], cursor, arrow_target, out.get("queue_idle")))
        return out

    def click_actor(self, actor: int, *, is_precise: int = 1,
                    queue_gate: bool = True, hover_pause: Optional[float] = None,
                    actor_mouse_up: bool = True, cleanup: bool = True,
                    verbose: bool = True) -> dict:
        """★★ **统一点击原语** —— 复刻 PC 自己的真实松手流程
        （`BP_PlayerController.cpp:683-748`，逐行）：

            683: `BP_Logic::GlobalMouseUp(mouseDownActor, &bWasConsumed)`
            687: `if (!bWasConsumed) {`
            705:     `actor->OnActorMouseUp()`
            748:     `actor->OnActorClicked(IsPrecise)`
               `}`

        ⇒ **点击 ≠ 直接调 `OnActorClicked`**：先要 `GlobalMouseUp(mouseDownActor)`，
        只有它**没吃掉**这次点击（`consumed == false`）时才转发给 actor。
        参数是 **`mouseDownActor`**（L1 那个字段）⇒ 所以点击同样需要 L1 的 PC 状态。

        分层：L0 队列闸门 → 悬停(+转发) →（可选 `KARDS_CLICK_PC=1`）`GlobalMouseUp`
        → `OnActorClicked(IsPrecise)`。⛔ **不碰任何 PC 鼠标字段/手柄 API**（用户定调 + 事故根因）。
        用于：抉择/预报候选、选手牌当目标、换牌标记……（widget 按钮另走它们的 `BndEvt__*`）。
        """
        if not self.obj_alive(actor):                              # 对已销毁对象调 ProcessEvent 会 AV（见 obj_alive）
            return {"actor": hex(actor), "ok": False, "reason": "actor_not_alive",
                    "error": "目标 actor 已被销毁/回收（陈旧候选），不点"}
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        logic = self.logic_actor()
        lcls = self.uclass_of_instance(logic)
        out = {"actor": hex(actor)}
        # L0
        if queue_gate:
            out["queue_idle"] = self.wait_queue_idle()
        # ⛔ 不调 `pc_drag_end()`：它内部 `ForceReleaseDrag()` 会额外触发一次 EndDrag
        #   （候选卡点完即自毁 ⇒ 撞成 use-after-free，见 `_nn_scratch/click-crash-report.md`）。
        # L1：悬停（红线：悬停必须发生）+ 转发
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        if f_enter:
            self.call0(actor, f_enter)
            self.settle(SETTLE_HOVER if hover_pause is None else hover_pause, 6)
            out["hovered"] = True
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        if f_disp:
            self.call_ptr(pc, f_disp, actor)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
            out["hover_dispatched"] = True
        # ★ 2026-09-26：**新点击流程默认关**（曾疑似把游戏点崩）—— 默认走旧行为
        #   （只 hover + 转发 + `OnActorClicked`）；要试 PC 真实流程设 `KARDS_CLICK_PC=1`。
        import os as _os2
        pc_flow = _os2.environ.get("KARDS_CLICK_PC", "0") == "1"
        out["pc_flow"] = pc_flow
        out["gamepad_api_used"] = False       # ★ 用户定调：点击不用 gamepad/手柄 API
        # ⛔ 不调 `SetMouseDownActorManual`（手柄 API）、不写 PC 鼠标字段、不调 `OnActorMouseDown`：
        #   伪造 leftButtonDown 会让游戏自己补一次 EndDrag，和我们的清理撞成两次 ⇒ use-after-free。
        # 同一次执行：写 PC 拖拽字段 + GlobalMouseUp(actor) [+ actor.OnActorMouseUp()]
        f_gmu = self.find_fn(lcls, "GlobalMouseUp") or self.find_fn(lcls, "GlobalMouseUpBattle")
        f_up = self.find_fn(cls, "OnActorMouseUp")
        writes = []          # ★ 点击链不写任何 PC 字段（只用 GlobalMouseUp 的问询语义）
        jw = []
        consumed = None
        if f_gmu and pc_flow:
            parms = bytearray(0x10)
            struct.pack_into("<Q", parms, 0x00, int(actor))      # mouseDownActor @0x00
            try:
                buf = self.api().call_raw_writes(writes, logic, f_gmu, bytes(parms))
                out["gmu_writeback"] = buf
                consumed = bool(buf[0x08]) if buf and len(buf) > 0x08 else None
            except Exception as e:                              # noqa: BLE001
                out["gmu_error"] = str(e)
        else:
            out["gmu_missing"] = True
        out["consumed"] = consumed
        # 未被消费 ⇒ 按真实输入转发给 actor（MouseUp → 再 Clicked）
        if actor_mouse_up and f_up and consumed is not True:
            try:
                self.call0(actor, f_up)
                self.settle(0.08, 4)  # 松开→Clicked 之间的按下态要渲染出来 ⇒ 按帧等（失焦降帧时旧写法会一帧都不画）
            except Exception as e:                              # noqa: BLE001
                out["up_error"] = str(e)
        if consumed is not True:
            f_click = self.find_fn(cls, "OnActorClicked")
            if f_click:
                self.call_i32(actor, f_click, int(is_precise))
                out["clicked"] = True
                self.settle(0.35, 6)  # 点击后等界面反应 ⇒ 按帧等（降帧时 0.35 s 里可能一帧没跑）
            else:
                out["click_missing"] = True
        if verbose:
            print("    click_actor 0x%X consumed=%s clicked=%s queue_idle=%s gamepad_api=False"
                  % (actor, out.get("consumed"), out.get("clicked"), out.get("queue_idle")))
        return out

    def _drag_lifecycle(self, actor: int, location_enum: int, location_number: int,
                        card_at_location: int = 0, verbose: bool = True,
                        hover_pause: Optional[float] = None,
                        use_mouse_up: bool = False,
                        also_mouse_up: bool = False) -> dict:
        """复刻真实鼠标拖拽：悬停 → 悬停转发 → 按下 → 起拖 → 拖动 tick → 写落点 → 落地。

        `hover_pause` 给了就用它当"在牌上停多久"（默认 `SETTLE_HOVER`）——
        想看 hover 效果时把它调大，眼睛跟得上。

        ★ `use_mouse_up=True` 用于**板卡**（移动/上线）：这类 actor 的提交口是
          **`OnActorMouseUp`（松手）**，不是 `OnActorEndDrag`（那是手牌那条，已证）。
          2026-09-25 实测：用 EndDrag 提交移动时，读回 `targetArrowFinalLength=0.0`、
          `IsTargetArrowLengthValid=0` —— 落点根本没人消费，游戏一点反应都没有。
          并且"写落点 + 松手"必须塞进**同一次 JS 执行**（`write_and_call`），
          否则中间插帧会把 cursor 字段按真实鼠标重算掉（和箭头那次同一个竞态）。

        ★ 2026-09-26 重构：本体**委托给统一原语 `drag_release()`**（L0 队列闸门 + L1 PC 拖拽
          状态机 + L2 cursor 组 + L3 落地口）。旧签名与返回键保留，调用方不用改。
          出牌/移动由此自动获得：`queueIsRunning` 闸门、`SetMouseDownActorManual`、
          `ForceReleaseDrag` 收尾、以及提交前再写一遍 PC 拖拽字段。
        """
        out = self.drag_release(
            actor,
            cursor={"card_under_cursor": card_at_location,
                    "location_number": location_number,
                    "location_enum": location_enum, "row": 1},
            commit=("mouse_up" if use_mouse_up else "end_drag"),
            arrow_length_gate=bool(use_mouse_up),   # 板卡（移动/上线）要过长度闸门
            # ★ 2026-09-26 更正（`drag-chain-report.md` §4 抓到的错）：这里原来写死
            #   `commit="both"`，注释还说"与重构前一致"——**不实**。老版
            #   （`_nn_scratch/old_ops/inject.py`）手牌只发 `OnActorEndDrag`。
            #   而板卡的 `OnActorMouseUp` 在 `PlaceBoardCard()` 前有双闸
            #   `CanPlayCard() && IsTargetArrowLengthValid()`（BP_BoardCard.cpp:1404-1411），
            #   且 :1402 会**先把箭头全销毁** ⇒ 没有箭头/长度时它静默早退，
            #   再补一个 `OnActorEndDrag` 也救不回。⇒ 按子类给落地口：
            #   手牌 `end_drag`（老版行为）、板卡 `mouse_up`（+ 必须给 `arrow_target`）。
            hover_pause=hover_pause,
            verbose=verbose)
        # 兼容旧返回键
        out["commit_fn"] = ("OnActorMouseUp+OnActorEndDrag" if len(out.get("commit_calls") or []) > 1
                            else ("OnActorMouseUp" if out.get("commit_calls")
                                  else "（找不到任何提交函数）"))
        out["cursor_state"] = {
            "cardUnderCursor": self.peek(actor, self.off(self.uclass_of_instance(actor),
                                                         "cardUnderCursor",
                                                         NOTE_OFF_CARD_UNDER_CURSOR), "s32"),
            "LocationNumberUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "LocationNumberUnderCursor",
                NOTE_OFF_LOCATION_NUMBER), "s32"),
            "LocationUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "LocationUnderCursor",
                NOTE_OFF_LOCATION_ENUM), "u8"),
            "RowUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "RowUnderCursor",
                NOTE_OFF_ROW_UNDER_CURSOR), "u8"),
        }
        return out

    # ------------------------------------------------------------ 回执
    def matchlog(self, refresh: bool = False):
        """动作流读取器 —— **长寿命单例**（`_mark()`/`_receipt()` 共用）。

        ★★ 2026-09-26 用户："动作为什么有点慢。" 实测（`_nn_scratch/probe_timing*.py`）：

        | 组件 | 冷 | 同实例第二次 |
        |---|---|---|
        | `MatchLog.locate()` | **4.4~5.8 s**（要扫 GObjects 找动作流容器） | **0 ms** |
        | `_mark()` | 4.2 s | **4.3 s** ← 每次都新建实例，等于每次都重扫 |
        | `_receipt(timeout=0.2)` | 14.6 s | **6.5 s** ← `timeout` 形同虚设 |

        ⇒ 一次两阶段出牌原本要多付 **9~15 s** 在"重新定位动作流"上，这就是"慢"的主因。
        修法：实例缓存 + **每次调用都验证新鲜度**。

        ★★ 2026-09-26 用户追问："那么怎么确保这个东西是最新的呢？" 答案分两半：

        * **对象指针：每次现算，绝不长期缓存。** 权威链是
          `GetLogic()` → `BP_Logic_C::onlineMatch // 0x0810`（`BP_Logic_classes.hpp:130`）
          —— `GetLogic` 是游戏自己的单例访问器，**换局必然换指针** ⇒ 每次拿到的都是当前这一局。
          成本 ≈ 2 次跨进程读（`logic_actor()` 本身已是缓存）。
        * **偏移表：可以永久缓存。** 4 个数组字段的偏移只依赖类布局（同一构建不变）。
        * 再加两道**便宜**的兜底（链断了/不在对局时也安全）：
          `MatchLog.is_stale()`（对象头 class 变了 / `AllMatchActions` 计数不单调）
          ⇒ `forget()` + 重新定位（`locate()` 现在先走 `ULevel::Actors` 快路径 ~5 ms，
          不再动辄 4~6 s 全扫）。
        """
        from kardsmem.matchlog import MatchLog
        ml = getattr(self, "_ml", None)
        if ml is None or refresh:
            ml = MatchLog(self.ks)
            self._ml = ml
            self._ml_located = False
        # ① 权威链现算"当前这一局的 OnlineMatch"；与缓存不同 ⇒ 换对象（pin 只在类变了才重算偏移）
        try:
            logic = self.logic_actor()
            if logic:
                off = self.off(self.uclass_of_instance(logic), "onlineMatch",
                               NOTE_OFF_LOGIC_ONLINE_MATCH)
                cur = self.m.ptr(logic + off) or 0
                if cur and cur != ml.obj:
                    self.notes.append("MatchLog：权威链给的对象变了 0x%X → 0x%X，换之"
                                      % (ml.obj, cur))
                    ml.pin(cur)
        except Exception as e:                                 # noqa: BLE001
            self.notes.append("MatchLog：权威链取 onlineMatch 失败（%s）" % e)
        # ② 兜底：链没给或给了假值 ⇒ 用便宜判据决定要不要重定位
        if not ml.obj or ml.is_stale():
            if ml.obj:
                self.notes.append("MatchLog：is_stale() 为真 ⇒ forget+重新定位")
                ml.forget()
            ml.locate()
        return ml

    def _receipt(self, mark: int, action_type: str, card_id: Optional[int] = None,
                 timeout: float = 2.5, awaiting_probe=None) -> dict:
        """动作成没成，看游戏自己的动作流（跟 ops.py 同一套判据）。

        `awaiting_probe`：可调用对象，返回真 ⇒ **立刻**回
        `{"ok": False, "awaiting_target": True}`，不再等满 `timeout`。
        用途：**部署型指向**的单位在阶段一**按设计不会有** `XActionPlayCardFromHand`
        （卡留在"待点目标"态，动作要到阶段二点目标才进流）—— 等满 3 s 是纯浪费。
        实测 `card_being_played_from_hand()` 热态 0 ms ⇒ 这个探测是免费的。
        """
        ml = self.matchlog()
        t0 = time.time()
        probe_hits = 0
        while True:
            rows = ml.since(mark)
            hit = [r for r in rows if r.get("action_type") == action_type]
            if card_id is not None:
                hit = [r for r in hit if _action_has_card(r, card_id)]
            if hit:
                return {"ok": True, "actions": hit, "waited": round(time.time() - t0, 2)}
            if awaiting_probe is not None:
                try:
                    if awaiting_probe():
                        # ★ 连续 3 次（~0.15 s）都成立才认 —— 避免抓在"提交中间态"上误判
                        probe_hits += 1
                        if probe_hits >= 3:
                            return {"ok": False, "actions": [], "awaiting_target": True,
                                    "waited": round(time.time() - t0, 3)}
                    else:
                        probe_hits = 0
                except Exception:                              # noqa: BLE001
                    pass
            if time.time() - t0 >= timeout:
                # ★ 失败时**统一**把游戏提示捞出来当权威理由（规格 §7.6f）：提示短命，
                #   只能在"刚失败"这一刻轮询；只回一个 ok=False 等于让失败不可诊断。
                return {"ok": False, "actions": [], "waited": round(time.time() - t0, 2),
                        "game_hints": self.notify_texts()}
            time.sleep(0.05)

    def _mark(self) -> int:
        return self.matchlog().mark()

    def _notify_watcher(self):
        """提示 watcher（惰性 + 缓存）。第一次 `poll()` 只建立基线（**不要**把它当"刚弹的提示"）。"""
        if self._nw is None:
            try:
                from kardsmem.notify import NotifyWatcher
                w = NotifyWatcher(self.ks)
                w.poll()                                  # 预热：吞掉调用前就存在的旧槽位
                self._nw = w
            except Exception as e:                        # noqa: BLE001
                self.notes.append("NotifyWatcher 建不起来（notify_texts 失效）：%s" % e)
                self._nw = False
        return self._nw or None

    def notify_texts(self, limit: int = 4) -> list:
        """动作被拒时游戏弹的**权威理由**（规格 §7.6f）——跟 `ops.py::_notify_texts` 同源。

        ★ 为什么必须有：外部判据（`CanAttack`/`CanPlayFromHand`）**只挑不判**，它的缺漏
          是必然的；动作发出去被游戏拒掉时，唯一能说清"为什么"的就是这条提示通道。
          只报 `ok=False` 而不报理由，等于把失败变成不可诊断。
        ★ 提示**短命**（淡入淡出后 widget 销毁）⇒ 只能轮询、事后捞不到 ⇒ 失败时**立刻**调它。
        读不到就回 `[]`（绝不抛）：提示是增强信息，不能因为它把主流程带崩。
        """
        w = self._notify_watcher()
        if not w:
            return []
        try:
            from kardsmem.locres import translator
            tr = translator()
            out, seen = [], set()
            for r in w.poll():
                t = tr(r.get("text") or "")
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            return out[:limit]
        except Exception as e:                            # noqa: BLE001
            self.notes.append("notify_texts 读取失败：%s" % e)
            return []

    # ------------------------------------------------------------ 动作
    def end_of_turn(self, verbose: bool = True, force: bool = False) -> dict:
        """结束回合：按钮的 悬停 → 按下 → 松开 → 点击(自身) → 点击(HUD 监听者)。

        ★ 为什么走 `BndEvt__*` 而不是按钮的 `OnMouseEnter`：后者签名带
          `(FGeometry, FPointerEvent)` 两个**结构体**入参，传 NULL 会崩；而真实点击
          驱动的实际逻辑就在这几个组件级句柄上。悬停那一步没有省 —— 它调的是
          `..._OnButtonHoverEvent__DelegateSignature`。
        ★ `force=False`（默认）时**拒绝**在"选择界面还开着"的情况下结束回合 —— 跟
          `ops.py::end_of_turn` 同一条纪律：那时点结束回合会让游戏**默认选最左**，
          等于替对手/替自己乱选。`force=True` 才越过（越过前明确知道这个后果）。
        """
        if not force:
            pend = self.pick_pending()
            if pend.get("pending"):
                how = ("select_hand_target()（手牌目标：点牌 + 确认两下）"
                       if pend.get("is_selecting_hand_target") == 1
                       else "pick_choice(index)（抉择/预报）")
                return {"ok": False, "pending": pend, "reason": "pending_selection",
                        "error": "选择界面还开着（%s）——结束回合会让游戏默认选最左；"
                                 "先用 %s。真要结束传 force=True"
                                 % (pend.get("reason") or pend, how)}
        hud = self.hud_actor()
        if not hud:
            return {"ok": False, "error": "找不到 %s 活实例（不在对局里？）" % HUD_CLASS}
        # ★ 2026-09-26（`click-crash-report.md` §5）：**先验证这个 HUD 属于当前这一局**。
        #   `hud_actor()` 在拿不到 `GetBoard()→BattleHUD` 链时会退回扫实例，历史上真点到过
        #   **上一局的 HUD**（handoff §872-879）；而 `end_of_turn` 是裸句柄调用，
        #   打到已销毁对象上就是 `access violation`（`0x8a53` 那次的疑似来源）。
        #   ⇒ 归属验证不过就**报错不点**，绝不赌。
        if not self.owned_by_current_match(hud):
            return {"ok": False, "error": "HUD(0x%X) 不属于当前这一局（疑似上一局残留）——拒绝点击"
                                          % hud}
        btn = self.end_turn_button(hud)
        if not btn:
            return {"ok": False, "error": "HUD 的 EndTurnButton 指针为空"}
        hud_cls = self.uclass_of_instance(hud)
        btn_cls = self.uclass_of_instance(btn)

        mk = self._mark()
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature"))
        self.settle(0.25, 6)
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature"))
        self.settle(0.08, 4)          # 按下态要真的渲染出来：按帧数等（失焦降帧时 0.08 s 可能一帧没画）
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature"))
        self.settle(0.05, 3)
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature"))
        self.call0(hud, self.fn(hud_cls, "BndEvt__EndTurnButton_K2Node_ComponentBoundEvent_249_OnButtonClickedEvent__DelegateSignature"))
        if verbose:
            print("  结束回合: hover→press→release→click(self)→click(HUD)")
        r = self._receipt(mk, "XActionEndOfTurn", timeout=3.0)
        if not r.get("ok"):
            r["game_hints"] = self.notify_texts()
        return r

    def wait_our_turn(self, limit: float = 300.0, verbose: bool = True) -> bool:
        """**只读**等我方回合（`ops.py::wait_our_turn` 同源）——不动内存、不动输入。

        每 `1.0s` 采一次快照，只在 `(turn, our_turn, kredits, match_finished)` 变化时
        打一行（避免刷屏）；`match_finished` → False（对局结束，等不到）；`our_turn` → True。
        """
        from kardsmem import board as BA
        t0 = time.time()
        last = None
        while time.time() - t0 < limit:
            st = BA.open_source("mem").snapshot()
            key = (st.turn, st.our_turn, (st.kredits or {}).get("local"), st.match_finished)
            if verbose and key != last:
                print("  t=%.0fs turn=%s our=%s k=%s fin=%s"
                      % (time.time() - t0, st.turn, st.our_turn,
                         (st.kredits or {}).get("local"), st.match_finished))
                last = key
            if st.match_finished:
                return False
            if st.our_turn:
                return True
            time.sleep(1.0)
        return False

    def preflight(self, card_id: Optional[int] = None, target=None,
                  action: Optional[str] = None, verbose: bool = True) -> dict:
        """动手前自检汇总（`ops.py::preflight` 同源，**不动鼠标/不写内存**）。

        只做两件有增量价值的事：
          ① 报"选择界面有没有开着"（开着时任何出牌/移动/攻击都被吃掉，这是机械事实，
             各动作函数自己也会拦，这里给一个提前看一眼的入口）；
          ② 给了 `card_id`（+可选 `target`/`action`）就顺手跑一次对应判据。
        ★ 这里的一切**都只是提前看**，不是又一道判据 —— 真正的拦截在动作函数里，
          而且判据缺漏是必然的（"只挑不判"）。
        """
        pend = self.pick_pending()
        blocked = bool(pend.get("pending"))
        out = {"pick_pending": pend, "blocked_by_pick": blocked}
        if verbose:
            print("选择界面：%s" % ("开着 —— 出牌/移动/攻击都会被吃掉，先处理它"
                                  if blocked else "没开，可以动手"))
        if card_id is None:
            return out
        card = self.find_card(card_id)
        out["card_found"] = card is not None
        if card is None:
            if verbose:
                print("card %s 不在我方手牌/场上" % card_id)
            return out
        if verbose:
            print("card %s name=%r type=%s loc=%s atk=%s hp=%s"
                  % (card_id, card.name, card.card_type, card.location,
                     card.attack, card.defense))
        if action in (None, "move") and target is None:
            # ★ 2026-09-27：不再"伪造拖拽态"去问 `CanMoveCardToLocation`（它要写
            #   `PC->SelectedCard`；见那个函数的 docstring）⇒ 这里如实报"问不出来"，
            #   移动是否成立由游戏在提交时自己判（`move_to_front`）。
            gm = self.can_move_to(card_id, LOC_BOARD_FRONTLINE, verbose=False)
            out["can_move"] = gm
            out["pinned"] = self.is_pinned(card_id)
            if verbose:
                print("can_move_frontline(%s)：%s | pinned=%s"
                      % (card.name, gm.get("reason") or gm.get("can"), out["pinned"]))
        if target is not None:
            rt = self.resolve_target(target)
            out["target"] = rt
            if rt.get("ok"):
                ca = self.game_can_attack(card_id, rt["card_id"], verbose=False)
                out["can_attack"] = ca
                if verbose:
                    print("can_attack(%s -> %s) = %s（%s）"
                          % (card.name, rt["card_id"], ca.get("can"),
                             ca.get("reason_zh") or ca.get("stopped")))
            elif verbose:
                print("target %r 解析失败：%s" % (target, rt.get("error")))
        return out


    def game_can_attack(self, attacker_id: int, target_id: int,
                        verbose: bool = False,
                        world_context: Optional[int] = None,
                        kredits_override: Optional[int] = None) -> dict:
        """跑**游戏自己的** `cardsCheckFunctions_C::CanAttack`（纯查询，不改数据）。

        签名（SDK 1.60）：`CanAttack(UBaseCardObject* attackerCard, UBaseCardObject* defenderCard,
        int32 attackerKredits, int32 currentTurn, TArray<UBaseCardObject*>& cardsInAttackedLocation,
        UObject* __WorldContext, bool* CanAttack_0, FString* failReason, FString* Reason_Param_1)`
        ⇒ ParmsSize `0x58`：
          0x00 attacker / 0x08 defender / 0x10 kredits / 0x14 turn /
          0x18 TArray(ptr,num,max) / 0x28 WorldContext / 0x30 出参 can /
          0x38 出参 failReason(FString) / 0x48 出参 Reason_Param_1(FString)
        """
        cls, cdo, f = self._bp_lib_fn("cardsCheckFunctions_C", "CanAttack")
        if not f:
            return {"ok": False, "stopped": "找不到 cardsCheckFunctions_C::CanAttack"}
        a = self._card_object(attacker_id)
        d = self._card_object(target_id)
        if not a or not d:
            return {"ok": False, "stopped": "拿不到 attacker/defender 的 UBaseCardObject"}
        from kardsmem import board as BA
        import struct as _s
        st = BA.open_source("mem").snapshot()
        kred = int(kredits_override if kredits_override is not None
                   else ((st.kredits or {}).get("local") or 0))
        # ★ `cardsInAttackedLocation` = **防守方那一行、那一侧**的全部卡对象指针
        #   （`agent/legality.location_cards` 的同一套语义；战斗机拦截靠它）。
        #   传空数组时"攻击者在前线"的组合会让游戏崩/卡（实测 27→54）。
        defender = next((c for c in st.cards if c.card_id == target_id), None)
        row_ptrs = []
        if defender is not None:
            for c in st.cards:
                if c.side == defender.side and c.location == defender.location:
                    p = (c.raw or {}).get("ptr") or self._card_object(c.card_id)
                    if p:
                        row_ptrs.append(p)
        parms = bytearray(0x58)
        _s.pack_into("<Q", parms, 0x00, a)
        _s.pack_into("<Q", parms, 0x08, d)
        _s.pack_into("<i", parms, 0x10, int(kred))
        _s.pack_into("<i", parms, 0x14, int(st.turn or 0))
        _s.pack_into("<Qii", parms, 0x18, 0, 0, 0)      # 数组位先占着，JS 侧填
        # ★★ 2026-09-26 **更正**（用户当场指出："指向税大部分单位没有。场上均无。
        #   你 legal 判断有问题。"）：`__WorldContext` 传 **0 是错的**，跟
        #   `CanSelectAsTarget` 是**同一个坑**（那里早已注明"传 0 ⇒ 任何目标都回
        #   `..._not_enough_kredits_to_target`"），我当时顺手写了句"CanAttack 那边
        #   传 0 是 OK 的"——**没有验证**。
        #   反证：`kardsmem cards --raw` 显示**全盘 `kredits_tax_as_enemy_target` 都是 0**、
        #   GUARD 的 `operation_cost` 就是 3，而"7 点打它"却回
        #   `not_enough_kredits_to_target`（连打 1/3 的 HUMBER 也一样）。
        #   ⇒ 传活的 `BP_Logic_C`（跟 CanSelectAsTarget 同一套默认）。
        wc = int(world_context if world_context is not None else (self.logic_actor() or 0))
        _s.pack_into("<Q", parms, 0x28, wc)             # __WorldContext（必须非 0）
        # ⚠ 实测：某些组合下 ProcessEvent 会抛 native "system error"（进程不死，但 RPC 报错）。
        #   不能让它把调用方带崩 —— 包起来，如实报"这次没问出来"。
        try:
            out = self.call_raw_arr(cdo, f, bytes(parms), 0x18, row_ptrs)   # ★ self = CDO
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "stopped": "CanAttack 调用抛异常：%s" % e,
                    "attacker": attacker_id, "target": target_id,
                    "row_ptrs": len(row_ptrs),
                    "source": "game:cardsCheckFunctions::CanAttack"}
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        can = bool(buf[0x30])
        res = {"ok": True, "can": can, "kredits": kred, "turn": st.turn,
               "world_context": hex(wc) if wc else None,
               "source": "game:cardsCheckFunctions::CanAttack", "func": hex(f)}
        for key, off in (("fail_reason", 0x38), ("reason_param_1", 0x48)):
            ptr, num = _s.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00") if raw else None
        if verbose:
            print("    游戏自己的 CanAttack(%s -> %s) -> can=%s failReason=%r"
                  % (attacker_id, target_id, can, res.get("fail_reason")))
        return res

    # ------------------------------------------------------------ 游戏自己的"总闸"预检
    # ★ 2026-09-25 用户定调：**预检用游戏的总入口**（NN/前端都要用）。
    #   `BP_Logic_C` 上三个总闸（都是实例函数，self = 活着的 BP_Logic_C）：
    #     CanPlayCardFromHand(int32 CardID, bool* Yes)   全程打牌判据（bool-only，没有 reason）
    #     CanCardDoAnything(int32 CardID, bool* canIt)   这张牌这回合还能不能做事
    #     CanIDoAnything(bool* canI)                     我方还有没有事可做
    #   `CanPlayCardFromHand` 内部依次做：指挥点(getTotalKreditCost≥getKreditBySide)、
    #   支援线满(IsLocationFull)、全局限制(IsThereGameplayRestriction/BlockCardFromBeingPlayedFromHand)、
    #   天气/自定义能力特例、卡自身 CanPlayFromHand、以及 CanSelectAsTarget(指向)。
    #   ★ 游戏自己的**提交路径**（`BP_Logic::GlobalMouseUpBattle`）调的则是两个带 reason 的
    #     叶子检查：`CanPlayFromHand` + `CanSelectAsTarget`（理由就是屏幕提示那份）。
    #   ⇒ 预检：先问总闸；它说不行再下钻取理由。
    # ------------------------------------------------------------ 游戏自己的"指向"判据
    def game_can_select_as_target(self, targeted_id: int, targeting_id: int,
                                  by_play_from_hand: bool = True,
                                  world_context: Optional[int] = None,
                                  remaining_kredits: Optional[int] = None,
                                  verbose: bool = False) -> dict:
        """跑**游戏自己的** `cardsCheckFunctions_C::CanSelectAsTarget`（纯查询，不改数据）。

        签名（SDK 1.60，`cardsCheckFunctions_classes.hpp:25`）：
            `CanSelectAsTarget(UBaseCardObject* Targeted, UBaseCardObject* Targeting,
                               bool byPlayFromHand, UObject* __WorldContext,
                               bool* can, FString* Reason, FString* Reason_Param_1,
                               FString* Reason_Param_2)`
        ParmsSize `0x128`（`cardsCheckFunctions_parameters.hpp:98`）：
            0x00 Targeted（**被指向**的那张） / 0x08 Targeting（**做出指向**的那张，即正在打的牌）
            0x10 byPlayFromHand（是不是"从手牌打出"引起的指向）
            0x18 __WorldContext（跟 CanAttack 一样传 0）
            0x20 出参 can / 0x28 Reason / 0x38 Reason_Param_1 / 0x48 Reason_Param_2
        它是游戏**判定"这张牌能不能指向那张卡"的权威**（箭头 ubergraph、
        `BP_HandCard::DoesThisCardHasAnyTarget`、`BP_Logic::GlobalMouseUpBattle`、
        `BP_Logic::CanPlayCardFromHand` 都调它，共 6 处调用点）。
        ⇒ "带目标出牌"的预检就用它，别自己算。
        """
        cls, cdo, f = self._bp_lib_fn("cardsCheckFunctions_C", "CanSelectAsTarget")
        if not f:
            return {"ok": False, "stopped": "找不到 cardsCheckFunctions_C::CanSelectAsTarget"}
        tgt = self._card_object(targeted_id)
        src = self._card_object(targeting_id)
        if not tgt or not src:
            return {"ok": False,
                    "stopped": "拿不到 UBaseCardObject（targeted=%s targeting=%s）"
                               % (bool(tgt), bool(src))}
        parms = bytearray(0x128)
        struct.pack_into("<Q", parms, 0x00, tgt)
        struct.pack_into("<Q", parms, 0x08, src)
        parms[0x10] = 1 if by_play_from_hand else 0
        # ★★ 2026-09-25 实机定位：`__WorldContext` **必须非 0**。
        #   传 0 时函数内部取不到 `BP_Logic_C`，于是指挥点按 0 读 ⇒
        #   **任何目标**都回 `can=False reason='play_from_hand_not_enough_kredits_to_target'`
        #   （看起来像"所有目标都不合法"，其实是上下文没给）。
        #   传 logic / playerController / 手牌 actor 都可以（实测三者一致：
        #   打 GUNSHIP MISSION 指向敌方支援线的 I-16 ISHAK → can=True reason=''）。
        #   默认用活着的 `BP_Logic_C`。
        #   （★ 2026-09-26 **更正**：当时这里写着"`CanAttack` 那边传 0 是 OK 的"——
        #     **是错的**。同一个坑在 `CanAttack` 上也成立：传 0 会让它回
        #     `not_enough_kredits_to_target`；已改成传活 `BP_Logic_C`。）
        struct.pack_into("<Q", parms, 0x18, int(world_context if world_context is not None
                                              else (self.logic_actor() or 0)))
        # `tmpRemainingKredits@0x58`：**不是** Parm 标记，但拒绝理由永远落在
        # `play_from_hand_not_enough_kredits_to_target` ⇒ 怀疑它要么是调用方该填的
        # "剩余指挥点"、要么函数内部靠 `__WorldContext` 取 Logic 才拿得到。两个都试。
        if remaining_kredits is not None:
            struct.pack_into("<i", parms, 0x58, int(remaining_kredits))
        try:
            out = self.api().call_raw(hex(cdo), hex(f), parms.hex())      # ★ self = CDO
        except Exception as e:                                         # noqa: BLE001
            return {"ok": False, "stopped": "CanSelectAsTarget 调用抛异常：%s" % e,
                    "source": "game:cardsCheckFunctions::CanSelectAsTarget"}
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        res = {"ok": True, "can": bool(buf[0x20]), "targeted": targeted_id,
               "targeting": targeting_id, "source": "game:cardsCheckFunctions::CanSelectAsTarget",
               "func": hex(f)}
        for key, off in (("reason", 0x28), ("reason_param_1", 0x38), ("reason_param_2", 0x48)):
            ptr, num = struct.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                if raw:
                    res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if verbose:
            print("    游戏自己的 CanSelectAsTarget(targeted=%s, targeting=%s) -> can=%s reason=%r"
                  % (targeted_id, targeting_id, res["can"], res.get("reason")))
        return res

    def _game_logic_bool(self, fn_name: str, card_id: Optional[int], verbose: bool = False) -> dict:
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C 实例"}
        cls = self.uclass_of_instance(logic)
        f = self.find_fn(cls, fn_name, inherited=True)
        if not f:
            return {"ok": False, "stopped": "BP_Logic_C 上找不到 %s" % fn_name}
        from kardsmem import board as BA                                       # noqa: F401
        import struct as _s
        parms = bytearray(0x08)
        if card_id is not None:
            _s.pack_into("<i", parms, 0x00, int(card_id))
        try:
            out = self.api().call_raw(hex(logic), hex(f), parms.hex())
        except Exception as e:                                       # noqa: BLE001
            return {"ok": False, "stopped": "%s 调用抛异常：%s" % (fn_name, e)}
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "%s 没返回" % fn_name}
        can = bool(buf[0x04] if card_id is not None else buf[0x00])
        res = {"ok": True, "can": can, "source": "game:BP_Logic::%s" % fn_name, "func": hex(f)}
        if verbose:
            print("    游戏自己的 %s -> %s" % (fn_name, can))
        return res

    def game_can_play_card_from_hand(self, card_id: int, verbose: bool = False) -> dict:
        """打牌**总闸**：`BP_Logic_C::CanPlayCardFromHand(CardID, bool* Yes)`（bool-only）。"""
        return self._game_logic_bool("CanPlayCardFromHand", card_id, verbose)

    def game_can_card_do_anything(self, card_id: int, verbose: bool = False) -> dict:
        """`BP_Logic_C::CanCardDoAnything(CardID, bool* canIt)`。"""
        return self._game_logic_bool("CanCardDoAnything", card_id, verbose)

    def game_can_i_do_anything(self, verbose: bool = False) -> dict:
        """`BP_Logic_C::CanIDoAnything(bool* canI)`。"""
        return self._game_logic_bool("CanIDoAnything", None, verbose)

    def precheck_play(self, card_id: int, verbose: bool = False) -> dict:
        """**打牌预检（NN/前端统一入口）**：先问游戏总闸，false 时下钻取理由。

        返回 `{"ok", "can", "source", "reason", ...}`；`reason` 是 `not_enough_kredits` /
        `supply_line_full` / 或游戏自己给的理由码（如 `enemy_unit`）。
        注意：**它只挑不判**——真打出去时照样走完整拖拽，让游戏自己裁决。
        """
        gate = self.game_can_play_card_from_hand(card_id, verbose=verbose)
        if gate.get("ok") and gate.get("can"):
            return {"ok": True, "can": True, "source": gate.get("source")}
        out = {"ok": True, "can": False, "source": gate.get("source"),
               "gate": gate}
        if not gate.get("ok"):
            out["stopped"] = gate.get("stopped")
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        card = next((c for c in st.cards if c.card_id == card_id and c.side == "local"), None)
        kred = (st.kredits or {}).get("local")
        # ⓿ **有别的选择界面开着** ⇒ 出牌/移动/攻击全部无效（游戏机制事实，CLAUDE.md 环境坑）。
        #    实机踩过：`isSelectingHandTarget=1` 卡住（我们的 175th 部署效果"选一张手牌
        #    放回牌组顶"从没完成），接下来每一张牌的总闸都回 False、而三种下钻
        #    （指挥点/支援线/卡自己的覆写）一个都不匹配 ⇒ `reason=None`，
        #    调用方完全看不出原因、只能瞎试。这条把它点出来。
        try:
            from kardsmem.pick import pick_state as _ps
            pst = _ps(self.ks)
            if pst.get("pending"):
                which = ("选一张手牌" if pst.get("is_selecting_hand_target") == 1
                         else "卡牌抉择界面" if pst.get("choose_one_active") == 1 else "选择界面")
                out.update(reason="pending_selection", pending="hand_target"
                           if pst.get("is_selecting_hand_target") == 1 else "choose_one",
                           reason_zh="现在还开着%s，出牌/移动/攻击都被游戏挡着" % which,
                           pending_evidence=pst.get("pending_evidence"))
                return out
        except Exception:                                          # noqa: BLE001
            pass
        # ① 指挥点
        if card is not None and kred is not None and card.kredit_cost is not None \
                and kred < card.kredit_cost:
            out.update(reason="not_enough_kredits", reason_zh="指挥点不够",
                       kredits=kred, cost=card.kredit_cost)
            return out
        # ② 支援线满（单位打不出去）
        if card is not None and card.card_type in self.UNIT_TYPES and self._free_slot() is None:
            out.update(reason="supply_line_full", reason_zh="支援线满了")
            return out
        # ③ 下钻：卡自己的规则（带 reason）
        detail = self.game_can_play_from_hand(card_id, verbose=verbose)
        out["detail"] = detail
        if detail.get("reason"):
            out.update(reason=detail.get("reason"), reason_zh="游戏自己给的理由码")
        return out

    def _card_object(self, card_id: int) -> int:
        """拿一张牌的 `UBaseCardObject`（手牌/板卡同基类，都用 actor 的 `selfBaseCardRef`）。"""
        rec = self.hand_actor(card_id)
        actor = rec["actor"] if rec else self.board_actor_of(card_id)
        if not actor:
            return 0
        cls = self.uclass_of_instance(actor)
        off = self.off(cls, "selfBaseCardRef", NOTE_OFF_ACTOR_SELF_BASECARD)
        return self.m.ptr(actor + off) or 0

    def _read_fstring(self, addr: int) -> Optional[str]:
        """FString {wchar*@+0, int32 num@+8} → str（num<=1 视为空）。"""
        p = self.m.ptr(addr) or 0
        num = self.m.i32(addr + 8)
        if not p or not num or num <= 1 or num > 4096:
            return None
        raw = self.m.read_exact(p, num * 2)
        return raw.decode("utf-16-le", "replace").rstrip("\x00") if raw else None

    def game_can_play_from_hand(self, card_id: int, verbose: bool = False) -> dict:
        """跑**游戏自己的** `UBaseCardObject::CanPlayFromHand`（纯查询，不改数据）。

        签名（SDK 1.60）：`CanPlayFromHand(bool* canIt, FString* Reason,
        FString* reasonParam1, FString* reasonParam2, UBaseCardObject** targetedCard)`
        ⇒ ParmsSize `0x40`：canIt@0x00 / Reason@0x08 / p1@0x18 / p2@0x28 / targetedCard@0x38。

        ★ 2026-09-25：既然它**不改数据**，就没必要再跑我们自己那套"进程外 VM 重演"来猜
          ——直接问游戏。（`agent/legality` 的 VM 路径留给"不能注入"的场合。）
        ⚠ 实测它**不查指挥点**：0 指挥点时 cost=2/3 的牌照样返回 canIt=1。
          指挥点是另一道闸门（`PayMovementCost` 那类），别拿这个函数的返回值当"付得起"。
        """
        obj = self._card_object(card_id)
        if not obj:
            return {"ok": False, "stopped": "找不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        f = self.find_fn(cls, "CanPlayFromHand", inherited=True)
        if not f:
            return {"ok": False, "stopped": "card %s 上找不到 CanPlayFromHand" % card_id}
        out = self.api().call_raw(hex(obj), hex(f), ("00" * 0x40))
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        import struct as _s
        can = bool(buf[0x00])
        ptr, num = _s.unpack_from("<Qi", buf, 0x08)
        reason = None
        if ptr and num and 1 < num <= 4096:
            raw = self.m.read_exact(ptr, num * 2)
            if raw:
                reason = raw.decode("utf-16-le", "replace").rstrip("\x00")
        res = {"ok": True, "can": can, "reason": reason,
               "targeted_card": _s.unpack_from("<Q", buf, 0x38)[0] or None,
               "source": "game:BaseCardObject::CanPlayFromHand", "func": hex(f)}
        if verbose:
            print("    游戏自己的 CanPlayFromHand(%s) -> canIt=%s reason=%r"
                  % (card_id, can, reason))
        return res

    def play_card(self, card_id: int, target_id: Optional[int] = None,
                  verbose: bool = True, hover_pause: Optional[float] = None,
                  force: bool = False, also_mouse_up: bool = False,
                  location: str = "back", slot: Optional[int] = None,
                  require_inactive: bool = False) -> dict:
        """打出一张手牌。事件卡/单位卡自动分流，落点都是真实合法的空位。

        `slot`（★ 2026-09-27 新增）：**单位部署的站位** = 支承线内的 `locationNumber`。
        不给就自己挑一个空槽（`_free_slot()`）。合法域只有**我方支承线/后排**
        （`LocationUnderCursor=LOC_BOARD_HQLEFT`）—— 实测游戏**不接受直接部署到前线**
        （见下面那段 2026-09-26 的结论），所以站位参数不表达"放哪一行"，
        只表达"这一行里的第几格"。**站位有意义**：插在两个单位之间会改变名次
        （相邻关系/名次类效果看它）。
        `require_inactive`：只对**反制**有意义 —— True 时"已经激活的反制"会被拒绝
        （默认 False = 允许重复打出，也就是**切换**，见下面那段）。

        `location`：单位卡放哪一行 —— `"back"`（我方支援线，默认）/ `"front"`（我方前线）。
        ★★ 2026-09-26 实测结论（用户确认"**游戏规则如此**"）：**游戏不接受"直接部署到前线"**。
          我们按真实鼠标会产生的落点写了 `LocationUnderCursor=7`（Board_Frontline）+
          `RowUnderCursor=1`，`drag_success=1`，但游戏仍把单位放进**支援线**
          （实测 8/8 的 5th RANGERS 落在 `back`）⇒ 上前线**只能**走 `move_to_front`
          （而移动要付操作费、且步兵移动后本回合不能再攻击）。
          这个参数保留有两个用处：① 把"这条路被堵死"这件事钉在代码里（谁再想直接摆前线，
          一眼就能看到结论和证据）；② 万一将来规则的口径变了，这里是现成的复现入口。

        ★★ 2026-09-25 修（用户实机指出："指挥点不够应该直接 stop，别往下走"）：
          旧版只问了这张卡**自己的** `CanPlayFromHand`——而它**不查指挥点**
          （见 `game_can_play_from_hand` 的注释：0 指挥点时 cost 2/3 的牌照样 canIt=1）。
          于是"打不起的牌"会一路走到拖拽/落地，游戏再拒绝一次，屏幕上弹一条
          "指挥点数不足"，调用方还拿不到"为什么没打出去"。现在先问**总闸**
          `BP_Logic_C::CanPlayCardFromHand`（`precheck_play()`：含指挥点、支援线满、
          全局限制、卡自身覆写、指向），它说不行就**当场返回**，不拖、不发事件。
          总闸只回 bool，所以再用卡自己的 `CanPlayFromHand` 下钻取理由（指挥点那条
          由 `precheck_play` 归因）。**注意这仍然只是"预检"**：真打时照样走完整拖拽，
          最终裁决权在游戏（§7.6f）。
        """
        _T0 = time.time()
        r = self.hand_actor(card_id)
        if not r:
            return {"ok": False, "error": "card %s 不在我方手牌" % card_id}
        card, _st = self._card(card_id)
        # ★★ 2026-09-27 **反制改成"可重复打出"**（用户口径："反制可以被多次打出（切换是否激活）"）。
        #   以前这里在 `gotchaActivated > 0` 时**硬拒绝** —— 那是为"别误取消激活"加的护栏，
        #   但它把**切换**这个正当语义一起挡了。现在：**放行**，但把切换结果如实报出来
        #   （`gotcha_activated_before/after` + `toggled_off`），免得调用方把"打两次 = 关掉"
        #   读成"第一次失败了"。要"只在没激活时打"的老行为传 `require_inactive=True`。
        if (card is not None and (card.card_type or "") == "gotcha"
                and (getattr(card, "gotcha_activated", 0) or 0) > 0):
            if require_inactive:
                return {"ok": False, "reason": "gotcha_already_activated",
                        "card": card.name, "gotcha_activated": card.gotcha_activated,
                        "error": "这张反制**已经激活**（gotchaActivated=%s），"
                                 "而调用方要求 require_inactive=True" % card.gotcha_activated}
        pc = self.precheck_play(card_id, verbose=verbose)
        if pc.get("ok") and pc.get("can") is False:
            return {"ok": False, "reason": pc.get("reason"), "gate": "BP_Logic::CanPlayCardFromHand",
                    "error": "游戏自己的总闸 CanPlayCardFromHand 说不行（reason=%r）"
                             % pc.get("reason"),
                    "precheck": pc}
        # ★★ 2026-09-25 实机补的第二道：**总闸说 True 不代表真能打**。
        #   实测 `GUNSHIP MISSION`（cost 2、`needs_hand_target=True`）在"敌方支援线没人"
        #   时：总闸 `CanPlayCardFromHand`=**True**，而卡自己的 `CanPlayFromHand`=
        #   `False reason='enemy_in_supply_line'`（它要一个敌方支援线单位当目标）。
        #   ⇒ 总闸内部那道 `CanSelectAsTarget` 在"当前没有箭头"时不能替代卡自己的覆写。
        #   所以**卡自己的覆写永远要问**（它是"为什么"的权威来源），只是别拿它当
        #   "缺目标"的否决：指向类在预检阶段没有箭头，必然报"要目标"。
        gp = self.game_can_play_from_hand(card_id, verbose=verbose)
        if gp.get("ok") and gp.get("can") is False:
            needs_t = bool(getattr(card, "needs_hand_target", False))
            reason = gp.get("reason")
            is_target_kind = needs_t or target_id is not None or reason in NEEDS_TARGET_REASONS
            if target_id is not None:
                # 给了目标 ⇒ 走**带目标出牌**那条路（复刻"拖到目标上松手"）
                return self.play_card_targeted(card_id, int(target_id), verbose=verbose,
                                               hover_pause=hover_pause,
                                               also_mouse_up=also_mouse_up)
            if is_target_kind:
                if not force:
                    # 明确停手：盲拖只会静默失败（动作流零新增），调用方还看不出原因
                    return {"ok": False, "reason": "needs_target", "game_reason": reason,
                            "card": card.name if card else None, "game": gp,
                            "error": "这张牌要指向目标（游戏自己的 reason=%r）——"
                                     "用 play_card_targeted(card_id, target_id)，"
                                     "或 force=True 先真拖一次（看游戏会不会自己开第二阶段）"
                                     % reason}
                # ★ `force=True`：**不拿"要目标"这条判据否决动作**（§7.6f）。
                #   典型场景：「部署型指向」（327th PATHFINDERS "部署：对 1 个单位造成 2 点伤害"）
                #   —— 预检阶段没有箭头/没有已选目标，卡自己的覆写必然回"要目标"，
                #   但那不代表拖不出去：**单位先落地，然后游戏进入"点选目标"状态**
                #   （用户给的旧 ops 流程：部署后需要点选目标；点到非法位置 ⇒ 卡回手牌；
                #    悬停在不符合要求的卡上会显示 reason）。
                #   ⇒ force 之后就**落到普通拖拽**，让游戏自己开那个状态。
                if verbose:
                    print("    ⚠ force：卡覆写说 needs_target（reason=%r）仍按**普通拖拽**发出去"
                          "（单位应落地并进入点选目标）" % reason)
            else:
                # 非"要目标"类的拒绝 ⇒ 以卡自己的覆写为准（它比总闸细）
                return {"ok": False, "reason": reason, "game": gp,
                        "error": "游戏自己的 CanPlayFromHand 说不行（reason=%r）" % reason}
        elif not pc.get("ok") and gp.get("ok") and gp.get("can") is False:
            return {"ok": False, "reason": gp.get("reason"), "game": gp,
                    "error": "游戏自己的 CanPlayFromHand 说不行（reason=%r）" % gp.get("reason")}

        # ★★ 2026-09-25 22:2x 的事故换来的硬闸门（**先读闸门，读不出来就别动手**）：
        #   那次 `precheck_play(27)` 返回了 `can=None`（= 注入调用**抛异常**，`agent/precheck`
        #   的 `_call` 丢掉重连一次还是失败），紧接着 **frida-agent.dll 在游戏进程里 fail-fast**
        #   （Windows 事件日志：出错模块 frida-agent.dll、异常 0xc0000409/BEX64），游戏进程直接没了。
        #   ⇒ 两道闸门**都读不出来**，说明注入侧已经不健康；此时拖拽/点击是在
        #     "连查询都失败"的进程里继续写状态，风险与收益完全不成比例。
        #   所以：读不出来就当**拒绝**（不是"不确定所以放行"）。宁可少打一张牌，也不把对局打崩。
        if not pc.get("ok") and not gp.get("ok"):
            return {"ok": False, "reason": "gate_unreadable", "gate": "both",
                    "error": "两道闸门（BP_Logic::CanPlayCardFromHand / 卡的 CanPlayFromHand）"
                             "都读不出来 ⇒ 注入侧可能已不健康，拒绝动手。"
                             "precheck=%r game=%r" % (pc.get("stopped"), gp.get("stopped"))}

        loc_enum = self.our_front_enum() if location == "front" else self.our_back_enum()
        place = None
        if card is not None and card.card_type in self.UNIT_TYPES:
            # ★ 2026-09-27：`slot` = **空隙序号**（0=最左 … n=最右，见 `gap_number` 的推导）。
            #   给了空隙就问游戏"拖到那儿该写第几号"；没给才退回"第一个空列号"（老行为）。
            place = self.deploy_location_number(
                None if slot is None else int(slot),
                row=("front" if location == "front" else "back"), verbose=verbose)
            loc_num = place.get("requested")
            if loc_num is None:
                return {"ok": False,
                        "error": ("我方前线满（最多 5 个单位），单位打不出去" if location == "front"
                                  else "我方支援线满（4 个单位 + 总部），单位打不出去")}
        else:
            # 非单位卡不看落点（Claude 的骄阳那次已证：这条分支只要求 RowUnderCursor 为真）
            loc_num = 0
        gotcha_before = (getattr(card, "gotcha_activated", 0) if card is not None else 0)
        _T1 = time.time()
        mk = self._mark()
        out = self._drag_lifecycle(r["actor"], loc_enum, loc_num,
                                   card_at_location=int((place or {}).get("card_at_location") or 0),
                                   verbose=verbose,
                                   hover_pause=(SETTLE_HOVER_PLAY if hover_pause is None
                                                else hover_pause),
                                   also_mouse_up=also_mouse_up)
        if place is not None:
            out["placement"] = {k: v for k, v in place.items() if k != "row_cards"}
        _T2 = time.time()
        # ★ 2026-10-01 用户："抉择部署的回执应该把抉择时间算在内"：带抉择的部署（5th RANGERS 一类），
        #   选项面板一弹出就**立刻返回**（别在部署这一步干等 5 s+ 的动作流），成交回执留给 `pick_choice`
        #   （它认 `XActionPlayCardFromHand{chooseOneIndex>=0}`）。只在面板真的 pending 时才早退。
        def _choice_panel_up():
            try:
                return bool(self.pick_pending().get("pending"))
            except Exception:                                        # noqa: BLE001
                return False
        outc = self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0,
                             awaiting_probe=_choice_panel_up)
        if outc.get("awaiting_target") and not outc.get("ok"):
            outc = dict(outc, ok=True, awaiting_choice=True,
                        note_choice="选项面板已弹出：成交回执由 pick_choice 确认")
        out.update(outc)
        out["timing"] = {"pre": round(_T1 - _T0, 2), "drag": round(_T2 - _T1, 2),
                         "receipt": round(time.time() - _T2, 2)}
        # 反制的切换：如实报 before/after（`gotchaActivated` 0↔N），别让调用方靠猜
        if card is not None and (card.card_type or "") == "gotcha":
            try:
                _c2, _ = self._card(card_id)
                after = (getattr(_c2, "gotcha_activated", None) if _c2 is not None else None)
            except Exception:                                    # noqa: BLE001
                after = None
            out["gotcha_activated_before"] = gotcha_before
            out["gotcha_activated_after"] = after
            out["toggled_off"] = bool(gotcha_before and after == 0)
        if verbose:
            print("    play card_id=%s -> 动作流回执 ok=%s" % (card_id, outc.get("ok")))
        return out

    def our_front_enum(self) -> int:
        """我方**前线**的 `ECardLocationEnum` 值（7 = `Board_Frontline`）。

        跟 `our_back_enum()` 一对；`move_to_front()` 用的是同一个值。
        """
        return LOC_BOARD_FRONTLINE

    def _target_play_location(self, card, slot: Optional[int] = None) -> tuple:
        """带目标出牌时的**落点**（跟 `play_card` 同一套规则）→ `(loc_enum, 列号, place)`。

        单位卡要落到我方后排的某个**空隙**；非单位卡不看落点（`play_card` 里那条注释：
        非单位分支只要求 `RowUnderCursor` 为真）。

        ★ 2026-09-27 两修：
          ① **`slot` 以前没透传到这里** —— `play_card_unit_with_target` 收了 `slot` 却走进
             这条分支被 `_free_slot()` 覆盖，表现为"请求 slot=1 却落到 3（= 最右）"
             （108gen 那次；用户："你实际上把 108 部署在了最右侧"）。
          ② **`slot` 的语义改成"空隙序号"**（原来是裸列号）—— 见 `gap_number`/`gap_request_key`：
             真要写进 `LocationNumberUnderCursor` 的是「光标左边那些卡的最大列号 + 1」，
             有空洞时和 `i` 不是一回事。
        """
        loc_enum = self.our_back_enum()
        if card is not None and (card.card_type or "") in self.UNIT_TYPES:
            place = self.deploy_location_number(None if slot is None else int(slot), row="back")
            return loc_enum, (place.get("requested") or 0), place
        return loc_enum, 0, {"source": "non_unit", "gap": None, "requested": 0}

    def play_card_targeted(self, card_id: int, target_id: int, verbose: bool = True,
                           hover_pause: Optional[float] = None,
                           also_mouse_up: bool = False,
                           slot: Optional[int] = None) -> dict:
        """**带目标出牌**：把手牌拖到目标卡上松手（复刻真实鼠标序列）。

        顺序（跟攻击那条同一套纪律，2026-09-25 实现）：
          悬停手牌 → 悬停转发 → 按下 → 起拖 → 拖动 tick（箭头由游戏自己生成）
          → **悬停目标卡**（目标 actor 收到 `OnActorMouseEnter`，同样转发给对手）
          → 给场上每个箭头写 `overCardID=目标卡id` + 松手（`OnActorEndDrag`），
            写与松手在**同一次 JS 执行**里
        为什么这么写（两条都是实机踩出来的）：
          * `BP_Logic_C::GetTargetArrowTargetCard` 取 `GetAllActorsOfClass(箭头)[0]->overCardID`
            ⇒ 只写"自己认的那个箭头"不够，要**每个箭头都写**；
          * 攻击上次失败十几次的根因就是**漏了"悬停目标 actor"这一步** —— 补上后第一下就成。
        纪律：先问游戏（`precheck_play` 总闸 + `CanSelectAsTarget` 指向判据），说不行就返回、不拖。

        ⚠ 2026-09-25 更正：这条路只覆盖**指令卡**那种"拖到落点松手就成交"的带目标出牌
          （`selectTargetOnPlayedFromHand=false` ⇒ 松手时 `PlaceHandCard(LocationNumberUnderCursor,
          targetCardID)` 直接成交，GUNSHIP MISSION 实测过）。
          **单位卡且"有目标"是两阶段的**：松手那一下只做**视觉落位 + 生成箭头**，
          不成交 ⇒ 必须接着 `select_unit_target(card_id, 目标)` 去**点目标**
          （`BP_Logic::GlobalMouseUp`）。本函数会读 `cardBeingPlayedFromHand` 并回
          `two_stage_needed=True` 提示这一点。

        ★ 2026-09-27 补一条（把上面那句话的边界说全）：**`selectTargetOnPlayedFromHand=True`
          的指令也是一次成交** —— 松手时 `BP_HandCard.cpp:1204` 因为 `!IsUnit` 直接走
          `Label_15862 → AttemptToPlayFinal`，而 `BP_PlayerMoves.cpp:2183-2198` 读的是
          卡自己的 `CanPlayFromHand` 出参（← `GetTargetedCard` ← 我们写进去的 `targetOverride`）
          ⇒ 目标有了就 `PlaceHandCard(...)` 成交。实机：`FOR FREEDOM`(手牌4) → `5th RANGERS`(24)
          = `XActionPlayCardFromHand{4, target=24}` + `ZActionGainAttack/Defense` + 抽牌。
          ⇒ 语义分工：**指令**用 `play_card_event_with_target()`（一次），**单位**用
          `play_card_unit_with_target()`（两阶段）。
        """
        rec = self.hand_actor(card_id)
        if not rec:
            return {"ok": False, "error": "card %s 不在我方手牌" % card_id}
        pc_gate = self.precheck_play(card_id, verbose=verbose)
        if pc_gate.get("ok") and pc_gate.get("can") is False:
            return {"ok": False, "reason": pc_gate.get("reason"),
                    "error": "总闸 CanPlayCardFromHand 说不行（reason=%r）" % pc_gate.get("reason")}
        sel = self.game_can_select_as_target(target_id, card_id, True, verbose=verbose)
        if sel.get("ok") and sel.get("can") is False:
            return {"ok": False, "reason": "target_rejected", "select_as_target": sel,
                    "error": "游戏自己的 CanSelectAsTarget 说这个目标不行（reason=%r）"
                             % sel.get("reason")}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.fn(cls, "OnActorMouseEnter")
        f_down = self.fn(cls, "OnActorMouseDown")
        f_start = self.fn(cls, "OnActorStartDrag")
        f_tick = self.fn(cls, "OnActorDragTick")
        f_end = self.find_fn(cls, "OnActorEndDrag") or self.find_fn(cls, "OnActorMouseUp")
        f_disp = self.fn(pc_cls, "MouseHoverDispatch")
        self.call0(actor, f_enter)
        # ★ 出牌的手牌悬停 = "鼠标经过这张牌然后拖出去"，不是刻意读它 ⇒ 用短档
        #   （用户 2026-09-26："打出牌的 hover 可以降得更短"）。悬停存在、转发照发（信息流不变）。
        self.settle(SETTLE_HOVER_PLAY if hover_pause is None else hover_pause, 6)  # 悬停停留按帧等，降帧时也保证悬停被处理
        self.call_ptr(pc, f_disp, actor)                      # 手牌悬停转发
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        self.call0(actor, f_down)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call_ptr(actor, f_start, 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, f_tick)                             # 箭头在这一步生成/更新
        self.settle(SETTLE_AFTER_TICK, 2)
        # 悬停**目标**卡（真实玩家把鼠标拖到目标上，目标 actor 会收到 enter）
        out = {"commit_fn": "OnActorEndDrag" if self.find_fn(cls, "OnActorEndDrag") else "OnActorMouseUp"}
        tgt_actor = self.board_actor_of(target_id)
        if tgt_actor:
            f_te = self.find_fn(self.uclass_of_instance(tgt_actor), "OnActorMouseEnter")
            if f_te:
                self.call0(tgt_actor, f_te)
                self.settle(SETTLE_HOVER, 6)
            self.call_ptr(pc, f_disp, tgt_actor)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        out["target_actor"] = hex(tgt_actor) if tgt_actor else None
        out["target_by_logic_before"] = self.arrow_target_by_logic()
        arrows = self.arrow_actors()
        out["arrows"] = len(arrows)
        # ★★ 2026-09-25 **这才是"带目标出牌"的关键**（前面写箭头 overCardID 是白写）：
        #   `BP_CardFunctions_C::GetTargetedCard(callerCard, hasTarget, card)`
        #   （1.58 导出 `BP_CardFunctions.cpp:283-324`）读的是**卡对象自己的字段**：
        #       callerCard->targetOverride（有 ⇒ 就是它）
        #       callerCard->currentTarget（1.60 的 dump 里没有这个字段，只有 targetOverride）
        #       两者都没有 ⇒ hasTarget=false
        #   ★ 而**游戏自己就是用这个字段做探测的**：`BP_HandCard.cpp:4783`
        #       fromCard->targetOverride = currentCard;   // 先假设目标是它
        #       fromCard->CanPlayFromHand(...)            // 再问行不行
        #       currentCard->targetOverride = nullptr;     // 问完清掉
        #     所以我们写这一格**不是编造状态**，是复刻游戏自己的探测/命中写法。
        #   ⇒ 落地时：把"正在打的那张卡的 card 对象"的 `targetOverride` 指到目标的 card 对象。
        #     问完**还原**（游戏自己也会清），否则会漏到后面的动作里。
        src_obj = self._card_object(card_id)
        tgt_obj = self._card_object(target_id)
        off_to = 0
        if src_obj:
            ocls = self.uclass_of_instance(src_obj)
            off_to = self.off(ocls, "targetOverride", NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
            out["targetOverride_off"] = hex(off_to)
        mk = self._mark()
        # ★★ 2026-09-25 修（就是 327th 那次"带目标出牌没反应"的原因）：
        #   **落点也要写**。`play_card`（普通出牌）会写 `cardUnderCursor /
        #   LocationNumberUnderCursor / LocationUnderCursor / RowUnderCursor`，
        #   而这条"带目标"的路之前**只写了 targetOverride** ⇒
        #   单位卡在 `AttemptToPlayFinal` 里目标有了、**落点却没有** ⇒ 不成交。
        #   订单卡（GUNSHIP MISSION）不需要落点，所以那次一次就成了 —— 差别就在这里。
        card, _st = self._card(card_id)          # 落点规则要用它（单位 vs 指令）
        loc_enum, loc_num, place = self._target_play_location(card, slot=slot)
        off_cuc = self.off(cls, "cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR)
        off_ln = self.off(cls, "LocationNumberUnderCursor", NOTE_OFF_LOCATION_NUMBER)
        off_le = self.off(cls, "LocationUnderCursor", NOTE_OFF_LOCATION_ENUM)
        off_r = self.off(cls, "RowUnderCursor", NOTE_OFF_ROW_UNDER_CURSOR)
        writes = [(actor, off_cuc, "s32", 0),
                  (actor, off_ln, "s32", int(loc_num)),
                  (actor, off_le, "u8", int(loc_enum)),
                  (actor, off_r, "u8", 1)]
        out["location"] = {"enum": loc_enum, "number": loc_num,
                           "gap": place.get("gap"), "source": place.get("source"),
                           "row_numbers": place.get("row_numbers")}
        writes += [(a, self.off(self.uclass_of_instance(a), "overCardID",
                                NOTE_OFF_ARROW_OVER_CARD_ID), "s32", int(target_id))
                   for a in arrows]
        if src_obj and tgt_obj:
            writes.append((src_obj, off_to, "ptr", tgt_obj))
        else:
            out["target_override_note"] = "拿不到 src/tgt 的 UBaseCardObject，只能写箭头"
        if writes:
            out["writes"] = len(writes)
            try:
                if also_mouse_up:
                    # 两个松手口都发（同一次 JS 执行）
                    f_up3 = self.find_fn(cls, "OnActorMouseUp")
                    jw = [[hex(int(o)), int(off), kind,
                           (hex(int(val)) if kind == "ptr" else int(val))]
                          for (o, off, kind, val) in writes]
                    out["commit_both"] = self.api().writeAndCall2(
                        jw, hex(actor), hex(f_up3 or f_end), hex(actor), hex(f_end))
                    out["commit_fn"] = "OnActorMouseUp+OnActorEndDrag"
                else:
                    out["writeback"] = self.write_and_call(writes, actor, f_end)
            except Exception as e:                                   # noqa: BLE001
                out["write_error"] = str(e)
        else:
            self.call0(actor, f_end)
        # ★ 阶段一（部署型指向）：单位会停在"待点目标"态、**按设计不会有**这个动作
        #   ⇒ `awaiting_probe` 一成立就早退，不白等满 3 s（实测省 ~3 s/次）。
        out.update(self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0,
                                 awaiting_probe=lambda: bool(self.card_being_played_from_hand())))
        # 还原 `targetOverride`（别把探测状态漏下去）
        if src_obj and tgt_obj:
            try:
                self.poke(src_obj, off_to, "ptr", 0)
                out["target_override_cleared"] = (self.m.ptr(src_obj + off_to) or 0) == 0
            except Exception as e:                                   # noqa: BLE001
                out["clear_error"] = str(e)
        # ★ 2026-09-25：**单位卡**（`IsUnit && DoesThisCardHasAnyTarget`）在阶段一只会被
        #   视觉落位 + 生成箭头，**不会**在这里成交（导出 `BP_HandCard.cpp:1198-1217`，
        #   走的是 Label_14483 而不是 Label_15772）⇒ 没有动作流回执是**正常的**，
        #   这时必须接着走第二阶段（`select_unit_target` 点目标）。
        #   指令卡（`selectTargetOnPlayedFromHand=false`）才会在这一步直接
        #   `PlaceHandCard(LocationNumberUnderCursor, targetCardID)` 成交。
        pending_after = self.card_being_played_from_hand()
        out["playing_after"] = pending_after
        if pending_after:
            out["two_stage_needed"] = True
            out["next"] = "select_unit_target(%s, 目标)" % card_id
        if verbose:
            print("    带目标出牌 card=%s -> target=%s 箭头=%s targetOverride=%s 回执 ok=%s%s"
                  % (card_id, target_id, out.get("arrows"), out.get("targetOverride_off"),
                     out.get("ok"),
                     "（单位留在待选目标态 ⇒ 接着 confirm 点目标）"
                     if out.get("two_stage_needed") else ""))
        return out

    def remove_target_arrow(self, verbose: bool = True) -> dict:
        """跑游戏自己的 `BP_Logic_C::RemoveTargetArrow()` —— **清掉场上遗留的箭头**。

        ★ 为什么需要：`BP_Logic_C::GetTargetArrowTargetCard` 读的是
          `GetAllActorsOfClass(BP_targetArrowRVX_C)[0]->overCardID`（1.58 导出 BP_Logic.cpp:7051）
          ⇒ 场上只要有一支**遗留箭头**，游戏就可能读到错的那一支（handoff §十五6 那个坑）。
          我们每试坏一次就留一支 ⇒ **下次实验前先用游戏自己的函数清干净**，
          别带着残渣做实验（用户 2026-09-25 提醒："第一次的箭头还在"）。
        """
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        f = self.find_fn(lcls, "RemoveTargetArrow")
        if not f:
            return {"ok": False, "stopped": "BP_Logic_C 上没有 RemoveTargetArrow"}
        self.call0(logic, f)
        self.settle(0.4, 6)           # 等引擎真的把箭头收掉再数剩余数量 ⇒ 按帧等
        left = len(self.arrow_actors())
        if verbose:
            print("    清箭头：RemoveTargetArrow 已调用，场上还剩 %d 支" % left)
        return {"ok": left == 0, "arrows_left": left}

    def aim_arrow(self, target_card_id: int, verbose: bool = True) -> dict:
        """把**场上那支箭头**指向目标卡：写 `overCardID` + 悬停目标 actor（+ 转发）。

        真实玩家"把箭头移到目标卡上方"产生的就是这两件事：
          * 箭头自身的 `overCardID`（`BP_Logic::GetTargetArrowTargetCard` 读的就是它，
            取 `GetAllActorsOfClass(箭头)[0]->overCardID`）—— 场上有几支就都写；
          * 目标 actor 收到 `OnActorMouseEnter` + `MouseHoverDispatch`
            （**这一步游戏会做目标校验并给出 reason**，也是转发给对手的那一步）。

        ★ 用户 2026-09-25 指点的两阶段流程：**先把牌拖到落点"部署"，此时箭头才显示；
          然后才"选目标"** —— 所以 aim 是**第二个动作**，不能和部署挤在一次。
        """
        tgt_actor = self.board_actor_of(target_card_id)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        arrows = self.arrow_actors()
        writes = []
        for a in arrows:
            acls = self.uclass_of_instance(a)
            writes.append((a, self.off(acls, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID),
                           "s32", int(target_card_id)))
        res = {"ok": bool(arrows), "arrows": len(arrows), "target_actor": hex(tgt_actor or 0)}
        if writes:
            res["writeback"] = self.write_and_call(writes, None, 0)   # 只写，不调用
        if tgt_actor:
            f_te = self.find_fn(self.uclass_of_instance(tgt_actor), "OnActorMouseEnter")
            if f_te:
                self.call0(tgt_actor, f_te)
                self.settle(SETTLE_HOVER, 6)
            f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
            if f_disp:
                self.call_ptr(pc, f_disp, tgt_actor)
                self.settle(SETTLE_AFTER_DISPATCH, 2)
            res["hovered_target"] = True
        if verbose:
            print("    箭头指向 %s：箭头数=%d 目标actor=%s 悬停=%s"
                  % (target_card_id, len(arrows), res["target_actor"], res.get("hovered_target")))
        return res

    def select_target(self, target_card_id: int, verbose: bool = True) -> dict:
        """**点一下目标卡**完成"带目标出牌"（正在打的那张牌从游戏自己的字段读）。

        ★★ 2026-09-25 整条重写（用户纠正"选择目标不是 Drag"）。旧实现是
          「写箭头 `overCardID` → 目标 actor 的 `OnActorMouseDown` + `OnActorMouseUp`」
          —— 两个都不对：
            * 板卡 `OnActorMouseUp`（`BP_BoardCard` entry 17498）那条链是"拖**板卡**"
              的提交（里面是 `PlaceBoardCard()`、`ArrangeCardsByLocation`），跟手牌
              带目标出牌不是一条路；
            * 真正的提交是 `BP_Logic::GlobalMouseUp` → `GlobalMouseUpBattle`
              （真人在这种状态下点目标时，PC 因 `consumed=true` **根本不会**把鼠标
              松开转发给目标 actor）。
        现在这里只是 `select_unit_target` 的薄封装：卡号取自
        `BP_Logic->cardBeingPlayedFromHand`（阶段一写上的那个，权威），不给就不点。
        """
        card_id = self.card_being_played_from_hand()
        if not card_id:
            return {"ok": False, "stopped": "BP_Logic->cardBeingPlayedFromHand=0 ⇒ "
                                           "现在不是'等选目标'状态（阶段一没成立）"}
        return self.select_unit_target(int(card_id), target_card_id, verbose=verbose)

    def probe_canplay_targeted(self, card_id: int, target_id: int,
                               verbose: bool = True) -> dict:
        """诊断：**临时**把目标写进卡对象的 `targetOverride`，再问这张卡自己的
        `CanPlayFromHand` 认不认它（读 `canIt` + `Reason`），问完**还原**。

        为什么需要（2026-09-25）：`BP_HandCard::OnActorEndDrag`(entry 13432) 里
        `AttemptToPlayFinal` 一旦 `canPlay=false` 就走 `ClearAndRearrange()`（卡回手、
        箭头销毁）—— 我们要分清是"**卡自己的规则不认这个目标**"还是"**写进去了但后面
        某步没过**"。这一步就把卡自己的回答原话取出来。
        """
        src = self._card_object(card_id)
        tgt = self._card_object(target_id)
        if not src or not tgt:
            return {"ok": False, "stopped": "拿不到 src/tgt 的 UBaseCardObject"}
        ocls = self.uclass_of_instance(src)
        f = self.find_fn(ocls, "CanPlayFromHand")
        if not f:
            return {"ok": False, "stopped": "卡对象上没有 CanPlayFromHand（不在 438 张里）"}
        off_to = self.off(ocls, "targetOverride", NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
        parms = bytearray(0x40)
        out = self.call_raw_hold([(src, off_to, "ptr", tgt)], src, f, bytes(parms))
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw_hold 没返回"}
        res = {"ok": True, "card": card_id, "target": target_id,
               "canIt": bool(buf[0x00]), "source": "game:BaseCardObject::CanPlayFromHand"}
        for key, off in (("reason", 0x08), ("reason_param_1", 0x18),
                         ("reason_param_2", 0x28)):
            ptr, num = struct.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                if raw:
                    res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        # 还原后确认（hall 已还原，再读一次保险）
        res["target_override_after"] = hex(self.m.ptr(src + off_to) or 0)
        if verbose:
            print("    诊断 CanPlayFromHand(card=%s, 目标=%s) -> canIt=%s reason=%r"
                  % (card_id, target_id, res["canIt"], res.get("reason")))
        return res

    def card_being_played_from_hand(self) -> int:
        """`BP_Logic_C::cardBeingPlayedFromHand` —— **正在打的**那张手牌的 CardID（0=没有）。

        两阶段"带目标出牌"的权威状态：阶段一（拖到我方支援线松手）写上它 + 落点，
        **阶段二点完目标才归 0**（1.58 导出 `BP_Logic.cpp:9156/9356`）。
        比"场上有没有箭头"可靠：箭头可能残留一支，这个字段不会。
        """
        logic = self.logic_actor()
        if not logic:
            return 0
        off = self.off(self.uclass_of_instance(logic), "cardBeingPlayedFromHand",
                       NOTE_OFF_LOGIC_PLAYING_FROM_HAND)
        raw = self.m.read_exact(logic + off, 4)
        return struct.unpack("<i", raw)[0] if raw else 0

    def card_being_played_loc(self) -> int:
        """`BP_Logic_C::cardBeingPlayedFromHandLocNum` —— 阶段一定下的**落点**编号。"""
        logic = self.logic_actor()
        if not logic:
            return -1
        off = self.off(self.uclass_of_instance(logic), "cardBeingPlayedFromHandLocNum",
                       NOTE_OFF_LOGIC_PLAYING_LOC)
        raw = self.m.read_exact(logic + off, 4)
        return struct.unpack("<i", raw)[0] if raw else -1

    def selecting_hand_target(self) -> Optional[bool]:
        """`BP_Board_C::isSelectingHandTarget // 0x0C21` —— 游戏自己的"手牌正在选目标"标志。

        同一个判据有原生 getter `BattleUtilityFunctions::HandCardIsSelectingTarget`，
        板卡按下流开头就用它早退；这里直接读字段，**只读、不调用**。
        """
        b = self.board_actor()
        if not b:
            return None
        off = self.off(self.uclass_of_instance(b), "isSelectingHandTarget",
                       NOTE_OFF_BOARD_SELECTING_HAND_TARGET)
        raw = self.m.read_exact(b + off, 1)
        return bool(raw[0]) if raw else None

    def select_unit_target(self, card_id: int, target_id: int,
                           verbose: bool = True,
                           press: bool = True,
                           fallback_up: bool = False) -> dict:
        """**点选一个场上单位作为目标**（提交口 `BP_Logic::GlobalMouseUp`）。★ 语义**不是**"部署"。

        ★★ 2026-09-27 改名 + 语义澄清（用户 2026-09-26 定调：
           "语义上，那个不是部署。虽然好像交互逻辑类似。"）
          这条链**唯一**在做的事是"**指向一个单位**"：`GlobalMouseUp` 读
          `BP_Logic->cardBeingPlayedFromHand` + 箭头 `overCardID` + 卡的 `targetOverride`
          ⇒ `PlaceHandCard(cardBeingPlayedFromHandLocNum, targetCardID)`。
          谁走到这个"待点目标"态**不重要**，两条**语义不同**的来源都汇到这里：
            * **单位**（`selectTargetOnPlayedFromHand=True` 且 `IsUnit`）：
              阶段一（松手）只做视觉落位 + 生成箭头，**必须再点一次目标** ⇒ 整套叫
              `play_card_unit_with_target()`（"部署一个单位并指向"）；
            * **指令/效果**（例：HIDDEN PLANS 选项 "Reveal a Covert unit and give it +1+1."）：
              选项层 resolve 之后游戏把这张**指令**也挂进同一个态 ⇒ 语义是"指向"、不是"部署"。
          所以动词按**语义**叫 `select_unit_target`；旧名 `confirm_deploy_target`
          已于 2026-09-27 **删除**（用户定调："旧名无需保留"）——不留兼容别名，
          免得下一个接手的人又把"部署"当成这条链的全部语义。

        ★★ 2026-09-25 用户当场纠正（"部署时是 EndDrag，选择目标不是 Drag 为什么 EndDrag"）
           + 逐句核实 1.58 导出之后**整条重写**。旧实现发的是
           `OnActorEndDrag`（在"正在打的那张手牌"上），那是**阶段一"部署"的口**，错了。

        真实链路（一手证据：1.58 导出逐句读过）：
          ① 阶段一 `BP_HandCard::OnActorEndDrag`（导出 :1198-1217）对**有目标**的单位
             ⇒ `logic->cardBeingPlayedFromHand = cardID`、
                `logic->cardBeingPlayedFromHandLocNum = 落点`，
                只做**视觉落位 + 生成箭头**（`BP_targetArrowRVX_C`），状态**不清**；
          ② 阶段二 = 真人在目标卡上**单击一次**。PC 的鼠标松开处理器
             （`BP_PlayerController.cpp:683` 一带）做的是：
                `logic->GlobalMouseUp(mouseDownActor, &consumed)`
                `if (consumed) return;`          ← 被吃掉就**不再**转发给 actor
                …→ `mouseDownActor->OnActorMouseUp()`
             ⇒ 箭头举起 + 有目标时 `GlobalMouseUp` 会**吃掉**这次点击：
               `BP_Logic::GlobalMouseUpBattle`（导出 :9297-9376）：
                 · `IsTargetArrowActive()` && `GetTargetArrowTargetCard()` 有目标
                   （读 `箭头->overCardID`，:7051）
                 · `cardBeingPlayedFromHand == 箭头->fromCard->cardID`（:9324）
                 · 卡自己的 `CanPlayFromHand` 出参 `targetedCard` **有效**
                   （`targetedCard` ← `GetTargetedCard` ← `卡对象->targetOverride`；
                    真鼠标那条路上是 `BP_targetArrowRVX::ReceiveTick` **每帧**按鼠标下
                    那张卡写的 —— 导出 `BP_targetArrowRVX.cpp:5883` 那条
                    `fromCard->targetOverride = tmpCurrentCard`）
                 · 再 `CanSelectAsTarget(_targetCard, 卡, true, ...)`
                 ⇒ `PlaceHandCard(cardBeingPlayedFromHandLocNum, targetedCard->cardID)`
                   + `cardBeingPlayedFromHand = 0` + `RemoveTargetArrow()`（:9347-9361）
        所以这里只做三件事：
          1) 悬停目标（`OnActorMouseEnter` + `MouseHoverDispatch`；**这一步会转发给对手**）
          2) 按下那一半（`MouseDownActor->OnActorMouseDown()`，PC 按下时就是这么调的；
             板卡自己的按下流开头就是"手牌正在选目标 ⇒ 直接 return"，不会干扰）
          3) **同一次 JS 执行**：写「每个箭头 `overCardID` + 卡对象 `targetOverride`」
             → 调 `logic->GlobalMouseUp(目标actor, &consumed)`
             （必须同一次：箭头 tick 每帧按真实鼠标重算这些字段，分两次 RPC 就被清。）
        ★ 不要发：`OnActorEndDrag`（阶段一的口）；**目标板卡**的 `OnActorMouseUp`
          —— `BP_BoardCard::OnActorMouseUp`（:1335-1425）那条链是"拖**板卡**"的提交
          （里面是 `PlaceBoardCard()`），跟手牌带目标出牌不是一条路；而且真人在这种
          状态下点目标时，PC 因为 `consumed=true` 根本**不会**调 target 的 MouseUp。
        回执：动作流 `XActionPlayCardFromHand`（带 `targetCardID`=目标）。
        """
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        f_gmu = self.find_fn(lcls, "GlobalMouseUp") or self.find_fn(lcls, "GlobalMouseUpBattle")
        if not f_gmu:
            return {"ok": False, "stopped": "BP_Logic_C 上没有 GlobalMouseUp/GlobalMouseUpBattle"}
        playing = self.card_being_played_from_hand()
        loc = self.card_being_played_loc()
        arrows = self.arrow_actors()
        out = {"playing_from_hand": playing, "loc_num": loc, "arrows": len(arrows),
               "selecting_hand_target": self.selecting_hand_target(),
               "call": "GlobalMouseUp"}
        if playing and int(playing) != int(card_id):
            out.update(ok=False,
                       stopped="BP_Logic->cardBeingPlayedFromHand=%s 不是 %s ⇒ 现在等的不是这张牌"
                               % (playing, card_id))
            return out
        if not arrows:
            out.update(ok=False, stopped="场上没有箭头 ⇒ 阶段一没成立，无从点目标")
            return out
        tgt_actor = self.board_actor_of(target_id)
        if not tgt_actor:
            out.update(ok=False, stopped="场上找不到目标 card %s 的 actor" % target_id)
            return out
        out["target_actor"] = hex(tgt_actor)
        src_obj = self._card_object(card_id)
        tgt_obj = self._card_object(target_id)
        if not src_obj or not tgt_obj:
            out.update(ok=False, stopped="拿不到 src/tgt 的 UBaseCardObject")
            return out
        off_to = self.off(self.uclass_of_instance(src_obj), "targetOverride",
                          NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
        # ---- 1) 悬停（真人点击前一定先悬停；**这一步会转发给对手**）----
        tcls = self.uclass_of_instance(tgt_actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(tcls, "OnActorMouseEnter")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        f_down = self.find_fn(tcls, "OnActorMouseDown")
        out["send"] = {"enter": bool(f_enter), "hover_dispatch": bool(f_disp),
                       "down": bool(f_down)}
        if f_enter:
            self.call0(tgt_actor, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, tgt_actor)       # 悬停转发（对手看得到的那一步）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ---- 2) 按下那一半（PC 在鼠标按下时调的就是这个）----
        if press and f_down:
            self.call0(tgt_actor, f_down)
            self.settle(SETTLE_AFTER_DOWN, 2)
        # ---- 3) 写 + GlobalMouseUp，**同一次 JS 执行** ----
        #   箭头那几项标 `hold=True`（不还原）：`GlobalMouseUpBattle` 内部会
        #   `DestroyAllActorsOfClass(箭头)`，不该再去写可能已回收的内存；
        #   卡对象那项必须还原（那是为复刻悬停临时写的，游戏自己也是探测完就清）。
        writes = [(src_obj, off_to, "ptr", tgt_obj)]
        for a in arrows:
            writes.append((a, self.off(self.uclass_of_instance(a), "overCardID",
                                       NOTE_OFF_ARROW_OVER_CARD_ID), "s32", int(target_id), True))
        out["writes"] = len(writes)
        parms = bytearray(0x10)
        struct.pack_into("<Q", parms, 0x00, int(tgt_actor))     # MouseUpActor @0x00
        mk = self._mark()
        try:
            out["writeback"] = self.call_raw_writes(writes, logic, f_gmu, bytes(parms))
        except Exception as e:                                     # noqa: BLE001
            out.update(ok=False, error=str(e))
            return out
        buf = out["writeback"] or b""
        out["consumed"] = bool(buf[0x08]) if len(buf) > 0x08 else None
        out.update(self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0))
        out["playing_after"] = self.card_being_played_from_hand()
        out["selecting_after"] = self.selecting_hand_target()
        out["target_override_after"] = hex(self.m.ptr(src_obj + off_to) or 0)
        # `consumed=False` ⇒ 游戏没吃这次点击。真实客户端这时才把鼠标松开转发给
        # actor（`PC: if (!consumed) …->OnActorMouseUp()`）；默认**不**补，因为那会跑
        # 目标板卡那条"拖板卡"的提交链（`PlaceBoardCard()`），只在诊断时才开。
        if fallback_up and out.get("consumed") is False:
            f_up = self.find_fn(tcls, "OnActorMouseUp")
            if f_up:
                self.call0(tgt_actor, f_up)
                out["fallback_up"] = "OnActorMouseUp"
        if verbose:
            print("    点目标 card=%s -> target=%s 箭头=%d consumed=%s 回执 ok=%s"
                  "（playing %s→%s）"
                  % (card_id, target_id, len(arrows), out.get("consumed"), out.get("ok"),
                     playing, out.get("playing_after")))
        return out

    # -------------------------------------------- 出牌：四个语义动词（2026-09-27 用户定的动作空间）
    def play_card_event(self, card_id: int, force: bool = False, verbose: bool = True,
                        require_inactive: bool = False) -> dict:
        """**打出指令/反制**（无目标）。

        * **指令**：拖到落点松手即成交（`AttemptToPlayFinal`）。
        * **反制（gotcha）**："打出"就是**在手里激活**（`XActionPlayCardFromHand`
          `location=Hand_Left` + 扣费，卡留手里、`gotchaActivated>0`）。
          ★ 用户口径（2026-09-27）："反制可以被多次打出（**切换**是否激活）" ⇒ 本函数
          **默认允许重复打出**，并回 `gotcha_activated_before/after` + `toggled_off`
          把"这次是激活还是取消"说清楚；要"只在未激活时打"传 `require_inactive=True`。
        """
        return self.play_card(card_id, force=force, verbose=verbose,
                              require_inactive=require_inactive)

    def play_card_event_with_target(self, card_id: int, target_id: int,
                                    verbose: bool = True) -> dict:
        """**打出指向性指令**（指令的目标在**松手那一次**成交）。

        实机配方（2026-09-27，`FOR FREEDOM` = 手牌4 → 我方 `5th RANGERS` = 场上24）：
            XActionPlayCardFromHand{cardID:4, targetCardID:24}
              ZActionPlayCardFromHand{cardID:4, location:Discard, targetCardID:24}
              ZActionGainAttack {cardID:24, +1 → 9, permBuff, instigator:4}
              ZActionGainDefense{cardID:24, +1 → 4, permBuff, instigator:4}
              ZActionDrawCardFromDeck{cardID:33}
        为什么**一次就成交、没有第二阶段**（一手证据，逐句读过）：
          `selectTargetOnPlayedFromHand=True` 的**指令**在松手那一次走
          `BP_HandCard.cpp:1204`（`!IsUnit` ⇒ `Label_15862`）→ `BP_PlayerMoves::AttemptToPlayFinal`
          （`:2183-2198`：读卡自己 `CanPlayFromHand` 的出参 `targetedCard` ←
          `BP_CardFunctions::GetTargetedCard` ← `卡对象->targetOverride`；给了 `targetOverride`
          就等于给了目标）⇒ `Label_15772 PlaceHandCard(LocationNumberUnderCursor, targetCardID)`。
          ⇒ **指令**不存在"待点目标"态；只有**单位**会停在那一刻等第二次点击
          （见 `play_card_unit_with_target`）。

        但**选项层之后的指令**（HIDDEN PLANS：`chooseOneCards` 非空）例外：出牌只把选项面板
        弹出来（`BP_HandCard.cpp:1191-1196` ⇒ `Label_16022`），选项 resolve 之后游戏才把
        这张卡挂进"待点目标" ⇒ 这时本函数会把 `needs_unit_click=True` 交回给调用方，
        由 `pick_choice()` + `select_unit_target()` 接着做（两步语义不同，别混成一步）。
        目标可以是**任意单位**（敌我皆可，卡面写 "Give **a unit** +1+1."）——
        合法性只问游戏自己的 `CanSelectAsTarget`（`play_card_targeted` 里那一道）。
        """
        r = self.play_card_targeted(card_id, int(target_id), verbose=verbose)
        playing = self.card_being_played_from_hand()
        r["playing_after"] = playing
        if not r.get("ok") and playing:
            r["needs_unit_click"] = int(playing)
            r["next"] = ("游戏停在'待点目标'（playing=%s）⇒ 接着 select_unit_target(%s, %s)"
                         % (playing, playing, target_id))
        return r

    def play_card_unit(self, card_id: int, slot: Optional[int] = None,
                       force: bool = False, verbose: bool = True) -> dict:
        """**打出单位**，可指定**站位**（`slot` = 该排的**空隙序号**，见 `gap_number`）。

        ★ 站位的合法域只有**我方支承线/后排**：实测游戏**不接受"直接部署到前线"**
          （`play_card(location="front")` 写 `LocationUnderCursor=7` 也照样落支承线，8/8）。
          上前线只能走 `move_to_front`（付操作费）。所以 `slot` 表达的是"这一行里第几格"，
          **不是**"放哪一行"；`slot=None` 就自动挑空槽。

        ★ 2026-09-27 用户纠正后的语义：`slot` 是**空隙序号**（"拖到谁和谁之间"）不是格子号 ——
          `0` = 最左（插到所有卡的左边）、`n` = 最右（插到所有卡的右边）；游戏再把空隙换成
          `LocationNumberUnderCursor`（= 「光标左边那些卡的最大列号 + 1」）并按它插入、
          密集重排。有空洞时"空隙序号 ≠ 最终格号"。
        """
        return self.play_card(card_id, force=force, verbose=verbose,
                              location="back", slot=slot)

    def play_card_unit_with_target(self, card_id: int, target_id: Optional[int] = None,
                                   slot: Optional[int] = None, force: bool = False,
                                   verbose: bool = True) -> dict:
        """**打出"部署时需要指向"的单位**；**没有合法目标就退化成 `play_card_unit`**。

        用户口径（2026-09-27）："如果无可指向目标，一般单位便无需指向即可打出，
        走上一路径。" ⇒ 两种情况退化：
          ① `target_id is None`；
          ② 给了目标，但**游戏自己的** `CanSelectAsTarget(目标, 这张卡, byPlayFromHand=True)`
             说不行（没有合法目标 / 目标已死 / 类型不对）⇒ 直接当普通单位打出
             （`fell_back_to_plain=True` + 原话放在 `target_rejected` 里）。
        有合法目标时：阶段一松手只做"视觉落位 + 生成箭头"，**必须再点一次目标**
        （阶段二 `select_unit_target`），并等箭头**异步**出现（实测 0.43 s）。

        为什么阶段一先试"一次性路径"（`play_card_targeted`）：指令卡（GUNSHIP MISSION /
        AIR SUPERIORITY 那类"松手即成交"）在这一步就成交；只有**单位**会停在
        "视觉落位 + 箭头"（`cardBeingPlayedFromHand` 非 0）⇒ 这时才走第二阶段。
        早先的写法（阶段一不带 target 直接 `play_card(force=True)`）对指令卡是**错的**：
        松手时 `AttemptToPlayFinal` 找不到目标 ⇒ `ClearAndRearrange` ⇒ 卡直接回手。
        ⚠ 这里不要留"看起来能越过判据、其实没读"的假参数（CLAUDE.md 弯路 #11）。
        """
        # ---- 退化路径 ①：没给目标 ----
        if target_id is None:
            r = self.play_card_unit(card_id, slot=slot, force=force, verbose=verbose)
            r["fell_back_to_plain"] = True
            r["fallback_reason"] = "no_target_given"
            return r
        # ---- 退化路径 ②：问了游戏，它说这个目标不行 ----
        sel = self.game_can_select_as_target(int(target_id), card_id, True, verbose=False)
        if sel.get("ok") and sel.get("can") is False:
            r = self.play_card_unit(card_id, slot=slot, force=force, verbose=verbose)
            r["fell_back_to_plain"] = True
            r["fallback_reason"] = "target_rejected"
            r["target_rejected"] = {"target": target_id, "reason": sel.get("reason")}
            if verbose:
                print("    目标 %s 被游戏否掉（reason=%r）⇒ 按**无目标**打出（用户口径）"
                      % (target_id, sel.get("reason")))
            return r
        out = {"card": card_id, "target": target_id}
        r1 = self.play_card_targeted(card_id, int(target_id), verbose=verbose, slot=slot)
        out["phase1"] = {k: r1.get(k) for k in ("ok", "reason", "error")}
        out["phase1"]["ok"] = bool(r1.get("ok"))
        pending = self.card_being_played_from_hand()
        out["playing_after_phase1"] = pending
        if not pending:
            out["committed"] = "phase1（一次性：targetOverride + EndDrag）"
            out["ok"] = bool(r1.get("ok"))
            return out
        # ★ 2026-09-26 实机（108gen→59gen 那次失败）：**箭头是异步出现的** ——
        #   阶段一结束时读 `arrow_actors()` 还是 0，稍后才有 1 支；`select_unit_target`
        #   立刻去点就会报"场上没有箭头 ⇒ 阶段一没成立"。⇒ 点目标前**先等箭头出现**。
        t_wait = time.time()
        while time.time() - t_wait < 2.0 and not self.arrow_actors():
            time.sleep(0.1)
        out["arrow_waited"] = round(time.time() - t_wait, 2)
        out["arrows_after_wait"] = len(self.arrow_actors())
        r2 = self.select_unit_target(int(pending), int(target_id), verbose=verbose)
        out["phase2"] = {k: r2.get(k) for k in ("ok", "consumed", "reason", "stopped",
                                                "playing_after", "error")}
        out["committed"] = "phase2（点目标）"
        out["ok"] = bool(r2.get("ok")) and not r2.get("playing_after")
        if verbose:
            print("    两阶段 card=%s -> target=%s：一次性 ok=%s → 点目标 ok=%s consumed=%s"
                  % (card_id, target_id, r1.get("ok"), r2.get("ok"), r2.get("consumed")))
        return out

    def hand_target_widget(self) -> int:
        """`BP_Board_C::selectHandTargetWidget` —— 等"选一张手牌当目标"时那个确认面板。"""
        b = self.board_actor()
        if not b:
            return 0
        off = self.off(self.uclass_of_instance(b), "selectHandTargetWidget",
                       NOTE_OFF_BOARD_SELECT_HAND_WIDGET)
        return self.m.ptr(b + off) or 0

    def hand_target_pending(self, verbose: bool = True, with_legal: bool = True) -> dict:
        """**只读**：现在是不是在等"点一张手牌当目标"（`selectTargetOnPlayedFromHand` 那一类效果）。

        机制（1.58 导出逐句读过）：
          * 源卡打完时调 `BP_CardFunctions::selectTargetFromHand(cardID)`（导出 :5305）
            ⇒ `NotifySelectHandTargetPending` ⇒ 服务端动作流里是
            `ZActionSelectHandTargetPending{cardBeingPlayed}`（**实机见过**：
            THE AMERICAN GUARD "Deployment: Choose a card in hand. Convert the top card
            of your deck into it."）；
          * 客户端 `ConfirmHandTargetButton_Widget::Construct` 里把
            `Board->isSelectingHandTarget = true` / `chooseOneActive = true` /
            `Board->selectHandTargetWidget = this`；
          * 玩家**点手牌** ⇒ `BP_HandCard::OnActorClicked`（entry 30474，
            `BranchOnPlatformType` 桌面分支）→ `selectHandTarget()`（导出 :5262）
            ⇒ `widget->setTarget(this)` + 给对手发 `toggle_select_hand_target;<cardID>`；
          * 玩家**点确认** ⇒ 本 widget 的
            `BndEvt__StatButton_K2Node_ComponentBoundEvent_0_onClicked__DelegateSignature`
            （entry 2468）⇒ `_playerMoves->AddMoveToPlayerMoveQueue(..., "handTargetSelected",
            targetCardID, 0, cardBeingPlayed)` → 源卡的 `OnHandTargetSelected(...)`。
        """
        b = self.board_actor()
        w = self.hand_target_widget()
        res = {"board_actor": hex(b) if b else None,
               "is_selecting_hand_target": self.selecting_hand_target(),
               "widget": hex(w) if w else None}
        if not w:
            res["pending"] = False
            if verbose:
                print("    没有 selectHandTargetWidget ⇒ 不在等选手牌")
            return res
        wcls = self.uclass_of_instance(w)
        off_cbp = self.off(wcls, "cardBeingPlayed", NOTE_OFF_HTGT_WIDGET_CARD_ID)
        off_tgt = self.off(wcls, "targetCardID", NOTE_OFF_HTGT_WIDGET_TARGET)
        off_obj = self.off(wcls, "_cardBeingPlayedObject", NOTE_OFF_HTGT_WIDGET_CARD_OBJ)
        raw = self.m.read_exact(w + off_cbp, 4)
        res["card_being_played"] = struct.unpack("<i", raw)[0] if raw else None
        raw = self.m.read_exact(w + off_tgt, 4)
        res["target_card_id"] = struct.unpack("<i", raw)[0] if raw else None
        res["source_card_obj"] = hex(self.m.ptr(w + off_obj) or 0)
        res["pending"] = bool(res["is_selecting_hand_target"])
        if res["pending"] and with_legal:
            # 每张手牌能不能选 —— 用游戏自己的 `IsValidHandTarget`（"确认"按钮灰不灰同源）
            cand = []
            for r in hand_card_actors(self.ks):
                lg = self.hand_target_legal(r.get("card_id"))
                cand.append({"card_id": r.get("card_id"), "name": r.get("name"),
                             "valid": lg.get("is_valid"), "reason": lg.get("reason")})
            res["candidates"] = cand
        if verbose:
            print("    等选手牌：pending=%s 源卡=%s 已点中=%s widget=0x%X"
                  % (res["pending"], res["card_being_played"], res["target_card_id"], w))
        return res

    def hand_target_legal(self, hand_card_id: int, verbose: bool = False) -> dict:
        """**只读**：`源卡.IsValidHandTarget(这张手牌, isIt, Reason)` —— 游戏自己判"这张能不能选"。

        这是"确认"按钮灰不灰（`greyInvalidTargets()`）用的同一个函数；
        `Reason` 是 FString（出参，在 parms 里是 16 字节的 FString 结构）。
        """
        w = self.hand_target_widget()
        if not w:
            return {"ok": False, "stopped": "不在等选手牌状态（没有 selectHandTargetWidget）"}
        wcls = self.uclass_of_instance(w)
        src = self.m.ptr(w + self.off(wcls, "_cardBeingPlayedObject",
                                      NOTE_OFF_HTGT_WIDGET_CARD_OBJ)) or 0
        if not src:
            off_cbp = self.off(wcls, "cardBeingPlayed", NOTE_OFF_HTGT_WIDGET_CARD_ID)
            raw = self.m.read_exact(w + off_cbp, 4)
            cid = struct.unpack("<i", raw)[0] if raw else 0
            src = self._card_object(cid) or 0
        tgt = self._card_object(hand_card_id)
        if not src or not tgt:
            return {"ok": False, "stopped": "拿不到源卡/候选手牌的 UBaseCardObject"}
        scls = self.uclass_of_instance(src)
        f = self.find_fn(scls, "IsValidHandTarget")
        if not f:
            return {"ok": False, "stopped": "源卡类上没有 IsValidHandTarget"}
        parms = bytearray(0x20)
        struct.pack_into("<Q", parms, 0x00, tgt)
        out = self.api().call_raw(hex(src), hex(f), bytes(parms).hex())
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        res = {"ok": True, "hand_card": hand_card_id,
               "is_valid": bool(buf[0x08]), "source": "game:BaseCardObject::IsValidHandTarget"}
        ptr, num = struct.unpack_from("<Qi", buf, 0x10)
        if ptr and num and 1 < num <= 4096:
            raw = self.m.read_exact(ptr, num * 2)
            if raw:
                res["reason"] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if verbose:
            print("    IsValidHandTarget(手牌 %s) -> %s reason=%r"
                  % (hand_card_id, res["is_valid"], res.get("reason")))
        return res

    def select_hand_target(self, hand_card_id: int, verbose: bool = True,
                           confirm: bool = True) -> dict:
        """**点一张手牌当目标**，然后点"确认"按钮 —— 复刻真人的两下点击。

        为什么这么写（用户 2026-09-25 的纪律：悬停不能省、点击要还原真实序列）：
          1) 悬停手牌 actor（`OnActorMouseEnter`）+ 悬停转发
             （`PC->MouseHoverDispatch`，**对手看得到**的那一步）；
          2) `手牌->OnActorClicked(1)` —— 真实左键点击在桌面分支就走这一支
             （entry 30474 → `selectHandTarget()`）；该函数会给对手发
             `toggle_select_hand_target;<cardID>`，所以**不能**跳过它直接写
             `widget->targetCardID`（那样信息流跟真人不一样）；
          3) 读回 `widget->targetCardID` 核对这一下真选中了没（不一致就返回、不点确认）；
          4) 点确认：`BndEvt__StatButton_..._onClicked__DelegateSignature`（entry 2468）
             ⇒ 入队 `handTargetSelected` → 源卡 `OnHandTargetSelected(...)`。
        """
        w = self.hand_target_widget()
        if not w:
            return {"ok": False, "stopped": "不在等选手牌状态"}
        rec = self.hand_actor(hand_card_id)
        if not rec:
            return {"ok": False, "stopped": "手牌 %s 找不到 actor" % hand_card_id}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        wcls = self.uclass_of_instance(w)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        f_click = self.fn(cls, "OnActorClicked")
        f_conf = self.find_fn(
            wcls, "BndEvt__StatButton_K2Node_ComponentBoundEvent_0_onClicked__DelegateSignature")
        res = {"hand_card": hand_card_id, "actor": hex(actor), "widget": hex(w),
               "send": {"enter": bool(f_enter), "hover_dispatch": bool(f_disp),
                        "click": bool(f_click), "confirm": bool(f_conf)}}
        if f_enter:
            self.call0(actor, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, actor)          # 悬停转发（对手可见）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        if not f_click:
            return dict(res, ok=False, stopped="手牌 actor 上没有 OnActorClicked")
        # ★ 2026-09-26 重构：走统一点击原语（L0 队列闸门 + L1 PC 状态 + `GlobalMouseUp` 那一跳）。
        #   旧写法直接 `call_i32(OnActorClicked, 1)`，等于跳过了 PC 的松手流程。
        res["click"] = self.click_actor(actor, is_precise=1, verbose=True)
        self.settle(SETTLE_AFTER_DOWN, 2)
        # 核对：widget 是不是认下了这一张
        off_tgt = self.off(wcls, "targetCardID", NOTE_OFF_HTGT_WIDGET_TARGET)
        raw = self.m.read_exact(w + off_tgt, 4)
        got = struct.unpack("<i", raw)[0] if raw else None
        res["widget_target_card_id"] = got
        if got != hand_card_id:
            return dict(res, ok=False, reason="click_not_registered",
                        stopped="点了手牌但 widget->targetCardID=%s（期望 %s）"
                                % (got, hand_card_id))
        if not confirm:
            return dict(res, ok=True, confirmed=False)
        if not f_conf:
            return dict(res, ok=True, confirmed=False,
                        stopped="找不到确认按钮的回调（已选中，未确认）")
        mk = self._mark()
        # ★ 2026-09-26（实机验证）**真按钮在子控件上**：这个 widget 自己只有
        #   `BndEvt__StatButton_..._onClicked` 一个句柄（直接调它 = 假点击），
        #   真按钮是它身上的 `confirmButton // 0x0368`
        #   （`verticalKardsButtonWithText_Widget_C`，和"投降"按钮同一个类），
        #   四件套在**子按钮**上（该类没有自己的 clicked —— click 走父回调）。
        #   实机：子按钮 hover/press/release + 父 onClicked → 动作流 `XActionHandTargetSelected`。
        off_btn = self.off(wcls, "confirmButton", 0x0368)
        cbtn = self.m.ptr(w + off_btn) or 0
        res["confirm_button"] = hex(cbtn)
        if cbtn:
            bcls = self.uclass_of_instance(cbtn)
            res["confirm_button_class"] = self.pool.fname_of(bcls)
            # 四步之间的停顿改按帧等（和换牌确认同一修法）：失焦降帧时裸 sleep 里游戏一帧没跑，
            # 按下/松开态不会渲染（2026-10-01 根因）。
            for suffix, pause, frames in (("OnButtonHoverEvent", 0.25, 6), ("OnButtonPressedEvent", 0.08, 4),
                                          ("OnButtonReleasedEvent", 0.05, 3), ("OnButtonClickedEvent", 0.20, 6)):
                f = None
                for n in (0, 1, 2, 5, 7):
                    f = self.find_fn(
                        bcls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_%d_%s__DelegateSignature"
                              % (n, suffix))
                    if f:
                        break
                if f:
                    self.call0(cbtn, f)
                    self.settle(pause, frames)
                    res.setdefault("confirm_button_calls", []).append(suffix)
        self.call0(w, f_conf)
        res["confirm_called"] = True
        # 动作流：这一步产生的是源卡的部署结算（THE AMERICAN GUARD 是"把牌库顶变成它"），
        # 具体 action_type 随卡不同，所以这里取"这一步之后新出现的全部动作"。
        from kardsmem.matchlog import MatchLog
        ml = self.matchlog()          # ★ 长寿命单例（locate 4~6 s，别再每次新建）
        t0 = time.time()
        rows = []
        while time.time() - t0 < 3.0:
            rows = ml.since(mk)
            if rows:
                break
            time.sleep(0.05)
        res["actions"] = rows
        res["ok"] = bool(rows)
        res["waited"] = round(time.time() - t0, 2)
        if verbose:
            print("    点手牌 %s：悬停→点击→确认 ⇒ 动作 %s"
                  % (hand_card_id, [r.get("action_type") for r in rows]))
        return res

    def board_card_screen_pos(self, card_id: int):
        """场上卡 → **屏幕像素**（(viewport_x, viewport_y, screen_x, screen_y, hwnd)）。

        `AActor::K2_GetActorLocation()`（原生 const，出参 `FVector@0x00`，ParmsSize 0x18）
        → `APlayerController::ProjectWorldLocationToScreen(FVector@0x00, FVector2D*@0x18,
           bool bPlayerViewportRelative@0x28, bool* ReturnValue@0x29)`（ParmsSize 0x30）
        → 视口坐标 → `win.client_to_screen()` 换成桌面像素。
        **两个都是原生只读**，不写任何状态。

        ★ 用途（2026-09-25 更正过一次）：这是**只读几何**，跟"要不要用真实鼠标"无关。
          曾用它给"物理鼠标点目标"那条路求坐标 —— **那条路用户已经否掉**
          （"不要真实鼠标"），物理鼠标的 `mouse_to_target`/`click_target_with_mouse`
          两个函数已删除。留着它是为了**交叉核对**：算出来的屏幕坐标可以用来确认
          "光标底下到底是哪张卡"（例如 `FindCardLocationUnderCursor` 的对照），
          它本身不写任何状态、不碰鼠标。
        """
        atk = self.board_actor_of(card_id)
        if not atk:
            return None
        acls = self.uclass_of_instance(atk)
        f_loc = self.find_fn(acls, "K2_GetActorLocation")
        if not f_loc:
            return None
        out = self.api().call_raw(hex(atk), hex(f_loc), "00" * 0x18)
        buf = bytes(out) if out else b""
        if len(buf) < 0x18:
            return None
        wx, wy, wz = struct.unpack_from("<ddd", buf, 0)
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f_prj = self.find_fn(pcls, "ProjectWorldLocationToScreen")
        if not f_prj:
            return None
        parms = bytearray(0x30)
        struct.pack_into("<ddd", parms, 0x00, wx, wy, wz)
        out2 = self.api().call_raw(hex(pc), hex(f_prj), parms.hex())
        buf2 = bytes(out2) if out2 else b""
        if len(buf2) < 0x30 or not buf2[0x29]:
            return None
        vx, vy = struct.unpack_from("<dd", buf2, 0x18)
        # ★ 2026-09-27：**不再 import ops**（旧鼠标实现要归档）—— 只要一个窗口句柄，
        #   直接用 `base/winapi.py` 找游戏窗口，**不置前**（只读诊断）。
        from base import winapi
        h = winapi.find_game_hwnd()
        if not h:
            return None
        sx, sy = winapi.client_to_screen(h, int(vx), int(vy))
        return (int(vx), int(vy), int(sx), int(sy), h)

    def click_board_card(self, card_id: int, verbose: bool = True) -> dict:
        """**点一张场上的卡**（复刻真实左键单击）：悬停 → 悬停转发 → 按下 → 松开。

        ⚠ 2026-09-25 更正：**这不是"部署后选目标"的做法**（旧注释在这里写错过）。
          选目标那条路的提交在 `BP_Logic::GlobalMouseUp` →
          `GlobalMouseUpBattle`（`PlaceHandCard(locNum, targetCardID)`），而且真人在
          那种状态下点目标时 PC 因为 `consumed=true` **不会**把鼠标松开转发给目标
          actor ⇒ 用 `select_target()`/`select_unit_target()`，别用这个。
          这个函数留着当**通用"点一张板卡"**（悬停+按下+松开都发，含转发给对手），
          用于需要"我点一下这张卡"的其它场合。
        悬停那一步不能省：**游戏在这一步做目标校验并给 reason**，而且真人点击本来
        就会先悬停；`MouseHoverDispatch` 是转发给对手的那一步。
        """
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的 actor" % card_id}
        cls = self.uclass_of_instance(atk)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        f_down = self.find_fn(cls, "OnActorMouseDown")
        f_up = self.find_fn(cls, "OnActorMouseUp")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        if f_enter:
            self.call0(atk, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, atk)          # 悬停转发（对手看得到的那一步）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        if f_down:
            self.call0(atk, f_down)
            self.settle(SETTLE_AFTER_DOWN, 2)
        if f_up:
            self.call0(atk, f_up)
        out = {"ok": True, "card_id": card_id, "actor": hex(atk),
               "send": {"enter": bool(f_enter), "down": bool(f_down), "up": bool(f_up),
                        "hover_dispatch": bool(f_disp)}}
        if verbose:
            print("    点场上卡 %s(actor=0x%X)：悬停→转发→按下→松开" % (card_id, atk))
        self.settle(0.4, 6)           # 点完等界面反应 ⇒ 按帧等（降帧时不空等）
        return out

    def _free_slot(self) -> Optional[int]:
        """我方后排的**空** locationNumber；**满了返回 None**（支援线 4 个单位 + 总部 = 0..4）。

        ⚠ 这里只在"没有真实鼠标位置"这一点上做替代：写进去的是真实合法的空位编号，
          游戏那边照样会自己校验（不合法就取消，不会发出去）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        used = {c.slot for c in st.cards
                if c.side == "local" and c.location in ("back", "hq") and c.slot is not None}
        for i in range(0, 5):                                # 后排只有 5 格（含总部）
            if i not in used:
                return i
        return None

    def _free_front_slot(self) -> Optional[int]:
        """我方**前线**的空 locationNumber（前线 4 格：0..3）。满了返回 None。"""
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        used = {c.slot for c in st.cards
                if c.side == "local" and c.location == "frontline" and c.slot is not None}
        for i in range(0, 4):
            if i not in used:
                return i
        return None

    # ------------------------------------------------ 站位（列号）与「空隙序号」
    # ★★ 2026-09-27 用户实机纠正：**"你实际上把 108 部署在了最右侧"** ——
    #   我按"请求 slot=1 = 第 1 格"理解站位，那是**错的**。用户给的权威模型：
    #   > 一般真人部署时，将单位拖拽到所需的位置：**两个单位中间，或最右最左侧，放手**。
    #   > 场面上的渲染会把单位挪开一些。最终部署时显示最终渲染。
    #   也就是说：**站位是"拖到哪个空隙"，不是"第几格"**；游戏再把"拖到哪"换算成
    #   列号，并按"谁在你左边"插进去、密集重排。换算链（`BP_Board.cpp`，1.58 导出；
    #   1.60 只有字段偏移变、逻辑同）：
    #
    #     FindCardLocationUnderCursor()          # **纯出参**：读真实鼠标 → 反投影 → 射线
    #       → 命中 `Board_HQLeft` / `Board_HQRight` / `Board_Frontline` 那一排
    #         （`GetObjectName(component)` 以 `Board_` 开头，`Board_HQLeft` ⇒ locEnum 5）
    #       → FindLocationNumberForCoordinates(命中点坐标, locEnum)
    #     FindLocationNumberForCoordinates(coords, loc):          # BlueprintPure
    #       highest = -1
    #       for 该排每张卡（GameState.FetchCardsByLocation 的顺序）:
    #           if 视觉板卡拿不到:            continue
    #           if 视觉板卡.doingConvertVisualAsNewCard:  return   # 变身动画中，整条不接
    #           n = 视觉板卡.cardLocationNumber                      # 它当前的列号
    #           视觉卡 = GetVisualCardFromID(该卡)                    # = 同一 actor（BP_BoardCard_C
    #                                                                #   继承 BP_BaseCard_C）
    #           if 视觉卡为空: goto Label_1604
    #           GetActorBounds → center, extent
    #           if NearlyEqual(center.X, cursor.X, extent.X - 2):
    #               cardAtLocation = 该卡; return                    # "光标底下是这张卡"
    #     Label_1604:
    #           if center.X < cursor.X and highest < n: highest = n
    #       locationNumber = highest + 1
    #
    #   ⇒ **写进 `LocationNumberUnderCursor` 的那个数 = 「光标左边那些卡的最大列号 + 1」**。
    #     要表达"插到第 i 个空隙"（0 = 最左，n = 最右），得写
    #         `numbers[i-1] + 1`（i>0），i==0 写 0        （numbers = 该排已排序的列号）
    #     ★ 它**不是最终格子号，是比较键**：行有空洞时两者不同（例如列号 [0,2] 的空隙 1
    #       要写 1 而不是 2；空洞行请求 3 ⇒ 最终落在 2，压实）。稠密行上两者相等，
    #       所以以前"稠密行 request=final"的观测看着像对的 —— 它掩盖了这个区别。
    #     ⚠ `_free_slot()`（默认路径）扫的是**第一个空列号**，在稠密行上恰好等于"追加到最右"；
    #       那次 slot 被吞掉落到 3（= 最右）就是这么来的：`_free_slot()` 回了 3。
    #
    #   验算方式是**问游戏自己**（`probe_gap_numbers()`）：用相邻两张卡的**渲染位置**
    #   构造空隙坐标，调 `BP_Board_C::FindLocationNumberForCoordinates` 读出参。
    #   该函数 `BlueprintCallable/BlueprintPure`、只写出参 ⇒ 红线允许的**只读查询**。

    def _row_cards(self, row: str = "back", side: str = "local") -> list:
        """该排的 `(列号, card_id)`，按列号升序。

        ★ `row="back"` **含总部** —— 总部和支援线单位同属 `locEnum=5`（`Board_HQLeft`），
          共用一套列号（`hq` 不是单独一排）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        locs = ("back", "hq") if row == "back" else ("frontline",)
        cs = [c for c in st.cards
              if c.side == side and c.location in locs and c.slot is not None]
        cs.sort(key=lambda c: (c.slot, c.uid))
        return [(int(c.slot), int(c.card_id)) for c in cs]

    @staticmethod
    def gap_request_key(numbers, gap: int) -> int:
        """「第 `gap` 个空隙」→ `LocationNumberUnderCursor` 该写的数（见上面那段推导）。

        `gap=0` ⇒ 最左（谁都不在光标左边 ⇒ 0）；`gap=n` ⇒ 最右（所有卡都在左边）。
        ★ 有空洞时 `numbers[i-1]+1 != i`，这正是要算而不是直接写 `i` 的原因。
        """
        nums = sorted(int(x) for x in numbers)
        if not nums or int(gap) <= 0:
            return 0
        return nums[min(int(gap), len(nums)) - 1] + 1

    def actor_bounds(self, actor: int):
        """`Actor::GetActorBounds(false, Origin, BoxExtent, false)` → `(origin, extent)`；只读。

        参数布局（**当前构建**）：`reverse-data/sdk/1.60.27292.launcher/CppSDK/SDK/
        Engine_parameters.hpp:3520` —— `bOnlyCollidingComponents@0x00` / `Origin@0x08` /
        `BoxExtent@0x20` / `bIncludeFromChildActors@0x38`（ParmsSize 0x40）。
        """
        if not actor:
            return None
        f = self.find_fn(self.uclass_of_instance(actor), "GetActorBounds")
        if not f:
            return None
        parms = bytearray(0x40)
        out = self.api().call_raw(hex(actor), hex(f), parms.hex())
        buf = bytes(out) if out else b""
        if len(buf) < 0x38:
            return None
        return (struct.unpack_from("<ddd", buf, 0x08),
                struct.unpack_from("<ddd", buf, 0x20))

    def game_location_number_for_coords(self, coords, location_enum: int) -> Optional[dict]:
        """问**游戏自己**：这个世界坐标落在该排的第几号（**只读**，不改任何状态）。

        调用 `BP_Board_C::FindLocationNumberForCoordinates(coords, loc)`。
        参数布局（**当前构建**）：`.../1.60.27292.launcher/CppSDK/SDK/BP_Board_parameters.hpp:4000`
        —— `Coordinates@0x00`(FVector 0x18) / `Location@0x18`(u8) /
        `locationNumber@0x1C`(out int32) / `cardAtLocation@0x20`(out int32)，ParmsSize 0x28。

        ★ 先用哨兵 `-999` 预填出参：函数在"某张卡正在播放变身动画"时会**提前 return 而不写
          出参**（`doingConvertVisualAsNewCard` 那一支）⇒ 读到哨兵就知道"游戏这条链没给答案"，
          不会把缓冲区里的 0 当成"第 0 号"。
        """
        board = self.board_actor()
        if not board:
            return None
        f = self.find_fn(self.uclass_of_instance(board), "FindLocationNumberForCoordinates")
        if not f:
            return None
        parms = bytearray(0x28)
        struct.pack_into("<ddd", parms, 0x00, float(coords[0]), float(coords[1]), float(coords[2]))
        parms[0x18] = int(location_enum) & 0xFF
        struct.pack_into("<ii", parms, 0x1C, -999, -999)
        out = self.api().call_raw(hex(board), hex(f), parms.hex())
        buf = bytes(out) if out else b""
        if len(buf) < 0x24:
            return None
        ln, cat = struct.unpack_from("<ii", buf, 0x1C)
        return {"location_number": ln, "card_at_location": cat,
                "early_out": (ln == -999)}

    def gap_number(self, gap: int, row: str = "back", verbose: bool = False) -> dict:
        """空隙序号 → **游戏自己算出来的**列号请求值（失败则退回解析式，并如实标注来源）。

        空隙坐标的构造（与真人手势一一对应）：
          * `gap=0`   → 最左：第一张卡中心往左一个卡宽
          * `0<gap<n` → 两张卡**中点**（"拖到两个单位中间"）
          * `gap=n`   → 最右：最后一张卡中心往右一个卡宽
        这只是**取样点**：游戏那边真正用的是"哪些卡在该点左边"，而**顺序**不受渲染位移影响
        （所以拖动时"把卡挪开一些"的预览不会改变答案）。
        """
        loc_enum = self.our_back_enum() if row == "back" else self.our_front_enum()
        cards = self._row_cards(row=row)
        nums = [n for n, _ in cards]
        ana = self.gap_request_key(nums, gap)
        res = {"gap": int(gap), "row": row, "row_numbers": nums,
               "requested": ana, "analytic": ana, "source": "analytic",
               "row_cards": [cid for _, cid in cards]}
        n = len(cards)
        if n == 0:
            res["note"] = "该排没人 ⇒ 第 0 个空隙 = 落点 0"
            return res
        g = max(0, min(int(gap), n))
        if g == 0:
            anchor_id = cards[0][1]
            other_id = None
        elif g == n:
            anchor_id = cards[n - 1][1]
            other_id = None
        else:
            anchor_id, other_id = cards[g - 1][1], cards[g][1]
        a = self.board_actor_of(anchor_id)
        b = self.board_actor_of(other_id) if other_id else 0
        ab = self.actor_bounds(a)
        if not ab:
            res["note"] = "拿不到卡 %s 的板卡/包围盒 ⇒ 只有解析值" % anchor_id
            return res
        (ax, ay, az), (aex, _aey, _aez) = ab
        if b:
            bb = self.actor_bounds(b)
            if not bb:
                res["note"] = "拿不到卡 %s 的包围盒 ⇒ 只有解析值" % other_id
                return res
            bx = bb[0][0]
            x = (ax + bx) / 2.0
        elif g == 0:
            x = ax - aex - 1.0
        else:
            x = ax + aex + 1.0
        coords = (x, ay, az)
        res["coords"] = coords
        gm = self.game_location_number_for_coords(coords, loc_enum)
        if gm is None:
            res["note"] = "游戏那边问不出来（拿不到 BP_Board_C / 函数）⇒ 用解析值"
            return res
        res["game"] = gm
        if gm["early_out"]:
            res["note"] = ("游戏这条链**故意没给答案**（有卡在播变身动画）"
                           "⇒ 用解析值；此刻真拖也大概率不接")
            return res
        res["requested"] = int(gm["location_number"])
        res["source"] = "game:BP_Board_C::FindLocationNumberForCoordinates"
        res["card_at_location"] = int(gm["card_at_location"])
        if res["requested"] != ana:
            res["warn"] = ("游戏算出来的列号 %s 与解析式 %s **不一致**"
                           "——以游戏为准，但记下来（渲染位置/空洞可能有别的解释）"
                           % (res["requested"], ana))
        if verbose:
            print("    空隙 %s/%s：列号请求 %s（%s；解析式 %s；该点底下的卡=%s）"
                  % (gap, row, res["requested"], res["source"], ana,
                     res.get("card_at_location")))
        return res

    def probe_gap_numbers(self, row: str = "back", verbose: bool = True) -> dict:
        """**只读**：把该排每个空隙的列号请求值列出来（游戏答案 vs 解析式），并排对账。

        用途：改了站位代码/怀疑渲染换排时，一条命令就能看出"空隙 i ⇒ 游戏说第几号"。
        """
        cards = self._row_cards(row=row)
        out = {"row": row, "row_cards": [cid for _, cid in cards],
               "row_numbers": [n for n, _ in cards], "gaps": []}
        for g in range(0, len(cards) + 1):
            out["gaps"].append(self.gap_number(g, row=row, verbose=False))
        if verbose:
            print("-- 空隙 → 列号请求（排=%s，列号 %s）--" % (row, out["row_numbers"]))
            for g in out["gaps"]:
                print("  空隙 %-2s → 请求 %-3s [%s]  解析式 %-3s  %s"
                      % (g["gap"], g["requested"], g["source"], g["analytic"],
                         ("⚠ " + g["warn"]) if g.get("warn") else
                         ("" if "game" in g else ("(" + g.get("note", "") + ")"))))
        return out

    def deploy_location_number(self, slot: Optional[int], row: str = "back",
                               verbose: bool = False) -> dict:
        """`slot`（**空隙序号**）→ 真正要写的列号数。`slot=None` ⇒ 老行为（第一个空列号）。"""
        if slot is None:
            n = self._free_front_slot() if row == "front" else self._free_slot()
            return {"requested": n, "source": "first_free_slot" if n is not None else "row_full",
                    "gap": None, "row_numbers": [n0 for n0, _ in self._row_cards(row=row)]}
        r = self.gap_number(int(slot), row=row, verbose=verbose)
        return r

    # ------------------------------------------------ 单例（**不扫实例**；用户 2026-09-26 定调）
    # 用户原话："不要扫实例，因为多 live 实例的问题几乎无解。相反，跟踪这样的单例。"
    # 起因是两个真实事故：① 上一局残留的 HUD 被 `instances_of_class()` 挑中，点到了
    # **上一局的 EndTurnButton**；② `picklist` 列出**已结算的残留候选 actor**（同一类
    # 6 个 live 实例 = 2 层 × 3）。GObjects 全量扫**天然分不清"当前这一局的"**。
    # ⇒ 能用**游戏自己的访问器**就用它（`UUtilityFunctions_C::GetLogic/GetBoard/GetGameState/
    # GetDSession/GetLevelManager`），它们返回的就是"当前这一局那一个"。
    def _util_out(self, fn_name: str, wc_off: int = 0x00, out_off: int = 0x08,
                  extra: Optional[list] = None) -> int:
        """调 `UUtilityFunctions_C::<fn_name>`（游戏自己的**单例访问器**）拿指针。

        签名形如 `static void GetLogic(UObject* __WorldContext, ABP_Logic_C** Logic)`
        ⇒ ParmsSize 0x10：`__WorldContext@0x00`、出参 `@0x08`。
        ★ `__WorldContext` 传 **GWorld**（`self.ks.world`）：本项目的硬规矩 ——
          **凡是收 `__WorldContext` 的函数都不许传 0**（2026-09-26 用户定调；
          传 0 时这些库函数取不到 world，会静默回空）。
        `extra` = [(off, "ptr"|"s32", value)]，给参数在出参前的情形（如
        `GetBPPlayerController(Player0@0x00, __WorldContext@0x08, out@0x10)`）。
        """
        cls, cdo, f = self._bp_lib_fn("UtilityFunctions_C", fn_name)
        if not f:
            return 0
        parms = bytearray(0x18)
        struct.pack_into("<Q", parms, wc_off, int(self.ks.world or 0))
        for off, kind, val in (extra or []):
            if kind == "ptr":
                struct.pack_into("<Q", parms, off, int(val))
            else:
                struct.pack_into("<i", parms, off, int(val))
        try:
            out = self.api().call_raw(hex(cdo), hex(f), bytes(parms).hex())
        except Exception as e:                                     # noqa: BLE001
            self.notes.append("%s 调用异常：%s" % (fn_name, e))
            return 0
        buf = bytes(out) if out else b""
        return struct.unpack_from("<Q", buf, out_off)[0] if len(buf) >= out_off + 8 else 0

    def singleton(self, which: str) -> int:
        """按名字拿**当前这一局的单例**（不扫实例）。`which ∈ {logic,board,game_state,session,level}`。"""
        table = {"logic": "GetLogic", "board": "GetBoard", "game_state": "GetGameState",
                 "session": "GetDSession", "level": "GetLevelManager"}
        fn = table.get(which)
        return self._util_out(fn) if fn else 0

    def class_cdo(self, class_name: str) -> int:
        """按类名拿它的 **CDO**（`UClass::ClassDefaultObject // 0x110`，SDK dump 核实）。

        用途：卡类 CDO 上就摆着 `title`（FText@0x58）—— 这就是用户说的"标题表内存里能读"。

        ⚠ 先前写成 `class_named(name) + 0x110` —— **错的**。`class_named()`（= `scan_classes`
          按 `UObject::Class` 的**名字**匹配）返回的是"**该类的某个对象**"（含 CDO 自己），
          不是 UClass；`+0x110` 于是落在普通字段上，实测恒 0 ⇒ `card_title()` 永远 None，
          还被 `except` 吞成一个看起来像"内存里读不到"的假象。正确跳法是**两跳**：
          该类对象 →（`UObject::Class`，`oa.class_of`）它的 UClass →（`+0x110`）CDO。
        """
        obj = self.class_named(class_name)
        if not obj:
            return 0
        ucls = self.oa.class_of(obj)
        if not ucls:
            return 0
        return self.m.ptr_or_zero(ucls + NOTE_OFF_UCLASS_CDO)

    def card_title(self, internal_name: str) -> Optional[str]:
        """**内部名 → 卡面标题**（纯内存读，不查静态导出表）。

        路径：`card_event_storm2_thunderstorm3` →（补 `_C`）`class_named` → CDO
        → `title`（FText @ `NOTE_OFF_CARD_TITLE` = 0x58）→ `kardsmem.names.ftext_at`。
        ★ 用户 2026-09-26 原话"标题表内存测能读" —— 说的就是这条：标题本来就是卡对象/CDO
          上的一个 FText 属性，不需要维护任何"内部名→标题"的静态映射。
        ★ 同一天：**卡类/CDO 运行时不会动**（用户原话），所以按类名拿 CDO 读就行，
          不需要去扫实例、更不需要缓存"哪个实例才是它"。
        ⚠ 曾经整条链回 `None` 的真因不是"读法不对"，是 `OFF_CARD_TITLE` **没定义**
          （`NameError` 被下面 `except` 吞了）—— `except` 宽泛捕获会把"代码 bug"伪装成
          "内存里没有"，这类地方要么早返回、要么别吞。
        """
        if not internal_name:
            return None
        from kardsmem.names import ftext_at
        cls_name = internal_name if internal_name.endswith("_C") else internal_name + "_C"
        # ① 先试 CDO（UClass::ClassDefaultObject//0x110 → title@0x58）：**不用扫**
        cdo = self.class_cdo(cls_name)
        if cdo:
            try:
                t = ftext_at(self.m, cdo, NOTE_OFF_CARD_TITLE)
                if t:
                    return t
            except Exception:                                      # noqa: BLE001
                pass
        # ② 退路：这个**精确类名**的一个活实例（游戏为候选 spawn 出来的非可视卡对象，
        #    实测存在）。注意这跟"扫实例选一个"不同 —— 类名是**权威待选集**给出来的，
        #    这里只是"按名字解析对象"，不是在多个候选里挑。
        obj = self.instance_of_class(cls_name)
        if not obj:
            return None
        try:
            return ftext_at(self.m, obj, NOTE_OFF_CARD_TITLE)
        except Exception:                                          # noqa: BLE001
            return None

    def _bp_lib_fn(self, class_name: str, fn_name: str):
        """在**同名 UClass 里挑真那个**，返回 (class, cdo_obj, func)。

        ⚠ 不能用 `class_named()`：它会把同名的**陈旧 UClass** 给你
        （实测 `BattleUtilityFunctions_C` 有 2 个同名类，陈旧那个自身只有 1 个 UFunction、
        连函数名都解不出来）。判据：**哪个类的自身 UFunction 列表里有这个函数**。

        ★★ `cdo_obj` 是**函数库自己的默认对象（CDO）**，调用时 `self` 必须用它，
        **不是** UClass、**也不是**某张卡 —— `agent/legality._locate()` 的注释早就写了：
        这些函数体里有 `EX_LocalVirtualFunction`（对 self 按名调用），
        self 给错会停在"函数 X 找不到"，甚至崩/卡（2026-09-25 实测：
        把 UClass 当 self 调 `CanAttack` 时，19→54 侥幸返回、27→54 直接卡死）。
        """
        key = ("_bp_lib", class_name, fn_name)
        if key in self._fn_cache:
            return self._fn_cache[key]
        got = (0, 0, 0)
        for p in self.oa.iter_objects(skip_cdo=False):
            c = self.oa.class_of(p)
            if not c:
                continue
            try:
                if self.pool.fname_of(c) != class_name:
                    continue
            except Exception:                                     # noqa: BLE001
                continue
            for f in kismet.functions(self.ks, c):
                if f["name"] == fn_name:
                    got = (c, p, f["addr"])                       # p = 这个类的对象/CDO
                    break
            if got[2]:
                break
        self._fn_cache[key] = got
        return got

    def can_move_to(self, card_id: int, location_enum: int = LOC_BOARD_FRONTLINE,
                    verbose: bool = False, simulate_drag: bool = False) -> dict:
        """**跑游戏自己的** `BattleUtilityFunctions_C::CanMoveCardToLocation`。

        ★★ 2026-09-27 **默认不再伪造拖拽态**（用户待办："`can_move_to(simulate_drag=True)`
           仍在临时写手柄字段 `SelectedCard` ⇒ 应去掉或改问法"）。
           `simulate_drag=True` 要**临时写 `ABP_PlayerController_C::SelectedCard // 0x0950`**
           —— 那是**手柄/拖拽态字段**（同一个字段在 `IsGamepad()` 分支里被读），跟我们
           已经删掉的那批"伪造 PC 拖拽状态"的写法**是同一类**（`click-crash-report.md`：
           伪造 `leftButtonDown` 让游戏自己补一次 `EndDrag` ⇒ use-after-free）。
           ⇒ 现在：
             * `simulate_drag=False`（**默认**）：**什么都不写**，并且**不假装有答案**
               —— 返回 `{"ok": False, "reason": "cannot_ask_without_drag", "can": None}`。
               为什么不返回 `can=False`：没有拖拽态时它**必然**回 False（第一个
               `IsValid(SelectedCard)` 就不过），把它当"游戏说不能移动"就是**假判据**。
             * `simulate_drag=True`：**只作诊断**，结果里带
               `writes_gamepad_field` 标明写了哪个字段；**别把它接进判决路径**
               （`move_to_front` 已经不问了，直接让游戏在提交时判）。

        签名（SDK 1.60）：`(ECardLocationEnum Location, UObject* __WorldContext, bool* bResult)`
        —— ParmsSize 0x18：Location@0x00、WorldContext@0x08、出参 bResult@0x10。

        ★★ 2026-09-25 **重大更正**（之前这条写错了，撤回留档）：
          旧的注释说"实测这个函数在 0 指挥点时返回 False，和屏幕提示'指挥点数不足'
          完全一致" —— **那是把相关当成了因果**。全量 dump 它的字节码后看到的真相是：
            * **它里面没有任何指挥点检查**（一条都没有）；
            * 它读的是 `PlayerController->SelectedCard`
              （`BP_PlayerController_C::SelectedCard // 0x0950`，类型 `ABP_BaseCard_C*`）
              —— 也就是"**当前正在拖的那张卡**"，预检阶段那个字段是 `None`
              ⇒ 第一个 `IsValid(SelectedCard)` 就不过 ⇒ **永远**回 False。
          实测：不假装拖拽时 6 个 (卡,地点) 组合全回 0，而同一次读 `SelectedCard` 就是 None。
          ⇒ 之前"它看指挥点"的结论**作废**。

        它真正的判据（字节码逐条）：
            1. `PlayerController` 有效；
            2. `PlayerController->SelectedCard` 有效（IsValid）；
            3. `SelectedCard->CardLocation == Hand_Left(3)`（手牌）⇒ 只允许 `Location == 5`
               （Board_HQLeft）；
               否则（板卡）先过一个 `CardLocation` 查表，再过 `Location`：
               `Board_Frontline(7)` → 看 `DoesSideControlTheFrontline`；
               `Board_HQLeft(5)` → 直接 False（移动是单向的，板卡只能上前线）；
               其余 → False；
            4. `IsLocationFull(Location) && !IsSelectedCardOrder()` ⇒ False
               （那行满了、而且拖的不是"命令"类卡）。
          枚举原文：`kards_structs.hpp` `ECardLocationEnum`（7=Board_Frontline）。

        `simulate_drag=False`（默认）：**只读、不写**，而且**明确回"问不出来"**
        （`reason="cannot_ask_without_drag"`），不把"必然的 False"冒充成游戏判据。
        `simulate_drag=True`（**诊断专用**）：把 `SelectedCard` 临时指到这张卡的 actor 上、
        问完**立刻还原**（cursor 那几个字段也一起还原）—— 这正是真实鼠标**按下**
        时游戏自己会写的状态。**它会写手柄/拖拽态字段**，结果里会标出来。
        """
        cls, cdo, f = self._bp_lib_fn("BattleUtilityFunctions_C", "CanMoveCardToLocation")
        if not f:
            return {"ok": False, "stopped": "找不到 BattleUtilityFunctions_C::CanMoveCardToLocation"}
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "stopped": "找不到 card %s 的板卡 actor" % card_id}
        if not simulate_drag:
            # ★ 什么都不写、也不给假答案（理由见 docstring）
            if verbose:
                print("    CanMoveCardToLocation：**不问**（问它要伪造 PC->SelectedCard，"
                      "没有拖拽态时它恒回 False ⇒ 那个 False 不是判据）")
            return {"ok": False, "reason": "cannot_ask_without_drag", "can": None,
                    "source": "game:CanMoveCardToLocation",
                    "location_enum": location_enum, "simulated_drag": False,
                    "note": "要问就必须临时写 ABP_PlayerController_C::SelectedCard // 0x0950"
                            "（手柄/拖拽态字段）⇒ 默认不写；上线让游戏在提交时自己判"
                            "（`move_to_front` 就是这么做的）"}
        acls = self.uclass_of_instance(atk)
        # 按"真实鼠标拖到该行"会产生的中间结果写 cursor（与 _drag_lifecycle 同性质）
        writes = []
        for name, kind, val in (("cardUnderCursor", "s32", card_id),
                                ("LocationNumberUnderCursor", "s32", 0),
                                ("LocationUnderCursor", "u8", location_enum),
                                ("RowUnderCursor", "u8", 1)):
            p = props.find_prop(self.ks, acls, name, pool=self.pool)
            if p:
                writes.append((atk, p["offset"], kind, val))
        if simulate_drag:
            # `SelectedCard` = **卡 actor**（`ABP_BaseCard_C*`），不是 `UBaseCardObject`：
            # SDK 写着 `ABP_PlayerController_C::SelectedCard // 0x0950` 是 `ABP_BaseCard_C*`，
            # 而 `CardLocation // 0x03B0` 也在 `ABP_BaseCard_C` 上（`BP_BaseCard_classes.hpp`）。
            pc = player_controller(self.ks)
            pcls = self.uclass_of_instance(pc)
            off = props.find_prop(self.ks, pcls, "SelectedCard", pool=self.pool)
            if pc and off:
                writes.append((pc, off["offset"], "ptr", atk))
        parms = bytearray(0x18)
        parms[0x00] = location_enum
        import struct as _s
        _s.pack_into("<Q", parms, 0x08, atk)
        if writes:
            out = self.call_raw_hold(writes, cdo, f, bytes(parms))     # ★ self = CDO，不是 UClass
        else:
            out = self.api().call_raw(hex(cdo), hex(f), parms.hex())
        res = out[0x10] if out and len(out) > 0x10 else None
        if verbose:
            print("    【诊断】游戏自己的 CanMoveCardToLocation(loc=%s, 伪造拖拽态=%s) -> %s"
                  "（写了 PC->SelectedCard，别把它接进判决路径）"
                  % (location_enum, simulate_drag, res))
        return {"ok": True, "can": bool(res), "func": hex(f), "class": hex(cls),
                "location_enum": location_enum, "simulated_drag": True,
                "writes_gamepad_field": "ABP_PlayerController_C::SelectedCard // 0x0950",
                "source": "game:CanMoveCardToLocation", "raw_result": res}

    def move_to_front(self, card_id: int, slot: Optional[int] = None,
                      verbose: bool = True, force: bool = False) -> dict:
        """**上线**：把后排单位移到前线（`XActionMoveCardToLine`）。

        移动是**单向**的（支援阵线 → 前线）；回撤只能靠"撤退"类效果。
        落点写的是"前线某空槽"——真实鼠标拖到那儿也会得到同一个值，
        合法性照样由游戏自己判（`OnActorEndDrag` 内部会再跑一遍）。
        `slot` 不给就自己挑一个空槽；**站位有意义**（插到某两个单位中间会改变名次），
        要精确站位就显式给。

        `force=True`：越过**进程外**闸门 `is_pinned`（纯读）—— §7.6f「判据只挑不判」。
        ★ 2026-09-27 起**不再问 `CanMoveCardToLocation`**：问它必须临时伪造
          `PC->SelectedCard`（手柄/拖拽态字段，`can_move_to` docstring 里有完整理由），
          而移动合不合法**游戏在提交那一刻自己判**（实机回执 `XActionMoveCardToLine`
          就是它判"可以"的结果；不行时下面会捞 `game_hints` 拿它的原话）。
        """
        rec, st = self._card(card_id)
        if rec is None:
            return {"ok": False, "error": "场上没有 card_id=%s" % card_id}
        if rec.location == "frontline":
            return {"ok": False, "error": "%s 已经在前线（移动是单向的）" % card_id}
        if rec.location not in ("back", "hq"):
            return {"ok": False, "error": "%s 不在后排（location=%s）" % (card_id, rec.location)}
        # ★ 压制（pin）的单位**不能移动或攻击**（百科原文）—— 读不出（None）**不拦**。
        pin = self.is_pinned(card_id)
        if pin is True and not force:
            return {"ok": False, "reason": "pinned", "pinned": True,
                    "error": "%s 被压制（pin），不能移动；确信要发就传 force=True" % card_id}
        # ★★ 2026-09-27：**不再调用 `can_move_to`**（用户待办：它临时写手柄字段
        #   `SelectedCard`）。没有真实拖拽态时那个函数**必然**回 False ⇒ 拿它拦动作
        #   等于用假判据否决（§7.6f 的反面）。让游戏在提交那一下自己判；拒绝的理由
        #   由 `game_hints`（提示通道）事后捞。
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "找不到 card %s 的 BP_BoardCard_C" % card_id}
        # ★ 2026-09-27：`slot` 现在是**空隙序号**（0=最左 … n=最右）；换算同 `play_card`
        #   （前线的列号同样"有空洞时 i != 请求值"）。不给 slot 仍是老行为（第一个空列号）。
        if slot is None:
            slot = self._free_front_slot()
            if slot is None:
                return {"ok": False, "error": "前线 4 格满了"}
            place = {"requested": slot, "source": "first_free_slot", "gap": None,
                     "row_numbers": [n for n, _ in self._row_cards(row="front")]}
        else:
            place = self.deploy_location_number(int(slot), row="front", verbose=verbose)
            slot = place.get("requested")
            if slot is None:
                return {"ok": False, "error": "前线 4 格满了"}
        mk = self._mark()
        # ★ 板卡：提交口是"松手"（OnActorMouseUp），不是 OnActorEndDrag
        out = self._drag_lifecycle(atk, LOC_BOARD_FRONTLINE, slot, verbose=verbose,
                                   use_mouse_up=True)
        r = self._receipt(mk, "XActionMoveCardToLine", card_id=card_id, timeout=3.0)
        out.update(r)
        out["slot"] = slot
        out["placement"] = {k: v for k, v in place.items() if k != "row_cards"}
        if not r.get("ok"):
            out["game_hints"] = self.notify_texts()       # 失败当场捞权威理由（提示短命）
        if verbose:
            print("    上线 card_id=%s -> 前线 slot=%s 回执 ok=%s" % (card_id, slot, r.get("ok")))
        return out

    def _card(self, card_id: int):
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        for c in st.cards:
            if c.card_id == card_id:
                return c, st
        return None, st

    def find_card(self, card_id: int, side: str = "local"):
        """我方的某张卡（`ops.py::find_card` 的同义入口；`side=None` 则不限侧）。"""
        rec, st = self._card(card_id)
        if rec is None:
            return None
        if side is not None and rec.side != side:
            return None
        return rec

    def can_act_now(self, card_id: int, verbose: bool = False) -> dict:
        """**本回合这张单位能不能动**（"部署病"）—— 纯读，不写任何东西。

        对标 `ops.py::can_act_now`（旧鼠标实现里有、注入侧之前一直缺），判据同源：
          `enterPlayOnTurn == 当前回合` ⇒ **刚部署** ⇒ 本回合不能移动/攻击，
          **除非这张牌有 `blitz`**（闪击）。
        ★★ 为什么不能用 `attack_left` / `movement_left`：**对刚部署的单位它们仍然读到 1**
          （`kardsmem/board.py:429-431` 有同一条实测记录）⇒ 拿它们判会把"部署病"误判成能打。
        ★ 这是**进程外判据**（只挑不判，§7.6f）：最终由游戏裁决 —— 权威答案是
          `game_can_attack()` / `game_can_card_do_anything()`（直接问游戏本体）。
        """
        rec, st = self._card(card_id)
        if rec is None:
            return {"ok": False, "error": "读不到 card %s（不在我方手牌/场上？）" % card_id}
        kws = [str(k).lower() for k in (rec.keywords or [])]
        blitz = any("blitz" in k for k in kws)
        ept, turn = rec.enter_play_on_turn, getattr(st, "turn", None)
        out = {"ok": True, "card": card_id, "name": rec.name, "blitz": blitz,
               "enter_play_on_turn": ept, "turn": turn, "keywords": kws}
        if blitz:
            out.update(can_act=True, reason="blitz")
        elif ept is None or turn is None:
            out.update(can_act=True, reason="fields_unreadable",
                       note="读不到进场回合 ⇒ **不拦**（保持旧行为）")
        else:
            out.update(can_act=(int(ept) != int(turn)),
                       reason=("deployed_this_turn" if int(ept) == int(turn) else "ok"))
        if verbose:
            print("    can_act_now(%s %s) = %s（%s；enterPlayOnTurn=%s turn=%s blitz=%s）"
                  % (card_id, rec.name, out["can_act"], out["reason"], ept, turn, blitz))
        return out

    _GAME_KW_GETTERS = (("getHasAlpine", "alpine"), ("getHasAmbush", "ambush"), ("getHasBlitz", "blitz"),
                        ("getHasFury", "fury"), ("getHasGuard", "guard"), ("getHasImmune", "immune"),
                        ("getHasShock", "shock"), ("getHasSmokescreen", "smokescreen"))

    def card_keywords_many(self, ptrs) -> dict:
        """同 `card_keywords`，但**一次 RPC** 问完一批牌：{ptr: [关键词...]}。
        （用户 2026-10-01："又多若干个 rpc，小心点"——逐牌逐关键词各一次 RPC 太多。）"""
        items, index = [], []
        for ptr in ptrs:
            cls = self.uclass_of_instance(ptr)
            for fn_name, key in self._GAME_KW_GETTERS:
                f = self.find_fn(cls, fn_name)
                if f:
                    items.append([hex(ptr), hex(f)])
                    index.append((ptr, key))
        out = {int(p): [] for p in ptrs}
        if not items:
            return {"ok": False, "error": "没有可调的 getHas*", "keywords": {}}
        res = list(self.api().call_out_u8_batch(items))
        bad = 0
        for (ptr, key), v in zip(index, res):
            if v == -1:
                bad += 1
            elif v:
                out[int(ptr)].append(key)
        return {"ok": bad == 0, "keywords": out, "errors": bad, "rpc": 1,
                "source": "game:UBaseCardObject::getHas*"}

    def card_keywords(self, card_id: Optional[int] = None, ptr: Optional[int] = None) -> dict:
        """**游戏本体的关键词真值**（`UBaseCardObject::getHas*`，各 `bool*` 出参）—— 纯只读调用。

        ★ 2026-10-01 实机（用户点破）：506th AIRBORNE 有**冲击**，我读到的关键词却是空的。
          `hasShock @0x1E9` 字节 = 0，而 `getHasShock` 返回 1 —— 冲击来自被授予的能力/静态定义
          （`receivedAbilitiesFromCards`、`GiveShock(…giverID)` 带来源），不在那一位上。
          关键词旗标位只是其中一路；游戏的 getter 才把所有来源合并。跟 `card_totals` 同一条纪律：
          屏幕上显示/规则上生效的量，问 getter，不自己拼分量。
        """
        obj = int(ptr) if ptr else self._card_object(int(card_id))
        if not obj:
            return {"ok": False, "error": "拿不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        kws, miss = [], []
        for fn_name, key in self._GAME_KW_GETTERS:
            f = self.find_fn(cls, fn_name)
            if not f:
                miss.append(fn_name)
                continue
            v = self.call_out_u8(obj, f)
            if v:
                kws.append(key)
        return {"ok": True, "card": card_id, "keywords": kws, "missing": miss,
                "source": "game:UBaseCardObject::getHas*"}

    def card_totals(self, card_id: int, verbose: bool = False) -> dict:
        """**游戏本体的显示值**（UI 用的那批 getter）—— 纯只读调用，不改任何状态。

        对标 `BP_Widget_HandCardTextV2.cpp`（卡面文本控件）里逐条读的：
          `getTotalAttack` / `getTotalDefense` / `getTotalKreditCost` /
          `getTotalOperationCost` / `getTotalHeavyArmor`（各 `int32*` 出参）
          + `getbuffsFromCardsAsJsonString(FString*)`（被贴效果一次给全）。
        （签名来自 `kards_classes.hpp:2639-2666`。）

        ★★ 为什么需要它（用户 2026-09-27 点破）：**进程外只读拿到的是"分量"**
          （`board_api` 读 `attack@0x6C` + `attackBuff@0x70`、`operationCost@0xB0` +
          `operationCostBuff@0xB4`，自己相加），而玩家屏幕上看到的是 getter 的结果 ——
          手牌会被贴膜、被减费，两者**不保证相等**。要和 UI 一字不差，只有调它。
        ⚠ 这条是**注入式只读**（红线允许：无副作用的纯查询），进程外/离线场景读不到时
          退回分量求和并在调用方标注来源（`board_api.Card.total_operation_cost()`）。
        """
        obj = self._card_object(card_id)
        if not obj:
            return {"ok": False, "error": "拿不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        out = {"ok": True, "card": card_id, "source": "game:UBaseCardObject::getTotal*"}
        miss = []
        for name, key in (("getTotalAttack", "attack"),
                          ("getTotalDefense", "defense"),
                          ("getTotalKreditCost", "kredit_cost"),
                          ("getTotalOperationCost", "operation_cost"),
                          ("getTotalHeavyArmor", "heavy_armor")):
            f = self.find_fn(cls, name)
            if not f:
                out[key] = None
                miss.append(name)
                continue
            buf = bytes(self.api().call_raw(hex(obj), hex(f), "00" * 4) or b"")
            out[key] = struct.unpack_from("<i", buf, 0)[0] if len(buf) >= 4 else None
        f = self.find_fn(cls, "getbuffsFromCardsAsJsonString")
        if f:
            buf = bytes(self.api().call_raw(hex(obj), hex(f), "00" * 16) or b"")
            if len(buf) >= 12:
                p, num = struct.unpack_from("<Qi", buf, 0)
                if p and 1 < num <= 65536:
                    raw = self.m.read_exact(p, num * 2)
                    if raw:
                        out["buffs_json"] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if miss:
            out["missing_getters"] = miss
        if verbose:
            print("    card_totals(%s) = 攻%s/防%s 费%s 行动费%s 重甲%s"
                  % (card_id, out.get("attack"), out.get("defense"),
                     out.get("kredit_cost"), out.get("operation_cost"),
                     out.get("heavy_armor")))
        return out

    def is_pinned(self, card_id: int):
        """**压制（pin）**：被压制的单位**不能移动或攻击**（百科原文），与"抑制(suppress)"两回事。

        读侧跟 `ops.py::is_pinned` **同源**（`kardsmem.cards.read_live_effects`）：
        `receivedAbilitiesFromCards` 出现 `pinned`，或 `buffsFromCards` 出现 `combat_pinned`。
        → True / False / **None（读不出 ⇒ 调用方不要拦）**。
        """
        rec, _st = self._card(card_id)
        ptr = (rec.raw or {}).get("ptr") if rec is not None else None
        if not ptr:
            return None
        try:
            from kardsmem import cards as C
            eff = C.read_live_effects(self.ks, ptr)
        except Exception:                                     # noqa: BLE001
            return None
        for ab in eff.get("received_abilities") or []:
            if "pinned" in (ab.get("ability") or "").lower():
                return True
        for b in eff.get("buffs_from_cards") or []:
            for k in (b.get("buffs") or {}):
                if "pinned" in k.lower():
                    return True
        return False

    def resolve_target(self, spec, side: str = "enemy") -> dict:
        """**目标规格 → card_id**。只解析、**不判合法性**（"挑"与"判"分开，规格 §7.6f）。

        语法与 `ops.py::attack_card(target=…)` 一致：
          int / 数字串          → 原样返回（调用方本来就知道 card_id）
          "hq"/"ehq"            → 敌方总部；"mhq"/"ohq"/"myhq" → 我方总部
          "front" / "front<i>"  → 敌方（`side` 指定时为我方）前线第 i 个，按 slot 密集名次，0 起
          "back"  / "back<i>"   → 同上，后排
          "guard" / "guard<i>"  → 敌方场上带 guard 的单位，按 slot 排，第 i 个
        `front_i` / `back_i` 里 `i` 省略 = 0。越界返回 `ok=False` + 实际数量（**不猜**）。
        ★★ 2026-09-27 用户定调：**不做卡名解析** —— "名字解析目标是没必要的。所有动作
          应该用卡牌 cardid 作为参数。因为**多个同名单位在场上是有可能的**。"
          ⇒ 名字无法消解歧义，动作入口只收 `card_id`（位置规格只是"人给短号"的便利，
          它本来就映射到确定的 card_id）。
        ★ 2026-09-27（顺手）：`front`/`back` 前缀**要求后缀是数字**（或为空）—— 否则
          `frontline` 这种串会在 `int("line")` 上抛 ValueError（以前是真会崩的输入）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()

        def _row(where, sd):
            rows = [c for c in st.cards if c.side == sd and c.location == where
                    and c.card_type not in ("order", "counter")]
            return sorted(rows, key=lambda c: (c.slot if c.slot is not None else 99))

        if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().lstrip("-").isdigit()):
            return {"ok": True, "card_id": int(spec), "kind": "id", "side": None}
        s = str(spec or "").strip().lower()
        if s in ("hq", "ehq", "enemyhq", "enemy_hq"):
            hq = st.hq.get("enemy")
            if hq is None:
                return {"ok": False, "spec": spec, "error": "读不到敌方总部"}
            return {"ok": True, "card_id": hq.card_id, "card": hq, "kind": "hq", "side": "enemy"}
        if s in ("mhq", "ohq", "myhq", "localhq", "my_hq"):
            hq = st.hq.get("local")
            if hq is None:
                return {"ok": False, "spec": spec, "error": "读不到我方总部"}
            return {"ok": True, "card_id": hq.card_id, "card": hq, "kind": "hq", "side": "local"}
        if s.startswith("guard"):
            idx = int(s[5:] or 0)
            gs = [c for c in st.cards
                  if c.side == "enemy" and c.location in ("frontline", "back")
                  and ("guard" in (c.keywords or [])
                       or ((c.raw or {}).get("all_keyword_flags") or {}).get("has_guard"))]
            gs.sort(key=lambda c: (c.slot if c.slot is not None else 99))
            if not gs:
                return {"ok": False, "spec": spec, "count": 0,
                        "error": "敌方现在没有 guard 单位 —— 可以直接打 hq"}
            if idx >= len(gs):
                return {"ok": False, "spec": spec, "count": len(gs),
                        "error": "guard 索引 %d 越界（敌方 guard 有 %d 个）" % (idx, len(gs))}
            return {"ok": True, "card_id": gs[idx].card_id, "card": gs[idx],
                    "kind": "guard", "index": idx, "side": "enemy"}
        for prefix, where in (("front", "frontline"), ("back", "back")):
            if s.startswith(prefix):
                suffix = s[len(prefix):]
                if suffix and not suffix.isdigit():
                    continue            # 不是 "back2" 这种规格 ⇒ 留给卡名解析（见下）
                idx = int(suffix or 0)
                rows = _row(where, side)
                if not rows:
                    return {"ok": False, "spec": spec, "count": 0,
                            "error": "%s 的 %s 没有单位" % (side, where)}
                if idx >= len(rows):
                    return {"ok": False, "spec": spec, "count": len(rows),
                            "error": "%s 索引 %d 越界（%s 有 %d 个）"
                                     % (prefix, idx, where, len(rows))}
                return {"ok": True, "card_id": rows[idx].card_id, "card": rows[idx],
                        "kind": prefix, "index": idx, "side": side}
        # ★ 2026-09-27：**不做卡名解析**（用户定调："名字解析目标是没必要的。
        #   所有动作应该用卡牌 cardid 作为参数。因为多个同名单位在场上是有可能的。"）
        #   ⇒ 目标一律 **card_id**（或 `front<i>`/`back<i>`/`hq` 这种**位置**规格）。
        #   同名歧义没法从名字上消解，所以名字根本不该是动作的入口。
        return {"ok": False, "spec": spec,
                "error": "解析不出目标规格（支持 **card_id** / hq / mhq / front[<i>] / "
                         "back[<i>] / guard[<i>]；★ 不接受卡名 —— 同名单位会有歧义，"
                         "动作一律用 card_id）"}

    def pick_target(self, exclude=(), side: str = "enemy", prefer_frontline: bool = True) -> dict:
        """给"要选一个敌方目标"的动作**挑**一个目标（`ops.py::pick_target` 的同源启发式）。

        ★ 这是**启发式挑法**，不是判据：挑出来照样交给游戏裁决（§7.6f）。
        打分：`attack + (10 if 前线 else 0)`，跳过 order/counter、跳过 `is_suppressed`、
        跳过 `exclude`；一个单位都没有就退总部。返回 `{"ok","card_id","why"}`。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        ex = set(exclude or ())
        best, best_score = None, None
        for c in st.cards:
            if c.side != side or c.card_id in ex:
                continue
            if c.location not in ("back", "hq", "frontline"):
                continue
            if c.card_type in ("order", "counter"):
                continue
            if getattr(c, "is_suppressed", False):
                continue
            score = (c.attack or 0) + (10 if (prefer_frontline and c.location == "frontline") else 0)
            if best_score is None or score > best_score:
                best, best_score = c, score
        if best is not None:
            return {"ok": True, "card_id": best.card_id, "card": best, "why": "score=%s" % best_score}
        hq = st.hq.get(side)
        if hq is not None:
            return {"ok": True, "card_id": hq.card_id, "card": hq, "why": "退总部（场上没有可选单位）"}
        return {"ok": False, "error": "挑不出目标（%s 侧连总部都读不到）" % side}

    # ------------------------------------------------------------ 原语（不再是"回合循环"）
    # ★ 2026-09-26：`play_turn` / `play_game` 两个启发式自动打牌**已删除**（用户定调：
    #   "自动脚本不能是简单启发式 —— 别在现有那个残废的 playturn 上投入"）。
    #   现在这个类只提供**一个个原子动作**，每一步由调用方（我 / agent 命令层 / NN）看着现场决定。
    UNIT_TYPES = ("tank", "fighter", "bomber", "infantry", "artillery",
                  "antiair", "antitank", "tankdestroyer")

    # 攻击：真实攻击走的是 **目标箭头**（BP_targetArrowRVX_C），不是拖拽落地。
    def _hover_actor(self, actor: int, seconds: float, dispatch: bool = True,
                     leave: bool = False) -> dict:
        """悬停一个 actor：`OnActorMouseEnter` → 停留 `seconds` → `MouseHoverDispatch`（转发给对手）
        → （`leave=True`）`OnActorMouseExit`。

        悬停是**发给对手的信息流**的一部分，所以哪怕很短也必须真的发生（不许 0 秒）。

        ★ 2026-09-30 用户点破"我们的悬停和真人悬停有差异，手牌悬停预览总是异步延迟加载"。
          实测（`_nn_scratch/census.py` 对 GUObjectArray 做类名计数、前后做差）：真人悬停 10 张手牌后
          预览相关对象**净减少**；我们合成悬停 10 次后 **+10 `TextureRenderTarget2D` / +10
          `W_CardHelpPage_C` / `cardHelpBar_Widget_PC_C` / `BP_HandCardLookUnlit_C` /
          `BP_Widget_HandCardTextV2_C` / `BP_Widget_DeckBoxLabel_C`，+20 `cardHelp_Widget_PC_C`**，
          等 60 s 也不回收。原因（读 `BP_PlayerController` 的 tick）：真鼠标换目标是
          「新卡 Enter → `MouseHoverDispatch(新卡)` → **旧卡 `OnActorMouseExit`**」，移到空处是
          `mouseOverActor=null` 再 `OnActorMouseExit`；我们只做了前半段，预览从不销毁。
          ⇒ `leave=True`：停留、转发之后补上 `OnActorMouseExit`（那个类没有 Exit 就跳过）。
        """
        cls = self.uclass_of_instance(actor)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        pc = player_controller(self.ks)
        f_disp = self.find_fn(self.uclass_of_instance(pc), "MouseHoverDispatch")
        out = {"ok": True, "actor": hex(actor), "seconds": float(seconds)}
        t0 = time.time()
        if f_enter:
            self.call0(actor, f_enter)
        self.settle(seconds, 6)       # 悬停停留按帧等：失焦降帧时也要给游戏足够帧数才算"停留过"
        if dispatch and f_disp:
            out["dispatched"] = self.call_ptr(pc, f_disp, actor)
        if leave:
            f_exit = self.find_fn(cls, "OnActorMouseExit")
            if f_exit:
                self.call0(actor, f_exit)
                out["left"] = True
            # 实测（census.py）：只补 Exit 预览仍不销毁（每次悬停仍 +1 个存活的
            # BP_HandCardLookUnlit_C 展示卡 + 一组 cardHelp 控件/渲染目标，3 s 与 0.3 s 停留都一样，
            # 5 分钟不回收）。游戏自己的清理入口是基类 `BP_BaseCard::DestroyShowCaseCard(bJustMine)`
            # （false ⇒ `BattleUtilityFunctions::DestroyShowCaseCardUtility`：销毁世界里的展示卡）。
            f_dsc = self.find_fn(cls, "DestroyShowCaseCard")
            if f_dsc:
                self.call_i32(actor, f_dsc, 0)
                out["showcase_destroyed"] = True
        out["dwell"] = round(time.time() - t0, 2)
        return out

    def hover_board_card(self, card_id: int, seconds: Optional[float] = None,
                         dispatch: bool = True, verbose: bool = True) -> dict:
        """**只做悬停**（场上卡）：`OnActorMouseEnter` → 停留 → 悬停转发。

        用途（用户 2026-09-26 要求）：`agent.session.inspect()` 看一张**场上**卡时，
        真实玩家一定会把它悬停住（卡面高亮/大卡信息面板），而且这一步**对手看得到**
        （`MouseHoverDispatch`）⇒ 属于"发出去的信息流"的一部分，**悬停必须留下**。

        `seconds` 默认 `SETTLE_HOVER_INSPECT`（**1.5 s**，刻意看牌的量级）。
        没有 `OnActorMouseLeave`（`BP_BoardCard_classes.hpp` 只有 `MouseEnter`/`MouseHover`）
        ⇒ 结束时不"离开"，卡保持高亮（真人不移开鼠标也是这样）。
        """
        sec = SETTLE_HOVER_INSPECT if seconds is None else max(SETTLE_FLOOR, seconds)
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的板卡 actor" % card_id}
        out = self._hover_actor(atk, sec, dispatch)
        out["card_id"] = card_id
        if verbose:
            print("    hover 场上卡 %s %.2fs（转发=%s）" % (card_id, sec, out.get("dispatched")))
        return out

    def hover_hand_card(self, card_id: int, seconds: Optional[float] = None,
                        dispatch: bool = True, verbose: bool = True) -> dict:
        """**只做悬停**（我方手牌卡，actor = `BP_HandCard_C`）。

        用户 2026-09-26："inspect **友方手牌**也 hover 1.5s"。
        悬停我方手牌同样会被转发给对手（`MouseHoverDispatch`）⇒ 短可以，但不能没有。
        """
        sec = SETTLE_HOVER_INSPECT if seconds is None else max(SETTLE_FLOOR, seconds)
        actor = (self.hand_actor(card_id) or {}).get("actor") or 0
        if not actor:
            return {"ok": False, "error": "找不到 card %s 的手牌 actor" % card_id}
        out = self._hover_actor(actor, sec, dispatch, leave=True)
        out["card_id"] = card_id
        if verbose:
            print("    hover 手牌 %s %.2fs（转发=%s）" % (card_id, sec, out.get("dispatched")))
        return out

    def board_actor_of(self, card_id: int) -> int:
        """场上（后排/前线/HQ）某张卡的 `BP_BoardCard_C` actor。"""
        from kardsmem.world import Locator
        loc = Locator(self.m, self.ks.base)
        for p in (loc.actors() or []):
            c = self.oa.class_of(p)
            if not c or self.pool.fname_of(c) != "BP_BoardCard_C":
                continue
            # 备注：actor 上的 CardID @0x3C8（规格 §5）
            if self.m.i32(p + NOTE_OFF_ACTOR_CARDID) == card_id:
                return p
        return 0

    def arrow_actors(self) -> list:
        from kardsmem.world import Locator
        loc = Locator(self.m, self.ks.base)
        out = []
        for p in (loc.actors() or []):
            c = self.oa.class_of(p)
            if c and self.pool.fname_of(c) == "BP_targetArrowRVX_C":
                out.append(p)
        return out

    def _arrow_for(self, actor_card: int) -> int:
        """找**这次拖拽的**箭头：`fromCard == 攻击者的卡对象`。

        箭头会跨拖拽复用，而且**新箭头在同一个拖拽里提交是不生效的** ——
        所以认它不能靠"新的那一个"，要靠 fromCard 绑定。
        """
        for a in self.arrow_actors():
            if self.m.ptr(a + NOTE_OFF_ARROW_FROM_CARD) == actor_card:
                return a
        return 0

    def arrow_target_by_logic(self) -> dict:
        """**游戏自己要读的那个目标**：`BP_Logic_C::GetTargetArrowTargetCard(card, hasTarget)`。

        ★ 只读诊断。它取 `GetAllActorsOfClass(BP_targetArrowRVX_C)[0]->overCardID`
          再 `GetCardFromID(...)`（1.58 导出 `BP_Logic.cpp:7051-7091`）——
          **取的是第 0 个箭头**：所以我们只给"自己认的那个箭头"写 overCardID 不够，
          场上只要有别的箭头（初始化那次空拖留下的、上一轮没清掉的）、而它恰好排第 0，
          游戏读到的就是 0/老值 ⇒ `hasTarget=false` ⇒ 提交被静默跳过。
        """
        logic = self.logic_actor()
        if not logic:
            return {"error": "没有 BP_Logic_C 实例"}
        lcls = self.uclass_of_instance(logic)
        f = self.find_fn(lcls, "GetTargetArrowTargetCard")
        if not f:
            return {"error": "找不到 GetTargetArrowTargetCard"}
        out = self.api().call_raw(hex(logic), hex(f), (b"\x00" * 0x10).hex())
        if not out or len(out) < 0x10:
            return {"error": "没返回"}
        card = struct.unpack_from("<Q", bytes(out), 0x00)[0]
        res = {"has_target": bool(out[0x08]), "card_obj": hex(card) if card else None,
               "arrows": [{"actor": hex(a),
                           "over_card_id": self.peek(a, NOTE_OFF_ARROW_OVER_CARD_ID, "s32")}
                          for a in self.arrow_actors()]}
        if card:
            try:
                ocls = self.uclass_of_instance(card)
                res["card_id"] = self.peek(card, self.off(ocls, "CardID", 0x003C), "s32")
            except Exception:                                      # noqa: BLE001
                res["card_id"] = None
        return res

    def _drag_lead(self, actor: int, cls: int, pc: int, pc_cls: int) -> int:
        """拖拽的"前半段"：悬停 → 悬停转发 → 按下 → 起拖 → 拖动 tick。

        起拖这一步会让引擎 Spawn 出 `BP_targetArrowRVX_C`（如果这颗棋子还没有）。
        返回 `OnActorStartDrag` 的 success 出参。
        """
        self.call0(actor, self.fn(cls, "OnActorMouseEnter"))
        self.settle(SETTLE_HOVER, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch"), actor)
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        self.call0(actor, self.fn(cls, "OnActorMouseDown"))
        self.settle(SETTLE_AFTER_DOWN, 2)
        succ = self.call_ptr(actor, self.fn(cls, "OnActorStartDrag"), 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, self.fn(cls, "OnActorDragTick"))
        self.settle(SETTLE_AFTER_TICK, 2)
        return succ

    def arrow_head_plane(self, arrow: int) -> int:
        """`BP_targetArrowRVX_C::arrowHeaderPlane` —— 箭头头部平面组件（`UStaticMeshComponent*`）。

        `GetArrowLength()` = `|头部平面.GetComponentLocation() − 箭头.GetActorLocation()|²`
        （1.58 导出 `BP_targetArrowRVX.cpp:1855`），而 `IsTargetArrowLengthValid()`
        要求它 `> 3000`（桌面）/5000（移动端）。真人拖拽会把头部平面拖远 ⇒ 天然满足；
        我们不动真实鼠标 ⇒ 它只有一百多 ⇒ **提交被静默跳过**。
        所以要把这个平面搬远 —— 纯客户端几何，不影响发给服务端的信息流。
        """
        off = self.off(self.uclass_of_instance(arrow), "arrowHeaderPlane",
                       NOTE_OFF_ARROW_HEAD_PLANE)
        return self.m.ptr(arrow + off) or 0

    def actor_location(self, actor: int):
        """`AActor::K2_GetActorLocation()`（原生 const，出参 FVector@0x00，ParmsSize 0x18）—— 只读。"""
        cls = self.uclass_of_instance(actor)
        f = self.find_fn(cls, "K2_GetActorLocation")
        if not f:
            return None
        out = self.api().call_raw(hex(actor), hex(f), "00" * 0x18)
        buf = bytes(out) if out else b""
        return struct.unpack_from("<ddd", buf, 0) if len(buf) >= 0x18 else None

    def attack_card(self, card_id: int, target, verbose: bool = True,
                    force: bool = False, retry=True) -> dict:
        """**攻击（公开入口，签名对齐 `ops.py::attack_card(card_id, target, retry, force)`）**。

        `target`：card_id（int）或目标规格串（`"hq"` / `"front0"` / `"back1"` / `"guard0"`，
        见 `resolve_target()`）。

        两道**前置闸门**（都在 `_attack_once` 之前，且都能用 `force` 越过）：
          ① **选择界面开着** ⇒ 任何动作都不生效（机械事实，不是判据）。`force` **也不越过**
             它 —— 跟 `ops.py` 一致：这时发出去只会被吃掉/把界面点了，不是"越过判据"。
          ② 游戏自己的 `cardsCheckFunctions_C::CanAttack`（`game_can_attack`）。它**只挑不判**：
             `ok=False`（算不出来）**不拦**；只有明确 `can=False` 才拦，且提示传 `force=True`。
             理由码/中文理由原样带出来，别让调用方只看到一个 `ok=False`。
        `retry`：`True` → 最多 3 次；int → 该次数。失败时**顺带把游戏提示捞出来**当拒绝理由
        （提示短命，必须在失败当场轮询）。
        """
        rt = self.resolve_target(target)
        if not rt.get("ok"):
            return {"ok": False, "target": target, "resolve": rt, "error": rt.get("error")}
        target_card_id = rt["card_id"]
        if not force:
            pend = self.pick_pending()
            if pend.get("pending"):
                return {"ok": False, "pending": pend, "reason": "pending_selection",
                        "error": "现在开着选择界面（%s）——攻击不会生效，先处理它（force 不越过它）"
                                 % (pend.get("reason") or pend)}
        pre = None
        if not force:
            pre = self.game_can_attack(card_id, target_card_id, verbose=False)
            if pre.get("ok") and pre.get("can") is False:
                return {"ok": False, "reason": "game_says_no", "reason_zh": pre.get("reason_zh"),
                        "precheck": pre, "target_card_id": target_card_id,
                        "hint": "游戏自己的 CanAttack 说不行；确信要发就传 force=True（§7.6f 只挑不判）"}
        # ★「免疫」是**独立信息**：攻击仍然合法、只是这一下不造成伤害。`CanAttack` 的 `can`
        #   不覆盖这个语义 ⇒ 单独用 `target_blockers` 读，并且**只提醒不拦**。
        immune = None
        tcard = rt.get("card")
        try:
            if tcard is not None and (tcard.raw or {}).get("ptr"):
                from kardsmem import cards as _C
                me = self.find_card(card_id)
                immune = _C.target_blockers(self.ks, tcard.raw["ptr"],
                                            attacker_type=(me.card_type if me else None),
                                            attacker_ptr=((me.raw or {}).get("ptr") if me else None))
        except Exception as e:                                # noqa: BLE001
            immune = {"error": str(e)}
        n = 3 if retry is True else max(1, int(retry or 1))
        last = None
        for i in range(n):
            out = self._attack_once(card_id, target_card_id, verbose=verbose)
            out["attempt"] = i + 1
            out["target_card_id"] = target_card_id
            if pre is not None:
                out["precheck"] = pre
            if immune is not None:
                out["defender_immune"] = immune
            if out.get("ok"):
                return out
            last = out
            if i + 1 < n:
                time.sleep(0.5)
        hints = self.notify_texts()
        if hints and last is not None:
            last["game_hints"] = hints
        return last or {"ok": False, "error": "attack 没跑起来"}

    def _attack_probe(self, atk: int) -> dict:
        """攻击提交前的只读快照：箭头字段 + PC 悬停状态 + 真实光标是否在窗口里。"""
        d = {}
        try:
            atk_obj = self.m.ptr(atk + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            ar = self._arrow_for(atk_obj) if atk_obj else 0
            d["arrow"] = self.dump_props(ar, own_only=True) if ar else None
            d["pc"] = self.pc_drag_state()
        except Exception as e:                                     # noqa: BLE001
            d["err"] = str(e)
        return d

    def _attack_once(self, card_id: int, target_card_id: int,
                     verbose: bool = True) -> dict:
        """攻击：复刻真实拖拽 + 目标箭头那条链，**不绕过合法性闸门**。

        真实鼠标攻击的序列（对着 `BP_BoardCard_C` / `BP_targetArrowRVX_C` 的函数表）：

            OnActorMouseEnter → MouseHoverDispatch(转发给对手)
            → OnActorMouseDown → OnActorStartDrag（引擎在这里 Spawn 出 BP_targetArrowRVX_C）
            → OnActorDragTick（箭头每 tick 读真实鼠标位置算 overCardID）
            → [把箭头目标写成真人悬停到目标上会得到的那个 cardID]
            → OnActorEndDrag（结算：内部再过一遍 CanAttack/合法性）

        ★ 为什么能写箭头目标：箭头自己靠 `ThrottledMouseLocation(真实鼠标)` 更新
          `overCardID@0x320` / `overCard@0x328`；没有真实鼠标时它算出来的是"没悬停到任何卡"。
          我们写的是"如果鼠标真停在目标上，箭头本该得到的那个值"——跟手牌落点那三个
          变量同一个性质，不是编造状态。而且**用的是游戏自己的
          `spectatorArrowNewTarget(int32)`**（`BlueprintCallable`），不是裸改内存。
        """

        # ★★ 2026-09-26 重构：本体改走**统一原语 `drag_release()`**
        #   （L0 `queueIsRunning` 闸门 + L1 PC 拖拽状态机 + L2 箭头/`cardUnderCursor` + L3 落地口），
        #   并按 `_nn_scratch/ida-attack-report.md` 补上两处以前漏掉的东西：
        #     ① **`cardUnderCursor`（在攻击者 actor 上，int32 cardID）** —— `PlaceBoardCard`
        #        决定"打谁 / 是移动还是攻击"的**唯一输入**，必须与落地同一次 JS 执行写；
        #     ② **只发一个 `OnActorMouseUp`** —— 老版（`1c6457c`，能打）就是单口；
        #        本次会话改成 MouseUp+EndDrag 之后攻击就再没进过动作流（见 handoff §22.11）。
        #   并恢复游戏自己的 `spectatorArrowNewTarget(target)`（老版做法）。
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的 BP_BoardCard_C" % card_id}
        diag = {}
        try:                                                   # 只读诊断：松手时 CanPlayCard 的各输入
            lg = self.logic_actor()
            lc = self.uclass_of_instance(lg)
            for nm in ("PlayState", "bEndTurnQueued", "myTurnHasStarted", "queueIsRunning"):
                try:
                    diag[nm] = self.peek(lg, self.off(lc, nm, 0), "u8")
                except Exception as e:                         # noqa: BLE001
                    diag[nm] = "err:%s" % e
            f_cpc = self.find_fn(self.uclass_of_instance(atk), "CanPlayCard", inherited=True)
            if f_cpc:
                o = self.api().call_raw(hex(atk), hex(f_cpc), "00" * 8)
                diag["CanPlayCard"] = int(bytes(o)[0]) if o else None
        except Exception as e:                                 # noqa: BLE001
            diag["err"] = str(e)
        mk = self._mark()
        out = self.drag_release(
            atk,
            cursor={"card_under_cursor": int(target_card_id), "location_enum": 7, "row": 1},  # IDA: PlaceBoardCard 需要 Location∈{5,6,7}且 Row=true，光标在窗外时这两项是旧值
            arrow_target=int(target_card_id),
            hover_other=self.board_actor_of(target_card_id),
            commit="mouse_up",
            prime_arrow=True,
            pre_commit=lambda: self._attack_probe(atk),
            verbose=verbose)
        out["target_card_id"] = target_card_id
        out["diag"] = diag
        try:                                                   # 提交后的队列/光标状态（定位"被吞"的是哪一道闸）
            out["queue_running_after"] = self.queue_running()
            cls_a = self.uclass_of_instance(atk)
            out["cursor_after"] = {
                "cardUnderCursor": self.peek(atk, self.off(cls_a, "cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR), "s32")}
        except Exception as e_c:                               # noqa: BLE001
            out["cursor_after_err"] = str(e_c)
        r = self._receipt(mk, "XActionAttackCard", card_id=card_id, timeout=3.0)
        if not r.get("ok"):
            # ★ 2026-10-01 用户："一般玩家可以拖拽多次，然后攻击之类会慢慢结算"——动作进队列后可能迟到成交。
            #   没回执时再等一会儿（总共 ~10 s），看是不是"慢结算"而不是被吞；迟到成交也算成功，并标 late。
            r_late = self._receipt(mk, "XActionAttackCard", card_id=card_id, timeout=0.2)   # 实测 4/4 失败都没有迟到成交 ⇒ 不再多等
            if r_late.get("ok"):
                r = dict(r_late, late_receipt=True, waited=round(3.0 + (r_late.get("waited") or 0), 2))
            else:
                out["late_wait_failed"] = True
        if not r.get("ok"):
            try:
                self.call0(atk, self.fn(self.uclass_of_instance(atk), "OnActorEndDrag"))
            except Exception:                                  # noqa: BLE001
                pass
        out.update(r)
        if verbose:
            print("    攻击 attacker=%s target=%s 回执 ok=%s（箭头=%s queue_idle=%s）"
                  % (card_id, target_card_id, r.get("ok"), out.get("arrows_written"),
                     out.get("queue_idle")))
        return out

    # ------------------------------------------------------------ 换牌（mulligan）
    # 真实流程（规格 §7.6d，逐跳有蓝图出处）：
    #   ① 点手牌 → `BP_HandCard_C::OnActorClicked()` → `MulliganDiscardToggle()` 翻转
    #      `shouldDiscard@0x980`（屏幕上出现红圈；再点一下取消）
    #   ② 点确认按钮 → `BP_ConfirmCardsButton_C` 的
    #      `BndEvt__..._onClicked__DelegateSignature()` → `BP_Logic_C::onCardSelectionConfirmed()`
    #      → `BP_Deck_C::MulliganDone()` → `DiscardAllMarkedCards()` → `SendCardsToMulligan()`
    # 我们只做"把鼠标会做的事按同样顺序做一遍"：悬停（含转发）→ 点击。
    def logic_actor(self, refresh: bool = False) -> int:
        """`BP_Logic_C` 实例（**缓存地址**）。

        ★ 2026-09-25 血的教训：`instance_of_class()` 每次都要**全量扫 GObjects**（实测
          单次 ~3-4s）。而换牌窗口只有几秒，用它做轮询 = 第一次采样就晚了 11 秒，
          窗口永远抓不到。这两个 actor 是常驻的（跨对局不销毁），所以地址缓存一次就够，
          之后每帧只做一次 `ReadProcessMemory`（微秒级）。
        """
        self._sync_match()
        if refresh or self._logic is None:
            # ★ 先走**游戏自己的单例访问器**（`UUtilityFunctions_C::GetLogic`）——
            #   它给的是"当前这一局那一个"；取不到（世界没起来/不在对局）才退回扫实例并记一笔。
            got = self.singleton("logic") or 0
            if not got:
                got = self.instance_of_class("BP_Logic_C") or 0
                if got:
                    self.notes.append("GetLogic() 没给指针，退回扫实例拿 BP_Logic_C")
            self._logic = got
            self._ml = None          # ★ 换了一局（GetLogic 变了）⇒ 动作流容器可能换 ⇒ 丢掉 MatchLog 缓存
        return self._logic

    def pregame_state(self) -> Optional[int]:
        """E_PreGameStates：0 Initialize / 1 SelectHomeBase / 2 ShowStartingHand /
        3 DoMulligan / 4 PlayerReady。**2 才是"在等我换牌"**。"""
        a = self.logic_actor()
        return self.peek(a, NOTE_OFF_LOGIC_PREGAMESTATE, "u8") if a else None

    def mulligan_done(self) -> Optional[bool]:
        a = self.logic_actor()
        return bool(self.peek(a, NOTE_OFF_LOGIC_MY_MULLIGAN_DONE, "u8")) if a else None

    def mulligan_marks(self) -> list:
        from kardsmem.pick import mulligan_marks
        return mulligan_marks(self.ks)

    def mulligan_mark(self, card_id: int, verbose: bool = True) -> dict:
        """点一张手牌：打上/取消"要换"标记（**一次点击 = 一次翻转**，不双击）。"""
        rec = self.hand_actor(card_id)
        if not rec:
            return {"ok": False, "error": "手牌里没有 card_id=%s" % card_id}
        # ★ 先看门槛：窗口没开时，游戏会把后面所有点击**默默吞掉**（屏幕一动不动），
        #   必须先报"窗口没开"，不能让它看起来像注入失败。
        if not self.showing_starting_hand():
            return {"ok": False, "card_id": card_id,
                    "error": "换牌窗口没开（Deck::showingStartingHand=0），游戏会吞掉这次点击"}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        off_sd = self.off(cls, "shouldDiscard", NOTE_OFF_HANDCARD_SHOULD_DISCARD)
        before = self.peek(actor, off_sd, "u8")

        self.call0(actor, self.fn(cls, "OnActorMouseEnter"))
        self.settle(SETTLE_HOVER, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch"), actor)
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ★ `OnActorClicked(bool IsPrecise)` **带一个参数**（SDK 签名核实过），不是
        #   无参事件——用 call0 传 NULL parms 会在函数体里读 IsPrecise 时空指针崩掉
        #   （实机复现：access violation accessing 0x0）。真实单击是"精确点击"，传 True。
        # ★ 2026-09-26 重构：走统一点击原语（先 `GlobalMouseUp(mouseDownActor)`，
        #   未消费才转发 `OnActorClicked`；带 L0 队列闸门 + L1 PC 状态）。
        click_res = self.click_actor(actor, is_precise=1, verbose=verbose)
        self.settle(0.25, 6)          # 等 shouldDiscard 真的翻转再读 ⇒ 按帧等
        after = self.peek(actor, off_sd, "u8")
        if verbose:
            print("    换牌标记 %s(%s): shouldDiscard %s -> %s"
                  % (rec.get("name"), card_id, before, after))
        return {"ok": before != after, "card_id": card_id, "marked_before": before,
                "marked_after": after}

    def _mulligan_ui_tail(self, verbose: bool = True) -> dict:
        """补 BP 在 `onConfirmClicked->Broadcast()` 之后的收尾：清 `MulliganTimeOutEventHandler` 计时器 + `RemoveFromParent()`。
        控件已不在（已自己收尾）⇒ 什么都不做。每一步都幂等。"""
        w = self.instance_of_class("BP_ConfirmCardsButton_C")
        if not w:
            return {"ran": False, "why": "换牌控件已不在（BP 自己收尾了）"}
        res = {"ran": True}
        cls = self.uclass_of_instance(w)
        try:
            off_h = self.off(cls, "MulliganTimeOutEventHandler", 0x03A8)
            handle = self.ks.m.u64(w + off_h) or 0
            res["timer_handle"] = handle
            if handle:
                _c, cdo, f = self._bp_lib_fn("KismetSystemLibrary", "K2_ClearAndInvalidateTimerHandle")
                if cdo and f:
                    parms = struct.pack("<QQ", w, handle)
                    res["timer_cleared"] = self.api().call_raw(hex(cdo), hex(f), parms.hex()) is not None
                else:
                    res["timer_cleared"] = False
        except Exception as e:                                    # noqa: BLE001
            res["timer_error"] = "%s: %s" % (type(e).__name__, e)
        try:
            f_rm = self.find_fn(cls, "RemoveFromParent")
            res["removed"] = bool(f_rm and self.call0(w, f_rm))
        except Exception as e:                                    # noqa: BLE001
            res["remove_error"] = "%s: %s" % (type(e).__name__, e)
        if verbose:
            print("    换牌收尾补做：%s" % res)
        return res

    def mulligan_confirm(self, verbose: bool = True) -> dict:
        """点确认按钮 —— **真的把子按钮按下去**（hover → pressed → released → clicked）。

        ★ 2026-09-26 用户当场指出："没点到确认按钮，还是等到重新调度结束才关窗口。"
          根因：`BP_ConfirmCardsButton_C` 自己**只有两个句柄**（一个无关的 CardZoomedButton，
          一个转发用 `..._MasterTextButton_..._1_onClicked`）—— **它没有 pressed/released**。
          真正的按钮是它身上的子 widget **`WBP_NUI_MasterTextButton // 0x0348`**，
          四件套全在子按钮类上（`WBP_NUI_MasterTextButton_classes.hpp`）：
              hover  `..._1_OnButtonHoverEvent` / `..._2_OnButtonHoverEvent`
              press  `..._5_OnButtonPressedEvent`
              release`..._7_OnButtonReleasedEvent`
              click  `..._0_OnButtonClickedEvent`
          ⇒ 老实现只在**父 widget** 上调 `onHovered` + `onClicked`：逻辑确实跑了
          （换牌成功），但**按钮组件从未被按下**，屏幕上看不到点击、窗口也不由它关闭。
          现在改成在**子按钮**上按真实顺序走一遍（clicked 那一跳会转发到父的回调）。
        """
        btn_widget = self.instance_of_class("BP_ConfirmCardsButton_C")
        if not btn_widget:
            return {"ok": False, "error": "找不到 BP_ConfirmCardsButton_C（不在换牌界面）"}
        cls = self.uclass_of_instance(btn_widget)
        out = {"ok": False, "parent": hex(btn_widget)}
        done_before = bool(self.mulligan_done())
        # ① 子按钮（真正的 UMG 按钮）
        off_child = self.off(cls, "WBP_NUI_MasterTextButton", 0x0348)
        child = self.m.peek(btn_widget, off_child, "ptr") if hasattr(self.m, "peek") else None
        if not child:
            child = self.m.ptr(btn_widget + off_child) or 0
        out["child"] = hex(child) if child else 0
        if child:
            ccls = self.uclass_of_instance(child)
            # ★ 2026-10-01 用户："确认按钮没点，只换了牌"：逻辑推进了但屏幕上看不到按钮被按下。
            #   这里的停顿是**裸 time.sleep**（按下 0.08 s / 松开 0.05 s），失焦降帧时这段时间里游戏一帧都没画，
            #   按下态根本没渲染出来 ⇒ 改成按**帧数**等（`settle(秒, 帧)`），和攻击手势同一原因同一修法。
            seq = (("hover", "BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature", 0.25, 6),
                   ("press", "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature", 0.08, 4),
                   ("release", "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature", 0.05, 3),
                   ("click", "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature", 0.25, 6))
            out["child_calls"] = []
            for name, fn_name, pause, frames in seq:
                f = self.find_fn(ccls, fn_name)
                out["child_calls"].append((name, bool(f)))
                if f:
                    self.call0(child, f)
                    self.settle(pause, frames)
        # ② 父 widget 的 onClicked 兜底（子按钮 clicked 通常会转发过来；幂等）
        f_click = self.find_fn(
            cls, "BndEvt__BP_ConfirmCardsButton_WBP_NUI_MasterTextButton_"
                 "K2Node_ComponentBoundEvent_1_onClicked__DelegateSignature")
        if f_click and not child:
            f_hover = self.find_fn(
                cls, "BndEvt__BP_ConfirmCardsButton_WBP_NUI_MasterTextButton_"
                     "K2Node_ComponentBoundEvent_0_onHovered__DelegateSignature")
            if f_hover:
                self.call0(btn_widget, f_hover)
                self.settle(0.25, 6)  # 兜底路径的 hover→click 之间按帧等（同子按钮四步的理由）
            self.call0(btn_widget, f_click)
        if verbose:
            print("    换牌确认: 子按钮 hover→press→release→click（child=0x%X，兜底父句柄=%s）"
                  % (child or 0, bool(f_click) and not child))
        # 回执：等 PreGameState 离开 2，或 myMulliganDone 变 1
        t0 = time.time()
        while time.time() - t0 < 3.0:
            if self.mulligan_done() or self.pregame_state() not in (2, 3):
                break
            time.sleep(0.1)
        done = bool(self.mulligan_done())
        # ★ 2026-09-25 曾观察到"确认点下去、逻辑推进了，但换牌 UI 不自己关"，
        #   当时用 `HideMulliganUi()` 兜底。
        # ★ 2026-09-26 更正（用户定调）：**那个兜底无效，已删**。真正的原因是
        #   按钮层级找错了（父 widget 没有 pressed/released，四件套在子按钮
        #   `WBP_NUI_MasterTextButton // 0x0348` 上）—— 把四件套在**子按钮**上走完，
        #   界面**本来就会自己关**（实机 1.22 s）。兜底既不是真人的行为，
        #   还会掩盖"点击没生效"。
        #   现在只**如实记录**：点击后界面有没有自己关；没关就报出来，不再替它收拾。
        # ★ 2026-10-03（用户：换牌的确成功了、对局也推进了，只是换牌按钮和提示还在；最后弹“换牌超时”）：
        #   逻辑推进了，但 BP 里 `onConfirmClicked->Broadcast()` 之后的**收尾**没跑——清超时计时器 + `RemoveFromParent()`
        #   （BP_ConfirmCardsButton.cpp Label_1108）。用户判断：回调没有全部触发。这里把漏掉的那两步补上（和 BP 同一对调用，
        #   纯表现层，不再 Broadcast，不会二次确认），并如实记录。
        advanced = (self.pregame_state() not in (2, 3, None)) or (done and not done_before)
        if advanced:
            out["ui_tail"] = self._mulligan_ui_tail(verbose=verbose)
        out["window_closed_by_click"] = (self.showing_starting_hand() is not True)
        res = {"ok": done,
               "pregame_state": self.pregame_state(),
               "mulligan_done": done,
               "marked": [r["card_id"] for r in self.mulligan_marks() if r.get("marked")]}
        res.update({k: out[k] for k in ("child", "child_calls", "window_closed_by_click", "ui_tail", "done_before")
                    if k in out})
        if done and not out.get("window_closed_by_click"):
            res["warning"] = "换牌逻辑推进了，但点击后界面没自己关（真点击可能没生效）"
        if verbose:
            print("    证据：window_closed_by_click=%s（无兜底）"
                  % out.get("window_closed_by_click"))
        return res

    # ------------------------------------------------------------ 抉择/二选一/三选一
    PICK_CLASS_ONE = "BP_ChooseOneCard_C"          # 二选一（"预报 / 抽一张牌"）
    PICK_CLASS_SPAWN = "BP_ChooseCardToSpawn_C"    # 三选一 / 预报的子选项 / develop
    # ★ 2026-09-26 补第三类：**牌库选牌**（scrying / "看牌库顶 N 张选一张"，13 个 case，
    #   例如 card_event_spoils_of_war / card_unit_panzer_iii_h / card_event_exploit_the_gap）。
    #   它是**独立一个类**，跟上面两个都不是父子关系 ⇒ 不补进来的话，这类抉择
    #   `picklist` 会报"没有候选在等"（症状跟当初漏 `BP_ChooseOneCard_C` 那次一模一样）。
    #   字段（`BP_ChooseCardToDraw_classes.hpp` 全字段核过）：
    #     `indexOfCardInDeck 0x0878`（ExposeOnSpawn，= 它在**牌库里的下标**，
    #      也是屏幕次序）、`cardBeingPlayed 0x0880`(触发手牌 actor)、
    #     `cardBeingPlayedID 0x095C`、`isOwnedByMe 0x0960`。
    #   ⚠ 这个类**没有名字字段**（不像 ChooseCardToSpawn 有 `Name_0`）⇒ 候选身份要靠
    #     `indexOfCardInDeck` 去**我方物理牌库**（`kardsmem.cards.deck_cards`，保持牌序、
    #     允许同名多份）取第 index 张。
    PICK_CLASS_DRAW = "BP_ChooseCardToDraw_C"
    NOTE_OFF_DRAW_INDEX = 0x0878        # int32 indexOfCardInDeck
    NOTE_OFF_DRAW_TRIGGER = 0x0880      # ptr   cardBeingPlayed（触发它的手牌 actor）
    NOTE_OFF_DRAW_TRIGGER_ID = 0x095C   # int32 cardBeingPlayedID
    NOTE_OFF_DRAW_OWNED = 0x0960        # bool  isOwnedByMe

    def pending_draw_sets(self, verbose: bool = True) -> dict:
        """★ **权威**：当前正在等的"选牌"候选集（三选一 / 预报两级），**不靠扫 actor**。

        路径：`BP_Logic_C::onlineMatch //0x0810` →
        `BP_OnlineMatch_C::selectCardToDrawPending //0x08F0`（`TMap<int32, FString>`）。
        ★ 2026-09-26 实测（dump 了那 0x80 字节）它的**真实语义**跟名字不完全一样：
          只有 **1 条**，`key` 是个小序号（如 2），`value` = **`"首张候选内部名:触发卡id"`**
          （例：`"card_event_sunny1_blue_sky:31"`，30 字符）。而且**只在"有选牌在等"时非空**、
          结束时为空 ⇒ 它给的是 **pending 标志 + 当前触发卡 id**，**不是**完整三项。
        完整三张在**动作流**里：`ZActionSelectCardToDrawPending{cardBeingPlayed, spawnCards}`
        （`spawnCards` = `a;b;c` 全串，就是这一级 spawn 出来的三张）。所以本函数把两者拼起来：
          trigger（map）→ 动作流里该 trigger 的 `spawnCards`（三张名）→ 与候选 actor 的 `index` 对照。
        返回：`{"ok", "pending": {trig_id: [三张名]}, "live": {trig_id: {...}}, "triggers": [...]}`；
        `live.*.index_match` = 候选 actor 按 index 排出来的名字是否与三张名一致 ⇒ 一致才说明
        "第 i 位 ↔ names[i]"这条映射成立（这是**点之前的权威依据**）。
        """
        from kardsmem.containers import tmap_int_to_fstring
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        off_om = self.off(lcls, "onlineMatch", NOTE_OFF_LOGIC_ONLINE_MATCH)
        om = self.m.ptr(logic + off_om) or 0
        if not om:
            return {"ok": False, "stopped": "BP_Logic_C::onlineMatch 为空（不在对局里？）"}
        ocls = self.uclass_of_instance(om)
        off_map = self.off(ocls, "selectCardToDrawPending", NOTE_OFF_OM_SELECT_CARD_TO_DRAW)
        raw = tmap_int_to_fstring(self.m, om + off_map)
        res = {"ok": True, "online_match": hex(om), "map_raw": raw}
        # ① map 给"pending 标志 + 当前 trigger"（value = "名字:triggerId"）
        trigs = []
        for _stage, val in raw.items():
            _nm, _sep, tid = (val or "").partition(":")
            if tid.strip().lstrip("-").isdigit():
                trigs.append(int(tid.strip()))
        # ② 动作流给"该 trigger 的完整三张"（spawnCards）
        names = {}
        try:
            from kardsmem.matchlog import MatchLog
            ml = self.matchlog()      # ★ 长寿命单例（别再每次新建：locate 4~6 s）
            for row in ml.since(max(0, ml.count() - 200)):
                for sa in (row.get("sub_actions") or []):
                    if sa.get("name") != "ZActionSelectCardToDrawPending":
                        continue
                    v = {x.get("name"): x for x in (sa.get("values") or [])}
                    cbp, sp = v.get("cardBeingPlayed", {}).get("value"), \
                        v.get("spawnCards", {}).get("text")
                    if cbp is not None and sp:
                        names[int(cbp)] = sp.split(";")
        except Exception as e:                                     # noqa: BLE001
            res["matchlog_error"] = str(e)
        res["pending"] = {t: names.get(t, []) for t in trigs}
        # 连标题一起给（内存读；`card_event_storm2_thunderstorm3` → `THUNDERSTORM`）
        res["titles"] = {n: self.card_title(n) for n in
                         {x for v in res["pending"].values() for x in v}}
        if not trigs and names:
            res["pending_hist"] = names       # 没在等时给最近几次的集合，便于对照
        cands = self.pick_candidates()
        live = {}
        for trig, nm in res["pending"].items():
            grp = sorted([c for c in cands if c.get("trigger_id") == trig],
                         key=lambda c: (c["index"] if c["index"] is not None else 99))
            live[trig] = {"actors": [(c["index"], c.get("name"), hex(c["actor"])) for c in grp],
                          "index_match": [c.get("name") for c in grp] == nm,
                          "n_names": len(nm), "n_actors": len(grp)}
        res["live"] = live
        res["triggers"] = sorted(res["pending"].keys())
        if verbose:
            print("    当前待选：%s" % (res["pending"] or "无（现在没在等选牌）"))
        return res

    def _deck_name_at(self, index: Optional[int], side: str = "local") -> Optional[str]:
        """我方**物理牌库**第 `index` 张的牌名（给"牌库选牌"的候选定位用）。

        `kardsmem.cards.deck_cards` 保持牌序、允许同名多份 ⇒ 按 `indexOfCardInDeck` 取。
        读不出/越界返回 None，**不抛** —— 它只是给候选"补个名字"的增强，
        不该因为它把整个 `pick_candidates` 打挂。
        """
        if index is None or index < 0:
            return None
        try:
            from kardsmem.cards import deck_cards
            rows = deck_cards(self.ks, side) or []
            if index < len(rows):
                return rows[index].get("name")
        except Exception:                                      # noqa: BLE001
            return None
        return None

    def pick_candidates(self, cache: Optional[dict] = None) -> list:
        """当前**二选一 + 三选一**候选（两类统一读法），按各自的 index 排好序。

        ★★ 2026-09-25 补的（用户点名的"二选一和三选一需要完善"）：
          旧版只有 `choose_candidates()`，**只认 `BP_ChooseCardToSpawn_C`**（三选一），
          于是 `US WEATHER BUREAU` 那一层"预报 / 抽一张牌"的二选一**一个候选都读不到**，
          `pick_choice()` 会直接报"现在没有候选在等"——那不是"没在等"，是"看不见"。

        两个类的字段（全部来自 SDK dump 逐字段，不是 diff 出来的）：
          `ABP_ChooseCardToSpawn_C`（三选一）：`indexOfCardInDeck//0x0878`(次序)、
              `cardBeingPlayed//0x0880`(触发者的手牌 actor)、`Name_0//0x095C`(候选内部名)、
              `SelectedCard//0x0964`、`cardBeingPlayedID//0x0968`、`isOwnedByMe//0x096C`
          `ABP_ChooseOneCard_C`（二选一）：`chooseOneIndex//0x09A0`(次序, ExposeOnSpawn)、
              `_card//0x08C0` / `SpawnCard//0x08B8`(这个分支对应的卡对象)、
              `_motherCard//0x09B8`(触发它的手牌 actor)、`needsTarget//0x09A4`、
              `Clickable//0x09C1`
          ⇒ **两个类都有可靠的屏幕次序字段**，不必再靠屏幕坐标排序。
             二选一的**选项文字**要另读：触发卡（`UBaseCardObject`）的
             `chooseOneCards//0x138`（`TArray<FChooseOneCardStruct>`，`FText cardText@+0`），
             下标就是 `chooseOneIndex`（游戏自己的 `BP_HandCard_C::SpawnChooseOneCards`
             就是这么生成这些 actor 的）。

        返回每项：`{actor, cls, cls_name, kind, index, order_ok, name, label, selected,
        clickable, needs_target, trigger_actor, trigger_id, card_obj, card_id}`。
        `order_ok=False` 表示次序字段读不出（这时 `index` 是 None，调用方自己决定要不要盲点）。
        空列表 = 现在没有这类候选在等。
        """
        # ★ 2026-10-01：`scan_classes`（GObjects 全量扫）有时一次吃 30 s（预报刚弹候选时实测 `pick_candidates` 30.6 s，
        #   正常 0.6 s）。候选 actor 都是**世界里的 level actor** ⇒ 走 level-actor 快路径（外部实测 236 个 actor 0.00 s）；
        #   快路径出错才退回全量扫。
        if cache is None:
            try:
                rows = {n: self.actors_of_class(n, refresh=True)
                        for n in (self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN, self.PICK_CLASS_DRAW)}
                # 安全网：快路径一个都没看到、但游戏明明在等选择（pick_pending）⇒ 候选可能不在 level actor 里 ⇒ 退回全量扫
                if not any(rows.values()) and self.pick_pending().get("pending"):
                    self.notes.append("pick_candidates：快路径为空但 pick_pending=True，退回全量扫")
                    rows = self.scan_classes([self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN,
                                              self.PICK_CLASS_DRAW], skip_cdo=True)
            except Exception as e_fast:                            # noqa: BLE001
                self.notes.append("pick_candidates：level-actor 快路径失败，退回全量扫（%s）" % e_fast)
                rows = self.scan_classes([self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN,
                                          self.PICK_CLASS_DRAW], skip_cdo=True)
        else:
            rows = cache
        out = []
        for cls_name, kind in ((self.PICK_CLASS_ONE, "choose_one"),
                               (self.PICK_CLASS_SPAWN, "choose_spawn"),
                               (self.PICK_CLASS_DRAW, "choose_draw")):
            for a in (rows.get(cls_name) or []):
                cls = self.uclass_of_instance(a)
                if not cls:
                    continue
                if kind == "choose_one":
                    idx = self.peek(a, self.off(cls, "chooseOneIndex", NOTE_OFF_ONE_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "_motherCard", NOTE_OFF_ONE_MOTHER)) or 0
                    card_obj = self.m.ptr(a + self.off(cls, "_card", NOTE_OFF_ONE_CARD)) or 0
                    spawn_obj = self.m.ptr(
                        a + self.off(cls, "SpawnCard", NOTE_OFF_ONE_SPAWN_CARD)) or 0
                    needs_t = bool(self.peek(a, self.off(cls, "needsTarget",
                                                        NOTE_OFF_ONE_NEEDS_TARGET), "u8"))
                    clickable = bool(self.peek(a, self.off(cls, "Clickable",
                                                           NOTE_OFF_ONE_CLICKABLE), "u8"))
                    name, selected, owned, trig_id = None, None, None, self._actor_card_id(trig)
                    is_effect = None
                elif kind == "choose_spawn":
                    idx = self.peek(a, self.off(cls, "indexOfCardInDeck",
                                                NOTE_OFF_SPAWN_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "cardBeingPlayed",
                                                   NOTE_OFF_SPAWN_TRIGGER)) or 0
                    card_obj = spawn_obj = 0
                    name = self.pool.fname_of(a, self.off(cls, "Name_0", NOTE_OFF_SPAWN_NAME))
                    selected = bool(self.peek(a, self.off(cls, "SelectedCard",
                                                          NOTE_OFF_SPAWN_SELECTED), "u8"))
                    owned = bool(self.peek(a, self.off(cls, "isOwnedByMe",
                                                       NOTE_OFF_SPAWN_OWNED), "u8"))
                    needs_t, clickable = None, None
                    trig_id = self.peek(a, self.off(cls, "cardBeingPlayedID",
                                                    NOTE_OFF_SPAWN_TRIGGER_ID), "s32")
                    # ★ 2026-09-27：`BP_ChooseCardToSpawn_C::isEffect // 0x980`。它是
                    #   同一分支里区分"**预报第一段**（三个天气模板，isEffect=0）"和
                    #   "**预报第二段 / effect 选择**（isEffect=1，提交串变
                    #   `effectSelected:<名>`）"的**权威标志** —— 这两层是**同一个 actor 类**，
                    #   光看类名分不出来（以前靠 `SelectedCard` 启发式，会误判）。
                    is_effect = bool(self.peek(a, self.off(cls, "isEffect",
                                                           NOTE_OFF_SPAWN_IS_EFFECT), "u8"))
                else:                                     # choose_draw（牌库选牌）
                    idx = self.peek(a, self.off(cls, "indexOfCardInDeck",
                                                self.NOTE_OFF_DRAW_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "cardBeingPlayed",
                                                   self.NOTE_OFF_DRAW_TRIGGER)) or 0
                    card_obj = spawn_obj = 0
                    owned = bool(self.peek(a, self.off(cls, "isOwnedByMe",
                                                       self.NOTE_OFF_DRAW_OWNED), "u8"))
                    needs_t, clickable, selected = None, None, None
                    is_effect = None
                    trig_id = self.peek(a, self.off(cls, "cardBeingPlayedID",
                                                    self.NOTE_OFF_DRAW_TRIGGER_ID), "s32")
                    # 这个类没有名字字段 ⇒ 用 `indexOfCardInDeck` 去**我方物理牌库**取。
                    # 取不到就留 None（调用方至少能靠 `index` 排屏幕次序，或者先 `raw deck`）。
                    name = self._deck_name_at(idx) if owned else None
                out.append({"actor": a, "cls": cls, "cls_name": cls_name, "kind": kind,
                            "index": idx, "order_ok": idx is not None, "name": name,
                            "label": None, "selected": selected, "clickable": clickable,
                            "needs_target": needs_t, "owned_by_me": owned,
                            "is_effect": is_effect,
                            # ★ actor 自己的 `CardID // 0x3C8`。对 **choose_draw** 尤其重要：
                            #   spawner 在 `FinishSpawningActor` 前显式写过它（
                            #   `BP_VisualController.cpp:10206`）⇒ 这是"这张候选是牌库里的哪张"
                            #   的**权威身份**，比用 `indexOfCardInDeck` 回查物理牌库更稳。
                            "card_id": self._actor_card_id(a),
                            "trigger_actor": trig, "trigger_id": trig_id,
                            "card_obj": card_obj or spawn_obj})
        # 二选一的选项文字 + "这个分支要不要目标"：触发卡的 chooseOneCards（下标 = chooseOneIndex）
        opts_cache = {}
        for c in out:
            if c["kind"] != "choose_one" or not c["trigger_actor"]:
                continue
            key = c["trigger_actor"]
            if key not in opts_cache:
                opts_cache[key] = self.choose_one_options(key)
            opts = opts_cache[key]
            if c["index"] is not None and 0 <= c["index"] < len(opts):
                c["label"] = opts[c["index"]].get("label")
                # ★ "选完这个分支还要再点目标"（两阶段）——以**游戏数组**为准；
                #   候选 actor 自己的 needsTarget 是它的副本，两个都留着互相校验。
                c["option_needs_target"] = opts[c["index"]].get("needs_target")
        out.sort(key=lambda c: (c["kind"], c["index"] if c["index"] is not None else 99))
        # ★ 2026-09-26（用户："标题表内存测能读"）：给每个有内部名的候选补上**卡面标题**
        #   —— 纯内存读（UClass → CDO → title FText@0x58），不查任何静态导出表。
        #   这样 `picklist` 就能直接显示"THUNDERSTORM"这种屏上真名，而不是内部名。
        for c in out:
            if c.get("name") and not c.get("title"):
                c["title"] = self.card_title(c["name"])
        return out

    def choose_one_options(self, trigger_actor: int, verbose: bool = False) -> list:
        """触发卡的 `chooseOneCards`（`UBaseCardObject+0x138`）→ **每个选项的文字 + 是否要目标**。

        `FChooseOneCardStruct { FText cardText // 0x00; bool selectTargetOnPlayedFromHand // 0x10 }`
        （`kards_structs.hpp:1154`）。`chooseOneIndex` 就是这个数组的下标 ——
        游戏自己的 `BP_HandCard_C::SpawnChooseOneCards(chooseOneCards, motherCardID)`
        就是拿着这个数组生成候选 actor 的。

        返回 `[{label, needs_target, index, ftext_ok}]`；读不出就如实标（**不编**）。
        `selectTargetOnPlayedFromHand` 就是"选完这个分支**还要再点目标**"的来源
        （`BP_ChooseOneCard.cpp:172` 把它抄进 actor 的 `needsTarget//0x09A4`）。

        ⚠ 2026-09-25：上一版这个函数读回来是空/空串（我误以为"卡上没有这个数组"）。
        实际 `us_weather_bureau` 的导出里明明有两条 `cardText`；所以这次把**每一层**的
        原始值都带进返回值（`ftext_ok`），读失败时能立刻看出是数组没读到还是 FText 解析失败。
        """
        actor = trigger_actor
        if not actor:
            return []
        from kardsmem.names import ftext_at                     # noqa: PLC0415（本文件惯例：用时才 import）
        acls = self.uclass_of_instance(actor)
        if not acls:
            return []
        obj = self.m.ptr(actor + self.off(acls, "selfBaseCardRef",
                                          NOTE_OFF_ACTOR_SELF_BASECARD)) or 0
        if not obj:
            return []
        ocls = self.uclass_of_instance(obj)
        base = obj + self.off(ocls, "chooseOneCards", NOTE_OFF_CARDOBJ_CHOOSE_ONE_CARDS)
        data = self.m.ptr(base) or 0
        num = self.m.i32(base + 8) or 0
        if verbose:
            print("    chooseOneOptions: actor=0x%X obj=0x%X base=0x%X data=0x%X num=%s"
                  % (actor, obj, base, data, num))
        if not data or num <= 0 or num > 8:
            return []
        out = []
        for i in range(num):
            ent = data + i * PICK_STRUCT_SIZE
            label = ftext_at(self.m, ent, 0)     # FChooseOneCardStruct::cardText 在条目 +0x00
            needs = bool(self.m.u8(ent + 0x10))
            out.append({"index": i, "label": label, "needs_target": needs,
                        "ftext_ok": label is not None})
        if verbose:
            for o in out:
                print("      选项%d label=%r needs_target=%s" % (o["index"], o["label"],
                                                                o["needs_target"]))
        return out

    def _actor_card_id(self, actor: int) -> Optional[int]:
        """`ABP_BaseCard_C::CardID // 0x03C8`（actor 上的那张牌的 id）。"""
        if not actor:
            return None
        cls = self.uclass_of_instance(actor)
        if not cls:
            return None
        return self.peek(actor, self.off(cls, "CardID", NOTE_OFF_ACTOR_CARDID), "s32")

    def pick_pending(self) -> dict:
        """现在**是不是**真的有一个"选一张牌"界面在等（`pick_state().pending`）。

        ★ 2026-09-25 实机教训：**候选 actor 存在 ≠ 界面在等**。点完第三层之后
          `chooseOneActive` 已经回 0、`pending=False`，但那 3 个 `BP_ChooseCardToSpawn_C`
          actor 还活着（GC 没来得及回收）——照 `pick_candidates()` 的列表去点，
          点的是"已经结束的那一层的残留"。所以**动手前必须先问这个**。
        """
        from kardsmem.pick import pick_state
        st = pick_state(self.ks)
        return {"pending": bool(st.get("pending")),
                "choose_one_active": st.get("choose_one_active"),
                "is_selecting_hand_target": st.get("is_selecting_hand_target"),
                "reason": st.get("reason")}

    def pick_groups(self, cache: Optional[dict] = None) -> dict:
        """候选按**触发者**分组 → `{trigger_key: [候选…]}`。

        ★ 为什么要分：一次二选一/三选一会连着弹好几层（US WEATHER BUREAU 二选一 →
          三选一 → 再选具体哪张），而**上一层的 actor 不一定会立刻消失**，
          于是同一个列表里可能混着两组不同触发者的候选（handoff §二3 那次
          就是踩在这里，`pick_choice(index)` 拿"列表里第几个"点错了）。
        """
        groups: dict = {}
        for c in self.pick_candidates(cache=cache):
            key = (c["kind"], c["trigger_actor"] or c["trigger_id"] or 0)
            groups.setdefault(key, []).append(c)
        for v in groups.values():
            v.sort(key=lambda c: c["index"] if c["index"] is not None else 99)
        return groups

    def choose_candidates(self) -> list:
        """当前**三选一**候选 → [{actor, index, name, selected, trigger_card_id}]。

        直接复用 `kardsmem.pick.choose_candidates()`（`BP_ChooseCardToSpawn_C`）。
        ★ 要看**二选一**（或两类一起看）用 `pick_candidates()`；这个函数保留是为了
          跟 `kardsmem`/`agent` 层的既有读法对齐。
        """
        from kardsmem.pick import choose_candidates as _cc
        return _cc(self.ks, owned_only=True)

    def pending_summary(self) -> dict:
        """**现在在等什么**（只读汇总）—— 对标 `ops.py::pick_state()`，但把四类等待一起报。

        三前端（shell/MCP/NN）每一步动手前先问这一个就够：
          `pick`（二选一/三选一/预报的面板在等）/ `hand_target`（选一张手牌当目标）/
          `board_target`（板卡"待点目标"= `cardBeingPlayedFromHand`，单位部署第二阶段或
          指令选项 resolve 之后）/ `arrows`（场上箭头数）/ `queue_running`（动作队列跑着）。
        ★ 只是**读**；判定"能不能动手"是调用方的事（`preflight()` 有汇总入口）。
        """
        return {"pick": self.pick_pending(),
                "candidates": len(self.pick_candidates() or []),
                "hand_target": self.selecting_hand_target(),
                "board_target": self.card_being_played_from_hand(),
                "board_target_loc": self.card_being_played_loc(),
                "arrows": len(self.arrow_actors()),
                "queue_running": self.queue_running()}

    def choose_one_with_target(self, index: int, target_id: int, trigger=None,
                               kind: str = "choose_one", verbose: bool = True) -> dict:
        """**"抉择 + 指向"两步合一**（HIDDEN PLANS / 保密计划那种卡）：先点选项，再点场上单位。

        ★★ 为什么不能拿 `pick_choice().ok` 当闸门（2026-09-27 实机，用户："你那次 pick 进入了
          state after choose。说明 with_select 没成功。是bug。"）：
          这类卡**点选项这一步本来就没有动作流回执** —— `XActionPlayCardFromHand
          {chooseOneIndex}` 要等**目标点完**才进流。实机 `play 16` → `choose 1`：动作流空，
          但 `chooseOneActive` 1→0 且 `cardBeingPlayedFromHand` **=16**（游戏自己进了
          "待点目标"态）。旧写法 `if not pick_choice.ok: return` 就死在这里 ⇒ 永远走不到第二步。
        ⇒ 闸门改成**游戏自己的状态**：只要 `card_being_played_from_hand()` 非 0 就继续点目标；
          真正的 `ok` 由**第二步** `select_unit_target` 的动作流判
          （`XActionPlayCardFromHand{targetCardID}`）。选项那步的 `ok=False` 如实留在 `choice` 里
          （旁证不许进 `ok` —— CLAUDE.md 弯路 #22；判据只能**往后挪一步**，不能降级成旁证）。
        """
        a = self.pick_choice(int(index), trigger=trigger, kind=kind, verbose=verbose)
        playing = self.card_being_played_from_hand()
        if not playing:
            # ① 选项自己就完事了（例：另一支"从牌库抽一张隐蔽单位"，不需要目标）；
            # ② 或者**真**没点上（动作流空 + 也没进待点目标态）。
            if a.get("ok"):
                return {"ok": True, "choice": a, "resolved_at_option": True,
                        "target_result": None}
            return {"ok": False, "choice": a, "reason": "option_not_consumed",
                    "error": "选项点了之后既没有动作流回执、也没进'待点目标'态 ⇒ 这一步没成"
                             "（若这张卡的选项不需要目标，请用 choose_one()）"}
        sel = self.select_unit_target(int(playing), int(target_id), verbose=verbose)
        out = {"ok": bool(sel.get("ok")), "choice": a, "target_result": sel,
               "playing_from_hand": int(playing), "criterion": "action_stream(第二步)"}
        if not a.get("ok"):
            out["note"] = ("选项那步动作流为空是**正常的**（这类卡的回执要等目标点完）——"
                           "判据是 target_result 里的 `XActionPlayCardFromHand{targetCardID}`")
        return out

    def pick_choice(self, index: int = 0, trigger=None, kind: Optional[str] = None,
                    is_effect: Optional[bool] = None, verbose: bool = True) -> dict:
        """点当前抉择界面的第 `index` 个候选（**在同一个触发者分组内**数）。

        `index` 的语义（2026-09-25 修）：**分组内**按屏幕次序的第几个，不是整个列表的
        第几个 —— 列表可能同时混着两层不同触发者的候选（见 `pick_groups()`）。

        `trigger`：显式指定触发者（`trigger_actor` 地址，或触发者的 CardID）。
        不给时：只在**唯一一组**候选时自动选；有多组就报 `ambiguous` 让调用方指定，
        **绝不猜**（猜错就是"点在了不存在的东西上"，还有把对手界面点掉的观感风险）。

        `kind`：`"choose_one"` / `"choose_spawn"`，用来在两个类同时有候选时消歧。

        点击方式：`OnActorClicked(bool IsPrecise)`（`ABP_BaseCard_C` 上，两类都继承它；
        实机验证过需要带参数，传 NULL 会在函数体里读 `IsPrecise` 空指针崩）。
        ★★ 判据（2026-09-27 用户当场纠正"**没pick到**"之后收窄）：**只看动作流**，
        而且只认两形状：
          * `XActionCardToDrawSelected` / `ZActionSelectCardToDrawPending` —— 选择本身发出去了；
          * `XActionPlayCardFromHand` 且 **`chooseOneIndex >= 0`** —— 这张牌的选项被消费了
            （出牌→解析出选择那条链；实测 HIDDEN PLANS 选项层就是它）。
        **不再**把"候选集合变了 / `SelectedCard` 翻 True / 只要有任何 action"算成功：
        本轮 `pick_choice(1)` 报了 `候选 2→1、outcome=advanced`，用户指出**其实没 pick 中**
        —— 那时 `actions=[]`，按旧判据却被 `candidates_changed` 判成了成功（假阳性）。
        集合变化/选中翻转/仍待选只作**旁证**返回（`candidates_changed`/`selected_flip`），
        不参与 `ok`。
        """
        _T = {"t0": time.time()}                                   # 分段计时（2026-10-01：抉择一步曾耗 60 s，查慢在哪）
        cands = self.pick_candidates()
        _T["cands"] = round(time.time() - _T["t0"], 2)
        if not cands:
            return {"ok": False, "error": "现在没有抉择候选在等（两类都为空）"}
        # ★ 动手前确认界面**真的**在等（候选 actor 可能是上一层没回收的残留）
        pd = self.pick_pending()
        _T["pending"] = round(time.time() - _T["t0"], 2)
        if not pd["pending"]:
            return {"ok": False, "error": "现在没有选择界面在等（%s）——别点残留 actor" % pd["reason"],
                    "pick_state": pd}
        cands = [c for c in cands if self.obj_alive(c["actor"])] or []   # 丢掉已销毁的陈旧候选
        if not cands:
            return {"ok": False, "reason": "no_live_candidates", "timing": _T,
                    "error": "候选 actor 都已被销毁/回收（陈旧），不点"}
        if kind:
            cands = [c for c in cands if c["kind"] == kind]
        # ★ 2026-09-27 实机补：预报**第一段 vs 第二段**是同一个 actor 类、
        #   `trigger_actor` 又都是 0 ⇒ 光靠 kind/index **分不开**（那次靠扫描顺序
        #   碰巧点对）。权威分界是 `BP_ChooseCardToSpawn_C::isEffect // 0x980`：
        #   第一段 is_effect=False、第二段/effect 选择 is_effect=True。
        if is_effect is not None:
            cands = [c for c in cands if c.get("is_effect") == bool(is_effect)]
            if not cands:
                return {"ok": False, "reason": "no_such_layer",
                        "error": "按 is_effect=%s 过滤后没有候选（现在等的是另一层？）"
                                 % is_effect}
        if trigger is not None:
            t = int(trigger)
            cands = [c for c in cands
                     if c["trigger_actor"] == t or c["trigger_id"] == t
                     or (c["card_obj"] and c["card_obj"] == t)]
        groups: dict = {}
        for c in cands:
            groups.setdefault((c["kind"], c["trigger_actor"] or c["trigger_id"] or 0), []).append(c)
        # ★ 丢掉"已经不在了"的那一层（实机数据）：
        #   二选一：点过之后 `Clickable` 会从 True 翻 False（点完 index0 两个候选都 False）；
        #   三选一：点过之后被点那个的 `SelectedCard` 翻 True —— 而**正在等你选**的那一层
        #   应该是"一个都没选"的。★ 踩过：OVERCAST 预报点完第一层后，第一层 3 个
        #   （有一个 selected=True）和第二层 3 个（都 False）**同时**在列表里
        #   ⇒ 两个同 kind 的组 ⇒ 旧代码报"ambigUous"拒绝动手，驱动在那一屏空转 18 步。
        live = {}
        for k, v in groups.items():
            if k[0] == "choose_one":
                if any(c["clickable"] is True for c in v):
                    live[k] = v
            elif not any(c["selected"] is True for c in v):
                live[k] = v
        if not live and all(
                (c["clickable"] is False) if c["kind"] == "choose_one"
                else (c["selected"] is True) for c in cands):
            # ★ 2026-09-25 实机补的洞：调用方显式给了 `kind=`（或 trigger=）之后，
            #   过滤结果里**只剩"已经不在的那一层"**时，旧代码 `live or groups` 会退回去
            #   点那个死掉的 actor —— 看起来无害，但那是一次**没有对应语义的输入**。
            #   实测场景：US WEATHER BUREAU 二选一 → 点了"Forecast."之后，二选一那两个
            #   actor 还在（Clickable=False），三选一刚弹出来；调用方按 kind='choose_one'
            #   过滤就会去点那两个残留。这里直接拒绝。
            return {"ok": False, "reason": "layer_gone",
                    "error": "过滤出来的候选全都是**已经结束的那一层**（clickable=False / 已选中），"
                             "别点残留 actor；用不带 kind 的调用或换 kind",
                    "candidates": cands}
        groups = live or groups
        if len(groups) > 1:
            # 同 kind 还有多组（极少）：取 actor 地址最大的那组 —— 新生成的对象地址更大，
            # 是"刚刚弹出来的这一层"。**这只是排序偏好**，不是判据；说出来免得当成事实。
            keys = sorted(groups, key=lambda k: max(c["actor"] for c in groups[k]))
            self.notes.append("多组同 kind 候选 %s，按 actor 地址取最新一组 %s"
                              % (list(groups), keys[-1]))
            groups = {keys[-1]: groups[keys[-1]]}
        if not groups:
            return {"ok": False, "error": "过滤后没有候选（trigger/kind 给错了？）",
                    "candidates": cands}
        if len(groups) > 1:
            return {"ok": False, "error": "有 %d 组不同触发者的候选，必须显式给 trigger= 或 kind="
                                          "（猜错就会点错层）" % len(groups),
                    "groups": {("%s/%s" % k): [(c["index"], c["name"] or c["label"], c["actor"])
                                               for c in v] for k, v in groups.items()}}
        (gkind, gtrig), group = next(iter(groups.items()))
        group.sort(key=lambda c: c["index"] if c["index"] is not None else 99)
        if index < 0 or index >= len(group):
            return {"ok": False, "error": "index=%d 超出该组候选数 %d" % (index, len(group)),
                    "group": group}
        pick = group[index]
        actor = pick["actor"]
        cls = pick["cls"]
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)

        mk = self._mark()
        # ★ 2026-09-26 重构：改走**统一点击原语** —— 它复刻 PC 的真实松手流程
        #   （`GlobalMouseUp(mouseDownActor)` → 未消费才转发 `OnActorClicked`），
        #   并带上 L0 队列闸门 + L1 PC 拖拽状态机。旧写法是"只 hover 一下就直接
        #   `OnActorClicked`"，跳过了 PC 那一跳。
        click_res = self.click_actor(actor, is_precise=1, verbose=True)
        _T["click"] = round(time.time() - _T["t0"], 2)
        if isinstance(click_res, dict) and click_res.get("reason") == "actor_not_alive":
            return {"ok": False, "reason": "actor_not_alive", "picked": pick, "timing": _T,
                    "error": click_res.get("error")}

        # 回执：**动作流**为准（`XActionCardToDrawSelected` = 选择真的发出去了；
        #   `ZActionSelectCardToDrawPending` = 进下一层）。另外三个旁证：
        #   ① 这层的候选集合变了 ② 这个 actor 的 `SelectedCard` 翻 True
        #   ③ 界面不再 pending。
        # ⚠ 实机踩过：只按"候选集合变没变"判会**假阴性**——点二选一那一层时
        #   动作流给的是 `XActionPlayCardFromHand`（那条链是"出牌→解析出选择"），
        #   我第一版只找 pick 那两个类型 ⇒ 明明点成功了却报 ok=False。
        from kardsmem.matchlog import MatchLog
        ml = self.matchlog()          # ★ 长寿命单例（别再每次新建：locate 4~6 s）
        PICK_ACTIONS = ("ZActionSelectCardToDrawPending", "XActionCardToDrawSelected")

        def _choose_one_index(row: dict) -> int:
            """从一条 action 里挖 `chooseOneIndex`（顶层 data 或任意 sub_action 里都找）。"""
            vals = []
            for d in (row.get("data") or []):
                if isinstance(d, dict) and d.get("name") == "chooseOneIndex":
                    vals.append(d.get("value"))
            for s in (row.get("sub_actions") or []):
                for d in ((s or {}).get("values") or []):
                    if isinstance(d, dict) and d.get("name") == "chooseOneIndex":
                        vals.append(d.get("value"))
            vals = [v for v in vals if isinstance(v, int)]
            return max(vals) if vals else -1

        t0 = time.time()
        rows = []
        seen = []
        while time.time() - t0 < 3.0:
            rows = ml.since(mk)
            seen = [r.get("action_type") for r in rows]
            if seen:
                break
            time.sleep(0.05)
        hit = [a for a in seen if a in PICK_ACTIONS]
        consumed = [(i, r.get("action_type"), _choose_one_index(r)) for i, r in enumerate(rows)
                    if r.get("action_type") == "XActionPlayCardFromHand"
                    and _choose_one_index(r) >= 0]
        _T["receipt"] = round(time.time() - _T["t0"], 2)
        after = self.pick_candidates()
        _T["after"] = round(time.time() - _T["t0"], 2)
        changed = (tuple(sorted(c["actor"] for c in after))
                   != tuple(sorted(c["actor"] for c in cands)))
        sel_flip = any(c["selected"] is True for c in after
                       if c["kind"] == gkind and c["index"] == pick["index"])
        still_pending = self.pick_pending()["pending"]
        # ★★ 2026-09-27 实机（HIDDEN PLANS / 保密计划 16，选项 "Reveal a Covert unit and
        #   give it +1+1."）：**带目标的抉择卡在"点选项"这一步动作流是空的** —— 真正的回执
        #   要等目标点完那一下（`ZActionPlayCardFromHand{chooseOneIndex:1, targetCardID:27}`
        #   + `ZActionRevealCard{27}` + 两组 `ZActionGain*`）。
        #   这时游戏自己的状态是"面板关了 + `cardBeingPlayedFromHand` = 这张牌" ⇒ 把它当
        #   **旁证**报出来（`awaiting_target` / `playing_from_hand`），**绝不进 `ok`**
        #   （弯路 #22 的纪律：集合变化、界面关了、选中翻转都不算"成没成"）。
        playing_after_pick = 0
        try:
            playing_after_pick = self.card_being_played_from_hand()
        except Exception:                                        # noqa: BLE001
            playing_after_pick = 0
        _T["end"] = round(time.time() - _T["t0"], 2)
        _T.pop("t0", None)
        awaiting = bool(playing_after_pick) and not still_pending
        # ★★ 判据 = **动作流**（用户 2026-09-27："没pick到"）：只见旁证不算成功。
        ok = bool(hit) or bool(consumed)
        if verbose:
            print("    抉择[%s] 点了第 %d 个（index=%s %s actor=0x%X）→ 动作流=%s "
                  "chooseOne消费=%s 候选 %d→%d 集合变=%s 选中=%s 仍待选=%s "
                  "待点目标=%s ⇒ ok=%s"
                  % (gkind, index, pick["index"], pick["name"] or pick["label"], actor,
                     hit or seen, [c[2] for c in consumed], len(cands), len(after),
                     changed, sel_flip, still_pending,
                     (playing_after_pick or "-"), ok))
        return {"ok": ok,
                "criterion": "action_stream",
                "outcome": ("awaiting_target" if awaiting
                            else ("advanced" if still_pending or changed else "resolved")),
                "kind": gkind, "picked": pick, "actions": hit,
                "consumed_by_play": consumed, "actions_seen": seen,
                "candidates_changed": changed, "selected_flip": sel_flip,
                "still_pending": still_pending,
                # 旁证（不进 ok）：游戏自己已经进了"待点目标"态 ⇒ 这一步还没回执是**正常的**
                "awaiting_target": awaiting,
                "playing_from_hand": playing_after_pick or None,
                "candidates_after": after, "timing": _T}

    def pick_layers(self, index: int = 0, kind: Optional[str] = None,
                    max_layers: int = 4, settle: float = 0.5,
                    verbose: bool = True) -> dict:
        """**一口气走完多层抉择链**（预报"天气→2K/4K/6K"、`US WEATHER BUREAU` 三层那种）。

        对标 `ops.py::act_pick`（旧鼠标版把**两级**写死在签名里：`index`/`index2`）。
        这里**不写死层数**：每点一层就重新读 `pick_pending()`/`pick_candidates()`，
        还在等就再点同一 `index`，直到没有候选等（或到 `max_layers`）。
        ★ 每层的成败**只看动作流**（`pick_choice` 的判据：`XActionCardToDrawSelected` /
          `ZActionSelectCardToDrawPending` / `XActionPlayCardFromHand{chooseOneIndex>=0}`）。
        实机（2026-09-26）：二选一 → 三选一 → 三选一，逐层 `XActionCardToDrawSelected`，
        最后一层点完 `pending=False`（面板自己关）。
        """
        layers = []
        ok = True
        for i in range(max(1, int(max_layers))):
            pd = self.pick_pending()
            cands = self.pick_candidates() if pd.get("pending") else []
            if not cands:
                break
            r = self.pick_choice(index, kind=kind, verbose=verbose)
            layers.append({"layer": i, "ok": bool(r.get("ok")),
                           "actions": r.get("actions"),
                           "consumed_by_play": r.get("consumed_by_play"),
                           "picked": (r.get("picked") or {}).get("name"),
                           "kind": r.get("kind"),
                           "candidates_before": len(cands)})
            if not r.get("ok"):
                ok = False
                break
            self.settle(settle, 4)    # 换层之间等 UI（每层一个新面板）⇒ 按帧等，降帧时不空等
        still = self.pick_pending()
        return {"ok": bool(layers) and ok and not still.get("pending"),
                "layers": layers, "n_layers": len(layers),
                "criterion": "action_stream",
                "still_pending": still.get("pending"), "pick_state": still}

    def choose_card(self, index: int = 0, kind: Optional[str] = None,
                    is_effect: Optional[bool] = None, trigger=None,
                    verbose: bool = True) -> dict:
        """**选一张牌**：当前等着的候选里点第 `index` 个（**在同一个触发者分组内**数）。

        ★★ 2026-09-27 合并（用户："是不是所有选卡最终都落在 notify 上？所以或许可能两种
          不用分开。"）—— **确实落在同一个 notifier 上**，所以对调用方**不需要分两个动词**：
            * `GetChooseSpawnCards` 在**全部导出里只有 1 个调用点**
              （`BP_CardFunctions.cpp:5637`，位于 `selectCardToDraw(...)` 的
              `fromTopOfDeck=false` 分支）；`fromTopOfDeck=true` 分支同样走
              `NotifySelectCardToDrawPending`（`:5610`/`:5705`）。
            * 通知器 → `BP_OnlineMatch::HandleSelectCardToDrawPending` →
              `AddSubActionSelectCardToDrawPending` → **一个**处理器
              `BP_VisualController::ExecuteSubactionSelectCardToDraw`（`:9817`），
              按 sub-action 的 `scrying` 标志分派到
              `BP_ChooseCardToDraw_C`（scrying=1）或 `BP_ChooseCardToSpawn_C`（scrying=0）。
          差异**只在读侧**，而且已经由 `pick_candidates()` 处理掉了：
            * scrying 的候选身份 = actor `CardID // 0x3C8`（本局真实卡）；
            * 非 scrying 的候选身份 = `Name_0 // 0x95C`（模板 FName）。
          调用方只管给 index ⇒ 一个动词足够。

        `kind`：**只在消歧时要**（同一时刻两类候选都在场上）。取值
          `"choose_one"`（卡自己的抉择面板 `BP_ChooseOneCard_C`，**另一个链**：由
          `BP_HandCard::SpawnChooseOneCards` 生成，不经这个 notifier）/
          `"choose_spawn"`（`scrying=0`）/ `"choose_draw"`（`scrying=1`）。
          不给就按 `pick_choice` 的"活跃层"规则自动挑；两类真的同时在等时会报
          `ambiguous`，那时才需要 `kind=`（**不猜**）。

        `is_effect`：**预报两段的权威分界**（第一段 0 / 第二段 1）。同一时刻两层都在等时
          **一定要给**：它们的 `trigger_actor` 都是 0、kind 也一样，`index` 分不开。
        ★ 提交方式：**单击即提交**（三个分支都是 `OnActorClicked` →
          `AddMoveToPlayerMoveQueue`；真按钮/确认键只出现在 `pick_hand` 那条链）。
        判据：**动作流**（`XActionCardToDrawSelected` / `ZActionSelectCardToDrawPending` /
        `XActionPlayCardFromHand{chooseOneIndex>=0}`），见 `pick_choice` 的 docstring。
        """
        return self.pick_choice(index, trigger=trigger, kind=kind,
                               is_effect=is_effect, verbose=verbose)

    def deck_actor(self, refresh: bool = False) -> int:
        """`BP_Deck_C` 实例（**缓存地址**，理由同 `logic_actor`）。

        ⚠ **不能缓存 0**：对局没加载完时 `BP_Deck_C` 还没创建，若把 0 缓存下来，
          之后就永远找不到了（一直返回 None）。只在非 0 时缓存。

        ★ 2026-09-25 真 bug：**同时存在两个 `BP_Deck_C`**——我方一个、对手（含 AI）
          一个，靠 `deckForEnemy`（bool @0x0478）区分。旧版 `instance_of_class()`
          只返回**第一个**找到的实例，遍历顺序不保证是我方那个——挑到对手那份后，
          它的 `showingStartingHand` 永远是我们这边看不到的东西，实测**全程读 0**，
          导致 `wait_for_mulligan()` 死等 90 秒也等不到窗口（症状看起来像"窗口太短
          抓不住"，其实是"问错了对象"）。改成显式过滤 `deckForEnemy==False`。
        """
        self._sync_match()
        if refresh or not self._deck:
            found = 0
            for p in self.instances_of_class("BP_Deck_C"):
                cls = self.uclass_of_instance(p)
                off = self.off(cls, "deckForEnemy", NOTE_OFF_DECK_FOR_ENEMY)
                if not self.peek(p, off, "u8"):
                    found = p
                    break
            self._deck = found
        return self._deck

    def showing_starting_hand(self) -> Optional[bool]:
        """`BP_Deck_C::showingStartingHand` —— **换牌窗口的真正判据**。

        ★ 2026-09-25 实机坐实"点了没反应"的根因：`MulliganDiscardToggle()` 的入口门槛是
          `Deck->showingStartingHand && IsInMyHand`。窗口没开时（这个字段 = 0），
          我们发进去的 hover/click **全部被游戏自己吞掉**，屏幕一动不动 ——
          看起来像"注入失败"，其实是"窗口不在"。所以动手前必须先看这个字段。
        """
        d = self.deck_actor()
        if not d:
            return None
        off = self.off(self.uclass_of_instance(d), "showingStartingHand",
                       NOTE_OFF_DECK_SHOWING_STARTING_HAND)
        return bool(self.peek(d, off, "u8"))

    def wait_for_mulligan(self, timeout: float = 300.0, verbose: bool = True) -> bool:
        """等换牌窗口：**`showingStartingHand == 1`**。

        ★ 两层等待，为了在**几秒的窗口**里抓住它：
          ① 先高频轮询 `PreGameState`（逻辑实例已缓存 ⇒ 单次 ~0.3ms）；
             阶段没到 2/3 之前不去找 `BP_Deck_C`（找它要全量扫 GObjects，代价 ~4s）。
          ② 阶段到了再开始查 `showingStartingHand`（第一次查可能要付一次全扫，
             之后就缓存住、每次 ~0.3ms）。
        ⚠ 之前这里一直用 0.25s 直接查 `showingStartingHand`，而每查一次还要全扫一遍，
          实际采样 ~0.1Hz ⇒ 窗口永远抓不到（实测第一次采样就迟了 11s）。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.pregame_state()
            if st in (2, 3):                       # ShowStartingHand / DoMulligan
                if self.showing_starting_hand():
                    if verbose:
                        print("  等到换牌窗口（showingStartingHand=1，%.1fs）" % (time.time() - t0))
                    return True
            time.sleep(0.05)
        return False

    def mulligan(self, discard=None, wait: float = 0.0, verbose: bool = True) -> dict:
        """换牌：等窗口（可选）→ 把 `discard` 里的牌逐张打标记 → 点确认。

        `discard=[]`（默认）＝一张都不换，直接确认 —— 真人最常见的选择。
        ⚠ 窗口很短（超时 55s，超时走 `MulliganTimedOut()→AutoClick()`，
        和手点**同一条路径**，所以超时确认与手点确认在内存里分不出来）。
        """
        if wait and not self.wait_for_mulligan(wait, verbose=verbose):
            return {"ok": False, "error": "%.0fs 内没等到换牌窗口" % wait}
        gate = self.showing_starting_hand()
        if not gate:
            return {"ok": False,
                    "error": "换牌窗口没开（showingStartingHand=%s）—— 游戏自己的门槛会吞掉所有点击"
                             % gate,
                    "pregame_state": self.pregame_state()}
        marks = [self.mulligan_mark(cid, verbose=verbose) for cid in (discard or [])]
        r = self.mulligan_confirm(verbose=verbose)
        r["marks"] = marks
        return r

    def instances_of_class(self, name: str, skip_cdo: bool = True,
                           cache: Optional[dict] = None) -> list:
        """该类**所有**活实例。

        ⚠ 有些 UI 类（比如 `Battle_Settings_Widget_C`）在进程里同时躺着好几个实例
        ——旧比赛留下的那个 `EndMatch_Surrender` 是空指针，挑错了就会"点了没反应"。

        ★ 2026-09-25：`GObjects` 实测涨到 12 万+ 对象后，单次全量扫描（遍历+查类名）
          要 4~10s——牌组页一套"选模式→选牌组→点开始"下来，若每一步各扫一遍，
          光扫描就能吃掉 30+ 秒，比换牌窗口本身还长（症状：每次都刚好错过窗口）。
          `cache` 传 `scan_classes()` 的结果就能把同一屏幕里的好几次查找**并成一次扫描**。
        """
        if cache is not None:
            return cache.get(name, [])
        return self.scan_classes([name], skip_cdo=skip_cdo).get(name, [])

    def scan_classes(self, names, skip_cdo: bool = True) -> dict:
        """**一次** `GObjects` 全量扫描，同时归类出多个类名各自的活实例列表。

        比对每个类名各调一次 `instances_of_class()` 快 N 倍（N=类名个数）——
        代价只有一次遍历，不是 N 次。用于"同一屏幕上要问好几个类"的场景
        （牌组页：模式列表 + 牌组按钮 + 侧栏，三者同时存在）。

        ★ 2026-09-25：**扫描本体挪进 Frida，在目标进程里执行**（`scanClassesNative`）。
        Python 这边原来的写法每个对象至少 3 次跨进程 `ReadProcessMemory`
        （flags/class/类名），12 万+对象实测 4~10s；同样的指针解引用发生在目标
        进程自己的地址空间里就是纯内存访问，没有系统调用开销。Frida 崩了/还没连上
        就退回 Python 版的全量遍历（正确性兜底，不会因为这个新路径整个用不了）。
        """
        names = list(names)
        try:
            out = self.api().scan_classes(names, skip_cdo)
            if isinstance(out, dict) and "error" not in out:
                return {n: [int(x, 16) for x in out.get(n, [])] for n in names}
        except Exception as e:                                # noqa: BLE001
            self.notes.append("scan_classes：Frida 原生扫描失败，退回 Python 版（%s）" % e)
        want = set(names)
        out = {n: [] for n in want}
        for p in self.oa.iter_objects(skip_cdo=skip_cdo):
            c = self.oa.class_of(p)
            if not c:
                continue
            n = self.pool.fname_of(c)
            if n in want:
                out[n].append(p)
        return out

    def _live_settings_panel(self, verbose: bool = True):
        """挑**当前这一局**的设置面板：`EndMatch_Surrender` 非空的那个。

        ★ 2026-09-28 审计发现：这里原来是"第一个非空指针就用"——投降是**不可逆**、
          没有二次确认的动作（见 `surrender()`），而这个类跟牌组页的按钮同属
          "旧屏幕残留的陈旧实例也可能非空指针"那一类坑（`select_deck()`/
          `list_decks()` 已经在同一类问题上真的崩过一次）。这个类目前没有
          找到像 `deckButton::OnClicked` 那样可靠的"活实例"信号（它的点击句柄
          绑在**面板自己的类**上，是 `BndEvt__..._onClicked__DelegateSignature`
          这种组件绑定事件，不是一个能读 `Num` 的多播委托字段）——**没有就不编**，
          按项目自己的证据标准（CLAUDE.md）：查不到就如实说查不到，不能拿"猜一个"
          冒充"验证过"。
          ⇒ 只有**恰好一个**非空候选时才返回它；多个都非空就拒绝并把候选列表
          原样报出来，让调用方自己看着办（跟 `pick_choice()` 撞到多组候选时
          "必须显式指定，不能猜"是同一个纪律），而不是像原来那样悄悄点第一个。
        """
        cands = []
        for p in self.instances_of_class("Battle_Settings_Widget_C"):
            off = self.off(self.uclass_of_instance(p), "EndMatch_Surrender",
                           NOTE_OFF_SETTINGS_SURRENDER_BTN)
            if self.m.ptr(p + off):
                cands.append(p)
        if len(cands) == 1:
            return cands[0]
        if verbose:
            if not cands:
                print("    _live_settings_panel：没有 EndMatch_Surrender 非空的实例")
            else:
                print("    _live_settings_panel：%d 个实例的 EndMatch_Surrender 都非空"
                      "（%s）—— 分不清哪个是当前这一局的，拒绝猜、不返回任何一个"
                      % (len(cands), ", ".join(hex(c) for c in cands)))
        return 0

    def call_raw_arr(self, obj: int, func: int, parms: bytes, arr_off: int,
                     ptrs) -> Optional[bytes]:
        """调一个函数，并在 `arr_off` 处塞一个**真实 TArray<UObject*>**（data 在目标进程里 alloc）。

        用来调 `cardsCheckFunctions::CanAttack` —— 它的 `cardsInAttackedLocation`
        是引用参数，必须由调用方组（防守方那一行/那一侧的卡对象）。
        """
        out = self.api().callRawArr(hex(obj), hex(func), bytes(parms).hex(), int(arr_off),
                                    [hex(int(p)) for p in (ptrs or [])])
        return bytes(out) if isinstance(out, (bytes, bytearray)) else out

    def call_raw_hold(self, writes, obj: int, func: int, parms: bytes,
                      arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
        """**临时写几个字段 → 调用 → 立刻还原**（同一次 JS 执行）。

        `writes` 同 `write_and_call` 的格式 `[[objHex, off, kind, value], …]`。
        用来问那些"只在拖拽中间态里才有答案"的函数：`can_move_to` 靠它把
        `PlayerController->SelectedCard` 临时指到目标卡上，问完还回去。

        ★ `arr_off` 不给 = 这个函数**没有引用数组参数**，一个字节都不许碰 parms。
          踩过：`callRawArr` 那条路无条件在 `arrOff` 处写 TArray 头（16 字节），
          而 `CanMoveCardToLocation` 的 `Location@0x00` 正好在头里 ⇒ 被盖成 0。
        """
        out = self.api().callRawArrHold(
            [[hex(int(w[0])), int(w[1]), w[2], w[3] if w[2] != "ptr" else hex(int(w[3]))]
             for w in writes],
            hex(obj), hex(func), bytes(parms).hex(),
            -1 if arr_off is None else int(arr_off),
            [hex(int(p)) for p in (ptrs or [])])
        return bytes(out) if isinstance(out, (bytes, bytearray)) else out

    def call_raw_writes(self, writes, obj: int, func: int, parms: bytes,
                        arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
        """**写若干字段 → 带参调用一次 → 按 mask 还原**（都在同一次 JS 执行里）。

        `writes` 同 `call_raw_hold`，但每项可以带第 5 个元素 `hold=True`
        （JS 里表现为 1）表示"这一项**不要还原**"。

        ★ 为什么需要这个变体：两阶段"点目标"（`select_unit_target`）里，
          箭头 `overCardID` 和卡对象 `targetOverride` 必须在
          `BP_Logic_C::GlobalMouseUp` 的**同一次执行**里写进去（箭头 tick 每帧按真实
          鼠标重算）；而 `GlobalMouseUpBattle` 内部会**销毁箭头**
          ⇒ 箭头那几项不能再用（可能已回收的）指针去还原，卡对象那项必须还原。
        """
        out = self.api().writeCallRaw(
            [[hex(int(w[0])), int(w[1]), w[2],
              (w[3] if w[2] != "ptr" else hex(int(w[3]))),
              (1 if (len(w) > 4 and w[4]) else 0)]
             for w in writes],
            hex(obj), hex(func), bytes(parms).hex(),
            -1 if arr_off is None else int(arr_off),
            [hex(int(p)) for p in (ptrs or [])])
        return bytes(out) if isinstance(out, (bytes, bytearray)) else out

    def call_out_u8(self, obj: int, func: int) -> Optional[int]:
        """调用一个"只有一个 bool 出参"的函数，返回那个出参。

        （ParmsSize=1，进去时是全 0 的缓冲；`ToggleSettingsMenu(bool* IsNowVisible)` 就是这种。）
        """
        b = self.api().call_raw(hex(obj), hex(func), "00")
        return int(b[0]) if b else None

    def _ensure_settings_open(self, panel: int, pcls: int, verbose: bool = True) -> bool:
        """把设置面板**收敛到"打开"**，返回是否成功。

        ★ 为什么不能像原来那样直接点齿轮：齿轮是**开关**。面板本来就开着时再点一次
          就把它关上了，然后再去点"投降"——就是点在看不见的按钮上（用户指出的 bug）。
          这里改用游戏自己的 `ToggleSettingsMenu(bool* IsNowVisible)`：它**告诉我们
          结果状态**。第一次调用把它翻一下并读结果，不是 True 就再翻一次，最多 3 次
          ⇒ 无论起始状态如何，结束时一定是"打开"。
        """
        f = self.fn(pcls, "ToggleSettingsMenu")
        for i in range(3):
            vis = self.call_out_u8(panel, f)
            if verbose:
                print("    ToggleSettingsMenu -> IsNowVisible=%s" % vis)
            if vis:
                return True
            self.settle(0.35, 6)      # 等设置面板真的打开（要它渲染）⇒ 按帧等
        return False

    # ------------------------------------------------------------ 投降（注入版）
    def surrender(self, confirm: bool = False, verbose: bool = True) -> dict:
        """投降，全程注入（不再用真实鼠标点 (1240,30)/(1134,57)）。

        ★ 跟 `ops.py::surrender` 同等纪律：这是**不可逆**动作，而且游戏**一键、没有二次
          确认**（规格 §7.6e）⇒ 自己做一道闸：`confirm=True` 才真发，否则原样返回
          `needs_confirm=True`（调用方/前端据此提问，而不是默默执行）。

        真实流程（规格 §7.6e + SDK 字段）：
          齿轮 `WBP_TopCornerButtons_Settings_C::SettingsPanelButton`
            → 设置面板 `Battle_Settings_Widget_C` 展开
            → 点 `EndMatch_Surrender`（`verticalKardsButtonWithText_Widget_C`）
            → 该按钮的 onClicked 绑到
              `BndEvt__Battle_Settings_Widget_EndMatch_Surrender_..._2_onClicked__DelegateSignature(Button)`
            → `OnEndMatchSurrenderClicked()` → `EndMatchFromAction("surrender")`
        ★ 投降**一键、没有二次确认**（规格 §7.6e），而且服务端可配置禁用
          （`surrendered_disabled_at_start`）—— 调用方要自己负责。
        """
        if not confirm:
            return {"ok": False, "needs_confirm": True,
                    "error": "surrender 不可逆，而且游戏没有二次确认；确定要投降请传 confirm=True"}
        panel = self._live_settings_panel(verbose=verbose)
        if not panel:
            return {"ok": False, "error": "挑不出当前这一局唯一的 Battle_Settings_Widget_C"
                                          "（要么全是空指针，要么有多个都非空、分不清哪个是活的——"
                                          "看上面打印的候选列表，`notes` 也会记一笔）"}
        pcls = self.uclass_of_instance(panel)

        # ① **明确把设置面板置为可见**，而不是去点齿轮。
        #    ★ 用户指出的 bug：齿轮是**开关**（`ToggleSettingsMenu`）。面板本来就开着时
        #      再点一次就把它关上了，然后再去点"投降"——等于点一个看不见的按钮。
        #      而且进程里有**多个**齿轮实例（旧的按钮指针是空的），逐个点更是乱的。
        #    ⇒ 改用游戏自己的 `SetSettingsVisibility(true)`：**幂等**的 setter
        #      （`ToggleSettingsMenu` 内部就是它在改状态），开着的再设一次还是开着。
        f_set = self.find_fn(pcls, "SetSettingsVisibility")
        if f_set:
            self.call_i32(panel, f_set, 1)
            self.settle(0.35, 6)      # 等设置面板打开 ⇒ 按帧等（降帧时 0.35 s 里可能一帧没跑）
            if verbose:
                print("    SetSettingsVisibility(True) 已发出（幂等，不依赖当前开关状态）")
        else:
            return {"ok": False, "error": "找不到 SetSettingsVisibility，不敢盲点齿轮"}

        # ② 投降按钮：先做按钮自己的悬停/按下/松开（可见效果），再触发它的 onClicked
        btn = self.m.ptr(panel + self.off(pcls, "EndMatch_Surrender", NOTE_OFF_SETTINGS_SURRENDER_BTN))
        if not btn:
            return {"ok": False, "error": "EndMatch_Surrender 指针为空"}
        bcls = self.uclass_of_instance(btn)
        # 投降按钮的悬停/按下/松开之间按帧等（同换牌确认：失焦降帧时按下态不会渲染）
        for suffix, pause, frames in (("2_OnButtonHoverEvent", 0.25, 6), ("0_OnButtonPressedEvent", 0.08, 4),
                                      ("1_OnButtonReleasedEvent", 0.05, 3)):
            f = self.find_fn(bcls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_%s__DelegateSignature" % suffix)
            if f:
                self.call0(btn, f)
                self.settle(pause, frames)
        f_click = self.fn(
            pcls, "BndEvt__Battle_Settings_Widget_EndMatch_Surrender_"
                  "K2Node_ComponentBoundEvent_2_onClicked__DelegateSignature")
        self.call_ptr(panel, f_click, btn)          # 这个句柄带一个 Button 入参
        if verbose:
            print("    投降按钮 onClicked 已发出")

        t0 = time.time()
        while time.time() - t0 < 4.0:
            from kardsmem import board as BA
            if BA.open_source("mem").snapshot().match_finished:
                break
            time.sleep(0.2)
        from kardsmem import attach as _attach
        from kardsmem import board as BA
        fin = BA.open_source("mem").snapshot().match_finished
        return {"ok": bool(fin), "match_finished": fin}

    # ------------------------------------------------------------ 开局流程（一屏一步）
    # ★ 纪律：**一屏一步，不自动连点**。每一步单独调用、单独验证（屏幕状态变了才走下一步），
    #   免得盲点在菜单里乱点把 UI 搅乱。
    def end_of_match_continue(self, verbose: bool = True) -> dict:
        """结算页（失败/胜利）点"继续"。

        实机核实（2026-09-25）：
          * 真实点击落在**全屏的 `ClickThrough`**（`UButton`，`Visibility=0` Visible）；
          * `LeaveBattlefield_Button` 此刻是 `SelfHitTestInvisible(4)` —— **点不到**，
            所以不能拿它当"继续"。
        绑定句柄在 `W_EndOfMatch_C` 上：
          `BndEvt__WBP_NUI_EndOfMatch_ClickThrough_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature`
        ⚠ 游戏里有 `allowClickThroughAfterDelay(Delay)` —— 太早点它不生效；调用方自己等够。
        """
        ws = self.instances_of_class("W_EndOfMatch_C")
        if not ws:
            return {"ok": False, "error": "没有 W_EndOfMatch_C（不在结算页？）"}
        w = ws[0]
        cls = self.uclass_of_instance(w)
        off_ct = self.off(cls, "ClickThrough", NOTE_OFF_EOM_CLICKTHROUGH)
        btn = self.m.ptr(w + off_ct)
        if not btn:
            return {"ok": False, "error": "ClickThrough 指针为空"}
        vis = self.peek(btn, NOTE_OFF_WIDGET_VISIBILITY, "u8")
        if vis != 0:
            return {"ok": False,
                    "error": "ClickThrough 当前不可点（Visibility=%s，0 才是 Visible）" % vis}
        f = self.fn(cls, "BndEvt__WBP_NUI_EndOfMatch_ClickThrough_"
                         "K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature")
        off_step = self.off(cls, "step", NOTE_OFF_EOM_STEP)
        before = self.peek(w, off_step, "u8")
        self.call0(w, f)
        # ★ 点一次就要能看到**这一步真的推进了**（`step` 变、或整个控件消失）——
        #   不做"盲点一长串"。`E_EndOFMatchStep` 是 0..8。
        # ★ 2026-09-25：这里原来每 0.15s 就 `instances_of_class()` 全扫一次判断
        #   "控件是不是没了"——`GObjects` 涨到 12 万+ 后单次全扫 4~10s，
        #   一个 4 秒的等待循环硬是被拖成十几秒。改用**同一个** `w` 指针复查
        #   `class_of()`（2 次内存读 vs 遍历 12 万个对象），便宜得多。
        t0 = time.time()
        after = before
        while time.time() - t0 < 4.0:
            still_alive = self.pool.fname_of(self.oa.class_of(w)) == "W_EndOfMatch_C"
            if not still_alive:
                after = None
                break
            after = self.peek(w, off_step, "u8")
            if after != before:
                break
            time.sleep(0.15)
        advanced = (after != before)
        if verbose:
            print("    结算页 ClickThrough 已点：step %s -> %s%s"
                  % (before, after, "" if advanced else "（没推进！可能 click-through 还没解锁）"))
        return {"ok": advanced, "step_before": before, "step_after": after,
                "advanced": advanced, "left_screen": after is None}

    # ------------------------------------------------------------ 牌组页（模式 + 开始）
    # 模式列表 = `WBP_NUI_Playbar_C`（左侧那一列）。每个模式是一个
    # `WBP_NUI_MasterPlaybarButton_C`，绑定的 onClicked 句柄名里带蓝图节点序号。
    #
    # ⚠⚠ **下面这张表的"节点名/序号 → 模式"对应关系是错的（2026-09-25 实机证伪）**：
    #   点了我以为是 training 的那一对句柄（hover `..._TrainingButton_..._10` +
    #   click `..._Training_..._5`）之后，界面从 **training（start_btn y=604）** 变成了
    #   **versus/casual**（probe: `casual_mode_btn` 命中 1.0、`start_btn` 找不到）——
    #   而 versus 才有"休闲"页签，training 上方什么都没有。⇒ 蓝图节点名（`Training`）
    #   跟它实际管哪个模式**对不上**，不能靠名字推。
    #   下一步要么在 Playbar 上找一个"当前选中模式"的字段，要么**一次点一个**、
    #   用 `start_btn` 的 y（604=训练 / 625=对战·休闲 / 651=对战·经典）当观测量把表对出来。
    PLAYBAR_MODES = {
        "training": ("TrainingButton", "Training", 5),
        "versus":   ("BattleButton", "Battle", 6),
        "skirmish": ("SkirmishButton", "Draft", 7),
        "campaign": ("CampaignButton", "Campaign", 9),
        "code":     ("BattleCodeButton", "BattleCode", 0),
    }

    def playbar(self, cache: Optional[dict] = None) -> int:
        """挑**这一屏**的 Playbar（TrainingButton 非空的那个 —— 又是陈旧实例坑）。"""
        for p in self.instances_of_class("WBP_NUI_Playbar_C", cache=cache):
            cls = self.uclass_of_instance(p)
            btn = self.m.ptr(p + self.off(cls, "TrainingButton", NOTE_OFF_PLAYBAR_TRAINING))
            if btn:
                return p
        return 0

    # ★ 牌组页四个类（模式列表 / 牌组按钮 / 侧栏 / 全局赛况数据）同屏共存——
    #   `quick_start()` 靠这张表一次扫完，后面几步全部传 cache，不再各扫各的。
    #   `BP_MatchData_C` 用来验证"选牌组"是不是真生效了（见 `select_deck` 的 verify+retry）。
    DECK_SCREEN_CLASSES = ("WBP_NUI_Playbar_C", "W_MatchDeckSelectionDeckButton_C",
                           "W_DeckSelectedSideBar_C", "BP_MatchData_C")

    def deck_screen_scan(self) -> dict:
        """一次扫完牌组页要用到的几个类，供 `quick_start()`/手动分步调用共享。"""
        return self.scan_classes(self.DECK_SCREEN_CLASSES)

    def chosen_deck_id(self, cache: Optional[dict] = None) -> Optional[int]:
        """`BP_MatchData_C::chosenDeckID`——当前真正生效的牌组 ID（不是 UI 摆设）。

        用来在"选牌组"点完之后**验证真的换了**，而不是信一次点击就完事。
        """
        rows = self.instances_of_class("BP_MatchData_C", cache=cache)
        if not rows:
            return None
        md = rows[0]
        cls = self.uclass_of_instance(md)
        off = self.off(cls, "chosenDeckID", NOTE_OFF_MATCHDATA_CHOSEN_DECK_ID)
        return self.peek(md, off, "s32")

    def select_mode(self, mode: str = "training", verbose: bool = True) -> dict:
        """在左侧模式列表里点一个模式（悬停 → 点击）。"""
        spec = self.PLAYBAR_MODES.get(mode)
        if not spec:
            return {"ok": False, "error": "未知模式 %r（可选 %s）" % (mode, list(self.PLAYBAR_MODES))}
        field, node, click_idx = spec
        bar = self.playbar()
        if not bar:
            return {"ok": False, "error": "找不到这一屏的 WBP_NUI_Playbar_C"}
        cls = self.uclass_of_instance(bar)
        btn = self.m.ptr(bar + self.off(cls, field, NOTE_OFF_PLAYBAR_TRAINING))
        if not btn:
            return {"ok": False, "error": "%s 指针为空" % field}
        f_hover = self.find_fn(
            cls, "BndEvt__WBP_NUI_Playbar_%sButton_K2Node_ComponentBoundEvent_10_onHovered__DelegateSignature"
                 % node)
        if f_hover:
            self.call0(bar, f_hover)
            self.settle(0.3, 6)       # 模式列表：悬停停留按帧等（降帧时悬停要真的被处理）
        f_click = self.fn(
            cls, "BndEvt__WBP_NUI_Playbar_%s_K2Node_ComponentBoundEvent_%d_onClicked__DelegateSignature"
                 % (node, click_idx))
        self.call0(bar, f_click)
        self.settle(0.8, 6)           # 点了模式后等界面切换 ⇒ 按帧等
        if verbose:
            print("    模式列表：已点 %s（悬停%s）" % (mode, "有" if f_hover else "无"))
        return {"ok": True, "mode": mode, "button": hex(btn)}

    def _live_deck_sidebar(self, cache: Optional[dict] = None, verbose: bool = False):
        """挑**这一屏**的 `W_DeckSelectedSideBar_C`：`PlayButton` 内层 `UButton`
        的 `OnClicked` **有订阅者**的那个。返回 `(sidebar, play_button, inner_button)`
        三个指针（后两个可能是 0），找不到活实例时 `sidebar=0`。

        ★ 2026-09-28 审计发现：`select_deck()`/`list_decks()` 已经在**同一屏**的
          牌组按钮上踩过"陈旧实例静态判据分不出来"这个坑，加了 delegate 订阅者数
          判据（`live = delegate_num(btn, NOTE_OFF_UBUTTON_ONCLICKED) > 0`）。
          但 `deck_start()`/`press_play()`/`start_enabled()` 这三个原来各自独立扫
          **同一个类**（`W_DeckSelectedSideBar_C`）、只看 `PlayButton` 指针是否
          非空——完全是同一个坑，只是没传播到这三处。收成一个方法，三处共用。
        """
        for s in self.instances_of_class("W_DeckSelectedSideBar_C", cache=cache):
            cls = self.uclass_of_instance(s)
            pb = self.m.ptr(s + self.off(cls, "PlayButton", NOTE_OFF_SIDEBAR_PLAY_BTN))
            if not pb:
                continue
            pcls = self.uclass_of_instance(pb)
            inner = self.m.ptr(pb + self.off(pcls, "Button", NOTE_OFF_PLAYBAR_INNER_BTN))
            if inner and self.delegate_num(inner, NOTE_OFF_UBUTTON_ONCLICKED) > 0:
                return s, pb, inner
        if verbose:
            print("    _live_deck_sidebar：没有 PlayButton 内层 OnClicked 有订阅者的"
                  " W_DeckSelectedSideBar_C（都是陈旧实例，或者不在牌组页）")
        return 0, 0, 0

    def deck_start(self, verbose: bool = True, cache: Optional[dict] = None) -> dict:
        """牌组页右下角的"开始"（`W_DeckSelectedSideBar_C` 的 PlayButton）。"""
        side, _pb, _inner = self._live_deck_sidebar(cache=cache, verbose=verbose)
        if not side:
            return {"ok": False, "error": "没有 live 的 W_DeckSelectedSideBar_C"
                                          "（PlayButton 内层 OnClicked 无订阅者，可能是陈旧实例）"}
        cls = self.uclass_of_instance(side)
        f = self.fn(cls, "BndEvt__WBP_NUI_DeckSelectedSideBar_playButton_"
                         "K2Node_ComponentBoundEvent_8_onClicked__DelegateSignature")
        self.call0(side, f)
        if verbose:
            print("    牌组页：PlayButton onClicked 已发出")
        self.settle(1.5, 6)           # 牌组页"开始"→等界面推进 ⇒ 按帧等（用户怀疑的"开始按钮"就是这里）
        return {"ok": True}

    def list_decks(self, cache: Optional[dict] = None) -> list:
        """牌组页上每个牌组按钮 → [{index, name, actor, vis, button, live}]（**只读，不点**）。

        `W_MatchDeckSelectionDeckButton_C` 的字段（SDK）：
            `deckName`   (UTextBlock*) @0x3C0   ← **牌组名可从内存读**，不用认字/认像素
            `deckButton` (UButton*)    @0x3C8   ← 真正可点的那个
        实测这一屏会一次性建出几十个按钮实例，所以**按名字挑**比按坐标挑稳。

        ★ **陈旧实例**：`GObjects` 全扫会连同名字重复的旧屏幕残留一起扫出来
          （实机撞见过 31 个牌组变成 62 条，一模一样的名字各两份）。单看 `vis`/
          `bIsEnabled` 分不出哪份是真的在接收点击——两份读出来经常一样。
          `live` 用 `deckButton::OnClicked` 的订阅者数判（`Num>0` 才是真的接了线，
          同 handoff §四·3 里 sidebar `PlayButton` 那套判据）。
        """
        from kardsmem.names import ftext_at
        out = []
        for p in self.instances_of_class("W_MatchDeckSelectionDeckButton_C", cache=cache):
            cls = self.uclass_of_instance(p)
            off_n = self.off(cls, "deckName", NOTE_OFF_DECKBTN_NAME)
            off_b = self.off(cls, "deckButton", NOTE_OFF_DECKBTN_BUTTON)
            tb = self.m.ptr(p + off_n)
            btn = self.m.ptr(p + off_b)
            name = None
            if tb:
                try:
                    name = ftext_at(self.m, tb, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    name = None
            live = self.delegate_num(btn, NOTE_OFF_UBUTTON_ONCLICKED) > 0 if btn else False
            out.append({"actor": hex(p), "index": len(out), "name": name,
                        "vis": self.peek(p, NOTE_OFF_WIDGET_VISIBILITY, "u8"),
                        "button": hex(btn) if btn else None, "live": live})
        return out

    def select_deck(self, name: str = None, index: int = None, verbose: bool = True,
                    cache: Optional[dict] = None) -> dict:
        """选牌组：按名字（或序号）点那个牌组按钮。

        句柄：`BndEvt__WBP_EditDeckSelectionDeck_deckButton_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature`
        （注意名字里带的是 `WBP_EditDeckSelectionDeck`，跟类名 `W_MatchDeckSelectionDeckButton` 不一致 ——
         蓝图复用留下的，别按类名猜）。

        ★ **多 live 实例**：`OnClicked` 有订阅者只能说明"点了会有人接"，不能说明
          "接的就是当前这一屏"——实机撞见过同名牌组 2 个实例**都** live（都有订阅者）
          的情况，静态判据分不出谁是真的。改成**点完就验**：读
          `BP_MatchData_C::chosenDeckID` 点前点后有没有变，没变就换下一个 live 候选
          再试一次，而不是赌第一个候选一定对。
        """
        rows = self.list_decks(cache=cache)
        matches = []
        for r in rows:
            if index is not None and r["index"] == index:
                matches = [r]
                break
            if name is not None and r["name"] and name.lower() in r["name"].lower():
                matches.append(r)
        if not matches:
            return {"ok": False, "error": "没找到牌组 name=%r index=%r" % (name, index),
                    "decks": [(r["index"], r["name"]) for r in rows]}
        live_matches = [r for r in matches if r.get("live")]
        candidates = live_matches or matches
        if len(matches) > 1 and verbose:
            print("    ⚠ %r 匹配到 %d 个同名实例（live=%d 个），按顺序试、点完验证 chosenDeckID"
                  % (name, len(matches), len(live_matches)))
        if not live_matches:
            return {"ok": False, "error": "只找到陈旧实例（OnClicked 订阅者数=0），点了也没用",
                    "deck": candidates[0] if candidates else None}

        # ★★ 2026-09-25 血的教训（实机崩溃复现过）：**绝不对第二个候选发起调用**。
        #   真实鼠标每次点击都是当场重新命中测试，指哪打哪；注入用的是**提前扫好
        #   缓存的指针**——第一次点击一旦让引擎重建了这一屏（哪怕只是这一个按钮的
        #   父级列表），候选列表里其它指针就可能变成野指针，再调用就是
        #   `EXCEPTION_ACCESS_VIOLATION`（复现过一次，读了个位于 0x1f 附近的地址，
        #   典型的"对着已经不在了的对象调引擎函数"）。这里只点**一个**——排第一的
        #   live 候选——点完只做**只读**验证，不管有没有变，都不再碰任何别的指针。
        pick = candidates[0]
        if not pick["button"]:
            return {"ok": False, "error": "该牌组按钮指针为空", "deck": pick}
        before = self.chosen_deck_id(cache=cache)
        actor = int(pick["actor"], 16)
        cls = self.uclass_of_instance(actor)
        f_hover = self.find_fn(cls, "BndEvt__WBP_NUI_MatchDeckSelectionDeckButton_Button_0_"
                                    "K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature")
        if f_hover:
            self.call0(actor, f_hover)
            self.settle(0.3, 6)       # 牌组按钮悬停停留按帧等
        f_click = self.fn(cls, "BndEvt__WBP_EditDeckSelectionDeck_deckButton_"
                               "K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature")
        self.call0(actor, f_click)
        self.settle(0.8, 6)           # 点完牌组等 chosenDeckID 真的变 ⇒ 按帧等
        after = self.chosen_deck_id()          # ★ 不传 cache：chosenDeckID 是刚点出来的新状态，必须现读
        changed = (after is not None and after != before)
        if verbose:
            print("    已选牌组：%s（index=%s）chosenDeckID %s -> %s%s"
                  % (pick["name"], pick["index"], before, after,
                     "" if changed else "（没变——可能本来就选中了它，也可能没点上，不再试第二个候选）"))
        return {"ok": True, "deck": pick["name"], "index": pick["index"],
                "chosen_deck_id": after, "unverified": not changed}

    def list_toggles(self) -> list:
        """找 `RankedCasualToggle`（排位/休闲）—— 对战模式才有。"""
        for s in self.instances_of_class("W_DeckSelectedSideBar_C"):
            cls = self.uclass_of_instance(s)
            t = self.m.ptr(s + self.off(cls, "RankedCasualToggle", NOTE_OFF_SIDEBAR_RANK_TOGGLE))
            if t:
                return [{"sidebar": hex(s), "toggle": hex(t),
                         "left": hex(self.m.ptr(t + 0x350) or 0),
                         "right": hex(self.m.ptr(t + 0x348) or 0)}]
        return []

    def list_modes(self, cache: Optional[dict] = None) -> list:
        """模式列表每个按钮的**标签**（从内存读，`TextArea` 是 UTextBlock）—— 只读。

        为什么要按标签而不是按蓝图节点名：上一次实机证伪过，节点名 `Training` 那对句柄
        点下去变成了**对战**。`WBP_NUI_MasterPlaybarButton_C` 自带 `TextArea`(0x348) 和
        `Button`(0x368)，所以能"看名字点"。
        """
        from kardsmem.names import ftext_at
        bar = self.playbar(cache=cache)
        if not bar:
            return []
        cls = self.uclass_of_instance(bar)
        out = []
        for field, note in (("WorldChampionshipButton", NOTE_OFF_PLAYBAR_WORLD),
                            ("TrainingButton", NOTE_OFF_PLAYBAR_TRAINING),
                            ("TournamentButton", NOTE_OFF_PLAYBAR_TOURNAMENT),
                            ("SkirmishButton", NOTE_OFF_PLAYBAR_SKIRMISH)):
            btn = self.m.ptr(bar + self.off(cls, field, note))
            if not btn:
                out.append({"field": field, "label": None, "button": None})
                continue
            bcls = self.uclass_of_instance(btn)
            ta = self.m.ptr(btn + self.off(bcls, "TextArea", NOTE_OFF_PLAYBAR_TEXTAREA))
            inn = self.m.ptr(btn + self.off(bcls, "Button", NOTE_OFF_PLAYBAR_INNER_BTN))
            label = None
            if ta:
                try:
                    label = ftext_at(self.m, ta, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    label = None
            out.append({"field": field, "label": label, "actor": hex(btn),
                        "button": hex(inn) if inn else None,
                        "vis": self.peek(btn, NOTE_OFF_WIDGET_VISIBILITY, "u8")})
        return out

    def widget_enabled(self, widget: int) -> Optional[bool]:
        """`UWidget::bIsEnabled` 是**位域**：`0x00D9` 那一字节的 **bit 2**（SDK: BitIndex 0x02）。

        ⇒ 判断按钮是不是"灰的"就读这一位（别只看 Visibility —— 灰按钮照样 Visible）。
        """
        b = self.peek(widget, NOTE_OFF_WIDGET_ENABLED_BYTE, "u8")
        if b is None:
            return None
        return bool(b & NOTE_OFF_WIDGET_ENABLED_MASK)

    def delegate_num(self, obj: int, off: int) -> int:
        """多播委托（`TArray<FScriptDelegate>`）的订阅者数：`Data`@+0、`Num`@+8。

        ★ handoff §12.6：判断"这个 UI 到底有没有接线"，读 `Num` 比看 `bIsEnabled`/
        `Visibility` 硬——同名控件的陈旧实例（旧屏幕残留、还没被销毁的那一份）
        订阅者表通常是空的（`Num=0`），可点、可见，点了却没人接。用这个当"活实例"判据。
        """
        if not obj:
            return 0
        return self.peek(obj, off + 8, "s32") or 0

    def deck_invalid_reason(self, name: str = None, index: int = None) -> Optional[str]:
        """读某个牌组按钮上"为什么不能打"的原文（`InvalidDeckReasonText`，URichTextBlock@0x360）。

        实测：选 `预备日快` 后开始按钮变灰，理由是 **`含未拥有卡牌`** —— 游戏自己说的，
        比我们猜"是不是预备卡的问题"可靠。
        """
        from kardsmem.names import ftext_at
        for r in self.list_decks():
            if (index is not None and r["index"] == index) or \
               (name is not None and r["name"] and name.lower() in r["name"].lower()):
                a = int(r["actor"], 16)
                c = self.uclass_of_instance(a)
                p = self.m.ptr(a + self.off(c, "InvalidDeckReasonText", NOTE_OFF_DECKBTN_REASON))
                if not p:
                    return None
                try:
                    return ftext_at(self.m, p, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    return None
        return None

    def start_enabled(self, cache: Optional[dict] = None) -> dict:
        """开始按钮到底能不能点 —— **看内层 `UButton`**，不是外层包装。

        ★ 实测坑：`W_DeckSelectedSideBar_C::PlayButton`（`WBP_NUI_MasterTextButton_C`）
          外层的 `bIsEnabled` 一直是 True，而**内层 `UButton` 才是真的开关**：
          选了 `预备日快` 之后外层仍 True、内层变 False（开始按钮灰）。
          ⇒ 只读外层会以为"能点"，然后点在一个灰按钮上（和前面几个坑同一类）。
        ★ 2026-09-28：改走 `_live_deck_sidebar()`——原来这里直接拿第一个
          `PlayButton` 非空的实例，同一屏可能有陈旧实例混在里面（跟 `press_play()`
          是同一个坑，见那边的注释）。
        """
        side, pb, inner = self._live_deck_sidebar(cache=cache)
        if not side:
            return {"play_button": None, "clickable": False,
                    "error": "没有 live 的 W_DeckSelectedSideBar_C（不在牌组页，或全是陈旧实例）"}
        return {"play_button": hex(pb),
                "outer_enabled": self.widget_enabled(pb),
                "inner": hex(inner) if inner else None,
                "inner_enabled": self.widget_enabled(inner) if inner else None,
                "clickable": bool(self.widget_enabled(inner)) if inner else False}

    def select_mode_by_label(self, label: str = "TRAINING", verbose: bool = True,
                             cache: Optional[dict] = None) -> dict:
        """按**标签**选模式，并点按钮**自己**的句柄（悬停→按下→松开→点击）。

        不猜 playbar 那一层的 `BndEvt__..._Training_..._5` —— 那个节点名实机证伪过。
        `WBP_NUI_MasterPlaybarButton_C` 自己就有完整 4 个句柄
        （hover `_4`/`_6`、pressed `_9`、released `_8`、clicked `_3`）。
        """
        P = "BndEvt__WBP_NUI_MasterPlaybarButton_Button_K2Node_ComponentBoundEvent_"
        modes = self.list_modes(cache=cache)
        for m in modes:
            if not (m.get("label") and label.lower() in m["label"].lower()):
                continue
            btn = int(m["actor"], 16)
            cls = self.uclass_of_instance(btn)
            # 四步 + 点击后等界面：全部按帧等（失焦降帧时裸 sleep 里游戏一帧没跑）
            seq = ((self.find_fn(cls, P + "4_OnButtonHoverEvent__DelegateSignature"), 0.30, 6),
                   (self.find_fn(cls, P + "9_OnButtonPressedEvent__DelegateSignature"), 0.08, 4),
                   (self.find_fn(cls, P + "8_OnButtonReleasedEvent__DelegateSignature"), 0.05, 3),
                   (self.fn(cls, P + "3_OnButtonClickedEvent__DelegateSignature"), 0.80, 6))
            for f, dt, frames in seq:
                if f:
                    self.call0(btn, f)
                    self.settle(dt, frames)
            if verbose:
                print("    模式：已点 %s（%s）" % (m["label"], m["field"]))
            return {"ok": True, "label": m["label"], "field": m["field"], "actor": m["actor"]}
        return {"ok": False, "error": "没有标签匹配 %r 的模式按钮" % label,
                "modes": [(x["field"], x["label"]) for x in modes]}

    def press_play(self, verbose: bool = True, cache: Optional[dict] = None) -> dict:
        """点"开始"—— **先确认它真的可点**（看内层 UButton），灰的就不点。

        ★ 2026-09-25 晚：一度怀疑"跳步"（直接调 sidebar 的 `..._8_onClicked`，
        没先过 wrapper 自己的 hover/pressed/released）是"点了没反应"的根因，改成了
        wrapper 4 跳版本——但用户随后拿**真实物理鼠标**去点同一个按钮，同样没反应。
        这就排除了"注入跳步"这个假设：问题在客户端自己卡住了，不是我们调用序列的
        锅。改回这个更简单的版本（该记录留着，省得以后又去查这条死路）。
        ★ 2026-09-28：改走 `_live_deck_sidebar()`，而且**只扫一次、直接复用同一个
          指针**去点——原来是先调 `start_enabled()`（它自己扫一遍拿到一个 sidebar）
          判断能不能点，再**独立重新扫一遍**去点击，两次扫描理论上可能扫到不同的
          实例（同屏多个 live 候选时）。现在两件事共用同一次解析结果，不会对着
          "判断能点的那个"和"真正点的那个"是两个不同对象。
        """
        side, pb, inner = self._live_deck_sidebar(cache=cache, verbose=verbose)
        if not side:
            return {"ok": False, "error": "没有 live 的 W_DeckSelectedSideBar_C"
                                          "（PlayButton 内层 OnClicked 无订阅者，可能是陈旧实例）"}
        st = {"play_button": hex(pb), "outer_enabled": self.widget_enabled(pb),
              "inner": hex(inner) if inner else None,
              "inner_enabled": self.widget_enabled(inner) if inner else None,
              "clickable": bool(self.widget_enabled(inner)) if inner else False}
        # ★ 2026-10-03（用户拍板）：默认**不拦**灰按钮；原判据是
        #   `if not st.get("clickable")`（灰的就不点），想临时恢复严格模式：
        #   设 `KARDS_PLAY_BLOCK_GRAY=1`。为什么默认关：这行只决定我们自己发不发
        #   这次点击 —— 不改按钮 enabled/视觉（界面照旧灰），也不碰服务端校验；
        #   开局是否真成立由 `gui.autoplay.verify_start()` 点击后只读判定。
        if play_gray_guard_on() and not st.get("clickable"):
            return {"ok": False, "start": st,
                    "error": "开始按钮是灰的（内层 UButton disabled），不点"}
        cls = self.uclass_of_instance(side)
        f = self.fn(cls, "BndEvt__WBP_NUI_DeckSelectedSideBar_playButton_"
                         "K2Node_ComponentBoundEvent_8_onClicked__DelegateSignature")
        self.call0(side, f)
        if verbose:
            print("    牌组页：开始按钮已点（点之前确认过内层 enabled=True）")
        self.settle(1.5, 6)           # 开始按钮 → 等开局界面推进 ⇒ 按帧等（同 deck_start）
        return {"ok": True, "start_before": st}

    def quick_start(self, mode_label: str = "TRAINING", deck_name: str = None,
                    deck_index: int = None, verbose: bool = True) -> dict:
        """牌组页三连（选模式→选牌组→点开始）。

        ★ 2026-09-25：`GObjects` 扫描本体已经搬进 Frida（`scan_classes()`），
          单次全扫从 4~10s 压到 1~2s——不再需要死抠"只扫一遍"，**分两次扫更稳**：
          实机撞见过牌组按钮列表在**切模式之前**还没建出来（`quick_start` 一开始就
          扫、扫到的还是切模式前那一屏，`list_decks()` 直接是空的）。改成切模式
          **之后**再扫一次牌组页（侧栏+牌组按钮+赛况数据），两次扫描加起来也就
          2~4s，比死磕"一次扫描"更不容易踩这类时序坑。
        """
        t0 = time.time()
        r1 = self.select_mode_by_label(mode_label, verbose=verbose)
        cache = self.deck_screen_scan()
        if verbose:
            print("    quick_start: 切模式后重新扫描完成（累计 %.2fs）" % (time.time() - t0))
        r2 = self.select_deck(name=deck_name, index=deck_index, verbose=verbose, cache=cache)
        r3 = self.press_play(verbose=verbose, cache=cache)
        ok = bool(r1.get("ok") and r2.get("ok") and r3.get("ok"))
        return {"ok": ok, "mode": r1, "deck": r2, "play": r3,
                "scan_seconds": time.time() - t0}

    # ------------------------------------------------------------ 自检
    def selftest(self) -> dict:
        """只解析、不动手：把所有要用的函数/偏移/实例都解析一遍并打印。"""
        rep = {"ok": True, "items": [], "notes": []}
        st = None
        try:
            from kardsmem import board as BA
            st = BA.open_source("mem").snapshot()
        except Exception as e:                               # noqa: BLE001
            rep["ok"] = False
            rep["items"].append(("snapshot", "FAIL", str(e)))
            return rep

        rep["items"].append(("对局", "in_match",
                             "cards=%d turn=%s our_turn=%s kredits=%s"
                             % (len(st.cards), st.turn, st.our_turn, st.kredits)))
        rep["items"].append(("my_side", "ok", self.my_side()))
        try:
            rep["items"].append(("我方后排枚举", "ok", self.our_back_enum()))
            rep["items"].append(("空槽位", "ok", self._free_slot()))
        except Exception as e:                               # noqa: BLE001
            rep["ok"] = False
            rep["items"].append(("落点枚举", "FAIL", str(e)))

        hud = self.hud_actor()
        rep["items"].append(("HUD 实例", "ok" if hud else "FAIL", hex(hud) if hud else "没找到"))
        if hud:
            hud_cls = self.uclass_of_instance(hud)
            btn = self.end_turn_button(hud)
            rep["items"].append(("EndTurnButton", "ok" if btn else "FAIL", hex(btn) if btn else "空"))
            btn_cls = self.uclass_of_instance(btn) if btn else 0
            if btn_cls:
                for nm in ("BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature"):
                    rep["items"].append(("按钮句柄", nm.split("_")[-1], hex(self.find_fn(btn_cls, nm))))
                rep["items"].append(("HUD 句柄", "click249",
                                     hex(self.find_fn(hud_cls, "BndEvt__EndTurnButton_K2Node_ComponentBoundEvent_249_OnButtonClickedEvent__DelegateSignature"))))

        hand = hand_card_actors(self.ks)
        rep["items"].append(("我方手牌 actor", "count=%d" % len(hand),
                             [(r.get("card_id"), r.get("name")) for r in hand]))
        if hand:
            cls = self.uclass_of_instance(hand[0]["actor"])
            for nm in ("OnActorMouseEnter", "OnActorMouseDown", "OnActorStartDrag",
                       "OnActorDragTick", "OnActorEndDrag"):
                f = self.find_fn(cls, nm)
                rep["items"].append(("手牌手势", nm, hex(f) if f else "缺失"))
                if not f:
                    rep["ok"] = False
            for nm, note in (("cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR),
                             ("LocationNumberUnderCursor", NOTE_OFF_LOCATION_NUMBER),
                             ("LocationUnderCursor", NOTE_OFF_LOCATION_ENUM),
                             ("RowUnderCursor", NOTE_OFF_ROW_UNDER_CURSOR)):
                rep["items"].append(("落点字段", nm, "0x%X" % self.off(cls, nm, note)))
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        rep["items"].append(("PC 悬停转发", "MouseHoverDispatch",
                             hex(self.find_fn(pc_cls, "MouseHoverDispatch"))))
        rep["items"].append(("ProcessEvent RVA", "0x%X" % B.RVA["UObject_ProcessEvent"],
                             "vtable idx 0x%X" % B.PROCESS_EVENT_IDX))
        rep["notes"] = list(self.notes)
        return rep


def _action_has_card(row: dict, card_id: int) -> bool:
    """动作流一条里有没有提到这个 cardID（我方条目是数字键、对手是具名键）。"""
    for v in (row.get("data") or []):
        name = (v.get("name") or "")
        txt = v.get("text")
        val = v.get("value")
        if name in ("cardID", "attackerCardID", "cardId") and val == card_id:
            return True
        if name in ("0", "1") and (txt == str(card_id) or val == card_id):
            return True
    return False


def main(argv=None) -> int:
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    cmd = argv[0] if argv else "selftest"
    inj = Injector()
    try:
        if cmd == "selftest":
            rep = inj.selftest()
            print("=== ops_inject selftest ===")
            for group, name, val in rep["items"]:
                print("  %-12s %-42s %s" % (group, name, val))
            if rep["notes"]:
                print("--- 备注 ---")
                for n in rep["notes"]:
                    print("  " + n)
            print("结论:", "OK" if rep["ok"] else "有 FAIL")
            return 0 if rep["ok"] else 1
        if cmd == "end":
            print(inj.end_of_turn())
            return 0
        if cmd == "play":
            print(inj.play_card(int(argv[1])))
            return 0
        # ---- 游戏自己的"粗判断"API（NN/前端预检用；只读、不改对局） ----
        if cmd == "canplay":                     # 打牌总闸（含指挥点/支援线/限制/指向）
            r = inj.game_can_play_card_from_hand(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "cando":                       # 这张牌这回合还能不能做事
            r = inj.game_can_card_do_anything(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "cani":                        # 我方还有没有事可做
            r = inj.game_can_i_do_anything(verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "precheck":                    # 总闸 + 下钻取理由
            print(inj.precheck_play(int(argv[1]), verbose=True))
            return 0
        if cmd == "canfront":                    # 上线判据（默认**不问**：问它要伪造拖拽态）
            # canfront <card_id> [--simulate-drag]   ← 诊断口，会写 PC->SelectedCard
            sim = "--simulate-drag" in argv[1:]
            print(inj.can_move_to(int(argv[1]), LOC_BOARD_FRONTLINE, verbose=True,
                                  simulate_drag=sim))
            return 0
        if cmd == "canattack":                   # 游戏自己的 CanAttack（self=CDO）
            print(inj.game_can_attack(int(argv[1]), int(argv[2]), verbose=True))
            return 0
        if cmd == "picklist":                    # 当前二选一/三选一候选（两类一起）
            for c in inj.pick_candidates():
                print("  [%s] index=%s %-22s label=%-14s actor=0x%X trig=0x%X(kid=%s)"
                      " needs_target=%s clickable=%s is_effect=%s card_id=%s"
                      % (c["kind"], c["index"], c["name"] or "-", c["label"] or "-",
                         c["actor"], c["trigger_actor"], c["trigger_id"],
                         c["needs_target"], c["clickable"], c.get("is_effect"),
                         c.get("card_id")))
            return 0
        if cmd == "pick":                        # pick <index> [trigger] [kind]
            kw = {}
            if len(argv) > 2:
                t = argv[2]
                kw["trigger"] = int(t, 0)
            if len(argv) > 3:
                kw["kind"] = argv[3]
            r = inj.pick_choice(int(argv[1]), verbose=True, **kw)
            print(r if not r.get("ok") else {k: v for k, v in r.items() if k != "candidates_after"})
            return 0 if r.get("ok") else 1
        if cmd == "mulligan":
            # 用法: mulligan [--discard 26,31] [--wait 300]
            discard = []
            wait = 0.0
            rest = argv[1:]
            for i, a in enumerate(rest):
                if a == "--discard" and i + 1 < len(rest):
                    discard = [int(x) for x in rest[i + 1].split(",") if x.strip()]
                if a == "--wait" and i + 1 < len(rest):
                    wait = float(rest[i + 1])
            r = inj.mulligan(discard=discard, wait=wait)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "pregame":
            print("PreGameState =", inj.pregame_state(),
                  " myMulliganDone =", inj.mulligan_done())
            print("marked:", [(r["card_id"], r["name"], r["marked"]) for r in inj.mulligan_marks()])
            return 0
        if cmd == "surrender":
            r = inj.surrender()
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "attack":
            print(inj.attack_card(int(argv[1]), int(argv[2])))
            return 0
        # ---- 指向语义分层的三个动词（2026-09-27；旧名见各自 docstring 的兼容别名） ----
        if cmd == "target":                      # target <spec>：目标规格 → card_id（含按卡名）
            print(inj.resolve_target(argv[1]))
            return 0
        if cmd == "event":                   # event <card_id> [--require-inactive]：指令/反制
            r = inj.play_card_event(int(argv[1]), verbose=True,
                                    require_inactive="--require-inactive" in argv[2:])
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "eventt":                  # eventt <card_id> <target>：指向性指令（一次成交）
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.play_card_event_with_target(int(argv[1]), rt["card_id"], verbose=True)
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "unit":                    # unit <card_id> [slot] [t=<target>]：单位（可带站位/目标）
            slot, tgt = None, None
            for a in argv[2:]:
                if a.startswith("t="):
                    rt = inj.resolve_target(a[2:])
                    if not rt.get("ok"):
                        print("目标解析失败:", rt)
                        return 1
                    tgt = rt["card_id"]
                else:
                    slot = int(a)
            if tgt is None:
                r = inj.play_card_unit(int(argv[1]), slot=slot, verbose=True)
            else:
                r = inj.play_card_unit_with_target(int(argv[1]), tgt, slot=slot, verbose=True)
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "unitt":                   # unitt <unit_card_id> <target> [slot]：单位两阶段指向
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.play_card_unit_with_target(int(argv[1]), rt["card_id"],
                                               slot=(int(argv[3]) if len(argv) > 3 else None),
                                               verbose=True)
            print({k: v for k, v in r.items() if k != "candidates"})
            return 0 if r.get("ok") else 1
        if cmd == "aimunit":                     # aimunit <playing_card_id> <target>：点选目标单位
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.select_unit_target(int(argv[1]), rt["card_id"], verbose=True)
            print({k: v for k, v in r.items() if k != "candidates"})
            return 0 if r.get("ok") else 1
        if cmd == "canact":                      # canact <card_id>：本回合能不能动（部署病）
            r = inj.can_act_now(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can_act") else 1
        if cmd == "forecast":                    # forecast [index] [kind]：一口气走完多层抉择链
            r = inj.pick_layers(int(argv[1]) if len(argv) > 1 else 0,
                                kind=(argv[2] if len(argv) > 2 else None), verbose=True)
            print({k: v for k, v in r.items() if k != "pick_state"})
            return 0 if r.get("ok") else 1
        if cmd == "choose":                      # choose <i> [kind]：选一张（一个动词）
            kw = {}
            if len(argv) > 2:
                kw["kind"] = argv[2]
            r = inj.choose_card(int(argv[1]) if len(argv) > 1 else 0, verbose=True, **kw)
            print({k: v for k, v in r.items() if k != "candidates_after"})
            return 0 if r.get("ok") else 1
        if cmd == "chooseone":                   # chooseone <i> <target>：抉择+指向（两步合一）
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.choose_one_with_target(int(argv[1]), rt["card_id"], verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "totals":                      # totals <card_id>：游戏本体的显示值（UI 同源）
            r = inj.card_totals(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "gaps":                        # gaps [back|front]：空隙序号 → 列号请求（只读对账）
            r = inj.probe_gap_numbers((argv[1] if len(argv) > 1 else "back"), verbose=True)
            print({k: v for k, v in r.items() if k != "gaps"})
            return 0
        if cmd == "gaptop":                      # gaptop <空隙序号> [back|front]：只算不写
            r = inj.gap_number(int(argv[1]), row=(argv[2] if len(argv) > 2 else "back"),
                               verbose=True)
            print(r)
            return 0
        if cmd == "front":                       # front <card_id> [slot]：上线/移动
            r = inj.move_to_front(int(argv[1]),
                                  slot=(int(argv[2]) if len(argv) > 2 else None),
                                  verbose=True)
            print({k: v for k, v in r.items()})
            return 0 if r.get("ok") else 1
        if cmd == "pickhand":                    # pickhand <hand_card_id>：选手牌当目标并确认
            r = inj.select_hand_target(int(argv[1]), verbose=True)
            print({k: v for k, v in r.items() if k != "legal"})
            return 0 if r.get("ok") else 1
        if cmd == "pending":                     # pending：现在在等什么（只读汇总）
            print(inj.pending_summary())
            return 0
        if cmd == "wait":                        # wait [seconds]：等我方回合
            ok = inj.wait_our_turn(limit=float(argv[1]) if len(argv) > 1 else 300.0)
            print("我方回合:", ok)
            return 0 if ok else 1
        if cmd == "mullmarks":                   # mullmarks：换牌界面每张牌的 shouldDiscard
            for r in inj.mulligan_marks():
                print("  ", r)
            return 0
        if cmd == "mullmark":                    # mullmark <card_id>：切换待替换标记
            r = inj.mulligan_mark(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "mullgo":                      # mullgo：确认换牌（真按钮四件套）
            r = inj.mulligan_confirm(verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "arrows":
            print([hex(a) for a in inj.arrow_actors()])
            return 0
        if cmd == "board":
            from kardsmem import board as BA
            st = BA.open_source("mem").snapshot()
            for c in st.cards:
                if c.location in ("back", "frontline", "hq"):
                    a = inj.board_actor_of(c.card_id)
                    print("  %-26s id=%-6s %-10s actor=%s" % (c.name, c.card_id, c.location,
                                                              hex(a) if a else "?"))
            return 0
        print("用法: selftest | end | play <card_id> | attack <attacker_id> <target_id> | arrows | board")
        print("      canplay <card_id>   游戏总闸 BP_Logic::CanPlayCardFromHand（bool，含指挥点/支援线/限制/指向）")
        print("      cando <card_id>     BP_Logic::CanCardDoAnything（这张牌还能不能做事）")
        print("      cani                BP_Logic::CanIDoAnything（我方还有没有事可做）")
        print("      precheck <card_id>  总闸 + 下钻取理由（NN/前端预检入口）")
        print("      canfront <card_id>  游戏自己的 CanMoveCardToLocation（能不能上线）")
        print("      canattack <a> <t>   游戏自己的 CanAttack（带 failReason）")
        print("      picklist            当前二选一/三选一候选（两类一起，带屏幕次序与选项文字）")
        print("      pick <i> [trigger] [kind]  点同一触发者分组内的第 i 个候选")
        print("      target <spec>       目标规格 → card_id（card_id / hq / front0 / back1 / guard0；不收卡名）")
        print("      event <card> [--require-inactive]   打出指令/反制（反制=切换，默认允许重复）")
        print("      eventt <card> <tgt> 指向性指令（松手即成交）")
        print("      unit <card> [slot] [t=<tgt>]        打出单位（slot = 支承线内**站位**）")
        print("      unitt <card> <tgt> [slot]           单位+指向（无合法目标 ⇒ 自动退化成 unit）")
        print("      canfront <card> [--simulate-drag]  上线判据；默认不问（问它要伪造拖拽态）")
        print("      canact <card>       本回合能不能动（部署病：enterPlayOnTurn vs turn，blitz 例外）")
        print("      front <card> [slot] 上线/移动（XActionMoveCardToLine）")
        print("      forecast [i] [kind] 一口气走完多层抉择链（预报/天气→2K/4K/6K）")
        print("      choose <i> [kind]   选一张（choose_one / choose_spawn / choose_draw；单击提交）")
        print("      totals <card_id>    游戏本体的显示值 getTotal*（与卡面 UI 同源，只读）")
        print("      pickhand <card>     选手牌当目标 + 点确认按钮")
        print("      pending             现在在等什么（抉择/手牌目标/板卡待点目标/箭头/队列）")
        print("      wait [sec]          等我方回合")
        print("      mullmarks / mullmark <card> / mullgo   换牌：读取标记 / 切换标记 / 确认")
        return 2
    finally:
        inj.close()


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
