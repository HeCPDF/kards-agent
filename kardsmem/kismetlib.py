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
* `IMPURE` —— **有副作用**（`Delay` / `Set*PropertyByName` / 定时器 / 控制台命令）。
  这些**不实现**，查到就抛 `ImpureCall`，让求值器**响亮地失败**，
  而不是悄悄返回 None 把错误结果算下去。
  ★ 例外（2026-10-02）：**只写日志**的三个（`PrintString` / `PrintText` / `LogString`）
  按 **no-op** 实现 —— 它们改不了任何游戏状态，而卡牌链里常有"先把这步打印出来"的调试
  节点；在打印处停会让后面的**真实效果**（例：`CRUISER SCOUTS` 的 `SetCardsSeenByCipher`）
  白丢。语义上 no-op 与真机一致（真机写完日志继续跑）。

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


class FallThrough:
    """**软钩子**的"我不接管"返回值（2026-10-06）：`vm.hooks[名]` 返回 `FALLTHROUGH` ⇒ VM 当这个钩子不存在，
    继续走后面的分派（游戏自有原语 / 通用原语 / 蓝图字节码 / 虚调用）。

    为什么要有它：硬钩子（返回别的值）会**整个**接管同名函数；而有的原生函数只有**一部分调用**要我们接管
    （例：`FetchCardFromCardID(id)`——脚本刚生成的新牌 id 要换成我们的卡句柄，真牌 id 照走游戏自己的字节码）。
    热重载会让本模块里出现两个 `FallThrough` 类对象 ⇒ VM 按**类名**认（同 `NativeOut`）。"""
    __slots__ = ()

    def __repr__(self) -> str:
        return "FALLTHROUGH"


FALLTHROUGH = FallThrough()


def is_fallthrough(v) -> bool:
    return v is FALLTHROUGH or type(v).__name__ == "FallThrough"


class NativeOut(tuple):
    """原生原语的"**返回值 + 出参**"约定：`NativeOut(ret, *outs)`。

    ★ 2026-10-02（NATIVE-COVERAGE §14.5 #2）：UE 里不少原语**同时**有返回值和出参，
      典型 `Map_Find(Map, Key, &Value) -> bool`。旧约定只能返回一个值 —— 我们把它当
      返回值写回去，调用方拿"值"当 bool 判"找没找到"：value=0/None 就被误判成没找到。
      VM 见过这个类型就会：`outs` 逐个写出参、`ret` 当调用表达式的返回值。
    """
    __slots__ = ()

    def __new__(cls, ret, *outs):
        return super().__new__(cls, (ret, tuple(outs)))


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

# ★ 2026-10-02 IDA 全量普查（Claude，`NATIVE-COVERAGE-1.60.md`）：字节码里调的是**小写**
#   `abs` / `round` / `max` / `min`（约 100 处），而表里只有大写变体 ⇒ 这些调用以前**全部落空**
#   （静默 None，分支走偏）。这里补小写别名，按参数类型分派（int 走整数实现、float 走浮点实现）。
_lower_math = {
    "abs": lambda a: (U.i32(abs(U.i32(a))) if isinstance(a, int) else abs(float(a))),
    "min": lambda a, b: (min(U.i32(a), U.i32(b))
                         if isinstance(a, int) and isinstance(b, int)
                         else min(float(a), float(b))),
    "max": lambda a, b: (max(U.i32(a), U.i32(b))
                         if isinstance(a, int) and isinstance(b, int)
                         else max(float(a), float(b))),
    "round": U.round_half_away,
}
for _k, _fn in _lower_math.items():
    PURE.setdefault(_k, _fn)                       # 不带前缀的裸名
    PURE.setdefault("KismetMathLibrary::" + _k, _fn)


# --------------------------------------------------------------------------
# KismetArrayLibrary —— 蓝图数组就是 Python list
# --------------------------------------------------------------------------
def zero_like(arr):
    """越界/空数组的**零值**：按已见元素推断（UE 的 `Array_Get` 返回元素类型的默认值：
    int→0、bool→false、float→0.0、string→""、对象→null）。

    ★ 2026-10-02（IJN AKAGI 实机）：旧实现一律返回 None，而调用方
    `Greater_IntInt(Array_Get(...), 0)` 会 `int(None)` 抛 TypeError ⇒ 整条链断。
    """
    if not arr:
        return 0
    x = arr[0]
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return 0
    if isinstance(x, float):
        return 0.0
    if isinstance(x, str):
        return ""
    return None


