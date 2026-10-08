#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_session.py -- 常驻会话，一个进程、一次 attach，命令逐条读、结果逐条写。

跟旧的 session.py 不同：不维护一张过时的命令名表，直接用 `agent.precheck`/
`agent.session.AgentSession`（跟生产命令层完全同一条代码路径，2026-09-28 审计过
没有 string-dispatch drift）。每行命令就是一段 Python 表达式，在
{sess, precheck, BA, time} 这个命名空间里 eval/exec。

通道（文件，不用 stdin/管道，因为这个进程要跨多个工具调用持续存活）：
    kards-data/live/live_cmd.txt   <- 追加一行 = 下一条命令
    kards-data/live/live_log.txt   -> 结果按行追加
（位置统一由 `agent/paths.py` 给；2026-10-03 从 `` 搬进 `kards-agent/tools/`。）

一次只 attach 一次；不写任何自动打对局的循环逻辑；每条命令后打印
precheck.healthy()，不健康就明说。
"""
import os
import sys
import time
import traceback

import _bootstrap  # noqa: F401,E402  —— 接好仓库根

from base import paths as P  # noqa: E402

P.ensure_dirs()
CMD = P.LIVE_CMD
LOG = P.LIVE_LOG


def main():
    logf = open(LOG, "a", encoding="utf-8")

    def out(*a):
        line = " ".join(str(x) for x in a)
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    out("======== live_session start %s ========" % time.strftime("%H:%M:%S"))
    from agent import precheck
    from agent.session import AgentSession
    from kardsmem import board as BA

    sess = AgentSession()
    ok = precheck.available()
    out("precheck.available() = %s  (err=%s)" % (ok, precheck.status().get("last_error")))
    # ★ 2026-10-02（A7b）：`GetGameInstanceSubsystem` 靠"类→实例"索引，建表要全扫一遍
    #   对象池（只读类指针，实测 **5.4 s**）。放在**启动时**建，别让它落在决策热路径上
    #   （求值链预算 0.6 s）。失败不影响主流程（索引会在首次用到时再建）。
    if ok:
        try:
            from kardsmem import subsystems as _subs
            _t0 = time.time()
            _idx = _subs.build_index(sess._kardsmem(), force=True)
            out("warm-up: subsystem 索引 %d 项，%.1fs" % (len(_idx), time.time() - _t0))
        except Exception as _exc:                              # noqa: BLE001
            out("warm-up 跳过：%s: %s" % (type(_exc).__name__, _exc))

    ns = {"sess": sess, "precheck": precheck, "BA": BA, "time": time}

    if os.path.exists(CMD):
        os.remove(CMD)
    open(CMD, "w", encoding="utf-8").close()
    out("命令通道就绪（之后追加到 live_cmd.txt 的命令才会被执行；此前写入的会被清掉）")
    done = 0
    while True:
        try:
            with open(CMD, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        except FileNotFoundError:
            lines = []
        while done < len(lines):
            cmd = lines[done]
            done += 1
            out(">>> %s" % cmd)
            if cmd.strip() == "quit":
                out("======== live_session quit ========")
                return
            try:
                try:
                    result = eval(cmd, ns)
                    out("<<<", repr(result))
                except SyntaxError:
                    exec(cmd, ns)
                    out("<<< (exec ok)")
            except Exception:                                      # noqa: BLE001
                out("!! exception:", traceback.format_exc(limit=6))
            try:
                if not precheck.healthy():
                    out("** 注入侧不健康: %s" % precheck.unhealth_reason())
            except Exception:                                      # noqa: BLE001
                pass
        time.sleep(0.3)


if __name__ == "__main__":
    main()
