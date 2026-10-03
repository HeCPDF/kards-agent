# -*- coding: utf-8 -*-
"""实机：对手牌逐张 VM 空跑（不带目标），打印效果摘要与覆盖情况。

原来是 `python -m semantics.effectvm --hand`；它要拿 `AgentSession`（agent 层），而 semantics 不许反向依赖 agent，
所以挪到 tools/。用法：`python tools/effects_live_hand.py`（游戏在对局里；只读）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 仓库根

from semantics.effectvm import enumerate_effects                                      # noqa: E402


def _live_hand() -> int:
    """实机：对手牌逐张 VM 空跑（不带目标），打印效果摘要与覆盖情况。

    这是「覆盖缺口清单」的来源：`complete=False` 的牌就是 VM 还没覆盖到的原语/分支，
    要么补 VM，要么补钩子词汇（用户：效果未知的牌不该存在）。
    需要游戏在对局里；只读，不注入、不写。
    """
    from agent import session as _session
    sess = _session.AgentSession(translate=False, warm=False)
    km, st = sess._kardsmem(), sess.snapshot()
    side = getattr(st, "my_side_raw", None)
    for c in st.hand("local"):
        ptr = (getattr(c, "raw", None) or {}).get("ptr")
        if not ptr:
            print("%-26s (没有对象指针)" % c.name)
            continue
        r = enumerate_effects(km, ptr, 0, False, hook=None, my_side=side)
        outs = r["outcomes"]
        print("%-26s complete=%-5s runs=%d 分支=%d stopped=%s" % (
            c.name, r["complete"], r["runs"], len(outs), (r["stopped"] or "-")[:70]))
        for w, e in outs[:4]:
            print("      %.2f  %s" % (w, {k: v for k, v in e.items() if k != "uncertain"}))
    return 0


if __name__ == "__main__":
    sys.exit(_live_hand())
