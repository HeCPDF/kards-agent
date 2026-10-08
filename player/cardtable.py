#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""player.cardtable —— 卡牌静态特征表（`CardTable`）。

卡型键 = **FName**（`card_unit_175th_infantry_regiment` 这种资产名）——它是
三级 id（实例 uid / 对局槽位 card_id / 卡型 FName）里唯一跨局、跨启动稳定的键。
投影后的样本里可见卡只带英文显示名（`"HUMBER Mk II"`），所以本表另建
**显示名 → FName** 的反查（136 个重名标题，取"可收藏优先、非 wildcard 优先"的
规范条目）。

数据源：`server/data/cards.json`（2019 张，项目自己的逆向后端导出）。
不用官网 JSON（KARDS-NN.md §3.2：那份证实缺 444 张、多 373 张不存在的卡）。

threat_level（`UBaseCardObject::threat_level`，float @0x544，261/2019 张非零）
这一版**先留列位**（全 0）——接导出时只改 `_threat_of()` 一处，特征布局不动。

跑自检：
    cd kards-agent
    python -m player.cardtable
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# 特征布局（冻结：加列只能往后追加，旧样本重编码即可）
# ---------------------------------------------------------------------------
RARITY_NAMES = ("Common", "Uncommon", "Rare", "Unique")
FACTION_NAMES = ("Anzac", "Britain", "Finland", "France", "Germany", "Italy",
                 "Japan", "Neutral", "Poland", "Soviet", "USA", "None")
TYPE_NAMES = ("infantry", "tank", "artillery", "fighter", "bomber",
              "order", "location", "gotcha", "wildcard", "unknown")
UNIT_TYPES = frozenset(("infantry", "tank", "artillery", "fighter", "bomber"))

# cards.json 里出现过的全部关键词（2026-09-28 盘点，30 个）。冻结成位图，
# 以后新卡带新关键词时进 `keyword_extra` 计数桶，不改布局。
KEYWORD_NAMES = (
    "isInPermanentPool", "isDefenseBuff", "selectTargetOnPlayedFromHand",
    "waitWithOnOtherCardPlayedUntilHandTargetSelected", "hasDeployment",
    "hasGuard", "hasBlitz", "hasSmokescreen", "hasAmbush", "hasFury",
    "isReserved", "isRemoval", "isHQRepair", "isAttackBuff", "isDraw",
    "isDiscard", "isDirectDamage", "campaignUpgradable", "isHQDamage",
    "hasMobilize", "showBoostIcon", "hasPincer", "hasSalvage",
    "hasDestruction", "hasShock", "hasAlpine", "isKreditsBuff",
    "hasScrying", "hasCovert", "isRepair",
)

# 布局：名字 -> (起, 止)。selftest 用它逐段断言。
FEATURE_LAYOUT = {
    "cost":         (0, 1),      # kredits / 8
    "attack":       (1, 2),      # / 10
    "defense":      (2, 3),      # / 10
    "op_cost":      (3, 4),      # / 4
    "is_unit":      (4, 5),
    "is_order":     (5, 6),
    "is_location":  (6, 7),
    "is_other_type": (7, 8),
    "rarity":       (8, 12),
    "faction":      (12, 24),
    "keywords":     (24, 54),    # 30 位
    "keyword_extra": (54, 55),   # 不在冻结表里的关键词个数 / 5（截断）
    "has_attack":   (55, 56),
    "collectible":  (56, 57),
    "unknown_identity": (57, 58),
    "has_authored_threat": (58, 59),
    "threat":       (59, 60),    # 0..0.5
}
FEATURE_DIM = 60

_UNKNOWN_FNAME = "<unknown>"


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


