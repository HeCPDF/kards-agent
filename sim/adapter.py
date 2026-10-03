#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.adapter —— L0→L2 适配：游戏快照（`board_api.Card` 列表）→ `Sim`。

2026-10-03 从 `policy/boardeval.py` 物理迁入。`kw_of` / `actionable` / `hand_eff` 等由调用方（`player.rule`）注入。
"""
from __future__ import annotations

from typing import Optional

from sim.state import ENEMY, LOCAL, UNIT_TYPES, H, Sim, U



# ---------------------------------------------------------------------------
# 从快照建 Sim
# ---------------------------------------------------------------------------
def from_cards(cards, kw_of, actionable=None, kredits: float = 0.0, hand_eff=None,
               slots: float = 0.0, pending=None, front_owner=None, turn=None,
               legal=None, pair_eff=None, after_hq=None, deck_left: int = -1,
               fatigue: int = 0, deck=None, deck_cards=None, attack_fx=None,
               rng_seed: Optional[int] = None, death_fx=None,
               kredit_max: float = 24.0, event_fx=None, forecast=None, hand_target_fx=None, spawn_pick=None, spawn_stats=None, front_limited=False) -> Sim:
    """`kw_of(card)` → 关键词名集合；`actionable(card)` → 我方单位此刻能否行动；
    `hand_eff(card)` → 效果摘要 dict（可空）。"""
    units, hq, hand = {}, {LOCAL: 20, ENEMY: 20}, {}
    hq_guarded = False
    hq_seen = False
    # 卡 id → 牌模板：收缴（SalvageMultipleUnits）要用被收缴目标的模板造 1/1 复制。
    # 场上牌的 `eff` 不在这里跑 VM（贵）：保留名字/费用/类型/攻防/关键词，效果留空。
    templates: dict = {}
    for c in cards:
        side = c.side
        if side not in (LOCAL, ENEMY):
            continue
        if c.location == "hq":
            hq[side] = int(c.defense or 0)
            if side == ENEMY:
                hq_guarded = bool(getattr(c, "is_being_guarded", False))
                hq_seen = True
            continue
        atk = getattr(c, "total_attack", None)
        atk = atk() if callable(atk) else c.attack
        opc = getattr(c, "total_operation_cost", None)
        opc = opc() if callable(opc) else getattr(c, "operation_cost", None)
        if c.location == "hand" and side == LOCAL:
            eff = None
            try:
                eff = hand_eff(c) if hand_eff else None
            except Exception:                                     # noqa: BLE001
                eff = None
            h = H(c.card_id, getattr(c, "name", "?"), int(c.kredit_cost or 0),
                  c.card_type if c.card_type in UNIT_TYPES else "order",
                  int(atk or 0), int(c.defense or 0), kw_of(c), eff)
            hand[c.card_id] = h
            templates[c.card_id] = h
            continue
        if c.location not in ("frontline", "back"):
            continue
        acted = sick = False
        if side == LOCAL and actionable is not None:
            try:
                ok = actionable(c)
            except Exception:                                     # noqa: BLE001
                ok = True
            if not ok:
                ent = getattr(c, "enter_play_on_turn", None)
                if ent is not None and turn is not None and ent == turn:
                    sick = True                    # 本回合刚部署：下回合能动，贴闪击现在就能动
                else:
                    acted = True                   # 已行动过/被压制等：本回合不能再动
        units[c.card_id] = U(c.card_id, side, c.location, int(atk or 0), int(c.defense or 0),
                             int(c.kredit_cost or 0), c.card_type, kw_of(c), acted,
                             1 if opc is None else opc, sick=sick,
                             pinned=bool(getattr(c, "is_suppressed", False)),
                             guarded=bool(getattr(c, "is_being_guarded", False)),
                             aa_hq=(after_hq or {}).get(c.card_id),
                             mdef=int(getattr(c, "max_defense", None)
                                      or c.defense or 0))
        templates[c.card_id] = H(c.card_id, getattr(c, "name", "?"), int(c.kredit_cost or 0),
                                 c.card_type if c.card_type in UNIT_TYPES else "order",
                                 int(atk or 0), int(c.defense or 0), kw_of(c), {})
    sim = Sim(units, hq, kredits, hand, slots, pending, hq_guarded, front_owner, legal, pair_eff,
              deck_left=deck_left, fatigue=fatigue, deck=deck, deck_cards=deck_cards,
              attack_fx=attack_fx, rng_seed=rng_seed, card_templates=templates,
              death_fx=death_fx, kredit_max=kredit_max, event_fx=event_fx, forecast=forecast, hand_target_fx=hand_target_fx, spawn_pick=spawn_pick,
              spawn_stats=spawn_stats, front_limited=front_limited)
    for cid, h in (deck_cards or {}).items():          # 牌库模板也进表（收缴/移动等按 id 取）
        sim.card_templates.setdefault(cid, h)
    sim.hq_known = hq_seen
    return sim
