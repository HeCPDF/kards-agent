# -*- coding: utf-8 -*-
"""engine.natives.convert —— `ConvertCard` 的状态变更本体（直跑 sink 与重放表**共用**，规则只写一处）。

原版：`BP_CardFunctions::ConvertCard`（BP_CardFunctions.cpp:12611）——旧牌离场（不算摧毁）→ 同位置同序号 `CreateCard(目标名)`。
本模块只做**状态**那一段（出厂数值换牌）；钩子扇出（`convert_old`/`convert_new`/0x22）与 `applied` 记账是调用方的事。
对应关系同 `sim.engine._apply_convert`（字典路）：在场 ⇒ 同侧同行造新牌（`sick=True`，前线去 `smokescreen`）；手牌/牌库 ⇒ 同位置换
新模板（效果留空并记缺口）。
"""
from __future__ import annotations

from typing import Callable, Optional

__all__ = ["convert_one", "convert_args", "convert_from_args"]


def convert_one(state, old, cv: dict, on_old: Optional[Callable] = None):
    """把 `old` 这张牌按载荷 `cv`（`effectvm._convert_payload` 的形状：`atk/dfn/cost/opc/armor/typ/kw/name`）转成新牌。

    → `(kind, nid, gap)`：
      * `kind`：`"board"` / `"hand"` / `"deck"`；`None` ⇒ 既不在场也不在我方手牌/牌库（调用方记 `gap`，没有状态变更）；
      * `nid`：新牌的临时 id（`-(5000 + tmp_seq)`；`tmp_seq` 在这里自增 ⇒ 重放与直跑从同一状态起会得到同一个 id）；
      * `gap`：该写进缺口表的说明（可能为 None）。
    `on_old(uid)`：在场分支里**旧牌离场之前**调用一次（直跑用它扇 `convert_old`，那时旧牌还在场）。
    """
    from engine.state import H, U                                          # noqa: PLC0415
    name = cv.get("name") or "?"
    units = getattr(state, "units", {}) or {}
    hand = getattr(state, "hand", {}) or {}
    deck = getattr(state, "deck", None)
    u = units.get(old)
    if u is not None:                                                      # ① 在场
        if on_old is not None:
            on_old(old)
        state.tmp_seq = int(getattr(state, "tmp_seq", 0) or 0) + 1
        nid = -(5000 + state.tmp_seq)
        nu = U(nid, u.side, u.row, cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("cost") or 0,
               cv.get("typ") or u.typ, cv.get("kw") or (), opc=cv.get("opc") or 1,
               sick=True, armor=cv.get("armor") or 0, mdef=cv.get("dfn") or 0)
        if nu.row == "frontline":
            nu.kw = set(nu.kw or ()) - {"smokescreen"}
        units.pop(old, None)
        units[nid] = nu
        (getattr(state, "card_templates", {}) or {}).pop(old, None)
        return "board", nid, None
    if old in hand:                                                        # ② 手牌
        h = hand.pop(old)
        state.tmp_seq = int(getattr(state, "tmp_seq", 0) or 0) + 1
        nid = -(5000 + state.tmp_seq)
        hand[nid] = H(nid, name, cv.get("cost") or 0, cv.get("typ") or h.typ,
                      cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("kw") or (), {})
        return "hand", nid, ("convert：手牌里的转化牌 %s 的效果未知（出厂数值已换、eff 留空）；"
                             "ExecuteOnSpawnedInHandEvents 的后果未建模" % name)
    if isinstance(deck, list) and old in deck:                             # ③ 牌库（同下标换）
        i = deck.index(old)
        dc = getattr(state, "deck_cards", None)
        if not isinstance(dc, dict):
            dc = state.deck_cards = {}
        old_h = dc.pop(old, None)
        state.tmp_seq = int(getattr(state, "tmp_seq", 0) or 0) + 1
        nid = -(5000 + state.tmp_seq)
        tpl = H(nid, name, cv.get("cost") or 0, cv.get("typ") or (old_h.typ if old_h else "infantry"),
                cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("kw") or (), {})
        deck[i] = nid
        dc[nid] = tpl
        ct = getattr(state, "card_templates", None)
        if not isinstance(ct, dict):
            ct = state.card_templates = {}
        ct[nid] = tpl
        return "deck", nid, ("convert：牌库里转化出的新牌 %s 的效果未知（eff 留空）；"
                             "ExecuteOnAfterDeckChanged(0x3) 的后果未建模" % name)
    return None, None, "convert：%r 既不在场、也不在我方手牌/牌库（未建模）" % (old,)


def convert_args(cv: dict) -> tuple:
    """载荷 → `applied` 条目里带的尾部元组（`name, atk, dfn, cost, opc, armor, typ, kw`）：重放只靠它还原 `cv`。"""
    return (cv.get("name") or "?", cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("cost") or 0, cv.get("opc") or 0,
            cv.get("armor") or 0, cv.get("typ"), tuple(cv.get("kw") or ()))


def convert_from_args(name, atk, dfn, cost, opc, armor, typ, kw) -> dict:
    """`convert_args` 的逆。"""
    return {"name": name, "atk": atk, "dfn": dfn, "cost": cost, "opc": opc, "armor": armor, "typ": typ, "kw": tuple(kw or ())}
