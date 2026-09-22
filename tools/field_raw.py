#!/usr/bin/env python3
"""打印 board.read_field() 的原始结构，找单位条目的坐标字段。"""
import sys

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/ 与 vendor/

board = _bootstrap.upstream_board()  # noqa: E402  —— 上游 OCR（**只作核对**）
import win    # noqa: E402

win.set_dpi_aware()
wins = win.find_by_process("kards")
hwnd = wins[0]["hwnd"]
frame = win.capture_client_bgr(hwnd)
print("frame %s" % (frame.shape,))
f = board.read_field(frame)
print("keys: %s" % sorted(f.keys()))
print()
for k in ("rows", "our_support", "enemy_support", "frontline", "our_units", "enemy_units",
          "hq_our", "hq_enemy"):
    v = f.get(k)
    if v is None:
        print("--- %s = None" % k)
        continue
    if isinstance(v, list):
        print("--- %s  (list, n=%d)" % (k, len(v)))
        for i, u in enumerate(v):
            if k == "rows":
                print("   [%d] cy=%s y0=%s y1=%s side=%s n=%s boxes=%d" % (
                    i, u.get("cy"), u.get("y0"), u.get("y1"), u.get("side"),
                    u.get("n"), len(u.get("boxes") or [])))
            else:
                print("   [%d] %s" % (i, u))
    else:
        print("--- %s = %s" % (k, v))
