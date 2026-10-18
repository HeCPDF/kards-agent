# -*- coding: utf-8 -*-
"""手牌效果预览：独立小窗，**按游戏当前的随机种子，把手里每张牌打出去会发生什么，用人话写出来**。
运行：`python -m gui.effect_oracle`（游戏在对局里；只读）。

玩家关心的是"这张牌会得到什么"（抽到哪张、生成哪张、伤害几点……），不是随机数本身。所以这里不显示种子运算，
只显示结果：对每张手牌，用外部 VM 空跑它的打出效果（`semantics.effectvm.enumerate_effects`，随机点用**活种子**
确定地算，见 `kardsmem/rng.py`），再由 `gui.effect_text` 翻成中文。三选一类（`selectCardToDraw`）改用
`semantics.choosespawn.predict` 给出屏幕上会出现的 3 张候选。

边界（如实写在窗口里）：
  * 前提是"下一个抽随机数的就是这张牌"：对手/别的效果先抽了，种子一动，窗口会自动重算；
  * 需要指向目标的牌，不带目标空跑，只显示与目标无关的部分；
  * VM 没覆盖到的原语会如实显示"取不到/缺口"，不编。
只读：`ReadProcessMemory`，不注入、不依赖监听器，对局中可以开着。
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Optional

from .hand_preview import LiveSource

APP_NAME = "KARDS 手牌效果预览"
POLL_S = 1.5


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
class Panel(ttk.Frame):
    """可嵌入任何 Notebook / 窗口的预览面板。`start()` 才开始读游戏，`stop()` 停；不看的时候不读、不跑 VM。"""

    def __init__(self, master, source=None):
        super().__init__(master)
        self.source = source
        self.q: "queue.Queue" = queue.Queue()
        self.rows: list = []
        self._stop: Optional[threading.Event] = None
        self._build()
        self.after(200, self._tick)

    def _build(self):
        self.status = ttk.Label(self, text="未启动（切到本页签才读游戏）")
        self.status.pack(fill="x", padx=8, pady=(8, 2))
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=4)
        cols = (("name", "手牌", 220), ("eff", "打出后会发生", 640))
        self.tree = ttk.Treeview(paned, columns=[c[0] for c in cols], show="headings", height=9, selectmode="browse")
        for k, t, w in cols:
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="w")
        self.tree.bind("<<TreeviewSelect>>", lambda *_: self._show_detail())
        paned.add(self.tree, weight=3)
        self.detail = tk.Text(paned, height=8, state="disabled", wrap="word", font=("Microsoft YaHei UI", 10))
        paned.add(self.detail, weight=2)
        ttk.Label(self, foreground="#8a8f98", wraplength=860,
                  text="只读游戏内存。前提：下一个抽随机数的就是这张牌；对手或别的效果先抽了，结果会自动重算。"
                       "需要指向目标的牌不带目标计算。取不到的效果如实标出，不编。").pack(fill="x", padx=8, pady=(0, 6))

    # ---- 生命周期 ----
    def start(self):
        if self.source is None or (self._stop is not None and not self._stop.is_set()):
            return
        self._stop = ev = threading.Event()
        self.status.configure(text="连接中…")
        threading.Thread(target=self._run, args=(ev,), daemon=True).start()

    def stop(self):
        if self._stop is not None:
            self._stop.set()

    def _run(self, ev: threading.Event):
        while not ev.is_set():
            res = self.source.poll()
            if not ev.is_set():
                self.q.put(res)
            ev.wait(POLL_S)

    # ---- 显示 ----
    def show(self, res: dict):
        """把一次 `poll()` 的结果画出来（测试直接调它）。"""
        seed = res.get("seed")
        if res.get("ok"):
            self.status.configure(text="● 已连接游戏（只读）　手牌 %d 张　%s" % (
                len(res["rows"]), "随机种子已读到（随机结果按它确定地算）" if seed is not None else "⚠ 读不到随机种子"))
        else:
            self.status.configure(text="● " + str(res.get("why")))
        keep = self.tree.selection()
        self.rows = list(res.get("rows") or [])
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=(r["name"], "；".join(r["lines"])[:140]))
        if keep and keep[0] in self.tree.get_children():
            self.tree.selection_set(keep[0])
        self._show_detail()

    def _show_detail(self):
        sel = self.tree.selection()
        text = ""
        if sel:
            r = self.rows[int(sel[0])]
            text = r["name"] + "\n" + "\n".join("· " + s for s in r["lines"])
            if r.get("gaps"):
                text += "\n\n缺口：" + r["gaps"]
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.configure(state="disabled")

    def _tick(self):
        try:
            res = None
            while True:
                res = self.q.get_nowait()
        except queue.Empty:
            pass
        if res is not None:
            self.show(res)
        self.after(200, self._tick)


class App(tk.Tk):
    """独立窗口：就是一个撑满的 `Panel`。"""

    def __init__(self, source=None):
        super().__init__()
        headless = os.environ.get("KARDS_GUI_SELFTEST_EXIT_MS", "").isdigit()
        if headless:
            self.withdraw()
        self.title(APP_NAME)
        self.geometry("900x560")
        self.minsize(640, 400)
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
    App(LiveSource()).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
