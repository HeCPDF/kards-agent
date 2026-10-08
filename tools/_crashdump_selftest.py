# -*- coding: utf-8 -*-
"""start_watch / selftest / verify_full_dump —— 从 crashdump.py 原样搬出（P6 拆文件，零逻辑改动）。
"""
from __future__ import annotations

import _crashdump_base as _m__crashdump_base
globals().update({_k: _v for _k, _v in vars(_m__crashdump_base).items() if not _k.startswith("__")})
import _crashdump_mdmp as _m__crashdump_mdmp
globals().update({_k: _v for _k, _v in vars(_m__crashdump_mdmp).items() if not _k.startswith("__")})
import _crashdump_watch as _m__crashdump_watch
globals().update({_k: _v for _k, _v in vars(_m__crashdump_watch).items() if not _k.startswith("__")})
import _crashdump_procdump as _m__crashdump_procdump
globals().update({_k: _v for _k, _v in vars(_m__crashdump_procdump).items() if not _k.startswith("__")})


def start_watch(pids=None, rva_whitelist=DEFAULT_RVAS, out_dir: str = DEFAULT_OUT_DIR,
                max_dumps: int = 3, engine: str = "procdump", **kw):
    """在**后台线程**里跑调试器循环；调用方立刻拿到 Watcher，不阻塞。

    pids=None        → 按 image_name 自动等进程出现 / 重启后自动再挂
    rva_whitelist    → 见 match_whitelist（int = 主模块 RVA；空 = 只落 second-chance）
    max_dumps        → 同一进程一次运行最多几份（异常风暴保护）
    engine           → "procdump"（**默认**：微软 ProcDump 抓 + 我们事后补元数据/按 RVA
                                  判定；它做不到按 RVA 过滤，所以会多留几份待筛）
                       "native"（自研调试器循环：能**在落盘前**按故障指令 RVA 过滤，
                                 没有无害 AV 时一份都不写；备用路线）
    """
    if engine == "auto":
        engine = "procdump" if procdump_accepted().get("accepted") else "native"
    common = dict(pids=pids, rva_whitelist=rva_whitelist, out_dir=out_dir,
                  max_dumps=max_dumps)
    # 两个引擎接受的参数不同：按引擎把 kw 分派过去，不把无关参数硬塞给构造函数。
    native_keys = {"image_name", "min_free_gb", "dump_timeout", "poll_interval",
                   "status_path", "breakpoint_continue", "max_targets", "verbose"}
    pd_keys = {"image_name", "min_free_gb", "status_path", "codes", "first_chance",
               "termination", "poll_interval", "keep_nonmatching", "reflect",
               "dump_timeout", "verbose"}
    keys = pd_keys if engine == "procdump" else native_keys
    common.update({k: v for k, v in kw.items() if k in keys})
    dropped = sorted(set(kw) - keys)
    if dropped:
        log("（引擎 %s 用不到这些参数：%s）" % (engine, ", ".join(dropped)))
    if engine == "procdump":
        w = ProcDumpWatcher(**common)
    else:
        w = Watcher(**common)
    return w.start()


# --------------------------------------------------------------------------- selftest
# 注意：这是**要交给子进程执行的源码**，所以别在这里用 % 格式化（里面的 % 会和
# 我们自己的拼串打架 —— 踩过）。延迟用 DELAY 占位符替换。
AV_SNIPPET = ("import ctypes,sys,time,os;"
              "sys.stderr.write('   [target] up pid=' + str(os.getpid()) + chr(10));"
              "sys.stderr.flush();"
              "time.sleep(DELAY);"
              "sys.stderr.write('   [target] faulting now' + chr(10));"
              "sys.stderr.flush();"
              "b=(ctypes.c_char*8).from_address(1);"
              "print(b[0])")


def _selftest_child(delay: float = 2.0):
    """一个**一定会访问违例**的小进程：从地址 1 读一个字节。

    为什么不用 `ctypes.string_at(0)`：那条路 ctypes 自己包了 SEH，会转成 OSError。
    从地址 1 读 c_char 数组的元素是裸 C 读、**没有** SEH 包装 ——
    实测进程直接以 0xC0000005 结束（就是我们要的第一手现场）。
    """
    return subprocess.Popen([sys.executable, "-c",
                             AV_SNIPPET.replace("DELAY", "%.2f" % delay)],
                            stdout=subprocess.DEVNULL)


