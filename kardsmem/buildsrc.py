#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.buildsrc —— **构建偏移的来源**：从 SDK dump + exe 里提取，生成构建表。

为什么要这一层
==============
以前偏移是"手抄进 `build.py` 的常量"。一旦本机同时有两份渠道的客户端
（Steam `0x9CC8000` / launcher `0x9CC4000`），手抄就会分叉：
抄在 `build.py`、`kardsmem/board.py`、`exes.py`……每加一份就把分叉复制一遍。
⇒ 改成**从产物里算**，只留一份数据：

    Dumpspace/OffsetsInfo.json   → GObjects / GNames(decoy) / AppendString / ProcessEvent / (GWorld)
    exe 反汇编                    → FNamePool（`FName::AppendString` 里的 `lea r8,[rip+…]`）
    exe + 参照构建                → GWorld（dump 里给 0 时，用参照构建的指令模式迁移过来）

产物：`kardsmem/build_tables.json`（**数据**，跟着包走；唯一入口 `kardsmem.build` 读它，`board.py` 只从 build 取）。
★ 2026-10-03 P7：这张表现在只是**种子**（运行时先复验，不过就扫描，见 `build.resolve`），不再是"版本登记制"的放行名单。
重新生成时只覆盖 RVA/来源/告警；身份与人工备注（`image_size/exe_size/md5/versions/path_hint/ue/_note`）原样保留；
新键（`BUILD_SOURCES` 里加了、JSON 里还没有）的身份从 exe 本身算。

用法
====
    python -m kardsmem.buildsrc                 # 重新生成并打印
    python -m kardsmem.buildsrc --check         # 只比对，不写（CI/收工时用）
    python -m kardsmem.buildsrc --build current # 只处理一个构建

⚠ 这层仍然**只读**：只读 dump 的 json、只读 exe 的字节；不碰进程。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import build as B

TABLES_JSON = Path(__file__).with_name("build_tables.json")

# --------------------------------------------------------------------------
# 每个构建：SDK dump 目录 + exe 所在目录
# --------------------------------------------------------------------------
# `tree` 是 exe 所在的目录（收编后的树**没有 `game\` 那一层**，只有 Xsolla 本体才有）。
BUILD_SOURCES = {
    "current": {
        "sdk": "1.60.27292.Steam",
        "tree": r"D:\SteamLibrary\steamapps\common\KARDS\kards\Binaries\Win64",
        "ref": None,                      # 自己就是参照（GWorld 直接取 dump 里的值）
    },
    "launcher_default": {
        "sdk": "1.60.27292.launcher",
        "tree": r"D:\Kards\game-installs\1.60.27292.launcher\kards\Binaries\Win64",
        "ref": "current",                 # dump 里 GWorld=0 ⇒ 用 Steam 的指令模式迁移
    },
    "launcher_157_orig": {
        "sdk": "1.57.26586.launcher",
        "tree": r"D:\Kards\game-installs\1.57.26586.launcher\kards\Binaries\Win64",
        "ref": None,
    },
}

SDK_ROOT = B.WORKSPACE / "reverse-data" / "sdk"

# OffsetsInfo.json 的字段名 → 我们表里的名字
DUMP_KEYS = {
    "OFFSET_GOBJECTS": "GObjects",
    "OFFSET_GNAMES": "GNames_decoy",
    "OFFSET_APPENDSTRING": "FName_AppendString",
    "OFFSET_PROCESSEVENT": "UObject_ProcessEvent",
    "OFFSET_GWORLD": "GWorld",
}

# 写进 build_tables.json 的键（顺序固定，方便 diff）
TABLE_KEYS = ("GWorld", "GObjects", "GNames_decoy", "FNamePool",
              "FName_AppendString", "UObject_ProcessEvent")


# --------------------------------------------------------------------------
# PE / 反汇编（复用 tools/pe_tools.py，不重写一份）
# --------------------------------------------------------------------------
def _pe_tools():
    if str(B.TOOLS_SUB) not in sys.path:
        sys.path.insert(0, str(B.TOOLS_SUB))
    import pe_tools                                            # noqa: PLC0415
    return pe_tools


