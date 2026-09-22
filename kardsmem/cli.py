#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""kardsmem.cli —— 内存侧工具链的统一命令行。

    python -m kardsmem <命令> [参数]

命令一览（全部只读）
====================
| 命令 | 作用 |
|---|---|
| `verify`     | 进程 + 构建指纹 + 定位链 + 文档/代码常量一致性 |
| `procs`      | 列出所有 kards 进程（多份同名 exe 认错人时看这里） |
| `state`      | 归一化盘面（手牌/场上/弃牌/牌库/HQ/指挥点/回合） |
| `hand [side]`| 手牌（默认 local），按 `locationNumber` 排 |
| `cards [--raw [UID]]` | 盘面卡；`--raw` 摊开单卡全部原始字段 |
| `discard [side]` | 弃牌堆 |
| `gs [--full]`| GameState 原始字段；`--full` 含牌库 id / 静态表 / 重连表 |
| `names`      | FNamePool 探测 + 解析示例（R5） |
| `rendered`   | 屏幕上摆着的每一张卡（`ABP_BaseCard_C`，不读图） |
| `pick`       | 是不是在等我选牌（抉择/预报/部署后选目标） |
| `candidates` | 选择界面的候选 + 与盘面卡的配对 |
| `targets <id\|名字>` | 读侧近似的"可指向目标"（**近似**，非权威） |
| `dump [--out F]` | 聚合快照（JSON），回归样本就存这个 |
| `selftest`   | 离线断言（含 board_api 的 34 项）+ 实机探测 |

退出码：0 正常 / 2 读不到（游戏没开、不在对局）/ 1 断言失败。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, build as B

SUBCOMMANDS = ("verify", "procs", "exes", "state", "hand", "cards", "deck", "discard", "effects", "gs", "names",
               "rendered", "pick", "candidates", "targets", "dump", "selftest", "docs")


# --------------------------------------------------------------------------
def _out(obj, as_json: bool, text_fn) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1) if as_json else text_fn())


def cmd_procs(a) -> int:
    from . import proc as P
    rows = P.list_kards_processes()
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("没有 kards 进程在跑")
        return 2
    for r in rows:
        print("pid=%-6s %-28s image=%-10s md5=%s matched=%s"
              % (r["pid"], r["exe"], r["image_size"], (r["md5"] or "?")[:12], r["matched"]))
        print("         %s" % r.get("path"))
    print("\n★ 只有 matched=current 的那一份能用本工具链的偏移表。")
    return 0


def cmd_exes(a) -> int:
    """本机每一份 kards-Win64-Shipping.exe 的指纹 + URL 补丁状态（解释"为什么对不上"）。"""
    from . import exes as E
    argv = []
    if a.json:
        argv.append("--json")
    if a.no_md5:
        argv.append("--no-md5")
    for d in a.probe or []:
        argv += ["--probe", d]
    return E.main(argv)


