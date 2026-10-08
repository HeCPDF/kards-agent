#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""死亡链扇出（直跑 `DirectCtx.on_death` <-> 重放 `apply_calls(on_death=...)` <-> 字典路 `_apply_death`）。

原版次序（`BP_CardFunctions.cpp::ApplyRemoveCardFromBoard`@18002）：`ExecuteOnBeforeLeaveBoardOrOwnerEvents`(:18028)
-> 离场 -> `ExecuteOnAfterLeaveBoardOrOwnerEvents`(:18053) -> destroyed 则 `ExecuteOnCardDestroyedFunction`(:18060)。
即死亡后果在**单位已离场之后**跑 => 回调看到的是「已不在 units」的状态；且 `applied` 里的 destroy/died 记录不被改写
（重放据此复现同一次扇出）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import calls as CALLS                                    # noqa: E402
from engine import scripts as SC                                     # noqa: E402
from engine import state as S                                        # noqa: E402

ME, OPP = 1, 2
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(*units):
    s = S.Sim({}, {ME: 20, OPP: 20}, 5, {}, my_side=ME, slots=3, kredit_max=24)
    for u in units:
        s.units[u.id] = u
    s.playing_side = ME
    return s


def unit(uid, side=OPP, atk=3, dfn=2):
    return S.U(uid, side, "frontline", atk, dfn, 1, "infantry")


def fanout(log):
    def _f(st, uid):
        log.append((uid, uid in st.units))        # 记『回调时它还在不在场』
        st.hq[ME] = st.hq.get(ME, 0) - 2          # 一个可见的状态后果（像 death_fx={'heal_hq': -2}）
    return _f


def diff(a, b):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}


def main():
    ptr_ids = {0xA1: 11, 0xA2: 12}

    # ---- DestroyCard：移场 -> 记 destroy -> 扇出（单位已离场），仅一次 ----
    log = []
    s = mk(unit(11), unit(12))
    h = SC.native_hooks(s, ptr_ids=ptr_ids, my_side=ME, on_death=fanout(log))
    h["DestroyCard"](None, None, None, [0xA1, 0], None)
    chk("DestroyCard 扇出一次、回调时已离场", log == [(11, False)], str(log))
    chk("扇出的状态后果落到 state", s.hq[ME] == 18, str(s.hq))
    ctx = h["__ctx__"]
    chk("applied 记录不被改写（仍有 destroy）", any(c[0] == "destroy" and c[1] == 11 for c in ctx.applied), str(ctx.applied))

    # ---- 不注入 => 行为与旧口径一致（不扇）----
    s0 = mk(unit(11))
    h0 = SC.native_hooks(s0, ptr_ids=ptr_ids, my_side=ME)
    h0["DestroyCard"](None, None, None, [0xA1, 0], None)
    chk("没注入 on_death => 不扇出（hq 不变）", s0.hq[ME] == 20 and 11 not in s0.units, str(s0.hq))

    # ---- 三角：直跑（扇出）== 重放（扇出）----
    log_c = []
    s2 = mk(unit(11), unit(12))
    res = CALLS.apply_calls(s2, list(ctx.applied), on_death=fanout(log_c))
    d = diff(SC._state_snapshot(s), SC._state_snapshot(s2))
    chk("重放(destroy_card+destroy)只扇一次、与直跑同状态", log_c == [(11, False)] and not d and not res.gaps,
        "%s %s gaps=%s" % (log_c, d, res.gaps))
    s3 = mk(unit(11))
    res = CALLS.apply_calls(s3, list(ctx.applied))
    chk("重放不注入 on_death => 不扇（旧行为）", not res.gaps and s3.hq[ME] == 20, str(res.gaps))

    # ---- 伤害致死：deal_damage 自己 pop -> died -> 扇出；重放同口径 ----
    for verb, args, kind in (("DamageCard", [0xA1, 5, 0], "damage_card"),
                             ("DamageMultipleCards", [[0xA1, 0xA2], 5], "damage_aoe")):
        log = []
        s = mk(unit(11), unit(12))
        h = SC.native_hooks(s, ptr_ids=ptr_ids, my_side=ME, on_death=fanout(log))
        h[verb](None, None, None, list(args), None)
        applied = list(h["__ctx__"].applied)
        want = [(11, False)] if verb == "DamageCard" else [(11, False), (12, False)]
        chk("%s 致死扇出（已离场）" % verb, log == want, str(log))
        chk("%s 的 applied 仍含 died + %s" % (verb, kind),
            any(c[0] == "died" for c in applied) and any(c[0] == kind for c in applied), str(applied))
        log_c = []
        s2 = mk(unit(11), unit(12))
        res = CALLS.apply_calls(s2, applied, on_death=fanout(log_c))
        d = diff(SC._state_snapshot(s), SC._state_snapshot(s2))
        chk("%s 重放扇出同次数、同状态" % verb, log_c == want and not d and not res.gaps,
            "%s %s %s" % (log_c, res.gaps, d))

    # ---- 没死的伤害不扇 ----
    log = []
    s = mk(unit(11, dfn=9))
    h = SC.native_hooks(s, ptr_ids=ptr_ids, my_side=ME, on_death=fanout(log))
    h["DamageCard"](None, None, None, [0xA1, 2, 0], None)
    chk("没死 => 不扇", log == [] and 11 in s.units, str(log))

    # ---- leave_board（RemoveMultipleCardsFromBoard）不算被摧毁 => 不扇 ----
    log = []
    s = mk(unit(11))
    h = SC.native_hooks(s, ptr_ids=ptr_ids, my_side=ME, on_death=fanout(log))
    h["RemoveMultipleCardsFromBoard"](None, None, None, [[0xA1]], None)
    chk("离场但不算摧毁 => 不扇", log == [] and 11 not in s.units, str(log))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
