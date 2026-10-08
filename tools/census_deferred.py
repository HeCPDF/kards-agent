#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""延迟/常驻效果普查（离线，读 BP 导出）→ `docs/DEFERRED-EFFECTS-CENSUS.md`。

问题（用户 2026-10-07："延迟效果有很多。不能只建模这一个"）：ECHELON 之外，哪些牌的效果**活过打出这一步**？
口径（全部是静态扫描，数得出来的才写数字；判不出来的写"启发式"）：

  * 卡 = `Blueprints/Cards/**/card_*.cpp`（排除 `card_display_*` 皮肤）。
  * `Type` / `usedTriggers` / `public void On…(` 覆写 / `cardFunction->…` 调用 / `GameplayTags` 逐文件读。
  * **打出即结算的钩子**（`PLAYTIME_HOOKS`）之外的覆写 = "留下来的钩子"（armed hooks）。
  * 指令（order）打出后仍带这类钩子 ⇒ **打出之后继续挂着**（ECHELON/JUNGLE FEVER/天气牌…）；
    单位/地点的这类钩子是**常驻在场**（读侧从活盘面拿，不属于"打出之后才布置"）——只计数。
  * 授予（`CustomAbilityAdd("名字", …)`）、倒计时（`SetCountdown`）、游戏限制（`AddGameplayRestriction`）、
    触发队列（`AddToTriggerQueue`）、`autoplay` 标签、反制（`Type=gotcha`）、"直到回合结束"类各记一族。
  * 持续时间/范围是**文本+调用启发式**（见 `classify_*`），不是语义证明；表里标"启发式"。

