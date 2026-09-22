#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mull_watch.py —— 监视换牌阶段：检测确认按钮模板、记录内存手牌、连拍若干帧。

只观测、不点击。用法: python mull_watch.py [deadline_s]
"""
import os
import sys
import time

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win  # noqa: E402
import board_api as BA  # noqa: E402

REPO = r"D:\Kards\OCR-Kards-Auto"
OUT = r"D:\Kards\reverse-data\shots\mull"
os.makedirs(OUT, exist_ok=True)

BTN = cv2.imread(os.path.join(REPO, "ui_templates", "mulligan_ok_btn.png"))
END = cv2.imread(os.path.join(REPO, "ui_templates", "end_turn_btn.png"))
DEADLINE = float(sys.argv[1]) if len(sys.argv) > 1 else 150.0

win.set_dpi_aware()


def hwnd():
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    ws = win.find_by_process("kards")
    return ws[0]["hwnd"] if ws else None


def best(frame, tpl):
    if tpl is None or tpl.shape[0] > frame.shape[0] or tpl.shape[1] > frame.shape[1]:
        return 0.0, (0, 0)
    res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return mx, (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2)


def mem_brief():
    try:
        st = BA.open_source("mem").snapshot()
        hand = sorted(st.hand("local"), key=lambda c: (c.slot if c.slot is not None else 99))
        return "turn=%s our_turn=%s k=%s slotL=%s board=%d hand=%d [%s]" % (
            st.turn, st.our_turn, st.kredits.get("local"), st.slots.get("local"),
            len(st.field_units("local")), len(hand),
            " ".join("%s:%s(c%s,sl%s)" % (c.card_id, (c.name or "?")[:14], c.kredit_cost, c.slot) for c in hand))
    except Exception as e:
        return "mem error: %s" % e


def main():
    h = hwnd()
    t0 = time.time()
    seen = False
    burst = 0
    i = 0
    while time.time() - t0 < DEADLINE:
        i += 1
        f = win.capture_client_bgr(h, allow_screen_fallback=True)
        if f is None:
            time.sleep(0.5)
            continue
        s_btn, c_btn = best(f, BTN)
        s_end, c_end = best(f, END)
        el = time.time() - t0
        if not seen and s_btn >= 0.70:
            seen = True
            print("[%.1fs] MULLIGAN UI DETECTED btn=%.3f center=%s end_turn=%.3f" % (el, s_btn, c_btn, s_end))
            print("        mem: %s" % mem_brief())
            cv2.imwrite(os.path.join(OUT, "detect_%02d.png" % i), f)
            burst = 8
        if seen:
            cv2.imwrite(os.path.join(OUT, "burst_%02d.png" % i), f)
            print("[%.1fs] burst %d  btn=%.3f@%s end=%.3f" % (el, burst, s_btn, c_btn, s_end))
            burst -= 1
            if burst <= 0:
                break
            time.sleep(0.8)
        else:
            if i % 5 == 0:
                print("[%.1fs] waiting... btn=%.3f end=%.3f" % (el, s_btn, s_end))
            time.sleep(0.6)
    if not seen:
        print("NOT DETECTED within %.0fs" % DEADLINE)
        print("mem: %s" % mem_brief())


if __name__ == "__main__":
    main()
