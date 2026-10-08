#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打敌方总部的保真（2026-10-07 00:04 局：搜索连续三步预测"致命"、总部连吃三击没死）。

BP `CalculateDamageDealt`（`BP_CardFunctions.cpp:14809+`）对**任何**受击牌（含 HQ 地点牌）：先判 `getHasImmune`
（免疫 ⇒ 伤害 0），再扣 `getTotalHeavyArmor`（[0,3]）。以前 sim/适配器只读总部防御 ⇒ 总部带重甲/免疫时
sim 高估伤害、把"打不死"的序列评成致命（+975）。这里钉：
  ① sim_attack 打总部：重甲先扣、免疫为 0；② 搜索：有重甲时同样的攻击者凑不够致命，没重甲时致命；
  ③ 适配器把总部的重甲/免疫读进 Sim；④ `_probe_hq` 把致命序列逐步的 sim 总部防御/攻击值/对账字段写进 probe。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _cards import MY_SIDE, mk_card                              # noqa: E402
from engine import state as S                                    # noqa: E402
from engine.adapter import from_cards                            # noqa: E402
import sim.engine as E                                           # noqa: E402
import policy.search as SE                                       # noqa: E402
from evaluation.value import W                                   # noqa: E402
from kardsmem.gamemodel import other_side                        # noqa: E402

ME, OPP = 1, 2
bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))


def mk(hq=9, armor=0, immune=False, atks=(3, 3, 3)):
    units = {i + 1: S.U(i + 1, ME, "frontline", a, 3, 1, "infantry") for i, a in enumerate(atks)}
    sim = S.Sim(units, {ME: 20, OPP: hq}, 6, {}, my_side=ME)
    sim.hq_armor, sim.hq_immune = {OPP: armor}, {OPP: immune}
    return sim


def main():
    s = E.sim_attack(mk(armor=0), 1, None, hq=True)
    chk("无重甲：3 攻打总部 9 ⇒ 6", s.hq[OPP] == 6, str(s.hq))
    s = E.sim_attack(mk(armor=1), 1, None, hq=True)
    chk("总部重甲 1：3 攻只打出 2 ⇒ 7", s.hq[OPP] == 7, str(s.hq))
    s = E.sim_attack(mk(armor=3), 1, None, hq=True)
    chk("总部重甲 3：3 攻打出 0（不为负）", s.hq[OPP] == 9, str(s.hq))
    s = E.sim_attack(mk(immune=True), 1, None, hq=True)
    chk("总部免疫 ⇒ 伤害 0", s.hq[OPP] == 9, str(s.hq))

    # ② 搜索：三个 3 攻对 9 血总部——无重甲刚好致命；带 1 重甲就不致命（00:04 局的形态）
    r0 = SE.search(mk(armor=0), W, 3, 8, 12, 6)
    r1 = SE.search(mk(armor=1), W, 3, 8, 12, 6)
    chk("无重甲：搜索给出致命序列（分 > lethal/2）", r0 and r0[0][0] > W["lethal"] / 2, str(r0[0][0] if r0 else None))
    chk("总部重甲 1：同样三个攻击者**不**再被评成致命", (not r1) or r1[0][0] < W["lethal"] / 2,
        str(r1[0][0] if r1 else None))

    # ③ 适配器读总部重甲/免疫
    me_hq = mk_card(90, "local", "hq", "location", 0, 20, name="HQ_ME")
    en_hq = mk_card(91, "enemy", "hq", "location", 0, 9, name="ALEXANDRIA")
    en_hq.obj.heavyArmor, en_hq.obj.heavyArmorBuff, en_hq.obj.isImmune = 1, 1, False
    u = mk_card(1, "local", "frontline", "infantry", 3, 3, 2, 1, "U1")
    sim = from_cards([me_hq, en_hq, u], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    opp = other_side(MY_SIDE)
    chk("适配器：敌方总部重甲 1+1 ⇒ 2", sim.hq_armor.get(opp) == 2, str(sim.hq_armor))
    chk("适配器：敌方总部防御照常读", sim.hq.get(opp) == 9, str(sim.hq))
    c = sim.copy()
    chk("Sim.copy 带着总部重甲/免疫", c.hq_armor == sim.hq_armor and c.hq_immune == sim.hq_immune)
    en_hq.obj.isImmune = True
    sim = from_cards([me_hq, en_hq, u], lambda c: (), actionable=lambda c: True, kredits=5, my_side=MY_SIDE)
    chk("适配器：免疫读进来", bool(sim.hq_immune.get(opp)))

    # ④ probe：致命序列逐步的总部防御 + 攻击值 + 对账
    from player.rule import RuleV2
    rule = object.__new__(RuleV2)
    rule.P = {"lethal": W["lethal"]}
    rule.probe = {}
    rule._st = type("St", (), {"turn": 18})()
    sm = mk(armor=0)
    seqs = SE.search(sm, W, 3, 8, 12, 6)
    rule._hq_expect = {"turn": 18, "act": "attack X → HQ", "before": 9, "pred": 4}
    rule._probe_hq(sm, seqs, None)
    pr = rule.probe
    chk("probe.hq_det 有 sim 值", pr["hq_det"]["def"] == 9 and pr["hq_det"]["armor"] == 0, str(pr.get("hq_det")))
    chk("probe.atk_hq 列出每个能打总部的单位与攻击值", len(pr["atk_hq"]) == 3 and pr["atk_hq"][0][1] == 3, str(pr.get("atk_hq")))
    ch = pr.get("lethal_chain")
    chk("probe.lethal_chain 逐步记 sim 总部防御（最后 ≤ 0）", bool(ch) and ch[0]["steps"][-1][1] <= 0, str(ch))
    chk("probe.hq_check 记预测 vs 实读的差", pr["hq_check"]["hq_pred_after"] == 4 and pr["hq_check"]["hq_real_now"] == 9
        and pr["hq_check"]["shortfall"] == 5, str(pr.get("hq_check")))
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
