#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.encode —— 一条样本（可见性投影后的 JSON） -> 张量 + 候选集 + 标签索引。

设计对应 KARDS-NN.md：
    §3.1  set-based token 化：每张**可见**卡一个实体 token + 一个全局向量。
          - 双方牌库：只进全局计数，**不生成实体**（P2/P3）。
          - 敌方手牌：只进计数；被 intel 看过的（`reason == "P4b_intel_seen"`）
            才生成实体（P1/P4b）。
          - 未揭露的隐蔽场上牌：生成匿名实体（无身份/无攻防，P4）。
    §4.2  候选集**不由网络猜**：主体/目标/选项按 phase 从状态里结构化构造。
    §4.4  判据只在 verdict 明确有罪时给 −8 加性偏置（数据里有就带上，没有就 0
          = unknown），结构性不可用才是真 mask。

本模块**不 import torch**（numpy 足够）——训练侧 dataset.py 才转到 torch。

自检（合成样本，离线）：
    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.encode
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .features import FEATURE_DIM, CardTable

# ---------------------------------------------------------------------------
# 常量（冻结）
# ---------------------------------------------------------------------------
# ★ 座位迁移：实体的 side 特征槽是 ML 视角的**相对编码**（相对 `viewer` 这个座位）：
#   0 = 与 viewer 同座位（"自己"）、1 = 对面、2 = 座位读不出。样本里的座位是 1/2 整数（ESide 数值），
#   槽位在构造时用 `side == viewer` 判；没有字符串座位。live/自对局样本 viewer==本地座位，所以槽 0 就是『我方』。
#   （对面视角的 peek 样本：旧版槽位是绝对的『本地/对方』，现在跟着 viewer 走，和 kredits_*/hand_* 的全局特征一致。）
SLOT_NAMES = ("self", "other", "unknown")
SLOT_SELF, SLOT_OTHER, SLOT_UNKNOWN = 0, 1, 2
ZONE_NAMES = ("pad", "hq", "frontline", "back", "hand", "discard", "deck",
              "option")
#                  0     1      2          3      4      5         6      7
ZONE_ID = {n: i for i, n in enumerate(ZONE_NAMES)}

PHASE_NAMES = ("main", "pick", "hand_target", "mulligan", "deploy_target",
               "deck_pick")
PHASE_ID = {n: i for i, n in enumerate(PHASE_NAMES)}

# 动作类型（全局 8 类；每个 phase 只允许其中一部分 —— 阶段头用 allowed mask）
TYPE_NAMES = ("play", "attack", "move", "end", "pick",
              "hand_target_selected", "mulligan", "choose_target")
TYPE_ID = {n: i for i, n in enumerate(TYPE_NAMES)}

PHASE_TYPES = {
    "main":          ("play", "attack", "move", "end"),
    "pick":          ("pick",),
    "hand_target":   ("hand_target_selected",),
    "mulligan":      ("mulligan",),
    "deploy_target": ("choose_target",),
    "deck_pick":     ("pick",),
}

CAND_KIND_NAMES = ("hand", "board", "hq", "option", "other")

ENT_NUM_DIM = 8      # attack/def/费/槽位/隐蔽/揭露/有身份/我方
GLOBAL_DIM = 18

GLOBAL_LAYOUT = (
    ("turn", 30.0), ("kredits_local", 12.0), ("kredits_enemy", 12.0),
    ("hand_local", 10.0), ("hand_enemy", 10.0),
    ("deck_local", 40.0), ("deck_enemy", 40.0),
    ("discard_local", 30.0), ("discard_enemy", 30.0),
    ("hq_def_local", 40.0), ("hq_def_enemy", 40.0),
    ("hq_known_local", 1.0), ("hq_known_enemy", 1.0),
    ("actor_is_viewer", 1.0), ("phase_id", 8.0), ("n_entities", 60.0),
    ("enemy_hand_hidden", 10.0), ("identity_known_ratio", 1.0),
)

LEGAL_BIAS = -8.0    # §4.4：判据明确有罪 -> 加性偏置（不是 −inf）


