#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`selectCardToDraw` 三选一加入手牌（CRUISER SCOUTS / SOUL OF OLD JAPAN / 好人寥寥）。

* 洗牌这一半（`choosespawn.shuffle_pick`）用三次实机观测回放（`_nn_scratch/choose_spawn_predict.py --selftest` 同源；
  第三次是盲预测命中）。VM 那一半（跑卡自己的 `GetChooseSpawnCards`）需要游戏在跑，实机见 TODO A8。
* sim 这一半：`choose_spawn` 标记 + 候选表 ⇒ 单层 `select_card_to_draw` 提示；选中的牌立刻进手牌；没有候选表 ⇒ 记缺口。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cards import ME, OPP                                      # noqa: E402
from semantics import choosespawn as CS                            # noqa: E402
from engine.state import H, Sim                                 # noqa: E402
from sim.engine import A, apply, prompt_of, run                 # noqa: E402
from sim.prompt import Done, Suspended, leaves, resolve        # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


POOL = ["card_unit_tigercat", "card_unit_tropic_lightning", "card_unit_fifth_ohio",
        "card_unit_the_american_guard", "card_unit_sykes_regulars", "card_unit_507th_pir",
        "card_unit_thunderbolt", "card_unit_the_professionals", "card_unit_327th_pathfinders",
        "card_unit_133rd_ironman", "card_unit_c47_skytrain", "card_unit_m6",
        "card_unit_big_red_one", "card_unit_us_signal_corps", "card_unit_m26_pershing",
        "card_unit_b_26_marauder", "card_unit_101st_airborne", "card_unit_b_29_super_fortress"]
OBS = [(99421739, ["card_unit_the_professionals", "card_unit_b_29_super_fortress", "card_unit_101st_airborne"]),
       (1151364801, ["card_unit_b_26_marauder", "card_unit_tigercat", "card_unit_big_red_one"]),
       (2429547777, ["card_unit_us_signal_corps", "card_unit_327th_pathfinders", "card_unit_fifth_ohio"])]


def main():
    for seed, want in OBS:
        idx, draws = CS.shuffle_pick(seed, len(POOL))
        chk("洗牌回放 seed=%d" % seed, [POOL[i] for i in idx] == want and draws == len(POOL), str(draws))

    cands = [{"name": "card_unit_a", "atk": 3, "dfn": 3, "cost": 3, "typ": "infantry"},
             {"name": "card_unit_b", "atk": 1, "dfn": 1, "cost": 1, "typ": "infantry"},
             {"name": "card_unit_c", "atk": 5, "dfn": 4, "cost": 5, "typ": "tank"}]
    hand = {7: H(7, "SOUL", 2, "order", 0, 0, (), {"choose_spawn": True})}
    st = Sim({}, {ME: 20, OPP: 20}, 5.0, hand, 5.0, spawn_pick={7: cands}, my_side=ME)
    a = A("order", 7, None, 2, "SOUL")
    pr = prompt_of(st, a)
    chk("提示：select_card_to_draw 单层、三个候选", pr is not None and pr.kind == "select_card_to_draw"
        and [o.key for o in pr.options] == ["card_unit_a", "card_unit_b", "card_unit_c"] and pr.meta.get("spawn"))
    susp = run(st, a)
    chk("run 挂起", isinstance(susp, Suspended))
    lv = leaves(susp)
    chk("三条叶路径", len(lv) == 3 and [p for p, _d in lv] == [("card_unit_a",), ("card_unit_b",), ("card_unit_c",)])
    d = resolve(susp, ["card_unit_c"])
    chk("选中的牌立刻进手牌（5/4 坦克），发起牌已打出",
        isinstance(d, Done) and 7 not in d.state.hand
        and any(h.name == "card_unit_c" and (h.atk, h.dfn, h.cost) == (5, 4, 5) for h in d.state.hand.values()))
    chk("原状态不被改（resume 不修改原状态）", 7 in st.hand and len(st.hand) == 1)
    ap = apply(st, A("order", 7, None, 2, "SOUL", path=("card_unit_b",)))
    chk("apply 带路径", any(h.name == "card_unit_b" for h in ap.hand.values()))

    st2 = Sim({}, {ME: 20, OPP: 20}, 5.0, {7: H(7, "SOUL", 2, "order", 0, 0, (), {"choose_spawn": True})}, 5.0, my_side=ME)
    chk("没有候选表 ⇒ 无提示", prompt_of(st2, a) is None)
    ap2 = apply(st2, a)
    chk("没有候选表 ⇒ 如实记缺口，不编候选", any("choose_spawn" in g for g in ap2.gaps) and len(ap2.hand) == 0)

    from policy import answer as PA
    rows = [{"name": "card_unit_a_C_123", "trigger_id": 7, "index": 0}, {"name": "card_unit_b", "trigger_id": 7, "index": 1}]
    hit = PA.plan_choose_spawn(rows, {7: ("card_unit_b",)}, lambda s: str(s).split("_C_")[0])
    chk("照计划点：按名字命中屏幕行", hit is not None and hit[0]["index"] == 1)
    chk("计划里的牌不在屏幕上 ⇒ None（交给重算）", PA.plan_choose_spawn(rows, {7: ("card_unit_zzz",)}, lambda s: s) is None)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
