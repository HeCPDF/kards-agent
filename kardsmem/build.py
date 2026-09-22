#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.build —— 构建指纹与 RVA 表的**唯一真源**。

为什么单独一层
==============
磁盘上有**多份同名** `kards-Win64-Shipping.exe`（多棵安装树），偏移各不相同；
私服 URL 补丁还会在不改 RVA 的前提下改掉 md5 —— **同一个构建的 md5 也会不同**。
所以"这个文件是不是偏移表对应的构建"必须按 **SizeOfImage** 判断，
详见 `exes.py` 与 `reverse-data/reports/EXE-IDENTITY.md`。
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
TOOLS_DIR = AGENT_ROOT                      # 执行侧脚本（ops.py 等）现在和 kardsmem 同级
RE_TOOLS_DIR = WORKSPACE / "reverse-data" / "tools"   # 逆向工具仍在那边
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
        "image_size": 0x9CC8000,          # toolhelp 报告的 modBaseSize（不是文件大小）
        "exe_size": 160489984,            # 文件字节数
        "md5": "395e470f06837f6e60ce5c53c6df2a22",
        "path_hint": r"D:\SteamLibrary\steamapps\common\KARDS\kards\Binaries\Win64",
        "ue": "5.6.1-44394996 Shipping / ++UE5+Release-5.6-Fork-kards",
    },
    # ---- 下面几份是**本机真实存在的其它副本**，用来解释"为什么指纹对不上" ----
    # ★ 2026-09-21 实测（`python -m kardsmem exes`）：
    #   同名 exe 有 **4 个文件**（3 棵树 + 1 个 .orig-backup），其中一份被改过**服务器 URL**
    #   ⇒ **同一个构建也会有不同的 md5**。判据分工见 exes.py / reports/EXE-IDENTITY.md。
    "launcher_157_orig": {
        "module": "kards-Win64-Shipping.exe",
        "image_size": 0x9CBB000,
        "exe_size": 160441344,
        "md5": "65866f78b3bc56138f3fa20030659b55",
        "path_hint": r"D:\Kards\game-installs\1.57.26586.launcher\game\kards\Binaries\Win64\kards-Win64-Shipping.exe",
        "ue": "5.6.1（另一个游戏版本 1.57.26586，launcher 渠道）",
        "_note": "这份**原件**就是 IDA 那个 2 GB `.i64` 分析的构建 —— 不是「缺失的第 4 个构建」。"
                 "★ 2026-09-21：整棵树搬到 `D:\\Kards\\game-installs\\1.57.26586.launcher\\`，"
                 "且**原件已接管正名**（原来正名是 patch 版、原件叫 `.exe.orig-backup`，现已对调，"
                 "patch 版删除）。`.i64` 跟着树一起搬，就在同目录。",
    },
    "launcher_157_patched": {
        "module": "kards-Win64-Shipping.exe",
        "image_size": 0x9CBB000,
        "exe_size": 160441344,
        "md5": "201773bc49f52ff8b9e6c8aae172ae03",
        "path_hint": r"（按需生成）…\1.57.26586.launcher\game\…\kards-Win64-Shipping.patched.exe",
        "ue": "5.6.1（另一个游戏版本 1.57.26586，launcher 渠道）",
        "_note": "★ **同一构建 + 私服 URL 补丁**：文件大小/SizeOfImage 与 launcher_157_orig 完全相同，"
                 "只有 `.rdata` 里两个 URL 字面量被替换成 http://127.0.0.1:5231/ ⇒ md5 必然不同。"
                 "RVA 没动，所以偏移表照样能用。"
                 "⚠ 2026-09-21：磁盘上**已删除**，正名留给原版。需要时用 "
                 "`python D:\\Kards\\server\\tools\\patch_exe.py <原版exe> --root http://127.0.0.1:5231/ --apply` "
                 "重新生成，产物是同目录的 `kards-Win64-Shipping.patched.exe`（脚本不再改源文件）。"
                 "本条目保留是为了认出这个 md5。",
    },
    "launcher_default": {
        "module": "kards-Win64-Shipping.exe",
        "image_size": 0x9CC4000,
        "exe_size": 160476160,
        "md5": "7c6a83c7d002d57d3581b87296eda98b",
        "path_hint": r"D:\Kards\game-installs\1.58.27125.launcher\game\kards\Binaries\Win64\kards-Win64-Shipping.exe",
        "ue": "5.6.1 / **1.58.27125 launcher 渠道**（与 Steam 的 1.58.27125 同版本号、不同二进制）",
        "_note": ("★ 2026-09-21：整棵树**复制**进 `D:\\Kards\\game-installs\\1.58.27125.launcher\\`；"
                  "`…\\Games\\KARDS\\default\\` **原地那份保留不删**（Xsolla launcher 的注册表 "
                  "`HKCU\\SOFTWARE\\XSOLLA\\…\\default :: prefix` 指着它）⇒ 同一份会被扫到两次，正常。"
                  "原来旁边的 `kards-Win64-Shipping2.exe`（私服补丁版）**已删除**。"
                  "实测生效的 mod pak `card_740th_research_develop_P.pak` 也在这棵树。"
                  "⚠ 目录名 `default` 是 Xsolla 的**分支名**，不是版本号。"),
    },
    "launcher_default_patched": {
        "module": "kards-Win64-Shipping.patched.exe",
        "image_size": 0x9CC4000,
        "exe_size": 160476160,
        "md5": "724728d23007c6c4a087f03d384e5465",
        "path_hint": r"（按需生成）…\1.58.27125.launcher\game\…\kards-Win64-Shipping.patched.exe",
        "ue": "5.6.1 / 1.58.27125 launcher 渠道",
        "_note": ("★ **同一构建 + 私服 URL 补丁**（文件长度/SizeOfImage 与 launcher_default 完全相同，"
                  "只有两个 URL 字面量被换成 http://127.0.0.1:5231/）。"
                  "⚠ 2026-09-21：原来那份叫 `kards-Win64-Shipping2.exe`，**已删除**，正名留给原版。"
                  "需要时用 `patch_exe.py <原版exe> --root … --apply` 重新生成，"
                  "产物固定叫 `kards-Win64-Shipping.patched.exe`（md5 应当还是 724728d2…）。"),
    },
}
MODULE_NAME = BUILDS[CURRENT]["module"]

