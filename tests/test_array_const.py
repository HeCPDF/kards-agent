#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ArrayConst` 求值回归（离线）。

2026-10-02 实机日志（22:17 那局）暴露：`ArrayConst` 反汇编早就有（`kismet.py` 把元素挂在
`kids`），但 **VM 的 `eval` 没有这个分支** ⇒ ORP GENERAL HALLER / BOMBING RAID 一撞上就
`Unsupported: 表达式 opcode ArrayConst`（整条效果链丢）。补分支後用真实卡复验：
ORP GENERAL HALLER → `{"convert": true}`、BOMBING RAID → `{"damage": 3, "damage_aoe": 2}`。

这里钉住求值语义：数组字面量 = 逐个求值 kids（含空数组、嵌套）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem.kismet import Expr                                 # noqa: E402
from kardsmem.vm import Frame, VM                                # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def _const(v):
    e = Expr("IntConst", 0)
    e.args["v"] = v
    return e


def _arr(*vals):
    e = Expr("ArrayConst", 0)
    e.kids = [_const(v) for v in vals]
    return e


def main():
    class _Sess:                       # `kismet.Resolve(session)` 只要 `.m` / `.names_pool()`
        m = object()

        def names_pool(self):
            return None

    vm = VM(_Sess())
    f = Frame(None, {})
    chk("ArrayConst 三个元素", vm.eval(_arr(1, 2, 3), f) == [1, 2, 3], repr(vm.eval(_arr(1, 2, 3), f)))
    chk("ArrayConst 空数组", vm.eval(_arr(), f) == [], repr(vm.eval(_arr(), f)))
    inner = _arr(7, 8)
    outer = Expr("ArrayConst", 0)
    outer.kids = [_const(0), inner]
    got = vm.eval(outer, f)
    chk("ArrayConst 嵌套（元素本身是数组）", got == [0, [7, 8]], repr(got))
    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
