#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从换牌帧里量出中间那排卡片的水平区间/中心（按亮度列投影找连续卡片块）。"""
import sys
import cv2
import numpy as np

src = sys.argv[1]
img = cv2.imread(src)
h, w = img.shape[:2]
band = img[210:420, :, :]
gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
col = gray.mean(axis=0)
# 卡片是亮的（灰白卡面），木头背景暗
thr = float(np.percentile(col, 55))
mask = col > thr
segs = []
i = 0
while i < w:
    if mask[i]:
        j = i
        while j < w and mask[j]:
            j += 1
        if j - i >= 40:
            segs.append((i, j, (i + j) // 2))
        i = j
    else:
        i += 1
print("thr=%.1f  segments:" % thr)
for a, b, c in segs:
    print("   x %4d..%4d  width=%3d  center=%4d" % (a, b, b - a, c))
if len(segs) >= 2:
    cs = [c for _, _, c in segs]
    dx = [(cs[k + 1] - cs[k]) for k in range(len(cs) - 1)]
    print("   centers=%s  deltas=%s  mid=%.1f" % (cs, dx, (cs[0] + cs[-1]) / 2))
