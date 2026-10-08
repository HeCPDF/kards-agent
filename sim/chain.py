#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.chain —— 兼容壳：抽牌链/autoplay 冲刷已端口进 `engine.chain`（P2，2026-10-03）。

还留在本模块的只有"骨架期"的 `Outcome` / `simulate()`（单步转移；等 P4/P5 换成 engine 真链）。
旧 import（`from sim.chain import draw_chain` / `C.DRAW_CAP`）保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.dispatch import Dispatcher, Event, Result            # noqa: F401  （旧 import 兼容）
from engine.state import State                                    # noqa: F401

# 端口进 engine 的链：旧名字从本模块 re-export。
from engine.chain import DRAW_CAP, draw_chain, flush_autoplay     # noqa: E402,F401
from engine.natives.damage import apply_fatigue                   # noqa: E402,F401

GAP_NO_CHAIN = "sim.chain：事件队列/分发器未接（骨架期只做单步转移，未推到稳态）"


@dataclass
class Outcome:
    """模拟结果。字段名按 §3.3，`trace` 供调试/回放/留痕（现在一定是空的）。"""

    state_end: State
    trace: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    complete: bool = False
    rng_used: int = 0
    branches: list = field(default_factory=list)


def simulate(state: State, actions, ctx=None) -> Outcome:
    """把 `actions`（一个 `A` 或一串 `A`）依次推演到稳态。

    骨架期：逐个 `sim.engine.apply`（单步），并把"没接连锁"如实记成缺口。
    """
    from sim import engine as _B                           # 规则引擎（2026-10-03 起 apply 在 sim.engine）

    seq = list(actions) if isinstance(actions, (list, tuple)) else [actions]
    end = state
    trace = []
    for a in seq:
        end = _B.apply(end, a)
        trace.append({"action": repr(a), "kind": getattr(a, "kind", None),
                      "src": getattr(a, "src", None), "dst": getattr(a, "dst", None)})
    gaps = list(getattr(end, "gaps", None) or []) + [GAP_NO_CHAIN]
    return Outcome(state_end=end, trace=trace, gaps=gaps, complete=False,
                   rng_used=0, branches=[])

