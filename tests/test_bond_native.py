#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协力（Bond）原生移植（2026-10-06）：`engine/natives/bond.py` + sim 打出路径 + `RemoveBond` sink/重放。

钉住的原版规则（BP 1.60 导出；行号见 `engine/natives/bond.py` 文件头）：
  * 检查在**打出时**（`CardPlayedFromHand`），不在回合开始；`HasBond ∧ ¬activeBondFactions.Contains(faction)` ⇒
    `ApplyFatigueDamage(side, fromBond=true)` = 当前疲劳计数的伤害、计数 +1（与空库抽牌共用计数）；
  * `activeBondFactions` 只在**回合开始**重算（回合中新部署的单位不算）；
  * `RemoveBond` 写 `bond_removed` ⇒ `HasBond` 变假 ⇒ 不扣；`GiveBond` 反之；
  * 总部被打爆 ⇒ 牌自己的效果不再执行。
每个判据都给两个会产生不同结论的输入（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from engine import state as S                                        # noqa: E402
from engine import calls as CALLS                                    # noqa: E402
from engine import scripts as SC                                     # noqa: E402
from engine.natives import bond as B                                 # noqa: E402
import sim.engine as E                                               # noqa: E402
from kardsmem import gamemodel as GM                                 # noqa: E402
import _cards as CC                                                  # noqa: E402

fails = 0
ME, OPP = 1, 2
USA, BRI = int(GM.EFaction.USA), int(GM.EFaction.Britain)


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(hand, units=None, fatigue=3, bond_factions=None, hq=20):
    s = S.Sim(dict(units or {}), {ME: hq, OPP: 20}, 10.0, {h.id: h for h in hand}, my_side=ME, fatigue=fatigue)
    s.bond_factions = bond_factions
    return s


def bond_order(i=1, faction=USA, **kw):
    return S.H(i, "BOND ORDER", 2, "order", faction=faction, bond=True, **kw)


