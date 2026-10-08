#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`engine/calls.py` 的**三角验证**（P5/M5）：直跑 ≡ 直跑→取 applied→重放。

判据（每一行都跑一遍）：同一个动词、同一组实参，
  ① 直跑 sink 改 `state_a`（`engine.scripts.native_hooks`）；
  ② 把它的 `ctx.applied` 用 `engine.calls.apply_calls` 重放到**同构的新** `state_b`；
  ⇒ `_state_snapshot(state_a) == _state_snapshot(state_b)` **且**重放无缺口 ✓。

为什么这条判据值钱：`apply_calls` 表里的实现是**照着 sink 逐行镜像**的 ⇒ 一旦漂移，
这里当场红 ✓（这正是 M5"类型化状态变更"要的性质：**语义只有一处来源**）。
★ 表外的 kind 另有断言（必须记缺口、**不许静默跳过**）✓。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import calls as CALLS                                    # noqa: E402
from engine import scripts as SC                                     # noqa: E402
from engine import state as S                                        # noqa: E402

ME, OPP = 1, 2
P1 = 0x7001
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), kredits=5, slots=3):
    s = S.Sim({}, {ME: 20, OPP: 20}, kredits, {}, my_side=ME, slots=slots, kredit_max=24)
    for u in units:
        s.units[u.id] = u
    s.playing_side = ME
    s.opp_kredits, s.opp_slots = 5, 2
    return s


def unit(uid=11, side=ME, atk=3, dfn=4, kw=(), row="frontline"):
    return S.U(uid, side, row, atk, dfn, 1, "infantry", kw=kw)


def tri(label, verb, args, mk_state, *, ptr_ids=None, gates=None, expect_kind=None, hq_ptr_sides=None):
    """三角验证一步；返回 (ok, why)。"""
    s1 = mk_state()
    h1 = SC.native_hooks(s1, ptr_ids=ptr_ids or {}, my_side=ME, gates=gates, hq_ptr_sides=hq_ptr_sides)
    ctx = h1["__ctx__"]
    h1[verb](None, None, None, list(args), None)
    applied = list(ctx.applied)
    if not applied:
        return False, "直跑没产出任何 applied（args 约定不对？）gaps=%s" % (ctx.gaps[:2],)
    s2 = mk_state()
    res = CALLS.apply_calls(s2, applied)
    a, b = SC._state_snapshot(s1), SC._state_snapshot(s2)
    diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
    # 重放**不该引入新缺口**；sink 自己报的缺口（例：`apply_suppress` 的"未建模那部分"）两条路都有 ✓
    sink_gaps = list(ctx.gaps)
    extra_gaps = [g for g in res.gaps if g not in sink_gaps]
    ok = not diff and not extra_gaps
    if expect_kind and not any((c[0] == expect_kind) for c in applied):
        return False, "applied 里没有预期的 kind=%s：%s" % (expect_kind, applied[:3])
    return ok, "applied=%s diff=%s extra_gaps=%s" % (applied[:3], diff, extra_gaps[:2])


