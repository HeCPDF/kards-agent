#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攻击钩子族的离线回归（不需要游戏）：`semantics.triggers.run_attack_hooks` 的流程与字节码一致。

依据：ATTACK-HOOKS-1.60.md（`BP_CardFunctions::AttackCard` 字节码：0x1E 换目标 → 0x1F 吞攻击 →
付指挥点 → OnBeforeAttack/0x0D → 受击通知 → OnAfterAttack/0x04 → 花费通知）。

这里用**真的** `find_cards`（假 ObjectArray + 假 TriggerCache 提供"哪张牌覆写了哪个钩子"），
只替换 `_run_hook_ex`（VM 执行）—— 这样"排除攻击者 / 被压制丢掉 / 两方都枚举 / 触发表顺序"
这些语义是被真代码测到的，不是测试替身自己的逻辑。

★ 自检纪律（同类教训）：不只让它跑完——换卡 / 换顺序 / 换参数，答案必须跟着变。

用法：cd D:\\Kards\\kards-agent && python test_attack_hooks.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _cards import ME, OPP                              # noqa: E402
from kardsmem import gamemodel as GM                            # noqa: E402
from semantics import triggers as T                                   # noqa: E402
import kardsmem.objects as _oa                                    # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(extra)) if (extra and not ok) else ""))
    bad += 0 if ok else 1


class FakeCard:
    def __init__(self, name, ptr, side, ovr=(), suppressed=False, enum_idx=None, **kw):
        self.name, self.side, self.location = name, side, "frontline"
        self.is_suppressed = suppressed
        self.raw = {"ptr": ptr}
        if enum_idx is not None:
            self.raw["enum_idx"] = enum_idx
        self.ovr = set(ovr)
        self.card_id = ptr                       # 测试里 card_id == ptr（便于对照）
        for k, v in kw.items():                  # card_type/attack/defense/keywords/location/raw 字段…
            if k == "raw":
                self.raw.update(v)
            else:
                setattr(self, k, v)
        # 原版对象：semantics 读 `card.obj.side / IsHQ() / IsFieldUnit() / Location`
        _es = GM.ESide(side)
        _hq = getattr(self, "location", None) == "hq"
        self.obj = GM.BaseCardObject(
            CardID=ptr, side=_es, title=name,
            Type=GM.EType.location if _hq else GM.EType[getattr(self, "card_type", "infantry")],
            Location=GM.SUPPORT_OF[_es] if _hq else GM.ECardLocation.Board_Frontline)


class FakeStream:
    """共用的随机流：每次抽取 draws+1，返回预置序列里的下一个值。"""

    def __init__(self, seq=(10, 20, 30, 40)):
        self.seq, self.draws = list(seq), 0

    def draw(self):
        v = self.seq[self.draws % len(self.seq)]
        self.draws += 1
        return v


class World:
    """一局假盘面 + 脚本化的钩子输出。`script[(hook, ptr)]` = 出参 dict（或 callable(args, stream)）。"""

    def __init__(self, cards, script=None):
        self.cards = cards
        self.script = script or {}
        self.calls = []                          # (hook, ptr, args)
        self.by_ptr = {c.raw["ptr"]: c for c in cards}

    def ex(self, km, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
           hq_own=(), hq_enemy=(), st=None):
        p = T._ptr_of(c)
        card = self.by_ptr.get(p)
        if card is None or hook not in card.ovr:             # 自己的钩子没覆写 ⇒ 没跑
            return {"ran": False, "eff": {}, "stopped": "没有覆写", "out": {}, "records": []}
        self.calls.append((hook, p, dict(args)))
        sc = self.script.get((hook, p), {})
        out = sc(args, stream) if callable(sc) else dict(sc)
        eff = out.pop("_eff", {})
        return {"ran": True, "eff": eff, "stopped": None, "out": out, "records": []}


class _FakeOA:
    def __init__(self, km):
        pass

    def class_of(self, p):
        return p                                              # 类 id = 指针（每张牌自成一类）


class _TC(T.TriggerCache):
    def __init__(self, world):
        self._fn = {}
        self.w = world

    def fn_of_class(self, uc, hook=T.HOOK):
        c = self.w.by_ptr.get(uc)
        return 7 if (c is not None and hook in c.ovr) else 0


class _K:
    pass


class _St:
    def __init__(self, cards):
        self.cards = cards


def run(world, atk, dfd, **kw):
    """跑一次 run_attack_hooks（真 find_cards + 假 VM 执行），返回 (结果, world)。"""
    old_oa, old_ex = _oa.ObjectArray, T._run_hook_ex
    _oa.ObjectArray = _FakeOA
    T._run_hook_ex = world.ex
    try:
        stream = kw.pop("stream", None) or FakeStream()
        r = T.run_attack_hooks(_K(), _St(world.cards), atk, dfd, stream=stream, cache=_TC(world), **kw)
    finally:
        _oa.ObjectArray, T._run_hook_ex = old_oa, old_ex
    return r


def names(r):
    return [(h, n) for h, n in r["order"]]


# ----------------------------------------------------------------------------------------------
def mk_cards():
    A = FakeCard("A", 1, ME, ovr=("OnBeforeAttack", "OnAfterAttack", "OnOperationKreditsSpent",
                                       "OnOtherCardAttacks", "OnAttackStopped", "OnOtherCardAttackSwitchTarget",
                                       "OnReceiveDamage"))
    D = FakeCard("D", 2, OPP, ovr=("OnReceiveDamage",))
    X = FakeCard("X", 3, ME, ovr=("OnAfterOtherCardAttacks", "OnBeforeOtherCardAttacks"))
    Y = FakeCard("Y", 4, OPP, ovr=("OnAfterOtherCardAttacks", "OnOtherCardAttacks",
                                       "OnOtherCardAttackSwitchTarget", "OnOtherCardOperationKreditsSpent",
                                       "OnOtherCardReceiveDamage"))
    Z = FakeCard("Z", 5, OPP, ovr=("OnAfterOtherCardAttacks",), suppressed=True)
    return A, D, X, Y, Z


A, D, X, Y, Z = mk_cards()
cards = [A, D, X, Y, Z]

