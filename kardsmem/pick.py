#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.pick —— 选择界面（抉择 / 预报 / 换牌 / 部署后选目标）的内存判据。

权威
====
`reports/KARDS-AUTOMATION.md` §7.6：

| 判据 | 位置 | 取法 |
|---|---|---|
| **正在等我选牌** | `ABP_Board_C + 0x0AC9` | `UWorld+0x30` PersistentLevel → `ULevel+0xA0` Actors → 找 `PropertiesSize ∈ {0x0F68, 0x0F70}` |
| 部署后等选目标 | `ABP_Board_C + 0x0C21` | 同上 |
| 抉择另一套 | `ABP_PlayerController_C + 0x9B4/0x9B8/0x9C8` | `UWorld+0x228` GI → `+0x38 LocalPlayers` → `ULocalPlayer+0x30` PC |

本模块**不用** board_api 的私有全局（`RVA_GWORLD` 之类）当世界锚点 ——
一切走 `Locator(session.m, session.base)`，偏移来自 `build.py`（唯一真源）。

读不出的语义
============
`MemRO.u8/i32/ptr` 读失败返回 **None**，读到 0 返回 **0**。本模块**如实区分**：

* `choose_one_active = 0`  ⇒ 字段在、值是 0 ⇒ 没在等我选
* `choose_one_active = None` ⇒ **读不出**（地址非法/进程没了）⇒ **不下结论**
* `pending` 只在有**正面证据**（某个判据==1）时为 True；全是 0/None 时为 False，
  但 `reason` 会写清"是读不出还是真的没有"。

CLI
===
    cd D:\\Kards\\reverse-data\\tools
    python -m kardsmem.pick
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from typing import Optional

from . import build as B
from . import proc as P
from .world import Locator
from .rendered import (KIND_BOUND, KIND_CANDIDATE, RenderedCard, _load_names,
                       rendered_cards, summary)

# --------------------------------------------------------------------------
# 偏移
# --------------------------------------------------------------------------
OFF_UWORLD_PERSISTENT_LEVEL = 0x30
OFF_LEVEL_ACTORS = 0xA0
OFF_UWORLD_GAME_INSTANCE = 0x228
OFF_GI_LOCALPLAYERS = 0x38
OFF_LOCALPLAYER_PC = 0x30

BOARD_CHOOSE_ACTIVE = 0x0AC9        # ABP_Board_C + 0x0AC9  u8
BOARD_SELECTING_TARGET = 0x0C21     # ABP_Board_C + 0x0C21  u8
PC_SEL, PC_TARGETS, PC_INDEX = 0x9B4, 0x9B8, 0x9C8

BOARD_SIZES = tuple(B.SIZE_BASECARD_ACTOR_CANDIDATES)   # (0x0F68, 0x0F70)


# --------------------------------------------------------------------------
# 定位
# --------------------------------------------------------------------------
def board_actor(session) -> int:
    """`ABP_Board_C` 的 actor 地址（propsize ∈ `(0x0F68, 0x0F70)`）。找不到返回 0。

    注意：`Locator.find_actors` 沿 SuperStruct 走，所以 Board 的 BP 子类也算。
    """
    loc = Locator(session.m, session.base)
    for a in loc.actors():
        if loc.is_cdo(a):
            continue
        if loc.propsize(a) in BOARD_SIZES:
            return a
    return 0


def player_controller(session) -> int:
    """`ULocalPlayer+0x30` 的 PlayerController（找不到返回 0）。"""
    loc = Locator(session.m, session.base)
    return loc.player_controller or 0


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
def _u8(m, addr: int, notes: dict, name: str):
    """读 u8 并**记下读不出**（None ≠ 0）。"""
    v = m.u8(addr)
    if v is None:
        notes[name] = "读不出 @0x%X" % addr
    return v


