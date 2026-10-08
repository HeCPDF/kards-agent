#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kardsmem/board.py —— 盘面快照：把游戏进程内存里的盘面读成归一化的 `BoardState`。

调用方只认 `BoardState`（卡、双方指挥点、回合、钩子注册表、帧率…），不关心内存布局：

    from kardsmem.board import open_source
    src = open_source()
    st  = src.snapshot()
    st.kredits[st.my_side], st.our_turn, len(st.board())

只读：`PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`。不依赖 reverse-data，
字段偏移走 UE 反射链现算（`kardsmem/props.py`），全局 RVA 见 `kardsmem/build.py`。

约定
====
- 坐标一律是游戏窗口 **客户区** 坐标（本仓库惯用 1280x720）。
- 座位用游戏自己的 `ESide`（1=left 先手 / 2=right 后手）；**没有 local / enemy**。本地是哪一边由 `mySide` 决定
  （`BoardState.my_side`，每局读一次），“我方”= `side == my_side`。
- 任何读不出来的字段一律 `None`，并把原因记进 `BoardState.unknown`；
  **不猜**。`complete` 为 False 表示至少有一个关键字段是 None。

只读保证（mem 后端）
====================
只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`。
没有注入、没有 WriteProcessMemory、没有远程线程、没有 hook。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .gamemodel import (other_side, BaseCardObject, ECardLocation, EFaction, ERarity, ESide, EType, GameState, enum_or_none)

# --------------------------------------------------------------------------
# 零、仓库根（只用于定位 config 之类的随包文件；不再向 sys.path 注入任何东西）
# --------------------------------------------------------------------------
AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # 仓库根


# --------------------------------------------------------------------------
# 一、归一化的盘面模型（调用方只认这些）
# --------------------------------------------------------------------------


# ECardLocationEnum。★ 实测修正：5/6 (Board_HQLeft/Right) 不是"只有 HQ"，
# 而是**我方/敌方的整个后排** —— HQ 与支援线单位都在里面，靠 locationNumber 分槽。
LOCATION_NAMES = {
    0: None, 1: "deck", 2: "deck", 3: "hand", 4: "hand",
    5: "back", 6: "back", 7: "frontline", 8: "discard", 9: "deck",
}
LOCATION_ENUM = {
    "NotAvailable": 0, "Deck_Left": 1, "Deck_Right": 2, "Hand_Left": 3,
    "Hand_Right": 4, "Board_HQLeft": 5, "Board_HQRight": 6,
    "Board_Frontline": 7, "Discard": 8, "Deck": 9,
}

# ETypeEnum
CARD_TYPES = {
    0: None, 1: "location", 2: "order", 3: "tank", 4: "fighter", 5: "bomber",
    6: "infantry", 7: "artillery", 8: "antiair", 9: "antitank",
    10: "tankdestroyer", 11: "gotcha", 12: "wildcard",
}

# ESideEnum 的**原始**取值。1=left / 2=right 是服务端视角的编号，
# **不等于**本地/敌方 —— 哪一边是本地由 ABP_Board_C::mySide 决定，每局可能不同。
SIDE_LEFT, SIDE_RIGHT = 1, 2

# ★ 哪一边是本地玩家 —— 纯用 GameState 的三个字段推，不猜偏移。
#
#   turn 是双方共用的计数，turn==1 属于 startingSide，之后交替：
#       该走的一方 = startingSide            (turn 为奇数)
#                  = 另一方                  (turn 为偶数)
#   再用 isLocalClientTurn 把"该走的一方"翻译成本地/敌方：
#       本地 = 该走的一方                     (isLocalClientTurn 真)
#            = 另一方                        (假)
#
# ⚠ 历史教训：曾经把 `ABP_Board_C + 0x3C8` 当成 mySide —— 它在两局里恰好都等于 2、
#   而那两局本地确实是 right，于是被误认为可用。人机镜像局里它仍是 2，但屏幕证明
#   本地是 side=1 ⇒ **那个字段是常量（多半是玩家数），不是 mySide**。别再用它。
OFF_BGS_STARTING_SIDE = 0x668
OFF_BGS_IS_LOCAL_TURN = 0x680
OFF_BGS_CURRENT_TURN = 0x684


def _other(side):
    return SIDE_RIGHT if side == SIDE_LEFT else SIDE_LEFT


def read_my_side(m, gamestate):
    """本地玩家的 ESideEnum（1/2）。读不出/还没开打返回 None —— **不猜**。

    只用 ABP_GameState_Battle_C 的 startingSide / isLocalClientTurn / currentTurn。
    三条独立证据交叉验证过（回合奇偶、玩家自述的先后手、某张已知归属的牌的 side 字段）。
    """
    if not gamestate:
        return None
    ss = m.u8(gamestate + OFF_BGS_STARTING_SIDE)
    lt = m.u8(gamestate + OFF_BGS_IS_LOCAL_TURN)
    turn = m.i32(gamestate + OFF_BGS_CURRENT_TURN)
    if ss not in (SIDE_LEFT, SIDE_RIGHT) or lt is None or not turn or turn < 1:
        return None
    to_move = ss if (turn % 2 == 1) else _other(ss)
    return to_move if lt else _other(to_move)


class Card:
    """一张牌：原版 `UBaseCardObject`（`.obj`）+ 少量我们加的元数据。

    原来的归一化字段（`side="local"`、`location="hand"` …）已删：座位用游戏自己的 `ESide`（`.side`），
    位置看 `.obj.Location`（`ECardLocation`）或 `.obj.InHand()` 一类谓词，**没有 local / enemy**。
    下面的只读属性是 `obj` 的同名别名（读不出为 None），给没迁完的调用点过渡用。
    """
    __slots__ = ("uid", "obj", "fname", "raw")

    def __init__(self, uid: str, obj: BaseCardObject, fname: Optional[str] = None, raw: Optional[dict] = None):
        self.uid, self.obj, self.fname, self.raw = uid, obj, fname, (raw if raw is not None else {})

    @property
    def location(self):
        # 故意抛 RuntimeError（不是 AttributeError）：`getattr(c, "location", None)` 不会把它吞成 None。
        raise RuntimeError("Card.location 已删：用 card.obj.Location（ECardLocation）或 obj.InHand()/InFrontline()/IsHQ()… 谓词")

    # ---- 原版字段的别名 ----
    @property
    def side(self) -> Optional[ESide]:
        return self.obj.side

    @property
    def slot(self): return self.obj.locationNumber

    @property
    def card_id(self): return self.obj.CardID

    @property
    def name(self): return self.obj.title

    @property
    def card_type(self) -> Optional[str]:
        return None if self.obj.Type is None else self.obj.Type.name

    @property
    def attack(self): return self.obj.attack

    @property
    def attack_buff(self): return self.obj.attackBuff

    @property
    def defense(self): return self.obj.defense

    @property
    def max_attack(self): return self.obj.maxAttack

    @property
    def max_defense(self): return self.obj.maxDefense

    @property
    def kredit_cost(self): return self.obj.getTotalKredits()

    @property
    def operation_cost(self): return self.obj.operationCost

    @property
    def operation_cost_buff(self): return self.obj.operationCostBuff

    @property
    def kredits_tax_as_enemy_target(self): return self.obj.KreditsTax_AsEnemyTarget

    @property
    def gotcha_activated(self): return self.obj.gotchaActivated

    @property
    def is_suppressed(self): return self.obj.isSuppressed

    @property
    def is_revealed(self): return self.obj.isRevealed

    @property
    def is_being_guarded(self): return self.obj.isBeingGuarded

    @property
    def under_enemy_control(self): return self.obj.underEnemyControl

    @property
    def needs_hand_target(self): return self.obj.selectTargetOnPlayedFromHand

    @property
    def enter_play_on_turn(self): return self.obj.enterPlayOnTurn

    @property
    def cipher(self): return self.obj.cipher

    @property
    def has_been_attacked_this_turn(self): return self.obj.hasBeenAttackedThisTurn

    @property
    def faction_enum(self): return None if self.obj.faction is None else int(self.obj.faction)

    @property
    def rarity_enum(self): return None if self.obj.rarity is None else int(self.obj.rarity)

    @property
    def card_set_enum(self): return self.obj.cardSet

    @property
    def target_uid(self) -> Optional[str]:
        t = self.obj.CurrentTarget
        return ("0x%X" % t) if t else None

    @property
    def can_act(self) -> Optional[bool]:
        """本回合还能不能行动：`attackLeft>0 且 本回合没攻击过`（**主动**旗标 `hasAttackedThisTurn@0x281`，
        别用被动的 `hasBeenAttackedThisTurn@0x280`）。"""
        o = self.obj
        if o.attackLeft is None or o.hasAttackedThisTurn is None:
            return None
        return bool(o.attackLeft > 0 and not o.hasAttackedThisTurn)

    @property
    def keywords(self) -> list:
        o = self.obj
        kw = [name for flag, name in _KEYWORD_FLAGS if getattr(o, flag)]
        if o.heavyArmor:
            kw.append("heavyarmor%d" % o.heavyArmor)
        return kw

    # ★ P3（2026-10-03）：这两个原来写成了**方法**，而 `engine.effectvm` 用
    #   `getattr(c, "total_attack")` 读"当前总量"喂 `cur_stats`（SetValue 折算要用）——
    #   拿到的是**绑定方法**，随即被 `isinstance(v, (int, float))` 过滤掉 ⇒
    #   `cur_stats` 里**从来没有** `attack` / `opcost` ⇒ 那两个 SetValue 分支一直在记
    #   "读不到目标当前值"的缺口、**效果被整个丢掉**（活死代码，离线测试喂假 cur_stats 所以没暴露）。
    #   改成 `@property`（原版取值就是 `getTotalAttack()` / `getTotalOperationCost()`）。
    @property
    def total_attack(self) -> Optional[int]:
        return self.obj.getTotalAttack()

    @property
    def total_operation_cost(self) -> Optional[int]:
        """行动费 = 基础 + buff（进程外求和）；要权威值用 `ops.inject.card_totals(card_id)`。"""
        return self.obj.getTotalOperationCost()

    @property
    def heavy_armor(self) -> Optional[int]:
        return self.obj.heavyArmor

    @property
    def heavy_armor_buff(self) -> Optional[int]:
        return self.obj.heavyArmorBuff

    @property
    def total_heavy_armor(self) -> Optional[int]:
        """原版 `getTotalHeavyArmor()` = `clamp(0, 3, heavyArmor + heavyArmorBuff)`。

        ★ 2026-10-04：**以前没有这两个 property**，而 `engine.effectvm._fill_cur_stats`
        读的正是 `getattr(card, "total_heavy_armor")` / `("armor_buff", "heavy_armor_buff")`
        —— `Card` 上取不到、快照 `raw` 里也没有（raw 只带 `heavy_armor` 一个）⇒
        `cur_stats` 里**从来没有** `armor`/`armor_buff` ⇒ **重甲 SetValue 在实机路径上记
        "读不到目标当前值"的缺口、效果被整个丢掉**。R12 的测试之所以是绿的，是因为它
        往 `raw` 里**自己塞了那两个键**（fixture 与实机形状不符）—— 与 2026-10-03 那次
        `total_attack` 写成方法、离线测试喂假 `cur_stats` 是**同一类**错误（弯路 #11/#12）。
        夹取口径：IDA `getTotalHeavyArmor` 与 `kardsmem/cards.py:815` 都是 `[0,3]`。
        """
        a = self.obj.heavyArmor
        if a is None:
            return None
        v = int(a) + int(self.obj.heavyArmorBuff or 0)
        return 0 if v < 0 else (3 if v > 3 else v)

    def __repr__(self) -> str:
        o = self.obj
        return "Card(%s id=%s %s side=%s loc=%s)" % (self.uid, o.CardID, o.title, o.side, o.Location)


# 关键词旗标 → 名字（`BaseCardObject.has*` 字段名）
_KEYWORD_FLAGS = [("hasGuard", "guard"), ("hasShock", "shock"), ("hasCovert", "covert"), ("hasBlitz", "blitz"),
                  ("hasAmbush", "ambush"), ("hasSmokescreen", "smokescreen"), ("hasMobilize", "mobilize"),
                  ("hasAlpine", "alpine"), ("hasFury", "fury"), ("hasDeployment", "deployment"),
                  ("hasDestruction", "destruction"), ("hasScrying", "scrying"), ("hasPincer", "pincer"),
                  ("hasSalvage", "salvage")]


@dataclass
class BoardState:
    source: str
    taken_at: float = field(default_factory=time.time)
    complete: bool = True
    unknown: list = field(default_factory=list)       # 这个后端应该有、但这次没读到
    unsupported: list = field(default_factory=list)   # 这个后端按设计就不提供

    turn: Optional[int] = None
    our_turn: Optional[bool] = None
    action_process: Optional[bool] = None      # ABP_GameState_Battle_C@0x531：动作（含 StartTurnBySide）处理中
    my_side: Optional[ESide] = None     # 本地玩家的座位（游戏的 mySide，1=left / 2=right）；None = 读不出
    kredits: dict = field(default_factory=lambda: {ESide.left: None, ESide.right: None})   # 键 = ESide
    slots: dict = field(default_factory=lambda: {ESide.left: None, ESide.right: None})   # 键 = ESide
    slots_lost: dict = field(default_factory=lambda: {ESide.left: None, ESide.right: None})   # 键 = ESide
    fatigue: dict = field(default_factory=lambda: {ESide.left: None, ESide.right: None})   # 键 = ESide
    frontline_owner: Optional[ESide] = None
    weather_played_this_turn: Optional[bool] = None
    operation_kredits_spent: Optional[int] = None
    max_possible_kredits: Optional[int] = None   # int32@GS+0x338（getMaxPossibleKredits 的真值）
    # 游戏自己的"哪些牌挂了哪个触发"注册表 `CardFunctionTriggers`（GS+0x588）：{触发号: [card_id…]}（TSet 元素顺序）。
    # ★ 脚本中途接手时必须靠它知道**此刻生效的钩子**（含不在场的：弃牌堆/牌库里的天气牌、已打出的指令），
    #   不能靠"从头看到了什么"推断。None = 读不出（不等于"没有钩子"）。
    registry: Optional[dict] = None
    frontline_limiters: Optional[list] = None   # GS+0x3A8 `TSet<int32> FrontlineLimiters`；非空 ⇒ `IsFrontlineLimited()`（前线只容 2 张）
    match_finished: Optional[bool] = None
    hq: dict = field(default_factory=lambda: {ESide.left: None, ESide.right: None})   # 键 = ESide
    cards: list = field(default_factory=list)
    # 游戏原版结构（ABP_GameState_Battle_C，字段名 / 键类型照 SDK；见 kardsmem/gamemodel.py）。上面那些是它的派生视图。
    game: Optional[GameState] = None

    # ---- 便捷视图（座位用 ESide；不传 = 我方）----
    @property
    def my_side_raw(self) -> Optional[int]:
        """兼容别名：`my_side` 的 int 值（1/2）。新代码直接用 `my_side`（`ESide` 是 IntEnum，二者可互换比较）。"""
        return None if self.my_side is None else int(self.my_side)

    @property
    def other_side(self) -> Optional[ESide]:
        return None if self.my_side is None else other_side(self.my_side)

    def seat(self, side: Optional[ESide] = None) -> ESide:
        side = self.my_side if side is None else side
        if side is None:
            raise ValueError("本地座位读不出（mySide 为空）：不能按『我方』取牌")
        return side

    def hand(self, side: Optional[ESide] = None) -> list:
        side = self.seat(side)
        return [c for c in self.cards if c.obj.side == side and c.obj.InHand()]

    def board(self, side: Optional[ESide] = None) -> list:
        """前线（`Board_Frontline=7`）上的单位（双方共用一条前线，按 side 过滤）。"""
        side = self.seat(side)
        return [c for c in self.cards if c.obj.side == side and c.obj.InFrontline()]

    def support(self, side: Optional[ESide] = None) -> list:
        """支援线（`Board_HQLeft/Right`）里除 HQ 以外的单位。"""
        side = self.seat(side)
        return [c for c in self.cards if c.obj.side == side and c.obj.InSupportLine() and not c.obj.IsHQ()]

    def field_units(self, side: Optional[ESide] = None) -> list:
        """真正在场的单位：前线 + 支援线（不含 HQ），排除弃牌堆/牌库。"""
        side = self.seat(side)
        return [c for c in self.cards if c.obj.side == side and c.obj.IsFieldUnit()]

    def discard(self, side: Optional[ESide] = None) -> list:
        side = self.seat(side)
        return [c for c in self.cards if c.obj.side == side and c.obj.InDiscard()]

    def by_uid(self, uid: str) -> Optional[Card]:
        for c in self.cards:
            if c.uid == uid:
                return c
        return None

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("cards", "game", "hq")}
        d["cards"] = [{"uid": c.uid, **{k: (int(v) if isinstance(v, ESide) else v)
                                         for k, v in c.obj.__dict__.items() if k != "raw"}} for c in self.cards]
        d["hq"] = {int(k): (v.uid if v is not None else None) for k, v in self.hq.items()}
        return d


class SourceUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 二、后端基类
# --------------------------------------------------------------------------

class BoardSource:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def snapshot(self) -> BoardState:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# 三、mem 后端：只读进程内存
# --------------------------------------------------------------------------
#
# 构建指纹/RVA：★ 2026-10-03 P7 起**不在这里维护**。唯一入口是 `kardsmem.build`
# （数据 = `build_tables.json` 种子表；选择 = 用户缓存 → 种子复验 → 运行时扫描，见 `build.resolve`）。
# 本模块只取用：`MEM_BUILD`（身份，信息性）与 `RVA_*`（★ P7-S3b：调用期读，仅供自检合成目标和旧工具）；
# 运行时读 GWorld 一律走 `_build.RVA[...]`（共享单例，解析成功后原地更新）。
# import 方向：`board → build`（build 不在顶层 import board/proc，所以没有环；
# build 需要的进程枚举在叶子模块 `version.py`）。`_find_pid`/`_module_of` 仍在本模块，`proc` 引用它们。
from . import build as _build

_REF = _build.BUILDS.get(_build.CURRENT) or {}
MEM_BUILD = {
    "module": "kards-Win64-Shipping.exe",
    "image_size": _build.RVA_IMAGE_SIZE or _REF.get("image_size"),
    "exe_size": _REF.get("exe_size"),
    "md5": _REF.get("md5"),
}

# ★ P7-S3b：这里原来是 import 期快照（`RVA_GWORLD = _build.RVA[...]`）。RVA 走解析链后
#   可能本进程运行期间才确定 ⇒ 改成模块 __getattr__ 调用期读（同 `names.py`；PEP 562）。
#   模块内部想用值时**直接写 `_build.RVA[...]`** —— 裸全局名查找不会走 __getattr__。
_BOARD_RVA_ALIASES = {"RVA_GWORLD": "GWorld", "RVA_GOBJECTS": "GObjects", "RVA_GNAMES": "GNames_decoy"}


def __getattr__(name: str):
    """供自检/旧工具的只读别名：`board.RVA_GWORLD` = 当前生效的 `build.RVA["GWorld"]`。"""
    key = _BOARD_RVA_ALIASES.get(name)
    if key is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    return _build.RVA[key]

OFF_UOBJECT_FLAGS = 0x08
OFF_UOBJECT_CLASS = 0x10
OFF_UCLASS_PROPSIZE = 0x58
OFF_UCLASS_SUPER = 0x40
FLAG_RF_CDO = 0x10

OFF_UWORLD_GAMESTATE = 0x1B0
SIZE_UWORLD = 2536
SIZE_AKARDS_GAMESTATE = 912
SIZE_BP_GAMESTATE_BATTLE = 1960
SIZE_BASECARD = 1656

# AkardsGameState：key + 4 个 20 字节侧值结构体，必须**一次性原子读**
GS_SIDE_BLOCK_OFF, GS_SIDE_BLOCK_LEN = 0x330, 0x60
OFF_AGS_KEY = 0x334
AGS_SIDES = {                      # (种类, 原始 left/right) → 偏移
    ("kredit", "left"): 0x33C, ("kredit", "right"): 0x350,
    ("slot", "left"): 0x364, ("slot", "right"): 0x378,
}

# ABP_GameState_Battle_C
BGS_U8 = {
    "frontline_owner": 0x3A0, "has_captured_frontline": 0x4C0,
    "action_process": 0x531, "stop_further_actions": 0x600, "stop_attack": 0x601,
    "starting_side": 0x668, "left_ally_faction": 0x669, "right_ally_faction": 0x66A,
    "left_main_faction": 0x66B, "right_main_faction": 0x66C,
    "is_local_client_turn": 0x680, "match_finished": 0x688,
    "unit_destroyed_this_turn": 0x689, "weather_played_this_turn": 0x750,
}
BGS_I32 = {
    "hq_damaged_turn_left": 0x4B0, "hq_damaged_turn_right": 0x4B4,
    "slots_lost_left": 0x4B8, "slots_lost_right": 0x4BC,
    "fatigue_right": 0x5D8, "fatigue_left": 0x5DC,
    "hq_card_id_left": 0x5E0, "hq_card_id_right": 0x5F0,
    "current_turn": 0x684, "operation_kredits_spent": 0x754,
    # ★ 2026-10-02（IDA 0x144B3C010 / 用户）：`getMaxPossibleKredits` 的真值 = int32@GS+0x338，
    #   **不是槽数**（旧实现把槽数当上限）。effectvm / cardnatives 都从这里取。
    "max_possible_kredits": 0x338,
}
BGS_PTR = {"hq_left": 0x5E8, "hq_right": 0x5F8}
OFF_ALLCARDSINBATTLE = 0x538
OFF_FRONTLINE_LIMITERS = 0x3A8          # TSet<int32>（BP_GameState_Battle::IsFrontlineLimited = 集合非空）
OFF_CARD_FUNCTION_TRIGGERS = 0x588     # TMap<ERegisteredCardFunction, FintegerSetStruct>
TARRAY_NUM_OFF = 0x08
# AllCardsInBattle = TMap<int32, UBaseCardObject*> = TSet<TPair<int32, UBaseCardObject*>>。
# UE 的 TSet 元素是 { TPair Value; int32 HashNextId; int32 HashIndex } = 16+4+4 = 24 字节；
# TPair 内部 Key 在 +0、指针在 +8（+4 那 4 字节是对齐 padding）。
TMAP_ELEM_SIZE = 24
TMAP_VALUE_OFF = 0x08

# UBaseCardObject：明文身份字段 + 5 条加密记录
CARD_KEY_OFF = 0x55C
CARD_TAMPER_FLAG_OFF = 0x564
CARD_RECORDS = {"attack": 0x568, "attackBuff": 0x57C, "kredit": 0x590,
                "kreditBuff": 0x5A4, "defense": 0x5B8}
CARD_REC_LO, CARD_REC_HI = 0x568, 0x5E0
CARD_U8 = {
    "type_enum": 0x68, "is_suppressed": 0x33A, "is_revealed": 0x33B,
    "location_enum": 0x275, "side_enum": 0x276, "needs_hand_target": 0x11D,
    "has_deployment": 0x1D8, "has_blitz": 0x1DA, "has_ambush": 0x1DB,
    "has_smokescreen": 0x1E4, "has_mobilize": 0x1E5, "has_alpine": 0x1E6,
    "has_fury": 0x1E7, "has_guard": 0x1E8, "has_shock": 0x1E9, "has_covert": 0x1EA,
    "has_attacked_this_turn": 0x281, "has_ever_attacked": 0x274,
    # ★★ 2026-09-27 用户纠正：**`hasAttackedThisTurn` 与 `hasBeenAttackedThisTurn` 不是一回事**
    #   （"被动和主动的区别"）—— 这俩只差一个 `Been`，语义正好相反，读/写结论时必须带主语：
    #     * `hasAttackedThisTurn  @0x281` = **这张单位本回合攻击过**（主动）⇒ 判"还能不能再攻击"用它
    #     * `hasBeenAttackedThisTurn @0x280` = **这张单位本回合被攻击过**（被动）⇒ 判
    #       "伏击（每回合首次被攻击才触发）这回合还用不用得上"用它
    #   （SDK：`kards_classes.hpp` 的 `hasBeenAttackedThisTurn // 0x0280` /
    #     `hasAttackedThisTurn // 0x0281`，相邻两行。）
    # ★ 2026-09-23 补：先前漏掉的一批。起因是把「被守护」当成要按相邻关系**推算**的量，
    #   而 `kards_classes.hpp` 里就摆着 `bool isBeingGuarded; // 0x0288`
    #   （1.57.26586 / 1.58.27125 / 1.60.27292 三个构建同偏移）。
    #   顺手把 UBaseCardObject 的 bool 全扫了一遍，把在用的都收进来。
    "is_being_guarded": 0x288,          # ★ 被守护（GiveGuard/RemoveGuard 带 instigatorID
                                        #   ⇒ 是带来源的状态，**不能按相邻关系推算**）
    # ★ 2026-10-04（P3 ⑦）：`isImmune @0x289`（SDK `kards_classes.hpp:2514`，正好接在 0x288 后面）。
    #   原版伏击的**必要条件**之一：`CalculateDamageDealt` `:14877-14887` 里
    #   `dealer.getHasImmune()` 为真 ⇒ **不走伏击**（免疫的伤害方打伏击单位时照常打出伤害）。
    "is_immune": 0x289,
    "under_enemy_control": 0x2A0,       # 归属 != 控制方（心控），见 §11.1 F3b
    "has_been_attacked_this_turn": 0x280,
    "has_destruction": 0x1D9, "has_scrying": 0x200,
    "has_pincer": 0x258, "has_salvage": 0x262,
    "is_gold_card": 0x30C, "is_salvaged": 0x338, "card_seen": 0x260,
    # ★ 2026-09-27 补（用户要的 board/inspect 字段；**都是运行时字段**，不是 CDO）：
    #   `faction @0x7C` / `rarity @0x11C` / `cardSet @0x130`（`kards_classes.hpp` 的
    #   `UBaseCardObject` 属性块，与 `type @0x68` 同一段）—— 存**枚举原值**，
    #   中文名由 `agent/view.py::FACTION_ZH` 之类映射（board_api 不做本地化）。
    "faction_enum": 0x7C, "rarity_enum": 0x11C, "card_set_enum": 0x130,
}
CARD_I32 = {
    "card_id": 0x264, "attack_plain": 0x6C, "attack_buff_plain": 0x70,
    "defense_plain": 0x74,
    # ★ 2026-10-04（P3 ⑥）：`range @0x78` —— SDK `kards_classes.hpp:2439`
    #   （`int32 range; // 0x0078`，UBaseCardObject 属性块，正好接在 `defense @0x74` 后面）。
    #   原版 `cardsCheckFunctions::CanAttack`（`:185-204`）用它判 `not_enough_range`：
    #   **攻击者不在前线 ∧ 目标不在前线 ∧ range < 2 ⇒ 打不了**。
    #   我们以前没读这个字段，sim 里用"兵种百科"（步兵/坦克只能打前线、空军/炮兵哪都能打）近似
    #   —— 那只在"兵种默认 range"下碰巧等价（实机上 range 才是权威）。
    "range": 0x78, "kredits_plain": 0x80, "kredits_buff_plain": 0x84,
    "kredits_tax": 0x88,          # KreditsTax_AsEnemyTarget：被敌方指向时额外加费
    "operation_cost": 0xB0, "heavy_armor": 0x1DC, "heavy_armor_buff": 0x1E0,
    # ★ 2026-09-27 补：`operationCostBuff @0xB4`。**以前漏了它** —— 被减行动费的卡
    #   （`operation_cost` 显示会偏低/偏高）看着"没被贴效果"。UI 显示的是
    #   `UBaseCardObject::getTotalOperationCost()`，那条走注入侧（`ops_inject.card_totals`）。
    "operation_cost_buff": 0xB4,
    "movement_left": 0x268, "attack_left": 0x26C, "location_number": 0x278,
    # ★ 2026-09-21 实机：游戏会拦"部署当回合的行动"（提示原文
    #   「单位无法在部署的回合中移动或攻击。」），而 attack_left/movement_left
    #   对刚部署的单位仍然读到 1 —— 所以真正的判据是 enterPlayOnTurn 和当前回合数比较。
    "enter_play_turn": 0x270,
    # ★ 2026-10-04 补：**压制（pin）** —— `IsPinned = pinnedTurns > 0`（IDA 0x144AFCD30；
    #   同一字段在 `kardsmem/cards.py:892` 早就在读，只是**没进快照**）。
    #   为什么必须进快照：`ops/play.py:792` 的移动动词用**只读** `is_pinned` 挡住被压制的单位
    #   （实机 2026-10-04 一局里 2 次 `move_up` 被拒："6 被压制（pin），不能移动"），
    #   而规则侧**生成候选时不查它** ⇒ 白提一个注定被拒的动作。
    #   §7.6f「判据负责挑、游戏负责判」：这里补的是"挑"，不改变"最终由游戏裁决"。
    "pinned_turns": 0x27C,
    "max_attack": 0x2A4, "max_defense": 0x2A8, "choose_one_index": 0x310,
    # ★ 2026-10-02：情报值（0..9）。导出文件里字段名被压缩显示成 `ln`，SDK 里是 `cipher`。
    "cipher": 0x25C,
    # ★ 2026-09-26 补：`UBaseCardObject::gotchaActivated`（= 反制在手里"已激活"的次序编号，
    #   0 = 没激活）。来自 `kards_classes.hpp`（1.60.27292：`// 0x0308`）。
    #   为什么必须读它：`BP_HandCard::ToggleGotcha` 是**开关** —— 已经激活的反制再 play 一次
    #   会**取消激活**（实机踩过）。有它才能"防重复激活"。
    "gotcha_activated": 0x308,
}
CARD_PTR = {"current_target": 0x2B0, "target_override": 0x548}
OFF_CARD_TITLE = 0x58          # FText title —— 牌名的正解（FText 不是 FName，不受名字池混淆影响）


