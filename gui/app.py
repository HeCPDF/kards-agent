# -*- coding: utf-8 -*-
"""KARDS Agent 控制面板。运行：`python -m gui.app`（在 kards-agent 目录）。

面板只读写文件、不碰游戏：开始/停止通过 `control.json`，“开始”再往 `live_cmd.txt` 追加一行让常驻监听器去跑。
“启动监听器”会 attach 游戏进程，所以点之前有确认框；游戏没开时不要点。

功能清单参照 OCR-Kards-Auto 面板（致谢，见 NOTICE）（见 `gui/core.py` 头注释的对照表）：功能选择、开始/停止、状态胶囊（运行中/空闲/已停止+退出码提示）、
实时状态（局数/费用/盘面/最近动作/问题数/在等窗口）、日志查看（着色 + 只看关键行 + 自动滚动/到底部 + 按文件指纹刷新）、
检测更新（手动 + 开机静默 + 留痕）、窗口尺寸位置记忆、关面板时优雅带走**自己启动的**监听器、`--selftest`。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import autoplay as AP
from . import control as C
from . import core as K
from . import history as H
from . import update_check as U
from . import watcher as W

from base import paths as _paths

LIVE_SESSION = os.path.join(_paths.AGENT_ROOT, "tools", "live_session.py")
LIVE_LOG = _paths.LIVE_LOG
APP_NAME = "KARDS Agent"
STATE_ZH = {"idle": "空闲", "preparing": "准备中", "starting": "正在开局", "playing": "对局中",
            "waiting_next": "等待下一局", "finished": "已完成", "stopped": "已停止"}
COLS = (("i", "#", 46), ("turn", "回合", 50), ("phase", "阶段", 60), ("kind", "动作", 100),
        ("card", "牌", 56), ("target", "目标", 56), ("score", "评分", 64), ("ok", "成功", 50),
        ("t_decide", "决策s", 60), ("t_sim", "模拟s", 56), ("t_exec", "执行s", 56), ("eff", "效果来源", 150), ("gaps", "缺口", 46), ("note", "备注", 260))
TAG_COLORS = {"ok": "#1b8a3a", "warn": "#b36b00", "bad": "#c0172d", "act": "#1d4f91", "state": "#6a3fa0", "dim": "#8a8f98"}
PILL_COLORS = {"off": "#8a8f98", "idle": "#1d4f91", "run": "#1b8a3a", "stop": "#b36b00", "bad": "#c0172d"}
PS_QUERY = W.PS_QUERY


def watcher_pids() -> list:
    """常驻监听器（live_session.py）的进程号；PowerShell 查，失败返回 []。"""
    return W.pids()


def tail_file(path: str, n: int = 150) -> str:
    return "\n".join(K.tail_lines(path, n))


class ToolTip:
    """悬停提示（退出码翻译成人话挂在这里）。"""

    def __init__(self, widget):
        self.w, self.text, self.tip = widget, "", None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def set(self, text: str):
        self.text = text or ""

    def _show(self, _e=None):
        if not self.text or self.tip is not None:
            return
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        x, y = self.w.winfo_rootx() + 10, self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=420).pack()

    def _hide(self, _e=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class LogView(ttk.Frame):
    """带着色 / 只看关键行 / 自动滚动 / 按文件指纹刷新的日志区（对标上游日志面板）。"""

    def __init__(self, master, path_fn, title: str, loader=None):
        super().__init__(master)
        self.path_fn, self.loader = path_fn, loader
        self.only_key = tk.BooleanVar(value=False)
        self.autoscroll, self._rev = True, None
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=title).pack(side="left")
        ttk.Checkbutton(bar, text="只看关键行", variable=self.only_key,
                        command=self.refresh_now).pack(side="left", padx=10)
        ttk.Button(bar, text="到底部", command=self.to_bottom).pack(side="left")
        self.lbl_warn = ttk.Label(bar, text="", foreground="#c0172d")
        self.lbl_warn.pack(side="right")
        self.txt = tk.Text(self, wrap="none", font=("Consolas", 10))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in TAG_COLORS.items():
            self.txt.tag_configure(tag, foreground=color)
        self.txt.bind("<MouseWheel>", lambda e: self.after(50, self._sync_autoscroll))
        self.txt.bind("<ButtonRelease-1>", lambda e: self._sync_autoscroll())

    def _sync_autoscroll(self):
        self.autoscroll = self.txt.yview()[1] >= 0.98

    def to_bottom(self):
        self.autoscroll = True
        self.txt.see("end")

    def refresh_now(self):
        self._rev = None
        self.refresh()

    def refresh(self):
        p = self.path_fn()
        # ★ 判据是“文件变没变”，不是“行数变没变”：尾部只取 400 行，行数会饱和，之后永远判没变（上游踩过的坑）
        rev = (K.log_rev(p) if p else "", self.only_key.get())
        if rev == self._rev:
            return
        self._rev = rev
        lines = self.loader() if self.loader else K.tail_lines(p, 400)
        if self.only_key.get():
            lines = K.only_key(lines)
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for ln in lines[-400:]:
            self.txt.insert("end", ln + "\n", K.mark_line(ln) or ())
        if self.autoscroll:
            self.txt.see("end")


class App(tk.Tk):
    def __init__(self, debug: bool = False, size=(1180, 760)):
        super().__init__()
        if os.environ.get("KARDS_GUI_SELFTEST_EXIT_MS", "").isdigit():
            self.withdraw()            # 无头冒烟：窗口不映射到桌面（不抢游戏焦点、不闪屏），控件照常创建、事件循环照常跑
        self.debug = debug
        self.title("%s 控制面板" % APP_NAME)
        self.geometry(K.geometry_string(K.load_win_state(), default=size))
        self.minsize(*K.WIN_MIN)
        self.rows, self._rows_sig, self._pids = [], None, []
        self._pids_ready = False       # 第一次查完监听器进程之前显示检测中
        self._launch = None            # 正在启动监听器时：{"proc", "t0", "log_off"}
        self._started_proc = None      # 本面板启动的监听器进程（关面板时只带走它）
        self._exit_info = None         # 本面板启动的监听器退出了：{"code", "hint"}
        self._game_info = ""           # 启动时检测到的游戏版本/构建
        self._play_t0 = None
        self._upd_url = ""
        self._toast_after = None
        self._build()
        self._load_control_into_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()
        threading.Thread(target=self._poll_watcher, daemon=True).start()
        threading.Thread(target=self._auto_update, daemon=True).start()
        # 无头冒烟（打包产物验收用）：设了 KARDS_GUI_SELFTEST_EXIT_MS=<毫秒> 就在窗口建好、跑过若干次 _tick 后自己关掉。
        ms = os.environ.get("KARDS_GUI_SELFTEST_EXIT_MS", "")
        if ms.isdigit():
            self.after(int(ms), self._selftest_exit)

    def _selftest_exit(self):
        """冒烟自动退出：先落一个标记文件（证明 Tk 窗口真建出来了、事件循环真跑过），再 destroy（不弹确认框、不碰监听器）。"""
        try:
            os.makedirs(K.STATE_DIR, exist_ok=True)
            with open(os.path.join(K.STATE_DIR, "gui_smoke_ok.txt"), "w", encoding="utf-8") as f:
                json.dump({"title": self.title(), "size": [self.winfo_width(), self.winfo_height()],
                           "children": len(self.winfo_children()), "frozen": _paths.FROZEN}, f, ensure_ascii=False)
        except Exception:                                     # noqa: BLE001
            pass
        self.destroy()

    # ---------------- 界面 ----------------
    def _build(self):
        hdr = ttk.Frame(self, padding=(10, 8))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="KARDS · AGENT", font=("", 13, "bold")).pack(side="left")
        self.lbl_ver = ttk.Label(hdr, text="v?", foreground="#666")
        self.lbl_ver.pack(side="left", padx=8)
        self.pill = tk.Label(hdr, text="监听器检测中…", fg="white", bg=PILL_COLORS["off"], padx=10, pady=2,
                             font=("", 10, "bold"))
        self.pill.pack(side="left", padx=8)
        self.pill_tip = ToolTip(self.pill)
        self.btn_stop = ttk.Button(hdr, text="⏹ 立即停", command=self.stop_now)
        self.btn_stop.pack(side="right", padx=2)
        self.btn_go = ttk.Button(hdr, text="▶ 开始", command=self.start)
        self.btn_go.pack(side="right", padx=2)
        self.btn_update = ttk.Button(hdr, text="检测更新", command=self.on_update)
        self.btn_update.pack(side="right", padx=8)

        body = ttk.Frame(self, padding=(8, 0))
        body.pack(fill="x")
        # ---- 功能选择 ----
        box = ttk.LabelFrame(body, text="功能选择", padding=8)
        box.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.v_sw = {}
        for i, sw in enumerate(K.SWITCHES):
            v = tk.BooleanVar(value=sw["default"])
            self.v_sw[sw["key"]] = v
            cb = ttk.Checkbutton(box, text=sw["label"], variable=v, command=self.apply_live_flags)
            cb.grid(row=i, column=0, sticky="w")
            ttk.Label(box, text=sw["hint"], foreground="#666").grid(row=i, column=1, sticky="w", padx=8)
        r = len(K.SWITCHES)
        self.v_total = tk.IntVar(value=1)
        self.v_unl = tk.BooleanVar(value=False)
        self.v_auto = tk.BooleanVar(value=True)
        self.v_turns = tk.IntVar(value=40)
        self.v_avoid = tk.StringVar(value="")
        ttk.Separator(box).grid(row=r, column=0, columnspan=2, sticky="ew", pady=6)
        row = ttk.Frame(box)
        row.grid(row=r + 1, column=0, columnspan=2, sticky="w")
        ttk.Label(row, text="跑几局").pack(side="left")
        ttk.Spinbox(row, from_=1, to=999, textvariable=self.v_total, width=6).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="不限局数", variable=self.v_unl).pack(side="left", padx=8)
        ttk.Checkbutton(row, text="打完自动开下一局", variable=self.v_auto, command=self.apply_live_flags).pack(side="left", padx=8)
        row2 = ttk.Frame(box)
        row2.grid(row=r + 2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(row2, text="每局最多回合").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=99, textvariable=self.v_turns, width=5).pack(side="left", padx=4)
        ttk.Label(row2, text="回避牌名(逗号分隔)").pack(side="left", padx=(14, 0))
        ttk.Entry(row2, textvariable=self.v_avoid, width=28).pack(side="left", padx=4)
        row3 = ttk.Frame(box)
        row3.grid(row=r + 3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(row3, text="⏸ 打完当前局后停", command=self.stop_after).pack(side="left", padx=2)
        ttk.Button(row3, text="保存设置（不开始）", command=self.save_settings).pack(side="left", padx=8)
        self.btn_watch = ttk.Button(row3, text="启动监听器…", command=self.start_watcher)
        self.btn_watch.pack(side="left", padx=8)
        ttk.Button(row3, text="停止监听器…", command=self.stop_watcher).pack(side="left")

        # ---- 实时状态 ----
        st = ttk.LabelFrame(body, text="实时状态", padding=8)
        st.pack(side="left", fill="both", expand=True)
        self.lbl_big = ttk.Label(st, text="—", font=("", 16, "bold"))
        self.lbl_big.grid(row=0, column=0, columnspan=2, sticky="w")
        self.lbl_sub = ttk.Label(st, text="", foreground="#555", wraplength=460, justify="left")
        self.lbl_sub.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.kv = {}
        for i, (k, label) in enumerate((("watcher", "监听器"), ("kre", "指挥点"), ("board", "模拟盘面"),
                                        ("act", "最近动作"), ("rounds", "本段局数"), ("opts", "启动参数"),
                                        ("elapsed", "本局用时"))):
            ttk.Label(st, text=label, foreground="#666").grid(row=2 + i, column=0, sticky="nw", padx=(0, 10))
            lbl = ttk.Label(st, text="—", wraplength=420, justify="left")
            lbl.grid(row=2 + i, column=1, sticky="w")
            self.kv[k] = lbl
        self.lbl_badge = tk.Label(st, text="", padx=6)
        self.lbl_badge.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.lbl_diag = ttk.Label(self, text="", foreground="#555", wraplength=1140, justify="left")
        self.lbl_diag.pack(anchor="w", padx=12, pady=(4, 0))

        # ---- 页签 ----
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=6)
        hist = ttk.Frame(nb)
        nb.add(hist, text="决策历史")
        bar = ttk.Frame(hist)
        bar.pack(fill="x")
        ttk.Label(bar, text="对局日志").pack(side="left")
        self.cb_log = ttk.Combobox(bar, width=34, state="readonly")
        self.cb_log.pack(side="left", padx=4)
        self.cb_log.bind("<<ComboboxSelected>>", lambda e: self._reload_rows(force=True))
        self.v_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="跟随最新", variable=self.v_follow).pack(side="left", padx=8)
        self.v_kind = tk.StringVar(value="全部")
        self.cb_kind = ttk.Combobox(bar, width=14, state="readonly", textvariable=self.v_kind, values=["全部"])
        self.cb_kind.pack(side="left")
        self.cb_kind.bind("<<ComboboxSelected>>", lambda e: self._fill_tree())
        self.lbl_sum = ttk.Label(bar, text="")
        self.lbl_sum.pack(side="right")
        pan = ttk.PanedWindow(hist, orient="vertical")
        pan.pack(fill="both", expand=True)
        tf = ttk.Frame(pan)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in COLS], show="headings", height=12)
        for key, title, w in COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        pan.add(tf, weight=3)
        self.txt_detail = tk.Text(pan, height=10, wrap="none")
        pan.add(self.txt_detail, weight=2)

        self.ev_view = LogView(nb, lambda: C.EVENTS, "事件日志（面板 / 自动对局）")
        nb.add(self.ev_view, text="事件日志")
        self.live_view = LogView(nb, lambda: LIVE_LOG, "监听器输出（live_log.txt）")
        nb.add(self.live_view, text="监听器输出")
        self.toast_lbl = tk.Label(self, text="", bg="#222", fg="white", padx=12, pady=8, justify="left", wraplength=520)

    # ---------------- 小部件 ----------------
    def toast(self, msg: str, ms: int = 3200):
        """右下角的临时提示；启动失败这类要人读的消息留久一点。"""
        self.toast_lbl.config(text=msg)
        self.toast_lbl.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
        if self._toast_after is not None:
            self.after_cancel(self._toast_after)
        self._toast_after = self.after(ms, self.toast_lbl.place_forget)

    def _set_pill(self, kind: str, text: str, tip: str = ""):
        self.pill.config(text=text, bg=PILL_COLORS[kind])
        self.pill_tip.set(tip)

    # ---------------- 控制 ----------------
    def _checked(self) -> dict:
        return {k: bool(v.get()) for k, v in self.v_sw.items()}

    def _settings(self) -> dict:
        avoid = [x.strip() for x in self.v_avoid.get().split(",") if x.strip()]
        d = {"games_total": max(1, int(self.v_total.get())), "unlimited": bool(self.v_unl.get()),
             "auto_next": bool(self.v_auto.get()), "turns": max(1, int(self.v_turns.get())), "avoid": avoid}
        d.update(K.build_opts(self._checked()))
        return d

    def _load_control_into_widgets(self):
        c = C.load_control()
        self.v_total.set(c["games_total"])
        self.v_unl.set(c["unlimited"])
        self.v_auto.set(c["auto_next"])
        self.v_turns.set(c["turns"])
        self.v_avoid.set(", ".join(c["avoid"] or []))
        for k, on in K.checked_from_control(c).items():
            if k in self.v_sw:
                self.v_sw[k].set(on)

    def save_settings(self):
        C.update_control(**self._settings())
        self.toast("设置已保存")

    def apply_live_flags(self):
        """对局中改开关也立即生效（只写这几项，别覆盖其它）。`forbid` 每步由 player.play.play 下一局读取；`auto_next` 立即生效。"""
        o = K.build_opts(self._checked())
        C.update_control(auto_next=bool(self.v_auto.get()), forbid=o["forbid"], dry_run=o["dry_run"])

    def start(self):
        if not self._pids:
            messagebox.showwarning("监听器没在运行", "常驻监听器（live_session.py）没有运行。先点“启动监听器…”。")
            return
        if C.load_status().get("busy"):
            self.save_settings()
            self.toast("已经有一局在编排中；设置已保存，不重复启动。")
            return
        C.update_control(run=True, games_done=0, abort_now=False, **self._settings())
        C.update_status(state="preparing", note="已发出开始指令，等待监听器接手")
        AP.enqueue()
        C.log_event("面板：开始（%s）" % json.dumps(self._settings(), ensure_ascii=False))
        self._play_t0 = time.time()
        self.toast("已发出开始指令")

    def stop_after(self):
        C.update_control(auto_next=False)
        self.v_auto.set(False)
        C.log_event("面板：打完当前局后停")
        self.toast("打完当前这局后不再开新局")

    def stop_now(self):
        if messagebox.askyesno("立即停", "当前这局会在下一步开始前收手（不再操作游戏，也不会自动认输）。确定？"):
            C.update_control(abort_now=True, run=False)
            C.log_event("面板：立即停")
            self.toast("已发出立即停")

    # ---------------- 监听器 ----------------
    def start_watcher(self):
        if self._launch is not None:
            messagebox.showwarning("正在启动中", "监听器正在启动（已 %d 秒，等待它 attach 游戏），请稍候，不要重复启动。"
                                   % (time.time() - self._launch["t0"]))
            return
        if self._pids:
            messagebox.showinfo("已在运行", "监听器已经在运行（pid %s）。" % self._pids)
            return
        # 解释器：真跑探针，坏的跳过；一个都没有就把试过的整张表念出来
        py = K.engine_python()
        if py is None:
            detail = "\n".join("  · %s\n      %s" % (p, why) for p, why in K._PYTHON_DIAG) or "  · 一个 Python 都没找到"
            self.toast("找不到能用的 Python 解释器。试过：\n" + detail, 12000)
            messagebox.showerror("找不到解释器", "试过这些：\n" + detail)
            return
        info = W.detect_version()
        # ★ 2026-10-03 P7：版本号不是放行条件。RVA 由监听器 attach 时自己确定（缓存 → 种子复验 → 扫描），
        #   不再传 KARDS_BUILD（它现在只是调试覆盖）；只有“游戏没开/认不出版本”才挡在这里。
        if info["version"] is None:
            messagebox.showerror("认不出游戏", info["why"] + "\n\n不启动（监听器 attach 需要游戏在跑）。")
            return
        if not messagebox.askyesno(
                "启动监听器", "这会启动常驻监听器并 attach 到 KARDS 游戏进程。\n\n"
                "检测到：%s\nRVA 由监听器 attach 时自行确定（缓存 → 种子复验 → 扫描；预期来源：%s）。解释器 %s。\n\n"
                "· 游戏必须已经打开；\n· 不要对同一个游戏起第二个监听器；\n确定启动？" % (W.describe(info), info.get("source"), py)):
            return
        self._game_info = W.describe(info)
        env = dict(os.environ)
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        _paths.ensure_dirs()
        out = open(_paths.LIVE_STDOUT, "a", encoding="utf-8")
        try:
            off = os.path.getsize(LIVE_LOG)
        except OSError:
            off = 0
        proc = subprocess.Popen(K.listener_cmd(py, LIVE_SESSION), cwd=_paths.AGENT_ROOT, stdout=out, stderr=out,
                                stdin=subprocess.DEVNULL, creationflags=flags, env=env)
        self._started_proc, self._exit_info = proc, None
        self._launch = {"proc": proc, "t0": time.time(), "log_off": off}
        self._pids = sorted(set(self._pids) | {proc.pid})
        self.btn_watch.config(state="disabled")
        C.log_event("面板：启动监听器（pid %s）" % proc.pid)

    def stop_watcher(self):
        """优雅退出（abort_now + quit，最多等 90 s）；超时再问要不要强杀。"""
        if not self._pids:
            messagebox.showinfo("没有在运行", "没有找到监听器进程。")
            return
        if not messagebox.askyesno(
                "停止监听器",
                "会先让当前这局收手（不再操作游戏），再让监听器正常退出，最多等 90 秒。\n"
                "监听器退出后游戏本身不受影响，但需要重新“启动监听器”才能继续自动对局。\n\n确定？"):
            return
        W.request_quit()
        self.toast("正在等待监听器退出…", 6000)

        def work():
            ok = W.wait_exit(90.0)
            self.after(0, lambda: self._after_stop(ok))
        threading.Thread(target=work, daemon=True).start()

    def _after_stop(self, ok: bool):
        self._pids = W.pids()
        if ok:
            C.update_status(state="stopped", busy=False, note="监听器已正常退出")
            messagebox.showinfo("已停止", "监听器已正常退出。")
            return
        if messagebox.askyesno(
                "没能正常退出",
                "90 秒内监听器没有退出（可能卡在一步里）。\n\n强制结束进程？\n"
                "注意：强杀会卸载 frida agent，历史上出现过游戏随之崩溃（CLAUDE.md 弯路 #31）。确定要强杀？",
                icon="warning"):
            killed = W.force_kill()
            C.update_status(state="stopped", busy=False, note="监听器已强杀 %s" % killed)
            self._pids = W.pids()

    def _on_close(self):
        """关面板：记窗口几何；若监听器是**本面板启动的**，优雅带走它（不强杀、不碰别人起的监听器）。"""
        try:
            K.save_win_state(self.winfo_width(), self.winfo_height(), self.winfo_x(), self.winfo_y())
        except Exception:                                     # noqa: BLE001
            pass
        p = self._started_proc
        if p is not None and p.poll() is None:
            if C.load_status().get("busy") and not messagebox.askyesno(
                    "关闭面板", "当前有一局在进行。关闭面板会让本面板启动的监听器收手并退出（游戏不受影响）。确定关闭？"):
                return
            try:
                W.request_quit()
                K.log_update("[面板] 关窗口时请监听器优雅退出（pid %s）" % p.pid)
                t0 = time.time()
                while p.poll() is None and time.time() - t0 < 10:
                    self.update()
                    time.sleep(0.2)
            except Exception:                                 # noqa: BLE001
                pass                                          # 带不走也不许挡住关窗口
        self.destroy()

    # ---------------- 检测更新 ----------------
    def _auto_update(self):
        """开机后台静默查一次：查到才改按钮，其余不说话；但一定留痕。"""
        time.sleep(3)
        try:
            ver = K.read_version()
            r = U.check(str(ver.get("version") or "0.0.0"), ver.get("update_url") or None)
            K.log_update("[自动] 本地 %s ok=%s has_update=%s via=%s | %s" % (
                ver.get("version"), r.get("ok"), r.get("has_update"), r.get("via"), r.get("msg")))
            if r.get("has_update"):
                self._upd_url = r.get("url") or ""
                self.after(0, lambda: self.btn_update.config(text="有新版本 %s" % (r.get("remote") or "")))
        except Exception as e:                                # noqa: BLE001
            K.log_update("[自动] 出错（%s: %s）" % (type(e).__name__, e))

    def on_update(self):
        if self._upd_url:
            import webbrowser
            try:
                webbrowser.open(self._upd_url)
                self.toast("已用浏览器打开：" + self._upd_url, 7000)
            except Exception as e:                            # noqa: BLE001
                self.toast("打不开浏览器（%s）。地址：%s" % (e, self._upd_url), 9000)
            return
        ver = K.read_version()
        r = U.check(str(ver.get("version") or "0.0.0"), ver.get("update_url") or None)
        K.log_update("[手动] 本地 %s ok=%s has_update=%s via=%s | %s" % (
            ver.get("version"), r.get("ok"), r.get("has_update"), r.get("via"), r.get("msg")))
        self.toast(r["msg"], 7000 if r["ok"] else 11000)
        if r.get("has_update") and r.get("url"):
            self._upd_url = r["url"]
            self.btn_update.config(text="打开发布页")

    # ---------------- 刷新 ----------------
    def _poll_watcher(self):
        while True:
            self._pids = W.pids()
            self._pids_ready = True
            time.sleep(5)

    def _tick(self):
        try:
            self._refresh_status()
            self._refresh_logs()
            self._reload_rows()
            self.ev_view.refresh()
            self.live_view.refresh()
        except Exception as exc:                              # noqa: BLE001
            self.toast("刷新出错：%s: %s" % (type(exc).__name__, exc), 6000)
        self.after(2500, self._tick)

    def _check_launch(self):
        """“启动中”的收尾：attach 成功 / precheck 不可用 / 进程提前死掉 / 超时。"""
        L = self._launch
        if L is None:
            return
        age = time.time() - L["t0"]
        st = W.launch_state(LIVE_LOG, L["log_off"])
        rc = L["proc"].poll()
        if st == "ready":
            self._launch = None
            C.log_event("面板：监听器已就绪（%.0f s）" % age)
            self.toast("监听器已就绪（%.0f s）" % age)
        elif st == "failed" or rc is not None or age > 150:
            self._launch = None
            tail = "\n".join(K.tail_lines(_paths.LIVE_STDOUT, 40))
            hint = K.explain_exit_code(rc, tail + ("\nprecheck.available() = False" if st == "failed" else ""))
            why = ("attach 失败（precheck 不可用）" if st == "failed"
                   else "进程提前退出（退出码 %s）" % rc if rc is not None else "150 秒内没有 attach 结果")
            self._exit_info = {"code": rc, "hint": hint}
            C.log_event("面板：监听器启动失败：%s" % why)
            messagebox.showerror("监听器启动失败", why + "\n\n" + hint + "\n\n看“监听器输出”页签和 live_stdout.txt。")
        if self._launch is None:
            self.btn_watch.config(state="normal")

    def _refresh_status(self):
        self._check_launch()
        self.lbl_ver.config(text="v" + str(K.read_version().get("version", "?")))
        c, s = C.load_control(), C.load_status()
        ps = K.parse_status(C.tail_events(300), self.rows, s)
        running = bool(s.get("busy"))
        # 状态胶囊
        if self._launch is not None:
            self._set_pill("stop", "监听器启动中… %d s" % (time.time() - self._launch["t0"]))
        elif not self._pids_ready:
            self._set_pill("off", "监听器检测中…")
        elif not self._pids:
            if self._exit_info is not None:
                self._set_pill("bad", "监听器已退出（退出码 %s）" % self._exit_info["code"], self._exit_info["hint"])
            else:
                self._set_pill("off", "监听器未运行")
        elif running:
            self._set_pill("run", "运行中 pid=%s" % ",".join(map(str, self._pids)))
        elif s.get("state") == "stopped":
            self._set_pill("stop", "已停止", s.get("note") or "")
        else:
            self._set_pill("idle", "空闲 pid=%s" % ",".join(map(str, self._pids)))
        self.btn_go.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        if running and self._play_t0 is None:
            self._play_t0 = time.time()
        if not running:
            self._play_t0 = None
        # 实时状态
        big = STATE_ZH.get(s["state"], s["state"]) if (running or s["state"] == "stopped") else "空闲"
        self.lbl_big.config(text=big)
        rr = " · ".join("第%d局 %s%s" % (r["n"], "打完" if r["played"] else "未打成", (" %s min" % r["min"]) if r["min"] is not None else "")
                        for r in ps["rounds"][-3:])
        total = "∞" if c["unlimited"] else str(c["games_total"])
        if ps["waiting"]:
            self.lbl_sub.config(text="⚠ 在等：上一局的读数没清 / 游戏没在前面（下面的读数可能是旧的）", foreground="#b36b00")
        else:
            self.lbl_sub.config(text=(s.get("note") or rr or ("点右上角「开始」" if not running else "")), foreground="#555")
        self.kv["watcher"].config(text=("pid %s" % ",".join(map(str, self._pids)) if self._pids else "未运行")
                                  + (("  |  " + self._game_info) if self._game_info else ""))
        self.kv["kre"].config(text="—" if ps["kredits"] is None else str(ps["kredits"]))
        b = ps["board"]
        self.kv["board"].config(text="—" if not b else "我方/敌方单位 %s 个，手牌 %s 张（模拟里）" % (b["sim_units"], b["sim_hand"]))
        la = ps["last_action"]
        self.kv["act"].config(text="—" if not la else "[%s] %s" % (la["kind"], la["note"]))
        self.kv["rounds"].config(text=(rr or "—") + "    （已打 %d / %s 局，自动下一局：%s）" % (
            c["games_done"], total, "是" if c["auto_next"] else "否"))
        forb = c.get("forbid") or []
        self.kv["opts"].config(text=("只看不动 " if c.get("dry_run") else "") + ("禁用：" + ",".join(forb) if forb else "全开"))
        self.kv["elapsed"].config(text="—" if self._play_t0 is None else "%d s" % (time.time() - self._play_t0))
        if ps["issues"]:
            self.lbl_badge.config(text="值得看一眼 %d" % ps["issues"], bg="#fde2e4", fg="#c0172d")
        else:
            self.lbl_badge.config(text="本段干净", bg="#dcf5e1", fg="#1b8a3a")

    def _refresh_logs(self):
        logs = H.list_logs()
        names = [os.path.basename(p) for p in logs[:60]]
        if list(self.cb_log["values"]) != names:
            self.cb_log["values"] = names
        if names and (self.v_follow.get() or not self.cb_log.get()):
            self.cb_log.set(names[0])

    def _cur_log(self):
        n = self.cb_log.get()
        return os.path.join(H.LOG_DIR, n) if n else None

    def _reload_rows(self, force: bool = False):
        p = self._cur_log()
        if not p:
            return
        try:
            sig = (p, os.path.getsize(p))
        except OSError:
            return
        if not force and sig == self._rows_sig:
            return
        self._rows_sig = sig
        self.rows = H.load_rows(p)
        kinds = sorted({r["kind"] for r in self.rows if r["kind"]})
        self.cb_kind["values"] = ["全部"] + kinds
        self._fill_tree()
        self._show_diag()

    def _show_diag(self):
        """“诊断”行：本局模拟耗时（B1）、最近的缺口、RVA 缓存、最近一次换牌收尾（B4）。"""
        d = H.diagnostics(self.rows)
        parts = []
        if d["sim_mean"] is not None:
            parts.append("模拟耗时 最近 %.2f s / 均值 %.2f s / 最大 %.2f s" % (d["t_sim"], d["sim_mean"], d["sim_max"]))
        if d["gap_names"]:
            gn = d["gap_names"]
            parts.append("缺口 %d：%s%s" % (len(gn), "、".join(gn[:4]), "…" if len(gn) > 4 else ""))
        if d["ui_tail"]:
            parts.append("换牌收尾：%s" % d["ui_tail"])
        parts.append(W.rva_cache_text())
        self.lbl_diag.config(text="诊断：" + "    |    ".join(parts))

    def _fill_tree(self):
        k = self.v_kind.get()
        sel = [r for r in self.rows if k == "全部" or r["kind"] == k]
        self.tree.delete(*self.tree.get_children())
        for r in sel:
            vals = []
            for key, _t, _w in COLS:
                v = r.get(key)
                if key == "ok":
                    v = "" if v is None else ("✓" if v else "✗")
                elif key in ("score",) and isinstance(v, (int, float)):
                    v = "%.2f" % v
                elif key in ("t_decide", "t_sim", "t_exec") and isinstance(v, (int, float)):
                    v = "%.1f" % v
                vals.append("" if v is None else v)
            self.tree.insert("", "end", iid=str(r["i"]), values=vals)
        if self.v_follow.get() and sel:
            self.tree.see(str(sel[-1]["i"]))
        sm = H.summarize(self.rows)
        self.lbl_sum.config(text="共 %d 步，失败 %d，带缺口 %d，回合 %s" %
                            (sm["steps"], sm["failed"], sm["with_gaps"], sm["turns"] or "-"))

    def _show_detail(self, _e=None):
        s = self.tree.selection()
        if not s:
            return
        r = next((x for x in self.rows if str(x["i"]) == s[0]), None)
        if r:
            self.txt_detail.delete("1.0", "end")
            self.txt_detail.insert("end", json.dumps(r["raw"], ensure_ascii=False, indent=1, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description="KARDS Agent 控制面板")
    ap.add_argument("--debug", action="store_true", help="调试：刷新出错时不吞异常")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=760)
    ap.add_argument("--selftest", action="store_true",
                    help="不开窗口：把解析出的路径/解释器/开关写进 kards-data/gui/gui_selftest.txt 就退出")
    args = ap.parse_args(argv)
    if args.selftest:
        os.makedirs(K.STATE_DIR, exist_ok=True)
        info = K.selftest_info()
        with open(os.path.join(K.STATE_DIR, "gui_selftest.txt"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    App(debug=args.debug, size=(args.width, args.height)).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
