#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_seat_cache.py —— **离线**回归：跨对局的座位/Logic 缓存必须失效。

为什么需要它（2026-10-02，C/D 两连局实机）：

* C 局一切正常；D 局（同一常驻进程、同一个 `kardsmem.Session`）**整局 0 出牌** ——
  87 个动作全被拒，清一色 `card X 不在我方手牌`。规则侧走 board_api 的回合奇偶
  那条读法能看见 9 张手牌，注入侧 `hand_actor()` 却一张都找不到 ⇒ 两边视图分叉。
* 根因是"一局内不会变"的缓存没有跨局失效：
  1. `kardsmem/cards.py::_my_seat` 把座位缓存到 **session 对象**上，写一次永不失效；
  2. `kardsmem/matchlog.py::forget()` 漏清 `_my_side_cache`；
  3. `MatchLog._logic()/_my_side_cache` 命中时不验新鲜度（长寿命 MatchLog 跨局复用）。

这些用例全部**离线**（伪造内存/对象），不需要游戏在跑。跑法：
    python test_seat_cache.py          # 在 kards-agent/ 下
"""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys



from kardsmem import matchlog as ML                      # noqa: E402
from kardsmem import cards as C                          # noqa: E402

FAILS = 0


def chk(name, got, want):
    global FAILS
    ok = (got == want)
    if not ok:
        FAILS += 1
    print("  [%s] %-62s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


class FakeMem:
    """`MatchLog` 只需要 ptr/i32/u8 三个读原语。"""

    def __init__(self):
        self.seat = 1         # `Logic.mySide` 的值
        self.logic_ptr = 0x5000

    def ptr(self, addr):
        return self.logic_ptr

    def i32(self, addr):
        return 0

    def u8(self, addr):
        return self.seat

    def atomic(self, addr, n):
        return None


class FakeSession:
    def __init__(self):
        self.m = FakeMem()
        self.base = 0


class FakeObjectArray:
    """`locate()` 重扫时用的空对象表（扫不到任何东西 ⇒ 返回 False，不缓存失败）。"""

    def __init__(self, session):
        self.s = session

    def pool(self):
        class _Pool:
            def fname_of(self, cls):
                return ""
        return _Pool()

    def class_of(self, ptr):
        return 0

    def iter_objects(self):
        return iter(())


class FakeLocator:
    def __init__(self, m, base):
        pass

    def actors(self):
        return []


def new_ml():
    s = FakeSession()
    ml = ML.MatchLog(s)
    ml._obj = 0x1000
    ml._cls = 0x2000
    ml._off = {"all": 0x30}
    return s, ml


def main():
    print("== A. MatchLog.forget() 必须把 mySide/Logic 两个缓存一起清掉 ==")
    s, ml = new_ml()
    ml._my_side_cache = 2
    ml._logic_cache = (0x5000, 0x9000)
    ml.forget()
    chk("forget() 清 _my_side_cache", ml._my_side_cache, None)
    chk("forget() 清 _logic_cache", ml._logic_cache, None)

    print("== A2. pin() 换对象（权威链说换局）也要清 mySide/Logic 缓存 ==")
    import kardsmem.objects as O2
    old_oa2 = O2.ObjectArray
    try:
        class OA2:
            def __init__(self, sess):
                pass

            def class_of(self, ptr):
                return 0x2000
        O2.ObjectArray = OA2
        s, ml = new_ml()
        ml._my_side_cache = 1
        ml._logic_cache = (0x5000, 0x9000)
        ml.pin(0x2000)
        chk("pin() 清 _my_side_cache", ml._my_side_cache, None)
        chk("pin() 清 _logic_cache", ml._logic_cache, None)
        chk("pin() 换到新对象", ml.obj, 0x2000)
    finally:
        O2.ObjectArray = old_oa2

    print("== B. my_side() 同一局内缓存、换局后必须重读 ==")
    s, ml = new_ml()
    stale = {"v": False}
    ml.is_stale = lambda: stale["v"]                    # 假新鲜度判据
    ml._logic_field = lambda name: ({"offset": 0x0}, 0x3000)
    s.m.seat = 1
    chk("第一局读到 1", ml.my_side(), 1)
    s.m.seat = 2
    chk("同局内仍用缓存（不误伤一局内稳定性）", ml.my_side(), 1)
    stale["v"] = True                                   # 换局
    chk("换局后重读，拿到新座位 2", ml.my_side(), 2)
    stale["v"] = False
    s.m.seat = 1
    chk("第二局内继续用缓存（2）", ml.my_side(), 2)
    stale["v"] = True
    chk("第三局再换座位 ⇒ 重读 1", ml.my_side(), 1)

    print("== C. _logic() 命中缓存前也验新鲜度（换局必须换 Logic 指针） ==")
    import kardsmem.objects as O
    import kardsmem.props as P
    old_fp, old_oa = P.find_prop, O.ObjectArray
    try:
        P.find_prop = lambda sess, cls, name: {"offset": 0x10}

        class OA:
            def __init__(self, sess):
                pass

            def class_of(self, ptr):
                return 0x9000 + ptr
        O.ObjectArray = OA
        s, ml = new_ml()
        stale = {"v": False}
        ml.is_stale = lambda: stale["v"]

        def fake_locate(*a, **k):
            """真 locate() 重扫后会重新填 _obj/_cls —— 这里模拟这条。"""
            ml._obj = 0x1000
            ml._cls = 0x2000
            return True
        ml.locate = fake_locate
        s.m.logic_ptr = 0x5000
        chk("第一次读 Logic", ml._logic(), (0x5000, 0x9000 + 0x5000))
        s.m.logic_ptr = 0x6000
        chk("同局内仍用缓存", ml._logic(), (0x5000, 0x9000 + 0x5000))
        stale["v"] = True
        chk("换局后重读 Logic", ml._logic(), (0x6000, 0x9000 + 0x6000))
    finally:
        P.find_prop, O.ObjectArray = old_fp, old_oa

    print("== D. locate() 不会把上一局的 OnlineMatch 当结论（stale ⇒ 丢掉重找） ==")
    import kardsmem.objects as O
    import kardsmem.world as W
    old_oa, old_loc = O.ObjectArray, W.Locator
    try:
        O.ObjectArray = FakeObjectArray
        W.Locator = FakeLocator
        s, ml = new_ml()
        ml._last_count = 5
        ml.is_stale = lambda: True
        got = ml.locate()
        chk("stale 时 locate() 重扫（扫不到 ⇒ False）", got, False)
        chk("旧对象指针已丢弃", ml._obj, None)
    finally:
        O.ObjectArray, W.Locator = old_oa, old_loc

    print("== E. cards._my_seat 不再把座位钉在 session 上（跨局跟着 MatchLog 走） ==")
    seat = {"v": 1}
    stub = {"made": 0}

    class StubMatchLog:
        def __init__(self, session):
            stub["made"] += 1

        def my_side(self):
            return seat["v"]

    old_ml = ML.MatchLog
    try:
        ML.MatchLog = StubMatchLog
        sess = FakeSession()
        chk("第一局座位 1", C._my_seat(sess), 1)
        seat["v"] = 2
        chk("换局后立刻拿到新座位 2（不沿用 session 缓存）", C._my_seat(sess), 2)
        chk("session 上不再写 _my_seat_cache", hasattr(sess, "_my_seat_cache"), False)
        chk("每 session 只建一个 MatchLog", stub["made"], 1)
    finally:
        ML.MatchLog = old_ml

    print("== F. 座位是绝对的 ESide；『我方』= my_side(session)（随 MatchLog 走），读不出抛 ValueError ==")
    from kardsmem.gamemodel import ESide
    try:
        ML.MatchLog = StubMatchLog
        seat["v"] = 2
        sess = FakeSession()
        chk("side_enum=2 ⇒ ESide.right（与谁是本地无关）", C._seat_of(2), ESide.right)
        chk("side_enum=1 ⇒ ESide.left", C._seat_of(1), ESide.left)
        chk("side_enum=0 ⇒ None", C._seat_of(0), None)
        chk("座位 2：my_side(session) = ESide.right", C.my_side(sess), ESide.right)
        seat["v"] = 1
        sess2 = FakeSession()
        chk("座位 1：my_side(session) = ESide.left", C.my_side(sess2), ESide.left)
        seat["v"] = None
        sess3 = FakeSession()
        try:
            C.my_side(sess3)
            chk("mySide 读不出 ⇒ my_side() 抛 ValueError", "没抛", "抛")
        except ValueError:
            chk("mySide 读不出 ⇒ my_side() 抛 ValueError", True, True)
        try:
            C.hand(sess3)
            chk("mySide 读不出 ⇒ hand() 缺省座位抛 ValueError", "没抛", "抛")
        except ValueError:
            chk("mySide 读不出 ⇒ hand() 缺省座位抛 ValueError", True, True)
        chk("命令行 left/right 不需要 mySide", C.parse_side(sess3, "right"), ESide.right)
        seat["v"] = 2
        chk("命令行 me/opp 入口处现算", (C.parse_side(FakeSession(), "me"), C.parse_side(FakeSession(), "opp")),
            (ESide.right, ESide.left))
    finally:
        ML.MatchLog = old_ml

    print("")
    if FAILS:
        print("★ FAILED: %d 项" % FAILS)
        return 1
    print("★ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
