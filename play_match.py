#!/usr/bin/env python3
"""自动打完一局（人机）。策略：坦克打脸（前线单位优先打敌方总部）。

要点（都是踩过的坑）：
  * 手牌 x **不写死**：光标停 SAFE_POINT 量扇形左右边缘 -> 查
    `config/hand_layout.json` 的 `layouts[count].probes`；张数用 mem 的手牌数交叉校验。
  * **每出一张牌都要重新量**（张数变了，扇形按算法重排）。
  * 每一步都用内存回读判成败 —— `drag_deploy` 返回 True 只代表鼠标事件发出去了。
  * 全屏选牌界面（开发/预报）会盖住棋盘：用 `end_turn_btn` 模板是否可见来判定，
    然后点中间那张牌。

用法: play_match.py [max_our_turns] [max_seconds]
"""
import json
import os
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")

import actions          # noqa: E402
import cv2              # noqa: E402
import deploy           # noqa: E402
import hand_calibrate   # noqa: E402
import hand_scanner_v2 as hs  # noqa: E402
import ui_state         # noqa: E402
import win              # noqa: E402
from board_api import ENEMY, LOCAL, MemoryBoardSource, OcrBoardSource  # noqa: E402

PROJ = r"D:\Kards\OCR-Kards-Auto"
SHOTS = r"D:\Kards\reverse-data\shots"
LOGP = r"D:\Kards\reverse-data\logs\play_match.log"
FRONT_Y = 380
HOLD = hs.HOLD
LAYOUT_PATH = os.path.join(PROJ, "config", "hand_layout.json")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOGP, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def snap():
    s = MemoryBoardSource()
    st = s.snapshot()
    s.close()
    return st


def brief(st):
    return "turn=%s our=%s k=%s/%s backL=%s frontL=%s enemyField=%s hq=%s/%s fin=%s" % (
        st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
        [(c.card_id, c.slot) for c in st.support(LOCAL)],
        [(c.card_id, c.attack, c.slot) for c in st.board(LOCAL)],
        [(c.card_id, c.defense) for c in st.field_units(ENEMY) if c.location != "hq"],
        st.hq[LOCAL].defense, st.hq[ENEMY].defense, st.match_finished)


def cs(hwnd, x, y):
    return win.client_to_screen(hwnd, x, y)


def drag(hwnd, sx, sy, ex, ey):
    p, q = cs(hwnd, sx, sy), cs(hwnd, ex, ey)
    return actions.move_drag(p[0], p[1], q[0], q[1])


def hand_probes(hwnd, want_n):
    """量边缘 -> 查布局表 -> 该张数下的 probes。对不上返回 None（fail-closed）。"""
    actions.set_cursor(*cs(hwnd, *hs.SAFE_POINT))
    time.sleep(hs.SAFE_SETTLE)
    frame = win.capture_client_bgr(hwnd)
    L, R = hand_calibrate.detect_edges(frame)
    try:
        layouts = json.load(open(LAYOUT_PATH, encoding="utf-8"))["layouts"]
    except Exception as exc:
        log("读 hand_layout.json 失败: %s" % exc)
        return None
    cands = [v for v in layouts.values()
             if L is not None and abs(v["left_edge"] - L) <= hs.LAYOUT_EDGE_TOL]
    both = [v for v in cands if R is not None and v.get("right_edge")
            and abs(v["right_edge"] - R) <= hs.LAYOUT_RIGHT_TOL]
    pick = None
    if both:
        pick = min(both, key=lambda v: abs(v["left_edge"] - L) + abs(v["right_edge"] - R))
    elif cands:
        pick = min(cands, key=lambda v: abs(v["left_edge"] - L))
    if not pick:
        log("边缘 (%s,%s) 在布局表里查不到（容差 %d）" % (L, R, hs.LAYOUT_EDGE_TOL))
        return None
    probes = pick.get("probes") or []
    if len(probes) != want_n:
        log("⚠ 布局表说 %s 张、内存说 %d 张 -> 不信，放弃本次出牌" % (
            pick.get("count"), want_n))
        return None
    log("手牌布局：边缘(%s,%s) -> 「%s 张」probes=%s" % (L, R, pick.get("count"), probes))
    return probes


def modal_up(hwnd, tpls):
    """"全屏选牌界面盖住了棋盘"的判据：End Turn 模板看不见。"""
    tpl = (tpls or {}).get("end_turn_btn")
    if tpl is None:
        return False
    frame = win.capture_client_bgr(hwnd)
    score, _box = ui_state.match_one(frame, tpl)
    return score < 0.60, frame, score


