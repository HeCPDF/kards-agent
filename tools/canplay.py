#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canplay.py —— 拿 Kismet 求值器跑一张卡的 `CanPlayFromHand`（进程外，只读）。

这是「进程外算合法性」那条链的**端到端演示**：

    kismet.py       从内存读 UFunction 的字节码并反汇编
    props.py        反射链按名字拿字段偏移
    cards.py        把卡对象读成字段视图
    kismetlib.py    通用原生原语
    cardnatives.py  UBaseCardObject::* 原语
    vm.py           把这些拼起来跑

用法：
    python tools/canplay.py --dump <x.DMP> [--limit 8]
    python tools/canplay.py [--limit 8]          # 游戏在跑时直接 attach

★ 输出里的 `stopped` 不是 bug，是**诚实的缺口**：原语没实现 / 字段读不到时
  求值器会停下并说清原因，而不是给一个错答案（规格 §7.6f「判据只挑不判」）。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from kardsmem import cards as CARDS, kismet, props        # noqa: E402
from kardsmem.cardnatives import CardNatives              # noqa: E402
from kardsmem.kismetlib import Unimplemented              # noqa: E402
from kardsmem.objects import ObjectArray                  # noqa: E402
from kardsmem.vm import VM                                # noqa: E402

OFF_UOBJECT_CLASS = 0x10


def make_get_field(session):
    """`(对象, 字段名) -> 值`。偏移走反射链现算，值按属性类型读。**只读。**"""
    import struct
    m = session.m
    pool = session.names_pool()
    cache = {}
    # 一次空跑期间游戏内存不会变（VM 是纯的、不写回）⇒ 容器类字段（Map/Array）按 (对象, 字段) 记住，
    # 不然同一张表被反复整张重读（实测一张牌 4.5 万次内存读）。`get_field` 每次空跑新建一个 ⇒ 天然不跨调用。
    vals = {}

    def get(obj, name):
        if not obj:
            raise Unimplemented("在空对象上读 %s" % name)
        vk = (obj, name)
        if vk in vals:
            return vals[vk]
        v = _get_uncached(obj, name)
        p_ = cache.get((m.ptr_or_zero(obj + OFF_UOBJECT_CLASS), name))
        if p_ is not None and p_.get("type") in ("ArrayProperty", "MapProperty", "SetProperty"):
            vals[vk] = v
        return v

    def _get_uncached(obj, name):
        uc = m.ptr_or_zero(obj + OFF_UOBJECT_CLASS)
        key = (uc, name)
        if key not in cache:
            cache[key] = props.find_prop(session, uc, name) if uc else None
        p = cache[key]
        if p is None:
            raise Unimplemented("类里没有字段 %s" % name)
        a, t = obj + p["offset"], p["type"]
        if t == "BoolProperty":
            return props.read_bool(m, obj, p)
        if t == "IntProperty":
            return m.i32(a)
        if t in ("ByteProperty", "EnumProperty"):
            return m.u8(a)
        if t == "FloatProperty":
            return m.f32(a)
        if t == "DoubleProperty":
            d = m.read(a, 8)
            return struct.unpack("<d", d)[0] if d else None
        if t in ("ObjectProperty", "ClassProperty", "SoftObjectProperty",
                 "InterfaceProperty"):
            return m.ptr_or_zero(a)
        if t == "NameProperty":
            i = m.u32(a)
            return pool.fname(i, 0) if i else None
        if t == "ArrayProperty":
            return _read_array(session, m, a, p, name)
        if t == "MapProperty":
            return _read_map(session, m, a, p, name)
        if t == "SetProperty":
            return _read_set(session, m, a, p, name)
        if t == "StrProperty":
            # FString：{TCHAR* Data; int32 Num; int32 Max}
            data, num = m.ptr_or_zero(a), m.i32(a + 8) or 0
            if not data or num <= 1 or num > 4096:
                return ""
            raw = m.read(data, (num - 1) * 2)
            return raw.decode("utf-16-le", "replace") if raw else ""
        if t == "StructProperty":
            # 结构体字段（如随机流 `encryptionStream`）：VM 里没有结构体成员名，给一个不透明的
            # 影子字典——只读；逻辑若真的去读它的成员，会在那一步明确报停，而不是在这里就停。
            return {"__struct__": name, "__addr__": a}
        raise Unimplemented("字段 %s 的类型 %s 还没有读法" % (name, t))

    return get