@dataclass
class EncodedSample:
    """一条样本的编码结果（纯 numpy；dataset.collate 再转 torch）。"""

    game: str
    phase: str
    seat: int                  # 此样本的行动座位（1/2）
    viewer: int                # 投影视角座位（1/2）
    t: float
    state_hash: str
    n_entities: int

    # 实体 token
    ent_fname: np.ndarray      # int64 [N]   词表 id（0=unknown）
    ent_feat: np.ndarray       # f32  [N,F] 静态卡面特征
    ent_side: np.ndarray       # int64 [N]
    ent_zone: np.ndarray       # int64 [N]
    ent_num: np.ndarray        # f32  [N,ENT_NUM_DIM]
    ent_mask: np.ndarray       # bool [N] 恒 True（保留给 padding 语义）
    glob: np.ndarray           # f32  [GLOBAL_DIM]

    # 候选（存实体下标）
    subj: np.ndarray           # int64 [Ks]
    subj_kind: np.ndarray      # int64 [Ks]
    subj_legal: np.ndarray     # f32   [Ks]  +1/0/-1
    targ: np.ndarray
    targ_kind: np.ndarray
    targ_legal: np.ndarray
    opt: np.ndarray
    opt_kind: np.ndarray
    opt_legal: np.ndarray
    context: int               # 上下文实体下标（两阶段动作的第二条样本），-1=无

    # 标签（下标都指向对应候选；-1 = 该样本在这个头上没有监督）
    type_idx: int
    subj_label: int
    targ_label: int
    opt_label: int
    mull_discard: Optional[np.ndarray]   # f32 [Ko]，nan = 未知
    value: float               # 整局胜负（+1/-1），nan = 没有

    label_src: str
    label_available: bool
    debug: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 状态 -> 实体
# ---------------------------------------------------------------------------
def _zone_of(card: dict) -> str:
    z = card.get("location") or "pad"
    return z if z in ZONE_ID else "pad"


class LegacySeatError(ValueError):
    """样本里的座位不是整数 1/2（座位迁移之前录的旧样本：字符串座位）。"""


def _seat(v, what: str) -> int:
    """样本里的座位字段 -> 整数 1/2；字符串（旧录制）/ 其它 -> LegacySeatError（不猜旧座位名是几号）。"""
    if isinstance(v, bool) or not isinstance(v, int) or v not in (1, 2):
        raise LegacySeatError("样本 %s 不是座位整数 1/2：%r（旧录制需要先迁移）" % (what, v))
    return int(v)


def _by_seat(d, seat: int):
    """按座位取值：内存里的键是整数，经 JSON 往返后是 "1"/"2" 字符串，两种都认。"""
    if not d:
        return None
    if seat in d:
        return d[seat]
    return d.get(str(seat))


def _slot_of(side, viewer: int) -> int:
    """卡的座位 -> side 特征槽（相对 viewer）。座位读不出 -> 未知槽（显式，不当对面）。"""
    if side not in (1, 2):
        return SLOT_UNKNOWN
    return SLOT_SELF if side == viewer else SLOT_OTHER


def _visible(card: dict, viewer: int) -> tuple:
    """这张卡能不能变成实体 token。返回 (keep, reason)。

    只信投影结果自带的结构：`hidden=True` 的牌库/敌手牌不生成实体。
    """
    loc = _zone_of(card)
    if loc == "deck":
        return False, "P2_deck_remaining"
    if card.get("hidden") and loc == "hand" and _slot_of(card.get("side"), viewer) != SLOT_SELF:
        return False, "P1_enemy_hand"
    return True, str(card.get("reason") or "")


def _sort_key(card: dict, viewer: int) -> tuple:
    prio = {"hq": 0, "frontline": 1, "back": 2, "hand": 3, "discard": 4}
    return (prio.get(_zone_of(card), 9), _slot_of(card.get("side"), viewer),
            int(card.get("slot") or 0), int(card.get("card_id") or 0))


