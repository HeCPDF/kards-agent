#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.dataset —— 录制数据加载 / 切分 / 组 batch（KARDS-NN.md §1.3、§5.3）。

数据 = `D:\\Kards\\kards-data\\recordings\\*.jsonl.gz`（仓库外，避免账号信息
进公开库发布纪律）。一行 = 一个决策点；本模块把它编码成 `EncodedSample`，
再按需切成 torch batch。

★ 切分纪律（§5.3，最容易自欺的一步）：
    - `time`：按局的**首条样本时间**排序，最后 val_frac 局做验证（贴近部署）。
    - `random`：按局随机抽（不是按样本随机抽！相邻决策点几乎同一个局面）。
    - `deck`：本仓库当前**没有** per-game 卡组头信息（header 还没落盘）⇒
      退化成 `random` 并在 info 里如实标注，绝不假装做了 leave-one-deck-out。

自检：
    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.dataset
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .encode import (ENT_NUM_DIM, GLOBAL_DIM, PHASE_NAMES, EncodedSample,
                     TYPE_NAMES, encode_sample)
from .features import FEATURE_DIM, CardTable

from base import paths as _paths_d
DEFAULT_DATA_DIR = Path(_paths_d.RECORD_DIR)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def iter_record_files(data=DEFAULT_DATA_DIR) -> list:
    p = Path(data)
    if p.is_file():
        return [p]
    files = sorted(glob.glob(str(p / "*.jsonl.gz"))) + \
        sorted(glob.glob(str(p / "*.jsonl")))
    return [Path(f) for f in files]


def load_records(data=DEFAULT_DATA_DIR, verbose: bool = False) -> list:
    """读 JSONL / JSONL.GZ。容忍被强杀的录制器留下的半截 gzip 尾
    （`EOFError`：已经 flush 过的行照样有效，见 player/record.py 的注释）。"""
    out, bad = [], 0
    for f in iter_record_files(data):
        opener = gzip.open if f.suffix == ".gz" else open
        kw = {"encoding": "utf-8"} if f.suffix != ".gz" else {"encoding": "utf-8"}
        n0 = len(out)
        try:
            with opener(f, "rt", **kw) as fh:
                try:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            bad += 1
                            continue
                        if isinstance(row, dict) and row.get("state") \
                                and row.get("kind") != "header":
                            out.append(row)
                except EOFError:
                    pass
        except OSError as e:
            if verbose:
                print("  [跳过] %s: %s" % (f.name, e))
            continue
        if verbose:
            print("  %-42s %5d 条" % (f.name, len(out) - n0))
    if bad and verbose:
        print("  （%d 行 JSON 解析失败，已跳过）" % bad)
    return out


def build_encoded(samples: Iterable[dict], table: Optional[CardTable] = None,
                  verbose: bool = False, drop_fallback: bool = False) -> list:
    """编码。`drop_fallback=True` 时丢掉录制器标记为 `fallback_post` 的样本
    （那些样本的 state_before 是动作之后的盘面，训练信号是坏的；默认保留是
    为了兼容 2026-09-28 之前没有这个标记的老数据）。"""
    table = table or CardTable.load()
    out = []
    skipped = Counter()
    for s in samples:
        enc = encode_sample(s, table)
        if enc is None:
            skipped["no_state"] += 1
            continue
        if drop_fallback and enc.debug.get("state_before_src") == "fallback_post":
            skipped["state_before_fallback"] += 1
            continue
        if enc.n_entities == 0:
            skipped["no_entities"] += 1
        out.append(enc)
    if verbose and skipped:
        print("  编码跳过：", dict(skipped))
    return out


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------
def split_games(samples: list, mode: str = "time", val_frac: float = 0.2,
                seed: int = 0) -> tuple:
    """返回 (train, val, info)。切分的单位是**局**，不是样本。"""
    by_game = defaultdict(list)
    for s in samples:
        by_game[s.game].append(s)
    games = sorted(by_game, key=lambda g: min(x.t for x in by_game[g]))
    info = {"mode": mode, "n_games": len(games), "games": games}
    if len(games) <= 1:
        info["note"] = "只有 %d 局：全部进 train，val 为空（指标不可读，只当管线冒烟）" % len(games)
        return list(samples), [], info
    n_val = max(1, int(round(len(games) * val_frac)))
    if mode == "time":
        val_games = set(games[-n_val:])
    elif mode in ("random", "deck"):
        rng = random.Random(seed)
        val_games = set(rng.sample(games, n_val))
        if mode == "deck":
            info["note"] = ("当前数据没有 per-game 卡组头信息 ⇒ 按局随机切，"
                            "不是 leave-one-deck-out（不许当卡组泛化指标用）")
    else:
        raise ValueError("unknown split mode: %r" % mode)
    train = [s for s in samples if s.game not in val_games]
    val = [s for s in samples if s.game in val_games]
    info["val_games"] = sorted(val_games)
    return train, val, info


