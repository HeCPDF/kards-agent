# -*- coding: utf-8 -*-
"""回合首步指挥点等待：ActionProcess 回落 + 指挥点稳定即放行（离线，假会话）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from player import loop as L                                        # noqa: E402
from player.loop import Loop                                        # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


class St:
    my_side = 1

    def __init__(self, ap, k):
        self.action_process, self.kredits = ap, {1: k}


class Sess:
    def __init__(self, seq):
        self.seq, self.i = seq, 0

    def snapshot(self):
        s = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return s


slept = []
L._settle = lambda sec, frames=6: slept.append(sec)

lp = Loop(Sess([St(True, 0), St(False, 3), St(False, 3)]), None, live=False, verbose=False)
st, why = lp._wait_turn_ready(St(True, 0))
chk("ActionProcess 回落且两读一致 ⇒ ready", why == "ready" and st.kredits[1] == 3)
chk("  只轮询了 3 次（远小于等满 2s）", lp.sess.i == 3)

lp = Loop(Sess([St(False, 0), St(False, 2), St(False, 2)]), None, live=False, verbose=False)
st, why = lp._wait_turn_ready(St(False, 0))
chk("指挥点还在变（0→2）不放行，稳定后才 ready", why == "ready" and st.kredits[1] == 2 and lp.sess.i == 3)

lp = Loop(Sess([St(None, 1)]), None, live=False, verbose=False)
slept.clear()
st, why = lp._wait_turn_ready(St(None, 1))
chk("读不到旗标 ⇒ no_flag，补等满 turn_settle", why == "no_flag" and sum(slept) >= lp.turn_settle - 0.5)

lp = Loop(Sess([St(True, 1)]), None, live=False, verbose=False)
lp.turn_settle = 0.3
import time
_t = time.time()
st, why = lp._wait_turn_ready(St(True, 1))
chk("ActionProcess 一直为真 ⇒ timeout（有上限）", why == "timeout")

# ---------------------------------------------------------------- 轻量路径（peek_turn_state）
class PSess:
    """假会话：peek 按序列吐 (turn, k, slots, action_process)；snapshot 被调用就记数（轻量路径不该调）。"""

    def __init__(self, seq, ok=True):
        self.seq, self.i, self.snaps, self.ok = seq, 0, 0, ok

    def peek_turn_state(self, my_side=None):
        if not self.ok:
            return {"ok": False}
        turn, k, s, ap = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return {"ok": True, "turn": turn, "mine_k": k, "mine_slots": s, "action_process": ap}

    def snapshot(self):
        self.snaps += 1
        return St(False, 0)


def mk(seq, settle=2.0, ok=True):
    sess = PSess(seq, ok)
    lp = Loop(sess, None, live=False, verbose=False)
    lp.turn_settle, lp.turn_poll = settle, 0.001
    return lp, sess


L._sleep_poll = lambda sec: time.sleep(sec)

# 1 flip 时指挥点 0（槽数还没涨）→ 涨到 (turn+1)//2 且稳定 ⇒ ready；全程不建整快照
lp, se = mk([(5, 0, 2, False), (5, 0, 2, False), (5, 3, 3, False), (5, 3, 3, False), (5, 3, 3, False)])
st, why = lp._wait_turn_ready(St(True, 0))
chk("k0=0 → 槽数涨到本回合值且指挥点=槽数、连续两读一致 ⇒ ready", why == "ready" and se.i == 4)
chk("  轻量路径不建整快照", se.snaps == 0)
chk("  记下本回合槽数", lp._settled_slots == (5, 3))

# 2 陷阱：对手回合末旧槽数=2、上回合一点没花所以指挥点也=2 ⇒ 槽数还没涨，不放行
lp, se = mk([(5, 2, 2, False)] * 6 + [(5, 3, 3, False)] * 3)
lp._settled_slots = (3, 2)
st, why = lp._wait_turn_ready(St(True, 2))
chk("旧槽数≠本回合应有值（指挥点恰=旧槽数）不放行，涨到 3 才 ready", why == "ready" and se.i == 8)

# 2b 回合号滞后（读到的回合号还是上一个我方回合 3，槽数 2，上次落定 (3,2)）⇒ 不放行
lp, se = mk([(3, 2, 2, False)] * 400, settle=0.2)
lp._settled_slots = (3, 2)
st, why = lp._wait_turn_ready(St(True, 2))
chk("回合号没动（≤ 上次落定回合）⇒ 视为新局重置，但槽数==应有值时仍按判据放行（无上局约束）", why == "ready")
lp, se = mk([(5, 2, 2, False)] * 400, settle=0.2)
lp._settled_slots = (3, 2)
st, why = lp._wait_turn_ready(St(True, 2))
chk("回合号已是 5 而槽数还是旧值 2（应有值 3）⇒ timeout", why == "timeout")

# 3 ActionProcess 为真 ⇒ 不放行（即使指挥点/槽数都对）
lp, se = mk([(5, 3, 3, True)] * 400, settle=0.2)
st, why = lp._wait_turn_ready(St(True, 0))
chk("ActionProcess 一直为真 ⇒ timeout", why == "timeout")

# 4 原事故保护：指挥点一直 0 ⇒ 不放行，吃满上限
lp, se = mk([(5, 0, 3, False)] * 400, settle=0.2)
t0 = time.time()
st, why = lp._wait_turn_ready(St(True, 0))
chk("指挥点一直 0（kredits=0 事故）⇒ timeout，且不早放", why == "timeout" and time.time() - t0 >= 0.19)
chk("  timeout 上限 ≈ turn_settle（轮询轻量，不超 0.5 s）", time.time() - t0 < 0.5)

# 5 槽数上限 12
lp, se = mk([(25, 12, 12, False)] * 5)
lp._settled_slots = (23, 12)
st, why = lp._wait_turn_ready(St(True, 0))
chk("槽数封顶 12：turn 25 指挥点 12 ⇒ ready（不要求比上次大）", why == "ready")

# 6 新局：回合号倒退 ⇒ 丢掉上局槽数
lp, se = mk([(1, 1, 1, False)] * 5)
lp._settled_slots = (21, 11)
st, why = lp._wait_turn_ready(St(True, 0))
chk("新局 turn 1：上局槽数 11 不拦", why == "ready")

# 7 偶数回合（后手）：turn 4 ⇒ 槽数 2
lp, se = mk([(4, 0, 1, False), (4, 2, 2, False), (4, 2, 2, False)])
st, why = lp._wait_turn_ready(St(True, 0))
chk("后手回合 turn 4 ⇒ 应有槽数 2", why == "ready")

# 8 轻量读一开始就读不出 ⇒ 退回旧的整快照路径
lp, se = mk([(5, 3, 3, False)], ok=False)
lp.sess.snapshot = lambda: St(False, 3)
n_snap = []
_orig = lp._wait_turn_ready_snap
lp._wait_turn_ready_snap = lambda s: (n_snap.append(1), _orig(s))[1]
st, why = lp._wait_turn_ready(St(True, 0))
chk("peek 读不出 ⇒ 走旧路径", n_snap == [1] and why == "ready")

# 9 board.MemoryBoardSource.peek_turn_state：假内存解密 + 取我方座位
import struct                                                       # noqa: E402
from kardsmem import board as BD                                    # noqa: E402
from kardsmem.gamemodel import ESide                                # noqa: E402


class FakeM:
    GS = 0x10000

    def __init__(self, kl, kr, sl, sr, turn, ap):
        key = 0x1234
        b = bytearray(BD.GS_SIDE_BLOCK_LEN)
        struct.pack_into("<i", b, BD.OFF_AGS_KEY - BD.GS_SIDE_BLOCK_OFF, key)
        for (kind, lr), off in BD.AGS_SIDES.items():
            v = {("kredit", "left"): kl, ("kredit", "right"): kr, ("slot", "left"): sl, ("slot", "right"): sr}[(kind, lr)]
            struct.pack_into("<5i", b, off - BD.GS_SIDE_BLOCK_OFF, 1, 0, 0, v ^ key, 0)
        self.b, self.turn, self.ap = bytes(b), turn, ap

    def blob(self, a, n):
        return self.b if a == self.GS + BD.GS_SIDE_BLOCK_OFF else None

    def u8(self, a):
        return int(self.ap) if a == self.GS + BD.BGS_U8["action_process"] else None

    def i32(self, a):
        return self.turn if a == self.GS + BD.BGS_I32["current_turn"] else None


src = BD.MemoryBoardSource.__new__(BD.MemoryBoardSource)
src._attach = lambda: None
src._m = FakeM(0, 3, 2, 3, 5, False)
src._locate = lambda m: (1, FakeM.GS, True, [])
pk = src.peek_turn_state(ESide.right)
chk("peek_turn_state：解密两侧指挥点/槽数，取我方(right)座位",
    pk["ok"] and pk["mine_k"] == 3 and pk["mine_slots"] == 3 and pk["kredits"][ESide.left] == 0
    and pk["turn"] == 5 and pk["action_process"] is False)
src._locate = lambda m: (None, None, None, ["x"])
chk("读不出 GameState ⇒ ok=False（不猜）", src.peek_turn_state(ESide.right)["ok"] is False)

# 8 卡牌额外加槽：turn 12 应有 6，实际 8（k==s==8）⇒ 仍放行（旧等号判据会连续 timeout）
lp, se = mk([(12, 0, 8, False), (12, 8, 8, False), (12, 8, 8, False)])
st, why = lp._wait_turn_ready(St(True, 0))
chk("额外加槽（turn 12 槽数 8 > 应有 6）⇒ ready", why == "ready")
# 9 陈旧：turn 12 但读到上回合没花完的 k==s==5（< 应有 6）⇒ 不放行
lp, se = mk([(12, 5, 5, False)] * 6)
lp.turn_settle = 0.3
st, why = lp._wait_turn_ready(St(True, 0))
chk("陈旧读数（5==5 < 应有 6）⇒ 不放行（timeout）", why == "timeout")

print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
