#!/usr/bin/env python3
"""验证 KreditsTax_AsEnemyTarget（+0x88）：谁身上带"被指向加费"。

预期：敌方那个步兵 id=44 为 1，其余为 0。
"""
import sys

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

src = MemoryBoardSource()
st = src.snapshot()
src.close()

print("turn=%s our_turn=%s kredits=%s/%s" % (
    st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"]))
print()
print("%-8s %-6s %-10s %-4s %-4s %-6s %s" % (
    "id", "side", "loc", "atk", "def", "tax", "type"))
for c in st.cards:
    if c.location in ("frontline", "back", "hq"):
        mark = "  <= 被指向 +%d" % c.kredits_tax_as_enemy_target \
            if (c.kredits_tax_as_enemy_target or 0) else ""
        print("%-8s %-6s %-10s %-4s %-4s %-6s %s%s" % (
            c.card_id, c.side, c.location, c.attack, c.defense,
            c.kredits_tax_as_enemy_target, c.card_type, mark))

taxed = [c for c in st.cards if c.kredits_tax_as_enemy_target]
print()
print("带税的单位：%s" % [(c.card_id, c.side, c.kredits_tax_as_enemy_target) for c in taxed])
print("手牌：%s" % [(c.card_id, c.kredit_cost, c.needs_hand_target) for c in st.hand(LOCAL)])