def pick_state(session) -> dict:
    """一次选择界面状态快照（全字段都在返回值里，读不出的写 None + `reason` 说明）。"""
    m = session.m
    notes: dict = {}
    st = {
        "board": None, "board_found": False, "board_propsize": None,
        "board_actors": 0,
        "choose_one_active": None,
        "is_selecting_hand_target": None,
        "pc": None, "pc_propsize": None,
        "pc_choose_one_selection": None,
        "pc_choose_one_card_targets": None,
        "pc_choose_one_card_targets_raw": None,
        "pc_choose_one_targets_num": None,
        "pc_choose_one_targets_max": None,
        "pc_choose_one_index": None,
        "pending": False, "reason": "", "notes": notes,
    }

    loc = Locator(m, session.base)
    st["world"] = _hex(loc.world)
    st["persistent_level"] = _hex(loc.persistent_level)
    st["level_actors"] = len(loc.actors())

    # -- Board -----------------------------------------------------------
    board = 0
    for a in loc.actors():
        if loc.is_cdo(a):
            continue
        if loc.propsize(a) in BOARD_SIZES:
            st["board_actors"] += 1
            if not board:
                board = a
    st["board"] = _hex(board)
    st["board_found"] = bool(board)
    if board:
        st["board_propsize"] = loc.propsize(board)
        st["choose_one_active"] = _u8(m, board + BOARD_CHOOSE_ACTIVE, notes,
                                      "choose_one_active")
        st["is_selecting_hand_target"] = _u8(m, board + BOARD_SELECTING_TARGET, notes,
                                             "is_selecting_hand_target")
    else:
        notes["board"] = ("propsize ∈ %s 的 actor 一个都没有（不在对局/未进棋盘）"
                          % (BOARD_SIZES,))

    # -- PlayerController ------------------------------------------------
    pc = loc.player_controller or 0
    st["pc"] = _hex(pc)
    if pc:
        st["pc_propsize"] = loc.propsize(pc)
        st["pc_choose_one_selection"] = m.i32(pc + PC_SEL)
        if st["pc_choose_one_selection"] is None:
            notes["pc_choose_one_selection"] = "读不出 @0x%X" % (pc + PC_SEL)
        # ⚠ 用 ptr_or_zero：board_api._Mem.ptr 会把"值为 0"也映射成 None
        #   （board_api.py:444 的 PTR_MIN 过滤），那样就分不清"空数组"和"读不出"。
        tg_raw = m.read_exact(pc + PC_TARGETS, 8)
        if tg_raw is None:
            notes["pc_choose_one_card_targets"] = "TArray.Data 读不出 @0x%X" % (pc + PC_TARGETS)
        elif tg_raw == b"\x00" * 8:
            # 读得到，就是 0 ⇒ 这个 TArray 现在是空的（**不是**读不出）
            st["pc_choose_one_card_targets_raw"] = 0
            st["pc_choose_one_targets_num"] = m.i32(pc + PC_TARGETS + 8)
            st["pc_choose_one_targets_max"] = m.i32(pc + PC_TARGETS + 0xC)
            notes["pc_choose_one_card_targets"] = (
                "TArray.Data==NULL ⇒ 当前是空数组（Data/num/max = 0x0/%s/%s）；**不是**读不出"
                % (st["pc_choose_one_targets_num"], st["pc_choose_one_targets_max"]))
        else:
            tg = m.ptr_or_zero(pc + PC_TARGETS)
            st["pc_choose_one_card_targets"] = _hex(tg)
            st["pc_choose_one_card_targets_raw"] = tg
            st["pc_choose_one_targets_num"] = m.i32(pc + PC_TARGETS + 8)
            st["pc_choose_one_targets_max"] = m.i32(pc + PC_TARGETS + 0xC)
            if tg == 0:
                notes["pc_choose_one_card_targets"] = (
                    "读到 0x%X：不是合法指针（PTR_MIN..PTR_MAX 之外）⇒ 视作无目标"
                    % struct.unpack("<Q", tg_raw)[0])
            if st["pc_choose_one_targets_num"] is None:
                notes["pc_choose_one_targets_num"] = "读不出 @0x%X" % (pc + PC_TARGETS + 8)
        st["pc_choose_one_index"] = m.i32(pc + PC_INDEX)
        if st["pc_choose_one_index"] is None:
            notes["pc_choose_one_index"] = "读不出 @0x%X" % (pc + PC_INDEX)
    else:
        notes["pc"] = "LocalPlayers[0]+0x30 为空/读不出（结算页常见，见 §12）"

    # -- pending ---------------------------------------------------------
    # 「正在等玩家选牌/选目标」= 有**正面证据**。读不出（None）不算证据。
    ev = []
    if st["choose_one_active"] == 1:
        ev.append("board.chooseOneActive@0x%X==1" % BOARD_CHOOSE_ACTIVE)
    if st["is_selecting_hand_target"] == 1:
        ev.append("board.isSelectingHandTarget@0x%X==1" % BOARD_SELECTING_TARGET)
    sel = st["pc_choose_one_selection"]
    if sel not in (None, 0):
        ev.append("pc.ChooseOneSelection@0x%X=%s≠0" % (PC_SEL, sel))
    st["pending"] = bool(ev)
    st["pending_evidence"] = ev

    if ev:
        st["reason"] = "正在等玩家选择：" + " 或 ".join(ev)
    else:
        unread = [k for k, v in st.items()
                  if k in ("choose_one_active", "is_selecting_hand_target",
                           "pc_choose_one_selection") and v is None]
        if unread:
            st["reason"] = ("没有选择中的正面证据，但 %s 读不出 ⇒ **不下结论**"
                            % "/".join(unread))
        else:
            st["reason"] = "三个判据都读到了且都是 0 ⇒ 当前不在选择界面"
    return st


