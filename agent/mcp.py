#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp.py —— MCP server（前端②）。

    cd kards-agent && python -m agent.mcp              # 只读工具
    cd kards-agent && python -m agent.mcp --allow-actions   # 连动鼠标的也开放

为什么自己写协议而不是装 `mcp` 包
==================================
本项目的一贯做法是**只 vendor 真在用的东西**（见 `vendor/README.md`）。
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

import agentpath  # noqa: F401,E402

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
]


class Server:
    def __init__(self, allow_actions: bool = False):
        self.allow = allow_actions
        self.a = None                     # 惰性：别在 initialize 之前就去 attach 游戏

    def session(self):
        if self.a is None:
            from .session import AgentSession
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
            from . import view
            lines.append(view.render_history(acts, a._my_player_id(acts),  # noqa: SLF001
                                             a.st, tr=a.tr))
        return "\n".join(lines)

    # ---- 会动鼠标的 ----
    def _act(self, label, fn, action_type, *args):
        from . import view
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
        问不到才退回 `agent.legality.Legality.can_play_from_hand`。"""
        from .session import ACTION_TYPES
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
                return a.deploy_unit_with_target(cid, tg)
            return a.play_order_on_unit(cid, tg)

        return note + self._act("出牌 " + (a.tr(c.name) or "?"), _play,
                                ACTION_TYPES["play"], c.card_id, tid, bool(force))

    def t_attack(self, unit, target, force=False):
        from .session import ACTION_TYPES
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
        问不到才退回 `agent.legality.Legality.can_move`（借 `CanAttack` 的
        通用行动子集——游戏没有单独的 `CanMove`，那句 2026-09-25 已更正，见 legality.py）。
        ★ 2026-09-27：`CanMoveCardToLocation` 现在**默认不问**（问它要伪造
        `PC->SelectedCard`）；移动合法性由游戏在提交时判。"""
        from .session import ACTION_TYPES
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
        from .session import ACTION_TYPES
        return self._act("结束回合", self.session().end_turn, ACTION_TYPES["end"])

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
