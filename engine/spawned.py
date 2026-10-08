#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.spawned —— **脚本里刚生成的牌**的登记表（2026-10-06，用户："直跑认不出刚生成的牌是 bug"）。

问题
====
卡牌脚本常这样写（RALLY CALEDONIA / HELL ON WHEELS / CARRIER BATTLE / IRON VICTORY）：

    cardFunction->Spawn…(…, &spawnedCardID);                 // 出参：新牌的 card id
    cardFunction->GetCardFromID(spawnedCardID, &card);       // BP_CardFunctions.cpp:377
        → GameStateRef->FetchCardFromCardID(id, &card)       // BP_GameState_Battle.cpp:1537：id <= 0 ⇒ nullptr；
                                                             //   否则在 `AllCardsInBattle`（TMap<int32, 卡指针>）里查
    cardFunction->ChangeAttack(card, …) / ChangeKreditCost(card, …) / JSON_SetInt(card, …) / card->cardID …

新牌只存在于**模拟**里（负 id 的 `U`/`H`），游戏内存里的 `AllCardsInBattle` 没有它 ⇒ `FetchCardFromCardID` 回 null ⇒
后面所有对 `card` 的调用要么在 `LetBool` 里 TypeError（出参没写 ⇒ None）、要么"目标指针换不出场上的单位"。

做法
====
给每张新牌发一个**合成卡句柄**（落在进程地址空间之外的保留段；与真指针不会撞）。三处用它：

  1. `FetchCardFromCardID(id)` 是**软钩子**（`kismetlib.FALLTHROUGH`）：id 在本表里 ⇒ 返回句柄；不在 ⇒ 交还给游戏自己的字节码；
  2. `ptr_ids[句柄] = 新牌 id` ⇒ 所有"指针 → card_id"的 sink（`ctx.unit_of` / `hand_card_of`）与 `to_effects`（字典路）原样认得；
  3. 卡对象的**读**（`getTotalAttack` / `getHasGameplayTag` / `->cardID` / `->side` …）由 `engine.effectvm.record_effects`
     把句柄的视图/字段接到"该牌的静态卡 + 模拟里的当前值"上。

本模块只管**登记**（纯数据，不认识 kardsmem / sim）；视图与字段的拼装在 `effectvm`。
录制路（字典路 A）与直跑路（B）**各自一份登记表**（`Recorder.spawned` / `DirectCtx.spawned`，`_direct_hooks` 里并成同一份）。
"""
from __future__ import annotations

from typing import Optional

__all__ = ["SpawnedCards", "HANDLE_BASE"]

#: 句柄起点。64 位用户态地址上限是 0x7FFF_FFFF_FFFF；这一段在游戏进程实际用到的堆/映像区之外（转储里也没有），
#: 离线对账的假地址覆盖层用的是 0x7E0000000000 起（`reconcile_batch.DumpMem.FAKE_BASE`），故取更低的一段错开。
HANDLE_BASE = 0x7B0000000000
HANDLE_STRIDE = 0x1000


class SpawnedCards:
    """`{card_id: 信息}` / `{句柄: 信息}` 登记表。信息 = `{"id","handle","name","side","where","row"}`：

    * `where`：`"board"`（场上；`row` = `"frontline"`/`"back"`）/ `"hand"`（我方手牌）/ `"opp_hand"`（对方手牌，模拟里只有张数）；
    * `side`：座位（`ESide` 的整数 1/2，读不出 ⇒ None）。
    """

    def __init__(self):
        self.by_id: dict = {}
        self.by_handle: dict = {}
        self._n = 0
        self._opp_seq = 0

    def register(self, cid, name, side, where: str, row: Optional[str] = None) -> int:
        """登记一张新牌 → 句柄。同一个 id 再登记 ⇒ 沿用旧句柄并更新信息。"""
        old = self.by_id.get(cid)
        if old is not None:
            old.update(name=name, side=side, where=where, row=row)
            return old["handle"]
        self._n += 1
        h = HANDLE_BASE + self._n * HANDLE_STRIDE
        info = {"id": cid, "handle": h, "name": name, "side": side, "where": where, "row": row}
        self.by_id[cid] = info
        self.by_handle[h] = info
        return h

    def next_opp_id(self) -> int:
        """给"进了对方手牌"的新牌发一个 id（对方手牌在模拟里没有逐张的牌，只有张数 ⇒ 这个 id 只用来认句柄）。"""
        self._opp_seq += 1
        return -(5000 + self._opp_seq)

    def handle_of(self, cid) -> Optional[int]:
        info = self.by_id.get(cid) if isinstance(cid, int) and not isinstance(cid, bool) else None
        return None if info is None else info["handle"]

    def info_of_handle(self, handle) -> Optional[dict]:
        try:
            return self.by_handle.get(handle)
        except TypeError:                                    # 不可哈希（列表等）
            return None

    def info_of_id(self, cid) -> Optional[dict]:
        try:
            return self.by_id.get(cid)
        except TypeError:
            return None