def _hex(v) -> Optional[str]:
    return ("0x%X" % v) if v else None


# --------------------------------------------------------------------------
# 候选聚合
# --------------------------------------------------------------------------
@dataclass
class Candidates:
    """`candidates()` 的结构化返回（`dict` 视图见 `as_dict`）。"""
    state: dict
    rendered: list
    candidates: list          # kind == "candidate"（身份读不出，如天气一级三选一）
    bound: list               # kind == "bound"（CardID 与 self_ref 都有效）
    summary: dict

    def as_dict(self) -> dict:
        return {"state": self.state,
                "rendered": self.rendered,
                "candidates": self.candidates,
                "bound": self.bound,
                "rendered_summary": self.summary}


def candidates(session, state=None) -> dict:
    """`pick_state()` + 屏幕上每一张卡（`rendered`）+ 两个分组（`candidates`/`bound`）。

    `candidates` = 屏幕上**读不出身份**的卡（`CardID==0` 或 `self_ref==NULL`）——
    天气一级那三选一就是这一类（§6）；**不要**把它当成"候选牌全都在这"，
    能读出身份的候选牌会落在 `bound` 里。
    """
    st = pick_state(session) if state is None else state
    snap = None
    snap_err = None
    try:
        snap = session.snapshot()
    except Exception as e:               # noqa: BLE001
        snap_err = "%s: %s" % (type(e).__name__, e)
    rc = rendered_cards(session, snapshot=snap)
    sm = summary(rc)
    if snap_err:
        sm["snapshot_error"] = snap_err
    cand = [r for r in rc if r.kind == KIND_CANDIDATE]
    bound = [r for r in rc if r.kind == KIND_BOUND]
    return Candidates(state=st, rendered=rc, candidates=cand, bound=bound,
                      summary=sm).as_dict()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        s = P.attach()
    except Exception as e:               # noqa: BLE001
        print("attach 失败：%s: %s" % (type(e).__name__, e))
        return 2

    print("== kardsmem.pick ==")
    print("session: %s" % s.describe())
    try:
        st = pick_state(s)
        print("--- pick_state() ---")
        print("world                 = %s" % st["world"])
        print("persistent_level      = %s" % st["persistent_level"])
        print("level_actors          = %d" % st["level_actors"])
        print("board                 = %s   board_found=%s  propsize=%s  命中 actor 数=%d"
              % (st["board"], st["board_found"], st["board_propsize"], st["board_actors"]))
        print("  (期望 propsize ∈ %s)" % (BOARD_SIZES,))
        print("choose_one_active     @0x%X = %s" % (BOARD_CHOOSE_ACTIVE, st["choose_one_active"]))
        print("is_selecting_hand_tgt @0x%X = %s" % (BOARD_SELECTING_TARGET,
                                                    st["is_selecting_hand_target"]))
        print("pc                    = %s   propsize=%s" % (st["pc"], st["pc_propsize"]))
        print("pc.ChooseOneSelection @0x%X = %s" % (PC_SEL, st["pc_choose_one_selection"]))
        print("pc.ChooseOneCardTargets @0x%X = %s  num@+8 = %s  max@+0xC = %s"
              % (PC_TARGETS, st["pc_choose_one_card_targets"], st["pc_choose_one_targets_num"],
                 st["pc_choose_one_targets_max"]))
        print("pc.chooseOneIndex     @0x%X = %s" % (PC_INDEX, st["pc_choose_one_index"]))
        print("pending               = %s" % st["pending"])
        print("reason                = %s" % st["reason"])
        if st["notes"]:
            print("notes（读不出的字段就是读不出，不是 0）：")
            for k, v in st["notes"].items():
                print("  %-28s %s" % (k, v))

        # 屏幕卡（顺带把 rendered 的分组也打出来；不额外多读内存）
        loc = Locator(s.m, s.base)
        ba = board_actor(s)
        if ba:
            print("board_actor(session) = 0x%X  propsize=%s  uclass=0x%X"
                  % (ba, loc.propsize(ba), loc.uclass_of(ba)))
            if _names_available():
                print("  names.py 存在 → class_name 由 rendered.py 填入")
            else:
                print("  class_name = None：可选模块 kardsmem.names 不存在，**不猜**"
                      "（UClass 的 FName 在 uclass+0x%X）" % B.OFF_UOBJECT_NAME)
        else:
            print("board_actor(session) = 0（无 ABP_Board_C actor）")

        c = candidates(s, state=st)
        sm = c["rendered_summary"]
        print("--- candidates() ---")
        print("屏幕卡 = %d  kind 分布=%s" % (sm["total"], sm["by_kind"]))
        print("身份读不出（kind=candidate）= %d" % len(c["candidates"]))
        for r in c["candidates"]:
            print("  " + r.line())
        print("身份确定（kind=bound）     = %d" % len(c["bound"]))
        for r in c["bound"]:
            print("  " + r.line())
        print("匹配上 %d / 未匹配 %d" % (sm["matched"], sm["unmatched"]))
        if sm.get("snapshot_error"):
            print("snapshot 失败：%s（因此未做配对）" % sm["snapshot_error"])
        for u in sm["unmatched_detail"]:
            print("  未匹配 %s CardID=%s name=%r notes=%s"
                  % (u["actor"], u["card_id"], u["name"], u["notes"]))
    finally:
        s.close()
    return 0


