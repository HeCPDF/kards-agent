#!/usr/bin/env python3
"""两个后端并排：mem 给真值（身份/数值），ocr 给屏幕坐标（框）。

演示黑箱 API 的用法 —— 调用方拿同一套 BoardState，坐标只能从 ocr 来。
"""
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import LOCAL, MemoryBoardSource, OcrBoardSource  # noqa: E402

print("========== OCR（像素，带框） ==========")
o = OcrBoardSource()
if not o.available():
    print("ocr 不可用")
else:
    sto = o.snapshot()
    print("our_turn=%s kredits.local=%s hq=%s/%s" % (
        sto.our_turn, sto.kredits[LOCAL],
        sto.hq[LOCAL].defense if sto.hq[LOCAL] else None,
        sto.hq["enemy"].defense if sto.hq["enemy"] else None))
    for c in sto.cards:
        print("  %-6s %-10s type=%-10s can_act=%-5s box=%s" % (
            c.side, c.location, c.card_type, c.can_act, c.raw.get("box")))

print()
print("========== MEM（真值） ==========")
m = MemoryBoardSource()
stm = m.snapshot()
m.close()
print("turn=%s our_turn=%s kredits=%s/%s slots=%s/%s" % (
    stm.turn, stm.our_turn, stm.kredits[LOCAL], stm.kredits["enemy"],
    stm.slots[LOCAL], stm.slots["enemy"]))
for c in stm.cards:
    if c.location in ("frontline", "back", "hq"):
        print("  %-6s %-10s id=%-4s atk=%-3s def=%-3s slot=%-4s target=%s" % (
            c.side, c.location, c.card_id, c.attack, c.defense, c.slot, c.target_uid))
print("手牌：%s" % [(c.card_id, c.card_type, c.kredit_cost, c.needs_hand_target)
                    for c in stm.hand(LOCAL)])
