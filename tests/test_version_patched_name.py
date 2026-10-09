# -*- coding: utf-8 -*-
"""`kardsmem.version.exe_names`：原名与私服补丁版 `.patched.exe` 都被接受，别的名字不行。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kardsmem import version as V                                               # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


n = V.exe_names("kards-Win64-Shipping.exe")
chk("原名", "kards-win64-shipping.exe" in n)
chk("补丁版", "kards-win64-shipping.patched.exe" in n)
chk("别的名字不在内", "kards.exe" not in n and "notepad.exe" not in n)
print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
