# -*- coding: utf-8 -*-
"""agent.promptinfo —— 把 `AgentSession.pending()` 的原始字典归一成 `PromptInfo`（总纲 §3/§7，OPS 文档 §4.5）。

OPS 只**如实报告**游戏现在挂着什么提示（只读），"答什么"由策略层（`policy.answer`）决定。
`PromptInfo.kind` 与 `sim.prompt.Prompt.kind` 同一套词：choose_one | select_card_to_draw | hand_target | board_target | arrow。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class PromptInfo:
    kind: str
    options: List[dict] = field(default_factory=list)       # choose_one / select_card_to_draw 的候选行；其它为空
    source: Any = None                                      # 发起牌 id（trigger_id / card_being_played / instigator）
    layer: int = 1
    raw: dict = field(default_factory=dict)                 # 原样保留，供日志


def from_pending(p: dict) -> Optional[PromptInfo]:
    """`pending()` 字典 → 当前**最该先回答**的那个提示；没有提示 ⇒ None。优先级：抉择/选牌 > 手牌目标 > 板卡待点目标 > 箭头。"""
    if not p:
        return None
    rows = p.get("choose_one") or []
    if rows:
        kinds = {r.get("kind") for r in rows}
        kind = "select_card_to_draw" if kinds & {"select_card_to_draw", "forecast", "choose_card"} else "choose_one"
        src = next((r.get("trigger_id") for r in rows if r.get("trigger_id") is not None), None)
        return PromptInfo(kind, list(rows), src, raw={"choose_one": rows})
    pp = p.get("pick_pending") or {}
    if pp.get("pending"):
        return PromptInfo("select_card_to_draw", [], pp.get("trigger_id") or pp.get("card_being_played"), raw={"pick_pending": pp})
    ht = p.get("hand_target") or {}
    if ht.get("pending"):
        return PromptInfo("hand_target", [], ht.get("card_being_played"), raw={"hand_target": ht})
    bt = p.get("board_target")
    if isinstance(bt, int) and bt:
        return PromptInfo("board_target", [], bt, raw={"board_target": bt})
    if p.get("arrows"):
        return PromptInfo("arrow", [], None, raw={"arrows": p.get("arrows")})
    return None
