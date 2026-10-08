# -*- coding: utf-8 -*-
"""semantics.deferred —— `player` 不许直接 import `engine`，经这里转一道（同 `semantics/shadow.py`）。

只做再导出：延迟/常驻效果登记表的**常量与判定**（真实现 `engine/deferred.py`）。
"""
from engine.deferred import (ENEMY_EVENT_HOOKS, FIRE_POINTS, PLAYTIME_HOOKS, SUBJECT_HOOKS,   # noqa: F401
                             Armed, arm, fire, leftover_hooks)
