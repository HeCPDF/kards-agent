#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""policy.boardeval —— 场面评分 + 动作模拟 + 回合内搜索（纯函数，不碰游戏）。

想法（用户 2026-09-30）：给场面打分，dry-run 每个候选动作之后的结果取分数差；
花费要纳入（分数/花费，或把花费当成场面的一部分）；还要能处理这些牌：

    生产   0K，+1 指挥点（存费）          → 资源牌：只在「能解锁后续动作」时才值得打
    计划   2K，抽一张                      → 把用不完的指挥点换成手牌：排在所有真收益之后
    爆破   0K，对一个单位 1 点伤害         → 补刀/组合：0 费不等于该立刻打
    霍尔姆/持续弹幕  下一友方回合失去指挥点 → 延迟效果：下回合预算变少
    贴闪击/奋战/守护/烟幕/冲击…            → 关键词 = 对规则的修改，价值由模拟出来
    解除压制                                → 压制 = 单位不能行动；解除 = 行动价值恢复
    往手牌里加牌的牌                        → 价值 = 被加入那张牌的持有价值

所以这里不再是「给每种牌写一条估值规则」，而是三层：

    状态 Sim   单位（含规则状态）+ 总部 + 指挥点/槽/待生效 + 手牌（含效果摘要）
    转移 apply 每种动作怎么改状态（攻击/部署/上线/出指令，指令按效果摘要改）
    价值 V(s)  单位「身体价值 + 行动价值」+ 总部 + 手牌持有价值 + 指挥点/槽/延迟效果
    搜索      深度 2~3 的束搜索：选「一串动作之后 V 最高」序列的第一步

规则来自 `KARDS-RULES-ENCYCLOPEDIA.md`（闪击/奋战/烟幕/伏击/冲击/重甲/坦克…），效果摘要
（伤害数值/给指挥点/抽牌/给关键词/压制…）来自 `semantics/cardprobe.py` 读到的字节码。

诚实的限制
----------
* 字节码里出现某个动词 ≠ 这一次会发生（用户 2026-09-30 纠正过）。效果摘要只用来**估值**，
  不用来判定能不能出；合法性一律问游戏（`sess.can_*`）。只挑不判。
