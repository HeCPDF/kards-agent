#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import 卫生棘轮（P5，2026-10-04）：`import a.b as c` 拿到的**必须真的是模块**。

为什么要有这条（A 在 P5 迁移里抓到的**生产 bug**，离线测试全绿也测不出来）：
    `player/rule.py` 原来写 `import evaluation.value as _va` —— 但 `evaluation/__init__.py:20` 有
    `from .value import DEFAULT_WEIGHTS, delta, value`，把包属性 `evaluation.value` **遮成了函数**
    ⇒ Python 3.7+ 的 `import a.b as c` 走 `getattr(a, 'b')` ⇒ `_va` 拿到的是**函数**、不是模块 ✗。
    `import player.rule` 不报错（import 语句本身合法），`_va.W`/`_va.delta`/`_va.unit_value` 12 处
    却全是**运行期 AttributeError** ⇒ 实机"决策第一步就崩"，而 `tests/run_all.py` 91/91 全绿 ✗。
    （原 `policy/boardeval.py:46` 文件头就写过这条警告 —— 谁也没拿它当守卫 ✗。）

判据（两条）：
  ① **静态+导入**：扫生产包里的 `import a.b as c`，用 `importlib` 复现绑定语义，
     断言绑定出来的是 `types.ModuleType`；不是模块就把 file:line 与"实际绑到了什么"打出来 ✗。
  ② **定向回归**：`player/rule.py` 的那批别名必须解析成**应有的类型**（`_W` 是 dict、
     `_delta`/`_unit_value` 可调用、`_H`/`_U` 是类、`_en`/`_se`/`_ad` 是模块）——
     即"曾经炸过的那 12 处调用点"至少有个东西盯着 ✓。

限制（如实）：只扫**生产包**（`tests/` 不在内 —— 迁移期间测试在改，扫它会互相干扰；且这个 bug 类
是生产代码才致命 ✓）；不分析运行期动态 import（`importlib.import_module(chr(...))` 这类拼出来的名字 ✗）。
"""
import importlib
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PKGS = ("base", "kardsmem", "engine", "ops", "semantics", "sim", "evaluation", "policy",
        "agent", "player", "interfaces", "gui", "tools")
PAT = re.compile(r'^\s*import\s+([A-Za-z_][\w.]*)\s+as\s+([A-Za-z_]\w*)')

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    bad = []
    skipped = []
    scanned = 0
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
                    lines = open(full, encoding="utf-8").read().splitlines()
                except OSError:
                    continue
                for i, ln in enumerate(lines, 1):
                    if ln.strip().startswith("#"):
                        continue
                    m = PAT.match(ln)
                    if not m:
                        continue
                    mod_name, alias = m.group(1), m.group(2)
                    if "." not in mod_name:
                        continue                       # `import os as _os` 不可能被遮蔽 ✓
                    scanned += 1
                    parent, child = mod_name.rsplit(".", 1)
                    try:
                        parent_mod = importlib.import_module(parent)
                    except ImportError as ex:           # 第三方依赖没装（如 torch）⇒ **无法判定**，如实跳过 ✓
                        skipped.append((rel, i, mod_name, str(ex)[:40]))
                        continue
                    except Exception as ex:            # noqa: BLE001
                        bad.append((rel, i, mod_name, alias, "父包 import 失败: %s" % ex))
                        continue
                    # CPython 的 `import a.b as c`：**先**把 `a.b` 导进来，**再** `getattr(a, "b")`，
                    #   取不到才回退 `sys.modules["a.b"]`（PEP 328 / bpo-30024）。
                    #   ⇒ `import ctypes.wintypes as wt`：导入后父包属性就是那个子模块 ✓ 不算 bug；
                    #     而 `evaluation.value` 被同名**函数**遮蔽时 getattr **成功**拿到函数 ⇒ 不回退 ⇒ 正是 bug ✗。
                    try:
                        importlib.import_module(mod_name)
                        bound = getattr(parent_mod, child)
                    except (ImportError, AttributeError):
                        bound = sys.modules.get(mod_name)
                    if not isinstance(bound, types.ModuleType):
                        bad.append((rel, i, mod_name, alias,
                                    "实际绑到了 %s（%r）" % (type(bound).__name__, bound)))
    chk("① 生产代码里 `import a.b as c` 一律绑到**模块**（不是被同名函数/变量遮蔽的对象）",
        not bad, "扫了 %d 处；问题 %d 处：%s；**无法判定（依赖缺失）%d 处**：%s"
        % (scanned, len(bad), bad[:6], len(skipped), skipped[:3]))

    # ② 定向回归：rule.py 那批别名的**类型**
    import player.rule as R
    ok = (isinstance(R._W, dict) and callable(R._delta) and callable(R._unit_value)
          and isinstance(R._H, type) and isinstance(R._U, type)
          and all(isinstance(getattr(R, n), types.ModuleType) for n in ("_en", "_se", "_ad")))
    chk("② `player/rule.py` 的别名解析成应有的类型（`_W` dict / `_delta`,`_unit_value` 可调用 / "
        "`_H`,`_U` 类 / `_en`,`_se`,`_ad` 模块）—— 曾经 12 处运行期炸的就是它们",
        ok, "_W=%s _delta=%s _unit_value=%s _H=%s _U=%s" %
        (type(R._W).__name__, type(R._delta).__name__, type(R._unit_value).__name__,
         type(R._H).__name__, type(R._U).__name__))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
