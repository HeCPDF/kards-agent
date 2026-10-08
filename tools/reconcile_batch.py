#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reconcile_batch.py —— P5 影子对账的**离线批量**版（不需要游戏在跑）。

为什么能离线
============
`record_effects` 的 VM 要读两样东西：**函数字节码**（`kismet.find_function`）和**卡牌/盘面字段**。它们都经
`km.m`（一个只读内存句柄）读。游戏崩溃/手动抓下来的**整进程转储**（`kards-data/crashdumps/*.dmp`，`procdump -ma`，
约 3.4 GB，含完整堆）里这些字节都在 ⇒ 给 `kardsmem.Session` 换一个**以转储为后端的只读句柄**（`DumpMem`），
VM、`CardNatives`、`make_read_hooks`、`HasBond`/`GetCardsInHandBySide` 这类读内存的原语就全都能离线跑。
（`kardsmem/objects.py` 的注释早就写了"session 只要 .m 和 .base（转储也行）"。）

做什么
======
对每个转储：读出当时的盘面（`MemoryBoardSource` 同一份代码，换了句柄），再造几种**盘面变体**
（原样 / 清空场上单位 / 只留我方单位 / 只留对方单位），每种变体里把**每张指令牌**（不管转储里它在牌库/弃牌堆/手里、
也不管哪一方）都放进我方手牌，然后用**真的** `RuleV2._search_sim` 建 `Sim`、**真的** `RuleV2._shadow_check`
做三方对账（A=字典路 `_apply_eff`；B=对 `Sim` 副本直跑；C=B 的 `applied` 经 `engine.calls.apply_calls` 重放），
按牌聚合成 same / diff_ab / diff_bc / skipped（带原因）。

只离线
======
* 只读转储文件；`agent.precheck.call_read/call_write` 在整个运行期间被换成"调用即抛"（防止任何注入/实机 RPC）。
* 不碰 `kards-data/live`、不碰 gui 控制文件；报告写到 `kards-agent/docs/RECONCILE-BATCH-REPORT.json`（默认，可用 --report 改）。

用法
====
    python tools/reconcile_batch.py                          # kards-data/crashdumps 里最新的 2 个转储
    python tools/reconcile_batch.py --dumps A.dmp B.dmp --report out.json
    python tools/reconcile_batch.py --all --scenarios as_dump,empty_board
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import copy
import glob
import json
import mmap
import os
import struct
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if AGENT_ROOT not in sys.path:                                      # 直接 `python tools/reconcile_batch.py` 也能 import tools.*
    sys.path.insert(0, AGENT_ROOT)

import tools.mdmp as _mdmp                                          # noqa: E402

MODULE_LOWER = "kards-win64-shipping.exe"
MODULE_LIST_STREAM = 4
SCENARIOS = ("as_dump", "empty_board", "mine_only", "foe_only")
DEFAULT_REPORT = os.path.join(AGENT_ROOT, "docs", "RECONCILE-BATCH-REPORT.json")


# ------------------------------------------------------------------ 转储 → 只读内存句柄
def module_base(path: str, module: str = MODULE_LOWER) -> Optional[int]:
    """从 minidump 的 ModuleListStream 里找游戏 exe 的加载基址（没有 ⇒ None）。"""
    st = _mdmp.streams(path)
    if MODULE_LIST_STREAM not in st:
        return None
    _sz, rva = st[MODULE_LIST_STREAM]
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        n = struct.unpack_from("<I", mm, rva)[0]
        for i in range(n):
            o = rva + 4 + i * 108
            base, _size, _chk, _ts, nrva = struct.unpack_from("<QIIII", mm, o)
            ln = struct.unpack_from("<I", mm, nrva)[0]
            name = mm[nrva + 4: nrva + 4 + ln].decode("utf-16le", "replace")
            if name.lower().replace("/", "\\").rsplit("\\", 1)[-1] == module:
                return base
    return None


