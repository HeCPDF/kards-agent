"""一次「建 sim」期间的只读共享缓存（`RuleV2._search_sim` 开一个作用域，里面所有 VM 空跑共用）。

为什么要有它（2026-10-07，离线剖析 `_search_sim` 的 build_sim_s 3.9 s 中位数）：
  `death_fx` / `attack_fx` / `draw_fx` 逐项各跑一遍 `effectvm.record_effects`（几百次），而**每次空跑都白手起家**：
  * 卡视图（`cards.read_raw`，内含 `atomic` 读 ⇒ 两次读 + `sleep(1.5ms)`）按指针重读，同一张牌在一次建 sim 里被读几十次；
  * 反射链 `props.find_prop`（走 UClass 属性链）按 (类, 字段名) 重走；
  * `_fill_cur_stats` 把快照里**每张牌**的数值再算一遍（每次空跑 ~3 ms，输入全相同）。
  这些的输入在**一次建 sim 的几秒内是同一份**（`st` 是一张静止快照；空跑 VM 是纯的、不写回游戏）⇒ 共用一份是等价的，
  而且比"各读各的"更一致（游戏在后台动画时不会让两次空跑读到不同的牌面）。

边界（别扩大）：
  * **只在 `build_scope()` 里生效**；作用域外（离线测试、单次探针、手写 `record_effects` 调用）一切行为与以前完全一致；
  * **线程私有**（`threading.local`）：别的线程的空跑不会读到本线程作用域里的东西；
  * **不跨作用域**：下一次建 sim（盘面可能已变）从空表开始——跨步复用要证明"输入没变"，这里不做（见 TODO.md 同日条）；
  * 读失败（`None`/`{}`）**不进共享表**，下一次空跑照旧重试（与"每次空跑自己读"的旧口径一致）。
"""
from __future__ import annotations

import contextlib
import threading

_tls = threading.local()


class Scope:
    """共享表的容器 + 命中统计（进 `probe.timing.stages` 旁边的诊断，不参与决策）。"""

    __slots__ = ("tables", "hits", "misses", "calls")

    def __init__(self):
        self.tables = {}
        self.hits = {}
        self.misses = {}
        self.calls = []        # [(耗时 s, 钩子名, 卡指针, 视图未命中数, 停因)] —— `engine.effectvm.record_effects` 逐次记（诊断）

    def table(self, name: str, owner=None) -> dict:
        """按 (表名, 属主对象 id) 取一张表（属主 = 内存会话，避免两个会话的指针混用）。"""
        return self.tables.setdefault((name, id(owner) if owner is not None else 0), {})

    def note(self, name: str, hit: bool) -> None:
        d = self.hits if hit else self.misses
        d[name] = d.get(name, 0) + 1

    def note_call(self, dt: float, hook, ptr, vmiss: int, stopped=None) -> None:
        self.calls.append((dt, hook, ptr, vmiss, stopped))

    def stats(self) -> dict:
        return {"hits": dict(self.hits), "misses": dict(self.misses)}

    def hot(self, top: int = 5) -> dict:
        """诊断：这次建 sim 里 VM 空跑的总账 + 最贵的几次 + 按钩子汇总（只读，不参与决策）。
        2026-10-08：同一份代码 build_sim_s 中位 0.24 s / 0.72 s / 1.8 s，日志里只有"视图未命中 83 次"这个结果，
        看不出是谁读的 ⇒ 逐次记（钩子 / 卡指针 / 耗时 / 该次新读了多少张卡视图）。"""
        by = {}
        for dt, hook, _p, vm, _st in self.calls:
            r = by.setdefault(hook or "?", [0, 0.0, 0])
            r[0] += 1
            r[1] += dt
            r[2] += vm
        worst = sorted(self.calls, key=lambda c: -c[0])[:top]
        return {"n": len(self.calls), "sum_s": round(sum(c[0] for c in self.calls), 3),
                "by_hook": {k: [v[0], round(v[1], 3), v[2]]
                            for k, v in sorted(by.items(), key=lambda kv: -kv[1][1])[:top]},
                "worst": [{"s": round(dt, 3), "hook": hook, "ptr": ptr, "vmiss": vm, "stopped": (str(st)[:60] if st else None)}
                          for dt, hook, ptr, vm, st in worst]}


def current():
    """当前线程正在生效的作用域；没有 ⇒ None（调用方走旧路径）。"""
    return getattr(_tls, "scope", None)


@contextlib.contextmanager
def build_scope():
    """开一个作用域；嵌套时复用最外层（内层不新开、不提前清）。"""
    outer = current()
    if outer is not None:
        yield outer
        return
    sc = Scope()
    _tls.scope = sc
    try:
        yield sc
    finally:
        _tls.scope = None
