# -*- coding: utf-8 -*-
"""动作闸门的"选择界面还开着"要剔除幽灵手牌目标提示（2026-10-08 实机：KAR 无可选手牌，攻击/结束回合被自己的闸门连拒 6 步）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from ops.choices import ChoiceMixin                                   # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


class F(ChoiceMixin):
    def __init__(self, pend, ht):
        self._p, self._h = pend, ht

    def pick_pending(self):
        return dict(self._p)

    def hand_target_pending(self, verbose=True, with_legal=True):
        if isinstance(self._h, Exception):
            raise self._h
        return self._h


HT_PEND = {"pending": True, "is_selecting_hand_target": 1, "choose_one_active": 0}
r = F(HT_PEND, {"pending": False, "pending_raw": True, "ghost": "widget.pendingKill=True"}).pending_gate()
chk("手牌目标旗标 + 已判幽灵 ⇒ 不拦（带 ghost 原因）", r["pending"] is False and "pendingKill" in r["ghost"])
r = F(HT_PEND, {"pending": True}).pending_gate()
chk("活的手牌目标提示 ⇒ 仍拦", r["pending"] is True)
r = F({"pending": True, "is_selecting_hand_target": 0, "choose_one_active": 1}, {"pending": False, "ghost": "x"}).pending_gate()
chk("抉择界面（choose_one）⇒ 仍拦，不被手牌目标幽灵判据放行", r["pending"] is True)
r = F(HT_PEND, RuntimeError("boom")).pending_gate()
chk("判幽灵读取失败 ⇒ 保守按原样拦", r["pending"] is True)
r = F({"pending": False}, None).pending_gate()
chk("没有待选 ⇒ 不拦", r["pending"] is False)
print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