# 1) 完整顺序（无 stop / 无换目标）
w = World(cards)
r = run(w, A, D, damage=3, damage_to_attacker=2, cost=2)
expect = [
    ("OnOtherCardAttackSwitchTarget", "A"),       # 0x1E：攻击者**也**被问（不排除）
    ("OnOtherCardAttackSwitchTarget", "Y"),
    # 0x1F：攻击者 A 虽然覆写了它，但**被排除**；只问 Y
    ("OnOtherCardAttacks", "Y"),
    ("<pay>", "2"),                               # 指挥点在 OnBeforeAttack 之前扣
    ("OnBeforeAttack", "A"),                      # 自己的先
    ("OnBeforeOtherCardAttacks", "X"),            # 然后 0x0D
    ("OnReceiveDamage", "D"),                     # 受击通知：防守方先
    ("OnOtherCardReceiveDamage", "Y"),
    ("OnReceiveDamage", "A"),                     # 然后攻击者（反击伤害 2>0）
    ("OnOtherCardReceiveDamage", "Y"),
    ("OnAfterAttack", "A"),                       # 攻击后：自己的先
    ("OnAfterOtherCardAttacks", "X"),             # 0x04：按触发表顺序，Z 被压制被丢掉
    ("OnAfterOtherCardAttacks", "Y"),
    ("OnOperationKreditsSpent", "A"),             # 花费通知：自己的先
    ("OnOtherCardOperationKreditsSpent", "Y"),
]
chk("完整顺序：0x1E(含攻击者) → 0x1F(排除攻击者) → 付费 → 自己/0x0D → 受击 → 自己/0x04 → 花费通知",
    names(r) == expect, names(r))
chk("双方都枚举：本方 X 与敌方 Y 的 0x04 都跑了；被压制的 Z 没跑",
    [n for h, n in names(r) if h == "OnAfterOtherCardAttacks"] == ["X", "Y"])
chk("outcome=resolved，付了 2", r["outcome"] == "resolved" and r["paid"] == 2)

# 2) 参数顺序不能反：0x04 是 (defender, attacker)，0x1F/0x0D/0x1E 是 (attacker, defender)
c04 = [a for h, p, a in w.calls if h == "OnAfterOtherCardAttacks"][0]
c0d = [a for h, p, a in w.calls if h == "OnBeforeOtherCardAttacks"][0]
c1f = [a for h, p, a in w.calls if h == "OnOtherCardAttacks"][0]
c1e = [a for h, p, a in w.calls if h == "OnOtherCardAttackSwitchTarget"][0]
chk("0x04 实参：defenderCard=D(2)、attackerCard=A(1)、damageToDefender=3、attackCost=2",
    c04 == {"defenderCard": 2, "attackerCard": 1, "damageToDefender": 3, "attackCost": 2}, c04)
chk("0x0D 实参：cardAttacking=A(1)、defenderCard=D(2)", c0d == {"cardAttacking": 1, "defenderCard": 2}, c0d)
chk("0x1F 实参：cardAttacking=A(1)、defenderCard=D(2)", c1f == {"cardAttacking": 1, "defenderCard": 2}, c1f)
chk("0x1E 实参：cardAttacking=A(1)、oldDefender=D(2)", c1e == {"cardAttacking": 1, "oldDefender": 2}, c1e)
own_after = [a for h, p, a in w.calls if h == "OnAfterAttack"][0]
chk("OnAfterAttack 自己的钩子实参：(defenderCard, wasShockAttack, attackCost)",
    own_after == {"defenderCard": 2, "wasShockAttack": False, "attackCost": 2}, own_after)
c34 = [a for h, p, a in w.calls if h == "OnOtherCardReceiveDamage"]
chk("0x34 实参：fromCard=施害方, toCard=受害方, fromAttack=True, Damage（防守方受 3，攻击者受 2）",
    c34 == [{"fromCard": 1, "toCard": 2, "fromAttack": True, "Damage": 3},
            {"fromCard": 2, "toCard": 1, "fromAttack": True, "Damage": 2}], c34)
ks = [a for h, p, a in w.calls if h == "OnOtherCardOperationKreditsSpent"][0]
chk("0x44 实参：cardOperated=A、kreditsSpent=cost", ks == {"cardOperated": 1, "kreditsSpent": 2}, ks)

# 3) stopAttack：付费前结束，什么后续都不触发
w = World(cards, {("OnOtherCardAttacks", 4): {"stopAttack": True}})
r = run(w, A, D, damage=3, cost=2)
chk("stopAttack=true ⇒ 不付费、不触发 OnBeforeAttack/0x0D/受击/OnAfterAttack/0x04/花费通知",
    r["outcome"] == "stopped" and r["paid"] == 0 and r["stop"]
    and [h for h, _n in names(r)] == ["OnOtherCardAttackSwitchTarget", "OnOtherCardAttackSwitchTarget",
                                      "OnOtherCardAttacks"], names(r))
chk("stop 影响结果：换掉 Y 的出参（不 stop）答案就变回 resolved",
    run(World(cards, {("OnOtherCardAttacks", 4): {"stopAttack": False}}), A, D, damage=3,
        cost=2)["outcome"] == "resolved")

# 3b) 0x1F 不提前 break：两张牌都问了，stop 取"或"
C1 = FakeCard("C1", 11, OPP, ovr=("OnOtherCardAttacks",))
C2 = FakeCard("C2", 12, OPP, ovr=("OnOtherCardAttacks",))
w = World([A, D, C1, C2], {("OnOtherCardAttacks", 11): {"stopAttack": True}})
r = run(w, A, D, damage=1, cost=1)
chk("0x1F 全问一遍（C1 回 stop 之后 C2 仍被问）",
    [n for h, n in names(r) if h == "OnOtherCardAttacks"] == ["C1", "C2"] and r["stop"])

# 4) AttackedAndStopped：付费、OnBeforeAttack/0x0D 仍跑，伤害被吞：OnAttackStopped + 花费通知，无 OnAfterAttack/0x04
w = World(cards, {("OnOtherCardAttacks", 4): {"AttackedAndStopped": True}})
r = run(w, A, D, damage=3, cost=2)
hs = [h for h, _n in names(r)]
chk("AttackedAndStopped ⇒ 消耗行动：付费、OnBeforeAttack、0x0D、OnAttackStopped、花费通知；无受击/OnAfterAttack/0x04",
    r["outcome"] == "consumed" and r["paid"] == 2 and r["attacked_and_stopped"] and not r["stop"]
    and hs == ["OnOtherCardAttackSwitchTarget", "OnOtherCardAttackSwitchTarget", "OnOtherCardAttacks", "<pay>",
               "OnBeforeAttack", "OnBeforeOtherCardAttacks", "OnAttackStopped",
               "OnOperationKreditsSpent", "OnOtherCardOperationKreditsSpent"], hs)
chk("AttackedAndStopped 之后防守方置空（defender=0）", r["defender"] == 0)

# 5) 换目标：第一张回新防守方的牌生效并 break；之后的钩子都用新防守方
D2 = FakeCard("D2", 20, OPP, ovr=())
w = World([A, D, D2, X, Y], {("OnOtherCardAttackSwitchTarget", 1): {"newDefender": 20},
                             ("OnOtherCardAttackSwitchTarget", 4): {"newDefender": 99}})
