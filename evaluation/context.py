#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluation.context —— 评估上下文（同回合计价等），**尚未接线**。

字段按 `EVAL-ARCHITECTURE.md` §3.4 与汇总报告 §8 第 4 项（TODO A1⑤）：
  * `same_turn`：只在同一回合内比较（贴膜这一步本身不产生价值）；
  * `repeat_zero`：重复牌（效果已存在于场上同一状态）Δ≈0 记 0；
  * `buff_no_followup_zero`：贴在本回合不能攻击的单位、又没有后续攻击 ⇒ 记 0；
  * `gap_value`：取不到效果（缺口）的价值 —— 弯路 #38：记 0，不按费用估。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Context:
    """评估侧上下文；**未接线** —— 现在传进 `evaluation.delta()` 会 `NotImplementedError`。"""

    same_turn: bool = True
    repeat_zero: bool = True
    buff_no_followup_zero: bool = True
    gap_value: float = 0.0
