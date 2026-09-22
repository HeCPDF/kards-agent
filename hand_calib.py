#!/usr/bin/env python3
"""标定手牌槽位的客户区 x —— 不需要识别牌面。

原理：悬停一张手牌时，屏幕中间会弹出放大卡（一大块亮色）。扫一遍 x，
用"中间区域亮度相对基线是否抬升"判断该 x 底下有没有牌，把区间分出来，
再按 mem 的手牌张数均分出每张牌的中心 x。

用法: hand_calib.py [n_cards] [--show]
"""
import sys
import time

import agentpath  # noqa: F401  —— 接上 vendor/ 和本项目根

import numpy as np   # noqa: E402
import win           # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

SAFE_CLIENT = (1270, 360)
HOVER_Y = 700
HOLD = 0.30
REGION = (380, 270, 780, 530)      # 放大卡出现的区域 (x0,y0,x1,y1)


def _region_mean(frame):
    x0, y0, x1, y1 = REGION
    return float(frame[y0:y1, x0:x1].mean())


def calibrate(hwnd, n_cards, lo=380, hi=920, step=10, thr=6.0, verbose=False):
    """-> (xs, spans, baseline)。xs 长度 == n_cards（拿不到就按跨度均分）。"""
    from win import client_to_screen            # noqa: E402
    import actions                              # noqa: E402

    actions.set_cursor(*client_to_screen(hwnd, *SAFE_CLIENT))
    time.sleep(0.35)
    base = _region_mean(win.capture_client_bgr(hwnd))
    if verbose:
        print("baseline=%.1f" % base)

    hits = []
    for x in range(lo, hi + 1, step):
        actions.set_cursor(*client_to_screen(hwnd, x, HOVER_Y))
        time.sleep(HOLD)
        m = _region_mean(win.capture_client_bgr(hwnd))
        hits.append((x, m - base))
        if verbose:
            print("  x=%-4d delta=%+.1f%s" % (x, m - base, "  <= 有牌" if m - base > thr else ""))
        actions.set_cursor(*client_to_screen(hwnd, *SAFE_CLIENT))
        time.sleep(0.06)

    runs = []
    cur = None
    for x, d in hits:
        if d > thr:
            cur = [x] if cur is None else cur
            cur.append(x)
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    # 合并间隔过小的段（同一张牌被抖动切开）
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][-1] <= step * 1.5:
            merged[-1].extend(r)
        else:
            merged.append(list(r))
    spans = [(min(r), max(r)) for r in merged]

    if len(spans) == n_cards:
        centers = [int((a + b) / 2) for a, b in spans]
    elif spans:
        # 跨度均分：手牌扇形本来就是等距铺开的
        left, right = spans[0][0], spans[-1][1]
        if n_cards == 1:
            centers = [int((left + right) / 2)]
        else:
            pitch = (right - left) / (n_cards - 1)
            centers = [int(round(left + i * pitch)) for i in range(n_cards)]
    else:
        centers = []
    return centers, spans, base


def main():
    show = "--show" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.3)

    s = MemoryBoardSource()
    st = s.snapshot()
    s.close()
    hand = st.hand(LOCAL)
    n = int(args[0]) if args else len(hand)
    print("内存手牌 n=%d：%s" % (len(hand), [(c.slot, c.card_id, c.kredit_cost) for c in hand]))
    centers, spans, base = calibrate(hwnd, n, verbose=show)
    print("扫到的区间: %s" % spans)
    print("每张牌中心 x: %s" % centers)
    if len(centers) == len(hand):
        print("对位：%s" % [(c.card_id, x) for c, x in zip(hand, centers)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
