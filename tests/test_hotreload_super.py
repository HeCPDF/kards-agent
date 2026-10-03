#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热重载后旧实例调用零参 super() 不再报 TypeError（CLAUDE.md 弯路 #39 的根治）。"""
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from player import hotreload as H                                      # noqa: E402

SRC1 = '''
class Base:
    def f(self):
        return "base"

class Child(Base):
    def f(self):
        return "child1+" + super().f()
    @classmethod
    def g(cls):
        return "g1+" + super().__name__ if False else "g1"
'''
SRC2 = SRC1.replace("child1+", "child2+")
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += (not ok)


d = tempfile.mkdtemp()
path = os.path.join(d, "hr_demo_mod.py")
open(path, "w", encoding="utf-8").write(SRC1)
sys.path.insert(0, d)
import hr_demo_mod as M                                               # noqa: E402

inst = M.Child()
chk("重载前", inst.f() == "child1+base")
open(path, "w", encoding="utf-8").write(SRC2)
import time
os.utime(path, (time.time() + 5, time.time() + 5))
importlib.invalidate_caches()
r = H.reload_module("hr_demo_mod")
try:
    out = inst.f()
except TypeError as e:
    out = "TypeError: %s" % e
chk("重载后旧实例调用新方法（含 super()）", out == "child2+base", "%s | %s" % (out, r))
sys.exit(1 if fails else 0)
