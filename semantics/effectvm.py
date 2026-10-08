# -*- coding: utf-8 -*-
"""semantics.effectvm —— 兼容别名：真实现已端口到 `engine.effectvm`（P2，2026-10-03）。

为什么不是逐名 re-export：与 `semantics/triggers.py` 同理——调用方（`player/rule.py`、多个测试）
按模块属性用 `EV.record_effects` 一类名字，且可能有 monkeypatch；替换成**同一个模块对象**后
属性读写、私有名、补丁全部保持单例（`semantics.effectvm is engine.effectvm`）。

P4 把玩家侧 import 改成 `engine.effectvm` / `engine.scripts` 之后，本文件删除。
"""
import sys as _sys

from engine import effectvm as _impl

_sys.modules[__name__] = _impl
