#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`engine.effectvm._direct_hooks`（P5 影子的直跑接缝，离线）：

* 已迁动词 ⇒ 直接改 `direct_state` 并记 `ctx.applied`；未迁动词 ⇒ 回退录制路径；
* 载荷型动词（ConvertCard）⇒ 在**调用点**现算载荷喂给 sink（算不出 ⇒ 不喂、记缺口、不改状态）；
* 注入的抽牌链 / spawn_stat 真的被 sink 用到。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from engine import effectvm as EV                                    # noqa: E402
from engine import state as S                                        # noqa: E402

ME, OPP = 1, 2
bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


class Rec:
    def __init__(self):
        self.ptr_ids = {}
        self.stream = None
        self.calls = []


def mk():
    return S.Sim({}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME, slots=5.0)


def main():
    # ① 已迁动词直改状态 + 未迁动词回退录制
    s = mk()
    rec = Rec()
    fb = {"SomeUnmigratedVerb": lambda vm, f, o, a, e: rec.calls.append(("fallback", a))}
    hooks, ctx = EV._direct_hooks(s, rec, fb, ME)
    hooks["GiveKreditsBySide"](None, None, None, [ME, 2, 0], None)
    chk("已迁动词（GiveKreditsBySide）直改状态：指挥点 5→7", s.kredits == 7.0, str(s.kredits))
    chk("ctx.applied 记下类型化调用", any(c[0] == "kredits" for c in ctx.applied), str(ctx.applied))
    hooks["SomeUnmigratedVerb"](None, None, None, [1], None)
    chk("未迁动词回退录制路径", rec.calls == [("fallback", [1])], str(rec.calls))

    # ② ConvertCard：载荷在调用点现算
    cv = {"ids": [11], "name": "NEW", "atk": 7, "dfn": 8, "cost": 3, "typ": "tank", "kw": (), "opc": 1,
          "armor": 0, "skip_trigger": False}
    s = mk()
    s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=())
    old = EV._convert_payload
    EV._convert_payload = lambda vm, a: dict(cv)
    try:
        hooks, ctx = EV._direct_hooks(s, Rec(), {}, ME)
        hooks["ConvertCard"](None, None, None, [[11], 7, "NEW", 0, False], None)
    finally:
        EV._convert_payload = old
    nu = [u for u in s.units.values() if u.id < 0]
    chk("ConvertCard：载荷现算后旧牌被换成新牌（atk7）", 11 not in s.units and len(nu) == 1 and nu[0].atk == 7,
        str(s.units))

    # ③ 载荷算不出 ⇒ 不改状态 + 记缺口（不编）
    s = mk()
    s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=())

    def _boom(vm, a):
        raise RuntimeError("读不到目标卡")
    EV._convert_payload = _boom
    try:
        hooks, ctx = EV._direct_hooks(s, Rec(), {}, ME)
        hooks["ConvertCard"](None, None, None, [[11], 7, "NEW", 0, False], None)
    finally:
        EV._convert_payload = old
    chk("载荷算不出 ⇒ 单位原样、缺口里写明原因", 11 in s.units and any("取不到载荷" in g for g in ctx.gaps), str(ctx.gaps))

    # ④ 注入的抽牌链被 sink 调用
    s = mk()
    got = []
    hooks, ctx = EV._direct_hooks(s, Rec(), {}, ME, on_draw=lambda st, n: got.append(n))
    chk("注入 on_draw 后直跑表里有抽牌 sink（调用细节由 test_scripts_direct 钉住）", "DrawTopCardFromDeck" in hooks)
    # ⑤ 门数据与录制路径共用 rec.cur_stats：buffable=True ⇒ ChangeAttack 改得动；缺门数据 ⇒ 记缺口不改
    s = mk()
    s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=())
    rec = Rec()
    rec.ptr_ids = {0xA0: 11}
    rec.cur_stats = {0xA0: {"buffable": True, "unrevealed_covert": False, "attack": 2, "attack_buff": 0}}
    hooks, ctx = EV._direct_hooks(s, rec, {}, ME)
    hooks["ChangeAttack"](None, None, None, [0xA0, 0xA0, 3, 1, False], None)
    chk("门数据共用 rec.cur_stats：buffable=True ⇒ 攻击 2→5", s.units[11].atk == 5, "atk=%s gaps=%s" % (s.units[11].atk, ctx.gaps))
    s = mk()
    s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=())
    rec = Rec()
    rec.ptr_ids = {0xA0: 11}
    hooks, ctx = EV._direct_hooks(s, rec, {}, ME)
    hooks["ChangeAttack"](None, None, None, [0xA0, 0xA0, 3, 1, False], None)
    chk("没有门数据 ⇒ 不改状态、缺口里写明『缺门数据』", s.units[11].atk == 2 and any("缺门数据" in g for g in ctx.gaps), str(ctx.gaps))

    # ⑥ 回归（2026-10-06 实机影子：AIR BLITZ / KM BISMARCK 的 diff_ab）：`DamageCard` 的目标是**总部牌指针**，
    #    而总部牌没被 VM 读过 ⇒ 不在 ptr_ids ⇒ 旧版 sink 记『目标指针换不出 card_id』并**没伤到总部**。
    #    `_direct_hooks` 现在按 `rec.hq_enemy/hq_own` 指针集（与字典路 `damage_hq`/`damage_own_hq` 同口径）认总部。
    from engine import calls as CALLS
    from sim import engine as _en
    HQE, HQO = 0xE0, 0xE1
    s = mk()
    s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=())
    rec = Rec()
    rec.ptr_ids = {0xA0: 11}
    rec.hq_enemy, rec.hq_own = frozenset([HQE]), frozenset([HQO])
    hooks, ctx = EV._direct_hooks(s, rec, {}, ME)
    hooks["DamageCard"](None, None, None, [HQE, 3, 11, False, False, False], None)
    chk("DamageCard(敌方总部指针, 3) ⇒ 敌方总部 20→17、没有『换不出 card_id』缺口",
        s.hq[OPP] == 17 and s.hq[ME] == 20 and not ctx.gaps, "hq=%s gaps=%s" % (s.hq, ctx.gaps))
    hooks["DamageCard"](None, None, None, [HQO, 4, 11, False, False, False], None)
    chk("DamageCard(己方总部指针, 4) ⇒ 己方总部 20→16", s.hq[ME] == 16 and s.hq[OPP] == 17, str(s.hq))
    # 对账：与字典路 `damage_hq`（`_apply_eff`）结果一致；C 路（调用重放）也一致
    s_a = mk()
    _en._apply_eff(s_a, {"damage_hq": 3}, None, src=None)
    s_c = mk()
    CALLS.apply_calls(s_c, [c for c in ctx.applied if c[0] == "damage_hq"][:1])
    chk("三方一致：字典路 damage_hq=3 / 直跑 / 调用重放 的敌方总部同为 17",
        s_a.hq[OPP] == 17 and s_c.hq[OPP] == 17, "A=%s C=%s" % (s_a.hq, s_c.hq))
    # 对照：没喂总部指针集 ⇒ 仍如实记缺口（不猜）
    s = mk()
    hooks, ctx = EV._direct_hooks(s, Rec(), {}, ME)
    hooks["DamageCard"](None, None, None, [0xBAD, 3, 11, False, False, False], None)
    chk("对照：不认识的指针 ⇒ 总部不动 + 缺口", s.hq[OPP] == 20 and ctx.gaps, "hq=%s gaps=%s" % (s.hq, ctx.gaps))
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
