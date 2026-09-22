#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pickdump.py —— 选择界面一开就把**完整卡表**打下来。

`ops.pick_candidates` 认定"候选会以 id>=4000 落进 discard/hand/deck"。这个前提
在预报**一级**（蓝天/薄雾/狂风）下不成立：那时 `pick_candidates` 返回空。
但"渲染 actor 里没有"不等于"卡表里没有" —— 要把整张卡表打出来才能下结论。

用法: python pickdump.py [秒数]      只读内存，不动鼠标。
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/（要 import ops）
OUT = _bootstrap.work_dir("logs", "pickevidence")


def main(argv=None) -> int:
    a = argv or sys.argv[1:]
    secs = float(a[0]) if a else 600.0
    import ops
    t0, done = time.time(), 0
    print("等选择界面… %.0fs" % secs)
    while time.time() - t0 < secs:
        ps = ops.pick_state()
        if not (ps and ps.get("choose_active")):
            time.sleep(0.2)
            continue
        st = ops.src().snapshot()
        rows = [{"card_id": c.card_id, "name": c.name, "side": c.side,
                 "location": c.location, "slot": c.slot, "type": c.card_type,
                 "cost": c.kredit_cost} for c in st.cards]
        big = [r for r in rows if (r["card_id"] or 0) >= 4000]
        tag = time.strftime("%Y%m%d-%H%M%S") + "-dump"
        os.makedirs(OUT, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "pick_state": ps,
               "turn": st.turn, "n_cards": len(rows),
               "by_loc": {"%s/%s" % k: v for k, v in
                          collections.Counter((r["side"], r["location"]) for r in rows).items()},
               "id_ge_4000": big, "cards": rows}
        with open(os.path.join(OUT, tag + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        print("\n★ 选择界面开着（hand_target=%s）turn=%s 卡表 %d 张，id>=4000 的 %d 张"
              % (ps.get("selecting_hand_target"), st.turn, len(rows), len(big)))
        for r in big[:12]:
            print("   id=%-6s %-28s %s/%s slot=%s" % (r["card_id"], r["name"],
                                                      r["side"], r["location"], r["slot"]))
        print("   分布: %s" % rec["by_loc"])
        print("   → %s" % os.path.join(OUT, tag + ".json"))
        done += 1
        time.sleep(3.0)
    print("结束，落盘 %d 次。" % done)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
