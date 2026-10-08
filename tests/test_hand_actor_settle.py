#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`WorldMixin.hand_actor`：**横幅期瞬时假阴性**的修补（离线回归）。

实机证据（2026-10-04，`rule-live-20261004-112356.jsonl`）：一局里 4 次 `card X 不在我方手牌`
（`ops/play.py` 的 `hand_actor()` 返回空），但**同一张卡几秒后就打出成功**
（`card 33` 下一步成功；`card 29` 在 turn 32 打出）⇒ 不是候选陈旧，是**扫场那一路在手牌控件
重建窗口里短暂扫不到**（3/4 次都紧跟回合开始的横幅）。

三条相反的行为都要钉住（弯路 #11：两个不同输入必须给出不同答案）：
  ① 扫场命中 ⇒ 立刻用**扫场**记录、不等待（**不能**用 v2 的 actor 去拖拽：它是卡场景 actor，语义不同）；
  ② 扫场 miss + **权威账本 v2 里有** ⇒ 视为重建窗口 ⇒ 重试，最终拿到扫场记录；
  ③ 两边都没有 / 账本读不出 ⇒ **立刻** None、**不等待**（不惩罚正常的"不在手"判定，与改动前同延迟）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ops.world as W                                                # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _Fake:
    """只带 `ks` 的假 self —— `hand_actor` 只用它。"""

    ks = object()


def main():
    real_scan, real_ledger = W.hand_card_actors, W.hand_card_actors_v2
    sleeps = []
    f = _Fake()

    def sleep(t):
        sleeps.append(t)

    try:
        # ① 扫场直接命中
        W.hand_card_actors = lambda ks: [{"card_id": 7, "actor": "A"}]
        W.hand_card_actors_v2 = lambda ks: []
        sleeps.clear()
        r = W.WorldMixin.hand_actor(f, 7, sleep=sleep)
        chk("扫场命中 ⇒ 返回**扫场**记录且不等待", r and r["actor"] == "A" and not sleeps, "r=%s sleeps=%s" % (r, sleeps))

        # ② 重建窗口：扫场前两次 miss、第三次命中；账本里有 ⇒ 重试
        calls = {"n": 0}

        def scan_seq(ks):
            calls["n"] += 1
            return [] if calls["n"] < 3 else [{"card_id": 7, "actor": "A"}]

        W.hand_card_actors = scan_seq
        W.hand_card_actors_v2 = lambda ks: [{"card_id": 7, "actor": "Z"}]
        sleeps.clear()
        r = W.WorldMixin.hand_actor(f, 7, tries=5, gap=0.35, sleep=sleep)
        chk("重建窗口：扫场 miss×2 后命中 ⇒ 重试拿到扫场记录（不是账本记录）",
            bool(r) and r["actor"] == "A" and calls["n"] == 3 and sleeps == [0.35, 0.35],
            "r=%s n=%s sleeps=%s" % (r, calls["n"], sleeps))

        # ③ 两边都没有该卡 ⇒ 立刻 None、不等待
        W.hand_card_actors = lambda ks: []
        W.hand_card_actors_v2 = lambda ks: [{"card_id": 9}]
        sleeps.clear()
        chk("扫场与账本都没有该卡 ⇒ 立刻 None、零等待",
            W.WorldMixin.hand_actor(f, 7, sleep=sleep) is None and not sleeps, str(sleeps))

        # ④ 账本**有 7**、扫场始终没有 ⇒ 等到上限仍 None（**不误报命中**）
        W.hand_card_actors_v2 = lambda ks: [{"card_id": 7, "actor": "Z"}]
        sleeps.clear()
        chk("账本有但扫场始终没有 ⇒ 等满上限后 None（不给假记录）",
            W.WorldMixin.hand_actor(f, 7, tries=3, gap=0.1, sleep=sleep) is None and len(sleeps) == 2,
            str(sleeps))

        # ⑤ 账本读不出（例如座位读不出）⇒ 立刻 None、不等待（保持旧行为）
        def boom(ks):
            raise ValueError("座位读不出")

        W.hand_card_actors_v2 = boom
        sleeps.clear()
        chk("账本读不出 ⇒ 立刻 None、零等待（不改旧行为）",
            W.WorldMixin.hand_actor(f, 7, sleep=sleep) is None and not sleeps, str(sleeps))

        # ⑥ 诊断：want / scan / ledger 三组都给出来，且过滤 None
        W.hand_card_actors = lambda ks: [{"card_id": 3}, {"card_id": None}]
        W.hand_card_actors_v2 = lambda ks: [{"card_id": 3}, {"card_id": 8}]
        d = W.WorldMixin.hand_actor_diag(f, 7)
        chk("诊断字段：want/scan/ledger（过滤掉 card_id=None）",
            d["want"] == 7 and d["scan"] == [3] and d["ledger"] == [3, 8], str(d))
    finally:
        W.hand_card_actors, W.hand_card_actors_v2 = real_scan, real_ledger

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
