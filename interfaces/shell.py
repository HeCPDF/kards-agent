#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell.py —— 交互式 REPL（前端①）。

    cd kards-agent && python -m interfaces.shell

命令
====
    board [full]        盘面（`full` 连敌方手牌一起列）
    pins                盘面 + 逐实例的**被压制**（慢，要读每张卡的 live effects）
    i / inspect <卡>    一张卡的全部
    log [n] [me|opp|left|right|1|2]  对局历史（双方动作，§7.6g）
    events              自上次以来的新提示（拒绝理由等）
    wait                等到我方回合
    play <手牌> [目标]   出牌
    attack <单位> <目标> 攻击（`attack ! …` 跳过预检强制发出）
    can <单位>          这个单位现在能打谁（进程外重算，三态）
    front <单位> [slot] 支援 → 前线（slot＝空隙序号，不给就自动挑）
    end                 结束回合
    pending             现在在等什么（抉择/手牌目标/板卡待点目标/箭头）
    choose <i> [kind] [trigger]        选一张（三选一/预报/牌库选牌/effect）
    chooseone <i> [trigger]            卡自己的抉择面板（二选一那种）
    chooseone <i> <目标> [trigger]     抉择 + 指向（HIDDEN PLANS 那种）
    aim <目标> [手牌]    手动补点一次"待点目标"（部署第二阶段/选项后指向），
                        不给手牌就用游戏自己记的"正在打的那张"
    mullmarks           换牌界面：每张手牌的"要换"标记
    mullmark <手牌>      换牌界面：翻转一张的标记
    mullgo              换牌界面：确认
    mulldone            换牌阶段结束了吗（只读）
    htarget <手牌>       选一张手牌当目标（点击 + 确认）
    htpending           是不是在等"选一张手牌当目标"（只读）
    htlegal <手牌>       这张手牌能不能被选当目标（只读）
    surrender confirm    投降（**不可逆**，必须显式打 confirm）
    canplay <手牌> [目标] 这张牌现在能不能打（只挑不判，三态）
    canmove <单位>       这个单位能不能上前线（只挑不判，三态）
    canact <单位>        这个单位本回合还能不能动（部署病/闪击）
    pinned <卡>          这张卡是不是被压制（只读，三态）
    target <规格>        规格→card_id（hq/mhq/front<i>/back<i>/guard<i>）
    picktarget [side]    给"要选一个目标"的动作启发式挑一个（不算判据）
    totals <卡>          游戏本体的显示值（攻/防/费/行动费，UI 同源）
    hints                游戏刚弹的提示（拒绝理由权威来源，短命）
    preflight [卡] [目标] 动手前自检（选择界面开着没有 + 可选判据），不拦
    picklayers <i> [kind] 一口气走完多层抉择链（预报"天气→2K/4K/6K"那种）
    refresh / r         重取盘面
    help / q

卡怎么指
========
`board` 打出来的短号：`h1` 手牌 / `m2` 我方场上 / `e3` 敌方场上 / `hq` `ehq`。
也接受 `#卡表id` 和 `@uid`。

三条规矩（和命令层一致）
========================
1. **动作前先确认目标还在。** 短号是上一次渲染的产物，对手回合可能已经把盘面改了
   ⇒ 执行前重取快照、**按 uid 复核**，对不上就拒绝并让你重看。
2. **预检只挑不判。** 算出来不合法会 `reject`，但**带 `!` 强制就照发**
   （`attack! m1 ehq`）—— 判据一定有缺漏，不能拿它否决动作（§7.6f）。
3. **动作成没成看动作流，不看副作用。** `MatchLog.receipt()`（§7.6g），
   不是"手牌有没有少一张"。

★ 会动鼠标的命令（play/attack/front/end）执行前会**再问一次**，
  除非启动时带 `--yes`。
