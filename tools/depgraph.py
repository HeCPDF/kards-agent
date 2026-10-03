# -*- coding: utf-8 -*-
"""依赖图：包 / 顶层模块之间谁 import 谁（只看 kards-agent 自己的代码）。

用法：`python tools/depgraph.py`            → 打印各包行数与包级依赖
      `python tools/depgraph.py --modules`  → 额外打印顶层散文件被谁用
      `python tools/depgraph.py --dot`      → 输出 Graphviz dot（贴到 docs/ 里画图）
"""
from __future__ import annotations

import ast
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"_archive", "__pycache__", "venv", "data", "kards-data", ".git", "dist"}


def py_files(root=ROOT):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                yield os.path.relpath(p, root).replace("\\", "/"), p


def owner(rel: str) -> str:
    """文件属于哪个“单元”：包名；顶层散文件按文件名；测试归 tests。"""
    parts = rel.split("/")
    if len(parts) == 1:
        return "tests" if parts[0].startswith("test_") else parts[0][:-3]
    return parts[0]


def build():
    files = dict(py_files())
    units = {owner(r) for r in files}
    lines = collections.Counter()
    edges = collections.defaultdict(set)
    for rel, p in files.items():
        src = owner(rel)
        text = open(p, encoding="utf-8", errors="replace").read()
        lines[src] += text.count("\n")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names = [n.module.split(".")[0]]
            for nm in names:
                if nm in units and nm != src:
                    edges[src].add(nm)
    return units, lines, edges


def main(argv):
    units, lines, edges = build()
    if "--dot" in argv:
        print("digraph kards_agent {\n  rankdir=LR;")
        for s in sorted(edges):
            for d in sorted(edges[s]):
                if s != "tests":
                    print('  "%s" -> "%s";' % (s, d))
        print("}")
        return 0
    print("单元（行数）：")
    for u, n in lines.most_common():
        print("  %-14s %7d" % (u, n))
    print("\n依赖（不含 tests）：")
    for s in sorted(edges):
        if s != "tests":
            print("  %-12s -> %s" % (s, ", ".join(sorted(edges[s]))))
    if "--modules" in argv:
        users = collections.defaultdict(set)
        for s, ds in edges.items():
            for d in ds:
                users[d].add(s)
        print("\n谁用它：")
        for u in sorted(units):
            print("  %-14s <- %s" % (u, ", ".join(sorted(users.get(u, ())))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
