#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""policy.search —— L4 动作生成 + 选择路径展开 + 回合内束搜索。

2026-10-03 起从 `policy/boardeval.py` 物理迁入。L4 是唯一同时认识 L2（`sim.engine`）与 L3（`evaluation.value`）的层。
"""
from __future__ import annotations

from typing import Iterable, Optional

from evaluation.value import W, evaluate, unit_value
from sim.engine import (A, _fx_stopped, _restricted, apply, can_hit_hq, can_hit_unit, prompt_of, row_full, run, sim_order)
from sim.state import GROUND, UNIT_TYPES, H, Sim, U


def wire_sim() -> None:
    """把**估值侧**的两个东西接到 `sim` 上（L4 是唯一同时认识 L2/L3 的层 ✓）：

      * `sim.engine.set_estimation_params(draw_v=W["draw_v"])` —— 匿名牌持有价值（**估值参数**，
        不是规则常量；规则常量手牌上限已走 `HAND_CAP` ✓）；
      * `sim.engine.set_unit_valuer(unit_value)` —— 选目标启发式（"打最值钱的敌人"）要一张牌的价值。

    **幂等** ✓。之前这段写在 `policy/boardeval.py` 门面里（门面一被 import 就生效）；现在收成一个
    显式入口 ⇒ 门面只是转调它，将来门面删掉时，新入口只要调 `wire_sim()` 就行（P5 的接缝 ✓）。
    """
    from sim import engine as _en
    _en.set_estimation_params(draw_v=W["draw_v"])
    _en.set_unit_valuer(unit_value)



PATH_CAP = 24          # 每次生成行动时，展开出的"选择路径"最多这么多条（用户 2026-10-03：最多 24 枝 + 剪枝）


def expand_paths(sim: Sim, acts: list, w: dict = W, cap: int = PATH_CAP) -> list:
    """把"打出后会挂起提示"的行动展开成**每条路径一个行动**（路径 = 对各层提示的答案）。总纲 §4/§6：
    模拟对提示挂起（`sim.engine.run`），这里逐候选 `resume` 得到叶路径；叶总数 > `cap` 时按粗评分（走完这一步的评估值）
    剪掉低分的（每张牌至少留它自己最好的一条；没有提示的行动原样保留）。束宽不动。"""
    from sim.prompt import leaves
    out, expanded = [], []                       # expanded: [(A, 粗分 or None)]
    for a in acts:
        if prompt_of(sim, a) is None:
            out.append(a)
            continue
        for path, done in leaves(run(sim, a)):
            na = A(a.kind, a.src, a.dst, a.cost, "%s %s" % (a.label, list(path)), path=path)
            out.append(na)
            expanded.append((na, done.state))
    if len(expanded) > cap:
        scored = sorted(((evaluate(st, w), na) for na, st in expanded), key=lambda x: -x[0])
        keep = {id(na) for _v, na in scored[:cap]}
        first = {}
        for _v, na in scored:                    # 每张牌至少留它自己最好的一条
            first.setdefault(na.src, na)
        keep |= {id(na) for na in first.values()}
        drop = {id(na) for na, _st in expanded} - keep
        out = [a for a in out if id(a) not in drop]
    return out


def gen_actions(sim: Sim, w: dict = W, order_targets_max: int = 4) -> list:
    return expand_paths(sim, _gen_actions(sim, w, order_targets_max), w)


def _gen_actions(sim: Sim, w: dict = W, order_targets_max: int = 4) -> list:
    out = []
    if sim.playing_side == sim.opp:                  # 行动权已交给对方：我方这一回合没有后续动作
        return out
    mine = [u for u in sim.units.values() if u.side == sim.me]
    foes = [u for u in sim.units.values() if u.side == sim.opp]
    ground_restricted = _restricted(sim, 4)        # cannotAttackWithGroundUnits
    for a in mine:
        if not a.can_act() or a.opc > sim.kredits or (a.atk <= 0 and not a.aa_hq):
            continue
        # 地面 = 非空军（游戏用 `IsGroundUnit` 原生判；我们只有 infantry/tank/artillery 三类地面）
        if ground_restricted and a.typ in ("infantry", "tank", "artillery"):
            continue
        for t in foes:
            if a.atk > 0 and can_hit_unit(sim, a, t) and not _fx_stopped(sim, a.id, t.id):
                out.append(A("attack", a.id, t.id, a.opc, "%s>%s" % (a.id, t.id)))
        if sim.hq_known and can_hit_hq(sim, a) and not _fx_stopped(sim, a.id, "hq"):
            out.append(A("attack", a.id, "hq", a.opc, "%s>hq" % a.id))
    if sim.front_owner != sim.opp:
        for u in mine:
            if (u.row == "back" and u.typ in GROUND and u.can_act() and not u.moved
                    and u.opc <= sim.kredits and not row_full(sim, sim.me, "frontline")):
                out.append(A("move", u.id, None, u.opc, "move %s" % u.id))
    for c in sim.hand.values():
        if c.cost > sim.kredits or c.id is None or c.id < 0 and c.name == "?":
            continue
        # 对局限制：不能打指令 / 不能部署单位（type 2/3）——与游戏判据同序（先指令后部署）
        _kind = "deploy" if c.is_unit() else "order"
        if (_kind == "order" and _restricted(sim, 2)) or (_kind == "deploy" and _restricted(sim, 3)):
            continue
        if c.is_unit() and row_full(sim, sim.me, "back"):
            continue                                 # 原版 `CanMoveCardToLocation`：IsLocationFull && 不是指令 ⇒ 部署不了
        lt = sim.legal.get(c.id)
        if lt is not None:
            # 游戏的判断函数说了能指向谁 ⇒ 每个 (牌, 目标) 各自一条走法（目标可以是 None=不指向）
            kind = "deploy" if c.is_unit() else "order"
            for t in lt:
                if t is None or t == "hq" or t in sim.units:
                    out.append(A(kind, c.id, t, c.cost, "%s %s->%s" % (kind, c.name, t)))
            continue
        if c.is_unit():
            if not c.eff.get("_needs_target"):       # 带目标的部署由调用方的目标逻辑处理
                out.append(A("deploy", c.id, None, c.cost, "deploy %s" % c.name))
            continue
        e = c.eff
        side = e.get("target")
        if side is None and not any(k in e for k in ("damage", "destroy", "retreat", "pin",
                                                     "unpin", "give", "buff", "damage_hq")):
            out.append(A("order", c.id, None, c.cost, "order %s" % c.name))
            continue
        cands = []
        if side in ("enemy", "any", None):
            cands += foes
        if side in ("friend", "any", None):
            cands += mine
        if e.get("damage") or e.get("damage_hq") or side in ("enemy", "any"):
            cands_hq = ["hq"]
        else:
            cands_hq = []
        # 只保留价值最高的几个目标，控制分支
        scored = []
        for t in cands:
            after = sim_order(sim, c.id, t.id)
            scored.append((evaluate(after, w), t.id))
        scored.sort(key=lambda x: -x[0])
        for _, tid in scored[:order_targets_max]:
            out.append(A("order", c.id, tid, c.cost, "order %s->%s" % (c.name, tid)))
        for h in cands_hq:
            out.append(A("order", c.id, h, c.cost, "order %s->hq" % c.name))
    return out


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def search(sim: Sim, w: dict = W, depth: int = 3, beam: int = 8, branch: int = 12,
           top: int = 12, exclude: Iterable = (), skip=None) -> list:
    """回合内束搜索。返回 [(序列最终分数 − 起点分数, [A...])]，按分数降序，
    每个「第一步」只保留它最好的那条序列。终局评分不含剩余指挥点（用不完作废）。"""
    excl = set(exclude)
    v0 = evaluate(sim, w)
    frontier = [(v0, sim, [])]
    best_by_first: dict = {}

    def note(v, seq):
        if not seq:
            return
        k = seq[0].key()
        if k not in best_by_first or v > best_by_first[k][0]:
            best_by_first[k] = (v, seq)

    for _ in range(depth):
        nxt = []
        for v, s, seq in frontier:
            acts = [a for a in gen_actions(s, w)
                    if (a.key() not in excl or seq) and not (skip is not None and skip(a))]
            # 先按单步 delta 粗排，限制分支
            scored = []
            for a in acts:
                s2 = apply(s, a)
                scored.append((evaluate(s2, w), a, s2))
            scored.sort(key=lambda x: -x[0])
            for v2, a, s2 in scored[:branch]:
                nxt.append((v2, s2, seq + [a]))
                note(v2, seq + [a])
        nxt.sort(key=lambda x: -x[0])
        frontier = nxt[:beam]
        if not frontier:
            break
    ranked = sorted(((v - v0, seq) for v, seq in best_by_first.values()), key=lambda x: -x[0])
    return ranked[:top]
