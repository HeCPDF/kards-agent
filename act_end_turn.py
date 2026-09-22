#!/usr/bin/env python3
"""点「结束回合」。位置由 end_turn_btn 模板匹配给出（不猜坐标）。

只有在模板命中 >= 0.60（= 确实是我的回合）时才点。
"""
import os
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import ui_state         # noqa: E402
import win              # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

PROJ = r"D:\Kards\OCR-Kards-Auto"
MIN_SCORE = 0.60


def main():
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.3)

    meta = os.path.join(PROJ, "config", "templates.json")
    tpls = ui_state.load_templates(ui_state.load_meta(meta))
    tpl = tpls.get("end_turn_btn")
    if tpl is None:
        print("templates 里没有 end_turn_btn；现有键：%s" % sorted(tpls))
        return 1

    frame = win.capture_client_bgr(hwnd)
    score, box = ui_state.match_one(frame, tpl)
    print("end_turn_btn  score=%.3f  box=%s" % (score, box))
    if box is None or score < MIN_SCORE:
        print("按钮不可见（score < %.2f）-> 现在不是我的回合，不点" % MIN_SCORE)
        return 1

    cx = int(box[0] + box[2] // 2)
    cy = int(box[1] + box[3] // 2)
    sx, sy = win.client_to_screen(hwnd, cx, cy)
    print("click client (%d,%d) -> screen (%d,%d)" % (cx, cy, sx, sy))
    ok = actions.click(sx, sy)
    print("click -> %s" % ok)

    for i in range(6):
        time.sleep(0.7)
        s = MemoryBoardSource()
        st = s.snapshot()
        s.close()
        print("  +%.1fs turn=%s our_turn=%s kredits=%s/%s" % (
            (i + 1) * 0.7, st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"]))
        if st.our_turn is False:
            print("=> 交棒成功，现在是对手回合")
            return 0
    print("=> 6 次采样后 our_turn 仍为 True，需要人工看一眼")
    return 2


if __name__ == "__main__":
    sys.exit(main())