"""
from __future__ import annotations

import sys
import time
from typing import Optional


from agent import view  # noqa: E402
from agent.session import ACTION_TYPES, AgentSession  # noqa: E402

BANNER = """KARDS agent shell —— 输入 help 看命令，q 退出
★ 读侧只读内存，动作一律走 `agent.session.AgentSession`（→ `ops_inject` 合成事件；
  `ops.py`（旧物理鼠标实现）已于 2026-09-27 归档停用）。"""


class Shell:
    def __init__(self, auto_yes: bool = False):
        print("接入中（要扫两遍 GObjects 定位，约 8 秒）……")
        t0 = time.time()
        self.a = AgentSession()
        self.yes = auto_yes
        print("就绪 %.1fs   动作流=%s  提示=%s"
              % (time.time() - t0,
                 "OK" if (self.a.log and self.a.log.locate()) else "读不到",
                 "OK" if self.a.notify else "读不到"))

    # ------------------------------------------------------------ 工具
    def _drain(self):
        """把积压的提示和新动作吐出来 —— **每条命令的响应里都带上**。"""
        ev = self.a.events()
        # ★ 同一句提示会被重发多次（`ops` 的重试阶梯每次都弹一条）⇒ 合并计数，
        #   但**不丢弃** —— 次数本身是信息（发了几次都被拒）。
        seen = []
        for e in ev:
            t = e.get("text_zh") or e.get("text")
            if seen and seen[-1][0] == t:
                seen[-1][1] += 1
            else:
                seen.append([t, 1])
        for t, n in seen:
            print("  〔提示〕%s%s" % (t, ("  ×%d" % n) if n > 1 else ""))
        acts = self.a.new_actions()
        if acts:
            from agent import view
            me = self.a._my_player_id(acts)                  # noqa: SLF001
            print(view.render_history(acts, me, self.a.st, tr=self.a.tr))

    def _confirm(self, what: str) -> bool:
        if self.yes:
            return True
        try:
            return input("  即将%s —— 回车确认 / 输入 n 取消：" % what).strip().lower() != "n"
        except EOFError:
            return False

    def _card(self, token: str, mine: bool = False):
        """解析一张卡；`mine=True` 时要求是我方的卡（`card.side == st.my_side`）。"""
        c = self.a.resolve(token)
        if c is None:
            print("  认不出 %r。先 `board` 看短号，或用 #卡表id / @uid" % token)
            return None
        if mine and not self.a.is_mine(c):
            print("  %s 是%s的卡" % (token, view.side_zh(self.a.st, c.side)))
            return None
        return c

    @staticmethod
    def _judge_name(r: dict) -> str:
        """这句话是谁说的：游戏自己 / 进程外重算。**别把两者混着叫"判据"。**"""
        return ("游戏自己" if r.get("source") == "game" else "进程外判据")

    def _print_result(self, r):
        """打印一个 `AgentSession` 写动词的返回 dict。

        ★ 这些动词（choose/mulligan/select_hand_target/surrender…）内部已经
          按**动作流**自己判过成败（`ops_inject` 的 `criterion="action_stream"`
          那一套，见弯路 #22/#28），**不再**在这一层用 `mark()/receipt()` 二次
          判定——那样反而会跟内部判据打架。这里只负责如实转述。
        """
        if not isinstance(r, dict):
            print("  " + str(r))
            return
        if r.get("ok"):
            print("  ✔ " + (r.get("outcome") or r.get("criterion") or "生效"))
        else:
            why = r.get("error") or r.get("reason_zh") or r.get("reason") or r.get("outcome") or "（没给理由）"
            print("  ✘ " + str(why))
        for k in ("awaiting_target", "playing_from_hand", "fell_back_to_plain",
                  "toggled_off", "resolved_at_option", "unverified"):
            if r.get(k):
                print("     旁证：%s=%s" % (k, r[k]))
        self.a.snapshot()

    def _act(self, label: str, fn, action_type: Optional[str], *args):
        """跑一个鼠标动作，并用**动作流**判成败（不是看副作用）。"""
        if not self._confirm(label):
            print("  取消")
            return
        mk = self.a.log.mark() if (self.a.log and self.a.log.locate()) else None
        try:
            ok_mouse = fn(*args)
        except Exception as e:                               # noqa: BLE001
            print("  执行层抛了：%s" % e)
            return
        if mk is None:
            print("  鼠标动作返回 %s（动作流读不到，无法确认是否真的生效）" % ok_mouse)
            return
        r = self.a.log.receipt(mk, action_type, timeout=2.5)
        if r["ok"]:
            from agent import view
            me = self.a._my_player_id(r["actions"])          # noqa: SLF001
            print("  ✔ 生效（%.2fs）" % r["waited"])
            print(view.render_history(r["actions"], me, self.a.st, tr=self.a.tr))
        else:
            print("  ✘ 动作流里没有出现（等了 %.1fs）⇒ 客户端没认这一步" % r["waited"])
            self._drain()                # 拒绝理由在提示里
        self.a.snapshot()

    # ------------------------------------------------------------ 命令
    def do_board(self, arg=""):
        full = arg.strip() == "full"
        print(self.a.board(full=full)["text"])

    def do_pins(self, arg=""):
        print(self.a.board(pins=True)["text"])

    def do_inspect(self, arg=""):
        if not arg.strip():
            print("  用法：inspect <卡>")
            return
        r = self.a.inspect(arg.strip())
        print(r["text"] if r.get("ok") else "  " + r["error"])

    def do_log(self, arg=""):
        parts = arg.split()
        n, side_tok = 20, None
        for p in parts:
            if p in ("1", "2"):                # 座位号（整数 1/2 歧义时当座位；条数请写 >=3 的数）
                side_tok = p
            elif p.isdigit():
                n = int(p)
            else:
                side_tok = p
        side = None
        if side_tok is not None:              # 输入入口一次性解析成 ESide
            if self.a.st is None:
                self.a.snapshot()
            try:
                side = view.parse_side(side_tok, self.a.st.my_side)
            except ValueError as e:
                print("  " + str(e))
                return
        r = self.a.history(tail=n, side=side)
        if not r.get("ok"):
            print("  " + r["error"])
            return
        print("对局历史（共 %d 条，显示后 %d 条）：" % (r["total"], len(r["rows"])))
        print(r["text"])

    def do_events(self, arg=""):
        ev = self.a.events()
        if not ev:
            print("  （没有新提示）")
        tag = {"reject": "拒绝", "banner": "横幅", "settlement": "结算"}
        for e in ev:
            print("  〔%s〕%s" % (tag.get(e.get("kind"), "提示"), e.get("text_zh") or e.get("text")))

    def do_wait(self, arg=""):
        print("  等我方回合……（Ctrl-C 中断）")
        try:
            ok = self.a.wait_our_turn(limit=float(arg) if arg.strip() else 300)
        except KeyboardInterrupt:
            print("  中断")
            return
        print("  " + ("轮到我方了" if ok else "没等到（对局结束或超时）"))
        self.a.snapshot()
        self._drain()

    def do_play(self, arg=""):
        force = arg.strip().startswith("!")
        parts = arg.lstrip("! ").split()
        if not parts:
            print("  用法：play <手牌> [目标]   （play ! … 强制发出）")
            return
        c = self._card(parts[0], mine=True)
        if c is None:
            return
        if not c.obj.InHand():
            print("  %s 不在手牌里（在%s）" % (parts[0], view.row_of(c)))
            return
        tgt = None
        tcard = None
        if len(parts) > 1:
            t = self._card(parts[1])
            if t is None:
                return
            tgt = t.obj.CardID
            tcard = t
        elif c.obj.selectTargetOnPlayedFromHand:
            # ★ 指向类的牌缺参数就**问**，不自己挑（规格 §11.1 F1）
            print("  %s 需要指向目标。用 `play %s <目标>` 指定。"
                  % (self.a.tr(c.name), parts[0]))
            return
        # ★ 2026-09-24 F10：出牌预检接上了——跑这张卡自己的 `CanPlayFromHand`
        #   覆写（438 张里有的才跑得动），跟 `attack`/`front` 一样只挑不判。
        #   ★ 2026-09-25：默认改成**问游戏自己的总闸**（`BP_Logic_C::CanPlayCardFromHand`），
        #   问不到才退回进程外 VM；措辞跟着 `source` 走，别把游戏的判据说成"进程外判据"。
        if not force:
            r = self.a.can_play(c, tcard)
            if r.get("ok") and r.get("can") is False:
                print("  ✘ %s说不行：%s" % (self._judge_name(r),
                                          r.get("reason_zh") or r.get("reason")))
                print("     —— 预检只挑不判。确认要发就用： play ! %s" % " ".join(parts))
                return
            if not r.get("ok"):
                print("  （判据没算出来：%s —— 照发）" % (r.get("stopped") or "")[:60])
        self._act("出牌 %s%s%s" % (self.a.tr(c.name),
                                  ("→" + parts[1]) if tgt else "",
                                  "（强制）" if force else ""),
                  self._play_fn(c, tgt, force),
                  ACTION_TYPES["play"], c.obj.CardID, tgt)

    def _play_fn(self, card, tgt, force: bool):
        """出牌的执行口 —— **按语义分派**（2026-09-27 语义分层）：

          * 无目标 ⇒ `play()`（拖到落点松手）；
          * **单位** + 目标 ⇒ `play_card_unit_with_target()`（两阶段：落地 → 点目标）；
          * **指令** + 目标 ⇒ `play_card_event_with_target()`（松手那一次即成交）。

        旧版一律走 `ops.play_card_from_hand` —— 那个实现把三条链塞在一个名字里，
        而 `ops.py` 已归档。
        """
        is_unit = (card.card_type or "") not in ("order", "counter", "gotcha")

        def fn(cid, tg=None):
            if tg is None:
                return self.a.play(cid, force=force)
            if is_unit:
                return self.a.play_card_unit_with_target(cid, tg)
            return self.a.play_card_event_with_target(cid, tg)
        return fn

    def do_can(self, arg=""):
        """`can <我方单位>` —— 这个单位现在能打谁（进程外重算，只挑不判）。"""
        if not arg.strip():
            print("  用法：can <我方单位>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        print("%s 现在能打谁：" % self.a.tr(c.name))
        print(self.a.attack_targets(c)["text"])

    def do_attack(self, arg=""):
        force = arg.strip().startswith("!")
        parts = arg.lstrip("! ").split()
        if len(parts) < 2:
            print("  用法：attack <我方单位> <目标>   （attack ! … 强制发出）")
            return
        c = self._card(parts[0], mine=True)
        if c is None:
            return
        t = self._attack_spec(parts[1])
        if t is None:
            return
        # ★ 预检**只挑不判**：算出来不合法只是 reject，带 `!` 就照发（§7.6f）。
        #   算不出来（stopped）**一律放行** —— 判据缺漏不能变成否决权。
        tgt = self.a.resolve(parts[1])
        if tgt is not None and not force:
            r = self.a.can_attack(c, tgt)
            if r.get("ok") and r.get("can") is False:
                print("  ✘ %s说不行：%s" % (self._judge_name(r),
                                          r.get("reason_zh") or r.get("reason")))
                print("     —— 预检只挑不判。确认要发就用： attack ! %s" % " ".join(parts))
                return
            if not r.get("ok"):
                print("  （判据没算出来：%s —— 照发）" % (r.get("stopped") or "")[:60])
        self._act("用 %s 攻击 %s%s" % (self.a.tr(c.name), parts[1],
                                       "（强制）" if force else ""),
                  lambda cid, tgt: self.a.attack(cid, tgt, force=force),
                  ACTION_TYPES["attack"], c.obj.CardID, t)

    def _attack_spec(self, token: str):
        """短号 → `ops.attack_card` 认的目标串（`hq` / `front<i>` / `back<i>`）。

        ★ `front<i>` / `back<i>` 里的 i 是**该行内的名次**（按 slot 排序后的下标），
          **不是 `locationNumber`**。踩过一次：把 slot=2 直接写成 `back2`，
          而敌方后排 [slot0, slot2, slot3] 的名次是 [0,1,2] ⇒ 打到了第三张。
          游戏还正确地拒绝了（那张是"被守护"），否则这个 bug 会静悄悄打错目标。
        """
        c = self.a.resolve(token)
        if c is None:
            # 也允许直接写 ops 的原生写法
            if token in ("hq",) or token.startswith(("front", "back", "guard")):
                return token
            print("  认不出目标 %r" % token)
            return None
        if self.a.is_mine(c):
            print("  %s 是我方的卡，不能当攻击目标" % token)
            return None
        crow = view.row_of(c)
        if crow == "hq":
            return "hq"
        if crow not in ("frontline", "back"):
            print("  %s 不在场上（在%s）" % (token, crow))
            return None
        row = sorted([x for x in self.a.st.cards
                      if x.side == c.side and view.row_of(x) == crow
                      and (crow == "back" or x.card_type not in ("order", "counter"))],
                     key=lambda x: (x.slot if x.slot is not None else 99))
        try:
            i = [x.uid for x in row].index(c.uid)
        except ValueError:
            print("  %s 不在敌方 %s 行里（盘面变了？先 refresh）" % (token, crow))
            return None
        return ("front%d" if crow == "frontline" else "back%d") % i

    def do_front(self, arg=""):
        force = arg.strip().startswith("!")
        parts = arg.lstrip("! ").split()
        if not parts:
            print("  用法：front <我方支援单位> [slot]   （front ! … 强制发出）")
            return
        c = self._card(parts[0], mine=True)
        if c is None:
            return
        # ★ 2026-09-27：不再有"落点 x"（那是物理鼠标的坐标）。新版走**合成事件**
        #   （`move_to_front` 内部写 cursor 字段 + 搬 owner 箭头头平面过长度闸门），
        #   站位用**前线空槽 slot** 表达；不给就自己挑一个空槽。
        slot = int(parts[1]) if len(parts) > 1 else None
        # ★ 2026-09-24 F10b：移动预检接上了（借 `CanAttack` 的通用行动子集判据，
        #   `semantics.legality.Legality.can_move`）。跟 `do_attack` 一样**只挑不判**：
        #   算出来不合法只是 reject，带 `!` 就照发；算不出来（stopped）一律放行。
        if not force:
            r = self.a.can_move(c)
            if r.get("ok") and r.get("can") is False:
                print("  ✘ %s说不行：%s" % (self._judge_name(r),
                                          r.get("reason_zh") or r.get("reason")))
                print("     —— 预检只挑不判。确认要发就用： front ! %s" % " ".join(parts))
                return
            if not r.get("ok"):
                print("  （判据没算出来：%s —— 照发）" % (r.get("stopped") or "")[:60])
        self._act("把 %s 移到前线%s" % (self.a.tr(c.name), "（强制）" if force else ""),
                  lambda cid, sl: self.a.move_up(cid, slot=sl, force=force),
                  ACTION_TYPES["move"], c.obj.CardID, slot)

    def do_end(self, arg=""):
        self._act("结束回合", self.a.end_turn, ACTION_TYPES["end"])

    def do_pending(self, arg=""):
        """现在在等什么：抉择候选 / 手牌目标 / 板卡待点目标 / 箭头。只读。"""
        p = self.a.pending()
        if not p.get("waiting"):
            print("  （没在等任何选择）")
            return
        if p.get("choose_one"):
            for c in p["choose_one"]:
                print("  choose_one 候选：idx=%s %s%s%s" % (
                    c.get("index"), c.get("name") or c.get("card_id"),
                    "  [effect]" if c.get("is_effect") else "",
                    "  [需目标]" if c.get("needs_target") else ""))
        pp = p.get("pick_pending") or {}
        if pp.get("pending"):
            print("  pick_pending: %s" % pp)
        if p.get("board_target"):
            print("  板卡待点目标：%s" % p["board_target"])
        ht = p.get("hand_target") or {}
        if ht.get("pending"):
            print("  等选一张手牌当目标：%s" % ht)

    def do_choose(self, arg=""):
        """`choose <index> [kind] [trigger]` —— 选一张（三选一/预报/牌库选牌/effect）。"""
        parts = arg.split()
        if not parts or not parts[0].lstrip("-").isdigit():
            print("  用法：choose <index> [kind] [trigger]")
            return
        index = int(parts[0])
        kind = parts[1] if len(parts) > 1 else None
        trigger = int(parts[2]) if len(parts) > 2 else None
        if not self._confirm("选候选 #%d%s" % (index, ("（%s）" % kind) if kind else "")):
            print("  取消")
            return
        self._print_result(self.a.choose_card(index, kind=kind, trigger=trigger))

    def do_chooseone(self, arg=""):
        """`chooseone <index> [trigger]` 或 `chooseone <index> <目标> [trigger]`。"""
        parts = arg.split()
        if not parts or not parts[0].lstrip("-").isdigit():
            print("  用法：chooseone <index> [trigger]  |  chooseone <index> <目标> [trigger]")
            return
        index = int(parts[0])
        # ★ 第二个参数是数字**且**解析不出卡就当 trigger，否则当目标短号
        trigger = None
        target = None
        if len(parts) > 1:
            if parts[1].lstrip("-").isdigit() and self.a.resolve(parts[1]) is None:
                trigger = int(parts[1])
            else:
                target = parts[1]
        if len(parts) > 2:
            trigger = int(parts[2])
        if target is not None:
            t = self._card(target)
            if t is None:
                return
            if not self._confirm("选选项 #%d → 指向 %s" % (index, self.a.tr(t.name))):
                print("  取消")
                return
            self._print_result(self.a.choose_one_with_target(index, t.obj.CardID, trigger=trigger))
        else:
            if not self._confirm("选选项 #%d" % index):
                print("  取消")
                return
            self._print_result(self.a.choose_one(index, trigger=trigger))

    def do_mullmarks(self, arg=""):
        for m in self.a.mulligan_marks():
            print("  %s  %s%s" % (m.get("card_id"), self.a.tr(m.get("name")),
                                  "  [要换]" if m.get("marked") else ""))

    def do_mullmark(self, arg=""):
        if not arg.strip():
            print("  用法：mullmark <手牌>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        if not self._confirm("翻转 %s 的换牌标记" % self.a.tr(c.name)):
            print("  取消")
            return
        self._print_result(self.a.mulligan_mark(c.obj.CardID))

    def do_mullgo(self, arg=""):
        if not self._confirm("确认换牌"):
            print("  取消")
            return
        self._print_result(self.a.mulligan_confirm())

    def do_htarget(self, arg=""):
        """选一张手牌当目标（`selectTargetOnPlayedFromHand`），会给对手发信息流。"""
        if not arg.strip():
            print("  用法：htarget <手牌>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        if not self._confirm("选 %s 当手牌目标" % self.a.tr(c.name)):
            print("  取消")
            return
        self._print_result(self.a.select_hand_target(c.obj.CardID))

    def do_surrender(self, arg=""):
        """投降。**不可逆**，游戏没有二次确认——必须显式打 `surrender confirm`。"""
        if arg.strip() != "confirm":
            print("  投降不可逆。确认要投就输入： surrender confirm")
            return
        if not self._confirm("投降"):
            print("  取消")
            return
        self._print_result(self.a.surrender(confirm=True))

    def do_aim(self, arg=""):
        """`aim <目标> [手牌]` —— 手动补点一次"待点目标"

        用于部署单位的**第二阶段**或选项 resolve 之后（HIDDEN PLANS 那种）游戏已经
        进了"待点目标"态、但你想单独重发这一下（比如 `play`/`chooseone` 那次没点上）。
        不给手牌就用游戏自己记的"正在打的那张"（`select_target`）。
        """
        parts = arg.split()
        if not parts:
            print("  用法：aim <目标> [手牌]")
            return
        t = self._card(parts[0])
        if t is None:
            return
        if len(parts) > 1:
            c = self._card(parts[1], mine=True)
            if c is None:
                return
            if not self._confirm("点选 %s 指向 %s" % (self.a.tr(c.name), self.a.tr(t.name))):
                print("  取消")
                return
            self._print_result(self.a.select_unit_target(c.obj.CardID, t.obj.CardID))
        else:
            if not self._confirm("点选目标 %s" % self.a.tr(t.name)):
                print("  取消")
                return
            self._print_result(self.a.select_target(t.obj.CardID))

    def do_mulldone(self, arg=""):
        print("  %s" % self.a.mulligan_done())

    def do_htpending(self, arg=""):
        print("  %s" % self.a.hand_target_pending())

    def do_htlegal(self, arg=""):
        if not arg.strip():
            print("  用法：htlegal <手牌>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        print("  %s" % self.a.hand_target_legal(c.obj.CardID))

    def do_canplay(self, arg=""):
        """`canplay <手牌> [目标]` —— 只读预检（只挑不判，见 §7.6f）。"""
        parts = arg.split()
        if not parts:
            print("  用法：canplay <手牌> [目标]")
            return
        c = self._card(parts[0], mine=True)
        if c is None:
            return
        t = self._card(parts[1]) if len(parts) > 1 else None
        if len(parts) > 1 and t is None:
            return
        r = self.a.can_play(c, t)
        if not r.get("ok"):
            print("  （判据没算出来：%s）" % (r.get("stopped") or ""))
            return
        print("  %s：%s" % (self._judge_name(r),
                            "能打" if r.get("can") else (r.get("reason_zh") or r.get("reason"))))

    def do_canmove(self, arg=""):
        if not arg.strip():
            print("  用法：canmove <单位>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        r = self.a.can_move(c)
        if r.get("game_gate") == "not_asked":
            print("  游戏闸门没问（%s）—— 走进程外判据" % (r.get("game_gate_reason") or ""))
        if not r.get("ok"):
            print("  （判据没算出来：%s）" % (r.get("stopped") or ""))
            return
        print("  %s：%s" % (self._judge_name(r),
                            "能上" if r.get("can") else (r.get("reason_zh") or r.get("reason"))))

    def do_canact(self, arg=""):
        if not arg.strip():
            print("  用法：canact <单位>")
            return
        c = self._card(arg.strip(), mine=True)
        if c is None:
            return
        print("  %s" % self.a.can_act_now(c.obj.CardID))

    def do_pinned(self, arg=""):
        if not arg.strip():
            print("  用法：pinned <卡>")
            return
        c = self._card(arg.strip())
        if c is None:
            return
        print("  %s" % self.a.is_pinned(c.obj.CardID))

    def do_target(self, arg=""):
        """`target <规格>` —— 目标规格串（`hq`/`mhq`/`front<i>`/`back<i>`/`guard<i>`）→ card_id。"""
        if not arg.strip():
            print("  用法：target <规格>")
            return
        print("  %s" % self.a.resolve_target(arg.strip()))

    def do_picktarget(self, arg=""):
        """启发式挑一个目标，**不算判据**——只是给"要选一个"的动作一个默认值。"""
        side = None                       # 缺省 = 对方（session 里按 my_side 算）
        if arg.strip():                   # 输入入口一次性解析成 ESide
            if self.a.st is None:
                self.a.snapshot()
            try:
                side = view.parse_side(arg.strip(), self.a.st.my_side)
            except ValueError as e:
                print("  " + str(e))
                return
        print("  %s" % self.a.pick_target(side=side))

    def do_totals(self, arg=""):
        if not arg.strip():
            print("  用法：totals <卡>")
            return
        c = self._card(arg.strip())
        if c is None:
            return
        print("  %s" % self.a.card_totals(c.obj.CardID))

    def do_hints(self, arg=""):
        """游戏刚弹的提示（拒绝理由的权威来源）。提示短命，只能轮询。"""
        for t in (self.a.notify_texts() or []):
            print("  〔提示〕%s" % t)

    def do_preflight(self, arg=""):
        parts = arg.split()
        card = self._card(parts[0], mine=True) if parts else None
        if parts and card is None:
            return
        target = parts[1] if len(parts) > 1 else None
        print("  %s" % self.a.preflight(card.obj.CardID if card else None, target=target))

    def do_picklayers(self, arg=""):
        """`picklayers <index> [kind]` —— 一口气走完多层抉择链（预报"天气→2K/4K/6K"那种）。"""
        parts = arg.split()
        if not parts or not parts[0].lstrip("-").isdigit():
            print("  用法：picklayers <index> [kind]")
            return
        index = int(parts[0])
        kind = parts[1] if len(parts) > 1 else None
        if not self._confirm("走完多层抉择（从 #%d 开始）" % index):
            print("  取消")
            return
        self._print_result(self.a.pick_layers(index, kind=kind))

    def do_refresh(self, arg=""):
        self.a.snapshot()
        print("  已重取")

    def do_help(self, arg=""):
        print(__doc__)

    # ------------------------------------------------------------ 主循环
    ALIASES = {"i": "inspect", "r": "refresh", "h": "help", "hist": "log",
               "b": "board", "a": "attack", "p": "play", "e": "end",
               "t": "can", "targets": "can"}

    def run(self):
        print(BANNER)
        while True:
            try:
                line = input("kards> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in ("q", "quit", "exit"):
                return 0
            cmd, _, arg = line.partition(" ")
            cmd = self.ALIASES.get(cmd, cmd)
            fn = getattr(self, "do_" + cmd, None)
            if fn is None:
                print("  没有 %r 这条命令（help 看列表）" % cmd)
                continue
            try:
                fn(arg)
            except Exception as e:                           # noqa: BLE001
                import traceback
                traceback.print_exc()
                print("  命令炸了：%s" % e)
            self._drain()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="KARDS agent 交互 shell")
    ap.add_argument("--yes", action="store_true", help="动鼠标前不再确认")
    a = ap.parse_args(argv)
    return Shell(auto_yes=a.yes).run()


if __name__ == "__main__":
    sys.exit(main())
