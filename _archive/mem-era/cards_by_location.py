#!/usr/bin/env python3
"""按 location 分组列出所有卡，用于确认某张牌打完落到哪。"""
import collections
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import MemoryBoardSource  # noqa: E402

src = MemoryBoardSource()
st = src.snapshot()
src.close()

g = collections.defaultdict(list)
for c in st.cards:
    g[(c.side, c.location)].append(c)

for k in sorted(g, key=lambda k: (str(k[0]), str(k[1]))):
    items = g[k]
    print("%-16s n=%-3d ids=%s" % ("%s/%s" % k, len(items), [c.card_id for c in items]))

print()
print("所有 location == frontline 之外的、且带 locationNumber 的卡：")
for c in st.cards:
    if c.location in ("hq", "frontline"):
        print("  %-16s id=%-4s slot=%-4s atk=%-3s def=%-3s loc_enum=%s" % (
            "%s/%s" % (c.side, c.location), c.card_id, c.slot, c.attack, c.defense,
            c.raw.get("location_enum")))
