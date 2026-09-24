#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.rendered —— 列出**当前屏幕上摆着的每一张卡**（不读图）。

来源与权威
==========
`reports/spec/KARDS-AUTOMATION.md` §5（屏幕上的卡也从内存读）：
`ABP_BaseCard_C` 及其子类的 **actor**，类大小 `0x830`：
    CardID          @ +0x03C8  (int32)        —— 对局内临时实例 id
    selfBaseCardRef @ +0x0808  (UBaseCardObject*)
                                 → `+0x58` FText → `FTextData+0x20` 明文 UTF-16 卡名

找法（**不用** board_api 的私有全局当世界锚点）：
    Locator(session.m, session.base).find_actors(SIZE_ACTOR)
`Locator.find_actors` 会沿 `UClass+0x40 SuperStruct` 链走，所以 BP 子类（`ABP_XxxCard_C`）
也会被认出来；`Locator.is_cdo()` 挡掉 CDO。

`kind` 的分类依据（**有证据才细分，否则一律 `ui_or_hand_or_board`**）
==================================================================
本模块手上只有两个可读字段：`self_ref@0x0808` 与 `CardID@0x03C8`。
* 没有**任何**内存证据能把"手牌 / 场上 / 换牌界面"的屏幕卡区分开
  （它们都是同一个 `ABP_BaseCard_C` 家族，位置信息在 Slot/Canvas 组件里，
  本模块不去解析 —— 解析了也只是"猜"）。所以**缺省**取值
  `"ui_or_hand_or_board"`：它只声明"这是一张屏幕卡"，不声明它在哪。
* 只有当**身份读不出来**时才能给出更强、更安全的断言：
    - `self_ref == 0` 或 `CardID == 0` → `"candidate"`。
      这正是 §6 记的天气一级三选一（蓝天/薄雾/狂风）：`CardID=0` **且**
      `selfBaseCardRef=NULL` ⇒ 读不出身份。看到这种情况就知道
      "屏幕上有卡，但内存没给它身份"（候选/占位）。
    - `self_ref != 0` **且** `CardID != 0` → `"bound"`。两张表都对得上，
      身份是确定的，只是"它在手牌还是场上"仍要另找证据（别在本模块断言）。

只读
====
只用 `Session.m`（`MemRO`：`PROCESS_VM_READ` + `ReadProcessMemory`）。
不写内存、不注入、不 hook。任何读失败 → 字段是 `None`，**绝不用 0 冒充**。

CLI
===
    cd D:\\Kards\\reverse-data\\tools
    python -m kardsmem.rendered
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

from . import build as B
from . import proc as P
from .world import Locator

# --------------------------------------------------------------------------
# 偏移（本文件的唯一一份；探针脚本里那些抄来抄去的副本不要再用）
# --------------------------------------------------------------------------
SIZE_ACTOR = 0x830           # ABP_BaseCard_C（及其子类）
OFF_CARDID = 0x03C8          # int32
OFF_SELFREF = 0x0808         # UBaseCardObject*

OFF_CARD_TITLE = 0x58        # UBaseCardObject + 0x58 = FText title
# FTextData 里明文 FString 的候选偏移（与 board_api._read_card_name 同一张表）
FTEXT_TEXT_OFFSETS = (0x20, 0x18, 0x28, 0x30, 0x38, 0x10, 0x40)

KIND_UNKNOWN = "ui_or_hand_or_board"
KIND_CANDIDATE = "candidate"
KIND_BOUND = "bound"

# --------------------------------------------------------------------------
# board_api 复用（唯一实现处；import 失败也不能崩 —— 名字有本地回退）
# --------------------------------------------------------------------------
_read_card_name = None
try:                                     # pragma: no cover - 环境相关
    from board_api import _read_card_name as _ba_read_card_name
    _read_card_name = _ba_read_card_name
except Exception:                        # noqa: BLE001 - 缺了就用自己的实现
    _read_card_name = None

# names.py 由另一条线负责；**存在才用**，不存在时 class_name 一律 None。
# 注意：它是**可选**依赖，而且可能在本进程启动之后才被写出来 ——
# 所以这里 **不缓存失败**：加载失败时每次调用都重试一次（成功后就固定下来）。
_names = None
_names_probed = False


