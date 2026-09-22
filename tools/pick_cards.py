#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pick_cards.py —— 在画面中间带里找"候选卡"的水平中心（抉择/预报的选择界面）。

判据：候选卡是屏幕中间竖着的大卡，卡面亮、卡框高，夹在暗木背景之间。
用法: python pick_cards.py <png> [y0] [y1]
"""
import sys

import cv2
import numpy as np

Y0 = int(sys.argv[2]) if len(sys.argv) > 2 else 210
Y1 = int(sys.argv[3]) if len(sys.argv) > 3 else 520


def detect(path, y0=Y0, y1=Y1, thr=105, min_frac=0.62, min_w=55, merge_gap=26):
    img = cv2.imread(path)
    band = cv2.cvtColor(img[y0:y1], cv2.COLOR_BGR2GRAY)
    frac = (band > thr).mean(axis=0)
    raw = []
    i = 0
    n = len(frac)
    while i < n:
        if frac[i] > min_frac:
            j = i
            while j < n and frac[j] > min_frac:
                j += 1
            raw.append([i, j])
            i = j
        else:
            i += 1
    # 合并被暗色卡面切开的段
    merged = []
    for a, b in raw:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = [(int((a + b) / 2), b - a, a, b) for a, b in merged if b - a >= min_w]
    return out, frac


if __name__ == "__main__":
    path = sys.argv[1]
    cards, _ = detect(path)
    print("%s  候选卡(center,width,x0,x1): %s" % (path, cards))
