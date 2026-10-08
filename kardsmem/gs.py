#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.gs —— GameState 层：指挥点/槽位的原子解密 + board_api 未展开的字段。

board_api 的 mem 后端已经读了 `BGS_U8 / BGS_I32 / BGS_PTR` 里的绝大多数字段，
本模块不重抄那些偏移，而是：
  1. **独立**提供侧值块（`gs+0x330..0x390`）的原子解密 —— 有的调用方只想拿指挥点，
     不必构造整个 BoardState；
  2. 补上 board_api **没有展开**的三块（本次整理新做的实测）：
     - `DeckCardIDs_Left/Right`（0x4A0 / 0x490）：**物理牌库**（允许重复），
       用来回答"同名牌有几张"这个 `AllCardsInBattle`（key=CardID，会塌缩）答不了的问题；
     - `AllStaticCardsSortedByName`（0x670）：2019 张静态卡表的容器形态；
     - `ReconnectHiddenEnemyKreditsByTurnNumber`（0x758）：重连用的敌方历史指挥点 TMap。
  3. 给一个 `snapshot()`（dict），供聚合层与 CLI 用。

只读。没有任何写路径。
"""

from __future__ import annotations

import struct
from typing import Optional

from . import build as B
from .gamemodel import ESide
from .proc import board_api
from .world import Locator

# board_api 已经定义好的偏移：直接复用，避免第二份定义
BGS_U8 = board_api.BGS_U8
BGS_I32 = board_api.BGS_I32
BGS_PTR = board_api.BGS_PTR
GS_SIDE_BLOCK_OFF = board_api.GS_SIDE_BLOCK_OFF
GS_SIDE_BLOCK_LEN = board_api.GS_SIDE_BLOCK_LEN
OFF_AGS_KEY = board_api.OFF_AGS_KEY
AGS_SIDES = board_api.AGS_SIDES
_decrypt = board_api._decrypt

# kardsmem 补充（board_api 未展开）
OFF_DECK_IDS_RIGHT = 0x490
OFF_DECK_IDS_LEFT = 0x4A0
OFF_STATIC_CARDS = 0x670
OFF_HIDDEN_KREDITS_MAP = 0x758
OFF_MAX_POSSIBLE_KREDITS = 0x338      # getMaxPossibleKredits 的真值（IDA 0x144B3C010）
TARRAY_NUM_OFF = 0x08
TARRAY_MAX_OFF = 0x0C


def _tarray(mem, addr: int):
    """读一个 TArray 头：`{ptr@0, num@+8, max@+0xC}`。"""
    p = mem.ptr(addr)
    return p, mem.i32(addr + TARRAY_NUM_OFF), mem.i32(addr + TARRAY_MAX_OFF)


class GameState:
    """包住一个 GameState 对象的只读视图。`kind` 见 `Locator.gamestate_kind()`。"""

    def __init__(self, session):
        self.s = session
        self.mem = session.m
        self.loc = Locator(session.m, session.base)
        self.addr = self.loc.gamestate or 0
        self.size = self.loc.gamestate_size

    # -- 身份 ------------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.addr)

    @property
    def in_battle(self) -> bool:
        """战斗 GameState 类已加载。**主菜单也为真** —— 不是「在对局中」。"""
        return self.size == B.SIZE_BP_GAMESTATE_BATTLE

    @property
    def match_active(self) -> bool:
        """真的在一局里：指挥点加密记录有效（X != 0）。

        主菜单下 in_battle 同样为真，但两侧 kredits 都是 None（X==0，游戏返回 -100）。
        判「对局中」一律用本属性，不要用 in_battle。
        """
        if not self.in_battle:
            return False
        k = self.kredits()
        return any(v is not None for v in k.values())

    @property
    def kind(self) -> str:
        return self.loc.gamestate_kind()

    def u8(self, off: int):
        return self.mem.u8(self.addr + off) if self.addr else None

    def i32(self, off: int):
        return self.mem.i32(self.addr + off) if self.addr else None

    def p(self, off: int) -> int:
        return (self.mem.ptr(self.addr + off) or 0) if self.addr else 0

    # -- 指挥点 / 槽位（★ 必须原子读） -----------------------------------
    def side_block(self) -> dict:
        """`gs+0x330..0x390` 一次原子读 + 解密。

        返回 {key, tamper_flag, kredits:{ESide:int|None}, slots:{ESide:int|None}, raw_ok}（键是座位 `ESide`，
        不再有 local/enemy；要“我方”的值用 `kredits()[gs.my_side]`）。
        任何一条解不出来就是 None —— **不猜**（旧报告里"上限每回合+1"就是因为猜）。
        """
        out = {"key": None, "tamper_flag": None, "kredits": {ESide.left: None, ESide.right: None},
               "slots": {ESide.left: None, ESide.right: None}, "raw_ok": False}
        if not self.addr:
            return out
        blk = self.mem.atomic(self.addr + GS_SIDE_BLOCK_OFF, GS_SIDE_BLOCK_LEN)
        if blk is None:
            return out
        out["raw_ok"] = True
        out["tamper_flag"] = self._i32_at(blk, 0x330 - GS_SIDE_BLOCK_OFF)
        out["key"] = self._i32_at(blk, OFF_AGS_KEY - GS_SIDE_BLOCK_OFF)
        for (kind, lr), off in AGS_SIDES.items():
            X, Y, _Z, enc, _frame = struct.unpack_from("<5i", blk, off - GS_SIDE_BLOCK_OFF)
            v = _decrypt(out["key"], X, Y, enc)
            (out["kredits"] if kind == "kredit" else out["slots"])[ESide.left if lr == "left" else ESide.right] = v
        return out

    # -- 指挥点上限（不是槽数！） ----------------------------------------
    @property
    def max_possible_kredits(self) -> Optional[int]:
        """`UBaseCardObject::getMaxPossibleKredits` 的真值：`int32@GS+0x338`（IDA 0x144B3C010）。

        ★ 2026-10-02（用户）：以前 effectvm 把这个原语映射成"槽数"，与 IDA 不符 —— 它读的是
        GameState 里的一个独立 int 字段。board_api 的快照也带同名属性（`max_possible_kredits`）。
        """
        return self.i32(OFF_MAX_POSSIBLE_KREDITS)

    # -- ★ 哪一边是本地玩家 ----------------------------------------------
    @property
    def my_side(self):
        """本地玩家的 ESideEnum（1=left / 2=right）。读不出返回 None。

        由 startingSide / isLocalClientTurn / currentTurn 推出 —— **每局可能不同**，
        1 不一定是本地。推导与踩过的坑见 board_api.read_my_side。
        """
        return board_api.read_my_side(self.mem, self.addr)

    @staticmethod
    def _i32_at(blk: bytes, off: int) -> Optional[int]:
        if off + 4 > len(blk):
            return None
        return struct.unpack_from("<i", blk, off)[0]

    def kredits(self) -> dict:
        return self.side_block()["kredits"]

    def slots(self) -> dict:
        return self.side_block()["slots"]

    # -- ★ 物理牌库（board_api 未展开） ---------------------------------
    def _seat(self, side: Optional[ESide]) -> ESide:
        """side 缺省 = 我方（`my_side`）；读不出 my_side 就抛 ValueError，**不默认 1**。"""
        if side is None:
            side = self.my_side
            if side is None:
                raise ValueError("本地座位读不出（mySide 为空）：不能按『我方』取牌库")
        return ESide(side)

    def deck_tarray_raw(self, side: Optional[ESide] = None) -> dict:
        """`DeckCardIDs_Left(0x4A0) / Right(0x490)` 的 TArray 头 + 前若干元素。

        AllCardsInBattle 的 key 是**运行时 CardID**（同名牌会塌缩），所以"牌库里
        还剩几张同名牌"只能从这里读。
        ★ 2026-09-21 实测确认：元素类型就是 **int32 的运行时 CardID**，
        每个 id 都能在 `AllCardsInBattle` 里找到对应卡（本局 22/22、23/23 全部命中）；
        `as_ptr` 只是留一份对照，不是别的解读。
        """
        off = OFF_DECK_IDS_LEFT if self._seat(side) == ESide.left else OFF_DECK_IDS_RIGHT
        if not self.addr:
            return {"off": off, "available": False}
        p, num, mx = _tarray(self.mem, self.addr + off)
        out = {"off": off, "ptr": p, "num": num, "max": mx, "available": bool(p)}
        if p and num and 0 < num <= 4096:
            b = self.mem.read_exact(p, num * 4)
            if b:
                out["as_i32"] = list(struct.unpack_from("<%di" % num, b))[:16]
            b8 = self.mem.read_exact(p, min(num, 16) * 8)
            if b8:
                out["as_ptr"] = ["0x%X" % v for v in
                                 struct.unpack_from("<%dQ" % min(num, 16), b8)]
        return out

    def deck_ids(self, side: Optional[ESide] = None) -> Optional[list]:
        """i32 解读下的整条牌库 id 列表（不确定元素类型时返回 None）。"""
        raw = self.deck_tarray_raw(side)
        p, num = raw.get("ptr"), raw.get("num")
        if not p or not num or not (0 < num <= 4096):
            return None
        b = self.mem.read_exact(p, num * 4)
        if not b:
            return None
        return list(struct.unpack_from("<%di" % num, b))

    # -- 静态卡表 --------------------------------------------------------
    def static_card_table(self) -> dict:
        p, num, mx = _tarray(self.mem, self.addr + OFF_STATIC_CARDS) if self.addr else (0, None, None)
        return {"off": OFF_STATIC_CARDS, "ptr": p, "num": num, "max": mx}

    # -- 重连用敌方历史指挥点 TMap ---------------------------------------
    def hidden_kredits_map(self) -> dict:
        """`ReconnectHiddenEnemyKreditsByTurnNumber`：TMap<int32,int32> 形态的只读探测。

        元素步长按 8（TMap<int32,int32> 的 TSet 元素 = pair 8 + hash 8 = 16 字节）
        与 16 两种解读各取前 12 个"看起来像 (turn, kredit)"的对，供人工判断。
        """
        addr = self.addr + OFF_HIDDEN_KREDITS_MAP
        data = self.mem.ptr(addr) if self.addr else 0
        num = self.mem.i32(addr + TARRAY_NUM_OFF) if self.addr else None
        out = {"off": OFF_HIDDEN_KREDITS_MAP, "ptr": data, "num": num, "pairs_stride16": []}
        if data and num and 0 < num <= 4096:
            b = self.mem.read_exact(data, min(num, 12) * 16)
            if b:
                for i in range(0, len(b) - 16 + 1, 16):
                    k = struct.unpack_from("<i", b, i)[0]
                    v = struct.unpack_from("<i", b, i + 4)[0]
                    if 0 < k < 100 and 0 <= v < 100:
                        out["pairs_stride16"].append((k, v))
        return out

    # -- 汇总 ------------------------------------------------------------
    def snapshot(self) -> dict:
        if not self.available:
            return {"available": False, "kind": None}
        b = {k: self.u8(o) for k, o in BGS_U8.items()}
        i = {k: self.i32(o) for k, o in BGS_I32.items()}
        d = {
            "available": True,
            "addr": hex(self.addr),
            "kind": self.kind,
            "in_battle": self.in_battle,
            "match_active": self.match_active,
            "my_side": None if self.my_side is None else int(self.my_side),
            "u8": b,
            "i32": i,
            "hq_ptr": {k: ("0x%X" % self.p(o) if self.p(o) else None)
                       for k, o in BGS_PTR.items()},
            "kredits": self.kredits(),
            "slots": self.slots(),
            "tamper_flag": self.side_block()["tamper_flag"],
        }
        if self.in_battle:
            d["deck_left"] = self.deck_tarray_raw(ESide.left)
            d["deck_right"] = self.deck_tarray_raw(ESide.right)
            d["static_cards"] = self.static_card_table()
            d["hidden_kredits"] = self.hidden_kredits_map()
        return d


def load(session=None) -> GameState:
    from .proc import attach
    return GameState(session or attach())


_STATIC_PROV_CACHE: dict = {}


def _card_fname(mem, pool, ptr: int) -> Optional[str]:
    """卡 data asset 的 `Name_0@0x50`（FName）→ 小写字符串。读不出 ⇒ None。"""
    b = mem.read_exact(ptr + 0x50, 8)
    if not b:
        return None
    idx, n = struct.unpack("<ii", b)
    if not idx:
        return None
    nm = pool.fname(idx, n)
    return nm.casefold() if nm else None


def make_static_card_provider(session):
    """**静态卡表提供者**（NATIVE-COVERAGE-1.60 §15.6）：

    `AllStaticCardsSortedByName`（`GameState+0x670`，2019 张，`TArray<UBaseCardObject*>`）
    → `{Name_0@0x50 小写: 指针}`；返回的 `static_card(name_lower)` 给出
    `cards.read_raw(..., light_effects=False)` 视图（按指针缓存）。

    用法：`cn = CardNatives(...); cn.static_card = make_static_card_provider(session);
    cn.static_cards = provider.names`（`GetStatic*`/`getHasVeteranUpgrade` 用）。
    读不到表（不在对局/未 attach）⇒ 返回**空提供者**（`.names = frozenset()`），
    调用方的 `_static()` 会如实地把它当"未命中"抛 Unimplemented —— 不编造默认卡。
    按 session.pid 缓存（2019 次名字读取只做一次）。
    """
    key = getattr(session, "pid", None) or id(session)
    hit = _STATIC_PROV_CACHE.get(key)
    if hit is not None:
        return hit
    table: dict = {}
    try:
        gs = GameState(session)
        mem = session.m
        p, num, _mx = _tarray(mem, gs.addr + OFF_STATIC_CARDS) if gs.addr else (0, None, None)
        if p and num and 0 < num <= 8192:
            pool = session.names_pool()
            for i in range(int(num)):
                cptr = mem.ptr(p + i * 8)
                if not cptr:
                    continue
                nm = _card_fname(mem, pool, cptr)
                if nm:
                    table.setdefault(nm, cptr)
    except Exception:                                         # noqa: BLE001
        table = {}

    dbghelp_cache: dict = {}

    def static_card(name_lower):
        cptr = table.get(str(name_lower).casefold())
        if not cptr:
            return None
        if cptr not in dbghelp_cache:
            from .cards import read_raw
            dbghelp_cache[cptr] = read_raw(session, cptr, light_effects=False)
        return dbghelp_cache[cptr]

    static_card.names = frozenset(table)
    static_card.ptr_of = lambda nm: table.get(str(nm).casefold())      # 静态卡对象指针（合成句柄的字段读取兜底用）
    static_card.table_size = len(table)
    static_card._session = session
    _STATIC_PROV_CACHE[key] = static_card
    return static_card


def main(argv=None) -> int:
    import argparse
    import json
    from .proc import attach
    ap = argparse.ArgumentParser(description="GameState 只读视图")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true", help="含牌库/静态表/重连表")
    a = ap.parse_args(argv)
    s = attach()
    g = GameState(s)
    if not g.available:
        print("读不到 GameState（GWorld 没建出来 / 不在对局）")
        return 2
    if a.full:
        d = g.snapshot()
    else:
        d = {"addr": hex(g.addr), "kind": g.kind, "in_battle": g.in_battle,
             "match_active": g.match_active,
             "kredits": g.kredits(), "slots": g.slots(),
             "turn": g.i32(BGS_I32["current_turn"]),
             "our_turn": g.u8(BGS_U8["is_local_client_turn"]),
             "match_finished": g.u8(BGS_U8["match_finished"])}
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        for k, v in d.items():
            if k in ("u8", "i32"):
                print("%s:" % k)
                for kk, vv in v.items():
                    print("   %-34s = %s" % (kk, vv))
            else:
                print("%-24s = %s" % (k, v))
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