def handle_modal(hwnd, tag):
    """开发/预报那种"屏幕中间几张牌"的界面：点最靠中间那张。"""
    frame = win.capture_client_bgr(hwnd)
    p = os.path.join(SHOTS, "modal_%s.png" % tag)
    cv2.imwrite(p, frame)
    boxes = []
    try:
        import board
        boxes = [b for b in (board.card_boxes(frame) or [])
                 if 140 < (b.get("cy") or 0) < 560 and (b.get("w") or 0) > 60]
    except Exception as exc:
        log("card_boxes 失败: %s" % exc)
    log("选牌界面：存帧 %s，候选框 %s" % (p, [(b["cx"], b["cy"]) for b in boxes]))
    if boxes:
        box = min(boxes, key=lambda b: abs(b["cx"] - 640))
        tx, ty = box["cx"], box["cy"]
    else:
        tx, ty = 640, 360
    actions.click(*cs(hwnd, tx, ty))
    log("点中间那张 -> client(%d,%d)" % (tx, ty))
    time.sleep(1.6)


def do_attack(hwnd, st, oc):
    """前线里能行动的、攻击力>0 的 -> 先打敌方总部（打脸），打不动再找场上单位。"""
    fronts = sorted(st.board(LOCAL), key=lambda c: (c.slot or 0, c.uid))
    actors = [c for c in fronts if c.can_act and (c.attack or 0) > 0]
    if not actors:
        return 0
    fboxes = sorted([b for b in (oc.get("front") or []) if b], key=lambda b: b["cx"])
    pairs = list(zip(fronts, fboxes))
    targets = []
    if (oc.get("hq_enemy") or {}).get("cx") is not None:
        targets.append(("敌方总部(脸)", oc["hq_enemy"]))
    for b in oc.get("enemy") or []:
        if b:
            targets.append(("敌方单位", b))
    hits = 0
    for c in actors:
        src = next((b for cc, b in pairs if cc.card_id == c.card_id), None)
        if not src:
            log("  id=%s 拿不到坐标，跳过" % c.card_id)
            continue
        for tname, tb in targets:
            b4 = snap()
            hq0 = b4.hq[ENEMY].defense
            f0 = [(x.card_id, x.defense) for x in b4.field_units(ENEMY)]
            log("攻击 id=%s(atk=%s) (%s,%s) -> %s (%s,%s)" % (
                c.card_id, c.attack, src["cx"], src["cy"], tname, tb["cx"], tb["cy"]))
            drag(hwnd, src["cx"], src["cy"], tb["cx"], tb["cy"])
            time.sleep(1.0)
            af = snap()
            hq1 = af.hq[ENEMY].defense
            f1 = [(x.card_id, x.defense) for x in af.field_units(ENEMY)]
            if hq1 != hq0 or f1 != f0:
                log("  ✅ 打中：脸 %s->%s，敌方场上 %s->%s" % (hq0, hq1, f0, f1))
                hits += 1
                break
            log("  ❌ 没变化")
        else:
            log("  ⚠ id=%s 打不动任何目标" % c.card_id)
    return hits


def do_deploy(hwnd, tpls, max_plays=4):
    """出得起的非指向牌，便宜的先出；每出一张都重新量手牌布局。"""
    played = 0
    for _ in range(max_plays):
        st = snap()
        k = st.kredits[LOCAL] or 0
        hand = st.hand(LOCAL)                      # 已按 locationNumber 排序
        if not hand or k <= 0:
            return played
        try:
            import board
            field = board.read_field(win.capture_client_bgr(hwnd))
            cands = list(deploy.deploy_candidates(field))
        except Exception as exc:
            log("deploy_candidates 失败: %s" % exc)
            cands = []
        cands += [(420, 500), (860, 500)]
        if not cands:
            return played
        probes = hand_probes(hwnd, len(hand))
        if not probes:
            return played
        # 便宜的先出；指向牌按"卡面费用"估（目标上的税拿不到就先不算）
        order = sorted(range(len(hand)), key=lambda i: (hand[i].kredit_cost or 99))
        done = False
        for i in order:
            c = hand[i]
            if (c.kredit_cost or 99) > (st.kredits[LOCAL] or 0):
                continue
            for dx, dy in cands[:3]:
                ids0 = [h.card_id for h in snap().hand(LOCAL)]
                log("出牌 id=%s cost=%s 手牌x=%d 落点(%d,%d)" % (
                    c.card_id, c.kredit_cost, probes[i], dx, dy))
                deploy.drag_deploy(hwnd, probes[i], card_y=700, drop=(dx, dy),
                                   hover_wait=HOLD)
                time.sleep(1.1)
                af = snap()
                ids1 = [h.card_id for h in af.hand(LOCAL)]
                if c.card_id not in ids1 and len(ids1) < len(ids0):
                    log("  ✅ 出牌成功（手牌 %s -> %s, k=%s）" % (
                        ids0, ids1, af.kredits[LOCAL]))
                    played += 1
                    done = True
                    break
                log("  ❌ 没出去（手牌仍 %s）" % ids1)
            if done:
                break
        if not done:
            return played
    return played


