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
| `hand [side]`| 手牌（side = left/right/me/opp，默认 me），按 `locationNumber` 排 |
| `cards [--raw [UID]]` | 盘面卡；`--raw` 摊开单卡全部原始字段 |
| `discard [side]` | 弃牌堆 |
| `gs [--full]`| GameState 原始字段；`--full` 含牌库 id / 静态表 / 重连表 |
| `names`      | FNamePool 探测 + 解析示例（R5） |
| `rendered`   | 屏幕上摆着的每一张卡（`ABP_BaseCard_C`，不读图） |
| `pick`       | 是不是在等我选牌（抉择/预报/部署后选目标） |
| `candidates` | 选择界面的候选 + 与盘面卡的配对 |
| `targets <id\|名字>` | 读侧近似的"可指向目标"（**近似**，非权威） |
| `dump [--out F]` | 聚合快照（JSON），回归样本就存这个 |
| `selftest`   | 离线断言（含 board_api 的 30 项）+ 实机探测 |

退出码：0 正常 / 2 读不到（游戏没开、不在对局）/ 1 断言失败。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, build as B
from .gamemodel import ECardLocation, ESide

# 命令行的座位文字：left/right = 绝对座位（ESide 1/2）；me/opp = 本局我方/对方，只在命令入口处
# 用 `cards.parse_side()` 现算（读不出 mySide ⇒ ValueError），内部不存字符串。
SIDE_CHOICES = ("left", "right", "me", "opp")

SUBCOMMANDS = ("verify", "procs", "exes", "state", "hand", "cards", "deck", "discard", "effects", "gs", "names",
               "rendered", "pick", "candidates", "targets", "dump", "selftest", "docs")


# --------------------------------------------------------------------------
def _loc_name(card):
    loc = card.obj.Location
    return None if loc is None else ECardLocation(loc).name


