#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`sim/dispatch.py` 事件队列的行为测试（"连锁到稳态"的发动机）。

钉住的语义（`EVAL-ARCHITECTURE.md` §3.3 + §7「已查」）：
  * 队列跑到空 = 稳态；`trace` 记得下每一步是谁处理的；
  * `mode="dfs"` 新事件排队首（= 同步递归）、`"bfs"` 排尾（= 现有抽牌链的旧语义）；
  * `Result.stop=True` = `SetStopFurtherActions`：**中止当前事件剩下的 handler**；
  * 没有 handler 的事件 ⇒ `complete=False` + 缺口，**不猜**；
  * 步数/深度上限 ⇒ `complete=False` + 缺口，不假装算完。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sim.dispatch import Dispatcher, Event, Result             # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    # ---- 顺序：dfs 新事件优先 / bfs 排队尾 ----
    for mode, want in (("dfs", ["a", "c", "b"]), ("bfs", ["a", "b", "c"])):
        d = Dispatcher(mode=mode)
        seen = []

        @d.on("a")
        def _a(ev, ctx, _seen=seen):
            _seen.append("a")
            return Result(events=[ev.child("c")])

        @d.on("b")
        def _b(ev, ctx, _seen=seen):
            _seen.append("b")

        @d.on("c")
        def _c(ev, ctx, _seen=seen):
            _seen.append("c")

        r = d.run([Event("a"), Event("b")])
        chk("%s：处理顺序 %s" % (mode, want), seen == want, str(seen))
        chk("%s：跑完 = complete" % mode, r.complete and r.steps == 3, str(r.steps))

    # ---- stop：中止**当前事件**剩下的 handler（不是清空整个队列）----
    d = Dispatcher()
    log = []

    @d.on("x")
    def _x1(ev, ctx, _log=log):
        _log.append("x1")
        return Result(stop=True)

    @d.on("x")
    def _x2(ev, ctx, _log=log):
        _log.append("x2")

    @d.on("y")
    def _y(ev, ctx, _log=log):
        _log.append("y")

    r = d.run([Event("x"), Event("y")])
    chk("stop 中止本事件后续 handler（x2 不跑）", log == ["x1", "y"], str(log))
    chk("stop 记进 Run.stopped", r.stopped == [{"kind": "x", "handler": "_x1"}], str(r.stopped))
    chk("stop 不算缺口（是原版规则行为）", r.complete and not r.gaps, str(r.gaps))

    # ---- 没 handler：记缺口、不猜 ----
    r = Dispatcher().run(Event("unknown_kind"))
    chk("缺 handler ⇒ complete=False + 缺口", (not r.complete) and any("handler" in g for g in r.gaps),
        str(r.gaps))

    # ---- 上限：步数 ----
    d = Dispatcher(max_steps=3)

    @d.on("loop")
    def _loop(ev, ctx):
        return Result(events=[ev.child("loop")])

    r = d.run(Event("loop"))
    chk("步数上限 ⇒ 截断 + 缺口（不死循环）",
        (not r.complete) and r.steps == 3 and any("步数上限" in g for g in r.gaps), str(r.steps))

    # ---- 上限：深度 ----
    d = Dispatcher(max_depth=2)

    @d.on("deep")
    def _deep(ev, ctx):
        return Result(events=[ev.child("deep")])

    r = d.run(Event("deep"))
    chk("深度上限 ⇒ 跳过 + 缺口", (not r.complete) and any("深度" in g for g in r.gaps), str(r.gaps))

    # ---- handler 返回值容错：None / Event / list ----
    d = Dispatcher()

    @d.on("n")
    def _n(ev, ctx):
        return None

    @d.on("e")
    def _e(ev, ctx):
        return Result(events=[Event("n")])

    r = d.run([Event("e"), Event("n")])
    # bfs：种子 n 先跑，e 产生的子事件 n 排到队尾 ⇒ 3 步
    chk("handler 返回 None / 事件列表都能跑（BFS：种子 n 先于子事件）",
        r.complete and r.steps == 3 and r.trace[0]["kind"] == "e", str(r.steps))

    # ---- 换数据答案要变（弯路 #11）：同一个 kind 注册两个 handler，顺序跟着注册变 ----
    def make(order):
        d = Dispatcher()
        seq = []
        for i in order:
            def _h(ev, ctx, _i=i, _seq=seq):
                _seq.append(_i)
            d.on("k")(_h)
        return d, seq

    _d1, seq1 = make([1, 2])
    _d1.run(Event("k"))
    _d2, seq2 = make([2, 1])
    _d2.run(Event("k"))
    chk("注册顺序变了结果就变（不是固定集合）", seq1 == [1, 2] and seq2 == [2, 1],
        "%s / %s" % (seq1, seq2))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
