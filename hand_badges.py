#!/usr/bin/env python3
"""看 unit_state.find_cost_badges() 能不能找到**手牌**的费用于徽章。

如果能，就用它给出手牌槽位的客户区 x —— 这样就不必猜/标定手牌扇形位置。
"""
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import unit_state  # noqa: E402
import win         # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

win.set_dpi_aware()
wins = win.find_by_process("kards")
hwnd = wins[0]["hwnd"]
win.bring_to_front(hwnd)
frame = win.capture_client_bgr(hwnd)
print("frame %s" % (frame.shape,))

badges = unit_state.find_cost_badges(frame)
print("find_cost_badges -> %d" % len(badges))
for b in badges:
    print("  cx=%-5s cy=%-5s w=%-3s h=%-3s state=%-7s from=%s" % (
        b.get("cx"), b.get("cy"), b.get("w"), b.get("h"), b.get("state"), b.get("from")))

hand = [b for b in badges if (b.get("cy") or 0) > 560]
hand.sort(key=lambda b: b["cx"])
print()
print("手牌带(cy>560) 的徽章 cx：%s" % [b["cx"] for b in hand])

s = MemoryBoardSource()
st = s.snapshot()
s.close()
print("内存手牌（locationNumber 升序）：%s" % [
    (c.slot, c.card_id, c.kredit_cost) for c in st.hand(LOCAL)])
