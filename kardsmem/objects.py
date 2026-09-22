#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.objects —— 遍历 `GUObjectArray`：拿到**所有** UObject，不只是 Actor。

为什么需要它
============
`world.Locator.actors()` 走的是 `PersistentLevel->Actors`，**只能拿到 Actor**。
而 UMG widget（界面）、`MatchController`、`GameInstance` 这些统统不是 Actor
⇒ 整个界面层在内存里是盲区。这正是"点击有没有被界面吃掉"判不出来的根因。

布局来自 Dumper-7 的 SDK 输出（`sdk/1.58.27125.Steam/CppSDK/SDK/Basic.hpp`），
不是猜的：

    Offsets::GObjects = 0x091FF4E0            （与 build.RVA["GObjects"] 一致）
    TUObjectArray  +0x00 Objects(FUObjectItem**)   ← **分块**数组，不是平坦数组
                   +0x10 MaxElements  +0x14 NumElements
                   +0x18 MaxChunks    +0x1C NumChunks
                   ElementsPerChunk = 0x10000
    FUObjectItem   0x18 字节，+0x00 Object(UObject*)

    obj(i) = Objects[i / 0x10000][i % 0x10000].Object

只读。

用法
====
    from kardsmem import attach
    from kardsmem.objects import ObjectArray
    oa = ObjectArray(s)
    oa.count()                          # 对象总数
    oa.find_by_class_name("Battle_Settings_Widget_C")     # → [addr, ...]
    oa.find_by_propsize(3640)           # 按类大小找（与 §4.1 同一判据）

★ 全量遍历有几十万个对象，**别在每帧调用**。要么缓存结果，
  要么先用 `limit` 早停。界面对象在一局里是稳定的，抓一次存住即可。
"""
from __future__ import annotations

from typing import Iterator, Optional

from . import build as B

OFF_OBJECTS = 0x00
OFF_MAX_ELEMENTS = 0x10
OFF_NUM_ELEMENTS = 0x14
OFF_MAX_CHUNKS = 0x18
OFF_NUM_CHUNKS = 0x1C
ELEMENTS_PER_CHUNK = 0x10000
FUOBJECTITEM_SIZE = 0x18

OFF_UOBJECT_FLAGS = 0x08
OFF_UOBJECT_CLASS = 0x10
OFF_UOBJECT_NAME = 0x18
FLAG_RF_CDO = 0x10          # RF_ClassDefaultObject —— CDO 不是实例，默认滤掉

SANE_MAX = 4_000_000        # 对象数上界：越过就是读错了地方，别去 seek 几亿次


class ObjectArray:
    """`GUObjectArray` 的只读视图。`session` 只要 `.m` 和 `.base`（转储也行）。"""

    def __init__(self, session, rva: Optional[int] = None):
        self.m = session.m
        self.base = session.base
        self.addr = self.base + (rva if rva is not None else B.RVA["GObjects"])
        self._pool = None
        self._session = session

    # ---- 头 ----
    def chunks_ptr(self) -> int:
        return self.m.ptr_or_zero(self.addr + OFF_OBJECTS)

    def count(self) -> int:
        n = self.m.i32(self.addr + OFF_NUM_ELEMENTS) or 0
        return n if 0 < n < SANE_MAX else 0

    def num_chunks(self) -> int:
        return self.m.i32(self.addr + OFF_NUM_CHUNKS) or 0

    def header(self) -> dict:
        return {"addr": self.addr, "chunks": self.chunks_ptr(),
                "num_elements": self.m.i32(self.addr + OFF_NUM_ELEMENTS),
                "max_elements": self.m.i32(self.addr + OFF_MAX_ELEMENTS),
                "num_chunks": self.num_chunks(),
                "max_chunks": self.m.i32(self.addr + OFF_MAX_CHUNKS)}

    def sane(self) -> bool:
        """头长得对不对。**不对就别往下走** —— 拿错地址遍历会读出满屏垃圾指针。"""
        h = self.header()
        n, nc = h["num_elements"] or 0, h["num_chunks"] or 0
        if not h["chunks"] or not (0 < n < SANE_MAX) or not (0 < nc <= 8192):
            return False
        # 块数必须刚好够装下元素数
        return nc == (n + ELEMENTS_PER_CHUNK - 1) // ELEMENTS_PER_CHUNK or nc * ELEMENTS_PER_CHUNK >= n

    # ---- 遍历 ----
    def at(self, i: int) -> int:
        chunks = self.chunks_ptr()
        if not chunks:
            return 0
        c = self.m.ptr_or_zero(chunks + (i // ELEMENTS_PER_CHUNK) * 8)
        if not c:
            return 0
        return self.m.ptr_or_zero(c + (i % ELEMENTS_PER_CHUNK) * FUOBJECTITEM_SIZE)

    def iter_objects(self, limit: Optional[int] = None,
                     skip_cdo: bool = True) -> Iterator[int]:
        """逐块整读，**不要**一个对象一个 ReadProcessMemory —— 差两个数量级。"""
        n = self.count()
        if not n:
            return
        chunks = self.chunks_ptr()
        got = 0
        for ci in range(self.num_chunks()):
            c = self.m.ptr_or_zero(chunks + ci * 8)
            if not c:
                continue
            lo = ci * ELEMENTS_PER_CHUNK
            cnt = min(ELEMENTS_PER_CHUNK, n - lo)
            if cnt <= 0:
                break
            raw = self.m.read(c, cnt * FUOBJECTITEM_SIZE)
            if not raw:
                continue
            for k in range(cnt):
                p = int.from_bytes(raw[k * FUOBJECTITEM_SIZE:
                                       k * FUOBJECTITEM_SIZE + 8], "little")
                if not (0x10000 <= p <= 0x7FFFFFFFFFFF):
                    continue
                if skip_cdo:
                    fl = self.m.i32(p + OFF_UOBJECT_FLAGS)
                    if fl is None or (fl & FLAG_RF_CDO):
                        continue
                yield p
                got += 1
                if limit and got >= limit:
                    return

    # ---- 查找 ----
    def pool(self):
        if self._pool is None:
            self._pool = self._session.names_pool()
        return self._pool

    def class_of(self, obj: int) -> int:
        return self.m.ptr_or_zero(obj + OFF_UOBJECT_CLASS)

    def class_name(self, obj: int) -> Optional[str]:
        c = self.class_of(obj)
        return self.pool().fname_of(c) if c else None

    def obj_name(self, obj: int) -> Optional[str]:
        return self.pool().fname_of(obj)

    def find_by_propsize(self, size: int, limit: Optional[int] = None) -> list:
        """按 `UClass::PropertiesSize` 找实例（与 §4.1 同一判据，不认名字）。"""
        from .props import OFF_PROPSIZE
        out, seen = [], {}
        for p in self.iter_objects():
            c = self.class_of(p)
            if not c:
                continue
            if c not in seen:
                seen[c] = self.m.i32(c + OFF_PROPSIZE)
            if seen[c] == size:
                out.append(p)
                if limit and len(out) >= limit:
                    break
        return out

    def find_by_class_name(self, name: str, limit: Optional[int] = None) -> list:
        """按类名找实例。**慢**（每个对象一次 FName 解析），只用于一次性定位；
        定位到之后改用 `find_by_propsize` 或缓存指针。"""
        out, seen = [], {}
        pool = self.pool()
        for p in self.iter_objects():
            c = self.class_of(p)
            if not c:
                continue
            if c not in seen:
                seen[c] = pool.fname_of(c)
            if seen[c] == name:
                out.append(p)
                if limit and len(out) >= limit:
                    break
        return out
