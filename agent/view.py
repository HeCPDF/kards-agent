#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view.py —— 盘面的**编号方案**与渲染。

编号（handle）
==============
屏幕上没有卡号，内存里的 uid 是对象地址（16 位十六进制，人没法念）。所以命令层
自己发一套**短号**，在一次渲染内稳定，下一条命令沿用上一次渲染的号：

    h1..hN    我方手牌（按 locationNumber 升序）
    m1..mN    我方场上单位（前线在前、支援在后，行内按 slot 升序）
    e1..eN    敌方场上单位（同上）
    hq / ehq  我方 / 敌方总部
    H1..HN    敌方手牌（只有 `board full` 才列；平时不该看）

另外一律接受：

    #1234     卡表 id（`card_id`）—— 和 `ops.py` 的参数一致
    @1c4b...  uid（对象地址）—— 唯一无歧义的写法

★ 短号会随盘面变。`resolve()` 拿的是**生成这批短号的那次快照**，所以
  "先 board 再 play h3" 之间如果盘面变了（对手回合插手），解析会指向旧卡。
  `AgentSession` 因此在执行动作前重新取快照并**按 uid 复核**，对不上就拒绝。

关键词与状态标记
================
用户要看的六个：被压制 / 被守护 / 守护 / 烟幕 / 奋战 / 冲击。其中

  * 守护 guard、烟幕 smokescreen、奋战 fury、冲击 shock 是**卡自身的关键词位**
    （`raw.all_keyword_flags`），直接读。
  * **被压制** pin 不是关键词位，是逐实例贴上来的效果
    （`receivedAbilitiesFromCards['pinned']` / `buffsFromCards['combat_pinned']`），
    要走 `kardsmem.cards.read_live_effects`，比读位贵。
  * **被守护** 也是**字段**：`isBeingGuarded@0x0288`（不是按相邻关系推算 ——
    曾经这么写过，见 `guarded_map` 里的更正）。`board_api` 还没读它（§11.1 F3a）。
