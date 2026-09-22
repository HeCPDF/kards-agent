#!/usr/bin/env python3
"""轮询等待轮到自己（或状态变化时打印一行）。

用法: wait_turn.py [ours|any] [cap_seconds]
"""
import sys
import time

sys.path.insert(0, r"D:\Kards\OCR-Kards-Auto\src")
from board_api import LOCAL, MemoryBoardSource  # noqa: E402

want = sys.argv[1] if len(sys.argv) > 1 else "ours"
cap = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

src = MemoryBoardSource()
t0 = time.time()
last = None
try:
    while time.time() - t0 < cap:
        st = src.snapshot()
        key = (st.turn, st.our_turn, st.kredits[LOCAL], st.kredits["enemy"],
               len(st.field_units(LOCAL)), len(st.field_units("enemy")),
               st.hq[LOCAL].defense, st.hq["enemy"].defense)
        if key != last:
            print("%5.1fs turn=%-3s our_turn=%-5s kredits=%s/%-3s field=%d/%d hq=%s/%s" % (
                time.time() - t0, key[0], key[1], key[2], key[3], key[4], key[5], key[6], key[7]))
            sys.stdout.flush()
            last = key
        if want == "ours" and st.our_turn:
            print("=> 轮到我方（turn=%s, kredits=%s, slot=%s）" % (
                st.turn, st.kredits[LOCAL], st.slots[LOCAL]))
            break
        time.sleep(2.0)
finally:
    src.close()
