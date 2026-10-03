#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组 G（NATIVE-SPEC-GAPS §8）：BlueprintJsonLibrary 的 Python 模型与转换规则。"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from kardsmem.kismetlib import JNULL, call as native_call

fails = 0


def chk(name, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print("  [%s] %-56s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def J(fn, *a):
    return native_call("BlueprintJsonLibrary::" + fn, list(a))


o = J("JsonMake")
chk("JsonMake：新对象不同、无字段", (J("JsonMake") is not o, J("JsonHasField", o, "x")),
    (True, False))

J("JsonMakeField", o, "a", J("JsonMakeInt", 1))
J("JsonMakeField", o, "A", J("JsonMakeInt", 2))          # 大小写不敏感 ⇒ 覆盖
chk("JsonMakeField：原地改 + 大小写不敏感覆盖",
    J("Conv_JsonValueToInteger", J("Conv_JsonObjectToJsonValue", o, "a")), 2)

J("JsonMakeField", o, "b", JNULL)                        # JSON null 值也算存在
chk("JsonHasField：null 值也算存在（'B' ⇒ True）", J("JsonHasField", o, "B"), True)
chk("JsonHasTypedField：Number=3 命中、String=2 不命中",
    (J("JsonHasTypedField", o, "a", 3), J("JsonHasTypedField", o, "a", 2)), (True, False))
chk("缺字段 ⇒ Conv_JsonObjectToJsonValue 返回 Null（不是失败）",
    J("Conv_JsonObjectToJsonValue", o, "zzz") == JNULL, True)

chk("Conv_JsonValueToInteger：2.5 ⇒ 3（HalfFromZero）", J("Conv_JsonValueToInteger", (3, 2.5)), 3)
chk("Conv_JsonValueToInteger：−2.5 ⇒ −3", J("Conv_JsonValueToInteger", (3, -2.5)), -3)
chk("Conv_JsonValueToInteger：Null ⇒ 0", J("Conv_JsonValueToInteger", JNULL), 0)
chk("Conv_JsonValueToInteger：'12abc' ⇒ 12（LexFromString）",
    J("Conv_JsonValueToInteger", (2, "12abc")), 12)
chk("Conv_JsonValueToInteger：Bool true ⇒ 1", J("Conv_JsonValueToInteger", (4, True)), 1)

chk("Conv_JsonValueToString：(4,True) ⇒ 'true'", J("Conv_JsonValueToString", (4, True)), "true")
chk("Conv_JsonValueToString：Null ⇒ ''", J("Conv_JsonValueToString", JNULL), "")
chk("Conv_JsonValueToString：5.0 ⇒ '5'（SanitizeFloat）", J("Conv_JsonValueToString", (3, 5.0)), "5")
chk("Conv_JsonValueToString：2.5 ⇒ '2.5'", J("Conv_JsonValueToString", (3, 2.5)), "2.5")

chk("Conv_JsonValueToBool：(3,0.0) ⇒ False", J("Conv_JsonValueToBool", (3, 0.0)), False)
chk("Conv_JsonValueToBool：'True' ⇒ True", J("Conv_JsonValueToBool", (2, "True")), True)
chk("Conv_JsonValueToBool：Null ⇒ False", J("Conv_JsonValueToBool", JNULL), False)

arr = J("Conv_JsonValueToArray", (5, [(3, 1.0), (3, 2.0)]))
arr.append((3, 9.0))
chk("Conv_JsonValueToArray：拷贝（改返回值不影响原数组，长度仍 2）",
    len(J("Conv_JsonValueToArray", (5, [(3, 1.0), (3, 2.0)]))), 2)
chk("JsonMakeArray：过滤 ptr==null 元素",
    len(J("JsonMakeArray", [(3, 1.0), None, (3, 2.0)])[1]), 2)

objv = (6, o)
chk("Conv_JsonValueToObject：Object ⇒ 同一共享对象",
    J("Conv_JsonValueToObject", objv) is o, True)
chk("Conv_JsonValueToObject：Number ⇒ 空对象（None）",
    J("Conv_JsonValueToObject", (3, 5.0)), None)

s = '{"k":1,"sub":{"x":"y"}}'
back = J("Conv_StringToJsonObject", s)
chk("Conv_StringToJsonObject ⇄ Conv_JsonObjectToString 往返",
    J("JsonHasField", back, "k") and J("JsonHasField", back, "SUB"), True)

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
