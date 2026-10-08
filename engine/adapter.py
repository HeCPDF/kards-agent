#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.adapter —— L0→L2 适配：游戏快照（`board_api.Card` 列表）→ `Sim`。

2026-10-03 从 `policy/boardeval.py` 物理迁入 `sim/`，同日晚 P2 第十刀端口进 `engine/`
（`sim/adapter.py` 只剩 re-export 壳）。`kw_of` / `actionable` / `hand_eff` 等由调用方（`player.rule`）注入。

★ 这里读的是**快照牌**（`kardsmem.board` 的 `Card`，带 `.obj` = 原版 `BaseCardObject`）的**原版字段**
（`o.CardID` / `o.Location` / `o.getTotal*()` …）；旧的派生别名（`.card_id` / `.side` 字符串）一律不碰
（REFACTOR-PLAN P1 的 S3'）。
"""
from __future__ import annotations

from typing import Optional

from kardsmem.gamemodel import ECardLocation, EType, ESide, other_side
from engine.state import UNIT_TYPES, H, Sim, U



# ---------------------------------------------------------------------------
# 从快照建 Sim
# ---------------------------------------------------------------------------
def from_cards(cards, kw_of, actionable=None, kredits: float = 0.0, hand_eff=None,
               slots: float = 0.0, pending=None, front_owner=None, turn=None,
               legal=None, pair_eff=None, after_hq=None, deck_left: int = -1,
               fatigue: int = 0, deck=None, deck_cards=None, attack_fx=None,
               rng_seed: Optional[int] = None, death_fx=None,
               kredit_max: float = 24.0, event_fx=None, forecast=None, hand_target_fx=None, spawn_pick=None, spawn_stats=None, front_limited=False, my_side=None,
               bond_of=None, bond_factions=None) -> Sim:
    """`cards` 是带 `.obj`（`BaseCardObject`）的快照牌；`my_side`（`ESide`）决定谁是本地。
    `kw_of(card)` → 关键词名集合；`actionable(card)` → 我方单位此刻能否行动；
    `hand_eff(card)` → 效果摘要 dict（可空）。

    协力（`engine/natives/bond.py`）：`bond_of(card)` → 这张牌带不带 `ability.bond` 标签（读侧由调用方给，离线默认 False）；
    `bond_factions` = 原版 `activeBondFactions`（`None` = 读不出 ⇒ 打出协力牌时不下结论）。
    `bond_removed` 读快照 `raw["received_abilities"]` 里有没有 `"bond_removed"`（读不到 ⇒ False）。"""
    if my_side is None:
        raise ValueError("from_cards 需要 my_side（本地玩家是 left 还是 right）；读不出就别建模拟")
    opp = other_side(my_side)
    units, hq, hand = {}, {my_side: 20, opp: 20}, {}
    hq_guarded = False
    hq_seen = False
    hq_armor, hq_immune = {}, {}
    # 卡 id → 牌模板：收缴（SalvageMultipleUnits）要用被收缴目标的模板造 1/1 复制。
    # 场上牌的 `eff` 不在这里跑 VM（贵）：保留名字/费用/类型/攻防/关键词，效果留空。
    templates: dict = {}
    for c in cards:
        o = c.obj
        if o is None or o.side not in (ESide.left, ESide.right):
            continue
        side = o.side
        loc = o.Location
        if loc in (ECardLocation.Board_HQLeft, ECardLocation.Board_HQRight) and o.Type == EType.location:
            hq[side] = int(o.defense or 0)
            # ★ 2026-10-07：BP `CalculateDamageDealt` 对**任何**受击牌（含 HQ 地点牌）都先判 `getHasImmune`（免疫 ⇒ 伤害 0）、
            #   再扣 `getTotalHeavyArmor`（[0,3]）。以前 HQ 只读防御 ⇒ 实机 00:04 局三次打总部都"没打够"。
            try:
                hq_armor[side] = max(0, min(3, int(o.heavyArmor or 0) + int(o.heavyArmorBuff or 0)))
            except Exception:                                     # noqa: BLE001
                hq_armor[side] = 0
            hq_immune[side] = bool(getattr(o, "isImmune", False))
            if side == opp:
                hq_guarded = bool(o.isBeingGuarded)
                hq_seen = True
            continue
        atk = o.getTotalAttack()
        opc = o.getTotalOperationCost()
        cost = int(o.getTotalKredits() or 0)
        typ = o.Type.name if o.Type is not None else None
        in_hand = loc in (ECardLocation.Hand_Left, ECardLocation.Hand_Right)
        if in_hand and side == my_side:
            eff = None
            try:
                eff = hand_eff(c) if hand_eff else None
            except Exception:                                     # noqa: BLE001
                eff = None
            _bond = False
            try:
                _bond = bool(bond_of(c)) if bond_of else False
            except Exception:                                     # noqa: BLE001
                _bond = False
            _ra = (getattr(c, "raw", None) or {}).get("received_abilities") or ()
            _removed = any((a.get("ability") if isinstance(a, dict) else a) == "bond_removed" for a in _ra)
            h = H(o.CardID, o.title or "?", cost, typ if typ in UNIT_TYPES else "order",
                  int(atk or 0), int(o.defense or 0), kw_of(c), eff,
                  cost_buff=int(o.kreditsBuff or 0),
                  faction=(None if o.faction is None else int(o.faction)), bond=_bond, bond_removed=_removed)
            hand[o.CardID] = h
            templates[o.CardID] = h
            continue
        if loc == ECardLocation.Board_Frontline:
            where = "frontline"
        elif loc in (ECardLocation.Board_HQLeft, ECardLocation.Board_HQRight):
            where = "back"
        else:
            continue
        acted = sick = False
        if side == my_side and actionable is not None:
            try:
                ok = actionable(c)
            except Exception:                                     # noqa: BLE001
                ok = True
            if not ok:
                if o.enterPlayOnTurn is not None and turn is not None and o.enterPlayOnTurn == turn:
                    sick = True                    # 本回合刚部署：下回合能动，贴闪击现在就能动
                else:
                    acted = True                   # 已行动过/被压制等：本回合不能再动
        # ★ 2026-10-07 实机：`CanCardDoAnything`（actionable）只说"还能做点什么"（比如还能**移动**），
        #   不等于还有**攻击次数**。已经攻击过（非奋战）/移动过的单位，游戏的 `CanAttack` 回 `no_attack_left` /
        #   `has_already_attacked`，而 sim 凭默认 `attacks_left=1`（奋战 2）以为还能打 ⇒ 搜索把"打总部致命"这类最优手
        #   放在已无攻击的单位头上，闸门一次次拒（每次问 ~4 s），真正能打的单位反而排不上。快照里就有 `attackLeft@0x26C`
        #   （含奋战、不含闪击），读到就直接用。
        _al = getattr(o, "attackLeft", None)
        units[o.CardID] = U(o.CardID, side, where, int(atk or 0), int(o.defense or 0),
                            cost, typ, kw_of(c), acted,
                            1 if opc is None else opc, sick=sick,
                            attacks_left=(int(_al) if isinstance(_al, int) and side == my_side else None),
                            # `IsPinned = pinnedTurns > 0`（kardsmem/board.py 注释：IDA 0x144AFCD30）；`isSuppressed` 是另一个标志，
                            # 光看它会漏掉被"定住"的单位 ⇒ 实机 move_up 被闸门拒（2026-10-08 一局 3 次，"32 被压制（pin）"）
                            pinned=bool(o.isSuppressed) or (int((getattr(c, "raw", None) or {}).get("pinned_turns") or 0) > 0),
                            guarded=bool(o.isBeingGuarded),
                            aa_hq=(after_hq or {}).get(o.CardID),
                            mdef=int(o.maxDefense or o.defense or 0),
                            atk_buff=int(o.attackBuff or 0),
                            # ★ P3 ③（2026-10-04）：运行时自定义能力（`receivedAbilitiesFromCards`）。
                            #   快照 raw 里带 `received_abilities`（`board._read_card` 对**场上**的牌读），
                            #   读不到 ⇒ 空（`excess` 这类能力就按没有算 —— 不猜）。
                            ab=tuple((getattr(c, "raw", None) or {}).get("received_abilities") or ()),
                            # ★ P3 ⑥：`range @0x78`（原版 `CanAttack` 的 `not_enough_range` 判据）。
                            #   快照对象上就有（`BaseCardObject.range`），raw 兜底，读不到 ⇒ None。
                            rng=(o.range if getattr(o, "range", None) is not None
                                 else (getattr(c, "raw", None) or {}).get("range")),
                            # ★ P3 ⑦：`hasBeenAttackedThisTurn`（快照 @0x280）—— 伏击的必要条件之一
                            #   （`CalculateDamageDealt` :14881-14887）。
                            been_attacked=bool(getattr(o, "hasBeenAttackedThisTurn", False)),
                            # ★ P3 ⑦：`isImmune @0x289`（伏击的必要条件之一）。
                            immune=bool(getattr(o, "isImmune", False)),
                            # ★ P4 第三刀：两字段模型 —— 总量之外的**独立 buff 字段**
                            #   （原版 `operationCostBuff`/`heavyArmorBuff`；直跑按原版写基础值要用）。
                            opc_buff=int(o.operationCostBuff or 0),
                            armor_buff=int(o.heavyArmorBuff or 0),
                            faction=(None if o.faction is None else int(o.faction)))
        templates[o.CardID] = H(o.CardID, o.title or "?", cost,
                                typ if typ in UNIT_TYPES else "order",
                                int(atk or 0), int(o.defense or 0), kw_of(c), {})
    sim = Sim(units, hq, kredits, hand, slots, pending, hq_guarded, front_owner, legal, pair_eff,
              deck_left=deck_left, fatigue=fatigue, deck=deck, deck_cards=deck_cards,
              attack_fx=attack_fx, rng_seed=rng_seed, card_templates=templates,
              death_fx=death_fx, kredit_max=kredit_max, event_fx=event_fx, forecast=forecast, hand_target_fx=hand_target_fx, spawn_pick=spawn_pick,
              spawn_stats=spawn_stats, front_limited=front_limited, my_side=my_side)
    for cid, h in (deck_cards or {}).items():          # 牌库模板也进表（收缴/移动等按 id 取）
        sim.card_templates.setdefault(cid, h)
    sim.hq_known = hq_seen
    sim.hq_armor, sim.hq_immune = hq_armor, hq_immune
    sim.bond_factions = None if bond_factions is None else set(bond_factions)
    return sim
