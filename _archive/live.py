#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live.py —— 对局实时状态（只读）+ 动作原语，供自动化控制用。

用法:
  python live.py state                 # 打印紧凑盘面
  python live.py hand                  # 只打印手牌（含 locationNumber）
  python live.py play <card_id> [x y]  # 出牌：拖到 (x,y) 或自动选空槽位
  python live.py end                   # 结束回合
"""
from __future__ import annotations

import os
import sys
import time

SRC = r"D:\Kards\OCR-Kards-Auto\src"
sys.path.insert(0, SRC)

import board_api as BA  # noqa: E402
import win  # noqa: E402
import actions  # noqa: E402
import deploy  # noqa: E402

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_state.json")


def get_source():
    return BA.open_source("mem")


def snapshot():
    return get_source().snapshot()


def hwnd_of():
    win.set_dpi_aware()
    for w in win.find_by_process("kards"):
        try:
            if win.looks_like_game(w):
                return w["hwnd"]
        except Exception:
            pass
    wins = win.find_by_process("kards")
    return wins[0]["hwnd"] if wins else None


def fmt_card(c, show_slot=True):
    kw = ",".join(c.keywords or [])
    tgt = ("->" + str(c.target_uid)) if c.target_uid else ""
    tax = getattr(c, "kredits_tax_as_enemy_target", None) or 0
    s = "%s id=%-3s %-10s %s/%s cost=%-2s op=%-2s" % (
        "[" + str(c.slot) + "]" if show_slot else "",
        c.card_id, (c.name or "?")[:26], c.attack, c.defense, c.kredit_cost, c.operation_cost)
    if tax:
        s += " tax=%s" % tax
    if c.is_suppressed:
        s += " SUPPRESSED"
    if c.needs_hand_target:
        s += " NEEDS_TGT"
    raw = c.raw or {}
    if "attack_left" in raw or "move_left" in raw:
        s += " act(atk=%s,mv=%s)" % (raw.get("attack_left"), raw.get("move_left"))
    if kw:
        s += " " + kw
    if tgt:
        s += " " + tgt
    return s


def print_state():
    st = snapshot()
    print("turn=%s our_turn=%s kredits=%s/%s slots=%s/%s front=%s finished=%s" % (
        st.turn, st.our_turn, st.kredits.get("local"), st.kredits.get("enemy"),
        st.slots.get("local"), st.slots.get("enemy"), st.frontline_owner, st.match_finished))
    for side in ("local", "enemy"):
        hq = st.hq.get(side)
        print("  %-5s HQ def=%s   hand=%d support=%d front=%d discard=%d deck=%d" % (
            side, (hq.defense if hq else None), len(st.hand(side)), len(st.support(side)),
            len(st.board(side)), len(st.discard(side)),
            len([c for c in st.cards if c.side == side and c.location == "deck"])))
    for side in ("local", "enemy"):
        hand = sorted(st.hand(side), key=lambda c: (c.slot if c.slot is not None else 99))
        print("  -- %s hand (%d) --" % (side, len(hand)))
        for c in hand:
            print("     " + fmt_card(c))
    for side in ("local", "enemy"):
        units = [c for c in st.field_units(side) if c.card_type != "location"]
        units = sorted(units, key=lambda c: ((c.location or ""), (c.slot if c.slot is not None else 99)))
        print("  -- %s field (%d) --" % (side, len(units)))
        for c in units:
            print("     " + fmt_card(c))
    print("  -- discard --")
    for side in ("local", "enemy"):
        for c in st.discard(side):
            print("     %s %s" % (side, fmt_card(c, show_slot=False)))


def hand_sorted(st, side="local"):
    return sorted(st.hand(side), key=lambda c: (c.slot if c.slot is not None else 99))


def main():
    if len(sys.argv) < 2:
        print_state()
        return
    cmd = sys.argv[1]
    if cmd == "state":
        print_state()
    elif cmd == "wait":
        # 等到我方回合（turn >= 可选参数）
        want = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        t0 = time.time()
        last = None
        while time.time() - t0 < 300:
            st = snapshot()
            key = (st.turn, st.our_turn, st.kredits.get("local"), len(st.field_units("local")),
                   len(st.field_units("enemy")), st.match_finished)
            if key != last:
                print("  t=%.0fs turn=%s our=%s k=%s fieldL=%d fieldE=%d finished=%s" % (
                    time.time() - t0, st.turn, st.our_turn, st.kredits.get("local"),
                    len(st.field_units("local")), len(st.field_units("enemy")), st.match_finished))
                last = key
            if st.match_finished:
                print("MATCH FINISHED")
                return
            if st.our_turn and (st.turn or 0) >= want:
                print("OUR TURN (turn=%s kredits=%s)" % (st.turn, st.kredits.get("local")))
                return
            time.sleep(1.5)
        print("TIMEOUT waiting")
    elif cmd == "hand":
        st = snapshot()
        for c in hand_sorted(st):
            print(fmt_card(c))
    elif cmd == "end":
        hwnd = hwnd_of()
        sx, sy = win.client_to_screen(hwnd, 1185, 471)
        ok = actions.click(sx, sy)
        print("click end_turn ->", ok)
    elif cmd == "play":
        card_id = int(sys.argv[2])
        st = snapshot()
        hand = hand_sorted(st)
        card = next((c for c in hand if c.card_id == card_id), None)
        if card is None:
            print("card %s not in hand" % card_id)
            return
        probes = None
        try:
            import json
            with open(os.path.join(SRC, "..", "config", "hand_layout.json"), encoding="utf-8") as f:
                layout = json.load(f)["layouts"]
            probes = layout[str(len(hand))]["probes"]
        except Exception as e:
            print("layout load failed:", e)
        idx = card.slot if card.slot is not None else 0
        hx = probes[idx] if probes and idx < len(probes) else 490 + idx * 76
        hwnd = hwnd_of()
        if len(sys.argv) >= 5:
            dx, dy = int(sys.argv[3]), int(sys.argv[4])
        else:
            cands = deploy.deploy_candidates(None) if False else None
            dx, dy = 640 + idx * 10, 500
        print("drag card_id=%s (idx=%s) hand_x=%s -> (%s,%s)" % (card_id, idx, hx, dx, dy))
        ok = deploy.drag_deploy(hwnd, hx, 700, drop=(dx, dy), hover_wait=0.30)
        print("drag_deploy ->", ok)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
