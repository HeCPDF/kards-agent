#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crashdump.py —— KARDS 崩溃时自动落**完整内存** dump（常驻旁观，只读）。

为什么要有它
============
游戏偶发访问违例（0xC0000005），UE 自带的崩溃处理器只写 ~750KB 的 minidump
（寄存器 + 栈 + 少量内存，**没有堆**）。已知崩溃点的样子是：

    kards_Win64_Shipping+0x11d37e6   call qword ptr [rax+28h]   rax=垃圾值

这是 UE 非动态多播委托的调用列表被遍历、某一项的委托实例已被释放（use-after-free）。
小 dump 里看不到那个列表、也看不到被释放的对象是什么 —— 所以必须在**崩的那一刻**
对进程做一次 MiniDumpWithFullMemory。崩溃不可控、只是偶发 ⇒ 只能常驻等它。

三个子命令
==========
    python tools/crashdump.py watch                    # 常驻等崩溃（Ctrl+C 干净分离）
    python tools/crashdump.py now [--pid N] [--tag T]  # 立刻打一份（测试/复现打点）
    python tools/crashdump.py analyze <dump> [--heap 0xADDR]
    python tools/crashdump.py selftest                 # 自测（不需要游戏）
    python tools/crashdump.py status                   # 打印 status.json（给 agent 看）

也可以当库用（本文件**没有**任何 import 期副作用）：

    from crashdump import start_watch, dump_now, analyze
    w = start_watch()                     # 后台线程里跑调试器循环，调用方不阻塞
    ...
    w.stop()                              # 干净 DebugActiveProcessStop

设计要点（每一条都是踩过坑才写下来的）
======================================
1. **只用 Windows Debug API 旁观**：DebugActiveProcess / WaitForDebugEvent /
   ContinueDebugEvent。不写内存、不改保护、不注入代码。
   ★ 一进来先把 `DebugSetProcessKillOnExit(FALSE)` 打开：默认行为是**调试器进程一退出
     就把被调试进程杀掉**。这条正是"短命注入进程一退出游戏就崩"的同类事故来源，
     所以它必须是 attach 之后第一个调用。
2. **DBG_EXCEPTION_NOT_HANDLED**：first-chance 异常原样还给游戏自己处理（我们不吞）。
   唯一例外是 attach 时那个初始断点（0x80000003）—— 它**没有**任何游戏侧处理器，
   交回去 = 进程当场被杀。所以初始断点必须 DBG_CONTINUE（所有调试器都这么干）。
3. **时间线尽量短**：非白名单的 first-chance 异常只做「读事件 → ContinueDebugEvent」，
   不查模块、不问上下文（UE 每帧都有大量无害 first-chance：0xE06D7363 / 0x80000003 /
   0x80000004 / 0x40010006 / 0x406D1388 …）。
4. **写 dump 时进程是冻住的**（调试事件挂起 = 全体线程暂停），MiniDumpWriteDump 就在
   这个窗口里跑，写完立刻 ContinueDebugEvent 把异常还给游戏。写不完就超时解冻
   （宁可 dump 不完整，也不能把游戏永久冻在调试器里 —— 这是无人值守工具的底线）。
5. **异常上下文必须自己搭**：DEBUG_EVENT 里只有内联的 EXCEPTION_RECORD，没有
   CONTEXT 指针（那不是 SEH 的 EXCEPTION_POINTERS）。做法是 GetThreadContext 拿故障
   线程上下文，再在自己进程里拼 EXCEPTION_POINTERS，用 ClientPointers=FALSE 传进去。
   ★ 坑：`minidumpapiset.h` 整个包在 `#include <pshpack4.h>` 里 ⇒ x64 上
     `MINIDUMP_EXCEPTION_INFORMATION` 是 **16 字节**（ThreadId@0, ExceptionPointers@4,
     ClientPointers@12）。ctypes 不写 `_pack_ = 4` 就是 24 字节，dbghelp 会去读错位的
     指针，直接返回 ERROR_INVALID_HANDLE（或卡死）—— 实测踩过。
6. **调试器和 frida_agent 共存**：frida 不走 Debug API，两者不抢调试端口。我们只是
   多了一个"异常先给调试器看一眼"的旁观者。★ 一个必须知道的副作用：进程被调试时
   `UnhandledExceptionFilter` 不再调用游戏注册的处理器（系统直接转 second-chance 给
   调试器）⇒ 挂着 watch 时 UE 自己那份小 dump **不会**再写。我们的完整 dump 覆盖它。
