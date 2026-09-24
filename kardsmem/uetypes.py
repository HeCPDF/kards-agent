#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.uetypes —— 把 UE 的标量语义固化成函数，**不要赌 Python 恰好一样**。

为什么必须单独一层
==================
Python 的算术和 C++ 的不一样，而且不一样的地方恰好都在**会算错结果**的位置：

| | C++ / UE | Python |
|---|---|---|
| `int32` 溢出 | 回绕（x86 实测行为） | 任意精度，永不溢出 |
| 整除 | 向**零**截断：`-7/2 == -3` | 向**下**取整：`-7//2 == -4` |
| 取模 | 符号随**被除数**：`-7%2 == -1` | 符号随**除数**：`-7%2 == 1` |
| 除以 0 | `Divide_IntInt` **返回 0** 并记一条警告 | 抛 `ZeroDivisionError` |
| `float` | 32 位 | 64 位 |

⇒ 直接用 `a // b`、`a % b`、`a / b` 去实现 `Divide_IntInt` 之类**一定会错**，
  而且错在负数和边界上 —— 平时看不出来，出问题时极难定位。

除零返回 0 这条有出处：`Engine/Source/Runtime/Engine/Classes/Kismet/KismetMathLibrary.inl`
```
int32 UKismetMathLibrary::Divide_IntInt(int32 A, int32 B)
{ if (B == 0) { ReportError_Divide_IntInt(); return 0; } return (A / B); }
```
`Percent_IntInt` 同理。

用法
====
    from kardsmem import uetypes as U
    U.i32(0x7FFFFFFF + 1)   # -> -2147483648
    U.div_i32(-7, 2)        # -> -3（不是 -4）
    U.div_i32(5, 0)         # -> 0（不抛异常）

只有纯计算，不碰内存。
"""
from __future__ import annotations

import math
import struct

INT8_MIN, INT8_MAX = -0x80, 0x7F
INT16_MIN, INT16_MAX = -0x8000, 0x7FFF
INT32_MIN, INT32_MAX = -0x80000000, 0x7FFFFFFF
INT64_MIN, INT64_MAX = -0x8000000000000000, 0x7FFFFFFFFFFFFFFF
UINT8_MAX = 0xFF


# --------------------------------------------------------------------------
# 回绕
# --------------------------------------------------------------------------
def i32(v) -> int:
    """截成 32 位有符号并**回绕**（C++ 有符号溢出是 UB，但 x86 上就是回绕）。"""
    return ((int(v) + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def i64(v) -> int:
    return ((int(v) + (1 << 63)) & ((1 << 64) - 1)) - (1 << 63)


def u8(v) -> int:
    return int(v) & 0xFF


def u32(v) -> int:
    return int(v) & 0xFFFFFFFF


def f32(v) -> float:
    """按 32 位 float round-trip。UE 的 `float` 是 32 位，`double`/BP 的 Float 是 64 位。

    ★ 蓝图里 UE5 的 `Float` 引脚其实是 **double**（`Add_DoubleDouble` 那一族），
      真正的 32 位只出现在 `*_FloatFloat` 和少数结构体字段上。别一律 f32。
    """
    return struct.unpack("<f", struct.pack("<f", float(v)))[0]


# --------------------------------------------------------------------------
# 整数除法 / 取模：C++ 语义
# --------------------------------------------------------------------------
def _c_div(a: int, b: int) -> int:
    """向零截断（C++ `/`）。"""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _c_mod(a: int, b: int) -> int:
    """符号随被除数（C++ `%`）。恒等式：a == _c_div(a,b)*b + _c_mod(a,b)。"""
    return a - _c_div(a, b) * b


def div_i32(a, b) -> int:
    """`UKismetMathLibrary::Divide_IntInt` —— **除以 0 返回 0**。"""
    a, b = i32(a), i32(b)
    if b == 0:
        return 0
    return i32(_c_div(a, b))


def mod_i32(a, b) -> int:
    """`Percent_IntInt` —— **模 0 返回 0**。"""
    a, b = i32(a), i32(b)
    if b == 0:
        return 0
    return i32(_c_mod(a, b))


def div_i64(a, b) -> int:
    a, b = i64(a), i64(b)
    return 0 if b == 0 else i64(_c_div(a, b))


def mod_i64(a, b) -> int:
    a, b = i64(a), i64(b)
    return 0 if b == 0 else i64(_c_mod(a, b))


def div_u8(a, b) -> int:
    a, b = u8(a), u8(b)
    return 0 if b == 0 else u8(a // b)


def mod_u8(a, b) -> int:
    a, b = u8(a), u8(b)
    return 0 if b == 0 else u8(a % b)


def div_f(a, b) -> float:
    """`Divide_DoubleDouble` —— 除以 0 返回 0（不是 inf，也不抛）。"""
    b = float(b)
    if b == 0.0:
        return 0.0
    return float(a) / b


# --------------------------------------------------------------------------
# 转换
# --------------------------------------------------------------------------
def trunc_to_i32(v) -> int:
    """`FMath::TruncToInt32` —— 向零截断后截成 int32。

    ⚠ NaN / 溢出在 C++ 里是 UB；这里取**饱和**，并且不抛异常 ——
      求值器宁可给个确定的坏值，也不要在半路炸掉。
    """
    x = float(v)
    if math.isnan(x):
        return 0
    x = math.trunc(x)
    if x > INT32_MAX:
        return INT32_MAX
    if x < INT32_MIN:
        return INT32_MIN
    return int(x)


def round_half_away(v) -> int:
    """`FMath::RoundToInt` —— .5 一律**远离零**（不是 Python 的 banker's rounding）。"""
    x = float(v)
    if math.isnan(x):
        return 0
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


