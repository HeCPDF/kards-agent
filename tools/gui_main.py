#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui_main.py —— 打包版（PyInstaller，`kards-agent.exe`）的**唯一入口**；开发布局下也能直接跑。

一个 exe 兼任三个角色（用户解压后双击就能用，不装 Python）：

    kards-agent.exe                 控制面板（等价 `python -m gui.app`）
    kards-agent.exe --listener      常驻监听器（等价 `python tools/live_session.py`；面板的“启动监听器”就起它）
    kards-agent.exe --selfcheck     离线自检：import 闭包里每个模块、渲染 JS 模板、查 build_tables.json、
                                    查 frida/numpy/tkinter；**不碰游戏、不 attach**；全过退出码 0
    kards-agent.exe --probe         秒退 0（面板启动监听器前的“解释器探针”，见 gui.core.python_probe）

为什么是单 exe + 子命令：面板用 `subprocess.Popen(gui.core.listener_cmd(...))` 起监听器，冻结后没有“另一个 python”可用，
只能再起自己；`gui.watcher.PS_QUERY` 按命令行里的 `--listener` 找到它。exe 是窗口子系统（无控制台），
监听器的 stdout/stderr 由面板重定向进 `<data>/live/live_stdout.txt`，同时 live_session 自己写 `live_log.txt`。

其余参数原样交给面板的 argparse（`--debug` / `--selftest` / `--width` …）。
环境变量：`KARDS_GUI_SELFTEST_EXIT_MS=<毫秒>`：面板窗口建好后自动关闭（无头冒烟用，退出码 0）。
"""
from __future__ import annotations

import os
import sys
import traceback

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(HERE)

if not FROZEN:
    # 开发布局：把仓库根和 tools/ 放进 sys.path（`live_session` 是脚本式的 `import _bootstrap`）。冻结包里这两个名字
    # 由 build_exe.py 以 hidden-import 带进 PYZ，不需要路径。
    for _p in (AGENT_ROOT, HERE):
        if _p not in sys.path:
            sys.path.insert(0, _p)

#: 自检里允许“缺依赖就跳过”的模块（可选依赖，不随包发；缺了不算失败）。
OPTIONAL_MODULES: dict = {}
#: 不在 gui_closure 里、但运行期靠 sys.path 注入按名字 import 的模块（`from canplay import ...`），打包时作为顶层模块带上。
EXTRA_TOPLEVEL = ("canplay", "_bootstrap")


# ---------------------------------------------------------------- 窗口子系统下的输出
def _ensure_stdio(log_path: str = None) -> None:
    """窗口子系统的 exe 在没有重定向时 `sys.stdout/stderr` 是 None（print 静默丢）。监听器模式下
    接到 `live_stdout.txt`，让 traceback 至少有处可去；面板模式不动。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        path = log_path
        if path is None:
            from base import paths as P
            P.ensure_dirs()
            path = P.LIVE_STDOUT
        f = open(path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    except Exception:                                             # noqa: BLE001
        pass


def _attach_parent_console() -> None:
    """`--selfcheck` 要把结果打给人看：窗口子系统的 exe 默认没有控制台，从 cmd/PowerShell 里手动跑时附着父控制台
    （被重定向到文件/管道时 stdout 本来就有效，不动）。"""
    if sys.stdout is not None:
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):              # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
    except Exception:                                             # noqa: BLE001
        pass


