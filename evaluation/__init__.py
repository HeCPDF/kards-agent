#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluation —— L3 评估（价值），纯函数，**不含任何游戏规则**。

分层与依赖方向见 `EVAL-ARCHITECTURE.md` §3.1/§3.4：
    L3 只依赖 `sim.state` 的数据结构；**不得** import L2 的规则实现、
    也不得自己夹取/模拟触发。`test_arch_rules.py` 钉住方向。

现状（§1.1 补充 0）：`value()` / `delta()` 转调 `policy.boardeval` 的
`evaluate()`/`delta()`（行为一字不改），权重仍是 `boardeval.W`；
同回合计价等上下文（§8-4）在 `evaluation/context.py` 里先占位，**尚未接线**
（传 `ctx` 会明确报错，不静默忽略）。
"""
from .context import Context
from .value import DEFAULT_WEIGHTS, delta, value

__all__ = ["value", "delta", "Context", "DEFAULT_WEIGHTS"]
