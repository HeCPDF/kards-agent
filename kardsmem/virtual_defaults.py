#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚函数（BlueprintNativeEvent）**没有覆写时**的原生默认实现 —— 出参默认值表。

依据：`reverse-data/reports/report/NATIVE-COVERAGE-1.60.md` §3（vtable 逐槽反编译）与 §6.1。
游戏语义：卡自己覆写了虚函数 ⇒ 跑**覆写字节码**；**没覆写** ⇒ 引擎调用 `UBaseCardObject`
上的原生默认实现（表见下）。我们的 VM 原来在第二种情况抛
`Unimplemented("原生函数 … 没有字节码")` —— 这就是 119 张卡（GetPlayFromHandDamage 127 次调用）
"一调就停"的来源。

★★ 使用纪律（报告 §3.1，违反会静默改变语义）：
  * 这张表**只能用在 VM 分发的最后一步**（"先找覆写、没有才用默认"）；
  * **绝不能**塞进 `cardnatives._TABLE` —— 那里排在覆写查找之前，会抢先把
    `CanPlayFromHand`（438 个覆写）、`GetPlayFromHandDamage`（129 个覆写）等**吃掉**。
  * 出参名取 SDK 的形参名（`kards_classes.hpp`），VM 用 `params_of(ufunc)` 按名字写回；
    名字对不上就**如实停**（`vm._apply_virtual_default` 会抛 Unimplemented），别猜。

值可以是常量，或 `callable(入参 dict) -> 值`（"回显输入"那一类默认实现）。
"""
from __future__ import annotations

# 默认实现**有副作用**（不在只读 VM 里执行）的虚函数：调用方应记录，不要执行。
NO_EXECUTE = frozenset(("OnSuppressed",))         # §3.1：默认实现是抑制通知（写状态）

VIRTUAL_DEFAULTS: dict = {
    # —— 判据类：清一色 "canIt = true"（Reason/reasonParam1/2 保持空串，由零值填）——
    "CanPlayFromHand":        {"canIt": True},
    "CanBeTargetted":         {"canIt": True},     # 0 个 BP 覆写；我们的近似版曾比游戏更严（§7#1）
    "CanOtherCardBeTargetted": {"canIt": True},    # 仅 no_3_commando 覆写
    "IsValidHandTarget":      {"isIt": True},
    "ShouldHighlightInHand":  {"shouldHighlight": False},
    "BlockCardFromBeingPlayedFromHand": {"bLocked": False},
    "FilterCardsToScry":      {"IsValid": True},
    # —— 伤害修正类：不覆写 = 不改动（回显 / 0）——
    "OnCardDealDamage_ModifyDamageDealt":
        {"newDamage": lambda a: a["Damage"]},
    "OnBeforeOtherCardGainDefense":
        {"newDefenseToAdd": lambda a: a["defenseToAdd"], "stopAction": False},
    "OnBeforeOtherCardGainDefenseAfterAdd":
        {"stopAction": False},
    "OnBeforeOtherCardDeploymentTrigger":
        {"cancelDeploymentEffect": False},
    "OnOtherCardDealDamageAddDamage":
        {"damageToAdd": 0, "reRunAtEnd": False},
    "OnOtherCardDealDamageAddDamageAfterCalc":
        {"damageToAdd": 0, "stopAdding": False},
    "OnDealDamageAddDamageAfterCalc":
        {"damageToAdd": 0},
    "OnOtherCardAttacks":
        {"stopAttack": False, "AttackedAndStopped": False},
    "OnBeforeOtherCardLoseMobilize":
        {"dontLoseMobilize": False},
    "OnOtherCardAttackSwitchTarget":
        {"newDefender": lambda a: a["oldDefender"]},
    # —— 数值 getter：不覆写 = 0 ——
    "GetPlayFromHandDamage":  {"Damage": 0},       # 129 个覆写都排在这条前面
    "GetBeforeAttackAttackBuff":  {"amount": 0},
    "GetBeforeAttackDefenseBuff": {"amount": 0},
    "GetPassiveDefenseBuff":      {"amount": 0},
    "GetBeforeAttackDamage":      {"amount": 0},
    # —— 选牌：默认给空表（其余出参由零值填）——
    "GetChooseSpawnCards":    {"Cards": []},
}


def selftest() -> int:
    """离线自检：表结构 + 与 1.60 SDK 形参名逐条对齐（名字错=静默写错出参，必须在离线抓出来）。"""
    bad = 0

    def chk(name, ok, extra=""):
        nonlocal bad
        bad += 0 if ok else 1
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))

    # SDK `kards_classes.hpp`（1.60.27292.launcher）里这些函数的**出参名**（按声明顺序）。
    sdk_outs = {
        "CanPlayFromHand": ["canIt", "Reason", "reasonParam1", "reasonParam2", "targetedCard"],
        "CanBeTargetted": ["canIt", "Reason", "reasonParam1", "reasonParam2"],
        "CanOtherCardBeTargetted": ["canIt", "Reason", "reasonParam1", "reasonParam2"],
        "IsValidHandTarget": ["isIt", "Reason"],
        "ShouldHighlightInHand": ["shouldHighlight"],
        "BlockCardFromBeingPlayedFromHand": ["bLocked", "Reason", "reasonParam1", "reasonParam2"],
        "FilterCardsToScry": ["IsValid"],
        "OnCardDealDamage_ModifyDamageDealt": ["newDamage"],
        "OnBeforeOtherCardGainDefense": ["newDefenseToAdd", "stopAction"],
        "OnBeforeOtherCardGainDefenseAfterAdd": ["stopAction"],
        "OnBeforeOtherCardDeploymentTrigger": ["cancelDeploymentEffect"],
        "OnOtherCardDealDamageAddDamage": ["damageToAdd", "reRunAtEnd"],
        "OnOtherCardDealDamageAddDamageAfterCalc": ["damageToAdd", "stopAdding"],
        "OnDealDamageAddDamageAfterCalc": ["damageToAdd"],
        "OnOtherCardAttacks": ["stopAttack", "AttackedAndStopped"],
        "OnBeforeOtherCardLoseMobilize": ["dontLoseMobilize"],
        "OnOtherCardAttackSwitchTarget": ["newDefender"],
        "GetPlayFromHandDamage": ["Damage"],
        "GetBeforeAttackAttackBuff": ["amount"],
        "GetBeforeAttackDefenseBuff": ["amount"],
        "GetPassiveDefenseBuff": ["amount"],
        "GetBeforeAttackDamage": ["amount"],
        "GetChooseSpawnCards": ["Cards", "MarkAsSeen", "keepOrder"],
    }
    chk("表里 23 个虚函数都有 SDK 签名", set(VIRTUAL_DEFAULTS) == set(sdk_outs),
        str(sorted(set(VIRTUAL_DEFAULTS) ^ set(sdk_outs))))
    for fn, spec in VIRTUAL_DEFAULTS.items():
        extra = [p for p in spec if p not in sdk_outs.get(fn, [])]
        chk("%s 的出参名在 SDK 里存在" % fn, not extra, str(extra))
    chk("OnSuppressed 只登记、不执行",
        "OnSuppressed" in NO_EXECUTE and "OnSuppressed" not in VIRTUAL_DEFAULTS)
    return bad


if __name__ == "__main__":
    import sys

    sys.exit(1 if selftest() else 0)
