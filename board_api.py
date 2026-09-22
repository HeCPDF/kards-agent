#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
board_api.py —— 盘面信息的黑箱 API。

目的
====
把"盘面长什么样"这件事和数据来源**解耦**。调用方只认 `BoardState`，
不关心它是截图识别出来的还是读进程内存读出来的：

    from board_api import open_source
    src = open_source()          # 或 open_source("ocr") / open_source("mem")
    st  = src.snapshot()
    st.kredits["local"], st.our_turn, len(st.board("local"))

为什么需要这层
==============
- 读进程内存（`mem`）精度高、不受分辨率/遮挡影响，但对某些执行环境属于
  **侵入式**手段，可能不被允许；
- 截图识别（`ocr`）是非侵入式的，代价是慢、依赖模板与标定。

所以两条路都留在同一个接口后面，用 `--source` / `KARDS_BOARD_SOURCE` 切换，
`auto` 会优先用可用的那个。**调用方代码不需要改。**

本文件是自包含的：`mem` 后端不依赖 reverse-data 那棵树，`ocr` 后端
只依赖本仓库 src/ 下已有的模块。

约定
====
- 坐标一律是游戏窗口 **客户区** 坐标（本仓库惯用 1280x720）。
- 阵营统一归一化成 `"local"` / `"enemy"`；原始 `ESideEnum`（1=left, 2=right）
  放在 `Card.raw["side_enum"]`。实测 **left(1) = 本地**。
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

# --------------------------------------------------------------------------
# 零、本仓库 / 上游仓库的位置
# --------------------------------------------------------------------------
# 上游 `OCR-Kards-Auto`（yumehanab1，GPL-3.0）是**别人的项目**，我们只读不改。
# 自己加的状态和模板放在 `kards-agent/config/` 当 overlay，启动时盖在上游之上。
# 这样上游随时能 `git pull` 而不产生冲突，我们的改动也不会混进别人的工作树。
AGENT_ROOT = os.path.dirname(os.path.abspath(__file__))
AGENT_UPSTREAM = os.environ.get("KARDS_OCR_ROOT") or os.path.join(
    os.path.dirname(AGENT_ROOT), "OCR-Kards-Auto")


def _overlay_json(name: str) -> dict:
    p = os.path.join(AGENT_ROOT, "config", name)
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _overlay_templates() -> dict:
    """我们自己的模板。★ path 要转成**绝对路径** —— 上游的 `load_templates`
    把相对路径接在它自己的 PROJECT_ROOT 上，否则会去上游树里找我们的图。"""
    out = {}
    for k, v in (_overlay_json("templates.json").get("templates") or {}).items():
        v = dict(v)
        if not os.path.isabs(v.get("path", "")):
            v["path"] = os.path.join(AGENT_ROOT, v["path"])
        out[k] = v
    return out


def _overlay_states() -> dict:
    return _overlay_json("states.json").get("states") or {}


# --------------------------------------------------------------------------
# 一、归一化的盘面模型（调用方只认这些）
# --------------------------------------------------------------------------

LOCAL, ENEMY = "local", "enemy"

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


def side_maps(my_side):
    """→ (by_enum, by_lr)：{1/2 → local/enemy} 与 {'left'/'right' → local/enemy}。

    my_side 读不出时退回"1=本地"这个历史默认，并由调用方记进 unknown。
    """
    if my_side == SIDE_RIGHT:
        return ({0: None, SIDE_LEFT: ENEMY, SIDE_RIGHT: LOCAL},
                {"left": ENEMY, "right": LOCAL})
    return ({0: None, SIDE_LEFT: LOCAL, SIDE_RIGHT: ENEMY},
            {"left": LOCAL, "right": ENEMY})


# 历史默认（my_side 读不出时用）。**不要**直接拿它当映射表 —— 用 side_maps()。
SIDE_ENUM = {0: None, SIDE_LEFT: LOCAL, SIDE_RIGHT: ENEMY}


