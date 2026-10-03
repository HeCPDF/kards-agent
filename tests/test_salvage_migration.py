#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收缴（salvage）规则下沉的特征测试（"迁移前后行为不变"）。

迁移：`boardeval._salvage_one` → `sim/effects.py::apply_salvage`（boardeval 只留旧名转调）。
顺带修一处依赖方向问题：`hand_cap`（规则常数）原先直接读评估权重表 `W["hand_cap"]`，
现在由调用方传进 sim（§3.1：sim 不 import 权重）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
from policy.boardeval import H, U                               # noqa: E402
from sim.effects import apply_salvage                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), hand=(), **kw):
    return B.Sim({u.id: u for u in units}, {"local": 20, "enemy": 20}, 5.0,
                 {c.id: c for c in hand}, **kw)


def main():
    tpl = H(21, "BIG UNIT", 7, "tank", 5, 6, ("guard", "blitz"), {})

    # 1) 迁移前的口径：走 `_apply_eff({"salvage_ids": [...]})` 这条老路
    s = mk([U(1, "local", "back", 2, 2, 2, "infantry")], card_templates={21: tpl})
    B._apply_eff(s, {"salvage_ids": [21]}, None)
    got = list(s.hand.values())
    chk("转调路径：进手牌 1 张、1/1、费用 min(7,3)=3、关键词保留",
        len(got) == 1 and (got[0].atk, got[0].dfn, got[0].cost) == (1, 1, 3)
        and got[0].kw == frozenset(("guard", "blitz")), str(got))
    chk("转调路径：名字/类型/效果原样保留",
        got[0].name == "BIG UNIT" and got[0].typ == "tank" and got[0].eff == {},
        "%s/%s" % (got[0].name, got[0].typ))

    # 2) 直接调 sim 侧新 API：同输入同输出
    s2 = mk([U(1, "local", "back", 2, 2, 2, "infantry")], card_templates={21: tpl})
    created = apply_salvage(s2, 21, hand_cap=B.W["hand_cap"])
    got2 = list(s2.hand.values())
    chk("sim.apply_salvage 与旧路径结果一致（行为不变）",
        created is True and len(got2) == 1 and (got2[0].atk, got2[0].dfn, got2[0].cost) == (1, 1, 3)
        and got2[0].kw == got[0].kw and got2[0].name == got[0].name)

    # 3) 手牌满 ⇒ 不创建（BP IsLocationFull 分支）
    full = mk([U(1, "local", "back", 2, 2, 2, "infantry")],
              hand=[H(100 + i, "F%d" % i, 1, "order") for i in range(B.W["hand_cap"])],
              card_templates={21: tpl})
    chk("手牌满 ⇒ 不创建、返回 False", apply_salvage(full, 21, hand_cap=9) is False
        and len(full.hand) == 9)

    # 4) 认不出模板 ⇒ 1/1 infantry "?"（不当作指令牌）
    s4 = mk([U(1, "local", "back", 2, 2, 2, "infantry")])
    apply_salvage(s4, 999, hand_cap=9)
    h4 = list(s4.hand.values())[0]
    chk("认不出模板 ⇒ 1/1 infantry、名字 '?'",
        (h4.atk, h4.dfn, h4.typ, h4.name) == (1, 1, "infantry", "?"), str((h4.atk, h4.dfn, h4.typ)))

    # 5) 换数据答案要变：费用 7 vs 2（min(...,3) 夹到 2）
    cheap = H(22, "CHEAP", 2, "infantry", 3, 3, (), {})
    s5 = mk([U(1, "local", "back", 2, 2, 2, "infantry")], card_templates={22: cheap})
    apply_salvage(s5, 22, hand_cap=9)
    h5 = list(s5.hand.values())[0]
    chk("换数据答案跟着变：原费 2 ⇒ 收缴后仍是 2（3 是上限）", h5.cost == 2 and h5.atk == 1)

    # 6) hand_cap 是参数（sim 不读权重表）：传 1 时手上有 1 张就拒绝
    s6 = mk([U(1, "local", "back", 2, 2, 2, "infantry")],
            hand=[H(50, "X", 1, "order")], card_templates={21: tpl})
    chk("hand_cap 传参生效（cap=1 ⇒ 手上已有 1 张就拒）",
        apply_salvage(s6, 21, hand_cap=1) is False and len(s6.hand) == 1)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