class _EntityBuilder:
    def __init__(self, table: CardTable, viewer: int):
        self.table = table
        self.viewer = viewer
        self.fname: list = []
        self.feat: list = []
        self.side: list = []
        self.zone: list = []
        self.num: list = []
        self.meta: list = []      # dict：card_id / reason / 名字，供候选/标签解析用

    def add_card(self, card: dict, reason: str = ""):
        fname = card.get("fname")
        title = card.get("name")
        resolved = self.table.resolve(fname, title)
        vec = self.table.vector(fname, title)
        loc = _zone_of(card)
        side = card.get("side")
        self.fname.append(self.table.id_of(resolved))
        self.feat.append(vec)
        slot = _slot_of(side, self.viewer)
        self.side.append(slot)
        self.zone.append(ZONE_ID[loc])
        self.num.append([
            min(float(card.get("attack") or 0) / 10.0, 1.0),
            min(float(card.get("defense") or 0) / 10.0, 1.0),
            min(float(card.get("kredit_cost") or 0) / 8.0, 1.0),
            min(float(card.get("slot") or 0) / 8.0, 1.0),
            1.0 if card.get("hidden") else 0.0,
            1.0 if card.get("is_revealed") else 0.0,
            1.0 if resolved else 0.0,
            1.0 if slot == SLOT_SELF else 0.0,
        ])
        self.meta.append({"card_id": card.get("card_id"), "reason": reason,
                          "title": title, "fname": resolved})
        return len(self.fname) - 1

    def add_option(self, name: Optional[str]):
        """pick/mulligan/deck_pick 的候选（样本里只给了名字——FName 或显示名）。"""
        resolved = self.table.resolve(name, name)
        vec = self.table.vector(name, name)
        self.fname.append(self.table.id_of(resolved))
        self.feat.append(vec)
        self.side.append(SLOT_SELF)
        self.zone.append(ZONE_ID["option"])
        self.num.append([0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                         1.0 if resolved else 0.0, 1.0])
        self.meta.append({"card_id": None, "reason": "option",
                          "title": name, "fname": resolved})
        return len(self.fname) - 1

    def arrays(self) -> dict:
        n = len(self.fname)
        return {
            "ent_fname": np.asarray(self.fname, dtype=np.int64),
            "ent_feat": np.asarray(self.feat, dtype=np.float32).reshape(n, FEATURE_DIM),
            "ent_side": np.asarray(self.side, dtype=np.int64),
            "ent_zone": np.asarray(self.zone, dtype=np.int64),
            "ent_num": np.asarray(self.num, dtype=np.float32).reshape(n, ENT_NUM_DIM),
        }


# ---------------------------------------------------------------------------
# 候选集
# ---------------------------------------------------------------------------
def _pick_entities(builder: _EntityBuilder, viewer: int,
                   zones, side: Optional[int] = None,
                   exclude_zone=(), only_identity=False) -> list:
    """`side` 是 side 特征槽（SLOT_SELF / SLOT_OTHER），不是座位。"""
    out = []
    for i, m in enumerate(builder.meta):
        z = ZONE_NAMES[builder.zone[i]]
        s = int(builder.side[i])
        if z not in zones or (exclude_zone and z in exclude_zone):
            continue
        if side is not None and s != side:
            continue
        if only_identity and not m["fname"]:
            continue
        out.append(i)
    return out


def _kind_of(zone_name: str) -> int:
    return CAND_KIND_NAMES.index({"hand": "hand", "frontline": "board",
                                  "back": "board", "hq": "hq",
                                  "option": "option"}.get(zone_name, "other"))


