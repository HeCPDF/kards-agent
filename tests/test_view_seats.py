# -*- coding: utf-8 -*-
"""agent/view.py 的座位迁移回归：短号/渲染/解析只看 `st.my_side`（ESide），不依赖 1 号座位，读不出就明确失败。

离线，不需要游戏。本地玩家分别放在 1 号 / 2 号座位各跑一遍，结论必须一致。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fake_cards import BoardState, mk_card                 # noqa: E402
from agent import view                                       # noqa: E402
from kardsmem.gamemodel import ESide                         # noqa: E402

fails = 0


def chk(name, ok):
    global fails
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    fails += 0 if ok else 1


def board(me):
    opp = ESide.right if me == ESide.left else ESide.left
    cards = [mk_card(1, me, "hq", "MYHQ", 0, 20, 0, 0), mk_card(2, opp, "hq", "THEIRHQ", 0, 20, 0, 0),
             mk_card(10, me, "hand", "H-ONE", 1, 1, 1, 0), mk_card(11, me, "hand", "H-TWO", 2, 2, 2, 1),
             mk_card(12, opp, "hand", "THEIR-HAND", 3, 3, 3, 0),
             mk_card(20, me, "frontline", "MY-FRONT", 3, 3, 3, 0), mk_card(21, me, "back", "MY-BACK", 2, 2, 2, 1),
             mk_card(30, opp, "frontline", "OPP-FRONT", 4, 4, 4, 0), mk_card(31, opp, "deck", "DECK", 1, 1, 1, 0)]
    return BoardState(source="t", cards=cards, my_side=me, turn=3, our_turn=True,
                      kredits={me: 5, opp: 2}, slots={me: 3, opp: 3}, hq={me: cards[0], opp: cards[1]})


for me in (ESide.left, ESide.right):
    tag = "我方=%d号座位" % int(me)
    st = board(me)
    h = view.Handles(st)
    chk(tag + "：h1/h2 是我方手牌", h.by_handle["h1"].card_id == 10 and h.by_handle["h2"].card_id == 11)
    chk(tag + "：H1 是对方手牌", h.by_handle["H1"].card_id == 12)
    chk(tag + "：m1/m2 是我方前线+后排，e1 是对方前线",
        h.by_handle["m1"].card_id == 20 and h.by_handle["m2"].card_id == 21 and h.by_handle["e1"].card_id == 30)
    chk(tag + "：hq / ehq", h.by_handle["hq"].card_id == 1 and h.by_handle["ehq"].card_id == 2)
    txt = view.render_board(st, h)
    chk(tag + "：渲染含我方/敌方单位，不含牌库牌", "MY-FRONT" in txt and "OPP-FRONT" in txt and "DECK" not in txt)
    chk(tag + "：指挥点 我 5 / 敌 2", "指挥点 我 5 / 敌 2" in txt)
    chk(tag + "：牌库厚度 敌 1", "牌库厚度：我 0   敌 1" in txt)
    chk(tag + "：#10 优先解析到我方手牌", view.resolve(st, "#10", h).card_id == 10)
    chk(tag + "：side_zh 现算", view.side_zh(st, st.my_side) == "我方" and view.side_zh(st, st.other_side) == "敌方")
    chk(tag + "：parse_side me/opp/left/right/1/2",
        view.parse_side("me", st.my_side) == me and view.parse_side("opp", st.my_side) == st.other_side
        and view.parse_side("left", st.my_side) == ESide.left and view.parse_side("2", None) == ESide.right)

st = board(ESide.left)
st.my_side = None
try:
    view.Handles(st)
    chk("my_side 读不出 => Handles 抛 ValueError", False)
except ValueError:
    chk("my_side 读不出 => Handles 抛 ValueError", True)
chk("my_side 读不出 => render_board 明确说读不出（不渲染）", "mySide 读不出" in view.render_board(st))
try:
    view.parse_side("me", None)
    chk("my_side 读不出 => parse_side('me') 抛 ValueError", False)
except ValueError:
    chk("my_side 读不出 => parse_side('me') 抛 ValueError", True)

try:
    board(ESide.left).cards[0].location
    chk("card.location 访问抛 RuntimeError", False)
except RuntimeError:
    chk("card.location 访问抛 RuntimeError", True)

print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
sys.exit(1 if fails else 0)
