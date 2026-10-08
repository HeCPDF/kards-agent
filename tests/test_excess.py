#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原版 `excess` 自定义能力（P3 ③）：伤害超过目标总防的部分**转打敌方总部**。

依据（2026-10-04）：
  * `ExecuteAttackCard` `:17218-17244`（攻击链）与 `ApplyDamageToCard` `:16414-16446`（通用）
    同构：来源带 `HasCustomAbility("excess")` ∧ 目标 `IsUnit()` ∧ `finalDamage > 目标总防`
    ⇒ `ExcessDamage = 差`、打目标的伤害封顶到总防；
  * 溢出随后 `DamageCard(GetLocationCardBySide(对面), ExcessDamage, 来源, false, false, false)`
    （`ApplyDamageToCard` `:16489-16498`）；
  * **FModel 导出那句 `goto Label_3496` 是反编译 artifact** —— 只读反汇编
    （`_nn_scratch/probe_excess_asm.py`）显示拆分后直接落到 `setAndEncryptDefense`（伤害照常应用）。

两个不同输入必须给出不同答案（弯路 #11）：带 `excess` vs 不带；伤害 > 总防 vs ≤ 总防。
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


def mk(atk=8, dfn=3, ab=(), side=ME, hq=(20, 20)):
    u = S.U(1, side, "frontline", atk, dfn, 1, "infantry", ab=ab)
    t = S.U(2, OPP if side == ME else ME, "frontline", 2, dfn, 1, "infantry")
    return S.Sim({1: u, 2: t}, {ME: hq[0], OPP: hq[1]}, 1, {}, my_side=ME)


def main():
    # ① 不带 excess：8 攻打 3 防 ⇒ 目标吃满 8（dfn 变 -5 → 由调用方判死），总部不动
    s = mk(ab=())
    s2 = E.sim_attack(s, 1, 2)
    chk("不带 `excess`：8 攻打 3 防 ⇒ 目标吃满 8、敌方总部不变",
        s2.units.get(2) is None or s2.units[2].dfn <= 0, "units=%s hq=%s" % (list(s2.units), s2.hq))
    chk("不带 `excess`：敌方总部仍是 20", s2.hq.get(OPP) == 20, str(s2.hq))

    # ② 带 excess：伤害封顶到总防、溢出（8−3=5）打敌方总部
    s = mk(ab=("excess",))
    s2 = E.sim_attack(s, 1, 2)
    chk("带 `excess`：打目标的伤害被封顶到 3（目标正好归零）",
        2 not in s2.units or s2.units[2].dfn == 3 - 3, str({k: v.dfn for k, v in s2.units.items()}))
    chk("带 `excess`：溢出 5 点打到敌方总部（20 ⇒ 15）", s2.hq.get(OPP) == 15, str(s2.hq))

    # ③ 伤害 ≤ 总防 ⇒ 不溢出（门是"严格大于"）
    s = mk(atk=3, dfn=3, ab=("excess",))
    s2 = E.sim_attack(s, 1, 2)
    chk("伤害 == 总防 ⇒ **不**溢出（原版是 `>`）", s2.hq.get(OPP) == 20, str(s2.hq))
    s = mk(atk=2, dfn=3, ab=("excess",))
    s2 = E.sim_attack(s, 1, 2)
    chk("伤害 < 总防 ⇒ 不溢出、总部不变", s2.hq.get(OPP) == 20, str(s2.hq))

    # ④ 状态字段本身：`U.ab` 要能带、`copy()` 要保住
    #   ★ 2026-10-04（P4 第十二刀）更正：`ab` 从 `frozenset` 改成**计数映射**
    #     `{能力名: 次数}` —— 原版 `HasCustomAbility(name) = receivedAbilities[name] > 0` 本来就是计数
    #     （`CustomAbilityAdd/Remove` 增减、减到 0 才没有）；消费端全是 `in u.ab` ⇒ 语义不变 ✓。
    u = S.U(9, ME, "back", 1, 1, 1, "infantry", ab=("excess", "lethal"))
    chk("`U.ab` 存能力**计数映射**（集合入参归一成每项 1）、且 `copy()` 保住",
        u.ab == {"excess": 1, "lethal": 1} and u.copy().ab == u.ab, str(u.ab))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
