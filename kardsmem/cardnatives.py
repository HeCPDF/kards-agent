#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.cardnatives —— `UBaseCardObject::*` 原生函数的进程外实现。

它是原语表里最大的一块缺口：120 个函数 / 6429 个调用点（仅次于 KismetMath 和
KismetArray）。和 Kismet 那些通用库不同，**这些是游戏自己的原生代码，在 exe 里，
没有蓝图字节码可读，也没有引擎源码可抄**。

能在进程外重算的理由：它们读的字段**我们已经在读了**（`board_api.Card`：
类型 / 位置 / 阵营 / 攻防 / 费用 / 压制 / 关键词）。所以这里做的是**字段映射**，
不是逆向 —— 每条都能指出依据的是哪个字段。

设计
====
* 原语签名照 SDK（`kards_classes.hpp`）：都是卡对象上的方法，出参用指针。
  这里统一成 **`fn(card, *args) -> value`**，出参由调用方分派。
* **字段读不到的一律抛 `Unimplemented`**，绝不拿"多半是 False"糊过去 ——
  静默的错误答案比响亮的缺口危险得多（这正是 §7.6f 说"判据只挑不判"的原因）。
* 需要盘面级信息的（轮到谁、是不是战役模式）从 `Ctx` 拿，不猜。

    from kardsmem.cardnatives import CardNatives
    cn = CardNatives(board_state)          # board_api.BoardState
    cn.call("IsUnit", card)                # -> True/False
    cn.call("IsSideActive", card, "local")
"""
from __future__ import annotations

import time

from .kismetlib import Unimplemented

# ECardLocationEnum 归一化后的位置名（见 board_api）
ON_BOARD = ("frontline", "hq", "back")
UNIT_TYPES = {"tank", "fighter", "bomber", "infantry", "artillery",
              "antiair", "antitank", "tankdestroyer"}
AIR_TYPES = {"fighter", "bomber"}
GROUND_TYPES = UNIT_TYPES - AIR_TYPES


def _loc(c):
    """牌的位置。读不到（对手隐藏牌等）⇒ "?"：与任何具体位置比较都为假，不抛。"""
    v = F(c, "location")
    return "?" if v is None else v


def _loc_enum(c):
    """位置**枚举原值**（+0x275，SDK 里是 `Pad`）：Hand={3,4}、Deck={1,2}（IDA 0x144AFC890）。

    `location` 字符串是我们读侧的归一（`LOCATION_NAMES[9]="deck"` 会把 Deck 单数形式也算 deck），
    而游戏只认 1/2/3/4 —— D4/D9 就是修这个。读不到枚举时退回字符串映射（Deck 给 1）。
    """
    e = F(c, "location_enum")
    if e is not None:
        return e
    return {"deck": 1, "hand": 3, "hq": 5, "back": 6, "frontline": 7, "discard": 8}.get(_loc(c))


_STAT_PLUS100 = ("defense", "kredit", "kreditBuff")     # 哨兵符号（IDA：其余为 −100）


def _game_decrypt(field: str, key, X, Y, enc):
    """游戏 `getAndDecryptX` 的公式（IDA，§12-U1）：

    `mult(X)==0`（记录未初始化）⇒ **哨兵**：attack/attackBuff = −100、defense/kredit/
    kreditBuff = **+100**（于是 `getTotalDefense = clamp(100,0,99) = 99`）；
    否则 `((enc ^ key) − Y) / X`（**32 位有符号整除、向零取整**，不夹 —— 与 C 的 `/` 一致）。
    读不齐 ⇒ None（调用方退回旧路径）。实测：稳态对象（实例/静态模板/CDO）全是 mult=1939，
    这个哨兵只在"刚构造还没写记录"的瞬态窗口出现 —— 但按游戏语义实现。
    """
    if X == 0:
        return -100 if field not in _STAT_PLUS100 else 100
    if key is None or Y is None or enc is None:
        return None
    num = (enc ^ key) - Y
    q = abs(num) // abs(X)
    return q if (num >= 0) == (X >= 0) else -q


def _fold(s) -> str:
    """游戏里 TMap<FString> 键比较/哈希是**大小写不敏感**的（IDA：CRC 前把 a–z 折成 A–Z）。
    `casefold()` 对 ASCII 与之等价。"""
    return str(s).casefold() if s is not None else ""


def _stat(c, what, *keys):
    """攻/防读数：优先用**原始加密记录**按游戏公式解码（含 mult==0 哨兵）；
    没有 records_raw 时退回 view 字段。
    ★ 2026-09-30 实机（effectvm 空跑 HEATWAVE）：天气卡的逻辑会遍历手牌读 defense，
      手牌里的指令没有这个字段 ⇒ 原来在这里整条判据停掉（非单位按 0）。"""
    sh = F(c, "shadow_stats")             # 组 D：setAndEncrypt* 的影子优先
    if isinstance(sh, dict) and what in sh:
        return int(sh[what])
    rec = F(c, "records_raw")
    if isinstance(rec, dict) and what in rec:
        row = rec[what]
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            v0 = _game_decrypt(what, F(c, "key"), row[0], row[1], row[3])
            if v0 is not None:
                return v0
    v = F(c, *keys)
    if v is None:
        ct = F(c, "card_type")
        if ct is None or ct not in UNIT_TYPES:
            return 0          # 非单位 / 身份未知的牌（如对手隐藏手牌）：攻防按 0
    return _need(v, what)


def _dec_rec(c, rec_name, view_name=None):
    """§2 #4：加密解码读器（`getAndDecryptX`）——优先用原始记录按游戏公式（哨兵/向零取整）；
    没有 records_raw 时退回 view 字段（None ⇒ 抛，不猜）。

    `rec_name` 是记录名（`CARD_RECORDS` 的键：attack/attackBuff/kredit/kreditBuff/defense），
    `view_name` 是 view 字段名（默认同 rec_name）。
    """
    # 组 D：同一次 VM 空跑里 `setAndEncryptX` 写过的影子值优先（写→读一致）。
    sh = F(c, "shadow_stats")
    if isinstance(sh, dict) and rec_name in sh:
        return int(sh[rec_name])
    r = F(c, "records_raw")
    if isinstance(r, dict) and rec_name in r:
        row = r[rec_name]
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            v = _game_decrypt(rec_name, F(c, "key"), row[0], row[1], row[3])
            if v is not None:
                return v
    name = view_name or rec_name
    return _need(F(c, name), name)


def _defense_raw(c):
    """未夹的 `getAndDecryptDefense`（§2 #1）。"""
    return _dec_rec(c, "defense", "defense")


def _need(v, what):
    """字段没读到就抛 —— 不要用 None 当 False。"""
    if v is None:
        raise Unimplemented("BaseCardObject: 需要的字段读不到（%s）" % what)
    return v


def _fail(msg):
    """lambda 里不能写 `raise` —— 包一层函数调用。"""
    raise Unimplemented(msg)


def _seat(x):
    """归一成 **ESideEnum 的座位号 1/2** —— 求值器内部一律用引擎表示。

    ★ 2026-09-23 踩的坑：这里原先归一成 `'local'/'enemy'`，结果 `GetOppositeSide`
      把字符串还给字节码，下一条 `EqualEqual_ByteByte` 直接炸。
      **`local/enemy` 是我们读侧的展示层，不能泄漏进求值器。**

    ★ 座位 ≠ 阵营（§4.12）：1/2 只是座位，哪个座位是本地由回合奇偶推。
      所以接受 `'local'/'enemy'` 时必须知道本地占哪个座位 —— 见 `CardNatives.my_seat`。
    """
    if isinstance(x, bool):
        raise Unimplemented("side 不该是 bool")
    if isinstance(x, int) and x in (1, 2):
        return x
    s = str(x).split("::")[-1].lower()
    if s in ("1", "left"):
        return 1
    if s in ("2", "right"):
        return 2
    raise Unimplemented("认不出的 side=%r（要 ESideEnum 的 1/2）" % (x,))


class CardNatives:
    """`board` 是 `board_api.BoardState`（或任何有 `.turn` / `.our_turn` 的对象）。"""

    def __init__(self, board=None, campaign: bool = False, view=None,
                 my_seat=None, ks=None):
        self.board = board
        # 本地玩家占哪个座位（ESideEnum 1/2）。座位 != 阵营，见 §4.12：
        # 它由回合奇偶推出来，`BoardState.my_side_raw` 就是它。
        self.my_seat = my_seat if my_seat is None else _seat(my_seat)
        self.campaign = campaign
        # 求值器里传来传去的是**卡对象指针**，而这里要的是带字段的视图。
        # `view(ptr) -> dict|Card` 由调用方注入（通常接 `kardsmem.cards.read_raw`）。
        # 不注入时只能吃已经是 dict/Card 的卡。
        self.view = view
        # ★ 2026-10-02：少部分原语要**跨进程读**（如 `GetMatchController` 要遍历关卡 actor
        #   找 `MatchController_C`）——由调用方（effectvm）注入进程会话；缺了那些原语抛
        #   Unimplemented（不猜）。
        self.ks = ks
        # ★ 2026-10-02：把模块级 `APPROX` 挂到实例上 —— `vm.py` 是
        #   `getattr(self.cn, "APPROX", ())`，以前 CardNatives **没有这个属性**
        #   ⇒ "掺了近似"从来没被标记过（CanBeTargetted / CanEndTurn 也一样）。
        self.APPROX = APPROX

    # ---- 分派 ----
    def call(self, name: str, card, *args):
        fn = _TABLE.get(name)
        if fn is None:
            raise Unimplemented("BaseCardObject::%s" % name)
        if name in _NO_RECEIVER:       # 库函数 / GameState 方法：接收者不是卡，别当卡去读
            return fn(self, card, *args)
        return fn(self, self._view(card), *args)

    def _view(self, card):
        """指针 → 带字段的视图。已经是 dict/Card 的原样返回。"""
        if isinstance(card, int) and card:
            if self.view is None:
                raise Unimplemented("拿到的是卡对象指针 %#x，但没注入 view()" % card)
            return self.view(card)
        return card

    def has(self, name: str) -> bool:
        return name in _TABLE

    @staticmethod
    def arity(name: str) -> int:
        """这个原语**吃几个入参**（不含 self/card，也不含出参）。

        ★ Kismet 把出参也当实参压进调用点（`IsUnit(bool* isIt)` 在字节码里是
          一个带 1 个参数的调用），所以求值器必须知道真正的入参个数，
          才能把多出来的那些认成出参、把返回值写回去。
        """
        import inspect
        fn = _TABLE.get(name)
        if fn is None:
            raise Unimplemented("BaseCardObject::%s" % name)
        try:
            n = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):                      # noqa: BLE001
            return 0
        return max(0, n - 2)          # 去掉 self 和 card

    # ---- 取字段 ----
    @staticmethod
    def f(card, *keys):
        """按给定顺序取第一个非 None 的字段。

        ★ 为什么要多个候选：读侧有**两层**，键名不一样 ——
          `kardsmem.cards.read_raw()` 给的是贴近内存的原始名
          （`total_kredit_cost` / `kredit` / `enter_play_turn`），
          `board_api.Card` 给的是归一名（`kredit_cost` / `enter_play_on_turn`）。
          原语两种都要能吃，否则换个调用方就整片读不到。
        """
        if card is None:
            return None
        get = card.get if isinstance(card, dict) else (lambda k: getattr(card, k, None))
        for k in keys:
            v = get(k)
            if v is not None:
                return v
        return None


