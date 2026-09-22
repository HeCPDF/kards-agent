#!/usr/bin/env python3
"""抓一帧 KARDS 客户区并存成 PNG，供人工/模型查看。

不改变窗口大小（除非客户端不是 1280x720 且给了 --force）。
"""
import os
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import cv2            # noqa: E402
import win            # noqa: E402

WANT = (1280, 720)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else r"D:\Kards\reverse-data\shots\now.png"
    force = "--force" in sys.argv
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    w = wins[0]
    hwnd = w["hwnd"]
    keys = {k: v for k, v in w.items() if k != "hwnd"}
    print("hwnd=0x%X  %s" % (hwnd, keys))
    if not win.window_is_capturable(hwnd):
        print("窗口不可截（被遮挡/最小化？）")
    frame = win.capture_client_bgr(hwnd)
    if frame is None:
        print("抓帧失败")
        return 1
    print("client shape: %s" % (frame.shape,))
    if force and (frame.shape[1], frame.shape[0]) != WANT:
        win.set_window_client_size(hwnd, WANT[0], WANT[1])
        time.sleep(0.8)
        frame = win.capture_client_bgr(hwnd)
        print("forced -> %s" % (frame.shape,))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, frame)
    print("saved %s (%d bytes)" % (out, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
