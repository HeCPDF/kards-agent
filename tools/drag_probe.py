#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drag_probe.py —— 拖拽诊断：按住时截一帧，看牌有没有被拿起来。
用法: python drag_probe.py x0 y0 x1 y1 [settle]
"""
import os
import sys
import time

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/ 与 vendor/
SHOTS = _bootstrap.shots_dir()

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win  # noqa: E402
import actions  # noqa: E402

x0, y0, x1, y1 = (int(v) for v in sys.argv[1:5])
settle = float(sys.argv[5]) if len(sys.argv) > 5 else 0.20

win.set_dpi_aware()
h = [w for w in win.find_by_process("kards")][0]["hwnd"]

sx0, sy0 = win.client_to_screen(h, x0, y0)
sx1, sy1 = win.client_to_screen(h, x1, y1)

base = win.capture_client_bgr(h, allow_screen_fallback=True)
cv2.imwrite(os.path.join(SHOTS, "probe_0_base.png"), base)

import win32api
import win32con
actions.set_cursor(sx0, sy0)
time.sleep(settle)
cv2.imwrite(os.path.join(SHOTS, "probe_1_hover.png"),
            win.capture_client_bgr(h, allow_screen_fallback=True))

win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.20)
cv2.imwrite(os.path.join(SHOTS, "probe_2_pressed.png"),
            win.capture_client_bgr(h, allow_screen_fallback=True))

actions.move_stepped(sx0, sy0, sx1, sy1)
actions.set_cursor(sx1, sy1)
time.sleep(0.35)
cv2.imwrite(os.path.join(SHOTS, "probe_3_dragging.png"),
            win.capture_client_bgr(h, allow_screen_fallback=True))

win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.5)
cv2.imwrite(os.path.join(SHOTS, "probe_4_dropped.png"),
            win.capture_client_bgr(h, allow_screen_fallback=True))

for tag in ("1_hover", "2_pressed", "3_dragging", "4_dropped"):
    img = cv2.imread(os.path.join(SHOTS, "probe_%s.png") % tag)
    d = float(np.abs(base.astype(int) - img.astype(int)).mean())
    print("probe_%s: diff vs base = %.3f" % (tag, d))