def _cand_arrays(builder: _EntityBuilder, idxs: list, verdict: dict,
                 meta_key: str = "card_id"):
    idxs = list(idxs)
    kinds = np.asarray([_kind_of(ZONE_NAMES[builder.zone[i]]) for i in idxs],
                       dtype=np.int64)
    legal = np.zeros(len(idxs), dtype=np.float32)
    for k, i in enumerate(idxs):
        cid = builder.meta[i].get(meta_key)
        v = (verdict or {}).get(str(cid)) if cid is not None else None
        if isinstance(v, dict):
            lg = v.get("legal")
            if lg in (1, -1):
                legal[k] = float(lg)
    return np.asarray(idxs, dtype=np.int64), kinds, legal


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def encode_sample(sample: dict, table: CardTable,
                  dedup_options: bool = True) -> Optional[EncodedSample]:
    """`dedup_options=False`：pick/mulligan 的候选**保持原顺序、不去重**
    （在线回路要用它把网络输出的下标原样映射回游戏 UI 的候选序号，
    去重会让下标错位）。训练侧默认 True（老录制里同名牌多次出现是同一轮）。"""
    state = sample.get("state")
    if not isinstance(state, dict):
        return None
    # 座位必须是整数 1/2（落盘格式）；不猜：state.viewer 缺了才退到 sample.seat（行动座位），都没有就报错
    viewer = _seat(state.get("viewer") if state.get("viewer") is not None else sample.get("seat"), "viewer")
    seat = _seat(sample.get("seat") if sample.get("seat") is not None else viewer, "seat")
    phase = sample.get("phase") or "main"
    if phase not in PHASE_ID:
        phase = "main"
    debug: dict = {"unresolved": []}
    # 录制器自报的 state_before 来源（2026-09-28 起会写）：ring=动作之前 /
    # fallback_post=只能拿动作之后的快照兜底（该样本的盘面语义是坏的）。
    debug["state_before_src"] = (sample.get("receipt") or {}).get("state_before_src")

    builder = _EntityBuilder(table, viewer)
    for card in sorted((state.get("cards") or []), key=lambda c: _sort_key(c, viewer)):
        keep, reason = _visible(card, viewer)
        if not keep:
            debug[reason] = debug.get(reason, 0) + 1
            continue
        builder.add_card(card, reason)

    # ---- 候选池（实体下标）
    my_hand = _pick_entities(builder, viewer, ("hand",), side=SLOT_SELF)
    enemy_hand = _pick_entities(builder, viewer, ("hand",), side=SLOT_OTHER)
    my_board = _pick_entities(builder, viewer, ("frontline", "back"), side=SLOT_SELF)
    enemy_board = _pick_entities(builder, viewer, ("frontline", "back"), side=SLOT_OTHER)
    enemy_hq = _pick_entities(builder, viewer, ("hq",), side=SLOT_OTHER)

    label = sample.get("label") or {}
    ltype = label.get("type")
    verdict = sample.get("verdict") or {}

    # phase 归一：老录制把"手牌选目标"落在 main 里（recorder 修好之前的数据）
    if phase == "main" and ltype == "hand_target_selected":
        phase = "hand_target"

    subj = targ = opt = np.zeros(0, dtype=np.int64)
    subj_k = targ_k = opt_k = np.zeros(0, dtype=np.int64)
    subj_l = targ_l = opt_l = np.zeros(0, dtype=np.float32)
    context = -1

    if phase == "main":
        play_subj = my_hand
        board_subj = my_board
        attack_targ = enemy_board + enemy_hq
        if ltype == "play":
            subj, subj_k, subj_l = _cand_arrays(builder, play_subj, verdict)
            opt, opt_k, opt_l = _cand_arrays(builder, [], verdict)
            # ★ 指向性指令（play_select）的目标是**同一条决策**的一部分 ⇒ 目标候选
            #   跟攻击共用"敌方场上 + 总部"（训练时老录制没有 play 的 target 标签，
            #   该头拿不到监督；在线推理时这张头就是要用的）。
            targ, targ_k, targ_l = _cand_arrays(builder, attack_targ, verdict)
        elif ltype in ("attack", "move"):
            # ★ move 的候选只有**后排**单位：移动是单向的（上线），前线单位不能当
            #   move 的主体（2026-09-29 实机："26 已经在前线（移动是单向的）"）。
            pool = board_subj if ltype == "attack" else _pick_entities(
                builder, viewer, ("back",), side=SLOT_SELF)
            subj, subj_k, subj_l = _cand_arrays(builder, pool, verdict)
            opt, opt_k, opt_l = _cand_arrays(builder, [], verdict)
            if ltype == "attack":
                targ, targ_k, targ_l = _cand_arrays(builder, attack_targ, verdict)
            else:
                # move 的目标语义未定（G2：线/槽位还不是干净参数）；
                # 当前录制里 move 的标签也是 label_available=False。
                targ, targ_k, targ_l = _cand_arrays(builder, [], verdict)
        else:   # end / 未知
            subj, subj_k, subj_l = _cand_arrays(builder, [], verdict)
            targ, targ_k, targ_l = _cand_arrays(builder, [], verdict)
            opt, opt_k, opt_l = _cand_arrays(builder, [], verdict)
    elif phase in ("pick", "deck_pick"):
        names = list(sample.get("candidates", {}).get("option") or [])
        opt_index = list(sample.get("candidates", {}).get("option_index") or [])
        seen, opt_names, opt_ids = set(), [], []
        for n in names:
            if n is None:
                continue
            if dedup_options and n in seen:
                continue
            seen.add(n)
            opt_names.append(n)
            opt_ids.append(opt_index[len(opt_ids)] if len(opt_ids) < len(opt_index)
                           else len(opt_ids))
        opt_list = [builder.add_option(n) for n in opt_names]
        debug["option_names"] = opt_names
        debug["option_index"] = opt_ids
        opt, opt_k, opt_l = _cand_arrays(builder, opt_list, verdict, meta_key="fname")
        subj, subj_k, subj_l = _cand_arrays(builder, [], verdict)
        targ, targ_k, targ_l = _cand_arrays(builder, [], verdict)
        # 发起卡（label.subject 是运行时 card_id）当 query 上下文
        if label.get("subject") is not None:
            context = _find_by_card_id(builder, label.get("subject"),
                                       my_board + my_hand)
    elif phase == "hand_target":
        # 第二条样本：subject=发起卡（context，不监督），target=被选中的手牌
        opt, opt_k, opt_l = _cand_arrays(builder, [], verdict)
        subj, subj_k, subj_l = _cand_arrays(builder, my_hand + my_board, verdict)
        if label.get("subject") is not None:
            context = _find_by_card_id(builder, label.get("subject"),
                                       my_hand + my_board)
        targ, targ_k, targ_l = _cand_arrays(builder, my_hand, verdict)
    elif phase == "mulligan":
        names = list(sample.get("candidates", {}).get("option") or [])
        opt_index = list(sample.get("candidates", {}).get("option_index") or [])
        opt_list = [builder.add_option(n) for n in names]
        debug["option_names"] = names
        debug["option_index"] = opt_index or list(range(len(names)))
        opt, opt_k, opt_l = _cand_arrays(builder, opt_list, verdict, meta_key="fname")
        subj, subj_k, subj_l = _cand_arrays(builder, [], verdict)
        targ, targ_k, targ_l = _cand_arrays(builder, [], verdict)
    elif phase == "deploy_target":
        opt, opt_k, opt_l = _cand_arrays(builder, [], verdict)
        subj, subj_k, subj_l = _cand_arrays(builder, [], verdict)
        targ, targ_k, targ_l = _cand_arrays(builder, enemy_board + enemy_hq, verdict)
        if label.get("subject") is not None:
            context = _find_by_card_id(builder, label.get("subject"), my_hand + my_board)

    # ---- 标签 -> 候选下标
    type_idx = TYPE_ID.get(ltype, -1) if ltype in PHASE_TYPES[phase] else -1
    subj_label = targ_label = opt_label = -1
    if type_idx >= 0:
        if phase == "main":
            if ltype in ("play", "attack", "move"):
                subj_label = _resolve_card_id(builder, label.get("subject"), subj, debug,
                                              "subject")
            if ltype == "attack":
                targ_label = _resolve_card_id(builder, label.get("target"), targ, debug,
                                              "target")
        elif phase in ("pick", "deck_pick", "mulligan"):
            opt_label = _resolve_option(builder, label.get("option"), opt, debug)
        elif phase in ("hand_target", "deploy_target"):
            targ_label = _resolve_card_id(builder, label.get("target"), targ, debug,
                                          "target")

    mull_discard = None
    if phase == "mulligan":
        lab_opt = label.get("option") or {}
        names = debug.get("option_names") or []
        mull_discard = np.full(len(opt), np.nan, dtype=np.float32)
        for k, n in enumerate(names):
            if isinstance(lab_opt, dict) and n in lab_opt:
                mull_discard[k] = 1.0 if lab_opt[n] else 0.0

    value = sample.get("value", sample.get("result"))
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float("nan")

    ent = builder.arrays()
    glob = _global_vector(state, builder, phase, viewer, seat)
    debug["_reasons"] = {i: m["reason"] for i, m in enumerate(builder.meta)}
    # ★ 在线回路要靠这些把"候选下标"映回 card_id / 游戏 UI 序号
    debug["subj_ids"] = [builder.meta[i]["card_id"] for i in subj.tolist()]
    debug["targ_ids"] = [builder.meta[i]["card_id"] for i in targ.tolist()]
    debug["opt_names"] = [builder.meta[i]["title"] for i in opt.tolist()]
    debug["context_id"] = (builder.meta[context]["card_id"]
                           if 0 <= context < len(builder.meta) else None)

    enc = EncodedSample(
        game=str(sample.get("game") or "?"), phase=phase, seat=seat,
        viewer=viewer, t=float(sample.get("t") or 0.0),
        state_hash=str(sample.get("state_hash") or ""),
        n_entities=int(ent["ent_fname"].shape[0]),
        glob=glob,
        ent_mask=np.ones(int(ent["ent_fname"].shape[0]), dtype=bool),
        subj=subj, subj_kind=subj_k, subj_legal=subj_l,
        targ=targ, targ_kind=targ_k, targ_legal=targ_l,
        opt=opt, opt_kind=opt_k, opt_legal=opt_l, context=int(context),
        type_idx=int(type_idx), subj_label=int(subj_label),
        targ_label=int(targ_label), opt_label=int(opt_label),
        mull_discard=mull_discard, value=value,
        label_src=str(sample.get("label_src") or ""),
        label_available=bool(sample.get("label_available")),
        debug=debug, **ent,
    )
    return enc


