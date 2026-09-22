#!/usr/bin/env python3
"""打印我方手牌细节：够不够费、要不要选目标、类型。"""
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import MemoryBoardSource, LOCAL  # noqa: E402

src = MemoryBoardSource()
st = src.snapshot()
src.close()

print("turn=%s our_turn=%s kredits=%s/%s slots=%s/%s" % (
    st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
    st.slots[LOCAL], st.slots["enemy"]))
print("complete=%s unknown=%s" % (st.complete, st.unknown))
print()
print("我方手牌：")
for i, c in enumerate(st.hand(LOCAL)):
    afford = "" if c.kredit_cost is None else ("出得起" if c.kredit_cost <= (st.kredits[LOCAL] or 0) else "出不起")
    print("  [%d] id=%-4s %-10s cost=%-3s atk=%-3s def=%-3s 需要选目标=%-5s kw=%-22s %s" % (
        i, c.card_id, c.card_type, c.kredit_cost, c.attack, c.defense,
        c.needs_hand_target, ",".join(c.keywords), afford))
print()
print("我方阵线：", [(c.card_id, c.attack, c.defense, c.can_act) for c in st.board(LOCAL)])
print("敌方阵线：", [(c.card_id, c.attack, c.defense) for c in st.board("enemy")])
print("我方 HQ：id=%s def=%s" % (st.hq[LOCAL].card_id, st.hq[LOCAL].defense))
print("敌方 HQ：id=%s def=%s" % (st.hq["enemy"].card_id, st.hq["enemy"].defense))
