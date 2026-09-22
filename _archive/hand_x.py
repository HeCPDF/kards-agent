#!/usr/bin/env python3
"""把手牌槽位绑到 x 坐标：用 board.card_boxes() 认出底部手牌框，
再和内存里的手牌列表（按 locationNumber 排序）并排打印。

目的：确认 locationNumber 与画面左右顺序一致，从而知道"想出的那张牌在哪个 x"。
"""
import sys

import agentpath  # noqa: F401  —— 接上 vendor/ 和本项目根

import board          # noqa: E402
import win            # noqa: E402
from board_api import MemoryBoardSource, LOCAL  # noqa: E402

win.set_dpi_aware()
wins = win.find_by_process("kards")
if not wins:
    print("没有 kards 窗口")
    raise SystemExit(1)
hwnd = wins[0]["hwnd"]
frame = win.capture_client_bgr(hwnd)
print("frame", None if frame is None else frame.shape)
if frame is None:
    raise SystemExit(1)

try:
    boxes = board.card_boxes(frame)
except TypeError:
    boxes = board.card_boxes(frame, debug=False)
boxes = boxes or []
print("card_boxes -> %d boxes" % len(boxes))

hand = [b for b in boxes if (b.get("cy") or 0) > board.HAND_MIN_Y]
hand.sort(key=lambda b: b["cx"])
print("HAND_MIN_Y = %s" % board.HAND_MIN_Y)
print("底部卡框 %d 个：" % len(hand))
for i, b in enumerate(hand):
    print("  x=%4d y=%4d w=%3d h=%3d  cx=%4d cy=%4d" % (
        b["x"], b["y"], b["w"], b["h"], b["cx"], b["cy"]))

src = MemoryBoardSource()
st = src.snapshot()
src.close()
print()
print("内存手牌（按 locationNumber 排序）：")
for c in st.hand(LOCAL):
    print("  slot=%-3s id=%-4s %-10s cost=%s" % (
        c.slot, c.card_id, c.card_type, c.kredit_cost))
