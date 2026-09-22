#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.exes —— 认 exe：本机每一份 `kards-Win64-Shipping.exe` 的指纹。

为什么必须有这一层
==================
本机有多份**同名** exe（多棵安装树 + 若干备份件），它们的 md5 互不相同。
"这个文件是不是偏移表对应的构建"**只按 SizeOfImage 判断** —— 它直接决定 RVA 有没有效。

★ 判据分工（别混）：

| 判据 | 说明什么 | 用途 |
|---|---|---|
| **SizeOfImage**（PE 头；= 进程里 toolhelp 报的 module size） | 是哪一份**构建** | **决定偏移表能不能用** |
| **md5** | 字节级是否完全一致 | 只说明这份**副本**有没有被动过；同构建的两份副本 md5 允许不同 |
| **文件大小** | 只是个旁证 | 不要拿它和 SizeOfImage 相提并论（曾经因此把"多出来的进程"当悬案） |

所以：**md5 不匹配 ≠ 构建不匹配**。只要 SizeOfImage 对得上，偏移就能用；
`Session.attach()` 也按这条规则放行"同构建、字节不同"的副本（`info.md5_match=False`）。

用法
====
    python -m kardsmem exes            # 本机全部副本的指纹表
    python -m kardsmem exes --json
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from typing import Optional

from . import build as B

# 本机已知的安装树（改机器/换盘要改这里，或看 `--probe <dir>`）
#
# ★ 2026-09-21 搬家：launcher 的两棵树按**游戏版本**收进了工程里
#   `D:\Kards\game-installs\<版本>.<渠道>\`（说明见该目录的 README.md）。
#   `…\Games\KARDS\default\` **原地那份没删** —— Xsolla launcher 的注册表
#   `HKCU\SOFTWARE\XSOLLA\…\default :: prefix` 指着它，删了 launcher 会重下 8 GB。
#   所以 default 与 game-installs\1.58.27125.launcher 是**同一份的两个副本**，
#   下面都列出来，免得"多出来一份"又变成悬案。
KNOWN_TREES = [
    ("steam", r"D:\SteamLibrary\steamapps\common\KARDS\kards\Binaries\Win64"),
    ("1.57.26586.launcher", r"D:\Kards\game-installs\1.57.26586.launcher"
                            r"\game\kards\Binaries\Win64"),
    ("1.58.27125.launcher", r"D:\Kards\game-installs\1.58.27125.launcher"
                            r"\game\kards\Binaries\Win64"),
    ("launcher(default, 原地副本)", r"D:\Program Files\KARDS - The WWII Card Game"
                                    r"\Games\KARDS\default\game\kards\Binaries\Win64"),
]

# ★ 用**子串**匹配，不用精确文件名：同一棵树下出现过 `kards-Win64-Shipping2.exe`
#   这类变体（外加 `.bak` 之类的备份件）。早先用 `EXE_NAME in name` 的精确匹配
#   会**整个漏掉它们** —— 那正是"还有一份对不上"的来源。
SHIPPING_MARK = "shipping"
SKIP_SUFFIXES = (".i64", ".pdb", ".ilk", ".exp", ".lib", ".zip", ".rar", ".7z")


def is_exe_candidate(name: str) -> bool:
    low = name.lower()
    if any(low.endswith(s) for s in SKIP_SUFFIXES):
        return False
    return SHIPPING_MARK in low


# --------------------------------------------------------------------------
def _pe_image_size(path: str) -> Optional[int]:
    """PE 头的 SizeOfImage。优先复用 `tools/pe_tools.py`（PE 解析的唯一实现处）。"""
    if str(B.TOOLS_SUB) not in sys.path:
        sys.path.insert(0, str(B.TOOLS_SUB))
    try:
        import pe_tools                                    # noqa: PLC0415
    except ImportError:
        pe_tools = None
    if pe_tools is not None:
        try:
            _d, _ib, size, _secs = pe_tools.parse_pe(path)
            return size
        except Exception:                                  # noqa: BLE001
            return None
    try:                                                   # 兜底：只读头 0x200
        with open(path, "rb") as f:
            d = f.read(0x200)
        e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            return None
        return struct.unpack_from("<I", d, e_lfanew + 24 + 56)[0]
    except Exception:                                      # noqa: BLE001
        return None


def _md5(path: str, chunk: int = 1 << 22) -> Optional[str]:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(chunk), b""):
                h.update(c)
        return h.hexdigest()
    except OSError:
        return None


def inspect(path: str, do_md5: bool = True) -> dict:
    """一份 exe 的全部事实（不判断"该不该用"，那是 `classify` 的事）。"""
    rec = {"path": path, "name": os.path.basename(path), "exists": os.path.isfile(path)}
    if not rec["exists"]:
        return rec
    rec["size"] = os.path.getsize(path)
    rec["size_of_image"] = _pe_image_size(path)
    rec["md5"] = _md5(path) if do_md5 else None
    rec["matched"] = B.identify(rec["size_of_image"], rec["md5"], rec["size"])
    return rec