def _card_dict(card) -> dict:
    """Card → 可 JSON 的 dict（取 `obj` 的字段；枚举写成 int，座位写 1/2）。"""
    from enum import IntEnum
    return {k: (int(v) if isinstance(v, IntEnum) else v)
            for k, v in card.obj.__dict__.items() if k != "raw"}


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
    """本机每一份 kards-Win64-Shipping.exe 的指纹（解释 md5 为什么对不上）。"""
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
        out.append({"uid": c.uid, "card_id": c.obj.CardID, "name": c.name, "side": c.side,
                    "location": _loc_name(c), "slot": c.slot, "effects": eff})
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
    d["selected_build"] = {"key": B.CURRENT, "source": getattr(B, "BUILD_SOURCE", "?")}
    d["rva_status"] = B.status()
    d["rva"] = {k: hex(v) for k, v in B.RVA.items()}
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0 if (d.get("session") or {}).get("ok") else 2
    print("== 本进程的 RVA ==  参照种子 %s（选表来源：%s）；RVA 来源=%s 已复验=%s 版本=%s"
          % (B.CURRENT, getattr(B, "BUILD_SOURCE", "?"), B.RVA_SOURCE, B.RVA_VERIFIED, B.RVA_VERSION))
    for _st, _ok, _d in (B.RESOLVED.steps if B.RESOLVED else []):
        print("   解析链 %-5s %s %s" % (_st, "OK  " if _ok else "FAIL", _d))
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
    side = C.parse_side(s, a.side)         # me/opp 只在入口处用本局 mySide 现算
    cs = C.hand(s, side)
    if a.json:
        print(json.dumps([_card_dict(c) for c in cs], ensure_ascii=False, indent=1))
    else:
        print("== 座位%d(%s) 手牌 %d 张（locationNumber 升序）==" % (int(side), a.side, len(cs)))
        for c in cs:
            print("  slot=%-3s id=%-6s %-26s %-9s a/d=%s/%s cost=%-3s op=%-3s enterTurn=%-3s %s%s"
                  % (c.slot, c.obj.CardID, (c.name or "?")[:26], c.card_type, c.attack, c.defense,
                     c.obj.getTotalKredits(), c.obj.operationCost, c.obj.enterPlayOnTurn, " ".join(c.keywords),
                     "  needsTarget" if c.obj.selectTargetOnPlayedFromHand else ""))
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
    side = C.parse_side(s, a.side)
    rows = C.deck_cards(s, side)
    if rows is None:
        print("牌库读不出（DeckCardIDs 为空 / 不在对局）")
        s.close()
        return 2
    if a.json:
        print(json.dumps({"rows": rows, "multiset": C.deck_multiset(s, side)},
                         ensure_ascii=False, indent=1))
        s.close()
        return 0
    ms = C.deck_multiset(s, side) or {}
    print("== 座位%d(%s) 牌库 %d 张，%d 个不同名字（★ 同名多份在这里才看得见）=="
          % (int(side), a.side, len(rows), len(ms)))
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
    side = C.parse_side(s, a.side)
    cs = C.discard(s, side)
    if a.json:
        print(json.dumps([_card_dict(c) for c in cs], ensure_ascii=False, indent=1))
        s.close()
        return 0
    print("== 座位%d(%s) 弃牌堆 %d 张 ==" % (int(side), a.side, len(cs)))
    for c in cs[:a.limit]:
        print("  id=%-6s %-26s %-11s a/d=%s/%s cost=%s" %
              (c.obj.CardID, (c.name or "?")[:26], c.card_type, c.attack, c.defense, c.obj.getTotalKredits()))
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
          (card.name, card.obj.CardID, None if card.side is None else int(card.side), _loc_name(card)))
    for c in cands:
        print("  %-26s side=%-6s loc=%-15s slot=%-3s tax=%-3s suppressed=%s"
              % ((c["name"] or "?")[:26], int(c["side"]) if c["side"] is not None else None,
                 ECardLocation(c["location"]).name if c["location"] is not None else None, c["slot"], c["tax"],
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
    print("项目索引：%s" % p)
    print("读侧库说明：%s" % (B.TOOLS_DIR / "kardsmem" / "README.md"))
    print("取材/标定工具：%s" % B.TOOLS_SUB)
    print("归档（被取代的一次性探针）：%s" % (B.TOOLS_DIR / "_archive"))
    if p.exists():
        print("-" * 60)
        print(p.read_text(encoding="utf-8"))
    return 0


# --------------------------------------------------------------------------
# selftest 的一个子块：customJson 43 标记解析器的**结构自检**（纯离线）
# --------------------------------------------------------------------------
def _selftest_custom_json_flags(chk) -> None:
    """`cards.read_custom_json_flags()` 的离线单元测试。

    ★ 这不是"实机验证过"——游戏在主菜单、场上没有真实卡对象，customJson
    多半是空 TMap，没法拿真数据核对 `FJsonValueBoolean::Value@+0x0C` 这条
    按引擎源码推算出来的偏移对不对。这里测的是**解析代码本身**：
    给它一段我们自己按 UE 源码结构拼出来的字节，看它能不能摊开成正确的
    `{标记名: bool}`。

    按 CLAUDE.md 弯路 #11 的教训，**只跑一组输入不算数**——一个"永远返回
    同一个值"的坏实现也能通过单组用例。所以这里拼两组内容不同的 customJson
    （键的集合不同、同名键的布尔值也故意相反），分别断言，确保解析结果
    真的跟着输入变，而不是巧合对上一次。
    """
    import ctypes
    import os as _os
    from .proc import MemRO

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_uint32, ctypes.c_uint32]
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    MEM_RESERVE, MEM_COMMIT, PAGE_RW = 0x2000, 0x1000, 0x04

    from . import cards as _C

    allocs = []

    def alloc(size):
        a = k32.VirtualAlloc(None, max(size, 1), MEM_RESERVE | MEM_COMMIT, PAGE_RW)
        allocs.append(a)
        return a

    def build_custom_json(card_addr, pairs):
        """在本进程内存里拼一个 `customJson` TMap，返回值不需要——直接写进 card_addr。

        `pairs`：`[(key:str, value:bool), ...]`。布局见 cards.py 里那段 P2 注释：
        card+0x518(FBlueprintJsonObject=TSharedPtr<FJsonObject>)
          -> FJsonObject(=TMap 头 {data_ptr@0, num@8})
            -> N * {FString key(0x10) + TSharedPtr<FJsonValue> value(0x10)
                    + HashNextId(4) + HashIndex(4)}  步长 0x28
                 每个 value 指向一个 FJsonValue：{vtable占位(8), EJson Type@8=4, bool@0xC}
        """
        n = len(pairs)
        entries = alloc(_C.STRIDE_TMAP_FSTRING_JSONVALUE * max(n, 1))
        for i, (key, val) in enumerate(pairs):
            wide = key.encode("utf-16-le") + b"\x00\x00"
            keybuf = alloc(len(wide))
            ctypes.memmove(ctypes.c_void_p(keybuf), wide, len(wide))
            jv = alloc(16)
            ctypes.memmove(ctypes.c_void_p(jv), b"\xEE" * 8, 8)          # 假 vtable，解析代码不读它
            struct_type_bool = ctypes.create_string_buffer(8)
            import struct as _st
            _st.pack_into("<i?", struct_type_bool, 0, _C._EJSON_BOOLEAN, bool(val))
            ctypes.memmove(ctypes.c_void_p(jv + 8), struct_type_bool.raw, 8)
            entry = ctypes.create_string_buffer(_C.STRIDE_TMAP_FSTRING_JSONVALUE)
            _st.pack_into("<QiiQQii", entry, 0,
                         keybuf, len(key) + 1, 0,     # FString{ptr,num,max(不用)}
                         jv, 0,                        # TSharedPtr<FJsonValue>{obj,refctrl(不用)}
                         0, 0)                         # HashNextId/HashIndex（不用）
            ctypes.memmove(ctypes.c_void_p(entries + i * _C.STRIDE_TMAP_FSTRING_JSONVALUE),
                          entry.raw, _C.STRIDE_TMAP_FSTRING_JSONVALUE)
        json_obj = alloc(16)                                              # FJsonObject = TMap 头
        import struct as _st2
        buf16 = ctypes.create_string_buffer(16)
        _st2.pack_into("<Qi", buf16, 0, entries, n)
        ctypes.memmove(ctypes.c_void_p(json_obj), buf16.raw, 16)
        buf_shared = ctypes.create_string_buffer(16)
        _st2.pack_into("<QQ", buf_shared, 0, json_obj, 0)                 # TSharedPtr<FJsonObject>
        ctypes.memmove(ctypes.c_void_p(card_addr + _C.OFF_CARD_CUSTOM_JSON), buf_shared.raw, 16)

    class _FakeSession:
        pass

    try:
        card_addr = alloc(_C.OFF_CARD_CUSTOM_JSON + 0x20)
        m = MemRO(_os.getpid())
        sess = _FakeSession()
        sess.m = m

        # 输入 A：命中两个已知标记，一真一假
        build_custom_json(card_addr, [("triggered", True), ("veteran", False)])
        flags_a = _C.read_custom_json_flags(sess, card_addr)
        chk("customJson A: triggered=True", flags_a.get("triggered"), True)
        chk("customJson A: veteran=False", flags_a.get("veteran"), False)
        chk("customJson A: 未命中的键给 None（不是 False）", flags_a.get("inEffect"), None)
        chk("customJson A: 返回的键覆盖全部 43 个", sorted(flags_a), sorted(_C.JSON_BOOL_FLAG_NAMES))

        # 输入 B：换一组不同的键/不同的真假值——专治"永远返回同一个值"这种坏实现
        # （CLAUDE.md 弯路 #11：card_targets.py 的 out() 就是靠"两组不同输入对比"才挖出来的）
        build_custom_json(card_addr, [("triggered", False), ("inEffect", True),
                                      ("suppressionException", True)])
        flags_b = _C.read_custom_json_flags(sess, card_addr)
        chk("customJson B: triggered=False（和 A 相反，证明真的在读新数据）",
            flags_b.get("triggered"), False)
        chk("customJson B: inEffect=True", flags_b.get("inEffect"), True)
        chk("customJson B: suppressionException=True", flags_b.get("suppressionException"), True)
        chk("customJson B: veteran 这次没设，给 None", flags_b.get("veteran"), None)

        # 空 TMap（游戏没在对局时最可能长这样）：全部给 None，不报错
        build_custom_json(card_addr, [])
        flags_empty = _C.read_custom_json_flags(sess, card_addr)
        chk("customJson 空: 43 个全是 None", set(flags_empty.values()), {None})

        m.close()
    finally:
        for a in allocs:
            if a:
                k32.VirtualFree(ctypes.c_void_p(a), 0, 0x8000)


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
    chk("current build md5_match", good.md5_match, True)
    # 另一个构建（SizeOfImage 不同）—— **一律不放行**：拿错 RVA 只会读到垃圾
    other_key = next(k for k in B.BUILDS if k != B.CURRENT)
    ob = B.BUILDS[other_key]
    other = B.validate(ob["image_size"], ob["md5"])
    chk("另一构建 仍拒绝", other.ok, False)
    chk("另一构建 matched", other.matched, other_key)
    # 同一个构建、字节不同（本地改动过）⇒ **必须放行**：SizeOfImage 决定 RVA 有效性
    same_img = B.validate(B.BUILDS[B.CURRENT]["image_size"],
                          "ffffffffffffffffffffffffffffffff")
    chk("同构建·字节不同 放行", same_img.ok, True)
    chk("同构建·字节不同 md5_match=False", same_img.md5_match, False)
    chk("同构建·字节不同 仍认出构建", same_img.matched, B.CURRENT)
    # RVA 表：选中的构建要与表一致；两个构建的**已采证值**不许漂
    if B.RVA_SOURCE in ("seed", "default", "env"):      # 缓存/扫描来源的值本来就可以与种子不同
        chk("RVA == RVA_BY_BUILD[CURRENT]（来源 %s）" % B.RVA_SOURCE, dict(B.RVA), B.RVA_BY_BUILD[B.CURRENT])
    else:
        print("  [INFO] 当前 RVA 来源 %s（非种子），跳过与种子逐项相等" % B.RVA_SOURCE)
    chk("Steam FNamePool 未漂移", B.RVA_BY_BUILD["current"]["FNamePool"], 0x0911B9C0)
    chk("Steam GNames(decoy) 未漂移", B.RVA_BY_BUILD["current"]["GNames_decoy"], 0x090E2E28)
    chk("launcher FNamePool 未漂移", B.RVA_BY_BUILD["launcher_default"]["FNamePool"], 0x09118940)
    chk("launcher GWorld 未漂移", B.RVA_BY_BUILD["launcher_default"]["GWorld"], 0x08F5F5B0)
    from . import cards as _C
    chk("AllCardsInBattle elem", _C.TMAP_ELEM_SIZE, 24)

    print("== B. 种子表单一来源（P7：build_tables.json 是唯一数据；board 不再有第二份表） ==")
    from .proc import board_api as BA
    chk("board 没有第二份表（_BUILD_TABLE 已删）", hasattr(BA, "_BUILD_TABLE"), False)
    chk("board 不再有 VERSION_TO_BUILD 式登记", hasattr(__import__("kardsmem.version", fromlist=["x"]), "VERSION_TO_BUILD"), False)
    _ref = B.BUILDS[B.CURRENT]
    chk("board.MEM_BUILD.exe_size == 参照种子", BA.MEM_BUILD["exe_size"], _ref["exe_size"])
    chk("board.MEM_BUILD.md5 == 参照种子", BA.MEM_BUILD["md5"], _ref["md5"])
    chk("board.RVA_GWORLD == build.RVA['GWorld']（调用期读，P7-S3b）", BA.RVA_GWORLD, B.RVA["GWorld"])
    for _key, _rv in B.RVA_BY_BUILD.items():
        _bd = B.BUILDS.get(_key, {})
        chk("[%s] 种子 RVA 含核心四项" % _key, all(_rv.get(k) for k in B.CORE_RVA_KEYS), True)
        chk("[%s] 种子身份含 image_size/exe_size/md5/versions" % _key,
            all(_bd.get(k) for k in ("image_size", "exe_size", "md5", "versions")), True)
    _allv = [v for b_ in B.BUILDS.values() for v in b_["versions"]]
    chk("versions 在种子表里不重复", len(_allv), len(set(_allv)))

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

    print("== D2. customJson 43 标记解析（合成 TMap，纯离线，不需要游戏在跑） ==")
    _selftest_custom_json_flags(chk)

    print("== E. board_api 自带 selftest（30 项，合成目标） ==")
    rc = BA._run_selftest()
    chk("board_api selftest rc", rc, 0)

    # ★ 纯函数，不需要游戏在跑。测的全是**Python 原生行为与 UE 不同**的点
    #   （整除向零 / 取模符号 / 除零给 0 / int32 回绕 / Stri 不分大小写…）——
    #   这些错了不会崩，只会静悄悄算出错的合法性判定。
    print("== E3. uetypes + kismetlib（UE 标量语义与原生原语） ==")
    from . import kismetlib as _KL
    chk("kismetlib selftest rc", _KL.selftest(), 0)

    # ★ 纯函数，不需要游戏在跑。测的是 TSparseArray 的位图判空洞逻辑——
    #   `containers.py` 存在的唯一理由就是不再"当连续数组硬读"，这条回归
    #   直接造一个有空洞的假 TSparseArray 核对空洞真的被跳过。
    print("== E5. kardsmem.containers（TSparseArray/TSet/TMap 位图读法） ==")
    from . import containers as _CT
    _ct_rows = _CT.selftest()
    chk("containers selftest rc", 0 if all(r[0] == "PASS" for r in _ct_rows) else 1, 0)

    # ★ 只依赖本地 Game.locres 导出，不需要游戏在跑——核对"提示 → reject/banner/
    #   settlement"这条分类（§11.2「提示文本 → 事件流分类」）没有退化。
    print("== E6. kardsmem.notify.classify（提示 → reject/banner/settlement） ==")
    from . import notify as _NF
    _nf_rows = _NF.selftest()
    chk("notify selftest rc", 0 if all(r[0] == "PASS" for r in _nf_rows) else 1, 0)

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
            print("  board: turn=%s cards=%d my_side=%s hand[L/R]=%d/%d discard[L/R]=%d/%d"
                  % (st.turn, len(st.cards), st.my_side_raw,
                     len(st.hand(ESide.left)), len(st.hand(ESide.right)),
                     len(st.discard(ESide.left)), len(st.discard(ESide.right))))
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

    p = sub.add_parser("exes", help="本机每份 kards exe 的指纹表")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-md5", action="store_true")
    p.add_argument("--probe", action="append", metavar="DIR", help="额外扫一个目录")
    p.set_defaults(fn=cmd_exes)

    p = sub.add_parser("state", help="归一化盘面")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-cards", action="store_true")
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser("hand", help="手牌")
    p.add_argument("side", nargs="?", default="me", choices=SIDE_CHOICES,
                   help="left/right = 绝对座位 1/2；me/opp = 本局我方/对方（入口处用 mySide 现算）")
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
    p.add_argument("side", nargs="?", default="me", choices=SIDE_CHOICES,
                   help="left/right = 绝对座位 1/2；me/opp = 本局我方/对方（入口处用 mySide 现算）")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_discard)

    p = sub.add_parser("deck", help="物理牌库（含同名多份）")
    p.add_argument("side", nargs="?", default="me", choices=SIDE_CHOICES,
                   help="left/right = 绝对座位 1/2；me/opp = 本局我方/对方（入口处用 mySide 现算）")
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
