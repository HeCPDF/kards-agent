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

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
try:                                    # 跟 tools/ 其它脚本一致；本脚本不依赖它
    import _bootstrap  # noqa: F401
except Exception:                       # noqa: BLE001
    pass

# --------------------------------------------------------------------------- 常量
DEFAULT_IMAGE = "kards-Win64-Shipping.exe"
DEFAULT_OUT_DIR = r"D:\Kards\kards-data\crashdumps"
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
    script = os.path.abspath(__file__)
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


# --------------------------------------------------------------------------- 读 dump
MEM_STATES = {0x1000: "MEM_COMMIT", 0x2000: "MEM_RESERVE", 0x10000: "MEM_FREE"}
MEM_TYPES = {0x1000000: "MEM_IMAGE", 0x40000: "MEM_MAPPED", 0x20000: "MEM_PRIVATE"}
PAGE_PROTECT = {0x01: "NOACCESS", 0x02: "R", 0x04: "RW", 0x08: "WRITECOPY",
                0x10: "X", 0x20: "XR", 0x40: "XRW", 0x80: "XWRITECOPY",
                0x100: "GUARD", 0x200: "NOCACHE", 0x400: "WRITECOMBINE"}


def _md_streams(f) -> dict:
    f.seek(0)
    head = f.read(32)
    sig, ver, n, dir_rva = struct.unpack_from("<4sIII", head, 0)
    if sig != b"MDMP":
        raise DumpError("%s 不是 minidump（签名 %r）" % (getattr(f, "name", "?"), sig))
    f.seek(dir_rva)
    raw = f.read(12 * n)
    out = {}
    for i in range(n):
        t, sz, rva = struct.unpack_from("<III", raw, 12 * i)
        out[t] = (sz, rva)
    stamp = struct.unpack_from("<I", head, 24)[0]
    return {"streams": out, "timestamp": stamp, "version": ver}


