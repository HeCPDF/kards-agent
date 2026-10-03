#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.baselines —— 三条基线（KARDS-NN.md §6：NN 必须赢，否则没有价值）。

    1. always-end       每回合直接结束；非 main 相位退化成"永远选第一个候选"
    2. random-legal     结构化合法候选里均匀随机（判据 unknown 的一起进池）
    3. greedy-heuristic 简单贪心：能攻击就打、打最软的；否则把费用打满；
                        再不行上线；都不行就结束

三条都输出同一格式的预测，交给 `eval.score_predictions` 用同一套指标打分——
不允许基线用一套算法、模型用另一套（那就没法比了）。

自检：
    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.baselines
"""
from __future__ import annotations

import random

import numpy as np

from .encode import PHASE_NAMES, TYPE_NAMES, EncodedSample

Pred = dict


def _pred(type_=None, subj=-1, targ=-1, opt=-1, mull=None) -> Pred:
    return {"type": type_, "subj": subj, "targ": targ, "opt": opt, "mull": mull}


def _cost(s: EncodedSample, ent_idx: int) -> float:
    return float(s.ent_num[ent_idx, 2]) * 8.0


def _atk(s: EncodedSample, ent_idx: int) -> float:
    return float(s.ent_num[ent_idx, 0]) * 10.0


def _dfn(s: EncodedSample, ent_idx: int) -> float:
    return float(s.ent_num[ent_idx, 1]) * 10.0


def _kredits(s: EncodedSample) -> float:
    gi = 1  # GLOBAL_LAYOUT 里 kredits_local 的下标
    return float(s.glob[gi]) * 12.0


# ---------------------------------------------------------------------------
class AlwaysEnd:
    name = "always-end"

    def predict(self, s: EncodedSample) -> Pred:
        if s.phase == "main":
            return _pred(type_="end")
        if s.phase in ("pick", "deck_pick") and len(s.opt):
            return _pred(type_=TYPE_NAMES[s.type_idx] if s.type_idx >= 0 else "pick",
                         opt=0)
        if s.phase in ("hand_target", "deploy_target") and len(s.targ):
            return _pred(type_="hand_target_selected" if s.phase == "hand_target"
                         else "choose_target", targ=0)
        if s.phase == "mulligan":
            return _pred(type_="mulligan", mull=np.zeros(len(s.opt), dtype=bool))
        return _pred()


class RandomLegal:
    name = "random-legal"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def predict(self, s: EncodedSample) -> Pred:
        r = self.rng
        if s.phase == "main":
            pool = []
            # ★ 约定：预测输出的是**候选位置**（下标），不是实体下标 —— 跟标签同构。
            hand = [k for k, i in enumerate(s.subj) if s.ent_zone[i] == 4]  # 4=hand
            board = [k for k, i in enumerate(s.subj) if s.ent_zone[i] in (2, 3)]
            if hand:
                pool.append("play")
            if board and len(s.targ):
                pool.append("attack")
            if board:
                pool.append("move")
            pool.append("end")
            t = r.choice(pool)
            if t == "end":
                return _pred(type_="end")
            if t == "attack":
                return _pred(type_=t, subj=r.choice(board), targ=r.randrange(len(s.targ)))
            if t == "move":
                return _pred(type_=t, subj=r.choice(board))
            return _pred(type_="play", subj=r.choice(hand))
        if s.phase in ("pick", "deck_pick") and len(s.opt):
            return _pred(type_="pick", opt=r.randrange(len(s.opt)))
        if s.phase == "hand_target" and len(s.targ):
            return _pred(type_="hand_target_selected", targ=r.randrange(len(s.targ)))
        if s.phase == "deploy_target" and len(s.targ):
            return _pred(type_="choose_target", targ=r.randrange(len(s.targ)))
        if s.phase == "mulligan":
            return _pred(type_="mulligan",
                         mull=np.array([r.random() < 0.5 for _ in range(len(s.opt))]))
        return _pred()


class GreedyHeuristic:
    name = "greedy-heuristic"

    def predict(self, s: EncodedSample) -> Pred:
        if s.phase == "main":
            board = [k for k, i in enumerate(s.subj) if s.ent_zone[i] in (2, 3)]
            hand = [k for k, i in enumerate(s.subj) if s.ent_zone[i] == 4]
            # 1) 攻击：挑攻击力最高者，打防御最低的目标
            if board and len(s.targ):
                k = max(board, key=lambda k: _atk(s, s.subj[k]))
                j = min(range(len(s.targ)), key=lambda j: _dfn(s, s.targ[j]))
                return _pred(type_="attack", subj=k, targ=j)
            # 2) 出牌：费用付得起里挑最贵的
            if hand:
                afford = [k for k in hand if _cost(s, s.subj[k]) <= _kredits(s)]
                if afford:
                    k = max(afford, key=lambda k: _cost(s, s.subj[k]))
                    return _pred(type_="play", subj=k)
            # 3) 上线：挑攻击力最高的（占位语义未定，先只给主体）
            if board:
                k = max(board, key=lambda k: _atk(s, s.subj[k]))
                return _pred(type_="move", subj=k)
            return _pred(type_="end")
        if s.phase in ("pick", "deck_pick") and len(s.opt):
            k = max(range(len(s.opt)), key=lambda k: _cost(s, s.opt[k]))
            return _pred(type_="pick", opt=k)
        if s.phase == "hand_target" and len(s.targ):
            k = max(range(len(s.targ)), key=lambda k: _dfn(s, s.targ[k]))
            return _pred(type_="hand_target_selected", targ=k)
        if s.phase == "deploy_target" and len(s.targ):
            k = min(range(len(s.targ)), key=lambda k: _dfn(s, s.targ[k]))
            return _pred(type_="choose_target", targ=k)
        if s.phase == "mulligan":
            # 无信息时"全留"是常见的人类保守策略
            return _pred(type_="mulligan", mull=np.zeros(len(s.opt), dtype=bool))
        return _pred()


def all_baselines(seed: int = 0) -> list:
    return [AlwaysEnd(), RandomLegal(seed), GreedyHeuristic()]


# ---------------------------------------------------------------------------
# 在线强基线：会问游戏自己的判据（`agent/nn.py --policy rule` 用它做 A/B 对照）
# ---------------------------------------------------------------------------
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
                                         -float(x.attack or 0))).card_id

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
        me = [c for c in st.cards if c.side == "local" and c.location in ("frontline", "back")]
        foes = [c for c in st.cards if c.side == "enemy" and c.location in ("frontline", "back")]
        ehq = [c for c in st.cards if c.side == "enemy" and c.location == "hq"]
        hand = list(st.hand("local"))
        kred = int((st.kredits or {}).get("local") or 0)

        if ehq:                                   # ① 致命一击
            hq_def = float(ehq[0].defense or 0)
            dmg, hitters = 0.0, []
            for a in me:
                if self.sess.can_attack(a, ehq[0]).get("can"):
                    dmg += float(a.attack or 0)
                    hitters.append(a)
            if hitters and dmg >= hq_def:
                return {"kind": "attack", "card": hitters[0].card_id,
                        "target": ehq[0].card_id, "score": 100.0,
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
            return {"kind": "attack", "card": a.card_id, "target": t.card_id,
                    "score": best_s, "note": "规则：交换 %s→%s" % (a.name, t.name)}

        affordable = [c for c in hand if (c.kredit_cost or 0) <= kred]   # ③ 出牌
        affordable.sort(key=lambda c: self.unit_score(c) / max(1, c.kredit_cost or 1),
                        reverse=True)
        for c in affordable:
            if self.sess.can_play(c).get("can") is False:
                continue
            is_unit = bool(c.attack) or bool(c.defense)
            wants = self.needs_target(c, self.table)
            if is_unit and wants:
                tg = self._best_target(foes + ehq)
                if tg is not None:
                    return {"kind": "play_unit_target", "card": c.card_id,
                            "target": tg, "note": "规则：部署指向 %s" % c.name}
            if is_unit and not wants:
                return {"kind": "play_unit", "card": c.card_id,
                        "note": "规则：部署 %s" % c.name}
            if not is_unit and wants:
                tg = self._best_target(foes + ehq)
                if tg is not None:
                    return {"kind": "play_event_target", "card": c.card_id,
                            "target": tg, "note": "规则：指向指令 %s" % c.name}
            if not is_unit:
                return {"kind": "play_event", "card": c.card_id,
                        "note": "规则：指令 %s" % c.name}

        for u in me:                              # ④ 上线
            if u.location == "back" and self.sess.can_move(u).get("can"):
                return {"kind": "move_up", "card": u.card_id,
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

    def choose_hand_target(self, st, pending=None):
        legal = [c for c in st.hand("local")
                 if self.sess.hand_target_legal(c).get("can") is not False]
        if not legal:
            return None
        c = max(legal, key=self._val)
        return {"kind": "hand_target", "card": c.card_id,
                "note": "规则：手牌目标 %s" % c.name}

    def choose_board_target(self, st, instigator: int):
        cands = [c for c in st.cards if c.side == "enemy"
                 and c.location in ("frontline", "back")]
        cands += [c for c in st.cards if c.side == "enemy" and c.location == "hq"]
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
            return self.choose_hand_target(st, pend)
        if phase == "board_target":
            bt = (pend or {}).get("board_target")
            inst = bt if isinstance(bt, int) else None
            return self.choose_board_target(st, inst) if inst else None
        if phase == "mulligan":
            marks = self.sess.mulligan_marks()
            return self.choose_mulligan(marks) if marks else None
        return None


# ---------------------------------------------------------------------------
class _C:
    """离线自检用的假卡（跟 board_api.Card 同字段名）。"""

    def __init__(self, side, loc, cid, name="X", atk=0, dfn=0, cost=0, slot=0):
        self.side, self.location, self.card_id, self.name = side, loc, cid, name
        self.attack, self.defense, self.kredit_cost, self.slot = atk, dfn, cost, slot
        self.uid, self.is_revealed, self.keywords, self.raw = "0x%X" % cid, False, [], {}


class _S:
    def __init__(self, cards, kredits=3, our_turn=True, turn=5, finished=False):
        self.cards = cards
        self.kredits = {"local": kredits, "enemy": 2}
        self.our_turn, self.turn, self.match_finished = our_turn, turn, finished

    def hand(self, side):
        return [c for c in self.cards if c.side == side and c.location == "hand"]


class _Sess:
    """假命令层：只回答判据。"""

    def __init__(self, attacks=None):
        self.attacks = attacks or {}

    def can_attack(self, a, t):
        return {"ok": True, "can": self.attacks.get((a.card_id, t.card_id), False)}

    def can_play(self, c):
        return {"ok": True, "can": True}

    def can_move(self, u):
        return {"ok": True, "can": True}

    def hand_target_legal(self, c):
        return {"ok": True, "can": True}

    def mulligan_marks(self):
        return []


def selftest() -> int:
    from .encode import _fake_card, _fake_state, _sample, encode_sample
    from .features import CardTable

    table = CardTable.load()
    fails = 0

    def chk(name, ok):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    st = _fake_state()
    st["cards"].append(_fake_card("local", "back", 9001, "15th ENGINEERS", 5, 2, 5, 2))
    atk = encode_sample(_sample(st, label={"type": "attack", "subject": 13003,
                                           "target": 59, "option": None}), table)
    pk = encode_sample(_sample(st, phase="pick",
                               candidates={"subject": [], "target": [],
                                           "option": ["card_event_overcast",
                                                      "card_event_naval_battle"]},
                               label={"type": "pick", "subject": None,
                                      "target": None,
                                      "option": "card_event_overcast"}), table)
    ae, rl, gr = AlwaysEnd(), RandomLegal(seed=1), GreedyHeuristic()

    chk("always-end：main 一律 end", ae.predict(atk)["type"] == "end")
    chk("always-end：pick 选第 0 个", ae.predict(pk)["opt"] == 0)

    p1 = rl.predict(atk)
    chk("random-legal：攻击时主体/目标都在候选内",
        p1["type"] != "attack"
        or (0 <= p1["subj"] < len(atk.subj) and 0 <= p1["targ"] < len(atk.targ)))
    seen = {rl.predict(pk)["opt"] for _ in range(30)}
    chk("random-legal：多次采样能出不同选项（不是常数）", len(seen) >= 2)

    g = gr.predict(atk)
    chk("greedy：攻击挑攻击力最高的主体", g["type"] == "attack"
        and _atk(atk, atk.subj[g["subj"]]) == 5.0)
    chk("greedy：攻击挑防御最低的目标（FRONTIER FORCE 2/6）",
        g["targ"] == min(range(len(atk.targ)), key=lambda j: _dfn(atk, atk.targ[j])))

    g2 = gr.predict(pk)
    chk("greedy：pick 里看不出费用时也不越界", 0 <= g2["opt"] < len(pk.opt))

    # ---- StrategicRule（在线强基线）：这里离线测它的**排序逻辑**
    st = _S([_C("local", "frontline", 10, "A", 5, 2, 3),
             _C("local", "frontline", 11, "B", 6, 4, 4),
             _C("enemy", "hq", 41, "HQ", 0, 9, 0),
             _C("enemy", "back", 50, "E", 2, 3, 2)])
    sr = StrategicRule(_Sess({(10, 41): True, (11, 41): True}))
    a = sr.choose_main(st)
    chk("rule 基线：致命一击优先（5+6≥9 打总部）",
        a["kind"] == "attack" and a["target"] == 41)

    st2 = _S([_C("local", "frontline", 10, "A", 5, 5, 3),
              _C("enemy", "hq", 41, "HQ", 0, 30, 0),
              _C("enemy", "back", 50, "SOFT", 1, 3, 1),
              _C("enemy", "back", 51, "BIG", 7, 9, 6)])
    sr2 = StrategicRule(_Sess({(10, 41): False, (10, 50): True, (10, 51): True}))
    a2 = sr2.choose_main(st2)
    chk("rule 基线：有利交换挑软的", a2["kind"] == "attack" and a2["target"] == 50)
    a6 = StrategicRule(_Sess({(10, 41): False, (10, 50): False,
                              (10, 51): False})).choose_main(st2)
    chk("rule 基线反例：判据全 False 时不许攻击", a6["kind"] != "attack")

    st3 = _S([_C("local", "hand", 20, "CHEAP", 2, 2, 2),
              _C("local", "hand", 21, "BEST", 5, 5, 3),
              _C("local", "hand", 22, "TOO-EXPENSIVE", 9, 9, 9),
              _C("enemy", "hq", 41, "HQ", 0, 20, 0)], kredits=3)
    a3 = StrategicRule(_Sess()).choose_main(st3)
    chk("rule 基线：费用内挑价值最高的", a3["kind"] == "play_unit" and a3["card"] == 21)

    st4 = _S([_C("local", "back", 30, "U", 3, 3, 3),
              _C("enemy", "hq", 41, "HQ", 0, 20, 0)], kredits=0)
    chk("rule 基线：没牌可打就上线",
        StrategicRule(_Sess()).choose_main(st4)["kind"] == "move_up")
    st5 = _S([_C("enemy", "hq", 41, "HQ", 0, 20, 0)], kredits=0)
    chk("rule 基线：无事可做 -> 结束",
        StrategicRule(_Sess()).choose_main(st5)["kind"] == "end")

    marks = [{"slot": i, "name": n, "card_id": i + 1} for i, n in
             enumerate(("A", "B", "C", "D"))]
    am = StrategicRule(_Sess()).choose_mulligan(marks)
    chk("rule 基线：换牌最多丢 2 张",
        am["kind"] == "mulligan" and len(am["marks"]) == 2)

    print("baselines selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