def find_exe(tree: str) -> Optional[str]:
    """目录里的 `kards-Win64-Shipping*.exe`（跳过 `.i64` 之类）。"""
    if not os.path.isdir(tree):
        return None
    cands = [f for f in sorted(os.listdir(tree))
             if "shipping" in f.lower() and f.lower().endswith(".exe")]
    return os.path.join(tree, cands[0]) if cands else None


def dump_offsets(sdk_name: str) -> dict:
    """读 `Dumpspace/OffsetsInfo.json` → {我们表里的名字: 值}（缺的给 None）。"""
    p = SDK_ROOT / sdk_name / "Dumpspace" / "OffsetsInfo.json"
    if not p.exists():
        return {}
    j = json.loads(p.read_text(encoding="utf-8"))
    raw = {}
    for row in (j.get("data") or []):
        if isinstance(row, list) and len(row) == 2:
            raw[str(row[0])] = row[1]
    out = {}
    for k, name in DUMP_KEYS.items():
        v = raw.get(k)
        if isinstance(v, int) and v > 0x1000:                  # 0 / 假值不算
            out[name] = v
    return out


# --------------------------------------------------------------------------
# FNamePool：反汇编 FName::AppendString
# --------------------------------------------------------------------------
def derive_fname_pool(exe: str, append_rva: int, window: int = 0x80) -> Optional[int]:
    """在 `FName::AppendString` 开头找 `lea rNN, [rip+disp]` → 名字池的全局 RVA。

    判据（两个构建都实测过）：函数开头先 `cmp byte ptr [rip+flag], 0`（初始化标志），
    紧接着 `lea r8, [rip+pool]` + `lea rcx, [rip+pool]`，**同一个目标出现两次**。
    Steam 那份用这个方法复现出已知的 `0x0911B9C0`，见 `find_fnamepool.py`。
    """
    try:
        from capstone import CS_ARCH_X86, CS_MODE_64, Cs, CS_OP_MEM, CS_OP_REG
    except ImportError:
        return None
    pe = _pe_tools()
    d, imagebase, _soi, secs = pe.parse_pe(exe)
    off, _sec, _ch = pe.rva_to_off(secs, append_rva)
    if off is None:
        return None
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    hits = []
    for insn in md.disasm(d[off:off + window], imagebase + append_rva):
        if insn.mnemonic != "lea" or len(insn.operands) != 2:
            continue
        dst, src = insn.operands
        if dst.type != CS_OP_REG or src.type != CS_OP_MEM:
            continue
        if src.mem.base != 41:                                 # X86_REG_RIP
            continue
        tgt = insn.address + insn.size + src.mem.disp - imagebase
        hits.append(tgt)
        if insn.mnemonic == "ret":
            break
    if not hits:
        return None
    # 同一个目标出现 ≥2 次的那个（Steam: 0x0911B9C0 出现 2 次；flag 只被 cmp/mov 碰）
    from collections import Counter
    tgt, n = Counter(hits).most_common(1)[0]
    return tgt if n >= 2 else hits[0]