def _wait_for_dumps(w: Watcher, n: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(w.status()["dumps"]) >= n:
            return True
        time.sleep(0.25)
    return len(w.status()["dumps"]) >= n


def _verify_dump_with_cdb(dump_path: str, cdb: str | None,
                          want: tuple = ("access violation",),
                          expect_rip: int | None = None) -> dict:
    """验收原文那一条：`cdb -z <dump> -c ".ecxr; k; q"` 要能看到故障栈。

    这里跑的是它的超集 `.ecxr; r; k 30; q`（多要一份寄存器），因为"`.ecxr` 停在故障
    指令上"最硬的判据是 .ecxr 之后 `r` 里的 rip == dump 里记的异常 RIP；
    另外 cdb 载入 dump 时会自己把"异常地址 + 那条指令"打出来，也一并核对。
    """
    try:
        txt = os.path.splitext(dump_path)[0] + ".ecxr-k.txt"
        res = run_cdb(dump_path, ".ecxr; r; k 30; q", txt, cdb=cdb, timeout=600.0)
    except DumpError as exc:
        return {"ok": False, "error": str(exc)}
    text = res["text"]
    frames = parse_frames(_after_command_echo(text), limit=30)
    regs = parse_registers(text)
    blob = text.lower()
    checks = {
        "loose_ecxr_context": "doesn't have an exception context" not in blob,
        "has_frames": bool(frames),
        "has_instruction_of_interest": "exception of interest" in blob,
        "lines": ["%s!%s+%s" % (f.get("module"), f.get("symbol"), f.get("offset"))
                  for f in frames[:5]],
        "rip_after_ecxr": regs.get("rip"),
        "expect_rip": expect_rip,
    }
    for token in want:
        checks["has_%s" % sanitize(token)] = token.lower() in blob
    landed = _ecxr_landed_on(text, expect_rip) if expect_rip else {"stopped_at_fault": None}
    checks["landed"] = landed
    checks["ok"] = bool(checks["has_frames"] and checks["loose_ecxr_context"]
                        and all(v for k, v in checks.items() if k.startswith("has_"))
                        and (landed.get("stopped_at_fault") or expect_rip is None))
    checks["txt"] = res["txt"]
    return checks


def _ecxr_landed_on(text: str, expect_rip: int | None) -> dict:
    """`cdb -z <dump>` 载入时（以及 `.ecxr` 之后）会把"异常地址 + 那条指令"打出来：

        (4e94.70cc): Access violation - code c0000005 (first/second chance not available)
        python314!PyBytes_FromStringAndSize+0xb7:
        00007ffa`772b2b87 0fb601          movzx   eax,byte ptr [rcx] ...

    第一列就是 RIP。拿它和 dump 里的异常 RIP 对一下 —— 这就是"停在故障指令上"的判据。
    （反汇编字节可能连成 2~12 个 hex 字符，所以别按"字节间有空格"去切。）
    """
    out = {"expect_rip": expect_rip, "found_rips": [], "stopped_at_fault": None,
           "instruction": None}
    if expect_rip is None:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-f`]{8,17})\s+([0-9a-f]{2,12})\s+\S", line)
        if not m:
            continue
        try:
            addr = int(m.group(1).replace("`", ""), 16)
        except ValueError:
            continue
        out["found_rips"].append(addr)
        if addr == expect_rip:
            out["stopped_at_fault"] = True
            out["instruction"] = line.strip()
    if out["stopped_at_fault"] is None:
        out["stopped_at_fault"] = False
    out["found_rips"] = out["found_rips"][:8]
    return out


def verify_full_dump(dump_path: str, cdb: str | None = None,
                     want: tuple = ("access violation",)) -> dict:
    """验收用的三件套（用户要求）：dumpchk 说完整 / `.ecxr` 停得住 / `dq` 有值。

    也给 selftest 当判据 —— 换抓取引擎（自研 ↔ ProcDump）时必须逐条过。
    """
    out = {"path": dump_path, "ok": False}
    md = MiniDump(dump_path)
    # 1) dumpchk
    try:
        p = subprocess.run([DUMPCHK, dump_path], capture_output=True, timeout=600)
        text = _decode_console((p.stdout or b"") + (p.stderr or b""))
        out["dumpchk_finished"] = "Finished dump check" in text
        out["dumpchk_full_memory"] = "with Full Memory" in text
    except Exception as exc:                                # noqa: BLE001
        out["dumpchk_error"] = str(exc)
    out["memory"] = md.memory_stats()["verdict"]
    out["heap_available"] = md.memory_stats()["heap_available"]
    exc_info = md.exception or {}
    out["exception_code"] = ("0x%08x" % exc_info["code"]) if exc_info.get("code") else None
    out["fault_rip"] = exc_info.get("rip")
    # 故障模块优先按"哪个模块包含故障 RIP"定（自测靶子不是 kards，别拿名字硬套）
    main = (md.module_of(out["fault_rip"]) if out["fault_rip"] else None) or \
        main_module_of(md.modules, DEFAULT_IMAGE) or (md.modules[0] if md.modules else None)
    if out["fault_rip"] and main:
        out["fault_rva"] = out["fault_rip"] - main["base"]
    # 2) .ecxr 停在故障指令
    v = _verify_dump_with_cdb(dump_path, cdb, want=want, expect_rip=out["fault_rip"])
    out["ecxr"] = {k: v.get(k) for k in ("ok", "loose_ecxr_context", "has_frames",
                                         "rip_after_ecxr", "lines")}
    out["ecxr_instruction"] = (v.get("landed") or {}).get("instruction")
    out["ecxr_stopped_at_fault"] = (v.get("landed") or {}).get("stopped_at_fault")
    # 3) dq 一个已提交私有区段里的地址
    priv = sorted([r for r in md.memory_info
                   if r["state"] == 0x1000 and r["type"] == 0x20000],
                  key=lambda r: -r["size"])
    addr = None
    for r in priv[:20]:
        blob = md.read(r["base"], 0x1000)
        if not blob:
            continue
        for off in range(0, len(blob) - 8, 8):
            if blob[off:off + 8] != b"\0" * 8:
                addr = r["base"] + off
                break
        if addr:
            break
    out["dq_address"] = addr
    if addr:
        try:
            syms = os.path.join(os.path.dirname(os.path.abspath(dump_path)), "symbols")
            os.makedirs(syms, exist_ok=True)
            c = subprocess.run([cdb_path(cdb), "-z", dump_path, "-y", syms,
                                "-c", "dq %x L2; q" % addr],
                               capture_output=True, timeout=600)
            txt = _decode_console((c.stdout or b"") + (c.stderr or b""))
            shown = [ln for ln in txt.splitlines() if ln.strip().startswith("0000")]
            out["dq_lines"] = shown[:3]
            out["dq_has_values"] = bool(shown) and "????????`????????" not in " ".join(shown)
        except Exception as exc:                            # noqa: BLE001
            out["dq_error"] = str(exc)
    out["ok"] = bool(out.get("dumpchk_finished") and out.get("heap_available")
                     and out.get("ecxr_stopped_at_fault") and out.get("dq_has_values"))
    return out


# --------------------------------------------------------------------------- selftest
def _scen_a(work: str, cdb: str | None, watchers: list, children: list,
            timeout: float) -> tuple[dict, dict | None]:
    """A：first-chance 白名单命中 → 当场落 dump；`.ecxr` 必须停在故障指令上。"""
    child = _selftest_child(2.5)
    children.append(child)
    # A/B/C 三幕测的是**自研调试器循环**的能力（落盘前按 RVA 过滤 + first/second
    # chance 分离 + 重启再挂），所以显式指定 engine="native"（默认已是 procdump）。
    w = start_watch(pids=[child.pid], rva_whitelist=("*",), out_dir=work,
                    max_dumps=3, dump_timeout=180.0, engine="native")
    watchers.append(w)
    got = _wait_for_dumps(w, 1, timeout)
    st = w.status()
    rec = st["dumps"][0] if st["dumps"] else None
    scen = {"name": "A first-chance 白名单命中 + .ecxr 停在故障指令",
            "ok": False, "got_dump": got, "state": st["state"],
            "reason": rec.get("reason") if rec else None}
    if rec:
        scen.update(dump=rec["path"], exc_code=rec["exc_code"], size=rec["size"],
                    seconds=rec.get("seconds"), frozen_seconds=rec.get("frozen_seconds"))
        an = analyze(rec["path"], cdb=cdb, timeout=900.0, txt_path=rec["path"] + ".txt")
        expect = an["summary"]["fault_rip"]
        verify = _verify_dump_with_cdb(rec["path"], cdb, want=("access violation",),
                                      expect_rip=expect)
        landed = verify.get("landed") or {}
        scen["cdb"] = verify
        scen["ecxr"] = landed
        scen["analyze"] = {
            "exception": an["summary"]["exception"]["code"],
            "fault_rip": an["summary"]["fault_rip"],
            "fault_rva": an["summary"]["fault_rva"],
            "access_address": an["summary"]["exception"]["access_address"],
            "threads": an["summary"]["thread_count"],
            "stack": ["%s!%s+%s" % (f.get("module"), f.get("symbol"), f.get("offset"))
                      for f in an["summary"]["stack"][:6]],
            "frida": an["summary"]["frida"],
            "txt": an["txt"],
        }
        blob = " ".join(scen["analyze"]["stack"]).lower()
        scen["ok"] = bool(got and rec["size"] > 1_000_000
                          and rec["exc_code"] == "0xc0000005"
                          and verify.get("ok") and landed.get("stopped_at_fault")
                          and an["summary"]["exception"]["access_address"] == 1
                          and ("ctypes" in blob or "pybytes" in blob
                               or "python" in blob))
    w.stop(timeout=30.0)
    watchers.remove(w)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)
    return scen, rec


