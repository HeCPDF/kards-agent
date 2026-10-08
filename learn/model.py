#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn.model —— set-based encoder + 阶段头/指针头/价值头（KARDS-NN.md §3.3、§4.2）。

    card_embed = id_embedding(FName) ⊕ MLP(静态特征)     ← §3.2
    tokens     = [全局 token] + 每个可见实体的 token
    encoder    = TransformerEncoder(4 层 / d=128 / 8 头)
    三个头     :
        type    头 = 全局 token 上的分类（每 phase 用 allowed mask 裁剪）
        subject 指针 = 全局 token 当 query，主体候选当 key（点积打分）
        target  指针 = 两阶段动作第二条用 context 实体当 query（§4.1）
        option  指针 = pick / mulligan / deck_pick 的候选（同一套指针头）
    价值头     = 全局 token -> 1 维（整局胜负，MC 回报；没有标签就 mask 掉）

★ §4.4 纪律：判据的 legal=-1 只给 **加性偏置 −8**，不是 −inf；unknown 不动，
  并且 legal 值本身当候选特征喂进 key（让网络学"判据什么时候不可信"）。

自检（含"过拟合一条真实决策 + 两个不同局面必须给出不同输出"）：
    cd kards-agent
    nn/venv/Scripts/python.exe -m nn.model
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .encode import (CAND_KIND_NAMES, GLOBAL_DIM, PHASE_NAMES, PHASE_TYPES,
                     TYPE_NAMES)
from .features import FEATURE_DIM

LEGAL_BIAS = -8.0     # §4.4


@dataclass
class ModelConfig:
    d: int = 128
    layers: int = 4
    heads: int = 8
    dropout: float = 0.1
    id_dim: int = 24
    card_hidden: int = 64
    draft: int = 0          # 预留：diffusion/draft 之类后续扩展，先不用
    vocab_size: int = 0     # 由训练脚本按 CardTable 填
    extra: dict = field(default_factory=dict)


class CardEncoder(nn.Module):
    """card_embed = Dropout(id_embedding) ⊕ MLP(静态特征)。"""

    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.id_embed = nn.Embedding(max(2, cfg.vocab_size), cfg.id_dim)
        self.id_drop = nn.Dropout(cfg.dropout)
        self.feat_mlp = nn.Sequential(
            nn.Linear(FEATURE_DIM, cfg.card_hidden), nn.ReLU(),
            nn.Linear(cfg.card_hidden, cfg.card_hidden), nn.ReLU(),
        )
        self.out_dim = cfg.id_dim + cfg.card_hidden

    def forward(self, fname_id, feat):
        return torch.cat([self.id_drop(self.id_embed(fname_id)),
                          self.feat_mlp(feat)], dim=-1)


class CandidateScorer(nn.Module):
    """指针头：query · key / sqrt(d) + 偏置（判据有罪 −8、候选类型一个 bias）。"""

    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.q = nn.Linear(cfg.d, cfg.d)
        self.k = nn.Linear(cfg.d, cfg.d)
        self.kind_bias = nn.Embedding(len(CAND_KIND_NAMES), 1)
        self.legal_scale = nn.Parameter(torch.tensor(0.0))
        self.scale = float(cfg.d) ** 0.5

    def forward(self, h_ent, query, cand, cand_kind, cand_legal, cand_mask):
        # h_ent [B,N,d] query [B,d] cand [B,K]（实体下标，-1=padding）
        safe = cand.clamp(min=0)
        keys = h_ent.gather(1, safe.unsqueeze(-1).expand(-1, -1, h_ent.shape[-1]))
        keys = self.k(keys)
        score = (self.q(query).unsqueeze(1) * keys).sum(-1) / self.scale
        score = score + self.kind_bias(cand_kind.clamp(min=0)).squeeze(-1)
        score = score + self.legal_scale * cand_legal
        score = score + LEGAL_BIAS * (cand_legal < 0).float()
        score = score.masked_fill(~cand_mask, -1e9)
        return score