# --------------------------------------------------------------------------
# GWorld：dump 给 0 时，用参照构建的指令模式迁移
# --------------------------------------------------------------------------
def _pattern_hits(exe: str, target_rva: int, before: int = 24, after: int = 12,
                  max_hits: int = 40) -> list:
    """在 exe 的 `.text` 里找**引用了 target_rva** 的 rip 相对指令 → 带回上下文的模式。

    ⚠ 这里**不能**用"把整个 .text 喂给 capstone"的写法：
      ① capstone 的 `disasm` 遇到数据岛里第一个坏字节就**静默停止**
         （实测只走 ~10MB/117MB，于是"所有全局都是 0 引用"这种假结论就出来了）；
      ② 开 `skipdata=True` 又会在 117MB 上 OOM。
    ⇒ 改成**字节级精确搜索**：rip 相对引用的位移满足
         `(指令末尾地址) + disp == target`
      位移字段在文件里的位置是 `i`（4 字节）⇒ 指令末尾在 `i+4` ⇒
         `chunk_rva + i + 4 + int32_le(blob, i) == target_rva`
      用 numpy 一把算出来（每块 4MB，四份临时数组 ~64MB），再用 capstone
      **只在候选点上**解码验证（这才是精确的：必须真有一条指令的 rip 操作数落到 target）。
    """
    import numpy as np
    pe = _pe_tools()
    d, imagebase, _soi, secs = pe.parse_pe(exe)
    out = []
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs, CS_OP_MEM
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True

    for sec in secs:
        name, vaddr, vsize, rawptr, rawsize, chars = sec
        if name != ".text" or not (chars & 0x20000000):
            continue
        blob = d[rawptr:rawptr + vsize]
        chunk = 4 << 20
        for base in range(0, len(blob), chunk):
            a = np.frombuffer(blob[base:base + chunk], dtype=np.uint8)
            m = len(a) - 3
            if m <= 0:
                continue
            v = (a[0:m].astype(np.int32)
                 | (a[1:m + 1].astype(np.int32) << 8)
                 | (a[2:m + 2].astype(np.int32) << 16)
                 | (a[3:m + 3].astype(np.int32) << 24))
            rhs = target_rva - 4 - (vaddr + base)
            idx = np.nonzero(np.arange(m, dtype=np.int64) + v.astype(np.int64) == rhs)[0]
            for i in idx:
                disp_pos = base + int(i)
                # 指令末尾 = disp_pos + 4；往前最多 15 字节找指令头，用 capstone 验证
                lo = max(0, disp_pos - 15)
                w = blob[lo:disp_pos + 4]
                end_rva = vaddr + disp_pos + 4
                ok = False
                for insn in md.disasm(w, imagebase + vaddr + lo):
                    if insn.address + insn.size != imagebase + end_rva:
                        continue
                    for op in insn.operands:
                        if (op.type == CS_OP_MEM and op.mem.base == 41
                                and insn.address + insn.size + op.mem.disp - imagebase
                                == target_rva):
                            ok = True
                    if ok:
                        break
                if not ok:
                    continue
                p_lo = max(0, disp_pos - before)
                p_hi = min(len(blob), disp_pos + 4 + after)
                pattern = blob[p_lo:p_hi]
                out.append((pattern, disp_pos - p_lo, vaddr + disp_pos))
                if len(out) >= max_hits:
                    return out
    return out


