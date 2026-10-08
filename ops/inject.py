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

# ★ P6/D1（2026-10-03）：本文件已拆成 `ops/` 下多个模块，这里只剩**兼容门面**：
#   `from ops.inject import Injector, <任何旧名字>` 全部照旧可用；方法按职责分布在各 mixin 里：
#     conn.ConnMixin / world.WorldMixin / gesture.GestureMixin / query.QueryMixin /
#     play.PlayMixin / choices.ChoiceMixin / flow.FlowMixin / cli.SelftestMixin（+ cli.main）
#   模块级常量与辅助在 consts.py / support.py。方法归属表见 docs/STRUCTURE.md 的 ops 一节。
#   ★ 步骤 3（2026-10-04）：L1 原语抽到 `primitives.py`（conn/world/gesture/play/flow/query
#     都改为调用它）；★ 步骤 4：需要**临时写字段**的诊断搬到 `diag.DiagMixin`，
#     只读查询 `query.QueryMixin` 不再具备写字段的能力（判据在 `tests/test_ops_rules.py`）。

import atexit                                                   # noqa: F401
import json                                                     # noqa: F401
import os                                                       # noqa: F401
import struct                                                   # noqa: F401
import time                                                     # noqa: F401
from typing import Optional                                     # noqa: F401

from kardsmem import attach, kismet, props                       # noqa: E402,F401
from kardsmem import build as B                                  # noqa: E402,F401
from kardsmem.objects import ObjectArray                         # noqa: E402,F401
from kardsmem.pick import hand_card_actors, player_controller    # noqa: E402,F401
from ops import load_js_template as _load_js_template            # noqa: E402,F401
from base import paths as _paths_oi                              # noqa: E402,F401

