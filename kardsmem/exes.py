#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.exes —— 认 exe：本机每一份 `kards-Win64-Shipping.exe` 的指纹与**补丁状态**。

为什么必须有这一层
==================
本机有多份**同名** exe，而且**有几份被人改过服务器 URL**（私服补丁：
把 `https://kards.live.1939api.com[/config]` 换成 `http://127.0.0.1:5231/` + NUL 填充，
工具是 `D:\\Kards\\server\\tools\\patch_exe.py`，旁边留 `.orig-backup`）。
于是**同一个构建**的两份副本会有**不同的 md5** —— 这就是"为什么对不上"。

★ 判据分工（别混）：

| 判据 | 说明什么 | 用途 |
|---|---|---|
| **SizeOfImage**（PE 头；= 进程里 toolhelp 报的 module size） | 是哪一份**构建** | **决定偏移表能不能用**。URL 补丁只改 `.rdata` 里的字符串常量：不移动节、不改代码 ⇒ **所有 RVA 保持有效** |
| **md5** | 字节级是否完全一致 | 判断有没有被动过（含补丁） |
| **URL 字面量**（live / 本地） | 这份有没有被打过私服补丁 | 见 `server/tools/patch_exe.py`；`.orig-backup` 是打补丁前的原件 |

所以：**md5 不匹配 ≠ 构建不匹配**。只要 SizeOfImage 对得上，偏移就能用；
`Session.attach()` 也按这条规则放行"同构建 + URL 补丁版"（记 `patched=True`）。

用法
====
    python -m kardsmem exes            # 本机全部副本的指纹/补丁状态表
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

# ★ 用**子串**匹配，不用精确文件名：历史上本机出现过 `kards-Win64-Shipping2.exe`
#   （default 树下那份打了私服补丁的副本）。早先用 `EXE_NAME in name` 的精确匹配
#   会**整个漏掉它** —— 这正是"还有一份对不上"的来源。子串匹配保留着，因为
#   `patch_exe.py` 现在产出的是 `kards-Win64-Shipping.patched.exe`（同样要被扫到）。
#   下面同时支持 `.orig-backup` / `.bak` / `-Copy` 之类的后缀。
#   ⚠ 2026-09-21 起磁盘上**不再常驻** patch 版：正名一律是原版，patch 版按需生成。
SHIPPING_MARK = "shipping"
SKIP_SUFFIXES = (".i64", ".pdb", ".ilk", ".exp", ".lib", ".zip", ".rar", ".7z")
LIVE_MARKER = b"kards.live.1939api.com"
LIVE_ROOT = b"https://kards.live.1939api.com/"
LIVE_CONFIG = b"https://kards.live.1939api.com/config"
LOCAL_MARKERS = (b"127.0.0.1:5231", b"localhost:5231", b"127.0.0.1:5232")
PATCH_TOOL = r"D:\Kards\server\tools\patch_exe.py"


def is_exe_candidate(name: str) -> bool:
    low = name.lower()
    if any(low.endswith(s) for s in SKIP_SUFFIXES):
        return False
    return SHIPPING_MARK in low


# --------------------------------------------------------------------------
def _pe_image_size(path: str) -> Optional[int]:
    """PE 头的 SizeOfImage。复用 `tools/pe_tools.py`（PE 解析的唯一实现处）。"""
    if str(B.TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(B.TOOLS_DIR))
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


def _count(data: bytes, needle: bytes, cap: int = 8) -> list:
    out, start = [], 0
    while len(out) < cap:
        i = data.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + 1
    return out


