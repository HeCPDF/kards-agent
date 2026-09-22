#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.names —— 真名字池（FNamePool）→ 字符串；FText 明文回退路线。

一行话
======
    from kardsmem import attach
    from kardsmem.names import FNamePool
    s = attach()
    p = FNamePool(s.m, s.base)        # 池在 base + 0x0911B9C0
    p.probe()                         # 布局探测（含头 0x40 字节 hexdump）
    p.name_at(0)                      # 'None'（★ 索引稀疏：下一个是 name_at(3)）
    p.card_asset_name(card_ptr)       # 'card_unit_...'（FName 路线，读 card+0x50）

为什么是 0x0911B9C0 而不是 0x090E2E28
=====================================
`0x090E2E28`（SDK 里的 `GNames`）**不是**名字池：`CppSDK/SDK/Basic.hpp` 自己写着
"GNames is only a fallback and null by default"，Dumper-7 在那里兜底一个 null。
真池地址来自本机反汇编 `FName::AppendString`（RVA 0x0137F000）：
    0137F020  lea r8,[rip+…]            → base + 0x0911B9C0
    0137F046  shr ecx,10h               → Block = idx >> 16   (block_bits = 16)
    0137F05B  add rcx,[r8+rdx*8+10h]    → ★ Blocks[] 内联在 pool + 0x10
构造器 0x0137AA30 也印证：`memset(this+0x10,0,0x10000)` + `*(this+0x10)=Malloc(0x20000)`。

布局（静态验证）
================
    +0x00  SRWLock（8 字节：u32 锁值 + u32 线程 id）
    +0x08  CurrentBlock / MaxChunkIndex (u32)
    +0x0C  ByteCursor (u32)
    +0x10  Blocks[8192]（内联数组，8 字节/项；Blocks[0] 就在 pool+0x10）
    entry 地址 = Blocks[idx >> 16] + (idx & 0xFFFF) * 2      ← ★ 步长是 2 字节
    entry 头 u16 = (Len << 6) | (Hash << 1) | bIsWide ；字符串紧跟其后
      Len == 0 → 间接项："下一个 index + number"，next_idx = i32@(entry+2)，number = i32@(entry+6)
      bIsWide  → UTF-16LE，否则 latin-1
      Len > 0x400 → 判为垃圾
实测 chunk0 头：`1E 01 'None' 10 03 'ByteProperty' C0 02 'IntProperty'`
（head 0x011E → len=4/wide=0；0x0310 → len=12；0x02C0 → len=11）
★ **索引是稀疏的**：块内 entry 依次紧挨（`advance = 2 + len`），而
  `FNameEntryId = 字节偏移 / 2`，所以实测（pid 3908）：
      byte  0 → idx  0  'None'
      byte  6 → idx  3  'ByteProperty'
      byte 20 → idx 10  'IntProperty'
      byte 34 → idx 17  'BoolProperty'
  ⇒ **`name_at(0/3/10/17)`** 才是这四个名字；`name_at(1)`、`name_at(2)` 落在
    `'None'` 的字符串内部，返回乱码是**正常行为**，不是 bug。
  想按"第几个名字"顺序数，用 `iter_entries()`（按 head 的 span 推进）而不是连续 idx。
  （旧记录里"entry(idx+1) = entry(idx) + 2 + 2*ceil(len/2)"的算法是错的：实测就是 +2+len。）

踩过的坑（不要重犯）
====================
1. 读文本**必须**用 `mem.read_exact`。旧工具 `fname_live.py` 内部有个 0x20000 的固定
   缓冲，超上限**静默返回 None** —— 所有"0 命中"的负结论都是这么来的。本模块的
   `read_text` 按需大读（FName 最长 0x400，FText 明文按声明的字符数精确读）。
2. `Blocks[0]` 实测过 `0x16D4E660000`（本次会话是 `0x1C03CB80000`），**ASLR 后会变** ——
   只拿它做"像不像堆指针"的判断（64 KiB 对齐 + 在 PTR_MIN..PTR_MAX 内），绝不硬编码。
3. 名字池**不混淆不加密**。拿 UE4 的 `FNameEntry` 头格式去扫 UE5 池本来就该 0 命中，
   那不是证据。
4. `probe()` **绝不能把 `Blocks[]` 里的 0 缓存下来**：池是懒增长的，缓存 0 会让之后的
   `block()` 直接短路成 0（踩过：probe() 之后 `name_at(0)` 突然全 None）。

