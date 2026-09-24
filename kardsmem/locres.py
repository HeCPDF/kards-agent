#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.locres —— 读游戏自带的本地化表（`Game.locres`），把英文翻成中文。

为什么需要它
============
**内存里读出来的字符串一律是英文**（2026-09-23 实机确认，规格 §7.6g / §11.2）：
卡名、规则文本、提示文本、动作流里的 `card_name`，全是英文。
用户客户端是中文 —— 他说「第五步兵旅」，我们读到的是 `5th BRIGADE`。

中文不在卡对象上。摊开卡的 `title@0x58` 的 `FTextData` 看过，`+0x20` 那个 FString
**就是英文**，没有第二份。⇒ 中文只存在于本地化文件里，运行时替换。

表就在仓库里（第一手，不用依赖上游的官网卡表）：

    reverse-data/exports-<build>/kards/Content/Localization/Game/<lang>/Game.locres

怎么把英文接上中文
==================
locres 的 key 是 32 位十六进制的 GUID（UE 自动生成），**不是卡名**，
所以没法直接"按卡名查中文"。但 **en 和 zh-Hans 两份文件的 (namespace, key) 是同一套**
⇒ 两边各读一遍，按 (namespace, key) 对起来，就得到 **英文 → 中文** 的映射。
我们从内存读到的正是英文 ⇒ 直接查这张表。

格式（version 3 = Optimized_CityHash64_UTF16，实测）
====================================================
    magic[16]                 0e147475674a03fc4a15909dc3377f1b
    uint8   version           3
    int64   StringArrayOffset
    uint32  EntriesCount
    uint32  NamespaceCount
    每个 namespace：
        uint32  hash          ★ 实测是 **4 字节**（按 8 字节解会当场对不上）
        FString namespace
        uint32  KeyCount
        每个 key：
            uint32  hash      4 字节
            FString key
            uint32  SourceStringHash
            int32   StringIndex      → 指向字符串表
    StringArrayOffset 处：
        uint32 count，然后每条： FString + uint32 RefCount

    FString：int32 len；len < 0 ⇒ UTF-16LE 共 -len 个字符（含结尾 0），
             否则 UTF-8 共 len 字节（含结尾 0）。

用法
====
    from kardsmem.locres import translator
    zh = translator()                       # 默认 zh-Hans
    zh("5th BRIGADE")                       # '第 5 步兵旅'
    zh("没收录的句子")                       # 原样返回（**不猜**）
