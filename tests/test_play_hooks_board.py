#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`+hooks` 族（`run_play_hooks`/`run_enter_play`）的两个真 bug 回归测试（离线，桩掉 VM）。

2026-10-02 实机只读探针（`_nn_scratch/probe_hooks_deep2.py`）抓到两条，症状都是
"链恒空、还被 except 吞掉"：
  ① `rule._play_hooks_triggers` 把 `board_api.Card` 传给 `exclude=`，而 `find_cards`
     以前直接 `set(exclude)` ⇒ 每次 `TypeError: unhashable type: 'Card'`；
  ② `triggers._run_hook` / `run_enter_play` 跑 VM 时没传 `board`/`my_seat`
     ⇒ 钩子的 `IsSideActive` 当场如实停下（`Unimplemented ... 需要 BoardState`）。

这里用**不可哈希**的替身卡（和 `board_api.Card` 一样 `__hash__ = None`）钉住 ①，
并用假 `record_effects` 断言 ② 真的把 `board`/`my_seat` 透传下去了。
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cards import ME, OPP, mk_card                               # noqa: E402
import semantics.effectvm as EV                                   # noqa: E402
import player.rule as R                                        # noqa: E402
import semantics.triggers as T                                    # noqa: E402
import kardsmem.objects as O                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def _Card(name, ptr, cid):
    return mk_card(cid, ME, "frontline", "infantry", 1, 1, 1, 1, name, ptr=ptr)


class _Cache:
    """`TriggerCache` 的最小替身：任何类都"挂着这个钩子"。"""
    _cls = {}

    def fn_of_class(self, uc, hook):
        return 0xF00D


class _Km:
    m = None
    base = 0


def main():
    played, other = _Card("PLAYED", 0x11, 1), _Card("OTHER", 0x22, 2)
    st = SimpleNamespace(cards=[played, other], my_side=ME, other_side=OPP, my_side_raw=1)
    km = _Km()
    O.ObjectArray.class_of = lambda self, p: 0xAA00
    calls = []

    def fake_record_effects(km_, cp, *a, **kw):
        calls.append({"cp": cp, **kw})
        return {"eff": {"buff": [1, 1]}, "stopped": None, "ran": True}

    orig = EV.record_effects
    EV.record_effects = fake_record_effects
    try:
        res = T.run_play_hooks(km, st, played, my_side=1, exclude=[played], cache=_Cache())
        chk("exclude 传 Card（不可哈希）不再 TypeError", True)
        names = [h.get("name") for h in (res.get("hits") or [])]
        chk("被 exclude 的打出牌自己不在 hits 里", "PLAYED" not in names, str(names))
        chk("别的挂钩子的卡照常出 hit", "OTHER" in names, str(names))
        chk("打到 VM 的调用带 board/my_seat",
            bool(calls) and all(c.get("board") is st and c.get("my_seat") == 1 for c in calls),
            "calls=%d board?=%s seat=%s" % (len(calls),
                                            [c.get("board") is st for c in calls],
                                            [c.get("my_seat") for c in calls]))

        calls.clear()
        T.run_enter_play(km, st, played, my_side=1, exclude=[played], cache=_Cache())
        chk("run_enter_play 同样带 board/my_seat",
            bool(calls) and all(c.get("board") is st and c.get("my_seat") == 1 for c in calls),
            "calls=%d" % len(calls))
    finally:
        EV.record_effects = orig

    # ---- rule 层：`_play_hooks_triggers` 必须给 `exclude` 传可哈希的**指针** ----
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS, use_vm=False)
    pol.marker_err, pol.gaps, pol.eff_src = {}, {}, {}
    pol._playhook_cache, pol._st = {}, st
    pol._rng_seed = lambda: None
    pol._km = lambda: km
    seen = {}
    real_run = T.run_play_hooks

    def fake_run(km_, st_, played_, **kw):
        seen.update(kw)
        return {"hits": [{"hook": "OnCounterMeasureTriggered", "name": "OTHER", "ptr": 0x22,
                          "side": ME, "eff": {"buff": [1, 1]}, "stopped": None}]}

    T.run_play_hooks = fake_run
    try:
        got = pol._play_hooks_triggers(st, played, seed=None)
    finally:
        T.run_play_hooks = real_run
    exc = seen.get("exclude") or []
    chk("rule 层：hits → (card_id, eff) 映射正确", got == [(2, {"buff": [1, 1]})], repr(got))
    chk("rule 层：exclude 全是指针（可哈希），不是 Card",
        bool(exc) and all(isinstance(x, int) and x > 0 for x in exc), repr(exc))
    chk("rule 层：无异常时不写 marker_err", pol.marker_err == {}, repr(pol.marker_err))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
