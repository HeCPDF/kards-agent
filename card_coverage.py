#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""card_coverage.py —— 卡牌机制 → **自动化可行性**普查（只读磁盘上的导出物，不碰游戏）。

数据源
======
1. FModel 导出的卡牌 cpp：`reverse-data\\exports-1.58.27125.Steam\\kards\\Content\\Blueprints\\Cards\\**\\card_*.cpp`
   （2019 张）—— 每张卡的机制字段/函数都在里面：`selectTargetOnPlayedFromHand`、
   `chooseOneCards` + `WhichChooseOne`、`GetChooseSpawnCards`、`spawnCardName`、
   `selectCardToDraw`、`OnHandTargetSelected`、`selectTargetFromHand`、`DamageMultipleCards`…
2. `server\\data\\cards.json`（2019 条：title/type/faction/collectible/text）—— 判定可收藏、拿卡面文本。

它回答的问题
============
**哪些卡需要额外交互**（选目标 / 从 N 张里选 1 / 从牌库或对手手牌选牌 / 多目标 / 抉择），
以及这些交互在现有执行层（`tools/ops.py`）里**能不能点**：
- 已标定坐标的界面：抉择 2 张、预报/候选 3 张（两级）→ `ops.PICK_X = {2:[537,741], 3:[420,639,858]}`
- 未标定：牌库选牌、看对手手牌、>3 张候选（滚动）
- 完全没有流程：抉择之后再选目标（两阶段）、目标在**手牌**里

结论沉淀在 `reports/CARD-AUTOMATION-COVERAGE.md`。

用法：
    python card_coverage.py                    # 统计 + 分类示例
    python card_coverage.py --json out.json    # 逐卡明细落盘
    python card_coverage.py --card <名字>       # 看一张卡
    python card_coverage.py --list <标记>       # 列出某一类的全部卡
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentpath  # noqa: E402  —— 工作区根按标志物找，别硬编码

WORKSPACE = agentpath.workspace()
CARDS_CPP_ROOT = os.path.join(WORKSPACE, r"reverse-data\exports-1.58.27125.Steam\kards\Content\Blueprints\Cards")
CARDS_JSON = os.path.join(WORKSPACE, r"server\data\cards.json")

# 标记 → (说明, 自动化现状)   现状：ok=已支持 / half=有流程但未验证或易失败 / gap=没流程或坐标未标定
MARKERS = {
    "target_on_play":  ("部署/打出后需要选目标（selectTargetOnPlayedFromHand）", "ok"),
    "on_hand_target":  ("指向性指令：手牌拖到目标上（OnHandTargetSelected）", "ok"),
    "target_from_hand": ("目标在**手牌**里：selectTargetFromHand", "half"),
    "choose_one":      ("抉择：chooseOneCards / WhichChooseOne（2 张）", "ok"),
    "two_stage":       ("★ 抉择的选项本身还要选目标（两步）", "gap"),
    "choose_spawn":    ("★ 从候选里选 1 生成：GetChooseSpawnCards（开发类）", "half"),
    "forecast":        ("预报（天气两级选择）", "half"),
    "select_card_to_draw": ("★ 从牌库 / 牌堆顶 N 张里挑 1 张", "gap"),
    "multi":           ("多目标/全体（DamageMultipleCards 等）", "half"),
    "discard":         ("涉及弃牌（弃手牌 / 从弃牌堆取）", "half"),
}


def load_meta():
    if not os.path.isfile(CARDS_JSON):
        return {}
    with open(CARDS_JSON, "r", encoding="utf-8") as f:
        rows = json.load(f)
    return {os.path.basename(c.get("source") or ""): c for c in rows if c.get("source")}