# --------------------------------------------------------------------------
# 换牌：逐卡的"待替换"标记 —— **仍未找到**
# --------------------------------------------------------------------------
HANDCARD_PROPSIZE = 3640          # 0xE38 = BP_HandCard_C
OFF_HC_CARDOBJ = 0x808            # BP_HandCard_C -> UBaseCardObject*（+0xD60 处有同一指针）
OFF_HC_CARDOBJ2 = 0xD60

# ★★ +0x924 不是换牌标记。这条曾被当成结论提交过（e265378），已撤回。
#   反射链给出了它的真身：**`MulliganHoverTimeline__Direction`**（ByteProperty）
#   —— 悬停动画 Timeline 的播放方向。一切都对上了：
#     * 名字里有 Mulligan，所以与换牌界面强相关
#     * 点一张就翻转，因为点击触发 MulliganHover()
#     * 任意时刻最多一个“反向播放中”—— 同一时刻只有一张在做那个动画
#   教训：字节 diff 能找到“跟着动作变的东西”，但分不出“状态”和“该状态的动画”。
OFF_HC_UNKNOWN_924 = 0x924        # = MulliganHoverTimeline__Direction

# 换牌标记 = `BP_HandCard_C::shouldDiscard`（BoolProperty，本构建 **+0x980**）。
#
#   不要硬编码这个 0x980：蓝图类的字段布局是运行时按 FProperty 链建的，
#   换个构建就变。`mulligan_marks()` 走 `props.find_prop(uclass, "shouldDiscard")`
#   现算，下面这个常量只是**给人看的备注**。
OFF_HC_SHOULDDISCARD_HINT = 0x980
#
#   怎么找到的（方法本身比结论值钱）：
#     1. 导出的蓝图里搜 mulligan → `BP_HandCard.cpp` 的 `MulliganDiscardToggle()`
#        就是 `shouldDiscard = !shouldDiscard`，true 走 `ShowDiscardStatus()`
#        （屏幕上那个红色禁止圈），false 走 `RemoveDiscardStatus()`。
#        **名字是蓝图给的，不是猜的。**
#     2. 偏移问引擎自己的反射链要（`kardsmem.props`），不 diff 字节。
#     3. 拿原先那两份转储回归：before 四张全 False，after **恰好** slot0 一张 True，
#        与文件名"换第一张牌前/后"完全一致。
#   ⚠ 目前只过了转储回归，**还没做实机多选回归**（标两张、取消一张）。
#
# 之前排除过的位置（留档，证明搜索空间已经扫过）：
#   * `UBaseCardObject` 整类 0x678 —— 两次独立实机点击，零字节变化
#     （对的：标记在 **actor**`BP_HandCard_C` 上，不在卡对象上）
#   * `ABP_Board_C` 前 0x1000 / `ABP_PlayerController_C` 前 0xA00 —— 无可逆变化
#   * "把指针加进待换列表" —— 引用集合前后完全一致
#   教训：字节 diff 是最后手段。先问"这个语义在蓝图里叫什么名字"，
#   再用反射链把名字换成偏移 —— 一步到位，还不依赖抓帧时机。


