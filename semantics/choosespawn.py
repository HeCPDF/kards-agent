# -*- coding: utf-8 -*-
"""`selectCardToDraw` 三选一一族（CRUISER SCOUTS / SOUL OF OLD JAPAN / 好人寥寥 …）的候选预测。

总纲 `ARCHITECTURE.md` §1/§4：依赖随机流的东西由 `sim` 层按种子先算成**确定的**结果，评估层不见随机。
本模块就是这一步：给一张带 `GetChooseSpawnCards` 的牌 + 活种子 ⇒ 屏幕上那 3 张候选（内部名）。

做法（不手写各卡的过滤，全部跑卡自己的字节码）：
  1. 在 VM 里跑这张牌的 `GetChooseSpawnCards`（出参 `cards` / `markAsSeen` / `keepOrder`）。
     它里面的两个原生由本模块按只读数据喂：
       * `IsCardReserved(name)`：官方 API 本地缓存里没有这张牌的英文标题 ⇒ 视为"进了预备"；
       * `GetAllActiveStaticCards(includeNotAttainable, includeReserved)`：静态卡表（保持表顺序）∩ 卡集允许集
         （`KEEP_SETS`，见 `tools/choose_spawn_predict.py` 的 switch 表）∩（没进预备，除非 includeReserved）。
  2. `keepOrder=false`：游戏随后 `Array_ShuffleFromStream(cards, cardsRandomStream)`（正向 Fisher-Yates，N 次抽取）取前 3
     ⇒ 用活种子的 `Stream` 副本洗；`keepOrder=true`：卡自己已在字节码里按流抽好，取前 3。
  3. 读不出种子 / API 缓存缺失 / VM 停了 ⇒ 如实返回 `stopped`（调用方记缺口、不编候选）。

验证：`_nn_scratch/choose_spawn_predict.py --selftest` 的三次实机观测（含一次盲预测命中）；本模块的离线测试
`test_choosespawn.py` 用同样的观测回放洗牌这一半，VM 那一半需要游戏在跑（实机见 TODO A8）。
"""
from __future__ import annotations

import json
import os
import struct
from typing import Optional

# 卡集允许集（`GetAllActiveStaticCards(includeNotAttainable=false)` 的 switch：排除 Special(2)/OnlySpawnable(3)/
# Candidate(4)/Placeholder(5)/Expansion1(6)/Expansion2(7)、NotAvailable(0)、Wildcards(11)、Core(14)、Reserved(22) 及更高位）
KEEP_SETS = frozenset({1, 8, 9, 10, 12, 13, 15, 16, 17, 18, 19, 20, 21})
OFF_FACTION, OFF_TYPE, OFF_RARITY, OFF_CARDSET = 0x7C, 0x68, 0x11C, 0x130
from base import paths as _paths  # noqa: E402

API_CACHE = _paths.API_CACHE


_LEGAL_MEMO: dict = {}


def legal_titles(path: str = API_CACHE) -> Optional[frozenset]:
    """官方 API 当前卡表（= 没进预备）的英文标题（大写）集合；缓存读不出 ⇒ None。

    按 (路径, mtime_ns, 大小) 记住解析结果：这个 json 有两千多行、每次 `_play_hooks` 都整份重解析（~70 ms/次）。
    文件一变（API 缓存刷新）键就变，立刻重读；读不出（None）不记。"""
    try:
        stt = os.stat(path)
        key = (path, stt.st_mtime_ns, stt.st_size)
    except OSError:
        key = None
    if key is not None and key in _LEGAL_MEMO:
        return _LEGAL_MEMO[key]
    out = _legal_titles_uncached(path)
    if key is not None and out is not None:
        _LEGAL_MEMO.clear()
        _LEGAL_MEMO[key] = out
    return out


def _legal_titles_uncached(path: str) -> Optional[frozenset]:
    try:
        out = set()
        for row in json.load(open(path, encoding="utf-8")):
            j = row.get("json")
            j = json.loads(j) if isinstance(j, str) else j
            ti = (j or {}).get("title")
            en = (ti.get("en-EN") if isinstance(ti, dict) else ti) or ""
            if en:
                out.add(en.upper())
        return frozenset(out) or None
    except Exception:                                             # noqa: BLE001
        return None


def static_cards(km) -> list:
    """静态卡表按表顺序的 `[(ptr, 内部名, 英文标题大写, cardSet)]`。"""
    from kardsmem.gs import load as load_gs
    from kardsmem.names import OFF_CARD_TITLE, ftext_at
    gs = load_gs(km)
    t = gs.static_card_table()
    blob = km.m.read_exact(t["ptr"], (t["num"] or 0) * 8)
    pool = km.names_pool()
    out = []
    for i in range(0, len(blob or b""), 8):
        p = struct.unpack_from("<Q", blob, i)[0]
        n = pool.fname_of(p)
        if not n:
            continue
        out.append((p, n.split("_C_")[0], (ftext_at(km.m, p, OFF_CARD_TITLE) or "").upper(), km.m.u8(p + OFF_CARDSET)))
    return out


def shuffle_pick(seed: int, n: int, k: int = 3) -> tuple:
    """`Array_ShuffleFromStream` 洗 n 张后取前 k 个下标 → `(下标列表, 抽取数)`。"""
    from kardsmem.rng import Stream
    st = Stream(seed)
    order = st.shuffle(list(range(n)))
    return order[:k], st.draws


