# -*- coding: utf-8 -*-
"""base.version —— 项目版本号的唯一来源（`tools/make_release.py` 读它命名发布包；CHANGELOG.md 与之对应）。

注意：这是 kards-agent **本身**的版本，不是游戏版本（游戏版本见 `kardsmem/version.py`），
也不是 `kardsmem.__version__`（读侧子包自己的接口版本）。
"""
from __future__ import annotations

__version__ = "0.1.0"
