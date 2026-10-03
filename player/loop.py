#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""player.loop —— 前端③：**神经网络在线回路**（KARDS-NN.md §7 / §9-P3）。

本文件里**只有神经网络策略**（`NNPolicy`）和回路（`Loop`）。手写启发式不在这
——它们跟离线评估那三条基线一起放在 `nn/baselines.py`（网络必须打赢的对照组），
要用得显式 `--policy rule`。

`NNPolicy` 的动作**完全来自网络**：

    type 头     选动作类型（play / attack / move / end，按 phase 掩码）
    subject 头  选主体（play=手牌，attack/move=我方场上单位）
    target 头   选目标（敌方场上单位 + 总部；hand_target=我方手牌）
    option 头   pick / 牌库选牌
    mulligan 头 起手逐张 keep/discard

主回合四选一是**一起比**的：`type_logit[type] + 指针分` 取最大（§4.2 的在线侧），
没有任何"致命一击优先"之类的手写优先级。默认 `strict`：编码/推理失败就停手，
不偷偷换规则——这样"动作确实是网络给的"可验证；要降级得显式 `--fallback-rule`。

其它纪律（跟项目一致）：
    * **默认 dry-run**，`--live` 才真发动作；只在训练局跑，排位/休闲要用户同意；
    * 判据只挑不判（这里只用来**排序**，真动作照样发给游戏自己判）；
    * 每步核对回执，被拒就打印游戏自己的提示并停手；
    * 决策全落盘 `kards-data/nn/logs/`（配 `python -m player.record` 就是 bootstrap 数据）。

用法：
    python -m player.loop --selftest
    python -m player.loop --ckpt D:\\Kards\\kards-data\\nn\\runs\\x\\ckpt.pt            # dry-run
    python -m player.loop --ckpt ... --live --max-actions 60
    python -m player.loop --policy rule --ckpt ...        # 对照组（启发式基线）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional


from agent import precheck
from agent import session as _session
from learn.baselines import StrategicRule


def _settle(seconds: float, frames: int = 6) -> None:
    """按帧等：走注入侧 `Injector.settle(秒, 帧)` —— 失焦降帧时"睡 0.x 秒"里游戏可能
    一帧都没跑，界面就不会反应（2026-10-01 换牌/攻击被吞的根因）。
    拿不到注入侧（不健康/没装调度）就退回原来的纯墙钟等待，行为不变。"""
    try:
        r = precheck.call_write("settle", seconds, frames)
        if isinstance(r, dict) and r.get("ok") is False:
            raise RuntimeError(r.get("stopped") or "settle 被拒")
        return
    except Exception:                                         # noqa: BLE001
        time.sleep(seconds)

LOCAL, ENEMY = "local", "enemy"
BOARD = ("frontline", "back")
from base import paths as _paths_nn
LOG_DIR = _paths_nn.NN_LOG_DIR


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
@dataclass
class Action:
    kind: str                      # play_unit|play_event|play_*_target|attack|move_up|end|pick|hand_target|board_target|mulligan
    card: Optional[int] = None     # card_id
    target: Optional[int] = None   # card_id
    index: Optional[int] = None    # pick 候选序号（游戏 UI 的 index）
    marks: tuple = ()              # mulligan：要丢的 slot
    note: str = ""
    score: float = 0.0
    meta: dict = field(default_factory=dict)
    path: tuple = ()               # 选择路径（行动 = 动作 + 其后全部选择）：搜索时定好，执行侧弹出提示时照点

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "card": self.card, "target": self.target,
             "index": self.index, "marks": list(self.marks),
             "note": self.note, "score": round(self.score, 3), "meta": self.meta}
        if self.path:
            d["path"] = list(self.path)
        return d


def as_action(x) -> Optional[Action]:
    """`nn/baselines.py` 的基线返回 dict；统一转成 Action。"""
    if x is None or isinstance(x, Action):
        return x
    if isinstance(x, dict):
        d = dict(x)
        meta = d.pop("meta", None) or {}
        d.setdefault("marks", tuple(d.get("marks") or ()))
        d["path"] = tuple(d.get("path") or ())
        return Action(meta=meta, **d)
    raise TypeError("认不出的动作类型：%r" % (x,))