def pool_hooks(km, card_ptr: int = 0, legal: Optional[frozenset] = None, table: Optional[list] = None) -> dict:
    """卡池类原生的只读钩子 `{"IsCardReserved": …, "GetAllActiveStaticCards": …}`（VM 没有它们的字节码：它们落到
    `BP_Logic → UtilityFunctions.isCardReserved → GetDSession().cards_reserve_changes`，离线/空会话里 `GetDSession()` 是空 ⇒ VM 在
    `@0x53 Let: 在空对象上读 cards_reserve_changes` 停）。凡是从"全部可用卡"里随机取牌的指令（ATLANTIC CONVOY / RED BANNER /
    URAL FACTORIES …）都要它们。原版：`GetAllActiveStaticCards`（BP_CardFunctions.cpp:~3990-4050，卡集 switch + `NotifyCheckCardReserved`@4032）、
    `IsCardReserved`（BP_CardFunctions.cpp:23027）。预备判据 = 官方 API 本地缓存没有这张牌的英文标题；缓存/静态卡表读不出 ⇒ `{}`
    （不编池子，VM 照旧停在原语上并如实记缺口）。静态卡表按 km 缓存（整表扫描不便宜）。"""
    from semantics import effectvm as EV
    legal = legal if legal is not None else legal_titles()
    if legal is None:
        return {}
    if table is None:
        table = km.__dict__.get("_static_cards_cache") if hasattr(km, "__dict__") else None
        if table is None:
            try:
                table = static_cards(km)
            except Exception:                                     # noqa: BLE001
                return {}
            try:
                km._static_cards_cache = table
            except Exception:                                     # noqa: BLE001
                pass
    by_ptr = {p: (n, ti, cs) for p, n, ti, cs in table}
    by_name = {n: ti for _p, n, ti, _cs in table}
    title_of_self = by_ptr.get(card_ptr, (None, None, None))[1]

    def hk_reserved(vm, frame, obj, args, e):
        nm = args[0] if args else None
        ti = by_name.get(str(nm).split("_C_")[0]) if nm is not None else title_of_self
        val = bool(ti) and ti not in legal
        EV._generic_out(frame, e, val)
        return val

    def hk_active(vm, frame, obj, args, e):
        include_na = bool(args[0]) if args else False
        include_res = bool(args[1]) if len(args) > 1 else False
        res = []
        for p, n, ti, cs in table:
            if not n.startswith("card_"):
                continue
            if not include_na and cs not in KEEP_SETS:
                continue
            if not include_res and ti not in legal:
                continue
            res.append(p)
        EV._generic_out(frame, e, res)
        return res
    return {"IsCardReserved": hk_reserved, "GetAllActiveStaticCards": hk_active}


def predict(km, card_ptr: int, seed: Optional[int], st=None, legal: Optional[frozenset] = None,
            timeout_s: float = 8.0) -> dict:
    """→ `{"names": [内部名×≤3], "pool": N, "keep_order": bool, "draws": n, "stopped": None|原因}`。"""
    out = {"names": [], "pool": 0, "keep_order": None, "draws": 0, "stopped": None}
    if seed is None:
        out["stopped"] = "读不到随机种子"
        return out
    legal = legal if legal is not None else legal_titles()
    if legal is None:
        out["stopped"] = "官方 API 本地缓存读不出（kards-data/api/kards_api_cards.json），池子的“没进预备”无法判定"
        return out
    from semantics import effectvm as EV
    try:
        table = static_cards(km)
    except Exception as ex:                                       # noqa: BLE001
        out["stopped"] = "静态卡表读不出：%s: %s" % (type(ex).__name__, ex)
        return out
    by_ptr = {p: (n, ti, cs) for p, n, ti, cs in table}
    pool = pool_hooks(km, card_ptr, legal=legal, table=table)

    hooks = {}
    if st is not None:
        hooks.update(EV.make_read_hooks(st, getattr(st, "my_side_raw", None)))
    hooks.update(pool)
    r = EV.record_effects(km, card_ptr, 0, False, hook="GetChooseSpawnCards",
                          my_side=getattr(st, "my_side_raw", None), read_hooks=hooks,
                          slots=dict(getattr(st, "slots", None) or {}), rng_seed=seed, timeout_s=timeout_s,
                          board=st, my_seat=getattr(st, "my_side_raw", None))
    o = {str(k).lower(): v for k, v in (r.get("out") or {}).items()}      # 出参名大小写按 BP 声明（`Cards`/`keepOrder`）
    cards = o.get("cards")
    if r.get("stopped") or not isinstance(cards, (list, tuple)):
        out["stopped"] = str(r.get("stopped") or "GetChooseSpawnCards 没给出 cards")[:200]
        return out
    keep = bool(o.get("keeporder"))
    out["keep_order"], out["pool"] = keep, len(cards)
    ptrs = [c for c in cards if isinstance(c, int) and c in by_ptr]
    if keep:
        pick = list(range(min(3, len(ptrs))))
        out["draws"] = int(r.get("draws") or 0)
    else:
        pick, out["draws"] = shuffle_pick(seed, len(ptrs))
    out["names"] = [by_ptr[ptrs[i]][0] for i in pick]
    return out
