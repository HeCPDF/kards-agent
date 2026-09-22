#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reset_input.py —— 清掉卡住的拖拽状态：多次 LEFTUP + 在中性位置点一下。"""
import sys
import time

import win32api
import win32con

for _ in range(3):
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.15)

# 中性位置：顶部中间（不是任何按钮）
win32api.SetCursorPos((200, 300))
time.sleep(0.3)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.06)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.4)
print("input reset done; cursor at", win32api.GetCursorPos())