def _load_names():
    """惰性加载 `kardsmem.names`（可选）。返回模块或 None；**不缓存失败**。"""
    global _names, _names_probed
    if _names is not None:
        return _names
    try:
        from . import names as mod
        _names = mod
    except ImportError:
        _names = None
    _names_probed = True
    return _names


# --------------------------------------------------------------------------
# FText → 明文
# --------------------------------------------------------------------------
def _read_fstring(m, a: int, maxn: int = 300) -> Optional[str]:
    """读一个 `FString{ptr@0, num@+8}`（内存里是明文 UTF-16）。读不出返回 None。"""
    p = m.ptr(a)
    n = m.i32(a + 8)
    if not p or n is None or n <= 0 or n > maxn:
        return None
    b = m.read(p, n * 2)
    if not b:
        return None
    try:
        s = b.decode("utf-16-le", "replace")
    except Exception:                    # noqa: BLE001
        return None
    s = s.rstrip("\x00").strip()
    return s or None


def read_ftext(m, ftext_addr: int, diag: Optional[dict] = None) -> Optional[str]:
    """`FText`（`ftext_addr` 指向 FTextData*）→ 明文。

    先试 board_api 的 `_read_card_name`（同一个偏移表的已验证实现），
    失败或不可用时用本地实现走 `FTEXT_TEXT_OFFSETS`。
    **记下是哪个偏移命中的**（`diag["ftext_off"]`），免得日后又靠猜。
    """
    if not ftext_addr:
        return None
    if _read_card_name is not None:
        try:
            s = _read_card_name(m, ftext_addr - OFF_CARD_TITLE)
            if s:
                if diag is not None:
                    diag["name_from"] = "board_api._read_card_name"
                return s
        except Exception:                # noqa: BLE001
            pass
    td = m.ptr(ftext_addr)
    if not td:
        return None
    for off in FTEXT_TEXT_OFFSETS:
        s = _read_fstring(m, td + off)
        if s:
            if diag is not None:
                diag["name_from"] = "local FString@0x%X" % off
                diag["ftext_off"] = off
            return s
    return None