# ---------------------------------------------------------------------------
# 覆盖率 / 标签质量报告（录制数据能不能训，一眼看这里）
# ---------------------------------------------------------------------------
def coverage(samples: list) -> dict:
    per = defaultdict(lambda: Counter())
    reasons = Counter()
    for s in samples:
        key = "%s/%s" % (s.phase, s.label_src or "?")
        c = per[key]
        c["n"] += 1
        c["label_available"] += 1 if s.label_available else 0
        if s.type_idx >= 0:
            c["type"] += 1
        if s.subj.shape[0]:
            c["subj_cand"] += 1
        if s.subj_label >= 0:
            c["subj_label"] += 1
        if s.targ.shape[0]:
            c["targ_cand"] += 1
        if s.targ_label >= 0:
            c["targ_label"] += 1
        if s.opt.shape[0]:
            c["opt_cand"] += 1
        if s.opt_label >= 0:
            c["opt_label"] += 1
        if s.mull_discard is not None and len(s.mull_discard):
            c["mull_cand"] += len(s.mull_discard)
            c["mull_known"] += int(np.sum(~np.isnan(s.mull_discard)))
        for r in s.debug.get("unresolved", []):
            reasons[r.split("=")[0]] += 1
        if s.debug.get("state_before_src") == "fallback_post":
            reasons["state_before=fallback_post"] += 1
    return {"per_phase_src": {k: dict(v) for k, v in sorted(per.items())},
            "unresolved_reasons": dict(reasons)}


def describe(samples: list) -> str:
    cov = coverage(samples)
    lines = ["  样本 %d 条（%d 局）" % (len(samples),
                                     len({s.game for s in samples}))]
    for k, v in cov["per_phase_src"].items():
        lines.append(
            "    %-28s n=%-4d type=%-4d subject=%d/%d target=%d/%d option=%d/%d"
            % (k, v.get("n", 0), v.get("type", 0),
               v.get("subj_label", 0), v.get("subj_cand", 0),
               v.get("targ_label", 0), v.get("targ_cand", 0),
               v.get("opt_label", 0), v.get("opt_cand", 0)))
        if v.get("mull_cand"):
            lines.append("      └ mulligan 逐张标注 %d/%d 可读"
                         % (v.get("mull_known", 0), v.get("mull_cand", 0)))
    if cov["unresolved_reasons"]:
        lines.append("    标签解不出（不进 loss，绝不猜）：%s"
                     % cov["unresolved_reasons"])
    return "\n".join(lines)


def main_type_counts(samples: list) -> Counter:
    c = Counter()
    for s in samples:
        if s.phase == "main" and 0 <= s.type_idx < len(TYPE_NAMES):
            c[TYPE_NAMES[s.type_idx]] += 1
    return c


