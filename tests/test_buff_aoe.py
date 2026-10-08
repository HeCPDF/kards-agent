#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群体 buff 逐张结算（"给你所有单位 +3+2"，SCORCHING SUN 2 / MONSOON RAIN 2 一类）。

标量 `buff` 只够单目标；VM 录到的每次 ChangeAttack/ChangeDefense 现在按目标卡记 `buff_ids`
（`effectvm.to_effects`），模拟逐张结算。同时钉住：不带目标的群体 buff 不会被套到"某一个目标"上。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
import semantics.effectvm as EV                                    # noqa: E402
from engine.state import Sim, U                                    # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), kred=5.0, hq=(20, 20), **kw):
    return Sim({u.id: u for u in units}, {ME: hq[0], OPP: hq[1]}, kred, {}, **kw, my_side=ME)


def main():
    st = mk([U(1, ME, "frontline", 2, 2, 2, "infantry"),
             U(2, ME, "back", 1, 1, 1, "infantry"),
             U(3, OPP, "frontline", 2, 2, 2, "infantry")])
    eff = {"buff": [6, 4], "buff_ids": {1: [3, 2], 2: [3, 2]}}
    _apply_eff(st, eff, None)
    chk("群体 buff：两张我方单位各 +3/+2", (st.units[1].atk, st.units[1].dfn) == (5, 4)
        and (st.units[2].atk, st.units[2].dfn) == (4, 3), str({k: (u.atk, u.dfn) for k, u in st.units.items()}))
    chk("群体 buff：敌方单位不动", (st.units[3].atk, st.units[3].dfn) == (2, 2))

    # 带目标的单体 buff 老路径不变（只有一张）
    st2 = mk([U(1, ME, "frontline", 2, 2, 2, "infantry"), U(2, ME, "back", 1, 1, 1, "infantry")])
    _apply_eff(st2, {"buff": [2, 2], "buff_ids": {1: [2, 2]}}, 1)
    chk("单体 buff：只加在目标上", (st2.units[1].atk, st2.units[2].atk) == (4, 1))

    # to_effects：每次 ChangeAttack/Defense 按目标卡指针记账
    rec = EV.Recorder() if hasattr(EV, "Recorder") else None
    if rec is not None:
        rec.ptr_ids[0x100] = 1
        rec.ptr_ids[0x200] = 2
        for p in (0x100, 0x200):
            for verb, n in (("ChangeAttack", 3), ("ChangeDefense", 2)):
                rec.records.append({"verb": verb, "args": [p, 9, n], "tainted": False})
        e = EV.to_effects(rec, my_side=1)
        chk("to_effects：buff_ids 逐张记账", e.get("buff_ids") == {1: [3, 2], 2: [3, 2]} and e.get("buff") == [6, 4], str(e))
    # SetValue 型（MONSOON RAIN 2：把所有单位的攻/防/行动费设为 2）⇒ 换成增量 = 2 - 当前值
    rec = EV.Recorder()
    rec.ptr_ids[0x100], rec.ptr_ids[0x200] = 1, 2
    rec.cur_stats[0x100] = {"attack": 5, "defense": 4, "opcost": 1}
    rec.cur_stats[0x200] = {"attack": 1, "defense": 1, "opcost": 3}
    for p in (0x100, 0x200):
        for verb in ("ChangeAttack", "ChangeDefense", "ChangeOperationCost"):
            rec.records.append({"verb": verb, "args": [p, 9, 2, 2], "tainted": False})
    e = EV.to_effects(rec, my_side=1)
    chk("SetValue：设为 2 ⇒ 增量（5/4 → -3/-2；1/1 → +1/+1；行动费 1→2、3→2 合计 0）",
        e.get("buff_ids") == {1: [-3, -2], 2: [1, 1]} and e.get("buff") == [-2, -1] and not e.get("opcost"), str(e))
    rec2 = EV.Recorder()
    rec2.ptr_ids[0x100] = 1
    rec2.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 3, 1], "tainted": False})
    chk("permBuff(1) 仍是加法", EV.to_effects(rec2, my_side=1).get("buff") == [3, 0])
    # 随机撤退一个敌方单位（TROPICAL STORM 2）：数组只有 1 张 ⇒ 只撤这一张，不是敌方前线全撤
    rec3 = EV.Recorder()
    rec3.ptr_ids[0x300] = 3
    rec3.records.append({"verb": "MakeCardRetreat", "args": [[0x300], 9], "tainted": False})
    e3 = EV.to_effects(rec3, my_side=1)
    chk("MakeCardRetreat 单张 ⇒ retreat_ids=[3]", e3.get("retreat_ids") == [3] and "retreat_aoe" not in e3, str(e3))
    rec4 = EV.Recorder()
    rec4.records.append({"verb": "MakeCardRetreat", "args": [[0x999], 9], "tainted": False})
    e4 = EV.to_effects(rec4, my_side=1)
    chk("换不出 id ⇒ 不产出撤退效果、记缺口（不兜底成整排撤退）", "retreat_ids" not in e4 and any("MakeCardRetreat" in g for g in rec4.gaps), str(e4))
    sx = mk([U(3, OPP, "frontline", 2, 2, 2, "infantry"), U(4, OPP, "frontline", 2, 2, 2, "infantry")])
    _apply_eff(sx, e3, None)
    chk("sim 只撤被点名的那一张", 3 not in sx.units and 4 in sx.units, str(list(sx.units)))
    # 群体撤退（DELAYING TACTICS：前线所有单位撤退，敌我都有）⇒ 数组里的每一张都结算，后排的不动
    rec5 = EV.Recorder()
    for p_, i_ in ((0x501, 5), (0x502, 6), (0x503, 7)):
        rec5.ptr_ids[p_] = i_
    rec5.records.append({"verb": "MakeCardRetreat", "args": [[0x501, 0x502, 0x503], 9], "tainted": False})
    e5 = EV.to_effects(rec5, my_side=1)
    sy = mk([U(5, ME, "frontline", 2, 2, 2, "infantry"), U(6, OPP, "frontline", 2, 2, 2, "infantry"),
             U(7, OPP, "frontline", 2, 2, 2, "infantry"), U(8, OPP, "back", 2, 2, 2, "infantry")])
    _apply_eff(sy, e5, None)
    chk("群体撤退：数组里的 3 张（敌我前线）都撤，后排的 8 不动", e5.get("retreat_ids") == [5, 6, 7] and list(sy.units) == [8],
        "%s %s" % (e5, list(sy.units)))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