def _mem_class():
    from kardsmem import proc as P

    class DumpMem(P.MemRO):
        """以 minidump（Memory64List）为后端的只读句柄；读不到返回 None（与 `MemRO` 同口径，绝不抛）。
        与 `tools/dumpmem.py::DumpMem` 的区别：这个是 `MemRO` 子类（能直接塞给 `kardsmem.Session.m`），
        且**VA 连续的相邻区段会拼接**（整进程转储里大数组/对象常被切成相邻区段；有空洞才算读不出）。"""

        def __init__(self, path: str, ranges=None):
            self.pid = 0
            self.h = 1                                   # 非空即可（`_Mem.close` 里才用）
            self._f = open(path, "rb")
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
            self.rg = ranges if ranges is not None else _mdmp.ranges(path)
            self._starts = [r[0] for r in self.rg]

        def close(self):
            try:
                self._mm.close()
                self._f.close()
            except Exception:                            # noqa: BLE001
                pass

        # ---- 覆盖层（离线"改内存"，只在本进程里；绝不回写转储文件）----
        # blob = 凭空分配的对象副本（合成牌）；patch = 对转储里已有字节的小补丁（让陈旧的实例字段回到出厂值）
        FAKE_BASE = 0x7E0000000000

        def alloc_copy(self, src: int, size: int):
            """把 `src` 处 `size` 字节复制到一块**假地址**（转储里不存在的区域）⇒ 新地址；读不到源 ⇒ None。"""
            data = self.read(src, size)
            if not data or len(data) != size:
                return None
            if not hasattr(self, "_blobs"):
                self._blobs, self._next = [], self.FAKE_BASE
            a = self._next
            self._next += ((size + 0xFFF) & ~0xFFF) + 0x1000
            self._blobs.append((a, bytearray(data)))
            return a

        def patch(self, addr: int, data: bytes) -> None:
            for s, b in getattr(self, "_blobs", ()):
                if s <= addr and addr + len(data) <= s + len(b):
                    b[addr - s: addr - s + len(data)] = data
                    return
            if not hasattr(self, "_patches"):
                self._patches = []
            self._patches.append((addr, bytes(data)))

        def read(self, addr: int, size: int):
            if size <= 0 or addr <= 0:
                return None
            for s, b in getattr(self, "_blobs", ()):
                if s <= addr and addr + size <= s + len(b):
                    return bytes(b[addr - s: addr - s + size])
            out = self._read_dump(addr, size)
            pt = getattr(self, "_patches", None)
            if pt and out:
                ba = bytearray(out)
                for a, d in pt:
                    lo, hi = max(a, addr), min(a + len(d), addr + len(ba))
                    if lo < hi:
                        ba[lo - addr: hi - addr] = d[lo - a: hi - a]
                out = bytes(ba)
            return out

        def _read_dump(self, addr: int, size: int):
            i = bisect.bisect_right(self._starts, addr) - 1
            parts = []
            while size > 0 and 0 <= i < len(self.rg):
                va, sz, off = self.rg[i]
                if addr < va or addr >= va + sz:
                    break
                n = min(size, va + sz - addr)
                parts.append(self._mm[off + addr - va: off + addr - va + n])
                addr += n
                size -= n
                i += 1
            return b"".join(parts) if parts else None

    return DumpMem


def open_dump(path: str, base: Optional[int] = None):
    """→ `kardsmem.Session`（合成目标：不查进程、不校验指纹；RVA 取 `build.RVA` 当前生效表）。"""
    from kardsmem import proc as P
    base = base or module_base(path)
    if not base:
        raise RuntimeError("转储里找不到 %s 模块：%s" % (MODULE_LOWER, path))
    s = P.Session(base=base)
    s.m = _mem_class()(path)
    return s


def snapshot_of(km):
    """用 `board.MemoryBoardSource`（同一份读盘面代码）从转储读出 `BoardState`。"""
    from kardsmem import board as BA
    src = BA.MemoryBoardSource(pid=1, validate_build=False, base=km.base)
    src._m = km.m
    src._attach()
    return src.snapshot()


# ------------------------------------------------------------------ 离线护栏 / 假会话
class OfflineViolation(RuntimeError):
    pass


@contextlib.contextmanager
def offline_guard():
    """运行期间把所有"打到游戏进程"的出口换成抛错：`agent.precheck` 的读/写注入。"""
    patched = []
    try:
        from agent import precheck
        for nm in ("call_read", "call_write", "_call"):
            if hasattr(precheck, nm):
                patched.append((precheck, nm, getattr(precheck, nm)))

                def _boom(*a, _nm=nm, **k):
                    raise OfflineViolation("离线批量对账里不许调 agent.precheck.%s（会打到游戏进程）" % _nm)
                setattr(precheck, nm, _boom)
    except ImportError:
        pass
    try:
        yield
    finally:
        for mod, nm, old in patched:
            setattr(mod, nm, old)


class OfflineSess:
    """`RuleV2` 要的最小会话：给 km；游戏侧判断（`can_play` 等）离线没有 ⇒ 一律「可以」（影子对账比的是效果，不是可玩性）。"""

    def __init__(self, km):
        self.km = km

    def _kardsmem(self):
        return self.km

    def can_play(self, c, target=None):
        return {"can": True, "source": "offline"}

    def can_attack(self, a, t):
        return {"can": True, "source": "offline"}

    def can_move(self, u):
        return {"can": True, "source": "offline"}

    def hand_target_legal(self, c):
        return {"can": True, "source": "offline"}

    def mulligan_marks(self):
        return []


