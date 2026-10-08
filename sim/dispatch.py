#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.dispatch —— 兼容壳：事件队列 + 分发器已端口进 `engine.dispatch`（P2，2026-10-03）。

旧 import（`from sim.dispatch import Dispatcher, Event, Result`，如 `tests/test_dispatch_queue.py`）
保持不变；缺口头改成 `engine.dispatch：`（测试只匹配子串）。
"""
from engine.dispatch import (DEFAULT_MAX_DEPTH, DEFAULT_MAX_STEPS, Dispatcher,  # noqa: F401
                             Event, Result, Run)

__all__ = ["DEFAULT_MAX_DEPTH", "DEFAULT_MAX_STEPS", "Dispatcher", "Event", "Result", "Run"]

