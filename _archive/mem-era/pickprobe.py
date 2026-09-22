#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pickprobe.py —— 从内存判断"游戏是不是在等我选牌"。

链路（偏移来自 Dumper-7 本版本 SDK）：
  UWorld + 0x228                      = OwningGameInstance
  UGameInstance + 0x38                = LocalPlayers (TArray<ULocalPlayer*>)
  ULocalPlayer + 0x30                 = PlayerController
  UWorld + 0x30                       = PersistentLevel
  ULevel  + 0xA0                      = Actors (TArray<AActor*>)
  ABP_Board_C        : chooseOneActive@0x0AC9, isSelectingHandTarget@0x0C21   (类大小 0x0F70)
  ABP_PlayerController_C: ChooseOneSelection@0x9B4, ChooseOneCardTargets@0x9B8, chooseOneIndex@0x9C8
"""
import sys

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import board_api as BA  # noqa: E402

OFF_UWORLD_PERSISTENT = 0x30
OFF_UWORLD_GI = 0x228
OFF_LEVEL_ACTORS = 0xA0
OFF_GI_LOCALPLAYERS = 0x38
OFF_LP_PC = 0x30
OFF_UOBJECT_CLASS = 0x10
OFF_UCLASS_PROPSIZE = 0x58
SIZE_BOARD_CAND = (0x0F70, 0x0F68)   # ABP_Board_C（dump 说 0x0F70，实测见过 0x0F68）
PC_SEL, PC_TARGETS, PC_INDEX = 0x9B4, 0x9B8, 0x9C8
BOARD_CHOOSE_ACTIVE = 0x0AC9
BOARD_SELECTING_TARGET = 0x0C21


def proplen(m, obj):
    if not obj:
        return None
    cls = m.ptr(obj + OFF_UOBJECT_CLASS)
    return m.i32(cls + OFF_UCLASS_PROPSIZE) if cls else None


def find_board(m, world):
    lvl = m.ptr(world + OFF_UWORLD_PERSISTENT)
    if not lvl:
        return None, 0, []
    arr = m.ptr(lvl + OFF_LEVEL_ACTORS)
    num = m.i32(lvl + OFF_LEVEL_ACTORS + 8)
    hits, sizes = [], {}
    if not arr or not num or num > 20000:
        return None, num or 0, []
    for i in range(num):
        a = m.ptr(arr + i * 8)
        if not a:
            continue
        s = proplen(m, a)
        sizes[s] = sizes.get(s, 0) + 1
        if s in SIZE_BOARD_CAND:
            hits.append(a)
    return (hits[0] if hits else None), num, sorted(sizes.items(), key=lambda kv: -kv[1])


def main():
    src = BA.open_source("mem")
    st = src.snapshot()
    m, base = src._m, src.base

    world = m.ptr(base + BA.RVA_GWORLD)
    gi = m.ptr(world + OFF_UWORLD_GI)
    lp_arr = m.ptr(gi + OFF_GI_LOCALPLAYERS)
    lp = m.ptr(lp_arr) if lp_arr else 0
    pc = m.ptr(lp + OFF_LP_PC) if lp else 0

    board, nact, sizes = find_board(m, world)
    print("turn=%s our=%s k=%s fin=%s" % (st.turn, st.our_turn, st.kredits.get("local"), st.match_finished))
    print("PlayerController=0x%X propsize=%s" % (pc, proplen(m, pc)))
    print("Actors n=%s" % nact)
    print("ABP_Board_C      =0x%X  (期望 propsize∈%s)" % (board or 0, SIZE_BOARD_CAND))
    if board:
        print("  chooseOneActive      @0x0AC9 = %s" % m.u8(board + BOARD_CHOOSE_ACTIVE))
        print("  isSelectingHandTarget@0x0C21 = %s" % m.u8(board + BOARD_SELECTING_TARGET))
    if pc:
        print("  ChooseOneSelection   @0x9B4  = %s" % m.i32(pc + PC_SEL))
        tg = m.ptr(pc + PC_TARGETS)
        print("  ChooseOneCardTargets @0x9B8  = 0x%X num=%s" % (
            tg or 0, m.i32(pc + PC_TARGETS + 8) if tg else None))
        print("  chooseOneIndex       @0x9C8  = %s" % m.i32(pc + PC_INDEX))
    print("--- actor propsize 直方图 top10（帮助确认 Board 尺寸）---")
    for s, c in sizes[:10]:
        print("   size=%-6s x%d" % (s, c))


if __name__ == "__main__":
    main()
