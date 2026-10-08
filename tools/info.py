#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""info.py —— **只读**看牌：给 card_id，打印它所有能读到的字段。

刻意**不用**注入（不 attach）：这是纯 `ReadProcessMemory`（`board_api` → `kardsmem`），
⇒ 看牌不占注入会话、无崩溃风险，任何时候都能跑。

用法:
    python tools/info.py 35 2 8 11 10 19 25      # 手牌/场上都能看
    python tools/info.py --all                   # 我方手牌全部
    python tools/info.py --board                 # 双方场上全部

（2026-09-26 从 `_nn_scratch/info.py` 搬进 `tools/`；当时代替"每看一张牌就起一次注入"用。）
"""
import sys

import _bootstrap  # noqa: F401  —— 接上仓库根
from kardsmem import board as BA  # noqa: E402

KEYS = ("side", "slot", "card_type", "kredit_cost", "operation_cost",
        "attack", "attack_buff", "defense", "max_attack", "max_defense",
        "can_act", "needs_hand_target", "is_suppressed", "is_revealed",
        "is_being_guarded", "under_enemy_control", "enter_play_on_turn",
        "kredits_tax_as_enemy_target", "gotcha_activated", "target_uid", "keywords")


def main():
    args = [a for a in sys.argv[1:]]
    st = BA.open_source("mem").snapshot()
    if "--board" in args:
        ids = [c.obj.CardID for c in st.cards
               if c.obj.IsFieldUnit() or c.obj.IsHQ()]
    elif not [a for a in args if a.lstrip("-").isdigit()]:
        if st.my_side is None:
            print("本地座位 mySide 读不出（不在对局里？）—— 不能按『我方手牌』取牌；请显式给 card_id")
            return 1
        ids = [c.obj.CardID for c in st.hand()]
    else:
        ids = [int(a) for a in args if a.lstrip("-").isdigit()]
    print("turn=%s our_turn=%s my_side=%s kredits=%s" % (
        st.turn, st.our_turn, st.my_side_raw, {int(k): v for k, v in st.kredits.items()}))
    for cid in ids:
        got = [c for c in st.cards if c.obj.CardID == cid]
        if not got:
            print("  [%s] 读不到（不在快照里）" % cid)
            continue
        for c in got:
            print("  [%s] %s %s" % (c.obj.CardID, c.name, c.card_type))
            print("        %-24s %s" % ("location", getattr(c.obj.Location, "name", c.obj.Location)))
            for k in KEYS:
                v = getattr(c, k, None)
                if v not in (None, [], False):
                    print("        %-24s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
