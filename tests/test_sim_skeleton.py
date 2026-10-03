#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骨架的特征测试（"迁移前后行为不变" + 诚实缺口）。

对应 `EVAL-ARCHITECTURE.md` §1.1 补充 0/4：包边界建立时**行为一字不改**，
所以这里逐条钉住：`sim.simulate` / `evaluation.value` / `evaluation.delta`
与 `policy.boardeval` 的旧接口**同输入同输出**；没接上的东西（连锁到稳态、
同回合计价）必须**明确报出来**，不许静默。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
import evaluation                                              # noqa: E402
import sim                                                     # noqa: E402
from policy.boardeval import A, H, U                            # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), hand=(), kred=5.0, hq=(20, 20), **kw):
    return sim.State({u.id: u for u in units}, {"local": hq[0], "enemy": hq[1]}, kred,
                     {c.id: c for c in hand}, **kw)


def main():
    st = mk([U(1, "local", "back", 4, 3, 3, "artillery"),
             U(5, "enemy", "frontline", 3, 3, 3, "infantry")])
    chk("State 可构造、copy() 返回 State",
        isinstance(st, sim.State) and isinstance(st.copy(), sim.State))

    # ---- 行为不变（骨架 = 原样保留 boardeval + 转调）----
    chk("evaluation.value(st) == boardeval.evaluate(st)",
        evaluation.value(st) == B.evaluate(st), "%.4f" % evaluation.value(st))
    chk("evaluation.DEFAULT_WEIGHTS is boardeval.W",
        evaluation.DEFAULT_WEIGHTS is B.W)

    a = A("attack", 1, 5)
    end = sim.simulate(st, a).state_end
    ref = B.apply(st, a)
    same = (end.hq == ref.hq and end.kredits == ref.kredits
            and sorted((k, u.atk, u.dfn) for k, u in end.units.items())
            == sorted((k, u.atk, u.dfn) for k, u in ref.units.items()))
    chk("sim.simulate(单步) 的终态 == boardeval.apply", same)
    chk("evaluation.delta(start,end) == boardeval.delta",
        abs(evaluation.delta(st, end) - B.delta(st, ref)) < 1e-9)

    # ---- 诚实：没接的东西要报出来 ----
    out = sim.simulate(st, a)
    chk("骨架期 simulate 如实 complete=False", out.complete is False)
    chk("骨架期缺口里写明'事件队列/分发器未接'",
        any("事件队列" in g for g in out.gaps), str(out.gaps))
    try:
        evaluation.delta(st, end, ctx=evaluation.Context())
        chk("传 ctx（同回合计价未接线）必须明确报错", False, "静默通过了")
    except NotImplementedError as e:
        chk("传 ctx（同回合计价未接线）必须明确报错", True, str(e)[:24] + "…")

    # ---- 换数据答案要变（弯路 #11）----
    st2 = mk([U(1, "local", "back", 4, 3, 3, "artillery"),
              U(5, "enemy", "frontline", 3, 3, 3, "infantry")], hq=(20, 4))
    d1 = evaluation.delta(st, B.apply(st, A("attack", 1, 5)))
    d2 = evaluation.delta(st, B.apply(st, A("attack", 1, "hq")))
    chk("换动作答案跟着变（打单位 vs 打总部）", abs(d1 - d2) > 1e-6,
        "%.3f vs %.3f" % (d1, d2))

    # ---- 深拷贝语义（搜索要靠它）----
    c = st.copy()
    c.units[5].dfn = 1
    chk("copy() 是深拷贝（改副本不动原状态）", st.units[5].dfn == 3)

    # ---- 卡牌模板：H 从 sim 里也能拿到（L2 数据结构）----
    chk("sim.H 就是 boardeval.H", sim.H is H)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