r = run(w, A, D, damage=3, cost=2)
sw = [n for h, n in names(r) if h == "OnOtherCardAttackSwitchTarget"]
chk("换目标 break：A 先换（D→D2），Y 不再被问", sw == ["A"] and r["switched"] and r["defender"] == 20, names(r))
c04 = [a for h, p, a in w.calls if h == "OnAfterOtherCardAttacks"][0]
c1f = [a for h, p, a in w.calls if h == "OnOtherCardAttacks"][0]
chk("换目标之后 0x1F/0x04 都拿新防守方 D2(20)", c04["defenderCard"] == 20 and c1f["defenderCard"] == 20, (c04, c1f))
chk("换目标影响结果：不换时防守方仍是 D(2)",
    run(World([A, D, D2, X, Y]), A, D, damage=3, cost=2)["defender"] == 2)
chk("newDefender 回自己（== 当前防守方）⇒ 视为没换，继续问下一张",
    names(run(World([A, D, Y], {("OnOtherCardAttackSwitchTarget", 1): {"newDefender": 2},
                                ("OnOtherCardAttackSwitchTarget", 4): {"newDefender": 4}}),
              A, D, damage=1, cost=1)).count(("OnOtherCardAttackSwitchTarget", "Y")) == 1)

# 6) 随机流共用 + 触发表顺序决定"谁先抽"：换顺序答案要变
def draw_eff(args, stream):
    return {"_eff": {"damage": stream.draw()}}


def with_idx(cs):
    """给整局的牌都标上创建序（`_registry_order` 要求每张都有，缺一张就如实退化为快照顺序）。"""
    out = []
    for i, c in enumerate(cs):
        n = FakeCard(c.name, c.raw["ptr"], c.side, ovr=c.ovr, suppressed=c.is_suppressed, enum_idx=i)
        out.append(n)
    return out


P = FakeCard("P", 31, ME, ovr=("OnAfterOtherCardAttacks",))
Q = FakeCard("Q", 32, OPP, ovr=("OnAfterOtherCardAttacks",))
sc = {("OnAfterOtherCardAttacks", 31): draw_eff, ("OnAfterOtherCardAttacks", 32): draw_eff}
base = [A, D, P, Q]
r1 = run(World(with_idx(base), sc), A, D, damage=1, cost=1)
e1 = {h["name"]: h["eff"]["damage"] for h in r1["hits"] if h["hook"] == "OnAfterOtherCardAttacks"}
# 创建顺序对调：快照里 P 仍排在 Q 前面，但 Q 的 enum_idx 更小
swapped = with_idx([A, D, Q, P])
snap = {c.name: c for c in swapped}
r2 = run(World([snap["A"], snap["D"], snap["P"], snap["Q"]], sc), A, D, damage=1, cost=1)
e2 = {h["name"]: h["eff"]["damage"] for h in r2["hits"] if h["hook"] == "OnAfterOtherCardAttacks"}
chk("共用一条流 + 触发表顺序：先创建的先抽（P=10,Q=20）", e1 == {"P": 10, "Q": 20}, e1)
chk("换创建顺序（enum_idx 对调）⇒ 答案跟着变（Q=10,P=20）", e2 == {"Q": 10, "P": 20}, e2)
chk("抽取数累加到同一条流上（draws=2）", r1["draws"] == 2)
r3 = run(World([A, D, Q, P], sc), A, D, damage=1, cost=1)            # 没有 enum_idx 的退化：保持快照顺序
chk("没有 enum_idx ⇒ 保持快照顺序（如实退化）",
    [h["name"] for h in r3["hits"] if h["hook"] == "OnAfterOtherCardAttacks"] == ["Q", "P"])

# 7) 压制：被压制的攻击者不跑自己的 OnBeforeAttack/OnAfterAttack；被压制的受击方没有受击通知
As = FakeCard("A", 1, ME, ovr=A.ovr, suppressed=True)
r = run(World([As, D, X, Y]), As, D, damage=3, cost=2)
chk("攻击者被压制 ⇒ 自己的 OnBeforeAttack/OnAfterAttack 不跑；花费通知照跑（字节码不查压制）；别人的 0x04 照跑",
    ("OnBeforeAttack", "A") not in names(r) and ("OnAfterAttack", "A") not in names(r)
    and ("OnOperationKreditsSpent", "A") in names(r)
    and ("OnAfterOtherCardAttacks", "X") in names(r))
Ds = FakeCard("D", 2, OPP, ovr=("OnReceiveDamage",), suppressed=True)
r = run(World([A, Ds, X, Y]), A, Ds, damage=3, cost=2)
chk("受击方被压制 ⇒ 无受击通知（OnReceiveDamage/0x34 对防守方）",
    [n for h, n in names(r) if h == "OnReceiveDamage"] == [] and
    [h for h in names(r) if h == ("OnOtherCardReceiveDamage", "Y")] == [])
r = run(World([A, D, X, Y]), A, D, damage=0, cost=2)
chk("伤害 0 ⇒ 防守方没有受击通知", ("OnReceiveDamage", "D") not in names(r))

# 8) 换卡 ⇒ 答案变：去掉 X 后 0x04/0x0D 里就没有 X
r = run(World([A, D, Y]), A, D, damage=3, cost=2)
chk("换卡影响结果：没有 X ⇒ 0x04 只剩 Y", [n for h, n in names(r) if h == "OnAfterOtherCardAttacks"] == ["Y"])
# 9) 指挥点在 OnBeforeAttack 之前：order 里 <pay> 的位置
r = run(World(cards), A, D, damage=3, cost=2)
i_pay = [h for h, _n in names(r)].index("<pay>")
i_before = [h for h, _n in names(r)].index("OnBeforeAttack")
chk("付指挥点在 OnBeforeAttack 之前、0x1F 之后（字节码 @2764）",
    [h for h, _n in names(r)].index("OnOtherCardAttacks") < i_pay < i_before)
# 10) include_own=False ⇒ 攻击者自己的钩子都不跑
r = run(World(cards), A, D, damage=3, cost=2, include_own=False)
chk("include_own=False：自己的钩子都不跑，别人的照跑",
    all(h not in ("OnBeforeAttack", "OnAfterAttack", "OnOperationKreditsSpent") for h, _n in names(r))
    and ("OnAfterOtherCardAttacks", "X") in names(r))