7. **不 import 项目内其它模块**：崩溃分析工具必须在整棵树被搬动/改坏时照常能跑。
   `tools/mdmp.py` 那几行解析太小，复制成本低于耦合成本。`import _bootstrap` 只为跟
   `tools/` 的惯例一致（拿路径），拿不到也不影响本脚本任何功能。

输出
====
    D:\\Kards\\kards-data\\crashdumps\\<ts>-<pid>-<exc>-rva<rva>.dmp    完整 dump
    …\\<同名>.json                                                   现场元数据
    …\\<同名>.txt                                                    cdb 分析结果
    …\\status.json                                                   （原子写）状态
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import msvcrt
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from bisect import bisect_right

# P6：实现已按职责拆到 tools/_crashdump_*.py（零逻辑改动）；本文件保留文档 + CLI，并把所有名字
# （含下划线私有名）重新导出，`import crashdump` / `crashdump.X` / 命令行用法都与拆分前一致。
import _crashdump_selftest as _m_all
globals().update({_k: _v for _k, _v in vars(_m_all).items() if not _k.startswith("__")})


# --------------------------------------------------------------------------- CLI
def _emit(payload: dict, ok: bool, json_mode: bool, human: list | None = None) -> int:
    """`--json` 时 stdout **只有一行 JSON**；不带 `--json` 时给人读。进度全在 stderr。"""
    if json_mode:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    else:
        for line in (human or []):
            print(line)
        if not human:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return 0 if ok else 1


def _parse_rvas(values) -> tuple:
    out = []
    for v in values or ():
        v = v.strip()
        if v == "*" or "+" in v:            # "*" 或 "mod+0xRVA"
            out.append(v)
            continue
        out.append(int(v, 0))
    return tuple(out)


def _cmd_watch(a) -> int:
    common = dict(pids=a.pid or None,
                  rva_whitelist=(_parse_rvas(a.rva) if a.rva is not None
                                 else DEFAULT_RVAS),
                  out_dir=a.out, max_dumps=a.max_dumps, image_name=a.image,
                  min_free_gb=a.min_free_gb, status_path=a.status_path,
                  engine=a.engine, verbose=not a.quiet)
    if a.engine == "procdump":
        common.update(codes=tuple(a.procdump_codes.split(",")), termination=a.pd_term,
                      keep_nonmatching=a.keep_nonmatching, reflect=a.reflect,
                      dump_timeout=a.dump_timeout)
    elif a.engine == "auto":
        common.update(codes=tuple(a.procdump_codes.split(",")), termination=a.pd_term,
                      keep_nonmatching=a.keep_nonmatching, reflect=a.reflect,
                      dump_timeout=a.dump_timeout, breakpoint_continue=a.breakpoints)
    else:
        common.update(dump_timeout=a.dump_timeout, breakpoint_continue=a.breakpoints)
    w = start_watch(**common)
    log("状态文件：%s" % w.status_path)
    if a.engine == "procdump":
        # ★ 读 status() 的快照（它有 `self.command or 预览` 的回退），别直接摸
        #   `w.command` —— 那个字段是后台线程写的，跟 start() 返回之间本来有竞态。
        log("引擎=procdump：命令 %s"
            % " ".join(w.status()["procdump"]["cmdline_preview"]))
        log("停止时给它发 CTRL_BREAK（正常退出），**不会** kill —— 杀它会把游戏带崩")
    else:
        log("引擎=native：Ctrl+C 结束时先排空事件再 DebugActiveProcessStop，"
            "不会把游戏留在挂起态")
    deadline = time.time() + a.duration if a.duration else None
    try:
        while True:
            if deadline and time.time() >= deadline:
                log("--duration 到点，收工")
                break
            if not w.status()["watching"]:
                log("监视线程已退出")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在干净分离 …")
    stopped = w.stop(timeout=180.0)
    st = w.status()
    payload = {"ok": stopped, "state": st["state"], "stopped": stopped,
               "engine": st.get("engine", "native"),
               "dumps": st["dumps"], "exc_counts": st.get("exc_counts"),
               "events": st.get("events"), "stats": st.get("stats"),
               "self_usage": st.get("self_usage"),
               "status_file": w.status_path, "error": st.get("error")}
    human = ["dump 共 %d 份：" % len(st["dumps"])] + \
            ["  %s  %.1f MB  %s" % (d["path"], d["size"] / 1e6, d["exc_code"])
             for d in st["dumps"]] + \
            ["first-chance 异常计数：%s" % st.get("exc_counts"),
             "调试事件计数：%s" % st.get("events"),
             "自身开销：%s" % st.get("self_usage")]
    return _emit(payload, stopped, a.json, human)


