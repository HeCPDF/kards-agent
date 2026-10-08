#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.dispatch —— 事件队列 + 分发器（"连锁到稳态"的发动机）。

P2（2026-10-03）：从 `sim/dispatch.py` 原样端口；`sim.dispatch` 只是 re-export 壳。
缺口头用自己的前缀 `engine.dispatch：`（测试只匹配子串，日志更如实）。

契约（`EVAL-ARCHITECTURE.md` §3.3）：
    动作 → 事件入队 → 按原版顺序跑钩子/效果 → 产生的新事件再入队 → 队列空 = **稳态**。

顺序与互斥（§7「已查」部分，必须按此实现）：
  * **反制先手**：`GetActiveGotchasOrdered` —— `IsGotcha ∧ gotchaActivated>0`，key =
    `card_event_ultra` = −(cardID+1000000)、`card_event_interception` = −cardID、
    其余 = `gotchaActivated`，撞车用备用 key 100/101…，**升序**。
    这份排序**已经**实现在 `engine/triggers.py::ordered_gotchas`（单一规则来源），
    本模块**不复制**它：分发器只按 L1 给的顺序把清单跑完。
  * **`SetStopFurtherActions`**：某张反制触发后置位 ⇒ **中止该动作剩余的所有钩子**。
    用 `Result(stop=True)` 表达：分发器停掉当前事件剩下的 handler，并记进 `Run.stopped`
    —— 这是原版规则行为，**不是**缺口。
  * §7「未查」清单（叫停语义的精确位置、多张反制互相影响、触发表只加不删…）⇒ 记缺口，不猜。

队列顺序（`mode`）：
  * `"dfs"`（默认，栈）：新事件**排到队首**先处理 —— 等价于现有代码的"钩子内部同步递归"
    （如抽牌链：抽到 → 抽到时效果 → 它的再抽，先跑完再回到外层队列）。
    **默认必须是 dfs**：改成 FIFO 会让牌库顺序敏感的链（抵抗那类连抽）算出不同结果，
    而"模拟按原版"要求顺序一致（改顺序得有 IDA 证据）。
  * `"bfs"`（FIFO）：留给"已确认原版是广度优先"的链。

上限：`max_steps`（总步数，防死循环）与 `max_depth`（单事件深度）。触顶 ⇒
`Run.complete=False` + 缺口，**不**假装算完。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

DEFAULT_MAX_STEPS = 1000
DEFAULT_MAX_DEPTH = 64


@dataclass
class Event:
    """一个待处理的事件（`kind` = "draw" / "deploy" / "destroyed" / …）。"""

    kind: str
    args: dict = field(default_factory=dict)
    depth: int = 0
    origin: str = "seed"          # 谁产生的（诊断/留痕；种子事件是 "seed"）

    def child(self, kind: str, **over) -> "Event":
        """本事件的子事件：**继承 args**（同一个盘面/上下文），depth+1。"""
        args = dict(self.args)
        args.update(over)
        return Event(kind, args, self.depth + 1, origin="%s" % self.kind)


@dataclass
class Result:
    """handler 的返回值：新事件 + 是否叫停 + 缺口。"""

    events: list = field(default_factory=list)
    stop: bool = False
    gaps: list = field(default_factory=list)
    note: str = ""


@dataclass
class Run:
    """一次队列驱动的结果（`trace` 供调试/留痕）。"""

    steps: int = 0
    trace: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    complete: bool = True
    stopped: list = field(default_factory=list)


def _norm(r) -> Result:
    if r is None:
        return Result()
    if isinstance(r, Result):
        return r
    if isinstance(r, (list, tuple)):
        return Result(events=list(r))
    if isinstance(r, Event):
        return Result(events=[r])
    raise TypeError("handler 只能返回 None / Result / [Event] / Event，收到 %r" % (type(r),))


class Dispatcher:
    """`kind → [handler…]`，`run()` 把队列推到空（稳态）。

    同一个 kind 有多个 handler 时**按注册顺序**跑；顺序本身（谁先谁后）属于 L1 的
    证据（`ordered_gotchas` / 触发表元素序），不在这里重排。
    """

    def __init__(self, handlers=None, *, mode: str = "dfs",
                 max_steps: int = DEFAULT_MAX_STEPS, max_depth: int = DEFAULT_MAX_DEPTH):
        if mode not in ("dfs", "bfs"):
            raise ValueError("mode 只能是 dfs / bfs，收到 %r" % (mode,))
        self.mode = mode
        self.max_steps = int(max_steps)
        self.max_depth = int(max_depth)
        self.handlers: dict = {}
        for kind, fns in (handlers or {}).items():
            for fn in (fns if isinstance(fns, (list, tuple)) else [fns]):
                self.on(kind)(fn)

    def on(self, kind: str):
        """注册 handler（可当装饰器用）。同一 kind 多次注册 = 按注册顺序依次跑。"""
        def deco(fn):
            self.handlers.setdefault(kind, []).append(fn)
            return fn
        return deco

    def names(self, kind: str) -> list:
        return [f.__name__ for f in self.handlers.get(kind, ())]

    def run(self, events, ctx=None) -> Run:
        if isinstance(events, Event):
            q = deque([events])
        elif isinstance(events, (list, tuple, deque)):
            q = deque(events)
        else:
            raise TypeError("events 只能是 Event 或 Event 序列")
        run = Run()
        while q:
            if run.steps >= self.max_steps:
                run.complete = False
                run.gaps.append("engine.dispatch：步数上限 %d 触顶（疑似死循环或超长链），"
                                "剩余 %d 个事件没跑" % (self.max_steps, len(q)))
                break
            ev = q.popleft()
            if ev.depth > self.max_depth:
                run.complete = False
                run.gaps.append("engine.dispatch：事件 %s 深度超过 %d，跳过" % (ev.kind, self.max_depth))
                continue
            fns = self.handlers.get(ev.kind)
            if not fns:
                run.complete = False
                run.gaps.append("engine.dispatch：没有处理 %s 的 handler（事件被丢弃，不猜结果）" % ev.kind)
                continue
            for fn in fns:
                run.steps += 1
                r = _norm(fn(ev, ctx))
                run.trace.append({"kind": ev.kind, "depth": ev.depth, "handler": fn.__name__,
                                  "events": [e.kind for e in r.events], "stop": bool(r.stop)})
                run.gaps += list(r.gaps)
                if r.events:
                    if self.mode == "dfs":
                        q.extendleft(reversed(r.events))
                    else:
                        q.extend(r.events)
                if r.stop:
                    run.stopped.append({"kind": ev.kind, "handler": fn.__name__})
                    break
        # ★ §3.3 缺口协议：有缺口就是"没算完"——`complete` 不许比 `gaps` 乐观
        if run.gaps:
            run.complete = False
        return run

