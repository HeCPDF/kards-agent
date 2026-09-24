#!/usr/bin/env python3
"""从手牌某个 x 拖一张牌到我方阵线，打印前后状态并存前后截图。

用法: act_deploy.py <card_client_x> <drop_client_x> <drop_client_y>
"""
import os
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import cv2              # noqa: E402
import deploy           # noqa: E402
import hand_scanner_v2  # noqa: E402
import win              # noqa: E402
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

SHOTS = r"D:\Kards\reverse-data\shots"


def snap():
    s = MemoryBoardSource()
    st = s.snapshot()
    s.close()
    return st


def show(tag, st):
    print("%s turn=%s our_turn=%s kredits=%s/%s hand=%s board=%s hq=%s/%s" % (
        tag, st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
        [(c.card_id, c.kredit_cost) for c in st.hand(LOCAL)],
        [(c.card_id, c.attack, c.defense) for c in st.board(LOCAL)],
        st.hq[LOCAL].defense, st.hq["enemy"].defense))


def shot(name, hwnd):
    f = win.capture_client_bgr(hwnd)
    p = os.path.join(SHOTS, name)
    cv2.imwrite(p, f)
    print("  shot -> %s  std=%.1f" % (p, float(f.std())))
    return p


def main():
    card_x = int(sys.argv[1])
    auto = sys.argv[2] == "auto"
    if auto:
        dx = dy = None
        rest = sys.argv[3:]
    else:
        dx, dy = int(sys.argv[2]), int(sys.argv[3])
        rest = sys.argv[4:]
    target = (int(rest[0]), int(rest[1])) if len(rest) > 1 else None

    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.35)

    if auto:
        import board
        win.bring_to_front(hwnd)
        frame = win.capture_client_bgr(hwnd)
        field = board.read_field(frame)
        cands = deploy.deploy_candidates(field)
        print("deploy_candidates -> %s" % cands[:6])
        if not cands:
            print("没有候选落点，放弃")
            return 1
        dx, dy = cands[0]

    print("HOLD=%.2f  card_x=%d drop=(%d,%d) target=%s  -> screen card=%s" % (
        hand_scanner_v2.HOLD, card_x, dx, dy, target,
        win.client_to_screen(hwnd, card_x, 700)))

    show("BEFORE", snap())
    shot("act_before.png", hwnd)

    # on_pressed：按下瞬间的那一帧里，"被拖起来的那张牌"画在光标上，
    # 是"到底抓了哪张"唯一的地面真值。
    def on_pressed():
        shot("act_pressed.png", hwnd)

    ok = deploy.drag_deploy(hwnd, card_x, card_y=700, drop=(dx, dy),
                            hover_wait=hand_scanner_v2.HOLD, on_pressed=on_pressed)
    print("drag_deploy -> %s" % ok)
    time.sleep(1.4)
    shot("act_dropped.png", hwnd)
    show("AFTER-DROP", snap())

    if target:
        print("点击目标 client %s -> screen %s" % (
            target, win.client_to_screen(hwnd, *target)))
        time.sleep(0.6)
        ok2 = actions.click(*win.client_to_screen(hwnd, *target))
        print("click -> %s" % ok2)
        time.sleep(2.0)

    shot("act_after.png", hwnd)
    show("AFTER ", snap())
    return 0


if __name__ == "__main__":
    sys.exit(main())