class MiniDump:
    """只读地打开一份 minidump：内存区段 / 模块 / 线程 / 异常 / 内存信息表。

    为什么不复用 tools/dumpmem.py：崩溃分析工具要在整棵树被搬动/改坏时照常跑，
    而这里要多解四个流（异常、线程、内存信息、MiscInfo），复用省不下多少代码。
    """

    def __init__(self, path: str):
        self.path = path
        self.handles: list = []
        with open(path, "rb") as f:
            self.name = f.name
            meta = _md_streams(f)
            self.streams: dict = meta["streams"]
            self.timestamp = meta["timestamp"]
            # 两种内存流都要认：完整 dump 走 Memory64ListStream（数据在文件里连排），
            # UE 自带的小 dump 走 MemoryListStream（每段各有自己的 RVA）。
            self.ranges = []
            if MD_STREAM_MEMORY64 in self.streams:
                self.ranges = self._read_memory64(f)
            elif MD_STREAM_MEMORY_LIST in self.streams:
                self.ranges = self._read_memory_list(f)
            self.modules = self._read_modules(f)
            self.threads = self._read_threads(f)
            self.exception = self._read_exception(f)
            self.memory_info = (self._read_memory_info(f)
                                if MD_STREAM_MEMORY_INFO_LIST in self.streams else [])
            self.misc = self._read_misc(f)
            self.unloaded = self._read_unloaded(f)
            self._starts = [r[0] for r in self.ranges]

    # -- 流 ------------------------------------------------------------------
    def _read_memory64(self, f) -> list:
        _sz, rva = self.streams[MD_STREAM_MEMORY64]
        f.seek(rva)
        n, base_rva = struct.unpack("<QQ", f.read(16))
        raw = f.read(16 * n)
        out, off = [], base_rva
        for i in range(n):
            va, size = struct.unpack_from("<QQ", raw, 16 * i)
            out.append((va, size, off))
            off += size
        out.sort()
        return out

    def _read_memory_list(self, f) -> list:
        """MINIDUMP_MEMORY_LIST：NumberOfMemoryRanges + N × MINIDUMP_MEMORY_DESCRIPTOR
        （每项 16 字节：StartOfMemoryRange(8) + DataSize(4) + Rva(4)）。"""
        _sz, rva = self.streams[MD_STREAM_MEMORY_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(16 * n)
        out = []
        for i in range(n):
            va, size, data_rva = struct.unpack_from("<QII", raw, 16 * i)
            if size:
                out.append((va, size, data_rva))
        out.sort()
        return out

    def _read_modules(self, f) -> list:
        if MD_STREAM_MODULE_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_MODULE_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(108 * n)
        out = []
        for i in range(n):
            base, size, _ck, _ts, name_rva = struct.unpack_from("<QIIII", raw, 108 * i)
            f.seek(name_rva)
            ln = struct.unpack("<I", f.read(4))[0]
            nm = f.read(ln).decode("utf-16-le", "replace") if 0 < ln < 4096 else "?"
            out.append({"base": base, "size": size, "name": nm})
        out.sort(key=lambda m: m["base"])
        return out

    def _read_unloaded(self, f) -> list:
        """MINIDUMP_UNLOADED_MODULE_LIST 有个 **12 字节头**（SizeOfHeader/SizeOfEntry/
        NumberOfEntries，全是 u32），后面才是条目；条目 24 字节：
        BaseOfImage(8) + SizeOfImage(4) + CheckSum(4) + TimeDateStamp(4) + ModuleNameRva(4)。
        ★ 第一版把"头"当成了"条目数"、又按 12 字节/条解 —— 真实游戏 dump 上名字全是乱码。
        """
        if MD_STREAM_UNLOADED_MODULE_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_UNLOADED_MODULE_LIST]
        f.seek(rva)
        # ★ 这个头是 3 个 u32（12 字节），不是 <IIQ —— MemoryInfoList 那个才是 u64。
        size_of_header, size_of_entry, n = struct.unpack("<III", f.read(12))
        if size_of_header < 12 or size_of_header > 64 or n > 100000:
            return []
        stride = size_of_entry if size_of_entry >= 24 else 24
        f.seek(rva + size_of_header)
        raw = f.read(stride * n)
        out = []
        for i in range(n):
            base, size, _ck, _ts, name_rva = struct.unpack_from("<QIIII", raw,
                                                                stride * i)
            f.seek(name_rva)
            ln = struct.unpack("<I", f.read(4))[0]
            nm = f.read(ln).decode("utf-16-le", "replace") if 0 < ln < 4096 else "?"
            out.append({"base": base, "size": size, "name": nm})
        return out

    def _read_threads(self, f) -> list:
        if MD_STREAM_THREAD_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_THREAD_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(48 * n)
        out = []
        for i in range(n):
            tid, suspend, pri_class, pri, teb = struct.unpack_from("<IIIIQ", raw, 48 * i)
            stack_start, _stack_sz, _stack_rva = struct.unpack_from(
                "<QII", raw, 48 * i + 24)
            ctx_sz, ctx_rva = struct.unpack_from("<II", raw, 48 * i + 40)
            rip = None
            if ctx_rva and ctx_sz >= 0x100:
                f.seek(ctx_rva + 0xF8)
                rip = struct.unpack("<Q", f.read(8))[0]
            out.append({"tid": tid, "suspend": suspend, "teb": teb, "rip": rip,
                        "stack_start": stack_start, "context_rva": ctx_rva,
                        "context_size": ctx_sz})
        out.sort(key=lambda t: t["tid"])
        return out

    def _read_exception(self, f):
        """MINIDUMP_EXCEPTION_STREAM：ThreadId(4) __align(4) MINIDUMP_EXCEPTION(152)
        ThreadContext(MINIDUMP_LOCATION_DESCRIPTOR 8) = 168 字节。
        MINIDUMP_EXCEPTION 里的 ExceptionAddress 在 +16（64 位字面值）。"""
        if MD_STREAM_EXCEPTION not in self.streams:
            return None
        _sz, rva = self.streams[MD_STREAM_EXCEPTION]
        f.seek(rva)
        raw = f.read(168)
        tid, _pad = struct.unpack_from("<II", raw, 0)
        code, flags = struct.unpack_from("<II", raw, 8)
        addr = struct.unpack_from("<Q", raw, 16)[0]
        nparams = struct.unpack_from("<I", raw, 32)[0]
        info = list(struct.unpack_from("<15Q", raw, 40))
        ctx_sz, ctx_rva = struct.unpack_from("<II", raw, 160)
        rip = None
        if ctx_rva and ctx_sz >= 0x100:
            f.seek(ctx_rva + 0xF8)
            rip = struct.unpack("<Q", f.read(8))[0]
        return {"tid": tid, "code": code, "flags": flags, "address": addr,
                "num_parameters": nparams,
                "information": info[:max(0, min(nparams, 15))],
                "context_rva": ctx_rva, "context_size": ctx_sz, "rip": rip}

    def _read_memory_info(self, f) -> list:
        """MINIDUMP_MEMORY_INFO_LIST：头 16 字节（SizeOfHeader u32 / SizeOfEntry u32 /
        NumberOfEntries u64），条目 48 字节 × SizeOfEntry。
        ★ 第一版按 <IQ（12 字节）读头 —— 偏移全错，结果整张内存表读成空表，
          真实 dump 上被判成"没抓到堆"。"""
        _sz, rva = self.streams[MD_STREAM_MEMORY_INFO_LIST]
        f.seek(rva)
        _size_of_header, size_of_entry, n = struct.unpack("<IIQ", f.read(16))
        stride = size_of_entry if size_of_entry >= 48 else 48
        raw = f.read(stride * n)
        out = []
        for i in range(n):
            (base, alloc_base, alloc_prot, _a1, region, state, protect, typ,
             _a2) = struct.unpack_from("<QQIIQIIII", raw, stride * i)
            out.append({"base": base, "alloc_base": alloc_base,
                        "alloc_protect": alloc_prot, "size": region,
                        "state": state, "protect": protect, "type": typ})
        out.sort(key=lambda r: r["base"])
        return out

    def _read_misc(self, f):
        """MINIDUMP_MISC_INFO：SizeOfInfo/Flags1/ProcessId/ProcessCreateTime/
        ProcessUserTime/ProcessKernelTime —— 后四个是 **ULONG32**（不是 64 位）。"""
        if MD_STREAM_MISC_INFO not in self.streams:
            return None
        _sz, rva = self.streams[MD_STREAM_MISC_INFO]
        f.seek(rva)
        raw = f.read(24)
        if len(raw) < 24:
            return None
        size_of_info, flags1, pid, create_time = struct.unpack_from("<IIII", raw, 0)
        return {"size": size_of_info, "flags": flags1, "pid": pid,
                "create_time": create_time,
                "uptime_s": (self.timestamp - create_time) if create_time else None}

    # -- 读内存 --------------------------------------------------------------
    def read(self, addr: int, size: int) -> bytes | None:
        if not addr or size <= 0:
            return None
        i = bisect_right(self._starts, addr) - 1
        if i < 0:
            return None
        va, sz, off = self.ranges[i]
        if addr + size > va + sz:
            return None
        with open(self.path, "rb") as f:
            f.seek(off + (addr - va))
            d = f.read(size)
        return d if len(d) == size else None

    def u64(self, addr: int):
        d = self.read(addr, 8)
        return struct.unpack("<Q", d)[0] if d else None

    def u32(self, addr: int):
        d = self.read(addr, 4)
        return struct.unpack("<I", d)[0] if d else None

    # -- 地址分类 ------------------------------------------------------------
    def region_of(self, addr: int) -> dict | None:
        lo, hi = 0, len(self.memory_info) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            r = self.memory_info[mid]
            if addr < r["base"]:
                hi = mid - 1
            elif addr >= r["base"] + r["size"]:
                lo = mid + 1
            else:
                return r
        return None

    def in_dump(self, addr: int, size: int = 1) -> bool:
        i = bisect_right(self._starts, addr) - 1
        if i < 0:
            return False
        va, sz, _off = self.ranges[i]
        return addr + size <= va + sz

    def module_of(self, addr: int) -> dict | None:
        lo, hi = 0, len(self.modules) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            m = self.modules[mid]
            if addr < m["base"]:
                hi = mid - 1
            elif addr >= m["base"] + max(m["size"], 1):
                lo = mid + 1
            else:
                return m
        return None

    def memory_stats(self) -> dict:
        """这份 dump 到底抓到多少内存 —— 「小 dump（没堆）」要如实说。

        判据（不猜）：
          * 有没有 MemoryInfoListStream（= 生成时带没带 MiniDumpWithFullMemoryInfo）
          * 实际抓到的字节数
          * 有内存信息表时，把"已提交私有内存"加起来比一比 —— 差一个数量级就是"没堆"
        UE 自带那份 UEMinidump.dmp 是 0.7MB 级别，这里会直接判成 small。
        """
        captured = sum(size for _va, size, _off in self.ranges)
        try:
            file_size = os.path.getsize(self.path)
        except OSError:
            file_size = 0
        committed_private = committed_image = committed_other = 0
        for r in self.memory_info:
            if r["state"] != 0x1000:
                continue
            if r["type"] == 0x20000:
                committed_private += r["size"]
            elif r["type"] == 0x1000000:
                committed_image += r["size"]
            else:
                committed_other += r["size"]
        has_mi = bool(self.memory_info)
        heap_available = None
        if has_mi:
            heap_available = bool(committed_private > 0
                                  and captured >= 0.5 * committed_private)
        elif captured < 256 * 1024 ** 2:
            heap_available = False
        if captured < 256 * 1024 ** 2 and not has_mi:
            verdict = ("small dump（文件 %.1f MB / 抓到 %.1f MB）：没有 FullMemoryInfo、"
                       "也没有堆 —— 只有模块/线程/栈和少量内存。想查对象内容必须用 "
                       "crashdump 落的完整 dump。" % (file_size / 1e6, captured / 1e6))
        elif has_mi and heap_available:
            verdict = ("full dump：抓到 %.2f GB（私有提交 %.2f GB），堆在里面"
                       % (captured / 2**30, committed_private / 2**30))
        elif has_mi:
            verdict = ("不完整：只抓到 %.1f MB，而进程私有提交内存有 %.2f GB —— "
                       "大部分堆没进 dump（写到一半？）"
                       % (captured / 1e6, committed_private / 2**30))
        else:
            verdict = ("未知：有 %.2f GB 内存但没带 FullMemoryInfo 表，"
                       "无法判断堆的完整性" % (captured / 2**30))
        return {"file_size": file_size,
                "captured_bytes": captured, "captured_mb": round(captured / 1e6, 1),
                "ranges": len(self.ranges),
                "has_memory_info_list": has_mi,
                "committed_private_bytes": committed_private,
                "committed_image_bytes": committed_image,
                "committed_other_bytes": committed_other,
                "heap_available": heap_available, "verdict": verdict}

    def classify(self, addr: int) -> dict:
        """这个地址在 dump 里是什么？—— 给「哪个委托实例已被释放」当判据。"""
        r = self.region_of(addr)
        out = {"address": addr, "state": None, "type": None, "protect": None,
               "region_base": None, "region_size": None, "allocation_base": None,
               "in_dump": self.in_dump(addr, 8), "module": None,
               "verdict": "unknown"}
        if r:
            out.update(state=MEM_STATES.get(r["state"], hex(r["state"])),
                       type=MEM_TYPES.get(r["type"], hex(r["type"])),
                       protect="|".join(n for v, n in PAGE_PROTECT.items()
                                        if r["protect"] & v) or hex(r["protect"]),
                       region_base=r["base"], region_size=r["size"],
                       allocation_base=r["alloc_base"])
        m = self.module_of(addr)
        if m:
            out["module"] = m["name"]
            out["module_rva"] = addr - m["base"]
        if r and r["state"] == 0x10000:
            out["verdict"] = "unmapped(MEM_FREE)"
        elif r and r["state"] == 0x2000:
            out["verdict"] = "reserved-but-not-committed"
        elif r and (r["protect"] & 0x01):            # PAGE_NOACCESS
            out["verdict"] = "committed-PAGE_NOACCESS"
        elif r and r["state"] == 0x1000:
            out["verdict"] = "committed"
        elif r is None and not self.memory_info:
            out["verdict"] = "unknown(no MemoryInfoList)"
        elif r is None:
            out["verdict"] = "outside-any-region"
        if not out["in_dump"] and out["verdict"] == "committed":
            out["verdict"] = "committed-but-not-captured"
        return out


