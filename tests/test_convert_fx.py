#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A5-①：转化后接线（`ConvertCard`，BP_CardFunctions.cpp:12611）——钩子顺序 / 形参 / 互斥 + `Sim.event_fx["convert"]` 消费。

假 VM 下验证（真 VM 与实机未验）：
  * 旧牌（在场）离场族：OnLeaveBoardOrOwner(8, method 6) → 0x2E → OnCardLocationMoved("Convert") → 0x2F → OnAfterLeaveBoard → 0x8，
    都排除旧牌自己（0x 系列）、被压制的旧牌不跑自己的；
  * 新牌：有实例才跑 OnEnterPlay(5) + 0x2B；没有实例 ⇒ 不跑并记 gaps（不拿旧牌冒充）；
  * 0x22 整次转化只一轮、`cardID ∈ newCardIDs` 的跳过、`skipTrigger` 真则不发；
  * 手牌里的旧牌没有离场钩子；
  * sim：旧牌离场后果 → 换牌（同位置、前线去烟幕）→ 新牌后果 → 0x22 后果；牌库里的转化同下标换牌。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _hookfake import ME, OPP, Checker, card, names, run          # noqa: E402
from engine.state import EVENT_FX_KINDS                            # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402
from sim.state import H, Sim, U                                    # noqa: E402

chk = Checker()

OLD_H = {"OnLeaveBoardOrOwner", "OnCardLocationMoved", "OnAfterLeaveBoard",
         "OnOtherCardLeaveBoardOrOwner", "OnOtherCardLocationMoved", "OnAfterOtherCardLeaveBoardOrOwner"}   # 旧牌自己 + 想吃 0x 系列（应被排除）
W_H = {"OnOtherCardLeaveBoardOrOwner", "OnOtherCardLocationMoved", "OnAfterOtherCardLeaveBoardOrOwner",
       "OnOtherCardEnterPlay", "OnOtherCardConverted"}
NEW_H = {"OnEnterPlay"}                                         # 新牌自己的进场钩子
NEW_H22 = {"OnEnterPlay", "OnOtherCardConverted"}               # 新牌同时是 0x22 的登记牌（应被 cardID∈newCardIDs 跳过）
HAND_H = {"OnLeaveBoardOrOwner", "OnCardLocationMoved", "OnAfterLeaveBoard"}   # 手牌里的旧牌只有自己的离场钩子


def ovr(**by_id):
    return {1000 + int(k): set(v) for k, v in by_id.items()}


