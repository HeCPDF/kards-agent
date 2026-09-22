#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentpath —— 一行把本项目自己的路径接好。

    import agentpath   # noqa: F401
    import win, actions, deploy          # 解析到 kards-agent/vendor/

为什么有 vendor/
================
本项目 fork 自 **OCR-Kards-Auto**（yumehanab1，GPL-3.0），但只用得上它的一小块：

    win.py      窗口 / DPI / 客户区坐标 / 截图 / 置前
    actions.py  鼠标原语
    deploy.py   drag_deploy（拖拽出牌的手势）

上游其余部分（OCR 读盘面、手牌扫描、官网卡表 json、自动打牌状态机）我们**不用**：
盘面从内存读（`kardsmem`），出牌逻辑是另一条路线。
所以这三个文件搬进 `vendor/` 原样保留，不再依赖上游那棵工作树存在。

`AGENT_ROOT` 也接进 sys.path，于是 `import board_api` / `import kardsmem` 一起可用。
"""
from __future__ import annotations

import os
import sys

AGENT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(AGENT_ROOT, "vendor")

for _p in (VENDOR, AGENT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def upstream_src():
    """上游 `OCR-Kards-Auto/src` 的路径，**存在才接进 sys.path**，否则 None。

    只有 OCR 读盘面那条路（`board.read_field`、`hand_calibrate`、`hand_scanner_v2`）
    还需要它 —— 那是硬约束②的 `ocr` 后端，内存后端不依赖。
    ⇒ **上游没 checkout 也能跑**，只是 ocr 后端不可用。调用方必须能接受它返回 None。
    """
    p = os.environ.get("KARDS_OCR_ROOT")
    p = os.path.join(p, "src") if p else os.path.join(
        os.path.dirname(AGENT_ROOT), "OCR-Kards-Auto", "src")
    if not os.path.isdir(p):
        return None
    if p not in sys.path:
        sys.path.append(p)          # ★ append：vendor 优先，别被上游同名模块盖掉
    return p
