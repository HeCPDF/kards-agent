# -*- coding: utf-8 -*-
"""ops/gesture.py —— 手势原语：拖拽生命周期、点击、悬停、目标箭头、屏幕坐标（合成鼠标事件序列，不挪真实光标）。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import struct
import time
from typing import Optional

from kardsmem.pick import player_controller
from ops import primitives as P
from ops.consts import (
    CURSOR_FIELD, NOTE_OFF_ACTOR_SELF_BASECARD, NOTE_OFF_ARROW_FINAL_LENGTH, NOTE_OFF_ARROW_FROM_CARD,
    NOTE_OFF_ARROW_HEAD_PLANE, NOTE_OFF_ARROW_OVER_CARD, NOTE_OFF_ARROW_OVER_CARD_ID,
    NOTE_OFF_CARD_UNDER_CURSOR, NOTE_OFF_LOCATION_ENUM, NOTE_OFF_LOCATION_NUMBER, NOTE_OFF_PC_DRAG_ACTOR,
    NOTE_OFF_PC_DRAG_STARTED, NOTE_OFF_PC_LEFT_BUTTON_DOWN, NOTE_OFF_PC_MOUSE_DOWN_ACTOR,
    NOTE_OFF_PC_MOUSE_OVER_ACTOR, NOTE_OFF_PC_OLD_MOUSE_OVER_ACTOR, NOTE_OFF_ROW_UNDER_CURSOR,
    SETTLE_AFTER_DISPATCH, SETTLE_AFTER_DOWN, SETTLE_AFTER_TICK, SETTLE_FLOOR, SETTLE_HOVER, SETTLE_HOVER_ATTACK,
    SETTLE_HOVER_INSPECT,
)


class GestureMixin:
    """手势原语：拖拽生命周期、点击、悬停、目标箭头、屏幕坐标（合成鼠标事件序列，不挪真实光标）。"""

    def screen_of_card(self, card_id: int) -> dict:
        """目标卡在**游戏视口**里的屏幕坐标（只读）：`K2_GetActorLocation` + `APlayerController::ProjectWorldLocationToScreen`。
        用来把真实光标移到目标上（用户 2026-10-01：真鼠标放在总部上，攻击就能打）。"""
        actor = self.board_actor_of(card_id)
        if not actor:
            return {"ok": False, "error": "场上找不到 card %s 的 actor" % card_id}
        loc = self.actor_location(actor)
        if not loc:
            return {"ok": False, "error": "读不到 actor 位置"}
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f = self.find_fn(pcls, "ProjectWorldLocationToScreen")
        if not f:
            return {"ok": False, "error": "PC 上没有 ProjectWorldLocationToScreen"}
        parms = bytearray(0x30)
        struct.pack_into("<ddd", parms, 0x00, loc[0], loc[1], loc[2])
        parms[0x28] = 0                                            # bPlayerViewportRelative=false：视口像素
        out = P.call_ufunction(self.api(), pc, f, parms)
        buf = bytes(out) if out else b""
        if len(buf) < 0x2A:
            return {"ok": False, "error": "投影没返回"}
        x, y = struct.unpack_from("<dd", buf, 0x18)
        return {"ok": bool(buf[0x29]), "x": x, "y": y, "world": loc}

    def pc_drag_state(self) -> dict:
        """PC 上**鼠标**拖拽用的 5 个字段当前值（只读，排查用）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        out = {"pc": hex(pc)}
        for name, note in (("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR),
                           ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR),
                           ("dragActor", NOTE_OFF_PC_DRAG_ACTOR),
                           ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN),
                           ("dragStarted", NOTE_OFF_PC_DRAG_STARTED)):
            try:
                off = self.off(pcls, name, note)
                v = self.m.ptr(pc + off) if "Actor" in name else self.m.u8(pc + off)
                out[name] = hex(v) if v else 0
            except Exception:                                  # noqa: BLE001
                out[name] = None
        return out

    def _pc_writes(self, actor: int):
        """L1：把 PC 的拖拽 5 字段写成"正在拖 actor"（可塞进同一次 JS 执行）。

        ★ 为什么不只调 `SetMouseDownActorManual`：`OnActorStartDrag` 的 `success` 出参
          **只有一条路径写 true**，我们传 0 ⇒ 游戏读到 false ⇒ PC 会把 `mouseDownActor`
          清空（报告 §5）。所以**在提交那一次执行里再写一遍**最稳。
        """
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        w = []
        for name, note, kind, val in (
                ("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR, "ptr", actor),
                ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR, "ptr", actor),
                ("dragActor", NOTE_OFF_PC_DRAG_ACTOR, "ptr", actor),
                ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN, "u8", 1),
                ("dragStarted", NOTE_OFF_PC_DRAG_STARTED, "u8", 1)):
            try:
                w.append((pc, self.off(pcls, name, note), kind, val))
            except Exception:                                  # noqa: BLE001
                pass
        return w

    def pc_drag_begin(self, actor: int, verbose: bool = False) -> dict:
        """L1：让 PC 进入"正在拖 actor"（调游戏自己的 `SetMouseDownActorManual`）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f = self.find_fn(pcls, "SetMouseDownActorManual")
        out = {"pc": hex(pc), "setter": bool(f)}
        if f:
            out["ret"] = self.call_ptr(pc, f, actor)
        else:
            self.notes.append("PC 上没有 SetMouseDownActorManual ⇒ 只写字段兜底")
        if verbose:
            print("    L1 pc_drag_begin: setter=%s  %s" % (bool(f), out))
        return out

    def pc_clear_hover(self, verbose: bool = False) -> dict:
        """动作收尾：把 PC 的「悬停/按下/拖拽」指针全部写回 0（**只写零，不调用任何函数**）。

        ★ 2026-09-30 崩溃现场（`kards+0x11d37e6` / `+0x11ad577`，多播委托清理里对已释放
          对象做虚调用，虚表指针成了 0x1e3020030 / 0x100000000）+ `click-crash-report.md` §3：
          游戏每帧用 `oldMouseOverActor`（上一帧的 mouseOverActor）和 `mouseOverActor` 比较来发
          进入/离开事件。我们悬停的目标单位一旦被这次动作打死销毁，这两个字段就成了悬空指针，
          下一帧游戏对已释放对象发事件 ⇒ 崩。鼠标不在任何东西上时游戏自己写的就是 0，
          所以写零不是伪造状态。
        ⛔ 不用 `ForceEndMouseOver()`：它会对当前悬停对象发「离开」事件——对象已释放时正好踩雷
          （同 `click_actor` 里不调 `pc_drag_end` 的理由）。
        """
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        writes = []
        for name, note, kind in (
                ("mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR, "ptr"),
                ("oldMouseOverActor", NOTE_OFF_PC_OLD_MOUSE_OVER_ACTOR, "ptr"),
                ("MouseDownActor", NOTE_OFF_PC_MOUSE_DOWN_ACTOR, "ptr"),
                ("dragActor", NOTE_OFF_PC_DRAG_ACTOR, "ptr"),
                ("leftButtonDown", NOTE_OFF_PC_LEFT_BUTTON_DOWN, "u8"),
                ("dragStarted", NOTE_OFF_PC_DRAG_STARTED, "u8")):
            try:
                writes.append((pc, self.off(pcls, name, note), kind, 0))
            except Exception:                                  # noqa: BLE001
                pass
        back = self.write_and_call(writes) if writes else []
        out = {"pc": hex(pc), "n": len(writes), "back": back}
        if verbose:
            print("    pc_clear_hover:", out)
        return out

    def pc_drag_end(self, verbose: bool = False) -> dict:
        """L1 收尾：`ForceReleaseDrag()` + `ForceEndMouseOver()`（幂等，清残留）。"""
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        out = {"pc": hex(pc)}
        for name in ("ForceReleaseDrag", "ForceEndMouseOver"):
            f = self.find_fn(pcls, name)
            out[name] = bool(f)
            if f:
                try:
                    self.call0(pc, f)
                except Exception as e:                         # noqa: BLE001
                    out[name + "_err"] = str(e)
        if verbose:
            print("    L1 pc_drag_end:", out)
        return out

    def drag_release(self, actor: int, *,
                     cursor: Optional[dict] = None,
                     arrow_target: Optional[int] = None,
                     arrow_length_gate: bool = False,
                     hover_other: Optional[int] = None,
                     commit: str = "end_drag",
                     hover_pause: Optional[float] = None,
                     prime_arrow: bool = False,
                     arrow_setter: bool = True,
                     queue_gate: bool = True,
                     cleanup: bool = True,
                     pre_commit=None,
                     verbose: bool = True) -> dict:
        """★★ **统一的"拖拽并松手"原语**（L0+L1+L2 共用，L3 由 `commit` 指定）。

        * `cursor`：`{"card_under_cursor": id, "location_number": n, "location_enum": e, "row": r}`
          —— 只写给了的项（L2，写在**被拖的 actor** 上）。
        * `arrow_target`：给了就写箭头（`overCardID` + `overCard` + 头部平面搬远），
          并默认先调游戏自己的 `spectatorArrowNewTarget(target)`。
        * `commit`：`"end_drag"`（手牌）/ `"mouse_up"`（板卡）/ `"both"` / `"none"`。
        * `prime_arrow`：没有箭头时空拖一次把它生出来（攻击必需）。
        * `queue_gate`：先等 `queueIsRunning==false`（L0）。
        """
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        out = {"actor": hex(actor), "commit": commit}

        # ★ 2026-09-26 二分开关（首次实机回归排查用）：任一层都能单独关掉，
        #   用来判断"某条拖拽失败"到底是新加的哪一层引入的。
        #     KARDS_DRAG_L0=0 关队列闸门；KARDS_DRAG_L1=0 关 PC 拖拽状态机（退回重构前行为）
        #     KARDS_DRAG_L3=both|mouse_up|end_drag  覆盖落地口
        import os as _os
        l0 = _os.environ.get("KARDS_DRAG_L0", "1") != "0"
        l1 = _os.environ.get("KARDS_DRAG_L1", "0") != "0"   # ★ 默认 **0**：L1 未验证前退回已验证行为
        l3 = _os.environ.get("KARDS_DRAG_L3")
        if l3:
            commit = l3
            out["commit"] = commit

        # ---- L0：队列闸门 ----
        if queue_gate and l0:
            out["queue_idle"] = self.wait_queue_idle()
        # ---- L1 起手：清残留拖拽态 ----
        if cleanup and l1:
            self.pc_drag_end()
        # ---- 箭头预备（攻击：同一个拖拽里刚 Spawn 的箭头游戏不认目标）----
        # ★ 2026-10-01 用户：攻击"主要延迟在箭头出现、在目标上松手"，真人≤5 s、熟练者<3 s。
        #   旧做法 = 先整套空拖一次（悬停+按下+起拖+EndDrag+等 1 s）把箭头"预热"出来，再来一次真拖 ⇒ 两遍手势。
        #   新做法（默认）：只拖一次，起拖后**等箭头 actor 真的出现 + 至少几帧**再悬停目标/落地；
        #   失焦降帧下靠帧数兜底。KARDS_ATTACK_PRIME=1 退回旧的两遍做法。
        fast_prime = prime_arrow and _os.environ.get("KARDS_ATTACK_PRIME", "0") != "1"
        if prime_arrow and not fast_prime and not self.arrow_actors():
            self._drag_lead(actor, cls, pc, pc_cls)
            try:
                self.call0(actor, self.fn(cls, "OnActorEndDrag", inherited=True))
            except Exception:                                  # noqa: BLE001
                pass
            self.settle(1.0, 6)       # KARDS_ATTACK_PRIME=1 的旧两遍做法：等箭头真的生成 ⇒ 按帧等，失焦降帧时不空等
            out["primed"] = True
        # ---- L1 悬停 + 转发（红线：悬停必须发生）----
        self.call0(actor, self.fn(cls, "OnActorMouseEnter", inherited=True))
        _hv = SETTLE_HOVER_ATTACK if prime_arrow else SETTLE_HOVER      # 攻击手势的悬停单独一档（consts 注释）
        self.settle(_hv if hover_pause is None else hover_pause, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch", inherited=True), actor)
        out["hover_dispatched"] = True
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        # ---- L1 官方 setter ----
        if l1:
            out["pc_begin"] = self.pc_drag_begin(actor)
        # ---- 按下 / 起拖 / 拖动 tick ----
        self.call0(actor, self.fn(cls, "OnActorMouseDown", inherited=True))
        self.settle(SETTLE_AFTER_DOWN, 2)
        f_start = self.fn(cls, "OnActorStartDrag", inherited=True)
        out["drag_success"] = self.call_ptr(actor, f_start, 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, self.fn(cls, "OnActorDragTick", inherited=True))
        self.settle(SETTLE_AFTER_TICK, 2)
        if fast_prime:
            atk_obj_f = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            t_arrow = time.time()
            while time.time() - t_arrow < 1.5 and not (atk_obj_f and self._arrow_for(atk_obj_f)):
                time.sleep(0.02)
            out["arrow_wait"] = round(time.time() - t_arrow, 3)
            self.settle(0.05, 3)                                   # 箭头生成后至少再走几帧（它自己的 tick）
        # ---- 目标 actor 悬停（真实玩家把箭头拖到目标上方；红线：这一步不能省）----
        if hover_other:
            ocls = self.uclass_of_instance(hover_other)
            f_oe = self.find_fn(ocls, "OnActorMouseEnter")
            if f_oe:
                self.call0(hover_other, f_oe)
                self.settle(_hv, 6)
            self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch", inherited=True), hover_other)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
            out["hover_other"] = hex(hover_other)
        if pre_commit is not None:                                 # 提交前的只读诊断钩子（不写任何东西）
            try:
                out["pre_commit"] = pre_commit()
            except Exception as e_pc:                              # noqa: BLE001
                out["pre_commit_err"] = str(e_pc)
        # ---- L2：写 cursor + 箭头（与提交同一次 JS 执行）----
        writes = list(self._pc_writes(actor)) if l1 else []     # L1 再写一次（防 StartDrag 清空）
        for key, note in (("card_under_cursor", NOTE_OFF_CARD_UNDER_CURSOR),
                          ("location_number", NOTE_OFF_LOCATION_NUMBER),
                          ("location_enum", NOTE_OFF_LOCATION_ENUM),
                          ("row", NOTE_OFF_ROW_UNDER_CURSOR)):
            if cursor and key in cursor and cursor[key] is not None:
                kind = "s32" if key in ("card_under_cursor", "location_number") else "u8"
                writes.append((actor, self.off(cls, CURSOR_FIELD[key], note), kind,
                               int(cursor[key])))
        calls = []
        # ★ 2026-09-26：**没有目标卡、但要过长度闸门**（移动/上线）——
        #   板卡提交前 `IsTargetArrowLengthValid()` 要求 owner 箭头长度 > 3000，
        #   而 `OnActorMouseUp` 会先销毁箭头（长度在销毁时按 owner 卡回写）
        #   ⇒ 必须把 owner 箭头头部平面搬远 + 写回长度（同一次 JS 执行）。
        if arrow_target is None and arrow_length_gate:
            atk_obj_g = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            own_g = self._arrow_for(atk_obj_g) if atk_obj_g else 0
            out["gate_arrow"] = hex(own_g) if own_g else None
            if own_g:
                # ★ 2026-10-01（回合 9 实机：5th RANGERS 刚攻击完再上线被拒，落地后 `cardUnderCursor` 成了 41）：
                #   `OnActorMouseUp` 会用 `GetTargetArrowTargetCard` 读箭头的 `overCardID` 覆盖 `cardUnderCursor`，
                #   而箭头上还留着上一次攻击的目标 ⇒ 移动被当成"打 41"。移动/上线没有目标 ⇒ 把箭头的目标清零
                #   （同一次 JS 执行里写，和提交原子）。
                try:
                    acls_g = self.uclass_of_instance(own_g)
                    writes.append((own_g, self.off(acls_g, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID), "s32", 0))
                    writes.append((own_g, self.off(acls_g, "overCard", NOTE_OFF_ARROW_OVER_CARD), "ptr", 0))
                    out["gate_arrow_target_cleared"] = True
                except Exception as e_g:                       # noqa: BLE001
                    out["gate_arrow_clear_error"] = str(e_g)
                writes.append((actor, self.off(cls, "targetArrowFinalLength",
                                               NOTE_OFF_ARROW_FINAL_LENGTH), "f64", 4000.0))
                head_g = self.arrow_head_plane(own_g)
                if head_g:
                    f_rel_g = self.find_fn(self.uclass_of_instance(head_g),
                                           "K2_SetRelativeLocation")
                    if f_rel_g:
                        parms_g = bytearray(0x128)
                        struct.pack_into("<ddd", parms_g, 0x00, 4000.0, 0.0, 0.0)
                        calls.append([hex(head_g), hex(f_rel_g), bytes(parms_g).hex()])
                        out["gate_head_moved"] = hex(head_g)
        if arrow_target is not None:
            arrows = self.arrow_actors()
            if arrow_setter and arrows:
                try:
                    self.call_i32(arrows[0], self.fn(self.uclass_of_instance(arrows[0]),
                                                     "spectatorArrowNewTarget"),
                                  int(arrow_target))
                    out["arrow_setter"] = True
                except Exception as e:                         # noqa: BLE001
                    out["arrow_setter_error"] = str(e)
            tgt_actor = self.board_actor_of(arrow_target)
            tgt_obj = self.m.ptr(tgt_actor + NOTE_OFF_ACTOR_SELF_BASECARD) if tgt_actor else 0
            for a in (arrows or []):
                acls = self.uclass_of_instance(a)
                writes.append((a, self.off(acls, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID),
                               "s32", int(arrow_target)))
                if tgt_obj:
                    writes.append((a, self.off(acls, "overCard", NOTE_OFF_ARROW_OVER_CARD),
                                   "ptr", tgt_obj))
                # ★ 2026-10-01：真悬停时箭头自己的 tick 会把 `BoardCardUnderTargetArrow`（`BP_BoardCard_C*`，BP_targetArrowRVX.cpp:56/:776-804）
                #   设成目标的**视觉板卡 actor**；我们只写了 overCardID/overCard，这一项一直是 null。
                #   只在**反射链真的查得到**这个字段时才写（查不到不乱写偏移）。
                if tgt_actor:
                    try:
                        from kardsmem import props as _props
                        _pr = _props.find_prop(self.ks, acls, "BoardCardUnderTargetArrow", pool=self.pool)
                        if _pr and _pr.get("offset"):
                            writes.append((a, int(_pr["offset"]), "ptr", tgt_actor))
                            out["arrow_under_written"] = True
                    except Exception:                              # noqa: BLE001
                        pass
            out["arrows_written"] = len(arrows)
            # ★ 2026-09-26（`drag-chain-report.md` §6）：搬头平面/写长度必须认**owner 卡的那支箭头**。
            #   长度是在箭头销毁时按 **owner 卡**回写的（`BP_targetArrowRVX.cpp:953-955`），
            #   拿 `arrows[0]` 可能搬到别人的箭头 ⇒ `IsTargetArrowLengthValid` 过不了、提交静默失败。
            atk_obj_x = self.m.ptr(actor + NOTE_OFF_ACTOR_SELF_BASECARD) or 0
            owner_arrow = self._arrow_for(atk_obj_x) if atk_obj_x else 0
            head_arrow = owner_arrow or (arrows[0] if arrows else 0)
            out["head_arrow"] = hex(head_arrow) if head_arrow else None
            out["owner_arrow_matched"] = bool(owner_arrow)
            head = self.arrow_head_plane(head_arrow) if head_arrow else 0
            if head:
                f_rel = self.find_fn(self.uclass_of_instance(head), "K2_SetRelativeLocation")
                if f_rel:
                    parms = bytearray(0x128)
                    struct.pack_into("<ddd", parms, 0x00, 4000.0, 0.0, 0.0)
                    calls.append([hex(head), hex(f_rel), bytes(parms).hex()])
                    writes.append((actor, self.off(cls, "targetArrowFinalLength",
                                                   NOTE_OFF_ARROW_FINAL_LENGTH), "f64", 4000.0))
                    out["head_plane_moved"] = hex(head)
        # ★ 2026-10-01（`x_pre_commit` 对照）：攻击**成功**的提交前 PC.mouseOverActor = 目标（合成悬停还挂着），
        #   **失败**的提交前它已是 0 —— 真鼠标不在目标上时，游戏自己的帧 tick 会把它清掉（帧数等待让更多 tick 跑过）。
        #   真鼠标停在目标上就能打正是这个原因 ⇒ 在提交的同一次 JS 执行里把 PC.mouseOverActor 写回目标，
        #   提交时恒为"悬停在目标上"。（动作后 `pc_clear_hover` 仍会清零，不留悬空指针。）
        if hover_other:
            try:
                writes.append((pc, self.off(pc_cls, "mouseOverActor", NOTE_OFF_PC_MOUSE_OVER_ACTOR),
                               "ptr", int(hover_other)))
                out["pc_hover_rewritten"] = hex(int(hover_other))
            except Exception as e_h:                               # noqa: BLE001
                out["pc_hover_rewrite_err"] = str(e_h)
        # ---- L3：落地口 ----
        # ★ 2026-09-26（`drag-chain-report.md` §1/§2）：落地口**按子类**，而 `BP_BaseCard`
        #   自己的 `OnActorMouseUp` 是**空壳**（BP_BaseCard.cpp:1186-1187）。
        #   `inherited=True` 有可能解析到基类那个空壳 ⇒ 调了等于没调（症状正是
        #   "预检全过、箭头也设了、动作流零新增、无提示"）⇒ **先认 actor 自己类上的实现**。
        f_up = (self.find_fn(cls, "OnActorMouseUp", inherited=False)
                or self.find_fn(cls, "OnActorMouseUp", inherited=True))
        f_end = (self.find_fn(cls, "OnActorEndDrag", inherited=False)
                 or self.find_fn(cls, "OnActorEndDrag", inherited=True))
        out["commit_fns_own_class"] = {
            "mouse_up": bool(self.find_fn(cls, "OnActorMouseUp", inherited=False)),
            "end_drag": bool(self.find_fn(cls, "OnActorEndDrag", inherited=False))}
        seq = []
        if commit == "both":
            seq = [f for f in (f_up, f_end) if f]
        elif commit == "mouse_up":
            seq = [f for f in (f_up,) if f] or [f for f in (f_end,) if f]
        elif commit == "end_drag":
            seq = [f for f in (f_end,) if f] or [f for f in (f_up,) if f]
        for f in seq:
            calls.append([hex(actor), hex(f), ""])
        if cleanup and l1:
            f_rel_pc = self.find_fn(self.uclass_of_instance(pc), "ForceReleaseDrag")
            if f_rel_pc:
                calls.append([hex(pc), hex(f_rel_pc), ""])
        out["commit_calls"] = [hex(f) for f in seq]
        out["layers"] = {"L0": l0, "L1": l1, "L3": commit}
        if writes or calls:
            # 走 L1 原语（§2：手势不得自己拼 JS RPC 调用；编码在 primitives.encode_writes）
            out["writeback"] = P.write_then_calls(self.api(), writes, calls)
        out["after"] = self.pc_drag_state()
        if verbose:
            print("    drag_release commit=%s seq=%s cursor=%s arrow=%s queue_idle=%s"
                  % (commit, out["commit_calls"], cursor, arrow_target, out.get("queue_idle")))
        return out

    def click_actor(self, actor: int, *, is_precise: int = 1,
                    queue_gate: bool = True, hover_pause: Optional[float] = None,
                    actor_mouse_up: bool = True, cleanup: bool = True,
                    verbose: bool = True) -> dict:
        """★★ **统一点击原语** —— 复刻 PC 自己的真实松手流程
        （`BP_PlayerController.cpp:683-748`，逐行）：

            683: `BP_Logic::GlobalMouseUp(mouseDownActor, &bWasConsumed)`
            687: `if (!bWasConsumed) {`
            705:     `actor->OnActorMouseUp()`
            748:     `actor->OnActorClicked(IsPrecise)`
               `}`

        ⇒ **点击 ≠ 直接调 `OnActorClicked`**：先要 `GlobalMouseUp(mouseDownActor)`，
        只有它**没吃掉**这次点击（`consumed == false`）时才转发给 actor。
        参数是 **`mouseDownActor`**（L1 那个字段）⇒ 所以点击同样需要 L1 的 PC 状态。

        分层：L0 队列闸门 → 悬停(+转发) →（可选 `KARDS_CLICK_PC=1`）`GlobalMouseUp`
        → `OnActorClicked(IsPrecise)`。⛔ **不碰任何 PC 鼠标字段/手柄 API**（用户定调 + 事故根因）。
        用于：抉择/预报候选、选手牌当目标、换牌标记……（widget 按钮另走它们的 `BndEvt__*`）。
        """
        if not self.obj_alive(actor):                              # 对已销毁对象调 ProcessEvent 会 AV（见 obj_alive）
            return {"actor": hex(actor), "ok": False, "reason": "actor_not_alive",
                    "error": "目标 actor 已被销毁/回收（陈旧候选），不点"}
        cls = self.uclass_of_instance(actor)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        logic = self.logic_actor()
        lcls = self.uclass_of_instance(logic)
        out = {"actor": hex(actor)}
        # L0
        if queue_gate:
            out["queue_idle"] = self.wait_queue_idle()
        # ⛔ 不调 `pc_drag_end()`：它内部 `ForceReleaseDrag()` 会额外触发一次 EndDrag
        #   （候选卡点完即自毁 ⇒ 撞成 use-after-free，见 `click-crash-report.md`）。
        # L1：悬停（红线：悬停必须发生）+ 转发
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        if f_enter:
            self.call0(actor, f_enter)
            self.settle(SETTLE_HOVER if hover_pause is None else hover_pause, 6)
            out["hovered"] = True
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        if f_disp:
            self.call_ptr(pc, f_disp, actor)
            self.settle(SETTLE_AFTER_DISPATCH, 2)
            out["hover_dispatched"] = True
        # ★ 2026-09-26：**新点击流程默认关**（曾疑似把游戏点崩）—— 默认走旧行为
        #   （只 hover + 转发 + `OnActorClicked`）；要试 PC 真实流程设 `KARDS_CLICK_PC=1`。
        import os as _os2
        pc_flow = _os2.environ.get("KARDS_CLICK_PC", "0") == "1"
        out["pc_flow"] = pc_flow
        out["gamepad_api_used"] = False       # ★ 用户定调：点击不用 gamepad/手柄 API
        # ⛔ 不调 `SetMouseDownActorManual`（手柄 API）、不写 PC 鼠标字段、不调 `OnActorMouseDown`：
        #   伪造 leftButtonDown 会让游戏自己补一次 EndDrag，和我们的清理撞成两次 ⇒ use-after-free。
        # 同一次执行：写 PC 拖拽字段 + GlobalMouseUp(actor) [+ actor.OnActorMouseUp()]
        f_gmu = self.find_fn(lcls, "GlobalMouseUp") or self.find_fn(lcls, "GlobalMouseUpBattle")
        f_up = self.find_fn(cls, "OnActorMouseUp")
        writes = []          # ★ 点击链不写任何 PC 字段（只用 GlobalMouseUp 的问询语义）
        jw = []
        consumed = None
        if f_gmu and pc_flow:
            parms = bytearray(0x10)
            struct.pack_into("<Q", parms, 0x00, int(actor))      # mouseDownActor @0x00
            try:
                buf = self.api().call_raw_writes(writes, logic, f_gmu, bytes(parms))
                out["gmu_writeback"] = buf
                consumed = bool(buf[0x08]) if buf and len(buf) > 0x08 else None
            except Exception as e:                              # noqa: BLE001
                out["gmu_error"] = str(e)
        else:
            out["gmu_missing"] = True
        out["consumed"] = consumed
        # 未被消费 ⇒ 按真实输入转发给 actor（MouseUp → 再 Clicked）
        if actor_mouse_up and f_up and consumed is not True:
            try:
                self.call0(actor, f_up)
                self.settle(0.08, 4)  # 松开→Clicked 之间的按下态要渲染出来 ⇒ 按帧等（失焦降帧时旧写法会一帧都不画）
            except Exception as e:                              # noqa: BLE001
                out["up_error"] = str(e)
        if consumed is not True:
            f_click = self.find_fn(cls, "OnActorClicked")
            if f_click:
                self.call_i32(actor, f_click, int(is_precise))
                out["clicked"] = True
                self.settle(0.35, 6)  # 点击后等界面反应 ⇒ 按帧等（降帧时 0.35 s 里可能一帧没跑）
            else:
                out["click_missing"] = True
        if verbose:
            print("    click_actor 0x%X consumed=%s clicked=%s queue_idle=%s gamepad_api=False"
                  % (actor, out.get("consumed"), out.get("clicked"), out.get("queue_idle")))
        return out

    def _drag_lifecycle(self, actor: int, location_enum: int, location_number: int,
                        card_at_location: int = 0, verbose: bool = True,
                        hover_pause: Optional[float] = None,
                        use_mouse_up: bool = False,
                        also_mouse_up: bool = False) -> dict:
        """复刻真实鼠标拖拽：悬停 → 悬停转发 → 按下 → 起拖 → 拖动 tick → 写落点 → 落地。

        `hover_pause` 给了就用它当"在牌上停多久"（默认 `SETTLE_HOVER`）——
        想看 hover 效果时把它调大，眼睛跟得上。

        ★ `use_mouse_up=True` 用于**板卡**（移动/上线）：这类 actor 的提交口是
          **`OnActorMouseUp`（松手）**，不是 `OnActorEndDrag`（那是手牌那条，已证）。
          2026-09-25 实测：用 EndDrag 提交移动时，读回 `targetArrowFinalLength=0.0`、
          `IsTargetArrowLengthValid=0` —— 落点根本没人消费，游戏一点反应都没有。
          并且"写落点 + 松手"必须塞进**同一次 JS 执行**（`write_and_call`），
          否则中间插帧会把 cursor 字段按真实鼠标重算掉（和箭头那次同一个竞态）。

        ★ 2026-09-26 重构：本体**委托给统一原语 `drag_release()`**（L0 队列闸门 + L1 PC 拖拽
          状态机 + L2 cursor 组 + L3 落地口）。旧签名与返回键保留，调用方不用改。
          出牌/移动由此自动获得：`queueIsRunning` 闸门、`SetMouseDownActorManual`、
          `ForceReleaseDrag` 收尾、以及提交前再写一遍 PC 拖拽字段。
        """
        out = self.drag_release(
            actor,
            cursor={"card_under_cursor": card_at_location,
                    "location_number": location_number,
                    "location_enum": location_enum, "row": 1},
            commit=("mouse_up" if use_mouse_up else "end_drag"),
            arrow_length_gate=bool(use_mouse_up),   # 板卡（移动/上线）要过长度闸门
            # ★ 2026-09-26 更正（`drag-chain-report.md` §4 抓到的错）：这里原来写死
            #   `commit="both"`，注释还说"与重构前一致"——**不实**。老版
            #   （`old_ops/inject.py`）手牌只发 `OnActorEndDrag`。
            #   而板卡的 `OnActorMouseUp` 在 `PlaceBoardCard()` 前有双闸
            #   `CanPlayCard() && IsTargetArrowLengthValid()`（BP_BoardCard.cpp:1404-1411），
            #   且 :1402 会**先把箭头全销毁** ⇒ 没有箭头/长度时它静默早退，
            #   再补一个 `OnActorEndDrag` 也救不回。⇒ 按子类给落地口：
            #   手牌 `end_drag`（老版行为）、板卡 `mouse_up`（+ 必须给 `arrow_target`）。
            hover_pause=hover_pause,
            verbose=verbose)
        # 兼容旧返回键
        out["commit_fn"] = ("OnActorMouseUp+OnActorEndDrag" if len(out.get("commit_calls") or []) > 1
                            else ("OnActorMouseUp" if out.get("commit_calls")
                                  else "（找不到任何提交函数）"))
        out["cursor_state"] = {
            "cardUnderCursor": self.peek(actor, self.off(self.uclass_of_instance(actor),
                                                         "cardUnderCursor",
                                                         NOTE_OFF_CARD_UNDER_CURSOR), "s32"),
            "LocationNumberUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "LocationNumberUnderCursor",
                NOTE_OFF_LOCATION_NUMBER), "s32"),
            "LocationUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "LocationUnderCursor",
                NOTE_OFF_LOCATION_ENUM), "u8"),
            "RowUnderCursor": self.peek(actor, self.off(
                self.uclass_of_instance(actor), "RowUnderCursor",
                NOTE_OFF_ROW_UNDER_CURSOR), "u8"),
        }
        return out

    def remove_target_arrow(self, verbose: bool = True) -> dict:
        """跑游戏自己的 `BP_Logic_C::RemoveTargetArrow()` —— **清掉场上遗留的箭头**。

        ★ 为什么需要：`BP_Logic_C::GetTargetArrowTargetCard` 读的是
          `GetAllActorsOfClass(BP_targetArrowRVX_C)[0]->overCardID`（1.58 导出 BP_Logic.cpp:7051）
          ⇒ 场上只要有一支**遗留箭头**，游戏就可能读到错的那一支（handoff §十五6 那个坑）。
          我们每试坏一次就留一支 ⇒ **下次实验前先用游戏自己的函数清干净**，
          别带着残渣做实验（用户 2026-09-25 提醒："第一次的箭头还在"）。
        """
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C"}
        lcls = self.uclass_of_instance(logic)
        f = self.find_fn(lcls, "RemoveTargetArrow")
        if not f:
            return {"ok": False, "stopped": "BP_Logic_C 上没有 RemoveTargetArrow"}
        self.call0(logic, f)
        self.settle(0.4, 6)           # 等引擎真的把箭头收掉再数剩余数量 ⇒ 按帧等
        left = len(self.arrow_actors())
        if verbose:
            print("    清箭头：RemoveTargetArrow 已调用，场上还剩 %d 支" % left)
        return {"ok": left == 0, "arrows_left": left}

    def aim_arrow(self, target_card_id: int, verbose: bool = True) -> dict:
        """把**场上那支箭头**指向目标卡：写 `overCardID` + 悬停目标 actor（+ 转发）。

        真实玩家"把箭头移到目标卡上方"产生的就是这两件事：
          * 箭头自身的 `overCardID`（`BP_Logic::GetTargetArrowTargetCard` 读的就是它，
            取 `GetAllActorsOfClass(箭头)[0]->overCardID`）—— 场上有几支就都写；
          * 目标 actor 收到 `OnActorMouseEnter` + `MouseHoverDispatch`
            （**这一步游戏会做目标校验并给出 reason**，也是转发给对手的那一步）。

        ★ 用户 2026-09-25 指点的两阶段流程：**先把牌拖到落点"部署"，此时箭头才显示；
          然后才"选目标"** —— 所以 aim 是**第二个动作**，不能和部署挤在一次。
        """
        tgt_actor = self.board_actor_of(target_card_id)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        arrows = self.arrow_actors()
        writes = []
        for a in arrows:
            acls = self.uclass_of_instance(a)
            writes.append((a, self.off(acls, "overCardID", NOTE_OFF_ARROW_OVER_CARD_ID),
                           "s32", int(target_card_id)))
        res = {"ok": bool(arrows), "arrows": len(arrows), "target_actor": hex(tgt_actor or 0)}
        if writes:
            res["writeback"] = self.write_and_call(writes, None, 0)   # 只写，不调用
        if tgt_actor:
            f_te = self.find_fn(self.uclass_of_instance(tgt_actor), "OnActorMouseEnter")
            if f_te:
                self.call0(tgt_actor, f_te)
                self.settle(SETTLE_HOVER, 6)
            f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
            if f_disp:
                self.call_ptr(pc, f_disp, tgt_actor)
                self.settle(SETTLE_AFTER_DISPATCH, 2)
            res["hovered_target"] = True
        if verbose:
            print("    箭头指向 %s：箭头数=%d 目标actor=%s 悬停=%s"
                  % (target_card_id, len(arrows), res["target_actor"], res.get("hovered_target")))
        return res

    def board_card_screen_pos(self, card_id: int):
        """场上卡 → **屏幕像素**（(viewport_x, viewport_y, screen_x, screen_y, hwnd)）。

        `AActor::K2_GetActorLocation()`（原生 const，出参 `FVector@0x00`，ParmsSize 0x18）
        → `APlayerController::ProjectWorldLocationToScreen(FVector@0x00, FVector2D*@0x18,
           bool bPlayerViewportRelative@0x28, bool* ReturnValue@0x29)`（ParmsSize 0x30）
        → 视口坐标 → `win.client_to_screen()` 换成桌面像素。
        **两个都是原生只读**，不写任何状态。

        ★ 用途（2026-09-25 更正过一次）：这是**只读几何**，跟"要不要用真实鼠标"无关。
          曾用它给"物理鼠标点目标"那条路求坐标 —— **那条路用户已经否掉**
          （"不要真实鼠标"），物理鼠标的 `mouse_to_target`/`click_target_with_mouse`
          两个函数已删除。留着它是为了**交叉核对**：算出来的屏幕坐标可以用来确认
          "光标底下到底是哪张卡"（例如 `FindCardLocationUnderCursor` 的对照），
          它本身不写任何状态、不碰鼠标。
        """
        atk = self.board_actor_of(card_id)
        if not atk:
            return None
        acls = self.uclass_of_instance(atk)
        f_loc = self.find_fn(acls, "K2_GetActorLocation")
        if not f_loc:
            return None
        out = P.call_ufunction(self.api(), atk, f_loc, "00" * 0x18)
        buf = bytes(out) if out else b""
        if len(buf) < 0x18:
            return None
        wx, wy, wz = struct.unpack_from("<ddd", buf, 0)
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        f_prj = self.find_fn(pcls, "ProjectWorldLocationToScreen")
        if not f_prj:
            return None
        parms = bytearray(0x30)
        struct.pack_into("<ddd", parms, 0x00, wx, wy, wz)
        out2 = P.call_ufunction(self.api(), pc, f_prj, parms)
        buf2 = bytes(out2) if out2 else b""
        if len(buf2) < 0x30 or not buf2[0x29]:
            return None
        vx, vy = struct.unpack_from("<dd", buf2, 0x18)
        # ★ 2026-09-27：**不再 import ops**（旧鼠标实现要归档）—— 只要一个窗口句柄，
        #   直接用 `base/winapi.py` 找游戏窗口，**不置前**（只读诊断）。
        from base import winapi
        h = winapi.find_game_hwnd()
        if not h:
            return None
        sx, sy = winapi.client_to_screen(h, int(vx), int(vy))
        return (int(vx), int(vy), int(sx), int(sy), h)

    def click_board_card(self, card_id: int, verbose: bool = True) -> dict:
        """**点一张场上的卡**（复刻真实左键单击）：悬停 → 悬停转发 → 按下 → 松开。

        ⚠ 2026-09-25 更正：**这不是"部署后选目标"的做法**（旧注释在这里写错过）。
          选目标那条路的提交在 `BP_Logic::GlobalMouseUp` →
          `GlobalMouseUpBattle`（`PlaceHandCard(locNum, targetCardID)`），而且真人在
          那种状态下点目标时 PC 因为 `consumed=true` **不会**把鼠标松开转发给目标
          actor ⇒ 用 `select_target()`/`select_unit_target()`，别用这个。
          这个函数留着当**通用"点一张板卡"**（悬停+按下+松开都发，含转发给对手），
          用于需要"我点一下这张卡"的其它场合。
        悬停那一步不能省：**游戏在这一步做目标校验并给 reason**，而且真人点击本来
        就会先悬停；`MouseHoverDispatch` 是转发给对手的那一步。
        """
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的 actor" % card_id}
        cls = self.uclass_of_instance(atk)
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        f_down = self.find_fn(cls, "OnActorMouseDown")
        f_up = self.find_fn(cls, "OnActorMouseUp")
        f_disp = self.find_fn(pc_cls, "MouseHoverDispatch")
        if f_enter:
            self.call0(atk, f_enter)
            self.settle(SETTLE_HOVER, 6)
        if f_disp:
            self.call_ptr(pc, f_disp, atk)          # 悬停转发（对手看得到的那一步）
            self.settle(SETTLE_AFTER_DISPATCH, 2)
        if f_down:
            self.call0(atk, f_down)
            self.settle(SETTLE_AFTER_DOWN, 2)
        if f_up:
            self.call0(atk, f_up)
        out = {"ok": True, "card_id": card_id, "actor": hex(atk),
               "send": {"enter": bool(f_enter), "down": bool(f_down), "up": bool(f_up),
                        "hover_dispatch": bool(f_disp)}}
        if verbose:
            print("    点场上卡 %s(actor=0x%X)：悬停→转发→按下→松开" % (card_id, atk))
        self.settle(0.4, 6)           # 点完等界面反应 ⇒ 按帧等（降帧时不空等）
        return out

    def actor_bounds(self, actor: int):
        """`Actor::GetActorBounds(false, Origin, BoxExtent, false)` → `(origin, extent)`；只读。

        参数布局（**当前构建**）：`reverse-data/sdk/1.60.27292.launcher/CppSDK/SDK/
        Engine_parameters.hpp:3520` —— `bOnlyCollidingComponents@0x00` / `Origin@0x08` /
        `BoxExtent@0x20` / `bIncludeFromChildActors@0x38`（ParmsSize 0x40）。
        """
        if not actor:
            return None
        f = self.find_fn(self.uclass_of_instance(actor), "GetActorBounds")
        if not f:
            return None
        parms = bytearray(0x40)
        out = P.call_ufunction(self.api(), actor, f, parms)
        buf = bytes(out) if out else b""
        if len(buf) < 0x38:
            return None
        return (struct.unpack_from("<ddd", buf, 0x08),
                struct.unpack_from("<ddd", buf, 0x20))

    # 攻击：真实攻击走的是 **目标箭头**（BP_targetArrowRVX_C），不是拖拽落地。
    def _hover_actor(self, actor: int, seconds: float, dispatch: bool = True,
                     leave: bool = False) -> dict:
        """悬停一个 actor：`OnActorMouseEnter` → 停留 `seconds` → `MouseHoverDispatch`（转发给对手）
        → （`leave=True`）`OnActorMouseExit`。

        悬停是**发给对手的信息流**的一部分，所以哪怕很短也必须真的发生（不许 0 秒）。

        ★ 2026-09-30 用户点破"我们的悬停和真人悬停有差异，手牌悬停预览总是异步延迟加载"。
          实测（`census.py` 对 GUObjectArray 做类名计数、前后做差）：真人悬停 10 张手牌后
          预览相关对象**净减少**；我们合成悬停 10 次后 **+10 `TextureRenderTarget2D` / +10
          `W_CardHelpPage_C` / `cardHelpBar_Widget_PC_C` / `BP_HandCardLookUnlit_C` /
          `BP_Widget_HandCardTextV2_C` / `BP_Widget_DeckBoxLabel_C`，+20 `cardHelp_Widget_PC_C`**，
          等 60 s 也不回收。原因（读 `BP_PlayerController` 的 tick）：真鼠标换目标是
          「新卡 Enter → `MouseHoverDispatch(新卡)` → **旧卡 `OnActorMouseExit`**」，移到空处是
          `mouseOverActor=null` 再 `OnActorMouseExit`；我们只做了前半段，预览从不销毁。
          ⇒ `leave=True`：停留、转发之后补上 `OnActorMouseExit`（那个类没有 Exit 就跳过）。
        """
        cls = self.uclass_of_instance(actor)
        f_enter = self.find_fn(cls, "OnActorMouseEnter")
        pc = player_controller(self.ks)
        f_disp = self.find_fn(self.uclass_of_instance(pc), "MouseHoverDispatch")
        out = {"ok": True, "actor": hex(actor), "seconds": float(seconds)}
        t0 = time.time()
        if f_enter:
            self.call0(actor, f_enter)
        self.settle(seconds, 6)       # 悬停停留按帧等：失焦降帧时也要给游戏足够帧数才算"停留过"
        if dispatch and f_disp:
            out["dispatched"] = self.call_ptr(pc, f_disp, actor)
        if leave:
            f_exit = self.find_fn(cls, "OnActorMouseExit")
            if f_exit:
                self.call0(actor, f_exit)
                out["left"] = True
            # 实测（census.py）：只补 Exit 预览仍不销毁（每次悬停仍 +1 个存活的
            # BP_HandCardLookUnlit_C 展示卡 + 一组 cardHelp 控件/渲染目标，3 s 与 0.3 s 停留都一样，
            # 5 分钟不回收）。游戏自己的清理入口是基类 `BP_BaseCard::DestroyShowCaseCard(bJustMine)`
            # （false ⇒ `BattleUtilityFunctions::DestroyShowCaseCardUtility`：销毁世界里的展示卡）。
            f_dsc = self.find_fn(cls, "DestroyShowCaseCard")
            if f_dsc:
                self.call_i32(actor, f_dsc, 0)
                out["showcase_destroyed"] = True
        out["dwell"] = round(time.time() - t0, 2)
        return out

    def hover_board_card(self, card_id: int, seconds: Optional[float] = None,
                         dispatch: bool = True, verbose: bool = True) -> dict:
        """**只做悬停**（场上卡）：`OnActorMouseEnter` → 停留 → 悬停转发。

        用途（用户 2026-09-26 要求）：`agent.session.inspect()` 看一张**场上**卡时，
        真实玩家一定会把它悬停住（卡面高亮/大卡信息面板），而且这一步**对手看得到**
        （`MouseHoverDispatch`）⇒ 属于"发出去的信息流"的一部分，**悬停必须留下**。

        `seconds` 默认 `SETTLE_HOVER_INSPECT`（**1.5 s**，刻意看牌的量级）。
        没有 `OnActorMouseLeave`（`BP_BoardCard_classes.hpp` 只有 `MouseEnter`/`MouseHover`）
        ⇒ 结束时不"离开"，卡保持高亮（真人不移开鼠标也是这样）。
        """
        sec = SETTLE_HOVER_INSPECT if seconds is None else max(SETTLE_FLOOR, seconds)
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "error": "场上找不到 card %s 的板卡 actor" % card_id}
        out = self._hover_actor(atk, sec, dispatch)
        out["card_id"] = card_id
        if verbose:
            print("    hover 场上卡 %s %.2fs（转发=%s）" % (card_id, sec, out.get("dispatched")))
        return out

    def hover_hand_card(self, card_id: int, seconds: Optional[float] = None,
                        dispatch: bool = True, verbose: bool = True) -> dict:
        """**只做悬停**（我方手牌卡，actor = `BP_HandCard_C`）。

        用户 2026-09-26："inspect **友方手牌**也 hover 1.5s"。
        悬停我方手牌同样会被转发给对手（`MouseHoverDispatch`）⇒ 短可以，但不能没有。
        """
        sec = SETTLE_HOVER_INSPECT if seconds is None else max(SETTLE_FLOOR, seconds)
        actor = (self.hand_actor(card_id) or {}).get("actor") or 0
        if not actor:
            return {"ok": False, "error": "找不到 card %s 的手牌 actor" % card_id}
        out = self._hover_actor(actor, sec, dispatch, leave=True)
        out["card_id"] = card_id
        if verbose:
            print("    hover 手牌 %s %.2fs（转发=%s）" % (card_id, sec, out.get("dispatched")))
        return out

    def arrow_actors(self) -> list:
        from kardsmem.world import Locator
        loc = Locator(self.m, self.ks.base)
        out = []
        for p in (loc.actors() or []):
            c = self.oa.class_of(p)
            if c and self.pool.fname_of(c) == "BP_targetArrowRVX_C":
                out.append(p)
        return out

    def _arrow_for(self, actor_card: int) -> int:
        """找**这次拖拽的**箭头：`fromCard == 攻击者的卡对象`。

        箭头会跨拖拽复用，而且**新箭头在同一个拖拽里提交是不生效的** ——
        所以认它不能靠"新的那一个"，要靠 fromCard 绑定。
        """
        for a in self.arrow_actors():
            if self.m.ptr(a + NOTE_OFF_ARROW_FROM_CARD) == actor_card:
                return a
        return 0

    def arrow_target_by_logic(self) -> dict:
        """**游戏自己要读的那个目标**：`BP_Logic_C::GetTargetArrowTargetCard(card, hasTarget)`。

        ★ 只读诊断。它取 `GetAllActorsOfClass(BP_targetArrowRVX_C)[0]->overCardID`
          再 `GetCardFromID(...)`（1.58 导出 `BP_Logic.cpp:7051-7091`）——
          **取的是第 0 个箭头**：所以我们只给"自己认的那个箭头"写 overCardID 不够，
          场上只要有别的箭头（初始化那次空拖留下的、上一轮没清掉的）、而它恰好排第 0，
          游戏读到的就是 0/老值 ⇒ `hasTarget=false` ⇒ 提交被静默跳过。
        """
        logic = self.logic_actor()
        if not logic:
            return {"error": "没有 BP_Logic_C 实例"}
        lcls = self.uclass_of_instance(logic)
        f = self.find_fn(lcls, "GetTargetArrowTargetCard")
        if not f:
            return {"error": "找不到 GetTargetArrowTargetCard"}
        out = P.call_ufunction(self.api(), logic, f, b"\x00" * 0x10)
        if not out or len(out) < 0x10:
            return {"error": "没返回"}
        card = struct.unpack_from("<Q", bytes(out), 0x00)[0]
        res = {"has_target": bool(out[0x08]), "card_obj": hex(card) if card else None,
               "arrows": [{"actor": hex(a),
                           "over_card_id": self.peek(a, NOTE_OFF_ARROW_OVER_CARD_ID, "s32")}
                          for a in self.arrow_actors()]}
        if card:
            try:
                ocls = self.uclass_of_instance(card)
                res["card_id"] = self.peek(card, self.off(ocls, "CardID", 0x003C), "s32")
            except Exception:                                      # noqa: BLE001
                res["card_id"] = None
        return res

    def _drag_lead(self, actor: int, cls: int, pc: int, pc_cls: int) -> int:
        """拖拽的"前半段"：悬停 → 悬停转发 → 按下 → 起拖 → 拖动 tick。

        起拖这一步会让引擎 Spawn 出 `BP_targetArrowRVX_C`（如果这颗棋子还没有）。
        返回 `OnActorStartDrag` 的 success 出参。
        """
        self.call0(actor, self.fn(cls, "OnActorMouseEnter"))
        self.settle(SETTLE_HOVER, 6)
        self.call_ptr(pc, self.fn(pc_cls, "MouseHoverDispatch"), actor)
        self.settle(SETTLE_AFTER_DISPATCH, 2)
        self.call0(actor, self.fn(cls, "OnActorMouseDown"))
        self.settle(SETTLE_AFTER_DOWN, 2)
        succ = self.call_ptr(actor, self.fn(cls, "OnActorStartDrag"), 0)
        self.settle(SETTLE_AFTER_DOWN, 2)
        self.call0(actor, self.fn(cls, "OnActorDragTick"))
        self.settle(SETTLE_AFTER_TICK, 2)
        return succ

    def arrow_head_plane(self, arrow: int) -> int:
        """`BP_targetArrowRVX_C::arrowHeaderPlane` —— 箭头头部平面组件（`UStaticMeshComponent*`）。

        `GetArrowLength()` = `|头部平面.GetComponentLocation() − 箭头.GetActorLocation()|²`
        （1.58 导出 `BP_targetArrowRVX.cpp:1855`），而 `IsTargetArrowLengthValid()`
        要求它 `> 3000`（桌面）/5000（移动端）。真人拖拽会把头部平面拖远 ⇒ 天然满足；
        我们不动真实鼠标 ⇒ 它只有一百多 ⇒ **提交被静默跳过**。
        所以要把这个平面搬远 —— 纯客户端几何，不影响发给服务端的信息流。
        """
        off = self.off(self.uclass_of_instance(arrow), "arrowHeaderPlane",
                       NOTE_OFF_ARROW_HEAD_PLANE)
        return self.m.ptr(arrow + off) or 0

    def actor_location(self, actor: int):
        """`AActor::K2_GetActorLocation()`（原生 const，出参 FVector@0x00，ParmsSize 0x18）—— 只读。"""
        cls = self.uclass_of_instance(actor)
        f = self.find_fn(cls, "K2_GetActorLocation")
        if not f:
            return None
        out = P.call_ufunction(self.api(), actor, f, "00" * 0x18)
        buf = bytes(out) if out else b""
        return struct.unpack_from("<ddd", buf, 0) if len(buf) >= 0x18 else None
