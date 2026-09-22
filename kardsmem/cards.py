#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.cards —— 盘面卡的枚举与**原始读数**层。

分层说明
========
- "归一化盘面"（`Card` / `BoardState`，双后端可换）的唯一实现在
  `OCR-Kards-Auto/src/board_api.py`，本模块**不重写**它：
  `cards()` / `snapshot()` 直接调它的 mem 后端。
- 本模块补的是**它没有的原始读数**：把一个 `UBaseCardObject` 的全部字段
  （明文身份 + 5 条加密记录 + FText 文本 + FName 资产名）一次性摊开，
  并且**逐字段说明可信度**。CLI 的 `card` / `cards --raw` 用它——
  它取代了以前散落的一次性脚本 `card_dump.py` / `board_dump.py`。

踩过的坑（写在这里，别重犯）
============================
1. `AllCardsInBattle` 元素 **24 字节**（不是 16）：`key@0 | pad@4 | ptr@8 | HashNextId@0x10 | HashIndex@0x14`。
2. `ArrayNum` **含墓碑** ⇒ 必须用"指针 + 类身份（SuperStruct 链到 `UBaseCardObject`）+ 排除 CDO"过滤。
3. 卡牌当前数值**只在加密记录里**；明文 `0x6C/0x70/0x74/0x80/0x84` 是**模板值**。
4. 加密记录必须**原子读** `card+0x568..0x5E0`（`MemRO.atomic`）。
5. 弃牌堆里的阵亡单位 `defense` 是**负值**（游戏自己夹到 0）。
"""

from __future__ import annotations

import struct
from typing import Optional

from . import build as B
from .proc import board_api
from .world import Locator

SIZE_BASECARD = board_api.SIZE_BASECARD              # 1656
OFF_ALLCARDSINBATTLE = board_api.OFF_ALLCARDSINBATTLE  # GS+0x538
TMAP_ELEM_SIZE = board_api.TMAP_ELEM_SIZE            # 24
TMAP_VALUE_OFF = board_api.TMAP_VALUE_OFF            # 0x08
CARD_I32 = board_api.CARD_I32
CARD_U8 = board_api.CARD_U8
CARD_RECORDS = board_api.CARD_RECORDS
CARD_REC_LO, CARD_REC_HI = board_api.CARD_REC_LO, board_api.CARD_REC_HI
CARD_KEY_OFF = board_api.CARD_KEY_OFF
CARD_TAMPER_FLAG_OFF = board_api.CARD_TAMPER_FLAG_OFF
OFF_CARD_TITLE = board_api.OFF_CARD_TITLE
OFF_CARD_TEXT = 0x90
OFF_CARD_FLAVOR = 0xA0
OFF_CARD_CUSTOM_JSON = 0x518

# ---- ★ 逐实例的"实时效果 / 被贴的效果"（不是模板值）----------------------------
# 布局来源：Dumper-7 SDK `kards_classes.hpp`（`class UBaseCardObject`，第 2467 行起）；
# 与运行时已验证过的偏移（`CardID@0x264`、`key@0x55C`、`customJson@0x518`）一致。
#   +0x00B8  TMap<int32, FCardBuffData>         buffsFromCards             (0x50)
#   +0x0108  TArray<int32>                      cardsBuffedByThisCard      (0x10)
#   +0x0208  TMap<FString, FCardsGivingAbility> receivedAbilitiesFromCards (0x50)
#   +0x0504  FName customName1 / +0x050C customName2 / +0x0518 customJson
#   FCardBuffData       = { FText cardName@0x00; TMap<FString,int32> BuffMap@0x10 }   size 0x60
#   FCardsGivingAbility = { TArray<int32> CardsGivingAbility@0x00 }                   size 0x10
# TMap 元素 = { TPair; int32 HashNextId; int32 HashIndex } ⇒ 步长：
#   TMap<int32,FCardBuffData>          pair 4+pad4+0x60 = 0x68 → 元素 **0x70**
#   TMap<FString,int32>                pair 16+4 对齐到 24    → 元素 **0x20**
#   TMap<FString,FCardsGivingAbility>  pair 16+16 = 32        → 元素 **0x28**
#
# 机制（FModel 导出 cpp 实证：`card_event_order_of_the_day` / `card_event_grim_day`）：
#   cardFunction->AddKreditsTax(卡, ±N, 给效果的卡ID)  → 记进 buffsFromCards（**指向税就是这么"贴"的**）
#   cardFunction->CustomAbilityAdd("passive", 卡ID, 给效果的卡ID) / CustomAbilityRemove / HasCustomAbilityFromCard
# ⇒ **模板值 `KreditsTax_AsEnemyTarget@0x88` ≠ 实际税**：实际值还要加上 buffsFromCards 里的增幅。
OFF_BUFFS_FROM_CARDS = 0x00B8
OFF_CARDS_BUFFED_BY_THIS = 0x0108
#   免疫（SDK 0x0289 / 0x0290）：GiveImmune 会置 isImmune=true 并把 giver 记进 cardsGivingImmunity，
#   同时在 buffsFromCards 里记一条名为 "immune" 的 buff（和 Suppress 同一套路）。
OFF_IS_IMMUNE = 0x0289
OFF_CARDS_GIVING_IMMUNITY = 0x0290
OFF_RECEIVED_ABILITIES = 0x0208
OFF_CUSTOM_NAME1 = 0x0504
OFF_CUSTOM_NAME2 = 0x050C
SIZE_CARD_BUFF_DATA = 0x60
OFF_CBD_CARD_NAME = 0x00
OFF_CBD_BUFF_MAP = 0x10
STRIDE_TMAP_INT_BUFFDATA = 0x70
STRIDE_TMAP_FSTRING_INT = 0x20
STRIDE_TMAP_FSTRING_ABILITY = 0x28

_decrypt = board_api._decrypt
LOCATION_NAMES = board_api.LOCATION_NAMES
CARD_TYPES = board_api.CARD_TYPES
SIDE_ENUM = board_api.SIDE_ENUM

# AllCardsInBattle 里混着的两条占位条目（Location/Type 全 0、加密记录无效）
PLACEHOLDER_KEYS = (30000000, 60000000)


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
def enumerate_raw(session, skip_placeholders: bool = True) -> list:
    """`AllCardsInBattle` → [(map_key, card_ptr)]。

    只做"这个指针是不是一张真的 UBaseCardObject"的判据，不做别的解释。
    `skip_placeholders=True` 时丢掉那两条 `key in (30000000, 60000000)` 的占位条目
    （它们的 Location/Type 都是 0、加密记录无效，board_api 也把它们当无名条目丢掉）。
    """
    loc = Locator(session.m, session.base)
    gs = loc.gamestate
    if not gs:
        return []
    m = session.m
    addr = gs + OFF_ALLCARDSINBATTLE
    data = m.ptr(addr)
    n = m.i32(addr + 8)
    out = []
    if not data or not n or not (0 < n < 100000):
        return out
    elems = m.read_exact(data, n * TMAP_ELEM_SIZE)
    if not elems:
        return out
    cache, seen = {}, set()
    for off in range(0, len(elems) - TMAP_ELEM_SIZE + 1, TMAP_ELEM_SIZE):
        key = struct.unpack_from("<i", elems, off)[0]
        p = struct.unpack_from("<Q", elems, off + TMAP_VALUE_OFF)[0]
        if not (board_api.PTR_MIN <= p <= board_api.PTR_MAX) or p % 8 or p in seen:
            continue
        seen.add(p)
        if skip_placeholders and key in PLACEHOLDER_KEYS:
            continue
        if loc.is_cdo(p):
            continue
        if loc.derives_from(p, SIZE_BASECARD, cache=cache):
            out.append((key, p))
    return out


# --------------------------------------------------------------------------
# 单卡原始读数
# --------------------------------------------------------------------------
def _ftext(mem, addr: int) -> Optional[str]:
    return board_api._read_card_name(mem, addr - OFF_CARD_TITLE) if addr else None


def _ftext_direct(mem, addr: int) -> Optional[str]:
    """直接给一个 **FText 地址**（不是"卡+0x58"）解出明文。
    用于 `FCardBuffData.cardName` 这类内嵌 FText。"""
    td = mem.ptr(addr)
    if not td:
        return None
    for off in (0x20, 0x18, 0x28, 0x30, 0x38, 0x10, 0x40):
        s = board_api._fstring(mem, td + off)
        if s:
            return s
    return None


# --------------------------------------------------------------------------
# ★ 实时效果（逐实例"被贴的效果"）—— 布局见文件开头注释
# --------------------------------------------------------------------------
def _tmap(mem, addr: int, stride: int, cap: int = 512):
    """读一个 TMap/TSet：返回 (data_ptr, num, blob)。读不出返回 (0, None, None)。"""
    data = mem.ptr(addr)
    num = mem.i32(addr + 8)
    if not data or not num or num <= 0 or num > cap:
        return 0, num, None
    blob = mem.read_exact(data, num * stride)
    if blob is None or len(blob) != num * stride:
        return data, num, None
    return data, num, blob


def _fstring_at(mem, addr: int) -> Optional[str]:
    return board_api._fstring(mem, addr)


def _read_buff_map(mem, addr: int) -> dict:
    """`FCardBuffData.BuffMap`：TMap<FString /*能力名*/, int32 /*数值*/>。"""
    data, num, blob = _tmap(mem, addr, STRIDE_TMAP_FSTRING_INT)
    out = {}
    if blob is None:
        return out
    for off in range(0, len(blob) - STRIDE_TMAP_FSTRING_INT + 1, STRIDE_TMAP_FSTRING_INT):
        name = _fstring_at(mem, data + off)
        val = struct.unpack_from("<i", blob, off + 0x10)[0]
        if name:
            out[name] = val
    return out


def read_buffs_from_cards(session, ptr: int) -> list:
    """`buffsFromCards`(0xB8)：**谁给这张卡贴了什么**。

    `TMap<int32 /*给效果的卡 CardID*/, FCardBuffData{cardName, TMap<FString,int32> BuffMap}>`
    `AddKreditsTax(target, ±N, giverID)` 就是往这里记 —— 所以**指向税的实际值**在这里，
    而不是模板字段 `KreditsTax_AsEnemyTarget@0x88`。
    """
    mem = session.m
    data, num, blob = _tmap(mem, ptr + OFF_BUFFS_FROM_CARDS, STRIDE_TMAP_INT_BUFFDATA)
    rows = []
    if blob is None:
        return rows
    for off in range(0, len(blob) - STRIDE_TMAP_INT_BUFFDATA + 1, STRIDE_TMAP_INT_BUFFDATA):
        giver = struct.unpack_from("<i", blob, off)[0]
        base = data + off + 0x08                      # FCardBuffData 起点
        rows.append({
            "giver_card_id": giver,
            "giver_name": _ftext_direct(mem, base + OFF_CBD_CARD_NAME),
            "buffs": _read_buff_map(mem, base + OFF_CBD_BUFF_MAP),
        })
    return rows


def read_cards_buffed_by_this_card(session, ptr: int) -> list:
    """`cardsBuffedByThisCard`(0x108)：这张卡给别人贴过什么（对方的 CardID 列表）。"""
    mem = session.m
    data = mem.ptr(ptr + OFF_CARDS_BUFFED_BY_THIS)
    num = mem.i32(ptr + OFF_CARDS_BUFFED_BY_THIS + 8)
    if not data or not num or num <= 0 or num > 512:
        return []
    blob = mem.read_exact(data, num * 4)
    if blob is None:
        return []
    return list(struct.unpack_from("<%di" % num, blob))


def read_received_abilities(session, ptr: int) -> list:
    """`receivedAbilitiesFromCards`(0x208)：**这张卡被贴了哪些"能力"、谁给的**。

    `TMap<FString /*能力名，如 "passive"*/, FCardsGivingAbility{TArray<int32> CardsGivingAbility}>`
    对应 `CustomAbilityAdd/Remove` 与 `HasCustomAbilityFromCard`。
    """
    mem = session.m
    data, num, blob = _tmap(mem, ptr + OFF_RECEIVED_ABILITIES, STRIDE_TMAP_FSTRING_ABILITY)
    rows = []
    if blob is None:
        return rows
    for off in range(0, len(blob) - STRIDE_TMAP_FSTRING_ABILITY + 1, STRIDE_TMAP_FSTRING_ABILITY):
        name = _fstring_at(mem, data + off)
        arr = data + off + 0x10
        p = mem.ptr(arr)
        n = mem.i32(arr + 8)
        givers = []
        if p and n and 0 < n <= 128:
            b = mem.read_exact(p, n * 4)
            if b:
                givers = list(struct.unpack_from("<%di" % n, b))
        if name:
            rows.append({"ability": name, "givers": givers})
    return rows


def read_live_effects(session, ptr: int) -> dict:
    """★ 一张卡**此刻**被贴的效果（不是模板值）。

    返回：`buffs_from_cards` / `cards_buffed_by_this_card` / `received_abilities` /
    `custom_name1` / `custom_name2` / `custom_json_ptr`，
    以及按 buff 名里含 "tax" 的条目算出的 `kredits_tax_extra`（指向税增量估计）。
    """
    mem = session.m
    out = {
        "buffs_from_cards": read_buffs_from_cards(session, ptr),
        "cards_buffed_by_this_card": read_cards_buffed_by_this_card(session, ptr),
        "received_abilities": read_received_abilities(session, ptr),
        "custom_name1": None, "custom_name2": None,
        "custom_json_ptr": None,
        "kredits_tax_template": mem.i32(ptr + CARD_I32["kredits_tax"]),
        "kredits_tax_extra": None,
        "kredits_tax_live_estimate": None,
    }
    td = mem.ptr(ptr + OFF_CARD_CUSTOM_JSON)
    out["custom_json_ptr"] = ("0x%X" % td) if td else None
    # customName1/2 是 FName → 需要名字池（读不出就留 None，不影响其它字段）
    try:
        pool = session.names_pool()
        for key, off in (("custom_name1", OFF_CUSTOM_NAME1), ("custom_name2", OFF_CUSTOM_NAME2)):
            idx = mem.i32(ptr + off)
            num = mem.i32(ptr + off + 4)
            if idx and idx > 0:
                out[key] = pool.fname(idx, num or 0)
    except Exception:                                        # noqa: BLE001
        pass
    extra = 0
    found = False
    for row in out["buffs_from_cards"]:
        for name, val in (row.get("buffs") or {}).items():
            if "tax" in name.lower():
                extra += val
                found = True
    out["kredits_tax_extra"] = extra if found else 0
    if out["kredits_tax_template"] is not None:
        out["kredits_tax_live_estimate"] = out["kredits_tax_template"] + (extra if found else 0)
    # ★ 用户权威规则：**被抑制的卡失去全部效果** —— 数据可能还在，但游戏不再生效。
    #   所以 `kredits_tax_live_estimate` 只在 `effects_active` 为真时才算数。
    sup = mem.u8(ptr + CARD_U8["is_suppressed"])
    out["is_suppressed"] = None if sup is None else bool(sup)
    out["effects_active"] = None if sup is None else (not bool(sup))
    # ★ 免疫（百科：「单位或总部具有免疫时不会受到伤害」）—— 可被指定，但**不掉血**。
    imm = mem.u8(ptr + OFF_IS_IMMUNE)
    out["is_immune"] = None if imm is None else bool(imm)
    givers = read_cards_giving_immunity(session, ptr)
    out["cards_giving_immunity"] = givers
    out["immune_buff"] = [b for b in (out.get("buffs_from_cards") or [])
                          if any("immune" in k.lower() for k in (b.get("buffs") or {}))]
    return out


def read_cards_giving_immunity(session, ptr: int) -> list:
    """`cardsGivingImmunity`(0x290)：谁给了这张卡免疫（TArray<int32>）。"""
    mem = session.m
    addr = ptr + OFF_CARDS_GIVING_IMMUNITY
    data = mem.ptr(addr)
    num = mem.i32(addr + 8)
    if not data or not num or num <= 0 or num > 256:
        return []
    b = mem.read_exact(data, num * 4)
    return list(struct.unpack_from("<%di" % num, b)) if b else []


def has_live_effects(eff: dict) -> bool:
    """这张卡身上有没有"非模板"的东西（决定要不要打印它）。"""
    return bool(eff.get("buffs_from_cards") or eff.get("cards_buffed_by_this_card")
                or eff.get("received_abilities") or eff.get("custom_name1")
                or eff.get("custom_name2"))


# --------------------------------------------------------------------------
# ★ 卡级"不能做某事"的限制（导出实证：CustomName1Add / CustomAbilityAdd / 卡面 customName1）
#   标记族见 `python tools/card_coverage.py` 之外的一次性统计：
#     cantAttack / cantAttack:location / cantMove / cantRetreat / cantBePinned /
#     cantBeSuppressed / cantBeAttackedBy:ground / cantBeTargetedByEnemyOrder / cant_target_suppressed
#   落点：① 卡面 FName customName1/2（静态；如 28cm_coasttal_howitzer 的 cantAttack:location）
#         ② receivedAbilitiesFromCards（运行时 CustomAbilityAdd；如 fw_200_condor 的 cantAttack:location）
#         ③ buffsFromCards 的 BuffMap 键
#   原生判据（在 exe 里，读的也是这些）：HasCantAttack / HasCantAttackType("air"|"ground"|<类型>) /
#   HasCantBeAttackedBy(unitType)；wrapper 在 `Content\Library\cardsCheckFunctions.cpp::CanAttack`。
# --------------------------------------------------------------------------
RESTRICTION_MARKERS = {
    "cantattack:location": "不能攻击总部",
    "cantbeattackedby": "不能被某类单位攻击（:ground 等）",
    "cantbetargetedbyenemyorder": "不能被敌方指令指定",
    "cantbesuppressed": "不能被抑制",
    "cantbepinned": "不能被压制",
    "cantretreat": "不能撤退",
    "cant_target_suppressed": "不能被指定为抑制目标",
    "ignorecantattack_location": "无视『不能攻击总部』",
    "cantattack": "不能攻击",
    "cantmove": "不能移动",
}


def _match_markers(low: str) -> list:
    """在字符串里找标记；**长标记优先**（`cantattack:location` 不会再算成 `cantattack`）。"""
    hits, consumed = [], [False] * len(low)
    for mk in sorted(RESTRICTION_MARKERS, key=len, reverse=True):
        i = 0
        while True:
            j = low.find(mk, i)
            if j < 0:
                break
            if not any(consumed[j:j + len(mk)]):
                hits.append(mk)
                for k in range(j, j + len(mk)):
                    consumed[k] = True
            i = j + 1
    return hits


def restriction_flags(eff: dict) -> dict:
    """从 `read_live_effects()` 的结果里提取"不能做某事"的标记。

    返回 `{标记: [来源, ...]}`（长标记优先，所以 `cantattack:location` 不会重复计进 `cantattack`），
    外加 `_found`（命中的标记列表）。
    """
    found = {}
    for src, val in (("customName1", eff.get("custom_name1")),
                     ("customName2", eff.get("custom_name2"))):
        if val:
            for mk in _match_markers(str(val).lower()):
                found.setdefault(mk, []).append(src)
    for ab in eff.get("received_abilities") or []:
        for mk in _match_markers((ab.get("ability") or "").lower()):
            found.setdefault(mk, []).append("ability:%s" % ab.get("ability"))
    for b in eff.get("buffs_from_cards") or []:
        for k in (b.get("buffs") or {}):
            for mk in _match_markers(k.lower()):
                found.setdefault(mk, []).append("buff:%s" % k)
    out = {mk: found.get(mk, []) for mk in RESTRICTION_MARKERS}
    out["_found"] = sorted(found)
    return out


def action_blockers(session, card_uid_or_ptr, turn: Optional[int] = None) -> dict:
    """★ 回答"这个单位现在能不能攻击 / 能不能移动"，并给出理由。

    组合（全部读内存）：
      1. **部署当回合**：`enter_play_on_turn == turn` 且**无闪击** ⇒ 不能动（权威；计数器不算数）
      2. **压制（pin）**：`receivedAbilitiesFromCards['pinned']` / `combat_pinned` ⇒ 不能移动或攻击
      3. **卡级限制**：`cantAttack` / `cantAttack:location` / `cantMove` / `cantRetreat` …
         —— ⚠ **被抑制时这些限制也一并失效**（抑制=失去所有关键词和效果）
      4. **位置/兵种**：支援阵线的地面单位不能攻击、步兵只能打相邻战线…（⚠ 这一层还没实机验证，
         所以这里只标注 `location_rule_checked=False`，不要当权威）
    """
    out = {"can_attack": None, "can_move": None, "reasons": [],
           "location_rule_checked": False}
    try:
        from .proc import attach
        s = session
        ptr = card_uid_or_ptr
        if isinstance(card_uid_or_ptr, str):
            c = find(s, uid=card_uid_or_ptr)
            ptr = (c.raw or {}).get("ptr") if c else None
            d = read_raw(s, ptr) if ptr else {}
            eff = read_live_effects(s, ptr) if ptr else {}
        else:
            d = read_raw(s, ptr)
            eff = read_live_effects(s, ptr)
    except Exception as e:                                   # noqa: BLE001
        out["reasons"].append("读不出：%s" % e)
        return out

    sup = eff.get("is_suppressed")
    out["is_suppressed"] = sup
    kw = d.get("keywords") or []
    if sup:
        out["reasons"].append("被抑制 ⇒ 关键词与效果全部失效（限制也随之失效）")
    # 1) 部署当回合
    ept = d.get("enter_play_turn")
    if turn is not None and ept is not None and ept == turn and "blitz" not in kw:
        out["reasons"].append("本回合刚部署且无闪击 ⇒ 不能移动/攻击")
        out["can_attack"] = out["can_move"] = False
    # 2) 压制
    pinned = False
    for ab in eff.get("received_abilities") or []:
        if "pinned" in (ab.get("ability") or "").lower():
            pinned = True
    for b in eff.get("buffs_from_cards") or []:
        if any("pinned" in k.lower() for k in (b.get("buffs") or {})):
            pinned = True
    if pinned and not sup:
        out["reasons"].append("被压制（pin）⇒ 不能移动或攻击")
        out["can_attack"] = out["can_move"] = False
    # 3) 卡级限制（被抑制时失效）
    rf = restriction_flags(eff)
    out["restrictions"] = rf
    if not sup:
        if rf["cantattack"]:
            out["reasons"].append("卡级限制：%s（来源 %s）" % (RESTRICTION_MARKERS["cantattack"], rf["cantattack"]))
            out["can_attack"] = False
        if rf["cantmove"]:
            out["reasons"].append("卡级限制：%s（来源 %s）" % (RESTRICTION_MARKERS["cantmove"], rf["cantmove"]))
            out["can_move"] = False
        if rf["cantattack:location"]:
            out["reasons"].append("卡级限制：不能攻击总部（cantAttack:location）")
    if out["can_attack"] is None:
        out["can_attack"] = True
    if out["can_move"] is None:
        out["can_move"] = True
    return out


# 允许类标记（拥有它就能突破某条限制）
PERMISSION_MARKERS = {
    "cantargetcovert": "可以指定隐蔽单位",
    "ignorecantattack_location": "无视『不能攻击总部』",
}


def target_blockers(session, defender_uid_or_ptr, attacker_type: Optional[str] = None,
                    attacker_ptr: Optional[int] = None) -> dict:
    """★ **防御侧**："为什么不能攻击这个目标"（读侧判据，全部来自百科原文 + 导出标记）。

    * **烟幕** `has_smokescreen` ⇒ 百科「烟幕单位**无法被敌方单位攻击**」（它移动/攻击后失去烟幕）
    * **隐蔽** `has_covert` ⇒ 百科「揭示前不受指令、反制或单位效果影响」；攻击方需带
      `canTargetCovert`（导出里有 5 处）才能指定
    * **被守护** ⇒ 百科「被守护单位只能被轰炸机和炮兵攻击」（相邻规则由调用方按行判定）
    * `cantBeAttackedBy:<type>` ⇒ 该类单位不能攻击它（导出 6 处，如 `:ground`）
    * **被抑制** ⇒ 百科不在其列，且 `CanSelectAsTarget` 是 `isSuppressed || canIt` ⇒ **放行**
      （被抑制的卡失去关键词，烟幕/守护也随之为空）
    """
    out = {"can_be_attacked": None, "blocked": False, "reasons": [], "flags": {}}
    try:
        s = session
        ptr = defender_uid_or_ptr
        if isinstance(defender_uid_or_ptr, str):
            c = find(s, uid=defender_uid_or_ptr)
            ptr = (c.raw or {}).get("ptr") if c else None
        if not ptr:
            out["reasons"].append("定位不到目标实例")
            return out
        d = read_raw(s, ptr)
        eff = read_live_effects(s, ptr)
    except Exception as e:                                   # noqa: BLE001
        out["reasons"].append("读不出：%s" % e)
        return out

    kw = d.get("keywords") or []
    sup = d.get("is_suppressed")
    out["is_suppressed"] = sup
    out["flags"] = {"smokescreen": bool(d.get("keyword_flags", {}).get("has_smokescreen")),
                    "covert": bool(d.get("keyword_flags", {}).get("has_covert")),
                    "guard": bool(d.get("keyword_flags", {}).get("has_guard")),
                    "suppressed": bool(sup),
                    "immune": bool(eff.get("is_immune"))}
    atk = (attacker_type or "").lower()
    perms = []
    if attacker_ptr:
        try:
            rfa = restriction_flags(read_live_effects(s, attacker_ptr))
            perms = rfa.get("_found", []) + _match_markers(
                str((read_live_effects(s, attacker_ptr).get("custom_name1") or "")).lower())
        except Exception:                                    # noqa: BLE001
            perms = []
    has_covert_perm = any("cantargetcovert" in p.lower() for p in perms)

    if out["flags"]["suppressed"]:
        out["reasons"].append("目标被抑制 ⇒ 关键词失效（放行）")
    else:
        if out["flags"]["smokescreen"]:
            out["reasons"].append("烟幕：无法被敌方单位攻击（它移动/攻击后才会失去烟幕）")
        if out["flags"]["covert"] and not has_covert_perm:
            out["reasons"].append("隐蔽：揭示前不能被指定（除非攻击方带 canTargetCovert）")
        rfd = restriction_flags(eff)
        for mk in rfd.get("_found", []):
            if mk.startswith("cantbeattackedby"):
                out["reasons"].append("目标带 %s ⇒ 该类单位不能攻击它" % mk)
    out["can_be_attacked"] = not out["reasons"] or out["flags"]["suppressed"]
    out["blocked"] = not out["can_be_attacked"]
    # 免疫不是"不能指定"，而是"**可指定但不掉血**"（百科：「具有免疫时不会受到伤害」）
    out["damage_blocked"] = bool(out["flags"]["immune"])
    if out["damage_blocked"]:
        out["reasons"].append("目标**免疫**：可以打，但不会受到伤害（别把'没掉血'当失败）")
    return out


def read_raw(session, ptr: int, map_key: Optional[int] = None,
             with_text: bool = True, with_effects: bool = False) -> dict:
    """把一个 `UBaseCardObject` 摊开成 dict。**读不出的字段就是 None。**

    `trust` 字段说明每类值的可信度（明文模板 / 加密当前值 / 事件簿记）。
    `with_effects=True` 时额外读"逐实例被贴的效果"（`read_live_effects`，见其文档）。
    """
    m = session.m
    key = m.i32(ptr + CARD_KEY_OFF)
    rec = m.atomic(ptr + CARD_REC_LO, CARD_REC_HI - CARD_REC_LO)
    vals = {}
    if rec is not None:
        for nm, roff in CARD_RECORDS.items():
            X, Y, _Z, enc, _frame = struct.unpack_from("<5i", rec, roff - CARD_REC_LO)
            vals[nm] = _decrypt(key, X, Y, enc)
    # 额外：faction/rarity/isReserved/isInPermanentPool/EffectType
    # （SDK 偏移 0x7C/0x11C/0x132/0x133/0x135）—— 蓝图判据（IsValidHandTarget / CanPlayFromHand）要用，
    # board_api 的字段表里没有。
    u8 = {k: m.u8(ptr + o) for k, o in CARD_U8.items()}
    u8["faction_enum"] = m.u8(ptr + 0x7C)
    u8["rarity_enum"] = m.u8(ptr + 0x11C)
    u8["is_reserved"] = m.u8(ptr + 0x132)
    u8["is_in_permanent_pool"] = m.u8(ptr + 0x133)
    u8["effect_type"] = m.u8(ptr + 0x135)
    i32 = {k: m.i32(ptr + o) for k, o in CARD_I32.items()}
    loc_enum = u8.get("location_enum")
    type_enum = u8.get("type_enum")
    location = LOCATION_NAMES.get(loc_enum) if loc_enum is not None else None
    if location == "back" and type_enum == 1:
        location = "hq"
    d = {
        "ptr": ptr,
        "ptr_hex": "0x%X" % ptr,
        "map_key": map_key,
        "placeholder": bool(map_key in PLACEHOLDER_KEYS),
        "card_id": i32.get("card_id"),
        "name": board_api._read_card_name(m, ptr),
        "type_enum": type_enum,
        "card_type": CARD_TYPES.get(type_enum) if type_enum is not None else None,
        "side_enum": u8.get("side_enum"),
        "side": SIDE_ENUM.get(u8.get("side_enum")) if u8.get("side_enum") is not None else None,
        "location_enum": loc_enum,
        "location": location,
        "location_number": i32.get("location_number"),
        # ---- 当前值（加密记录，权威） ----
        "attack": vals.get("attack"),
        "attack_buff": vals.get("attackBuff"),
        "defense": vals.get("defense"),
        "kredit": vals.get("kredit"),
        "kredit_buff": vals.get("kreditBuff"),
        "total_attack": None if vals.get("attack") is None
        else min(99, max(0, (vals.get("attack") or 0) + (vals.get("attackBuff") or 0))),
        "total_kredit_cost": None if vals.get("kredit") is None and vals.get("kreditBuff") is None
        else min(99, (vals.get("kredit") or 0) + (vals.get("kreditBuff") or 0)),
        "total_defense": None if vals.get("defense") is None
        else min(99, max(0, vals.get("defense") or 0)),
        # ---- 明文（模板值 / 规则参数） ----
        "attack_plain": i32.get("attack_plain"),
        "defense_plain": i32.get("defense_plain"),
        "kredits_plain": i32.get("kredits_plain"),
        "operation_cost": i32.get("operation_cost"),
        "kredits_tax_as_enemy_target": i32.get("kredits_tax"),
        "heavy_armor": i32.get("heavy_armor"),
        "heavy_armor_buff": i32.get("heavy_armor_buff"),
        "total_heavy_armor": None if i32.get("heavy_armor") is None
        else min(3, max(0, (i32.get("heavy_armor") or 0) + (i32.get("heavy_armor_buff") or 0))),
        # ---- 行动能力（⚠ attack_left/movement_left 含 Fury、不含 Blitz；移动单向：支援→前线） ----
        "enter_play_turn": i32.get("enter_play_turn"),
        "movement_left": i32.get("movement_left"),
        "attack_left": i32.get("attack_left"),
        "has_attacked_this_turn": u8.get("has_attacked_this_turn"),
        "can_act_now": None,
        "max_attack": i32.get("max_attack"),
        "max_defense": i32.get("max_defense"),
        # ---- 规则/身份 ----
        "needs_hand_target": None if u8.get("needs_hand_target") is None
        else bool(u8.get("needs_hand_target")),
        "is_suppressed": None if u8.get("is_suppressed") is None else bool(u8.get("is_suppressed")),
        "is_revealed": None if u8.get("is_revealed") is None else bool(u8.get("is_revealed")),
        "choose_one_index": i32.get("choose_one_index"),
        "current_target": m.ptr(ptr + board_api.CARD_PTR["current_target"]),
        "target_override": m.ptr(ptr + board_api.CARD_PTR["target_override"]),
        "keywords": [k for k in ("has_deployment", "has_blitz", "has_ambush", "has_smokescreen",
                                 "has_mobilize", "has_alpine", "has_fury", "has_guard",
                                 "has_shock", "has_covert") if u8.get(k)],
        "keyword_flags": {k: u8.get(k) for k in CARD_U8 if k.startswith("has_")},
        "key": key,
        "tamper_flag": m.u8(ptr + CARD_TAMPER_FLAG_OFF),
        "records_ok": rec is not None,
        "trust": {
            "attack/defense/kredit": "加密当前值（原子读）" if rec is not None else "读不出",
            "attack_plain/defense_plain/kredits_plain": "模板值，别当当前值",
            "movement_left/attack_left": "含 Fury(奋战)不含 Blitz；判据用 enter_play_turn + has_blitz。移动单向：支援阵线→前线",
            "customJson": "逐实例簿记，不是身份",
        },
    }
    if d["current_target"] is not None:
        d["current_target_hex"] = "0x%X" % d["current_target"]
    if with_text:
        d["title"] = d["name"]
        d["text"] = _ftext(m, ptr + OFF_CARD_TEXT)
        d["flavor_text"] = _ftext(m, ptr + OFF_CARD_FLAVOR)
        # ⚠ customJson 是 `FBlueprintJsonObject`（0x10，内部 TSharedPtr<FJsonObject>），**不是 FText**。
        # 以前这里当 FText 读是错的；现在只暴露指针，要看内容得解 FJsonObject。
        cjp = m.ptr(ptr + OFF_CARD_CUSTOM_JSON)
        d["custom_json_ptr"] = ("0x%X" % cjp) if cjp else None
    if with_effects:
        d["live_effects"] = read_live_effects(session, ptr)
    return d


def can_act_now(card, turn: Optional[int], has_blitz: bool = False) -> Optional[bool]:
    """"这张场上的单位现在能不能动/攻击"的权威判据。

    游戏规则：「单位无法在部署的回合中移动或攻击。」而内存的
    `movementLeft/attackLeft` 对刚部署单位**仍读到 1** —— 所以判据是
    `enterPlayOnTurn` 与当前回合号比较（闪击例外）。
    `card` 可以是 `board_api.Card`（用 `.enter_play_on_turn` / `.keywords`）或 `read_raw` 的 dict。
    """
    if isinstance(card, dict):
        enter, kw = card.get("enter_play_turn"), card.get("keywords") or []
        atk_left = card.get("attack_left")
    else:
        enter, kw = getattr(card, "enter_play_on_turn", None), (getattr(card, "keywords", None) or [])
        atk_left = (getattr(card, "raw", None) or {}).get("attack_left")
    if enter is None or turn is None:
        return None
    if enter == turn and not (has_blitz or "blitz" in kw):
        return False
    return bool(atk_left is None or atk_left > 0)


# --------------------------------------------------------------------------
# 归一化视图（委托 board_api）
# --------------------------------------------------------------------------
def snapshot(session):
    return session.snapshot()


def cards(session) -> list:
    return snapshot(session).cards


def hand(session, side: str = "local") -> list:
    return sorted(snapshot(session).hand(side), key=lambda c: (c.slot if c.slot is not None else 0))


def discard(session, side: str = "local") -> list:
    return snapshot(session).discard(side)


def deck_cards(session, side: str = "local") -> Optional[list]:
    """**物理牌库**（同名牌保留多份）—— 这是 `AllCardsInBattle` 答不了的问题。

    `AllCardsInBattle` 的 key 是运行时 CardID，而同名蓝图的每一张实例有自己的
    CardID，所以**单看那张 map 会以为每个名字只有一份**。物理张数在
    `ABP_GameState_Battle_C::DeckCardIDs_Left(0x4A0) / Right(0x490)`：
    实测是 **`TArray<int32>`，元素 = 运行时 CardID**，且每个 id 都能在 map 里找到对应卡。

    返回 `[{"card_id", "name", "side", "location"}]`（保持牌库顺序、允许重复）；
    读不出返回 None。
    本函数用它取代了靠猜元素类型的做法。
    """
    from .gs import GameState
    g = GameState(session)
    ids = g.deck_ids(side)
    if not ids:
        return None
    st = snapshot(session)
    by_id = {}
    for c in st.cards:
        by_id.setdefault(c.card_id, c)
    out = []
    for i in ids:
        c = by_id.get(i)
        out.append({"card_id": i,
                    "name": (c.name if c else None),
                    "side": (c.side if c else side),
                    "location": (c.location if c else None),
                    "matched": c is not None})
    return out


def deck_multiset(session, side: str = "local") -> Optional[dict]:
    """牌库按名字计数（物理多份会 >1）。读不出返回 None。"""
    rows = deck_cards(session, side)
    if rows is None:
        return None
    out = {}
    for r in rows:
        key = r["name"] or ("<id %s>" % r["card_id"])
        out[key] = out.get(key, 0) + 1
    return out

def find(session, uid: Optional[str] = None, card_id: Optional[int] = None,
         name: Optional[str] = None, side: Optional[str] = None,
         location: Optional[str] = None):
    """按 uid / card_id / 名字（子串、不分大小写）找一张卡。找不到返回 None。"""
    want_uid = uid if isinstance(uid, str) else ("0x%X" % uid if uid is not None else None)
    for c in snapshot(session).cards:
        if want_uid and c.uid != want_uid:
            continue
        if card_id is not None and c.card_id != card_id:
            continue
        if name and (not c.name or name.upper() not in c.name.upper()):
            continue
        if side and c.side != side:
            continue
        if location and c.location != location:
            continue
        return c
    return None


def rows(session) -> dict:
    """纯内存的盘面位置图：每一行从左到右第几张是谁的。

    行 = `location`（back / frontline / hand / deck / discard），
    列 = `locationNumber`（实测同排内 mem slot 升序 ↔ 像素 cx 升序）。
    """
    st = snapshot(session)
    out = {}
    for c in st.cards:
        out.setdefault("%s/%s" % (c.side, c.location), []).append(c)
    for k in out:
        out[k].sort(key=lambda c: (c.slot if c.slot is not None else 0, c.card_id or 0))
    return out


def target_candidates(session, card) -> list:
    """**读侧近似**的"可指向目标"（R8）。

    ⚠ 这是**近似**，不是权威：游戏的合法性是三层谓词跑出来的，权威实现在
    shipping exe 里（`CanBeTargetted 0x4A7F910` / `CanOtherCardBeTargetted 0x4A7FC10`
    / `IsValidHandTarget 0x4A83370` / `CanPlayFromHand 0x4A7FEC0`）。
    本函数只把**读得到**的判据用上，用于决策与提示，不用于判定"游戏一定会接受"：
      1. 出牌卡自己 `CanPlayFromHand`（读不到）→ 放行；
      2. 对手卡先过 `is_suppressed || canIt`；被压制 ⇒ 放行；
      3. 费用 = 卡面费用 + Σ(目标 `KreditsTax_AsEnemyTarget@0x88`)；
      4. 位置/阵地可及性由调用方按兵种决定（空军可打后排/总部等）。
    """
    st = snapshot(session)
    my_side = card.side
    out = []
    for c in st.cards:
        if c.uid == card.uid:
            continue
        if c.location not in ("frontline", "back", "hq"):
            continue
        if c.side == my_side:
            continue
        entry = {"uid": c.uid, "name": c.name, "side": c.side, "location": c.location,
                 "slot": c.slot, "card_id": c.card_id,
                 "suppressed": c.is_suppressed,
                 "tax": c.kredits_tax_as_enemy_target,
                 "passes_suppressed_rule": True}
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# 打印
# --------------------------------------------------------------------------
def format_table(st, show_cards: bool = True) -> str:
    lines = ["turn=%s our_turn=%s our_side=%s complete=%s"
             % (st.turn, st.our_turn, st.our_side, st.complete),
             "kredits local=%-4s enemy=%-4s slots local=%-4s enemy=%-4s"
             % (st.kredits.get("local"), st.kredits.get("enemy"),
                st.slots.get("local"), st.slots.get("enemy")),
             "match_finished=%s frontline_owner=%s" % (st.match_finished, st.frontline_owner)]
    for side in ("local", "enemy"):
        hq = st.hq.get(side)
        lines.append("%-5s HQ=%-4s def=%-4s | hand=%-2d front=%-2d support=%-2d discard=%-2d deck=%d"
                     % (side, (hq.card_id if hq else "?"), (hq.defense if hq else "?"),
                        len(st.hand(side)), len(st.board(side)), len(st.support(side)),
                        len(st.discard(side)), len([c for c in st.cards
                                                    if c.side == side and c.location == "deck"])))
    if not show_cards:
        return "\n".join(lines)
    lines.append("%-26s %-6s %-10s %-11s %-6s %-4s %s" % ("name", "side", "loc", "type", "a/d", "cost", "kw"))
    for c in st.cards:
        lines.append("%-26s %-6s %-10s %-11s %-6s %-4s %s%s"
                     % ((c.name or "?")[:26], c.side, c.location, c.card_type or "?",
                        "%s/%s" % (c.attack, c.defense), c.kredit_cost,
                        " ".join(c.keywords),
                        ("  ->%s" % c.target_uid) if c.target_uid else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import json
    from .proc import attach
    ap = argparse.ArgumentParser(description="盘面卡（归一化 + 原始字段）")
    ap.add_argument("--raw", metavar="UID_OR_CARDID", nargs="?", const="*",
                    help="摊开原始字段；给 * 摊开全部")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rows", action="store_true", help="按行/列打印位置图")
    ap.add_argument("--deck", nargs="?", const="local", metavar="SIDE",
                    help="物理牌库（含同名多份）")
    a = ap.parse_args(argv)
    s = attach()
    if a.deck:
        deckrows = deck_cards(s, a.deck)
        if rows is None:
            print("牌库读不出（DeckCardIDs 为空 / 不在对局）")
            s.close()
            return 2
        for r in rows:
            print("  id=%-6s %-28s matched=%s" % (r["card_id"], r["name"], r["matched"]))
        ms = deck_multiset(s, a.deck)
        dups = {k: v for k, v in (ms or {}).items() if v > 1}
        print("== %s 牌库 %d 张，%d 个名字，其中多份的: %s"
              % (a.deck, len(rows), len(ms or {}), dups))
        s.close()
        return 0
    if a.rows:
        for row, cs in sorted(rows(s).items()):
            print("%-20s %s" % (row, "  ".join("#%s %s" % (c.slot, c.name) for c in cs)))
        s.close()
        return 0
    if a.raw is not None:
        out = []
        for key, ptr in enumerate_raw(s):
            c = read_raw(s, ptr, map_key=key)
            if a.raw != "*" and str(a.raw) not in (str(c["card_id"]), c["ptr_hex"]):
                continue
            out.append(c)
        print(json.dumps(out, ensure_ascii=False, indent=1) if a.json else
              "\n\n".join(json.dumps(o, ensure_ascii=False, indent=1) for o in out))
        s.close()
        return 0
    st = s.snapshot()
    print(format_table(st) if not a.json else json.dumps(st.as_dict(), ensure_ascii=False, indent=1))
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
