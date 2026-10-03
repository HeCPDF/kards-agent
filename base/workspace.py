#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base.workspace —— 开发布局下的目录定位（伞仓库根、`reverse-data/` 取材目录）。仅供 tools/ 与开发期脚本用；
运行期文件的位置见 `base.paths`。
"""
from __future__ import annotations

import os
import sys

AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # 仓库根


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
