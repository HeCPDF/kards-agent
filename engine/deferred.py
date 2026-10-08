#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.deferred —— **延迟/常驻效果登记表**（2026-10-07，用户："延迟效果有很多。不能只建模这一个"）。

问题：很多牌的效果**活过打出这一步**——指令打出后 `OnPlayedFromHand` 只是"布防"，真正的后果在之后某个事件的钩子里
（ECHELON：`OnAfterOtherCardAttacks`；天气/战备类：`OnStartOfTurn`；"回合结束时…"：`OnEndOfTurn`；…）。
全量清单与分类见 `docs/DEFERRED-EFFECTS-CENSUS.md`（`tools/census_deferred.py` 生成：732 张指令里 180 张留下了钩子）。
以前打出这类牌只看 `OnPlayedFromHand` ⇒ 空跑结果是 `{}` ⇒ 被当成"已知的空"（`eff_src="vm(空)"`）——**把没建模说成了没效果**。

本模块是一个**通用设施**，不是 per-card 规则：

  * **布防**（`arm`）：指令打出时（`sim.engine.sim_order`），按 `event_fx["armed_spec"][手牌 id]["hooks"]`（rule 从牌的 `On*` 覆写读出）
    在 `Sim.armed` 里登记 `Armed(来源牌, 钩子名, 座位)`。钩子名不在 `FIRE_POINTS` 里 ⇒ **记缺口**（`sim.gaps`），不静默。
  * **后果**不是手写的：`event_fx["armed"][(来源牌, 钩子名, 事件主体)]` = rule 用 VM 把那个钩子体对"此事件 + 此主体"空跑出来的效果桶
    （授予者账 `Sim.grants` 通过 `view_overrides` 喂给 `HasCustomAbilityFromCard`）。sim 只负责"事件发生时查表应用"。
  * **触发**（`fire`）：sim 的事件点（`FIRE_POINTS`）调它——攻击结算后 / 回合结束 / 回合开始。应用走调用方注入的 `apply`
    （engine 不 import sim，同 `calls.apply_calls(on_draw=…)` 的分层）。
  * **诚实**：事件点没接 / 主体没预算 / 桶缺失 ⇒ 都进 `state.gaps`；rule 把"带未建模延迟钩子的牌"记成缺口而不是 `vm(空)`。

