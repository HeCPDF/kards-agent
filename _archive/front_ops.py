#!/usr/bin/env python3
"""把支援线的单位拖上前线；可选让某个单位攻击敌方总部。

用法:
  front_ops.py show                  # 打印 mem 与 ocr 的单位/坐标对应关系
  front_ops.py move [front_y]        # 把 back 里的非 HQ 单位逐个拖到前线的 front_y
  front_ops.py attack <card_id>      # 让该单位攻击敌方总部

坐标分工：mem 给身份/location；ocr 给屏幕框。
"""
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import cv2              # noqa: E402
import win              # noqa: E402
from board_api import LOCAL, MemoryBoardSource, OcrBoardSource  # noqa: E402

SHOTS = r"D:\Kards\reverse-data\shots"


def snap():
    s = MemoryBoardSource()
    r = s.snapshot()
    s.close()
    return r


def brief(st):
    return "turn=%s our=%s k=%s/%s backL=%s frontL=%s hqL=%s frontE=%s hqE=%s" % (
        st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
        [(c.card_id, c.slot) for c in st.support(LOCAL)],
        [(c.card_id, c.slot) for c in st.board(LOCAL)],
        st.hq[LOCAL].defense,
        [(c.card_id, c.slot) for c in st.board("enemy")],
        st.hq["enemy"].defense)


def hwnd_of():
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        return None
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.35)
    return hwnd


def ocr_units():
    """-> (our_back[(id? 无), box], our_front[...], enemy_hq_box) 只有坐标与类型。"""
    o = OcrBoardSource()
    if not o.available():
        return None
    sto = o.snapshot()
    front_all = [(c.card_type, c.raw.get("box"), c.can_act)
                 for c in sto.cards if c.location == "frontline"]
    return {
        "back": [(c.card_type, c.raw.get("box")) for c in sto.support(LOCAL)],
        "front": [(t, b) for t, b, _a in front_all],
        "front_act": [(t, b) for t, b, a in front_all if a],
        "enemy_front": [(c.card_type, c.raw.get("box")) for c in sto.board("enemy")],
        "enemy_hq": (sto.hq["enemy"].raw.get("box") if sto.hq["enemy"] else None),
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    mode = sys.argv[1]
    hwnd = hwnd_of()
    if hwnd is None:
        print("没有 kards 窗口")
        return 1

    if mode == "show":
        st = snap()
        print("MEM %s" % brief(st))
        print("OCR %s" % ocr_units())
        return 0

    if mode == "move":
        front_y = int(sys.argv[2]) if len(sys.argv) > 2 else 380
        only = {int(v) for v in sys.argv[3:]} if len(sys.argv) > 3 else None
        if only:
            print("只上前线：%s" % sorted(only))
        for attempt in range(4):
            st = snap()
            allbacks = st.support(LOCAL)
            oc = ocr_units() or {}
            oboxes = sorted([b for _t, b in oc.get("back", []) if b],
                            key=lambda b: b.get("cx") or 0)
            print("第 %d 轮 mem back=%s  ocr cx=%s" % (
                attempt + 1, [(c.card_id, c.slot) for c in allbacks],
                [b.get("cx") for b in oboxes]))
            if len(oboxes) != len(allbacks):
                print("  ⚠ 数量不一致(%d vs %d)，按 mem.slot 升序 / ocr.cx 升序对位"
                      % (len(allbacks), len(oboxes)))
            pairs_all = list(zip(sorted(allbacks, key=lambda c: (c.slot or 0, c.uid)),
                                 oboxes))
            todo = [(c, b) for c, b in pairs_all
                    if only is None or c.card_id in only]
            if not todo:
                print("没有要移动的单位了")
                break
            moved = 0
            for c, b in todo:
                cx, cy = b.get("cx"), b.get("cy")
                if cx is None:
                    continue
                sx, sy = win.client_to_screen(hwnd, cx, cy)
                ex, ey = win.client_to_screen(hwnd, cx, front_y)
                print("  拖 id=%s 从 (%d,%d) 到 (%d,%d)" % (c.card_id, cx, cy, cx, front_y))
                ok = actions.move_drag(sx, sy, ex, ey)
                time.sleep(0.8)
                st2 = snap()
                landed = [x.card_id for x in st2.board(LOCAL)]
                if c.card_id in landed:
                    print("    ✅ 上线成功（id=%s 现在在 frontline）" % c.card_id)
                    moved += 1
                else:
                    print("    ❌ 没上线（frontline 现在=%s）" % landed)
            f = win.capture_client_bgr(hwnd)
            cv2.imwrite(SHOTS + r"\front_after.png", f)
            if moved == 0:
                print("本轮一个都没上去 -> 换个 front_y 再试")
                break
        print("FINAL %s" % brief(snap()))
        return 0

    if mode == "attack":
        want = int(sys.argv[2])
        st = snap()
        oc = ocr_units() or {}
        fronts = sorted(st.board(LOCAL), key=lambda c: (c.slot or 0, c.uid))
        if not any(c.card_id == want for c in fronts):
            print("id=%s 不在前线，先 move。现在前线=%s" % (
                want, [(c.card_id, c.slot) for c in fronts]))
            return 1
        fboxes = sorted([b for _t, b in oc.get("front", []) if b],
                        key=lambda b: b.get("cx") or 0)
        pairs = list(zip(fronts, fboxes))
        print("前线对位：%s" % [(c.card_id, c.slot, (b or {}).get("cx")) for c, b in pairs])
        # 归属**只用内存**判（Card.side / locationNumber），像素侧只贡献坐标。
        # 这里打印可行动集合仅做交叉核对，不参与决策。
        print("（核对用）ocr 可行动(orange)：%s  vs  mem can_act：%s" % (
            [b.get("cx") for _t, b in oc.get("front_act", [])],
            [c.card_id for c, _b in pairs if c.can_act]))
        src = next((b for c, b in pairs if c.card_id == want), None)
        if not src:
            print("对不到位：mem front=%s ocr front=%s" % (
                [(c.card_id, c.slot) for c in fronts], oc))
            return 1
        hq = (oc or {}).get("enemy_hq") or {}
        if hq.get("cx") is None:
            print("拿不到敌方总部坐标：%s" % hq)
            return 1
        print("攻击：id=%s (%s,%s) -> 敌方总部 (%s,%s)" % (
            want, src.get("cx"), src.get("cy"), hq.get("cx"), hq.get("cy")))
        sx, sy = win.client_to_screen(hwnd, src["cx"], src["cy"])
        ex, ey = win.client_to_screen(hwnd, hq["cx"], hq["cy"])
        ok = actions.move_drag(sx, sy, ex, ey)
        print("move_drag -> %s" % ok)
        for i in range(5):
            time.sleep(0.6)
            print("  +%.1fs %s" % ((i + 1) * 0.6, brief(snap())))
        f = win.capture_client_bgr(hwnd)
        cv2.imwrite(SHOTS + r"\attack_after.png", f)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
