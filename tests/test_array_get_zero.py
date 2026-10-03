#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`Array_Get` / `ArrayGetByRef` 越界应给**元素类型的零值**（离线）。

2026-10-02 实机（IJN AKAGI）：链上 `Greater_IntInt(ArrayGetByRef(cardsIDs, 0), 0)`，
`cardsIDs` 为空 ⇒ 旧实现返回 None ⇒ `int(None)` TypeError ⇒ 整条链断。
UE 语义：`Array_Get` 越界返回元素类型的默认值（int→0、bool→false、float→0.0、
string→""、对象→null）。这里按"已见元素类型"推断（空数组按 int→0）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import kismetlib as KL                            # noqa: E402
from kardsmem.kismet import Expr                                # noqa: E402
from kardsmem.vm import Frame, VM                               # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    chk("库：int 数组越界 → 0", KL.call("KismetArrayLibrary::Array_Get", [[1, 2], 9]) == 0)
    chk("库：空数组 → 0", KL.call("KismetArrayLibrary::Array_Get", [[], 0]) == 0)
    chk("库：string 数组越界 → ''",
        KL.call("KismetArrayLibrary::Array_Get", [["a"], 9]) == "")
    chk("库：float 数组越界 → 0.0",
        KL.call("KismetArrayLibrary::Array_Get", [[1.5], 9]) == 0.0)
    chk("库：正常下标原样返回",
        KL.call("KismetArrayLibrary::Array_Get", [[7, 8], 1]) == 8)

    class _Sess:
        m = object()

        def names_pool(self):
            return None

    vm = VM(_Sess())
    f = Frame(None, {})

    def _arr_ref(*vals):
        e = Expr("ArrayGetByRef", 0)
        arr = Expr("ArrayConst", 0)
        arr.kids = [Expr("IntConst", 0) for _ in vals]
        for k, v in zip(arr.kids, vals):
            k.args["v"] = v
        idx = Expr("IntConst", 0)
        idx.args["v"] = 9
        e.kids = [arr, idx]
        return e

    chk("VM：非空 int 数组越界 → 0", vm.eval(_arr_ref(1, 2), f) == 0)
    chk("VM：空数组越界 → 0", vm.eval(_arr_ref(), f) == 0)
    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
