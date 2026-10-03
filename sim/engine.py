#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.engine —— L2 规则引擎：谁能打谁 / 动作转移（攻击·部署·上线·出指令·回合结束）/ 效果摘要结算 / 钩子后果结算。

2026-10-03 起从 `policy/boardeval.py` 物理迁入（函数体一字不改，只改名字来源）；**不得** import `evaluation` / `policy` / `player.rule`。
两处与"价值"耦合的过渡口，**显式注入而不是 import**（待"返回概率分支"重构后删除）：
  * `WEIGHTS`：规则里用到的 2 个常量（`hand_cap` 手牌上限是游戏规则；`draw_v` 匿名牌持有价值是估值参数）——由 `policy/boardeval.py` 把评估侧的权重表绑进来；
  * `_evaluate`：`outcomes` 分支取期望/取最好时要给终态打分——同样由门面绑定 `set_evaluator`。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sim.state import (DRAW_CAP, ENEMY, EVENT_FX_KINDS, FRONTLINE_CAP, FRONTLINE_CAP_LIMITED, GROUND, LOCAL,
                       NO_RETALIATION, SUPPORT_UNITS_CAP, UNIT_TYPES,
                       H, Sim, U)

WEIGHTS = {"hand_cap": 9, "draw_v": 1.2}      # 门面会把它换成评估侧的 W（同一个 dict）
_unit_valuer = None


def set_unit_valuer(fn) -> None:
    """门面绑定 `unit_value(u) -> 价值`（"打最值钱的那个敌人"这类**选目标启发式**用）。过渡口：
    目标选择该移到策略层（M3），移走后删。（`evaluate` 的注入口已在 M1 删除——sim 不再给终态打分。）"""
    global _unit_valuer
    _unit_valuer = fn


def _unit_value(u) -> float:
    if _unit_valuer is None:
        raise RuntimeError("sim.engine 没有绑定 unit_value；先 import policy.boardeval 或调用 set_evaluator(…, unit_value=…)")
    return _unit_valuer(u)


def _unit_value(u) -> float:
    if _unit_valuer is None:
        raise RuntimeError("sim.engine 没有绑定 unit_value；先 import policy.boardeval 或调用 set_evaluator(…, unit_value=…)")
    return _unit_valuer(u)


def _evaluate(sim, w=None) -> float:
    if _evaluator is None:
        raise RuntimeError("sim.engine 没有绑定评估函数（outcomes 分支需要）；先 import policy.boardeval 或调用 set_evaluator")
    return _evaluator(sim) if w is None else _evaluator(sim, w)


# ---------------------------------------------------------------------------
# 规则：谁能打谁
# ---------------------------------------------------------------------------
def _tkind(t: U) -> str:
    return "front" if t.row == "frontline" else "support"


def _rows_ok(a: U, tk: str) -> bool:
    """兵种×所在行（规则百科 + OCR 上游用户确认）：步兵/坦克在支援线只能打敌方前线；
    站上前线后打支援线/总部；战斗机/轰炸机/炮兵哪里都能打。"""
    if a.typ in GROUND:
        return tk == "front" if a.row == "back" else tk in ("support", "hq")
    return True


def _interceptors(sim: Sim) -> bool:
    return any(u.side == ENEMY and u.row == "back" and u.typ == "fighter"
               for u in sim.units.values())


def can_hit_hq(sim: Sim, a: U) -> bool:
    if sim.hq_guarded and a.typ != "artillery":
        return False
    if a.typ == "bomber" and _interceptors(sim):
        return False
    return _rows_ok(a, "hq")


def can_hit_unit(sim: Sim, a: U, t: U) -> bool:
    if "smokescreen" in t.kw or t.guarded:
        return False
    if a.typ == "bomber" and _interceptors(sim) and t.row == "back":
        return False
    return _rows_ok(a, _tkind(t))


# ---------------------------------------------------------------------------
# 动作 & 转移
# ---------------------------------------------------------------------------
class A:
    """一个行动。kind ∈ attack | deploy | order | move。dst 是目标 id（'hq' = 敌方总部）。

    ★ 2026-10-03（用户总原则）：**行动 = 动作 + 其后的全部选择**。`path` 是选择路径（目前一层：抉择牌的选项下标；
    `eff` 是这条路径自己的效果摘要（已不含 `outcomes`），`apply` 优先用它。同一张牌的不同路径是**不同的行动**。"""
    __slots__ = ("kind", "src", "dst", "cost", "label", "path", "eff")

    def __init__(self, kind, src, dst=None, cost=0.0, label="", path=(), eff=None):
        self.kind, self.src, self.dst, self.cost, self.label = kind, src, dst, cost, label
        self.path, self.eff = tuple(path), eff

    def key(self):
        return (self.kind, self.src, self.dst, self.path)

    def __repr__(self):
        return "A(%s %s->%s c=%s%s)" % (self.kind, self.src, self.dst, self.cost,
                                        (" path=%s" % (self.path,)) if self.path else "")


def _apply_death(s: Sim, uid) -> None:
    """单位被摧毁时的触发（`OnDestroyed` 0x27，效果预计算在 `s.death_fx`）。

    用 `unit_eff` 语义应用：此刻该单位已不在 `s.units` ⇒ "给自己加 buff"这类自然落空，
    洗牌 / 抽牌 / 对敌伤害 / 指挥点等照常结算（5th SASEBO 的"洗牌 + 抽 1"就走这里）。
    """
    eff = s.death_fx.get(uid)
    if eff:
        _apply_unit_eff(s, uid, eff)


def _dmg_unit(s: Sim, t: U, dmg: float, engage: bool = False) -> None:
    """（规则本体已迁到 `sim/effects.py::deal_damage`；这里保留旧名转调，行为一字不改。）

    `on_death` 注入 `_apply_death` —— 那一段要跑 0x27/0x18/打捞的**死亡链**（L1 钩子），
    sim 只做"掉血/离场"这条状态变更（§3.1 分层）。
    """
    from sim.effects import deal_damage
    deal_damage(s, t, dmg, engage=engage, on_death=_apply_death)


def _fx_apply(s: Sim, b: Optional[dict]) -> None:
    """应用 attack_fx 里的一个时点（全局效果 + 逐牌效果）。钩子效果的目标已经在 triggers 里按记录拆好：
    全局的走 `_apply_eff(..., target="any")`（`damage_hq`/`heal_hq`/指挥点/抽牌/…），逐牌的以那张牌为目标。"""
    if not b:
        return
    if b.get("eff"):
        _apply_eff(s, dict(b["eff"], target="any"), None)
    for uid, e in b.get("units") or ():
        if uid in s.units:
            _apply_eff(s, dict(e), uid)


def _fx_for(sim: Sim, attacker: int, dst) -> Optional[dict]:
    return sim.attack_fx.get((attacker, dst)) if sim.attack_fx else None


def _fatigue_once(s: Sim) -> None:
    """游戏 `BP_CardFunctions::ApplyFatigueDamage` 的一次结算（空库抽牌 / 协力未满足）。

    ★ 2026-10-02：规则本体已迁到 `sim/chain.py::apply_fatigue`（规则下沉，§5 步骤 3），
      这里保留旧名**转调** —— 行为一字不改（`test_draw_chain.py` 钉住）。
    """
    from sim.chain import apply_fatigue                      # 过渡期桥（避免 import 环）
    apply_fatigue(s)


