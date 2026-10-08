#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P5 影子对账 `RuleV2._shadow_check`（假 VM，真 `Sim`）：

* 字典路与直跑路一致 ⇒ same；不一致 ⇒ diff_ab（含具体字段）；
* 随机/抉择、VM 没跑完 ⇒ skipped（不比、不报错）；
* 只记录：不改传入的 sim；结果按 (牌, 目标, 回合, 种子) 缓存。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _hookfake import ME, OPP, Checker, K, card                   # noqa: E402
import player.rule as R                                            # noqa: E402
from policy.search import wire_sim as _wire_sim                    # noqa: E402
_wire_sim()
from sim.state import H, Sim, U                                    # noqa: E402
import semantics.effectvm as EV                                    # noqa: E402

chk = Checker()


class St:
    def __init__(self, cards):
        self.cards, self.my_side, self.other_side, self.turn = cards, ME, OPP, 5
        self.slots = {}


def mk(eff, applied, **rr):
    order = card(40, "ORDER", "hand", ME, ptr=40)
    st = St([order])
    sim = Sim({}, {}, 5.0, hand={40: H(40, "ORDER", 2, "order", 0, 0, (), {})}, my_side=ME,
              pair_eff={(40, None): eff})
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS, use_vm=True)
    pol._km = lambda: K()
    pol._seat = lambda: ME
    pol._other = lambda: OPP
    pol._hq_ptrs = lambda s: ()
    pol._rng_seed = lambda: 7
    pol.is_unit = lambda c: False
    base = {"records": [], "stopped": None, "complete": True, "chance": [], "choice": None,
            "applied": applied, "direct_gaps": []}
    base.update(rr)
    old = EV.record_effects, EV.make_read_hooks
    def _fake(*a, direct_state=None, **k):
        if direct_state is not None:                 # 假 VM：直跑 = 把 applied 按 sink 语义落到状态上
            from semantics.shadow import apply_calls
            apply_calls(direct_state, applied)
        return dict(base)
    EV.record_effects = _fake
    EV.make_read_hooks = lambda *a, **k: {}
    try:
        k0 = sim.kredits
        res = pol._shadow_check(st, sim)
    finally:
        EV.record_effects, EV.make_read_hooks = old
    return res, sim.kredits == k0


