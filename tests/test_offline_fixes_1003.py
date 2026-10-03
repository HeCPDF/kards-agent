#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-10-03 两处离线修复的回归。

① `SOUL OF OLD JAPAN` 的 `cards_reserve_changes` 空读：根因是 `Get*OfClass` 只在
   **关卡 actor 表**里找（`Locator.actors()` 233 个里没有 `BP_Logic_C`）⇒ 返回空表、
   蓝图后面读空对象字段。修法：找不到时按类名做一次全对象扫兜底（缓存 5 min）。
② `THE POMPADOURS` 那类"在手牌中生效"的牌：`triggers.run_play_hooks` 不再
   `on_board_only=True`，由钩子自身判据决定（手牌里的触发者也要跑）。

（`BLUE SKY` 的 autoplay 三选一不在本测试里：用户 2026-10-03 明确"9 选 1 已建好模，忽略"。）
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import semantics.triggers as T                                     # noqa: E402
import kardsmem.objects as O                                   # noqa: E402
from kardsmem import cardnatives as CN                         # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _Pool:
    def fname_of(self, cls):
        return {0x10: "BP_Logic_C_2147355775", 0x20: "NothingHere"}.get(cls, "")


class _OA:
    def __init__(self, ks=None):
        pass

    def class_of(self, p):
        return 0x10

    def pool(self):
        return _Pool()

    def class_name(self, p):
        return "BP_Logic_C_2147355775"

    def find_by_class_name(self, name, limit=None):
        return [0xAA] if name == "BP_Logic_C_2147355775" else []


class _Ks:
    base = 0
    pid = 4242
    m = SimpleNamespace(ptr_or_zero=lambda a: 0)


def main():
    # ---- ① Get*OfClass 的全对象扫兜底 ----
    orig_snap, orig_oa = CN._actors_snapshot, O.ObjectArray
    CN._ACTOR_CACHE.clear()
    CN._actors_snapshot = lambda ks, ttl=5.0: []          # 关卡 actor 表里没有它
    O.ObjectArray = _OA
    try:
        cn = CN.CardNatives(ks=_Ks())
        got = CN._get_actor_of_class(cn, 0, 0x10)
        chk("① 关卡表没有 ⇒ 兜底扫到（单个）", got == 0xAA, hex(got or 0))
        got_all = CN._get_all_actors_of_class(cn, 0, 0x10)
        chk("① 兜底扫到（全部）", got_all == [0xAA], str(got_all))
        CN._ACTOR_CACHE.clear()
        miss = CN._get_actor_of_class(cn, 0, 0x20)
        chk("① 兜底也找不到 ⇒ 0（不编）", miss == 0, str(miss))
    finally:
        CN._actors_snapshot, O.ObjectArray = orig_snap, orig_oa
        CN._ACTOR_CACHE.clear()

    # ---- ② run_play_hooks 不再按位置过滤（手牌触发者要跑）----
    seen = {}
    orig_fc, orig_run = T.find_cards, T._run_hook

    class _HandCard:
        name = "THE POMPADOURS"
        side = "local"
        raw = {"ptr": 0xBB}
        card_id = 99

    def fake_fc(km, st, exclude=(), cache=None, hook=None, on_board_only=True, **kw):
        seen["on_board_only"] = on_board_only
        return [] if on_board_only else [(_HandCard(), 1)]

    T.find_cards = fake_fc
    T._run_hook = lambda *a, **k: ({"heal_hq": 1}, None)
    try:
        res = T.run_play_hooks(object(), SimpleNamespace(cards=[]), 0x1234,
                               my_side=1, exclude=(), include_counter=True)
    finally:
        T.find_cards, T._run_hook = orig_fc, orig_run
    chk("② 传给 find_cards 的 on_board_only=False", seen.get("on_board_only") is False,
        str(seen))
    names = [h.get("name") for h in (res.get("hits") or [])]
    chk("② 手牌里的触发者进了 hits", "THE POMPADOURS" in names, str(names[:3]))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
