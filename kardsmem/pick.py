#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.pick —— 选择界面（抉择 / 预报 / 换牌 / 部署后选目标）的内存判据。

权威
====
`reports/spec/KARDS-AUTOMATION.md` §7.6：

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
        #   （kardsmem/board.py:444 的 PTR_MIN 过滤），那样就分不清"空数组"和"读不出"。
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
# 三选一候选：身份 + 屏幕次序 —— `ABP_ChooseCardToSpawn_C` 自己就有答案
# --------------------------------------------------------------------------
# ★ **这是任何"三选一"界面的通用读法，不是预报专属**：不管背后是
#   `keepOrder=true`（固定几张，如预报/发现天气类型）还是 `keepOrder=false`
#   （蓝图给候选池、原生抽样，如"好人寥寥"），显示出来的候选**都**是这个类的
#   实例，字段语义完全一样——两条路径 2026-09-24 都实机验证过，别按触发它的
#   具体卡把这个函数当成"预报专用"再去重复造一个。
# ★ 2026-09-24 实机命中一次真的预报选择（OVERCAST 触发）才挖出来的——比规格里
#   原计划的 `_forecastOptions`（读触发卡自己的 TArray）简单得多，**不用先找到
#   触发卡**：candidate 自己（`ABP_ChooseCardToSpawn_C`，继承 `ABP_BaseCard_C`）
#   身上就有一份完整的自证：
#     `indexOfCardInDeck@0x0878` (int32) —— 屏幕第几个（0 起，从左到右），
#         实机核对过：0=蓝天(最左)/1=薄雾(中)/2=狂风(最右)，跟视觉顺序一致。
#     `Name_0@0x095C` (FName)  —— 这张候选卡的内部名字（如 `card_event_rain1_mist`），
#         直接给身份，不需要 CardID（这类候选的 CardID 本来就读不出，恒为 0）。
#     `cardBeingPlayedID@0x0968` (int32) —— 触发这次选择的那张卡的 CardID
#         （实机例子：35 = OVERCAST/阴云密布，能反查 `session.snapshot()` 核对）。
#     `SelectedCard@0x0964` (bool) —— 选没选中这张（点完应该会翻）。
#   全部来自 SDK dump（`BP_ChooseCardToSpawn_classes.hpp`），不是猜的/diff 出来的。
CHOOSE_CLASS = "BP_ChooseCardToSpawn_C"
OFF_CCS_INDEX = 0x0878
OFF_CCS_CARD_BEING_PLAYED = 0x0880
OFF_CCS_NAME = 0x095C
OFF_CCS_SELECTED = 0x0964
OFF_CCS_CARD_BEING_PLAYED_ID = 0x0968
OFF_CCS_OWNED_BY_ME = 0x096C          # bool `isOwnedByMe`（SDK 逐字段核实，2026-09-24）


def choose_candidates(session, owned_only: bool = True) -> list:
    """当前 `BP_ChooseCardToSpawn_C` 候选（**任何三选一界面通用**——`keepOrder=true`
    固定几张的那一类，如预报/发现天气类型；也包括 `keepOrder=false` 蓝图给候选池、
    原生抽样的那一类，如"好人寥寥"；两条路径都实机验证过，字段语义一样），
    按 `indexOfCardInDeck` 排好序 → [{actor, index, name, selected, trigger_card_id,
    owned_by_me}]。

    `name` 是内部名字（`card_event_rain1_mist` 这种），不是显示名——要中文/英文显示名
    还得再查一层卡表（`reverse-data/sdk/<build>/kards/kards_data.json` 之类），
    这里只负责"读到身份 + 排好屏幕顺序"。空列表 = 现在没有这类候选在等。

    ★ `owned_only=True`（默认）：只返回 `isOwnedByMe==True` 的候选。这条字段
      2026-09-24 才从 SDK 逐字段表里核实到（`BP_ChooseCardToSpawn_classes.hpp`
      `bool isOwnedByMe; // 0x096C`），此前 KARDS-NN.md §11 一直把"可能读到
      对手的三选一候选"记成未验证的泄露风险——**没有实机验证过这个字段本身
      的取值语义**（没有对手正在选三选一的场景可测），先按"存在就该信"的
      原则默认过滤，传 `owned_only=False` 拿到未过滤的原始集合（调试/核实用）。
    """
    from .objects import ObjectArray
    from .names import FNamePool
    oa = ObjectArray(session)
    pool = FNamePool(session.m, session.base)
    out = []
    for a in oa.find_by_class_name(CHOOSE_CLASS):
        owned = bool(session.m.u8(a + OFF_CCS_OWNED_BY_ME))
        if owned_only and not owned:
            continue
        out.append({
            "actor": a,
            "index": session.m.i32(a + OFF_CCS_INDEX),
            "name": pool.fname_of(a, OFF_CCS_NAME),
            "selected": bool(session.m.u8(a + OFF_CCS_SELECTED)),
            "trigger_card_id": session.m.i32(a + OFF_CCS_CARD_BEING_PLAYED_ID),
            "owned_by_me": owned,
        })
    out.sort(key=lambda r: (r["index"] is None, r["index"]))
    return out


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


