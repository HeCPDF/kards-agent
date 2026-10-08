#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`getTotalAttack` 原生 = clamp(基础攻击 + attackBuff, 0, 99)（实机 bug：58th 有冲击 +2 被读成 1 攻 ⇒
2nd RAIDING BRIGADE『摧毁攻击 ≤2 的敌方单位』挑了它，游戏拒绝）。

旧实现在有 `records_raw`（实机路径）时只解基础攻击、漏了 attackBuff。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kardsmem import cardnatives as CN                      # noqa: E402

T = CN._TABLE["getTotalAttack"]
K, X = 12345, 1939
bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-52s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def enc(v, y=77):
    return ((v * X + y) ^ K), y


def raw_card(base, buff, shadow=None):
    e1, y1 = enc(base)
    e2, y2 = enc(buff, 91)
    c = {"card_type": "infantry", "key": K,
         "records_raw": {"attack": (X, y1, 0, e1, 0), "attackBuff": (X, y2, 0, e2, 0)}}
    if shadow:
        c["shadow_stats"] = shadow
    return c


def main():
    chk("实机路径：基础 1 + buff 2 = 3（58th 有冲击）", T(None, raw_card(1, 2)), 3)
    chk("实机路径：无 buff 仍是基础值", T(None, raw_card(4, 0)), 4)
    chk("实机路径：负 buff 夹到 0", T(None, raw_card(2, -5)), 0)
    chk("实机路径：夹到 99", T(None, raw_card(90, 50)), 99)
    chk("同次空跑里 setAndEncryptAttack 写的基础值 + 原 buff", T(None, raw_card(1, 2, {"attack": 5})), 7)
    chk("只有视图字段：用 total_attack", T(None, {"card_type": "infantry", "attack": 1, "attack_buff": 2,
                                              "total_attack": 3}), 3)
    chk("只有 attack（无 buff 信息）", T(None, {"card_type": "infantry", "attack": 4}), 4)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
