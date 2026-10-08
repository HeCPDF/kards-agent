#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.scripts_ctx —— 直跑上下文 `DirectCtx` + 出参写入 `write_out`（P6 自 `engine/scripts.py` 原样拆出）。

各 sink 族模块（`scripts_sinks_core` / `scripts_sinks_more`）与 `engine.scripts` 共用；`engine.scripts` 再导出。
"""
from __future__ import annotations

from typing import Optional

__all__ = ["DirectCtx", "write_out"]


def write_out(frame, e, idx: int, value) -> None:
    """把值写进调用点第 `idx` 个 kid 指向的局部变量（出参）。

    与 `engine.effectvm.Recorder._write_out`（`effectvm.py:319`）**同一约定** —— 直跑与录制
    必须给出参写同样的东西，否则 BP 的控制流会在两条路上分叉（P4 收尾时合并成一处实现）。
    """
    try:
        name = e.kids[idx].args.get("prop")
        if name and frame is not None:
            frame.locals[name] = value
    except Exception:                                        # noqa: BLE001
        pass


class DirectCtx:
    """直跑上下文：**状态 + 指针→card_id + 本地座位 + 缺口**（没有 eff 字典）。

    * `state`：`engine.state.Sim`（或任何鸭子类型：`kredits`/`slots`/`kredit_max`/`units`…）
    * `ptr_ids`：`{card_ptr: card_id}`（快照/录制器已经填过的那份，直接复用）
    * `my_side`：本局座位（1/2，`ESide`）；`side` 入参与它比较决定"我方/对方"
    * `gaps`：状态变更做不了的（例如"对方槽的绝对值我们没建模"）**如实记这里**，不猜
    * `applied`：本刀顺带记下"真跑过哪些原生"，给对账/诊断用（不是效果字典）
    """

    def __init__(self, state, ptr_ids: Optional[dict] = None, my_side=None,
                 gaps: Optional[list] = None, gates: Optional[dict] = None,
                 on_death=None, on_draw=None, on_event=None, hq_card_ids: Optional[dict] = None,
                 spawn_stat=None, payload: Optional[dict] = None, rng=None,
                 hq_ptr_sides: Optional[dict] = None):
        self.state = state
        self.ptr_ids = ptr_ids if ptr_ids is not None else {}
        self.my_side = my_side
        self.gaps = gaps if gaps is not None else []
        self.gates = gates if gates is not None else {}
        #: 死亡链**扇出**回调 `(state, uid)`（**调用方注入** —— 0x27 触发 + `death_fx` 预计算效果是 L1 钩子层的事）：
        #: 单位**已离场**、`applied` 已记 `destroy`/`died` 之后由 `ctx.destroy`/`ctx.died` 调用（不改写记录 ⇒ 重放仍能复现）。
        #: 缺省 None = 只移场 + 记事件（第四/五刀定的边界；调用方事后自己扇）。
        self.on_death = on_death
        #: 抽牌链回调 `(state, n)`（**调用方注入** —— 通常是 `engine.chain.draw_chain`：
        #: 它要"已知牌库信息 + 评估权重"，那些是调用方的事，engine 的 sink 只负责路由）。
        self.on_draw = on_draw
        #: 事件扇出回调 `(kind, uid)`（**调用方注入**）：给了就**当场**扇（保住"离场前"那类顺序），
        #: 没给就只留 `ctx.events` 表（调用方事后再扇 —— 那时"还在场"的事件就晚了，见 `fire`）。
        self.on_event = on_event
        #: 生成单位时要的**静态面板**查询 `name -> {atk,dfn,cost,typ,kw}`（**调用方注入**，
        #: 与 `sim.spawn_stats` 同源）—— 读静态卡表是调用方的事（engine 不碰卡库）。
        #: 查不到 ⇒ sink 记缺口、**不生成**（绝不编一个面板出来）。
        self.spawn_stat = spawn_stat
        #: **按调用**的载荷（录制期算出来的东西，直跑由调用方喂）：例 `{"convert": {...}}`
        #: —— `ConvertCard` 的目标卡数值/是否 skipTrigger 由 `effectvm._convert_payload` 在录制期用
        #: 存活视图算（与 `MakeVeteran` 的 `vet` 同一套分层：视图=调用方、规则=engine）。
        self.payload = payload if payload is not None else {}
        #: 牌局**随机流**（**调用方注入**，通常是 `kardsmem.rng.Stream`：`wrapper_int(lo,hi)`/`shuffle(list)`）。
        #: 与 `sim.rng` 同一条流 ✓。**没注入 ⇒ 涉随机的动词记缺口、不猜**（也不假装洗过牌 ✓）。
        self.rng = rng
        #: HQ 的 card_id → 座位（**调用方注入**）：`DamageMultipleCards` 的数组里可能混着**敌方总部**的
        #: 卡 id —— 旧路在 `effectvm:728` 把它拆成 `damage_hq`/`damage_own_hq`，直跑得同样认得出来。
        self.hq_card_ids = hq_card_ids if hq_card_ids is not None else {}
        #: HQ 的**卡指针** → 座位（**调用方注入**）：`DamageCard(card, …)` 的第一个实参是**指针**，而总部牌不一定被 VM 读过
        #: ⇒ 不在 `ptr_ids` 里（2026-10-06 实机影子：AIR BLITZ/KM BISMARCK 的『目标指针换不出 card_id』⇒ 直跑没伤到总部）。
        #: 旧路（`effectvm` 的 `damage` 分支）也是按 `rec.hq_enemy`/`hq_own` **指针集**认总部的 ⇒ 直跑同口径。
        self.hq_ptr_sides = hq_ptr_sides if hq_ptr_sides is not None else {}
        #: 脚本里刚生成的牌的登记表（`engine/spawned.py`）：spawn 类 sink 登记、`FetchCardFromCardID` 软钩子查；
        #: `effectvm._direct_hooks` 会把它换成录制器的同一份。`spawn_gate(info)` 给新牌句柄喂门数据（同样由调用方注入）。
        from engine.spawned import SpawnedCards                        # noqa: PLC0415
        self.spawned = SpawnedCards()
        self.spawn_gate = None
        #: 随机点的**选取**回调 `(动词, 域大小) -> 下标`（调用方注入；缺省 None）。给了 ⇒ 枚举模式（`forced`）也能走进 sink
        #: （录制器的 `_pick` 记 `nodes`/`chance`，影子对账逐分支比较靠它）；活种子（`ctx.rng`）优先、不枚举。
        self.pick = None
        self.applied: list = []
        # ★ P4 第二刀（2026-10-04）的关键设计：旧的 `eff` 字典把**状态变更**和**事件扇出**
        #   （`pin` → 0x3D、`suppress` → 0x3A…）混在同一个键里；直跑没有字典 ⇒ 事件必须**显式
        #   记下来**，由调用方按既有的 `event_fx`（rule 侧预计算）扇出，**不能丢**。
        #   形状 `("pin"|"suppress"|…, uid)`；这是**事件表**，不是效果字典（P4 的目标是删字典）。
        self.events: list = []

    def fire(self, kind: str, uid) -> None:
        """记一条"要扇出的事件"（调用方负责按 `event_fx` 应用）。

        ★ 若调用方给了 `on_event`，**当场**扇出（并仍记进 `ctx.events`）—— 这一条是**顺序**用的：
        有的原版事件必须在"单位还在场"时扇（撤退 0x36：`_apply_unit_eff` 那时才找得到目标），
        只留事件表让调用方事后再扇就晚了（那时已经离场）。
        """
        self.events.append((kind, uid))
        if self.on_event is not None:
            self.on_event(kind, uid)

    def destroy(self, uid, why: str = "") -> None:
        """**摧毁**：把单位移出场面 + 记一条 `("destroy", uid)` 事件。

        死亡的触发链（`OnDestroyed` 0x27 + `death_fx` 里的预计算效果）**不在这里算** —— 那是 L1 钩子层
        的事：调用方注入 `on_death` ⇒ 离场 + 记事件之后当场扇（原版：ApplyRemoveCardFromBoard（BP_CardFunctions.cpp:18002）
        BeforeLeave:18028 → 离场 → AfterLeave:18053 → ExecuteOnCardDestroyedFunction:18060）；没注入 ⇒ 由调用方事后按 `event_fx`/`death_fx` 扇出（直跑只做"状态变更 + 事件表"，P4 的边界）。
        """
        u = getattr(self.state, "units", {}).pop(uid, None)
        if u is None:
            self.gaps.append("destroy：%s 不在场上（未结算）" % uid)
            return
        self.applied.append(("destroy", uid, why))
        self.fire("destroy", uid)
        if self.on_death is not None:                   # 单位已离场 ⇒ 死亡链（字典路 `_apply_eff` 的 pop → `_apply_death` 同序）
            self.on_death(self.state, uid)

    def died(self, uid, why: str = "") -> None:
        """**已经离场**之后的死亡登记（事件 only，**不再 pop**）。

        为什么要跟 `destroy` 分开：`engine.natives.damage.deal_damage` 在 `dfn <= 0` 时**自己 pop**
        然后回调 `on_death(state, uid)` —— 那条路上单位已经不在了，再 pop 会记一条**假缺口**
        （实测：`destroy：11 不在场上`）。这里只记事件（`("destroy", uid)`），与显式
        `destroy`（还在场、需要 pop）分开用。
        """
        self.applied.append(("died", uid, why))
        self.fire("destroy", uid)
        if self.on_death is not None:
            self.on_death(self.state, uid)

    def retreat(self, uid, why: str = "") -> None:
        """**撤退/回手**：**先**扇 `("retreat", uid)` 事件（那时还在场）**再**移场。

        顺序照原版/`sim` 的既有口径（`_apply_eff` 的注释）：0x36 是"别人看到它撤退"，
        扇它的时候单位**还在场**（`_apply_unit_eff` 那时才找得到目标），然后才离场。
        也**不发**"被摧毁"事件（撤退 ≠ 摧毁：被摧毁才触发死亡效果）。
        """
        u = getattr(self.state, "units", {}).get(uid)
        if u is None:
            self.gaps.append("retreat：%s 不在场上（未结算）" % uid)
            return
        self.applied.append(("retreat", uid, why))
        self.fire("retreat", uid)                       # ★ 先扇（还在场）
        self.state.units.pop(uid, None)                 # 再离场

    def leave_board(self, uid, why: str = "") -> None:
        """**离场但不算被摧毁**（`RemoveMultipleCardsFromBoard` 那条）：只 pop、**不发**任何事件。

        与 `destroy` 分开：`sim` 的既有口径写着"离场但不算被摧毁 ⇒ 不触发 `death_fx`" ⇒
        多发一条 `("destroy", uid)` 会让调用方跑一整套死亡效果（行为就变了）。
        """
        u = getattr(self.state, "units", {}).pop(uid, None)
        if u is None:
            self.gaps.append("leave_board：%s 不在场上（未结算）" % uid)
            return
        self.applied.append(("leave_board", uid, why))

    def hq_side_of(self, value):
        """`value`（卡指针**或**卡 id）若是某一方的**总部**牌 ⇒ 它的座位，否则 None。

        先按指针（`hq_ptr_sides`），再按 card_id（`hq_card_ids`，以及 `ptr_ids` 里换得出 id 的总部指针）。
        """
        try:
            side = self.hq_ptr_sides.get(value)
        except TypeError:
            side = None
        if side is not None:
            return side
        cid = self.cid_of(value)
        if cid is None:
            cid = value
        try:
            side = self.hq_card_ids.get(cid)
            if side is not None:
                return side
            for p, sd in self.hq_ptr_sides.items():
                if self.ptr_ids.get(p) == cid:
                    return sd
        except TypeError:
            pass
        return None

    def unit_of(self, ptr):
        """卡指针 → 状态里的单位（经 `ptr_ids` 的 card_id）。认不出/不在场上 ⇒ None。"""
        cid = self.cid_of(ptr)
        if cid is None:
            return None
        return getattr(self.state, "units", {}).get(cid)

    def ptr_of_id(self, card_id):
        """卡 id → 卡指针（`ptr_ids` 的反查；换不出 ⇒ None）。门数据（`gates`）按指针存，收 ID 的动词要它。"""
        for p, i in self.ptr_ids.items():
            if i == card_id:
                return p
        return None

    def unit_by_id(self, card_id):
        """**卡 ID** → 状态里的单位（有的动词签名收的是 ID 而不是指针，例 `CustomAbilityAdd` 的 `cardID`、
        `DamageCard` 的 `damagerCardID`）—— 这两类别再混用 `unit_of`（会恒查不到，静默失效）。"""
        if card_id is None:
            return None
        return (getattr(self.state, "units", {}) or {}).get(card_id)

    def unit_arg(self, value):
        """**一个目标实参** → 状态里的单位：**先当卡 ID 查、再当卡指针查**。

        ★ 为什么两向都试（2026-10-04，P4 第十八刀，**两次踩坑的教训**）：BP 各动词的**第一个实参语义不统一**，
        必须逐个看签名 —— 例：
          * **收 ID**：`PinUnit(int cardID,…)` / `SuppressUnit(int cardID,…)` /
            `SuppressMultipleUnits(const TArray<int>*& cardsToSuppress,…)` / `RemoveCardFromBoard(int cardID,…)` /
            `ResetUnitOperations(int cardID,…)` / `ChangedPinnedTurns(int cardID,…)` / `RevealCard(int cardID,…)` /
            `DestroyMultipleCards(const TArray<int>*& cardsToDestroy,…)` / `DamageMultipleCards(TArray<int>& receiverIDs,…)`；
          * **收指针**：`DestroyCard(UBaseCardObject* card,…)` / `DamageCard(UBaseCardObject* card,…)` /
            `ChangeAttack|ChangeDefense|ChangeHeavyArmor|ChangeKreditCost(UBaseCardObject* card,…)` /
            `AddKreditsTax` / `FullyHealCard` / `RemovePin` / `MakeCardsFight` / `MoveUnitFromBoardToOwnersHand`；
          * **数组是指针**：`MakeCardRetreat(const TArray<UBaseCardObject*>*& cards,…)`（它那份映射是对的 ✓）。
        我两次把"ID 当指针"（或反过来）写进 sink，而**测试夹具也跟着喂错** ⇒ 全绿却与实机不符 ✗。
        两向解析既不猜语义、也不丢目标：ID 先试（我们状态里 id 是小整数、指针是地址，实际不会撞 ✓）。
        """
        u = self.unit_by_id(value)
        if u is not None:
            return u
        return self.unit_of(value)

    def seat_of(self, side):
        """原版的 `ESideEnum`（1/2）→ 本状态的座位口径（`state.me`/`state.opp`）。

        1 = local/我方（`my_side`），2 = 对方；读不出（None/未知）⇒ `None`（调用方记缺口）。
        """
        if side == 1:
            return self.my_side
        if side == 2:
            me = self.my_side
            return getattr(self.state, "opp", None) if me is not None else None
        return None

    def hand_card_of(self, ptr):
        """卡指针 → **手牌/牌库**里的那张牌（`Sim.hand` 以 card_id 为键 ✓；`deck_cards` 是同一批模板）。

        `ChangeKreditCost` 的目标就是手牌那张牌（`effectvm:679`）⇒ 它不在 `state.units` 里，
        不能走 `unit_of`。
        """
        cid = self.cid_of(ptr)
        if cid is None:
            return None
        return ((getattr(self.state, "hand", {}) or {}).get(cid)
                or (getattr(self.state, "deck_cards", {}) or {}).get(cid))

    # -- 门（原版两道门要的卡属性，状态里没有 ⇒ 由调用方喂） ------------------
    def dict_of(self, name: str) -> dict:
        """拿状态上那个**可变字典**字段（不存在就建）—— **写**的时候必须用它。

        别用"`or {}` 兜底再写"那种写法：**空字典是 falsy** ⇒ 表达式会造一个新字典、写进去就丢了 ✗。
        这个坑在本文件里犯过**三次**（`restrictions` 的 append、`deck_cards` 的 setdefault、`deck_cards` 的
        setitem）⇒ 现在收成一个方法，并有静态守卫（在 `tests/test_scripts_direct.py` 里）禁止那种写法。
        **读**的时候用 `or {}` 没问题（只读、不写回）✓。
        """
        cur = getattr(self.state, name, None)
        if not isinstance(cur, dict):
            cur = {}
            setattr(self.state, name, cur)
        return cur

    def list_of(self, name: str) -> list:
        """同 `dict_of`，但给列表字段用（`restrictions`/`deck` 那类）。"""
        cur = getattr(self.state, name, None)
        if not isinstance(cur, list):
            cur = []
            setattr(self.state, name, cur)
        return cur

    def gate_of(self, ptr):
        """取这张卡的**门数据**（`{"buffable": bool, "unrevealed_covert": bool}`）。

        为什么状态里没有：`buffable`/`unrevealed_covert` 是**卡**属性（关键词 + 揭示位 + 位置），
        而 `Sim` 只有场面状态 ⇒ 由调用方（rule/policy，从快照同一份数据里）喂进来。
        **取不到就返回 None** —— sink 会记缺口、不改状态（不猜；与"判据不足就记缺口"一致 ✓）。
        ★ 键可以是**卡指针**也可以是**卡 ID**（有的动词签名收 ID：`CustomAbilityAdd(cardID)` ✓）。
        """
        return (self.gates or {}).get(ptr)

    # -- 侧别 ---------------------------------------------------------------
    def is_mine(self, side) -> bool:
        """`ESideEnum` 入参是不是"我方"。读不到座位 ⇒ **当对方处理**（不猜成我方）。"""
        if self.my_side is None or side is None:
            return False
        try:
            return int(side) == int(self.my_side)
        except Exception:                                    # noqa: BLE001
            return side == self.my_side

    def cid_of(self, ptr):
        """卡指针 → card_id（认不出 ⇒ None；调用方据此记缺口）。"""
        try:
            return self.ptr_ids.get(ptr)
        except Exception:                                    # noqa: BLE001
            return None
