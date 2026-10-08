#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`engine.scripts` 直跑缝（P4 第一刀）：资源族 sink + 回退设计 + **对账**。

对账是 P4 的退出条件之一（`REFACTOR-PLAN.md:51`："同一批卡牌的直跑结果与旧记录路径逐卡对账一致"）——
本文件把它缩到**单动词**粒度先钉住：`reconcile_one` 用同一组实参跑
"录制→`to_effects`→`_apply_eff`"（旧）与"直跑 sink"（新），要求两边状态**逐字段相等**。

每个判据都给两个会产生不同结论的输入（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import scripts as SC                                     # noqa: E402
from engine import state as S                                        # noqa: E402
# 对账的两条路要**注入**进 engine（engine 不许 import sim，理由见 `reconcile_one` 的注释）：
from engine.effectvm import to_effects as _to_effects                # noqa: E402
from sim.engine import _apply_eff as _apply_eff                      # noqa: E402
from engine.natives import stats as ST                               # noqa: E402

fails = 0
ME, OPP = 1, 2


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(kredits=2, slots=3, kredit_max=24):
    return S.Sim({}, {ME: 20, OPP: 20}, kredits, {}, my_side=ME, slots=slots,
                 kredit_max=kredit_max)


def run(hooks, verb, args):
    hooks[verb](None, None, None, list(args), None)
    return hooks["__ctx__"]