def main():
    # ---- 资源族 ----
    for label, args, kind in (("我方指挥点", [1, 3, None], "kredits"),
                              ("对方指挥点", [2, 3, None], "kredits")):
        ok, why = tri("资源/" + label, "GiveKreditsBySide", args, mk, expect_kind=kind)
        chk("三角验证 资源/%s：`GiveKreditsBySide` 直跑 ≡ 重放" % label, ok, why)
    ok, why = tri("资源/槽", "ChangeKreditSlotsBySide", [1, 2, None], mk, expect_kind="slots")
    chk("三角验证 资源/槽：`ChangeKreditSlotsBySide` 直跑 ≡ 重放", ok, why)
    ok, why = tri("资源/绝对值", "setKreditBySide", [1, 7, None], mk, expect_kind="kredit_set")
    chk("三角验证 资源/绝对值：`setKreditBySide`（我方）直跑 ≡ 重放", ok, why)

    # ---- 数值族（门：buffable + unrevealed_covert，两个都要喂 ✓）----
    _mk_u = lambda: mk([unit(kw=("shock",))])                        # noqa: E731
    _GATES = {P1: {"buffable": True, "unrevealed_covert": False}}
    for verb, args in (("ChangeAttack", [P1, 7, 2, 1]),
                       ("ChangeDefense", [P1, 7, 2, 1]),
                       ("ChangeHeavyArmor", [P1, 7, 2, 1]),
                       ("ChangeOperationCost", [P1, 7, 2, 1])):
        ok, why = tri(verb, verb, args, _mk_u, ptr_ids={P1: 11}, gates=_GATES)
        chk("三角验证 数值/%s：直跑 ≡ 重放" % verb, ok, why)

    # ---- 本回合加攻 ----
    ok, why = tri("加攻", "AddAttackUntilEndOfTurn", [P1, 7, 3], _mk_u, ptr_ids={P1: 11},
                  expect_kind="attack_turn")
    chk("三角验证 本回合加攻：`AddAttackUntilEndOfTurn` 直跑 ≡ 重放（总量 + `atk_turn` 两处都要重放 ✓）", ok, why)

    # ---- 状态族（`PinUnit`/`SuppressUnit` 收 **ID**；`RemovePin` 收**指针** —— 逐动词不同，别一刀切 ✓）----
    for verb, args, kind, ptrs in (("PinUnit", [11, 7, None], "pin", {}),
                                   ("RemovePin", [P1, 7, None], "unpin", {P1: 11}),
                                   ("SuppressUnit", [11, 7, 0, None], "suppress", {})):
        ok, why = tri(verb, verb, args, _mk_u, ptr_ids=ptrs, expect_kind=kind)
        chk("三角验证 状态/%s：直跑 ≡ 重放" % verb, ok, why)

    # ---- 控制权（2026-10-06 补：CONFUSION / PARTISANS 跑通后暴露的"表外 kind='steal'"）----
    _mk_e = lambda: mk([unit(11, OPP), unit(12, ME)])                # noqa: E731
    ok, why = tri("偷牌", "TakeControlOfEnemyUnit", [P1, 7, None], _mk_e, ptr_ids={P1: 11}, expect_kind="steal")
    chk("三角验证 控制权：`TakeControlOfEnemyUnit` 直跑 ≡ 重放（steal 条目同一函数 `apply_take_control`）", ok, why)
    s_miss = mk()
    r_miss = CALLS.apply_calls(s_miss, [("steal", 999)])
    chk("steal 的目标不在场 ⇒ 记缺口、不改状态", any("steal" in g for g in r_miss.gaps), str(r_miss.gaps))

    # ---- 生命周期 / 伤害（两者都按**指针**收目标 ✓）----
    ok, why = tri("摧毁", "DestroyCard", [P1, 7, 0, None], _mk_u, ptr_ids={P1: 11},
                  expect_kind="destroy_card")
    chk("三角验证 生命周期：`DestroyCard`（指针）直跑 ≡ 重放（离场；`destroy_card`+`destroy` "
        "两条日志靠 popped 去重 ✓）", ok, why)
    ok, why = tri("伤害", "DamageCard", [P1, 5, 7, False, False, False, None], _mk_u,
                  ptr_ids={P1: 11}, expect_kind="damage_card")
    chk("三角验证 伤害：`DamageCard`（指针）直跑 ≡ 重放（扣防；降到 ≤0 时两条路都该走死亡那一步 ✓）", ok, why)


    # ---- 总部牌作目标（ChangeDefense / ChangeAttack / PinUnit / DestroyCard 的 HQ 指针形态）----
    HQP, OWNP = 0x900, 0x901
    sides = {HQP: OPP, OWNP: ME}
    for label, amt, ct, start, want in (("永久加防 +2", 2, 1, 20, 22), ("永久减防 -3", -3, 1, 20, 17),
                                        ("设为 5（SetValue）", 5, 2, 20, 5), ("加防不夹到旧上限（22→24）", 2, 1, 22, 24)):
        def _mkh(start=start):
            st = mk([unit(11, ME)])
            st.hq[OPP] = start
            return st
        ok, why = tri(label, "ChangeDefense", [HQP, 7, amt, ct, False, None], _mkh, ptr_ids={}, hq_ptr_sides=sides,
                      expect_kind="change_defense_hq")
        s_chk = _mkh()
        h_chk = SC.native_hooks(s_chk, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
        h_chk["ChangeDefense"](None, None, None, [HQP, 7, amt, ct, False, None], None)
        chk("三角验证 总部 ChangeDefense %s：直跑 ≡ 重放，且敌方总防 = %d" % (label, want),
            ok and s_chk.hq[OPP] == want and s_chk.hq[ME] == 20, "%s hq=%s" % (why, s_chk.hq))
    s_own = mk()
    h_own = SC.native_hooks(s_own, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
    h_own["ChangeDefense"](None, None, None, [OWNP, 7, 3, 1, False, None], None)
    chk("己方总部 +3 防 ⇒ 己方 23、敌方不动、无缺口", s_own.hq[ME] == 23 and s_own.hq[OPP] == 20
        and not h_own["__ctx__"].gaps, "%s %s" % (s_own.hq, h_own["__ctx__"].gaps))
    s_rj = mk()
    h_rj = SC.native_hooks(s_rj, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
    h_rj["ChangeDefense"](None, None, None, [HQP, 7, 5, 0, False, None], None)       # changeType 0 ⇒ 原版 Label_3734 拒绝
    chk("总部 ChangeDefense changeType=0（原版拒绝）⇒ 不改、不记 applied、无缺口",
        s_rj.hq[OPP] == 20 and not h_rj["__ctx__"].applied and not h_rj["__ctx__"].gaps,
        "%s %s" % (s_rj.hq, h_rj["__ctx__"].applied))
    s_lo = mk()
    h_lo = SC.native_hooks(s_lo, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
    h_lo["ChangeDefense"](None, None, None, [HQP, 0, 5, 1, False, None], None)       # instigatorID <= 0 ⇒ 原版 error 日志 + return
    chk("总部 ChangeDefense instigatorID<=0（原版 Label_386）⇒ 不改", s_lo.hq[OPP] == 20, str(s_lo.hq))
    ok, why = tri("总部攻击", "ChangeAttack", [HQP, 7, 2, 1, False, None], lambda: mk(), ptr_ids={}, hq_ptr_sides=sides,
                  expect_kind="change_attack_hq")
    chk("三角验证 总部 ChangeAttack：直跑记无状态标记，重放不引入缺口、总防不动", ok, why)
    s_pin = mk()
    h_pin = SC.native_hooks(s_pin, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
    h_pin["PinUnit"](None, None, None, [HQP, 7], None)
    chk("`PinUnit(总部)` ⇒ 合法 no-op（原版 IsUnit 门），不记缺口", not h_pin["__ctx__"].gaps
        and not h_pin["__ctx__"].applied, str(h_pin["__ctx__"].gaps))
    s_dc = mk()
    h_dc = SC.native_hooks(s_dc, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)
    h_dc["DestroyCard"](None, None, None, [HQP, 7, 0, None], None)
    g_dc = h_dc["__ctx__"].gaps
    chk("`DestroyCard(总部)` ⇒ 专门的缺口（总部牌），不是『指针换不出单位』，状态不动",
        len(g_dc) == 1 and "总部" in g_dc[0] and s_dc.hq[OPP] == 20, str(g_dc))

    # 字典路（A）对敌方总部加防：`heal_opp_hq` ⇒ 与直跑同一个终态（对账口径）
    import sim.engine as _EN
    s_a = mk()
    _EN._apply_eff(s_a, {"heal_opp_hq": 2}, "hq")
    s_b = mk()
    SC.native_hooks(s_b, ptr_ids={}, my_side=ME, hq_ptr_sides=sides)["ChangeDefense"](None, None, None, [HQP, 7, 2, 1, False, None], None)
    chk("字典路 `heal_opp_hq`=2 ≡ 直跑 `ChangeDefense(敌方总部,+2)`（hq 终态一致）", s_a.hq == s_b.hq == {ME: 20, OPP: 22},
        "%s %s" % (s_a.hq, s_b.hq))

    # ---- 税 / 揭示 / 限制 / 行动权 / 分出胜负 ----
    ok, why = tri("指向税", "AddKreditsTax", [P1, 3, None], _mk_u, ptr_ids={P1: 11},
                  expect_kind="kredits_tax")
    chk("三角验证 指向税：`AddKreditsTax` 直跑 ≡ 重放", ok, why)
    ok, why = tri("揭示", "RevealCard", [11, 7, None], _mk_u, expect_kind="reveal")
    chk("三角验证 揭示：`RevealCard` 直跑 ≡ 重放（去 covert）", ok, why)
    ok, why = tri("限制", "AddGameplayRestriction", [1, 5, 11, 2], _mk_u, expect_kind="restriction_add")
    chk("三角验证 限制：`AddGameplayRestriction` 直跑 ≡ 重放", ok, why)
    for verb, args, kind in (("SetPlayingSide", [1, None], "playing_side"),
                             ("ForceEndTurn", [], "force_end_turn"),
                             ("EndMatch", [1, 0.0], "end_match")):
        ok, why = tri(verb, verb, args, mk, expect_kind=kind)
        chk("三角验证 行动权/胜负：`%s` 直跑 ≡ 重放" % verb, ok, why)

    # ---- 群体伤害 / 回满防御（2026-10-06：重放表补齐 `damage_aoe` / `heal_unit`）----
    P2 = 0x7002
    _mk_two = lambda: mk([unit(11, dfn=4), unit(12, side=OPP, dfn=2)])           # noqa: E731
    ok, why = tri("群体伤害", "DamageMultipleCards", [[11, 12], 3, 7], _mk_two, expect_kind="damage_aoe")
    chk("三角验证 群体伤害：`DamageMultipleCards`（id 数组；一个活下来、一个阵亡）直跑 ≡ 重放", ok, why)
    ok, why = tri("群体伤害/免疫", "DamageMultipleCards", [[11], 3, 7],
                  lambda: mk([S.U(11, ME, "frontline", 3, 4, 1, "infantry", kw=("immune",))]),
                  expect_kind="damage_aoe")
    chk("三角验证 群体伤害：免疫单位吃 0（`deal_damage` 自己判）直跑 ≡ 重放", ok, why)
    res = CALLS.apply_calls(mk(), [("damage_aoe", 99, 3)])
    chk("`damage_aoe` 目标不在场上 ⇒ 记缺口（不静默）", len(res.gaps) == 1 and not res.applied, str(res.gaps))

    def _mk_hurt():
        s_ = mk()
        s_.units[11] = S.U(11, ME, "frontline", 2, 2, 1, "infantry", mdef=5)
        return s_
    ok, why = tri("回满", "FullyHealCard", [P1, 7], _mk_hurt, ptr_ids={P1: 11},
                  gates={11: {"heal_vetoed": False}}, expect_kind="heal_unit")
    chk("三角验证 回满：`FullyHealCard` 直跑 ≡ 重放（`defense = maxDefense`）", ok, why)
    res = CALLS.apply_calls(mk(), [("heal_unit", 99, 5)])
    chk("`heal_unit` 目标不在场上 ⇒ 记缺口", len(res.gaps) == 1 and not res.applied, str(res.gaps))

    # ---- 影子对账：重放缺口里扣掉直跑已声明的（SUPPRESS_GAP 两边各一条 ≠ 分叉）----
    from player.rule import _replay_new_gaps
    chk("`_replay_new_gaps`：B 已声明的缺口按条数扣掉，多出来的才算重放缺口",
        _replay_new_gaps(["a", "a", "b"], ["a", "a"]) == ["b"]
        and _replay_new_gaps(["a", "a"], ["a"]) == ["a"] and _replay_new_gaps([], None) == [])

    # ---- 表外 kind：必须**记缺口**，不许静默跳过 ----
    res = CALLS.apply_calls(mk(), [("spawn", -2001, "X"), ("veteran", 11, True),
                                   ("draw", 2), ("no_such_kind", 1)])
    chk("★ 表外 kind 一律**记缺口**（`spawn`/`veteran`/没注入 on_draw 的 `draw`/未知名 —— 信息不全就不猜 ✓，"
        "绝不静默跳过 ✗）", len(res.gaps) == 4 and not res.applied,
        "gaps=%s applied=%s" % (res.gaps, res.applied))

    # ---- 明确"不改状态"的 kind：不算缺口，但也不许改状态 ----
    s = mk()
    snap0 = SC._state_snapshot(s)
    res = CALLS.apply_calls(s, [("intel_seen", 3, ME)])
    chk("`intel_seen` 等**声明不改状态**的 kind：不算缺口、也真的没改状态 ✓",
        not res.gaps and len(res.applied) == 1 and SC._state_snapshot(s) == snap0,
        "applied=%s gaps=%s" % (res.applied, res.gaps))

    # ---- 挂起提示标记：不改状态、不算缺口、进 `res.pending` 交调用方展开 ----
    s = mk()
    snap0 = SC._state_snapshot(s)
    res = CALLS.apply_calls(s, [("forecast_pending", 16), ("choose_spawn_pending", 17), ("hand_target_pending", 18)])
    chk("挂起提示（预报/三选一/选手牌）：不改状态、**不是缺口**、全进 `pending`（调用方有候选表就展开，没有才记缺口）✓",
        not res.gaps and SC._state_snapshot(s) == snap0
        and res.pending == [("forecast", 16), ("choose_spawn", 17), ("hand_target", 18)]
        and len(res.applied) == 3, "gaps=%s pending=%s" % (res.gaps, res.pending))
    # 真的走直跑 sink 产出的 `forecast_pending`：重放后进 pending（不再是 diff_bc 的假缺口）
    s = mk()
    h = SC.native_hooks(s, my_side=ME)
    h["Forecast"](None, None, None, [16], None)
    res = CALLS.apply_calls(mk(), h["__ctx__"].applied)
    chk("直跑 `Forecast` 记下的调用经重放 ⇒ pending=[('forecast',16)]、无缺口 ✓",
        res.pending == [("forecast", 16)] and not res.gaps, "pending=%s gaps=%s" % (res.pending, res.gaps))

    # ---- gain_cards：抽牌链由调用方注入；没注入 ⇒ 缺口，注入了 ⇒ 调一次且记 applied ----
    got = []
    res = CALLS.apply_calls(mk(), [("gain_cards", 1)])
    chk("gain_cards 没注入 on_draw ⇒ 缺口（不猜）", len(res.gaps) == 1 and not res.applied, str(res.gaps))
    res = CALLS.apply_calls(mk(), [("gain_cards", 2)], on_draw=lambda st, n: got.append(n))
    chk("gain_cards 注入 on_draw ⇒ 调用 n=2、无缺口", got == [2] and not res.gaps and res.applied == [("gain_cards", 2)], str(res))

    # ---- draw（我方抽牌，直跑 sink 记 ("draw", n)）：同 gain_cards 口径经注入链重放；对方 opp_draw 仍是缺口 ----
    got = []
    res = CALLS.apply_calls(mk(), [("draw", 1)])
    chk("draw 没注入 on_draw ⇒ 缺口（不猜）", len(res.gaps) == 1 and not res.applied, str(res.gaps))
    res = CALLS.apply_calls(mk(), [("draw", 1), ("draw", 3)], on_draw=lambda st, n: got.append(n))
    chk("draw 注入 on_draw ⇒ 依次 n=1、3、无缺口、记 applied",
        got == [1, 3] and not res.gaps and res.applied == [("draw", 1), ("draw", 3)], "%s %s" % (got, res))
    res = CALLS.apply_calls(mk(), [("draw", "x")], on_draw=lambda st, n: got.append(n))
    chk("draw 张数不是整数 ⇒ 缺口、不调链", len(res.gaps) == 1 and got == [1, 3], str(res.gaps))
    got = []
    s = mk()
    res = CALLS.apply_calls(s, [("opp_draw", 2), ("opp_draw", 1)], on_draw=lambda st, n: got.append(n))
    chk("opp_draw（对方抽牌）⇒ `opp_cards += n`、不经我方抽牌链、无缺口", s.opp_cards == 3 and not got and not res.gaps
        and res.applied == [("opp_draw", 2), ("opp_draw", 1)], "%s %s" % (s.opp_cards, res))
    res = CALLS.apply_calls(mk(), [("opp_draw", "x")])
    chk("opp_draw 张数不是整数 ⇒ 缺口", len(res.gaps) == 1, str(res.gaps))
    # 三角验证：直跑 sink（DrawCardsFromDeckBySide side=对方 / DrawSpecific side=对方）≡ 重放
    for fname, args in (("DrawCardsFromDeckBySide", (5, 2, 2, True)), ("DrawSpecificCardFromDeckBySide", (5, 9, 2, True))):
        sb = mk()
        hb = SC.native_hooks(sb, my_side=ME)
        hb[fname](None, None, None, list(args), None)
        applied = [c for c in hb["__ctx__"].applied if c[0] == "opp_draw"]
        sc = mk()
        rc = CALLS.apply_calls(sc, hb["__ctx__"].applied)
        chk("三角验证 对方抽牌（%s）：直跑 ≡ 重放（opp_cards 与全量快照一致）" % fname,
            bool(applied) and sb.opp_cards > 0 and SC._state_snapshot(sb) == SC._state_snapshot(sc) and not rc.gaps,
            "b=%s c=%s gaps=%s" % (sb.opp_cards, sc.opp_cards, rc.gaps))

    # ---- kredit_cost：手牌那张牌的费用改动（YOUR COURAGE 的"手中协力牌 -1 花费"）----
    s = mk()
    s.hand[7] = S.H(7, "BOND", 3, "infantry")
    res = CALLS.apply_calls(s, [("kredit_cost", 7, -1, 0)])
    chk("kredit_cost 重放：手牌里那张牌的费用改动，无缺口（同一原生 change_kredit_cost）",
        not res.gaps and s.hand[7].cost != 3 and res.applied == [("kredit_cost", 7, -1, 0)],
        "cost=%s gaps=%s" % (s.hand[7].cost, res.gaps))
    res = CALLS.apply_calls(mk(), [("kredit_cost", 99, -1, 0)])
    chk("kredit_cost：牌不在手牌/牌库 ⇒ 缺口", len(res.gaps) == 1, str(res.gaps))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