用法：  python tools/census_deferred.py [--exports <导出目录>] [--out docs/DEFERRED-EFFECTS-CENSUS.md]
离线、只读导出；"registry 现状"一列取自 `engine.deferred.FIRE_POINTS`（单一来源）。
"""
from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_EXPORTS = os.path.join(os.path.dirname(ROOT), "reverse-data",
                               "exports-1.60.27292.launcher-only-decompiled-BP", "kards",
                               "Content", "Blueprints", "Cards")
ALT_EXPORTS = r"D:\Kards\reverse-data\exports-1.60.27292.launcher-only-decompiled-BP\kards\Content\Blueprints\Cards"

from engine.deferred import PLAYTIME_HOOKS            # noqa: E402  单一来源（rule 与本工具共用）
_ORDER_LIKE = ("order",)

_RE_TYPE = re.compile(r"enum Type = ETypeEnum::(\w+)")
_RE_TEXT = re.compile(r'FText Text = FText\("((?:[^"\\]|\\.|"")*)"\)')
_RE_HANDLER = re.compile(r"public void (On\w+)\(")
_RE_USED = re.compile(r"usedTriggers = \{([^}]*)\}")
_RE_ABILITY = re.compile(r'CustomAbilityAdd\("([^"]+)"')
_RE_NAME = re.compile(r'FName Name = FName\("([^"]+)"\)')
_RE_TITLE = re.compile(r'FText title = FText\("([^"]*)"\)')


def classify_duration(text: str, body: str) -> str:
    """持续时间（启发式）：按卡面文字关键字 + 调用名。"""
    t = (text or "").lower()
    if "until end of turn" in t or "this turn" in t or "until the end of the turn" in t:
        return "本回合"
    if "destroyEndOfTurn" in body or "AddAttackUntilEndOfTurn" in body:
        return "本回合"
    if "next turn" in t or "start of your next" in t or "next enemy turn" in t:
        return "下回合"
    if "countdown" in t or "SetCountdown" in body:
        return "倒计时"
    if "each turn" in t or "every turn" in t or "at the end of your turn" in t or "at the start of your turn" in t:
        return "每回合/常驻"
    return "常驻(未写期限)"


def classify_scope(body: str) -> str:
    """范围（启发式）：遍历全盘/按方取牌 = 一批单位；否则自身/一侧。"""
    if "GetAllCardsOnBoard" in body or "GetCardsOnBoardBySide" in body or "GetAllUnitsOnBoard" in body:
        return "场上一批单位"
    if "GetCardsInHandBySide" in body or "GetAllCardsInHand" in body:
        return "手牌"
    if "Deck" in body:
        return "牌库"
    return "自身/一侧"


def scan(exports: str):
    files = [f for f in glob.glob(os.path.join(exports, "**", "card_*.cpp"), recursive=True)
             if "card_display" not in os.path.basename(f)]
    rows = []
    for f in files:
        s = open(f, encoding="utf-8", errors="replace").read()
        m_t = _RE_TYPE.search(s)
        typ = m_t.group(1) if m_t else "?"
        handlers = sorted(set(_RE_HANDLER.findall(s)))
        m_u = _RE_USED.search(s)
        used = [x.strip() for x in m_u.group(1).split(",") if x.strip()] if m_u else []
        m_x = _RE_TEXT.search(s)
        text = m_x.group(1).replace('""', '"') if m_x else ""
        m_n = _RE_NAME.search(s)
        m_ti = _RE_TITLE.search(s)
        armed = [h for h in handlers if h not in PLAYTIME_HOOKS]
        abil = sorted(set(_RE_ABILITY.findall(s)))
        rows.append({
            "name": m_n.group(1) if m_n else os.path.basename(f)[:-4],
            "title": m_ti.group(1) if m_ti else "",
            "type": typ, "path": os.path.relpath(f, exports).replace("\\", "/"),
            "handlers": handlers, "used": used, "armed": armed, "abil": abil, "text": text,
            "autoplay": 'FName("autoplay")' in s,
            "countdown": "SetCountdown" in s or "DecrementCountdown" in s,
            "restrict": "AddGameplayRestriction" in s,
            "tq": "AddToTriggerQueue" in s,
            "side_eff": "GameplaySideEffect" in s,
            "gotcha": typ == "gotcha",
            "until_eot": ("AddAttackUntilEndOfTurn" in s or "destroyEndOfTurn" in s),
            "duration": classify_duration(text, s), "scope": classify_scope(s),
            "json": "JSON_Set" in s or "JSON_Add" in s,
        })
    return rows


def _fire_status():
    try:
        from engine import deferred as D
        return dict(D.FIRE_POINTS)
    except Exception:                                         # noqa: BLE001
        return {}


def render(rows, exports) -> str:
    N = len(rows)
    fire = _fire_status()
    bytype = collections.Counter(r["type"] for r in rows)
    orders = [r for r in rows if r["type"] in _ORDER_LIKE]
    o_armed = [r for r in orders if r["armed"]]
    o_plain = len(orders) - len(o_armed)
    L = []
    w = L.append
    w("# 延迟/常驻效果普查（DEFERRED-EFFECTS-CENSUS）")
    w("")
    w("*由 `tools/census_deferred.py` 生成（离线静态扫描，1.60.27292 BP 导出）；别手改，改工具后重跑。*")
    w("")
    w("口径：卡 = `Blueprints/Cards/**/card_*.cpp`（不含皮肤），共 **%d** 张。**打出即结算的钩子**（%s）不算“留下来”；"
      "其余 `public void On…` 覆写 = 留下来的钩子（armed hooks）。持续时间/范围是启发式（文字+调用名），不是语义证明。"
      % (N, "、".join(sorted(PLAYTIME_HOOKS))))
    w("")
    w("## 1. 总览")
    w("")
    w("| 类型 | 张数 |")
    w("|---|---|")
    for t, n in bytype.most_common():
        w("| %s | %d |" % (t, n))
    w("")
    w("指令（order）%d 张：**打出后仍带留下来的钩子 %d 张**，纯一次性 %d 张。" % (len(orders), len(o_armed), o_plain))
    w("")
    fam = [
        ("A 指令打出后仍挂着钩子（order + armed hook）", [r for r in rows if r["type"] == "order" and r["armed"]]),
        ("B 单位/地点常驻在场钩子（读侧从活盘面拿，不属“打出之后才布置”）",
         [r for r in rows if r["type"] not in ("order", "gotcha") and r["armed"]]),
        ("C 反制（gotcha，敌方回合从手牌触发）", [r for r in rows if r["gotcha"]]),
        ("D 授予自定义能力（CustomAbilityAdd，任意类型）", [r for r in rows if r["abil"]]),
        ("E autoplay 标签（天气/预报：回合开始才落地）", [r for r in rows if r["autoplay"]]),
        ("F 倒计时（SetCountdown/DecrementCountdown）", [r for r in rows if r["countdown"]]),
        ("G 游戏限制（AddGameplayRestriction，带期限的一侧禁令）", [r for r in rows if r["restrict"]]),
        ("H 触发队列（AddToTriggerQueue）", [r for r in rows if r["tq"]]),
        ("I 侧效果（AddGameplaySideEffect，如 blockgotcha）", [r for r in rows if r["side_eff"]]),
        ("J 回合末清除（AddAttackUntilEndOfTurn / destroyEndOfTurn）", [r for r in rows if r["until_eot"]]),
    ]
    w("| 族 | 张数 |")
    w("|---|---|")
    for name, rs in fam:
        w("| %s | %d |" % (name, len(rs)))
    w("")
    w("## 2. A 族：指令留下来的钩子（按钩子分）")
    w("")
    w("“registry 现状”取自 `engine.deferred.FIRE_POINTS`：**已接** = sim 在对应事件点会触发该钩子的预计算后果；"
      "**未接** = 打出这类指令时 `RuleV2` 会记缺口（不再记成 `vm(空)`）。")
    w("")
    by_hook = collections.defaultdict(list)
    for r in o_armed:
        for h in r["armed"]:
            by_hook[h].append(r)
    w("| 钩子 | 指令张数 | registry 现状 | 触发事件 |")
    w("|---|---|---|---|")
    for h, rs in sorted(by_hook.items(), key=lambda kv: -len(kv[1])):
        st = fire.get(h)
        w("| %s | %d | %s | %s |" % (h, len(rs), "**已接**" if st else "未接", st or "—"))
    w("")
    w("### 2.1 持续时间 × 范围（启发式；A 族每张牌各计一次）")
    w("")
    dur = collections.Counter((r["duration"], r["scope"]) for r in o_armed)
    w("| 持续时间 | 范围 | 张数 |")
    w("|---|---|---|")
    for (d, sc), n in sorted(dur.items(), key=lambda kv: -kv[1]):
        w("| %s | %s | %d |" % (d, sc, n))
    w("")
    w("### 2.2 每个钩子的牌（标题；★ = 同时有 CustomAbilityAdd 授予）")
    w("")
    for h, rs in sorted(by_hook.items(), key=lambda kv: -len(kv[1])):
        w("- **%s**（%d）：%s" % (h, len(rs), "、".join(
            ("%s%s" % (r["title"] or r["name"], "★" if r["abil"] else "")) for r in sorted(rs, key=lambda x: x["name"]))))
    w("")
    w("## 3. D 族：授予的自定义能力名")
    w("")
    ab = collections.Counter()
    ab_cards = collections.defaultdict(list)
    for r in rows:
        for a in r["abil"]:
            ab[a] += 1
            ab_cards[a].append(r["title"] or r["name"])
    w("| 能力名 | 授予它的牌数 | 举例 | 谁读它 |")
    w("|---|---|---|---|")
    readers = {"trigger": "授予者自己的钩子里 `HasCustomAbilityFromCard(\"trigger\", 授予者ID)`（ECHELON 一族）",
               "passive": "同上（`passive` 授予 + 授予者钩子过滤）", "destruction": "摧毁时触发（授予者/引擎）",
               "excess": "`ExecuteAttackCard`（已建模：`sim._excess_split`）",
               "lethal": "`CalculateDamageDealt`（已建模）", "cantRetreat": "`MakeCardRetreat`（已建模）"}
    for a, n in ab.most_common():
        w("| %s | %d | %s | %s |" % (a, n, "、".join(ab_cards[a][:3]), readers.get(a, "（见对应卡/引擎，未逐个核对）")))
    w("")
    w("## 4. 其余族的牌（标题）")
    w("")
    for name, rs in fam[2:]:
        if name.startswith("D "):
            continue
        w("- **%s**（%d）：%s%s" % (name, len(rs), "、".join((r["title"] or r["name"]) for r in sorted(rs, key=lambda x: x["name"])[:40]),
                                  "……" if len(rs) > 40 else ""))
    w("")
    w("## 5. 读法 / 局限")
    w("")
    w("* `usedTriggers` 只列了 %d 张牌有；真实钩子集合以 `public void On…` 覆写为准（本表用覆写）。" % sum(1 for r in rows if r["used"]))
    w("* 单位的“留下来的钩子”（B 族）已由读侧在活盘面上跑；**假想打出（sim 里的手牌）** 才需要 registry 把它们“布置”进 sim。")
    w("* 同一张牌可落进多族。族与族不互斥，表内张数不可相加。")
    w("* 导出目录：`%s`" % exports.replace("\\", "/"))
    w("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exports", default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "DEFERRED-EFFECTS-CENSUS.md"))
    ns = ap.parse_args(argv)
    exp = ns.exports or (DEFAULT_EXPORTS if os.path.isdir(DEFAULT_EXPORTS) else ALT_EXPORTS)
    if not os.path.isdir(exp):
        print("找不到导出目录：%s（--exports 指定）" % exp)
        return 2
    rows = scan(exp)
    md = render(rows, exp)
    with open(ns.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    print("写入 %s（%d 张牌）" % (ns.out, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
