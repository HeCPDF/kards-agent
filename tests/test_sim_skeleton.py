#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骨架的特征测试（"迁移前后行为不变" + 诚实缺口）。

对应 `EVAL-ARCHITECTURE.md` §1.1 补充 0/4：包边界建立时**行为一字不改**，
所以这里逐条钉住：`sim.simulate` / `evaluation.value` / `evaluation.delta`
与 `sim.engine` 的动作转移（`apply` / `delta`）**同输入同输出**；没接上的东西
（连锁到稳态、同回合计价）必须**明确报出来**，不许静默。

★ P5（2026-10-04，删 `policy.boardeval` 门面）：原来这里比的另一半是**门面转发**的
`policy.boardeval.*`；门面删掉后，比的直接是**真家**（`engine.state` / `sim.engine` /
`evaluation.value`），断言的不变式一条没少（"同一个对象/同一个答案"照旧钉）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                              # noqa: E402
import evaluation                                              # noqa: E402
import sim                                                     # noqa: E402
from engine.state import H, Sim, U                              # noqa: E402
from evaluation.value import W, delta, evaluate                 # noqa: E402
from sim.engine import A, apply                                 # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), hand=(), kred=5.0, hq=(20, 20), **kw):
    return sim.State({u.id: u for u in units}, {ME: hq[0], OPP: hq[1]}, kred,
                     {c.id: c for c in hand}, **kw, my_side=ME)


def main():
    st = mk([U(1, ME, "back", 4, 3, 3, "artillery"),
             U(5, OPP, "frontline", 3, 3, 3, "infantry")])
    chk("State 可构造、copy() 返回 State",
        isinstance(st, sim.State) and isinstance(st.copy(), sim.State))

    # ---- 行为不变（骨架 = 真家实现 + 壳转调）----
    chk("evaluation.value(st) == evaluate(st)（公开入口与真实现同一答案；门面已删 ⇒ 现在钉的就是这两者）",
        evaluation.value(st) == evaluate(st), "%.4f" % evaluation.value(st))
    chk("evaluation.DEFAULT_WEIGHTS 就是 evaluation.value.W（单一权重表，没人偷偷复制一份）",
        evaluation.DEFAULT_WEIGHTS is W)

    a = A("attack", 1, 5)
    end = sim.simulate(st, a).state_end
    ref = apply(st, a)
    same = (end.hq == ref.hq and end.kredits == ref.kredits
            and sorted((k, u.atk, u.dfn) for k, u in end.units.items())
            == sorted((k, u.atk, u.dfn) for k, u in ref.units.items()))
    chk("sim.simulate(单步) 的终态 == sim.engine.apply", same)
    chk("evaluation.delta(start,end) == evaluation.value.delta",
        abs(evaluation.delta(st, end) - delta(st, ref)) < 1e-9)

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
    st2 = mk([U(1, ME, "back", 4, 3, 3, "artillery"),
              U(5, OPP, "frontline", 3, 3, 3, "infantry")], hq=(20, 4))
    d1 = evaluation.delta(st, apply(st, A("attack", 1, 5)))
    d2 = evaluation.delta(st, apply(st, A("attack", 1, "hq")))
    chk("换动作答案跟着变（打单位 vs 打总部）", abs(d1 - d2) > 1e-6,
        "%.3f vs %.3f" % (d1, d2))

    # ---- 深拷贝语义（搜索要靠它）----
    c = st.copy()
    c.units[5].dfn = 1
    chk("copy() 是深拷贝（改副本不动原状态）", st.units[5].dfn == 3)

    # ---- 卡牌模板：H 从 sim 里也能拿到（L2 数据结构）；壳不许另留一份实现 ----
    chk("sim.state 的 H 就是 engine.state.H（P2 兼容壳只转调，不另留实现）", sim.H is H)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