def _scen_b(work: str, cdb: str | None, watchers: list, children: list,
            timeout: float) -> tuple[dict, dict | None]:
    """B：白名单为空 → 只走 second-chance（未处理异常）那条路。"""
    child = _selftest_child(2.5)
    children.append(child)
    w = start_watch(pids=[child.pid], rva_whitelist=(), out_dir=work,
                    max_dumps=3, dump_timeout=180.0, engine="native")
    watchers.append(w)
    got = _wait_for_dumps(w, 1, timeout)
    st = w.status()
    rec = st["dumps"][0] if st["dumps"] else None
    scen = {"name": "B second-chance（未处理异常）", "ok": False, "got_dump": got,
            "exc_counts": st.get("exc_counts"), "state": st["state"],
            "reason": rec.get("reason") if rec else None}
    if rec:
        scen.update(dump=rec["path"], exc_code=rec["exc_code"], size=rec["size"])
        verify = _verify_dump_with_cdb(rec["path"], cdb, want=("access violation",))
        scen["cdb"] = verify
        scen["ok"] = bool(got and rec["size"] > 1_000_000
                          and rec["exc_code"] == "0xc0000005"
                          and "second-chance" in (rec.get("reason") or "")
                          and len(st["dumps"]) == 1
                          and verify.get("ok"))
    w.stop(timeout=30.0)
    watchers.remove(w)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)
    return scen, rec


