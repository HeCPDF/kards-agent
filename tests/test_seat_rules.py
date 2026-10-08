#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""座位规则（docs/SEAT-MIGRATION.md）：生产代码里不许再有 local / enemy 座位。

游戏自己只有两个座位枚举（先手 left=1 / 后手 right=2）和 `mySide`；“我方”是 `side == mySide` 的谓词，不是第三种座位。
AST 检查（不是 grep：注释、文档字符串、`"target": "enemy"` 这种『目标阵营关系词』不算）：

* 名字 `LOCAL` / `ENEMY`（大小写精确）
* 比较里出现字符串 `"local"` / `"enemy"`（`x == "local"`、`x in ("local", "enemy")`）
* 字典下标 / `.get()` 用 `"local"` / `"enemy"` 当键（`st.kredits["local"]`、`hq.get("enemy")`）
* `.location` 与位置字符串比较（`c.location == "hand"`、`in ("frontline", "back")`）
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PKGS = ("base", "kardsmem", "ops", "semantics", "sim", "evaluation", "policy", "agent", "learn", "player",
        "interfaces", "gui", "tools")
SEAT_WORDS = {"local", "enemy"}
LOC_WORDS = {"hand", "frontline", "back", "hq", "discard", "deck"}
# 已知的合法例外：(相对路径, 行号范围内的说明)。迁移期可以临时登记，但每条都要写理由。
ALLOW: dict = {}
# 不是座位的同名词：VM 变量种类 `ref.kind == "local"`；效果『目标阵营关系词』("target": "enemy"/"friend"/"any")。
ALLOW_TEXT = (("kardsmem/vm.py", "ref.kind"), ("policy/search.py", "side in (\"enemy\""))


def _strs(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out = set()
        for e in node.elts:
            out |= _strs(e)
        return out
    return set()


def _is_location_attr(node):
    return isinstance(node, ast.Attribute) and node.attr == "location"


def scan(path):
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    # `if __name__ == "__main__":` 里的自检 / 命令行不算
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name) \
                and n.test.left.id == "__name__":
            for sub in ast.walk(n):
                skip.add(id(sub))
    hits = []
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.Name) and n.id in ("LOCAL", "ENEMY"):
            hits.append((n.lineno, "名字 %s" % n.id))
        elif isinstance(n, ast.Compare):
            parts = [n.left] + list(n.comparators)
            words = set()
            for p in parts:
                words |= _strs(p)
            if words & SEAT_WORDS:
                hits.append((n.lineno, "比较里的字符串座位 %s" % sorted(words & SEAT_WORDS)))
            if any(_is_location_attr(p) for p in parts) and words & LOC_WORDS:
                hits.append((n.lineno, ".location 与位置字符串比较 %s" % sorted(words & LOC_WORDS)))
        elif isinstance(n, ast.Attribute) and n.attr == "location" and isinstance(n.ctx, ast.Load)                 and not (isinstance(n.value, ast.Name) and n.value.id in ("self", "EType", "_GM", "GM")):
            hits.append((n.lineno, "读 .location（Card.location 已删；用 obj.Location / 谓词）"))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None]:
                if _strs(d) & SEAT_WORDS:
                    hits.append((n.lineno, "函数 %s 的默认参数是字符串座位" % n.name))
        elif isinstance(n, ast.keyword) and n.arg in ("side", "sides", "my_side", "playing_side", "seat")                 and _strs(n.value) & SEAT_WORDS:
            hits.append((n.value.lineno, "关键字 %s= 传字符串座位" % n.arg))
        elif isinstance(n, ast.Subscript):
            if _strs(n.slice) & SEAT_WORDS:
                hits.append((n.lineno, "用字符串座位当下标"))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "setdefault", "pop") \
                and n.args and _strs(n.args[0]) & SEAT_WORDS:
            hits.append((n.lineno, "用字符串座位当 %s 的键" % n.func.attr))
    return hits


def main():
    bad = []
    for pkg in PKGS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, pkg)):
            dn[:] = [d for d in dn if d not in ("__pycache__", "venv")]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                for ln, why in scan(p):
                    if (rel, ln) in ALLOW:
                        continue
                    line = open(p, encoding="utf-8").read().splitlines()[ln - 1]
                    if any(rel == r and t in line for r, t in ALLOW_TEXT):
                        continue
                    bad.append("%s:%d  %s" % (rel, ln, why))
    if "--list" in sys.argv:
        print("\n".join(bad))
    print("座位残留 %d 处" % len(bad))
    if bad and "--list" not in sys.argv:
        print("\n".join(bad[:60]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