def _fstring(m: "_Mem", a: int, maxn: int = 300):
    """读一个 FString{a, num@+8} 为 str（内存里是明文 UTF-16）。"""
    p = m.ptr(a)
    n = m.i32(a + 8)
    if not p or not n or n <= 0 or n > maxn:
        return None
    b = m.read(p, n * 2)
    if not b:
        return None
    try:
        s = b.decode("utf-16-le", "replace")
    except Exception:
        return None
    s = s.rstrip("\x00")
    return s or None


def _read_card_name(m: "_Mem", card: int):
    """从 `card+0x58` 的 FText 解出牌名。

    ★★ 2026-09-21 更正：这个 build 的 **FNamePool 既不混淆也不加密**，
    之前"全库 0 命中"是因为拿 UE4 的头格式（`0E 00 "UObject"`）去扫 UE5 的池；
    真池在 **RVA `0x0911B9C0`**（`GNames=0x090E2E28` 只是 Dumper-7 的 null 兜底常量）。
    实机已解出 `card_unit_104th_infantry_regiment` / `card_unit_p40_warhawk` 这类资产名，
    见 `kards-agent/kardsmem/names.py` 与 `python -m kardsmem names`。

    本函数仍走 **FText 路线**（更快、且与名字池解耦）：
    `card+0x58 -> FTextData -> +0x20` 的本地化字符串是普通 FString（明文 UTF-16）。
    实测：id9='106th INFANTRY REGIMENT'、id35='EXPANSION'、id21='TYPE 93'、
    id32='TYPE 92 Jyu-Shokosha' —— 是对应中文牌名的英文源串。
    """
    td = m.ptr(card + OFF_CARD_TITLE)
    if not td:
        return None
    for off in (0x20, 0x18, 0x28, 0x30, 0x38, 0x10, 0x40):
        s = _fstring(m, td + off)
        if s:
            return s
    return None

