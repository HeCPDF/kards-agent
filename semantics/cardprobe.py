#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""semantics.cardprobe —— 从**游戏进程里的蓝图字节码**读一张牌"打出前就能知道"的事。

每张牌是一个蓝图类，它把自己用到的钩子都注册成了自己的 `UFunction`
（`CanPlayFromHand` / `OnPlayedFromHand` / `OnHandTargetSelected` / …），钩子里调用
的就是游戏引擎的动词（`GetTargetedCard` / `DamageCard` / `selectTargetFromHand` …）。
所以不需要任何外部表：

    偏敌方还是偏我方   钩子里出现的动词集合（伤害/消灭/压制/撤退 vs 加攻防/给关键词…）
    伤害数值（尽力）    `DamageCard` 调用参数里的整数常量

⚠ **只当排序倾向，不当事实**（用户 2026-09-30 纠正）：动词出现在字节码里 ≠ 这一次会
  发生 —— 部署效果不总触发、没手牌时不会要求选手牌，条件都在分支里，静态分析答不了。
  "要不要指向 / 指谁合法 / 要不要再选手牌 / 单位能不能动" 一律问游戏自己。

只读：走 `kardsmem.kismet` 在**本进程**里反汇编，游戏进程里不执行任何东西。
结果按 **UClass 地址**缓存（同一张牌型的所有实例共用），一局只反汇编一次。

⚠ 这里的"动词→敌我倾向"只是**排序信号**（同项目红线：只挑不判）。谁能被指向、能不能
  打，权威永远是 `CanPlayFromHand` 的求值结果（`sess.can_play(card, target=t)`）。