OFF_ARRAY_INNER = 0x78          # FArrayProperty::InnerProperty（Basic.hpp 确认）
OFF_MAP_KEYPROP = 0x70          # FMapProperty::KeyProperty（Basic.hpp:1290）
OFF_MAP_VALPROP = 0x78          # FMapProperty::ValueProperty（Basic.hpp:1291）
OFF_STRUCTPROP_STRUCT = 0x70    # FStructProperty::Struct（Basic.hpp:1262）


def _align_up(x: int, a: int) -> int:
    return (x + a - 1) // a * a


def _read_map(session, m, addr, prop, name):
    """`TMap<K,V>` → `{key: value}`。**真正读 `AllocationFlags` 位图**（见
    `kardsmem.containers`），不是"当连续数组硬读+靠数据像不像筛"的近似——
    这条链就是 2026-09-23 撞到的那个坑（`semantics.legality.can_attack` 在
    `m1` 攻击 `ehq` 时卡在 `BP_GameState_Battle_C::CardFunctionTriggers`
    这个 `TMap<ERegisteredCardFunction, FintegerSetStruct>`）。

    ★ 空表一定读得出（跟 `_read_array` 的"空数组一定读得出"是同一条纪律）。
    ★ Key/Value 类型现算（`FMapProperty::KeyProperty`/`ValueProperty`），
      不硬编码某一张表的布局——但**支持的类型组合有限**：
        key   ∈ {ByteProperty, EnumProperty, IntProperty, ObjectProperty 族}
        value ∈ {同上 scalar 类型} 或 {StructProperty 且 struct 名字是
                 `integerSetStruct`（= `FintegerSetStruct` = `TSet<int32>`，
                 走 `kardsmem.containers.tset_int32_values`）}
      遇到没覆盖的类型组合**抛 `Unimplemented`**，不编答案——跟其它原语一个规矩。
    """
    from kardsmem import containers as CT
    field = prop.get("field")
    if not field:
        raise Unimplemented("字段 %s：MapProperty 没有 field 指针，读不到 Key/ValueProperty" % name)
    key_prop = m.ptr_or_zero(field + OFF_MAP_KEYPROP)
    val_prop = m.ptr_or_zero(field + OFF_MAP_VALPROP)
    pool = session.names_pool()
    key_t = props._field_class_name(pool, m, key_prop) if key_prop else None      # noqa: SLF001
    val_t = props._field_class_name(pool, m, val_prop) if val_prop else None      # noqa: SLF001
    key_size = m.i32(key_prop + props.OFF_FP_ELEMSIZE) if key_prop else None
    val_size = m.i32(val_prop + props.OFF_FP_ELEMSIZE) if val_prop else None
    if not key_size or not val_size:
        raise Unimplemented("字段 %s：MapProperty 的 Key/Value 类型读不出（key=%s val=%s）"
                            % (name, key_t, val_t))

    def read_key(a):
        if key_t in ("ByteProperty", "EnumProperty"):
            return m.u8(a)
        if key_t == "IntProperty":
            return m.i32(a)
        if key_t in ("ObjectProperty", "ClassProperty", "SoftObjectProperty"):
            return m.ptr_or_zero(a)
        raise Unimplemented("字段 %s：MapProperty 的 key 类型 %s 还没有读法" % (name, key_t))

    val_struct_name = None
    if val_t == "StructProperty":
        struct_ptr = m.ptr_or_zero(val_prop + OFF_STRUCTPROP_STRUCT)
        val_struct_name = pool.fname_of(struct_ptr) if struct_ptr else None
        if val_struct_name != "integerSetStruct":
            raise Unimplemented("字段 %s：MapProperty 的 value 结构体 %r 还没有读法"
                                "（目前只支持 integerSetStruct=TSet<int32>）"
                                % (name, val_struct_name))

    def read_value(a):
        if val_struct_name == "integerSetStruct":
            return CT.tset_int32_values(m, a)
        if val_t in ("ObjectProperty", "ClassProperty", "SoftObjectProperty"):
            return m.ptr_or_zero(a)
        if val_t == "IntProperty":
            return m.i32(a)
        if val_t in ("ByteProperty", "EnumProperty"):
            return m.u8(a)
        raise Unimplemented("字段 %s：MapProperty 的 value 类型 %s 还没有读法" % (name, val_t))

    # `TPair<K,V>` 布局：key 在 0，value 按自身对齐从 key 后面起（结构体值按 8
    # 对齐——目前唯一支持的结构体值 `FintegerSetStruct` 内部有指针，必须 8 对齐；
    # 标量值按"对齐==自身大小，封顶 8"这条 POD 惯例，跟已验证过的
    # `AllCardsInBattle`（`TPair<int32,ptr>`：key@0/4B，pad 4，value@8/8B，
    # 共 16B）用的是同一条规则）。元素整体再加 8 字节的 HashNextId/HashIndex。
    val_align = 8 if val_struct_name else min(val_size, 8)
    val_off = _align_up(key_size, val_align)
    elem_align = max(val_align, 4)
    tpair_size = _align_up(val_off + val_size, elem_align)
    elem_stride = _align_up(tpair_size + 8, elem_align)

    out = {}
    for elem_addr in CT.sparsearray_slots(m, addr, elem_stride):
        k = read_key(elem_addr)
        if k is None:
            continue
        out[k] = read_value(elem_addr + val_off)
    return out


