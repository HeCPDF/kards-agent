#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.containers —— UE 稀疏容器（TSparseArray/TSet/TMap）的**真实**读法。

为什么不能"当连续数组硬读"
==========================
`kardsmem/board.py::_enumerate_cards()` 读 `AllCardsInBattle`（`TMap<int32, ptr>`）时，
拿 TArray 的 `Num` 当"有多少个合法元素"扫过去，用"指针像不像卡对象"筛掉空洞——
这是能用但不严谨的近似：`TSparseArray` 删除元素时**不搬家**，被删的槽位留在
数组里当"空闲链表"节点复用（`PrevFreeIndex`/`NextFreeIndex`），槽位本身的字节
不会被清零，只是不再算数。近似法赌的是"空洞里的陈旧数据凑巧通不过合法性检查"，
对指针型的值大概率成立，但换一个值类型（比如本模块要读的
`TMap<uint8, FintegerSetStruct>`，值是内嵌结构体不是指针）就没有类似的
"看着不像"的检验手段——不能再赌。

真正的判据只有一个：`TSparseArray::AllocationFlags`（`FBitArray`）第 i 位是不是
`1`。本模块老老实实读这个位图，不猜、不近似。

字节布局来自哪里（① grep SDK dump，见 CLAUDE.md 查找顺序）
============================================================
不是照抄 UE 官方源码得来的——**这是 fork**（`++UE5+Release-5.6-Fork-kards`，
CLAUDE.md 弯路 #3 就是「照抄原版布局」栽的跟头）。这次布局来自这个游戏自己的
Dumper-7 SDK 导出**自带的容器实现**：
`reverse-data/sdk/<build>/CppSDK/UnrealContainers.hpp`
（Dumper-7 生成、给外部工具直接 `#include` 用的头文件，跟游戏本体一套二进制
布局，`DUMPER7_ASSERTS_*` 宏在编译期会校验大小，等于官方替我们验过尺寸）：

    TArray<T>          { T* Data; int32 Num; int32 Max; }                     16B
    FBitArray          { TInlineAllocator<4>::ForElementType<int32> Data;     32B
                          int32 NumBits; int32 MaxBits; }
      ForElementType<int32> { int32 InlineData[4]; int32* SecondaryData; }    24B
      ⇒ NumBits <= 128 时数据就在 InlineData 里（SecondaryData==NULL）；
        超过 128 位才落堆，这时读 SecondaryData 指向的位图。
    TSparseArray<T>    { TArray<Elem> Data; FBitArray AllocationFlags;        56B(0x38)
                          int32 FirstFreeIndex; int32 NumFreeIndices; }
      —— **这个结构体自身大小固定 56 字节，跟 T 是什么无关**（`Data` 只是个
         指向堆上变长数组的头，元素本身的大小只影响堆上那块内存怎么切）。
    TSet<T>            { TSparseArray<SetElement<T>> Elements;                80B(0x50)
                          TInlineAllocator<1>::ForElementType<int32> Hash;
                          int32 HashSize; }
      SetElement<T>    { T Value; int32 HashNextId; int32 HashIndex; }
      —— **TSet<T> 自身表头大小也固定 80 字节**，跟 T 无关（同上）。这就是为什么
         `FintegerSetStruct`（= `TSet<int32>`，见 `integerSetStruct_structs.hpp`）
         和本模块要读的 `CardFunctionTriggers`
         （`TMap<ERegisteredCardFunction, FintegerSetStruct>`
         = `TSet<TPair<uint8, FintegerSetStruct>>`，`BP_GameState_Battle_
         classes.hpp:59`：`0x0588(0x0050)`）表头大小碰巧都是 0x50——不是巧合，
         是 TSet 表头本来就跟元素类型无关，两处 SDK 里的尺寸标注互相印证。
    TMap<K,V>          { TSet<TPair<K,V>> Elements; }                         跟 TSet 一样大

只读，不越红线：本模块只调用 `session.m` 上现成的 `ReadProcessMemory` 封装
（`u8`/`i32`/`read_exact`/`ptr_or_zero`），不写、不注入。

