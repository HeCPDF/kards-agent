# -*- coding: utf-8 -*-
"""crashdump 的底座：常量 / ctypes 结构体 / 进程与模块枚举 / minidump 写入 / 白名单 / status 发布。
从 crashdump.py 原样搬出（P6 拆文件，零逻辑改动）；设计说明见 crashdump.py 的模块文档。
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

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
try:                                    # 跟 tools/ 其它脚本一致；本脚本不依赖它
    import _bootstrap  # noqa: F401
except Exception:                       # noqa: BLE001
    pass

# --------------------------------------------------------------------------- 常量
DEFAULT_IMAGE = "kards-Win64-Shipping.exe"
DEFAULT_OUT_DIR = os.path.join(__import__("base.paths", fromlist=["DATA"]).DATA, "crashdumps")
KITS = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64"
CDB = os.path.join(KITS, "cdb.exe")
# ★ 显式用调试器目录里那份新版 dbghelp.dll（System32 那份是旧版，功能少）。
#   同目录的依赖（symsrv 等）会按 DLL 自身目录解析，所以全路径加载是安全的。
DBGHELP = os.path.join(KITS, "dbghelp.dll")
DUMPCHK = os.path.join(KITS, "dumpchk.exe")
SYSINTERNALS = r"H:\Tool_Collections\Folder\SysinternalsSuite"
PROCDUMP64 = os.path.join(SYSINTERNALS, "procdump64.exe")
VMMAP64 = os.path.join(SYSINTERNALS, "vmmap64.exe")
DEFAULT_RVAS = (0x11D37E6, 0x11AD577)
MIN_FREE_BYTES = 10 * 1024 ** 3         # 写之前要求目标盘至少 10GB 空闲

# MiniDumpType：需求点名的五个，外加 ProcessThreadData（PEB/TEB，`!analyze`/`~*` 要用）
MINIDUMP_WITH_DATA_SEGS = 0x00000001
MINIDUMP_WITH_FULL_MEMORY = 0x00000002
MINIDUMP_WITH_HANDLE_DATA = 0x00000004
MINIDUMP_WITH_UNLOADED_MODULES = 0x00000020
MINIDUMP_WITH_PROCESS_THREAD_DATA = 0x00000100
MINIDUMP_WITH_FULL_MEMORY_INFO = 0x00000800
MINIDUMP_WITH_THREAD_INFO = 0x00001000
DUMP_FLAGS = (MINIDUMP_WITH_FULL_MEMORY | MINIDUMP_WITH_HANDLE_DATA
              | MINIDUMP_WITH_UNLOADED_MODULES | MINIDUMP_WITH_FULL_MEMORY_INFO
              | MINIDUMP_WITH_THREAD_INFO | MINIDUMP_WITH_PROCESS_THREAD_DATA)

# minidump 流类型（我们自己解析用；只取要用的几个）
MD_STREAM_THREAD_LIST = 3
MD_STREAM_MODULE_LIST = 4
MD_STREAM_MEMORY_LIST = 5               # 小 dump（MiniDumpNormal）用这个
MD_STREAM_EXCEPTION = 6
MD_STREAM_MEMORY64 = 9
MD_STREAM_MISC_INFO = 15
MD_STREAM_MEMORY_INFO_LIST = 16
MD_STREAM_UNLOADED_MODULE_LIST = 14

EXCEPTION_ACCESS_VIOLATION = 0xC0000005
EXCEPTION_BREAKPOINT = 0x80000003
STATUS_TIMEOUT = 0x00000102             # STILL_ACTIVE 的邻居：WaitForDebugEvent 超时

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001

DEBUG_EVENT_CODES = {1: "EXCEPTION", 2: "CREATE_THREAD", 3: "CREATE_PROCESS",
                     4: "EXIT_THREAD", 5: "EXIT_PROCESS", 6: "LOAD_DLL",
                     7: "UNLOAD_DLL", 8: "OUTPUT_DEBUG_STRING", 9: "RIP"}
EVENT_EXCEPTION = 1
EVENT_CREATE_PROCESS = 3
EVENT_EXIT_PROCESS = 5

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_LIMITED = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
THREAD_ALL_ACCESS = 0x1FFFFF
THREAD_GET_CONTEXT = 0x0008
THREAD_QUERY_INFORMATION = 0x0040
CONTEXT_AMD64 = 0x00100000
CONTEXT_ALL = CONTEXT_AMD64 | 0x1F

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_PARTIAL_COPY = 299

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wt.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


class EXCEPTION_RECORD(ctypes.Structure):
    """winnt.h 的 EXCEPTION_RECORD（**不是** 4 字节打包的 MINIDUMP_EXCEPTION_RECORD）。

    x64 上自然对齐 = 152 字节，正好也是 DEBUG_EVENT 里内联那一块的布局。
    """
    _fields_ = [
        ("ExceptionCode", wt.DWORD),
        ("ExceptionFlags", wt.DWORD),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wt.DWORD),
        ("ExceptionInformation", ctypes.c_ulonglong * 15),
    ]


class M128A(ctypes.Structure):
    _fields_ = [("Low", ctypes.c_ulonglong), ("High", ctypes.c_longlong)]


class XMM_SAVE_AREA32(ctypes.Structure):
    _fields_ = [
        ("ControlWord", wt.WORD), ("StatusWord", wt.WORD), ("TagWord", wt.BYTE),
        ("Reserved1", wt.BYTE), ("ErrorOpcode", wt.WORD), ("ErrorOffset", wt.DWORD),
        ("ErrorSelector", wt.WORD), ("Reserved2", wt.WORD), ("DataOffset", wt.DWORD),
        ("DataSelector", wt.WORD), ("Reserved3", wt.WORD), ("MxCsr", wt.DWORD),
        ("MxCsr_Mask", wt.DWORD), ("FloatRegisters", M128A * 8),
        ("XmmRegisters", M128A * 16), ("Reserved4", wt.BYTE * 96),
    ]


class CONTEXT(ctypes.Structure):
    """x64 CONTEXT，sizeof == 0x4D0。字段顺序照 winnt.h，偏移是本脚本的硬契约。"""
    _fields_ = [
        ("P1Home", ctypes.c_ulonglong), ("P2Home", ctypes.c_ulonglong),
        ("P3Home", ctypes.c_ulonglong), ("P4Home", ctypes.c_ulonglong),
        ("P5Home", ctypes.c_ulonglong), ("P6Home", ctypes.c_ulonglong),
        ("ContextFlags", wt.DWORD), ("MxCsr", wt.DWORD),
        ("SegCs", wt.WORD), ("SegDs", wt.WORD), ("SegEs", wt.WORD), ("SegFs", wt.WORD),
        ("SegGs", wt.WORD), ("SegSs", wt.WORD), ("EFlags", wt.DWORD),
        ("Dr0", ctypes.c_ulonglong), ("Dr1", ctypes.c_ulonglong),
        ("Dr2", ctypes.c_ulonglong), ("Dr3", ctypes.c_ulonglong),
        ("Dr6", ctypes.c_ulonglong), ("Dr7", ctypes.c_ulonglong),
        ("Rax", ctypes.c_ulonglong), ("Rcx", ctypes.c_ulonglong),
        ("Rdx", ctypes.c_ulonglong), ("Rbx", ctypes.c_ulonglong),
        ("Rsp", ctypes.c_ulonglong), ("Rbp", ctypes.c_ulonglong),
        ("Rsi", ctypes.c_ulonglong), ("Rdi", ctypes.c_ulonglong),
        ("R8", ctypes.c_ulonglong), ("R9", ctypes.c_ulonglong),
        ("R10", ctypes.c_ulonglong), ("R11", ctypes.c_ulonglong),
        ("R12", ctypes.c_ulonglong), ("R13", ctypes.c_ulonglong),
        ("R14", ctypes.c_ulonglong), ("R15", ctypes.c_ulonglong),
        ("Rip", ctypes.c_ulonglong),
        ("FltSave", XMM_SAVE_AREA32),
        ("VectorRegister", M128A * 26),
        ("VectorControl", ctypes.c_ulonglong),
        ("DebugControl", ctypes.c_ulonglong),
        ("LastBranchToRip", ctypes.c_ulonglong),
        ("LastBranchFromRip", ctypes.c_ulonglong),
        ("LastExceptionToRip", ctypes.c_ulonglong),
        ("LastExceptionFromRip", ctypes.c_ulonglong),
    ]


class EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.POINTER(CONTEXT))]


class MINIDUMP_EXCEPTION_INFORMATION(ctypes.Structure):
    """★ `_pack_ = 4` 不能删！minidumpapiset.h 全部包在 pshpack4 里，x64 上：
    ThreadId@0（4B）、ExceptionPointers@4（8B）、ClientPointers@12（4B），共 16B。
    不加 pack 是 24B，dbghelp 会读偏移 4 那几个填充字节当指针 ⇒
    MiniDumpWriteDump 立刻失败（实测 ERROR_INVALID_HANDLE）。
    """
    _pack_ = 4
    _fields_ = [("ThreadId", wt.DWORD),
                ("ExceptionPointers", ctypes.POINTER(EXCEPTION_POINTERS)),
                ("ClientPointers", wt.BOOL)]


class DEBUG_EVENT(ctypes.Structure):
    """x64 上 DEBUG_EVENT = 16 字节头 + 160 字节联合体。联合体按字节收，
    需要哪个字段就 struct.unpack_from（省得为 9 个联合成员各写一遍结构）。"""
    _fields_ = [("dwDebugEventCode", wt.DWORD), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD), ("_pad", wt.DWORD),
                ("_union", ctypes.c_byte * 176)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


_PROTOS = [
    (k32.DebugActiveProcess, [wt.DWORD], wt.BOOL),
    (k32.DebugActiveProcessStop, [wt.DWORD], wt.BOOL),
    (k32.DebugSetProcessKillOnExit, [wt.BOOL], wt.BOOL),
    (k32.WaitForDebugEvent, [ctypes.POINTER(DEBUG_EVENT), wt.DWORD], wt.BOOL),
    (k32.ContinueDebugEvent, [wt.DWORD, wt.DWORD, wt.DWORD], wt.BOOL),
    (k32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
    (k32.OpenThread, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
    (k32.CloseHandle, [wt.HANDLE], wt.BOOL),
    (k32.GetThreadContext, [wt.HANDLE, ctypes.POINTER(CONTEXT)], wt.BOOL),
    (k32.ReadProcessMemory, [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wt.BOOL),
    (k32.TerminateProcess, [wt.HANDLE, wt.UINT], wt.BOOL),
    (k32.CreateToolhelp32Snapshot, [wt.DWORD, wt.DWORD], wt.HANDLE),
    (k32.Process32First, [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)], wt.BOOL),
    (k32.Process32Next, [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)], wt.BOOL),
    (k32.Thread32First, [wt.HANDLE, ctypes.POINTER(THREADENTRY32)], wt.BOOL),
    (k32.Thread32Next, [wt.HANDLE, ctypes.POINTER(THREADENTRY32)], wt.BOOL),
    (k32.Module32FirstW, [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wt.BOOL),
    (k32.Module32NextW, [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wt.BOOL),
    (k32.GetProcessTimes, [wt.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                           ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)], wt.BOOL),
    (k32.IsWow64Process, [wt.HANDLE, ctypes.POINTER(wt.BOOL)], wt.BOOL),
    (k32.QueryFullProcessImageNameW, [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                      ctypes.POINTER(wt.DWORD)], wt.BOOL),
    (k32.OpenProcessToken, [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)], wt.BOOL),
    (k32.GetCurrentProcess, [], wt.HANDLE),
    (advapi32.AdjustTokenPrivileges, [wt.HANDLE, wt.BOOL, ctypes.c_void_p, wt.DWORD,
                                      ctypes.c_void_p, ctypes.c_void_p], wt.BOOL),
    (advapi32.LookupPrivilegeValueW, [wt.LPCWSTR, wt.LPCWSTR,
                                      ctypes.POINTER(_LUID)], wt.BOOL),
]
for _fn, _args, _res in _PROTOS:
    _fn.argtypes = _args
    _fn.restype = _res

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_DUMP_LOCK = threading.Lock()           # 一个进程内同时只准一份 dump 在写
_OPEN_ON_TIMEOUT = []                   # 超时未收尾的 dump 文件对象（见 _run_dump 注释）
_ELEV = {"checked": False, "debug_privilege": False, "admin": False}
NAME_MAX = 120
_WINDOWS_EPOCH = 11644473600.0          # 1601-01-01 → 1970-01-01 的秒数


# --------------------------------------------------------------------------- 小工具
def log(msg: str, *, quiet: bool = False) -> None:
    """进度一律走 stderr：stdout 只留给 `--json` 那一行。"""
    if not quiet:
        sys.stderr.write("[crashdump] %s\n" % msg)
        sys.stderr.flush()


def _hex(v) -> str:
    return "0x%x" % v if isinstance(v, int) else str(v)


def _filetime_to_unix(ft: FILETIME) -> float:
    v = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return v / 1e7 - _WINDOWS_EPOCH


def enable_debug_privilege() -> bool:
    """把 SeDebugPrivilege 打开（管理员才拿得到）。

    同一用户、同一完整性级别的进程其实**不需要**它就能 DebugActiveProcess（实测过），
    但游戏要是以管理员/更高完整性跑的，没有这个特权就只能吃 ERROR_ACCESS_DENIED。
    返回是否真的开成功（没成功不算错，后面 attach 失败再报权限）。
    """
    token = wt.HANDLE()
    try:
        if not k32.OpenProcessToken(k32.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(token)):
            _ELEV["checked"] = True
            return False
        _ELEV["admin"] = _token_is_elevated(token)
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                              ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        # 两者都在 advapi32（曾经写成 kernel32 → AttributeError 把监视线程炸掉过）
        advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        ok = ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED
        _ELEV["debug_privilege"] = ok
        _ELEV["checked"] = True
        return ok
    except Exception:                   # noqa: BLE001 —— 拿不到特权不是致命错误
        _ELEV["checked"] = True
        return False
    finally:
        if token:
            k32.CloseHandle(token)


def _token_is_elevated(token) -> bool:
    """TokenElevation（TokenInformationClass = 20）：进程是不是提权运行的。

    status.json 里写 `elevated` 就是靠它 —— 提权跑的时候用户得知道。
    """
    try:
        val = wt.DWORD(0)
        need = wt.DWORD(0)
        k32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wt.DWORD, ctypes.POINTER(wt.DWORD)]
        if k32.GetTokenInformation(token, 20, ctypes.byref(val),
                                   ctypes.sizeof(val), ctypes.byref(need)):
            return bool(val.value)
    except Exception:                   # noqa: BLE001
        pass
    return False


def elevation_state() -> dict:
    if not _ELEV["checked"]:
        enable_debug_privilege()
    return {"elevated": bool(_ELEV["admin"] or _ELEV["debug_privilege"]),
            "admin": bool(_ELEV["admin"]),
            "debug_privilege": bool(_ELEV["debug_privilege"])}


def elevated_hint(cmd: str = "watch") -> str:
    """没权限时给一条能直接粘的提权命令（需求 §6：不许静默失败）。

    优先给 `sudo.exe --inline`（Windows 11 自带；**每次都会弹 UAC，要人在屏幕前点**），
    再附一条 RunAs 的等价写法。本工具**不会自己提权** —— UAC 弹框必须有人点。
    """
    script = os.path.join(TOOLS_DIR, "crashdump.py")        # P6：本函数搬出后 __file__ 不再是 crashdump.py
    inner = 'python "%s" %s' % (script, cmd)
    return ('sudo --inline %s\n'
            '    （或） Start-Process powershell -Verb RunAs -ArgumentList '
            '\'-NoExit\',\'-Command\',\'%s\'' % (inner, inner))


def list_pids(image_name: str | None = None) -> list[dict]:
    """按 exe 名（大小写不敏感、允许子串）列进程；image_name=None 表示全部。"""
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(pe))
        while ok:
            name = pe.szExeFile.decode("mbcs", "replace")
            if image_name is None or image_name.lower() in name.lower():
                out.append({"pid": pe.th32ProcessID, "exe": name,
                            "threads": pe.cntThreads})
            ok = k32.Process32Next(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def process_info(pid: int) -> dict:
    """镜像路径 / 启动时间 / 运行时长 / 是否 WOW64。权限不够时字段为 None。"""
    info = {"pid": pid, "image": None, "started": None, "uptime_s": None,
            "wow64": None, "alive": False}
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return info
    try:
        info["alive"] = True
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            info["image"] = buf.value
        ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                               ctypes.byref(kt), ctypes.byref(ut)):
            info["started"] = _filetime_to_unix(ct)
            info["uptime_s"] = max(0.0, time.time() - info["started"])
        w = wt.BOOL()
        if k32.IsWow64Process(h, ctypes.byref(w)):
            info["wow64"] = bool(w.value)
    finally:
        k32.CloseHandle(h)
    return info


def is_wow64(pid: int) -> bool | None:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        w = wt.BOOL()
        return bool(w.value) if k32.IsWow64Process(h, ctypes.byref(w)) else None
    finally:
        k32.CloseHandle(h)


_DEBUGQ_LAST_ERROR: dict = {}


def is_debugged(pid: int) -> bool | None:
    """这个进程现在是不是**已经有人**在调试（ProcessDebugObjectHandle != 0）。

    ProcDump 引擎靠它做预检：目标已被别的调试器/ProcDump 占着时，ProcDump 只会立刻
    退出（`The process is already being debugged.`），那就不要在 2 秒循环里反复起它。
    → True/False；查不到（权限不够）返回 None，调用方按"不知道"处理。
    """
    # ★ 访问掩码必须带 PROCESS_QUERY_INFORMATION：只给 QUERY_LIMITED(0x1000) 时
    #   NtQueryInformationProcess(ProcessDebugObjectHandle) 返回 0xC0000002
    #   (STATUS_NOT_IMPLEMENTED)，函数就一直返回 None、预检等于没做（实测踩过）。
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        # ★ 这里**不要**给 NtQueryInformationProcess 设 argtypes：设了以后传 byref()
        #   会走 ctypes 的转换检查、直接抛异常，而异常被下面的 except 吞掉 →
        #   函数永远返回 None（实测踩过，预检等于没做）。保持原型不设、自己 cast。
        out = ctypes.c_void_p(0)
        st = ntdll.NtQueryInformationProcess(wt.HANDLE(h), 30,
                                             ctypes.cast(ctypes.byref(out),
                                                         ctypes.c_void_p),
                                             ctypes.sizeof(out), None)
        # 没被调试时内核返回 STATUS_PORT_NOT_SET(0xC0000353) —— 那是**正常答案**，
        # 不是错误（第一版只认 st==0，结果"没人在调试"和"查不到"都变成 None，
        # 预检等于没做）。
        if st == 0:
            return bool(out.value)
        if (st & 0xFFFFFFFF) == 0xC0000353:
            return False
        _DEBUGQ_LAST_ERROR["status"] = st & 0xFFFFFFFF
        return None
    except Exception as exc:            # noqa: BLE001
        _DEBUGQ_LAST_ERROR["exception"] = "%s: %s" % (type(exc).__name__, exc)
        return None
    finally:
        k32.CloseHandle(h)


def modules_of(pid: int) -> list[dict]:
    """→ [{base,size,name,path}]，按 base 升序。"""
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            out.append({"base": me.modBaseAddr or 0, "size": me.modBaseSize,
                        "name": me.szModule, "path": me.szExePath})
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    out.sort(key=lambda m: m["base"])
    return out


def main_module_of(modules: list[dict], image_name: str) -> dict | None:
    base = os.path.basename(image_name).lower()
    for m in modules:
        if m["name"].lower() == base:
            return m
    for m in modules:                    # 名称带后缀的变体（Shipping2.exe 之类）
        if base in m["name"].lower():
            return m
    return modules[0] if modules else None


def module_of(addr: int, modules: list[dict]) -> dict | None:
    lo, hi = 0, len(modules) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        m = modules[mid]
        if addr < m["base"]:
            hi = mid - 1
        elif m["size"] and addr >= m["base"] + m["size"]:
            lo = mid + 1
        else:
            if m["size"] == 0 and addr != m["base"]:
                lo = mid + 1
                continue
            return m
    return None


def thread_rips(pid: int) -> list[dict]:
    """所有线程的 tid + 当前 RIP（GetThreadContext，只读）。

    进程在调试事件里冻住时读到的是**那一刻**的一致快照；进程跑着时读到的是就近值
    （线程可能在两行之间就变了，当参考不当判据）。
    """
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                rec = {"tid": te.th32ThreadID, "rip": None, "rsp": None, "rbp": None,
                       "error": None}
                h = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False,
                                   te.th32ThreadID)
                if not h:
                    rec["error"] = "OpenThread err=%d" % ctypes.get_last_error()
                else:
                    try:
                        ctx = CONTEXT()
                        ctx.ContextFlags = CONTEXT_ALL
                        if k32.GetThreadContext(h, ctypes.byref(ctx)):
                            rec["rip"] = ctx.Rip
                            rec["rsp"] = ctx.Rsp
                            rec["rbp"] = ctx.Rbp
                            rec["rax"] = ctx.Rax
                        else:
                            rec["error"] = "GetThreadContext err=%d" % ctypes.get_last_error()
                    finally:
                        k32.CloseHandle(h)
                out.append(rec)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    out.sort(key=lambda t: t["tid"])
    return out


def free_bytes(path: str) -> int:
    probe = path
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    return shutil.disk_usage(probe or ".").free


def atomic_json(path: str, obj) -> None:
    """先写临时文件再 os.replace（Windows 上 os.replace 是原子的）——
    轮询方永远读不到半截文件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                   # noqa: BLE001
        return None


