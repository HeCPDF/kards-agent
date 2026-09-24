#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session.py —— `AgentSession`：**唯一**的动词集合。

三个前端（交互 shell / MCP / NN）都套在它上面。每个动词返回**结构化结果**
（dict），渲染交给前端。这样三份实现不会漂移。

分工（不许越界）
================
    读状态   →  kardsmem / board_api（**只读内存**）
    动作     →  ops.py（**模拟鼠标**）
    本层     →  两样都不碰，只负责编排、编号、预检、回执

动作回执用的是**动作流**（§7.6g），不是"看手牌有没有变"：

    mk = self.log.mark()
    ops.act_attack(...)
    r  = self.log.receipt(mk, "XActionAttackCard")

⚠ `MatchLog.locate()` 要 ~4s，所以 `AgentSession` 在**构造时**就预热，
  免得 `mark()` 把这 4 秒算进去、把动作当成"动手前就有的"。
"""
from __future__ import annotations

import time
from typing import Optional

import agentpath  # noqa: F401  —— 接上 vendor/ 和项目根

import board_api as BA  # noqa: E402

from . import view  # noqa: E402

LOCAL, ENEMY = "local", "enemy"

# 动作类型 → 动作流里的 action_type（回执用）
ACTION_TYPES = {
    "play": "XActionPlayCardFromHand",
    "attack": "XActionAttackCard",
    "end": "XActionEndOfTurn",
    "move": "XActionMoveCardToLine",
}


class AgentSession:
    """一次会话。持有只读内存会话、动作流、本地化表、以及上一次渲染的短号表。"""

    def __init__(self, translate: bool = True, warm: bool = True):
        self.src = BA.open_source("mem")
        self.km = None              # kardsmem 只读会话（惰性）
        self.log = None             # MatchLog
        self.notify = None          # NotifyWatcher
        self._tr = (lambda s: s)
        self.st = None              # 最近一次快照
        self.handles = None         # 最近一次渲染的短号表
        self._log_cursor = 0
        if translate:
            try:
                from kardsmem.locres import translator
                self._tr = translator()
            except Exception:                                # noqa: BLE001
                pass
        if warm:
            self.warm()

    # ---------------------------------------------------------------- 基础
    def _kardsmem(self):
        if self.km is None:
            from kardsmem import attach
            self.km = attach()
        return self.km

    def warm(self) -> dict:
        """预热：把两个"首次要扫一遍 GObjects"的定位先做掉（各 ~4s）。

        ★ 不预热的话，第一条命令会莫名其妙卡 8 秒，而且 `mark()` 会把定位耗时
          算进动作窗口 —— 动作落在那段里就会被判成"没发生"。踩过一次。
        """
        t0 = time.time()
        out = {"matchlog": False, "notify": False}
        try:
            from kardsmem.matchlog import MatchLog
            self.log = MatchLog(self._kardsmem())
            out["matchlog"] = bool(self.log.locate())
            if out["matchlog"]:
                self._log_cursor = self.log.count()
        except Exception as e:                               # noqa: BLE001
            out["matchlog_error"] = str(e)
        try:
            from kardsmem.notify import NotifyWatcher
            self.notify = NotifyWatcher(self._kardsmem())
            self.notify.poll()          # 建基线，别把开局就有的当成新事件
            out["notify"] = True
        except Exception as e:                               # noqa: BLE001
            out["notify_error"] = str(e)
        out["seconds"] = round(time.time() - t0, 2)
        return out

    def tr(self, s):
        """英文 → 中文（内存里读出来的一律是英文，见 `kardsmem.locres`）。"""
        return self._tr(s) if s else s

    def snapshot(self):
        self.st = self.src.snapshot()
        self.handles = view.Handles(self.st)
        return self.st

    # ---------------------------------------------------------------- 看
    def board(self, full: bool = False, pins: bool = False) -> dict:
        st = self.snapshot()
        p = self.pins() if pins else None
        return {"state": st,
                "text": view.render_board(st, self.handles, full=full, pins=p,
                                          tr=self.tr)}

    def pins(self) -> dict:
        """{uid: True/False/None}：逐实例的**被压制**。比读位贵，按需调。"""
        from kardsmem import cards as C
        out = {}
        for c in (self.st or self.snapshot()).cards:
            if c.location not in view.UNIT_ROWS:
                continue
            ptr = (c.raw or {}).get("ptr")
            if not ptr:
                continue
            try:
                eff = C.read_live_effects(self._kardsmem(), ptr)
            except Exception:                                # noqa: BLE001
                continue
            hit = any("pinned" in (a.get("ability") or "").lower()
                      for a in eff.get("received_abilities") or [])
            if not hit:
                hit = any("pinned" in k.lower()
                          for b in eff.get("buffs_from_cards") or []
                          for k in (b.get("buffs") or {}))
            out[c.uid] = bool(hit)
        return out

    def resolve(self, token: str):
        """短号/#id/@uid → Card。盘面还没取过就先取一次。"""
        if self.st is None:
            self.snapshot()
        return view.resolve(self.st, token, self.handles)

    def inspect(self, token: str) -> dict:
        """一张卡的全部：国籍、所属方、费用、行动费、攻防、描述、词条、被贴效果、位置。"""
        c = self.resolve(token)
        if c is None:
            return {"ok": False, "error": "认不出 %r（用 board 看短号，或 #卡表id / @uid）"
                                          % token}
        from kardsmem import cards as C
        from kardsmem.names import ftext_at
        ptr = (c.raw or {}).get("ptr")
        km = self._kardsmem()
        d = {"card": c, "handle": self.handles.handle(c) if self.handles else "?"}
        d["title"] = self.tr(c.name)
        d["title_en"] = c.name
        if ptr:
            d["rules_text"] = self.tr(ftext_at(km.m, ptr, view.OFF_CARD_TEXT))
            d["flavor_text"] = self.tr(ftext_at(km.m, ptr, view.OFF_CARD_FLAVOR))
            try:
                d["effects"] = C.read_live_effects(km, ptr)
            except Exception as e:                           # noqa: BLE001
                d["effects_error"] = str(e)
        d["faction"] = view.FACTION_ZH.get((c.raw or {}).get("faction_enum"))
        if d["faction"] is None and ptr:
            d["faction"] = view.FACTION_ZH.get(km.m.u8(ptr + view.OFF_CARD_FACTION))
        d["ok"] = True
        d["text"] = view.render_inspect(d, self.st, tr=self.tr)
        return d

    # ---------------------------------------------------------------- 历史
    def history(self, tail: int = 20, mine: Optional[bool] = None) -> dict:
        """对局历史 —— **双方**每个动作（§7.6g）。

        ★ 这是目前唯一能知道"对手做了什么"的来源。`AllMatchActions` 是累积的，
          所以随时读随时有，能把开局到现在整段补齐。
        """
        if self.log is None or not self.log.locate():
            return {"ok": False, "error": "读不到动作流（不在对局里？）", "rows": []}
        rows = self.log.all()
        me = self._my_player_id(rows)
        if mine is True:
            rows = [r for r in rows if r.get("player_id") == me]
        elif mine is False:
            rows = [r for r in rows if r.get("player_id") != me]
        shown = rows[-tail:] if tail else rows
        if self.st is None:
            self.snapshot()
        return {"ok": True, "rows": shown, "total": len(rows), "me": me,
                "text": view.render_history(shown, me, self.st, tr=self.tr)}

    def _my_player_id(self, rows) -> Optional[int]:
        """本地玩家的 player_id。

        **不猜**：从 `myMatchActions` 里取（那个数组按定义只装我方动作）。
        它空的话退回"动作流里 `action_id == -1` 的那些"—— 我方动作是本地上传的，
        上传时 id 恒为 -1（§7.6g，用户确认）。
        """
        try:
            mine = self.log.mine()
            if mine:
                return mine[-1].get("player_id")
        except Exception:                                    # noqa: BLE001
            pass
        for r in reversed(rows or []):
            if r.get("action_id") == -1:
                return r.get("player_id")
        return None

    # ---------------------------------------------------------------- 判据
    def legality(self):
        """进程外判据（懒加载）。**只挑不判**（§7.6f）。"""
        if getattr(self, "_leg", None) is None:
            from .legality import Legality
            st = self.st or self.snapshot()
            self._leg = Legality(self._kardsmem(), my_seat=st.my_side_raw)
        return self._leg

    def can_attack(self, attacker, defender) -> dict:
        return self.legality().can_attack(self.st or self.snapshot(),
                                          attacker, defender)

    def can_move(self, unit) -> dict:
        return self.legality().can_move(self.st or self.snapshot(), unit)

    def can_play(self, card, target=None) -> dict:
        return self.legality().can_play_from_hand(self.st or self.snapshot(), card, target)

    def attack_targets(self, attacker) -> dict:
        """某个单位现在能打谁。★ 算不出来的**留着**标成"不知道"，不剔掉。"""
        st = self.st or self.snapshot()
        rows = self.legality().targets(st, attacker)
        from . import view
        return {"rows": rows,
                "text": view.render_targets(rows, self.handles, tr=self.tr)}

    def events(self) -> list:
        """自上次调用以来的新提示（动作流不覆盖的那部分：**拒绝理由**）。"""
        if self.notify is None:
            return []
        try:
            return [dict(r, text_zh=self.tr(r["text"])) for r in self.notify.poll()]
        except Exception:                                    # noqa: BLE001
            return []

    def new_actions(self) -> list:
        """自上次调用以来的新动作。"""
        if self.log is None or not self.log.locate():
            return []
        n = self.log.count()
        if n <= self._log_cursor:
            self._log_cursor = min(self._log_cursor, n)
            return []
        rows = self.log.since(self._log_cursor)
        self._log_cursor = n
        return rows
