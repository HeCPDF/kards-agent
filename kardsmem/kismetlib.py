#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.kismetlib —— Kismet **原生函数**的 Python 实现（求值器的原语表）。

背景
====
`kismet.py` 能把蓝图字节码反汇编出来，但字节码里有一半的调用落到**原生函数**
（`FUNC_Native`，逻辑在 exe 里，没有字节码）：全游戏 23478 个 `UFunction` 里
11853 个是原生的。要在进程外跑蓝图逻辑，这些必须自己实现。

实现范围是**数据驱动**的，不靠猜：`tools/native_calls.json` 是把全游戏 10861 个
有字节码的函数反汇编一遍数出来的调用频次表。本模块按那张表从高到低实现。

    KismetMathLibrary    160 个 / 21861 次
    KismetArrayLibrary    24 个 /  9092 次
    BaseCardObject       120 个 /  6429 次   ← 游戏自有，在 `cardnatives.py`
    KismetStringLibrary   39 个 /  4141 次
    KismetSystemLibrary   71 个 /  3030 次

语义从哪来
==========
`H:\\EpicGames\\UE_5.6\\Engine\\Source\\Runtime\\Engine\\Classes\\Kismet\\KismetMathLibrary.inl`
等引擎源码**逐个对照**，不是凭印象。标量语义（回绕/截断/除零）统一走 `uetypes`，
因为 Python 的 `//`、`%`、`/` 和 C++ 的都不一样（见该模块开头那张表）。

纯度
====
求值器必须是**纯的**（红线：不写游戏内存）。所以原语分三类：

* `PURE`   —— 纯计算，随便调
* `READS`  —— 要读游戏内存/对象（如 `IsValid`），由调用方注入上下文
* `IMPURE` —— **有副作用**（`Delay` / `PrintString` / `Set*PropertyByName` / 定时器）。
  这些**不实现**，查到就抛 `ImpureCall`，让求值器**响亮地失败**，
  而不是悄悄返回 None 把错误结果算下去。

    from kardsmem.kismetlib import call, ImpureCall, Unimplemented
    call("KismetMathLibrary::Divide_IntInt", [7, 2])      # -> 3
    call("KismetMathLibrary::Divide_IntInt", [7, 0])      # -> 0
    call("KismetSystemLibrary::Delay", [0.5])             # -> ImpureCall
