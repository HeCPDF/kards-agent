#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""score_candidates 的临时手牌 id 必须按牌名唯一（否则不同次调用的“第 0 个候选”共用缓存，天气路径同分）。"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from player.rule import RuleV2                                          # noqa: E402

src = inspect.getsource(RuleV2.score_candidates)
ok = "_fake_ids" in src and ("cid = -1000 - i" + chr(10)) not in src
print("  [%s] 临时手牌 id 按牌名固定、不再用候选序号" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