"""
from __future__ import annotations

from typing import Optional

LOCAL, ENEMY = "local", "enemy"

# 关键词位 → 显示名。顺序即显示顺序。
KW_ZH = [
    ("guard", "守护"),
    ("smokescreen", "烟幕"),
    ("fury", "奋战"),
    ("shock", "冲击"),
    ("blitz", "闪击"),
    ("ambush", "伏击"),
    ("covert", "隐蔽"),
    ("mobilize", "动员"),
    ("alpine", "山地"),
    ("deployment", "部署"),
]
KW_SET = {k for k, _ in KW_ZH}

UNIT_ROWS = ("frontline", "back")

# UBaseCardObject 上的几个偏移。**仅作备注**，能走反射链的地方就走反射链。
OFF_CARD_TEXT = 0x90        # Text (FText)：规则文本 —— 白板卡上装的是史实文案
OFF_CARD_FLAVOR = 0xA0      # flavorText (FText)
OFF_CARD_FACTION = 0x7C     # faction (EFactionEnum)

# EFactionEnum（KardsCore_structs.hpp:18）
FACTION_ZH = {0: None, 1: "德国", 2: "英国", 3: "日本", 4: "苏联", 5: "美国",
              6: "法国", 7: "意大利", 8: "波兰", 9: "芬兰", 10: "澳新",
              11: "盟军", 12: "中立"}

# 动作流里的 action_type → 人话（§7.6g）
ACTION_ZH = {
    "XActionPlayCardFromHand": "出牌",
    "XActionAttackCard": "攻击",
    "XActionEndOfTurn": "结束回合",
    "XActionStartOfTurn": "回合开始",
    "XActionCardToDrawSelected": "选牌抽取",
    "XActionMoveCard": "移动",
    "XActionSurrender": "投降",
}


def kw_of(card) -> list:
    """卡自身的关键词（小写英文名）。位表优先，退回归一后的 keywords 列表。"""
    flags = (getattr(card, "raw", None) or {}).get("all_keyword_flags") or {}
    if flags:
        out = [k[4:] for k, v in flags.items() if k.startswith("has_") and v]
        if out:
            return [k for k, _ in KW_ZH if k in out] + \
                   [k for k in out if k not in KW_SET]
    return list(getattr(card, "keywords", None) or [])


def _row_of(card) -> str:
    return card.location or "?"


def guarded_map(st, side: str) -> dict:
    """→ {uid: True}：**被守护**的单位。

    ★ 2026-09-23 更正：这里先前是**按相邻关系推算**的（"同方同行、slot 相差 1 有守护单位"）。
      那是错的 —— 游戏自己有这个字段：`UBaseCardObject::isBeingGuarded@0x0288`
      （1.57 / 1.58 / 1.60 三个构建同偏移）。而且 `GiveGuard/RemoveGuard` 带
      `instigatorID` ⇒ 它是**带来源的状态**，不是每帧按相邻关系算出来的，
      所以"守护被某张卡临时赋予/移除"时推算值会和游戏对不上。

      2026-09-23 已接进 `board_api`（`Card.is_being_guarded`）。实机验证：
      敌方 5th BRIGADE(slot2) `guard=True`，相邻的 LANCASHIRE FUSILIERS(slot3)
      `is_being_guarded=True`，不相邻的 FRONTIER FORCE(slot0) 为 False —— 和百科一致。
    """
    out = {}
    for c in st.cards:
        if c.side != side or c.location not in UNIT_ROWS:
            continue
        v = getattr(c, "is_being_guarded", None)
        if v is None:
            v = (c.raw or {}).get("is_being_guarded")
        if v:
            out[c.uid] = True
    return out


# --------------------------------------------------------------- 编号
class Handles:
    """一次渲染产出的短号表。`resolve()` 用它把 `h3` 翻回卡。"""

    def __init__(self, st):
        self.st = st
        self.by_handle = {}
        self.by_uid = {}
        self._assign()

    def _put(self, h, card):
        self.by_handle[h] = card
        self.by_uid.setdefault(card.uid, h)

    def _assign(self):
        st = self.st

        def sortkey(c):
            return (c.slot if c.slot is not None else 99, c.uid)

        for i, c in enumerate(sorted(st.hand(LOCAL), key=sortkey), 1):
            self._put("h%d" % i, c)
        for i, c in enumerate(sorted(st.hand(ENEMY), key=sortkey), 1):
            self._put("H%d" % i, c)
        for pfx, side in (("m", LOCAL), ("e", ENEMY)):
            n = 0
            for row in UNIT_ROWS:
                for c in sorted([x for x in st.cards
                                 if x.side == side and x.location == row],
                                key=sortkey):
                    n += 1
                    self._put("%s%d" % (pfx, n), c)
        for h, side in (("hq", LOCAL), ("ehq", ENEMY)):
            c = (st.hq or {}).get(side)
            if c is not None:
                self._put(h, c)

    def handle(self, card) -> str:
        return self.by_uid.get(getattr(card, "uid", None), "?")


def resolve(st, token: str, handles: Optional[Handles] = None):
    """`h3` / `#1234` / `@1c4bbacc4f0` / 裸 uid → Card，找不到返回 None。

    **不猜**：`#id` 在场上有多张同名卡时返回 None 并由调用方报歧义，
    而不是随手挑第一张（`ops.find_card` 就是挑第一张，那是它的历史行为）。
    """
    if not token:
        return None
    t = token.strip()
    if handles is not None and t in handles.by_handle:
        return handles.by_handle[t]
    if t.startswith("@"):
        t = t[1:]
    if t.startswith("#"):
        try:
            cid = int(t[1:], 0)
        except ValueError:
            return None
        hits = [c for c in st.cards if c.card_id == cid and c.side == LOCAL] or \
               [c for c in st.cards if c.card_id == cid]
        return hits[0] if len(hits) == 1 else None
    c = st.by_uid(t)
    if c is not None:
        return c
    # 裸数字：先当 card_id 试
    try:
        cid = int(t, 0)
    except ValueError:
        return None
    hits = [c for c in st.cards if c.card_id == cid and c.side == LOCAL]
    return hits[0] if len(hits) == 1 else None


def ambiguous(st, token: str) -> list:
    """`#id` 对上了多张时，把它们都列出来（给调用方报错用）。"""
    t = token.lstrip("#@").strip()
    try:
        cid = int(t, 0)
    except ValueError:
        return []
    return [c for c in st.cards if c.card_id == cid]


# --------------------------------------------------------------- 渲染
def _n(v, dash="?"):
    return dash if v is None else str(v)


def _stat(c) -> str:
    """攻/防。攻击带 buff 时写成 `3(+2)`。"""
    a, d = c.attack, c.defense
    if a is None and d is None:
        return "?/?"
    b = c.attack_buff or 0
    at = "%s%s" % (_n(a), ("+%d" % b) if b else "")
    return "%s/%s" % (at, _n(d))


def _marks(c, guarded: dict, pinned: Optional[bool] = None) -> str:
    """状态标记串：关键词 + 被压制 + 被守护 + 被抑制。"""
    out = [zh for k, zh in KW_ZH if k in kw_of(c)]
    if c.uid in guarded:
        out.append("被守护")
    if pinned is True:
        out.append("被压制")
    if c.is_suppressed:
        out.append("被抑制")
    # 去重且保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return " ".join(uniq)


def _name(c, w=18, tr=None):
    """卡名。★ 内存里是**英文**，`tr` 给了就翻成中文（`kardsmem.locres`）。"""
    s = c.name or ("#%s" % c.card_id if c.card_id is not None else "?")
    if tr:
        s = tr(s) or s
    return s if len(s) <= w else s[:w - 1] + "…"


def render_board(st, handles: Optional[Handles] = None, full: bool = False,
                 pins: Optional[dict] = None, tr=None) -> str:
    """把盘面渲染成一张表。`pins` 是 {uid: True/False/None}（压制，贵，按需传）。"""
    h = handles or Handles(st)
    pins = pins or {}
    L = []
    k = st.kredits or {}
    L.append("回合 %s   %s   指挥点 我 %s / 敌 %s   指挥部 %s:%s   前线 %s%s" % (
        _n(st.turn),
        "【我方回合】" if st.our_turn else ("敌方回合" if st.our_turn is False else "回合归属未知"),
        _n(k.get(LOCAL)), _n(k.get(ENEMY)),
        _n((st.slots or {}).get(LOCAL)), _n((st.slots or {}).get(ENEMY)),
        _n(st.frontline_owner),
        "   ★对局已结束" if st.match_finished else ""))
    if st.unknown:
        L.append("  读不到：" + "、".join(st.unknown[:6]) +
                 ("…" if len(st.unknown) > 6 else ""))

    def unit_rows(side):
        rows = []
        g = guarded_map(st, side)
        for row, zh in (("frontline", "前线"), ("back", "支援")):
            for c in sorted([x for x in st.cards
                             if x.side == side and x.location == row],
                            key=lambda c: (c.slot if c.slot is not None else 99, c.uid)):
                rows.append("  %-4s %-4s %-19s %-7s 行动费%-3s %s" % (
                    h.handle(c), zh, _name(c, tr=tr), _stat(c),
                    _n(c.operation_cost, "-"), _marks(c, g, pins.get(c.uid))))
        return rows

    hq = (st.hq or {})
    L.append("-- 敌方 --------------------------------------------------------")
    if hq.get(ENEMY) is not None:
        L.append("  %-4s %-4s %-19s %-7s" % ("ehq", "总部", _name(hq[ENEMY], tr=tr), _stat(hq[ENEMY])))
    L += unit_rows(ENEMY) or ["  （空）"]
    eh = st.hand(ENEMY)
    if full and eh:
        gE = guarded_map(st, ENEMY)
        L.append("  敌方手牌 %d 张（★ 这是对手的不公开信息）：" % len(eh))
        for c in sorted(eh, key=lambda c: (c.slot if c.slot is not None else 99, c.uid)):
            L.append("    %-4s %-19s %-7s 费%-3s %s" % (
                h.handle(c), _name(c, tr=tr), _stat(c), _n(c.kredit_cost, "-"), _marks(c, gE)))
    elif eh:
        L.append("  敌方手牌 %d 张（`board full` 才展开）" % len(eh))

    L.append("-- 我方 --------------------------------------------------------")
    L += unit_rows(LOCAL) or ["  （空）"]
    if hq.get(LOCAL) is not None:
        L.append("  %-4s %-4s %-19s %-7s" % ("hq", "总部", _name(hq[LOCAL], tr=tr), _stat(hq[LOCAL])))

    L.append("-- 手牌 --------------------------------------------------------")
    gL = guarded_map(st, LOCAL)
    hand = sorted(st.hand(LOCAL), key=lambda c: (c.slot if c.slot is not None else 99, c.uid))
    if not hand:
        L.append("  （空）")
    for c in hand:
        tax = c.kredits_tax_as_enemy_target
        L.append("  %-4s %-19s %-9s %-7s 费%-3s%s%s%s" % (
            h.handle(c), _name(c, tr=tr), (c.card_type or "?"), _stat(c),
            _n(c.kredit_cost, "-"),
            (" 行动费%s" % c.operation_cost) if c.operation_cost else "",
            " 需指向" if c.needs_hand_target else "",
            (" 被指向+%d费" % tax) if tax else ""))
        mk = _marks(c, gL)
        if mk:
            L[-1] += "  " + mk
    return "\n".join(L)


# --------------------------------------------------------------- inspect
LOC_ZH = {"hand": "手牌", "frontline": "前线", "back": "支援", "hq": "总部",
          "discard": "弃牌堆", "deck": "牌库",
          # 动作流里用的是引擎侧的 location 串（ELocationEnum 的名字），和归一后的不一样
          "board_frontline": "前线", "board_hqleft": "支援", "board_hqright": "支援",
          "hand_left": "手牌", "hand_right": "手牌"}
SIDE_ZH = {LOCAL: "我方", ENEMY: "敌方"}


import re as _re

# UE 富文本：`<bold>…</>`。结束标签是 `</>`，**没有标签名** —— 别写成 `</[a-z]…>`。
_TAGS = _re.compile(r"<[^>]*>")


def _clean(t):
    """去掉富文本标签（`<bold>…</>`），保留文字。"""
    return _TAGS.sub("", t) if t else t


def _ellipsis(t, n):
    return t if not t or len(t) <= n else t[:n] + "…"


def render_inspect(d: dict, st=None, tr=None) -> str:
    """一张卡的全貌。字段读不到就写"读不到"，**不留白也不编**。"""
    c = d["card"]
    L = []
    title = d.get("title") or "?"
    en = d.get("title_en")
    L.append("%s  %s%s" % (d.get("handle", "?"), title,
                           ("（%s）" % en) if en and en != title else ""))
    L.append("  %-6s %-6s %s" % (
        SIDE_ZH.get(c.side, "?"), d.get("faction") or "国籍读不到",
        (c.card_type or "类型读不到")))
    # 位置
    loc = LOC_ZH.get(c.location, c.location or "?")
    pos = "%s" % loc
    if c.slot is not None and c.location in ("frontline", "back", "hand"):
        pos += " 第%d位" % c.slot
    L.append("  位置：%s%s" % (
        pos, ("（进场于回合 %s）" % c.enter_play_on_turn)
        if c.enter_play_on_turn is not None else ""))
    # 数值
    L.append("  费用：%s   行动费：%s%s" % (
        _n(c.kredit_cost, "-"), _n(c.operation_cost, "-"),
        ("   被敌方指向时 +%d费" % c.kredits_tax_as_enemy_target)
        if c.kredits_tax_as_enemy_target else ""))
    if c.attack is not None or c.defense is not None:
        L.append("  攻防：%s%s" % (
            _stat(c),
            ("   上限 %s/%s" % (c.max_attack, c.max_defense))
            if c.max_attack is not None else ""))
    # 词条 / 状态
    g = {c.uid: True} if getattr(c, "is_being_guarded", None) else {}
    mk = _marks(c, g)
    L.append("  词条/状态：%s" % (mk or "（无）"))
    if c.needs_hand_target:
        L.append("  ★ 出牌时需要指向目标")
    if getattr(c, "under_enemy_control", None):
        L.append("  ★ 处于敌方控制之下")
    # 文本
    # ★ 白板卡（没有技能）的 `Text@0x90` 装的是**史实文案**，和 `flavorText` 一样。
    #   实测：LANCASHIRE FUSILIERS 两个字段一字不差。⇒ 相同就不当规则文本展示。
    rules, flavor = d.get("rules_text"), d.get("flavor_text")
    if rules and rules != flavor:
        L.append("  描述：%s" % _clean(rules))
    if flavor:
        L.append("  风味：%s" % _ellipsis(_clean(flavor), 120))
    # 被其它卡给予的效果
    eff = d.get("effects") or {}
    if d.get("effects_error"):
        L.append("  被贴效果：读不到（%s）" % d["effects_error"])
    else:
        L += _render_effects(eff, st, tr)
    return "\n".join(L)


def _src_name(st, card_id, tr=None):
    """来源 CardID → 卡名。★ 动作流/效果里给的是**临时 CardID**，不是卡表 id。"""
    if st is None or card_id is None:
        return "#%s" % card_id
    for c in st.cards:
        if c.card_id == card_id:
            nm = (tr(c.name) if tr else c.name) or "?"
            return "%s(#%s)" % (nm, card_id)
    return "#%s" % card_id


# ★ F4b（2026-09-23）：`ability_<key>_title` 是 `Game.locres` 里 `helpbubbles`
#   namespace 下唯一一批"内部能力名 → 标题"的完整表（只有 14 条，逐条列出过，
#   不是猜的）。`receivedAbilitiesFromCards` 的 ability 名（`pinned`/`guard`/…）
#   直接命中；`buffsFromCards` 的 buff 键用的是另一套稍有出入的拼法
#   （比如 pin 的 buff 键是 `combat_pinned`，ability 名是 `pinned`——文件头
#   docstring 早就记了这对），所以留一张**逐个核实过**的别名表兜底，
#   命不中就原样显示，绝不瞎猜。
_ABILITY_ALIAS = {"combat_pinned": "pinned"}


def _ability_name_zh(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    try:
        from kardsmem import locres
        zh = locres.namespace_zh("helpbubbles", "ability_%s_title" % name)
        if zh:
            return zh
        alias = _ABILITY_ALIAS.get(name)
        if alias:
            zh = locres.namespace_zh("helpbubbles", "ability_%s_title" % alias)
            if zh:
                return zh
    except Exception:                                        # noqa: BLE001
        pass
    return name


def _render_effects(eff: dict, st=None, tr=None) -> list:
    """被其它卡给予的效果：**文本 + 来源**（用户点名要的）。"""
    L = []
    rows = []
    for b in eff.get("buffs_from_cards") or []:
        # `read_buffs_from_cards` 直接给了 giver_name（FText），有就用，没有再按 CardID 回查
        src = (tr(b.get("giver_name")) if (tr and b.get("giver_name")) else b.get("giver_name"))             or _src_name(st, b.get("giver_card_id"), tr)
        for k, v in (b.get("buffs") or {}).items():
            nm = _ability_name_zh(k)
            rows.append("%s %+d  ← %s" % (nm, v, src) if isinstance(v, int)
                        else "%s=%s  ← %s" % (nm, v, src))
    for ab in eff.get("received_abilities") or []:
        givers = ", ".join(_src_name(st, g, tr) for g in (ab.get("givers") or [])) or "?"
        rows.append("%s  ← %s" % (_ability_name_zh(ab.get("ability")), givers))
    if rows:
        L.append("  被其它卡给予的效果：")
        L += ["    " + r for r in rows]
        if eff.get("effects_active") is False:
            L.append("    ★ 这张卡**被抑制**，以上效果当前不生效")
    if eff.get("is_immune"):
        L.append("  ★ 免疫：可以被指定为目标，但**不掉血**")
    return L


# --------------------------------------------------------------- 历史
def render_history(rows: list, me=None, st=None, tr=None) -> str:
    """对局历史。★ 两侧记法不同（§7.6g）：对手侧字段具名，我方侧是字段号。"""
    if not rows:
        return "（还没有动作）"
    L = []
    for r in rows:
        who = r.get("player_id")
        tag = "我方" if (me is not None and who == me) else "敌方"
        typ = ACTION_ZH.get(r.get("action_type"), r.get("action_type") or "?")
        L.append("  %-4s %-10s %s" % (tag, typ, _action_detail(r, st, tr)))
    return "\n".join(L)


def _kv(r) -> dict:
    """动作参数 → {名字: 值}。★ `Value` 和 `Text` 必须分开看：

    我方侧（本地上传的载荷）**全部塞在 `Text` 里、`Value` 恒为 -1**；
    对手侧（服务器回传）才按类型放。曾经把 `text="17"` 误当成整数 17 —— 它是卡组代码。
    """
    out = {}
    for v in r.get("data") or []:
        n = v.get("name")
        if n is None:
            continue
        out[n] = v["text"] if v.get("text") is not None else v.get("value")
    return out


# 我方侧字段号 → 含义。★ 字段号**按 action_type 分配**，不是全局位置序号
#   （攻击里卡组代码在 2/3，出牌里在 4）⇒ 只能逐 action_type 列（§11.1 F5b）。
MINE_FIELDS = {
    "XActionAttackCard": {"0": "attackerCardID", "1": "defenderCardID",
                          "2": "attackerCode", "3": "defenderCode"},
    "XActionPlayCardFromHand": {"0": "cardID", "4": "cardCode"},
}


def _action_detail(r, st=None, tr=None) -> str:
    kv = _kv(r)
    typ = r.get("action_type")
    fields = MINE_FIELDS.get(typ)
    if fields:                          # 我方侧：把字段号翻成名字
        kv = {fields.get(k, k): v for k, v in kv.items()}

    def card(x):
        try:
            return _src_name(st, int(x), tr)
        except (TypeError, ValueError):
            return str(x)

    if typ == "XActionAttackCard":
        return "%s → %s" % (card(kv.get("attackerCardID")), card(kv.get("defenderCardID")))
    if typ == "XActionPlayCardFromHand":
        # ★ 先按 CardID 回查盘面 —— 那样拿到的是**本地化后的卡名**。
        #   `card_name` 是蓝图资产名（`card_unit_frontier_force`），本地化表里没有它。
        nm = card(kv.get("cardID"))
        if nm.startswith("#") and kv.get("card_name"):
            nm = kv["card_name"]
        loc = LOC_ZH.get(kv.get("location"), kv.get("location"))
        tgt = kv.get("targetCardID")
        s = nm
        if loc:
            s += " → %s" % loc
        if tgt not in (None, 0, "0"):
            s += "  指向 %s" % card(tgt)
        return s
    if typ in ("XActionEndOfTurn", "XActionStartOfTurn"):
        return ""
    # 其余：原样列出，别编
    return " ".join("%s=%s" % (k, v) for k, v in kv.items() if k != "14")


# --------------------------------------------------------------- 判据
def render_targets(rows, handles=None, tr=None) -> str:
    """「这个单位现在能打谁」。

    ★ 三态，不是两态：能打 / 不能打（给理由）/ **不知道**（判据没算出来）。
      "不知道"绝不折叠进"不能打" —— 判据只挑不判，缺漏是必然的（§7.6f）。
    """
    if not rows:
        return "  （场上没有敌方目标）"
    L = []
    for c, r in rows:
        h = handles.handle(c) if handles else "?"
        if not r.get("ok"):
            mark = "？不知道"
            why = (r.get("stopped") or "")[:52]
        elif r.get("can"):
            mark, why = "✔ 可以打", ""
        else:
            mark, why = "✘", r.get("reason_zh") or r.get("reason") or ""
        if r.get("approx"):
            why += "  ※含近似判据(%s)" % ",".join(r["approx"])
        L.append("  %-4s %-20s %-8s %s" % (h, _name(c, 20, tr), mark, why))
    return "\n".join(L)
