#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim —— L2 模拟器（规则引擎）。

分层与依赖方向见 `EVAL-ARCHITECTURE.md` §3.1：
    L2 模拟（本包）只能往下依赖 L1（原语/钩子/随机流）与 L0（kardsmem/board_api）；
    **不得** import `evaluation` / `policy` / `player.rule`（`test_arch_rules.py` 钉住）。

现状（2026-10-02，§1.1 补充 0「包边界现在就建，不搬家」）：
  * `State` 暂由 `policy.boardeval.Sim` 承载（行为一字不改），名字与边界先立住；
  * `simulate()` 目前只做**单步转移**并如实报 `complete=False` + 缺口，
    事件队列/分发器（连锁到稳态，本轮的必做项）还没接上 —— 见 `sim/chain.py` 顶部。
"""
from .chain import GAP_NO_CHAIN, Outcome, simulate
from .state import H, State, U

__all__ = ["State", "U", "H", "Outcome", "simulate", "GAP_NO_CHAIN"]
