#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PARACHUTE ASSAULT 簇（2026-10-06 离线批量对账）：`SpawnMultipleCardsOnBattlefield` + `GiveX(新牌 id)` 的 A/B/C 三路一致。

原版依据（1.58.27125 导出，逐行读过）：
  * `BP_CardFunctions.cpp:8407-8610` `SpawnMultipleCardsOnBattlefield(side, Frontline, cardNames[], spawnerID, giveBlitz, &spawnedCardIDs, makeVeteran)`：
    `cardNames` 非空且 `spawnerID>0` 才干活；逐名生成，**该排已满 ⇒ break**；出参 = 新牌 **id**；`giveBlitz` ⇒ `GiveBlitz(新牌 id, spawnerID)`；
  * `card_event_parachute_assault.cpp`：生成到「支援线补满」个 `card_unit_506th_airborne`，再对 `spawnedCardIDs[i]` 逐张 `GiveShock(id, cardID)`；
  * `BP_CardFunctions.cpp:1886/2001/2124/9839`：`GiveGuard/GiveFury/GiveBlitz/GiveShock` 的首参是 **int cardID**（不是指针）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import semantics.effectvm as EV                                    # noqa: E402
from engine import calls as CALLS                                  # noqa: E402
from engine import scripts as SC                                   # noqa: E402
from engine import state as S                                      # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402

ME, OPP = 1, 2
fails = 0
_STAT = {"atk": 3, "dfn": 3, "cost": 3, "typ": "infantry", "kw": ()}


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))
    if not ok:
        fails += 1


def mk(units=(), **kw):
    s = S.Sim({}, {ME: 20, OPP: 20}, 5, {}, my_side=ME, slots=3, kredit_max=24, **kw)
    for u in units:
        s.units[u.id] = u
    s.opp_slots = 0
    return s


class _Kid:
    def __init__(self, prop):
        self.args = {"prop": prop}


class _E:
    kids = [_Kid("a"), _Kid("b"), _Kid("c"), _Kid("d"), _Kid("e"), _Kid("spawned"), _Kid("g")]


class _Fr:
    def __init__(self):
        self.locals = {}


def main():
    names = ["card_unit_506th_airborne"] * 3
    # ---- B：直跑 sink ----
    s1 = mk(spawn_stats=lambda n: _STAT)
    h = SC.native_hooks(s1, my_side=ME, spawn_stat=lambda n: _STAT)
    ctx, fr = h["__ctx__"], _Fr()
    h["SpawnMultipleCardsOnBattlefield"](None, fr, None, [ME, False, names, 7, True, None, False], _E())
    new = sorted(u.id for u in s1.units.values() if u.id < 0)
    chk("直跑：生成 3 张到支援线、出参是它们的 id、giveBlitz ⇒ blitz 且不再 sick",
        len(new) == 3 and sorted(fr.locals["spawned"]) == new
        and all(u.row == "back" and "blitz" in u.kw and not u.sick for u in s1.units.values()),
        str([(u.id, u.row, u.kw, u.sick) for u in s1.units.values()]))
    # 之后逐张 GiveShock(id)（BP 签名收 id）
    for uid in fr.locals["spawned"]:
        h["GiveShock"](None, fr, None, [uid, 7, None], _E())
    chk("GiveShock(新牌 id) 命中（Give 族首参是 cardID，不是指针）",
        all("shock" in u.kw for u in s1.units.values()) and not ctx.gaps, str(ctx.gaps))
    # ---- C：重放 ----
    s2 = mk(spawn_stats=lambda n: _STAT)
    res = CALLS.apply_calls(s2, list(ctx.applied))
    a, b = SC._state_snapshot(s1), SC._state_snapshot(s2)
    chk("重放（spawn + give_kw）≡ 直跑，无缺口", a == b and not res.gaps,
        "diff=%s gaps=%s" % ({k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}, res.gaps))
    # ---- 边界：排满 ⇒ break；spawnerID<=0 ⇒ 空 ----
    s3 = mk([S.U(100 + i, ME, "back", 1, 1, 1, "infantry") for i in range(5)], spawn_stats=lambda n: _STAT)
    h3 = SC.native_hooks(s3, my_side=ME, spawn_stat=lambda n: _STAT)
    fr3 = _Fr()
    h3["SpawnMultipleCardsOnBattlefield"](None, fr3, None, [ME, False, names, 7, False, None, False], _E())
    chk("支援线 6 格（含总部），已 5 张 ⇒ 只生成 1 张后 break", len(fr3.locals["spawned"]) <= 1, str(fr3.locals))
    s4 = mk(spawn_stats=lambda n: _STAT)
    h4 = SC.native_hooks(s4, my_side=ME, spawn_stat=lambda n: _STAT)
    fr4 = _Fr()
    h4["SpawnMultipleCardsOnBattlefield"](None, fr4, None, [ME, False, names, 0, False, None, False], _E())
    chk("spawnerID<=0 ⇒ 不生成、出参为空", fr4.locals.get("spawned") == [] and not s4.units)

    # ---- A：录制 → 效果摘要 → 字典路 ----
    rec = EV.Recorder()
    rec.records.append({"verb": "SpawnMultipleCardsOnBattlefield", "args": [ME, False, names, 7, True, None, False],
                        "tainted": False, "spawn_ids": [-7001, -7002, -7003]})
    for i in (-7001, -7002, -7003):
        rec.records.append({"verb": "GiveShock", "args": [i, 7, None], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：3 条 spawn_cards、各带 blitz+shock，且没有游离的 give",
        len(e.get("spawn_cards", [])) == 3 and all(set(x.get("kw_add", ())) == {"blitz", "shock"} for x in e["spawn_cards"])
        and "give" not in e, str(e))
    s5 = mk(spawn_stats=lambda n: _STAT)
    _apply_eff(s5, e, None)
    a5 = SC._state_snapshot(s5)
    chk("字典路 ≡ 直跑（3 张、blitz+shock、不 sick）", [v for k, v in a5.items() if k.startswith("u")] ==
        [v for k, v in SC._state_snapshot(s1).items() if k.startswith("u")],
        "A=%s B=%s" % ([v for k, v in a5.items() if k.startswith("u")],
                       [v for k, v in SC._state_snapshot(s1).items() if k.startswith("u")]))
    print("\n%s" % ("全部通过" if not fails else "失败 %d 项" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
