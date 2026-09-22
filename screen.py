#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screen.py —— 用仓库自己的 ui_state 分类当前界面，并打印各模板分数（全屏最佳匹配）。"""
import os
import sys

import agentpath  # noqa: F401  —— 接上 vendor/ 和本项目根

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win  # noqa: E402
import ui_state  # noqa: E402

win.set_dpi_aware()


def get_hwnd():
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    ws = win.find_by_process("kards")
    return ws[0]["hwnd"] if ws else None


def frame_and_hwnd():
    hwnd = get_hwnd()
    return win.capture_client_bgr(hwnd, allow_screen_fallback=True), hwnd


def full_frame_scores(frame):
    """不限 region，全屏最佳匹配每个模板。"""
    root = os.path.join(SRC, "..", "config", "templates.json")
    repo = os.path.abspath(os.path.join(SRC, ".."))
    meta = ui_state.load_meta(root)
    out = []
    for name in meta.get("templates", {}):
        path = os.path.join(repo, meta["templates"][name]["path"])
        tpl = cv2.imread(path)
        if tpl is None:
            continue
        if tpl.shape[0] > frame.shape[0] or tpl.shape[1] > frame.shape[1]:
            continue
        res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        out.append((mx, name, loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2,
                    tpl.shape[1], tpl.shape[0]))
    out.sort(reverse=True)
    return out


def main():
    frame, hwnd = frame_and_hwnd()
    if frame is None:
        print("capture failed")
        return
    out = full_frame_scores(frame)
    for score, name, cx, cy, w, h in out:
        print("%-18s %.4f  center=(%d,%d) size=%dx%d" % (name, score, cx, cy, w, h))
    print("mean=%.1f std=%.1f" % (frame.mean(), frame.std()))
    if len(sys.argv) > 1:
        cv2.imwrite(sys.argv[1], frame)


if __name__ == "__main__":
    main()