F = CardNatives.f


# --------------------------------------------------------------------------
# 类型判定 —— 依据 `Card.card_type`
# --------------------------------------------------------------------------
def _type_is(*types):
    def fn(self, card):
        return _need(F(card, "card_type"), "card_type") in types
    return fn


# --------------------------------------------------------------------------
# 位置 / 阵营
# --------------------------------------------------------------------------
def _is_on_board(self, card):
    """★ `Board_HQLeft/Right`(5/6) 是**整个后排**（含总部），不是只有总部（§4.11）。
    归一后 `hq` 和 `back` 都来自那两个值，所以三个都算"在场上"。"""
    return _loc(card) in ON_BOARD


def _kredits(self, side):
    """座位 `side`（ESideEnum 1/2）现在的指挥点。**需要 BoardState + my_seat**。

    盘面把指挥点按 `local`/`enemy` 存，而字节码问的是座位号 ⇒ 要知道本地占哪个座位
    才能翻译。缺任何一样都抛 —— 猜错一边会让费用判据整段反过来。
    """
    if self.board is None:
        raise Unimplemented("getKreditBySide 需要 BoardState")
    if self.my_seat is None:
        raise Unimplemented("getKreditBySide 需要知道本地占哪个座位（my_seat）")
    k = getattr(self.board, "kredits", None) or {}
    v = k.get("local" if _seat(side) == self.my_seat else "enemy")
    return _need(v, "kredits[%s]" % side)


# --------------------------------------------------------------------------
# 近似原语：游戏那边是**原生代码**（读不到字节码），我们只能按百科 + 实测重写
# --------------------------------------------------------------------------
# ★ 它们和上面那些不是一回事。上面的是"读一个我们本来就在读的字段"，
#   结论和游戏必然一致；这里的是**我们自己的重写**，可能和游戏不一致。
#   所以单独列进 `APPROX`，求值器会把"这次答案里掺了近似"一路冒泡到最终结果。
#   判据本来就只挑不判（§7.6f），标出来是为了让调用方知道这条结论有多硬。
APPROX = {"CanBeTargetted", "CanEndTurn", "IsReconnectMatch"}


def _can_be_targetted(self, card, targetting_card=None, by_play_from_hand=False):
    """`CanBeTargetted(&canIt, &Reason, &p1, &p2, targettingCard, byPlayFromHand)`。

    ★ **出参在前面**（四个），入参在后面 —— 求值器按 `CPF_OutParm` 标志分参，
      所以这里的形参只写入参，返回一个 4 元组按位置写回。

    语义（游戏内百科 + 实测，见规格 §6.4）：守方限制。
      * **烟幕**：无法被敌方**单位**攻击（移动/攻击后才失去）
      * **未揭示的隐蔽**：不受指令/反制/单位效果影响
      * `cantBeAttackedBy:<type>`：该类单位不能攻击它
      * **被抑制的目标一律放行**（抑制会让它失去这些保护）

    ⚠ 这是**近似**：真正的判据在原生代码里，我们读不到。漏掉的情形会让它
      过于宽松（说能打其实不能）—— 那没关系，游戏会拒绝，我们读提示拿权威理由。
      反过来**绝不能让它变成否决权**（§7.6f）。
    """
    if _need(F(card, "is_suppressed"), "is_suppressed"):
        return (True, "", "", "")          # 被抑制 ⇒ 失去全部保护，放行
    kw = F(card, "keywords") or []
    flags = F(card, "keyword_flags") or {}

    def has(k):
        return k in kw or bool(flags.get("has_" + k))

    if has("smokescreen") and not by_play_from_hand:
        return (False, "defender_has_smokescreen", "", "")
    if has("covert") and not _need(F(card, "is_revealed"), "is_revealed"):
        return (False, "cant_be_attacked_by_unit", "", "")
    for ab in (F(card, "received_abilities") or []):
        nm = (ab.get("ability") or "").lower()
        if nm.startswith("cantbeattackedby"):
            t = nm.split(":", 1)[1] if ":" in nm else ""
            att = (F(targetting_card, "card_type") if targetting_card else None)
            if not t or (att and str(att).lower() == t):
                return (False, "cant_be_attacked_by_unit", t, "")
    return (True, "", "", "")


def _is_side_active(self, card, side):
    """轮到 `side`（座位 1/2）没有。**需要盘面**，卡对象上没有这个信息。"""
    if self.board is None:
        raise Unimplemented("IsSideActive 需要 BoardState（轮到谁不在卡对象上）")
    ot = getattr(self.board, "our_turn", None)
    if ot is None:
        raise Unimplemented("IsSideActive: BoardState.our_turn 读不到")
    if self.my_seat is None:
        raise Unimplemented("IsSideActive 需要知道本地占哪个座位（my_seat）")
    return bool(ot) if _seat(side) == self.my_seat else (not ot)


def _opposite_side(self, card):
    """返回**座位号**（1<->2），不是 local/enemy —— 字节码接着会拿它做 ByteByte 比较。"""
    return 2 if _card_seat(card) == 1 else 1


def _card_seat(card) -> int:
    """卡在哪个座位。`read_raw` 给 `side_enum`(1/2)，归一层给 `side`(local/enemy)。"""
    raw = F(card, "side_enum")
    if raw is not None:
        return _seat(raw)
    raise Unimplemented("需要 side_enum（座位号）；只有归一后的 side 是不够的")


# --------------------------------------------------------------------------
# 关键词 —— 依据 `Card.keywords`（cards.py 现在读得到这 10 个）
# --------------------------------------------------------------------------
def _kw(card, name) -> bool:
    ks = F(card, "keywords")
    if ks is None:
        raise Unimplemented("需要 keywords，但这个后端没给")
    return name in ks


