#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**免疫单位不吃伤害**（P3 ⑦，2026-10-04）—— 用户当场纠正后按原版三处证据实现。

原版三处一致（`BP_CardFunctions.cpp`）：
  * `CalculateDamageDealt` `:14823-14836`：开头就查 `_damageRecieverCard->getHasImmune()`，
    真 ⇒ `damage = 0; doesDamageRecieverDie = false; wasShockAttack = false; return`
    —— **吃 0 伤害、也不会死**；
  * `ExecuteOnDealDamageAddDamageAfterCalc` `:14568-14575`：`toCard.getHasImmune()` ⇒ `finalDamage = 0`；
  * 群体伤害（`ApplyDamageToMultipleCards` 一类）`:15698-15703`：免疫目标**直接跳过**（连 0x26 触发都不走）。

⇒ 落在两处：`engine/natives/damage.py::deal_damage`（免疫 ⇒ 直接返回）与 `sim/engine.py::sim_attack`
（钩子管线路径的扣血、还击、`excess` 溢出、`lethal` 斩杀都要看免疫）。

两个不同输入必须给出不同答案（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import state as S                                        # noqa: E402
import sim.engine as E                                               # noqa: E402
from engine.natives.damage import deal_damage                        # noqa: E402

fails = 0
ME, OPP = 1, 2


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(t_immune=False, a_immune=False, a_atk=2, t_dfn=5, t_atk=2, a_dfn=5, a_ab=()):
    a = S.U(1, ME, "frontline", a_atk, a_dfn, 1, "infantry", rng=2, ab=a_ab, immune=a_immune)
    t = S.U(2, OPP, "frontline", t_atk, t_dfn, 1, "infantry", immune=t_immune)
    return S.Sim({1: a, 2: t}, {ME: 20, OPP: 20}, 9, {}, my_side=ME)


def main():
    # ① 战斗：免疫的防守方吃 0 伤害
    s = mk(t_immune=False)
    s2 = E.sim_attack(s, 1, 2)
    chk("不免疫：2 攻打 5 防 ⇒ 掉到 3", 2 in s2.units and s2.units[2].dfn == 3,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(t_immune=True)
    s2 = E.sim_attack(s, 1, 2)
    chk("**免疫**：2 攻打 5 防 ⇒ **一点伤害都不吃**（仍是 5）",
        2 in s2.units and s2.units[2].dfn == 5, "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ② 效果伤害（deal_damage）同样不吃
    s = mk(t_immune=False)
    deal_damage(s, s.units[2], 4)
    chk("不免疫：效果 4 点伤害 ⇒ 5→1", s.units[2].dfn == 1, "dfn=%s" % s.units[2].dfn)
    s = mk(t_immune=True)
    deal_damage(s, s.units[2], 4)
    chk("**免疫**：效果 4 点伤害 ⇒ 一点不掉、也不死", 2 in s.units and s.units[2].dfn == 5,
        "dfn=%s" % (s.units[2].dfn if 2 in s.units else None))

    # ③ 免疫的攻击方也不吃还击
    s = mk(a_immune=False)
    s2 = E.sim_attack(s, 1, 2)
    chk("不免疫的攻击方：吃 2 点还击（5→3）", 1 in s2.units and s2.units[1].dfn == 3,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(a_immune=True)
    s2 = E.sim_attack(s, 1, 2)
    chk("**免疫的攻击方**：不吃还击（仍是 5）", 1 in s2.units and s2.units[1].dfn == 5,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ④ 免疫 + lethal：不吃伤害、也不会被斩杀（原版 doesDamageRecieverDie=false）
    s = mk(t_immune=True, a_ab=("lethal",), a_atk=9)
    s2 = E.sim_attack(s, 1, 2)
    chk("**免疫** + 攻击方带 `lethal` ⇒ 目标既不吃伤害也不被斩杀（仍 5 防）",
        2 in s2.units and s2.units[2].dfn == 5, "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(t_immune=False, a_ab=("lethal",), a_atk=9)
    s2 = E.sim_attack(s, 1, 2)
    chk("对照（不免疫）+ `lethal` ⇒ 直接斩杀", 2 not in s2.units,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
