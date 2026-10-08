#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim/ + engine/ 出处注释棘轮（SIM-FIDELITY.md §3 第 6 条 / REFACTOR-PLAN §3）：
写明“原版出处”的注释数量**只许升不许降**（P2 起 engine/ 也计入，规则按阶段往 engine 搬）。

约定写法：注释/文档字符串里含 `原版`（如 `原版：BP_CardFunctions::SpawnCardToBoard`）。
新增或移植一条规则就把出处写上、基线改大；不许删出处。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 基线（2026-10-03 量得）。只许升。
# ★ P2（2026-10-03）：`sim/state.py` 的真实现端口进 `engine/state.py`（sim 侧只剩 re-export 壳），
#   所以 sim 侧那条基线随搬迁下调、总数记到 engine 侧 —— 合计仍然只升。
# ★ P6（2026-10-06）：`engine/triggers.py` 拆成 triggers / triggers_specs / triggers_families；15 处出处全在 families（合计不变）。
# ★ P2：`sim/dispatch.py` 的真实现端口进 `engine/dispatch.py`（sim 侧只剩 re-export 壳）。
BASELINE = {"sim/chain.py": 0, "sim/dispatch.py": 0, "sim/engine.py": 33, "sim/state.py": 0,
            "engine/state.py": 9, "engine/dispatch.py": 4, "engine/chain.py": 4, "engine/natives/cards.py": 1,
            "engine/triggers.py": 0, "engine/triggers_specs.py": 0, "engine/triggers_families.py": 15, "engine/effectvm.py": 13, "engine/natives/stats.py": 25,
            "engine/__init__.py": 2, "engine/natives/__init__.py": 2, "engine/natives/board.py": 3,
            "engine/natives/damage.py": 1, "engine/natives/status.py": 1, "engine/natives/kredits.py": 1,
            "engine/natives/bond.py": 9}
fails = 0


def count(path):
    return sum("原版" in ln for ln in open(os.path.join(ROOT, path), encoding="utf-8"))


def count_dir(rel):
    tot = 0
    for dp, _dn, fn in os.walk(os.path.join(ROOT, rel)):
        for f in fn:
            if f.endswith(".py"):
                tot += count(os.path.relpath(os.path.join(dp, f), ROOT))
    return tot


def main():
    global fails
    for p, base in BASELINE.items():
        n = count(p)
        ok = n >= base
        print("  [%s] %s 出处注释 %d ≥ 基线 %d" % ("PASS" if ok else "FAIL", p, n, base))
        fails += (not ok)
    tot = count_dir("sim") + count_dir("engine")
    print("  sim/ + engine/ 合计 %d 处（基线 %d）" % (tot, sum(BASELINE.values())))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