def sim_attack(sim: Sim, attacker: int, target: Optional[int], hq: bool = False) -> Sim:
    s = sim.copy()
    a = s.units.get(attacker)
    if a is None:
        return s
    dst = "hq" if (hq or target is None) else target
    fx = _fx_for(sim, attacker, dst)
    if fx:
        if fx.get("stop"):                            # StopAttack：不付费、不耗行动、什么都不发生
            return s
        sw = fx.get("switch_to")
        if sw is not None and sw != dst:              # 0x1E 换了防守方
            dst = sw
            hq, target = (sw == "hq"), (None if sw == "hq" else sw)
    bk = (fx or {}).get("buckets") or {}
    paid = fx.get("paid") if fx and fx.get("paid") is not None else a.opc
    s.kredits -= paid
    a.attacks_left -= 1
    a.acted = a.attacks_left <= 0
    a.kw = a.kw - {"smokescreen"}                    # 攻击后失去烟幕
    if not can_move_and_attack(a) and a.moved:
        a.attacks_left = 0
    _fx_apply(s, bk.get("before"))
    if fx and fx.get("consumed"):                     # AttackedAndStopped：花了费、耗了行动，不结算伤害
        _fx_apply(s, bk.get("after"))
        return s
    dd = fx.get("dmg_def") if fx else None
    da = fx.get("dmg_att") if fx else None
    if hq or target is None:
        s.hq[ENEMY] = s.hq.get(ENEMY, 0) - max(a.atk if dd is None else dd, 0)
        _fx_apply(s, bk.get("mid"))
        if a.aa_hq and not (fx and fx.get("own_after")):
            _apply_eff(s, a.aa_hq, None)               # 抽牌/回血（含随机分支的期望）
        _fx_apply(s, bk.get("after"))
        return s
    t = s.units.get(target)
    if t is None:
        return s
    if dd is not None:                                # 钩子管线给了最终伤害/阵亡 ⇒ 用它，不再自己近似
        t.dfn -= max(dd, 0)
        a.dfn -= max(da or 0, 0)
        if "shock" in a.kw:
            a.kw = a.kw - {"shock"}
        _fx_apply(s, bk.get("mid"))
        t_dead = bool(fx.get("def_dies")) or t.dfn <= 0
        a_dead = bool(fx.get("att_dies")) or a.dfn <= 0
        if t_dead:
            s.units.pop(t.id, None)
            _apply_death(s, t.id)
            _fx_apply(s, bk.get("def_destroyed"))
        if a_dead:
            s.units.pop(attacker, None)
            _apply_death(s, attacker)
            _fx_apply(s, bk.get("att_destroyed"))
        _fx_apply(s, bk.get("after"))
        return s
    t_atk = t.atk
    if "ambush" in t.kw:                              # 伏击：先反击，能杀死则攻击方打不出伤害
        if t_atk >= a.dfn:
            s.units.pop(attacker, None)
            _apply_death(s, attacker)
            return s
        a.dfn -= max(t_atk - a.armor, 0)
        t_atk = 0
    shock = "shock" in a.kw
    _dmg_unit(s, t, a.atk, engage=True)
    if shock:
        a.kw = a.kw - {"shock"}                       # 冲击：首击无反击，用后移除
    elif a.typ not in NO_RETALIATION and t_atk > 0:   # 反击（炮兵/轰炸机不吃）
        a.dfn -= max(t_atk - a.armor, 0)
        if a.dfn <= 0:
            s.units.pop(attacker, None)
            _apply_death(s, attacker)
    if fx:                                            # 只有钩子后果（没有改写伤害）：按近似结算后的生死叠上
        _fx_apply(s, bk.get("mid"))
        if t.id not in s.units:
            _fx_apply(s, bk.get("def_destroyed"))
        if attacker not in s.units:
            _fx_apply(s, bk.get("att_destroyed"))
        _fx_apply(s, bk.get("after"))
    return s


def row_full(s: Sim, side: str, row: str) -> bool:
    """原版 `FetchCardsByLocation` 的 `isLocationFull`：支援线按"含总部 5 张"⇒ 单位 4；前线双方共用 5（受限时 2）。"""
    if row == "frontline":
        cap = FRONTLINE_CAP_LIMITED if getattr(s, "front_limited", False) else FRONTLINE_CAP
        return sum(1 for u in s.units.values() if u.row == "frontline") >= cap
    return sum(1 for u in s.units.values() if u.row == "back" and u.side == side) >= SUPPORT_UNITS_CAP


def sim_deploy(sim: Sim, uid: int, atk: int, dfn: int, cost: int, typ: str, kw=(),
               hand_id: Optional[int] = None) -> Sim:
    s = sim.copy()
    s.units[uid] = U(uid, LOCAL, "back", atk, dfn, cost, typ, kw, sick=True)   # 部署病
    s.kredits -= cost
    if hand_id is not None:
        s.hand.pop(hand_id, None)
    for m in s.enter_mods:                         # 本回合常驻的"别的牌进场"效果（JUNGLE FEVER）对刚进场的单位生效
        _apply_eff(s, m, uid)
    return s


def can_move_and_attack(u: U) -> bool:
    """原版：`BaseCardObject::CanMoveAndAttackInTheSameTurn`（BP_CardFunctions::MoveCardToFrontline，BP_CardFunctions.cpp:17590）
    = 装甲单位，或 customName1 带 `CanMoveAndAttackInTheSameTurn`（`move_attack` 伪关键词，由 rule 填）。"""
    return u.typ == "tank" or "move_attack" in u.kw


def sim_move(sim: Sim, uid: int) -> Sim:
    """上线（后排→前线）。**上线钩子（0x32）的预计算后果**在 `event_fx["move"]` 里，见下。

    ★ 2026-10-02：临时攻击力（`atk_turn`）在 `sim_turn_end` 里清零，见那里。
    """
    s = sim.copy()
    u = s.units.get(uid)
    if u is not None:
        u.row, u.moved = "frontline", True
        s.kredits -= u.opc
        if not can_move_and_attack(u):                # 原版 MoveCardToFrontline：否则 attackLeft = 0
            u.attacks_left = 0
        # ★ 2026-10-02（§8-8 上线族 0x32）：上线**会触发钩子**（自己 `OnMoveToFrontline` +
        #   旁观者 `OnOtherCardMoveToFrontline`）。预计算结果在 `event_fx["move"]` 里
        #   （rule 跑 VM 得到），这里按逐牌效果语义结算 —— 否则"上线触发的东西"对评估不可见。
        _event_fx_apply(s, "move", uid)
    return s


def sim_turn_end(sim: Sim) -> Sim:
    """**把"回合结束"这一步也模拟掉**：我方牌 `OnEndOfTurn`（0x19）的预计算后果
    （`event_fx["turn_end"]`，rule 跑 VM 得到）+ `RemoveBuffsEndOfTurn`（`AddAttackUntilEndOfTurn`
    的临时攻击力清零 —— BP `ExecuteEndOfTurnQueue` 最后一步）。

    为什么要有它（用户口径"eval = 打出这一步后游戏里会发生什么，就模拟什么"）：
    评估一个候选动作的终态时，那个终态其实是"我停手之后、这一回合结束时的盘面" ——
    `OnEndOfTurn` 的效果（例如"回合结束对敌方 HQ 造成 X"）本就该算进去。
    `turn_ended` 幂等标记防重复结算。顺序照 BP：先所有 0x19 钩子，最后才清 buff。
    """
    if getattr(sim, "turn_ended", False):
        return sim                                   # 幂等：同一份终态只结算一次
    s = sim.copy()
    s.enter_mods = []
    for uid, eff in (s.event_fx.get("turn_end") or {}).items():
        if uid in s.units and eff:
            _apply_unit_eff(s, uid, eff)
    for u in s.units.values():                       # RemoveBuffsEndOfTurn：临时攻清零
        if getattr(u, "atk_turn", 0):
            u.atk -= u.atk_turn
            u.atk_turn = 0
    s.turn_ended = True
    return s


