#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctl.py —— 轻量控制原语（客户区坐标进，内部转屏幕坐标）。

  python ctl.py click <cx> <cy>       # 点击客户区坐标
  python ctl.py move <cx> <cy>        # 移动光标
  python ctl.py drag <x0> <y0> <x1> <y1> [settle]
  python ctl.py park                  # 光标停到安全点
"""
import sys
import time

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import win  # noqa: E402
import actions  # noqa: E402


def get_hwnd():
    win.set_dpi_aware()
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    ws = win.find_by_process("kards")
    return ws[0]["hwnd"] if ws else None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    hwnd = get_hwnd()
    cmd = sys.argv[1]
    if cmd == "click":
        cx, cy = int(sys.argv[2]), int(sys.argv[3])
        sx, sy = win.client_to_screen(hwnd, cx, cy)
        ok = actions.click(sx, sy)
        print("click client(%d,%d) -> screen(%d,%d) -> %s" % (cx, cy, sx, sy, ok))
    elif cmd == "move":
        cx, cy = int(sys.argv[2]), int(sys.argv[3])
        sx, sy = win.client_to_screen(hwnd, cx, cy)
        ok = actions.set_cursor(sx, sy)
        print("move client(%d,%d) -> screen(%d,%d) -> %s" % (cx, cy, sx, sy, ok))
    elif cmd == "drag":
        x0, y0, x1, y1 = (int(v) for v in sys.argv[2:6])
        settle = float(sys.argv[6]) if len(sys.argv) > 6 else 0.15
        sx0, sy0 = win.client_to_screen(hwnd, x0, y0)
        sx1, sy1 = win.client_to_screen(hwnd, x1, y1)
        ok = actions.move_drag(sx0, sy0, sx1, sy1, settle=settle)
        print("drag client(%d,%d)->(%d,%d) -> %s" % (x0, y0, x1, y1, ok))
    elif cmd == "park":
        ok = actions.park_cursor(hwnd)
        print("park ->", ok)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