_TABLE = {
    # ---- 类型 ----
    "IsUnit": lambda self, c: _need(F(c, "card_type"), "card_type") in UNIT_TYPES,
    "IsAirUnit": _type_is(*AIR_TYPES),
    "IsGroundUnit": lambda self, c: _need(F(c, "card_type"), "card_type") in GROUND_TYPES,
    "IsTank": _type_is("tank"),
    "IsFighter": _type_is("fighter"),
    "IsBomber": _type_is("bomber"),
    "IsInfantry": _type_is("infantry"),
    "IsArtillery": _type_is("artillery"),
    "IsAntiAir": _type_is("antiair"),
    "IsAntiTank": _type_is("antitank"),
    "IsTankDestroyer": _type_is("tankdestroyer"),
    "IsOrder": _type_is("order"),
    "IsLocation": _type_is("location"),
    "IsGotcha": _type_is("gotcha", "wildcard"),

    # ---- 位置 ----
    "IsLocatedOnBoard": _is_on_board,
    # D4/D9（NATIVE-SPEC-GAPS §11）：游戏按**枚举原值**判 —— Hand={3,4}、Deck={1,2}
    #（9 "Deck" 单数形式**不算** Deck）。旧的 `location == "deck"` 会把 9 也算进去。
    "IsLocatedInHand": lambda self, c: _loc_enum(c) in (3, 4),
    "IsLocatedInDeck": lambda self, c: _loc_enum(c) in (1, 2),
    "IsLocatedInDiscard": lambda self, c: _loc(c) == "discard",

    # ---- 阵营 ----
    "GetOppositeSide": _opposite_side,
    # 归一层已经把"哪边是本地"算好了，这里直接用它；没有归一 side 时退回座位比对。
    # ★ 攻击限制三兄弟（`cardsCheckFunctions::CanAttack` 要用）。
    #   它们查的是**逐实例被贴的能力**，不是关键词位。
    # ★ **压制（pin）**：百科「被压制的单位不能移动或攻击」。它不是关键词位，
    #   而是逐实例贴上来的效果 —— `receivedAbilitiesFromCards` 里出现 `pinned`，
    #   或 `buffsFromCards` 里出现 `combat_pinned`（`ops.is_pinned` 用的同一判据）。
    #   先前记在 NOT_READABLE 里说"没有 pinned 字段"，那是只看了关键词位。
    "IsPinned": lambda self, c: (
        any("pinned" in a for a in _need(_abilities(c), "received_abilities"))
        or any("pinned" in k.lower()
               for b in (c.get("buffs_from_cards") or [])
               for k in (b.get("buffs") or {}))),
    # ★ **近似**原语（见 APPROX 旁边的说明）：游戏那边是原生代码，我们读不到。
    "CanBeTargetted": _can_be_targetted,
    # 某一**座位**现在有多少指挥点。需要盘面（卡对象上没有这个信息）。
    "getKreditBySide": lambda self, c, side: _kredits(self, side),
    # ★ 自定义名上的属性标记。**只答得了"没有"那一半**：
    #   `customName1/2` 为空 ⇒ 肯定不带任何属性 ⇒ False（这是确定的）。
    #   非空时要解析那串名字的格式，我们还不知道 ⇒ 抛 Unimplemented，**不猜**。
    #   实测大多数卡这两个字段都是空的，所以这半个实现已经能让判据跑通绝大多数情形。
    "CustomName1HasAttribute": lambda self, c, a: _custom_attr(c, "custom_name1", a),
    "CustomName2HasAttribute": lambda self, c, a: _custom_attr(c, "custom_name2", a),
    # 还有没有攻击次数 —— `attack_left`（含 Fury，**不含 Blitz**，见 §6.1）。
    # ★ 它对"刚部署的单位"仍读到 1，所以**不能**拿它判"这回合能不能动"；
    #   但 `CanAttack` 里问的就是次数本身（`no_attack_left`），这里如实回答即可 ——
    #   部署当回合的限制由同一函数里的 `deployment_sickness` 分支管。
    "HasAttackLeft": lambda self, c: (
        _need(F(c, "attack_left"), "attack_left") > 0),
    # `heavy_armor + heavy_armor_buff`，夹在 [0,3]（`kardsmem.cards.read_raw` 已经算好，
    # 不是猜的上限——SDK 里重甲只有 0~3 四档）。NOT_READABLE 里原来写"没有这个字段"，
    # 是没查 board_api 已经读了 0x1DC/0x1E0 这两个 CARD_I32。
    # §2 #1（IDA 0x144B14FF0）：clamp(heavy@0x1DC + buff@0x1E0, 0, 3)。
    "getTotalHeavyArmor": lambda self, c: max(0, min(3, _need(
        F(c, "heavy_armor"), "heavy_armor") + _need(F(c, "heavy_armor_buff"),
                                                    "heavy_armor_buff"))),
    # `getCardsBuffedByThisCard(TArray<int32>* Cards)` —— `cardsBuffedByThisCard@0x108`
    # 早就有读法（`kardsmem.cards.read_cards_buffed_by_this_card`，接进了
    # `read_live_effects` 的 `cards_buffed_by_this_card`）。NOT_READABLE 那条
    # "同上，反向" 是没查这处，跟 GameplayTags/HeavyArmor 一个模式。
    "getCardsBuffedByThisCard": lambda self, c: (
        _need(F(c, "cards_buffed_by_this_card"), "cards_buffed_by_this_card")),
    # `isBuffedByCard(int32 instigatorID, bool* isBuffed)` —— `buffsFromCards`
    # 每一行都带 `giver_card_id`（`read_buffs_from_cards`），查有没有这个来源就够，
    # 不需要"逐实例被贴效果关系"这种更重的东西（NOT_READABLE 原来的理由过虑了）。
    # ⚠ `read_buffs_from_cards` 把"TMap 本来就是空的"和"读失败"都归成 `[]`
    #   （沿用了它一直以来的做法，不是这里新引入的），所以这条原语在**读失败**
    #   那个极少数情形下会把"读不到"悄悄当成"没被贴过"。跟这个模块其它所有
    #   用到 `buffs_from_cards` 的地方（`_render_effects` 等）是同一个既有的
    #   权衡，没有单独加固。
    "isBuffedByCard": lambda self, c, instigator_id: any(
        row.get("giver_card_id") == instigator_id
        for row in (F(c, "buffs_from_cards") or [])),
    # 这回合打过了没 —— `has_attacked_this_turn` 是现成的位（CARD_U8）。
    "HasAttackedThisTurn": lambda self, c: bool(
        _need(F(c, "has_attacked_this_turn"), "has_attacked_this_turn")),
    "HasCustomAbility": lambda self, c, a: (
        str(a).lower() in _need(_abilities(c), "received_abilities")),
    # `FGameplayTagContainer GameplayTags@0x30`（`kards.cards.read_gameplay_tags`，
    # §11 P2 补完）。★ 只做**精确匹配**——UE 原生的 `HasTag` 还认层级前缀
    # （"A.B" 命中 "A.B.C"），我们没证据这里用得到那种语义，先按精确比对来。
    # ⚠ 签名是 `getHasGameplayTag(const FGameplayTag& inputTag, bool* HasTag)` ——
    #   入参是个**结构体**（里面只有一个 FName），不是裸字符串。求值器对
    #   `MakeStruct`/`StructConst` 这类字面量目前怎么摊给调用方**没有实测验证过**
    #   （这条原语至今没被任何跑通的调用路径触发过）。⇒ 只接受"已经是字符串"
    #   这一种明确形态，其它一律报 Unimplemented——宁可停下，不要拿一个
    #   猜出来的类型转换悄悄给出可能错的答案。
    "getHasGameplayTag": lambda self, c, t: (
        _has_tag(c, str(t))                    # D5：显式 tag ∪ ParentTags（层级）∪ 能力键
        if isinstance(t, str) else
        _fail("getHasGameplayTag：入参不是字符串（是 %r），"
              "求值器怎么摊平 FGameplayTag 这个结构体字面量还没验证过" % (t,))),
    "HasCantAttack": lambda self, c: (
        any("cantattack" == a for a in _need(_abilities(c), "received_abilities"))),
    "HasCantAttackType": lambda self, c, t: (
        ("cantattack:" + str(t).lower())
        in _need(_abilities(c), "received_abilities")),
    "HasCantBeAttackedBy": lambda self, c, t: (
        ("cantbeattackedby:" + str(t).lower())
        in _need(_abilities(c), "received_abilities")),
    "IsOwnedByClientSide": lambda self, c: (
        F(c, "side") == "local" if F(c, "side") is not None
        else _card_seat(c) == _need(self.my_seat, "my_seat")),
    "IsSideActive": _is_side_active,
    "IsSameSideUnit": lambda self, c, side: (
        _need(F(c, "card_type"), "card_type") in UNIT_TYPES
        and _card_seat(c) == _seat(side)),

    # ---- 数值 ----
    "getTotalAttack": lambda self, c: _stat(c, "attack", "total_attack", "attack"),
    # §2 #1（IDA 0x144B14FB0）：clamp(getAndDecryptDefense, 0, 99) —— 未夹的原始值再夹。
    "getTotalDefense": lambda self, c: max(0, min(99, _defense_raw(c))),
    "getTotalKreditCost": lambda self, c: _need(
        F(c, "total_kredit_cost", "kredit_cost", "kredit"), "kredit_cost"),
    "getTotalOperationCost": lambda self, c: _need(F(c, "operation_cost"), "operation_cost"),
    "getAndDecryptAttack": lambda self, c: _stat(c, "attack", "attack"),  # 明文侧
    # §2 #4：四个解码读器都走原始记录（哨兵 +100/−100、向零取整）。
    "getAndDecryptDefense": lambda self, c: _dec_rec(c, "defense", "defense"),
    "getAndDecryptAttackBuff": lambda self, c: _dec_rec(c, "attackBuff", "attack_buff"),
    "getAndDecryptKredit": lambda self, c: _dec_rec(c, "kredit", "kredit"),
    "getAndDecryptKreditBuff": lambda self, c: _dec_rec(c, "kreditBuff", "kredit_buff"),

    # ---- 状态 / 关键词 ----
    "IsSuppressed": lambda self, c: bool(_need(F(c, "is_suppressed"), "is_suppressed")),
    "getHasBlitz": lambda self, c: _kw(c, "has_blitz"),
    "getHasGuard": lambda self, c: _kw(c, "has_guard"),
    "getHasFury": lambda self, c: _kw(c, "has_fury"),
    "getHasAmbush": lambda self, c: _kw(c, "has_ambush"),
    "getHasSmokescreen": lambda self, c: _kw(c, "has_smokescreen"),
    "getHasCovert": lambda self, c: _kw(c, "has_covert"),
    "getHasMobilize": lambda self, c: _kw(c, "has_mobilize"),
    "getHasAlpine": lambda self, c: _kw(c, "has_alpine"),
    "getHasShock": lambda self, c: _kw(c, "has_shock"),
    "getHasDeployment": lambda self, c: _kw(c, "has_deployment"),
    # 隐蔽且**尚未揭示**。`AttemptToPlayFinal` 里用它挡"不能指向未揭示的隐蔽牌"
    # （`BP_PlayerMoves.cpp:2213` 那段，除非出牌方带 canTargetCovert）。
    "IsUnrevealedCovertCard": lambda self, c: (
        _kw(c, "has_covert") and not bool(_need(F(c, "is_revealed"), "is_revealed"))),
}


# --------------------------------------------------------------------------
# 明确**还做不到**的（读不到所需字段）—— 留在这里是为了让人一眼看清缺口在哪，
# 而不是等到运行时才发现。它们不进 _TABLE，调用就抛 Unimplemented。
# --------------------------------------------------------------------------
def _abilities(card) -> list:
    """这张卡**被贴上的能力名**（小写）。

    来自 `receivedAbilitiesFromCards`（`cards.read_live_effects`）。
    ★ `read_raw` 里**没有**这一项 ⇒ 调用方的 `view` 必须把它补进来
      （`semantics/legality.py` 的 `make_rich_view` 就是干这个的）。
      补不进来就返回 None，让原语抛 `Unimplemented` —— 宁可停下，不要答错。
    """
    v = card.get("received_abilities") if isinstance(card, dict) else None
    if v is None:
        return None
    return [(a.get("ability") or "").lower() for a in v]


def _custom_attr(card, key, attr):
    """`CustomNameNHasAttribute(attr)` 的**半个实现**（见调用处注释）。"""
    v = card.get(key) if isinstance(card, dict) else None
    if v in (None, "", "None"):
        return False
    raise Unimplemented(
        "CustomNameHasAttribute：%s=%r 非空，而这串名字的格式还没解析（§11.2 P2）"
        % (key, v))


NOT_READABLE = {
    "IsVeteran": "没有 veteran 字段（cards.py 现有 10 个关键词里没有它）",
    # ★ getHasGameplayTag / HasCustomAbility 已经在 PRIMS 里实现了（分别是
    #   2026-09-23 补的 FGameplayTagContainer@0x30 读取器、和更早就有的
    #   receivedAbilitiesFromCards 判据）——这份字典只是文档，不接进调度，
    #   两条留着没删就成了两条假消息，误导下一个来查的人。已划掉。
    "HasCustomAbilityFromCard": "同上（这条是真的还没实现：这是查『某张具体的卡』有没有\
给出这个能力，`_abilities()` 现在只读得出能力名的并集，读不出是谁给的）",
    # isBuffedByCard / getCardsBuffedByThisCard 也已经在 PRIMS 里实现了
    # （2026-09-24，`buffsFromCards`/`cardsBuffedByThisCard` 早就有读法，
    # 原来这两条理由也是没查就写的）——已划掉。
    "WhichChooseOne": "抉择分支，需要 customJson(0x518) 解析（规格 §11 的 P2）",
    "ShouldGotchaTrigger": "需要触发条件求值",
}
# 战役专有：排位/休闲对局里恒为假，但**我们不假设**你在打哪种模式。
# 传 `CardNatives(board, campaign=False)` 才会按"非战役"回答。
CAMPAIGN_ONLY = ("HasCampaignUpgrade", "CampaignSetText", "CampaignAddKreditCost",
                 "CampaignAddDefense", "CampaignAddAttack", "CampaignAddBlitz")


def _campaign_false(self, card, *args):
    if self.campaign:
        raise Unimplemented("战役模式下的 Campaign* 还没实现")
    return False


