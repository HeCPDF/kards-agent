#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_search_sim.py —— `RuleV2._search_sim`（build_sim_s）的**离线**计时 + 等价对拍（不碰游戏、不注入）。

做什么：把崩溃转储（`kards-data/crashdumps/*.dmp`，当只读内存用，同 `tools/reconcile_batch.py`）里的盘面喂给**真的**
`_search_sim`，各跑「不开建 sim 作用域」（旧行为）与「开作用域」（`kardsmem/readscope.py`）若干轮，打印：
  * 总耗时 + 各阶段（`probe.timing.stages` 同一份：death_fx / attack_fx / fx_draw / from_cards …）；
  * 两种模式产物的 sha1（death/attack/draw/event_fx/pair_eff/hand/sim.death_fx/sim.attack_fx）——**必须相同**；
  * 共享卡视图的"就地被改"检查（共享表里每张视图与此刻现读的逐字相同）。
`--hook-units`：往盘面里塞几张**真有** `OnOtherCardDrawnFromDeck` 覆写的静态单位（复制静态卡对象到覆盖层，同
`reconcile_batch.synthetic_orders` 的做法），复现实战里"牌库 ~30 张 × 旁观者"的 draw_fx 量级（转储里的牌组大多没有旁观者）。

用法（需 `KARDS_DATA_DIR=D:/Kards/kards-data`）：
    python tools/bench_search_sim.py kards-data/crashdumps/kards-Win64-Shipping.exe_261001_092807.dmp --hook-units
注意：转储读内存比实机 ReadProcessMemory 快、也没有游戏线程竞争 ⇒ 绝对秒数只当**相对**比较；实机数以日志为准。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import struct
import sys
import time

AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if AGENT_ROOT not in sys.path:
    sys.path.insert(0, AGENT_ROOT)

HOOK_UNITS = ("card_unit_125_rifle_regiment", "card_unit_27_fusiliers", "card_unit_3rd_canadian_division",
              "card_unit_4th_guards_rifles", "card_unit_67th_baranovichi")


def canon(o) -> str:
    """与字典序无关的规范化文本（做哈希用）。"""
    if isinstance(o, dict):
        return "{" + ",".join(sorted("%s:%s" % (canon(k), canon(v)) for k, v in o.items())) + "}"
    if isinstance(o, (list, tuple)):
        return "[" + ",".join(canon(x) for x in o) + "]"
    if isinstance(o, (set, frozenset)):
        return "<" + ",".join(sorted(canon(x) for x in o)) + ">"
    return repr(o)


def add_hook_units(RB, km, st, n_mine: int = 3) -> int:
    from kardsmem import board as BA, gs as G, props
    m = km.m
    prov = G.make_static_card_provider(km)
    reader = RB._card_reader(km)
    ref = next(c for c in st.cards if c.obj.IsFieldUnit() and c.obj.side == st.my_side)
    foe = next((c for c in st.cards if c.obj.IsFieldUnit() and c.obj.side != st.my_side), ref)
    p0 = props.find_prop(km, m.ptr_or_zero(ref.raw["ptr"] + RB.OFF_UOBJ_CLASS), "cardFunction")
    cf = m.ptr_or_zero(ref.raw["ptr"] + p0["offset"]) if p0 else 0
    out = []
    for i, nm in enumerate(HOOK_UNITS):
        v = prov(nm)
        src = int(v["ptr"])
        uc = m.ptr_or_zero(src + RB.OFF_UOBJ_CLASS)
        addr = m.alloc_copy(src, m.i32(uc + RB.OFF_UCLASS_PROPSIZE))
        who = ref if i < n_mine else foe
        m.patch(addr + BA.CARD_I32["card_id"], struct.pack("<i", 60000 + i))
        m.patch(addr + BA.CARD_U8["location_enum"], bytes([int(who.obj.Location)]))
        m.patch(addr + BA.CARD_U8["side_enum"], bytes([int(who.obj.side)]))
        pc = props.find_prop(km, uc, "cardFunction")
        if pc and cf:
            m.patch(addr + pc["offset"], struct.pack("<Q", cf))
        c = reader._read_card(m, addr, [])
        if c is not None:
            out.append(c)
    st.cards = list(st.cards) + out
    return len(out)


def run_once(RB, path: str, scoped: bool, hook_units: bool):
    from kardsmem import cards as CARDS, readscope as RS
    km, st = RB.open_state(path)
    if hook_units:
        add_hook_units(RB, km, st)
    keep = []
    orig = RS.build_scope

    @contextlib.contextmanager
    def keeping():
        with orig() as sc:
            keep.append(sc)
            yield sc

    @contextlib.contextmanager
    def off():
        yield RS.Scope()                              # 不设 current() ⇒ 等于没有作用域（旧行为）
    RS.build_scope = keeping if scoped else off
    try:
        with RB.offline_guard():
            R, pol = RB._policy(km, st)
            ehq = R._hq_of(st.cards, st.other_side)
            t = time.perf_counter()
            sim = pol._search_sim(st, 10.0, ehq)
            dt = time.perf_counter() - t
    finally:
        RS.build_scope = orig
    fx = {"death": pol._death_fx(st), "attack": dict(pol._attack_fx(st, ehq)), "draw": pol._draw_fx(st),
          "event": sim.event_fx, "pair_eff": sim.pair_eff, "hand": {k: getattr(h, "eff", None) for k, h in sim.hand.items()},
          "death_fx": sim.death_fx, "attack_fx": sim.attack_fx}
    digest = hashlib.sha1(canon(fx).encode("utf-8")).hexdigest()[:12]
    bad = n = 0
    if scoped and keep:
        for (name, _o), tab in keep[-1].tables.items():
            if name == "view":
                for ptr, v in tab.items():
                    n += 1
                    bad += canon(CARDS.read_raw(km, ptr)) != canon(v)
    stages = dict(pol._stage_t)
    stats = getattr(pol, "_scope_stats", None)
    km.m.close()
    return dt, digest, stages, stats, (n, bad)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dump")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--hook-units", action="store_true", help="塞几张有 OnOtherCardDrawnFromDeck 覆写的单位（复现 draw_fx 量级）")
    a = ap.parse_args(argv)
    os.environ.setdefault("KARDS_DATA_DIR", os.path.join(os.path.dirname(AGENT_ROOT), "kards-data"))
    import tools.reconcile_batch as RB
    digests = set()
    for _ in range(a.rounds):
        for scoped in (False, True):
            dt, dg, stg, stats, (nv, bad) = run_once(RB, a.dump, scoped, a.hook_units)
            digests.add(dg)
            print("%-6s %.2fs %s %s" % ("scope" if scoped else "plain", dt, dg,
                                        {k: v for k, v in stg.items() if v >= 0.05}), flush=True)
            if scoped:
                print("       命中统计 %s；共享视图就地被改检查：%d 张里 %d 张被改" % (stats, nv, bad), flush=True)
                if bad:
                    return 2
    print("两种模式产物哈希%s" % ("一致" if len(digests) == 1 else "【不一致】 %s" % sorted(digests)))
    return 0 if len(digests) == 1 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
