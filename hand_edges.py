#!/usr/bin/env python3
"""在手牌带里找卡与卡之间的暗缝，从而定出每张手牌的 x 区间。

用法: hand_edges.py <frame.png> [y0 y1 x0 x1]
"""
import sys

import cv2
import numpy as np


def main():
    path = sys.argv[1]
    y0, y1 = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (692, 714)
    x0, x1 = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (400, 900)

    img = cv2.imread(path)
    if img is None:
        print("读不到 %s" % path)
        return 1
    band = img[y0:y1, x0:x1].astype(np.float32)
    prof = band.mean(axis=2).mean(axis=0)          # 每列平均亮度
    base = float(np.median(prof))

    drop = 22
    seams = [x0 + i for i, v in enumerate(prof) if v < base - drop]
    groups = []
    for s in seams:
        if groups and s - groups[-1][-1] <= 4:
            groups[-1].append(s)
        else:
            groups.append([s])
    centers = [int(np.mean(g)) for g in groups]

    print("band y=%d..%d  x=%d..%d   baseline=%.0f  drop=%.0f" % (y0, y1, x0, x1, base, drop))
    print("暗缝列:", centers)
    if len(centers) >= 2:
        print("=> 由暗缝切出的卡区间:")
        edges = [x0] + centers + [x1]
        for i in range(len(edges) - 1):
            a, b = edges[i], edges[i + 1]
            if b - a >= 20:
                print("   [%4d .. %4d]  中心 x=%4d  宽=%d" % (a, b, (a + b) // 2, b - a))
    print()
    print("亮度剖面（每 6px 一个采样）:")
    print("  " + " ".join("%d" % prof[i] for i in range(0, len(prof), 6)))
    print("  x从%d起, 每列6px" % x0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
