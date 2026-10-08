#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""延迟/常驻效果登记表（`engine/deferred.py`）离线测试：ECHELON + 回合开始族 + 回合结束族 + 诚实（缺口）+ A/B/C 对账。

用户 2026-10-07 报："它似乎不认得梯队的效果"——实机日志里 `eff_src["ECHELON"]=="vm(空)"`（`OnPlayedFromHand` 空跑为空，被当成"已知的空"）。
BP（`card_event_echelon.cpp`，逐行读过）：打出时对场上每个己方单位 `CustomAbilityAdd("trigger", 单位id, 本牌id)`；
之后 `OnAfterOtherCardAttacks(defender, …)`：`enterPlayOnTurn>0` ∧ 被打的牌是己方**单位** ⇒ 场上每张**别的**、`HasCustomAbilityFromCard("trigger", 本牌id)`
的牌 `ChangeAttack(+1)` + `ChangeDefense(+1)`（永久）。

夹具口径（弯路 #47）：**桶**（`event_fx["armed"]`）按 VM 空跑真实会产出的形状手写（`buff_ids` = `{单位 id: [攻, 防]}`，
`effectvm.to_effects` 对 `ChangeAttack/Defense(非选定目标)` 就是这形状）；这里验的是 sim/登记表这一侧的接线，不是 VM 本身（VM 侧要实机）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sim.engine as EN                                          # noqa: E402
import sim.state as S                                            # noqa: E402
from engine import scripts as SC                                 # noqa: E402
from engine.calls import apply_calls                             # noqa: E402
from engine.effectvm import Recorder, to_effects                 # noqa: E402
from engine.natives.abilities import has_grant                   # noqa: E402
from evaluation.value import W, armed_value, evaluate            # noqa: E402
from kardsmem.gamemodel import ESide                             # noqa: E402

ME, OPP = ESide(1), ESide(2)
fails = 0
ECH = 50                                                          # ECHELON 的卡 id（= 手牌 id）
ECH_HOOK = "OnAfterOtherCardAttacks"


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(own=(1, 2, 3), foes=(21,), kredits=5):
    units = {}
    for i in own:
        units[i] = S.U(i, ME, "frontline", 2, 3, 1, "infantry")
    for i in foes:
        units[i] = S.U(i, OPP, "frontline", 2, 3, 1, "infantry")
    s = S.Sim(units, {ME: 20, OPP: 20}, kredits, {}, my_side=ME, slots=5)
    s.hand[ECH] = S.H(ECH, "ECHELON", 2, "order",
                      eff={"ability_grants": [["trigger", i, ECH] for i in own]})
    return s


def echelon_tables(s, own):
    """VM 会给的桶：被打的是 d ⇒ **别的**被授予单位各 +1+1；d 在预计算名单里（`probed`）。"""
    sp = {"hooks": (ECH_HOOK,), "grants": tuple(("trigger", i) for i in own),
          "probed": set(own) | {21, "hq", "own_hq"}, "probed_hooks": set()}
    armed = {}
    for d in own:
        others = {i: [1, 1] for i in own if i != d}
        if others:
            armed[(ECH, ECH_HOOK, d)] = {"buff_ids": others}
    s.event_fx["armed_spec"] = {ECH: sp}
    s.event_fx["armed"] = armed


def stats(s, *ids):
    return [(s.units[i].atk, s.units[i].dfn) for i in ids]


