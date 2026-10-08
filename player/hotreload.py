"""热重载（2026-10-01）：改了 Python 代码后，**不重启监听器/游戏**就在运行中的对局里生效。

用法：往 `kards-data/live/hot_reload.txt`（`agent/paths.py`）写模块名（每行一个，空文件 = 默认列表），
`nn.Loop.step` 每一步开头检查这个文件，存在就执行 `run()` 然后删掉它。

做法（只重载 Python，**不动 frida 里的 JS**——JS/CModule 的改动仍然要重启监听器）：
  * `importlib.reload(mod)` 会在**同一个模块对象**里重新执行代码：模块级函数/常量的全局字典不变，
    引用 `mod.func` 的地方立即用到新版本；
  * 但**类**会被重新创建成新类对象，已经存在的实例（`Injector`、`RuleV2`、`Loop`……）还挂在旧类上 ⇒
    把新类里的方法/类属性**覆盖到旧类**上，再把模块字典里的类名**指回旧类**（保持 isinstance/实例关系不变）。
  * 重载失败（语法错等）不影响正在跑的版本：先 `compile` 检查，再 reload；出错只记录。
注意：新增的**实例属性**（`__init__` 里新加的字段）旧实例没有——用 `getattr(self, 'x', 默认)` 的写法，或重启。
"""
from __future__ import annotations

import importlib
import os
import sys
import time

from base import paths as _P

FLAG = _P.HOT_RELOAD_FLAG
LOG = _P.HOT_RELOAD_LOG
# ★ 2026-10-03：boardeval 已拆成分层包（sim/evaluation/policy）——按依赖顺序（下层在前）重载。
# ★ 2026-10-04（P5）：门面 `policy.boardeval` **已删** ⇒ 从重载清单里去掉。留着的话每次热重载都会记一条
#   `policy.boardeval: 异常 No module named …`（逐模块 catch、不中断，但**必报错**，日志会被噪声污染 ✗）。
# ★ 2026-10-03（D1）：`ops/inject.py` 拆成 mixin 模块——方法在 `ops.<mixin>` 里，`Injector` 自己的类字典几乎是空的，
#   所以 mixin 模块必须**先于** `ops.inject` 重载（`reload_module` 把新方法并回各自的旧 mixin 类，旧 `Injector` 实例继承的就是旧 mixin 类）。
# ★ 2026-10-03（P2）：`engine/` 是真实现（natives/state/dispatch），`sim/*` 里有的是 re-export 壳
#   —— engine 模块必须排在用它的 sim 模块**之前**重载，否则壳里还绑着旧函数对象。
# ★ 2026-10-06（P6）：`engine.triggers` 拆成 `triggers_specs`（数据表）/ `triggers` / `triggers_families`（钩子族）——
#   顺序固定 specs → triggers → families（families 末尾把新函数写回 `engine.triggers`）。
#   `semantics.triggers` / `semantics.effectvm` 是同一模块对象别名（sys.modules），只重载 engine 侧即可。
DEFAULT_MODULES = ["ops", "ops.consts", "ops.support", "ops.conn", "ops.world", "ops.gesture", "ops.query", "ops.play",
                   "ops.choices", "ops.flow", "ops.cli", "ops.inject",
                   "engine.state", "engine.dispatch", "engine.natives.board", "engine.natives.damage",
                   "engine.natives.status", "engine.natives.kredits", "engine.natives.cards",
                   "engine.chain", "engine.triggers_specs", "engine.triggers", "engine.triggers_families", "engine.effectvm", "engine.adapter",
                   "sim.state", "sim.prompt", "sim.dispatch", "sim.chain", "sim.effects",
                   "sim.engine", "sim.adapter", "evaluation.value", "policy.plan", "policy.answer", "policy.forced",
                   "policy.search", "agent.promptinfo", "agent.ops_result", "player.rule", "player.play", "agent.session",
                   "kardsmem.rng", "semantics.forecast", "player.loop", "player.play_guard"]


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:                                              # noqa: BLE001
        pass


