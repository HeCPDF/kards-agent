#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""field.py —— 用 OCR 后端的 read_field 拿盘面卡框坐标，并与内存槽位配对。

打印：每行（our_support / frontline / enemy_support）的单位框 cx,cy 与内存 side/locationNumber 的对应。
"""
import os
import sys

import _bootstrap  # noqa: E402,F401  —— 接上 kards-agent/ 与 vendor/

import cv2  # noqa: E402
import win  # noqa: E402
B = _bootstrap.upstream_board()  # noqa: E402  —— 上游 OCR 读盘面（**只作核对**）
import board_api as BA  # noqa: E402

win.set_dpi_aware()


def hwnd():
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    ws = win.find_by_process("kards")
    return ws[0]["hwnd"] if ws else None


def main():
    h = hwnd()
    f = win.capture_client_bgr(h, allow_screen_fallback=True)
    if len(sys.argv) > 1:
        cv2.imwrite(sys.argv[1], f)
    field = B.read_field(f, debug=False)
    print("== OCR rows ==")
    for i, r in enumerate(field.get("rows") or []):
        us = r.get("units") or []
        print("  row%d cy=%.0f side=%-9s n=%d  %s" % (
            i, r.get("cy", -1), r.get("side"), len(us),
            " ".join("cx=%.0f" % u["cx"] for u in us)))
    for key in ("our_support", "enemy_support", "frontline"):
        us = field.get(key) or []
        print("  %-14s n=%d  %s" % (key, len(us), " ".join("(%.0f,%.0f)" % (u["cx"], u["cy"]) for u in us)))
    for key in ("hq_our", "hq_enemy"):
        u = field.get(key)
        print("  %-14s %s" % (key, ("(%d,%d,w=%d,h=%d)" % (u["cx"], u["cy"], u["w"], u["h"])) if u else None))

    st = BA.open_source("mem").snapshot()
    print("== mem ==")
    for side in ("local", "enemy"):
        for c in sorted([x for x in st.field_units(side) if x.card_type != "location"],
                        key=lambda c: ((c.location or ""), (c.slot if c.slot is not None else 99))):
            print("  %-5s %-9s slot=%-2s id=%-5s %s %s/%s" % (
                side, c.location, c.slot, c.card_id, (c.name or "?")[:18], c.attack, c.defense))


if __name__ == "__main__":
    main()