@dataclass
class Step:
    t: float
    phase: str
    turn: Optional[int]
    kredits: Optional[int]
    action: Optional[dict]
    result: Optional[dict] = None
    executed: bool = False
    note: str = ""
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 神经网络策略
# ---------------------------------------------------------------------------
class NNPolicy(StrategicRule):
    """吃 `nn/train_bc.py` 的 checkpoint，三个头直接出动作。

    继承 `StrategicRule` 只复用它的两个**纯函数**（`needs_target` / `_best_target`），
    决策逻辑全部重写在下面对应的 `choose_*` / `decide_*` 里。
    """

    name = "nn"
    MAIN_TYPES = ("play", "attack", "move", "end")

    def __init__(self, sess, table, ckpt_path=None, device=None, strict: bool = True,
                 roster: Optional[dict] = None, mulligan_rule: bool = True):
        super().__init__(sess, table=table)
        import torch
        from learn.encode import TYPE_ID
        from learn.model import KardsNet, ModelConfig
        self.torch = torch
        self.TYPE_ID = TYPE_ID
        ckpt = None
        if ckpt_path:
            ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
            cfg = ModelConfig(**{k: v for k, v in ckpt["config"].items()
                                 if k in ModelConfig.__dataclass_fields__})
        else:
            # 无 checkpoint = 随机初始化：只用于自检/对照，不是可用策略
            cfg = ModelConfig(vocab_size=len(getattr(table, "vocab", None) or {}) or 2048)
        self.model = KardsNet(cfg)
        if ckpt:
            self.model.load_state_dict(ckpt["state_dict"])
        self.model.eval()
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.strict = strict
        self.roster = roster
        self.ckpt_path = ckpt_path
        # ★ 2026-09-29 用户定调："换牌全留是败笔，这套是跳费卡组"。
        #   mulligan 头只有 8 条标签、全是"全留" ⇒ 没训出来，不能拿它当真策略；
        #   默认走 `StrategicRule.choose_mulligan`（留便宜/跳费、扔贵），
        #   要对比网络头就 `--mulligan nn`。
        self.mulligan_rule = bool(mulligan_rule)
        self.probe: dict = {}

    # ------------------------------------------------------------ 编码 / 前向
    def _encode(self, st, phase: str, ltype: Optional[str] = None,
                options=None, option_index=None):
        from learn import schema
        from learn.encode import encode_sample
        state = schema.project_state(st, LOCAL, self.roster)
        cands = {"subject": [], "target": [], "option": list(options or [])}
        if option_index is not None:
            cands["option_index"] = list(option_index)
        sample = {
            "game": "live", "patch": None, "seat": LOCAL,
            "turn": getattr(st, "turn", None), "t": time.time(),
            "phase": phase, "state": state, "candidates": cands,
            "verdict": {}, "raw": [], "receipt": {}, "events": [],
            "state_hash": "",
            "label": {"type": ltype, "subject": None, "target": None, "option": None},
            "label_src": "live", "label_available": False,
        }
        return encode_sample(sample, self.table, dedup_options=False)

    def _forward(self, encs: list) -> list:
        from learn.dataset import to_torch_batch
        outs = []
        with self.torch.no_grad():
            for enc in encs:
                outs.append(self.model(to_torch_batch([enc], device=self.device)))
        return outs

    def _np(self, x):
        if hasattr(x, "detach"):
            return x.detach().float().cpu().numpy()
        return x                       # 已经是 ndarray（force_play 里会二次转换）

    @staticmethod
    def _card(st, card_id):
        for c in st.cards:
            if c.card_id == card_id:
                return c
        return None

    # ------------------------------------------------------------ 主回合
    def choose_main(self, st, exclude_cards=None) -> Action:
        encs = {}
        for t in ("play", "attack", "move"):
            enc = self._encode(st, "main", ltype=t)
            if enc is not None:
                encs[t] = enc
        if not encs:
            raise RuntimeError("main 相位编码失败（0 个变体）")
        outs = dict(zip(encs.keys(), self._forward(list(encs.values()))))

        tl, scores, detail = None, {}, {}
        for t, out in outs.items():
            tl = self._np(out["type_logits"][0])
            base = float(tl[self.TYPE_ID[t]])
            enc = encs[t]
            if t == "play" and len(enc.subj):
                s = self._np(out["subject_scores"][0])
                if exclude_cards:
                    for _i, _cid in enumerate(enc.debug["subj_ids"]):
                        if _cid in exclude_cards:
                            s[_i] = -1e9
                si = int(s.argmax())
                card_id = enc.debug["subj_ids"][si]
                extra, tij = 0.0, None
                card = self._card(st, card_id)
                if card is not None and self.needs_target(card, self.table) \
                        and len(enc.targ):
                    ts = self._np(out["target_scores"][0])
                    tij, extra = int(ts.argmax()), float(ts.max())
                scores[("play", si, tij)] = base + float(s[si]) + extra
                detail["play"] = {"type_logit": round(base, 3),
                                  "subject": round(float(s[si]), 3),
                                  "target": round(extra, 3), "card_id": card_id}
            elif t == "attack" and len(enc.subj) and len(enc.targ):
                s, ts = self._np(out["subject_scores"][0]), self._np(out["target_scores"][0])
                if exclude_cards:
                    for _i, _cid in enumerate(enc.debug["subj_ids"]):
                        if _cid in exclude_cards:
                            s[_i] = -1e9
                si, tij = int(s.argmax()), int(ts.argmax())
                scores[("attack", si, tij)] = base + float(s[si]) + float(ts[tij])
                detail["attack"] = {"type_logit": round(base, 3),
                                    "subject": round(float(s[si]), 3),
                                    "target": round(float(ts[tij]), 3),
                                    "card_id": enc.debug["subj_ids"][si],
                                    "target_id": enc.debug["targ_ids"][tij]}
            elif t == "move" and len(enc.subj):
                s = self._np(out["subject_scores"][0])
                if exclude_cards:
                    for _i, _cid in enumerate(enc.debug["subj_ids"]):
                        if _cid in exclude_cards:
                            s[_i] = -1e9
                si = int(s.argmax())
                scores[("move", si, None)] = base + float(s[si])
                detail["move"] = {"type_logit": round(base, 3),
                                  "subject": round(float(s[si]), 3),
                                  "card_id": enc.debug["subj_ids"][si]}
        if tl is not None:
            scores[("end", None, None)] = float(tl[self.TYPE_ID["end"]])
            detail["end"] = {"type_logit": round(float(tl[self.TYPE_ID["end"]]), 3)}
        if not scores:
            raise RuntimeError("main 相位一个候选都没编出来")

        best = max(scores, key=scores.get)
        self.probe = {"scores": {"|".join(str(x) for x in k): round(v, 3)
                                 for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
                      "detail": detail}
        kind, enc = best[0], encs.get(best[0])
        if kind == "end":
            return Action("end", note="nn:end", score=scores[best])
        card_id = enc.debug["subj_ids"][best[1]]
        if kind == "move":
            return Action("move_up", card=card_id, score=scores[best], note="nn:move")
        if kind == "attack":
            return Action("attack", card=card_id,
                          target=enc.debug["targ_ids"][best[2]],
                          score=scores[best], note="nn:attack")
        card = self._card(st, card_id)
        is_unit = bool(getattr(card, "attack", 0)) or bool(getattr(card, "defense", 0))
        target = enc.debug["targ_ids"][best[2]] if best[2] is not None else None
        if is_unit and target is not None:
            return Action("play_unit_target", card=card_id, target=target,
                          score=scores[best], note="nn:play(指向单位)")
        if is_unit:
            return Action("play_unit", card=card_id, score=scores[best], note="nn:play")
        if target is not None:
            return Action("play_event_target", card=card_id, target=target,
                          score=scores[best], note="nn:play(指向指令)")
        return Action("play_event", card=card_id, score=scores[best], note="nn:play")

    def force_play(self, st, exclude_cards=None) -> Optional[Action]:
        """只在这个 phase 的**出牌**分支里取最优（给"不许空过"的守卫用）。

        `Loop` 在"网络选了 end，但手里确实有费用够、游戏也说能出的牌"时会调它
        —— 类型被守卫改写，但**出哪张仍然是网络给的**（指针头 argmax）。

        ★ 2026-09-29 用户："我没见过它打出过牌。" ⇒ 这里改成**沿分数降序找第一张
          游戏自己也说能出的牌**（`can_play`；判据说不知道的也放行，让游戏在真实
          上下文里再判一次）。这样既不会因为指针头首选被游戏否掉就整回合空过，
          也不会拿手写优先级替网络挑牌。
        """
        enc = self._encode(st, "main", ltype="play")
        if enc is None or not len(enc.subj):
            return None
        out = self._forward([enc])[0]
        s = self._np(out["subject_scores"][0])
        if exclude_cards:
            for i, cid in enumerate(enc.debug["subj_ids"]):
                if cid in exclude_cards:
                    s[i] = -1e9
        card_id, card, si = None, None, -1
        for cand in self._np(s).argsort()[::-1]:
            cid = enc.debug["subj_ids"][int(cand)]
            if cid is None:
                continue
            c = self._card(st, cid)
            if c is None:
                continue
            try:
                if self.sess.can_play(c).get("can") is False:
                    continue
            except Exception:                                     # noqa: BLE001
                pass
            card_id, card, si = cid, c, int(cand)
            break
        if card is None:
            return None
        needs = self.needs_target(card, self.table) if card is not None else None
        target = None
        if needs and len(enc.targ):
            tij = int(self._np(out["target_scores"][0]).argmax())
            target = enc.debug["targ_ids"][tij]
        is_unit = bool(getattr(card, "attack", 0)) or bool(getattr(card, "defense", 0))
        if is_unit and target is not None:
            return Action("play_unit_target", card=card_id, target=target,
                          note="guard:出牌（网络选）")
        if is_unit:
            return Action("play_unit", card=card_id, note="guard:出牌（网络选）")
        if target is not None:
            return Action("play_event_target", card=card_id, target=target,
                          note="guard:出牌（网络选）")
        return Action("play_event", card=card_id, note="guard:出牌（网络选）")

    # ------------------------------------------------------------ pick / 选牌
    def decide_pick(self, st, rows: list) -> Optional[Action]:
        names = [r.get("label") or r.get("name") or "?" for r in rows]
        idx = [r.get("index") if r.get("index") is not None else i
               for i, r in enumerate(rows)]
        enc = self._encode(st, "pick", options=names, option_index=idx)
        if enc is None:
            raise RuntimeError("pick 编码失败")
        out = self._forward([enc])[0]
        s = self._np(out["option_scores"][0])
        k = int(s.argmax())
        row = rows[k] if k < len(rows) else rows[0]
        self.probe = {"pick": {"scores": [round(float(v), 3) for v in s.tolist()],
                               "chosen": k, "index": row.get("index"),
                               "kind": row.get("kind"),
                               "needs_target": row.get("needs_target")}}
        meta = {"kind": row.get("kind"), "trigger_id": row.get("trigger_id")}
        if row.get("needs_target"):
            tg = self._pick_target(st)
            if tg is not None:
                return Action("pick", index=row.get("index"), target=tg[0],
                              score=float(s[k]), meta=meta, note="nn:pick+指向")
        return Action("pick", index=row.get("index"), score=float(s[k]), meta=meta,
                      note="nn:pick")

    def _pick_target(self, st):
        enc = self._encode(st, "deploy_target")
        if enc is None or not len(enc.targ):
            return None
        s = self._np(self._forward([enc])[0]["target_scores"][0])
        j = int(s.argmax())
        return enc.debug["targ_ids"][j], float(s[j])

    # ------------------------------------------------------------ 选目标 / 换牌
    def decide_hand_target(self, st, pend=None, exclude_cards=None) -> Optional[Action]:
        enc = self._encode(st, "hand_target")
        if enc is None or not len(enc.targ):
            raise RuntimeError("hand_target 没有候选")
        s = self._np(self._forward([enc])[0]["target_scores"][0])
        if exclude_cards:
            for i, cid in enumerate(enc.debug["targ_ids"]):
                if cid in exclude_cards:
                    s[i] = -1e9
        j = int(s.argmax())
        if float(s[j]) < -1e8:
            raise RuntimeError("hand_target 候选全被排除")
        return Action("hand_target", card=enc.debug["targ_ids"][j],
                      score=float(s[j]), note="nn:手牌目标")

    def decide_board_target(self, st, instigator: int) -> Optional[Action]:
        enc = self._encode(st, "deploy_target")
        if enc is None or not len(enc.targ):
            raise RuntimeError("deploy_target 没有候选")
        s = self._np(self._forward([enc])[0]["target_scores"][0])
        j = int(s.argmax())
        return Action("board_target", card=instigator,
                      target=enc.debug["targ_ids"][j],
                      score=float(s[j]), note="nn:待点目标")

    def decide_mulligan(self, st, marks: list) -> Optional[Action]:
        if self.mulligan_rule:
            act = as_action(StrategicRule.choose_mulligan(self, marks))
            if act is not None:
                act.note = "mulligan:规则（NN mulligan 头未训练）— " + act.note
            return act
        names = [m.get("name") or "?" for m in marks]
        slots = [m.get("slot") for m in marks]
        enc = self._encode(st, "mulligan", options=names, option_index=slots)
        if enc is None:
            raise RuntimeError("mulligan 编码失败")
        logits = self._np(self._forward([enc])[0]["mulligan_logits"][0])[:len(marks)]
        disc = [slots[i] for i, v in enumerate(logits) if float(v) > 0.0]
        if len(disc) >= len(marks) > 1:
            disc = disc[:-1]                      # 不许把起手全丢
        self.probe = {"mulligan": {"logits": [round(float(v), 3) for v in logits.tolist()],
                                   "discard_slots": disc}}
        return Action("mulligan", marks=tuple(disc), note="nn:换牌 %s" % (disc or "全留"))

    # ------------------------------------------------------------ 统一入口
    def _rule_decide(self, st, phase: str, pend: Optional[dict]) -> Optional[Action]:
        """降级路径：**显式调用父类（StrategicRule）的实现**。

        ⚠ 不能写成 `super().decide(...)` —— 那是 `self.decide` 的父类版本，
        里面又回头调 `self.choose_main`（已被本类覆盖）⇒ 无限递归/再次失败。
        """
        if phase == "main":
            return as_action(StrategicRule.choose_main(self, st))
        if phase == "pick":
            rows = (pend or {}).get("choose_one") or []
            return as_action(StrategicRule.choose_pick(self, rows)) if rows else None
        if phase == "hand_target":
            return as_action(StrategicRule.choose_hand_target(self, st, pend))
        if phase == "board_target":
            bt = (pend or {}).get("board_target")
            inst = bt if isinstance(bt, int) else None
            return (as_action(StrategicRule.choose_board_target(self, st, inst))
                    if inst else None)
        if phase == "mulligan":
            marks = self.sess.mulligan_marks()
            return as_action(StrategicRule.choose_mulligan(self, marks)) if marks else None
        return None

    def decide(self, st, phase: str, pend: Optional[dict] = None,
               exclude_cards=None) -> Optional[Action]:
        try:
            if phase == "main":
                return self.choose_main(st, exclude_cards=exclude_cards)
            if phase == "pick":
                rows = (pend or {}).get("choose_one") or []
                return self.decide_pick(st, rows) if rows else None
            if phase == "hand_target":
                return self.decide_hand_target(st, pend, exclude_cards=exclude_cards)
            if phase == "board_target":
                bt = (pend or {}).get("board_target")
                inst = bt if isinstance(bt, int) else None
                return self.decide_board_target(st, inst) if inst else None
            if phase == "mulligan":
                marks = self.sess.mulligan_marks()
                return self.decide_mulligan(st, marks) if marks else None
            return None
        except Exception as e:                                    # noqa: BLE001
            if self.strict:
                raise
            act = self._rule_decide(st, phase, pend)
            if act is not None:
                act.note = "nn 失败(%s) → 规则：%s" % (e, act.note)
            return act


# ---------------------------------------------------------------------------
# 回路
# ---------------------------------------------------------------------------
class Loop:
    def __init__(self, sess, policy, live: bool = False, max_actions: int = 40,
                 log_path: Optional[str] = None, verbose: bool = True,
                 timing: Optional[dict] = None, max_rejects: int = 4):
        self.sess, self.policy = sess, policy
        self.live, self.max_actions, self.verbose = live, max_actions, verbose
        self.log_path = log_path
        self.steps: list = []
        self.done = False
        # ★ 节奏：**不能太规则**（用户 2026-09-28 定调）。每个动作前抽一次
        #   说话/犹豫时间：基础 U(min,max) + 概率性长考 U(long_min,long_max)；
        #   等对方回合也按随机间隔轮询。要复现就固定 --seed。
        # ★ 用户 2026-09-28 定调："不规则"= **0.1s 量级**的延迟抖动（不是几秒的
        #   思考停顿），而且**不许改 ops/inject.py** —— 所以抖动只加在回路这一层：
        #   每个动作前后各抽一次几十~几百毫秒的随机量，等对方回合也按短随机间隔轮询。
        t = dict(timing or {})
        self.think_min = float(t.get("think_min", 0.05))
        self.think_max = float(t.get("think_max", 0.35))
        self.long_prob = float(t.get("long_prob", 0.0))
        self.long_min = float(t.get("long_min", 0.5))
        self.long_max = float(t.get("long_max", 1.2))
        self.rng = random.Random(t.get("seed"))
        # ★ 2026-09-30 实机：pick 成功后面板还没换页就重发同一批候选，连拒 5 次、每次 4~12s
        #   （还有"面板弹出前就判成 main、发了 end 被拒"）。动作成功后先留一小段落定时间，
        #   让下一步读到的 pending 是动画之后的。0 = 关掉。
        self.settle = float(t.get("settle", 0.7))
        self.turn_settle = float(t.get("turn_settle", 2.0))   # 用户 2026-10-01："回合开始后可能是 kredit 还没回满，略微延迟 1s"（1.2→2.2）
        self.settle_long = float(t.get("settle_long", 2.2))
        self.max_rejects = int(max_rejects)
        self.rejects = 0
        self._idle = 0            # 连续"不在对局里"的轮数（回菜单/结算页的兜底）
        self._idle_since = None   # 空盘面从什么时候开始（按时间判，不按步数）
        self.max_turns = int(t.get("max_turns") or 0)   # 只打 N 个我方回合（0=不限）
        self.turns_done = 0
        self._saw_pending = False   # 上一步见过等待态（如 pick）⇒ 下一步仍要问
        self._mulligan_done_local = False   # 本地记：已成功确认过换牌（防判据滞后重进）
        self._rejected = set()      # 被游戏拒过的 (kind, card, target)：这一步别再提
        self._rej_kind: dict = {}   # (回合, 动作类) → 被拒次数
        self._turn_seen = None
        self.no_pass_guard = True   # "有可出的牌就不许空过"（NN 未训好期间的守卫）
        # 快照钩子：给"同进程录制器"用（`Recorder.tick(st)`）——一个进程一个会话
        self.on_snapshot = None

    def _jitter(self) -> float:
        """一次"人类式"的操作前延迟（秒）。"""
        d = self.rng.uniform(min(self.think_min, self.think_max),
                             max(self.think_min, self.think_max))
        if self.long_prob > 0 and self.rng.random() < self.long_prob:
            d += self.rng.uniform(min(self.long_min, self.long_max),
                                  max(self.long_min, self.long_max))
        return d

    def _execute(self, a: Action) -> dict:
        s = self.sess
        import os as _os
        # ★★ 2026-10-01 用户**硬要求**："必须不用"动真鼠标。两条拐棍
        #   （`_park_cursor_outside` 每个动作前把光标挪出窗口 / `_cursor_to_target` 攻击前把光标挪到目标卡上）
        #   **已删除**（PLAN 阶段 0 任务 2）：实机 16/16 证明提交批次写
        #   `LocationUnderCursor=7`/`RowUnderCursor=1` 就够了，不需要靠真鼠标位置救。
        #   `cursor_in_window` 的日志字段保留 —— 用来识别"用户介入"（那段的失败不算缺陷）。
        if a.kind == "attack":
            # 只试一次：真人一次拖拽只提交一次。旧默认 retry=True 会连拖 3 轮（再补一个
            # OnActorEndDrag），每轮都生成/销毁箭头等对象；2026-09-30 崩溃现场
            # （kards+0x11ad577，多播委托清理时对已释放对象做虚调用）就出现在这种重试里。
            # 2026-10-01：失败常成簇（连着几次都被吞），真人也会再拖一次 ⇒ 重试 1 次（共 2 次），
            #   日志里 `attempt` 记下是第几次成的，用来判断"重试能否救回来"。
            return s.attack(a.card, a.target, retry=2)
        if a.kind == "play_unit":
            return s.play_card_unit(a.card)
        if a.kind == "play_unit_target":
            return s.play_card_unit_with_target(a.card, a.target)
        if a.kind == "play_event":
            return s.play_card_event(a.card)
        if a.kind == "play_event_target":
            return s.play_card_event_with_target(a.card, a.target)
        if a.kind == "move_up":
            return s.move_up(a.card)
        if a.kind == "end":
            return s.end_turn()
        if a.kind == "pick":
            if a.target is not None:
                return s.choose_one_with_target(a.index, a.target,
                                                trigger=a.meta.get("trigger_id"))
            return s.pick(a.index, kind=a.meta.get("kind"),
                          trigger=a.meta.get("trigger_id"))
        if a.kind == "hand_target":
            return s.select_hand_target(a.card)
        if a.kind == "board_target":
            return s.select_unit_target(a.card, a.target)
        if a.kind == "mulligan":
            # marks 里是手牌**位置**（slot = location_number），`mulligan_mark` 要的是
            # card_id —— 直接把 slot 当 card_id 传，点到的是别的牌/找不到牌
            # （2026-09-30 实机：换牌标记全落空）。先按当前手牌映射。
            rows = s.mulligan_marks() or []
            by_slot = {r.get("slot"): r.get("card_id") for r in rows}
            ids = [by_slot.get(x, x if x in {r.get("card_id") for r in rows} else None)
                   for x in a.marks]
            # 发牌动画没落定时点击是无效的（2026-09-30：回路一检测到换牌就点，标记不翻转，
            # 反复重试到窗口关闭）。先等一下；标了没翻转就再试**一次**，不无限重试
            # （重复注入点击还会把游戏打崩：AV 0x28）。
            _settle(1.2, 6)      # 等发牌动画落定 ⇒ 按帧等（降帧时 1.2 s 里可能一帧没跑）
            out = []
            for cid in ids:
                if cid is None:
                    continue
                r1 = s.mulligan_mark(cid)
                if not r1.get("ok") and not r1.get("error"):
                    time.sleep(0.8)
                    r1 = s.mulligan_mark(cid)
                out.append(r1)
            out.append(s.mulligan_confirm())
            return {"ok": all(x.get("ok", True) for x in out), "steps": out}
        return {"ok": False, "error": "未实现的动作 %r" % a.kind}

    def phase_of(self, st, pend: Optional[dict] = None) -> str:
        # ★ 只有**真的在对局里**（盘面非空）才认 "finished"：牌组页/加载中也会读到
        #   上一局残留的 `match_finished=True`，那会把刚点完开始的一局误判成已结束
        #   （2026-09-29 实机：第 2 局起每局只走 1 步）。
        if getattr(st, "match_finished", False) and len(getattr(st, "cards", None) or []):
            return "finished"
        # ★ 注入调用越少越不容易踩到 frida-agent 的崩溃（2026-09-28 实机教训：
        #   点"开始"那一下崩在 frida-agent.dll，出错模块名就是注入器）。
        #   ⇒ 对方回合**根本不问** pending（那些等待态只可能在我方回合出现），
        #     换牌标记也只在开局（turn<=1）才查。待命时每步注入调用从 ~6 降到 ~0。
        our = bool(getattr(st, "our_turn", False))
        if pend is None:
            pend = self.sess.pending() if (our or self._saw_pending) else {}
        # ★ 2026-09-30 实机：pick 面板关掉后，`choose_one` 里仍会**残留**候选行
        #   （pick_choice 自己就报"别点残留"）。只看行非空会把它当成"还在选"，
        #   回路空转 25 步（≈30s）。面板是否真的开着，以游戏自己的 `pick_pending` 为准；
        #   读不到（None）才退回"行非空"的旧判据。
        pp = pend.get("pick_pending")
        panel_open = (pp.get("pending") if isinstance(pp, dict) else None)
        pick_now = bool(pend.get("choose_one")) and panel_open is not False
        self._saw_pending = bool(pick_now
                                 or (pend.get("hand_target") or {}).get("pending")
                                 or pend.get("board_target"))
        if pick_now:
            return "pick"
        if (pend.get("hand_target") or {}).get("pending"):
            return "hand_target"
        if pend.get("board_target"):
            return "board_target"
        # ★ 2026-09-30 用户点破：游戏明明在等选择（pick_pending=True），可候选行和
        #   hand_target 都读不到时，旧代码掉进 "main" 去算"还能做什么"——面板开着时
        #   出牌/攻击/结束回合全会被吞。游戏说在等，就**只等**，不做主阶段决策。
        if panel_open is True:
            self._saw_pending = True
            return "hold"
        # ★ 换牌判据（2026-09-29 修）：`mulligan_marks()` 返回的是**当前手牌**，
        #   任何时刻都非空 —— 拿它当"在换牌界面"会把整局卡在 turn 1（实机踩过）。
        #   正确判据是游戏自己的 `Logic.myMulliganDone`（False=还在换牌）。
        if int(getattr(st, "turn", 0) or 0) <= 1 and not self._mulligan_done_local:
            try:
                md = self.sess.mulligan_done()
            except Exception:                                    # noqa: BLE001
                md = None
            # ★ 换牌**窗口**才是硬判据：`myMulliganDone` 会滞后（实机：读到 True 时
            #   窗口还开着，于是对着换牌界面狂点"结束回合"，被游戏默默吞掉）。
            try:
                showing = precheck.call_read("showing_starting_hand")
            except Exception:                                    # noqa: BLE001
                showing = None
            if md is False or showing is True:
                return "mulligan"
        if our:
            return "main"
        return "wait"

    def _has_playable(self, st) -> bool:
        """手里有没有"费用够、游戏也说能出"的牌（判据说不准也当作能出）。"""
        kred = int((st.kredits or {}).get(LOCAL) or 0)
        for c in st.hand(LOCAL):
            if (c.kredit_cost or 0) > kred:
                continue
            try:
                if self.sess.can_play(c).get("can") is not False:
                    return True
            except Exception:                                     # noqa: BLE001
                return True
        return False

    def _try_end_turn(self):
        """停手前把回合交出去（否则残局会卡住，runner 也回不到牌组页）。"""
        if not self.live:
            return
        try:
            st = self.sess.snapshot()
            if getattr(st, "our_turn", False) and len(st.cards):
                r = self.sess.end_turn()
                print("   停手前结束回合：%s" % (r or {}).get("ok"), flush=True)
        except Exception as e:                                    # noqa: BLE001
            print("   停手前结束回合失败：%s" % e, flush=True)

    def step(self) -> Optional[Step]:
        try:                                                       # 热重载：flag 文件在就重载 Python 模块（见 player/hotreload.py）
            from player import hotreload as _hr
            _hr.check_and_run()
        except Exception:                                          # noqa: BLE001
            pass
        _t0 = time.time()
        st = self.sess.snapshot()
        _t_snap = time.time() - _t0
        if self.on_snapshot is not None:
            try:
                self.on_snapshot(st)
            except Exception as e:                                # noqa: BLE001
                if self.verbose:
                    print("     [录制钩子出错，继续] %s" % e)
        our = bool(getattr(st, "our_turn", False))
        pend = self.sess.pending() if (our or self._saw_pending) else {}
        phase = self.phase_of(st, pend)
        if getattr(st, "turn", None) != self._turn_seen:
            self._turn_seen = getattr(st, "turn", None)
            self._rejected.clear()
            if hasattr(self.policy, "suppress_kinds"):
                self.policy.suppress_kinds.clear()
        rec = Step(t=time.time(), phase=phase, turn=getattr(st, "turn", None),
                   kredits=(st.kredits or {}).get(LOCAL), action=None)
        if phase == "finished":
            self.done = True
            rec.note = "对局结束"
            self.steps.append(rec)
            return rec
        if phase == "hold":
            # 面板在等、但候选还读不到：短睡再问，不决策（别在开着的面板上乱点）
            time.sleep(self.rng.uniform(0.4, 0.8))
            rec.note = "游戏在等选择（pick_pending），候选未就绪——只等不决策"
            self._hold_n = getattr(self, "_hold_n", 0) + 1
            if self._hold_n % 20 == 0:
                rec.note += "（已连续 %d 步）" % self._hold_n
            self.steps.append(rec)
            return rec
        self._hold_n = 0
        if phase == "wait":
            # 不规则轮询：等一小段随机时间再回来看（不要每 0.5 s 准点抬头）
            self.sess.wait_our_turn(limit=self.rng.uniform(0.4, 1.2))
            time.sleep(self.rng.uniform(0.05, 0.30))
            rec.note = "等对方回合"
            # 对手回合是空闲时间：预热手牌的 VM 效果缓存（我方回合的决策就不用再等 VM）
            if hasattr(self.policy, "warm"):
                try:
                    self.policy.warm(st)
                except Exception:                                 # noqa: BLE001
                    pass
            # 兜底：牌面空 = 不在对局里（回菜单/结算/还在加载）⇒ 别无限等下去。
            # 用**时间**判（不是步数）：加载一局要十几秒，按步数会误判。
            if getattr(st, "turn", 0) in (0, None):
                if self._idle_since is None:
                    self._idle_since = time.time()
                elif time.time() - self._idle_since > 180.0:
                    self.done = True
                    rec.note = "不在对局中（空盘面超过 180s）——收工"
            else:
                self._idle_since = None
            if self.done and rec.note.startswith("不在"):
                pass
            self.steps.append(rec)
            return rec
        self._idle_since = None
        # ★ 回合首步：`our_turn` 翻成 True 时指挥点可能还没结算完（日志里多个我方回合
        #   首步读到 kredits=0、闸门全说不行，于是当场结束回合，白丢一整回合）。
        #   首步先停一小会儿再重拍；两次读数都记进日志（rec.extra），事后可核。
        if phase == "main" and self.live and self.turn_settle > 0                 and getattr(self, "_settled_turn", None) != rec.turn:
            k0 = (st.kredits or {}).get(LOCAL)
            _settle(self.turn_settle, 6)   # 回合首步等指挥点结算 ⇒ 按帧等（结算要游戏真的跑帧）
            try:
                st = self.sess.snapshot()
                rec.kredits = (st.kredits or {}).get(LOCAL)
            except Exception:                                     # noqa: BLE001
                pass
            rec.extra["kred_settle"] = (k0, rec.kredits)
            self._settled_turn = rec.turn
        excl = {c for (_k, c, _t) in self._rejected if c is not None}
        _t1 = time.time()
        act = as_action(self.policy.decide(st, phase, pend, exclude_cards=excl))
        # 分段计时（用户 2026-10-01：单步延迟仍高，"结束回合花 15 s"）：快照 / 决策 / 之后的执行各多久
        rec.extra["t_snap"] = round(_t_snap, 2)
        rec.extra["t_decide"] = round(time.time() - _t1, 2)
        rec.extra["t_pre"] = round(_t1 - _t0 - _t_snap, 2)
        if rec.extra["t_pre"] > 3.0:                               # 异常慢的"决策前"那段：记下 pending() 各项耗时
            rec.extra["t_pending_parts"] = getattr(self.sess, "last_pending_timing", None)
        # ★ 守卫（用户 2026-09-29："阴云密布在手牌中不打"）：网络想空过，但手里确实
        #   有费用够、游戏也说能出的牌 ⇒ 只改写"类型"，**出哪张仍旧由网络指针头给**。
        # 用户口径（2026-09-29）："我没见过它打出过牌" ⇒ 只要**还有可出的牌**，
        # 就不许 end / 光上线（这两类占满回合＝白扔费用）。攻击仍然优先，
        # 因为攻击是当前棋盘上更硬的收益；出哪张牌仍旧由网络指针头给。
        if (phase == "main" and act is not None and act.kind in ("end", "move_up")
                and self.no_pass_guard and hasattr(self.policy, "force_play")
                and self._has_playable(st)):
            forced = self.policy.force_play(st, exclude_cards=excl)
            if forced is not None and forced.card not in excl:
                forced.note += "｜guard:有牌可出，禁止空过"
                act = forced
        rec.action = act.to_dict() if act else None
        if act is not None and getattr(self.policy, "probe", None):
            rec.extra["probe"] = self.policy.probe
        if act is None:
            rec.note = "没有可执行的动作"
            if self.live:
                time.sleep(0.4)          # 别空转烧 max_actions（面板还在换页时会走到这里）
            self.steps.append(rec)
            return rec
        if self.verbose:
            print("[%s] turn=%s k=%s -> %s" % (phase, rec.turn, rec.kredits,
                                               json.dumps(rec.action, ensure_ascii=False)))
        if not self.live:
            rec.note = "dry-run（未执行）"
            self.steps.append(rec)
            if act.kind == "end":
                self.done = True
            return rec
        delay = self._jitter()
        rec.extra["delay"] = round(delay, 2)
        time.sleep(delay)
        # ★ 发出前重拍一次：带目标的动作，目标（和出牌者）必须**此刻**还在原位。
        #   两次崩局都发生在带目标的注入调用中途；对着已经消失/挪位的对象写目标字段
        #   是最像的成因（未证实）。多花 ~0.4s，换掉"对陈旧对象下手"这一类风险。
        if act.target is not None or act.kind in ("attack", "move_up"):
            try:
                fresh = self.sess.snapshot()
                cur = {c.card_id: c.location for c in fresh.cards}
                old_loc = {c.card_id: c.location for c in st.cards}
                gone = [x for x in (act.card, act.target)
                        if x is not None and (x not in cur or cur[x] != old_loc.get(x))]
                if gone:
                    rec.note = "发出前目标/主体已变（%s）——跳过，下一步重算" % gone
                    self._rejected.add((act.kind, act.card, act.target))
                    self.steps.append(rec)
                    return rec
            except Exception:                                     # noqa: BLE001
                pass
        if act.kind in ("attack", "move_up", "deploy", "play_unit"):
            try:                      # 攻击者/目标此刻所在的行（成败对照用：前线 vs 后排）
                loc = {c.card_id: (c.location, c.side) for c in st.cards}
                rec.extra["atk_loc"] = loc.get(act.card)
                rec.extra["tgt_loc"] = loc.get(act.target) if act.target is not None else None
                byid = {c.card_id: c for c in st.cards}
                if hasattr(self.policy, "_kw"):
                    for key, cid in (("atk_kw", act.card), ("tgt_kw", act.target)):
                        c_ = byid.get(cid)
                        if c_ is not None:
                            rec.extra[key] = sorted(self.policy._kw(c_))
                    t_ = byid.get(act.target) if act.target is not None else None
                    if t_ is not None:
                        rec.extra["tgt_info"] = [t_.name, t_.card_type, getattr(t_, "is_being_guarded", None)]
            except Exception:                                     # noqa: BLE001
                pass
        try:                                                       # 真实光标当前在不在游戏窗口里（对照攻击成败）
            import ctypes
            from ctypes import wintypes
            _r, _p = wintypes.RECT(), wintypes.POINT()
            ctypes.windll.user32.GetWindowRect(self._fg_hwnd, ctypes.byref(_r)) if self._fg_hwnd else None
            ctypes.windll.user32.GetCursorPos(ctypes.byref(_p))
            rec.extra["cursor_in_window"] = bool(self._fg_hwnd and _r.left <= _p.x <= _r.right and _r.top <= _p.y <= _r.bottom)
        except Exception:                                          # noqa: BLE001
            pass
        _t2 = time.time()
        res = self._execute(act)
        rec.extra["t_exec"] = round(time.time() - _t2, 2)
        rec.extra["t_gap_before_exec"] = round(_t2 - _t1 - rec.extra.get("t_decide", 0.0), 2)   # 决策后到发出前（含随机延迟、重拍）
        rec.result = {k: v for k, v in (res or {}).items()
                      if k in ("ok", "error", "stopped", "needs_unit_click")}
        # 换牌：确认那一步的界面收尾（`ui_tail`）与警告上提到日志里，B4 才查得到“按钮/提示有没有收掉”
        if isinstance(res, dict):
            for st_ in reversed(res.get("steps") or ()):
                if isinstance(st_, dict) and ("ui_tail" in st_ or "warning" in st_):
                    for k_ in ("ui_tail", "warning", "window_closed_by_click"):
                        if k_ in st_:
                            rec.result[k_] = st_[k_]
                    break
        if isinstance(res, dict):                                 # 分段耗时（play_card / pick_choice / 攻击等给的）
            for k_ in ("timing", "arrow_wait", "waited", "pre_commit", "attempt", "late_receipt"):
                if res.get(k_) is not None:
                    rec.extra["x_" + k_] = res[k_]
        rec.executed = bool((res or {}).get("ok"))
        # 抉择点选：面板已经推进到下一层（outcome=advanced）说明这一下被游戏吃掉了，只是动作流
        # 回执滞后。当成被拒会重发同一批陈旧候选（一局里出现过 6 次拒绝 + 十几步空转）。
        if (not rec.executed and act.kind == "pick" and isinstance(res, dict)
                and res.get("outcome") == "advanced"):
            rec.executed = True
            rec.note = "抉择面板已推进（回执滞后）"
        # ★ 2026-09-30 用户点破（5th RANGERS）：**部署会弹抉择面板**——卡先留在手里、
        #   选完才落地，所以第一步的动作流本来就是空的（#28）。出牌后如果游戏进了
        #   "在等选择"的态，这一步就是成功的第一半，不是被拒；否则会白记一次拒绝、
        #   还白等一轮重试。（只在出牌类动作上这么判；判据是游戏自己的 pending。）
        if not rec.executed and act.kind.startswith("play_"):
            try:
                pd = self.sess.pending()
            except Exception:                                     # noqa: BLE001
                pd = {}
            # ★ 注入器挂了（游戏崩了）时，pending() 每一项都是"被拦截"的字典——非空但不是
            #   真状态；2026-09-30 13:57 崩局时就把这一步错记成了"面板开了=成功"。
            if any(isinstance(v, dict) and (v.get("injector_unhealthy") or v.get("blocked_by_unhealthy"))
                   for v in pd.values()):
                pd = {}
            pp = pd.get("pick_pending")
            bt = pd.get("board_target")
            if ((pd.get("choose_one") and isinstance(pp, dict) and pp.get("pending"))
                    or (pd.get("hand_target") or {}).get("pending")
                    or (isinstance(bt, int) and bt)):
                rec.executed = True
                rec.note = "出牌打开了选择面板（抉择/选目标在出牌后弹出）"
        # ★ 2026-10-01：预报换层时 `layer_gone`/`no_live_candidates`/`actor_not_alive` 是"下一层候选还没出来/上一层的残留"，
        #   不是被拒——旧写法把它们记成拒绝，连续 4 次（`--max-rejects`）就整局停手，游戏卡在选择界面。
        #   现在：只等（1 s），不计入被拒，也不进熔断。
        if (not rec.executed and act.kind == "pick" and isinstance(res, dict)
                and res.get("reason") in ("layer_gone", "no_live_candidates", "actor_not_alive")):
            rec.note = "预报换层/候选未就绪（%s）——等" % res.get("reason")
            time.sleep(1.0)
            self.steps.append(rec)
            return rec
        if not rec.executed:
            # ★ 2026-10-02（用户看出"能赢却不打脸"）：游戏**还开着自己的选择面板**时，
            #   注入器会拒绝任何动作（这是对的），但**这不是被拒** —— 把它记成被拒会让
            #   这个动作本回合再也不能提，"清完面板再补刀"那一步就没了。
            #   实测：致命一击 `attack THE PROFESSIONALS → DANZIG (+945)` 就这么被废掉，
            #   随后转去打了一个单位（看着像"不打脸"）。
            #   处理：不记 _rejected / 不计 rejects / 不熔断，只等一下（下一步会走 pick 清面板）。
            if isinstance(res, dict) and "现在开着选择界面" in str(res.get("error") or ""):
                rec.note = "面板还开着（%s）——先清面板，不动别的" % str(res.get("error"))[:36]
                self._rejected.discard((act.kind, act.card, act.target))
                time.sleep(0.4)
                self.steps.append(rec)
                return rec
            hints = self.sess.notify_texts(limit=3)
            rec.note = "被拒：%s" % (hints or res)
            if self.verbose:
                print("     被拒：", hints or res)
            self.rejects += 1
            self._rejected.add((act.kind, act.card, act.target))
            # ★ 回合开始后紧接的出牌被拒、提示是"友方回合"这类横幅（指挥点/界面还没就绪）：再等 1.5 s 重试一次，
            #   不记入被拒集合、不吃额度（每回合每张牌最多一次）。
            try:
                banner = any("友方回合" in str(h) for h in (hints or []))
            except Exception:                                     # noqa: BLE001
                banner = False
            _rk = (self._turn_seen, act.kind, act.card)
            if banner and act.kind.startswith("play_") and _rk not in getattr(self, "_banner_retry", set()):
                self._banner_retry = getattr(self, "_banner_retry", set()) | {_rk}
                self._rejected.discard((act.kind, act.card, act.target))
                self.rejects = max(0, self.rejects - 1)
                rec.note += "（横幅期被拒，1.5s 后重试一次）"
                time.sleep(1.5)
            # 同一类动作本回合被拒 2 次 ⇒ 本回合不再试这一类（换别的动作/结束回合），
            # 别让同一类的连续失败把 max_rejects 吃光、整局停手（2026-09-30 攻击全被拒时发生）。
            # ★ 只对「回合内可换别的做法」的动作类熔断（攻击/上线/出牌）。换牌、抉择、选目标等
            #   没有替代做法，被拒就是被拒，必须照常累计到 max_rejects 停手——第一版把 rejects 清零
            #   没区分这个，换牌标记失败被无限重试了 ~35 次，最后把游戏打崩（2026-09-30 18:19）。
            if act.kind in ("attack", "move_up", "play_unit", "play_event",
                            "play_unit_target", "play_event_target"):
                k = (self._turn_seen, act.kind.split("_")[0])
                self._rej_kind[k] = self._rej_kind.get(k, 0) + 1
                if self._rej_kind[k] >= 2 and hasattr(self.policy, "suppress_kinds"):
                    self.policy.suppress_kinds.add(act.kind)
                    self.rejects = max(0, self.rejects - 1)   # 熔断已处理这一类，不吃整局停手的额度
            time.sleep(self.rng.uniform(0.1, 0.4))
        elif act.kind == "end":
            self.turns_done += 1
        elif act.kind == "mulligan":
            self._mulligan_done_local = True
        # 每个动作之后（成功或失败）都把 PC 的悬停/拖拽指针清成 0：目标可能刚被这一步打死销毁，
        # 悬空的 mouseOverActor/oldMouseOverActor 会让游戏下一帧对已释放对象发事件（崩溃现场）。
        if self.live and act.kind not in ("end",):
            try:
                precheck.call_write("pc_clear_hover")
            except Exception:                                     # noqa: BLE001
                pass
        if rec.executed:
            self.rejects = 0
            self._rejected.clear()
            if self.settle > 0 and act.kind != "end":
                # 部署/出牌/抉择/选目标会生成或移动对象、放动画：多等一会儿再做下一步
                # （下一步紧接着攻击刚部署的单位，是崩溃现场里的共同点）
                long_ = act.kind.startswith("play") or act.kind in (
                    "pick", "hand_target", "board_target", "move_up")
                # 动作之后"让游戏反应"的这段也按帧等：降帧时下一步会对着没更新的界面做动作
                _settle(self.settle_long if long_ else self.settle, 6 if long_ else 3)
        # 动作之后再抽一次 0.1s 量级的尾巴，避免每步间隔一模一样
        if self.live:
            time.sleep(self.rng.uniform(0.03, 0.25))
        self.steps.append(rec)
        return rec

    def run(self) -> dict:
        n = 0
        while not self.done and n < self.max_actions:
            rec = self.step()
            n += 1
            if rec is None:
                break
            # 注入侧一旦不健康（游戏崩了/脚本被销毁）就整轮停手，别对着死进程空转
            if self.live and not precheck.healthy():
                print("注入侧不健康（%s）⇒ 停手" % precheck.unhealth_reason(), flush=True)
                self.done = True
                break
            # 被游戏拒：允许重试到上限（每次拒都落盘），超了就停手
            if self.live and self.rejects >= self.max_rejects > 0:
                print("连续被拒 %d 次（--max-rejects）⇒ 停手" % self.rejects)
                self.done = True
                self._try_end_turn()
                break
            if self.max_turns and self.turns_done >= self.max_turns:
                print("已打满 %d 个我方回合（--turns）⇒ 收工" % self.turns_done)
                self.done = True
                self._try_end_turn()
                break
        out = {"policy": getattr(self.policy, "name", "?"),
               "live": self.live, "n": n, "done": self.done,
               "steps": [s.__dict__ for s in self.steps]}
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        return out


# ---------------------------------------------------------------------------
# 离线自检（不碰游戏）
# ---------------------------------------------------------------------------
class _C:
    def __init__(self, side, loc, cid, name="X", atk=0, dfn=0, cost=0, slot=0):
        self.side, self.location, self.card_id, self.name = side, loc, cid, name
        self.attack, self.defense, self.kredit_cost, self.slot = atk, dfn, cost, slot
        self.uid, self.is_revealed = "0x%X" % cid, False
        self.keywords, self.raw = [], {}


class _S:
    def __init__(self, cards, kredits=3, our_turn=True, turn=5, finished=False):
        self.cards = cards
        self.kredits = {"local": kredits, "enemy": 2}
        self.our_turn, self.turn, self.match_finished = our_turn, turn, finished

    def hand(self, side):
        return [c for c in self.cards if c.side == side and c.location == "hand"]


class _Sess:
    """假命令层：只回答判据/等待态，不做动作。"""

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

    def pending(self):
        return {}

    def mulligan_marks(self):
        return []

    def wait_our_turn(self, limit=30.0):
        return True


def selftest() -> int:
    from learn.features import CardTable
    fails = 0

    def chk(name, ok):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    table = CardTable.load()
    st = _S([_C(LOCAL, "hand", 20, "WE CAN DO IT!", 0, 0, 5, 0),
             _C(LOCAL, "frontline", 10, "A", 5, 3, 3, 1),
             _C(ENEMY, "hq", 41, "HQ", 0, 20, 0, 0),
             _C(ENEMY, "back", 50, "E", 2, 3, 2, 1)], kredits=5)

    # ① 网络策略：决策来自网络（probe 里必须有三类 + end 的分数）
    pol = NNPolicy(_Sess(), table, ckpt_path=None, device="cpu")
    a = pol.choose_main(st)
    chk("NN：主回合给出动作", isinstance(a, Action) and a.kind in (
        "play_unit", "play_event", "play_unit_target", "play_event_target",
        "attack", "move_up", "end"))
    chk("NN：probe 里是网络的分项分数（type+指针）",
        "scores" in pol.probe and "detail" in pol.probe
        and any(k.startswith("play") for k in pol.probe["scores"])
        and any(k.startswith("end") for k in pol.probe["scores"]))
    a_again = pol.choose_main(st)
    chk("NN：同一局面两次决策一致（确定性）", a.to_dict() == a_again.to_dict())

    # ② **网络驱动证明**：改 type 头的 bias，动作必须跟着变
    tid = pol.TYPE_ID
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["play"]] += 60.0
    a_play = pol.choose_main(st)
    chk("NN 驱动：把 type 头 bias 推向 play ⇒ 动作变成 play",
        a_play.kind.startswith("play"))
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["play"]] -= 60.0
        pol.model.type_head.bias[tid["attack"]] += 60.0
    a_atk = pol.choose_main(st)
    chk("NN 驱动：把 bias 推向 attack ⇒ 动作变成 attack（目标由目标头给）",
        a_atk.kind == "attack" and a_atk.target in (41, 50))
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["attack"]] -= 60.0

    # ③ 指针头驱动：把 subject 打分器改成"只看某个实体"（用 query 权重放大），
    #    只要动作跟着变就说明主体确实来自指针头而不是别的什么东西
    enc_play = pol._encode(st, "main", ltype="play")
    out_play = pol._forward([enc_play])[0]
    si = int(pol._np(out_play["subject_scores"][0]).argmax())
    chk("NN：play 主体下标落在候选范围内",
        0 <= si < len(enc_play.subj)
        and enc_play.debug["subj_ids"][si] is not None)
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["end"]] -= 60.0
    no_end = pol.choose_main(st).to_dict()
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["end"]] += 120.0
    must_end = pol.choose_main(st).to_dict()
    chk("NN 驱动：压低 end bias ⇒ 不做 end；抬高 ⇒ 一定做 end（双向）",
        no_end["kind"] != "end" and must_end["kind"] == "end")
    with pol.torch.no_grad():
        pol.model.type_head.bias[tid["end"]] -= 60.0

    # ④ 回路：dry-run 不执行、finished 会收尾、dict 动作能转 Action
    lp = Loop(_Sess(), pol, live=False, max_actions=1)
    lp.sess.snapshot = lambda: st
    rec = lp.step()
    chk("回路 dry-run：有动作但 executed=False",
        rec.action is not None and rec.executed is False and rec.phase == "main")
    chk("dict -> Action 转换", as_action({"kind": "end", "note": "x"}).kind == "end")
    lp2 = Loop(_Sess(), StrategicRule(_Sess()), live=False, max_actions=1)
    lp2.sess.snapshot = lambda: _S([_C(LOCAL, "hq", 1, "HQ", 0, 20, 0)], finished=True)
    chk("回路：对局结束 -> finished", lp2.step().phase == "finished")
    # ★ 反例（2026-09-29 实机踩过）：刚点完开始、盘面还没加载时，快照会读到上一局
    #   残留的 match_finished=True + 空盘面 —— 不能当成"对局结束"。
    lp3 = Loop(_Sess(), StrategicRule(_Sess()), live=False, max_actions=1)
    lp3.sess.snapshot = lambda: _S([], finished=True)
    chk("空盘面 + 残留 finished 不算结束（防开局误判）",
        lp3.step().phase != "finished")

    # ⑤ 规则基线（在 nn/baselines.py 里）也能被回路当策略用（dict -> Action）
    rule = StrategicRule(_Sess({(10, 41): True}))
    ra = as_action(rule.choose_main(st))
    chk("规则基线经 as_action 可用于回路",
        isinstance(ra, Action) and ra.kind in ("attack", "play_event"))

    # ⑥ strict vs fallback：**编码失败**时，strict 必须抛，fallback 必须退规则
    pol_s = NNPolicy(_Sess(), table, ckpt_path=None, device="cpu", strict=True)
    pol_s._encode = lambda *a, **k: None
    try:
        pol_s.choose_main(st)
        chk("strict：编码失败必须抛错（不悄悄降级）", False)
    except Exception:
        chk("strict：编码失败必须抛错（不悄悄降级）", True)
    pol_f = NNPolicy(_Sess(), table, ckpt_path=None, device="cpu", strict=False)
    pol_f._encode = lambda *a, **k: None
    fb = pol_f.decide(st, "main", {})
    chk("fallback：编码失败退回规则基线（不是递归/崩溃）",
        isinstance(fb, Action) and fb.note.startswith("nn 失败"))

    print("player.loop selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NN 在线回路（前端③；默认 dry-run）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--live", action="store_true", help="真的发动作（默认只打印）")
    ap.add_argument("--policy", default="nn", choices=["nn", "rule", "rule2"],
                    help="nn=网络策略（默认）；rule=旧启发式基线；"
                         "rule2=player/rule.py（字节码画像 + 游戏闸门；推荐在常驻会话里用 "
                         "player.rule.play，不要起短命进程 —— 退出会卸载 frida 崩游戏）")
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--fallback-rule", action="store_true",
                    help="网络失败时退回规则（默认 strict：失败即停手）")
    ap.add_argument("--device", default=None)
    ap.add_argument("--mulligan", default="rule", choices=["rule", "nn"],
                    help="换牌决策：rule=跳费口径（默认；NN mulligan 头未训练）；"
                         "nn=用网络头（只有 8 条全留标签，仅作对比）")
    ap.add_argument("--max-actions", type=int, default=40)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--think-min", type=float, default=0.05,
                    help="动作前抖动下限（秒）——默认 0.1s 量级")
    ap.add_argument("--think-max", type=float, default=0.35)
    ap.add_argument("--long-prob", type=float, default=0.0,
                    help="偶尔叠一段更长停顿的概率（默认 0 = 不叠）")
    ap.add_argument("--long-min", type=float, default=0.5)
    ap.add_argument("--long-max", type=float, default=1.2)
    ap.add_argument("--seed", type=int, default=None, help="固定随机节奏（复现用）")
    ap.add_argument("--max-rejects", type=int, default=4,
                    help="累计被游戏拒几次就停手（0=不限）")
    ap.add_argument("--turns", type=int, default=0,
                    help="只打 N 个我方回合就收工（0=不限）")
    a = ap.parse_args(argv)
    if a.selftest:
        return 1 if selftest() else 0

    sess = _session.AgentSession(translate=False, warm=True)
    table = None
    try:
        from learn.features import CardTable
        table = CardTable.load()
    except Exception:                                             # noqa: BLE001
        pass
    if a.policy == "nn":
        if not a.ckpt:
            print("--policy nn 需要 --ckpt（没有权重就不是神经网络策略）")
            return 2
        policy = NNPolicy(sess, table, ckpt_path=a.ckpt, device=a.device,
                          strict=not a.fallback_rule,
                          mulligan_rule=(a.mulligan == "rule"))
    elif a.policy == "rule2":
        from player.rule import RuleV2
        policy = RuleV2(sess, table=table)
    else:
        policy = StrategicRule(sess, table=table)
    log = None if a.no_log else os.path.join(
        LOG_DIR, "policy-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    timing = {"think_min": a.think_min, "think_max": a.think_max,
              "long_prob": a.long_prob, "long_min": a.long_min,
              "long_max": a.long_max, "seed": a.seed, "max_turns": a.turns}
    loop = Loop(sess, policy, live=a.live,
                max_actions=1 if a.once else a.max_actions, log_path=log,
                timing=timing, max_rejects=a.max_rejects)
    print("策略=%s  live=%s（dry-run 只打印，不动鼠标）" % (policy.name, a.live))
    print("节奏：基础 %.1f~%.1fs，%.0f%% 概率叠 %.0f~%.0fs 长考（seed=%s）"
          % (a.think_min, a.think_max, a.long_prob * 100, a.long_min, a.long_max,
             a.seed))
    out = loop.run()
    print("走了 %d 步；日志 -> %s" % (out["n"], log))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
