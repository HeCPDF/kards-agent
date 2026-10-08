# -*- coding: utf-8 -*-
"""ops/cli.py —— 自检（只解析、不动手）与命令行入口 `main`。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
`main` 里唯一的改动：函数体第一行 `from ops.inject import Injector`（原来 `Injector` 与 `main` 同在一个文件，现在避免循环 import）。
"""
from __future__ import annotations

from kardsmem import build as B
from kardsmem.pick import hand_card_actors, player_controller
from ops.consts import (
    LOC_BOARD_FRONTLINE, NOTE_OFF_CARD_UNDER_CURSOR, NOTE_OFF_LOCATION_ENUM, NOTE_OFF_LOCATION_NUMBER,
    NOTE_OFF_ROW_UNDER_CURSOR,
)


class SelftestMixin:
    """自检（只解析、不动手）与命令行入口 `main`。"""

    # ------------------------------------------------------------ 自检
    def selftest(self) -> dict:
        """只解析、不动手：把所有要用的函数/偏移/实例都解析一遍并打印。"""
        rep = {"ok": True, "items": [], "notes": []}
        st = None
        try:
            from kardsmem import board as BA
            st = BA.open_source("mem").snapshot()
        except Exception as e:                               # noqa: BLE001
            rep["ok"] = False
            rep["items"].append(("snapshot", "FAIL", str(e)))
            return rep

        rep["items"].append(("对局", "in_match",
                             "cards=%d turn=%s our_turn=%s kredits=%s"
                             % (len(st.cards), st.turn, st.our_turn, st.kredits)))
        rep["items"].append(("my_side", "ok", self.my_side()))
        try:
            rep["items"].append(("我方后排枚举", "ok", self.our_back_enum()))
            rep["items"].append(("空槽位", "ok", self._free_slot()))
        except Exception as e:                               # noqa: BLE001
            rep["ok"] = False
            rep["items"].append(("落点枚举", "FAIL", str(e)))

        hud = self.hud_actor()
        rep["items"].append(("HUD 实例", "ok" if hud else "FAIL", hex(hud) if hud else "没找到"))
        if hud:
            hud_cls = self.uclass_of_instance(hud)
            btn = self.end_turn_button(hud)
            rep["items"].append(("EndTurnButton", "ok" if btn else "FAIL", hex(btn) if btn else "空"))
            btn_cls = self.uclass_of_instance(btn) if btn else 0
            if btn_cls:
                for nm in ("BndEvt__Button_0_K2Node_ComponentBoundEvent_1_OnButtonHoverEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_5_OnButtonPressedEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_7_OnButtonReleasedEvent__DelegateSignature",
                           "BndEvt__Button_0_K2Node_ComponentBoundEvent_0_OnButtonClickedEvent__DelegateSignature"):
                    rep["items"].append(("按钮句柄", nm.split("_")[-1], hex(self.find_fn(btn_cls, nm))))
                rep["items"].append(("HUD 句柄", "click249",
                                     hex(self.find_fn(hud_cls, "BndEvt__EndTurnButton_K2Node_ComponentBoundEvent_249_OnButtonClickedEvent__DelegateSignature"))))

        hand = hand_card_actors(self.ks)
        rep["items"].append(("我方手牌 actor", "count=%d" % len(hand),
                             [(r.get("card_id"), r.get("name")) for r in hand]))
        if hand:
            cls = self.uclass_of_instance(hand[0]["actor"])
            for nm in ("OnActorMouseEnter", "OnActorMouseDown", "OnActorStartDrag",
                       "OnActorDragTick", "OnActorEndDrag"):
                f = self.find_fn(cls, nm)
                rep["items"].append(("手牌手势", nm, hex(f) if f else "缺失"))
                if not f:
                    rep["ok"] = False
            for nm, note in (("cardUnderCursor", NOTE_OFF_CARD_UNDER_CURSOR),
                             ("LocationNumberUnderCursor", NOTE_OFF_LOCATION_NUMBER),
                             ("LocationUnderCursor", NOTE_OFF_LOCATION_ENUM),
                             ("RowUnderCursor", NOTE_OFF_ROW_UNDER_CURSOR)):
                rep["items"].append(("落点字段", nm, "0x%X" % self.off(cls, nm, note)))
        pc = player_controller(self.ks)
        pc_cls = self.uclass_of_instance(pc)
        rep["items"].append(("PC 悬停转发", "MouseHoverDispatch",
                             hex(self.find_fn(pc_cls, "MouseHoverDispatch"))))
        rep["items"].append(("ProcessEvent RVA", "0x%X" % B.RVA["UObject_ProcessEvent"],
                             "vtable idx 0x%X" % B.PROCESS_EVENT_IDX))
        rep["notes"] = list(self.notes)
        return rep