# --------------------------------------------------------------------------- cdb
CMD_ANALYZE = ".ecxr; k 30; r; !analyze -v; ~*k 8; q"
RE_FRAME = re.compile(r"^\s*(?:([0-9a-f]{2})\s+)?([0-9a-f]{16})\s+([0-9a-f]{16})\s+(\S.*?)\s*$")
RE_THREAD = re.compile(r"^\s*([.\s])*\s*(\d+)\s+Id:\s*([0-9a-f]+)\.([0-9a-f]+)\s+Suspend:")
RE_REGVAL = re.compile(r"([a-z][a-z0-9]{1,3})=([0-9a-f]{2,17})")
REGS_WANTED = ("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "rip",
               "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
               "efl", "cs", "ss", "ds", "es", "fs", "gs", "iopl")


def cdb_path(cdb: str | None = None) -> str:
    path = cdb or CDB
    if not os.path.exists(path):
        raise DumpError("找不到 cdb：%s（需求里写死的路径；装了 Windows SDK 调试器才有）"
                        % path)
    return path


def run_cdb(dump_path: str, commands: str, txt_path: str, cdb: str | None = None,
            timeout: float = 1200.0, symbol_dir: str | None = None) -> dict:
    """跑一次 cdb，把**原始输出**存成 txt（成功失败都存，失败了更要看）。

    符号：只给一个**本地空目录**（`-y` + `_NT_SYMBOL_PATH`）。
    这样 cdb 只用模块自带的导出表，一个字节都不会去够网络 ——
    "没装符号服务器也能跑通"就是靠这条。
    """
    exe = cdb_path(cdb)
    symbol_dir = symbol_dir or os.path.join(os.path.dirname(os.path.abspath(dump_path)),
                                            "symbols")
    os.makedirs(symbol_dir, exist_ok=True)
    env = dict(os.environ)
    env["_NT_SYMBOL_PATH"] = symbol_dir          # ★ 覆盖掉机器上可能存在的 srv* 设置
    env["_NT_ALT_SYMBOL_PATH"] = symbol_dir
    env["_NT_EXECUTABLE_IMAGE_PATH"] = ""
    cmd = [exe, "-z", os.path.abspath(dump_path), "-y", symbol_dir, "-c", commands]
    t0 = time.perf_counter()
    timed_out = False
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        raw, _ = proc.communicate()
    dt = time.perf_counter() - t0
    text = _decode_console(raw)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# crashdump.py analyze: %s\n" % exe)
        f.write("# dump: %s\n" % os.path.abspath(dump_path))
        f.write("# cdb -z <dump> -y <本地符号目录> -c \"%s\"\n" % commands)
        f.write("# 耗时 %.1fs%s\n\n" % (dt, "（超时被杀）" if timed_out else ""))
        f.write(text)
    return {"ok": proc.returncode == 0 and not timed_out, "seconds": round(dt, 2),
            "timed_out": timed_out, "text": text, "txt": txt_path,
            "returncode": proc.returncode, "commands": commands}