def main():
    old = card(8, "OLD", "frontline", OPP)
    w = card(20, "W", "back", ME)
    sup = card(21, "SUP", "back", ME, is_suppressed=True)
    new = card(31, "NEW", "frontline", OPP)
    hand_old = card(9, "HANDOLD", "hand", OPP)
    cards = [old, w, sup, new, hand_old]
    O = ovr(**{"8": OLD_H, "20": W_H, "21": W_H, "31": NEW_H, "9": HAND_H})

    # ---- 顺序 / 形参 ----
    _r, calls = run(TR.run_convert, cards, O, new, [8], [31], "NEWNAME", 40, old_cards=[old])
    seq = names(calls)
    want = [("OnLeaveBoardOrOwner", "OLD"), ("OnOtherCardLeaveBoardOrOwner", "W"),
            ("OnCardLocationMoved", "OLD"), ("OnOtherCardLocationMoved", "W"),
            ("OnAfterLeaveBoard", "OLD"), ("OnAfterOtherCardLeaveBoardOrOwner", "W"),
            ("OnEnterPlay", "NEW"), ("OnOtherCardEnterPlay", "W"), ("OnOtherCardConverted", "W")]
    chk("顺序：旧牌离场(自己→0x2E) → 移动(自己→0x2F) → 离场后(自己→0x8) → 新牌 OnEnterPlay(5)+0x2B → 0x22",
        seq == want, str(seq))
    chk("互斥：旧牌自己不吃 0x2E/0x2F/0x8（排除自己）；被压制的 SUP 被 fetch 丢掉；手牌里的 HANDOLD 没有离场钩子；"
        "新牌 NEW 不吃 0x22（cardID∈newCardIDs）",
        not any(n in ("SUP", "HANDOLD") for _h, n in seq)
        and ("OnOtherCardLeaveBoardOrOwner", "OLD") not in seq and ("OnOtherCardConverted", "NEW") not in seq, str(seq))
    a = {h: ar for h, _n, ar, _k in calls}
    chk("形参：离场 (goingToLocation 8, method 6)；0x2E Method=6；移动 MoveReason=Convert/旧位置=前线 7/ChangeOwner 假",
        a["OnLeaveBoardOrOwner"] == {"goingToLocation": 8, "leavePlayMethod": 6}
        and a["OnOtherCardLeaveBoardOrOwner"] == {"cardLeaving": 8, "goingToLocation": 8, "Method": 6}
        and a["OnCardLocationMoved"] == {"OldLocation": 7, "NewLocation": 8, "ChangeOwner": False, "MoveReason": "Convert"}
        and a["OnOtherCardLocationMoved"]["cardMoved"] == 8 and a["OnOtherCardLocationMoved"]["MoveReason"] == "Convert",
        str((a["OnLeaveBoardOrOwner"], a["OnCardLocationMoved"])))
    chk("形参：0x8 oldLocation=7、goingToLocation=8；OnEnterPlay Method=5；0x2B cardPlayed=新牌 Method=5；"
        "0x22 旧/新 id、名字、发起者",
        a["OnAfterLeaveBoard"] == {"goingToLocation": 8}
        and a["OnAfterOtherCardLeaveBoardOrOwner"] == {"cardLeaving": 8, "OldLocation": 7}
        and a["OnEnterPlay"] == {"Method": 5} and a["OnOtherCardEnterPlay"] == {"cardPlayed": 31, "Method": 5}
        and a["OnOtherCardConverted"] == {"oldCardIDs": [8], "newCardIDs": [31], "newCardName": "NEWNAME",
                                          "instigatorID": 40})

    # ---- 没有新牌实例：不冒充，记 gaps ----
    r, calls = run(TR.run_convert, cards, O, None, [8], [-7001], "NEWNAME", 40, old_cards=[old])
    seq = names(calls)
    chk("没有新牌实例 ⇒ 不跑新牌 OnEnterPlay(5)/0x2B，旧牌离场族和 0x22 照跑",
        ("OnEnterPlay", "NEW") not in seq and ("OnOtherCardEnterPlay", "W") not in seq
        and seq[0] == ("OnLeaveBoardOrOwner", "OLD") and seq[-1] == ("OnOtherCardConverted", "W"), str(seq))
    chk("没有新牌实例 ⇒ meta.gaps 如实记", r["meta"]["new_hooks"] is False
        and any("新牌实例不存在" in g for g in r["meta"]["gaps"]), str(r["meta"]["gaps"]))

    # ---- skipTrigger ----
    _r, calls = run(TR.run_convert, cards, O, new, [8], [31], "NEWNAME", 40, skip_trigger=True, old_cards=[old])
    chk("skipTrigger ⇒ 不发 0x22，其它照跑", "OnOtherCardConverted" not in [h for h, _n in names(calls)]
        and ("OnEnterPlay", "NEW") in names(calls), str(names(calls)))

    # ---- 手牌里的旧牌 / 被压制的旧牌 ----
    _r, calls = run(TR.run_convert, cards, O, None, [9], [-7001], "NEWNAME", 40, old_cards=[hand_old])
    chk("旧牌在手牌 ⇒ 没有离场族（离场钩子只发在场的牌）", [h for h, _n in names(calls)] == ["OnOtherCardConverted"],
        str(names(calls)))
    sold = card(8, "OLD", "frontline", OPP, is_suppressed=True)
    _r, calls = run(TR.run_convert, [sold, w, new], O, None, [8], [-7001], "NEWNAME", 40, old_cards=[sold])
    seq = names(calls)
    chk("被压制的旧牌：自己的三个钩子不跑，旁观者 0x2E/0x2F/0x8 仍跑",
        ("OnLeaveBoardOrOwner", "OLD") not in seq and ("OnCardLocationMoved", "OLD") not in seq
        and ("OnAfterLeaveBoard", "OLD") not in seq
        and [h for h, n in seq if n == "W"][:3] == ["OnOtherCardLeaveBoardOrOwner", "OnOtherCardLocationMoved",
                                                   "OnAfterOtherCardLeaveBoardOrOwner"], str(seq))

    # ---- 多张：逐张旧牌先于新牌；0x22 只一轮；新牌互相不吃 0x22 ----
    old2 = card(18, "OLD2", "back", OPP)
    new2 = card(32, "NEW2", "back", OPP)
    O2 = ovr(**{"8": OLD_H, "18": OLD_H, "20": W_H, "31": NEW_H22, "32": NEW_H22})
    _r, calls = run(TR.run_convert, [old, old2, w, new, new2], O2, [new, new2], [8, 18], [31, 32], "N", 40,
                    old_cards=[old, old2])
    seq = names(calls)
    first_new = min(i for i, (h, _n) in enumerate(seq) if h == "OnEnterPlay")
    last_old = max(i for i, (h, n) in enumerate(seq) if n in ("OLD", "OLD2") or h.endswith("LeaveBoardOrOwner"))
    chk("多张：两张旧牌的离场族都在新牌 OnEnterPlay 之前", last_old < first_new and
        [n for h, n in seq if h == "OnLeaveBoardOrOwner"] == ["OLD", "OLD2"], str(seq))
    chk("多张：OnEnterPlay 按新牌次序，0x22 整次转化只发一轮（W 一次；NEW/NEW2 不吃）",
        [n for h, n in seq if h == "OnEnterPlay"] == ["NEW", "NEW2"]
        and [n for h, n in seq if h == "OnOtherCardConverted"] == ["W", ] + [n for h, n in seq if h == "OnOtherCardConverted"][1:]
        and ("OnOtherCardConverted", "NEW") not in seq and ("OnOtherCardConverted", "NEW2") not in seq
        and len([1 for h, n in seq if h == "OnOtherCardConverted"]) == 1, str(seq))

    # ---- convert_fx 聚合（按旧牌 id / 新牌 / 0x22 分开）----
    def behave(h, c, args):
        recs = {"OnOtherCardLeaveBoardOrOwner": 2, "OnOtherCardConverted": 3, "OnEnterPlay": 5}
        if h in recs:
            return {"records": [{"verb": "GiveKreditsBySide", "args": [int(ME), recs[h], 0], "tainted": False}]}
        return {}
    fx, _calls = run(TR.convert_fx, cards, O, [8], [-7001], "NEWNAME", 40, False, old_cards=[old], new_card=None,
                     behave=behave)
    chk("convert_fx：old[旧 id]/after 分开；没有新牌 ⇒ new 空 + gaps 记缺口",
        fx["old"].get(8, {}).get("kredit") == 2 and fx["after"].get("kredit") == 3 and fx["new"] == {}
        and any("新牌实例不存在" in g for g in fx["gaps"]) and fx["unverified"], str(fx))
    fx2, _calls = run(TR.convert_fx, cards, O, [8], [31], "NEWNAME", 40, False, old_cards=[old], new_card=new,
                      behave=behave)
    chk("convert_fx：给了新牌实例 ⇒ new 有后果、不再记新牌缺口", fx2["new"].get("kredit") == 5
        and not any("新牌实例不存在" in g for g in fx2["gaps"]), str(fx2))

    # ---- sim 消费 ----
    def mk(units, ef=None, **kw):
        return Sim({u.id: u for u in units}, {ME: 20, OPP: 20}, 5.0, {}, event_fx=ef or {}, my_side=ME, **kw)

    cv = {"ids": [8], "name": "NEWNAME", "atk": 1, "dfn": 1, "cost": 1, "opc": 1, "typ": "infantry",
          "kw": ["smokescreen", "guard"], "instigator": 40, "skip_trigger": False}
    key = (40, (8,), "NEWNAME")
    fxv = {"old": {8: {"kredit": 1}}, "new": {"kredit": 2}, "after": {"damage_own_hq": 2}, "gaps": ["convert：测试缺口"]}
    s = mk([U(8, OPP, "frontline", 3, 3, 3, "infantry", {"fury"}, armor=1)], {"convert": {key: fxv}})
    _apply_eff(s, {"convert": dict(cv)}, None)
    nu = [u for u in s.units.values()]
    chk("sim：旧牌换成出厂数值的新牌（同排、buff/重甲/关键词不继承），前线新牌去烟幕保留 guard",
        len(nu) == 1 and nu[0].id < 0 and nu[0].row == "frontline" and nu[0].kw == frozenset({"guard"})
        and nu[0].armor == 0 and (nu[0].atk, nu[0].dfn) == (1, 1), str([(u.id, u.row, sorted(u.kw)) for u in nu]))
    chk("sim：旧牌离场后果 +1、新牌后果 +2（指挥点 5→8），0x22 后果己方总部 -2；缺口并入", s.kredits == 8.0
        and s.hq[ME] == 18 and "convert：测试缺口" in s.gaps, str((s.kredits, s.hq, s.gaps)))
    s = mk([U(8, OPP, "frontline", 3, 3, 3, "infantry")], {"convert": {key: fxv}})
    _apply_eff(s, {"convert": dict(cv, skip_trigger=True)}, None)
    chk("sim：skipTrigger ⇒ 不结算 0x22（总部不掉血）", s.hq[ME] == 20 and s.kredits == 8.0, str((s.hq, s.kredits)))
    s = mk([U(8, OPP, "back", 3, 3, 3, "infantry")], {"convert": {key: fxv}})
    _apply_eff(s, {"convert": dict(cv)}, None)
    chk("sim：支援线的新牌保留烟幕（RemoveSmokescreen 只对前线）", [sorted(u.kw) for u in s.units.values()] == [["guard", "smokescreen"]])
    s = mk([U(8, OPP, "frontline", 3, 3, 3, "infantry")])
    _apply_eff(s, {"convert": dict(cv)}, None)
    chk("sim：没有预计算条目 ⇒ 记缺口、状态仍按出厂数值换牌", any("没有预计算" in g for g in s.gaps)
        and len(s.units) == 1, str(s.gaps))
    # 牌库里的转化：同下标换牌
    tpl = {i: H(i, "C%d" % i, 1, "infantry", 1, 1) for i in (7, 8, 9)}
    s = mk([], deck=[7, 8, 9], deck_cards=dict(tpl))
    _apply_eff(s, {"convert": dict(cv)}, None)
    chk("sim：牌库里的转化 ⇒ 同下标换成新牌（7, 新, 9），缺口如实记", s.deck[0] == 7 and s.deck[2] == 9 and s.deck[1] < 0
        and s.deck_cards[s.deck[1]].name == "NEWNAME" and 8 not in s.deck_cards
        and any("牌库里转化出的新牌" in g for g in s.gaps), str((s.deck, s.gaps)))
    s = mk([], deck=[7, 9], deck_cards={i: tpl[i] for i in (7, 9)})
    _apply_eff(s, {"convert": dict(cv)}, None)
    chk("sim：旧牌不在己方牌库/场上/手牌 ⇒ 什么都不换", s.deck == [7, 9] and not s.units)

    # ---- EVENT_FX_KINDS 登记 ----
    for k in ("convert", "damage", "abilities"):
        chk("EVENT_FX_KINDS 登记了 %s" % k, k in EVENT_FX_KINDS)
    print("失败 %d 项" % chk.fails)
    return chk.fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
