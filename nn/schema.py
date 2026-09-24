#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.schema —— 样本 schema + **可见性投影**（录制与训练的唯一契约）。

见 `reverse-data/reports/spec/KARDS-NN.md` §1.3（样本 schema）/ §2（可见性）。
投影规则只在这一处实现，`agent/record.py` 写样本时调用它，别在别处再抄一份。

★ 已拍板（§2.2）：
    - 敌方手牌：只给张数，不给内容（P1）。
    - 双方牌库剩余的**顺序/身份**：不给（P2/P3，真隐藏，玩家自己也不知道）。
    - 但**己方卡组名单**（这 30 张是什么）不算隐藏信息——玩家开局就知道，
      给它是"策略空间"的必要条件（§3.4），只是不给"剩余顺序"。
    - 未揭示的隐蔽/背面牌身份：不给，除非 `is_revealed==1`（P4）。
    - `Hidden*` 命名的字段（如 `ReconnectHiddenEnemyKreditsByTurnNumber`）：不用（P6）。

★ 已知未解决的口子（如实标注，不假装修好）：
    - P5：`kardsmem.pick.choose_candidates()` 扫 `BP_ChooseCardToSpawn_C` 时
      **不按 owner 过滤**，理论上可能把对手的三选一候选也读出来——本模块
      目前不做二次过滤（没有可靠的 owner 字段可用），录制器要留意这条。
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

LOCAL, ENEMY = "local", "enemy"

PHASES = ("mulligan", "main", "pick", "forecast", "hand_target",
          "deploy_target", "deck_pick")


# ---------------------------------------------------------------------------
# 可见性投影
# ---------------------------------------------------------------------------
def _card_public_view(c, viewer: str, full_hand_of_viewer: bool) -> Optional[dict]:
    """一张卡该给查看者（`viewer`）看到什么。返回 `None` 表示"这张卡整体不给"
    （目前没有需要整条隐藏的情形，占位用）。
    """
    is_mine = c.side == viewer
    loc = c.location

    # P4：未揭示的隐蔽/背面牌——不给身份，除非 is_revealed
    hidden_identity = ("has_covert" in (c.keywords or [])) and not c.is_revealed

    # P1/P7：对方手牌只给"这里有一张"，不给内容
    if loc == "hand" and not is_mine:
        return {"side": c.side, "location": "hand", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P1_enemy_hand"}

    # P2/P3：双方牌库剩余——不给身份/顺序；但己方"卡组名单"走另一条口（见下）
    if loc == "deck":
        return {"side": c.side, "location": "deck", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P2_deck_remaining"}

    if hidden_identity:
        return {"side": c.side, "location": loc, "uid": c.uid, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P4_covert"}

    # 其余：可见（场上/总部/弃牌/己方手牌）——P8/P9/P10
    return {
        "side": c.side, "location": loc, "uid": c.uid, "card_id": c.card_id,
        "name": c.name, "attack": c.attack, "defense": c.defense,
        "kredit_cost": c.kredit_cost, "slot": c.slot,
        "is_revealed": c.is_revealed,
        "hidden": False,
    }


def project_state(st, viewer: str, deck_roster: Optional[dict] = None) -> dict:
    """把一份 `BoardState` 投影成"`viewer` 这一侧真人玩家看得到的东西"。

    `deck_roster`：`{"local": [fname, ...], "enemy": [...]}` —— 己方卡组名单
    （多重集合，不含顺序）。这不是从 `st` 里推出来的（§2.2：牌库剩余身份不给，
    但开局卡组名单不算隐藏信息），要由调用方在录制**开局**时另外提供一次
    （比如从换牌前的完整起始牌库读一次，只做这一次，别每个决策点都重读）。
    """
    cards = []
    for c in st.cards:
        v = _card_public_view(c, viewer, full_hand_of_viewer=True)
        if v is not None:
            cards.append(v)

    hand_counts = {side: len(st.hand(side)) for side in (LOCAL, ENEMY)}
    deck_counts = {}
    for side in (LOCAL, ENEMY):
        deck_counts[side] = sum(1 for c in st.cards if c.side == side and c.location == "deck")

    out = {
        "viewer": viewer,
        "turn": st.turn,
        "hand_count": hand_counts,     # P1/P7：对方只给数量；己方数量也顺带给，卡本身在 cards 里已有内容
        "deck_count": deck_counts,     # P2/P3：双方都只给数量（顺序/身份不给）
        "kredits": dict(getattr(st, "kredits", None) or {}),
        "cards": cards,
    }
    if deck_roster is not None:
        out["deck_roster"] = {LOCAL: sorted(deck_roster.get(LOCAL) or [])}
        # ★ 敌方卡组名单不给（§3.4："敌方卡组：不给名单，只给已见"）——
        #   即使调用方传了 enemy 的名单，这里也不透出去，双重保险。
    return out


def state_hash(projected: dict) -> str:
    """同局面去重用。只依赖已投影后的字典，不依赖字典 key 顺序。"""
    blob = json.dumps(projected, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 样本 schema（纯文档 + 一个构造 helper，不做校验框架，先跑起来再说）
# ---------------------------------------------------------------------------
def make_sample(*, game, patch, seat, turn, t, phase, state, candidates,
                 verdict, label, label_src, label_available, raw,
                 receipt, events, state_after_hash=None) -> dict:
    """按 KARDS-NN.md §1.3 的字段拼一条样本。纯字典拼装，字段名和顺序
    以规格为准，改字段名要两边一起改（`agent/record.py` 也读这份定义）。
    """
    s = {
        "game": game, "patch": patch, "seat": seat, "turn": turn, "t": t,
        "phase": phase, "state": state, "state_hash": state_hash(state),
        "candidates": candidates, "verdict": verdict,
        "label": label, "label_src": label_src, "label_available": label_available,
        "raw": raw, "receipt": receipt, "events": events,
    }
    if state_after_hash is not None:
        s["state_after_hash"] = state_after_hash
    return s