# ------------------------------------------------------------------ 盘面变体
def _hand_loc(my_side):
    from kardsmem.gamemodel import HAND_OF, ESide
    return HAND_OF[ESide(my_side)]


def _is_order_like(c) -> bool:
    """指令/事件类：非单位、非总部/地点牌、非反制（反制不是『打出生效』）。"""
    from kardsmem.gamemodel import EType
    t = c.obj.Type
    return t == EType.order


def make_scenario(st, scenario: str):
    """→ 盘面副本：把每个同名指令牌放进我方手牌（只留一张/名），再按变体裁剪场上单位。"""
    s2 = copy.deepcopy(st)
    me = s2.my_side
    keep, seen = [], set()
    hand_loc = _hand_loc(me)
    for c in s2.cards:
        o = c.obj
        if o.IsFieldUnit():
            fu = o.side == me
            if scenario == "empty_board" or (scenario == "mine_only" and not fu) or (scenario == "foe_only" and fu):
                continue
            keep.append(c)
            continue
        if _is_order_like(c) and c.name not in seen:
            seen.add(c.name)
            o.Location, o.side = hand_loc, me
            keep.append(c)
            continue
        if _is_order_like(c):
            continue                                       # 同名重复：丢掉，免得手牌里出两份
        if o.InHand():
            continue                                       # 转储里原来的手牌（单位等）：不参与，免得抢位
        keep.append(c)                                     # 总部、牌库、弃牌堆等原样
    s2.cards = keep
    return s2


# ------------------------------------------------------------------ 实例"出厂化"与合成牌（扩大覆盖）
OFF_UOBJ_CLASS, OFF_UCLASS_PROPSIZE = 0x10, 0x58
_SKIP_OWN = ("UberGraphFrame",)


def freshen_instance(km, ptr: int) -> int:
    """把一张实例牌**自有类**（BP 子类，不含 `UBaseCardObject`）的属性重置成该类 CDO 的出厂值 → 重置的属性个数。

    为什么：对账把转储里**弃牌堆/牌库**的指令牌塞进"手牌"。弃牌堆里的实例已经打出过，BP 里 `OnPlayedFromHand` 往自己的
    成员数组里写过东西（NAVAL BATTLE 的 `cardsToDestroy`、PARACHUTE ASSAULT 的 `unitsToSpawn`），VM 读到的是上一次打出残留的
    陈旧数组 ⇒ 假缺口 / 假效果（NAVAL BATTLE 摧毁了转储里 57/44/48 这三张**早已不在场上**的卡；PARACHUTE 停在读 NameProperty 数组）。
    真游戏里一张在手牌里的牌这些成员都是空的 ⇒ 用 CDO 的值覆盖（只改本进程里的覆盖层，不回写转储）。
    读不到 CDO / 句柄不支持覆盖 ⇒ 0（原样不动）。"""
    from kardsmem import props
    m = getattr(km, "m", None)
    if m is None or not hasattr(m, "patch") or not ptr:
        return 0
    uc = m.ptr_or_zero(ptr + OFF_UOBJ_CLASS)
    cdo = m.ptr_or_zero(uc + 0x110) if uc else 0                  # UClass::ClassDefaultObject（弯路 #20）
    if not cdo:
        return 0
    n = 0
    for r in props.struct_props(km, uc):
        if r.get("name") in _SKIP_OWN or not r.get("offset") or r.get("offset") < 0:
            continue
        sz = int(r.get("size") or 0) * max(int(r.get("array_dim") or 1), 1)
        d = m.read_exact(cdo + r["offset"], sz) if sz > 0 else None
        if d:
            m.patch(ptr + r["offset"], d)
            n += 1
    return n


def _card_reader(km):
    from kardsmem import board as BA
    src = BA.MemoryBoardSource(pid=1, validate_build=False, base=km.base)
    src._m = km.m
    src._attach()
    return src


_SYN_CID0 = 50000