只读。全部路径只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`。
"""

from __future__ import annotations

import json
import struct
from typing import Optional

from . import build as B

try:                                    # 只为一个常量：board_api.ALLOC_ALIGN
    from . import proc as _proc
    _BOARD = _proc.board_api
    PTR_MIN = _proc.PTR_MIN
    PTR_MAX = _proc.PTR_MAX
    ALLOC_ALIGN = getattr(_BOARD, "ALLOC_ALIGN", 0x10000)
except Exception:                       # pragma: no cover - 极端环境下退到字面量
    PTR_MIN, PTR_MAX, ALLOC_ALIGN = 0x10000, 0x7FFFFFFFFFFF, 0x10000

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
RVA_FNAME_POOL = B.RVA["FNamePool"]        # 0x0911B9C0
RVA_GNAMES_DECOY = B.RVA["GNames_decoy"]   # 0x090E2E28（**不是**池，仅作对照）

POOL_LOCK_OFF = 0x00
POOL_CURBLOCK_OFF = 0x08
POOL_CURSOR_OFF = 0x0C
POOL_BLOCKS_OFF = 0x10
BLOCK_BITS = 16
POOL_MAX_BLOCKS = 8192                     # 内联数组容量（构造器 memset 0x10000 bytes）
ENTRY_STRIDE = 2                           # 8 字节对齐 + len<<6 已经保证 2 字节对齐
ENTRY_LEN_SHIFT = 6
ENTRY_HASH_MASK = 0x1F                     # (head >> 1) & 0x1F
ENTRY_WIDE_BIT = 1
ENTRY_MAX_LEN = 0x400                      # 超过就是垃圾
POOL_PROBE_N = 0x40                        # 池头 hexdump 长度

OFF_UOBJECT_NAME = B.OFF_UOBJECT_NAME      # 0x18：UObject::Name 是 FName{idx@+0, number@+4}
OFF_CARD_ASSET_NAME = 0x50                 # UBaseCardObject::Name_0（FName，资产名）
OFF_CARD_TITLE = 0x58                      # UBaseCardObject::title（FText，牌名）
OFF_FTEXT_DATA = 0x00                      # FText 首字段 = FTextData*
OFF_FTEXT_STRING = 0x20                    # FTextData::Text = FString{ptr@+0, num@+8}

_FTEXT_HEAD_SIZES = (0x30, 0x28, 0x20, 0x18, 0x10)   # 逐级缩小，绕开未映射页
_MAX_TEXT_CHARS = 4096                     # 正常卡名/效果文本远小于此


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _fmt_hexdump(raw: Optional[bytes], label: str = "") -> str:
    """把字节串渲染成 hexdump 文本（不依赖 mem.hexdump，便于合成自检）。"""
    if not raw:
        return "  <%s> unreadable" % label
    out = []
    for off in range(0, len(raw), 16):
        row = raw[off:off + 16]
        hexs = " ".join("%02X" % c for c in row)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
        out.append("  %08X  %-47s  %s" % (off, hexs, txt))
    return "\n".join(out)


def _looks_like_heap_ptr(p: Optional[int]) -> bool:
    """像不像一个已经分配的堆指针（64 KiB 对齐 + 落在用户态范围）。"""
    if not p:
        return False
    return PTR_MIN <= p < PTR_MAX and p % ALLOC_ALIGN == 0


def _u16(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _i32(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<i", b, off)[0]


def entry_span(head: int) -> int:
    """一个条目占掉的**索引字节数**（= 块内下一个条目相对本条目的字节偏移）。

    实测（pid 3908，`Blocks[0]` 起）：
        `None`(len=4)          @ +0x00 → 下一个 @ +0x06
        `ByteProperty`(len=12) @ +0x06 → 下一个 @ +0x14
        `IntProperty`(len=11)  @ +0x14 → 下一个 @ +0x22
        `BoolProperty`(len=12) @ +0x22
    ⇒ 窄字符 `span = 2 + len`；宽字符（UTF-16）`span = 2 + 2*len`。
    ★ 这条**只用于"顺序读前几个 entry"**（probe 用）。
      按 `idx` 索引走的是 `idx*2` —— 见 `name_at` 的 docstring（索引是稀疏的）。
    """
    ln = head >> ENTRY_LEN_SHIFT
    return 2 + (ln * 2 if (head & ENTRY_WIDE_BIT) else ln)


def read_text(mem, addr: int, n: int, wide: bool) -> Optional[str]:
    """按需精确读 n 个"字符"并解码。**用 read_exact**，短读即失败，不静默截断。"""
    if not addr or n <= 0:
        return None
    raw = mem.read_exact(addr, n * (2 if wide else 1))
    if raw is None:
        return None
    try:
        return raw.decode("utf-16-le" if wide else "latin-1")
    except (UnicodeDecodeError, LookupError):
        return None


# --------------------------------------------------------------------------
# FNamePool
# --------------------------------------------------------------------------
class FNamePool:
    """KARDS（UE 5.6.1）运行进程里的 FNamePool，只读。

    `probe()` 是探测入口（顺便填好 `blocks[]`）；`name_at()` 不依赖 probe，
    自己按 `Blocks[idx>>16]` 现读块指针 —— 这样小池/合成池也能工作。
    """

    def __init__(self, mem, base: Optional[int] = None, rva: int = RVA_FNAME_POOL):
        if mem is None:
            raise ValueError("FNamePool 需要 mem（MemRO / board_api._Mem）")
        self.m = mem
        if base is None:
            base = getattr(mem, "base", None)
        if not base:
            raise ValueError("FNamePool 需要 base（模块基址）：Session.base 或显式传入")
        self.base = int(base)
        self.rva = int(rva)
        self.addr = self.base + self.rva
        self.block_bits = BLOCK_BITS
        self.layout: dict = {}
        self.blocks: list = []
        self.max_block = -1
        self.byte_cursor: Optional[int] = None
        self.chunk0: int = 0
        self.chunk0_offset: Optional[int] = None     # chunk0 里第 0 个 entry 的偏移
        self.ok = False

    # -- 探测 ------------------------------------------------------------
    def probe(self, size: int = POOL_PROBE_N) -> dict:
        """探测一次布局，返回可打印/可 JSON 的结果（只读，不改进程）。

        结果键：
            pool / base / rva / addr / header / header_hex / header_hexdump
            lock_value / lock_thread / max_block / byte_cursor
            max_block_plausible / block_ptrs / nonnull_blocks / cursor_in_block
            chunk0 / chunk0_aligned / chunk0_byte_len / chunk0_hexdump / entry_offset
            entries（chunk0 前 3 个 entry 解出的字符串）
            header_layout / layout_ok / chunk0_available / ok / reason
        """
        m, out = self.m, {}
        out.update({
            "pool": hex(self.addr), "base": hex(self.base), "rva": hex(self.rva),
            "addr": self.addr, "header": None, "header_hex": None,
            "header_hexdump": None, "lock_value": None, "lock_thread": None,
            "max_block": None, "byte_cursor": None, "max_block_plausible": False,
            "block_ptrs": [], "nonnull_blocks": 0, "cursor_in_block": None,
            "chunk0": None, "chunk0_aligned": False, "chunk0_byte_len": None,
            "chunk0_hexdump": None, "entry_offset": None, "entries": [],
            "header_layout": [], "layout_ok": False, "chunk0_available": False,
            "ok": False, "reason": None,
        })

        # 1) 池头 0x40 字节（一次定长读，绝不短读）
        head = m.read_exact(self.addr, size)
        if head is None:
            out["reason"] = "池头读不出来 @0x%X（base+RVA 不对？）" % self.addr
            self.layout, self.ok = out, False
            return out
        out["header"] = head.hex()
        out["header_hex"] = head.hex()
        out["header_hexdump"] = _fmt_hexdump(head, "FNamePool+0x00")

        # 2) 布局字段
        lock_lo = _u32(head, POOL_LOCK_OFF)
        lock_hi = _u32(head, POOL_LOCK_OFF + 4)
        cur_block = _u32(head, POOL_CURBLOCK_OFF)
        cursor = _u32(head, POOL_CURSOR_OFF)
        out["lock_value"] = lock_lo                    # SRWLock 低 32 位（本机实测 0xFFFFFFFF）
        out["lock_thread"] = lock_hi
        out["max_block"] = cur_block
        out["byte_cursor"] = cursor
        out["max_block_plausible"] = bool(cur_block < 0x10000)
        out["header_layout"] = [
            ("+0x00 u32", lock_lo, "SRWLock 锁值"),
            ("+0x04 u32", lock_hi, "SRWLock 线程 id"),
            ("+0x08 u32", cur_block, "CurrentBlock / MaxChunkIndex"),
            ("+0x0C u32", cursor, "ByteCursor"),
            ("+0x10 ..", None, "Blocks[8192] 内联数组（8 字节/项）"),
        ]
        self.byte_cursor = cursor
        self.max_block = cur_block

        # 3) Blocks[] 前 8 项（一次 0x40 定长读，避免逐项 syscall）
        blk_raw = m.read_exact(self.addr + POOL_BLOCKS_OFF, 8 * 8)
        if blk_raw is None:
            out["reason"] = "Blocks[] 前 8 项读不出来 @0x%X" % (self.addr + POOL_BLOCKS_OFF)
            self.layout, self.ok = out, False
            return out
        ptrs = [struct.unpack_from("<Q", blk_raw, i * 8)[0] for i in range(8)]
        out["block_ptrs"] = [("0x%X" % p) if p else None for p in ptrs]
        out["nonnull_blocks"] = sum(1 for p in ptrs if p)
        # ★ 只把**非空**的缓存进 self.blocks：0 只是个快照（池在增长），
        #   缓存 0 会污染后续 block() 的读取（踩过：probe() 之后 name_at(0) 直接失灵）。
        while len(self.blocks) < len(ptrs):
            self.blocks.append(0)
        for i, p in enumerate(ptrs):
            if p:
                self.blocks[i] = p

        # 4) chunk0
        chunk0 = ptrs[0]
        out["chunk0"] = ("0x%X" % chunk0) if chunk0 else None
        self.chunk0 = chunk0
        if not chunk0:
            out["reason"] = "Blocks[0] == 0（池还没被写入？）"
            self.layout, self.ok = out, False
            return out
        out["chunk0_aligned"] = _looks_like_heap_ptr(chunk0)

        nbytes = min(max(cursor, 0x100), 0x800)
        c0 = m.read_exact(chunk0, nbytes)
        if c0 is None and nbytes > 0x100:
            c0 = m.read_exact(chunk0, 0x100)
        if c0 is None:
            out["reason"] = "chunk0 读不出来 @0x%X" % chunk0
            self.layout, self.ok = out, False
            return out
        out["chunk0_byte_len"] = len(c0)
        out["chunk0_hexdump"] = _fmt_hexdump(c0[:0x60], "chunk0")
        out["chunk0_available"] = True

        # 4b) 第 0 个 entry 在 chunk0 里的偏移（实测 0；用 'None' 反查做兜底）
        off = 0
        if not (self.name_of_entry(chunk0) or "").strip():
            pos = c0.find(b"None")
            if 0 < pos <= 16:
                off = pos - ENTRY_STRIDE if pos >= ENTRY_STRIDE else 0
                if not (self.name_of_entry(chunk0 + off) or "").strip():
                    off = 0
        out["entry_offset"] = off
        self.chunk0_offset = off

        # 5) chunk0 里前 3 个 entry 解出的字符串
        #    顺序读取：`addr(k+1) = addr(k) + entry_span(head_k)`（advance = 2 + len）。
        #    ★ 同时给出**稀疏 idx = 块内字节偏移 / 2**（实测 0 / 3 / 10 / 17），
        #      因为 `name_at(1)`、`name_at(2)` 会落在 'None' 字符串内部。
        base_off = off
        ea = chunk0 + base_off
        for k in range(3):
            raw2 = m.read_exact(ea, 2)
            h = _u16(raw2) if raw2 else None
            off_in_block = ea - chunk0
            out["entries"].append({
                "index": off_in_block // ENTRY_STRIDE,      # 稀疏 idx（= byteOffset/2）
                "ordinal": k,                               # 顺序序号
                "offset": off_in_block,
                "addr": "0x%X" % ea, "head": h,
                "len": (h >> ENTRY_LEN_SHIFT) if h is not None else None,
                "hash": ((h >> 1) & ENTRY_HASH_MASK) if h is not None else None,
                "is_wide": bool(h & ENTRY_WIDE_BIT) if h is not None else None,
                "name": self.name_of_entry(ea),
            })
            if h is None:
                break
            ea += entry_span(h)

        # 6) 结论
        out["cursor_in_block"] = bool(cursor) and cursor < 0x20000
        out["layout_ok"] = bool(out["max_block_plausible"] and out["nonnull_blocks"] >= 1)
        bad = [e for e in out["entries"] if not (e.get("name") or "").strip()]
        out["ok"] = bool(out["layout_ok"] and out["chunk0_available"]
                         and out["chunk0_aligned"] and len(out["entries"]) == 3 and not bad)
        if not out["ok"] and not out["reason"]:
            if bad:
                out["reason"] = "chunk0 前 3 个 entry 没解出字符串：%r" % (out["entries"],)
            elif not out["chunk0_aligned"]:
                out["reason"] = "Blocks[0]=0x%X 不像堆指针（未 64KiB 对齐？）" % chunk0
            else:
                out["reason"] = "布局可疑：max_block=%s nonnull=%s" % (
                    out["max_block"], out["nonnull_blocks"])
        self.layout, self.ok = out, out["ok"]
        return out

    # -- entry → 字符串 ---------------------------------------------------
    def block(self, block_index: int) -> int:
        """`Blocks[block_index]`（越界/读不出返回 0）。

        缓存里只有**非空**指针；缓存没有就现读一次（池会随进程增长，块指针是懒分配的）。
        """
        if block_index < 0 or block_index >= POOL_MAX_BLOCKS:
            return 0
        if 0 <= block_index < len(self.blocks) and self.blocks[block_index]:
            return self.blocks[block_index]
        p = self.m.ptr(self.addr + POOL_BLOCKS_OFF + block_index * 8) or 0
        if p:
            if block_index >= len(self.blocks):
                self.blocks.extend([0] * (block_index + 1 - len(self.blocks)))
            self.blocks[block_index] = p
        return p

    def entry_addr(self, idx: int, block_bits: int = BLOCK_BITS) -> int:
        """`entry = Blocks[idx >> block_bits] + (idx & ((1<<bits)-1)) * 2`。"""
        if idx is None or idx < 0:
            return 0
        b = self.block(idx >> block_bits)
        return (b + ((idx & ((1 << block_bits) - 1)) * ENTRY_STRIDE)) if b else 0

    def entry_head(self, idx: int, block_bits: int = BLOCK_BITS):
        """读 entry 头，返回 (addr, head, 2 字节原始头)。读不出返回 (0, None, None)。

        头是 2 字节、字符串紧跟其后；entry 地址 2 字节对齐，所以**两次 2 字节定长读**
        一定落在同一个页内（4KB 页），不会出现"读 4 字节跨页失败"的假阴性。
        """
        a = self.entry_addr(idx, block_bits)
        if not a:
            return 0, None, None
        raw = self.m.read_exact(a, 2)
        if raw is None:                     # 极端情况：头正好压在页尾，退一步只读 1 字节
            raw = self.m.read_exact(a, 1)
            if raw is None:
                return 0, None, None
        return a, _u16(raw), raw

    def name_of_entry(self, a: int, depth: int = 0) -> Optional[str]:
        """从一个已经拿到的 **entry 地址** 解字符串（不经过 `Blocks[]` 索引）。

        与 `name_at` 共用同一套头部解码；拆出来是为了：(a) 可以在已知偏移上单独验证；
        (b) probe() 摆出 chunk0 前几个 entry 时直接复用它。
        """
        if not a:
            return None
        raw = self.m.read_exact(a, 2)
        if raw is None:
            raw = self.m.read_exact(a, 1)
            if raw is None:
                return None
        head = _u16(raw)
        ln = head >> ENTRY_LEN_SHIFT
        if ln == 0:
            if depth >= 4:
                return None
            nxt = self.m.i32(a + 2)
            if nxt is None or nxt < 0:
                return None
            return self.name_at(nxt, self.block_bits, depth + 1)
        if ln > ENTRY_MAX_LEN:
            return None
        return read_text(self.m, a + 2, ln, bool(head & ENTRY_WIDE_BIT))

    def name_at(self, idx: int, block_bits: int = BLOCK_BITS, depth: int = 0) -> Optional[str]:
        """第 idx 个 FName entry 的字符串（不含 number 后缀）。

        ★ 索引是**稀疏**的：`FNameEntryId = 块内字节偏移 / 2`，而块内 entry 是按
          `advance = 2 + len`（窄）/ `2 + 2*len`（宽）**依次紧挨着**摆的。
          所以实测池里（`Blocks[0]` 起）：
              name_at(0)  = 'None'          （byte 0）
              name_at(3)  = 'ByteProperty'  （byte 6）
              name_at(10) = 'IntProperty'   （byte 20）
              name_at(17) = 'BoolProperty'  （byte 34）
          **idx 1 / idx 2 落在 'None' 的字符串内部，返回乱码是正常行为**，不是 bug。
          想按"第几个名字"顺序数，请用 `name_of_entry(block(0) + 偏移)` 或
          `pool.iter_entries()`（顺序推进），别用连续 idx。

        - `len == 0` → 间接项：返回 `name_at(next_idx)`（number 由 `fname_of` 负责拼）。
        - `bIsWide` → UTF-16LE，否则 latin-1。
        - 读不出 / `len > 0x400` / 间接链太深 → None。
        """
        if idx is None or idx < 0:
            return None
        a = self.entry_addr(idx, block_bits)
        if not a:
            return None
        return self.name_of_entry(a, depth)

    def iter_entries(self, start: int = 0, count: int = 3, block: int = 0,
                     block_bits: int = BLOCK_BITS) -> list:
        """从块内字节偏移 `start` 起**顺序**读 count 个 entry。

        这才是"第 1/2/3 个名字"的正确读法（不用连续 idx，因为索引稀疏）。
        返回 `[{"offset", "idx", "addr", "head", "len", "hash", "is_wide", "name"}, ...]`
        """
        b = self.block(block)
        if not b:
            return []
        out, ea = [], b + start
        for _ in range(max(0, count)):
            raw = self.m.read_exact(ea, 2)
            h = _u16(raw) if raw else None
            off = ea - b
            out.append({
                "offset": off, "idx": off // ENTRY_STRIDE, "addr": "0x%X" % ea, "head": h,
                "len": (h >> ENTRY_LEN_SHIFT) if h is not None else None,
                "hash": ((h >> 1) & ENTRY_HASH_MASK) if h is not None else None,
                "is_wide": bool(h & ENTRY_WIDE_BIT) if h is not None else None,
                "name": self.name_of_entry(ea),
            })
            if h is None:
                break
            ea += entry_span(h)
        return out

    def fname(self, index: int, number: int = 0, block_bits: int = BLOCK_BITS) -> Optional[str]:
        """FName 的**显示形式**：`number > 0` 时 UE 显示 `Name_%d`（number-1）。"""
        s = self.name_at(index, block_bits)
        if s is None:
            return None
        if number and number > 0:
            return "%s_%d" % (s, number - 1)
        return s

    def fname_of(self, uobject_ptr: int, off: int = OFF_UOBJECT_NAME) -> Optional[str]:
        """读 `UObject::Name`（FName{idx@+0, number@+4}）→ 显示字符串。

        `FName::ToString()` 的规则：`number > 0` 时追加 `_%d`（number-1）。
        """
        if not uobject_ptr:
            return None
        raw = self.m.read_exact(uobject_ptr + off, 8)
        if raw is None:
            return None
        idx, number = struct.unpack("<ii", raw)
        if idx < 0:
            return None
        return self.fname(idx, number)

    # -- 卡牌资产名 ------------------------------------------------------
    def card_asset_name(self, card_ptr: int) -> Optional[str]:
        """`UBaseCardObject+0x50` 的 FName（资产名，期望形如 `card_unit_...`）。

        先读 `Name_0@+0x50`（SDK 里就是资产名）；它解不出来时退回 `UObject::Name@+0x18`。
        两者都读不出才返回 None。
        """
        if not card_ptr:
            return None
        raw = self.m.read_exact(card_ptr + OFF_CARD_ASSET_NAME, 8)
        if raw is not None:
            idx, number = struct.unpack("<ii", raw)
            if idx > 0:
                s = self.fname(idx, number)
                if s:
                    return s
        return self.fname_of(card_ptr, OFF_UOBJECT_NAME)

    # -- 诊断 ------------------------------------------------------------
    def debug_card(self, card_ptr: int) -> dict:
        """一张 UBaseCardObject 上两条 FName 路线的原始读数（对不上时看这里）。"""
        out = {"ptr": "0x%X" % (card_ptr or 0)}
        for label, off in (("fname_50", OFF_CARD_ASSET_NAME),
                           ("uobject_18", OFF_UOBJECT_NAME)):
            raw = self.m.read_exact(card_ptr + off, 8) if card_ptr else None
            if raw is None:
                out[label] = None
                out[label + "_raw"] = None
                continue
            idx, number = struct.unpack("<ii", raw)
            out[label] = self.fname(idx, number)
            out[label + "_raw"] = {"index": idx, "number": number}
        return out

    def describe(self) -> str:
        return ("FNamePool @0x%X (base=0x%X + 0x%X) ok=%s max_block=%s "
                "byte_cursor=%s chunk0=%s" % (
                    self.addr, self.base, self.rva, self.ok, self.max_block,
                    self.byte_cursor, ("0x%X" % self.chunk0) if self.chunk0 else None))


# --------------------------------------------------------------------------
# FText 明文回退路线（FName 读不出时用）
# --------------------------------------------------------------------------
def _ftext_string(mem, data: int) -> Optional[str]:
    """从 FTextData 里取 FString{ptr@+0, num@+8}（内存里是明文 UTF-16）。

    ★ 用 read_exact：先按 0x30/0x28/0x20/0x18/0x10 逐级缩小找**能定长读到**的头，
      （FTextData 是 0x28 字节的小分配，直接按大长度读可能撞上未映射页而整体失败），
      再按声明的字符数**精确**读字符串本体。
    """
    for sz in _FTEXT_HEAD_SIZES:
        head = mem.read_exact(data, sz)
        if head is None:
            continue
        p = struct.unpack_from("<Q", head, OFF_FTEXT_STRING)[0]
        n = _i32(head, OFF_FTEXT_STRING + 8)
        if not (PTR_MIN <= p < PTR_MAX) or n <= 0 or n > _MAX_TEXT_CHARS:
            continue
        s = read_text(mem, p, n, True)
        if s:
            return s.rstrip("\x00") or None
    return None


def ftext_plain(mem, ftext_addr: int) -> str | None:
    """`FText` → `FTextData+0x20` 的明文 UTF-16（回退路线）。

    FText 的首字段就是 `FTextData*`（引用计数落在 +0x08；+0x18 是 FTextHistory*）。
    实测有效路径：`card+0x58 -> FTextData+0x20 -> FString`。
    """
    if not ftext_addr:
        return None
    data = mem.ptr(ftext_addr + OFF_FTEXT_DATA) or 0
    if not data:
        return None
    s = _ftext_string(mem, data)
    if s:
        return s
    # 极少数情况下 +0x00 上的引用不可用，而 FTextData 指针落在 +0x08/+0x10
    for off in (0x08, 0x10):
        d = mem.ptr(ftext_addr + off) or 0
        if d and d != data:
            s = _ftext_string(mem, d)
            if s:
                return s
    return None


def ftext_at(mem, obj_addr: int, ftext_off: int) -> str | None:
    """直接从 `obj_addr + ftext_off` 读一个内联 FText（例如卡牌 title @0x58）。"""
    if not obj_addr:
        return None
    return ftext_plain(mem, obj_addr + ftext_off)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
# 注：这里**没有**"合成池"自检。手写一个逐字节精确的 chunk（既满足"顺序相接"又满足
# "稀疏 idx = byte/2"）性价比太低，而且它已经制造过假 FAIL；算法正确性由**实机断言**
# 覆盖（name_at(0/3/10/17) + probe() + entry0 头 + 真实卡牌的 FName/FText 双路线）。
# 需要离线验证算法时，直接对着 `name_of_entry` 传一个已知的 entry 地址即可。
def _rec(rows: list, name, got, want):
    ok = (got == want)
    rows.append((name, got, want, ok))
    return ok


def selftest(mem=None, base=None) -> list:
    """自检：常量/头部解码（离线）+ 实机断言（有 mem/base 时）。

    返回 `[(name, got, want, ok), ...]`；不抛。
    """
    rows: list = []

    # --- 纯常量 / 头部解码（不需要进程）--------------------------------
    _rec(rows, "RVA_FNAME_POOL", hex(RVA_FNAME_POOL), "0x911b9c0")
    _rec(rows, "RVA_GNAMES_decoy(≠池)", hex(RVA_GNAMES_DECOY), "0x90e2e28")
    _rec(rows, "POOL_BLOCKS_OFF", hex(POOL_BLOCKS_OFF), "0x10")
    _rec(rows, "entry_stride", ENTRY_STRIDE, 2)

    hdr = bytes([0x1E, 0x01]) + b"None" + bytes([0x10, 0x03]) + b"ByteProperty"
    _rec(rows, "chunk0 头 'None' head>>6", _u16(hdr, 0) >> ENTRY_LEN_SHIFT, 4)
    _rec(rows, "chunk0 头 'None' bIsWide", _u16(hdr, 0) & ENTRY_WIDE_BIT, 0)
    _rec(rows, "chunk0 头 'None' 字符串", hdr[2:6].decode("latin-1"), "None")
    _rec(rows, "'ByteProperty' head>>6", _u16(hdr, 6) >> ENTRY_LEN_SHIFT, 12)
    _rec(rows, "'ByteProperty' 字符串", hdr[8:20].decode("latin-1"), "ByteProperty")
    # ★ 实测（pid 3908）：块内逐条紧挨，advance = 2 + len ⇒ span 就是 6 / 14
    _rec(rows, "entry_span('None') == 2+len", entry_span(_u16(hdr, 0)), 6)
    _rec(rows, "entry_span('ByteProperty') == 2+len", entry_span(_u16(hdr, 6)), 14)

    # --- 实机 ------------------------------------------------------------
    if mem is None or not base:
        rows.append(("实机:未 attach", "跳过", "拿到 mem/base", None))
        return rows

    pool = FNamePool(mem, base)
    info = pool.probe()
    rows.append(("实机:probe().ok", info.get("ok"), True, bool(info.get("ok"))))
    rows.append(("实机:Blocks[0] 是合法堆指针", info.get("chunk0_aligned"), True,
                 bool(info.get("chunk0_aligned"))))
    rows.append(("实机:chunk0 可读", info.get("chunk0_available"), True,
                 bool(info.get("chunk0_available"))))
    rows.append(("实机:ByteCursor 合理", info.get("cursor_in_block"), True,
                 bool(info.get("cursor_in_block"))))

    # ★ 实测（pid 3908）：chunk0 的 entry 逐个落在 byte 0/6/20/34，
    #   而 FNameEntryId = byteOffset / 2 ⇒ idx 0/3/10/17。
    #   （idx 1 / idx 2 落在 "None" 字符串内部，读到乱码是**正常**的，见 name_at docstring。）
    _rec(rows, "实机:name_at(0) == None", pool.name_at(0), "None")
    _rec(rows, "实机:name_at(3) == ByteProperty", pool.name_at(3), "ByteProperty")
    _rec(rows, "实机:name_at(10) == IntProperty", pool.name_at(10), "IntProperty")
    _rec(rows, "实机:name_at(17) == BoolProperty", pool.name_at(17), "BoolProperty")

    # 顺序读取（probe 用 iter_entries：不靠 idx，靠 head 的 span 推进）
    seq = [e.get("name") for e in pool.iter_entries(0, 3)]
    _rec(rows, "实机:iter_entries(0,3)", seq, ["None", "ByteProperty", "IntProperty"])
    ents = info.get("entries") or []
    _rec(rows, "实机:probe 第 2 个 entry 的偏移",
         (ents[1].get("offset") if len(ents) > 1 else None), 6)
    _rec(rows, "实机:probe 第 3 个 entry 的偏移",
         (ents[2].get("offset") if len(ents) > 2 else None), 20)

    a, head, _raw = pool.entry_head(0)
    rows.append(("实机:entry0 头可读", head is not None, True, head is not None))
    if head is not None:
        rows.append(("实机:entry0 head>>6 == 4", head >> ENTRY_LEN_SHIFT, 4,
                     (head >> ENTRY_LEN_SHIFT) == 4))

    # FText 回退路线 vs FName 路线：拿真实卡做冒烟
    try:
        from . import proc as _p
        s = _p.attach(require_build=False, validate_md5=False)
        st = s.snapshot()
        cards = [c for c in (st.cards or []) if (c.raw or {}).get("ptr")]
        rows.append(("实机:snapshot 卡数 > 0", len(cards), "> 0", bool(cards)))
        if cards:
            c = cards[0]
            ptr = c.raw["ptr"]
            rows.append(("实机:FText 明文回退可用", bool(c.name), True, bool(c.name)))
            got = pool.card_asset_name(ptr)
            rows.append(("实机:card_asset_name 非 None", got, "非 None", bool(got)))
            rows.append(("实机:fname_of(+0x18) 可读",
                         pool.fname_of(ptr) is not None, True,
                         pool.fname_of(ptr) is not None))
    except Exception as e:                                   # pragma: no cover
        rows.append(("实机:snapshot", "异常 %r" % (e,), "可读", False))

    return rows


# 实机：解析几张小卡（FName 路线 vs FText 明文路线）
# --------------------------------------------------------------------------
def _card_candidates(sess, limit: int = 12) -> list:
    """收集可用的 UBaseCardObject 指针：优先进 board_api 快照，其次直接扫 GS 的 TMap。"""
    out = []
    try:
        st = sess.snapshot()
        for c in (st.cards or []):
            ptr = (c.raw or {}).get("ptr")
            if ptr:
                out.append({"ptr": ptr, "ftext": c.name, "card_id": c.card_id,
                            "src": "snapshot"})
    except Exception as e:
        out.append({"ptr": 0, "error": "snapshot 失败: %r" % (e,), "src": "snapshot"})
    if len([r for r in out if r.get("ptr")]) >= limit:
        return out[:limit]

    # 回退：AllCardsInBattle（GS + 0x538），元素 24 字节，指针在 +0x08
    try:
        from .world import Locator
        loc = Locator(sess.m, sess.base)
        gs = loc.gamestate
        if gs:
            arr = sess.m.ptr(gs + 0x538) or 0
            num = sess.m.i32(gs + 0x538 + 8) or 0
            seen = {r.get("ptr") for r in out}
            for i in range(max(0, min(num, 400))):
                p = sess.m.ptr(arr + i * 24 + 8) or 0
                if not p or p in seen:
                    continue
                seen.add(p)
                out.append({"ptr": p, "ftext": None, "card_id": None, "src": "TMap24"})
    except Exception as e:
        out.append({"ptr": 0, "error": "TMap 回退失败: %r" % (e,), "src": "TMap24"})
    return out[:limit]


def _resolve_cards(sess, pool, limit: int = 5) -> tuple:
    """解析若干张真实卡：返回 (rows, diagnostics)。rows 里同时带 FName 与 FText 名。"""
    rows, diags = [], []
    for cand in _card_candidates(sess, max(limit * 3, 12)):
        if not cand.get("ptr"):
            if cand.get("error"):
                diags.append(cand["error"])
            continue
        ptr = cand["ptr"]
        dbg = pool.debug_card(ptr)
        ftext = cand.get("ftext")
        if ftext is None:
            ftext = ftext_at(sess.m, ptr, OFF_CARD_TITLE)
        asset = pool.card_asset_name(ptr)
        rows.append({
            "ptr": "0x%X" % ptr,
            "card_id": cand.get("card_id"),
            "src": cand.get("src"),
            "asset_name": asset,
            "fname_50": dbg.get("fname_50"),
            "fname_50_index": (dbg.get("fname_50_raw") or {}).get("index"),
            "fname_50_number": (dbg.get("fname_50_raw") or {}).get("number"),
            "uobject_18": dbg.get("uobject_18"),
            "ftext": ftext,
            "ftext_ok": bool(ftext),
        })
        if len(rows) >= limit:
            break
    return rows, diags


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _fmt_probe(info: dict) -> str:
    L = []
    L.append("FNamePool @ %s  (base=%s + rva=%s)" % (info.get("pool"), info.get("base"),
                                                    info.get("rva")))
    L.append("  ok=%s  reason=%s" % (info.get("ok"), info.get("reason")))
    lv = info.get("lock_value")
    cv = info.get("byte_cursor")
    L.append("  +0x00 SRWLock      lock_value=%s  lock_thread=%s" % (
        lv, info.get("lock_thread")))
    L.append("  +0x08 CurrentBlock/MaxChunkIndex = %s   plausible=%s" % (
        info.get("max_block"), info.get("max_block_plausible")))
    L.append("  +0x0C ByteCursor   = %s%s  in_block=%s" % (
        cv, (" (0x%X)" % cv) if isinstance(cv, int) else "", info.get("cursor_in_block")))
    L.append("  +0x10 Blocks[0..7] = %s  (nonnull=%s)" % (
        ", ".join(str(x) for x in (info.get("block_ptrs") or [])),
        info.get("nonnull_blocks")))
    L.append("  Blocks[0] = %s  64KiB 对齐(像堆指针) = %s" % (
        info.get("chunk0"), info.get("chunk0_aligned")))
    L.append("  chunk0 可读 = %s (读到 %s 字节)  entry_offset=%s" % (
        info.get("chunk0_available"), info.get("chunk0_byte_len"),
        info.get("entry_offset")))
    L.append("---- 池头 0x40 字节 ----")
    L.append(info.get("header_hexdump") or "  <none>")
    L.append("---- chunk0 前 0x60 字节 ----")
    L.append(info.get("chunk0_hexdump") or "  <none>")
    L.append("---- chunk0 顺序读取前 3 个 entry（稀疏 idx = 字节偏移/2）----")
    for e in (info.get("entries") or []):
        L.append("  #%s @%-16s off=0x%-4X idx=%-5s head=0x%04X len=%-4s wide=%-5s -> %r"
                 % (e.get("ordinal"), e.get("addr"), e.get("offset") or 0,
                    e.get("index"),
                    e.get("head") if e.get("head") is not None else 0,
                    e.get("len"), e.get("is_wide"), e.get("name")))
    L.append("  ★ 索引稀疏：name_at(%s/%s/%s) 才是这三个；name_at(1)/(2) 落在字符串内部属正常"
             % tuple([e.get("index") for e in (info.get("entries") or [])[:3]] +
                     [None] * (3 - len(info.get("entries") or []))))
    return "\n".join(L)


def _fmt_cards(rows: list, diags: list) -> str:
    L = ["---- 实机卡牌：FName 路线 vs FText 明文路线 ----"]
    if not rows:
        L.append("  <没拿到任何 UBaseCardObject 指针>")
    for r in rows:
        L.append("  card=%-14s id=%-6s src=%-8s asset(Name_0@0x50 idx=%s num=%s)=%-32s "
                 "UObject::Name@0x18=%-26s FText=%r" % (
                     r.get("ptr"), r.get("card_id"), r.get("src"),
                     r.get("fname_50_index"), r.get("fname_50_number"),
                     r.get("asset_name"), r.get("uobject_18"), r.get("ftext")))
    for d in diags:
        L.append("  [诊断] %s" % d)
    return "\n".join(L)


def main(argv=None) -> int:
    """CLI：probe + selftest + 实机解析几张小卡。返回退出码（0 = 全通过）。"""
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m kardsmem.names",
        description="KARDS 内存侧只读工具：真名字池 FNamePool → FName；FText 明文回退")
    ap.add_argument("--cards", "-n", type=int, default=5, help="实机解析多少张卡（默认 5）")
    ap.add_argument("--json", action="store_true", help="把结果以 JSON 打到 stdout")
    ap.add_argument("--no-attach", action="store_true", help="只做离线自检，不碰进程")
    a = ap.parse_args(argv)

    result = {"session": None, "probe": None, "selftest": [], "cards": [], "diags": []}
    sess = None
    try:
        if not a.no_attach:
            from . import proc as _p
            sess = _p.attach(require_build=False, validate_md5=False)
            result["session"] = sess.info.as_dict()
    except Exception as e:
        result["diags"].append("attach 失败: %r" % (e,))

    pool = None
    if sess is not None:
        try:
            pool = FNamePool(sess.m, sess.base)
            result["probe"] = pool.probe()
        except Exception as e:
            result["diags"].append("FNamePool 构造/probe 失败: %r" % (e,))

    rows = selftest(sess.m if sess else None, sess.base if sess else None)
    result["selftest"] = [{"name": n, "got": g, "want": w, "ok": b} for (n, g, w, b) in rows]

    if pool is not None:
        try:
            result["cards"], more = _resolve_cards(sess, pool, a.cards)
            result["diags"].extend(more)
        except Exception as e:
            result["diags"].append("解析卡牌失败: %r" % (e,))

    failed = [r for r in rows if r[3] is False]
    result["ok"] = bool(pool is not None and result["probe"] and result["probe"].get("ok")
                        and not failed)

    out = sys.stdout
    if a.json:
        json.dump(result, out, ensure_ascii=False, indent=2, default=str)
        out.write("\n")
    else:
        out.write("=" * 78 + "\n")
        out.write("session: %s\n" % (result["session"] if result["session"] else "(未 attach)"))
        for d in result["diags"]:
            out.write("  [诊断] %s\n" % d)
        out.write("=" * 78 + "\n")
        out.write((_fmt_probe(result["probe"]) if result["probe"]
                   else "FNamePool: probe 未执行") + "\n")
        out.write("=" * 78 + "\n")
        out.write(_fmt_cards(result["cards"], []) + "\n")
        out.write("=" * 78 + "\n")
        out.write("selftest:\n")
        for (n, g, w, b) in rows:
            out.write("  [%s] %-38s got=%r want=%r\n" % (
                {True: "PASS", False: "FAIL", None: "SKIP"}.get(b, "?"), n, g, w))
        p = sum(1 for r in rows if r[3] is True)
        f = sum(1 for r in rows if r[3] is False)
        s = sum(1 for r in rows if r[3] is None)
        out.write("  => PASS %d / FAIL %d / SKIP %d\n" % (p, f, s))
        out.write("RESULT: %s\n" % ("OK" if result["ok"] else "NOT OK"))
    out.flush()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