def _cmd_now(a) -> int:
    res = dump_now(pid=a.pid, tag=a.tag or "", out_dir=a.out, timeout=a.timeout,
                   image_name=a.image, engine=a.engine, reflect=a.reflect)
    rec = {"path": res["path"], "size": res["size"], "exc_code": None,
           "fault_rva": None, "tid": None, "time": time.time(),
           "json": res.get("json"), "seconds": res["seconds"], "kind": "now"}
    res["status_file_updated"] = publish_manual_dump(default_status_path(a.out), rec)
    res["ok"] = True
    return _emit(res, True, a.json,
                 ["✔ %s  %.1f MB  写了 %.1fs（引擎 %s%s）pid=%s  线程 %d  模块 %d"
                  % (res["path"], res["size"] / 1e6, res["seconds"], res["engine"],
                     " -r" if res.get("reflect") else "", res["pid"],
                     res["threads"], res["modules"]),
                  "  cdb 打开：cdb -z \"%s\"" % res["path"],
                  "  同名 .json / .context.txt / .memory.csv 也在同一目录"])


def _cmd_analyze(a) -> int:
    res = analyze(a.dump, heap=a.heap, cdb=a.cdb, timeout=a.timeout,
                  txt_path=a.out_txt, delegates=a.delegates)
    s = res["summary"]
    human = [
        "异常 %s  tid=%s  故障 RIP=%s  RVA=%s  线程 %d" %
        (s["exception"]["code"], s["exception"]["tid"],
         _hex(s["fault_rip"]) if s["fault_rip"] else None,
         ("%s+0x%x" % (s["fault_module"], s["fault_rva"]))
         if s["fault_rva"] is not None else None, s["thread_count"]),
        "调用栈（前 12 帧）：",
    ] + ["  %2d %s!%s+%s" % (i, f.get("module"), f.get("symbol"), f.get("offset"))
         for i, f in enumerate(s["stack"][:12])] + [
        "frida：崩溃线程有 frida 帧=%s；在 ProcessEvent=%s；含 frida 帧的线程 %s"
        % (s["frida"]["crash_thread_has_frida_frame"],
           s["frida"]["crash_thread_in_processevent"],
           s["frida"]["threads_with_frida_frames"]),
        "内存：%s" % s["memory"]["verdict"],
        "完整 cdb 输出：%s" % res["txt"]]
    if "delegates" in s:
        d = s["delegates"]
        human.append("委托候选 %d 组，可疑项 %d 个" % (len(d["candidates"]),
                                                     len(d["suspects"])))
        for sus in d["suspects"][:8]:
            human.append("  ★ 下标 %d @ %s  ptr=%s  %s"
                         % (sus["index"], _hex(sus["address"]), _hex(sus["ptr"]),
                            "；".join(sus["notes"])))
    if "heap" in s:
        for line in s["heap"]["verdict"][:6]:
            human.append("  heap: %s" % line)
        human.append("  heap 细节：%s" % s["heap"]["cdb_txt"])
    return _emit({"ok": True, "txt": res["txt"], "summary": s}, True, a.json, human)


def _cmd_selftest(a) -> int:
    rep = selftest(keep=a.keep, cdb=a.cdb, out_dir=a.out, timeout=a.timeout,
                   crashes=a.crashes)
    human = ["自测%s（工作目录 %s）" % ("通过 ✔" if rep["ok"] else "**失败**",
                                      rep["work_dir"])]
    for s in rep["scenarios"]:
        human.append("  [%s] %s %s" % ("ok" if s.get("ok") else "FAIL", s["name"],
                                       s.get("skipped", "")))
        if s.get("dump"):
            human.append("        dump=%s  %.1fMB  exc=%s  reason=%s"
                         % (s["dump"], s["size"] / 1e6, s.get("exc_code"),
                            s.get("reason")))
        if s.get("ecxr"):
            human.append("        .ecxr 停在故障指令=%s  指令=%s"
                         % (s["ecxr"].get("stopped_at_fault"),
                            (s["ecxr"].get("instruction") or "")[:70]))
        if s.get("cdb"):
            human.append("        cdb 前几帧：%s" % (s["cdb"].get("lines"),))
        if s.get("paths"):
            human.append("        %d 份 dump：%s" % (len(s["paths"]), s["paths"]))
    human.append("  自身开销：%s" % rep.get("self_usage"))
    return _emit(rep, rep["ok"], a.json, human)


