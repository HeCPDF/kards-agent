# -*- coding: utf-8 -*-
"""kardsmem.gamemodel —— 游戏**原版**的数据结构（枚举、`UBaseCardObject`、`ABP_GameState_Battle_C`）。

用户 2026-10-03：“board 得使用原版一样的数据结构。”

* 枚举：名字与取值逐字照 SDK（`KardsCore_structs.hpp` / `kards_structs.hpp`），不另起名字；
* `BaseCardObject`：字段名与类型照 `UBaseCardObject`（`kards_classes.hpp`），读不出的字段是 None；
* `GameState`：字段名照 `ABP_GameState_Battle_C`，里面的“按位置取牌”等函数照 BP 的同名函数移植
  （出处写在函数注释里）。
* 这里**没有**“本地 / 敌方”这种游戏里不存在的概念：游戏里只有 `ESide.left / right`；
  哪一边是本地玩家由 `mySide` 决定（`GameState.mySide`，每局可能不同）。需要“我方 / 对方”的地方用
  `GameState.is_mine(card)`。

纯数据 + 纯函数，不碰进程内存（读内存在 `kardsmem/board.py`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional


# ------------------------------------------------------------------ 枚举（取值照 SDK）
class ESide(IntEnum):
    """ESideEnum。1=left / 2=right 是服务端视角的编号，**不等于**本地 / 敌方。"""
    NotAvailable = 0
    left = 1
    right = 2


class ECardLocation(IntEnum):
    """ECardLocationEnum。支援线（含 HQ）是 Board_HQLeft / Board_HQRight，前线是 Board_Frontline（双方共用）。"""
    NotAvailable = 0
    Deck_Left = 1
    Deck_Right = 2
    Hand_Left = 3
    Hand_Right = 4
    Board_HQLeft = 5
    Board_HQRight = 6
    Board_Frontline = 7
    Discard = 8
    Deck = 9


class EType(IntEnum):
    """ETypeEnum。"""
    NotAvailable = 0
    location = 1          # HQ 卡的类型就是 location
    order = 2
    tank = 3
    fighter = 4
    bomber = 5
    infantry = 6
    artillery = 7
    antiair = 8
    antitank = 9
    tankdestroyer = 10
    gotcha = 11
    wildcard = 12


class EFaction(IntEnum):
    """EFactionEnum。"""
    NotAvailable = 0
    Germany = 1
    Britain = 2
    Japan = 3
    Soviet = 4
    USA = 5
    France = 6
    Italy = 7
    Poland = 8
    Finland = 9
    Anzac = 10
    Allies = 11
    Neutral = 12


class ERarity(IntEnum):
    """ERarityEnum。"""
    NotAvailable = 0
    Common = 1
    Uncommon = 2
    Rare = 3
    Unique = 4
    Legendary = 5


def enum_or_none(cls, v):
    """原始数值 → 枚举；None / 越界 → None（读不出就是读不出，不猜）。"""
    if v is None:
        return None
    try:
        return cls(int(v))
    except ValueError:
        return None


BOARD_LOCATIONS = (ECardLocation.Board_HQLeft, ECardLocation.Board_HQRight, ECardLocation.Board_Frontline)
HAND_OF = {ESide.left: ECardLocation.Hand_Left, ESide.right: ECardLocation.Hand_Right}
SUPPORT_OF = {ESide.left: ECardLocation.Board_HQLeft, ESide.right: ECardLocation.Board_HQRight}
DECK_OF = {ESide.left: ECardLocation.Deck_Left, ESide.right: ECardLocation.Deck_Right}

# 原版 `FetchCardsByLocation` 的容量（BP_GameState_Battle，见 docs/SIM-FIDELITY.md）：
# 手牌 9；支援线含 HQ 共 5（⇒ 单位 4）；前线 5，`IsFrontlineLimited` 时 2。
HAND_CAP, SUPPORT_CAP, FRONTLINE_CAP, FRONTLINE_CAP_LIMITED = 9, 5, 5, 2


# ------------------------------------------------------------------ UBaseCardObject
@dataclass
class BaseCardObject:
    """`UBaseCardObject` 的运行时实例。字段名 / 类型照 SDK；读不出为 None。

    `ptr` / `Name` / `title` 等是我们加的元数据（不是游戏字段），放在最后一段。
    """
    # ---- 身份与位置 ----
    CardID: Optional[int] = None
    Type: Optional[EType] = None
    Location: Optional[ECardLocation] = None
    side: Optional[ESide] = None
    originalSide: Optional[ESide] = None
    locationNumber: Optional[int] = None
    faction: Optional[EFaction] = None
    rarity: Optional[ERarity] = None
    cardSet: Optional[int] = None                    # ECardSetEnum 原值（取值多，不全列）
    # ---- 数值（加密记录已解出）----
    attack: Optional[int] = None
    attackBuff: Optional[int] = None
    defense: Optional[int] = None
    maxAttack: Optional[int] = None
    maxDefense: Optional[int] = None
    kredits: Optional[int] = None                    # 基础费用（加密记录解出）；总费用见 getTotalKredits()
    kreditsBuff: Optional[int] = None
    KreditsTax_AsEnemyTarget: Optional[int] = None
    operationCost: Optional[int] = None
    operationCostBuff: Optional[int] = None
    heavyArmor: Optional[int] = None
    heavyArmorBuff: Optional[int] = None
    range: Optional[int] = None
    cipher: Optional[int] = None
    # ---- 回合内状态 ----
    movementLeft: Optional[int] = None
    attackLeft: Optional[int] = None
    enterPlayOnTurn: Optional[int] = None
    hasEverAttacked: Optional[bool] = None
    hasAttackedThisTurn: Optional[bool] = None       # 主动：本回合攻击过
    hasBeenAttackedThisTurn: Optional[bool] = None   # 被动：本回合被攻击过（≠ 上一个！）
    attackCountThisTurn: Optional[int] = None
    pinnedTurns: Optional[int] = None
    gotchaActivated: Optional[int] = None
    # ---- 状态旗标 ----
    isBeingGuarded: Optional[bool] = None
    isImmune: Optional[bool] = None
    underEnemyControl: Optional[bool] = None
    isSuppressed: Optional[bool] = None
    isRevealed: Optional[bool] = None
    isGoldCard: Optional[bool] = None
    isSalvaged: Optional[bool] = None
    cardSeen: Optional[bool] = None
    selectTargetOnPlayedFromHand: Optional[bool] = None
    # ---- 关键词旗标（原版就是一组 bool）----
    hasDeployment: Optional[bool] = None
    hasDestruction: Optional[bool] = None
    hasBlitz: Optional[bool] = None
    hasAmbush: Optional[bool] = None
    hasSmokescreen: Optional[bool] = None
    hasMobilize: Optional[bool] = None
    hasAlpine: Optional[bool] = None
    hasFury: Optional[bool] = None
    hasGuard: Optional[bool] = None
    hasShock: Optional[bool] = None
    hasCovert: Optional[bool] = None
    hasScrying: Optional[bool] = None
    hasPincer: Optional[bool] = None
    hasSalvage: Optional[bool] = None
    # ---- 我们的元数据（不是游戏字段）----
    ptr: Optional[int] = None                        # 对象地址
    Name: Optional[str] = None                       # `Name_0`：资产名（FName）
    title: Optional[str] = None                      # 显示名
    CurrentTarget: Optional[int] = None              # 指针
    raw: dict = field(default_factory=dict)

    # ---- 原版 BP 里的同名便捷函数（UBaseCardObject::IsUnit 等）----
    def IsUnit(self) -> Optional[bool]:
        if self.Type is None:
            return None
        return self.Type in (EType.tank, EType.fighter, EType.bomber, EType.infantry, EType.artillery,
                             EType.antiair, EType.antitank, EType.tankdestroyer)

    def IsAirUnit(self) -> Optional[bool]:
        return None if self.Type is None else self.Type in (EType.fighter, EType.bomber)

    def IsGroundUnit(self) -> Optional[bool]:
        return None if self.Type is None else self.Type in (EType.tank, EType.infantry, EType.artillery)

    def IsLocation(self) -> Optional[bool]:
        return None if self.Type is None else self.Type == EType.location

    def IsOrder(self) -> Optional[bool]:
        return None if self.Type is None else self.Type == EType.order

    def IsLocatedOnBoard(self) -> Optional[bool]:
        return None if self.Location is None else self.Location in BOARD_LOCATIONS

    # ---- 位置谓词（原版没有这几个名字；它们只是 Location/Type 的组合，集中写一处）----
    def GetOppositeSide(self) -> Optional[ESide]:
        """`UBaseCardObject::GetOppositeSide`（IDA 0x144AF7700）：side==left→right，==right→left；
        side 不是 1/2（静态卡 NotAvailable）时**不写出参**⇒ 调用方得到 0。这里返回 None 表示该情形。"""
        if self.side not in (ESide.left, ESide.right):
            return None
        return ESide.right if self.side == ESide.left else ESide.left

    def InHand(self) -> bool:
        return self.Location in (ECardLocation.Hand_Left, ECardLocation.Hand_Right)

    def InFrontline(self) -> bool:
        return self.Location == ECardLocation.Board_Frontline

    def InSupportLine(self) -> bool:
        """支援线（含 HQ 卡本身）。"""
        return self.Location in (ECardLocation.Board_HQLeft, ECardLocation.Board_HQRight)

    def IsHQ(self) -> bool:
        return self.InSupportLine() and self.Type == EType.location

    def IsFieldUnit(self) -> bool:
        """真正在场的单位：前线 + 支援线（不含 HQ 卡）。"""
        return self.InFrontline() or (self.InSupportLine() and self.Type != EType.location)

    def InDiscard(self) -> bool:
        return self.Location == ECardLocation.Discard

    def getTotalAttack(self) -> Optional[int]:
        return None if self.attack is None else _clamp_total(self.attack + (self.attackBuff or 0))

    def getTotalKredits(self) -> Optional[int]:
        if self.kredits is None and self.kreditsBuff is None:
            return None
        return _clamp_total((self.kredits or 0) + (self.kreditsBuff or 0))

    def getTotalOperationCost(self) -> Optional[int]:
        return None if self.operationCost is None else _clamp_total(self.operationCost + (self.operationCostBuff or 0))


# 原版：三个"总量" getter 都夹到 `[0,99]` —— `getTotalAttack`（IDA 0x144B14E90）、
#   `getTotalKredits`（0x144B15020）、`getTotalOperationCost`（0x144B15200）；
#   见 `NATIVE-COVERAGE-1.60.md` §14.2 #5/#6/#14 与 `kardsmem/cards.py` 的 `total_*`（那边一直夹）。
# ★ 2026-10-03（P3 R13）：这里以前**不夹** ⇒ 同一条读数在两条读侧路径上不一致：
#   `cardnatives`/`cards.read_raw` 走夹过的值，而 `sim`（经 `engine.adapter` → 本方法）走**没夹**的
#   ⇒ 90 攻 + 50 buff 会被读成 **140**（游戏里是 99），把伤害/威胁算高。
_TOTAL_MAX = 99


def _clamp_total(v: Optional[int]) -> Optional[int]:
    """原版 `clamp(0, 99)`（写法同 `BP_CardFunctions` 的 `((v<0)?0:((v>99)?99:v))`）。"""
    if v is None:
        return None
    return 0 if v < 0 else (_TOTAL_MAX if v > _TOTAL_MAX else v)


def other_side(side: ESide) -> ESide:
    return ESide.right if side == ESide.left else ESide.left

# ------------------------------------------------------------------ ABP_GameState_Battle_C
@dataclass
class GameState:
    """`ABP_GameState_Battle_C` 里我们读得到的部分。字段名照 SDK。

    * `AllCardsInBattle`：`TMap<int32, UBaseCardObject*>`，**稀疏数组下标升序 = 卡牌创建顺序**，
      这里保持这个顺序（dict 插入序）；不要再排序（热浪那类 `GetRandomCard` 依赖它）。
    * `kredits` / `kreditSlots` / `slotsLost` / `fatigue` 的键是 `ESide`（游戏里按 left / right 存）。
    """
    currentTurn: Optional[int] = None
    startingSide: Optional[ESide] = None
    isLocalClientTurn: Optional[bool] = None
    mySide: Optional[ESide] = None                   # 本地玩家是哪一边（由 startingSide/isLocalClientTurn/currentTurn 推）
    matchFinished: Optional[bool] = None
    FrontlineOwner: Optional[ESide] = None
    FrontlineLimiters: Optional[List[int]] = None    # TSet<int32>；非空 ⇒ IsFrontlineLimited()
    CardFunctionTriggers: Optional[Dict[int, List[int]]] = None   # {ERegisteredCardFunction: [CardID…]}
    maxPossibleKredits: Optional[int] = None
    operationKreditsSpent: Optional[int] = None
    weatherPlayedThisTurn: Optional[bool] = None
    kredits: Dict[ESide, Optional[int]] = field(default_factory=dict)
    kreditSlots: Dict[ESide, Optional[int]] = field(default_factory=dict)
    slotsLost: Dict[ESide, Optional[int]] = field(default_factory=dict)
    fatigue: Dict[ESide, Optional[int]] = field(default_factory=dict)
    hqCardID: Dict[ESide, Optional[int]] = field(default_factory=dict)
    AllCardsInBattle: Dict[int, BaseCardObject] = field(default_factory=dict)

    # ---- 本地 / 对方（游戏里没有这个概念，由 mySide 决定）----
    def other_side(self, side: ESide) -> ESide:
        return other_side(side)

    # ---- 原版 `ABP_GameState_Battle_C` 的座位函数（`BP_CardFunctions::GetClientSide/GetOpponentSide/GetPlayingSide`
    #      只是转调 `GameStateRef->…`，卡牌脚本和 Action/通知/钩子调度层都用它们）----
    def GetClientSide(self) -> Optional[ESide]:
        """本地客户端的座位 = `mySide`。"""
        return self.mySide

    def GetOpponentSide(self) -> Optional[ESide]:
        return None if self.mySide is None else other_side(self.mySide)

    def GetPlayingSide(self) -> Optional[ESide]:
        """此刻该行动的一方：`startingSide` 在奇数回合，另一方在偶数回合（`BoardState` 里 `read_my_side` 用的同一条推导）。
        读不出 `startingSide`/`currentTurn` ⇒ None。"""
        if self.startingSide not in (ESide.left, ESide.right) or not self.currentTurn or self.currentTurn < 1:
            return None
        return self.startingSide if self.currentTurn % 2 == 1 else other_side(self.startingSide)

    def IsClientPlaying(self) -> Optional[bool]:
        p = self.GetPlayingSide()
        return None if p is None or self.mySide is None else p == self.mySide

    def is_mine(self, card: BaseCardObject) -> Optional[bool]:
        if self.mySide is None or card.side is None:
            return None
        return card.side == self.mySide

    # ---- 原版函数的移植 ----
    def GetCardFromID(self, card_id: int) -> Optional[BaseCardObject]:
        """BP `GetCardFromID`。"""
        return self.AllCardsInBattle.get(card_id)

    def IsFrontlineLimited(self) -> Optional[bool]:
        """BP_GameState_Battle::IsFrontlineLimited：`FrontlineLimiters` 非空。读不出 ⇒ None。"""
        if self.FrontlineLimiters is None:
            return None
        return len(self.FrontlineLimiters) > 0

    def FetchCardsByLocation(self, location: ECardLocation, side: Optional[ESide] = None) -> dict:
        """BP `FetchCardsByLocation`：→ `{"QtyInLocation", "isLocationFull", "AllCardsInLocation", "FirstCard"}`。

        `location` 是 `Board_Frontline` 时双方共用（`side` 忽略）；其余按 `Location == location` 取。
        满的判据用原版容量（手牌 9 / 支援线 5 含 HQ / 前线 5，受限 2）。
        """
        cards = [c for c in self.AllCardsInBattle.values() if c.Location == location
                 and (location == ECardLocation.Board_Frontline or side is None or c.side == side)]
        cap = {ECardLocation.Hand_Left: HAND_CAP, ECardLocation.Hand_Right: HAND_CAP,
               ECardLocation.Board_HQLeft: SUPPORT_CAP, ECardLocation.Board_HQRight: SUPPORT_CAP,
               ECardLocation.Board_Frontline: (FRONTLINE_CAP_LIMITED if self.IsFrontlineLimited() else FRONTLINE_CAP)
               }.get(location)
        return {"QtyInLocation": len(cards), "isLocationFull": (cap is not None and len(cards) >= cap),
                "AllCardsInLocation": cards, "FirstCard": cards[0] if cards else None}

    def GetCardsInHandBySide(self, side: ESide) -> List[BaseCardObject]:
        return self.FetchCardsByLocation(HAND_OF[side], side)["AllCardsInLocation"]

    def GetAllCardsOnBoard(self) -> List[BaseCardObject]:
        """BP `GetAllCardsOnBoard`：支援线（含 HQ）+ 前线上的所有牌。"""
        return [c for c in self.AllCardsInBattle.values() if c.Location in BOARD_LOCATIONS]

    def GetAllCardsInFrontline(self) -> List[BaseCardObject]:
        return self.FetchCardsByLocation(ECardLocation.Board_Frontline)["AllCardsInLocation"]
