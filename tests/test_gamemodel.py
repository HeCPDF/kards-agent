#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.gamemodel：枚举取值 / 字段名与 SDK 一致，FetchCardsByLocation 等函数照 BP。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kardsmem.gamemodel import (BaseCardObject, ECardLocation, EFaction, ESide, EType, GameState,     # noqa: E402
                                enum_or_none)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += (not ok)


def card(i, side, loc, typ=EType.infantry):
    return BaseCardObject(CardID=i, side=side, Location=loc, Type=typ)


def main():
    chk("ESide 取值（SDK：left=1 right=2）", (ESide.left, ESide.right) == (1, 2))
    chk("ECardLocation 取值（SDK：Hand_Left=3 … Board_Frontline=7 Discard=8）",
        [ECardLocation.Hand_Left, ECardLocation.Hand_Right, ECardLocation.Board_HQLeft, ECardLocation.Board_HQRight,
         ECardLocation.Board_Frontline, ECardLocation.Discard] == [3, 4, 5, 6, 7, 8])
    chk("EType 取值（location=1 order=2 tank=3 … infantry=6 artillery=7 wildcard=12）",
        (EType.location, EType.order, EType.tank, EType.infantry, EType.artillery, EType.wildcard) == (1, 2, 3, 6, 7, 12))
    chk("EFaction：Japan=3 Poland=8", (EFaction.Japan, EFaction.Poland) == (3, 8))
    chk("enum_or_none：None / 越界 ⇒ None", enum_or_none(EType, None) is None and enum_or_none(EType, 99) is None
        and enum_or_none(EType, 6) is EType.infantry)

    ids = iter(range(1, 100))
    cards = [card(next(ids), ESide.left, ECardLocation.Board_HQLeft, EType.location)]       # 左 HQ
    cards += [card(next(ids), ESide.left, ECardLocation.Board_HQLeft) for _ in range(3)]    # 左支援线 3 单位
    cards += [card(next(ids), ESide.right, ECardLocation.Board_Frontline) for _ in range(2)]
    cards += [card(next(ids), ESide.left, ECardLocation.Hand_Left) for _ in range(9)]
    g = GameState(mySide=ESide.left, FrontlineLimiters=[])
    for c in cards:
        g.AllCardsInBattle[c.CardID] = c
    r = g.FetchCardsByLocation(ECardLocation.Board_HQLeft, ESide.left)
    chk("FetchCardsByLocation：支援线含 HQ 共 4 张，未满（上限 5）", r["QtyInLocation"] == 4 and not r["isLocationFull"], str(r["QtyInLocation"]))
    g.AllCardsInBattle[99] = card(99, ESide.left, ECardLocation.Board_HQLeft)
    chk("支援线 5 张（HQ+4 单位）⇒ 满", g.FetchCardsByLocation(ECardLocation.Board_HQLeft, ESide.left)["isLocationFull"])
    chk("手牌 9 张 ⇒ 满", g.FetchCardsByLocation(ECardLocation.Hand_Left, ESide.left)["isLocationFull"])
    chk("前线 2 张、未受限 ⇒ 未满", not g.FetchCardsByLocation(ECardLocation.Board_Frontline)["isLocationFull"])
    g.FrontlineLimiters = [7]
    chk("IsFrontlineLimited：集合非空 ⇒ True；前线上限变 2 ⇒ 2 张即满",
        g.IsFrontlineLimited() is True and g.FetchCardsByLocation(ECardLocation.Board_Frontline)["isLocationFull"])
    g.FrontlineLimiters = None
    chk("FrontlineLimiters 读不出 ⇒ IsFrontlineLimited 为 None（不猜）", g.IsFrontlineLimited() is None)
    chk("is_mine：按 mySide 判", g.is_mine(cards[0]) is True and g.is_mine(cards[4]) is False)
    c = BaseCardObject(Type=EType.fighter, attack=2, attackBuff=1, kredits=3, kreditsBuff=-1, operationCost=1,
                       operationCostBuff=1, Location=ECardLocation.Board_Frontline)
    chk("BaseCardObject 同名函数：IsUnit/IsAirUnit/getTotalAttack/getTotalKredits/IsLocatedOnBoard",
        c.IsUnit() and c.IsAirUnit() and not c.IsGroundUnit() and c.getTotalAttack() == 3
        and c.getTotalKredits() == 2 and c.getTotalOperationCost() == 2 and c.IsLocatedOnBoard())
    chk("读不出的字段 ⇒ None（不编）", BaseCardObject().IsUnit() is None and BaseCardObject().getTotalAttack() is None)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
