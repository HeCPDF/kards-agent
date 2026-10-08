#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.eval —— 指标 + 三条基线 + 输入敏感性自检（KARDS-NN.md §6）。

★ 指标必须**能失败**（项目弯路 #11）：
    - 除 top-1 外同时报 top-3、macro-F1、按 phase 分组、按"有没有标签"分层；
    - 三条基线（always-end / random-legal / greedy-heuristic）用**同一套**打分代码；
    - `input_sensitivity()`：两个不同局面喂进去，argmax 必须变。恒定输出的
      "常数头"会让 identical_rate -> 1.0，直接判 FAIL —— 覆盖率类指标抓不到它。

★ 一个如实的口径说明：BC 训练是 teacher forcing —— subject/target 候选集按
   **真实动作类型**构造（main 里 play 的候选=手牌、attack 的候选=场上单位）。
  所以这里报的 subject/target top-1 是"**已知动作类型后**选对主体/目标"的准确率；
  在线回路要按类型头选出的类型各跑一次指针头（KARDS-NN.md §4.2）。

自检 / 用法：
    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.eval                      # 自检（合成数据）
    nn/venv/Scripts/python.exe -m nn.eval --ckpt <ckpt.pt>     # 评一个 checkpoint
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict

import numpy as np

from base import paths as _paths
from .baselines import all_baselines
from .dataset import (build_encoded, describe, load_records, split_games,
                      to_torch_batch, iterate_batches)
from .encode import PHASE_NAMES, TYPE_NAMES
from .features import CardTable


# ---------------------------------------------------------------------------
# 模型预测
# ---------------------------------------------------------------------------
def predict_model(model, samples: list, batch_size: int = 32, device=None) -> list:
    import torch

    model.eval()
    preds = []
    with torch.no_grad():
        for group in iterate_batches(samples, batch_size):
            batch = to_torch_batch(group, device=device)
            out = model(batch)
            for b, s in enumerate(group):
                scores = {}
                for head, key in (("subj", "subject_scores"),
                                  ("targ", "target_scores"),
                                  ("opt", "option_scores")):
                    sc = out[key][b].detach().float().cpu().numpy()
                    scores[head] = sc
                mull = out["mulligan_logits"][b].detach().float().cpu().numpy()
                tl = out["type_logits"][b].detach().float().cpu().numpy()
                t_idx = int(np.argmax(tl))
                pred = {
                    "type": TYPE_NAMES[t_idx],
                    "type_idx": t_idx,
                    "subj": int(np.argmax(scores["subj"])) if len(scores["subj"]) else -1,
                    "targ": int(np.argmax(scores["targ"])) if len(scores["targ"]) else -1,
                    "opt": int(np.argmax(scores["opt"])) if len(scores["opt"]) else -1,
                    "mull": (mull > 0).astype(bool) if len(mull) else None,
                    "scores": scores,
                }
                preds.append(pred)
    return preds


def predict_baseline(baseline, samples: list) -> list:
    return [baseline.predict(s) for s in samples]


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def _topk(scores, label: int, k: int) -> bool:
    if scores is None or label < 0 or len(scores) == 0:
        return False
    kk = min(k, len(scores))
    return label in np.argsort(-np.asarray(scores))[:kk].tolist()


def _macro_f1(y_true, y_pred, labels) -> float:
    f1s = []
    for c in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        if tp + fp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return float(np.mean(f1s)) if f1s else float("nan")


