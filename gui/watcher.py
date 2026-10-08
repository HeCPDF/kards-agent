# -*- coding: utf-8 -*-
"""常驻监听器（`tools/live_session.py`）的查找 / 优雅退出 / 强杀。

优雅退出 = 往 `live_cmd.txt` 追加 `quit`（监听器逐条读命令，读到就正常返回）。
但它**一次只处理一条**：正在跑 `player.play.play` 时要等这局收手，所以先发 `abort_now`。
强杀只在优雅退出超时后、由用户确认才做——进程被强行结束会卸载 frida agent，实机出过游戏崩溃。
"""
from __future__ import annotations

import os
import subprocess
import time

from . import autoplay as AP
from . import control as C

# 窗口子系统的 exe（打包版面板）每次轮询都起 powershell：不加 CREATE_NO_WINDOW 会闪黑窗并抢游戏焦点。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 两种监听器命令行：开发布局 `python …\live_session.py`；冻结包 `kards-agent.exe --listener`。
# 正则写成 `[.]` / `[-]-` 的形式，是为了让 PowerShell 自己的命令行（含这段正则原文）匹配不上自己。
PS_QUERY = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'live_session[.]py|[-]-listener( |$)' } "
            "| Select-Object -ExpandProperty ProcessId")


def pids() -> list:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_QUERY],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except Exception:                                         # noqa: BLE001
        return []


def request_quit(cmd_path: str = AP.LIVE_CMD) -> None:
    """让当前局收手并让监听器退出：`abort_now`+`run=False`，再追加 `quit`。"""
    C.update_control(abort_now=True, run=False)
    with open(cmd_path, "a", encoding="utf-8") as f:
        f.write("quit\n")
    C.log_event("面板：请求监听器退出（abort_now + quit）")


def wait_exit(timeout_s: float = 90.0, poll_s: float = 1.0, pids_fn=pids, sleep=time.sleep,
              clock=time.time) -> bool:
    """等监听器进程消失；超时返回 False。"""
    end = clock() + timeout_s
    while clock() < end:
        if not pids_fn():
            return True
        sleep(poll_s)
    return not pids_fn()


def force_kill(pids_fn=pids, killer=None) -> list:
    """强杀所有监听器进程（`taskkill /F`），返回被杀的 pid。**仅在用户确认后调用。**"""
    killed = []
    for p in pids_fn():
        if killer is not None:
            killer(p)
        else:
            subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW)
        killed.append(p)
    if killed:
        C.log_event("面板：强杀监听器 %s" % killed)
    return killed


def launch_state(log_path: str, offset: int) -> str:
    """看 `live_log.txt` 从 `offset` 起的新内容：`ready`（attach 成功）/ `failed`（precheck 不可用）/ `pending`。
    live_session 启动时会打印 `precheck.available() = True|False`。"""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new = f.read()
    except OSError:
        return "pending"
    if "precheck.available() = True" in new:
        # ★ 2026-10-03：attach 成功 ≠ 能收命令。监听器预热完才会清空并重建命令通道，期间写进 live_cmd.txt 的命令会丢
        #   （实机丢过一次“开始”）。所以要等它打印“命令通道就绪”才算 ready。
        return "ready" if "命令通道就绪" in new else "pending"
    if "precheck.available() = False" in new:
        return "failed"
    return "pending"


# ---------------------------------------------------------------- 运行中游戏的版本（只作身份/显示）
def detect_version(pids_fn=None, read_fn=None):
    """从**运行中游戏进程内存**读 `版本号.分支`（只读）。→ `{"version", "build", "source", "pid", "why"}`。

    ★ 2026-10-03 P7：版本号**不是放行条件**。`build` = 种子表里登记过这个版本的键（没登记 ⇒ None，只表示“不在种子表”）；
    `source` = RVA 预期来源 `seed|cache|scan`（只看文件，不碰进程：种子表登记过 ⇒ seed；用户缓存里有 ⇒ cache；
    否则首次 attach 会扫描一次并写缓存）。真正的复验/扫描在监听器 attach 时做（`kardsmem.build.resolve`）。
    没有游戏/多个进程/认不出版本 ⇒ version=None + 原因。不看 exe 大小，也不看安装树（见 `kardsmem/version.py`）。"""
    from kardsmem import version as V, build as B
    pids_fn = pids_fn or V.game_pids
    read_fn = read_fn or V.read_game_version
    out = {"version": None, "build": None, "source": None, "pid": None, "why": ""}
    pids_ = pids_fn()
    if not pids_:
        out["why"] = "没有找到 kards-Win64-Shipping 进程（游戏开了吗？）"
        return out
    if len(pids_) > 1:
        out["why"] = "同时有多个游戏进程：%s（不猜）" % pids_
        return out
    out["pid"] = pids_[0]
    gv = read_fn(pids_[0])
    out["version"] = gv["version"]
    if gv["version"] is None:
        out["why"] = gv["why"]
        return out
    b = B.seed_for_display(gv["version"])
    out["build"] = b
    if b:
        out["source"] = "seed"
        out["why"] = "进程内存里的版本串 %s（%s）→ 种子表 %s（attach 时仍会对进程复验）" % (gv["version"], gv["why"], b)
    else:
        out["source"] = "cache" if _has_rva_cache(gv["version"]) else "scan"
        out["why"] = ("进程内存里的版本串 %s（%s）；种子表没有这个版本，%s" % (
            gv["version"], gv["why"], "用户缓存里有" if out["source"] == "cache" else "首次 attach 会扫描一次 RVA（只读，约十几秒）并写缓存"))
    return out


def _has_rva_cache(version) -> bool:
    try:
        from kardsmem import rvascan
        return os.path.exists(rvascan.cache_path(version))
    except Exception:                                         # noqa: BLE001
        return False


def describe(info: dict) -> str:
    if info.get("version") and info.get("build"):
        return "游戏版本 %s（种子表 %s）" % (info["version"], info["build"])
    if info.get("version"):
        return "游戏版本 %s；%s" % (info["version"], info.get("why"))
    return info.get("why") or "未识别"


def rva_cache_text(version=None) -> str:
    """RVA 缓存状态（`kardsmem.rvascan` 的用户缓存，按 版本.分支 一个文件）：给面板“诊断”行用，纯文件检查。"""
    try:
        from kardsmem import rvascan
        names = sorted(os.listdir(rvascan.CACHE_DIR)) if os.path.isdir(rvascan.CACHE_DIR) else []
        if version:
            ok = any(n.startswith(version) for n in names)
            return "RVA 缓存：%s（%s）" % ("有" if ok else "无，下次 attach 会扫描一次（约 20 s）", version)
        return "RVA 缓存：%d 个版本" % len(names)
    except Exception:                                         # noqa: BLE001
        return "RVA 缓存：读不出"
