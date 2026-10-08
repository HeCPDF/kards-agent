#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§8-6 断言落盘：反制先手 / 0x18 摧毁效果倍增 / 打捞 / 例外表 / 死亡链 / 去重。

全部离线（`_Chain` 用 `object.__new__` 构造 + 打桩，不碰进程）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from types import SimpleNamespace as NS


from _cards import ME, OPP                              # noqa: E402
from kardsmem import gamemodel as GM                            # noqa: E402
from semantics import triggers as TR

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


def _obj(side, loc=GM.ECardLocation.Board_Frontline):
    """语义层读 `card.obj.side / Location / IsHQ()…`：假卡也带原版对象。"""
    return GM.BaseCardObject(side=side, Location=loc, Type=GM.EType.infantry)


def new_chain():
    return object.__new__(TR._Chain)


# ---- 反制先手（GetActiveGotchasOrdered 先收 OnOtherCardDealDamage）----
gotcha = NS(card_id=11, card_type="gotcha", gotcha_activated=1, fname="card_event_interception",
            name="INTERCEPTION", raw={"ptr": 101}, side=OPP, obj=_obj(OPP))
dealer = NS(card_id=7, card_type="infantry", gotcha_activated=0, fname="card_unit_x",
            name="X", raw={"ptr": 102}, side=ME, obj=_obj(ME))
to_card = NS(card_id=5, card_type="infantry", raw={"ptr": 103}, side=OPP, obj=_obj(OPP))
chain = new_chain()
chain.st = NS(cards=[gotcha, dealer])
chain.include_own = False
chain.suppressed = lambda c: False
chain.others = lambda hook, **kw: []
calls = []
chain.call = lambda hook, c, args: calls.append((hook, getattr(c, "card_id", 0)))
chain.deal_damage_effects(to_card, dealer, 3, 0)
chk("反制先手：先给已激活的反制发 OnOtherCardDealDamage",
    calls and calls[0] == ("OnOtherCardDealDamage", 11), str(calls))

# ---- 0x18 摧毁效果倍增（TriggerMultiple=1 ⇒ OnDestroyed 重放一次）----
victim = NS(card_id=21, name="V", card_type="infantry", raw={"ptr": 201}, location="frontline",
            is_suppressed=False, side=OPP, obj=_obj(OPP))
killer = NS(card_id=22, name="K", card_type="infantry", raw={"ptr": 202}, side=ME, obj=_obj(ME))
chain2 = new_chain()
chain2.include_own = True
chain2.in_combat = True
chain2.bucket = "def_destroyed"
chain2.card = lambda x: x
chain2.suppressed = lambda c: False
chain2.others = lambda hook, **kw: ([(victim, 1)] if hook == "OnDestructionEffectTriggered" else [])
destroyed_calls = []


def _call2(hook, c, args):
    if hook == "OnDestroyed":
        destroyed_calls.append(("OnDestroyed", args.get("TriggerNotDestroyed")))
    if hook == "OnDestructionEffectTriggered":
        return {"out": {"TriggerMultiple": 1}}
    return {}


chain2.call = _call2
chain2.salvage = lambda v, k: None
chain2.destroyed_fn(victim, killer, {id(victim)})
chk("0x18：OnDestroyed 先跑一次常规（False），再按 TriggerMultiple 重放（True）",
    destroyed_calls == [("OnDestroyed", False), ("OnDestroyed", True)], str(destroyed_calls))

# ---- 打捞（击杀者有 salvage、敌方、无 cantBeSalvagedd ⇒ salvage_ids）----
chain3 = new_chain()
chain3.hits, chain3.order = [], []
chain3.bucket = "def_destroyed"
chain3.card = lambda x: x
chain3.kw_of = lambda c: ({"salvage"} if getattr(c, "card_id", 0) == 22 else set())
chain3.salvage(victim, killer)
chk("打捞：salvage_ids=[被摧毁单位 card_id]", chain3.hits and chain3.hits[0]["eff"] == {"salvage_ids": [21]},
    str(chain3.hits))
chain4 = new_chain()
chain4.hits, chain4.order = [], []
chain4.bucket = "def_destroyed"
chain4.card = lambda x: x
chain4.kw_of = lambda c: set()                     # 击杀者没有 salvage
chain4.salvage(victim, killer)
chk("打捞：击杀者无 salvage ⇒ 不产出", not chain4.hits)

# ---- 例外表（SUPPRESSION_EXCEPTIONS 非空且命中逻辑正确）----
chk("例外表非空", len(TR.SUPPRESSION_EXCEPTIONS) > 0, "n=%d" % len(TR.SUPPRESSION_EXCEPTIONS))
nm0 = next(iter(TR.SUPPRESSION_EXCEPTIONS))
hook0 = sorted(TR.SUPPRESSION_EXCEPTIONS[nm0])[0]
c_ex = NS(fname=nm0, name=nm0)
chk("例外表：命中（%s → %s）" % (nm0, hook0), TR._suppression_excepted(c_ex, hook0) is True)
chk("例外表：未列出的 hook ⇒ False", TR._suppression_excepted(c_ex, "OnNope") is False)

# ---- 死亡链顺序（双方都死：逐阶段交错，先防守方后攻击方）----
att = NS(card_id=31, name="A", location="frontline", raw={}, obj=_obj(ME))
dfd = NS(card_id=32, name="D", location="frontline", raw={}, obj=_obj(OPP))
chain5 = new_chain()
chain5.card = lambda x: x
seq = []
for step in ("before_destroyed", "leave_board", "location_moved", "after_leave"):
    setattr(chain5, step, (lambda s: (lambda who, *a: seq.append((s, getattr(who, "card_id", 0)))))(step))
chain5.destroyed_fn = lambda who, killer, all_d: seq.append(("destroyed_fn", getattr(who, "card_id", 0)))
chain5.destroy_combat(att, dfd, True, True)
chk("死亡链：防守方先、攻击方后，逐阶段交错",
    seq == [("before_destroyed", 32), ("before_destroyed", 31),
            ("leave_board", 32), ("leave_board", 31),
            ("location_moved", 32), ("location_moved", 31),
            ("after_leave", 32), ("after_leave", 31),
            ("destroyed_fn", 32), ("destroyed_fn", 31)], str(seq))

# ---- 摧毁链去重（to_fx 默认不输出 def_destroyed/att_destroyed 桶）----
st = NS(cards=[])
hit = {"hook": "OnDestroyed", "name": "V", "ptr": 0, "side": OPP,
       "bucket": "def_destroyed", "eff": {"damage": 1}, "stopped": None,
       "out": {}, "records": [], "chance": [], "gaps": []}
res = {"hits": [hit], "defender": 0, "damage": None}
fx_dedup = TR.to_fx(res, st)
fx_full = TR.to_fx(res, st, dedupe_death=False)
chk("去重：默认不输出摧毁桶（death_fx 管）",
    "def_destroyed" not in (fx_dedup.get("buckets") or {}), str(fx_dedup.get("buckets")))
chk("去重：dedupe_death=False 时保留摧毁桶",
    "def_destroyed" in (fx_full.get("buckets") or {}), str(fx_full.get("buckets")))

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