for _n in CAMPAIGN_ONLY:
    _TABLE[_n] = _campaign_false


# --------------------------------------------------------------------------
# ★ BySide 族：side(座位) → 座位 / 位置 / 指挥点（2026-10-02，1.60.27292.launcher，IDA f773083b）
# --------------------------------------------------------------------------
# 约定（对所有函数）：
#   * side 一律是 **ESideEnum 座位号**：0=NotAvailable 1=left 2=right（不是 local/enemy）。
#   * 位置是 ECardLocationEnum：1/2=Deck_Left/Right 3/4=Hand_Left/Right 5/6=Board_HQLeft/Right（整个后排）
#     7=Frontline 8=Discard 9=Deck；0=NotAvailable。
#   * 接收者不是卡（静态库函数 / AkardsGameState 方法）⇒ 登记在 `_NO_RECEIVER`，`call()` 不去读它。
#   * `wc` = `__WorldContext`/`WorldContextObject`：字节码里作为**入参**压进调用点（UCombatHelperFunctions 的静态函数），
#     真函数不读它（IDA：callee 无参数引用）；这里收下并忽略。AkardsGameState / GameStateAccessSubsystem 的同名方法没有它。
#   * 返回值约定同本模块：单值直接 return；VM 会把它 `_write_outs` 到末尾出参（GameState 的 void+out 版本）
#     或直接当表达式值（静态库函数的 ReturnValue）。
#   * 需要"本地占哪个座位"的一律要 `my_seat`，需要"轮到谁"的一律要 `board.our_turn`；缺了抛 Unimplemented，不猜。
#   * 这一族**都不消耗随机数**；只读函数无副作用；`set*BySide`/`SetPlayingSide`/`SetActiveSide` 是写，不实现（effectvm 记录）。
_NO_RECEIVER = {
    "GetTheOtherSide", "GetSupportLineBySide", "GetHandLocationBySide", "GetDeckLocationBySide",
    "GetClientSide", "GetOpponentSide", "GetPlayingSide", "IsClientPlaying",
    "GetClientSideHandLocation", "GetOpponentSideHandLocation", "GetActiveSide",
    "getKreditBySide", "getKreditSlotBySide", "getMaxPossibleKredits",
    "GetMatchController",
    "IsReconnectMatch",           # 接收者是 MatchController 对象（不是卡）——别去 _view 它
    "GetGameInstanceSubsystem",   # USubsystemBlueprintLibrary 的静态函数（A7b）
    "GetActorOfClass",            # UGameplayStatics 的静态函数（A7b 链上的下一站）
    "GetAllActorsOfClass",        # 同上（第 2 局 SOUL OF OLD JAPAN 的卡点）
}


def _sd(x) -> int:
    """容忍 0 的座位归一：0/None→0，1/2→1/2，'left'/'right'→1/2；其它抛。"""
    if x is None or x == 0:
        return 0
    return _seat(x)


def _need_seat(self):
    if self.my_seat is None:
        raise Unimplemented("需要知道本地占哪个座位（my_seat）")
    return self.my_seat


def _need_turn(self):
    if self.board is None:
        raise Unimplemented("需要 BoardState（轮到谁）")
    ot = getattr(self.board, "our_turn", None)
    if ot is None:
        raise Unimplemented("BoardState.our_turn 读不到")
    return bool(ot)


def _need_max_kredits(self, c=None):
    """`getMaxPossibleKredits` 的真值 —— `int32@GS+0x338`（IDA 0x144B3C010）。

    ★ 2026-10-02（用户）：盘面快照现在带 `max_possible_kredits`（board_api.BGS_I32）⇒ 如实返回；
      快照没读到才抛 —— **不拿槽数顶替**（那是旧实现把 `getMaxPossibleKredits` 当槽数的错）。
    """
    if self.board is None:
        raise Unimplemented("需要 BoardState（getMaxPossibleKredits）")
    v = getattr(self.board, "max_possible_kredits", None)
    if not isinstance(v, int):
        raise Unimplemented("getMaxPossibleKredits：快照没读到 GameState+0x338")
    return int(v)


_MC_CACHE: dict = {}


def _match_controller(ks) -> int:
    """关卡 actor 里的 `MatchController_C`（= C++ `AMatchControllerV2` 的 BP 子类）。

    实机（2026-10-02）：`Locator(ks.m, ks.base).actors()`（267 个）里恰好一个
    `MatchController_C`。`CRUISER SCOUTS` / `STRETCH THE LINE` 的效果链里
    `LetObj: GetMatchController` 需要它 —— 之前 VM 停在这 ⇒ `SetCardsSeenByCipher`
    到不了 ⇒ 情报触发拿不到。找不到返回 0（None），让蓝图里的判空分支自己走。
    """
    if ks is None:
        raise Unimplemented("GetMatchController 需要进程会话（ks）")
    import time as _t
    pid = getattr(ks, "pid", None)
    hit = _MC_CACHE.get(pid)
    if hit and (_t.time() - hit[1]) < 2.0:
        return hit[0]
    try:
        from kardsmem.objects import ObjectArray
        from kardsmem.world import Locator
        oa = ObjectArray(ks)
        for a in Locator(ks.m, ks.base).actors():
            try:
                if oa.class_name(a) == "MatchController_C":
                    _MC_CACHE[pid] = (a, _t.time())
                    return a
            except Exception:                                     # noqa: BLE001
                continue
    except Exception:                                             # noqa: BLE001
        pass
    return 0


_ACTOR_CACHE: dict = {}


def _actors_snapshot(ks, ttl: float = 5.0):
    """`[(actor, class)]` 快照，**两个 GetActor*OfClass 共用**。

    ★ 2026-10-02 性能：`Locator.actors()` 每次重读整张 actor 表，而 CRUISER SCOUTS 的链
    会反复问不同类 ⇒ 冷跑实测 **5.58 s**（远超单卡预算）。actor 表在几秒内不会变，
    这里按 pid 缓存 5 s，两条原语共用同一份。
    """
    import time as _t
    from kardsmem.objects import ObjectArray
    from kardsmem.world import Locator
    pid = getattr(ks, "pid", None)
    key = (pid, "__actors__")
    hit = _ACTOR_CACHE.get(key)
    if hit and (_t.time() - hit[1]) < ttl:
        return hit[0]
    oa = ObjectArray(ks)
    rows = [(a, oa.class_of(a)) for a in Locator(ks.m, ks.base).actors()]
    _ACTOR_CACHE[key] = (rows, _t.time())
    return rows


def _class_matches(ks, cls: int, uclass: int) -> bool:
    """`uclass` 是不是 `cls`（或它的子类）——顺着 `SuperStruct` 走，最多 16 层。"""
    cur, depth = uclass, 0
    while cur and depth < 16:
        if cur == cls:
            return True
        cur = ks.m.ptr_or_zero(cur + 0x40)
        depth += 1
    return False


def _gobjects_of_class(ks, cls: int, limit: int = 8, ttl: float = 300.0):
    """**兜底**：关卡 actor 表里找不到时，按类名做一次全对象扫（慢 4–9 s，按类缓存 5 min）。

    ★ 2026-10-02（SOUL OF OLD JAPAN 实机）：它向 `GetAllActorsOfClass` 要的是
    `BP_Logic_C_2147355775`（局内 Logic actor），而 `Locator.actors()` 读到的 233 个里
    **没有它** ⇒ 返回空表 ⇒ 蓝图后面读空对象的字段（`cards_reserve_changes`）整条断。
    这里加一层"按类名找实例"的慢速兜底；命中后同样按 (pid, 类) 缓存。
    """
    import time as _t
    from kardsmem.objects import ObjectArray
    pid = getattr(ks, "pid", None)
    key = (pid, cls, "__gobjects__")
    hit = _ACTOR_CACHE.get(key)
    if hit and (_t.time() - hit[1]) < ttl:
        return hit[0]
    oa = ObjectArray(ks)
    name = oa.pool().fname_of(cls) if cls else None
    found = []
    if name:
        found = oa.find_by_class_name(name, limit=max(1, int(limit))) or []
    _ACTOR_CACHE[key] = (found, _t.time())
    return found


def _get_actor_of_class(self, ctx=None, cls=None):
    """`UGameplayStatics::GetActorOfClass(WorldContextObject, ActorClass) -> AActor*`。

    ★ 2026-10-02（CRUISER SCOUTS 实机复验链）：先前的 `LogError` no-op 之后停在这里。
    做法与 `_match_controller` 同源（`Locator.actors()` 读关卡 actor 表，实测 267 个），
    但这里**按传入的类**匹配，且顺着 `SuperStruct` 认父类（蓝图传基类的情况）。
    结果按 (pid, 类) 缓存 2 s —— 决策链里同一帧可能问好几次。
    """
    if not getattr(self, "ks", None):
        raise Unimplemented("GetActorOfClass 需要进程会话（ks）")
    if not cls:
        return 0
    pid = getattr(self.ks, "pid", None)
    key = (pid, cls)
    hit = _ACTOR_CACHE.get(key)
    if hit and (time.time() - hit[1]) < 30.0:          # 类级命中缓存 30 s
        return hit[0]
    for a, uc in _actors_snapshot(self.ks):
        if _class_matches(self.ks, cls, uc):
            _ACTOR_CACHE[key] = (a, time.time())
            return a
    fb = _gobjects_of_class(self.ks, cls, limit=4)          # 关卡 actor 表没有 ⇒ 慢速兜底
    if fb:
        _ACTOR_CACHE[key] = (fb[0], time.time())
        return fb[0]
    return 0


def _get_all_actors_of_class(self, ctx=None, cls=None):
    """`UGameplayStatics::GetAllActorsOfClass(WorldContextObject, ActorClass, &OutActors)`。

    ★ 2026-10-02（第 2 局日志）：`SOUL OF OLD JAPAN` 停在 `CallMath: GetAllActorsOfClass`。
    与 `_get_actor_of_class` 同一套数据源（`Locator.actors()`），区别是**返回全部命中**，
    并且顺 `SuperStruct` 认父类。结果按 (pid, 类) 缓存 2 s（一帧内可能问多次）。
    """
    if not getattr(self, "ks", None):
        raise Unimplemented("GetAllActorsOfClass 需要进程会话（ks）")
    if not cls:
        return []
    pid = getattr(self.ks, "pid", None)
    key = (pid, cls, "all")
    hit = _ACTOR_CACHE.get(key)
    if hit and (time.time() - hit[1]) < 30.0:
        return hit[0]
    out = [a for a, uc in _actors_snapshot(self.ks) if _class_matches(self.ks, cls, uc)]
    if not out:
        out = _gobjects_of_class(self.ks, cls, limit=16)
    _ACTOR_CACHE[key] = (out, time.time())
    return out