def synthetic_orders(km, st, api_titles, only=None) -> list:
    """给**转储里没有实例**的官方指令造合成手牌（`--orders all`）→ `[Card]`（已按 `st.my_side` 放进我方手牌）。

    做法：静态卡表（`GameState+0x670` `AllStaticCardsSortedByName`，整进程转储里有，约 2019 张）里每张卡都有一个完整的
    `UBaseCardObject` 数据资产（类/标题/费用/文本，`Type@0x68`；加密记录的 key 恒 1939）。把它**复制到假地址**（覆盖层），
    只补 4 个运行期字段：`cardID`（正数、唯一 ⇒ 否则 `spawnerID>0` 之类的闸门会拦）、`location`（我方手牌）、`side`、
    `cardFunction`（取转储里任一真实牌的那个活 `BP_CardFunctions_C`；静态卡自己是空的，弯路 #44）。
    VM 只读这张对象的字段/类上的字节码 ⇒ 与真实实例等价（成员数组出厂为空，不需要 `freshen_instance`）。
    同标题多变体（HEATWAVE ×3 等）⇒ 标题带上内部名后缀以免合并统计；已在手牌里的标题不重复造。"""
    from kardsmem import board as BA, gs as G, props
    from kardsmem.gamemodel import EType
    m = getattr(km, "m", None)
    if m is None or not hasattr(m, "alloc_copy") or st.my_side is None:
        return []
    prov = G.make_static_card_provider(km)
    reader = _card_reader(km)
    have = {c.name for c in st.cards if c.obj.InHand()}
    ref = next((c.raw["ptr"] for c in st.cards if (getattr(c, "raw", None) or {}).get("ptr")
                and c.obj.CardID and not c.obj.IsHQ()), 0)
    cf = 0
    if ref:
        p = props.find_prop(km, m.ptr_or_zero(ref + OFF_UOBJ_CLASS), "cardFunction")
        cf = m.ptr_or_zero(ref + p["offset"]) if p else 0
    cands = []
    for nm in sorted(prov.names):
        v = prov(nm)
        if v and v.get("card_type") == "order" and nm.startswith("card_event_") \
                and (v.get("title") or "").upper() in api_titles                 and (not only or (v.get("title") or "").upper() in only):
            cands.append((nm, v))
    per_title = defaultdict(int)
    for _nm, v in cands:
        per_title[(v.get("title") or "").upper()] += 1
    cache = m.__dict__.setdefault("_syn_cache", {})
    out, seat = [], int(st.my_side)
    used_plain = set()
    hand_loc = int(_hand_loc(st.my_side))
    for i, (nm, v) in enumerate(cands):
        title = (v.get("title") or "").upper()
        if title in have and per_title[title] == 1:
            continue
        if per_title[title] > 1 and title in have:
            pass                                          # 变体：转储里已有的那份按标题统计，这里补**全部**变体并加后缀
        src = int(v["ptr"])
        uc = m.ptr_or_zero(src + OFF_UOBJ_CLASS)
        key = (nm, seat)
        addr = cache.get(key)
        if addr is None:
            size = m.i32(uc + OFF_UCLASS_PROPSIZE) if uc else 0
            addr = m.alloc_copy(src, size) if size and 0 < size < 0x10000 else None
            if not addr:
                continue
            cid = _SYN_CID0 + len(cache)
            m.patch(addr + BA.CARD_I32["card_id"], struct.pack("<i", cid))
            m.patch(addr + BA.CARD_U8["location_enum"], bytes([hand_loc]))
            m.patch(addr + BA.CARD_U8["side_enum"], bytes([seat]))
            pc = props.find_prop(km, uc, "cardFunction")
            if pc and cf:
                m.patch(addr + pc["offset"], struct.pack("<Q", cf))
            cache[key] = addr
        c = reader._read_card(m, addr, [])
        if c is None or c.obj.Type != EType.order:
            continue
        if per_title[title] > 1 and (title in have or title in used_plain):
            c.obj.title = "%s <%s>" % (title, nm[len("card_event_"):])
        used_plain.add(title)
        out.append(c)
    return out


# ------------------------------------------------------------------ 一个场景的对账
def _policy(km, st):
    import player.rule as R
    from policy.search import wire_sim
    wire_sim()
    pol = R.RuleV2(OfflineSess(km), params={"vm_budget_s": 1e9, "vm_one_s": SHADOW_VM_S, "shadow": False, "shadow_vm_s": SHADOW_VM_S,
                                                       "shadow_branch_cap": 16})
    pol._st = st
    # ★ 牌局随机流种子（用户 2026-10-06：随机不当分支，用游戏自己的流）：转储里读得到 `cardsRandomStream.Seed`
    #   （`kardsmem.rng.read_seed(km)` 走关卡 actor 里播种过的 `BP_CardFunctions_C`，离线内存后端同一份代码）⇒ A/B/C 都拿它按 LCG 跑出具体结果。
    #   读不到才用**固定**假种子（`RuleV2._rng_seed` 缺省的 `getrandbits` 会让两次离线复跑结果不同）；报告里如实标 `rng_fake`。
    try:
        from kardsmem.rng import read_seed
        if read_seed(km) is None:
            pol.__dict__["_fake_seed_by_turn"] = {getattr(st, "turn", None): FALLBACK_SEED}
    except Exception:                                        # noqa: BLE001
        pol.__dict__["_fake_seed_by_turn"] = {getattr(st, "turn", None): FALLBACK_SEED}
    return R, pol


