# -*- coding: utf-8 -*-
"""policy.answer —— 对游戏挂起的**提示**给答案（总纲 §6）：先照计划，计划落空才交给调用方重算。

这里只放"照计划"那一半（纯函数，可离线测）；"重算"（VM 分支 / 把候选当临时手牌评估 / 强制决策枚举）仍在 `player/rule.py` /
`policy/forced.py`——它们和这里是同一个入口的两半，`rule.choose_*` 只是按提示类别分发。
每个函数返回 `(命中的行 | 候选 id | None)`，None = 没计划或计划落空。
"""
from __future__ import annotations

from typing import Callable, Optional


def plan_choose_one(rows: list, plan: dict) -> Optional[dict]:
    """抉择/二选一：路径 `(下标,)`，命中 `row["index"]` 相等的那一行。"""
    for r in rows:
        pp = plan.get(r.get("trigger_id"))
        if pp and isinstance(pp[0], int) and r.get("index") is not None and int(r["index"]) == int(pp[0]):
            return r
    return None


def plan_forecast(rows: list, plan: dict, fc_types: dict, norm: Callable, name_of: Callable) -> Optional[tuple]:
    """预报两层：路径 `(天气, 变体)`。第一层候选名都在 `fc_types`（三张天气类型卡）⇒ 点那种天气；
    否则（第二层）按 `name_of(天气, 变体)`（当前种子预测的内部名）找候选。→ `(行, 路径)` 或 None。"""
    if not rows or not plan:
        return None
    pp = plan.get(rows[0].get("trigger_id")) or (next(iter(plan.values())) if len(plan) == 1 else None)
    if not (pp and len(pp) == 2 and all(isinstance(x, str) for x in pp)):
        return None
    w, t = pp
    names = {norm(r.get("name") or r.get("label")) for r in rows}
    if names <= set(fc_types):
        hit = next((r for r in rows if fc_types.get(norm(r.get("name") or r.get("label"))) == w), None)
    else:
        nm = name_of(w, t)
        hit = next((r for r in rows if nm and norm(r.get("name") or r.get("label")) == norm(nm)), None)
    return (hit, tuple(pp)) if hit is not None else None


def plan_hand_target(legal_ids, plan: dict, instigator) -> Optional[int]:
    """手牌目标：路径 `(手牌 id,)`，必须仍在合法候选里。"""
    pp = plan.get(instigator)
    if pp and pp[0] in set(legal_ids):
        return pp[0]
    return None


def plan_choose_spawn(rows: list, plan: dict, norm: Callable) -> Optional[tuple]:
    """"三选一加入手牌"（selectCardToDraw 一族）：路径 `(候选内部名,)`，按名字命中屏幕上的那一行。→ `(行, 路径)` 或 None。"""
    for r in rows:
        pp = plan.get(r.get("trigger_id"))
        if pp and len(pp) == 1 and isinstance(pp[0], str):
            want = norm(pp[0])
            hit = next((x for x in rows if norm(x.get("name") or x.get("label")) == want), None)
            if hit is not None:
                return hit, tuple(pp)
    return None