def _get_game_instance_subsystem(self, ctx=None, cls=None):
    """`USubsystemBlueprintLibrary::GetGameInstanceSubsystem(ContextObject, Class) -> UObject*`。

    ★ 2026-10-02（TODO A7b）：`CRUISER SCOUTS` 的打出链会调它；VM 撞上就整条断。
    它是**库静态函数**（全游戏 368 次调用 / 91 个调用者，`census2/calls_raw.json`），
    接收者是函数库、不是卡 ⇒ 登记进 `_NO_RECEIVER`。

    实现走"一次性索引"（`kardsmem.subsystems`）：进程里 96,044 个对象、只读类指针全扫
    5.28 s，放进求值链不可行；而 `UGameInstance::SubsystemCollection` 又不是反射属性
    （`BP_KardsGameInstance_C` 的 61 个属性里没有它）⇒ 只能启动时建索引、之后查表。
    找不到返回 0（蓝图自己的判空分支会走）。
    """
    if not getattr(self, "ks", None):
        raise Unimplemented("GetGameInstanceSubsystem 需要进程会话（ks）")
    from . import subsystems as _subs
    return _subs.find(self.ks, cls) or 0


def _is_reconnect_match(self, mc=None):
    """`AMatchControllerV2::IsReconnectMatch(&isReconnecting, &clientMulliganDone, &otherMulliganDone)`。

    ★ 2026-10-02（`+intel` 的最后一个卡点，TODO A7）：`CRUISER SCOUTS` / `STRETCH THE LINE`
      的打出钩子会调它；VM 以前撞上就整条断 ⇒ `SetCardsSeenByCipher` 到不了 ⇒ 情报标记不触发。

    ★★ 只读实测（20:24，`_nn_scratch/probe_mc_chain.py` 走 CDO 的类链）确认：
      **这三个名字是那个函数的出参，不是类上的字段**（`GObjects-Dump-WithProperties.txt`
      里它们出现在 `IsReconnectMatch` 的参数表 `[00000000..00000002]`）⇒ 反射**不可能**找到它们。
      运行时的 `MatchController_C` 类链上与本函数相关的真实字段只有：
        · `reconnectInSameTurn`（Bool，父类，off 760）
        · `MulliganData`（Struct，BP 类，off 4008）、`mulliganReplacementReceived`（Bool，4056）
        · `reconnectLoading`（ObjectProperty，4104）
    ★★ 2026-10-02 第二步：**调用点已经读了**（`_nn_scratch/probe_reconnect_callsite.py` /
      `probe_reconnect_body.py`）。唯一消费它的是 BP `CreateAction_AddSubAction`，形态是：
        21  Context(GetMatchController) → FinalFunction(IsReconnectMatch)(&a,&b,&c)
        80  JumpIfNot(to=276) on `a`        ← 只有第一个出参决定分支
        85..271  重连分支（ExecuteXActionsWithLocalSubActions），`b`/`c` 只在这里被读
        276..    正常路径（拼 action 描述），**不读 a/b/c**
      ⇒ 非重连局里"返回值"只影响走哪条分支；`a=False` 就是走正常路径。
      实测四个可读字段在**正常对局**里的值（`probe_reconnect_fields.json`）：
        `reconnectInSameTurn=false`、`reconnectLoading=0x0`、
        `mulliganReplacementReceived=true`（**正常局也 true ⇒ 不能当重连判据**）、
        `MulliganData` 48 字节非零（同上）。
      ⇒ 护栏只用 `reconnectInSameTurn` + `reconnectLoading`：
        两者都"灭" ⇒ 断言非重连局，返回 `(False, False, False)`；
        任一"亮"或读不到 ⇒ 抛 `Unimplemented`（重连分支要 IDA 函数体，不猜）。
      `b`/`c` 在正常路径上无人消费，给 False 与"没有洗牌替换在进行"一致。
      **函数体本身仍未读** ⇒ 整个原语登记进 `APPROX`，求值器会把"掺了近似"冒泡出去。
      返回 tuple ⇒ VM 按位置写回三个出参。
    """
    ptr = mc if isinstance(mc, int) and mc else _match_controller(self.ks)
    if not ptr:
        raise Unimplemented("IsReconnectMatch: 找不到 MatchController_C")
    try:
        from kardsmem import props as _props
        from kardsmem.objects import ObjectArray as _OA
        uc = _OA(self.ks).class_of(ptr)
    except Exception as exc:                                   # noqa: BLE001
        raise Unimplemented("IsReconnectMatch: 取类失败 %s" % exc)
    pool = self.ks.names_pool() if hasattr(self.ks, "names_pool") else None
    try:
        all_props = _props.class_props(self.ks, uc, pool)
    except Exception as exc:                                   # noqa: BLE001
        raise Unimplemented("IsReconnectMatch: 取属性表失败 %s" % exc)

    def _find(*want):
        """按名字找属性：**大小写不敏感**，并容忍游戏自己的拼写（导出里就有 `SetIsReconecting`
        这种少一个 n 的写法）——把候选名字逐个试。"""
        for w in want:
            lw = w.lower()
            for r in all_props:
                if str(r.get("name") or "").lower() == lw:
                    return r
        for w in want:                                         # 再放宽：包含子串
            lw = w.lower().replace("reconnecting", "reconect")
            for r in all_props:
                if lw in str(r.get("name") or "").lower():
                    return r
        return None

    # ---- 护栏：只用 reconnectInSameTurn / reconnectLoading 判"非重连局" ----
    #   （`mulliganReplacementReceived` 与 `MulliganData` 在正常局也亮，见 docstring 实测。）
    sig = {}
    for nm in ("reconnectInSameTurn", "reconnectLoading"):
        pr = _find(nm)
        if not pr:
            raise Unimplemented("IsReconnectMatch: 类上找不到 %s（无法判定是否重连局）" % nm)
        try:
            if pr.get("type") == "BoolProperty":
                v = _props.read_bool(self.ks.m, ptr, pr)
            elif pr.get("type") in ("ObjectProperty", "ClassProperty"):
                v = bool(self.ks.m.ptr_or_zero(ptr + (pr.get("offset") or 0)))
            else:
                raw = self.ks.m.u8(ptr + (pr.get("offset") or 0))
                v = None if raw is None else bool(raw)
        except Exception:                                      # noqa: BLE001
            v = None
        if v is None:
            raise Unimplemented("IsReconnectMatch: 字段 %s 读不到（无法判定是否重连局）" % nm)
        sig[nm] = bool(v)
    hot = [k for k, v in sig.items() if v]
    if hot:
        raise Unimplemented(
            "IsReconnectMatch: 检测到重连迹象 %s ⇒ 重连分支需要 AMatchControllerV2::"
            "IsReconnectMatch 的函数体（IDA）；非重连局已可判定" % hot)
    return (False, False, False)


def _can_end_turn(self, c=None):
    """`AMatchControllerV2::CanEndTurn() -> bool` —— **默认 True（近似）**。

    ★ 2026-10-02（用户）："默认它是 true，没影响" —— 核实过：全 1.60 导出里
      `CanEndTurn()` 只有 **2 个调用点**，都在 UI/Logic，不在任何卡牌效果链上：
        · `BP_Widget_Battle_HUD.cpp` —— 决定"结束回合"按钮的 disabled；
        · `BP_Logic.cpp` —— `onCardSelectionConfirmed()` 的前置闸门。
      函数体没反编译过（NATIVE-COVERAGE §14.4 把它列为未做）⇒ 这是**近似**，
      已登记进 `APPROX`（求值器会把"掺了近似"一路冒泡到结果里）。
    """
    return True


def _other(s: int) -> int:
    return {1: 2, 2: 1}.get(s, 0)


def _playing_side(self) -> int:
    """GameState.playingSide@0x38E（byte）。外部：轮到本地 ⇒ my_seat，否则对面座位。"""
    me = _need_seat(self)
    return me if _need_turn(self) else _other(me)


def _kredit_like(self, side, field):
    """getKreditBySide / getKreditSlotBySide：side 不是 1/2 ⇒ -100（IDA）。"""
    s = _sd(side)
    if s not in (1, 2):
        return -100
    if self.board is None:
        raise Unimplemented("%s 需要 BoardState" % field)
    me = _need_seat(self)
    v = (getattr(self.board, field, None) or {}).get("local" if s == me else "enemy")
    return _need(v, "%s[%s]" % (field, s))


def _opposite_side2(self, card):
    """UBaseCardObject::GetOppositeSide（IDA 0x144AF7700）：side@0x276==1→2，==2→1，否则**不写出参**⇒调用方变量保持零值 0。"""
    raw = F(card, "side_enum")
    if raw is None:
        raise Unimplemented("需要 side_enum（座位号）；只有归一后的 side 是不够的")
    return _other(_sd(raw))


