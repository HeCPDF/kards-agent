#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mulverify.py —— 拿两份 minidump 核对 `BP_HandCard_C::shouldDiscard`。

判据（三条都得过，缺一条就不算证实）：
  1. 反射链能在 `BP_HandCard_C` 里找到名叫 `shouldDiscard` 的 BoolProperty，
     且 `props.sanity()` 说整条链都落在 PropertiesSize 里（否则偏移表本身就错了）。
  2. before 转储里所有手牌 marked 全 False。
  3. after 转储里**恰好一张** marked=True —— 而且是用户点的那一张。

用法：python mulverify.py <before.DMP> <after.DMP>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  —— 接上 kards-agent/ 与 vendor/

from dumpmem import DumpSession                      # noqa: E402
from kardsmem import pick, props                     # noqa: E402
from kardsmem.world import Locator                   # noqa: E402


def one(path: str) -> dict:
    s = DumpSession(path)
    out = {"path": os.path.basename(path), "base": s.base}
    rows = pick.hand_card_actors(s)
    out["hand"] = len(rows)
    if not rows:
        s.close()
        return out
    loc = Locator(s.m, s.base)
    uc = loc.uclass_of(rows[0]["actor"])
    out["uclass"] = uc
    out["sanity"] = props.sanity(s, uc)
    p = props.find_prop(s, uc, "shouldDiscard")
    out["prop"] = p
    if p:
        for r in rows:
            r["marked"] = props.read_bool(s.m, r["actor"], p)
    out["rows"] = [(r.get("slot"), r.get("card_id"), r.get("name"), r.get("marked"))
                   for r in rows]
    s.close()
    return out


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    if len(argv) != 2:
        print(__doc__)
        return 2
    res = []
    for p in argv:
        r = one(p)
        res.append(r)
        print("=== %s  base=%#x  手牌 %d 张" % (r["path"], r["base"], r["hand"]))
        if r.get("sanity"):
            sa = r["sanity"]
            print("    反射链自检: propsize=%s 属性 %d 个（有名字 %d）越界 %d → %s"
                  % (sa["propsize"], sa["count"], sa["named"], sa["out_of_range"],
                     "OK" if sa["ok"] else "★ 不可信"))
        pr = r.get("prop")
        print("    shouldDiscard: %s" % (
            ("+0x%X %s fieldsize=%s byteoff=%s bytemask=%#04x"
             % (pr["offset"], pr["type"], pr.get("bit_field_size"),
                pr.get("bit_byte_offset"), pr.get("bit_byte_mask") or 0))
            if pr else "★ 没找到"))
        for slot, cid, nm, mk in r.get("rows", []):
            print("      slot=%-4s id=%-6s %-28s marked=%s" % (slot, cid, nm, mk))
    if len(res) == 2 and res[0].get("rows") and res[1].get("rows"):
        b = [m for *_x, m in res[0]["rows"]]
        a = [m for *_x, m in res[1]["rows"]]
        print("\n结论：before %s → after %s" % (b, a))
        if not any(b) and sum(1 for x in a if x) == 1:
            print("★ 对上了：before 全未标记，after 恰好一张被标记。")
        else:
            print("★ 对不上 —— 不要据此下结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
