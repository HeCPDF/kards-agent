"""预测 `keepOrder=false` 三选一（好人寥寥等）的 3 张候选 —— 纯只读。

模型（2026-10-01 深夜用两个实机观测解出，6 个位置约束全中）：
    池 =  游戏**当前合法卡**（官方 API `cards` 返回的就是"没进预备"的那批，实时拉）
        ∩ 这张卡自己的过滤（好人寥寥：faction=USA(5)、rarity=Elite(4)、IsUnit）
        ∩ 静态卡表卡集过滤（`GetAllActiveStaticCards(includeNotAttainable=false)` 的 switch：
          排除 Special(2)/OnlySpawnable(3)/Candidate(4)/Placeholder(5)/Expansion1(6)/Expansion2(7)、
          NotAvailable(0)、Wildcards(11)、Core(14)、Reserved(22) 及更高位）
    然后 `Array_ShuffleFromStream(池, cardsRandomStream)`（正向 Fisher-Yates，**N 次抽取**，N=池子大小）
    取前 3 张 = 屏幕从左到右。

自检（--selftest，用两次实机观测回放）：
    obs1 seed=99421739      → [the_professionals, b_29_super_fortress, 101st_airborne]
    obs2 seed=1151364801    → [b_26_marauder, tigercat, big_red_one]
    （两次的出牌瞬间都实测跳 18 步 = 池子 18 张 ✓）

用法：python choose_spawn_predict.py [--card card_event_a_few_good_men] [--seed N] [--refresh-api]
"""
import json
import os
import struct
import sys

import _bootstrap                                              # noqa: F401,E402
from base import paths as P                                   # noqa: E402
os.environ.setdefault("KARDS_BUILD", "launcher_default")

KEEP_SETS = {1, 8, 9, 10, 12, 13, 15, 16, 17, 18, 19, 20, 21}   # 见 docstring 的 switch 表
UNIT_TYPES = {3, 4, 5, 6, 7, 8, 9, 10}                         # ETypeEnum：坦克/战斗机/轰炸机/步兵/炮兵/…


def api_legal(refresh=False):
    """游戏官方 API 当前返回的卡（= 没有进预备的那批），键 = 英文标题大写。"""
    from kards_api_reserved import load as api_load
    out = {}
    for c in api_load(refresh):
        j = c.get("json")
        j = json.loads(j) if isinstance(j, str) else j
        if not isinstance(j, dict):
            continue
        ti = j.get("title")
        en = (ti.get("en-EN") if isinstance(ti, dict) else ti) or ""
        if en:
            out[en.upper()] = j
    return out


def card_objects(ks, want_faction=5, want_rarity=4):
    """静态卡表里筛出候选池（按 faction/rarity/单位），返回 [(内部名, 英文标题, cardSet)]，保持表顺序。"""
    from kardsmem.gs import load as load_gs
    from kardsmem.names import OFF_CARD_TITLE, ftext_at
    gs = load_gs(ks)
    t = gs.static_card_table()
    blob = ks.m.read_exact(t["ptr"], (t["num"] or 0) * 8)
    ptrs = [struct.unpack_from("<Q", blob, i)[0] for i in range(0, len(blob), 8)]
    pool, out = ks.names_pool(), []
    for p in ptrs:
        n = pool.fname_of(p) or ""
        if not n.startswith("card_") or n.startswith("card_event_"):
            continue
        if ks.m.u8(p + 0x7C) != want_faction or ks.m.u8(p + 0x11C) != want_rarity:
            continue
        if ks.m.u8(p + 0x68) not in UNIT_TYPES:
            continue
        out.append((n.split("_C_")[0], (ftext_at(ks.m, p, OFF_CARD_TITLE) or "").upper(),
                    ks.m.u8(p + 0x130)))
    return out


def build_pool(cards, legal):
    """只留"当前合法（没进预备）"且卡集允许的 —— 这就是要洗的那个池子。"""
    return [(n, ti) for n, ti, cs in cards if ti in legal and cs in KEEP_SETS]


def predict(ks, legal, seed=None):
    from kardsmem.rng import Stream, read_seed
    pool = build_pool(card_objects(ks), legal)
    seed = seed if seed is not None else read_seed(ks)
    st = Stream(seed)
    order = st.shuffle(list(range(len(pool))))
    return [pool[i][0] for i in order[:3]], len(pool), st.draws, seed


def selftest():
    """回放两次实机观测（池子固定为当时解出的 18 张，顺序 = 静态表顺序）。"""
    from kards_api_reserved import load as api_load
    legal = {}
    for c in api_load(False):
        j = c.get("json")
        j = json.loads(j) if isinstance(j, str) else j
        ti = (j or {}).get("title")
        en = (ti.get("en-EN") if isinstance(ti, dict) else ti) or ""
        if en:
            legal[en.upper()] = j
    # 两次观测时池子的 18 张（内部名，按静态表顺序）—— 由 6 个约束反解得到
    pool = ["card_unit_tigercat", "card_unit_tropic_lightning", "card_unit_fifth_ohio",
            "card_unit_the_american_guard", "card_unit_sykes_regulars", "card_unit_507th_pir",
            "card_unit_thunderbolt", "card_unit_the_professionals", "card_unit_327th_pathfinders",
            "card_unit_133rd_ironman", "card_unit_c47_skytrain", "card_unit_m6",
            "card_unit_big_red_one", "card_unit_us_signal_corps", "card_unit_m26_pershing",
            "card_unit_b_26_marauder", "card_unit_101st_airborne", "card_unit_b_29_super_fortress"]
    from kardsmem.rng import Stream
    obs = [(99421739, ["card_unit_the_professionals", "card_unit_b_29_super_fortress",
                       "card_unit_101st_airborne"]),
           (1151364801, ["card_unit_b_26_marauder", "card_unit_tigercat",
                         "card_unit_big_red_one"]),
           # obs3 = **盲预测**命中（先给答案、用户后打）：截图 choose_spawn_blind_hit_20261002.png
           (2429547777, ["card_unit_us_signal_corps", "card_unit_327th_pathfinders",
                         "card_unit_fifth_ohio"])]
    bad = 0
    for seed, want in obs:
        st = Stream(seed)
        got = [pool[i] for i in st.shuffle(list(range(len(pool))))[:3]]
        ok = got == want
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + "seed=%d → %s" % (seed, [g.replace("card_unit_", "") for g in got]))
    print("池子 %d 张；失败 %d 项" % (len(pool), bad))
    return bad


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if selftest() else 0)
    refresh = "--refresh-api" in sys.argv
    seed = None
    if "--seed" in sys.argv:
        seed = int(sys.argv[sys.argv.index("--seed") + 1], 0)
    from kardsmem import attach
    from kardsmem.locres import translator
    ks = attach()
    tr = translator()
    top, n, draws, s = predict(ks, api_legal(refresh), seed)
    print("池子 %d 张，seed=%d，洗牌抽 %d 次" % (n, s, draws))
    for i, c in enumerate(top):
        ti = c.replace("card_unit_", "")
        print("  %d. %-32s" % (i + 1, ti))
    io = open(os.path.join(P.DATA, "choose_spawn_pred.json"), "w", encoding="utf-8")
    json.dump({"seed": s, "pool": n, "top3": top}, io, ensure_ascii=False, indent=1)
    io.close()