def _arr_get(arr, i):
    """`Array_Get` —— 越界在引擎里会记一条警告并返回**零值**，**不抛**。

    零值按元素类型给（见 `zero_like`）——这条以前实现成 None，导致
    `Greater_IntInt(None, 0)` 在 VM 里炸（IJN AKAGI 实机）。
    """
    if not arr or not (0 <= i < len(arr)):
        return zero_like(arr)
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

# 对象有效性：求值器里对象就是非空指针（求值器看不到"已销毁但指针还在"的对象）。
# 蓝图里小写的 clamp（数值类型不定）：通用夹取
_many("KismetMathLibrary", {"clamp": lambda v, lo, hi: min(max(v, lo), hi)})
_many("KismetSystemLibrary", {
    "GetFrameCount": lambda: 0,          # 只做去重/计时用；纯求值里恒 0
    "GetGameTimeInSeconds": lambda *a: 0.0,
    "IsValid": lambda o: bool(o),
    "IsValidClass": lambda o: bool(o),
    # ★ 2026-10-02（第 2 局）：`IsSimulatingInEditor(WorldContext)` —— Shipping 客户端
    #   永远不在编辑器里 ⇒ 恒 False（SOUL OF OLD JAPAN 的链上卡点）。
    "IsSimulatingInEditor": lambda *a: False,
})

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
    # ★ 2026-10-02 复核（SDK 签名）：Contains(SearchIn, Substring, bool bUseCase=false, bool bSearchFromEnd=false) —— 默认**忽略大小写**；
    #   StartsWith/EndsWith/Replace 的 ESearchCase：CaseSensitive=0 / IgnoreCase=1，BP 默认 IgnoreCase。
    #   以前一律按大小写敏感比，字节码显式传 false/1 时会算错。
    "Contains": lambda a, sub, use_case=False, from_end=False: (
        _s(sub) in _s(a) if bool(use_case) else _s(sub).casefold() in _s(a).casefold()),
    "StartsWith": lambda a, pre, case=1: (
        _s(a).startswith(_s(pre)) if int(case) == 0 else _s(a).casefold().startswith(_s(pre).casefold())),
    "EndsWith": lambda a, suf, case=1: (
        _s(a).endswith(_s(suf)) if int(case) == 0 else _s(a).casefold().endswith(_s(suf).casefold())),
    "Conv_IntToString": lambda a: str(U.i32(a)),
    "Conv_Int64ToString": lambda a: str(U.i64(a)),
    "Conv_ByteToString": lambda a: str(U.u8(a)),
    "Conv_BoolToString": lambda a: "true" if bool(a) else "false",
    "Conv_NameToString": _s,
    "Conv_StringToName": _s,
    # ★ 2026-10-02（A7 验证时撞到）：UE 里 FString / FText 是**两种类型**，蓝图节点会显式转换
    #   （`CRUISER SCOUTS` 的打出链在 `CreateAction_AddSubAction` 里就有 `Conv_StringToText`）。
    #   我们的表示统一是 Python `str` ⇒ 这几个是恒等/格式化，**不掺任何状态量**。
    #   注意带 `*_` 的那几个（UE 里 IntToText 是 5 个形参）：`arity()` 见到 VAR_POSITIONAL
    #   会返回 None ⇒ 不切出参，正合这些"只有返回值、没有出参"的库函数。
    "Conv_StringToText": _s,
    "Conv_TextToString": _s,
    "Conv_NameToText": _s,
    "Conv_IntToText": lambda a, *_: str(U.i32(a)),
    "Conv_ByteToText": lambda a, *_: str(U.u8(a)),
    "Conv_BoolToText": lambda a, *_: "true" if bool(a) else "false",
    "Conv_StringToInt": lambda a: _str_to_int(a),
    "Trim": lambda a: _s(a).lstrip(),
    "TrimTrailing": lambda a: _s(a).rstrip(),
    "IsNumeric": lambda a: _s(a).lstrip("+-").replace(".", "", 1).isdigit(),
    "JoinStringArray": lambda arr, sep="": _s(sep).join(_s(x) for x in (arr or [])),
    "GetCharacterArrayFromString": lambda a: list(_s(a)),
    "FindSubstring": lambda a, sub, *_x: U.i32(_s(a).find(_s(sub))),
    # ★ 2026-10-02（A7 验证）：`Format(InFormat, InArgs) -> FText` —— **只喂 action 描述文本**，
    #   不参与任何状态量（`CRUISER SCOUTS` 的打出链在 `CreateAction_AddSubAction` 里撞到它，
    #   不实现就整条断在"拼描述"这一步、后面的 `SetCardsSeenByCipher` 到不了）。
    #   实现口径：按 `FormatArgumentData{ArgumentName, ArgumentValue}` 替换 `{名字}`；
    #   认不出/缺字段的占位符**原样留着**，宁可文本难看也不抛错断链（文本不进求值状态）。
    "Format": lambda fmt, args=None: _kismet_format(fmt, args),
}


