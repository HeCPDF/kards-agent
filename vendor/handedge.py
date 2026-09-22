#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vendored from OCR-Kards-Auto/src/hand_calibrate.py (yumehanab1), GPL-3.0
# https://github.com/yumehanab1/OCR-Kards-Auto -- see ../LICENSE
"""handedge —— 上游的**手牌扇形边缘检测**，只抓这四个函数。

为什么只搬这么点
==============
上游的 `hand_calibrate.py` 连带着 `hand_scanner_v2`（1551 行）、`ui_state`、
`cv_io` 一整条 OCR 链，而我们实际只用 `detect_left_edge` ——
它是纯粹的逐列色差扫描，只要 numpy 和一帧图。

手牌 x 坐标是内存里**没有**的东西（扇形展开是纯客户端排版），
所以这一块必须走像素，是真正用得上上游算法的地方。

★ 调用条件（上游的原注里写得很清楚，别绕过）：
  必须在【鼠标停在安全点、没悬停任何手牌】时调用。
  悬停会把扇形展开，左边缘跟着变（实测 367 vs 394）。
"""
from __future__ import annotations

import numpy as np

EDGE_Y0, EDGE_Y1 = 680, 715
EDGE_THRESH = 45
EDGE_X_MIN, EDGE_X_MAX = 150, 1100
BG_SAMPLE = (150, 250)        # 桌面色参考区域(x 范围)
MIN_FAN_RUN_W = 40
EDGE_HYSTERESIS = True
EDGE_THRESH_WEAK = 25


def _extend_edge(diff, x, step):
    """
    从一个已经过强阈值的边缘点**向外**走,只要还亮着(> EDGE_THRESH_WEAK)就继续。

    step = -1 往左(左边缘)、+1 往右(右边缘)。只看**连续**像素,不跳空隙 ——
    跳空隙等于把"隔着背景的装饰"也连进来(踩过,见 EDGE_HYSTERESIS 的说明)。
    """
    if x is None or not EDGE_HYSTERESIS:
        return x
    while EDGE_X_MIN <= x + step < EDGE_X_MAX and diff[x + step] > EDGE_THRESH_WEAK:
        x += step
    return x


def _edge_runs(diff, reverse=False):
    """把"连续 ≥6 列超阈值"的区段都找出来,返回 [(起点, 宽度)],按扫描方向排。"""
    runs = []
    run = 0
    rng = (range(min(EDGE_X_MAX, len(diff)) - 1, EDGE_X_MIN - 1, -1)
           if reverse else range(EDGE_X_MIN, min(EDGE_X_MAX, len(diff))))
    last = None
    for x in rng:
        if diff[x] > EDGE_THRESH:
            run += 1
            last = x
        else:
            if run >= 6:
                # reverse 时 last 是这段的"最左端",x+1 是"最右端"
                start = (x + 1) if reverse else (x - run)
                runs.append((start, run))
            run = 0
    if run >= 6:
        start = (last - run + 1) if reverse else (last - run + 1)
        runs.append((start, run))
    return runs


def detect_left_edge(frame):
    """
    返回手牌扇形左边缘的 x,找不到返回 None。

    做法:在 y 680-715 这段(卡面露出区)逐列比较与桌面背景色的平均差异,
    取第一段**够宽**的连续超阈值区段的起点。

    ★ 2026-09-11 傍晚修:"第一个连续 6 列"会被**桌面装饰**骗到(见 MIN_FAN_RUN_W),
      实测把左边缘判成 277(真值 390),于是布局表查不到 -> **整局不出牌**。
      现在只有宽度 ≥ MIN_FAN_RUN_W 的区段才认;被跳过的会记在
      `detect_left_edge.skipped` 里,方便诊断工具打出来。

    ★ 重要:必须在【鼠标停在 SAFE_POINT、没有悬停任何手牌】时调用。
    KARDS 悬停手牌时会把扇形展开(被悬停的牌上移、其余让位),左边缘会跟着
    变(实测同一副手牌,悬停左边时 x=367、悬停中间时 x=394)。测量条件不一致
    的话,这张对照表就废了。校准和使用两边都要先回 SAFE_POINT。
    """
    x0, x1 = BG_SAMPLE
    bg = frame[EDGE_Y0:EDGE_Y1, x0:x1].reshape(-1, 3).mean(axis=0)
    strip = frame[EDGE_Y0:EDGE_Y1, :].astype(np.int16)
    diff = np.abs(strip - bg).mean(axis=(0, 2))

    runs = _edge_runs(diff, reverse=False)
    detect_left_edge.skipped = [r for r in runs if r[1] < MIN_FAN_RUN_W]
    for start, w in runs:
        if w >= MIN_FAN_RUN_W:
            # ★ 2026-09-19:锚点选出来之后再沿弱阈值**向外**延伸(见 EDGE_HYSTERESIS)
            return _extend_edge(diff, start, -1)
    return _extend_edge(diff, runs[0][0], -1) if runs else None


def detect_right_edge(frame):
    """
    返回手牌扇形右边缘的 x,找不到返回 None。

    与 detect_left_edge 完全对称(从右往左扫)**而且同样要求区段够宽**
    (见 MIN_FAN_RUN_W:右边也可能有装饰,比如右侧那块鹰徽面板)。

    同样要在鼠标停 SAFE_POINT 时调用(见 detect_left_edge 的说明)。
    """
    x0, x1 = BG_SAMPLE
    bg = frame[EDGE_Y0:EDGE_Y1, x0:x1].reshape(-1, 3).mean(axis=0)
    strip = frame[EDGE_Y0:EDGE_Y1, :].astype(np.int16)
    diff = np.abs(strip - bg).mean(axis=(0, 2))

    runs = _edge_runs(diff, reverse=True)
    detect_right_edge.skipped = [r for r in runs if r[1] < MIN_FAN_RUN_W]
    for start, w in runs:
        if w >= MIN_FAN_RUN_W:
            # ★ 2026-09-19:锚点选出来之后再沿弱阈值**向外**延伸(见 EDGE_HYSTERESIS)。
            #   实机那一帧最右那张蓝卡只剩 8px 过阈值,靠这一步才回到 916。
            return _extend_edge(diff, start + w - 1, +1)
    return (_extend_edge(diff, runs[0][0] + runs[0][1] - 1, +1)
            if runs else None)
