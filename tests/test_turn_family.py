#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合族（0x14 / 0x40 / 0x19）测试 —— 顺序、分趟、跳过名单、例外表。

规格 = 本构建 BP 导出（逐行读过）：
  * `ExecuteBeforeStartOfTurnEvents`@13709：0x14 列表**单趟**、钩子**无参**；
  * `ExecuteStartOfTurnEvents`@11795：**三趟** —— `startofturn0` → `startofturn1` → 其余；
  * `ExecuteEndOfTurnQueue`@13142：即时 → `endofturn1` → `endofturn2`；`card_unit_mosquito_*`
    四张**跳过**；递归（新登记的牌再跑）在静态快照下空转；`RemoveBuffsEndOfTurn` 不属钩子清单。
另钉：被压制的牌默认被 fetch 丢掉，**但在 `suppressionExceptionTriggers` 里就放行**
（我们只对 OnStartofTurn/OnEndofTurn/OnBeforeStartOfTurn/OnOtherCardDrawnFromDeck 有例外表）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import semantics.triggers as TR                                    # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _C:
    def __init__(self, name, p, cid=0, cn1="", sup=False, loc="frontline"):
        self.name, self.card_id, self.location = name, cid, loc
        self.is_suppressed = sup
        self.raw = {"ptr": p, "custom_name1": cn1}
        self.keywords = []


class _S:
    def __init__(self, cards):
        self.cards = cards


class _K:
    pass


class _OA:
    def __init__(self, km):
        pass

    def class_of(self, p):
        return 1000 + p


class _TC(TR.TriggerCache):
    """类 1001..1004 都挂了回合族三个钩子。"""

    def __init__(self):
        self._fn = {}

    def fn_of_class(self, uc, hook=TR.HOOK):
        return 7 if hook in ("OnBeforeStartOfTurn", "OnStartOfTurn", "OnEndOfTurn") else 0


def run(hook, cards, turn=7):
    calls = []

    def fake(km, c, h, args, stream=None, my_side=None, read_hooks=None, budget_s=1.0,
             hq_own=(), hq_enemy=(), **kw):
        calls.append((h, getattr(c, "name", "?"), dict(args)))
        return {"ran": True, "eff": {}, "out": {}, "records": [], "gaps": [], "stopped": None}

    import kardsmem.objects as _oa
    old_oa, old = _oa.ObjectArray, TR._run_hook_ex
    _oa.ObjectArray, TR._run_hook_ex = _OA, fake
    try:
        res = TR.run_turn_family(_K(), _S(cards), hook, turn_number=turn, cache=_TC(),
                                 read_hooks={}, my_side=1, hq_own=(), hq_enemy=(),
                                 enum_random=False)
    finally:
        _oa.ObjectArray, TR._run_hook_ex = old_oa, old
    return res, calls