OFF_SET_ELEMPROP = 0x70         # FSetProperty::ElementProp（与 FMapProperty::KeyProp 同偏移）


def _read_set(session, m, addr, prop, name):
    """`TSet<int32>` → Python list（`GameState.FrontlineLimiters` 就是这种；USS YORKTOWN 链要读它）。

    ★ 只支持元素是 4 字节整型的集合（元素 `{int32; HashNextId; HashIndex}` 12 字节，
      `kardsmem.containers.tset_int32_values` 已有）；别的元素类型 ⇒ 抛 `Unimplemented`，不编。
    """
    from kardsmem import containers as CT
    field = prop.get("field")
    elem = m.ptr_or_zero(field + OFF_SET_ELEMPROP) if field else 0
    pool = session.names_pool()
    et = props._field_class_name(pool, m, elem) if elem else None   # noqa: SLF001
    size = m.i32(elem + props.OFF_FP_ELEMSIZE) if elem else None
    if et == "IntProperty" and size == 4:
        return CT.tset_int32_values(m, addr)
    raise Unimplemented("字段 %s：TSet 的元素类型 %s(size=%s) 还没有读法（目前只支持 TSet<int32>）"
                        % (name, et, size))


def _read_array(session, m, addr, prop, name):
    """`TArray<T>` → Python list。

    ★ **空数组一定读得出**（Num==0 ⇒ `[]`），这一条就够很多判据往下走了
      （`IsThereGameplayRestriction` 里的 `GameplayRestrictionEffects` 平时就是空的）。
    ★ 非空且元素类型我们不会读 ⇒ **抛 `Unimplemented`，不编**。
      宁可停下说"这个内层类型没读法"，也不要返回一个长度对、内容错的列表。
    """
    head = m.read_exact(addr, 0x10)
    if not head:
        raise Unimplemented("字段 %s：TArray 头读不出" % name)
    data, num, _mx = struct.unpack("<QiI", head)
    if num == 0:
        return []
    if num < 0 or num > 4096:
        raise Unimplemented("字段 %s：TArray 长度 %d 不合理" % (name, num))
    inner = m.ptr_or_zero(prop["field"] + OFF_ARRAY_INNER) if prop.get("field") else 0
    pool = session.names_pool()
    it = props._field_class_name(pool, m, inner) if inner else None   # noqa: SLF001
    size = m.i32(inner + props.OFF_FP_ELEMSIZE) if inner else None
    if it in ("ObjectProperty", "ClassProperty", "SoftObjectProperty") and size == 8:
        return [m.ptr_or_zero(data + i * 8) for i in range(num)]
    if it == "IntProperty" and size == 4:
        return [m.i32(data + i * 4) for i in range(num)]
    raise Unimplemented("字段 %s：TArray 有 %d 个元素，内层类型 %s 还没有读法"
                        % (name, num, it))


