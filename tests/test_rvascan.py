#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem/rvascan.py 的离线测试：合成 .data blob + 合成进程内存，覆盖各"形状"判据与缓存。"""
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import rvascan as R                                # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class Mem:
    """稀疏合成内存：{起始地址: bytes}。"""

    def __init__(self):
        self.regs = {}

    def put(self, addr, data):
        self.regs[addr] = bytes(data)

    def read(self, addr, n):
        for a, d in self.regs.items():
            if a <= addr and addr + n <= a + len(d):
                return d[addr - a: addr - a + n]
        return None


def main():
    BASE = 0x7FF700000000
    HI = BASE + 0x9000000
    # ---- FNamePool：.data 里 P+0x10 的槽指向 64KiB 对齐的块，块首条目是 None ----
    mem = Mem()
    block0 = 0x21E00000000
    entry = struct.pack("<H", 4 << 6) + b"None" + b"\0\0"        # len=4、非宽
    mem.put(block0, entry + b"\0" * 64)
    blob = bytearray(0x400)
    P = 0x100
    struct.pack_into("<I", blob, P + R.POOL_CURBLOCK_OFF, 3)
    struct.pack_into("<I", blob, P + R.POOL_CURSOR_OFF, 0x1234)
    struct.pack_into("<Q", blob, P + R.POOL_BLOCKS_OFF, block0)
    # 干扰：同样是 64K 对齐指针，但块里不是 None
    other = 0x21E10000000
    mem.put(other, struct.pack("<H", 5 << 6) + b"Hello" + b"\0")
    struct.pack_into("<I", blob, 0x200 + R.POOL_CURBLOCK_OFF, 1)
    struct.pack_into("<Q", blob, 0x200 + R.POOL_BLOCKS_OFF, other)
    got = R.find_fnamepool_in_blob(bytes(blob), 0x5000, mem.read)
    chk("FNamePool：只认第 0 条是 None 的那个", got == [0x5000 + P], str([hex(x) for x in got]))
    # 宽字符位不能置
    mem.put(block0, struct.pack("<H", (4 << 6) | 1) + b"None")
    chk("FNamePool：宽字符头不算", R.find_fnamepool_in_blob(bytes(blob), 0x5000, mem.read) == [])

    # ---- GObjects：头自洽 + 前 16 个对象 vtable 在模块内 ----
    mem = Mem()
    chunks, chunk0 = 0x21F00000000, 0x21F10000000
    mem.put(chunks, struct.pack("<Q", chunk0))
    items = b""
    for i in range(16):
        obj = 0x22000000000 + i * 0x100
        items += struct.pack("<Q", obj) + b"\0" * 16
        mem.put(obj, struct.pack("<QQQQ", BASE + 0x7000000 + i * 8, 0, 0x22100000000, 0))
    mem.put(chunk0, items)
    blob = bytearray(0x400)
    G = 0x40
    num = 150000
    struct.pack_into("<Q", blob, G, chunks)
    struct.pack_into("<iiii", blob, G + 0x10, 0x210000, num, 33, (num + 0xFFFF) // 0x10000)   # MaxChunks=33 在 6..0x5FF 内
    got = R.find_gobjects_in_blob(bytes(blob), 0x6000, mem.read, BASE, HI)
    chk("GObjects：自洽头 + 像样对象 ⇒ 命中", got == [0x6000 + G], str(got))
    bad = bytearray(blob)
    struct.pack_into("<iiii", bad, G + 0x10, 0x210000, num, 33, 1)         # NumChunks 与 Num 不匹配
    chk("GObjects：块数对不上 ⇒ 不命中", R.find_gobjects_in_blob(bytes(bad), 0x6000, mem.read, BASE, HI) == [])
    mem2 = Mem()
    mem2.put(chunks, struct.pack("<Q", chunk0))
    mem2.put(chunk0, items)                                                  # 对象本体读不到
    chk("GObjects：对象读不到 ⇒ 不命中", R.find_gobjects_in_blob(bytes(blob), 0x6000, mem2.read, BASE, HI) == [])

    # ---- 槽查找 ----
    blob = bytearray(0x100)
    struct.pack_into("<Q", blob, 0x38, 0xABCDEF000)
    chk("find_slots_holding", R.find_slots_holding([(0x9000, bytes(blob))], {0xABCDEF000}) == [0x9038])

    # ---- 多槽按代码引用取舍（mov rax,[rip+disp32]：48 8B 05 disp32） ----
    text = bytearray(0x400)
    def _mov(at, target_rva, text_rva=0x1000):
        text[at:at + 3] = bytes([0x48, 0x8B, 0x05])
        struct.pack_into("<i", text, at + 3, target_rva - (text_rva + at + 7))
    for i in range(8):
        _mov(0x10 * i, 0x9389ED8)
    _mov(0x200, 0x8F5F5B0)
    cr = R.count_code_refs(bytes(text), 0x1000, [0x9389ED8, 0x8F5F5B0])
    chk("count_code_refs：8 次 vs 1 次", cr == {0x9389ED8: 8, 0x8F5F5B0: 1}, str(cr))
    secs = [(".text", 0x1000, 0x400, 0)]
    rd = lambda a, n: bytes(text[a - 0x400000 - 0x1000:a - 0x400000 - 0x1000 + n]) if a - 0x400000 - 0x1000 >= 0 else None
    pk = R.pick_by_code_refs(rd, 0x400000, secs, [0x8F5F5B0, 0x9389ED8])
    chk("pick_by_code_refs：取引用多的槽", pk and pk[0] == 0x9389ED8)
    text2 = bytearray(text); 
    chk("pick_by_code_refs：势均力敌 ⇒ None（不猜）",
        R.pick_by_code_refs(lambda a, n: bytes(8 * 0 + n), 0x400000, secs, [1, 2]) is None)

    # ---- ProcessEvent：vtable 采样 ----
    mem = Mem()
    objs = []
    for k in range(10):
        o = 0x23000000000 + k * 0x100
        vt = 0x24000000000 + (k % 2) * 0x1000                                # 两张 vtable
        mem.put(o, struct.pack("<Q", vt))
        slots = bytearray(0x4D * 8 + 8)
        struct.pack_into("<Q", slots, R.PROCESS_EVENT_IDX * 8, BASE + 0x159CAB0)   # 都指向同一个 PE
        mem.put(vt, bytes(slots))
        objs.append(o)
    chk("ProcessEvent：采样一致 ⇒ 取该函数", R.most_common_vtable_slot(mem.read, objs) == BASE + 0x159CAB0)
    vt2 = 0x24000000000
    s2 = bytearray(0x4D * 8 + 8)
    struct.pack_into("<Q", s2, R.PROCESS_EVENT_IDX * 8, BASE + 0x111)
    mem.put(vt2, bytes(s2))                                                  # 一半对象改指别处
    chk("ProcessEvent：采样不一致(占比<0.9) ⇒ None", R.most_common_vtable_slot(mem.read, objs) is None)

    # ---- 缓存 ----
    d = tempfile.mkdtemp(prefix="rvacache_")
    rva = {"GObjects": 1, "FNamePool": 2, "GWorld": 3, "UObject_ProcessEvent": 4}
    R.save_cache("1.60.27292.launcher", 0x9CC4000, rva, d)
    chk("缓存：同版本同镜像大小命中", R.load_cache("1.60.27292.launcher", 0x9CC4000, d) == rva)
    chk("缓存：镜像大小不同 ⇒ 失效", R.load_cache("1.60.27292.launcher", 0x9CC8000, d) is None)
    chk("缓存：别的版本 ⇒ 没有", R.load_cache("1.60.27292.Steam", 0x9CC4000, d) is None)

    # ---- PE 节解析 ----
    pe = bytearray(0x1000)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 0x80)
    pe[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", pe, 0x80 + 6, 2)
    struct.pack_into("<H", pe, 0x80 + 20, 0xF0)
    for i, (nm, rva_, vs, ch) in enumerate((("text", 0x1000, 0x100, 0x60000020), ("data", 0x2000, 0x40, 0xC0000040))):
        o = 0x80 + 24 + 0xF0 + i * 40
        pe[o:o + 8] = nm.encode().ljust(8, b"\0")
        struct.pack_into("<IIII", pe, o + 8, vs, rva_, 0, 0)
        struct.pack_into("<I", pe, o + 36, ch)
    m = Mem()
    m.put(BASE, bytes(pe))
    m.put(BASE + 0x2000, b"\x11" * 0x40)
    secs = R.sections(m.read, BASE)
    chk("sections：解析出 2 个节", [s[0] for s in secs] == ["text", "data"], str(secs))
    blobs = R.writable_blobs(m.read, BASE, secs)
    chk("writable_blobs：只取可写非代码节（.data）", len(blobs) == 1 and blobs[0][0] == 0x2000 and blobs[0][1] == b"\x11" * 0x40)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
