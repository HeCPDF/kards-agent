# -*- coding: utf-8 -*-
"""engine —— 目标架构里的"无头游戏"（`docs/REFACTOR-PLAN.md` §1/P2）。

分工：`kardsmem` 读侧产出**原版结构**（`gamemodel.GameState` / `BaseCardObject`），engine 在其上
**照原版重写规则**（移植而不是设计，`docs/SIM-FIDELITY.md`）：`natives/` 是原版原生函数的 Python 版，
之后 `triggers.py` 是触发分发、`scripts.py` 直接在状态上跑卡牌蓝图字节码。`sim` 是过渡实现，
逐步改成 engine 的薄壳（P2 起的迁移顺序见 REFACTOR-PLAN）。

当前进度（2026-10-03，P2）：`state / dispatch / chain / natives/* / triggers` 已端口进 engine，
`sim/*` 与 `semantics.triggers` 是兼容壳（re-export / 同一模块对象别名）。逐刀清单与下一步见
`docs/REFACTOR-PLAN.md` §5。
"""