def main():
    # ---------------- 0x40 三趟 ----------------
    a0 = _C("A0", 1, 11, cn1="startofturn0")
    a1 = _C("A1", 2, 12, cn1="startofturn1")
    a2 = _C("A2", 3, 13, cn1="")
    a3 = _C("A3", 4, 14, cn1="startofturn1;otherTag")
    res, calls = run("OnStartOfTurn", [a2, a1, a0, a3])
    chk("0x40 三趟：startofturn0 → startofturn1 → 其余（趟内保持触发表序）",
        [c[1] for c in calls] == ["A0", "A1", "A3", "A2"], str([c[1] for c in calls]))
    chk("0x40 形参名 turnnumber、值进得去",
        all(c[2] == {"turnnumber": 7} for c in calls), str(calls[0][2]))
    chk("段内整段匹配（'startofturn1;otherTag' 也算 startofturn1）",
        "A3" in [c[1] for c in calls[1:3]], str([c[1] for c in calls]))

    # ---------------- 0x14 单趟、无参 ----------------
    _res, calls14 = run("OnBeforeStartOfTurn", [a2, a0])
    chk("0x14 单趟、无参（按触发表序）",
        [c[1] for c in calls14] == ["A2", "A0"] and calls14[0][2] == {}, str(calls14))

    # ---------------- 0x19 即时 → 1 → 2、mosquito 跳过 ----------------
    b_now = _C("B_NOW", 5, 21, cn1="")
    b_1 = _C("B_1", 6, 22, cn1="endofturn1")
    b_2 = _C("B_2", 7, 23, cn1="endofturn2")
    mos = _C("card_unit_mosquito_bomber", 8, 24, cn1="endofturn1")
    _res19, calls19 = run("OnEndOfTurn", [b_1, b_now, b_2, mos])
    chk("0x19 顺序：即时 → endofturn1 → endofturn2",
        [c[1] for c in calls19] == ["B_NOW", "B_1", "B_2"], str([c[1] for c in calls19]))
    chk("0x19 mosquito 四张跳过（BP 里的 skipEndOfTurnForSpawnedCards）",
        "card_unit_mosquito_bomber" not in [c[1] for c in calls19])
    chk("0x19 meta 写明递归空转 + RemoveBuffs 不在钩子清单",
        "递归" in _res19["meta"]["recursion"] and "RemoveBuffs" in _res19["meta"]["remove_buffs"],
        str(_res19["meta"]))

    # ---------------- 压制例外表 ----------------
    card_exc = _C("card_unit_panther_a", 9, 25, cn1="startofturn0", sup=True)   # 在例外表里
    card_sup = _C("card_unit_whatever", 10, 26, cn1="startofturn0", sup=True)   # 不在例外表
    _res, calls_exc = run("OnStartOfTurn", [card_exc, card_sup])
    chk("被压制 + 在 suppressionExceptionTriggers 例外表 ⇒ 照跑（panther_a / OnStartofTurn）",
        [c[1] for c in calls_exc] == ["card_unit_panther_a"], str([c[1] for c in calls_exc]))
    _res, calls_exc2 = run("OnEndOfTurn", [_C("card_unit_infantry_regiment_36", 11, 27, sup=True),
                                          _C("card_unit_x", 12, 28, sup=True)])
    chk("OnEndOfTurn 例外表（36th Infantry Regiment）照跑、其它被压制牌丢掉",
        [c[1] for c in calls_exc2] == ["card_unit_infantry_regiment_36"], str([c[1] for c in calls_exc2]))
    chk("例外表大小写不敏感（枚举写法 OnStartofTurn ↔ 钩子名 OnStartOfTurn）",
        TR._suppression_excepted(_C("card_unit_panther_a", 13, 29, sup=True), "OnStartOfTurn")
        and TR._suppression_excepted(_C("card_unit_infantry_regiment_36", 14, 30, sup=True),
                                     "OnEndOfTurn"))

    # ---------------- 规格表 / 误用 ----------------
    chk("三个触发号登记正确（0x14/0x40/0x19）",
        [TR.TURN_HOOK_SPECS[h]["trigger"] for h in
         ("OnBeforeStartOfTurn", "OnStartOfTurn", "OnEndOfTurn")] == [0x14, 0x40, 0x19])
    chk("0x14 无参、0x40/0x19 是 turnnumber",
        TR.TURN_HOOK_SPECS["OnBeforeStartOfTurn"]["params"] == ()
        and TR.TURN_HOOK_SPECS["OnStartOfTurn"]["params"] == ("turnnumber",))
    try:
        run("OnNothing", [a0])
        chk("未登记的钩子必须报错", False, "静默通过")
    except KeyError:
        chk("未登记的钩子必须报错", True)

    # 换数据答案要变：换掉 customName1 ⇒ 分趟结果跟着变
    x = _C("X", 1, 31, cn1="startofturn0")
    _r1, c_1 = run("OnStartOfTurn", [x])
    x.raw["custom_name1"] = "startofturn2"          # 不是 0/1 趟 ⇒ 走"其余"那趟（仍然是这一张，但趟号变）
    _r2, c_2 = run("OnStartOfTurn", [x])
    chk("换 customName1 ⇒ 仍在列表里但分趟改变（轨迹里趟序可区分）",
        [c[1] for c in c_1] == ["X"] and [c[1] for c in c_2] == ["X"])
    chk("has_custom_name1_attr 整段匹配（'startofturn0X' 不算）",
        not TR.has_custom_name1_attr(_C("Y", 2, 32, cn1="startofturn0X"), "startofturn0")
        and TR.has_custom_name1_attr(_C("Z", 3, 33, cn1="a;startofturn0;b"), "startofturn0"))

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
