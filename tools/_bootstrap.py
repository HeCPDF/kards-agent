#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_bootstrap —— `kards-agent/tools/` 下脚本的路径引导（替代搬走的 `_agentpath`）。

    import _bootstrap   # noqa: F401   —— 这一行就够

之后可用：`import kardsmem` / `import board_api` / `import win` / `import actions`。

为什么要有这一层
================
这些脚本 2026-09-22 从 `reverse-data/tools/` 搬进 `kards-agent/tools/`。
搬之前每个脚本都硬编码了 `D:\\Kards\\...` 的 sys.path —— 搬完就全废了。
**绝对路径不要再写进脚本里**：一律按标志物往上找（`reverse-data/` + `.git`）。

顺带提供几个目录解析（证据/截图仍然落在 `reverse-data/logs|shots`，那是取材产物）。
"""
from __future__ import annotations

import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(TOOLS_DIR)

for _p in (AGENT_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)



def workspace() -> str:
    """仓库根（含 `reverse-data/` 的那一层）。"""
    p = AGENT_ROOT
    while True:
        if os.path.isdir(os.path.join(p, "reverse-data")):
            return p
        nxt = os.path.dirname(p)
        if nxt == p:
            raise RuntimeError("往上找不到仓库根（应含 reverse-data/）")
        p = nxt


def work_dir(*parts: str) -> str:
    """`reverse-data/<parts...>`，目录不存在就建。"""
    d = os.path.join(workspace(), "reverse-data", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def logs_dir() -> str:
    return work_dir("logs")


def shots_dir() -> str:
    return work_dir("shots")
