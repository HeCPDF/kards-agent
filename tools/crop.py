#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已抓的帧里裁剪区域并放大保存。用法: python crop.py <src> <out> x y w h [scale]"""
import sys
import cv2

src, out = sys.argv[1], sys.argv[2]
x, y, w, h = (int(v) for v in sys.argv[3:7])
scale = float(sys.argv[7]) if len(sys.argv) > 7 else 2.0
img = cv2.imread(src)
if img is None:
    print("cannot read", src)
    raise SystemExit(1)
crop = img[y:y + h, x:x + w]
crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
cv2.imwrite(out, crop)
print("saved", out, crop.shape)
