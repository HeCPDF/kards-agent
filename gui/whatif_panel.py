# -*- coding: utf-8 -*-
"""“盘面模拟”面板：左边是当前实时盘面（短号 h1/m2/e3/hq/ehq），下面输入命令，程序基于当前盘面**只模拟**（不向游戏发动作），
返回指令式的结果串（与“手牌效果预览”同格式）。逻辑在 `player.whatif`，这里只管界面与文字。

    deploy h2 [slot] [choose 1] [with e1]   部署单位（slot = 部署后在我方支援线（含总部）从左数第几个，1 = 最左）
    play h3 [choose 1] [with e2]            打出指令（抉择牌必须写 choose <下标>）
    attack m1 [with] ehq   攻击              front m1   上线              end   结束回合
    spawn <牌名>   回答三选一     forecast   预报：只在第一层能跑，走一步、把 9 张候选一次列出来
    spawn <天气/档>   预报也可以一次一层地真选（两次 spawn：先天气、再档）；选中的牌下回合进手牌
    select h2   选手牌
    undo   撤回上一条            reset      重取基线、重建模拟、清掉步骤（回到实时盘面）
    多条命令用 ; 分隔，按次序执行、按次序输出，出错就停

盘面每 2 秒自动重读一次（只读内存、不重建模拟）；要重取基线并重建模拟只有 `reset`（命令或点选界面底栏的 reset），
所以没有单独的“刷新盘面”按钮。只在启动后才读游戏（只读内存），不向游戏发任何动作。主面板从顶栏按钮把它开成**独立窗口**
（不进页签：页签高度被主窗口钉死，里面的点选盘面没法滚），也可独立运行：`python -m gui.whatif_panel`。
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from . import effect_text as ET
from . import textutil as TU
from . import whatif_board as WB

APP_NAME = "KARDS 盘面模拟"
POLL_S = 2.0


def _option_lines(ch: dict, ids: dict) -> List[str]:
    """选项列表：抉择 = 下标 + 该选项的效果；三选一/预报 = 候选名；选手牌 = 手牌名。"""
    out = []
    for key, label, payload in ch["options"]:
        if ch.get("need") == "choose":
            text = "; ".join(ET.describe(payload, None, None, lambda cid: ids.get(cid, "card#%s" % cid))) if isinstance(payload, dict) else str(label)
            out.append("  %s) %s" % (key, text))
        elif ch.get("need") == "select" and isinstance(key, int):
            out.append("  - %s" % ids.get(key, key))
        else:
            out.append("  - %s" % label)
    return out


def format_result(res) -> str:
    """`player.whatif.Result` → 多行文字（纯函数，离线可测）。"""
    ids = res.labels or {}
    L: List[str] = ["> %s" % res.echo] if res.echo else []
    ch = res.choices
    if not res.ok:
        L.append("  ✘ %s" % res.error)
    elif res.kind in ("undo", "reset"):
        pass
    else:
        # effect = 打出这张牌的**流程本身**（按 PlayCardFromHand 的次序：先扣指挥点/手牌离手，再跑卡牌效果）；
        # result = 精简后的盘面净变化（上面已经写过的代价不再重复）。
        eff_rows: List[str] = []
        if res.eff is not None:
            eff_rows = [x for x in ET.describe(res.eff, None, None, lambda cid: ids.get(cid, "card#%s" % cid))
                        if x != "(no on-play effect)"]
        diff = list(res.diff or ())
        cost = [x for x in diff if x.startswith("kredit ") or x.startswith("hand -")]
        rest = [x for x in diff if x not in cost]
        rows = cost + eff_rows
        if rows:
            L.append("effect:")
            L.extend("  " + x for x in rows)
        if rest:
            L.append("result:")
            L.extend("  " + x for x in rest)
    if ch:
        need = ch["need"]
        if ch.get("done"):
            if need == "forecast" and ch.get("picked"):            # 选完了：写出选中的牌（下回合开始进手牌）
                L.append("forecast: picked %s -> %s (下回合开始进手牌)"
                         % (" / ".join(str(x) for x in (ch.get("picks") or [])), ch.get("card") or "?"))
            else:
                L.append("done: %s" % ", ".join(str(x) for x in (ch.get("picks") or [])))
        elif need == "choose":
            L.append("options (write: choose <i> inside deploy/play):")
        elif need == "spawn":
            L.append("pending: pick one to add to your hand  ->  spawn <card>")
        elif need == "forecast":
            if (ch.get("meta") or {}).get("merged"):                # forecast 已走一步、候选列出来了：从这里选一张
                L.append("pending: forecast  ->  spawn <牌名/下标>（从这 %d 张候选里选一张）" % len(ch.get("options") or ()))
            elif int(ch.get("layer") or 1) > 1:                      # 第一层已经用 spawn 选过：不能再 forecast
                L.append("pending: forecast  ->  spawn <档>（第一层已经用 spawn 选过，不能再 forecast）")
            else:
                L.append("pending: forecast  ->  spawn <候选>（一次一层地选，先天气再档）"
                         " 或 forecast（走一步、把 9 张候选一次列出来再选）")
        else:
            L.append("pending: pick a hand card  ->  select <card>")
        L.extend(_option_lines(ch, ids))
    for n in res.notes:
        L.append("note: " + n)
    return "\n".join(L)


class Panel(ttk.Frame):
    """可嵌入的盘面模拟面板。`source` 是 `player.whatif.WhatIf`（或同接口的假对象）；`start()` 才开始读游戏。"""

    def __init__(self, master, source=None):
        super().__init__(master)
        self.source = source
        self.q: "queue.Queue" = queue.Queue()
        self._stop: Optional[threading.Event] = None
        self._busy = False
        self.history: List[str] = []
        self._hi = 0
        self._depth = 0
        sess = getattr(source, "sess", None)
        self.texts = WB.TextSource(sess) if sess is not None else None
        self._build()
        self.after(200, self._tick)

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.status = ttk.Label(top, text="未启动")
        self.status.pack(side="left")
        # 不再放“刷新盘面”按钮：盘面本来就每 2 秒自动重读一次（只读内存）；要重取基线、重建模拟、清掉步骤
        # 只有 reset（命令或点选界面底栏的 reset）——两者以前都像“刷新”，但只有一个真的重建模拟。
        ttk.Label(top, text="盘面每 %g 秒自动刷新；重取基线/重建模拟用 reset" % POLL_S, foreground="#666").pack(side="right")
        pan = ttk.PanedWindow(self, orient="vertical")
        pan.pack(fill="both", expand=True, padx=8, pady=4)
        self.tabs = ttk.Notebook(pan)
        self.pick = WB.BoardView(self.tabs, self.run_text, self.query_options if self.source is not None else None)        # 点选界面：点牌拼命令，走同一条 run_line
        self.tabs.add(self.pick, text="点选")
        self.board = TU.make_readonly(tk.Text(self.tabs, height=16, wrap="none", font=("Consolas", 10)))
        self.tabs.add(self.board, text="文本盘面")
        pan.add(self.tabs, weight=3)
        self.out = TU.make_readonly(tk.Text(pan, height=10, wrap="word", font=("Consolas", 10)))
        pan.add(self.out, weight=2)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(bar, text="命令 >").pack(side="left")
        self.cmd = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.cmd)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda *_: self.submit())
        e.bind("<Up>", lambda *_: self._hist(-1))          # 返回 "break"：不让 Tk 默认行为把光标挪回开头
        e.bind("<Down>", lambda *_: self._hist(1))
        TU.add_entry_menu(e)
        ttk.Button(bar, text="模拟", command=self.submit).pack(side="left")
        self.entry = e

    # ---- 生命周期 ----
    def start(self):
        if self.source is None or (self._stop is not None and not self._stop.is_set()):
            return
        self._stop = ev = threading.Event()
        self.status.configure(text="连接中…")
        threading.Thread(target=self._poll, args=(ev,), daemon=True).start()

    def stop(self):
        if self._stop is not None:
            self._stop.set()

    def _poll(self, ev: threading.Event):
        while not ev.is_set():
            self._fetch_board()
            ev.wait(POLL_S)

    def _fetch_board(self):
        try:
            text = self.source.view()[0]
            model = None
            if hasattr(self.source, "board_model"):
                model = self.source.board_model()
                if model is not None and self.texts is not None:
                    self.texts.fill(model)                          # 规则文本：只读内存，放在后台线程里
            self.q.put(("board", text))
            if model is not None:
                self.q.put(("model", model))
        except Exception as ex:                                     # noqa: BLE001
            self.q.put(("status", "● 未连接游戏（%s）" % ((str(ex).splitlines() or [type(ex).__name__])[0][:80])))

    # ---- 命令 ----
    def _hist(self, d):
        if not self.history:
            return "break"
        self._hi = max(0, min(len(self.history), self._hi + d))
        self.cmd.set(self.history[self._hi] if self._hi < len(self.history) else "")
        self.entry.icursor("end")                          # 光标留在行尾（以前跑到开头）
        return "break"

    def query_options(self, tok: str):
        """点选界面选中一张牌：后台问“这张牌现在能怎么用”，答案回到界面线程。"""
        def work():
            try:
                self.q.put(("opts", (tok, self.source.card_options(tok))))
            except Exception:                                       # noqa: BLE001
                self.q.put(("opts", (tok, {})))
        threading.Thread(target=work, daemon=True).start()

    def submit(self):
        self.run_text(self.cmd.get().strip())

    def run_text(self, text: str):
        """命令框和点选界面共用的入口。"""
        text = (text or "").strip()
        if not text or self.source is None:
            return
        if self._busy:
            self._append("（上一条还在模拟，稍等）")
            return
        self.history.append(text)
        self._hi = len(self.history)
        if self.cmd.get().strip() == text:                          # 点选界面发的命令不动命令框里正在输入的字
            self.cmd.set("")
        self._busy = True
        self._append("… 模拟中：%s（要建一次模拟场面，可能要几秒）" % text)
        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def _run(self, text):
        try:
            results = self.source.run_line(text)
            self.q.put(("result", "\n".join(format_result(r) for r in results)))
            self.q.put(("depth", results[-1].depth if results else 0))
            self.q.put(("results", results))
            self._fetch_board()                                     # 立刻刷新盘面（点选界面不等下一次轮询）
        except Exception as ex:                                     # noqa: BLE001
            self.q.put(("result", "  ✘ 模拟出错：%s: %s" % (type(ex).__name__, str(ex)[:160])))

    # ---- 显示 ----
    def _set(self, w: tk.Text, text: str):
        """盘面文字没变就不动它——否则每次轮询都会清掉你刚选中的文字、把滚动位置拉回去。"""
        if getattr(w, "_last_text", None) == text:
            return
        w._last_text = text
        TU.set_text(w, text, keep_view=True)

    def _append(self, text: str):
        at_bottom = self.out.yview()[1] >= 0.999
        self.out.insert("end", text + "\n")
        if at_bottom:                                      # 你在往上翻看旧结果时，不把你拽到底
            self.out.see("end")

    def show_board_status(self):
        self.status.configure(text="● 已连接游戏（只读）　模拟 %d 步（undo 撤回 / reset 回实时）　命令：deploy h2; play h3 with e2; spawn <牌名>; forecast; undo; reset"
                              % self._depth)

    def show_board(self, text: str):
        self.show_board_status()
        self._set(self.board, text)

    def _tick(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "board":
                    self.show_board(payload)
                elif kind == "model":
                    self.pick.show(payload)
                elif kind == "opts":
                    self.pick.set_options(*payload)
                elif kind == "results":
                    self.pick.on_results(payload)
                elif kind == "depth":
                    self._depth = payload
                    self.show_board_status()
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "result":
                    self._busy = False
                    self._append(payload + "\n")
        except queue.Empty:
            pass
        self.after(200, self._tick)


class App(tk.Tk):
    def __init__(self, source=None):
        super().__init__()
        headless = os.environ.get("KARDS_GUI_SELFTEST_EXIT_MS", "").isdigit()
        if headless:
            self.withdraw()
        self.title(APP_NAME)
        self.geometry("960x700")
        self.panel = Panel(self, source)
        self.panel.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if source is not None and not headless:
            self.panel.start()
        if headless:
            self.after(int(os.environ["KARDS_GUI_SELFTEST_EXIT_MS"]), self._close)

    def _close(self):
        self.panel.stop()
        self.destroy()


def main(argv=None) -> int:
    from agent import session as S
    from player.whatif import WhatIf
    App(WhatIf(S.AgentSession(translate=False, warm=False))).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