def _reason(payload: str) -> str:
    """skipped 串 `牌→目标：原因` → 原因（去掉目标/地址，便于聚合）。"""
    s = str(payload)
    for sep in ("：", ": "):
        if sep in s:
            s = s.split(sep, 1)[1]
            break
    import re
    s = re.sub(r"0x[0-9A-Fa-f]+", "0x…", s)
    return s.strip()[:90]


DEFAULT_KREDITS = 10.0
#: 转储里读不到牌局随机流种子时用的固定假种子（离线复跑可复现；结果标 `rng_fake`）
FALLBACK_SEED = 0x1BADB002
#: 影子对账里单张牌的 VM 时限（实机 0.6 s；离线从转储读内存慢得多 ⇒ 放宽，避免把『慢』记成『VM 超时』的假跳过）
SHADOW_VM_S = 30.0


def innermost_stop(stopped) -> str:
    """VM 的 `stopped` 常是层层包裹的串（`rule._shadow_check` 还会截成 60 字）。取最里层的 `Unimplemented @… <函数>: <原语>`。"""
    import re
    s = str(stopped or "")
    s = re.sub(r"[\\'\"]+", "", s)                           # 层层转义留下的 \ 和引号
    hits = re.findall(r"Unimplemented @0x[0-9A-Fa-f]+ ([^:]+): ([^\n]*)", s)
    if hits:
        fn, rest = hits[-1]
        return ("Unimplemented %s: %s" % (fn.strip(), rest.strip()))[:110]
    return re.sub(r"0x[0-9A-Fa-f]+", "0x…", s)[:110]


def _norm_gap(g) -> str:
    import re
    return re.sub(r"\d{6,}", "<ptr>", str(g))[:110]


def _only_match(name: str, only) -> bool:
    """`--only` 的名单匹配：整名（含 ` <变体>` 后缀）或去掉后缀的标题，大小写不敏感。"""
    n = str(name).upper()
    return n in only or n.split(" <", 1)[0] in only