# --------------------------------------------------------------------------
# 手牌 actor：权威路子 —— `BP_Deck_C::CardsPulledFromDeckBeforeBeingPutIntoHand`
# --------------------------------------------------------------------------
# ★ 规格 §11.2 P1：`hand_card_actors()` 是"扫全场按 PropertiesSize==3640 猜
#   BP_HandCard_C，再按 side 过滤"——这是近似（对方手牌背面也是同一个类，
#   只能靠 `cards.read_raw` 读出的 side 字段事后过滤，逻辑上跟游戏自己"这张牌
#   在不在我手上"的账本没有关系，是从结果反推）。
#
#   权威集合是 `BP_Deck_C` 自己维护的那个数组，字段名和类型来自 SDK dump
#   （`BP_Deck_classes.hpp`，只 grep 出来，没有猜）：
#     TArray<class ABP_BaseCard_C*>  CardsPulledFromDeckBeforeBeingPutIntoHand
#         // 声明偏移 0x0378（本构建 1.58.27125.Steam，仅供人看，不硬编码）
#     bool                            deckForEnemy
#         // 声明偏移 0x0478（本构建），唯一权威的"这副牌堆是哪一方的"判据——
#         //   不用再靠"读出牌身份、按 card.side 过滤"这种间接推断。
#   两个都是**蓝图变量**，跟 `shouldDiscard`（换牌标记）一样，字段布局是运行时
#   按 FProperty 链建的：这里只把声明偏移当"给人看的备注"，实际偏移一律用
#   `props.find_prop()` 现算，换构建也不用改代码。
#
#   数组元素是 `ABP_BaseCard_C*`（卡的**场景 actor**），不是 `UBaseCardObject*`
#   （`cards.read_raw` 吃的那个）。桥接字段同样查 SDK 得到：
#     `ABP_BaseCard_C::selfBaseCardRef`  class UBaseCardObject*  // 0x0808
#   `BP_HandCard_C` 继承自 `ABP_BaseCard_C`（`BP_HandCard_classes.hpp:24`），
#   这正是 `hand_card_actors()` 里 `OFF_HC_CARDOBJ = 0x808` 一直好使的原因——
#   两条路子读的其实是**同一个继承字段**，只是一个硬编码、一个现算。
#
# ⚠ 验证现状（诚实写明，别糊弄）：2026-09-24 写这段代码时游戏在跑但
#   **不在对局中**（主菜单，`match_active=False`）——`BP_Deck_C` 在主菜单下
#   有没有实例、`CardsPulledFromDeckBeforeBeingPutIntoHand` 是不是空数组，
#   都取决于游戏什么时候创建/销毁这个 actor，**这次没有实机对局数据核实过**。
#   已验证的只是**结构**：三个字段名都在当前构建（1.58.27125.Steam）的 SDK
#   dump 里查得到、类型对得上（`TArray<T*>` / `bool` / `T*`），`selfBaseCardRef`
#   的偏移与生产代码里已经在用的 `OFF_HC_CARDOBJ` 一致（交叉印证，不是巧合）。
#   下次有真实对局时，用 `hand_card_actors_v2()` 和 `hand_card_actors()`
#   （旧的 propsize 扫描）交叉比对结果，对不上就说明这里有问题。
DECK_CLASS = "BP_Deck_C"
OFF_DECK_CARDS_HINT = 0x378         # 给人看的备注，实际现算，见上面注释
OFF_DECK_FOR_ENEMY_HINT = 0x478     # 同上


