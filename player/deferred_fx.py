# -*- coding: utf-8 -*-
"""player.deferred_fx —— 延迟/常驻效果的**预计算**（rule 侧）：给 `Sim` 建 `event_fx["armed_spec"]` 与 `event_fx["armed"]`。

分工（同 `attack_fx`/`event_fx` 的一贯口径）：
  * **VM 是权威**：把手牌指令**留下来的钩子**（`OnAfterOtherCardAttacks`/`OnEndOfTurn`/`OnStartOfTurn`…，census 见
    `docs/DEFERRED-EFFECTS-CENSUS.md`）按"假设它此刻已经打出"对每个事件主体空跑一遍，得到整份效果桶；
  * `sim`/`engine.deferred` 只管布防与事件点查表（不手写任何一张牌的规则）；
  * **授予账**（`CustomAbilityAdd` 记下的 `ability_grants`）经 `view_overrides` 喂给 VM 里的 `HasCustomAbilityFromCard`
    （ECHELON：钩子体只对"它授予过 `trigger` 的单位"生效）。
诚实：空跑停在未实现原语 / 没预算 / 钩子没有 sim 事件点 ⇒ **记缺口**（`rule.gaps[牌名+"#deferred"]`），绝不当成"没有效果"。
本模块只依赖 `semantics.*`（`player` 不许直接 import `engine`）。
"""
from __future__ import annotations

import time

from semantics import effectvm as EV
from semantics.deferred import FIRE_POINTS, SUBJECT_HOOKS, leftover_hooks


def hooks_of(rule, c):
    """手牌指令 `c` 打出后仍挂着的钩子名列表；单位/地点 ⇒ `[]`（常驻在场钩子由读侧从活盘面拿）；读不出 ⇒ `None`。"""
    km = rule._km()
    if km is None or not rule.P.get("use_vm", True):
        return None
    try:
        if rule.is_unit(c):
            return []
        from semantics import cardprobe
        p = cardprobe.profile(km, c)
    except Exception:                                         # noqa: BLE001
        return None
    if not p or not p.get("ok"):
        return None
    return leftover_hooks(p.get("handlers"))


def note(rule, c, hooks) -> None:
    """把"这张牌带延迟钩子"如实写进 `rule.gaps` / `rule.eff_src` 之外的诊断表 `rule.deferred_seen`。

    缺口文本三类（任一类都意味着**这张牌的延迟后果没有被估值**）：钩子没有 sim 事件点（本函数）/ 事件由对方发起而评估权重为 0
    （`valuation_gap`）/ 空跑没跑出来（`probe_fx`）。
    """
    ds = rule.__dict__.setdefault("deferred_seen", {})
    ds[c.name] = list(hooks)
    unmodeled = [h for h in hooks if h not in FIRE_POINTS]
    if unmodeled:
        rule.gaps[c.name + "#deferred"] = "留下来的钩子 %s 没有 sim 事件点（FIRE_POINTS 之外）⇒ 打出后的延迟效果未建模，不计" % ",".join(unmodeled)


def valuation_gap(rule, c, hooks) -> None:
    """事件由**对方**发起的钩子（ECHELON）：我方回合搜索不展开对方回合 ⇒ 要靠评估权重 `W["armed_enemy_hits"]`（预期被打次数）估值；
    权重为 0（缺省，不编造）⇒ 记缺口——**不再把这张牌记成 `vm(空)`**。"""
    from semantics.deferred import ENEMY_EVENT_HOOKS
    en = [h for h in hooks if h in ENEMY_EVENT_HOOKS]
    if en and not float(rule._W.get("armed_enemy_hits", 0.0) or 0.0):
        rule.gaps[c.name + "#deferred_val"] = ("延迟钩子 %s 由对方事件触发，搜索不展开对方回合；W['armed_enemy_hits']=0 ⇒ 后果已建模但**未估值**"
                                                % ",".join(en))


def spec_fx(rule, st, sim) -> dict:
    """`event_fx["armed_spec"]`：`{手牌 id: {"hooks": (…), "grants": ((能力名, 单位 id)…)}}`（只含**有留下来的钩子**的指令）。"""
    out = {}
    by_id = {c.obj.CardID: c for c in (st.cards or [])}
    for h in sim.hand.values():
        c = by_id.get(h.id)
        if c is None or h.is_unit():
            continue
        hooks = hooks_of(rule, c)
        if not hooks:
            continue
        note(rule, c, hooks)
        grants = []
        for e in [h.eff] + [v for (k, _t), v in (sim.pair_eff or {}).items() if k == h.id]:
            for x in (e or {}).get("ability_grants") or ():
                if (x[0], x[1]) not in grants:
                    grants.append((x[0], x[1]))
        out[h.id] = {"hooks": tuple(hooks), "grants": tuple(grants), "probed": set(), "probed_hooks": set()}
    return out