"""
from __future__ import annotations

import io
import os
import struct
from typing import Callable, Optional

MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")
DEFAULT_LANG = "zh-Hans"


def _fstring(buf: bytes, o: int):
    (n,) = struct.unpack_from("<i", buf, o)
    o += 4
    if n == 0:
        return "", o
    if n < 0:
        cnt = -n
        return buf[o:o + cnt * 2].decode("utf-16-le", "replace").rstrip("\x00"), o + cnt * 2
    return buf[o:o + n].decode("utf-8", "replace").rstrip("\x00"), o + n


def load(path: str) -> dict:
    """`Game.locres` → {(namespace, key): 文本}。"""
    b = io.open(path, "rb").read()
    if b[:16] != MAGIC:
        raise ValueError("不是 locres 文件（magic 对不上）：%s" % path)
    o = 16
    ver = b[o]
    o += 1
    if ver < 3:
        raise ValueError("只实现了 version>=3，这份是 %d：%s" % (ver, path))
    (str_off,) = struct.unpack_from("<q", b, o)
    o += 8
    o += 4                                     # EntriesCount（只作校验，用不到）
    (ns_count,) = struct.unpack_from("<I", b, o)
    o += 4

    # 先读字符串表
    so = str_off
    (sc,) = struct.unpack_from("<I", b, so)
    so += 4
    strings = []
    for _ in range(sc):
        s, so = _fstring(b, so)
        so += 4                                # RefCount
        strings.append(s)

    out = {}
    for _ in range(ns_count):
        o += 4                                 # namespace hash（4 字节，实测）
        ns, o = _fstring(b, o)
        (kc,) = struct.unpack_from("<I", b, o)
        o += 4
        for _k in range(kc):
            o += 4                             # key hash
            key, o = _fstring(b, o)
            o += 4                             # SourceStringHash
            (idx,) = struct.unpack_from("<i", b, o)
            o += 4
            if 0 <= idx < len(strings):
                out[(ns, key)] = strings[idx]
    return out


def locale_dir(build: Optional[str] = None) -> Optional[str]:
    """找 `exports-<build>/kards/Content/Localization/Game`。找不到返回 None。"""
    from .build import WORKSPACE
    root = os.path.join(str(WORKSPACE), "reverse-data")
    if not os.path.isdir(root):
        return None
    cands = []
    for d in sorted(os.listdir(root)):
        if not d.startswith("exports-"):
            continue
        if build and build not in d:
            continue
        p = os.path.join(root, d, "kards", "Content", "Localization", "Game")
        if os.path.isdir(p):
            cands.append(p)
    return cands[-1] if cands else None


def build_map(lang: str = DEFAULT_LANG, build: Optional[str] = None) -> dict:
    """→ {英文: 目标语言}。两份 locres 按 (namespace, key) 对起来。

    ⚠ 同一句英文在不同 namespace 下可能有**不同**译法。这种冲突一律**丢弃**
      （既不挑第一个也不挑最长的）—— 宁可不翻，也不给一个可能错的译文。
    """
    d = locale_dir(build)
    if not d:
        return {}
    en_p = os.path.join(d, "en", "Game.locres")
    zh_p = os.path.join(d, lang, "Game.locres")
    if not (os.path.isfile(en_p) and os.path.isfile(zh_p)):
        return {}
    en, zh = load(en_p), load(zh_p)
    out, bad = {}, set()
    for k, src in en.items():
        dst = zh.get(k)
        if not src or not dst or src == dst:
            continue
        if src in out and out[src] != dst:
            bad.add(src)
        out[src] = dst
    for s in bad:
        out.pop(s, None)
    return out


_CACHE = {}
_NS_CACHE = {}


def namespace_en(namespace: str, build: Optional[str] = None) -> dict:
    """一个 namespace 下 {key: 英文原文}。★ **不是**卡名/规则文本那种散在各处的字符串，

    是像 `helpbubbles` 这种**内部 key 直接对应英文 UI 文案**的表——`failReason`
    这类内部串本身不是 loc key，查不到；但游戏另外用
    `notify_cant_attack_<...>` / `ability_<...>_title` 这类**前缀+内部串**的
    key 把它们收进了这个 namespace（§7.6g/F10c/F4b 都靠这条路径）。
    """
    key = (namespace, build)
    if key not in _NS_CACHE:
        out = {}
        d = locale_dir(build)
        if d:
            try:
                en = load(os.path.join(d, "en", "Game.locres"))
                out = {k: v for (ns, k), v in en.items() if ns == namespace}
            except Exception:                                # noqa: BLE001
                pass
        _NS_CACHE[key] = out
    return _NS_CACHE[key]


def namespace_zh(namespace: str, key: str, lang: str = DEFAULT_LANG,
                 build: Optional[str] = None) -> Optional[str]:
    """`namespace_en` 里的一条，翻成目标语言。查不到（key 不在表里 / 没有对应译文）
    返回 `None` —— 调用方决定退回什么（原样 / 手写表），这里**绝不编**。"""
    en = namespace_en(namespace, build).get(key)
    if not en:
        return None
    zh = translator(lang, build)(en)
    return zh if zh and zh != en else None


def translator(lang: str = DEFAULT_LANG, build: Optional[str] = None) -> Callable[[str], str]:
    """→ 一个 `英文 -> 目标语言` 的函数。查不到**原样返回**（不猜、不半翻）。"""
    key = (lang, build)
    if key not in _CACHE:
        try:
            _CACHE[key] = build_map(lang, build)
        except Exception:                                    # noqa: BLE001
            _CACHE[key] = {}
    table = _CACHE[key]

    def tr(s):
        if not s:
            return s
        return table.get(s, s)
    tr.table = table                # 调试用：看命中率 / 找没收录的句子
    return tr


def main(argv=None) -> int:
    """`python -m kardsmem.locres [--lang zh-Hans] [句子...]`"""
    import argparse
    ap = argparse.ArgumentParser(description="本地化表（只读文件，不碰游戏）")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--build")
    ap.add_argument("words", nargs="*", help="要翻的英文；不给就抽样展示")
    a = ap.parse_args(argv)
    tr = translator(a.lang, a.build)
    print("表里 %d 条（%s）；来源：%s" % (len(tr.table), a.lang, locale_dir(a.build)))
    if a.words:
        for w in a.words:
            got = tr(w)
            print("  %r -> %s" % (w, repr(got) if got != w else "（表里没有，原样返回）"))
        return 0
    for i, (k, v) in enumerate(tr.table.items()):
        if i >= 8:
            break
        print("  %r -> %r" % (k[:50], v[:50]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
