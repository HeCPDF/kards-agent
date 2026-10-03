#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim 出处注释棘轮（SIM-FIDELITY.md §3 第 6 条）：`sim/` 里写明“原版出处”的注释数量**只许升不许降**。

约定写法：注释/文档字符串里含 `原版`（如 `原版：BP_CardFunctions::SpawnCardToBoard`）。
新增或移植一条规则就把出处写上、基线改大；不许删出处。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 基线（2026-10-03 量得）。只许升。
BASELINE = {"sim/chain.py": 5, "sim/dispatch.py": 4, "sim/engine.py": 5, "sim/state.py": 4}
fails = 0


def count(path):
    return sum("原版" in ln for ln in open(os.path.join(ROOT, path), encoding="utf-8"))


def main():
    global fails
    for p, base in BASELINE.items():
        n = count(p)
        ok = n >= base
        print("  [%s] %s 出处注释 %d ≥ 基线 %d" % ("PASS" if ok else "FAIL", p, n, base))
        fails += (not ok)
    tot = sum(count(os.path.join("sim", f)) for f in os.listdir(os.path.join(ROOT, "sim")) if f.endswith(".py"))
    print("  sim/ 合计 %d 处（基线 %d）" % (tot, sum(BASELINE.values())))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
