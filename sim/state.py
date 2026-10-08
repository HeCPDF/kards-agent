#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.state —— 兼容壳：状态的真实现已端口进 `engine.state`（P2，2026-10-03）。

`U` / `H` / `Sim` / `State` 与常量（`GROUND` / `NO_RETALIATION` / `UNIT_TYPES` / `EVENT_FX_KINDS` /
`ABILITIES_KEYWORDS` / `DRAW_CAP` / 每排容量）的真实现都在 `engine/state.py`；本模块只 re-export，
旧 import（`from sim.state import Sim, U, H` …）保持不变。出处注释随实现搬去了 engine
（`tests/test_sim_source_tags.py` 的棘轮：sim 侧基线随搬迁下调、engine 侧新增计数）。
"""
from engine.state import (ABILITIES_KEYWORDS, DRAW_CAP, EVENT_FX_KINDS, FRONTLINE_CAP,  # noqa: F401
                          FRONTLINE_CAP_LIMITED, GROUND, HAND_CAP, NO_RETALIATION,
                          SUPPORT_UNITS_CAP, UNIT_TYPES, H, Sim, State, U)
from kardsmem.gamemodel import ESide, other_side                                        # noqa: F401