def reconcile_state(km, st, *, with_untargeted: bool = True, kredits: float = DEFAULT_KREDITS,
                    orders: str = "dump", only=None) -> dict:
    """对一份盘面 `st`（我方手牌里的指令）做三方对账 → `{牌名: {status: n}, reasons, samples}` 的行表。

    ★ `RuleV2._search_sim` 只给**需要目标**的牌填 `pair_eff`（不带目标的指令只有 `H.eff`，影子 `_shadow_check` 遍历的是
      `pair_eff` ⇒ 不带目标的指令**从来没被影子比过**）。这里把 `(cid, None)` 补进 `sim.pair_eff`（取 `H.eff`，与搜索
      实际用的同一份字典），让它们也进同一个 `_shadow_check`（rule.py 不改）。
    ★ 指挥点固定成 `kredits`（缺省 10，在 `kredit_max`=24 内）：转储里那一刻的指挥点可能是 0（买不起 ⇒ 带目标的牌不进候选），
      也不能给超过上限的数（A 路夹到上限、B 路不夹，是夹具造成的假分歧——第一轮批量里 99 点就造出了一堆假 `kredits` 差异）。"""
    for c in st.cards:                                     # 手牌里的实例回到"没打出过"的出厂成员值（见 freshen_instance）
        if c.obj.InHand() and c.obj.side == st.my_side and (getattr(c, "raw", None) or {}).get("ptr"):
            freshen_instance(km, c.raw["ptr"])
    only = {x.strip().upper() for x in (only or ()) if x.strip()} or None
    if orders == "all":                                    # 覆盖扩展：转储里没有实例的官方指令也合成进手牌
        titles = set(api_order_titles())
        if only:
            titles &= {o.split(" <", 1)[0] for o in only}
        st.cards = list(st.cards) + synthetic_orders(km, st, titles)
    if only:                                               # --only：我方手牌里的指令只留名单内的（其余牌不动）
        from kardsmem.gamemodel import EType
        st.cards = [c for c in st.cards
                    if not (c.obj.InHand() and c.obj.side == st.my_side and c.obj.Type == EType.order)
                    or _only_match(c.name, only)]
    R, pol = _policy(km, st)
    ehq = R._hq_of(st.cards, st.other_side)
    sim = pol._search_sim(st, float(kredits), ehq)
    if with_untargeted:
        for cid, h in list(sim.hand.items()):
            if not any(k[0] == cid for k in sim.pair_eff):
                sim.pair_eff[(cid, None)] = dict(getattr(h, "eff", None) or {})
    pol._shadow_out = {}
    names = {c.obj.CardID: c.name for c in st.cards}
    ptr_name = {(getattr(c, "raw", None) or {}).get("ptr"): c.name for c in st.cards}
    side_info = defaultdict(lambda: {"stop": Counter(), "dgaps": Counter(), "exact": 0})
    import semantics.effectvm as EV
    orig = EV.record_effects

    def _spy(km_, ptr, *a, **k):
        r = orig(km_, ptr, *a, **k)
        nm = ptr_name.get(ptr)
        if nm is not None and isinstance(r, dict):
            if r.get("stopped"):
                side_info[nm]["stop"][innermost_stop(r["stopped"])] += 1
            for g in (r.get("direct_gaps") or ()):
                side_info[nm]["dgaps"][_norm_gap(g)] += 1
            if r.get("exact") and k.get("direct_state") is not None:
                side_info[nm]["exact"] += 1                   # 这次直跑里随机点用了牌局流（确定结果，不枚举）
        return r
    EV.record_effects = _spy
    t0 = time.time()
    try:
        res = pol._shadow_check(st, sim, budget_s=1e9, max_cards=10 ** 9)
    finally:
        EV.record_effects = orig
    rows = defaultdict(lambda: {"same": 0, "diff_ab": 0, "diff_bc": 0, "skipped": 0, "reasons": Counter(),
                                "samples": {}, "diff_keys": Counter(), "direct_gaps": Counter(),
                                "rng_exact": 0, "choice_options": 0})
    seen_skips = set()
    for key, (status, payload) in pol._shadow_out.items():
        name = names.get(key[1], str(key[1]))
        r = rows[name]
        r[status] += 1
        _tag = payload.get("card") if isinstance(payload, dict) else str(payload)
        if "[选项" in str(_tag):                              # 抉择：每个选项是独立条目（用户 2026-10-06）
            r["choice_options"] += 1
        if status == "skipped":
            r["reasons"][_reason(payload)] += 1
            seen_skips.add(payload)
        elif status in ("diff_ab", "diff_bc"):
            r["samples"].setdefault(status, payload)
            for dk in (payload.get("diff") or {}):
                r["diff_keys"]["%s:%s" % (status, "unit" if str(dk)[:1] == "u" and str(dk)[1:].lstrip("-").isdigit() else dk)] += 1
            if status == "diff_bc" and payload.get("gaps"):
                r["diff_keys"]["diff_bc:gap:%s" % _reason(payload["gaps"][-1])[:60]] += 1
    for s in res.get("skipped", ()):                        # 抛异常的（没进缓存）
        if s in seen_skips:
            continue
        name = str(s).split("→", 1)[0]
        r = rows[name]
        r["skipped"] += 1
        r["reasons"][_reason(s)] += 1
    for name, r in rows.items():                            # 用没被截断的停因替换 skipped 的原因；记直跑缺口
        si = side_info.get(name)
        if not si:
            continue
        if r["skipped"] and any("Unimplemented" in k for k in si["stop"]):
            r["reasons"] = Counter(dict(si["stop"]))
        r["direct_gaps"].update(si["dgaps"])
        r["rng_exact"] += si["exact"]
    return {"rows": rows, "gaps": res.get("gaps", {}), "elapsed_s": round(time.time() - t0, 2),
            "n_hand": len(sim.hand), "n_pairs": len(sim.pair_eff),
            "rng_seed": pol._rng_seed(), "rng_fake": bool(getattr(pol, "seed_fake", False)),
            "choice_actions": res.get("choice_actions", 0), "enum_fallback": res.get("enum_fallback", 0)}


# ------------------------------------------------------------------ 批量
def classify(row: dict) -> str:
    if row["diff_ab"]:
        return "diff_ab"
    if row["diff_bc"]:
        return "diff_bc"
    if row["same"]:
        return "same"
    return "skipped"


def open_state(path: str):
    """→ `(km, st)`：转储作后端的会话 + 当时的盘面。"""
    km = open_dump(path)
    return km, snapshot_of(km)


