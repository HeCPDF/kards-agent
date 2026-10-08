#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui_closure.py —— 求控制面板（GUI）运行所需代码的 import 闭包（供 build_exe 打包、gui_main 自检用）。

入口 = 面板 `gui.app` + 面板拉起的常驻监听器 `tools.live_session`。闭包按 AST 求（函数体内的延迟 import 也算）。
`--list` 直接打印闭包。
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRIES = ("gui.app", "tools.live_session")


def _mod_path(mod: str, root: str):
    base = os.path.join(root, *mod.split("."))
    if os.path.isfile(base + ".py"):
        return base + ".py"
    if os.path.isfile(os.path.join(base, "__init__.py")):
        return os.path.join(base, "__init__.py")
    return None


def _imports(path: str, mod: str, is_pkg: bool):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError):
        return
    pkg = mod if is_pkg else mod.rpartition(".")[0]
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                parts = pkg.split(".") if pkg else []
                up = parts[:len(parts) - (n.level - 1)] if n.level > 1 else parts
                m = ".".join(up + ([n.module] if n.module else []))
            else:
                m = n.module or ""
            if m:
                yield m
                for a in n.names:               # from pkg import submodule
                    yield m + "." + a.name
        elif isinstance(n, ast.Call):           # importlib.import_module("x.y") / __import__("x.y")
            f = n.func
            nm = getattr(f, "attr", None) or getattr(f, "id", None)
            if nm in ("import_module", "__import__") and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                yield n.args[0].value


def closure(root: str = ROOT, entries=ENTRIES) -> dict:
    """→ {模块名: 文件相对路径}。只收仓库内的模块（标准库/第三方自然排除）。"""
    seen, todo = {}, list(entries)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        p = _mod_path(m, root)
        if p is None:
            continue
        seen[m] = os.path.relpath(p, root).replace("\\", "/")
        is_pkg = p.endswith("__init__.py")
        # 父包的 __init__ 也会被执行
        parts = m.split(".")
        for i in range(1, len(parts)):
            todo.append(".".join(parts[:i]))
        pkg_name = m if is_pkg else m.rpartition(".")[0]
        for dep in _imports(p, m, is_pkg):
            segs = dep.split(".")
            # 脚本式同目录 import（`tools/live_session.py` 里的 `import _bootstrap`）：按所在包补前缀
            if pkg_name and _mod_path(pkg_name + "." + segs[0], root):
                todo.append(pkg_name + "." + segs[0])
            for i in range(len(segs), 0, -1):    # 最长的、真实存在的前缀
                cand = ".".join(segs[:i])
                if _mod_path(cand, root):
                    todo.append(cand)
                    break
    return seen


if __name__ == "__main__":
    c = closure()
    if "--list" in sys.argv:
        for k, v in sorted(c.items()):
            print(k, v)
    print("模块 %d 个" % len(c), file=sys.stderr)