def deck_actors(session) -> list:
    """两个 `BP_Deck_C` 实例（本方/敌方）→ [{actor, deck_for_enemy, side}]。

    `deckForEnemy` 是唯一权威 side 判据（反射链现算）；读不出就是 `side=None`，
    **不猜**（None ≠ False，别把"读不出"当成"本方"）。
    """
    from .objects import ObjectArray
    from .props import find_prop, read_bool

    loc = Locator(session.m, session.base)
    oa = ObjectArray(session)
    out = []
    for a in oa.find_by_class_name(DECK_CLASS):
        uclass = loc.uclass_of(a)
        prop = find_prop(session, uclass, "deckForEnemy") if uclass else None
        is_enemy = read_bool(session.m, a, prop) if prop else None
        side = None if is_enemy is None else ("enemy" if is_enemy else "local")
        out.append({"actor": a, "deck_for_enemy": is_enemy, "side": side})
    return out


def hand_card_actors_v2(session, side: str = "local") -> list:
    """手牌 actor 的权威读法：直接读 `BP_Deck_C::CardsPulledFromDeckBeforeBeingPutIntoHand`。

    返回格式跟 `hand_card_actors()` 对齐（同一批 key），方便直接互相比对/替换：
    `[{actor, card_ptr, card_id, name, slot, side}]`。

    跟旧实现（扫全场 propsize 猜 `BP_HandCard_C` 再按 side 过滤）的区别：
    这里不扫描、不猜——数组本身就是游戏自己维护的"这副牌堆已抽进手牌的卡"账本，
    `side` 由 `deckForEnemy` 直接给，不用等读出卡身份再反推。

    找不到对应 side 的 Deck，或数组是空的，返回 `[]`（不报错——主菜单/结算页
    很可能就是没有这个 actor 或数组为空，属于正常状态，见函数上方注释）。
    """
    from .props import find_prop

    m = session.m
    loc = Locator(m, session.base)

    decks = [d for d in deck_actors(session) if d["side"] == side]
    if not decks:
        return []
    deck = decks[0]["actor"]

    duclass = loc.uclass_of(deck)
    arr_prop = (find_prop(session, duclass, "CardsPulledFromDeckBeforeBeingPutIntoHand")
                if duclass else None)
    if not arr_prop or arr_prop.get("offset") is None:
        return []
    base = deck + arr_prop["offset"]
    data_ptr = m.ptr_or_zero(base)
    num = m.i32(base + 8) or 0
    if not data_ptr or num <= 0:
        return []

    raw = m.read(data_ptr, num * 8)
    if not raw:
        return []

    actors = [int.from_bytes(raw[i * 8:i * 8 + 8], "little") for i in range(num)]
    actors = [a for a in actors if a]
    if not actors:
        return []

    # selfBaseCardRef：跟旧 hand_card_actors() 的 OFF_HC_CARDOBJ 是同一个继承
    # 字段（ABP_BaseCard_C 基类成员，BP_HandCard_C 直接继承），这里现算而不是
    # 复用那个硬编码常量，避免"两条路子分别对/错"互相掩盖问题。
    a0_uclass = loc.uclass_of(actors[0])
    cardref_prop = find_prop(session, a0_uclass, "selfBaseCardRef") if a0_uclass else None

    out = []
    for a in actors:
        rec = {"actor": a, "card_ptr": None, "card_id": None, "name": None,
               "slot": None, "side": None}
        cobj = m.ptr_or_zero(a + cardref_prop["offset"]) if cardref_prop else None
        if cobj:
            rec["card_ptr"] = cobj
            try:
                from . import cards as _C
                c = _C.read_raw(session, cobj)
                if c:
                    rec["card_id"] = c.get("card_id")
                    rec["name"] = c.get("name")
                    rec["slot"] = c.get("location_number")
                    rec["side"] = c.get("side")
            except Exception:                                # noqa: BLE001
                pass
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