def _decode_console(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _after_command_echo(text: str) -> str:
    """cdb 会把 `cdb: Reading initial command '...'` 打出来；从它后面开始解析，
    免得把启动横幅里的东西当结果。"""
    idx = text.find("cdb: Reading initial command")
    if idx < 0:
        return text
    nl = text.find("\n", idx)
    return text[nl + 1:] if nl >= 0 else text


def parse_registers(text: str) -> dict:
    body = _after_command_echo(text)
    regs = {}
    for line in body.splitlines():
        if not RE_REGVAL.search(line.replace("`", "")):
            if regs:
                break                   # 寄存器块是连续的，断了就收工
            continue
        for name, val in RE_REGVAL.findall(line.replace("`", "")):
            if name in REGS_WANTED and name not in regs:
                try:
                    regs[name] = int(val, 16)
                except ValueError:
                    pass
    return regs


def _split_callsite(text: str) -> dict:
    """`module!sym+0x12` / `module+0x12` / `module!sym` → 三段。"""
    mod, _, rest = text.partition("!")
    sym, off = (rest or None), None
    if sym and "+" in sym:
        sym, _, off = sym.rpartition("+")
    elif not rest and "+" in mod:
        mod, _, off = mod.rpartition("+")
    return {"module": mod.strip() or None, "symbol": (sym.strip() if sym else None),
            "offset": off, "text": text}


def parse_frames(block: str, limit: int = 30) -> list:
    out = []
    for line in block.splitlines():
        m = RE_FRAME.match(line.replace("`", ""))
        if not m:
            if out:
                break
            continue
        frame = {"sp": int(m.group(2), 16), "ret": int(m.group(3), 16)}
        frame.update(_split_callsite(m.group(4).strip()))
        out.append(frame)
        if len(out) >= limit:
            break
    return out


def parse_threads(text: str, frames_per_thread: int = 8) -> list:
    """把 `~*k 8` 那段拆成 [{tid, current, frames:[...]}]。"""
    body = _after_command_echo(text)
    lines = body.splitlines()
    threads, cur = [], None
    for i, line in enumerate(lines):
        m = RE_THREAD.match(line.replace("`", ""))
        if m:
            cur = {"tid": int(m.group(4), 16), "pid": int(m.group(3), 16),
                   "current": bool(m.group(1) and m.group(1).strip() == "."),
                   "suspend": None, "frames": []}
            sm = re.search(r"Suspend:\s*(\d+)", line)
            if sm:
                cur["suspend"] = int(sm.group(1))
            threads.append(cur)
            continue
        if cur is None:
            continue
        fm = RE_FRAME.match(line.replace("`", ""))
        if fm and len(cur["frames"]) < frames_per_thread:
            fr = {"sp": int(fm.group(2), 16), "ret": int(fm.group(3), 16)}
            fr.update(_split_callsite(fm.group(4).strip()))
            cur["frames"].append(fr)
    return threads


def _analyze_fields(text: str) -> dict:
    """从 `!analyze -v` 里抠几个名字段（只是旁证，主判据用我们自己解析的 dump）。"""
    out = {}
    pats = {
        "bugcheck_str": r"BUGCHECK_STR:\s*(.+)",
        "failure_bucket": r"FAILURE_BUCKET_ID:\s*(.+)",
        "module_name": r"MODULE_NAME:\s*(.+)",
        "image_name": r"IMAGE_NAME:\s*(.+)",
        "process_name": r"PROCESS_NAME:\s*(.+)",
        "exception_code_line": r"EXCEPTION_CODE:\s*(.+)",
        "os_version": r"OS_VERSION:\s*(.+)",
    }
    for key, pat in pats.items():
        m = re.search(pat, text)
        if m:
            out[key] = m.group(1).strip()
    m = re.search(r"FAULTING_IP:\s*\n\s*(\S+)\s*\n\s*([0-9a-f`]+)\s", text)
    if m:
        out["faulting_ip_symbol"] = m.group(1)
        out["faulting_ip"] = int(m.group(2).replace("`", ""), 16)
    return out


def _frida_report(threads: list, crash_tid: int | None) -> dict:
    """frida_agent 当时在不在跑？（栈里有没有 frida 帧 / 有没有卡在 ProcessEvent 里）

    这是给"崩溃是不是我们的自动化（frida）引起的"提供第一手判据。
    """
    frida_threads, pe_threads = [], []
    for t in threads:
        blob = " ".join((f.get("text") or "") for f in t["frames"]).lower()
        if "frida" in blob:
            frida_threads.append(t["tid"])
        if "processevent" in blob or "process_internal" in blob:
            pe_threads.append(t["tid"])
    crash = next((t for t in threads if t["tid"] == crash_tid), None)
    crash_frames = [f["text"] for f in crash["frames"]] if crash else []
    return {
        "threads_with_frida_frames": frida_threads,
        "threads_in_processevent": pe_threads,
        "crash_thread_has_frida_frame": any("frida" in (f or "").lower()
                                            for f in crash_frames),
        "crash_thread_in_processevent": crash_tid in pe_threads,
        "crash_thread_frames": crash_frames,
    }


def analyze(dump_path: str, heap: int | str | None = None, cdb: str | None = None,
            timeout: float = 1200.0, txt_path: str | None = None,
            delegates: bool = False) -> dict:
    """跑 cdb（`.ecxr; k 30; r; !analyze -v; ~*k 8; q`）→ 同名 .txt + summary。

    没有符号服务器也能跑：cdb 只被喂了本地空符号目录，其余靠模块导出表。
    summary 里的异常码/RIP/RVA/线程数都是**我们自己解 dump** 得到的（可靠），
    cdb 的输出用来给寄存器、调用栈和 !analyze 的旁证。
    """
    dump_path = os.path.abspath(dump_path)
    if not os.path.exists(dump_path):
        raise DumpError("没有这个 dump：%s" % dump_path)
    txt_path = txt_path or os.path.splitext(dump_path)[0] + ".txt"
    md = MiniDump(dump_path)
    res = run_cdb(dump_path, CMD_ANALYZE, txt_path, cdb=cdb, timeout=timeout)
    text = res["text"]
    regs = parse_registers(text)
    frames = parse_frames(_after_command_echo(text), limit=30)
    threads = parse_threads(text)
    exc = md.exception
    crash_tid = exc["tid"] if exc else (next((t["tid"] for t in threads if t["current"]),
                                             None))
    crash_thread = next((t for t in threads if t["tid"] == crash_tid), None)
    fault_rip = (regs.get("rip") if "rip" in regs else None) or (exc or {}).get("rip")
    main = (md.module_of(fault_rip) if fault_rip else None) or \
        main_module_of(md.modules, DEFAULT_IMAGE) or (md.modules[0] if md.modules else None)
    fault_rva = None
    fault_mod = None
    if fault_rip:
        m = md.module_of(fault_rip)
        if m:
            fault_mod, fault_rva = m["name"], fault_rip - m["base"]

    summary = {
        "dump": dump_path,
        "txt": txt_path,
        "size": os.path.getsize(dump_path),
        "dump_time": md.timestamp,
        "process": {"pid": (md.misc or {}).get("pid"),
                    "uptime_s": (md.misc or {}).get("uptime_s"),
                    "image": next((m["name"] for m in md.modules
                                   if main and m["base"] == main["base"]), None)},
        "exception": {
            "code": ("0x%08x" % exc["code"]) if exc else None,
            "code_int": exc["code"] if exc else None,
            "address": exc["address"] if exc else None,
            "tid": exc["tid"] if exc else None,
            "parameters": exc["information"] if exc else None,
            "access_type": (["read", "write", "execute"][exc["information"][0]]
                            if exc and exc["information"]
                            and exc["information"][0] <= 2 else None),
            "access_address": (exc["information"][1] if exc and len(exc["information"]) > 1
                               else None),
            "note": None if exc else
                    "这份 dump 没有异常流（`now` 打的那种）；.ecxr 不可用，"
                    "用 sidecar .json 里的线程 RIP 表定位。",
        },
        "fault_rip": fault_rip,
        "fault_rva": fault_rva,
        "fault_module": fault_mod,
        "main_module_base": main["base"] if main else None,
        "registers": {k: regs.get(k) for k in REGS_WANTED if k in regs},
        "stack": frames[:12],
        "stack_all": frames,
        "crash_thread": crash_tid,
        "thread_count": len(md.threads) or len(threads),
        "threads": [{"tid": t["tid"], "rip": t["rip"], "suspend": t["suspend"]}
                    for t in md.threads] or
                   [{"tid": t["tid"], "suspend": t["suspend"], "rip": None}
                    for t in threads],
        "cdb": {"ok": res["ok"], "seconds": res["seconds"], "timed_out": res["timed_out"],
                "commands": CMD_ANALYZE, "returncode": res["returncode"]},
        "analyze": _analyze_fields(text),
        "memory": md.memory_stats(),
        "modules": {"count": len(md.modules), "unloaded": len(md.unloaded),
                    "main_base": main["base"] if main else None},
    }
    summary["frida"] = _frida_report(threads, crash_tid)
    if not res["ok"] and res["timed_out"]:
        summary["cdb"]["note"] = ("cdb 超时被杀：dump 很大时 `!analyze -v` 会慢；"
                                 "txt 里是已产出的部分")
    if heap is not None:
        try:
            summary["heap"] = analyze_heap(dump_path, heap, md=md, cdb=cdb,
                                           timeout=timeout)
        except BaseException as exc:                        # noqa: BLE001
            summary["heap"] = {"error": "%s: %s" % (type(exc).__name__, exc),
                               "note": "heap 分析自己失败了，别影响主结果"}
    if delegates:
        try:
            summary["delegates"] = scan_delegates(dump_path, md=md, registers=regs,
                                                  rsp=regs.get("rsp"))
        except BaseException as exc:                        # noqa: BLE001
            summary["delegates"] = {"error": "%s: %s" % (type(exc).__name__, exc),
                                   "note": "delegates 扫描自己失败了，别影响主结果"}
    return {"txt": txt_path, "summary": summary}


# --------------------------------------------------------------------------- heap
CMD_HEAP = ".ecxr; r; !address {addr}; dqs {addr} L8; !heap -x {addr}; ln {addr}; q"
REG_CANDIDATES = ("rdi", "rcx", "rax", "rsi", "rbx", "rdx", "rbp", "r8", "r9",
                  "r10", "r11", "r12", "r13", "r14", "r15", "rsp")


class _Names:
    """可选加成：能 import 到 kardsmem/dumpmem 时顺便把名字解出来。

    解不出来（换构建、池地址变了、树被搬走）就整体退化成 None，主流程不受影响。
    崩溃分析工具不能因为"顺手解个名字"就不干活。
    """

    def __init__(self, dump_path: str, base: int | None):
        self.pool = None
        self.why = None
        try:
            from dumpmem import DumpMem                     # noqa: PLC0415
            from kardsmem.names import FNamePool            # noqa: PLC0415
        except BaseException as exc:                        # noqa: BLE001
            self.why = "import 不到 dumpmem/kardsmem.names：%s" % exc
            return
        try:
            self.mem = DumpMem(dump_path)
            self.pool = FNamePool(self.mem, base)
        except BaseException as exc:        # ★ dumpmem 用的是 raise SystemExit
            self.why = "名字池建不起来（多半是这份 dump 没带完整内存）：%s" % exc

    def ok(self) -> bool:
        return self.pool is not None

    def fname(self, index: int, number: int = 0):
        if not self.pool or index in (0, 0xFFFFFFFF):
            return None
        try:
            return self.pool.fname(index, number)
        except Exception:                                   # noqa: BLE001
            return None

    def object_name(self, obj: int):
        if not self.pool or not obj:
            return None
        try:
            return self.pool.fname_of(obj)
        except Exception:                                   # noqa: BLE001
            return None


def _describe_object(md: MiniDump, obj: int, names: _Names | None) -> dict:
    """一个指针指向的到底是不是活的 UObject（UObject 布局：vtable@0, flags@8,
    index@0xC, class@0x10, name@0x18 —— 见 kardsmem/build.py）。"""
    out = {"address": obj, "address_hex": _hex(obj), "region": md.classify(obj),
           "vtable": None, "vtable_module": None, "class": None, "name": None,
           "notes": []}
    if not obj:
        out["notes"].append("空指针")
        return out
    vt = md.u64(obj)
    out["vtable"] = vt
    if vt:
        vm = md.module_of(vt)
        out["vtable_module"] = vm["name"] if vm else None
        if vm is None:
            out["notes"].append("★ vtable（对象第一个 qword）不在任何已加载模块里 —— "
                                "这块内存已经不像活的 UObject（已释放/被覆写）")
    else:
        out["notes"].append("★ 读不出 vtable（这块不在 dump 里，或已不可访问）")
    verdict = out["region"]["verdict"]
    if verdict.startswith("unmapped") or verdict == "reserved-but-not-committed":
        out["notes"].append("★★ 指针落在**未映射**的地址空间 —— 这个委托实例已经被释放"
                            "（页都还给系统了）")
    cls = md.u64(obj + 0x10)
    out["class_ptr"] = cls
    if names and names.ok():
        out["name"] = names.object_name(obj)
        out["class"] = names.object_name(cls) if cls else None
    if out["name"] is None and md.in_dump(obj + 0x18, 8):
        out["name_fname_raw"] = md.u64(obj + 0x18)   # 没名字池，至少留原始 FName
    return out


def _walk_tarray(md: MiniDump, base: int, limit: int = 64) -> dict | None:
    """把 base 当 UE 的 TArray 头（{Data, Num, Max} 24 字节）试着走一遍。"""
    hdr = md.read(base, 24)
    if not hdr:
        return None
    data, num, mx = struct.unpack("<QQQ", hdr)
    if not (1 <= num <= 8192) or mx < num or mx > (1 << 24) or data < 0x10000:
        return None
    if not md.in_dump(data, 16) and not md.region_of(data):
        return None
    entries = []
    for i in range(min(num, limit)):
        raw = md.read(data + 16 * i, 16)
        if not raw:
            entries.append({"i": i, "unreadable": True, "entry": data + 16 * i})
            continue
        obj, fn = struct.unpack("<QQ", raw)
        entries.append({"i": i, "entry": data + 16 * i, "object": obj,
                        "fname_raw": fn, "fname_index": fn & 0xFFFFFFFF,
                        "fname_number": fn >> 32})
    return {"header": base, "data": data, "num": num, "max": mx,
            "in_dump": md.in_dump(data, 16 * max(1, num)), "entries": entries,
            "truncated": num > limit}


def _looks_like_entry(md: MiniDump, addr: int) -> bool:
    """直接指着 entry（Object ptr + FName）而不是 TArray 头的情况。"""
    obj = md.u64(addr)
    if not obj or obj < 0x10000:
        return False
    vt = md.u64(obj)
    return bool(vt and md.module_of(vt))


def analyze_heap(dump_path: str, address, md: MiniDump | None = None,
                 cdb: str | None = None, timeout: float = 1200.0,
                 registers: dict | None = None) -> dict:
    """`analyze --heap <addr>`：这个地址是什么 + 顺着寄存器指的数组把委托列表读出来。

    两个来源合起来给判据：
      * cdb 的 `!address` / `dqs` / `!heap -x` / `ln`（人读的原文进同名 txt）
      * 我们自己解 dump（机器可读：区段状态、每个 entry 的指针落在哪、vtable 在不在
        模块里）—— 后面这几条直接回答"哪个委托实例已经被释放"。
    """
    addr = address if isinstance(address, int) else int(str(address), 0)
    dump_path = os.path.abspath(dump_path)
    md = md or MiniDump(dump_path)
    txt = "%s.heap-%x.txt" % (os.path.splitext(dump_path)[0], addr)
    cmds = CMD_HEAP.format(addr=_hex(addr))
    res = run_cdb(dump_path, cmds, txt, cdb=cdb, timeout=timeout)
    regs = registers or parse_registers(res["text"])
    base = main_module_of(md.modules, DEFAULT_IMAGE)
    names = _Names(dump_path, base["base"] if base else None)

    report = {
        "address": addr,
        "address_hex": _hex(addr),
        "region": md.classify(addr),
        "qwords": [{"address": addr + 8 * i, "value": md.u64(addr + 8 * i)}
                   for i in range(8)],
        "module": (md.module_of(addr) or {}).get("name"),
        "in_dump": md.in_dump(addr, 8),
        "cdb_txt": txt,
        "cdb": {"ok": res["ok"], "seconds": res["seconds"], "commands": cmds,
                "timed_out": res["timed_out"]},
        "registers": {k: regs.get(k) for k in REG_CANDIDATES if k in regs},
        "array_candidates": [],
        "names": {"resolved": names.ok(), "why": names.why},
        "verdict": [],
    }
    seen = set()
    sources = [(r, regs.get(r)) for r in REG_CANDIDATES if regs.get(r)]
    sources.append(("(给定地址)", addr))
    for src, ptr in sources:
        if not ptr or ptr < 0x10000:
            continue
        arr = _walk_tarray(md, ptr)
        if arr is not None and ("hdr", ptr) not in seen:
            seen.add(("hdr", ptr))
            for e in arr["entries"]:
                if e.get("unreadable"):
                    continue
                e["object_info"] = _describe_object(md, e["object"], names)
                e["fname"] = names.fname(e["fname_index"], e["fname_number"])
                e["suspect"] = any(n.startswith("★")
                                   for n in e["object_info"]["notes"])
            report["array_candidates"].append({
                "source": "%s=%s" % (src, _hex(ptr)),
                "kind": "TArray 头 {Data,Num,Max}",
                "header": arr["header"], "data": arr["data"], "num": arr["num"],
                "max": arr["max"], "in_dump": arr["in_dump"],
                "truncated": arr["truncated"], "entries": arr["entries"]})
            report["verdict"].append("%s 指向一个 TArray 头：Data=%s Num=%d Max=%d"
                                     % (src, _hex(arr["data"]), arr["num"], arr["max"]))
        if _looks_like_entry(md, ptr) and ("data", ptr) not in seen:
            seen.add(("data", ptr))
            entries = []
            for i in range(8):
                raw = md.read(ptr + 16 * i, 16)
                if not raw:
                    break
                obj, fn = struct.unpack("<QQ", raw)
                info = _describe_object(md, obj, names)
                entries.append({"i": i, "entry": ptr + 16 * i, "object": obj,
                                "fname_index": fn & 0xFFFFFFFF,
                                "fname_number": fn >> 32, "object_info": info,
                                "fname": names.fname(fn & 0xFFFFFFFF, fn >> 32),
                                "suspect": any(n.startswith("★")
                                               for n in info["notes"])})
            report["array_candidates"].append({
                "source": "%s=%s" % (src, _hex(ptr)),
                "kind": "委托 entry 数组（直接指着 16 字节项）",
                "data": ptr, "num": len(entries), "entries": entries})
            report["verdict"].append("%s 看起来直接指向委托 entry 数组（16B/项）" % src)
    if not report["array_candidates"]:
        report["verdict"].append(
            "没有哪个通用寄存器/给定地址看起来像委托列表（TArray 头或 entry 数组）。"
            "看 cdb_txt 里的 `!address` / `!heap -x` / `dqs` 原文。")
    return report


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


def _live_log_path() -> str:
    """监听器日志的落点：优先 `base.paths.LIVE_LOG`（运行期位置只有那一张表），拿不到回退默认。

    ★ 为什么不直接写死 import：本脚本要在整棵树被搬动/改坏时照常能跑（见文件头设计要点 7），
      所以做成"能拿到就拿、拿不到回退"——别让这一步把 .context.txt 带崩。
      （2026-10-03：监听器从 `_nn_scratch/` 搬进 `tools/`，通道移到 `kards-data/live/`。）
    """
    try:
        from base import paths as _paths          # noqa: PLC0415
        return _paths.LIVE_LOG
    except Exception:                              # noqa: BLE001
        return os.path.join("D:" + os.sep, "Kards", "kards-data", "live", "live_log.txt")


def _context_bundle(dump_path: str, tail: int = 30) -> str:
    """把"崩溃前我们最后做了什么"拷到 dump 旁边（同名 .context.txt）。

    两路来源（都只读，缺了就写缺）：
      * D:\\Kards\\kards-data\\nn\\logs\\rule-live-*.jsonl 里**最新**那个的最后 30 行
      * 监听器日志（`base.paths.LIVE_LOG`，默认 D:\\Kards\\kards-data\\live\\live_log.txt）的最后 30 行
    目的：打开 dump 的那一瞬就能对上"崩溃前自动化发了什么命令"。
    """
    src_a = r"D:\Kards\kards-data\nn\logs"
    src_b = _live_log_path()
    out_path = os.path.splitext(dump_path)[0] + ".context.txt"
    lines = ["# crashdump.py 现场上下文  dump=%s" % os.path.abspath(dump_path),
             "# UTC=%s  本地=%s"
             % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))]
    newest = None
    try:
        cands = [os.path.join(src_a, n) for n in os.listdir(src_a)
                 if n.startswith("rule-live-") and n.endswith(".jsonl")]
        if cands:
            newest = max(cands, key=lambda p: os.path.getmtime(p))
    except OSError:
        newest = None
    for label, path in (("rule-live（最新）", newest), ("live_log.txt", src_b)):
        lines.append("")
        if not path:
            lines.append("## %s —— 没有（目录里找不到 rule-live-*.jsonl）" % label)
            continue
        lines.append("## %s —— %s（最后 %d 行，原样）" % (label, path, tail))
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                back = min(size, 65536)
                f.seek(size - back)
                blob = f.read(back)
            text = _decode_console(blob)
            ls = [ln for ln in text.splitlines() if ln.strip()]
            lines.extend(ls[-tail:])
        except OSError as exc:
            lines.append("## 读不了 %s：%s" % (path, exc))
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return out_path


