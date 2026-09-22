#!/usr/bin/env python3
"""查 AllCardsInBattle 的 TMap key 语义：是 CardID 还是槽位索引？

决定"条目数"能不能当成"卡数"用。
"""
import collections
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import MemoryBoardSource  # noqa: E402

src = MemoryBoardSource()
st = src.snapshot()
src.close()

keys = [c.raw.get("map_key") for c in st.cards]
print("entries            :", len(keys))
print("distinct keys      :", len(set(keys)))
print("min / max key      :", min(keys), "/", max(keys))
print("first 24 keys      :", keys[:24])
print("sorted keys (all)  :", sorted(keys))

ids = [c.card_id for c in st.cards]
dup = [k for k, v in collections.Counter(ids).items() if v > 1]
print("distinct CardIDs   :", len(set(ids)), " duplicated CardIDs:", len(dup))
print("sample dups        :", sorted(dup)[:12])

print("by side/location   :")
for k, v in sorted(collections.Counter((c.side, c.location) for c in st.cards).items(),
                   key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
    print("   %-16s %d" % ("%s/%s" % k, v))

odd = [c for c in st.cards if c.side not in ("local", "enemy")]
print("侧别读不出的条目   :", len(odd))
for c in odd[:6]:
    print("   key=%-10s loc_enum=%-3s type_enum=%-3s id=%s def=%s" % (
        c.raw.get("map_key"), c.raw.get("location_enum"), c.raw.get("type_enum"),
        c.card_id, c.defense))

print("key -> (side, loc, CardID) for first 12 entries:")
for c in st.cards[:12]:
    print("   key=%-6s %-6s %-10s id=%s" % (c.raw.get("map_key"), c.side, c.location, c.card_id))
