#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent —— **命令层**：把读侧（`kardsmem`）和写侧（`ops_inject`）收成一组动词。

为什么要有这一层
================
交互 shell、MCP server、NN 三个前端要的东西**完全一样**：

    看盘面 / 看某张卡 / 出牌 / 移动 / 攻击 / 结束回合 / 等我方回合 / 看历史

如果每个前端各写一遍，三份实现会立刻开始漂移 —— 尤其是"动作前要不要预检"
"提示文本怎么回收"这种带策略的部分。所以：

    session.py   AgentSession —— **唯一**的动词集合，返回结构化结果（dict）
    view.py      把盘面渲染成人看的表；把 `h1/m2/e3` 这种编号翻译回卡
    （历史不在本层：走 `kardsmem.matchlog` 的对局动作流，§7.6g）
    legality.py  出牌前的进程外预检（Kismet 求值器跑 `CanPlayFromHand`）
    shell.py     交互式 REPL           ← 前端 1
    (mcp.py)     MCP server            ← 前端 2（未做）
    (nn.py)      策略循环              ← 前端 3（未做）

三条不变量
==========
1. **读走内存，写走合成事件。** 命令层不碰这条线：所有"读"落到 `kardsmem`/`board_api`
   的只读路径，所有"写"落到 `ops_inject` 的合成事件（不挪真实光标）。没有第三种。
   ★ 2026-09-27：旧的物理鼠标实现 `ops.py` 已归档为 `_archive/ops_mouse.py`。
2. **预检只挑不判。** `legality` 说"不合法"时动词返回 `rejected`，**不是**失败；
   调用方带 `force=True` 就照发。游戏才是裁判（规格 §7.6f）。
3. **读不到就说读不到。** 任何字段拿不到一律是 `None` + 进 `unknown`，
   绝不用默认值糊过去。
"""
from __future__ import annotations

__all__ = ["AgentSession", "render_board", "render_inspect", "render_history",
           "resolve", "Handles"]


def __getattr__(name):
    """按需从子模块取符号。★ 只找**存在的**子模块 —— 以前列了个还没写的
    `journal`，结果 `from . import view` 也被它连累炸掉（子模块导入会先走这里）。"""
    import importlib
    for mod in ("session", "view"):
        try:
            m = importlib.import_module("." + mod, __name__)
        except ImportError:
            continue
        if hasattr(m, name):
            v = getattr(m, name)
            globals()[name] = v
            return v
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