# 被改过服务器 URL 的已知副本：它们的 md5 与"原件"不同，但**构建相同**。
# `build.validate()` 与 `Session.attach()` 允许这类副本（记 patched=True）。
KNOWN_URL_PATCHED = {"launcher_157_patched", "launcher_default_patched"}

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
    patched: bool = False              # 被改过服务器 URL（同构建，RVA 未变）
    orig_md5: Optional[str] = None     # 旁边 .orig-backup 的 md5（= 干净指纹）
    checks: list = field(default_factory=list)

    def describe(self) -> str:
        return ("pid=%s base=0x%X image=0x%X md5=%s matched=%s ok=%s%s"
                % (self.pid, self.base, self.image_size or 0, self.md5 or "?",
                   self.matched or "-", self.ok,
                   ("  patched(URL)" if self.patched else "")))

    def as_dict(self) -> dict:
        return {"pid": self.pid, "base": self.base, "image_size": self.image_size,
                "md5": self.md5, "file_size": self.file_size, "module_path": self.module_path,
                "matched": self.matched, "ok": self.ok, "patched": self.patched,
                "orig_md5": self.orig_md5, "checks": list(self.checks)}


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

    ★ **先比 md5，再比 SizeOfImage**：md5 能区分"同构建 + URL 补丁"的孪生副本，
    而 SizeOfImage 只能定位到**构建**（所以 `.orig-backup` 与补丁版会映到同一个构建族）。
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
             file_size: Optional[int] = None, patched: Optional[bool] = None,
             orig_md5: Optional[str] = None) -> BuildInfo:
    """纯函数版校验（不碰进程），供 selftest 与 `Session.attach()` 用。

    `patched=True` 表示"这份 exe 被改过服务器 URL"（同构建、RVA 未变）——
    此时 md5 必然与原件不同，但**偏移表照样有效**，所以放行并如实标注。
    """
    info = BuildInfo(image_size=image_size, md5=md5, file_size=file_size,
                     orig_md5=orig_md5, patched=bool(patched))
    want = BUILDS[CURRENT]
    ok_img = image_size == want["image_size"]
    ok_md5 = (md5 == want["md5"])
    info.matched = identify(image_size, md5, file_size)
    info.checks.append(("image_size", image_size, want["image_size"], ok_img))
    info.checks.append(("md5", md5, want["md5"], ok_md5))
    if file_size is not None:
        info.checks.append(("exe_size", file_size, want["exe_size"],
                            file_size == want["exe_size"]))
    if ok_img and (ok_md5 or md5 is None):
        info.ok = True
    elif ok_img and patched:
        # 同构建 + 服务器 URL 补丁：md5 不同是**预期**的，URL 只在 .rdata 里，RVA 没动
        info.ok = True
        info.checks.append(
            ("url_patched", "已打私服 URL 补丁（SizeOfImage 相同 ⇒ RVA 未变，偏移有效）",
             "允许读数", True))
        if orig_md5:
            info.checks.append(("orig_md5(.orig-backup)", orig_md5, want["md5"],
                                orig_md5 == want["md5"]))
    if info.matched and info.matched not in (CURRENT,) and md5 and not info.ok:
        info.checks.append(("warning", "attach 到的是 %s，偏移表不适用" % info.matched,
                            CURRENT, False))
    return info


def spec_consistency() -> list:
    """把代码里的常量与 `reports/kards-offsets.json` 对一遍，返回不一致列表。

    "整理"的一半工作是防止**文档与代码再次漂移** —— 这个函数就是那道闸。
    """
    out = []
    if not SPEC_JSON.exists():
        return [("spec_missing", str(SPEC_JSON), "存在", False)]
    spec = json.loads(SPEC_JSON.read_text(encoding="utf-8"))

    def note(name, got, want):
        if want is not None and got != want:
            out.append((name, got, want, False))

    g = spec.get("globals_rva", {})
    for key, js_key in (("GWorld", "GWorld"), ("GObjects", "GObjects"),
                        ("GNames_decoy", "GNames"), ("FNamePool", "FNamePool")):
        if js_key in g:
            note("globals_rva.%s" % js_key, RVA[key], _as_int(g[js_key]))
    fp = spec.get("fname_pool", {})
    if isinstance(fp.get("address_rva"), str):
        note("fname_pool.address_rva", RVA["FNamePool"], _as_int(fp["address_rva"]))
    b = spec.get("build", {})
    for key, js_key in (("image_size", "image_size"), ("exe_size", "exe_size"), ("md5", "md5")):
        if js_key in b:
            note("build.%s" % js_key, BUILDS[CURRENT][key],
                 _as_int(b[js_key]) if key != "md5" else b[js_key])
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
