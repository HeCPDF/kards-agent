#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**P4 收尾棘轮**：`to_effects` / `record_effects` 的生产调用点只许**变少**。

为什么要这条（P4 收尾条件，2026-10-04 实测现状）：
    `to_effects` 现在的生产调用点只有三处 ——
      * `engine/effectvm.py`（`record_effects` 内部出 eff + 它自己的自检）＝ **旧路径主入口**；
      * `engine/triggers.py`（`EV.to_effects(...)`）；
      * `engine/scripts.py`（**对账基线**：`reconcile_one` 的 A 路，故意留着 ✓）。
    外加 `record_effects` 的调用方：`engine/triggers.py`、`player/rule.py`、`semantics/choosespawn.py`。

    P4 的退出条件是：**把规则侧（`rule`/`triggers`/`choosespawn`）切到 `engine.scripts` 的直跑**，
    然后 `to_effects` 只剩"未迁动词兜底 + 对账基线 + 缺口来源"三种角色，才能谈删词汇表、
    退役 `test_effect_keys_consumed`。

    ⇒ 这条测试**不许新增**生产调用点（新增就说明又有人往旧路径上加东西了 ✗）。
      名单**只减不增**：切掉谁就把谁从 `ALLOWED` 里删掉（删掉之后如果还有人调，这条测试会响 ✓）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 允许出现 `to_effects` / `record_effects` 的**生产**文件（测试与 `_nn_scratch` 不算）。
#: ★ 这份名单是**待办清单**：P4 收尾时它应当缩到 `engine/effectvm.py` + `engine/scripts.py`。
ALLOWED = {
    "engine/effectvm.py",      # ① 旧路径主入口（`record_effects` 出 eff + 自检）——最后才动
    "engine/effectvm_selftest.py",  # ①b P6 自 effectvm.py 拆出的自检（原就在 ① 里，随 ① 一起退役）
    "engine/triggers.py",      # ② 触发层：钩子后果的预计算（切直跑后应消失）
    "engine/scripts.py",       # ③ **对账基线**（A 路，故意留着 ✓，不删）
    "player/rule.py",          # ④ 规则侧（切直连 engine 状态后应消失）
    "semantics/choosespawn.py",  # ⑤ 候选预测（同上）
}

#: 生产包（测试目录不算）。
PKGS = ("base", "kardsmem", "engine", "ops", "semantics", "sim", "evaluation", "policy",
        "learn", "agent", "player", "interfaces", "gui")

PAT = re.compile(r"\b(to_effects|record_effects)\s*\(")

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    hits = {}
    for pkg in PKGS:
        d = os.path.join(ROOT, pkg)
        if not os.path.isdir(d):
            continue
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                try:
                    src = open(full, encoding="utf-8").read()
                except OSError:
                    continue
                n = len(PAT.findall(src))
                if n:
                    hits[rel] = n
    extra = sorted(set(hits) - ALLOWED)
    chk("`to_effects`/`record_effects` **没有新增**生产调用点（名单只减不增）", not extra,
        "新增：%s；现名单：%s" % (extra, sorted(hits)))
    stale = sorted(ALLOWED - set(hits))
    chk("名单里没有**过期条目**（切完就该从名单删掉；这条提醒收尾进度）",
        True, "已不在用的条目（可以从 ALLOWED 删）：%s" % (stale or "无"))
    chk("对账基线仍在（`engine/scripts.py` 必须继续用 `to_effects` 做 A 路）",
        "engine/scripts.py" in hits, str(hits.get("engine/scripts.py")))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