from ops.consts import (   # noqa: F401
    NOTE_OFF_CARD_UNDER_CURSOR, NOTE_OFF_LOCATION_NUMBER, NOTE_OFF_LOCATION_ENUM, NOTE_OFF_ROW_UNDER_CURSOR,
    NOTE_OFF_HUD_END_TURN_BTN, NOTE_OFF_ACTOR_CARDID, NOTE_OFF_ACTOR_SELF_BASECARD,
    NOTE_OFF_CARDOBJ_TARGET_OVERRIDE, NOTE_OFF_ARROW_OVER_CARD_ID, NOTE_OFF_ARROW_OVER_CARD,
    NOTE_OFF_PC_MOUSE_OVER_ACTOR, NOTE_OFF_PC_OLD_MOUSE_OVER_ACTOR, NOTE_OFF_PC_LEFT_BUTTON_DOWN,
    NOTE_OFF_PC_MOUSE_DOWN_ACTOR, NOTE_OFF_PC_DRAG_ACTOR, NOTE_OFF_PC_DRAG_STARTED, CURSOR_FIELD,
    NOTE_OFF_BOARD_SELECTING_HAND_TARGET, NOTE_OFF_BOARD_SELECT_HAND_WIDGET, NOTE_OFF_HTGT_WIDGET_CARD_ID,
    NOTE_OFF_HTGT_WIDGET_TARGET, NOTE_OFF_HTGT_WIDGET_CARD_OBJ, NOTE_OFF_ARROW_FINAL_LENGTH,
    NOTE_OFF_ARROW_FROM_CARD, NOTE_OFF_ARROW_HEAD_PLANE, NOTE_OFF_LOGIC_PLAYING_FROM_HAND,
    NOTE_OFF_LOGIC_PLAYING_LOC, NOTE_OFF_LOGIC_ONLINE_MATCH, NOTE_OFF_OM_SELECT_CARD_TO_DRAW,
    NOTE_OFF_UCLASS_CDO, NOTE_OFF_WORLD_LEVELS, NOTE_OFF_ULEVEL_ACTORS, NOTE_OFF_BOARD_BATTLE_HUD,
    NOTE_OFF_HUDCONTAINER_HUDPC, NOTE_OFF_CARD_TITLE, NOTE_OFF_HANDCARD_SHOULD_DISCARD,
    NOTE_OFF_LOGIC_PREGAMESTATE, NOTE_OFF_LOGIC_MY_MULLIGAN_DONE, NOTE_OFF_SETTINGS_SURRENDER_BTN,
    NOTE_OFF_DECK_SHOWING_STARTING_HAND, NOTE_OFF_DECK_FOR_ENEMY, NOTE_OFF_WIDGET_VISIBILITY,
    NOTE_OFF_EOM_CLICKTHROUGH, NOTE_OFF_EOM_STEP, NOTE_OFF_PLAYBAR_TRAINING, NOTE_OFF_SIDEBAR_PLAY_BTN,
    NOTE_OFF_DECKBTN_NAME, NOTE_OFF_DECKBTN_REASON, NOTE_OFF_DECKBTN_BUTTON, NOTE_OFF_TEXTBLOCK_TEXT,
    NOTE_OFF_SIDEBAR_RANK_TOGGLE, NOTE_OFF_WIDGET_ENABLED_BYTE, NOTE_OFF_WIDGET_ENABLED_MASK,
    NOTE_OFF_PLAYBAR_WORLD, NOTE_OFF_PLAYBAR_TOURNAMENT, NOTE_OFF_PLAYBAR_SKIRMISH, NOTE_OFF_PLAYBAR_TEXTAREA,
    NOTE_OFF_PLAYBAR_INNER_BTN, NOTE_OFF_UBUTTON_ONCLICKED, NOTE_OFF_MATCHDATA_CHOSEN_DECK_ID,
    NOTE_OFF_SPAWN_INDEX, NOTE_OFF_SPAWN_TRIGGER, NOTE_OFF_SPAWN_NAME, NOTE_OFF_SPAWN_SELECTED,
    NOTE_OFF_SPAWN_TRIGGER_ID, NOTE_OFF_SPAWN_OWNED, NOTE_OFF_SPAWN_IS_EFFECT, NOTE_OFF_ONE_INDEX,
    NOTE_OFF_ONE_NEEDS_TARGET, NOTE_OFF_ONE_IS_BEING_PLAYED, NOTE_OFF_ONE_SPAWN_CARD, NOTE_OFF_ONE_CARD,
    NOTE_OFF_ONE_MOTHER, NOTE_OFF_ONE_CLICKABLE, NOTE_OFF_CARDOBJ_CHOOSE_ONE_CARDS, PICK_STRUCT_SIZE,
    NEEDS_TARGET_REASONS, LOC_BOARD_HQLEFT, LOC_BOARD_HQRIGHT, LOC_BOARD_FRONTLINE, HUD_CLASS, MODULE,
    SETTLE_HOVER, SETTLE_AFTER_DISPATCH, SETTLE_AFTER_DOWN, SETTLE_AFTER_TICK, SETTLE_FLOOR,
    SETTLE_FLOOR_MINOR, _settle, SETTLE_HOVER, SETTLE_AFTER_DISPATCH, SETTLE_AFTER_DOWN, SETTLE_AFTER_TICK,
    SETTLE_HOVER_INSPECT, SETTLE_HOVER_PLAY, render_js,
)
from ops.support import (   # noqa: F401
    play_gray_guard_on, FRIDA_TMP, _frida_tmp_setup, sweep_frida_tmp, _CACHE_PATH, _load_persistent_cache,
    _save_persistent_cache, _action_has_card,
)
from ops.conn import ConnMixin                                    # noqa: E402
from ops.world import WorldMixin                                  # noqa: E402
from ops.gesture import GestureMixin                              # noqa: E402
from ops.query import QueryMixin                                  # noqa: E402
from ops.diag import DiagMixin                                    # noqa: E402
from ops.play import PlayMixin                                    # noqa: E402
from ops.choices import ChoiceMixin                               # noqa: E402
from ops.flow import FlowMixin                                    # noqa: E402
from ops.cli import SelftestMixin, main                           # noqa: E402


class Injector(ConnMixin, WorldMixin, GestureMixin, QueryMixin, DiagMixin, PlayMixin, ChoiceMixin,
               FlowMixin, SelftestMixin):
    """一条常驻的 Frida 连接 + 全部地址/偏移的运行时解析。"""


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