def _find_by_card_id(builder: _EntityBuilder, card_id, pool) -> int:
    for i in pool:
        if builder.meta[i].get("card_id") == card_id:
            return int(i)
    return -1


def _resolve_card_id(builder: _EntityBuilder, card_id, cand, debug: dict,
                     what: str) -> int:
    if card_id is None:
        return -1
    for k, i in enumerate(cand.tolist()):
        if builder.meta[i].get("card_id") == card_id:
            return int(k)
    debug["unresolved"].append("%s=%s" % (what, card_id))
    return -1


def _resolve_option(builder: _EntityBuilder, name, cand, debug: dict) -> int:
    if name is None:
        return -1
    for k, i in enumerate(cand.tolist()):
        if builder.meta[i].get("title") == name or builder.meta[i].get("fname") == name:
            return int(k)
    debug["unresolved"].append("option=%s" % name)
    return -1


def _global_vector(state: dict, builder: _EntityBuilder, phase: str,
                   viewer: int, seat: int) -> np.ndarray:
    g = np.zeros(GLOBAL_DIM, dtype=np.float32)
    idx = {name: i for i, (name, _) in enumerate(GLOBAL_LAYOUT)}
    scale = {name: s for name, s in GLOBAL_LAYOUT}

    def put(name, value):
        g[idx[name]] = float(value) / scale[name]

    kredits = state.get("kredits") or {}
    hands = state.get("hand_count") or {}
    decks = state.get("deck_count") or {}
    other = 2 if viewer == 1 else 1
    # 全局特征名里的 *_local / *_enemy 是冻结的特征槽名（checkpoint 兼容）：local=viewer 自己，enemy=对面
    put("turn", state.get("turn") or 0)
    put("kredits_local", _by_seat(kredits, viewer) or 0)
    put("kredits_enemy", _by_seat(kredits, other) or 0)
    put("hand_local", _by_seat(hands, viewer) or 0)
    put("hand_enemy", _by_seat(hands, other) or 0)
    put("deck_local", _by_seat(decks, viewer) or 0)
    put("deck_enemy", _by_seat(decks, other) or 0)
    disc = {SLOT_SELF: 0, SLOT_OTHER: 0}
    hq_def = {}
    for i, m in enumerate(builder.meta):
        z = ZONE_NAMES[builder.zone[i]]
        s = int(builder.side[i])
        if z == "discard" and s in disc:
            disc[s] += 1
        if z == "hq" and s in disc:
            hq_def[s] = max(hq_def.get(s, 0.0),
                            float(builder.num[i][1]) * 10.0)
    put("discard_local", disc[SLOT_SELF])
    put("discard_enemy", disc[SLOT_OTHER])
    if SLOT_SELF in hq_def:
        put("hq_def_local", hq_def[SLOT_SELF])
        g[idx["hq_known_local"]] = 1.0
    if SLOT_OTHER in hq_def:
        put("hq_def_enemy", hq_def[SLOT_OTHER])
        g[idx["hq_known_enemy"]] = 1.0
    g[idx["actor_is_viewer"]] = 1.0 if seat == viewer else 0.0
    put("phase_id", PHASE_ID.get(phase, 0))
    put("n_entities", len(builder.meta))
    put("enemy_hand_hidden", max(0, int(_by_seat(hands, other) or 0)
                                 - _count_visible_hand(builder, SLOT_OTHER)))
    known = sum(1 for m in builder.meta if m["fname"])
    g[idx["identity_known_ratio"]] = known / max(1, len(builder.meta))
    return g


