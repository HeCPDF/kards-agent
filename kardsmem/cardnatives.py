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

from .kismetlib import Unimplemented

# ECardLocationEnum 归一化后的位置名（见 board_api）
ON_BOARD = ("frontline", "hq", "back")
UNIT_TYPES = {"tank", "fighter", "bomber", "infantry", "artillery",
              "antiair", "antitank", "tankdestroyer"}
AIR_TYPES = {"fighter", "bomber"}
GROUND_TYPES = UNIT_TYPES - AIR_TYPES


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
                 my_seat=None):
        self.board = board
        # 本地玩家占哪个座位（ESideEnum 1/2）。座位 != 阵营，见 §4.12：
        # 它由回合奇偶推出来，`BoardState.my_side_raw` 就是它。
        self.my_seat = my_seat if my_seat is None else _seat(my_seat)
        self.campaign = campaign
        # 求值器里传来传去的是**卡对象指针**，而这里要的是带字段的视图。
        # `view(ptr) -> dict|Card` 由调用方注入（通常接 `kardsmem.cards.read_raw`）。
        # 不注入时只能吃已经是 dict/Card 的卡。
        self.view = view

    # ---- 分派 ----
    def call(self, name: str, card, *args):
        fn = _TABLE.get(name)
        if fn is None:
            raise Unimplemented("BaseCardObject::%s" % name)
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
    return _need(F(card, "location"), "location") in ON_BOARD


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
APPROX = {"CanBeTargetted"}


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
    "IsLocatedInHand": lambda self, c: _need(F(c, "location"), "location") == "hand",
    "IsLocatedInDeck": lambda self, c: _need(F(c, "location"), "location") == "deck",
    "IsLocatedInDiscard": lambda self, c: _need(F(c, "location"), "location") == "discard",

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
    "getTotalHeavyArmor": lambda self, c: _need(F(c, "total_heavy_armor"), "total_heavy_armor"),
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
        (str(t) in _need(F(c, "gameplay_tags"), "gameplay_tags"))
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
    "getTotalAttack": lambda self, c: _need(F(c, "total_attack", "attack"), "attack"),
    "getTotalDefense": lambda self, c: _need(F(c, "total_defense", "defense"), "defense"),
    "getTotalKreditCost": lambda self, c: _need(
        F(c, "total_kredit_cost", "kredit_cost", "kredit"), "kredit_cost"),
    "getTotalOperationCost": lambda self, c: _need(F(c, "operation_cost"), "operation_cost"),
    "getAndDecryptAttack": lambda self, c: _need(F(c, "attack"), "attack"),  # 明文侧
    "getAndDecryptDefense": lambda self, c: _need(F(c, "defense"), "defense"),
    "getAndDecryptKredit": lambda self, c: _need(F(c, "kredit", "kredit_cost"), "kredit"),

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
      （`agent/legality.py` 的 `make_rich_view` 就是干这个的）。
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
        ("HasCampaignUpgrade(非战役)", ("HasCampaignUpgrade", c), False),
    ]
    bad = 0
    for name, (fn, card), want in cases:
        got = cn.call(fn, card)
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
