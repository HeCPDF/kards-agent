# -*- coding: utf-8 -*-
"""kardsmem.cardnatives 的合成卡自检（P6 拆文件，原样搬出自 cardnatives.py 末尾，零逻辑改动）。

入口仍是 `kardsmem.cardnatives.selftest()` / `python -m kardsmem.cardnatives`。
"""
from __future__ import annotations

from .cardnatives import CardNatives
from .gamemodel import ESide
from .kismetlib import Unimplemented


# --------------------------------------------------------------------------
# 自检：合成一张卡，把**字段映射**钉住。
# 这里不测"游戏规则对不对"（那要实机），只测"我有没有把字段接错"——
# 接错的典型后果是把 hq 当成不在场上、把 enemy 当成 local，全是静默的错答案。
# --------------------------------------------------------------------------
class _Board:
    def __init__(self, our_turn=True):
        self.our_turn = our_turn


_CARD = {
    "card_type": "infantry", "location": "frontline", "side": ESide.left,
    "attack": 3, "defense": 4, "kredit_cost": 2, "operation_cost": 1,
    "is_suppressed": False, "is_revealed": False, "side_enum": 1,
    "keywords": ["has_blitz", "has_covert"],
    # 复核后原语要的字段（read_raw 默认会给；合成卡要自己补）
    "custom_name1": "", "custom_name2": "", "operation_cost_buff": 0, "received_abilities": [],
    "gameplay_tags": [], "pinned_turns": 0, "custom_json_keys": [], "custom_json_nums": {},
}


def selftest() -> int:
    cn = CardNatives(_Board(our_turn=True), my_seat=ESide.left)     # 合成卡的 side=left，本地占 left
    c = dict(_CARD)
    cases = [
        ("IsUnit", ("IsUnit", c), True),
        ("IsInfantry", ("IsInfantry", c), True),
        ("IsTank 否", ("IsTank", c), False),
        ("IsGroundUnit", ("IsGroundUnit", c), True),
        ("IsAirUnit 否", ("IsAirUnit", c), False),
        ("IsLocatedOnBoard(frontline)", ("IsLocatedOnBoard", c), True),
        ("IsLocatedInHand 否", ("IsLocatedInHand", c), False),
        ("IsOwnedByClientSide", ("IsOwnedByClientSide", c), True),
        ("GetOppositeSide 返回座位号", ("GetOppositeSide", c), 2),
        ("getTotalAttack", ("getTotalAttack", c), 3),
        ("getHasBlitz", ("getHasBlitz", c), True),
        ("getHasGuard 否", ("getHasGuard", c), False),
        ("未揭示的隐蔽牌", ("IsUnrevealedCovertCard", c), True),
        ("HasCampaignUpgrade(非战役, 空数组)", ("HasCampaignUpgrade", c), False),
    ]
    bad = 0
    for name, (fn, card), want in cases:
        got = cn.call(fn, card, *((1,) if fn == "HasCampaignUpgrade" else ()))
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] %-28s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))

    # ★ hq 和 back 也算"在场上"：Board_HQLeft/Right(5/6) 是整个后排，不是只有总部
    for loc, want in (("hq", True), ("back", True), ("deck", False), ("discard", False)):
        got = cn.call("IsLocatedOnBoard", dict(c, location=loc))
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] IsLocatedOnBoard(%-9s) got=%r want=%r"
              % ("PASS" if ok else "FAIL", loc, got, want))

    # 轮次相关：必须跟着 BoardState 走，不能从卡对象猜
    for ot, side, want in ((True, 1, True), (True, 2, False),
                           (False, 1, False), (False, 2, True)):
        got = CardNatives(_Board(ot), my_seat=1).call("IsSideActive", c, side)
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] IsSideActive(our_turn=%-5s,%-5s) got=%r want=%r"
              % ("PASS" if ok else "FAIL", ot, side, got, want))

    # 缺字段必须**抛**，不能当 False
    for name, card, why in (
            ("IsUnit", {"card_type": None}, "card_type 缺"),
            ("getHasBlitz", {"keywords": None}, "keywords 缺"),
            ("IsSideActive", c, "没有 BoardState")):
        try:
            (cn if why != "没有 BoardState" else CardNatives(None)).call(
                name, card, *((1,) if name == "IsSideActive" else ()))
            print("  [FAIL] %s 没抛（%s）" % (name, why))
            bad += 1
        except Unimplemented:
            print("  [PASS] %s 缺字段时抛 Unimplemented（%s）" % (name, why))

    print("cardnatives selftest: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0