def main():
    # ---- ① ECHELON：3 个单位，敌人打其一 ⇒ 另两个 +1+1、被打的不动 ----
    own = (1, 2, 3)
    s0 = mk(own)
    echelon_tables(s0, own)
    s1 = EN.sim_order(s0, ECH)
    chk("打出 ECHELON ⇒ 三个单位都被授予 `trigger`，账记在 `grants[(ECH,'trigger')]`",
        s1.grants.get((ECH, "trigger")) == {1, 2, 3} and all("trigger" in s1.units[i].ab for i in own),
        str(s1.grants))
    chk("`HasCustomAbilityFromCard` 的答案（`has_grant`）：授予过的真、别的单位/别的授予者假",
        has_grant(s1, 2, "trigger", ECH) and not has_grant(s1, 21, "trigger", ECH) and not has_grant(s1, 2, "trigger", 99))
    chk("布防：`armed` 里有一条 (ECH, OnAfterOtherCardAttacks, 我方)",
        [(a.src, a.hook, a.side) for a in s1.armed] == [(ECH, ECH_HOOK, ME)], str(s1.armed))
    chk("打出这一步本身不改数值（效果是延迟的）", stats(s1, 1, 2, 3) == [(2, 3)] * 3, str(stats(s1, 1, 2, 3)))
    s2 = EN.sim_enemy_attack_event(s1, 1)
    chk("★ 敌方打 1 号 ⇒ 2、3 号各 +1/+1（永久），1 号不变", stats(s2, 1, 2, 3) == [(2, 3), (3, 4), (3, 4)], str(stats(s2, 1, 2, 3)))
    s3 = EN.sim_enemy_attack_event(s2, 2)
    chk("再打 2 号 ⇒ 1、3 号再 +1/+1（每次攻击一次，累计）", stats(s3, 1, 2, 3) == [(3, 4), (3, 4), (4, 5)], str(stats(s3, 1, 2, 3)))
    chk("登记表是**可复制状态**：分支里的 `armed`/`grants` 互不污染（原状态没被事件改）",
        stats(s1, 1, 2, 3) == [(2, 3)] * 3 and s2.armed is not s1.armed and s2.grants is not s1.grants)

    # ---- ② 打出后才部署的单位不在授予账里 ⇒ 不受益；它被打则缺口（事后进场没预计算）----
    s4 = EN.sim_order(s0, ECH)
    s4 = EN.sim_deploy(s4, 9, 2, 2, 1, "infantry")
    chk("事后部署的单位没有 `trigger`（`ab`/`grants` 都没有它）",
        "trigger" not in s4.units[9].ab and 9 not in s4.grants[(ECH, "trigger")])
    s5 = EN.sim_enemy_attack_event(s4, 1)
    chk("1 号被打 ⇒ 老单位照常受益，9 号**不**受益", stats(s5, 2, 3, 9) == [(3, 4), (3, 4), (2, 2)], str(stats(s5, 2, 3, 9)))
    s6 = EN.sim_enemy_attack_event(s4, 9)
    chk("★ 9 号被打（不在预计算名单）⇒ 记缺口 `deferred.unprobed`、不猜效果",
        any("deferred.unprobed" in g for g in s6.gaps) and stats(s6, 1, 2, 3) == [(2, 3)] * 3, str(s6.gaps))

    # ---- ③ 场上没有己方单位：没有授予、事件不炸、不凭空加数值 ----
    se = mk(own=())
    se.hand[ECH].eff = {}
    echelon_tables(se, ())
    se1 = EN.sim_order(se, ECH)
    se2 = EN.sim_enemy_attack_event(se1, 21)
    chk("无己方单位 ⇒ 无授予；敌方单位被打（已空跑过，守卫挡掉）⇒ 什么都不发生、也不记缺口",
        not se1.grants and stats(se2, 21) == [(2, 3)] and not [g for g in se2.gaps if "deferred" in g], str(se2.gaps))
    s7 = mk(own=(1,))
    echelon_tables(s7, (1,))
    s7 = EN.sim_enemy_attack_event(EN.sim_order(s7, ECH), 1)
    chk("只有 1 个己方单位被打 ⇒ 没有'别的单位'可加成（桶空）⇒ 不变", stats(s7, 1) == [(2, 3)])

    # ---- ④ 通过 sim_attack 触发：我方打敌方单位 ⇒ 被打的是敌方牌 ⇒ ECHELON（我方牌）守卫挡掉，我方单位不变 ----
    s8 = EN.sim_order(s0, ECH)
    s8.units[1].row = "frontline"
    s9 = EN.sim_attack(s8, 1, 21)
    chk("我方单位打敌方单位 ⇒ `OnAfterOtherCardAttacks` 在事件点触发（走了登记表），但敌方被打 ⇒ ECHELON 没后果、也没缺口",
        stats(s9, 2, 3) == [(2, 3)] * 2 and not [g for g in s9.gaps if "deferred" in g], str(s9.gaps))

    # ---- ⑤ 评估：缺省权重 0 ⇒ 不估值（不编造）；给了预期被打次数才 > 0；只有 1 个单位时仍为 0 ----
    chk("缺省 `armed_enemy_hits=0` ⇒ `armed_value==0`（不编造次数）", armed_value(s1, W) == 0.0)
    w1 = dict(W, armed_enemy_hits=1.0)
    v = armed_value(s1, w1)
    exp = (W["w_atk"] * 1 + W["w_def"] * 1) * 2                  # 每个主体各让"别的 2 个"各 +1/+1
    chk("给 `armed_enemy_hits=1` ⇒ 值 = 平均每次被打让其余单位的身体增益之和（3 个单位 ⇒ 2×(w_atk+w_def)）",
        abs(v - exp) < 1e-9, "v=%s exp=%s" % (v, exp))
    chk("★ 只在**有别的单位可加成**时才有价值：单个单位 ⇒ 0；没打出（无 `armed`）⇒ 0",
        armed_value(s7, w1) == 0.0 and armed_value(s0, w1) == 0.0)
    chk("`evaluate` 把它计入：权重 1 的终态比权重 0 高恰好 exp",
        abs(evaluate(s1, w1) - evaluate(s1, W) - exp) < 1e-9)

    # ---- ⑥ 回合开始族（OnStartOfTurn：天气/战备类的延迟结算）：登记后在 `sim_turn_start` 触发 ----
    st0 = mk(own=(1,))
    st0.hand.clear()
    st0.hand[60] = S.H(60, "START CARD", 1, "order", eff={})
    st0.event_fx["armed_spec"] = {60: {"hooks": ("OnStartOfTurn",), "grants": (), "probed": set(), "probed_hooks": {"OnStartOfTurn"}}}
    st0.event_fx["armed"] = {(60, "OnStartOfTurn", None): {"buff_ids": {1: [2, 0]}}}
    st1 = EN.sim_order(st0, 60)
    chk("回合开始族：打出时布防（不立刻生效）", [(a.hook) for a in st1.armed] == ["OnStartOfTurn"] and st1.units[1].atk == 2)
    st2 = EN.sim_turn_start(st1, turn_number=3)
    chk("★ `sim_turn_start` ⇒ 触发 `OnStartOfTurn` 登记的后果（1 号 +2 攻）", st2.units[1].atk == 4, str(st2.units[1].atk))
    st_o = st1.copy()
    st_o.armed[0].side = OPP
    chk("对方座位的回合钩子不在我方回合开始触发", EN.sim_turn_start(st_o, turn_number=3).units[1].atk == 2)

    # ---- ⑦ 回合结束族（OnEndOfTurn）+ 一次性 ----
    se0 = mk(own=(1,))
    se0.hand.clear()
    se0.hand[61] = S.H(61, "END CARD", 1, "order", eff={})
    se0.event_fx["armed_spec"] = {61: {"hooks": ("OnEndOfTurn",), "one_shot": ("OnEndOfTurn",), "grants": (),
                                       "probed": set(), "probed_hooks": {"OnEndOfTurn"}}}
    se0.event_fx["armed"] = {(61, "OnEndOfTurn", None): {"damage_hq": 3}}
    se1 = EN.sim_order(se0, 61)
    se2 = EN.sim_turn_end(se1)
    chk("★ `sim_turn_end` ⇒ 触发 `OnEndOfTurn`（对敌方总部 3 点），一次性的触发后摘掉",
        se2.hq[OPP] == 17 and not se2.armed, "hq=%s armed=%s" % (se2.hq, se2.armed))
    chk("`sim_turn_end` 幂等：再调不会重复结算", EN.sim_turn_end(se2).hq[OPP] == 17)
    se3 = se1.copy()
    se3.event_fx["armed_spec"][61] = dict(se3.event_fx["armed_spec"][61], one_shot=())
    se3.armed[0].one_shot = False
    chk("非一次性（常驻）⇒ 触发后仍在登记表里", len(EN.sim_turn_end(se3).armed) == 1)

    # ---- ⑧ 诚实：没有 sim 事件点的钩子 ⇒ 布防时记缺口，不登记、不假装生效 ----
    su = mk(own=(1,))
    su.hand.clear()
    su.hand[62] = S.H(62, "OTHER CARD", 1, "order", eff={})
    su.event_fx["armed_spec"] = {62: {"hooks": ("OnOtherCardDestroyed", ECH_HOOK), "grants": (), "probed": set(), "probed_hooks": set()}}
    su1 = EN.sim_order(su, 62)
    chk("未接的钩子（OnOtherCardDestroyed）⇒ 缺口 `deferred.unmodeled` 点名；已接的（OnAfterOtherCardAttacks）照常登记",
        any("deferred.unmodeled" in g and "OnOtherCardDestroyed" in g for g in su1.gaps)
        and [a.hook for a in su1.armed] == [ECH_HOOK], "gaps=%s armed=%s" % (su1.gaps, su1.armed))

    # ---- ⑨ A/B/C 对账：`CustomAbilityAdd` 三条路同账（计数 + giver）----
    def fresh():
        return mk(own=(1, 2))

    a = fresh()
    r = Recorder(0, 0, None)
    r.cur_stats = {}
    r.records.append({"verb": "CustomAbilityAdd", "args": ["trigger", 1, ECH, False, False], "tainted": False})
    r.records.append({"verb": "CustomAbilityAdd", "args": ["trigger", 2, ECH, False, False], "tainted": False})
    EN._apply_eff(a, dict(to_effects(r, my_side=1), target=None), None, src=ECH)
    b = fresh()
    h = SC.native_hooks(b, my_side=ME, gates={1: {"buffable": True}, 2: {"buffable": True}})
    h["CustomAbilityAdd"](None, None, None, ["trigger", 1, ECH, False, False], None)
    h["CustomAbilityAdd"](None, None, None, ["trigger", 2, ECH, False, False], None)
    c = fresh()
    rr = apply_calls(c, h["__ctx__"].applied)
    sa, sb, sc = SC._state_snapshot(a), SC._state_snapshot(b), SC._state_snapshot(c)
    chk("★ A（字典路）= B（直跑）= C（重放）：`grants` 与每单位 `ab` 逐项一致",
        sa == sb == sc and sa["grants"] == ((str((ECH, "trigger")), (1, 2)),),
        "A-B=%s B-C=%s gaps=%s" % ({k: (sa.get(k), sb.get(k)) for k in sa if sa.get(k) != sb.get(k)},
                                   {k: (sb.get(k), sc.get(k)) for k in sb if sb.get(k) != sc.get(k)}, rr.gaps))
    rg = Recorder(0, 0, None)
    rg.cur_stats, rg.ptr_ids = {7: {"buffable": False}}, {7: 1}
    rg.records.append({"verb": "CustomAbilityAdd", "args": ["trigger", 1, ECH], "tainted": False})
    chk("门 `CanCardBeBuffed` 为假 ⇒ 字典路不授予（直跑 sink 同门，见 test_scripts_direct）",
        not to_effects(rg, my_side=1).get("ability_grants"))

    # ---- ⑩ rule 接线：带延迟钩子的指令**不再**记成 `vm(空)`，而是 `vm(延迟)` + 缺口 ----
    import player.rule as R
    import player.deferred_fx as DFX
    from test_marker_plumbing import mk_pol, _C
    pol = mk_pol()
    pol._vm_eff = lambda *a, **k: {"_src": "vm"}                 # VM 跑完了、OnPlayedFromHand 没产出（ECHELON 的实机现象）
    orig = DFX.hooks_of
    try:
        DFX.hooks_of = lambda rule, c_: [ECH_HOOK]
        e = pol._hand_eff(_C("ECHELON", 1))
        chk("★ ECHELON：`eff_src` 不再是 `vm(空)`（带 `延迟`），且不带 `_vm_empty`",
            "延迟" in pol.eff_src["ECHELON"] and pol.eff_src["ECHELON"] != "vm(空)" and not e.get("_vm_empty"), repr(pol.eff_src))
        chk("对方事件钩子 + `armed_enemy_hits=0` ⇒ 缺口 `#deferred_val`（后果已建模但未估值）", "ECHELON#deferred_val" in pol.gaps, str(pol.gaps))
        pol2 = mk_pol()
        pol2._vm_eff = lambda *a, **k: {"_src": "vm"}
        DFX.hooks_of = lambda rule, c_: ["OnOtherCardDestroyed"]
        pol2._hand_eff(_C("X", 2))
        chk("没有 sim 事件点的钩子 ⇒ 缺口 `#deferred`（点名钩子）", "X#deferred" in pol2.gaps and "OnOtherCardDestroyed" in pol2.gaps["X#deferred"], str(pol2.gaps))
        pol3 = mk_pol()
        pol3._vm_eff = lambda *a, **k: {"_src": "vm"}
        DFX.hooks_of = lambda rule, c_: []
        pol3._hand_eff(_C("PLAIN ORDER", 3))
        chk("没有留下来的钩子的指令 ⇒ 仍是已知的空 `vm(空)`（别把真的空也说成缺口）", pol3.eff_src["PLAIN ORDER"] == "vm(空)" and not pol3.gaps, str(pol3.eff_src) + str(pol3.gaps))
    finally:
        DFX.hooks_of = orig

    # ---- ⑪ rule 侧预计算（`player/deferred_fx`）：名单 / 空跑实参 / 授予账喂给 VM / 预算 / 停在原语 ⇒ 缺口 ----
    import types
    probes = []

    class _O:
        def __init__(s_, cid, side, field=True):
            s_.CardID, s_.side, s_._f = cid, side, field

        def IsLocatedOnBoard(s_):
            return True

        def IsFieldUnit(s_):
            return s_._f

    def _card(cid, side, name="U", field=True):
        return types.SimpleNamespace(obj=_O(cid, side, field), raw={"ptr": 0x1000 + cid}, name=name)

    st = types.SimpleNamespace(cards=[_card(ECH, ME, "ECHELON", False), _card(1, ME), _card(2, ME), _card(21, OPP)],
                               my_side=ME, other_side=OPP, turn=7)
    sim = mk(own=(1, 2))
    echelon_tables(sim, (1, 2))
    sim.event_fx.pop("armed_spec")
    sim.event_fx.pop("armed")

    def _probe(km, st_, ptr, hook, args, ov, vo, rh, hq_o, hq_e, one_s):
        probes.append((hook, args["defenderCard"], vo, ov))
        d = args["defenderCard"] - 0x1000
        return {"stopped": None, "eff": {"buff_ids": {i: [1, 1] for i in (1, 2) if i != d}} if d in (1, 2) else {}, "gaps": []}

    rule = types.SimpleNamespace(P=dict(R.PARAMS, use_vm=True), gaps={}, fx_meta={}, _W=dict(W),
                                 _km=lambda: object(), is_unit=lambda c_: False, _hq_ptrs=lambda side_: (),
                                 _armed_probe=_probe)
    orig_prof = None
    try:
        import semantics.cardprobe as CP
        orig_prof = CP.profile
        CP.profile = lambda km, c_: {"ok": True, "handlers": ["OnPlayedFromHand", ECH_HOOK, "CanPlayFromHand"]}
        spec = DFX.spec_fx(rule, st, sim)
        chk("`spec_fx`：只收**留下来的**钩子（OnPlayedFromHand/CanPlayFromHand 不算），授予账取自打出效果 `ability_grants`",
            spec[ECH]["hooks"] == (ECH_HOOK,) and spec[ECH]["grants"] == (("trigger", 1), ("trigger", 2)), str(spec))
        sim.event_fx["armed_spec"] = spec
        fx = DFX.probe_fx(rule, st, sim)
        chk("`probe_fx`：对场上每个单位各空跑一次（我方 2 + 敌方 1），桶键 = (来源牌, 钩子, 主体)",
            sorted(k for k in fx) == [(ECH, ECH_HOOK, 1), (ECH, ECH_HOOK, 2)] and len(probes) == 3, str(sorted(fx)) + " probes=%d" % len(probes))
        chk("授予账经 `view_overrides` 喂给 VM（被授予单位的 `received_abilities` 带 giver=来源牌 id）",
            all(vo[0x1000 + 1]["received_abilities"][0]["givers"] == [ECH] for _h, _d, vo, _o in probes), str(probes[0][2]))
        chk("来源牌按'已打出'空跑：`side`=我方座位、`enterPlayOnTurn`=本回合",
            probes[0][3] == {(0x1000 + ECH, "side"): 1, (0x1000 + ECH, "enterPlayOnTurn"): 7}, str(probes[0][3]))
        chk("预计算名单 `probed` = 空跑跑完的主体（敌方单位 21 也在 ⇒ 触发时是'空跑过、无后果'，不是缺口）",
            spec[ECH]["probed"] == {1, 2, 21}, str(spec[ECH]["probed"]))
        # 预算耗尽 ⇒ 剩下的主体不进名单（触发时记缺口）；VM 停在原语 ⇒ 缺口 + 不进名单
        for sp in spec.values():
            sp["probed"].clear()
        rule.P["armed_probe_s"] = -1.0
        DFX.probe_fx(rule, st, sim)
        chk("预算耗尽 ⇒ 没空跑的主体不进 `probed`（sim 触发时记 `deferred.unprobed`，不猜）",
            not spec[ECH]["probed"] and rule.fx_meta["armed"]["skipped"] == 3, str(rule.fx_meta))
        rule.P["armed_probe_s"] = 3.0
        rule2 = types.SimpleNamespace(**dict(rule.__dict__, gaps={}, fx_meta={},
                                             _armed_probe=lambda *a, **k: {"stopped": "Unimplemented @0x2C Context: 'Foo'", "eff": {}, "gaps": []}))
        spec[ECH]["probed"].clear()
        out = DFX.probe_fx(rule2, st, sim)
        chk("VM 停在未实现原语 ⇒ 无桶、不进名单、`rule.gaps['ECHELON'#deferred]` 点名原因",
            not out and not spec[ECH]["probed"] and "VM 停在" in rule2.gaps.get("ECHELON#deferred", ""),
            str(rule2.gaps))
    finally:
        if orig_prof is not None:
            CP.profile = orig_prof

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
