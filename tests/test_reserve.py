#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`kardsmem.reserve.is_reserved`：游戏 `isCardReserved` 的逐行复刻（纯函数，离线）。

判据来自游戏自己的 BP（`Library/UtilityFunctions.cpp:6394`）：变更表里"已生效且更晚"的那条决定；没有命中则用静态卡的默认 `isReserved`。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from kardsmem import reserve as RS                                   # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


def ch(t, to=(), back=()):
    return {"activation": t, "to_be_reserved": set(to), "coming_back": set(back)}


def main():
    NOW = 1000
    chk("没有变更：用静态卡默认值（两种取值各一）", RS.is_reserved("a", True, [], NOW) is True and RS.is_reserved("a", False, [], NOW) is False)
    chk("变更里不含这张牌：仍用静态默认值", RS.is_reserved("a", False, [ch(10, to={"b"})], NOW) is False)
    chk("已生效的 to_be_reserved ⇒ 进预备（盖过静态默认 False）", RS.is_reserved("a", False, [ch(10, to={"a"})], NOW) is True)
    chk("已生效的 coming_back ⇒ 回池（盖过静态默认 True）", RS.is_reserved("a", True, [ch(10, back={"a"})], NOW) is False)
    chk("还没到生效时间的变更不算", RS.is_reserved("a", False, [ch(5000, to={"a"})], NOW) is False)
    chk("先进预备、后回池：晚的那条赢", RS.is_reserved("a", False, [ch(10, to={"a"}), ch(20, back={"a"})], NOW) is False)
    chk("先回池、后进预备：晚的那条赢", RS.is_reserved("a", False, [ch(10, back={"a"}), ch(20, to={"a"})], NOW) is True)
    chk("后一条尚未生效：前一条仍然有效", RS.is_reserved("a", False, [ch(10, to={"a"}), ch(5000, back={"a"})], NOW) is True)
    # 数组顺序是游戏的迭代顺序：`activation >= latest` 才覆盖；乱序时较早的那条排在后面不能覆盖较晚的
    chk("数组里较早的一条排在后面，不覆盖较晚的", RS.is_reserved("a", False, [ch(20, to={"a"}), ch(10, back={"a"})], NOW) is True)
    t = RS.utc_ticks(0)
    chk("utc_ticks：1970-01-01 = 621355968000000000", t == 621355968000000000 and RS.utc_ticks(1) - t == 10_000_000)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