def context_to_dict(ctx: CONTEXT) -> dict:
    return {
        "rip": ctx.Rip, "rsp": ctx.Rsp, "rbp": ctx.Rbp,
        "rax": ctx.Rax, "rbx": ctx.Rbx, "rcx": ctx.Rcx, "rdx": ctx.Rdx,
        "rsi": ctx.Rsi, "rdi": ctx.Rdi,
        "r8": ctx.R8, "r9": ctx.R9, "r10": ctx.R10, "r11": ctx.R11,
        "r12": ctx.R12, "r13": ctx.R13, "r14": ctx.R14, "r15": ctx.R15,
        "eflags": ctx.EFlags, "cs": ctx.SegCs, "ss": ctx.SegSs,
    }


def sanitize(tag: str, maxlen: int = 32) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)[:maxlen].strip("_")


def _safe(fn, default):
    """跑一段"可选"的采集代码：失败就返回 default（并且不打断主流程）。"""
    try:
        return fn()
    except Exception:                   # noqa: BLE001
        return default


# --------------------------------------------------------------------------- dump
class DumpError(RuntimeError):
    """带人话的失败原因（权限、磁盘、dbghelp）。"""


_dbghelp = None


def _dbghelp_dll():
    global _dbghelp
    if _dbghelp is None:
        # 先试调试器目录里的新版（显式全路径），拿不到再退回系统那份。
        try:
            dll = ctypes.WinDLL(DBGHELP, use_last_error=True)
        except OSError:
            dll = ctypes.WinDLL("dbghelp.dll", use_last_error=True)
        dll.MiniDumpWriteDump.argtypes = [
            wt.HANDLE, wt.DWORD, wt.HANDLE, wt.DWORD,
            ctypes.POINTER(MINIDUMP_EXCEPTION_INFORMATION),
            ctypes.c_void_p, ctypes.c_void_p]
        dll.MiniDumpWriteDump.restype = wt.BOOL
        _dbghelp = dll
    return _dbghelp


