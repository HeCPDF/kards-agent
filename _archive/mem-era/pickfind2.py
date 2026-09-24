#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pickfind2.py —— 从内存列出"当前屏幕上摆着的卡"（ABP_BaseCard_C actor）。

ABP_BaseCard_C 类大小 0x0830；关键字段：
  CardID          @0x03C8  (int32)
  selfBaseCardRef @0x0808  (UBaseCardObject*)
候选牌（抉择/预报/发现）就是这些 actor，**内容完全从内存读，不读图**。
"""
import sys

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import board_api as BA  # noqa: E402

SIZE_BASECARD = 0x0830
OFF_CARDID = 0x03C8
OFF_SELFREF = 0x0808


def proplen(m, obj):
    cls = m.ptr(obj + 0x10)
    return m.i32(cls + 0x58) if cls else None


def derives_from_basecard(m, obj, target=SIZE_BASECARD):
    """对象是不是 ABP_BaseCard_C 或其子类（沿 SuperStruct 链找 0x830）。"""
    cls = m.ptr(obj + 0x10)
    seen = 0
    while cls and seen < 40:
        if m.i32(cls + 0x58) == target:
            return True
        cls = m.ptr(cls + 0x40)
        seen += 1
    return False


def main():
    src = BA.open_source("mem")
    src.snapshot()                       # 触发 attach（base 才会被赋上）
    m, base = src._m, src.base
    world = m.ptr(base + BA.RVA_GWORLD)
    lvl = m.ptr(world + 0x30)
    arr = m.ptr(lvl + 0xA0)
    n = m.i32(lvl + 0xA8)
    print("actors=%s" % n)
    rows = []
    for i in range(n):
        a = m.ptr(arr + i * 8)
        if not a:
            continue
        if not derives_from_basecard(m, a):
            continue
        cid = m.i32(a + OFF_CARDID)
        ref = m.ptr(a + OFF_SELFREF)
        nm = BA._read_card_name(m, ref) if ref else None
        rows.append((i, a, cid, nm))
    print("ABP_BaseCard_C(含子类) actors = %d" % len(rows))
    for i, a, cid, nm in rows:
        print("  actor[%3d] 0x%X CardID=%-6s %s" % (i, a, cid, nm))
    # 只看"新出现/异常 id"的
    print("--- id >= 1000（动态生成的候选/令牌）---")
    for i, a, cid, nm in rows:
        if (cid or 0) >= 1000:
            print("  actor[%3d] 0x%X CardID=%-6s %s" % (i, a, cid, nm))


if __name__ == "__main__":
    main()
