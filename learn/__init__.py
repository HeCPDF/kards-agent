#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nn —— KARDS 神经网络（前端③）的训练侧。

模块地图（KARDS-NN.md §8）：
    schema.py     样本 schema + 可见性投影（录制与训练的唯一契约，recorder 也读它）
    features.py   卡牌静态特征表（FName 索引 + 显示名反查）
    encode.py     一条样本 -> 张量（实体 token / 全局向量 / 候选集 / 标签）
    dataset.py    录制数据加载 + 按局/按时间切分 + 组 batch
    model.py      encoder + 阶段头/指针头/价值头
    baselines.py  三条基线（always-end / random-legal / greedy-heuristic）
    train_bc.py   BC 训练入口
    eval.py       指标 + 基线对比 + 输入敏感性自检

依赖约定：录制器（player/record.py）**不**依赖本包的训练侧；训练侧只吃
`D:\\Kards\\kards-data\\recordings\\*.jsonl.gz`，不碰游戏。
"""