_TABLE.update({
    # --- UCombatHelperFunctions（原生静态，蓝图可调；IDA 已验证）---
    # 签名：ESideEnum GetTheOtherSide(ESideEnum SideToGet, UObject* WorldContextObject)   thunk 0x144A5F040 → 0x144AF9B90
    #   1→2, 2→1, 其它→0。
    "GetTheOtherSide": lambda self, c, s, wc=None: _other(_sd(s)),
    # ECardLocationEnum GetSupportLineBySide(side, wc)   thunk 0x144A5EF70 → 0x144AF9AA0    1→5(HQLeft) 2→6(HQRight) 其它→0
    "GetSupportLineBySide": lambda self, c, s, wc=None: {1: 5, 2: 6}.get(_sd(s), 0),
    # ECardLocationEnum GetHandLocationBySide(side, wc)   thunk 0x144A5EC70 → 0x144AF49C0   1→3(HandLeft) 2→4(HandRight) 其它→0
    "GetHandLocationBySide": lambda self, c, s, wc=None: {1: 3, 2: 4}.get(_sd(s), 0),
    # ECardLocationEnum GetDeckLocationBySide(side, wc)   thunk 0x144A5E960 → 0x144AF3250   1→1(DeckLeft) 2→2(DeckRight) 其它→0
    "GetDeckLocationBySide": lambda self, c, s, wc=None: {1: 1, 2: 2}.get(_sd(s), 0),
    # ECardLocationEnum GetClientSideHandLocation(wc)  thunk 0x144A5E540 → 0x144AEFB40
    #   clientSide(GameState+0x38C)==1→3，==2→4，否则 0；GameState 取不到也是 0。
    "GetClientSideHandLocation": lambda self, c, wc=None: {1: 3, 2: 4}.get(_need_seat(self), 0),
    # ECardLocationEnum GetOpponentSideHandLocation(wc)  thunk 0x144A5EE50 → 0x144AF76C0
    #   opponentSide(GameState+0x38D)==1→3，==2→4，否则 0（注意：是"对手座位"的手牌位置，不是 4-3 取反）。
    "GetOpponentSideHandLocation": lambda self, c, wc=None: {1: 3, 2: 4}.get(_other(_need_seat(self)), 0),
    # ESideEnum GetClientSide / GetOpponentSide / GetPlayingSide(wc) 与 bool IsClientPlaying(wc)
    #   thunks 0x144A5E4B0/0x144A5EDC0/0x144A5EEE0/0x144A5F110，都是 `GameState = sub_144AF4900(wc)`，GameState 取不到 ⇒ 0/False；
    #   否则分别读 byte@0x38C(client) / @0x38D(opponent) / @0x38E(playing)；
    #   IsClientPlaying = (client!=0 and playing!=0 and client==playing)。
    #   同名方法还在 UGameStateAccessSubsystem（无入参，同样读 +908/+909/+910，IDA 0x144A65530/0x144A65A50/0x144A65B20/0x144A65D90）
    #   和 AkardsGameState（void + 出参，0x144A65490/0x144A659B0/0x144A65A80/0x144A65CF0）。三者用同一实现。
    "GetClientSide": lambda self, c, wc=None: _need_seat(self),
    "GetOpponentSide": lambda self, c, wc=None: _other(_need_seat(self)),
    "GetPlayingSide": lambda self, c, wc=None: _playing_side(self),
    "IsClientPlaying": lambda self, c, wc=None: _need_turn(self),
    # AkardsGameMode::GetActiveSide（byte@0x388，IDA 0x144A653E0）/ AMatchControllerV2::GetActiveSide（byte@MC+0xCF9=3321，IDA 0x144AEF2E0）
    #   ⚠ 这两个字段只验证了"是 byte 读取"；"= 当前行动方"按名字+用途**推断**，与 playingSide 同值。
    "GetActiveSide": lambda self, c, wc=None: _playing_side(self),

    # --- AkardsGameState 指挥点读数（IDA：getKreditBySide 0x144B3BE50 / getKreditSlotBySide 0x144B3BF30）---
    # void getKreditBySide(ESideEnum SideToGet, int32& outputKredit)：记录 side1@GS+0x33C、side2@GS+0x350（20 字节：mult,add,spare,enc,frame），
    #   key@GS+0x334；值 = trunc(((enc ^ key) - add) / mult)；mult==0 或 side∉{1,2} ⇒ **-100**。
    "getKreditBySide": lambda self, c, s: _kredit_like(self, s, "kredits"),
    # void getKreditSlotBySide(side, int32& outputKreditSlot)：同上，记录在 side1@0x364、side2@0x378；非法 ⇒ -100。
    "getKreditSlotBySide": lambda self, c, s: _kredit_like(self, s, "slots"),
    # void getMaxPossibleKredits(int32& out)：直接读 int@GS+0x338（IDA 0x144B3C010）。
    # ★ 2026-10-02 快照已带该字段（board_api.BGS_I32 → BoardState.max_possible_kredits）⇒ 如实返回。
    "getMaxPossibleKredits": _need_max_kredits,
    # bool CanEndTurn()（AMatchControllerV2，原生）—— 默认 True 的**近似**（见函数注释/APPROX）。
    "CanEndTurn": _can_end_turn,
    # void GetMatchController(AMatchControllerV2*& Out)：返回关卡里的 `MatchController_C`
    #   actor（实机 267 个 actor 里恰好 1 个；`CRUISER SCOUTS` / `STRETCH THE LINE` 的
    #   `LetObj: GetMatchController` 需要它，拿不到就停 ⇒ 情报链断）。
    "GetMatchController": lambda self, c, *a: _match_controller(self.ks),
    # ★ 2026-10-02（TODO A7）：`AMatchControllerV2::IsReconnectMatch(&isReconnecting,
    #   &clientMulliganDone, &otherMulliganDone)` —— `CRUISER SCOUTS` / `STRETCH THE LINE`
    #   的打出钩子会调它，VM 以前撞上就整条断（情报标记因此永远出不来）。
    #   实现在 MatchController_C 上按反射读三个同名字段；读不到如实抛 Unimplemented。
    "IsReconnectMatch": _is_reconnect_match,
    # ★ 2026-10-02（TODO A7b）：`USubsystemBlueprintLibrary::GetGameInstanceSubsystem` ——
    #   CRUISER SCOUTS 链上的最后一个已知卡点；靠 `kardsmem.subsystems` 的一次性索引。
    "GetGameInstanceSubsystem": _get_game_instance_subsystem,
    # ★ 2026-10-02：`UGameplayStatics::GetActorOfClass` —— CRUISER SCOUTS 链上 LogError 之后的卡点
    "GetActorOfClass": _get_actor_of_class,
    # ★ 2026-10-02（第 2 局）：`UGameplayStatics::GetAllActorsOfClass` —— SOUL OF OLD JAPAN 的卡点
    "GetAllActorsOfClass": _get_all_actors_of_class,

    # --- UBaseCardObject::GetOppositeSide 修正（边界：side=0 ⇒ 0，不再抛）---
    "GetOppositeSide": _opposite_side2,
})


# --------------------------------------------------------------------------
# ★ 读侧复核后的更正与补全（2026-10-02，IDA 对照；逐条依据见 NATIVE-COVERAGE-1.60.md §14）
#   这一块用 `_TABLE.update` 覆盖上面同名旧实现（旧实现留在文件里作对照，别再改它们）。
#   每个函数：①名字/类/种类/IDA 地址在注释首行；②参数约定；③实现。
# --------------------------------------------------------------------------
def _kw_has(c, key, plain) -> bool:
    """flag 位：view 是 read_raw 时 `keywords` 里有 `has_xxx`；是 board_api.Card 时有 `xxx`。"""
    ks = F(c, "keywords")
    if ks is None:
        raise Unimplemented("需要 keywords，但这个后端没给")
    return key in ks or plain in ks


def _rcv(c, name) -> int:
    """receivedAbilitiesFromCards[name] 的给予者数（IDA：flag OR 该数组非空）。缺 received_abilities 抛。

    ★ D5/#22：**givers 缺失时要抛**，不能猜成 1 —— 游戏要求该数组非空（NUM>0）才算命中；
      给不出 NUM 就该说"不知道"（Unimplemented），而不是假装有一个给予者。键比较大小写不敏感。
    """
    rows = F(c, "received_abilities")
    if rows is None:
        raise Unimplemented("需要 received_abilities（view 没给）")
    k = _fold(name)
    for a in rows:
        if _fold(a.get("ability") or "") == k:
            g = a.get("givers")
            if g is None:
                raise Unimplemented("received_abilities 的 givers 读不到（游戏要求数组非空，不能猜）")
            return len(g)
    return 0


def _kwfn(flag_key, plain, ability):
    def fn(self, c):
        # 先看 flag：为真就不需要 received_abilities（短路与游戏一致）
        if _kw_has(c, flag_key, plain):
            return True
        return _rcv(c, ability) > 0
    return fn


def _segs(c, key):
    s = F(c, key)
    if s is None:
        raise Unimplemented("需要 %s（view 没给 customName；read_raw 默认会给）" % key)
    # §3 #26：游戏按 `Stricmp(str,"None")≠0` 排除 None（**大小写不敏感**），空段也丢。
    return [x for x in str(s).split(";") if x and _fold(x) != "none"]


def _attr2(c, key, attr) -> bool:
    """CustomNameNHasAttribute（IDA 0x144AE5570）：attr 空⇒False；customName 按 ';' 切、去空段、整段相等。
    比较函数 sub_141215850 疑似大小写不敏感，未核 ⇒ 用 casefold（游戏里属性名大小写固定，不影响）。"""
    if not attr:
        return False
    a = str(attr).casefold()
    return any(x.casefold() == a for x in _segs(c, key))


def _type_or(t, attr):
    def fn(self, c):
        if _need(F(c, "card_type"), "card_type") == t:
            return True
        return _attr2(c, "custom_name1", attr)
    return fn


def _ability_has(c, name) -> bool:
    return _rcv(c, name) > 0


def _cant(c, ability) -> bool:
    """HasCantAttack*/HasCantBeAttackedBy（IDA 0x144AFA690/6F0/780）：customName1 含该属性 OR 有该自定义能力。"""
    return _attr2(c, "custom_name1", ability) or _ability_has(c, ability)


def _json_keys(c):
    k = F(c, "custom_json_keys")
    if k is None:
        raise Unimplemented("需要 custom_json_keys（view 没给）")
    return k


def _combat_keywords(self, c):
    out = [k for k, fn in ((1, "getHasAmbush"), (2, "getHasBlitz"), (3, "getHasFury"),
                           (4, "getHasGuard"), (6, "getHasShock"), (7, "getHasSmokescreen"))
           if self.call(fn, c)]
    if self.call("getTotalHeavyArmor", c) > 0:
        out.append(5)
    return out, len(out)


def _temp_buff(c, iid, key) -> int:
    rows = F(c, "buffs_from_cards")
    if rows is None:
        raise Unimplemented("需要 buffs_from_cards（view 没给）")
    k = _fold(key)
    for row in rows:
        if row.get("giver_card_id") == iid:
            # §2 #3：内层 BuffMap 的键串比较**大小写不敏感**（游戏里 CRC 前折大小写）。
            for kk, v in (row.get("buffs") or {}).items():
                if _fold(kk) == k:
                    return int(v)
            return 0
    return 0


def _weather(c) -> bool:
    tags = F(c, "gameplay_tags")
    if tags is None:
        raise Unimplemented("需要 gameplay_tags（view 没给）")
    ws = ("subtype.rain", "subtype.sunny", "subtype.storm")
    return any(t == w or str(t).startswith(w + ".") for t in tags for w in ws)


