#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.adapter —— 兼容壳：L0→L2 适配（`from_cards`）的真实现已端口进 `engine.adapter`（P2，2026-10-03）。

快照牌 → `Sim` 的转换是 engine 的事（它产出 `engine.state.Sim`），本模块只 re-export，旧 import
（`policy/boardeval.py` 的 `import sim.adapter as _adapter`、`BE.from_cards(...)`）保持不变。
出处与细节见 `engine/adapter.py`；`tests/test_arch_rules.py` 钉住
`sim.adapter.from_cards is engine.adapter.from_cards`，免得壳里悄悄留一份旧实现。
"""
from engine.adapter import from_cards                                                   # noqa: F401