* 权重都是**拍的初值**；`V(s)` 的形式（`w·φ(s)`）留出了后续用对局日志回归校准的口子。
* 对手手牌/牌库不可见 ⇒ 这里不做对手回合的对抗搜索，只算「我方这一回合」。
"""
from __future__ import annotations

from typing import Iterable, Optional

# ★ 2026-10-03：本文件现在只是**兼容门面** + 离线自检。真实现已按分层迁出：
#     sim/state.py     状态（U/H/Sim/常量）            sim/engine.py   规则引擎（转移/效果结算/钩子后果）
#     sim/adapter.py   快照→Sim（from_cards）          evaluation/value.py  价值（W/evaluate/delta）
#     policy/search.py 动作生成/选择路径展开/束搜索
#   旧名字（含下划线的内部名）原样从这里能取到，`player.rule` 与各 `test_*.py` 不用改。
import importlib

_value = importlib.import_module("evaluation.value")      # 注意：`evaluation.value` 属性被包里同名函数 `value` 遮住，别用 `import a.b as c`
import policy.search as _search
import sim.adapter as _adapter
import sim.engine as _engine
import sim.state as _state

for _m in (_state, _value, _engine, _search, _adapter):
    for _n in dir(_m):
        if not _n.startswith("__"):
            globals().setdefault(_n, getattr(_m, _n))

_engine.WEIGHTS = W                    # 规则里用到的 2 个常量（hand_cap/draw_v）与评估侧同一个 dict
_engine.set_unit_valuer(unit_value)          # 选目标启发式用（过渡口，M3 移到策略层后删）


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def B_equal(a, b) -> bool:
    return (evaluate(a) == evaluate(b) and a.gaps == b.gaps
            and {k: (u.atk, u.dfn) for k, u in a.units.items()} == {k: (u.atk, u.dfn) for k, u in b.units.items()})


def selftest() -> int:
    fails = 0

    def chk(name, ok, extra=""):
        nonlocal fails
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        fails += 0 if ok else 1

    def mk(units=(), hand=(), kred=5.0, hq=(20, 20), **kw):
        return Sim({u.id: u for u in units}, {LOCAL: hq[0], ENEMY: hq[1]}, kred,
                   {c.id: c for c in hand}, **kw)

    # ---- 基础：攻击/部署/上线 ----
    base = mk([U(1, LOCAL, "back", 4, 3, 3, "artillery"), U(2, LOCAL, "back", 3, 3, 3, "infantry"),
               U(5, ENEMY, "frontline", 3, 3, 3, "infantry"), U(6, ENEMY, "frontline", 1, 1, 1, "infantry")])
    d_kill = delta(base, sim_attack(base, 1, 5))
    d_trade = delta(base, sim_attack(base, 2, 5))
    d_chip = delta(base, sim_attack(base, 1, 6))
    chk("炮兵杀敌 delta>0（免反击）", d_kill > 0, "%.2f" % d_kill)
    chk("平换 delta 明显小于白赚", d_trade < d_kill, "%.2f < %.2f" % (d_trade, d_kill))
    chk("杀高价值 > 杀低价值", d_kill > d_chip)
    chk("打总部有正收益", delta(base, sim_attack(base, 1, None, hq=True)) > 0)
    lethal = mk([U(1, LOCAL, "frontline", 5, 3, 3, "artillery")], hq=(20, 5))
    chk("致命一击 delta 极大", delta(lethal, sim_attack(lethal, 1, None, hq=True)) > 500)
    chk("模拟不改入参", base.units[5].dfn == 3 and base.hq[ENEMY] == 20)

    # ---- 抽牌：按"盘外再算一遍"——牌库是真实状态；空库抽 = 游戏自己的疲劳规则（用户 2026-10-02）----
    top = H(7, "TOP CARD", 2, "order", eff={"_hold": 5.0})
    dt = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=[7], deck_cards={7: top})
    _res_eff(dt, {"draw": 1})
    chk("牌库顶已知：抽到的是那张牌本身（_hold=5.0）",
        len(dt.hand) == 1 and list(dt.hand.values())[0].name == "TOP CARD"
        and hold_value(list(dt.hand.values())[0]) == 5.0 and dt.hq[LOCAL] == 20)
    dt2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=[7], deck_cards={7: top},
             fatigue=0)
    _res_eff(dt2, {"draw": 2})                     # 顶牌只有 1 张 ⇒ 第 2 抽 = 空库疲劳
    chk("牌库抽空 ⇒ 第 2 抽吃疲劳（伤害=当前计数 0 ⇒ 不掉血，计数→1）",
        dt2.hq[LOCAL] == 20 and dt2.fatigue == 1 and len(dt2.hand) == 1,
        "hq=%s fatigue=%s hand=%d" % (dt2.hq[LOCAL], dt2.fatigue, len(dt2.hand)))
    fat3 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=[], fatigue=3)
    _res_eff(fat3, {"draw": 2})                    # 空库连抽 2：先 3 伤（计数→4），再 4 伤（计数→5）
    chk("空库连抽：疲劳伤害随计数递增（3 → 4）",
        fat3.hq[LOCAL] == 20 - 3 - 4 and fat3.fatigue == 5 and not fat3.hand,
        "hq=%s fatigue=%s" % (fat3.hq[LOCAL], fat3.fatigue))
    unk = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    _res_eff(unk, {"draw": 2})                     # 完全不知道牌库 ⇒ 两张匿名牌
    chk("牌库未知（≠空）：抽 2 张 ⇒ 两张匿名牌（draw_v），不计疲劳",
        unk.hq[LOCAL] == 20 and unk.fatigue == 0 and len(unk.hand) == 2)
    # 洗牌：按游戏自己的 Array_ShuffleFromStream（Stream.shuffle），且换种子答案要变
    from kardsmem.rng import Stream
    dk = [H(i, "C%d" % i, 1, "order", eff={"_hold": 1.0}) for i in range(8)]
    dc = {i: dk[i] for i in range(8)}
    sh1 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=list(range(8)),
             deck_cards=dc, rng_seed=12345)
    _apply_eff(sh1, {"deck_shuffle": "local"}, None)
    want1 = Stream(12345).shuffle(list(range(8)))
    chk("洗牌 = Stream.shuffle（与独立复算一致）",
        sh1.deck == want1, str(sh1.deck))
    sh2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=list(range(8)),
             deck_cards=dc, rng_seed=999)
    _apply_eff(sh2, {"deck_shuffle": "local"}, None)
    chk("换种子 ⇒ 洗出的顺序跟着变", sh2.deck != sh1.deck)
    # 抵抗：抽到时效果（-1 指挥点 / 自己总部 1 伤 / 再抽 1 张）⇒ 连抽靠队列递归
    res = H(9, "RESISTANCE", 0, "order",
            eff={"_hold": 0.0, "_on_draw": {"heal_hq": -1, "kredit": -1, "draw": 1}})
    nextc = H(10, "NEXT", 2, "order", eff={"_hold": 1.0})
    cd = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], kred=5.0,
            deck=[res.id, nextc.id], deck_cards={res.id: res, nextc.id: nextc})
    _res_eff(cd, {"draw": 1})
    chk("抵抗链：抽 1 张开出抵抗 ⇒ 它自己再抽 1 张（连抽），并结算 -1 费/自己总部 -1",
        len(cd.hand) == 2 and cd.kredits == 4.0 and cd.hq[LOCAL] == 19 and cd.draws_done == 2,
        "hand=%d kred=%s hq=%s draws=%d" % (len(cd.hand), cd.kredits, cd.hq[LOCAL], cd.draws_done))

    # ---- 收缴（SalvageMultipleUnits）：1/1、费用≤3 的复制进手牌；手牌满则跳过 ----
    tpl = H(21, "BIG UNIT", 7, "tank", 5, 6, ("guard", "blitz"), {})
    sv = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], card_templates={21: tpl})
    _apply_eff(sv, {"salvage_ids": [21]}, None)
    got_h = list(sv.hand.values())
    chk("收缴：进手牌 1 张，攻/防=1，费用 min(7,3)=3，关键词保留",
        len(got_h) == 1 and got_h[0].atk == 1 and got_h[0].dfn == 1
        and got_h[0].cost == 3 and got_h[0].kw == frozenset(("guard", "blitz")),
        str(got_h[0].kw) if got_h else "empty")
    full = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")],
              hand=[H(100 + i, "F%d" % i, 1, "order", eff={"_hold": 0.4})
                    for i in range(W["hand_cap"])],
              card_templates={21: tpl})
    _apply_eff(full, {"salvage_ids": [21]}, None)
    chk("收缴：手牌满（9/9）⇒ 不创建（BP IsLocationFull 分支）", len(full.hand) == W["hand_cap"])

    # ---- 协力（Bond）未满足：打出时己方总部吃一次疲劳伤害（BP ApplyFatigueDamage）----
    bd = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], fatigue=2)
    _apply_eff(bd, {"bond_fatigue": True}, None)
    chk("协力未满足：伤害 = 当前疲劳计数 2，计数 → 3",
        bd.hq[LOCAL] == 18 and bd.fatigue == 3,
        "hq=%s fatigue=%s" % (bd.hq[LOCAL], bd.fatigue))

    # ---- 压制回合数（ChangedPinnedTurns）：正 = 压住；负 = 解除 ----
    pin_u = U(5, ENEMY, "frontline", 3, 3, 3, "infantry")
    pn = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry"), pin_u])
    _apply_eff(pn, {"pin_turns": 2}, 5)
    chk("ChangedPinnedTurns(+2) ⇒ 目标被压制", pn.units[5].pinned)
    _apply_eff(pn, {"pin_turns": -1}, 5)
    chk("ChangedPinnedTurns(-1) ⇒ 解除压制", not pn.units[5].pinned)

    # ---- StealCardFromBoardToDeck：目标离场 + 洗进己方牌库 ----
    sd_tpl = H(22, "STOLEN", 4, "tank", 4, 4, (), {})
    stl = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry"), U(22, ENEMY, "frontline", 4, 4, 4, "tank")],
             deck=[7, 8], deck_cards={7: H(7, "D7", 1, "order"), 8: H(8, "D8", 1, "order")},
             card_templates={22: sd_tpl}, rng_seed=7)
    _apply_eff(stl, {"steal_to_deck": LOCAL, "deck_shuffle": LOCAL}, 22)
    chk("StealCardFromBoardToDeck ⇒ 目标离场、牌库 +1 张（22 在里面）",
        22 not in stl.units and 22 in stl.deck and len(stl.deck) == 3, str(stl.deck))
    chk("StealCardFromBoardToDeck ⇒ 洗过牌（顺序 = Stream.shuffle）",
        stl.deck == list(Stream(7).shuffle([7, 8, 22])), str(stl.deck))

    # ---- SpawnCardInDeckBySide：n 张新牌进牌库（顶部/底部/随机位），每张都消耗一次随机 ----
    sp1 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")],
             deck=[7], deck_cards={7: H(7, "D7", 1, "order")}, rng_seed=11)
    _apply_eff(sp1, {"deck_add": 2, "deck_add_side": LOCAL, "deck_add_bottom": False,
                     "deck_add_shuffle": False}, None)
    chk("塞牌（顶部）：牌库 3 张，两张新牌在 index 0/1",
        len(sp1.deck) == 3 and sp1.deck[0] < 0 and sp1.deck[1] < 0 and sp1.deck[2] == 7,
        str(sp1.deck))
    cmp11 = Stream(11)                              # 独立复算：第 1 张时牌库长 1、第 2 张时长 2
    cmp11.wrapper_int(0, 1)
    cmp11.wrapper_int(0, 2)
    chk("塞牌：每张消耗一次 RandomIntFromRangeWithStream（与独立复算的步数一致）",
        sp1.rng.draws == cmp11.draws, "%s vs %s" % (sp1.rng.draws, cmp11.draws))
    sp2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")],
             deck=[7, 8], deck_cards={7: H(7, "D7", 1, "order"), 8: H(8, "D8", 1, "order")},
             rng_seed=5)
    _apply_eff(sp2, {"deck_add": 1, "deck_add_side": LOCAL, "deck_add_bottom": True,
                     "deck_add_shuffle": True}, None)
    want_sp = Stream(5)
    want_sp.wrapper_int(0, 2)                       # BP 循环里那次无条件抽取
    want_deck = list(want_sp.shuffle([7, 8, -3001]))
    chk("塞牌（底部+shuffle）：随机消耗与洗牌顺序都和独立复算一致",
        sp2.deck == want_deck, "%s vs %s" % (sp2.deck, want_deck))

    # ---- MoveUnitFromSupportToFrontLine：推上前线（不花行动费）；前线被对方占 ⇒ 不动 ----
    mv = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    _apply_eff(mv, {"move_front": True}, 1)
    chk("推上前线：后排单位 → 前线，且不扣行动费",
        mv.units[1].row == "frontline" and mv.kredits == 5.0, str(mv.units[1].row))
    mv2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], front_owner=ENEMY)
    _apply_eff(mv2, {"move_front": True}, 1)
    chk("推上前线：前线归对方 ⇒ BP 拒绝，单位留在后排", mv2.units[1].row == "back")

    # ---- unit_eff（情报触发 OnIntelTriggered 的逐牌效果）----
    ie = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry"),
             U(5, ENEMY, "frontline", 3, 3, 3, "infantry")])
    _apply_eff(ie, {"unit_eff": [(1, {"buff": [2, 2]})]}, None)
    chk("unit_eff：buff 加到触发卡自己（+2/+2）",
        ie.units[1].atk == 4 and ie.units[1].dfn == 4,
        "%s/%s" % (ie.units[1].atk, ie.units[1].dfn))
    _apply_eff(ie, {"unit_eff": [(1, {"damage": 3})]}, None)
    chk("unit_eff：damage 近似打敌方最值钱的单位（3/3 被打死）", 5 not in ie.units)
    ie2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], deck=[7],
             deck_cards={7: H(7, "D7", 1, "order")})
    _apply_eff(ie2, {"unit_eff": [(1, {"draw": 1})]}, None)
    chk("unit_eff：draw 走全局（抽 1 张进手牌）", len(ie2.hand) == 1, str(len(ie2.hand)))
    dh = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], hq=(20, 20))
    _apply_eff(dh, {"damage_hq": 3}, None)           # 无 target 标记也应结算（7th Scottish 那类）
    chk("damage_hq（自动打 HQ，无 target 标记）⇒ 敌方总部 -3", dh.hq[ENEMY] == 17,
        str(dh.hq[ENEMY]))

    # ---- 死亡触发（death_fx = OnDestroyed 预计算）----
    dfx = mk([U(1, LOCAL, "frontline", 2, 1, 2, "infantry"),
              U(5, ENEMY, "frontline", 3, 3, 3, "infantry")],
             deck=[7], deck_cards={7: H(7, "D7", 1, "order")},
             death_fx={1: {"draw": 1}})
    dfx_after = sim_attack(dfx, 5, 1)                # 敌方 3/3 打死我方 2/1
    chk("death_fx：单位被打死时触发 OnDestroyed 效果（draw 1）",
        1 not in dfx_after.units and len(dfx_after.hand) == 1,
        "units=%s hand=%d" % (sorted(dfx_after.units), len(dfx_after.hand)))

    # ---- 指挥点/槽夹取（EVAL-NATIVES-AND-HOOKS §8-1）----
    km1 = mk([], kred=23.0, kredit_max=24.0)
    _res_eff(km1, {"kredit": 3})
    chk("指挥点夹取：23 + 3 → 24（max=24）", km1.kredits == 24.0, str(km1.kredits))
    km2 = mk([], kred=23.0, kredit_max=10.0)
    _res_eff(km2, {"kredit": 3})
    chk("指挥点夹取：max=10 时 23 + 3 → 10", km2.kredits == 10.0, str(km2.kredits))
    km3 = mk([], kred=5.0)
    _res_eff(km3, {"kredit": -30})
    chk("指挥点夹取：−30 → 0", km3.kredits == 0.0, str(km3.kredits))
    km4 = mk([], slots=20.0)
    _res_eff(km4, {"slot": 10})
    chk("槽夹取：20 + 10 → 24（max=24）", km4.slots == 24.0, str(km4.slots))

    # ---- §15.5 群体键消费（*_aoe_ids）----
    ao = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry"),
             U(5, ENEMY, "frontline", 2, 2, 2, "infantry"),
             U(6, ENEMY, "frontline", 2, 2, 2, "infantry"),
             U(7, ENEMY, "frontline", 2, 2, 2, "infantry")],
            death_fx={5: {"draw": 1}}, deck=[9],
            deck_cards={9: H(9, "D9", 1, "order")})
    _apply_eff(ao, {"destroy_aoe_ids": [5, 6]}, None)
    chk("destroy_aoe_ids：摧毁 2 个 ⇒ 剩 1 个 + death_fx 触发（draw 1）",
        sorted(ao.units) == [1, 7] and len(ao.hand) == 1,
        "units=%s hand=%d" % (sorted(ao.units), len(ao.hand)))
    rm = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry"),
             U(6, ENEMY, "frontline", 2, 2, 2, "infantry")],
            death_fx={5: {"draw": 1}})
    _apply_eff(rm, {"remove_aoe_ids": [5]}, None)
    chk("remove_aoe_ids：离场但不算摧毁（death_fx 不触发、无抽牌）",
        sorted(rm.units) == [6] and not rm.hand, str(sorted(rm.units)))
    pn = mk([U(5, ENEMY, "frontline", 3, 3, 3, "infantry")])
    _apply_eff(pn, {"pin_aoe_ids": [5]}, None)
    chk("pin_aoe_ids：被压制 ⇒ can_act False", not pn.units[5].can_act())
    dm = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry"),
             U(6, ENEMY, "frontline", 3, 3, 3, "infantry")])
    _apply_eff(dm, {"damage_aoe": 2, "damage_aoe_ids": [5, 6]}, None)
    chk("damage_aoe_ids：2/2 吃 2 阵亡、3/3 吃 2 剩 1",
        5 not in dm.units and dm.units[6].dfn == 1,
        "units=%s dfn6=%s" % (sorted(dm.units), getattr(dm.units.get(6), "dfn", None)))
    td = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")],
            deck=[9], deck_cards={9: H(9, "D9", 1, "order")})
    _apply_eff(td, {"to_deck_aoe_ids": [1], "to_deck_position": 0}, None)
    chk("to_deck_aoe_ids：己方单位回牌库顶（index 0）且离场",
        td.deck[0] == 1 and 1 not in td.units, str(td.deck))

    # ---- §15.5：heal / damage_own_hq / opp_cards / opp_spawn ----
    hl = mk([U(1, LOCAL, "back", 2, 3, 2, "infantry", mdef=5)])
    _apply_eff(hl, {"heal": True}, 1)
    chk("heal：FullyHealCard ⇒ dfn = mdef（3 → 5）", hl.units[1].dfn == 5,
        str(hl.units[1].dfn))
    oh = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")], hq=(20, 20))
    _apply_eff(oh, {"damage_own_hq": 2}, None)
    chk("damage_own_hq：己方 HQ 20 → 18", oh.hq[LOCAL] == 18, str(oh.hq[LOCAL]))
    oc = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    s0 = evaluate(oc)
    _res_eff(oc, {"opp_draw": 2})
    chk("opp_cards：对手抽 2 ⇒ 我方评估下降",
        oc.opp_cards == 2 and evaluate(oc) < s0, "%s -> %s" % (s0, evaluate(oc)))
    os_ = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    n_foes = sum(1 for u in os_.units.values() if u.side == ENEMY)
    _res_eff(os_, {"opp_spawn": 1})
    chk("opp_spawn：敌方场上 +1 个单位",
        sum(1 for u in os_.units.values() if u.side == ENEMY) == n_foes + 1)
    stl = mk([U(5, ENEMY, "frontline", 3, 3, 3, "infantry")])
    _apply_eff(stl, {"steal": True}, 5)
    chk("steal：敌方单位归我方，且**当回合即可行动**（BP movementLeft=1/attackLeft=1）；前线唯一一张留在前线",
        stl.units[5].side == LOCAL and stl.units[5].can_act() and stl.units[5].row == "frontline")
    stl2 = mk([U(5, ENEMY, "frontline", 3, 3, 3, "infantry", ("fury",)), U(6, LOCAL, "frontline", 1, 1, 1, "infantry")])
    _apply_eff(stl2, {"steal": True}, 5)
    chk("steal：前线 >1 张 ⇒ 去我方后排；有 fury ⇒ attackLeft=2",
        stl2.units[5].row == "back" and stl2.units[5].attacks_left == 2)
    hl0 = mk([U(7, LOCAL, "back", 2, 3, 2, "infantry", mdef=5)])
    hl0.units[7].dfn = 0
    _apply_eff(hl0, {"heal": True}, 7)
    chk("heal：总防御=0 ⇒ 门槛不过，不动（BP: totalDefense>0）", hl0.units[7].dfn == 0)
    vt = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    _apply_eff(vt, {"veteran": True}, 1)
    chk("veteran：无数值 ⇒ 只打标记 + 记缺口", "veteran" in vt.units[1].kw and any("veteran" in g for g in vt.gaps))
    vt2 = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry", mdef=2)])
    _apply_eff(vt2, {"veteran": {"atk_to": 4, "atk_from": 2, "dfn_to": 5, "opc_to": 1, "opc_from": 1,
                                  "armor_to": 1, "armor_buff": 0,
                                  "kw": {"fury": True, "guard": False, "blitz": False}}}, 1)
    u2 = vt2.units[1]
    chk("veteran：按 BP 设为 `_vet` 绝对值（攻 +2、防=最大防=5、重甲 1、获得 fury ⇒ attackLeft 2）",
        (u2.atk, u2.dfn, u2.mdef, u2.armor, "fury" in u2.kw, u2.attacks_left) == (4, 5.0, 5.0, 1, True, 2),
        str((u2.atk, u2.dfn, u2.mdef, u2.armor, sorted(u2.kw), u2.attacks_left)))

    cvs = mk([U(8, ENEMY, "frontline", 5, 5, 4, "tank", ("guard",), armor=1)])
    cvs.units[8].atk_turn = 0
    _apply_eff(cvs, {"convert": {"ids": [8], "name": "card_unit_x", "atk": 1, "dfn": 1, "cost": 1, "opc": 1,
                                  "armor": 0, "typ": "infantry", "kw": []}}, None)
    nu = [u for u in cvs.units.values() if u.side == ENEMY]
    chk("convert：旧牌离场、同位置换成出厂数值的新牌（buff/重甲/关键词不继承），新牌召唤失调",
        8 not in cvs.units and len(nu) == 1 and (nu[0].atk, nu[0].dfn, nu[0].armor, nu[0].row, nu[0].sick)
        == (1, 1, 0, "frontline", True) and not nu[0].kw)
    cvb = mk([U(8, ENEMY, "frontline", 5, 5, 4, "tank")])
    _apply_eff(cvb, {"convert": True}, None)
    chk("convert：无目标数值 ⇒ 不结算 + 缺口", 8 in cvb.units and any("convert" in g for g in cvb.gaps))

    # ---- RevealCard / AddDefenseToMultipleCards / AddKreditsTax / AddGameplayRestriction ----
    cov = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry", ("covert",))])
    _apply_eff(cov, {"reveal": True}, 5)
    chk("RevealCard ⇒ 去掉隐蔽关键词", "covert" not in cov.units[5].kw)

    dfn = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry"),
              U(5, ENEMY, "frontline", 2, 2, 2, "infantry")])
    _apply_eff(dfn, {"defense_aoe": 2, "defense_aoe_ids": [1, 5]}, None)
    chk("群体加防 +2 ⇒ 两张各 +2 防",
        dfn.units[1].dfn == 4 and dfn.units[5].dfn == 4,
        "%s/%s" % (dfn.units[1].dfn, dfn.units[5].dfn))
    dfn2 = mk([U(5, ENEMY, "frontline", 2, 1, 2, "infantry")])
    _apply_eff(dfn2, {"defense_aoe": -2, "defense_aoe_ids": [5]}, None)
    chk("群体加防 -2 打到 ≤0 ⇒ 单位被摧毁（BP 末尾 DestroyMultipleCards）", 5 not in dfn2.units)

    tx = mk([U(1, LOCAL, "back", 2, 2, 2, "infantry")])
    base_v = unit_value(tx.units[1])
    _apply_eff(tx, {"kredits_tax": 3}, 1)
    chk("指向税：tax=3 且 unit_value 变高（敌人指向它要多花钱）",
        tx.units[1].tax == 3 and unit_value(tx.units[1]) > base_v,
        "%s → %s" % (base_v, unit_value(tx.units[1])))

    rst = mk([U(1, LOCAL, "frontline", 3, 3, 3, "infantry"),
              U(5, ENEMY, "frontline", 3, 3, 3, "infantry")],
             hand=[H(30, "ORDER", 1, "order", eff={"_hold": 1.0}),
                   H(31, "UNIT", 1, "infantry", 2, 2)])
    kinds = lambda s: {a.kind for a in gen_actions(s)}          # noqa: E731
    chk("限制前：指令/部署/攻击都在", {"order", "deploy", "attack"} <= kinds(rst), str(kinds(rst)))
    rst.restrictions.append({"side": LOCAL, "type": 2, "turns": 2})
    chk("cannotPlayOrders ⇒ 指令不再生成（部署/攻击仍在）",
        "order" not in kinds(rst) and {"deploy", "attack"} <= kinds(rst), str(kinds(rst)))
    rst.restrictions.append({"side": LOCAL, "type": 3, "turns": 2})
    chk("cannotDeployUnits ⇒ 部署不再生成", "deploy" not in kinds(rst), str(kinds(rst)))
    rst.restrictions.append({"side": LOCAL, "type": 4, "turns": 2})
    chk("cannotAttackWithGroundUnits ⇒ 步兵攻击不再生成", "attack" not in kinds(rst), str(kinds(rst)))
    _apply_eff(rst, {"restriction_remove": [{"side": LOCAL, "type": 2, "turns": 0}]}, None)
    chk("RemoveGameplayRestriction(type=2) ⇒ 指令动作恢复", "order" in kinds(rst), str(kinds(rst)))

    # ---- 规则：伏击 / 冲击 / 重甲 / 烟幕 / 守护 / 拦截 ----
    amb = mk([U(1, LOCAL, "back", 3, 2, 3, "infantry"), U(5, ENEMY, "frontline", 3, 3, 3, "infantry", ("ambush",))])
    a2 = sim_attack(amb, 1, 5)
    chk("伏击：先手反击杀死攻击方，目标毫发无伤", 1 not in a2.units and a2.units[5].dfn == 3)
    shk = mk([U(1, LOCAL, "back", 3, 2, 3, "infantry", ("shock",)), U(5, ENEMY, "frontline", 3, 5, 3, "infantry")])
    s2 = sim_attack(shk, 1, 5)
    chk("冲击：首击无反击并移除冲击", s2.units[1].dfn == 2 and "shock" not in s2.units[1].kw)
    arm = mk([U(1, LOCAL, "back", 3, 3, 3, "infantry"), U(5, ENEMY, "frontline", 3, 5, 3, "tank", (), armor=2)])
    chk("重甲：交战伤害减少", sim_attack(arm, 1, 5).units[5].dfn == 4)
    smk = mk([U(1, LOCAL, "back", 3, 3, 3, "artillery"), U(5, ENEMY, "frontline", 3, 3, 3, "infantry", ("smokescreen",))])
    chk("烟幕：不能被攻击", not any(a.dst == 5 for a in gen_actions(smk)))
    gh = mk([U(1, LOCAL, "frontline", 3, 3, 3, "infantry")]); gh.hq_guarded = True
    chk("总部被守护：非炮兵打不到", not any(a.dst == "hq" for a in gen_actions(gh)))
    ic = mk([U(1, LOCAL, "back", 3, 3, 3, "bomber"), U(5, ENEMY, "back", 2, 2, 2, "fighter")])
    chk("敌方支援线有战斗机：轰炸机打不到支援线/总部", not gen_actions(ic))
    gr = mk([U(1, LOCAL, "back", 3, 3, 3, "infantry"), U(5, ENEMY, "back", 2, 2, 2, "infantry")])
    chk("步兵在支援线打不到敌方支援线", not any(a.kind == "attack" for a in gen_actions(gr)))

    # ---- 关键词是规则修改：贴闪击 / 奋战 / 解除压制 ----
    sick = U(1, LOCAL, "back", 4, 3, 3, "artillery", sick=True)
    blitz = H(30, "GIVE BLITZ", 1, "order", eff={"give": ["blitz"], "target": "friend"})
    bs = mk([sick, U(5, ENEMY, "frontline", 2, 2, 2, "infantry")], [blitz])
    seqs = search(bs, depth=2)
    best = seqs[0][1] if seqs else []
    chk("贴闪击 → 搜索出「先贴闪击、再攻击」", [a.kind for a in best][:2] == ["order", "attack"],
        str(best))
    fu = U(1, LOCAL, "back", 4, 4, 3, "artillery")
    fury = H(31, "GIVE FURY", 1, "order", eff={"give": ["fury"], "target": "friend"})
    fs = mk([fu, U(5, ENEMY, "frontline", 2, 2, 2, "infantry"), U(6, ENEMY, "frontline", 2, 2, 2, "infantry")],
            [fury])
    fbest = search(fs, depth=3)[0][1]
    chk("贴奋战 → 一个单位打两次", [a.kind for a in fbest].count("attack") == 2, str(fbest))
    pinned = U(1, LOCAL, "back", 4, 4, 3, "artillery", pinned=True)
    unp = H(32, "UNPIN", 1, "order", eff={"unpin": True, "target": "friend"})
    ps = mk([pinned, U(5, ENEMY, "frontline", 2, 2, 2, "infantry")], [unp])
    pbest = search(ps, depth=2)[0][1]
    chk("解除压制 → 搜索出「先解压、再攻击」", [a.kind for a in pbest][:2] == ["order", "attack"], str(pbest))
    chk("被压制的单位本身不产生攻击动作",
        not any(a.kind == "attack" for a in gen_actions(mk([pinned, U(5, ENEMY, 'frontline', 2, 2, 2, 'infantry')]))))

    # ---- 资源牌：生产 / 计划 / 爆破 / 延迟扣费 ----
    prod = H(40, "PRODUCTION", 0, "order", eff={"kredit": 1})
    exp = H(41, "EXPENSIVE", 4, "infantry", 5, 5)                 # 4 费 5/5：差 1 点买不起
    s_short = mk([], [prod, exp], kred=3)
    b = search(s_short, depth=2)[0][1]
    chk("生产：预算差 1 点时先打生产、再部署", [a.kind for a in b][:2] == ["order", "deploy"], str(b))
    s_rich = mk([], [prod, H(42, "CHEAP", 2, "infantry", 2, 2)], kred=6)
    r = search(s_rich, depth=2)
    chk("生产：钱够用时不打生产（留着存费）", r[0][1][0].kind == "deploy", str(r[0][1]))
    plan = H(43, "PLAN", 2, "order", eff={"draw": 1})
    s_left = mk([], [plan], kred=2)
    chk("计划：闲置指挥点换一张牌（正收益）", search(s_left, depth=1)[0][0] > 0)
    s_both = mk([], [plan, H(44, "UNIT2", 2, "infantry", 3, 3)], kred=2)
    fb = search(s_both, depth=1)[0][1]
    chk("计划：有更好的用途时让位给部署", fb[0].kind == "deploy", str(fb))
    blast = H(45, "BLAST", 0, "order", eff={"damage": 1, "target": "enemy"})
    s_big = mk([U(5, ENEMY, "frontline", 5, 6, 5, "tank")], [blast], kred=2)
    chk("爆破：对大单位 1 点伤害不值得立刻打", not search(s_big, depth=1) or search(s_big, depth=1)[0][0] < 0.3,
        str(search(s_big, depth=1)[:1]))
    weak = U(1, LOCAL, "back", 3, 3, 3, "artillery")
    s_combo = mk([weak, U(5, ENEMY, "frontline", 4, 4, 3, "infantry")], [blast], kred=3)
    cb = search(s_combo, depth=2)[0][1]
    chk("爆破 + 攻击 组合：1 点伤害 + 3 攻击 = 杀死 4 血目标（先后顺序都行）",
        sorted(a.kind for a in cb)[:2] == ["attack", "order"], str(cb))
    holm = H(46, "HOLM", 3, "infantry", 4, 4, eff={"kredit_next": -2})
    hs = mk([], [holm], kred=3)
    normal = H(47, "PLAIN", 3, "infantry", 4, 4)
    ns = mk([], [normal], kred=3)
    chk("延迟扣费：同样身板，带下回合扣费的收益更低",
        search(hs, depth=1)[0][0] < search(ns, depth=1)[0][0] or not search(hs, depth=1))
    # 注：霍尔姆的扣费挂在部署单位的效果上，这里用指令形式的 kredit_next 验证方向
    dbt = H(48, "BARRAGE", 2, "order", eff={"kredit_next": -3, "damage": 2, "target": "enemy"})
    ds = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry")], [dbt], kred=2)
    plain = H(49, "SHOT", 2, "order", eff={"damage": 2, "target": "enemy"})
    ps2 = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry")], [plain], kred=2)
    chk("持续弹幕：同样杀伤，带下回合扣 3 点的收益更低",
        search(ds, depth=1)[0][0] < search(ps2, depth=1)[0][0])

    # ---- 随机/抉择分支：sim 不再取期望/取最好（M1，总纲 §4）；没人处理 ⇒ 只应用公共部分 + 记缺口 ----
    conv = H(60, "CONVOY", 2, "order", eff={"target": "enemy", "outcomes": [
        (1 / 3, {"damage": 0}), (1 / 3, {"damage": 1}), (1 / 3, {"damage": 2})]})
    sc = mk([U(5, ENEMY, "frontline", 3, 3, 3, "infantry")], [conv], kred=2)
    sc_end = sim_order(sc, 60, 5)
    chk("随机分支没被种子解析：不写进状态（目标仍满血）+ 记缺口", sc_end.units[5].dfn == 3
        and any("sim.outcomes" in g for g in sc_end.gaps), str(sc_end.gaps))
    chk("评估层不见随机：同一张牌两次推演结果完全相同", B_equal(sim_order(sc, 60, 5), sc_end))
    rnd_only = H(64, "RANDKW", 1, "order", eff={"chance": ["GiveRandomCombatKeyword"], "target": "friend"})
    sr = mk([U(1, LOCAL, "back", 3, 3, 3, "artillery")], [rnd_only], kred=1)
    sr_end = sim_order(sr, 64, 1)
    chk("域未知的随机：状态不变、记缺口、没有『保守期望加成』", any("sim.random" in g for g in sr_end.gaps)
        and sr_end.bonus == 0.0, str((sr_end.bonus, sr_end.gaps)))

    # ---- 攻击后触发（OnAfterAttack）：0 攻单位「上线 → 打总部」有收益（DINGO：抽 1 + 己方总部 +1）----
    dg = U(30, LOCAL, "back", 0, 1, 1, "tank", (), opc=1, aa_hq={"draw": 1, "heal_hq": 1})
    ds0 = mk([dg, U(50, ENEMY, "back", 1, 2, 2, "infantry")], [], kred=3)
    ds0.hq[LOCAL] = 15
    kinds = [(a.kind, a.dst) for a in gen_actions(ds0)]
    chk("0 攻单位在后排：先能『上线』（打总部要先上线）", ("move", None) in kinds, str(kinds))
    ds1 = apply(ds0, A("move", 30, None, 1))
    kinds1 = [(a.kind, a.dst) for a in gen_actions(ds1)]
    chk("上线后：有『打总部』这条走法（哪怕攻击力 0）", ("attack", "hq") in kinds1, str(kinds1))
    chk("上线后：不列打单位（攻击力 0 打不动）", all(d != 50 for k, d in kinds1 if k == "attack"))
    g_move = delta(ds0, ds1)
    g_hit = delta(ds1, apply(ds1, A("attack", 30, "hq", 1)))
    chk("上线本身没有收益，收益在打总部（抽牌+回血）", g_hit > g_move and g_hit > 0, "move=%.2f hit=%.2f" % (g_move, g_hit))
    best = search(ds0, depth=3, beam=8, branch=12)
    seq0 = best[0][1] if best else []
    chk("搜索能把『上线 → 打总部』当成一条链选出来",
        [x.kind for x in seq0[:2]] == ["move", "attack"] and best[0][0] > 0,
        str([(round(v, 2), [x.kind for x in q]) for v, q in best[:2]]))
    nz = mk([U(31, LOCAL, "back", 0, 1, 1, "tank", (), opc=1)], [], kred=3)
    chk("没有攻击后效果的 0 攻单位：不列打总部", not any(a.kind == "attack" for a in gen_actions(apply(nz, A("move", 31, None, 1)))))

    # ---- 带目标的牌：游戏说能指向谁，就逐个 (牌, 目标) 列成走法 ----
    shot = H(70, "SHOT", 2, "order", eff={"damage": 2, "target": "enemy"})
    ts = mk([U(5, ENEMY, "frontline", 2, 2, 2, "infantry"), U(6, ENEMY, "frontline", 2, 2, 2, "infantry"),
             U(7, ENEMY, "back", 2, 2, 2, "infantry")], [shot], kred=2)
    ts.legal = {70: [5, 7]}                       # 游戏说只能指 5 和 7（6 被免疫/烟幕之类）
    tgts = sorted(a.dst for a in gen_actions(ts) if a.kind == "order")
    chk("只列游戏说合法的目标", tgts == [5, 7], str(tgts))
    ts.pair_eff = {(70, 5): {"damage": 2}, (70, 7): {"damage": 0}}
    ds = {a.dst: delta(ts, apply(ts, a)) for a in gen_actions(ts) if a.kind == "order"}
    chk("每一对各自用自己的效果（同一张牌，指 5 与指 7 收益不同）", ds[5] > ds[7], str(ds))
    ub = H(71, "DEPLOY-SHOCK", 3, "infantry", 3, 3)
    us = mk([U(5, ENEMY, "frontline", 1, 1, 1, "infantry")], [ub], kred=3)
    us.legal = {71: [5, None]}
    us.pair_eff = {(71, 5): {"damage": 1}}
    ka = sorted((str(a.dst) for a in gen_actions(us) if a.kind == "deploy"))
    chk("带目标的部署：指向 5 / 不指向 各是一条走法", ka == ["5", "None"] or ka == ["None", "5"], str(ka))
    d_t = delta(us, apply(us, A("deploy", 71, 5, 3)))
    d_n = delta(us, apply(us, A("deploy", 71, None, 3)))
    chk("带目标的部署：能顺手杀死 1/1 的走法更好", d_t > d_n, "%.2f > %.2f" % (d_t, d_n))
    stale = mk([U(5, ENEMY, "frontline", 1, 1, 1, "infantry")], [shot], kred=2)
    stale.legal = {70: [5, 99]}
    chk("目标已不在场（陈旧）就不列", [a.dst for a in gen_actions(stale)] == [5])
    sk = search(ts, depth=1, skip=lambda a: a.dst == 5)
    chk("search 的 skip：被游戏否掉的走法不再出现", all(seq[0].dst != 5 for _, seq in sk))

    # ---- 部署 / 手牌持有价值 ----
    dep = H(50, "TANK", 4, "tank", 5, 5)
    ds2 = mk([], [dep], kred=4)
    chk("部署好单位 delta>0", search(ds2, depth=1)[0][0] > 0)
    junk = H(51, "JUNK", 4, "infantry", 0, 1)
    js = mk([], [junk], kred=4)
    rj = search(js, depth=1)
    chk("劣质高费单位：收益远低于好单位", (rj[0][0] if rj else -9) < search(ds2, depth=1)[0][0] - 1.0,
        str(rj[:1]))
    return fails


if __name__ == "__main__":
    import sys
    print("policy.boardeval 离线自检")
    n = selftest()
    print("失败 %d 项" % n)
    sys.exit(1 if n else 0)
