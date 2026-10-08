# -*- coding: utf-8 -*-
"""2026-10-06 卡死看门狗 + 陈旧 hand_target 判别（离线，假会话/假策略）。

事故：KING'S AFRICAN RIFLES 部署后游戏没有任何选择界面，但 `hand_target.pending` 读成 True 并跨回合残留，
回路在 hand_target 阶段空转 ~1000 s。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from player import loop as L                                        # noqa: E402
from player.loop import Loop, Action, _S, _C, _Sess                # noqa: E402
from player.rule import ME                                          # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


class Pol:
    name = "fake"

    def __init__(self, act=None):
        self.act = act
        self.seen_excl = None

    def decide(self, st, phase, pend=None, exclude_cards=None):
        self.seen_excl = set(exclude_cards or ())
        return self.act


def HT(cbp=19, widget="0xW", **kw):
    return {"hand_target": dict({"pending": True, "card_being_played": cbp, "widget": widget}, **kw)}


def mk(turn, hand=True):
    cards = [_C(ME, "hand", 5, "KAR", cost=3)] if hand else []
    cards.append(_C(ME, "frontline", 7, "U"))
    return _S(cards, turn=turn)


lp = Loop(_Sess(), Pol(), live=False, verbose=False)
# 1 手牌为空 ⇒ 陈旧
chk("手牌为空：hand_target 不成立", lp.phase_of(mk(17, hand=False), HT()) != "hand_target")
chk("  记下了陈旧原因", bool(lp.stale_hand_target))
# 2 真提示（有手牌、同回合）保留；跨回合同 key ⇒ 陈旧
lp = Loop(_Sess(), Pol(), live=False, verbose=False)
chk("有手牌同回合：保留 hand_target", lp.phase_of(mk(17), HT()) == "hand_target")
chk("同回合再读仍保留", lp.phase_of(mk(17), HT()) == "hand_target")
chk("跨回合同一 (源卡, widget)：判陈旧", lp.phase_of(mk(18), HT()) != "hand_target")
chk("  `_saw_pending` 没被陈旧读数置位", lp._saw_pending is False)
# 3 不再 pending ⇒ 清掉记录，同 key 新提示重新计
chk("提示消失后记录清空", lp.phase_of(mk(19), {"hand_target": {"pending": False}}) != "hand_target" and not lp._ht_seen)
chk("同 key 新提示（新回合）有效", lp.phase_of(mk(19), HT()) == "hand_target")
# 4 游戏判全灰
lp = Loop(_Sess(), Pol(), live=False, verbose=False)
chk("candidates 全不合法：陈旧",
    lp.phase_of(mk(5), HT(candidates=[{"card_id": 5, "valid": False}])) != "hand_target")
# 4b 同源的 pick_pending 旗标一并按陈旧处理 ⇒ 不进 hold（2026-10-07 KAR 无可选手牌：游戏跳过效果，但 pick_pending 仍读 True）
lp = Loop(_Sess(), Pol(), live=False, verbose=False)
_ph = lp.phase_of(mk(5), dict(HT(candidates=[{"card_id": 5, "valid": False}]), pick_pending={"pending": True}))
chk("陈旧 hand_target 同时 pick_pending=True ⇒ 不 hold（继续正常出牌）", _ph not in ("hold", "hand_target"), str(_ph))
chk("candidates 有合法：保留",
    lp.phase_of(mk(5), HT(widget="0xZ", candidates=[{"card_id": 5, "valid": True}])) == "hand_target")


# 5 看门狗：连续无进展 ⇒ stuck + done
class Rec:
    def __init__(self, phase="hand_target", executed=False, action=None, turn=17):
        self.phase, self.executed, self.action, self.turn = phase, executed, action, turn
        self.extra, self.note = {}, ""


lp = Loop(_Sess(), Pol(), live=False, verbose=False)
lp.max_stuck = 5
for i in range(4):
    chk("第 %d 步无进展：未停" % (i + 1), lp._watch_stuck(Rec()) is False)
chk("第 5 步：停手", lp._watch_stuck(Rec()) is True and lp.done and lp.stuck["steps"] == 5, str(lp.stuck))
# 有进展 / 换阶段 ⇒ 清零
lp = Loop(_Sess(), Pol(), live=False, verbose=False)
lp.max_stuck = 3
lp._watch_stuck(Rec()); lp._watch_stuck(Rec())
lp._watch_stuck(Rec(executed=True))
lp._watch_stuck(Rec()); lp._watch_stuck(Rec())
chk("成功一步后计数清零", lp.stuck is None)
lp._watch_stuck(Rec(phase="main"))
chk("离开选择类阶段清零", lp._np_count == 0)
# 同一动作连续被拒 ⇒ 加入排除集
lp = Loop(_Sess(), Pol(), live=False, verbose=False)
act = {"kind": "hand_target", "card": 2, "target": None}
for _ in range(3):
    lp._watch_stuck(Rec(action=act))
chk("同一动作被拒 3 次 ⇒ 排除", ("hand_target", 2, None) in lp._rejected)
# run()：看门狗触发后结果带 stuck
lp = Loop(_Sess(), Pol(), live=False, verbose=False, max_actions=100)
lp.max_stuck = 4
lp.step = lambda: Rec()
out = lp.run()
chk("run() 返回 stuck 且只跑 4 步", out.get("stuck") and out["n"] == 4, str(out.get("n")))
lp = Loop(_Sess(), Pol(), live=False, verbose=False, max_actions=7)
lp.max_stuck = 0
lp.step = lambda: Rec()
chk("max_stuck=0 关闭看门狗", lp.run().get("stuck") is None)

# 6 step()：策略无视排除集又给出刚被拒的牌 ⇒ 当没有动作
class S2(_Sess):
    def snapshot(self):
        return mk(17)

    def pending(self):
        return HT()


pol = Pol(Action("hand_target", card=2))
lp = Loop(S2(), pol, live=False, verbose=False)
lp._turn_seen = 17
lp._rejected.add(("hand_target", 2, None))
rec = lp.step()
chk("被拒过的手牌候选不再提", rec.action is None and rec.note == "没有可执行的动作" and pol.seen_excl == {2},
    str((rec.action, rec.note, pol.seen_excl)))

# 7 session.hand_target_legal：is_valid → can
from agent import precheck                                          # noqa: E402
from agent.session import AgentSession as Session                                   # noqa: E402
orig = precheck.call_read
try:
    sess = Session.__new__(Session)
    sess._cid = lambda c: c
    for r, want in (({"ok": True, "is_valid": False}, False), ({"ok": True, "is_valid": True}, True),
                    ({"ok": False, "stopped": "x"}, None)):
        precheck.call_read = lambda fn, *a, _r=r, **k: dict(_r)
        got = sess.hand_target_legal(3).get("can")
        chk("hand_target_legal %s ⇒ can=%s" % (r, want), got is want, str(got))
finally:
    precheck.call_read = orig

# 8 编排层：stuck ⇒ 投降
from gui import autoplay as AP                                      # noqa: E402
chk("autoplay 看门狗分支存在", "res.get(\"stuck\")" in open(AP.__file__, encoding="utf-8").read())

print("失败 %d" % bad)
sys.exit(1 if bad else 0)