# ==============================================================================================
# 伤害管线（CalculateDamageDealt 的移植 + ExecuteOnDealDamageAddDamage / AfterCalc 的钩子）
# ==============================================================================================
def unit(name, ptr, side, typ="infantry", atk=2, dfn=2, kw=(), ovr=(), **k):
    return FakeCard(name, ptr, side, ovr=ovr, card_type=typ, attack=atk, defense=dfn, keywords=list(kw), **k)


def dmg(world, a, d, **kw):
    return run(world, a, d, cost=1, **kw)


# 1) 基础：3 攻打 2 防 ⇒ 打死；换防守方 5 防 ⇒ 没打死（换数据答案要变）
A1, D1 = unit("A", 1, ME, atk=3, dfn=4), unit("D", 2, OPP, atk=2, dfn=2)
r = dmg(World([A1, D1]), A1, D1)
chk("基础伤害：3 攻打 2 防 ⇒ 3 点、防守方阵亡；反击 2 点、攻击者(防 4)不死",
    r["damage"] == 3 and r["defender_destroyed"] and r["damage_to_attacker"] == 2 and not r["attacker_destroyed"], r)
D1b = unit("D", 2, OPP, atk=2, dfn=5)
r = dmg(World([A1, D1b]), A1, D1b)
chk("换防守方(防 5) ⇒ 不阵亡（答案跟着变）", r["damage"] == 3 and not r["defender_destroyed"])

# 2) 重甲：防守方重甲 2 ⇒ 3-2=1
Dar = unit("D", 2, OPP, atk=2, dfn=5, raw={"total_heavy_armor": 2})
chk("重甲 2 ⇒ 伤害 1", dmg(World([A1, Dar]), A1, Dar)["damage"] == 1)

# 3) 伏击：防守方伏击、攻击者防 2 ⇒ 防守方的伤害(3) >= 攻击者防(2) ⇒ 攻击者打不出伤害，且被打死
Aamb = unit("A", 1, ME, atk=3, dfn=2)
Damb = unit("D", 2, OPP, atk=3, dfn=5, kw=("ambush",))
r = dmg(World([Aamb, Damb]), Aamb, Damb)
chk("伏击：攻击者伤害被清零且阵亡", r["damage"] == 0 and r["attacker_destroyed"], r)
Aamb2 = unit("A", 1, ME, atk=3, dfn=4)
r = dmg(World([Aamb2, Damb]), Aamb2, Damb)
chk("伏击：攻击者防 4 > 3 ⇒ 伏击杀不死，正常对打（答案跟着变）", r["damage"] == 3 and not r["attacker_destroyed"], r)
Dwas = unit("D", 2, OPP, atk=3, dfn=5, kw=("ambush",), has_been_attacked_this_turn=True)
r = dmg(World([Aamb, Dwas]), Aamb, Dwas)
chk("伏击只在'本回合首次被攻击'生效：已被攻击过 ⇒ 不触发", r["damage"] == 3, r)

# 4) 冲击：攻击者带冲击 ⇒ 防守方反击为 0，wasShockAttack=True 传给 OnAfterAttack
Ash = unit("A", 1, ME, atk=3, dfn=4, kw=("shock",), ovr=("OnAfterAttack",))
D4 = unit("D", 2, OPP, atk=2, dfn=5)
w = World([Ash, D4])
r = dmg(w, Ash, D4)
oa = [a for h, p, a in w.calls if h == "OnAfterAttack"][0]
chk("冲击：反击为 0；OnAfterAttack.wasShockAttack=True", r["damage_to_attacker"] == 0 and oa["wasShockAttack"] is True, (r, oa))

# 5) 炮兵攻击不吃反击；轰炸机攻击步兵无反击；轰炸机攻击战斗机有反击
Art = unit("A", 1, ME, typ="artillery", atk=3, dfn=2)
chk("炮兵攻击 ⇒ 无反击", dmg(World([Art, D4]), Art, D4)["damage_to_attacker"] == 0)
Bmb = unit("B", 3, ME, typ="bomber", atk=2, dfn=2)
chk("轰炸机攻击步兵 ⇒ 无反击", dmg(World([Bmb, D4]), Bmb, D4)["damage_to_attacker"] == 0)
Fgt = unit("F", 4, OPP, typ="fighter", atk=2, dfn=2)
chk("轰炸机攻击战斗机 ⇒ 有反击(战斗机例外)", dmg(World([Bmb, Fgt]), Bmb, Fgt)["damage_to_attacker"] == 2)

# 6) lethal：1 点伤害也打死非总部；对总部(location)无效
Alt = unit("A", 1, ME, atk=1, dfn=3, raw={"received_abilities": [{"ability": "lethal"}]})
Dbig = unit("D", 2, OPP, atk=0, dfn=9)
chk("lethal：1 点伤害打死 9 防单位", dmg(World([Alt, Dbig]), Alt, Dbig)["defender_destroyed"])
HQe = FakeCard("HQ", 9, OPP, card_type="location", attack=0, defense=9, keywords=[], location="hq")
r = dmg(World([Alt, HQe]), Alt, HQe)
chk("lethal 对总部无效", not r["defender_destroyed"] and r["outcome"] == "resolved", r)

# 7) 钩子改写伤害：ModifyDamageDealt(+2) → 0x25(+1) → 自己 AfterCalc(+1) → 0x26(+1，第二张不再被问)
Ah = unit("A", 1, ME, atk=3, dfn=9, ovr=("OnCardDealDamage_ModifyDamageDealt", "OnDealDamageAddDamageAfterCalc"))
Dh = unit("D", 2, OPP, atk=0, dfn=20)
X25 = FakeCard("X25", 31, ME, ovr=("OnOtherCardDealDamageAddDamage",), card_type="order")
Y26 = FakeCard("Y26", 32, OPP, ovr=("OnOtherCardDealDamageAddDamageAfterCalc",), card_type="order")
Z26 = FakeCard("Z26", 33, OPP, ovr=("OnOtherCardDealDamageAddDamageAfterCalc",), card_type="order")
sc = {("OnCardDealDamage_ModifyDamageDealt", 1): lambda a, st_: {"newDamage": a["Damage"] + 2},
      ("OnOtherCardDealDamageAddDamage", 31): {"damageToAdd": 1},
      ("OnDealDamageAddDamageAfterCalc", 1): {"damageToAdd": 1},
      ("OnOtherCardDealDamageAddDamageAfterCalc", 32): {"damageToAdd": 1, "stopAdding": True}}
w = World([Ah, Dh, X25, Y26, Z26], sc)
r = dmg(w, Ah, Dh)
chk("伤害管线：3 +2(ModifyDamageDealt) +1(0x25) +1(自己AfterCalc) +1(0x26) = 8", r["damage"] == 8, r["damage"])
chk("0x26 回 stopAdding 之后 Z26 不再被问（两次 AfterCalc 各只问到 Y26）",
    [p for h, p, a in w.calls if h == "OnOtherCardDealDamageAddDamageAfterCalc"] == [32, 32])