def _clampf(v, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _res_eff(s: Sim, e: dict) -> None:
    """资源类效果（出指令和部署单位共用）：指挥点/槽/延迟扣费/抽牌/加牌/召唤。"""
    # ★ EVAL-NATIVES §8-1：游戏的 `ChangeKreditsBySide` 等叶子写 GameState 后，BP 层把
    #   指挥点/槽夹到 [0, max]；模拟在同一处夹取（支出**不**夹——它是"扣多少"）。
    s.kredits = _clampf(s.kredits + e.get("kredit", 0), 0.0, s.kredit_max)
    if e.get("kredit_next"):
        s.pending.append((1, e["kredit_next"]))
    s.slots = _clampf(s.slots + e.get("slot", 0), 0.0, s.kredit_max)
    s.opp_kredits += e.get("opp_kredit", 0)
    s.opp_slots += e.get("opp_slot", 0)
    n_draw = int(e.get("draw", 0) + e.get("gain_cards", 0))
    # ★ 2026-10-02：抽牌链（抽到 → 抽到时效果 / autoplay → 再抽…）**已迁到
    #   `sim/chain.py::draw_chain`**（规则下沉，§5 步骤 3）。那条链原本是这里的
    #   `while` 队列；现在由事件队列驱动，语义不变：
    #     · 队列顺序 = **DFS**（新事件排队首）= 原来"钩子内部同步递归"的行为；
    #     · 上限仍是 `DRAW_CAP`（抵抗那种连抽几十张的链）；
    #     · "再抽 N 张"只入队、其余部分交给 `_apply_eff` —— 否则会被算两遍。
    #   权重（`hand_cap` / `anon_hold`）由评估侧传进去：sim 不 import 权重表。
    if n_draw:
        from sim.chain import draw_chain
        _run = draw_chain(s, n_draw, apply_effect=_apply_eff, hand_cap=WEIGHTS["hand_cap"],
                          anon_hold=WEIGHTS["draw_v"], cap=DRAW_CAP,
                          bystander_fx=(s.event_fx.get("draw") or {}).get)
        s.gaps += list(_run.gaps)
    if e.get("spawn_cards"):
        # 原版 `SpawnCardToBoard`（BP_CardFunctions，1.60 导出）：非前线 ⇒ 目标排满就**不生成**（没有指定卡 id 时）；
        # 前线 ⇒ 前线被对方占就拒绝。生成的单位用卡自己的面板（`spawn_stats`），查不到面板 ⇒ 记缺口、不编。
        for sc in e["spawn_cards"]:
            side = LOCAL if sc.get("mine", True) else ENEMY
            row = "frontline" if sc.get("row") == "frontline" else "back"
            if row_full(s, side, row):
                continue
            if row == "frontline" and s.front_owner == (ENEMY if side == LOCAL else LOCAL):
                continue
            stat = s.spawn_stats(sc["name"]) if s.spawn_stats else None
            if not stat:
                s.gaps.append("spawn：%s 的面板读不到，生成的单位没法入模拟（不编）" % sc["name"])
                continue
            s.tmp_seq += 1
            uid = -(2000 + s.tmp_seq) if side == LOCAL else -(2100 + s.tmp_seq)
            s.units[uid] = U(uid, side, row, stat.get("atk", 0), stat.get("dfn", 0), stat.get("cost", 0),
                             stat.get("typ", "infantry"), stat.get("kw", ()), sick=True)
            if side == LOCAL:
                for m in s.enter_mods:
                    _apply_eff(s, m, uid)
    else:
        for i in range(int(e.get("spawn", 0))):          # 旧摘要（没有卡名）：只能按通用 2/2，且仍受排容量限制
            if row_full(s, LOCAL, "back"):
                break
            uid = -(2000 + len(s.units) + i)
            s.units[uid] = U(uid, LOCAL, "back", 2, 2, 0, "infantry", (), sick=True)
        for i in range(int(e.get("opp_spawn", 0))):        # §15.5：对方场上加单位（对我们不利）
            if row_full(s, ENEMY, "back"):
                break
            uid = -(2100 + len(s.units) + i)
            s.units[uid] = U(uid, ENEMY, "back", 2, 2, 0, "infantry", (), sick=True)
    s.opp_cards += int(e.get("opp_draw", 0) + e.get("opp_gain_cards", 0))


def _apply_eff(s: Sim, e: dict, target) -> None:
    """把一份效果摘要作用到 `s` 上（原地）。`target`：目标单位 id / "hq" / None。"""
    s.bonus += e.get("_est", 0.0)
    if e.get("chance"):
        # 域未知的随机（VM 没法按种子推演的随机动词）：状态不变、如实记缺口——评估层不见随机，也不加"期望"
        s.gaps.append("sim.random：%s 无法按种子推演，状态不变" % (e["chance"],))
    if e.get("outcomes"):
        # ★ M1（总纲 §4/§5）：这里**不再**对分支取期望/取最好。
        #   · 我们的选择（`outcomes_mode=max`）由策略经 `sim.engine.run` 的提示展开（路径），不该走到这里；
        #   · 随机由 VM/`sim` 按种子算成确定结果，不该再有随机分支。
        #   走到这里 = 没人处理：只应用各分支的**公共部分**，并如实记缺口。
        s.gaps.append("sim.outcomes：%d 个分支没有被提示/种子解析（%s），只应用公共部分"
                      % (len(e["outcomes"]), "抉择" if e.get("outcomes_mode") == "max" else "随机"))
        e = {k: v for k, v in e.items() if k not in ("outcomes", "outcomes_mode")}
    t = s.units.get(target) if target not in (None, "hq") else None
    if e.get("damage") and t is not None:
        _dmg_unit(s, t, e["damage"])
    # ★ 2026-10-02：`damage_hq` 就是"对**敌方 HQ** 的伤害"（VM 里 `DamageCard(敌方HQ卡)` 现在记成
    #   它；静态摘要里也同义）——不再要求调用方额外标 target，否则 7th SCOTTISH BORDERERS 这类
    #   "自动打 HQ"的效果会被静默丢掉（用户：打伤害这么浅显，就算入）。
    if e.get("damage_hq"):
        s.hq[ENEMY] = s.hq.get(ENEMY, 0) - e["damage_hq"]
    if e.get("damage_own_hq"):                    # §15.5：己方 HQ 受伤（self_damage 拆键）
        s.hq[LOCAL] = s.hq.get(LOCAL, 0) - e["damage_own_hq"]
    if target == "hq" and e.get("damage"):
        s.hq[ENEMY] = s.hq.get(ENEMY, 0) - e["damage"]
    if e.get("destroy") and t is not None:
        s.units.pop(t.id, None)
        _apply_death(s, t.id)                     # 被效果摧毁也算 OnDestroyed
    if e.get("retreat") and t is not None:
        # 0x36：**先**结算"别人看到它撤退"的后果（那时它还在场，`_apply_unit_eff` 才找得到目标），
        # 再让单位离场。规则出处：BP `ApplyMakeCardRetreat`@16567（自己 OnBeforeRetreat + 0x36）。
        _event_fx_apply(s, "retreat", t.id)
        s.units.pop(t.id, None)
    if e.get("retreat_ids"):
        # 撤退的是空跑时数组里**那几张**（原版 `MakeCardRetreat(Cards[])`；随机目标已由活种子定）
        for cid in e["retreat_ids"]:
            if cid in s.units:
                _event_fx_apply(s, "retreat", cid)
                s.units.pop(cid, None)
    if e.get("fight"):
        # MakeCardsFight：两张牌互相战斗（规则在 `sim/effects.py`，这里只转调）。
        # `effectvm` 会给 [id_a, id_b]；只有 `True` = 指针换不出 id ⇒ 如实记缺口、不结算。
        from sim.effects import apply_fight
        fs = e["fight"]
        if isinstance(fs, (list, tuple)) and len(fs) == 2:
            s.gaps += apply_fight(s, int(fs[0]), int(fs[1]), deal_damage=_dmg_unit,
                                  kill=_apply_death, damages=e.get("fight_dmg"))
        else:
            s.gaps.append("fight：参数里没有一对 card_id（effectvm 没换出来），未结算")
    if e.get("convert"):
        # ConvertCard（BP_CardFunctions@12611，逐行读过）：每张旧牌——在场：`ApplyRemoveCardFromBoard`（离场、
        #   不算摧毁）；然后在**同位置同序号** `CreateCard(目标名)`（出厂数值，buff 丢失）；在牌库：同下标换新牌；
        #   在场的新牌走 `OnEnterPlay(5)`，其它牌收到 `OnOtherCardConverted`(0x22)。
        cv = e["convert"] if isinstance(e["convert"], dict) else None
        if cv is None:
            s.gaps.append("convert：ConvertCard 没取到目标卡数值（effectvm 只记了布尔）⇒ 不结算")
        else:
            for old in cv.get("ids", ()):
                u = s.units.get(old)
                if u is not None:
                    s.tmp_seq += 1
                    nid = -(5000 + s.tmp_seq)
                    nu = U(nid, u.side, u.row, cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("cost") or 0,
                           cv.get("typ") or u.typ, cv.get("kw") or (), opc=cv.get("opc") or 1,
                           sick=True, armor=cv.get("armor") or 0, mdef=cv.get("dfn") or 0)
                    s.units.pop(old, None)
                    s.units[nid] = nu
                    s.card_templates.pop(old, None)
                elif old in s.hand:
                    h = s.hand[old]
                    s.tmp_seq += 1
                    nid = -(5000 + s.tmp_seq)
                    s.hand.pop(old)
                    s.hand[nid] = H(nid, cv.get("name") or "?", cv.get("cost") or 0, cv.get("typ") or h.typ,
                                    cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("kw") or (), {})
                    s.gaps.append("convert：手牌里的转化牌 %s 的效果未知（出厂数值已换，eff 留空）" % cv.get("name"))
                elif old in s.deck:
                    s.gaps.append("convert：牌库里的转化未建模（下标换牌）")
            s.gaps.append("convert：新牌 OnEnterPlay(5) / 其它牌 OnOtherCardConverted(0x22) 后续钩子未建模")
    if e.get("enter_mod"):
        s.enter_mods.append(dict(e["enter_mod"]))        # 打出后常驻到回合结束（JUNGLE FEVER 一类）
    if e.get("choose_spawn"):
        # selectCardToDraw：三选一加入手牌。有候选表（`sim.spawn_pick`）时走 `run` 的挂起路径、标记已被剥掉，不会走到这里。
        s.gaps.append("choose_spawn：三选一加入手牌（selectCardToDraw）的候选未展开（没有可用的种子预测）")
    if e.get("forecast"):
        # 预报：弹出第一层三选一。有候选表（`sim.forecast`）时走 `run` 的挂起路径、`forecast` 标记已被剥掉，不会走到这里。
        s.gaps.append("forecast：预报选哪张（两层 3×3）未展开（没有可用的种子预测）")
    if e.get("hand_target_pending"):
        # selectTargetFromHand：玩家之后选手牌，后果在 `OnHandTargetSelected`（175th 等："放到牌库顶"）。
        s.gaps.append("hand_target：部署后要选一张手牌（OnHandTargetSelected 的后果）未建模")
    if e.get("pin") and t is not None:
        t.pinned = True
        _event_fx_apply(s, "pin", t.id)            # 0x3D：别人看到"这张牌被定住了"
    if e.get("suppress") and t is not None:
        # 压制 ≠ 定住（BP SuppressMultipleUnits@7456：清关键词/重甲/指向税 + 置 isSuppressed）
        from sim.effects import apply_suppress
        s.gaps += apply_suppress(s, t.id)
        _event_fx_apply(s, "suppress", t.id)       # 0x3A：别人看到"这张牌被压制了"
    if e.get("unpin") and t is not None:
        t.pinned = False
    _bids = e.get("buff_ids") or {}
    _buff_aoe = bool(_bids) and (len(_bids) > 1 or target is None)   # 群体 buff：逐张结算，标量 `buff` 不再套到单个目标上
    if _buff_aoe:
        for cid, (da, dd) in _bids.items():
            u = s.units.get(cid)
            if u is not None:
                u.atk += da
                u.dfn += dd
    if t is not None:
        for k in e.get("give", ()):
            t.kw = t.kw | {k}
            if k == "blitz":
                t.sick = False
            if k == "fury":
                t.attacks_left = max(t.attacks_left, 2 if not t.acted else 1)
        if e.get("buff") and not _buff_aoe:
            t.atk += e["buff"][0]
            t.dfn += e["buff"][1]
        if e.get("heal"):
            # FullyHealCard（BP_CardFunctions@701，2026-10-03 读过）：门槛 = 当前总防御>0 ∧ 缺口>0；
            # 通过后 `defense = maxDefense`。`OnBeforeFullyRepaired`(0xC) 可否决、0x2C/`OnFullyRepaired`
            # 的后续钩子这里未建模（如实记缺口，不编）。
            if t.dfn > 0 and float(t.mdef) - t.dfn > 0:
                hfx = (s.event_fx.get("heal") or {}).get(t.id) or {}
                if not hfx.get("heal_vetoed"):                 # 0xC OnBeforeFullyRepaired 返回 stopAction ⇒ 中止
                    t.dfn = float(t.mdef)
                    _event_fx_apply(s, "heal", t.id)           # 0x2C / 自己的 OnFullyRepaired
        if e.get("steal"):
            # TakeControlOfEnemyUnit → ChangeUnitOwnership（BP@18087，2026-10-03 读过）：
            #   side=我方、underEnemyControl=true、movementLeft=1、attackLeft=1（有 fury 则 2）
            #   ⇒ **偷来当回合就能动/能攻**（旧实现写 sick=True 是错的）。
            #   落点：原在前线且前线 >1 张 ⇒ 去我方后排；前线只有它 ⇒ 留在前线；原在后排 ⇒ 我方后排。
            #   我方后排满、OnBeforeLeaveBoardOrOwner/OnEnterPlay(0) 等后续钩子未建模 ⇒ 记缺口。
            if t.row == "frontline":
                n_front = sum(1 for u in s.units.values() if u.row == "frontline")
                if n_front > 1:
                    t.row = "back"
            t.side = LOCAL
            t.sick = False
            t.moved = False
            t.acted = False
            t.attacks_left = 2 if "fury" in t.kw else 1
            _event_fx_apply(s, "steal", t.id)                  # 离场/入场钩子（0x2E/0x8/OnEnterPlay(0)/0x2B）
            s.gaps.append("steal：我方后排满的分支 / hasActivePincerEffect / CardLocationMoved 未建模")
        if e.get("veteran"):
            # MakeVeteran（BP_CardFunctions@7127，逐行读过）：门槛 = 在场 ∧ 有 `_vet` 静态卡 ∧ 还不是老兵 ∧ 总防>0。
            #   通过后：attack/defense/operationCost/heavyArmor 经 `ChangeX(…, veteranSet=5)` 设为 `_vet` 的**绝对值**
            #   （veteranSet 与 SetValue 同一分支；防御同时置 maxDefense；buff 不动），然后
            #   ambush/blitz/fury/mobilize/shock/smokescreen 直接赋成 `_vet` 的值，guard 同理，
            #   新获得 fury 且「还有攻击次数」⇒ attackLeft+1。
            vet = e["veteran"] if isinstance(e["veteran"], dict) else None
            if "veteran" not in t.kw and t.dfn > 0:
                t.kw = t.kw | {"veteran"}
                if vet is None:
                    s.gaps.append("veteran：没取到 `_vet` 静态卡数值（只打标记，数值改写未结算）")
                else:
                    if vet.get("atk_to") is not None and vet.get("atk_from") is not None:
                        t.atk += int(vet["atk_to"]) - int(vet["atk_from"])
                    if vet.get("dfn_to") is not None:
                        t.dfn = float(vet["dfn_to"])
                        t.mdef = float(vet["dfn_to"])
                    if vet.get("opc_to") is not None and vet.get("opc_from") is not None:
                        t.opc = max(t.opc + int(vet["opc_to"]) - int(vet["opc_from"]), 0)
                    if vet.get("armor_to") is not None:
                        t.armor = min(3, max(0, int(vet["armor_to"]) + int(vet.get("armor_buff") or 0)))
                    had_fury = "fury" in t.kw
                    for kwn, on in (vet.get("kw") or {}).items():
                        if kwn == "guard":
                            continue                      # guard 另算（卡自带 guard 自定义能力时不改，sim 读不到）
                        t.kw = (t.kw | {kwn}) if on else (t.kw - {kwn})
                    if (vet.get("kw") or {}).get("guard") is not None:
                        g_on = bool(vet["kw"]["guard"])
                        t.kw = (t.kw | {"guard"}) if g_on else (t.kw - {"guard"})
                        if g_on:
                            t.kw = t.kw - {"smokescreen"}     # BP：获得 guard ⇒ RemoveSmokescreen
                    if "blitz" in t.kw:
                        t.sick = False
                    if (vet.get("kw") or {}).get("fury") and not had_fury and (t.attacks_left > 0 or t.acted):
                        t.attacks_left += 1
                    s.gaps.append("veteran：guard 的『卡自带 guard 自定义能力则不改』、destruction、"
                                  "ExecuteOnOtherCardsAbilitiesChanged 未建模")
                _event_fx_apply(s, "veteran", t.id)            # OnBecomingVeteran + 0x20
        if e.get("armor"):
            t.armor += e["armor"]
        # ★ 攻击链钩子里出现的单位属性变化（ATTACK-HOOKS-1.60.md §6）
        if e.get("attack_turn"):                      # 本回合 +N 攻
            t.atk += e["attack_turn"]
            t.atk_turn += e["attack_turn"]             # 单独记：这部分回合结束会消失（见 unit_value）
        if e.get("opcost"):                           # 行动费变化
            t.opc = max(t.opc + e["opcost"], 0)
        for k, kw in (("remove_immune", "immune"), ("remove_fury", "fury"), ("remove_ambush", "ambush"),
                      ("remove_smokescreen", "smokescreen"), ("remove_alpine", "alpine"),
                      ("remove_salvage", "salvage"), ("remove_shock", "shock"),
                      ("remove_guard", "guard"), ("remove_blitz", "blitz"),
                      ("remove_mobilize", "mobilize")):
            if e.get(k):
                t.kw = t.kw - {kw}
        if e.get("pin_turns"):
            # ChangedPinnedTurns：正 = 压制更久（模拟只记布尔压制态），负 = 减少压制回合数。
            # ★ 我们没建模 `pinnedTurns` 的剩余回合数（单回合粒度的 delta 用不到）；
            #   负数按"解除压制"处理——"还差几回合解压"对这一步的估值没有影响。
            n = int(e["pin_turns"])
            if n > 0:
                t.pinned = True
            elif n < 0:
                t.pinned = False
        if e.get("reveal"):
            # RevealCard：BP 里 isRevealed=true、hasCovert=false。模拟里 covert 只体现在关键词上，
            # 揭示 = 去掉 covert（未揭示隐蔽单位不可被指向/攻击的判据由此失效）。
            t.kw = t.kw - {"covert"}
            _event_fx_apply(s, "reveal", t.id)     # 0x37：别人看到"这张牌被揭示了"
        if e.get("kredits_tax"):
            # AddKreditsTax：KreditsTax_AsEnemyTarget = max(0, 当前 + 增量)
            t.tax = max(0, int(t.tax) + int(e["kredits_tax"]))
    if t is not None and e.get("reset_ops"):          # ResetUnitOperations：行动次数重置
        t.attacks_left = max(t.attacks_left, 2 if "fury" in t.kw else 1)
        t.acted, t.moved = False, False
    if e.get("to_deck") and target in s.hand:
        # 手牌目标提示（175th / PBY CATALINA）：`MoveCardToTopOfOwnersDeck(选中的手牌)` ⇒ 手里少这张、牌库顶多这张（下回合必抽到它）
        h = s.hand.pop(target)
        s.card_templates.setdefault(target, h)
        if s.deck_known:
            s.deck.insert(0, target)
            s.deck_cards[target] = h
        else:
            s.gaps.append("sim.to_deck：牌库顺序未知，放回牌库顶只记缺口")
    if t is not None and (e.get("remove_unit") or e.get("to_deck")):
        s.units.pop(t.id, None)                       # 离场（不算摧毁）/ 放回牌库顶
    if e.get("end_match") in (LOCAL, ENEMY):              # EndMatch(winnerSide)：直接分出胜负（致命一击/致命败北）
        s.hq[ENEMY if e["end_match"] == LOCAL else LOCAL] = 0
    if e.get("heal_hq"):
        s.hq[LOCAL] = s.hq.get(LOCAL, 0) + e["heal_hq"]
    for cid in e.get("discard_ids", ()):           # DiscardCardFromHand：己方手牌里有这张就移走（敌方的不建模）
        s.hand.pop(cid, None)
    if e.get("playing_side"):                      # SetPlayingSide/SetActiveSide：行动权换边
        s.playing_side = e["playing_side"]
    if e.get("bond_fatigue"):
        # 协力（Bond）未满足：BP `OnCardPlayedFromHand` 里
        # `HasBond && !activeBondFactions.Contains(faction)` ⇒ `ApplyFatigueDamage(side, fromBond=true)`
        # = 己方总部吃一次疲劳伤害（与空库抽牌同一套计数）。
        _fatigue_once(s)
    for sid in e.get("salvage_ids", ()):           # 收缴：1/1、费用≤3 的复制进手牌
        _salvage_one(s, sid)
    for uid, eff_i in e.get("unit_eff", ()):       # 情报触发等"逐牌效果"（见 _apply_unit_eff）
        _apply_unit_eff(s, uid, eff_i)
    if e.get("defense_aoe") and e.get("defense_aoe_ids"):
        # AddDefenseToMultipleCards：按 id 数组逐张加/减防（amount<0 = 设总防，≤0 的由 BP 摧毁）。
        amt = int(e["defense_aoe"])
        for cid in e["defense_aoe_ids"]:
            u = s.units.get(cid)
            if u is None:
                continue
            u.dfn += amt
            if amt < 0 and u.dfn <= 0:
                s.units.pop(cid, None)
                _apply_death(s, cid)
    # ★ NATIVE-COVERAGE §15.5：群体动词的 id 列表逐项消费
    # ★ 组 D（NATIVE-SPEC-GAPS §5）：`setAndEncrypt*` 叶子写侧 → 绝对值（clamp 在消费侧）。
    #   目标可能是场上单位（set_attack/defense）或手牌（set_kredit）；HQ 卡不在 units 里
    #   （hq 用数值表示）⇒ 命中不了时如实跳过。
    for cid, v in (e.get("set_defense") or {}).items():
        u = s.units.get(cid)
        if u is not None:
            u.dfn = max(0, min(99, int(v)))
    for cid, v in (e.get("set_attack") or {}).items():
        u = s.units.get(cid)
        if u is not None:
            u.atk = max(0, min(99, int(v)))
    for cid, v in (e.get("set_kredit") or {}).items():
        h = s.hand.get(cid)
        if h is not None:
            h.cost = max(0, min(99, int(v)))
    for key in ("set_attack_buff", "set_kredit_buff"):
        if e.get(key):
            # U/H 只存"总量"，没有独立的 buff 字段 ⇒ 单独改 buff 无法如实落账：
            # 记缺口（不把它硬塞进总量，避免把"buff 变化"误当"总量变化"）。
            s.gaps.append("%s 未消费（模拟只有总量字段）" % key)
    if e.get("destroy_aoe_ids"):
        for cid in e["destroy_aoe_ids"]:
            if cid in s.units:
                s.units.pop(cid, None)
                _apply_death(s, cid)
    if e.get("remove_aoe_ids"):
        for cid in e["remove_aoe_ids"]:
            s.units.pop(cid, None)                     # 离场但不算被摧毁 ⇒ 不触发 death_fx
    if e.get("pin_aoe_ids"):
        for cid in e["pin_aoe_ids"]:
            u = s.units.get(cid)
            if u is not None:
                u.pinned = True
                _event_fx_apply(s, "pin", cid)
    if e.get("suppress_aoe_ids"):
        from sim.effects import apply_suppress
        for cid in e["suppress_aoe_ids"]:
            if cid in s.units:
                s.gaps += apply_suppress(s, cid)
                _event_fx_apply(s, "suppress", cid)
    if e.get("to_deck_aoe_ids"):
        pos = int(e.get("to_deck_position") or 0)
        for cid in e["to_deck_aoe_ids"]:
            u = s.units.get(cid)
            if u is None:
                continue
            s.units.pop(cid, None)
            if u.side == LOCAL and s.deck_known:
                s.deck.insert(max(0, min(pos, len(s.deck))), cid)
    if e.get("damage_aoe") and e.get("damage_aoe_ids"):
        amt = float(e["damage_aoe"])
        for cid in e["damage_aoe_ids"]:
            u = s.units.get(cid)
            if u is not None:
                _dmg_unit(s, u, amt)
    for r in e.get("restriction_add", ()):         # AddGameplayRestriction：影子列表
        s.restrictions.append(dict(r))
    for r in e.get("restriction_remove", ()):      # RemoveGameplayRestriction：同 side+type 移除
        s.restrictions = [x for x in s.restrictions
                          if not (x.get("side") == r.get("side") and x.get("type") == r.get("type"))]
    if e.get("steal_to_deck") and t is not None:
        # StealCardFromBoardToDeck：目标离场 → 同名复制进 deckSide 的牌库 → 洗牌（同一效果里的
        # `deck_shuffle` 由下面那段执行）。只对**己方**牌库建模（敌方牌序我们读不到、也不关心）。
        if e["steal_to_deck"] == LOCAL and s.deck_known:
            tpl = s.card_templates.get(t.id)
            s.deck.append(t.id)
            if tpl is not None:
                s.deck_cards.setdefault(t.id, tpl)
        s.units.pop(t.id, None)
    if e.get("move_front") and t is not None and t.row == "back":
        # MoveUnitFromSupportToFrontLine：效果导致的移动，**不花行动费、不算"移动过"**
        # （BP 直接 MoveCardToFrontline）。前线属于对方时 BP 拒绝执行。
        if s.front_owner != ENEMY:
            t.row = "frontline"
    if e.get("deck_add") and e.get("deck_add_side") == LOCAL and s.deck_known:
        # SpawnCardInDeckBySide：n 张新牌进己方牌库（按 BP 的 addToTop=!bottom / 随机位置）。
        # ★ 每张牌**都要抽一次** `RandomIntFromRangeWithStream(0, len)`（BP 在循环里无条件调用，
        #   结果只在 RandomWithoutShuffle 时用作插入位置）——不抽就会让之后的洗牌/抽牌跟游戏错位。
        for _ in range(int(e["deck_add"])):
            s.tmp_seq += 1
            cid = -(3000 + s.tmp_seq)
            tpl = H(cid, e.get("deck_add_name") or "?", 2, "order",
                    eff={"_hold": WEIGHTS["draw_v"]})
            pos = s.rng.wrapper_int(0, len(s.deck)) if s.rng is not None else None
            if e.get("deck_add_wo_shuffle") and pos is not None:
                s.deck.insert(min(len(s.deck), pos), cid)
            elif e.get("deck_add_bottom"):
                s.deck.append(cid)
            else:
                s.deck.insert(0, cid)                  # addToTop = !bottom
            s.deck_cards[cid] = tpl
            s.card_templates[cid] = tpl
        if e.get("deck_add_shuffle"):
            if s.rng is not None:
                s.deck = list(s.rng.shuffle(list(s.deck)))
            else:
                s.deck, s.deck_known = [], False
    if e.get("deck_shuffle"):                      # ShuffleDeckBySide：洗**整副牌**（游戏：Array_ShuffleFromStream）
        if e["deck_shuffle"] == LOCAL and s.deck:
            if s.rng is not None:
                # ★ 洗的是**id 列表**（与游戏 `SetDeckBySide` 同构）：`Stream.shuffle` = 正向 Fisher-Yates
                s.deck = list(s.rng.shuffle(list(s.deck)))
            else:
                # 洗过牌但没有随机流种子 ⇒ 之后的牌顶**不可知**（宁可不猜，也别按旧牌序抽）
                s.deck, s.deck_known = [], False
    _res_eff(s, e)


def _apply_unit_eff(s: Sim, uid, eff: dict) -> None:
    """**逐牌效果**（`unit_eff`：情报触发 `OnIntelTriggered` 这类"某张卡自己响应"的效果）。

    - `buff/armor/give/heal/attack_turn/opcost` 等 → 作用于**触发的那张卡自己**；
    - `damage/destroy/pin/retreat`（对敌方向）→ 作用于**敌方场上价值最高**的单位
      （BP 里如 INNISKILLING FUSILIERS 是"随机一个敌方场上卡"——用最优目标近似随机期望；
      敌方场上没单位时按 BP 的 `GetRandomCard` 空集合语义**不结算**）；
    - 其余（`spawn/draw/kredit/slot/…`）走全局 `_apply_eff`。
    """
    if not isinstance(eff, dict):
        return
    e = {k: v for k, v in eff.items() if k not in ("uncertain", "chance", "_src")}
    hostile = {}
    for k in ("damage", "destroy", "pin", "retreat"):
        if e.get(k):
            hostile[k] = e.pop(k)
    if e:
        _apply_eff(s, e, uid if uid in s.units else None)
    if hostile:
        foes = [u for u in s.units.values() if u.side == ENEMY]
        if foes:
            t = max(foes, key=_unit_value)
            _apply_eff(s, hostile, t.id)


def _event_fx_apply(s: Sim, kind: str, card_id) -> None:
    """把**预计算的钩子后果** `Sim.event_fx[kind][card_id]` 按逐牌效果语义结算掉。

    这一层是"某事件发生 ⇒ 别的牌看到之后的反应"的唯一消费点（上线 0x32 / 被抽到 0x2A /
    被压制 0x3A / 被定住 0x3D …）；没命中就什么都不做（预计算侧已经记过缺口）。
    """
    fx = (s.event_fx.get(kind) or {}).get(card_id)
    if fx:
        _apply_unit_eff(s, card_id, fx)


def _salvage_one(s: Sim, cid) -> None:
    """（规则本体已迁到 `sim/effects.py::apply_salvage`；这里保留旧名转调，行为一字不改。

    `hand_cap` 由评估侧传（它原先直接读权重表 `WEIGHTS["hand_cap"]` —— 规则读权重正是 §3.1 要避免的）。
    """
    from sim.effects import apply_salvage
    apply_salvage(s, cid, hand_cap=WEIGHTS["hand_cap"])


def sim_order(sim: Sim, hand_id: int, target: Optional[int] = None, eff: Optional[dict] = None) -> Sim:
    """出一张指令。效果取 `eff`（选择路径自带的）> 「这一对（牌, 目标）空跑出的效果」> 牌自己的效果摘要。"""
    s = sim.copy()
    c = s.hand.pop(hand_id, None)
    if c is None:
        return s
    s.kredits -= c.cost
    _apply_eff(s, eff if eff is not None else sim.pair_eff.get((hand_id, target), c.eff), target)
    return s


def _eff_of(sim: Sim, a: A):
    """部署/出指令这一步的效果摘要（路径自带 > 这一对空跑出的 > 牌自己的）。"""
    if a.kind not in ("deploy", "order"):
        return None
    c = sim.hand.get(a.src)
    if c is None:
        return None
    return a.eff if a.eff is not None else sim.pair_eff.get((a.src, a.dst), c.eff)


def _choice_branches(e):
    """效果摘要里的**我方抉择分支**（`outcomes_mode == "max"`，来自 VM 对 `WhichChooseOne` 的枚举）→ `[分支效果]`；
    不是抉择返回 None。随机分支（没有 `outcomes_mode`）不是我们能选的，**不在这里**（M1 清理：随机由 sim 按种子算掉）。"""
    if not isinstance(e, dict) or e.get("outcomes_mode") != "max":
        return None
    outs = e.get("outcomes") or []
    if len(outs) < 2:
        return None
    e0 = {k: v for k, v in e.items() if k not in ("outcomes", "outcomes_mode")}
    return [dict(e0, **eff_i) for _w, eff_i in outs]


def _has_key(obj, key) -> bool:
    """嵌套的效果摘要/钩子后果表里有没有（真值的）`key`。"""
    if isinstance(obj, dict):
        return any((k == key and bool(v)) or _has_key(v, key) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_has_key(x, key) for x in obj)
    return False


def _strip_key(obj, key):
    """深拷贝并去掉嵌套里的 `key`（用于"提示已经答完"之后重放该钩子后果）。"""
    if isinstance(obj, dict):
        return {k: _strip_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [_strip_key(x, key) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_key(x, key) for x in obj)
    return obj


def _hook_fx_slot(sim: Sim, a: A):
    """攻击/移动动作上挂着的预计算钩子后果表项 → `(容器名, 键, 值)`；没有 ⇒ None。
    攻击 = `attack_fx[(攻击者, 目标)]`；上线 = `event_fx["move"][单位]`。（总纲 §1：移动、攻击触发的提示也是同一个决策。）"""
    if a.kind == "attack":
        v = (sim.attack_fx or {}).get((a.src, a.dst))
        return ("attack_fx", (a.src, a.dst), v) if v else None
    if a.kind == "move":
        v = (sim.event_fx.get("move") or {}).get(a.src)
        return ("event_fx.move", a.src, v) if v else None
    return None


def _without_forecast(sim: Sim, a: A) -> Sim:
    """攻击/上线：把钩子后果里的 `forecast` 标记去掉（提示已答完）后再结算该动作。"""
    s2 = sim.copy()
    slot = _hook_fx_slot(sim, a)
    if slot:
        name, key, v = slot
        if name == "attack_fx":
            s2.attack_fx = dict(sim.attack_fx)
            s2.attack_fx[key] = _strip_key(v, "forecast")
        else:
            s2.event_fx = {k: dict(x) for k, x in sim.event_fx.items()}
            s2.event_fx["move"][key] = _strip_key(v, "forecast")
    return s2


def _forecast_pending(sim: Sim, a: A) -> bool:
    if not sim.forecast:
        return False
    if a.kind in ("deploy", "order"):
        e = _eff_of(sim, a)
        return isinstance(e, dict) and bool(e.get("forecast"))
    slot = _hook_fx_slot(sim, a)
    return bool(slot and _has_key(slot[2], "forecast"))


def _spawn_pending(sim: Sim, a: A) -> bool:
    if not sim.spawn_pick or a.kind not in ("deploy", "order") or not sim.spawn_pick.get(a.src):
        return False
    e = _eff_of(sim, a)
    return isinstance(e, dict) and bool(e.get("choose_spawn"))


def prompt_of(sim: Sim, a: A):
    """这个行动会不会在执行中**挂起一个提示**？是 ⇒ 返回第一层 `Prompt`（不执行），否则 None。"""
    from sim.prompt import Option, Prompt
    e = _eff_of(sim, a)
    br = _choice_branches(e)
    if br is not None:
        return Prompt("choose_one", tuple(Option(i, payload=eff_i) for i, eff_i in enumerate(br)), source=a.src)
    if isinstance(e, dict) and e.get("hand_target_pending"):
        sus = hand_target_suspended(sim, a.src)
        if sus is not None:
            return sus.prompt
    if _spawn_pending(sim, a):
        return Prompt("select_card_to_draw", tuple(Option(c["name"], label=c["name"], payload=c) for c in sim.spawn_pick[a.src]),
                      source=a.src, layer=1, meta={"spawn": True})
    if _forecast_pending(sim, a):
        return Prompt("select_card_to_draw", tuple(Option(w) for w in ("sunny", "rain", "storm") if w in sim.forecast),
                      source=a.src, layer=1, meta={"forecast": True})
    return None


def hand_target_suspended(sim: Sim, instigator_id):
    """手牌目标提示：候选 = 手里的牌（表 `sim.hand_target_fx[发起牌]` 里有效果的）；`resume(手牌 id)` 把该候选的效果应用上。
    没有候选表 ⇒ None。**不管提示是我们的动作引发还是回合开始钩子引发**，都是这一个入口（`policy.forced` 也用它）。"""
    from sim.prompt import Done, Option, Prompt, Suspended
    fx = (sim.hand_target_fx or {}).get(instigator_id)
    if not fx:
        return None
    opts = tuple(Option(cid, payload=eff) for cid, eff in fx.items() if cid in sim.hand and cid != instigator_id)
    if not opts:
        return None

    def resume(cid):
        s2 = sim.copy()
        _apply_eff(s2, dict(fx[cid]), cid)
        return Done(s2)
    return Suspended(Prompt("hand_target", opts, source=instigator_id), resume)


def run(sim: Sim, a: A):
    """执行一个行动，遇到需要我们做选择的点就挂起（`sim.prompt`）：`Done` | `Suspended`。

    * 抉择牌：`choose_one`，候选 = VM 枚举出的各分支；`resume(i)` 把第 i 个分支的效果应用到（刚部署的）牌上；
    * 预报：`select_card_to_draw` 两层——第一层 3 种天气，第二层 3 个变体（候选表 `sim.forecast` 是**按种子预测好的确定结果**）；
      选完把"下回合开始进手牌的牌"写进 `pending_cards`（原版：写进变量、下回合开始才 spawn，弯路 #26）；
    * 没有提示 ⇒ 直接 `Done(apply 的结果)`。
    """
    from sim.prompt import Done, Option, Prompt, Suspended
    pr = prompt_of(sim, a)
    if pr is None:
        return Done(_apply_action(sim, a))
    e = _eff_of(sim, a) or {}
    if pr.kind == "hand_target":
        e0 = {k: v for k, v in e.items() if k != "hand_target_pending"}
        return hand_target_suspended(_apply_action(sim, a, eff=e0), a.src)
    if pr.kind == "choose_one":
        branches = {o.key: o.payload for o in pr.options}
        return Suspended(pr, lambda k: Done(_apply_action(sim, a, eff=branches[k], on_self=True)))
    if pr.meta.get("spawn"):
        # 三选一加入手牌：选中的牌**立刻**进手牌（和预报不同——预报写进变量、下回合开始才 spawn）
        e0 = {k: v for k, v in e.items() if k != "choose_spawn"}
        cands = {o.key: o.payload for o in pr.options}

        def resume_spawn(name):
            s2 = _apply_action(sim, a, eff=e0)
            c = cands[name]
            if len(s2.hand) < WEIGHTS["hand_cap"]:
                nid = -(6000 + s2.tmp_seq)
                s2.tmp_seq += 1
                s2.hand[nid] = H(nid, c["name"], c.get("cost") or 0, c.get("typ") or "infantry",
                                 c.get("atk") or 0, c.get("dfn") or 0, c.get("kw") or (), {})
            return Done(s2)
        return Suspended(pr, resume_spawn)
    # 预报（打出/部署/攻击/上线 触发的都是同一个两层提示）
    if a.kind in ("deploy", "order"):
        e0 = {k: v for k, v in e.items() if k != "forecast"}
        apply_clean = lambda: _apply_action(sim, a, eff=e0)           # noqa: E731
    else:
        apply_clean = lambda: _apply_action(_without_forecast(sim, a), a)   # noqa: E731

    def layer1(w):
        row = sim.forecast[w]
        pr2 = Prompt("select_card_to_draw", tuple(Option(t, label=row[t][0], payload=row[t]) for t in ("light", "medium", "heavy") if t in row),
                     source=a.src, layer=2, meta={"forecast": True, "weather": w})

        def layer2(t):
            name, hint = row[t]
            s2 = apply_clean()
            s2.pending_cards.append((name, hint))
            return Done(s2)
        return Suspended(pr2, layer2)
    return Suspended(pr, layer1)


def _apply_action(sim: Sim, a: A, eff=None, on_self: bool = False) -> Sim:
    """一个行动 + **动作边界**：行动本身结算完之后冲刷 autoplay 待打出队列（原版 `ExecuteAutoPlayCards`）。"""
    s = _apply_action_raw(sim, a, eff, on_self)
    if s.autoplay_queue:
        from sim.chain import flush_autoplay
        flush_autoplay(s, _apply_eff)
    return s


def _apply_action_raw(sim: Sim, a: A, eff=None, on_self: bool = False) -> Sim:
    """`apply` 的本体（不管提示）：`eff` 覆盖效果；`on_self` ⇒ 部署的效果落在刚部署的这张牌自己身上。"""
    if a.kind == "attack":
        return sim_attack(sim, a.src, None if a.dst == "hq" else a.dst, hq=(a.dst == "hq"))
    if a.kind == "move":
        return sim_move(sim, a.src)
    if a.kind == "deploy":
        c = sim.hand.get(a.src)
        if c is None:
            return sim.copy()
        s2 = sim_deploy(sim, -(a.src + 5000), c.atk, c.dfn, c.cost, c.typ, c.kw, hand_id=a.src)
        e = eff if eff is not None else _eff_of(sim, a)
        # 部署效果（资源类：霍尔姆的下回合扣费…；带目标的：这一对空跑出的效果）；选择路径展开出来的部署
        # （5th RANGERS 的 +4/+4 / 行动费 0）效果落在**刚部署的这张牌自己**身上
        tgt = a.dst if (a.dst is not None or not on_self) else -(a.src + 5000)
        _apply_eff(s2, e, tgt)
        return s2
    if a.kind == "order":
        return sim_order(sim, a.src, a.dst, eff if eff is not None else a.eff)
    return sim.copy()


def apply(sim: Sim, a: A) -> Sim:
    """应用一个行动。带 `path`（提示答案）的行动按路径一路 `resume`；没有路径但会挂起提示 ⇒ 旧行为（`_apply_eff` 里的
    `outcomes` 分支取最好，过渡口，M1 收尾时删）。"""
    if a.path:
        from sim.prompt import Done, resolve
        r = resolve(run(sim, a), a.path)
        if isinstance(r, Done):
            return r.state
        raise ValueError("行动路径 %r 没走完（仍挂起 %s）" % (a.path, r.prompt.kind))
    return _apply_action(sim, a)


def _fx_stopped(sim: Sim, attacker: int, dst) -> bool:
    """这一对攻击会被 0x1F 吞掉（StopAttack：既不付费也不结算）⇒ 搜索里没必要生成。"""
    fx = _fx_for(sim, attacker, dst)
    return bool(fx and fx.get("stop"))


def _restricted(sim: Sim, rtype: int) -> bool:
    """我方（local）是否被对局限制 `rtype` 卡住（`GameplayRestrictionEffects` 的影子）。

    类型表（从 BP 调用点反推，见 effectvm.RESTRICTION_VERBS）：
      2=cannotPlayOrders, 3=cannotDeployUnits, 4=cannotAttackWithGroundUnits。
    只约束我方动作——对手的限制只影响对手（`gen_actions` 本来就不生成对手动作）。
    """
    return any(r.get("side") == LOCAL and int(r.get("type", -1)) == rtype
               for r in sim.restrictions)
