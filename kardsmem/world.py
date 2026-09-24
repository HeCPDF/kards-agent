#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.world —— 从 GWorld 走到 UWorld / GameState / Board / PlayerController。

唯一可靠的类身份判据是 **`UStruct::PropertiesSize`**（沿 `SuperStruct` 走）：
`.idmap` 里的 `*_VFT` 名字不能用来比实例 vptr（UWorld 实例会落在
`UVirtualTextureBuilder_VFT` 上，旧会话就这样踩过）。

链：
    UWorld    = *(base + GWorld)              PropertiesSize == 2536
    GameState = *(UWorld + 0x1B0)             1960 = ABP_GameState_Battle_C（对战中）
                                              912  = AkardsGameState（不在对局）
    Actors    = *(UWorld + 0x30) -> ULevel     TArray<AActor*> @ ULevel+0xA0
    PC        = *(UWorld + 0x228) -> GI -> LocalPlayers[0] -> ULocalPlayer+0x30
"""

from __future__ import annotations

from typing import Optional

from . import build as B


class Locator:
    """一个进程上的一串"对象定位"操作。不做缓存假设：每次调用都重读指针链。"""

    def __init__(self, mem, base: int):
        self.m = mem
        self.base = base
        self._actor_cache = None      # (taken_at, list)
        self._actor_cache_age = 0.0

    # -- 类身份 ----------------------------------------------------------
    def uclass_of(self, obj: int) -> int:
        return self.m.ptr(obj + B.OFF_UOBJECT_CLASS) or 0 if obj else 0

    def propsize(self, obj: int) -> Optional[int]:
        """对象的类大小（沿 UClass+0x58 读）。"""
        if not obj:
            return None
        cls = self.uclass_of(obj)
        return self.m.i32(cls + B.OFF_UCLASS_PROPSIZE) if cls else None

    def class_propsize(self, uclass: int) -> Optional[int]:
        return self.m.i32(uclass + B.OFF_UCLASS_PROPSIZE) if uclass else None

    def derives_from(self, obj: int, size: int, max_depth: int = 16,
                     cache: Optional[dict] = None) -> bool:
        """对象是不是某个 PropertiesSize == size 的类（或其子类）的实例。"""
        cls = self.uclass_of(obj)
        return self.class_derives_from(cls, size, max_depth, cache)

    def class_derives_from(self, uclass: int, size: int, max_depth: int = 16,
                           cache: Optional[dict] = None) -> bool:
        if not uclass:
            return False
        if cache is not None and uclass in cache:
            return cache[uclass]
        ok, cls, seen = False, uclass, 0
        while cls and seen < max_depth:
            if self.class_propsize(cls) == size:
                ok = True
                break
            nxt = self.m.ptr(cls + B.OFF_UCLASS_SUPER) or 0
            if not nxt or nxt == cls:
                break
            cls = nxt
            seen += 1
        if cache is not None:
            cache[uclass] = ok
        return ok

    def is_cdo(self, obj: int) -> bool:
        fl = self.m.u32(obj + B.OFF_UOBJECT_FLAGS)
        return bool(fl is not None and (fl & B.FLAG_RF_CDO))

    # -- 指针链 ----------------------------------------------------------
    @property
    def world(self) -> int:
        return self.m.ptr(self.base + B.RVA["GWorld"]) or 0

    def is_uword(self, obj: int) -> bool:
        return bool(obj) and self.propsize(obj) == B.SIZE_UWORLD

    @property
    def gamestate(self) -> int:
        w = self.world
        return (self.m.ptr(w + B.OFF_UWORLD_GAMESTATE) or 0) if w else 0

    @property
    def gamestate_size(self) -> Optional[int]:
        gs = self.gamestate
        return self.propsize(gs) if gs else None

    @property
    def in_battle(self) -> bool:
        """战斗 GameState 类已加载。**主菜单也为真** —— 不是「在对局中」。

        判「真的在一局里」用 gs.GameState.match_active。
        """
        return self.gamestate_size == B.SIZE_BP_GAMESTATE_BATTLE

    def gamestate_kind(self) -> str:
        sz = self.gamestate_size
        return {B.SIZE_BP_GAMESTATE_BATTLE: "ABP_GameState_Battle_C",
                B.SIZE_AKARDS_GAMESTATE: "AkardsGameState"}.get(sz, "unknown(%s)" % sz)

    @property
    def persistent_level(self) -> int:
        w = self.world
        return (self.m.ptr(w + B.OFF_UWORLD_PERSISTENT_LEVEL) or 0) if w else 0

    # -- Level Actors ----------------------------------------------------
    def actors(self, refresh: bool = True) -> list:
        """`ULevel::Actors`（TArray<AActor*>），跳过空指针。"""
        lvl = self.persistent_level
        if not lvl:
            return []
        arr = self.m.ptr(lvl + B.OFF_LEVEL_ACTORS) or 0
        num = self.m.i32(lvl + B.OFF_LEVEL_ACTORS + 8)
        if not arr or not num or num < 0 or num > 20000:
            return []
        out = []
        for i in range(num):
            a = self.m.ptr(arr + i * 8)
            if a:
                out.append(a)
        return out

    def actor_size_histogram(self) -> dict:
        hist = {}
        for a in self.actors():
            s = self.propsize(a)
            hist[s] = hist.get(s, 0) + 1
        return hist

    def find_actors(self, sizes, max_depth: int = 40) -> list:
        """按类大小（含子类）找 actor。`sizes` 可以是 int 或集合。

        Board / PlayerController / 屏幕卡 actor 都是这样找出来的 ——
        不依赖名字，也不需要 GObjects 全量遍历。
        """
        want = {sizes} if isinstance(sizes, int) else set(sizes)
        cache, out = {}, []
        for a in self.actors():
            for s in want:
                if self.derives_from(a, s, max_depth=max_depth, cache=cache):
                    out.append(a)
                    break
        return out

    def find_actor(self, sizes) -> int:
        hits = self.find_actors(sizes)
        return hits[0] if hits else 0

    # -- PlayerController ------------------------------------------------
    @property
    def game_instance(self) -> int:
        w = self.world
        return (self.m.ptr(w + B.OFF_UWORLD_GAME_INSTANCE) or 0) if w else 0

    def local_players(self) -> list:
        gi = self.game_instance
        if not gi:
            return []
        arr = self.m.ptr(gi + B.OFF_GI_LOCALPLAYERS) or 0
        num = self.m.i32(gi + B.OFF_GI_LOCALPLAYERS + 8)
        if not arr or not num or num < 0 or num > 8:
            return []
        return [p for p in (self.m.ptr(arr + i * 8) for i in range(num)) if p]

    @property
    def player_controller(self) -> int:
        lps = self.local_players()
        if not lps:
            return 0
        return self.m.ptr(lps[0] + B.OFF_LOCALPLAYER_PC) or 0

    # -- 摘要 ------------------------------------------------------------
    def describe(self) -> dict:
        w = self.world
        gs = self.gamestate
        return {
            "base": hex(self.base),
            "u_world": hex(w) if w else None,
            "u_world_propsize": self.propsize(w) if w else None,
            "gamestate": hex(gs) if gs else None,
            "gamestate_kind": self.gamestate_kind() if gs else None,
            "in_battle": self.in_battle,
            "persistent_level": hex(self.persistent_level) if self.persistent_level else None,
            "level_actors": len(self.actors()),
            "player_controller": hex(self.player_controller) if self.player_controller else None,
        }