def classify(rec: dict) -> dict:
    """给一份 exe 下结论：构建身份 + 字节是否与偏移表目标一致。"""
    if not rec.get("exists"):
        return {"verdict": "missing", "usable": False, "note": "文件不存在"}
    size_img = rec.get("size_of_image")
    md5 = rec.get("md5")
    want = B.BUILDS[B.CURRENT]
    same_build = (size_img == want["image_size"])
    exact = (md5 == want["md5"])
    out = {
        "build": B.identify(size_img, md5, rec.get("size")),
        "size_of_image": size_img,
        "same_build_as_offsets": same_build,
        "byte_exact": exact,
    }
    if not same_build:
        extra = ""
        kb = out["build"]
        if kb and kb in B.BUILDS:
            extra = "；该文件是 **%s**（%s）" % (kb, B.BUILDS[kb].get("ue") or "?")
        note = ("SizeOfImage 0x%s ≠ 偏移表的 0x%s ⇒ **不是**同一份构建，"
                "不要用本偏移表读数%s"
                % (("%X" % size_img) if size_img else "?", "%X" % want["image_size"], extra))
        out.update(verdict="other-build", usable=False, note=note)
    elif md5 is None:
        out.update(verdict="same-build", usable=True,
                   note="SizeOfImage 与偏移表目标相同 ⇒ RVA 有效；md5 本次未校验")
    elif exact:
        out.update(verdict="exact", usable=True, note="字节级与偏移表目标一致")
    else:
        out.update(verdict="same-build-modified", usable=True,
                   note="SizeOfImage 与偏移表目标相同（⇒ RVA 全有效），但字节不同"
                        "（这份副本被本地改动过）—— 读数可用，但要记得它不是原版字节")
    return out


def scan(trees=None, do_md5: bool = True, all_exe: bool = False) -> list:
    """扫若干安装树（`trees=[(标签, 目录), ...]`，默认 `KNOWN_TREES`）。

    `all_exe=True` 时列出目录下**所有** `.exe`（把意料之外的副本也翻出来）。
    """
    rows = []
    for label, d in (trees or KNOWN_TREES):
        if not os.path.isdir(d):
            rows.append({"tree": label, "dir": d, "missing": True,
                         "verdict": {"verdict": "dir-missing", "usable": False,
                                     "note": "目录不存在"}})
            continue
        names = [n for n in sorted(os.listdir(d))
                 if (n.lower().endswith(".exe") or is_exe_candidate(n))
                 and (is_exe_candidate(n) or all_exe)]
        for name in names:
            rec = inspect(os.path.join(d, name), do_md5=do_md5)
            rec["tree"] = label
            rec["verdict"] = classify(rec)
            rows.append(rec)
    return rows


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="本机 kards exe 指纹表")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-md5", action="store_true", help="跳过 md5（快，但认不出字节级差异）")
    ap.add_argument("--probe", metavar="DIR", action="append", default=[],
                    help="额外扫一个目录")
    ap.add_argument("--all", dest="all_exe", action="store_true",
                    help="连目录里所有 .exe 一起列（含与 kards 无关的）")
    a = ap.parse_args(argv)
    trees = list(KNOWN_TREES) + [("--probe", d) for d in a.probe]
    rows = scan(trees, do_md5=not a.no_md5, all_exe=a.all_exe)

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print("★ 偏移表对应的是：image=0x%X size=%d md5=%s"
          % (B.BUILDS[B.CURRENT]["image_size"], B.BUILDS[B.CURRENT]["exe_size"],
             B.BUILDS[B.CURRENT]["md5"]))
    print("★ 规则：**SizeOfImage 决定偏移能不能用**；md5 只说明这份副本的字节有没有被动过")
    print("★ 匹配的是**名字含 shipping 的所有文件**（含 `-Shipping2.exe`、`.bak` 备份件）；"
          "`--all` 连其它 exe 一起列\n")
    for r in rows:
        if r.get("missing"):
            print("[%s] %s  —— %s" % (r["tree"], r.get("dir"), r["verdict"]["note"]))
            continue
        v = r["verdict"]
        print("【%s】%s" % (r["tree"], r["name"]))
        print("   path   : %s" % r["path"])
        print("   size   : %-12s SizeOfImage=%-10s md5=%s"
              % (r.get("size"), ("0x%X" % r["size_of_image"]) if r.get("size_of_image") else "?",
                 r.get("md5") or "(跳过)"))
        print("   判定   : %s  usable=%s  构建=%s"
              % (v.get("verdict"), v.get("usable"), v.get("build") or "-"))
        print("   %s" % v.get("note"))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
