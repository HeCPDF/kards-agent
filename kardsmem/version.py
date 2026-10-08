# -*- coding: utf-8 -*-
"""kardsmem.version —— 用**运行中游戏自报的 `版本号.分支`** 选构建（偏移表 / RVA）。

为什么：已知的每个 `版本号.分支`（如 `1.60.27292.launcher`）与它的 RVA 表一一对应（用户 2026-10-03 定）。
以前靠 `SizeOfImage` 间接推构建、再靠环境变量 `KARDS_BUILD` 选表；现在直接问游戏"你是哪个版本"。

怎么问：游戏启动时引擎已把 pak 里 `DefaultGame.ini` 的 `ProjectVersion=Kards <版本号>.<分支>`
读进内存（IDA：`UKardsGameInstance2::GetProjectVersion` / `UFunctionLibrary::GetProjectVersion`
都是 `GConfig->GetString("/Script/EngineSettings.GeneralProjectSettings","ProjectVersion",…)`）。
只读 `VirtualQueryEx`+`ReadProcessMemory` 扫这个形状（UTF-16 与 ASCII），≥2 处且只有一个不同的值才采信。

版本号**只当身份**：RVA 缓存键、日志、面板显示；它**不是放行条件**（2026-10-03 P7：未登记的版本也能起来，
走 用户缓存 → 种子表复验 → 运行时扫描，见 `kardsmem/build.py::resolve`）。`KARDS_BUILD` 仅作调试覆盖。

本模块**不依赖** `build.py` / `kardsmem/board.py`（它们 import 本模块），只读。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import time

GAME_EXE = "kards-Win64-Shipping.exe"
from base import paths as _paths_v
CACHE = _paths_v.VERSION_CACHE

SHAPE16 = re.compile(rb"K\x00a\x00r\x00d\x00s\x00 \x00((?:[0-9]\x00){1,2}\.\x00(?:[0-9]\x00){1,3}\.\x00"
                     rb"(?:[0-9]\x00){3,6}\.\x00(?:[A-Za-z0-9_]\x00){3,16})(?![A-Za-z0-9_]\x00)")
SHAPE8 = re.compile(rb"Kards ((?:[0-9]{1,2})\.(?:[0-9]{1,3})\.(?:[0-9]{3,6})\.(?:[A-Za-z0-9_]{3,16}))(?![A-Za-z0-9_])")
NEEDLE16 = "Kards ".encode("utf-16-le") + b"1\x00"           # 版本号第一段目前是 1.x
NEEDLE8 = b"Kards 1."


def scan_chunk(data: bytes) -> list:
    """一块内存里所有 `Kards a.b.c.分支` → 去前缀后的字符串列表（UTF-16 与 ASCII 各自校验）。"""
    out = []
    for needle, shape, wide in ((NEEDLE16, SHAPE16, True), (NEEDLE8, SHAPE8, False)):
        i = data.find(needle)
        while i != -1:
            m = shape.match(data[i:i + (64 if wide else 40)])
            if m:
                g = m.group(1)
                out.append(g.decode("utf-16-le") if wide else g.decode("ascii"))
            i = data.find(needle, i + 1)
    return out


def _iter_process_chunks(pid: int, chunk: int = 16 << 20):
    """只读遍历进程已提交、可读的内存（非 guard/noaccess）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]
    k32.OpenProcess.restype = wt.HANDLE
    k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    h = k32.OpenProcess(0x400 | 0x10, False, pid)
    if not h:
        raise OSError("OpenProcess(%d) 失败 err=%d" % (pid, ctypes.get_last_error()))
    try:
        mbi, addr = MBI(), 0
        buf = ctypes.create_string_buffer(chunk + 64)
        while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if mbi.State == 0x1000 and not (mbi.Protect & 0x101) and (mbi.Protect & 0xFE):
                off = 0
                while off < size:
                    n = min(size - off, chunk)
                    got = ctypes.c_size_t(0)
                    want = min(n + 64, size - off)               # 多读 64 字节：跨块边界的串不丢
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, want, ctypes.byref(got)) and got.value:
                        yield buf.raw[:got.value]
                    off += n
            addr = base + size
            if addr >= 0x7FFFFFFF0000:
                break
    finally:
        k32.CloseHandle(h)


