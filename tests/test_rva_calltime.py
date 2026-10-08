#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P7-S3b 回归：RVA 一律**调用期**读，不许在 import 期烤值（合成内存，不碰真进程）。

背景（`docs/ARCH-RELEASE-VCS.md` §7.2 S3b）：RVA 走"用户缓存 → 种子复验 → 扫描"解析链，
值可能在本进程运行期间才确定/改变。旧实现有三处 import 期快照：
  * `kardsmem.names.RVA_FNAME_POOL` 模块常量 + `FNamePool.__init__` 的默认实参；
  * `ops.consts.JS`（frida 注入脚本按 import 时的地址渲染）。
本测试把 `build.RVA` 改掉后**不重新 import**，断言新读者立刻拿到新值。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import build as B                       # noqa: E402
from kardsmem import names as N                       # noqa: E402
from kardsmem import board as BD                      # noqa: E402
from ops import consts as OC                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class FakeMem:
    """FNamePool 只要求 mem.base 能读出来（本测试不真的读内存）。"""

    base = 0x140000000


def main():
    saved = dict(B.RVA)
    try:
        # --- names：模块属性（PEP 562 __getattr__）跟随 build.RVA -------------------
        chk("names 模块级没有 import 期快照（RVA_FNAME_POOL 不在 vars 里）",
            "RVA_FNAME_POOL" not in vars(N) and "RVA_GNAMES_DECOY" not in vars(N))
        B.RVA["FNamePool"] = 0x111
        chk("names.RVA_FNAME_POOL 调用期读", N.RVA_FNAME_POOL == 0x111, hex(N.RVA_FNAME_POOL))
        B.RVA["FNamePool"] = 0x222
        chk("改 build.RVA 后 names.RVA_FNAME_POOL 立即跟随（无需重 import）", N.RVA_FNAME_POOL == 0x222)
        B.RVA["GNames_decoy"] = 0x333
        chk("names.RVA_GNAMES_DECOY 调用期读", N.RVA_GNAMES_DECOY == 0x333)
        try:
            N.RVA_NOT_EXIST                                     # noqa: B018
            chk("__getattr__ 未知属性抛 AttributeError", False)
        except AttributeError:
            chk("__getattr__ 未知属性抛 AttributeError", True)

        # --- FNamePool.__init__ 的默认实参不许烤死 ---------------------------------
        B.RVA["FNamePool"] = 0x444
        pool = N.FNamePool(FakeMem())
        chk("FNamePool() 默认 rva = 当前 build.RVA（不是 import 期值）", pool.rva == 0x444, hex(pool.rva))
        chk("FNamePool.addr = base + 当前 rva", pool.addr == FakeMem.base + 0x444, hex(pool.addr))
        B.RVA["FNamePool"] = 0x555
        chk("FNamePool() 再构造又跟随新值", N.FNamePool(FakeMem()).rva == 0x555)
        chk("显式 rva 仍然优先（含 0）", N.FNamePool(FakeMem(), rva=0).rva == 0)

        # --- board：旧工具/自检用的 RVA_* 别名同样调用期读 -------------------------
        chk("board 模块级没有 import 期 RVA 快照",
            "RVA_GWORLD" not in vars(BD) and "RVA_GOBJECTS" not in vars(BD))
        B.RVA["GWorld"] = 0x777
        B.RVA["GObjects"] = 0x888
        chk("board.RVA_GWORLD 调用期读", BD.RVA_GWORLD == 0x777, hex(BD.RVA_GWORLD))
        chk("board.RVA_GOBJECTS 调用期读", BD.RVA_GOBJECTS == 0x888)
        B.RVA["GWorld"] = 0x999
        chk("改 build.RVA 后 board.RVA_GWORLD 立即跟随", BD.RVA_GWORLD == 0x999)

        # --- ops.consts.render_js：调用期渲染 -------------------------------------
        chk("ops.consts 没有 import 期 JS 常量", not hasattr(OC, "JS"))
        B.RVA["UObject_ProcessEvent"] = 0x0A0A
        B.RVA["GObjects"] = 0x0B0B
        B.RVA["FNamePool"] = 0x0C0C
        s1 = OC.render_js()
        # 模板里写法是 `ptr(%(pe_rva)d)` —— 十进制（agent.js.tpl:19/76/77），按十进制断言
        chk("render_js() 用当前 RVA 渲染（十进制出现在脚本里）",
            all(str(v) in s1 for v in (0x0A0A, 0x0B0B, 0x0C0C)))
        chk("render_js() 无残留占位符 / 有内容", "%(" not in s1 and len(s1) > 10000)
        B.RVA["FNamePool"] = 0x0D0D
        s2 = OC.render_js()
        chk("改 RVA 后再渲染 ⇒ 输出随之变化（证明不是快照）", s1 != s2 and str(0x0D0D) in s2)
    finally:
        B.RVA.clear()
        B.RVA.update(saved)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(main())
