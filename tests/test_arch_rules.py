#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖方向测试 —— `EVAL-ARCHITECTURE.md` §3.1 / §1.1 补充 0。

规则（架构标准，不是建议）：
  * `sim/`（L2 规则）**不得** import `evaluation` / `policy` / `player.rule`；
  * `evaluation/`（L3 价值）只许依赖 `sim.state` 的**数据结构**，
    不得 import `sim` 的规则模块，也不得 import `policy` / `player.rule`；
  * `policy/boardeval.py` 是过渡期实现，`sim` / `evaluation` 通过**白名单**转调它 ——
    白名单写死在本文件里，**加一条就得改这个测试**（不允许悄悄长出来）。

再跑一遍真实 import：两个包没有循环依赖、转调结果与 boardeval 一致（行为不变）。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 过渡期桥（§1.1 补充 0：boardeval 原样保留、新边界转调）。改动本表 = 架构变更。
# ★ 2026-10-03：真实现已迁入 sim/、evaluation/、policy/，`policy.boardeval` 只剩兼容门面 ⇒ 桥清空（任何新桥都得先改这里）。
BRIDGE_ALLOW = {}
FORBID_SIM = ("evaluation", "policy", "player.rule", "policy.boardeval")
FORBID_EVAL = ("policy", "player.rule", "policy.boardeval")
EVAL_ALLOW_SIM = ("sim.state",)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def imports_of(path, pkg):
    """该文件顶层 import 的模块名集合（相对 import 按包名展开）。"""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                mod = "%s.%s" % (pkg, node.module) if node.module else pkg
                out.add(mod)
            elif node.module:
                out.add(node.module)
    return out


def scan(pkg):
    d = os.path.join(ROOT, pkg)
    out = {}
    for dirpath, _dirnames, files in os.walk(d):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                out[rel] = imports_of(p, pkg)
    return out


def hits(imps, prefixes):
    return sorted({m for m in imps
                   for pre in prefixes
                   if m == pre or m.startswith(pre + ".")})


# ---------------------------------------------------------------------------
# 分层依赖表（docs/STRUCTURE.md）：每个包只许 import 表里列的包。加一条边 = 架构变更，必须改这里。
# `if __name__ == "__main__":` 里的 import（自检 / 命令行入口）不算。
# ---------------------------------------------------------------------------
LAYERS = {
    "base": set(),
    "kardsmem": {"base"},
    "ops": {"base", "kardsmem"},
    "semantics": {"base", "kardsmem"},
    "sim": {"base", "kardsmem"},
    "evaluation": {"sim"},
    "policy": {"sim", "evaluation", "kardsmem", "base"},
    "learn": {"base"},
    "agent": {"base", "kardsmem", "ops", "semantics"},
    "player": {"base", "kardsmem", "ops", "semantics", "sim", "evaluation", "policy", "agent", "learn"},
    "interfaces": {"base", "kardsmem", "agent"},
    "gui": {"base", "kardsmem", "agent", "player"},
}


def layer_imports(path):
    """该文件里（不含 `__main__` 守卫块）import 的顶层包名集合。"""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)

    def is_main_guard(n):
        return (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name)
                and n.test.left.id == "__name__")

    out = set()

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if is_main_guard(ch):
                continue
            if isinstance(ch, ast.Import):
                out.update(a.name.split(".")[0] for a in ch.names)
            elif isinstance(ch, ast.ImportFrom) and ch.level == 0 and ch.module:
                out.add(ch.module.split(".")[0])
            walk(ch)
    walk(tree)
    return out


def check_layers():
    known = set(LAYERS)
    bad = []
    for pkg, allowed in LAYERS.items():
        d = os.path.join(ROOT, pkg)
        for dirpath, _dn, files in os.walk(d):
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dirpath, f)
                    got = (layer_imports(p) & known) - {pkg}
                    extra = got - allowed
                    if extra:
                        bad.append((os.path.relpath(p, ROOT).replace("\\", "/"), sorted(extra)))
    chk("分层依赖：每个包只 import 依赖表里允许的包（见 docs/STRUCTURE.md）", not bad, str(bad))
    tops = {n for n in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, n)) and os.path.exists(os.path.join(ROOT, n, "__init__.py"))}
    undeclared = sorted(tops - known - {"tools", "tests"})
    chk("每个顶层包都在依赖表里登记（新包要先登记）", not undeclared, str(undeclared))


def main():
    check_layers()
    sim_files = scan("sim")
    ev_files = scan("evaluation")
    chk("扫到 sim/ 与 evaluation/ 的源码", bool(sim_files) and bool(ev_files),
        "sim=%d evaluation=%d" % (len(sim_files), len(ev_files)))

    bad = []
    for rel, imps in sorted(sim_files.items()):
        allow = BRIDGE_ALLOW.get(rel, set())
        bad += [(rel, m) for m in hits(imps, FORBID_SIM) if m not in allow]
    chk("sim/ 不 import evaluation/policy/player.rule", not bad, str(bad))

    bad = []
    for rel, imps in sorted(ev_files.items()):
        allow = BRIDGE_ALLOW.get(rel, set())
        ev_hits = [m for m in hits(imps, ("sim",))
                   if not (m == "sim.state" or m.startswith("sim.state."))]
        ev_hits += hits(imps, FORBID_EVAL)
        bad += [(rel, m) for m in ev_hits if m not in allow]
    chk("evaluation/ 只依赖 sim.state（+白名单桥）", not bad, str(bad))

    # policy/（L4）可以依赖 sim 与 evaluation，但不得反向依赖 L5 编排（player.rule / player.loop / agent.session）
    pol_files = scan("policy")
    badp = [(rel, hits(imps, ("player.rule", "player.loop", "agent.session", "policy.boardeval")))
            for rel, imps in pol_files.items() if hits(imps, ("player.rule", "player.loop", "agent.session", "policy.boardeval"))]
    chk("policy/ 不 import player.rule/nn/session/boardeval（L4 不反向依赖 L5/门面）", not badp and bool(pol_files), str(badp))
    # 白名单本身也要被钉住：多一条就是架构变更，必须显式改测试
    used = sorted((rel, sorted(m)) for rel, m in BRIDGE_ALLOW.items())
    chk("过渡期桥白名单已清空（2026-10-03：真实现在 sim/evaluation/policy，boardeval 只是门面）", used == [], str(used))

    # 真实 import（循环依赖会在这里炸）
    sys.path.insert(0, ROOT)
    try:
        import policy.boardeval as B
        import evaluation
        import sim
        chk("import sim / evaluation / policy.boardeval 都能过", True)
    except Exception as e:                                     # noqa: BLE001
        chk("import sim / evaluation / policy.boardeval 都能过", False, repr(e))
        print("\n结论：FAIL（%d 项失败）" % fails)
        return 1

    st = sim.State({}, {"local": 20, "enemy": 20}, 5.0)
    chk("sim.State 是 sim.state.Sim 的子类，且门面 boardeval.Sim 就是它", isinstance(st, B.Sim) and B.Sim.__module__ == "sim.state")
    chk("evaluation.value ≡ boardeval.evaluate（行为不变）",
        evaluation.value(st) == B.evaluate(st))
    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
