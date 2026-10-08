# -*- coding: utf-8 -*-
"""ProcDump 引擎 + dump_now —— 从 crashdump.py 原样搬出（P6 拆文件，零逻辑改动）。
"""
from __future__ import annotations

import _crashdump_base as _m__crashdump_base
globals().update({_k: _v for _k, _v in vars(_m__crashdump_base).items() if not _k.startswith("__")})
import _crashdump_mdmp as _m__crashdump_mdmp
globals().update({_k: _v for _k, _v in vars(_m__crashdump_mdmp).items() if not _k.startswith("__")})


def dump_now(pid: int | None = None, tag: str = "", out_dir: str = DEFAULT_OUT_DIR,
             timeout: float = 900.0, image_name: str = DEFAULT_IMAGE,
             engine: str = "auto", reflect: bool = False) -> dict:
    """对正在跑的游戏立刻落一份完整 dump（不碰调试器，纯只读）。

    ★ 默认走 **ProcDump**（`-ma`，**不加 `-r`**）。ProcDump 不可用/没接受 EULA 时
      自动退回自研 `MiniDumpWriteDump`（engine="native" 可强制）。

    ★★ `-r`（反射/克隆）实测**不能当默认**：它确实能把冻结压到 0 秒，
      但克隆出来的副本**丢掉了大部分私有堆页**（实测：游戏那一刻 1.13GB 私有堆里
      0.83GB 读不出来，cdb 里那些地址是 `????????`）—— 而"看清被访问的对象"正是完整
      dump 的全部意义，所以默认不要它。要零冻结就显式 reflect=True（/--reflect）。

    两条路都**不附加调试器**（不会在游戏身上留下调试会话），所以 dump 里没有异常流
    （没有 .ecxr）：现场看同名 .json 里的线程 RIP 表，cdb 里 `~<tid>s; r; k`。
    """
    if pid is None:
        cands = list_pids(image_name)
        if not cands:
            raise DumpError("没找到 %s（游戏没在跑？）" % image_name)
        cands.sort(key=lambda c: (c["threads"], c["pid"]))
        pid = cands[-1]["pid"]
        if len(cands) > 1:
            log("★ 有 %d 个匹配 %s：挑了线程最多的 pid=%d（其它 %s）"
                % (len(cands), image_name, pid,
                   ", ".join(str(c["pid"]) for c in cands if c["pid"] != pid)))
    info = process_info(pid)
    if not info["alive"]:
        raise DumpError("pid=%d 打不开（不存在 / 已退出 / 权限不够）" % pid)
    if info["wow64"]:
        raise DumpError("pid=%d 是 WOW64(32 位) 进程；本工具的 CONTEXT 布局只支持 x64" % pid)
    _ensure_space(out_dir)
    modules = modules_of(pid)
    path = os.path.join(out_dir, dump_name(pid, tag=tag))
    if info["image"]:
        pe_fingerprint(info["image"])        # 预热（md5 要读 1~2GB），别拖慢 dump
    mem_csv_path = os.path.splitext(path)[0] + ".memory.csv"
    mem = memory_map_csv(pid, mem_csv_path)
    ctx_path = _context_bundle(path)
    engine, why = resolve_engine(engine)
    log("now 用引擎 %s（%s）" % (engine, why))
    if engine == "procdump":
        res = procdump_manual_dump(pid, path, reflect=reflect, timeout=timeout)
        tids = _safe(lambda: [{"tid": t["tid"], "rip": t["rip"]}
                              for t in MiniDump(path).threads], [])
    else:
        tids = thread_rips(pid)
        res = _run_dump(pid, path, None, None, timeout=timeout)
    main = main_module_of(modules, image_name)
    when = time.time()
    payload = {
        "kind": "now",
        "pid": pid,
        "tag": tag,
        "time": when,
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
        "time_local": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when)),
        "image": info["image"],
        "process_started": info["started"],
        "uptime_s": info["uptime_s"],
        "context_txt": ctx_path,
        "memory_csv": mem.get("csv"),
        "memory_summary": {k: v for k, v in mem.items() if k != "csv"},
        "exception": None,
        "note": "now dump 没有异常流（.ecxr 不可用）；用下面 threads 里的 tid 做 "
                "`~<tid>s; r; k`。",
        "module_base": main["base"] if main else None,
        "modules": [{"base": m["base"], "size": m["size"], "name": m["name"]}
                    for m in modules],
        "threads": tids,
        "thread_count": len(tids),
        "self_usage": self_usage(),
        "dump": {"path": res.get("path"), "size": res.get("size"),
                 "seconds": res.get("seconds"), "timed_out": res.get("timed_out"),
                 "ok": res.get("ok"), "error": res.get("error"),
                 "flags": "0x%x" % DUMP_FLAGS, "engine": engine,
                 "reflect": bool(reflect and engine == "procdump"),
                 "wall_seconds": res.get("wall_seconds"),
                 "cmd": res.get("cmd")},
    }
    payload.update(build_sidecars(pid, path, info, modules, image_name))
    out = {"path": res.get("path"), "size": res.get("size") or 0,
           "seconds": round(res.get("seconds") or 0.0, 3),
           "pid": pid, "ok": bool(res.get("ok")),
           "timed_out": bool(res.get("timed_out")),
           "error": res.get("error"),
           "engine": engine, "reflect": bool(reflect and engine == "procdump"),
           "wall_seconds": res.get("wall_seconds"),
           "procdump_text": res.get("text") if engine == "procdump" else None,
           "threads": len(tids), "modules": len(modules),
           "uptime_s": info["uptime_s"]}
    if res.get("ok"):
        out["json"] = _sidecar_json(path, payload)
    else:
        _sidecar_json(path, payload)     # 失败也留一份现场（含 dbghelp 的错误）
        raise DumpError("dump 失败：%s" % res.get("error"))
    return out


