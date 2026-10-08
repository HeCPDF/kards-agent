#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.effects —— 写侧规则（"某个动词到底改了什么状态"）的 L2 实现。

规矩（`EVAL-ARCHITECTURE.md` §3.1/§3.3）：
  * 规则只写在这里（或 `sim/` 的其它模块），**评估侧不许再实现一遍**；
  * 每条规则的出处写在函数上（报告章节 + IDA 地址/未读就如实写"未读"）；
  * 语义没证据 ⇒ 只记缺口，**不动状态**（§3.3 缺口协议）。

P2（2026-10-03）：受伤/阵亡（`deal_damage`）、`MakeCardsFight`（`apply_fight`）与压制（`apply_suppress`）
已端口进 `engine.natives`（移植体与出处注释在那里），这里保留旧名字转调 —— 调用方
（`sim.engine` / `policy.boardeval` / 测试）不用改。
"""
from __future__ import annotations

# 端口进 engine 的规则（P2）：旧名字从本模块 re-export，行为一字不改。
from engine.natives.cards import apply_salvage                                    # noqa: E402,F401
from engine.natives.damage import FIGHT_GAP, apply_fight, deal_damage             # noqa: E402,F401
from engine.natives.status import SUPPRESS_GAP, SUPPRESS_STRIP, apply_suppress    # noqa: E402,F401
