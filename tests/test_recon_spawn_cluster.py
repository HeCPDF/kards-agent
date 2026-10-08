#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线批量对账（2026-10-06）里 USS YORKTOWN / DEATH FROM ABOVE / HEATWAVE / OVERCAST 的 A/B/C 分歧修复回归。

原版依据（1.58.27125 导出，逐行读过）：
  * `BP_CardFunctions.cpp:6417-6446` `SpawnCardonBattlefield(side, Frontline, card_name, …)`：`Frontline` 真 ⇒ `spawnlocation=0x7`（前线），
    假 ⇒ 支援线（USS YORKTOWN：`Frontline = !IsLocationFull(前线)`，`card_event_uss_yorktown.cpp:15-23`）；
  * `card_event_sunny2_heatwave.cpp`：对**每个**场上单位 `AddAttackUntilEndOfTurn(unit, cardID, 空军 3 / 其余 2)`；
  * `card_event_overcast.cpp:36-41`：`GainKreditSlot(this, side)` + `GainKreditSlot(this, GetOppositeSide())` ⇒ 双方各 +1；
  * `card_event_death_from_above.cpp`：随机敌方单位 `DestroyCard(randomCard, this)`（随机由活种子定成确定结果）。
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
_STAT = {"atk": 1, "dfn": 2, "cost": 1, "typ": "fighter", "kw": ()}


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(units=(), **kw):
    s = S.Sim({}, {ME: 20, OPP: 20}, 5, {}, my_side=ME, slots=3, kredit_max=24, **kw)
    for u in units:
        s.units[u.id] = u
    s.opp_slots = 0
    return s