def _open_target(pid: int) -> wt.HANDLE:
    """拿一个能读内存 + 能喂 dbghelp 的进程句柄。"""
    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            raise DumpError(
                "OpenProcess(PROCESS_ALL_ACCESS) 被拒（pid=%d）。要么本进程权限不够，"
                "要么目标以更高完整性在跑。用管理员身份重开：\n    %s"
                % (pid, elevated_hint("now")))
        raise DumpError("OpenProcess(pid=%d) 失败 err=%d" % (pid, err))
    return h


def _grab_context(tid: int) -> CONTEXT | None:
    """故障线程的 CONTEXT。调试事件把线程冻住时读到的就是异常现场。"""
    h = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None
    try:
        ctx = CONTEXT()
        ctx.ContextFlags = CONTEXT_ALL
        return ctx if k32.GetThreadContext(h, ctypes.byref(ctx)) else None
    finally:
        k32.CloseHandle(h)


def _build_exception_param(rec: EXCEPTION_RECORD, ctx: CONTEXT, tid: int):
    """拼一份**本进程地址空间里**的 EXCEPTION_POINTERS + MINIDUMP_EXCEPTION_INFORMATION。

    ClientPointers=FALSE 的意思是"指针在我（debugger）这边"，所以这两个对象必须是我们
    自己进程里的。DEBUG_EVENT 只给了内联的 EXCEPTION_RECORD，**没有**游戏侧
    EXCEPTION_POINTERS 的地址（那不是 SEH），所以走不了 ClientPointers=TRUE。
    返回值要一直活着到 MiniDumpWriteDump 返回。
    """
    ep = EXCEPTION_POINTERS(ctypes.pointer(rec), ctypes.pointer(ctx))
    mei = MINIDUMP_EXCEPTION_INFORMATION(tid, ctypes.pointer(ep), False)
    return mei, (rec, ctx, ep)