def read_game_version(pid: int, need_hits: int = 2, max_seconds: float = 120.0, reader=None) -> dict:
    """→ `{"version"|None, "hits", "why", "seconds"}`。≥`need_hits` 处且只有一个不同值才采信（多值/不足 ⇒ None+原因）。"""
    t0, hits, why = time.time(), {}, ""
    try:
        for data in (reader(pid) if reader is not None else _iter_process_chunks(pid)):
            for s in scan_chunk(data):
                hits[s] = hits.get(s, 0) + 1
            if sum(hits.values()) >= need_hits and len(hits) == 1:
                break
            if time.time() - t0 > max_seconds:
                why = "扫描超时（%.0f s）" % max_seconds
                break
    except Exception as exc:                                      # noqa: BLE001
        why = "读进程内存失败：%s: %s" % (type(exc).__name__, exc)
    total = sum(hits.values())
    if len(hits) == 1 and total >= need_hits:
        return {"version": next(iter(hits)), "hits": hits, "why": "内存里 %d 处一致" % total,
                "seconds": time.time() - t0}
    if len(hits) > 1:
        why = why or "内存里出现多个不同的版本串：%s" % hits
    elif total:
        why = why or "只找到 %d 处（需要 ≥%d 处一致才采信）" % (total, need_hits)
    else:
        why = why or "内存里没找到 `Kards 数字.数字.数字.分支` 形状的串"
    return {"version": None, "hits": hits, "why": why, "seconds": time.time() - t0}


# ---------------------------------------------------------------- 找进程
class _PE32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def game_pids(exe: str = GAME_EXE) -> list:
    """按**进程名**找游戏主进程（不看 SizeOfImage）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    if snap in (None, -1, ctypes.c_void_p(-1).value):
        return out
    try:
        e = _PE32()
        e.dwSize = ctypes.sizeof(_PE32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("mbcs", "replace").lower() == exe.lower():
                out.append(e.th32ProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _start_time(pid: int):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)                     # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def _cache_load():
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_save(d):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def running_version(use_cache: bool = True, pids_fn=game_pids, read_fn=read_game_version, start_fn=_start_time):
    """运行中游戏的 `版本号.分支`；没有游戏/多个进程/认不出 ⇒ None。
    缓存键 = (pid, 进程创建时间)：同一个进程只扫一次内存，进程重启自动失效。"""
    pids = pids_fn()
    if len(pids) != 1:
        return None
    pid = pids[0]
    key = "%s:%s" % (pid, start_fn(pid))
    if use_cache:
        c = _cache_load()
        if c.get("key") == key and c.get("version"):
            return c["version"]
    v = read_fn(pid).get("version")
    if v and use_cache:
        _cache_save({"key": key, "version": v})
    return v


def version_of_pid(pid: int, use_cache: bool = True, read_fn=read_game_version, start_fn=_start_time):
    """指定进程的 `版本号.分支`（缓存键 = (pid, 创建时间)）；认不出 ⇒ None。"""
    key = "%s:%s" % (pid, start_fn(pid))
    if use_cache:
        c = _cache_load()
        if c.get("key") == key and c.get("version"):
            return c["version"]
    v = read_fn(pid).get("version")
    if v and use_cache:
        _cache_save({"key": key, "version": v})
    return v


# ---------------------------------------------------------------- 主模块（镜像大小 / 基址）
class _ME32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD), ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


def game_module(pid: int, exe: str = GAME_EXE):
    """进程主模块 → `(基址, SizeOfImage, 路径)`；找不到 ⇒ None。只读（Toolhelp 快照）。

    放在这个无依赖的叶子模块里：`build.py` 在 import 时就要知道镜像大小，而 `board.py`/`proc.py` 反过来要 import
    `build.py`，不能让 `build.py` 顶层去 import 它们（会成环）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_ME32W)]
    k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ME32W)]
    snap = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if snap in (None, -1, ctypes.c_void_p(-1).value):
        return None
    try:
        e = _ME32W()
        e.dwSize = ctypes.sizeof(_ME32W)
        ok = k32.Module32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szModule.lower() == exe.lower():
                return (e.modBaseAddr or 0, e.modBaseSize, e.szExePath)
            ok = k32.Module32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return None
