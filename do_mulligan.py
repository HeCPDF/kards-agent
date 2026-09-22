#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""do_mulligan.py —— 在换牌阶段执行：点选要换掉的牌 -> 点确认。

用法: python do_mulligan.py <slot> [<slot> ...]        # slot = 内存 locationNumber
      python do_mulligan.py --none                     # 不换任何牌，直接确认
每个动作前后存帧到 shots/mull/act_*.png
"""
import json
import os
import sys
import time

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import cv2  # noqa: E402
import win  # noqa: E402
import actions  # noqa: E402
import board_api as BA  # noqa: E402

OUT = r"D:\Kards\reverse-data\shots\mull"
os.makedirs(OUT, exist_ok=True)
REPO = r"D:\Kards\OCR-Kards-Auto"
CONFIRM = (638, 667)
HAND_Y = 690
BTN = cv2.imread(os.path.join(REPO, "ui_templates", "mulligan_ok_btn.png"))


def hwnd():
    win.set_dpi_aware()
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    ws = win.find_by_process("kards")
    return ws[0]["hwnd"] if ws else None


def snap(h, tag):
    f = win.capture_client_bgr(h, allow_screen_fallback=True)
    if f is not None:
        cv2.imwrite(os.path.join(OUT, "act_%s.png" % tag), f)
    s = 0.0
    if f is not None and BTN is not None:
        res = cv2.matchTemplate(f, BTN, cv2.TM_CCOEFF_NORMED)
        s = float(res.max())
    return f, s


def probes_for(count):
    p = os.path.join(REPO, "config", "hand_layout.json")
    with open(p, encoding="utf-8") as fh:
        layout = json.load(fh)["layouts"]
    # ★ layouts 是 **按 left_edge 作 key** 的: {"421": {count:5, probes:[...], right_edge:...}, ...}
    for k, v in layout.items():
        try:
            if int(v.get("count", -1)) == count:
                return v["probes"], v.get("left_edge", k)
        except Exception:
            continue
    return None, None


def main():
    h = hwnd()
    st = BA.open_source("mem").snapshot()
    hand = sorted(st.hand("local"), key=lambda c: (c.slot if c.slot is not None else 99))
    n = len(hand)
    probes, left = probes_for(n)
    print("hand n=%d probes=%s left_edge=%s" % (n, probes, left))
    for c in hand:
        print("   slot=%s id=%s %-22s cost=%s type=%s" % (c.slot, c.card_id, (c.name or "?")[:22], c.kredit_cost, c.card_type))

    args = sys.argv[1:]
    if args and args[0] == "--none":
        slots = []
    elif args:
        slots = [int(a) for a in args]
    else:
        # 默认策略：换掉 >=5 费的牌，保留便宜的那张
        slots = [c.slot for c in hand if (c.kredit_cost or 0) >= 5]
        print("default policy -> replace slots", slots)

    if probes is None:
        print("NO LAYOUT for n=%d, abort" % n)
        return

    f, s = snap(h, "00_before")
    print("before: btn=%.3f" % s)

    for k, slot in enumerate(slots):
        if slot is None or slot >= len(probes):
            continue
        x = probes[slot]
        sx, sy = win.client_to_screen(h, x, HAND_Y)
        ok = actions.click(sx, sy)
        time.sleep(0.45)
        _, s2 = snap(h, "%02d_after_click_slot%d" % (k + 1, slot))
        print("click slot=%d at client(%d,%d) -> %s ; btn=%.3f" % (slot, x, HAND_Y, ok, s2))

    if len(slots):
        time.sleep(0.4)
        f, s = snap(h, "90_selected")
        print("after selection: btn=%.3f" % s)

    sx, sy = win.client_to_screen(h, *CONFIRM)
    ok = actions.click(sx, sy)
    print("CONFIRM click (%d,%d) -> %s" % (CONFIRM + (ok,)))
    time.sleep(1.6)
    _, s = snap(h, "99_confirmed")
    print("after confirm: mulligan_btn=%.3f (应显著下降)" % s)


if __name__ == "__main__":
    main()