# --------------------------------------------------------------------------- 自身开销
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE,
                                        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                        wt.DWORD]
_psapi.GetProcessMemoryInfo.restype = wt.BOOL
k32.GetProcessTimes.restype = wt.BOOL
_SELF_BASE: dict = {}


def self_usage() -> dict:
    """watch 自己吃多少（CPU 秒 / 常驻内存）—— 验收要报这个。

    CPU% 是**相对 watch 启动那一刻**的平均值（`cpu_s / 墙钟`），
    所以常驻时看到的就是"平均拖了多少 CPU"。
    """
    h = k32.GetCurrentProcess()
    ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    cpu = None
    if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                           ctypes.byref(kt), ctypes.byref(ut)):
        cpu = ((kt.dwHighDateTime << 32 | kt.dwLowDateTime)
               + (ut.dwHighDateTime << 32 | ut.dwLowDateTime)) / 1e7
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    ws = peak = pf = None
    if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
        ws, peak, pf = pmc.WorkingSetSize, pmc.PeakWorkingSetSize, pmc.PageFaultCount
    now = time.time()
    if "cpu" not in _SELF_BASE and cpu is not None:
        _SELF_BASE.update(cpu=cpu, time=now)
    out = {"cpu_s": round(cpu, 3) if cpu is not None else None,
           "working_set_mb": round((ws or 0) / 2**20, 1),
           "peak_working_set_mb": round((peak or 0) / 2**20, 1),
           "page_faults": pf}
    if cpu is not None and "cpu" in _SELF_BASE:
        wall = max(1e-6, now - _SELF_BASE["time"])
        out["wall_s"] = round(wall, 1)
        out["cpu_pct"] = round(100.0 * (cpu - _SELF_BASE["cpu"]) / wall, 3)
    return out