@dataclass
class Card:
    """一张牌的归一化状态。任何字段都可能是 None（读不出就是读不出）。"""
    uid: str                     # 本次快照内稳定；mem 后端就是对象地址
    side: Optional[str] = None
    location: Optional[str] = None       # deck/hand/frontline/hq/discard
    slot: Optional[int] = None           # locationNumber，阵线上的列号
    card_id: Optional[int] = None
    name: Optional[str] = None
    card_type: Optional[str] = None
    attack: Optional[int] = None
    attack_buff: Optional[int] = None
    defense: Optional[int] = None
    max_attack: Optional[int] = None
    max_defense: Optional[int] = None
    kredit_cost: Optional[int] = None
    operation_cost: Optional[int] = None
    kredits_tax_as_enemy_target: Optional[int] = None   # 被敌方指向时额外加费
    is_suppressed: Optional[bool] = None
    is_revealed: Optional[bool] = None
    can_act: Optional[bool] = None
    target_uid: Optional[str] = None
    needs_hand_target: Optional[bool] = None   # selectTargetOnPlayedFromHand
    enter_play_on_turn: Optional[int] = None   # 这张牌进场的回合号（和 BoardState.turn 比较）
    keywords: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def total_attack(self) -> Optional[int]:
        if self.attack is None:
            return None
        return self.attack + (self.attack_buff or 0)


@dataclass
class BoardState:
    source: str
    taken_at: float = field(default_factory=time.time)
    complete: bool = True
    unknown: list = field(default_factory=list)       # 这个后端应该有、但这次没读到
    unsupported: list = field(default_factory=list)   # 这个后端按设计就不提供

    turn: Optional[int] = None
    our_turn: Optional[bool] = None
    our_side: Optional[str] = None
    my_side_raw: Optional[int] = None   # ABP_Board_C::mySide（1/2）；None = 读不出
    kredits: dict = field(default_factory=lambda: {LOCAL: None, ENEMY: None})
    slots: dict = field(default_factory=lambda: {LOCAL: None, ENEMY: None})
    slots_lost: dict = field(default_factory=lambda: {LOCAL: None, ENEMY: None})
    fatigue: dict = field(default_factory=lambda: {LOCAL: None, ENEMY: None})
    frontline_owner: Optional[str] = None
    weather_played_this_turn: Optional[bool] = None
    operation_kredits_spent: Optional[int] = None
    match_finished: Optional[bool] = None
    hq: dict = field(default_factory=lambda: {LOCAL: None, ENEMY: None})
    cards: list = field(default_factory=list)

    # ---- 便捷视图 --------------------------------------------------------
    def hand(self, side: str = LOCAL) -> list:
        return [c for c in self.cards if c.side == side and c.location == "hand"]

    def board(self, side: str = LOCAL) -> list:
        """前线（`Board_Frontline=7`）上的单位。"""
        return [c for c in self.cards if c.side == side and c.location == "frontline"]

    def support(self, side: str = LOCAL) -> list:
        """后排（`Board_HQLeft/Right=5/6`）里除 HQ 以外的单位。"""
        return [c for c in self.cards if c.side == side and c.location == "back"]

    def field_units(self, side: str = LOCAL) -> list:
        """真正在场的单位：前线 + 后排（不含 HQ），排除弃牌堆/牌库。"""
        return [c for c in self.cards
                if c.side == side and c.location in ("frontline", "back")]

    def discard(self, side: str = LOCAL) -> list:
        return [c for c in self.cards if c.side == side and c.location == "discard"]

    def by_uid(self, uid: str) -> Optional[Card]:
        for c in self.cards:
            if c.uid == uid:
                return c
        return None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["taken_at"] = self.taken_at
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
# 构建指纹：三个同名的 kards-Win64-Shipping.exe 只有这一个与 idmap/dump 对应，
# 换构建必须重新核对偏移（判定方法见 reverse-data/reports/EXE-IDENTITY.md）。
#
MEM_BUILD = {
    "module": "kards-Win64-Shipping.exe",
    "image_size": 0x9CC8000,           # toolhelp 的 modBaseSize，不是文件大小
    "exe_size": 160489984,
    "md5": "395e470f06837f6e60ce5c53c6df2a22",
}

