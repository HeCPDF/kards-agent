#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""effectvm 的 `GetAllCardsOnBoard` 钩子回归（离线）。

BP_CardFunctions@457：只要 IsLocatedOnBoard + covert 过滤，**无防御/side/IsUnit 判据**
（`card_event_echelon` 用它给全场单位挂 trigger；以前整条停在"函数 GetAllCardsOnBoard"）。
对照：`GetAllUnitsOnBoard` 要 防御>0 ∧ IsUnit。
"""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import semantics.effectvm as EV                                   # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def card(ptr, side, loc, defense, ctype="infantry", kws=(), revealed=True):
    return NS(raw={"ptr": ptr}, side=side, location=loc, defense=defense, card_type=ctype,
              keywords=list(kws), is_revealed=revealed)


def main():
    cs = [card(1, "local", "frontline", 3), card(2, "enemy", "back", 2),
          card(3, "enemy", "hq", 20, "hq"), card(4, "local", "back", 0),
          card(5, "local", "hand", 2), card(6, "enemy", "back", 2, kws=["covert"], revealed=False)]
    st = NS(cards=cs, turn=3, my_side_raw=1, slots={}, kredits={})
    hooks = EV.make_read_hooks(st, 1)
    vm = NS(s=None)
    got = hooks["GetAllCardsOnBoard"](vm, NS(), None, [True], NS(kids=[]))
    chk("含 HQ、含防御=0、双方都有；不含手牌", sorted(got) == [1, 2, 3, 4, 6], str(got))
    got = hooks["GetAllCardsOnBoard"](vm, NS(), None, [False], NS(kids=[]))
    chk("includeCovert=False ⇒ 去掉未揭示的隐蔽牌", sorted(got) == [1, 2, 3, 4], str(got))
    got = hooks["GetAllUnitsOnBoard"](vm, NS(), None, [True], NS(kids=[]))
    chk("对照：GetAllUnitsOnBoard 要防御>0 ∧ 单位（无 HQ/无 0 防御）", sorted(got) == [1, 2, 6], str(got))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