# --------------------------------------------------------------------------- exe 指纹
_FINGERPRINT_CACHE: dict = {}


def pe_fingerprint(path: str) -> dict | None:
    """SizeOfImage / TimeDateStamp / md5 / 段表 —— **认构建靠 SizeOfImage**
    （RVA 能不能用只看它；md5 只说明这份副本有没有被动过）。

    带缓存：同一个 (路径, 大小, mtime) 只算一次。md5 要读 1~2GB，
    绝不能在"游戏冻住"的窗口里做。
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"error": "stat 失败：%s" % exc, "path": path}
    key = (path, st.st_size, int(st.st_mtime))
    if key in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE[key]
    out = {"path": path, "file_size": st.st_size, "md5": None,
           "size_of_image": None, "timestamp": None, "timestamp_utc": None,
           "machine": None, "sections": []}
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if head[:2] != b"MZ":
                raise ValueError("不是 PE（没有 MZ）")
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            f.seek(e_lfanew)
            pe = f.read(24)
            if pe[:4] != b"PE\0\0":
                raise ValueError("不是 PE（没有 PE\\0\\0）")
            machine, nsec, stamp, _psym, _nsym, opt_size, _chars = \
                struct.unpack_from("<HHIIIHH", pe, 4)
            out.update(machine="0x%04x" % machine, timestamp=stamp,
                       timestamp_utc=(time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime(stamp)) if stamp else None))
            opt = f.read(opt_size)
            magic = struct.unpack_from("<H", opt, 0)[0] if len(opt) >= 2 else 0
            if magic == 0x20B:                       # PE32+
                out["size_of_image"] = struct.unpack_from("<I", opt, 0x38)[0]
                out["image_base"] = struct.unpack_from("<Q", opt, 0x18)[0]
            elif magic == 0x10B:                     # PE32
                out["size_of_image"] = struct.unpack_from("<I", opt, 0x38)[0]
                out["image_base"] = struct.unpack_from("<I", opt, 0x1C)[0]
            else:
                raise ValueError("optional header magic=0x%x" % magic)
            f.seek(e_lfanew + 24 + opt_size)
            secs = []
            for _ in range(nsec):
                raw = f.read(40)
                if len(raw) < 40:
                    break
                name = raw[:8].rstrip(b"\0").decode("latin-1")
                vsize, vaddr = struct.unpack_from("<II", raw, 8)
                raw_size, _raw_ptr = struct.unpack_from("<II", raw, 16)
                chars = struct.unpack_from("<I", raw, 36)[0]
                secs.append({"name": name, "virtual_address": vaddr,
                             "virtual_size": vsize, "raw_size": raw_size,
                             "characteristics": "0x%08x" % chars})
            out["sections"] = secs
            # md5 放最后（唯一的大 IO），分块读
            h = hashlib.md5()
            f.seek(0)
            while True:
                chunk = f.read(4 << 20)
                if not chunk:
                    break
                h.update(chunk)
            out["md5"] = h.hexdigest()
    except (OSError, ValueError, struct.error) as exc:
        out["error"] = str(exc)
    _FINGERPRINT_CACHE[key] = out
    return out


def section_of(fingerprint: dict | None, base: int, va: int) -> dict | None:
    """addr 落在哪个 PE 段里（给"虚表在不在 .rdata"当判据）。"""
    if not fingerprint or not base:
        return None
    off = va - base
    for s in fingerprint.get("sections") or []:
        start = s["virtual_address"]
        size = max(s["virtual_size"], s["raw_size"])
        if size and start <= off < start + size:
            return {"name": s["name"], "offset": off - start}
    return None


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__alignment1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD), ("Protect", wt.DWORD),
                ("Type", wt.DWORD), ("__alignment2", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
MAX_USER_ADDRESS = 0x7FFFFFFF0000


def memory_map_csv(pid: int, csv_path: str) -> dict:
    """`!address -summary` 的原料：VirtualQueryEx 走一遍地址空间 → CSV + 汇总。

    写 CSV 是需求点名的（基址,大小,状态,保护,类型）。跟着 dump 走的好处：
    dump 打开后一眼能看出某个野指针落在**已提交 / 已保留 / 未映射**哪一类 ——
    0x1e3020030 这种值到底是不是"页都还给系统了"，看这张表最快。
    * 走的时候进程若冻着（watch 路径），这张表和 dump 是同一时刻的。
    """
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_VM_READ, False, pid)
    if not h:
        return {"error": "OpenProcess 失败 err=%d" % ctypes.get_last_error(),
                "csv": None, "regions": 0}
    rows = []
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    guard = 0
    try:
        while addr < MAX_USER_ADDRESS and guard < 500000:
            guard += 1
            got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                     ctypes.sizeof(mbi))
            if not got:
                addr += 0x1000            # 空洞：翻一页继续
                continue
            if mbi.RegionSize == 0:
                break
            rows.append((mbi.BaseAddress, mbi.RegionSize, mbi.State, mbi.Protect,
                         mbi.Type))
            addr = mbi.BaseAddress + mbi.RegionSize
    finally:
        k32.CloseHandle(h)
    by_state: dict = {}
    by_type: dict = {}
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("base,size,state,protect,type\n")
        for base, size, state, protect, typ in rows:
            f.write("0x%x,0x%x,%s,%s,%s\n"
                    % (base, size, MEM_STATES.get(state, "0x%x" % state),
                       "|".join(n for v, n in PAGE_PROTECT.items() if protect & v)
                       or "0x%x" % protect,
                       MEM_TYPES.get(typ, "0x%x" % typ if typ else "0")))
            s = MEM_STATES.get(state, "0x%x" % state)
            by_state.setdefault(s, {"count": 0, "bytes": 0})
            by_state[s]["count"] += 1
            by_state[s]["bytes"] += size
            t = MEM_TYPES.get(typ, "none" if not typ else "0x%x" % typ)
            by_type.setdefault(t, {"count": 0, "bytes": 0})
            by_type[t]["count"] += 1
            by_type[t]["bytes"] += size
    return {"csv": csv_path, "regions": len(rows), "by_state": by_state,
            "by_type": by_type, "scanned_to": addr,
            "note": "VirtualQueryEx 现场快照；dump 里同一地址若已不在这些区段里，"
                    "就是崩溃后又被改过（正常，UE 一直在分配）"}


def build_sidecars(pid: int, dump_path: str, info: dict, modules: list,
                   image_name: str = DEFAULT_IMAGE) -> dict:
    """dump 旁边那几份附属文件（.json 的公共部分）。

    ★ 顺序讲究：这里所有操作都**不需要**进程冻着（除了调用方在冻着的时候顺带
      拿过一次一致性快照），所以 watch 路径把它放在 ContinueDebugEvent 之后也行；
      但 memory.csv 是在冻着的窗口里跑的（和 dump 同一时刻），见 _dump_for。
    """
    extra = {}
    main = main_module_of(modules, image_name)
    fp = pe_fingerprint(info.get("image") or "") if info.get("image") else None
    extra["exe"] = {
        "path": info.get("image"),
        "size_of_image": (fp or {}).get("size_of_image"),
        "size_of_image_in_process": main["size"] if main else None,
        "md5": (fp or {}).get("md5"),
        "pe_timestamp": (fp or {}).get("timestamp"),
        "pe_timestamp_utc": (fp or {}).get("timestamp_utc"),
        "file_size": (fp or {}).get("file_size"),
        "machine": (fp or {}).get("machine"),
        "sections": (fp or {}).get("sections"),
        "why": "RVA 能不能用只看 SizeOfImage；md5 只说明这份副本有没有被动过",
        "error": (fp or {}).get("error"),
    }
    return extra


# --------------------------------------------------------------------------- delegates
DELEGATE_STRIDE = 16        # TArray<FScriptDelegate> 一项 = {UObject*(8), FName{idx4,num4}}


def sidecar_of(dump_path: str) -> dict | None:
    """dump 旁边的同名 .json（`now`/`watch` 都会写）。"""
    p = os.path.splitext(dump_path)[0] + ".json"
    return read_json(p)


def _entry_shape(md: MiniDump, addr: int) -> dict | None:
    raw = md.read(addr, DELEGATE_STRIDE)
    if not raw:
        return None
    ptr, split = struct.unpack("<QQ", raw)
    return {"ptr": ptr, "index": split & 0xFFFFFFFF, "number": split >> 32}


def _plausible_entry(e: dict | None) -> bool:
    """「16 字节一项、第二个 dword 是小的非负整数」的判据（FName 那半截）。

    FName = {ComparisonIndex(u32), Number(u32)}；Number 几乎总是 0，
    索引也是几万这个量级 —— 所以这两个都能当"像不像"的筛子。
    """
    return bool(e and e["ptr"] >= 0x10000
                and e["index"] <= 0x00FFFFFF and e["number"] <= 0xFFFF)


def scan_delegates(dump_path: str, md: MiniDump | None = None,
                   registers: dict | None = None, rsp: int | None = None,
                   stack_qwords: int = 256, limit: int = 64,
                   max_candidates: int = 96) -> dict:
    """`analyze --delegates`：在故障线程的寄存器和栈上找多播委托调用列表。

    判据（需求原文）：16 字节一项、第二个 dword 是小的非负整数；对每一项读第一个
    8 字节指针，看它落不落在已提交页里、它指向的对象第一个 8 字节（虚表指针）落不落
    在主模块 .rdata 里。**只给候选**，不追求 100% 准。

    这是"哪个委托实例被释放了"最直接的答案：
    列表里那一项的指针要么不在已提交页里，要么它的"对象"已经没有正常虚表了。
    """
    dump_path = os.path.abspath(dump_path)
    md = md or MiniDump(dump_path)
    sc = sidecar_of(dump_path) or {}
    regs = dict(registers or {})
    if not regs:
        regs = sc.get("registers") or {}
    main = main_module_of(md.modules, DEFAULT_IMAGE)
    main_base = (main or {}).get("base")
    exe = sc.get("exe") or {}
    sections = exe.get("sections")
    if rsp is None:
        rsp = regs.get("rsp")

    cands: list[tuple[str, int]] = []
    for name in ("rdi", "rcx", "rax", "rdx", "rsi", "rbx", "rbp", "r8", "r9", "r10",
                 "r11", "r12", "r13", "r14", "r15"):
        v = regs.get(name)
        if v:
            cands.append((name, v))
    stack_used = 0
    if rsp:
        blob = md.read(rsp, stack_qwords * 8)
        if blob:
            for i in range(0, len(blob), 8):
                v = struct.unpack_from("<Q", blob, i)[0]
                if 0x10000 <= v < 0x7FFFFFFFFFFF:
                    cands.append(("stack[rsp+0x%x]" % (i,), v))
                    stack_used += 1

    out_cands, suspects = [], []
    seen = set()
    for src, base in cands:
        if base in seen or len(out_cands) >= max_candidates:
            continue
        entries = []
        for i in range(limit):
            e = _entry_shape(md, base + i * DELEGATE_STRIDE)
            if not _plausible_entry(e):
                break
            entries.append({"i": i, "address": base + i * DELEGATE_STRIDE, **e})
        if not entries:
            continue
        committed = [md.classify(e["ptr"])["verdict"].startswith(("committed", "unknown"))
                     for e in entries]
        if len(entries) < 2 and all(committed):
            continue                     # 单项且看着正常：多半是巧合
        seen.add(base)
        for e in entries:
            info = md.classify(e["ptr"])
            vt = md.u64(e["ptr"])
            sec = section_of({"sections": sections}, main_base, vt) if vt else None
            if sec is None and vt and main_base:
                m2 = md.module_of(vt)
                sec = {"name": (m2 or {}).get("name") or "?", "offset": None}
            notes = []
            bad_region = not info["verdict"].startswith(("committed", "unknown"))
            if bad_region:
                notes.append("指针不在已提交页里（%s）—— 这一项指向的实例已被释放"
                             % info["verdict"])
            if vt is None:
                notes.append("读不出虚表（对象内存已不可访问/未捕获）")
            elif main_base and not (vt >= main_base and vt < main_base + (1 << 32)):
                notes.append("虚表不在主模块地址区间（vtable=%s → %s）"
                             % (_hex(vt), (sec or {}).get("name")))
            elif sec and sec.get("name") not in (".rdata", ".data", ".text"):
                notes.append("虚表落在主模块的 %s 段（不是 .rdata）" % sec.get("name"))
            e.update({"region_verdict": info["verdict"], "region_protect": info["protect"],
                      "vtable": vt, "vtable_section": (sec or {}).get("name"),
                      "vtable_rva": (vt - main_base) if (vt and main_base) else None,
                      "suspect": bool(notes), "notes": notes})
            if e["suspect"]:
                suspects.append({"address": e["address"], "index": e["i"], "base": base,
                                 "source": src, "ptr": e["ptr"],
                                 "verdict": info["verdict"], "notes": notes})
        out_cands.append({"source": src, "base": base, "count": len(entries),
                          "entries": entries})
    return {
        "registers_used": {k: regs.get(k) for k in
                           ("rdi", "rcx", "rax", "rdx", "rsi", "rbx", "rbp", "rsp")
                           if regs.get(k)},
        "stack_scanned_qwords": stack_used,
        "stack_base": rsp,
        "main_module_base": main_base,
        "rdata_known": bool(sections),
        "candidates": out_cands,
        "suspects": suspects,
        "note": ("rdata_known=False 表示没读到 sidecar .json 里的段表，"
                 "「虚表在不在 .rdata」这条判据退化成了「在不在主模块内」。"),
    }


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
