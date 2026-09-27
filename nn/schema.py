#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.schema —— 样本 schema + **可见性投影**（录制与训练的唯一契约）。

见 `reverse-data/reports/spec/KARDS-NN.md` §1.3（样本 schema）/ §2（可见性）。
投影规则只在这一处实现，`agent/record.py` 写样本时调用它，别在别处再抄一份。

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

LOCAL, ENEMY = "local", "enemy"

PHASES = ("mulligan", "main", "pick", "forecast", "hand_target",
          "deploy_target", "deck_pick")


# ---------------------------------------------------------------------------
# 可见性投影
# ---------------------------------------------------------------------------
def _card_public_view(c, viewer: str, full_hand_of_viewer: bool) -> Optional[dict]:
    """一张卡该给查看者（`viewer`）看到什么。返回 `None` 表示"这张卡整体不给"
    （目前没有需要整条隐藏的情形，占位用）。
    """
    is_mine = c.side == viewer
    loc = c.location
    raw = getattr(c, "raw", None) or {}

    # P4（2026-09-26 更正为**只对场上单位**）：未揭示的隐蔽单位不给身份。
    #   ★ 用户原话："isRevealed 只对场上单位有意义：不可被效果指向，攻防数值等均看不到。"
    #   ⇒ 手牌**不**受 `is_revealed` 管（`RevealCard` 写的是 `isRevealed=1` + `hasCovert=0`，
    #     语义就是把场上隐蔽单位翻正）。
    hidden_identity = (loc in ("frontline", "back")
                       and ("has_covert" in (c.keywords or [])) and not c.is_revealed)

    # P1/P7：对方手牌只给"这里有一张"，不给内容——**但 intel 揭示过的例外**。
    #   ★ 2026-09-26 实机坐实：`UBaseCardObject::cardSeen // 0x0260` 是"这张（敌方手牌）
    #     已被我方看过"的持久标记（`SetCardsSeenByCipher` 只挑 `!cardSeen` 的再洗牌取 N 张
    #     ⇒ 天然不重复命中）。实测 intel 1 = 敌方 4 张手牌里恰好 1 张 `card_seen=1`。
    #   ⚠ 该标记**只在它还在手牌里时有效**（打出/离开手牌后同一 uid 上会变回 0）
    #     ⇒ 要"记牌"必须在看到的那一刻由我们自己落进动作流 token（`KARDS-NN.md` §2.2）。
    if loc == "hand" and not is_mine:
        if raw.get("card_seen") == 1:
            return {
                "side": c.side, "location": "hand", "uid": None, "card_id": c.card_id,
                "name": c.name, "attack": None, "defense": None,
                "kredit_cost": c.kredit_cost, "slot": None,
                "is_revealed": None, "hidden": False, "reason": "P4b_intel_seen",
            }
        return {"side": c.side, "location": "hand", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P1_enemy_hand"}

    # P2/P3：双方牌库剩余——不给身份/顺序；但己方"卡组名单"走另一条口（见下）
    if loc == "deck":
        return {"side": c.side, "location": "deck", "uid": None, "card_id": None,
                "name": None, "fname": None, "hidden": True, "reason": "P2_deck_remaining"}

    if hidden_identity:
        return {"side": c.side, "location": loc, "uid": None, "card_id": None,
                "name": None, "attack": None, "defense": None,
                "hidden": True, "reason": "P4_covert"}

    # 其余：可见（场上/总部/弃牌/己方手牌）——P8/P9/P10
    return {
        "side": c.side, "location": loc, "uid": c.uid, "card_id": c.card_id,
        "name": c.name, "attack": c.attack, "defense": c.defense,
        "kredit_cost": c.kredit_cost, "slot": c.slot,
        "is_revealed": c.is_revealed,
        "hidden": False,
    }


def project_state(st, viewer: str, deck_roster: Optional[dict] = None) -> dict:
    """把一份 `BoardState` 投影成"`viewer` 这一侧真人玩家看得到的东西"。

    `deck_roster`：`{"local": [fname, ...], "enemy": [...]}` —— 己方卡组名单
    （多重集合，不含顺序）。这不是从 `st` 里推出来的（§2.2：牌库剩余身份不给，
    但开局卡组名单不算隐藏信息），要由调用方在录制**开局**时另外提供一次
    （比如从换牌前的完整起始牌库读一次，只做这一次，别每个决策点都重读）。
    """
    cards = []
    for c in st.cards:
        v = _card_public_view(c, viewer, full_hand_of_viewer=True)
        if v is not None:
            cards.append(v)

    hand_counts = {side: len(st.hand(side)) for side in (LOCAL, ENEMY)}
    deck_counts = {}
    for side in (LOCAL, ENEMY):
        deck_counts[side] = sum(1 for c in st.cards if c.side == side and c.location == "deck")

    out = {
        "viewer": viewer,
        "turn": st.turn,
        "hand_count": hand_counts,     # P1/P7：对方只给数量；己方数量也顺带给，卡本身在 cards 里已有内容
        "deck_count": deck_counts,     # P2/P3：双方都只给数量（顺序/身份不给）
        "kredits": dict(getattr(st, "kredits", None) or {}),
        "cards": cards,
    }
    if deck_roster is not None:
        out["deck_roster"] = {LOCAL: sorted(deck_roster.get(LOCAL) or [])}
        # ★ 敌方卡组名单不给（§3.4："敌方卡组：不给名单，只给已见"）——
        #   即使调用方传了 enemy 的名单，这里也不透出去，双重保险。
    return out


def state_hash(projected: dict) -> str:
    """同局面去重用。只依赖已投影后的字典，不依赖字典 key 顺序。"""
    blob = json.dumps(projected, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 对面视角（KARDS-NN.md §2.3）
# ---------------------------------------------------------------------------
def other_side(viewer: str) -> str:
    return ENEMY if viewer == LOCAL else LOCAL


def project_both_seats(st, viewer_self: str = LOCAL,
                       deck_roster: Optional[dict] = None) -> dict:
    """一局、两个座位各投影一次（`--peek` 全量另存的用途）。

    ★ 2026-09-26 用户定调："全量另存 `--peek` 可以用于补充训练集。**对面视角**。"
    ⇒ 同一份状态按 `seat` 投影两次，样本 ×2；标签也是现成的（对手当时真做的那个动作）。
    对面视角的样本**不许比那个座位当时能看到的更多**（拿不准就标 unknown/false）。
    """
    return {
        "self": project_state(st, viewer_self, deck_roster=deck_roster),
        "other": project_state(st, other_side(viewer_self), deck_roster=deck_roster),
    }


# ---------------------------------------------------------------------------
# 泄漏测试（KARDS-NN.md §2.2 P0 验收；"能失败"的测试）
# ---------------------------------------------------------------------------
def leak_check(projected: dict, viewer: str) -> list:
    """返回**违规项**列表（空 = 通过）。故意做成"能失败"，见 `selftest()` 的反例。

    四条断言（§2.2）：
      ① 敌方手牌身份 0 条 —— 例外：`reason == "P4b_intel_seen"`（`cardSeen==1`）；
      ② 敌方牌库内容/顺序 0 条；
      ③ 敌方卡组名单 0 条（`deck_roster` 里不许出现 enemy）；
      ④ 未揭露的隐蔽**场上**牌身份/攻防 0 条。
    """
    bad = []
    if projected.get("viewer") != viewer:
        bad.append("viewer 字段被改动：%r != %r" % (projected.get("viewer"), viewer))
    for c in projected.get("cards") or []:
        side, loc = c.get("side"), c.get("location")
        reason = c.get("reason")
        if side != viewer and loc == "hand":
            if reason != "P4b_intel_seen" and (c.get("card_id") or c.get("name")):
                bad.append("① 敌方手牌身份泄漏：card_id=%r name=%r" % (c.get("card_id"), c.get("name")))
        if side != viewer and loc == "deck":
            if c.get("card_id") or c.get("name") or c.get("uid"):
                bad.append("② 敌方牌库内容泄漏：%r" % (c,))
        if reason == "P4_covert" and (c.get("card_id") or c.get("attack") is not None
                                     or c.get("defense") is not None):
            bad.append("④ 未揭露隐蔽单位泄漏：%r" % (c,))
    roster = projected.get("deck_roster") or {}
    if ENEMY in roster and roster.get(ENEMY):
        bad.append("③ 敌方卡组名单泄漏：%r" % (roster.get(ENEMY),))
    return bad


# ---------------------------------------------------------------------------
# 自检（离线，不需要游戏）
# ---------------------------------------------------------------------------
class _FakeCard:
    def __init__(self, side, location, card_id=None, name=None, covert=False,
                 is_revealed=False, card_seen=0):
        self.side, self.location = side, location
        self.uid = "0x%X" % (0x1000 + (card_id or 0))
        self.card_id, self.name = card_id, name
        self.attack, self.defense = 3 if name else None, 4 if name else None
        self.kredit_cost, self.slot = 2, 1
        self.is_revealed = is_revealed
        self.keywords = ["has_covert"] if covert else []
        self.raw = {"card_seen": card_seen, "ptr": 0x1000 + (card_id or 0)}


class _FakeState:
    def __init__(self, cards):
        self.cards = cards
        self.turn, self.kredits = 7, {"local": 5, "enemy": 3}

    def hand(self, side):
        return [c for c in self.cards if c.side == side and c.location == "hand"]


def selftest() -> int:
    """两组会导致**不同结论**的输入（CLAUDE.md 弯路 #11 的纪律），外加一个必须失败的反例。"""
    st = _FakeState([
        _FakeCard(LOCAL, "hand", 11, "MY CARD"),
        _FakeCard(ENEMY, "hand", 22, "HIDDEN ONE", card_seen=0),
        _FakeCard(ENEMY, "hand", 23, "SEEN ONE", card_seen=1),
        _FakeCard(ENEMY, "back", 33, "COVERT GUY", covert=True, is_revealed=False),
        _FakeCard(ENEMY, "back", 34, "OPEN GUY", covert=True, is_revealed=True),
        _FakeCard(ENEMY, "deck", 44, "DECK CARD"),
    ])
    fails = 0
    p = project_state(st, LOCAL, deck_roster={"local": ["A", "B"], "enemy": ["X", "Y"]})
    by = {(c.get("card_id"), c.get("reason")): c for c in p["cards"]}
    chk = [
        ("intel 看过的敌手牌给身份", any(c.get("card_id") == 23 for c in p["cards"])),
        ("没看过的敌手牌不给身份", not any(c.get("card_id") == 22 for c in p["cards"])),
        ("未揭露隐蔽单位不给身份/攻防",
         any(c.get("reason") == "P4_covert" and not c.get("card_id") and c.get("attack") is None
             for c in p["cards"])),
        ("已揭露隐蔽单位给身份", any(c.get("card_id") == 34 for c in p["cards"])),
        ("敌牌库不给身份", not any(c.get("card_id") == 44 for c in p["cards"])),
        ("己方手牌给身份", any(c.get("card_id") == 11 for c in p["cards"])),
        ("敌方卡组名单不透出", ENEMY not in (p.get("deck_roster") or {})),
        ("泄漏测试通过（干净样本）", leak_check(p, LOCAL) == []),
        ("对面视角：自己的牌变隐藏、对方的变可见",
         any(c.get("card_id") == 22 for c in project_state(st, ENEMY)["cards"])
         and not any(c.get("card_id") == 11 for c in project_state(st, ENEMY)["cards"])),
        ("对面视角样本本身也过泄漏测试", leak_check(project_state(st, ENEMY), ENEMY) == []),
        ("project_both_seats 出两份", set(project_both_seats(st)) == {"self", "other"}),
    ]
    for name, ok in chk:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1
    # ★ 反例：把一条"敌方手牌带身份"的样本塞进去，**必须报错**（否则投影只是文档上写着）
    dirty = json.loads(json.dumps(p))
    dirty["cards"].append({"side": ENEMY, "location": "hand", "card_id": 99, "name": "LEAK"})
    caught = bool(leak_check(dirty, LOCAL))
    print("  [%s] 反例：塞入敌方手牌身份必须被抓（能失败）" % ("PASS" if caught else "FAIL"))
    fails += 0 if caught else 1
    dirty2 = json.loads(json.dumps(p))
    dirty2["deck_roster"] = {ENEMY: ["X"]}
    caught2 = bool(leak_check(dirty2, LOCAL))
    print("  [%s] 反例：敌方卡组名单必须被抓" % ("PASS" if caught2 else "FAIL"))
    fails += 0 if caught2 else 1
    print("schema selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


# ---------------------------------------------------------------------------
# 样本 schema（纯文档 + 一个构造 helper，不做校验框架，先跑起来再说）
# ---------------------------------------------------------------------------
def make_sample(*, game, patch, seat, turn, t, phase, state, candidates,
                 verdict, label, label_src, label_available, raw,
                 receipt, events, state_after_hash=None) -> dict:
    """按 KARDS-NN.md §1.3 的字段拼一条样本。纯字典拼装，字段名和顺序
    以规格为准，改字段名要两边一起改（`agent/record.py` 也读这份定义）。
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
    import sys
    sys.exit(1 if selftest() else 0)