chk("ModifyDamageDealt 被调 2 次（两次 CalculateDamageDealt 各一次）",
    len([1 for h, p, a in w.calls if h == "OnCardDealDamage_ModifyDamageDealt" and p == 1]) == 2)
w0 = World([Ah, Dh, X25, Y26, Z26], {})
chk("管线影响结果：钩子都不改写 ⇒ 3（脚本不同答案不同）", dmg(w0, Ah, Dh)["damage"] == 3)
a25 = [a for h, p, a in w.calls if h == "OnOtherCardDealDamageAddDamage"][0]
chk("0x25 实参：cardDealingDamage/toCard/Damage(已含 ModifyDamageDealt)/fromAttack/isDefenderDamage",
    a25 == {"cardDealingDamage": 1, "toCard": 2, "Damage": 5, "fromAttack": True, "isDefenderDamage": False}, a25)
a26 = [a for h, p, a in w.calls if h == "OnOtherCardDealDamageAddDamageAfterCalc"][0]
chk("0x26 实参：cardDealingDamage/toCard/Damage/fromAttack/isRedirected",
    a26 == {"cardDealingDamage": 1, "toCard": 2, "Damage": 7, "fromAttack": True, "isRedirected": False}, a26)

# 8) 受击/幸存/造成伤害/摧毁链的顺序与形参
ALL_OWN = ("OnReceiveDamage", "OnSurvivedCombat", "OnCardDealDamage", "OnDestroyed", "OnBeforeDestroyed",
           "OnLeaveBoardOrOwner", "OnAfterLeaveBoard", "OnAfterDestroyed", "OnCardLocationMoved")
A8 = unit("A", 1, ME, atk=3, dfn=4, ovr=ALL_OWN)
D8 = unit("D", 2, OPP, atk=5, dfn=2, ovr=ALL_OWN)
Xo = FakeCard("X", 3, ME, ovr=("OnOtherCardDestroyed", "OnOtherCardSurvivedCombat", "OnOtherCardDealDamage",
                                    "OnBeforeOtherCardDestroyed", "OnOtherCardLeaveBoardOrOwner",
                                    "OnAfterOtherCardLeaveBoardOrOwner", "OnOtherCardLocationMoved"), card_type="order")
w = World([A8, D8, Xo])
r = dmg(w, A8, D8)                 # A 3/4 vs D 5/2：双方都打死
hs = [(h, n) for h, n in names(r)]
chk("双方都死：先防守方后攻击方，逐阶段交错（BeforeDestroyed→BeforeLeave→Moved→AfterLeave→Destroyed）",
    r["attacker_destroyed"] and r["defender_destroyed"] and
    [x for x in hs if x[0] in ("OnBeforeDestroyed", "OnLeaveBoardOrOwner", "OnCardLocationMoved", "OnAfterLeaveBoard",
                               "OnDestroyed", "OnAfterDestroyed")]
    == [("OnBeforeDestroyed", "D"), ("OnBeforeDestroyed", "A"), ("OnLeaveBoardOrOwner", "D"),
        ("OnLeaveBoardOrOwner", "A"), ("OnCardLocationMoved", "D"), ("OnCardLocationMoved", "A"),
        ("OnAfterLeaveBoard", "D"), ("OnAfterLeaveBoard", "A"), ("OnDestroyed", "D"), ("OnAfterDestroyed", "D"),
        ("OnDestroyed", "A"), ("OnAfterDestroyed", "A")], hs)
chk("双方都死 ⇒ 没有幸存通知", all(h not in ("OnSurvivedCombat", "OnOtherCardSurvivedCombat") for h, _n in hs))
od = [a for h, p, a in w.calls if h == "OnOtherCardDestroyed"]
chk("0x27 实参：cardDestroyed/killer/TriggerNotDestroyed/destroyedLocation/selfIsAlsoGettingDestroyed/destroyedInCombat",
    od[0] == {"cardDestroyed": 2, "killer": 1, "TriggerNotDestroyed": False, "destroyedLocation": 7,
              "selfIsAlsoGettingDestroyed": False, "destroyedInCombat": True}, od[0])
# 只有防守方死：攻击者幸存 ⇒ 幸存通知(攻击者)；摧毁链只对防守方
A8b = unit("A", 1, ME, atk=3, dfn=9, ovr=ALL_OWN)
r = dmg(World([A8b, D8, Xo]), A8b, D8)
hs = [(h, n) for h, n in names(r)]
chk("只有防守方死：攻击者幸存通知 + 防守方摧毁链；防守方没有幸存通知",
    ("OnSurvivedCombat", "A") in hs and ("OnSurvivedCombat", "D") not in hs and ("OnDestroyed", "D") in hs
    and ("OnDestroyed", "A") not in hs and ("OnOtherCardSurvivedCombat", "X") in hs, hs)
i = {k: n for n, k in enumerate(hs)}
chk("顺序：受击 → 幸存 → 造成伤害 → 摧毁链",
    i[("OnReceiveDamage", "D")] < i[("OnSurvivedCombat", "A")] < i[("OnCardDealDamage", "A")]
    < i[("OnBeforeDestroyed", "D")] < i[("OnDestroyed", "D")], hs)

# 9) 总部被打死 ⇒ 对局结束，后面什么都不发生
HQ9 = FakeCard("HQ", 9, OPP, card_type="location", attack=0, defense=3, keywords=[], location="hq",
               ovr=("OnOtherCardDestroyed",))
A9 = unit("A", 1, ME, atk=3, dfn=4, ovr=("OnAfterAttack", "OnOperationKreditsSpent"))
r = dmg(World([A9, HQ9, Xo]), A9, HQ9)
chk("打死总部 ⇒ outcome=match_end、winner=local；无 OnAfterAttack/花费通知/摧毁链",
    r["outcome"] == "match_end" and r["match_end"] == ME
    and all(h not in ("OnAfterAttack", "OnOperationKreditsSpent", "OnOtherCardDestroyed") for h, _n in names(r)),
    names(r))
HQ9b = FakeCard("HQ", 9, OPP, card_type="location", attack=0, defense=9, keywords=[], location="hq")
chk("总部没死 ⇒ 照常 resolved", dmg(World([A9, HQ9b]), A9, HQ9b)["outcome"] == "resolved")
chk("防守方是总部 ⇒ 没有幸存通知",
    all(h not in ("OnSurvivedCombat", "OnOtherCardSurvivedCombat")
        for h, _n in names(dmg(World([A9, HQ9b, Xo]), A9, HQ9b))))

