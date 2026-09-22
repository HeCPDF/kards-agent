#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""release.py —— 强制松开鼠标左键（清理卡住的拖拽状态）。也可发一次 ESC。"""
import sys
import time

import win32api
import win32con

win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.2)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.3)
print("left button released")