def main():
    # ① GiveKreditsBySide：我方加/减，出参 qqq=false
    h = SC.native_hooks(mk(kredits=2), my_side=ME)
    ctx = run(h, "GiveKreditsBySide", [ME, 5, 0, None])
    chk("`GiveKreditsBySide(我方, +5)` ⇒ kredits 2→7（夹取走 natives）",
        ctx.state.kredits == 7, "kredits=%s applied=%s" % (ctx.state.kredits, ctx.applied))
    chk("记下「真跑过」的动词（诊断用，不是效果字典）", ("kredits", "mine", 5) in ctx.applied, str(ctx.applied))

    h = SC.native_hooks(mk(kredits=2), my_side=ME)
    ctx = run(h, "GiveKreditsBySide", [OPP, 5, 0, None])
    chk("`GiveKreditsBySide(对方, +5)` ⇒ **opp_kredits** +5、我方 kredits 不变",
        ctx.state.kredits == 2 and getattr(ctx.state, "opp_kredits", None) == 5,
        "mine=%s opp=%s" % (ctx.state.kredits, getattr(ctx.state, "opp_kredits", None)))

    # ② 槽：+1 / −1 / 上限夹取
    h = SC.native_hooks(mk(slots=3), my_side=ME)
    ctx = run(h, "GainKreditSlot", [0, ME])
    chk("`GainKreditSlot(我方)` ⇒ slots 3→4", ctx.state.slots == 4, "slots=%s" % ctx.state.slots)
    h = SC.native_hooks(mk(slots=3), my_side=ME)
    ctx = run(h, "LoseKreditSlot", [ME])
    chk("`LoseKreditSlot(我方)` ⇒ slots 3→2", ctx.state.slots == 2, "slots=%s" % ctx.state.slots)
    h = SC.native_hooks(mk(slots=3, kredit_max=3), my_side=ME)
    ctx = run(h, "GainKreditSlot", [0, ME])
    chk("槽到 `kredit_max` ⇒ 夹住不超（3→3）", ctx.state.slots == 3, "slots=%s" % ctx.state.slots)

    # ③ setKreditSlotBySide：我方绝对值可直跑；对方只记缺口、不改状态
    h = SC.native_hooks(mk(slots=3), my_side=ME)
    ctx = run(h, "setKreditSlotBySide", [ME, 7])
    chk("`setKreditSlotBySide(我方, 7)` ⇒ 槽绝对值设成 7", ctx.state.slots == 7 and not ctx.gaps,
        "slots=%s gaps=%s" % (ctx.state.slots, ctx.gaps))
    h = SC.native_hooks(mk(slots=3), my_side=ME)
    ctx = run(h, "setKreditSlotBySide", [OPP, 7])
    chk("`setKreditSlotBySide(对方, 7)` ⇒ **记缺口、不改状态**（对方槽没有绝对值，不猜）",
        ctx.state.slots == 3 and getattr(ctx.state, "opp_slots", 0) == 0 and ctx.gaps,
        "slots=%s opp=%s gaps=%s" % (ctx.state.slots, getattr(ctx.state, "opp_slots", 0), ctx.gaps))

    # ④ 回退设计：已迁动词优先、未迁动词走 fallback（录制路径）
    seen = []

    def _fallback(vm, frame, obj, args, e):
        seen.append(args)
        return "rec"

    h = SC.native_hooks(mk(kredits=1), my_side=ME, fallback={"MoveCardToFrontline": _fallback,
                                                              "GiveKreditsBySide": _fallback})
    # ★ 2026-10-04 更正：原来这里用 `DamageCard` 当"未迁动词"，但第六刀之后它**已迁**（直跑优先）
    #   ⇒ 换成真正还没迁的 `MoveCardToFrontline`（移动族尚未直跑）。
    chk("未迁动词（`MoveCardToFrontline`）走 fallback",
        h["MoveCardToFrontline"](None, None, None, [1, 2], None) == "rec" and seen)
    r = h["GiveKreditsBySide"](None, None, None, [ME, 4, 0, None], None)
    chk("**已迁动词优先**（`GiveKreditsBySide` 没被 fallback 抢走）",
        r is None and h["__ctx__"].state.kredits == 5, "kredits=%s ret=%r" % (h["__ctx__"].state.kredits, r))

    # ⑤ 对账：旧路（录制→to_effects→_apply_eff）与新路（直跑）逐字段一致
    for verb, args, label in (("GiveKreditsBySide", [ME, 5, 0, None], "我方 +5"),
                              ("GiveKreditsBySide", [OPP, 3, 0, None], "对方 +3"),
                              ("GiveKreditsBySide", [ME, -5, 0, None], "我方 −5（夹到 0）")):
        a, b, diff = SC.reconcile_one(verb, args, make_state=mk, my_side=ME,
                                      to_effects=_to_effects, apply_eff=_apply_eff)
        chk("对账：`%s` %s ⇒ 旧路与新路一致（diff 空）" % (verb, label), not diff,
            "diff=%s" % diff)

    # ⑥ 状态族（第二刀）：pin / unpin / suppress，以及**事件不能丢**
    P1, P2 = 0x500, 0x501
    st = mk()
    st.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=("shock", "guard"), armor=2, tax=1)
    st.units[12] = S.U(12, OPP, "frontline", 2, 3, 1, "infantry")
    h = SC.native_hooks(st, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = run(h, "PinUnit", [P1])
    chk("`PinUnit` ⇒ 该单位 `pinned=True`，且**事件 0x3D 记进 ctx.events**（直跑不产 eff 字典，事件不能丢）",
        ctx.state.units[11].pinned and ("pin", 11) in ctx.events,
        "pinned=%s events=%s" % (ctx.state.units[11].pinned, ctx.events))

    h = SC.native_hooks(st, ptr_ids={P1: 11, P2: 12}, my_side=ME)   # 新 ctx（上一条的 events 不该串进来）
    ctx = run(h, "PinUnit", [0xDEAD])                      # 认不出的指针
    chk("`PinUnit` 目标认不出 ⇒ **记缺口、不改状态**",
        ctx.gaps and not ctx.events and not ctx.applied, "gaps=%s events=%s" % (ctx.gaps, ctx.events))

    h = SC.native_hooks(st, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = run(h, "RemovePin", [P1])
    chk("`RemovePin` ⇒ `pinned=False`（无事件）", ctx.state.units[11].pinned is False and not ctx.events,
        "pinned=%s events=%s" % (ctx.state.units[11].pinned, ctx.events))

    h = SC.native_hooks(st, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = run(h, "SuppressUnit", [P1])
    u = ctx.state.units[11]
    chk("`SuppressUnit` ⇒ 走 `natives.status.apply_suppress`：剥关键词/清重甲/清指向税/置 pinned/攻击次数 0",
        u.pinned and "shock" not in u.kw and "guard" not in u.kw and u.armor == 0 and u.tax == 0
        and u.attacks_left == 0, "kw=%s armor=%s tax=%s al=%s" % (u.kw, u.armor, u.tax, u.attacks_left))
    chk("`SuppressUnit` ⇒ 事件 0x3A 记下来，且把「没建模的那部分」如实记缺口",
        ("suppress", 11) in ctx.events and any("压制" in g for g in ctx.gaps), str(ctx.gaps))

    h = SC.native_hooks(st, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = run(h, "SuppressMultipleUnits", [[P1, P2]])
    chk("`SuppressMultipleUnits([p1,p2])` ⇒ **两张都被压制**、两条事件",
        ctx.state.units[11].pinned and ctx.state.units[12].pinned
        and ("suppress", 11) in ctx.events and ("suppress", 12) in ctx.events, str(ctx.events))

    # ⑦ 状态族对账：pin（带 target）与群体压制（id 型）
    def _mk_pin():
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=("shock",), armor=2, tax=1)
        s.units[12] = S.U(12, OPP, "frontline", 2, 3, 1, "infantry")
        return s

    a, b, diff = SC.reconcile_one("PinUnit", [P1], make_state=_mk_pin, my_side=ME, target=11,
                                  ptr_ids={P1: 11}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`PinUnit`（带 target）⇒ 旧路与新路一致", not diff, "diff=%s" % diff)
    a, b, diff = SC.reconcile_one("SuppressMultipleUnits", [[11, 12], None, 0], make_state=_mk_pin, my_side=ME,
                                  ptr_ids={}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`SuppressMultipleUnits`（**id 数组**，真实形状）⇒ 旧路与新路一致", not diff, "diff=%s" % diff)

    # ⑧ 更正（P4 第十八刀）：`*_aoe_ids` **本来就是 card_id**（BP 签名 `const TArray<int>*&`，
    #   真实调用点见 `SuppressUnit`：`MakeArray_Array = [ cardID ]; SuppressMultipleUnits(...)`）
    #   ⇒ 记录层**原样透传**。第一轮我拿自己手写的指针当证据加了映射，那是**回归**（真 id 会被映成 None）。
    from engine import effectvm as _EV                                    # noqa: E402

    def _eff_for(verb, args, ptr_ids):
        r = _EV.Recorder(0, 0, None)
        r.cur_stats = {}
        r.ptr_ids = dict(ptr_ids)
        r.records.append({"verb": verb, "args": list(args), "tainted": False})
        return _EV.to_effects(r, my_side=ME), r.gaps

    eff, gaps = _eff_for("SuppressMultipleUnits", [[11, 12], None, 0], {})
    chk("`*_aoe_ids` **原样透传 card_id**（喂 11/12 ⇒ 出 11/12；数组本来就是 id）",
        eff.get("suppress_aoe_ids") == [11, 12] and not gaps, "eff=%s gaps=%s" % (eff, gaps))

    # ⑨ 数值族（第三刀）：门 + 两字段模型
    def _mk_stat():
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 3, 5, 1, "infantry", atk_buff=2)   # 基础 1
        return s

    h = SC.native_hooks(_mk_stat(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"buffable": True, "unrevealed_covert": False}})
    ctx = run(h, "ChangeAttack", [P1, 7, 5, ST.TEMP_BUFF_GIVE, False, None])
    u = ctx.state.units[11]
    chk("`ChangeAttack`（门放过、ct=0）⇒ **buff 字段** 2→7、总量 3→8（原版两字段写对）",
        u.atk == 8 and u.atk_buff == 7, "atk=%s buff=%s" % (u.atk, u.atk_buff))

    h = SC.native_hooks(_mk_stat(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"buffable": False, "unrevealed_covert": False}})
    ctx = run(h, "ChangeAttack", [P1, 7, 5, ST.TEMP_BUFF_GIVE, False, None])
    chk("`ChangeAttack` 门为假（`CanCardBeBuffed`）⇒ **什么都不改**",
        ctx.state.units[11].atk == 3 and not ctx.applied, "atk=%s applied=%s"
        % (ctx.state.units[11].atk, ctx.applied))

    h = SC.native_hooks(_mk_stat(), ptr_ids={P1: 11}, my_side=ME)         # 没有门数据
    ctx = run(h, "ChangeAttack", [P1, 7, 5, ST.TEMP_BUFF_GIVE, False, None])
    chk("`ChangeAttack` 缺门数据 ⇒ **记缺口、不改状态**（不猜）",
        ctx.state.units[11].atk == 3 and ctx.gaps, "gaps=%s" % ctx.gaps)

    h = SC.native_hooks(_mk_stat(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"unrevealed_covert": True}})
    ctx = run(h, "ChangeHeavyArmor", [P1, 7, 2, ST.SET_VALUE, False, None])
    chk("`ChangeHeavyArmor` 目标**未揭示的隐蔽牌** ⇒ 静默不改（原版 `:10332-10340`）",
        ctx.state.units[11].armor == 0 and not ctx.applied, "armor=%s applied=%s"
        % (ctx.state.units[11].armor, ctx.applied))

    h = SC.native_hooks(_mk_stat(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"unrevealed_covert": False}})
    ctx = run(h, "ChangeHeavyArmor", [P1, 7, 2, ST.SET_VALUE, False, None])
    chk("`ChangeHeavyArmor` 已揭示 ⇒ 基础设成 clamp_armor(2)=2、总量 2",
        ctx.state.units[11].armor == 2 and ctx.applied, "armor=%s" % ctx.state.units[11].armor)

    # ⑩ 数值族对账：总量字段两边一致（`atk_buff` 是直跑**多做**的那部分，另行断言）
    def _mk_for_rec():
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 3, 5, 1, "infantry", atk_buff=2)
        return s

    a, b, diff = SC.reconcile_one("ChangeAttack", [P1, 7, 5, ST.TEMP_BUFF_GIVE, False, None],
                                  make_state=_mk_for_rec, my_side=ME, ptr_ids={P1: 11},
                                  gates={P1: {"buffable": True}},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`ChangeAttack`(ct=0) 的**总量**两边一致（`atk` 相同）",
        a.units[11].atk == b.units[11].atk == 8, "a=%s b=%s" % (a.units[11].atk, b.units[11].atk))
    chk("★ 直跑**多做**的那部分：`atk_buff` 也维护了（旧路只发总量增量、没有 buff 字段）",
        b.units[11].atk_buff == 7 and getattr(a.units[11], "atk_buff", None) == 2,
        "直跑 buff=%s 旧路 buff=%s" % (b.units[11].atk_buff, a.units[11].atk_buff))

    # ⑪ 防御族（第四刀）：≤0 ⇒ 摧毁（走 ctx.destroy）+ ct==2 的 after_set 事件
    def _mk_def():
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 4, 1, "infantry")
        return s

    h = SC.native_hooks(_mk_def(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"buffable": True}})
    ctx = run(h, "ChangeDefense", [P1, 7, 0, ST.SET_VALUE, False, None])
    chk("`ChangeDefense` 设成 0 ⇒ 单位**离场** + 记 `(\"destroy\", uid)` 事件（死亡链交调用方）",
        11 not in ctx.state.units and ("destroy", 11) in ctx.events, "events=%s" % ctx.events)
    chk("`ChangeDefense` ct==2 ⇒ 另记 `after_defense_set` 事件（`OnAfterDefenseIsSet` + 0x6）",
        ("after_defense_set", 11) in ctx.events, "events=%s" % ctx.events)

    h = SC.native_hooks(_mk_def(), ptr_ids={P1: 11}, my_side=ME,
                        gates={P1: {"buffable": False}})
    ctx = run(h, "ChangeDefense", [P1, 7, 0, ST.SET_VALUE, False, None])
    chk("`ChangeDefense` 门为假（`CanCardBeBuffed`）⇒ 什么都不改（不摧毁、无事件）",
        11 in ctx.state.units and not ctx.events and not ctx.applied,
        "events=%s applied=%s" % (ctx.events, ctx.applied))

    # ⑫ 防御族对账：`ct=2 设 0` 两条路都该"摧毁"（旧路 `set_defense` → `_def_delta` 的 ≤0 ⇒ 摧毁）
    a, b, diff = SC.reconcile_one("ChangeDefense", [P1, 7, 0, ST.SET_VALUE, False, None],
                                  make_state=_mk_def, my_side=ME, ptr_ids={P1: 11},
                                  gates={P1: {"buffable": True, "defense": 4}},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`ChangeDefense(2,设为 0)` ⇒ 两条路都把单位打掉（旧路的 `_def_delta` 与直跑的 `ctx.destroy` 一致）",
        (11 in a.units) == (11 in b.units) and not diff,
        "旧路在场上=%s 直跑在场上=%s diff=%s" % (11 in a.units, 11 in b.units, diff))

    # ⑬ 生命周期族（第五刀）：`DestroyCard`
    h = SC.native_hooks(_mk_def(), ptr_ids={P1: 11}, my_side=ME)
    ctx = run(h, "DestroyCard", [P1, P1])
    chk("`DestroyCard`：单位离场 + 两个事件（`before_other_card_destroyed` **先**、`destroy` 后，照原版 `:877`→`:882`）",
        11 not in ctx.state.units and ctx.events == [("before_other_card_destroyed", 11), ("destroy", 11)],
        "events=%s" % ctx.events)
    h = SC.native_hooks(_mk_def(), ptr_ids={P1: 11}, my_side=ME)
    ctx = run(h, "DestroyCard", [0xBAD])                   # 认不出的指针
    chk("`DestroyCard` 指针认不出 ⇒ **记缺口**（原版的 `!IsValid` 是「卡无效」，与我们「认不出」不是一回事）",
        ctx.gaps and not ctx.events and 11 in ctx.state.units, "gaps=%s" % ctx.gaps)

    a, b, diff = SC.reconcile_one("DestroyCard", [P1, P1], make_state=_mk_def, my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`DestroyCard` ⇒ 两条路都把它打掉（旧路 `destroy` 效果 → `_apply_death`）",
        (11 in a.units) == (11 in b.units) and not diff,
        "旧路在场上=%s 直跑在场上=%s diff=%s" % (11 in a.units, 11 in b.units, diff))

    # ⑭ 伤害族（第六刀）：`DamageCard` —— 含 excess、出参、以及"不在场 vs 认不出"的区分
    import types

    def _mk_dmg(t_dfn=3, t_atk=2, dealer_ab=(), hq=(20, 20)):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 5, 1, "infantry")            # 攻击方
        s.units[12] = S.U(12, OPP, "frontline", t_atk, t_dfn, 1, "infantry")
        s.units[11].ab = frozenset(dealer_ab)
        s.hq = {ME: hq[0], OPP: hq[1]}
        return s

    def _frame_for(idx):
        """造一个假 frame/e 只为一件事：看**出参**有没有被写（`write_out` 的约定）。"""
        e = types.SimpleNamespace(kids=[types.SimpleNamespace(args={"prop": "targetDestroyed"})] * (idx + 1))
        return types.SimpleNamespace(locals={}), e

    s = _mk_dmg(t_dfn=3)
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    fr, e = _frame_for(6)
    h["DamageCard"](None, fr, None, [P2, 5, 11, False, False, False], e)
    chk("`DamageCard` 5 点打 3 防 ⇒ 单位离场 + 出参 `targetDestroyed=True`（原版 `:16468-16472`）",
        12 not in s.units and fr.locals.get("targetDestroyed") is True,
        "在场上=%s out=%s" % (12 in s.units, fr.locals))
    s = _mk_dmg(t_dfn=9)
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    fr, e = _frame_for(6)
    h["DamageCard"](None, fr, None, [P2, 3, 11, False, False, False], e)
    chk("`DamageCard` 3 点打 9 防 ⇒ 活着、掉到 6、出参 `False`",
        12 in s.units and s.units[12].dfn == 6 and fr.locals.get("targetDestroyed") is False,
        "dfn=%s out=%s" % (s.units[12].dfn if 12 in s.units else None, fr.locals))

    s = _mk_dmg()
    s.units[12].immune = True
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    fr, e = _frame_for(6)
    h["DamageCard"](None, fr, None, [P2, 99, 11, False, False, False], e)
    chk("`DamageCard` 打**免疫**单位 ⇒ 一点不吃、也不死（原版 `:14823-14836`）",
        12 in s.units and s.units[12].dfn == 3, "dfn=%s" % s.units[12].dfn)

    s = _mk_dmg(dealer_ab=("excess",))                     # 攻击方带 excess
    s.hq = {ME: 20, OPP: 20}
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    h["DamageCard"](None, None, None, [P2, 8, 11, False, False, False], None)
    chk("`DamageCard` 攻击方带 `excess` ∧ 8 > 3 防 ⇒ 目标封顶吃 3、**溢出 5 打对面总部**（20→15）",
        12 not in s.units and s.hq[OPP] == 15, "hq=%s units=%s" % (s.hq, list(s.units)))
    s = _mk_dmg()                                          # 对照：不带 excess
    s.hq = {ME: 20, OPP: 20}
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    h["DamageCard"](None, None, None, [P2, 8, 11, False, False, False], None)
    chk("对照：攻击方**不带** `excess` ⇒ 8 点全打在目标上、总部不动",
        s.hq[OPP] == 20 and 12 not in s.units, "hq=%s" % s.hq)

    s = _mk_dmg()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = h["__ctx__"]
    h["DamageCard"](None, None, None, [0xBAD, 5, 11, False, False, False], None)   # 认不出
    chk("`DamageCard` 指针认不出 ⇒ **记缺口**", ctx.gaps, "gaps=%s" % ctx.gaps)
    s2 = _mk_dmg()
    h2 = SC.native_hooks(s2, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx2 = h2["__ctx__"]
    h2["DamageCard"](None, None, None, [P1, 5, P1, False, False, False], None)     # 在 ptr_ids 但…（P1 在场上）
    chk("（对照）目标在场上 ⇒ 正常结算，不记缺口", not ctx2.gaps, "gaps=%s" % ctx2.gaps)

    a, b, diff = SC.reconcile_one("DamageCard", [P2, 3, P1, False, False, False],
                                  make_state=lambda: _mk_dmg(t_dfn=9), my_side=ME,
                                  ptr_ids={P1: 11, P2: 12}, target=12,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`DamageCard` 3 点打 9 防 ⇒ 两条路都掉到 6（旧路 `damage` 效果 → `_damage_card`）",
        a.units[12].dfn == b.units[12].dfn == 6 and not diff,
        "旧=%s 直跑=%s diff=%s" % (a.units[12].dfn, b.units[12].dfn, diff))

    # ⑮ 抽牌族（第七刀）：`DrawTopCardFromDeck` —— 旧字典里**根本没有这个动词**
    calls = []
    s = mk()
    h = SC.native_hooks(s, ptr_ids={}, my_side=ME, on_draw=lambda st, n: calls.append(n))
    ctx = h["__ctx__"]
    h["DrawTopCardFromDeck"](None, None, None, [1, 7, False, False, False, 0.4, False, None], None)
    chk("`DrawTopCardFromDeck(我方)` ⇒ 调用注入的抽牌链 1 张（字典里没这个动词 ⇒ 旧路得靠解释函数体）",
        calls == [1] and ("draw", 1) in ctx.applied, "calls=%s applied=%s" % (calls, ctx.applied))
    chk("`DrawTopCardFromDeck` 如实记缺口：抽到的**卡对象出参**没建模（scry 路径会用它）",
        any("卡对象出参未建模" in g for g in ctx.gaps), str(ctx.gaps))

    calls2 = []
    s = mk()
    h = SC.native_hooks(s, ptr_ids={}, my_side=ME, on_draw=lambda st, n: calls2.append(n))
    ctx2 = h["__ctx__"]
    h["DrawTopCardFromDeck"](None, None, None, [2, 7, True, False, False, 0.4, False, None], None)
    chk("`DrawTopCardFromDeck(对方)` ⇒ **不抽**、记缺口（我方只有「已知牌数」这一档）",
        not calls2 and ctx2.gaps, "calls=%s gaps=%s" % (calls2, ctx2.gaps))

    s = mk()
    h = SC.native_hooks(s, ptr_ids={}, my_side=ME)          # 没注入抽牌链
    ctx3 = h["__ctx__"]
    h["DrawTopCardFromDeck"](None, None, None, [1, 7, False, False, False, 0.4, False, None], None)
    chk("`DrawTopCardFromDeck` 没注入 `on_draw` ⇒ 记缺口、不猜（不假装抽了）",
        any("没注入抽牌链" in g for g in ctx3.gaps), str(ctx3.gaps))

    # ⑯ 对账（这一族是**刻意的不对称**）：旧字典对这个动词一个字都没有 ⇒ A 路只出缺口、B 路真抽
    r = _EV.Recorder(0, 0, None)
    r.cur_stats = {}
    r.records.append({"verb": "DrawTopCardFromDeck",
                      "args": [1, 7, False, False, False, 0.4, False, None], "tainted": False})
    eff = _EV.to_effects(r, my_side=1)
    gaps = r.gaps
    chk("★ 抽牌族对账：旧路（**字典层** `to_effects` 单独跑）对这个动词**产出为空**，而直跑真抽 "
        "—— 字典层没有「未登记动词」的概念（实机链上是由 VM 解释它的函数体兜着的，见 sink 注释）",
        not eff and calls, "eff=%s 直跑 calls=%s" % (eff, calls))

    # ⑰ 撤退族（第八刀）：`MakeCardRetreat` —— 跳过 `cantRetreat`，且**不发**"被摧毁"事件
    def _mk_ret(ab=()):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry")
        s.units[11].ab = frozenset(ab)
        s.units[12] = S.U(12, OPP, "frontline", 2, 3, 1, "infantry")
        return s

    s = _mk_ret()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["MakeCardRetreat"](None, None, None, [[P1], 7], None)
    chk("`MakeCardRetreat([11])` ⇒ 单位离场 + 事件 `(\"retreat\", 11)`，**没有** `(\"destroy\", …)`（撤退≠摧毁）",
        11 not in s.units and ("retreat", 11) in ctx.events
        and not any(k == "destroy" for k, _ in ctx.events), "events=%s" % ctx.events)

    s = _mk_ret(ab=("cantRetreat",))
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["MakeCardRetreat"](None, None, None, [[P1], 7], None)
    chk("`MakeCardRetreat` 对带 `cantRetreat` 的单位 ⇒ **跳过**（还在场、无事件；原版 `:1727-1732`）",
        11 in s.units and not ctx.events and ("retreat_skipped", 11) in ctx.applied,
        "在场上=%s applied=%s" % (11 in s.units, ctx.applied))

    s = _mk_ret()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["MakeCardRetreat"](None, None, None, [[0xBAD], 7], None)
    chk("`MakeCardRetreat` 指针认不出 ⇒ 记缺口、不动状态", ctx.gaps and 11 in s.units, str(ctx.gaps))

    # ★ 顺序判据（两个输入会给出不同结论）：`on_event` 当场扇 ⇒ 扇的那一刻单位**还在场**
    seen = []
    s = _mk_ret()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME,
                        on_event=lambda kind, uid: seen.append((kind, uid, 11 in s.units)))
    h["MakeCardRetreat"](None, None, None, [[P1], 7], None)
    chk("★ 撤退的 0x36 是**在离场前**扇的（`on_event` 看到 `在场上=True`）—— 原版/`sim` 同序，"
        "否则 `_apply_unit_eff` 找不到目标",
        seen == [("retreat", 11, True)], "seen=%s" % seen)
    seen_after = []
    s = _mk_ret()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)      # 对照：不给 on_event
    ctx = h["__ctx__"]
    h["MakeCardRetreat"](None, None, None, [[P1], 7], None)
    seen_after = [(k, u, 11 in s.units) for k, u in ctx.events]
    chk("（对照）不给 `on_event` ⇒ 只有事件表，事后看**已经离场**（`在场上=False`）"
        "—— 这就是为什么需要 `on_event`", seen_after == [("retreat", 11, False)], "seen=%s" % seen_after)

    a, b, diff = SC.reconcile_one("MakeCardRetreat", [[P1], 7], make_state=_mk_ret, my_side=ME,
                                  ptr_ids={P1: 11}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`MakeCardRetreat` ⇒ 两条路都把单位撤下（旧路 `retreat` 效果 → 先扇事件再离场）",
        (11 in a.units) == (11 in b.units) and not diff,
        "旧路在场上=%s 直跑在场上=%s diff=%s" % (11 in a.units, 11 in b.units, diff))

    # ⑱ 关键词授予/移除族（第九刀，20 个动词）：只在"原先没有/原先有"时广播 0x1D
    def _mk_kw(kw=(), sick=True, attacks_left=0, acted=False):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=kw, sick=sick,
                          attacks_left=attacks_left, acted=acted)
        return s

    s = _mk_kw()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["GiveGuard"](None, None, None, [P1, 7], None)
    chk("`GiveGuard` 原先没有 ⇒ 加上 `guard` + **广播能力已变**（原版 0x1D 只在原先没有时发）",
        "guard" in s.units[11].kw and ctx.events == [("abilities_changed", 11)],
        "kw=%s events=%s" % (s.units[11].kw, ctx.events))
    s = _mk_kw(kw=("guard",))
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["GiveGuard"](None, None, None, [P1, 7], None)
    chk("（对照）`GiveGuard` 原先**已有** ⇒ 不重复广播（`ctx.events` 空）",
        "guard" in s.units[11].kw and not ctx.events, "events=%s" % ctx.events)

    s = _mk_kw(sick=True)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["GiveBlitz"](None, None, None, [P1, 7], None)
    chk("`GiveBlitz` ⇒ 附加规则 `sick=False`（与 `sim:609` 同口径）", s.units[11].sick is False)
    s = _mk_kw(attacks_left=1, acted=True)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["GiveFury"](None, None, None, [P1, 7], None)
    chk("`GiveFury`（已行动）⇒ `attacks_left = max(1, 1)` 不变；未行动则抬到 2（`sim:611`）",
        s.units[11].attacks_left == 1, "al=%s" % s.units[11].attacks_left)
    s = _mk_kw(attacks_left=0, acted=False)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["GiveFury"](None, None, None, [P1, 7], None)
    chk("（第二个输入）`GiveFury`（未行动）⇒ `attacks_left` 抬到 **2**",
        s.units[11].attacks_left == 2, "al=%s" % s.units[11].attacks_left)

    s = _mk_kw(kw=("guard", "shock"))
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["RemoveGuard"](None, None, None, [P1, 7], None)
    chk("`RemoveGuard` 原先有 ⇒ 删掉 + 广播", "guard" not in s.units[11].kw and ctx.events,
        "kw=%s events=%s" % (s.units[11].kw, ctx.events))
    s = _mk_kw(kw=("immune",))
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["RemoveImmune"](None, None, None, [P1, 7], None)
    chk("（例外）`RemoveImmune` 删掉但**不广播**（原版 `immune/alpine/salvage` 不广播 0x1D）",
        "immune" not in s.units[11].kw and not ctx.events, "events=%s" % ctx.events)

    a, b, diff = SC.reconcile_one("GiveGuard", [P1, 7], make_state=_mk_kw, my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`GiveGuard` ⇒ 两条路都加上 `guard`（旧路 `give` 效果 → `sim` 同一套广播规则）",
        "guard" in a.units[11].kw and "guard" in b.units[11].kw and not diff, "diff=%s" % diff)

    # ⑲ 手牌族（第十刀）：`ChangeKreditCost` —— 目标在**手牌**（不是场上 target）
    def _mk_hand(cost=3, cost_buff=1):
        s = mk()
        s.hand = {21: ST_H(21, "TEST ORDER", cost, "order", cost_buff=cost_buff)}
        return s

    from engine.state import H as ST_H                                  # noqa: E402

    s = _mk_hand(cost=3, cost_buff=1)                                   # 基础 2、buff 1
    h = SC.native_hooks(s, ptr_ids={P1: 21}, my_side=ME)
    h["ChangeKreditCost"](None, None, None, [P1, 7, 5, ST.SET_VALUE, True, False, False], None)
    chk("`ChangeKreditCost(ct=2,设为 5)` ⇒ 基础=5、总量 = clamp(5+1)=**6**（两字段模型 ✓）",
        s.hand[21].cost == 6 and s.hand[21].cost_buff == 1,
        "cost=%s buff=%s" % (s.hand[21].cost, s.hand[21].cost_buff))

    s = _mk_hand(cost=3, cost_buff=1)
    h = SC.native_hooks(s, ptr_ids={P1: 21}, my_side=ME)
    ctx = h["__ctx__"]
    h["ChangeKreditCost"](None, None, None, [P1, 7, 5, ST.VETERAN_SET, True, False, False], None)
    chk("★ `ChangeKreditCost(ct=5)` ⇒ **不改数值**、只发通知（`Label_1614`，与 `ChangeAttack` 的集合不同）",
        s.hand[21].cost == 3 and ("kredit_cost_changed", 21) in ctx.events, "events=%s" % ctx.events)

    s = _mk_hand(cost=3, cost_buff=1)
    h = SC.native_hooks(s, ptr_ids={P1: 21}, my_side=ME,
                        gates={P1: {"unrevealed_covert": True}})
    h["ChangeKreditCost"](None, None, None, [P1, 7, 5, ST.SET_VALUE, False, False, False], None)
    chk("门：`skipCovertCheck=false` ∧ 目标是未揭示的隐蔽牌 ⇒ 静默不改（原版）",
        s.hand[21].cost == 3, "cost=%s" % s.hand[21].cost)
    s = _mk_hand(cost=3, cost_buff=1)
    h = SC.native_hooks(s, ptr_ids={P1: 21}, my_side=ME,
                        gates={P1: {"unrevealed_covert": False}})
    h["ChangeKreditCost"](None, None, None, [P1, 7, 5, ST.SET_VALUE, False, False, False], None)
    chk("（第二个输入）门：已揭示 ⇒ 正常设值到 6", s.hand[21].cost == 6, "cost=%s" % s.hand[21].cost)

    a, b, diff = SC.reconcile_one("ChangeKreditCost", [P1, 7, 5, ST.SET_VALUE, True, False, False],
                                  make_state=_mk_hand, my_side=ME, ptr_ids={P1: 21},
                                  gates={P1: {"cost": 3, "cost_buff": 1}},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`ChangeKreditCost`(ct=2,设为 5) ⇒ 两条路手牌费用都到 6（旧路 `cost_ids` → `sim` 落到 `hand`）",
        a.hand[21].cost == b.hand[21].cost == 6 and not diff,
        "旧=%s 直跑=%s diff=%s" % (a.hand[21].cost, b.hand[21].cost, diff))

    # ⑳ AOE 族（第十一刀）：数组动词 —— 摧毁 / 移出（不算摧毁）/ 群体伤害（含打总部）
    P3_ = 0x502

    def _mk_aoe(dfn=3, hq=(20, 20)):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry")
        s.units[12] = S.U(12, OPP, "frontline", 2, dfn, 1, "infantry")
        s.hq = {ME: hq[0], OPP: hq[1]}
        return s

    s = _mk_aoe()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = h["__ctx__"]
    h["DestroyMultipleCards"](None, None, None, [[P1, P2], 7], None)
    chk("`DestroyMultipleCards([p1,p2])` ⇒ 两张都离场 + 两条 `(\"destroy\", …)` 事件（死亡链交调用方）",
        not s.units and ctx.events == [("destroy", 11), ("destroy", 12)], "events=%s" % ctx.events)

    s = _mk_aoe()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = h["__ctx__"]
    h["RemoveMultipleCardsFromBoard"](None, None, None, [[P1, P2], 7], None)
    chk("★ 对照：`RemoveMultipleCardsFromBoard` ⇒ 也离场但**一条事件都不发**（「离场但不算被摧毁 ⇒ 不触发 death_fx」）",
        not s.units and not ctx.events, "events=%s" % ctx.events)

    s = _mk_aoe(dfn=3)
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    h["DamageMultipleCards"](None, None, None, [[P1, P2], 2, 7], None)
    chk("`DamageMultipleCards([p1,p2], 2)` ⇒ **两张各吃 2**（不是只打一张）",
        s.units[11].dfn == 1 and s.units[12].dfn == 1,
        "dfn=%s/%s" % (s.units[11].dfn, s.units[12].dfn))

    HQ_PTR, HQ_CID = 0x600, 99
    s = _mk_aoe(hq=(20, 20))
    h = SC.native_hooks(s, ptr_ids={P1: 11, HQ_PTR: HQ_CID}, my_side=ME,
                        hq_card_ids={HQ_CID: OPP})
    ctx = h["__ctx__"]
    h["DamageMultipleCards"](None, None, None, [[HQ_PTR], 3, 7], None)
    chk("★ `DamageMultipleCards` 数组里是**敌方总部**的 card id ⇒ 打总部（旧路 `effectvm:728` 的 `damage_hq` 拆分）",
        s.hq[OPP] == 17 and ("damage_hq", OPP, 3) in ctx.applied, "hq=%s applied=%s" % (s.hq, ctx.applied))
    s = _mk_aoe(hq=(20, 20))
    h = SC.native_hooks(s, ptr_ids={P1: 11, HQ_PTR: HQ_CID}, my_side=ME,
                        hq_card_ids={HQ_CID: OPP})
    h["DamageMultipleCards"](None, None, None, [[P1], 3, 7], None)
    chk("（第二个输入）同一个动词、给的是**单位**指针 ⇒ 打单位、总部不动",
        s.hq[OPP] == 20 and 11 not in s.units, "hq=%s units=%s" % (s.hq, list(s.units)))

    a, b, diff = SC.reconcile_one("DestroyMultipleCards", [[11, 12], 7], make_state=_mk_aoe, my_side=ME,
                                  ptr_ids={},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`DestroyMultipleCards`（**id 数组**）⇒ 两条路都把两张打掉（旧路 `destroy_aoe_ids` → `_apply_death`）",
        (11 in a.units) == (11 in b.units) and (12 in a.units) == (12 in b.units) and not diff,
        "旧=%s/%s 直跑=%s/%s diff=%s" % (11 in a.units, 12 in a.units, 11 in b.units, 12 in b.units, diff))

    # ㉑ 自定义能力族（第十二刀）：`ability` 在 0、`cardID` 在 1（**ID 不是指针**）+ 计数语义
    def _mk_ca(buffable=True):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry")
        return s

    s = _mk_ca()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={11: {"buffable": True}})
    h["CustomAbilityAdd"](None, None, None, ["cantRetreat", 11, 7, True, True], None)
    chk("`CustomAbilityAdd(ability, **cardID**)` ⇒ `ab[\"cantRetreat\"]=1`（实参位次：ability 在 0、ID 在 1）",
        s.units[11].ab.get("cantRetreat") == 1, str(s.units[11].ab))
    h["CustomAbilityAdd"](None, None, None, ["cantRetreat", 11, 7, True, True], None)
    chk("再给一次 ⇒ 计数 **2**（原版是计数不是布尔）", s.units[11].ab.get("cantRetreat") == 2,
        str(s.units[11].ab))

    s = _mk_ca()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={P1: {"buffable": True}})
    ctx = h["__ctx__"]
    h["CustomAbilityAdd"](None, None, None, ["cantRetreat", P1, 7, True, True], None)   # 喂**指针**（错位）
    chk("★ 把**指针**当 `cardID` 喂 ⇒ 认不出（记缺口、不改）—— 这正是 `DamageCard` 那次同一个坑",
        not s.units[11].ab and ctx.gaps, "ab=%s gaps=%s" % (s.units[11].ab, ctx.gaps))

    s = _mk_ca()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={11: {"buffable": False}})
    h["CustomAbilityAdd"](None, None, None, ["cantRetreat", 11, 7, True, True], None)
    chk("门：`CanCardBeBuffed` 为假 ⇒ 不改（原版 `:5769-5772`）", not s.units[11].ab, str(s.units[11].ab))

    s = _mk_ca()
    s.units[11].ab = {"cantRetreat": 2}
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["CustomAbilityRemove"](None, None, None, ["cantRetreat", 11, 7, False, None], None)
    chk("`CustomAbilityRemove(RemoveAllGivers=false)` 从 2 ⇒ **1，能力还在**（`in` 仍为真）",
        s.units[11].ab.get("cantRetreat") == 1 and "cantRetreat" in s.units[11].ab,
        str(s.units[11].ab))
    h["CustomAbilityRemove"](None, None, None, ["cantRetreat", 11, 7, False, None], None)
    chk("（第二个输入）再删一次 ⇒ 计数到 0 ⇒ **能力消失**（`in` 为假）",
        "cantRetreat" not in s.units[11].ab, str(s.units[11].ab))

    # ㉒ 对账：`CustomAbilityAdd` 2026-10-07 起**不再**被录制侧忽略（记成 `ability_grants` ⇒ 字典路/直跑/重放三条路同账：计数 + giver）；
    #    `CustomAbilityRemove` 仍是忽略动词（giver 级删除没建模，sink 记缺口）⇒ A 路空。
    r = _EV.Recorder(0, 0, None)
    r.cur_stats = {}
    r.records.append({"verb": "CustomAbilityAdd", "args": ["cantRetreat", 11, 7, True, True], "tainted": False})
    eff = _EV.to_effects(r, my_side=1)
    chk("★ 自定义能力族：录制侧把 `CustomAbilityAdd` 记成 `ability_grants=[名, 单位 id, 授予者 id]`（不再整个忽略）",
        eff.get("ability_grants") == [["cantRetreat", 11, 7]], "eff=%s" % eff)
    r2 = _EV.Recorder(0, 0, None)
    r2.cur_stats = {}
    r2.records.append({"verb": "CustomAbilityRemove", "args": ["cantRetreat", 11, 7, False, None], "tainted": False})
    chk("`CustomAbilityRemove` 仍被录制侧忽略（giver 级删除未建模）", not _EV.to_effects(r2, my_side=1))

    # ㉓ 指向税 + by-side 增量槽（第十三刀）
    def _mk_tax(tax=0):
        s = mk(slots=3)
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", tax=tax)
        return s

    s = _mk_tax(tax=1)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    _e = types.SimpleNamespace(kids=[types.SimpleNamespace(args={"prop": "qqq"})] * 4)
    fr = types.SimpleNamespace(locals={})
    h["AddKreditsTax"](None, fr, None, [P1, 3, 7, None], _e)
    chk("`AddKreditsTax(+3)` ⇒ 指向税 1→4、出参 `qqq=False`（原版两条路都写 false `:7322`/`:7329`）",
        s.units[11].tax == 4 and fr.locals.get("qqq") is False,
        "tax=%s out=%s" % (s.units[11].tax, fr.locals))
    s = _mk_tax(tax=1)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    _e2 = types.SimpleNamespace(kids=[types.SimpleNamespace(args={"prop": "qqq"})] * 4)
    fr2 = types.SimpleNamespace(locals={})
    h["AddKreditsTax"](None, fr2, None, [P1, -5, 7, None], _e2)
    chk("★ 第二个输入：`AddKreditsTax(−5)` ⇒ 夹到 **0**（原版 `max(…, 0)` `:7310`）",
        s.units[11].tax == 0, "tax=%s" % s.units[11].tax)

    s = mk(slots=3)
    h = SC.native_hooks(s, my_side=ME)
    h["ChangeKreditSlotsBySide"](None, None, None, [1, 2, 7], None)
    chk("`ChangeKreditSlotsBySide(我方, +2)` ⇒ 槽 3→5（**增量**语义，位次 side 在 0、delta 在 1）",
        s.slots == 5, "slots=%s" % s.slots)
    s = mk(slots=3)
    h = SC.native_hooks(s, my_side=ME)
    h["ChangeKreditSlotsBySide"](None, None, None, [2, 2, 7], None)
    chk("（第二个输入）给**对方** ⇒ `opp_slots +2`、我方槽不动（增量对对方也算得出来 ✓）",
        s.opp_slots == 2 and s.slots == 3, "opp=%s slots=%s" % (s.opp_slots, s.slots))

    a, b, diff = SC.reconcile_one("AddKreditsTax", [P1, 3, 7, None], make_state=_mk_tax, my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`AddKreditsTax(+3)` ⇒ 两条路指向税都到 3（旧路 `kredits_tax` → `sim` 同一套夹取）",
        a.units[11].tax == b.units[11].tax == 3 and not diff,
        "旧=%s 直跑=%s diff=%s" % (a.units[11].tax, b.units[11].tax, diff))

    # ㉔ 揭示 + 单卡回手（第十四刀）
    def _mk_rev(kw=("covert", "shock")):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 3, 1, "infantry", kw=kw)
        return s

    s = _mk_rev()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["RevealCard"](None, None, None, [11, 7, None], None)     # cardID（**不是指针**）
    chk("`RevealCard(cardID)` ⇒ `kw` 里去掉 `covert`（原版 `hasCovert=false` `:9199`）+ 0x37 事件；"
        "`UpdateGuarded` 重算未建模 ⇒ 如实记缺口",
        "covert" not in s.units[11].kw and ("reveal", 11) in ctx.events
        and any("UpdateGuarded" in g for g in ctx.gaps),
        "kw=%s events=%s" % (s.units[11].kw, ctx.events))
    s = _mk_rev()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["RevealCard"](None, None, None, [0xBAD, 7, None], None)
    chk("（第二个输入）`RevealCard` 给一个**认不出**的 cardID ⇒ 记缺口、不动状态",
        "covert" in s.units[11].kw and ctx.gaps and not ctx.events, "gaps=%s" % ctx.gaps)

    s = _mk_rev()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["MoveUnitFromBoardToOwnersHand"](None, None, None, [P1, 7], None)
    chk("`MoveUnitFromBoardToOwnersHand(ptr)` ⇒ 离场 + `(\"retreat\", 11)` 事件；"
        "『进手牌』未建模 ⇒ 记缺口（不假装塞了一张）",
        11 not in s.units and ("retreat", 11) in ctx.events
        and any("进原主手牌" in g for g in ctx.gaps), "events=%s gaps=%s" % (ctx.events, ctx.gaps))
    s = _mk_rev()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    ctx = h["__ctx__"]
    h["MoveUnitFromBoardToOwnersHand"](None, None, None, [P2, 7], None)   # 12 在 ptr_ids 里但不在场上
    chk("（第二个输入）已知但**不在场** ⇒ 原版门（`IsUnit ∧ IsLocatedOnBoard`）不通过 = **合法 no-op**、不记缺口",
        not ctx.gaps and not ctx.events, "gaps=%s" % ctx.gaps)

    a, b, diff = SC.reconcile_one("RevealCard", [11, 7, None], make_state=_mk_rev, my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`RevealCard` ⇒ 两条路都把 `covert` 摘掉（旧路 `reveal` 效果 → `sim:718` 同一件事）",
        "covert" not in a.units[11].kw and "covert" not in b.units[11].kw and not diff,
        "kw 旧=%s 直跑=%s diff=%s" % (a.units[11].kw, b.units[11].kw, diff))

    # ㉕ `setAndEncrypt*` 叶写入族（第十五刀）：直跑**没有影子**，直接写字段
    def _mk_enc(atk=3, atk_buff=2, dfn=5, cost=3, cost_buff=1):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", atk, dfn, 1, "infantry", atk_buff=atk_buff)
        s.hand = {21: ST_H(21, "TEST ORDER", cost, "order", cost_buff=cost_buff)}
        return s

    s = _mk_enc()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 21}, my_side=ME)
    h["setAndEncryptAttack"](None, None, None, [P1, 7, 1, 2, 3], None)
    chk("`setAndEncryptAttack(7)` ⇒ 基础=7、总量 = clamp(7+buff 2)=**9**（叶写入不夹基础，只夹总量）",
        s.units[11].atk == 9 and s.units[11].atk_buff == 2,
        "atk=%s buff=%s" % (s.units[11].atk, s.units[11].atk_buff))
    h["setAndEncryptAttackBuff"](None, None, None, [P1, 0, 1, 2, 3], None)
    chk("（第二个输入）`setAndEncryptAttackBuff(0)` ⇒ buff 清 0、总量回到基础 **7**",
        s.units[11].atk == 7 and s.units[11].atk_buff == 0,
        "atk=%s buff=%s" % (s.units[11].atk, s.units[11].atk_buff))

    s = _mk_enc()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["setAndEncryptDefense"](None, None, None, [P1, 0, 1, 2, 3], None)
    chk("★ `setAndEncryptDefense(0)` ⇒ 防御写成 0，但**单位不离场**（摧毁是**调用点**的规则，"
        "叶写入只管写 —— 与 `ChangeDefense` 的 ≤0 摧毁区分开）",
        s.units[11].dfn == 0 and 11 in s.units and not ctx.gaps,
        "dfn=%s 在场上=%s gaps=%s" % (s.units[11].dfn, 11 in s.units, ctx.gaps))

    s = _mk_enc(cost=3, cost_buff=1)
    h = SC.native_hooks(s, ptr_ids={P2: 21}, my_side=ME)
    h["setAndEncryptKredit"](None, None, None, [P2, 5, 1, 2, 3], None)
    chk("`setAndEncryptKredit(5)` 目标是**手牌**（走 `hand_card_of`）⇒ 基础=5、总量 = clamp(5+1)=6",
        s.hand[21].cost == 6 and s.hand[21].cost_buff == 1,
        "cost=%s buff=%s" % (s.hand[21].cost, s.hand[21].cost_buff))

    s = _mk_enc()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["setAndEncryptAttack"](None, None, None, [0xBAD, 7, 1, 2, 3], None)
    chk("`setAndEncryptAttack` 指针认不出 ⇒ 记缺口、不动状态", ctx.gaps and s.units[11].atk == 3,
        str(ctx.gaps))

    r = _EV.Recorder(0, 0, None)
    r.cur_stats = {0x500: {"attack": 1, "attackBuff": 2}}
    r.records.append({"verb": "setAndEncryptAttack", "args": [0x500, 7, 1, 2, 3], "tainted": False})
    eff = _EV.to_effects(r, my_side=1)
    chk("★ 叶写入族对账（刻意不对称）：**字典层** `to_effects` 单独跑它产出为空 —— 录制侧它是 "
        "`RECORD_ONLY`（先写影子、换算在别处），而直跑**没有影子**、直接写字段",
        not eff, "eff=%s" % eff)

    # ㉖ 第十六刀那批（战斗/治疗/离场/前线/重置/定住回合）
    def _mk_two(front_owner=None):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, 5, 1, "infantry")
        s.units[12] = S.U(12, OPP, "frontline", 2, 3, 1, "infantry")
        if front_owner is not None:
            s.front_owner = front_owner
        return s

    s = _mk_two()
    h = SC.native_hooks(s, ptr_ids={P1: 11, P2: 12}, my_side=ME)
    h["MakeCardsFight"](None, None, None, [P1, P2, 7], None)
    chk("`MakeCardsFight(p1, p2)` ⇒ 互相吃对方 atk（己方 5→3、对方 3→1）—— 规则来自 "
        "`engine.natives.damage.apply_fight`，sink 只做指针→id 与回调",
        s.units[11].dfn == 3 and s.units[12].dfn == 1,
        "dfn=%s/%s" % (s.units[11].dfn, s.units[12].dfn))
    s = _mk_two()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["MakeCardsFight"](None, None, None, [P1, P2, 7], None)          # P2 没映射
    chk("（第二个输入）`MakeCardsFight` 有一个指针换不出 id ⇒ 记缺口、不结算",
        ctx.gaps and s.units[11].dfn == 5 and s.units[12].dfn == 3, str(ctx.gaps))

    def _mk_hurt(dfn=2, mdef=5):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, dfn, 1, "infantry", mdef=mdef)
        return s

    s = _mk_hurt()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["FullyHealCard"](None, None, None, [P1, 7], None)
    chk("`FullyHealCard`（防御 2 < maxDefense 5）⇒ 回满到 **5**",
        s.units[11].dfn == 5, "dfn=%s" % s.units[11].dfn)
    s = _mk_hurt(dfn=5, mdef=5)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["FullyHealCard"](None, None, None, [P1, 7], None)
    chk("（第二个输入）门槛不过（防御 == maxDefense）⇒ 什么都不做", s.units[11].dfn == 5,
        "dfn=%s" % s.units[11].dfn)
    s = _mk_hurt()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={11: {"heal_vetoed": True}})
    h["FullyHealCard"](None, None, None, [P1, 7], None)
    chk("0xC 否决（`gates[uid].heal_vetoed`）⇒ 不回血", s.units[11].dfn == 2, "dfn=%s" % s.units[11].dfn)

    s = _mk_two()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["RemoveCardFromBoard"](None, None, None, [P1, 7], None)
    chk("`RemoveCardFromBoard` ⇒ 离场但**零事件**（`remove_unit` 不算被摧毁，`sim:737` 同口径）",
        11 not in s.units and not ctx.events, "events=%s" % ctx.events)

    s = _mk_two()
    s.units[11].row = "back"
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["MoveUnitFromSupportToFrontLine"](None, None, None, [P1, 7], None)
    chk("`MoveUnitFromSupportToFrontLine`（前线**不是**对方的）⇒ 支援线 → 前线",
        s.units[11].row == "frontline", "row=%s" % s.units[11].row)
    s = _mk_two(front_owner=OPP)
    s.units[11].row = "back"
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["MoveUnitFromSupportToFrontLine"](None, None, None, [P1, 7], None)
    chk("（第二个输入）前线**属于对方** ⇒ 原版拒绝执行，还是留在支援线",
        s.units[11].row == "back", "row=%s" % s.units[11].row)

    s = _mk_two()
    s.units[11].attacks_left, s.units[11].acted, s.units[11].moved = 0, True, True
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["ResetUnitOperations"](None, None, None, [P1, 7], None)
    chk("`ResetUnitOperations`（无 fury）⇒ 攻击次数 = max(0,1)=1、`acted`/`moved` 清零",
        s.units[11].attacks_left == 1 and s.units[11].acted is False and s.units[11].moved is False,
        "al=%s acted=%s moved=%s" % (s.units[11].attacks_left, s.units[11].acted, s.units[11].moved))
    s = _mk_two()
    s.units[11].kw = {"fury"}
    s.units[11].attacks_left = 0
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["ResetUnitOperations"](None, None, None, [P1, 7], None)
    chk("（第二个输入）带 `fury` ⇒ 攻击次数抬到 **2**（`sim:726` 同口径）",
        s.units[11].attacks_left == 2, "al=%s" % s.units[11].attacks_left)

    s = _mk_two()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["ChangedPinnedTurns"](None, None, None, [P1, 7, 2], None)
    chk("`ChangedPinnedTurns(+2)` ⇒ 定住 + 扇 0x3D `(\"pin\", uid)`",
        s.units[11].pinned and ("pin", 11) in ctx.events, "events=%s" % ctx.events)
    h["ChangedPinnedTurns"](None, None, None, [P1, 7, -1], None)
    chk("（第二个输入）`ChangedPinnedTurns(−1)` ⇒ 解除定住",
        s.units[11].pinned is False, "pinned=%s" % s.units[11].pinned)

    a, b, diff = SC.reconcile_one("FullyHealCard", [P1, 7], make_state=_mk_hurt, my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`FullyHealCard` ⇒ 两条路都回满到 5（旧路 `heal` 效果 → `sim:618` 同一门槛）",
        a.units[11].dfn == b.units[11].dfn == 5 and not diff,
        "旧=%s 直跑=%s diff=%s" % (a.units[11].dfn, b.units[11].dfn, diff))

    # ㉗ 控制权/弃牌（第十七刀）+ 顺手修掉的真 bug（`salvage_ids`/`discard_ids` 存的是指针）
    def _eff_ids(verb, args, ptr_ids):
        r = _EV.Recorder(0, 0, None)
        r.cur_stats = {}
        r.ptr_ids = dict(ptr_ids)
        r.records.append({"verb": verb, "args": list(args), "tainted": False})
        return _EV.to_effects(r, my_side=ME), r.gaps

    eff, gaps = _eff_ids("SalvageUnit", [11, 7], {})
    chk("★ 撤回第十七刀的「修法」：`SalvageUnit` 的实参**本来就是 card_id**（签名 `TArray<int>`）⇒ 原样透传 11",
        eff.get("salvage_ids") == [11] and not gaps, "eff=%s gaps=%s" % (eff, gaps))
    eff, gaps = _eff_ids("DiscardCardFromHand", [77, 7], {})
    chk("★ 同上：`DiscardCardFromHand(int cardID, …)` 收的就是 **card_id** ⇒ 原样透传 77（上一轮加的映射是回归）",
        eff.get("discard_ids") == [77] and not gaps, "eff=%s" % eff)

    def _mk_ctl(front_extra=False, side=OPP):
        s = mk()
        s.units[11] = S.U(11, side, "frontline", 2, 3, 1, "infantry", sick=True, acted=True, moved=True)
        if front_extra:
            s.units[12] = S.U(12, ME, "frontline", 2, 3, 1, "infantry")
        return s

    s = _mk_ctl(front_extra=True)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    ctx = h["__ctx__"]
    h["TakeControlOfEnemyUnit"](None, None, None, [P1, 7, None], None)
    chk("`TakeControlOfEnemyUnit`（前线 >1 张）⇒ 归我方 + **挪到后排** + `sick/acted/moved` 清零 + "
        "攻击次数 1 + 扇 `(\"steal\", uid)`",
        s.units[11].side == ME and s.units[11].row == "back" and s.units[11].sick is False
        and s.units[11].acted is False and s.units[11].moved is False
        and s.units[11].attacks_left == 1 and ("steal", 11) in ctx.events,
        "side=%s row=%s al=%s events=%s" % (s.units[11].side, s.units[11].row, s.units[11].attacks_left, ctx.events))
    s = _mk_ctl(front_extra=False)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["TakeControlOfEnemyUnit"](None, None, None, [P1, 7, None], None)
    chk("（第二个输入）前线**只有它** ⇒ 留在前线（原版落点规则）", s.units[11].row == "frontline",
        "row=%s" % s.units[11].row)

    s = _mk_hand()
    h = SC.native_hooks(s, ptr_ids={P1: 21}, my_side=ME)
    ctx = h["__ctx__"]
    h["DiscardCardFromHand"](None, None, None, [21, 7], None)      # 实参是 **card ID**（`BP_CardFunctions.cpp:3271` 签名；以前误当指针）
    chk("`DiscardCardFromHand(card_id)` ⇒ 己方手牌里有这张 ⇒ 移走（`sim:743` 同口径）",
        21 not in s.hand and ("discard", 21) in ctx.applied, "hand=%s" % list(s.hand))
    s = _mk_hand()
    h = SC.native_hooks(s, ptr_ids={P1: 21, P2: 77}, my_side=ME)
    ctx = h["__ctx__"]
    h["DiscardCardFromHand"](None, None, None, [77, 7], None)
    chk("（第二个输入）不在我方手牌 ⇒ 记缺口（敌方手牌不建模）", ctx.gaps and not ctx.applied,
        str(ctx.gaps))

    a, b, diff = SC.reconcile_one("TakeControlOfEnemyUnit", [P1, 7, None],
                                  make_state=lambda: _mk_ctl(front_extra=True), my_side=ME,
                                  ptr_ids={P1: 11}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`TakeControlOfEnemyUnit` ⇒ 两条路都把单位抢过来（旧路 `steal` 效果 → 同一条已下沉的规则）",
        a.units[11].side == b.units[11].side == ME and a.units[11].row == b.units[11].row and not diff,
        "旧 side=%s row=%s 直跑 side=%s row=%s diff=%s"
        % (a.units[11].side, a.units[11].row, b.units[11].side, b.units[11].row, diff))

    # ㉘ 第十九刀：群体防御（**id 数组**）+ 牌库顶 + 抽牌/进手牌
    def _mk_deck(hand_ids=(21, 22), deck=(91, 92)):
        s = mk()
        s.hand = {i: ST_H(i, "H%d" % i, 2, "order") for i in hand_ids}
        s.deck = list(deck)
        s.deck_known = True
        s.deck_cards = {}
        s.card_templates = {}
        s.opp_cards = 0
        s.units[11] = S.U(11, ME, "frontline", 2, 4, 1, "infantry")
        s.units[12] = S.U(12, OPP, "frontline", 2, 4, 1, "infantry")
        return s

    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    h["AddDefenseToMultipleCards"](None, None, None, [[11, 12], 2, 7, None], None)
    chk("`AddDefenseToMultipleCards([11,12], +2)` ⇒ **两张各 +2**（数组里是 card **ID**，签名逐字）",
        s.units[11].dfn == 6 and s.units[12].dfn == 6, "dfn=%s/%s" % (s.units[11].dfn, s.units[12].dfn))
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["AddDefenseToMultipleCards"](None, None, None, [[11], -99, 7, None], None)
    chk("（第二个输入）负量降到 ≤0 ⇒ **摧毁**（`sim:756-763` 同口径）",
        11 not in s.units and ("destroy", 11) in ctx.events, "events=%s" % ctx.events)

    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [21, 7, 0, None], None)
    chk("`MoveCardToTopOfOwnersDeck(21)` ⇒ 手牌少这张 + **牌库顶**多这张（首参是 card **ID**）",
        21 not in s.hand and s.deck[0] == 21, "hand=%s deck=%s" % (list(s.hand), s.deck[:3]))
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [77, 7, 0, None], None)
    chk("（第二个输入）不在我方手牌 ⇒ 记缺口、不动牌库", ctx.gaps and s.deck == [91, 92], str(ctx.gaps))

    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    h["MoveMultipleCardsToTopOfOwnersDeck"](None, None, None, [[21, 22], 7, 0, None], None)
    chk("`MoveMultipleCardsToTopOfOwnersDeck([21,22])` ⇒ 两张都回牌库顶（数组是 **ID**）",
        not s.hand and s.deck[:2] == [22, 21], "hand=%s deck=%s" % (list(s.hand), s.deck[:4]))

    draws = []
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, on_draw=lambda st, n: draws.append(n))
    h["SpawnCardInHandBySide"](None, None, None, [1, "SOME CARD", 7, True, False, False], None)
    chk("`SpawnCardinHandbySide(我方)` ⇒ 手里**多一张按名字造的新牌**，**不抽牌、不碰牌库**（原版 `CreateCard`，不是抽牌；"
        "以前走抽牌链把牌库顶抽进手牌 ✗）",
        draws == [] and s.deck == [91, 92] and len(s.hand) == 3
        and any(h_.name == "SOME CARD" for h_ in s.hand.values())
        and any(a_[0] == "gain_hand" for a_ in h["__ctx__"].applied),
        "draws=%s deck=%s hand=%s" % (draws, s.deck, {k: v.name for k, v in s.hand.items()}))
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["SpawnCardInHandBySide"](None, None, None, [2, "SOME CARD", 7, True, False, False], None)
    chk("（第二个输入）给**对方** ⇒ 对方手牌只有张数：`opp_cards+1`、记 `opp_gain_hand`（与字典路 `opp_gain_cards` 同口径；2026-10-06 起不再是缺口）",
        s.opp_cards == 1 and ctx.applied == [("opp_gain_hand", "SOME CARD")], str((ctx.gaps, ctx.applied)))

    draws2 = []
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, on_draw=lambda st, n: draws2.append(n))
    h["DrawCardsFromDeckBySide"](None, None, None, [7, 1, 2, True, False, None, 0.0], None)
    chk("`DrawCardsFromDeckBySide(instigator, side=1, numCards=2)` ⇒ 抽 **2**（位次：side 在 1、张数在 2）",
        draws2 == [2], "draws=%s" % draws2)
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["DrawCardsFromDeckBySide"](None, None, None, [7, 2, 2, True, False, None, 0.0], None)
    chk("（第二个输入）side=2 ⇒ `opp_cards += 2`（与旧路 `opp_draw` 同口径），不抽自己的牌",
        s.opp_cards == 2 and ("opp_draw", 2) in ctx.applied, "opp_cards=%s" % s.opp_cards)

    draws3 = []
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, on_draw=lambda st, n: draws3.append(n))
    h["DrawSpecificCardFromDeckBySide"](None, None, None, [7, 55, 1, True], None)
    chk("`DrawSpecificCardFromDeckBySide(cardID=55, side=1)`：55 **不在牌库** ⇒ 原版 `Array_Contains` 假就 `return`，不抽（cardID 在 1、side 在 2）",
        draws3 == [] and s.deck == [91, 92], "draws=%s deck=%s" % (draws3, s.deck))
    draws3 = []
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, on_draw=lambda st, n: draws3.append(n))
    h["DrawSpecificCardFromDeckBySide"](None, None, None, [7, 92, 1, True], None)
    chk("`DrawSpecificCardFromDeckBySide(cardID=92)`：92 在牌库第 2 位 ⇒ 先挪到牌顶、再抽 1 张（抽**指定**的那张，不是牌库顶）",
        draws3 == [1] and s.deck[0] == 92, "draws=%s deck=%s" % (draws3, s.deck))

    a, b, diff = SC.reconcile_one("AddDefenseToMultipleCards", [[11, 12], 2, 7, None],
                                  make_state=_mk_deck, my_side=ME, ptr_ids={},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`AddDefenseToMultipleCards` ⇒ 两条路都各 +2（旧路 `defense_aoe_ids` 也是 **id**）",
        a.units[11].dfn == b.units[11].dfn == 6 and a.units[12].dfn == b.units[12].dfn == 6 and not diff,
        "旧=%s/%s 直跑=%s/%s diff=%s" % (a.units[11].dfn, a.units[12].dfn,
                                         b.units[11].dfn, b.units[12].dfn, diff))

    # ㉙ 第二十刀：本回合加攻 / 行动权 / 收缴
    def _mk_turn(atk=3):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", atk, 4, 1, "infantry")
        s.units[12] = S.U(12, OPP, "frontline", 5, 4, 2, "infantry")
        s.playing_side = ME
        s.hand = {}
        return s

    s = _mk_turn()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)
    h["AddAttackUntilEndOfTurn"](None, None, None, [P1, 7, 3], None)
    chk("`AddAttackUntilEndOfTurn(p1, +3)` ⇒ 总量 3→6 **且** `atk_turn` 记 3（回合末会消失的那部分）",
        s.units[11].atk == 6 and s.units[11].atk_turn == 3,
        "atk=%s atk_turn=%s" % (s.units[11].atk, s.units[11].atk_turn))

    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    h["ForceEndTurn"](None, None, None, [], None)
    chk("`ForceEndTurn()` ⇒ 行动权交给**对方**（录制侧 `END_TURN_VERBS` 的语义）",
        s.playing_side == OPP, "playing_side=%s" % s.playing_side)
    s = _mk_turn()
    s.playing_side = OPP
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["SetPlayingSide"](None, None, None, [1, None], None)
    chk("`SetPlayingSide(1)` ⇒ `playing_side` = 我方（side 在 0）", s.playing_side == ME,
        "playing_side=%s" % s.playing_side)
    h["SetActiveSide"](None, None, None, [2, None], None)
    chk("`SetActiveSide(2)` ⇒ `playing_side` = 对方", s.playing_side == OPP, "playing_side=%s" % s.playing_side)
    h["SwitchPlayingSide"](None, None, None, [1, None], None)
    chk("`SwitchPlayingSide(1)` ⇒ 同口径（录制侧把这四个都当 `SIDE_VERBS` 处理）",
        s.playing_side == ME and len(ctx.applied) == 3, "playing_side=%s applied=%s"
        % (s.playing_side, ctx.applied))

    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["SalvageMultipleUnits"](None, None, None, [[12], 7, None], None)
    _copies = list(s.hand.values())
    chk("`SalvageMultipleUnits([12])` ⇒ 走已端口的 `engine.natives.cards.apply_salvage`："
        "**手牌里多一张 1/1 复制**（cost/atk/dfn 都是 1）",
        len(_copies) == 1 and _copies[0].cost == 1 and _copies[0].atk == 1 and _copies[0].dfn == 1,
        "hand=%s" % {k: (v.cost, v.atk, v.dfn) for k, v in s.hand.items()})
    s = _mk_turn()
    s.hand = {i: ST_H(i, "H", 2, "order") for i in range(9)}          # 手牌满（HAND_CAP=9）
    h = SC.native_hooks(s, my_side=ME)
    h["SalvageMultipleUnits"](None, None, None, [[12], 7, None], None)
    chk("（第二个输入）手牌已满 ⇒ **不多出那张**（`apply_salvage` 的上限规则）", len(s.hand) == 9,
        "hand=%s" % len(s.hand))

    a, b, diff = SC.reconcile_one("AddAttackUntilEndOfTurn", [11, 7, 3], make_state=_mk_turn, my_side=ME,
                                  ptr_ids={}, target=11,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`AddAttackUntilEndOfTurn` ⇒ 两条路总量都到 6、`atk_turn` 都是 3，`diff={}`",
        a.units[11].atk == b.units[11].atk == 6 and a.units[11].atk_turn == b.units[11].atk_turn == 3
        and not diff,
        "旧=%s/%s 直跑=%s/%s diff=%s" % (a.units[11].atk, a.units[11].atk_turn,
                                         b.units[11].atk, b.units[11].atk_turn, diff))

    # ㉚ 第二十一刀：偷进牌库 / 分出胜负 / 回合倒数 / 随机弃牌
    s = _mk_deck()
    h = SC.native_hooks(s, ptr_ids={}, my_side=ME)
    ctx = h["__ctx__"]
    h["StealCardFromBoardToDeck"](None, None, None, [12, 7, 1, None], None)      # cardID=12、deckSide=1
    chk("`StealCardFromBoardToDeck(cardID=12, deckSide=我方)` ⇒ 单位**离场** + 同名进**己方牌库**",
        12 not in s.units and 12 in s.deck and not any("未建模" in g for g in ctx.gaps),
        "units=%s deck=%s gaps=%s" % (list(s.units), s.deck[:4], ctx.gaps))
    s = _mk_deck()
    h = SC.native_hooks(s, ptr_ids={}, my_side=ME)
    ctx = h["__ctx__"]
    h["StealCardFromBoardToDeck"](None, None, None, [12, 7, 2, None], None)      # deckSide=对方
    chk("（第二个输入）`deckSide=对方` ⇒ 仍然**离场**，但进对方牌库未建模 ⇒ 记缺口（我方牌库不动）",
        12 not in s.units and 12 not in s.deck and any("对方" in g for g in ctx.gaps),
        "deck=%s gaps=%s" % (s.deck[:4], ctx.gaps))

    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["EndMatch"](None, None, None, [1, 0.0], None)                            # winnerSide=1（我方赢）
    chk("`EndMatch(赢家=我方)` ⇒ **对方总部清零**（`sim:733-734` 同口径）+ 扇 `end_match`",
        s.hq[OPP] == 0 and ("end_match", OPP) in ctx.events, "hq=%s events=%s" % (s.hq, ctx.events))
    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    h["EndMatch"](None, None, None, [2, 0.0], None)                            # winnerSide=2（对方赢）
    chk("（第二个输入）`EndMatch(赢家=对方)` ⇒ **我方总部清零**（不是永远打对方）", s.hq[ME] == 0,
        "hq=%s" % s.hq)

    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["SetCountdown"](None, None, None, [11, 3, None], None)
    chk("`SetCountdown` ⇒ **只记缺口**（回合倒数机制未建模；不编一个没人读的字段）",
        ctx.gaps and not ctx.applied, str(ctx.gaps))
    h["DiscardRandomCardFromHand"](None, None, None, [1, 7, None], None)
    chk("`DiscardRandomCardFromHand` ⇒ **只记缺口**（随机点不能替游戏选一张）",
        any("随机" in g for g in ctx.gaps), str(ctx.gaps))

    a, b, diff = SC.reconcile_one("StealCardFromBoardToDeck", [12, 7, 1, None],
                                  make_state=_mk_deck, my_side=ME, ptr_ids={}, target=12,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`StealCardFromBoardToDeck` ⇒ 两条路都**离场**、直跑把 12 放进牌库；"
        "★ 旧路牌库最终是**空**——录制侧额外补了 `deck_shuffle`（BP 末尾必然洗牌），而 `sim` 没有随机流时"
        "把牌库标成未知并清空；直跑这一刀只调了这一个动词（洗牌是**另一个**动词调用）⇒ 差异**可解释**",
        (12 in a.units) == (12 in b.units) and 12 in b.deck and not a.deck and diff.get("deck"),
        "旧在场上=%s deck=%s 直跑在场上=%s deck=%s diff=%s"
        % (12 in a.units, a.deck, 12 in b.units, b.deck[:4], diff))

    # ㉛ 第二十二刀：`MakeVeteran`（规则从 sim 下沉到 engine；载荷由调用方喂）
    _VET = {"atk_to": 5, "atk_from": 2, "dfn_to": 6, "opc_to": 0, "opc_from": 2,
            "armor_to": 2, "armor_buff": 1,
            "kw": {"fury": True, "blitz": True, "guard": True, "shock": False}}

    def _mk_vet(dfn=3, kw=("shock",), opc=2, armor=1):
        s = mk()
        s.units[11] = S.U(11, ME, "frontline", 2, dfn, 1, "infantry", kw=kw, opc=opc, armor=armor)
        return s

    s = _mk_vet()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={11: {"vet": _VET}})
    ctx = h["__ctx__"]
    h["MakeVeteran"](None, None, None, [P1, None], None)
    u = s.units[11]
    chk("`MakeVeteran`（调用方喂了 `_vet` 载荷）⇒ 攻 2→**5**、防 3→**6**（mdef 同置）、行动费 2→**0**、"
        "重甲 1→**3**、关键词按载荷改（shock 掉、fury/blitz/guard 加、guard⇒去 smokescreen）、"
        "blitz⇒sick=False、扇 `veteran`",
        u.atk == 5 and u.dfn == 6 and u.mdef == 6 and u.opc == 0 and u.armor == 3
        and "shock" not in u.kw and {"fury", "blitz", "guard", "veteran"} <= set(u.kw)
        and u.sick is False and ("veteran", 11) in ctx.events,
        "atk=%s dfn=%s opc=%s armor=%s kw=%s events=%s"
        % (u.atk, u.dfn, u.opc, u.armor, sorted(u.kw), ctx.events))

    s = _mk_vet()
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME)          # 没喂载荷
    ctx = h["__ctx__"]
    h["MakeVeteran"](None, None, None, [P1, None], None)
    chk("（第二个输入）调用方**没喂** `_vet` 载荷 ⇒ 只打 `veteran` 标记 + **如实记缺口**（不编数值）",
        "veteran" in s.units[11].kw and s.units[11].atk == 2 and any("没取到" in g for g in ctx.gaps),
        "atk=%s kw=%s gaps=%s" % (s.units[11].atk, sorted(s.units[11].kw), ctx.gaps))

    s = _mk_vet(dfn=0)
    h = SC.native_hooks(s, ptr_ids={P1: 11}, my_side=ME, gates={11: {"vet": _VET}})
    h["MakeVeteran"](None, None, None, [P1, None], None)
    chk("（第三个输入）门槛不过（总防 0）⇒ **什么都不改**、也不打标记", "veteran" not in s.units[11].kw,
        "kw=%s" % sorted(s.units[11].kw))

    a, b, diff = SC.reconcile_one("MakeVeteran", [11, None], make_state=_mk_vet, my_side=ME,
                                  ptr_ids={}, target=11, gates={11: {"vet": _VET}},
                                  record_extra={"vet": _VET},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`MakeVeteran` ⇒ 两条路数值与关键词都一致（旧路 → 同一条已下沉的规则），`diff={}`",
        a.units[11].atk == b.units[11].atk == 5 and a.units[11].dfn == b.units[11].dfn == 6
        and set(a.units[11].kw) == set(b.units[11].kw) and not diff,
        "旧 atk/dfn/kw=%s/%s/%s 直跑=%s/%s/%s diff=%s"
        % (a.units[11].atk, a.units[11].dfn, sorted(a.units[11].kw),
           b.units[11].atk, b.units[11].dfn, sorted(b.units[11].kw), diff))

    # ㉜ 第二十三刀：生成到棋盘（面板由调用方喂；排满/无面板都不编）
    _STAT = {"atk": 4, "dfn": 5, "cost": 2, "typ": "tank", "kw": ("guard",)}

    def _mk_spawn(units=0):
        s = mk()
        for i in range(units):                                   # 塞满我方支援线（容量 4，不含总部）
            s.units[100 + i] = S.U(100 + i, ME, "back", 1, 1, 1, "infantry")
        s.spawn_stats = lambda n: _STAT if n == "TEST TANK" else None   # 旧路的面板来源（两侧同源 ✓）
        return s

    s = _mk_spawn()
    h = SC.native_hooks(s, my_side=ME, spawn_stat=lambda n: _STAT if n == "TEST TANK" else None)
    ctx = h["__ctx__"]
    h["SpawnCardInFrontline"](None, None, None, ["TEST TANK", 1, 7, None, False, None, None], None)
    _new = [u for u in s.units.values() if u.id < 0]
    chk("`SpawnCardInFrontline(卡名@0, side@1)` ⇒ 前线多一个单位、**用卡自己的面板**、`sick=True`、"
        "扇 `(\"spawn\", uid)`（进场钩子交调用方）",
        len(_new) == 1 and _new[0].row == "frontline" and _new[0].atk == 4 and _new[0].dfn == 5
        and _new[0].typ == "tank" and _new[0].sick is True and ctx.events == [("spawn", _new[0].id)],
        "new=%s events=%s" % ([(u.id, u.row, u.atk, u.dfn, u.typ, u.sick) for u in _new], ctx.events))

    s = _mk_spawn()
    h = SC.native_hooks(s, my_side=ME)                              # 没喂面板
    ctx = h["__ctx__"]
    h["SpawnCardInFrontline"](None, None, None, ["TEST TANK", 1, 7, None, False, None, None], None)
    chk("（第二个输入）**面板读不到** ⇒ 记缺口、**不生成**（不编数值）",
        not [u for u in s.units.values() if u.id < 0] and any("面板读不到" in g for g in ctx.gaps),
        str(ctx.gaps))

    s = _mk_spawn(units=4)                                          # 我方支援线满（容量 4）
    h = SC.native_hooks(s, my_side=ME, spawn_stat=lambda n: _STAT)
    ctx = h["__ctx__"]
    h["SpawnCardOnBattlefield"](None, None, None, [1, False, "TEST TANK", 7, None, False], None)
    chk("（第三个输入）`SpawnCardonBattlefield(side@0, _, 卡名@2)` 落**支援线**，但**排满** ⇒ 原版判定不过、"
        "**不生成**（合法 no-op，不记缺口）",
        not [u for u in s.units.values() if u.id < 0] and not ctx.gaps, "gaps=%s" % ctx.gaps)

    a, b, diff = SC.reconcile_one("SpawnCardInFrontline", ["TEST TANK", 1, 7, None, False, None, None],
                                  make_state=_mk_spawn, my_side=ME, ptr_ids={},
                                  spawn_stat=lambda n: _STAT if n == "TEST TANK" else None,
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`SpawnCardInFrontline` ⇒ 两条路都在前线生成一个同面板的单位（旧路走 `spawn_cards` + `spawn_stats`）",
        len(a.units) == len(b.units) == 1
        and [u.atk for u in a.units.values()] == [u.atk for u in b.units.values()] == [4] and not diff,
        "旧=%s 直跑=%s diff=%s" % ([(u.id, u.atk, u.dfn) for u in a.units.values()],
                                   [(u.id, u.atk, u.dfn) for u in b.units.values()], diff))

    # ㉝ 第二十四刀：转化（`ConvertCard`；载荷按**调用**喂）
    _CV = {"ids": [11], "name": "NEW CARD", "atk": 7, "dfn": 8, "cost": 3, "typ": "tank",
           "kw": ("guard", "smokescreen"), "opc": 2, "armor": 1, "skip_trigger": False}

    def _mk_conv(kw=("shock",), row="frontline"):
        s = mk()
        s.units[11] = S.U(11, ME, row, 2, 3, 1, "infantry", kw=kw)
        s.hand = {21: ST_H(21, "OLD", 2, "order")}
        s.deck = [31]
        s.deck_known = True
        s.deck_cards = {31: ST_H(31, "OLD", 2, "order")}
        s.card_templates = {31: ST_H(31, "OLD", 2, "order")}
        return s

    s = _mk_conv()
    h = SC.native_hooks(s, my_side=ME, payload={"convert": _CV})
    ctx = h["__ctx__"]
    h["ConvertCard"](None, None, None, [[11], 7, "NEW CARD", 0, False], None)
    _nu = [u for u in s.units.values() if u.id < 0]
    chk("`ConvertCard`（在场）⇒ 旧牌换成新牌：**同侧同行**、出厂数值（atk7/dfn8/opc2/armor1）、`sick=True`、"
        "关键词按载荷（guard 有、**smokescreen 被去掉**）、事件 `convert_old`/`convert_new`/`convert`",
        11 not in s.units and len(_nu) == 1 and _nu[0].atk == 7 and _nu[0].dfn == 8
        and _nu[0].opc == 2 and _nu[0].sick is True and "guard" in _nu[0].kw
        and "smokescreen" not in _nu[0].kw
        and [k for k, _ in ctx.events] == ["convert_old", "convert_new", "convert"],
        "new=%s events=%s" % ([(u.id, u.atk, u.dfn, u.opc, u.sick, sorted(u.kw)) for u in _nu], ctx.events))

    s = _mk_conv()
    h = SC.native_hooks(s, my_side=ME, payload={"convert": {"ids": [21], "name": "NEW CARD",
                                                             "atk": 4, "dfn": 4, "cost": 1, "typ": "order"}})
    ctx = h["__ctx__"]
    h["ConvertCard"](None, None, None, [[21], 7, "NEW CARD", 0, False], None)
    chk("（第二个输入）**手牌**里的转化 ⇒ 同位置换成新模板（出厂数值）+ 如实记缺口"
        "（新牌效果未知 / ExecuteOnSpawnedInHandEvents 未建模）",
        21 not in s.hand and len(s.hand) == 1 and list(s.hand.values())[0].atk == 4
        and any("手牌里的转化" in g for g in ctx.gaps),
        "hand=%s gaps=%s" % ({k: (v.atk, v.dfn) for k, v in s.hand.items()}, ctx.gaps[:2]))

    s = _mk_conv()
    h = SC.native_hooks(s, my_side=ME)                       # 没喂载荷
    ctx = h["__ctx__"]
    h["ConvertCard"](None, None, None, [[11], 7, "NEW CARD", 0, False], None)
    chk("（第三个输入）**没喂载荷** ⇒ 记缺口、**什么都不改**（不编数值）",
        11 in s.units and not ctx.applied and any("没拿到目标卡数值" in g for g in ctx.gaps),
        "applied=%s gaps=%s" % (ctx.applied, ctx.gaps[:1]))

    a, b, diff = SC.reconcile_one("ConvertCard", [[11], 7, "NEW CARD", 0, False],
                                  make_state=_mk_conv, my_side=ME, ptr_ids={},
                                  payload={"convert": _CV}, record_extra={"conv": _CV},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    _na, _nb = [u for u in a.units.values() if u.id < 0], [u for u in b.units.values() if u.id < 0]
    chk("对账：`ConvertCard`（在场）⇒ 两条路都换成新牌（同一套落点：同侧同行 + 出厂数值 + 去 smokescreen）",
        len(_na) == len(_nb) == 1 and _na[0].atk == _nb[0].atk == 7
        and sorted(_na[0].kw) == sorted(_nb[0].kw) and not diff,
        "旧=%s 直跑=%s diff=%s" % ([(u.id, u.atk, sorted(u.kw)) for u in _na],
                                   [(u.id, u.atk, sorted(u.kw)) for u in _nb], diff))

    # ㉞ 第二十五刀：真有消费者的限制族（真 sink）+ 明确未建模族（显式缺口）
    def _mk_restr():
        s = mk()
        s.restrictions = []
        return s

    s = _mk_restr()
    h = SC.native_hooks(s, my_side=ME)
    h["AddGameplayRestriction"](None, None, None, [1, 5, 11, 2], None)
    chk("`AddGameplayRestriction(side=1, type=5, …, turns=2)` ⇒ 影子列表多一条**与录制侧同形状**的条目 "
        "`{'side','type','turns'}`（对账才有意义）",
        s.restrictions == [{"side": ME, "type": 5, "turns": 2}], str(s.restrictions))
    h["RemoveGameplayRestriction"](None, None, None, [1, 5, 11, False, None], None)
    chk("（第二个输入）`RemoveGameplayRestriction(side=1, type=5)` ⇒ **按 side+type 移除**（`sim:809-811` 同口径）",
        s.restrictions == [], str(s.restrictions))

    s = _mk_restr()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["SetCardsSeenByCipher"](None, None, None, [3, 7, 1, None], None)
    chk("`SetCardsSeenByCipher(3, …, side=1)` ⇒ 记 `intel_seen` + 扇事件（消费者是 **rule 层**的 "
        "`_intel_triggers`，不在 engine 里跑触发）",
        ("intel_seen", 3, ME) in ctx.applied and ("intel_seen", 3) in ctx.events,
        "applied=%s events=%s" % (ctx.applied, ctx.events))
    s2 = _mk_restr()
    h2 = SC.native_hooks(s2, my_side=ME)
    ctx2 = h2["__ctx__"]
    h2["SetCardsSeenByCipher"](None, None, None, [0, 7, 1, None], None)
    chk("（第二个输入）`n = 0` ⇒ **什么都不记**（录制侧同口径：`n > 0` 才记）", not ctx2.applied,
        str(ctx2.applied))

    s = _mk_restr()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["CustomName1Add"](None, None, None, [11, "isAlsoTank"], None)
    chk("明确未建模族（例 `CustomName1Add`）⇒ **显式记缺口**、不改状态（比落到录制侧悄悄过去强 ✓）",
        ctx.gaps and not ctx.applied and any("自定义名 1" in g for g in ctx.gaps), str(ctx.gaps))

    a, b, diff = SC.reconcile_one("AddGameplayRestriction", [1, 5, 11, 2],
                                  make_state=_mk_restr, my_side=ME, ptr_ids={},
                                  to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`AddGameplayRestriction` ⇒ 两条路影子列表一致（快照现在也带 `restrictions`），`diff={}`",
        a.restrictions == b.restrictions == [{"side": ME, "type": 5, "turns": 2}] and not diff,
        "旧=%s 直跑=%s diff=%s" % (a.restrictions, b.restrictions, diff))

    # ㉟ 第二十六刀：抉择/预报的挂起标记（记事件 + 如实缺口；**绝不编分支**）
    s = _mk_restr()
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["selectCardToDraw"](None, None, None, [16, False, True, None], None)
    chk("`selectCardToDraw` ⇒ 记挂起事件 `choose_spawn_pending` + 如实缺口（候选要种子预测，不能编）",
        ("choose_spawn_pending", 16) in ctx.events and ctx.applied == [("choose_spawn_pending", 16)]
        and any("choose_spawn" in g for g in ctx.gaps), "events=%s gaps=%s" % (ctx.events, ctx.gaps))
    h["Forecast"](None, None, None, [16, None], None)
    chk("`Forecast` ⇒ `forecast_pending` 事件 + 缺口（9 条路径属 rule 的活种子预测）",
        ("forecast_pending", 16) in ctx.events and any("预报" in g for g in ctx.gaps),
        "events=%s gaps=%s" % (ctx.events, ctx.gaps))
    h["selectTargetFromHand"](None, None, None, [16, None], None)
    chk("`selectTargetFromHand` ⇒ `hand_target_pending` 事件 + 缺口（后果在之后的 `OnHandTargetSelected`）",
        ("hand_target_pending", 16) in ctx.events and any("hand_target" in g for g in ctx.gaps),
        "events=%s gaps=%s" % (ctx.events, ctx.gaps))
    chk("★ 三者都**不改状态**（不是状态变更 ⇒ 不能编一个分支/一张牌出来）",
        not ctx.state.units and not ctx.state.hand, "units=%s hand=%s" % (ctx.state.units, ctx.state.hand))

    # ㊱ 第二十七刀：绝对值指挥点 / 洗牌 / 往牌库塞牌 / 单卡收缴（收尾那 4 个）
    _FAKE_RNG = type("R", (), {"wrapper_int": lambda self, lo, hi: lo,
                               "shuffle": lambda self, xs: list(reversed(xs))})()

    s = mk(slots=3)
    h = SC.native_hooks(s, my_side=ME)
    ctx = h["__ctx__"]
    h["setKreditBySide"](None, None, None, [1, 7, None], None)
    chk("`setKreditBySide(我方, 7)` ⇒ 指挥点**直接写成 7**（我方有绝对值；`sim` 无该键消费者，录制侧是折算增量 ✓）",
        s.kredits == 7 and ("kredit_set", ME, 7) in ctx.applied, "kredits=%s" % s.kredits)
    s2 = mk(slots=3)
    h2 = SC.native_hooks(s2, my_side=ME)
    ctx2 = h2["__ctx__"]
    h2["setKreditBySide"](None, None, None, [2, 7, None], None)
    chk("（第二个输入）给**对方** ⇒ 记缺口、不改状态（对方绝对值我们不知道 ⇒ 不猜）",
        ctx2.gaps and s2.kredits == 2, "kredits=%s gaps=%s" % (s2.kredits, ctx2.gaps))

    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, rng=_FAKE_RNG)
    h["ShuffleDeckBySide"](None, None, None, [1, False, 7, None], None)
    chk("`ShuffleDeckBySide(我方)`（注入了流）⇒ 牌库按流洗过（假流=反转 ⇒ [92, 91]）",
        s.deck == [92, 91], "deck=%s" % s.deck)
    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME)                        # 没注入流
    ctx = h["__ctx__"]
    h["ShuffleDeckBySide"](None, None, None, [1, False, 7, None], None)
    chk("（第二个输入）**没注入流** ⇒ 记缺口 + 牌库标成未知（`deck=[]`、`deck_known=False`，同 `sim`）",
        s.deck == [] and s.deck_known is False and ctx.gaps, "deck=%s known=%s" % (s.deck, s.deck_known))

    s = _mk_deck()
    h = SC.native_hooks(s, my_side=ME, rng=_FAKE_RNG)
    ctx = h["__ctx__"]
    h["SpawnCardInDeckBySide"](None, None, None, [1, "NEW ORDER", 7, 2, 0, False, False, False, False,
                                                  False, False, False], None)
    _added = [cid for cid in s.deck if cid < -3000]
    chk("`SpawnCardInDeckBySide(我方, 2 张)` ⇒ 牌库多 **2 张**占位模板（id 负号流水）、"
        "并**如实记缺口**（面板/效果要读静态卡表 ⇒ 调用方的事）",
        len(_added) == 2 and all(s.deck_cards.get(c) is not None for c in _added)
        and any("效果" in g for g in ctx.gaps),
        "added=%s deck=%s gaps=%s" % (_added, s.deck[:4], ctx.gaps[:1]))

    s = _mk_turn()
    h = SC.native_hooks(s, my_side=ME)
    h["SalvageUnit"](None, None, None, [[12], 7, None], None)
    _copies = list(s.hand.values())
    chk("`SalvageUnit([12])`（单卡收缴）⇒ 与 `SalvageMultipleUnits` **同一套**：手牌多一张 1/1 复制",
        len(_copies) == 1 and _copies[0].atk == 1 and _copies[0].dfn == 1,
        "hand=%s" % {k: (v.cost, v.atk, v.dfn) for k, v in s.hand.items()})

    a, b, diff = SC.reconcile_one("SalvageUnit", [[12], 7, None], make_state=_mk_turn, my_side=ME,
                                  ptr_ids={}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("对账：`SalvageUnit` ⇒ 两条路手牌里那张 1/1 复制一致（旧路 `salvage_ids` → `apply_salvage`）",
        len(a.hand) == len(b.hand) == 1
        and list(a.hand.values())[0].atk == list(b.hand.values())[0].atk == 1 and not diff,
        "旧=%s 直跑=%s diff=%s" % ({k: (v.cost, v.atk) for k, v in a.hand.items()},
                                   {k: (v.cost, v.atk) for k, v in b.hand.items()}, diff))

    # ㊲ 回归（第二十七刀抓到第三次的 falsy-默认坑）：**空牌库**时"偷进牌库"必须真写进去
    s = _mk_deck(hand_ids=(), deck=())
    s.units[12] = S.U(12, OPP, "frontline", 2, 3, 1, "infantry")
    h = SC.native_hooks(s, my_side=ME)
    h["StealCardFromBoardToDeck"](None, None, None, [12, 7, 1, None], None)
    chk("★ 回归：**牌库为空**时 `StealCardFromBoardToDeck` 仍要把卡真写进牌库 "
        "（`(… or []).append` 那种写法会因为空列表 falsy 而**丢** ✗）",
        s.deck == [12], "deck=%s" % s.deck)

    # ㊳ 静态守卫：禁止**写**状态字典/列表的 falsy-默认写法（本文件里犯过三次 ⇒ 收成 ctx.dict_of/list_of）
    import re as _re
    _bad = []
    # P6：scripts.py 已拆成 scripts / scripts_ctx / scripts_sinks_core / scripts_sinks_more ⇒ 四个文件都扫
    for _fn in ("scripts.py", "scripts_ctx.py", "scripts_sinks_core.py", "scripts_sinks_more.py"):
      for _i, _ln in enumerate(open(os.path.join(ROOT, "engine", _fn), encoding="utf-8")
                               .read().splitlines(), 1):
        if _ln.strip().startswith("#"):
            continue
        if _re.search(r"or\s*\{\}\)\[|or\s*\{\}\)\.setdefault|or\s*\[\]\)\.(append|extend|insert)", _ln):
            _bad.append((_fn, _i, _ln.strip()[:70]))
    chk("★ 静态守卫：`engine/scripts.py` 里不许再出现 `(… or {{}})[k] =` / `(… or []).append` 这类"
        "**写**状态字典/列表的写法（空容器 falsy ⇒ 写进临时对象就丢 ✗）—— 本文件里犯过三次",
        not _bad, str(_bad))

    # ㊴ 动作级对账（P5 正题的安全网）：一串动词、A（旧路重放）vs B（直跑）、能定位第几步分叉
    _sa, _sb, _d, _per = SC.reconcile_run(
        [("GiveKreditsBySide", [1, 2, None], None), ("GiveKreditsBySide", [1, 3, None], None)],
        make_state=mk, my_side=ME, ptr_ids={}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("`reconcile_run`：两条资源动词连做 ⇒ 两条路指挥点都 2→4→**7**、`diff={}`（**增量按当时的值折算** —— "
        "第 2 步若拿初始值算就会错 ✗，这正是它必须逐条现读的原因）",
        _sa.kredits == _sb.kredits == 7 and not _d and all(not x for x in _per),
        "旧=%s 直跑=%s diff=%s per=%s" % (_sa.kredits, _sb.kredits, _d, _per))

    _sa, _sb, _d, _per = SC.reconcile_run(
        [("AddAttackUntilEndOfTurn", [11, 7, 2], 11), ("AddAttackUntilEndOfTurn", [11, 7, 3], 11)],
        make_state=_mk_turn, my_side=ME, ptr_ids={}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("`reconcile_run`：同一条数值动词连做两次 ⇒ 两条路 `atk`/`atk_turn` 都累计到 8/5、`diff={}`",
        _sa.units[11].atk == _sb.units[11].atk == 8
        and _sa.units[11].atk_turn == _sb.units[11].atk_turn == 5 and not _d,
        "旧 atk/atk_turn=%s/%s 直跑=%s/%s diff=%s" % (_sa.units[11].atk, _sa.units[11].atk_turn,
                                                      _sb.units[11].atk, _sb.units[11].atk_turn, _d))

    # ★ 非空转 + 定位：第 1 步两条路一致、第 2 步才分叉 ⇒ 必须**只**在第 2 步报出来
    _sa, _sb, _d, _per = SC.reconcile_run(
        [("GiveKreditsBySide", [1, 1, None], None),
         ("StealCardFromBoardToDeck", [12, 7, 1, None], 12)],
        make_state=_mk_deck, my_side=ME, ptr_ids={}, to_effects=_to_effects, apply_eff=_apply_eff)
    chk("★ `reconcile_run` **非空转 + 定位**：第 1 步两条路一致（`per_step[0]=={}`）、第 2 步才分叉"
        "（`per_step[1]` 里有 `deck` —— 旧路多带了 `deck_shuffle` 副作用）⇒ 它真能看见差异、且指出是第几步",
        not _per[0] and "deck" in _per[1] and "deck" in _d,
        "per[0]=%s per[1].keys=%s" % (_per[0], sorted(_per[1])))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