def main():
    # ① 打出时检查：没有同国单位 ⇒ 一次疲劳（伤害 = 当前计数 3，计数 → 4）
    s = mk([bond_order()], bond_factions=set())
    a = E.sim_order(s, 1)
    chk("未满足 ⇒ 总部 -3（当前疲劳计数）、计数 3→4", a.hq[ME] == 17 and a.fatigue == 4, "hq=%s fat=%s" % (a.hq[ME], a.fatigue))
    chk("只扣一次（不是每个回合开始/每张手牌）", s.hq[ME] == 20 and s.fatigue == 3, "原状态不被污染")
    # 对照输入：国家在集合里 ⇒ 不扣
    b = E.sim_order(mk([bond_order()], bond_factions={USA}), 1)
    chk("满足（faction ∈ 集合）⇒ 不扣、计数不动", b.hq[ME] == 20 and b.fatigue == 3, "hq=%s fat=%s" % (b.hq[ME], b.fatigue))
    # 对照输入：集合里是别国 ⇒ 仍扣
    c = E.sim_order(mk([bond_order()], bond_factions={BRI}), 1)
    chk("集合里只有别国 ⇒ 仍扣", c.hq[ME] == 17)
    # 非协力牌 ⇒ 不扣
    d = E.sim_order(mk([S.H(1, "PLAIN", 2, "order", faction=USA)], bond_factions=set()), 1)
    chk("不带协力的牌 ⇒ 不扣", d.hq[ME] == 20 and d.fatigue == 3)
    # 集合未知 ⇒ 不下结论（不扣、记缺口）
    e = E.sim_order(mk([bond_order()], bond_factions=None), 1)
    chk("activeBondFactions 未知 ⇒ 不扣 + 缺口（不编）", e.hq[ME] == 20 and any("bond" in g for g in e.gaps), str(e.gaps))

    # ② 部署路径（deploy）同样原生走检查
    unit = S.H(2, "BOND TANK", 3, "tank", 3, 3, faction=USA, bond=True)
    s2 = mk([unit], bond_factions=set())
    r = E.apply(s2, E.A("deploy", 2))
    chk("部署协力单位且未满足 ⇒ 总部 -3", r.hq[ME] == 17 and any(u.faction == USA for u in r.units.values()),
        "hq=%s" % r.hq[ME])
    r2 = E.apply(mk([unit], bond_factions={USA}), E.A("deploy", 2))
    chk("部署协力单位且满足 ⇒ 不扣", r2.hq[ME] == 20)

    # ③ 集合只在回合开始更新：回合中部署同国单位 ⇒ 集合不变、后面的协力牌仍扣
    own = S.U(10, ME, "frontline", 2, 2, 2, "infantry", faction=USA)
    s3 = mk([S.H(5, "USA UNIT", 1, "infantry", 1, 1, faction=USA), bond_order(1)], units={10: own}, bond_factions=set())
    s3 = E.apply(s3, E.A("deploy", 5))
    chk("回合中部署同国单位 ⇒ 集合仍是回合开始时的（空）", s3.bond_factions == set(), str(s3.bond_factions))
    s3b = E.sim_order(s3, 1)
    chk("…所以随后打协力牌仍扣", s3b.hq[ME] == 17)
    s3t = E.sim_turn_start(s3, 4)
    chk("sim_turn_start 重算：场上有 USA 单位 ⇒ 集合 {USA}", s3t.bond_factions == {USA}, str(s3t.bond_factions))
    # ↑ 回合开始会抽牌/补给，这里只核对集合；打出的对照单独做（避免被抽牌链干扰手牌）
    s3u = S.Sim({10: own}, {ME: 20, OPP: 20}, 5.0, {1: bond_order(1)}, my_side=ME, fatigue=3)
    s3u.bond_factions = set()
    B.set_active_bonds_at_start_of_turn(s3u, ME)
    chk("原生重算后打协力牌不扣", E.sim_order(s3u, 1).hq[ME] == 20)
    # 对照：对方单位 / 未揭示隐蔽单位不算
    foe = S.U(11, OPP, "frontline", 2, 2, 2, "infantry", faction=BRI)
    cov = S.U(12, ME, "back", 1, 1, 1, "infantry", kw=("covert",), faction=BRI)
    s3v = S.Sim({10: own, 11: foe, 12: cov}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    B.set_active_bonds_at_start_of_turn(s3v, ME)
    chk("只收己方、非未揭示隐蔽的单位", s3v.bond_factions == {USA}, str(s3v.bond_factions))
    B.set_active_bonds_at_start_of_turn(s3v, OPP)
    chk("集合属于行动方（side=OPP ⇒ {Britain}）", s3v.bond_factions == {BRI}, str(s3v.bond_factions))
    nof = S.Sim({10: S.U(13, ME, "back", 1, 1, 1, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    B.set_active_bonds_at_start_of_turn(nof, ME)
    chk("单位 faction 读不出 ⇒ 集合置 None（未知）+ 缺口", nof.bond_factions is None and nof.gaps, str(nof.gaps))

    # ④ RemoveBond / GiveBond
    s4 = mk([bond_order(1), bond_order(2, faction=BRI), S.H(3, "PLAIN", 1, "order", faction=USA)], bond_factions=set())
    t = S.Sim.copy(s4)
    done = B.remove_bond_from_hand(t)
    chk("remove_bond_from_hand 只动带协力的牌", sorted(done) == [1, 2], str(done))
    chk("移除后 HasBond 变假", not B.has_bond(t.hand[1]) and t.hand[1].bond is True and t.hand[1].bond_removed)
    chk("写时复制：原状态/兄弟分支的手牌不受影响", B.has_bond(s4.hand[1]) and not s4.hand[1].bond_removed)
    ta = E.sim_order(t, 1)
    chk("移除协力后打出不扣", ta.hq[ME] == 20 and ta.fatigue == 3)
    chk("（对照）没移除的同一张牌仍扣", E.sim_order(s4, 1).hq[ME] == 17)
    chk("remove_bond 幂等", B.remove_bond(t, 1) is False)
    chk("目标不在手牌 ⇒ False", B.remove_bond(t, 99) is False)
    chk("give_bond 把移除的协力还回来", B.give_bond(t, 1) is True and B.has_bond(t.hand[1]))
    chk("give_bond 对已有协力的牌不动", B.give_bond(t, 1) is False)
    chk("give_bond 给普通牌 ⇒ 带上协力", B.give_bond(t, 3) is True and B.has_bond(t.hand[3]))

    # ⑤ 总部被打爆 ⇒ 牌自己的效果不再执行
    s5 = mk([S.H(1, "HEAL ORDER", 2, "order", faction=USA, bond=True, eff={"heal_hq": 5})], fatigue=25, bond_factions=set())
    r5 = E.sim_order(s5, 1)
    chk("疲劳打爆总部 ⇒ 牌的回复效果不执行（原版 :18693-18700 立刻 return）", r5.hq[ME] == -5, "hq=%s" % r5.hq[ME])
    r5b = E.sim_order(mk([S.H(1, "HEAL ORDER", 2, "order", faction=USA, bond=True, eff={"heal_hq": 5})], fatigue=25,
                         bond_factions={USA}), 1)
    chk("（对照）满足协力时回复照常", r5b.hq[ME] == 25)

    # ⑥ 读侧：从快照牌算 activeBondFactions（原生）
    me_u = CC.mk_card(1, CC.ME, "frontline", "infantry"); me_u.obj.faction = GM.EFaction.USA
    me_cov = CC.mk_card(2, CC.ME, "back", "infantry"); me_cov.obj.faction = GM.EFaction.Japan; me_cov.obj.hasCovert = True
    me_cov.obj.isRevealed = False
    me_cov2 = CC.mk_card(3, CC.ME, "back", "infantry"); me_cov2.obj.faction = GM.EFaction.Italy; me_cov2.obj.hasCovert = True
    me_cov2.obj.isRevealed = True
    me_hand = CC.mk_card(4, CC.ME, "hand", "infantry"); me_hand.obj.faction = GM.EFaction.France
    me_hq = CC.mk_card(5, CC.ME, "hq", "infantry"); me_hq.obj.faction = GM.EFaction.Poland
    foe_u = CC.mk_card(6, CC.OPP, "frontline", "infantry"); foe_u.obj.faction = GM.EFaction.Germany
    cards = [me_u, me_cov, me_cov2, me_hand, me_hq, foe_u]
    got = B.active_bond_factions(cards, CC.ME)
    chk("快照读侧：己方在场单位 + 已揭示隐蔽；手牌/总部/对方/未揭示隐蔽不算", got == {int(GM.EFaction.USA), int(GM.EFaction.Italy)}, str(got))
    chk("换边得到另一个集合", B.active_bond_factions(cards, CC.OPP) == {int(GM.EFaction.Germany)})

    # ⑦ 适配器把 bond/faction/集合喂进 Sim
    from engine.adapter import from_cards
    hb = CC.mk_card(7, CC.ME, "hand", "order"); hb.obj.faction = GM.EFaction.USA
    hb_removed = CC.mk_card(8, CC.ME, "hand", "order"); hb_removed.obj.faction = GM.EFaction.USA
    hb_removed.raw = dict(hb_removed.raw, received_abilities=[{"ability": "bond_removed", "givers": [1]}])
    hq_me, hq_opp = CC.mk_card(20, CC.ME, "hq", "order", defense=20), CC.mk_card(21, CC.OPP, "hq", "order", defense=20)
    sim = from_cards([hb, hb_removed, me_u, hq_me, hq_opp], lambda c: set(), my_side=CC.ME,
                     bond_of=lambda c: True, bond_factions={int(GM.EFaction.USA)}, fatigue=2)
    chk("适配器：H.bond/faction、集合", sim.hand[7].bond and sim.hand[7].faction == USA
        and sim.bond_factions == {USA} and sim.units[1].faction == USA)
    chk("适配器：快照里带 bond_removed ⇒ H.bond_removed（HasBond 假）", sim.hand[8].bond_removed and not B.has_bond(sim.hand[8])
        and not sim.hand[7].bond_removed)
    sim0 = from_cards([hb, hq_me, hq_opp], lambda c: set(), my_side=CC.ME, fatigue=2)
    chk("适配器：没给 bond_of/集合 ⇒ 默认非协力、集合未知", not sim0.hand[7].bond and sim0.bond_factions is None)
    chk("Sim.copy 复制集合（互不影响）", (lambda c: (c.bond_factions.add(99), 99 not in sim.bond_factions)[1])(sim.copy()))

    # ⑧ RemoveBond 直跑 sink（RATIONING 分支 0 的叶子）+ 类型化调用重放
    st = mk([bond_order(1), S.H(3, "PLAIN", 1, "order", faction=USA)], bond_factions=set())
    hooks = SC.native_hooks(st, my_side=ME)
    hooks["RemoveBond"](None, None, None, [1, 999, None], None)
    ctx = hooks["__ctx__"]
    chk("RemoveBond sink：手牌写 bond_removed、记 applied", st.hand[1].bond_removed and ("remove_bond", 1) in ctx.applied, str(ctx.applied))
    hooks["RemoveBond"](None, None, None, [1, 999, None], None)
    chk("RemoveBond sink 幂等（不重复记）", ctx.applied.count(("remove_bond", 1)) == 1)
    hooks["RemoveBond"](None, None, None, [42, 999, None], None)
    chk("RemoveBond sink：不在手牌 ⇒ 缺口、不改状态", any("42" in g for g in ctx.gaps), str(ctx.gaps))
    fresh = mk([bond_order(1)], bond_factions=set())
    res = CALLS.apply_calls(fresh, [("remove_bond", 1)])
    chk("类型化调用 remove_bond 重放 == 直跑结果", fresh.hand[1].bond_removed and not res.gaps and ("remove_bond", 1) in res.applied,
        str(res.gaps))
    res2 = CALLS.apply_calls(mk([]), [("remove_bond", 7)])
    chk("重放：目标不在手牌 ⇒ 缺口", bool(res2.gaps))
    chk("记下的 applied 经重放 == 直跑（对账）", [c for c in ctx.applied if c[0] == "remove_bond"] == [("remove_bond", 1)])

    # ⑨ 旧字典键已删（行为迁到原生一步）
    chk("效果字典里不再有 bond_fatigue 键（sim/engine 不再消费）",
        "bond_fatigue" not in open(os.path.join(ROOT, "sim", "engine.py"), encoding="utf-8").read().replace(
            "旧键 `bond_fatigue` 已删", ""))

    # ⑩ 出处核对：BP 导出件里真有我写的东西（发布导出里没有 reverse-data 就跳过）
    base = os.path.normpath(os.path.join(ROOT, "..", "reverse-data", "exports-1.60.27292.launcher-only-decompiled-BP",
                                         "kards", "Content"))
    cf = os.path.join(base, "Blueprints", "Cards", "BP_CardFunctions.cpp")
    if not os.path.exists(cf):
        print("  [SKIP] 出处核对：找不到 BP 导出件（发布导出里没有 reverse-data，属正常）")
    else:
        def lines(p):
            return open(p, encoding="utf-8", errors="replace").read().splitlines()
        L = lines(cf)

        def has(path_lines, n, *needles):
            seg = "\n".join(path_lines[n - 1:n + 3])
            return all(x in seg for x in needles)
        chk("出处 BP_CardFunctions :18682 HasBond 调用点", has(L, 18682, "HasBond"))
        chk("出处 :18684 activeBondFactions.Contains", has(L, 18684, "activeBondFactions", "faction"))
        chk("出处 :18693 ApplyFatigueDamage(side, true)", has(L, 18693, "ApplyFatigueDamage", "true"))
        chk("出处 :20185 ApplyFatigueDamage 定义", has(L, 20185, "ApplyFatigueDamage", "fromBond"))
        chk("出处 :12979 空库抽牌 fromBond=false", has(L, 12979, "ApplyFatigueDamage", "false"))
        chk("出处 :23020 RemoveBond 定义 + :23022 bond_removed", has(L, 23020, "RemoveBond") and has(L, 23022, "bond_removed"))
        chk("出处 :22908 GiveBond 定义 + :22971 清 bond_removed", has(L, 22908, "GiveBond") and has(L, 22971, "bond_removed"))
        gs = lines(os.path.join(base, "BP_GameState_Battle.cpp"))
        chk("出处 BP_GameState_Battle :2529 SetActiveBondsAtStartOfTurn + :2576 Set_Add",
            has(gs, 2529, "SetActiveBondsAtStartOfTurn") and has(gs, 2576, "Set_Add", "activeBondFactions"))
        lg = lines(os.path.join(base, "Blueprints", "Logic", "BP_Logic.cpp"))
        chk("出处 BP_Logic :10001 StartTurnBySide 里重算", has(lg, 10001, "SetActiveBondsAtStartOfTurn"))
        # 回合开始流程里没有协力检查（更正 CLAUDE.md 旧说法）：全导出 fromBond=true 只有一处
        import glob
        trues = []
        for p in glob.glob(os.path.join(base, "**", "*.cpp"), recursive=True):
            try:
                for i, ln in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                    if "ApplyFatigueDamage(" in ln and "public void" not in ln and ", true," in ln:
                        trues.append((os.path.basename(p), i))
            except OSError:
                pass
        chk("全导出里 ApplyFatigueDamage(…, true, …) 只有一处调用点（CardPlayedFromHand）", trues == [("BP_CardFunctions.cpp", 18693)],
            str(trues))
        rat = os.path.join(base, "Blueprints", "Cards", "USA", "Homefront", "events", "card_event_rationing.cpp")
        txt = open(rat, encoding="utf-8", errors="replace").read()
        chk("RATIONING：遍历手牌 HasBond → RemoveBond；另一支 ChangeDefense(+4)",
            "RemoveBond(" in txt and "HasBond(" in txt and "GetCardsInHandBySide" in txt and "ChangeDefense(" in txt and ", 4," in txt)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
