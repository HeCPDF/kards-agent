# -*- coding: utf-8 -*-
"""ops/choices.py —— 选择与抉择：二选一 / 三选一 / 牌库选牌 / 手牌目标，候选读取与点选。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import struct
import time
from typing import Optional

from kardsmem.pick import hand_card_actors, player_controller
from ops.consts import (
    NOTE_OFF_ACTOR_CARDID, NOTE_OFF_ACTOR_SELF_BASECARD, NOTE_OFF_BOARD_SELECT_HAND_WIDGET,
    NOTE_OFF_CARDOBJ_CHOOSE_ONE_CARDS, NOTE_OFF_HTGT_WIDGET_CARD_ID, NOTE_OFF_HTGT_WIDGET_CARD_OBJ,
    NOTE_OFF_HTGT_WIDGET_TARGET, NOTE_OFF_LOGIC_ONLINE_MATCH, NOTE_OFF_OM_SELECT_CARD_TO_DRAW,
    NOTE_OFF_ONE_CARD, NOTE_OFF_ONE_CLICKABLE, NOTE_OFF_ONE_INDEX, NOTE_OFF_ONE_MOTHER,
    NOTE_OFF_ONE_NEEDS_TARGET, NOTE_OFF_ONE_SPAWN_CARD, NOTE_OFF_SPAWN_INDEX, NOTE_OFF_SPAWN_IS_EFFECT,
    NOTE_OFF_SPAWN_NAME, NOTE_OFF_SPAWN_OWNED, NOTE_OFF_SPAWN_SELECTED, NOTE_OFF_SPAWN_TRIGGER,
    NOTE_OFF_SPAWN_TRIGGER_ID, PICK_STRUCT_SIZE, SETTLE_AFTER_DISPATCH, SETTLE_AFTER_DOWN, SETTLE_HOVER,
)


def ghost_reason(ws: dict) -> str:
    """`hand_target_widget_state` → 幽灵原因（空串 = 看不出幽灵）。只认**硬证据**：面板已在拆/已销毁。

    BP 生命周期（1.60 导出）：
      * 旗标 `isSelectingHandTarget`/`chooseOneActive` 有**两处**置 true：逻辑侧
        `BP_OnlineMatch::AddSubActionSelectHandTargetPending`（:15596，打出源卡时**无条件**置位，不看有无候选）
        与 widget `Construct`（`ConfirmHandTargetButton_Widget.cpp:199-207`，同时挂 `selectHandTargetWidget`）；
      * 只有 widget `Destruct`（:247-257）无条件复位（旗标 false + 指针 nullptr）；另一条复位在
        `BP_Logic::autoPickHandTargets`（:8640，仅回合结束且 pending 列表非空）。
        ⇒ widget 没走到 Destruct（或根本没建）旗标就一直挂着；
      * 无合法目标时 `Construct`（:216-226）自己走"入队 handTargetSelected(无目标)+doNext()"，`doNext()` 收尾
        置 `pendingKill=true`、`Delay(0.01)`、`RemoveFromParent()`。
    """
    if not isinstance(ws, dict):
        return ""
    if ws.get("finish_destroyed") or ws.get("begin_destroyed"):
        return "widget 已销毁（ObjectFlags %s）" % ws.get("object_flags")
    if ws.get("pending_kill"):
        return "widget.pendingKill=True（面板已在拆/卡在拆除中）"
    return ""


class ChoiceMixin:
    """选择与抉择：二选一 / 三选一 / 牌库选牌 / 手牌目标，候选读取与点选。"""

    def hand_target_widget(self) -> int:
        """`BP_Board_C::selectHandTargetWidget` —— 等"选一张手牌当目标"时那个确认面板。"""
        b = self.board_actor()
        if not b:
            return 0
        off = self.off(self.uclass_of_instance(b), "selectHandTargetWidget",
                       NOTE_OFF_BOARD_SELECT_HAND_WIDGET)
        return self.m.ptr(b + off) or 0

    # UObject::ObjectFlags（obj+0x08）里的销毁标志
    RF_BEGIN_DESTROYED = 0x00008000
    RF_FINISH_DESTROYED = 0x00010000

    def hand_target_widget_state(self, w: int, wcls: int) -> dict:
        """**只读**：手牌选目标确认面板"还活着吗"的全部可读证据（诊断 + 幽灵判别用）。

        字段（BP：`UI/ConfirmHandTargetButton_Widget.cpp`）：
          * `pending_kill`：BP 布尔，`doNext()` 队列空时在 `Delay(0.01)`+`RemoveFromParent()` **之前**置 true（:3928 段）
            ⇒ True = 面板已在拆；
          * `queue_num`：`cardBeingPlayedQue` 里还排着几张源卡；`has_selected`：玩家是否已点中一张；
          * `object_flags/begin_destroyed/finish_destroyed`：UObject 销毁标志；
          * `in_viewport`：`UUserWidget::IsInViewport()`（**仅诊断**，未在实机验证前不据此判幽灵）；
          * `board_choose_one_active`：同一对旗标里的另一个。
        任何一项读不出就是 None，不抛。
        """
        st: dict = {}
        try:
            raw = self.m.read_exact(w + 0x08, 4)
            if raw:
                fl = struct.unpack("<I", raw)[0]
                st["object_flags"] = hex(fl)
                st["begin_destroyed"] = bool(fl & self.RF_BEGIN_DESTROYED)
                st["finish_destroyed"] = bool(fl & self.RF_FINISH_DESTROYED)
        except Exception as e:                                    # noqa: BLE001
            st["flags_error"] = str(e)
        def _u8(name):
            try:
                off = self.off(wcls, name, -1)
                if off is None or off < 0:
                    return None
                raw = self.m.read_exact(w + off, 1)
                return bool(raw[0]) if raw else None
            except Exception:                                     # noqa: BLE001
                return None
        st["pending_kill"] = _u8("pendingKill")
        st["has_selected"] = _u8("hasSelected")
        st["turn_has_ended"] = _u8("turnHasEnded")
        try:
            off = self.off(wcls, "cardBeingPlayedQue", -1)
            st["queue_num"] = (struct.unpack("<i", self.m.read_exact(w + off + 8, 4))[0]
                               if off is not None and off >= 0 else None)
        except Exception:                                         # noqa: BLE001
            st["queue_num"] = None
        try:
            f = self.find_fn(wcls, "IsInViewport")
            v = self.call_out_u8(w, f) if f else None
            st["in_viewport"] = None if v is None else bool(v)
        except Exception:                                         # noqa: BLE001
            st["in_viewport"] = None
        try:
            b = self.board_actor()
            off = self.off(self.uclass_of_instance(b), "chooseOneActive", -1) if b else -1
            raw = self.m.read_exact(b + off, 1) if b and off >= 0 else None
            st["board_choose_one_active"] = bool(raw[0]) if raw else None
        except Exception:                                         # noqa: BLE001
            st["board_choose_one_active"] = None
        return st

    def hand_target_pending(self, verbose: bool = True, with_legal: bool = True) -> dict:
        """**只读**：现在是不是在等"点一张手牌当目标"（`selectTargetOnPlayedFromHand` 那一类效果）。

        机制（1.58 导出逐句读过）：
          * 源卡打完时调 `BP_CardFunctions::selectTargetFromHand(cardID)`（导出 :5305）
            ⇒ `NotifySelectHandTargetPending` ⇒ 服务端动作流里是
            `ZActionSelectHandTargetPending{cardBeingPlayed}`（**实机见过**：
            THE AMERICAN GUARD "Deployment: Choose a card in hand. Convert the top card
            of your deck into it."）；
          * 客户端 `ConfirmHandTargetButton_Widget::Construct` 里把
            `Board->isSelectingHandTarget = true` / `chooseOneActive = true` /
            `Board->selectHandTargetWidget = this`；
          * 玩家**点手牌** ⇒ `BP_HandCard::OnActorClicked`（entry 30474，
            `BranchOnPlatformType` 桌面分支）→ `selectHandTarget()`（导出 :5262）
            ⇒ `widget->setTarget(this)` + 给对手发 `toggle_select_hand_target;<cardID>`；
          * 玩家**点确认** ⇒ 本 widget 的
            `BndEvt__StatButton_K2Node_ComponentBoundEvent_0_onClicked__DelegateSignature`
            （entry 2468）⇒ `_playerMoves->AddMoveToPlayerMoveQueue(..., "handTargetSelected",
            targetCardID, 0, cardBeingPlayed)` → 源卡的 `OnHandTargetSelected(...)`。
        """
        b = self.board_actor()
        w = self.hand_target_widget()
        res = {"board_actor": hex(b) if b else None,
               "is_selecting_hand_target": self.selecting_hand_target(),
               "widget": hex(w) if w else None}
        if not w:
            res["pending"] = False
            if verbose:
                print("    没有 selectHandTargetWidget ⇒ 不在等选手牌")
            return res
        wcls = self.uclass_of_instance(w)
        off_cbp = self.off(wcls, "cardBeingPlayed", NOTE_OFF_HTGT_WIDGET_CARD_ID)
        off_tgt = self.off(wcls, "targetCardID", NOTE_OFF_HTGT_WIDGET_TARGET)
        off_obj = self.off(wcls, "_cardBeingPlayedObject", NOTE_OFF_HTGT_WIDGET_CARD_OBJ)
        raw = self.m.read_exact(w + off_cbp, 4)
        res["card_being_played"] = struct.unpack("<i", raw)[0] if raw else None
        raw = self.m.read_exact(w + off_tgt, 4)
        res["target_card_id"] = struct.unpack("<i", raw)[0] if raw else None
        res["source_card_obj"] = hex(self.m.ptr(w + off_obj) or 0)
        res["pending_raw"] = bool(res["is_selecting_hand_target"])
        res["pending"] = res["pending_raw"]
        if res["pending_raw"]:
            ws = self.hand_target_widget_state(w, wcls)
            res["widget_state"] = ws
            reason = ghost_reason(ws)
            if reason:
                # ★ 2026-10-07 幽灵提示：旗标还挂着但 widget 已在拆（pendingKill）/已销毁 ⇒ 不是活提示。
                res["pending"] = False
                res["ghost"] = reason
        if res["pending"] and with_legal:
            # 每张手牌能不能选 —— 用游戏自己的 `IsValidHandTarget`（"确认"按钮灰不灰同源）
            cand = []
            for r in hand_card_actors(self.ks):
                lg = self.hand_target_legal(r.get("card_id"))
                cand.append({"card_id": r.get("card_id"), "name": r.get("name"),
                             "valid": lg.get("is_valid"), "reason": lg.get("reason")})
            res["candidates"] = cand
            # BP 证据（ConfirmHandTargetButton_Widget::Construct，导出 :216/:226）：`setLegalTargets()` 之后
            # `legalTargets` 为空 ⇒ 游戏自己不开提示，直接入队 handTargetSelected(无目标) 并 doNext() 拆 widget。
            # 我们的逐张 IsValidHandTarget 与 setLegalTargets 同源，所以"候选非空且全不合法"= 提示已被游戏自己跳过。
            if cand and not any(c.get("valid") for c in cand):
                res["pending"] = False
                res["ghost"] = "候选全不合法（legalTargets 空 ⇒ 游戏自跳）"
        if verbose:
            print("    等选手牌：pending=%s 源卡=%s 已点中=%s widget=0x%X"
                  % (res["pending"], res["card_being_played"], res["target_card_id"], w))
        return res

    def select_hand_target(self, hand_card_id: int, verbose: bool = True,
                           confirm: bool = True) -> dict:
        """**点一张手牌当目标**，然后点"确认"按钮 —— 复刻真人的两下点击。

        为什么这么写（用户 2026-09-25 的纪律：悬停不能省、点击要还原真实序列）：
          1) 悬停手牌 actor（`OnActorMouseEnter`）+ 悬停转发
             （`PC->MouseHoverDispatch`，**对手看得到**的那一步）；
          2) `手牌->OnActorClicked(1)` —— 真实左键点击在桌面分支就走这一支
             （entry 30474 → `selectHandTarget()`）；该函数会给对手发
             `toggle_select_hand_target;<cardID>`，所以**不能**跳过它直接写
             `widget->targetCardID`（那样信息流跟真人不一样）；
          3) 读回 `widget->targetCardID` 核对这一下真选中了没（不一致就返回、不点确认）；
          4) 点确认：`BndEvt__StatButton_..._onClicked__DelegateSignature`（entry 2468）
             ⇒ 入队 `handTargetSelected` → 源卡 `OnHandTargetSelected(...)`。
        """
        w = self.hand_target_widget()
        if not w:
            return {"ok": False, "stopped": "不在等选手牌状态"}
        rec = self.hand_actor(hand_card_id)
        if not rec:
            return {"ok": False, "stopped": "手牌 %s 找不到 actor" % hand_card_id}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        wcls = self.uclass_of_instance(w)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        f_click = self.fn(cls, "OnActorClicked")
        f_conf = self.find_fn(
            wcls, "BndEvt__StatButton_K2Node_ComponentBoundEvent_0_onClicked__DelegateSignature")
        res = {"hand_card": hand_card_id, "actor": hex(actor), "widget": hex(w),
               "send": {"enter": bool(f_enter), "hover_dispatch": bool(f_disp),
                        "click": bool(f_click), "confirm": bool(f_conf)}}
        if f_enter:
            self.call0(actor, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, actor)          # 悬停转发（对手可见）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        if not f_click:
            return dict(res, ok=False, stopped="手牌 actor 上没有 OnActorClicked")
        # ★ 2026-09-26 重构：走统一点击原语（L0 队列闸门 + L1 PC 状态 + `GlobalMouseUp` 那一跳）。
        #   旧写法直接 `call_i32(OnActorClicked, 1)`，等于跳过了 PC 的松手流程。
        res["click"] = self.click_actor(actor, is_precise=1, verbose=True)
        self.settle(SETTLE_AFTER_DOWN, 2)
        # 核对：widget 是不是认下了这一张
        off_tgt = self.off(wcls, "targetCardID", NOTE_OFF_HTGT_WIDGET_TARGET)
        raw = self.m.read_exact(w + off_tgt, 4)
        got = struct.unpack("<i", raw)[0] if raw else None
        res["widget_target_card_id"] = got
        if got != hand_card_id:
            return dict(res, ok=False, reason="click_not_registered",
                        stopped="点了手牌但 widget->targetCardID=%s（期望 %s）"
                                % (got, hand_card_id))
        if not confirm:
            return dict(res, ok=True, confirmed=False)
        if not f_conf:
            return dict(res, ok=True, confirmed=False,
                        stopped="找不到确认按钮的回调（已选中，未确认）")
        mk = self._mark()
        # ★ 2026-09-26（实机验证）**真按钮在子控件上**：这个 widget 自己只有
        #   `BndEvt__StatButton_..._onClicked` 一个句柄（直接调它 = 假点击），
        #   真按钮是它身上的 `confirmButton // 0x0368`
        #   （`verticalKardsButtonWithText_Widget_C`，和"投降"按钮同一个类），
        #   四件套在**子按钮**上（该类没有自己的 clicked —— click 走父回调）。
        #   实机：子按钮 hover/press/release + 父 onClicked → 动作流 `XActionHandTargetSelected`。
        off_btn = self.off(wcls, "confirmButton", 0x0368)
        cbtn = self.m.ptr(w + off_btn) or 0
        res["confirm_button"] = hex(cbtn)
        if cbtn:
            bcls = self.uclass_of_instance(cbtn)
            res["confirm_button_class"] = self.pool.fname_of(bcls)
            # 四步之间的停顿改按帧等（和换牌确认同一修法）：失焦降帧时裸 sleep 里游戏一帧没跑，
            # 按下/松开态不会渲染（2026-10-01 根因）。
            for suffix, pause, frames in (("OnButtonHoverEvent", 0.25, 6), ("OnButtonPressedEvent", 0.08, 4),
                                          ("OnButtonReleasedEvent", 0.05, 3), ("OnButtonClickedEvent", 0.20, 6)):
                f = None
                for n in (0, 1, 2, 5, 7):
                    f = self.find_fn(
                        bcls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_%d_%s__DelegateSignature"
                              % (n, suffix))
                    if f:
                        break
                if f:
                    self.call0(cbtn, f)
                    self.settle(pause, frames)
                    res.setdefault("confirm_button_calls", []).append(suffix)
        self.call0(w, f_conf)
        res["confirm_called"] = True
        # 动作流：这一步产生的是源卡的部署结算（THE AMERICAN GUARD 是"把牌库顶变成它"），
        # 具体 action_type 随卡不同，所以这里取"这一步之后新出现的全部动作"。
        from kardsmem.matchlog import MatchLog
        ml = self.matchlog()          # ★ 长寿命单例（locate 4~6 s，别再每次新建）
        t0 = time.time()
        rows = []
        while time.time() - t0 < 3.0:
            rows = ml.since(mk)
            if rows:
                break
            time.sleep(0.05)
        res["actions"] = rows
        res["ok"] = bool(rows)
        res["waited"] = round(time.time() - t0, 2)
        if verbose:
            print("    点手牌 %s：悬停→点击→确认 ⇒ 动作 %s"
                  % (hand_card_id, [r.get("action_type") for r in rows]))
        return res

    # ------------------------------------------------------------ 抉择/二选一/三选一
    PICK_CLASS_ONE = "BP_ChooseOneCard_C"          # 二选一（"预报 / 抽一张牌"）
    PICK_CLASS_SPAWN = "BP_ChooseCardToSpawn_C"    # 三选一 / 预报的子选项 / develop
    # ★ 2026-09-26 补第三类：**牌库选牌**（scrying / "看牌库顶 N 张选一张"，13 个 case，
    #   例如 card_event_spoils_of_war / card_unit_panzer_iii_h / card_event_exploit_the_gap）。
    #   它是**独立一个类**，跟上面两个都不是父子关系 ⇒ 不补进来的话，这类抉择
    #   `picklist` 会报"没有候选在等"（症状跟当初漏 `BP_ChooseOneCard_C` 那次一模一样）。
    #   字段（`BP_ChooseCardToDraw_classes.hpp` 全字段核过）：
    #     `indexOfCardInDeck 0x0878`（ExposeOnSpawn，= 它在**牌库里的下标**，
    #      也是屏幕次序）、`cardBeingPlayed 0x0880`(触发手牌 actor)、
    #     `cardBeingPlayedID 0x095C`、`isOwnedByMe 0x0960`。
    #   ⚠ 这个类**没有名字字段**（不像 ChooseCardToSpawn 有 `Name_0`）⇒ 候选身份要靠
    #     `indexOfCardInDeck` 去**我方物理牌库**（`kardsmem.cards.deck_cards`，保持牌序、
    #     允许同名多份）取第 index 张。
    PICK_CLASS_DRAW = "BP_ChooseCardToDraw_C"
    NOTE_OFF_DRAW_INDEX = 0x0878        # int32 indexOfCardInDeck
    NOTE_OFF_DRAW_TRIGGER = 0x0880      # ptr   cardBeingPlayed（触发它的手牌 actor）
    NOTE_OFF_DRAW_TRIGGER_ID = 0x095C   # int32 cardBeingPlayedID
    NOTE_OFF_DRAW_OWNED = 0x0960        # bool  isOwnedByMe

    def pending_draw_sets(self, verbose: bool = True) -> dict:
        """★ **权威**：当前正在等的"选牌"候选集（三选一 / 预报两级），**不靠扫 actor**。

        路径：`BP_Logic_C::onlineMatch //0x0810` →
        `BP_OnlineMatch_C::selectCardToDrawPending //0x08F0`（`TMap<int32, FString>`）。
        ★ 2026-09-26 实测（dump 了那 0x80 字节）它的**真实语义**跟名字不完全一样：
          只有 **1 条**，`key` 是个小序号（如 2），`value` = **`"首张候选内部名:触发卡id"`**
          （例：`"card_event_sunny1_blue_sky:31"`，30 字符）。而且**只在"有选牌在等"时非空**、
          结束时为空 ⇒ 它给的是 **pending 标志 + 当前触发卡 id**，**不是**完整三项。
        完整三张在**动作流**里：`ZActionSelectCardToDrawPending{cardBeingPlayed, spawnCards}`
        （`spawnCards` = `a;b;c` 全串，就是这一级 spawn 出来的三张）。所以本函数把两者拼起来：
          trigger（map）→ 动作流里该 trigger 的 `spawnCards`（三张名）→ 与候选 actor 的 `index` 对照。
        返回：`{"ok", "pending": {trig_id: [三张名]}, "live": {trig_id: {...}}, "triggers": [...]}`；
        `live.*.index_match` = 候选 actor 按 index 排出来的名字是否与三张名一致 ⇒ 一致才说明
        "第 i 位 ↔ names[i]"这条映射成立（这是**点之前的权威依据**）。
        """
        from kardsmem.containers import tmap_int_to_fstring
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        off_om = self.off(lcls, "onlineMatch", NOTE_OFF_LOGIC_ONLINE_MATCH)
        om = self.m.ptr(logic + off_om) or 0
        if not om:
            return {"ok": False, "stopped": "BP_Logic_C::onlineMatch 为空（不在对局里？）"}
        ocls = self.uclass_of_instance(om)
        off_map = self.off(ocls, "selectCardToDrawPending", NOTE_OFF_OM_SELECT_CARD_TO_DRAW)
        raw = tmap_int_to_fstring(self.m, om + off_map)
        res = {"ok": True, "online_match": hex(om), "map_raw": raw}
        # ① map 给"pending 标志 + 当前 trigger"（value = "名字:triggerId"）
        trigs = []
        for _stage, val in raw.items():
            _nm, _sep, tid = (val or "").partition(":")
            if tid.strip().lstrip("-").isdigit():
                trigs.append(int(tid.strip()))
        # ② 动作流给"该 trigger 的完整三张"（spawnCards）
        names = {}
        try:
            from kardsmem.matchlog import MatchLog
            ml = self.matchlog()      # ★ 长寿命单例（别再每次新建：locate 4~6 s）
            for row in ml.since(max(0, ml.count() - 200)):
                for sa in (row.get("sub_actions") or []):
                    if sa.get("name") != "ZActionSelectCardToDrawPending":
                        continue
                    v = {x.get("name"): x for x in (sa.get("values") or [])}
                    cbp, sp = v.get("cardBeingPlayed", {}).get("value"), \
                        v.get("spawnCards", {}).get("text")
                    if cbp is not None and sp:
                        names[int(cbp)] = sp.split(";")
        except Exception as e:                                     # noqa: BLE001
            res["matchlog_error"] = str(e)
        res["pending"] = {t: names.get(t, []) for t in trigs}
        # 连标题一起给（内存读；`card_event_storm2_thunderstorm3` → `THUNDERSTORM`）
        res["titles"] = {n: self.card_title(n) for n in
                         {x for v in res["pending"].values() for x in v}}
        if not trigs and names:
            res["pending_hist"] = names       # 没在等时给最近几次的集合，便于对照
        cands = self.pick_candidates()
        live = {}
        for trig, nm in res["pending"].items():
            grp = sorted([c for c in cands if c.get("trigger_id") == trig],
                         key=lambda c: (c["index"] if c["index"] is not None else 99))
            live[trig] = {"actors": [(c["index"], c.get("name"), hex(c["actor"])) for c in grp],
                          "index_match": [c.get("name") for c in grp] == nm,
                          "n_names": len(nm), "n_actors": len(grp)}
        res["live"] = live
        res["triggers"] = sorted(res["pending"].keys())
        if verbose:
            print("    当前待选：%s" % (res["pending"] or "无（现在没在等选牌）"))
        return res

    def _deck_name_at(self, index: Optional[int], side=None) -> Optional[str]:
        """我方**物理牌库**第 `index` 张的牌名（给"牌库选牌"的候选定位用）。

        `kardsmem.cards.deck_cards` 保持牌序、允许同名多份 ⇒ 按 `indexOfCardInDeck` 取。
        读不出/越界返回 None，**不抛** —— 它只是给候选"补个名字"的增强，
        不该因为它把整个 `pick_candidates` 打挂。
        """
        if index is None or index < 0:
            return None
        try:
            from kardsmem.cards import deck_cards
            rows = (deck_cards(self.ks) if side is None else deck_cards(self.ks, side)) or []
            if index < len(rows):
                return rows[index].get("name")
        except Exception:                                      # noqa: BLE001
            return None
        return None

    def pick_candidates(self, cache: Optional[dict] = None) -> list:
        """当前**二选一 + 三选一**候选（两类统一读法），按各自的 index 排好序。

        ★★ 2026-09-25 补的（用户点名的"二选一和三选一需要完善"）：
          旧版只有 `choose_candidates()`，**只认 `BP_ChooseCardToSpawn_C`**（三选一），
          于是 `US WEATHER BUREAU` 那一层"预报 / 抽一张牌"的二选一**一个候选都读不到**，
          `pick_choice()` 会直接报"现在没有候选在等"——那不是"没在等"，是"看不见"。

        两个类的字段（全部来自 SDK dump 逐字段，不是 diff 出来的）：
          `ABP_ChooseCardToSpawn_C`（三选一）：`indexOfCardInDeck//0x0878`(次序)、
              `cardBeingPlayed//0x0880`(触发者的手牌 actor)、`Name_0//0x095C`(候选内部名)、
              `SelectedCard//0x0964`、`cardBeingPlayedID//0x0968`、`isOwnedByMe//0x096C`
          `ABP_ChooseOneCard_C`（二选一）：`chooseOneIndex//0x09A0`(次序, ExposeOnSpawn)、
              `_card//0x08C0` / `SpawnCard//0x08B8`(这个分支对应的卡对象)、
              `_motherCard//0x09B8`(触发它的手牌 actor)、`needsTarget//0x09A4`、
              `Clickable//0x09C1`
          ⇒ **两个类都有可靠的屏幕次序字段**，不必再靠屏幕坐标排序。
             二选一的**选项文字**要另读：触发卡（`UBaseCardObject`）的
             `chooseOneCards//0x138`（`TArray<FChooseOneCardStruct>`，`FText cardText@+0`），
             下标就是 `chooseOneIndex`（游戏自己的 `BP_HandCard_C::SpawnChooseOneCards`
             就是这么生成这些 actor 的）。

        返回每项：`{actor, cls, cls_name, kind, index, order_ok, name, label, selected,
        clickable, needs_target, trigger_actor, trigger_id, card_obj, card_id}`。
        `order_ok=False` 表示次序字段读不出（这时 `index` 是 None，调用方自己决定要不要盲点）。
        空列表 = 现在没有这类候选在等。
        """
        # ★ 2026-10-01：`scan_classes`（GObjects 全量扫）有时一次吃 30 s（预报刚弹候选时实测 `pick_candidates` 30.6 s，
        #   正常 0.6 s）。候选 actor 都是**世界里的 level actor** ⇒ 走 level-actor 快路径（外部实测 236 个 actor 0.00 s）；
        #   快路径出错才退回全量扫。
        if cache is None:
            try:
                rows = {n: self.actors_of_class(n, refresh=True)
                        for n in (self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN, self.PICK_CLASS_DRAW)}
                # 安全网：快路径一个都没看到、但游戏明明在等选择（pick_pending）⇒ 候选可能不在 level actor 里 ⇒ 退回全量扫
                if not any(rows.values()) and self.pick_pending().get("pending"):
                    self.notes.append("pick_candidates：快路径为空但 pick_pending=True，退回全量扫")
                    rows = self.scan_classes([self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN,
                                              self.PICK_CLASS_DRAW], skip_cdo=True)
            except Exception as e_fast:                            # noqa: BLE001
                self.notes.append("pick_candidates：level-actor 快路径失败，退回全量扫（%s）" % e_fast)
                rows = self.scan_classes([self.PICK_CLASS_ONE, self.PICK_CLASS_SPAWN,
                                          self.PICK_CLASS_DRAW], skip_cdo=True)
        else:
            rows = cache
        out = []
        for cls_name, kind in ((self.PICK_CLASS_ONE, "choose_one"),
                               (self.PICK_CLASS_SPAWN, "choose_spawn"),
                               (self.PICK_CLASS_DRAW, "choose_draw")):
            for a in (rows.get(cls_name) or []):
                cls = self.uclass_of_instance(a)
                if not cls:
                    continue
                if kind == "choose_one":
                    idx = self.peek(a, self.off(cls, "chooseOneIndex", NOTE_OFF_ONE_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "_motherCard", NOTE_OFF_ONE_MOTHER)) or 0
                    card_obj = self.m.ptr(a + self.off(cls, "_card", NOTE_OFF_ONE_CARD)) or 0
                    spawn_obj = self.m.ptr(
                        a + self.off(cls, "SpawnCard", NOTE_OFF_ONE_SPAWN_CARD)) or 0
                    needs_t = bool(self.peek(a, self.off(cls, "needsTarget",
                                                        NOTE_OFF_ONE_NEEDS_TARGET), "u8"))
                    clickable = bool(self.peek(a, self.off(cls, "Clickable",
                                                           NOTE_OFF_ONE_CLICKABLE), "u8"))
                    name, selected, owned, trig_id = None, None, None, self._actor_card_id(trig)
                    is_effect = None
                elif kind == "choose_spawn":
                    idx = self.peek(a, self.off(cls, "indexOfCardInDeck",
                                                NOTE_OFF_SPAWN_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "cardBeingPlayed",
                                                   NOTE_OFF_SPAWN_TRIGGER)) or 0
                    card_obj = spawn_obj = 0
                    name = self.pool.fname_of(a, self.off(cls, "Name_0", NOTE_OFF_SPAWN_NAME))
                    selected = bool(self.peek(a, self.off(cls, "SelectedCard",
                                                          NOTE_OFF_SPAWN_SELECTED), "u8"))
                    owned = bool(self.peek(a, self.off(cls, "isOwnedByMe",
                                                       NOTE_OFF_SPAWN_OWNED), "u8"))
                    needs_t, clickable = None, None
                    trig_id = self.peek(a, self.off(cls, "cardBeingPlayedID",
                                                    NOTE_OFF_SPAWN_TRIGGER_ID), "s32")
                    # ★ 2026-09-27：`BP_ChooseCardToSpawn_C::isEffect // 0x980`。它是
                    #   同一分支里区分"**预报第一段**（三个天气模板，isEffect=0）"和
                    #   "**预报第二段 / effect 选择**（isEffect=1，提交串变
                    #   `effectSelected:<名>`）"的**权威标志** —— 这两层是**同一个 actor 类**，
                    #   光看类名分不出来（以前靠 `SelectedCard` 启发式，会误判）。
                    is_effect = bool(self.peek(a, self.off(cls, "isEffect",
                                                           NOTE_OFF_SPAWN_IS_EFFECT), "u8"))
                else:                                     # choose_draw（牌库选牌）
                    idx = self.peek(a, self.off(cls, "indexOfCardInDeck",
                                                self.NOTE_OFF_DRAW_INDEX), "s32")
                    trig = self.m.ptr(a + self.off(cls, "cardBeingPlayed",
                                                   self.NOTE_OFF_DRAW_TRIGGER)) or 0
                    card_obj = spawn_obj = 0
                    owned = bool(self.peek(a, self.off(cls, "isOwnedByMe",
                                                       self.NOTE_OFF_DRAW_OWNED), "u8"))
                    needs_t, clickable, selected = None, None, None
                    is_effect = None
                    trig_id = self.peek(a, self.off(cls, "cardBeingPlayedID",
                                                    self.NOTE_OFF_DRAW_TRIGGER_ID), "s32")
                    # 这个类没有名字字段 ⇒ 用 `indexOfCardInDeck` 去**我方物理牌库**取。
                    # 取不到就留 None（调用方至少能靠 `index` 排屏幕次序，或者先 `raw deck`）。
                    name = self._deck_name_at(idx) if owned else None
                out.append({"actor": a, "cls": cls, "cls_name": cls_name, "kind": kind,
                            "index": idx, "order_ok": idx is not None, "name": name,
                            "label": None, "selected": selected, "clickable": clickable,
                            "needs_target": needs_t, "owned_by_me": owned,
                            "is_effect": is_effect,
                            # ★ actor 自己的 `CardID // 0x3C8`。对 **choose_draw** 尤其重要：
                            #   spawner 在 `FinishSpawningActor` 前显式写过它（
                            #   `BP_VisualController.cpp:10206`）⇒ 这是"这张候选是牌库里的哪张"
                            #   的**权威身份**，比用 `indexOfCardInDeck` 回查物理牌库更稳。
                            "card_id": self._actor_card_id(a),
                            "trigger_actor": trig, "trigger_id": trig_id,
                            "card_obj": card_obj or spawn_obj})
        # 二选一的选项文字 + "这个分支要不要目标"：触发卡的 chooseOneCards（下标 = chooseOneIndex）
        opts_cache = {}
        for c in out:
            if c["kind"] != "choose_one" or not c["trigger_actor"]:
                continue
            key = c["trigger_actor"]
            if key not in opts_cache:
                opts_cache[key] = self.choose_one_options(key)
            opts = opts_cache[key]
            if c["index"] is not None and 0 <= c["index"] < len(opts):
                c["label"] = opts[c["index"]].get("label")
                # ★ "选完这个分支还要再点目标"（两阶段）——以**游戏数组**为准；
                #   候选 actor 自己的 needsTarget 是它的副本，两个都留着互相校验。
                c["option_needs_target"] = opts[c["index"]].get("needs_target")
        out.sort(key=lambda c: (c["kind"], c["index"] if c["index"] is not None else 99))
        # ★ 2026-09-26（用户："标题表内存测能读"）：给每个有内部名的候选补上**卡面标题**
        #   —— 纯内存读（UClass → CDO → title FText@0x58），不查任何静态导出表。
        #   这样 `picklist` 就能直接显示"THUNDERSTORM"这种屏上真名，而不是内部名。
        for c in out:
            if c.get("name") and not c.get("title"):
                c["title"] = self.card_title(c["name"])
        return out

    def choose_one_options(self, trigger_actor: int, verbose: bool = False) -> list:
        """触发卡的 `chooseOneCards`（`UBaseCardObject+0x138`）→ **每个选项的文字 + 是否要目标**。

        `FChooseOneCardStruct { FText cardText // 0x00; bool selectTargetOnPlayedFromHand // 0x10 }`
        （`kards_structs.hpp:1154`）。`chooseOneIndex` 就是这个数组的下标 ——
        游戏自己的 `BP_HandCard_C::SpawnChooseOneCards(chooseOneCards, motherCardID)`
        就是拿着这个数组生成候选 actor 的。

        返回 `[{label, needs_target, index, ftext_ok}]`；读不出就如实标（**不编**）。
        `selectTargetOnPlayedFromHand` 就是"选完这个分支**还要再点目标**"的来源
        （`BP_ChooseOneCard.cpp:172` 把它抄进 actor 的 `needsTarget//0x09A4`）。

        ⚠ 2026-09-25：上一版这个函数读回来是空/空串（我误以为"卡上没有这个数组"）。
        实际 `us_weather_bureau` 的导出里明明有两条 `cardText`；所以这次把**每一层**的
        原始值都带进返回值（`ftext_ok`），读失败时能立刻看出是数组没读到还是 FText 解析失败。
        """
        actor = trigger_actor
        if not actor:
            return []
        from kardsmem.names import ftext_at                     # noqa: PLC0415（本文件惯例：用时才 import）
        acls = self.uclass_of_instance(actor)
        if not acls:
            return []
        obj = self.m.ptr(actor + self.off(acls, "selfBaseCardRef",
                                          NOTE_OFF_ACTOR_SELF_BASECARD)) or 0
        if not obj:
            return []
        ocls = self.uclass_of_instance(obj)
        base = obj + self.off(ocls, "chooseOneCards", NOTE_OFF_CARDOBJ_CHOOSE_ONE_CARDS)
        data = self.m.ptr(base) or 0
        num = self.m.i32(base + 8) or 0
        if verbose:
            print("    chooseOneOptions: actor=0x%X obj=0x%X base=0x%X data=0x%X num=%s"
                  % (actor, obj, base, data, num))
        if not data or num <= 0 or num > 8:
            return []
        out = []
        for i in range(num):
            ent = data + i * PICK_STRUCT_SIZE
            label = ftext_at(self.m, ent, 0)     # FChooseOneCardStruct::cardText 在条目 +0x00
            needs = bool(self.m.u8(ent + 0x10))
            out.append({"index": i, "label": label, "needs_target": needs,
                        "ftext_ok": label is not None})
        if verbose:
            for o in out:
                print("      选项%d label=%r needs_target=%s" % (o["index"], o["label"],
                                                                o["needs_target"]))
        return out

    def _actor_card_id(self, actor: int) -> Optional[int]:
        """`ABP_BaseCard_C::CardID // 0x03C8`（actor 上的那张牌的 id）。"""
        if not actor:
            return None
        cls = self.uclass_of_instance(actor)
        if not cls:
            return None
        return self.peek(actor, self.off(cls, "CardID", NOTE_OFF_ACTOR_CARDID), "s32")

    def pending_gate(self) -> dict:
        """动作闸门用的"选择界面还开着吗"：`pick_pending` 之上**剔除幽灵手牌目标提示**。

        2026-10-08 实机：KAR 无可选手牌时游戏跳过提示，但 `isSelectingHandTarget` 旗标悬挂、widget 已 `pendingKill`
        （`hand_target_pending` 正确判成 `ghost`），而动作闸门直接看原始旗标 ⇒ 攻击/结束回合被我们自己拒了 6 步。
        这里把"仅手牌目标旗标 + 已判幽灵"的情形当作没有待选（返回 `ghost` 原因），其余情形原样返回。
        """
        pend = self.pick_pending()
        try:
            if (pend.get("pending") and pend.get("is_selecting_hand_target") == 1
                    and not pend.get("choose_one_active")):
                ht = self.hand_target_pending(verbose=False, with_legal=False)
                if isinstance(ht, dict) and ht.get("ghost") and not ht.get("pending"):
                    return dict(pend, pending=False, ghost=ht["ghost"], pending_raw=True)
        except Exception as exc:                                  # noqa: BLE001
            pend = dict(pend, ghost_read_error=repr(exc))     # 判幽灵读不出 ⇒ 保守按原样拦，但留痕
        return pend

    def pick_pending(self) -> dict:
        """现在**是不是**真的有一个"选一张牌"界面在等（`pick_state().pending`）。

        ★ 2026-09-25 实机教训：**候选 actor 存在 ≠ 界面在等**。点完第三层之后
          `chooseOneActive` 已经回 0、`pending=False`，但那 3 个 `BP_ChooseCardToSpawn_C`
          actor 还活着（GC 没来得及回收）——照 `pick_candidates()` 的列表去点，
          点的是"已经结束的那一层的残留"。所以**动手前必须先问这个**。
        """
        from kardsmem.pick import pick_state
        st = pick_state(self.ks)
        return {"pending": bool(st.get("pending")),
                "choose_one_active": st.get("choose_one_active"),
                "is_selecting_hand_target": st.get("is_selecting_hand_target"),
                "reason": st.get("reason")}

    def pick_groups(self, cache: Optional[dict] = None) -> dict:
        """候选按**触发者**分组 → `{trigger_key: [候选…]}`。

        ★ 为什么要分：一次二选一/三选一会连着弹好几层（US WEATHER BUREAU 二选一 →
          三选一 → 再选具体哪张），而**上一层的 actor 不一定会立刻消失**，
          于是同一个列表里可能混着两组不同触发者的候选（handoff §二3 那次
          就是踩在这里，`pick_choice(index)` 拿"列表里第几个"点错了）。
        """
        groups: dict = {}
        for c in self.pick_candidates(cache=cache):
            key = (c["kind"], c["trigger_actor"] or c["trigger_id"] or 0)
            groups.setdefault(key, []).append(c)
        for v in groups.values():
            v.sort(key=lambda c: c["index"] if c["index"] is not None else 99)
        return groups

    def choose_candidates(self) -> list:
        """当前**三选一**候选 → [{actor, index, name, selected, trigger_card_id}]。

        直接复用 `kardsmem.pick.choose_candidates()`（`BP_ChooseCardToSpawn_C`）。
        ★ 要看**二选一**（或两类一起看）用 `pick_candidates()`；这个函数保留是为了
          跟 `kardsmem`/`agent` 层的既有读法对齐。
        """
        from kardsmem.pick import choose_candidates as _cc
        return _cc(self.ks, owned_only=True)

    def pending_summary(self) -> dict:
        """**现在在等什么**（只读汇总）—— 对标 `ops.py::pick_state()`，但把四类等待一起报。

        三前端（shell/MCP/NN）每一步动手前先问这一个就够：
          `pick`（二选一/三选一/预报的面板在等）/ `hand_target`（选一张手牌当目标）/
          `board_target`（板卡"待点目标"= `cardBeingPlayedFromHand`，单位部署第二阶段或
          指令选项 resolve 之后）/ `arrows`（场上箭头数）/ `queue_running`（动作队列跑着）。
        ★ 只是**读**；判定"能不能动手"是调用方的事（`preflight()` 有汇总入口）。
        """
        return {"pick": self.pick_pending(),
                "candidates": len(self.pick_candidates() or []),
                "hand_target": self.selecting_hand_target(),
                "board_target": self.card_being_played_from_hand(),
                "board_target_loc": self.card_being_played_loc(),
                "arrows": len(self.arrow_actors()),
                "queue_running": self.queue_running()}

    def choose_one_with_target(self, index: int, target_id: int, trigger=None,
                               kind: str = "choose_one", verbose: bool = True) -> dict:
        """**"抉择 + 指向"两步合一**（HIDDEN PLANS / 保密计划那种卡）：先点选项，再点场上单位。

        ★★ 为什么不能拿 `pick_choice().ok` 当闸门（2026-09-27 实机，用户："你那次 pick 进入了
          state after choose。说明 with_select 没成功。是bug。"）：
          这类卡**点选项这一步本来就没有动作流回执** —— `XActionPlayCardFromHand
          {chooseOneIndex}` 要等**目标点完**才进流。实机 `play 16` → `choose 1`：动作流空，
          但 `chooseOneActive` 1→0 且 `cardBeingPlayedFromHand` **=16**（游戏自己进了
          "待点目标"态）。旧写法 `if not pick_choice.ok: return` 就死在这里 ⇒ 永远走不到第二步。
        ⇒ 闸门改成**游戏自己的状态**：只要 `card_being_played_from_hand()` 非 0 就继续点目标；
          真正的 `ok` 由**第二步** `select_unit_target` 的动作流判
          （`XActionPlayCardFromHand{targetCardID}`）。选项那步的 `ok=False` 如实留在 `choice` 里
          （旁证不许进 `ok` —— 同类教训；判据只能**往后挪一步**，不能降级成旁证）。
        """
        a = self.pick_choice(int(index), trigger=trigger, kind=kind, verbose=verbose)
        playing = self.card_being_played_from_hand()
        if not playing:
            # ① 选项自己就完事了（例：另一支"从牌库抽一张隐蔽单位"，不需要目标）；
            # ② 或者**真**没点上（动作流空 + 也没进待点目标态）。
            if a.get("ok"):
                return {"ok": True, "choice": a, "resolved_at_option": True,
                        "target_result": None}
            return {"ok": False, "choice": a, "reason": "option_not_consumed",
                    "error": "选项点了之后既没有动作流回执、也没进'待点目标'态 ⇒ 这一步没成"
                             "（若这张卡的选项不需要目标，请用 choose_one()）"}
        sel = self.select_unit_target(int(playing), int(target_id), verbose=verbose)
        out = {"ok": bool(sel.get("ok")), "choice": a, "target_result": sel,
               "playing_from_hand": int(playing), "criterion": "action_stream(第二步)"}
        if not a.get("ok"):
            out["note"] = ("选项那步动作流为空是**正常的**（这类卡的回执要等目标点完）——"
                           "判据是 target_result 里的 `XActionPlayCardFromHand{targetCardID}`")
        return out

    def pick_choice(self, index: int = 0, trigger=None, kind: Optional[str] = None,
                    is_effect: Optional[bool] = None, verbose: bool = True) -> dict:
        """点当前抉择界面的第 `index` 个候选（**在同一个触发者分组内**数）。

        `index` 的语义（2026-09-25 修）：**分组内**按屏幕次序的第几个，不是整个列表的
        第几个 —— 列表可能同时混着两层不同触发者的候选（见 `pick_groups()`）。

        `trigger`：显式指定触发者（`trigger_actor` 地址，或触发者的 CardID）。
        不给时：只在**唯一一组**候选时自动选；有多组就报 `ambiguous` 让调用方指定，
        **绝不猜**（猜错就是"点在了不存在的东西上"，还有把对手界面点掉的观感风险）。

        `kind`：`"choose_one"` / `"choose_spawn"`，用来在两个类同时有候选时消歧。

        点击方式：`OnActorClicked(bool IsPrecise)`（`ABP_BaseCard_C` 上，两类都继承它；
        实机验证过需要带参数，传 NULL 会在函数体里读 `IsPrecise` 空指针崩）。
        ★★ 判据（2026-09-27 用户当场纠正"**没pick到**"之后收窄）：**只看动作流**，
        而且只认两形状：
          * `XActionCardToDrawSelected` / `ZActionSelectCardToDrawPending` —— 选择本身发出去了；
          * `XActionPlayCardFromHand` 且 **`chooseOneIndex >= 0`** —— 这张牌的选项被消费了
            （出牌→解析出选择那条链；实测 HIDDEN PLANS 选项层就是它）。
        **不再**把"候选集合变了 / `SelectedCard` 翻 True / 只要有任何 action"算成功：
        本轮 `pick_choice(1)` 报了 `候选 2→1、outcome=advanced`，用户指出**其实没 pick 中**
        —— 那时 `actions=[]`，按旧判据却被 `candidates_changed` 判成了成功（假阳性）。
        集合变化/选中翻转/仍待选只作**旁证**返回（`candidates_changed`/`selected_flip`），
        不参与 `ok`。
        """
        _T = {"t0": time.time()}                                   # 分段计时（2026-10-01：抉择一步曾耗 60 s，查慢在哪）
        cands = self.pick_candidates()
        _T["cands"] = round(time.time() - _T["t0"], 2)
        if not cands:
            return {"ok": False, "error": "现在没有抉择候选在等（两类都为空）"}
        # ★ 动手前确认界面**真的**在等（候选 actor 可能是上一层没回收的残留）
        pd = self.pick_pending()
        _T["pending"] = round(time.time() - _T["t0"], 2)
        if not pd["pending"]:
            return {"ok": False, "error": "现在没有选择界面在等（%s）——别点残留 actor" % pd["reason"],
                    "pick_state": pd}
        cands = [c for c in cands if self.obj_alive(c["actor"])] or []   # 丢掉已销毁的陈旧候选
        if not cands:
            return {"ok": False, "reason": "no_live_candidates", "timing": _T,
                    "error": "候选 actor 都已被销毁/回收（陈旧），不点"}
        if kind:
            cands = [c for c in cands if c["kind"] == kind]
        # ★ 2026-09-27 实机补：预报**第一段 vs 第二段**是同一个 actor 类、
        #   `trigger_actor` 又都是 0 ⇒ 光靠 kind/index **分不开**（那次靠扫描顺序
        #   碰巧点对）。权威分界是 `BP_ChooseCardToSpawn_C::isEffect // 0x980`：
        #   第一段 is_effect=False、第二段/effect 选择 is_effect=True。
        if is_effect is not None:
            cands = [c for c in cands if c.get("is_effect") == bool(is_effect)]
            if not cands:
                return {"ok": False, "reason": "no_such_layer",
                        "error": "按 is_effect=%s 过滤后没有候选（现在等的是另一层？）"
                                 % is_effect}
        if trigger is not None:
            t = int(trigger)
            cands = [c for c in cands
                     if c["trigger_actor"] == t or c["trigger_id"] == t
                     or (c["card_obj"] and c["card_obj"] == t)]
        groups: dict = {}
        for c in cands:
            groups.setdefault((c["kind"], c["trigger_actor"] or c["trigger_id"] or 0), []).append(c)
        # ★ 丢掉"已经不在了"的那一层（实机数据）：
        #   二选一：点过之后 `Clickable` 会从 True 翻 False（点完 index0 两个候选都 False）；
        #   三选一：点过之后被点那个的 `SelectedCard` 翻 True —— 而**正在等你选**的那一层
        #   应该是"一个都没选"的。★ 踩过：OVERCAST 预报点完第一层后，第一层 3 个
        #   （有一个 selected=True）和第二层 3 个（都 False）**同时**在列表里
        #   ⇒ 两个同 kind 的组 ⇒ 旧代码报"ambigUous"拒绝动手，驱动在那一屏空转 18 步。
        live = {}
        for k, v in groups.items():
            if k[0] == "choose_one":
                if any(c["clickable"] is True for c in v):
                    live[k] = v
            elif not any(c["selected"] is True for c in v):
                live[k] = v
        if not live and all(
                (c["clickable"] is False) if c["kind"] == "choose_one"
                else (c["selected"] is True) for c in cands):
            # ★ 2026-09-25 实机补的洞：调用方显式给了 `kind=`（或 trigger=）之后，
            #   过滤结果里**只剩"已经不在的那一层"**时，旧代码 `live or groups` 会退回去
            #   点那个死掉的 actor —— 看起来无害，但那是一次**没有对应语义的输入**。
            #   实测场景：US WEATHER BUREAU 二选一 → 点了"Forecast."之后，二选一那两个
            #   actor 还在（Clickable=False），三选一刚弹出来；调用方按 kind='choose_one'
            #   过滤就会去点那两个残留。这里直接拒绝。
            return {"ok": False, "reason": "layer_gone",
                    "error": "过滤出来的候选全都是**已经结束的那一层**（clickable=False / 已选中），"
                             "别点残留 actor；用不带 kind 的调用或换 kind",
                    "candidates": cands}
        groups = live or groups
        if len(groups) > 1:
            # 同 kind 还有多组（极少）：取 actor 地址最大的那组 —— 新生成的对象地址更大，
            # 是"刚刚弹出来的这一层"。**这只是排序偏好**，不是判据；说出来免得当成事实。
            keys = sorted(groups, key=lambda k: max(c["actor"] for c in groups[k]))
            self.notes.append("多组同 kind 候选 %s，按 actor 地址取最新一组 %s"
                              % (list(groups), keys[-1]))
            groups = {keys[-1]: groups[keys[-1]]}
        if not groups:
            return {"ok": False, "error": "过滤后没有候选（trigger/kind 给错了？）",
                    "candidates": cands}
        if len(groups) > 1:
            return {"ok": False, "error": "有 %d 组不同触发者的候选，必须显式给 trigger= 或 kind="
                                          "（猜错就会点错层）" % len(groups),
                    "groups": {("%s/%s" % k): [(c["index"], c["name"] or c["label"], c["actor"])
                                               for c in v] for k, v in groups.items()}}
        (gkind, gtrig), group = next(iter(groups.items()))
        group.sort(key=lambda c: c["index"] if c["index"] is not None else 99)
        if index < 0 or index >= len(group):
            return {"ok": False, "error": "index=%d 超出该组候选数 %d" % (index, len(group)),
                    "group": group}
        pick = group[index]
        actor = pick["actor"]
        cls = pick["cls"]
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)

        mk = self._mark()
        # ★ 2026-09-26 重构：改走**统一点击原语** —— 它复刻 PC 的真实松手流程
        #   （`GlobalMouseUp(mouseDownActor)` → 未消费才转发 `OnActorClicked`），
        #   并带上 L0 队列闸门 + L1 PC 拖拽状态机。旧写法是"只 hover 一下就直接
        #   `OnActorClicked`"，跳过了 PC 那一跳。
        click_res = self.click_actor(actor, is_precise=1, verbose=True)
        _T["click"] = round(time.time() - _T["t0"], 2)
        if isinstance(click_res, dict) and click_res.get("reason") == "actor_not_alive":
            return {"ok": False, "reason": "actor_not_alive", "picked": pick, "timing": _T,
                    "error": click_res.get("error")}

        # 回执：**动作流**为准（`XActionCardToDrawSelected` = 选择真的发出去了；
        #   `ZActionSelectCardToDrawPending` = 进下一层）。另外三个旁证：
        #   ① 这层的候选集合变了 ② 这个 actor 的 `SelectedCard` 翻 True
        #   ③ 界面不再 pending。
        # ⚠ 实机踩过：只按"候选集合变没变"判会**假阴性**——点二选一那一层时
        #   动作流给的是 `XActionPlayCardFromHand`（那条链是"出牌→解析出选择"），
        #   我第一版只找 pick 那两个类型 ⇒ 明明点成功了却报 ok=False。
        from kardsmem.matchlog import MatchLog
        ml = self.matchlog()          # ★ 长寿命单例（别再每次新建：locate 4~6 s）
        PICK_ACTIONS = ("ZActionSelectCardToDrawPending", "XActionCardToDrawSelected")

        def _choose_one_index(row: dict) -> int:
            """从一条 action 里挖 `chooseOneIndex`（顶层 data 或任意 sub_action 里都找）。"""
            vals = []
            for d in (row.get("data") or []):
                if isinstance(d, dict) and d.get("name") == "chooseOneIndex":
                    vals.append(d.get("value"))
            for s in (row.get("sub_actions") or []):
                for d in ((s or {}).get("values") or []):
                    if isinstance(d, dict) and d.get("name") == "chooseOneIndex":
                        vals.append(d.get("value"))
            vals = [v for v in vals if isinstance(v, int)]
            return max(vals) if vals else -1

        t0 = time.time()
        rows = []
        seen = []
        while time.time() - t0 < 3.0:
            rows = ml.since(mk)
            seen = [r.get("action_type") for r in rows]
            if seen:
                break
            time.sleep(0.05)
        hit = [a for a in seen if a in PICK_ACTIONS]
        consumed = [(i, r.get("action_type"), _choose_one_index(r)) for i, r in enumerate(rows)
                    if r.get("action_type") == "XActionPlayCardFromHand"
                    and _choose_one_index(r) >= 0]
        _T["receipt"] = round(time.time() - _T["t0"], 2)
        after = self.pick_candidates()
        _T["after"] = round(time.time() - _T["t0"], 2)
        changed = (tuple(sorted(c["actor"] for c in after))
                   != tuple(sorted(c["actor"] for c in cands)))
        sel_flip = any(c["selected"] is True for c in after
                       if c["kind"] == gkind and c["index"] == pick["index"])
        still_pending = self.pick_pending()["pending"]
        # ★★ 2026-09-27 实机（HIDDEN PLANS / 保密计划 16，选项 "Reveal a Covert unit and
        #   give it +1+1."）：**带目标的抉择卡在"点选项"这一步动作流是空的** —— 真正的回执
        #   要等目标点完那一下（`ZActionPlayCardFromHand{chooseOneIndex:1, targetCardID:27}`
        #   + `ZActionRevealCard{27}` + 两组 `ZActionGain*`）。
        #   这时游戏自己的状态是"面板关了 + `cardBeingPlayedFromHand` = 这张牌" ⇒ 把它当
        #   **旁证**报出来（`awaiting_target` / `playing_from_hand`），**绝不进 `ok`**
        #   （弯路 #22 的纪律：集合变化、界面关了、选中翻转都不算"成没成"）。
        playing_after_pick = 0
        try:
            playing_after_pick = self.card_being_played_from_hand()
        except Exception:                                        # noqa: BLE001
            playing_after_pick = 0
        _T["end"] = round(time.time() - _T["t0"], 2)
        _T.pop("t0", None)
        awaiting = bool(playing_after_pick) and not still_pending
        # ★★ 判据 = **动作流**（用户 2026-09-27："没pick到"）：只见旁证不算成功。
        ok = bool(hit) or bool(consumed)
        if verbose:
            print("    抉择[%s] 点了第 %d 个（index=%s %s actor=0x%X）→ 动作流=%s "
                  "chooseOne消费=%s 候选 %d→%d 集合变=%s 选中=%s 仍待选=%s "
                  "待点目标=%s ⇒ ok=%s"
                  % (gkind, index, pick["index"], pick["name"] or pick["label"], actor,
                     hit or seen, [c[2] for c in consumed], len(cands), len(after),
                     changed, sel_flip, still_pending,
                     (playing_after_pick or "-"), ok))
        return {"ok": ok,
                "criterion": "action_stream",
                "outcome": ("awaiting_target" if awaiting
                            else ("advanced" if still_pending or changed else "resolved")),
                "kind": gkind, "picked": pick, "actions": hit,
                "consumed_by_play": consumed, "actions_seen": seen,
                "candidates_changed": changed, "selected_flip": sel_flip,
                "still_pending": still_pending,
                # 旁证（不进 ok）：游戏自己已经进了"待点目标"态 ⇒ 这一步还没回执是**正常的**
                "awaiting_target": awaiting,
                "playing_from_hand": playing_after_pick or None,
                "candidates_after": after, "timing": _T}

    def pick_layers(self, index: int = 0, kind: Optional[str] = None,
                    max_layers: int = 4, settle: float = 0.5,
                    verbose: bool = True) -> dict:
        """**一口气走完多层抉择链**（预报"天气→2K/4K/6K"、`US WEATHER BUREAU` 三层那种）。

        对标 `ops.py::act_pick`（旧鼠标版把**两级**写死在签名里：`index`/`index2`）。
        这里**不写死层数**：每点一层就重新读 `pick_pending()`/`pick_candidates()`，
        还在等就再点同一 `index`，直到没有候选等（或到 `max_layers`）。
        ★ 每层的成败**只看动作流**（`pick_choice` 的判据：`XActionCardToDrawSelected` /
          `ZActionSelectCardToDrawPending` / `XActionPlayCardFromHand{chooseOneIndex>=0}`）。
        实机（2026-09-26）：二选一 → 三选一 → 三选一，逐层 `XActionCardToDrawSelected`，
        最后一层点完 `pending=False`（面板自己关）。
        """
        layers = []
        ok = True
        for i in range(max(1, int(max_layers))):
            pd = self.pick_pending()
            cands = self.pick_candidates() if pd.get("pending") else []
            if not cands:
                break
            r = self.pick_choice(index, kind=kind, verbose=verbose)
            layers.append({"layer": i, "ok": bool(r.get("ok")),
                           "actions": r.get("actions"),
                           "consumed_by_play": r.get("consumed_by_play"),
                           "picked": (r.get("picked") or {}).get("name"),
                           "kind": r.get("kind"),
                           "candidates_before": len(cands)})
            if not r.get("ok"):
                ok = False
                break
            self.settle(settle, 4)    # 换层之间等 UI（每层一个新面板）⇒ 按帧等，降帧时不空等
        still = self.pick_pending()
        return {"ok": bool(layers) and ok and not still.get("pending"),
                "layers": layers, "n_layers": len(layers),
                "criterion": "action_stream",
                "still_pending": still.get("pending"), "pick_state": still}

    def choose_card(self, index: int = 0, kind: Optional[str] = None,
                    is_effect: Optional[bool] = None, trigger=None,
                    verbose: bool = True) -> dict:
        """**选一张牌**：当前等着的候选里点第 `index` 个（**在同一个触发者分组内**数）。

        ★★ 2026-09-27 合并（用户："是不是所有选卡最终都落在 notify 上？所以或许可能两种
          不用分开。"）—— **确实落在同一个 notifier 上**，所以对调用方**不需要分两个动词**：
            * `GetChooseSpawnCards` 在**全部导出里只有 1 个调用点**
              （`BP_CardFunctions.cpp:5637`，位于 `selectCardToDraw(...)` 的
              `fromTopOfDeck=false` 分支）；`fromTopOfDeck=true` 分支同样走
              `NotifySelectCardToDrawPending`（`:5610`/`:5705`）。
            * 通知器 → `BP_OnlineMatch::HandleSelectCardToDrawPending` →
              `AddSubActionSelectCardToDrawPending` → **一个**处理器
              `BP_VisualController::ExecuteSubactionSelectCardToDraw`（`:9817`），
              按 sub-action 的 `scrying` 标志分派到
              `BP_ChooseCardToDraw_C`（scrying=1）或 `BP_ChooseCardToSpawn_C`（scrying=0）。
          差异**只在读侧**，而且已经由 `pick_candidates()` 处理掉了：
            * scrying 的候选身份 = actor `CardID // 0x3C8`（本局真实卡）；
            * 非 scrying 的候选身份 = `Name_0 // 0x95C`（模板 FName）。
          调用方只管给 index ⇒ 一个动词足够。

        `kind`：**只在消歧时要**（同一时刻两类候选都在场上）。取值
          `"choose_one"`（卡自己的抉择面板 `BP_ChooseOneCard_C`，**另一个链**：由
          `BP_HandCard::SpawnChooseOneCards` 生成，不经这个 notifier）/
          `"choose_spawn"`（`scrying=0`）/ `"choose_draw"`（`scrying=1`）。
          不给就按 `pick_choice` 的"活跃层"规则自动挑；两类真的同时在等时会报
          `ambiguous`，那时才需要 `kind=`（**不猜**）。

        `is_effect`：**预报两段的权威分界**（第一段 0 / 第二段 1）。同一时刻两层都在等时
          **一定要给**：它们的 `trigger_actor` 都是 0、kind 也一样，`index` 分不开。
        ★ 提交方式：**单击即提交**（三个分支都是 `OnActorClicked` →
          `AddMoveToPlayerMoveQueue`；真按钮/确认键只出现在 `pick_hand` 那条链）。
        判据：**动作流**（`XActionCardToDrawSelected` / `ZActionSelectCardToDrawPending` /
        `XActionPlayCardFromHand{chooseOneIndex>=0}`），见 `pick_choice` 的 docstring。
        """
        return self.pick_choice(index, trigger=trigger, kind=kind,
                               is_effect=is_effect, verbose=verbose)