def score_predictions(samples: list, preds: list) -> dict:
    """同构打分：模型和三条基线都走这一个函数。"""
    m = defaultdict(Counter)
    y_true, y_pred = [], []
    for s, p in zip(samples, preds):
        ph = s.phase
        m[ph]["n"] += 1
        # ---- main：类型 + 主体 + 目标 + 端到端
        if ph in ("main", "hand_target", "deploy_target"):
            if s.type_idx >= 0:
                m["all"]["type_n"] += 1
                ok = (p.get("type") == TYPE_NAMES[s.type_idx])
                m["all"]["type_ok"] += 1 if ok else 0
                if ph == "main":
                    y_true.append(TYPE_NAMES[s.type_idx])
                    y_pred.append(p.get("type"))
            if s.subj_label >= 0:
                m["all"]["subj_n"] += 1
                if p.get("subj") == s.subj_label:
                    m["all"]["subj_ok"] += 1
                if _topk(p.get("scores", {}).get("subj"), s.subj_label, 3):
                    m["all"]["subj_top3"] += 1
            if s.targ_label >= 0:
                m["all"]["targ_n"] += 1
                if p.get("targ") == s.targ_label:
                    m["all"]["targ_ok"] += 1
                if _topk(p.get("scores", {}).get("targ"), s.targ_label, 3):
                    m["all"]["targ_top3"] += 1
            if s.opt_label >= 0:
                m["all"]["opt_n"] += 1
                if p.get("opt") == s.opt_label:
                    m["all"]["opt_ok"] += 1
                if _topk(p.get("scores", {}).get("opt"), s.opt_label, 3):
                    m["all"]["opt_top3"] += 1
            if ph == "main" and s.type_idx >= 0:
                full_ok = (p.get("type") == TYPE_NAMES[s.type_idx])
                if s.subj_label >= 0:
                    full_ok = full_ok and p.get("subj") == s.subj_label
                if s.targ_label >= 0:
                    full_ok = full_ok and p.get("targ") == s.targ_label
                m["all"]["full_n"] += 1
                m["all"]["full_ok"] += 1 if full_ok else 0
        if ph == "mulligan" and s.mull_discard is not None:
            known = ~np.isnan(s.mull_discard)
            if known.any():
                mp = p.get("mull")
                if mp is not None:
                    mp = np.asarray(mp)
                    k = min(len(mp), len(s.mull_discard))
                    ok = (mp[:k] == (s.mull_discard[:k] > 0.5)) & known[:k]
                    m["mulligan"]["n_known"] += int(known[:k].sum())
                    m["mulligan"]["ok"] += int(np.sum(ok))
                else:
                    m["mulligan"]["n_known"] += int(known.sum())

    out = {}
    a = m["all"]
    out["type_top1"] = a["type_ok"] / a["type_n"] if a["type_n"] else float("nan")
    out["type_n"] = a["type_n"]
    out["type_macro_f1"] = _macro_f1(y_true, y_pred, TYPE_NAMES) if y_true else float("nan")
    out["subject_top1"] = a["subj_ok"] / a["subj_n"] if a["subj_n"] else float("nan")
    out["subject_top3"] = a["subj_top3"] / a["subj_n"] if a["subj_n"] else float("nan")
    out["subject_n"] = a["subj_n"]
    out["target_top1"] = a["targ_ok"] / a["targ_n"] if a["targ_n"] else float("nan")
    out["target_top3"] = a["targ_top3"] / a["targ_n"] if a["targ_n"] else float("nan")
    out["target_n"] = a["targ_n"]
    out["option_top1"] = a["opt_ok"] / a["opt_n"] if a["opt_n"] else float("nan")
    out["option_top3"] = a["opt_top3"] / a["opt_n"] if a["opt_n"] else float("nan")
    out["option_n"] = a["opt_n"]
    out["full_top1"] = a["full_ok"] / a["full_n"] if a["full_n"] else float("nan")
    out["full_n"] = a["full_n"]
    mu = m["mulligan"]
    out["mulligan_acc"] = mu["ok"] / mu["n_known"] if mu["n_known"] else float("nan")
    out["mulligan_n"] = mu["n_known"]
    out["counts"] = {ph: dict(v) for ph, v in m.items() if ph != "all"}
    return out


# ---------------------------------------------------------------------------
# 输入敏感性（专抓"常数头"）
# ---------------------------------------------------------------------------
def input_sensitivity(model, samples: list, k_pairs: int = 64, seed: int = 0,
                      batch_size: int = 32, device=None) -> dict:
    """同一 phase 里抽两两不同局面的样本对，看模型的主头预测是否总是一样。

    常数头 -> identical_rate ≈ 1.0（FAIL）；真会看盘面的模型应明显 < 1.0。
    """
    import random as _r
    import torch

    groups = defaultdict(list)
    for s in samples:
        groups[s.phase].append(s)
    pairs = []
    rng = _r.Random(seed)
    for ph, ss in groups.items():
        if len(ss) < 2:
            continue
        for _ in range(k_pairs):
            a, b = rng.sample(ss, 2)
            if a.state_hash and a.state_hash == b.state_hash:
                continue
            pairs.append((ph, a, b))
    if not pairs:
        return {"n_pairs": 0, "identical_rate": float("nan"),
                "per_phase": {}, "note": "没有足够的成对样本（同一局面去重后）"}

    model.eval()
    same, per = 0, defaultdict(Counter)
    flat = [x for pair in pairs for x in pair[1:]]
    preds = predict_model(model, flat, batch_size=batch_size, device=device)
    for i, (ph, a, b) in enumerate(pairs):
        pa, pb = preds[2 * i], preds[2 * i + 1]
        if ph == "main":
            key = ("type",)
        elif ph in ("pick", "deck_pick"):
            key = ("opt",)
        else:
            key = ("targ",)
        eq = all(pa[k] == pb[k] for k in key)
        same += 1 if eq else 0
        per[ph]["n"] += 1
        per[ph]["same"] += 1 if eq else 0
    return {"n_pairs": len(pairs), "identical_rate": same / len(pairs),
            "per_phase": {k: v["same"] / v["n"] for k, v in per.items()}}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _fmt(x):
    return "  --  " if x is None or (isinstance(x, float) and np.isnan(x)) \
        else "%.3f" % x