def _kismet_format(fmt, args=None) -> str:
    """`KismetStringLibrary::Format` 的**文本专用**实现（见 `_string["Format"]` 的注释）。

    `args` 是 `FormatArgumentData` 结构体列表（VM 里结构体是 dict）：
    取 `ArgumentName` 当占位符名、`ArgumentValue` 当值，把 `{名字}` 换掉；
    认不出的占位符原样保留。**不碰任何状态量**，出错也不抛（返回原串）。
    """
    text = _s(fmt)
    m = {}
    for a in (args or []):
        if isinstance(a, dict):
            nm = a.get("ArgumentName")
            if nm is None:
                nm = a.get("argumentName")
            v = a.get("ArgumentValue")
            if v is None:
                v = a.get("argumentValue")
        elif isinstance(a, (tuple, list)) and len(a) >= 2:
            nm, v = a[0], a[1]
        else:
            continue
        if nm is not None:
            m[_s(nm)] = v
    if not m:
        return text
    try:
        import re

        def _sub(mo):
            key = mo.group(1)
            return _s(m[key]) if key in m else mo.group(0)

        return re.sub(r"\{([^{}]*)\}", _sub, text)
    except Exception:                                         # noqa: BLE001
        return text


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
    "KismetSystemLibrary::StackTrace",
    "KismetSystemLibrary::ExecuteConsoleCommand", "KismetSystemLibrary::QuitGame",
    "KismetSystemLibrary::LaunchURL",
    "KismetSystemLibrary::K2_SetTimerDelegate",
    "KismetSystemLibrary::K2_ClearAndInvalidateTimerHandle",
    "KismetSystemLibrary::LoadAsset", "KismetSystemLibrary::LoadAsset_Blocking",
}
# ★ 2026-10-02：只写日志的三个 —— **no-op**，见模块 docstring 的例外说明。
#   （真机是"写日志然后继续跑"；在打印处停下会丢后面的真实效果。）
PURE |= {"KismetSystemLibrary::%s" % n: (lambda *a, **k: None)
         for n in ("PrintString", "PrintText", "LogString")}
# ★ 2026-10-02（CRUISER SCOUTS 实机复验）：游戏自己的日志库 `ClientLoggerFunctions::LogError`
#   （全游戏 38 次调用，census2/calls_raw.json）也是**只写日志**：VM 以前一撞就断，
#   整条链停在"打个错误日志"上。同 PrintString 的口径按 no-op 处理（改不了任何状态）。
PURE |= {"ClientLoggerFunctions::LogError": (lambda *a, **k: None)}
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
    hit = _ARITY_MEMO.get(name)
    if hit is not None and hit[0] is fn:       # 按函数对象身份记（测试/热重载换了实现就自然失效）
        return hit[1]
    try:
        import inspect
        sig = inspect.signature(fn)
        if any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values()):
            n = None                         # *args ⇒ 不切
        else:
            n = len(sig.parameters)
    except (TypeError, ValueError):           # pragma: no cover
        return None
    _ARITY_MEMO[name] = (fn, n)               # `inspect.signature` 每次 ~0.1 ms，VM 里每个调用点都问一次（一次建 sim 数千次）
    return n


_ARITY_MEMO: dict = {}


def call(name: str, args: list):
    """按 `"Class::Function"` 调用。未实现/不纯都**抛**，绝不静默返回 None。"""
    if name in IMPURE:
        raise ImpureCall("%s 有副作用，纯求值器不能执行它" % name)
    fn = PURE.get(name)
    if fn is None:
        raise Unimplemented(name)
    n = arity(name)
    return fn(*(args[:n] if n is not None else args))


