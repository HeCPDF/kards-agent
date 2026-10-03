#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fight` / `convert` 的消费测试（汇总报告 §8 第 3 项收尾）。

规格：`NATIVE-COVERAGE-1.60.md` §15.5 第 977 行 —— `MakeCardsFight(unitThisSide,
unitOppositeSide, instigatorID)` 建模为"两张牌互相受对方 atk 的伤害"，报告自标
**BP 函数体未读、待核**（§9 / TODO A5）⇒ 实现照做但**必须如实记缺口**。
`ConvertCard` 语义完全没读过（只看了签名）⇒ **只记缺口、不动状态**（不许编造）。

另钉住 `effectvm` 侧：`MakeCardsFight` 的两个实参是**卡指针**，要靠 `rec.ptr_ids`
换成 card_id（换不出 ⇒ 记缺口、保留布尔）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import semantics.effectvm as EV                                    # noqa: E402
from policy.boardeval import U                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), kred=5.0, hq=(20, 20), **kw):
    return B.Sim({u.id: u for u in units}, {"local": hq[0], "enemy": hq[1]}, kred, {}, **kw)


def main():
    # ---- 1) 报告口径的例子：a(3/3) vs b(2/2) ⇒ b 阵亡、a 剩 1 防 ----
    st = mk([U(1, "local", "frontline", 3, 3, 3, "infantry"),
             U(2, "enemy", "frontline", 2, 2, 2, "infantry")])
    B._apply_eff(st, {"fight": [1, 2]}, None)
    chk("3/3 vs 2/2：2 号阵亡、1 号剩 1 防",
        (2 not in st.units) and st.units[1].dfn == 1,
        "units=%s" % {k: (u.atk, u.dfn) for k, u in st.units.items()})
    chk("fight 记缺口（BP 体未读，不许假装是 IDA 结论）",
        any("MakeCardsFight" in g for g in st.gaps), str(st.gaps))

    # ---- 2) 换数据答案要变：a(1/1) vs b(3/3) 反过来 ----
    st2 = mk([U(1, "local", "frontline", 1, 1, 1, "infantry"),
              U(2, "enemy", "frontline", 3, 3, 3, "infantry")])
    B._apply_eff(st2, {"fight": [1, 2]}, None)
    chk("1/1 vs 3/3：1 号阵亡、2 号剩 2 防",
        (1 not in st2.units) and st2.units[2].dfn == 2,
        "units=%s" % {k: (u.atk, u.dfn) for k, u in st2.units.items()})

    # ---- 3) 目标不在场 ⇒ 缺口 + 不动状态 ----
    st3 = mk([U(1, "local", "frontline", 3, 3, 3, "infantry")])
    B._apply_eff(st3, {"fight": [1, 99]}, None)
    chk("目标不在场 ⇒ 记缺口、双方都不掉血",
        st3.units[1].dfn == 3 and any("不在场上" in g for g in st3.gaps), str(st3.gaps))

    # ---- 4) 只有布尔（effectvm 没换出 id）⇒ 记缺口，不静默 ----
    st4 = mk([U(1, "local", "frontline", 3, 3, 3, "infantry"),
              U(2, "enemy", "frontline", 2, 2, 2, "infantry")])
    B._apply_eff(st4, {"fight": True}, None)
    chk("fight=True（缺 id）⇒ 记缺口、不结算",
        st4.units[2].dfn == 2 and any("没换出来" in g for g in st4.gaps), str(st4.gaps))

    # ---- 5) convert：只记缺口、不动状态 ----
    st5 = mk([U(1, "local", "frontline", 3, 3, 3, "infantry")])
    B._apply_eff(st5, {"convert": True}, 1)
    u = st5.units[1]
    chk("convert ⇒ 缺口且单位属性一字不动",
        (u.atk, u.dfn) == (3, 3) and any("ConvertCard" in g for g in st5.gaps), str(st5.gaps))

    # ---- 6) effectvm：卡指针 → card_id ----
    rec = EV.Recorder()
    rec.ptr_ids = {0x1000: 11, 0x2000: 22}
    rec.records = [{"verb": "MakeCardsFight", "args": [0x1000, 0x2000, 7], "tainted": False}]
    eff = EV.to_effects(rec)
    chk("MakeCardsFight：ptr→id 出 [11, 22]", eff.get("fight") == [11, 22], str(eff))

    # ---- 7) 换指针 ⇒ 答案跟着变（不是写死） ----
    rec2 = EV.Recorder()
    rec2.ptr_ids = {0x3000: 5, 0x4000: 6}
    rec2.records = [{"verb": "MakeCardsFight", "args": [0x3000, 0x4000, 7], "tainted": False}]
    chk("换一组指针 ⇒ 出 [5, 6]", EV.to_effects(rec2).get("fight") == [5, 6])

    # ---- 8) 指针换不出 id ⇒ 记缺口 + 保留布尔（不许当成 [None, None]） ----
    rec3 = EV.Recorder()
    rec3.records = [{"verb": "MakeCardsFight", "args": [0x999, 0x888, 7], "tainted": False}]
    eff3 = EV.to_effects(rec3)
    chk("换不出 id ⇒ fight=True 且记缺口",
        eff3.get("fight") is True and any("MakeCardsFight" in g for g in rec3.gaps),
        "%s / %s" % (eff3.get("fight"), rec3.gaps))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
