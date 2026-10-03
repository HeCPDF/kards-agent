#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产代码不得依赖 `_nn_scratch/`（用户 2026-10-03：“为什么这些没进 kards-agent 目录！！为什么这些不清理一下？”）。

`_nn_scratch/` 只放一次性探针脚本。生产部件（监听器、开局闸门、API 缓存…）住在 `kards-agent/`，运行期文件的位置统一由
`agent/paths.py` 给。本测试用 AST 扫生产目录里**非文档字符串**的字符串常量：出现 `_nn_scratch` 就红。
（文档字符串/注释里提到“某个探针脚本验证过”是允许的——那是出处，不是依赖。）
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCAN_DIRS = ("base", "kardsmem", "semantics", "sim", "evaluation", "policy", "ops", "agent", "player",
             "interfaces", "learn", "gui")
SCAN_FILES = ()
# 白名单已清空（2026-10-03：crashdump.py 改用 base.paths.LIVE_LOG）；只许空，不许加
ALLOW = set()
TOOLS_DIR = "tools"

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def hits(path):
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                doc_ids.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids \
                and "_nn_scratch" in node.value:
            out.append((node.lineno, node.value[:60]))
    return out


def py_files():
    for d in SCAN_DIRS + (TOOLS_DIR,):
        for dp, _dn, fn in os.walk(os.path.join(ROOT, d)):
            for f in fn:
                if f.endswith(".py"):
                    yield os.path.join(dp, f)
    for f in SCAN_FILES:
        yield os.path.join(ROOT, f)


def main():
    bad, allowed = [], []
    for p in py_files():
        h = hits(p)
        if not h:
            continue
        rel = os.path.relpath(p, ROOT)
        (allowed if rel in ALLOW else bad).append((rel, h))
    for rel, h in bad:
        print("    ✗ %s: %s" % (rel, h[:3]))
    chk("生产代码（agent/kardsmem/sim/policy/evaluation/gui/ops/tools/…）运行期不引用 _nn_scratch", not bad)
    chk("生产代码已无 _nn_scratch 白名单（工具一律改用 base.paths）", not allowed and not bad,
        str([r for r, _ in allowed]))
    from base import paths as P
    chk("base.paths 的运行期位置都在 kards-data/ 下", all(os.path.abspath(x).startswith(os.path.abspath(P.DATA))
                                                          for x in (P.LIVE_CMD, P.LIVE_LOG, P.API_CACHE, P.HOT_RELOAD_FLAG)))
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import crashdump as CD                                          # noqa: E402
    chk("crashdump 的监听器日志落点 = base.paths.LIVE_LOG",
        os.path.normcase(CD._live_log_path()) == os.path.normcase(P.LIVE_LOG),
        CD._live_log_path())
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
