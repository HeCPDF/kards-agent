# -*- coding: utf-8 -*-
"""kardsmem.rvascan —— **运行时从进程里扫出 RVA**（不再靠检进 VCS 的版本表）。

动机（用户 2026-10-03）：调用者指定版本不合理；RVA 可以在运行时花时间扫描并生成。
⇒ 不维护"版本 → RVA"的表，也不需要 `KARDS_BUILD`。游戏在跑，就从它的内存里**按结构形状**找出：

  | 名字                 | 怎么找（都是版本无关的结构特征）                                                         |
  |----------------------|------------------------------------------------------------------------------------------|
  | `FNamePool`          | `.data` 里的 8 字节槽 S：`P=S-0x10` 处 CurrentBlock/Cursor 合理、`Blocks[0]=S 的值` 指向的块里第 0 个条目是 `None` |
  | `GObjects`           | `.data` 里的 `FUObjectArray` 头：Objects 指针 + Max/Num/MaxChunks/NumChunks 自洽，且前几个对象的 vtable 落在模块内、ClassPrivate 可读 |
  | `GWorld`             | 先在 GObjects 里找类名 `World` 的实例（排除 `Default__World`），再在 `.data` 里找"存着这个地址"的槽 |
  | `UObject_ProcessEvent` | 对象采样：绝大多数对象的 vtable[`PROCESS_EVENT_IDX`] 指向同一个函数                    |

返回的 RVA 与旧表（SDK dump + IDA 得到的真值）逐项对得上才算过 —— 旧表现在只当**回归基准**
（`tests/fixtures/golden_rva.json`，不参与运行时选择）。
扫描结果按**游戏自报版本**缓存（`version.py` 读的 `ProjectVersion`）；缓存加载前要快速复验，复验不过就重扫。

只读。纯函数部分（`find_*_in_blob`）不碰进程，可用合成数据离线测试。
"""
from __future__ import annotations

import json
import os
import struct
import time
from typing import Callable, Optional

PROCESS_EVENT_IDX = 0x4C           # UObject vtable 里 ProcessEvent 的下标（UE 5.x；与 build.PROCESS_EVENT_IDX 同值）
PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF
ELEMENTS_PER_CHUNK = 0x10000
FUOBJECTITEM_SIZE = 0x18
OFF_UOBJECT_VTABLE, OFF_UOBJECT_CLASS, OFF_UOBJECT_NAME = 0x00, 0x10, 0x18
POOL_CURBLOCK_OFF, POOL_CURSOR_OFF, POOL_BLOCKS_OFF = 0x08, 0x0C, 0x10
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "kards-agent", "rva-cache")


class ScanError(RuntimeError):
    """某一项扫不出来（原因写在消息里）。"""


# ---------------------------------------------------------------- PE 节
def sections(read: Callable[[int, int], Optional[bytes]], base: int) -> list:
    """进程内模块头 → `[(name, rva, vsize, characteristics)]`。"""
    hdr = read(base, 0x1000)
    if not hdr or hdr[:2] != b"MZ":
        raise ScanError("读不到模块 PE 头")
    pe = struct.unpack_from("<I", hdr, 0x3C)[0]
    if hdr[pe:pe + 4] != b"PE\0\0":
        raise ScanError("PE 签名不对")
    nsec = struct.unpack_from("<H", hdr, pe + 6)[0]
    opt = struct.unpack_from("<H", hdr, pe + 20)[0]
    out = []
    for i in range(nsec):
        o = pe + 24 + opt + i * 40
        name = hdr[o:o + 8].split(b"\0")[0].decode("ascii", "replace")
        vsize, rva, _rs, _ro = struct.unpack_from("<IIII", hdr, o + 8)
        ch = struct.unpack_from("<I", hdr, o + 36)[0]
        out.append((name, rva, vsize, ch))
    return out


def writable_blobs(read, base, secs=None, chunk: int = 1 << 20) -> list:
    """可写节（`.data`）的内容：`[(rva, bytes)]`。全局变量（GObjects/FNamePool/GWorld）都在这里。"""
    secs = secs or sections(read, base)
    out = []
    for name, rva, vsize, ch in secs:
        if not (ch & 0x80000000) or ch & 0x20000000:          # 要可写、且不是代码节
            continue
        parts, off = [], 0
        while off < vsize:
            n = min(chunk, vsize - off)
            d = read(base + rva + off, n)
            parts.append(d if d is not None else b"\0" * n)
            off += n
        out.append((rva, b"".join(parts)))
    return out