def main():
    # ---- ① 生成：`Frontline` 实参决定落点；载荷带全 ⇒ 重放与直跑一致 ----
    for flag, want_row in ((True, "frontline"), (False, "back")):
        s1 = mk(spawn_stats=lambda n: _STAT)
        h = SC.native_hooks(s1, my_side=ME, spawn_stat=lambda n: _STAT)
        ctx = h["__ctx__"]
        # SpawnCardonBattlefield(side, Frontline, card_name, spawnerID, …)
        h["SpawnCardOnBattlefield"](None, None, None, [ME, flag, "card_unit_brewster_f2a", 7, None, False, -1, 0, False, False, None], None)
        new = [u for u in s1.units.values() if u.id < 0]
        chk("直跑：SpawnCardOnBattlefield(Frontline=%s) ⇒ 落 %s" % (flag, want_row),
            len(new) == 1 and new[0].row == want_row, str([(u.id, u.row) for u in new]))
        s2 = mk(spawn_stats=lambda n: _STAT)
        res = CALLS.apply_calls(s2, list(ctx.applied))
        a, b = SC._state_snapshot(s1), SC._state_snapshot(s2)
        chk("重放：`spawn` 载荷（side/row/面板）⇒ 与直跑同一份状态、无缺口（Frontline=%s）" % flag,
            a == b and not res.gaps, "diff=%s gaps=%s" % ({k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}, res.gaps))

    # ---- ② effectvm：召唤落点 / 对方槽 / 群体本回合攻 / 随机摧毁 ----
    rec = EV.Recorder()
    rec.records.append({"verb": "SpawnCardOnBattlefield",
                        "args": [ME, True, "card_unit_brewster_f2a", 7, None, False, -1, 0, False, False], "tainted": False})
    rec.records.append({"verb": "SpawnCardOnBattlefield",
                        "args": [ME, False, "card_unit_brewster_f2a", 7, None, False, -1, 0, False, False], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：Frontline 真/假 ⇒ spawn_cards 的 row = frontline/back",
        [x["row"] for x in e.get("spawn_cards", [])] == ["frontline", "back"], str(e.get("spawn_cards")))

    rec = EV.Recorder()
    rec.records.append({"verb": "GainKreditSlot", "args": [0x1, ME], "tainted": False})
    rec.records.append({"verb": "GainKreditSlot", "args": [0x1, OPP], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：OVERCAST 双方各 +1 槽 ⇒ slot=1、opp_slot=1（原先对方那次被记到我方 ⇒ slot=2）",
        e.get("slot") == 1 and e.get("opp_slot") == 1, str(e))
    s = mk()
    _apply_eff(s, e, None)
    chk("_apply_eff：我方槽 +1、对方槽 +1", s.slots == 4 and s.opp_slots == 1, "slots=%s opp=%s" % (s.slots, s.opp_slots))

    rec = EV.Recorder()
    rec.ptr_ids.update({0x100: 1, 0x200: 2, 0x300: 3})
    for p, n in ((0x100, 2), (0x200, 3), (0x300, 2)):
        rec.records.append({"verb": "AddAttackUntilEndOfTurn", "args": [p, 9, n], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：HEATWAVE ⇒ attack_turn_ids 逐张记账（空军 3、其余 2）",
        e.get("attack_turn_ids") == {1: 2, 2: 3, 3: 2} and e.get("attack_turn") == 7, str(e))
    s = mk([S.U(1, ME, "back", 5, 3, 1, "infantry"), S.U(2, ME, "back", 2, 1, 1, "fighter"), S.U(3, OPP, "frontline", 2, 1, 1, "infantry")])
    _apply_eff(s, e, None)
    chk("_apply_eff：群体 +攻 逐张结算（含敌方单位），记 atk_turn（回合结束消失的部分）",
        [(u.atk, u.atk_turn) for u in (s.units[1], s.units[2], s.units[3])] == [(7, 2), (5, 3), (4, 2)],
        str({k: (u.atk, u.atk_turn) for k, u in s.units.items()}))
    s = mk([S.U(1, ME, "back", 5, 3, 1, "infantry")])
    e1 = {"attack_turn": 2, "attack_turn_ids": {1: 2}}
    _apply_eff(s, e1, 1)
    chk("单体（带目标、ids 只有一张）仍走标量路径、只加一次", (s.units[1].atk, s.units[1].atk_turn) == (7, 2))

    rec = EV.Recorder()
    rec.ptr_ids.update({0x100: 1, 0x200: 2})
    for p in (0x100, 0x200):
        rec.records.append({"verb": "ChangeOperationCost", "args": [p, 9, -1, 0, True, False, False], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：HEATWAVE 2 行动费 -1 ⇒ opcost_ids 逐张记账", e.get("opcost_ids") == {1: -1, 2: -1} and e.get("opcost") == -2, str(e))
    s = mk([S.U(1, ME, "back", 5, 3, 1, "infantry"), S.U(2, ME, "back", 2, 1, 1, "infantry")])
    s.units[1].opc, s.units[2].opc = 2, 0
    _apply_eff(s, e, None)
    chk("_apply_eff：行动费逐张 -1（下限 0）", (s.units[1].opc, s.units[2].opc) == (1, 0), "%s %s" % (s.units[1].opc, s.units[2].opc))

    rec = EV.Recorder()
    rec.ptr_ids[0x400] = 46
    rec.records.append({"verb": "DestroyCard", "args": [0x400, 9], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("to_effects：DEATH FROM ABOVE（脚本自己挑的非目标单位）⇒ destroy_aoe_ids=[46]，不再是裸 destroy",
        e.get("destroy_aoe_ids") == [46] and "destroy" not in e, str(e))
    rec = EV.Recorder()
    rec.ptr_ids[0x400] = 46
    rec.target_ptr = 0x400
    rec.records.append({"verb": "DestroyCard", "args": [0x400, 9], "tainted": False})
    e = EV.to_effects(rec, my_side=ME)
    chk("选定目标的 DestroyCard 老路径不变（destroy=True）", e.get("destroy") is True and "destroy_aoe_ids" not in e, str(e))
    s = mk([S.U(46, OPP, "frontline", 6, 4, 1, "tank"), S.U(47, OPP, "back", 1, 1, 1, "infantry")])
    _apply_eff(s, {"destroy_aoe_ids": [46]}, None)
    chk("_apply_eff：只消灭那一张", 46 not in s.units and 47 in s.units)

    print("\n%s" % ("全部通过" if not fails else "失败 %d 项" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