def do_move_front(hwnd, st, oc):
    if st.frontline_owner not in (None, LOCAL):
        log("前线是敌方的，不上线")
        return 0
    backs = sorted(st.support(LOCAL), key=lambda c: (c.slot or 0))
    bboxes = sorted([b for b in (oc.get("back") or []) if b], key=lambda b: b["cx"])
    moved = 0
    for c, b in zip(backs, bboxes):
        st = snap()
        if (st.kredits[LOCAL] or 0) <= 0:
            break
        log("上线 id=%s (%s,%s) -> (%s,%d)" % (c.card_id, b["cx"], b["cy"], b["cx"], FRONT_Y))
        drag(hwnd, b["cx"], b["cy"], b["cx"], FRONT_Y)
        time.sleep(1.0)
        af = snap()
        if any(x.card_id == c.card_id for x in af.board(LOCAL)):
            log("  ✅ 上线（k=%s）" % af.kredits[LOCAL])
            moved += 1
        else:
            log("  ❌ 没上去（前线=%s）" % [(x.card_id, x.slot) for x in af.board(LOCAL)])
    return moved


def end_turn(hwnd, tpls):
    tpl = (tpls or {}).get("end_turn_btn")
    if tpl is None:
        return False
    frame = win.capture_client_bgr(hwnd)
    score, box = ui_state.match_one(frame, tpl)
    if box is None or score < 0.60:
        return False
    cx, cy = int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)
    actions.click(*cs(hwnd, cx, cy))
    log("点结束回合（score=%.3f）" % score)
    return True


def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 2400.0
    win.set_dpi_aware()
    wins = win.find_by_process("kards")
    if not wins:
        log("没有 kards 窗口")
        return 1
    hwnd = wins[0]["hwnd"]
    win.bring_to_front(hwnd)
    time.sleep(0.4)
    tpls = ui_state.load_templates(ui_state.load_meta(
        os.path.join(PROJ, "config", "templates.json")))
    log("模板 %d 个；开始 %s" % (len(tpls), brief(snap())))

    t0 = time.time()
    our_turns = 0
    while our_turns < cap and time.time() - t0 < budget:
        st = snap()
        if st.match_finished:
            log("对局结束（match_finished）：%s" % brief(st))
            break
        if not st.our_turn:
            time.sleep(2.5)
            continue
        our_turns += 1
        log("===== 我方第 %d 个回合 ===== %s" % (our_turns, brief(st)))

        up, frame, score = modal_up(hwnd, tpls)
        if up:
            log("检测到全屏选牌界面（end_turn 模板 score=%.3f）" % score)
            handle_modal(hwnd, "t%d_%d" % (st.turn, our_turns))
            continue

        do_attack(hwnd, st, ocr_view())
        do_deploy(hwnd, tpls)
        do_move_front(hwnd, snap(), ocr_view())
        st = snap()
        if st.match_finished:
            log("对局结束：%s" % brief(st))
            break
        up, frame, score = modal_up(hwnd, tpls)
        if up:
            log("回合内又出现选牌界面（score=%.3f）" % score)
            handle_modal(hwnd, "post_t%d" % st.turn)
            continue
        if not end_turn(hwnd, tpls):
            log("结束回合按钮不可见，等 3s")
            time.sleep(3)
            continue
        time.sleep(2.0)
        while time.time() - t0 < budget:
            st = snap()
            if st.our_turn or st.match_finished:
                break
            time.sleep(2.0)

    frame = win.capture_client_bgr(hwnd)
    cv2.imwrite(os.path.join(SHOTS, "match_final.png"), frame)
    log("收工（我方回合 %d）：%s" % (our_turns, brief(snap())))
    return 0


def ocr_view():
    o = OcrBoardSource()
    if not o.available():
        return {}
    sto = o.snapshot()
    return {
        "front": [c.raw.get("box") for c in sto.cards if c.location == "frontline"],
        "back": [c.raw.get("box") for c in sto.support(LOCAL)],
        "enemy": [c.raw.get("box") for c in sto.cards
                  if c.side == ENEMY and c.location in ("back", "frontline")],
        "hq_enemy": (sto.hq[ENEMY].raw.get("box") if sto.hq[ENEMY] else None),
    }


if __name__ == "__main__":
    sys.exit(main())
