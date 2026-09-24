#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""release.py —— 强制松开鼠标左键（清理卡住的拖拽状态）。也可发一次 ESC。"""
import sys
import time

import win32api
import win32con

# ★ 下面这段会**真的动鼠标**，必须关在 __main__ 里。
#   踩过（2026-09-23）：批量 `import` 扫描时它们被导入，当场释放了鼠标按键、
#   把光标挪到了 (200,300)。导入不该有副作用。

if __name__ == "__main__":
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.3)
    print("left button released")
