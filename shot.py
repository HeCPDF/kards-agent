#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓一帧游戏客户区并存成 PNG。用法: python shot.py <输出路径>"""
import os
import sys

import agentpath  # noqa: F401  —— 接上 vendor/ 和本项目根

import win  # noqa: E402
import cv2  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(agentpath.shots(), "live.png")

win.set_dpi_aware()
wins = win.find_by_process("kards")
if not wins:
    print("NO WINDOW")
    raise SystemExit(1)
w = None
for cand in wins:
    try:
        if win.looks_like_game(cand):
            w = cand
            break
    except Exception:
        pass
w = w or wins[0]

hwnd = w["hwnd"]
frame = win.capture_client_bgr(hwnd, allow_screen_fallback=True)
if frame is None:
    print("CAPTURE FAILED")
    raise SystemExit(2)
os.makedirs(os.path.dirname(out), exist_ok=True)
cv2.imwrite(out, frame)
print("hwnd=%s title=%r shape=%s -> %s" % (hwnd, w.get("title"), frame.shape, out))