def _write_minidump(out_path: str, hproc, pid: int, mei, keepalive, box: dict) -> None:
    """真正调 dbghelp。跑在单独线程里（为什么见 _run_dump）。"""
    t0 = time.perf_counter()
    try:
        with open(out_path, "wb") as f:
            hfile = wt.HANDLE(msvcrt.get_osfhandle(f.fileno()))
            ctypes.set_last_error(0)
            ok = _dbghelp_dll().MiniDumpWriteDump(
                hproc, pid, hfile, DUMP_FLAGS,
                ctypes.byref(mei) if mei is not None else None, None, None)
            err = ctypes.get_last_error()
        box.update(ok=bool(ok),
                   error=None if ok else "MiniDumpWriteDump err=%d" % err,
                   seconds=time.perf_counter() - t0,
                   size=os.path.getsize(out_path) if os.path.exists(out_path) else 0)
    except Exception as exc:            # noqa: BLE001
        box.update(ok=False, error="%s: %s" % (type(exc).__name__, exc),
                   seconds=time.perf_counter() - t0, size=0)
    finally:
        del keepalive


def _run_dump(pid: int, out_path: str, mei, keepalive, timeout: float = 300.0) -> dict:
    """写一份 dump → {ok, path, size, seconds, timed_out, error}。

    为什么放单独线程 + 超时
    ----------------------
    写 dump 期间**游戏整个冻着**（调试事件挂起 = 全体线程暂停）。dbghelp 偶尔会因为
    目标里有卡死的 I/O 而长时间不返回；无人值守工具不能把游戏永久冻在调试器里。
    超时后调用方照常 ContinueDebugEvent 解冻游戏，并把这份 dump 记为不完整。
    代价：超时的那个写线程还卡在 dbghelp 里 ⇒ **那个文件对象故意不关**
    （关掉再让另一条线程往里写会写坏别的文件；宁可泄一个句柄）。
    """
    if not _DUMP_LOCK.acquire(timeout=timeout):
        return {"ok": False, "path": out_path, "size": 0, "seconds": 0.0,
                "timed_out": False, "error": "另一个 dump 还在写（本进程内串行）"}
    try:
        hproc = _open_target(pid)
        try:
            box: dict = {}
            th = threading.Thread(target=_write_minidump,
                                  args=(out_path, hproc, pid, mei, keepalive, box),
                                  name="crashdump-writer", daemon=True)
            t0 = time.perf_counter()
            th.start()
            th.join(timeout)
            if th.is_alive():
                _OPEN_ON_TIMEOUT.append(out_path)
                return {"ok": False, "path": out_path, "size": 0,
                        "seconds": time.perf_counter() - t0, "timed_out": True,
                        "error": "MiniDumpWriteDump 超过 %.0fs 没返回；已解冻目标，"
                                 "dump 不完整" % timeout}
            box.setdefault("timed_out", False)
            box.setdefault("path", out_path)
            box.setdefault("seconds", time.perf_counter() - t0)
            return box
        finally:
            k32.CloseHandle(hproc)
    finally:
        _DUMP_LOCK.release()