# 10) excess：溢出伤害打总部
Aex = unit("A", 1, ME, atk=5, dfn=9, raw={"received_abilities": [{"ability": "excess"}]})
Dex = unit("D", 2, OPP, atk=0, dfn=2)
r = dmg(World([Aex, Dex]), Aex, Dex)
chk("excess：5 打 2 防 ⇒ 对单位 2、溢出 3 打总部", r["damage"] == 2 and r["excess"] == 3 and
    any(h["hook"] == "<excess>" and h["eff"] == {"damage_hq": 3} for h in r["hits"]), r)

Xex = FakeCard("X", 3, ME, ovr=("OnAfterOtherCardAttacks",), card_type="order")
wex = World([Aex, Dex, Xex])
r = dmg(wex, Aex, Dex)
c04x = [a for h, p, a in wex.calls if h == "OnAfterOtherCardAttacks"][0]
chk("0x04 的 damageToDefender 是 excess 夹值之前的最终伤害(5)，而不是夹后的(2)", c04x["damageToDefender"] == 5 and r["damage"] == 2, c04x)

# ==============================================================================================
# 钩子记录 → sim.engine 效果（to_fx / hit_effects）
# ==============================================================================================
HQe2 = FakeCard("HQ", 90, OPP, card_type="location", defense=20, location="hq", keywords=[])
HQl2 = FakeCard("HQ", 91, ME, card_type="location", defense=20, location="hq", keywords=[])
UB = unit("UB", 5, ME, atk=1, dfn=1)
st_fx = _St([A1, D1, UB, HQe2, HQl2])
hit = {"hook": "OnAfterOtherCardAttacks", "bucket": "after", "records": [
    {"verb": "ChangeAttack", "args": [5, 0, 2, 0], "tainted": False},                 # 给 UB(ptr 5) +2 攻
    {"verb": "DrawCardsFromDeckBySide", "args": [0, 1, 2], "tainted": False},          # 抽 2
    {"verb": "DamageCard", "args": [90, 3, 1, False, False, False], "tainted": False},  # 打敌方总部 3
    {"verb": "ChangeDefense", "args": [91, 0, 3, 0], "tainted": False},                # 己方总部 +3
    {"verb": "PinUnit", "args": [2], "tainted": False}], "chance": []}
g, per = T.hit_effects(hit, st_fx, my_side=ME, hq_own=(91,), hq_enemy=(90,))
chk("hit_effects：目标是某张牌 ⇒ 记到那张牌（UB +2 攻、D 被压制）",
    per.get(5, {}).get("buff") == [2, 0] and per.get(2, {}).get("pin") is True, per)
chk("hit_effects：抽牌/打敌方总部/己方总部回血 ⇒ 全局",
    g.get("draw") == 2 and g.get("damage_hq") == 3 and g.get("heal_hq") == 3, g)
chk("hit_effects：换目标 ⇒ 效果落在另一张牌上（答案变）",
    T.hit_effects(dict(hit, records=[{"verb": "ChangeAttack", "args": [2, 0, 2, 0], "tainted": False}]),
                  st_fx, my_side=ME)[1].get(2, {}).get("buff") == [2, 0])
fx = T.to_fx({"hits": [dict(hit, hook="OnAfterAttack")], "stop": False, "attacked_and_stopped": False,
              "switched": False, "defender": 2, "paid": 1, "damage": 3, "damage_to_attacker": 2,
              "defender_destroyed": True, "attacker_destroyed": False, "match_end": None},
             st_fx, my_side=ME, hq_own=(91,), hq_enemy=(90,))
chk("to_fx：own_after、dmg/dies 透传、after 桶带全局+逐牌",
    fx["own_after"] and fx["dmg_def"] == 3 and fx["def_dies"] and not fx["att_dies"]
    and fx["buckets"]["after"]["eff"].get("draw") == 2
    and (5, {"buff": [2, 0]}) in fx["buckets"]["after"]["units"], fx)
fx2 = T.to_fx({"hits": [], "stop": False, "attacked_and_stopped": False, "switched": True, "defender": 5,
               "paid": 1, "damage": 0, "damage_to_attacker": 0, "defender_destroyed": False,
               "attacker_destroyed": False, "match_end": None}, st_fx, my_side=ME)
chk("to_fx：换目标 ⇒ switch_to = 新防守方的 card_id", fx2["switch_to"] == 5)
chk("to_fx：被吞的标志", T.to_fx({"hits": [], "stop": True, "attacked_and_stopped": False, "switched": False,
                                "defender": 2}, st_fx, my_side=ME)["stop"] is True)

# ==============================================================================================
# sim.engine 消费 attack_fx：stop / consumed / switch / 伤害覆盖 / 各时点效果
# ==============================================================================================
from engine.adapter import from_cards                            # noqa: E402
from engine.state import Sim, U                                  # noqa: E402
from evaluation.value import W, delta, evaluate                  # noqa: E402
from policy.search import gen_actions                            # noqa: E402
from sim.engine import _apply_eff, sim_attack                    # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cards import MY_SIDE, mk_card                              # noqa: E402


def mksim(fx=None, kred=5.0):
    us = {1: U(1, ME, "back", 3, 4, 3, "infantry", opc=1),
          2: U(2, OPP, "frontline", 2, 2, 3, "infantry"),
          3: U(3, OPP, "frontline", 1, 6, 3, "infantry")}
    return Sim(us, {ME: 20, OPP: 20}, kred, attack_fx=fx, my_side=ME)


s0 = sim_attack(mksim(), 1, 2)
chk("基线(无 fx)：3 攻打 2/2 ⇒ 打死，付 1 点指挥点", 2 not in s0.units and s0.kredits == 4.0)
sa = mksim({(1, 2): {"stop": True}})
s1 = sim_attack(sa, 1, 2)
chk("fx.stop ⇒ 不付费、不耗行动、防守方原样（攻击被吞）",
    s1.kredits == 5.0 and s1.units[1].attacks_left == 1 and 2 in s1.units and s1.units[2].dfn == 2)
chk("fx.stop ⇒ 搜索里不再生成这一步（别的目标仍在）",
    not any(a.kind == "attack" and a.src == 1 and a.dst == 2 for a in gen_actions(sa))
    and any(a.kind == "attack" and a.src == 1 and a.dst == 3 for a in gen_actions(sa)))
chk("fx.stop 改变评估：delta(吞) != delta(不吞)",
    delta(mksim(), sim_attack(mksim(), 1, 2)) != delta(sa, s1))
