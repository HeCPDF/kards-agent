#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.snapshot —— 把各层聚合**一份快照**（可 JSON 落盘），供上层/回归比对。

一份快照里有什么
================
- `meta`    ：pid / 模块基址 / 构建指纹 / 定位链 / 版本（**回归时必须先看这里**）
- `board`   ：`board_api` 归一化盘面（手牌、场上、弃牌堆、牌库、HQ、指挥点、回合）
- `gs`      ：GameState 原始字段 + 侧值块解密 + 牌库/静态表（`--full` 时）
- `names`   ：FNamePool 探测结果（FName 路线能不能用）
- `rendered`：屏幕上摆着的每一张卡（`ABP_BaseCard_C` actor，不读图）
- `pick`    ：选择界面状态（是不是在等我选牌、候选是谁）
- `hand` / `discard`：按阵营切好的视图
- `notes`   ：这次没读到的部分（**不猜**：读不出就写在这里）

任何一层失败都不会让整份快照失败 —— 失败原因进 `notes`。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import time
from typing import Optional

from . import __version__, build as B
from .world import Locator


def _safe(fn, notes: list, label: str, default=None):
    try:
        return fn()
    except Exception as e:                                   # noqa: BLE001 - 聚合层要兜住一切
        notes.append("%s 失败: %s: %s" % (label, type(e).__name__, e))
        return default


def _jsonable(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def collect(session=None, include_rendered: bool = True, include_names: bool = True,
            include_gs: bool = True, include_gs_full: bool = False) -> dict:
    from .proc import attach
    s = session or attach()
    notes = []
    loc = Locator(s.m, s.base)
    out = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kardsmem": __version__,
            "build": s.info.as_dict(),
            "locator": _safe(loc.describe, notes, "locator"),
            "red_line": "只读（PROCESS_QUERY_INFORMATION|PROCESS_VM_READ + ReadProcessMemory）",
        },
        "board": None, "gs": None, "names": None, "rendered": None, "pick": None,
        "hand": {}, "discard": {}, "notes": notes,
    }

    st = _safe(s.snapshot, notes, "board_api.snapshot")
    if st is not None:
        out["board"] = _jsonable(st.as_dict())
        out["hand"] = {"local": _jsonable(st.hand("local")), "enemy": _jsonable(st.hand("enemy"))}
        out["discard"] = {"local": _jsonable(st.discard("local")),
                          "enemy": _jsonable(st.discard("enemy"))}
    else:
        out["notes"].append("盘面读不到（游戏没开 / 不在对局 / GWorld 未建）")

    if include_gs:
        def _gs():
            from .gs import GameState
            g = GameState(s)
            return g.snapshot() if include_gs_full else {
                "addr": hex(g.addr), "kind": g.kind, "in_battle": g.in_battle,
                "match_active": g.match_active,
                "kredits": g.kredits(), "slots": g.slots()}
        out["gs"] = _safe(_gs, notes, "gs")

    if include_names:
        def _names():
            from .names import FNamePool
            p = FNamePool(s.m, s.base)
            pr = p.probe()
            return {"ok": p.ok, "addr": hex(p.addr), "probe": pr}
        out["names"] = _safe(_names, notes, "names")

    if include_rendered:
        def _rendered():
            mod = importlib.import_module(".rendered", __package__)
            rows = mod.rendered_cards(s)
            return _jsonable(rows)
        out["rendered"] = _safe(_rendered, notes, "rendered")

        def _pick():
            mod = importlib.import_module(".pick", __package__)
            return _jsonable(mod.pick_state(s))
        out["pick"] = _safe(_pick, notes, "pick")

    return _jsonable(out)


def dump(path: Optional[str] = None, **kw) -> dict:
    d = collect(**kw)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        d["_written"] = path
    return d


def text(d: dict) -> str:
    """把快照渲染成人看的短摘要（不是完整 dump）。"""
    m, b = d.get("meta", {}), d.get("board") or {}
    L = []
    L.append("== meta ==")
    L.append("  build  : %s" % (m.get("build") or {}))
    loc = m.get("locator") or {}
    L.append("  locator: u_world=%s gamestate=%s actors=%s"
             % (loc.get("u_world"), loc.get("gamestate_kind"), loc.get("level_actors")))
    if b:
        L.append("== board ==")
        L.append("  turn=%s our_turn=%s finished=%s kredits=%s slots=%s"
                 % (b.get("turn"), b.get("our_turn"), b.get("match_finished"),
                    b.get("kredits"), b.get("slots")))
        for side in ("local", "enemy"):
            hq = (b.get("hq") or {}).get(side) or {}
            L.append("  %-5s HQ=%s def=%s hand=%s front=%s support=%s discard=%s"
                     % (side, hq.get("card_id"), hq.get("defense"),
                        len(d.get("hand", {}).get(side) or []),
                        len([c for c in b.get("cards", [])
                             if c.get("side") == side and c.get("location") == "frontline"]),
                        len([c for c in b.get("cards", [])
                             if c.get("side") == side and c.get("location") == "back"]),
                        len(d.get("discard", {}).get(side) or [])))
    g = d.get("gs")
    if g:
        L.append("== gs ==  %s  %s  kredits=%s slots=%s"
                 % (g.get("kind"), g.get("addr"), g.get("kredits"), g.get("slots")))
    n = d.get("names")
    if n:
        pr = n.get("probe") or {}
        ents = pr.get("entries") or []
        L.append("== names == ok=%s pool=%s chunk0=%s blocks=%s max_block=%s"
                 % (n.get("ok"), pr.get("pool"), pr.get("chunk0"),
                    pr.get("nonnull_blocks"), pr.get("max_block")))
        if ents:
            L.append("   前几个 entry（★ 索引稀疏：idx = byteOffset/2）: %s"
                     % ", ".join("idx%s@%s=%r" % (e.get("index"), e.get("offset"), e.get("name"))
                                 for e in ents[:4]))
    r = d.get("rendered")
    if r is not None:
        L.append("== rendered == %d 张：%s" % (len(r), ", ".join(
            "%s(id=%s)" % ((x.get("name") or "?"), x.get("card_id")) for x in r[:12])))
    p = d.get("pick")
    if p:
        L.append("== pick == pending=%s choose_one_active=%s is_selecting_hand_target=%s"
                 % (p.get("pending"), p.get("choose_one_active"), p.get("is_selecting_hand_target")))
    if d.get("notes"):
        L.append("== notes ==")
        for x in d["notes"]:
            L.append("  - %s" % x)
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="聚合快照（只读）")
    ap.add_argument("--out", help="写到 JSON 文件")
    ap.add_argument("--json", action="store_true", help="打到 stdout（JSON）")
    ap.add_argument("--no-rendered", action="store_true")
    ap.add_argument("--gs-full", action="store_true")
    a = ap.parse_args(argv)
    d = dump(path=a.out, include_rendered=not a.no_rendered, include_gs_full=a.gs_full)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(text(d))
        if a.out:
            print("\n已写入 %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