def hand_card_actors(session) -> list:
    """本方手牌的 `BP_HandCard_C` actor → [{actor, card_ptr, card_id, name, slot}]。

    它本身是可用的（换牌标记的搜索范围就是它），只是标记字段未知。
    ⚠ 对方手牌也有 `BP_HandCard_C` 且同样持有卡对象（背面不代表读不到），
      所以默认按 side 过滤到本方。
    """
    loc = Locator(session.m, session.base)
    m = session.m
    out = []
    for p in (loc.actors() or []):
        if loc.propsize(p) != HANDCARD_PROPSIZE:
            continue
        cobj = m.ptr(p + OFF_HC_CARDOBJ)
        if not cobj:
            continue
        rec = {"actor": p, "card_ptr": cobj, "card_id": None, "name": None,
               "slot": None, "side": None}
        try:
            from . import cards as _C
            c = _C.read_raw(session, cobj)
            if c:
                rec["card_id"] = c.get("card_id")
                rec["name"] = c.get("name")
                rec["slot"] = c.get("location_number")
                rec["side"] = c.get("side")
        except Exception:                                    # noqa: BLE001
            pass
        if rec.get("side") == "enemy":
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.get("slot") is None, r.get("slot")))
    return out


def mulligan_marks(session) -> list:
    """换牌界面里每张手牌的"要换掉"标记 → hand_card_actors() 的记录 + `marked`。

    标记位不是猜出来的，是**从蓝图拿名字、从反射链拿偏移**：
      · `BP_HandCard.cpp` 的 `MulliganDiscardToggle()` 把成员 `bool shouldDiscard`
        取反，true 走 `HandCardLook->ShowDiscardStatus()`（屏幕上那个红圈）。
      · 偏移由 `props.find_prop()` 走 `FProperty` 链现算 —— 蓝图类的字段布局是
        运行时建的，任何硬编码常量都只对一个构建成立。

    ★ 这条路子是上一轮那个错误结论（把 `+0x924` 当标记位）的正解：
      当时是先 diff 字节再猜语义，一次巧合就上了钩；反射链是**引擎自己的答案**。
    """
    from .props import find_prop, read_bool
    from .world import Locator

    rows = hand_card_actors(session)
    if not rows:
        return rows
    loc = Locator(session.m, session.base)
    uclass = loc.uclass_of(rows[0]["actor"])
    prop = find_prop(session, uclass, "shouldDiscard") if uclass else None
    for r in rows:
        r["marked"] = read_bool(session.m, r["actor"], prop) if prop else None
    if prop:
        rows[0].setdefault("_prop", prop)
    return rows


def _names_available() -> bool:
    """`kardsmem.names` 在不在（可选模块；**不缓存失败**，它在另一条线上可能刚被写出来）。"""
    return _load_names() is not None


if __name__ == "__main__":
    raise SystemExit(main())
