#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.kismet —— 直接读**运行时的 Kismet 字节码**并反汇编。

为什么不走 FModel 的反编译
==========================
FModel 导出的伪 C++ 是二手的：类型被抹成 `class U*`、控制流变成 goto/Label 汤、
个别节点会丢。**字节码才是游戏真正执行的东西。**

为什么从内存读而不是从 uasset 读
================================
uasset/uexp 里的操作数是**序列化的 import/export 索引**，要自己建表重连；
内存里的同一段字节码，操作数是**已经解析好的真指针**（`FProperty*` / `UObject*` /
`UFunction*`）—— 实测 `BP_HandCard_C::DiscardMulliganCard` 的 `EX_InstanceVariable`
操作数就是该类属性链里的 FField 地址。配上 §4.13 的反射链，指针当场能翻成名字和偏移。
⇒ **整个 uasset 解析层不用写。**

    UStruct +0x60 Script(TArray<uint8>)：Data@0x60 Num@0x68 Max@0x6C
    UStruct +0x48 Children(UField*)，沿 UField::Next(+0x28) 走 → 该类的所有 UFunction

红线
====
本模块**只读字节码并在 Python 里解释**。游戏进程里不执行任何东西，不写一个字节。

来源
====
opcode 表抄自 UE 5.6 `CoreUObject/Public/UObject/Script.h` 的 `EExprToken`；
操作数布局抄自 `Developer/ScriptDisassembler/Private/ScriptDisassembler.cpp`。
`FScriptName` 在脚本流里占 **12 字节**（ComparisonIndex/Dummy/Number），
不是运行时 FName 的 8 字节 —— 这是最容易错的一处。
"""
from __future__ import annotations

import struct
from typing import Optional

# --------------------------------------------------------------------------
# UStruct / UFunction
# --------------------------------------------------------------------------
OFF_CHILDREN = 0x48
OFF_SCRIPT_DATA = 0x60
OFF_SCRIPT_NUM = 0x68
OFF_FUNC_FLAGS = 0xB0
OFF_UFIELD_NEXT = 0x28
FUNC_NATIVE = 0x00000400

SCRIPT_NAME_SIZE = 12          # FScriptName：ComparisonIndex(4) + Dummy(4) + Number(4)

EX = {
    0x00: "LocalVariable", 0x01: "InstanceVariable", 0x02: "DefaultVariable",
    0x04: "Return", 0x06: "Jump", 0x07: "JumpIfNot", 0x09: "Assert",
    0x0B: "Nothing", 0x0C: "NothingInt32", 0x0F: "Let", 0x11: "BitFieldConst",
    0x12: "ClassContext", 0x13: "MetaCast", 0x14: "LetBool", 0x15: "EndParmValue",
    0x16: "EndFunctionParms", 0x17: "Self", 0x18: "Skip", 0x19: "Context",
    0x1A: "Context_FailSilent", 0x1B: "VirtualFunction", 0x1C: "FinalFunction",
    0x1D: "IntConst", 0x1E: "FloatConst", 0x1F: "StringConst", 0x20: "ObjectConst",
    0x21: "NameConst", 0x22: "RotationConst", 0x23: "VectorConst", 0x24: "ByteConst",
    0x25: "IntZero", 0x26: "IntOne", 0x27: "True", 0x28: "False", 0x29: "TextConst",
    0x2A: "NoObject", 0x2B: "TransformConst", 0x2C: "IntConstByte", 0x2D: "NoInterface",
    0x2E: "DynamicCast", 0x2F: "StructConst", 0x30: "EndStructConst", 0x31: "SetArray",
    0x32: "EndArray", 0x33: "PropertyConst", 0x34: "UnicodeStringConst",
    0x35: "Int64Const", 0x36: "UInt64Const", 0x37: "DoubleConst", 0x38: "Cast",
    0x39: "SetSet", 0x3A: "EndSet", 0x3B: "SetMap", 0x3C: "EndMap", 0x3D: "SetConst",
    0x3E: "EndSetConst", 0x3F: "MapConst", 0x40: "EndMapConst", 0x41: "Vector3fConst",
    0x42: "StructMemberContext", 0x43: "LetMulticastDelegate", 0x44: "LetDelegate",
    0x45: "LocalVirtualFunction", 0x46: "LocalFinalFunction", 0x48: "LocalOutVariable",
    0x4A: "DeprecatedOp4A", 0x4B: "InstanceDelegate", 0x4C: "PushExecutionFlow",
    0x4D: "PopExecutionFlow", 0x4E: "ComputedJump", 0x4F: "PopExecutionFlowIfNot",
    0x50: "Breakpoint", 0x51: "InterfaceContext", 0x52: "ObjToInterfaceCast",
    0x53: "EndOfScript", 0x54: "CrossInterfaceCast", 0x55: "InterfaceToObjCast",
    0x5A: "WireTracepoint", 0x5B: "SkipOffsetConst", 0x5C: "AddMulticastDelegate",
    0x5D: "ClearMulticastDelegate", 0x5E: "Tracepoint", 0x5F: "LetObj",
    0x60: "LetWeakObjPtr", 0x61: "BindDelegate", 0x62: "RemoveMulticastDelegate",
    0x63: "CallMulticastDelegate", 0x64: "LetValueOnPersistentFrame",
    0x65: "ArrayConst", 0x66: "EndArrayConst", 0x67: "SoftObjectConst",
    0x68: "CallMath", 0x69: "SwitchValue", 0x6A: "InstrumentationEvent",
    0x6B: "ArrayGetByRef", 0x6C: "ClassSparseDataVariable", 0x6D: "PropertyConst2",
}


class Undecoded(Exception):
    """遇到没实现的 opcode —— 带上偏移，方便补。不是崩溃，是覆盖缺口。"""


class Expr:
    """一条表达式：op 名 + 操作数 dict + 起止偏移 + 子表达式。"""

    __slots__ = ("op", "at", "end", "args", "kids")

    def __init__(self, op, at):
        self.op, self.at, self.end, self.args, self.kids = op, at, at, {}, []

    def __repr__(self):
        a = " ".join("%s=%s" % (k, v) for k, v in self.args.items())
        return "%s(%s)" % (self.op, a) if a else self.op


class Reader:
    """脚本流游标。`resolve` 把指针翻成可读名字（可选）。"""

    def __init__(self, code: bytes, resolve=None):
        self.b, self.i, self.resolve = code, 0, resolve

    def u8(self):
        v = self.b[self.i]; self.i += 1; return v

    def u16(self):
        v = struct.unpack_from("<H", self.b, self.i)[0]; self.i += 2; return v

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.i)[0]; self.i += 4; return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.i)[0]; self.i += 4; return v

    def i64(self):
        v = struct.unpack_from("<q", self.b, self.i)[0]; self.i += 8; return v

    def f32(self):
        v = struct.unpack_from("<f", self.b, self.i)[0]; self.i += 4; return v

    def f64(self):
        v = struct.unpack_from("<d", self.b, self.i)[0]; self.i += 8; return v

    def ptr(self):
        v = struct.unpack_from("<Q", self.b, self.i)[0]; self.i += 8; return v

    def name(self):
        """FScriptName：12 字节，只有前 4 字节的 ComparisonIndex 有用。"""
        idx = struct.unpack_from("<I", self.b, self.i)[0]
        num = struct.unpack_from("<I", self.b, self.i + 8)[0]
        self.i += SCRIPT_NAME_SIZE
        if self.resolve:
            return self.resolve.name(idx, num)
        return "Name#%d" % idx

    def skip(self):
        return self.u32()          # CodeSkipSizeType = int32（未开 64KB 限制）

    def cstr(self):
        j = self.b.index(b"\x00", self.i)
        s = self.b[self.i:j].decode("latin-1", "replace")
        self.i = j + 1
        return s

    def wstr(self):
        j = self.i
        while struct.unpack_from("<H", self.b, j)[0] != 0:
            j += 2
        s = self.b[self.i:j].decode("utf-16-le", "replace")
        self.i = j + 2
        return s

    def obj(self, kind="obj"):
        p = self.ptr()
        if self.resolve:
            return self.resolve.obj(p, kind)
        return "%s@%#x" % (kind, p)


def _parms(r, e):
    """吃掉参数表，直到 EX_EndFunctionParms。"""
    while True:
        if r.b[r.i] == 0x16:
            r.i += 1
            return
        e.kids.append(expr(r))


def expr(r: Reader) -> Expr:
    """解一条表达式（递归）。布局照 UE 的 `ScriptDisassembler.cpp`。"""
    at = r.i
    op = r.u8()
    nm = EX.get(op)
    if nm is None:
        raise Undecoded("未知 opcode 0x%02X @ 0x%X" % (op, at))
    e = Expr(nm, at)

    if nm in ("LocalVariable", "InstanceVariable", "DefaultVariable",
              "LocalOutVariable", "ClassSparseDataVariable", "PropertyConst"):
        e.args["prop"] = r.obj("prop")
    elif nm in ("Nothing", "EndOfScript", "EndFunctionParms", "EndStructConst",
                "EndArray", "EndArrayConst", "EndSet", "EndMap", "EndSetConst",
                "EndMapConst", "IntZero", "IntOne", "True", "False", "NoObject",
                "NoInterface", "Self", "EndParmValue", "PopExecutionFlow",
                "Breakpoint", "Tracepoint", "WireTracepoint", "DeprecatedOp4A"):
        pass
    elif nm == "NothingInt32":
        e.args["v"] = r.i32()
    elif nm == "Return":
        e.kids.append(expr(r))
    elif nm in ("Jump", "PushExecutionFlow"):
        e.args["to"] = r.skip()
    elif nm == "JumpIfNot":
        e.args["to"] = r.skip()
        e.kids.append(expr(r))
    elif nm == "PopExecutionFlowIfNot":
        e.kids.append(expr(r))
    elif nm == "ComputedJump":
        e.kids.append(expr(r))
    elif nm == "Assert":
        e.args["line"] = r.u16(); e.args["debug"] = r.u8()
        e.kids.append(expr(r))
    elif nm in ("Let", "LetValueOnPersistentFrame"):
        if nm == "Let":
            e.args["prop"] = r.obj("prop")
        else:
            e.args["dest"] = r.obj("prop")
        e.kids.append(expr(r))
        if nm == "Let":
            e.kids.append(expr(r))
    elif nm in ("LetBool", "LetObj", "LetWeakObjPtr", "LetDelegate",
                "LetMulticastDelegate", "AddMulticastDelegate",
                "RemoveMulticastDelegate", "ClearMulticastDelegate"):
        e.kids.append(expr(r))
        if nm != "ClearMulticastDelegate":
            e.kids.append(expr(r))
    elif nm == "BitFieldConst":
        e.args["prop"] = r.obj("prop"); e.args["bit"] = r.u8()
    elif nm in ("Context", "Context_FailSilent", "ClassContext", "InterfaceContext"):
        if nm == "InterfaceContext":
            e.kids.append(expr(r))
        else:
            e.kids.append(expr(r))                 # 上下文对象
            e.args["skip"] = r.skip()              # 失败时跳过多少
            e.args["rprop"] = r.obj("prop")        # 返回值属性
            e.kids.append(expr(r))                 # 真正的调用
    elif nm == "StructMemberContext":
        e.args["prop"] = r.obj("prop")
        e.kids.append(expr(r))
    elif nm in ("VirtualFunction", "LocalVirtualFunction"):
        e.args["fn"] = r.name()
        _parms(r, e)
    elif nm in ("FinalFunction", "LocalFinalFunction", "CallMath"):
        e.args["fn"] = r.obj("func")
        _parms(r, e)
    elif nm == "CallMulticastDelegate":
        e.args["sig"] = r.obj("func")
        e.kids.append(expr(r))
        _parms(r, e)
    elif nm == "IntConst":
        e.args["v"] = r.i32()
    elif nm == "IntConstByte":
        e.args["v"] = r.u8()
    elif nm in ("Int64Const", "UInt64Const"):
        e.args["v"] = r.i64()
    elif nm == "FloatConst":
        e.args["v"] = r.f32()
    elif nm == "DoubleConst":
        e.args["v"] = r.f64()
    elif nm == "ByteConst":
        e.args["v"] = r.u8()
    elif nm == "SkipOffsetConst":
        e.args["v"] = r.skip()
    elif nm == "StringConst":
        e.args["v"] = r.cstr()
    elif nm == "UnicodeStringConst":
        e.args["v"] = r.wstr()
    elif nm == "NameConst":
        e.args["v"] = r.name()
    elif nm in ("ObjectConst", "SoftObjectConst", "InstanceDelegate"):
        if nm == "SoftObjectConst":
            e.kids.append(expr(r))
        elif nm == "InstanceDelegate":
            e.args["fn"] = r.name()
        else:
            e.args["v"] = r.obj()
    elif nm == "VectorConst":
        e.args["v"] = (r.f64(), r.f64(), r.f64())
    elif nm == "Vector3fConst":
        e.args["v"] = (r.f32(), r.f32(), r.f32())
    elif nm == "RotationConst":
        e.args["v"] = (r.f64(), r.f64(), r.f64())
    elif nm == "TransformConst":
        e.args["v"] = tuple(r.f64() for _ in range(10))
    elif nm == "StructConst":
        e.args["struct"] = r.obj("struct"); e.args["size"] = r.i32()
        while r.b[r.i] != 0x30:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "ArrayConst":
        e.args["inner"] = r.obj("prop"); e.args["n"] = r.i32()
        while r.b[r.i] != 0x66:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "SetArray":
        e.kids.append(expr(r))
        while r.b[r.i] != 0x32:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "SetSet":
        e.kids.append(expr(r)); e.args["n"] = r.i32()
        while r.b[r.i] != 0x3A:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "SetMap":
        e.kids.append(expr(r)); e.args["n"] = r.i32()
        while r.b[r.i] != 0x3C:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "MapConst":
        e.args["key"] = r.obj("prop"); e.args["val"] = r.obj("prop")
        e.args["n"] = r.i32()
        while r.b[r.i] != 0x40:
            e.kids.append(expr(r))
        r.i += 1
    elif nm == "SetConst":
        e.args["inner"] = r.obj("prop"); e.args["n"] = r.i32()
        while r.b[r.i] != 0x3E:
            e.kids.append(expr(r))
        r.i += 1
    elif nm in ("DynamicCast", "MetaCast", "ObjToInterfaceCast",
                "CrossInterfaceCast", "InterfaceToObjCast"):
        e.args["cls"] = r.obj("class")
        e.kids.append(expr(r))
    elif nm == "Cast":
        e.args["kind"] = r.u8()
        e.kids.append(expr(r))
    elif nm == "Skip":
        e.args["skip"] = r.skip()
        e.kids.append(expr(r))
    elif nm == "BindDelegate":
        e.args["fn"] = r.name()
        e.kids.append(expr(r)); e.kids.append(expr(r))
    elif nm == "ArrayGetByRef":
        e.kids.append(expr(r)); e.kids.append(expr(r))
    elif nm == "SwitchValue":
        n = r.u16(); e.args["n"] = n
        e.args["end"] = r.skip()
        e.kids.append(expr(r))                     # 被switch的值
        for _ in range(n):
            e.kids.append(expr(r))                 # case 值
            r.skip()                               # 下一个 case 的偏移
            e.kids.append(expr(r))                 # case 结果
        e.kids.append(expr(r))                     # default
    elif nm == "TextConst":
        e.args["kind"] = r.u8()
        # 0=Empty 1=LocalizedText 2=InvariantText 3=LiteralString 4=StringTableEntry
        k = e.args["kind"]
        if k == 1:
            for _ in range(3):
                e.kids.append(expr(r))
        elif k in (2, 3):
            e.kids.append(expr(r))
        elif k == 4:                               # StringTableEntry
            e.args["table"] = r.obj()              # ★ 先一个指针，再两个字串
            e.kids.append(expr(r)); e.kids.append(expr(r))
    elif nm == "InstrumentationEvent":
        e.args["kind"] = r.u8()
        if e.args["kind"] in (2, 3):               # 进入/退出某个事件
            e.args["ev"] = r.name()
    elif nm == "PropertyConst2":
        e.args["prop"] = r.obj("prop")
    else:
        raise Undecoded("opcode %s(0x%02X) @ 0x%X 未实现操作数布局" % (nm, op, at))

    e.end = r.i
    return e


def disasm(code: bytes, resolve=None, stop_on_error: bool = True):
    """整段反汇编 → [Expr]。完整解析到 `EndOfScript` 且**刚好**用完字节 = 强验证。"""
    r = Reader(code, resolve)
    out = []
    while r.i < len(code):
        try:
            e = expr(r)
        except Undecoded:
            if stop_on_error:
                raise
            break
        except (IndexError, struct.error) as ex:
            raise Undecoded("流越界 @ 0x%X (%s)" % (r.i, ex))
        out.append(e)
        if e.op == "EndOfScript":
            break
    return out, r.i


# --------------------------------------------------------------------------
# 从内存取函数
# --------------------------------------------------------------------------
def functions(session, ustruct: int) -> list:
    """某个 UClass/UStruct 自己的 `UFunction` → [{addr, name, native, size}]。"""
    m = session.m
    pool = session.names_pool()
    out, f, n = [], m.ptr_or_zero(ustruct + OFF_CHILDREN), 0
    while f and n < 4096:
        n += 1
        fl = m.u32(f + OFF_FUNC_FLAGS) or 0
        out.append({"addr": f, "name": pool.fname_of(f),
                    "flags": fl, "native": bool(fl & FUNC_NATIVE),
                    "size": m.i32(f + OFF_SCRIPT_NUM) or 0})
        f = m.ptr_or_zero(f + OFF_UFIELD_NEXT)
    return out


def script_of(session, ufunc: int) -> Optional[bytes]:
    """`UFunction` 的字节码。原生函数返回 None（逻辑在 exe 里，没有字节码）。"""
    m = session.m
    data = m.ptr_or_zero(ufunc + OFF_SCRIPT_DATA)
    num = m.i32(ufunc + OFF_SCRIPT_NUM) or 0
    if not data or num <= 0 or num > 4_000_000:
        return None
    return m.read(data, num)


def find_function(session, ustruct: int, name: str) -> Optional[int]:
    for f in functions(session, ustruct):
        if f["name"] == name:
            return f["addr"]
    return None


# --------------------------------------------------------------------------
# 指针 → 可读名字
# --------------------------------------------------------------------------
class Resolve:
    """把脚本流里的裸指针翻成名字。**这是走内存路线的红利**：
    uasset 里这些位置是序列化索引，得自己建 import/export 表才能连回去。"""

    def __init__(self, session):
        self.s = session
        self.m = session.m
        self.pool = session.names_pool()
        self._c = {}

    def name(self, idx, num=0):
        try:
            return self.pool.fname(idx, num or 0) or "Name#%d" % idx
        except Exception:                                    # noqa: BLE001
            return "Name#%d" % idx

    def obj(self, p, kind="obj"):
        if not p:
            return "None"
        if p in self._c:
            return self._c[p]
        out = None
        if kind == "prop":                       # FField：名字在 +0x20
            idx = self.m.u32(p + 0x20)
            if idx:
                out = self.name(idx)
        if out is None:                          # UObject：名字在 +0x18
            try:
                out = self.pool.fname_of(p)
            except Exception:                    # noqa: BLE001
                out = None
        out = out or ("%s@%#x" % (kind, p))
        self._c[p] = out
        return out


def dump(session, ufunc: int, indent: str = "  ") -> str:
    """反汇编一个 UFunction → 可读文本（带偏移，便于和跳转目标对上）。"""
    code = script_of(session, ufunc)
    if code is None:
        return "(原生函数，没有字节码)"
    es, _used = disasm(code, Resolve(session))
    lines = []

    def walk(e, d):
        lines.append("%5d %s%s" % (e.at, indent * d, e))
        for k in e.kids:
            walk(k, d + 1)

    for e in es:
        walk(e, 0)
    return "\n".join(lines)
