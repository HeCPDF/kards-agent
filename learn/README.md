# nn —— KARDS 神经网络训练侧（前端③的 P1/P2）

权威设计是 `reverse-data/reports/spec/KARDS-NN.md`；交接稿是
`reverse-data/reports/_archive/KARDS-NN-HANDOFF-2026-09-28.md`（已归档；现行设计看
`reverse-data/reports/spec/KARDS-NN.md`）。本文件只讲"怎么跑"。

## 模块

| 文件 | 干什么 | 依赖 |
|---|---|---|
| `schema.py` | 样本 schema + 可见性投影（**录制器也读它**，唯一契约） | 无 |
| `features.py` | 卡牌静态特征表：FName 索引 + 显示名反查（`server/data/cards.json`，2019 张，60 维） | numpy |
| `encode.py` | 一条样本 → 实体 token / 全局向量 / 候选集 / 标签下标；含防泄漏断言 | numpy |
| `dataset.py` | 读 `kards-data/recordings/*.jsonl.gz`、按局切分、组 batch、标签覆盖率报告 | numpy（batch 时才 torch） |
| `model.py` | CardEncoder + Transformer + type/subject/target/option 指针头 + 价值头 + 多头 loss | torch |
| `baselines.py` | 三条基线：always-end / random-legal / greedy-heuristic | numpy |
| `train_bc.py` | 第一阶段 BC 训练入口 | torch |
| `eval.py` | 指标（per-phase top-1/top-3、macro-F1、full-action）+ 基线对比 + 输入敏感性 | torch |

在线侧（前端③）在 `kards-agent/agent/nn.py`：**网络策略 `NNPolicy` + 回路 `Loop`**。
里面**没有启发式**——手写规则在 `nn/baselines.py::StrategicRule`（网络必须打赢的
对照组之一），要用得显式 `--policy rule`。

## 跑法（全部在 `kards-agent/` 目录下，用 NN 专用 venv）

```powershell
# 单元自检（离线，不需要游戏；每个都能失败）
nn\venv\Scripts\python.exe -m nn.features
nn\venv\Scripts\python.exe -m nn.encode
nn\venv\Scripts\python.exe -m nn.dataset
nn\venv\Scripts\python.exe -m nn.model
nn\venv\Scripts\python.exe -m nn.baselines

# 冒烟训练（真实录制数据，CPU 就够）
nn\venv\Scripts\python.exe -m nn.train_bc --epochs 8 --split random --val-frac 0.5 `
    --batch 16 --d 64 --layers 2 --heads 4 --cpu `
    --out D:\Kards\kards-data\nn\runs\smoke

# 评估一个 checkpoint（自动带三条基线 + 输入敏感性）
nn\venv\Scripts\python.exe -m nn.eval --ckpt D:\Kards\kards-data\nn\runs\smoke\ckpt.pt

# 正式训练（3070 上开 CUDA；权重与数据都在仓库外）
nn\venv\Scripts\python.exe -m nn.train_bc --epochs 40 --split time --val-frac 0.2

# 在线回路（前端③）：默认 dry-run，只打印打算做什么
nn\venv\Scripts\python.exe -m agent.nn --ckpt D:\Kards\kards-data\nn\runs\<run>\ckpt.pt
# 真发动作（只在训练局；排位/休闲要用户同意）
nn\venv\Scripts\python.exe -m agent.nn --ckpt ... --live --max-actions 60
# 对照组：启发式基线（不是网络策略）
nn\venv\Scripts\python.exe -m agent.nn --policy rule --ckpt ...
```

在线回路的动作来源（`NNPolicy`）：`type` 头选类型、`subject`/`target` 指针选主体目标、
`option` 头管 pick/牌库选牌、`mulligan` 头逐张 keep/discard；主回合四选一是
`type_logit + 指针分` 一起比，**没有手写优先级**。默认 strict：编码/推理失败即停手，
`--fallback-rule` 才退回 `StrategicRule`。自检 `python -m agent.nn --selftest` 里
有一组"**改网络权重 ⇒ 动作跟着变**"的双向证明（证明动作确实来自网络）。

权重/数据落点：`D:\Kards\kards-data\`（仓库外 —— 发布纪律 + 体积）。
`nn/venv/` 被 `.gitignore` 的 `venv/` 覆盖，不会进库。

## 数据契约（和录制器的接口）

一行 = 一个决策点，字段见 `schema.py::make_sample` / `KARDS-NN.md §1.3`。
编码器只信这些：

* `state`（**投影后**的盘面）—— `viewer` 字段决定"我方"是谁；
* `candidates` —— 现在只有 `option`（pick/mulligan 的候选名）有内容；
  main 的主体/目标候选由 `encode.py` 从 `state` 结构化构造（`§4.3`）；
* `label` —— `{type, subject, target, option}`，subject/target 是**对局内
  slot 号**（`card_id`），不是 FName；
* `receipt.state_before_src`（2026-09-28 起）—— `ring` = state_before 来自动作
  之前的静默快照；`fallback_post` = 只能拿动作之后的快照兜底（坏样本，
  `train_bc` 默认丢掉）。

## 当前已知缺口（如实列，不假装）

1. **老录制数据（2026-09-28 之前）大多不可用于主体/目标监督**：`state_before`
   对齐 bug（已修，见 `player/record.py` 的注释）导致 live1/live2 里我方 `play`
   主体只有 1/24 还在手牌。type 头仍可用；主体/目标覆盖率 ~12%。**需要用修好
   的录制器重录一局来复验**。
2. **没有整局胜负 header** ⇒ 价值头没有监督（`compute_losses` 会自动跳过）。
   等录制器写 header 后再训。
3. **move 的目标语义**（G2：线/槽位）还没有干净参数 ⇒ move 只训主体、不训目标。
4. **`deploy_target` / `deck_pick` 还没有真实样本**（phase 分支已实现，未实机录到）。
5. **teacher forcing 口径**：subject/target 候选集按**真实动作类型**构造；
   在线回路要按 type 头的选择各跑一次指针头（§4.2）。因此离线
   `subject@1/target@1` 读作"已知动作类型后选对主体/目标"。
6. **`threat_level` 尚未接导出**（列位已留好，`features._threat_of` 一处即可接线）。
7. **卡组切分（leave-one-deck-out）** 还没有 header 里的卡组信息，`--split deck`
   目前退化成按局随机并在 `info` 里标注——不许当卡组泛化指标用。

## 指标口径（能失败）

`eval.report()` 的每行都是同一套 `score_predictions`：`type@1 / macro-F1 /
subject@1 / subject@3 / target@1 / option@1 / full@1`，外加
`input_sensitivity`（同一 phase 抽成对样本，**identical_rate 越接近 1 越像常数头**；
项目真实踩过一个"18/20 覆盖率但永远返回同一个值"的求值器，这条就是为它准备的）。
分母（有标签的样本数）每次都跟着打印；没有标签的样本不进指标，也**绝不猜**。
