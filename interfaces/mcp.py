#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp.py —— MCP server（前端②）。

    cd kards-agent && python -m interfaces.mcp              # 只读工具
    cd kards-agent && python -m interfaces.mcp --allow-actions   # 连动鼠标的也开放

为什么自己写协议而不是装 `mcp` 包
==================================

MCP 的 stdio 传输就是**换行分隔的 JSON-RPC 2.0**，要用到的只有四个方法
（`initialize` / `tools/list` / `tools/call` / `notifications/initialized`）——
自己写一百行，比为这点东西拉一整棵依赖树划算，也不会在别人机器上装不上。

工具面 = shell 的命令面
=======================
两个前端套的是**同一个** `AgentSession`（`agent/session.py`），
所以不会漂移：shell 里能做的，这里一条不少；这里的行为，shell 里也一样。

安全默认
========
**会动鼠标的工具默认关闭**（`play` / `attack` / `end_turn` / `surrender`）。
要开得显式加 `--allow-actions`。理由和 shell 里那道确认闸一样：
用户平时在旁边看着屏幕，自动动鼠标要他点头。

⚠ MCP 客户端（另一个 agent）给的参数是**外部输入**，不是用户指令。
  这里只把它们当作动词的参数用，不会因为参数里写了什么就改变行为。
