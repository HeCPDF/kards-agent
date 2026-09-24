#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mulliganprobe.py —— 找"这张牌被勾选要换掉"的内存位。

换牌界面的**候选牌本身不用找**：它们就是普通的本地手牌
（`location=hand`，`locationNumber` = 屏幕从左到右的位次），
`python -m kardsmem hand local` 在换牌界面下直接能读 —— 已实测。

缺的是逐卡的"选中/未选中"。本工具用 diff 定位：
  1. 在换牌界面跑 `python mulliganprobe.py base`   → 存下每张手牌的全字节快照
  2. 人工（或 --click N）点一张牌，使它变成"要换掉"
  3. 跑 `python mulliganprobe.py diff`             → 打印哪些偏移变了

只读内存 + 可选一次点击。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  —— 接上 kards-agent/ 与 vendor/

SNAP = os.path.join(_bootstrap.logs_dir(), "mulligan_base.json")
CARD_SPAN = 0x600          # UBaseCardObject 类大小 1656=0x678，取前 0x600 够覆盖明文区


def _hand():
    """→ [(card_id, name, slot, ptr)]，按 locationNumber 升序。"""
    from kardsmem.proc import attach
    s = attach()
    st = s.snapshot()
    out = []
    for c in st.cards:
        if c.side == "local" and c.location == "hand":
            p = (c.raw or {}).get("ptr")
            out.append((c.card_id, c.name, c.slot, p))
    out.sort(key=lambda t: (t[2] is None, t[2]))
    return s, out


def _read(s, ptr):
    b = s.m.atomic(ptr, CARD_SPAN)
    return b.hex() if b else None


def base() -> int:
    s, hand = _hand()
    if not hand:
        print("手牌读不到（不在换牌界面？）")
        return 1
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "cards": []}
    for cid, name, slot, ptr in hand:
        print("  slot=%s id=%-6s %-28s ptr=%s" % (slot, cid, name, hex(ptr or 0)))
        rec["cards"].append({"card_id": cid, "name": name, "slot": slot,
                             "ptr": ptr, "bytes": _read(s, ptr) if ptr else None})
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    json.dump(rec, open(SNAP, "w", encoding="utf-8"), ensure_ascii=False)
    s.close()
    print("基线已存：%d 张 → %s" % (len(hand), SNAP))
    return 0


def diff() -> int:
    if not os.path.exists(SNAP):
        print("没有基线，先跑 `python mulliganprobe.py base`")
        return 1
    old = json.load(open(SNAP, encoding="utf-8"))
    s, hand = _hand()
    by_id = {c["card_id"]: c for c in old["cards"]}
    # 加密记录每帧都在变，先算出"所有卡都在变"的偏移当噪声底
    changed_per_card = {}
    for cid, name, slot, ptr in hand:
        o = by_id.get(cid)
        if not o or not o.get("bytes") or not ptr:
            continue
        ob = bytes.fromhex(o["bytes"])
        nb = s.m.atomic(ptr, CARD_SPAN)
        if nb is None:
            continue
        diffs = [i for i in range(min(len(ob), len(nb))) if ob[i] != nb[i]]
        changed_per_card[cid] = (name, slot, ob, nb, diffs)
    if not changed_per_card:
        print("没有可比对的卡")
        return 1
    everywhere = set.intersection(*(set(v[4]) for v in changed_per_card.values())) \
        if len(changed_per_card) > 1 else set()
    print("噪声底（每张卡都在变的偏移，多半是加密记录/帧计数）：%d 个" % len(everywhere))
    print("加密记录区 0x568..0x5E0 属于预期噪声。\n")
    for cid, (name, slot, ob, nb, diffs) in sorted(changed_per_card.items(),
                                                   key=lambda t: t[1][1] or 0):
        only = [i for i in diffs if i not in everywhere]
        print("slot=%s id=%-6s %-28s 变了 %d 处，其中只有它变的 %d 处"
              % (slot, cid, name, len(diffs), len(only)))
        for i in only:
            if 0x568 <= i <= 0x5E0:
                continue                      # 加密记录，跳过
            print("    +0x%03X : %02X -> %02X" % (i, ob[i], nb[i]))
    s.close()
    return 0


def auto(index=1) -> int:
    """base → 点一张 → diff，**全在同一个进程里**。

    ★ 为什么不能分三次跑：换牌窗口很短（几十秒），每次起 python
      都要重新 attach + 快照，三个往返走下来窗口就超时关了 —— 实测踩过，
      那次 diff 跨了换牌超时，结果不作数。

    一个尚未分清的问题：点一张牌到底是**立即换掉**还是**标记等确认**？
    上次观到 DEATH FROM ABOVE 变成了 STRATEGIC BOMBING，但那一帧跟超时确认
    混在一起，分不出是哪一个导致的。所以这里点完马上 diff，不点确认。
    """
    import time as _t
    import win, actions
    from kardsmem.proc import attach

    s = attach()
    st = s.snapshot()
    hand = sorted([c for c in st.cards if c.side == "local" and c.location == "hand"],
                  key=lambda c: (c.slot is None, c.slot))
    if not hand:
        print("手牌读不到（不在换牌界面？）")
        return 1
    n = len(hand)
    snap0 = {}
    for c in hand:
        ptr = (c.raw or {}).get("ptr")
        snap0[c.card_id] = (c.name, c.slot, ptr, _read(s, ptr) if ptr else None)
    print("基线 %d 张: %s" % (n, [(c.slot, c.name) for c in hand]))

    win.set_dpi_aware()
    h = win.find_by_process("kards")[0]["hwnd"]
    win.bring_to_front(h)           # ★ 没焦点的窗口会把点击吃掉
    _t.sleep(0.4)
    x = int(round(640 + (index - (n - 1) / 2.0) * 223.6))
    print("点第 %d 张 %s @(%d,340)" % (index, hand[index].name, x))
    actions.click(*win.client_to_screen(h, x, 340))
    _t.sleep(0.6)

    st2 = s.snapshot()
    now = {c.card_id: c for c in st2.cards
           if c.side == "local" and c.location == "hand"}
    print("\n点完的手牌: %s" % [(c.slot, c.name) for c in
                                  sorted(now.values(), key=lambda c: (c.slot is None, c.slot))])
    gone = [cid for cid in snap0 if cid not in now]
    if gone:
        print("★ 有卡直接离手了（id=%s）⇒ 点一下就是**立即换牌**，没有"
              "标记态，也就不存在所谓的逐卡勾选位。" % gone)
    else:
        print("没有卡离手 ⇒ 应该是**标记态**，往下看哪个字节变了：")
    for cid, (name, slot, ptr, ob) in sorted(snap0.items(), key=lambda t: t[1][1] or 0):
        c = now.get(cid)
        if not c or not ob or not ptr:
            continue
        nb = s.m.atomic(ptr, CARD_SPAN)
        if nb is None:
            continue
        ob_b = bytes.fromhex(ob)
        diffs = [i for i in range(min(len(ob_b), len(nb)))
                 if ob_b[i] != nb[i] and not (0x568 <= i <= 0x5E0)]
        print("  slot=%s id=%-6s %-26s 变了 %d 处" % (slot, cid, name, len(diffs)))
        for i in diffs[:24]:
            print("      +0x%03X : %02X -> %02X" % (i, ob_b[i], nb[i]))
    s.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["base", "diff", "auto"])
    ap.add_argument("index", nargs="?", type=int, default=1)
    a = ap.parse_args(argv)
    if a.cmd == "auto":
        return auto(a.index)
    return base() if a.cmd == "base" else diff()


if __name__ == "__main__":
    sys.exit(main())
