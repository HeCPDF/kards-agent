# -*- coding: utf-8 -*-
"""policy.forced —— 系统引发的强制决策（总纲 §3/§6）：起点不是我们的动作，弹出时当场枚举 + 评估取最好。

例：PBY CATALINA 回合开始"抽 2 放 1 回牌库顶"（`OnStartOfTurn` 钩子拉起手牌目标提示）。
入口与"我们的动作引发的提示"是**同一个**：`sim.engine.hand_target_suspended`——只是这里没有触发动作，直接对挂起态逐候选 `resume` 再评估。
"""
from __future__ import annotations

from evaluation.value import W, evaluate
from sim.engine import hand_target_suspended


def answer_hand_target(sim, instigator_id, w: dict = W):
    """→ `(最佳候选手牌 id, [(分数, 候选 id)…])`；没有候选表/候选 ⇒ None。分数 = 选了它之后终态的评估值。"""
    sus = hand_target_suspended(sim, instigator_id)
    if sus is None:
        return None
    scored = sorted(((evaluate(sus.resume(o.key).state, w), o.key) for o in sus.prompt.options), key=lambda x: -x[0])
    return scored[0][1], scored
