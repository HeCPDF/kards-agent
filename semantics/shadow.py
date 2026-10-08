# -*- coding: utf-8 -*-
"""semantics.shadow —— P5 影子对账用的 engine 件（`player` 不许直接 import `engine`，经这里转一道）。

只做再导出：状态快照（对账口径）+ 调用重放器。逻辑在 `player/rule.py::_shadow_check`。
"""
from engine.calls import apply_calls          # noqa: F401
from engine.scripts import _state_snapshot as snapshot   # noqa: F401
