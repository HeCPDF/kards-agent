#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mull_auto.py —— 全自动换牌：等换牌界面 -> 点选要换掉的牌 -> 确认 -> 回读验证。

关键事实（本次实测）：
  * 换牌时手牌**横排大图显示在屏幕中间**（不是底部的扇形），卡片纵向 ~210..480，点 y=340。
  * 确认按钮 mulligan_ok_btn 在 (638,667)，模板匹配 1.000。
  * 卡片中心：x_i = 640 + (i-(n-1)/2)*223.6
  * 点卡 = 切换"要替换"（BP_HandCard::MulliganDiscardToggle）。

用法: python mull_auto.py [--keep-cheap N] [--max N]
"""
from __future__ import annotations

import os
import sys
import time

import agentpath  # noqa: F401  —— 接上 vendor/ 和本项目根

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win  # noqa: E402
import actions  # noqa: E402
import board_api as BA  # noqa: E402

REPO = agentpath.AGENT_ROOT
OUT = r"D:\Kards\reverse-data\shots\mull"
os.makedirs(OUT, exist_ok=True)
BTN = cv2.imread(os.path.join(REPO, "ui_templates", "mulligan_ok_btn.png"))
END = cv2.imread(os.path.join(REPO, "ui_templates", "end_turn_btn.png"))

CARD_Y = 340
PITCH = 223.6
CENTER_X = 640
CONFIRM = (638, 667)


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


def score(frame, tpl):
    if frame is None or tpl is None:
        return 0.0
    res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max())


def snap(h):
    return win.capture_client_bgr(h, allow_screen_fallback=True)


def card_boxes(n):
    xs = [int(round(CENTER_X + (i - (n - 1) / 2.0) * PITCH)) for i in range(n)]
    half = int(PITCH / 2) - 18
    return [(x - half, x + half) for x in xs]


def region_diff(a, b, x0, x1, y0=215, y1=470):
    if a is None or b is None:
        return -1.0
    pa = a[y0:y1, x0:x1].astype(np.int16)
    pb = b[y0:y1, x0:x1].astype(np.int16)
    return float(np.abs(pa - pb).mean())


def wait_mulligan(h, deadline=90.0):
    t0 = time.time()
    while time.time() - t0 < deadline:
        f = snap(h)
        s = score(f, BTN)
        if s >= 0.70:
            return f, s, time.time() - t0
        time.sleep(0.5)
    return None, 0.0, time.time() - t0


def main():
    keep_cheap = 2
    max_replace = 3
    if "--keep-cheap" in sys.argv:
        keep_cheap = int(sys.argv[sys.argv.index("--keep-cheap") + 1])
    if "--max" in sys.argv:
        max_replace = int(sys.argv[sys.argv.index("--max") + 1])

    h = hwnd()
    print("[1] 等换牌界面 ...")
    f, s, el = wait_mulligan(h)
    if f is None:
        print("    没等到换牌界面（%.0fs）" % el)
        return
    print("    检测到换牌界面 %.1fs 后, btn=%.3f" % (el, s))
    cv2.imwrite(os.path.join(OUT, "auto_00_detected.png"), f)

    st = BA.open_source("mem").snapshot()
    hand = sorted(st.hand("local"), key=lambda c: (c.slot if c.slot is not None else 99))
    n = len(hand)
    print("[2] 内存手牌 n=%d: %s" % (n, [(c.card_id, (c.name or "?")[:16], c.kredit_cost) for c in hand]))

    # 选要换掉的：按费用降序，取前 max_replace 个；但保留费用最低的 keep_cheap 张
    order = sorted(range(n), key=lambda i: -(hand[i].kredit_cost or 0))
    by_cost_asc = sorted(range(n), key=lambda i: (hand[i].kredit_cost or 0))
    protect = set(by_cost_asc[:keep_cheap])
    picks = [i for i in order if i not in protect][:max_replace]
    picks.sort()
    print("[3] 计划换掉 slot=%s（保护最便宜的 %d 张）" % (picks, keep_cheap))

    boxes = card_boxes(n)
    before = f
    for i in picks:
        x = int(round(CENTER_X + (i - (n - 1) / 2.0) * PITCH))
        sx, sy = win.client_to_screen(h, x, CARD_Y)
        ok = actions.click(sx, sy)
        time.sleep(0.45)
        after = snap(h)
        x0, x1 = boxes[i]
        d = region_diff(before, after, x0, x1)
        print("    点 slot=%d @client(%d,%d) -> %s ; 该卡区域变化=%.2f %s" % (
            i, x, CARD_Y, ok, d, "OK(已切换)" if d > 1.0 else "⚠️ 看起来没变"))
        before = after

    time.sleep(0.4)
    sel = snap(h)
    cv2.imwrite(os.path.join(OUT, "auto_01_selected.png"), sel)
    print("[4] 选中后 btn=%.3f（应仍 ~1.0）" % score(sel, BTN))

    sx, sy = win.client_to_screen(h, *CONFIRM)
    print("[5] 点确认 @client%s -> %s" % (str(CONFIRM), actions.click(sx, sy)))
    time.sleep(1.8)
    fin = snap(h)
    cv2.imwrite(os.path.join(OUT, "auto_02_confirmed.png"), fin)
    print("    确认后: mulligan_btn=%.3f  end_turn=%.3f" % (score(fin, BTN), score(fin, END)))

    time.sleep(1.5)
    st2 = BA.open_source("mem").snapshot()
    hand2 = sorted(st2.hand("local"), key=lambda c: (c.slot if c.slot is not None else 99))
    old = set(c.card_id for c in hand)
    new = set(c.card_id for c in hand2)
    print("[6] 换牌后手牌 n=%d: %s" % (len(hand2), [(c.card_id, (c.name or "?")[:16], c.kredit_cost) for c in hand2]))
    print("    被换掉(旧有新无)=%s   新摸到(新有旧无)=%s" % (sorted(old - new), sorted(new - old)))


if __name__ == "__main__":
    main()
