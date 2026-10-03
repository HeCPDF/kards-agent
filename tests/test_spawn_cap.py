#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 / 部署的排容量——按原版实现（用户 2026-10-03：「sim 要改。尊重原游戏实现」；起因：支援线已满时脚本还去打
PLAN WEST（"往支援线加两张 LEGIONS"），sim 却无条件生成单位，估出 +5.38，实际空打）。

原版依据（1.60 导出）：
  * `BP_GameState_Battle::FetchCardsByLocation`：手牌 9；**支援线 5 张含总部卡 ⇒ 单位 4**；前线 5（受限时 2）。
  * `BP_CardFunctions::SpawnCardToBoard`：非前线 ⇒ `isLocationFull` 且没指定卡 id ⇒ 不生成；前线被对方占 ⇒ 拒绝。
  * `CanMoveCardToLocation`：`IsLocationFull && 不是指令` ⇒ 不能部署单位。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import policy.boardeval as B                                    # noqa: E402
from policy.boardeval import A, H, U                            # noqa: E402
from policy.search import _gen_actions                         # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


LEGION = {"atk": 2, "dfn": 2, "cost": 2, "typ": "infantry", "kw": ()}


def board(n_back, hand=None, **kw):
    units = {i: U(i, "local", "back", 1, 1, 1, "infantry") for i in range(1, n_back + 1)}
    return B.Sim(units, {"local": 20, "enemy": 20}, 6.0, hand or {}, 6.0, **kw)


def spawn(s, n=2, row="back", mine=True):
    B._apply_eff(s, {"spawn": n, "spawn_cards": [{"name": "card_unit_legion_pol", "row": row, "mine": mine}] * n}, None)
    return s


def main():
    stats = lambda nm: LEGION if nm == "card_unit_legion_pol" else None    # noqa: E731

    s = spawn(board(4, spawn_stats=stats))
    chk("支援线已满（4 单位 + 总部 = 5）⇒ 一张都不生成（PLAN WEST 空打）", len(s.units) == 4)
    s = spawn(board(3, spawn_stats=stats))
    chk("还剩 1 格 ⇒ 只生成 1 张，且用卡自己的面板（LEGIONS 2/2，部署病）",
        len(s.units) == 4 and sum(1 for u in s.units.values() if u.id < 0 and (u.atk, u.dfn) == (2, 2) and u.sick) == 1)
    s = spawn(board(0, spawn_stats=stats))
    chk("空线 ⇒ 生成 2 张", len(s.units) == 2)
    s = spawn(board(0))
    chk("读不到面板 ⇒ 不编、记缺口", len(s.units) == 0 and any("面板读不到" in g for g in s.gaps))

    f = B.Sim({i: U(i, "local", "frontline", 1, 1, 1, "infantry") for i in range(1, 6)}, {"local": 20, "enemy": 20}, 6.0, {}, 6.0,
              spawn_stats=stats)
    spawn(f, 1, "frontline")
    chk("前线满 5 ⇒ 不生成", len(f.units) == 5)
    f2 = B.Sim({1: U(1, "local", "frontline", 1, 1, 1, "infantry")}, {"local": 20, "enemy": 20}, 6.0, {}, 6.0,
               spawn_stats=stats, front_limited=True)
    spawn(f2, 2, "frontline")
    chk("前线受限（IsFrontlineLimited）容 2 ⇒ 只再生成 1 张", len(f2.units) == 2)
    f3 = B.Sim({1: U(1, "enemy", "frontline", 1, 1, 1, "infantry")}, {"local": 20, "enemy": 20}, 6.0, {}, 6.0,
               front_owner="enemy", spawn_stats=stats)
    spawn(f3, 1, "frontline")
    chk("前线被对方占 ⇒ 拒绝生成", len(f3.units) == 1)

    e = B.Sim({i: U(-i, "enemy", "back", 1, 1, 1, "infantry") for i in range(1, 5)}, {"local": 20, "enemy": 20}, 6.0, {}, 6.0,
              spawn_stats=stats)
    spawn(e, 1, "back", mine=False)
    chk("对方支援线满 ⇒ 对方也生成不了", len(e.units) == 4)

    g = board(3)
    B._apply_eff(g, {"spawn": 3}, None)
    chk("旧摘要（没有卡名）仍受容量限制：3 个 + 3 个只能到 4", sum(1 for u in g.units.values() if u.side == "local") == 4)

    hand = {10: H(10, "INF", 2, "infantry", 2, 2, (), {}), 11: H(11, "ORDER", 2, "order", 0, 0, (), {})}
    full = board(4, hand)
    kinds = {(a.kind, a.src) for a in _gen_actions(full)}
    chk("支援线满 ⇒ 不再提议部署单位，指令照常", ("deploy", 10) not in kinds and ("order", 11) in kinds, str(sorted(kinds, key=str)))
    room = board(3, hand)
    kinds2 = {(a.kind, a.src) for a in _gen_actions(room)}
    chk("还有空位 ⇒ 部署照常提议", ("deploy", 10) in kinds2)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