def _has_tag(c, tag) -> bool:
    """`getHasGameplayTag` 的游戏语义（IDA 0x144B14580，D5）：

    `tag ∈ GameplayTags@0x30`（精确 FName）**或** `tag ∈ ParentTags@0x40`（层级：显式持有
    子 tag ⇒ 视为持有父 tag；我们用"显式 tag == 父 or 以 `父.` 开头"等价）**或** 把 tag 名
    小写后在 `receivedAbilitiesFromCards@0x208` 里能查到键（键比较大小写不敏感）。
    """
    t = _fold(tag)
    for e in _need(F(c, "gameplay_tags"), "gameplay_tags"):
        e = _fold(e)
        if e == t or e.startswith(t + "."):
            return True
    rows = F(c, "received_abilities") or []
    return any(_fold(a.get("ability") or "") == t for a in rows)


def _static_has(self, name) -> bool:
    if self.static_cards is None:
        raise Unimplemented("需要静态卡表（CardNatives.static_cards = {小写类名}）")
    return str(name).lower() in self.static_cards


def _veteran(self, c, ignore_suppress=False):
    """IsVeteran(ignoreSuppress,&isIt)（IDA 0x144AFD1C0）：`ignoreSuppress or !isSuppressed` 时 =
    customJson 含键 'veteran'；否则 False。"""
    if not ignore_suppress and bool(_need(F(c, "is_suppressed"), "is_suppressed")):
        return False
    return "veteran" in _json_keys(c)


def _pinned(self, c):
    """IsPinned（IDA 0x144AFCD30）：pinnedTurns@0x27C > 0。旧实现靠 receivedAbilities/buffs 里的 'pinned' 字样，
    与游戏判据不是同一个字段。view 没有 pinned_turns 时才退回旧判据（近似）。"""
    pt = F(c, "pinned_turns")
    if pt is not None:
        return pt > 0
    return (any("pinned" in a for a in _need(_abilities(c), "received_abilities"))
            or any("pinned" in k.lower() for b in (c.get("buffs_from_cards") or [])
                   for k in (b.get("buffs") or {})))


def _from_card(c, ability, giver) -> bool:
    rows = F(c, "received_abilities")
    if rows is None:
        raise Unimplemented("需要 received_abilities")
    for a in rows:
        if (a.get("ability") or "").lower() == str(ability).lower():
            g = a.get("givers")
            if g is None:
                raise Unimplemented("received_abilities 没带 givers，答不了『谁给的』")
            return giver in g
    return False


def _should_gotcha(self, c, trig):
    """ShouldGotchaTrigger(triggerCard,&shouldIt)（IDA 0x144B0DDD0）。
    ① triggerCard 非空 且 未揭示 且 带隐蔽 且 本卡 customName1 没有 'canTargetCovert' ⇒ False；
    ② 本卡在手牌(location 3/4) 且 gotchaActivated>0 且 activeSide != 本卡 side ⇒ 暂定 True，否则 False；
    ③ 暂定 True 时还要问 cardFunction：'sideeffect.blockgotcha'（会消耗一次）与『场上有 customJson 含 stop_gotcha 的卡』。
       这两项在 cardFunction 里，进程外没有 ⇒ 暂定 True 就抛 Unimplemented（不猜）；可注入 `self.gotcha_blockers`。"""
    tc = self._view(trig) if trig else None
    if tc is not None:
        if (not bool(_need(F(tc, "is_revealed"), "is_revealed"))
                and bool(_kw_has(tc, "has_covert", "covert"))
                and not _attr2(c, "custom_name1", "canTargetCovert")):
            return False
    loc = _need(F(c, "location_enum"), "location_enum")
    if loc not in (3, 4) or _need(F(c, "gotcha_activated"), "gotcha_activated") <= 0:
        return False
    if _playing_side(self) == _card_seat(c):
        return False
    gb = getattr(self, "gotcha_blockers", None)
    if gb is None:
        raise Unimplemented("ShouldGotchaTrigger：sideeffect.blockgotcha / stop_gotcha 在 cardFunction 里，进程外没有")
    return not gb(c)


_TABLE.update({
    # CanBeTargetted（虚，vtable+712 → 0x1422B85D0）：默认 canIt=true；0 个 BP 类覆写 ⇒ 直接登记是安全的。
    # 参数：(card, targettingCard, byPlayFromHand)，出参 4 个在前：canIt, Reason, reasonParam1, reasonParam2。
    "CanBeTargetted": lambda self, c, tc=None, by_hand=False: (True, "", "", ""),
    # getTotalOperationCost（IDA 0x144B15200）= max(0, operationCost@0xB0 + operationCostBuff@0xB4)
    "getTotalOperationCost": lambda self, c: max(0, _need(F(c, "operation_cost"), "operation_cost")
                                                  + _need(F(c, "operation_cost_buff"), "operation_cost_buff")),
    # getTotalKreditCost（IDA 0x144B15020）= clamp(kredit+kreditBuff, 0, 99)
    "getTotalKreditCost": lambda self, c: max(0, min(99, _need(
        F(c, "total_kredit_cost", "kredit_cost", "kredit"), "kredit_cost"))),
    "getAttackTempBuffAmount": lambda self, c, iid: _temp_buff(c, iid, "attack_tempBuffGive"),
    "getKreditTempBuffAmount": lambda self, c, iid: _temp_buff(c, iid, "kredit_tempBuffGive"),
    "GetCombatKeywords": _combat_keywords,
    # 关键词：flag OR receivedAbilitiesFromCards[name]（IDA 0x144B14300/14080/14920/147E0；其余同形）
    "getHasBlitz": _kwfn("has_blitz", "blitz", "blitz"),
    "getHasGuard": _kwfn("has_guard", "guard", "guard"),
    "getHasFury": _kwfn("has_fury", "fury", "fury"),
    "getHasAmbush": _kwfn("has_ambush", "ambush", "ambush"),
    "getHasShock": _kwfn("has_shock", "shock", "shock"),
    "getHasAlpine": _kwfn("has_alpine", "alpine", "alpine"),
    "getHasSmokescreen": _kwfn("has_smokescreen", "smokescreen", "smokescreen"),
    "getHasImmune": lambda self, c: bool(F(c, "is_immune")) or _rcv(c, "immune") > 0,
    # 类型（isAlso* 属性，IDA）
    "IsTank": _type_or("tank", "isAlsoTank"), "IsFighter": _type_or("fighter", "isAlsoFighter"),
    "IsBomber": _type_or("bomber", "isAlsoBomber"), "IsInfantry": _type_or("infantry", "isAlsoInfantry"),
    "IsArtillery": _type_or("artillery", "isAlsoArtillery"),
    "IsArmorUnit": lambda self, c: (_need(F(c, "card_type"), "card_type") in ("tank", "tankdestroyer")
                                    or _attr2(c, "custom_name1", "isArmorUnit")),
    "IsGunUnit": lambda self, c: _need(F(c, "card_type"), "card_type") in ("artillery", "antiair", "antitank"),
    "CanMoveAndAttackInTheSameTurn": lambda self, c: (self.call("IsArmorUnit", c)
                                                      or _attr2(c, "custom_name1", "CanMoveAndAttackInTheSameTurn")),
    "CustomName1HasAttribute": lambda self, c, a: _attr2(c, "custom_name1", a),
    "CustomName2HasAttribute": lambda self, c, a: _attr2(c, "custom_name2", a),
    "GetCustomName2Attributes": lambda self, c: _segs(c, "custom_name2"),
    # 能力 / 限制（IDA 0x144AFA810/8E0/690/6F0/780）
    "HasCustomAbility": lambda self, c, a: _ability_has(c, a),
    "HasCustomAbilityFromCard": lambda self, c, a, giver: _from_card(c, a, giver),
    "HasCantAttack": lambda self, c: _cant(c, "cantAttack"),
    "HasCantAttackType": lambda self, c, t: _cant(c, "cantAttack:" + str(t)),
    "HasCantBeAttackedBy": lambda self, c, t: _cant(c, "cantBeAttackedBy:" + str(t)),
    "IsPinned": _pinned,
    "IsVeteran": _veteran,
    "ShouldGotchaTrigger": _should_gotcha,
    # 状态
    "HasMovementLeft": lambda self, c: _need(F(c, "movement_left"), "movement_left") > 0,
    "HasIntel": lambda self, c: _need(F(c, "cipher"), "cipher") > 0,
    "getIntel": lambda self, c: max(0, min(9, _need(F(c, "cipher"), "cipher"))),
    # §2 #1（IDA 0x144AFC010）：**未夹**的 getAndDecryptDefense < maxDefense（mult==0 ⇒ 100 ⇒ True）。
    "IsDamaged": lambda self, c: _defense_raw(c) < _need(F(c, "max_defense"), "max_defense"),
    "IsExile": lambda self, c: _need(F(c, "exile_nation"), "exile_nation") != 0,
    "getExileNation": lambda self, c: _need(F(c, "exile_nation"), "exile_nation"),
    # D5：HasBond = getHasGameplayTag("ability.bond") ∧ ¬HasCustomAbility("bond_removed")（IDA）。
    "HasBond": lambda self, c: (_has_tag(c, "ability.bond")
                                and not _ability_has(c, "bond_removed")),
    "IsChooseOneCard": lambda self, c: _need(F(c, "choose_one_cards_n"), "choose_one_cards_n") != 0,
    # D5：IsWeatherCard = 任一 "subtype.{rain,sunny,storm}" 命中（ParentTags 层级）。
    "IsWeatherCard": lambda self, c: any(_has_tag(c, "subtype." + w)
                                         for w in ("rain", "sunny", "storm")),
    "IsForecastCard": lambda self, c: _need(F(c, "class_fname"), "class_fname").lower() in (
        "card_event_sunny1_blue_sky", "card_event_rain1_mist", "card_event_storm1_gale"),
    # §4 #72/#52：customJson 的键**存在即真**（值为 JSON null 也算）；键比较大小写不敏感。
    "hasActiveCountdownEffect": lambda self, c: any(
        _fold(k) == "countdown_timer" for k in _json_keys(c)),
    "getCountdownValue": lambda self, c: (int((F(c, "custom_json_nums") or {}).get("countdown_timer", -1))
                                          if "countdown_timer" in _json_keys(c) else -1),
    "hasActivePincerEffect": lambda self, c: any(
        _fold(k) in ("pincer_givers", "pincer_receiver") for k in _json_keys(c)),
    "getHasVeteranUpgrade": lambda self, c: _static_has(self, str(_need(F(c, "class_fname"), "class_fname")) + "_vet"),
    # HasCampaignUpgrade（IDA 0x144AFA660）：id ∈ activeUpgrades@0x328；PvP 恒空。view 没给就按"非战役=空"。
    "HasCampaignUpgrade": lambda self, c, i: ((int(i) in (F(c, "active_upgrades") or ())) if not self.campaign
                                              else _fail("战役模式下 HasCampaignUpgrade 需要 active_upgrades")),
})
CardNatives.static_cards = None       # 静态卡表（小写类名集合），getHasVeteranUpgrade 用；调用方可注入
CardNatives.gotcha_blockers = None    # callable(card)->bool：cardFunction 侧 blockgotcha/stop_gotcha 的答案


