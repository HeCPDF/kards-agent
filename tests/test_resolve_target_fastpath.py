# -*- coding: utf-8 -*-
"""`QueryMixin.resolve_target`：数字规格直接返回 card_id，**不取全量快照**（约 0.35 s 的白读，攻击执行里的冗余）；
位置规格（hq / front / back / guard）仍然要读盘面。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from kardsmem import board as BA                                    # noqa: E402
from ops.query import QueryMixin                                    # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


calls = []


def fake_open(kind="mem"):
    calls.append(kind)
    raise RuntimeError("读盘面不该发生")


real = BA.open_source
BA.open_source = fake_open
try:
    q = QueryMixin()
    for spec in (12, "12", " 7 ", "-3"):
        r = q.resolve_target(spec)
        chk("数字规格 %r ⇒ 原样返回 card_id，且没有读盘面" % (spec,),
            r == {"ok": True, "card_id": int(spec), "kind": "id", "side": None} and not calls)
    try:
        q.resolve_target("hq")
        chk("位置规格 hq 仍然要读盘面", False)
    except RuntimeError:
        chk("位置规格 hq 仍然要读盘面", calls == ["mem"])
finally:
    BA.open_source = real
print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
