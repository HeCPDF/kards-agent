#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0x1C / 0x3 / 0x16 三条触发族的**取效果方式**回归测试（离线，桩掉 VM 与内存）。

为什么单独测：2026-10-02 实机发现这三条族的实现都写成
    `r = EV.record_effects(...); outs = r["outcomes"]`
—— 而 `record_effects` 的返回里**没有** `outcomes`（那是 `enumerate_effects` 的；
死亡链早先踩过同款坑）⇒ 实战里恒空，日志只留下"场上没有覆写 OnIntelTriggered 的
本方卡"这种假象诊断。

本测试把两个 runner 都换成桩：
  * `enumerate_effects` → 正常的 outcomes 形状（必须被用上）；
  * `record_effects`    → 只有 `eff`、没有 `outcomes`（旧实现会在这里拿到空）。
只要哪天有人改回 `record_effects`，断言立刻失败。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import player.rule as R                                          # noqa: E402
from kardsmem import kismet as K                                # noqa: E402
from kardsmem import objects as O                               # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _Card:
    def __init__(self, name, cid, loc, side, ptr, **kw):
        self.name, self.card_id = name, cid
        self.location, self.side = loc, side
        self.raw = {"ptr": ptr}
        self.cipher = kw.get("cipher", 0)
        self.fname = kw.get("fname")
        self.attack = kw.get("attack", 1)
        self.defense = kw.get("defense", 1)
        self.keywords = []


class _St:
    def __init__(self, cards, seat=1):
        self.cards, self.my_side_raw, self.turn = cards, seat, 3
        self.slots = {}


class _Km:
    """`ObjectArray(session)` 只要求 `.m` / `.base`（构造后马上被桩掉 `class_of`）。"""
    m = None
    base = 0


def mk_pol(outcomes):
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS, use_vm=True)
    pol.intel_dbg, pol.eff_src, pol.gaps, pol.marker_err = {}, {}, {}, {}
    pol._st = None
    pol._km = lambda: _Km()
    pol._rng_seed = lambda: None
    pol._eff_cache, pol._vm_spent, pol._eff_timeouts = {}, 0.0, {}
    pol._deathfx_cache = None
    pol.fx_meta = {}
    pol._trigcache = None
    pol._trigcache_turn = None
    R.EV.make_read_hooks = lambda *a, **k: {}
    K.find_function = lambda *a, **k: 0xDEAD0000
    O.ObjectArray.class_of = lambda self, ptr: 0xBEEF0000
    R.EV.enumerate_effects = lambda *a, **k: {"outcomes": outcomes, "complete": True,
                                              "stopped": None}
    R.EV.record_effects = lambda *a, **k: {"eff": {"SHOULD_NOT_BE_USED": True},
                                           "complete": True, "stopped": None}
    return pol


def main():
    src = _Card("CRUISER SCOUTS", 11, "hand", "local", 0x1001, cipher=3)
    trig = _Card("NAKAJIMA B5N2", 12, "frontline", "local", 0x1002)
    st = _St([src, trig])

    pol = mk_pol([(1.0, {"attack": 1, "defense": 1})])
    got = pol._intel_triggers(st, src)
    chk("intel：enumerate 的单分支结果被采纳", got == [(12, {"attack": 1, "defense": 1})], repr(got))
    chk("intel：没有落假诊断", not pol.intel_dbg.get("why"), repr(pol.intel_dbg.get("why")))

    pol = mk_pol([(0.5, {"attack": 1}), (0.5, {"attack": 2})])
    got = pol._intel_triggers(st, src)
    chk("intel：多分支保留 outcomes 结构与权重",
        got and got[0][0] == 12 and len(got[0][1].get("outcomes") or []) == 2, repr(got))

    pol = mk_pol([(1.0, {})])
    got = pol._intel_triggers(st, src)
    chk("intel：有覆写但空效果 ⇒ 返回空且 tried 记原因",
        got == [] and "NAKAJIMA B5N2" in (pol.intel_dbg.get("tried") or {}), repr(pol.intel_dbg))

    # 0x3：牌库变化族（guard 读 eff 里的 deck_shuffle/deck_add…）
    pol = mk_pol([(1.0, {"deck_add": 1})])
    got = pol._deck_changed_triggers(st, {"deck_shuffle": 1})
    chk("deckchg：enumerate 的结果被采纳", got == [(12, {"deck_add": 1})], repr(got))

    # 0x16：洗牌族（guard 还要求 skipSubAction=true）
    pol = mk_pol([(1.0, {"deck_add": 1})])
    got = pol._deck_shuffled_triggers(st, src, {"deck_shuffle": 1, "deck_shuffle_skip": True})
    chk("shuffled：enumerate 的结果被采纳", got == [(12, {"deck_add": 1})], repr(got))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