def make_view(session):
    c = {}

    def view(ptr):
        if ptr not in c:
            c[ptr] = CARDS.read_raw(session, ptr) or {}
        return c[ptr]
    return view


def hook_targeted(has_target, target):
    """`GetTargetedCard(self, &hasTarget, &card)` —— 玩家**当前指向**谁。

    ★ 这是 UI 态，内存里没有"鼠标正指着哪张卡"，所以必须由调用方喂进来。
      也正因如此，`CanPlayFromHand` 只能回答"指向这张合不合法"，
      不能替你回答"该指谁" —— 那要遍历候选逐个试。
    """
    def fn(vm, frame, obj, args, e):
        names = [k.args.get("prop") for k in e.kids]
        for n, v in zip(names[1:], (has_target, target)):
            if n:
                frame.locals[n] = v
        return None
    return fn


def collect(session, limit):
    """→ [(类名, 卡对象, UFunction)]，只取类上有 CanPlayFromHand 覆写的活卡。"""
    oa = ObjectArray(session)
    pool = oa.pool()
    seen, out = {}, []
    for p in oa.iter_objects():
        c = oa.class_of(p)
        if not c:
            continue
        if c not in seen:
            seen[c] = (pool.fname_of(c), kismet.find_function(session, c, "CanPlayFromHand"))
        nm, fn = seen[c]
        if fn and nm and nm.startswith("card_"):
            out.append((nm, p, fn))
            if len(out) >= limit:
                break
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="进程外跑 CanPlayFromHand")
    ap.add_argument("--dump", help="minidump 路径；不给就 attach 活进程")
    ap.add_argument("--limit", type=int, default=8)
    a = ap.parse_args(argv)

    if a.dump:
        from dumpmem import DumpSession
        s = DumpSession(a.dump)
    else:
        from kardsmem import attach
        s = attach()

    rows = collect(s, a.limit)
    if not rows:
        print("没找到有 CanPlayFromHand 覆写的活卡对象")
        return 1
    get_field, view = make_get_field(s), make_view(s)
    # 指向目标：优先挑一张**单位**，否则大部分卡的"有目标"分支和"无目标"看不出区别
    # （指令牌当目标时本来就该被拒）。
    view_ = view
    target = next((p for _n, p, _f in rows
                   if (view_(p) or {}).get("card_type") in
                   ("tank", "infantry", "artillery", "antiair", "antitank",
                    "tankdestroyer", "fighter", "bomber")), rows[-1][1])
    print("指向目标：%#x（%s）\n" % (target, (view_(target) or {}).get("card_type")))
    ok = stopped = 0
    for nm, obj, fn in rows:
        print("=== %-44s obj=%#x" % (nm, obj))
        for label, has, tgt in (("有目标", True, target), ("无目标", False, 0)):
            vm = VM(s, get_field=get_field,
                    card_natives=CardNatives(None, view=view),
                    hooks={"GetTargetedCard": hook_targeted(has, tgt)})
            r = vm.run(fn, self_obj=obj)
            if r["stopped"]:
                stopped += 1
                print("   [%s] 停在：%s" % (label, r["stopped"]))
            else:
                ok += 1
                o = r["out"]
                print("   [%s] canIt=%-5s reason=%r"
                      % (label, o.get("canIt"), o.get("Reason")))
    print("\n跑完 %d 次，停下 %d 次" % (ok, stopped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
