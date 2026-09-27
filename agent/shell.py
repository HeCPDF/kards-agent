#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell.py —— 交互式 REPL（前端①）。

    cd kards-agent && python -m agent.shell

命令
====
    board [full]        盘面（`full` 连敌方手牌一起列）
    pins                盘面 + 逐实例的**被压制**（慢，要读每张卡的 live effects）
    i / inspect <卡>    一张卡的全部
    log [n] [me|enemy]  对局历史（双方动作，§7.6g）
    events              自上次以来的新提示（拒绝理由等）
    wait                等到我方回合
    play <手牌> [目标]   出牌
    attack <单位> <目标> 攻击（`attack ! …` 跳过预检强制发出）
    can <单位>          这个单位现在能打谁（进程外重算，三态）
    front <单位> [x]    支援 → 前线
    end                 结束回合
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

import agentpath  # noqa: F401,E402

from .session import ACTION_TYPES, AgentSession  # noqa: E402

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
            from . import view
            me = self.a._my_player_id(acts)                  # noqa: SLF001
            print(view.render_history(acts, me, self.a.st, tr=self.a.tr))

    def _confirm(self, what: str) -> bool:
        if self.yes:
            return True
        try:
            return input("  即将%s —— 回车确认 / 输入 n 取消：" % what).strip().lower() != "n"
        except EOFError:
            return False

    def _card(self, token: str, want_side: Optional[str] = None):
        c = self.a.resolve(token)
        if c is None:
            print("  认不出 %r。先 `board` 看短号，或用 #卡表id / @uid" % token)
            return None
        if want_side and c.side != want_side:
            print("  %s 是%s的卡" % (token, "我方" if c.side == "local" else "敌方"))
            return None
        return c

    @staticmethod
    def _judge_name(r: dict) -> str:
        """这句话是谁说的：游戏自己 / 进程外重算。**别把两者混着叫"判据"。**"""
        return ("游戏自己" if r.get("source") == "game" else "进程外判据")

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
            from . import view
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
        n, mine = 20, None
        for p in parts:
            if p.isdigit():
                n = int(p)
            elif p in ("me", "my", "我"):
                mine = True
            elif p in ("enemy", "opp", "敌"):
                mine = False
        r = self.a.history(tail=n, mine=mine)
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
        c = self._card(parts[0], "local")
        if c is None:
            return
        if c.location != "hand":
            print("  %s 不在手牌里（在%s）" % (parts[0], c.location))
            return
        tgt = None
        tcard = None
        if len(parts) > 1:
            t = self._card(parts[1])
            if t is None:
                return
            tgt = t.card_id
            tcard = t
        elif c.needs_hand_target:
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
                  ACTION_TYPES["play"], c.card_id, tgt)

    def _play_fn(self, card, tgt, force: bool):
        """出牌的执行口 —— **按语义分派**（2026-09-27 语义分层）：

          * 无目标 ⇒ `play()`（拖到落点松手）；
          * **单位** + 目标 ⇒ `deploy_unit_with_target()`（两阶段：落地 → 点目标）；
          * **指令** + 目标 ⇒ `play_order_on_unit()`（松手那一次即成交）。

        旧版一律走 `ops.play_card_from_hand` —— 那个实现把三条链塞在一个名字里，
        而 `ops.py` 已归档。
        """
        is_unit = (card.card_type or "") not in ("order", "counter", "gotcha")

        def fn(cid, tg=None):
            if tg is None:
                return self.a.play(cid, force=force)
            if is_unit:
                return self.a.deploy_unit_with_target(cid, tg)
            return self.a.play_order_on_unit(cid, tg)
        return fn

    def do_can(self, arg=""):
        """`can <我方单位>` —— 这个单位现在能打谁（进程外重算，只挑不判）。"""
        if not arg.strip():
            print("  用法：can <我方单位>")
            return
        c = self._card(arg.strip(), "local")
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
        c = self._card(parts[0], "local")
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
                  ACTION_TYPES["attack"], c.card_id, t)

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
        if c.side != "enemy":
            print("  %s 是我方的卡，不能当攻击目标" % token)
            return None
        if c.location == "hq":
            return "hq"
        if c.location not in ("frontline", "back"):
            print("  %s 不在场上（在%s）" % (token, c.location))
            return None
        row = sorted([x for x in self.a.st.cards
                      if x.side == "enemy" and x.location == c.location
                      and (c.location == "back" or x.card_type not in ("order", "counter"))],
                     key=lambda x: (x.slot if x.slot is not None else 99))
        try:
            i = [x.uid for x in row].index(c.uid)
        except ValueError:
            print("  %s 不在敌方 %s 行里（盘面变了？先 refresh）" % (token, c.location))
            return None
        return ("front%d" if c.location == "frontline" else "back%d") % i

    def do_front(self, arg=""):
        force = arg.strip().startswith("!")
        parts = arg.lstrip("! ").split()
        if not parts:
            print("  用法：front <我方支援单位> [slot]   （front ! … 强制发出）")
            return
        c = self._card(parts[0], "local")
        if c is None:
            return
        # ★ 2026-09-27：不再有"落点 x"（那是物理鼠标的坐标）。新版走**合成事件**
        #   （`move_to_front` 内部写 cursor 字段 + 搬 owner 箭头头平面过长度闸门），
        #   站位用**前线空槽 slot** 表达；不给就自己挑一个空槽。
        slot = int(parts[1]) if len(parts) > 1 else None
        # ★ 2026-09-24 F10b：移动预检接上了（借 `CanAttack` 的通用行动子集判据，
        #   `agent.legality.Legality.can_move`）。跟 `do_attack` 一样**只挑不判**：
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
                  ACTION_TYPES["move"], c.card_id, slot)

    def do_end(self, arg=""):
        self._act("结束回合", self.a.end_turn, ACTION_TYPES["end"])

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
