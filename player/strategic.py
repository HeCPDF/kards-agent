#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""player.strategic —— `StrategicRule`：在线规则策略的基类（`RuleV2` 继承它，复用换牌与纯函数）。

每个候选的合法性都问游戏自己（`sess.can_play` / `can_attack` / `can_move`），规则只排序。
返回 **dict**（不是 `player.loop.Action`），由 `player.loop` 统一转成动作类型。
"""
from __future__ import annotations


class StrategicRule:
    """**基线，不是策略主体**（KARDS-NN.md §6 的 greedy 那一档的在线强化版）。

    为什么放这里而不是 `agent/nn.py`：它是**启发式**，不是神经网络。`nn/` 里
    只放网络本身和"网络必须打赢的对照组"；`agent/nn.py`（前端③）只放网络策略
    和回路。要在线上跑这条规则，显式 `--policy rule`，它会被当作外部基线导入。

    跟 `GreedyHeuristic` 的区别：它是在线版——每个候选的合法性都问游戏自己
    （`sess.can_play` / `can_attack` / `can_move`），不看结构化候选。规则只排序：
    致命一击 → 有利交换 → 打满费用 → 上线 → 结束。

    ⚠ 返回的是 **dict**（不是 `player.loop.Action`）——`nn/` 不许反向依赖 `agent/`；
    由 `agent/nn.py` 统一转成它的动作类型。
    """

    name = "rule-baseline"
    max_mulligan_discard = 3     # 起手 5 张最多扔 3 张（至少留 2）
    min_keep = 2

    def __init__(self, sess, table=None):
        self.sess = sess
        self.table = table

    @staticmethod
    def _val(c) -> float:
        return float(getattr(c, "attack", 0) or 0) + float(getattr(c, "defense", 0) or 0)

    @classmethod
    def unit_score(cls, c) -> float:
        return (3.0 * float(getattr(c, "attack", 0) or 0)
                + 2.0 * float(getattr(c, "defense", 0) or 0)
                - 1.5 * float(getattr(c, "kredit_cost", 0) or 0))

    @staticmethod
    def needs_target(card, table=None):
        """从静态卡表读"这张牌需不需要指向"（查不到返回 None=不知道）。"""
        if table is None:
            return None
        fn = table.resolve(title=getattr(card, "name", None))
        if not fn:
            return None
        kw = (table.by_fname.get(fn) or {}).get("keywords") or {}
        return bool(kw.get("selectTargetOnPlayedFromHand"))

    @staticmethod
    def _best_target(cands):
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        return min(cands, key=lambda x: (float(x.defense or 0),
                                         -float(x.attack or 0))).obj.CardID

    def choose_mulligan(self, marks: list) -> dict:
        """**跳费/中速卡组的换牌口径**（用户 2026-09-29 定调："换牌全留是败笔，
        这套是跳费卡组"）：

            留：便宜牌（≤2 费）与跳费件（静态表 `isKreditsBuff`，如 THE WAR MACHINE
                "gain 1 extra kredit slot"、PATRON、IRON FROM THE NORTH）；
            扔：贵牌（费用越高越先扔）；最多扔 3 张、至少留 2 张。

        这是**基线口径**，不是网络输出 —— NN 的 mulligan 头目前只有 8 条标签且
        全是"全留"，等于没训（`agent/nn.py --mulligan` 默认走这条规则）。
        """
        rows = []
        for m in marks:
            cost, ramp = 9, False
            if self.table is not None:
                fn = self.table.resolve(title=m.get("name"))
                row = (self.table.by_fname.get(fn) or {}) if fn else {}
                if row.get("kredits") is not None:
                    cost = int(row["kredits"])
                ramp = bool((row.get("keywords") or {}).get("isKreditsBuff"))
            score = float(cost) - (3.0 if ramp else 0.0) - (2.0 if cost <= 2 else 0.0)
            rows.append((score, m))
        rows.sort(key=lambda x: -x[0])
        n = min(self.max_mulligan_discard, max(0, len(marks) - self.min_keep))
        pick = [m["slot"] for s, m in rows[:n] if s > 0]
        return {"kind": "mulligan", "marks": tuple(pick),
                "note": "规则：留便宜/跳费，扔贵 %s" % (pick or "（全留）")}

    def choose_main(self, st) -> dict:
        # 我方 = st.my_side（读不出 => st.seat() 抛 ValueError，不默认按 1 号座位）；对方 = st.other_side
        mine, theirs = st.seat(), st.other_side
        me = [c for c in st.cards if c.side == mine and c.obj.IsFieldUnit()]
        foes = [c for c in st.cards if c.side == theirs and c.obj.IsFieldUnit()]
        ehq = [c for c in st.cards if c.side == theirs and c.obj.IsHQ()]
        hand = list(st.hand(mine))
        kred = int((st.kredits or {}).get(mine) or 0)

        if ehq:                                   # ① 致命一击
            hq_def = float(ehq[0].defense or 0)
            dmg, hitters = 0.0, []
            for a in me:
                if self.sess.can_attack(a, ehq[0]).get("can"):
                    dmg += float(a.attack or 0)
                    hitters.append(a)
            if hitters and dmg >= hq_def:
                return {"kind": "attack", "card": hitters[0].obj.CardID,
                        "target": ehq[0].obj.CardID, "score": 100.0,
                        "note": "规则：致命 %d≥%d" % (dmg, hq_def)}

        best, best_s = None, -1e9                 # ② 有利交换
        for a in me:
            for t in foes:
                if not self.sess.can_attack(a, t).get("can"):
                    continue
                kills = float(a.attack or 0) >= float(t.defense or 0)
                dies = float(t.attack or 0) >= float(a.defense or 0)
                s = (2.0 if kills else 0.0) + (-1.5 if dies else 1.0) + 0.1 * self._val(t)
                if s > best_s:
                    best, best_s = (a, t), s
        if best is not None and best_s > 0:
            a, t = best
            return {"kind": "attack", "card": a.obj.CardID, "target": t.obj.CardID,
                    "score": best_s, "note": "规则：交换 %s→%s" % (a.name, t.name)}

        affordable = [c for c in hand if (c.obj.getTotalKredits() or 0) <= kred]   # ③ 出牌
        affordable.sort(key=lambda c: self.unit_score(c) / max(1, c.obj.getTotalKredits() or 1),
                        reverse=True)
        for c in affordable:
            if self.sess.can_play(c).get("can") is False:
                continue
            is_unit = bool(c.attack) or bool(c.defense)
            wants = self.needs_target(c, self.table)
            if is_unit and wants:
                tg = self._best_target(foes + ehq)
                if tg is not None:
                    return {"kind": "play_unit_target", "card": c.obj.CardID,
                            "target": tg, "note": "规则：部署指向 %s" % c.name}
            if is_unit and not wants:
                return {"kind": "play_unit", "card": c.obj.CardID,
                        "note": "规则：部署 %s" % c.name}
            if not is_unit and wants:
                tg = self._best_target(foes + ehq)
                if tg is not None:
                    return {"kind": "play_event_target", "card": c.obj.CardID,
                            "target": tg, "note": "规则：指向指令 %s" % c.name}
            if not is_unit:
                return {"kind": "play_event", "card": c.obj.CardID,
                        "note": "规则：指令 %s" % c.name}

        for u in me:                              # ④ 上线
            if u.obj.InSupportLine() and self.sess.can_move(u).get("can"):
                return {"kind": "move_up", "card": u.obj.CardID,
                        "note": "规则：上线 %s" % u.name}

        return {"kind": "end", "note": "规则：没有更好的动作"}   # ⑤ 结束

    def choose_pick(self, cands: list) -> dict:
        best, best_s = 0, -1e9
        for i, c in enumerate(cands):
            s = float(c.get("attack") or 0) + float(c.get("defense") or 0)
            s -= 0.1 * float(c.get("kredit_cost") or 0)
            if s > best_s:
                best, best_s = i, s
        row = cands[best] if cands else {}
        return {"kind": "pick", "index": row.get("index"),
                "meta": {"kind": row.get("kind"), "trigger_id": row.get("trigger_id")},
                "note": "规则：候选 %d 个" % len(cands)}

    def choose_hand_target(self, st, pending=None, exclude_cards=None):
        legal = [c for c in st.hand(st.seat())
                 if c.obj.CardID not in (exclude_cards or ())
                 and self.sess.hand_target_legal(c).get("can") is not False]
        if not legal:
            return None
        c = max(legal, key=self._val)
        return {"kind": "hand_target", "card": c.obj.CardID,
                "note": "规则：手牌目标 %s" % c.name}

    def choose_board_target(self, st, instigator: int):
        theirs = st.other_side
        if theirs is None:
            raise ValueError("本地座位 mySide 读不出：无法确定对方")
        cands = [c for c in st.cards if c.side == theirs and c.obj.IsFieldUnit()]
        cands += [c for c in st.cards if c.side == theirs and c.obj.IsHQ()]
        tg = self._best_target(cands)
        if tg is None:
            return None
        return {"kind": "board_target", "card": instigator, "target": tg,
                "note": "规则：待点目标 → %s" % tg}

    def decide(self, st, phase: str, pend=None, exclude_cards=None):
        # `exclude_cards` 是"这一步别再用这些牌"（被游戏拒过）—— 规则基线不需要，
        # 签名跟 NNPolicy 保持一致，供 Loop 统一调用。
        if phase == "main":
            return self.choose_main(st)
        if phase == "pick":
            rows = (pend or {}).get("choose_one") or []
            return self.choose_pick(rows) if rows else None
        if phase == "hand_target":
            return self.choose_hand_target(st, pend, exclude_cards=exclude_cards)
        if phase == "board_target":
            bt = (pend or {}).get("board_target")
            inst = bt if isinstance(bt, int) else None
            return self.choose_board_target(st, inst) if inst else None
        if phase == "mulligan":
            marks = self.sess.mulligan_marks()
            return self.choose_mulligan(marks) if marks else None
        return None


# ---------------------------------------------------------------------------