def _cmd_status(a) -> int:
    path = a.status_path or default_status_path(a.out)
    st = read_json(path)
    if st is None:
        return _emit({"ok": False, "error": "读不到 %s" % path, "status_file": path},
                     False, a.json, ["读不到 %s" % path])
    st = dict(st)
    st["ok"] = True
    st["status_file"] = path
    human = ["state=%s pid=%s since=%s"
             % (st.get("state"), st.get("pid"),
                time.strftime("%H:%M:%S", time.localtime(st.get("since") or 0))),
             "dumps=%d  异常计数=%s  事件计数=%s"
             % (len(st.get("dumps") or []), st.get("exc_counts"), st.get("events")),
             "error=%s" % st.get("error")]
    for d in (st.get("dumps") or [])[-3:]:
        human.append("  %s  %.1f MB  %s" % (d.get("path"), (d.get("size") or 0) / 1e6,
                                            d.get("exc_code")))
    return _emit(st, True, a.json, human)


def _cmd_stop_procdump(a) -> int:
    """收掉游离的 ProcDump（可能是别的 agent 的会话留下的）。**CTRL_BREAK，不 kill。**

    ★ 默认只**列出来**（含父进程），必须 `--pid N` 或 `--all` 才真的动手 ——
      这条护栏是因为我自己踩过：不加参数就把**另一个 agent 会话的** ProcDump 收掉了。
    """
    cands = list_pids("procdump")
    if a.pid is not None:
        cands = [c for c in cands if c["pid"] == a.pid] or \
                ([{"pid": a.pid, "exe": "?"}] if _pid_alive(a.pid) else [])
    if not cands:
        return _emit({"ok": True, "found": 0, "results": [],
                      "note": "没有 procdump 在跑"},
                     True, a.json, ["没有 procdump 在跑"])

    def parent_of(pid: int) -> str:
        parent = ""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").ParentProcessId"
                 % pid], capture_output=True, text=True, timeout=30)
            ppid = (out.stdout or "").strip().splitlines()
            ppid = int(ppid[-1]) if ppid and ppid[-1].isdigit() else None
            if ppid:
                out2 = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").Name"
                     % ppid], capture_output=True, text=True, timeout=30)
                parent = "%s(%d)" % ((out2.stdout or "").strip().splitlines()[-1]
                                     if (out2.stdout or "").strip() else "?", ppid)
        except Exception:                                   # noqa: BLE001
            pass
        return parent or "?"

    # 先只列（不动手）
    if a.pid is None and not a.all:
        rows = [{"pid": c["pid"], "parent": parent_of(c["pid"])} for c in cands]
        human = ["发现 %d 个 procdump（**没有动手**）：" % len(rows)]
        for r in rows:
            human.append("  pid=%s 父进程=%s" % (r["pid"], r["parent"]))
        human.append("要收掉哪一个：--pid <N>；确实要全收：--all（都走 CTRL_BREAK，不 kill）")
        return _emit({"ok": True, "found": len(rows), "listed": rows,
                      "note": "只列不动手：加 --pid N 或 --all 才真的收"}, True,
                     a.json, human)

    results = []
    for c in cands:
        parent = parent_of(c["pid"])
        r = stop_stray_procdump(c["pid"])
        r["parent"] = parent
        results.append(r)
    ok = all(r["ok"] for r in results)
    human = ["找到 %d 个 procdump：" % len(cands)]
    for r in results:
        human.append("  pid=%s 父进程=%s → %s%s"
                     % (r["pid"], r.get("parent") or "?",
                        "已优雅退出 ✔" if r["ok"] else "★ 没停掉（见下）",
                        "" if r["ok"] else "\n      " + (r.get("hint") or r.get("text", ""))))
    return _emit({"ok": ok, "found": len(cands), "results": results}, ok, a.json, human)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="crashdump.py",
        description="KARDS 崩溃自动落完整内存 dump（只读旁观）/ 手动打点 / cdb 分析")
    ap.add_argument("--json", action="store_true", default=False,
                    help="stdout 只输出一行 JSON（进度全在 stderr）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    silent_default = dict(default=argparse.SUPPRESS)

    p = sub.add_parser("watch", help="常驻等崩溃（等进程出现；重启后自动再挂）")
    p.add_argument("--pid", type=int, action="append", help="明确指定 pid（可重复）")
    p.add_argument("--rva", action="append",
                   help="白名单：0x11d37e6 或 mod+0x...（可重复；不传=默认两条）")
    p.add_argument("--out", default=DEFAULT_OUT_DIR)
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--max-dumps", type=int, default=3)
    p.add_argument("--min-free-gb", type=float, default=10.0)
    p.add_argument("--dump-timeout", type=float, default=300.0)
    p.add_argument("--status-path", default=None)
    p.add_argument("--breakpoints", choices=("initial", "all", "none"),
                   default="initial",
                   help="int3 断点怎么回（默认只放初始那个；见文件头说明）")
    p.add_argument("--engine", choices=("native", "procdump", "auto"),
                   default="procdump",
                   help="抓取引擎（默认 procdump=微软 ProcDump 抓+事后按 RVA 判定）；"
                        "native=自研调试器循环（能在落盘前按 RVA 过滤，备用）；"
                        "auto=ProcDump 可用就用它")
    p.add_argument("--procdump-codes", default="C0000005",
                   help="procdump 引擎的 -f 过滤（逗号分隔）")
    p.add_argument("--pd-term", action="store_true",
                   help="procdump 引擎再加 -t（进程终止时也落一份）")
    p.add_argument("--prune-nonmatching", dest="keep_nonmatching",
                   action="store_false", default=True,
                   help="procdump 引擎：不在 RVA 白名单里的 dump 直接删掉（默认留着）")
    p.add_argument("--reflect", action="store_true",
                   help="procdump 引擎加 -r（反射/克隆：少冻目标；实测 .ecxr 仍可用）。"
                        "崩溃 dump 默认不加（定格那一瞬的保真优先）")
    p.add_argument("--duration", type=float, default=0.0, help="跑多少秒收工（0=一直跑）")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", **silent_default)

    p = sub.add_parser("now", help="立刻给正在跑的游戏打一份完整 dump")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--tag", default="")
    p.add_argument("--out", default=DEFAULT_OUT_DIR)
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--engine", choices=("auto", "procdump", "native"), default="auto",
                   help="默认 auto：ProcDump 可用就用它（-ma）")
    p.add_argument("--reflect", action="store_true", default=False,
                   help="procdump 引擎加 -r（反射/克隆）：游戏冻结 0 秒，但实测**丢大部分"
                        "私有堆页**（cdb 里那些地址是 ????????），默认不开")
    p.add_argument("--json", action="store_true", **silent_default)

    p = sub.add_parser("analyze", help="cdb 分析 dump → 同名 .txt + summary")
    p.add_argument("dump")
    p.add_argument("--heap", default=None,
                   help="顺带回答「这个地址是什么」并走一遍委托列表")
    p.add_argument("--delegates", action="store_true",
                   help="在寄存器和栈上找多播委托调用列表候选")
    p.add_argument("--cdb", default=None)
    p.add_argument("--timeout", type=float, default=1200.0)
    p.add_argument("--out-txt", default=None)
    p.add_argument("--json", action="store_true", **silent_default)

    p = sub.add_parser("selftest", help="三幕自测（不需要游戏）")
    p.add_argument("--out", default=None, help="自测工作目录（默认系统临时目录）")
    p.add_argument("--cdb", default=None)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--crashes", type=int, default=3, help="C 幕的崩溃次数")
    p.add_argument("--keep", dest="keep", action="store_true", default=True)
    p.add_argument("--clean", dest="keep", action="store_false")
    p.add_argument("--json", action="store_true", **silent_default)

    p = sub.add_parser("status", help="打印 status.json（给 agent 读）")
    p.add_argument("--out", default=DEFAULT_OUT_DIR)
    p.add_argument("--status-path", default=None)
    p.add_argument("--json", action="store_true", **silent_default)

    p = sub.add_parser("stop-procdump",
                       help="优雅收掉游离的 ProcDump（CTRL_BREAK，绝不 kill）")
    p.add_argument("--pid", type=int, default=None,
                   help="要收掉哪一个（**不传只列出来给你看，不动手**）")
    p.add_argument("--all", action="store_true",
                   help="收掉本机所有 procdump64（会打印每个的父进程；慎用）")
    p.add_argument("--json", action="store_true", **silent_default)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "watch":
            return _cmd_watch(a)
        if a.cmd == "now":
            return _cmd_now(a)
        if a.cmd == "analyze":
            return _cmd_analyze(a)
        if a.cmd == "selftest":
            return _cmd_selftest(a)
        if a.cmd == "status":
            return _cmd_status(a)
        if a.cmd == "stop-procdump":
            return _cmd_stop_procdump(a)
        ap.error("unknown command %s" % a.cmd)
        return 2
    except DumpError as exc:
        return _emit({"ok": False, "error": str(exc)}, False,
                     getattr(a, "json", False), ["✘ %s" % exc])
    except KeyboardInterrupt:
        return _emit({"ok": False, "error": "interrupted"}, False,
                     getattr(a, "json", False), ["(中断)"])


if __name__ == "__main__":
    sys.exit(main())
