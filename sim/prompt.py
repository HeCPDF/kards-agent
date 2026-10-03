# -*- coding: utf-8 -*-
"""sim.prompt —— 提示（Prompt）与可挂起的运行（总纲 `ARCHITECTURE.md` §1/§4）。

一个决策 = 触发 + 其后全部提示的答案路径。模拟执行到"需要**我们**做选择"的点就**挂起**：

    Done(state, trace, gaps, complete)         稳态
    Suspended(prompt, resume)                  挂起：策略对 `prompt.options` 逐个 `resume(key)`，得到各路径的 Done / 下一层 Suspended

* **随机不在这里**：依赖随机流的东西（候选列表、洗牌……）由 `sim` 按种子**先算成确定的结果**再进来；评估层不见随机。
* `resume` 不修改原状态（调用方持有的 `Suspended` 可以对不同 key 反复 `resume`）——状态在挂起点之前已经复制好。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass(frozen=True)
class Option:
    """提示里的一个候选。`key` 是**答案**（路径里存它）：抉择 = 选项下标；预报 = 天气名 / 变体名。"""
    key: Any
    label: str = ""
    payload: Any = None


@dataclass
class Prompt:
    kind: str                                   # choose_one | select_card_to_draw | hand_target | board_target | …
    options: tuple = ()
    source: Any = None                          # 触发这个提示的牌 id
    layer: int = 1                              # 同一提示链里的第几层（预报 = 1、2）
    meta: dict = field(default_factory=dict)


@dataclass
class Done:
    state: Any
    trace: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    complete: bool = True


class Suspended:
    """挂起的运行：`prompt` + `resume(key) -> Done | Suspended`。"""

    def __init__(self, prompt: Prompt, resume: Callable[[Any], Any]):
        self.prompt = prompt
        self._resume = resume

    def resume(self, key):
        if key not in [o.key for o in self.prompt.options]:
            raise KeyError("答案 %r 不在提示 %s 的候选里：%s" % (key, self.prompt.kind, [o.key for o in self.prompt.options]))
        return self._resume(key)


def resolve(run, answers) -> Any:
    """按给定答案路径一路 `resume` 到底；路径用完仍挂起 ⇒ 返回当前的 `Suspended`（调用方决定）。"""
    r = run
    for k in answers:
        if not isinstance(r, Suspended):
            break
        r = r.resume(k)
    return r


def leaves(run, max_leaves: Optional[int] = None) -> List[tuple]:
    """DFS 展开所有叶路径 → `[(path, Done)]`（`max_leaves` 到了就停）。"""
    out: List[tuple] = []

    def go(r, path):
        if max_leaves is not None and len(out) >= max_leaves:
            return
        if isinstance(r, Done):
            out.append((path, r))
            return
        for o in r.prompt.options:
            go(r.resume(o.key), path + (o.key,))

    go(run, ())
    return out