_KEYWORDS = [("has_guard", "guard"), ("has_shock", "shock"), ("has_covert", "covert"),
             ("has_blitz", "blitz"), ("has_ambush", "ambush"),
             ("has_smokescreen", "smokescreen"), ("has_mobilize", "mobilize"),
             ("has_alpine", "alpine"), ("has_fury", "fury"),
             ("has_deployment", "deployment"),
             # 2026-09-23 补齐（先前这张表少了四个关键词位）
             ("has_destruction", "destruction"), ("has_scrying", "scrying"),
             ("has_pincer", "pincer"), ("has_salvage", "salvage")]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x2, 0x8, 0x10
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF
ALLOC_ALIGN = 0x10000   # 64 KiB 对齐的指针一定不是 UObject，用来粗筛

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE), ("szModule", wt.WCHAR * 256),
                ("szExePath", wt.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]


def _name_matches(nm: str, want: str, stem: str) -> bool:
    """`nm` 是不是 `want`（或它的 `.patched.exe` 变体）。

    ★ 2026-09-28：`tools/patch_exe.py` 故意**不改**正名文件、另存
    `<stem>.patched.exe`（见 `game-installs/README.md` §5 的命名纪律）——
    这份产物拿去跑，进程主模块名就真的是 `kards-Win64-Shipping.patched.exe`，
    跟这里精确匹配的 `kards-Win64-Shipping.exe` 对不上，导致私服补丁版
    连不上任何 kards-agent 工具（读侧和 `ops_inject` 共用这两个函数）。
    只换了两个等长字面量，`SizeOfImage` 没变 ⇒ 偏移照样有效，所以放行
    `<stem>.patched.exe` 是同一条"SizeOfImage 决定有效性"策略的延伸，
    不是新开的口子。
    """
    if nm in (want, stem):
        return True
    patched = stem + ".patched.exe"
    return nm == patched or nm == patched[:-4]


