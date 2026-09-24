#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pickfind.py —— 在选择界面打开时，从内存里把"候选牌"找出来（不读图）。

思路：候选牌是真实的 UBaseCardObject 实例，游戏把它们挂在某个对象的 TArray 里。
在 ABP_Board_C / ABP_PlayerController_C / GameState 上扫 TArray 形态
(ptr@off, num@off+8, max@off+12)，元素是指向 UBaseCardObject（或其 BP 子类）的指针，打印卡名。
"""
import sys

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import board_api as BA  # noqa: E402

SIZE_CARD = 1656
OFF_UOBJECT_CLASS = 0x10
OFF_UCLASS_PROPSIZE = 0x58
OFF_UCLASS_SUPER = 0x40


def proplen(m, obj):
    if not obj:
        return None
    cls = m.ptr(obj + OFF_UOBJECT_CLASS)
    return m.i32(cls + OFF_UCLASS_PROPSIZE) if cls else None


def is_card(m, obj):
    """对象是不是 UBaseCardObject（沿 SuperStruct 链找 1656）。"""
    if not obj:
        return False
    cls = m.ptr(obj + OFF_UOBJECT_CLASS)
    seen = 0
    while cls and seen < 40:
        if m.i32(cls + OFF_UCLASS_PROPSIZE) == SIZE_CARD:
            return True
        cls = m.ptr(cls + OFF_UCLASS_SUPER)
        seen += 1
    return False


def scan_arrays(m, obj, size, label):
    hits = []
    for off in range(0, size - 16, 4):
        p = m.ptr(obj + off)
        if not p or p < 0x10000 or p > 0x7FFFFFFFFFFF:
            continue
        num = m.i32(obj + off + 8)
        mx = m.i32(obj + off + 12)
        if num is None or mx is None or not (1 <= num <= 32) or not (num <= mx <= 4096):
            continue
        cards = []
        ok = 0
        for i in range(num):
            e = m.ptr(p + i * 8)
            if e and is_card(m, e):
                ok += 1
                nm = BA._read_card_name(m, e)
                cid = m.i32(e + BA.CARD_I32["card_id"])
                cards.append((cid, nm, e))
        if ok == num and num >= 1:
            hits.append((off, num, cards))
    for off, num, cards in hits:
        print("  %s +0x%04X  n=%d" % (label, off, num))
        for cid, nm, e in cards:
            print("      id=%-6s 0x%X %s" % (cid, e, nm))
    return hits


def main():
    src = BA.open_source("mem")
    st = src.snapshot()
    m, base = src._m, src.base
    world = m.ptr(base + BA.RVA_GWORLD)
    gi = m.ptr(world + 0x228)
    lp_arr = m.ptr(gi + 0x38)
    lp = m.ptr(lp_arr) if lp_arr else 0
    pc = m.ptr(lp + 0x30) if lp else 0
    lvl = m.ptr(world + 0x30)
    arr = m.ptr(lvl + 0xA0)
    n = m.i32(lvl + 0xA8)
    board = 0
    if arr and n:
        for i in range(n):
            a = m.ptr(arr + i * 8)
            if a and proplen(m, a) in (0x0F68, 0x0F70):
                board = a
                break
    print("board=0x%X pc=0x%X" % (board, pc))
    print("--- 扫 ABP_Board_C (size 0x%X) ---" % (0x0F70))
    if board:
        scan_arrays(m, board, 0x0F70, "Board")
    print("--- 扫 PlayerController (size 0x%X) ---" % (2508 + 8,))
    if pc:
        scan_arrays(m, pc, 2508 + 8, "PC")


if __name__ == "__main__":
    main()