def _scen_c(work: str, cdb: str | None, watchers: list, children: list,
            timeout: float, crashes: int = 3) -> tuple[dict, list]:
    """C：pids=None（等进程出现）+ **连续 N 次**崩溃起停 → 不能漏、不能重复落。

    用 python.exe 的**硬链接**造一个独立 exe 名：直接拿 "python.exe" 当 image 名会挂到
    机器上所有 python（比如常驻的 live_session）上去，那是事故。
    """
    scen = {"name": "C 等进程出现 + 连续 %d 次崩溃不丢不重" % crashes, "ok": False,
            "crashes": crashes}
    target_name = "crashdump-selftest-target.exe"
    link_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                             target_name)
    try:
        if os.path.exists(link_path):
            os.remove(link_path)
        os.link(sys.executable, link_path)          # 同目录硬链接：python314.dll 找得到
    except OSError as exc:
        scen["skipped"] = "建不了硬链接 %s（%s）" % (link_path, exc)
        return scen, []
    try:
        w = start_watch(pids=None, image_name=target_name, rva_whitelist=("*",),
                        out_dir=work, max_dumps=6, dump_timeout=180.0,
                        poll_interval=0.3, engine="native")
        watchers.append(w)
        time.sleep(0.8)
        scen["state_before"] = w.status()["state"]
        for i in range(crashes):
            child = subprocess.Popen([link_path, "-c",
                                      AV_SNIPPET.replace("DELAY", "2.0")],
                                     stdout=subprocess.DEVNULL)
            children.append(child)
            ok = _wait_for_dumps(w, i + 1, timeout)
            scen["crash_%d" % (i + 1)] = ok
            if not ok:
                break
            time.sleep(0.3)
        st = w.status()
        dumps = st["dumps"]
        scen.update(state_after=st["state"], n_dumps=len(dumps),
                    refused=st["stats"].get("refused"),
                    exc_counts=st["exc_counts"], events=st["events"],
                    self_usage=st.get("self_usage"),
                    paths=[d["path"] for d in dumps],
                    sizes=[d["size"] for d in dumps],
                    pids=[os.path.basename(d["path"]).split("-p")[-1].split("-")[0]
                          for d in dumps])
        scen["ok"] = bool(len(dumps) == crashes
                          and all(d["size"] > 1_000_000 for d in dumps)
                          and len(set(scen["paths"])) == crashes
                          and len(set(scen["pids"])) == crashes
                          and len(set(scen["sizes"])) == crashes
                          and not st["stats"].get("refused"))
        w.stop(timeout=30.0)
        watchers.remove(w)
    finally:
        if os.path.exists(link_path):
            try:
                os.remove(link_path)
            except OSError:
                pass
    return scen, []


