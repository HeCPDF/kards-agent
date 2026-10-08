# -*- coding: utf-8 -*-
"""ops/diag.py —— **诊断**（`OPS-ARCHITECTURE.md` §2 旁路 / §3 规则 #23 / §7 步骤 4）。

这个模块存在的**唯一理由**，就是把"**为了问出一个答案而临时写字段**"这类东西从只读查询里
**物理搬走**：

  * `ops/query.py`（只读查询：`Can*` / `card_totals` / `gap_number` …）从 `ops.primitives`
    只 import **只读原语** ⇒ 它**写不出来**（不是靠自觉，是 import 期就没有写的能力）；
  * 需要"临时写一个字段、问完立刻还原"的诊断（`simulate_drag` 那一类）**只能**写在这里，
    并且结果里必须**自报**写了哪个字段（`writes_gamepad_field` / `writes_target_override`）
    —— 调用方一眼能看出这个答案不是"纯读出来的"。

★ 纪律（弯路 #23 的血）
========================
1. **诊断的答案不许接进判决路径**：`can_move_to(simulate_drag=True)` 的 `can` 是靠伪造
   `PC->SelectedCard`（手柄/拖拽态字段）问出来的；没有拖拽态时那个函数**恒回 False**，
   那个 False 也**不是判据**。`move_to_front` 因此干脆不问，让游戏在**提交那一刻**判。
2. **写与消费必须在同一次 JS 执行**（#19）：一律走 L1 的 `call_raw_hold`
   （= `primitives.write_then_call`，写完 → 调用 → **立刻还原**），不许 `poke` 完再 `call`。
3. 结果必须带 `writes_*` 字段**自报写了什么**（用户 2026-09-27 定调："要诊断必须显式"）。
4. 新增"要写才答得出来"的东西一律放这里，**别放回 `ops/query.py`**
   （`tests/test_ops_rules.py` 有一条 lint 盯着查询模块没有写能力）。
"""
from __future__ import annotations

import struct

from kardsmem import props
from kardsmem.pick import player_controller
from ops.consts import NOTE_OFF_CARDOBJ_TARGET_OVERRIDE


