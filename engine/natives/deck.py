# -*- coding: utf-8 -*-
"""engine.natives.deck —— 牌库/手牌的**造牌与搬牌**原生（字典路 `sim._apply_eff`、直跑 sink、重放 `apply_calls` 三方共用的**唯一实现**）。

为什么单独成包：对账（`tools/reconcile_batch.py`）发现这几类动词三条路各写一份、写得不一样（见 TODO「2026-10-06 牌库/手牌对账」）：

  * `SpawnCardInDeckBySide`（`BP_CardFunctions.cpp:6648`）：先 `spawnerID > 0` 才干活；逐张 `CreateCard`（位置=牌库）→ **每张都抽一次**
    `RandomIntFromRangeWithStream(0, 牌库张数)`（无条件，结果只在 `RandomWithoutShuffle` 时当插入位置）→
    `AddCardToDeckBySide(side, 新id, addToTop = !bottom, position = RandomWithoutShuffle ? 随机 : -1)`；全部塞完后 `shuffle` ⇒
    `ShuffleDeckBySide(side, true, spawnerID)`（**一次**）。
  * `SpawnCardinHandbySide`（`:6371`）：`spawnerID > 0` 才干活；`CreateCard(位置=手牌)` ⇒ 手里**多一张新牌**，**不碰牌库**、不触发"抽到时"。
    以前字典路/直跑都把它记成 `gain_cards` 走**抽牌链**（= 把牌库顶那张抽进手牌）⇒ 牌库少一张、后面的牌序全错位。
  * `MoveCardToTopOfOwnersDeck` / `MoveMultipleCardsToTopOfOwnersDeck`（`:3415` / `:14112`）→ `MoveCardToTopOfDeck`（`:17880`）→
    `GameState::AddCardToDeckBySide(originalSide, cardID, true, positionFromTop)`：牌**离开手牌**，按 `positionFromTop` 插入牌库
    （0 = 牌顶）。多张是**逐张**调用（后插的在更上面）。
  * `ShuffleDeckBySide`：`Array_ShuffleFromStream`（`kardsmem.rng.Stream.shuffle`），耗随机流 N 次。

本包不 import `sim`（依赖方向：kardsmem → engine → sim）：只认鸭子类型的 `state`（`deck`/`deck_known`/`deck_cards`/`card_templates`/`hand`/`tmp_seq`/
`cap_used`/`gaps`/`spawn_stats`/`hold_v`）。读不出 ⇒ 记缺口，不编数。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from engine.natives.board import HAND_CAP

__all__ = ["make_card", "spawn_in_deck", "shuffle_deck", "move_hand_to_deck", "spawn_in_hand", "pull_specific_to_top"]


def make_card(state, cid: int, name: str, hold: Optional[float] = None):
    """按**卡名**造一张模拟用的牌模板 `H`。

    优先读静态卡表面板（`state.spawn_stats(内部名)` ⇒ 费用/类型/攻防；与"生成单位""三选一"同一来源）；读不到 ⇒ 中性占位
    （费用 2、`order`）。**效果 eff 一律留空**（原版新牌的效果要跑它自己的字节码，那是调用方的事 ⇒ 调用方记缺口）；
    只挂 `_hold`（匿名牌的持有价值，评估侧给）。"""
    from engine.state import H                                          # noqa: PLC0415
    eff = {"_hold": hold} if hold else {}
    stat = None
    fn = getattr(state, "spawn_stats", None)
    if callable(fn):
        # 静态面板不会变、而字典路在束搜索里会反复造牌 ⇒ 按卡名缓存在回调函数对象上（`Sim.copy` 共享同一个回调）；
        # 回调是绑定方法（不能挂属性）就不缓存。
        cache = getattr(fn, "_deck_stat_cache", None)
        if cache is None:
            try:
                fn._deck_stat_cache = cache = {}
            except (AttributeError, TypeError):
                cache = None
        if cache is not None and name in cache:
            stat = cache[name]
        else:
            try:
                stat = fn(name)
            except Exception:                                           # noqa: BLE001
                stat = None
            if cache is not None and stat is not None:                  # 读失败（None）不缓存：下次再试
                cache[name] = stat
    if stat:
        return H(cid, name, stat.get("cost") or 0, stat.get("typ") or "order", stat.get("atk") or 0, stat.get("dfn") or 0,
                 (), eff)
    return H(cid, name, 2, "order", eff=eff)


def _gaps(state, gaps):
    if gaps is not None:
        return gaps
    g = getattr(state, "gaps", None)
    if g is None:
        g = state.gaps = []
    return g


def _dict(state, name):
    d = getattr(state, name, None)
    if d is None:
        d = {}
        setattr(state, name, d)
    return d


def _hold(state, hold):
    return hold if hold is not None else getattr(state, "hold_v", None)


def spawn_in_deck(state, name: str, n: int, *, bottom: bool = False, random_wo_shuffle: bool = False,
                  rng=None, hold: Optional[float] = None, gaps: Optional[list] = None) -> List[Tuple[int, int]]:
    """`SpawnCardInDeckBySide` 的塞牌段（**不含**末尾洗牌，见 `shuffle_deck`）→ `[(新牌 id, 插入下标)]`。

    牌库未知（`deck_known=False`）⇒ 记缺口、不塞。每张**都**耗一次 `rng.wrapper_int(0, len(deck))`（没有 rng 就没法对齐 ⇒ pos=None，
    且 `random_wo_shuffle` 退成牌顶，同旧实现）。"""
    out: List[Tuple[int, int]] = []
    if not getattr(state, "deck_known", False):
        _gaps(state, gaps).append("SpawnCardInDeckBySide：牌库未知 ⇒ 只记缺口（同 `sim`）")
        return out
    if not isinstance(getattr(state, "deck", None), list):
        state.deck = []
    deck = state.deck
    h = _hold(state, hold)
    for _ in range(int(n)):
        state.tmp_seq = int(getattr(state, "tmp_seq", 0) or 0) + 1
        cid = -(3000 + state.tmp_seq)
        tpl = make_card(state, cid, name, h)
        pos = rng.wrapper_int(0, len(deck)) if rng is not None else None
        if random_wo_shuffle and pos is not None:
            idx = min(len(deck), pos)
        elif bottom:
            idx = len(deck)
        else:
            idx = 0                                             # addToTop = !bottom
        deck.insert(idx, cid)
        _dict(state, "deck_cards")[cid] = tpl
        _dict(state, "card_templates")[cid] = tpl
        out.append((cid, idx))
    return out


def shuffle_deck(state, rng, gaps: Optional[list] = None) -> bool:
    """`ShuffleDeckBySide`（己方牌库）：用牌局随机流洗。没有 rng ⇒ 洗不出来，牌库标成未知（`deck=[]`、`deck_known=False`，同 `sim`）。
    空牌库/未知牌库 ⇒ 什么都不做（原版洗空数组不耗随机流）。→ 是否真的洗了。"""
    deck = getattr(state, "deck", None)
    if not deck:
        return False
    if rng is None:
        state.deck, state.deck_known = [], False
        _gaps(state, gaps).append("ShuffleDeckBySide：没注入随机流 ⇒ 洗不出来，牌库标成未知（同 `sim`）")
        return False
    state.deck = list(rng.shuffle(list(deck)))
    return True


def move_hand_to_deck(state, cid, pos=0, gaps: Optional[list] = None) -> Optional[int]:
    """把一张**手牌**放进己方牌库的 `pos`（从牌顶数，0 = 牌顶）。→ 插入下标；做不了 ⇒ None（原因已记缺口）。

    原版：`MoveCardToTopOfDeck`（`:17880`）→ `AddCardToDeckBySide(side, id, true, positionFromTop)`。牌从手牌里拿走。
    `pos` 不是整数 ⇒ 当 0（牌顶）并记缺口。"""
    hand = getattr(state, "hand", None) or {}
    if cid not in hand:
        _gaps(state, gaps).append("牌库顶：%r 不在我方手牌（敌方手牌不建模）⇒ 未结算" % (cid,))
        return None
    h = hand.pop(cid)
    _dict(state, "card_templates").setdefault(cid, h)
    if not getattr(state, "deck_known", False):
        _gaps(state, gaps).append("牌库顶：牌库顺序未知（`deck_known=False`）⇒ 只记缺口，不放回")
        return None
    if isinstance(pos, bool) or not isinstance(pos, int):
        _gaps(state, gaps).append("牌库顶：positionFromTop=%r 不是整数 ⇒ 按牌顶（0）" % (pos,))
        pos = 0
    deck = state.deck
    idx = max(0, min(int(pos), len(deck)))
    deck.insert(idx, cid)
    _dict(state, "deck_cards")[cid] = h
    return idx


def spawn_in_hand(state, name: str, hold: Optional[float] = None, gaps: Optional[list] = None) -> Optional[int]:
    """`SpawnCardinHandbySide` 的我方分支：手里多一张名为 `name` 的新牌 → 新牌 id（模拟里的负 id，与匿名抽牌同一段 `-(1000+n)`）。

    **不碰牌库**。手牌已满（`HAND_CAP`）⇒ 不加并记缺口（满手时 `CreateCard` 的处理没读过，不猜）。
    原版新牌进手后还要 `ExecuteOnSpawnedInHandEvents`（旁观者/自己的"进手牌时"钩子）⇒ 未建模，调用方记缺口。"""
    hand = state.hand
    if len(hand) >= HAND_CAP:
        _gaps(state, gaps).append("SpawnCardinHandbySide：手牌已满（%d）⇒ 新牌的去向（CreateCard 满手分支）未建模，不加" % HAND_CAP)
        return None
    state.cap_used = int(getattr(state, "cap_used", 0) or 0) + 1
    cid = -(1000 + state.cap_used)
    h = make_card(state, cid, name, _hold(state, hold))
    hand[cid] = h
    _dict(state, "card_templates")[cid] = h
    return cid


def pull_specific_to_top(state, cid, gaps: Optional[list] = None) -> Optional[bool]:
    """`DrawSpecificCardFromDeckBySide(instigatorID, cardID, side, cardSeen)` 的前半（`BP_CardFunctions.cpp:6308`）：
    牌库里**有**这张 ⇒ `RemoveCardFromDeckBySide` + `AddCardToDeckBySide(side, id, true, -1)`（放到牌顶）→ 随后 `DrawTopCardFromDeck`（调用方走抽牌链抽 1 张）；
    **没有** ⇒ 什么都不做（原版 `Array_Contains` 假就 `return`）。
    → True = 已挪到牌顶、该抽一张；False = 不在牌库、不抽；None = 牌库顺序未知（调用方按匿名抽牌，记缺口）。"""
    if not getattr(state, "deck_known", False):
        _gaps(state, gaps).append("DrawSpecificCardFromDeckBySide：牌库顺序未知 ⇒ 抽哪张不可知，按匿名抽牌")
        return None
    deck = state.deck
    if cid not in deck:
        return False
    deck.remove(cid)
    deck.insert(0, cid)
    return True