def cmd_effects(a) -> int:
    """★ 逐实例"被贴的效果"（不是模板值）。无参数=列出所有带实时效果的卡；给 UID/card_id=看单卡。"""
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    st = s.snapshot()
    want = None
    if a.card:
        want = C.find(s, uid=a.card, card_id=_maybe_int(a.card), name=str(a.card))
        if want is None:
            print("找不到这张卡：%s（用 `kardsmem cards` 看可用 card_id / 名字）" % a.card)
            s.close()
            return 2
        if not want.raw or not want.raw.get("ptr"):
            print("这张卡的 raw['ptr'] 读不到，无法定位实例")
            s.close()
            return 2
    out = []
    for c in st.cards:
        ptr = (c.raw or {}).get("ptr")
        if not ptr or (want is not None and c.uid != want.uid):
            continue
        eff = C.read_live_effects(s, ptr)
        if want is None and not C.has_live_effects(eff):
            continue
        out.append({"uid": c.uid, "card_id": c.card_id, "name": c.name, "side": c.side,
                    "location": c.location, "slot": c.slot, "effects": eff})
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        s.close()
        return 0
    if not out:
        print("当前盘面上没有任何卡带「被贴的效果」（buffsFromCards / receivedAbilities / customName 全空）。")
        print("提示：这类数据只在效果生效后才有 —— 先打一张会贴效果的牌，例如")
        print("      ORDER OF THE DAY（芬兰，给 SISSI 贴 +1 指向税）或 GRIM DAY（+2）。")
        s.close()
        return 0
    for r in out:
        e = r["effects"]
        print("【%s】id=%-6s %-28s %s/%s slot=%s" %
              (r["uid"], r["card_id"], (r["name"] or "?")[:28], r["side"], r["location"], r["slot"]))
        print("   指向税: 模板=%s  贴的增量=%s  实际估计=%s"
              % (e.get("kredits_tax_template"), e.get("kredits_tax_extra"),
                 e.get("kredits_tax_live_estimate")))
        for b in e.get("buffs_from_cards") or []:
            print("   被贴 ← id=%-6s %-24s %s"
                  % (b.get("giver_card_id"), (b.get("giver_name") or "?")[:24], b.get("buffs")))
        if e.get("cards_buffed_by_this_card"):
            print("   这张卡贴了 → %s" % e["cards_buffed_by_this_card"])
        for ab in e.get("received_abilities") or []:
            print("   能力 ← %-14s 给者=%s" % (ab.get("ability"), ab.get("givers")))
        if e.get("custom_name1") or e.get("custom_name2"):
            print("   customName1/2: %s / %s" % (e.get("custom_name1"), e.get("custom_name2")))
        print()
    s.close()
    return 0


