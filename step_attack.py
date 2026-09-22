#!/usr/bin/env python3
"""单步攻击（调试用）：指定我方单位 id 和打法，执行一次拖拽并回读验证。

用法:
  step_attack.py <attacker_card_id> face        # 打敌方总部
  step_attack.py <attacker_card_id> front       # 打敌方前线那个单位
  step_attack.py <attacker_card_id> back <n>    # 打敌方后排第 n 个（0 起）

attacker 可以在我方前排或后排（后排单位能否打到敌方前线由游戏判定）。
"""
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import win              # noqa: E402
from board_api import ENEMY, LOCAL, MemoryBoardSource, OcrBoardSource  # noqa: E402


def snap():
    s = MemoryBoardSource()
    st = s.snapshot()
    s.close()
    return st


def brief(st):
    return "k=%s/%s frontL=%s backL=%s enemyBack=%s enemyFront=%s hq=%s/%s" % (
        st.kredits[LOCAL], st.kredits[ENEMY],
        [(c.card_id, c.attack, c.defense) for c in st.board(LOCAL)],
        [(c.card_id, c.attack, c.defense) for c in st.support(LOCAL)],
        [(c.card_id, c.attack, c.defense) for c in st.support(ENEMY)],
        [(c.card_id, c.attack, c.defense) for c in st.board(ENEMY)],
        st.hq[LOCAL].defense, st.hq[ENEMY].defense)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    want = int(sys.argv[1])
    mode = sys.argv[2]
    idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    win.set_dpi_aware()
    hwnd = win.find_by_process("kards")[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.4)

    st = snap()
    print("BEFORE %s" % brief(st))

    o = OcrBoardSource()
    if not o.available():
        print("ocr 不可用")
        return 1
    sto = o.snapshot()
    front_boxes = sorted([c.raw.get("box") for c in sto.cards
                          if c.location == "frontline" and c.raw.get("box")],
                         key=lambda b: b["cx"])
    back_boxes = sorted([c.raw.get("box") for c in sto.support(LOCAL)
                         if c.raw.get("box")], key=lambda b: b["cx"])
    enemy_back = sorted([c.raw.get("box") for c in sto.support(ENEMY)
                         if c.raw.get("box")], key=lambda b: b["cx"])
    hq_e = sto.hq[ENEMY].raw.get("box") if sto.hq[ENEMY] else None

    # attacker 坐标：先在前线配对，再在后排配对
    src = None
    f_l = sorted(st.board(LOCAL), key=lambda c: (c.slot or 0, c.uid))
    print("我方前线 mem=%s  ocr front cx=%s （前线若为敌方占据，这些框不属于我们）"
          % ([(c.card_id, c.slot) for c in f_l], [b["cx"] for b in front_boxes]))
    b_l = sorted(st.support(LOCAL), key=lambda c: (c.slot or 0, c.uid))
    print("我方后排 mem=%s  ocr back  cx=%s" % (
        [(c.card_id, c.slot) for c in b_l], [b["cx"] for b in back_boxes]))
    for c, b in zip(b_l, back_boxes):
        if c.card_id == want:
            src = ("back", b)
    for c, b in zip(f_l, front_boxes):
        if c.card_id == want:
            src = ("front", b)
    if not src:
        print("找不到 id=%s 的坐标（前排/后排都没配上）" % want)
        return 1
    where, sb = src
    print("attacker id=%s 在我方%s (%s,%s)" % (want, where, sb["cx"], sb["cy"]))

    if mode == "face":
        if not hq_e:
            print("拿不到敌方总部坐标")
            return 1
        tb, tname = hq_e, "敌方总部(脸)"
    elif mode == "front":
        if not front_boxes:
            print("敌方前线没有单位")
            return 1
        tb, tname = front_boxes[0], "敌方前线#0"
    elif mode == "back":
        if idx >= len(enemy_back):
            print("敌方后排只有 %d 个框" % len(enemy_back))
            return 1
        tb, tname = enemy_back[idx], "敌方后排#%d" % idx
    else:
        print("未知打法 %s" % mode)
        return 2

    ee = sorted(st.support(ENEMY), key=lambda c: (c.slot or 0, c.uid))
    f_e = sorted(st.board(ENEMY), key=lambda c: (c.slot or 0, c.uid))
    print("目标 %s box=(%s,%s)  mem 敌方后排=%s 前线=%s" % (
        tname, tb["cx"], tb["cy"], [(c.card_id, c.defense) for c in ee],
        [(c.card_id, c.defense) for c in f_e]))
    print("拖 (%s,%s) -> (%s,%s)" % (sb["cx"], sb["cy"], tb["cx"], tb["cy"]))
    p = win.client_to_screen(hwnd, sb["cx"], sb["cy"])
    q = win.client_to_screen(hwnd, tb["cx"], tb["cy"])
    actions.move_drag(p[0], p[1], q[0], q[1])
    for i in range(4):
        time.sleep(0.6)
        print("  +%.1fs %s" % ((i + 1) * 0.6, brief(snap())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