"""
from __future__ import annotations

from typing import Optional

# 引擎自己的动词（不是逐张牌的表）：出现在钩子里时的倾向。
HOSTILE = frozenset((
    "DamageCard", "DamageMultipleCards", "DestroyCard", "DestroyMultipleCards",
    "PinUnit", "SuppressUnit", "MakeCardRetreat", "TakeControlOfEnemyUnit",
    "StealCardFromBoardToDeck", "RemoveCardFromBoard", "MoveCardToTopOfOwnersDeck",
    "MoveUnitFromBoardToOwnersHand", "MakeCardsFight", "TriggerDestruction",
    "DiscardCardFromHand", "DiscardRandomCardFromHand",
))
FRIENDLY = frozenset((
    "GiveBlitz", "GiveGuard", "GiveShock", "GiveFury", "GiveAmbush", "GiveSmokescreen",
    "GiveSalvage", "FullyHealCard", "MakeVeteran", "RemovePin", "AddAttackUntilEndOfTurn",
    "ChangeHeavyArmor", "MoveUnitFromSupportToFrontLine", "ChangeOperationCost",
))
# 两可（±都有）：ChangeAttack / ChangeDefense / ConvertCard / CustomAbilityAdd
TARGETED = "GetTargetedCard"
HAND_PICK = frozenset(("selectTargetFromHand",))
CARD_PICK = frozenset(("selectCardToDraw", "Forecast", "Scrying"))
HOOKS = ("CanPlayFromHand", "OnPlayedFromHand", "OnHandTargetSelected")
# 立刻生效的钩子 / 延迟到回合边界的钩子（其余 On* 钩子不管）
NOW_HOOKS = ("OnPlayedFromHand", "OnDeployed", "OnDeploy", "OnPlayed", "OnHandTargetSelected")
LATER_HOOK_MARKS = ("startofturn", "endofturn")

# 引擎动词 → 效果摘要键（数量取调用子树里的整数常量）。不是逐张牌的表，是游戏 API 的词汇。
GIVE_KW = {"GiveBlitz": "blitz", "GiveFury": "fury", "GiveShock": "shock", "GiveGuard": "guard",
           "GiveAmbush": "ambush", "GiveSmokescreen": "smokescreen"}
COUNT_VERBS = {"GiveKreditsBySide": "kredit", "DrawCardsFromDeckBySide": "draw",
               "DamageCard": "damage", "DamageMultipleCards": "damage_aoe",
               "LoseKreditSlot": "slot_loss", "SpawnCardOnBattlefield": "spawn",
               "SpawnCardInHandBySide": "gain_cards"}
FLAG_VERBS = {"DestroyCard": "destroy", "DestroyMultipleCards": "destroy",
              "PinUnit": "pin", "SuppressUnit": "pin", "RemovePin": "unpin",
              "MakeCardRetreat": "retreat", "MoveUnitFromBoardToOwnersHand": "retreat"}

_cache: dict = {}


def _fname(x) -> str:
    """`e.args['fn']` 可能是纯名字，也可能是 'Class:Func' / 'Function /Script/..Func'。"""
    s = str(x or "")
    for sep in (":", ".", "/", " "):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def _ints_under(e, acc: list) -> None:
    """一个调用表达式子树里的所有整数常量（含 IntZero/IntOne，带符号）。"""
    for k in e.kids:
        if k.op in ("IntConst", "IntConstByte"):
            v = k.args.get("v", k.args.get("value"))
            if isinstance(v, int):
                acc.append(v)
        elif k.op == "IntZero":
            acc.append(0)
        elif k.op == "IntOne":
            acc.append(1)
        _ints_under(k, acc)


def _walk(e, calls: list, ints: dict):
    fn = e.args.get("fn")
    if fn is not None:
        name = _fname(fn)
        calls.append(name)
        acc: list = []
        _ints_under(e, acc)
        if acc:
            ints.setdefault(name, []).extend(acc)
    for k in e.kids:
        _walk(k, calls, ints)


def probe_class(km, uclass: int) -> dict:
    """一个牌型（UClass）的画像。读不出来的字段是 None，不编。"""
    if uclass in _cache:
        return _cache[uclass]
    from kardsmem import kismet
    out = {"handlers": [], "verbs": set(), "damage": None, "ok": False}
    try:
        own = {f["name"]: f for f in kismet.functions(km, uclass) if not f["native"]}
        out["handlers"] = sorted(own)
        calls = []
        ints_all: dict = {}
        for h in sorted(own):
            # 分析：HOOKS 里的三个 + 所有 On* 钩子（立刻/延迟效果都在里面）
            if h not in HOOKS and not h.startswith("On"):
                continue
            f = own.get(h)
            code = kismet.script_of(km, f["addr"])
            if not code:
                continue
            es, _ = kismet.disasm(code, kismet.Resolve(km), stop_on_error=False)
            hc: list = []
            ints: dict = {}
            for e in es:
                _walk(e, hc, ints)
            out.setdefault("by_hook", {})[h] = sorted(set(hc))
            out.setdefault("ints_by_hook", {})[h] = ints
            calls += hc
            for k2, v2 in ints.items():
                ints_all.setdefault(k2, []).extend(v2)
        v = set(calls)
        out["verbs"] = v
        dm = [x for x in (ints_all.get("DamageCard") or ints_all.get("DamageMultipleCards") or [])
              if 0 < x < 50]
        out["damage"] = max(dm) if dm else None
        out["ok"] = True
    except Exception as ex:                                       # noqa: BLE001
        out["error"] = "%s: %s" % (type(ex).__name__, ex)
    _cache[uclass] = out
    return out


def profile(km, card) -> dict:
    """一张牌 → 画像。`card` 是快照里的 Card（带 `raw['ptr']`）。"""
    ptr = (getattr(card, "raw", None) or {}).get("ptr")
    if not ptr:
        return {"ok": False, "error": "no ptr"}
    from kardsmem.objects import ObjectArray
    uc = ObjectArray(km).class_of(ptr)
    if not uc:
        return {"ok": False, "error": "no uclass"}
    return probe_class(km, uc)


def effects_of(p: dict) -> dict:
    """画像 → 效果摘要（给 `boardeval` 模拟用；键的含义见 boardeval 的 EFFECT KEYS）。

    ★ 只是「钩子里调了哪些动词、带什么整数」，**不保证这一次会发生**（用户 2026-09-30
      纠正过：部署效果不总触发）。所以只用来估值；出不出、能不能，问游戏。
    """
    eff: dict = {}
    later: dict = {}
    for h, ints in (p.get("ints_by_hook") or {}).items():
        hl = h.lower()
        is_later = any(m in hl for m in LATER_HOOK_MARKS)
        is_now = (not is_later) and (h in NOW_HOOKS or h.startswith("OnDeploy"))
        target = later if is_later else eff if is_now else None
        if target is None:
            continue
        calls = set((p.get("by_hook") or {}).get(h, ()))
        for verb, key in COUNT_VERBS.items():
            if verb in calls:
                vals = [x for x in ints.get(verb, []) if -30 < x < 30 and x != 0]
                n = vals[0] if vals else 1
                target[key] = target.get(key, 0) + n
        for verb, key in FLAG_VERBS.items():
            if verb in calls:
                target[key] = True
        for verb, kw in GIVE_KW.items():
            if verb in calls:
                target.setdefault("give", []).append(kw)
    # 延迟钩子里的指挥点变化 ⇒ 下一友方回合的变化量；丢槽 ⇒ 槽变化
    if later.get("kredit"):
        eff["kredit_next"] = eff.get("kredit_next", 0) + later["kredit"]
    if later.get("slot_loss"):
        eff["slot"] = eff.get("slot", 0) - later["slot_loss"]
    if eff.get("slot_loss"):
        eff["slot"] = eff.get("slot", 0) - eff.pop("slot_loss")
    # 目标阵营倾向：只用于排序/候选，不是事实
    v = p.get("verbs") or set()
    hostile, friendly = bool(v & HOSTILE), bool(v & FRIENDLY)
    if hostile and not friendly:
        eff["target"] = "enemy"
    elif friendly and not hostile:
        eff["target"] = "friend"
    # 静态读到了伤害常量、但钩子分类没摊出 damage（例如钩子名不在 NOW_HOOKS）⇒ 补上
    if p.get("damage") and "damage" not in eff and "DamageCard" in v:
        eff["damage"] = p["damage"]
    return eff


def summarize(p: dict) -> dict:
    """画像 → **排序倾向**。

    ★ 2026-09-30 用户纠正：字节码里出现某个动词 ≠ 这一次会发生（部署效果不总触发、
      没手牌时不会要求选手牌，条件都在分支里）。所以这里**不再产出**"要不要指向/
      要不要选手牌"这类布尔事实 —— 那些一律问游戏（运行时旗标 `needs_hand_target`、
      `CanPlayFromHand` 带目标求值、出牌后的 `pending()`）。只留：
        side    钩子里的动词偏敌方还是偏我方（给候选排序用）
        damage  尽力读到的伤害常量（给击杀估值用）
        verbs   原样保留，调试看
    """
    v = p.get("verbs") or set()
    hostile, friendly = bool(v & HOSTILE), bool(v & FRIENDLY)
    side = "enemy" if hostile and not friendly else            "friend" if friendly and not hostile else None
    return {"known": bool(p.get("ok")), "side": side, "damage": p.get("damage"),
            "verbs": sorted(v), "effects": effects_of(p)}


if __name__ == "__main__":
    # 实机：列出手牌里每张牌的画像（需要游戏在对局里）。
    import argparse
    from agent import session as _session
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="含场上/对方已知牌")
    a = ap.parse_args()
    sess = _session.AgentSession(translate=False, warm=False)
    km, st = sess._kardsmem(), sess.snapshot()
    cards = st.cards if a.all else st.hand("local")
    for c in cards:
        s = summarize(profile(km, c))
        print("%-24s type=%-9s target=%-5s hand=%-5s pick=%-5s side=%-6s dmg=%s\n    %s"
              % (c.name, c.card_type, s["needs_target"], s["hand_target"], s["card_pick"],
                 s["side"], s["damage"], " ".join(s["verbs"])[:160]))
