#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A5-③：`ExecuteOnOtherCardsAbilitiesChanged`（0x1D `OnOtherCardAbilitiesChanged`，BP_CardFunctions.cpp:20885）。

假 VM 下验证（真 VM 与实机未验）：
  * runner：fetch 0x1D 逐张调用，**不排除被改的牌自己**，被压制的丢掉，形参 `cardChanging`；
  * 改变后的关键词视图（`view_overrides`）只在给了才传给 VM；`apply_view_override` 的加/删/摘能力表；
  * `MakeVeteran` 尾段：0x20 之后接 0x1D（`run_veteran`）；
  * 调用点清单 `ABILITIES_CALLERS` 与 BP 导出里真正调用它的函数一致（导出在才查）；
  * sim：只有原版会广播的 7 个关键词、且「原先没有才算赋予 / 原先有才算移除」时才消费 `event_fx["abilities"]`。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _hookfake import ME, OPP, Checker, card, names, run          # noqa: E402
from policy.search import wire_sim as _wire_sim                    # noqa: E402
_wire_sim()                        # ★ 门面已删（P5）：wiring 的唯一接缝（原来靠 import 门面的副作用）
import semantics.effectvm as EV                                    # noqa: E402
import semantics.triggers as TR                                    # noqa: E402
from sim import state as SS                                        # noqa: E402
from sim.engine import _apply_eff                                  # noqa: E402
from sim.state import Sim, U                                       # noqa: E402

chk = Checker()
ROOT = os.path.dirname(HERE)
BP = os.path.join(os.path.dirname(ROOT), "reverse-data", "exports-1.58.27125.Steam", "kards", "Content",
                  "Blueprints", "Cards", "BP_CardFunctions.cpp")


def ovr(d):
    return {1000 + k: set(v) for k, v in d.items()}


