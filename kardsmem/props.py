#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.props —— 走 UE5 反射链，**按名字**把蓝图成员变量解析成偏移。

为什么要有这一层
================
`ABP_Board_C` / `BP_HandCard_C` 这些是纯 `UBlueprintGeneratedClass`：exe 里**零个**
原生函数，字段布局不是静态写死的，是运行时按 `FProperty` 链算出来的。
所以"某个蓝图变量在对象里的偏移"这个问题，IDA 答不了，FModel 也只给声明顺序
（对不上偏移：对齐、bool 位域打包、父类大小都会插进来）。**唯一的权威是运行时的属性链。**

这也是本轮那条被撤回的结论（`BP_HandCard_C+0x924` 当成换牌标记）本该走的路：
与其 diff 字节再猜语义，不如直接问引擎"`shouldDiscard` 在哪"。

    from kardsmem import attach
    from kardsmem.props import class_props, find_prop
    s = attach()
    for p in class_props(s, uclass):      # → [{name, offset, type, size, bit_*}]
        ...
    find_prop(s, uclass, "shouldDiscard")

只读：只有 ReadProcessMemory。`session` 只需要 `.m`（读接口）和 `.names_pool()`，
所以 `DumpSession`（minidump 当内存源）一样能用。

布局（UE 5.6）
==============
    UStruct  +0x40 SuperStruct   +0x48 Children(UField*)   +0x50 ChildProperties(FField*)
             +0x58 PropertiesSize(i32)  ← 已在 world.py 独立确认，拿它当锚点自检
    FField   +0x08 ClassPrivate  +0x10 Owner(8 字节带标记位)  +0x18 Next  +0x20 NamePrivate
    FProperty+0x30 ArrayDim  +0x34 ElementSize  +0x38 PropertyFlags  +0x44 Offset_Internal
    FBoolProperty +0x70 FieldSize +0x71 ByteOffset +0x72 ByteMask +0x73 FieldMask
    FFieldClass +0x00 Name(FName)

