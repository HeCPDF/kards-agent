# -*- coding: utf-8 -*-
"""learn/ 自检用的假牌工厂：产出真 `kardsmem.board.Card`（带原版 `BaseCardObject`）。

learn/ 层只许依赖 base（tests/test_arch_rules.py 的分层表），所以 learn 里的自检不 import kardsmem，
而是在各自 `if __name__ == "__main__":` 里从这里拿工厂，注入给 selftest。

座位是 `ESide`：本地玩家固定 `ME = ESide.left`，对方 `OPP = ESide.right`（与 `tests/_cards.py` 一致）。
`zone` 用 learn 的区域名：hand / frontline / back / hq / deck / discard。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kardsmem import gamemodel as GM                          # noqa: E402
from kardsmem.board import BoardState, Card                   # noqa: E402

ME = GM.ESide.left
OPP = GM.ESide.right

_LOC = {"frontline": lambda s: GM.ECardLocation.Board_Frontline,
        "back": lambda s: GM.SUPPORT_OF[s], "hq": lambda s: GM.SUPPORT_OF[s],
        "hand": lambda s: GM.HAND_OF[s], "deck": lambda s: GM.DECK_OF[s],
        "discard": lambda s: GM.ECardLocation.Discard}


def mk_card(card_id, side, zone, name=None, attack=3, defense=4, kredit_cost=2, slot=1,
            card_type="infantry", covert=False, is_revealed=False, card_seen=False,
            operation_cost=1, keywords=()):
    side = GM.ESide(int(side))
    typ = GM.EType.location if zone == "hq" else GM.EType[card_type]
    obj = GM.BaseCardObject(
        CardID=card_id, Type=typ, Location=_LOC[zone](side), side=side, locationNumber=slot,
        attack=attack, attackBuff=0, defense=defense, maxDefense=defense, kredits=kredit_cost, kreditsBuff=0,
        operationCost=operation_cost, operationCostBuff=0, isRevealed=is_revealed, hasCovert=covert,
        cardSeen=card_seen, title=name)
    for kw in keywords:
        setattr(obj, "has" + kw.capitalize(), True)
    return Card(uid="0x%X" % (0x1000 + (card_id or 0)), obj=obj)