def main():
    x = card(80, "X", "frontline", ME)
    l1 = card(81, "L1", "back", OPP)
    sup = card(82, "SUP", "back", OPP, is_suppressed=True)
    hl = card(83, "HANDL", "hand", ME)
    cards = [x, l1, sup, hl]
    H1D = {"OnOtherCardAbilitiesChanged"}
    O = ovr({80: H1D, 81: H1D, 82: H1D, 83: H1D})

    # ---- runner ----
    r, calls = run(TR.run_abilities_changed, cards, O, x)
    chk("0x1D：逐张调用，不排除被改的牌自己（X 自己覆写了也调），手牌里的也调，被压制的被 fetch 丢掉",
        names(calls) == [("OnOtherCardAbilitiesChanged", "X"), ("OnOtherCardAbilitiesChanged", "L1"),
                         ("OnOtherCardAbilitiesChanged", "HANDL")], str(names(calls)))
    chk("0x1D：形参 cardChanging=被改的牌；meta 张数/视图", all(a == {"cardChanging": 80} for _h, _n, a, _k in calls)
        and r["meta"] == {"n_0x1d": 3, "view": "pre_change"}, str(r["meta"]))
    chk("没给 view_overrides ⇒ 不往 VM 传这个参数（读快照里的改变前状态，meta 如实标 pre_change）",
        all("view_overrides" not in k for _h, _n, _a, k in calls))
    vo = TR.abilities_view_override(x, "guard", True)
    r2, calls2 = run(TR.run_abilities_changed, cards, O, x, view_overrides=vo)
    chk("给了 view_overrides ⇒ 原样传给每次 VM 调用，meta.view=post_change",
        all(k.get("view_overrides") == vo for _h, _n, _a, k in calls2) and r2["meta"]["view"] == "post_change", str(vo))
    chk("abilities_view_override：赋予 ⇒ 加旗标位；移除 ⇒ 去旗标位 + 摘能力表",
        vo == {80: {"keywords_add": ["has_guard"]}}
        and TR.abilities_view_override(x, "fury", False) == {80: {"keywords_remove": ["has_fury", "fury"],
                                                                  "received_remove": ["fury"]}})

    # ---- apply_view_override ----
    base = {"keywords": ["has_blitz"], "received_abilities": [{"ability": "Guard", "givers": [3]},
                                                              {"ability": "fury", "givers": [4]}], "attack": 2}
    v = EV.apply_view_override(base, {"keywords_add": ["has_guard"], "keywords_remove": ["has_blitz", "blitz"],
                                      "received_remove": ["guard"], "attack": 9})
    chk("apply_view_override：加旗标位 / 删旗标位 / 摘能力表（大小写不敏感）/ 其它键直接覆盖，且不改原 dict",
        v["keywords"] == ["has_guard"] and [a["ability"] for a in v["received_abilities"]] == ["fury"] and v["attack"] == 9
        and base["keywords"] == ["has_blitz"] and len(base["received_abilities"]) == 2, str(v))
    from kardsmem import cardnatives as CN
    cn = CN.CardNatives(None, view=lambda p: v, ks=None)
    chk("改变后的视图被 getHas* 原生读到（赋予的 guard 生效；移除的 blitz 失效）",
        cn.call("getHasGuard", 1) is True and cn.call("getHasBlitz", 1) is False)

    # ---- MakeVeteran 尾段：0x20 之后接 0x1D ----
    vet = card(90, "VET", "frontline", ME)
    wv = card(91, "WV", "back", OPP)
    OV = ovr({90: {"OnBecomingVeteran", "OnOtherCardAbilitiesChanged"}, 91: {"OnOtherCardBecomingVeteran", "OnOtherCardAbilitiesChanged"}})
    rv, cv = run(TR.run_veteran, [vet, wv], OV, vet)
    chk("MakeVeteran：自己 OnBecomingVeteran → 0x20 → 末尾 0x1D（BP_CardFunctions.cpp:7280）",
        names(cv) == [("OnBecomingVeteran", "VET"), ("OnOtherCardBecomingVeteran", "WV"),
                      ("OnOtherCardAbilitiesChanged", "VET"), ("OnOtherCardAbilitiesChanged", "WV")]
        and rv["meta"]["n_0x1d"] == 2 and rv["meta"]["abilities_view"] == "pre_change", str(names(cv)))
    vv = {90: {"keywords_add": ["has_guard"]}}
    _rv, cv2 = run(TR.run_veteran, [vet, wv], OV, vet, view_overrides=vv)
    chk("MakeVeteran：给了升级后视图 ⇒ 整条链（含自己的 OnBecomingVeteran）都带上", all(k.get("view_overrides") == vv for *_x, k in cv2),
        str([k for *_x, k in cv2][:1]))

    # ---- 调用点清单与 BP 导出一致 ----
    if os.path.exists(BP):
        fn, found = None, set()
        for ln in open(BP, encoding="utf-8", errors="replace"):
            m = re.match(r"\s{4}public .*?\b(\w+)\(", ln)
            if m:
                fn = m.group(1)
            if "ExecuteOnOtherCardsAbilitiesChanged(" in ln and "public" not in ln and fn:
                found.add(fn)
        chk("ABILITIES_CALLERS = BP 导出里真正调用 ExecuteOnOtherCardsAbilitiesChanged 的函数（%d 个）" % len(found),
            found == set(TR.ABILITIES_CALLERS), "多=%s 缺=%s" % (sorted(set(TR.ABILITIES_CALLERS) - found), sorted(found - set(TR.ABILITIES_CALLERS))))
    else:
        print("  [SKIP] BP 导出不在本机，跳过调用点核对")
    chk("sim 与 triggers 的关键词清单同源（ABILITIES_KW == sim.state.ABILITIES_KEYWORDS）",
        tuple(TR.ABILITIES_KW) == tuple(SS.ABILITIES_KEYWORDS))
    chk("原版不广播的关键词（immune/alpine/salvage）不在清单里", not ({"immune", "alpine", "salvage"} & set(SS.ABILITIES_KEYWORDS)))

    # ---- sim 消费 ----
    def mk(units, tbl):
        return Sim({u.id: u for u in units}, {ME: 20, OPP: 20}, 5.0, {}, event_fx={"abilities": tbl}, my_side=ME)

    tbl = {(1, "+guard"): {"damage_own_hq": 1}, (1, "-blitz"): {"damage_own_hq": 2}, (1, "-immune"): {"damage_own_hq": 4},
           (1, "+alpine"): {"damage_own_hq": 8}, ("gap",): ["abilities：测试缺口"]}
    s = mk([U(1, ME, "frontline", 3, 3, 3, "infantry", {"blitz", "immune"})], tbl)
    _apply_eff(s, {"give": ["guard"]}, 1)
    chk("sim：赋予原先没有的 guard ⇒ 先改关键词再广播 0x1D（己方总部 -1），缺口并入",
        "guard" in s.units[1].kw and s.hq[ME] == 19 and "abilities：测试缺口" in s.gaps, str((s.hq, s.gaps)))
    _apply_eff(s, {"give": ["guard"]}, 1)
    chk("sim：已经有了 ⇒ 原版不广播（总部不再掉血）", s.hq[ME] == 19)
    _apply_eff(s, {"remove_blitz": True}, 1)
    chk("sim：移除原先有的 blitz ⇒ 广播（-2）", "blitz" not in s.units[1].kw and s.hq[ME] == 17, str(s.hq))
    _apply_eff(s, {"remove_blitz": True}, 1)
    chk("sim：移除原先就没有的 ⇒ 不广播", s.hq[ME] == 17)
    _apply_eff(s, {"remove_immune": True}, 1)
    chk("sim：immune 的移除不在原版的广播清单里 ⇒ 即使表里有键也不消费", "immune" not in s.units[1].kw and s.hq[ME] == 17)
    _apply_eff(s, {"give": ["alpine"]}, 1)
    chk("sim：alpine 的赋予同理不广播", s.hq[ME] == 17)
    _apply_eff(s, {"give": ["fury"]}, 1)
    chk("sim：清单里的关键词但表里没条目（没有牌因它做出反应）⇒ 什么都不发生", s.hq[ME] == 17 and "fury" in s.units[1].kw)
    _apply_eff(s, {"armor": 1}, 1)
    chk("sim：ChangeHeavyArmor 的 0x1D 没喂改变后视图 ⇒ 表非空时如实记缺口、不广播",
        s.units[1].armor == 1 and any("ChangeHeavyArmor" in g for g in s.gaps) and s.hq[ME] == 17, str(s.gaps))
    s = mk([U(1, ME, "frontline", 3, 3, 3, "infantry")], {})
    _apply_eff(s, {"give": ["guard"], "armor": 1}, 1)
    chk("sim：表为空（场上没有牌覆写 0x1D）⇒ 零开销、不记缺口", not s.gaps and s.hq[ME] == 20)
    print("失败 %d 项" % chk.fails)
    return chk.fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
