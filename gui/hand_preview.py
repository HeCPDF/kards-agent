# -*- coding: utf-8 -*-
"""手牌效果预览的数据源：读游戏（只读）→ 每张手牌"打出后做什么"的指令式清单（不翻译，见 `gui.effect_text`）。

给 `gui.effect_oracle` 用，也可单独 import 做别的界面。外部 VM 空跑（`engine.effectvm.enumerate_effects`，随机点用活种子
确定地算，见 `kardsmem/rng.py`）；三选一类改用 `semantics.choosespawn.predict`。
"""
from __future__ import annotations

from typing import Optional

from . import effect_text as ET


# ---------------------------------------------------------------------------
class LiveSource:
    """`poll()` → `{"ok", "why", "seed", "rows": [{"id", "name", "lines", "gaps"}]}`；对同一 (种子, 手牌) 不重算。"""

    def __init__(self):
        self.sess = None
        self._sig = None
        self._buckets = None
        self._last: dict = {"ok": False, "why": "连接中…", "seed": None, "rows": []}

    @staticmethod
    def _names(km):
        """显示名 = FName（内部名）本身，不查标题、不翻译、不附加信息。"""
        return lambda internal: str(internal)

    def poll(self) -> dict:
        try:
            if self.sess is None:
                from agent import session as S
                self.sess = S.AgentSession(translate=False, warm=False)
            km = self.sess._kardsmem()
            from kardsmem import rng as R
            seed = R.read_seed(km)
            st = self.sess.snapshot()
            seat = getattr(st, "my_side", None)
            if seat is None:
                return {"ok": False, "why": "读不出本地座位（不在对局里？）", "seed": seed, "rows": []}
            hand = list(st.hand())
            sig = (seed, tuple(c.obj.CardID for c in hand))
            if sig == self._sig:
                return self._last
            rows = [self._row(km, st, seat, seed, c) for c in hand]
            self._sig = sig
            self._last = {"ok": True, "why": "", "seed": seed, "rows": rows}
            return self._last
        except Exception as ex:                                     # noqa: BLE001
            self.sess = None                                        # 游戏重启过：下次重新连
            self._sig = None
            return {"ok": False, "why": "未连接游戏（%s）" % ((str(ex).splitlines() or [type(ex).__name__])[0][:80]),
                    "seed": None, "rows": []}

    def _row(self, km, st, seat, seed, c) -> dict:
        from semantics.effectvm import enumerate_effects
        ptr = (getattr(c, "raw", None) or {}).get("ptr")
        names = self._names(km)
        row = {"id": c.obj.CardID, "name": c.name, "lines": [], "gaps": ""}
        if not ptr:
            row["lines"] = ["(no object pointer; effect unavailable)"]
            return row
        try:
            from player.rule import RuleV2
            r = enumerate_effects(km, ptr, 0, False, hook=None, my_side=seat, board=st, my_seat=seat,
                                  read_hooks=RuleV2._play_hooks(km, st, seat, ptr),
                                  slots=dict(getattr(st, "slots", None) or {}), rng_seed=seed, budget_s=1.0)
        except Exception as ex:                                     # noqa: BLE001
            row["lines"] = ["(VM run failed: %s)" % str(ex)[:80]]
            return row
        stop = str(r.get("stopped") or "")
        if "selectCardToDraw" in stop or "GetChooseSpawnCards" in stop:
            row["lines"] = self._choose_spawn(km, st, ptr, seed, names)
            return row
        outs = r.get("outcomes") or []
        eff = None
        if outs and any(e for _, e in outs):
            eff = dict(outs[0][1]) if len(outs) == 1 else {"outcomes": [(w, dict(e)) for w, e in outs]}
            if len(outs) > 1 and r.get("choice"):
                eff["outcomes_mode"] = "max"          # 抉择：选哪个是玩家的事（写 choose one of，不写概率/random）
        if eff is None:
            if "覆写" in stop and "没有" in stop:
                row["lines"] = ["(no on-play effect)"]
            elif r.get("complete") and not stop:
                # 钩子跑完了、什么都没记录：效果有，但按**当前盘面**没有可作用的对象（例：P-51 的“随机对一个敌方空军造成 5 点伤害”，
                # 敌方场上没有空军）。这不是“取不到”，不能写成 unavailable。
                row["lines"] = ["(on-play effect exists, but nothing happens on the current board)"]
            else:
                row["lines"] = ["(effect unavailable)"]
                row["gaps"] = stop[:120] or "VM 没给出结果"
            return row
        if _has(eff, "choose_spawn"):
            row["lines"] = self._choose_spawn(km, st, ptr, seed, names)
            return row
        if _has(eff, "forecast"):
            row["lines"] = self._forecast(km, seed, names)
            return row
        row["lines"] = ET.describe(eff, names, self._draw_resolver(km, st, seat), self._id_namer(km, st))
        if not r.get("complete"):
            row["gaps"] = "effect may be incomplete: " + (stop[:100] or "VM did not finish")
        if not r.get("exact") and seed is None:
            row["gaps"] = (row["gaps"] + "；" if row["gaps"] else "") + "random seed unreadable; random results are indicative only"
        return row

    @staticmethod
    def _id_namer(km, st):
        """场上卡 id → `FName#id`（同名多张靠 id 区分）；认不出写 `card#id`。"""
        by_id = {c.obj.CardID: c for c in (st.cards or [])}
        pool = km.names_pool()

        def nm(cid):
            c = by_id.get(cid)
            ptr = (getattr(c, "raw", None) or {}).get("ptr") if c is not None else None
            n = ((pool.fname_of(ptr) if ptr else "") or "").split("_C_")[0]
            return "%s#%s" % (n or (c.name if c is not None else "card"), cid)
        return nm

    def _draw_resolver(self, km, st, seat):
        """`draw N` → 真牌：按游戏当前的牌库顺序（`DeckCardIDs`，抽牌取下标 0，与 `engine/chain.py` 一致）取前 N 张的 FName。
        `DrawSpecificCardFromDeck` 的项直接按卡 id 取。这张牌先洗/改了牌库之后才抽的部分，顺序已变，写 `?`。读不出牌库 ⇒ None（退回 `draw N`）。"""
        try:
            from kardsmem.gs import load as load_gs
            ids = list(load_gs(km).deck_ids(seat) or [])
        except Exception:                                           # noqa: BLE001
            ids = []
        if not ids:
            return lambda eff: None
        by_id = {c.obj.CardID: c for c in (st.cards or [])}
        pool = km.names_pool()

        def fname(cid):
            c = by_id.get(cid)
            if c is None:
                return "?"
            ptr = (getattr(c, "raw", None) or {}).get("ptr")
            n = (pool.fname_of(ptr) if ptr else "") or ""
            return n.split("_C_")[0] if n else str(c.name)

        def resolve(eff):
            seq = list(eff.get("draw_seq") or [None] * int(eff.get("draw") or 0))
            if not seq:
                return None
            n_pre = int(eff.get("draw_pre", 0))
            deck, out = list(ids), []
            for i, it in enumerate(seq):
                if i >= n_pre:
                    out.append("?")                                 # 牌库已被这张牌先改过（洗牌/加牌），顺序不再可知
                elif it is None:
                    out.append(fname(deck.pop(0)) if deck else "(deck empty)")
                else:
                    if it in deck:
                        deck.remove(it)
                    out.append(fname(it))
            return ", ".join(out)
        return resolve

    def _choose_spawn(self, km, st, ptr, seed, names) -> list:
        """三选一：这张牌自己的 GetChooseSpawnCards + 活种子洗牌后的前 3 张 = 屏幕上会弹出的候选。"""
        if seed is None:
            return ["choose_spawn 1 of (?)  [random seed unreadable]"]
        try:
            from semantics import choosespawn as CS
            p = CS.predict(km, ptr, seed, st=st)
        except Exception as ex:                                     # noqa: BLE001
            return ["choose_spawn (prediction failed: %s)" % str(ex)[:80]]
        if p.get("stopped"):
            return ["choose_spawn 1 of (?)  [%s]" % p["stopped"][:90]]
        if not p["names"] and not p.get("pool"):
            return ["(nothing to pick: candidate pool is empty, e.g. the deck has no cards)"]      # 此时原版打不出这张牌
        return ["choose_spawn 1 of (%s)" % ", ".join(names(n) for n in p["names"])]

    def _forecast(self, km, seed, names) -> list:
        """预报：3 天气 × light/medium/heavy = 9 张候选（`kardsmem.forecast_pool`，池版预测，实机三档验证过）。"""
        if seed is None:
            return ["forecast from (?)  [random seed unreadable]"]
        try:
            from kardsmem.forecast_pool import TIERS, WEATHERS, predict_ptrs, read_weather_buckets, strip_serial
            if self._buckets is None:
                self._buckets = read_weather_buckets(km)
            pool = km.names_pool()
            rows = predict_ptrs(seed, self._buckets)
        except Exception as ex:                                     # noqa: BLE001
            return ["forecast (prediction failed: %s)" % str(ex)[:80]]
        out = []
        n = 0
        for w in WEATHERS:
            row = rows.get(w) or {}
            cs = []
            for t in TIERS:
                # 静态卡表里的内部名不带结尾的 `_C`（见 choosespawn.static_cards），这里对齐才查得到标题
                nm = strip_serial(pool.fname_of(row[t]) or "").removesuffix("_C") if row.get(t) else ""
                n += 1 if nm else 0
                cs.append(names(nm) if nm else "?")
            out.append("%s: %s" % (w, ", ".join(cs)))
        return ["forecast from (%d cards)" % n] + ["  " + x for x in out]


def _has(eff, key) -> bool:
    """效果摘要（含多分支）里是否带某个标记键。"""
    if not isinstance(eff, dict):
        return False
    if eff.get(key):
        return True
    return any(isinstance(sub, dict) and sub.get(key) for _w, sub in (eff.get("outcomes") or []))
