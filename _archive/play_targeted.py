#!/usr/bin/env python3
"""打一张"需要指向"的牌：部署到落点 -> 立刻点敌方目标。

费用判据（★ 用户实测规则）：卡面费用 + 1（指向）<= 当前指挥点。
坐标分工：mem 给身份/费用/手牌槽位；ocr 给屏幕坐标（框）。
"""
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import cv2              # noqa: E402
import deploy           # noqa: E402
import hand_scanner_v2  # noqa: E402
import win              # noqa: E402
from board_api import LOCAL, MemoryBoardSource, OcrBoardSource  # noqa: E402

# 手牌槽位 -> 客户区 x（本机 1280x720 标定；slot1=565 / slot0=490 已悬停验证）
HAND_X0, HAND_PITCH = 490, 76
DROP = (420, 500)
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
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        print("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.35)

    st = snap()
    print("BEFORE %s" % brief(st))

    hand = st.hand(LOCAL)                       # 已按 locationNumber 排序
    cand = [(i, c) for i, c in enumerate(hand) if c.needs_hand_target]
    if not cand:
        print("手里没有需要指向的牌")
        return 1
    idx, card = cand[0]
    need = (card.kredit_cost or 0) + 1
    have = st.kredits[LOCAL] or 0
    print("选中 id=%s（%s，费用 %s）槽位=%d  需要 %d（含指向+1）  持有 %d"
          % (card.card_id, card.card_type, card.kredit_cost, idx, need, have))
    if need > have:
        print("=> 费用不够，不打")
        return 1
    card_x = HAND_X0 + idx * HAND_PITCH

    o = OcrBoardSource()
    if not o.available():
        print("ocr 不可用，拿不到目标坐标")
        return 1
    sto = o.snapshot()
    units = [c for c in sto.field_units("enemy") if c.location != "hq"]
    print("敌方场上目标：%s" % [(c.card_type, c.raw.get("box")) for c in units])
    if not units:
        print("没有可点的敌方单位")
        return 1
    box = units[0].raw.get("box") or {}
    tx, ty = box.get("cx"), box.get("cy")
    if tx is None:
        print("目标没有坐标：%s" % box)
        return 1

    f = win.capture_client_bgr(hwnd)
    cv2.imwrite(SHOTS + r"\pt_before.png", f)
    print("拖拽 client(%d,700) -> %s，落点 %s" % (
        card_x, win.client_to_screen(hwnd, card_x, 700), DROP))
    ok = deploy.drag_deploy(hwnd, card_x, card_y=700, drop=DROP,
                            hover_wait=hand_scanner_v2.HOLD)
    print("drag_deploy -> %s" % ok)

    time.sleep(0.35)
    sx, sy = win.client_to_screen(hwnd, tx, ty)
    print("点目标 client(%s,%s) -> screen(%d,%d)" % (tx, ty, sx, sy))
    print("click -> %s" % actions.click(sx, sy))

    for i in range(5):
        time.sleep(0.55)
        print("  +%.1fs %s" % ((i + 1) * 0.55, brief(snap())))
    f = win.capture_client_bgr(hwnd)
    cv2.imwrite(SHOTS + r"\pt_after.png", f)
    print("shot -> pt_after.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