# ---------------------------------------------------------------------------
# torch batch
# ---------------------------------------------------------------------------
def to_torch_batch(batch: list, device=None) -> dict:
    """把若干 `EncodedSample` 补成定长张量。真实调用前不 import torch。"""
    import torch

    B = len(batch)
    N = max(1, max(s.n_entities for s in batch))
    F = FEATURE_DIM
    G = GLOBAL_DIM
    Ks = max(1, max(len(s.subj) for s in batch))
    Kt = max(1, max(len(s.targ) for s in batch))
    Ko = max(1, max(len(s.opt) for s in batch))

    ent_fname = np.zeros((B, N), dtype=np.int64)
    ent_feat = np.zeros((B, N, F), dtype=np.float32)
    ent_side = np.zeros((B, N), dtype=np.int64)
    ent_zone = np.zeros((B, N), dtype=np.int64)
    ent_num = np.zeros((B, N, ENT_NUM_DIM), dtype=np.float32)
    ent_mask = np.zeros((B, N), dtype=bool)
    glob = np.zeros((B, G), dtype=np.float32)

    def pad_cand(attr, K):
        idx = np.full((B, K), -1, dtype=np.int64)
        kind = np.zeros((B, K), dtype=np.int64)
        legal = np.zeros((B, K), dtype=np.float32)
        mask = np.zeros((B, K), dtype=bool)
        for b, s in enumerate(batch):
            a = getattr(s, attr)
            k = len(a)
            if k:
                idx[b, :k] = a
                kind[b, :k] = getattr(s, attr + "_kind")
                legal[b, :k] = getattr(s, attr + "_legal")
                mask[b, :k] = True
        return idx, kind, legal, mask

    subj, subj_kind, subj_legal, subj_mask = pad_cand("subj", Ks)
    targ, targ_kind, targ_legal, targ_mask = pad_cand("targ", Kt)
    opt, opt_kind, opt_legal, opt_mask = pad_cand("opt", Ko)

    type_label = np.full(B, -1, dtype=np.int64)
    subj_label = np.full(B, -1, dtype=np.int64)
    targ_label = np.full(B, -1, dtype=np.int64)
    opt_label = np.full(B, -1, dtype=np.int64)
    context = np.full(B, -1, dtype=np.int64)
    value = np.full(B, np.nan, dtype=np.float32)
    phase = np.zeros(B, dtype=np.int64)
    mull_label = np.full((B, Ko), np.nan, dtype=np.float32)
    mull_mask = np.zeros((B, Ko), dtype=bool)

    for b, s in enumerate(batch):
        n = s.n_entities
        ent_fname[b, :n] = s.ent_fname
        ent_feat[b, :n] = s.ent_feat
        ent_side[b, :n] = s.ent_side
        ent_zone[b, :n] = s.ent_zone
        ent_num[b, :n] = s.ent_num
        ent_mask[b, :n] = True
        glob[b] = s.glob
        type_label[b] = s.type_idx
        subj_label[b] = s.subj_label
        targ_label[b] = s.targ_label
        opt_label[b] = s.opt_label
        context[b] = s.context
        value[b] = s.value
        phase[b] = PHASE_NAMES.index(s.phase) if s.phase in PHASE_NAMES else 0
        if s.mull_discard is not None and len(s.mull_discard):
            k = min(len(s.mull_discard), Ko)
            mull_label[b, :k] = s.mull_discard[:k]
            mull_mask[b, :k] = ~np.isnan(s.mull_discard[:k])

    t = lambda a, dtype=None: torch.as_tensor(a, dtype=dtype, device=device)
    out = {
        "ent_fname": t(ent_fname, torch.long),
        "ent_feat": t(ent_feat, torch.float32),
        "ent_side": t(ent_side, torch.long),
        "ent_zone": t(ent_zone, torch.long),
        "ent_num": t(ent_num, torch.float32),
        "ent_mask": t(ent_mask, torch.bool),
        "glob": t(glob, torch.float32),
        "subj": t(subj, torch.long), "subj_kind": t(subj_kind, torch.long),
        "subj_legal": t(subj_legal, torch.float32), "subj_mask": t(subj_mask, torch.bool),
        "targ": t(targ, torch.long), "targ_kind": t(targ_kind, torch.long),
        "targ_legal": t(targ_legal, torch.float32), "targ_mask": t(targ_mask, torch.bool),
        "opt": t(opt, torch.long), "opt_kind": t(opt_kind, torch.long),
        "opt_legal": t(opt_legal, torch.float32), "opt_mask": t(opt_mask, torch.bool),
        "type_label": t(type_label, torch.long),
        "subj_label": t(subj_label, torch.long),
        "targ_label": t(targ_label, torch.long),
        "opt_label": t(opt_label, torch.long),
        "context": t(context, torch.long),
        "value": t(value, torch.float32),
        "phase": t(phase, torch.long),
        "mull_label": t(mull_label, torch.float32),
        "mull_mask": t(mull_mask, torch.bool),
    }
    out["meta"] = [{"game": s.game, "phase": s.phase, "seat": s.seat,
                    "viewer": s.viewer, "state_hash": s.state_hash} for s in batch]
    return out


