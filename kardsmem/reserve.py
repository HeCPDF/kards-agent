# -*- coding: utf-8 -*-
"""卡牌"是否进了预备池"——**数据全部来自游戏自己**（只读内存），不看官网导出的 json。

游戏的判据（`UtilityFunctions::isCardReserved`，1.60 BP 导出 `Library/UtilityFunctions.cpp:6394`，已逐行读过）：
    for change in DSession.cards_reserve_changes:                # AKardsSession+0xBE0，TArray<FCardsReservePoolChanges>
        if card ∈ change.to_be_reserved ∪ change.coming_back:
            if change.activation_date <= 服务器 UTC 现在  and  change.activation_date >= latest:
                latest = change.activation_date
                moved, back = (card ∈ to_be_reserved), (card ∉ to_be_reserved)      # 后生效的覆盖先生效的
    if moved or back: return moved
    return StaticCard(card).isReserved                           # UBaseCardObject+0x132，静态卡自带的默认值
（`moved/back/latest` 是函数局部变量，每次调用重新从默认值开始；已按"函数库没有成员变量"判断，见 `docs-local`）。
服务器 UTC = `server_time_on_logon(+0x750) + RealTimeSeconds`；外部读不到 RealTimeSeconds，这里用本机 UTC 近似
（只有"预备名单恰好在这几秒内生效"时才会差一回合，且对局里这个名单不会变）。

布局：`FCardsReservePoolChanges` = { FDateTime activation_date @0; TSet<FName> to_be_reserved @8 (0x50);
TSet<FName> coming_back @0x58 (0x50) }，元素步长 0xA8；`TSet<FName>` 的元素 = { FName(8) ; HashNextId(4) ; HashIndex(4) } 16 字节。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Set

OFF_CHANGES = 0xBE0               # AKardsSession::cards_reserve_changes
CHANGE_STRIDE = 0xA8
OFF_TO_BE_RESERVED = 0x08
OFF_COMING_BACK = 0x58
ELEM_STRIDE_FNAME = 16
OFF_STATIC_IS_RESERVED = 0x132    # UBaseCardObject::isReserved
SESSION_CLASSES = ("BP_KardsSession_C", "KardsSession", "AKardsSession")
_TICKS_EPOCH_1970 = 621355968000000000     # FDateTime 以 100 ns 为单位、从 0001-01-01 起算
_CACHE: dict = {}
RECHECK_S = 5.0


def utc_ticks(now: Optional[float] = None) -> int:
    """本机 UTC → FDateTime ticks。"""
    t = time.time() if now is None else now
    return _TICKS_EPOCH_1970 + int(t * 10_000_000)


def is_reserved(name: str, static_flag: bool, changes: List[dict], now_ticks: int) -> bool:
    """游戏 `isCardReserved` 的逐行复刻（纯函数）。`changes` = `[{"activation": ticks, "to_be_reserved": set, "coming_back": set}]`，
    按数组顺序。`name` 是内部名（不带 `_C`）。"""
    latest = 0
    moved = back = False
    for ch in changes:
        in_to, in_back = name in ch["to_be_reserved"], name in ch["coming_back"]
        if not (in_to or in_back):
            continue
        if ch["activation"] <= now_ticks and ch["activation"] >= latest:
            latest = ch["activation"]
            moved, back = in_to, not in_to
    if moved or back:
        return moved
    return bool(static_flag)


def _session_ptr(km) -> int:
    from kardsmem.objects import ObjectArray
    pid = getattr(km, "pid", None)
    hit = _CACHE.get(pid)
    if hit and time.time() - hit[1] < RECHECK_S and hit[0]:
        return hit[0]
    oa = ObjectArray(km)
    cands = []
    try:
        from .world import Locator
        for a in Locator(km.m, km.base).actors():
            try:
                if oa.class_name(a) in SESSION_CLASSES:
                    cands.append(a)
            except Exception:                                      # noqa: BLE001
                continue
    except Exception:                                              # noqa: BLE001
        cands = []
    if not cands:                                                  # 不在关卡 actor 里：退回全量 GObjects（慢，只做一次）
        for cls in SESSION_CLASSES:
            cands = [o for o in (oa.find_by_class_name(cls) or []) if not (oa.obj_name(o) or "").startswith("Default__")]
            if cands:
                break
    best = max(cands, key=lambda o: km.m.u32(o + 0x0C) or 0, default=0)      # InternalIndex 最大 = 最新
    if best:
        _CACHE[pid] = (best, time.time())
    return best


def _fname_set(km, addr: int) -> Set[str]:
    from kardsmem import containers as ct
    pool = km.names_pool()
    out: Set[str] = set()
    for el in ct.sparsearray_slots(km.m, addr, ELEM_STRIDE_FNAME):
        idx, num = km.m.u32(el), km.m.u32(el + 4)
        if idx is None:
            continue
        nm = pool.fname(idx, num or 0)
        if nm:
            out.add(nm)
    return out


def read_changes(km) -> List[dict]:
    """游戏当前的 `cards_reserve_changes`；读不到会话 ⇒ 抛 RuntimeError（调用方如实报缺口，不当"没有变更"）。"""
    s = _session_ptr(km)
    if not s:
        raise RuntimeError("找不到 KardsSession（cards_reserve_changes 所在对象）")
    data, num = km.m.ptr_or_zero(s + OFF_CHANGES), km.m.i32(s + OFF_CHANGES + 8)
    if num is None or num < 0 or num > 4096 or (num and not data):
        raise RuntimeError("cards_reserve_changes 读出异常（num=%r）" % (num,))
    out = []
    for i in range(num):
        a = data + i * CHANGE_STRIDE
        out.append({"activation": km.m.i64(a) if hasattr(km.m, "i64") else int.from_bytes(km.m.read_exact(a, 8), "little"),
                    "to_be_reserved": _fname_set(km, a + OFF_TO_BE_RESERVED),
                    "coming_back": _fname_set(km, a + OFF_COMING_BACK)})
    return out


def checker(km, now_ticks: Optional[int] = None) -> Callable[[int, str], bool]:
    """→ `reserved(静态卡指针, 内部名) -> bool`：游戏自己的数据（变更表 + 静态卡 isReserved）。读一次变更表，之后纯内存查。"""
    changes = read_changes(km)
    now = utc_ticks() if now_ticks is None else now_ticks

    def reserved(static_ptr: int, name: str) -> bool:
        flag = bool(km.m.u8(static_ptr + OFF_STATIC_IS_RESERVED)) if static_ptr else False
        return is_reserved(name, flag, changes, now)
    return reserved
