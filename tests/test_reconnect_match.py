#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`IsReconnectMatch` 护栏 + `APPROX` 接线的离线回归（不碰游戏）。

背景（TODO A7，2026-10-02）：
  * 三个出参是**函数参数**，反射找不到 ⇒ 只能按可读字段做**有护栏**的判定；
    实测确认正常对局里 `mulliganReplacementReceived=true`、`MulliganData` 非零
    （不能当重连判据），只有 `reconnectInSameTurn` / `reconnectLoading` 是干净信号。
  * `vm.py` 用 `getattr(self.cn, "APPROX", ())` 标"掺了近似"，但 `CardNatives` 以前
    **没有 APPROX 属性** ⇒ 近似标记从来没生效过（本测试一并钉住）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import kardsmem.objects as O                                   # noqa: E402
import kardsmem.props as P                                     # noqa: E402
from kardsmem import cardnatives as CN                         # noqa: E402
from kardsmem.kismetlib import Unimplemented                   # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _Mem:
    def __init__(self):
        self.u8map, self.ptrmap = {}, {}

    def u8(self, addr):
        return self.u8map.get(addr, 0)

    def ptr_or_zero(self, addr):
        return self.ptrmap.get(addr, 0)

    def read(self, addr, n):
        return b"\x00" * int(n)


class _Ks:
    base = 0                                  # ObjectArray(session) 只用到 .m / .base

    def __init__(self, mem):
        self.m = mem

    def names_pool(self):
        return None


PROPS = [{"name": "reconnectInSameTurn", "type": "BoolProperty", "offset": 760},
         {"name": "reconnectLoading", "type": "ObjectProperty", "offset": 4104}]


def _setup(mem):
    CN._match_controller = lambda ks: 0x1000
    O.ObjectArray.class_of = lambda self, p: 0x2000
    P.class_props = lambda session, uc, pool=None, **k: list(PROPS)
    P.read_bool = lambda m, obj, pr: bool(m.u8map.get(obj + pr["offset"], 0))
    return CN.CardNatives(ks=_Ks(mem))


def main():
    # ① 正常局（两个信号都灭）⇒ (False, False, False)
    mem = _Mem()
    cn = _setup(mem)
    got = CN._is_reconnect_match(cn)
    chk("干净信号 ⇒ 三个出参全 False", got == (False, False, False), repr(got))

    # ② reconnectLoading 非空 ⇒ 抛缺口（不猜重连局的分支）
    mem = _Mem()
    mem.ptrmap[0x1000 + 4104] = 0x9999
    cn = _setup(mem)
    try:
        CN._is_reconnect_match(cn)
        chk("reconnectLoading 非空 ⇒ 抛 Unimplemented", False, "没抛")
    except Unimplemented as exc:
        chk("reconnectLoading 非空 ⇒ 抛 Unimplemented", "reconnectLoading" in str(exc), str(exc)[:80])

    # ③ reconnectInSameTurn=true ⇒ 抛缺口
    mem = _Mem()
    mem.u8map[0x1000 + 760] = 1
    cn = _setup(mem)
    try:
        CN._is_reconnect_match(cn)
        chk("reconnectInSameTurn=true ⇒ 抛 Unimplemented", False, "没抛")
    except Unimplemented as exc:
        chk("reconnectInSameTurn=true ⇒ 抛 Unimplemented",
            "reconnectInSameTurn" in str(exc), str(exc)[:80])

    # ④ APPROX 接线（vm.py 是 getattr(self.cn, "APPROX", ())）
    cn2 = CN.CardNatives()
    chk("CardNatives 实例带 APPROX 属性", isinstance(getattr(cn2, "APPROX", None), (set, frozenset)),
        repr(getattr(cn2, "APPROX", None)))
    chk("IsReconnectMatch 登记进 APPROX", "IsReconnectMatch" in getattr(cn2, "APPROX", ()),
        repr(getattr(cn2, "APPROX", None)))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
