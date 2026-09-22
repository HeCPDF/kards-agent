#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem —— KARDS **内存侧（只读）工具链**的统一入口。

一行话
======
    from kardsmem import attach
    s = attach()
    print(s.snapshot().cards)      # 盘面/手牌/弃牌（board_api mem 后端）
    print(s.gs().deck_ids(LOCAL))  # GameState 额外字段
    print(s.names().fname_of(...))  # FName（真名字池）
    print(s.rendered())            # 屏幕上摆着的每一张卡（不读图）
    print(s.pick())                # 是不是在等我选牌 + 候选是谁

分层（每层只依赖下一层）
========================
| 模块 | 职责 |
|---|---|
| `build`  | 多份同名 exe 的指纹 + 本/旧 build 的 RVA 表（**唯一真源**） |
| `proc`   | 只读 attach、MemRO（定长读/原子读/u16,u64）、进程枚举 |
| `world`  | GWorld → UWorld / GameState / Board / PlayerController |
| `gs`     | GameState 额外字段（牌库 id、静态卡表、隐藏指挥点） |
| `names`  | FNamePool（0x0911B9C0）→ 字符串；FText 明文回退 |
| `cards`  | 盘面卡（`AllCardsInBattle` 24B 步长 + 解密），委托 board_api |
| `rendered` | `ABP_BaseCard_C` actor：屏幕上摆着的每一张卡 |
| `pick`   | 选择界面状态（抉择/预报/换牌）+ 候选聚合 |
| `snapshot` | 上面全部 → 一个 dict（可 JSON 落盘） |
| `cli`    | `python -m kardsmem <cmd>` |

红线
====
**只读**。全部路径只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` +
`ReadProcessMemory`；没有写入、注入、hook。（执行侧（模拟鼠标）不在本包内，
见 `tools/ops.py`；本包只负责"读"。）

底层原语不重复实现：`board_api.py` 是 `OpenProcess/ReadProcessMemory` 与
"盘面卡"逻辑的唯一实现处，本包继承/调用它，只补它缺的那几块。
"""

from __future__ import annotations

__version__ = "1.0"

from . import build, proc, world          # noqa: F401
from .build import BUILDS, RVA, RVA_OLD, BuildInfo, WORKSPACE  # noqa: F401
from .proc import (BuildMismatch, MemRO, ProcessNotFound, Session,  # noqa: F401
                   attach, list_kards_processes, probe)

# 其余子模块按需加载（写它们的文件可能在整理过程中被移动，避免 import 期炸）
_LAZY = ("cards", "cli", "gs", "names", "pick", "rendered", "snapshot")


def __getattr__(name):
    if name in _LAZY:
        import importlib
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


__all__ = ["build", "proc", "world", "attach", "Session", "MemRO", "probe",
           "list_kards_processes", "BuildInfo", "BUILDS", "RVA", "RVA_OLD",
           "WORKSPACE", "ProcessNotFound", "BuildMismatch", "__version__"] + list(_LAZY)
