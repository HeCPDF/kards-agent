#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`deal_damage` 规则下沉的特征测试（迁移前后行为不变）。

迁移：`sim.engine._dmg_unit` → `sim/effects.py::deal_damage`（`sim.engine` 只留旧名转调）。
顺带把"阵亡回调"变成显式参数（`on_death`）—— sim 只做"掉血/离场"，死亡触发链（L1 钩子）
由调用方注入，符合 EVAL-ARCHITECTURE §3.1 的分层。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
from engine.state import Sim, U                                 # noqa: E402
from sim.engine import _dmg_unit                                # noqa: E402
from sim.effects import deal_damage                            # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(**kw):
    return Sim({}, {ME: 20, OPP: 20}, 5.0, {}, **kw, my_side=ME)


def main():
    # 1) 普通效果伤害：直接扣防御、不扣重甲、不死不离场
    s = mk()
    u = U(1, OPP, "frontline", 2, 5, 2, "infantry")
    u.armor = 2
    s.units[1] = u
    deal_damage(s, u, 3)                       # 非战斗 ⇒ 不吃重甲
    chk("非战斗伤害：直接扣 3（dfn 5→2），重甲不参与", u.dfn == 2 and 1 in s.units)

    # 2) 战斗伤害：先扣重甲
    u2 = U(2, OPP, "frontline", 2, 5, 2, "infantry")
    u2.armor = 2
    s.units[2] = u2
    deal_damage(s, u2, 3, engage=True)         # 3-2=1
    chk("战斗伤害：3 伤吃 2 重甲 ⇒ 只掉 1（dfn 5→4）", u2.dfn == 4)

    # 3) 致死：离场 + 回调（回调就是死亡触发链的挂点）
    calls = []
    u3 = U(3, OPP, "frontline", 2, 2, 2, "infantry")
    s.units[3] = u3
    deal_damage(s, u3, 5, on_death=lambda st, uid: calls.append(uid))
    chk("致死：单位离场 + on_death 回调带上 id", 3 not in s.units and calls == [3], str(calls))

    # 4) 负伤害/0 伤害（`max(dmg,0)`）不涨血
    u4 = U(4, OPP, "frontline", 2, 2, 2, "infantry")
    s.units[4] = u4
    deal_damage(s, u4, -5)
    deal_damage(s, u4, 0)
    chk("负伤害/零伤害：防御不涨（dfn 仍 2）", u4.dfn == 2)

    # 5) 迁移前后一致：旧名转调 vs 直接调用
    a = mk()
    ua = U(9, OPP, "frontline", 2, 3, 2, "infantry")
    a.units[9] = ua
    _dmg_unit(a, ua, 1)
    b = mk()
    ub = U(9, OPP, "frontline", 2, 3, 2, "infantry")
    b.units[9] = ub
    deal_damage(b, ub, 1)
    chk("转调路径与直接调用结果一致（dfn 3→2）", ua.dfn == ub.dfn == 2)

    # 6) 换数据答案要变：同样 3 伤打 5 防 vs 打 2 防
    s6a = mk()
    a6 = U(1, OPP, "frontline", 2, 5, 2, "infantry")
    s6a.units[1] = a6
    deal_damage(s6a, a6, 3)
    s6b = mk()
    b6 = U(1, OPP, "frontline", 2, 2, 2, "infantry")
    s6b.units[1] = b6
    deal_damage(s6b, b6, 3)
    chk("换数据答案跟着变：5 防剩 2、2 防阵亡",
        a6.dfn == 2 and 1 in s6a.units and 1 not in s6b.units)

    # 7) 战斗伤害把重甲吃光 ⇒ 0 伤（防御不变、不离场）
    u7 = U(7, OPP, "frontline", 2, 3, 2, "infantry")
    u7.armor = 5
    s7 = mk()
    s7.units[7] = u7
    deal_damage(s7, u7, 3, engage=True)
    chk("重甲 ≥ 伤害 ⇒ 0 伤（dfn 不变）", u7.dfn == 3)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