def _slots(blob: bytes):
    """按 8 字节对齐迭代 `(偏移, 值)`；只给非 0 值。"""
    mv = memoryview(blob[: len(blob) // 8 * 8]).cast("Q")
    for i, v in enumerate(mv):
        if v:
            yield i * 8, v


def _is_user_ptr(v: int) -> bool:
    return PTR_MIN <= v < PTR_MAX


# ---------------------------------------------------------------- 纯函数：在 blob 里找候选
def find_fnamepool_in_blob(blob: bytes, blob_rva: int, read) -> list:
    """候选 FNamePool RVA 列表。判据：Blocks[0] 指向的块里第 0 条是 `None`（FNameEntry：u16 头、len=head>>6、非宽字符）。"""
    out = []
    for off, v in _slots(blob):
        if off < POOL_BLOCKS_OFF or not _is_user_ptr(v) or v & 0xFFFF:
            continue                                           # 块指针是 64 KiB 对齐的堆块
        p = off - POOL_BLOCKS_OFF
        curblock = struct.unpack_from("<I", blob, p + POOL_CURBLOCK_OFF)[0]
        cursor = struct.unpack_from("<I", blob, p + POOL_CURSOR_OFF)[0]
        if curblock > 0x1000 or cursor > 0x20000:
            continue
        ent = read(v, 8)
        if not ent or len(ent) < 6:
            continue
        head = struct.unpack_from("<H", ent, 0)[0]
        if (head & 1) == 0 and (head >> 6) == 4 and ent[2:6] == b"None":
            out.append(blob_rva + p)
    return out


def find_gobjects_in_blob(blob: bytes, blob_rva: int, read, module_lo: int, module_hi: int) -> list:
    """候选 GObjects RVA 列表（`FUObjectArray` 头自洽 + 前几个对象像样）。判据参照 Dumper-7 的 ObjectArray::Init。"""
    out = []
    for off, v in _slots(blob):
        if off + 0x20 > len(blob) or not _is_user_ptr(v):
            continue
        mx, num, mxc, nc = struct.unpack_from("<iiii", blob, off + 0x10)
        # Dumper-7 `ObjectArray` 对 FChunkedFixedUObjectArray 的判据：1≤NumChunks≤0x14、6≤MaxChunks≤0x5FF、
        # Num>0x800、Max>0x10000、Max%16==0、floor(Num/每块)+1==NumChunks（刚好整除时我们也认 ceil）。
        if not (0x800 < num <= mx and mx > 0x10000 and mx % 16 == 0 and 1 <= nc <= 0x14 and 6 <= mxc <= 0x5FF):
            continue
        if nc not in (num // ELEMENTS_PER_CHUNK + 1, (num + ELEMENTS_PER_CHUNK - 1) // ELEMENTS_PER_CHUNK):
            continue
        c0 = read(v, 8)
        if not c0:
            continue
        chunk0 = struct.unpack("<Q", c0)[0]
        if not _is_user_ptr(chunk0):
            continue
        items = read(chunk0, FUOBJECTITEM_SIZE * 16)
        if not items or len(items) < FUOBJECTITEM_SIZE * 16:
            continue
        good = 0
        for i in range(16):
            obj = struct.unpack_from("<Q", items, i * FUOBJECTITEM_SIZE)[0]
            if not _is_user_ptr(obj):
                continue
            head = read(obj, 0x20)
            if not head or len(head) < 0x20:
                continue
            vt = struct.unpack_from("<Q", head, OFF_UOBJECT_VTABLE)[0]
            cls = struct.unpack_from("<Q", head, OFF_UOBJECT_CLASS)[0]
            if module_lo <= vt < module_hi and _is_user_ptr(cls):
                good += 1
        if good >= 8:
            out.append(blob_rva + off)
    return out


def find_slots_holding(blobs: list, values: set) -> list:
    """`.data` 里 8 字节槽的值 ∈ `values` 的所有 RVA（GWorld 找槽用）。"""
    out = []
    for rva, blob in blobs:
        for off, v in _slots(blob):
            if v in values:
                out.append(rva + off)
    return out


def count_code_refs(text_blob: bytes, text_rva: int, targets) -> dict:
    """`.text` 里 RIP 相对引用（disp32，后面可跟 0/1/4 字节立即数）命中各目标 RVA 的次数。

    Dumper-7 对 GWorld 多候选的取舍思路：真正的全局被代码大量引用，复制品/缓存槽几乎没人引用。"""
    import numpy as np
    out = {t: 0 for t in targets}
    n = len(text_blob) - 4
    if n <= 0:
        return out
    a = np.frombuffer(text_blob, dtype=np.uint8)
    v = (a[:n].astype(np.int64) | (a[1:n + 1].astype(np.int64) << 8) | (a[2:n + 2].astype(np.int64) << 16)
         | (a[3:n + 3].astype(np.int64) << 24))
    v = np.where(v >= 1 << 31, v - (1 << 32), v)
    pos = np.arange(n, dtype=np.int64) + text_rva
    for t in targets:
        for k in (0, 1, 4):
            out[t] += int(np.count_nonzero(v == (t - (pos + 4 + k))))
    return out


def pick_by_code_refs(read, base: int, secs: list, slots: list, ratio: int = 4):
    """多个候选槽 ⇒ 取被代码引用最多的那个；必须是第二名的 `ratio` 倍以上，否则 None（不猜）。"""
    text = [s for s in secs if s[0] == ".text"]
    if not text:
        return None
    _n, rva, vsize, _c = text[0]
    cnt = {t: 0 for t in slots}
    step = 1 << 24
    for off in range(0, vsize, step):
        blob = read(base + rva + off, min(step, vsize - off) + 8)
        if blob:
            for t, c in count_code_refs(blob, rva + off, slots).items():
                cnt[t] += c
    ranked = sorted(cnt.items(), key=lambda kv: -kv[1])
    if len(ranked) < 2 or ranked[0][1] >= max(1, ratio * max(1, ranked[1][1])):
        return ranked[0][0], cnt
    return None


def most_common_vtable_slot(read, objs: list, idx: int = PROCESS_EVENT_IDX, need: float = 0.9):
    """对象采样：vtable[idx] 里出现次数最多的函数地址；占比 < need ⇒ None。"""
    counts, total = {}, 0
    for o in objs:
        vt = read(o, 8)
        if not vt:
            continue
        vtp = struct.unpack("<Q", vt)[0]
        fn = read(vtp + idx * 8, 8) if _is_user_ptr(vtp) or vtp > 0x140000000 else None
        if not fn:
            continue
        counts[struct.unpack("<Q", fn)[0]] = counts.get(struct.unpack("<Q", fn)[0], 0) + 1
        total += 1
    if not total:
        return None
    fn, n = max(counts.items(), key=lambda kv: kv[1])
    return fn if n / total >= need else None


# ---------------------------------------------------------------- 在线：对一个进程完整扫一遍
def scan_process(m, base: int, image_size: int, log: Callable[[str], None] = lambda s: None) -> dict:
    """`m`：`MemRO`（`read/ptr_or_zero/i32`）；→ `{"GObjects","FNamePool","GWorld","UObject_ProcessEvent"}`（RVA）。
    任何一项找不出或不唯一 ⇒ 抛 `ScanError`（不猜）。"""
    from . import names as N, objects as O
    t0 = time.time()
    read = m.read
    blobs = writable_blobs(read, base)
    log("可写节 %d 个，共 %.1f MB" % (len(blobs), sum(len(b) for _, b in blobs) / 1e6))
    lo, hi = base, base + image_size

    pools = [r for rva, b in blobs for r in find_fnamepool_in_blob(b, rva, read)]
    if len(pools) != 1:
        raise ScanError("FNamePool 候选 %d 个（期望 1）：%s" % (len(pools), [hex(x) for x in pools[:5]]))
    pool_rva = pools[0]
    log("FNamePool = 0x%X" % pool_rva)

    gobjs = [r for rva, b in blobs for r in find_gobjects_in_blob(b, rva, read, lo, hi)]
    if len(gobjs) != 1:
        raise ScanError("GObjects 候选 %d 个（期望 1）：%s" % (len(gobjs), [hex(x) for x in gobjs[:5]]))
    gobj_rva = gobjs[0]
    log("GObjects = 0x%X" % gobj_rva)

    class _S:                                                     # ObjectArray 只要 .m 和 .base
        pass
    s = _S()
    s.m, s.base = m, base
    arr = O.ObjectArray(s, rva=gobj_rva)
    if not arr.sane():
        raise ScanError("GObjects 头不自洽")
    pool = N.FNamePool(m, base, rva=pool_rva)
    pool.probe()
    n = arr.count()
    sample = []
    world_cls, worlds = None, []
    for i in range(n):
        o = arr.at(i)
        if not o:
            continue
        if len(sample) < 400:
            sample.append(o)
        cls = m.ptr_or_zero(o + OFF_UOBJECT_CLASS)
        if not cls:
            continue
        if world_cls is None and pool.fname_of(o) == "World" and pool.fname_of(cls) == "Class":
            world_cls = o                                          # 找到 UClass "World"
    if world_cls is None:
        raise ScanError("GObjects 里没找到类 World")
    for i in range(n):
        o = arr.at(i)
        if o and m.ptr_or_zero(o + OFF_UOBJECT_CLASS) == world_cls and pool.fname_of(o) not in (None, "Default__World"):
            worlds.append(o)
    log("World 实例 %d 个" % len(worlds))
    slots = find_slots_holding(blobs, set(worlds))
    code_refs = None
    if len(slots) > 1:
        picked = pick_by_code_refs(read, base, sections(read, base), slots)
        if picked:
            code_refs = picked[1]
            log("GWorld 多槽 %s，按代码引用数取 0x%X（%s）" % ([hex(x) for x in slots], picked[0],
                                                   {hex(k): v for k, v in picked[1].items()}))
            slots = [picked[0]]
    if len(slots) != 1:
        raise ScanError("GWorld 槽 %d 个（期望 1）：%s" % (len(slots), [hex(x) for x in slots[:5]]))

    pe = most_common_vtable_slot(read, sample)
    if pe is None:
        raise ScanError("ProcessEvent：vtable[0x%X] 采样不一致" % PROCESS_EVENT_IDX)
    out = {"GObjects": gobj_rva, "FNamePool": pool_rva, "GWorld": slots[0], "UObject_ProcessEvent": pe - base}
    log("GWorld = 0x%X  ProcessEvent = 0x%X  用时 %.1fs" % (out["GWorld"], out["UObject_ProcessEvent"], time.time() - t0))
    return out


# ---------------------------------------------------------------- 缓存
def cache_path(version: str, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, "%s.json" % version)


def load_cache(version: str, image_size: int, cache_dir: str = CACHE_DIR) -> Optional[dict]:
    try:
        with open(cache_path(version, cache_dir), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("version") != version or d.get("image_size") != image_size:
        return None
    return d.get("rva")


def save_cache(version: str, image_size: int, rva: dict, cache_dir: str = CACHE_DIR) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_path(version, cache_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": version, "image_size": image_size, "rva": rva, "scanned_at": time.time()}, f, indent=1)
    os.replace(tmp, cache_path(version, cache_dir))


def quick_verify(m, base: int, rva: dict) -> bool:
    """缓存复验（毫秒级）：FNamePool 第 0 条是 None、GObjects 头自洽、GWorld 槽指向 World。任何一项不过 ⇒ False。"""
    try:
        from . import names as N, objects as O
        pool = N.FNamePool(m, base, rva=rva["FNamePool"])
        pool.probe()
        if pool.name_at(0) != "None":
            return False

        class _S:
            pass
        s = _S()
        s.m, s.base = m, base
        if not O.ObjectArray(s, rva=rva["GObjects"]).sane():
            return False
        w = m.ptr_or_zero(base + rva["GWorld"])
        if w:
            cls = m.ptr_or_zero(w + OFF_UOBJECT_CLASS)
            if not cls or pool.fname_of(cls) != "World":
                return False
        return True
    except Exception:                                             # noqa: BLE001
        return False


def resolve(m, base: int, image_size: int, version: Optional[str], log=lambda s: None,
            cache_dir: str = CACHE_DIR, force: bool = False) -> dict:
    """缓存命中且复验通过 ⇒ 直接用；否则扫描并写缓存。→ `{"rva": {...}, "source": "cache"|"scan"}`。"""
    if version and not force:
        c = load_cache(version, image_size, cache_dir)
        if c and quick_verify(m, base, c):
            return {"rva": c, "source": "cache"}
    rva = scan_process(m, base, image_size, log)
    if version:
        save_cache(version, image_size, rva, cache_dir)
    return {"rva": rva, "source": "scan"}
