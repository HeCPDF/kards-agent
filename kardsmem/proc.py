#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.proc —— 只读进程接入层。

红线（用户给的权威前提）
========================
只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`。
**不注入、不 WriteProcessMemory、不远程线程、不 hook。**

本模块**不重复实现** Win32 原语：`OpenProcess/ReadProcessMemory/Toolhelp32`
的唯一实现在 `board_api.py`（**本项目自己的**，就在 `kards-agent/`；已验证、带 30 项 selftest），
这里只是继承它、补上"原子读 / 定长读 / u16,u64"三个缺口。
以前 `mem_probe.py`、`fname_live.py`、`board_api.py` 各自抄了一份 —— 现在只有一份。

用法
====
    from kardsmem import attach
    s = attach()                       # 校验构建指纹，失败就抛
    s.m.i32(s.base + RVA["GWorld"])    # 极薄读写
    s.close()

    带 try 的用法：attach(require_build=False) 允许"进程在跑但构建对不上"，
    此时 `s.info.ok is False`，调用方自己决定要不要用。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
from typing import Optional

from . import build as B

# --------------------------------------------------------------------------
# 复用 board_api 的底层原语（唯一实现处）
# --------------------------------------------------------------------------
if str(B.BOARD_API_SRC) not in sys.path:
    sys.path.insert(0, str(B.BOARD_API_SRC))

try:
    import board_api as board_api          # noqa: E402
except ImportError as e:                   # pragma: no cover - 环境缺失时的明确报错
    raise ImportError(
        "找不到 board_api（预期在 %s）。设 KARDS_SRC 环境变量指向它所在目录。"
        % B.BOARD_API_SRC) from e

_find_pid = board_api._find_pid
_module_of = board_api._module_of
_k32 = board_api._k32
INVALID_HANDLE_VALUE = board_api.INVALID_HANDLE_VALUE
TH32CS_SNAPPROCESS, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x2, 0x8, 0x10

PTR_MIN, PTR_MAX = board_api.PTR_MIN, board_api.PTR_MAX


class ProcessNotFound(RuntimeError):
    pass


class BuildMismatch(RuntimeError):
    pass


class MemRO(board_api._Mem):
    """只读内存访问器：在 board_api._Mem 上补定长读、原子读与整数宽度。

    任何读失败返回 None，**绝不抛**；调用方拿 None 就当作"读不出"。
    """

    def read_exact(self, addr: int, size: int) -> Optional[bytes]:
        """定长读：长度不足视为失败（board_api._Mem.read 会返回短缓冲）。"""
        if not addr or size <= 0:
            return None
        d = self.read(addr, size)
        if d is None or len(d) != size:
            return None
        return d

    def atomic(self, addr: int, size: int, tries: int = 6, gap: float = 0.0015) -> Optional[bytes]:
        """原子读：连续两次读到完全相同的内容才返回。

        ★ 必须用它读"每帧重新随机化的加密记录"：
          GameState 侧值块 `gs+0x330..0x390`、卡牌 `card+0x568..0x5E0`。
          这两个块里的 X/Y 会随帧变化，分字段读会撕裂成垃圾；
          即使一次性读，也可能正好卡在写入中途 —— 所以读两次比对。
        """
        last = None
        for _ in range(max(1, tries)):
            a = self.read_exact(addr, size)
            if a is not None:
                if last is not None and a == last:
                    return a
                last = a
            if gap:
                time.sleep(gap)
        return None

    def u16(self, a):
        d = self.read_exact(a, 2)
        return struct.unpack("<H", d)[0] if d else None

    def i16(self, a):
        d = self.read_exact(a, 2)
        return struct.unpack("<h", d)[0] if d else None

    def u64(self, a):
        d = self.read_exact(a, 8)
        return struct.unpack("<Q", d)[0] if d else None

    def i64(self, a):
        d = self.read_exact(a, 8)
        return struct.unpack("<q", d)[0] if d else None

    def f32(self, a):
        d = self.read_exact(a, 4)
        return struct.unpack("<f", d)[0] if d else None

    def ptr_or_zero(self, a) -> int:
        return self.ptr(a) or 0

    def hexdump(self, addr: int, n: int, label: str = "") -> str:
        """返回十六进制+ASCII 文本（不 print，便于调用方决定去向）。"""
        b = self.read(addr, n)
        if not b:
            return "  <%s> unreadable @0x%X" % (label, addr)
        lines = []
        for off in range(0, len(b), 16):
            row = b[off:off + 16]
            hexs = " ".join("%02X" % c for c in row)
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
            lines.append("  %08X  %-47s  %s" % (off, hexs, txt))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 进程枚举（诊断用：多份 exe 认错人时看这里）
# --------------------------------------------------------------------------
# ★ 结构体直接用 board_api 的那两个：`_k32.Process32First` 的 argtypes 已经按它们注册过，
#   自己再定义一个同名结构体会让 ctypes 报 "expected LP__PROCESSENTRY32 instance"。
_PROCESSENTRY32 = board_api._PROCESSENTRY32
_MODULEENTRY32W = board_api._MODULEENTRY32W


def list_kards_processes() -> list:
    """所有进程名含 kards 的进程 + 其主模块尺寸/md5/是否匹配偏移表。

    这是"务必确认 attach 到哪一份"的落地检查（旧会话曾 attach 到 launcher）。
    """
    out = []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _k32.Process32First(snap, ctypes.byref(e))
        while ok:
            nm = e.szExeFile.decode("mbcs", "replace")
            if "kards" in nm.lower():
                mod = _module_of(e.th32ProcessID, nm)
                rec = {"pid": e.th32ProcessID, "exe": nm, "module": None,
                       "image_size": None, "path": None, "md5": None, "matched": None}
                if mod:
                    base, size, path = mod
                    rec.update({"module": hex(base), "image_size": size, "path": path})
                    from .build import md5_file, identify
                    if path:
                        rec["md5"] = md5_file(path)
                    rec["matched"] = identify(size, rec["md5"])
                out.append(rec)
            ok = _k32.Process32Next(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class Session:
    """一次只读接入。`m` 是内存句柄，`base` 是模块基址，`info` 是构建身份。"""

    def __init__(self, pid: Optional[int] = None, require_build: bool = True,
                 base: Optional[int] = None, module: str = B.MODULE_NAME,
                 validate_md5: bool = True):
        self.pid = pid
        self.module = module
        self.require_build = require_build
        self.validate_md5 = validate_md5
        self.base = 0
        self.m: Optional[MemRO] = None
        self.info = B.BuildInfo()
        self._board_source = None
        self._locator = None
        self._names = None
        if base is not None:
            # 合成目标 / 自检：跳过模块查找与指纹校验
            self.pid = pid or 0
            self.base = base
            self.info = B.BuildInfo(pid=self.pid, base=base, matched="synthetic", ok=True)
            self.info.checks.append(("synthetic", "跳过构建校验", "仅用于自检", True))
            self.m = MemRO(self.pid) if self.pid else None
            return
        self.attach()

    # -- attach ----------------------------------------------------------
    def attach(self) -> "Session":
        pid = self.pid or _find_pid(self.module)
        if not pid:
            raise ProcessNotFound("%s 没在跑（先启动游戏）" % self.module)
        self.pid = pid
        mod = _module_of(pid, self.module)
        if not mod:
            raise ProcessNotFound("在 pid %d 里找不到模块 %s" % (pid, self.module))
        base, size, path = mod
        self.base = base

        md5 = B.md5_file(path) if (path and self.validate_md5) else None
        file_size = None
        try:
            import os
            file_size = os.path.getsize(path) if path else None
        except OSError:
            pass

        # ★ 判据只有 SizeOfImage（决定 RVA 有没有效）。md5 不同不是拒绝理由 ——
        #   同构建的两份副本字节可以不同（本地改动通常只碰 .rdata 常量），
        #   `validate()` 会把它记成 `md5_match=False`，如实但不否决。
        info = B.validate(size, md5, file_size)
        info.pid, info.base, info.module_path = pid, base, path
        self.info = info

        if self.m is None:
            self.m = MemRO(pid)
        if self.require_build and not info.ok:
            self.close()
            raise BuildMismatch(
                "attach 到的不是偏移表对应的构建：%s\n"
                "（期望 image=0x%X md5=%s；实得 image=0x%X md5=%s，matched=%s）"
                % (info.describe(), B.BUILDS[B.CURRENT]["image_size"],
                   B.BUILDS[B.CURRENT]["md5"], size or 0, md5 or "?", info.matched))
        return self

    # -- 便捷 ------------------------------------------------------------
    @property
    def world(self) -> int:
        """`*(UWorld**)(base + GWorld)`（读不出返回 0）。"""
        return self.m.ptr(self.base + B.RVA["GWorld"]) or 0

    def board_source(self):
        """惰性创建一个 board_api 的 mem 后端（盘面/卡牌的唯一读取实现）。"""
        if self._board_source is None:
            src = board_api.MemoryBoardSource(pid=self.pid, validate_build=False)
            src._attach()
            self._board_source = src
        return self._board_source

    def snapshot(self):
        return self.board_source().snapshot()

    # -- 上层视图的便捷入口（惰性 import，避免模块循环依赖） ---------------
    def locator(self):
        """`world.Locator`（对象定位：UWorld/GameState/Board/PC/Actors）。"""
        from .world import Locator
        if self._locator is None:
            self._locator = Locator(self.m, self.base)
        return self._locator

    def gs(self):
        """`gs.GameState`（指挥点/槽位/牌库 id/静态卡表）。"""
        from .gs import GameState
        return GameState(self)

    def names_pool(self):
        """`names.FNamePool`（FName → 字符串）。**注意**：实例带布局探测缓存，复用同一个。"""
        from .names import FNamePool
        if self._names is None:
            self._names = FNamePool(self.m, self.base)
            self._names.probe()
        return self._names

    def rendered(self, **kw):
        """`rendered.rendered_cards(self)`：屏幕上摆着的每一张卡（不读图）。"""
        from .rendered import rendered_cards
        return rendered_cards(self, **kw)

    def pick(self):
        """`pick.pick_state(self)`：是不是在等我选牌。"""
        from .pick import pick_state
        return pick_state(self)

    def cards_raw(self, **kw):
        """`cards.enumerate_raw(self)`：`AllCardsInBattle` → [(map_key, ptr)]。"""
        from .cards import enumerate_raw
        return enumerate_raw(self, **kw)

    def card_raw(self, ptr: int, **kw):
        """`cards.read_raw(self, ptr)`：单卡全部原始字段。"""
        from .cards import read_raw
        return read_raw(self, ptr, **kw)

    def deck(self, side: str = "local"):
        """物理牌库（含同名多份）。"""
        from .cards import deck_cards
        return deck_cards(self, side)

    def describe(self) -> str:
        return "mem(%s)" % self.info.describe()

    def close(self):
        if self._board_source:
            try:
                self._board_source.close()
            except Exception:
                pass
            self._board_source = None
        self._locator = None
        self._names = None
        if self.m:
            self.m.close()
            self.m = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


_default: Optional[Session] = None


def attach(pid: Optional[int] = None, require_build: bool = True,
           reuse: bool = True, **kw) -> Session:
    """接入正在跑的 KARDS（只读）。默认校验构建指纹，失败抛 BuildMismatch。"""
    global _default
    if reuse and _default is not None and pid is None and _default.base:
        s = _default
        if s.m and s.board_source().available():
            return s
        _default = None
    s = Session(pid=pid, require_build=require_build, **kw)
    if pid is None:
        _default = s
    return s


def probe(pid: Optional[int] = None) -> dict:
    """给 CLI 用的"能不能读"总结：进程状态 + 构建身份 + GWorld 好不好使。

    这里**做** md5 校验（读一遍 160 MB，约 1 秒）—— `verify` 的结论要以它为准；
    `require_build=False` 表示"校验失败也把结果报出来"，而不是抛异常。
    """
    out = {"processes": list_kards_processes(), "session": None, "error": None}
    try:
        s = Session(pid=pid, require_build=False, validate_md5=True)
    except ProcessNotFound as e:
        out["error"] = str(e)
        return out
    info = s.info.as_dict()
    world = s.world
    info["gworld"] = hex(world)
    info["gworld_ok"] = False
    if world:
        from .world import Locator
        loc = Locator(s.m, s.base)
        info["gworld_ok"] = loc.is_uword(world)
        info["gamestate"] = hex(loc.gamestate or 0)
        info["gamestate_size"] = loc.gamestate_size
        info["in_battle"] = loc.in_battle
        from .gs import GameState
        info["match_active"] = GameState(s).match_active
    out["session"] = info
    s.close()
    return out
