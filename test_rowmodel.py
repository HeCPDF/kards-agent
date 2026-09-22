# -*- coding: utf-8 -*-
"""行模型离线回归：用 2026-09-21 实机快照的真实 slot 分布喂 screen_map。"""
import sys, json, io, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops


class C:
    def __init__(self, card_id, side, location, slot, name="X", card_type="infantry"):
        self.card_id = card_id; self.side = side; self.location = location
        self.slot = slot; self.name = name; self.card_type = card_type


class S:
    def __init__(self, cards): self.cards = cards


def show(title, st, field):
    d, warn = ops.screen_map_detailed(st, field)
    print("== %s ==" % title)
    for w in warn:
        print("   [warn] %s" % w)
    for cid, v in sorted(d.items(), key=lambda t: (t[1]["row"], t[1]["rank"])):
        print("   id=%-3s %-10s rank=%d/%d slot=%s x=%-4d y=%-3d %s (model=%s ocr=%s)"
              % (cid, v["row"], v["rank"], v["n"], v["slot"], v["x"], v["y"],
                 v["source"], v["x_model"], v["x_ocr"]))
    return d, warn


# --- 1. 实机快照的真实分布：local back slot0 + local hq slot1；enemy hq slot0 + enemy back slot1
cards = [
    C(1,  "local", "back",      0, "B-17", "bomber"),
    C(41, "local", "hq",        1, "CHERBOURG", "location"),
    C(2,  "enemy", "hq",        0, "CHERBOURG", "location"),
    C(3,  "enemy", "back",      1, "104th INF"),
    C(4,  "enemy", "frontline", 0, "M8 GREYHOUND", "tank"),
    C(5,  "enemy", "frontline", 1, "1st MARINES"),
]
d, warn = show("实机快照分布（无 OCR）", S(cards), {})
assert not warn, warn
# 两张一行 ⇒ 对称落在 center ± pitch/2
assert (d[1]["x"], d[41]["x"]) == (570, 714), (d[1], d[41])   # 642 ∓ 71.5
assert d[2]["x"] == 639 - 71 and d[3]["x"] == 639 + 71
assert d[4]["x"] == 645 - 68 and d[5]["x"] == 645 + 68
assert d[41]["y"] == 525 and d[2]["y"] == 179 and d[4]["y"] == 350

# --- 2. 单张：居中
one = [C(9, "local", "hq", 0, "HQ", "location")]
d2, _ = show("我方排只有总部", S(one), {})
assert d2[9]["x"] == 642, d2[9]

# --- 3. 五张前线：等距
five = [C(10 + i, "enemy", "frontline", i) for i in range(5)]
d3, _ = show("前线 5 个单位", S(five), {})
xs = [d3[10 + i]["x"] for i in range(5)]
assert xs == [645 - 272, 645 - 136, 645, 645 + 136, 645 + 272], xs

# --- 4. OCR 数目对得上 ⇒ 吸附到实测值
field_ok = {"frontline": [{"cx": 380, "cy": 350}, {"cx": 515, "cy": 350},
                          {"cx": 648, "cy": 350}, {"cx": 782, "cy": 350},
                          {"cx": 915, "cy": 350}]}
d4, w4 = show("OCR 全中 ⇒ 吸附", S(five), field_ok)
assert all(d4[10 + i]["source"] == "ocr-confirmed" for i in range(5)), d4
assert [d4[10 + i]["x"] for i in range(5)] == [380, 515, 648, 782, 915]
assert not w4, w4

# --- 5. ★ 回归：OCR 漏检一个框 ⇒ 绝不错位，全部退回模型
field_miss = {"frontline": [{"cx": 380, "cy": 350}, {"cx": 515, "cy": 350},
                            {"cx": 648, "cy": 350}, {"cx": 782, "cy": 350}]}
d5, w5 = show("★ OCR 漏检 1 框（旧代码在这里错位）", S(five), field_miss)
assert all(d5[10 + i]["source"] == "model" for i in range(5)), d5
assert [d5[10 + i]["x"] for i in range(5)] == xs, "漏检不得改变任何一张的坐标"
assert len(d5) == 5, "漏检不得让任何一张丢坐标"
assert any("mem=5 ocr=4" in w for w in w5), w5

# --- 6. locationNumber 不密集（某张死了留空位）⇒ 告警但仍按名次排
sparse = [C(20, "enemy", "frontline", 0), C(21, "enemy", "frontline", 2)]
d6, w6 = show("locationNumber 稀疏 0,2", S(sparse), {})
assert any("非密集" in w for w in w6), w6
assert d6[20]["x"] == 645 - 68 and d6[21]["x"] == 645 + 68

# --- 7. 指令/反制牌不占行位
mixed = [C(30, "local", "back", 0, "U1"), C(31, "local", "back", 1, "ORDER", "order")]
d7, _ = show("order 不占行位", S(mixed), {})
assert 31 not in d7 and d7[30]["n"] == 1 and d7[30]["x"] == 642

print("\nROW-MODEL SELFTEST: PASS（7 组）")