def _rebind_class_cell(member, new, old) -> int:
    """零参 `super()` 的方法带一个 `__class__` 闭包单元，指向**定义它的类**。把新类的方法并回旧类时，
    这个单元仍指新类 ⇒ 旧实例调用 `super()` 报 `TypeError: obj is not an instance or subtype of type`
    （CLAUDE.md 弯路 #39）。根治：把单元里的类改指旧类。返回改写的单元数。"""
    fns = []
    if isinstance(member, (classmethod, staticmethod)):
        fns.append(member.__func__)
    elif isinstance(member, property):
        fns.extend(f for f in (member.fget, member.fset, member.fdel) if f is not None)
    else:
        fns.append(member)
    n = 0
    for fn in fns:
        code, cells = getattr(fn, "__code__", None), getattr(fn, "__closure__", None)
        if code is None or not cells or "__class__" not in code.co_freevars:
            continue
        cell = cells[code.co_freevars.index("__class__")]
        try:
            if cell.cell_contents is new:
                cell.cell_contents = old
                n += 1
        except (ValueError, TypeError):                            # 空单元 / 不可写
            continue
    return n


def reload_module(name: str) -> str:
    """重载一个模块并把新类的内容并回旧类。返回一行结果文字。"""
    mod = sys.modules.get(name)
    if mod is None:
        return "%s: 未导入，跳过" % name
    if getattr(sys, "frozen", False):
        # 冻结的 exe 包：代码在 PYZ 里、没有 .py 可重读，热重载不适用（改代码要重新打包）。
        return "%s: 冻结包不支持热重载，跳过" % name
    path = getattr(mod, "__file__", None)
    if path:
        try:
            with open(path, "rb") as f:
                compile(f.read(), path, "exec")                  # 语法错就别 reload
        except SyntaxError as e:
            return "%s: 语法错误，未重载（%s）" % (name, e)
    old_classes = {k: v for k, v in vars(mod).items() if isinstance(v, type) and v.__module__ == mod.__name__}
    try:
        importlib.reload(mod)
    except Exception as e:                                         # noqa: BLE001
        return "%s: reload 失败（%s: %s）" % (name, type(e).__name__, e)
    patched = 0
    for cname, old in old_classes.items():
        new = vars(mod).get(cname)
        if new is None or new is old or not isinstance(new, type):
            continue
        for k, v in list(vars(new).items()):
            if k in ("__dict__", "__weakref__", "__module__", "__qualname__", "__doc__"):
                continue
            _rebind_class_cell(v, new, old)
            try:
                setattr(old, k, v)
                patched += 1
            except Exception:                                      # noqa: BLE001
                pass
        setattr(mod, cname, old)                                   # 模块里的类名指回旧类（实例关系不变）
    return "%s: ok（并回 %d 个类属性，%d 个类）" % (name, patched, len(old_classes))


def run(flag_path: str = FLAG) -> list:
    """读 flag 文件（模块名列表，空 = 默认），逐个重载，删掉 flag，写日志。"""
    try:
        with open(flag_path, "r", encoding="utf-8") as f:
            names = [ln.strip() for ln in f.read().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    except Exception:                                              # noqa: BLE001
        names = []
    names = names or list(DEFAULT_MODULES)
    out = []
    for n in names:
        try:
            out.append(reload_module(n))
        except Exception as e:                                     # noqa: BLE001
            out.append("%s: 异常 %s" % (n, e))
    try:
        os.remove(flag_path)
    except Exception:                                              # noqa: BLE001
        pass
    for line in out:
        _log(line)
    return out


def check_and_run() -> list:
    """`Loop.step` 每步调用：flag 在就重载，不在就几乎零开销。"""
    if os.path.exists(FLAG):
        return run()
    return []
