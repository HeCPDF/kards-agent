#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.build —— 构建指纹与 RVA 表的**唯一真源**。

为什么单独一层
==============
磁盘上有**多份同名** `kards-Win64-Shipping.exe`（多棵安装树），偏移各不相同。
判据只有一条：**SizeOfImage 决定这是哪一份构建、偏移表能不能用**；
md5 只说明这一份副本的字节有没有被动过 —— **同一个构建的两份副本，md5 允许不同**
（本地改动常常只碰 `.rdata` 里的常量：不移动节、不改代码 ⇒ RVA 全部保持有效）。
详见 `exes.py`（本机副本指纹表）。
任何"先 attach 再读"的代码都必须先证明自己 attached 到的是哪一份，否则读出来的全是垃圾。

本模块把"哪个构建 + 哪些 RVA"集中在一处，其余模块只 import 常量，不各自抄一份地址。

只读。本模块不发任何 syscall。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _find_workspace() -> Path:
    """往上找仓库根（认标志目录），**不要数 parents[N]**。

    ★ 2026-09-22 搬家踩过：本模块原来在 `reverse-data/tools/kardsmem/`，
      用的是 `parents[3]`；搬到 `kards-agent/kardsmem/` 后层数变了，
      硬编码的层数当场失效。按标志物找就跟目录深度无关了。
    """
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "reverse-data").is_dir() and (p / ".git").exists():
            return p
    return here.parents[2]


WORKSPACE = Path(os.environ.get("KARDS_WORKSPACE") or _find_workspace())
AGENT_ROOT = Path(__file__).resolve().parents[1]    # kards-agent/
REPORTS_DIR = WORKSPACE / "reverse-data" / "reports"
TOOLS_DIR = AGENT_ROOT                      # 自动化脚本根（`ops.py` 等和 kardsmem 同级）
TOOLS_SUB = AGENT_ROOT / "tools"            # 取材/标定/PE 等工具（2026-09-22 从 reverse-data 搬来）
RE_TOOLS_DIR = WORKSPACE / "reverse-data" / "tools"   # 逆向/静态/第三方工具（FModel、u4pak、idmap…）
SPEC_JSON = REPORTS_DIR / "kards-offsets.json"      # 机器可读规格（人工维护）
# `board_api.py` 是**我们的**代码。它原先寄放在上游 `OCR-Kards-Auto/src/` 里，
# 2026-09-22 搬回自己家 `kards-agent/`（上游是别人的 GPL-3.0 仓库，不该被我们污染）。
BOARD_API_SRC = Path(os.environ.get("KARDS_SRC") or AGENT_ROOT)
# 上游只读引用：模板图、config 基线、`win`/`actions` 这些原语还在那边。
UPSTREAM_OCR = Path(os.environ.get("KARDS_OCR_ROOT") or (WORKSPACE / "OCR-Kards-Auto"))

# --------------------------------------------------------------------------
# 构建指纹：本 build = 唯一与 idmap / SDK dump / IDA 对应的一份
# --------------------------------------------------------------------------
CURRENT = "current"        # 正在跑的这一份（偏移表就是为它写的）
OLD = "old"                # IDA .i64 对应的上一版

