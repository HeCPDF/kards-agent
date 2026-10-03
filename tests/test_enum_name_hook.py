#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""effectvm 求值链的 `GetEnumeratorUserFriendlyName` 钩子回归（离线）。

背景（A8 核实，2026-10-02）：这个原语以前只有 `semantics/legality.py` 的 VM 提供，
`effectvm.make_read_hooks` 里没有 ⇒ 卡（如 KYOTO REGIMENT）在**盘外评估**里一问枚举名
就整条停。这里钉住：ETypeEnum 的取值能翻译成人话、别的枚举/越界**如实抛**。
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import semantics.effectvm as EV                                   # noqa: E402
from kardsmem.kismetlib import Unimplemented                   # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    st = SimpleNamespace(cards=[], turn=3, my_side_raw=1, slots={}, kredits={})
    hooks = EV.make_read_hooks(st, 1)
    chk("钩子已提供", "GetEnumeratorUserFriendlyName" in hooks, str(len(hooks)))
    fn = hooks["GetEnumeratorUserFriendlyName"]
    vm = SimpleNamespace(s=None)                    # 名字直接是字符串时用不到 session

    got = fn(vm, None, None, ["ETypeEnum", 4], None)
    chk("ETypeEnum 4 ⇒ fighter", got == "fighter", repr(got))
    got = fn(vm, None, None, ["ETypeEnum", 0], None)
    chk("ETypeEnum 0 ⇒ NotAvailable", got == "NotAvailable", repr(got))
    try:
        fn(vm, None, None, ["EFactionEnum", 1], None)
        chk("别的枚举 ⇒ 抛 Unimplemented", False, "没抛")
    except Unimplemented as exc:
        chk("别的枚举 ⇒ 抛 Unimplemented", "ETypeEnum" in str(exc), str(exc)[:60])
    try:
        fn(vm, None, None, ["ETypeEnum", 99], None)
        chk("越界 ⇒ 抛 Unimplemented", False, "没抛")
    except Unimplemented as exc:
        chk("越界 ⇒ 抛 Unimplemented", "超出" in str(exc), str(exc)[:60])

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