def probe_fx(rule, st, sim) -> dict:
    """`event_fx["armed"]`：`{(来源牌 id, 钩子名, 主体 id|None): 桶}`（桶 = 整份效果摘要）。

    主体型钩子（`SUBJECT_HOOKS`）对场上每个单位 + 双方总部（键 `"hq"` = 敌方总部、`"own_hq"` = 我方总部）各空跑一次；
    回合钩子空跑一次。预算 `P["armed_probe_s"]`（缺省 3 s）：超了 ⇒ 剩下的主体**不进 `probed` 名单** ⇒ sim 触发时记缺口、不猜。
    """
    spec = sim.event_fx.get("armed_spec") or {}
    km = rule._km()
    if not spec or km is None or st is None or not rule.P.get("use_vm", True):
        return {}
    seat = st.my_side
    by_id = {c.obj.CardID: c for c in (st.cards or [])}
    board = [c for c in (st.cards or []) if c.obj.IsLocatedOnBoard()
             and (getattr(c, "raw", None) or {}).get("ptr")]
    subj = {}
    for c in board:
        if c.obj.IsFieldUnit():
            subj[c.obj.CardID] = c
    hq_e, hq_o = rule._hq_ptrs(st.other_side), rule._hq_ptrs(st.my_side)
    ptr_of = {k: c.raw["ptr"] for k, c in subj.items()}
    if hq_e:
        ptr_of["hq"] = hq_e[0]
    if hq_o:
        ptr_of["own_hq"] = hq_o[0]
    out = {}
    budget = float(rule.P.get("armed_probe_s", 3.0))
    t0 = time.time()
    meta = rule.fx_meta.setdefault("armed", {}) if hasattr(rule, "fx_meta") else {}
    meta.update({"probes": 0, "skipped": 0})
    for src, sp in spec.items():
        c = by_id.get(src)
        ptr = (getattr(c, "raw", None) or {}).get("ptr") if c is not None else None
        if not ptr:
            continue
        ov = {(ptr, "side"): int(seat), (ptr, "enterPlayOnTurn"): st.turn}
        vo = {}
        for name, uid in sp.get("grants") or ():          # 打出时授予的能力（带授予者 id）⇒ 钩子体的 `HasCustomAbilityFromCard` 答得出
            p_u = ptr_of.get(uid)
            if p_u:
                rows = vo.setdefault(p_u, {"received_abilities": []})["received_abilities"]
                rows.append({"ability": name, "givers": [src], "count": 1})
        rh = EV.make_read_hooks(st, seat, field_overrides=ov)
        for hook in sp["hooks"]:
            if hook not in FIRE_POINTS:
                continue
            todo = []
            if hook in SUBJECT_HOOKS:
                for key, p_d in ptr_of.items():
                    atk_ptr = next((p for k2, p in ptr_of.items() if k2 != key and k2 not in ("hq", "own_hq")), 0)
                    todo.append((key, {"defenderCard": p_d, "attackerCard": atk_ptr,
                                       "damageToDefender": 0, "attackCost": 1}))
            else:
                todo.append((None, {"turnnumber": st.turn}))
            for key, args in todo:
                if time.time() - t0 > budget:
                    meta["skipped"] += 1
                    continue
                meta["probes"] += 1
                try:
                    r = rule._armed_probe(km, st, ptr, hook, args, ov, vo, rh, hq_o, hq_e,
                                          float(rule.P.get("armed_one_s", 0.6)))
                except Exception as ex:                       # noqa: BLE001
                    rule.gaps[c.name + "#deferred"] = "延迟钩子 %s 空跑出错：%s: %s" % (hook, type(ex).__name__, str(ex)[:80])
                    continue
                if r.get("stopped"):
                    if not ("没有" in str(r["stopped"]) and "覆写" in str(r["stopped"])):
                        rule.gaps[c.name + "#deferred"] = "延迟钩子 %s 的 VM 停在：%s" % (hook, str(r["stopped"])[:100])
                    continue                                   # 没跑完 ⇒ 不进 probed ⇒ 触发时记缺口
                eff = {k: v for k, v in (r.get("eff") or {}).items() if k != "uncertain"}
                (sp["probed"].add(key) if hook in SUBJECT_HOOKS else sp["probed_hooks"].add(hook))
                if eff:
                    out[(src, hook, key)] = eff
                if r.get("gaps"):
                    rule.gaps.setdefault(c.name + "#deferred", "延迟钩子 %s 空跑里有读不出的状态：%s" % (hook, str(r["gaps"][0])[:90]))
    return out