class KardsNet(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.cfg = cfg
        self.card = CardEncoder(cfg)
        self.side_emb = nn.Embedding(3, 8)
        self.zone_emb = nn.Embedding(8, 8)
        self.num_proj = nn.Linear(8, 16)
        self.ent_proj = nn.Linear(self.card.out_dim + 32, cfg.d)
        self.glob_proj = nn.Linear(GLOBAL_DIM, cfg.d)
        self.cls = nn.Parameter(torch.zeros(1, 1, cfg.d))
        layer = nn.TransformerEncoderLayer(
            d_model=cfg.d, nhead=cfg.heads, dim_feedforward=cfg.d * 2,
            dropout=cfg.dropout, batch_first=True, norm_first=True,
            activation="gelu")
        self.encoder = nn.TransformerEncoder(layer, num_layers=cfg.layers,
                                             enable_nested_tensor=False)
        self.type_head = nn.Linear(cfg.d, len(TYPE_NAMES))
        self.scorer = CandidateScorer(cfg)
        self.value_head = nn.Linear(cfg.d, 1)
        # 每种 phase 允许的动作类型（结构性 mask，§4.4）
        mask = torch.zeros(len(PHASE_NAMES), len(TYPE_NAMES))
        for i, ph in enumerate(PHASE_NAMES):
            for tname in PHASE_TYPES[ph]:
                mask[i, TYPE_NAMES.index(tname)] = 1.0
        self.register_buffer("phase_type_mask", mask)

    def forward(self, batch: dict) -> dict:
        g = batch["glob"]
        ent = self.card(batch["ent_fname"], batch["ent_feat"])
        ent = torch.cat([ent,
                         self.side_emb(batch["ent_side"]),
                         self.zone_emb(batch["ent_zone"]),
                         self.num_proj(batch["ent_num"])], dim=-1)
        tokens = torch.cat([self.glob_proj(g).unsqueeze(1),
                            self.ent_proj(ent)], dim=1)
        B, N1 = tokens.shape[0], tokens.shape[1]
        keys_pad = torch.cat([
            torch.zeros(B, 1, dtype=torch.bool, device=tokens.device),
            ~batch["ent_mask"]], dim=1)
        h = self.encoder(tokens, src_key_padding_mask=keys_pad)
        h_glob, h_ent = h[:, 0], h[:, 1:]

        type_logits = self.type_head(h_glob)
        allowed = self.phase_type_mask[batch["phase"]].bool()
        type_logits = type_logits.masked_fill(~allowed, -1e9)

        ctx = batch["context"]
        B_, d_ = h_ent.shape[0], h_ent.shape[-1]
        ctx_ok = (ctx >= 0).view(B_, 1, 1)
        ctx_idx = ctx.clamp(min=0).view(B_, 1, 1).expand(B_, 1, d_)
        h_ctx = h_ent.gather(1, ctx_idx)
        ctx_h = torch.where(ctx_ok, h_ctx, h_glob.unsqueeze(1)).squeeze(1)
        out = {
            "type_logits": type_logits,
            "subject_scores": self.scorer(h_ent, h_glob, batch["subj"],
                                          batch["subj_kind"], batch["subj_legal"],
                                          batch["subj_mask"]),
            "target_scores": self.scorer(h_ent, ctx_h, batch["targ"],
                                         batch["targ_kind"], batch["targ_legal"],
                                         batch["targ_mask"]),
            "option_scores": self.scorer(h_ent, ctx_h, batch["opt"],
                                         batch["opt_kind"], batch["opt_legal"],
                                         batch["opt_mask"]),
            "value": torch.tanh(self.value_head(h_glob)).squeeze(-1),
        }
        # mulligan：每个候选一个二分类 logit（不复用指针头）
        safe = batch["opt"].clamp(min=0)
        keys = h_ent.gather(1, safe.unsqueeze(-1).expand(-1, -1, h_ent.shape[-1]))
        out["mulligan_logits"] = (self.scorer.k(keys)
                                  * self.scorer.q(h_glob).unsqueeze(1)
                                  ).sum(-1) / self.scorer.scale
        return out


def masked_ce(logits: torch.Tensor, label: torch.Tensor,
              mask: Optional[torch.Tensor] = None) -> Optional[torch.Tensor]:
    """只在有标签的样本上算 CE；全被 mask 就返回 None（调用方跳过）。"""
    ok = label >= 0
    if mask is not None:
        ok = ok & mask
    if not bool(ok.any()):
        return None
    return F.cross_entropy(logits[ok], label[ok].clamp(min=0))


def compute_losses(out: dict, batch: dict, type_weight=None) -> dict:
    """BC 多头 loss。每个头各自归一（谁有标签谁出力，互不淹没）。"""
    losses = {}
    main_ok = batch["phase"] == PHASE_NAMES.index("main")
    type_ok = (batch["type_label"] >= 0) & main_ok
    if bool(type_ok.any()):
        losses["type"] = F.cross_entropy(out["type_logits"][type_ok],
                                         batch["type_label"][type_ok],
                                         weight=type_weight)
    subj_mask = batch["subj_label"] >= 0
    if bool(subj_mask.any()):
        losses["subject"] = F.cross_entropy(
            out["subject_scores"][subj_mask],
            batch["subj_label"][subj_mask].clamp(min=0))
    targ_mask = batch["targ_label"] >= 0
    if bool(targ_mask.any()):
        losses["target"] = F.cross_entropy(
            out["target_scores"][targ_mask],
            batch["targ_label"][targ_mask].clamp(min=0))
    opt_mask = batch["opt_label"] >= 0
    if bool(opt_mask.any()):
        losses["option"] = F.cross_entropy(
            out["option_scores"][opt_mask],
            batch["opt_label"][opt_mask].clamp(min=0))
    if bool(batch["mull_mask"].any()):
        losses["mulligan"] = F.binary_cross_entropy_with_logits(
            out["mulligan_logits"][batch["mull_mask"]],
            batch["mull_label"][batch["mull_mask"]])
    val_ok = ~torch.isnan(batch["value"])
    if bool(val_ok.any()):
        losses["value"] = F.mse_loss(out["value"][val_ok], batch["value"][val_ok])
    return losses


LOSS_WEIGHTS = {"type": 1.0, "subject": 1.0, "target": 1.0,
                "option": 1.0, "mulligan": 1.0, "value": 0.5}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    from .dataset import to_torch_batch
    from .encode import _fake_card, _fake_state, _sample, encode_sample
    from .features import CardTable

    torch.manual_seed(0)
    table = CardTable.load()
    fails = 0

    def chk(name, ok):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    st = _fake_state()
    # 给攻击方再加一张我方场上单位：subject 头就有 2 个候选，loss 才不是恒 0
    st_rich = _fake_state()
    st_rich["cards"].append(_fake_card(1, "back", 9001, "15th ENGINEERS",
                                       5, 2, 5, 2))
    atk = encode_sample(_sample(st_rich, label={"type": "attack",
                                                "subject": 13003,
                                                "target": 59, "option": None}),
                        table)
    end = encode_sample(_sample(st, label={"type": "end", "subject": None,
                                           "target": None, "option": None}), table)
    pk = encode_sample(_sample(st, phase="pick",
                               candidates={"subject": [], "target": [],
                                           "option": ["card_event_overcast",
                                                      "card_event_naval_battle"]},
                               label={"type": "pick", "subject": None,
                                      "target": None,
                                      "option": "card_event_overcast"}), table)
    cfg = ModelConfig(d=32, layers=2, heads=4, dropout=0.0,
                      vocab_size=len(table.vocab))
    model = KardsNet(cfg)
    n_params = sum(p.numel() for p in model.parameters())
    print("  小模型参数量：%d（正式配置 d=128/4 层约 2~3M）" % n_params)

    batch = to_torch_batch([atk, end, pk])
    out = model(batch)
    chk("type_logits [B,8]", tuple(out["type_logits"].shape) == (3, len(TYPE_NAMES)))
    chk("subject/ target/ option 分数形状",
        out["subject_scores"].shape[0] == 3
        and out["target_scores"].shape[0] == 3
        and out["option_scores"].shape[0] == 3)
    chk("结构性 mask：end 样本的 play/attack/move 被挡住",
        float(out["type_logits"][2, TYPE_NAMES.index("attack")].detach()) < -1e8)
    chk("结构性 mask：main 允许的四类没被挡",
        float(out["type_logits"][1, TYPE_NAMES.index("end")].detach()) > -1e8
        and float(out["type_logits"][1, TYPE_NAMES.index("attack")].detach()) > -1e8)
    chk("结构性 mask：pick 相位只留 pick",
        float(out["type_logits"][2, TYPE_NAMES.index("pick")].detach()) > -1e8)

    losses = compute_losses(out, batch)
    chk("loss 头齐全（type/subject/target/option）",
        {"type", "subject", "target", "option"} <= set(losses))

    # 过拟合一条 attack 样本：loss 必须降、argmax 必须指到标签（能失败）
    model2 = KardsNet(cfg)
    opt = torch.optim.Adam(model2.parameters(), lr=3e-3)
    one = to_torch_batch([atk])
    losses0 = None
    for _ in range(120):
        opt.zero_grad()
        o = model2(one)
        ls = compute_losses(o, one)
        loss = sum(LOSS_WEIGHTS[k] * v for k, v in ls.items())
        loss.backward()
        opt.step()
        if losses0 is None:
            losses0 = {k: float(v.detach()) for k, v in ls.items()}
    o = model2(one)
    ls1 = {k: float(v.detach()) for k, v in compute_losses(o, one).items()}
    chk("过拟合单条：subject loss 下降（%.3f -> %.3f）"
        % (losses0["subject"], ls1["subject"]), ls1["subject"] < losses0["subject"])
    chk("过拟合单条：target loss 下降（%.3f -> %.3f）"
        % (losses0["target"], ls1["target"]), ls1["target"] < losses0["target"])
    chk("argmax：subject 指到攻击者",
        int(o["subject_scores"][0].argmax()) == atk.subj_label)
    chk("argmax：target 指到被攻击者",
        int(o["target_scores"][0].argmax()) == atk.targ_label)

    # 输入敏感性（§6：专抓"常数头"）：两个不同局面，输出必须变
    st2 = _fake_state()
    st2["kredits"][1] = 0           # 座位 1 = 本地玩家（自检约定）
    st2["cards"] = [c for c in st2["cards"] if c.get("card_id") != 13003]
    end2 = encode_sample(_sample(st2, label={"type": "end", "subject": None,
                                             "target": None, "option": None}), table)
    out2 = model2(to_torch_batch([end, end2]))
    d_type = float((out2["type_logits"][0] - out2["type_logits"][1]
                    ).abs().max().detach())
    d_glob = float((to_torch_batch([end, end2])["glob"][0]
                    - to_torch_batch([end, end2])["glob"][1]).abs().max())
    chk("输入敏感性：换一个局面 type logits 必须不同（%.4f）" % d_type, d_type > 1e-4)
    chk("（前置条件）两个局面的全局向量确实不同（%.4f）" % d_glob, d_glob > 1e-6)

    print("model selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