def scan():
    meta = load_meta()
    out = []
    for dirpath, _dirs, files in os.walk(CARDS_CPP_ROOT):
        for fn in files:
            if not fn.endswith(".cpp") or not fn.startswith("card_"):
                continue
            try:
                txt = open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            spawn = re.search(r'spawnCardName\s*=\s*"([^"]*)"', txt)
            names = [x for x in (spawn.group(1).split(";") if spawn else []) if x.strip()]
            co = re.search(r"chooseOneCards = \{(.{0,4000}?)\n    \};", txt, re.S)
            two = bool(co and re.search(r"selectTargetOnPlayedFromHand.*true", co.group(1)))
            j = meta.get(fn, {})
            marks = {
                "target_on_play": "selectTargetOnPlayedFromHand" in txt,
                "on_hand_target": "OnHandTargetSelected" in txt,
                "target_from_hand": "selectTargetFromHand" in txt,
                "choose_one": ("chooseOneCards" in txt) or ("WhichChooseOne" in txt),
                "two_stage": two,
                "choose_spawn": "GetChooseSpawnCards" in txt,
                "forecast": bool(re.search(r"[Ff]orecast", txt)),
                "select_card_to_draw": "selectCardToDraw" in txt,
                "multi": any(k in txt for k in ("DamageMultipleCards", "GetAllTargets",
                                                "selectMultiple", "SelectMultiple")),
                "discard": bool(re.search(r"Discard", txt)),
            }
            out.append({
                "name": fn[:-4],
                "file": os.path.relpath(os.path.join(dirpath, fn), CARDS_CPP_ROOT),
                "title": j.get("title"),
                "collectible": j.get("collectible"),
                "type": j.get("type"),
                "text": (j.get("text") or "")[:220],
                "spawn_n": len(names),
                "spawn_list": ";".join(names),
                "marks": marks,
            })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="卡牌机制 → 自动化可行性普查")
    ap.add_argument("--json", metavar="OUT")
    ap.add_argument("--list", metavar="MARKER")
    ap.add_argument("--card", metavar="NAME")
    ap.add_argument("--examples", type=int, default=6)
    a = ap.parse_args(argv)

    if not os.path.isdir(CARDS_CPP_ROOT):
        print("找不到卡牌导出目录：%s" % CARDS_CPP_ROOT)
        return 2
    cards = scan()
    coll = sum(1 for c in cards if c["collectible"])
    print("card_*.cpp：%d 张（可收藏 %d ／ 不可收藏 %d）\n"
          % (len(cards), coll, len(cards) - coll))

    if a.card:
        hits = [c for c in cards if a.card in c["name"]]
        for c in hits:
            print(json.dumps(c, ensure_ascii=False, indent=1))
        return 0 if hits else 2
    if a.list:
        for c in cards:
            if c["marks"].get(a.list):
                print("  %-52s coll=%-5s %s" % (c["name"], c["collectible"], c["text"][:70]))
        return 0

    print("=== 各族（按卡牌文件）===")
    print("%-20s %5s %5s  %-4s %s" % ("标记", "总数", "可收藏", "现状", "说明"))
    for key, (desc, status) in sorted(MARKERS.items(),
                                      key=lambda kv: -sum(1 for c in cards if c["marks"].get(kv[0]))):
        hit = [c for c in cards if c["marks"].get(key)]
        print("%-20s %5d %5d  %-4s %s" % (key, len(hit), sum(1 for c in hit if c["collectible"]),
                                          status, desc))

    need = [c for c in cards if any(c["marks"].values())]
    print("\n需要额外交互（并集）：%d 张（可收藏 %d）；完全不需要：%d 张"
          % (len(need), sum(1 for c in need if c["collectible"]), len(cards) - len(need)))

    for key in ("two_stage", "select_card_to_draw", "choose_spawn", "target_from_hand", "multi"):
        hit = [c for c in cards if c["marks"].get(key)]
        if not hit:
            continue
        print("\n=== %s（%s）—— %d 张 ===" % (key, MARKERS[key][0], len(hit)))
        for c in hit[:a.examples]:
            print("   %-50s coll=%-5s %s" % (c["name"][:50], c["collectible"], c["text"][:72]))
        if len(hit) > a.examples:
            print("   ... 还有 %d 张（--list %s）" % (len(hit) - a.examples, key))

    big = [c for c in cards if c["spawn_n"] > 3]
    if big:
        print("\n=== spawnCardName 里候选 >3 的（若要全部展示 ⇒ 需要滚动/翻页）—— %d 张 ===" % len(big))
        for c in big[:a.examples]:
            print("   %-50s n=%-3d %s" % (c["name"][:50], c["spawn_n"], c["spawn_list"][:80]))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"markers": {k: v[1] for k, v in MARKERS.items()}, "cards": cards},
                      f, ensure_ascii=False, indent=1)
        print("\n已写入 %s" % a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
