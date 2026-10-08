#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`RuleV2._blocked_without_target`：要目标的单位牌、我们没找到任何合法目标时，问游戏自己的 CanPlayFromHand
（实机 T8：3rd CARPATHIAN 场上没有友方单位，仍被排成『不指向』部署）。假 sess，离线。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _hookfake import card                                          # noqa: E402
import player.rule as R                                             # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


class Sess:
    def __init__(self, reply=None, boom=False):
        self.reply, self.boom, self.n = reply, boom, 0

    def _inj(self, fn, *a, **k):
        self.n += 1
        assert fn == "game_can_play_from_hand"
        if self.boom:
            raise RuntimeError("注入侧不可用")
        return self.reply


def pol(sess):
    p = R.RuleV2.__new__(R.RuleV2)
    p.sess = sess
    p._act_cache = {}
    p._epoch = 1
    return p


def main():
    c = card(34, "3rd CARPATHIAN", "hand")
    chk("游戏说 can=False（reason=friendly_unit）⇒ 不带目标的走法被挡",
        pol(Sess({"ok": True, "can": False, "reason": "friendly_unit"}))._blocked_without_target(c) is True)
    chk("游戏说 can=True ⇒ 不挡", pol(Sess({"ok": True, "can": True}))._blocked_without_target(c) is False)
    chk("问不到（ok=False）⇒ 不挡（宁可多试一步）", pol(Sess({"ok": False, "error": "x"}))._blocked_without_target(c) is False)
    chk("注入侧抛异常 ⇒ 不挡", pol(Sess(boom=True))._blocked_without_target(c) is False)
    s = Sess({"ok": True, "can": False, "reason": "friendly_unit"})
    p = pol(s)
    p._blocked_without_target(c)
    p._blocked_without_target(c)
    chk("同一 epoch 内只问一次（缓存）", s.n == 1, "n=%d" % s.n)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
