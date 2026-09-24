#!/usr/bin/env python3
"""抓牌 -> 拖到落点 -> **几乎立刻**点目标（用于"单位部署指向：先打出后选择"）。

用法: act_target.py <card_x> <drop_x> <drop_y> <target_x> <target_y> [delay_s]
"""
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
    r = s.snapshot()
    s.close()
    return r


def brief(st):
    return "turn=%s our=%s k=%s/%s hand=%s fieldL=%s fieldE=%s hq=%s/%s" % (
        st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
        [(c.card_id, c.kredit_cost) for c in st.hand(LOCAL)],
        [(c.card_id, c.attack, c.defense) for c in st.field_units(LOCAL)],
        [(c.card_id, c.attack, c.defense) for c in st.field_units("enemy")],
        st.hq[LOCAL].defense, st.hq["enemy"].defense)


def main():
    card_x, dx, dy, tx, ty = (int(v) for v in sys.argv[1:6])
    delay = float(sys.argv[6]) if len(sys.argv) > 6 else 0.3

    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.35)

    print("BEFORE %s" % brief(snap()))
    t0 = time.time()
    ok = deploy.drag_deploy(hwnd, card_x, card_y=700, drop=(dx, dy),
                            hover_wait=hand_scanner_v2.HOLD)
    t_drag = time.time() - t0
    print("drag_deploy -> %s  (%.2fs)" % (ok, t_drag))

    time.sleep(delay)
    sx, sy = win.client_to_screen(hwnd, tx, ty)
    ok2 = actions.click(sx, sy)
    print("click target client(%d,%d)->screen(%d,%d) -> %s  (+%.2fs after release)" % (
        tx, ty, sx, sy, ok2, delay))

    for i in range(4):
        time.sleep(0.6)
        st = snap()
        print("  +%.1fs %s" % ((i + 1) * 0.6, brief(st)))

    f = win.capture_client_bgr(hwnd)
    cv2.imwrite(SHOTS + r"\target_after.png", f)
    print("shot -> target_after.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
