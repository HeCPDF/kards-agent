# -*- coding: utf-8 -*-
"""`QueryMixin._free_front_slot`：前线是双方共用 5 位（受限时 2），不是"我方 4 格"（2026-10-08 实机：误报满 ⇒ move_up 自拒 4 次）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from _cards import ME, OPP, mk_card                                # noqa: E402
from kardsmem import board as BA                                    # noqa: E402
from ops.query import QueryMixin                                    # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


class Snap:
    my_side = ME

    def __init__(self, cards, limiters=None):
        self.cards, self.frontline_limiters = cards, limiters


class Src:
    def __init__(self, snap):
        self._s = snap

    def snapshot(self):
        return self._s


def run(cards, limiters=None):
    real = BA.open_source
    BA.open_source = lambda kind="mem": Src(Snap(cards, limiters))
    try:
        return QueryMixin()._free_front_slot()
    finally:
        BA.open_source = real


def front(cid, side, slot):
    c = mk_card(cid, side, "frontline", "infantry", 1, 1, 1, 1, "U%d" % cid)
    c.obj.locationNumber = slot
    return c


four = [front(1, ME, 0), front(2, ME, 1), front(3, OPP, 2), front(4, OPP, 3)]
chk("前线已有 4 张（含对方）⇒ 还有第 5 位（列号 4），不再误报满", run(four) == 4)
five = four + [front(5, OPP, 4)]
chk("前线 5 张 ⇒ 满，返回 None", run(five) is None)
chk("前线受限（FrontlineLimiters 非空，容量 2）且已有 2 张 ⇒ 满", run(four[:2], limiters=[7]) is None)
chk("空前线 ⇒ 列号 0", run([]) == 0)
chk("有空洞：占 0、2 ⇒ 取第一个空列号 1", run([front(1, ME, 0), front(2, OPP, 2)]) == 1)
print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
