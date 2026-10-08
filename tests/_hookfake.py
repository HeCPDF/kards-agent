# -*- coding: utf-8 -*-
"""A5 钩子族离线测试夹具：假 VM（记录每次钩子调用的 顺序 / 形参）+ 假触发缓存（谁覆写了哪些钩子）。

用法：
    res, calls = run(TR.run_convert, cards, OVR, new_card, [31], [-1], "NAME", 9, behave=..., old_cards=[...])
    calls = [(钩子名, 卡名, 形参 dict, 额外 kw)]；OVR = {类号: {钩子名...}}，类号 = 1000 + 卡指针（见 `OA`）。
`behave(钩子名, 卡, 形参) -> {"out": {...}, "records": [...]}` 决定假 VM 的出参 / 记录（缺省全空）。
不碰游戏进程、不碰 ObjectArray 真实现（只在 `run` 期间 monkeypatch）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import semantics.triggers as TR                                   # noqa: E402
from _cards import ME, OPP, mk_card                               # noqa: E402,F401


class K:
    """假 km。"""


class OA:
    """假 ObjectArray：指针 p 的类号 = 1000 + p。"""

    def __init__(self, km):
        pass

    def class_of(self, p):
        return 1000 + p


class TC(TR.TriggerCache):
    """假触发缓存：`ovr[类号]` 里列出的钩子 ⇒ 该类覆写了它。"""

    def __init__(self, ovr):
        self._fn = {}
        self.ovr = ovr

    def fn_of_class(self, uc, hook=TR.HOOK):
        return 7 if hook in self.ovr.get(uc, ()) else 0


class St:
    def __init__(self, cards):
        self.cards = cards


def card(cid, name, loc="frontline", side=ME, ptr=None, **kw):
    """真 `kardsmem.board.Card`（带原版 BaseCardObject），指针缺省 = card_id（便于对照形参）。"""
    return mk_card(cid, side=side, location=loc, name=name, ptr=cid if ptr is None else ptr, **kw)


import contextlib                                                  # noqa: E402


@contextlib.contextmanager
def patched(ovr, behave=None):
    """把 `ObjectArray` / `triggers._run_hook_ex` 换成假的，yield `calls` 列表（离开时还原）。"""
    calls = []

    def fake(km, c, h, a, stream=None, my_side=None, read_hooks=None, budget_s=1.0, hq_own=(), hq_enemy=(), **kw2):
        if h not in ovr.get(1000 + TR._ptr_of(c), ()):
            # 真 VM：这张牌的类没有覆写这个钩子 ⇒ ran=False（自己的钩子不经 fetch，runner 会直接问，所以假 VM 也得这样答）
            return {"ran": False, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}
        calls.append((h, getattr(c, "name", "?"), dict(a), dict(kw2)))
        b = behave(h, c, a) if behave else {}
        return {"ran": True, "eff": {}, "out": dict(b.get("out") or {}), "records": list(b.get("records") or []),
                "gaps": list(b.get("gaps") or []), "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = OA, fake
    try:
        yield calls
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old


def run(fn, cards, ovr, *args, behave=None, **kw):
    """在假 VM 下跑 `fn(km, st, *args, cache=…, …)`；返回 `(结果, calls)`。"""
    with patched(ovr, behave) as calls:
        res = fn(K(), St(cards), *args, cache=TC(ovr), read_hooks={}, my_side=ME, hq_own=(), hq_enemy=(),
                 enum_random=False, **kw)
    return res, calls


def names(calls):
    return [(h, n) for h, n, _a, _k in calls]


class Checker:
    def __init__(self):
        self.fails = 0

    def __call__(self, name, ok, extra=""):
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        if not ok:
            self.fails += 1
