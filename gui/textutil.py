# -*- coding: utf-8 -*-
"""文本控件的小工具：只读但**可选、可复制**的 Text（`state="disabled"` 的 Text 点不进焦点，Ctrl+C 不生效），输入框的右键菜单。

只读 Text 的做法：控件始终保持 `state="normal"`（这样能聚焦、能选、能复制），用按键绑定拦掉一切修改（键入、删除、粘贴、剪切），
放行方向键/翻页/Ctrl+C/Ctrl+A/Ctrl+Insert。程序往里写字就直接 `insert`/`delete`（不用再来回切 state）。
"""
from __future__ import annotations

import tkinter as tk
from typing import Optional

_NAV = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Shift_L", "Shift_R", "Control_L", "Control_R"}


def ro_key(keysym: str, state: int) -> Optional[str]:
    """只读 Text 的按键判定：返回 `"break"` ⇒ 拦掉，`None` ⇒ 放行。（纯函数，离线可测）"""
    ctrl = bool(state & 0x4)
    if keysym in _NAV:
        return None
    if ctrl and keysym.lower() in ("c", "a", "insert"):
        return None
    return "break"


def make_readonly(w: tk.Text) -> tk.Text:
    """把 Text 变成只读但可选可复制；带右键菜单（复制 / 全选）。"""
    w.configure(state="normal", cursor="xterm")

    def on_key(e):
        return ro_key(e.keysym, e.state)

    def select_all(_e=None):
        w.tag_add("sel", "1.0", "end-1c")
        return "break"

    def copy(_e=None):
        try:
            txt = w.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        w.clipboard_clear()
        w.clipboard_append(txt)
        return "break"

    w.bind("<Key>", on_key)
    w.bind("<<Paste>>", lambda e: "break")
    w.bind("<<Cut>>", lambda e: "break")
    w.bind("<Control-a>", select_all)
    w.bind("<Control-A>", select_all)
    w.bind("<Button-1>", lambda e: w.focus_set())
    menu = tk.Menu(w, tearoff=0)
    menu.add_command(label="复制", command=copy)
    menu.add_command(label="全选", command=select_all)
    w.bind("<Button-3>", lambda e: (w.focus_set(), menu.tk_popup(e.x_root, e.y_root)))
    return w


def set_text(w: tk.Text, text: str, keep_view: bool = False) -> None:
    """整体替换只读 Text 的内容（不动选区之外的状态；`keep_view` ⇒ 保持滚动位置）。"""
    top = w.yview()[0] if keep_view else None
    w.delete("1.0", "end")
    w.insert("end", text)
    if top is not None:
        w.yview_moveto(top)


def add_entry_menu(e) -> None:
    """输入框右键菜单（剪切/复制/粘贴/全选）+ Ctrl+A。Tk 在 Windows 上的 Entry 本来就支持 Ctrl+C/V/X，这里补菜单和全选。"""
    menu = tk.Menu(e, tearoff=0)
    menu.add_command(label="剪切", command=lambda: e.event_generate("<<Cut>>"))
    menu.add_command(label="复制", command=lambda: e.event_generate("<<Copy>>"))
    menu.add_command(label="粘贴", command=lambda: e.event_generate("<<Paste>>"))
    menu.add_command(label="全选", command=lambda: (e.select_range(0, "end"), e.icursor("end")))
    e.bind("<Button-3>", lambda ev: (e.focus_set(), menu.tk_popup(ev.x_root, ev.y_root)))
    e.bind("<Control-a>", lambda ev: (e.select_range(0, "end"), e.icursor("end"), "break")[-1])
