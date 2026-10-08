#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原版 `MakeCardRetreat` 的 **`cantRetreat` 门**（P3 ⑤，2026-10-04）。

依据：`BP_CardFunctions::MakeCardRetreat`（`BP_CardFunctions.cpp:1709-1754`）先逐张
`HasCustomAbility("cantRetreat")` **跳过**（`:1727-1735`），再把剩下的按支援线/前线分两批调
`ApplyMakeCardRetreat`（`:1742`/`:1754`）。`HasCustomAbility(name)` 的权威读法是
`cardnatives._ability_has(c, name) = received_abilities[name] > 0` —— 也就是
`receivedAbilitiesFromCards`@0x208（快照 raw 里由 `board._read_card` 对**场上**的牌读出来）。

我们以前不看这道门 ⇒ 会把带 `cantRetreat` 的牌也撤掉（多撤）。
两个不同输入必须给出不同答案（弯路 #11）：一张带、一张不带。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from engine import effectvm as EV                                    # noqa: E402
from _cards import mk_card                                           # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    c_ok = mk_card(11, location="frontline", ptr=0x600)
    c_no = mk_card(12, location="frontline", ptr=0x601)
    c_no.raw["received_abilities"] = ["cantRetreat"]      # 实机：board._read_card 读 0x208

    r = EV.Recorder()
    EV._fill_cur_stats(r, [c_ok, c_no])
    chk("判据：带 `cantRetreat` 的牌 `cur_stats['cant_retreat']=True`，另一张 False",
        (r.cur_stats.get(0x601) or {}).get("cant_retreat") is True
        and (r.cur_stats.get(0x600) or {}).get("cant_retreat") is False,
        "no=%s ok=%s" % (r.cur_stats.get(0x601), r.cur_stats.get(0x600)))
    chk("判据：`abilities` 一起带出来（与 `excess` 共用同一张表）",
        "cantRetreat" in ((r.cur_stats.get(0x601) or {}).get("abilities") or ()),
        str((r.cur_stats.get(0x601) or {}).get("abilities")))

    r.records.append({"verb": "MakeCardRetreat", "args": [[0x600, 0x601], 9], "tainted": False})
    e = EV.to_effects(r, my_side=1)
    chk("门：`retreat_ids` **只剩不带 cantRetreat 的那张**（11）",
        e.get("retreat_ids") == [11], str(e))

    # 对照输入：两张都不带 ⇒ 两张都撤
    c_a = mk_card(11, location="frontline", ptr=0x600)
    c_b = mk_card(13, location="frontline", ptr=0x602)
    r2 = EV.Recorder()
    EV._fill_cur_stats(r2, [c_a, c_b])
    r2.records.append({"verb": "MakeCardRetreat", "args": [[0x600, 0x602], 9], "tainted": False})
    e2 = EV.to_effects(r2, my_side=1)
    chk("对照输入：都不带 cantRetreat ⇒ 两张都进 `retreat_ids`", e2.get("retreat_ids") == [11, 13], str(e2))

    # 全部带 cantRetreat ⇒ 什么都不产出（原版就是什么都不做），**且不记缺口**
    r3 = EV.Recorder()
    EV._fill_cur_stats(r3, [c_no])
    r3.records.append({"verb": "MakeCardRetreat", "args": [[0x601], 9], "tainted": False})
    e3 = EV.to_effects(r3, my_side=1)
    chk("全被门挡下 ⇒ 不产出 `retreat_ids`、也不记缺口（这是「已知的不做」）",
        not e3.get("retreat_ids") and not r3.gaps, "eff=%s gaps=%s" % (e3, r3.gaps))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