真实地址 = obj + Offset_Internal + ByteOffset，取 `byte & ByteMask`。
"""
from __future__ import annotations

from typing import Optional

# --- UStruct ---
OFF_SUPER = 0x40
OFF_CHILDREN = 0x48
OFF_CHILD_PROPS = 0x50
OFF_PROPSIZE = 0x58

# --- FField ---（★ 本 Fork 的 `FFieldVariant Owner` 只占 **8 字节**：
#     指针的最低位当 bIsUObject 标记（实测 Owner=uclass|1）。
#     虚幻官方常见布局里 Owner 是 16 字节，照抄会让 0x10 之后**整体错位 8**——
#     踩过：按 Next=0x20/Name=0x28 走，108 个属性的链只走出 8 个，名字全是垃圾。）
OFF_FF_CLASS = 0x08        # FFieldClass*
OFF_FF_OWNER = 0x10        # 带标记位的指针
OFF_FF_NEXT = 0x18
OFF_FF_NAME = 0x20         # FName

# --- FProperty ---
OFF_FP_ARRAYDIM = 0x30
OFF_FP_ELEMSIZE = 0x34
OFF_FP_FLAGS = 0x38
OFF_FP_OFFSET = 0x44       # Offset_Internal

# --- FBoolProperty（相对 FField 起始）---
# 实测 BP_HandCard_C 里 18 个 BoolProperty 的这 4 字节**全是** `01 00 01 FF`
# ⇒ 蓝图 bool 各占一整字节，从不位域打包。仍按掩码读，别假设。
OFF_FB_FIELDSIZE = 0x70
OFF_FB_BYTEOFFSET = 0x71
OFF_FB_BYTEMASK = 0x72
OFF_FB_FIELDMASK = 0x73

MAX_CHAIN = 4096          # 属性链够长就当它坏了，别在野指针上转圈


def _fname(pool, mem, addr: int) -> Optional[str]:
    """FName @ addr → 字符串。"""
    if not addr:
        return None
    idx = mem.u32(addr)
    num = mem.u32(addr + 4)
    if idx is None:
        return None
    try:
        return pool.fname(idx, num or 0)
    except Exception:
        return None


def _field_class_name(pool, mem, field: int) -> Optional[str]:
    fc = mem.ptr_or_zero(field + OFF_FF_CLASS)
    return _fname(pool, mem, fc) if fc else None


def struct_props(session, ustruct: int, pool=None) -> list:
    """**只**这一层（不含父类）的属性 → [{...}]，按链表顺序。"""
    mem = session.m
    pool = pool or session.names_pool()
    out, f, n = [], mem.ptr_or_zero(ustruct + OFF_CHILD_PROPS), 0
    while f and n < MAX_CHAIN:
        n += 1
        rec = {
            "field": f,
            "name": _fname(pool, mem, f + OFF_FF_NAME),
            "type": _field_class_name(pool, mem, f),
            "offset": mem.i32(f + OFF_FP_OFFSET),
            "size": mem.i32(f + OFF_FP_ELEMSIZE),
            "array_dim": mem.i32(f + OFF_FP_ARRAYDIM),
            "flags": mem.u64(f + OFF_FP_FLAGS),
        }
        if rec["type"] == "BoolProperty":
            rec["bit_field_size"] = mem.u8(f + OFF_FB_FIELDSIZE)
            rec["bit_byte_offset"] = mem.u8(f + OFF_FB_BYTEOFFSET)
            rec["bit_byte_mask"] = mem.u8(f + OFF_FB_BYTEMASK)
            rec["bit_field_mask"] = mem.u8(f + OFF_FB_FIELDMASK)
        out.append(rec)
        f = mem.ptr_or_zero(f + OFF_FF_NEXT)
    return out


def class_props(session, uclass: int, pool=None, max_depth: int = 16) -> list:
    """连父类一起走（子类在前，父类在后）。"""
    mem = session.m
    pool = pool or session.names_pool()
    out, c, d = [], uclass, 0
    while c and d < max_depth:
        d += 1
        for r in struct_props(session, c, pool):
            r["owner"] = c
            r["owner_propsize"] = mem.i32(c + OFF_PROPSIZE)
            out.append(r)
        c = mem.ptr_or_zero(c + OFF_SUPER)
    return out


def find_prop(session, uclass: int, name: str, pool=None) -> Optional[dict]:
    """按名字找一个属性（子类优先）。"""
    for r in class_props(session, uclass, pool):
        if r.get("name") == name:
            return r
    return None


def read_bool(mem, obj: int, prop: dict) -> Optional[bool]:
    """按 FBoolProperty 的描述读一个 bool（独立 bool 和位域都吃）。"""
    if prop.get("type") != "BoolProperty":
        return None
    a = obj + (prop["offset"] or 0) + (prop.get("bit_byte_offset") or 0)
    b = mem.u8(a)
    if b is None:
        return None
    mask = prop.get("bit_byte_mask") or 0xFF
    return bool(b & mask)


def sanity(session, uclass: int, pool=None) -> dict:
    """自检：**每个属性都必须落在 PropertiesSize 里**。

    这是判断"偏移表对不对"的硬判据 —— 如果 OFF_FP_OFFSET 猜错了，
    读出来的 offset 会是 ElementSize/Flags 的碎片，立刻越界或出现大量重复 0。
    """
    mem = session.m
    ps = mem.i32(uclass + OFF_PROPSIZE)
    rows = class_props(session, uclass, pool)
    bad = [r for r in rows
           if r["offset"] is None or r["size"] is None
           or r["offset"] < 0 or r["offset"] + r["size"] > (ps or 0)]
    named = [r for r in rows if r.get("name")]
    return {"propsize": ps, "count": len(rows), "named": len(named),
            "out_of_range": len(bad),
            "ok": bool(rows) and not bad and len(named) == len(rows)}