def read_card_name(m, self_ref: int, diag: Optional[dict] = None) -> Optional[str]:
    """`UBaseCardObject* + 0x58` 的 FText → 牌名。`self_ref == 0` 时返回 None。"""
    if not self_ref:
        return None
    return read_ftext(m, self_ref + OFF_CARD_TITLE, diag)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class RenderedCard:
    """屏幕上的一张卡（`ABP_BaseCard_C` 家族 actor）。

    所有可能读不出的字段都是 `Optional`：**`None` = 读不出**，
    不要把它当成 0 / 空字符串 / "没有"。
    """
    actor: int                                  # actor 对象地址
    card_id: Optional[int]                      # actor+0x03C8（None = 读不出）
    self_ref: int                               # actor+0x0808（0 = 空指针）
    name: Optional[str] = None                  # self_ref+0x58 FText 明文
    class_name: Optional[str] = None            # UClass 的 FName（要 names.py，缺则 None）
    base_card: Optional[object] = None          # 与 board snapshot 对上的 Card
    kind: str = KIND_UNKNOWN                    # 见模块 docstring
    matched_by: Optional[str] = None            # "card_id" / "name" / None
    propsize: Optional[int] = None              # 诊断：actor 的类大小
    uclass: int = 0                             # 诊断：UClass 地址
    index: Optional[int] = None                 # 诊断：在 ULevel::Actors 里的下标
    notes: dict = field(default_factory=dict)   # 诊断：读不出的字段写在这

    # -- 便捷 ------------------------------------------------------------
    @property
    def matched(self) -> bool:
        return self.base_card is not None

    def as_dict(self) -> dict:
        return {"actor": hex(self.actor), "card_id": self.card_id,
                "self_ref": hex(self.self_ref) if self.self_ref else None,
                "name": self.name, "class_name": self.class_name,
                "kind": self.kind, "matched_by": self.matched_by,
                "propsize": self.propsize,
                "uclass": hex(self.uclass) if self.uclass else None,
                "index": self.index,
                "base_card": (None if self.base_card is None else {
                    "uid": getattr(self.base_card, "uid", None),
                    "card_id": getattr(self.base_card, "card_id", None),
                    "name": getattr(self.base_card, "name", None),
                    "side": getattr(self.base_card, "side", None),
                    "location": getattr(self.base_card, "location", None),
                    "slot": getattr(self.base_card, "slot", None),
                }),
                "notes": dict(self.notes)}

    def line(self) -> str:
        cid = "None" if self.card_id is None else ("%d" % self.card_id)
        nm = self.name if self.name is not None else "<读不出>"
        cn = self.class_name if self.class_name is not None else "-"
        if self.base_card is None:
            mt = "未匹配"
        else:
            bc = self.base_card
            mt = "匹配(%s) %s/%s slot=%s" % (
                self.matched_by, getattr(bc, "side", "?"),
                getattr(bc, "location", "?"), getattr(bc, "slot", "?"))
        return ("actor[%s] 0x%X propsize=%s CardID=%-6s %-28s class=%-24s kind=%-20s %s"
                % (self.index if self.index is not None else "?",
                   self.actor, self.propsize, cid, nm[:28], cn[:24], self.kind, mt))


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
def _class_name_of(loc: Locator, uclass: int, fname) -> Optional[str]:
    """UClass 的 FName（`uclass+0x18`）。没有 names.py / 解析器就是 None。

    ⚠ `names.FNamePool.fname_of(obj)` **自己会加 `OFF_UOBJECT_NAME`**
    （`names.py:371`），所以这里传 **`uclass` 本身**，不要再 `+0x18`
    （传 `uclass+0x18` 会读成 +0x30 的垃圾 ⇒ 解不出名字）。
    其它解析器若只接受 FName 索引，就退回"自己读 token、再 call `fname(idx)`"。
    """
    if not uclass or fname is None:
        return None
    try:
        return fname.fname_of(uclass)
    except TypeError:
        pass
    except Exception:                    # noqa: BLE001 - 解析器千奇百怪，不许崩
        return None
    # 退化路径：解析器暴露的是 name_at/fname（拿索引，不是拿 UObject*）
    for meth in ("name_at", "fname"):
        fn = getattr(fname, meth, None)
        if fn is None:
            continue
        tok = loc.m.i32(uclass + B.OFF_UOBJECT_NAME)
        if tok is None:
            return None
        try:
            return fn(tok & 0xFFFF) or fn((tok >> 16) & 0xFFFF)
        except Exception:                # noqa: BLE001
            return None
    return None


def rendered_cards(session, fname=None, snapshot=None) -> list:
    """列出当前**屏幕上摆着的每一张卡**（`ABP_BaseCard_C` 及子类 actor）。不读图。

    参数
    ----
    session   : `kardsmem.proc.Session`
    fname     : 可选，`names.FNamePool` 实例（用来填 `class_name`）。
                为 None 时尝试自建；`names.py` 不存在则 `class_name` 留 None。
    snapshot  : 可选，`session.snapshot()` 的结果。传了就顺手做匹配；
                为 None 则**不匹配**（`base_card` 全 None，避免偷偷多读一次）。

    返回按 `ULevel::Actors` 下标升序的 `list[RenderedCard]`。
    """
    m = session.m
    loc = Locator(m, session.base)
    names_mod = _load_names()

    # 自动找 FName 解析器（names.py 缺失时静默放弃）
    if fname is None and names_mod is not None:
        try:
            pool = names_mod.FNamePool(m, session.base)
            fname = pool
        except Exception:                # noqa: BLE001
            fname = None

    out = []
    for idx, a in enumerate(loc.actors()):
        # 先过 CDO 闸（CDO 也在 GObjects 里，但保险起见；见 Locator.is_cdo）
        if loc.is_cdo(a):
            continue
        if not loc.derives_from(a, SIZE_ACTOR):
            continue

        notes = {}
        cid = m.i32(a + OFF_CARDID)
        if cid is None:
            notes["card_id"] = "读不出（i32 失败）"
        ref = m.ptr(a + OFF_SELFREF)
        if ref is None:
            notes["self_ref"] = "读不出（ptr 失败）"
            ref = 0
        elif ref == 0:
            notes["self_ref"] = "空指针（该屏卡的 UBaseCardObject 未绑定）"

        diag = {}
        nm = read_card_name(m, ref, diag) if ref else None
        if ref and nm is None:
            notes["name"] = "FText 读不出（self_ref=0x%X）" % ref
        notes.update(diag)

        uclass = loc.uclass_of(a)
        ps = loc.class_propsize(uclass)
        cn = _class_name_of(loc, uclass, fname)
        if cn is None and names_mod is None:
            notes["class_name"] = ("kardsmem.names 不存在（可选模块）→ class_name=None，"
                                   "**不猜**（UClass 的 FName 在 uclass+0x%X）"
                                   % B.OFF_UOBJECT_NAME)
        elif cn is None:
            notes["class_name"] = ("FName 解析器返回 None（names.py 在但没解出这个名字，"
                                   "token=0x%s）"
                                   % ("%08X" % (m.i32(uclass + B.OFF_UOBJECT_NAME) & 0xFFFFFFFF)
                                      if m.i32(uclass + B.OFF_UOBJECT_NAME) is not None
                                      else "读不出"))

        kind = _classify(cid, ref, notes)
        out.append(RenderedCard(actor=a, card_id=cid, self_ref=ref, name=nm,
                                class_name=cn, kind=kind, propsize=ps,
                                uclass=uclass, index=idx, notes=notes))

    if snapshot is not None:
        match_to_board(out, snapshot)
    return out