def main(argv=None) -> int:
    from ops.inject import Injector
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    cmd = argv[0] if argv else "selftest"
    inj = Injector()
    try:
        if cmd == "selftest":
            rep = inj.selftest()
            print("=== ops_inject selftest ===")
            for group, name, val in rep["items"]:
                print("  %-12s %-42s %s" % (group, name, val))
            if rep["notes"]:
                print("--- 备注 ---")
                for n in rep["notes"]:
                    print("  " + n)
            print("结论:", "OK" if rep["ok"] else "有 FAIL")
            return 0 if rep["ok"] else 1
        if cmd == "end":
            print(inj.end_of_turn())
            return 0
        if cmd == "play":
            print(inj.play_card(int(argv[1])))
            return 0
        # ---- 游戏自己的"粗判断"API（NN/前端预检用；只读、不改对局） ----
        if cmd == "canplay":                     # 打牌总闸（含指挥点/支援线/限制/指向）
            r = inj.game_can_play_card_from_hand(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "cando":                       # 这张牌这回合还能不能做事
            r = inj.game_can_card_do_anything(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "cani":                        # 我方还有没有事可做
            r = inj.game_can_i_do_anything(verbose=True)
            print(r)
            return 0 if r.get("can") else 1
        if cmd == "precheck":                    # 总闸 + 下钻取理由
            print(inj.precheck_play(int(argv[1]), verbose=True))
            return 0
        if cmd == "canfront":                    # 上线判据（默认**不问**：问它要伪造拖拽态）
            # canfront <card_id> [--simulate-drag]   ← 诊断口，会写 PC->SelectedCard
            sim = "--simulate-drag" in argv[1:]
            print(inj.can_move_to(int(argv[1]), LOC_BOARD_FRONTLINE, verbose=True,
                                  simulate_drag=sim))
            return 0
        if cmd == "canattack":                   # 游戏自己的 CanAttack（self=CDO）
            print(inj.game_can_attack(int(argv[1]), int(argv[2]), verbose=True))
            return 0
        if cmd == "picklist":                    # 当前二选一/三选一候选（两类一起）
            for c in inj.pick_candidates():
                print("  [%s] index=%s %-22s label=%-14s actor=0x%X trig=0x%X(kid=%s)"
                      " needs_target=%s clickable=%s is_effect=%s card_id=%s"
                      % (c["kind"], c["index"], c["name"] or "-", c["label"] or "-",
                         c["actor"], c["trigger_actor"], c["trigger_id"],
                         c["needs_target"], c["clickable"], c.get("is_effect"),
                         c.get("card_id")))
            return 0
        if cmd == "pick":                        # pick <index> [trigger] [kind]
            kw = {}
            if len(argv) > 2:
                t = argv[2]
                kw["trigger"] = int(t, 0)
            if len(argv) > 3:
                kw["kind"] = argv[3]
            r = inj.pick_choice(int(argv[1]), verbose=True, **kw)
            print(r if not r.get("ok") else {k: v for k, v in r.items() if k != "candidates_after"})
            return 0 if r.get("ok") else 1
        if cmd == "mulligan":
            # 用法: mulligan [--discard 26,31] [--wait 300]
            discard = []
            wait = 0.0
            rest = argv[1:]
            for i, a in enumerate(rest):
                if a == "--discard" and i + 1 < len(rest):
                    discard = [int(x) for x in rest[i + 1].split(",") if x.strip()]
                if a == "--wait" and i + 1 < len(rest):
                    wait = float(rest[i + 1])
            r = inj.mulligan(discard=discard, wait=wait)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "pregame":
            print("PreGameState =", inj.pregame_state(),
                  " myMulliganDone =", inj.mulligan_done())
            print("marked:", [(r["card_id"], r["name"], r["marked"]) for r in inj.mulligan_marks()])
            return 0
        if cmd == "surrender":
            r = inj.surrender()
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "attack":
            print(inj.attack_card(int(argv[1]), int(argv[2])))
            return 0
        # ---- 指向语义分层的三个动词（2026-09-27；旧名见各自 docstring 的兼容别名） ----
        if cmd == "target":                      # target <spec>：目标规格 → card_id（含按卡名）
            print(inj.resolve_target(argv[1]))
            return 0
        if cmd == "event":                   # event <card_id> [--require-inactive]：指令/反制
            r = inj.play_card_event(int(argv[1]), verbose=True,
                                    require_inactive="--require-inactive" in argv[2:])
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "eventt":                  # eventt <card_id> <target>：指向性指令（一次成交）
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.play_card_event_with_target(int(argv[1]), rt["card_id"], verbose=True)
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "unit":                    # unit <card_id> [slot] [t=<target>]：单位（可带站位/目标）
            slot, tgt = None, None
            for a in argv[2:]:
                if a.startswith("t="):
                    rt = inj.resolve_target(a[2:])
                    if not rt.get("ok"):
                        print("目标解析失败:", rt)
                        return 1
                    tgt = rt["card_id"]
                else:
                    slot = int(a)
            if tgt is None:
                r = inj.play_card_unit(int(argv[1]), slot=slot, verbose=True)
            else:
                r = inj.play_card_unit_with_target(int(argv[1]), tgt, slot=slot, verbose=True)
            print({k: v for k, v in r.items() if k not in ("candidates", "select_as_target")})
            return 0 if r.get("ok") else 1
        if cmd == "unitt":                   # unitt <unit_card_id> <target> [slot]：单位两阶段指向
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.play_card_unit_with_target(int(argv[1]), rt["card_id"],
                                               slot=(int(argv[3]) if len(argv) > 3 else None),
                                               verbose=True)
            print({k: v for k, v in r.items() if k != "candidates"})
            return 0 if r.get("ok") else 1
        if cmd == "aimunit":                     # aimunit <playing_card_id> <target>：点选目标单位
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.select_unit_target(int(argv[1]), rt["card_id"], verbose=True)
            print({k: v for k, v in r.items() if k != "candidates"})
            return 0 if r.get("ok") else 1
        if cmd == "canact":                      # canact <card_id>：本回合能不能动（部署病）
            r = inj.can_act_now(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("can_act") else 1
        if cmd == "forecast":                    # forecast [index] [kind]：一口气走完多层抉择链
            r = inj.pick_layers(int(argv[1]) if len(argv) > 1 else 0,
                                kind=(argv[2] if len(argv) > 2 else None), verbose=True)
            print({k: v for k, v in r.items() if k != "pick_state"})
            return 0 if r.get("ok") else 1
        if cmd == "choose":                      # choose <i> [kind]：选一张（一个动词）
            kw = {}
            if len(argv) > 2:
                kw["kind"] = argv[2]
            r = inj.choose_card(int(argv[1]) if len(argv) > 1 else 0, verbose=True, **kw)
            print({k: v for k, v in r.items() if k != "candidates_after"})
            return 0 if r.get("ok") else 1
        if cmd == "chooseone":                   # chooseone <i> <target>：抉择+指向（两步合一）
            rt = inj.resolve_target(argv[2])
            if not rt.get("ok"):
                print("目标解析失败:", rt)
                return 1
            r = inj.choose_one_with_target(int(argv[1]), rt["card_id"], verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "totals":                      # totals <card_id>：游戏本体的显示值（UI 同源）
            r = inj.card_totals(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "gaps":                        # gaps [back|front]：空隙序号 → 列号请求（只读对账）
            r = inj.probe_gap_numbers((argv[1] if len(argv) > 1 else "back"), verbose=True)
            print({k: v for k, v in r.items() if k != "gaps"})
            return 0
        if cmd == "gaptop":                      # gaptop <空隙序号> [back|front]：只算不写
            r = inj.gap_number(int(argv[1]), row=(argv[2] if len(argv) > 2 else "back"),
                               verbose=True)
            print(r)
            return 0
        if cmd == "front":                       # front <card_id> [slot]：上线/移动
            r = inj.move_to_front(int(argv[1]),
                                  slot=(int(argv[2]) if len(argv) > 2 else None),
                                  verbose=True)
            print({k: v for k, v in r.items()})
            return 0 if r.get("ok") else 1
        if cmd == "pickhand":                    # pickhand <hand_card_id>：选手牌当目标并确认
            r = inj.select_hand_target(int(argv[1]), verbose=True)
            print({k: v for k, v in r.items() if k != "legal"})
            return 0 if r.get("ok") else 1
        if cmd == "pending":                     # pending：现在在等什么（只读汇总）
            print(inj.pending_summary())
            return 0
        if cmd == "wait":                        # wait [seconds]：等我方回合
            ok = inj.wait_our_turn(limit=float(argv[1]) if len(argv) > 1 else 300.0)
            print("我方回合:", ok)
            return 0 if ok else 1
        if cmd == "mullmarks":                   # mullmarks：换牌界面每张牌的 shouldDiscard
            for r in inj.mulligan_marks():
                print("  ", r)
            return 0
        if cmd == "mullmark":                    # mullmark <card_id>：切换待替换标记
            r = inj.mulligan_mark(int(argv[1]), verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "mullgo":                      # mullgo：确认换牌（真按钮四件套）
            r = inj.mulligan_confirm(verbose=True)
            print(r)
            return 0 if r.get("ok") else 1
        if cmd == "arrows":
            print([hex(a) for a in inj.arrow_actors()])
            return 0
        if cmd == "board":
            from kardsmem import board as BA
            st = BA.open_source("mem").snapshot()
            for c in st.cards:
                if c.obj.IsLocatedOnBoard():
                    a = inj.board_actor_of(c.obj.CardID)
                    print("  %-26s id=%-6s %-10s actor=%s" % (c.name, c.obj.CardID, c.obj.Location.name,
                                                              hex(a) if a else "?"))
            return 0
        print("用法: selftest | end | play <card_id> | attack <attacker_id> <target_id> | arrows | board")
        print("      canplay <card_id>   游戏总闸 BP_Logic::CanPlayCardFromHand（bool，含指挥点/支援线/限制/指向）")
        print("      cando <card_id>     BP_Logic::CanCardDoAnything（这张牌还能不能做事）")
        print("      cani                BP_Logic::CanIDoAnything（我方还有没有事可做）")
        print("      precheck <card_id>  总闸 + 下钻取理由（NN/前端预检入口）")
        print("      canfront <card_id>  游戏自己的 CanMoveCardToLocation（能不能上线）")
        print("      canattack <a> <t>   游戏自己的 CanAttack（带 failReason）")
        print("      picklist            当前二选一/三选一候选（两类一起，带屏幕次序与选项文字）")
        print("      pick <i> [trigger] [kind]  点同一触发者分组内的第 i 个候选")
        print("      target <spec>       目标规格 → card_id（card_id / hq / front0 / back1 / guard0；不收卡名）")
        print("      event <card> [--require-inactive]   打出指令/反制（反制=切换，默认允许重复）")
        print("      eventt <card> <tgt> 指向性指令（松手即成交）")
        print("      unit <card> [slot] [t=<tgt>]        打出单位（slot = 支承线内**站位**）")
        print("      unitt <card> <tgt> [slot]           单位+指向（无合法目标 ⇒ 自动退化成 unit）")
        print("      canfront <card> [--simulate-drag]  上线判据；默认不问（问它要伪造拖拽态）")
        print("      canact <card>       本回合能不能动（部署病：enterPlayOnTurn vs turn，blitz 例外）")
        print("      front <card> [slot] 上线/移动（XActionMoveCardToLine）")
        print("      forecast [i] [kind] 一口气走完多层抉择链（预报/天气→2K/4K/6K）")
        print("      choose <i> [kind]   选一张（choose_one / choose_spawn / choose_draw；单击提交）")
        print("      totals <card_id>    游戏本体的显示值 getTotal*（与卡面 UI 同源，只读）")
        print("      pickhand <card>     选手牌当目标 + 点确认按钮")
        print("      pending             现在在等什么（抉择/手牌目标/板卡待点目标/箭头/队列）")
        print("      wait [sec]          等我方回合")
        print("      mullmarks / mullmark <card> / mullgo   换牌：读取标记 / 切换标记 / 确认")
        return 2
    finally:
        inj.close()
