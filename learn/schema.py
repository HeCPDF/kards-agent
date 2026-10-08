#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.schema —— 样本 schema + **可见性投影**（录制与训练的唯一契约）。

见 `reverse-data/reports/spec/KARDS-NN.md` §1.3（样本 schema）/ §2（可见性）。
投影规则只在这一处实现，`player/record.py` 写样本时调用它，别在别处再抄一份。

★ 已拍板（§2.2）：
    - 敌方手牌：只给张数，不给内容（P1）。
    - 双方牌库剩余的**顺序/身份**：不给（P2/P3，真隐藏，玩家自己也不知道）。
    - 但**己方卡组名单**（这 30 张是什么）不算隐藏信息——玩家开局就知道，
      给它是"策略空间"的必要条件（§3.4），只是不给"剩余顺序"。
    - 未揭示的隐蔽/背面牌身份：不给，除非 `is_revealed==1`（P4）。
    - `Hidden*` 命名的字段（如 `ReconnectHiddenEnemyKreditsByTurnNumber`）：不用（P6）。

★ 已知未解决的口子（如实标注，不假装修好）：
    - P5：`kardsmem.pick.choose_candidates()` 扫 `BP_ChooseCardToSpawn_C` 时
      **不按 owner 过滤**，理论上可能把对手的三选一候选也读出来——本模块
      目前不做二次过滤（没有可靠的 owner 字段可用），录制器要留意这条。
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

# ★ 座位迁移（docs/SEAT-MIGRATION.md）：样本里的座位一律是游戏的 `ESide` 数值（1=left / 2=right，
#   落盘就是整数 1/2），**没有**字符串座位。"我方"只是谓词：`card.side == st.my_side`。
#   本层只许依赖 base（tests/test_arch_rules.py 分层表），所以这里不 import kardsmem：座位用整数（ESide 是 IntEnum，
#   和 1/2 互换比较），牌的位置/座位全靠 `card.obj`（原版 BaseCardObject）上的谓词与字段读。
SEATS = (1, 2)

PHASES = ("mulligan", "main", "pick", "forecast", "hand_target",
          "deploy_target", "deck_pick")


def other_seat(seat) -> int:
    """对面的座位（1<->2）。座位不是 1/2 => ValueError（不猜）。"""
    seat = int(seat)
    if seat not in SEATS:
        raise ValueError("座位必须是 1/2：%r" % (seat,))
    return 2 if seat == 1 else 1


def seat_of(v) -> Optional[int]:
    """卡/状态上的座位值 -> 整数 1/2；None / NotAvailable(0) / 其它 -> None。"""
    if v is None:
        return None
    v = int(v)
    return v if v in SEATS else None


def zone_of(c) -> str:
    """牌所在的区域名（样本里的 `location` 字段，ML 特征词表 `encode.ZONE_NAMES` 的一员）：
    hq / frontline / back（支援线单位）/ hand / discard / deck / "?"（位置读不出）。
    全由 `c.obj` 的原版位置谓词现算；不比较位置字符串。"""
    o = c.obj
    if o.IsHQ():
        return "hq"
    if o.InFrontline():
        return "frontline"
    if o.InSupportLine():
        return "back"
    if o.InHand():
        return "hand"
    if o.InDiscard():
        return "discard"
    if getattr(getattr(o, "Location", None), "name", "").startswith("Deck"):
        return "deck"
    return "?"


def _by_seat(d, seat):
    """按座位取值：内存里的键是整数，经 JSON 往返后是 "1"/"2" 字符串，两种都认。"""
    if not d:
        return None
    seat = int(seat)
    if seat in d:
        return d[seat]
    return d.get(str(seat))


