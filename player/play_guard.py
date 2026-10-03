#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开局的**硬闸门**：对局进行中绝不点 `press_play`。

2026-10-02 事故：`open_next` 在"对局还没结束、`end_of_match_continue` 全部失败"的情况下，
只凭 `list_decks` 非空就点了"开始" —— 游戏随后以
`EXCEPTION_ACCESS_VIOLATION reading address 0xffffffffffffffff` 崩溃。

判据（只读）：`kardsmem.gs.GameState.match_active`（指挥点加密记录解出来了才为真）。
`True`/`None`（读不到）一律**拒绝开局** —— 宁可不开，也不能把游戏撞崩。

★ 2026-10-02（同日第二次事故后的修正）：初版写的是 `Locator.in_battle`，那是**错的** ——
`kardsmem/gs.py` 里白纸黑字写着"战斗 GameState 类已加载。**主菜单也为真** —— 不是「在对局中」"，
官方判据是 `GameState.match_active`。用 `in_battle` 会把主菜单也判成对局中 ⇒ 一局都开不了。
"""


def in_match(sess):
    """只读判断是否**真的在一局里**；True/False/None（None=判不出）。

    `match_active` 判据 = 战斗 GameState + 两侧指挥点至少有一侧解得出（`X != 0`）。
    主菜单：战斗 GameState 在、但 kredits 全 None ⇒ False（正因为如此不能用 `in_battle`）。

    ★★ 2026-10-02 三次修正（牌组页也会 `match_active=True`）：实机在**牌组页**读到
    `kredits={'local':0,'enemy':1}`（上一局残留的 GameState）⇒ 单用 `match_active` 会把
    "牌组页"判成对局中，闸门永远拦着、一局都开不了。改成**组合判据**：
    `match_active` 为真时，再看旁证——`sess.history()` 读得到动作流 / 快照 `turn>0` / 场上有牌；
    三个旁证**全都明确说"没有对局"**才允许开局（其余情况一律拦，保守方向=拦）。
    """
    try:
        gs = sess._kardsmem().gs()
        if not bool(gs.match_active):
            return False
        # match_active 为真：再找"真的在打"的旁证；找不到任何一条 ⇒ 牌组页/结算页的残留
        try:
            hist = sess.history(tail=1) or {}
            if hist.get("ok"):
                # ★ 2026-10-03（实机：上一局打完、已回牌组页，match_active / 快照仍是上一局残留，等 90 s 也不清）：
                #   动作流最后一条是 `ActionEndMatch` ⇒ 这一局**确定已结束**，残留读数不算“对局中”。
                #   （对局中途动作流不会以 ActionEndMatch 结尾；新一局开始后动作流会被新局覆盖。）
                rows = hist.get("rows") or []
                if rows and str(rows[-1].get("action_type")) == "ActionEndMatch":
                    return False
                return True
        except Exception:                                         # noqa: BLE001
            return True                        # 读不到就按"可能在对局中"处理（拦）
        try:
            st = sess.snapshot()
        except Exception:                                         # noqa: BLE001
            return True
        if (getattr(st, "turn", 0) or 0) > 0 or len(getattr(st, "cards", None) or ()) > 0:
            return True
        return False
    except Exception:                                         # noqa: BLE001
        return None


def safe_to_start(sess, decks=None):
    """→ `(ok, in_match, reason)`。`decks` 传了就要求非空（牌组页就绪）。"""
    im = in_match(sess)
    if im is not False:
        return False, im, ("对局进行中" if im else "判不出是否在对局（match_active=None）")
    if decks is not None and not decks:
        return False, im, "牌组列表读不到（不在牌组页）"
    return True, im, ""
