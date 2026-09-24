#!/usr/bin/env python3
"""按**内存给的张数**查布局表拿 probes，再逐个悬停验证每个 probe 都压在手牌上。

只移动光标 + 截图，不点击、不出牌。
"""
import json
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import hand_calibrate as hc          # noqa: E402
import hand_scanner_v2 as hs         # noqa: E402
import numpy as np                   # noqa: E402
import win                           # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

LAYOUT = r"D:\Kards\OCR-Kards-Auto\config\hand_layout.json"
REGION = (380, 270, 780, 530)


def rmean(frame):
    x0, y0, x1, y1 = REGION
    return float(frame[y0:y1, x0:x1].mean())


def main():
    win.set_dpi_aware()
    hwnd = win.find_by_process("kards")[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.4)

    s = MemoryBoardSource()
    st = s.snapshot()
    s.close()
    hand = st.hand(LOCAL)
    n = len(hand)
    print("mem 手牌 n=%d  %s" % (n, [(c.slot, c.card_id, c.kredit_cost) for c in hand]))

    layouts = json.load(open(LAYOUT, encoding="utf-8"))["layouts"]
    by_count = {v["count"]: v for v in layouts.values()}
    entry = by_count.get(n)
    if not entry:
        print("表里没有 %d 张的条目" % n)
        return 1
    print("按张数查到: left_edge=%s right_edge=%s probes=%s" % (
        entry["left_edge"], entry.get("right_edge"), entry["probes"]))

    # 一致性校验：量边缘
    sc = hs.HandScannerV2(hwnd, {})
    L, R = sc.measure_edges()
    print("实测边缘 L=%s R=%s ；与表差 左=%s 右=%s" % (
        L, R, abs(L - entry["left_edge"]) if L else None,
        abs(R - entry["right_edge"]) if R else None))

    # 基线（光标在 SAFE_POINT）—— 判"有没有弹面板"要用**逐像素差异**，
    # 不能用区域均值差：实测不同牌的均值差可以小到 ±1.6（卡面明暗接近桌面）。
    base_img = win.capture_client_bgr(hwnd)
    print("baseline region mean = %.1f" % rmean(base_img))
    print()
    import actions
    from hand_scanner_v2 import diff_bbox
    for i, px in enumerate(entry["probes"]):
        actions.set_cursor(*win.client_to_screen(hwnd, px, 700))
        time.sleep(0.40)
        f = win.capture_client_bgr(hwnd)
        try:
            bb = diff_bbox(base_img, f)
        except Exception as exc:
            bb = "diff_bbox 异常 %s" % exc
        mad = float(np.abs(f.astype(np.int16) - base_img.astype(np.int16)).mean())
        print("  probe[%d] x=%-4d  MAD=%6.2f  diff_bbox=%s  %s" % (
            i, px, mad, bb, "有面板" if (bb and not isinstance(bb, str)) else "⚠ 没面板?"))
        actions.set_cursor(*win.client_to_screen(hwnd, *hs.SAFE_POINT))
        time.sleep(0.15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
