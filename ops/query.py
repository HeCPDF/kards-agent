# -*- coding: utf-8 -*-
"""ops/query.py —— 合法性与**只读**查询：向游戏本体问 Can*、落点/空隙换算、关键词与显示值 getter、目标规格解析。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。

★★ 步骤 4（`OPS-ARCHITECTURE.md` §2 旁路 / §7）——**本模块没有"写字段"的能力**：
  * 它从 `ops.primitives` 只 import **只读原语**（名字逐个列在下面，全是 `READ_PRIMITIVES` 里的）；
    写原语（`poke` / `write_then_call` / `write_then_call_keep` / `write_then_calls` /
    `write_and_call2` / `write_fields_then_call` / `poke_and_call0`）**根本不在这个命名空间里**
    ⇒ "顺手临时写个字段再问"在这里**写不出来**（不是靠自觉，是 import 期就没有）；
  * 唯一一个"问它要临时写拖拽态字段"的查询（`can_move_to(simulate_drag=True)`）已经被搬到
    `ops/diag.py`：本模块只负责**分发**（鸭子类型找 `can_move_to_simulated`），自己不写。
  * 判据由 `tests/test_ops_rules.py` 钉住（"查询模块无写能力"那两条 lint）。
"""
from __future__ import annotations

import struct
from typing import Optional

from ops.consts import (
    LOC_BOARD_FRONTLINE, NOTE_OFF_BOARD_SELECTING_HAND_TARGET,
    NOTE_OFF_HTGT_WIDGET_CARD_ID, NOTE_OFF_HTGT_WIDGET_CARD_OBJ, NOTE_OFF_LOGIC_PLAYING_FROM_HAND,
    NOTE_OFF_LOGIC_PLAYING_LOC,
)
# ★ 只读原语（**只 import 这两个**；见文件头的步骤 4 纪律）
from ops.primitives import call_out_u8_batch, call_ufunction


