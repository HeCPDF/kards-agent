#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`move_up` 不再提议**被压制**的单位（原版 `IsPinned = pinnedTurns > 0`）。

实机（2026-10-04，`rule-live-20261004-114623.jsonl`）：一局里 2 次 `move_up` 被拒 ——
`"6 被压制（pin），不能移动；确信要发就传 force=True"`（`ops/play.py:792` 的**只读**闸门），
而规则侧生成候选时不查它 ⇒ 白提一个注定被拒的动作（§7.6f「判据负责挑、游戏负责判」）。

这里钉三件事：
  ① `kardsmem/board.py` 的字段表里有 `pinned_turns`（偏移 0x27C，IDA 0x144AFCD30），且**真的进了快照 raw**；
  ② `rule._is_pinned` 的语义（压制 ⇒ True；读不到/为 0 ⇒ False，**不拦**）；
  ③ `_play_cands`（生成 move_up 候选的那个方法）**真的调用了** `_is_pinned`（不是只定义不用 —— 用 AST 查，别用字符串）。
"""
import ast
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import board as B                                      # noqa: E402
from player import rule as R                                         # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    # ① 字段表 + 真的组装进 raw
    chk("board 的 CARD_I32 里有 pinned_turns=0x27C（IDA 0x144AFCD30）",
        B.CARD_I32.get("pinned_turns") == 0x27C, str(B.CARD_I32.get("pinned_turns")))
    bsrc = open(os.path.join(ROOT, "kardsmem", "board.py"), encoding="utf-8").read()
    chk("快照 raw 里真的带出 pinned_turns（不只是表里有）",
        '"pinned_turns": i32["pinned_turns"]' in bsrc)

    # ② 判定语义：两个不同输入必须给出不同答案（弯路 #11）
    chk("被压制（pinned_turns=1）⇒ True", R._is_pinned(SimpleNamespace(raw={"pinned_turns": 1})) is True)
    chk("没被压制（pinned_turns=0）⇒ False", R._is_pinned(SimpleNamespace(raw={"pinned_turns": 0})) is False)
    chk("字段读不到（老快照/空 raw）⇒ False（**不拦**，判据不足就放行）",
        R._is_pinned(SimpleNamespace(raw={})) is False
        and R._is_pinned(SimpleNamespace(raw=None)) is False
        and R._is_pinned(SimpleNamespace()) is False)

    # ③ 结构性保证：move_up 候选生成处真的用了它（AST，不是字符串匹配）
    tree = ast.parse(open(os.path.join(ROOT, "player", "rule.py"), encoding="utf-8").read())
    used = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_move_cands":
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_is_pinned":
                    used = True
    chk("_move_cands（生成 move_up 候选处）真的调用了 _is_pinned（AST 查）", used)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
