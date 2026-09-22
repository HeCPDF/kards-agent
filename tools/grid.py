#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给帧画上 100px 网格（客户区坐标）并裁剪，便于人眼读坐标。 用法: grid.py <src> <out> [y0] [y1]"""
import sys
import cv2

src, out = sys.argv[1], sys.argv[2]
y0 = int(sys.argv[3]) if len(sys.argv) > 3 else 0
y1 = int(sys.argv[4]) if len(sys.argv) > 4 else 720
img = cv2.imread(src)
crop = img[y0:y1].copy()
h, w = crop.shape[:2]
for x in range(0, w, 100):
    cv2.line(crop, (x, 0), (x, h), (0, 0, 255), 1)
    cv2.putText(crop, str(x), (x + 2, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
for y in range(0, h, 100):
    cv2.line(crop, (0, y), (w, y), (0, 255, 0), 1)
    cv2.putText(crop, str(y + y0), (4, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
cv2.imwrite(out, crop)
print("saved", out, crop.shape)
