#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session.py —— `AgentSession`：**唯一**的动词集合。

三个前端（交互 shell / MCP / NN）都套在它上面。每个动词返回**结构化结果**
（dict），渲染交给前端。这样三份实现不会漂移。

分工（不许越界）
================
    读状态   →  kardsmem / board_api（**只读内存**）
    动作     →  ops_inject（**游戏内合成事件**，不挪真实光标；旧的物理鼠标
                `ops.py` 已于 2026-09-27 归档为 `_archive/ops_mouse.py`）
    本层     →  两样都不碰，只负责编排、编号、预检、回执

动作回执用的是**动作流**（§7.6g），不是"看手牌有没有变"：

    mk = self.log.mark()
    self.attack(...)          # → ops_inject.attack_card
    r  = self.log.receipt(mk, "XActionAttackCard")

⚠ `MatchLog.locate()` 要 ~4s，所以 `AgentSession` 在**构造时**就预热，
  免得 `mark()` 把这 4 秒算进去、把动作当成"动手前就有的"。
"""
from __future__ import annotations

import os
import time
from typing import Optional


from kardsmem import board as BA  # noqa: E402

from agent import view  # noqa: E402
from kardsmem.gamemodel import ESide  # noqa: E402

# 动作类型 → 动作流里的 action_type（回执用）
ACTION_TYPES = {
    "play": "XActionPlayCardFromHand",
    "attack": "XActionAttackCard",
    "end": "XActionEndOfTurn",
    "move": "XActionMoveCardToLine",
}


class AgentSession:
    """一次会话。持有只读内存会话、动作流、本地化表、以及上一次渲染的短号表。"""

    def __init__(self, translate: bool = True, warm: bool = True,
                 game_gate: Optional[bool] = None):
        self.src = BA.open_source("mem")
        self.km = None              # kardsmem 只读会话（惰性）
        self.log = None             # MatchLog
        self.notify = None          # NotifyWatcher
        self._tr = (lambda s: s)
        self.st = None              # 最近一次快照
        self.handles = None         # 最近一次渲染的短号表
        self._log_cursor = 0
        # 预检优先问**游戏自己**（`agent/precheck.py`）。None = 跟随
        # `KARDS_GAME_GATE`（默认开）；显式 False 就永远走进程外 VM。
        self.use_game_gate = (game_gate if game_gate is not None
                              else os.environ.get("KARDS_GAME_GATE", "1") != "0")
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
        # my_side 读不出 => 不发短号（Handles 需要知道谁是我方；st.unknown 里已记缺口），不默认按 1 号座位
        self.handles = view.Handles(self.st) if self.st.my_side is not None else None
        return self.st

    def peek_turn_state(self, my_side=None):
        """轻量读（毫秒级）：回合号 / ActionProcess / 我方指挥点与槽数；不建整张快照。后端不支持 ⇒ None。"""
        fn = getattr(self.src, "peek_turn_state", None)
        if fn is None:
            return None
        return fn(my_side)

    # ---------------------------------------------------------------- 看
    def board(self, full: bool = False, pins: bool = False) -> dict:
        st = self.snapshot()
        p = self.pins() if pins else None
        if full:
            self.fill_fname(st)
        return {"state": st,
                "text": view.render_board(st, self.handles, full=full, pins=p,
                                          tr=self.tr)}

    def fill_fname(self, st=None) -> int:
        """给快照里的卡补**资产名**（`UBaseCardObject.Name_0 // 0x50` 的 FName）。

        ★ 2026-09-27：`board_api.Card.name` 是 **FText 标题**（`title@0x58`，可被
          `campaignName` 覆盖），而"这张牌到底是哪个模板"要看**资产名**（FName）。
          名字池在 `kardsmem.names`（`card_asset_name`），所以补在这里而不是 board_api
          （board_api 保持"不依赖 kardsmem"的分层）。返回补上的张数。
        """
        st = st or self.st or self.snapshot()
        try:
            pool = self._kardsmem().names_pool()
        except Exception:                                    # noqa: BLE001
            return 0
        n = 0
        for c in st.cards:
            ptr = (c.raw or {}).get("ptr")
            if not ptr or getattr(c, "fname", None):
                continue
            try:
                c.fname = pool.card_asset_name(ptr)
            except Exception:                                # noqa: BLE001
                continue
            n += 1 if c.fname else 0
        return n

    def pins(self) -> dict:
        """{uid: True/False/None}：逐实例的**被压制**。比读位贵，按需调。"""
        from kardsmem import cards as C
        out = {}
        for c in (self.st or self.snapshot()).cards:
            if not c.obj.IsFieldUnit():
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

    def inspect(self, token: str, hover: bool = True,
                hover_seconds: Optional[float] = None) -> dict:
        """一张卡的全部：国籍、所属方、费用、行动费、攻防、描述、词条、被贴效果、位置。

        ★ 用户 2026-09-26 定调："inspect **友方手牌**也 hover 1.5s，**场上** 1.5s。"
          理由：真实玩家看牌一定会悬停住（卡面高亮/大卡面板），而且悬停会被转发给对手
          （`MouseHoverDispatch`）⇒ 这是"发出去的信息流"的一部分，inspect 不该是"零动作"的。
          * **友方手牌** → `hover_hand_card()`；**场上（我方/敌方）** → `hover_board_card()`；
          * 其它位置（牌库/弃牌/敌方手牌）不悬停；
          * `hover_seconds=None` ⇒ 用注入侧默认 `SETTLE_HOVER_INSPECT`（1.5 s）；
          * `hover=False` 关掉（纯只读/离线用）。
        """
        c = self.resolve(token)
        if c is None:
            return {"ok": False, "error": "认不出 %r（用 board 看短号，或 #卡表id / @uid）"
                                          % token}
        # ★ 先悬停（在场上/我方手牌才做）—— 悬停窗口里再读文字，等于"看着牌读"
        hover_res = None
        if hover:
            from agent import precheck
            fn = None
            if c.obj.IsFieldUnit():
                fn = "hover_board_card"
            elif c.obj.InHand() and self.st.my_side is not None and c.side == self.st.my_side:
                fn = "hover_hand_card"
            if fn:
                kw = {} if hover_seconds is None else {"seconds": float(hover_seconds)}
                try:
                    hover_res = precheck.call_write(fn, c.obj.CardID, **kw)
                except Exception as e:                           # noqa: BLE001
                    hover_res = {"ok": False, "error": str(e)}
        from kardsmem import cards as C
        from kardsmem.names import ftext_at
        ptr = (c.raw or {}).get("ptr")
        km = self._kardsmem()
        d = {"card": c, "handle": self.handles.handle(c) if self.handles else "?"}
        d["title"] = self.tr(c.name)
        d["title_en"] = c.name
        d["hover"] = hover_res
        if ptr:
            d["rules_text"] = self.tr(ftext_at(km.m, ptr, view.OFF_CARD_TEXT))
            d["flavor_text"] = self.tr(ftext_at(km.m, ptr, view.OFF_CARD_FLAVOR))
            # 资产名（FName@0x50）—— 与 FText 标题不是一回事，见 `fill_fname` 的注释
            try:
                c.fname = km.names_pool().card_asset_name(ptr) or c.fname
            except Exception:                                # noqa: BLE001
                pass
            try:
                d["effects"] = C.read_live_effects(km, ptr)
            except Exception as e:                           # noqa: BLE001
                d["effects_error"] = str(e)
            # ★ 2026-09-27：**游戏本体的显示值**（UI 用的就是这几个 getter，见
            #   `BP_Widget_HandCardTextV2.cpp` 的 `cardObject->getTotal*`）。进程外只读
            #   拿到的是分量（`attack + attackBuff` 等），未必等于它 ⇒ 注入侧可用时把它
            #   取回来，渲染时以它为准。取不到就退回分量（并在文本里标明来源）。
            try:
                from agent import precheck
                d["totals"] = precheck.call_read("card_totals", c.obj.CardID)
            except Exception as e:                           # noqa: BLE001
                d["totals"] = {"ok": False, "stopped": str(e)}
        d["faction"] = view.FACTION_ZH.get((c.raw or {}).get("faction_enum"))
        if d["faction"] is None and ptr:
            d["faction"] = view.FACTION_ZH.get(km.m.u8(ptr + view.OFF_CARD_FACTION))
        d["ok"] = True
        d["text"] = view.render_inspect(d, self.st, tr=self.tr)
        return d

    # ---------------------------------------------------------------- 历史
    def is_mine(self, card) -> bool:
        """`card.side == st.my_side`（最近一次快照）。my_side 读不出 => ValueError。"""
        if self.st is None:
            self.snapshot()
        return view.is_mine(self.st, card)

    def history(self, tail: int = 20, side: Optional[ESide] = None) -> dict:
        """对局历史 —— **双方**每个动作（§7.6g）。

        ★ 这是目前唯一能知道"对手做了什么"的来源。`AllMatchActions` 是累积的，
          所以随时读随时有，能把开局到现在整段补齐。
        """
        if self.log is None or not self.log.locate():
            return {"ok": False, "error": "读不到动作流（不在对局里？）", "rows": []}
        rows = self.log.all()
        me = self._my_player_id(rows)
        mine = None
        if side is not None:                  # 只看哪一方的动作：side 是 ESide；『我方』= side == my_side
            if self.st is None:
                self.snapshot()
            mine = (side == self.st.seat())
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
        """进程外判据（懒加载）。**只挑不判**（§7.6f）。

        ★ 2026-10-02：`Legality` 是**按座位构造**的（`my_seat=`），而这个缓存挂在
        session 上会活过好几局 —— 第二局换了号座位就会继续用第一局的 `my_seat`
        （跟 `kardsmem.cards._my_seat` 同族的跨局缓存）。座位读出来了且跟缓存不同
        就重建；读不出（None，换牌窗口）**不动缓存**，免得把热身好的判据反复丢掉。
        """
        st = self.st or self.snapshot()
        seat = st.my_side_raw
        leg = getattr(self, "_leg", None)
        if leg is None or (seat in (1, 2) and getattr(leg, "my_seat", None) != seat):
            from semantics.legality import Legality
            self._leg = Legality(self._kardsmem(), my_seat=seat)
        return self._leg

    def _gate(self):
        """注入式预检（**问游戏自己**）；用不了就返回 None，调用方退回 VM。

        ★ 2026-09-25 用户定调："我们预先的检测就用这个"（NN 也要用）。
          失败面（没开游戏 / 构建不匹配 / 注不进去 / `KARDS_GAME_GATE=0`）
          **一律降级**，绝不因为"问不到游戏"就把动作否掉（§7.6f）。
        """
        if not self.use_game_gate:
            return None
        from agent import precheck
        return precheck if precheck.available() else None

    @staticmethod
    def _from_game(r: dict, reason: str = "") -> dict:
        """游戏自己的答案 → 前端的统一形状（跟 `semantics.legality` 的返回同构）。

        形状必须一致，否则 shell/MCP 的渲染当场漂移；`source`/`judged_by`
        标明这句话是谁说的（前端据此改措辞，别把游戏的判据说成"进程外判据"）。
        """
        from semantics.legality import reason_zh
        out = {"ok": True, "can": bool(r.get("can")), "source": "game",
               "judged_by": r.get("source"), "reason": reason,
               "reason_zh": reason_zh(reason),
               # 游戏自己算的 ⇒ 没有掺我们的近似原语（`legality` 那边才可能有）
               "approx": [], "raw": r}
        # ⚠ 有些游戏的查询**只回 bool、不给理由**（`CanMoveCardToLocation` 就是），
        #   这时 `reason_zh("")` 会给"合法"—— 跟 `can=False` 正好说反。别让前端
        #   打出"✘ 不行：合法"这种自相矛盾的话。
        if not out["can"] and not reason:
            out["reason"] = "no_reason_given"
            out["reason_zh"] = "（游戏说不行，但没给理由）"
        for k in ("kredits", "cost", "location_enum"):
            if k in r:
                out[k] = r[k]
        return out

    def can_attack(self, attacker, defender) -> dict:
        g = self._gate()
        if g is not None:
            try:
                r = g.can_attack(attacker, defender, snapshot=self.st)       # 复用本次决策的快照（每次重新快照 4–6 s）
            except TypeError:                                                # 旧接口 / 测试替身没有 snapshot 参数
                r = g.can_attack(attacker, defender)
            if r.get("ok"):
                reason = r.get("fail_reason") or ""
                out = self._from_game(r, reason)
                out["fail_reason"] = reason or None
                return out
        r = self.legality().can_attack(self.st or self.snapshot(),
                                      attacker, defender)
        return r if r.get("source") else dict(r, source="vm")

    def can_move(self, unit) -> dict:
        """能不能上线。

        ★ 2026-09-25：游戏自己那个 `BattleUtilityFunctions_C::CanMoveCardToLocation`
        读的是 `PlayerController->SelectedCard`（"当前正在拖的那张卡"）——预检阶段
        没有真实拖拽，那个字段是 None，**它永远回 False**（字节码 + 实机都对上了）。
        `ops_inject.can_move_to(simulate_drag=True)` 会把那个字段临时指到这张卡上、
        问完还原，所以**True 是可信的**（游戏确认了"这张卡能放这行"）。
        ★★ 2026-09-27：那个"伪造拖拽态"**默认关掉了**（用户待办；它写的是
        `PC->SelectedCard // 0x0950` —— 手柄/拖拽态字段，与已删的"伪造 PC 拖拽"
        同族）。所以现在默认**问不到**游戏，退回进程外 VM 判据，并在结果里
        **明说"游戏闸门没问"**（不再出现 `game_says=False` 这种把"没问"读成"不行"的字样）。
        要诊断可显式 `precheck.can_move_to(unit, simulate_drag=True)`。
        """
        if self.use_game_gate:
            from agent import precheck
            r = precheck.can_move_to(unit, precheck.LOC_FRONTLINE)
            if r.get("ok") and r.get("can"):
                return self._from_game(r, "")
            game_no = r
        else:
            game_no = None
        r = self.legality().can_move(self.st or self.snapshot(), unit)
        out = r if r.get("source") else dict(r, source="vm")
        # ★ 不是 `_from_game`——游戏那句不能当结论（见 docstring），只能当参考信息。
        if game_no is not None:
            if game_no.get("reason") == "cannot_ask_without_drag":
                out["game_gate"] = "not_asked"      # 没问 ≠ 说不行
                out["game_gate_reason"] = game_no.get("note")
            else:
                out["game_says"] = bool(game_no.get("can"))
                out["game"] = game_no
        return out

    def can_play(self, card, target=None) -> dict:
        """出牌预检。

        ★ 2026-09-25：**默认问游戏自己**（`BP_Logic_C::CanPlayCardFromHand` 总闸，
        不行再下钻取理由——见 `agent/precheck.py`）。给了 `target` 才退回进程外 VM：
        总闸只收 `CardID`，内部读的是**当前**指向状态，而预检阶段还没有箭头，
        问它得不到"指向这张卡时行不行"；VM 路径能挂 `GetTargetedCard` 钩子模拟
        （`tools/canplay.py` 那条已 16/16 验过）。
        """
        if target is None:
            g = self._gate()
            if g is not None:
                r = g.precheck_play(card)
                if r.get("ok"):
                    if r.get("can"):
                        return self._from_game(r, "")
                    out = self._from_game(r, r.get("reason") or "")
                    out["reason_zh"] = r.get("reason_zh") or out["reason_zh"]
                    return out
        r = self.legality().can_play_from_hand(self.st or self.snapshot(), card, target)
        return r if r.get("source") else dict(r, source="vm")

    def attack_targets(self, attacker) -> dict:
        """某个单位现在能打谁。★ 算不出来的**留着**标成"不知道"，不剔掉。"""
        st = self.st or self.snapshot()
        rows = self.legality().targets(st, attacker)
        from agent import view
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

    # ------------------------------------------------------------------
    # 动作（**注入侧写动词**）
    # ------------------------------------------------------------------
    # ★ 2026-09-26 加；2026-09-27 起**唯一**的写侧（旧 `ops.py` 物理鼠标已归档）：
    #   这一组 = `ops_inject`（进程内合成"悬停→按下→拖动/点击"，复刻真实鼠标的
    #   **事件序列**，只是不挪真实光标）——满足红线（判据是"发给服务端/对手的信息流
    #   跟真人等价"），而且不抢用户的鼠标、跟窗口焦点无关。
    #   一律经 `precheck.call_write`：同一个 attach + 同一套"调用抛异常 ⇒ 不健康 ⇒
    #   整轮停手"的判据（事故教训，handoff §十六）。
    #   每个动作的成败**看游戏自己的动作流**（返回里的 `actions`），不看副作用。
    def _cid(self, who) -> Optional[int]:
        """短号 / `#id` / `@uid` / Card / 整数 → card_id。

        ★ 2026-09-27 用户定调：**动作参数一律 card_id**，不做卡名解析
        （"因为多个同名单位在场上是有可能的"）。短号（`h1`/`e2`）本来就映射到确定 card_id。
        """
        if who is None:
            return None
        if isinstance(who, int):
            return int(who)
        cid = getattr(who, "card_id", None)
        if cid is not None:
            return int(cid)
        if isinstance(who, str):
            c = self.resolve(who)
            return int(c.obj.CardID) if c is not None else None
        return None

    def _inj(self, fn: str, *args, **kw) -> dict:
        """跑一个 `ops_inject.Injector` 上的方法（健康判据见本段开头）。"""
        from agent import precheck
        if not self.use_game_gate:
            return {"ok": False, "stopped": "KARDS_GAME_GATE=0 —— 注入式动作被关掉了"}
        return precheck.call_write(fn, *args, **kw)

    def pending(self) -> dict:
        """**现在在等什么**（只读）：二选一/三选一候选、牌库选牌、手牌选目标、
        板卡两阶段"待点目标"、箭头。三前端（shell/MCP/NN）每一步都该先问它。"""
        import time as _time
        from agent import precheck
        tm = {}

        def _timed(name, *a, **k):
            t = _time.time()
            try:
                return precheck.call_read(name, *a, **k)
            finally:
                tm[name] = round(_time.time() - t, 2)
        # 分段计时（2026-10-01：一步 pending() 曾吃掉 72 s）→ 存到 self.last_pending_timing，由 nn 写进日志
        out = {"choose_one": _timed("pick_candidates"),
               "pick_pending": _timed("pick_pending"),
               "board_target": _timed("card_being_played_from_hand"),
               "hand_target": _timed("hand_target_pending", verbose=False),
               "arrows": _timed("arrow_target_by_logic")}
        self.last_pending_timing = tm
        # ★ 幽灵提示（ops 读到 widget 已在拆/候选全不合法）⇒ 同源的 pick_pending（chooseOneActive 与
        #   isSelectingHandTarget 是同一对旗标、同一处置位/复位）也是幽灵；没有真候选行时一并摘掉。
        ht = out.get("hand_target")
        pp = out.get("pick_pending")
        if (isinstance(ht, dict) and ht.get("ghost") and isinstance(pp, dict) and pp.get("pending")
                and not (out.get("choose_one") or [])):
            out["pick_pending"] = dict(pp, pending=False, stale=ht.get("ghost"))
        out["waiting"] = bool((out["choose_one"] or [])
                              or (out["pick_pending"] or {}).get("pending")
                              or out["board_target"]
                              or (out["hand_target"] or {}).get("pending"))
        return out

    def observe_prompt(self):
        """**只读**：游戏现在挂着的提示 → `agent.promptinfo.PromptInfo`（没有 ⇒ None）。
        OPS 只报告，"答什么"由策略层决定（总纲 §7）。"""
        from agent.promptinfo import from_pending
        return from_pending(self.pending())

    def pick(self, index: int, kind: Optional[str] = None,
             trigger: Optional[int] = None) -> dict:
        """点一个抉择候选（二选一/三选一/牌库选牌）。层错位时按 `kind`/`trigger` 指定。"""
        return self._inj("pick_choice", int(index), kind=kind, trigger=trigger)

    def play(self, card, target=None, force: bool = False,
             location: str = "back", slot: Optional[int] = None) -> dict:
        """出牌**路由器**（按卡自己的覆写自动分流）。★ 动作空间请优先用四个显式动词：
        `play_card_event` / `play_card_event_with_target` / `play_card_unit` /
        `play_card_unit_with_target` —— 它们的语义写死，不需要调用方猜。

        `slot`：单位**站位** = 该排的**空隙序号**（0＝最左 … n＝最右，即"拖到谁和谁之间"，
        见 `ops_inject.gap_number`；**不是**格子号）。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        tgt = self._cid(target) if target is not None else None
        return self._inj("play_card", cid, target_id=tgt, force=bool(force),
                         location=location, slot=slot)

    def play_card_event(self, card, force: bool = False,
                        require_inactive: bool = False) -> dict:
        """**打出指令/反制**（无目标）。反制 = 在手里激活，**可重复打出 = 切换**
        （返回 `gotcha_activated_before/after`、`toggled_off`）。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        return self._inj("play_card_event", cid, force=bool(force),
                         require_inactive=bool(require_inactive))

    def play_card_unit(self, card, slot: Optional[int] = None,
                       force: bool = False) -> dict:
        """**打出单位**，`slot` 指定**站位**（该排的**空隙序号**：0＝最左 … n＝最右，
        "拖到谁和谁之间"；不给就自动挑空槽）。
        ★ 站位只在支承线内有效 —— 前线只能 `move_up()`。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        return self._inj("play_card_unit", cid, slot=slot, force=bool(force))

    def play_card_event_with_target(self, card, target) -> dict:
        """**指向性指令**：把指令打出去并指向一个场上单位（`selectTargetOnPlayedFromHand=true`）。

        语义分层（2026-09-27）：**指令一次成交**（松手那一下 `AttemptToPlayFinal` 读
        `targetOverride` 就 `PlaceHandCard`），**单位要两阶段**（见 `play_card_unit_with_target`）。
        返回里 `needs_unit_click` 非空 ⇒ 这张指令是**选项层之后**才要指目标
        （HIDDEN PLANS 那种）：先 `choose_one_with_target()`。
        """
        cid, tgt = self._cid(card), self._cid(target)
        if cid is None or tgt is None:
            return {"ok": False, "error": "认不出 card/target（%r / %r）" % (card, target)}
        return self._inj("play_card_event_with_target", cid, tgt)

    def play_card_unit_with_target(self, card, target=None, slot: Optional[int] = None) -> dict:
        """**打出"部署时需要指向"的单位**；**没有合法目标就退化成 `play_card_unit()`**。

        ★ 用户口径（2026-09-27）："如果无可指向目标，一般单位便无需指向即可打出，
        走上一路径。" ⇒ `target=None`、或游戏自己的 `CanSelectAsTarget` 说不合法时，
        直接当普通单位打出（返回里 `fell_back_to_plain=True`）。
        目标是 **card_id**（名字有歧义，不收）。
        `slot`：站位 = 该排的**空隙序号**（0＝最左 … n＝最右）。
        """
        cid, tgt = self._cid(card), (None if target is None else self._cid(target))
        if cid is None or (target is not None and tgt is None):
            return {"ok": False, "error": "认不出 card/target（%r / %r）" % (card, target)}
        return self._inj("play_card_unit_with_target", cid, tgt, slot=slot)

    def select_unit_target(self, card, target) -> dict:
        """**点选一个场上单位作为目标**（`GlobalMouseUp` 那一口）—— 语义不是"部署"。

        用于：① 单位部署后的第二阶段；② HIDDEN PLANS 那类**选项层之后**的指向
        （选项 resolve 后游戏把指令也挂进同一个"待点目标"态）。目标只收 **card_id**。
        """
        cid, tgt = self._cid(card), self._cid(target)
        if cid is None or tgt is None:
            return {"ok": False, "error": "认不出 card/target"}
        return self._inj("select_unit_target", cid, tgt)

    def select_target(self, target) -> dict:
        """点一个场上目标（"正在打的那张牌"自动从游戏字段读）。"""
        tgt = self._cid(target)
        if tgt is None:
            return {"ok": False, "error": "认不出 target"}
        return self._inj("select_target", tgt)

    def hand_target_pending(self) -> dict:
        """只读：现在是不是在等"点一张手牌当目标"（`selectTargetOnPlayedFromHand`）。"""
        from agent import precheck
        return precheck.call_read("hand_target_pending", verbose=False)

    def hand_target_legal(self, card) -> dict:
        """只读：这张手牌能不能被选（游戏自己的 `IsValidHandTarget`，跟"确认"按钮同源）。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        from agent import precheck
        r = precheck.call_read("hand_target_legal", cid)
        # ★ ops 返回的是 `is_valid`，而策略层读的是 `can`（None=问不到，不拦）。以前没翻译 ⇒ 过滤形同虚设
        #   （2026-10-06 审计）。游戏明说不合法（ok 且 is_valid=False）⇒ can=False。
        if isinstance(r, dict) and "can" not in r and r.get("ok") and r.get("is_valid") is not None:
            r = dict(r, can=bool(r["is_valid"]))
        return r

    def select_hand_target(self, card, confirm: bool = True) -> dict:
        """**点一张手牌当目标**（悬停 → 真点击 → 核对 → 点确认按钮）。

        这条链会给对手发 `toggle_select_hand_target;<id>`（信息流的一部分），
        所以不能绕过点击直接写 widget 字段。
        """
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        return self._inj("select_hand_target", cid, confirm=bool(confirm))

    def attack(self, attacker, target, force: bool = False, retry=True, snapshot=None) -> dict:
        """攻击（合成"起拖→悬停目标→写箭头目标+搬头部平面→落地"）。

        `target` 可以是短号/card_id，也可以是**目标规格串**（`"hq"` / `"front0"` /
        `"back1"` / `"guard0"`）—— 由注入侧 `resolve_target()` 解析。
        `force=True` 越过两道进程外闸门（选择界面那道**不越过**，那是机械事实）；
        `retry` 控制最多试几次（默认 3）。失败时返回里带 `game_hints`（游戏提示）。"""
        a = self._cid(attacker)
        if a is None:
            return {"ok": False, "error": "认不出 attacker %r" % (attacker,)}
        t = self._cid(target)
        if t is None:
            if isinstance(target, str) and target.strip():
                t = target.strip().lower()          # 规格串：注入侧解析
            else:
                return {"ok": False, "error": "认不出 target %r" % (target,)}
        if snapshot is not None:       # 发出前刚拍的盘面：给注入侧的 CanAttack 闸门复用（省一次 4–6 s 全量快照）
            return self._inj("attack_card", a, t, force=bool(force), retry=retry, snapshot=snapshot)
        return self._inj("attack_card", a, t, force=bool(force), retry=retry)

    def move_up(self, card, slot: Optional[int] = None, force: bool = False) -> dict:
        """上线（支援线 → 前线）。★ 付**操作费**，且步兵移动后本回合不能再攻击。
        `slot`：前线里的**空隙序号**（0＝最左 … n＝最右）。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        return self._inj("move_to_front", cid, slot=slot, force=bool(force))

    def end_turn(self, force: bool = False) -> dict:
        """结束回合（HUD 按钮组件级回调，悬停+按下+松开都发）。

        ★ 默认**拒绝**在"选择界面还开着"时结束回合 —— 那会让游戏默认选最左。"""
        return self._inj("end_of_turn", force=bool(force))

    # ---------------------------------------------------------------- 换牌（开局）
    # ★ 2026-09-27：补上（对齐已归档的 `ops.py`：mulligan_marks/act_mulligan_toggle/
    #   act_mulligan_confirm/in_mulligan）。`player/record.py` 之前只能绕过 session
    #   去直接 import ops 读标记 —— 现在这一整套回到"唯一动词集合"里。
    def mulligan_marks(self) -> list:
        """只读：换牌界面每张手牌的 `shouldDiscard` 标记（`{card_id,name,marked}`）。"""
        from agent import precheck
        return precheck.call_read("mulligan_marks") or []

    def mulligan_mark(self, card, verbose: bool = True) -> dict:
        """切换一张手牌的"要换"标记（**一次点击 = 一次翻转**，不双击）。"""
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        return self._inj("mulligan_mark", cid, verbose=verbose)

    def mulligan_confirm(self, verbose: bool = True) -> dict:
        """点换牌界面的**确认按钮**（真按钮 = 子控件四件套，不是伪造点击）。"""
        return self._inj("mulligan_confirm", verbose=verbose)

    def mulligan_done(self) -> Optional[bool]:
        """只读：换牌阶段结束了吗（True/False/None=读不出）。"""
        from agent import precheck
        return precheck.call_read("mulligan_done")

    # ---------------------------------------------------------------- 判据/驱动
    def can_act_now(self, unit) -> dict:
        """只读：这个单位**本回合能不能动**（部署病：`enterPlayOnTurn == turn` 不能动，
        除非 `blitz`）。★ 别用 `attack_left`/`movement_left` 判 —— 刚部署的单位它们
        仍然读到 1（`kardsmem/board.py:429`）。最终由游戏裁决（`can_attack`）。"""
        cid = self._cid(unit)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (unit,)}
        from agent import precheck
        return precheck.call_read("can_act_now", cid)

    def pick_layers(self, index: int = 0, kind: Optional[str] = None,
                    max_layers: int = 4) -> dict:
        """**一口气走完多层抉择链**（预报"天气→2K/4K/6K"、三层那种）。
        每层成败只看动作流（`pick_choice` 的判据）。"""
        return self._inj("pick_layers", int(index), kind=kind, max_layers=int(max_layers))

    # ---- 三个"选牌"动词，按**候选身份语义**分（2026-09-27）----
    def choose_one(self, index: int = 0, trigger=None) -> dict:
        """卡自己的选项面板（`BP_ChooseOneCard_C`）：选项文字来自 `chooseOneCards`。"""
        return self._inj("pick_choice", int(index), trigger=trigger, kind="choose_one")

    def choose_one_with_target(self, index: int, target, trigger=None) -> dict:
        """**抉择 + 带目标**（HIDDEN PLANS 那种）：先点选项，再点场上单位。

        选项是否要目标由**游戏自己的**两个字段决定：`chooseOneCards[i].
        selectTargetOnPlayedFromHand`（`UBaseCardObject+0x138`）或候选 actor 的
        `needsTarget // 0x9A4`（`pick_candidates()` 里能看到 `needs_target`）。

        ★★ 2026-09-27 实机修 bug（用户："你那次 pick 进入了 state after choose。
        说明 with_select 没成功。是 bug。"）：**点选项这一步本来就没有动作流回执** ——
        这类卡的 `XActionPlayCardFromHand{chooseOneIndex}` 要等**目标点完**才进流
        （实机 `ZActionPlayCardFromHand{cardID:16, targetCardID:27, chooseOneIndex:1}` +
        `ZActionRevealCard{27}`）。旧写法拿 `pick_choice.ok==False` 提前返回 ⇒ 永远走不到
        第二步。现在整个两步流程在 `ops_inject.choose_one_with_target()` 里（单一实现），
        闸门是**游戏自己的状态** `card_being_played_from_hand`，`ok` 由**目标那一步**
        的动作流判；选项那步的 `ok=False` 如实留在返回里（旁证不进 ok —— 弯路 #22：
        判据只能往后挪一步，不能降级成旁证）。
        """
        cid = self._cid(target)
        if cid is None:
            return {"ok": False, "error": "认不出 target %r" % (target,)}
        return self._inj("choose_one_with_target", int(index), cid, trigger=trigger)

    def choose_card(self, index: int = 0, kind: Optional[str] = None,
                    is_effect: Optional[bool] = None, trigger=None) -> dict:
        """**选一张牌**：三选一 / 预报两段 / effect / scrying（看牌库顶挑）都是这一个。

        ★★ 2026-09-27 合并（用户："三选一好像 ui 表现完全相同。可以合并。对于正常玩家来说
          基本完全相同"）：代码侧也确实**同一个 notifier**（`GetChooseSpawnCards` 全导出只有
          一个调用点，在 `selectCardToDraw` 的 false 分支；true 分支同样 notify）⇒ 对调用方
          一个动词就够。候选身份怎么读（`Name_0` vs actor `CardID`）由读侧处理。
        `kind`：只在**同一时刻两类候选都在场**时消歧（`"choose_spawn"` / `"choose_draw"` /
        `"choose_one"`）；不给就按活跃层规则自动挑，真歧义时如实报 `ambiguous`（不猜）。
        ★ 单击即提交；只有 `pick_hand` 是两步（选 + 确认按钮）。
        """
        return self._inj("choose_card", int(index), kind=kind, is_effect=is_effect,
                         trigger=trigger)

    def card_totals(self, card) -> dict:
        """只读：**游戏本体的显示值**（`getTotalAttack/Defense/KreditCost/OperationCost`）。

        卡面 UI 读的就是这几个 getter（`BP_Widget_HandCardTextV2.cpp`）⇒ 被贴膜/被减费时
        要和屏幕一致就用它，别用"分量相加"。
        """
        cid = self._cid(card)
        if cid is None:
            return {"ok": False, "error": "认不出 %r" % (card,)}
        from agent import precheck
        return precheck.call_read("card_totals", cid)

    def pending_summary(self) -> dict:
        """只读：现在在等什么（抉择/手牌目标/板卡待点目标/箭头/队列）。"""
        from agent import precheck
        return precheck.call_read("pending_summary")

    def surrender(self, confirm: bool = False) -> dict:
        """投降（**不可逆**，游戏没有二次确认）⇒ 必须显式 `confirm=True`。"""
        return self._inj("surrender", confirm=bool(confirm))

    def is_pinned(self, card):
        """只读：这张牌是不是被**压制**（不能移动/攻击）。返回 True/False/None。"""
        cid = self._cid(card)
        if cid is None:
            return None
        from agent import precheck
        return precheck.call_read("is_pinned", cid)

    def find_card(self, card):
        """只读：拿我方这张牌的盘面记录（`board_api.Card`）。"""
        cid = self._cid(card)
        if cid is None:
            return None
        from agent import precheck
        return precheck.call_read("find_card", cid)

    def resolve_target(self, spec) -> dict:
        """只读：目标规格 → card_id（`hq`/`mhq`/`front<i>`/`back<i>`/`guard<i>`）。

        ★ 不收卡名：同名单位会有歧义，**动作参数一律 card_id**（用户 2026-09-27 定调）。
        """
        from agent import precheck
        return precheck.call_read("resolve_target", spec)

    def pick_target(self, exclude=(), side: Optional[ESide] = None, prefer_frontline: bool = True) -> dict:
        """只读：给"要选一个敌方目标"的动作**挑**一个目标（启发式，不算判据）。

        `side` 是 `ESide`；缺省 = 对方（`st.other_side`），本地座位读不出 => ValueError（不默认按 1）。
        """
        if side is None:
            st = self.st or self.snapshot()
            if st.other_side is None:
                raise ValueError("本地座位 mySide 读不出：无法确定『对方』")
            side = st.other_side
        from agent import precheck
        return precheck.call_read("pick_target", exclude=exclude, side=side,
                                  prefer_frontline=prefer_frontline)

    def notify_texts(self, limit: int = 4) -> list:
        """只读：游戏刚弹的提示（**动作被拒的权威理由**）。提示短命，失败当场调。"""
        from agent import precheck
        return precheck.call_read("notify_texts", limit=limit)

    def preflight(self, card=None, target=None, action: Optional[str] = None) -> dict:
        """只读：动手前自检（选择界面开着没有 + 可选跑一次对应判据）。**不拦任何东西**。"""
        cid = self._cid(card) if card is not None else None
        from agent import precheck
        return precheck.call_read("preflight", cid, target=target, action=action)

    def wait_our_turn(self, limit: float = 300.0, poll: float = None) -> bool:
        """只读：等我方回合（每 1s 采一次快照）。对局结束 / 超时都回 False。"""
        from agent import precheck
        if poll is not None:
            return bool(precheck.call_read("wait_our_turn", limit=limit, verbose=False, poll=poll))
        return bool(precheck.call_read("wait_our_turn", limit=limit, verbose=False))

    def activate_countermeasure(self, card) -> dict:
        """**挂反制**：gotcha 卡"在手里激活"（`play` 就是激活）。

        已激活的会被 `play_card` 拦下（`gotchaActivated > 0`），因为再 play 一次是**开关**、
        会取消激活。
        """
        return self.play(card)
