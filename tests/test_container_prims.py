#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器原语补全（NATIVE-SPEC-GAPS §9/§11 D1/D2）：Array_Set/Resize/Reverse、
Set_Add/Remove/RemoveItems（保序 dict）、Map_Add/Clear，以及 Set_ToArray 的追加语义。"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from kardsmem.vm import (_a_reverse, _a_resize, _a_set, _m_add, _m_clear,
                         _s_add, _s_remove, _s_remove_items)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


# ---- Array_Set（IDA 0x143D7B0B0）----
a = [1, 2]
_a_set(a, 5, 9, True)
chk("Array_Set 越界+fit ⇒ 扩到 6 个（补 0）再写", a == [1, 2, 0, 0, 0, 9], str(a))
a = [1, 2]
_a_set(a, 5, 9, False)
chk("Array_Set 越界+不 fit ⇒ 不变", a == [1, 2], str(a))
a = [1, 2]
_a_set(a, -1, 9, True)
chk("Array_Set Index<0 ⇒ 不变", a == [1, 2], str(a))
a = [1, 2]
_a_set(a, 1, 7, False)
chk("Array_Set Index<Num ⇒ 覆盖", a == [1, 7], str(a))

# ---- Array_Resize / Reverse ----
a = [1, 2, 3]
_a_resize(a, 1)
chk("Array_Resize 变小 ⇒ 截断", a == [1], str(a))
_a_resize(a, 3)
chk("Array_Resize 变大 ⇒ 补 0", a == [1, 0, 0], str(a))
_a_resize(a, -1)
chk("Array_Resize <0 ⇒ 不变", a == [1, 0, 0], str(a))
a = [1, 2, 3]
_a_reverse(a)
chk("Array_Reverse ⇒ 反转", a == [3, 2, 1], str(a))

# ---- Set_*（保序 dict）----
s = {}
_s_add(s, 3)
_s_add(s, 3)
_s_add(s, 1)
chk("Set_Add 去重且保序", list(s.keys()) == [3, 1], str(list(s.keys())))
chk("Set_Remove 删存在项 ⇒ True", _s_remove(s, 3) is True)
chk("Set_Remove 再删 ⇒ False", _s_remove(s, 3) is False)
s = {1: None, 2: None, 3: None}
_s_remove_items(s, [2, 9])
chk("Set_RemoveItems ⇒ 去掉存在的", set(s.keys()) == {1, 3}, str(list(s.keys())))

# ---- Map_* ----
m = {}
_m_add(m, 1, 2)
_m_add(m, 1, 3)
chk("Map_Add 同键覆盖", m == {1: 3}, str(m))
_m_clear(m)
chk("Map_Clear ⇒ 空", m == {}, str(m))

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