def main():
    # 字典：+2 指挥点；直跑：同样 +2 ⇒ same
    res, untouched = mk({"kredit": 2}, [("kredits", "mine", 2)])
    chk("字典 == 直跑 ⇒ same=1、无分歧", res["n"] == 1 and res["same"] == 1 and not res["diff_ab"], str(res))
    chk("影子不改传入的 sim", untouched)
    # 直跑少做了（动词没迁 ⇒ applied 空）⇒ diff_ab
    res, _ = mk({"kredit": 2}, [])
    chk("直跑漏了 ⇒ diff_ab 且带字段", res["n"] == 1 and len(res["diff_ab"]) == 1
        and "kredits" in res["diff_ab"][0]["diff"], str(res))
    # 抉择/随机 ⇒ skipped
    res, _ = mk({"kredit": 2}, [("kredits", "mine", 2)], chance=[1])
    chk("随机分支 ⇒ skipped、n=0", res["n"] == 0 and len(res["skipped"]) == 1, str(res))
    res, _ = mk({"kredit": 2}, [], complete=False, stopped="Unimplemented @0x1")
    chk("VM 没跑完 ⇒ skipped", res["n"] == 0 and "Unimpl" in res["skipped"][0], str(res))
    # 挂起标记：字典路带 forecast 标记、直跑也记了 forecast_pending ⇒ 一致（same），不再算 diff_bc 的假缺口
    res, _ = mk({"forecast": True}, [("forecast_pending", 16)])
    chk("预报：字典 forecast 标记 ≡ 直跑 forecast_pending ⇒ same", res["same"] == 1 and not res["diff_bc"], str(res))
    # 一边有标记一边没有 ⇒ 记 diff_bc（带 pending 名单）
    res, _ = mk({}, [("forecast_pending", 16)])
    chk("只有直跑一边有预报挂起 ⇒ diff_bc 且带 pending 名单",
        len(res["diff_bc"]) == 1 and res["diff_bc"][0].get("pending") == ["forecast"], str(res))
    # ★ 回归（2026-10-06 实机 AIR BLITZ/KM BISMARCK）：`pair_eff` 的『打总部』目标键是字面量 "hq" ⇒ 影子必须把它换成
    #   敌方总部那张牌的指针喂给 VM（以前 `by_id.get("hq")` 恒 None ⇒ 直跑没有目标 ⇒ 总部没挨打 ⇒ diff_ab）
    order = card(40, "ORDER", "hand", ME, ptr=40)
    ehq = card(90, "HQ", "hq", OPP, ptr=0x990)
    st = St([order, ehq])
    sim = Sim({}, {ME: 20, OPP: 19}, 5.0, hand={40: H(40, "ORDER", 2, "order", 0, 0, (), {})}, my_side=ME,
              pair_eff={(40, "hq"): {"damage": 3}})
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS, use_vm=True)
    pol._km = lambda: K()
    pol._seat = lambda: ME
    pol._other = lambda: OPP
    pol._hq_ptrs = lambda s: ()
    pol._rng_seed = lambda: 7
    pol.is_unit = lambda c: False
    seen = {}
    old = EV.record_effects, EV.make_read_hooks

    def _fake2(km, ptr, tptr=0, has_target=False, *a, direct_state=None, **k):
        seen["t"] = (tptr, has_target)
        direct_state.hq[OPP] -= 3                    # 假 VM：直跑 = 对敌方总部 3 点
        return {"records": [], "stopped": None, "complete": True, "chance": [], "choice": None,
                "applied": [("damage_hq", OPP, 3)], "direct_gaps": []}
    EV.record_effects, EV.make_read_hooks = _fake2, (lambda *a, **k: {})
    try:
        res = pol._shadow_check(st, sim)
    finally:
        EV.record_effects, EV.make_read_hooks = old
    chk("目标 'hq' ⇒ 喂敌方总部指针（has_target=True）", seen.get("t") == (0x990, True), str(seen))
    chk("打总部：字典路 ≡ 直跑 ⇒ same", res["same"] == 1 and not res["diff_ab"], str(res))

    # ★ 2026-10-06（用户拍板 §5.7）：抉择（`WhichChooseOne`）**每个选项是独立动作**：逐选项各比一次、各记一条（标签 `[选项i]`），
    #   A 的 `outcomes[i]` ↔ B 的 `record_effects(forced=[i])`；随机有种子就不枚举（见 `_shadow_branches`）。
    def mk_branch(outcomes, per_branch, chance=(), nodes=None, seed=7, cap=None, exact=False):
        order = card(40, "ORDER", "hand", ME, ptr=40)
        st = St([order])
        sim = Sim({}, {}, 5.0, hand={40: H(40, "ORDER", 2, "order", 0, 0, (), {})}, my_side=ME,
                  pair_eff={(40, None): {"outcomes": [(0.5, dict(e)) for e in outcomes], "outcomes_mode": "max"}})
        pol = R.RuleV2.__new__(R.RuleV2)
        pol.P = dict(R.PARAMS, use_vm=True)
        if cap is not None:
            pol.P["shadow_branch_cap"] = cap
        pol._km = lambda: K()
        pol._seat = lambda: ME
        pol._other = lambda: OPP
        pol._hq_ptrs = lambda s: ()
        pol._rng_seed = lambda: seed
        pol.is_unit = lambda c: False
        old = EV.record_effects, EV.make_read_hooks
        calls = []

        def _fake(*a, forced=None, direct_state=None, **k):
            calls.append((forced, k.get("rng_seed")))
            idx = (forced or [0])[0]
            applied = per_branch[idx]
            from semantics.shadow import apply_calls
            apply_calls(direct_state, applied)
            return {"records": [], "stopped": None, "complete": True, "chance": list(chance), "choice": True, "exact": exact,
                    "nodes": nodes if nodes is not None else [{"verb": "WhichChooseOne", "size": len(outcomes)}],
                    "applied": applied, "direct_gaps": []}
        EV.record_effects, EV.make_read_hooks = _fake, (lambda *a, **k: {})
        try:
            res = pol._shadow_check(st, sim)
        finally:
            EV.record_effects, EV.make_read_hooks = old
        return res, calls, pol
    ok2 = [[("kredits", "mine", 1)], [("kredits", "mine", 2)]]
    res, calls, pol = mk_branch([{"kredit": 1}, {"kredit": 2}], ok2)
    chk("抉择：每个选项是独立条目（n=2、same=2、choice_actions=2），各跑一次（forced=None/[1]），种子都传给 B",
        res["n"] == 2 and res["same"] == 2 and not res["diff_ab"] and not res["skipped"] and res.get("choice_actions") == 2
        and [(list(f) if f else f, sd) for f, sd in calls] == [(None, 7), ([1], 7)], str(res) + str(calls))
    keys = sorted(k for k in pol._shadow_out)
    chk("抉择：缓存里每个选项一条（键尾是选择路径 (0,)/(1,)）", [k[-1] for k in keys] == [(0,), (1,)], str(keys))
    res2 = pol._shadow_check(St([card(40, "ORDER", "hand", ME, ptr=40)]), Sim({}, {}, 5.0, hand={40: H(40, "ORDER", 2, "order", 0, 0, (), {})},
                                                                              my_side=ME, pair_eff={(40, None): {"outcomes": [(0.5, {"kredit": 1}), (0.5, {"kredit": 2})],
                                                                                                                 "outcomes_mode": "max"}}))
    chk("抉择：同一回合同一种子再对账 ⇒ 走缓存、逐选项重放（n=2、same=2）", res2["n"] == 2 and res2["same"] == 2, str(res2))
    res, _, _ = mk_branch([{"kredit": 1}, {"kredit": 2}], [[("kredits", "mine", 1)], [("kredits", "mine", 3)]])
    chk("抉择：第 2 个选项不一致 ⇒ 只它 diff_ab（标签 [选项1]），第 1 个仍 same", len(res["diff_ab"]) == 1 and "[选项1]" in res["diff_ab"][0]["card"]
        and res["same"] == 1, str(res))
    res, _, _ = mk_branch([{"kredit": 1}, {"kredit": 2}], ok2, cap=1)
    chk("抉择：不再受 shadow_branch_cap 限制（每个选项本来就是独立动作）", res["n"] == 2 and not res["skipped"], str(res))
    # 随机：有种子 ⇒ 不枚举成分支；仍留着的随机点（没接进牌局流的原语）如实 skipped，且 B 只跑了一次
    res, calls, _ = mk_branch([{"kredit": 1}, {"kredit": 2}], ok2, chance=["GiveRandomCombatKeyword"],
                              nodes=[{"verb": "GiveRandomCombatKeyword", "size": 2}])
    chk("有种子 + 残留随机点 ⇒ skipped（原因点名动词、不枚举：B 只跑 1 次）", res["n"] == 0 and len(res["skipped"]) == 1
        and "GiveRandomCombatKeyword" in res["skipped"][0] and len(calls) == 1, str(res) + str(calls))
    # 随机：读不到种子（None）⇒ 退回旧的枚举（标签 [随机分支i]、cap 封顶）
    res, calls, _ = mk_branch([{"kredit": 1}, {"kredit": 2}], ok2, chance=["GiveRandomCombatKeyword"],
                              nodes=[{"verb": "GiveRandomCombatKeyword", "size": 2}], seed=None)
    chk("没有种子 ⇒ 随机枚举兜底（n=2、标签无 [选项]、enum_fallback=2）", res["n"] == 2 and res["same"] == 2 and res.get("enum_fallback") == 2
        and not res.get("choice_actions"), str(res))
    res, _, _ = mk_branch([{"kredit": 1}, {"kredit": 2}], ok2, chance=["GiveRandomCombatKeyword"],
                          nodes=[{"verb": "GiveRandomCombatKeyword", "size": 2}], seed=None, cap=1)
    chk("没有种子 + 分支数超 shadow_branch_cap ⇒ skipped", res["n"] == 0 and len(res["skipped"]) == 1, str(res))
    # 确定随机（exact）：单条目、不分支；计入 rng_exact
    order = card(40, "ORDER", "hand", ME, ptr=40)
    st = St([order])
    sim = Sim({}, {}, 5.0, hand={40: H(40, "ORDER", 2, "order", 0, 0, (), {})}, my_side=ME, pair_eff={(40, None): {"kredit": 1}})
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS, use_vm=True)
    pol._km, pol._seat, pol._other = (lambda: K()), (lambda: ME), (lambda: OPP)
    pol._hq_ptrs, pol._rng_seed, pol.is_unit = (lambda s: ()), (lambda: 7), (lambda c: False)
    old = EV.record_effects, EV.make_read_hooks
    seeds = []

    def _fake3(*a, direct_state=None, **k):
        seeds.append(k.get("rng_seed"))
        from semantics.shadow import apply_calls
        apply_calls(direct_state, [("kredits", "mine", 1)])
        return {"records": [], "stopped": None, "complete": True, "chance": [], "choice": False, "exact": True, "nodes": [],
                "applied": [("kredits", "mine", 1)], "direct_gaps": []}
    EV.record_effects, EV.make_read_hooks = _fake3, (lambda *a, **k: {})
    try:
        res = pol._shadow_check(st, sim)
    finally:
        EV.record_effects, EV.make_read_hooks = old
    chk("确定随机（exact）⇒ 单条目 same，rng_exact=1，B 拿到同一个种子", res["n"] == 1 and res["same"] == 1 and res.get("rng_exact") == 1
        and seeds == [7], str(res) + str(seeds))
    # 没有打出钩子覆写 ⇒ 与 A 比"什么都没发生"（不再 skipped）
    res, _ = mk({}, [], complete=False, stopped="这张牌没有 OnPlayedFromHand 覆写")
    chk("无打出覆写 ⇒ same（A 空 ≡ B 空）", res["same"] == 1 and not res["skipped"], str(res))
    return chk.done() if hasattr(chk, "done") else 0


if __name__ == "__main__":
    sys.exit(main())
