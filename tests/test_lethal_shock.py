#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`lethal` 与 `CantLoseShock`（P3 ⑦，2026-10-04）—— 照 `CalculateDamageDealt` / `ExecuteAttackCard` 实现。

* **`lethal`**：`BP_CardFunctions::CalculateDamageDealt` `:15076-15095` ——
  `_dealerCalculatedDamage > 0` ∧ `!receiver.IsLocation()`（目标不是总部）∧ 伤害方
  `HasCustomAbility("lethal")` ⇒ `localDoesDamageRecieverDie = true` ⇒ **目标直接死**，
  与它还剩多少防御无关（判据里的 damage 是**扣完重甲**之后的值，`:15033-15047`）。
* **`CantLoseShock`**：`ExecuteAttackCard`（只读反汇编 offset 617）—— 摘冲击的完整条件是
  `!defender.IsLocation() ∧ attacker.getHasShock() ∧ ¬attacker.HasCustomAbility("CantLoseShock")`。
  我们以前**无条件**摘 ⇒ 带该能力的单位会丢冲击。

两个不同输入必须给出不同答案（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import state as S                                        # noqa: E402
import sim.engine as E                                               # noqa: E402

fails = 0
ME, OPP = 1, 2


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(a_ab=(), a_kw=(), a_atk=2, t_dfn=99, t_armor=0, t_atk=0):
    a = S.U(1, ME, "frontline", a_atk, 5, 1, "infantry", kw=a_kw, ab=a_ab, rng=2)
    t = S.U(2, OPP, "frontline", t_atk, t_dfn, 1, "infantry", armor=t_armor)
    return S.Sim({1: a, 2: t}, {ME: 20, OPP: 20}, 9, {}, my_side=ME)


def main():
    # ① lethal：2 攻打 99 防 —— 不带 lethal ⇒ 活着且掉 2；带 lethal ⇒ 直接死
    s = mk(a_ab=())
    s2 = E.sim_attack(s, 1, 2)
    chk("不带 `lethal`：2 攻打 99 防 ⇒ 目标活着、掉到 97",
        2 in s2.units and s2.units[2].dfn == 97, "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(a_ab=("lethal",))
    s2 = E.sim_attack(s, 1, 2)
    chk("带 `lethal`：2 攻打 99 防 ⇒ **目标直接死**（原版 :15076-15095）",
        2 not in s2.units, "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    # ② lethal 的判据用**扣完重甲之后**的伤害：2 攻打 2 重甲 ⇒ 有效伤害 0 ⇒ 不触发
    s = mk(a_ab=("lethal",), a_atk=2, t_armor=2, t_dfn=99)
    s2 = E.sim_attack(s, 1, 2)
    chk("带 `lethal` 但**重甲吃光伤害**（2 攻 vs 2 甲 ⇒ 有效 0）⇒ 不触发斩杀",
        2 in s2.units and s2.units[2].dfn == 99, "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ③ CantLoseShock：攻击后冲击该不该摘
    s = mk(a_kw=("shock",), a_ab=())
    s2 = E.sim_attack(s, 1, 2)
    chk("普通情况：攻击后**摘掉**冲击",
        1 in s2.units and "shock" not in s2.units[1].kw, "kw=%s" % (s2.units.get(1).kw if 1 in s2.units else None))
    s = mk(a_kw=("shock",), a_ab=("CantLoseShock",))
    s2 = E.sim_attack(s, 1, 2)
    chk("带 `CantLoseShock`：攻击后**保留**冲击（原版 offset 617 的 ¬HasCustomAbility 分支）",
        1 in s2.units and "shock" in s2.units[1].kw, "kw=%s" % (s2.units.get(1).kw if 1 in s2.units else None))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