def _find_pid(image_name: str) -> Optional[int]:
    want = image_name.lower()
    stem = want[:-4] if want.endswith(".exe") else want
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _k32.Process32First(snap, ctypes.byref(e))
        while ok:
            nm = e.szExeFile.decode("mbcs", "replace").lower()
            if _name_matches(nm, want, stem):
                return e.th32ProcessID
            ok = _k32.Process32Next(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return None


def _module_of(pid: int, image_name: str):
    want = image_name.lower()
    stem = want[:-4] if want.endswith(".exe") else want
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        m = _MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(_MODULEENTRY32W)
        ok = _k32.Module32FirstW(snap, ctypes.byref(m))
        while ok:
            if _name_matches(m.szModule.lower(), want, stem):
                return (m.modBaseAddr or 0, m.modBaseSize, m.szExePath)
            ok = _k32.Module32NextW(snap, ctypes.byref(m))
    finally:
        _k32.CloseHandle(snap)
    return None


class _Mem:
    """极薄的只读读内存封装。任何读失败返回 None，绝不抛。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise SourceUnavailable("OpenProcess(%d) 失败: err=%d" % (pid, ctypes.get_last_error()))

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

    def read(self, addr: int, size: int) -> Optional[bytes]:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not _k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def u8(self, a):
        d = self.read(a, 1)
        return d[0] if d else None

    def i32(self, a):
        d = self.read(a, 4)
        return struct.unpack("<i", d)[0] if d else None

    def u32(self, a):
        d = self.read(a, 4)
        return struct.unpack("<I", d)[0] if d else None

    def ptr(self, a):
        d = self.read(a, 8)
        if not d:
            return None
        v = struct.unpack("<Q", d)[0]
        return v if PTR_MIN <= v <= PTR_MAX else None

    def blob(self, a, n):
        return self.read(a, n)

    # `kardsmem.containers` 读 TMap/TSet 要的两个方法（`proc.MemRO` 里也有同名实现）
    def read_exact(self, a, n):
        d = self.read(a, n) if a and n > 0 else None
        return d if d is not None and len(d) == n else None

    def ptr_or_zero(self, a):
        return self.ptr(a) or 0


def _propsize_hits(m: _Mem, uclass: Optional[int], wanted, lo=0x30, hi=0xC0):
    """按值找 UStruct::PropertiesSize —— 无名字依赖的类身份判据。"""
    out = []
    if not uclass:
        return out
    for off in range(lo, hi, 4):
        v = m.u32(uclass + off)
        if v in wanted:
            out.append((off, v))
    return out


def _derives_from(m: _Mem, uclass: Optional[int], size: int, cache: dict) -> bool:
    """沿 SuperStruct 链找 PropertiesSize == size（这样 BP 子类也能认出来）。"""
    if uclass in cache:
        return cache[uclass]
    ok = False
    cur = uclass
    for _ in range(16):
        if not cur:
            break
        if _propsize_hits(m, cur, (size,)):
            ok = True
            break
        nxt = m.ptr(cur + OFF_UCLASS_SUPER)
        if not nxt or nxt == cur:
            break
        cur = nxt
    cache[uclass] = ok
    return ok


def _decrypt(key, X, Y, enc):
    """value = ((key ^ enc) - Y) / X ；X==0 表示这条记录无效（游戏会返回 -100/100）。"""
    if key is None or X is None or enc is None or Y is None:
        return None
    if X == 0:
        return None
    num = (enc ^ key) - Y
    if num % X:
        return None          # 不是整除 = 读到写入中途，判为不可信
    return num // X


class MemoryBoardSource(BoardSource):
    """只读进程内存。无注入、无写入。"""

    name = "mem"

    def __init__(self, pid: Optional[int] = None, validate_build: bool = True,
                 base: Optional[int] = None):
        self.pid = pid
        self.validate_build = validate_build
        self.base_override = base
        self._m = None
        self.info = {}
        # side 映射的默认值；每次 snapshot() 会按 ABP_Board_C::mySide 重建
        self._ml = None          # 懒建的 kardsmem MatchLog（Logic.mySide 兜底用）
        self._ml_tried = False

    def _logic_matchlog(self):
        """懒建一次 `kardsmem.MatchLog`，复用它的 `my_side()`（换牌期间也能读，
        见 `kardsmem/matchlog.py` 2026-09-24 的说明）当 `read_my_side()` 的兜底。

        ★ 用户 2026-09-24 明确授权：`board_api` 不必再守着"不依赖 kardsmem"
        这条旧分层——`Logic.mySide` 比这个模块自己的回合奇偶反推更准，
        应该直接用，不必假装两层互不知道对方存在。
        只建一次（`MatchLog.locate()` 首次要扫一遍 GObjects，~4s，这个代价
        只在 `read_my_side()` 的快速路径失败时才付一次），之后的调用复用同一个
        实例（`my_side()` 自己也缓存结果）。kardsmem 不可用/找不到对局对象
        都安静返回 `None`，不炸主流程。
        """
        if self._ml_tried:
            return self._ml
        self._ml_tried = True
        try:
            from kardsmem import attach as _kattach
            from kardsmem.matchlog import MatchLog as _MatchLog
            ks = _kattach(pid=self.info.get("pid") or self.pid, require_build=False)
            self._ml = _MatchLog(ks)
        except Exception:                        # noqa: BLE001
            self._ml = None
        return self._ml

    # -- 可用性 ----------------------------------------------------------
    def available(self) -> bool:
        try:
            self._attach()
            return True
        except SourceUnavailable:
            return False

    def _attach(self):
        pid = self.pid or _find_pid(MEM_BUILD["module"])
        if not pid:
            raise SourceUnavailable("%s 没在跑" % MEM_BUILD["module"])
        if self.base_override is not None:
            # 合成目标（自检）用：直接给 base，跳过模块查找与构建校验
            self.base = self.base_override
            self.info = {"pid": pid, "base": self.base_override, "synthetic": True}
            if self._m is None:
                self._m = _Mem(pid)
            return
        mod = _module_of(pid, MEM_BUILD["module"])
        if not mod:
            raise SourceUnavailable("在 pid %d 里找不到模块" % pid)
        base, size, path = mod
        self.info = {"pid": pid, "base": base, "image_size": size, "path": path}
        if self.validate_build:
            # ★ 2026-09-28：放行条件只有一条 —— SizeOfImage 与偏移表目标相同，
            # 它直接决定 RVA 有没有效。md5/exe_size 只记录"这份副本的字节有没有
            # 被本地改动过"（比如 patch_exe.py 换了两个等长 NUL 补齐的 URL 字面量），
            # **不作否决**：本地改动只碰 .rdata 常量，不移动节、不改代码，所有 RVA
            # 照样有效。这条策略跟 kardsmem/proc.py::Session.attach() 早就是这么定的
            # （见 kardsmem/build.py::validate() 的同一句话），board_api 这份 _attach()
            # 一直没跟上，之前会在私服补丁版上误报"不是同一份构建"而拒绝读数。
            want_size = _build.RVA_IMAGE_SIZE or MEM_BUILD["image_size"]     # 当前生效的 RVA 是为哪个镜像大小解出来的
            if want_size and size != want_size:
                raise SourceUnavailable(
                    "模块 SizeOfImage 0x%X != 期望 0x%X —— 这份不是偏移对应的构建，"
                    "拒绝读数（偏移会用错）" % (size, want_size))
            if path and os.path.exists(path):
                h = hashlib.md5()
                with open(path, "rb") as f:
                    for c in iter(lambda: f.read(1 << 20), b""):
                        h.update(c)
                got = h.hexdigest()
                self.info["md5"] = got
                self.info["md5_match"] = (got == MEM_BUILD["md5"])
                fsz = os.path.getsize(path)
                self.info["exe_size"] = fsz
                self.info["exe_size_match"] = (fsz == MEM_BUILD["exe_size"])
        if self._m is None:
            self._m = _Mem(pid)
        self.base = base

    def describe(self) -> str:
        i = self.info or {}
        tag = "" if i.get("md5_match", True) else "(md5≠表内，SizeOfImage 相同⇒仍可用)"
        return "mem(pid=%s base=0x%X md5=%s%s)" % (i.get("pid"), i.get("base", 0),
                                                     i.get("md5", "?"), tag)

    def close(self):
        if self._m:
            self._m.close()
            self._m = None

    # -- 定位 ------------------------------------------------------------
    def _locate(self, m: _Mem):
        """GWorld -> UWorld -> GameState。返回 (world, gamestate, in_battle, notes)。"""
        notes = []
        world = m.ptr(self.base + _build.RVA["GWorld"])
        if not world:
            return None, None, None, ["GWorld 为空（世界还没建出来）"]
        uclass = m.ptr(world + OFF_UOBJECT_CLASS)
        if not _propsize_hits(m, uclass, (SIZE_UWORLD,)):
            return None, None, None, ["GWorld 指的不是 UWorld"]
        gs = m.ptr(world + OFF_UWORLD_GAMESTATE)
        if not gs:
            return None, None, None, ["UWorld+0x1B0 GameState 为空"]
        hits = _propsize_hits(m, m.ptr(gs + OFF_UOBJECT_CLASS),
                              (SIZE_AKARDS_GAMESTATE, SIZE_BP_GAMESTATE_BATTLE))
        size = hits[0][1] if hits else None
        if size is None:
            notes.append("GameState 类尺寸既不是 912 也不是 1960（可能是别的界面）")
        return world, gs, size == SIZE_BP_GAMESTATE_BATTLE, notes

    # -- 轻量读（回合首步等指挥点用；毫秒级，不建整张快照） ------------------------
    def peek_turn_state(self, my_side=None) -> dict:
        """只读 **GameState 里的几个标量**：回合号 / `ActionProcess` / 两侧指挥点与槽数（一次原子块读 + 解密）。

        为什么有它（2026-10-08）：回合首步要轮询"指挥点定了没"，而完整 `snapshot()` 要读全盘卡
        （实机 0.29–0.8 s/次）；这里只做 GWorld→UWorld→GameState 定位（3 次指针读）+ 原子块 + 2 个标量，毫秒级。
        返回 `{"ok", "turn", "action_process", "kredits":{ESide:int|None}, "slots":{…}, "mine_k", "mine_slots"}`；
        `my_side` 给了才填 `mine_*`（座位由调用方用整张快照的 `st.my_side` 传入，这里不再另推）。
        任何一步读不出 ⇒ `ok=False`（不猜）。"""
        out = {"ok": False, "turn": None, "action_process": None,
               "kredits": {}, "slots": {}, "mine_k": None, "mine_slots": None}
        self._attach()
        m = self._m
        _w, gs, in_battle, _n = self._locate(m)
        if not gs or not in_battle:
            return out
        blk = m.blob(gs + GS_SIDE_BLOCK_OFF, GS_SIDE_BLOCK_LEN)
        if not blk or len(blk) < GS_SIDE_BLOCK_LEN:
            return out
        key = struct.unpack_from("<i", blk, OFF_AGS_KEY - GS_SIDE_BLOCK_OFF)[0]
        for (kind, lr), off in AGS_SIDES.items():
            X, Y, _Z, enc, _rnd = struct.unpack_from("<5i", blk, off - GS_SIDE_BLOCK_OFF)
            seat = ESide.left if lr == "left" else ESide.right
            (out["kredits"] if kind == "kredit" else out["slots"])[seat] = _decrypt(key, X, Y, enc)
        ap = m.u8(gs + BGS_U8["action_process"])
        out["action_process"] = None if ap is None else bool(ap)
        out["turn"] = m.i32(gs + BGS_I32["current_turn"])
        if my_side is not None:
            out["mine_k"] = out["kredits"].get(my_side)
            out["mine_slots"] = out["slots"].get(my_side)
        out["ok"] = True
        return out

    # -- 快照 ------------------------------------------------------------
    def snapshot(self) -> BoardState:
        self._attach()
        m = self._m
        st = BoardState(source=self.name)
        st.unsupported = []
        world, gs, in_battle, notes = self._locate(m)
        st.unknown.extend(notes)

        # ★ 哪一边是本地玩家：ABP_Board_C::mySide。**每局可能不同**，不能写死 side==1。
        my_side = read_my_side(m, gs)
        my_side_src = "gamestate_parity"
        if my_side is None:
            # 回合奇偶反推需要 turn>=1，换牌阶段（turn 还没定型）会返回 None
            # （§7.6d 记过这个窗口不稳）。兜底：kardsmem 的 Logic.mySide，
            # 不受这条限制（2026-09-24 验证过，见 kardsmem/matchlog.py）。
            ml = self._logic_matchlog()
            if ml is not None:
                v = ml.my_side()
                if v is not None:
                    my_side, my_side_src = v, "logic_myside_fallback"
        st.my_side = enum_or_none(ESide, my_side)
        g = GameState(mySide=st.my_side)                          # 游戏原版结构
        st.game = g
        if my_side is None:
            st.unknown.append("本地是哪一侧推不出（turn/startingSide/isLocalClientTurn 不全，"
                              "kardsmem 兜底也没读到）⇒ mySide=None，不能按『我方』取牌")
        elif my_side_src == "logic_myside_fallback":
            st.unknown.append("my_side_raw 来自 kardsmem Logic.mySide 兜底"
                              "（GameState 回合奇偶反推当时读不出，通常发生在换牌阶段）")
        if not gs:
            st.complete = False
            return st

        # kredits / slots：一条原子读，见 reports/spec/KARDS-AUTOMATION.md §8「坑清单」
        blk = m.blob(gs + GS_SIDE_BLOCK_OFF, GS_SIDE_BLOCK_LEN)
        if blk:
            key = struct.unpack_from("<i", blk, OFF_AGS_KEY - GS_SIDE_BLOCK_OFF)[0]
            for (kind, lr), off in AGS_SIDES.items():
                X, Y, _Z, enc, _rnd = struct.unpack_from("<5i", blk, off - GS_SIDE_BLOCK_OFF)
                v = _decrypt(key, X, Y, enc)
                seat = ESide.left if lr == "left" else ESide.right
                (st.kredits if kind == "kredit" else st.slots)[seat] = v
                (g.kredits if kind == "kredit" else g.kreditSlots)[seat] = v
        else:
            st.unknown.append("读不到 kredits/slots 那一块")

        if not in_battle:
            st.unknown.append("不在对局里（只有 kredits/slots 可用）")
            st.complete = False
            return st

        b = {k: m.u8(gs + o) for k, o in BGS_U8.items()}
        i = {k: m.i32(gs + o) for k, o in BGS_I32.items()}
        st.turn = i["current_turn"]
        st.our_turn = None if b["is_local_client_turn"] is None else bool(b["is_local_client_turn"])
        st.action_process = None if b["action_process"] is None else bool(b["action_process"])
        st.match_finished = None if b["match_finished"] is None else bool(b["match_finished"])
        st.weather_played_this_turn = (None if b["weather_played_this_turn"] is None
                                       else bool(b["weather_played_this_turn"]))
        st.operation_kredits_spent = i["operation_kredits_spent"]
        st.max_possible_kredits = i["max_possible_kredits"]
        g.currentTurn, g.isLocalClientTurn, g.matchFinished = st.turn, st.our_turn, st.match_finished
        g.startingSide = enum_or_none(ESide, b["starting_side"])
        g.weatherPlayedThisTurn = st.weather_played_this_turn
        g.operationKreditsSpent, g.maxPossibleKredits = st.operation_kredits_spent, st.max_possible_kredits
        g.FrontlineOwner = enum_or_none(ESide, b["frontline_owner"])
        try:
            from kardsmem import containers as _ct
            st.registry = _ct.tmap_u8_to_intset(m, gs + OFF_CARD_FUNCTION_TRIGGERS)
        except Exception as _exc:                                 # noqa: BLE001
            st.registry = None
            st.unknown.append("触发注册表 CardFunctionTriggers 读不出：%s" % type(_exc).__name__)
        try:
            from kardsmem import containers as _ct2
            st.frontline_limiters = _ct2.tset_int32_values(m, gs + OFF_FRONTLINE_LIMITERS)
        except Exception as _exc:                                 # noqa: BLE001
            st.frontline_limiters = None
            st.unknown.append("FrontlineLimiters 读不出：%s" % type(_exc).__name__)
        st.slots_lost = {ESide.left: i["slots_lost_left"], ESide.right: i["slots_lost_right"]}
        st.fatigue = {ESide.left: i["fatigue_left"], ESide.right: i["fatigue_right"]}
        g.FrontlineLimiters, g.CardFunctionTriggers = st.frontline_limiters, st.registry
        g.slotsLost = {ESide.left: i["slots_lost_left"], ESide.right: i["slots_lost_right"]}
        g.fatigue = {ESide.left: i["fatigue_left"], ESide.right: i["fatigue_right"]}
        g.hqCardID = {ESide.left: i["hq_card_id_left"], ESide.right: i["hq_card_id_right"]}
        st.frontline_owner = g.FrontlineOwner

        # HQ 卡对象：权威句柄，单独拿
        hq_ptrs = {"hq_left": m.ptr(gs + BGS_PTR["hq_left"]),
                   "hq_right": m.ptr(gs + BGS_PTR["hq_right"])}

        cards = {}
        for _ei, (ptr, mkey) in enumerate(self._enumerate_cards(m, gs)):
            c = self._read_card(m, ptr, st.unknown)
            if c:
                c.raw["map_key"] = mkey
                # 枚举下标 = 触发表顺序 = 创建顺序（engine/triggers.py::_registry_order 用它排；
                # 共用一条 RNG 流时"谁先抽"取决于它）
                c.raw["enum_idx"] = _ei
                cards[ptr] = c
        for raw_side, p in (("hq_left", hq_ptrs["hq_left"]), ("hq_right", hq_ptrs["hq_right"])):
            if p and p not in cards:
                c = self._read_card(m, p, st.unknown)
                if c:
                    c.raw["enum_idx"] = 10 ** 9      # 不在 AllCardsInBattle 里的兜底 HQ：排最后
                    cards[p] = c

        # ★ 2026-10-02 IDA 普查（Claude，`BOARD-QUERY-NATIVES-1.60.md`）：游戏里枚举场上卡的真实顺序
        #    = `AllCardsInBattle`（TMap）**稀疏数组下标升序 = 卡牌创建顺序**。
        #    `_enumerate_cards` 就是按下标升序 yield 的 ⇒ 这里**不要再排序**：
        #    旧代码按 (side, location, slot, uid) 排会把真实顺序打乱，热浪那类
        #    `GetRandomCard` 会**选错目标**（我们的 hook 是把这个顺序喂给 VM 的）。
        st.cards = list(cards.values())
        for c in st.cards:                       # AllCardsInBattle：保持游戏里的枚举顺序（= 创建顺序），键 = 地图键
            g.AllCardsInBattle[c.raw.get("map_key", c.obj.CardID if c.obj else None)] = c.obj
        for c in st.cards:
            if c.obj.IsHQ() and c.defense is not None:
                st.hq[c.obj.side] = c

        if not st.cards:
            st.unknown.append("一张卡都没枚举到")
        if st.hq[ESide.left] is None or st.hq[ESide.right] is None:
            st.unknown.append("HQ 没读全")
        st.complete = not st.unknown
        return st

    def _enumerate_cards(self, m: _Mem, gs: int):
        """AllCardsInBattle (TMap) -> [(卡对象指针, map key)] 列表。

        元素 24 字节（见 `TMAP_ELEM_SIZE` 的说明），指针在 `+0x08`、key 在 `+0`。
        空闲槽靠"指针能不能过类身份校验"排除 —— 这个后端刻意识别不出名字，
        所以判据只能是 `UClass` 沿 `SuperStruct` 链能追到 `UBaseCardObject`
        （BP 子类尺寸不是 1656，直接比尺寸会漏），外加排除 CDO。
        """
        map_addr = gs + OFF_ALLCARDSINBATTLE
        data = m.ptr(map_addr)
        n = m.i32(map_addr + TARRAY_NUM_OFF)
        if not data or not n or not (0 < n < 100000):
            return []
        elems = m.blob(data, n * TMAP_ELEM_SIZE)
        if not elems:
            return []
        cache, out, seen = {}, [], set()
        for off in range(0, len(elems) - TMAP_ELEM_SIZE + 1, TMAP_ELEM_SIZE):
            key = struct.unpack_from("<i", elems, off)[0]
            p = struct.unpack_from("<Q", elems, off + TMAP_VALUE_OFF)[0]
            if not (PTR_MIN <= p <= PTR_MAX) or p % 8 or p in seen:
                continue
            seen.add(p)
            fl = m.u32(p + OFF_UOBJECT_FLAGS)
            if fl is not None and (fl & FLAG_RF_CDO):
                continue
            if _derives_from(m, m.ptr(p + OFF_UOBJECT_CLASS), SIZE_BASECARD, cache):
                out.append((p, key))
        return out

    def _read_card(self, m: _Mem, p: int, notes: Optional[list] = None) -> Optional[Card]:
        key = m.i32(p + CARD_KEY_OFF)
        rec = m.blob(p + CARD_REC_LO, CARD_REC_HI - CARD_REC_LO)
        if rec is None or key is None:
            return None
        vals = {}
        for nm, roff in CARD_RECORDS.items():
            X, Y, _Z, enc, _rnd = struct.unpack_from("<5i", rec, roff - CARD_REC_LO)
            vals[nm] = _decrypt(key, X, Y, enc)

        u8 = {k: m.u8(p + o) for k, o in CARD_U8.items()}
        i32 = {k: m.i32(p + o) for k, o in CARD_I32.items()}
        tgt = m.ptr(p + CARD_PTR["current_target"])

        # AllCardsInBattle 里还混着两条占位条目（实测 key = 30000000 / 60000000）：
        # Location 与 Type 都是 NotAvailable(0)，加密记录也无效。跳过。
        if u8["location_enum"] == 0 and u8["type_enum"] == 0:
            return None

        # AllCardsInBattle 里的 HQ 卡：Location 是 Board_HQLeft/Right 且 Type=location（`obj.IsHQ()`）。
        kw_dbg = {k: u8[k] for k, _ in _KEYWORDS}

        def _b(v):
            return None if v is None else bool(v)

        obj = BaseCardObject(
            CardID=i32["card_id"], Type=enum_or_none(EType, u8["type_enum"]),
            Location=enum_or_none(ECardLocation, u8["location_enum"]), side=enum_or_none(ESide, u8["side_enum"]),
            locationNumber=i32["location_number"], faction=enum_or_none(EFaction, u8["faction_enum"]),
            rarity=enum_or_none(ERarity, u8["rarity_enum"]), cardSet=u8["card_set_enum"],
            attack=vals["attack"], attackBuff=vals["attackBuff"], defense=vals["defense"],
            range=i32["range"],
            maxAttack=i32["max_attack"], maxDefense=i32["max_defense"],
            kredits=vals["kredit"], kreditsBuff=vals["kreditBuff"], KreditsTax_AsEnemyTarget=i32["kredits_tax"],
            operationCost=i32["operation_cost"], operationCostBuff=i32["operation_cost_buff"],
            heavyArmor=i32["heavy_armor"], heavyArmorBuff=i32["heavy_armor_buff"], cipher=i32["cipher"],
            movementLeft=i32["movement_left"], attackLeft=i32["attack_left"], enterPlayOnTurn=i32["enter_play_turn"],
            hasEverAttacked=_b(u8["has_ever_attacked"]), hasAttackedThisTurn=_b(u8["has_attacked_this_turn"]),
            hasBeenAttackedThisTurn=_b(u8["has_been_attacked_this_turn"]), gotchaActivated=i32["gotcha_activated"],
            isBeingGuarded=_b(u8["is_being_guarded"]), underEnemyControl=_b(u8["under_enemy_control"]),
            isImmune=_b(u8["is_immune"]),
            isSuppressed=_b(u8["is_suppressed"]), isRevealed=_b(u8["is_revealed"]), isGoldCard=_b(u8["is_gold_card"]),
            isSalvaged=_b(u8["is_salvaged"]), cardSeen=_b(u8["card_seen"]),
            selectTargetOnPlayedFromHand=_b(u8["needs_hand_target"]),
            hasDeployment=_b(u8["has_deployment"]), hasDestruction=_b(u8["has_destruction"]), hasBlitz=_b(u8["has_blitz"]),
            hasAmbush=_b(u8["has_ambush"]), hasSmokescreen=_b(u8["has_smokescreen"]), hasMobilize=_b(u8["has_mobilize"]),
            hasAlpine=_b(u8["has_alpine"]), hasFury=_b(u8["has_fury"]), hasGuard=_b(u8["has_guard"]),
            hasShock=_b(u8["has_shock"]), hasCovert=_b(u8["has_covert"]), hasScrying=_b(u8["has_scrying"]),
            hasPincer=_b(u8["has_pincer"]), hasSalvage=_b(u8["has_salvage"]),
            ptr=p, title=_read_card_name(m, p), CurrentTarget=tgt or None)

        # ★ 2026-10-04（P3 ③ `excess`）：**运行时自定义能力** —— `receivedAbilitiesFromCards`@0x208
        #   （`CustomAbilityAdd/Remove` 写、`cards.read_received_abilities` 读）。
        #   原版 `ExecuteAttackCard`（`:17218`，只读反汇编定案）用 `HasCustomAbility("excess")`
        #   决定"超过目标总防的伤害是否转打敌方总部" ⇒ sim 需要它。
        #   **只对场上的牌读**（location 5/6/7）：攻击者/防守方只可能是场上的牌，而每张牌一次
        #   TMap 读 —— 80 张全读会拖慢每次快照。读失败**记进 notes**（不静默；弯路 #20）。
        _abils = None
        if u8["location_enum"] in (5, 6, 7):
            try:
                from . import cards as _cards

                class _S:                       # `read_received_abilities` 只用 `session.m`
                    pass

                _s = _S()
                _s.m = m
                _abils = [r.get("ability") for r in _cards.read_received_abilities(_s, p)
                          if r.get("ability")]
            except Exception as exc:                                # noqa: BLE001
                if notes is not None:
                    notes.append("received_abilities 读不出（cardID=%s ptr=%#x: %s）"
                                 % (i32["card_id"], p, exc))

        return Card(
            uid="0x%X" % p, obj=obj,
            raw={"ptr": p, "side_enum": u8["side_enum"], "location_enum": u8["location_enum"],
                 "received_abilities": _abils,
                 "type_enum": u8["type_enum"], "attack_plain": i32["attack_plain"],
                 "defense_plain": i32["defense_plain"], "range": i32["range"], "kredits_plain": i32["kredits_plain"],
                 "choose_one_index": i32["choose_one_index"],
                 "move_left": i32["movement_left"], "attack_left": i32["attack_left"],
                 "enter_play_turn": i32["enter_play_turn"],
                 "pinned_turns": i32["pinned_turns"],
                 "heavy_armor": i32["heavy_armor"],
                 "is_being_guarded": u8["is_being_guarded"],
                 "is_immune": u8["is_immune"],
                 "under_enemy_control": u8["under_enemy_control"],
                 "has_been_attacked_this_turn": u8["has_been_attacked_this_turn"],
                 "is_gold_card": u8["is_gold_card"], "is_salvaged": u8["is_salvaged"],
                 "card_seen": u8["card_seen"],
                 "all_keyword_flags": kw_dbg})


# --------------------------------------------------------------------------
# 五、自检：不依赖游戏，起一个合成目标进程验证整条链路
# --------------------------------------------------------------------------

_SELFTEST_TARGET = r'''
import ctypes, json, sys, time
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
k32.VirtualAlloc.restype = ctypes.c_void_p
MEM_RESERVE, MEM_COMMIT, PAGE_RW = 0x2000, 0x1000, 0x04
base = k32.VirtualAlloc(None, 0x0A000000, MEM_RESERVE, PAGE_RW)

def commit(addr):
    a = addr & ~0xFFF
    assert k32.VirtualAlloc(ctypes.c_void_p(a), 0x1000 + (addr - a), MEM_COMMIT, PAGE_RW), hex(addr)

def w64(a, v): ctypes.c_uint64.from_address(a).value = v & 0xFFFFFFFFFFFFFFFF
def w32(a, v): ctypes.c_uint32.from_address(a).value = v & 0xFFFFFFFF
def w8(a, v):  ctypes.c_uint8.from_address(a).value = v & 0xFF

RVA_GWORLD = 0x08F625B0
world, gs = base + 0x05000000, base + 0x06000000
ucw, ucg, ucc = base + 0x07000000, base + 0x07100000, base + 0x07200000
unit, hqL, hqR = base + 0x08000000, base + 0x08100000, base + 0x08200000
for a in (base + RVA_GWORLD, world, gs, ucw, ucg, ucc, unit, hqL, hqR):
    commit(a)

KEY = 0x1234

def mkcard(p, loc, side, cid, ctype, atk, atkB, kred, kredB, dfn, ma, md, slot=0):
    w64(p + 0x10, ucc)
    w32(ucc + 0x58, 1656)
    w32(p + 0x55C, KEY)
    for off, val in ((0x568, atk), (0x57C, atkB), (0x590, kred), (0x5A4, kredB), (0x5B8, dfn)):
        X, Y = 1, 0
        w32(p + off + 0x00, X); w32(p + off + 0x04, Y); w32(p + off + 0x08, 0)
        w32(p + off + 0x0C, KEY ^ (Y + X * val)); w32(p + off + 0x10, 0)
    w8(p + 0x275, loc); w8(p + 0x276, side)
    w32(p + 0x264, cid); w32(p + 0x278, slot); w8(p + 0x68, ctype)
    w32(p + 0x2A4, ma); w32(p + 0x2A8, md)
    w32(p + 0x26C, 1); w8(p + 0x281, 0)
    w8(p + 0x33A, 1); w8(p + 0x11D, 1); w8(p + 0x1E8, 1)

mkcard(unit, 7, 1, 38, 6, 3, 1, 2, 0, 4, 3, 4, slot=2)
mkcard(hqL, 5, 1, 1, 1, 0, 0, 0, 0, 12, 0, 20)
mkcard(hqR, 6, 2, 41, 1, 0, 0, 0, 0, 2, 0, 20)

w32(ucw + 0x58, 2536)
w64(world + 0x10, ucw)                 # UObject::ClassPrivate —— 少了这行 _locate 会拒绝
w64(world + 0x1B0, gs)
w64(gs + 0x10, ucg)
w32(ucg + 0x58, 1960)

for off, val in ((0x33C, 12), (0x350, 10), (0x364, 12), (0x378, 11)):
    w32(gs + off + 0x00, 1); w32(gs + off + 0x04, 0)
    w32(gs + off + 0x08, 0); w32(gs + off + 0x0C, KEY ^ val); w32(gs + off + 0x10, 0)
w32(gs + 0x334, KEY)
w32(gs + 0x4B8, 1); w32(gs + 0x4BC, 2)
w32(gs + 0x5D8, 3); w32(gs + 0x5DC, 4)
w8(gs + 0x680, 1); w32(gs + 0x684, 23); w8(gs + 0x688, 0); w8(gs + 0x3A0, 1)
w64(gs + 0x5E8, hqL); w64(gs + 0x5F8, hqR)

arr = ctypes.create_string_buffer(72)
aa = ctypes.addressof(arr)
def elem(i, key, val):
    ctypes.c_int32.from_address(aa + i * 24).value = key
    ctypes.c_uint64.from_address(aa + i * 24 + 8).value = val
elem(0, 38, unit)
elem(1, 999, 0)                        # 空闲槽：指针为 0，会被范围检查丢掉
elem(2, 38, unit)                      # 重复指针 -> 去重
w64(gs + 0x538, aa); w32(gs + 0x540, 3); w32(gs + 0x544, 3)   # ptr@+0, Num@+8, Max@+0xC
w64(base + RVA_GWORLD, world)

print(json.dumps({"base": base}))
sys.stdout.flush()
time.sleep(120)
'''


def _run_selftest() -> int:
    import subprocess
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".py", prefix="kards_fake_")
    with os.fdopen(fd, "w") as f:
        # ★ 2026-09-25：合成目标里那句 `RVA_GWORLD = 0x08F625B0` 是**按当前选中的构建**
        #   替换掉的 —— 以前写死 Steam 的值，换 launcher 档时父子两边差 0x3000，
        #   selftest 就全读成 None（看起来像"偏移表错了"，其实是合成目标没跟上）。
        # ★ P7-S3b：这里必须直接读 `_build.RVA`（裸 `RVA_GWORLD` 不会走模块 __getattr__）。
        f.write(_SELFTEST_TARGET.replace("RVA_GWORLD = 0x08F625B0",
                                         "RVA_GWORLD = 0x%X" % _build.RVA["GWorld"]))
    proc = subprocess.Popen([sys.executable, path], stdout=subprocess.PIPE, text=True)
    try:
        try:
            info = json.loads(proc.stdout.readline())
        except Exception as exc:
            print("合成目标没报地址: %s" % exc)
            return 2
        src = MemoryBoardSource(pid=proc.pid, validate_build=False, base=info["base"])
        try:
            st = src.snapshot()
        finally:
            src.close()
    finally:
        proc.kill()
        try:
            os.unlink(path)
        except OSError:
            pass

    _render(st, True)

    checks = []

    def chk(name, got, want):
        checks.append((name, got, want, got == want))

    chk("turn", st.turn, 23)
    chk("our_turn", st.our_turn, True)
    chk("match_finished", st.match_finished, False)
    chk("frontline_owner", st.frontline_owner, ESide.left)
    chk("kredits.left", st.kredits[ESide.left], 12)
    chk("kredits.right", st.kredits[ESide.right], 10)
    chk("slots.left", st.slots[ESide.left], 12)
    chk("slots.right", st.slots[ESide.right], 11)
    chk("slots_lost.left", st.slots_lost[ESide.left], 1)
    chk("fatigue.right", st.fatigue[ESide.right], 3)
    chk("hq.left.defense", st.hq[ESide.left].defense if st.hq[ESide.left] else None, 12)
    chk("hq.right.defense", st.hq[ESide.right].defense if st.hq[ESide.right] else None, 2)
    chk("cards（空闲槽已跳过 + 重复指针已去重）", len(st.cards), 3)
    # 游戏原版结构（st.game / card.obj）与派生视图同源
    g = st.game
    chk("game.currentTurn / startingSide 为原版枚举", (g.currentTurn, type(g.startingSide).__name__), (23, "ESide"))
    chk("game.kredits 以 ESide 为键，值与 st.kredits 同源", dict(g.kredits), dict(st.kredits))
    chk("game.AllCardsInBattle 张数 == 视图张数", len(g.AllCardsInBattle), len(st.cards))
    chk("card.obj.Location / side 是原版枚举", {(type(c.obj.Location).__name__, type(c.obj.side).__name__) for c in st.cards},
        {("ECardLocation", "ESide")})
    unit = [c for c in st.cards if c.card_id == 38]
    chk("unit 找得到且唯一", len(unit), 1)
    for c in unit:
        chk("attack", c.attack, 3)
        chk("attack_buff", c.attack_buff, 1)
        chk("total_attack", c.total_attack, 4)
        chk("defense", c.defense, 4)
        chk("kredit_cost", c.kredit_cost, 2)
        chk("Location", c.obj.Location, ECardLocation.Board_Frontline)
        chk("side", c.side, ESide.left)
        chk("slot", c.slot, 2)
        chk("card_type", c.card_type, "infantry")
        chk("can_act", c.can_act, True)
        chk("is_suppressed", c.is_suppressed, True)
        chk("needs_hand_target", c.needs_hand_target, True)
        chk("keywords 有 guard", "guard" in c.keywords, True)
    # 合成目标的 GameState 里没有 startingSide/turn ⇒ 本地是哪一侧必然推不出。
    # 这一条是**预期内**的，其余任何 unknown 都不允许。真实对局里推不出会把 complete
    # 压成 False，那是有意为之：side 映射不确定时 local/enemy 可能整体互换。
    _expected = [u for u in st.unknown if "本地是哪一侧" in u]
    chk("unknown 只剩合成目标给不出的『本地是哪一侧』", len(st.unknown) - len(_expected), 0)
    chk("推不出本地侧时确实被记进 unknown", len(_expected), 1)
    chk("complete（扣除预期项）", st.complete or len(st.unknown) == len(_expected), True)

    ok = all(c[3] for c in checks)
    print("SELFTEST")
    for name, got, want, good in checks:
        print("  [%s] %-34s got=%s want=%s" % ("PASS" if good else "FAIL", name, got, want))
    print("SELFTEST RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 六、工厂
# --------------------------------------------------------------------------

def open_source(prefer: Optional[str] = None, **kw) -> BoardSource:
    """盘面来源。现在只有读内存一个后端（OCR 后端已移除）；多余的关键字参数被忽略。"""
    mem_kw = {k: v for k, v in kw.items() if k in ("pid", "validate_build", "base")}
    return MemoryBoardSource(**mem_kw)


# --------------------------------------------------------------------------
# 六、CLI
# --------------------------------------------------------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KARDS 盘面信息黑箱 API")
    ap.add_argument("--source", default=None, choices=[None, "mem"])
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--watch", type=float, default=0.0, help="秒；>0 则循环采样")
    ap.add_argument("--cards", action="store_true", help="附带打印卡列表")
    ap.add_argument("--selftest", action="store_true",
                    help="起合成目标进程自检，不需要游戏")
    args = ap.parse_args(argv)

    if args.selftest:
        return _run_selftest()

    src = open_source(args.source)
    try:
        if src.available():
            print("source: %s" % src.describe())
        else:
            print("source: %s 不可用" % src.name)
            return 2
        while True:
            st = src.snapshot()
            if args.json:
                print(json.dumps(st.as_dict(), ensure_ascii=False, default=str))
            else:
                _render(st, args.cards)
            if args.watch <= 0:
                return 0 if st.complete else 1
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    finally:
        close = getattr(src, "close", None)
        if close:
            close()


def _render(st: BoardState, show_cards: bool) -> None:
    def n(v):
        return "?" if v is None else v
    print("=" * 76)
    L, R = ESide.left, ESide.right
    print("turn=%s  our_turn=%s  mySide=%s  complete=%s" % (
        n(st.turn), n(st.our_turn), n(st.my_side), st.complete))
    print("kredits  left=%-4s right=%-4s   slots left=%-4s right=%-4s" % (
        n(st.kredits[L]), n(st.kredits[R]), n(st.slots[L]), n(st.slots[R])))
    print("slots_lost left=%-3s right=%-3s  fatigue left=%-3s right=%-3s" % (
        n(st.slots_lost[L]), n(st.slots_lost[R]), n(st.fatigue[L]), n(st.fatigue[R])))
    print("frontline_owner=%s  match_finished=%s  op_spent=%s" % (
        n(st.frontline_owner), n(st.match_finished), n(st.operation_kredits_spent)))
    for s in (L, R):
        hq = st.hq[s]
        print("%-5s HQ id=%-5s def=%-4s | hand=%-2d front=%-2d support=%-2d discard=%-2d" % (
            s.name + ('*' if s == st.my_side else ''), n(hq.card_id if hq else None), n(hq.defense if hq else None),
            len(st.hand(s)), len(st.board(s)), len(st.support(s)), len(st.discard(s))))
    if st.unknown:
        print("unknown: %s" % "; ".join(st.unknown))
    if st.unsupported:
        print("unsupported: %s" % "; ".join(st.unsupported))
    if show_cards:
        print("-" * 76)
        print("%-26s %-6s %-9s %-10s %-5s %-4s %s" % (
            "name", "side", "loc", "type", "a/d", "cost", "kw/target"))
        for c in st.cards:
            print("%-26s %-6s %-9s %-10s %-5s %-4s %s%s" % (
                (c.name or "-")[:26], c.side.name if c.side else None, c.obj.Location.name if c.obj.Location else None, c.card_type,
                "%s/%s" % (c.attack, c.defense), c.kredit_cost,
                ",".join(c.keywords),
                (" -> " + c.target_uid) if c.target_uid else ""))
    print("=" * 76)


if __name__ == "__main__":
    sys.exit(_main())
