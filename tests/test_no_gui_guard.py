#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**无窗口棘轮**：跑测试绝不许弹出 GUI 窗口。

为什么要这条（2026-10-04，用户要求）：
    `tests/test_gui_parity.py` 末尾的「面板冒烟」原来**无条件**建了一个 Tk 顶层窗口并
    `update()` —— 在没有显示器（CI）的机器上抛 `TclError` 被跳过了，可**在用户的机器上
    （有显示器）每次跑测试都真弹一个窗口** ✗。用户明确要求：跑测试不许造成弹窗。
    ⇒ 改成显式 opt-in（`KARDS_TEST_GUI_WINDOW=1`），并加这条棘轮钉住"以后也别再犯"。

判据（故意做得**简单可行**，宁可漏报也不要误报）：
    扫 `tests/` 下所有 `.py`（跳过本文件自己）：
      ① **硬判据**：某行出现「会建窗口 / 起 GUI」的字样 ⇒ 该文件里必须**同时**出现
         `KARDS_TEST_GUI_WINDOW` 的**实际用法**（`os.environ.get(...)` / `os.environ[...]` /
         `os.getenv(...)` / `"...": "0"` 这种赋值/字典键 / `... in os.environ`）——
         光在注释里提一句不算。也就是说：**要么别写，要么写在显式 opt-in 判定旁边**。
      ② **次序判据**：该文件里**第一处**"建窗口"字样必须排在**第一处**闸门用法**之后**
         （纯注释行不计，免得"文档里写了个 tkinter"就误报）——这就是"只能出现在
          `KARDS_TEST_GUI_WINDOW` 环境变量判定之后"的字面落地。
      ③ 两条**正向**断言（防"把门焊死"）：`run_all.py` 必须给子进程**显式**设
         `KARDS_TEST_GUI_WINDOW=0` 并 `env=` 传下去；`test_gui_parity.py` 的**纯函数**部分
         （`gui/core.py` / `gui/update_check.py`）必须仍是**模块级无条件** import。

**它抓不到什么（限制，如实写在这里，别当成"覆盖全了"）**：
    * 只认**字面**写法。"建窗口"的手段一旦绕开这些字样就抓不到 —— 例如
      `ctypes` 直接调 Win32 `CreateWindowExW`/`user32`、`os.startfile` 起一个外部 exe、
      `webbrowser`、`importlib.import_module(chr(103)+...)` 这种把模块名拼出来的写法、
      或换别的 GUI 库。**这条棘轮不是"证明不弹窗"，只是一条防复发的绊线。**
    * 抓不到**运行时**才决定建不建窗口的代码（只保证"字样出现时旁边有闸门字样"）。
    * 抓不到**别人进程**弹的窗口（比如某个测试 `subprocess` 起一个脚本去开面板），
      除非那个脚本名/命令行里带上了上面这些字样。
    * 只扫 `tests/`，**不扫** `gui/`（那儿本来就是窗口本体）、``、`tools/`。
