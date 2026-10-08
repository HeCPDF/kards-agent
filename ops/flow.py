# -*- coding: utf-8 -*-
"""ops/flow.py —— 换牌、结算、模式与牌组页、开局流程（press_play / quick_start）。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import struct
import time
from typing import Optional

from kardsmem.pick import player_controller
from ops import primitives as P
from ops.consts import (
    NOTE_OFF_DECKBTN_BUTTON, NOTE_OFF_DECKBTN_NAME, NOTE_OFF_DECKBTN_REASON,
    NOTE_OFF_DECK_SHOWING_STARTING_HAND, NOTE_OFF_EOM_CLICKTHROUGH, NOTE_OFF_EOM_STEP,
    NOTE_OFF_HANDCARD_SHOULD_DISCARD, NOTE_OFF_LOGIC_MY_MULLIGAN_DONE, NOTE_OFF_LOGIC_PREGAMESTATE,
    NOTE_OFF_MATCHDATA_CHOSEN_DECK_ID, NOTE_OFF_PLAYBAR_BUTTON_ONCLICKED, NOTE_OFF_PLAYBAR_INNER_BTN,
    NOTE_OFF_PLAYBAR_SKIRMISH,
    NOTE_OFF_PLAYBAR_TEXTAREA, NOTE_OFF_PLAYBAR_TOURNAMENT, NOTE_OFF_PLAYBAR_TRAINING, NOTE_OFF_PLAYBAR_WORLD,
    NOTE_OFF_SETTINGS_SURRENDER_BTN, NOTE_OFF_SIDEBAR_PLAY_BTN, NOTE_OFF_SIDEBAR_RANK_TOGGLE,
    NOTE_OFF_TEXTBLOCK_TEXT, NOTE_OFF_UBUTTON_ONCLICKED, NOTE_OFF_WIDGET_VISIBILITY, SETTLE_AFTER_DISPATCH,
    SETTLE_HOVER,
)
from ops.support import play_gray_guard_on


class FlowMixin:
    """换牌、结算、模式与牌组页、开局流程（press_play / quick_start）。"""

    def pregame_state(self) -> Optional[int]:
        """E_PreGameStates：0 Initialize / 1 SelectHomeBase / 2 ShowStartingHand /
        3 DoMulligan / 4 PlayerReady。**2 才是"在等我换牌"**。"""
        a = self.logic_actor()
        return self.peek(a, NOTE_OFF_LOGIC_PREGAMESTATE, "u8") if a else None

    def mulligan_done(self) -> Optional[bool]:
        a = self.logic_actor()
        return bool(self.peek(a, NOTE_OFF_LOGIC_MY_MULLIGAN_DONE, "u8")) if a else None

    def mulligan_marks(self) -> list:
        from kardsmem.pick import mulligan_marks
        return mulligan_marks(self.ks)

    def mulligan_mark(self, card_id: int, verbose: bool = True) -> dict:
        """点一张手牌：打上/取消"要换"标记（**一次点击 = 一次翻转**，不双击）。"""
        rec = self.hand_actor(card_id)
        if not rec:
            return {"ok": False, "error": "手牌里没有 card_id=%s" % card_id}
        # ★ 先看门槛：窗口没开时，游戏会把后面所有点击**默默吞掉**（屏幕一动不动），
        #   必须先报"窗口没开"，不能让它看起来像注入失败。
        if not self.showing_starting_hand():
            return {"ok": False, "card_id": card_id,
                    "error": "换牌窗口没开（Deck::showingStartingHand=0），游戏会吞掉这次点击"}
        actor = rec["actor"]
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        off_sd = self.off(cls, "shouldDiscard", NOTE_OFF_HANDCARD_SHOULD_DISCARD)
        before = self.peek(actor, off_sd, "u8")

        self.call0(actor, self.fn(cls, "OnActorMouseEnter"))
        self.settle(SETTLE_HOVER, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch"), actor)
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ★ `OnActorClicked(bool IsPrecise)` **带一个参数**（SDK 签名核实过），不是
        #   无参事件——用 call0 传 NULL parms 会在函数体里读 IsPrecise 时空指针崩掉
        #   （实机复现：access violation accessing 0x0）。真实单击是"精确点击"，传 True。
        # ★ 2026-09-26 重构：走统一点击原语（先 `GlobalMouseUp(mouseDownActor)`，
        #   未消费才转发 `OnActorClicked`；带 L0 队列闸门 + L1 PC 状态）。
        click_res = self.click_actor(actor, is_precise=1, verbose=verbose)
        self.settle(0.25, 6)          # 等 shouldDiscard 真的翻转再读 ⇒ 按帧等
        after = self.peek(actor, off_sd, "u8")
        if verbose:
            print("    换牌标记 %s(%s): shouldDiscard %s -> %s"
                  % (rec.get("name"), card_id, before, after))
        return {"ok": before != after, "card_id": card_id, "marked_before": before,
                "marked_after": after}

    def _mulligan_ui_tail(self, verbose: bool = True) -> dict:
        """补 BP 在 `onConfirmClicked->Broadcast()` 之后的收尾：清 `MulliganTimeOutEventHandler` 计时器 + `RemoveFromParent()`。
        控件已不在（已自己收尾）⇒ 什么都不做。每一步都幂等。"""
        w = self.instance_of_class("BP_ConfirmCardsButton_C")
        if not w:
            return {"ran": False, "why": "换牌控件已不在（BP 自己收尾了）"}
        res = {"ran": True}
        cls = self.uclass_of_instance(w)
        try:
            off_h = self.off(cls, "MulliganTimeOutEventHandler", 0x03A8)
            handle = self.ks.m.u64(w + off_h) or 0
            res["timer_handle"] = handle
            if handle:
                _c, cdo, f = self._bp_lib_fn("KismetSystemLibrary", "K2_ClearAndInvalidateTimerHandle")
                if cdo and f:
                    parms = struct.pack("<QQ", w, handle)
                    res["timer_cleared"] = P.call_ufunction(self.api(), cdo, f, parms) is not None
                else:
                    res["timer_cleared"] = False
        except Exception as e:                                    # noqa: BLE001
            res["timer_error"] = "%s: %s" % (type(e).__name__, e)
        try:
            f_rm = self.find_fn(cls, "RemoveFromParent")
            res["removed"] = bool(f_rm and self.call0(w, f_rm))
        except Exception as e:                                    # noqa: BLE001
            res["remove_error"] = "%s: %s" % (type(e).__name__, e)
        if verbose:
            print("    换牌收尾补做：%s" % res)
        return res

    def mulligan_confirm(self, verbose: bool = True) -> dict:
        """点确认按钮 —— **真的把子按钮按下去**（hover → pressed → released → clicked）。

        ★ 2026-09-26 用户当场指出："没点到确认按钮，还是等到重新调度结束才关窗口。"
          根因：`BP_ConfirmCardsButton_C` 自己**只有两个句柄**（一个无关的 CardZoomedButton，
          一个转发用 `..._MasterTextButton_..._1_onClicked`）—— **它没有 pressed/released**。
          真正的按钮是它身上的子 widget **`WBP_NUI_MasterTextButton // 0x0348`**，
          四件套全在子按钮类上（`WBP_NUI_MasterTextButton_classes.hpp`）：
              hover  `..._1_OnButtonHoverEvent` / `..._2_OnButtonHoverEvent`
              press  `..._5_OnButtonPressedEvent`
              release`..._7_OnButtonReleasedEvent`
              click  `..._0_OnButtonClickedEvent`
          ⇒ 老实现只在**父 widget** 上调 `onHovered` + `onClicked`：逻辑确实跑了
          （换牌成功），但**按钮组件从未被按下**，屏幕上看不到点击、窗口也不由它关闭。
          现在改成在**子按钮**上按真实顺序走一遍（clicked 那一跳会转发到父的回调）。
        """
        btn_widget = self.instance_of_class("BP_ConfirmCardsButton_C")
        if not btn_widget:
            return {"ok": False, "error": "找不到 BP_ConfirmCardsButton_C（不在换牌界面）"}
        cls = self.uclass_of_instance(btn_widget)
        out = {"ok": False, "parent": hex(btn_widget)}
        done_before = bool(self.mulligan_done())
        # ① 子按钮（真正的 UMG 按钮）
        off_child = self.off(cls, "WBP_NUI_MasterTextButton", 0x0348)
        child = self.m.peek(btn_widget, off_child, "ptr") if hasattr(self.m, "peek") else None
        if not child:
            child = self.m.ptr(btn_widget + off_child) or 0
        out["child"] = hex(child) if child else 0
        if child:
            ccls = self.uclass_of_instance(child)
            # ★ 2026-10-01 用户："确认按钮没点，只换了牌"：逻辑推进了但屏幕上看不到按钮被按下。
            #   这里的停顿是**裸 time.sleep**（按下 0.08 s / 松开 0.05 s），失焦降帧时这段时间里游戏一帧都没画，
            #   按下态根本没渲染出来 ⇒ 改成按**帧数**等（`settle(秒, 帧)`），和攻击手势同一原因同一修法。
            seq = (("hover", "BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature", 0.25, 6),
                   ("press", "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature", 0.08, 4),
                   ("release", "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature", 0.05, 3),
                   ("click", "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature", 0.25, 6))
            out["child_calls"] = []
            for name, fn_name, pause, frames in seq:
                f = self.find_fn(ccls, fn_name)
                out["child_calls"].append((name, bool(f)))
                if f:
                    self.call0(child, f)
                    self.settle(pause, frames)
        # ② 父 widget 的 onClicked 兜底（子按钮 clicked 通常会转发过来；幂等）
        f_click = self.find_fn(
            cls, "BndEvt__BP_ConfirmCardsButton_WBP_NUI_MasterTextButton_"
                 "K2Node_ComponentBoundEvent_1_onClicked__DelegateSignature")
        if f_click and not child:
            f_hover = self.find_fn(
                cls, "BndEvt__BP_ConfirmCardsButton_WBP_NUI_MasterTextButton_"
                     "K2Node_ComponentBoundEvent_0_onHovered__DelegateSignature")
            if f_hover:
                self.call0(btn_widget, f_hover)
                self.settle(0.25, 6)  # 兜底路径的 hover→click 之间按帧等（同子按钮四步的理由）
            self.call0(btn_widget, f_click)
        if verbose:
            print("    换牌确认: 子按钮 hover→press→release→click（child=0x%X，兜底父句柄=%s）"
                  % (child or 0, bool(f_click) and not child))
        # 回执：等 PreGameState 离开 2，或 myMulliganDone 变 1
        t0 = time.time()
        while time.time() - t0 < 3.0:
            if self.mulligan_done() or self.pregame_state() not in (2, 3):
                break
            time.sleep(0.1)
        done = bool(self.mulligan_done())
        # ★ 2026-09-25 曾观察到"确认点下去、逻辑推进了，但换牌 UI 不自己关"，
        #   当时用 `HideMulliganUi()` 兜底。
        # ★ 2026-09-26 更正（用户定调）：**那个兜底无效，已删**。真正的原因是
        #   按钮层级找错了（父 widget 没有 pressed/released，四件套在子按钮
        #   `WBP_NUI_MasterTextButton // 0x0348` 上）—— 把四件套在**子按钮**上走完，
        #   界面**本来就会自己关**（实机 1.22 s）。兜底既不是真人的行为，
        #   还会掩盖"点击没生效"。
        #   现在只**如实记录**：点击后界面有没有自己关；没关就报出来，不再替它收拾。
        # ★ 2026-10-03（用户：换牌的确成功了、对局也推进了，只是换牌按钮和提示还在；最后弹“换牌超时”）：
        #   逻辑推进了，但 BP 里 `onConfirmClicked->Broadcast()` 之后的**收尾**没跑——清超时计时器 + `RemoveFromParent()`
        #   （BP_ConfirmCardsButton.cpp Label_1108）。用户判断：回调没有全部触发。这里把漏掉的那两步补上（和 BP 同一对调用，
        #   纯表现层，不再 Broadcast，不会二次确认），并如实记录。
        advanced = (self.pregame_state() not in (2, 3, None)) or (done and not done_before)
        if advanced:
            out["ui_tail"] = self._mulligan_ui_tail(verbose=verbose)
        out["window_closed_by_click"] = (self.showing_starting_hand() is not True)
        res = {"ok": done,
               "pregame_state": self.pregame_state(),
               "mulligan_done": done,
               "marked": [r["card_id"] for r in self.mulligan_marks() if r.get("marked")]}
        res.update({k: out[k] for k in ("child", "child_calls", "window_closed_by_click", "ui_tail", "done_before")
                    if k in out})
        if done and not out.get("window_closed_by_click"):
            res["warning"] = "换牌逻辑推进了，但点击后界面没自己关（真点击可能没生效）"
        if verbose:
            print("    证据：window_closed_by_click=%s（无兜底）"
                  % out.get("window_closed_by_click"))
        return res

    def showing_starting_hand(self) -> Optional[bool]:
        """`BP_Deck_C::showingStartingHand` —— **换牌窗口的真正判据**。

        ★ 2026-09-25 实机坐实"点了没反应"的根因：`MulliganDiscardToggle()` 的入口门槛是
          `Deck->showingStartingHand && IsInMyHand`。窗口没开时（这个字段 = 0），
          我们发进去的 hover/click **全部被游戏自己吞掉**，屏幕一动不动 ——
          看起来像"注入失败"，其实是"窗口不在"。所以动手前必须先看这个字段。
        """
        d = self.deck_actor()
        if not d:
            return None
        off = self.off(self.uclass_of_instance(d), "showingStartingHand",
                       NOTE_OFF_DECK_SHOWING_STARTING_HAND)
        return bool(self.peek(d, off, "u8"))

    def wait_for_mulligan(self, timeout: float = 300.0, verbose: bool = True) -> bool:
        """等换牌窗口：**`showingStartingHand == 1`**。

        ★ 两层等待，为了在**几秒的窗口**里抓住它：
          ① 先高频轮询 `PreGameState`（逻辑实例已缓存 ⇒ 单次 ~0.3ms）；
             阶段没到 2/3 之前不去找 `BP_Deck_C`（找它要全量扫 GObjects，代价 ~4s）。
          ② 阶段到了再开始查 `showingStartingHand`（第一次查可能要付一次全扫，
             之后就缓存住、每次 ~0.3ms）。
        ⚠ 之前这里一直用 0.25s 直接查 `showingStartingHand`，而每查一次还要全扫一遍，
          实际采样 ~0.1Hz ⇒ 窗口永远抓不到（实测第一次采样就迟了 11s）。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.pregame_state()
            if st in (2, 3):                       # ShowStartingHand / DoMulligan
                if self.showing_starting_hand():
                    if verbose:
                        print("  等到换牌窗口（showingStartingHand=1，%.1fs）" % (time.time() - t0))
                    return True
            time.sleep(0.05)
        return False

    def mulligan(self, discard=None, wait: float = 0.0, verbose: bool = True) -> dict:
        """换牌：等窗口（可选）→ 把 `discard` 里的牌逐张打标记 → 点确认。

        `discard=[]`（默认）＝一张都不换，直接确认 —— 真人最常见的选择。
        ⚠ 窗口很短（超时 55s，超时走 `MulliganTimedOut()→AutoClick()`，
        和手点**同一条路径**，所以超时确认与手点确认在内存里分不出来）。
        """
        if wait and not self.wait_for_mulligan(wait, verbose=verbose):
            return {"ok": False, "error": "%.0fs 内没等到换牌窗口" % wait}
        gate = self.showing_starting_hand()
        if not gate:
            return {"ok": False,
                    "error": "换牌窗口没开（showingStartingHand=%s）—— 游戏自己的门槛会吞掉所有点击"
                             % gate,
                    "pregame_state": self.pregame_state()}
        marks = [self.mulligan_mark(cid, verbose=verbose) for cid in (discard or [])]
        r = self.mulligan_confirm(verbose=verbose)
        r["marks"] = marks
        return r

    def _live_settings_panel(self, verbose: bool = True):
        """挑**当前这一局**的设置面板：`EndMatch_Surrender` 非空的那个。

        ★ 2026-09-28 审计发现：这里原来是"第一个非空指针就用"——投降是**不可逆**、
          没有二次确认的动作（见 `surrender()`），而这个类跟牌组页的按钮同属
          "旧屏幕残留的陈旧实例也可能非空指针"那一类坑（`select_deck()`/
          `list_decks()` 已经在同一类问题上真的崩过一次）。这个类目前没有
          找到像 `deckButton::OnClicked` 那样可靠的"活实例"信号（它的点击句柄
          绑在**面板自己的类**上，是 `BndEvt__..._onClicked__DelegateSignature`
          这种组件绑定事件，不是一个能读 `Num` 的多播委托字段）——**没有就不编**，
          按项目自己的证据标准：查不到就如实说查不到，不能拿"猜一个"
          冒充"验证过"。
          ⇒ 只有**恰好一个**非空候选时才返回它；多个都非空就拒绝并把候选列表
          原样报出来，让调用方自己看着办（跟 `pick_choice()` 撞到多组候选时
          "必须显式指定，不能猜"是同一个纪律），而不是像原来那样悄悄点第一个。
        """
        cands = []
        for p in self.instances_of_class("Battle_Settings_Widget_C"):
            off = self.off(self.uclass_of_instance(p), "EndMatch_Surrender",
                           NOTE_OFF_SETTINGS_SURRENDER_BTN)
            if self.m.ptr(p + off):
                cands.append(p)
        if len(cands) == 1:
            return cands[0]
        if verbose:
            if not cands:
                print("    _live_settings_panel：没有 EndMatch_Surrender 非空的实例")
            else:
                print("    _live_settings_panel：%d 个实例的 EndMatch_Surrender 都非空"
                      "（%s）—— 分不清哪个是当前这一局的，拒绝猜、不返回任何一个"
                      % (len(cands), ", ".join(hex(c) for c in cands)))
        return 0

    def _ensure_settings_open(self, panel: int, pcls: int, verbose: bool = True) -> bool:
        """把设置面板**收敛到"打开"**，返回是否成功。

        ★ 为什么不能像原来那样直接点齿轮：齿轮是**开关**。面板本来就开着时再点一次
          就把它关上了，然后再去点"投降"——就是点在看不见的按钮上（用户指出的 bug）。
          这里改用游戏自己的 `ToggleSettingsMenu(bool* IsNowVisible)`：它**告诉我们
          结果状态**。第一次调用把它翻一下并读结果，不是 True 就再翻一次，最多 3 次
          ⇒ 无论起始状态如何，结束时一定是"打开"。
        """
        f = self.fn(pcls, "ToggleSettingsMenu")
        for i in range(3):
            vis = self.call_out_u8(panel, f)
            if verbose:
                print("    ToggleSettingsMenu -> IsNowVisible=%s" % vis)
            if vis:
                return True
            self.settle(0.35, 6)      # 等设置面板真的打开（要它渲染）⇒ 按帧等
        return False

    # ------------------------------------------------------------ 投降（注入版）
    def surrender(self, confirm: bool = False, verbose: bool = True) -> dict:
        """投降，全程注入（不再用真实鼠标点 (1240,30)/(1134,57)）。

        ★ 跟 `ops.py::surrender` 同等纪律：这是**不可逆**动作，而且游戏**一键、没有二次
          确认**（规格 §7.6e）⇒ 自己做一道闸：`confirm=True` 才真发，否则原样返回
          `needs_confirm=True`（调用方/前端据此提问，而不是默默执行）。

        真实流程（规格 §7.6e + SDK 字段）：
          齿轮 `WBP_TopCornerButtons_Settings_C::SettingsPanelButton`
            → 设置面板 `Battle_Settings_Widget_C` 展开
            → 点 `EndMatch_Surrender`（`verticalKardsButtonWithText_Widget_C`）
            → 该按钮的 onClicked 绑到
              `BndEvt__Battle_Settings_Widget_EndMatch_Surrender_..._2_onClicked__DelegateSignature(Button)`
            → `OnEndMatchSurrenderClicked()` → `EndMatchFromAction("surrender")`
        ★ 投降**一键、没有二次确认**（规格 §7.6e），而且服务端可配置禁用
          （`surrendered_disabled_at_start`）—— 调用方要自己负责。
        """
        if not confirm:
            return {"ok": False, "needs_confirm": True,
                    "error": "surrender 不可逆，而且游戏没有二次确认；确定要投降请传 confirm=True"}
        panel = self._live_settings_panel(verbose=verbose)
        if not panel:
            return {"ok": False, "error": "挑不出当前这一局唯一的 Battle_Settings_Widget_C"
                                          "（要么全是空指针，要么有多个都非空、分不清哪个是活的——"
                                          "看上面打印的候选列表，`notes` 也会记一笔）"}
        pcls = self.uclass_of_instance(panel)

        # ① **明确把设置面板置为可见**，而不是去点齿轮。
        #    ★ 用户指出的 bug：齿轮是**开关**（`ToggleSettingsMenu`）。面板本来就开着时
        #      再点一次就把它关上了，然后再去点"投降"——等于点一个看不见的按钮。
        #      而且进程里有**多个**齿轮实例（旧的按钮指针是空的），逐个点更是乱的。
        #    ⇒ 改用游戏自己的 `SetSettingsVisibility(true)`：**幂等**的 setter
        #      （`ToggleSettingsMenu` 内部就是它在改状态），开着的再设一次还是开着。
        f_set = self.find_fn(pcls, "SetSettingsVisibility")
        if f_set:
            self.call_i32(panel, f_set, 1)
            self.settle(0.35, 6)      # 等设置面板打开 ⇒ 按帧等（降帧时 0.35 s 里可能一帧没跑）
            if verbose:
                print("    SetSettingsVisibility(True) 已发出（幂等，不依赖当前开关状态）")
        else:
            return {"ok": False, "error": "找不到 SetSettingsVisibility，不敢盲点齿轮"}

        # ② 投降按钮：先做按钮自己的悬停/按下/松开（可见效果），再触发它的 onClicked
        btn = self.m.ptr(panel + self.off(pcls, "EndMatch_Surrender", NOTE_OFF_SETTINGS_SURRENDER_BTN))
        if not btn:
            return {"ok": False, "error": "EndMatch_Surrender 指针为空"}
        bcls = self.uclass_of_instance(btn)
        # 投降按钮的悬停/按下/松开之间按帧等（同换牌确认：失焦降帧时按下态不会渲染）
        for suffix, pause, frames in (("2_OnButtonHoverEvent", 0.25, 6), ("0_OnButtonPressedEvent", 0.08, 4),
                                      ("1_OnButtonReleasedEvent", 0.05, 3)):
            f = self.find_fn(bcls, "BndEvt__Button_0_K2Node_ComponentBoundEvent_%s__DelegateSignature" % suffix)
            if f:
                self.call0(btn, f)
                self.settle(pause, frames)
        f_click = self.fn(
            pcls, "BndEvt__Battle_Settings_Widget_EndMatch_Surrender_"
                  "K2Node_ComponentBoundEvent_2_onClicked__DelegateSignature")
        self.call_ptr(panel, f_click, btn)          # 这个句柄带一个 Button 入参
        if verbose:
            print("    投降按钮 onClicked 已发出")

        t0 = time.time()
        while time.time() - t0 < 4.0:
            from kardsmem import board as BA
            if BA.open_source("mem").snapshot().match_finished:
                break
            time.sleep(0.2)
        from kardsmem import attach as _attach
        from kardsmem import board as BA
        fin = BA.open_source("mem").snapshot().match_finished
        return {"ok": bool(fin), "match_finished": fin}

    # ------------------------------------------------------------ 开局流程（一屏一步）
    # ★ 纪律：**一屏一步，不自动连点**。每一步单独调用、单独验证（屏幕状态变了才走下一步），
    #   免得盲点在菜单里乱点把 UI 搅乱。
    def end_of_match_continue(self, verbose: bool = True) -> dict:
        """结算页（失败/胜利）点"继续"。

        实机核实（2026-09-25）：
          * 真实点击落在**全屏的 `ClickThrough`**（`UButton`，`Visibility=0` Visible）；
          * `LeaveBattlefield_Button` 此刻是 `SelfHitTestInvisible(4)` —— **点不到**，
            所以不能拿它当"继续"。
        绑定句柄在 `W_EndOfMatch_C` 上：
          `BndEvt__WBP_NUI_EndOfMatch_ClickThrough_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature`
        ⚠ 游戏里有 `allowClickThroughAfterDelay(Delay)` —— 太早点它不生效；调用方自己等够。
        """
        ws = self.instances_of_class("W_EndOfMatch_C")
        if not ws:
            return {"ok": False, "error": "没有 W_EndOfMatch_C（不在结算页？）"}
        w = ws[0]
        cls = self.uclass_of_instance(w)
        off_ct = self.off(cls, "ClickThrough", NOTE_OFF_EOM_CLICKTHROUGH)
        btn = self.m.ptr(w + off_ct)
        if not btn:
            return {"ok": False, "error": "ClickThrough 指针为空"}
        vis = self.peek(btn, NOTE_OFF_WIDGET_VISIBILITY, "u8")
        if vis != 0:
            return {"ok": False,
                    "error": "ClickThrough 当前不可点（Visibility=%s，0 才是 Visible）" % vis}
        f = self.fn(cls, "BndEvt__WBP_NUI_EndOfMatch_ClickThrough_"
                         "K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature")
        off_step = self.off(cls, "step", NOTE_OFF_EOM_STEP)
        before = self.peek(w, off_step, "u8")
        self.call0(w, f)
        # ★ 点一次就要能看到**这一步真的推进了**（`step` 变、或整个控件消失）——
        #   不做"盲点一长串"。`E_EndOFMatchStep` 是 0..8。
        # ★ 2026-09-25：这里原来每 0.15s 就 `instances_of_class()` 全扫一次判断
        #   "控件是不是没了"——`GObjects` 涨到 12 万+ 后单次全扫 4~10s，
        #   一个 4 秒的等待循环硬是被拖成十几秒。改用**同一个** `w` 指针复查
        #   `class_of()`（2 次内存读 vs 遍历 12 万个对象），便宜得多。
        t0 = time.time()
        after = before
        while time.time() - t0 < 4.0:
            still_alive = self.pool.fname_of(self.oa.class_of(w)) == "W_EndOfMatch_C"
            if not still_alive:
                after = None
                break
            after = self.peek(w, off_step, "u8")
            if after != before:
                break
            time.sleep(0.15)
        advanced = (after != before)
        if verbose:
            print("    结算页 ClickThrough 已点：step %s -> %s%s"
                  % (before, after, "" if advanced else "（没推进！可能 click-through 还没解锁）"))
        return {"ok": advanced, "step_before": before, "step_after": after,
                "advanced": advanced, "left_screen": after is None}

    # ------------------------------------------------------------ 牌组页（模式 + 开始）
    # 模式列表 = `WBP_NUI_Playbar_C`（左侧那一列）。每个模式是一个
    # `WBP_NUI_MasterPlaybarButton_C`，绑定的 onClicked 句柄名里带蓝图节点序号。
    #
    # ⚠⚠ **下面这张表的"节点名/序号 → 模式"对应关系是错的（2026-09-25 实机证伪）**：
    #   点了我以为是 training 的那一对句柄（hover `..._TrainingButton_..._10` +
    #   click `..._Training_..._5`）之后，界面从 **training（start_btn y=604）** 变成了
    #   **versus/casual**（probe: `casual_mode_btn` 命中 1.0、`start_btn` 找不到）——
    #   而 versus 才有"休闲"页签，training 上方什么都没有。⇒ 蓝图节点名（`Training`）
    #   跟它实际管哪个模式**对不上**，不能靠名字推。
    #   下一步要么在 Playbar 上找一个"当前选中模式"的字段，要么**一次点一个**、
    #   用 `start_btn` 的 y（604=训练 / 625=对战·休闲 / 651=对战·经典）当观测量把表对出来。
    PLAYBAR_MODES = {
        "training": ("TrainingButton", "Training", 5),
        "versus":   ("BattleButton", "Battle", 6),
        "skirmish": ("SkirmishButton", "Draft", 7),
        "campaign": ("CampaignButton", "Campaign", 9),
        "code":     ("BattleCodeButton", "BattleCode", 0),
    }

    def playbar(self, cache: Optional[dict] = None) -> int:
        """挑**这一屏**的 Playbar（TrainingButton 非空的那个 —— 又是陈旧实例坑）。"""
        for p in self.instances_of_class("WBP_NUI_Playbar_C", cache=cache):
            cls = self.uclass_of_instance(p)
            btn = self.m.ptr(p + self.off(cls, "TrainingButton", NOTE_OFF_PLAYBAR_TRAINING))
            if btn:
                return p
        return 0

    # ★ 牌组页四个类（模式列表 / 牌组按钮 / 侧栏 / 全局赛况数据）同屏共存——
    #   `quick_start()` 靠这张表一次扫完，后面几步全部传 cache，不再各扫各的。
    #   `BP_MatchData_C` 用来验证"选牌组"是不是真生效了（见 `select_deck` 的 verify+retry）。
    DECK_SCREEN_CLASSES = ("WBP_NUI_Playbar_C", "W_MatchDeckSelectionDeckButton_C",
                           "W_DeckSelectedSideBar_C", "BP_MatchData_C")

    def deck_screen_scan(self) -> dict:
        """一次扫完牌组页要用到的几个类，供 `quick_start()`/手动分步调用共享。"""
        return self.scan_classes(self.DECK_SCREEN_CLASSES)

    def chosen_deck_id(self, cache: Optional[dict] = None) -> Optional[int]:
        """`BP_MatchData_C::chosenDeckID`——当前真正生效的牌组 ID（不是 UI 摆设）。

        用来在"选牌组"点完之后**验证真的换了**，而不是信一次点击就完事。
        """
        rows = self.instances_of_class("BP_MatchData_C", cache=cache)
        if not rows:
            return None
        md = rows[0]
        cls = self.uclass_of_instance(md)
        off = self.off(cls, "chosenDeckID", NOTE_OFF_MATCHDATA_CHOSEN_DECK_ID)
        return self.peek(md, off, "s32")

    def select_mode(self, mode: str = "training", verbose: bool = True) -> dict:
        """在左侧模式列表里点一个模式。

        ★ 2026-10-04（实机抓到**假阳性**）：旧实现走 4 个 K2 句柄
          （`BndEvt__WBP_NUI_Playbar_<Mode>Button_..._10_onHovered` 与 `..._%d_onClicked`），
          点了**改不动模式** —— 牌组页 `selected_mode()` 是 `{'selected': 2}`（对战模式，截图里
          对战模式高亮）时调用它，它回 `ok=True`，随后 `selected_mode` **仍是 2**、界面一点没变
          （就是 2026-10-03 `select_mode_by_label` 注释里证伪过的那条老路）。
          这正是弯路 #22：`ok` 只看"点击路径成功"，不看**后置条件**。
        ⇒ 现在只走 `select_mode_by_label`（点按钮 `OnClicked` 多播里**真正订阅**的处理器），
          并且 **`ok` 以后置校验（`verified`）为准**。
        """
        r = self.select_mode_by_label(mode, verbose=verbose)
        if not r.get("verified"):
            return dict(r, ok=False,
                        error=r.get("error") or "点了模式但 `selected_mode` 没变（后置校验失败）")
        return r

    #: 左侧导航的三个入口 —— **名字来自导出件**（`WBP_NUI_Sidebar.cpp`）：
    #: `WBP_NUI_Sidebar_C : UWBP_NUI_MasterSidebar_C` 里写着
    #: `sidebarPlayButton`（开始）/`sidebarCardsButton`（卡牌）/`sidebarShopButton`（商店），
    #: 外加多播 `OnNUISideBar_PlayButtonClicked` / `OpenActiveGameMode`；
    #: 按钮类 `WBP_NUI_MasterSidebarButton_C` 自带 `OnClicked`（FMulticastScriptDelegate）
    #: 与 `sidebarButton`/`sidebarButtonText`。★ 别按类名猜：我先猜 `squareKardsButton_Widget_C`
    #: 是导航（实机一列：文本全是 "Button"、四个中文标签一个都不在）—— 那批按钮不是左侧导航。
    #: 导航控件数目固定，按**字段名**（枚举键）点，不按屏幕文字匹配（用户 2026-10-06）。
    SIDEBAR_BUTTONS = {"play": "sidebarPlayButton", "cards": "sidebarCardsButton", "shop": "sidebarShopButton"}

    def _sidebar_instances(self, cache: Optional[dict] = None) -> list:
        """当前屏的 `WBP_NUI_Sidebar_C` 实例（只读）。"""
        return self.instances_of_class("WBP_NUI_Sidebar_C", cache=cache)

    def list_sidebar_buttons(self, cache: Optional[dict] = None) -> list:
        """左侧导航三个按钮的**文本 + 订阅者数**（只读，不点）。

        为什么需要：live 全流程**不允许真鼠标、窗口必须后台**（用户 2026-10-04 口径）⇒ 导航只能走
        合成事件；先只读确认"哪个实例是活的"（按钮 `OnClicked` 有订阅者 = 活的）。
        """
        from kardsmem.names import ftext_at
        out = []
        for s in self._sidebar_instances(cache=cache):
            cls = self.uclass_of_instance(s)
            row = {"sidebar": hex(s)}
            for label, field in (("开始", "sidebarPlayButton"), ("卡牌", "sidebarCardsButton"),
                                 ("商店", "sidebarShopButton")):
                btn = self.m.ptr(s + self.off(cls, field, 0))
                txt, subs = None, 0
                if btn:
                    bcls = self.uclass_of_instance(btn)
                    tb = self.m.ptr(btn + self.off(bcls, "sidebarButtonText", 0x0350))
                    if tb:
                        try:
                            txt = ftext_at(self.m, tb, NOTE_OFF_TEXTBLOCK_TEXT)
                        except Exception:                          # noqa: BLE001
                            txt = None
                    # 内层原生 UButton = `Sidebarbutton`@0x360（SDK 逐字段）⇒ 用它的
                    # `OnClicked`@0x538 判活（控件自己的多播 @0x390 拿不稳，见 `press_sidebar`）。
                    inner = self.m.ptr(btn + self.off(bcls, "Sidebarbutton", 0x0360))
                    subs = self.delegate_num(inner, NOTE_OFF_UBUTTON_ONCLICKED) if inner else 0
                row[label] = {"btn": hex(btn) if btn else None, "text": txt, "subs": subs}
            out.append(row)
        return out

    def press_sidebar(self, label: str = "play", verbose: bool = False,
                      cache: Optional[dict] = None) -> dict:
        """点左侧导航（**合成事件**：不动真鼠标、与窗口焦点/前后台无关）。

        路径：`WBP_NUI_Sidebar_C` 实例 → 字段（`sidebarPlayButton` 等，**名字来自导出件**）→
        按钮 `OnClicked` 多播里**真正订阅**的处理器 → `call0`（与 `select_mode_by_label` 同源：
        不按蓝图节点名猜）。点完**按帧等**（失焦降帧时裸 sleep 里游戏一帧没跑），
        并用 `list_decks()` 非空做**后置校验** —— `ok` 只认后置条件，不认"我点了"（弯路 #22）。
        """
        field = self.SIDEBAR_BUTTONS.get(label)
        if not field:
            return {"ok": False, "error": "未知导航 %r（可选 %s）"
                    % (label, sorted(set(self.SIDEBAR_BUTTONS.values())))}
        for s in self._sidebar_instances(cache=cache):
            cls = self.uclass_of_instance(s)
            btn = self.m.ptr(s + self.off(cls, field, 0))
            if not btn:
                continue
            bcls = self.uclass_of_instance(btn)
            # ★ 用**内层原生 `UButton`** 的 `OnClicked`@0x538 点（弹窗那条路就是这么成的）；
            #   别用控件自己的多播（`WBP_NUI_MasterSidebarButton_C::OnClicked`@0x390 ——
            #   属性链解析不可靠，实测拿它 `call0` 直接 `access violation accessing 0x0`）。
            #   内层按钮在这个类里叫 `Sidebarbutton` @0x360（SDK 逐字段）。
            inner = self.m.ptr(btn + self.off(bcls, "Sidebarbutton", 0x0360))
            tgt = self.delegate_target(inner, NOTE_OFF_UBUTTON_ONCLICKED) if inner else None
            if not tgt:
                continue                       # 陈旧实例（没订阅者）⇒ 换下一个
            sub, fn_name = tgt
            f = self.find_fn(self.uclass_of_instance(sub), fn_name)
            if not f:
                return {"ok": False, "field": field, "handler": fn_name,
                        "error": "订阅者上没有这个函数"}
            if verbose:
                print("    导航：%s（%s）→ %s" % (label, field, fn_name))
            self.call0(sub, f)
            for _ in range(6):
                self.settle(0.3, 4)
                if field != "sidebarPlayButton" or self.list_decks():
                    return {"ok": True, "label": label, "field": field, "handler": fn_name,
                            "verified": "deck_page" if field == "sidebarPlayButton" else "pressed"}
            return {"ok": False, "field": field, "handler": fn_name,
                    "error": "点了但界面没推进（`list_decks` 仍为空）", "verified": False}
        return {"ok": False, "error": "没有 live 的 WBP_NUI_Sidebar_C（或 %s 无订阅者）" % field}

    def open_start_page(self, verbose: bool = True, cache: Optional[dict] = None) -> dict:
        """去「开始」页（= 左侧导航 `sidebarPlayButton`）—— 合成事件，窗口可留后台。"""
        return self.press_sidebar("play", verbose=verbose, cache=cache)

    #: 两种确认弹窗（导出件字段名 + SDK 偏移）—— 都扫，按**按钮文本**点：
    #: * `WBP_NUI_ConfirmCancelDialog_C`：`TextBlock_24`@0x3A8（正文）/`confirmButton`@0x3B0/`CancelButton`@0x3C0
    #: * `WBP_NUI_MasterDialogBox_C`：`YesButton`@0x348/`NoButton`@0x360/`MiddleButton`@0x368
    #:   （文本按钮 `WBP_NUI_MasterTextButton_C`：`buttonTextValue`@0x370、内层 `Button`@0x388，
    #:   其 `UButton::OnClicked` 在 +0x538）
    #: ★ 实测（2026-10-04 断线弹窗）：屏幕上那个是 **MasterDialogBox**，不是 ConfirmCancelDialog
    #:   —— 只写一个类会静默找不到（`list_dialogs` 回 `[]`）。
    DIALOG_CLASSES = (
        ("WBP_NUI_ConfirmCancelDialog_C", (("TextBlock_24", 0x03A8),
                                           ("confirmButton", 0x03B0), ("CancelButton", 0x03C0))),
        ("WBP_NUI_MasterDialogBox_C", (("TitleTextBox", 0x0350), ("YesButton", 0x0348),
                                       ("NoButton", 0x0360), ("MiddleButton", 0x0368))),
    )
    NOTE_OFF_TEXTBTN_TEXT = 0x0370
    NOTE_OFF_TEXTBTN_BUTTON = 0x0388

    def _text_button(self, btn: int):
        """`WBP_NUI_MasterTextButton_C` 实例 → `(文本, 内层 UButton)`（只读）。"""
        from kardsmem.names import ftext_at
        if not btn:
            return None, 0
        bcls = self.uclass_of_instance(btn)
        tb = self.m.ptr(btn + self.off(bcls, "buttonTextValue", self.NOTE_OFF_TEXTBTN_TEXT))
        txt = None
        if tb:
            try:
                txt = ftext_at(self.m, tb, NOTE_OFF_TEXTBLOCK_TEXT)
            except Exception:                                      # noqa: BLE001
                txt = None
        inner = self.m.ptr(btn + self.off(bcls, "Button", self.NOTE_OFF_TEXTBTN_BUTTON))
        return txt, inner

    def _dialog_rows(self, cache: Optional[dict] = None) -> list:
        """当前屏两种弹窗的**按钮清单**：`[{dialog, cls, title, buttons:[{field,btn,text,subs}]}]`。"""
        from kardsmem.names import ftext_at
        out = []
        for cls_name, fields in self.DIALOG_CLASSES:
            for d in self.instances_of_class(cls_name, cache=cache):
                cls = self.uclass_of_instance(d)
                row = {"dialog": hex(d), "cls": cls_name, "title": None, "buttons": []}
                for fname, foff in fields:
                    p = self.m.ptr(d + self.off(cls, fname, foff))
                    if not p:
                        continue
                    if "Button" not in fname:              # 正文/标题（`TextBlock_24`/`TitleTextBox`）
                        if row["title"] is None:
                            try:
                                row["title"] = ftext_at(self.m, p, NOTE_OFF_TEXTBLOCK_TEXT)
                            except Exception:                      # noqa: BLE001
                                row["title"] = None
                        continue
                    txt, inner = self._text_button(p)
                    row["buttons"].append({"field": fname, "btn": hex(p),
                                           "text": txt,
                                           "subs": self.delegate_num(inner, NOTE_OFF_UBUTTON_ONCLICKED)
                                           if inner else 0})
                out.append(row)
        return out

    def list_dialogs(self, cache: Optional[dict] = None) -> list:
        """当前屏的确认弹窗（正文 + 每个按钮的文本/订阅者数）—— **只读，不点**。"""
        return self._dialog_rows(cache=cache)

    def press_dialog_button(self, text: str = "重新连接", verbose: bool = True,
                            cache: Optional[dict] = None) -> dict:
        """按**按钮文本**点确认弹窗（**合成事件**：不动真鼠标、窗口可留后台）。

        为什么不按左右/字段名猜哪个是"重新连接"（弯路 #43：节点名、位置都不可靠）：
        两个按钮的文本都能从内存读（`buttonTextValue`@0x370）。找不到文本匹配 ⇒ **如实失败**，
        不"按位置替它点一下"（那是在赌）。
        """
        seen = []
        for row in self._dialog_rows(cache=cache):
            for b in row["buttons"]:
                seen.append(b.get("text"))
                if not (b.get("text") and str(text).lower() in str(b["text"]).lower()):
                    continue
                inner = 0
                # 按钮对象是文本按钮 ⇒ 再取一次内层 UButton（`onClicked` 在它身上）
                _txt, inner = self._text_button(int(b["btn"], 16))
                tgt = self.delegate_target(inner, NOTE_OFF_UBUTTON_ONCLICKED) if inner else None
                if not tgt:
                    return {"ok": False, "text": str(b["text"]), "field": b["field"],
                            "error": "按钮 OnClicked 没有订阅者（陈旧实例？）"}
                sub, fn_name = tgt
                f = self.find_fn(self.uclass_of_instance(sub), fn_name)
                if not f:
                    return {"ok": False, "text": str(b["text"]), "field": b["field"],
                            "handler": fn_name, "error": "订阅者上没有这个函数"}
                if verbose:
                    print("    弹窗（%s）：按文本点 %r（%s → %s）"
                          % (row["cls"], str(b["text"]), b["field"], fn_name))
                self.call0(sub, f)
                self.settle(0.4, 5)
                return {"ok": True, "text": str(b["text"]), "field": b["field"],
                        "handler": fn_name, "cls": row["cls"]}
        return {"ok": False, "error": "没有文本含 %r 的弹窗按钮（当前屏按钮文本：%s）" % (text, seen)}

    def reconnect(self, verbose: bool = True) -> dict:
        """断线弹窗的「重新连接」（合成事件；用户 2026-10-04：窗口可后台、不许真鼠标）。"""
        r = self.press_dialog_button("重新连接", verbose=verbose)
        if not r.get("ok"):
            r2 = self.press_dialog_button("Reconnect", verbose=False)
            if r2.get("ok"):
                return r2
        return r

    def _live_deck_sidebar(self, cache: Optional[dict] = None, verbose: bool = False):
        """挑**这一屏**的 `W_DeckSelectedSideBar_C`：`PlayButton` 内层 `UButton`
        的 `OnClicked` **有订阅者**的那个。返回 `(sidebar, play_button, inner_button)`
        三个指针（后两个可能是 0），找不到活实例时 `sidebar=0`。

        ★ 2026-09-28 审计发现：`select_deck()`/`list_decks()` 已经在**同一屏**的
          牌组按钮上踩过"陈旧实例静态判据分不出来"这个坑，加了 delegate 订阅者数
          判据（`live = delegate_num(btn, NOTE_OFF_UBUTTON_ONCLICKED) > 0`）。
          但 `deck_start()`/`press_play()`/`start_enabled()` 这三个原来各自独立扫
          **同一个类**（`W_DeckSelectedSideBar_C`）、只看 `PlayButton` 指针是否
          非空——完全是同一个坑，只是没传播到这三处。收成一个方法，三处共用。
        """
        for s in self.instances_of_class("W_DeckSelectedSideBar_C", cache=cache):
            cls = self.uclass_of_instance(s)
            pb = self.m.ptr(s + self.off(cls, "PlayButton", NOTE_OFF_SIDEBAR_PLAY_BTN))
            if not pb:
                continue
            pcls = self.uclass_of_instance(pb)
            inner = self.m.ptr(pb + self.off(pcls, "Button", NOTE_OFF_PLAYBAR_INNER_BTN))
            if inner and self.delegate_num(inner, NOTE_OFF_UBUTTON_ONCLICKED) > 0:
                return s, pb, inner
        if verbose:
            print("    _live_deck_sidebar：没有 PlayButton 内层 OnClicked 有订阅者的"
                  " W_DeckSelectedSideBar_C（都是陈旧实例，或者不在牌组页）")
        return 0, 0, 0

    def deck_start(self, verbose: bool = True, cache: Optional[dict] = None) -> dict:
        """牌组页右下角的"开始"（`W_DeckSelectedSideBar_C` 的 PlayButton）。"""
        side, _pb, _inner = self._live_deck_sidebar(cache=cache, verbose=verbose)
        if not side:
            return {"ok": False, "error": "没有 live 的 W_DeckSelectedSideBar_C"
                                          "（PlayButton 内层 OnClicked 无订阅者，可能是陈旧实例）"}
        cls = self.uclass_of_instance(side)
        f = self.fn(cls, "BndEvt__WBP_NUI_DeckSelectedSideBar_playButton_"
                         "K2Node_ComponentBoundEvent_8_onClicked__DelegateSignature")
        self.call0(side, f)
        if verbose:
            print("    牌组页：PlayButton onClicked 已发出")
        self.settle(1.5, 6)           # 牌组页"开始"→等界面推进 ⇒ 按帧等（用户怀疑的"开始按钮"就是这里）
        return {"ok": True}

    def list_decks(self, cache: Optional[dict] = None) -> list:
        """牌组页上每个牌组按钮 → [{index, name, actor, vis, button, live}]（**只读，不点**）。

        `W_MatchDeckSelectionDeckButton_C` 的字段（SDK）：
            `deckName`   (UTextBlock*) @0x3C0   ← **牌组名可从内存读**，不用认字/认像素
            `deckButton` (UButton*)    @0x3C8   ← 真正可点的那个
        实测这一屏会一次性建出几十个按钮实例，所以**按名字挑**比按坐标挑稳。

        ★ **陈旧实例**：`GObjects` 全扫会连同名字重复的旧屏幕残留一起扫出来
          （实机撞见过 31 个牌组变成 62 条，一模一样的名字各两份）。单看 `vis`/
          `bIsEnabled` 分不出哪份是真的在接收点击——两份读出来经常一样。
          `live` 用 `deckButton::OnClicked` 的订阅者数判（`Num>0` 才是真的接了线，
          同 handoff §四·3 里 sidebar `PlayButton` 那套判据）。
        """
        from kardsmem.names import ftext_at
        out = []
        for p in self.instances_of_class("W_MatchDeckSelectionDeckButton_C", cache=cache):
            cls = self.uclass_of_instance(p)
            off_n = self.off(cls, "deckName", NOTE_OFF_DECKBTN_NAME)
            off_b = self.off(cls, "deckButton", NOTE_OFF_DECKBTN_BUTTON)
            tb = self.m.ptr(p + off_n)
            btn = self.m.ptr(p + off_b)
            name = None
            if tb:
                try:
                    name = ftext_at(self.m, tb, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    name = None
            live = self.delegate_num(btn, NOTE_OFF_UBUTTON_ONCLICKED) > 0 if btn else False
            out.append({"actor": hex(p), "index": len(out), "name": name,
                        "vis": self.peek(p, NOTE_OFF_WIDGET_VISIBILITY, "u8"),
                        "button": hex(btn) if btn else None, "live": live})
        return out

    def select_deck(self, name: str = None, index: int = None, verbose: bool = True,
                    cache: Optional[dict] = None) -> dict:
        """选牌组：按名字（或序号）点那个牌组按钮。

        句柄：`BndEvt__WBP_EditDeckSelectionDeck_deckButton_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature`
        （注意名字里带的是 `WBP_EditDeckSelectionDeck`，跟类名 `W_MatchDeckSelectionDeckButton` 不一致 ——
         蓝图复用留下的，别按类名猜）。

        ★ **多 live 实例**：`OnClicked` 有订阅者只能说明"点了会有人接"，不能说明
          "接的就是当前这一屏"——实机撞见过同名牌组 2 个实例**都** live（都有订阅者）
          的情况，静态判据分不出谁是真的。改成**点完就验**：读
          `BP_MatchData_C::chosenDeckID` 点前点后有没有变，没变就换下一个 live 候选
          再试一次，而不是赌第一个候选一定对。
        """
        rows = self.list_decks(cache=cache)
        matches = []
        for r in rows:
            if index is not None and r["index"] == index:
                matches = [r]
                break
            if name is not None and r["name"] and name.lower() in r["name"].lower():
                matches.append(r)
        if not matches:
            return {"ok": False, "error": "没找到牌组 name=%r index=%r" % (name, index),
                    "decks": [(r["index"], r["name"]) for r in rows]}
        live_matches = [r for r in matches if r.get("live")]
        candidates = live_matches or matches
        if len(matches) > 1 and verbose:
            print("    ⚠ %r 匹配到 %d 个同名实例（live=%d 个），按顺序试、点完验证 chosenDeckID"
                  % (name, len(matches), len(live_matches)))
        if not live_matches:
            return {"ok": False, "error": "只找到陈旧实例（OnClicked 订阅者数=0），点了也没用",
                    "deck": candidates[0] if candidates else None}

        # ★★ 2026-09-25 血的教训（实机崩溃复现过）：**绝不对第二个候选发起调用**。
        #   真实鼠标每次点击都是当场重新命中测试，指哪打哪；注入用的是**提前扫好
        #   缓存的指针**——第一次点击一旦让引擎重建了这一屏（哪怕只是这一个按钮的
        #   父级列表），候选列表里其它指针就可能变成野指针，再调用就是
        #   `EXCEPTION_ACCESS_VIOLATION`（复现过一次，读了个位于 0x1f 附近的地址，
        #   典型的"对着已经不在了的对象调引擎函数"）。这里只点**一个**——排第一的
        #   live 候选——点完只做**只读**验证，不管有没有变，都不再碰任何别的指针。
        pick = candidates[0]
        if not pick["button"]:
            return {"ok": False, "error": "该牌组按钮指针为空", "deck": pick}
        before = self.chosen_deck_id(cache=cache)
        actor = int(pick["actor"], 16)
        cls = self.uclass_of_instance(actor)
        f_hover = self.find_fn(cls, "BndEvt__WBP_NUI_MatchDeckSelectionDeckButton_Button_0_"
                                    "K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature")
        if f_hover:
            self.call0(actor, f_hover)
            self.settle(0.3, 6)       # 牌组按钮悬停停留按帧等
        f_click = self.fn(cls, "BndEvt__WBP_EditDeckSelectionDeck_deckButton_"
                               "K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature")
        self.call0(actor, f_click)
        self.settle(0.8, 6)           # 点完牌组等 chosenDeckID 真的变 ⇒ 按帧等
        after = self.chosen_deck_id()          # ★ 不传 cache：chosenDeckID 是刚点出来的新状态，必须现读
        changed = (after is not None and after != before)
        if verbose:
            print("    已选牌组：%s（index=%s）chosenDeckID %s -> %s%s"
                  % (pick["name"], pick["index"], before, after,
                     "" if changed else "（没变——可能本来就选中了它，也可能没点上，不再试第二个候选）"))
        return {"ok": True, "deck": pick["name"], "index": pick["index"],
                "chosen_deck_id": after, "unverified": not changed}

    def list_toggles(self) -> list:
        """找 `RankedCasualToggle`（排位/休闲）—— 对战模式才有。"""
        for s in self.instances_of_class("W_DeckSelectedSideBar_C"):
            cls = self.uclass_of_instance(s)
            t = self.m.ptr(s + self.off(cls, "RankedCasualToggle", NOTE_OFF_SIDEBAR_RANK_TOGGLE))
            if t:
                return [{"sidebar": hex(s), "toggle": hex(t),
                         "left": hex(self.m.ptr(t + 0x350) or 0),
                         "right": hex(self.m.ptr(t + 0x348) or 0)}]
        return []

    def list_modes(self, cache: Optional[dict] = None) -> list:
        """模式列表每个按钮的**标签**（从内存读，`TextArea` 是 UTextBlock）—— 只读。

        为什么要按标签而不是按蓝图节点名：上一次实机证伪过，节点名 `Training` 那对句柄
        点下去变成了**对战**。`WBP_NUI_MasterPlaybarButton_C` 自带 `TextArea`(0x348) 和
        `Button`(0x368)，所以能"看名字点"。
        """
        from kardsmem.names import ftext_at
        bar = self.playbar(cache=cache)
        if not bar:
            return []
        cls = self.uclass_of_instance(bar)
        out = []
        for field, note in (("WorldChampionshipButton", NOTE_OFF_PLAYBAR_WORLD),
                            ("TrainingButton", NOTE_OFF_PLAYBAR_TRAINING),
                            ("TournamentButton", NOTE_OFF_PLAYBAR_TOURNAMENT),
                            ("SkirmishButton", NOTE_OFF_PLAYBAR_SKIRMISH)):
            btn = self.m.ptr(bar + self.off(cls, field, note))
            if not btn:
                out.append({"field": field, "label": None, "button": None})
                continue
            bcls = self.uclass_of_instance(btn)
            ta = self.m.ptr(btn + self.off(bcls, "TextArea", NOTE_OFF_PLAYBAR_TEXTAREA))
            inn = self.m.ptr(btn + self.off(bcls, "Button", NOTE_OFF_PLAYBAR_INNER_BTN))
            label = None
            if ta:
                try:
                    label = ftext_at(self.m, ta, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    label = None
            out.append({"field": field, "label": label, "actor": hex(btn),
                        "button": hex(inn) if inn else None,
                        "vis": self.peek(btn, NOTE_OFF_WIDGET_VISIBILITY, "u8")})
        return out

    def deck_invalid_reason(self, name: str = None, index: int = None) -> Optional[str]:
        """读某个牌组按钮上"为什么不能打"的原文（`InvalidDeckReasonText`，URichTextBlock@0x360）。

        实测：选 `预备日快` 后开始按钮变灰，理由是 **`含未拥有卡牌`** —— 游戏自己说的，
        比我们猜"是不是预备卡的问题"可靠。
        """
        from kardsmem.names import ftext_at
        for r in self.list_decks():
            if (index is not None and r["index"] == index) or \
               (name is not None and r["name"] and name.lower() in r["name"].lower()):
                a = int(r["actor"], 16)
                c = self.uclass_of_instance(a)
                p = self.m.ptr(a + self.off(c, "InvalidDeckReasonText", NOTE_OFF_DECKBTN_REASON))
                if not p:
                    return None
                try:
                    return ftext_at(self.m, p, NOTE_OFF_TEXTBLOCK_TEXT)
                except Exception:                            # noqa: BLE001
                    return None
        return None

    def start_enabled(self, cache: Optional[dict] = None) -> dict:
        """开始按钮到底能不能点 —— **看内层 `UButton`**，不是外层包装。

        ★ 实测坑：`W_DeckSelectedSideBar_C::PlayButton`（`WBP_NUI_MasterTextButton_C`）
          外层的 `bIsEnabled` 一直是 True，而**内层 `UButton` 才是真的开关**：
          选了 `预备日快` 之后外层仍 True、内层变 False（开始按钮灰）。
          ⇒ 只读外层会以为"能点"，然后点在一个灰按钮上（和前面几个坑同一类）。
        ★ 2026-09-28：改走 `_live_deck_sidebar()`——原来这里直接拿第一个
          `PlayButton` 非空的实例，同一屏可能有陈旧实例混在里面（跟 `press_play()`
          是同一个坑，见那边的注释）。
        """
        side, pb, inner = self._live_deck_sidebar(cache=cache)
        if not side:
            return {"play_button": None, "clickable": False,
                    "error": "没有 live 的 W_DeckSelectedSideBar_C（不在牌组页，或全是陈旧实例）"}
        return {"play_button": hex(pb),
                "outer_enabled": self.widget_enabled(pb),
                "inner": hex(inner) if inner else None,
                "inner_enabled": self.widget_enabled(inner) if inner else None,
                "clickable": bool(self.widget_enabled(inner)) if inner else False}

    def selected_mode(self, cache: Optional[dict] = None) -> Optional[dict]:
        """当前模式（只读）：`WBP_NUI_Playbar_C` 的 `ChosenGameMode`(0x438) / `SelectedGameMode`(0x462) 两字节。

        2026-10-03 实机（launcher 1.60）逐个调模式处理器测出：**选中态 = `SelectedGameMode`**。
        实测值（只用来判"变了没有"，不是官方枚举名）：Battle=2 / Training=1 / Draft=3 / Campaign=4 /
        BattleCode=6 / Championship=7 / Tournament=8。找不到 playbar ⇒ None。
        """
        bar = self.playbar(cache=cache)
        if not bar:
            return None
        cls = self.uclass_of_instance(bar)
        return {"playbar": hex(bar),
                "chosen": self.peek(bar, self.off(cls, "ChosenGameMode", 0x438), "u8"),
                "selected": self.peek(bar, self.off(cls, "SelectedGameMode", 0x462), "u8")}

    def select_mode_by_label(self, label: str = "TRAINING", verbose: bool = True,
                             cache: Optional[dict] = None) -> dict:
        """按**标签**选模式：调「游戏自己绑在按钮 OnClicked 上的处理器」，点完用 `SelectedGameMode` 后置验证。

        ★ 2026-10-03 实机（launcher 1.60）推翻了旧实现（旧版点按钮自身的 4 个 K2 句柄）：
          那 4 个句柄都存在、`call0` 也成功，但 **改不动模式**（`SelectedGameMode` 2→2、界面不切）；
          真正生效的是按钮 `OnClicked` 多播里订阅的 playbar 处理器
          （`BndEvt__WBP_NUI_Playbar_..._onClicked`）；而且 Battle/Training 两个按钮的处理器在 BP 里
          名字是**反的** ⇒ 这里现读绑定，不按名字猜。
        """
        modes = self.list_modes(cache=cache)
        for m in modes:
            if not (m.get("label") and label.lower() in m["label"].lower()):
                continue
            btn = int(m["actor"], 16)
            cls = self.uclass_of_instance(btn)
            tgt = self.delegate_target(btn, self.off(cls, "OnClicked", NOTE_OFF_PLAYBAR_BUTTON_ONCLICKED))
            if not tgt:
                return {"ok": False, "label": m.get("label"), "field": m.get("field"),
                        "error": "按钮 OnClicked 没有订阅者（陈旧实例？）"}
            sub, fn_name = tgt
            f = self.find_fn(self.uclass_of_instance(sub), fn_name)
            if not f:
                return {"ok": False, "label": m.get("label"), "field": m.get("field"),
                        "handler": fn_name, "error": "订阅者上没有函数 %s" % fn_name}
            before = self.selected_mode(cache=cache)
            self.call0(sub, f)
            after = before
            for _ in range(6):                     # 点击后等界面推进 ⇒ 按帧等（失焦降帧时裸 sleep 里游戏一帧没跑）
                self.settle(0.2, 4)
                after = self.selected_mode()
                if after and before and after.get("selected") != before.get("selected"):
                    break
            changed = bool(before and after and after.get("selected") != before.get("selected"))
            if verbose:
                print("    模式：%s（%s）SelectedGameMode %s -> %s%s"
                      % (m["label"], m["field"], (before or {}).get("selected"),
                         (after or {}).get("selected"), "" if changed else "（没变！）"))
            return {"ok": True, "label": m["label"], "field": m["field"], "actor": m["actor"],
                    "subscriber": hex(sub), "handler": fn_name, "verified": changed,
                    "mode_before": before, "mode_after": after}
        return {"ok": False, "error": "没有标签匹配 %r 的模式按钮" % label,
                "modes": [(x["field"], x["label"]) for x in modes]}

    def press_play(self, verbose: bool = True, cache: Optional[dict] = None) -> dict:
        """点"开始"—— **先确认它真的可点**（看内层 UButton），灰的就不点。

        ★ 2026-09-25 晚：一度怀疑"跳步"（直接调 sidebar 的 `..._8_onClicked`，
        没先过 wrapper 自己的 hover/pressed/released）是"点了没反应"的根因，改成了
        wrapper 4 跳版本——但用户随后拿**真实物理鼠标**去点同一个按钮，同样没反应。
        这就排除了"注入跳步"这个假设：问题在客户端自己卡住了，不是我们调用序列的
        锅。改回这个更简单的版本（该记录留着，省得以后又去查这条死路）。
        ★ 2026-09-28：改走 `_live_deck_sidebar()`，而且**只扫一次、直接复用同一个
          指针**去点——原来是先调 `start_enabled()`（它自己扫一遍拿到一个 sidebar）
          判断能不能点，再**独立重新扫一遍**去点击，两次扫描理论上可能扫到不同的
          实例（同屏多个 live 候选时）。现在两件事共用同一次解析结果，不会对着
          "判断能点的那个"和"真正点的那个"是两个不同对象。
        """
        side, pb, inner = self._live_deck_sidebar(cache=cache, verbose=verbose)
        if not side:
            return {"ok": False, "error": "没有 live 的 W_DeckSelectedSideBar_C"
                                          "（PlayButton 内层 OnClicked 无订阅者，可能是陈旧实例）"}
        st = {"play_button": hex(pb), "outer_enabled": self.widget_enabled(pb),
              "inner": hex(inner) if inner else None,
              "inner_enabled": self.widget_enabled(inner) if inner else None,
              "clickable": bool(self.widget_enabled(inner)) if inner else False}
        # ★ 2026-10-03（用户拍板）：默认**不拦**灰按钮；原判据是
        #   `if not st.get("clickable")`（灰的就不点），想临时恢复严格模式：
        #   设 `KARDS_PLAY_BLOCK_GRAY=1`。为什么默认关：这行只决定我们自己发不发
        #   这次点击 —— 不改按钮 enabled/视觉（界面照旧灰），也不碰服务端校验；
        #   开局是否真成立由 `gui.autoplay.verify_start()` 点击后只读判定。
        if play_gray_guard_on() and not st.get("clickable"):
            return {"ok": False, "start": st,
                    "error": "开始按钮是灰的（内层 UButton disabled），不点"}
        cls = self.uclass_of_instance(side)
        f = self.fn(cls, "BndEvt__WBP_NUI_DeckSelectedSideBar_playButton_"
                         "K2Node_ComponentBoundEvent_8_onClicked__DelegateSignature")
        self.call0(side, f)
        if verbose:
            print("    牌组页：开始按钮已点（点之前确认过内层 enabled=True）")
        self.settle(1.5, 6)           # 开始按钮 → 等开局界面推进 ⇒ 按帧等（同 deck_start）
        return {"ok": True, "start_before": st}

    def quick_start(self, mode_label: str = "TRAINING", deck_name: str = None,
                    deck_index: int = None, verbose: bool = True) -> dict:
        """牌组页三连（选模式→选牌组→点开始）。

        ★ 2026-09-25：`GObjects` 扫描本体已经搬进 Frida（`scan_classes()`），
          单次全扫从 4~10s 压到 1~2s——不再需要死抠"只扫一遍"，**分两次扫更稳**：
          实机撞见过牌组按钮列表在**切模式之前**还没建出来（`quick_start` 一开始就
          扫、扫到的还是切模式前那一屏，`list_decks()` 直接是空的）。改成切模式
          **之后**再扫一次牌组页（侧栏+牌组按钮+赛况数据），两次扫描加起来也就
          2~4s，比死磕"一次扫描"更不容易踩这类时序坑。
        """
        t0 = time.time()
        r1 = self.select_mode_by_label(mode_label, verbose=verbose)
        cache = self.deck_screen_scan()
        if verbose:
            print("    quick_start: 切模式后重新扫描完成（累计 %.2fs）" % (time.time() - t0))
        r2 = self.select_deck(name=deck_name, index=deck_index, verbose=verbose, cache=cache)
        r3 = self.press_play(verbose=verbose, cache=cache)
        ok = bool(r1.get("ok") and r2.get("ok") and r3.get("ok"))
        return {"ok": ok, "mode": r1, "deck": r2, "play": r3,
                "scan_seconds": time.time() - t0}
