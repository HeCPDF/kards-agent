# -*- coding: utf-8 -*-
"""测试用假卡工厂：产出和线上一致的 `kardsmem.board.Card`（带原版 `BaseCardObject`）。

本地玩家固定是 `ESide.left`（`MY_SIDE`），对方是 `right`。位置按原版 `ECardLocation` 映射：
hand → Hand_*；frontline → Board_Frontline；back → Board_HQ*（支援线）；hq → Board_HQ* 且 Type=location。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kardsmem import gamemodel as GM                          # noqa: E402
from kardsmem.board import Card                               # noqa: E402

ME = GM.ESide.left            # 本地玩家的座位（测试里固定）
OPP = GM.ESide.right          # 对方座位
MY_SIDE = ME


def mk_card(card_id, side=ME, location="hand", card_type="infantry", attack=1, defense=1,
            kredit_cost=1, operation_cost=1, name="X", enter_play_on_turn=None, is_suppressed=False,
            is_being_guarded=False, max_defense=None, gotcha_activated=0, keywords=(), ptr=None, cipher=None,
            fname=None, is_revealed=None, attack_buff=0, kredit_buff=0, atk_range=None):
    # 座位用 ESide（ME/OPP）；兼容旧调用的 "local"/"enemy"
    es = GM.ESide(side) if isinstance(side, int) else (ME if side == "local" else OPP)
    if location == "hand":
        loc = GM.HAND_OF[es]
    elif location == "frontline":
        loc = GM.ECardLocation.Board_Frontline
    elif location in ("back", "hq"):
        loc = GM.SUPPORT_OF[es]
    else:
        raise ValueError(location)
    typ = GM.EType.location if location == "hq" else GM.EType[card_type]
    obj = GM.BaseCardObject(
        CardID=card_id, Type=typ, Location=loc, side=es, attack=attack, attackBuff=attack_buff, defense=defense,
        maxDefense=max_defense, kredits=kredit_cost, kreditsBuff=kredit_buff, operationCost=operation_cost,
        operationCostBuff=0, range=atk_range, enterPlayOnTurn=enter_play_on_turn, isSuppressed=is_suppressed,
        isBeingGuarded=is_being_guarded, gotchaActivated=gotcha_activated, title=name, cipher=cipher,
        isRevealed=is_revealed, ptr=ptr)
    for kw in keywords:                       # 关键词 → 原版 hasXxx 旗标（Card.keywords 由它们派生）
        if kw.startswith("heavyarmor"):
            obj.heavyArmor = int(kw[len("heavyarmor"):] or 1)
        else:
            setattr(obj, "has" + kw.capitalize(), True)
    return Card(uid="t%d" % card_id, obj=obj, fname=fname, raw=({"ptr": ptr} if ptr is not None else {}))
