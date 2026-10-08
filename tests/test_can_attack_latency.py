#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`game_can_attack` 的延迟修补（离线回归，假 self，不碰游戏）。

2026-10-07 实机：攻击的游戏闸门 3.5-6.6 s/次（打牌闸门 0.03 s）。静态分析的真因：`_card_object(id)` 对场上卡
先 miss 手牌、再查账本 ⇒ 全量扫 GObjects。修法：① 攻击者/目标指针取自决策快照 `c.raw["ptr"]`；
② `_card_object` 先 `hand_actor(tries=1)`（不走账本扫描），都没找到才退回完整版；③ `_bp_lib_fn` 不缓存"没找到"。
这里钉：快照里有 ⇒ 一次 `_card_object` / 一次 GObjects 扫描都不发；快照没有 ⇒ 退回老路；结果带 `timing`。
"""
import os
import sys
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import query as OQ                                              # noqa: E402
from ops import world as OW                                              # noqa: E402
from ops import conn as OC                                               # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def card(cid, ptr, side=1, loc=7):
    return NS(obj=NS(CardID=cid, Location=loc), side=side, raw={"ptr": ptr})


class FakeApi:
    def __init__(self):
        self.calls = []

    def callRawArr(self, obj, func, parms, arr_off, ptrs):
        self.calls.append((obj, func, ptrs))
        b = bytearray(0x58)
        b[0x30] = 1
        return bytes(b)


class Fake(OQ.QueryMixin):
    def __init__(self):
        self._api = FakeApi()
        self.m = None
        self.card_object_calls = []
        self.logic_calls = 0

    def api(self):
        return self._api

    def _bp_lib_fn(self, c, f):
        return 1, 0xC0, 0xF0

    def _card_object(self, cid):
        self.card_object_calls.append(cid)
        return 0x9000 + cid

    def call_raw_arr(self, obj, func, parms, arr_off, ptrs):
        return OC.ConnMixin.call_raw_arr(self, obj, func, parms, arr_off, ptrs)

    def logic_actor(self, refresh=False):
        self.logic_calls += 1
        return 0x1234


def snap(cards):
    return NS(cards=cards, kredits={1: 5}, my_side=1, turn=3)


def main():
    st = snap([card(10, 0xA10, side=1), card(20, 0xA20, side=2), card(21, 0xA21, side=2)])
    f = Fake()
    r = f.game_can_attack(10, 20, snapshot=st)
    chk("快照里有两张卡 ⇒ 不调 _card_object", f.card_object_calls == [], str(f.card_object_calls))
    chk("结果 can=True 且指针来自快照", r.get("ok") and r["can"] is True and r["card_obj_src"] ==
        {"attacker": "snapshot", "defender": "snapshot"}, str(r))
    chk("同一行（防守方那侧）的指针传给数组", f._api.calls and sorted(int(x, 16) for x in f._api.calls[0][2]) == [0xA20, 0xA21],
        str(f._api.calls))
    chk("timing 各阶段齐全", all(k in r["timing"] for k in
        ("lib_fn_s", "snapshot_s", "card_objects_s", "row_ptrs_s", "logic_actor_s", "call_s")), str(r.get("timing")))
    chk("logic_actor 每次问一次（缓存在 logic_actor 内部）", f.logic_calls == 1)

    f2 = Fake()
    r2 = f2.game_can_attack(10, 99, snapshot=st)
    chk("目标不在快照 ⇒ 只对它退回 _card_object（老路保留）", f2.card_object_calls == [99]
        and r2["card_obj_src"] == {"attacker": "snapshot", "defender": "scan"}, str(r2))

    # --- _card_object：场上卡不许走账本全量扫描
    calls = []

    class W(OW.WorldMixin):
        def __init__(self, hand=None, board=0):
            self.hand, self.board = hand, board

        def hand_actor(self, cid, tries=5, gap=0.35, sleep=None):
            calls.append(tries)
            return self.hand if tries == 5 and self.hand else None

        def board_actor_of(self, cid):
            return self.board

        def uclass_of_instance(self, a):
            return 1

        def off(self, c, n, note):
            return 8

    class M:
        def ptr(self, a):
            return 0x5000

    w = W(board=0xB0)
    w.m = M()
    calls.clear()
    chk("场上卡：只扫一遍手牌（tries=1），命中 board 即停", w._card_object(5) == 0x5000 and calls == [1], str(calls))
    w2 = W(hand={"actor": 0x77}, board=0)
    w2.m = M()
    calls.clear()
    chk("两路都 miss ⇒ 退回完整 hand_actor（横幅期重试语义不变）", w2._card_object(5) == 0x5000 and calls == [1, 5],
        str(calls))

    # --- _bp_lib_fn 不缓存 miss
    class Conn(OC.ConnMixin):
        pass

    c = Conn.__new__(Conn)
    c._fn_cache = {}
    n = {"scan": 0}

    class OA:
        def iter_objects(self, skip_cdo=False):
            n["scan"] += 1
            return []

    c.oa = OA()
    c._bp_lib_fn("X_C", "Y")
    c._bp_lib_fn("X_C", "Y")
    chk("没找到不缓存（第二次仍会重试，而不是被永久钉成 0）", n["scan"] == 2 and not c._fn_cache, str(n))
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