"""
from __future__ import annotations

from . import uetypes as U


class Unimplemented(KeyError):
    """这个原生函数还没实现 —— 是覆盖缺口，不是崩溃。"""


class ImpureCall(RuntimeError):
    """有副作用的原生函数。求值器是纯的，不能执行它。"""


PURE: dict = {}
IMPURE: set = set()


def _reg(name):
    def deco(fn):
        PURE[name] = fn
        return fn
    return deco


def _many(prefix, table):
    for k, fn in table.items():
        PURE["%s::%s" % (prefix, k)] = fn


# --------------------------------------------------------------------------
# KismetMathLibrary
# --------------------------------------------------------------------------
_math = {
    # --- bool ---
    "BooleanAND": lambda a, b: bool(a) and bool(b),
    "BooleanOR": lambda a, b: bool(a) or bool(b),
    "BooleanNAND": lambda a, b: not (bool(a) and bool(b)),
    "BooleanXOR": lambda a, b: bool(a) != bool(b),
    "Not_PreBool": lambda a: not bool(a),
    "EqualEqual_BoolBool": lambda a, b: bool(a) == bool(b),
    "NotEqual_BoolBool": lambda a, b: bool(a) != bool(b),

    # --- int32 ---
    "Add_IntInt": lambda a, b: U.i32(U.i32(a) + U.i32(b)),
    "Subtract_IntInt": lambda a, b: U.i32(U.i32(a) - U.i32(b)),
    "Multiply_IntInt": lambda a, b: U.i32(U.i32(a) * U.i32(b)),
    "Divide_IntInt": U.div_i32,
    "Percent_IntInt": U.mod_i32,
    "Less_IntInt": lambda a, b: U.i32(a) < U.i32(b),
    "Greater_IntInt": lambda a, b: U.i32(a) > U.i32(b),
    "LessEqual_IntInt": lambda a, b: U.i32(a) <= U.i32(b),
    "GreaterEqual_IntInt": lambda a, b: U.i32(a) >= U.i32(b),
    "EqualEqual_IntInt": lambda a, b: U.i32(a) == U.i32(b),
    "NotEqual_IntInt": lambda a, b: U.i32(a) != U.i32(b),
    "Abs_Int": lambda a: U.i32(abs(U.i32(a))),
    "Min": lambda a, b: min(U.i32(a), U.i32(b)),
    "Max": lambda a, b: max(U.i32(a), U.i32(b)),
    "Clamp": lambda v, lo, hi: U.i32(U.clamp(U.i32(v), U.i32(lo), U.i32(hi))),
    "SelectInt": lambda a, b, pick: U.i32(a) if bool(pick) else U.i32(b),
    "SignOfInteger": lambda a: (0 if U.i32(a) == 0 else (1 if U.i32(a) > 0 else -1)),

    # --- int64 ---
    "Add_Int64Int64": lambda a, b: U.i64(U.i64(a) + U.i64(b)),
    "Subtract_Int64Int64": lambda a, b: U.i64(U.i64(a) - U.i64(b)),
    "Multiply_Int64Int64": lambda a, b: U.i64(U.i64(a) * U.i64(b)),
    "Divide_Int64Int64": U.div_i64,
    "Percent_Int64Int64": U.mod_i64,
    "Less_Int64Int64": lambda a, b: U.i64(a) < U.i64(b),
    "Greater_Int64Int64": lambda a, b: U.i64(a) > U.i64(b),
    "EqualEqual_Int64Int64": lambda a, b: U.i64(a) == U.i64(b),
    "NotEqual_Int64Int64": lambda a, b: U.i64(a) != U.i64(b),

    # --- byte（uint8）---
    "Add_ByteByte": lambda a, b: U.u8(U.u8(a) + U.u8(b)),
    "Subtract_ByteByte": lambda a, b: U.u8(U.u8(a) - U.u8(b)),
    "Multiply_ByteByte": lambda a, b: U.u8(U.u8(a) * U.u8(b)),
    "Divide_ByteByte": U.div_u8,
    "Percent_ByteByte": U.mod_u8,
    "EqualEqual_ByteByte": lambda a, b: U.u8(a) == U.u8(b),
    "NotEqual_ByteByte": lambda a, b: U.u8(a) != U.u8(b),
    "Less_ByteByte": lambda a, b: U.u8(a) < U.u8(b),
    "Greater_ByteByte": lambda a, b: U.u8(a) > U.u8(b),
    "LessEqual_ByteByte": lambda a, b: U.u8(a) <= U.u8(b),
    "GreaterEqual_ByteByte": lambda a, b: U.u8(a) >= U.u8(b),

    # --- double（UE5 蓝图的 Float 引脚其实是 double）---
    "Add_DoubleDouble": lambda a, b: float(a) + float(b),
    "Subtract_DoubleDouble": lambda a, b: float(a) - float(b),
    "Multiply_DoubleDouble": lambda a, b: float(a) * float(b),
    "Divide_DoubleDouble": U.div_f,
    "Less_DoubleDouble": lambda a, b: float(a) < float(b),
    "Greater_DoubleDouble": lambda a, b: float(a) > float(b),
    "LessEqual_DoubleDouble": lambda a, b: float(a) <= float(b),
    "GreaterEqual_DoubleDouble": lambda a, b: float(a) >= float(b),
    "EqualEqual_DoubleDouble": lambda a, b: float(a) == float(b),
    "NotEqual_DoubleDouble": lambda a, b: float(a) != float(b),
    "Abs": lambda a: abs(float(a)),
    "FMin": lambda a, b: min(float(a), float(b)),
    "FMax": lambda a, b: max(float(a), float(b)),
    "FClamp": lambda v, lo, hi: U.clamp(float(v), float(lo), float(hi)),
    "SelectFloat": lambda a, b, pick: float(a) if bool(pick) else float(b),
    "Lerp": U.lerp,
    "Multiply_IntFloat": lambda a, b: U.i32(a) * float(b),
    "Multiply_DoubleInt": lambda a, b: float(a) * U.i32(b),

    # --- 转换 ---
    "Conv_IntToBool": lambda a: U.i32(a) != 0,
    "Conv_BoolToInt": lambda a: 1 if bool(a) else 0,
    "Conv_BoolToFloat": lambda a: 1.0 if bool(a) else 0.0,
    "Conv_BoolToByte": lambda a: 1 if bool(a) else 0,
    "Conv_IntToInt64": lambda a: U.i64(U.i32(a)),
    "Conv_Int64ToInt": lambda a: U.i32(U.i64(a)),
    "Conv_IntToDouble": lambda a: float(U.i32(a)),
    "Conv_IntToFloat": lambda a: float(U.i32(a)),
    "Conv_IntToByte": lambda a: U.u8(U.i32(a)),
    "Conv_ByteToInt": lambda a: U.i32(U.u8(a)),
    "Conv_ByteToDouble": lambda a: float(U.u8(a)),
    "Conv_ByteToFloat": lambda a: float(U.u8(a)),
    "Conv_DoubleToInt": U.trunc_to_i32,
    "Conv_DoubleToInt64": lambda a: U.i64(U.trunc_to_i32(a)),
    "Conv_DoubleToFloat": U.f32,
    "Conv_FloatToDouble": lambda a: float(a),
    "FTrunc": U.trunc_to_i32,
    "FTrunc64": lambda a: U.i64(int(float(a))),
    "Round": U.round_half_away,
    "FFloor": lambda a: U.i32(__import__("math").floor(float(a))),
    "FCeil": lambda a: U.i32(__import__("math").ceil(float(a))),

    # --- name ---
    "EqualEqual_NameName": lambda a, b: _name_eq(a, b),
    "NotEqual_NameName": lambda a, b: not _name_eq(a, b),
}


def _name_eq(a, b) -> bool:
    """FName 比较**不分大小写**（引擎按 comparison index 比，索引本身就是大小写无关的）。

    我们这边 FName 已经被解析成字符串，所以按 casefold 比 —— 用 `==` 会漏判。
    """
    if a is None or b is None:
        return a is b
    return str(a).casefold() == str(b).casefold()


_many("KismetMathLibrary", _math)


# --------------------------------------------------------------------------
# KismetArrayLibrary —— 蓝图数组就是 Python list
# --------------------------------------------------------------------------
def _arr_get(arr, i):
    """`Array_Get` —— 越界在引擎里会记一条警告并返回**零值**，**不抛**。

    ★ 这里返回 None 表示零值。求值器拿到 None 要当"默认值"处理，
      别当成"读失败"——否则会把正常的越界分支误判成缺陷。
    """
    if not arr or not (0 <= i < len(arr)):
        return None
    return arr[i]


_array = {
    "Array_Get": _arr_get,
    "Array_Length": lambda arr: U.i32(len(arr or [])),
    "Array_LastIndex": lambda arr: U.i32(len(arr or []) - 1),
    "Array_IsValidIndex": lambda arr, i: 0 <= U.i32(i) < len(arr or []),
    "Array_IsEmpty": lambda arr: not (arr or []),
    "Array_IsNotEmpty": lambda arr: bool(arr or []),
    "Array_Contains": lambda arr, item: item in (arr or []),
    "Array_Find": lambda arr, item: U.i32((arr or []).index(item)
                                          if item in (arr or []) else -1),
    "Array_Identical": lambda a, b: list(a or []) == list(b or []),
}
_many("KismetArrayLibrary", _array)

# 会改数组的那几个（`Array_Add` / `Array_Clear` / …）不放进 PURE：
# 它们改的是**求值器影子堆里的本地数组**，语义正确，但必须由求值器自己执行
# （它才知道该改哪一份），所以留给求值器，不在这里注册。
MUTATING_ARRAY = {
    "Array_Add", "Array_AddUnique", "Array_Append", "Array_Clear", "Array_Insert",
    "Array_Remove", "Array_RemoveItem", "Array_Set", "Array_Resize", "Array_Reverse",
    "Array_Shuffle", "Array_ShuffleFromStream", "Array_Swap", "Array_Random",
    "SetArrayPropertyByName",
}


# --------------------------------------------------------------------------
# KismetStringLibrary
# --------------------------------------------------------------------------
def _s(x) -> str:
    return "" if x is None else str(x)


_string = {
    "Concat_StrStr": lambda a, b: _s(a) + _s(b),
    "EqualEqual_StrStr": lambda a, b: _s(a) == _s(b),
    "NotEqual_StrStr": lambda a, b: _s(a) != _s(b),
    # ★ 名字里的 `Stri` = case-**i**nsensitive。`NotEqual_StriStri` 是全游戏第 12 高频
    #   的原生调用（1484 次），按大小写敏感实现会大面积算错。
    "EqualEqual_StriStri": lambda a, b: _s(a).casefold() == _s(b).casefold(),
    "NotEqual_StriStri": lambda a, b: _s(a).casefold() != _s(b).casefold(),
    "Len": lambda a: U.i32(len(_s(a))),
    "IsEmpty": lambda a: _s(a) == "",
    "ToLower": lambda a: _s(a).lower(),
    "ToUpper": lambda a: _s(a).upper(),
    "Contains": lambda a, sub, *_x: _s(sub) in _s(a),
    "StartsWith": lambda a, pre, *_x: _s(a).startswith(_s(pre)),
    "EndsWith": lambda a, suf, *_x: _s(a).endswith(_s(suf)),
    "Conv_IntToString": lambda a: str(U.i32(a)),
    "Conv_Int64ToString": lambda a: str(U.i64(a)),
    "Conv_ByteToString": lambda a: str(U.u8(a)),
    "Conv_BoolToString": lambda a: "true" if bool(a) else "false",
    "Conv_NameToString": _s,
    "Conv_StringToName": _s,
    "Conv_StringToInt": lambda a: _str_to_int(a),
    "Trim": lambda a: _s(a).lstrip(),
    "TrimTrailing": lambda a: _s(a).rstrip(),
    "IsNumeric": lambda a: _s(a).lstrip("+-").replace(".", "", 1).isdigit(),
    "JoinStringArray": lambda arr, sep="": _s(sep).join(_s(x) for x in (arr or [])),
    "GetCharacterArrayFromString": lambda a: list(_s(a)),
    "FindSubstring": lambda a, sub, *_x: U.i32(_s(a).find(_s(sub))),
}


def _str_to_int(a) -> int:
    """`Conv_StringToInt` —— UE 的 `FCString::Atoi`：**解析不动就给 0**，不抛。"""
    t = _s(a).strip()
    neg = t.startswith("-")
    if neg or t.startswith("+"):
        t = t[1:]
    d = ""
    for ch in t:
        if not ch.isdigit():
            break
        d += ch
    if not d:
        return 0
    return U.i32(-int(d) if neg else int(d))


_many("KismetStringLibrary", _string)


# --------------------------------------------------------------------------
# 有副作用 / 需要引擎上下文的 —— **明确拒绝**，不要静默返回
# --------------------------------------------------------------------------
IMPURE |= {
    "KismetSystemLibrary::Delay", "KismetSystemLibrary::RetriggerableDelay",
    "KismetSystemLibrary::PrintString", "KismetSystemLibrary::PrintText",
    "KismetSystemLibrary::LogString", "KismetSystemLibrary::StackTrace",
    "KismetSystemLibrary::ExecuteConsoleCommand", "KismetSystemLibrary::QuitGame",
    "KismetSystemLibrary::LaunchURL",
    "KismetSystemLibrary::K2_SetTimerDelegate",
    "KismetSystemLibrary::K2_ClearAndInvalidateTimerHandle",
    "KismetSystemLibrary::LoadAsset", "KismetSystemLibrary::LoadAsset_Blocking",
}
# 所有 Set*PropertyByName 都是**写对象**，一律拒绝（红线）
IMPURE |= {"KismetSystemLibrary::Set%sPropertyByName" % t for t in (
    "Int", "Bool", "Text", "Object", "Name", "Byte", "String", "Double",
    "Float", "Structure", "SoftObject", "Class", "Vector", "Rotator",
    "LinearColor", "Transform", "Collision", "Array", "Int64")}
IMPURE |= {"KismetArrayLibrary::SetArrayPropertyByName",
           "BlueprintMapLibrary::SetMapPropertyByName",
           "BlueprintSetLibrary::SetSetPropertyByName"}

# 随机：**不是**纯函数（会推进引擎的随机流）。求值器要么注入种子，要么拒绝。
NONDETERMINISTIC = {
    "KismetMathLibrary::RandomFloatInRange", "KismetMathLibrary::RandomIntegerInRange",
    "KismetMathLibrary::RandomInteger", "KismetMathLibrary::RandomFloat",
    "KismetMathLibrary::RandomBool", "KismetArrayLibrary::Array_Random",
    "KismetArrayLibrary::Array_Shuffle",
}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def arity(name: str):
    """某个原语声明的**入参个数**。拿不到返回 None。

    ★ Kismet 把**出参也压进调用点**（`Array_Get(arr, i, &Item)` 在字节码里是
      一个带 3 个参数的调用，而我们的实现只收 2 个）⇒ 调用前必须按入参个数切开。
      `cardnatives` 早就这么做了，通用原语这边先前漏了 —— 实机跑 `CanAttack`
      时炸出 `_arr_get() takes 2 positional arguments but 3 were given`。
    """
    fn = PURE.get(name)
    if fn is None:
        return None
    try:
        import inspect
        sig = inspect.signature(fn)
        if any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values()):
            return None                      # *args ⇒ 不切
        return len(sig.parameters)
    except (TypeError, ValueError):           # pragma: no cover
        return None


def call(name: str, args: list):
    """按 `"Class::Function"` 调用。未实现/不纯都**抛**，绝不静默返回 None。"""
    if name in IMPURE:
        raise ImpureCall("%s 有副作用，纯求值器不能执行它" % name)
    fn = PURE.get(name)
    if fn is None:
        raise Unimplemented(name)
    n = arity(name)
    return fn(*(args[:n] if n is not None else args))


def coverage(freq: dict) -> dict:
    """拿 `tools/native_calls.json` 的 calls 表算覆盖率（按**调用次数**加权）。

    按次数而不是按函数个数：实现 10 个高频的比实现 100 个冷门的有用得多。
    """
    # 延迟 import：cardnatives 反过来要用本模块的 Unimplemented，模块级会循环。
    from .cardnatives import implemented as _card_impl
    card = _card_impl()

    tot = done = 0
    missing = {}
    for k, v in freq.items():
        tot += v
        owner, _, fn = k.rpartition("::")
        if (k in PURE or k in IMPURE or k in NONDETERMINISTIC or k in READS
                or fn in MUTATING_ARRAY or fn in MUTATING_MAP or fn in MUTATING_SET
                or (owner == "BaseCardObject" and fn in card)):
            done += v
        else:
            missing[k] = v
    return {"total_calls": tot, "covered_calls": done,
            "pct": round(100.0 * done / tot, 1) if tot else 0.0,
            "missing_top": sorted(missing.items(), key=lambda kv: -kv[1])[:25]}


# --------------------------------------------------------------------------
# 第二波：容器 / 字符串补齐 / 几何
# --------------------------------------------------------------------------
# 蓝图的 TMap/TSet 在求值器里就是 dict/set。读操作放这里，写操作同数组一样留给求值器。
_map = {
    "Map_Find": lambda m, k: (m or {}).get(k),
    "Map_Contains": lambda m, k: k in (m or {}),
    "Map_Length": lambda m: U.i32(len(m or {})),
    "Map_Keys": lambda m: list((m or {}).keys()),
    "Map_Values": lambda m: list((m or {}).values()),
    "Map_IsNotEmpty": lambda m: bool(m or {}),
    "Map_IsEmpty": lambda m: not (m or {}),
}
_many("BlueprintMapLibrary", _map)
MUTATING_MAP = {"Map_Add", "Map_Remove", "Map_Clear", "SetMapPropertyByName",
                "Map_GetKeyValueByIndex"}

_set = {
    "Set_Contains": lambda s, x: x in (s or set()),
    "Set_Length": lambda s: U.i32(len(s or set())),
    "Set_ToArray": lambda s: list(s or set()),
    "Set_IsNotEmpty": lambda s: bool(s or set()),
    "Set_IsEmpty": lambda s: not (s or set()),
    "Set_Union": lambda a, b: set(a or set()) | set(b or set()),
    "Set_Intersection": lambda a, b: set(a or set()) & set(b or set()),
    "Set_Difference": lambda a, b: set(a or set()) - set(b or set()),
}
_many("BlueprintSetLibrary", _set)
MUTATING_SET = {"Set_Add", "Set_AddItems", "Set_Remove", "Set_RemoveItems",
                "Set_Clear", "SetSetPropertyByName"}


def _split(s, sep, mode=0, cs=0):
    """`KismetStringLibrary::Split` —— 按**首次**出现切成 (左, 右)。

    引擎返回 bool + 两个出参；这里返回 (found, left, right)，由求值器分派到出参上。
    """
    t, d = _s(s), _s(sep)
    i = t.find(d) if d else -1
    if i < 0:
        return (False, t, "")
    return (True, t[:i], t[i + len(d):])


def _parse_into_array(s, sep=",", cull_empty=True):
    parts = _s(s).split(_s(sep) or ",")
    return [p for p in parts if p] if cull_empty else parts


_many("KismetStringLibrary", {
    "Split": _split,
    "ParseIntoArray": _parse_into_array,
    "Replace": lambda s, frm, to, *_x: _s(s).replace(_s(frm), _s(to)),
    "ReplaceInline": lambda s, frm, to, *_x: _s(s).replace(_s(frm), _s(to)),
    "LeftChop": lambda s, n: _s(s)[:max(0, len(_s(s)) - U.i32(n))],
    "RightChop": lambda s, n: _s(s)[U.i32(n):],
    "Left": lambda s, n: _s(s)[:max(0, U.i32(n))],
    "Right": lambda s, n: _s(s)[len(_s(s)) - max(0, U.i32(n)):],
    "GetSubstring": lambda s, start, cnt: _s(s)[U.i32(start):U.i32(start) + U.i32(cnt)],
    "GetCharacterAsNumber": lambda s, i: U.i32(ord(_s(s)[U.i32(i)])
                                               if 0 <= U.i32(i) < len(_s(s)) else 0),
    "Conv_StringToDouble": lambda s: _str_to_float(s),
})


def _str_to_float(a) -> float:
    """`FCString::Atof` —— 解析不动给 0.0，不抛。"""
    t = _s(a).strip()
    try:
        return float(t)
    except ValueError:
        neg = t.startswith("-")
        if neg or t.startswith("+"):
            t = t[1:]
        d = ""
        for ch in t:
            if ch.isdigit() or (ch == "." and "." not in d):
                d += ch
            else:
                break
        if not d or d == ".":
            return 0.0
        return -float(d) if neg else float(d)


# 几何：向量/变换在合法性判定里用不到，但它们在字节码里到处都是（MakeTransform 499 次）。
# 实现成朴素元组即可 —— 求值器只需要"能算下去、不炸"，不需要渲染精度。
_geom = {
    "MakeVector": lambda x, y, z: (float(x), float(y), float(z)),
    "BreakVector": lambda v: tuple(float(c) for c in (v or (0, 0, 0))),
    "MakeVector2D": lambda x, y: (float(x), float(y)),
    "BreakVector2D": lambda v: tuple(float(c) for c in (v or (0, 0))),
    "MakeRotator": lambda p, y, r: (float(p), float(y), float(r)),
    "BreakRotator": lambda v: tuple(float(c) for c in (v or (0, 0, 0))),
    "MakeTransform": lambda loc, rot, scale=(1.0, 1.0, 1.0): (loc, rot, scale),
    "BreakTransform": lambda t: (t or ((0, 0, 0), (0, 0, 0), (1, 1, 1))),
    "Add_VectorVector": lambda a, b: tuple(float(x) + float(y) for x, y in zip(a, b)),
    "Subtract_VectorVector": lambda a, b: tuple(float(x) - float(y) for x, y in zip(a, b)),
    "Multiply_VectorFloat": lambda v, f: tuple(float(x) * float(f) for x in v),
    "VLerp": lambda a, b, t: tuple(U.lerp(x, y, t) for x, y in zip(a, b)),
}
_many("KismetMathLibrary", _geom)


# --------------------------------------------------------------------------
# READS —— 需要读游戏对象才能答的原语
# --------------------------------------------------------------------------
# 这些**不是**纯计算，但也**不写**任何东西，所以求值器可以执行 —— 前提是调用方
# 注入一个上下文（怎么判断一个对象指针有效、怎么读卡牌字段）。
# 注册在这里只是为了让 coverage() 把它们算作"有着落"，实际实现由求值器注入。
READS = {
    "KismetSystemLibrary::IsValid",
    "KismetSystemLibrary::IsValidClass",
    "KismetSystemLibrary::GetObjectName",
    "KismetSystemLibrary::GetDisplayName",
    "KismetSystemLibrary::GetClassDisplayName",
    "KismetSystemLibrary::DoesImplementInterface",
    "KismetNodeHelperLibrary::GetEnumeratorUserFriendlyName",
}
# `BaseCardObject::*`（120 个 / 6429 次）是游戏自有的原生函数。它们读的都是
# 我们**已经在读**的字段（type / location / side / 攻防 / 关键词），所以能在进程外重算。
# 这是下一块工作，接口同 READS：由求值器按卡对象上下文注入。
READS_TODO_PREFIX = ("BaseCardObject::", "MatchControllerV2::", "FunctionLibrary::")


# --------------------------------------------------------------------------
# 自检：全是**与 Python 原生行为不同**的点（相同的地方没必要测）
# --------------------------------------------------------------------------
CASES = [
    ("Divide_IntInt 除零给0", "KismetMathLibrary::Divide_IntInt", [7, 0], 0),
    ("Divide_IntInt 向零截断", "KismetMathLibrary::Divide_IntInt", [-7, 2], -3),
    ("Percent_IntInt 符号随被除数", "KismetMathLibrary::Percent_IntInt", [-7, 2], -1),
    ("Add_IntInt 回绕", "KismetMathLibrary::Add_IntInt", [2147483647, 1], -2147483648),
    ("Add_ByteByte 回绕", "KismetMathLibrary::Add_ByteByte", [250, 10], 4),
    ("Divide_DoubleDouble 除零给0", "KismetMathLibrary::Divide_DoubleDouble", [5.0, 0.0], 0.0),
    # Stri = case-Insensitive；全游戏第 12 高频调用，搞错会大面积算错
    ("NotEqual_StriStri 不分大小写", "KismetStringLibrary::NotEqual_StriStri", ["Abc", "aBC"], False),
    ("NotEqual_StrStr 分大小写", "KismetStringLibrary::NotEqual_StrStr", ["Abc", "aBC"], True),
    ("FName 比较不分大小写", "KismetMathLibrary::EqualEqual_NameName", ["Guard", "guard"], True),
    ("Atoi 部分解析", "KismetStringLibrary::Conv_StringToInt", ["12abc"], 12),
    ("Atoi 解析不动给0", "KismetStringLibrary::Conv_StringToInt", ["abc"], 0),
    ("Array_Get 越界给零值", "KismetArrayLibrary::Array_Get", [[1, 2], 9], None),
    ("Map_Find 缺键给零值", "BlueprintMapLibrary::Map_Find", [{"a": 1}, "b"], None),
    ("Conv_IntToBool", "KismetMathLibrary::Conv_IntToBool", [0], False),
]

RAISES = [
    ("Delay 有副作用应拒绝", "KismetSystemLibrary::Delay", [1.0], ImpureCall),
    ("SetIntPropertyByName 写对象应拒绝",
     "KismetSystemLibrary::SetIntPropertyByName", [0, "x", 1], ImpureCall),
    ("未实现的应抛而不是静默", "BaseCardObject::IsUnit", [None], Unimplemented),
]


def selftest() -> int:
    from . import uetypes
    from . import cardnatives
    bad = uetypes.selftest() + cardnatives.selftest()
    for name, fn, args, want in CASES:
        got = call(fn, args)
        ok = got == want and type(got) is type(want)
        bad += 0 if ok else 1
        print("  [%s] %-28s got=%r want=%r"
              % ("PASS" if ok else "FAIL", name, got, want))
    for name, fn, args, exc in RAISES:
        try:
            call(fn, args)
            print("  [FAIL] %-28s 没抛 %s" % (name, exc.__name__))
            bad += 1
        except exc:
            print("  [PASS] %s" % name)
    print("kismetlib selftest: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
