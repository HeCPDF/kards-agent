#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`NativeOut`（返回值 + 出参）约定与 `Map_Find` 的离线断言（不需要游戏）。

依据 `NATIVE-COVERAGE-1.60.md` §14.5 #2：`Map_Find(Map, Key, &Value) -> bool` 这类
"返回 bool + 写出参"的原语，旧约定把 value 当返回值 ⇒ 调用方拿 value 判"找没找到"，
value=0/None 就被判成没找到。`NativeOut` 让 VM 把两者分开。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import types


from kardsmem.kismetlib import NativeOut, call as native_call   # noqa: E402
from kardsmem.vm import Frame, VM                               # noqa: E402

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def main():
    print("== A. 原语层：Map_Find 返回 (found, value) ==")
    r = native_call("BlueprintMapLibrary::Map_Find", [{"a": 0, "b": 7}, "a"])
    chk("key 存在但 value=0 ⇒ found=True, value=0", (isinstance(r, NativeOut), r[0], r[1][0]),
        (True, True, 0))
    r = native_call("BlueprintMapLibrary::Map_Find", [{"a": 0}, "zzz"])
    # 2026-10-06：缺键 ⇒ 出参写**值类型的零值**（THE BIG THREE 对空本地表无条件 Map_Find 再 +1，卡能结算只可能是缺键给 0）
    chk("key 不存在 ⇒ found=False, value=零值(0)", (r[0], r[1][0]), (False, 0))
    r = native_call("BlueprintMapLibrary::Map_Find", [{}, "zzz"])
    chk("空表缺键 ⇒ found=False, value=0", (r[0], r[1][0]), (False, 0))
    r = native_call("BlueprintMapLibrary::Map_Find", [{"a": "x"}, "zzz"])
    chk("字符串表缺键 ⇒ value=''", (r[0], r[1][0]), (False, ""))
    r = native_call("BlueprintMapLibrary::Map_Find", [None, "a"])
    chk("空 map 不炸 ⇒ found=False", r[0], False)

    print("== B. VM 层：返回值=bool、出参=value（旧约定：value 0 会被当成没找到） ==")

    def make_vm():
        v = VM.__new__(VM)
        v.s = None
        v.hooks = {}
        v.cn = None
        v._params = {}
        v.persist = {}
        return v

    def call_via_vm(mp, key):
        vm = make_vm()
        f = Frame(self_obj=0x1000)
        f.locals["M"] = mp
        f.locals["K"] = key
        e = types.SimpleNamespace(
            args={"fn": "Map_Find"},
            kids=[types.SimpleNamespace(op="LocalVariable", args={"prop": "M"}),
                  types.SimpleNamespace(op="LocalVariable", args={"prop": "K"}),
                  types.SimpleNamespace(op="LocalOutVariable", args={"prop": "V"})])
        ret = vm.call(e, f)
        # 出参写在 `frame.out`（`_write_outs` 的约定；`Frame.get` 也会先查 out）
        return ret, f.out.get("V", f.locals.get("V"))

    chk("value=0 的 key ⇒ 返回 True（不是 0）且出参写 0", call_via_vm({"a": 0}, "a"), (True, 0))
    chk("value=7 ⇒ 返回 True 且出参写 7", call_via_vm({"b": 7}, "b"), (True, 7))
    chk("key 不存在 ⇒ 返回 False 且出参写零值 0", call_via_vm({"a": 1}, "zzz"), (False, 0))

    print("%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