def run_batch(dumps, scenarios=SCENARIOS, log=print, reconcile=None, opener=None, scenario_maker=None) -> dict:
    """`reconcile`/`opener`/`scenario_maker` 留给离线测试注入假实现（`opener(path) -> (km, st)`）。"""
    reconcile = reconcile or reconcile_state
    opener = opener or open_state
    scenario_maker = scenario_maker or make_scenario
    agg = defaultdict(lambda: {"same": 0, "diff_ab": 0, "diff_bc": 0, "skipped": 0,
                               "reasons": Counter(), "samples": {}, "where": [], "diff_keys": Counter(),
                               "direct_gaps": Counter(), "rng_exact": 0, "choice_options": 0})
    runs, errors = [], []
    t_all = time.time()
    with offline_guard():
        for path in dumps:
            try:
                km, st = opener(path)
            except Exception as ex:                          # noqa: BLE001
                errors.append({"dump": os.path.basename(path), "error": "%s: %s" % (type(ex).__name__, str(ex)[:120])})
                log("  [跳过] %s：%s" % (os.path.basename(path), errors[-1]["error"]))
                continue
            if not getattr(st, "cards", None) or getattr(st, "my_side", None) is None:
                errors.append({"dump": os.path.basename(path), "error": "转储里没有对局盘面（菜单/加载中）"})
                log("  [跳过] %s：没有对局盘面" % os.path.basename(path))
                continue
            for sc in scenarios:
                try:
                    st2 = scenario_maker(st, sc)
                    out = reconcile(km, st2)
                except Exception as ex:                      # noqa: BLE001
                    errors.append({"dump": os.path.basename(path), "scenario": sc,
                                   "error": "%s: %s" % (type(ex).__name__, str(ex)[:120])})
                    log("  [出错] %s/%s：%s" % (os.path.basename(path), sc, errors[-1]["error"]))
                    continue
                runs.append({"dump": os.path.basename(path), "scenario": sc, "elapsed_s": out["elapsed_s"],
                             "n_hand": out["n_hand"], "n_pairs": out["n_pairs"], "turn": getattr(st, "turn", None),
                             "cards": len(out["rows"]), "rng_seed": out.get("rng_seed"), "rng_fake": out.get("rng_fake"),
                             "choice_actions": out.get("choice_actions", 0), "enum_fallback": out.get("enum_fallback", 0)})
                log("  %s / %-11s 手牌 %2d 对 %3d 用时 %6.1fs" % (os.path.basename(path)[-20:], sc, out["n_hand"],
                                                                   out["n_pairs"], out["elapsed_s"]))
                for name, r in out["rows"].items():
                    a = agg[name]
                    for k in ("same", "diff_ab", "diff_bc", "skipped"):
                        a[k] += r[k]
                    a["reasons"].update(r["reasons"])
                    a["diff_keys"].update(r["diff_keys"])
                    a["direct_gaps"].update(r.get("direct_gaps") or ())
                    a["rng_exact"] += r.get("rng_exact", 0)
                    a["choice_options"] += r.get("choice_options", 0)
                    for k, v in r["samples"].items():
                        a["samples"].setdefault(k, v)
                    tag = "%s/%s" % (os.path.basename(path)[-13:-4], sc)
                    if len(a["where"]) < 6:
                        a["where"].append(tag)
            try:
                km.m.close()
            except Exception:                                # noqa: BLE001
                pass                                         # 假会话没有 .m
    return build_report(agg, runs, errors, dumps, scenarios, time.time() - t_all)


def api_order_titles(path: Optional[str] = None) -> list:
    """官方卡表里 type=order 的卡名（英文大写，与游戏里 `Card.name` 同口径）。读不到 ⇒ []。"""
    if path is None:
        from base import paths
        path = paths.API_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return sorted({(c["json"].get("title") or {}).get("en-EN", "").upper()
                       for c in d if c.get("json", {}).get("type") == "order"} - {""})
    except Exception:                                        # noqa: BLE001
        return []


def build_report(agg, runs, errors, dumps, scenarios, elapsed, api_titles=None) -> dict:
    cards = {}
    for name in sorted(agg):
        a = agg[name]
        cards[name] = {"status": classify(a), "same": a["same"], "diff_ab": a["diff_ab"], "diff_bc": a["diff_bc"],
                       "skipped": a["skipped"], "reasons": dict(a["reasons"].most_common(5)),
                       "diff_keys": dict(a["diff_keys"].most_common(8)),
                       "direct_gaps": dict(a["direct_gaps"].most_common(4)), "samples": a["samples"], "where": a["where"],
                       "rng_exact": a.get("rng_exact", 0), "choice_options": a.get("choice_options", 0)}
    summary = Counter(c["status"] for c in cards.values())
    titles = api_order_titles() if api_titles is None else list(api_titles)
    covered = set(cards)
    report = {
        "tool": "tools/reconcile_batch.py",
        "offline": True,
        "dumps": [os.path.basename(d) for d in dumps],
        "scenarios": list(scenarios),
        "elapsed_s": round(elapsed, 1),
        "summary": {"cards": len(cards), **{k: summary.get(k, 0) for k in ("same", "diff_ab", "diff_bc", "skipped")},
                    "compare_same": sum(c["same"] for c in cards.values()),
                    "compare_diff_ab": sum(c["diff_ab"] for c in cards.values()),
                    "compare_diff_bc": sum(c["diff_bc"] for c in cards.values()),
                    "compare_skipped": sum(c["skipped"] for c in cards.values()),
                    # 随机/抉择口径（用户 2026-10-06）：随机用牌局流算成确定结果；抉择逐选项独立条目
                    "cards_with_rng_exact": sum(1 for c in cards.values() if c.get("rng_exact")),
                    "choice_option_entries": sum(c.get("choice_options", 0) for c in cards.values()),
                    "cards_with_choice": sum(1 for c in cards.values() if c.get("choice_options"))},
        "api_orders": {"total": len(titles), "reached": len(covered & set(titles)),
                       "not_reached": sorted(set(titles) - covered)[:400]},
        "cards": cards,
        "runs": runs,
        "errors": errors,
    }
    return report