BUILDS = {
    CURRENT: {
        "module": "kards-Win64-Shipping.exe",
        "version": "1.60.27292.Steam",    # 命名规则见 reports/GAME-VERSIONS.md：<版本号>.<渠道>
        "image_size": 0x9CC8000,          # toolhelp 报告的 modBaseSize（不是文件大小）
        "exe_size": 160489984,            # 文件字节数
        "md5": "395e470f06837f6e60ce5c53c6df2a22",
        "path_hint": r"D:\SteamLibrary\steamapps\common\KARDS\kards\Binaries\Win64",
        "ue": "5.6.1-44394996 Shipping / ++UE5+Release-5.6-Fork-kards",
    },
    # ---- 下面几份是**本机真实存在的其它副本**，用来解释"为什么指纹对不上" ----
    # ★ 实测（`python -m kardsmem exes`）：同名 exe 有多个文件（几棵安装树 + 备份件），
    #   同一个构建的不同副本 md5 可以不同（本地改动只碰 .rdata 常量）。
    #   判据分工：**SizeOfImage 决定偏移能不能用**，md5 只说明字节有没有被动过 —— 见 exes.py。
    "launcher_157_orig": {
        "module": "kards-Win64-Shipping.exe",
        "version": "1.57.26586.launcher",
        "image_size": 0x9CBB000,
        "exe_size": 160441344,
        "md5": "65866f78b3bc56138f3fa20030659b55",
        "path_hint": r"D:\Kards\game-installs\1.57.26586.launcher\game\kards\Binaries\Win64\kards-Win64-Shipping.exe",
        "ue": "5.6.1（另一个游戏版本 1.57.26586，launcher 渠道）",
        "_note": "这份**原件**就是 IDA 那个 2 GB `.i64` 分析的构建 —— 不是「缺失的第 4 个构建」。"
                 "★ 2026-09-21：整棵树搬到 `D:\\Kards\\game-installs\\1.57.26586.launcher\\`；"
                 "正名留给原版（另一份被本地改动过的同名副本已删除）。"
                 "`.i64` 跟着树一起搬，就在同目录。",
    },
    "launcher_default": {
        "module": "kards-Win64-Shipping.exe",
        "version": "1.58.27125.launcher",
        "image_size": 0x9CC4000,
        "exe_size": 160476160,
        "md5": "7c6a83c7d002d57d3581b87296eda98b",
        "path_hint": r"D:\Kards\game-installs\1.58.27125.launcher\game\kards\Binaries\Win64\kards-Win64-Shipping.exe",
        "ue": "5.6.1 / **1.58.27125 launcher 渠道**（与 Steam 的 1.58.27125 同版本号、不同二进制）",
        "_note": ("★ 2026-09-21：整棵树**复制**进 `D:\\Kards\\game-installs\\1.58.27125.launcher\\`；"
                  "`…\\Games\\KARDS\\default\\` **原地那份保留不删**（Xsolla launcher 的注册表 "
                  "`HKCU\\SOFTWARE\\XSOLLA\\…\\default :: prefix` 指着它）⇒ 同一份会被扫到两次，正常。"
                  "实测生效的 mod pak `card_740th_research_develop_P.pak` 也在这棵树。"
                  "⚠ 目录名 `default` 是 Xsolla 的**分支名**，不是版本号。"),
    },
}
MODULE_NAME = BUILDS[CURRENT]["module"]
BUILD_VERSION = BUILDS[CURRENT]["version"]     # `<版本号>.<渠道>`，与 reports/GAME-VERSIONS.md 同一套命名

# --------------------------------------------------------------------------
# 全局 RVA（本 build）
# --------------------------------------------------------------------------
RVA = {
    "GWorld": 0x08F625B0,
    "GObjects": 0x091FF4E0,
    "GNames_decoy": 0x090E2E28,   # ⚠ 不是名字池本体！见 FNAME_POOL_RVA
    "FNamePool": 0x0911B9C0,      # ★ 真名字池（反汇编 FName::AppendString 得到）
    "FName_AppendString": 0x0137F000,
    "UObject_ProcessEvent": 0x0159CBF0,   # vtable idx 0x4C
}
PROCESS_EVENT_IDX = 0x4C                  # vtable 里 ProcessEvent 的下标

# 原生取值口（用来核对内存读数，本工具链不调用它们）
NATIVE_RVA = {
    "getKreditBySide": 0x4A65030,
    "getKreditSlotBySide": 0x4A65110,
    "getMaxPossibleKredits": 0x4A651F0,
    "getEncryptionKey": 0x4A64330,
    "CanBeTargetted": 0x4A7F910,
    "CanOtherCardBeTargetted": 0x4A7FC10,
    "IsValidHandTarget": 0x4A83370,
    "CanPlayFromHand": 0x4A7FEC0,
    "HasAttackLeft": 0x4A81760,
    "HasMovementLeft": 0x4A82090,
    "CanMoveAndAttackInTheSameTurn": 0x4A7FB70,
    "GetStaticCard": 0x4A8B220,
    "GetAllStaticCards": 0x4A89C90,
    "GetStaticCardNames": 0x4A8B2B0,
}