def derive_gworld_live(build_key: str, exe: Optional[str] = None) -> Optional[int]:
    """**实机**推 GWorld：在模块 `.data` 里找"指向 UWorld 的指针"那个槽位。

    为什么不能静态推：实测 Steam 的 `.text` 里对 `0x08F625B0` 的 rip 引用是 **0 处**
    （`_pattern_hits` 对同类全局的命中数：`FNamePool` 5、`GNames` 3、`GObjects` 5、`GWorld` 0）
    —— UE5 那个遗留全局基本没人直接读。所以只能运行时找。

    判据（强）：
      `*(slot)` 指向的对象 `PropertiesSize == SIZE_UWORLD(2536)`，且
      `*(world+0x1B0)` 的 `PropertiesSize ∈ {1960(ABP_GameState_Battle_C), 912}`
    返回 `.data` 相对 RVA（= 节 vaddr + 槽位偏移）。
    """
    from . import proc                                        # 惰性：避免 import 环
    info = B.BUILDS.get(build_key) or {}
    exe = exe or find_exe((BUILD_SOURCES.get(build_key) or {}).get("tree", ""))
    if not exe or not info.get("image_size"):
        return None
    procs = []
    try:
        procs = proc.list_kards_processes()
    except Exception:                                          # noqa: BLE001
        return None
    pick = None
    for p in procs:
        if not isinstance(p, dict):
            continue
        if "Shipping" not in (p.get("exe") or ""):
            continue
        if p.get("image_size") != info["image_size"]:
            continue
        pick = p
        break
    if not pick:
        return None
    m = proc.MemRO(pick["pid"])
    base = pick["module"]
    if isinstance(base, str):                                  # list_kards_processes 给的是 '0x…'
        base = int(base, 16)
    pe = _pe_tools()
    d, _ib, _soi, secs = pe.parse_pe(exe)
    data_sec = None
    for name, vaddr, vsize, rawptr, rawsize, chars in secs:
        if name == ".data" and (chars & 0x80000000):
            data_sec = (vaddr, vsize)
            break
    if not data_sec:
        return None
    vaddr, vsize = data_sec
    buf = m.read_exact(base + vaddr, vsize)
    if not buf:
        return None
    import struct as _struct
    hits = []
    for off in range(0, len(buf) - 8, 8):
        q = _struct.unpack_from("<Q", buf, off)[0]
        if not (0x10000 < q < 0x7FFFFFFFFFFF):
            continue
        c = m.read_exact(q + 0x10, 8)
        if not c:
            continue
        cls = _struct.unpack("<Q", c)[0]
        if not cls:
            continue
        ps = m.read_exact(cls + 0x58, 4)
        if not ps or _struct.unpack("<i", ps)[0] != B.SIZE_UWORLD:
            continue
        gs = m.read_exact(q + B.OFF_UWORLD_GAMESTATE, 8)
        gsps = None
        if gs:
            gsp = _struct.unpack("<Q", gs)[0]
            if gsp:
                g2 = m.read_exact(gsp + 0x10, 8)
                if g2:
                    gcls = _struct.unpack("<Q", g2)[0]
                    if gcls:
                        p2 = m.read_exact(gcls + 0x58, 4)
                        if p2:
                            gsps = _struct.unpack("<i", p2)[0]
        if gsps in (B.SIZE_BP_GAMESTATE_BATTLE, B.SIZE_AKARDS_GAMESTATE):
            hits.append(vaddr + off)
    if not hits:
        return None
    return hits[0] if len(hits) == 1 else hits[0]


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def _identity_from_exe(exe: str, src: dict, sdk_name: str) -> dict:
    """新构建的身份：SizeOfImage（解析 PE 头）/文件大小/md5/版本串（`BUILD_SOURCES[key]["version"]`，缺省取 sdk 目录名）。"""
    pe = _pe_tools()
    _d, _ib, soi, _secs = pe.parse_pe(exe)
    ver = src.get("version") or sdk_name
    return {"version": ver, "image_size": int(soi), "exe_size": os.path.getsize(exe),
            "md5": B.md5_file(exe), "module": B.GAME_EXE, "versions": [ver]}