def _count_visible_hand(builder: _EntityBuilder, slot: int) -> int:
    n = 0
    for i, m in enumerate(builder.meta):
        if ZONE_NAMES[builder.zone[i]] == "hand" and int(builder.side[i]) == slot:
            n += 1
    return n


# ---------------------------------------------------------------------------
# 防泄漏断言（能失败）
# ---------------------------------------------------------------------------
def assert_no_leak(enc: EncodedSample) -> None:
    """编码后的样本里不许出现：敌手牌实体（除非 intel 看过）、任何牌库实体。

    `encode_sample` 已经按投影跳过它们；这里是对**编码产物**的独立第二道闸门
    ——故意构造一个脏样本喂进来，必须抛 AssertionError。
    """
    for i in range(enc.n_entities):
        zone = ZONE_NAMES[enc.ent_zone[i]]
        slot = int(enc.ent_side[i])
        if zone == "deck":
            raise AssertionError("牌库实体泄漏（P2/P3）：entity#%d" % i)
        if zone == "hand" and slot != SLOT_SELF:
            reason = enc.debug.get("_reasons", {}).get(i, "")
            if reason != "P4b_intel_seen":
                raise AssertionError("敌方手牌实体泄漏（P1）：entity#%d" % i)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _fake_card(side, loc, cid=None, name=None, atk=0, dfn=0, cost=0, slot=0,
               hidden=False, revealed=False, reason=None):
    d = {"side": side, "location": loc, "uid": None, "card_id": cid,
         "name": name, "attack": atk, "defense": dfn, "kredit_cost": cost,
         "slot": slot, "is_revealed": revealed, "hidden": hidden}
    if reason:
        d["reason"] = reason
    return d