# 旧 build 对照（只在核对 IDA 里的地址时用，**不要**拿它读当前进程）
RVA_OLD = {
    "GWorld": 0x08F575B0,
    "GObjects": 0x091F4060,
    "GNames_decoy": 0x090D79A8,
    "UObject_ProcessEvent": 0x0159C9F0,
    "getTotalOperationCost_sub": 0x144B10FA0,
}


# --------------------------------------------------------------------------
# UObject / 类身份判据
# --------------------------------------------------------------------------
OFF_UOBJECT_FLAGS = 0x08
OFF_UOBJECT_CLASS = 0x10
OFF_UOBJECT_NAME = 0x18          # FName {i32 comparisonIndex, i32 number}
OFF_UCLASS_SUPER = 0x40
OFF_UCLASS_PROPSIZE = 0x58
FLAG_RF_CDO = 0x10               # obj+0x08 & 0x10 -> ClassDefaultObject

OFF_UWORLD_GAMESTATE = 0x1B0
OFF_UWORLD_PERSISTENT_LEVEL = 0x30
OFF_UWORLD_GAME_INSTANCE = 0x228
OFF_LEVEL_ACTORS = 0xA0
OFF_GI_LOCALPLAYERS = 0x38
OFF_LOCALPLAYER_PC = 0x30

SIZE_UWORLD = 2536
SIZE_AKARDS_GAMESTATE = 912
SIZE_BP_GAMESTATE_BATTLE = 1960
SIZE_BASECARD_OBJECT = 1656
SIZE_BASECARD_ACTOR_CANDIDATES = (0x0F68, 0x0F70)   # ABP_Board_C
SIZE_BASECARD_ACTOR = 0x830                          # ABP_BaseCard_C


@dataclass
class BuildInfo:
    """一次 attach 的构建身份。`ok` 为 False 时**不要**信任何偏移读数。"""
    pid: int = 0
    base: int = 0
    image_size: Optional[int] = None
    module_path: Optional[str] = None
    md5: Optional[str] = None
    file_size: Optional[int] = None
    matched: Optional[str] = None      # BUILDS 里的 key
    ok: bool = False
    md5_match: bool = False            # 字节是否与偏移表目标完全一致（**信息性，不作否决**）
    checks: list = field(default_factory=list)

    def describe(self) -> str:
        return ("pid=%s base=0x%X image=0x%X md5=%s%s matched=%s ok=%s"
                % (self.pid, self.base, self.image_size or 0, self.md5 or "?",
                   "" if (self.md5_match or self.md5 is None) else "(≠表内)",
                   self.matched or "-", self.ok))

    def as_dict(self) -> dict:
        return {"pid": self.pid, "base": self.base, "image_size": self.image_size,
                "md5": self.md5, "file_size": self.file_size, "module_path": self.module_path,
                "matched": self.matched, "ok": self.ok, "md5_match": self.md5_match,
                "checks": list(self.checks)}


def md5_file(path: str, chunk: int = 1 << 20) -> Optional[str]:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(chunk), b""):
                h.update(c)
        return h.hexdigest()
    except OSError:
        return None


def identify(image_size: Optional[int], md5: Optional[str],
             file_size: Optional[int] = None) -> Optional[str]:
    """反查是 BUILDS 里的哪一个。

    ★ **先比 md5（精确到副本），再比 SizeOfImage（定位到构建）**：
    两份同构建的副本 md5 可以不同，这时只能落到"构建"那一层 —— 这已经够用，
    因为**偏移表是按构建给的**。
    """
    if md5:
        for key, b in BUILDS.items():
            if b.get("md5") == md5:
                return key
    if image_size is not None:
        for key, b in BUILDS.items():
            if b.get("image_size") == image_size:
                return key
    return None