RVA_GWORLD = 0x08F625B0
RVA_GOBJECTS = 0x091FF4E0
RVA_GNAMES = 0x090E2E28

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
}
BGS_PTR = {"hq_left": 0x5E8, "hq_right": 0x5F8}
OFF_ALLCARDSINBATTLE = 0x538
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
}
CARD_I32 = {
    "card_id": 0x264, "attack_plain": 0x6C, "attack_buff_plain": 0x70,
    "defense_plain": 0x74, "kredits_plain": 0x80, "kredits_buff_plain": 0x84,
    "kredits_tax": 0x88,          # KreditsTax_AsEnemyTarget：被敌方指向时额外加费
    "operation_cost": 0xB0, "heavy_armor": 0x1DC, "heavy_armor_buff": 0x1E0,
    "movement_left": 0x268, "attack_left": 0x26C, "location_number": 0x278,
    # ★ 2026-09-21 实机：游戏会拦"部署当回合的行动"（提示原文
    #   「单位无法在部署的回合中移动或攻击。」），而 attack_left/movement_left
    #   对刚部署的单位仍然读到 1 —— 所以真正的判据是 enterPlayOnTurn 和当前回合数比较。
    "enter_play_turn": 0x270,
    "max_attack": 0x2A4, "max_defense": 0x2A8, "choose_one_index": 0x310,
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
    见 `reverse-data/tools/kardsmem/names.py` 与 `python -m kardsmem names`。

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
             ("has_deployment", "deployment")]

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
            if nm in (want, stem):
                return e.th32ProcessID
            ok = _k32.Process32Next(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return None


def _module_of(pid: int, image_name: str):
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        m = _MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(_MODULEENTRY32W)
        ok = _k32.Module32FirstW(snap, ctypes.byref(m))
        while ok:
            if m.szModule.lower() == image_name.lower():
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
        self._by_enum = dict(SIDE_ENUM)

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
            if size != MEM_BUILD["image_size"]:
                raise SourceUnavailable(
                    "模块 SizeOfImage 0x%X != 期望 0x%X —— 这份不是偏移对应的构建，"
                    "拒绝读数（偏移会用错）" % (size, MEM_BUILD["image_size"]))
            if path and os.path.exists(path):
                h = hashlib.md5()
                with open(path, "rb") as f:
                    for c in iter(lambda: f.read(1 << 20), b""):
                        h.update(c)
                got = h.hexdigest()
                self.info["md5"] = got
                if got != MEM_BUILD["md5"]:
                    raise SourceUnavailable("磁盘 md5 %s != 期望 %s" % (got, MEM_BUILD["md5"]))
                # exe_size：kardsmem 会校验它，这里也补上
                fsz = os.path.getsize(path)
                self.info["exe_size"] = fsz
                if fsz != MEM_BUILD["exe_size"]:
                    raise SourceUnavailable("磁盘文件大小 %d != 期望 %d —— 不是同一份构建"
                                            % (fsz, MEM_BUILD["exe_size"]))
        if self._m is None:
            self._m = _Mem(pid)
        self.base = base

    def describe(self) -> str:
        i = self.info or {}
        return "mem(pid=%s base=0x%X md5=%s)" % (i.get("pid"), i.get("base", 0), i.get("md5", "?"))

    def close(self):
        if self._m:
            self._m.close()
            self._m = None

    # -- 定位 ------------------------------------------------------------
    def _locate(self, m: _Mem):
        """GWorld -> UWorld -> GameState。返回 (world, gamestate, in_battle, notes)。"""
        notes = []
        world = m.ptr(self.base + RVA_GWORLD)
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
        by_enum, by_lr = side_maps(my_side)
        self._by_enum = by_enum
        st.my_side_raw = my_side
        if my_side is None:
            st.unknown.append("本地是哪一侧推不出（turn/startingSide/isLocalClientTurn 不全）"
                              "⇒ 暂按 side=1 是本地，两侧可能互换")
        if not gs:
            st.complete = False
            return st

        # kredits / slots：一条原子读，见 reports/KARDS-AUTOMATION.md §8「坑清单」
        blk = m.blob(gs + GS_SIDE_BLOCK_OFF, GS_SIDE_BLOCK_LEN)
        if blk:
            key = struct.unpack_from("<i", blk, OFF_AGS_KEY - GS_SIDE_BLOCK_OFF)[0]
            for (kind, lr), off in AGS_SIDES.items():
                X, Y, _Z, enc, _rnd = struct.unpack_from("<5i", blk, off - GS_SIDE_BLOCK_OFF)
                v = _decrypt(key, X, Y, enc)
                (st.kredits if kind == "kredit" else st.slots)[by_lr[lr]] = v
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
        st.match_finished = None if b["match_finished"] is None else bool(b["match_finished"])
        st.weather_played_this_turn = (None if b["weather_played_this_turn"] is None
                                       else bool(b["weather_played_this_turn"]))
        st.operation_kredits_spent = i["operation_kredits_spent"]
        st.slots_lost = {by_lr["left"]: i["slots_lost_left"],
                         by_lr["right"]: i["slots_lost_right"]}
        st.fatigue = {by_lr["left"]: i["fatigue_left"],
                      by_lr["right"]: i["fatigue_right"]}
        fo = b["frontline_owner"]
        st.frontline_owner = by_enum.get(fo) if fo is not None else None
        st.our_side = LOCAL

        # HQ 卡对象：权威句柄，单独拿
        hq_ptrs = {"hq_left": m.ptr(gs + BGS_PTR["hq_left"]),
                   "hq_right": m.ptr(gs + BGS_PTR["hq_right"])}

        cards = {}
        for ptr, mkey in self._enumerate_cards(m, gs):
            c = self._read_card(m, ptr)
            if c:
                c.raw["map_key"] = mkey
                cards[ptr] = c
        for raw_side, p in (("hq_left", hq_ptrs["hq_left"]), ("hq_right", hq_ptrs["hq_right"])):
            if p and p not in cards:
                c = self._read_card(m, p)
                if c:
                    cards[p] = c

        st.cards = sorted(cards.values(),
                          key=lambda c: (c.side or "", c.location or "", c.slot or 0, c.uid))
        for c in st.cards:
            if c.location == "hq" and c.defense is not None:
                st.hq[c.side] = c

        if not st.cards:
            st.unknown.append("一张卡都没枚举到")
        if st.hq[LOCAL] is None or st.hq[ENEMY] is None:
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

    def _read_card(self, m: _Mem, p: int) -> Optional[Card]:
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

        side = (self._by_enum.get(u8["side_enum"])
                if u8["side_enum"] is not None else None)
        loc = LOCATION_NAMES.get(u8["location_enum"]) if u8["location_enum"] is not None else None
        # 后排里 ETypeEnum::location(1) 的那张才是 HQ，其余是支援线单位
        if loc == "back" and u8["type_enum"] == 1:
            loc = "hq"
        atk = vals["attack"]
        can_act = None
        if i32["attack_left"] is not None and u8["has_attacked_this_turn"] is not None:
            can_act = bool(i32["attack_left"] > 0 and not u8["has_attacked_this_turn"])

        kw = [name for flag, name in _KEYWORDS if u8.get(flag)]
        if i32.get("heavy_armor"):
            kw.append("heavyarmor%d" % i32["heavy_armor"])

        return Card(
            uid="0x%X" % p,
            side=side, location=loc, slot=i32["location_number"],
            card_id=i32["card_id"], name=_read_card_name(m, p),
            card_type=CARD_TYPES.get(u8["type_enum"]) if u8["type_enum"] is not None else None,
            attack=atk, attack_buff=vals["attackBuff"], defense=vals["defense"],
            max_attack=i32["max_attack"], max_defense=i32["max_defense"],
            kredit_cost=(None if vals["kredit"] is None and vals["kreditBuff"] is None
                         else (vals["kredit"] or 0) + (vals["kreditBuff"] or 0)),
            operation_cost=i32["operation_cost"],
            kredits_tax_as_enemy_target=i32["kredits_tax"],
            is_suppressed=None if u8["is_suppressed"] is None else bool(u8["is_suppressed"]),
            is_revealed=None if u8["is_revealed"] is None else bool(u8["is_revealed"]),
            can_act=can_act,
            target_uid=("0x%X" % tgt) if tgt else None,
            needs_hand_target=(None if u8["needs_hand_target"] is None
                               else bool(u8["needs_hand_target"])),
            enter_play_on_turn=i32["enter_play_turn"],
            keywords=kw,
            raw={"ptr": p, "side_enum": u8["side_enum"], "location_enum": u8["location_enum"],
                 "type_enum": u8["type_enum"], "attack_plain": i32["attack_plain"],
                 "defense_plain": i32["defense_plain"], "kredits_plain": i32["kredits_plain"],
                 "choose_one_index": i32["choose_one_index"],
                 "move_left": i32["movement_left"], "attack_left": i32["attack_left"],
                 "enter_play_turn": i32["enter_play_turn"],
                 "heavy_armor": i32["heavy_armor"],
                 "all_keyword_flags": {k: u8[k] for k, _ in _KEYWORDS}},
        )


# --------------------------------------------------------------------------
# 四、ocr 后端：截图识别（非侵入式）
# --------------------------------------------------------------------------

OCR_UNSUPPORTED = [
    "turn（回合数：像素侧没有来源）",
    "kredits.enemy / slots.* / slots_lost.* / fatigue.*（没有 OCR 通路）",
    "card.attack / card.defense（没解析卡面数字，只认类型图标）",
    "card.is_suppressed（压制标记没实现）",
    "match_finished",
]


class OcrBoardSource(BoardSource):
    """截图识别后端（非侵入式）。复用本仓库 src/ 下已有的识别模块。

    调用链与 `main_loop` 一致：`win.set_dpi_aware()` → 找窗口 →
    `win.set_window_client_size(hwnd,1280,720)` → 载模板 → 逐帧识别。

    能力缺口见 `OCR_UNSUPPORTED`：这些字段恒为 None，也**不会**记进 `unknown`
    （`unknown` 是"该有但这次没读到"）；调用方必须自己兜底。

    慢：手牌识别要逐张悬停等扇形展开（`hand_scanner_v2.HOLD`=0.30s/张），
    所以默认不扫手牌（`scan_hand=False`）。

    前置标定（缺了不会崩，但对应字段会是 None）：
      - `config/hand_layout.json` —— `hand_calibrate.py` 交互生成
      - `config/frontline_line.json` —— `frontline_line.py action_box` 交互生成
    """

    name = "ocr"

    def __init__(self, hwnd: Optional[int] = None, scan_hand: bool = False,
                 force_client=(1280, 720), process: str = "kards"):
        self.hwnd = hwnd
        self.scan_hand = scan_hand
        self.force_client = force_client
        self.process = process
        self._templates = None
        self._states = None
        self._scanner = None

    # ---- 前置 ----------------------------------------------------------
    @staticmethod
    def project_root() -> str:
        """**上游** OCR-Kards-Auto 的根（模板图、config 的基线都在那儿）。

        ★ 2026-09-22：本文件原先就放在上游的 `src/` 里，`project_root()` 靠
          `dirname(dirname(__file__))` 自然得到上游根。现在它搬进了 `kards-agent/`，
          那个算法会算成仓库根 —— 所以改成显式定位上游树。
          上游是**别人的仓库（GPL-3.0）**，我们不改它一个字；
          自己加的状态/模板走 `kards-agent/config/` 的 overlay。
        """
        return str(AGENT_UPSTREAM)

    @classmethod
    def _src_dir(cls) -> str:
        d = os.path.join(cls.project_root(), "src")
        if d not in sys.path:
            sys.path.insert(0, d)
        return d

    def _load(self):
        self._src_dir()
        import win
        import ui_state

        win.set_dpi_aware()
        if not self.hwnd:
            wins = win.find_by_process(self.process)
            if not wins:
                raise SourceUnavailable("找不到进程名含 %r 的窗口" % self.process)
            self.hwnd = wins[0]["hwnd"]
        if self.force_client:
            win.set_window_client_size(self.hwnd, self.force_client[0], self.force_client[1])
        if self._templates is None:
            meta_path = os.path.join(self.project_root(), "config", "templates.json")
            meta = ui_state.load_meta(meta_path) if os.path.exists(meta_path) else {}
            meta.setdefault("templates", {}).update(_overlay_templates())
            self._templates = ui_state.load_templates(meta) if meta["templates"] else {}
        if self._states is None:
            st_path = os.path.join(self.project_root(), "config", "states.json")
            self._states = ui_state.load_states(st_path) if os.path.exists(st_path) else {}
            self._states.update(_overlay_states())
        return win, ui_state

    def available(self) -> bool:
        try:
            self._load()
            return True
        except Exception:
            return False

    def describe(self) -> str:
        return "ocr(hwnd=0x%X templates=%d)" % (self.hwnd or 0, len(self._templates or {}))

    # ---- 快照 ----------------------------------------------------------
    def snapshot(self) -> BoardState:
        win, ui_state = self._load()
        import board
        import kredits
        import frontline_line

        st = BoardState(source=self.name)
        st.unsupported = list(OCR_UNSUPPORTED)
        st.our_side = LOCAL

        frame = win.capture_client_bgr(self.hwnd)
        if frame is None:
            st.complete = False
            st.unknown.append("抓不到帧（窗口被遮挡/最小化？）")
            return st

        # 回合归属：End Turn 按钮模板（与 turn_engine.END_TURN_MIN_SCORE=0.60 一致）
        tpl = (self._templates or {}).get("end_turn_btn")
        if tpl is None:
            st.unsupported.append("our_turn（模板 end_turn_btn 缺失）")
        else:
            score, _box = ui_state.match_one(frame, tpl)
            st.our_turn = bool(score >= 0.60)

        # 我方指挥点（像素侧只有这一侧；槽位上限没有 OCR 通路）
        try:
            st.kredits[LOCAL] = kredits.read_kredits(frame)
        except Exception as exc:
            st.unknown.append("read_kredits 抛异常: %s" % exc)

        field = None
        try:
            field = board.read_field(frame, templates=self._templates)
        except Exception as exc:
            st.unknown.append("read_field 抛异常: %s" % exc)

        if field:
            try:
                owner = frontline_line.read_frontline_owner(frame, field=field)["owner"]
                st.frontline_owner = {"our": LOCAL, "enemy": ENEMY}.get(owner)
            except Exception:
                pass

            for side_key, side in (("our_support", LOCAL), ("enemy_support", ENEMY)):
                for i, u in enumerate(field.get(side_key) or []):
                    st.cards.append(self._ocr_unit(u, side, i, "back"))
            # ★ read_field 把中间那一行标成 `side='frontline'`（既不是 our 也不是 enemy）
            #   —— 像素侧在这里**分不出归属**，所以 side 留 None，别假装知道。
            #   调用方可以用 `can_act`（badge_state=='orange'）认出"这回合能动的那张"。
            for i, u in enumerate(field.get("frontline") or []):
                c = self._ocr_unit(u, None, i, "frontline")
                c.raw["side_pixel"] = u.get("side")
                st.cards.append(c)
            for side_key, side in (("hq_our", LOCAL), ("hq_enemy", ENEMY)):
                hq = field.get(side_key)
                if hq:
                    c = self._hq_card(hq, side)
                    st.cards.append(c)
                    st.hq[side] = c

        if st.hq[ENEMY] is None:
            try:
                hq = board.find_enemy_hq(frame, field=field, templates=self._templates)
                if hq:
                    c = self._hq_card(hq, ENEMY)
                    st.cards.append(c)
                    st.hq[ENEMY] = c
            except Exception:
                pass

        if self.scan_hand:
            st.cards.extend(self._scan_hand())

        for side in (LOCAL, ENEMY):
            if st.hq[side] is None:
                st.unknown.append("%s HQ 没识别到" % side)
        if st.kredits[LOCAL] is None:
            st.unknown.append("我方 kredits 读不出")
        if st.our_turn is None and not any("our_turn" in u for u in st.unsupported):
            st.unknown.append("回合归属读不出")
        if st.frontline_owner is None:
            st.unknown.append("前线归属读不出")
        st.unsupported = sorted(set(st.unsupported))
        st.complete = not st.unknown
        return st

    # ---- 单卡 ----------------------------------------------------------
    @staticmethod
    def _ocr_unit(u: dict, side: str, idx: int, location: str = "frontline") -> Card:
        # ★ 单位字典的坐标在**顶层**（x/y/w/h/cx/cy），不在嵌套的 'box' 里。
        #   实测：board.read_field()['enemy_support'][0] = {'x':514,'y':102,...,'cx':580,'cy':175,...}
        box = u.get("box") or {k: u.get(k) for k in ("x", "y", "w", "h", "cx", "cy")}
        # ★ 像素侧的 read_field 会把 HQ 也算进 our_support/enemy_support；
        #   不按 is_hq 分流的话 HQ 会被重复计入（一次 back、一次 hq）。
        if u.get("is_hq"):
            location = "hq"
        badge = u.get("badge") or {}
        state = u.get("state") or badge.get("state")
        return Card(
            uid="ocr:%s:%s:%s" % (side, box.get("cx"), idx),
            side=side,
            location=location,
            slot=box.get("cx"),
            card_type=u.get("type"),
            can_act=(state == "orange") if state else None,
            raw={"box": box, "badge_state": state, "src": u.get("src"),
                 "guard": u.get("guard")},
        )

    @staticmethod
    def _hq_card(hq: dict, side: str) -> Card:
        return Card(
            uid="ocr:hq:%s" % side,
            side=side, location="hq", slot=hq.get("cx"),
            card_type="location", defense=hq.get("hp"),
            raw={"box": {k: hq.get(k) for k in ("x", "y", "w", "h", "cx", "cy")},
                 "hp_state": hq.get("state"), "from": hq.get("from")},
        )

    def _scan_hand(self) -> list:
        """手牌：`HandScannerV2.scan_fast()` -> Card（location='hand'）。

        慢：每张牌要悬停等扇形展开。`card_type`/`kredit_cost` 来自 icons + OCR +
        dHash 指纹的复合判据，可靠性见 `hand_scanner_v2._classify_core`。
        """
        try:
            import hand_scanner_v2 as hs
            if self._scanner is None:
                self._scanner = hs.HandScannerV2(self.hwnd, self._templates or {})
            cards = self._scanner.scan_fast()
        except Exception:
            return []
        out = []
        for c in cards or []:
            out.append(Card(
                uid="ocr:hand:%s" % c.get("x"),
                side=LOCAL, location="hand", slot=c.get("x"),
                name=c.get("name"), card_type=c.get("type"),
                kredit_cost=c.get("cost"),
                raw={"unknown": c.get("unknown"), "info": c.get("info")},
            ))
        return out


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
        f.write(_SELFTEST_TARGET)
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
    chk("frontline_owner", st.frontline_owner, "local")
    chk("kredits.local", st.kredits[LOCAL], 12)
    chk("kredits.enemy", st.kredits[ENEMY], 10)
    chk("slots.local", st.slots[LOCAL], 12)
    chk("slots.enemy", st.slots[ENEMY], 11)
    chk("slots_lost.local", st.slots_lost[LOCAL], 1)
    chk("fatigue.enemy", st.fatigue[ENEMY], 3)
    chk("hq.local.defense", st.hq[LOCAL].defense if st.hq[LOCAL] else None, 12)
    chk("hq.enemy.defense", st.hq[ENEMY].defense if st.hq[ENEMY] else None, 2)
    chk("cards（空闲槽已跳过 + 重复指针已去重）", len(st.cards), 3)
    unit = [c for c in st.cards if c.card_id == 38]
    chk("unit 找得到且唯一", len(unit), 1)
    for c in unit:
        chk("attack", c.attack, 3)
        chk("attack_buff", c.attack_buff, 1)
        chk("total_attack()", c.total_attack(), 4)
        chk("defense", c.defense, 4)
        chk("kredit_cost", c.kredit_cost, 2)
        chk("location", c.location, "frontline")
        chk("side", c.side, "local")
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
    """prefer: 'mem' | 'ocr' | 'auto' | None（None = 读环境变量，默认 auto）。

    多余的关键字参数按后端各自的白名单过滤，调用方不用关心是哪个后端。
    """
    prefer = (prefer or os.environ.get("KARDS_BOARD_SOURCE") or "auto").lower()
    mem_kw = {k: v for k, v in kw.items() if k in ("pid", "validate_build", "base")}
    ocr_kw = {k: v for k, v in kw.items()
              if k in ("hwnd", "scan_hand", "force_client", "process")}
    if prefer == "mem":
        return MemoryBoardSource(**mem_kw)
    if prefer == "ocr":
        return OcrBoardSource(**ocr_kw)
    for cls in (OcrBoardSource, MemoryBoardSource):
        try:
            s = cls(**ocr_kw) if cls is OcrBoardSource else cls(**mem_kw)
            if s.available():
                return s
        except Exception:
            continue
    return MemoryBoardSource(**mem_kw)   # 让它在 snapshot() 里如实报错


# --------------------------------------------------------------------------
# 六、CLI
# --------------------------------------------------------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KARDS 盘面信息黑箱 API")
    ap.add_argument("--source", default=None, choices=[None, "mem", "ocr", "auto"])
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--watch", type=float, default=0.0, help="秒；>0 则循环采样")
    ap.add_argument("--cards", action="store_true", help="附带打印卡列表")
    ap.add_argument("--scan-hand", action="store_true",
                    help="ocr 后端：连手牌一起扫（每张悬停 0.3s，慢）")
    ap.add_argument("--selftest", action="store_true",
                    help="起合成目标进程自检，不需要游戏")
    args = ap.parse_args(argv)

    if args.selftest:
        return _run_selftest()

    src = open_source(args.source, scan_hand=args.scan_hand)
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
    print("turn=%s  our_turn=%s  our_side=%s  complete=%s" % (
        n(st.turn), n(st.our_turn), n(st.our_side), st.complete))
    print("kredits  local=%-4s enemy=%-4s   slots local=%-4s enemy=%-4s" % (
        n(st.kredits[LOCAL]), n(st.kredits[ENEMY]), n(st.slots[LOCAL]), n(st.slots[ENEMY])))
    print("slots_lost local=%-3s enemy=%-3s  fatigue local=%-3s enemy=%-3s" % (
        n(st.slots_lost[LOCAL]), n(st.slots_lost[ENEMY]),
        n(st.fatigue[LOCAL]), n(st.fatigue[ENEMY])))
    print("frontline_owner=%s  match_finished=%s  op_spent=%s" % (
        n(st.frontline_owner), n(st.match_finished), n(st.operation_kredits_spent)))
    for s in (LOCAL, ENEMY):
        hq = st.hq[s]
        print("%-5s HQ id=%-5s def=%-4s | hand=%-2d front=%-2d support=%-2d discard=%-2d" % (
            s, n(hq.card_id if hq else None), n(hq.defense if hq else None),
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
                (c.name or "-")[:26], c.side, c.location, c.card_type,
                "%s/%s" % (c.attack, c.defense), c.kredit_cost,
                ",".join(c.keywords),
                (" -> " + c.target_uid) if c.target_uid else ""))
    print("=" * 76)


if __name__ == "__main__":
    sys.exit(_main())
