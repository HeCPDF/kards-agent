#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentpath —— 一行把本项目自己的路径接好。

    import agentpath   # noqa: F401
    import win, actions, deploy          # 解析到 kards-agent/vendor/

为什么有 vendor/
================
本项目 fork 自 **OCR-Kards-Auto**（yumehanab1，GPL-3.0），但只用得上它的一小块。
**这六个文件是真在用的**（逐个查过引用，不是"先搬过来再说"）：

    win.py      窗口 / DPI / 客户区坐标 / 截图 / 置前      ← ops/screen/shot/grab/工具
    actions.py  鼠标原语                                   ← ops/mull_auto/do_mulligan/工具
    deploy.py   drag_deploy（拖拽出牌的手势）              ← ops
    ui_state.py 模板匹配、界面分类                         ← screen/startmatch/board_api
    cv_io.py    cv2 认中文路径（ui_state 的依赖）          ← ui_state
    handedge.py 手牌扇形左右边缘                           ← ops（**内存里没有这个量**）

上游其余部分（OCR 读盘面、手牌扫描、官网卡表 json、自动打牌状态机）我们**不用**，
一次性的东西在 `_archive/`。**本项目与上游重合得不多**，用不上的不保留。

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


def workspace() -> str:
    """仓库根（含 `reverse-data/` 的那一层）。

    ⚠ 按标志物往上找，**别数 `parents[N]`** —— 搬过一次家，硬编码层数当场失效。
    """
    p = os.path.dirname(AGENT_ROOT)
    while True:
        if os.path.isdir(os.path.join(p, "reverse-data")):
            return p
        nxt = os.path.dirname(p)
        if nxt == p:
            return os.path.dirname(AGENT_ROOT)
        p = nxt


def _rd(*parts: str) -> str:
    d = os.path.join(workspace(), "reverse-data", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def shots(*parts: str) -> str:
    """`reverse-data/shots[/...]`（证据截图的家）。"""
    return _rd("shots", *parts)


def logs(*parts: str) -> str:
    """`reverse-data/logs[/...]`（快照与证据的家）。"""
    return _rd("logs", *parts)


def upstream_src():
    """上游 `OCR-Kards-Auto/src` 的路径，**存在才接进 sys.path**，否则 None。

    只有 OCR 读盘面那条路（`board.read_field`）还需要上游 ——
    它**只是核对手段**（和内存读数互验），不是对等后端。
    ⇒ **上游没 checkout 也能跑**，只是这条路不可用；调用方必须能接受它返回 None。
    """
    p = os.environ.get("KARDS_OCR_ROOT")
    p = os.path.join(p, "src") if p else os.path.join(
        os.path.dirname(AGENT_ROOT), "OCR-Kards-Auto", "src")
    if not os.path.isdir(p):
        return None
    if p not in sys.path:
        sys.path.append(p)          # ★ append：vendor 优先，别被上游同名模块盖掉
    return p