sc_ = mksim({(1, 2): {"consumed": True, "paid": 2, "buckets": {"after": {"eff": {"draw": 1}, "units": []}}}})
s2 = sim_attack(sc_, 1, 2)
chk("fx.consumed ⇒ 付 paid(2)、耗行动、不结算伤害、仍触发 after 桶(抽 1)",
    s2.kredits == 3.0 and s2.units[1].attacks_left == 0 and 2 in s2.units and s2.units[2].dfn == 2
    and s2.draws_done == 1)
ss = mksim({(1, 2): {"switch_to": 3}})
s3 = sim_attack(ss, 1, 2)
chk("fx.switch_to ⇒ 伤害落在新防守方 3（3 防 6 → 3），原目标 2 毫发无损",
    s3.units[2].dfn == 2 and s3.units[3].dfn == 3)
ssh = mksim({(1, 2): {"switch_to": "hq"}})
chk("fx.switch_to=hq ⇒ 总部吃伤害",
    sim_attack(ssh, 1, 2).hq[OPP] == 17 and 2 in sim_attack(ssh, 1, 2).units)
sd = mksim({(1, 2): {"dmg_def": 0, "dmg_att": 4, "def_dies": False, "att_dies": True}})
s4 = sim_attack(sd, 1, 2)
chk("fx 伤害覆盖：打 0、被反击 4 阵亡 ⇒ 攻击者消失、防守方在", 1 not in s4.units and s4.units[2].dfn == 2)
sd2 = mksim({(1, 2): {"dmg_def": 9, "dmg_att": 0, "def_dies": True, "att_dies": False}})
chk("fx 伤害覆盖：dmg_def=9 ⇒ 防守方阵亡、攻击者无损",
    2 not in sim_attack(sd2, 1, 2).units and sim_attack(sd2, 1, 2).units[1].dfn == 4)
bk = {"before": {"eff": {}, "units": [(1, {"buff": [2, 0]})]}, "mid": {"eff": {"damage_hq": 2}, "units": []},
      "def_destroyed": {"eff": {"kredit": 1}, "units": []}, "att_destroyed": {"eff": {"kredit": 5}, "units": []},
      "after": {"eff": {"heal_hq": 3}, "units": [(1, {"pin": True})]}}
sb = mksim({(1, 2): {"dmg_def": 3, "dmg_att": 0, "def_dies": True, "att_dies": False, "buckets": bk}})
s5 = sim_attack(sb, 1, 2)
chk("fx 各时点效果：before(攻击者+2攻)、mid(敌总部-2)、def_destroyed(+1 指挥点)、after(我方总部+3、攻击者被压制)；"
    "att_destroyed 没触发",
    s5.units[1].atk == 5 and s5.hq[OPP] == 18 and s5.kredits == 5.0 - 1 + 1 and s5.hq[ME] == 23
    and s5.units[1].pinned and 2 not in s5.units, (s5.units[1].atk, s5.hq, s5.kredits))
shq = mksim({(1, "hq"): {"dmg_def": 5, "own_after": True,
                         "buckets": {"after": {"eff": {"draw": 1}, "units": []}}}})
shq.units[1].aa_hq = {"draw": 3}
s6 = sim_attack(shq, 1, None, hq=True)
chk("打总部：dmg_def 覆盖攻击力(5)、own_after 时不再叠 aa_hq(只抽 1 而不是 4)",
    s6.hq[OPP] == 15 and s6.draws_done == 1, (s6.hq, s6.draws_done))
shq2 = mksim({(1, "hq"): {"buckets": {"after": {"eff": {"draw": 1}, "units": []}}}})
shq2.units[1].aa_hq = {"draw": 3}
chk("打总部：没 own_after ⇒ aa_hq 照叠(3) + after(1) = 4", sim_attack(shq2, 1, None, hq=True).draws_done == 4)
sk = mksim()
sk.units[2].kw = frozenset(("ambush", "guard"))
_apply_eff(sk, {"remove_ambush": True, "attack_turn": 2, "opcost": 1}, 2)
chk("_apply_eff：remove_ambush / attack_turn / opcost",
    sk.units[2].kw == frozenset(("guard",)) and sk.units[2].atk == 4 and sk.units[2].opc == 2)
_apply_eff(sk, {"remove_unit": True}, 2)
chk("_apply_eff：remove_unit ⇒ 单位离场", 2 not in sk.units)
cs = [mk_card(1, ME, "frontline", "infantry", 3, 4, 2, 1, "L", 0),
      mk_card(2, OPP, "frontline", "infantry", 1, 1, 2, 1, "E", 0)]
sf = from_cards(cs, lambda c: set(), kredits=3.0, attack_fx={(1, 2): {"stop": True}}, my_side=MY_SIDE)
chk("from_cards 把 attack_fx 传给 Sim", sf.attack_fx == {(1, 2): {"stop": True}})

# ==============================================================================================
# effectvm 的影子 customJson（JSON_* 钩子）
# ==============================================================================================
from semantics import effectvm as EV                                # noqa: E402


class _E:                                                        # 假调用点：kids 里的出参变量
    def __init__(self, outs):
        self.kids = [type("K", (), {"args": {"prop": nm}})() for nm in outs]


class _F:
    locals = {}


rec = EV.Recorder(0, 0, None)
rec.json_loader = lambda card: {"wasAttacked": True} if card == 7 else {}
fr = _F()
fr.locals = {}
get = rec.hook("JSON_GetBool")
get(None, fr, None, [7, "wasAttacked"], _E(["card", "name", "val", "found"]))
chk("JSON_GetBool：影子初值来自活内存读到的 bool 标记", fr.locals.get("val") is True and fr.locals.get("found") is True,
    fr.locals)
rec.hook("JSON_SetBool")(None, fr, None, [7, "wasAttacked", False], _E(["card", "name", "v", "found"]))
get(None, fr, None, [7, "wasAttacked"], _E(["card", "name", "val", "found"]))
chk("JSON_SetBool 写进影子、再读得到新值（不回写游戏）", fr.locals.get("val") is False)
get(None, fr, None, [8, "nope"], _E(["card", "name", "val", "found"]))
chk("没有的键 ⇒ found=False、值=默认", fr.locals.get("found") is False and fr.locals.get("val") is False)
rec.hook("JSON_AddToIntArray")(None, fr, None, [7, "arr", 4], _E(["c", "n", "v", "found"]))
rec.hook("JSON_AddToIntArray")(None, fr, None, [7, "arr", 5], _E(["c", "n", "v", "found"]))
rec.hook("JSON_GetIntArray")(None, fr, None, [7, "arr"], _E(["c", "n", "vals", "found"]))
chk("JSON 数组：AddToIntArray x2 ⇒ [4,5]", fr.locals.get("vals") == [4, 5])
rec.hook("JSON_Clear")(None, fr, None, [7, "arr"], _E(["c", "n", "found"]))
rec.hook("JSON_GetIntArray")(None, fr, None, [7, "arr"], _E(["c", "n", "vals", "found"]))
chk("JSON_Clear ⇒ 键没了", fr.locals.get("found") is False)
get2 = rec.hook("JSON_GetInt")
get2(None, fr, None, [7, "count"], _E(["c", "n", "val", "found"]))
chk("int 键初值读不出 ⇒ 记进 gaps（不编造）", any("count" in g for g in rec.gaps), rec.gaps)
chk("新动词已钩：RemoveImmune/GiveImmune/EndMatch/RemoveCardFromBoard/JSON_*/GotchaTriggered",
    all(v in EV.ALL_HOOKED for v in ("RemoveImmune", "GiveImmune", "EndMatch", "RemoveCardFromBoard",
                                     "JSON_SetBool", "JSON_GetInt", "GotchaTriggered")))

