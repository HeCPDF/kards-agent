#!/usr/bin/env python3
"""纯内存的盘面位置图：每一行从左到右第几张是谁的。

用 location_enum 分组（真实的行），locationNumber 当行内序号（从左到右）。
不需要任何像素信息。
"""
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import ENEMY, LOCAL, MemoryBoardSource  # noqa: E402

ROW_NAME = {
    1: "Deck_Left", 2: "Deck_Right", 3: "Hand_Left", 4: "Hand_Right",
    5: "Board_HQLeft(我方后排)", 6: "Board_HQRight(敌方后排)",
    7: "Board_Frontline(前线)", 8: "Discard(弃牌堆)", 9: "Deck",
}

src = MemoryBoardSource()
st = src.snapshot()
src.close()

print("turn=%s our_turn=%s kredits=%s/%s slots=%s/%s" % (
    st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
    st.slots[LOCAL], st.slots["enemy"]))
print()

for side, tag in ((LOCAL, "本地 side=1"), (ENEMY, "敌方 side=2")):
    print("========== %s ==========" % tag)
    mine = [c for c in st.cards if c.side == side]
    for le in sorted({(c.raw or {}).get("location_enum") for c in mine},
                     key=lambda v: (v is None, v)):
        items = [c for c in mine if (c.raw or {}).get("location_enum") == le]
        items.sort(key=lambda c: (c.slot if c.slot is not None else -1, c.card_id or 0))
        cells = []
        for i, c in enumerate(items):
            hq = " (HQ)" if c.card_type == "location" else ""
            cells.append("#%s id=%s %s/%s%s" % (c.slot, c.card_id, c.attack, c.defense, hq))
        print("  %-22s n=%-2d  %s" % (ROW_NAME.get(le, le), len(items), " | ".join(cells)))
    print()

print("========== 前线（左右顺序 + 归属，纯内存） ==========")
for side in (LOCAL, ENEMY):
    f = sorted([c for c in st.cards if c.side == side and c.location == "frontline"],
               key=lambda c: (c.slot if c.slot is not None else -1))
    print("  %-6s n=%d  %s" % (side, len(f),
          " | ".join("#%s id=%s atk=%s def=%s can_act=%s" % (
              c.slot, c.card_id, c.attack, c.defense, c.can_act) for c in f)))
