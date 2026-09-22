#!/usr/bin/env python3
"""裁一块区域并加上带刻度的标尺（标尺画在外侧留白，不遮挡画面）。

用法: crop_grid.py <in.png> <out.png> x0 y0 x1 y1 [scale]
坐标是原图客户区坐标 (1280x720)。
"""
import sys

import cv2
import numpy as np


def main():
    if len(sys.argv) < 7:
        print(__doc__)
        return 2
    src, out = sys.argv[1], sys.argv[2]
    x0, y0, x1, y1 = (int(v) for v in sys.argv[3:7])
    scale = float(sys.argv[7]) if len(sys.argv) > 7 else 1.0

    img = cv2.imread(src)
    if img is None:
        print("读不到 %s" % src)
        return 1
    h, w = img.shape[:2]
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    crop = img[y0:y1, x0:x1]
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_NEAREST)

    M = 44                                   # 外侧留白
    ch, cw = crop.shape[:2]
    canvas = np.full((ch + M, cw + M, 3), 30, np.uint8)
    canvas[M:, M:] = crop

    step = 20
    for gx in range(x0 - x0 % step, x1 + 1, step):
        px = M + int((gx - x0) * scale)
        major = (gx % 100 == 0)
        cv2.line(canvas, (px, M - 6 if major else M - 3), (px, ch + M),
                 (90, 90, 90) if major else (60, 60, 60), 1)
        if major:
            cv2.putText(canvas, str(gx), (px - 12, M - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 220, 255), 1, cv2.LINE_AA)
    for gy in range(y0 - y0 % step, y1 + 1, step):
        py = M + int((gy - y0) * scale)
        major = (gy % 100 == 0)
        cv2.line(canvas, (M - 6 if major else M - 3, py), (cw + M, py),
                 (90, 90, 90) if major else (60, 60, 60), 1)
        if major:
            cv2.putText(canvas, str(gy), (2, py + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 220, 255), 1, cv2.LINE_AA)

    cv2.imwrite(out, canvas)
    print("saved %s  crop=(%d,%d)-(%d,%d) scale=%.2f  canvas=%s" % (
        out, x0, y0, x1, y1, scale, canvas.shape))
    return 0


if __name__ == "__main__":
    sys.exit(main())