# 自检用座位（样本落盘格式：整数 1/2）。本地玩家 = 1，对面 = 2。
_ME, _OPP = 1, 2


def _fake_state():
    return {
        "viewer": _ME, "my_side": _ME, "turn": 7,
        "hand_count": {_ME: 2, _OPP: 3}, "deck_count": {_ME: 20, _OPP: 19},
        "kredits": {_ME: 5, _OPP: 3},
        "cards": [
            _fake_card(_ME, "hq", 1, "CHERBOURG", 0, 14, 0, 0),
            _fake_card(_OPP, "hq", 41, "ALEXANDRIA", 0, 19, 0, 0),
            _fake_card(_ME, "hand", 22, "NAVAL BATTLE", 0, 0, 7, 0),
            _fake_card(_ME, "hand", 37, "OVERCAST", 0, 0, 2, 1),
            _fake_card(_ME, "frontline", 13003, "BREWSTER F2A", 5, 2, 1, 1),
            _fake_card(_OPP, "back", 59, "FRONTIER FORCE", 2, 6, 3, 1),
            _fake_card(_OPP, "back", None, None, 0, 0, 0, 2,
                       hidden=True, reason="P4_covert"),
            _fake_card(_OPP, "hand", None, None, hidden=True, reason="P1_enemy_hand"),
            _fake_card(_OPP, "hand", 45, "104th INFANTRY REGIMENT", 1, 3, 2, 1,
                       reason="P4b_intel_seen"),
            _fake_card(_OPP, "deck", None, None, hidden=True, reason="P2_deck_remaining"),
            _fake_card(_OPP, "deck", None, None, hidden=True, reason="P2_deck_remaining"),
        ],
    }


def _sample(state, **kw):
    s = {"game": "g1", "patch": None, "seat": _ME, "turn": 7, "t": 1.0,
         "phase": "main", "state": state,
         "candidates": {"subject": [], "target": [], "option": []},
         "verdict": {}, "label": {"type": "end", "subject": None, "target": None,
                                  "option": None},
         "label_src": "matchlog", "label_available": True, "raw": [],
         "receipt": {}, "events": []}
    s.update(kw)
    return s