def _classify(card_id: Optional[int], self_ref: int, notes: dict) -> str:
    """`kind` 的唯一判据处（依据写在模块 docstring 里）。

    * `self_ref == 0` 或 `card_id == 0` → `"candidate"`（身份读不出，如天气一级三选一）
    * 两者都有效                        → `"bound"`（身份确定；**位置不断言**）
    * `card_id` 读不出（None）而 ref 有效 → 不敢断言，回 `"ui_or_hand_or_board"`
    """
    if self_ref == 0:
        return KIND_CANDIDATE
    if card_id is None:
        notes.setdefault("kind", "CardID 读不出 → 不敢细分，回 %s" % KIND_UNKNOWN)
        return KIND_UNKNOWN
    if card_id == 0:
        return KIND_CANDIDATE
    return KIND_BOUND


# --------------------------------------------------------------------------
# 与 board snapshot 配对
# --------------------------------------------------------------------------
def match_to_board(rendered, snapshot) -> None:
    """把 `rendered` 与 board snapshot 里的卡对上（**原地**写回 `base_card`）。

    配对顺序（越靠前越硬）：
      1. `CardID`（对局内临时实例 id）—— 唯一，最硬；
      2. `name`（FText 明文）—— 可能一对多，取第一个并把 `matched_by="name"` 标出来；
      3. 都不中 → `base_card = None`（未匹配，**不许瞎指**）。
    """
    cards = list(getattr(snapshot, "cards", None) or [])
    by_id = {}
    by_name = {}
    for c in cards:
        cid = getattr(c, "card_id", None)
        if cid is not None:
            by_id.setdefault(cid, c)
        nm = getattr(c, "name", None)
        if nm:
            by_name.setdefault(nm, c)

    for r in rendered:
        r.base_card = None
        r.matched_by = None
        if r.card_id is not None:
            c = by_id.get(r.card_id)
            if c is not None:
                r.base_card, r.matched_by = c, "card_id"
                continue
        if r.name:
            c = by_name.get(r.name)
            if c is not None:
                r.base_card, r.matched_by = c, "name"
                continue
        # 诊断：说不清为什么不匹配的，把线索留下
        if r.card_id is None and not r.name:
            r.notes.setdefault("match", "CardID 与 name 都读不出 → 无从匹配")
        elif r.card_id is None:
            r.notes.setdefault("match", "CardID 读不出，name=%r 在盘面里没有" % r.name)
        elif not r.name:
            r.notes.setdefault("match", "CardID=%d 不在盘面 AllCardsInBattle 里" % r.card_id)
        else:
            r.notes.setdefault("match", "CardID=%d / name=%r 都不在盘面里" % (r.card_id, r.name))


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------
def summary(rendered) -> dict:
    """给 `rendered` 一张汇总表（给调用方/测试看，不用它做判断）。"""
    by_kind = {}
    by_loc = {}
    matched = unmatched = 0
    have_id = have_name = 0
    unreadable = []
    for r in rendered:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        if r.card_id is not None:
            have_id += 1
        if r.name:
            have_name += 1
        if r.base_card is not None:
            matched += 1
            key = "%s/%s" % (getattr(r.base_card, "side", "?"),
                             getattr(r.base_card, "location", "?"))
            by_loc[key] = by_loc.get(key, 0) + 1
        else:
            unmatched += 1
            unreadable.append({"actor": hex(r.actor), "card_id": r.card_id,
                               "name": r.name, "kind": r.kind,
                               "notes": dict(r.notes)})
    return {
        "total": len(rendered),
        "by_kind": by_kind,
        "by_matched_location": by_loc,
        "matched": matched,
        "unmatched": unmatched,
        "card_id_readable": have_id,
        "name_readable": have_name,
        "unmatched_detail": unreadable,
        "name_from": sorted({r.notes.get("name_from") for r in rendered
                             if r.notes.get("name_from")}),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _snapshot_safe(session):
    """取快照；失败返回 (None, 原因)。**不吞掉原因** —— 输出里要如实写。"""
    try:
        return session.snapshot(), None
    except Exception as e:               # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        s = P.attach()
    except Exception as e:               # noqa: BLE001
        print("attach 失败：%s: %s" % (type(e).__name__, e))
        return 2

    print("== kardsmem.rendered ==")
    print("session: %s" % s.describe())
    try:
        loc = Locator(s.m, s.base)
        print("world=0x%X persistent_level=0x%X level_actors=%d"
              % (loc.world, loc.persistent_level, len(loc.actors())))

        snap, snap_err = _snapshot_safe(s)
        if snap is None:
            print("snapshot 失败（%s）→ 本次不做配对，base_card 全为 None" % snap_err)

        rc = rendered_cards(s, snapshot=snap)
        print("--- 屏幕卡（ABP_BaseCard_C 家族，propsize==0x%X）---" % SIZE_ACTOR)
        for r in rc:
            print("  " + r.line())

        sm = summary(rc)
        print("--- 汇总 ---")
        print("屏幕卡合计     = %d" % sm["total"])
        print("kind 分布      = %s" % sm["by_kind"])
        print("CardID 可读    = %d / %d" % (sm["card_id_readable"], sm["total"]))
        print("name   可读    = %d / %d   来源=%s"
              % (sm["name_readable"], sm["total"], sm["name_from"] or "-"))
        if snap is None:
            print("匹配           = 跳过（snapshot 不可用：%s）" % snap_err)
        else:
            print("匹配上 %d / 未匹配 %d（盘面卡共 %d 张，匹配键：card_id → name）"
                  % (sm["matched"], sm["unmatched"], len(snap.cards)))
            print("匹配到的位置分布 = %s" % sm["by_matched_location"])
            if sm["unmatched_detail"]:
                print("未匹配明细：")
                for u in sm["unmatched_detail"]:
                    print("  %s CardID=%s name=%r kind=%s notes=%s"
                          % (u["actor"], u["card_id"], u["name"], u["kind"], u["notes"]))
            # 反向：盘面上有、屏幕上没有（只对不在牌库/弃牌堆的卡有意义）
            on_screen_uid = {getattr(r.base_card, "uid", None) for r in rc if r.base_card}
            miss = [c for c in snap.cards
                    if c.uid not in on_screen_uid
                    and getattr(c, "location", None) in ("hand", "frontline", "back", "hq")]
            print("盘面有/屏幕无（hand+场上） = %d 张" % len(miss))
            for c in miss:
                print("  uid=%s side=%s loc=%s slot=%s CardID=%s name=%r"
                      % (c.uid, c.side, c.location, c.slot, c.card_id, c.name))
        print("turn=%s our_turn=%s match_finished=%s"
              % (None if snap is None else snap.turn,
                 None if snap is None else snap.our_turn,
                 None if snap is None else snap.match_finished))
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
