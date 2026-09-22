#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""startmatch.py —— 从任意界面走到"对局中"。

界面识别复用 OCR-Kards-Auto 的模板状态机（`config/states.json` + `templates.json`），
**点击坐标另记**：模板框的是标题/图案，不一定是按钮本身
（例：`defeat_btn` 框的是"失败"二字，按钮"继续"在下面）。

用法:
  python startmatch.py              # 走到 in_game 为止
  python startmatch.py --to mulligan   # 走到换牌界面就停（留给 pickwatch 抓证据）
  python startmatch.py --probe      # 只报当前界面，不点任何东西

⚠ 会动鼠标。⚠ 换牌界面默认**不自动确认** —— 换牌窗口是抓选择界面正证据的机会，
  别顺手点掉（要确认加 `--mulligan-ok`）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

SRC = r"D:\Kards\OCR-Kards-Auto"
sys.path.insert(0, os.path.join(SRC, "src"))

import win                                                    # noqa: E402
import actions                                                # noqa: E402
import ui_state as U                                          # noqa: E402

# 界面 → 该点哪里（客户区坐标）。None = 只等，不点。
# 坐标来源：模板 region 的中心，或实机截图量出来的按钮位置（后者标了「量」）。
CLICK = {
    "defeat":      (640, 680),   # 量：底部"继续"（模板框的是"失败"字样，不是按钮）
    "victory":     (640, 680),   # 量：同上位置的"继续"
    "main_menu":   (59, 147),    # play_btn region 中心
    "deck_select": (1164, 630),  # deck_ok_btn region 中心
    "post_match":  (640, 680),   # 量：结算后的等级/奖励页，同一个"继续"
    "queueing":    None,         # 排队中，等
    "mulligan":    (638, 667),   # mulligan_ok_btn region 中心（与规格 §7.2 一致）
    "in_game":     None,
}
CASUAL_MODE = (1217, 563)        # casual_mode_btn region 中心

# 牌组选择页左侧的模式列表（实机截图量的客户区坐标）。
MODES = {
    "versus":   (240, 135),   # 对战模式（真人）
    "training": (240, 186),   # 训练模式（人机）—— 攒数据用这个：对手不会投降
    "arena":    (240, 237),   # 竞技场
    "campaign": (240, 288),   # 战役模式
    "code":     (240, 339),   # 战斗代码
    "event":    (240, 390),   # 赛事模式
}
# 右下角那个浅色大按钮才是真正的开始（左上角的“开始”只是展开/收起模式列表）。
# 但它的 y **既随模式变也随牌组变**，实测四种：
#   对战+经典牌组  → 按钮写着“经典”      y=651（上方多一块赛制徽记）
#   对战+普通牌组  → “开始”              y=625（上方是排位/休闲页签）
#   训练模式        → “开始”              y=604（上方什么都没有）
#   战斗代码        → “开始”              y=625（上方是输入框）
# 所以不能写死，也不能按模式查表 —— 每次现场找。
BTN_ROI = (1040, 400, 1280, 720)   # 右下面板的搜索范围（客户区 x0,y0,x1,y1）
BTN_MIN_W, BTN_AREA = 150, 5000    # 排位/休闲页签才 ~108 宽，这两道阀把它们挡在外面
START_BTN_FALLBACK = (1163, 651)   # 实在找不到时的保底（对战+经典）


def find_start_btn(frame):
    """在右下面板里找那个浅色大按钮 → (x, y)；找不到返回 None。

    不认字（按钮上可能写“开始”也可能写赛制名如“经典”），只认几何：
    面板里最靠下的一个大浅色连通域。
    """
    import cv2
    import numpy as np
    x0, y0, x1, y1 = BTN_ROI
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y0:y1, x0:x1]
    n, _lab, stats, cent = cv2.connectedComponentsWithStats((g > 170).astype(np.uint8), 8)
    best = None
    for i in range(1, n):
        _x, _y, w, h, a = stats[i]
        if w < BTN_MIN_W or not (25 < h < 80) or a < BTN_AREA:
            continue
        cx, cy = int(cent[i][0]) + x0, int(cent[i][1]) + y0
        if best is None or cy > best[1]:
            best = (cx, cy)
    return best


def _load():
    meta = json.load(open(os.path.join(SRC, "config", "templates.json"), encoding="utf-8"))
    cwd = os.getcwd()
    os.chdir(SRC)                # 模板路径是相对 OCR-Kards-Auto 根的
    try:
        tpls = U.load_templates(meta)
    finally:
        os.chdir(cwd)
    states = U.load_states(os.path.join(SRC, "config", "states.json"))
    return tpls, states


def look(tpls, states, hwnd):
    f = win.capture_client_bgr(hwnd, allow_screen_fallback=True)
    if f is None:
        return None, {}, None
    st, matches = U.classify(f, tpls, states)
    return st, matches, f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="in_game")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--mulligan-ok", action="store_true",
                    help="换牌界面也自动点确认（默认停在那里，留给 pickwatch）")
    ap.add_argument("--mode", default="training", choices=sorted(MODES),
                    help="开局模式，默认 training（人机）")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args(argv)

    win.set_dpi_aware()
    ws = win.find_by_process("kards")
    if not ws:
        print("游戏没在跑")
        return 2
    h = ws[0]["hwnd"]
    win.bring_to_front(h)
    time.sleep(0.6)
    tpls, states = _load()

    if a.probe:
        st, matches, _ = look(tpls, states, h)
        print("state = %s" % st)
        print("matches = %s" % {k: round(v, 3) for k, v in
                                sorted(matches.items(), key=lambda t: -t[1])})
        _f = look(tpls, states, h)[2]
        print("start_btn = %s" % (find_start_btn(_f) if _f is not None else None,))
        return 0

    t0, last, stuck = time.time(), None, 0
    while time.time() - t0 < a.timeout:
        st, matches, f = look(tpls, states, h)
        if st != last:
            print("[%5.0fs] %s  %s" % (time.time() - t0, st,
                                       {k: round(v, 2) for k, v in matches.items()}))
            last, stuck = st, 0
        else:
            stuck += 1
        if st == a.to:
            print("到了：%s" % st)
            return 0
        if st == "mulligan" and not a.mulligan_ok:
            print("到换牌界面了 —— 默认不自动确认（--mulligan-ok 才点）。")
            return 0
        if st in ("deck_select", None):
            # 牌组选择页：模板匹配不稳（"开始"展开态与模板不同），按坐标走
            if a.mode in MODES:
                actions.click(*win.client_to_screen(h, *MODES[a.mode]))
                time.sleep(0.6)
            f2 = win.capture_client_bgr(h, allow_screen_fallback=True)
            btn = find_start_btn(f2) if f2 is not None else None
            if btn is None:
                print("   没找到开始按钮，用保底坐标 %s" % (START_BTN_FALLBACK,))
                btn = START_BTN_FALLBACK
            else:
                print("   开始按钮 @ %s" % (btn,))
            actions.click(*win.client_to_screen(h, *btn))
            time.sleep(2.0)
            continue
        tgt = CLICK.get(st)
        if tgt is not None:
            actions.click(*win.client_to_screen(h, *tgt))
            time.sleep(1.2)
        else:
            time.sleep(1.0)
        if stuck > 40:
            print("卡在 %s 不动了（40 次没变化）" % st)
            return 1
    print("超时，最后停在 %s" % last)
    return 1


if __name__ == "__main__":
    sys.exit(main())