# ---------------------------------------------------------------- 自检
def closure_modules() -> list:
    """自检要 import 的模块名。开发：`tools/gui_closure.py` 现算；冻结：读打包时落下的 `closure_modules.txt`。"""
    if FROZEN:
        p = os.path.join(getattr(sys, "_MEIPASS", AGENT_ROOT), "closure_modules.txt")
        with open(p, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    import gui_closure
    return sorted(gui_closure.closure())


def selfcheck(modules=None, out=None) -> int:
    """→ 退出码（0 = 全过）。每项一行 `PASS/FAIL/SKIP`，最后一行总结；同时写 `<data>/selfcheck.txt`。"""
    # 绝不读游戏：import 期 `kardsmem.build/board` 会（只读）扫游戏进程内存选表；自检设显式覆盖，彻底跳过。
    os.environ.setdefault("KARDS_BUILD", "current")
    lines, fails = [], []

    def emit(kind, name, extra=""):
        lines.append("%-4s %s%s" % (kind, name, ("  -- " + extra) if extra else ""))
        if kind == "FAIL":
            fails.append(name)

    from base import paths as P
    from base.version import __version__
    emit("INFO", "kards-agent %s  frozen=%s  python=%s" % (__version__, FROZEN, sys.version.split()[0]))
    emit("INFO", "AGENT_ROOT=%s  DATA=%s" % (P.AGENT_ROOT, P.DATA))

    mods = list(modules) if modules is not None else closure_modules() + list(EXTRA_TOPLEVEL)
    import importlib
    n_ok = 0
    for m in mods:
        try:
            importlib.import_module(m)
            n_ok += 1
        except ModuleNotFoundError as e:
            if m in OPTIONAL_MODULES and (e.name or "").split(".")[0] == OPTIONAL_MODULES[m]:
                emit("SKIP", "import " + m, "可选依赖 %s 未随包发" % OPTIONAL_MODULES[m])
            else:
                emit("FAIL", "import " + m, "%s: %s" % (type(e).__name__, e))
        except BaseException as e:                                # noqa: BLE001（SystemExit 等也算失败，不许带崩自检）
            emit("FAIL", "import " + m, "%s: %s" % (type(e).__name__, e))
    emit("PASS" if n_ok else "FAIL", "import 模块 %d/%d" % (n_ok, len(mods)))

    def check(name, fn):
        try:
            emit("PASS", name, str(fn() or ""))
        except BaseException as e:                                # noqa: BLE001
            emit("FAIL", name, "%s: %s" % (type(e).__name__, e))

    def _js():
        from ops import consts
        js = consts.render_js()
        if len(js) < 1000 or "%(" in js:
            raise AssertionError("JS 模板渲染异常（长度 %d）" % len(js))
        return "%d 字符" % len(js)

    def _tables():
        import json
        from kardsmem import build as B
        with open(B.TABLES_JSON, encoding="utf-8") as f:
            d = json.load(f)
        builds = d["builds"]
        if "current" not in builds or not B.RVA["UObject_ProcessEvent"]:
            raise AssertionError("种子表缺 current / RVA 为空")
        return "%d 个构建，当前种子 %s" % (len(builds), B.CURRENT if hasattr(B, "CURRENT") else "?")

    def _frida():
        import frida
        from frida import _frida                                 # noqa: F401  原生扩展必须随包
        return "frida %s（只 import，未 attach）" % frida.__version__

    def _numpy():
        import numpy
        return "numpy %s" % numpy.__version__

    def _tk():
        import tkinter
        r = tkinter.Tcl()
        return "Tcl %s" % r.eval("info patchlevel")

    def _data_dir():
        P.ensure_dirs()
        probe = os.path.join(P.DATA, ".selfcheck_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return P.DATA

    def _watcher_query():
        from gui import core as K, watcher as W
        cmd = K.listener_cmd(sys.executable)
        if FROZEN and cmd[-1] != "--listener":
            raise AssertionError("冻结包的监听器命令应为 [exe, --listener]，实际 %r" % cmd)
        import re
        pat = re.search(r"-match '([^']+)'", W.PS_QUERY).group(1)
        probe = " ".join(cmd) if FROZEN else "python tools/live_session.py"
        if not re.search(pat, probe, re.I):
            raise AssertionError("PS_QUERY 匹配不上监听器命令行 %r" % probe)
        if re.search(pat, W.PS_QUERY, re.I):
            raise AssertionError("PS_QUERY 会匹配到查询自己的 PowerShell")
        return "监听器命令行 %r" % cmd

    check("渲染 JS 模板 (ops/agent.js.tpl)", _js)
    check("种子表 kardsmem/build_tables.json", _tables)
    check("frida 原生扩展", _frida)
    check("numpy", _numpy)
    check("tkinter/Tcl", _tk)
    check("数据目录可写", _data_dir)
    check("监听器命令行 / 进程发现正则", _watcher_query)

    verdict = "自检通过" if not fails else "自检失败：%d 项 %s" % (len(fails), fails[:8])
    lines.append("=" * 8 + " " + verdict + " " + "=" * 8)
    text = "\n".join(lines)
    try:
        P.ensure_dirs()
        with open(os.path.join(P.DATA, "selfcheck.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:                                             # noqa: BLE001
        pass
    try:
        (out or sys.stdout).write(text + "\n")
    except Exception:                                             # noqa: BLE001
        pass
    return 1 if fails else 0


# ---------------------------------------------------------------- 角色分发
def run_listener(rest: list) -> int:
    """等价 `python tools/live_session.py`：它是脚本式的（`import _bootstrap`、模块级建目录），照常 import 后调 `main()`。"""
    sys.argv = [sys.argv[0]] + list(rest)
    _ensure_stdio()
    from tools import live_session
    live_session.main()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if FROZEN:
        import multiprocessing
        multiprocessing.freeze_support()
    if argv and argv[0] == "--probe":
        return 0
    if argv and argv[0] == "--selfcheck":
        _attach_parent_console()
        return selfcheck()
    if argv and argv[0] == "--listener":
        return run_listener(argv[1:])
    from gui import app
    return app.main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                          # noqa: BLE001
        # 窗口子系统下没有控制台，未捕获异常会静默消失：落一份文件再抛。
        try:
            from base import paths as _P
            _P.ensure_dirs()
            with open(os.path.join(_P.DATA, "crash_gui_main.txt"), "a", encoding="utf-8") as _f:
                _f.write(traceback.format_exc() + "\n")
        except Exception:                                          # noqa: BLE001
            pass
        raise
