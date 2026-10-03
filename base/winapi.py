# -*- coding: utf-8 -*-
"""base.winapi —— 找游戏窗口、客户区坐标换算、后台截图（不需要窗口在前台，不动鼠标、不抢焦点）。

只给**诊断 / 截图**用：游戏的读侧走进程内存（`kardsmem`），写侧走进程内合成事件（`ops`），都不依赖窗口位置。
截图用 `PrintWindow(PW_RENDERFULLCONTENT)` 拿 DWM 合成后的内容；窗口被别的窗口盖住时抓到的仍是游戏自己
（不是盖在上面的东西）。拿不到有效内容（全黑 / 最小化）就返回 None，由调用方决定怎么办——不悄悄退回屏幕截图。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from typing import Optional

GAME_EXE = "kards-win64-shipping.exe"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]

PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_user32.GetDC.argtypes = [wt.HWND]
_user32.GetDC.restype = wt.HDC
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsIconic.argtypes = [wt.HWND]
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wt.HDC]
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def set_dpi_aware() -> None:
    """让坐标是真实像素（不同缩放比下客户区尺寸才和游戏内一致）。重复调用无害。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                         # noqa: BLE001
        try:
            _user32.SetProcessDPIAware()
        except Exception:                                     # noqa: BLE001
            pass


def _exe_of_pid(pid: int) -> str:
    h = _kernel32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wt.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _kernel32.CloseHandle(h)


def find_game_windows(exe: str = GAME_EXE) -> list:
    """游戏进程的顶层可见窗口 → `[{"hwnd", "pid", "w", "h"}]`（客户区尺寸，面积大的在前）。"""
    out = []
    exe = exe.lower()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(pid.value) != exe:
            return True
        r = wt.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w > 0 and h > 0:
            out.append({"hwnd": int(hwnd), "pid": pid.value, "w": w, "h": h})
        return True
    _user32.EnumWindows(cb, 0)
    return sorted(out, key=lambda d: -d["w"] * d["h"])


def find_game_hwnd(exe: str = GAME_EXE) -> Optional[int]:
    ws = find_game_windows(exe)
    return ws[0]["hwnd"] if ws else None


def client_size(hwnd: int) -> tuple:
    r = wt.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def client_to_screen(hwnd: int, x: int, y: int) -> tuple:
    p = wt.POINT(int(x), int(y))
    _user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def is_foreground(hwnd: int) -> bool:
    return int(_user32.GetForegroundWindow() or 0) == int(hwnd)


def capture_client_bgr(hwnd: int):
    """窗口客户区 → `numpy.ndarray(H, W, 3)` BGR；最小化 / 抓到全黑 / 失败 ⇒ None。不需要前台。"""
    import numpy as np
    if _user32.IsIconic(hwnd):
        return None
    w, h = client_size(hwnd)
    if w <= 0 or h <= 0:
        return None
    hdc_win = _user32.GetDC(hwnd)
    hdc = _gdi32.CreateCompatibleDC(hdc_win)
    bmp = _gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = _gdi32.SelectObject(hdc, bmp)
    try:
        ok = _user32.PrintWindow(hwnd, hdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            return None
        bi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        got = _gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        if got != h:
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()
        if int(arr.max()) == 0:                                # 全黑 = 没抓到内容
            return None
        return arr
    finally:
        _gdi32.SelectObject(hdc, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(hdc)
        _user32.ReleaseDC(hwnd, hdc_win)