def format_table(report: dict) -> str:
    rows = ["%-34s %-8s %5s %7s %7s %7s  %s" % ("牌", "结论", "same", "diff_ab", "diff_bc", "skipped", "原因/样例")]
    for name, c in sorted(report["cards"].items(), key=lambda kv: ({"diff_ab": 0, "diff_bc": 1, "skipped": 2, "same": 3}[kv[1]["status"]], kv[0])):
        why = ""
        if c["reasons"]:
            why = "; ".join("%s×%d" % (k, v) for k, v in list(c["reasons"].items())[:2])
        if c.get("diff_keys") and not why:
            why = ", ".join("%s×%d" % kv for kv in list(c["diff_keys"].items())[:3])
        if c.get("direct_gaps") and c["status"] in ("diff_ab", "diff_bc"):
            why += " | 直跑缺口：" + "; ".join("%s×%d" % kv for kv in list(c["direct_gaps"].items())[:1])
        rows.append("%-34s %-8s %5d %7d %7d %7d  %s" % (name[:34], c["status"], c["same"], c["diff_ab"], c["diff_bc"],
                                                         c["skipped"], why))
    s = report["summary"]
    rows.append("")
    rows.append("牌 %d：same %d / diff_ab %d / diff_bc %d / skipped %d（比对次数 same %d、diff_ab %d、diff_bc %d、skipped %d）；"
                "官方指令 %d 张，转储里碰到 %d 张；用时 %.0fs"
                % (s["cards"], s["same"], s["diff_ab"], s["diff_bc"], s["skipped"], s["compare_same"], s["compare_diff_ab"],
                   s["compare_diff_bc"], s["compare_skipped"], report["api_orders"]["total"], report["api_orders"]["reached"],
                   report["elapsed_s"]) + "；随机用牌局流 %d 张、抉择逐选项 %d 张/%d 条"
                % (s.get("cards_with_rng_exact", 0), s.get("cards_with_choice", 0), s.get("choice_option_entries", 0)))
    return "\n".join(rows)


def default_dumps(n: int = 2) -> list:
    from base import paths
    fs = sorted(glob.glob(os.path.join(paths.DATA, "crashdumps", "*.dmp")), key=os.path.getmtime, reverse=True)
    return fs[:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P5 影子对账的离线批量版（转储作内存后端，不碰游戏）")
    ap.add_argument("--dumps", nargs="*", help="minidump/procdump -ma 文件；缺省 = kards-data/crashdumps 里最新的 N 个")
    ap.add_argument("-n", type=int, default=2, help="缺省取最新几个转储（默认 2）")
    ap.add_argument("--all", action="store_true", help="用 crashdumps 里全部转储")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS), help="逗号分隔：%s" % "/".join(SCENARIOS))
    ap.add_argument("--orders", choices=("dump", "all"), default="dump",
                    help="dump=只用转储里有实例的指令（约 37/338）；all=再把静态卡表里其余官方指令合成进手牌（离线覆盖扩展）")
    ap.add_argument("--only", default="", help="逗号分隔的牌名（英文大写标题，或带 <变体> 的整名）：只对账这些指令牌（加速）")
    ap.add_argument("--report", default=DEFAULT_REPORT, help="JSON 报告路径")
    a = ap.parse_args(argv)
    dumps = a.dumps or default_dumps(10 ** 6 if a.all else a.n)
    if not dumps:
        print("没有转储可用（kards-data/crashdumps/*.dmp）；见 --dumps")
        return 2
    scen = [x for x in a.scenarios.split(",") if x]
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()} or None
    print("离线对账：%d 个转储 × %d 种盘面" % (len(dumps), len(scen)))
    rep = run_batch(dumps, scen, reconcile=lambda km, st2: reconcile_state(km, st2, orders=a.orders, only=only))
    os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
    with open(a.report, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
    print(format_table(rep))
    print("报告：%s" % a.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
