#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同名原语的"同实体别名"不许被当成重名（离线）。

2026-10-02 实机（BLUE SKY）：卡片的钩子调 `min(...)`，VM 报
`Unimplemented: 原语名 min 在多个库里重名：['min', 'KismetMathLibrary::min']` ——
因为那批小写别名（abs/min/max/round）**既注册了裸名、又注册了限定名**，
而 `_build_name_index` 把短名的每个 full name 都当一个候选。
修法：短名的所有候选若指向**同一个函数对象** ⇒ 去重成一个。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import kismetlib as KL                            # noqa: E402
from kardsmem import vm as VMOD                                 # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    idx = VMOD._build_name_index()
    for short in ("min", "max", "abs", "round"):
        fulls = idx.get(short) or []
        chk("短名 %-6s 只剩一个候选" % short, len(fulls) == 1, str(fulls))
    # 裸名与限定名确实指向同一函数（这就是"同实体"）
    chk("裸名与限定名同实体",
        KL.PURE.get("min") is KL.PURE.get("KismetMathLibrary::min"))
    # 真的还能调
    chk("min 仍可调用", KL.call("min", [3, 7]) == 3)
    # 真重名（不同函数）不许被误合并：造一个人为冲突验证
    KL.PURE["FakeLibA::dup_test"] = lambda: 1
    KL.PURE["FakeLibB::dup_test"] = lambda: 2
    try:
        idx2 = VMOD._build_name_index()
        chk("不同实体的重名仍保持两个候选", len(idx2.get("dup_test") or []) == 2,
            str(idx2.get("dup_test")))
    finally:
        KL.PURE.pop("FakeLibA::dup_test", None)
        KL.PURE.pop("FakeLibB::dup_test", None)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