class DiagMixin:
    """**诊断**：为了问出答案而**临时写字段**的路径（与只读查询 `QueryMixin` 分开）。

    `ops.inject.Injector` 组合它 ⇒ 老的公共方法照旧可用（`probe_canplay_targeted` 就在这儿）；
    但 `ops/query.py` **不 import 这个模块**（那是步骤 4 的判据：查询模块没有写字段的能力）。
    """

    def can_move_to_simulated(self, card_id: int, location_enum: int, atk: int, cls: int, cdo: int,
                              f: int, verbose: bool = False) -> dict:
        """**诊断专用**：临时写 `PC->SelectedCard` + cursor 字段，再问
        `BattleUtilityFunctions_C::CanMoveCardToLocation`，问完立刻还原。

        ★ 由 `QueryMixin.can_move_to(simulate_drag=True)` **鸭子派发**过来（`ops/query.py`
          自己不 import 本模块、也不 import 任何写原语）。参数 `atk`/`cls`/`cdo`/`f` 由调用方
          （`board_actor_of` / `_bp_lib_fn` 的结果）解好传进来 —— 与拆分前逐字一致，避免重复解析。
        ★ 写的是"**真实鼠标按下时游戏自己会写的那几个字段**"，问完立刻还原；返回值里
          `writes_gamepad_field` 自报写了哪个字段，**别把它接进判决路径**（弯路 #23）。
        """
        acls = self.uclass_of_instance(atk)
        # 按"真实鼠标拖到该行"会产生的中间结果写 cursor（与 _drag_lifecycle 同性质）
        writes = []
        for name, kind, val in (("cardUnderCursor", "s32", card_id),
                                ("LocationNumberUnderCursor", "s32", 0),
                                ("LocationUnderCursor", "u8", location_enum),
                                ("RowUnderCursor", "u8", 1)):
            p = props.find_prop(self.ks, acls, name, pool=self.pool)
            if p:
                writes.append((atk, p["offset"], kind, val))
        # `SelectedCard` = **卡 actor**（`ABP_BaseCard_C*`），不是 `UBaseCardObject`：
        # SDK 写着 `ABP_PlayerController_C::SelectedCard // 0x0950` 是 `ABP_BaseCard_C*`，
        # 而 `CardLocation // 0x03B0` 也在 `ABP_BaseCard_C` 上（`BP_BaseCard_classes.hpp`）。
        pc = player_controller(self.ks)
        pcls = self.uclass_of_instance(pc)
        off = props.find_prop(self.ks, pcls, "SelectedCard", pool=self.pool)
        if pc and off:
            writes.append((pc, off["offset"], "ptr", atk))
        parms = bytearray(0x18)
        parms[0x00] = location_enum
        struct.pack_into("<Q", parms, 0x08, atk)
        if writes:
            out = self.call_raw_hold(writes, cdo, f, bytes(parms))     # ★ self = CDO，不是 UClass
        else:
            out = self.api().call_raw(hex(cdo), hex(f), parms.hex())
        res = out[0x10] if out and len(out) > 0x10 else None
        if verbose:
            print("    【诊断】游戏自己的 CanMoveCardToLocation(loc=%s, 伪造拖拽态=True) -> %s"
                  "（写了 PC->SelectedCard，别把它接进判决路径）" % (location_enum, res))
        return {"ok": True, "can": bool(res), "func": hex(f), "class": hex(cls),
                "location_enum": location_enum, "simulated_drag": True,
                "writes_gamepad_field": "ABP_PlayerController_C::SelectedCard // 0x0950",
                "source": "game:CanMoveCardToLocation", "raw_result": res}

    def probe_canplay_targeted(self, card_id: int, target_id: int,
                               verbose: bool = True) -> dict:
        """诊断：**临时**把目标写进卡对象的 `targetOverride`，再问这张卡自己的
        `CanPlayFromHand` 认不认它（读 `canIt` + `Reason`），问完**还原**。

        为什么需要（2026-09-25）：`BP_HandCard::OnActorEndDrag`(entry 13432) 里
        `AttemptToPlayFinal` 一旦 `canPlay=false` 就走 `ClearAndRearrange()`（卡回手、
        箭头销毁）—— 我们要分清是"**卡自己的规则不认这个目标**"还是"**写进去了但后面
        某步没过**"。这一步就把卡自己的回答原话取出来。

        ★ 步骤 4：从 `ops/query.py` 搬到这里（它**写** `targetOverride`，不是只读查询）。
          公共名/签名/**返回值**不变（`Injector.probe_canplay_targeted` 照旧可用）——
          所以这里**没有**新增 `writes_target_override` 之类的键；"这一步写了字段"只写在
          docstring 与 verbose 输出里。将来若要让结果自报（像 `can_move_to_simulated` 那样），
          那是一次**契约变更**，得单独决定，不该混在搬家这一步里。
        """
        src = self._card_object(card_id)
        tgt = self._card_object(target_id)
        if not src or not tgt:
            return {"ok": False, "stopped": "拿不到 src/tgt 的 UBaseCardObject"}
        ocls = self.uclass_of_instance(src)
        f = self.find_fn(ocls, "CanPlayFromHand")
        if not f:
            return {"ok": False, "stopped": "卡对象上没有 CanPlayFromHand（不在 438 张里）"}
        off_to = self.off(ocls, "targetOverride", NOTE_OFF_CARDOBJ_TARGET_OVERRIDE)
        parms = bytearray(0x40)
        out = self.call_raw_hold([(src, off_to, "ptr", tgt)], src, f, bytes(parms))
        buf = bytes(out) if out else b""
        if not buf:
            return {"ok": False, "stopped": "call_raw_hold 没返回"}
        res = {"ok": True, "card": card_id, "target": target_id,
               "canIt": bool(buf[0x00]), "source": "game:BaseCardObject::CanPlayFromHand"}
        for key, off in (("reason", 0x08), ("reason_param_1", 0x18),
                         ("reason_param_2", 0x28)):
            ptr, num = struct.unpack_from("<Qi", buf, off)
            if ptr and num and 1 < num <= 4096:
                raw = self.m.read_exact(ptr, num * 2)
                if raw:
                    res[key] = raw.decode("utf-16-le", "replace").rstrip("\x00")
        # 还原后确认（call_raw_hold 已还原，再读一次保险）
        res["target_override_after"] = hex(self.m.ptr(src + off_to) or 0)
        if verbose:
            print("    诊断 CanPlayFromHand(card=%s, 目标=%s) -> canIt=%s reason=%r"
                  % (card_id, target_id, res["canIt"], res.get("reason")))
        return res
