#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小接线 demo —— 不是训练管线，只证明一件事：

    FName embedding（卡的稳定身份键）⊕ 静态特征 → 指针头，
    能不能从**一条真实决策**里学出"该点哪个候选"。

背景（见 `reverse-data/reports/spec/KARDS-NN.md` §3.2/§4.2）：
    * 卡的身份键用 FName（`card_unit_175th_infantry_regiment` 这种），不用
      运行时 CardID/卡组代码——原因见同一份规格 §3.2 的"实例/对局/卡型三级"说明。
    * `card_embed = MLP(静态特征) ⊕ Dropout(id_embedding)`。
    * 动作头是"指针网络"：对候选 embedding 做点积打分，不是固定分类头。

这里唯一的真实数据是本轮会话实机测过的一次"选手牌"决策（§7.6 附近记录过）：
    发起卡 = 175th INFANTRY REGIMENT（部署：选1张手牌放回牌组顶）
    候选   = 当时手牌 5 张：WE CAN DO IT! / OVERCAST / DEATH FROM ABOVE /
             FIFTH OHIO / 17th INFANTRY BRIGADE
    人的选择 = WE CAN DO IT!
    （FName/cost/atk/def/threat_level 是这轮从 kardsmem 实机读出来的真实值，
      不是编的；card_id 是运行时槽位号，这里不用，只留 FName。）

只有 1 条样本，"训练"只是过拟合它，用来验证：
    1. FName → embedding 这条查表链路能跑通；
    2. 静态特征 ⊕ id embedding 拼出的 card_embed 能被指针头正确区分；
    3. loss 真的会下降、argmax 真的会指向标签——不是摆设。

跑法（这台机器上 torch 装在复制过来的 venv 里，不装进 kards-agent 的主环境）：
    nn/venv/Scripts/python.exe nn/demo_pointer_wiring.py
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# 1. 真实数据（2026-09-24 实机读出，见 kards-agent/CLAUDE.md 当天记录 / 会话记录）
# ---------------------------------------------------------------------------
CARDS = {
    # fname                                : (cost, atk, dfn, threat_level)
    "card_unit_175th_infantry_regiment":     (3, 6, 5, 0.0),   # 发起卡（instigator）
    "card_event_we_can_do_it":               (5, 0, 0, 0.0),   # ← 人选的这张
    "card_event_overcast":                   (2, 0, 0, 0.0),
    "card_event_death_from_above":           (4, 0, 0, 0.0),
    "card_unit_fifth_ohio":                  (3, 4, 4, 0.0),
    "card_unit_17th_infantry_brigade":       (2, 3, 2, 0.0),
}
INSTIGATOR = "card_unit_175th_infantry_regiment"
CANDIDATES = ["card_event_we_can_do_it", "card_event_overcast",
              "card_event_death_from_above", "card_unit_fifth_ohio",
              "card_unit_17th_infantry_brigade"]
LABEL_FNAME = "card_event_we_can_do_it"   # 人类真实选择

FNAME_VOCAB = {f: i for i, f in enumerate(CARDS)}   # ★ 词表 key = FName，不是 CardID/卡组代码


# ---------------------------------------------------------------------------
# 2. 模型：card_embed = MLP(静态特征) ⊕ Dropout(id_embedding)
# ---------------------------------------------------------------------------
class CardEncoder(nn.Module):
    def __init__(self, vocab_size, id_dim=8, feat_dim=4, hidden=16, out_dim=16,
                 id_dropout=0.2):
        super().__init__()
        self.id_embed = nn.Embedding(vocab_size, id_dim)
        self.id_drop = nn.Dropout(id_dropout)
        self.feat_mlp = nn.Sequential(
            nn.Linear(feat_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, out_dim - id_dim),
        )

    def forward(self, ids, feats):
        # ids: (N,) long   feats: (N, feat_dim) float
        id_vec = self.id_drop(self.id_embed(ids))
        feat_vec = self.feat_mlp(feats)
        return torch.cat([id_vec, feat_vec], dim=-1)   # (N, out_dim)


class PointerHead(nn.Module):
    """对候选 embedding 做点积打分——不是固定类别的 softmax 分类头。"""
    def forward(self, query, keys):
        # query: (out_dim,)   keys: (K, out_dim)
        return keys @ query   # (K,) 打分，外面再 softmax/CE


def to_tensor(fnames):
    ids = torch.tensor([FNAME_VOCAB[f] for f in fnames], dtype=torch.long)
    feats = torch.tensor([CARDS[f] for f in fnames], dtype=torch.float32)
    # 简单归一化，避免 cost(0~8)/atk/def(0~10)/threat(0~0.4) 量纲差太远
    feats = feats / torch.tensor([8.0, 10.0, 10.0, 0.5])
    return ids, feats


def main():
    torch.manual_seed(0)
    encoder = CardEncoder(vocab_size=len(FNAME_VOCAB))
    head = PointerHead()
    opt = torch.optim.Adam(encoder.parameters(), lr=0.05)

    inst_ids, inst_feats = to_tensor([INSTIGATOR])
    cand_ids, cand_feats = to_tensor(CANDIDATES)
    label = torch.tensor(CANDIDATES.index(LABEL_FNAME))

    def forward_once():
        query = encoder(inst_ids, inst_feats)[0]         # (out_dim,)
        keys = encoder(cand_ids, cand_feats)             # (K, out_dim)
        return head(query, keys)                         # (K,)

    with torch.no_grad():
        logits0 = forward_once()
    print("=== 训练前 ===")
    print("候选:", CANDIDATES)
    print("打分:", [round(x, 3) for x in logits0.tolist()])
    print("argmax 选中:", CANDIDATES[int(logits0.argmax())],
          "| 真实标签:", LABEL_FNAME)

    losses = []
    for step in range(200):
        opt.zero_grad()
        logits = forward_once()
        loss = F.cross_entropy(logits.unsqueeze(0), label.unsqueeze(0))
        loss.backward()
        opt.step()
        losses.append(loss.item())

    with torch.no_grad():
        logits1 = forward_once()
    print("\n=== 训练后（200 步，过拟合这 1 条样本）===")
    print("loss: %.4f -> %.4f" % (losses[0], losses[-1]))
    print("打分:", [round(x, 3) for x in logits1.tolist()])
    picked = CANDIDATES[int(logits1.argmax())]
    print("argmax 选中:", picked, "| 真实标签:", LABEL_FNAME,
          "| %s" % ("✅ 一致" if picked == LABEL_FNAME else "❌ 不一致"))

    print("\n=== 结论 ===")
    print("这只证明了接线通（FName 查表 → embedding ⊕ 静态特征 → 指针头 → CE loss 能降、"
          "argmax 能指对），不是一个训练出来的模型——全部信号来自这 1 条真实样本，"
          "换一条决策它什么都答不出来。真正有意义的下一步是 agent/record.py（P0），"
          "把这条链路接到真实录制数据上，样本数从 1 变成几万，见 KARDS-NN.md §1.4/§9。")


if __name__ == "__main__":
    main()
