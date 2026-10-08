#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伏击门 + 报复例外（P3 ⑦，2026-10-04）—— 照 `CalculateDamageDealt` 逐行读出来的条件。

`BP_CardFunctions::CalculateDamageDealt`（`:14809-15167`）是真正的伤害计算（`AttackCard` `:17064-17111`
对两个方向各调一次，再把结果喂给 `ExecuteAttackCard`）：

* **伏击**（`:14868-14949`）：`receiver.getHasAmbush() ∧ ¬ignoreAmbush ∧ ¬dealer.getHasImmune()
  ∧ dealingDamageIsAttacker ∧ ¬receiver.hasBeenAttackedThisTurn` ⇒ 再比
  `receiver.攻击 ≥ dealer.总防御 + 重甲 + 被动防御buff + beforeAttackBuff`：
  成立（伏击能打死攻击方）⇒ 攻击方这一击**打不出伤害**（`:14951`、`:14965`/`:14979`/`:15003` 同族）；
  不成立 ⇒ 攻击方照常打（`:14948` → `Label_2888`）。
  ★ 我们以前**漏了 `¬hasBeenAttackedThisTurn`** ⇒ 同一回合的第二次攻击也会被伏击吞掉。
* **报复例外**（`:14955-15005`）：`¬dealingDamageIsAttacker`（防守方还击）时，
  防守方是**炮兵** ⇒ 0；防守方是**轰炸机**且伤害方不是**战斗机/防空** ⇒ 0
  ⇒ 反过来：战斗机打轰炸机，**轰炸机会还击**（旧实现无条件豁免 ⇒ 偏差）。
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


def mk(be_attacked=False, atk_typ="infantry", def_typ="infantry", def_kw=("ambush",),
       a_atk=2, a_dfn=2, a_armor=0, t_atk=2, t_dfn=2):
    a = S.U(1, ME, "frontline", a_atk, a_dfn, 1, atk_typ, rng=2, armor=a_armor)
    t = S.U(2, OPP, "frontline", t_atk, t_dfn, 1, def_typ, kw=def_kw,
            been_attacked=be_attacked)
    return S.Sim({1: a, 2: t}, {ME: 20, OPP: 20}, 9, {}, my_side=ME)


def main():
    # ① 伏击门：本回合**第一次**被攻击 ⇒ 伏击生效（2 攻打 2 防 = 打得死 ⇒ 攻击方打不出伤害、自己阵亡）
    s = mk(be_attacked=False)
    s2 = E.sim_attack(s, 1, 2)
    chk("伏击生效（本回合首次被攻击）：攻击方被打死、伤害没落在目标上",
        1 not in s2.units and 2 in s2.units and s2.units[2].dfn == 2,
        "units=%s" % {k: (v.dfn, v.atk) for k, v in s2.units.items()})

    # ② 对照输入：**本回合已被攻击过** ⇒ 伏击**不**生效，攻击方照常打（目标掉 2 防、死）
    s = mk(be_attacked=True, a_dfn=9, t_dfn=2)
    s2 = E.sim_attack(s, 1, 2)
    chk("伏击**不**生效（本回合已被攻击过）：目标吃满 2 点死掉、攻击方只挨 2 点还击（9→7）",
        2 not in s2.units and 1 in s2.units and s2.units[1].dfn == 7,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ③ 伏击打不死攻击方 ⇒ 攻击方照常打（原版 :14948 → Label_2888）；攻击方只吃**伏击那一击**、
    #    不再另吃还击（伏击分支里 t_atk 被清零）
    s = mk(a_atk=2, a_dfn=9, t_atk=2, t_dfn=5)
    s2 = E.sim_attack(s, 1, 2)
    chk("伏击**杀不掉**攻击方（攻方 9 防 + 0 甲 vs 伏击方 2 攻）⇒ 攻方吃 2 点伏击（9→7）、"
        "照常打 2 点（目标 5→3）、不再另吃还击",
        2 in s2.units and s2.units[2].dfn == 3 and 1 in s2.units and s2.units[1].dfn == 7,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ④ 重甲计入伏击致死判据（原版 :14936-14946：防御 + 重甲 + …）
    s = mk(a_atk=2, a_dfn=1, a_armor=2, t_atk=2, t_dfn=5, def_kw=())
    s2 = E.sim_attack(s, 1, 2)
    chk("重甲 2 让 2 攻**打不死** 1 防的攻击方 ⇒ 攻击方活着、目标照常掉 2 防",
        1 in s2.units and 2 in s2.units and s2.units[2].dfn == 3,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ⑤ 报复豁免看**攻击方**（＝还击方向的 receiver）的类型 —— 原版 :14955-15005
    s = mk(atk_typ="bomber", def_typ="infantry", def_kw=(), a_dfn=9, t_atk=2, t_dfn=5)
    s2 = E.sim_attack(s, 1, 2)
    chk("**轰炸机**攻击者 vs 步兵防守方 ⇒ **不吃还击**（原版 :14983-15005）",
        1 in s2.units and s2.units[1].dfn == 9 and 2 in s2.units and s2.units[2].dfn == 3,
        "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(atk_typ="bomber", def_typ="fighter", def_kw=(), a_dfn=9, t_atk=2, t_dfn=5)
    s2 = E.sim_attack(s, 1, 2)
    chk("**轰炸机**攻击者 vs **战斗机**防守方 ⇒ **照常吃还击**（同一段的例外：dealer 是 fighter/AA）",
        1 in s2.units and s2.units[1].dfn == 7, "units=%s" % {k: v.dfn for k, v in s2.units.items()})
    s = mk(atk_typ="infantry", def_typ="bomber", def_kw=(), a_dfn=9, t_atk=2, t_dfn=5)
    s2 = E.sim_attack(s, 1, 2)
    chk("步兵攻击者 vs **轰炸机**防守方 ⇒ 照常吃还击（规则看攻击方，不看防守方）",
        1 in s2.units and s2.units[1].dfn == 7, "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    # ⑥ 免疫的伤害方无视伏击（原版 :14877-14887：`dealer.getHasImmune()` ⇒ 不走伏击）。
    #   对照 ①（同参数、不免疫）：伏击生效 ⇒ 攻击方打死、**目标一点伤害都没吃到**（dfn 仍是 2）。
    #   这里免疫 ⇒ 伏击不生效 ⇒ 目标照常吃 2 点（5→3）；攻击方随后仍会被**普通还击**打死
    #   （那是反击、不是伏击）。
    s = mk(be_attacked=False, a_atk=2, a_dfn=2, t_atk=2, t_dfn=5)
    s.units[1].immune = True
    s2 = E.sim_attack(s, 1, 2)
    chk("攻击方 `isImmune` ⇒ **伏击不生效**：目标吃到 2 点（5→3，对照组 ① 是一点没吃到）",
        2 in s2.units and s2.units[2].dfn == 3, "units=%s" % {k: v.dfn for k, v in s2.units.items()})

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