# cardnatives 里实现的函数所属的类（BaseCardObject + BySide 族的库/GameState 类）
_CARDNATIVE_OWNERS = ("BaseCardObject", "CombatHelperFunctions", "kardsGameState",
                      "GameStateAccessSubsystem", "kardsGameMode")


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
                or (owner in _CARDNATIVE_OWNERS and fn in card)):
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
def _map_find(m, k):
    """`Map_Find(Map, Key, &Value)`：命中 ⇒ (True, 值)；**缺键 ⇒ (False, 值类型的零值)**，不是 None。
    原版证据：THE BIG THREE（card_event_the_big_three.cpp:42-46）对**本地空 `factionCount`** 无条件 `Map_Find(...)` 再
    `Add_IntInt(Value, 1)` —— 不看返回的"找没找到"，卡能正常结算只可能是缺键时出参被写成默认值 0（UE `GenericMap_Find`
    缺键走 `InitializeValue(OutValue)`）。值类型按已有值推断（`zero_like`）；空表推不出类型 ⇒ 0（VM 里空指针也是 0）。"""
    m = m or {}
    if k in m:
        return NativeOut(True, m[k])
    return NativeOut(False, zero_like(list(m.values())))


_map = {
    # ★ Map_Find(Map, Key, &Value) -> bool：**返回"找没找到"，value 走出参** ——
    #   不能只返回 value（value=0/None 会被调用方当成"没找到"），见 NativeOut。
    "Map_Find": lambda m, k: _map_find(m, k),
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


# --------------------------------------------------------------------------
# 组 G：BlueprintJsonLibrary（NATIVE-SPEC-GAPS §8；纯函数 + 影子对象）
#   FBlueprintJsonObject = TSharedPtr<FJsonObject>、FBlueprintJsonValue = TSharedPtr<FJsonValue>；
#   在求值器里用 JObj / (type, payload) 元组表示（共享引用语义：MakeField/RemoveField 原地改）。
#   EJsonType：None0 Null1 String2 Number3 Boolean4 Array5 Object6。
#   键比较**大小写不敏感**；`Conv_JsonObjectToJsonValue` 对缺失字段返回**新的 Null 值**（不是失败）。
#   JSON null 值（JNULL）本身是"存在"的（HasField=True）。
# --------------------------------------------------------------------------
class JObj:
    """`FBlueprintJsonObject` 的 Python 模型：`d = {fold(key): (原键, jvalue)}`（保序）。"""

    __slots__ = ("d",)

    def __init__(self):
        self.d = {}


JNULL = (1, None)                       # EJsonType::Null


def _jfold(s) -> str:
    return str(s).casefold() if s is not None else ""


def _rhz(x) -> int:
    """`RoundHalfFromZero`（IDA/UE 数值转换）：±0.5 都往**远离零**的方向取整。"""
    import math
    x = float(x)
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


def _lex_int(s) -> int:
    """`LexFromString<int>` 的近似：开头的带符号整数；非数字 ⇒ 0（游戏失败不报错）。"""
    import re
    m = re.match(r"\s*([+-]?\d+)", str(s))
    return int(m.group(1)) if m else 0


def _str_to_bool(s) -> bool:
    return str(s).strip().lower() in ("true", "yes", "on", "1")


def _sanitize_float(v) -> str:
    """`FString::SanitizeFloat(v,0)` 的近似：最少小数位（5.0→"5"、2.5→"2.5"）。"""
    f = float(v)
    if f == int(f):
        return str(int(f))
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _jvalue_to_int(v):
    if v is None:
        return 0
    t, p = v
    if t == 4:
        return 1 if p else 0
    if t == 3:
        if -2 ** 31 <= float(p) <= 2 ** 31 - 1:
            return _rhz(p)
        return 0
    if t == 2:
        return _lex_int(p)
    return 0


def _jvalue_to_str(v):
    if v is None:
        return ""
    t, p = v
    if t == 2:
        return str(p)
    if t == 4:
        return "true" if p else "false"
    if t == 3:
        return _sanitize_float(p)
    return ""                                  # Null/Array/Object ⇒ 空串


def _jvalue_to_bool(v):
    if v is None:
        return False
    t, p = v
    if t == 4:
        return bool(p)
    if t == 3:
        return float(p) != 0.0
    if t == 2:
        return _str_to_bool(p)
    return False


def _jmake_field(o, name, v):
    if o is not None and v is not None:        # obj/value 的 ptr 为空 ⇒ 无操作（IDA）
        o.d[_jfold(name)] = (name, v)
    return o


def _jremove_field(o, name):
    if o is not None:
        o.d.pop(_jfold(name), None)
    return o


def _from_py(x):
    """Python 值 → jvalue（`Conv_StringToJsonObject` 用）。"""
    if x is None:
        return JNULL
    if isinstance(x, bool):
        return (4, x)
    if isinstance(x, (int, float)):
        return (3, float(x))
    if isinstance(x, str):
        return (2, x)
    if isinstance(x, list):
        return (5, [_from_py(i) for i in x])
    if isinstance(x, dict):
        o = JObj()
        for k, v in x.items():
            o.d[_jfold(k)] = (k, _from_py(v))
        return (6, o)
    return JNULL


def _to_py(v):
    if v is None:
        return None
    t, p = v
    if t == 1:
        return None
    if t in (2, 3, 4):
        return p
    if t == 5:
        return [_to_py(i) for i in p]
    if t == 6:
        return {k: _to_py(val) for k, (_orig, val) in p.d.items()}
    return None


_json = {
    "JsonMake": lambda: JObj(),
    "JsonMakeField": _jmake_field,
    "JsonRemoveField": _jremove_field,
    "JsonHasField": lambda o, n: o is not None and _jfold(n) in o.d,
    "JsonHasTypedField": lambda o, n, t: (o is not None and _jfold(n) in o.d
                                          and o.d[_jfold(n)][1][0] == int(t)),
    "Conv_JsonObjectToJsonValue": lambda o, n: (o.d[_jfold(n)][1]
                                                if (o is not None and _jfold(n) in o.d) else JNULL),
    "Conv_JsonValueToObject": lambda v: (v[1] if (v is not None and v[0] == 6) else None),
    "Conv_JsonValueToString": _jvalue_to_str,
    "Conv_JsonValueToInteger": _jvalue_to_int,
    "Conv_JsonValueToBool": _jvalue_to_bool,
    "Conv_JsonValueToArray": lambda v: (list(v[1]) if (v is not None and v[0] == 5) else []),
    "JsonMakeArray": lambda arr: (5, [x for x in (arr or []) if x is not None]),
    "JsonMakeBool": lambda b: (4, bool(b)),
    "JsonMakeInt": lambda i: (3, float(i)),
    "JsonMakeString": lambda s: (2, str(s)),
    # §12-U6 未确认：解析失败返回空对象还是 null ptr —— 这里取 None（空对象），并如实标注。
    "Conv_StringToJsonObject": lambda s: (_from_py(__import__("json").loads(s))[1]
                                          if str(s).strip() else None),
    "Conv_JsonObjectToString": lambda o: (
        "" if o is None else __import__("json").dumps(_to_py((6, o)), ensure_ascii=False,
                                                      separators=(",", ":"))),
}
_many("BlueprintJsonLibrary", _json)


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
    "Replace": lambda s, frm, to, case=1: (
        _s(s).replace(_s(frm), _s(to)) if int(case) == 0 or not _s(frm)
        else __import__("re").sub(__import__("re").escape(_s(frm)), lambda m: _s(to), _s(s), flags=__import__("re").I)),
    "ReplaceInline": lambda s, frm, to, *_x: _s(s).replace(_s(frm), _s(to)),
    "LeftChop": lambda s, n: _s(s)[:max(0, len(_s(s)) - U.i32(n))],
    # D3（NATIVE-SPEC-GAPS §11）：游戏 n≤0 ⇒ 整串；Python 的负切片会取尾部 ⇒ 先夹到 0。
    "RightChop": lambda s, n: _s(s)[max(0, U.i32(n)):],
    "Left": lambda s, n: _s(s)[:max(0, U.i32(n))],
    "Right": lambda s, n: _s(s)[max(0, len(_s(s)) - max(0, U.i32(n))):] if U.i32(n) > 0 else "",
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

# ★ 2026-10-02：普查里缺的纯函数（小写 abs/round/max/min 在上面 `_math` 附近已有别名，别重复登记——曾误覆盖过一次）。
_many("KismetMathLibrary", {
    "SelectString": lambda a, b, pick_a: _s(a) if bool(pick_a) else _s(b),
    "SelectObject": lambda a, b, pick_a: a if bool(pick_a) else b,
    "EqualEqual_ObjectObject": lambda a, b: a == b,
    "NotEqual_ObjectObject": lambda a, b: a != b,
    "And_IntInt": lambda a, b: U.i32(U.i32(a) & U.i32(b)),
    "BooleanNOR": lambda a, b: not (bool(a) or bool(b)),
    "InRange_IntInt": lambda v, lo, hi, inc_lo=True, inc_hi=True: (
        (U.i32(lo) <= U.i32(v) if inc_lo else U.i32(lo) < U.i32(v))
        and (U.i32(v) <= U.i32(hi) if inc_hi else U.i32(v) < U.i32(hi))),
    "MultiplyMultiply_FloatFloat": lambda b, e: float(b) ** float(e),
    # MapRangeClamped(Value, InRangeA, InRangeB, OutRangeA, OutRangeB)：线性映射并夹到出区间
    "MapRangeClamped": lambda v, a, b, oa, ob: (
        float(oa) if float(b) == float(a) else
        float(oa) + (float(ob) - float(oa)) * max(0.0, min(1.0, (float(v) - float(a)) / (float(b) - float(a))))),
    # MinOfIntArray(IntArray, &IndexOfMinValue, &MinValue)：空数组 ⇒ (-1, 0)；VM 按位置写回两个出参
    "MinOfIntArray": lambda arr: ((min(range(len(arr)), key=lambda i: arr[i]), min(arr)) if arr else (-1, 0)),
})
# GameplayTag 库（引擎语义，未在 IDA 里核；tag 在 VM 里按**名字字符串**处理，层级以 '.' 分隔）
def _tn(x):
    return str(x.get("TagName") if isinstance(x, dict) else x)


def _tag_match(a, b, exact=False):
    a, b = _tn(a), _tn(b)
    return a == b if exact else (a == b or a.startswith(b + "."))


_many("BlueprintGameplayTagLibrary", {
    "MatchesTag": lambda a, b, exact=False: _tag_match(a, b, exact),
    "HasAnyTags": lambda cont, other, exact=False: any(_tag_match(t, o, exact) for t in (cont or []) for o in (other or [])),
    "GetTagName": lambda tag: _tn(tag),
    "IsGameplayTagValid": lambda tag: _tn(tag) not in ("", "None"),
    "MakeGameplayTagContainerFromArray": lambda arr: list(arr or []),
})
_many("KismetSystemLibrary", {k: (lambda x: x) for k in (
    "MakeLiteralInt", "MakeLiteralByte", "MakeLiteralString", "MakeLiteralDouble", "MakeLiteralName",
    "MakeLiteralText", "MakeLiteralBool")})


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
    # ★ 2026-10-02 IDA 普查：字节码用小写 abs/round/max/min（约 100 处），以前全落空
    ("小写 abs 整数", "abs", [-5], 5),
    ("小写 abs 浮点", "abs", [-5.5], 5.5),
    ("小写 min 整数", "min", [3, 7], 3),
    ("小写 max 浮点", "KismetMathLibrary::max", [1.5, 2.5], 2.5),
    ("小写 round 半数远离零", "KismetMathLibrary::round", [2.5], 3),
    ("Array_Get 越界给零值(int→0)", "KismetArrayLibrary::Array_Get", [[1, 2], 9], 0),
    ("Array_Get 空数组给零值(int→0)", "KismetArrayLibrary::Array_Get", [[], 0], 0),
    # ★ Map_Find(Map, Key, &Value) -> bool：返回值=找没找到、value 走出参（NativeOut）。
    ("Map_Find 缺键 ⇒ (False, 值类型零值)", "BlueprintMapLibrary::Map_Find",
     [{"a": 1}, "b"], NativeOut(False, 0)),
    ("Map_Find 空表缺键 ⇒ (False, 0)", "BlueprintMapLibrary::Map_Find",
     [{}, "b"], NativeOut(False, 0)),
    ("Conv_IntToBool", "KismetMathLibrary::Conv_IntToBool", [0], False),
    # ★ 2026-10-02：只写日志的三个按 no-op（见模块 docstring 的例外说明）
    ("PrintString 是 no-op（不再打断效果链）",
     "KismetSystemLibrary::PrintString", ["x"], None),
    ("Conv_StringToText 恒等", "KismetStringLibrary::Conv_StringToText", ["abc"], "abc"),
    ("Format 按 ArgumentName 替换", "KismetStringLibrary::Format",
     ["hi {a}!", [{"ArgumentName": "a", "ArgumentValue": "X"}]], "hi X!"),
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
