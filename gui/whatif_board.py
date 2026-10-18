# -*- coding: utf-8 -*-
"""“盘面模拟”的点选界面：命令之外的第二种输入方式。

每张牌是一个可点的卡块（名字 / 费用 / 攻防 / 关键词 + 游戏内存里的规则文本）。点选只是**拼命令串**，
交给调用方（`run_cb`）走同一个 `WhatIf.run_line`，所以输出和命令框完全一致。

    点手牌          选中（单位 = deploy，指令 = play）；再点场上一个目标 = `… with <目标>`；或按“无目标”
    点我方场上单位   选中；再点敌方单位/总部 = `attack <单位> with <目标>`；或按“上线”
    抉择牌          按钮列出每个选项的效果，点一个 = 同一条命令加 `choose <i>`
    三选一          候选卡块（名字 + 规则文本），点“选这张” = `spawn <下标>`
    预报            第一层 3 个天气候选 + **第 4 个选项“运行 forecast”**（走一步、把 9 张候选一次列出来，可 undo；
                    第一层已经用 spawn 选过天气就不能再 forecast）；列出来的 9 张（或第二层的 3 张）都是卡块，
                    点“选这张” = `spawn <牌名/下标>` = 真的选
    选手牌          点手牌 = `select <手牌>`

规则文本来自卡对象 `+0x90`（`UBaseCardObject::Text`，FText 明文，只读内存），原样显示、不翻译；
模拟里新出现的牌/候选牌没有对象地址，按名字去静态卡表里找。读不出就留空，不编。
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from . import effect_text as ET

TILE_W = 150                       # 卡块宽度（像素）：窄，区域里自动换行
OFF_CARD_TEXT = 0x90               # UBaseCardObject::Text（FText），SDK dump 1.60 kards_classes.hpp
OFF_CARD_FLAVOR = 0xA0             # flavorText。★ 没有规则文字的牌（只有关键词），Text 里放的是和 flavorText 一样的历史介绍 ⇒ 相同就当没有
_TAG = re.compile(r"<[^<>]*>")

COL_MINE, COL_ENEMY, COL_ARMED, COL_AIM, COL_PLAIN, COL_TARGET = "#eaf2ff", "#fdeeee", "#ffe08a", "#c9f0c9", "#f2f2f2", "#ff9f9f"
ZONE_TITLES = (("enemy_hand", "敌方手牌"), ("enemy_support", "敌方支援线"), ("front", "前线"),
               ("my_support", "我方支援线"), ("my_hand", "我方手牌"))


def clean_text(s: Optional[str]) -> Optional[str]:
    """去掉富文本标签（<kw>…</>），其余原样。"""
    if not s:
        return None
    s = _TAG.sub("", s).replace("\r", "").strip()
    return s or None


# ---------------------------------------------------------------------------
# 规则文本（只读内存）
# ---------------------------------------------------------------------------
class TextSource:
    """`tile.uid` → 卡对象 `+0x90`；没有 uid（模拟里新出现的牌、候选牌）→ 静态卡表按名字找。结果缓存。"""

    def __init__(self, sess):
        self.sess = sess
        self._by_uid: dict = {}
        self._static: Optional[dict] = None

    def _km(self):
        return self.sess._kardsmem()

    def uid_text(self, uid: int) -> Optional[str]:
        if uid not in self._by_uid:
            from kardsmem.names import ftext_at
            m = self._km().m
            txt, flavor = ftext_at(m, uid, OFF_CARD_TEXT), ftext_at(m, uid, OFF_CARD_FLAVOR)
            self._by_uid[uid] = None if txt and txt == flavor else clean_text(txt)
        return self._by_uid[uid]

    def _table(self) -> dict:
        if self._static is None:
            from semantics.choosespawn import static_cards
            tab = {}
            for ptr, fname, title, _set in static_cards(self._km()):
                for k in (fname, fname.removesuffix("_C"), title):
                    if k:
                        tab.setdefault(k.upper(), ptr)
            self._static = tab
        return self._static

    def name_text(self, name: str) -> Optional[str]:
        ptr = self._table().get((name or "").upper())
        return self.uid_text(ptr) if ptr else None

    def fill(self, model) -> None:
        """给模型里每个卡块填规则文本（在后台线程里调，别在界面线程里读内存）。"""
        tiles = [t for z in model.zones.values() for t in z if t.kind != "hq"] + list(model.pending_tiles)
        for t in tiles:
            if t.text is not None:
                continue
            try:
                t.text = (self.uid_text(t.uid) if t.uid else None) or self.name_text(t.name)
            except Exception:                                      # noqa: BLE001
                t.text = None


# ---------------------------------------------------------------------------
# 点选逻辑（纯函数式状态机，离线可测）
# ---------------------------------------------------------------------------
class Picker:
    """选择状态 = 三件套：牌（手牌或我方场上单位）/ 位置（＋）/ 目标。每一项都能随时点选、再点同一项取消；
    `command()` 把当前选择拼成命令串，由“提交”按钮发出。`opts` 是 `WhatIf.card_options` 的答案（可指向谁、要不要目标）。"""

    def __init__(self):
        self.card = None                   # 选中的 Tile
        self.slot: Optional[int] = None    # deploy / front 的 slot（None = 最右）
        self.target = None                 # 目标 Tile
        self.opts: Optional[dict] = None   # card_options 的答案（没问到 / 还没回来 = None）
        self.last_cast: str = ""           # 最近提交的命令（抉择牌补 choose 用）

    armed = property(lambda self: self.card)       # 旧名

    def clear(self):
        self.card = self.target = self.opts = None
        self.slot = None

    disarm = clear

    # ---- 要不要目标 ----
    def needs_target(self) -> Optional[bool]:
        """True 需要 / False 不需要 / None 不知道。优先用 card_options（模拟的合法走法），其次读到的卡面标志。"""
        c, o = self.card, self.opts
        if c is None or c.zone != "my_hand":
            return None
        if o and o.get("legal"):
            return not o.get("no_target")
        return c.needs_target

    # ---- 点击 ----
    def click(self, tile, model=None) -> Optional[str]:
        """点一张牌。只改选择，返回值只有一种情况非 None：挂起 select 时点手牌 = `select <牌>`。"""
        pend = getattr(model, "pending", None)
        if pend:
            if pend.get("need") == "select" and tile.zone == "my_hand":
                return "select " + tile.token
            return None                                    # 别的挂起：用按钮回答
        c = self.card
        if tile.token == (c.token if c else None):         # 再点选中的牌 = 取消
            self.clear()
            return None
        mine_pick = tile.mine and tile.kind != "hq" and tile.zone in ("my_hand", "my_support", "front")
        if c is None:
            if mine_pick:
                self.card = tile
            return None
        if tile.token == (self.target.token if self.target else None):     # 再点目标 = 取消目标
            self.target = None
            return None
        if mine_pick and (tile.zone == "my_hand" or c.zone != "my_hand"):  # 换牌：位置/目标清掉
            self.clear()
            self.card = tile
            return None
        if tile.zone == "enemy_hand":
            return None
        if c.zone == "my_hand" and self.needs_target() is False:
            return None                                    # 这张牌不要目标：点场上不生效
        if c.zone != "my_hand" and (tile.mine or tile.zone not in ("enemy_support", "front")):
            return None                                    # 单位只能攻击敌方
        self.target = tile
        return None

    def toggle_slot(self, k: int, n: int):
        """点“＋”：再点同一个取消；最右（k=n+1）就是默认，不记。"""
        self.slot = None if (k > n or self.slot == k) else k

    # ---- 命令 ----
    def command(self) -> Optional[str]:
        c = self.card
        if c is None:
            return None
        if c.zone == "my_hand":
            parts = ["deploy" if c.kind == "unit" else "play", c.token]
            if c.kind == "unit" and self.slot:
                parts.append(str(self.slot))
            if self.target is not None and self.needs_target() is not False:
                parts += ["with", self.target.token]
            return " ".join(parts)
        if self.target is not None:
            return "attack %s with %s" % (c.token, self.target.token)
        return "front %s%s" % (c.token, (" %d" % self.slot) if self.slot else "")

    def missing(self) -> str:
        """提交不了的原因（空 = 可以提交）。"""
        c = self.card
        if c is None:
            return "先点一张手牌或我方场上单位"
        if c.zone == "my_hand" and (self.opts or {}).get("legal") is False:
            return (self.opts.get("why") or "这张牌现在无法打出")[:60]
        if c.zone == "my_hand" and self.needs_target() and self.target is None:
            return "这张牌需要指向目标：点场上一个目标"
        return ""

    def submit(self) -> Optional[str]:
        cmd = self.command()
        if cmd is None or self.missing():
            return None
        self.last_cast = cmd
        self.clear()
        return cmd

    def hint(self) -> str:
        c, o = self.card, self.opts or {}
        if c is None:
            return "点一张手牌或我方单位"
        parts = []
        if c.zone == "my_hand":
            nt = self.needs_target()
            parts.append({True: "需要目标：点场上一个%s" % ("已选 " + self.target.token if self.target else "目标（高亮的是合法目标）"),
                          False: "不需要目标", None: "目标：" + ("已选 " + self.target.token if self.target else "未选（可选）")}[nt])
            if c.kind == "unit":
                parts.append("位置：%s（点＋，再点取消）" % (self.slot or "最右"))
            if o.get("can_game") is False:
                parts.append("⚠ 游戏判定现在不能打%s" % ((" — " + o["why"]) if o.get("why") else ""))
        else:
            parts.append("攻击：点一个敌方单位/总部" if o.get("targets") is None or o.get("targets") else "")
            parts.append("上线位置：在前线点＋（现在：%s）" % (self.slot or "最右"))
        return "；".join(x for x in parts if x)


# ---------------------------------------------------------------------------
# 控件
# ---------------------------------------------------------------------------
def option_text(ch: dict, ids: dict) -> List[str]:
    """抉择选项 → [(i, 效果文字)]。"""
    out = []
    for key, label, payload in ch["options"]:
        text = "; ".join(ET.describe(payload, None, None, lambda cid: ids.get(cid, "card#%s" % cid))) \
            if isinstance(payload, dict) else str(label)
        out.append((key, text or str(label)))
    return out


class Flow(tk.Frame):
    """子控件从左到右排、排不下就换行（按各自的实际宽度，窗口变窄时自动重排）。
    `items` 只含卡块；“＋”位置按钮（gap）另记，按次序夹在卡块之间，不计入 `items`。"""

    def __init__(self, master, pad=4, **kw):
        super().__init__(master, **kw)
        self.pad = pad
        self.items: list = []
        self._gaps: list = []                      # [(插在第几个卡块之前, 控件)]
        self._lw = 0
        self.bind("<Configure>", lambda e: self._layout(e.width))

    def add(self, w):
        self.items.append(w)

    def clear(self):
        for w in self.items + [g for _i, g in self._gaps]:
            w.destroy()
        self.items, self._gaps = [], []

    def set_gaps(self, specs):
        """specs = [(插在第 i 个卡块之前（== len(items) 表示末尾）, 工厂(父控件) → 控件)]。传空 = 去掉所有“＋”。"""
        for _i, g in self._gaps:
            g.destroy()
        self._gaps = [(i, f(self)) for i, f in specs]
        self.finish()

    def finish(self):
        self._layout(self.winfo_width() or 1, force=True)

    def _layout(self, width, force=False):
        if width == self._lw and not force:
            return
        self._lw = width
        seq = []
        for k, w in enumerate(self.items):
            seq += [(0, g) for i, g in self._gaps if i == k]
            seq.append((1, w))
        seq += [(0, g) for i, g in self._gaps if i >= len(self.items)]
        rows, cur, x = [], [], 0
        for kind, w in seq:
            w.update_idletasks()
            ww = w.winfo_reqwidth()
            if cur and x + ww > width:
                rows.append(cur)
                cur, x = [], 0
            cur.append((kind, w, x, ww))
            x += ww + self.pad
        if cur:
            rows.append(cur)
        y = self.pad
        for row in rows:
            rh = max(w.winfo_reqheight() for _k, w, _x, _w in row if _k == 1) if any(k == 1 for k, *_ in row) else 28
            for kind, w, x0, ww in row:
                if kind == 1:
                    w.place(x=x0 + self.pad, y=y, width=ww)
                else:
                    w.place(x=x0 + self.pad, y=y, width=ww, height=rh)      # “＋”按钮拉到和这一行卡块一样高
            y += rh + self.pad
        self.configure(height=max(y, 1))


class TileView(tk.Frame):
    """一张卡块：名字、费用/攻防/关键词、规则文本。点它 → `on_click(tile)`。"""

    def __init__(self, master, tile, on_click: Callable, extra: Optional[Callable] = None):
        super().__init__(master, bd=1, relief="solid", bg=COL_PLAIN)
        self.tile = tile
        tk.Frame(self, width=TILE_W, height=1, bg=COL_PLAIN).pack()
        stats = []
        if tile.cost is not None:
            stats.append("c%g" % tile.cost)
        if tile.kind != "order" and (tile.atk is not None or tile.dfn is not None):
            stats.append("HQ %s" % tile.dfn if tile.kind == "hq" else str(tile.atk) if isinstance(tile.atk, str)
                         else "%s/%s" % ("-" if tile.atk is None else tile.atk, "-" if tile.dfn is None else tile.dfn))
        if tile.kw:
            stats.append("[%s]" % ", ".join(tile.kw))
        if tile.pinned:
            stats.append("pinned")
        self.labels = [
            tk.Label(self, text="%s  %s" % (tile.token, tile.name), font=("Consolas", 9, "bold"), anchor="w",
                     justify="left", wraplength=TILE_W - 8, bg=COL_PLAIN),
        ]
        if stats:
            self.labels.append(tk.Label(self, text=" ".join(stats), font=("Consolas", 9), anchor="w", justify="left",
                                        wraplength=TILE_W - 8, bg=COL_PLAIN))
        if tile.text:
            self.labels.append(tk.Label(self, text=tile.text, font=("Segoe UI", 8), fg="#444", anchor="w", justify="left",
                                        wraplength=TILE_W - 8, bg=COL_PLAIN))
        for lb in self.labels:
            lb.pack(fill="x", padx=3, pady=1)
        if extra is not None:                          # extra(父控件) → 卡块底部的控件（如“选这张”按钮）
            extra(self).pack(fill="x", padx=3, pady=3)
        self.paint(COL_MINE if tile.mine else COL_ENEMY if tile.zone != "pending" else COL_PLAIN)
        for w in [self] + list(self.winfo_children()):
            w.bind("<Button-1>", lambda e, t=tile: on_click(t))

    def paint(self, color: str):
        self.configure(bg=color)
        for w in self.winfo_children():
            if isinstance(w, (tk.Label, tk.Frame)):
                w.configure(bg=color)


class BoardView(ttk.Frame):
    """点选盘面。`run_cb(命令串)` 由外面执行（和命令框走同一条路）；`show(model)` 在界面线程里调。"""

    def __init__(self, master, run_cb: Callable[[str], None], query_cb: Optional[Callable[[str], None]] = None):
        super().__init__(master)
        self.run_cb = run_cb
        self.query_cb = query_cb                      # query_cb(牌号)：后台问 card_options，答案回来调 set_options
        self.picker = Picker()
        self.model = None
        self._last = None
        self._choice: Optional[tuple] = None          # (命令串, Result.choices, labels)
        self._build()

    def _build(self):
        self.head = ttk.Label(self, text="", wraplength=TILE_W * 3)
        self.head.pack(fill="x", padx=6, pady=(4, 0))
        self.bar = ttk.Frame(self)
        self.bar.pack(fill="x", padx=6, pady=2)
        # 固定 2 行高、宽度随父级：悬停提示换字时控件尺寸不变，整个页面不会跟着重排
        self.hint = tk.Label(self.bar, text="", width=1, height=2, anchor="nw", justify="left", fg="#555", wraplength=TILE_W * 3)
        self.hint.pack(side="top", fill="x")
        self.btns = ttk.Frame(self.bar)
        self.btns.pack(side="top", anchor="w", fill="x")
        self.canvas = tk.Canvas(self, highlightthickness=0, height=420)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.zones: dict = {}
        for key, title in ZONE_TITLES:
            lf = ttk.LabelFrame(self.inner, text=title)
            lf.pack(fill="x", padx=4, pady=3)
            fl = Flow(lf)
            fl.pack(fill="x")
            self.zones[key] = fl
        lf = ttk.LabelFrame(self.inner, text="待回答的选择")
        self.choice_box = Flow(lf)
        self.choice_box.pack(fill="x")
        self.choice_frame = lf

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ---- 显示 ----
    def show(self, model, force: bool = False):
        """盘面没变就不重画（轮询每 2 秒来一次，重画会打断点击、闪烁）。"""
        if model is None:
            return
        if not force and model == self._last:
            return
        self._last = self.model = model
        self.head.configure(text=model.header)
        self._draw()

    def _draw(self):
        m = self.model
        for key, fl in self.zones.items():
            fl.clear()
            for t in m.zones.get(key, []):
                tv = TileView(fl, t, self._on_tile)
                fl.add(tv)
            fl.finish()
        self._paint()
        self._draw_gaps()
        self._draw_choice()
        self._draw_bar()

    def _paint(self):
        c, tg = self.picker.card, self.picker.target
        valid = set((self.picker.opts or {}).get("targets") or ())
        for fl in self.zones.values():
            for tv in fl.items:
                t = tv.tile
                col = COL_MINE if t.mine else COL_ENEMY
                if c is not None and t.token == c.token:
                    col = COL_ARMED
                elif tg is not None and t.token == tg.token:
                    col = COL_TARGET
                elif t.token in valid:
                    col = COL_AIM                                  # 合法目标（只提示，不拦着你点别的）
                tv.paint(col)

    def _draw_bar(self):
        for w in self.btns.winfo_children():
            w.destroy()
        m, pk = self.model, self.picker
        pend = m.pending if m else None
        if pend:
            hint = {"spawn": "三选一：选一张（下面的候选）",
                    "select": "点一张我方手牌（select）", "choose": "抉择：选一个选项"}.get(pend["need"], "")
            if pend["need"] == "forecast":                          # 预报的候选都在“待回答的选择”区（第一层还有第 4 项 forecast）
                if (pend.get("meta") or {}).get("merged"):
                    hint = "预报：点“选这张” = 真的选一张（undo 可退回上一步）"
                elif int(pend.get("layer") or 1) <= 1:
                    hint = "预报：先选天气（再选档），或第 4 个选项“运行 forecast”先把剩下的候选一次列出来（可 undo）"
                else:
                    hint = "预报：点“选这张” = 真的选这一档（第一层已经选过天气，不能再 forecast）"
            self._set_hint(hint)
        else:
            self._set_hint(pk.hint())
            cmd, miss = pk.command(), pk.missing()
            b = ttk.Button(self.btns, text=("提交：" + cmd) if cmd else "提交", command=self._submit)
            b.pack(side="left", padx=2)
            if cmd is None or miss:
                b.state(["disabled"])
            if pk.card is not None:
                ttk.Button(self.btns, text="清除选择", command=self._cancel).pack(side="left", padx=2)
            if miss and pk.card is not None:
                ttk.Label(self.btns, text=miss, foreground="#b45309").pack(side="left", padx=6)
        ttk.Button(self.btns, text="reset", command=lambda: self._fire("reset")).pack(side="right", padx=2)
        ttk.Button(self.btns, text="undo", command=lambda: self._fire("undo")).pack(side="right", padx=2)
        if m is not None and getattr(m, "stale", False):
            # 实时盘面在模拟开始后变了：下面的点选/提交还按旧基线拼命令 ⇒ 在 undo/reset 旁提示 + 一键 reset。
            ttk.Button(self.btns, text="reset（盘面已变）", command=lambda: self._fire("reset")).pack(side="right", padx=2)
            ttk.Label(self.btns, text="实时盘面已经变了：建议 reset（下面是旧基线）",
                      foreground="#b36b00").pack(side="right", padx=8)

    def _gap_btn(self, par, tip: str, cmd, on: bool = False):
        bt = tk.Button(par, text="＋", width=2, font=("Segoe UI", 11, "bold"), command=cmd,
                       **({"bg": COL_ARMED, "relief": "sunken"} if on else {}))
        bt.bind("<Enter>", lambda e: self.hint.configure(text=tip))
        bt.bind("<Leave>", lambda e: self.hint.configure(text=self._hint_text()))
        return bt

    def _draw_gaps(self):
        """选中牌后，在对应阵线的卡块之间放“＋”（点选位置，再点取消）：
        单位牌 → 我方支援线（**总部也算一格**，两侧都有）= 部署位置；我方支援线上的单位 → 前线我方单位之间 = 上线位置。"""
        c, m = self.picker.card, self.model
        specs = {k: [] for k in self.zones}
        if c is not None and m is not None and not m.pending:
            if c.zone == "my_hand" and c.kind == "unit":
                its = self.zones["my_support"].items
                n = len(its)
                cur = self.picker.slot or n + 1
                for k in range(n + 1):
                    nm = ("插在 %s 左边" % ("总部" if its[k].tile.kind == "hq" else its[k].tile.name)) if k < n else "最右（默认）"
                    specs["my_support"].append((k, lambda par, k=k, nm=nm, on=(k + 1 == cur): self._gap_btn(
                        par, nm, lambda: self._gap(k + 1, n), on)))
            elif c.zone == "my_support" and c.kind != "hq":
                its = self.zones["front"].items
                mine = [i for i, tv in enumerate(its) if tv.tile.mine]
                cur = self.picker.slot or len(mine) + 1
                pos = [(i, j + 1) for j, i in enumerate(mine)] + [(mine[-1] + 1 if mine else len(its), len(mine) + 1)]
                for idx, k in pos:
                    nm = ("上线，插在 %s 左边" % its[idx].tile.name) if idx < len(its) and k <= len(mine) else "上线，放最右（默认）"
                    specs["front"].append((idx, lambda par, k=k, nm=nm, on=(k == cur): self._gap_btn(
                        par, nm, lambda: self._gap(k, len(mine)), on)))
        for key, fl in self.zones.items():
            fl.set_gaps(specs[key])

    def _gap(self, k, n):
        self.picker.toggle_slot(k, n)
        self._refresh()

    def _set_hint(self, text: str):
        self._base = text
        self.hint.configure(text=text)

    def _hint_text(self) -> str:
        return getattr(self, "_base", "")

    def _cancel(self):
        self.picker.clear()
        self._refresh()

    def _refresh(self):
        self._paint()
        self._draw_gaps()
        self._draw_bar()

    def _draw_choice(self):
        """“待回答的选择”区：抉择按钮 / 三选一候选 / 预报的两段式（第一层 3 天气 + 第 4 项 forecast；合并后的候选 9 或 3 张）。"""
        self.choice_box.clear()
        m = self.model
        shown = False
        title = "待回答的选择"
        if self._choice is not None:
            cmd, ch, ids = self._choice
            for key, text in option_text(ch, ids):
                b = ttk.Button(self.choice_box, text="choose %s: %s" % (key, text), width=24,
                               command=lambda k=key, c=cmd: self._pick_choose(c, k))
                self.choice_box.add(b)
            title = "抉择选项"
            shown = bool(ch["options"])
        elif m is not None and m.pending and m.pending["need"] == "spawn":
            for t in m.pending_tiles:
                tv = TileView(self.choice_box, t, lambda _t: None,
                              extra=lambda par, i=t.token: ttk.Button(par, text="选这张", command=lambda: self._fire("spawn " + i)))
                self.choice_box.add(tv)
            title = "三选一：选一张"
            shown = bool(m.pending_tiles)
        elif m is not None and m.pending and m.pending["need"] == "forecast":
            meta = m.pending.get("meta") or {}
            if meta.get("merged"):                                 # forecast 走完一步：候选就是牌，点“选这张”= 真的选
                for t in m.pending_tiles:
                    self.choice_box.add(TileView(self.choice_box, t, lambda _t: None,
                                                 extra=lambda par, i=t.token: ttk.Button(
                                                     par, text="选这张", command=lambda: self._fire("spawn " + i))))
                title = "预报候选 %d 张：点“选这张”= 真的选（undo 可退回上一步）" % len(m.pending_tiles)
            else:                                                  # 第一层：3 个天气候选 + 第 4 项“运行 forecast”
                layer = int(m.pending.get("layer") or 1)
                for t in m.pending_tiles:
                    self.choice_box.add(TileView(self.choice_box, t, lambda _t: None,
                                                 extra=lambda par, i=t.token: ttk.Button(
                                                     par, text=("选这种天气" if layer <= 1 else "选这张"),
                                                     command=lambda: self._fire("spawn " + i))))
                if layer <= 1:                                     # 第二层只剩 3 张、当面就能选，不再需要 forecast 这一步
                    self.choice_box.add(ttk.Button(self.choice_box, text="运行 forecast：先把剩下的候选一次列出来",
                                                   command=lambda: self._fire("forecast")))
                    title = ("预报：先选天气（再选档），或运行 forecast 把那 %d 张候选一次列出来"
                             % (len(m.pending_tiles) * 3))
                else:
                    title = "预报：这一种天气的 %d 张，点“选这张”= 真的选" % len(m.pending_tiles)
            shown = bool(m.pending_tiles)
        self.choice_frame.configure(text=title)
        if shown:
            self.choice_box.finish()
            self.choice_frame.pack(fill="x", padx=4, pady=3, before=self.zones["enemy_hand"].master)
        else:
            self.choice_frame.pack_forget()

    # ---- 交互 ----
    def _submit(self):
        cmd = self.picker.submit()
        if cmd:
            self._choice = None
            self.run_cb(cmd)
        self._refresh()

    def _fire(self, cmd: Optional[str]):
        """undo / reset / spawn / forecast 这类不走“三件套”的命令：发出，并清掉当前选择。"""
        if not cmd:
            return
        self._choice = None
        self.picker.clear()
        self.run_cb(cmd)
        self._refresh()

    def _on_tile(self, tile):
        self._choice = None
        before = self.picker.card.token if self.picker.card else None
        cmd = self.picker.click(tile, self.model)
        after = self.picker.card.token if self.picker.card else None
        if after is not None and after != before and self.query_cb is not None:
            self.picker.opts = None
            self.query_cb(after)                           # 问：这张牌能怎么用（可指向谁、要不要目标）；答案回来走 set_options
        self._refresh()
        if cmd:
            self.run_cb(cmd)

    def set_options(self, tok: str, opts: dict) -> None:
        """`card_options` 的答案回来了（界面线程）。过期的（已经换了牌）丢掉。"""
        c = self.picker.card
        if c is None or c.token != tok or not opts:
            return
        self.picker.opts = opts
        self._refresh()

    def _pick_choose(self, cmd: str, key):
        self._choice = None
        self.run_cb("%s choose %s" % (cmd, key))

    def on_results(self, results) -> None:
        """命令的结果回来后：若最后一条是“抉择牌缺 choose”，把选项做成按钮。"""
        self._choice = None
        if results:
            r = results[-1]
            ch = r.choices
            if (not r.ok) and ch and ch.get("need") == "choose" and self.picker.last_cast:
                self._choice = (self.picker.last_cast, ch, r.labels or {})
        if self.model is not None:
            self._draw_choice()


def main(argv=None) -> int:
    """单独启动：`python -m gui.whatif_board`（整个盘面模拟窗口，含点选界面；也可 `python -m gui.whatif_panel`）。"""
    from .whatif_panel import main as panel_main
    return panel_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