def report(samples: list, model=None, include_baselines: bool = True,
           device=None) -> str:
    lines = []
    rows = []
    if model is not None:
        rows.append(("nn", score_predictions(samples, predict_model(model, samples,
                                                                   device=device))))
        rows.append(("nn.input-sensitivity",
                     {"_special": input_sensitivity(model, samples, device=device)}))
    if include_baselines:
        for b in all_baselines(seed=0):
            rows.append((b.name, score_predictions(samples, predict_baseline(b, samples))))
    head = ("%-22s %7s %7s %7s %7s %7s %7s %7s"
            % ("", "type@1", "macroF1", "subj@1", "subj@3", "targ@1", "opt@1", "full@1"))
    lines.append(head)
    for name, m in rows:
        if "_special" in m:
            sp = m["_special"]
            lines.append("%-22s 输入敏感性 identical_rate=%s（%d 对；常数头=1.0）"
                         % (name, _fmt(sp.get("identical_rate")), sp.get("n_pairs", 0)))
            continue
        lines.append("%-22s %7s %7s %7s %7s %7s %7s %7s"
                     % (name, _fmt(m["type_top1"]), _fmt(m["type_macro_f1"]),
                        _fmt(m["subject_top1"]), _fmt(m["subject_top3"]),
                        _fmt(m["target_top1"]), _fmt(m["option_top1"]),
                        _fmt(m["full_top1"])))
    counts = score_predictions(samples, [{} for _ in samples])["counts"]
    lines.append("  标签覆盖：type=%d subject=%d target=%d option=%d full=%d "
                 "mulligan=%d（分母；没标签的样本不进对应指标）"
                 % (rows[0][1]["type_n"] if rows else 0,
                    rows[0][1]["subject_n"] if rows else 0,
                    rows[0][1]["target_n"] if rows else 0,
                    rows[0][1]["option_n"] if rows else 0,
                    rows[0][1]["full_n"] if rows else 0,
                    rows[0][1]["mulligan_n"] if rows else 0))
    lines.append("  按 phase 计数：%s" % dict(counts))
    return "\n".join(lines)


def load_checkpoint(path, device=None):
    import torch

    ckpt = torch.load(path, map_location=device or "cpu", weights_only=False)
    return ckpt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NN 评估（含三条基线）")
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--random-init", action="store_true",
                    help="不加载权重，只验证评估管线（指标必然很差）")
    ap.add_argument("--data", default=None)
    ap.add_argument("--split", default=None, choices=[None, "time", "random", "deck"])
    ap.add_argument("--val-frac", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--keep-fallback", action="store_true",
                    help="保留 state_before=fallback_post 的样本（默认丢弃，与训练一致）")
    a = ap.parse_args(argv)

    from .model import KardsNet, ModelConfig

    ckpt = load_checkpoint(a.ckpt) if a.ckpt else {}
    table = CardTable.load()
    samples = build_encoded(
        load_records(a.data or ckpt.get("data_dir") or None or
                     _paths.RECORD_DIR),
        table, drop_fallback=not a.keep_fallback)
    split = ckpt.get("split") or {}
    mode = a.split or split.get("mode", "time")
    val_frac = a.val_frac if a.val_frac is not None else split.get("val_frac", 0.2)
    seed = a.seed if a.seed is not None else split.get("seed", 0)
    _, val, info = split_games(samples, mode=mode, val_frac=val_frac, seed=seed)
    print("切分：", {k: v for k, v in info.items() if k != "games"})
    if not val:
        print("val 为空 —— 全部当 val 用（只当冒烟，不当指标）")
        val = samples
    print(describe(val))

    if a.ckpt:
        cfg = ModelConfig(**{k: v for k, v in ckpt["config"].items()
                             if k in ModelConfig.__dataclass_fields__})
        model = KardsNet(cfg)
        model.load_state_dict(ckpt["state_dict"])
    elif a.random_init:
        model = KardsNet(ModelConfig(vocab_size=len(table.vocab)))
    else:
        print("给 --ckpt 或 --random-init")
        return 2
    print(report(val, model=model, include_baselines=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