def to_bool(v) -> bool:
    """`Conv_IntToBool` 等：`InInt == 0 ? false : true`。"""
    return bool(v)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def lerp(a, b, t):
    """`UKismetMathLibrary::Lerp` —— 源码是 `A + V*(B-A)`，**不夹紧** t。"""
    return float(a) + float(t) * (float(b) - float(a))


# --------------------------------------------------------------------------
# 自检（纯函数，随时可跑：python -m kardsmem.uetypes）
# --------------------------------------------------------------------------
CASES = [
    ("i32 回绕上界", lambda: i32(INT32_MAX + 1), INT32_MIN),
    ("i32 回绕下界", lambda: i32(INT32_MIN - 1), INT32_MAX),
    ("u8 回绕", lambda: u8(256), 0),
    ("整除向零截断(负)", lambda: div_i32(-7, 2), -3),          # Python // 会给 -4
    ("整除向零截断(正)", lambda: div_i32(7, 2), 3),
    ("整除两负", lambda: div_i32(-7, -2), 3),
    ("取模符号随被除数", lambda: mod_i32(-7, 2), -1),          # Python % 会给 1
    ("取模正数", lambda: mod_i32(7, -2), 1),                   # Python % 会给 -1
    ("除零返回0", lambda: div_i32(5, 0), 0),
    ("模零返回0", lambda: mod_i32(5, 0), 0),
    ("浮点除零返回0", lambda: div_f(5.0, 0.0), 0.0),
    ("除法恒等式", lambda: div_i32(-7, 2) * 2 + mod_i32(-7, 2), -7),
    ("TruncToInt 向零", lambda: trunc_to_i32(-2.9), -2),
    ("TruncToInt 饱和", lambda: trunc_to_i32(1e30), INT32_MAX),
    ("TruncToInt NaN", lambda: trunc_to_i32(float("nan")), 0),
    ("RoundToInt 远离零", lambda: (round_half_away(0.5), round_half_away(-0.5)), (1, -1)),
    ("RoundToInt 不是银行家", lambda: round_half_away(2.5), 3),   # Python round() 会给 2
    ("f32 精度", lambda: f32(0.1) != 0.1, True),
    ("Lerp 不夹紧", lambda: lerp(0, 10, 2.0), 20.0),
]


def selftest() -> int:
    bad = 0
    for name, fn, want in CASES:
        got = fn()
        ok = got == want
        if not ok:
            bad += 1
        print("  [%s] %-24s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))
    print("uetypes selftest: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