# ==============================================================================================
# 端到端：钩子链 → to_fx → sim.engine（0x04 对总部造成伤害 = 烈日）
# ==============================================================================================
Aair = unit("A", 1, ME, typ="fighter", atk=4, dfn=3)
Dhq = FakeCard("HQ", 90, OPP, card_type="location", defense=20, location="hq", keywords=[])
Sun = FakeCard("SUN", 50, ME, ovr=("OnAfterOtherCardAttacks",), card_type="order")
w = World([Aair, Dhq, Sun], {})
_orig_ex = w.ex


def _ex_with_records(km, c, hook, args, stream, *a, **k):
    r = _orig_ex(km, c, hook, args, stream, *a, **k)
    if hook == "OnAfterOtherCardAttacks" and T._ptr_of(c) == 50:      # 烈日：对敌方总部造成"伤害"点伤害
        r["records"] = [{"verb": "DamageCard", "args": [90, args["damageToDefender"], 1, False, False, False],
                         "tainted": False}]
    return r


w.ex = _ex_with_records
r = run(w, Aair, Dhq, cost=1)
fx = T.to_fx(r, _St(w.cards), my_side=ME, hq_own=(), hq_enemy=(90,))
chk("端到端：烈日(0x04) 对敌方总部造成'攻击者造成的伤害'(4) ⇒ fx.after.eff.damage_hq == 4",
    fx["buckets"]["after"]["eff"].get("damage_hq") == 4, fx)
sim_e = Sim({1: U(1, ME, "back", 4, 3, 3, "fighter", opc=1)}, {ME: 20, OPP: 20}, 5.0,
               attack_fx={(1, "hq"): fx}, my_side=ME)
s_after = sim_attack(sim_e, 1, None, hq=True)
chk("端到端：sim.engine 里打总部 = 攻击力 4 + 烈日 4 = 8", s_after.hq[OPP] == 12, s_after.hq)
chk("端到端：不带 fx 只有 4",
    sim_attack(Sim(dict(sim_e.units), {ME: 20, OPP: 20}, 5.0, my_side=ME), 1, None, hq=True).hq[OPP] == 16)

sr = mksim()
sr.units[1].attacks_left, sr.units[1].acted = 0, True
_apply_eff(sr, {"reset_ops": True}, 1)
chk("_apply_eff：reset_ops ⇒ 行动次数重置（可以再打一次）", sr.units[1].attacks_left == 1 and not sr.units[1].acted)
chk("新增动词：ResetUnitOperations/SpawnCardInDeckBySide 已钩", "ResetUnitOperations" in EV.ALL_HOOKED
    and "SpawnCardInDeckBySide" in EV.ALL_HOOKED)

# ==============================================================================================
# 多目标动词 / 对打 / EndMatch
# ==============================================================================================
UC = unit("UC", 6, OPP, atk=3, dfn=3)
st_m = _St([A1, D1, UB, UC, HQe2, HQl2])
hit_m = {"hook": "OnDestroyed", "bucket": "def_destroyed", "chance": [], "records": [
    {"verb": "DamageMultipleCards", "args": [[2, 6], 2, 1, None], "tainted": False},
    {"verb": "SuppressMultipleUnits", "args": [[6], 1], "tainted": False},
    {"verb": "MakeCardsFight", "args": [5, 6, 1], "tainted": False},
    {"verb": "DestroyMultipleCards", "args": [[2], 1], "tainted": False}]}
g, per = T.hit_effects(hit_m, st_m, my_side=ME)
chk("多目标：DamageMultipleCards([2,6],2) ⇒ 两张各 damage 2；SuppressMultipleUnits([6]) ⇒ 6 被压制；"
    "DestroyMultipleCards([2]) ⇒ 2 destroy",
    per[2].get("damage") == 2 and per[6].get("damage") is not None and per[6].get("pin") is True
    and per[2].get("destroy") is True, per)
chk("MakeCardsFight(UB 1攻, UC 3攻)：UB 吃 UC 的 3、UC 吃 UB 的 1（再叠上面的 2）",
    per[5].get("damage") == 3 and per[6].get("damage") == 2 + 1, per)
chk("被随机污染的多目标记录不进效果（不编造）",
    T.hit_effects({"records": [{"verb": "DamageMultipleCards", "args": [[2], 5, 1, None], "tainted": True}]},
                  st_m, my_side=ME)[1] == {})
r_em = EV.Recorder(0, 0, None)
r_em.records = [{"verb": "EndMatch", "args": [1, 0.0], "tainted": False}]
r_em2 = EV.Recorder(0, 0, None)
r_em2.records = [{"verb": "EndMatch", "args": [2, 0.0], "tainted": False}]
chk("to_effects：EndMatch(winnerSide) ⇒ end_match = 真实座位 ESide（与 my_side 无关）",
    EV.to_effects(r_em, 1).get("end_match") == ME and EV.to_effects(r_em, 2).get("end_match") == ME
    and EV.to_effects(r_em2, 1).get("end_match") == OPP)
se = mksim()
_apply_eff(se, {"end_match": ME}, None)
chk("sim.engine：end_match=local ⇒ 敌方总部归零 ⇒ evaluate = +lethal；enemy ⇒ -lethal",
    evaluate(se) == W["lethal"] and (_apply_eff(sk := mksim(), {"end_match": OPP}, None) or evaluate(sk) == -W["lethal"]))
chk("新增多目标动词已钩", all(v in EV.ALL_HOOKED for v in ("SuppressMultipleUnits", "MakeCardsFight", "RemoveMultipleCardsFromBoard",
                                                   "MoveMultipleCardsToTopOfOwnersDeck")))

print("\n%d 项失败" % bad)
sys.exit(1 if bad else 0)