class QueryMixin:
    """合法性与只读查询：向游戏本体问 Can*、落点/空隙换算、关键词与显示值 getter、目标规格解析。"""

    def preflight(self, card_id: Optional[int] = None, target=None,
                  action: Optional[str] = None, verbose: bool = True) -> dict:
        """动手前自检汇总（`ops.py::preflight` 同源，**不动鼠标/不写内存**）。

        只做两件有增量价值的事：
          ① 报"选择界面有没有开着"（开着时任何出牌/移动/攻击都被吃掉，这是机械事实，
             各动作函数自己也会拦，这里给一个提前看一眼的入口）；
          ② 给了 `card_id`（+可选 `target`/`action`）就顺手跑一次对应判据。
        ★ 这里的一切**都只是提前看**，不是又一道判据 —— 真正的拦截在动作函数里，
          而且判据缺漏是必然的（"只挑不判"）。
        """
        pend = self.pick_pending()
        blocked = bool(pend.get("pending"))
        out = {"pick_pending": pend, "blocked_by_pick": blocked}
        if verbose:
            print("选择界面：%s" % ("开着 —— 出牌/移动/攻击都会被吃掉，先处理它"
                                  if blocked else "没开，可以动手"))
        if card_id is None:
            return out
        card = self.find_card(card_id)
        out["card_found"] = card is not None
        if card is None:
            if verbose:
                print("card %s 不在我方手牌/场上" % card_id)
            return out
        if verbose:
            print("card %s name=%r type=%s loc=%s atk=%s hp=%s"
                  % (card_id, card.name, card.card_type, card.obj.Location,
                     card.attack, card.defense))
        if action in (None, "move") and target is None:
            # ★ 2026-09-27：不再"伪造拖拽态"去问 `CanMoveCardToLocation`（它要写
            #   `PC->SelectedCard`；见那个函数的 docstring）⇒ 这里如实报"问不出来"，
            #   移动是否成立由游戏在提交时自己判（`move_to_front`）。
            gm = self.can_move_to(card_id, LOC_BOARD_FRONTLINE, verbose=False)
            out["can_move"] = gm
            out["pinned"] = self.is_pinned(card_id)
            if verbose:
                print("can_move_frontline(%s)：%s | pinned=%s"
                      % (card.name, gm.get("reason") or gm.get("can"), out["pinned"]))
        if target is not None:
            rt = self.resolve_target(target)
            out["target"] = rt
            if rt.get("ok"):
                ca = self.game_can_attack(card_id, rt["card_id"], verbose=False)
                out["can_attack"] = ca
                if verbose:
                    print("can_attack(%s -> %s) = %s（%s）"
                          % (card.name, rt["card_id"], ca.get("can"),
                             ca.get("reason_zh") or ca.get("stopped")))
            elif verbose:
                print("target %r 解析失败：%s" % (target, rt.get("error")))
        return out


    def game_can_attack(self, attacker_id: int, target_id: int,
                        verbose: bool = False,
                        world_context: Optional[int] = None,
                        kredits_override: Optional[int] = None,
                        snapshot=None) -> dict:
        """跑**游戏自己的** `cardsCheckFunctions_C::CanAttack`（纯查询，不改数据）。

        签名（SDK 1.60）：`CanAttack(UBaseCardObject* attackerCard, UBaseCardObject* defenderCard,
        int32 attackerKredits, int32 currentTurn, TArray<UBaseCardObject*>& cardsInAttackedLocation,
        UObject* __WorldContext, bool* CanAttack_0, FString* failReason, FString* Reason_Param_1)`
        ⇒ ParmsSize `0x58`：
          0x00 attacker / 0x08 defender / 0x10 kredits / 0x14 turn /
          0x18 TArray(ptr,num,max) / 0x28 WorldContext / 0x30 出参 can /
          0x38 出参 failReason(FString) / 0x48 出参 Reason_Param_1(FString)
        """
        import time as _t
        from kardsmem import board as BA
        import struct as _s
        timing = {}
        _t0 = _t.time()

        def _lap(key):
            nonlocal _t0
            now = _t.time()
            timing[key] = round(now - _t0, 4)
            _t0 = now

        cls, cdo, f = self._bp_lib_fn("cardsCheckFunctions_C", "CanAttack")
        _lap("lib_fn_s")
        if not f:
            return {"ok": False, "stopped": "找不到 cardsCheckFunctions_C::CanAttack", "timing": timing}
        # `snapshot`：调用方（本次决策）已经读过的盘面。原先每问一次都重新全量快照一遍（实机 4–6 s/次，1K 时被拒的攻击候选
        # 一个接一个地问 ⇒ 一次决策卡 14–18 s，2026-10-06 日志 `probe.asks`）；决策内盘面不变，复用即可。
        st = snapshot if snapshot is not None else BA.open_source("mem").snapshot()
        _lap("snapshot_s")
        # ★ 2026-10-07 静态分析：复用快照后仍 ~4 s/次的真因 = `_card_object(id)`。它先 `hand_actor()`（扫手牌控件；
        #   攻击者/目标都是**场上卡**，必然 miss），miss 后又 `hand_card_actors_v2()` 查账本 ⇒ `deck_actors()` ⇒
        #   `oa.find_by_class_name("BP_Deck_C")` = **Python 逐对象全量扫 GObjects（12 万+，秒级）**，随后才走
        #   `board_actor_of`（再扫一遍 level actors）。攻击者+目标各一次 ⇒ 2×。
        #   快照里每张卡的 `raw["ptr"]` 就是 `UBaseCardObject*`（`row_ptrs` 早就这么用），直接取，不再走 `_card_object`。
        by_id = {}
        for c in st.cards:
            p = (c.raw or {}).get("ptr")
            if p:
                by_id.setdefault(c.obj.CardID, p)
        a = by_id.get(attacker_id)
        d = by_id.get(target_id)
        src = {"attacker": "snapshot" if a else None, "defender": "snapshot" if d else None}
        if not a:
            a = self._card_object(attacker_id)          # 快照里没有 ⇒ 老路（慢，但保持原行为）
            src["attacker"] = "scan"
        if not d:
            d = self._card_object(target_id)
            src["defender"] = "scan"
        _lap("card_objects_s")
        if not a or not d:
            return {"ok": False, "stopped": "拿不到 attacker/defender 的 UBaseCardObject", "timing": timing}
        kred = int(kredits_override if kredits_override is not None
                   else ((st.kredits or {}).get(st.my_side) or 0))
        # ★ `cardsInAttackedLocation` = **防守方那一行、那一侧**的全部卡对象指针
        #   （`agent/legality.location_cards` 的同一套语义；战斗机拦截靠它）。
        #   传空数组时"攻击者在前线"的组合会让游戏崩/卡（实测 27→54）。
        defender = next((c for c in st.cards if c.obj.CardID == target_id), None)
        row_ptrs = []
        if defender is not None:
            for c in st.cards:
                if c.side == defender.side and c.obj.Location == defender.obj.Location:
                    p = (c.raw or {}).get("ptr") or self._card_object(c.obj.CardID)
                    if p:
                        row_ptrs.append(p)
        parms = bytearray(0x58)
        _s.pack_into("<Q", parms, 0x00, a)
        _s.pack_into("<Q", parms, 0x08, d)
        _s.pack_into("<i", parms, 0x10, int(kred))
        _s.pack_into("<i", parms, 0x14, int(st.turn or 0))
        _s.pack_into("<Qii", parms, 0x18, 0, 0, 0)      # 数组位先占着，JS 侧填
        # ★★ 2026-09-26 **更正**（用户当场指出："指向税大部分单位没有。场上均无。
        #   你 legal 判断有问题。"）：`__WorldContext` 传 **0 是错的**，跟
        #   `CanSelectAsTarget` 是**同一个坑**（那里早已注明"传 0 ⇒ 任何目标都回
        #   `..._not_enough_kredits_to_target`"），我当时顺手写了句"CanAttack 那边
        #   传 0 是 OK 的"——**没有验证**。
        #   反证：`kardsmem cards --raw` 显示**全盘 `kredits_tax_as_enemy_target` 都是 0**、
        #   GUARD 的 `operation_cost` 就是 3，而"7 点打它"却回
        #   `not_enough_kredits_to_target`（连打 1/3 的 HUMBER 也一样）。
        #   ⇒ 传活的 `BP_Logic_C`（跟 CanSelectAsTarget 同一套默认）。
        _lap("row_ptrs_s")
        wc = int(world_context if world_context is not None else (self.logic_actor() or 0))
        _s.pack_into("<Q", parms, 0x28, wc)             # __WorldContext（必须非 0）
        _lap("logic_actor_s")
        # ⚠ 实测：某些组合下 ProcessEvent 会抛 native "system error"（进程不死，但 RPC 报错）。
        #   不能让它把调用方带崩 —— 包起来，如实报"这次没问出来"。
        try:
            out = self.call_raw_arr(cdo, f, bytes(parms), 0x18, row_ptrs)   # ★ self = CDO
        except Exception as e:                                  # noqa: BLE001
            _lap("call_s")
            return {"ok": False, "stopped": "CanAttack 调用抛异常：%s" % e,
                    "attacker": attacker_id, "target": target_id,
                    "row_ptrs": len(row_ptrs),
                    "source": "game:cardsCheckFunctions::CanAttack", "timing": timing}
        _lap("call_s")
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回", "timing": timing}
        can = bool(buf[0x30])
        res = {"ok": True, "can": can, "kredits": kred, "turn": st.turn,
               "world_context": hex(wc) if wc else None,
               "source": "game:cardsCheckFunctions::CanAttack", "func": hex(f),
               "timing": timing, "card_obj_src": src}
        for key, off in (("fail_reason", 0x38), ("reason_param_1", 0x48)):
            ptr, num = _s.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00") if raw else None
        if verbose:
            print("    游戏自己的 CanAttack(%s -> %s) -> can=%s failReason=%r"
                  % (attacker_id, target_id, can, res.get("fail_reason")))
        return res

    # ------------------------------------------------------------ 游戏自己的"总闸"预检
    # ★ 2026-09-25 用户定调：**预检用游戏的总入口**（NN/前端都要用）。
    #   `BP_Logic_C` 上三个总闸（都是实例函数，self = 活着的 BP_Logic_C）：
    #     CanPlayCardFromHand(int32 CardID, bool* Yes)   全程打牌判据（bool-only，没有 reason）
    #     CanCardDoAnything(int32 CardID, bool* canIt)   这张牌这回合还能不能做事
    #     CanIDoAnything(bool* canI)                     我方还有没有事可做
    #   `CanPlayCardFromHand` 内部依次做：指挥点(getTotalKreditCost≥getKreditBySide)、
    #   支援线满(IsLocationFull)、全局限制(IsThereGameplayRestriction/BlockCardFromBeingPlayedFromHand)、
    #   天气/自定义能力特例、卡自身 CanPlayFromHand、以及 CanSelectAsTarget(指向)。
    #   ★ 游戏自己的**提交路径**（`BP_Logic::GlobalMouseUpBattle`）调的则是两个带 reason 的
    #     叶子检查：`CanPlayFromHand` + `CanSelectAsTarget`（理由就是屏幕提示那份）。
    #   ⇒ 预检：先问总闸；它说不行再下钻取理由。
    # ------------------------------------------------------------ 游戏自己的"指向"判据
    def game_can_select_as_target(self, targeted_id: int, targeting_id: int,
                                  by_play_from_hand: bool = True,
                                  world_context: Optional[int] = None,
                                  remaining_kredits: Optional[int] = None,
                                  verbose: bool = False) -> dict:
        """跑**游戏自己的** `cardsCheckFunctions_C::CanSelectAsTarget`（纯查询，不改数据）。

        签名（SDK 1.60，`cardsCheckFunctions_classes.hpp:25`）：
            `CanSelectAsTarget(UBaseCardObject* Targeted, UBaseCardObject* Targeting,
                               bool byPlayFromHand, UObject* __WorldContext,
                               bool* can, FString* Reason, FString* Reason_Param_1,
                               FString* Reason_Param_2)`
        ParmsSize `0x128`（`cardsCheckFunctions_parameters.hpp:98`）：
            0x00 Targeted（**被指向**的那张） / 0x08 Targeting（**做出指向**的那张，即正在打的牌）
            0x10 byPlayFromHand（是不是"从手牌打出"引起的指向）
            0x18 __WorldContext（跟 CanAttack 一样传 0）
            0x20 出参 can / 0x28 Reason / 0x38 Reason_Param_1 / 0x48 Reason_Param_2
        它是游戏**判定"这张牌能不能指向那张卡"的权威**（箭头 ubergraph、
        `BP_HandCard::DoesThisCardHasAnyTarget`、`BP_Logic::GlobalMouseUpBattle`、
        `BP_Logic::CanPlayCardFromHand` 都调它，共 6 处调用点）。
        ⇒ "带目标出牌"的预检就用它，别自己算。
        """
        cls, cdo, f = self._bp_lib_fn("cardsCheckFunctions_C", "CanSelectAsTarget")
        if not f:
            return {"ok": False, "stopped": "找不到 cardsCheckFunctions_C::CanSelectAsTarget"}
        tgt = self._card_object(targeted_id)
        src = self._card_object(targeting_id)
        if not tgt or not src:
            return {"ok": False,
                    "stopped": "拿不到 UBaseCardObject（targeted=%s targeting=%s）"
                               % (bool(tgt), bool(src))}
        parms = bytearray(0x128)
        struct.pack_into("<Q", parms, 0x00, tgt)
        struct.pack_into("<Q", parms, 0x08, src)
        parms[0x10] = 1 if by_play_from_hand else 0
        # ★★ 2026-09-25 实机定位：`__WorldContext` **必须非 0**。
        #   传 0 时函数内部取不到 `BP_Logic_C`，于是指挥点按 0 读 ⇒
        #   **任何目标**都回 `can=False reason='play_from_hand_not_enough_kredits_to_target'`
        #   （看起来像"所有目标都不合法"，其实是上下文没给）。
        #   传 logic / playerController / 手牌 actor 都可以（实测三者一致：
        #   打 GUNSHIP MISSION 指向敌方支援线的 I-16 ISHAK → can=True reason=''）。
        #   默认用活着的 `BP_Logic_C`。
        #   （★ 2026-09-26 **更正**：当时这里写着"`CanAttack` 那边传 0 是 OK 的"——
        #     **是错的**。同一个坑在 `CanAttack` 上也成立：传 0 会让它回
        #     `not_enough_kredits_to_target`；已改成传活 `BP_Logic_C`。）
        struct.pack_into("<Q", parms, 0x18, int(world_context if world_context is not None
                                              else (self.logic_actor() or 0)))
        # `tmpRemainingKredits@0x58`：**不是** Parm 标记，但拒绝理由永远落在
        # `play_from_hand_not_enough_kredits_to_target` ⇒ 怀疑它要么是调用方该填的
        # "剩余指挥点"、要么函数内部靠 `__WorldContext` 取 Logic 才拿得到。两个都试。
        if remaining_kredits is not None:
            struct.pack_into("<i", parms, 0x58, int(remaining_kredits))
        try:
            out = call_ufunction(self.api(), cdo, f, parms)             # ★ self = CDO
        except Exception as e:                                         # noqa: BLE001
            return {"ok": False, "stopped": "CanSelectAsTarget 调用抛异常：%s" % e,
                    "source": "game:cardsCheckFunctions::CanSelectAsTarget"}
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        res = {"ok": True, "can": bool(buf[0x20]), "targeted": targeted_id,
               "targeting": targeting_id, "source": "game:cardsCheckFunctions::CanSelectAsTarget",
               "func": hex(f)}
        for key, off in (("reason", 0x28), ("reason_param_1", 0x38), ("reason_param_2", 0x48)):
            ptr, num = struct.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                if raw:
                    res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if verbose:
            print("    游戏自己的 CanSelectAsTarget(targeted=%s, targeting=%s) -> can=%s reason=%r"
                  % (targeted_id, targeting_id, res["can"], res.get("reason")))
        return res

    def _game_logic_bool(self, fn_name: str, card_id: Optional[int], verbose: bool = False) -> dict:
        logic = self.logic_actor()
        if not logic:
            return {"ok": False, "stopped": "找不到 BP_Logic_C 实例"}
        cls = self.uclass_of_instance(logic)
        f = self.find_fn(cls, fn_name, inherited=True)
        if not f:
            return {"ok": False, "stopped": "BP_Logic_C 上找不到 %s" % fn_name}
        from kardsmem import board as BA                                       # noqa: F401
        import struct as _s
        parms = bytearray(0x08)
        if card_id is not None:
            _s.pack_into("<i", parms, 0x00, int(card_id))
        try:
            out = call_ufunction(self.api(), logic, f, parms)
        except Exception as e:                                       # noqa: BLE001
            return {"ok": False, "stopped": "%s 调用抛异常：%s" % (fn_name, e)}
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "%s 没返回" % fn_name}
        can = bool(buf[0x04] if card_id is not None else buf[0x00])
        res = {"ok": True, "can": can, "source": "game:BP_Logic::%s" % fn_name, "func": hex(f)}
        if verbose:
            print("    游戏自己的 %s -> %s" % (fn_name, can))
        return res

    def game_can_play_card_from_hand(self, card_id: int, verbose: bool = False) -> dict:
        """打牌**总闸**：`BP_Logic_C::CanPlayCardFromHand(CardID, bool* Yes)`（bool-only）。"""
        return self._game_logic_bool("CanPlayCardFromHand", card_id, verbose)

    def game_can_card_do_anything(self, card_id: int, verbose: bool = False) -> dict:
        """`BP_Logic_C::CanCardDoAnything(CardID, bool* canIt)`。"""
        return self._game_logic_bool("CanCardDoAnything", card_id, verbose)

    def game_can_i_do_anything(self, verbose: bool = False) -> dict:
        """`BP_Logic_C::CanIDoAnything(bool* canI)`。"""
        return self._game_logic_bool("CanIDoAnything", None, verbose)

    def precheck_play(self, card_id: int, verbose: bool = False) -> dict:
        """**打牌预检（NN/前端统一入口）**：先问游戏总闸，false 时下钻取理由。

        返回 `{"ok", "can", "source", "reason", ...}`；`reason` 是 `not_enough_kredits` /
        `supply_line_full` / 或游戏自己给的理由码（如 `enemy_unit`）。
        注意：**它只挑不判**——真打出去时照样走完整拖拽，让游戏自己裁决。
        """
        gate = self.game_can_play_card_from_hand(card_id, verbose=verbose)
        if gate.get("ok") and gate.get("can"):
            return {"ok": True, "can": True, "source": gate.get("source")}
        out = {"ok": True, "can": False, "source": gate.get("source"),
               "gate": gate}
        if not gate.get("ok"):
            out["stopped"] = gate.get("stopped")
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        card = next((c for c in st.cards if c.obj.CardID == card_id and c.side == st.my_side), None)
        kred = (st.kredits or {}).get(st.my_side)
        # ⓿ **有别的选择界面开着** ⇒ 出牌/移动/攻击全部无效（游戏机制事实，项目规则）。
        #    实机踩过：`isSelectingHandTarget=1` 卡住（我们的 175th 部署效果"选一张手牌
        #    放回牌组顶"从没完成），接下来每一张牌的总闸都回 False、而三种下钻
        #    （指挥点/支援线/卡自己的覆写）一个都不匹配 ⇒ `reason=None`，
        #    调用方完全看不出原因、只能瞎试。这条把它点出来。
        try:
            from kardsmem.pick import pick_state as _ps
            pst = _ps(self.ks)
            if pst.get("pending"):
                which = ("选一张手牌" if pst.get("is_selecting_hand_target") == 1
                         else "卡牌抉择界面" if pst.get("choose_one_active") == 1 else "选择界面")
                out.update(reason="pending_selection", pending="hand_target"
                           if pst.get("is_selecting_hand_target") == 1 else "choose_one",
                           reason_zh="现在还开着%s，出牌/移动/攻击都被游戏挡着" % which,
                           pending_evidence=pst.get("pending_evidence"))
                return out
        except Exception:                                          # noqa: BLE001
            pass
        # ① 指挥点
        if card is not None and kred is not None and card.obj.getTotalKredits() is not None \
                and kred < card.obj.getTotalKredits():
            out.update(reason="not_enough_kredits", reason_zh="指挥点不够",
                       kredits=kred, cost=card.obj.getTotalKredits())
            return out
        # ② 支援线满（单位打不出去）
        if card is not None and card.card_type in self.UNIT_TYPES and self._free_slot() is None:
            out.update(reason="supply_line_full", reason_zh="支援线满了")
            return out
        # ③ 下钻：卡自己的规则（带 reason）
        detail = self.game_can_play_from_hand(card_id, verbose=verbose)
        out["detail"] = detail
        if detail.get("reason"):
            out.update(reason=detail.get("reason"), reason_zh="游戏自己给的理由码")
        return out

    def game_can_play_from_hand(self, card_id: int, verbose: bool = False) -> dict:
        """跑**游戏自己的** `UBaseCardObject::CanPlayFromHand`（纯查询，不改数据）。

        签名（SDK 1.60）：`CanPlayFromHand(bool* canIt, FString* Reason,
        FString* reasonParam1, FString* reasonParam2, UBaseCardObject** targetedCard)`
        ⇒ ParmsSize `0x40`：canIt@0x00 / Reason@0x08 / p1@0x18 / p2@0x28 / targetedCard@0x38。

        ★ 2026-09-25：既然它**不改数据**，就没必要再跑我们自己那套"进程外 VM 重演"来猜
          ——直接问游戏。（`agent/legality` 的 VM 路径留给"不能注入"的场合。）
        ⚠ 实测它**不查指挥点**：0 指挥点时 cost=2/3 的牌照样返回 canIt=1。
          指挥点是另一道闸门（`PayMovementCost` 那类），别拿这个函数的返回值当"付得起"。
        """
        obj = self._card_object(card_id)
        if not obj:
            return {"ok": False, "stopped": "找不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        f = self.find_fn(cls, "CanPlayFromHand", inherited=True)
        if not f:
            return {"ok": False, "stopped": "card %s 上找不到 CanPlayFromHand" % card_id}
        out = call_ufunction(self.api(), obj, f, "00" * 0x40)
        buf = bytes.fromhex(out) if isinstance(out, str) else out
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        import struct as _s
        can = bool(buf[0x00])
        ptr, num = _s.unpack_from("<Qi", buf, 0x08)
        reason = None
        if ptr and num and 1 < num <= 4096:
            raw = self.m.read_exact(ptr, num * 2)
            if raw:
                reason = raw.decode("utf-16-le", "replace").rstrip("\x00")
        res = {"ok": True, "can": can, "reason": reason,
               "targeted_card": _s.unpack_from("<Q", buf, 0x38)[0] or None,
               "source": "game:BaseCardObject::CanPlayFromHand", "func": hex(f)}
        if verbose:
            print("    游戏自己的 CanPlayFromHand(%s) -> canIt=%s reason=%r"
                  % (card_id, can, reason))
        return res

    def _target_play_location(self, card, slot: Optional[int] = None) -> tuple:
        """带目标出牌时的**落点**（跟 `play_card` 同一套规则）→ `(loc_enum, 列号, place)`。

        单位卡要落到我方后排的某个**空隙**；非单位卡不看落点（`play_card` 里那条注释：
        非单位分支只要求 `RowUnderCursor` 为真）。

        ★ 2026-09-27 两修：
          ① **`slot` 以前没透传到这里** —— `play_card_unit_with_target` 收了 `slot` 却走进
             这条分支被 `_free_slot()` 覆盖，表现为"请求 slot=1 却落到 3（= 最右）"
             （108gen 那次；用户："你实际上把 108 部署在了最右侧"）。
          ② **`slot` 的语义改成"空隙序号"**（原来是裸列号）—— 见 `gap_number`/`gap_request_key`：
             真要写进 `LocationNumberUnderCursor` 的是「光标左边那些卡的最大列号 + 1」，
             有空洞时和 `i` 不是一回事。
        """
        loc_enum = self.our_back_enum()
        if card is not None and (card.card_type or "") in self.UNIT_TYPES:
            place = self.deploy_location_number(None if slot is None else int(slot), row="back")
            return loc_enum, (place.get("requested") or 0), place
        return loc_enum, 0, {"source": "non_unit", "gap": None, "requested": 0}

    def card_being_played_from_hand(self) -> int:
        """`BP_Logic_C::cardBeingPlayedFromHand` —— **正在打的**那张手牌的 CardID（0=没有）。

        两阶段"带目标出牌"的权威状态：阶段一（拖到我方支援线松手）写上它 + 落点，
        **阶段二点完目标才归 0**（1.58 导出 `BP_Logic.cpp:9156/9356`）。
        比"场上有没有箭头"可靠：箭头可能残留一支，这个字段不会。
        """
        logic = self.logic_actor()
        if not logic:
            return 0
        off = self.off(self.uclass_of_instance(logic), "cardBeingPlayedFromHand",
                       NOTE_OFF_LOGIC_PLAYING_FROM_HAND)
        raw = self.m.read_exact(logic + off, 4)
        return struct.unpack("<i", raw)[0] if raw else 0

    def card_being_played_loc(self) -> int:
        """`BP_Logic_C::cardBeingPlayedFromHandLocNum` —— 阶段一定下的**落点**编号。"""
        logic = self.logic_actor()
        if not logic:
            return -1
        off = self.off(self.uclass_of_instance(logic), "cardBeingPlayedFromHandLocNum",
                       NOTE_OFF_LOGIC_PLAYING_LOC)
        raw = self.m.read_exact(logic + off, 4)
        return struct.unpack("<i", raw)[0] if raw else -1

    def selecting_hand_target(self) -> Optional[bool]:
        """`BP_Board_C::isSelectingHandTarget // 0x0C21` —— 游戏自己的"手牌正在选目标"标志。

        同一个判据有原生 getter `BattleUtilityFunctions::HandCardIsSelectingTarget`，
        板卡按下流开头就用它早退；这里直接读字段，**只读、不调用**。
        """
        b = self.board_actor()
        if not b:
            return None
        off = self.off(self.uclass_of_instance(b), "isSelectingHandTarget",
                       NOTE_OFF_BOARD_SELECTING_HAND_TARGET)
        raw = self.m.read_exact(b + off, 1)
        return bool(raw[0]) if raw else None

    def hand_target_legal(self, hand_card_id: int, verbose: bool = False) -> dict:
        """**只读**：`源卡.IsValidHandTarget(这张手牌, isIt, Reason)` —— 游戏自己判"这张能不能选"。

        这是"确认"按钮灰不灰（`greyInvalidTargets()`）用的同一个函数；
        `Reason` 是 FString（出参，在 parms 里是 16 字节的 FString 结构）。
        """
        w = self.hand_target_widget()
        if not w:
            return {"ok": False, "stopped": "不在等选手牌状态（没有 selectHandTargetWidget）"}
        wcls = self.uclass_of_instance(w)
        src = self.m.ptr(w + self.off(wcls, "_cardBeingPlayedObject",
                                      NOTE_OFF_HTGT_WIDGET_CARD_OBJ)) or 0
        if not src:
            off_cbp = self.off(wcls, "cardBeingPlayed", NOTE_OFF_HTGT_WIDGET_CARD_ID)
            raw = self.m.read_exact(w + off_cbp, 4)
            cid = struct.unpack("<i", raw)[0] if raw else 0
            src = self._card_object(cid) or 0
        tgt = self._card_object(hand_card_id)
        if not src or not tgt:
            return {"ok": False, "stopped": "拿不到源卡/候选手牌的 UBaseCardObject"}
        scls = self.uclass_of_instance(src)
        f = self.find_fn(scls, "IsValidHandTarget")
        if not f:
            return {"ok": False, "stopped": "源卡类上没有 IsValidHandTarget"}
        parms = bytearray(0x20)
        struct.pack_into("<Q", parms, 0x00, tgt)
        out = call_ufunction(self.api(), src, f, parms)
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw 没返回"}
        res = {"ok": True, "hand_card": hand_card_id,
               "is_valid": bool(buf[0x08]), "source": "game:BaseCardObject::IsValidHandTarget"}
        ptr, num = struct.unpack_from("<Qi", buf, 0x10)
        if ptr and num and 1 < num <= 4096:
            raw = self.m.read_exact(ptr, num * 2)
            if raw:
                res["reason"] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if verbose:
            print("    IsValidHandTarget(手牌 %s) -> %s reason=%r"
                  % (hand_card_id, res["is_valid"], res.get("reason")))
        return res

    def _free_slot(self) -> Optional[int]:
        """我方后排的**空** locationNumber；**满了返回 None**（支援线 4 个单位 + 总部 = 0..4）。

        ⚠ 这里只在"没有真实鼠标位置"这一点上做替代：写进去的是真实合法的空位编号，
          游戏那边照样会自己校验（不合法就取消，不会发出去）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        used = {c.slot for c in st.cards
                if c.side == st.my_side and c.obj.InSupportLine() and c.slot is not None}
        for i in range(0, 5):                                # 后排只有 5 格（含总部）
            if i not in used:
                return i
        return None

    def _free_front_slot(self) -> Optional[int]:
        """我方**前线**的空 locationNumber。满了返回 None。

        ★ 2026-10-08 实机：原来写死"前线 4 格（0..3）"，而原版前线是**双方共用 5 位**（`FRONTLINE_CAP=5`，
        `IsFrontlineLimited` 时 2；`kardsmem/gamemodel.py`）⇒ 前线已有 4 张时（含对方的）本函数误报"满了"，
        一局里 `move_up` 被我们自己拒了 4 次。现在按真容量算：双方前线单位总数 ≥ 容量才算满，
        空列号在 `0..容量-1` 里找（列号是前线整排共用的 locationNumber）。"""
        from kardsmem import board as BA
        from kardsmem.gamemodel import FRONTLINE_CAP, FRONTLINE_CAP_LIMITED
        st = BA.open_source("mem").snapshot()
        front = [c for c in st.cards if c.obj.InFrontline() and c.slot is not None]
        limited = bool(getattr(st, "frontline_limiters", None))
        cap = FRONTLINE_CAP_LIMITED if limited else FRONTLINE_CAP
        if len(front) >= cap:
            return None
        used = {c.slot for c in front}
        for i in range(0, cap):
            if i not in used:
                return i
        return None

    @staticmethod
    def gap_request_key(numbers, gap: int) -> int:
        """「第 `gap` 个空隙」→ `LocationNumberUnderCursor` 该写的数（见上面那段推导）。

        `gap=0` ⇒ 最左（谁都不在光标左边 ⇒ 0）；`gap=n` ⇒ 最右（所有卡都在左边）。
        ★ 有空洞时 `numbers[i-1]+1 != i`，这正是要算而不是直接写 `i` 的原因。
        """
        nums = sorted(int(x) for x in numbers)
        if not nums or int(gap) <= 0:
            return 0
        return nums[min(int(gap), len(nums)) - 1] + 1

    def game_location_number_for_coords(self, coords, location_enum: int) -> Optional[dict]:
        """问**游戏自己**：这个世界坐标落在该排的第几号（**只读**，不改任何状态）。

        调用 `BP_Board_C::FindLocationNumberForCoordinates(coords, loc)`。
        参数布局（**当前构建**）：`.../1.60.27292.launcher/CppSDK/SDK/BP_Board_parameters.hpp:4000`
        —— `Coordinates@0x00`(FVector 0x18) / `Location@0x18`(u8) /
        `locationNumber@0x1C`(out int32) / `cardAtLocation@0x20`(out int32)，ParmsSize 0x28。

        ★ 先用哨兵 `-999` 预填出参：函数在"某张卡正在播放变身动画"时会**提前 return 而不写
          出参**（`doingConvertVisualAsNewCard` 那一支）⇒ 读到哨兵就知道"游戏这条链没给答案"，
          不会把缓冲区里的 0 当成"第 0 号"。
        """
        board = self.board_actor()
        if not board:
            return None
        f = self.find_fn(self.uclass_of_instance(board), "FindLocationNumberForCoordinates")
        if not f:
            return None
        parms = bytearray(0x28)
        struct.pack_into("<ddd", parms, 0x00, float(coords[0]), float(coords[1]), float(coords[2]))
        parms[0x18] = int(location_enum) & 0xFF
        struct.pack_into("<ii", parms, 0x1C, -999, -999)
        out = call_ufunction(self.api(), board, f, parms)
        buf = bytes(out) if out else b""
        if len(buf) < 0x24:
            return None
        ln, cat = struct.unpack_from("<ii", buf, 0x1C)
        return {"location_number": ln, "card_at_location": cat,
                "early_out": (ln == -999)}

    def gap_number(self, gap: int, row: str = "back", verbose: bool = False) -> dict:
        """空隙序号 → **游戏自己算出来的**列号请求值（失败则退回解析式，并如实标注来源）。

        空隙坐标的构造（与真人手势一一对应）：
          * `gap=0`   → 最左：第一张卡中心往左一个卡宽
          * `0<gap<n` → 两张卡**中点**（"拖到两个单位中间"）
          * `gap=n`   → 最右：最后一张卡中心往右一个卡宽
        这只是**取样点**：游戏那边真正用的是"哪些卡在该点左边"，而**顺序**不受渲染位移影响
        （所以拖动时"把卡挪开一些"的预览不会改变答案）。
        """
        loc_enum = self.our_back_enum() if row == "back" else self.our_front_enum()
        cards = self._row_cards(row=row)
        nums = [n for n, _ in cards]
        ana = self.gap_request_key(nums, gap)
        res = {"gap": int(gap), "row": row, "row_numbers": nums,
               "requested": ana, "analytic": ana, "source": "analytic",
               "row_cards": [cid for _, cid in cards]}
        n = len(cards)
        if n == 0:
            res["note"] = "该排没人 ⇒ 第 0 个空隙 = 落点 0"
            return res
        g = max(0, min(int(gap), n))
        if g == 0:
            anchor_id = cards[0][1]
            other_id = None
        elif g == n:
            anchor_id = cards[n - 1][1]
            other_id = None
        else:
            anchor_id, other_id = cards[g - 1][1], cards[g][1]
        a = self.board_actor_of(anchor_id)
        b = self.board_actor_of(other_id) if other_id else 0
        ab = self.actor_bounds(a)
        if not ab:
            res["note"] = "拿不到卡 %s 的板卡/包围盒 ⇒ 只有解析值" % anchor_id
            return res
        (ax, ay, az), (aex, _aey, _aez) = ab
        if b:
            bb = self.actor_bounds(b)
            if not bb:
                res["note"] = "拿不到卡 %s 的包围盒 ⇒ 只有解析值" % other_id
                return res
            bx = bb[0][0]
            x = (ax + bx) / 2.0
        elif g == 0:
            x = ax - aex - 1.0
        else:
            x = ax + aex + 1.0
        coords = (x, ay, az)
        res["coords"] = coords
        gm = self.game_location_number_for_coords(coords, loc_enum)
        if gm is None:
            res["note"] = "游戏那边问不出来（拿不到 BP_Board_C / 函数）⇒ 用解析值"
            return res
        res["game"] = gm
        if gm["early_out"]:
            res["note"] = ("游戏这条链**故意没给答案**（有卡在播变身动画）"
                           "⇒ 用解析值；此刻真拖也大概率不接")
            return res
        res["requested"] = int(gm["location_number"])
        res["source"] = "game:BP_Board_C::FindLocationNumberForCoordinates"
        res["card_at_location"] = int(gm["card_at_location"])
        if res["requested"] != ana:
            res["warn"] = ("游戏算出来的列号 %s 与解析式 %s **不一致**"
                           "——以游戏为准，但记下来（渲染位置/空洞可能有别的解释）"
                           % (res["requested"], ana))
        if verbose:
            print("    空隙 %s/%s：列号请求 %s（%s；解析式 %s；该点底下的卡=%s）"
                  % (gap, row, res["requested"], res["source"], ana,
                     res.get("card_at_location")))
        return res

    def probe_gap_numbers(self, row: str = "back", verbose: bool = True) -> dict:
        """**只读**：把该排每个空隙的列号请求值列出来（游戏答案 vs 解析式），并排对账。

        用途：改了站位代码/怀疑渲染换排时，一条命令就能看出"空隙 i ⇒ 游戏说第几号"。
        """
        cards = self._row_cards(row=row)
        out = {"row": row, "row_cards": [cid for _, cid in cards],
               "row_numbers": [n for n, _ in cards], "gaps": []}
        for g in range(0, len(cards) + 1):
            out["gaps"].append(self.gap_number(g, row=row, verbose=False))
        if verbose:
            print("-- 空隙 → 列号请求（排=%s，列号 %s）--" % (row, out["row_numbers"]))
            for g in out["gaps"]:
                print("  空隙 %-2s → 请求 %-3s [%s]  解析式 %-3s  %s"
                      % (g["gap"], g["requested"], g["source"], g["analytic"],
                         ("⚠ " + g["warn"]) if g.get("warn") else
                         ("" if "game" in g else ("(" + g.get("note", "") + ")"))))
        return out

    def deploy_location_number(self, slot: Optional[int], row: str = "back",
                               verbose: bool = False) -> dict:
        """`slot`（**空隙序号**）→ 真正要写的列号数。`slot=None` ⇒ 老行为（第一个空列号）。"""
        if slot is None:
            n = self._free_front_slot() if row == "front" else self._free_slot()
            return {"requested": n, "source": "first_free_slot" if n is not None else "row_full",
                    "gap": None, "row_numbers": [n0 for n0, _ in self._row_cards(row=row)]}
        r = self.gap_number(int(slot), row=row, verbose=verbose)
        return r

    def can_move_to(self, card_id: int, location_enum: int = LOC_BOARD_FRONTLINE,
                    verbose: bool = False, simulate_drag: bool = False) -> dict:
        """**跑游戏自己的** `BattleUtilityFunctions_C::CanMoveCardToLocation`。

        ★★ 2026-09-27 **默认不再伪造拖拽态**（用户待办："`can_move_to(simulate_drag=True)`
           仍在临时写手柄字段 `SelectedCard` ⇒ 应去掉或改问法"）。
           `simulate_drag=True` 要**临时写 `ABP_PlayerController_C::SelectedCard // 0x0950`**
           —— 那是**手柄/拖拽态字段**（同一个字段在 `IsGamepad()` 分支里被读），跟我们
           已经删掉的那批"伪造 PC 拖拽状态"的写法**是同一类**（`click-crash-report.md`：
           伪造 `leftButtonDown` 让游戏自己补一次 `EndDrag` ⇒ use-after-free）。
           ⇒ 现在：
             * `simulate_drag=False`（**默认**）：**什么都不写**，并且**不假装有答案**
               —— 返回 `{"ok": False, "reason": "cannot_ask_without_drag", "can": None}`。
               为什么不返回 `can=False`：没有拖拽态时它**必然**回 False（第一个
               `IsValid(SelectedCard)` 就不过），把它当"游戏说不能移动"就是**假判据**。
             * `simulate_drag=True`：**只作诊断**，结果里带
               `writes_gamepad_field` 标明写了哪个字段；**别把它接进判决路径**
               （`move_to_front` 已经不问了，直接让游戏在提交时判）。

        签名（SDK 1.60）：`(ECardLocationEnum Location, UObject* __WorldContext, bool* bResult)`
        —— ParmsSize 0x18：Location@0x00、WorldContext@0x08、出参 bResult@0x10。

        ★★ 2026-09-25 **重大更正**（之前这条写错了，撤回留档）：
          旧的注释说"实测这个函数在 0 指挥点时返回 False，和屏幕提示'指挥点数不足'
          完全一致" —— **那是把相关当成了因果**。全量 dump 它的字节码后看到的真相是：
            * **它里面没有任何指挥点检查**（一条都没有）；
            * 它读的是 `PlayerController->SelectedCard`
              （`BP_PlayerController_C::SelectedCard // 0x0950`，类型 `ABP_BaseCard_C*`）
              —— 也就是"**当前正在拖的那张卡**"，预检阶段那个字段是 `None`
              ⇒ 第一个 `IsValid(SelectedCard)` 就不过 ⇒ **永远**回 False。
          实测：不假装拖拽时 6 个 (卡,地点) 组合全回 0，而同一次读 `SelectedCard` 就是 None。
          ⇒ 之前"它看指挥点"的结论**作废**。

        它真正的判据（字节码逐条）：
            1. `PlayerController` 有效；
            2. `PlayerController->SelectedCard` 有效（IsValid）；
            3. `SelectedCard->CardLocation == Hand_Left(3)`（手牌）⇒ 只允许 `Location == 5`
               （Board_HQLeft）；
               否则（板卡）先过一个 `CardLocation` 查表，再过 `Location`：
               `Board_Frontline(7)` → 看 `DoesSideControlTheFrontline`；
               `Board_HQLeft(5)` → 直接 False（移动是单向的，板卡只能上前线）；
               其余 → False；
            4. `IsLocationFull(Location) && !IsSelectedCardOrder()` ⇒ False
               （那行满了、而且拖的不是"命令"类卡）。
          枚举原文：`kards_structs.hpp` `ECardLocationEnum`（7=Board_Frontline）。

        `simulate_drag=False`（默认）：**只读、不写**，而且**明确回"问不出来"**
        （`reason="cannot_ask_without_drag"`），不把"必然的 False"冒充成游戏判据。
        `simulate_drag=True`（**诊断专用**）：把 `SelectedCard` 临时指到这张卡的 actor 上、
        问完**立刻还原**（cursor 那几个字段也一起还原）—— 这正是真实鼠标**按下**
        时游戏自己会写的状态。**它会写手柄/拖拽态字段**，结果里会标出来。
        """
        cls, cdo, f = self._bp_lib_fn("BattleUtilityFunctions_C", "CanMoveCardToLocation")
        if not f:
            return {"ok": False, "stopped": "找不到 BattleUtilityFunctions_C::CanMoveCardToLocation"}
        atk = self.board_actor_of(card_id)
        if not atk:
            return {"ok": False, "stopped": "找不到 card %s 的板卡 actor" % card_id}
        if not simulate_drag:
            # ★ 什么都不写、也不给假答案（理由见 docstring）
            if verbose:
                print("    CanMoveCardToLocation：**不问**（问它要伪造 PC->SelectedCard，"
                      "没有拖拽态时它恒回 False ⇒ 那个 False 不是判据）")
            return {"ok": False, "reason": "cannot_ask_without_drag", "can": None,
                    "source": "game:CanMoveCardToLocation",
                    "location_enum": location_enum, "simulated_drag": False,
                    "note": "要问就必须临时写 ABP_PlayerController_C::SelectedCard // 0x0950"
                            "（手柄/拖拽态字段）⇒ 默认不写；上线让游戏在提交时自己判"
                            "（`move_to_front` 就是这么做的）"}
        # ★★ 步骤 4：`simulate_drag=True` 是**要写临时字段的诊断**，实现在 `ops/diag.py`。
        #   这里只**鸭子派发**（按属性名找）—— 本模块**不 import diag、也不 import 任何写原语**
        #   ⇒ "只读查询偷写字段"（弯路 #23）在结构上写不出来：`self.call_raw_hold` /
        #   `primitives.write_then_call` 在这个命名空间里根本不存在。
        #   组合成 `Injector` 时 `DiagMixin` 也在 ⇒ 老行为逐字不变。
        diag = getattr(self, "can_move_to_simulated", None)
        if diag is None:
            return {"ok": False, "reason": "diag_not_composed", "can": None,
                    "source": "game:CanMoveCardToLocation",
                    "location_enum": location_enum, "simulated_drag": True,
                    "note": "这个对象没有组合 ops/diag.py 的 DiagMixin ⇒ 诊断路径不可用"
                            "（只读对象不写字段，这是设计，不是缺功能）"}
        return diag(card_id, location_enum, atk, cls, cdo, f, verbose=verbose)

    def can_act_now(self, card_id: int, verbose: bool = False) -> dict:
        """**本回合这张单位能不能动**（"部署病"）—— 纯读，不写任何东西。

        对标 `ops.py::can_act_now`（旧鼠标实现里有、注入侧之前一直缺），判据同源：
          `enterPlayOnTurn == 当前回合` ⇒ **刚部署** ⇒ 本回合不能移动/攻击，
          **除非这张牌有 `blitz`**（闪击）。
        ★★ 为什么不能用 `attack_left` / `movement_left`：**对刚部署的单位它们仍然读到 1**
          （`kardsmem/board.py:429-431` 有同一条实测记录）⇒ 拿它们判会把"部署病"误判成能打。
        ★ 这是**进程外判据**（只挑不判，§7.6f）：最终由游戏裁决 —— 权威答案是
          `game_can_attack()` / `game_can_card_do_anything()`（直接问游戏本体）。
        """
        rec, st = self._card(card_id)
        if rec is None:
            return {"ok": False, "error": "读不到 card %s（不在我方手牌/场上？）" % card_id}
        kws = [str(k).lower() for k in (rec.keywords or [])]
        blitz = any("blitz" in k for k in kws)
        ept, turn = rec.obj.enterPlayOnTurn, getattr(st, "turn", None)
        out = {"ok": True, "card": card_id, "name": rec.name, "blitz": blitz,
               "enter_play_on_turn": ept, "turn": turn, "keywords": kws}
        if blitz:
            out.update(can_act=True, reason="blitz")
        elif ept is None or turn is None:
            out.update(can_act=True, reason="fields_unreadable",
                       note="读不到进场回合 ⇒ **不拦**（保持旧行为）")
        else:
            out.update(can_act=(int(ept) != int(turn)),
                       reason=("deployed_this_turn" if int(ept) == int(turn) else "ok"))
        if verbose:
            print("    can_act_now(%s %s) = %s（%s；enterPlayOnTurn=%s turn=%s blitz=%s）"
                  % (card_id, rec.name, out["can_act"], out["reason"], ept, turn, blitz))
        return out

    _GAME_KW_GETTERS = (("getHasAlpine", "alpine"), ("getHasAmbush", "ambush"), ("getHasBlitz", "blitz"),
                        ("getHasFury", "fury"), ("getHasGuard", "guard"), ("getHasImmune", "immune"),
                        ("getHasShock", "shock"), ("getHasSmokescreen", "smokescreen"))

    def card_keywords_many(self, ptrs) -> dict:
        """同 `card_keywords`，但**一次 RPC** 问完一批牌：{ptr: [关键词...]}。
        （用户 2026-10-01："又多若干个 rpc，小心点"——逐牌逐关键词各一次 RPC 太多。）"""
        items, index = [], []
        for ptr in ptrs:
            cls = self.uclass_of_instance(ptr)
            for fn_name, key in self._GAME_KW_GETTERS:
                f = self.find_fn(cls, fn_name)
                if f:
                    items.append([hex(ptr), hex(f)])
                    index.append((ptr, key))
        out = {int(p): [] for p in ptrs}
        if not items:
            return {"ok": False, "error": "没有可调的 getHas*", "keywords": {}}
        res = list(call_out_u8_batch(self.api(), items))
        bad = 0
        for (ptr, key), v in zip(index, res):
            if v == -1:
                bad += 1
            elif v:
                out[int(ptr)].append(key)
        return {"ok": bad == 0, "keywords": out, "errors": bad, "rpc": 1,
                "source": "game:UBaseCardObject::getHas*"}

    def card_keywords(self, card_id: Optional[int] = None, ptr: Optional[int] = None) -> dict:
        """**游戏本体的关键词真值**（`UBaseCardObject::getHas*`，各 `bool*` 出参）—— 纯只读调用。

        ★ 2026-10-01 实机（用户点破）：506th AIRBORNE 有**冲击**，我读到的关键词却是空的。
          `hasShock @0x1E9` 字节 = 0，而 `getHasShock` 返回 1 —— 冲击来自被授予的能力/静态定义
          （`receivedAbilitiesFromCards`、`GiveShock(…giverID)` 带来源），不在那一位上。
          关键词旗标位只是其中一路；游戏的 getter 才把所有来源合并。跟 `card_totals` 同一条纪律：
          屏幕上显示/规则上生效的量，问 getter，不自己拼分量。
        """
        obj = int(ptr) if ptr else self._card_object(int(card_id))
        if not obj:
            return {"ok": False, "error": "拿不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        kws, miss = [], []
        for fn_name, key in self._GAME_KW_GETTERS:
            f = self.find_fn(cls, fn_name)
            if not f:
                miss.append(fn_name)
                continue
            v = self.call_out_u8(obj, f)
            if v:
                kws.append(key)
        return {"ok": True, "card": card_id, "keywords": kws, "missing": miss,
                "source": "game:UBaseCardObject::getHas*"}

    def card_totals(self, card_id: int, verbose: bool = False) -> dict:
        """**游戏本体的显示值**（UI 用的那批 getter）—— 纯只读调用，不改任何状态。

        对标 `BP_Widget_HandCardTextV2.cpp`（卡面文本控件）里逐条读的：
          `getTotalAttack` / `getTotalDefense` / `getTotalKreditCost` /
          `getTotalOperationCost` / `getTotalHeavyArmor`（各 `int32*` 出参）
          + `getbuffsFromCardsAsJsonString(FString*)`（被贴效果一次给全）。
        （签名来自 `kards_classes.hpp:2639-2666`。）

        ★★ 为什么需要它（用户 2026-09-27 点破）：**进程外只读拿到的是"分量"**
          （`board_api` 读 `attack@0x6C` + `attackBuff@0x70`、`operationCost@0xB0` +
          `operationCostBuff@0xB4`，自己相加），而玩家屏幕上看到的是 getter 的结果 ——
          手牌会被贴膜、被减费，两者**不保证相等**。要和 UI 一字不差，只有调它。
        ⚠ 这条是**注入式只读**（红线允许：无副作用的纯查询），进程外/离线场景读不到时
          退回分量求和并在调用方标注来源（`board_api.Card.total_operation_cost`，P3 起是 property）。
        """
        obj = self._card_object(card_id)
        if not obj:
            return {"ok": False, "error": "拿不到 card %s 的 UBaseCardObject" % card_id}
        cls = self.uclass_of_instance(obj)
        out = {"ok": True, "card": card_id, "source": "game:UBaseCardObject::getTotal*"}
        miss = []
        for name, key in (("getTotalAttack", "attack"),
                          ("getTotalDefense", "defense"),
                          ("getTotalKreditCost", "kredit_cost"),
                          ("getTotalOperationCost", "operation_cost"),
                          ("getTotalHeavyArmor", "heavy_armor")):
            f = self.find_fn(cls, name)
            if not f:
                out[key] = None
                miss.append(name)
                continue
            buf = bytes(call_ufunction(self.api(), obj, f, "00" * 4) or b"")
            out[key] = struct.unpack_from("<i", buf, 0)[0] if len(buf) >= 4 else None
        f = self.find_fn(cls, "getbuffsFromCardsAsJsonString")
        if f:
            buf = bytes(call_ufunction(self.api(), obj, f, "00" * 16) or b"")
            if len(buf) >= 12:
                p, num = struct.unpack_from("<Qi", buf, 0)
                if p and 1 < num <= 65536:
                    raw = self.m.read_exact(p, num * 2)
                    if raw:
                        out["buffs_json"] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        if miss:
            out["missing_getters"] = miss
        if verbose:
            print("    card_totals(%s) = 攻%s/防%s 费%s 行动费%s 重甲%s"
                  % (card_id, out.get("attack"), out.get("defense"),
                     out.get("kredit_cost"), out.get("operation_cost"),
                     out.get("heavy_armor")))
        return out

    def is_pinned(self, card_id: int):
        """**压制（pin）**：被压制的单位**不能移动或攻击**（百科原文），与"抑制(suppress)"两回事。

        读侧跟 `ops.py::is_pinned` **同源**（`kardsmem.cards.read_live_effects`）：
        `receivedAbilitiesFromCards` 出现 `pinned`，或 `buffsFromCards` 出现 `combat_pinned`。
        → True / False / **None（读不出 ⇒ 调用方不要拦）**。
        """
        rec, _st = self._card(card_id)
        ptr = (rec.raw or {}).get("ptr") if rec is not None else None
        if not ptr:
            return None
        try:
            from kardsmem import cards as C
            eff = C.read_live_effects(self.ks, ptr)
        except Exception:                                     # noqa: BLE001
            return None
        for ab in eff.get("received_abilities") or []:
            if "pinned" in (ab.get("ability") or "").lower():
                return True
        for b in eff.get("buffs_from_cards") or []:
            for k in (b.get("buffs") or {}):
                if "pinned" in k.lower():
                    return True
        return False

    def resolve_target(self, spec, side=None) -> dict:
        """**目标规格 → card_id**。只解析、**不判合法性**（"挑"与"判"分开，规格 §7.6f）。

        语法与 `ops.py::attack_card(target=…)` 一致：
          int / 数字串          → 原样返回（调用方本来就知道 card_id）
          "hq"/"ehq"            → 敌方总部；"mhq"/"ohq"/"myhq" → 我方总部
          "front" / "front<i>"  → 敌方（`side` 指定时为我方）前线第 i 个，按 slot 密集名次，0 起
          "back"  / "back<i>"   → 同上，后排
          "guard" / "guard<i>"  → 敌方场上带 guard 的单位，按 slot 排，第 i 个
        `front_i` / `back_i` 里 `i` 省略 = 0。越界返回 `ok=False` + 实际数量（**不猜**）。
        ★★ 2026-09-27 用户定调：**不做卡名解析** —— "名字解析目标是没必要的。所有动作
          应该用卡牌 cardid 作为参数。因为**多个同名单位在场上是有可能的**。"
          ⇒ 名字无法消解歧义，动作入口只收 `card_id`（位置规格只是"人给短号"的便利，
          它本来就映射到确定的 card_id）。
        ★ 2026-09-27（顺手）：`front`/`back` 前缀**要求后缀是数字**（或为空）—— 否则
          `frontline` 这种串会在 `int("line")` 上抛 ValueError（以前是真会崩的输入）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()

        side = st.other_side if side is None else side          # 缺省 = 对方

        def _row(where, sd):
            rows = [c for c in st.cards if c.side == sd
                    and (c.obj.InFrontline() if where == "frontline" else c.obj.InSupportLine())
                    and c.card_type not in ("order", "counter")]
            return sorted(rows, key=lambda c: (c.slot if c.slot is not None else 99))

        if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().lstrip("-").isdigit()):
            return {"ok": True, "card_id": int(spec), "kind": "id", "side": None}
        s = str(spec or "").strip().lower()
        if s in ("hq", "ehq", "enemyhq", "enemy_hq"):
            hq = st.hq.get(st.other_side)
            if hq is None:
                return {"ok": False, "spec": spec, "error": "读不到敌方总部"}
            return {"ok": True, "card_id": hq.obj.CardID, "card": hq, "kind": "hq", "side": st.other_side}
        if s in ("mhq", "ohq", "myhq", "localhq", "my_hq"):
            hq = st.hq.get(st.my_side)
            if hq is None:
                return {"ok": False, "spec": spec, "error": "读不到我方总部"}
            return {"ok": True, "card_id": hq.obj.CardID, "card": hq, "kind": "hq", "side": st.my_side}
        if s.startswith("guard"):
            idx = int(s[5:] or 0)
            gs = [c for c in st.cards
                  if c.side == st.other_side and c.obj.IsFieldUnit()
                  and ("guard" in (c.keywords or [])
                       or ((c.raw or {}).get("all_keyword_flags") or {}).get("has_guard"))]
            gs.sort(key=lambda c: (c.slot if c.slot is not None else 99))
            if not gs:
                return {"ok": False, "spec": spec, "count": 0,
                        "error": "敌方现在没有 guard 单位 —— 可以直接打 hq"}
            if idx >= len(gs):
                return {"ok": False, "spec": spec, "count": len(gs),
                        "error": "guard 索引 %d 越界（敌方 guard 有 %d 个）" % (idx, len(gs))}
            return {"ok": True, "card_id": gs[idx].obj.CardID, "card": gs[idx],
                    "kind": "guard", "index": idx, "side": st.other_side}
        for prefix, where in (("front", "frontline"), ("back", "back")):
            if s.startswith(prefix):
                suffix = s[len(prefix):]
                if suffix and not suffix.isdigit():
                    continue            # 不是 "back2" 这种规格 ⇒ 留给卡名解析（见下）
                idx = int(suffix or 0)
                rows = _row(where, side)
                if not rows:
                    return {"ok": False, "spec": spec, "count": 0,
                            "error": "%s 座位的 %s 没有单位" % (side.name, where)}
                if idx >= len(rows):
                    return {"ok": False, "spec": spec, "count": len(rows),
                            "error": "%s 索引 %d 越界（%s 有 %d 个）"
                                     % (prefix, idx, where, len(rows))}
                return {"ok": True, "card_id": rows[idx].obj.CardID, "card": rows[idx],
                        "kind": prefix, "index": idx, "side": side}
        # ★ 2026-09-27：**不做卡名解析**（用户定调："名字解析目标是没必要的。
        #   所有动作应该用卡牌 cardid 作为参数。因为多个同名单位在场上是有可能的。"）
        #   ⇒ 目标一律 **card_id**（或 `front<i>`/`back<i>`/`hq` 这种**位置**规格）。
        #   同名歧义没法从名字上消解，所以名字根本不该是动作的入口。
        return {"ok": False, "spec": spec,
                "error": "解析不出目标规格（支持 **card_id** / hq / mhq / front[<i>] / "
                         "back[<i>] / guard[<i>]；★ 不接受卡名 —— 同名单位会有歧义，"
                         "动作一律用 card_id）"}

    def pick_target(self, exclude=(), side=None, prefer_frontline: bool = True) -> dict:
        """给"要选一个敌方目标"的动作**挑**一个目标（`ops.py::pick_target` 的同源启发式）。

        ★ 这是**启发式挑法**，不是判据：挑出来照样交给游戏裁决（§7.6f）。
        打分：`attack + (10 if 前线 else 0)`，跳过 order/counter、跳过 `is_suppressed`、
        跳过 `exclude`；一个单位都没有就退总部。返回 `{"ok","card_id","why"}`。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        ex = set(exclude or ())
        side = st.other_side if side is None else side          # 缺省 = 对方
        best, best_score = None, None
        for c in st.cards:
            if c.side != side or c.obj.CardID in ex:
                continue
            if not c.obj.IsLocatedOnBoard():
                continue
            if c.card_type in ("order", "counter"):
                continue
            if getattr(c, "is_suppressed", False):
                continue
            score = (c.attack or 0) + (10 if (prefer_frontline and c.obj.InFrontline()) else 0)
            if best_score is None or score > best_score:
                best, best_score = c, score
        if best is not None:
            return {"ok": True, "card_id": best.obj.CardID, "card": best, "why": "score=%s" % best_score}
        hq = st.hq.get(side)
        if hq is not None:
            return {"ok": True, "card_id": hq.obj.CardID, "card": hq, "why": "退总部（场上没有可选单位）"}
        return {"ok": False, "error": "挑不出目标（%s 侧连总部都读不到）" % side}
