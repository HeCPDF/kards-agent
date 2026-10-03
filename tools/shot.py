#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓一帧游戏客户区存成 PNG（后台截图：不需要窗口在前台、不动鼠标、不抢焦点）。

用法：`python tools/shot.py [输出路径]`；默认写到 `<数据目录>/shots/live.png`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 仓库根

import cv2                                                                         # noqa: E402

from base import paths, winapi                                                     # noqa: E402


def main(argv):
    out = argv[0] if argv else os.path.join(paths.DATA, "shots", "live.png")
    winapi.set_dpi_aware()
    wins = winapi.find_game_windows()
    if not wins:
        print("没有找到游戏窗口（游戏开了吗？）")
        return 1
    w = wins[0]
    frame = winapi.capture_client_bgr(w["hwnd"])
    if frame is None:
        print("抓帧失败（窗口最小化，或抓到的是全黑）")
        return 2
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, frame)
    print("hwnd=%s %dx%d -> %s" % (w["hwnd"], w["w"], w["h"], out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