def cmd_verify(a) -> int:
    from . import proc
    d = proc.probe(pid=a.pid)
    d["spec_consistency"] = [list(x) for x in B.spec_consistency()]
    d["expected_build"] = B.BUILDS[B.CURRENT]
    d["rva"] = {k: hex(v) for k, v in B.RVA.items()}
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0 if (d.get("session") or {}).get("ok") else 2
    print("== 期望构建 ==")
    for k, v in B.BUILDS[B.CURRENT].items():
        print("   %-12s %s" % (k, v))
    print("== kards 进程 ==")
    for r in d["processes"]:
        print("   pid=%-6s %-28s image=%-10s matched=%s"
              % (r["pid"], r["exe"], r["image_size"], r["matched"]))
    s = d.get("session")
    if not s:
        print("!! %s" % d.get("error"))
        return 2
    print("== 接入 ==")
    print("   pid=%s base=0x%X image=0x%X md5=%s matched=%s ok=%s"
          % (s["pid"], s["base"], s.get("image_size") or 0, s.get("md5"), s.get("matched"), s["ok"]))
    print("   GWorld=%s  u_world=%s  gamestate=%s (%s)  in_battle=%s  match_active=%s"
          % (s.get("gworld"), s.get("gworld_ok"), s.get("gamestate"), s.get("gamestate_size"),
             s.get("in_battle"), s.get("match_active")))
    if not s["ok"]:
        print("!! 构建指纹不匹配：偏移表不适用于这份 exe，读数不可信。")
    sc = d["spec_consistency"]
    print("== 文档/代码常量一致性（kards-offsets.json）==")
    if not sc:
        print("   OK：代码常量与规格 JSON 一致")
    for name, got, want, ok in sc:
        print("   [%s] %s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))
    return 0


def cmd_state(a) -> int:
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    st = s.snapshot()
    _out(st.as_dict(), a.json, lambda: C.format_table(st, show_cards=not a.no_cards))
    s.close()
    return 0


def cmd_hand(a) -> int:
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    cs = C.hand(s, a.side)
    if a.json:
        print(json.dumps([c.__dict__ for c in cs], ensure_ascii=False, indent=1))
    else:
        print("== %s 手牌 %d 张（locationNumber 升序）==" % (a.side, len(cs)))
        for c in cs:
            print("  slot=%-3s id=%-6s %-26s %-9s a/d=%s/%s cost=%-3s op=%-3s enterTurn=%-3s %s%s"
                  % (c.slot, c.card_id, (c.name or "?")[:26], c.card_type, c.attack, c.defense,
                     c.kredit_cost, c.operation_cost, c.enter_play_on_turn, " ".join(c.keywords),
                     "  needsTarget" if c.needs_hand_target else ""))
    s.close()
    return 0


def cmd_cards(a) -> int:
    from . import cards as C
    if getattr(a, "rows", False):
        return C.main(["--rows"])
    if a.raw is not None:
        argv = ["--raw", a.raw] + (["--json"] if a.json else [])
        return C.main(argv)
    from .proc import attach
    s = attach(pid=a.pid)
    st = s.snapshot()
    print(json.dumps(st.as_dict(), ensure_ascii=False, indent=1) if a.json
          else C.format_table(st))
    s.close()
    return 0


def cmd_deck(a) -> int:
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    rows = C.deck_cards(s, a.side)
    if rows is None:
        print("牌库读不出（DeckCardIDs 为空 / 不在对局）")
        s.close()
        return 2
    if a.json:
        print(json.dumps({"rows": rows, "multiset": C.deck_multiset(s, a.side)},
                         ensure_ascii=False, indent=1))
        s.close()
        return 0
    ms = C.deck_multiset(s, a.side) or {}
    print("== %s 牌库 %d 张，%d 个不同名字（★ 同名多份在这里才看得见）=="
          % (a.side, len(rows), len(ms)))
    for r in rows:
        print("  id=%-6s %-28s matched=%s" % (r["card_id"], r["name"], r["matched"]))
    dups = {k: v for k, v in ms.items() if v > 1}
    if dups:
        print("  多份: %s" % dups)
    s.close()
    return 0


def cmd_discard(a) -> int:
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    cs = C.discard(s, a.side)
    if a.json:
        print(json.dumps([c.__dict__ for c in cs], ensure_ascii=False, indent=1))
        s.close()
        return 0
    print("== %s 弃牌堆 %d 张 ==" % (a.side, len(cs)))
    for c in cs[:a.limit]:
        print("  id=%-6s %-26s %-11s a/d=%s/%s cost=%s" %
              (c.card_id, (c.name or "?")[:26], c.card_type, c.attack, c.defense, c.kredit_cost))
    if len(cs) > a.limit:
        print("  ... 还有 %d 张（--limit 调大）" % (len(cs) - a.limit))
    s.close()
    return 0


def cmd_gs(a) -> int:
    from .gs import main as gs_main
    argv = []
    if a.json:
        argv.append("--json")
    if a.full:
        argv.append("--full")
    return gs_main(argv)


def cmd_names(a) -> int:
    import importlib
    mod = importlib.import_module(".names", __package__)
    return mod.main(["--json"] if a.json else [])


def cmd_rendered(a) -> int:
    import importlib
    mod = importlib.import_module(".rendered", __package__)
    return mod.main(["--json"] if a.json else [])


def cmd_pick(a) -> int:
    import importlib
    mod = importlib.import_module(".pick", __package__)
    return mod.main(["--json"] if a.json else [])


def cmd_candidates(a) -> int:
    import importlib
    mod = importlib.import_module(".pick", __package__)
    fn = getattr(mod, "candidates", None)
    if fn is None:
        print("pick.candidates() 不存在")
        return 1
    from .proc import attach
    s = attach(pid=a.pid)
    d = _jsonable(fn(s))
    print(json.dumps(d, ensure_ascii=False, indent=1))
    s.close()
    return 0


def _jsonable(o):
    import dataclasses
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def cmd_targets(a) -> int:
    from . import cards as C
    from .proc import attach
    s = attach(pid=a.pid)
    card = C.find(s, uid=a.card, card_id=None if a.card is None else _maybe_int(a.card),
                  name=None if a.card is None else str(a.card))
    if card is None:
        print("找不到这张卡：%s（用 cards 看可用的 card_id / 名字）" % a.card)
        s.close()
        return 2
    cands = C.target_candidates(s, card)
    print("== %s (id=%s side=%s loc=%s) 的读侧近似可指向目标 ==" %
          (card.name, card.card_id, card.side, card.location))
    for c in cands:
        print("  %-26s side=%-6s loc=%-9s slot=%-3s tax=%-3s suppressed=%s"
              % ((c["name"] or "?")[:26], c["side"], c["location"], c["slot"], c["tax"],
                 c["suppressed"]))
    print("\n⚠ 这是读侧近似（位置 + 压制 + 税）；权威判据在 exe 里的 "
          "CanBeTargetted 0x4A7F910 / CanOtherCardBeTargetted 0x4A7FC10。")
    s.close()
    return 0


def _maybe_int(v):
    try:
        return int(v, 0)
    except (TypeError, ValueError):
        return None


def cmd_dump(a) -> int:
    from .snapshot import dump, text as stext
    d = dump(path=a.out, include_rendered=not a.no_rendered, include_gs_full=a.gs_full)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(stext(d))
        if a.out:
            print("\n已写入 %s" % a.out)
    return 0


def cmd_docs(a) -> int:
    p = B.TOOLS_DIR / "README.md"
    print("内存侧工具链索引：%s" % p)
    print("库说明：%s" % (B.TOOLS_DIR / "kardsmem" / "README.md"))
    print("归档（被取代的一次性探针）：%s" % (B.TOOLS_DIR / "_archive"))
    if p.exists():
        print("-" * 60)
        print(p.read_text(encoding="utf-8"))
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def cmd_selftest(a) -> int:
    fails = []

    def chk(name, got, want):
        ok = got == want
        print("  [%s] %-52s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))
        if not ok:
            fails.append(name)

    print("== A. 构建指纹表（纯函数） ==")
    good = B.validate(B.BUILDS[B.CURRENT]["image_size"], B.BUILDS[B.CURRENT]["md5"])
    chk("current build ok", good.ok, True)
    # 另一个构建（1.57.26586 树）——SizeOfImage 不同 ⇒ 不能用偏移表
    other = B.validate(0x9CBB000, "65866f78b3bc56138f3fa20030659b55")
    chk("other build not ok", other.ok, False)
    chk("other build matched", other.matched, "launcher_157_orig")
    # 同一个构建 + 私服 URL 补丁 ⇒ md5 不同但**必须放行**（RVA 未变）
    patched_ok = B.validate(B.BUILDS[B.CURRENT]["image_size"],
                            "ffffffffffffffffffffffffffffffff", patched=True)
    chk("URL 补丁版（同构建）放行", patched_ok.ok, True)
    # 但"另一个构建"即使打了补丁也不能放行（SizeOfImage 不同 ⇒ 偏移表不适用）
    patched_other = B.validate(0x9CBB000, "201773bc49f52ff8b9e6c8aae172ae03", patched=True)
    chk("URL 补丁版（另一构建）仍拒绝", patched_other.ok, False)
    chk("URL-patched matched", patched_other.matched, "launcher_157_patched")
    # default 树里的第二份 exe（Shipping2，同构建 + 私服补丁）也按"另一构建"拒绝
    ship2 = B.validate(0x9CC4000, "724728d23007c6c4a087f03d384e5465", patched=True)
    chk("Shipping2（另一构建）仍拒绝", ship2.ok, False)
    chk("Shipping2 matched", ship2.matched, "launcher_default_patched")
    # 同构建但没标 patched ⇒ 不放行（防止把"被改过"当成没事）
    unmarked = B.validate(0x9CBB000, "201773bc49f52ff8b9e6c8aae172ae03")
    chk("同构建但未标 patched: 不放行", unmarked.ok, False)
    chk("FNamePool rva", B.RVA["FNamePool"], 0x0911B9C0)
    chk("GNames is decoy", B.RVA["GNames_decoy"], 0x090E2E28)
    from . import cards as _C
    chk("AllCardsInBattle elem", _C.TMAP_ELEM_SIZE, 24)

    print("== B. 与 board_api 的常量一致性（防两层漂移） ==")
    from .proc import board_api as BA
    chk("GWorld rva == board_api", B.RVA["GWorld"], BA.RVA_GWORLD)
    chk("GObjects rva == board_api", B.RVA["GObjects"], BA.RVA_GOBJECTS)
    chk("GNames rva == board_api", B.RVA["GNames_decoy"], BA.RVA_GNAMES)
    chk("image_size == board_api", B.BUILDS[B.CURRENT]["image_size"], BA.MEM_BUILD["image_size"])
    chk("md5 == board_api", B.BUILDS[B.CURRENT]["md5"], BA.MEM_BUILD["md5"])
    chk("exe_size == board_api", B.BUILDS[B.CURRENT]["exe_size"], BA.MEM_BUILD["exe_size"])

    print("== C. 与规格 JSON 的一致性 ==")
    sc = B.spec_consistency()
    chk("kards-offsets.json 无漂移", sc, [])

    print("== D. MemRO 原子读（本进程合成缓冲） ==")
    import ctypes
    import os as _os
    from .proc import MemRO
    # 注意 board_api 的合成目标是**子进程脚本**（`_SELFTEST_TARGET`），
    # 它的 commit()/w64() 不是模块属性 —— 要写值就自己建一块可读写页。
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_uint32, ctypes.c_uint32]
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    MEM_RESERVE, MEM_COMMIT, PAGE_RW = 0x2000, 0x1000, 0x04
    addr = k32.VirtualAlloc(None, 0x1000, MEM_RESERVE | MEM_COMMIT, PAGE_RW)
    try:
        ctypes.memmove(ctypes.c_void_p(addr), b"KARDS\x00\x01\x02\x03" + b"\xAA" * 16, 24)
        m = MemRO(_os.getpid())
        chk("read_exact 长度", len(m.read_exact(addr, 28)), 28)
        chk("atomic 内容一致", m.atomic(addr, 28), ctypes.string_at(addr, 28))
        chk("u16", m.u16(addr + 6), 0x0201)
        chk("u32", m.u32(addr), 0x4452414B)
        chk("ptr 越界拒绝", m.ptr(addr + 4), None)
        m.close()
    finally:
        k32.VirtualFree(ctypes.c_void_p(addr), 0, 0x8000)

    print("== E. board_api 自带 selftest（34 项，合成目标） ==")
    rc = BA._run_selftest()
    chk("board_api selftest rc", rc, 0)

    print("== E2. ops.py 行模型回归（7 组，合成盘面） ==")
    import subprocess as _sp, sys as _sys, os as _op
    _t = _op.path.join(_op.path.dirname(_op.path.dirname(_op.path.abspath(__file__))),
                       "test_rowmodel.py")
    if not _op.path.exists(_t):
        chk("test_rowmodel.py 存在", False, True)
    else:
        _r = _sp.run([_sys.executable, _t], capture_output=True, text=True,
                     encoding="utf-8", errors="replace")
        if _r.returncode:
            print((_r.stdout or "")[-1200:]);  print((_r.stderr or "")[-800:])
        chk("行模型 selftest rc", _r.returncode, 0)

    print("== F. 实机探测（游戏没开就跳过） ==")
    from . import proc as P
    try:
        s = P.Session(pid=a.pid, require_build=False, validate_md5=False)
    except Exception as e:                                    # noqa: BLE001
        print("  [SKIP] %s" % e)
    else:
        from .world import Locator
        loc = Locator(s.m, s.base)
        from .gs import GameState
        _ma = GameState(s).match_active
        print("  pid=%s base=0x%X matched=%s in_battle=%s match_active=%s kind=%s actors=%d"
              % (s.pid, s.base, s.info.matched, loc.in_battle, _ma, loc.gamestate_kind(),
                 len(loc.actors())))
        if s.info.ok and not _ma:
            print("  [INFO] 不在对局中（主菜单下 in_battle 同样为真）—— 盘面项无意义")
        if s.info.ok:
            st = s.snapshot()
            print("  board: turn=%s cards=%d local_hand=%d enemy_hand=%d local_discard=%d"
                  % (st.turn, len(st.cards), len(st.hand("local")), len(st.hand("enemy")),
                     len(st.discard("local"))))
        s.close()

    print("\nSELFTEST RESULT: %s（%d 项失败）" % ("PASS" if not fails else "FAIL", len(fails)))
    if fails:
        for f in fails:
            print("   FAILED: %s" % f)
    return 0 if not fails else 1


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m kardsmem",
                                 description="KARDS 内存侧（只读）工具链 %s" % __version__,
                                 epilog="命令：" + " ".join(SUBCOMMANDS))
    ap.add_argument("--pid", type=int, default=None, help="指定 pid（默认自动找）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("verify", help="进程+构建指纹+定位链+常量一致性")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("procs", help="列出所有 kards 进程")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_procs)

    p = sub.add_parser("exes", help="本机每份 kards exe 的指纹 + 服务器 URL 补丁状态")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-md5", action="store_true")
    p.add_argument("--probe", action="append", metavar="DIR", help="额外扫一个目录")
    p.set_defaults(fn=cmd_exes)

    p = sub.add_parser("state", help="归一化盘面")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-cards", action="store_true")
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser("hand", help="手牌")
    p.add_argument("side", nargs="?", default="local", choices=("local", "enemy"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hand)

    p = sub.add_parser("cards", help="盘面卡（--raw 摊开原始字段）")
    p.add_argument("--raw", nargs="?", const="*", metavar="UID|CARDID")
    p.add_argument("--rows", action="store_true", help="按行/列打印位置图")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_cards)

    p = sub.add_parser("effects", help="★ 逐实例被贴的效果（实时，不是模板值）")
    p.add_argument("card", nargs="?", help="UID 或 card_id（省略=列出所有带实时效果的卡）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_effects)

    p = sub.add_parser("discard", help="弃牌堆")
    p.add_argument("side", nargs="?", default="local", choices=("local", "enemy"))
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_discard)

    p = sub.add_parser("deck", help="物理牌库（含同名多份）")
    p.add_argument("side", nargs="?", default="local", choices=("local", "enemy"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_deck)

    p = sub.add_parser("gs", help="GameState 原始字段")
    p.add_argument("--full", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_gs)

    p = sub.add_parser("names", help="FNamePool")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_names)

    p = sub.add_parser("rendered", help="屏幕上摆着的每一张卡")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_rendered)

    p = sub.add_parser("pick", help="选择界面状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("candidates", help="选择界面候选")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_candidates)

    p = sub.add_parser("targets", help="读侧近似的可指向目标")
    p.add_argument("card", help="card_id 或名字子串")
    p.set_defaults(fn=cmd_targets)

    p = sub.add_parser("dump", help="聚合快照")
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-rendered", action="store_true")
    p.add_argument("--gs-full", action="store_true")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("selftest", help="离线断言 + 实机探测")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("docs", help="打印工具链索引")
    p.set_defaults(fn=cmd_docs)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    try:
        return a.fn(a)
    except Exception as e:                                     # noqa: BLE001
        from .proc import BuildMismatch, ProcessNotFound
        if isinstance(e, (ProcessNotFound, BuildMismatch)):
            print("!! %s" % e)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