def selftest() -> int:
    table = CardTable.load()
    fails = 0

    def chk(name, ok):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    st = _fake_state()
    # 1) 可见性：牌库两张 + 未揭示敌手牌 1 张不进实体；隐蔽场上牌进（匿名）
    enc = encode_sample(_sample(st), table)
    chk("实体数 = 可见卡（HQ2+手2+场2+隐蔽1+intel1）", enc.n_entities == 8)
    zones = [ZONE_NAMES[z] for z in enc.ent_zone]
    chk("没有 deck 实体", "deck" not in zones)
    chk("敌手牌只留 intel 那张（名字=104th）",
        sum(1 for i in range(enc.n_entities)
            if ZONE_NAMES[enc.ent_zone[i]] == "hand"
            and int(enc.ent_side[i]) == SLOT_OTHER) == 1)
    chk("隐蔽单位匿名（unknown 词表 id=0）",
        any(enc.ent_fname[i] == 0 and ZONE_NAMES[enc.ent_zone[i]] == "back"
            and int(enc.ent_side[i]) == SLOT_OTHER for i in range(enc.n_entities)))
    try:
        assert_no_leak(enc)
        chk("干净样本过防泄漏断言", True)
    except AssertionError as e:
        chk("干净样本过防泄漏断言（%s）" % e, False)

    # 2) 反例（能失败）：把敌手牌伪装成可见（hidden=False）塞进去 -> 必须被抓
    dirty = _fake_state()
    dirty["cards"].append(_fake_card(_OPP, "hand", 99, "LEAK", 5, 5, 3, 3))
    enc_dirty = encode_sample(_sample(dirty), table)
    try:
        assert_no_leak(enc_dirty)
        chk("反例：未投影的敌手牌必须被抓", False)
    except AssertionError:
        chk("反例：未投影的敌手牌必须被抓", True)

    # 3) main/attack 的候选与标签解析
    atk = _sample(st, label={"type": "attack", "subject": 13003,
                             "target": 59, "option": None})
    ea = encode_sample(atk, table)
    chk("attack 主体候选=我方场上单位", ea.subj.shape[0] == 1)
    chk("attack 目标候选=敌方场上+总部", ea.targ.shape[0] == 3)
    chk("attack 标签指向正确（subject）", ea.subj_label == 0)
    chk("attack 标签指向正确（target=FRONTIER FORCE）",
        ea.targ_label >= 0 and ZONE_NAMES[ea.ent_zone[ea.targ[ea.targ_label]]] == "back")
    chk("attack 的 type_idx 正确", ea.type_idx == TYPE_ID["attack"])

    # 4) 标签在候选集里找不到 -> -1（不猜），并记原因
    bad = _sample(st, label={"type": "attack", "subject": 13003,
                             "target": 7777, "option": None})
    eb = encode_sample(bad, table)
    chk("解不出的 target 标 -1（绝不猜）", eb.targ_label == -1)
    chk("解不出时留下原因", any("target=7777" in x for x in eb.debug["unresolved"]))

    # 5) pick：选项来自 candidates.option（FName），标签按名字解析、去重
    pk = _sample(st, phase="pick",
                 candidates={"subject": [], "target": [],
                             "option": ["card_event_storm3_tropical_storm3",
                                        "card_event_rain1_mist",
                                        "card_event_storm3_tropical_storm3"]},
                 label={"type": "pick", "subject": None, "target": None,
                        "option": "card_event_rain1_mist"})
    ep = encode_sample(pk, table)
    chk("pick 选项去重后 2 个", ep.opt.shape[0] == 2)
    chk("pick 标签指向 mist", ep.opt_label == 1)
    chk("pick 的 type_idx 正确", ep.type_idx == TYPE_ID["pick"])

    # 6) hand_target：subject=发起卡当 context，target=手牌
    ht = _sample(st, phase="hand_target",
                 label={"type": "hand_target_selected", "subject": 22,
                        "target": 37, "option": None})
    eh = encode_sample(ht, table)
    chk("hand_target 目标的候选=我方手牌 2 张", eh.targ.shape[0] == 2)
    chk("hand_target 标签指向 OVERCAST", eh.targ_label == 1)
    chk("hand_target 的 context=发起卡 NAVAL BATTLE",
        eh.context >= 0 and eh.ent_fname[eh.context] != 0
        and eh.context in eh.subj.tolist())

    # 7) 全局向量：双方张数/指挥点/谁的座位
    gi = {n: i for i, (n, _) in enumerate(GLOBAL_LAYOUT)}
    chk("全局：指挥点 5/12", abs(float(enc.glob[gi["kredits_local"]]) - 5 / 12) < 1e-6)
    chk("全局：我方手牌 2/10", abs(float(enc.glob[gi["hand_local"]]) - 2 / 10) < 1e-6)
    chk("全局：actor_is_viewer=1", float(enc.glob[gi["actor_is_viewer"]]) == 1.0)
    chk("全局：HQ 已知位", float(enc.glob[gi["hq_known_local"]]) == 1.0)

    # 8) 对面座位样本（actor_is_viewer=0）
    other = _sample(st, seat=_OPP,
                    label={"type": "end", "subject": None, "target": None,
                           "option": None})
    eo = encode_sample(other, table)
    chk("对面座位：actor_is_viewer=0",
        float(eo.glob[gi["actor_is_viewer"]]) == 0.0)

    print("encode selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