giver 级账 `Sim.grants` 由 `natives.abilities.grant_ability` 写（字典路 / 直跑 / 重放三条路同一实现）。
"""
from __future__ import annotations

from typing import Callable, Optional

#: 打出 / 创建时就跑完的钩子（不算"留下来"）。`OnEnterPlay` 是单位部署那一步，同样一次性。census 工具与 rule 共用这一份。
PLAYTIME_HOOKS = frozenset((
    "OnPlayedFromHand", "OnHandTargetSelected", "OnCreateCard", "OnCreateCardApplyCampaignUpgrades",
    "OnEnterPlay", "OnStartOfGame", "OnCardSpawnedInHand", "OnCardDrawnFromDeck", "OnCardReset",
    "OnCardRevealed", "OnCounterMeasureTriggered",
))


def leftover_hooks(handlers) -> list:
    """牌自己覆写的 `On*` 里**留下来的**那些（打出后仍挂着）。"""
    return sorted(h for h in (handlers or ()) if str(h).startswith("On") and h not in PLAYTIME_HOOKS)


#: 留下来的钩子 → **sim 里会在哪个事件点触发它**（单一来源；census 工具的"已接/未接"列也读这张表）。
#: 不在表里的钩子 = 未接：`arm` 记缺口。接一个新钩子 = 这里加一行 + 对应事件点调 `fire` + rule 侧能预计算它的桶。
FIRE_POINTS = {
    "OnAfterOtherCardAttacks": "攻击结算后（sim_attack / sim_enemy_attack_event；事件主体=被打的牌）",
    "OnEndOfTurn": "己方回合结束（sim_turn_end）",
    "OnStartOfTurn": "己方回合开始（sim_turn_start ⑥）",
    "OnBeforeStartOfTurn": "己方回合开始（sim_turn_start ③）",
}
#: 事件主体不是 None 的钩子（桶按主体分：ECHELON 被打的牌不同，受益的单位集合不同）。
SUBJECT_HOOKS = frozenset(("OnAfterOtherCardAttacks",))
#: 事件由**对方**发起（我方回合的搜索里不会自己发生；评估侧要另外估，见 `evaluation.value.armed_value`）。
ENEMY_EVENT_HOOKS = frozenset(("OnAfterOtherCardAttacks",))


class Armed:
    """一条已布防的留下来的钩子。`src` = 来源牌 id；`side` = 来源牌的座位（回合钩子只在**它的**回合触发）；
    `one_shot` = 触发一次后摘掉（rule 能证明来源牌在钩子里自摧毁时才置位；缺省常驻）。"""
    __slots__ = ("src", "hook", "side", "one_shot")

    def __init__(self, src, hook, side, one_shot=False):
        self.src, self.hook, self.side, self.one_shot = src, hook, side, bool(one_shot)

    @property
    def enemy_event(self) -> bool:
        """事件由对方发起（我方回合搜索不展开）——评估侧据此另估（`evaluation.value.armed_value`；不 import 本模块 ⇒ 挂在对象上）。"""
        return self.hook in ENEMY_EVENT_HOOKS

    def copy(self) -> "Armed":
        return Armed(self.src, self.hook, self.side, self.one_shot)

    def key(self) -> tuple:
        return (self.src, self.hook, self.side, self.one_shot)

    def __repr__(self) -> str:
        return "Armed(%s %s side=%s%s)" % (self.src, self.hook, self.side, " one-shot" if self.one_shot else "")


def _spec(state, src) -> dict:
    return ((getattr(state, "event_fx", None) or {}).get("armed_spec") or {}).get(src) or {}


def arm(state, src, side) -> list:
    """指令 `src` 打出之后布防：按 `armed_spec[src]["hooks"]` 登记。返回**没接**的钩子名（已记缺口）。"""
    unmodeled = []
    sp = _spec(state, src)
    for h in sp.get("hooks") or ():
        if h in FIRE_POINTS:
            if not any(a.src == src and a.hook == h for a in state.armed):
                state.armed.append(Armed(src, h, side, one_shot=h in (sp.get("one_shot") or ())))
        else:
            unmodeled.append(h)
            state.gaps.append("deferred.unmodeled：来源牌 %s 的留下来的钩子 %s 没有 sim 事件点（FIRE_POINTS 之外），打出后效果不计" % (src, h))
    return unmodeled


def fire(state, hook: str, subject=None, *, apply: Callable, side=None) -> int:
    """事件 `hook` 发生（`subject` = 事件主体 id，如被打的牌；回合钩子为 None）：对每条匹配的布防查桶并应用。

    * `side`：回合钩子只触发**该座位**的牌（己方回合 ⇒ 己方的牌）；主体型钩子 `side=None` = 双方的牌都看（桶本身按守卫算过）。
    * 桶缺失：该主体**在预计算名单里**（`armed_spec[src]["probed"]`）⇒ 钩子体空跑后没有效果 ⇒ 什么都不发生；
      **不在名单里**（预算耗尽 / 事后才进场的单位）⇒ 记缺口，不猜。
    返回应用的桶数。
    """
    n = 0
    efx = (getattr(state, "event_fx", None) or {}).get("armed") or {}
    for a in list(state.armed):
        if a.hook != hook:
            continue
        if side is not None and hook not in SUBJECT_HOOKS and a.side != side:
            continue
        bucket = efx.get((a.src, hook, subject))
        if bucket:
            apply(state, bucket)
            n += 1
            if a.one_shot:
                state.armed.remove(a)
            continue
        sp = _spec(state, a.src)
        if hook in SUBJECT_HOOKS:
            if subject in (sp.get("probed") or ()):
                continue                                # 空跑过：这个主体上钩子体没有效果（例：守卫挡掉了）
        elif hook in (sp.get("probed_hooks") or ()):
            continue                                    # 回合钩子空跑过、结果为空
        state.gaps.append("deferred.unprobed：来源牌 %s 的 %s 在主体 %s 上没有预计算的后果（预算/事后进场/没空跑）——不计"
                          % (a.src, hook, subject))
    return n
