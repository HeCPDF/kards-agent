#!/usr/bin/env python3
"""悬停一个**客户区**坐标，只截 KARDS 客户区并存盘。

★ 坐标换算：`actions.set_cursor` / `move_drag` / `click` 收的都是**屏幕**坐标，
  客户区坐标必须先过 `win.client_to_screen(hwnd, cx, cy)`。搞混会把鼠标移到
  游戏外面去。

用法: hover_probe.py <client_x> <client_y> <out.png> [--no-park]
"""
import sys
import time

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/ 与 vendor/

import actions          # noqa: E402
import win              # noqa: E402

SAFE_CLIENT = (1270, 360)      # 客户区里的安全点（不压在卡上）


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    cx, cy = int(sys.argv[1]), int(sys.argv[2])
    out = sys.argv[3]
    park = "--no-park" not in sys.argv

    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    print("hwnd=0x%X" % hwnd)

    def to_screen(x, y):
        return win.client_to_screen(hwnd, x, y)

    win.bring_to_front(hwnd)
    time.sleep(0.25)

    sx, sy = to_screen(cx, cy)
    print("client (%d,%d) -> screen (%d,%d)" % (cx, cy, sx, sy))

    ok = actions.set_cursor(sx, sy)
    time.sleep(0.45)                                   # 等悬停面板出来
    # ★ 允许回退：PrintWindow 在悬停/覆盖层状态下会返回全白帧，
    #   capture_client_bgr 会用 mss 抓客户区屏幕矩形兜底（仍然只含 kards）。
    frame = win.capture_client_bgr(hwnd)
    if frame is not None:
        print("set_cursor=%s  frame=%s  mean=%.1f std=%.1f" % (
            ok, frame.shape, float(frame.mean()), float(frame.std())))
    else:
        print("set_cursor=%s  frame=None" % ok)
        return 1
    import cv2
    cv2.imwrite(out, frame)
    print("saved %s (%d bytes)" % (out, __import__("os").path.getsize(out)))

    if park:
        actions.set_cursor(*to_screen(*SAFE_CLIENT))    # 光标归位
    return 0


if __name__ == "__main__":
    sys.exit(main())
