#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攻击执行/轮询延迟修补（离线回归，假时钟/假注入器，不碰游戏）。

2026-10-07 实机：攻击 t_exec ≈ 7.7 s。静态拆解：注入侧 `attack_card` 在发手势前又问一次 CanAttack 闸门，
闸门不带快照 ⇒ 自己全量快照 4–6 s；手势里两次悬停各 1.0 s；提交前探针 dump 箭头全部反射字段；
等对方回合每 1.0 s 才采一次快照。这里钉：
  ① `attack_card(snapshot=)` 把盘面原样交给 `game_can_attack`；不给 ⇒ 不带 snapshot（旧行为）；
  ② `Session.attack(snapshot=)` 只在有快照时才多传该参数；
  ③ `wait_our_turn(poll=)`：回合在 0.3 s 开始 ⇒ ~0.3 s 内返回（旧 1.0 s）；limit 到点不再多睡；默认 poll=1.0 不变；
  ④ 攻击悬停常量：默认 0.5 s、不低于红线 0.3 s。
"""
import os
import sys
import time as _time
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


# ---------------------------------------------------------------- ① attack_card 传快照
from ops import play as OP                                              # noqa: E402


class FakeAtk(OP.PlayMixin if hasattr(OP, "PlayMixin") else object):
    pass


def _mixin_cls():
    for n in dir(OP):
        c = getattr(OP, n)
        if isinstance(c, type) and "attack_card" in c.__dict__:
            return c
    raise RuntimeError("找不到带 attack_card 的类")


Base = _mixin_cls()


class Fake(Base):
    def __init__(self):
        self.gate_kw = []
        self.once = 0

    def resolve_target(self, t):
        return {"ok": True, "card_id": int(t), "card": None}

    def pick_pending(self):
        return {"pending": False}

    def pending_gate(self):
        return self.pick_pending()

    def game_can_attack(self, a, t, verbose=False, **kw):
        self.gate_kw.append(kw)
        return {"ok": True, "can": True}

    def find_card(self, cid):
        return None

    def _attack_once(self, c, t, verbose=True):
        self.once += 1
        return {"ok": True}


snap = object()
f = Fake()
r = f.attack_card(1, 2, verbose=False, snapshot=snap)
chk("attack_card 把 snapshot 交给闸门", f.gate_kw and f.gate_kw[0].get("snapshot") is snap and r.get("ok"), str(f.gate_kw))
f2 = Fake()
f2.attack_card(1, 2, verbose=False)
chk("不给快照 ⇒ 闸门调用不带 snapshot（旧行为）", f2.gate_kw == [{}], str(f2.gate_kw))
f3 = Fake()
f3.attack_card(1, 2, verbose=False, force=True, snapshot=snap)
chk("force=True 仍不问闸门", f3.gate_kw == [] and f3.once == 1)

# ---------------------------------------------------------------- ② Session.attack 参数透传
from agent import session as S                                          # noqa: E402
from agent import precheck as PC                                        # noqa: E402

seen = []
_orig = PC.call_write
PC.call_write = lambda fn, *a, **k: (seen.append((fn, a, k)) or {"ok": True})
try:
    s = S.AgentSession.__new__(S.AgentSession)
    s.use_game_gate = True
    s._cid = lambda x: x if isinstance(x, int) else None
    s.attack(5, 6, retry=2)
    s.attack(5, 6, retry=2, snapshot=snap)
finally:
    PC.call_write = _orig
chk("无快照时不多传参数", "snapshot" not in seen[0][2], str(seen[0]))
chk("有快照时透传 snapshot", seen[1][2].get("snapshot") is snap, str(seen[1]))

# ---------------------------------------------------------------- ③ wait_our_turn 轮询间隔
from kardsmem import board as BA                                        # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def run_wait(limit, _unused, turn_at, **kw):
    clk = Clock()
    snaps = []

    class Src:
        def snapshot(self_):
            snaps.append(clk.t)
            return NS(turn=3, our_turn=clk.t >= turn_at, kredits={1: 2}, my_side=1, match_finished=False)

    orig_open = BA.open_source
    BA.open_source = lambda *_a, **_k: Src()
    orig_t, orig_s = OP.time.time, OP.time.sleep
    OP.time.time, OP.time.sleep = clk.time, clk.sleep
    try:
        g = Fake()
        ok = g.wait_our_turn(limit=limit, verbose=False, **kw)
    finally:
        OP.time.time, OP.time.sleep = orig_t, orig_s
        BA.open_source = orig_open
    return ok, clk


ok, clk = run_wait(1.2, 0.3, 0.3, poll=0.3)
chk("poll=0.3：回合 0.3 s 开始 ⇒ 0.3 s 发现", ok and abs(clk.t - 0.3) < 1e-6, "t=%.2f" % clk.t)
ok, clk = run_wait(1.2, 1.0, 0.3)
chk("默认 poll=1.0（旧行为）：要等到 1.0 s", ok and abs(clk.t - 1.0) < 1e-6, "t=%.2f" % clk.t)
ok, clk = run_wait(0.5, 1.0, 99)
chk("limit 到点即返回，不多睡一整个 poll", (not ok) and clk.t <= 0.5 + 1e-6, "t=%.2f" % clk.t)

# ---------------------------------------------------------------- ④ 悬停常量
from ops import consts as C                                             # noqa: E402

chk("攻击悬停 ≥ 红线 0.3 s", C.SETTLE_HOVER_ATTACK >= C.SETTLE_FLOOR - 1e-9, str(C.SETTLE_HOVER_ATTACK))
if "KARDS_SETTLE_HOVER_ATTACK" not in os.environ and "KARDS_SETTLE_SCALE" not in os.environ:
    chk("攻击悬停默认 0.5 s", abs(C.SETTLE_HOVER_ATTACK - 0.5) < 1e-9)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
