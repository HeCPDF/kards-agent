#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.chain —— 已经迁进 engine 的链：抽牌链 + autoplay 队列冲刷。

P2（2026-10-03）：从 `sim/chain.py` 原样端口（行为一字不改）；`sim.chain` re-export 旧名字。
`DRAW_CAP` 的单一来源在 `engine.state`（沙箱上限，非原版常量）。
"""
from __future__ import annotations

from engine.dispatch import Dispatcher, Event, Result
from engine.state import DRAW_CAP, H


def draw_chain(state, n, *, apply_effect, hand_cap: int = 9, anon_hold: float = 1.2,
               cap: int = DRAW_CAP, dispatcher=None, bystander_fx=None):
    """抽 `n` 张 + "抽到时"效果连锁，DFS 推到稳态（返回 `dispatch.Run`）。

    原版依据（用户 2026-10-02 口径："打出这一步后游戏里会发生什么，就模拟什么"）：
      * 牌库顶读得到 ⇒ 抽到的是**那张真实牌**（`deck` = 游戏 `DeckCardIDs_*` 的 id 序）；
      * 牌库空且牌序已知 ⇒ 走 `apply_fatigue`（伤害 = 当前计数，然后 +1）；
      * 牌序读不到（`deck_known=False`）⇒ 退回匿名牌（不假装知道抽到什么）。
      抽到手的牌若带 `_on_draw`（抵抗那种 autoplay）⇒ 它的**非抽牌部分**交给
      `apply_effect`（过渡期转调 `boardeval._apply_eff`），**抽牌部分只入队**
      （否则同一个"再抽 1 张"会被算两遍 —— 这条坑 2026-10-02 的旧实现已经踩过）。

    `hand_cap` / `anon_hold` 由调用方（评估侧）传进来：手牌上限是**规则**，
    匿名牌的持有价值是**价值** —— sim 不 import 权重表（§3.1 依赖方向）。

    `bystander_fx(card_id) -> eff | None`：**别人看到有人抽牌**时的反应（0x2A
    `OnOtherCardDrawnFromDeck`，见 `triggers.run_drawn_from_deck`；预计算结果放在调用方的表里，
    所以这里只做一次查表）。顺序按 BP `ExecuteOnDrawnFromDeck`@13783：抽到那张**自己**的
    `_on_draw` 先、旁观者后；两边的"再抽 N 张"都只**入队**（不双算）。
    """
    # ★ 队列顺序 = **FIFO**：迁移前的 `boardeval._res_eff` 就是"把再抽的 N 张追加到队尾"
    #   （不是嵌套递归）—— 纯重构不许改结果，按旧语义钉住。原版是不是 FIFO 需要 IDA
    #   证据（§7「未查」），有证据再改，并单独提交。
    from engine.natives.damage import apply_fatigue          # 空库 ⇒ 游戏自己的疲劳规则
    d = dispatcher or Dispatcher(mode="bfs", max_steps=cap * 8 + 64)
    hand = state                                       # 该链只改这一个盘面

    @d.on("draw")
    def _on_draw(ev, _ctx):
        if hand.draws_done >= cap:
            return Result(gaps=["engine.draw：抽牌次数触顶 %d（连抽链被截断）" % cap])
        hand.draws_done += 1
        cid = hand.deck.pop(0) if hand.deck else None
        on_draw = None
        if cid is not None:
            h = hand.deck_cards.get(cid)
            if h is None:                              # id 认不出模板（罕见）⇒ 按匿名牌估值
                h = H(cid, "?", 2, "order", eff={"_hold": anon_hold})
            if len(hand.hand) < hand_cap:
                hand.cap_used += 1
                hand.hand[-(1000 + hand.cap_used)] = h
            on_draw = h.eff.get("_on_draw")
            if on_draw and h.eff.get("_autoplay"):
                # 原版：autoplay 牌抽到/进手牌 ⇒ `AddAutoPlayCards` **入队**，动作边界才冲刷打出（见 flush_autoplay）
                hand.autoplay_queue.append((cid, dict(on_draw)))
                on_draw = None
        elif hand.deck_known:                          # 空库 ⇒ 游戏自己的疲劳规则
            apply_fatigue(hand)
        else:                                          # 牌序读不到 ⇒ 匿名牌，不假装知道
            if len(hand.hand) < hand_cap:
                hand.cap_used += 1
                hand.hand[-(1000 + hand.cap_used)] = H(-1, "?", 2, "order",
                                                       eff={"_hold": anon_hold})
        by = None
        if bystander_fx is not None and cid is not None:
            by = bystander_fx(cid)
        extra = 0
        for src in (on_draw, by):                  # 自己先、旁观者后（BP 顺序）
            if not src:
                continue
            eff = dict(src)
            extra += int(eff.pop("draw", 0))                # `gain_cards` 留在 eff 里交 `apply_effect`：它是造新牌、不是抽牌
            if eff:
                apply_effect(hand, eff, None)
        return Result(events=[ev.child("draw") for _ in range(extra)])

    return d.run([Event("draw", {"state": hand}) for _ in range(int(n))])


def flush_autoplay(state, apply_effect, cap: int = DRAW_CAP) -> int:
    """动作边界冲刷 autoplay 待打出队列（原版 `BP_OnlineMatch::ExecuteAutoPlayCards` / `BP_PlayerMoves` 每个动作之后）：
    队列里每张牌**免费**打出（跳过 `PayCardCost`），效果交给 `apply_effect`；打出的效果里可能再抽到 autoplay 牌 ⇒ 队列继续增长，
    循环到空（`cap` 防跑飞，超限记缺口）。→ 冲刷了几张。"""
    n = 0
    while state.autoplay_queue:
        if n >= cap:
            state.gaps.append("engine.autoplay：待打出队列触顶 %d（被截断）" % cap)
            state.autoplay_queue.clear()
            break
        _cid, eff = state.autoplay_queue.pop(0)
        n += 1
        if eff:
            apply_effect(state, dict(eff), None)
    return n

