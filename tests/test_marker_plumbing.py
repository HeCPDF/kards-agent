#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四个标记（`+intel`/`+deckchg`/`+hooks`/`+shuffled`）的**接线**测试（离线，桩掉 VM）。

为什么单独测：§8-9 要的是**实机**证据（`EVAL-MARKERS-CHECKLIST.md`），但接线本身可以离线钉住 ——
只要对应的 trigger 函数返回了东西，`rule._hand_eff` 就必须
  ① 把后缀拼进 `eff_src[c.name]`（`+intel` 等）；② 把效果并进 `unit_eff`；
  ③ trigger 抛异常时写进 `marker_err` 而不是静默吞掉。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import player.rule as R                                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _C:
    def __init__(self, name, cid):
        self.name, self.card_id, self.cost, self.typ = name, cid, 2, "order"
        self.location, self.side = "hand", "local"
        self.raw = {"ptr": 0x100 + cid}
        self.keywords, self.cipher = [], 0
        self.kredit_cost, self.needs_hand_target = 2, False


class _Table:
    def load(self):
        return None


def mk_pol():
    pol = R.RuleV2.__new__(R.RuleV2)                 # 不走 __init__（要真会话/表）
    pol.P = dict(R.PARAMS, use_vm=False)
    pol.gaps, pol.eff_src, pol.marker_err = {}, {}, {}
    pol.intel_dbg = {}
    pol._eff_cache, pol._vm_spent, pol._eff_timeouts = {}, 999.0, {}
    pol._st = None
    pol.avoid, pol._tried, pol._bad = set(), set(), set()
    pol.fx_meta = {}
    pol.probe = {}
    pol._plans = {}
    pol._kw_cache, pol.suppress_kinds = {}, set()
    pol.probe_fn = lambda c: {"known": True, "side": None, "effects": {}, "kw": set()}
    # 四个 trigger 函数默认"不触发"
    pol._intel_triggers = lambda *a, **k: []
    pol._deck_changed_triggers = lambda *a, **k: []
    pol._play_hooks_triggers = lambda *a, **k: []
    pol._deck_shuffled_triggers = lambda *a, **k: []
    pol._vm_eff = lambda *a, **k: None               # 不跑 VM：只测标记接线
    return pol


def main():
    c = _C("TEST CARD", 1)

    # ---- ① 四个 trigger 各返回东西 ⇒ 对应后缀必须出现，且效果并进 unit_eff ----
    cases = (("intel", "_intel_triggers", "vm"),
             ("deckchg", "_deck_changed_triggers", "vm"),
             ("hooks", "_play_hooks_triggers", "vm"),
             ("shuffled", "_deck_shuffled_triggers", "vm"))
    for tag, fn, _base in cases:
        pol = mk_pol()
        setattr(pol, fn, lambda *a, **k: [(7, {"draw": 1})])
        eff = pol._hand_eff(c)
        got = pol.eff_src.get("TEST CARD", "")
        chk("%s：trigger 返回非空 ⇒ eff_src 带 '+%s' 且 unit_eff 合并了它" % (tag, tag),
            ("+" + tag) in got and (eff or {}).get("unit_eff") == [(7, {"draw": 1})],
            "eff_src=%r unit_eff=%r" % (got, (eff or {}).get("unit_eff")))

    # ---- ② trigger 返回空 ⇒ 不加后缀（别伪造标记）----
    pol = mk_pol()
    eff = pol._hand_eff(c)
    chk("trigger 都返回空 ⇒ eff_src 不带任何标记后缀",
        not any(("+" + t) in str(pol.eff_src.get("TEST CARD", ""))
                for t, _f, _b in cases), repr(pol.eff_src.get("TEST CARD")))

    # ---- ③ trigger 抛异常 ⇒ 记 marker_err（不静默）----
    for tag, fn, _base in cases:
        pol = mk_pol()

        def _boom(*_a, **_k):
            raise RuntimeError("boom-%s" % tag)

        setattr(pol, fn, _boom)
        pol._hand_eff(c)
        chk("%s：trigger 抛异常 ⇒ marker_err 记录（不再静默）" % tag,
            tag in pol.marker_err and "boom" in pol.marker_err[tag], str(pol.marker_err))

    # ---- ④ 换数据答案要变：两个 trigger 同时返回 ⇒ 后缀叠加 ----
    pol = mk_pol()
    pol._intel_triggers = lambda *a, **k: [(7, {"draw": 1})]
    pol._play_hooks_triggers = lambda *a, **k: [(8, {"kredit": 1})]
    eff = pol._hand_eff(c)
    unit_ids = sorted(u for u, _e in (eff or {}).get("unit_eff") or [])
    chk("两个标记同时触发 ⇒ 后缀叠加（intel+hooks）且两份效果都在",
        "+intel" in pol.eff_src["TEST CARD"] and "+hooks" in pol.eff_src["TEST CARD"]
        and unit_ids == [7, 8], "eff_src=%r unit_ids=%s" % (pol.eff_src["TEST CARD"], unit_ids))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