def _url_state(path: str) -> dict:
    """扫 URL 字面量：live 有没有、本地有没有、各在哪些文件偏移。

    计数用**不含 scheme/后缀**的 `kards.live.1939api.com`：`.../config` 那条字面量
    以 root 为前缀，用整串去数会把同一条算两次（早期版本就吃过这个亏）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"error": str(e)}
    live = sorted(_count(data, LIVE_MARKER))
    local = []
    for m in LOCAL_MARKERS:
        local += _count(data, m)
    return {"live": live, "local": sorted(local),
            "live_n": len(live), "local_n": len(local),
            "patched": bool(local) and not live}


def inspect(path: str, do_md5: bool = True) -> dict:
    """一份 exe 的全部事实（不判断"该不该用"，那是 `classify` 的事）。"""
    rec = {"path": path, "name": os.path.basename(path), "exists": os.path.isfile(path)}
    if not rec["exists"]:
        return rec
    rec["size"] = os.path.getsize(path)
    rec["size_of_image"] = _pe_image_size(path)
    rec["md5"] = _md5(path) if do_md5 else None
    rec["url"] = _url_state(path)
    rec["matched"] = B.identify(rec["size_of_image"], rec["md5"], rec["size"])
    backup = path + ".orig-backup"
    rec["orig_backup"] = None
    if os.path.isfile(backup):
        b = {"path": backup, "size": os.path.getsize(backup),
             "size_of_image": _pe_image_size(backup),
             "md5": _md5(backup) if do_md5 else None}
        b["matched"] = B.identify(b["size_of_image"], b["md5"], b["size"])
        rec["orig_backup"] = b
    return rec


def classify(rec: dict) -> dict:
    """给一份 exe 下结论：构建身份 + 补丁状态 + 偏移表能不能用。"""
    if not rec.get("exists"):
        return {"verdict": "missing", "usable": False, "note": "文件不存在"}
    size_img = rec.get("size_of_image")
    md5 = rec.get("md5")
    url = rec.get("url") or {}
    want = B.BUILDS[B.CURRENT]
    orig = rec.get("orig_backup")
    effective_md5 = md5
    src = "自身"
    if md5 != want["md5"] and orig and orig.get("md5"):
        # 打补丁后 md5 变了；旁边备份才是"干净"指纹
        if orig["md5"] == want["md5"]:
            effective_md5, src = orig["md5"], ".orig-backup"
    same_build = (size_img == want["image_size"])
    exact = (md5 == want["md5"])
    out = {
        "build": B.identify(size_img, effective_md5, rec.get("size")),
        "matched_by": src,
        "size_of_image": size_img,
        "same_build_as_offsets": same_build,
        "byte_exact": exact,
        "url_patched": bool(url.get("patched")),
        "url_note": ("live×%d 本地×%d" % (url.get("live_n", 0), url.get("local_n", 0))),
        "backup": (orig or {}).get("md5"),
    }
    if exact:
        out.update(verdict="exact", usable=same_build,
                   note="字节级与偏移表一致")
    elif same_build and out["url_patched"]:
        out.update(verdict="same-build-url-patched", usable=True,
                   note=("同一构建 + 私服 URL 补丁（RVA 不变 ⇒ 偏移照用；md5 必然不同）。"
                         "原件指纹见 .orig-backup。补丁工具：%s" % PATCH_TOOL))
    elif same_build:
        out.update(verdict="same-build-modified", usable=True,
                   note="SizeOfImage 相同但字节与偏移表不同（非 URL 补丁）—— 偏移仍有效，但要留意改动内容")
    else:
        kb = out["build"]
        extra = ""
        if kb and kb in B.BUILDS:
            extra = "；该文件是 **%s**（%s）" % (kb, B.BUILDS[kb].get("ue") or "?")
            if kb in B.KNOWN_URL_PATCHED:
                extra += "，且已打过私服 URL 补丁"
        note = ("SizeOfImage 0x%s ≠ 偏移表的 0x%s ⇒ **不是**同一份构建，"
                "不要用本偏移表读数%s"
                % (("%X" % size_img) if size_img else "?", "%X" % want["image_size"], extra))
        out.update(verdict="other-build", usable=False, note=note)
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
            rec["is_backup"] = name.lower().endswith((".orig-backup", ".bak", ".backup"))
            rec["verdict"] = classify(rec)
            rows.append(rec)
        # 同目录里"同大小、未打补丁"的孪生文件 = 补丁版的原件候选（没有 .orig-backup 时靠它）
        for r in rows:
            if r.get("missing") or r.get("tree") != label:
                continue
            twins = [o for o in rows
                     if o.get("tree") == label and o is not r
                     and o.get("size") == r.get("size")
                     and not (o.get("url") or {}).get("patched")
                     and not (o.get("verdict") or {}).get("url_patched")]
            r["sibling_clean"] = [{"name": t["name"], "md5": t.get("md5"),
                                   "path": t["path"]} for t in twins]
    return rows


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="本机 kards exe 指纹与补丁状态")
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
    print("★ 规则：SizeOfImage 决定偏移能不能用；md5 只说明字节有没有被动过"
          "（URL 补丁会改 md5 但不动 RVA）")
    print("★ 匹配的是**名字含 shipping 的所有文件**（含 `-Shipping2.exe`、`.orig-backup`、`.bak`）；"
          "`--all` 连其它 exe 一起列\n")
    for r in rows:
        if r.get("missing"):
            print("[%s] %s  —— %s" % (r["tree"], r.get("dir"), r["verdict"]["note"]))
            continue
        v = r["verdict"]
        print("【%s】%s" % (r["tree"], r["name"]))
        print("   path   : %s" % r["path"])
        print("   size   : %-12s SizeOfImage=%-10s md5=%s%s"
              % (r.get("size"), ("0x%X" % r["size_of_image"]) if r.get("size_of_image") else "?",
                 r.get("md5") or "(跳过)", "  ← 备份件" if r.get("is_backup") else ""))
        print("   url    : %s" % v.get("url_note"))
        if r.get("orig_backup"):
            b = r["orig_backup"]
            print("   backup : %s  md5=%s" % (b["path"], b.get("md5")))
        if r.get("sibling_clean") and (v.get("url_patched") or r.get("is_backup")):
            for t in r["sibling_clean"]:
                print("   原件候选: %s  md5=%s  （同目录同大小、未打补丁）" % (t["name"], t["md5"]))
        print("   判定   : %s  usable=%s" % (v.get("verdict"), v.get("usable")))
        print("   %s" % v.get("note"))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