# ---------------------------------------------------------------------------
# 可见性投影
# ---------------------------------------------------------------------------
def _card_public_view(c, viewer: int, full_hand_of_viewer: bool) -> Optional[dict]:
    """一张卡该给查看者（`viewer`，座位 1/2）看到什么。返回 `None` 表示"这张卡整体不给"
    （目前没有需要整条隐藏的情形，占位用）。
    """
    side = seat_of(c.side)
    is_mine = side == viewer                # 『查看者自己的牌』；side 读不出 => 当作对方的（更保守）
    loc = zone_of(c)

    # P4（2026-09-26 更正为**只对场上单位**）：未揭示的隐蔽单位不给身份。
    #   ★ 用户原话："isRevealed 只对场上单位有意义：不可被效果指向，攻防数值等均看不到。"
    #   ⇒ 手牌**不**受 `is_revealed` 管（`RevealCard` 写的是 `isRevealed=1` + `hasCovert=0`，
    #     语义就是把场上隐蔽单位翻正）。
    hidden_identity = (loc in ("frontline", "back")
                       and bool(c.obj.hasCovert) and not c.obj.isRevealed)

    # P1/P7：对方手牌只给"这里有一张"，不给内容——**但 intel 揭示过的例外**。
    #   ★ 2026-09-26 实机坐实：`UBaseCardObject::cardSeen // 0x0260` 是"这张（敌方手牌）
    #     已被我方看过"的持久标记（`SetCardsSeenByCipher` 只挑 `!cardSeen` 的再洗牌取 N 张
    #     ⇒ 天然不重复命中）。实测 intel 1 = 敌方 4 张手牌里恰好 1 张 `card_seen=1`。
    #   ⚠ 该标记**只在它还在手牌里时有效**（打出/离开手牌后同一 uid 上会变回 0）
    #     ⇒ 要"记牌"必须在看到的那一刻由我们自己落进动作流 token（`KARDS-NN.md` §2.2）。
    if loc == "hand" and not is_mine:
        if c.obj.cardSeen:
            return {
                "side": side, "location": "hand", "uid": None, "card_id": c.obj.CardID,
                "name": c.name, "attack": None, "defense": None,
                "kredit_cost": c.obj.getTotalKredits(), "slot": None,
                "is_revealed": None, "hidden": False, "reason": "P4b_intel_seen",
            }
        return {"side": side, "location": "hand", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P1_enemy_hand"}

    # P2/P3：双方牌库剩余——不给身份/顺序；但己方"卡组名单"走另一条口（见下）
    if loc == "deck":
        return {"side": side, "location": "deck", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P2_deck_remaining"}

    if hidden_identity:
        return {"side": side, "location": loc, "uid": None, "card_id": None,
                "name": None, "attack": None, "defense": None,
                "hidden": True, "reason": "P4_covert"}

    # 其余：可见（场上/总部/弃牌/己方手牌）——P8/P9/P10
    return {
        "side": side, "location": loc, "uid": c.uid, "card_id": c.obj.CardID,
        "name": c.name, "attack": c.attack, "defense": c.defense,
        "kredit_cost": c.obj.getTotalKredits(), "slot": c.slot,
        "is_revealed": c.obj.isRevealed,
        "hidden": False,
    }


def project_state(st, viewer=None, deck_roster=None) -> dict:
    """把一份 `BoardState` 投影成"`viewer` 这一侧真人玩家看得到的东西"。

    `viewer`：座位（`ESide` / 1 / 2）；缺省 = 我方（`st.my_side`），**读不出就抛 ValueError**，不默认按 1。
    落盘字段（座位一律整数 1/2）：`viewer`、`my_side`（本局本地座位，读不出为 None）、
    `hand_count/deck_count/kredits`（键 = 座位整数）、`cards[*].side`。

    `deck_roster`：`viewer` 自己的卡组名单（fname 多重集合，不含顺序），可以给 list，也可以给
    `{座位: list}`（只取 `viewer` 那一项，对方的名单一概不透出）。这不是从 `st` 里推出来的
    （§2.2：牌库剩余身份不给，但开局卡组名单不算隐藏信息），要由调用方在录制**开局**时另外提供一次
    （比如从换牌前的完整起始牌库读一次，只做这一次，别每个决策点都重读）。
    """
    if viewer is None:
        if st.my_side is None:
            raise ValueError("本地座位 mySide 读不出：project_state 不能默认按 1 号座位（请显式传 viewer）")
        viewer = st.my_side
    viewer = int(viewer)
    if viewer not in SEATS:
        raise ValueError("viewer 必须是座位 1/2：%r" % (viewer,))

    cards = []
    for c in st.cards:
        v = _card_public_view(c, viewer, full_hand_of_viewer=True)
        if v is not None:
            cards.append(v)

    hand_counts = {seat: len(st.hand(seat)) for seat in SEATS}
    deck_counts = {seat: sum(1 for c in st.cards if seat_of(c.side) == seat and zone_of(c) == "deck")
                   for seat in SEATS}

    out = {
        "viewer": viewer,
        "my_side": seat_of(st.my_side),
        "turn": st.turn,
        "hand_count": hand_counts,     # P1/P7：对方只给数量；己方数量也顺带给，卡本身在 cards 里已有内容
        "deck_count": deck_counts,     # P2/P3：双方都只给数量（顺序/身份不给）
        "kredits": {int(k): v for k, v in (getattr(st, "kredits", None) or {}).items()},
        "cards": cards,
    }
    if deck_roster is not None:
        if isinstance(deck_roster, dict):
            mine = _by_seat(deck_roster, viewer)
        else:
            mine = deck_roster
        out["deck_roster"] = {viewer: sorted(mine or [])}
        # ★ 对面的卡组名单不给（§3.4："敌方卡组：不给名单，只给已见"）——
        #   即使调用方传了对面的名单，这里也不透出去，双重保险。
    return out


def state_hash(projected: dict) -> str:
    """同局面去重用。只依赖已投影后的字典，不依赖字典 key 顺序。"""
    blob = json.dumps(projected, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 对面视角（KARDS-NN.md §2.3）
# ---------------------------------------------------------------------------
def other_side(viewer) -> int:
    return other_seat(viewer)


def project_both_seats(st, viewer_self=None, deck_roster=None) -> dict:
    """一局、两个座位各投影一次（`--peek` 全量另存的用途）。

    ★ 2026-09-26 用户定调："全量另存 `--peek` 可以用于补充训练集。**对面视角**。"
    ⇒ 同一份状态按 `seat` 投影两次，样本 ×2；标签也是现成的（对手当时真做的那个动作）。
    对面视角的样本**不许比那个座位当时能看到的更多**（拿不准就标 unknown/false）。
    `viewer_self` 缺省 = 我方（`st.my_side`，读不出 => ValueError）。
    """
    if viewer_self is None:
        if st.my_side is None:
            raise ValueError("本地座位 mySide 读不出：project_both_seats 不能默认按 1 号座位")
        viewer_self = st.my_side
    return {
        "self": project_state(st, viewer_self, deck_roster=deck_roster),
        "other": project_state(st, other_seat(viewer_self), deck_roster=deck_roster),
    }


# ---------------------------------------------------------------------------
# 泄漏测试（KARDS-NN.md §2.2 P0 验收；"能失败"的测试）
# ---------------------------------------------------------------------------
def leak_check(projected: dict, viewer) -> list:
    """返回**违规项**列表（空 = 通过）。故意做成"能失败"，见 `selftest()` 的反例。

    四条断言（§2.2）：
      ① 对方手牌身份 0 条 —— 例外：`reason == "P4b_intel_seen"`（`cardSeen==1`）；
      ② 对方牌库内容/顺序 0 条；
      ③ 对方卡组名单 0 条（`deck_roster` 里不许出现 `viewer` 以外的座位）；
      ④ 未揭露的隐蔽**场上**牌身份/攻防 0 条。
    """
    viewer = int(viewer)
    bad = []
    if projected.get("viewer") != viewer:
        bad.append("viewer 字段被改动：%r != %r" % (projected.get("viewer"), viewer))
    for c in projected.get("cards") or []:
        side, loc = c.get("side"), c.get("location")
        reason = c.get("reason")
        if side != viewer and loc == "hand":
            if reason != "P4b_intel_seen" and (c.get("card_id") or c.get("name")):
                bad.append("① 对方手牌身份泄漏：card_id=%r name=%r" % (c.get("card_id"), c.get("name")))
        if side != viewer and loc == "deck":
            if c.get("card_id") or c.get("name") or c.get("uid"):
                bad.append("② 对方牌库内容泄漏：%r" % (c,))
        if reason == "P4_covert" and (c.get("card_id") or c.get("attack") is not None
                                     or c.get("defense") is not None):
            bad.append("④ 未揭露隐蔽单位泄漏：%r" % (c,))
    roster = projected.get("deck_roster") or {}
    for k, v in roster.items():
        if int(k) != viewer and v:
            bad.append("③ 对方卡组名单泄漏：座位%s %r" % (k, v))
    return bad


# ---------------------------------------------------------------------------
# 自检（离线，不需要游戏）
# ---------------------------------------------------------------------------
class _FakeState:
    """最小 BoardState 替身：只有 `schema` 用到的接口（cards/turn/kredits/my_side/hand）。
    牌是真的 `kardsmem.board.Card`（带原版 `BaseCardObject`），由调用方的 `mk` 工厂造。"""

    def __init__(self, cards, my_side=1):
        self.cards = cards
        self.my_side = my_side
        self.turn, self.kredits = 7, {1: 5, 2: 3}

    def hand(self, side=None):
        side = self.my_side if side is None else side
        if side is None:
            raise ValueError("my_side 读不出")
        return [c for c in self.cards if c.obj.side == side and c.obj.InHand()]


def selftest(mk) -> int:
    """两组会导致**不同结论**的输入（CLAUDE.md 弯路 #11 的纪律），外加一个必须失败的反例。

    `mk(card_id, side, zone, name, covert=False, is_revealed=False, card_seen=False)` 造真 `Card`
    （`python -m learn.schema` 会从 tests/_learn_cards.py 提供；本层不 import kardsmem）。
    本地座位固定 1，对方 2。"""
    ME, OPP = 1, 2
    st = _FakeState([
        mk(11, ME, "hand", "MY CARD"),
        mk(22, OPP, "hand", "HIDDEN ONE", card_seen=False),
        mk(23, OPP, "hand", "SEEN ONE", card_seen=True),
        mk(33, OPP, "back", "COVERT GUY", covert=True, is_revealed=False),
        mk(34, OPP, "back", "OPEN GUY", covert=True, is_revealed=True),
        mk(44, OPP, "deck", "DECK CARD"),
    ], my_side=ME)
    fails = 0
    p = project_state(st, ME, deck_roster={ME: ["A", "B"], OPP: ["X", "Y"]})
    chk = [
        ("intel 看过的敌手牌给身份", any(c.get("card_id") == 23 for c in p["cards"])),
        ("没看过的敌手牌不给身份", not any(c.get("card_id") == 22 for c in p["cards"])),
        ("未揭露隐蔽单位不给身份/攻防",
         any(c.get("reason") == "P4_covert" and not c.get("card_id") and c.get("attack") is None
             for c in p["cards"])),
        ("已揭露隐蔽单位给身份", any(c.get("card_id") == 34 for c in p["cards"])),
        ("敌牌库不给身份", not any(c.get("card_id") == 44 for c in p["cards"])),
        ("己方手牌给身份", any(c.get("card_id") == 11 for c in p["cards"])),
        ("对方卡组名单不透出", OPP not in (p.get("deck_roster") or {})),
        ("己方卡组名单透出", (p.get("deck_roster") or {}).get(ME) == ["A", "B"]),
        ("落盘字段：viewer/my_side 是整数 1/2", p["viewer"] == ME and p["my_side"] == ME
         and all(isinstance(c["side"], int) for c in p["cards"])),
        ("缺省 viewer = 我方（my_side）", project_state(st)["viewer"] == ME),
        ("泄漏测试通过（干净样本）", leak_check(p, ME) == []),
        ("对面视角：自己的牌变隐藏、对方的变可见",
         any(c.get("card_id") == 22 for c in project_state(st, OPP)["cards"])
         and not any(c.get("card_id") == 11 for c in project_state(st, OPP)["cards"])),
        ("对面视角样本本身也过泄漏测试", leak_check(project_state(st, OPP), OPP) == []),
        ("project_both_seats 出两份", set(project_both_seats(st)) == {"self", "other"}),
        ("my_side 读不出且没传 viewer => 抛错（不默认按 1）",
         _raises(lambda: project_state(_FakeState(st.cards, my_side=None)))),
    ]
    for name, ok in chk:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1
    # ★ 反例：把一条"对方手牌带身份"的样本塞进去，**必须报错**（否则投影只是文档上写着）
    dirty = json.loads(json.dumps(p))
    dirty["cards"].append({"side": OPP, "location": "hand", "card_id": 99, "name": "LEAK"})
    caught = bool(leak_check(dirty, ME))
    print("  [%s] 反例：塞入对方手牌身份必须被抓（能失败）" % ("PASS" if caught else "FAIL"))
    fails += 0 if caught else 1
    dirty2 = json.loads(json.dumps(p))
    dirty2["deck_roster"] = {str(OPP): ["X"]}          # 经 JSON 往返后键是字符串，也要抓得到
    caught2 = bool(leak_check(dirty2, ME))
    print("  [%s] 反例：对方卡组名单必须被抓" % ("PASS" if caught2 else "FAIL"))
    fails += 0 if caught2 else 1
    print("schema selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# 样本 schema（纯文档 + 一个构造 helper，不做校验框架，先跑起来再说）
# ---------------------------------------------------------------------------
def make_sample(*, game, patch, seat, turn, t, phase, state, candidates,
                 verdict, label, label_src, label_available, raw,
                 receipt, events, state_after_hash=None) -> dict:
    """按 KARDS-NN.md §1.3 的字段拼一条样本。纯字典拼装，字段名和顺序
    以规格为准，改字段名要两边一起改（`player/record.py` 也读这份定义）。
    """
    s = {
        "game": game, "patch": patch, "seat": seat, "turn": turn, "t": t,
        "phase": phase, "state": state, "state_hash": state_hash(state),
        "candidates": candidates, "verdict": verdict,
        "label": label, "label_src": label_src, "label_available": label_available,
        "raw": raw, "receipt": receipt, "events": events,
    }
    if state_after_hash is not None:
        s["state_after_hash"] = state_after_hash
    return s


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
    from _learn_cards import mk_card
    sys.exit(1 if selftest(mk_card) else 0)
