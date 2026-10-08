#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.train_bc —— 第一阶段行为克隆（KARDS-NN.md §5、§9-P2）。

    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.train_bc \
        --data D:\\Kards\\kards-data\\recordings \
        --out  D:\\Kards\\kards-data\\nn\\runs\\bc-0001 \
        --split time --val-frac 0.25 --epochs 30

规则（照规格抄，不自己发挥）：
    * 只在**有标签**的样本上算 loss（`label_available=false` / 解析不出的候选
      一律不进监督 —— 绝不猜）；
    * type 头按类别频率加权（"永远 end"能在总 accuracy 上混过去）；
    * 指标按 phase 报，且必须带三条基线（`nn.eval`）；
    * 权重落到**仓库外**（`kards-data/nn/runs/`），不随 kards-agent 发布。

当前数据的已知短板（跑起来会直接看到）：
    * 录制器 state_before 对齐有问题（攻击目标常常已经进了弃牌堆）⇒ 主体/目标
      标签可解析比例很低，type 头之外的头监督很少；
    * 没有整局胜负 header ⇒ 价值头暂时没有监督（会自动跳过）。
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from base import paths as _paths
from .dataset import (build_encoded, coverage, describe, iterate_batches,
                      load_records, main_type_counts, split_games, to_torch_batch)
from .encode import TYPE_NAMES
from .eval import predict_model, report, score_predictions
from .features import CardTable
from .model import LOSS_WEIGHTS, KardsNet, ModelConfig, compute_losses


def type_class_weight(samples: list, n_types: int) -> torch.Tensor:
    counts = main_type_counts(samples)
    from .encode import TYPE_NAMES
    w = torch.ones(n_types)
    total = sum(counts.values()) or 1
    for name, c in counts.items():
        i = TYPE_NAMES.index(name)
        w[i] = total / (len(counts) * c) if c else 1.0
    return w


def run_epoch(model, samples, batch_size, opt=None, type_weight=None,
              seed=0, device=None) -> dict:
    train = opt is not None
    model.train(train)
    sums, counts = {}, {}
    for group in iterate_batches(samples, batch_size, shuffle=train, seed=seed):
        batch = to_torch_batch(group, device=device)
        with torch.set_grad_enabled(train):
            out = model(batch)
            losses = compute_losses(out, batch, type_weight=type_weight)
            if not losses:
                continue
            total = sum(LOSS_WEIGHTS.get(k, 1.0) * v for k, v in losses.items())
            if train:
                opt.zero_grad()
                total.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                opt.step()
        for k, v in losses.items():
            sums[k] = sums.get(k, 0.0) + float(v.detach())
            counts[k] = counts.get(k, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BC 训练（P2）")
    ap.add_argument("--data", default=_paths.RECORD_DIR)
    ap.add_argument("--out", default=None,
                    help="默认 D:\\Kards\\kards-data\\nn\\runs\\bc-<时间戳>")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--split", default="time", choices=["time", "random", "deck"])
    ap.add_argument("--val-frac", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--d", type=int, default=128)
    ap.add_argument("--layers", type=int, default=4)
    ap.add_argument("--heads", type=int, default=8)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--limit", type=int, default=0, help="只用前 N 条（调试）")
    ap.add_argument("--keep-fallback", action="store_true",
                    help="保留 state_before=fallback_post（动作之后的快照）的样本；"
                         "默认丢弃")
    ap.add_argument("--cpu", action="store_true")
    a = ap.parse_args(argv)

    torch.manual_seed(a.seed)
    np.random.seed(a.seed)
    device = "cpu" if a.cpu or not torch.cuda.is_available() else "cuda"
    table = CardTable.load()
    raw = load_records(a.data, verbose=True)
    if not raw:
        print("没有数据：%s" % a.data)
        return 2
    samples = build_encoded(raw, table, verbose=True,
                            drop_fallback=not a.keep_fallback)
    if a.limit:
        samples = samples[:a.limit]
    train, val, info = split_games(samples, mode=a.split, val_frac=a.val_frac,
                                   seed=a.seed)
    print("切分：", {k: v for k, v in info.items() if k != "games"})
    print("训练集：")
    print(describe(train))
    if val:
        print("验证集：")
        print(describe(val))
    else:
        print("验证集为空（数据太少）—— 只训练，指标看训练集，别当泛化。")

    cfg = ModelConfig(d=a.d, layers=a.layers, heads=a.heads, dropout=a.dropout,
                      vocab_size=len(table.vocab))
    model = KardsNet(cfg).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, a.epochs))
    tw = type_class_weight(train, len(TYPE_NAMES))
    tw = tw.to(device)
    print("type 类别权重：",
          {k: round(float(v), 2) for k, v in zip(
              ("play", "attack", "move", "end", "pick", "hand_target",
               "mulligan", "choose_target"), tw)})

    out_dir = Path(a.out) if a.out else (
        Path(_paths.DATA) / "nn" / "runs" /
        time.strftime("bc-%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    history = []
    best = {"score": -1.0, "epoch": -1, "state": None}
    eval_set = val if val else train
    for ep in range(1, a.epochs + 1):
        tr = run_epoch(model, train, a.batch, opt=opt, type_weight=tw,
                       seed=a.seed + ep, device=device)
        va = run_epoch(model, eval_set, a.batch, opt=None, type_weight=tw,
                       device=device)
        m = score_predictions(eval_set, predict_model(model, eval_set,
                                                      device=device))
        # "能不能学出东西"的单标量：有标签的头平均 top-1（没有的跳过）
        vals = [m[k] for k in ("type_top1", "subject_top1", "target_top1", "option_top1")
                if not np.isnan(m[k])]
        score = float(np.mean(vals)) if vals else -1.0
        history.append({"epoch": ep, "train": tr, "val": va, "score": score,
                        "type_top1": m["type_top1"], "subject_top1": m["subject_top1"],
                        "target_top1": m["target_top1"], "option_top1": m["option_top1"]})
        if score > best["score"]:
            best = {"score": score, "epoch": ep,
                    "state": {k: v.detach().cpu().clone()
                              for k, v in model.state_dict().items()}}
        print("epoch %3d  train=%s  val=%s  score=%.3f (best %.3f @%d)"
              % (ep, {k: round(v, 3) for k, v in tr.items()},
                 {k: round(v, 3) for k, v in va.items()}, score, best["score"],
                 best["epoch"]))
        sched.step()

    if best["state"] is not None:
        model.load_state_dict(best["state"])
    ckpt = {
        "config": asdict(cfg), "state_dict": model.state_dict(),
        "split": {"mode": a.split, "val_frac": a.val_frac, "seed": a.seed},
        "data_dir": a.data, "games": sorted({s.game for s in samples}),
        "table": table.summary(), "history": history, "best_epoch": best["epoch"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "coverage": coverage(samples),
    }
    torch.save(ckpt, out_dir / "ckpt.pt")
    (out_dir / "history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\ncheckpoint -> %s（best epoch %d）" % (out_dir / "ckpt.pt", best["epoch"]))
    print("\n=== 验证集最终报告（模型 vs 三条基线）===")
    print(report(eval_set, model=model, include_baselines=True, device=device))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
