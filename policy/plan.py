# -*- coding: utf-8 -*-
"""policy.plan —— 行动计划（总纲 §1/§6）：一个决策 = 触发 + 提示答案路径。

搜索选中某个决策后，把它的**路径**存进 `PlanStore`（键 = 发起牌 id）；执行侧遇到提示时先问 `policy.answer`，
有计划就照点，计划落空（候选里没有这一项/随机偏离）才重算。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionPlan:
    trigger_kind: str                 # play_unit | play_event | attack | move_up …
    card: int
    target: object = None
    path: tuple = ()                  # 对各层提示的答案
    meta: dict = field(default_factory=dict)


class PlanStore(dict):
    """`{发起牌 id: 路径元组}`（dict 子类：兼容旧代码里直接 `plan.get(card)` 的写法）。"""

    def put(self, card, path) -> None:
        if path:
            self[card] = tuple(path)

    def consume(self, path) -> None:
        """两层都走完（或不再需要）⇒ 删掉所有等于这条路径的计划。"""
        for k in [k for k, v in self.items() if tuple(v) == tuple(path)]:
            self.pop(k, None)