"""
import os
import re
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(os.path.abspath(__file__))

#: 环境变量名（闸门）。**只有它**是"允许建窗口"的开关。
GATE = "KARDS_TEST_GUI_WINDOW"

#: 闸门的**实际用法**（不是"注释里提了一句"）：命中之一才算过了闸门。
GATE_USE = re.compile(
    r"""os\.environ(?:\.get)?\s*[\(\[]\s*['"]%s['"]      # os.environ.get("...") / os.environ["..."]
      | os\.getenv\s*\(\s*['"]%s['"]                    # os.getenv("...")
      | ['"]%s['"]\s*:                                  # {"...": "0"} / "...": 赋值
      | ['"]%s['"]\s*(?:not\s+)?in\s+os\.environ        # "..." in os.environ
    """ % (GATE, GATE, GATE, GATE),
    re.X)

#: 「会建窗口 / 起 GUI」的字样（名字, 正则, 为什么危险）。
TOKENS = (
    ("tkinter", re.compile(r"\btkinter\b"), "tk 本体（import 出来就可能建窗口）"),
    ("Tk()", re.compile(r"\bTk\s*\("), "建 Tk 顶层窗口"),
    ("Toplevel()", re.compile(r"\bToplevel\s*\("), "建 Tk 子窗口"),
    ("mainloop()", re.compile(r"\bmainloop\s*\("), "进 GUI 事件循环（会一直占着屏幕）"),
    ("messagebox", re.compile(r"\bmessagebox\b", re.I), "弹对话框"),
    ("gui.app / gui/app", re.compile(r"\bgui[./\\]app\b"), "面板本体（含 App(tk.Tk)）"),
    ("python -m gui", re.compile(r"-m\s+gui\b"), "命令行起面板"),
    ("run_gui", re.compile(r"\brun_gui\b"), "面板启动脚本（run_gui.bat）"),
    ("live_session", re.compile(r"\blive_session\b"), "常驻监听器（连着游戏/工具链，不该在测试里起）"),
    ("PyQt/PySide/wx", re.compile(r"\b(?:PyQt5|PyQt6|PySide2|PySide6|wx)\b\.?"), "别的 GUI 库"),
    ("CreateWindow", re.compile(r"\bCreateWindow\w*\b"), "Win32 直接建窗口（ctypes）"),
    ("startfile/webbrowser", re.compile(r"\b(?:os\.startfile|webbrowser)\b"), "起外部 GUI/浏览器"),
)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def scan():
    """扫 `tests/` ⇒ 每个**有命中**的文件一条记录：

    `{"rel": 相对名, "gate_at": 第一处闸门用法行号 | None, "hits": [(行号, 字样, 是否纯注释行)]}`
    """
    out = []
    for dirpath, dirs, files in os.walk(TESTS):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in sorted(files):
            if not fn.endswith(".py") or fn == SELF:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, TESTS).replace("\\", "/")
            lines = _read(full).splitlines()
            gate_at = next((i for i, ln in enumerate(lines, 1) if GATE_USE.search(ln)), None)
            hits = []
            for i, line in enumerate(lines, 1):
                for name, pat, _why in TOKENS:
                    if pat.search(line):
                        hits.append((i, name, line.lstrip().startswith("#")))
                        break                    # 一行只报第一个字样，够用了
            if hits:
                out.append({"rel": rel, "gate_at": gate_at, "hits": hits})
    return out


def _fmt(rel, i, name):
    return "tests/%s:%d %s" % (rel, i, name)


def main():
    recs = scan()
    hits = [(r["rel"], i, name, cmt) for r in recs for (i, name, cmt) in r["hits"]]
    print("  -- tests/ 下「会建窗口/起 GUI」字样的命中（file:line 字样）--")
    for rel, i, name, cmt in hits:
        print("     %s%s" % (_fmt(rel, i, name), "   ← 纯注释行" if cmt else ""))
    if not hits:
        print("     （无命中）")

    # ① 硬判据：文件里出现"建窗口"字样 ⇒ 该文件必须**同时**有闸门的实际用法
    ungated = [(r["rel"], i, name) for r in recs if r["gate_at"] is None for (i, name, _c) in r["hits"]]
    chk("没有『没过 %s 闸门就写建窗口字样』的文件（硬判据）" % GATE, not ungated,
        "未过闸门：%s" % ([_fmt(*t) for t in ungated] or "无"))
    # ② 次序判据：第一处"建窗口"字样必须排在第一处闸门用法**之后**（纯注释行不算）
    bad = [(r["rel"], i, name) for r in recs if r["gate_at"] is not None
           for (i, name, cmt) in r["hits"] if not cmt and i < r["gate_at"]]
    chk("建窗口字样**都在** %s 判定之后（次序判据）" % GATE, not bad,
        "排在闸门之前：%s" % ([_fmt(*t) for t in bad] or "无"))

    # ---- 正向断言：防"把门焊死"----
    ra = _read(os.path.join(TESTS, "run_all.py"))
    chk("run_all.py 给子进程**显式**设 %s=0 且 env= 传下去" % GATE,
        re.search(r"""['"]%s['"]\s*:\s*['"]0['"]""" % GATE, ra) is not None
        and "env=CHILD_ENV" in ra and "subprocess.run(" in ra,
        "找到 CHILD_ENV 定义=%s，env= 传参=%s" % (bool(re.search(r"CHILD_ENV\s*=", ra)), "env=CHILD_ENV" in ra))
    gp = _read(os.path.join(TESTS, "test_gui_parity.py"))
    chk("test_gui_parity.py：纯函数部分仍**无条件**跑（gui.core / gui.update_check 是模块级 import）",
        re.search(r"^from gui import core\b", gp, re.M) is not None
        and re.search(r"^from gui import update_check\b", gp, re.M) is not None,
        "闸门用法=%s" % bool(GATE_USE.search(gp)))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