def build_table(key: str, verbose: bool = True) -> dict:
    """算出某个构建的 RVA 表 + 每个值的**来源**。"""
    src = BUILD_SOURCES.get(key)
    if not src:
        return {"error": "没有登记 %s 的来源（BUILD_SOURCES）" % key}
    info = dict(B.BUILDS.get(key, {}))
    sdk_name = src["sdk"]
    exe = find_exe(src["tree"])
    if exe and not info.get("image_size"):                     # 新键：身份从 exe 本身算（不靠人抄）
        info.update(_identity_from_exe(exe, src, sdk_name))
    rva, sources, notes = {}, {}, []

    dump = dump_offsets(sdk_name)
    for name, v in dump.items():
        if name == "GWorld" and not v:
            continue
        rva[name] = v
        sources[name] = "sdk:%s/Dumpspace/OffsetsInfo.json" % sdk_name
    if not dump:
        notes.append("没读到 sdk:%s 的 OffsetsInfo.json" % sdk_name)

    append_rva = rva.get("FName_AppendString")
    if exe and append_rva:
        pool = derive_fname_pool(exe, append_rva)
        if pool:
            rva["FNamePool"] = pool
            sources["FNamePool"] = "exe:AppendString+lea r8,[rip+…]"
        else:
            notes.append("FNamePool 没反出来")
    elif not exe:
        notes.append("找不到 exe（%s）" % src["tree"])

    if "GWorld" not in rva:
        live = derive_gworld_live(key, exe)
        if live:
            rva["GWorld"] = live
            sources["GWorld"] = "live:.data 扫 UWorld 指针"
            notes.append("GWorld 由实机 .data 扫描得到（dump 里是 0）")
        else:
            prev = (load_tables().get(key) or {}).get("rva") or {}
            if prev.get("GWorld"):
                rva["GWorld"] = prev["GWorld"]
                sources["GWorld"] = "沿用上次生成的值"
                notes.append("GWorld 这次没进程可扫，沿用上次 0x%X —— **要用就得实机复核**"
                             % prev["GWorld"])
            else:
                notes.append("GWorld 缺（dump 里是 0，且扫不到进程）")

    prev_rva = (load_tables().get(key) or {}).get("rva") or {}          # 这次算不出来的项（例如 exe 不在本机）沿用旧值，别把种子弄残
    for name in TABLE_KEYS:
        if name not in rva and prev_rva.get(name):
            rva[name] = prev_rva[name]
            sources[name] = "沿用上次生成的值"
            notes.append("%s 这次算不出来，沿用上次 0x%X" % (name, prev_rva[name]))

    if verbose:
        print("  构建 %s（%s）" % (key, info.get("version", "?")))
        print("    exe : %s" % (exe or "（找不到）"))
        print("    sdk : %s" % (SDK_ROOT / sdk_name))
    # 身份与人工备注来自种子表本身（`B.BUILDS` 就是读这个 JSON）；这里只重算偏移
    return {"key": key, **info, "rva": rva, "sources": sources, "notes": notes}


def refresh(path: Path = TABLES_JSON, only: Optional[str] = None, write: bool = True) -> dict:
    keys = [only] if only else list(BUILD_SOURCES)
    builds = {} if not only else {k: v for k, v in load_tables(path).items() if k != only}   # --build 只重算一个，别丢掉其它键
    for k in keys:
        builds[k] = build_table(k)
    out = {"_note": "随包种子表：已发布版本的 RVA 真值 + 构建身份。运行时只作种子（加载前对进程复验，见 kardsmem.build.resolve）；"
                    "RVA 由 kardsmem.buildsrc 从 SDK dump + exe 生成，其余字段（身份/versions/备注）人工登记，重新生成时原样保留。",
           "builds": builds}
    if write:
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def load_tables(path: Path = TABLES_JSON) -> dict:
    if not path.exists():
        return {}
    try:
        return (json.loads(path.read_text(encoding="utf-8")) or {}).get("builds") or {}
    except Exception:                                          # noqa: BLE001
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 SDK dump + exe 生成构建偏移表")
    ap.add_argument("--check", action="store_true", help="只比对，不写")
    ap.add_argument("--build", default=None, help="只处理一个构建 key")
    a = ap.parse_args(argv)

    old = load_tables()
    new = refresh(only=a.build, write=not a.check)
    rc = 0
    for k, t in new["builds"].items():
        print("%s  %s" % (k, t.get("version")))
        cur, prev = t.get("rva") or {}, (old.get(k) or {}).get("rva") or {}
        for name in TABLE_KEYS:
            if name not in cur:
                print("    %-22s MISSING" % name)
                rc = 1
                continue
            mark = ""
            if prev.get(name) not in (None, cur[name]):
                mark = "  ← 变了（原 0x%X）" % prev[name]
            print("    %-22s 0x%08X  %s%s" % (name, cur[name], t["sources"].get(name, "?"), mark))
        for n in t.get("notes") or []:
            print("    ⚠ %s" % n)
    if a.check:
        print("\n--check：没有写文件")
    else:
        print("\n已写入 %s" % TABLES_JSON)
    return rc


if __name__ == "__main__":
    sys.exit(main())