def _scen_d(work: str, cdb: str | None, watchers: list, children: list,
            timeout: float, reflect: bool = False) -> tuple[dict, dict | None]:
    """D：ProcDump 引擎抓的异常 dump **等价性**（验收原文那三件套）。

    判据：dumpchk 说完整 / `.ecxr` 停在故障指令上 / `dq <堆地址>` 有值。
    reflect=True 那一轮额外回答"`-r` 保留不保留异常上下文"。
    """
    scen = {"name": "D ProcDump 异常 dump 等价（reflect=%s）" % reflect, "ok": False}
    probe = procdump_accepted()
    if not probe["exists"] or probe["accepted"] is not True:
        scen["skipped"] = ("ProcDump 不可用或 EULA 未接受（%s）；"
                           "手动跑一次 %s -accepteula 后这一幕才会真的跑" %
                           (probe.get("how") or probe, PROCDUMP64))
        scen["ok"] = True
        return scen, None
    child = _selftest_child(3.5)
    children.append(child)
    w = start_watch(pids=[child.pid], rva_whitelist=("*",), out_dir=work,
                    engine="procdump", max_dumps=1, reflect=reflect,
                    dump_timeout=300.0, verbose=False)
    watchers.append(w)
    got = _wait_for_dumps(w, 1, timeout)
    st = w.status()
    rec = st["dumps"][0] if st["dumps"] else None
    scen.update(got_dump=got, state=st["state"], cmdline=(st.get("procdump") or {}
                                                          ).get("cmdline"))
    if rec:
        scen.update(dump=rec["path"], size=rec["size"], exc_code=rec["exc_code"],
                    fault_rva=rec["fault_rva"], whitelisted=rec.get("whitelisted"),
                    reason=rec.get("reason"))
        v = verify_full_dump(rec["path"], cdb, want=("access violation",))
        scen["verify"] = {k: v.get(k) for k in
                          ("ok", "dumpchk_finished", "dumpchk_full_memory",
                           "heap_available", "ecxr_stopped_at_fault",
                           "ecxr_instruction", "dq_has_values", "dq_lines",
                           "memory", "fault_rva")}
        # 靶子的故障点在 python DLL 里：RVA 白名单用 "*"，所以只要求"命中"
        scen["ok"] = bool(got and rec["size"] > 1_000_000
                          and rec["exc_code"] == "0xc0000005"
                          and rec.get("whitelisted")
                          and v.get("ok"))
    w.stop(timeout=60.0)
    watchers.remove(w)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)
    return scen, rec


def selftest(keep: bool = True, cdb: str | None = None, out_dir: str | None = None,
             timeout: float = 90.0, crashes: int = 3) -> dict:
    """不依赖游戏的端到端自测（三幕）。

    A. first-chance 白名单命中 → 当场落完整 dump，`.ecxr` 停在故障指令上
    B. 白名单为空 → 走 second-chance（未处理异常）那条路
    C. pids=None：等进程出现（按 exe 名）+ 连续 3 次崩溃起停，不丢不重
    """
    work = out_dir or tempfile.mkdtemp(prefix="crashdump-selftest-")
    os.makedirs(work, exist_ok=True)
    report: dict = {"ok": False, "work_dir": work, "scenarios": [], "dumps": []}
    watchers: list[Watcher] = []
    children: list[subprocess.Popen] = []
    try:
        for name, fn in (("A", _scen_a), ("B", _scen_b)):
            scen, rec = fn(os.path.join(work, name), cdb, watchers, children, timeout)
            report["scenarios"].append(scen)
            if rec:
                report["dumps"].append(rec)
        scen, _ = _scen_c(os.path.join(work, "C"), cdb, watchers, children, timeout,
                          crashes=crashes)
        report["scenarios"].append(scen)
        for tag, reflect in (("D1", False), ("D2", True)):
            scen, rec = _scen_d(os.path.join(work, tag), cdb, watchers, children,
                                timeout, reflect=reflect)
            report["scenarios"].append(scen)
            if rec:
                report["dumps"].append(rec)
        report["status_files"] = [p for p in
                                  (os.path.join(work, n, "status.json")
                                   for n in ("A", "B", "C", "D1", "D2"))
                                  if os.path.exists(p)]
        report["ok"] = all(s.get("ok") for s in report["scenarios"])
        report["self_usage"] = self_usage()
    finally:
        for w in list(watchers):
            try:
                w.stop(timeout=30.0)
            except Exception:                                   # noqa: BLE001
                pass
        for c in children:
            try:
                if c.poll() is None:
                    c.kill()
            except Exception:                                   # noqa: BLE001
                pass
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return report