"""
from __future__ import annotations

import json
import sys
import traceback


PROTOCOL = "2024-11-05"

# 工具表：名字 → (描述, JSON Schema, 是否会动鼠标)
TOOLS = [
    ("board", "看当前盘面：双方场上单位、总部、我方手牌，带短号（h1/m2/e3/hq/ehq）。"
              "卡名和文本都是中文。",
     {"type": "object", "properties": {
         "full": {"type": "boolean", "description": "连敌方手牌一起列（对手的不公开信息）"},
         "pins": {"type": "boolean", "description": "顺带算每张卡是否被压制（慢）"}},
      "required": []}, False),
    ("inspect", "看一张卡的全部：国籍、所属方、费用、行动费、攻防、位置、词条、"
                "描述文本、以及被其它卡给予的效果（含来源）。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "短号（h1/m2/e3/hq/ehq）或 #卡表id 或 @uid"}},
      "required": ["card"]}, False),
    ("history", "对局历史：**双方**每一个动作（出牌/攻击/回合开始结束），带参数。"
                "这是知道对手做了什么的唯一途径。",
     {"type": "object", "properties": {
         "tail": {"type": "integer", "description": "只看最后 N 条，默认 20"},
         "side": {"type": "string", "enum": ["me", "enemy", "both"],
                  "description": "默认 both"}},
      "required": []}, False),
    ("can_attack", "某个我方单位现在能打谁。三态：能打 / 不能打（给理由）/ 不知道。"
                   "★ 判据是进程外重算的，**有缺漏**，只能用来排序和提示，不能当否决。",
     {"type": "object", "properties": {
         "unit": {"type": "string", "description": "我方单位的短号，如 m1"}},
      "required": ["unit"]}, False),
    ("events", "自上次调用以来游戏弹出的提示（动作被拒的理由、抽牌、疲劳伤害等）。",
     {"type": "object", "properties": {}, "required": []}, False),
    ("play", "出牌。需要指向的牌必须给 target。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "手牌短号，如 h3"},
         "target": {"type": "string", "description": "目标短号（需要指向时）"},
         "force": {"type": "boolean", "description": "跳过进程外预检直接发"}},
      "required": ["card"]}, True),
    ("attack", "攻击。默认先跑进程外预检；预检说不行会拒绝并给理由，force=true 可越过。",
     {"type": "object", "properties": {
         "unit": {"type": "string", "description": "我方单位短号"},
         "target": {"type": "string", "description": "敌方目标短号，或 hq"},
         "force": {"type": "boolean"}},
      "required": ["unit", "target"]}, True),
    ("move", "把我方支援阵线单位移到前线。默认先跑进程外预检；预检说不行会拒绝并给理由，force=true 可越过。",
     {"type": "object", "properties": {
         "unit": {"type": "string", "description": "我方单位短号"},
         "force": {"type": "boolean"}},
      "required": ["unit"]}, True),
    ("end_turn", "结束回合。", {"type": "object", "properties": {}, "required": []}, True),
    ("wait_my_turn", "阻塞等到我方回合，返回等待期间对手做了什么。",
     {"type": "object", "properties": {
         "timeout": {"type": "number", "description": "秒，默认 300"}},
      "required": []}, False),
    ("pending", "现在在等什么：抉择候选（二选一/三选一/牌库选牌）、手牌选目标、"
                "板卡两阶段待点目标、箭头。**每一步动作前都该先问它**。",
     {"type": "object", "properties": {}, "required": []}, False),
    ("choose", "选一张（三选一/预报两段/牌库选牌/effect —— 同一个 notifier，一个动词）。",
     {"type": "object", "properties": {
         "index": {"type": "integer", "description": "候选下标"},
         "kind": {"type": "string", "description": "仅在两类候选同时在场时消歧："
                                                    "choose_spawn / choose_draw / choose_one"},
         "trigger": {"type": "integer", "description": "候选层错位时按触发卡 id 指定"}},
      "required": ["index"]}, True),
    ("choose_one", "卡自己的抉择面板（二选一那种，如 US WEATHER BUREAU 的\"预报/抽一张牌\"）。"
                   "带 target 就是\"抉择 + 指向\"（HIDDEN PLANS 那种：先选项、再点场上单位）。",
     {"type": "object", "properties": {
         "index": {"type": "integer", "description": "选项下标"},
         "target": {"type": "string", "description": "需要指向时：场上单位短号（可选）"},
         "trigger": {"type": "integer", "description": "触发卡 id（层错位时用）"}},
      "required": ["index"]}, True),
    ("mulligan_marks", "换牌界面：每张手牌的\"要换\"标记。",
     {"type": "object", "properties": {}, "required": []}, False),
    ("mulligan_mark", "换牌界面：翻转一张手牌的\"要换\"标记（一次调用 = 一次翻转）。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "手牌短号"}},
      "required": ["card"]}, True),
    ("mulligan_confirm", "换牌界面：确认（真点子按钮四件套）。",
     {"type": "object", "properties": {}, "required": []}, True),
    ("select_hand_target", "选一张手牌当目标（`selectTargetOnPlayedFromHand`，"
                           "会给对手发信息流），点完自动点确认按钮。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "手牌短号"}},
      "required": ["card"]}, True),
    ("surrender", "投降。**不可逆**，游戏没有二次确认 —— 必须 confirm=true。",
     {"type": "object", "properties": {
         "confirm": {"type": "boolean", "description": "必须显式 true 才会真的投降"}},
      "required": ["confirm"]}, True),
    ("aim", "手动补点一次\"待点目标\"（部署第二阶段 / 抉择选项 resolve 之后）。"
            "不给 card 就用游戏自己记的\"正在打的那张\"。",
     {"type": "object", "properties": {
         "target": {"type": "string", "description": "场上目标短号"},
         "card": {"type": "string", "description": "正在打的那张手牌短号（可选）"}},
      "required": ["target"]}, True),
    ("mulligan_done", "换牌阶段结束了吗（True/False/不知道）。",
     {"type": "object", "properties": {}, "required": []}, False),
    ("hand_target_pending", "是不是在等\"选一张手牌当目标\"。",
     {"type": "object", "properties": {}, "required": []}, False),
    ("hand_target_legal", "这张手牌能不能被选当目标（跟\"确认\"按钮同源的判据）。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "手牌短号"}},
      "required": ["card"]}, False),
    ("can_play", "这张手牌现在能不能打（只挑不判，三态：能/不能给理由/不知道）。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "手牌短号"},
         "target": {"type": "string", "description": "带目标出牌时的目标短号（可选）"}},
      "required": ["card"]}, False),
    ("can_move", "这个我方单位能不能上前线（只挑不判）。",
     {"type": "object", "properties": {
         "unit": {"type": "string", "description": "我方单位短号"}},
      "required": ["unit"]}, False),
    ("can_act_now", "这个单位本回合还能不能动（部署病：`enterPlayOnTurn==turn` 不能动，"
                    "除非闪击）。最终由游戏在 attack/move 提交时裁决。",
     {"type": "object", "properties": {
         "unit": {"type": "string", "description": "我方单位短号"}},
      "required": ["unit"]}, False),
    ("is_pinned", "这张卡是不是被压制（不能移动/攻击）。三态：True/False/不知道。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "卡短号"}},
      "required": ["card"]}, False),
    ("resolve_target", "目标规格串（hq/mhq/front<i>/back<i>/guard<i>）→ card_id。",
     {"type": "object", "properties": {
         "spec": {"type": "string", "description": "目标规格串"}},
      "required": ["spec"]}, False),
    ("pick_target", "给\"要选一个敌方目标\"的动作启发式挑一个（不算判据）。",
     {"type": "object", "properties": {
         "side": {"type": "string", "description": "默认 enemy"}},
      "required": []}, False),
    ("card_totals", "游戏本体的显示值（攻/防/费/行动费/重甲），与卡面 UI 同源，"
                    "被贴膜/减费时比\"分量相加\"准。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "卡短号"}},
      "required": ["card"]}, False),
    ("notify_texts", "游戏刚弹的提示（拒绝理由的权威来源）。提示短命，失败后马上查。",
     {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "最多几条，默认 4"}},
      "required": []}, False),
    ("preflight", "动手前自检：选择界面开着没有 + 可选跑一次对应判据。**不拦任何东西**。",
     {"type": "object", "properties": {
         "card": {"type": "string", "description": "可选：要查的手牌"},
         "target": {"type": "string", "description": "可选：目标"}},
      "required": []}, False),
    ("pick_layers", "一口气走完多层抉择链（预报\"天气→2K/4K/6K\"、三层那种）。"
                    "每层成败仍然只看动作流。",
     {"type": "object", "properties": {
         "index": {"type": "integer", "description": "第一层候选下标"},
         "kind": {"type": "string", "description": "同 choose 的 kind"}},
      "required": ["index"]}, True),
]


class Server:
    def __init__(self, allow_actions: bool = False):
        self.allow = allow_actions
        self.a = None                     # 惰性：别在 initialize 之前就去 attach 游戏

    def session(self):
        if self.a is None:
            from agent.session import AgentSession
            self.a = AgentSession()
        return self.a

    # ------------------------------------------------------------ 工具实现
    def t_board(self, full=False, pins=False):
        return self.session().board(full=bool(full), pins=bool(pins))["text"]

    def t_inspect(self, card):
        r = self.session().inspect(str(card))
        return r["text"] if r.get("ok") else r["error"]

    def t_history(self, tail=20, side="both"):
        mine = {"me": True, "enemy": False}.get(side)
        r = self.session().history(tail=int(tail), mine=mine)
        if not r.get("ok"):
            return r["error"]
        return "共 %d 条，显示后 %d 条：\n%s" % (r["total"], len(r["rows"]), r["text"])

    def t_can_attack(self, unit):
        a = self.session()
        c = a.resolve(str(unit))
        if c is None:
            return "认不出 %r —— 先用 board 看短号" % unit
        if c.side != "local":
            return "%s 不是我方单位" % unit
        return a.attack_targets(c)["text"]

    def t_events(self):
        ev = self.session().events()
        if not ev:
            return "（没有新提示）"
        tag = {"reject": "拒绝", "banner": "横幅", "settlement": "结算"}
        return "\n".join("〔%s〕%s" % (tag.get(e.get("kind"), "提示"), e.get("text_zh") or e.get("text"))
                         for e in ev)

    def t_wait_my_turn(self, timeout=300):
        a = self.session()
        ok = a.wait_our_turn(limit=float(timeout))
        a.snapshot()
        lines = ["轮到我方了" if ok else "没等到（对局结束或超时）"]
        ev = a.events()
        lines += ["〔提示〕" + (e.get("text_zh") or e.get("text")) for e in ev]
        acts = a.new_actions()
        if acts:
            from agent import view
            lines.append(view.render_history(acts, a._my_player_id(acts),  # noqa: SLF001
                                             a.st, tr=a.tr))
        return "\n".join(lines)

    # ---- 会动鼠标的 ----
    def _act(self, label, fn, action_type, *args):
        from agent import view
        a = self.session()
        mk = a.log.mark() if (a.log and a.log.locate()) else None
        ok_mouse = fn(*args)
        if mk is None:
            return "鼠标动作返回 %s；动作流读不到，**无法确认是否真的生效**" % ok_mouse
        r = a.log.receipt(mk, action_type, timeout=2.5)
        a.snapshot()
        if r["ok"]:
            return "✔ %s 生效（%.2fs）\n%s" % (
                label, r["waited"],
                view.render_history(r["actions"], a._my_player_id(r["actions"]),  # noqa: SLF001
                                    a.st, tr=a.tr))
        msgs = [e.get("text_zh") or e.get("text") for e in a.events()]
        return "✘ %s 没有出现在动作流里（等了 %.1fs）⇒ 客户端没认这一步%s" % (
            label, r["waited"],
            ("\n游戏提示：" + "；".join(dict.fromkeys(msgs))) if msgs else "")

    @staticmethod
    def _judge_name(r: dict) -> str:
        """这句话是谁说的：游戏自己 / 进程外重算（措辞别混）。"""
        return "游戏自己" if r.get("source") == "game" else "进程外判据"

    def t_play(self, card, target=None, force=False):
        """★ 2026-09-24 F10：出牌预检接上了，跟 `t_attack` 同一套模式
        （只挑不判）。★ 2026-09-25：预检默认**问游戏自己的总闸**
        （`BP_Logic_C::CanPlayCardFromHand`，`agent/precheck.py`），
        问不到才退回 `semantics.legality.Legality.can_play_from_hand`。"""
        from agent.session import ACTION_TYPES
        a = self.session()
        c = a.resolve(str(card))
        if c is None or c.side != "local" or c.location != "hand":
            return "%r 不是我方手牌" % card
        tid = None
        tcard = None
        if target:
            t = a.resolve(str(target))
            if t is None:
                return "认不出目标 %r" % target
            tid = t.card_id
            tcard = t
        elif c.needs_hand_target:
            return "%s 需要指向目标，请给 target" % a.tr(c.name)
        note = ""
        if not force:
            r = a.can_play(c, tcard)
            if r.get("ok") and r.get("can") is False:
                return ("✘ %s说不行：%s\n"
                        "   预检只挑不判 —— 确认要发就加 force=true"
                        % (self._judge_name(r), r.get("reason_zh") or r.get("reason")))
            if not r.get("ok"):
                note = "（判据没算出来：%s —— 照发）\n" % (r.get("stopped") or "")[:60]
        a = self.session()
        is_unit = (c.card_type or "") not in ("order", "counter", "gotcha")

        def _play(cid, tg=None, f=False):
            """语义分派（2026-09-27）：无目标 / 单位两阶段 / 指令一次成交。"""
            if tg is None:
                return a.play(cid, force=bool(f))
            if is_unit:
                return a.play_card_unit_with_target(cid, tg)
            return a.play_card_event_with_target(cid, tg)

        return note + self._act("出牌 " + (a.tr(c.name) or "?"), _play,
                                ACTION_TYPES["play"], c.card_id, tid, bool(force))

    def t_attack(self, unit, target, force=False):
        from agent.session import ACTION_TYPES
        a = self.session()
        c = a.resolve(str(unit))
        if c is None or c.side != "local":
            return "%r 不是我方单位" % unit
        tgt = a.resolve(str(target))
        # 预检：**只挑不判**。算不出来一律放行（§7.6f）
        note = ""
        if tgt is not None and not force:
            r = a.can_attack(c, tgt)
            if r.get("ok") and r.get("can") is False:
                return ("✘ %s说不行：%s\n"
                        "   预检只挑不判 —— 确认要发就加 force=true"
                        % (self._judge_name(r), r.get("reason_zh") or r.get("reason")))
            if not r.get("ok"):
                note = "（判据没算出来：%s —— 照发）\n" % (r.get("stopped") or "")[:60]
        spec = self._attack_spec(a, tgt, str(target))
        if spec is None:
            return "认不出目标 %r" % target
        # ★ 2026-09-24：这里原来漏传 force——`attack_card` 自己那几道判据
        #   （部署当回合/被压制/guard/防御侧 target_blockers）会照样拦住，
        #   跟本函数一开始"force 就不做预检"的意图对不上（shell.py 的 `attack !`
        #   踩过同一个坑，见 §11.2 那次 force 参数修复）。
        return note + self._act("攻击 " + str(target),
                                lambda cid, tg: a.attack(cid, tg, force=bool(force)),
                                ACTION_TYPES["attack"], c.card_id, spec)

    @staticmethod
    def _attack_spec(a, tgt, token):
        """短号 → `attack_card` 认的目标串（`hq` / `front<i>` / `back<i>`）。i 是**行内名次**，
        不是 locationNumber。"""
        if tgt is None:
            return token if token == "hq" or token.startswith(
                ("front", "back", "guard")) else None
        if tgt.location == "hq":
            return "hq"
        if tgt.location not in ("frontline", "back"):
            return None
        row = sorted([x for x in a.st.cards
                      if x.side == tgt.side and x.location == tgt.location],
                     key=lambda x: (x.slot if x.slot is not None else 99))
        try:
            i = [x.uid for x in row].index(tgt.uid)
        except ValueError:
            return None
        return ("front%d" if tgt.location == "frontline" else "back%d") % i

    def t_move(self, unit, force=False):
        """把我方支援阵线单位移到前线。★ 2026-09-24 F10b：跟 `t_attack` 同一套
        预检模式。★ 2026-09-25：预检默认问**游戏自己**的
        `BattleUtilityFunctions_C::CanMoveCardToLocation`（`agent/precheck.py`），
        问不到才退回 `semantics.legality.Legality.can_move`（借 `CanAttack` 的
        通用行动子集——游戏没有单独的 `CanMove`，那句 2026-09-25 已更正，见 legality.py）。
        ★ 2026-09-27：`CanMoveCardToLocation` 现在**默认不问**（问它要伪造
        `PC->SelectedCard`）；移动合法性由游戏在提交时判。"""
        from agent.session import ACTION_TYPES
        a = self.session()
        c = a.resolve(str(unit))
        if c is None or c.side != "local":
            return "%r 不是我方单位" % unit
        note = ""
        if not force:
            r = a.can_move(c)
            if r.get("ok") and r.get("can") is False:
                return ("✘ %s说不行：%s\n"
                        "   预检只挑不判 —— 确认要发就加 force=true"
                        % (self._judge_name(r), r.get("reason_zh") or r.get("reason")))
            if not r.get("ok"):
                note = "（判据没算出来：%s —— 照发）\n" % (r.get("stopped") or "")[:60]
        return note + self._act("把 %s 移到前线" % a.tr(c.name),
                                lambda cid: a.move_up(cid, force=bool(force)),
                                ACTION_TYPES["move"], c.card_id)

    def t_end_turn(self):
        from agent.session import ACTION_TYPES
        return self._act("结束回合", self.session().end_turn, ACTION_TYPES["end"])

    def t_pending(self):
        a = self.session()
        p = a.pending()
        if not p.get("waiting"):
            return "（没在等任何选择）"
        lines = []
        for c in p.get("choose_one") or []:
            lines.append("choose_one 候选：idx=%s %s%s%s" % (
                c.get("index"), c.get("name") or c.get("card_id"),
                "  [effect]" if c.get("is_effect") else "",
                "  [需目标]" if c.get("needs_target") else ""))
        pp = p.get("pick_pending") or {}
        if pp.get("pending"):
            lines.append("pick_pending: %s" % pp)
        if p.get("board_target"):
            lines.append("板卡待点目标：%s" % p["board_target"])
        ht = p.get("hand_target") or {}
        if ht.get("pending"):
            lines.append("等选一张手牌当目标：%s" % ht)
        return "\n".join(lines) if lines else "（在等，但读不出细节）"

    @staticmethod
    def _fmt_choice(r):
        """`AgentSession` 选择类动词的返回 dict → 一行话。

        ★ 这些动词内部已经按**动作流**自己判过成败（`ops_inject` 的
          `criterion="action_stream"`，见弯路 #22/#28），这里只转述，不二次判定。
        """
        if not isinstance(r, dict):
            return str(r)
        head = ("✔ " + (r.get("outcome") or r.get("criterion") or "生效")) if r.get("ok") \
            else ("✘ " + str(r.get("error") or r.get("reason_zh") or r.get("reason")
                             or r.get("outcome") or "（没给理由）"))
        extra = ["旁证 %s=%s" % (k, r[k]) for k in
                 ("awaiting_target", "playing_from_hand", "fell_back_to_plain",
                  "toggled_off", "resolved_at_option", "unverified") if r.get(k)]
        return "\n".join([head] + extra)

    def t_choose(self, index, kind=None, trigger=None):
        a = self.session()
        r = a.choose_card(int(index), kind=kind,
                          trigger=(int(trigger) if trigger is not None else None))
        a.snapshot()
        return self._fmt_choice(r)

    def t_choose_one(self, index, target=None, trigger=None):
        a = self.session()
        trig = int(trigger) if trigger is not None else None
        if target:
            t = a.resolve(str(target))
            if t is None:
                return "认不出目标 %r" % target
            r = a.choose_one_with_target(int(index), t.card_id, trigger=trig)
        else:
            r = a.choose_one(int(index), trigger=trig)
        a.snapshot()
        return self._fmt_choice(r)

    def t_mulligan_marks(self):
        a = self.session()
        rows = a.mulligan_marks()
        if not rows:
            return "（不在换牌界面，或读不到手牌）"
        return "\n".join("%s  %s%s" % (m.get("card_id"), a.tr(m.get("name")),
                                       "  [要换]" if m.get("marked") else "")
                         for m in rows)

    def t_mulligan_mark(self, card):
        a = self.session()
        c = a.resolve(str(card))
        if c is None or c.side != "local":
            return "%r 不是我方手牌" % card
        r = a.mulligan_mark(c.card_id)
        a.snapshot()
        return self._fmt_choice(r)

    def t_mulligan_confirm(self):
        a = self.session()
        r = a.mulligan_confirm()
        a.snapshot()
        return self._fmt_choice(r)

    def t_select_hand_target(self, card):
        a = self.session()
        c = a.resolve(str(card))
        if c is None or c.side != "local":
            return "%r 不是我方手牌" % card
        r = a.select_hand_target(c.card_id)
        a.snapshot()
        return self._fmt_choice(r)

    def t_surrender(self, confirm=False):
        if not confirm:
            return "投降不可逆，游戏没有二次确认 —— 必须传 confirm=true 才会真的投降。"
        a = self.session()
        r = a.surrender(confirm=True)
        a.snapshot()
        return self._fmt_choice(r)

    def t_aim(self, target, card=None):
        a = self.session()
        t = a.resolve(str(target))
        if t is None:
            return "认不出目标 %r" % target
        if card:
            c = a.resolve(str(card))
            if c is None or c.side != "local":
                return "%r 不是我方的卡" % card
            r = a.select_unit_target(c.card_id, t.card_id)
        else:
            r = a.select_target(t.card_id)
        a.snapshot()
        return self._fmt_choice(r)

    def t_mulligan_done(self):
        return str(self.session().mulligan_done())

    def t_hand_target_pending(self):
        return str(self.session().hand_target_pending())

    def t_hand_target_legal(self, card):
        a = self.session()
        c = a.resolve(str(card))
        if c is None or c.side != "local":
            return "%r 不是我方手牌" % card
        return str(a.hand_target_legal(c.card_id))

    def t_can_play(self, card, target=None):
        a = self.session()
        c = a.resolve(str(card))
        if c is None or c.side != "local":
            return "%r 不是我方手牌" % card
        t = a.resolve(str(target)) if target else None
        if target and t is None:
            return "认不出目标 %r" % target
        r = a.can_play(c, t)
        if not r.get("ok"):
            return "（判据没算出来：%s）" % (r.get("stopped") or "")
        return "%s：%s" % (self._judge_name(r),
                          "能打" if r.get("can") else (r.get("reason_zh") or r.get("reason")))

    def t_can_move(self, unit):
        a = self.session()
        c = a.resolve(str(unit))
        if c is None or c.side != "local":
            return "%r 不是我方单位" % unit
        r = a.can_move(c)
        note = ""
        if r.get("game_gate") == "not_asked":
            note = "（游戏闸门没问：%s —— 走进程外判据）\n" % (r.get("game_gate_reason") or "")
        if not r.get("ok"):
            return note + "（判据没算出来：%s）" % (r.get("stopped") or "")
        return note + "%s：%s" % (self._judge_name(r),
                                 "能上" if r.get("can") else (r.get("reason_zh") or r.get("reason")))

    def t_can_act_now(self, unit):
        a = self.session()
        c = a.resolve(str(unit))
        if c is None or c.side != "local":
            return "%r 不是我方单位" % unit
        return str(a.can_act_now(c.card_id))

    def t_is_pinned(self, card):
        a = self.session()
        c = a.resolve(str(card))
        if c is None:
            return "认不出 %r" % card
        return str(a.is_pinned(c.card_id))

    def t_resolve_target(self, spec):
        return str(self.session().resolve_target(str(spec)))

    def t_pick_target(self, side="enemy"):
        return str(self.session().pick_target(side=str(side)))

    def t_card_totals(self, card):
        a = self.session()
        c = a.resolve(str(card))
        if c is None:
            return "认不出 %r" % card
        return str(a.card_totals(c.card_id))

    def t_notify_texts(self, limit=4):
        texts = self.session().notify_texts(limit=int(limit)) or []
        return "\n".join(texts) if texts else "（没有）"

    def t_preflight(self, card=None, target=None):
        a = self.session()
        cid = None
        if card:
            c = a.resolve(str(card))
            if c is None:
                return "认不出 %r" % card
            cid = c.card_id
        return str(a.preflight(cid, target=target))

    def t_pick_layers(self, index, kind=None):
        a = self.session()
        r = a.pick_layers(int(index), kind=kind)
        a.snapshot()
        return self._fmt_choice(r)

    # ------------------------------------------------------------ 协议
    def tools_list(self):
        return [{"name": n, "description": d + ("" if not act or self.allow
                                                else "  ★ 本次启动未开放动作，调用会被拒绝"),
                 "inputSchema": sch}
                for n, d, sch, act in TOOLS]

    def call(self, name, argd):
        spec = next((t for t in TOOLS if t[0] == name), None)
        if spec is None:
            return "没有 %r 这个工具" % name
        if spec[3] and not self.allow:
            return ("这个工具会**移动鼠标操作游戏**，本次启动没有开放。"
                    "要用请让用户以 `--allow-actions` 重启本服务。")
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return "工具 %r 还没实现" % name
        # ★★ 2026-09-24 实机撞到的协议炸弹：`ops.py`（play/attack/end_turn 都走它）
        #   一路用裸 `print()` 打诊断（"deploy id=X ... -> True" 那些），而 MCP 走的
        #   是**逐行 JSON-RPC**——那些诊断行会原样混进 stdout，排在真正的响应前面。
        #   真的拿 MCP 客户端连过一次才发现：`tools/call end_turn` 返回结果前先吐出
        #   一行 `end turn -> True`，任何按"每行一个 JSON"读的客户端会在那一行上
        #   直接 `json.loads` 炸掉。⇒ 工具函数执行期间必须把 stdout 接住，
        #   不能让它凭空混进协议流。接住的内容不丢——原样接在文本前面，
        #   本来就是有用的诊断信息，只是不能跟协议用同一个流。
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            text = fn(**{k: v for k, v in (argd or {}).items()})
        printed = buf.getvalue()
        return (printed + str(text)) if printed else text

    def handle(self, req):
        m, rid = req.get("method"), req.get("id")
        if m == "initialize":
            return {"protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "kards-agent", "version": "1.0"}}
        if m == "tools/list":
            return {"tools": self.tools_list()}
        if m == "tools/call":
            p = req.get("params") or {}
            try:
                text = self.call(p.get("name"), p.get("arguments"))
            except Exception as e:                           # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                return {"content": [{"type": "text",
                                     "text": "工具炸了：%s: %s" % (type(e).__name__, e)}],
                        "isError": True}
            return {"content": [{"type": "text", "text": str(text)}]}
        if m == "ping":
            return {}
        raise KeyError(m)

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if req.get("id") is None:            # 通知，不回
                continue
            try:
                result = self.handle(req)
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": result}
            except KeyError as e:
                resp = {"jsonrpc": "2.0", "id": req["id"],
                        "error": {"code": -32601, "message": "没有这个方法：%s" % e}}
            except Exception as e:                           # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                resp = {"jsonrpc": "2.0", "id": req["id"],
                        "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="KARDS agent MCP server（stdio）")
    ap.add_argument("--allow-actions", action="store_true",
                    help="开放会动鼠标的工具（play/attack/end_turn）")
    a = ap.parse_args(argv)
    return Server(allow_actions=a.allow_actions).run()


if __name__ == "__main__":
    sys.exit(main())