def validate(image_size: Optional[int], md5: Optional[str],
             file_size: Optional[int] = None) -> BuildInfo:
    """纯函数版校验（不碰进程），供 selftest 与 `Session.attach()` 用。

    **放行条件只有一条：SizeOfImage 与偏移表目标相同** —— 它直接决定 RVA 有没有效。
    md5 用来记录"这份副本的字节有没有被动过"，**只信息、不否决**：
    本地改动通常只碰 `.rdata` 里的常量，不移动节、不改代码 ⇒ 所有 RVA 照样有效。
    """
    info = BuildInfo(image_size=image_size, md5=md5, file_size=file_size)
    want = BUILDS[CURRENT]
    ok_img = (image_size == want["image_size"])
    ok_md5 = (md5 == want["md5"])
    info.md5_match = ok_md5
    info.matched = identify(image_size, md5, file_size)
    info.checks.append(("image_size", image_size, want["image_size"], ok_img))
    info.checks.append(("md5", md5, want["md5"], ok_md5))
    if file_size is not None:
        info.checks.append(("exe_size", file_size, want["exe_size"],
                            file_size == want["exe_size"]))
    if not ok_img:
        if info.matched and info.matched != CURRENT:
            info.checks.append(("warning", "attach 到的是 %s，偏移表不适用" % info.matched,
                                CURRENT, False))
        return info
    info.ok = True
    if ok_md5:
        info.checks.append(("bytes", "与偏移表目标字节一致", "一致", True))
    elif md5 is None:
        info.checks.append(("bytes", "md5 未校验（调用方跳过）", "跳过", True))
    else:
        info.checks.append(("bytes", "这份副本的字节与偏移表目标不同（本地改动过）",
                            "SizeOfImage 相同 ⇒ RVA 仍有效", True))
    return info


def spec_consistency() -> list:
    """把代码里的常量与 `reports/kards-offsets.json` 对一遍，返回不一致列表。

    规格里的偏移记录照 Dumper-7 的 `OffsetsInfo.json` 格式：
    `build.data` 是 `[[名字, 值], ...]`。所以"防漂移"就是逐条比这张表。
    """
    out = []
    if not SPEC_JSON.exists():
        return [("spec_missing", str(SPEC_JSON), "存在", False)]
    spec = json.loads(SPEC_JSON.read_text(encoding="utf-8"))

    data = (spec.get("build") or {}).get("data")
    if not isinstance(data, list):
        return [("build.data", None, "[[名字, 值], ...]（照 Dumper-7 OffsetsInfo.json）", False)]
    got = {}
    for row in data:
        if isinstance(row, list) and len(row) == 2:
            got[str(row[0])] = row[1]

    cur = BUILDS[CURRENT]
    want = {
        "VERSION": BUILD_VERSION,
        "SIZE_OF_IMAGE": cur["image_size"],
        "EXE_SIZE": cur["exe_size"],
        "MD5": cur["md5"],
        "OFFSET_GWORLD": RVA["GWorld"],
        "OFFSET_GOBJECTS": RVA["GObjects"],
        "OFFSET_GNAMES": RVA["GNames_decoy"],
        "OFFSET_FNAMEPOOL": RVA["FNamePool"],
        "OFFSET_APPENDSTRING": RVA["FName_AppendString"],
        "OFFSET_PROCESSEVENT": RVA["UObject_ProcessEvent"],
        "INDEX_PROCESSEVENT": PROCESS_EVENT_IDX,
        "OLD_OFFSET_GWORLD": RVA_OLD["GWorld"],
        "OLD_OFFSET_GOBJECTS": RVA_OLD["GObjects"],
        "OLD_OFFSET_GNAMES": RVA_OLD["GNames_decoy"],
        "OLD_OFFSET_PROCESSEVENT": RVA_OLD["UObject_ProcessEvent"],
    }
    for name, w in want.items():
        if name in got:
            g = _as_int(got[name])
            if g != w:
                out.append(("%s" % name, g, w, False))
        else:
            out.append(("%s 缺失" % name, None, w, False))
    return out

def _as_int(v):
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return v
    return v
