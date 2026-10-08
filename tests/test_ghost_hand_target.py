# -*- coding: utf-8 -*-
"""2026-10-07 幽灵手牌选目标提示的读侧判别（离线，假内存）。

BP 证据见 ops/choices.py::ghost_reason；这里钉住：
  * widget.pendingKill / 销毁标志 ⇒ pending=False（ghost 带原因），pending_raw 仍 True；
  * 候选非空且全不合法 ⇒ ghost；
  * 活提示（有合法候选，widget 没在拆）保持 pending=True；
  * 读不出的字段（None）不当幽灵；
  * session.pending() 在 hand_target.ghost 时把同源 pick_pending 一并摘掉（有真候选行则不摘）。"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from ops.choices import ChoiceMixin, ghost_reason                    # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


class FakeMem:
    def __init__(self, data):
        self.d = data                      # addr -> bytes

    def read_exact(self, addr, n):
        for a, b in self.d.items():
            if a <= addr and addr + n <= a + len(b):
                return b[addr - a:addr - a + n]
        return None

    def ptr(self, addr):
        r = self.read_exact(addr, 8)
        return struct.unpack("<Q", r)[0] if r else None


W = 0x10000
OFFS = {"pendingKill": 0x500, "hasSelected": 0x501, "turnHasEnded": 0x502, "cardBeingPlayedQue": 0x510,
        "cardBeingPlayed": 0x394, "targetCardID": 0x390, "_cardBeingPlayedObject": 0x3B8,
        "chooseOneActive": 0x20}


class Fake(ChoiceMixin):
    def __init__(self, flags=0, pkill=0, valid=(True,), sel=1, in_vp=1):
        blob = bytearray(0x600)
        struct.pack_into("<I", blob, 0x08, flags)
        blob[OFFS["pendingKill"]] = pkill
        struct.pack_into("<i", blob, OFFS["cardBeingPlayedQue"] + 8, 0)
        struct.pack_into("<i", blob, 0x394, 19)
        self.m = FakeMem({W: bytes(blob), 0x20000: bytes([sel]) + bytes(0x100)})
        self._valid = list(valid)
        self._vp = in_vp
        self.notes = []

    def board_actor(self): return 0x20000 - 0x0                    # noqa: E704
    def hand_target_widget(self): return W                         # noqa: E704
    def selecting_hand_target(self): return True                   # noqa: E704
    def uclass_of_instance(self, o): return 1                      # noqa: E704
    def off(self, cls, name, note): return OFFS.get(name, note)    # noqa: E704
    def find_fn(self, cls, name, inherited=True): return 7         # noqa: E704
    def call_out_u8(self, obj, f): return self._vp                 # noqa: E704
    def hand_target_legal(self, cid, verbose=False):
        return {"ok": True, "is_valid": self._valid[cid - 1], "reason": ""}


import ops.choices as OC                                           # noqa: E402
OC.hand_card_actors = lambda ks: [{"card_id": i + 1, "name": "C%d" % (i + 1)} for i in range(len(FAKE_VALID))]
FAKE_VALID = (True,)


def run(**kw):
    global FAKE_VALID
    FAKE_VALID = tuple(kw.get("valid", (True,)))
    f = Fake(**kw)
    f.ks = None
    return f.hand_target_pending(verbose=False)


r = run()
chk("活提示：pending 保持 True", r["pending"] is True and not r.get("ghost"), str(r))
chk("  widget_state 带诊断字段", {"pending_kill", "in_viewport", "queue_num", "object_flags"} <= set(r["widget_state"]), str(r))
chk("  in_viewport 读到 True", r["widget_state"]["in_viewport"] is True)
r = run(pkill=1)
chk("pendingKill=True ⇒ ghost，pending False，pending_raw True",
    r["pending"] is False and r["pending_raw"] is True and "pendingKill" in r["ghost"], str(r))
r = run(flags=0x8000)
chk("RF_BeginDestroyed ⇒ ghost", r["pending"] is False and "销毁" in r["ghost"], str(r))
r = run(valid=(False, False))
chk("候选全不合法 ⇒ ghost", r["pending"] is False and "legalTargets" in r["ghost"], str(r))
r = run(valid=(False, True))
chk("有一张合法 ⇒ 活提示", r["pending"] is True, str(r))
r = run(in_vp=0)
chk("in_viewport=False 仅诊断，不单独判幽灵", r["pending"] is True and r["widget_state"]["in_viewport"] is False, str(r))
chk("ghost_reason(None 字段) 为空", ghost_reason({"pending_kill": None, "begin_destroyed": None}) == "")
chk("ghost_reason(非 dict) 为空", ghost_reason(None) == "")

# session.pending 联动
from agent import session as S                                      # noqa: E402
from agent import precheck                                          # noqa: E402


def sess_pending(ht, pp, rows):
    table = {"pick_candidates": rows, "pick_pending": pp, "card_being_played_from_hand": 0,
             "hand_target_pending": ht, "arrow_target_by_logic": None}
    orig = precheck.call_read
    precheck.call_read = lambda name, *a, **k: table[name]
    try:
        s = S.AgentSession.__new__(S.AgentSession)
        return S.AgentSession.pending(s)
    finally:
        precheck.call_read = orig


o = sess_pending({"pending": False, "pending_raw": True, "ghost": "x"}, {"pending": True}, [])
chk("ghost ⇒ pick_pending 摘掉，waiting False", o["pick_pending"]["pending"] is False and o["waiting"] is False, str(o))
o = sess_pending({"pending": False, "pending_raw": True, "ghost": "x"}, {"pending": True}, [{"index": 0}])
chk("有真候选行 ⇒ 不摘 pick_pending", o["pick_pending"]["pending"] is True, str(o))
o = sess_pending({"pending": True}, {"pending": True}, [])
chk("非 ghost 不动", o["pick_pending"]["pending"] is True and o["waiting"] is True, str(o))

print("FAIL=%d" % bad)
sys.exit(1 if bad else 0)
