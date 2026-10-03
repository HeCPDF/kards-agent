#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2：对**运行中的真游戏**做只读 RVA 扫描，并与基准表（build_tables.json 里同版本的真值）逐项对账。

只读（ReadProcessMemory），不注入、不置前。用法：`python tools/rvascan_live.py [--no-cache]`。
基准只用来对账，**不参与扫描**。
"""
import json
import os
import sys
import time

import _bootstrap  # noqa: F401

from kardsmem import proc, rvascan, version as V

GOLDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kardsmem", "build_tables.json")


def main():
    pids = V.game_pids()
    if not pids:
        print("游戏没在跑")
        return 2
    pid = pids[0]
    ver = V.version_of_pid(pid)
    mod = proc._module_of(pid, "kards-Win64-Shipping.exe")
    base, size, _path = mod
    m = proc.MemRO(pid)
    t0 = time.time()
    r = rvascan.resolve(m, base, size, ver, log=lambda s: print("  [scan]", s), force="--no-cache" in sys.argv)
    print("版本 %s  来源 %s  用时 %.1fs" % (ver, r["source"], time.time() - t0))
    gold = {}
    for b in json.load(open(GOLDEN, encoding="utf-8"))["builds"].values():
        if b.get("version") == ver:
            gold = b["rva"]
    bad = 0
    for k, v in r["rva"].items():
        g = gold.get(k)
        ok = g == v
        if k == "GWorld" and g is not None and not ok:
            # 基准是早先 .data 扫描挑的槽；扫描器按"代码引用数"挑槽。两槽当前存着同一个 World ⇒ 等价
            ok = (m.u64(base + g) is not None) and m.u64(base + g) == m.u64(base + v)
            if ok:
                print("  （GWorld 槽不同但存着同一个 World 指针 ⇒ 视为等价；扫描器取代码引用多的槽）")
        bad += (not ok) and g is not None
        print("  %-22s 扫描 0x%X  基准 %s  %s" % (k, v, hex(g) if g is not None else "—", "OK" if ok else ("无基准" if g is None else "不一致")))
    print("结果：", "全部一致" if not bad and gold else ("有不一致" if bad else "无基准可比"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