# --------------------------------------------------------------------------- procdump
PROCDUMP_DIR = os.path.join(SYSINTERNALS, "procdump64.exe")


def procdump_accepted() -> dict:
    """ProcDump 的 EULA 有没有被接受过（**只读**，绝不替用户接受）。

    ★ 只查注册表（Sysinternals 惯例：HKCU\\Software\\Sysinternals\\ProcDump\\EulaAccepted）。
      **不再去跑 `procdump64.exe -?` 探测** —— 实测那一跑本身就会把这个键写上去
      （等于替用户接受了许可），这是必须避免的副作用。
    """
    out = {"exe": PROCDUMP64, "exists": os.path.exists(PROCDUMP64), "accepted": None,
           "how": None}
    if not out["exists"]:
        return out
    try:
        import winreg                                       # noqa: PLC0415
        for key in (r"Software\Sysinternals\ProcDump",
                    r"Software\Sysinternals\ProcDump64"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    val, _ = winreg.QueryValueEx(k, "EulaAccepted")
                    out.update(accepted=bool(val), how="registry %s" % key)
                    return out
            except OSError:
                continue
    except Exception:                   # noqa: BLE001
        pass
    out.update(accepted=False, how="注册表里没有（=还没接受过）")
    return out


def procdump_command(image: str = DEFAULT_IMAGE, out_dir: str = DEFAULT_OUT_DIR,
                     max_dumps: int = 3, codes=("C0000005",), first_chance: bool = True,
                     termination: bool = False, pid: int | None = None,
                     reflect: bool = False) -> list:
    """要跑的 ProcDump 命令行（也印给用户看：为什么提权、提的是哪条）。

    `-accepteula` 只是"已接受就别再问"，**第一次接受必须由用户手动跑**
    （见 CRASHDUMP.md，本工具不会替你点同意）。

    * pid 给定 → 直接盯那个 pid（`-w` 只在按名字等时才需要）
    * `-r` = 反射/克隆：只把目标冻住建快照那一会儿，写盘在克隆上做。
      实测两件事（都进了 selftest/报告）：
        1) **异常上下文保得住** —— 带 -r 的异常 dump 里 `.ecxr` 照样停在故障指令上
        2) **但私有堆页会丢** —— 游戏的 -r 副本里 1.13GB 私有堆有 0.83GB 读不出来
           （cdb `dq` 是 `????????`），因为 PSS 克隆把私有页挂到快照文件上、dbghelp
           不写它们。
      ⇒ 崩溃 dump 和手动 now **都默认不用 -r**（保真优先）；要零冻结再显式开。
    """
    cmd = [PROCDUMP64, "-accepteula", "-ma"]
    if termination:
        cmd.append("-t")
    if reflect:
        cmd.append("-r")
    cmd += ["-e", "1" if first_chance else "", "-f", ",".join(codes),
            "-n", str(max_dumps)]
    if pid:
        cmd += [str(pid), out_dir]
    else:
        cmd += ["-w", image, out_dir]
    return [c for c in cmd if c != ""]


def _parse_procdump_output(text: str) -> dict:
    """从 ProcDump 的输出里抠出它自己报的落盘路径 / 大小 / 耗时。

    它每行形如（中文系统上也是英文）：
        [22:13:53] Dump 1 initiated: <path>
        [22:13:54] Dump 1 writing: Estimated dump file size is 2725 MB.
        [22:14:03] Dump 1 complete: 2727 MB written in 9.8 seconds
    """
    out = {"path": None, "mb": None, "seconds": None, "dumps": [], "raw": text}
    for line in text.splitlines():
        m = re.search(r"Dump \d+ initiated:\s*(.+?)\s*$", line)
        if m:
            out["path"] = m.group(1)
        m = re.search(r"Dump \d+ complete:\s*([\d.]+) MB written in ([\d.]+) seconds",
                      line)
        if m:
            out["mb"] = float(m.group(1))
            out["seconds"] = float(m.group(2))
            out["dumps"].append({"mb": out["mb"], "seconds": out["seconds"]})
    return out


def procdump_manual_dump(pid: int, out_path: str, reflect: bool = True,
                         timeout: float = 900.0) -> dict:
    """用 ProcDump 给活着的进程打一份完整 dump（手动 `now` 的默认实现）。

    `-r`（反射/克隆）只把目标冻住**建快照那一小会儿**，写盘是在克隆上做的 ——
    对手动打点来说这直接把"游戏被冻多久"从十几秒压到零点几秒（实测见报告）。
    异常触发的 dump 能不能也带 `-r` 要看 `.ecxr` 还在不在，那是另一条路（watch），
    见 selftest 的 D 幕。
    """
    cmd = [PROCDUMP64, "-accepteula", "-ma"]
    if reflect:
        cmd.append("-r")
    cmd += [str(pid), out_path]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    wall = time.perf_counter() - t0
    text = _decode_console((p.stdout or b"") + (p.stderr or b""))
    info = _parse_procdump_output(text)
    path = info["path"] or out_path
    # ★ 别拿返回码当成功判据：实测 ProcDump 在"手动 dump 成功"时也会返回 1
    #   （"Dump count reached."）。判据是"它自己说 Dump N complete" + 文件真的在。
    size = os.path.getsize(path) if os.path.exists(path) else 0
    done = ("complete:" in text) or bool(info["mb"])
    return {"ok": bool(done and size > 0),
            "returncode": p.returncode, "cmd": cmd, "wall_seconds": round(wall, 2),
            "seconds": info["seconds"], "path": path,
            "size": size, "text": text,
            "error": None if (done and size > 0) else
                     "procdump rc=%d，且没有看到 'Dump ... complete'" % p.returncode}


# 优雅收掉游离 ProcDump 的辅助进程（**不是 kill**）：它自己 FreeConsole 后挂到
# ProcDump 的控制台上，再广播 CTRL_BREAK —— ProcDump 收到就按正常路径分离退出。
# 为什么另起进程：FreeConsole 会把**调用者**从自己的控制台摘掉（本工具不能这么干）。
_CTRL_BREAK_HELPER = r'''
import ctypes, sys, time
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
pid = int(sys.argv[1])
CTRL_BREAK_EVENT = 1
k32.FreeConsole()
if not k32.AttachConsole(ctypes.c_uint(pid)):
    print("attach-console-failed err=%d" % ctypes.get_last_error())
    sys.exit(2)
k32.SetConsoleCtrlHandler(None, True)          # 自己别被带走
# 先只打它自己的进程组（ProcDump 是用 CREATE_NEW_PROCESS_GROUP 起的，组号=它自己的 pid）
print("group-send", bool(k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)))
for _ in range(20):
    time.sleep(0.25)
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        print("gone"); sys.exit(0)
    k32.CloseHandle(h)
print("group-send-ineffective; broadcast")
print("broadcast-send", bool(k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)))
for _ in range(80):
    time.sleep(0.25)
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        print("gone"); sys.exit(0)
    k32.CloseHandle(h)
print("still-alive")
sys.exit(3)
'''


def stop_stray_procdump(pid: int, timeout: float = 40.0) -> dict:
    """优雅收掉一个（可能是别人留下的）ProcDump：CTRL_BREAK，**绝不 kill**。

    返回 {"pid", "ok", "still_alive", "text", "rc"}。`AttachConsole` 失败
    （常见：那个 ProcDump 是管理员起的）时会如实说"要先提权"。
    """
    try:
        p = subprocess.run([sys.executable, "-c", _CTRL_BREAK_HELPER, str(pid)],
                           capture_output=True, timeout=timeout)
        rc = p.returncode
        text = _decode_console((p.stdout or b"") + (p.stderr or b"")).strip()
    except subprocess.TimeoutExpired:
        rc, text = "timeout", "辅助进程超时"
    alive = _pid_alive(pid)
    out = {"pid": pid, "rc": rc, "ok": (rc == 0) and not alive,
           "still_alive": alive, "text": text}
    if alive and "attach-console-failed" in text:
        out["hint"] = ("挂不上它的控制台（多半它是管理员起的）：请在**管理员**终端里跑 "
                       "`python tools\\crashdump.py stop-procdump --pid %d`" % pid)
    elif alive:
        out["hint"] = ("它还在跑：请在它所在的控制台按 Ctrl+C（**不要用 kill**）"
                       "，或提权后再试一次 stop-procdump")
    return out


def resolve_engine(engine: str = "auto") -> tuple[str, str]:
    """决定这一份 dump 用谁写：("procdump"|"native", 人话原因)。

    auto：ProcDump 在 + EULA 已接受 → 用它；否则退回自研写入器（并把原因说清楚）。
    显式 procdump 但没接受 EULA → 抛错并给出**用户该手动跑的那条命令**
    （本工具永远不替你接受软件许可）。
    """
    if engine == "native":
        return "native", "显式指定自研写入器"
    probe = procdump_accepted()
    if not probe["exists"]:
        if engine == "procdump":
            raise DumpError("找不到 %s" % PROCDUMP64)
        return "native", "找不到 procdump64，退回自研写入器"
    if probe["accepted"] is False:
        msg = ("ProcDump 的 EULA 还没接受过（第一次必须你手动跑）：\n"
               "    %s -accepteula" % PROCDUMP64)
        if engine == "procdump":
            raise DumpError(msg)
        return "native", "ProcDump EULA 未接受，退回自研写入器（接受后自动改用它）"
    ver = "?"
    try:
        ver = subprocess.run([PROCDUMP64, "-accepteula", "-?"], capture_output=True,
                             timeout=30).stdout.decode("mbcs", "replace").split("\n")[1]
        ver = ver.strip() or "?"
    except Exception:                   # noqa: BLE001
        pass
    return "procdump", "微软 ProcDump（%s）" % ver[:60]


class ProcDumpWatcher:
    """用 ProcDump 抓、我们补元数据 —— 和 Watcher 同一个对外接口（start/stop/status）。

    为什么留这条路（而不是只留自研调试器循环）
    ----------------------------------------
    ProcDump 是微软自己维护的抓取器，落盘实现比我们稳。分工：
      * ProcDump：`-ma -e 1 -f C0000005 -n N -w <image> <dir>`（完整内存 + first-chance
        访问违例 + 最多 N 份 + 等进程出现）
      * 我们：**落完 dump 之后**补 .json（异常码/故障 RVA/线程 RIP/运行时长/exe 指纹）、
        .context.txt、.memory.csv，并按 **RVA 白名单事后判定**哪份是我们要的
    ★ 它做不到的正是白名单本身（-f 只能按异常码过滤）—— 所以第一次落盘前无法省下
      那些"无害 first-chance AV"的大 dump。游戏里这类 AV 多不多要先实测，
      见 CRASHDUMP.md 的对比结论。
    """

    def __init__(self, pids=None, rva_whitelist=DEFAULT_RVAS, out_dir: str = DEFAULT_OUT_DIR,
                 max_dumps: int = 3, image_name: str = DEFAULT_IMAGE,
                 min_free_gb: float = 10.0, status_path: str | None = None,
                 codes=("C0000005",), first_chance: bool = True, termination: bool = False,
                 poll_interval: float = 1.0, keep_nonmatching: bool = True,
                 reflect: bool = False, dump_timeout: float = 900.0,
                 verbose: bool = True):
        self.pids = list(pids) if pids else None
        self.rva_whitelist = tuple(rva_whitelist) if rva_whitelist else ()
        self.out_dir = out_dir
        self.max_dumps = int(max_dumps)
        self.image_name = image_name
        self.min_free_bytes = int(min_free_gb * 2 ** 30)
        self.status_path = status_path or default_status_path(out_dir)
        self.codes = tuple(codes)
        self.first_chance = first_chance
        self.termination = termination
        self.poll_interval = poll_interval
        self.keep_nonmatching = keep_nonmatching
        # 崩溃 dump 默认不用 -r：定格那一瞬的保真优先（冻结时长在崩溃场景无所谓）。
        # 想省冻结时间就传 reflect=True —— 实测 .ecxr 在 -r 下依然可用（selftest D 幕）。
        self.reflect = reflect
        self.dump_timeout = dump_timeout
        self.verbose = verbose

        self._proc = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = "waiting"
        self._state_since = time.time()
        self._error = None
        self._error_time = None
        self._pid = None
        self._dumps: list[dict] = []
        self._seen: set = set()
        self._last_publish = 0.0
        self._stats = {"dumps": 0, "rejected": 0, "rejected_bytes": 0}
        # ★ bug#3：启动那一刻目录里**已经存在**的 .dmp 不是我们抓的（可能是别处放的
        #   `now` dump、上一轮的 dump），绝不能当成"刚抓到崩溃"收编。
        self._started_at: float | None = None
        self._pre_existing: set = set()
        # ★ bug#2：ProcDump 立刻退出（附加上不去）时要退避 + 报错，不能每 2 秒无限重试。
        self._spawn_at = 0.0
        self._fail_streak = 0
        self._skip_reason = ""
        self._log_path = os.path.join(out_dir, "procdump.log")
        # ★ 这里先按"名字"形式建一份**预览**，`_spawn()` 里再按实际目标（可能是明确
        #   pid）覆盖它。这样 `w.command` 从 `start()` 返回那一刻起就**不是 None** ——
        #   CLI 曾经在 `" ".join(w.command)` 上撞过"后台线程还没走到 _spawn"的竞态。
        self.command = procdump_command(image_name, out_dir, max_dumps, codes,
                                        first_chance, termination, reflect=reflect)

    # -- 对外 ---------------------------------------------------------------
    def start(self) -> "ProcDumpWatcher":
        if self._thread and self._thread.is_alive():
            return self
        probe = procdump_accepted()
        if not probe["exists"]:
            self._set_error("找不到 %s" % PROCDUMP64)
            return self
        if probe["accepted"] is False:
            self._set_error(
                "ProcDump 的 EULA 还没接受过。**第一次必须你手动跑**（本工具不替你接受）：\n"
                "    %s -accepteula\n"
                "接受之后再启动 watch（也可以直接用 --engine native 走自研调试器循环）。"
                % PROCDUMP64)
            return self
        self._stop.clear()
        # ★ bug#3：记下"启动这一刻"和目录里已有的 .dmp 清单，_harvest 只认之后新落的
        self._started_at = time.time()
        try:
            self._pre_existing = {n for n in os.listdir(self.out_dir)
                                  if n.lower().endswith(".dmp")}
        except OSError:
            self._pre_existing = set()
        if self._pre_existing:
            self._log("目录里已有 %d 份 .dmp（%s%s），不当成新抓的 —— 只认启动之后落的"
                      % (len(self._pre_existing),
                         ", ".join(sorted(self._pre_existing)[:3]),
                         " …" if len(self._pre_existing) > 3 else ""))
        self._thread = threading.Thread(target=self._run, name="crashdump-procdump",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 60.0) -> bool:
        """**正常退出** ProcDump（Ctrl+Break/SIGINT），绝不 TerminateProcess ——
        杀它会让被调试的游戏一起崩（这个项目踩过同类坑）。"""
        self._stop.set()
        # ★ 给"刚 start() 就 stop()"那个窗口一点宽限：线程可能正卡在 _spawn 里，
        #   让它要么看到 _stop 直接返回、要么把 _proc 建好，好让我们能正常停掉它。
        grace = time.time() + 2.0
        while (self._proc is None and self._thread and self._thread.is_alive()
               and time.time() < grace):
            time.sleep(0.02)
        p = self._proc
        child_alive = False
        if p is not None and p.poll() is None:
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception as exc:                        # noqa: BLE001
                self._log("给 ProcDump 发 CTRL_BREAK 失败：%s" % exc)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
            child_alive = p.poll() is None
            if child_alive:
                # ★ 不许谎报"停好了"：Ctrl+Break 有可能送不到（控制台/进程组差异），
                #   那就如实报错 + 告诉人怎么手工收 —— **绝不用 kill**。
                self._set_error(
                    "ProcDump(pid=%s) 没在 %.0fs 内退出（**没有杀它**）：它可能还在抓。"
                    "请在它所在的控制台按 Ctrl+C，或用 `python tools\\crashdump.py "
                    "stop-procdump --pid %s` 优雅收掉" % (p.pid, timeout, p.pid))
        th = self._thread
        if th:
            th.join(10.0)
        self._publish(force=True)
        return (not child_alive) and not (th and th.is_alive())

    def status(self) -> dict:
        with self._lock:
            st = {"state": self._state, "pid": self._pid, "since": self._state_since,
                  "dumps": [dict(d) for d in self._dumps], "error": self._error,
                  "error_time": self._error_time, "updated": time.time(),
                  "watcher_pid": os.getpid(), "engine": "procdump",
                  "image": self.image_name, "out_dir": self.out_dir,
                  "max_dumps": self.max_dumps,
                  "rva_whitelist": [("0x%x" % e) if isinstance(e, int) else str(e)
                                    for e in self.rva_whitelist],
                  "procdump": {"exe": PROCDUMP64, "cmdline": self.command,
                               "cmdline_preview": (
                                   self.command or
                                   procdump_command(self.image_name, self.out_dir,
                                                    self.max_dumps, self.codes,
                                                    self.first_chance, self.termination,
                                                    reflect=self.reflect)),
                               "alive": bool(self._proc and self._proc.poll() is None),
                               "reflect": self.reflect,
                               "pid": self._proc.pid if self._proc else None},
                  "stats": dict(self._stats), "self_usage": self_usage(),
                  "elevated": bool(elevation_state()["elevated"]),
                  "watching": bool(self._thread and self._thread.is_alive())}
            if self._dumps:
                st["last_dump"] = dict(self._dumps[-1])
            return st

    # -- 内部 ---------------------------------------------------------------
    def _log(self, msg: str) -> None:
        log(msg, quiet=not self.verbose)

    def _set_error(self, msg: str, publish: bool = True) -> None:
        with self._lock:
            self._error = msg
            self._error_time = time.time()
            self._state = "error"
            self._state_since = time.time()
        self._log("★ %s" % msg)
        if publish:
            self._publish(force=True)

    def _publish(self, force: bool = False, min_gap: float = 0.0) -> None:
        now = time.time()
        if not force and (now - self._last_publish) < min_gap:
            return
        self._last_publish = now
        try:
            atomic_json(self.status_path, self.status())
        except Exception as exc:                            # noqa: BLE001
            self._log("★ status.json 写失败：%s" % exc)

    def _run(self) -> None:
        """ProcDump 子进程的监督循环：**它退出了就按新实例再挂一次**。

        为什么要重挂：`-w <名字>` 只等"下一个进程出现"，那一局跑完/崩完 ProcDump 自己
        就退了；游戏重启后要还能自动挂上（需求原文）—— 所以这里是循环 spawn，不是一次。
        """
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            _ensure_space(self.out_dir, self.min_free_bytes)
            self._log("（ProcDump 的 EULA 你已接受过；本工具不会替你接受）")
            while not self._stop.is_set():
                if not self._spawn():
                    if self._stop.is_set():
                        break
                    self._fail_streak += 1
                    reason = self._skip_reason or "ProcDump 没能启动"
                    backoff = min(60.0, 2.0 * self._fail_streak)
                    with self._lock:
                        self._error = reason
                        self._error_time = time.time()
                        self._state = "error"
                        self._state_since = time.time()
                    self._log("★ %s（第 %d 次，%.0fs 后重试）"
                              % (reason, self._fail_streak, backoff))
                    self._publish(force=True)
                    self._stop.wait(backoff)
                    continue
                while not self._stop.is_set() and self._proc.poll() is None:
                    pid = self._find_target_pid()
                    if pid:
                        self._pid = pid
                    self._harvest()
                    free = free_bytes(self.out_dir)
                    if free < self.min_free_bytes:
                        self._set_error("磁盘只剩 %.1f GB（阈值 %.1f GB）：别再写了"
                                        % (free / 2**30, self.min_free_bytes / 2**30),
                                        publish=False)
                    self._publish(min_gap=10.0)
                    self._stop.wait(self.poll_interval)
                if self._stop.is_set():
                    break
                # ★ bug#2：ProcDump 立刻退出 = 附加上不去（最常见：目标已被别人调试）。
                #   要退避 + 把原因写进 status.json，不能每 2 秒无限重试。
                rc = getattr(self._proc, "returncode", None)
                life = time.time() - self._spawn_at
                tail = self._log_tail()
                self._fail_streak = self._fail_streak + 1 if life < 5.0 else 0
                hint = ""
                if "already being debugged" in " ".join(tail).lower():
                    hint = ("目标已被别的调试器/ProcDump 占着 —— 先停掉那一个；"
                            "两个调试器不能同时挂同一个进程")
                self._log("ProcDump 退出 rc=%s（活了 %.1fs，连续失败 %d 次）%s"
                          % (rc, life, self._fail_streak,
                             ("　" + hint) if hint else ""))
                if tail:
                    self._log("    它最后说：\n      " + "\n      ".join(tail[-6:]))
                if self._fail_streak >= 2:
                    self._set_error("ProcDump 连续 %d 次立刻退出（rc=%s）%s%s"
                                    % (self._fail_streak, rc,
                                       ("：" + hint) if hint else "",
                                       ("；最后输出：" + " / ".join(tail[-3:]))
                                       if tail else ""), publish=False)
                else:
                    with self._lock:
                        self._state = "waiting"
                        self._state_since = time.time()
                self._publish(force=True)
                backoff = min(60.0, 2.0 * max(1, self._fail_streak))
                self._log("    %.0fs 后按新实例再挂一次" % backoff)
                self._stop.wait(backoff)
        except DumpError as exc:
            self._set_error(str(exc))
        except Exception as exc:                            # noqa: BLE001
            self._set_error("ProcDump 监督线程异常：%s: %s" % (type(exc).__name__, exc))
        finally:
            self._publish(force=True)

    def _spawn(self) -> bool:
        """起一个 ProcDump。给了明确 pid 就用 pid 形式（也便于自测），否则 `-w <名字>`。

        → True = 起来了；False = 故意没起（stop 竞态 / 目标已被别人调试），调用方要退避。
        """
        self._skip_reason = ""
        if self._stop.is_set():
            # ★ 竞态补丁：`stop()` 可能正好落在 `_run` 的 while 检查与这里之间。
            #   少了这一句，"已经要求停止"之后照样会 Popen 出一个**游离的 ProcDump**
            #   （`-w kards-Win64-Shipping.exe` 会在游戏下次启动时挂上去）。
            return False
        pid = None
        if self.pids:
            live = [p for p in self.pids if _pid_alive(p)]
            pid = live[0] if len(live) == 1 else None
        else:
            # ★ bug#2 的预检：要盯的进程**现在就在跑、而且已经有人在调试它**时，
            #   ProcDump 只会立刻退出（实测 rc=0xFFFFFFFE + "The process is already
            #   being debugged."）。那就别反复起它了 —— 记清楚原因 + 退避重试。
            cur = self._find_target_pid()
            if cur and is_debugged(cur):
                self._skip_reason = (
                    "目标 pid=%d 已经在被调试（另一个 ProcDump/调试器占着）—— 两个调试器"
                    "不可能同时挂同一个进程，先停掉那一个再试" % cur)
                return False
        self.command = procdump_command(self.image_name, self.out_dir, self.max_dumps,
                                        self.codes, self.first_chance, self.termination,
                                        pid=pid, reflect=self.reflect)
        self._log("启动 ProcDump：%s" % " ".join(self.command))
        flags = 0x00000200          # CREATE_NEW_PROCESS_GROUP：才能给它发 Ctrl+Break
        # ★ 它的 stdout 落到**文件**（不是没人读的 PIPE）：出问题时我们才看得到原因。
        #   实测过的例子：`Error debugging process: The process is already being
        #   debugged.` —— 第一版被 PIPE 吞掉，只能手工重跑同款命令才知道。
        self._spawn_at = time.time()
        try:
            logf = open(self._log_path, "ab", buffering=0)
            logf.write(("\n===== %s  %s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           " ".join(self.command))).encode("utf-8"))
            self._proc = subprocess.Popen(self.command, stdout=logf,
                                          stderr=subprocess.STDOUT,
                                          creationflags=flags)
            logf.close()            # 子进程有自己的句柄副本，我们这份可以关
        except OSError as exc:
            self._skip_reason = "起不了 ProcDump：%s" % exc
            return False
        with self._lock:
            self._state = "attached"
            self._state_since = time.time()
        self._publish(force=True)
        return True

    def _log_tail(self, n: int = 12) -> list:
        """ProcDump 自己那份输出（`<out_dir>/procdump.log`）的最后几行。"""
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                text = _decode_console(f.read())
        except OSError:
            return []
        return [ln.rstrip() for ln in text.splitlines() if ln.strip()][-n:]

    def _tail_output(self) -> str:
        try:
            if self._proc and self._proc.stdout:
                return "（不读它的管道，避免阻塞）"
        except Exception:                   # noqa: BLE001
            pass
        return "?"

    def _find_target_pid(self):
        if self.pids:
            for pid in self.pids:
                if _pid_alive(pid):
                    return pid
            return None
        cands = list_pids(self.image_name)
        if not cands:
            return None
        cands.sort(key=lambda c: (c["threads"], c["pid"]))
        return cands[-1]["pid"]

    def _harvest(self) -> None:
        """扫描输出目录里新出现的 .dmp，补元数据 + 事后按 RVA 白名单判定。"""
        try:
            names = os.listdir(self.out_dir)
        except OSError:
            return
        for name in sorted(names):
            if not name.lower().endswith(".dmp"):
                continue
            if name in self._pre_existing:
                continue                        # ★ bug#3：启动前就在目录里的，不收编
            path = os.path.join(self.out_dir, name)
            if path in self._seen:
                continue
            try:
                # ★ bug#3 的第二道闸：按 mtime 再确认一次"是启动之后落的"（1 秒容差）
                if os.path.getmtime(path) < (self._started_at or 0.0) - 1.0:
                    continue
            except OSError:
                continue
            if not self._stable(path):
                continue                        # 还在写
            self._seen.add(path)
            self._finish_dump(path)

    @staticmethod
    def _stable(path: str, quiet_for: float = 2.0) -> bool:
        """ProcDump 还在往这个文件里写吗（看大小在 quiet_for 秒内变不变）。"""
        try:
            s1 = os.path.getsize(path)
        except OSError:
            return False
        time.sleep(quiet_for)
        try:
            s2 = os.path.getsize(path)
        except OSError:
            return False
        return s1 == s2 and s1 > 0

    def _finish_dump(self, path: str) -> None:
        with self._lock:
            self._state = "dumping"
            self._state_since = time.time()
        self._publish(force=True)
        try:
            md = MiniDump(path)
        except Exception as exc:                            # noqa: BLE001
            self._set_error("读不了 ProcDump 落下的 %s：%s" % (path, exc))
            return
        exc_info = md.exception or {}
        rip = exc_info.get("rip")
        # ★ 故障模块按"哪个模块包含故障 RIP"定：换了目标（或自测靶子）时按 image 名
        #   去找会退化成 modules[0]，算出来的 RVA 就成了天文数字（踩过）。
        main = (md.module_of(rip) if rip else None) or \
            main_module_of(md.modules, self.image_name) or \
            (md.modules[0] if md.modules else None)
        base = main["base"] if main else None
        rva = (rip - base) if (rip and base) else None
        hit, _rva, entry = (match_whitelist(rip, md.modules, self.image_name,
                                            self.rva_whitelist)
                            if rip else (False, None, None))
        pid = self._pid
        info = process_info(pid) if pid else {"image": None, "uptime_s": None}
        threads = [{"tid": t["tid"], "rip": t["rip"]} for t in md.threads]
        when = time.time()
        payload = {
            "kind": "procdump", "engine": "procdump", "pid": pid,
            "tid": exc_info.get("tid"),
            "time": when,
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
            "time_local": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when)),
            "process_started": info.get("started"), "uptime_s": info.get("uptime_s"),
            "exception": {"code": "0x%08x" % exc_info["code"] if exc_info.get("code")
                          else None, "code_int": exc_info.get("code"),
                          "address": exc_info.get("address"),
                          "access_address": (exc_info.get("information") or [None, None])[1]
                          if exc_info.get("information") else None,
                          "tid": exc_info.get("tid")},
            "fault_rip": rip, "fault_rva": rva,
            "fault_module": main["name"] if main else None,
            "module_base": base,
            "whitelisted": bool(hit), "whitelist_entry": entry,
            "whitelist_source": "事后判定（ProcDump 不能按 RVA 过滤）",
            "modules": [{"base": m["base"], "size": m["size"], "name": m["name"]}
                        for m in md.modules],
            "threads": threads, "thread_count": len(threads),
            "memory": md.memory_stats(),
            "dump": {"path": path, "size": os.path.getsize(path),
                     "flags": "procdump -ma"},
            "note": ("ProcDump 抓的完整 dump；异常流由它自己的异常信息给出，"
                     "RVA 白名单是事后判的。"),
            "context_txt": None,
        }
        try:
            payload["context_txt"] = _context_bundle(path)
        except Exception:                                   # noqa: BLE001
            pass
        if pid and _pid_alive(pid):
            csv = os.path.splitext(path)[0] + ".memory.csv"
            try:
                payload["memory_csv"] = memory_map_csv(pid, csv).get("csv")
            except Exception:                               # noqa: BLE001
                pass
        payload["exe"] = build_sidecars(pid or 0, path, info, md.modules,
                                        self.image_name).get("exe")
        json_path = _sidecar_json(path, payload)
        record = {"path": path, "size": payload["dump"]["size"],
                  "exc_code": payload["exception"]["code"], "fault_rva": rva,
                  "fault_module": payload["fault_module"], "tid": exc_info.get("tid"),
                  "time": when, "json": json_path, "engine": "procdump",
                  "whitelisted": bool(hit),
                  "reason": ("白名单命中（%s）" % entry) if hit else
                            "不在白名单（ProcDump 先落了，事后判定）"}
        if not hit and not self.keep_nonmatching:
            try:
                os.remove(path)                             # 只删**我们自己目录里**
                record["pruned"] = True                     # ProcDump 刚落的那一份
                self._stats["rejected"] += 1
                self._stats["rejected_bytes"] += record["size"]
            except OSError:
                pass
        with self._lock:
            self._dumps.append(record)
            self._stats["dumps"] += 1
            self._state = "dumped"
            self._state_since = time.time()
        self._log("%s %s  %.1f MB  exc=%s  RVA=%s  %s"
                  % ("✔" if hit else "（非白名单）", os.path.basename(path),
                     record["size"] / 1e6, record["exc_code"],
                     ("%s+0x%x" % (record["fault_module"], rva)) if rva else None,
                     record["reason"]))
        self._publish(force=True)