def _ensure_space(out_dir: str, need: int = MIN_FREE_BYTES) -> None:
    os.makedirs(out_dir, exist_ok=True)
    free = free_bytes(out_dir)
    if free < need:
        raise DumpError(
            "%s 只剩 %.1f GB 空闲（要求 ≥ %.1f GB）—— 拒绝写完整 dump，免得把盘写满。"
            "清一清旧 dump 再来。"
            % (out_dir, free / 2**30, need / 2**30))


def dump_name(pid: int, exc_code: int | None = None, fault_rva: int | None = None,
              tag: str = "", when: float | None = None) -> str:
    """文件名把三样东西都带上：时间戳 / 异常码 / 故障 RVA（需求原文）。"""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
    parts = ["kards"]
    if exc_code is None:
        parts += ["now", ts]
    else:
        parts += [ts, "c%08x" % exc_code]
    parts.append("p%d" % pid)
    if fault_rva is not None:
        parts.append("rva%x" % fault_rva)
    if tag:
        s = sanitize(tag)
        if s:
            parts.append(s)
    return "-".join(parts) + ".dmp"


def _sidecar_json(dump_path: str, payload: dict) -> str:
    path = os.path.splitext(dump_path)[0] + ".json"
    atomic_json(path, payload)
    return path


def _fault_site(rip: int, modules: list[dict], image_name: str) -> dict:
    """RIP → (模块名, 模块基址, RVA)。白名单判据用的就是这里的 RVA。"""
    m = module_of(rip, modules)
    main = main_module_of(modules, image_name)
    return {
        "fault_module": m["name"] if m else None,
        "fault_module_base": m["base"] if m else None,
        "fault_rva": (rip - m["base"]) if m else None,
        "in_main_module": bool(m and main and m["base"] == main["base"]),
        "main_module_base": main["base"] if main else None,
    }


