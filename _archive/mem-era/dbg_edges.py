#!/usr/bin/env python3
"""只读诊断：量手牌扇形边缘，和布局表比对，并打印被跳过的窄段。

不回牌、不点击，只移动光标到 SAFE_POINT。
"""
import json
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import hand_calibrate as hc          # noqa: E402
import hand_scanner_v2 as hs         # noqa: E402
import win                           # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

LAYOUT = r"D:\Kards\OCR-Kards-Auto\config\hand_layout.json"

win.set_dpi_aware()
wins = win.find_by_process("kards")
if not wins:
    print("没有 kards 窗口")
    raise SystemExit(1)
hwnd = wins[0]["hwnd"]
win.bring_to_front(hwnd)
time.sleep(0.4)

s = MemoryBoardSource()
st = s.snapshot()
s.close()
hand = st.hand(LOCAL)
print("mem 手牌 n=%d  %s" % (len(hand), [(c.slot, c.card_id, c.kredit_cost) for c in hand]))

layouts = json.load(open(LAYOUT, encoding="utf-8"))["layouts"]
print("表格 (left_edge, right_edge, count): %s" % sorted(
    (v["left_edge"], v.get("right_edge"), v["count"]) for v in layouts.values()))
print("SAFE_POINT=%s SAFE_SETTLE=%s LAYOUT_EDGE_TOL=%s LAYOUT_RIGHT_TOL=%s" % (
    hs.SAFE_POINT, hs.SAFE_SETTLE, hs.LAYOUT_EDGE_TOL, hs.LAYOUT_RIGHT_TOL))
print()

sc = hs.HandScannerV2(hwnd, {})
for i in range(3):
    L, R = sc.measure_edges()
    print("第 %d 次 measure_edges -> L=%s R=%s  left_suspect=%s right_suspect=%s" % (
        i + 1, L, R, sc.last_left_suspect, sc.last_right_suspect))
    print("    detect_left_edge.skipped  = %s" % (getattr(hc.detect_left_edge, "skipped", None),))
    print("    detect_right_edge.skipped = %s" % (getattr(hc.detect_right_edge, "skipped", None),))
    if L is not None:
        cands = [(v["count"], v["left_edge"], abs(v["left_edge"] - L))
                 for v in layouts.values()]
        cands.sort(key=lambda t: t[2])
        print("    最近的表格条目: %s" % cands[:3])
    time.sleep(0.8)