def iterate_batches(samples: list, batch_size: int, shuffle: bool = False,
                    seed: int = 0):
    idx = list(range(len(samples)))
    if shuffle:
        random.Random(seed).shuffle(idx)
    for i in range(0, len(idx), batch_size):
        yield [samples[j] for j in idx[i:i + batch_size]]


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    from .encode import _fake_state, _sample

    fails = 0

    def chk(name, ok):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    table = CardTable.load()
    st = _fake_state()
    syn = [
        encode_sample(_sample(st, game="g1", t=1.0,
                              label={"type": "end", "subject": None,
                                     "target": None, "option": None}), table),
        encode_sample(_sample(st, game="g2", t=2.0,
                              label={"type": "attack", "subject": 13003,
                                     "target": 59, "option": None}), table),
        encode_sample(_sample(st, game="g3", t=3.0, phase="pick",
                              candidates={"subject": [], "target": [],
                                          "option": ["card_event_overcast"]},
                              label={"type": "pick", "subject": None,
                                     "target": None,
                                     "option": "card_event_overcast"}), table),
    ]
    chk("编码出 3 条合成样本", all(x is not None for x in syn))

    tr, va, info = split_games(syn, mode="time", val_frac=0.34)
    chk("按时间切分：val 取最后一局", {s.game for s in va} == {"g3"})
    chk("按时间切分：train 是前两局", {s.game for s in tr} == {"g1", "g2"})
    tr2, va2, info2 = split_games(syn, mode="random", val_frac=0.34, seed=0)
    chk("按局随机切分不拆局",
        not ({s.game for s in tr2} & {s.game for s in va2}))

    batch = to_torch_batch(syn[:2])
    chk("batch 形状：ent_num [B,N,8]",
        tuple(batch["ent_num"].shape) == (2, max(s.n_entities for s in syn[:2]),
                                          ENT_NUM_DIM))
    chk("batch 形状：glob [B,18]", tuple(batch["glob"].shape) == (2, GLOBAL_DIM))
    chk("padding 的候选带 mask（空候选也占 1 列）",
        batch["subj_mask"].shape[1] == 1
        and int(batch["subj_mask"][0].sum()) == 0)
    chk("attack 样本的 target 标签进 batch",
        int(batch["targ_label"][1]) >= 0)

    cov = coverage(syn)
    chk("覆盖率报告能跑", "per_phase_src" in cov and cov["unresolved_reasons"] == {})
    chk("main 类型计数", main_type_counts(syn)["end"] == 1)

    # 负例（能失败）：没有标签的样本，type/subject/target 全是 -1
    nb = encode_sample(_sample(st, label={"type": "play", "subject": None,
                                          "target": None, "option": None}), table)
    chk("反例：subject 为空时标签必须 -1（不是 0）", nb.subj_label == -1)

    # 真实数据（有就跑一遍统计，不改断言）
    real = load_records(verbose=False)
    if real:
        enc = build_encoded(real, table)
        print(describe(enc))
        print("  真实数据标注基线的类型分布：", dict(main_type_counts(enc)))

    print("dataset selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