def match_whitelist(rip: int, modules: list[dict], image_name: str,
                    whitelist) -> tuple[bool, int | None, str | None]:
    """故障指令地址在不在白名单里 → (命中, rva, 命中的条目写法)。

    条目两种写法：
      int          → 主模块（kards-Win64-Shipping.exe）基址 + RVA
      "mod+0xRVA"  → 指定模块（大小写不敏感；`*` = 任意模块）基址 + RVA
    特例："*" = 任意模块的任意访问违例（只给自测用 —— 自测目标的故障点在 python DLL 里）
    """
    site = _fault_site(rip, modules, image_name)
    rva = site["fault_rva"]
    if whitelist is None:
        return False, rva, None
    if isinstance(whitelist, (str, int)):
        whitelist = (whitelist,)
    for entry in whitelist:
        if entry is None:
            continue
        if isinstance(entry, int):
            if site["in_main_module"] and rva == entry:
                return True, rva, "%s+0x%x" % (image_name, entry)
            continue
        text = str(entry)
        if text == "*":
            return True, rva, "*"
        if "+" not in text:
            continue
        mod, _, off = text.rpartition("+")
        try:
            want = int(off, 16)
        except ValueError:
            continue
        here = image_name if site["in_main_module"] else (site["fault_module"] or "")
        mod = mod.strip()
        if mod != "*" and mod.lower() != here.lower():
            continue
        if rva == want:
            return True, rva, text
    return False, rva, None


# --------------------------------------------------------------------------- 状态
def default_status_path(out_dir: str = DEFAULT_OUT_DIR) -> str:
    return os.path.join(out_dir, "status.json")


def _pid_alive(pid: int) -> bool:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def _watcher_state_of(status_path: str) -> dict | None:
    st = read_json(status_path)
    if not isinstance(st, dict):
        return None
    wp = st.get("watcher_pid")
    if isinstance(wp, int) and _pid_alive(wp) and wp != os.getpid():
        return st
    return None


def publish_manual_dump(status_path: str, record: dict) -> bool:
    """`now`/`selftest` 的 dump 记录也写进 status.json —— 但只在没有活着的 watcher 时。

    有 watcher 在跑就别动它的文件：那会把它的 targets/dumps 全抹了。
    """
    if _watcher_state_of(status_path):
        return False
    st = read_json(status_path)
    if not isinstance(st, dict):
        st = {"state": "waiting", "pid": None, "since": time.time(), "dumps": [],
              "error": None}
    st.setdefault("dumps", []).append(record)
    st["updated"] = time.time()
    st["last_dump"] = record
    atomic_json(status_path, st)
    return True
