#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在整帧里全屏匹配 ui_templates/*.png，报告最佳位置（不限于 templates.json 的 region）。"""
import os
import sys
import cv2
import numpy as np

import _bootstrap  # noqa: E402,F401
TPL = os.path.join(_bootstrap.AGENT_ROOT, "ui_templates")

frame = cv2.imread(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_bootstrap.shots_dir(), "live_mulligan.png"))
names = sys.argv[2:] or ["mulligan_ok_btn", "end_turn_btn", "deck_ok_btn", "play_btn", "victory_btn", "hand_zone"]

for name in names:
    p = os.path.join(TPL, name + ".png")
    tpl = cv2.imread(p)
    if tpl is None:
        print("%-18s MISSING %s" % (name, p))
        continue
    if tpl.shape[0] > frame.shape[0] or tpl.shape[1] > frame.shape[1]:
        print("%-18s template bigger than frame" % name)
        continue
    res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    print("%-18s tpl=%dx%d  best=%.4f at (x=%d,y=%d) center=(%d,%d)" % (
        name, tpl.shape[1], tpl.shape[0], mx, loc[0], loc[1],
        loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2))