# --------------------------------------------------------------------------
# ★ UFunctionLibrary：比较 / 静态卡表（IDA 0x144A8A8E0→0x144B26540、0x144B2F700…；2026-10-02）
# --------------------------------------------------------------------------
_ETYPE = {"location": 1, "order": 2, "tank": 3, "fighter": 4, "bomber": 5, "infantry": 6, "artillery": 7,
          "antiair": 8, "antitank": 9, "tankdestroyer": 10, "gotcha": 11, "wildcard": 12}


def _en(x):
    return str(x).split("::")[-1].lower() if isinstance(x, str) else x


def _enum_compare(self, c, a, b):
    """EnumCompareSide / CardLocation / Faction / Type（四个共用一个真函数）：
    出参 `EnumAreTheyEqual Branches = (a != b)`，**Equal=0，NotEqual=1**（不是 bool(a==b)）。
    参数：args=[a, b]，出参写回末尾变量；Python 端返回 0/1。"""
    return 0 if _en(a) == _en(b) else 1


def _static(self, name):
    """静态卡表（GetStaticCard 查全局 TMap<FName,UBaseCardObject*>，IDA 0x144B2F700）。外部需注入
    `CardNatives.static_card = callable(name_lower)->view dict|None`；没注入就抛（不猜）。未命中游戏会 NewObject 一张空卡，
    外部不模拟这个路径：返回 None 时抛。"""
    f = getattr(self, "static_card", None)
    if f is None:
        raise Unimplemented("GetStatic*：需要注入静态卡表（CardNatives.static_card）")
    v = f(str(name).lower())
    if v is None:
        raise Unimplemented("静态卡表里没有 %r" % (name,))
    return v


def _static_exists(self, n):
    f = getattr(self, "static_card", None)
    if f is None:
        raise Unimplemented("StaticCardExists：需要注入静态卡表")
    return f(str(n).lower()) is not None


_TABLE.update({
    "EnumCompareSide": _enum_compare, "EnumCompareCardLocation": _enum_compare,
    "EnumCompareFaction": _enum_compare, "EnumCompareType": _enum_compare,
    # GetStaticType/Kredits（IDA）：Type@0x68 / kredits@0x80；Faction@0x7C / Rarity@0x11C（IDA-shape）
    "GetStaticType": lambda self, c, n: _ETYPE.get(_need(F(_static(self, n), "card_type"), "card_type"), 0),
    "GetStaticKredits": lambda self, c, n: _need(F(_static(self, n), "kredits_plain", "kredit"), "kredits_plain"),
    "GetStaticFaction": lambda self, c, n: _need(F(_static(self, n), "faction_enum"), "faction_enum"),
    "GetStaticRarity": lambda self, c, n: _need(F(_static(self, n), "rarity_enum"), "rarity_enum"),
    "GetStaticAttack": lambda self, c, n: _need(F(_static(self, n), "attack_plain"), "attack_plain"),
    "GetStaticDefense": lambda self, c, n: _need(F(_static(self, n), "defense_plain"), "defense_plain"),
    "GetStaticOperationCost": lambda self, c, n: _need(F(_static(self, n), "operation_cost"), "operation_cost"),
    "StaticCardExists": lambda self, c, n: (_static_exists(self, n)),
})
_NO_RECEIVER |= {"EnumCompareSide", "EnumCompareCardLocation", "EnumCompareFaction", "EnumCompareType",
                 "GetStaticType", "GetStaticKredits", "GetStaticFaction", "GetStaticRarity",
                 "GetStaticAttack", "GetStaticDefense", "GetStaticOperationCost", "StaticCardExists"}
CardNatives.static_card = None


# --------------------------------------------------------------------------
# ★ 静态卡表 / 枚举名 / 卡数组排序（IDA：GetStaticCard 0x144B2F700；GetFactionEnum 0x144B2D480=UEnum::GetValueByName）
# --------------------------------------------------------------------------
_FACTION = {"notavailable": 0, "germany": 1, "britain": 2, "japan": 3, "soviet": 4, "usa": 5, "france": 6,
            "italy": 7, "poland": 8, "finland": 9, "anzac": 10, "allies": 11, "neutral": 12}


def _sort_cards(key):
    def fn(self, c, arr):
        arr = list(arr or [])
        def k(p):
            v = self._view(p)
            x = F(v, *key)
            return (x is None, x if x is not None else 0)
        return sorted(arr, key=k)
    return fn


_TABLE.update({
    # GetStaticCard(FName)->UBaseCardObject*：返回**视图 dict**（不是指针；cardnatives 的后续调用直接吃 dict）。
    # 未命中时游戏会 NewObject 一张空卡，外部不模拟 ⇒ 抛。
    "GetStaticCard": lambda self, c, n: _static(self, n),
    # getStaticVeteranUpgrade(&staticVeteranCard)：静态卡 `<Name_0>_vet`（IDA 0x144B14DE0）
    "getStaticVeteranUpgrade": lambda self, c: _static(self, str(_need(F(c, "class_fname"), "class_fname")) + "_vet"),
    "GetStaticCardSet": lambda self, c, n: _need(F(_static(self, n), "card_set_enum"), "card_set_enum"),
    "GetStaticExileFaction": lambda self, c, n: _need(F(_static(self, n), "exile_nation"), "exile_nation"),
    # GetFactionEnum(FName)->EFactionEnum：`UEnum::GetValueByName`，查不到(-1)⇒0（IDA）。名字大小写不敏感，可带 `EFactionEnum::` 前缀。
    "GetFactionEnum": lambda self, c, n: _FACTION.get(str(n).split("::")[-1].lower(), 0),
    # SortCardsByLocationNumber / SortCardsByName(cardArray in/out)：按 locationNumber / 名字升序（引擎里是 Array.Sort；
    # 未读 sub 函数体 ⇒ 稳定性/并列顺序按"稳定排序"假设，低频）。
    "SortCardsByLocationNumber": _sort_cards(("location_number",)),
    "SortCardsByName": _sort_cards(("class_fname", "name")),
})
_NO_RECEIVER |= {"GetStaticCard", "GetStaticCardSet", "GetStaticExileFaction", "GetFactionEnum",
                 "SortCardsByLocationNumber", "SortCardsByName"}


def implemented() -> set:
    return set(_TABLE)


# --------------------------------------------------------------------------
# 自检：合成一张卡，把**字段映射**钉住。
# 这里不测"游戏规则对不对"（那要实机），只测"我有没有把字段接错"——
# 接错的典型后果是把 hq 当成不在场上、把 enemy 当成 local，全是静默的错答案。
# --------------------------------------------------------------------------
class _Board:
    def __init__(self, our_turn=True):
        self.our_turn = our_turn


_CARD = {
    "card_type": "infantry", "location": "frontline", "side": "local",
    "attack": 3, "defense": 4, "kredit_cost": 2, "operation_cost": 1,
    "is_suppressed": False, "is_revealed": False, "side_enum": 1,
    "keywords": ["has_blitz", "has_covert"],
    # 复核后原语要的字段（read_raw 默认会给；合成卡要自己补）
    "custom_name1": "", "custom_name2": "", "operation_cost_buff": 0, "received_abilities": [],
    "gameplay_tags": [], "pinned_turns": 0, "custom_json_keys": [], "custom_json_nums": {},
}


def selftest() -> int:
    cn = CardNatives(_Board(our_turn=True))
    c = dict(_CARD)
    cases = [
        ("IsUnit", ("IsUnit", c), True),
        ("IsInfantry", ("IsInfantry", c), True),
        ("IsTank 否", ("IsTank", c), False),
        ("IsGroundUnit", ("IsGroundUnit", c), True),
        ("IsAirUnit 否", ("IsAirUnit", c), False),
        ("IsLocatedOnBoard(frontline)", ("IsLocatedOnBoard", c), True),
        ("IsLocatedInHand 否", ("IsLocatedInHand", c), False),
        ("IsOwnedByClientSide", ("IsOwnedByClientSide", c), True),
        ("GetOppositeSide 返回座位号", ("GetOppositeSide", c), 2),
        ("getTotalAttack", ("getTotalAttack", c), 3),
        ("getHasBlitz", ("getHasBlitz", c), True),
        ("getHasGuard 否", ("getHasGuard", c), False),
        ("未揭示的隐蔽牌", ("IsUnrevealedCovertCard", c), True),
        ("HasCampaignUpgrade(非战役, 空数组)", ("HasCampaignUpgrade", c), False),
    ]
    bad = 0
    for name, (fn, card), want in cases:
        got = cn.call(fn, card, *((1,) if fn == "HasCampaignUpgrade" else ()))
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] %-28s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))

    # ★ hq 和 back 也算"在场上"：Board_HQLeft/Right(5/6) 是整个后排，不是只有总部
    for loc, want in (("hq", True), ("back", True), ("deck", False), ("discard", False)):
        got = cn.call("IsLocatedOnBoard", dict(c, location=loc))
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] IsLocatedOnBoard(%-9s) got=%r want=%r"
              % ("PASS" if ok else "FAIL", loc, got, want))

    # 轮次相关：必须跟着 BoardState 走，不能从卡对象猜
    for ot, side, want in ((True, 1, True), (True, 2, False),
                           (False, 1, False), (False, 2, True)):
        got = CardNatives(_Board(ot), my_seat=1).call("IsSideActive", c, side)
        ok = got == want
        bad += 0 if ok else 1
        print("  [%s] IsSideActive(our_turn=%-5s,%-5s) got=%r want=%r"
              % ("PASS" if ok else "FAIL", ot, side, got, want))

    # 缺字段必须**抛**，不能当 False
    for name, card, why in (
            ("IsUnit", {"card_type": None}, "card_type 缺"),
            ("getHasBlitz", {"keywords": None}, "keywords 缺"),
            ("IsSideActive", c, "没有 BoardState")):
        try:
            (cn if why != "没有 BoardState" else CardNatives(None)).call(
                name, card, *((1,) if name == "IsSideActive" else ()))
            print("  [FAIL] %s 没抛（%s）" % (name, why))
            bad += 1
        except Unimplemented:
            print("  [PASS] %s 缺字段时抛 Unimplemented（%s）" % (name, why))

    print("cardnatives selftest: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
