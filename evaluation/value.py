#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluation.value —— L3 价值函数（纯函数，**不含游戏规则**）。

2026-10-03 起这里是**真实现**（以前转调 `policy.boardeval`）：权重表 `W`、单位/总部/手牌/资源价值、`evaluate`、`delta`。
只依赖 `sim.state` 的数据结构（`test_arch_rules.py` 钉住）。
"""
from __future__ import annotations

from sim.state import (GROUND, NO_RETALIATION, UNIT_TYPES, H, Sim, U)

DEFAULT_WEIGHTS = None            # 见文件末：= W

W = {
    # ---- 单位 ----
    # 身体价值只由攻防+关键词决定（不含费用）：费用是"代价"，在搜索里当预算/在排序里当除数。
    "w_atk": 0.6, "w_def": 0.45, "w_armor": 0.5,
    "kw_guard": 0.5, "kw_smoke": 0.4, "kw_ambush": 0.4, "kw_shock": 0.3, "kw_other": 0.2,
    # 行动价值：能行动的单位每点攻击力的价值（压制/部署病/已行动 → 0）
    "act_w": 0.25,
    "fury_mult": 1.6,       # 奋战（一回合两击）
    "ranged_mult": 1.5,     # 炮兵/轰炸机/战斗机（不吃反击、能打远处）
    "sick_frac": 0.5,       # 部署病：本回合不能动，但下回合能动 ⇒ 行动价值打折而不是 0
    # ---- 总部 ----
    "hq_w": 0.6, "hq_low_at": 8, "hq_low_w": 0.25,
    "thr_w": 0.25,          # 每 1 点「能打到总部」的攻击力
    "lethal": 1000.0,
    # ---- 手牌 / 资源 ----
    "hold_phi": 0.6,        # 手牌持有价值 = phi × 打出后的价值（留着=保留选择权）
    "hold_min": 0.4,        # 任何一张牌本身的最低持有价值（0 费牌不该立刻乱打）
    "draw_v": 1.2,          # 抽到一张未知牌的价值
    "tax_w": 0.5,           # 指向税（KreditsTax_AsEnemyTarget）每 1 点的价值（对手指向它的成本）
    "lam_next": 0.9,        # 下回合 1 点指挥点的影子价格
    "slot_w": 2.0,          # 1 个指挥点槽（每回合多 1 点，剩余若干回合）
    "kred_w": 0.0,          # 剩余指挥点计入场面（方案 B）；搜索终局默认 0（用不完作废）
    "hand_cap": 9,
    # ★ 延迟/常驻效果（2026-10-07，`engine/deferred.py`）：事件由**对方**发起的钩子（ECHELON："你的某个单位被攻击时其余单位 +1+1"）在我方回合的
    #   搜索里不会自己发生。`armed_enemy_hits` = **预期被打次数**（敌方回合里打到我方单位的攻击数）。缺省 0 = **不估值**（不编造次数；
    #   rule 会为这类牌记缺口 `#deferred_val`）。这是**用户拍板项**：要让 bot 为 ECHELON 付费，就在这里给一个有依据的数。
    "armed_enemy_hits": 0.0,
}
KW_SCORE = {"guard": "kw_guard", "smokescreen": "kw_smoke", "ambush": "kw_ambush",
            "shock": "kw_shock", "blitz": "kw_other", "fury": "kw_other", "pincer": "kw_other"}



# ---------------------------------------------------------------------------
# 价值
# ---------------------------------------------------------------------------
def unit_value(u: U, w: dict = W) -> float:
    """身体价值：攻防 + 重甲 + 被动关键词（不含费用；**临时攻击力另算**）。

    ★ 2026-10-02（用户报"热浪留到结束回合前最后打出"）：`AddAttackUntilEndOfTurn` 加的攻击力
    **到回合结束就没了** ⇒ 只有"这个单位本回合还能打"时才算数；已经打完/被压制/部署病时它
    一分不值（§8-4：贴膜无后续攻击 ⇒ 记 0）。持久攻击力（`atk - atk_turn`）照旧计入。
    """
    temp = max(getattr(u, "atk_turn", 0), 0)
    body_atk = max(u.atk - temp, 0)
    usable_temp = temp if u.can_act() else 0          # 还能打才值钱（打完/压制/病 → 0）
    v = (w["w_atk"] * (body_atk + usable_temp) + w["w_def"] * max(u.dfn, 0)
         + w["w_armor"] * u.armor)
    # 指向税（AddKreditsTax）：这张牌被**敌方**指向时要多付的费 —— 加在己方单位上对我们有利，
    # 加在敌方单位上对我们不利（evaluate 会按 side 给符号）。
    v += w.get("tax_w", 0.0) * max(getattr(u, "tax", 0), 0)
    for k in u.kw:
        key = KW_SCORE.get(k)
        if key:
            v += w[key]
    return v


def activity_value(u: U, w: dict = W) -> float:
    """行动价值：这个单位能对敌人造成多少威胁。压制 ⇒ 0；部署病 ⇒ 打折（下回合能动）。

    ★ 2026-10-02：临时攻击力（`atk_turn`，回合结束消失）**只在"本回合还能打"时**计入；
    已经打完/部署病/压制时它到不了敌人身上 ⇒ 用持久攻击力 `atk - atk_turn`。
    """
    temp = max(getattr(u, "atk_turn", 0), 0)
    atk = float(u.atk if u.can_act() else max(u.atk - temp, 0))
    if atk <= 0 or u.pinned:
        return 0.0
    base = w["act_w"] * atk
    if "fury" in u.kw:
        base *= w["fury_mult"]
    if u.typ in NO_RETALIATION or u.typ == "fighter":
        base *= w["ranged_mult"]
    if u.sick and "blitz" not in u.kw:
        base *= w["sick_frac"]
    elif u.attacks_left <= 0 and not u.sick:
        base *= w["sick_frac"]                       # 本回合已用完，下回合还能动
    return base


def _threat(u: U) -> float:
    """这个单位**下回合**能对敌方总部造成的攻击力（粗略）：远程随时；步兵/坦克要站上前线。

    ★ 下回合 ⇒ 临时攻击力（`atk_turn`）已经消失，只用持久攻击力。
    """
    atk = max(u.atk - max(getattr(u, "atk_turn", 0), 0), 0)
    if atk <= 0 or u.pinned:
        return 0.0
    if u.typ in GROUND:
        return float(atk) if u.row == "frontline" else 0.0
    return 0.5 * float(atk)


def _hq_val(h: float, w: dict) -> float:
    v = w["hq_w"] * h
    if h < w["hq_low_at"]:
        v -= w["hq_low_w"] * (w["hq_low_at"] - max(h, 0)) ** 2 / w["hq_low_at"]
    return v


def hold_value(c: H, w: dict = W) -> float:
    """手牌里这张牌的持有价值（留着的选择权）；打出去它就没了，所以打牌收益要扣掉它。"""
    if c.is_unit():
        v = w["hold_phi"] * (w["w_atk"] * max(c.atk, 0) + w["w_def"] * max(c.dfn, 0))
    else:
        e = c.eff
        if "_hold" in e:
            return e["_hold"]
        v = w["hold_phi"] * (0.6 * c.cost)
        v = max(v, w["hold_phi"] * (e.get("kredit", 0) * w["lam_next"]
                                     + e.get("draw", 0) * w["draw_v"]
                                     + e.get("damage", 0) * 0.5))
    return max(v, w["hold_min"])


def pending_value(sim: Sim, w: dict = W) -> float:
    """待生效的指挥点变化 × 下回合影子价格（只算下一个我方回合）。"""
    v = sum(dk for dt, dk in sim.pending if dt == 1) * w["lam_next"]
    # 跨回合待进手牌的牌（预报选中的牌，下回合开始才 spawn）：价值 = 策略侧预估的提示（与旧 `_est` 同一个数）
    v += sum(float(hint or 0.0) for _name, hint in getattr(sim, "pending_cards", ()))
    return v


def armed_value(sim: Sim, w: dict = W) -> float:
    """已布防的**对方事件**钩子（`Sim.armed`，`engine.deferred`）的期望价值：`armed_enemy_hits` × 各候选被打主体上预计算桶的平均身体增益。

    只估 `buff_ids`（攻/防）这一种后果——其它后果形状（抽牌/伤害…）没有统一尺度 ⇒ 不估、不编（rule 的缺口里写着）。
    桶来自 VM 空跑（`event_fx["armed"]`），按**我方**每个在场单位当"被打的牌"各取一份再平均（敌方选谁打是未知的，均匀是最少假设）。
    """
    n = float(w.get("armed_enemy_hits", 0.0) or 0.0)
    if n <= 0.0 or not getattr(sim, "armed", None):
        return 0.0
    efx = (getattr(sim, "event_fx", None) or {}).get("armed") or {}
    subs = [uid for uid, u in sim.units.items() if u.side == sim.me]
    tot = 0.0
    for a in sim.armed:
        if a.side != sim.me or not a.enemy_event or not subs:
            continue
        vals = []
        for sid in subs:
            b = efx.get((a.src, a.hook, sid)) or {}
            vals.append(sum(w["w_atk"] * da + w["w_def"] * dd
                            for cid, (da, dd) in (b.get("buff_ids") or {}).items()
                            if cid in sim.units and sim.units[cid].side == sim.me and cid != sid))
        tot += sum(vals) / len(vals)
    return n * tot


def evaluate(sim: Sim, w: dict = W) -> float:
    if sim.hq.get(sim.opp, 1) <= 0:
        return w["lethal"]
    if sim.hq.get(sim.me, 1) <= 0:
        return -w["lethal"]
    e = 0.0
    for u in sim.units.values():
        s = 1.0 if u.side == sim.me else -1.0
        e += s * (unit_value(u, w) + activity_value(u, w) + w["thr_w"] * _threat(u))
    e += _hq_val(sim.hq.get(sim.me, 0), w) - _hq_val(sim.hq.get(sim.opp, 0), w)
    e += w.get("kred_w", 0.0) * sim.kredits
    e += sum(hold_value(c, w) for c in sim.hand.values())
    e += pending_value(sim, w)
    e += armed_value(sim, w)
    e += w["slot_w"] * sim.slots
    e -= w.get("kred_w", 0.0) * sim.opp_kredits + w["slot_w"] * sim.opp_slots     # 对手的指挥点/槽变化
    e -= w["hold_phi"] * w["draw_v"] * sim.opp_cards          # §15.5：对手多抽的牌（对我们不利）
    e += sim.bonus
    return e


def value(state, weights=None) -> float:
    """`V(s)`：只做价值判断，不含规则（规则在 L2）。"""
    return evaluate(state, weights if weights is not None else W)


def delta(start, end, weights=None, ctx=None) -> float:
    """`ΔV = V(终态) − V(起点)`。

    ★ `ctx`（同回合计价，§8-4/TODO A1⑤）**还没接线**：传了就明确拒绝，
      不静默忽略 —— 静默忽略会给出"看起来对"的错误价值（§3.5.6 失败可见）。
    """
    if ctx is not None:
        raise NotImplementedError(
            "evaluation.context 尚未接线（同回合计价 = TODO A1⑤）；"
            "传 ctx 语义会变，先拒绝而不是静默忽略")
    w = weights if weights is not None else W
    return evaluate(end, w) - evaluate(start, w)


DEFAULT_WEIGHTS = W
