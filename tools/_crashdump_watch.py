# -*- coding: utf-8 -*-
"""Watcher（Debug API 常驻监听）—— 从 crashdump.py 原样搬出（P6 拆文件，零逻辑改动）。
"""
from __future__ import annotations

import _crashdump_base as _m__crashdump_base
globals().update({_k: _v for _k, _v in vars(_m__crashdump_base).items() if not _k.startswith("__")})
import _crashdump_mdmp as _m__crashdump_mdmp
globals().update({_k: _v for _k, _v in vars(_m__crashdump_mdmp).items() if not _k.startswith("__")})


# --------------------------------------------------------------------------- watch
class _Target:
    """一个被我们旁观着的进程。"""

    __slots__ = ("pid", "image", "started", "uptime_s", "base", "dumps",
                 "initial_bp_seen", "exited", "attach_time")

    def __init__(self, pid: int, info: dict, base: int | None):
        self.pid = pid
        self.image = info.get("image")
        self.started = info.get("started")
        self.uptime_s = info.get("uptime_s")
        self.base = base
        self.dumps: list[dict] = []
        self.initial_bp_seen = False
        self.exited = False
        self.attach_time = time.time()


class Watcher:
    """常驻调试器循环。**一个线程干完所有 Debug API**（Windows 要求同一个线程收/回事件）。

    生命周期：
        w = Watcher(...); w.start()          # 立刻返回，循环在后台线程
        w.status()                            # 随时读（dict 深拷贝，可 JSON）
        w.stop()                              # 排空事件 → DebugActiveProcessStop → join
    """

    def __init__(self, pids=None, rva_whitelist=DEFAULT_RVAS, out_dir: str = DEFAULT_OUT_DIR,
                 max_dumps: int = 3, image_name: str = DEFAULT_IMAGE,
                 min_free_gb: float = 10.0, dump_timeout: float = 300.0,
                 poll_interval: float = 1.0, status_path: str | None = None,
                 breakpoint_continue: str = "initial", max_targets: int = 4,
                 verbose: bool = True):
        self.pids = list(pids) if pids else None
        self.rva_whitelist = tuple(rva_whitelist) if rva_whitelist else ()
        self.out_dir = out_dir
        self.max_dumps = int(max_dumps)
        self.image_name = image_name
        self.min_free_bytes = int(min_free_gb * 2 ** 30)
        self.dump_timeout = dump_timeout
        self.poll_interval = poll_interval
        self.status_path = status_path or default_status_path(out_dir)
        self.breakpoint_continue = breakpoint_continue
        self.max_targets = max_targets
        self.verbose = verbose

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._targets: dict[int, _Target] = {}
        self._retry_after: dict[int, float] = {}
        self._recent: dict[tuple, float] = {}
        self._exc_counts: dict[str, int] = {}
        self._events: dict[str, int] = {}
        self._stats = {"events": 0, "handle_max_ms": 0.0, "handle_total_ms": 0.0,
                       "frozen_max_ms": 0.0, "dumps": 0, "refused": 0,
                       "exception_max_ms": 0.0}
        self._state = "waiting"
        self._state_since = time.time()
        self._error = None
        self._error_time = None
        self._disk_block_until = 0.0
        self._next_scan = 0.0
        self._pid = None
        self._dumps: list[dict] = []
        self._last_publish = 0.0

    # ---------------------------------------------------------------- 对外
    def start(self) -> "Watcher":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="crashdump-watch",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 60.0) -> bool:
        """干净分离。返回是否真的停下来了（dump 正在写时会等它写完）。"""
        self._stop.set()
        th = self._thread
        if not th:
            return True
        th.join(timeout)
        alive = th.is_alive()
        if alive:
            self._set_error("stop() 超时：调试线程还在写 dump（写完会自己分离）")
        else:
            self._publish(force=True)
        return not alive

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        with self._lock:
            st = {
                "state": self._state,
                "pid": self._pid,
                "since": self._state_since,
                "dumps": [dict(d) for d in self._dumps],
                "error": self._error,
                "error_time": self._error_time,
                "updated": time.time(),
                "watcher_pid": os.getpid(),
                "image": self.image_name,
                "out_dir": self.out_dir,
                "max_dumps": self.max_dumps,
                "rva_whitelist": [("0x%x" % e) if isinstance(e, int) else str(e)
                                  for e in self.rva_whitelist],
                "targets": [{"pid": t.pid, "image": t.image, "uptime_s": t.uptime_s,
                             "dumps": len(t.dumps), "attached_at": t.attach_time}
                            for t in self._targets.values()],
                "events": dict(self._events),
                "exc_counts": dict(self._exc_counts),
                "stats": dict(self._stats),
                "elevated": bool(elevation_state()["elevated"]),
                "elevation": elevation_state(),
                "self_usage": self_usage(),
                "watching": bool(self._thread and self._thread.is_alive()),
            }
            if self._dumps:
                st["last_dump"] = dict(self._dumps[-1])
            return st

    # ---------------------------------------------------------------- 状态写盘
    def _publish(self, force: bool = False, min_gap: float = 0.0) -> None:
        now = time.time()
        if not force and (now - self._last_publish) < min_gap:
            return
        self._last_publish = now
        try:
            atomic_json(self.status_path, self.status())
        except Exception as exc:        # noqa: BLE001  —— 状态文件写不了不能拖垮监视
            self._log("★ status.json 写失败：%s" % exc)

    def _log(self, msg: str) -> None:
        log(msg, quiet=not self.verbose)

    def _set_state(self, state: str, pid: int | None = None, error: str | None = None,
                   publish: bool = True) -> None:
        with self._lock:
            changed = (state != self._state or pid != self._pid or error != self._error)
            self._state = state
            if pid is not None:
                self._pid = pid
            self._error = error
            if error:
                self._error_time = time.time()
            if changed:
                self._state_since = time.time()
        if publish and changed:
            self._publish(force=True)

    def _set_error(self, msg: str, pid: int | None = None,
                   publish: bool = True) -> None:
        if publish:
            self._log("★ %s" % msg)
        self._set_state("error", pid=pid, error=msg, publish=publish)

    # ---------------------------------------------------------------- 主循环
    def _run(self) -> None:
        """整个监视线程的入口。**任何异常都不许带着调试会话跑掉**：
        finally 里一定会把游戏放行（排空事件 + DebugActiveProcessStop）。"""
        try:
            # ★ 先立规矩再 attach：默认"调试器线程一退出就杀掉被调试进程"。
            #   这一条必须在 DebugActiveProcess 之前或紧随其后（见文件头说明）。
            k32.DebugSetProcessKillOnExit(False)
            got_priv = enable_debug_privilege()
            self._log("watch 启动：image=%s out=%s max_dumps=%d 白名单=%s "
                      "SeDebugPrivilege=%s"
                      % (self.image_name, self.out_dir, self.max_dumps,
                         [("0x%x" % e) if isinstance(e, int) else str(e)
                          for e in self.rva_whitelist] or "(空：只落 second-chance)",
                         "有" if got_priv else "无"))
            if not got_priv:
                self._log("  （当前非管理员。同用户进程仍可附加；attach 被拒时用：\n"
                          "    %s）" % elevated_hint("watch"))
            try:
                os.makedirs(self.out_dir, exist_ok=True)
            except OSError as exc:
                self._set_error("建不了输出目录 %s：%s" % (self.out_dir, exc))
            self._publish(force=True)
            while not self._stop.is_set():
                self._scan_targets()
                if not self._targets:
                    self._set_state("waiting", pid=None)
                    self._stop.wait(self.poll_interval)
                    continue
                try:
                    self._pump()
                except Exception as exc:                        # noqa: BLE001
                    # 兜底：事件泵自己炸了也不能把游戏留在挂起态（finally 里还会 detach）。
                    self._selfprotect("事件泵内部异常：%s: %s"
                                      % (type(exc).__name__, exc))
        except Exception as exc:                                # noqa: BLE001
            self._set_error("监视线程异常退出：%s: %s" % (type(exc).__name__, exc))
        finally:
            self._detach_all()
            self._publish(force=True)

    def _scan_targets(self) -> None:
        # ★ 已经挂着目标时别每个事件循环都全量枚举进程（CreateToolhelp32Snapshot +
        #   几百次 Process32Next，5 次/秒 = 白烧 8% 的 CPU —— 实测过）。
        #   有目标以后 2 秒扫一次就够（新进程最多迟 2 秒被发现）。
        now = time.time()
        if self._targets and now < self._next_scan:
            return
        self._next_scan = now + 2.0
        if self.pids:
            wanted = list(self.pids)
        else:
            wanted = [c["pid"] for c in list_pids(self.image_name)]
        for pid in wanted:
            if pid == os.getpid() or pid in self._targets:
                continue
            if len(self._targets) >= self.max_targets:
                break
            if now < self._retry_after.get(pid, 0.0):
                continue
            self._attach(pid)

    def _attach(self, pid: int) -> None:
        info = process_info(pid)
        if info["wow64"]:
            self._retry_after[pid] = time.time() + 60.0
            self._set_error("pid=%d 是 WOW64(32 位)，跳过（CONTEXT 只按 x64 解）" % pid)
            return
        if not info["alive"]:
            self._retry_after[pid] = time.time() + 2.0
            return
        if not k32.DebugActiveProcess(pid):
            err = ctypes.get_last_error()
            self._retry_after[pid] = time.time() + 10.0
            if err == ERROR_ACCESS_DENIED:
                self._set_error(
                    "DebugActiveProcess(pid=%d) 被拒（err=5）：权限不够（或该进程已被别的"
                    "调试器占用）。以管理员身份重开：\n    %s" % (pid, elevated_hint("watch")),
                    pid=pid)
            else:
                self._set_error("DebugActiveProcess(pid=%d) 失败 err=%d" % (pid, err),
                                pid=pid)
            return
        # 附上了：立刻关掉"退出即杀"（DebugActiveProcess 与它之间只有几十微秒）。
        k32.DebugSetProcessKillOnExit(False)
        modules = modules_of(pid)
        main = main_module_of(modules, self.image_name)
        t = _Target(pid, info, main["base"] if main else None)
        with self._lock:
            self._targets[pid] = t
        # 预热 exe 指纹（md5 要读 1~2GB）：绝不能等到"游戏冻住"的窗口里再做。
        if info.get("image"):
            pe_fingerprint(info["image"])
        self._log("已附加 pid=%d  基址=%s  运行 %.0fs  （%s）"
                  % (pid, _hex(t.base) if t.base else "?", t.uptime_s or 0,
                     os.path.basename(t.image or self.image_name)))
        self._set_state("attached", pid=pid, error=None)

    def _detach(self, pid: int, reason: str) -> None:
        """排空事件 → 确认没被挂起 → DebugActiveProcessStop。

        顺序很要紧：只有**不在调试事件里**（进程正在跑）的时候才能停调试；
        停在事件里 detach 会把游戏留在挂起态（需求硬约束 #2）。
        """
        t = self._targets.get(pid)
        if t is None:
            return
        if not t.exited:
            self._drain_events(pid, budget=0.5)
            if not k32.DebugActiveProcessStop(pid):
                err = ctypes.get_last_error()
                if err not in (ERROR_INVALID_PARAMETER, ERROR_ACCESS_DENIED):
                    self._log("DebugActiveProcessStop(pid=%d) err=%d" % (pid, err))
        with self._lock:
            self._targets.pop(pid, None)
            if self.pids and pid in self.pids:
                self.pids.remove(pid)       # 显式 pid 列表：进程没了就别再试同一个号
        self._log("已分离 pid=%d（%s）" % (pid, reason))
        if not self._targets:
            self._set_state("waiting", pid=None)

    def _detach_all(self) -> None:
        for pid in list(self._targets):
            self._detach(pid, "watch 结束")

    def _drain_events(self, pid: int, budget: float = 0.5) -> None:
        """把已排队但没取的事件取出来继续掉 —— 事件排队时进程是冻着的，
        丢下不管 = 分离后游戏卡在挂起态。"""
        ev = DEBUG_EVENT()
        deadline = time.time() + budget
        while time.time() < deadline and not self._stop.is_set():
            if not k32.WaitForDebugEvent(ctypes.byref(ev), 0):
                err = ctypes.get_last_error()
                if err in (121, 0x102):
                    return              # 队列空了
                return
            status = DBG_CONTINUE
            if ev.dwDebugEventCode == EVENT_EXCEPTION:
                status = DBG_EXCEPTION_NOT_HANDLED
            if ev.dwProcessId == pid and ev.dwDebugEventCode == EVENT_EXIT_PROCESS:
                self._targets[pid].exited = True
            k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)

    # ---------------------------------------------------------------- 事件泵
    def _pump(self) -> None:
        """收一个事件 → 处理 → **保证放行** → 再做收尾。

        这里的 try/except 是需求 #5 的落地：任何一步自己出错，都必须先把
        ContinueDebugEvent 发出去（游戏原样继续），然后才记错误 / 必要时分离重来。
        唯一"不能继续"的情况是 ContinueDebugEvent 本身失败 —— 那说明调试会话已经坏了，
        此时立刻分离（分离 = 彻底放行）。
        """
        ev = DEBUG_EVENT()
        if not k32.WaitForDebugEvent(ctypes.byref(ev), 200):
            err = ctypes.get_last_error()
            if err in (121, 0x102):         # 超时：正常（让我们能看 stop 标志）
                self._publish(min_gap=30.0)  # 心跳：让轮询方知道我们还活着
                return
            # 其它错误基本都是调试会话没了
            self._selfprotect("WaitForDebugEvent err=%d（调试会话没了），清空目标重来"
                              % err)
            return

        t0 = time.perf_counter()
        code = ev.dwDebugEventCode
        pid = ev.dwProcessId
        tid = ev.dwThreadId
        with self._lock:
            self._stats["events"] += 1
            self._events[DEBUG_EVENT_CODES.get(code, str(code))] = \
                self._events.get(DEBUG_EVENT_CODES.get(code, str(code)), 0) + 1
        status = DBG_CONTINUE
        post = None
        if code == EVENT_EXCEPTION:
            try:
                status, post = self._on_exception(ev)
            except Exception as exc:                            # noqa: BLE001
                self._log("★ 处理异常事件时自己出错（%s: %s）—— 先把游戏放行"
                          % (type(exc).__name__, exc))
                status, post = DBG_EXCEPTION_NOT_HANDLED, None
        elif code == EVENT_EXIT_PROCESS:
            t = self._targets.get(pid)
            if t:
                t.exited = True
                exit_code = struct.unpack_from("<I", ev._union, 0)[0]
                self._log("目标退出 pid=%d exit=%#x（本进程共落 %d 份 dump）"
                          % (pid, exit_code, len(t.dumps)))
        elif code == EVENT_CREATE_PROCESS:
            t = self._targets.get(pid)
            if t and not t.initial_bp_seen:
                pass                        # 等紧跟其后的初始断点
        dt = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._stats["handle_total_ms"] += dt
            if dt > self._stats["handle_max_ms"]:
                self._stats["handle_max_ms"] = dt
        if not k32.ContinueDebugEvent(pid, tid, status):
            err = ctypes.get_last_error()
            self._selfprotect("ContinueDebugEvent(pid=%d tid=%d) 失败 err=%d"
                              "（会话已坏，先分离放行）" % (pid, tid, err), pid=pid)
            return
        # ★ 到这里游戏已经放行了，下面才是"写文件 / 记状态 / 打日志"
        if post is not None:
            try:
                post()
            except Exception as exc:                            # noqa: BLE001
                self._selfprotect("dump 收尾时出错：%s: %s" % (type(exc).__name__, exc),
                                  pid=pid)
        if code == EVENT_EXIT_PROCESS:
            self._detach(pid, "进程退出")
        self._publish(min_gap=5.0 if status != DBG_CONTINUE else 30.0)

    def _selfprotect(self, msg: str, pid: int | None = None) -> None:
        """自己出问题时的统一出口：**先放行，再记录**。

        pid 给定 = 只分离那个进程；不给 = 全部分离（然后主循环会重新扫、重新附加）。
        分离前 _detach 会排空已排队的事件（事件排队时进程是冻着的），
        所以"分离"本身就是一条可靠的放行路径。
        """
        self._log("★ 自我保护：%s" % msg)
        self._set_error(msg, pid=pid, publish=False)   # 先只记在内存里
        try:
            if pid is not None and pid in self._targets:
                self._detach(pid, "自我保护放行")
            elif pid is None:
                self._detach_all()
        finally:
            self._publish(force=True)
        self._stop.wait(0.2)

    def _on_exception(self, ev: DEBUG_EVENT):
        """→ (ContinueDebugEvent 的状态, 放行之后要跑的收尾函数或 None)。"""
        pid, tid = ev.dwProcessId, ev.dwThreadId
        rec = EXCEPTION_RECORD()
        ctypes.memmove(ctypes.byref(rec), bytes(ev._union), ctypes.sizeof(rec))
        first = struct.unpack_from("<I", ev._union, 152)[0] != 0
        with self._lock:
            key = "0x%08x" % rec.ExceptionCode
            self._exc_counts[key] = self._exc_counts.get(key, 0) + 1
        t = self._targets.get(pid)
        if t is None:
            return DBG_EXCEPTION_NOT_HANDLED, None

        # 初始断点（attach 时系统在自己进程里踩的 int3）：**必须** DBG_CONTINUE。
        # 它没有任何游戏侧处理器，交回去 = UnhandledExceptionFilter → second-chance
        # → 当场把游戏杀掉（而且我们还会为它白写一份 dump）。
        if rec.ExceptionCode == EXCEPTION_BREAKPOINT and not t.initial_bp_seen:
            t.initial_bp_seen = True
            if self.breakpoint_continue == "none":
                return DBG_EXCEPTION_NOT_HANDLED, None
            return DBG_CONTINUE, None
        if rec.ExceptionCode == EXCEPTION_BREAKPOINT and self.breakpoint_continue == "all":
            return DBG_CONTINUE, None

        reason = None
        if not first:
            reason = "second-chance（未处理异常）"
        elif rec.ExceptionCode == EXCEPTION_ACCESS_VIOLATION:
            ctx = _grab_context(tid)
            if ctx is None:
                return DBG_EXCEPTION_NOT_HANDLED, None
            modules = modules_of(pid)
            hit, rva, entry = match_whitelist(ctx.Rip, modules, self.image_name,
                                              self.rva_whitelist)
            if hit:
                reason = "first-chance AV，故障指令落在白名单（%s）" % entry
            else:
                # 非白名单的 AV：只是放回去，不做重活（拖慢游戏的代价 >> 信息量）
                return DBG_EXCEPTION_NOT_HANDLED, None
        else:
            return DBG_EXCEPTION_NOT_HANDLED, None

        return DBG_EXCEPTION_NOT_HANDLED, self._dump_for(t, ev, rec, reason, first)

    # ---------------------------------------------------------------- 落盘
    def _dump_for(self, t: _Target, ev: DEBUG_EVENT, rec: EXCEPTION_RECORD,
                  reason: str, first: bool) -> None:
        tid = ev.dwThreadId
        accessed = (rec.ExceptionInformation[1]
                    if rec.NumberParameters >= 2 else None)
        key = (t.pid, tid, rec.ExceptionCode, accessed)
        now = time.time()
        # first-chance 落完马上还会来一次 second-chance（同一个异常），别写两份
        if now - self._recent.get(key, 0.0) < 15.0:
            return
        self._recent[key] = now
        if len(t.dumps) >= self.max_dumps:
            with self._lock:
                self._stats["refused"] += 1
            self._log("★ pid=%d 本次运行已落 %d 份 dump（上限 %d），这一份不写了"
                      % (t.pid, len(t.dumps), self.max_dumps))
            return

        ctx = _grab_context(tid)
        modules = modules_of(t.pid)
        site = _fault_site(ctx.Rip if ctx else (rec.ExceptionAddress or 0),
                           modules, self.image_name)
        if time.time() < self._disk_block_until:
            return None
        try:
            _ensure_space(self.out_dir, self.min_free_bytes)
        except DumpError as exc:
            with self._lock:
                self._stats["refused"] += 1
            self._disk_block_until = time.time() + 60.0     # 别对同一件事刷屏
            return lambda: self._set_error("拒绝写 dump（%s）：%s" % (reason, exc),
                                           pid=t.pid)

        path = os.path.join(self.out_dir, dump_name(
            t.pid, exc_code=rec.ExceptionCode, fault_rva=site["fault_rva"]))
        mei = keep = None
        if ctx is not None:
            mei, keep = _build_exception_param(rec, ctx, tid)
        self._log("→ 落 dump：pid=%d tid=%d %s 故障RIP=%s %s  (%s)"
                  % (t.pid, tid, reason,
                     _hex(ctx.Rip) if ctx else _hex(rec.ExceptionAddress or 0),
                     ("%s+0x%x" % (site["fault_module"], site["fault_rva"]))
                     if site["fault_rva"] is not None else "",
                     os.path.basename(path)))
        t0 = time.perf_counter()
        res = _run_dump(t.pid, path, mei, keep, timeout=self.dump_timeout)
        # 下面这些是"锦上添花"的采集：任何一个失败都不许把已经写好的 dump 丢掉
        # （dump 本体是最贵的东西，记录必须留下）。
        info = _safe(lambda: process_info(t.pid),
                     {"image": t.image, "started": t.started, "uptime_s": t.uptime_s})
        threads = _safe(lambda: thread_rips(t.pid), [])
        mem_csv_path = os.path.splitext(path)[0] + ".memory.csv"
        mem = _safe(lambda: memory_map_csv(t.pid, mem_csv_path),
                    {"error": "memory_map_csv 没跑成", "csv": None})
        frozen_s = time.perf_counter() - t0
        when = time.time()
        payload = {
            "kind": "watch",
            "pid": t.pid,
            "tid": tid,
            "tid64": "%x.%x" % (t.pid, tid),
            # 时间三件套：time 就是 time.time()（跟 nn 的日志同一个基准），
            # utc/local 是给人看的（对时间轴用）。
            "time": when,
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
            "time_local": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when)),
            "image": info.get("image") or t.image,
            "process_started": info.get("started") or t.started,
            "uptime_s": info.get("uptime_s"),
            "context_txt": None,        # 放行之后由 finish() 补上（见那里的说明）
            "memory_csv": mem.get("csv"),
            "memory_summary": {k: v for k, v in mem.items() if k != "csv"},
            "reason": reason,
            "first_chance": bool(first),
            "exception": {
                "code": rec.ExceptionCode,
                "code_hex": "0x%08x" % rec.ExceptionCode,
                "flags": rec.ExceptionFlags,
                "address": rec.ExceptionAddress or 0,
                "num_parameters": rec.NumberParameters,
                "information": list(rec.ExceptionInformation[:max(0, min(
                    rec.NumberParameters, 15))]),
                "access_type": (["read", "write", "execute"][rec.ExceptionInformation[0]]
                                if rec.NumberParameters >= 1
                                and rec.ExceptionInformation[0] <= 2 else None),
                "access_address": accessed,
            },
            "fault_rip": ctx.Rip if ctx else None,
            "fault_rva": site["fault_rva"],
            "fault_module": site["fault_module"],
            "module_base": site["fault_module_base"],
            "main_module_base": site["main_module_base"],
            "registers": context_to_dict(ctx) if ctx else None,
            "modules": [{"base": m["base"], "size": m["size"], "name": m["name"]}
                        for m in modules],
            "threads": threads,
            "thread_count": len(threads),
            "self_usage": self_usage(),
            "dump": {"path": path, "size": res.get("size"),
                     "seconds": round(res.get("seconds") or 0.0, 3),
                     "frozen_seconds": round(frozen_s, 3),
                     "timed_out": res.get("timed_out"), "ok": res.get("ok"),
                     "error": res.get("error"), "flags": "0x%x" % DUMP_FLAGS,
                     "incomplete": bool(res.get("timed_out"))},
        }
        payload.update(build_sidecars(t.pid, path, info, modules, self.image_name))
        record = {
            "path": path,
            "size": res.get("size") or 0,
            "exc_code": "0x%08x" % rec.ExceptionCode,
            "fault_rva": site["fault_rva"],
            "fault_module": site["fault_module"],
            "tid": tid,
            "time": when,
            "json": None,
            "seconds": round(res.get("seconds") or 0.0, 3),
            "frozen_seconds": round(frozen_s, 3),
            "reason": reason,
            "incomplete": bool(res.get("timed_out")),
            "error": res.get("error"),
            "context_txt": None,
            "memory_csv": mem.get("csv"),
        }

        def finish():
            """★ 这个闭包由 _pump 在 ContinueDebugEvent **之后**调用：
            游戏先放行，然后才写文件 / 记状态 / 打日志（需求 #5）。"""
            try:
                record["context_txt"] = _context_bundle(path)
                payload["context_txt"] = record["context_txt"]
            except Exception as exc:                            # noqa: BLE001
                self._log("（context.txt 没写成：%s）" % exc)
            try:
                record["json"] = _sidecar_json(path, payload)
            except Exception as exc:                            # noqa: BLE001
                self._log("（sidecar json 没写成：%s）" % exc)
            with self._lock:
                t.dumps.append(record)
                self._dumps.append(record)
                self._stats["dumps"] += 1
                if frozen_s * 1000.0 > self._stats["frozen_max_ms"]:
                    self._stats["frozen_max_ms"] = frozen_s * 1000.0
            if res.get("ok") and not res.get("timed_out"):
                self._log("✔ dump 完成：%s  %.1f MB  MiniDumpWriteDump %.1fs"
                          "（进程共冻 %.1fs）；同名 .json / .context.txt / .memory.csv 已写"
                          % (os.path.basename(path), (res.get("size") or 0) / 1e6,
                             res.get("seconds") or 0.0, frozen_s))
                self._set_state("dumped", pid=t.pid, error=None)
            else:
                self._set_error("dump 失败（pid=%d）：%s" % (t.pid, res.get("error")),
                                pid=t.pid)
        return finish
