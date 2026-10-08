# -*- coding: utf-8 -*-
"""ops/play.py —— 出牌 / 部署 / 上线 / 攻击 / 结束回合。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import struct
import time
from typing import Optional

from kardsmem.pick import player_controller
from ops import primitives as P
from ops.consts import (
    HUD_CLASS, LOC_BOARD_FRONTLINE, NEEDS_TARGET_REASONS, NOTE_OFF_ACTOR_SELF_BASECARD,
    NOTE_OFF_ARROW_OVER_CARD_ID, NOTE_OFF_CARDOBJ_TARGET_OVERRIDE, NOTE_OFF_CARD_UNDER_CURSOR,
    NOTE_OFF_LOCATION_ENUM, NOTE_OFF_LOCATION_NUMBER, NOTE_OFF_ROW_UNDER_CURSOR, SETTLE_AFTER_DISPATCH,
    SETTLE_AFTER_DOWN, SETTLE_AFTER_TICK, SETTLE_HOVER, SETTLE_HOVER_PLAY,
)


class PlayMixin:
    """出牌 / 部署 / 上线 / 攻击 / 结束回合。"""

    # ------------------------------------------------------------ 动作
    def end_of_turn(self, verbose: bool = True, force: bool = False) -> dict:
        """结束回合：按钮的 悬停 → 按下 → 松开 → 点击(自身) → 点击(HUD 监听者)。

        ★ 为什么走 `BndEvt__*` 而不是按钮的 `OnMouseEnter`：后者签名带
          `(FGeometry, FPointerEvent)` 两个**结构体**入参，传 NULL 会崩；而真实点击
          驱动的实际逻辑就在这几个组件级句柄上。悬停那一步没有省 —— 它调的是
          `..._OnButtonHoverEvent__DelegateSignature`。
        ★ `force=False`（默认）时**拒绝**在"选择界面还开着"的情况下结束回合 —— 跟
          `ops.py::end_of_turn` 同一条纪律：那时点结束回合会让游戏**默认选最左**，
          等于替对手/替自己乱选。`force=True` 才越过（越过前明确知道这个后果）。
        """
        if not force:
            pend = self.pending_gate()
            if pend.get("pending"):
                how = ("select_hand_target()（手牌目标：点牌 + 确认两下）"
                       if pend.get("is_selecting_hand_target") == 1
                       else "pick_choice(index)（抉择/预报）")
                return {"ok": False, "pending": pend, "reason": "pending_selection",
                        "error": "选择界面还开着（%s）——结束回合会让游戏默认选最左；"
                                 "先用 %s。真要结束传 force=True"
                                 % (pend.get("reason") or pend, how)}
        hud = self.hud_actor()
        if not hud:
            return {"ok": False, "error": "找不到 %s 活实例（不在对局里？）" % HUD_CLASS}
        # ★ 2026-09-26（`click-crash-report.md` §5）：**先验证这个 HUD 属于当前这一局**。
        #   `hud_actor()` 在拿不到 `GetBoard()→BattleHUD` 链时会退回扫实例，历史上真点到过
        #   **上一局的 HUD**（handoff §872-879）；而 `end_of_turn` 是裸句柄调用，
        #   打到已销毁对象上就是 `access violation`（`0x8a53` 那次的疑似来源）。
        #   ⇒ 归属验证不过就**报错不点**，绝不赌。
        if not self.owned_by_current_match(hud):
            return {"ok": False, "error": "HUD(0x%X) 不属于当前这一局（疑似上一局残留）——拒绝点击"
                                          % hud}
        btn = self.end_turn_button(hud)
        if not btn:
            return {"ok": False, "error": "HUD 的 EndTurnButton 指针为空"}
        hud_cls = self.uclass_of_instance(hud)
        btn_cls = self.uclass_of_instance(btn)

        mk = self._mark()
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature"))
        self.settle(0.25, 6)
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature"))
        self.settle(0.08, 4)          # 按下态要真的渲染出来：按帧数等（失焦降帧时 0.08 s 可能一帧没画）
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature"))
        self.settle(0.05, 3)
        self.call0(btn, self.fn(btn_cls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature"))
        self.call0(hud, self.fn(hud_cls, "BndEvt__EndTurnButton_K2Node_ComponentBoundEvent_249_OnButtonClickedEvent__DelegateSignature"))
        if verbose:
            print("  结束回合: hover→press→release→click(self)→click(HUD)")
        r = self._receipt(mk, "XActionEndOfTurn", timeout=3.0)
        if not r.get("ok"):
            r["game_hints"] = self.notify_texts()
        return r

    def wait_our_turn(self, limit: float = 300.0, verbose: bool = True, poll: float = 1.0) -> bool:
        """**只读**等我方回合（`ops.py::wait_our_turn` 同源）——不动内存、不动输入。

        每 `1.0s` 采一次快照，只在 `(turn, our_turn, kredits, match_finished)` 变化时
        打一行（避免刷屏）；`match_finished` → False（对局结束，等不到）；`our_turn` → True。
        """
        from kardsmem import board as BA
        t0 = time.time()
        last = None
        while time.time() - t0 < limit:
            st = BA.open_source("mem").snapshot()
            key = (st.turn, st.our_turn, (st.kredits or {}).get(st.my_side), st.match_finished)
            if verbose and key != last:
                print("  t=%.0fs turn=%s our=%s k=%s fin=%s"
                      % (time.time() - t0, st.turn, st.our_turn,
                         (st.kredits or {}).get(st.my_side), st.match_finished))
                last = key
            if st.match_finished:
                return False
            if st.our_turn:
                return True
            rem = limit - (time.time() - t0)
            if rem <= 0:
                break
            time.sleep(max(0.0, min(poll, rem)))        # poll 默认 1.0（旧值）；回路轮询用更短值
        return False

    def play_card(self, card_id: int, target_id: Optional[int] = None,
                  verbose: bool = True, hover_pause: Optional[float] = None,
                  force: bool = False, also_mouse_up: bool = False,
                  location: str = "back", slot: Optional[int] = None,
                  require_inactive: bool = False) -> dict:
        """打出一张手牌。事件卡/单位卡自动分流，落点都是真实合法的空位。

        `slot`（★ 2026-09-27 新增）：**单位部署的站位** = 支承线内的 `locationNumber`。
        不给就自己挑一个空槽（`_free_slot()`）。合法域只有**我方支承线/后排**
        （`LocationUnderCursor=LOC_BOARD_HQLEFT`）—— 实测游戏**不接受直接部署到前线**
        （见下面那段 2026-09-26 的结论），所以站位参数不表达"放哪一行"，
        只表达"这一行里的第几格"。**站位有意义**：插在两个单位之间会改变名次
        （相邻关系/名次类效果看它）。
        `require_inactive`：只对**反制**有意义 —— True 时"已经激活的反制"会被拒绝
        （默认 False = 允许重复打出，也就是**切换**，见下面那段）。

        `location`：单位卡放哪一行 —— `"back"`（我方支援线，默认）/ `"front"`（我方前线）。
        ★★ 2026-09-26 实测结论（用户确认"**游戏规则如此**"）：**游戏不接受"直接部署到前线"**。
          我们按真实鼠标会产生的落点写了 `LocationUnderCursor=7`（Board_Frontline）+
          `RowUnderCursor=1`，`drag_success=1`，但游戏仍把单位放进**支援线**
          （实测 8/8 的 5th RANGERS 落在 `back`）⇒ 上前线**只能**走 `move_to_front`
          （而移动要付操作费、且步兵移动后本回合不能再攻击）。
          这个参数保留有两个用处：① 把"这条路被堵死"这件事钉在代码里（谁再想直接摆前线，
          一眼就能看到结论和证据）；② 万一将来规则的口径变了，这里是现成的复现入口。

        ★★ 2026-09-25 修（用户实机指出："指挥点不够应该直接 stop，别往下走"）：
          旧版只问了这张卡**自己的** `CanPlayFromHand`——而它**不查指挥点**
          （见 `game_can_play_from_hand` 的注释：0 指挥点时 cost 2/3 的牌照样 canIt=1）。
          于是"打不起的牌"会一路走到拖拽/落地，游戏再拒绝一次，屏幕上弹一条
          "指挥点数不足"，调用方还拿不到"为什么没打出去"。现在先问**总闸**
          `BP_Logic_C::CanPlayCardFromHand`（`precheck_play()`：含指挥点、支援线满、
          全局限制、卡自身覆写、指向），它说不行就**当场返回**，不拖、不发事件。
          总闸只回 bool，所以再用卡自己的 `CanPlayFromHand` 下钻取理由（指挥点那条
          由 `precheck_play` 归因）。**注意这仍然只是"预检"**：真打时照样走完整拖拽，
          最终裁决权在游戏（§7.6f）。
        """
        _T0 = time.time()
        r = self.hand_actor(card_id)
        if not r:
            # ★ 2026-10-04：报错里带上**诊断**（弯路 #44）—— 两种读法各自看到哪些 card_id。
            #   起因：一局 4 次"不在手牌"其实都是**横幅期瞬时假阴性**（同一张卡几秒后就打出成功），
            #   光看错误文本分不清"候选陈旧 / 手牌控件重建窗口"，所以把两组 id 一起报出来。
            d = self.hand_actor_diag(card_id)
            return {"ok": False, "reason": "not_in_hand",
                    "error": "card %s 不在我方手牌（扫场读到 %s；Deck 权威账本读到 %s）"
                             % (card_id, d["scan"], d["ledger"]), "diag": d}
        card, _st = self._card(card_id)
        # ★★ 2026-09-27 **反制改成"可重复打出"**（用户口径："反制可以被多次打出（切换是否激活）"）。
        #   以前这里在 `gotchaActivated > 0` 时**硬拒绝** —— 那是为"别误取消激活"加的护栏，
        #   但它把**切换**这个正当语义一起挡了。现在：**放行**，但把切换结果如实报出来
        #   （`gotcha_activated_before/after` + `toggled_off`），免得调用方把"打两次 = 关掉"
        #   读成"第一次失败了"。要"只在没激活时打"的老行为传 `require_inactive=True`。
        if (card is not None and (card.card_type or "") == "gotcha"
                and (getattr(card, "gotcha_activated", 0) or 0) > 0):
            if require_inactive:
                return {"ok": False, "reason": "gotcha_already_activated",
                        "card": card.name, "gotcha_activated": card.obj.gotchaActivated,
                        "error": "这张反制**已经激活**（gotchaActivated=%s），"
                                 "而调用方要求 require_inactive=True" % card.obj.gotchaActivated}
        pc = self.precheck_play(card_id, verbose=verbose)
        if pc.get("ok") and pc.get("can") is False:
            return {"ok": False, "reason": pc.get("reason"), "gate": "BP_Logic::CanPlayCardFromHand",
                    "error": "游戏自己的总闸 CanPlayCardFromHand 说不行（reason=%r）"
                             % pc.get("reason"),
                    "precheck": pc}
        # ★★ 2026-09-25 实机补的第二道：**总闸说 True 不代表真能打**。
        #   实测 `GUNSHIP MISSION`（cost 2、`needs_hand_target=True`）在"敌方支援线没人"
        #   时：总闸 `CanPlayCardFromHand`=**True**，而卡自己的 `CanPlayFromHand`=
        #   `False reason='enemy_in_supply_line'`（它要一个敌方支援线单位当目标）。
        #   ⇒ 总闸内部那道 `CanSelectAsTarget` 在"当前没有箭头"时不能替代卡自己的覆写。
        #   所以**卡自己的覆写永远要问**（它是"为什么"的权威来源），只是别拿它当
        #   "缺目标"的否决：指向类在预检阶段没有箭头，必然报"要目标"。
        gp = self.game_can_play_from_hand(card_id, verbose=verbose)
        if gp.get("ok") and gp.get("can") is False:
            needs_t = bool(getattr(card, "needs_hand_target", False))
            reason = gp.get("reason")
            is_target_kind = needs_t or target_id is not None or reason in NEEDS_TARGET_REASONS
            if target_id is not None:
                # 给了目标 ⇒ 走**带目标出牌**那条路（复刻"拖到目标上松手"）
                return self.play_card_targeted(card_id, int(target_id), verbose=verbose,
                                               hover_pause=hover_pause,
                                               also_mouse_up=also_mouse_up)
            if is_target_kind:
                if not force:
                    # 明确停手：盲拖只会静默失败（动作流零新增），调用方还看不出原因
                    return {"ok": False, "reason": "needs_target", "game_reason": reason,
                            "card": card.name if card else None, "game": gp,
                            "error": "这张牌要指向目标（游戏自己的 reason=%r）——"
                                     "用 play_card_targeted(card_id, target_id)，"
                                     "或 force=True 先真拖一次（看游戏会不会自己开第二阶段）"
                                     % reason}
                # ★ `force=True`：**不拿"要目标"这条判据否决动作**（§7.6f）。
                #   典型场景：「部署型指向」（327th PATHFINDERS "部署：对 1 个单位造成 2 点伤害"）
                #   —— 预检阶段没有箭头/没有已选目标，卡自己的覆写必然回"要目标"，
                #   但那不代表拖不出去：**单位先落地，然后游戏进入"点选目标"状态**
                #   （用户给的旧 ops 流程：部署后需要点选目标；点到非法位置 ⇒ 卡回手牌；
                #    悬停在不符合要求的卡上会显示 reason）。
                #   ⇒ force 之后就**落到普通拖拽**，让游戏自己开那个状态。
                if verbose:
                    print("    ⚠ force：卡覆写说 needs_target（reason=%r）仍按**普通拖拽**发出去"
                          "（单位应落地并进入点选目标）" % reason)
            else:
                # 非"要目标"类的拒绝 ⇒ 以卡自己的覆写为准（它比总闸细）
                return {"ok": False, "reason": reason, "game": gp,
                        "error": "游戏自己的 CanPlayFromHand 说不行（reason=%r）" % reason}
        elif not pc.get("ok") and gp.get("ok") and gp.get("can") is False:
            return {"ok": False, "reason": gp.get("reason"), "game": gp,
                    "error": "游戏自己的 CanPlayFromHand 说不行（reason=%r）" % gp.get("reason")}

        # ★★ 2026-09-25 22:2x 的事故换来的硬闸门（**先读闸门，读不出来就别动手**）：
        #   那次 `precheck_play(27)` 返回了 `can=None`（= 注入调用**抛异常**，`agent/precheck`
        #   的 `_call` 丢掉重连一次还是失败），紧接着 **frida-agent.dll 在游戏进程里 fail-fast**
        #   （Windows 事件日志：出错模块 frida-agent.dll、异常 0xc0000409/BEX64），游戏进程直接没了。
        #   ⇒ 两道闸门**都读不出来**，说明注入侧已经不健康；此时拖拽/点击是在
        #     "连查询都失败"的进程里继续写状态，风险与收益完全不成比例。
        #   所以：读不出来就当**拒绝**（不是"不确定所以放行"）。宁可少打一张牌，也不把对局打崩。
        if not pc.get("ok") and not gp.get("ok"):
            return {"ok": False, "reason": "gate_unreadable", "gate": "both",
                    "error": "两道闸门（BP_Logic::CanPlayCardFromHand / 卡的 CanPlayFromHand）"
                             "都读不出来 ⇒ 注入侧可能已不健康，拒绝动手。"
                             "precheck=%r game=%r" % (pc.get("stopped"), gp.get("stopped"))}

        loc_enum = self.our_front_enum() if location == "front" else self.our_back_enum()
        place = None
        if card is not None and card.card_type in self.UNIT_TYPES:
            # ★ 2026-09-27：`slot` = **空隙序号**（0=最左 … n=最右，见 `gap_number` 的推导）。
            #   给了空隙就问游戏"拖到那儿该写第几号"；没给才退回"第一个空列号"（老行为）。
            place = self.deploy_location_number(
                None if slot is None else int(slot),
                row=("front" if location == "front" else "back"), verbose=verbose)
            loc_num = place.get("requested")
            if loc_num is None:
                return {"ok": False,
                        "error": ("我方前线满（最多 5 个单位），单位打不出去" if location == "front"
                                  else "我方支援线满（4 个单位 + 总部），单位打不出去")}
        else:
            # 非单位卡不看落点（Claude 的骄阳那次已证：这条分支只要求 RowUnderCursor 为真）
            loc_num = 0
        gotcha_before = (getattr(card, "gotcha_activated", 0) if card is not None else 0)
        _T1 = time.time()
        mk = self._mark()
        out = self._drag_lifecycle(r["actor"], loc_enum, loc_num,
                                   card_at_location=int((place or {}).get("card_at_location") or 0),
                                   verbose=verbose,
                                   hover_pause=(SETTLE_HOVER_PLAY if hover_pause is None
                                                else hover_pause),
                                   also_mouse_up=also_mouse_up)
        if place is not None:
            out["placement"] = {k: v for k, v in place.items() if k != "row_cards"}
        _T2 = time.time()
        # ★ 2026-10-01 用户："抉择部署的回执应该把抉择时间算在内"：带抉择的部署（5th RANGERS 一类），
        #   选项面板一弹出就**立刻返回**（别在部署这一步干等 5 s+ 的动作流），成交回执留给 `pick_choice`
        #   （它认 `XActionPlayCardFromHand{chooseOneIndex>=0}`）。只在面板真的 pending 时才早退。
        def _choice_panel_up():
            try:
                return bool(self.pick_pending().get("pending"))
            except Exception:                                        # noqa: BLE001
                return False
        outc = self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0,
                             awaiting_probe=_choice_panel_up)
        if outc.get("awaiting_target") and not outc.get("ok"):
            outc = dict(outc, ok=True, awaiting_choice=True,
                        note_choice="选项面板已弹出：成交回执由 pick_choice 确认")
        out.update(outc)
        out["timing"] = {"pre": round(_T1 - _T0, 2), "drag": round(_T2 - _T1, 2),
                         "receipt": round(time.time() - _T2, 2)}
        # 反制的切换：如实报 before/after（`gotchaActivated` 0↔N），别让调用方靠猜
        if card is not None and (card.card_type or "") == "gotcha":
            try:
                _c2, _ = self._card(card_id)
                after = (getattr(_c2, "gotcha_activated", None) if _c2 is not None else None)
            except Exception:                                    # noqa: BLE001
                after = None
            out["gotcha_activated_before"] = gotcha_before
            out["gotcha_activated_after"] = after
            out["toggled_off"] = bool(gotcha_before and after == 0)
        if verbose:
            print("    play card_id=%s -> 动作流回执 ok=%s" % (card_id, outc.get("ok")))
        return out

    def play_card_targeted(self, card_id: int, target_id: int, verbose: bool = True,
                           hover_pause: Optional[float] = None,
                           also_mouse_up: bool = False,
                           slot: Optional[int] = None) -> dict:
        """**带目标出牌**：把手牌拖到目标卡上松手（复刻真实鼠标序列）。

        顺序（跟攻击那条同一套纪律，2026-09-25 实现）：
          悬停手牌 → 悬停转发 → 按下 → 起拖 → 拖动 tick（箭头由游戏自己生成）
          → **悬停目标卡**（目标 actor 收到 `OnActorMouseEnter`，同样转发给对手）
          → 给场上每个箭头写 `overCardID=目标卡id` + 松手（`OnActorEndDrag`），
            写与松手在**同一次 JS 执行**里
        为什么这么写（两条都是实机踩出来的）：
          * `BP_Logic_C::GetTargetArrowTargetCard` 取 `GetAllActorsOfClass(箭头)[0]->overCardID`
            ⇒ 只写"自己认的那个箭头"不够，要**每个箭头都写**；
          * 攻击上次失败十几次的根因就是**漏了"悬停目标 actor"这一步** —— 补上后第一下就成。
        纪律：先问游戏（`precheck_play` 总闸 + `CanSelectAsTarget` 指向判据），说不行就返回、不拖。

        ⚠ 2026-09-25 更正：这条路只覆盖**指令卡**那种"拖到落点松手就成交"的带目标出牌
          （`selectTargetOnPlayedFromHand=false` ⇒ 松手时 `PlaceHandCard(LocationNumberUnderCursor,
          targetCardID)` 直接成交，GUNSHIP MISSION 实测过）。
          **单位卡且"有目标"是两阶段的**：松手那一下只做**视觉落位 + 生成箭头**，
          不成交 ⇒ 必须接着 `select_unit_target(card_id, 目标)` 去**点目标**
          （`BP_Logic::GlobalMouseUp`）。本函数会读 `cardBeingPlayedFromHand` 并回
          `two_stage_needed=True` 提示这一点。

        ★ 2026-09-27 补一条（把上面那句话的边界说全）：**`selectTargetOnPlayedFromHand=True`
          的指令也是一次成交** —— 松手时 `BP_HandCard.cpp:1204` 因为 `!IsUnit` 直接走
          `Label_15862 → AttemptToPlayFinal`，而 `BP_PlayerMoves.cpp:2183-2198` 读的是
          卡自己的 `CanPlayFromHand` 出参（← `GetTargetedCard` ← 我们写进去的 `targetOverride`）
          ⇒ 目标有了就 `PlaceHandCard(...)` 成交。实机：`FOR FREEDOM`(手牌4) → `5th RANGERS`(24)
          = `XActionPlayCardFromHand{4, target=24}` + `ZActionGainAttack/Defense` + 抽牌。
          ⇒ 语义分工：**指令**用 `play_card_event_with_target()`（一次），**单位**用
          `play_card_unit_with_target()`（两阶段）。
        """
        rec = self.hand_actor(card_id)
        if not rec:
            d = self.hand_actor_diag(card_id)
            return {"ok": False, "reason": "not_in_hand",
                    "error": "card %s 不在我方手牌（扫场读到 %s；Deck 权威账本读到 %s）"
                             % (card_id, d["scan"], d["ledger"]), "diag": d}
        pc_gate = self.precheck_play(card_id, verbose=verbose)
        if pc_gate.get("ok") and pc_gate.get("can") is False:
            return {"ok": False, "reason": pc_gate.get("reason"),
                    "error": "总闸 CanPlayCardFromHand 说不行（reason=%r）" % pc_gate.get("reason")}
        sel = self.game_can_select_as_target(target_id, card_id, True, verbose=verbose)
        if sel.get("ok") and sel.get("can") is False:
            return {"ok": False, "reason": "target_rejected", "select_as_target": sel,
                    "error": "游戏自己的 CanSelectAsTarget 说这个目标不行（reason=%r）"
                             % sel.get("reason")}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.fn(cls, "OnActorMouseEnter")
        f_down = self.fn(cls, "OnActorMouseDown")
        f_start = self.fn(cls, "OnActorStartDrag")
        f_tick = self.fn(cls, "OnActorDragTick")
        f_end = self.find_fn(cls, "OnActorEndDrag") or self.find_fn(cls, "OnActorMouseUp")
        f_disp = self.fn(pc_cls, "MouseHoverDispatch")
        self.call0(actor, f_enter)
        # ★ 出牌的手牌悬停 = "鼠标经过这张牌然后拖出去"，不是刻意读它 ⇒ 用短档
        #   （用户 2026-09-26："打出牌的 hover 可以降得更短"）。悬停存在、转发照发（信息流不变）。
        self.settle(SETTLE_HOVER_PLAY if hover_pause is None else hover_pause, 6)  # 悬停停留按帧等，降帧时也保证悬停被处理
        self.call_ptr(pc, f_disp, actor)                      # 手牌悬停转发
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        self.call0(actor, f_down)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call_ptr(actor, f_start, 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, f_tick)                             # 箭头在这一步生成/更新
        self.settle(SETTLE_AFTER_TICK, 2)
        # 悬停**目标**卡（真实玩家把鼠标拖到目标上，目标 actor 会收到 enter）
        out = {"commit_fn": "OnActorEndDrag" if self.find_fn(cls, "OnActorEndDrag") else "OnActorMouseUp"}
        tgt_actor = self.board_actor_of(target_id)
        if tgt_actor:
            f_te = self.find_fn(self.uclass_of_instance(tgt_actor), "OnActorMouseEnter")
            if f_te:
                self.call0(tgt_actor, f_te)
                self.settle(SETTLE_HOVER, 6)
            self.call_ptr(pc, f_disp, tgt_actor)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        out["target_actor"] = hex(tgt_actor) if tgt_actor else None
        out["target_by_logic_before"] = self.arrow_target_by_logic()
        arrows = self.arrow_actors()
        out["arrows"] = len(arrows)
        # ★★ 2026-09-25 **这才是"带目标出牌"的关键**（前面写箭头 overCardID 是白写）：
        #   `BP_CardFunctions_C::GetTargetedCard(callerCard, hasTarget, card)`
        #   （1.58 导出 `BP_CardFunctions.cpp:283-324`）读的是**卡对象自己的字段**：
        #       callerCard->targetOverride（有 ⇒ 就是它）
        #       callerCard->currentTarget（1.60 的 dump 里没有这个字段，只有 targetOverride）
        #       两者都没有 ⇒ hasTarget=false
        #   ★ 而**游戏自己就是用这个字段做探测的**：`BP_HandCard.cpp:4783`
        #       fromCard->targetOverride = currentCard;   // 先假设目标是它
        #       fromCard->CanPlayFromHand(...)            // 再问行不行
        #       currentCard->targetOverride = nullptr;     // 问完清掉
        #     所以我们写这一格**不是编造状态**，是复刻游戏自己的探测/命中写法。
        #   ⇒ 落地时：把"正在打的那张卡的 card 对象"的 `targetOverride` 指到目标的 card 对象。
        #     问完**还原**（游戏自己也会清），否则会漏到后面的动作里。
        src_obj = self._card_object(card_id)
        tgt_obj = self._card_object(target_id)
        off_to = 0
        if src_obj:
            ocls = self.uclass_of_instance(src_obj)
            off_to = self.off(ocls, "targetOverride", NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
            out["targetOverride_off"] = hex(off_to)
        mk = self._mark()
        # ★★ 2026-09-25 修（就是 327th 那次"带目标出牌没反应"的原因）：
        #   **落点也要写**。`play_card`（普通出牌）会写 `cardUnderCursor /
        #   LocationNumberUnderCursor / LocationUnderCursor / RowUnderCursor`，
        #   而这条"带目标"的路之前**只写了 targetOverride** ⇒
        #   单位卡在 `AttemptToPlayFinal` 里目标有了、**落点却没有** ⇒ 不成交。
        #   订单卡（GUNSHIP MISSION）不需要落点，所以那次一次就成了 —— 差别就在这里。
        card, _st = self._card(card_id)          # 落点规则要用它（单位 vs 指令）
        loc_enum, loc_num, place = self._target_play_location(card, slot=slot)
        off_cuc = self.off(cls, "cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR)
        off_ln = self.off(cls, "LocationNumberUnderCursor", NOTE_OFF_LOCATION_NUMBER)
        off_le = self.off(cls, "LocationUnderCursor", NOTE_OFF_LOCATION_ENUM)
        off_r = self.off(cls, "RowUnderCursor", NOTE_OFF_ROW_UNDER_CURSOR)
        writes = [(actor, off_cuc, "s32", 0),
                  (actor, off_ln, "s32", int(loc_num)),
                  (actor, off_le, "u8", int(loc_enum)),
                  (actor, off_r, "u8", 1)]
        out["location"] = {"enum": loc_enum, "number": loc_num,
                           "gap": place.get("gap"), "source": place.get("source"),
                           "row_numbers": place.get("row_numbers")}
        writes += [(a, self.off(self.uclass_of_instance(a), "overCardID",
                                NOTE_OFF_ARROW_OVER_CARD_ID), "s32", int(target_id))
                   for a in arrows]
        if src_obj and tgt_obj:
            writes.append((src_obj, off_to, "ptr", tgt_obj))
        else:
            out["target_override_note"] = "拿不到 src/tgt 的 UBaseCardObject，只能写箭头"
        if writes:
            out["writes"] = len(writes)
            try:
                if also_mouse_up:
                    # 两个松手口都发（同一次 JS 执行）—— 走 L1 原语 `primitives.write_and_call2`
                    f_up3 = self.find_fn(cls, "OnActorMouseUp")
                    out["commit_both"] = P.write_and_call2(
                        self.api(), writes, actor, f_up3 or f_end, actor, f_end)
                    out["commit_fn"] = "OnActorMouseUp+OnActorEndDrag"
                else:
                    out["writeback"] = self.write_and_call(writes, actor, f_end)
            except Exception as e:                                   # noqa: BLE001
                out["write_error"] = str(e)
        else:
            self.call0(actor, f_end)
        # ★ 阶段一（部署型指向）：单位会停在"待点目标"态、**按设计不会有**这个动作
        #   ⇒ `awaiting_probe` 一成立就早退，不白等满 3 s（实测省 ~3 s/次）。
        out.update(self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0,
                                 awaiting_probe=lambda: bool(self.card_being_played_from_hand())))
        # 还原 `targetOverride`（别把探测状态漏下去）
        if src_obj and tgt_obj:
            try:
                self.poke(src_obj, off_to, "ptr", 0)
                out["target_override_cleared"] = (self.m.ptr(src_obj + off_to) or 0) == 0
            except Exception as e:                                   # noqa: BLE001
                out["clear_error"] = str(e)
        # ★ 2026-09-25：**单位卡**（`IsUnit && DoesThisCardHasAnyTarget`）在阶段一只会被
        #   视觉落位 + 生成箭头，**不会**在这里成交（导出 `BP_HandCard.cpp:1198-1217`，
        #   走的是 Label_14483 而不是 Label_15772）⇒ 没有动作流回执是**正常的**，
        #   这时必须接着走第二阶段（`select_unit_target` 点目标）。
        #   指令卡（`selectTargetOnPlayedFromHand=false`）才会在这一步直接
        #   `PlaceHandCard(LocationNumberUnderCursor, targetCardID)` 成交。
        pending_after = self.card_being_played_from_hand()
        out["playing_after"] = pending_after
        if pending_after:
            out["two_stage_needed"] = True
            out["next"] = "select_unit_target(%s, 目标)" % card_id
        if verbose:
            print("    带目标出牌 card=%s -> target=%s 箭头=%s targetOverride=%s 回执 ok=%s%s"
                  % (card_id, target_id, out.get("arrows"), out.get("targetOverride_off"),
                     out.get("ok"),
                     "（单位留在待选目标态 ⇒ 接着 confirm 点目标）"
                     if out.get("two_stage_needed") else ""))
        return out

    def select_target(self, target_card_id: int, verbose: bool = True) -> dict:
        """**点一下目标卡**完成"带目标出牌"（正在打的那张牌从游戏自己的字段读）。

        ★★ 2026-09-25 整条重写（用户纠正"选择目标不是 Drag"）。旧实现是
          「写箭头 `overCardID` → 目标 actor 的 `OnActorMouseDown` + `OnActorMouseUp`」
          —— 两个都不对：
            * 板卡 `OnActorMouseUp`（`BP_BoardCard` entry 17498）那条链是"拖**板卡**"
              的提交（里面是 `PlaceBoardCard()`、`ArrangeCardsByLocation`），跟手牌
              带目标出牌不是一条路；
            * 真正的提交是 `BP_Logic::GlobalMouseUp` → `GlobalMouseUpBattle`
              （真人在这种状态下点目标时，PC 因 `consumed=true` **根本不会**把鼠标
              松开转发给目标 actor）。
        现在这里只是 `select_unit_target` 的薄封装：卡号取自
        `BP_Logic->cardBeingPlayedFromHand`（阶段一写上的那个，权威），不给就不点。
        """
        card_id = self.card_being_played_from_hand()
        if not card_id:
            return {"ok": False, "stopped": "BP_Logic->cardBeingPlayedFromHand=0 ⇒ "
                                           "现在不是'等选目标'状态（阶段一没成立）"}
        return self.select_unit_target(int(card_id), target_card_id, verbose=verbose)

    def select_unit_target(self, card_id: int, target_id: int,
                           verbose: bool = True,
                           press: bool = True,
                           fallback_up: bool = False) -> dict:
        """**点选一个场上单位作为目标**（提交口 `BP_Logic::GlobalMouseUp`）。★ 语义**不是**"部署"。

        ★★ 2026-09-27 改名 + 语义澄清（用户 2026-09-26 定调：
           "语义上，那个不是部署。虽然好像交互逻辑类似。"）
          这条链**唯一**在做的事是"**指向一个单位**"：`GlobalMouseUp` 读
          `BP_Logic->cardBeingPlayedFromHand` + 箭头 `overCardID` + 卡的 `targetOverride`
          ⇒ `PlaceHandCard(cardBeingPlayedFromHandLocNum, targetCardID)`。
          谁走到这个"待点目标"态**不重要**，两条**语义不同**的来源都汇到这里：
            * **单位**（`selectTargetOnPlayedFromHand=True` 且 `IsUnit`）：
              阶段一（松手）只做视觉落位 + 生成箭头，**必须再点一次目标** ⇒ 整套叫
              `play_card_unit_with_target()`（"部署一个单位并指向"）；
            * **指令/效果**（例：HIDDEN PLANS 选项 "Reveal a Covert unit and give it +1+1."）：
              选项层 resolve 之后游戏把这张**指令**也挂进同一个态 ⇒ 语义是"指向"、不是"部署"。
          所以动词按**语义**叫 `select_unit_target`；旧名 `confirm_deploy_target`
          已于 2026-09-27 **删除**（用户定调："旧名无需保留"）——不留兼容别名，
          免得下一个接手的人又把"部署"当成这条链的全部语义。

        ★★ 2026-09-25 用户当场纠正（"部署时是 EndDrag，选择目标不是 Drag 为什么 EndDrag"）
           + 逐句核实 1.58 导出之后**整条重写**。旧实现发的是
           `OnActorEndDrag`（在"正在打的那张手牌"上），那是**阶段一"部署"的口**，错了。

        真实链路（一手证据：1.58 导出逐句读过）：
          ① 阶段一 `BP_HandCard::OnActorEndDrag`（导出 :1198-1217）对**有目标**的单位
             ⇒ `logic->cardBeingPlayedFromHand = cardID`、
                `logic->cardBeingPlayedFromHandLocNum = 落点`，
                只做**视觉落位 + 生成箭头**（`BP_targetArrowRVX_C`），状态**不清**；
          ② 阶段二 = 真人在目标卡上**单击一次**。PC 的鼠标松开处理器
             （`BP_PlayerController.cpp:683` 一带）做的是：
                `logic->GlobalMouseUp(mouseDownActor, &consumed)`
                `if (consumed) return;`          ← 被吃掉就**不再**转发给 actor
                …→ `mouseDownActor->OnActorMouseUp()`
             ⇒ 箭头举起 + 有目标时 `GlobalMouseUp` 会**吃掉**这次点击：
               `BP_Logic::GlobalMouseUpBattle`（导出 :9297-9376）：
                 · `IsTargetArrowActive()` && `GetTargetArrowTargetCard()` 有目标
                   （读 `箭头->overCardID`，:7051）
                 · `cardBeingPlayedFromHand == 箭头->fromCard->cardID`（:9324）
                 · 卡自己的 `CanPlayFromHand` 出参 `targetedCard` **有效**
                   （`targetedCard` ← `GetTargetedCard` ← `卡对象->targetOverride`；
                    真鼠标那条路上是 `BP_targetArrowRVX::ReceiveTick` **每帧**按鼠标下
                    那张卡写的 —— 导出 `BP_targetArrowRVX.cpp:5883` 那条
                    `fromCard->targetOverride = tmpCurrentCard`）
                 · 再 `CanSelectAsTarget(_targetCard, 卡, true, ...)`
                 ⇒ `PlaceHandCard(cardBeingPlayedFromHandLocNum, targetedCard->cardID)`
                   + `cardBeingPlayedFromHand = 0` + `RemoveTargetArrow()`（:9347-9361）
        所以这里只做三件事：
          1) 悬停目标（`OnActorMouseEnter` + `MouseHoverDispatch`；**这一步会转发给对手**）
          2) 按下那一半（`MouseDownActor->OnActorMouseDown()`，PC 按下时就是这么调的；
             板卡自己的按下流开头就是"手牌正在选目标 ⇒ 直接 return"，不会干扰）
          3) **同一次 JS 执行**：写「每个箭头 `overCardID` + 卡对象 `targetOverride`」
             → 调 `logic->GlobalMouseUp(目标actor, &consumed)`
             （必须同一次：箭头 tick 每帧按真实鼠标重算这些字段，分两次 RPC 就被清。）
        ★ 不要发：`OnActorEndDrag`（阶段一的口）；**目标板卡**的 `OnActorMouseUp`
          —— `BP_BoardCard::OnActorMouseUp`（:1335-1425）那条链是"拖**板卡**"的提交
          （里面是 `PlaceBoardCard()`），跟手牌带目标出牌不是一条路；而且真人在这种
          状态下点目标时，PC 因为 `consumed=true` 根本**不会**调 target 的 MouseUp。
        回执：动作流 `XActionPlayCardFromHand`（带 `targetCardID`=目标）。
        """
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        f_gmu = self.find_fn(lcls, "GlobalMouseUp") or self.find_fn(lcls, "GlobalMouseUpBattle")
        if not f_gmu:
            return {"ok": False, "stopped": "BP_Logic_C 上没有 GlobalMouseUp/GlobalMouseUpBattle"}
        playing = self.card_being_played_from_hand()
        loc = self.card_being_played_loc()
        arrows = self.arrow_actors()
        out = {"playing_from_hand": playing, "loc_num": loc, "arrows": len(arrows),
               "selecting_hand_target": self.selecting_hand_target(),
               "call": "GlobalMouseUp"}
        if playing and int(playing) != int(card_id):
            out.update(ok=False,
                       stopped="BP_Logic->cardBeingPlayedFromHand=%s 不是 %s ⇒ 现在等的不是这张牌"
                               % (playing, card_id))
            return out
        if not arrows:
            out.update(ok=False, stopped="场上没有箭头 ⇒ 阶段一没成立，无从点目标")
            return out
        tgt_actor = self.board_actor_of(target_id)
        if not tgt_actor:
            out.update(ok=False, stopped="场上找不到目标 card %s 的 actor" % target_id)
            return out
        out["target_actor"] = hex(tgt_actor)
        src_obj = self._card_object(card_id)
        tgt_obj = self._card_object(target_id)
        if not src_obj or not tgt_obj:
            out.update(ok=False, stopped="拿不到 src/tgt 的 UBaseCardObject")
            return out
        off_to = self.off(self.uclass_of_instance(src_obj), "targetOverride",
                          NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
        # ---- 1) 悬停（真人点击前一定先悬停；**这一步会转发给对手**）----
        tcls = self.uclass_of_instance(tgt_actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(tcls, "OnActorMouseEnter")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        f_down = self.find_fn(tcls, "OnActorMouseDown")
        out["send"] = {"enter": bool(f_enter), "hover_dispatch": bool(f_disp),
                       "down": bool(f_down)}
        if f_enter:
            self.call0(tgt_actor, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, tgt_actor)       # 悬停转发（对手看得到的那一步）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ---- 2) 按下那一半（PC 在鼠标按下时调的就是这个）----
        if press and f_down:
            self.call0(tgt_actor, f_down)
            self.settle(SETTLE_AFTER_DOWN, 2)
        # ---- 3) 写 + GlobalMouseUp，**同一次 JS 执行** ----
        #   箭头那几项标 `hold=True`（不还原）：`GlobalMouseUpBattle` 内部会
        #   `DestroyAllActorsOfClass(箭头)`，不该再去写可能已回收的内存；
        #   卡对象那项必须还原（那是为复刻悬停临时写的，游戏自己也是探测完就清）。
        writes = [(src_obj, off_to, "ptr", tgt_obj)]
        for a in arrows:
            writes.append((a, self.off(self.uclass_of_instance(a), "overCardID",
                                       NOTE_OFF_ARROW_OVER_CARD_ID), "s32", int(target_id), True))
        out["writes"] = len(writes)
        parms = bytearray(0x10)
        struct.pack_into("<Q", parms, 0x00, int(tgt_actor))     # MouseUpActor @0x00
        mk = self._mark()
        try:
            out["writeback"] = self.call_raw_writes(writes, logic, f_gmu, bytes(parms))
        except Exception as e:                                     # noqa: BLE001
            out.update(ok=False, error=str(e))
            return out
        buf = out["writeback"] or b""
        out["consumed"] = bool(buf[0x08]) if len(buf) > 0x08 else None
        out.update(self._receipt(mk, "XActionPlayCardFromHand", card_id=card_id, timeout=3.0))
        out["playing_after"] = self.card_being_played_from_hand()
        out["selecting_after"] = self.selecting_hand_target()
        out["target_override_after"] = hex(self.m.ptr(src_obj + off_to) or 0)
        # `consumed=False` ⇒ 游戏没吃这次点击。真实客户端这时才把鼠标松开转发给
        # actor（`PC: if (!consumed) …->OnActorMouseUp()`）；默认**不**补，因为那会跑
        # 目标板卡那条"拖板卡"的提交链（`PlaceBoardCard()`），只在诊断时才开。
        if fallback_up and out.get("consumed") is False:
            f_up = self.find_fn(tcls, "OnActorMouseUp")
            if f_up:
                self.call0(tgt_actor, f_up)
                out["fallback_up"] = "OnActorMouseUp"
        if verbose:
            print("    点目标 card=%s -> target=%s 箭头=%d consumed=%s 回执 ok=%s"
                  "（playing %s→%s）"
                  % (card_id, target_id, len(arrows), out.get("consumed"), out.get("ok"),
                     playing, out.get("playing_after")))
        return out

    # -------------------------------------------- 出牌：四个语义动词（2026-09-27 用户定的动作空间）
    def play_card_event(self, card_id: int, force: bool = False, verbose: bool = True,
                        require_inactive: bool = False) -> dict:
        """**打出指令/反制**（无目标）。

        * **指令**：拖到落点松手即成交（`AttemptToPlayFinal`）。
        * **反制（gotcha）**："打出"就是**在手里激活**（`XActionPlayCardFromHand`
          `location=Hand_Left` + 扣费，卡留手里、`gotchaActivated>0`）。
          ★ 用户口径（2026-09-27）："反制可以被多次打出（**切换**是否激活）" ⇒ 本函数
          **默认允许重复打出**，并回 `gotcha_activated_before/after` + `toggled_off`
          把"这次是激活还是取消"说清楚；要"只在未激活时打"传 `require_inactive=True`。
        """
        return self.play_card(card_id, force=force, verbose=verbose,
                              require_inactive=require_inactive)

    def play_card_event_with_target(self, card_id: int, target_id: int,
                                    verbose: bool = True) -> dict:
        """**打出指向性指令**（指令的目标在**松手那一次**成交）。

        实机配方（2026-09-27，`FOR FREEDOM` = 手牌4 → 我方 `5th RANGERS` = 场上24）：
            XActionPlayCardFromHand{cardID:4, targetCardID:24}
              ZActionPlayCardFromHand{cardID:4, location:Discard, targetCardID:24}
              ZActionGainAttack {cardID:24, +1 → 9, permBuff, instigator:4}
              ZActionGainDefense{cardID:24, +1 → 4, permBuff, instigator:4}
              ZActionDrawCardFromDeck{cardID:33}
        为什么**一次就成交、没有第二阶段**（一手证据，逐句读过）：
          `selectTargetOnPlayedFromHand=True` 的**指令**在松手那一次走
          `BP_HandCard.cpp:1204`（`!IsUnit` ⇒ `Label_15862`）→ `BP_PlayerMoves::AttemptToPlayFinal`
          （`:2183-2198`：读卡自己 `CanPlayFromHand` 的出参 `targetedCard` ←
          `BP_CardFunctions::GetTargetedCard` ← `卡对象->targetOverride`；给了 `targetOverride`
          就等于给了目标）⇒ `Label_15772 PlaceHandCard(LocationNumberUnderCursor, targetCardID)`。
          ⇒ **指令**不存在"待点目标"态；只有**单位**会停在那一刻等第二次点击
          （见 `play_card_unit_with_target`）。

        但**选项层之后的指令**（HIDDEN PLANS：`chooseOneCards` 非空）例外：出牌只把选项面板
        弹出来（`BP_HandCard.cpp:1191-1196` ⇒ `Label_16022`），选项 resolve 之后游戏才把
        这张卡挂进"待点目标" ⇒ 这时本函数会把 `needs_unit_click=True` 交回给调用方，
        由 `pick_choice()` + `select_unit_target()` 接着做（两步语义不同，别混成一步）。
        目标可以是**任意单位**（敌我皆可，卡面写 "Give **a unit** +1+1."）——
        合法性只问游戏自己的 `CanSelectAsTarget`（`play_card_targeted` 里那一道）。
        """
        r = self.play_card_targeted(card_id, int(target_id), verbose=verbose)
        playing = self.card_being_played_from_hand()
        r["playing_after"] = playing
        if not r.get("ok") and playing:
            r["needs_unit_click"] = int(playing)
            r["next"] = ("游戏停在'待点目标'（playing=%s）⇒ 接着 select_unit_target(%s, %s)"
                         % (playing, playing, target_id))
        return r

    def play_card_unit(self, card_id: int, slot: Optional[int] = None,
                       force: bool = False, verbose: bool = True) -> dict:
        """**打出单位**，可指定**站位**（`slot` = 该排的**空隙序号**，见 `gap_number`）。

        ★ 站位的合法域只有**我方支承线/后排**：实测游戏**不接受"直接部署到前线"**
          （`play_card(location="front")` 写 `LocationUnderCursor=7` 也照样落支承线，8/8）。
          上前线只能走 `move_to_front`（付操作费）。所以 `slot` 表达的是"这一行里第几格"，
          **不是**"放哪一行"；`slot=None` 就自动挑空槽。

        ★ 2026-09-27 用户纠正后的语义：`slot` 是**空隙序号**（"拖到谁和谁之间"）不是格子号 ——
          `0` = 最左（插到所有卡的左边）、`n` = 最右（插到所有卡的右边）；游戏再把空隙换成
          `LocationNumberUnderCursor`（= 「光标左边那些卡的最大列号 + 1」）并按它插入、
          密集重排。有空洞时"空隙序号 ≠ 最终格号"。
        """
        return self.play_card(card_id, force=force, verbose=verbose,
                              location="back", slot=slot)

    def play_card_unit_with_target(self, card_id: int, target_id: Optional[int] = None,
                                   slot: Optional[int] = None, force: bool = False,
                                   verbose: bool = True) -> dict:
        """**打出"部署时需要指向"的单位**；**没有合法目标就退化成 `play_card_unit`**。

        用户口径（2026-09-27）："如果无可指向目标，一般单位便无需指向即可打出，
        走上一路径。" ⇒ 两种情况退化：
          ① `target_id is None`；
          ② 给了目标，但**游戏自己的** `CanSelectAsTarget(目标, 这张卡, byPlayFromHand=True)`
             说不行（没有合法目标 / 目标已死 / 类型不对）⇒ 直接当普通单位打出
             （`fell_back_to_plain=True` + 原话放在 `target_rejected` 里）。
        有合法目标时：阶段一松手只做"视觉落位 + 生成箭头"，**必须再点一次目标**
        （阶段二 `select_unit_target`），并等箭头**异步**出现（实测 0.43 s）。

        为什么阶段一先试"一次性路径"（`play_card_targeted`）：指令卡（GUNSHIP MISSION /
        AIR SUPERIORITY 那类"松手即成交"）在这一步就成交；只有**单位**会停在
        "视觉落位 + 箭头"（`cardBeingPlayedFromHand` 非 0）⇒ 这时才走第二阶段。
        早先的写法（阶段一不带 target 直接 `play_card(force=True)`）对指令卡是**错的**：
        松手时 `AttemptToPlayFinal` 找不到目标 ⇒ `ClearAndRearrange` ⇒ 卡直接回手。
        ⚠ 这里不要留"看起来能越过判据、其实没读"的假参数（同类教训）。
        """
        # ---- 退化路径 ①：没给目标 ----
        if target_id is None:
            r = self.play_card_unit(card_id, slot=slot, force=force, verbose=verbose)
            r["fell_back_to_plain"] = True
            r["fallback_reason"] = "no_target_given"
            return r
        # ---- 退化路径 ②：问了游戏，它说这个目标不行 ----
        sel = self.game_can_select_as_target(int(target_id), card_id, True, verbose=False)
        if sel.get("ok") and sel.get("can") is False:
            r = self.play_card_unit(card_id, slot=slot, force=force, verbose=verbose)
            r["fell_back_to_plain"] = True
            r["fallback_reason"] = "target_rejected"
            r["target_rejected"] = {"target": target_id, "reason": sel.get("reason")}
            if verbose:
                print("    目标 %s 被游戏否掉（reason=%r）⇒ 按**无目标**打出（用户口径）"
                      % (target_id, sel.get("reason")))
            return r
        out = {"card": card_id, "target": target_id}
        r1 = self.play_card_targeted(card_id, int(target_id), verbose=verbose, slot=slot)
        out["phase1"] = {k: r1.get(k) for k in ("ok", "reason", "error")}
        out["phase1"]["ok"] = bool(r1.get("ok"))
        pending = self.card_being_played_from_hand()
        out["playing_after_phase1"] = pending
        if not pending:
            out["committed"] = "phase1（一次性：targetOverride + EndDrag）"
            out["ok"] = bool(r1.get("ok"))
            return out
        # ★ 2026-09-26 实机（108gen→59gen 那次失败）：**箭头是异步出现的** ——
        #   阶段一结束时读 `arrow_actors()` 还是 0，稍后才有 1 支；`select_unit_target`
        #   立刻去点就会报"场上没有箭头 ⇒ 阶段一没成立"。⇒ 点目标前**先等箭头出现**。
        t_wait = time.time()
        while time.time() - t_wait < 2.0 and not self.arrow_actors():
            time.sleep(0.1)
        out["arrow_waited"] = round(time.time() - t_wait, 2)
        out["arrows_after_wait"] = len(self.arrow_actors())
        r2 = self.select_unit_target(int(pending), int(target_id), verbose=verbose)
        out["phase2"] = {k: r2.get(k) for k in ("ok", "consumed", "reason", "stopped",
                                                "playing_after", "error")}
        out["committed"] = "phase2（点目标）"
        out["ok"] = bool(r2.get("ok")) and not r2.get("playing_after")
        if verbose:
            print("    两阶段 card=%s -> target=%s：一次性 ok=%s → 点目标 ok=%s consumed=%s"
                  % (card_id, target_id, r1.get("ok"), r2.get("ok"), r2.get("consumed")))
        return out

    def move_to_front(self, card_id: int, slot: Optional[int] = None,
                      verbose: bool = True, force: bool = False) -> dict:
        """**上线**：把后排单位移到前线（`XActionMoveCardToLine`）。

        移动是**单向**的（支援阵线 → 前线）；回撤只能靠"撤退"类效果。
        落点写的是"前线某空槽"——真实鼠标拖到那儿也会得到同一个值，
        合法性照样由游戏自己判（`OnActorEndDrag` 内部会再跑一遍）。
        `slot` 不给就自己挑一个空槽；**站位有意义**（插到某两个单位中间会改变名次），
        要精确站位就显式给。

        `force=True`：越过**进程外**闸门 `is_pinned`（纯读）—— §7.6f「判据只挑不判」。
        ★ 2026-09-27 起**不再问 `CanMoveCardToLocation`**：问它必须临时伪造
          `PC->SelectedCard`（手柄/拖拽态字段，`can_move_to` docstring 里有完整理由），
          而移动合不合法**游戏在提交那一刻自己判**（实机回执 `XActionMoveCardToLine`
          就是它判"可以"的结果；不行时下面会捞 `game_hints` 拿它的原话）。
        """
        rec, st = self._card(card_id)
        if rec is None:
            return {"ok": False, "error": "场上没有 card_id=%s" % card_id}
        if rec.obj.InFrontline():
            return {"ok": False, "error": "%s 已经在前线（移动是单向的）" % card_id}
        if not rec.obj.InSupportLine():
            return {"ok": False, "error": "%s 不在后排（Location=%s）" % (card_id, rec.obj.Location)}
        # ★ 压制（pin）的单位**不能移动或攻击**（百科原文）—— 读不出（None）**不拦**。
        pin = self.is_pinned(card_id)
        if pin is True and not force:
            return {"ok": False, "reason": "pinned", "pinned": True,
                    "error": "%s 被压制（pin），不能移动；确信要发就传 force=True" % card_id}
        # ★★ 2026-09-27：**不再调用 `can_move_to`**（用户待办：它临时写手柄字段
        #   `SelectedCard`）。没有真实拖拽态时那个函数**必然**回 False ⇒ 拿它拦动作
        #   等于用假判据否决（§7.6f 的反面）。让游戏在提交那一下自己判；拒绝的理由
        #   由 `game_hints`（提示通道）事后捞。
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "找不到 card %s 的 BP_BoardCard_C" % card_id}
        # ★ 2026-09-27：`slot` 现在是**空隙序号**（0=最左 … n=最右）；换算同 `play_card`
        #   （前线的列号同样"有空洞时 i != 请求值"）。不给 slot 仍是老行为（第一个空列号）。
        if slot is None:
            slot = self._free_front_slot()
            if slot is None:
                return {"ok": False, "error": "前线 4 格满了"}
            place = {"requested": slot, "source": "first_free_slot", "gap": None,
                     "row_numbers": [n for n, _ in self._row_cards(row="front")]}
        else:
            place = self.deploy_location_number(int(slot), row="front", verbose=verbose)
            slot = place.get("requested")
            if slot is None:
                return {"ok": False, "error": "前线 4 格满了"}
        mk = self._mark()
        # ★ 板卡：提交口是"松手"（OnActorMouseUp），不是 OnActorEndDrag
        out = self._drag_lifecycle(atk, LOC_BOARD_FRONTLINE, slot, verbose=verbose,
                                   use_mouse_up=True)
        r = self._receipt(mk, "XActionMoveCardToLine", card_id=card_id, timeout=3.0)
        out.update(r)
        out["slot"] = slot
        out["placement"] = {k: v for k, v in place.items() if k != "row_cards"}
        if not r.get("ok"):
            out["game_hints"] = self.notify_texts()       # 失败当场捞权威理由（提示短命）
        if verbose:
            print("    上线 card_id=%s -> 前线 slot=%s 回执 ok=%s" % (card_id, slot, r.get("ok")))
        return out

    # ------------------------------------------------------------ 原语（不再是"回合循环"）
    # ★ 2026-09-26：`play_turn` / `play_game` 两个启发式自动打牌**已删除**（用户定调：
    #   "自动脚本不能是简单启发式 —— 别在现有那个残废的 playturn 上投入"）。
    #   现在这个类只提供**一个个原子动作**，每一步由调用方（我 / agent 命令层 / NN）看着现场决定。
    UNIT_TYPES = ("tank", "fighter", "bomber", "infantry", "artillery",
                  "antiair", "antitank", "tankdestroyer")

    def attack_card(self, card_id: int, target, verbose: bool = True,
                    force: bool = False, retry=True, snapshot=None) -> dict:
        """**攻击（公开入口，签名对齐 `ops.py::attack_card(card_id, target, retry, force)`）**。

        `target`：card_id（int）或目标规格串（`"hq"` / `"front0"` / `"back1"` / `"guard0"`，
        见 `resolve_target()`）。

        两道**前置闸门**（都在 `_attack_once` 之前，且都能用 `force` 越过）：
          ① **选择界面开着** ⇒ 任何动作都不生效（机械事实，不是判据）。`force` **也不越过**
             它 —— 跟 `ops.py` 一致：这时发出去只会被吃掉/把界面点了，不是"越过判据"。
          ② 游戏自己的 `cardsCheckFunctions_C::CanAttack`（`game_can_attack`）。它**只挑不判**：
             `ok=False`（算不出来）**不拦**；只有明确 `can=False` 才拦，且提示传 `force=True`。
             理由码/中文理由原样带出来，别让调用方只看到一个 `ok=False`。
        `retry`：`True` → 最多 3 次；int → 该次数。失败时**顺带把游戏提示捞出来**当拒绝理由
        （提示短命，必须在失败当场轮询）。
        """
        rt = self.resolve_target(target)
        if not rt.get("ok"):
            return {"ok": False, "target": target, "resolve": rt, "error": rt.get("error")}
        target_card_id = rt["card_id"]
        if not force:
            pend = self.pending_gate()
            if pend.get("pending"):
                return {"ok": False, "pending": pend, "reason": "pending_selection",
                        "error": "现在开着选择界面（%s）——攻击不会生效，先处理它（force 不越过它）"
                                 % (pend.get("reason") or pend)}
        pre = None
        if not force:
            # ★ 2026-10-07：`snapshot`（调用方发出前刚拍的盘面）复用给闸门；不给 ⇒ 闸门自己全量快照一遍（4–6 s，
            #   实机攻击 t_exec 7.7 s 的大头）。判据/失败处理不变，只是少拍一次。
            pre = (self.game_can_attack(card_id, target_card_id, verbose=False, snapshot=snapshot)
                   if snapshot is not None else
                   self.game_can_attack(card_id, target_card_id, verbose=False))
            if pre.get("ok") and pre.get("can") is False:
                return {"ok": False, "reason": "game_says_no", "reason_zh": pre.get("reason_zh"),
                        "precheck": pre, "target_card_id": target_card_id,
                        "hint": "游戏自己的 CanAttack 说不行；确信要发就传 force=True（§7.6f 只挑不判）"}
        # ★「免疫」是**独立信息**：攻击仍然合法、只是这一下不造成伤害。`CanAttack` 的 `can`
        #   不覆盖这个语义 ⇒ 单独用 `target_blockers` 读，并且**只提醒不拦**。
        immune = None
        tcard = rt.get("card")
        try:
            if tcard is not None and (tcard.raw or {}).get("ptr"):
                from kardsmem import cards as _C
                me = self.find_card(card_id)
                immune = _C.target_blockers(self.ks, tcard.raw["ptr"],
                                            attacker_type=(me.card_type if me else None),
                                            attacker_ptr=((me.raw or {}).get("ptr") if me else None))
        except Exception as e:                                # noqa: BLE001
            immune = {"error": str(e)}
        n = 3 if retry is True else max(1, int(retry or 1))
        last = None
        for i in range(n):
            out = self._attack_once(card_id, target_card_id, verbose=verbose)
            out["attempt"] = i + 1
            out["target_card_id"] = target_card_id
            if pre is not None:
                out["precheck"] = pre
            if immune is not None:
                out["defender_immune"] = immune
            if out.get("ok"):
                return out
            last = out
            if i + 1 < n:
                time.sleep(0.5)
        hints = self.notify_texts()
        if hints and last is not None:
            last["game_hints"] = hints
        return last or {"ok": False, "error": "attack 没跑起来"}

    def _attack_probe(self, atk: int) -> dict:
        """攻击提交前的只读快照：箭头字段 + PC 悬停状态 + 真实光标是否在窗口里。"""
        d = {}
        try:
            atk_obj = self.m.ptr(atk + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            ar = self._arrow_for(atk_obj) if atk_obj else 0
            # 箭头全字段 dump 只在 KARDS_ATTACK_PROBE_FULL=1 时做（只读诊断，反射读几十个字段，耗时；
            # 2026-10-01 的对照结论已固化为 pc_hover_rewritten）。默认只记箭头指针 + overCardID。
            import os as _os
            if ar and _os.environ.get("KARDS_ATTACK_PROBE_FULL", "0") == "1":
                d["arrow"] = self.dump_props(ar, own_only=True)
            else:
                d["arrow"] = {"ptr": hex(ar)} if ar else None
            d["pc"] = self.pc_drag_state()
        except Exception as e:                                     # noqa: BLE001
            d["err"] = str(e)
        return d

    def _attack_once(self, card_id: int, target_card_id: int,
                     verbose: bool = True) -> dict:
        """攻击：复刻真实拖拽 + 目标箭头那条链，**不绕过合法性闸门**。

        真实鼠标攻击的序列（对着 `BP_BoardCard_C` / `BP_targetArrowRVX_C` 的函数表）：

            OnActorMouseEnter → MouseHoverDispatch(转发给对手)
            → OnActorMouseDown → OnActorStartDrag（引擎在这里 Spawn 出 BP_targetArrowRVX_C）
            → OnActorDragTick（箭头每 tick 读真实鼠标位置算 overCardID）
            → [把箭头目标写成真人悬停到目标上会得到的那个 cardID]
            → OnActorEndDrag（结算：内部再过一遍 CanAttack/合法性）

        ★ 为什么能写箭头目标：箭头自己靠 `ThrottledMouseLocation(真实鼠标)` 更新
          `overCardID@0x320` / `overCard@0x328`；没有真实鼠标时它算出来的是"没悬停到任何卡"。
          我们写的是"如果鼠标真停在目标上，箭头本该得到的那个值"——跟手牌落点那三个
          变量同一个性质，不是编造状态。而且**用的是游戏自己的
          `spectatorArrowNewTarget(int32)`**（`BlueprintCallable`），不是裸改内存。
        """

        # ★★ 2026-09-26 重构：本体改走**统一原语 `drag_release()`**
        #   （L0 `queueIsRunning` 闸门 + L1 PC 拖拽状态机 + L2 箭头/`cardUnderCursor` + L3 落地口），
        #   并按 `ida-attack-report.md` 补上两处以前漏掉的东西：
        #     ① **`cardUnderCursor`（在攻击者 actor 上，int32 cardID）** —— `PlaceBoardCard`
        #        决定"打谁 / 是移动还是攻击"的**唯一输入**，必须与落地同一次 JS 执行写；
        #     ② **只发一个 `OnActorMouseUp`** —— 老版（`1c6457c`，能打）就是单口；
        #        本次会话改成 MouseUp+EndDrag 之后攻击就再没进过动作流（见 handoff §22.11）。
        #   并恢复游戏自己的 `spectatorArrowNewTarget(target)`（老版做法）。
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的 BP_BoardCard_C" % card_id}
        diag = {}
        try:                                                   # 只读诊断：松手时 CanPlayCard 的各输入
            lg = self.logic_actor()
            lc = self.uclass_of_instance(lg)
            for nm in ("PlayState", "bEndTurnQueued", "myTurnHasStarted", "queueIsRunning"):
                try:
                    diag[nm] = self.peek(lg, self.off(lc, nm, 0), "u8")
                except Exception as e:                         # noqa: BLE001
                    diag[nm] = "err:%s" % e
            f_cpc = self.find_fn(self.uclass_of_instance(atk), "CanPlayCard", inherited=True)
            if f_cpc:
                o = P.call_ufunction(self.api(), atk, f_cpc, "00" * 8)
                diag["CanPlayCard"] = int(bytes(o)[0]) if o else None
        except Exception as e:                                 # noqa: BLE001
            diag["err"] = str(e)
        mk = self._mark()
        out = self.drag_release(
            atk,
            cursor={"card_under_cursor": int(target_card_id), "location_enum": 7, "row": 1},  # IDA: PlaceBoardCard 需要 Location∈{5,6,7}且 Row=true，光标在窗外时这两项是旧值
            arrow_target=int(target_card_id),
            hover_other=self.board_actor_of(target_card_id),
            commit="mouse_up",
            prime_arrow=True,
            pre_commit=lambda: self._attack_probe(atk),
            verbose=verbose)
        out["target_card_id"] = target_card_id
        out["diag"] = diag
        try:                                                   # 提交后的队列/光标状态（定位"被吞"的是哪一道闸）
            out["queue_running_after"] = self.queue_running()
            cls_a = self.uclass_of_instance(atk)
            out["cursor_after"] = {
                "cardUnderCursor": self.peek(atk, self.off(cls_a, "cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR), "s32")}
        except Exception as e_c:                               # noqa: BLE001
            out["cursor_after_err"] = str(e_c)
        r = self._receipt(mk, "XActionAttackCard", card_id=card_id, timeout=3.0)
        if not r.get("ok"):
            # ★ 2026-10-01 用户："一般玩家可以拖拽多次，然后攻击之类会慢慢结算"——动作进队列后可能迟到成交。
            #   没回执时再等一会儿（总共 ~10 s），看是不是"慢结算"而不是被吞；迟到成交也算成功，并标 late。
            r_late = self._receipt(mk, "XActionAttackCard", card_id=card_id, timeout=0.2)   # 实测 4/4 失败都没有迟到成交 ⇒ 不再多等
            if r_late.get("ok"):
                r = dict(r_late, late_receipt=True, waited=round(3.0 + (r_late.get("waited") or 0), 2))
            else:
                out["late_wait_failed"] = True
        if not r.get("ok"):
            try:
                self.call0(atk, self.fn(self.uclass_of_instance(atk), "OnActorEndDrag"))
            except Exception:                                  # noqa: BLE001
                pass
        out.update(r)
        if verbose:
            print("    攻击 attacker=%s target=%s 回执 ok=%s（箭头=%s queue_idle=%s）"
                  % (card_id, target_card_id, r.get("ok"), out.get("arrows_written"),
                     out.get("queue_idle")))
        return out
