# -*- coding: utf-8 -*-
"""semantics.triggers —— 兼容别名：真实现已端口到 `engine.triggers`（P2，2026-10-03）。

为什么不是逐名 re-export：本模块的函数互相调用内部私有名（`_run_hook_ex` / `_run_hook` …），
调用方与测试会直接给模块属性打补丁（`TR._run_hook_ex = fake`，见 `tests/_hookfake.py`、
`test_attack_hooks.py` 等十几处）。逐名 re-export 会让补丁打在壳上、engine 内部调用看不见；
把 `semantics.triggers` 直接替换成 `engine.triggers` 这**同一个模块对象**，属性读写、
私有名、monkeypatch 全部保持单例（`semantics.triggers is engine.triggers`）。

P4 把玩家侧 import（`player/rule.py` 的 `from semantics import triggers as TR`）改成
`engine.triggers` 之后，本文件删除。
"""
import sys as _sys

from engine import triggers as _impl

_sys.modules[__name__] = _impl
