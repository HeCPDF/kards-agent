#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攻击合法性 = **原版 `cardsCheckFunctions::CanAttack`**（P3 ⑥，2026-10-04）。

以前 `sim/engine.py::_rows_ok`/`can_hit_*` 是一张手写的"兵种百科"（步兵/坦克在支援线只能打前线、
空军/炮兵"哪里都能打"），`SIM-FIDELITY` 判它 **近似**。照 BP 逐行读完之后换成原版规则：

* `:185-204` **`not_enough_range`**：`attacker.location != 7(前线) ∧ defender.location != 7(前线)
  ∧ attacker.range < 2` ⇒ 打不了。⇒ `range < 2` 的单位必须自己在前线、或打站在前线的目标。
* `:206-242` **`hq_is_being_garded`/`is_being_guarded`**：对 **`IsBomber() || IsArtillery()`** 直接放行。
* `:244-296` **`fighter_protecting`**：**只有轰炸机**查这条；看**被攻击那一行**里有没有战斗机
  （`type == 0x4`）；目标是 HQ 时裸 `return`＝放行。

每个判据都用**两个会产生不同结论的输入**对比（弯路 #11）。
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


def mk(attacker_row="back", typ="infantry", rng=1, tgt_row="back", tgt_typ="infantry",
       tgt_guarded=False, hq_guarded=False, enemy_fighter_row=None):
    a = S.U(1, ME, attacker_row, 1, 1, 1, typ, rng=rng)
    t = S.U(2, OPP, tgt_row, 1, 1, 1, tgt_typ, guarded=tgt_guarded)
    units = {1: a, 2: t}
    if enemy_fighter_row:
        units[3] = S.U(3, OPP, enemy_fighter_row, 1, 1, 1, "fighter")
    return S.Sim(units, {ME: 20, OPP: 20}, 1, {}, my_side=ME, hq_guarded=hq_guarded), a, t


def main():
    # ① range：原版 `not_enough_range`
    s, a, t = mk(attacker_row="back", typ="infantry", rng=1, tgt_row="back")
    chk("range=1 在支援线打支援线目标 ⇒ **打不了**（not_enough_range）",
        E.can_hit_unit(s, a, t) is False)
    s2, a2, t2 = mk(attacker_row="back", typ="infantry", rng=2, tgt_row="back")
    chk("同一个位置、**range=2** ⇒ 打得着（原版只看 range，不看兵种）",
        E.can_hit_unit(s2, a2, t2) is True)
    s3, a3, t3 = mk(attacker_row="back", typ="infantry", rng=1, tgt_row="frontline")
    chk("range=1 在支援线打**前线**目标 ⇒ 打得着（旧「百科」也是这个结论，但理由不同）",
        E.can_hit_unit(s3, a3, t3) is True)
    s4, a4, t4 = mk(attacker_row="frontline", typ="infantry", rng=1, tgt_row="back")
    chk("range=1 但**自己在前线** ⇒ 打得着",
        E.can_hit_unit(s4, a4, t4) is True)
    s5, a5, _t5 = mk(attacker_row="back", typ="artillery", rng=1, tgt_row="back")
    chk("打总部同理：range=1 在支援线 ⇒ **打不了总部**", E.can_hit_hq(s5, a5) is False)
    s6, a6, _t6 = mk(attacker_row="back", typ="artillery", rng=2, tgt_row="back")
    chk("打总部：range=2 在支援线 ⇒ 打得着（炮兵真实 range ≥ 2）", E.can_hit_hq(s6, a6) is True)

    # ② 护卫：原版豁免 **轰炸机 || 炮兵**
    s, a, t = mk(typ="infantry", tgt_row="frontline", tgt_guarded=True)
    chk("步兵打**被护卫**的单位 ⇒ 拒", E.can_hit_unit(s, a, t) is False)
    s, a, t = mk(typ="bomber", rng=2, tgt_row="frontline", tgt_guarded=True)
    chk("**轰炸机**打被护卫的单位 ⇒ 放行（原版 `IsBomber() || IsArtillery()`；旧实现误拒）",
        E.can_hit_unit(s, a, t) is True)
    s, a, t = mk(typ="artillery", rng=2, tgt_row="frontline", tgt_guarded=True)
    chk("炮兵打被护卫的单位 ⇒ 放行", E.can_hit_unit(s, a, t) is True)
    s, a, _t = mk(typ="infantry", rng=2, tgt_row="frontline", hq_guarded=True)
    chk("步兵打**被护卫的总部** ⇒ 拒", E.can_hit_hq(s, a) is False)
    s, a, _t = mk(typ="bomber", rng=2, tgt_row="frontline", hq_guarded=True)
    chk("**轰炸机**打被护卫的总部 ⇒ 放行（旧实现只豁免炮兵 ⇒ 误拒）",
        E.can_hit_hq(s, a) is True)

    # ③ fighter_protecting：只有轰炸机 + 看**被攻击那一行**
    s, a, t = mk(typ="bomber", rng=2, tgt_row="frontline", enemy_fighter_row="frontline")
    chk("轰炸机打前线单位、敌方战斗机**也在前线** ⇒ 被拦（fighter_protecting）",
        E.can_hit_unit(s, a, t) is False)
    s, a, t = mk(typ="bomber", rng=2, tgt_row="frontline", enemy_fighter_row="back")
    chk("敌方战斗机在**后排**、目标是前线单位 ⇒ **不拦**（原版看被攻击那一行；旧实现看后排 ⇒ 误拦）",
        E.can_hit_unit(s, a, t) is True)
    s, a, t = mk(typ="infantry", rng=2, tgt_row="back", enemy_fighter_row="back")
    chk("**非轰炸机**打后排目标、敌方后排有战斗机 ⇒ 不拦（原版只有轰炸机查这条）",
        E.can_hit_unit(s, a, t) is True)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