class CardTable:
    """FName/显示名 → 静态特征向量 + 词表 id。"""

    def __init__(self, rows: list):
        self.rows = rows
        self.by_fname: dict = {}
        for r in rows:
            n = r.get("name")
            if n:
                self.by_fname[n] = r
        # 显示名反查：重名标题取规范条目（可收藏 > 非 wildcard > 先出现）
        self.title_collisions = 0
        by_title: dict = {}
        for r in rows:
            t = r.get("title")
            if not t:
                continue
            cur = by_title.get(t)
            if cur is None:
                by_title[t] = r
                continue
            self.title_collisions += 1
            if self._title_rank(r) > self._title_rank(cur):
                by_title[t] = r
        self.by_title = {t: r.get("name") for t, r in by_title.items()}
        # 词表：0 保留给 unknown
        self.vocab = {_UNKNOWN_FNAME: 0}
        for n in sorted(self.by_fname):
            self.vocab[n] = len(self.vocab)

    @staticmethod
    def _title_rank(r: dict) -> tuple:
        return (1 if r.get("collectible") else 0,
                0 if r.get("type") == "wildcard" else 1)

    # ------------------------------------------------------------- 加载
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CardTable":
        if path is None:
            path = Path(__file__).resolve().parents[2] / "server" / "data" / "cards.json"
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        return cls(rows)

    # ------------------------------------------------------------- 查询
    def fname_of_title(self, title: Optional[str]) -> Optional[str]:
        if not title:
            return None
        return self.by_title.get(title)

    def resolve(self, fname: Optional[str] = None,
                title: Optional[str] = None) -> Optional[str]:
        if fname and fname in self.by_fname:
            return fname
        if fname in self.vocab:
            return fname
        return self.fname_of_title(title)

    def id_of(self, fname: Optional[str]) -> int:
        if fname is None:
            return 0
        return self.vocab.get(fname, 0)

    def is_known(self, fname: Optional[str] = None, title: Optional[str] = None) -> bool:
        return self.resolve(fname, title) is not None

    def vector(self, fname: Optional[str] = None,
               title: Optional[str] = None) -> np.ndarray:
        """固定长度静态特征。查不到 -> 全零 + unknown_identity=1（不是抛错）。"""
        out = np.zeros(FEATURE_DIM, dtype=np.float32)
        fn = self.resolve(fname, title)
        if fn is None:
            out[FEATURE_LAYOUT["unknown_identity"][0]] = 1.0
            return out
        r = self.by_fname[fn]
        L = FEATURE_LAYOUT
        out[L["cost"][0]] = min(_f(r.get("kredits")) / 8.0, 1.0)
        out[L["attack"][0]] = min(_f(r.get("attack")) / 10.0, 1.0)
        out[L["defense"][0]] = min(_f(r.get("defense")) / 10.0, 1.0)
        out[L["op_cost"][0]] = min(_f(r.get("operation_cost")) / 4.0, 1.0)
        typ = r.get("type")
        out[L["is_unit"][0]] = 1.0 if typ in UNIT_TYPES else 0.0
        out[L["is_order"][0]] = 1.0 if typ == "order" else 0.0
        out[L["is_location"][0]] = 1.0 if typ == "location" else 0.0
        out[L["is_other_type"][0]] = 1.0 if typ not in (
            UNIT_TYPES | {"order", "location"}) else 0.0
        rar = r.get("rarity")
        if rar in RARITY_NAMES:
            out[L["rarity"][0] + RARITY_NAMES.index(rar)] = 1.0
        fac = r.get("faction")
        out[L["faction"][0] + (FACTION_NAMES.index(fac) if fac in FACTION_NAMES
                               else FACTION_NAMES.index("None"))] = 1.0
        kw = r.get("keywords") or {}
        extra = 0
        for k, v in kw.items():
            if k in KEYWORD_NAMES:
                if v:
                    out[L["keywords"][0] + KEYWORD_NAMES.index(k)] = 1.0
            elif v:
                extra += 1
        out[L["keyword_extra"][0]] = min(extra / 5.0, 1.0)
        out[L["has_attack"][0]] = 1.0 if _f(r.get("attack")) > 0 else 0.0
        out[L["collectible"][0]] = 1.0 if r.get("collectible") else 0.0
        threat = self._threat_of(r)
        out[L["has_authored_threat"][0]] = 1.0 if threat != 0.0 else 0.0
        out[L["threat"][0]] = min(abs(threat) / 0.5, 1.0)
        return out

    @staticmethod
    def _threat_of(r: dict) -> float:
        """threat_level @0x544 —— 这一版没有导出接线，恒 0。

        接导出时：从蓝图默认值表读这个字段；261/2019 张非零、步长 0.05。
        只改这一个函数，特征布局与已训练权重不受影响（列位早就留好）。
        """
        return _f(r.get("threat_level"))

    # ------------------------------------------------------------- 统计
    def summary(self) -> dict:
        return {
            "n_cards": len(self.by_fname),
            "n_titles": len(self.by_title),
            "title_collisions": self.title_collisions,
            "vocab_size": len(self.vocab),
            "feature_dim": FEATURE_DIM,
        }


# ---------------------------------------------------------------------------
# 自检（离线，不需要游戏、不需要 torch）
# ---------------------------------------------------------------------------
def selftest() -> int:
    table = CardTable.load()
    s = table.summary()
    print("cards.json:", s)
    fails = 0
    L = FEATURE_LAYOUT

    def chk(name: str, ok: bool):
        nonlocal fails
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        fails += 0 if ok else 1

    chk("卡表规模 ~2019", s["n_cards"] > 2000)
    chk("特征维度 = 布局维度", FEATURE_DIM == 60
        and L["threat"][1] == FEATURE_DIM)

    # 已知卡：WE CAN DO IT!（cost=5, order, USA, Uncommon, 不可攻击）
    v = table.vector("card_event_we_can_do_it")
    chk("FName 直查：费用 5/8", abs(float(v[L["cost"][0]]) - 5 / 8) < 1e-6)
    chk("FName 直查：is_order=1、is_unit=0",
        v[L["is_order"][0]] == 1.0 and v[L["is_unit"][0]] == 0.0)
    chk("FName 直查：faction=USA",
        v[L["faction"][0] + FACTION_NAMES.index("USA")] == 1.0)
    chk("FName 直查：不是 unknown", v[L["unknown_identity"][0]] == 0.0)

    # 显示名反查（样本里可见卡只带英文显示名）
    chk("显示名反查 WE CAN DO IT!",
        table.fname_of_title("WE CAN DO IT!") == "card_event_we_can_do_it")
    chk("显示名反查的向量跟 FName 一致",
        np.array_equal(table.vector(title="WE CAN DO IT!"), v))
    chk("显示名反查 HUMBER Mk II",
        table.resolve(title="HUMBER Mk II") is not None)

    # 未知卡：给零向量 + unknown=1，不抛错
    u = table.vector(title="NOT A REAL CARD NAME")
    chk("未知卡 -> 零向量 + unknown=1",
        u[L["unknown_identity"][0]] == 1.0 and float(np.abs(u).sum()) == 1.0)

    # 反例（能失败的测试）：两张不同费卡的特征必须不同；
    # 如果这里变成相等，说明反查把所有卡都解析成了同一张 —— 必须报错。
    v2 = table.vector("card_event_overcast")
    chk("反例：不同卡的静态特征必须不同（防'全都解析到同一张'）",
        v[L["cost"][0]] != v2[L["cost"][0]])
    chk("反例：未知向量不能被当作已知卡",
        u[L["unknown_identity"][0]] != v[L["unknown_identity"][0]])

    print("features selftest: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