★ 2026-09-24：这条本来是 §11.2 里明确写着"不建议在赶进度的会话里顺手做"的
P1 坑（怕稀疏度没验证就照抄近似会读出"看起来正常但错的"数据）。这次没有走
近似那条路——直接实现真正的 `AllocationFlags` 位图判定，不管稀不稀疏、有没有
空洞都按定义正确；唯一还没做到的是**拿一局真实对局的数据核对结果语义对不对**
（当时游戏在后台、不在对局中，`CardFunctionTriggers` 大概率是空的，只能验证
"结构读得通、空表处理不炸"，验证不了"读出来的 CardID 集合和游戏实际行为一致"）。
"""
from __future__ import annotations

import struct
from typing import Iterator, List, Optional

# -- FBitArray ---------------------------------------------------------------
OFF_BITARRAY_INLINE = 0x00      # 内联数据（4 个 int32，最多 128 位）
OFF_BITARRAY_SECONDARY = 0x10   # 超过 128 位时落堆的指针
OFF_BITARRAY_NUMBITS = 0x18
OFF_BITARRAY_MAXBITS = 0x1C
SIZE_BITARRAY = 0x20

# -- TSparseArray（表头，跟元素类型无关，恒 56 字节）--------------------------
OFF_SPARSE_DATA_PTR = 0x00
OFF_SPARSE_DATA_NUM = 0x08         # NumAllocated()：含空洞的槽位总数
OFF_SPARSE_ALLOCFLAGS = 0x10       # 往后 0x20 字节是上面的 FBitArray
SIZE_TSPARSEARRAY_HEADER = 0x38

# -- TSet/TMap（表头，跟元素类型无关，恒 80 字节）-----------------------------
SIZE_TSET_HEADER = 0x50

SANE_MAX_SLOTS = 200_000    # 读错地址的兜底：真实卡池/注册表不会有这么多槽位


def read_bitarray_bits(mem, addr: int) -> List[bool]:
    """读一个 `FBitArray`（32 字节表头，在 `addr`）→ 每一位是不是 1 的列表。

    `NumBits` 就是这个位图逻辑上有多少位（= 对应 `TSparseArray` 的 `NumAllocated()`）。
    读不出表头本身（进程没了/地址非法）返回空列表——调用方按"没有候选"处理，
    不当"读到 0 个"这种正面结论用。
    """
    num_bits = mem.i32(addr + OFF_BITARRAY_NUMBITS)
    if not num_bits or num_bits <= 0:
        return []
    if num_bits <= 128:
        raw = mem.read_exact(addr + OFF_BITARRAY_INLINE, 16)
    else:
        heap = mem.ptr_or_zero(addr + OFF_BITARRAY_SECONDARY)
        if not heap:
            return []
        nwords = (num_bits + 31) // 32
        raw = mem.read_exact(heap, nwords * 4)
    if raw is None:
        return []
    words = struct.unpack("<%dI" % (len(raw) // 4), raw)
    out = []
    for i in range(num_bits):
        w, b = divmod(i, 32)
        out.append(bool(words[w] & (1 << b)))
    return out


def sparsearray_slots(mem, addr: int, elem_stride: int) -> Iterator[int]:
    """`TSparseArray` 表头在 `addr` → yield 每个**真正分配**
    （`AllocationFlags` 对应位为 1）的槽位在堆上的地址。

    空洞（已删除元素 / 空闲链表节点）一律跳过，不当元素读——这是本模块存在的
    唯一理由：别的地方（`board_api._enumerate_cards`）图省事直接扫全部槽位，
    靠"数据像不像"筛，本函数走真正的位图判定。
    """
    data_ptr = mem.ptr_or_zero(addr + OFF_SPARSE_DATA_PTR)
    num_allocated = mem.i32(addr + OFF_SPARSE_DATA_NUM)
    if not data_ptr or not num_allocated or num_allocated <= 0 or num_allocated > SANE_MAX_SLOTS:
        return
    flags = read_bitarray_bits(mem, addr + OFF_SPARSE_ALLOCFLAGS)
    n = min(num_allocated, len(flags))
    for i in range(n):
        if flags[i]:
            yield data_ptr + i * elem_stride


def tset_int32_values(mem, addr: int) -> List[int]:
    """`TSet<int32>`（比如 `FintegerSetStruct`）表头在 `addr` → 里面的 int32 值列表。

    元素是 `SetElement<int32> = {int32 Value; int32 HashNextId; int32 HashIndex;}`，
    12 字节一个（`int32` 内部没有指针，对齐只要 4，不用像下面那个 TMap 一样凑到 8）。
    """
    out = []
    for elem_addr in sparsearray_slots(mem, addr, 12):
        v = mem.i32(elem_addr)
        if v is not None:
            out.append(v)
    return out


# `CardFunctionTriggers`（`BP_GameState_Battle_classes.hpp:59`）专用的元素布局：
#   `SetElement<TPair<uint8, FintegerSetStruct>>`：
#     +0x00 TPair.First  (uint8 key，ERegisteredCardFunction)
#     +0x08 TPair.Second (FintegerSetStruct，0x50 字节，对齐到 8 从 +0x08 开始）
#     +0x58 HashNextId
#     +0x5C HashIndex
#   整个元素 stride = 0x60 = 96 字节。
CFT_ELEM_STRIDE = 0x60
CFT_KEY_OFF = 0x00
CFT_VALUE_OFF = 0x08


def tmap_u8_to_intset(mem, addr: int) -> dict:
    """`TMap<uint8 枚举, FintegerSetStruct>`（如 `CardFunctionTriggers`）在 `addr`
    → `{枚举值(int): [int32, ...]}`。空表（没有任何注册）返回 `{}`。
    """
    out = {}
    for elem_addr in sparsearray_slots(mem, addr, CFT_ELEM_STRIDE):
        key = mem.u8(elem_addr + CFT_KEY_OFF)
        if key is None:
            continue
        out[key] = tset_int32_values(mem, elem_addr + CFT_VALUE_OFF)
    return out


def tmap_int_to_fstring(mem, addr: int) -> dict:
    """`TMap<int32, FString>` 在 `addr` → `{int: str}`（空表返回 `{}`）。

    ★ 2026-09-26 加，用途很具体：`BP_OnlineMatch_C::selectCardToDrawPending`
    （`// 0x08F0`）**就是"当前正在等的"选牌"候选集** —— key = 触发卡 id，
    value = `a;b;c` 三张候选牌的**内部名串**。它等于动作流里
    `ZActionSelectCardToDrawPending{cardBeingPlayed, spawnCards}` 的内存本体
    ⇒ 有了它才能**确定当前待选的是哪三张**（扫候选 actor 分不清"活的"与"已结算残留"）。

    元素布局（跟 `CFT_*` 同一套推理，别硬试）：
      `SetElement<TPair<int32, FString>>`：
        +0x00 Key (int32，再补 4 字节对齐到 8)
        +0x08 Value (FString = {TCHAR* Data@+0, int32 Num@+8, int32 Max@+12}，16 字节)
        +0x18 HashNextId (int32)
        +0x1C HashIndex  (int32)
      ⇒ **元素 stride = 0x20**（32 字节；对齐由 FString 的 8 决定）。
    """
    out = {}
    for elem_addr in sparsearray_slots(mem, addr, 0x20):
        key = mem.i32(elem_addr)
        if key is None:
            continue
        p = mem.ptr_or_zero(elem_addr + 0x08)
        n = mem.i32(elem_addr + 0x10)
        s = None
        if p and n and 0 < n <= 4096:
            b = mem.read(p, n * 2)
            if b:
                s = b.decode("utf-16-le", "replace").rstrip("\x00")
        out[key] = s
    return out


def selftest() -> list:
    """离线断言：拿构造出来的假内存核对位图解析和空洞跳过逻辑，不需要游戏在跑。

    造一个 `FBitArray`（NumBits=10，位图 0b0000101101 —— 第 0/2/3/5 位是 1）
    和一个内联 `TSparseArray` 头（`NumAllocated=10`），核对 `sparsearray_slots`
    只吐出被标记的那几个槽位，不多不少——这就是"空洞会不会被误当元素"的回归。
    """
    import struct as _s

    class _FakeMem:
        def __init__(self, blob: bytes, base: int):
            self.blob, self.base = blob, base

        def _off(self, addr):
            return addr - self.base

        def i32(self, addr):
            o = self._off(addr)
            if o < 0 or o + 4 > len(self.blob):
                return None
            return _s.unpack_from("<i", self.blob, o)[0]

        def u8(self, addr):
            o = self._off(addr)
            if o < 0 or o + 1 > len(self.blob):
                return None
            return self.blob[o]

        def ptr_or_zero(self, addr):
            o = self._off(addr)
            if o < 0 or o + 8 > len(self.blob):
                return 0
            v = _s.unpack_from("<Q", self.blob, o)[0]
            return v

        def read_exact(self, addr, size):
            o = self._off(addr)
            if o < 0 or o + size > len(self.blob):
                return None
            return self.blob[o:o + size]

    rows = []
    BASE = 0x10000
    ELEM_STRIDE = 8
    ELEMS_AT = BASE + 0x1000
    NUM_SLOTS = 10
    bits = 0
    for i in (0, 2, 3, 5):
        bits |= (1 << i)
    # 构造：TSparseArray 表头（56 字节，0 起）+ 10 个 8 字节元素放在 ELEMS_AT
    header = bytearray(SIZE_TSPARSEARRAY_HEADER)
    _s.pack_into("<Q", header, OFF_SPARSE_DATA_PTR, ELEMS_AT)
    _s.pack_into("<i", header, OFF_SPARSE_DATA_NUM, NUM_SLOTS)
    _s.pack_into("<i", header, OFF_SPARSE_DATA_NUM + 4, NUM_SLOTS)     # Max，不关心
    _s.pack_into("<i", header, OFF_SPARSE_ALLOCFLAGS + OFF_BITARRAY_INLINE, bits)
    _s.pack_into("<i", header, OFF_SPARSE_ALLOCFLAGS + OFF_BITARRAY_NUMBITS, NUM_SLOTS)
    blob = bytearray(0x1000 + NUM_SLOTS * ELEM_STRIDE)
    blob[0:len(header)] = header
    for i in range(NUM_SLOTS):
        _s.pack_into("<q", blob, 0x1000 + i * ELEM_STRIDE, 1000 + i)   # 每格放个可辨认的值
    mem = _FakeMem(bytes(blob), BASE)
    got = sorted(mem.i32(a) if False else _s.unpack_from("<q", mem.read_exact(a, 8))[0]
                 for a in sparsearray_slots(mem, BASE, ELEM_STRIDE))
    want = sorted(1000 + i for i in (0, 2, 3, 5))
    rows.append(("PASS" if got == want else "FAIL",
                 "sparsearray_slots 跳过空洞", got, want))

    # 空表：NumAllocated=0 应该吐出空列表，不炸
    header0 = bytearray(SIZE_TSPARSEARRAY_HEADER)
    blob0 = bytes(header0)
    mem0 = _FakeMem(blob0, BASE)
    got0 = list(sparsearray_slots(mem0, BASE, ELEM_STRIDE))
    rows.append(("PASS" if got0 == [] else "FAIL", "空表不炸、不误报", got0, []))

    ok = all(r[0] == "PASS" for r in rows)
    for r in rows:
        print("  [%s] %s got=%s want=%s" % r)
    print("containers selftest: %s（%d 项失败）"
          % ("PASS" if ok else "FAIL", sum(1 for r in rows if r[0] == "FAIL")))
    return rows


if __name__ == "__main__":
    import sys
    rows = selftest()
    sys.exit(0 if all(r[0] == "PASS" for r in rows) else 1)
