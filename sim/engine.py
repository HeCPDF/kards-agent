#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim.engine —— L2 规则引擎：谁能打谁 / 动作转移（攻击·部署·上线·出指令·回合结束）/ 效果摘要结算 / 钩子后果结算。

2026-10-03 起从 `policy/boardeval.py` 物理迁入（函数体一字不改，只改名字来源；★ 2026-10-04 P5：门面**已删**）；**不得** import `evaluation` / `policy` / `player.rule`。
一个与"价值"耦合的过渡口，**显式注入而不是 import**（待"返回概率分支"重构后删除）：
  * `WEIGHTS`：只剩**估值参数**（`draw_v` 匿名牌持有价值）；游戏规则常量（手牌上限）已改走 `engine.natives.board.HAND_CAP` ✓；
  * `_unit_value` / `set_unit_valuer`：选目标启发式（"打最值钱的敌人"）要一张牌的价值。
  ★ 两者的注入接缝是 **`policy.search.wire_sim()`**（幂等；L4 是唯一同时认识 L2/L3 的层 ✓）——**不是** import 任何模块的副作用 ✓。

★ 2026-10-03 清掉两处死代码（P3 卫生）：`_unit_value` 曾**重复定义两次**（一模一样，后者覆盖前者）；
  `_evaluate` 引用的 `_evaluator` **从未定义**、也没有任何调用方（一调就 NameError）——随它的门面口一起删除
  （`evaluate` 的注入口在 M1 就删了，sim 不再给终态打分）。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 原版规则端口（P2，2026-10-03）：棋盘原生函数在 engine.natives.board；`row_full` 仍从本模块 re-export
# （`policy.search` 的旧 import 不变）。
from engine.natives.board import HAND_CAP, can_spawn_card, row_full                  # noqa: F401
from engine.natives.bond import (active_bond_factions, bond_check_on_play,        # noqa: F401（rule 经此取读侧）
                                 set_active_bonds_at_start_of_turn)
from engine.natives import deck as _deck_n                                         # 造牌/搬牌（字典路/直跑/重放共用）
from engine.natives import abilities as _abil_n                                    # 自定义能力 + giver 账（三条路共用）
from engine import deferred as _DF                                                 # 延迟/常驻效果登记表（布防 + 事件点触发）
from engine.natives.kredits import change_kredits, change_slots
from engine.natives.stats import clamp_armor, clamp_stat                            # P3 第一族：EChangeType/夹取单一来源
from sim.state import (ABILITIES_KEYWORDS, DRAW_CAP, EVENT_FX_KINDS, GROUND, NO_RETALIATION, UNIT_TYPES,
                       H, Sim, U)

WEIGHTS = {"draw_v": 1.2}      # ★ 只剩**估值参数**（匿名牌持有价值）；门面会把它换成评估侧的 W（同一个 dict）。
#   `hand_cap`（手牌上限 9）是**游戏规则常量**，已改用 `engine.natives.board.HAND_CAP`（= `kardsmem.gamemodel.HAND_CAP`）——
#   规则常量不该从权重表读（§3.1）；估计参数与规则常量分开，是 P5"policy 直接在 engine 状态上展开"的一小步。
_unit_valuer = None


def set_estimation_params(draw_v=None) -> None:
    """只设置**估值参数**（`draw_v`：匿名牌的持有价值）。

    ★ P5（2026-10-04）：把 wiring 从"整份替换 `WEIGHTS = W`"收成"只设估值参数" —— 规则常量
      （手牌上限）已改走 `HAND_CAP` ✓，整份替换还会把评估侧的其它键**顺手带进来**（没人读，但容易误导 ✓）。
    调用方 = `policy.search.wire_sim()`（它有权同时 import `evaluation` 与 `sim` ✓）；**幂等** ✓。
    """
    global WEIGHTS
    if draw_v is not None:
        WEIGHTS = {"draw_v": float(draw_v)}
        for _k, _v in _ESTIMATION_PARAMS.items():
            WEIGHTS.setdefault(_k, _v)


#: 估值参数的**中性默认**（没人注入时也能跑；真值由 `wire_sim()` 给 ✓）。
_ESTIMATION_PARAMS = {"draw_v": 1.2}


def set_unit_valuer(fn) -> None:
    """门面绑定 `unit_value(u) -> 价值`（"打最值钱的那个敌人"这类**选目标启发式**用）。过渡口：
    目标选择该移到策略层（M3），移走后删。（`evaluate` 的注入口已在 M1 删除——sim 不再给终态打分。）"""
    global _unit_valuer
    _unit_valuer = fn


def _unit_value(u) -> float:
    if _unit_valuer is None:
        raise RuntimeError("sim.engine 没有绑定 unit_value；先调用 policy.search.wire_sim()（P5：门面已删，"
                           "wiring 的唯一接缝）或直接 set_unit_valuer(…)")
    return _unit_valuer(u)


def _apply_buff_field(obj, buff_attr: str, total_attr: str, v) -> None:
    """把**独立的 buff 累加器**设成 `v`（原版 `setAndEncryptAttackBuff` / `setAndEncryptKreditBuff`）。

    原版：buff 累加器是**单独字段**，叶子里写的是它的**绝对值**、**不夹取**（`BP_CardFunctions.cpp`
    的 `ChangeAttack` 0/4 分支，`:11128-11149` / `:11185-11208`）；而**总量** `getTotal*` 才夹到 `[0,99]`。

    本模拟里 `total_attr`（`U.atk` / `H.cost`）是唯一被读的**总量**，所以：
    `基础值` 不变 ⇒ `总量 += (新 buff − 旧 buff)`，再按原版夹取。
    ★ 已知边界（如实记，不假装精确）：若总量此前**已经顶到 99**（夹过），从总量反推不出基础值，
    此时"加 delta"与"clamp(基础+新buff)"可能不等（只在攻/费 ≥99 的极端局面上出现）；
    真要用到那种局面，得把基础值也存成字段（见 `docs/P3-NATIVE-PORT.md` R10）。
    """
    old = getattr(obj, buff_attr)
    new = int(v)
    setattr(obj, buff_attr, new)
    setattr(obj, total_attr, clamp_stat(getattr(obj, total_attr) + (new - old)))


# ---------------------------------------------------------------------------
# 规则：谁能打谁
# ---------------------------------------------------------------------------
def _tkind(t: U) -> str:
    return "front" if t.row == "frontline" else "support"


def _rows_ok(a: U, tk: str) -> bool:
    """能不能打到"那一行/总部"的目标 —— **原版 `cardsCheckFunctions::CanAttack`（`:185-204`）**：

        if (attacker.location != 7 /*Board_Frontline*/
            && defender.location != 7   /*目标不在前线*/
            && attacker.range < 2):
            canAttack = false; failReason = "not_enough_range"

    说人话：**`range < 2` 的单位，要么自己站在前线，要么只能打站在前线的目标**；
    `range >= 2`（炮兵/轰炸机/战斗机那一类）不受这条限制。
    ★ 以前这里是"兵种百科"近似（步兵/坦克在支援线只能打前线、空军/炮兵"哪里都能打"）——
      那只在"兵种默认 range"下碰巧等价；现在按原版字段判（`U.rng`，快照从 `0x78` 读）。
    ★ `U.rng is None`（老 fixture 没喂 range）⇒ 退回旧表：**只在离线测试里走得到**，
      实机快照总带这个字段（`board._read_card`）。
    """
    if a.rng is None:
        if a.typ in GROUND:
            return tk == "front" if a.row == "back" else tk in ("support", "hq")
        return True
    if a.row == "frontline" or tk == "front":
        return True
    return int(a.rng) >= 2


def _interceptors(sim: Sim, row: str) -> bool:
    """**被攻击那一行**里有没有敌方战斗机（原版 `CanAttack` `:244-296` 的 `cardsInAttackedLocation`）。

    原版：只有**轰炸机**查这条（`:245`）；遍历 `cardsInAttackedLocation`，有 `type == 0x4`（战斗机）
    且**不是未揭示的隐蔽牌**的 ⇒ `fighter_protecting`。
    我们以前写的是"敌方**后排**有战斗机" —— 行取错了（打前线单位时该查**前线**），
    也没排除未揭示的隐蔽战斗机（`U` 现在没有 covert 信息，这条**如实记**为未建模）。
    """
    for u in sim.units.values():
        if u.side == sim.opp and u.typ == "fighter" and u.row == row:
            return True
    return False


def can_hit_hq(sim: Sim, a: U) -> bool:
    # ★ P3 ⑥：原版 `CanAttack`（`:206-216`）的护卫检查对 **轰炸机/炮兵**直接放行
    #   （`IsBomber() || IsArtillery()`）—— 我们以前只豁免炮兵 ⇒ **轰炸机被误拒**。
    if sim.hq_guarded and a.typ not in ("bomber", "artillery"):
        return False
    if a.typ == "bomber" and _interceptors(sim, "back"):
        return False
    return _rows_ok(a, "hq")


def can_hit_unit(sim: Sim, a: U, t: U) -> bool:
    # 烟幕：原版 `:306-332` 分成 `location_has_smokescreen`（目标是 location/HQ）与
    #   `defender_has_smokescreen`（目标是单位）两条；这里只管单位那条（HQ 的烟幕我们没建模，见 TODO）。
    # 护卫：`:206-242` 同样对 **轰炸机/炮兵**放行 —— 我们以前对单位**完全不豁免**（误拒）。
    if "smokescreen" in t.kw:
        return False
    if t.guarded and a.typ not in ("bomber", "artillery"):
        return False
    if a.typ == "bomber" and _interceptors(sim, t.row):
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


def _def_delta(s: Sim, cid, u, dd) -> None:
    """防御变化 + **原版"降到位（≤0）⇒ 摧毁"**（P3，2026-10-03）。

    原版 `ChangeDefense`（`BP_CardFunctions.cpp:11215-11696`）两条会改值的分支都带摧毁：
      * `2/3/5`（`Label_984`）：`defense = clamp(amount, 0, 99)`，随后 `getTotalDefense() == 0`
        ⇒ **`DestroyCard`**（`:11338-11347`）；
      * `1`（`Label_1995`）：`amount < 0` 时写完若 `getTotalDefense() <= 0` ⇒ **`DestroyCard`**（`:11396-11419`）。
    `0/4/6-9` 在 `ChangeDefense` 里是**拒绝**（`Label_3734`：记日志 + `qqq=false` + 返回）⇒ 防御没有 buff 通道。

    这与 `AddDefenseToMultipleCards`（`defense_aoe`）**早就在用**的口径一致（见那里的注释），
    本函数只是把同一条规则补到 `ChangeDefense` 的三条通道（`buff_ids` 群体 / 标量 `buff` / `set_defense` 绝对值）。

    `dd >= 0` 不摧毁：原版升防御那条分支的夹取下限是 **1**（`:11439`），本来就不可能落到 0。
    """
    u.dfn = clamp_stat(u.dfn + dd)
    if dd < 0 and u.dfn <= 0:
        s.units.pop(cid, None)
        _apply_death(s, cid)


def _dmg_unit(s: Sim, t: U, dmg: float, engage: bool = False) -> None:
    """（规则本体已迁到 `sim/effects.py::deal_damage`；这里保留旧名转调，行为一字不改。）

    `on_death` 注入 `_apply_death` —— 那一段要跑 0x27/0x18/打捞的**死亡链**（L1 钩子），
    sim 只做"掉血/离场"这条状态变更（§3.1 分层）。
    """
    from sim.effects import deal_damage
    deal_damage(s, t, dmg, engage=engage, on_death=_apply_death)


def _fx_apply(s: Sim, b: Optional[dict]) -> None:
    """应用一个时点的钩子后果（全局效果 + 逐牌效果）。**两种桶形状都要吃**：
    · 攻击族 `triggers.to_fx`：`{"eff": 全局, "units": [(uid, eff)]}`；
    · A5 伤害桶 `triggers._bucket_effs`：整份就是效果摘要，逐牌的挂在 `unit_eff` 键里
      （`_apply_eff` 直接认它，见 engine 里 `unit_eff` 的消费点）。
    钩子效果的目标已经在 triggers 里按记录拆好：全局的走 `_apply_eff(..., target="any")`
    （`damage_hq`/`heal_hq`/指挥点/抽牌/…），逐牌的以那张牌为目标。"""
    if not b:
        return
    if "eff" in b or "units" in b:
        if b.get("eff"):
            _apply_eff(s, dict(b["eff"], target="any"), None)
        for uid, e in b.get("units") or ():
            if uid in s.units:
                _apply_eff(s, dict(e), uid)
    else:
        _apply_eff(s, dict(b), None)


def _fx_for(sim: Sim, attacker: int, dst) -> Optional[dict]:
    return sim.attack_fx.get((attacker, dst)) if sim.attack_fx else None


def _fatigue_once(s: Sim) -> None:
    """游戏 `BP_CardFunctions::ApplyFatigueDamage` 的一次结算（空库抽牌 / 协力未满足）。

    ★ 2026-10-02：规则本体已迁到 `sim/chain.py::apply_fatigue`（规则下沉，§5 步骤 3），
      这里保留旧名**转调** —— 行为一字不改（`test_draw_chain.py` 钉住）。
    """
    from sim.chain import apply_fatigue                      # 过渡期桥（避免 import 环）
    apply_fatigue(s)


# ★ P4 第六刀（2026-10-04）：`excess` 的规则**下沉到 engine**（`engine.natives.damage.excess_split`，
#   实现一字未改）；这里保留旧名 `_excess_split` 转调，两个调用点不用改 —— **规则只写一处**。
from engine.natives.damage import excess_split as _excess_split     # noqa: E402


def _fire_armed(s: Sim, hook: str, subject=None, side=None) -> None:
    """延迟/常驻效果登记表的事件点（`engine.deferred.fire`）：桶 = 预计算的钩子后果（整份效果摘要 / `{"eff","units"}`，`_fx_apply` 两种都吃）。"""
    if s.armed:
        _DF.fire(s, hook, subject, apply=_fx_apply, side=side)


def sim_attack(sim: Sim, attacker: int, target: Optional[int], hq: bool = False) -> Sim:
    """一次攻击 + 它之后的事件点（`OnAfterOtherCardAttacks` 留下来的钩子，见 `engine.deferred`）。"""
    s = _sim_attack_body(sim, attacker, target, hq)
    if sim.armed and attacker in sim.units:
        dst = "hq" if (hq or target is None) else target
        fx = _fx_for(sim, attacker, dst)
        if fx and fx.get("switch_to") is not None:
            dst = fx["switch_to"]
        if not (fx and fx.get("stop")) and (dst == "hq" or dst in sim.units):     # 攻击真的发生了（没被吞、目标在场）
            _fire_armed(s, "OnAfterOtherCardAttacks", dst)
    return s


def sim_enemy_attack_event(sim: Sim, defender) -> Sim:
    """**对方的攻击落在我方牌 `defender` 上**这一事件（只触发留下来的钩子，**不结算战斗**）。

    搜索只替我方行动、不展开对方回合，所以 ECHELON 这类"我方单位被打时"的钩子在我方回合里永远不会自己发生；
    本函数是**显式的事件入口**——评估侧的敌方回合投影 / 测试用它；战斗结算（伤害/反击/死亡）不在这里。"""
    s = sim.copy()
    _fire_armed(s, "OnAfterOtherCardAttacks", defender)
    return s


def _sim_attack_body(sim: Sim, attacker: int, target: Optional[int], hq: bool = False) -> Sim:
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
        if dd is None:                                 # 无钩子管线结果：按 BP CalculateDamageDealt 近似（免疫 ⇒ 0；先扣重甲）
            dmg_hq = 0 if s.hq_immune.get(sim.opp) else max(a.atk - s.hq_armor.get(sim.opp, 0), 0)
        else:
            dmg_hq = max(dd, 0)
        s.hq[sim.opp] = s.hq.get(sim.opp, 0) - dmg_hq
        _fx_apply(s, bk.get("mid"))
        if a.aa_hq and not (fx and fx.get("own_after")):
            _apply_eff(s, a.aa_hq, None)               # 抽牌/回血（含随机分支的期望）
        _fx_apply(s, bk.get("after"))
        return s
    t = s.units.get(target)
    if t is None:
        return s
    if dd is not None:                                # 钩子管线给了最终伤害/阵亡 ⇒ 用它，不再自己近似
        if not t.immune:                              # ★ 免疫单位不吃伤害（原版 CalculateDamageDealt :14823）
            t.dfn -= max(_excess_split(s, a, t, dd), 0)   # ★ 原版 excess：溢出去对面总部（见 _excess_split）
        t.been_attacked = True                        # 原版字段：这一击之后目标本回合"被攻击过"
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
    # ★ P3 ⑦（2026-10-04，`CalculateDamageDealt` `:14868-14949` 逐行读）：**伏击**的完整条件 ——
    #   目标的 `getHasAmbush()` ∧ ¬`ignoreAmbush` ∧ ¬`dealer.getHasImmune()` ∧ 这一击是**攻击方**打的
    #   ∧ **¬目标本回合已被攻击过**（`:14881`）。生效后：只有"防守方的攻击 ≥ 攻击方
    #   的总防御+重甲+被动防御buff"（＝伏击能把攻击方**打死**）时，攻击方这一击才**打不出伤害**
    #   （`:14951`）；杀不掉 ⇒ 攻击方照常打（`:14948` → `Label_2888`）。
    #   ★ 我们以前漏了 `hasBeenAttackedThisTurn` 与 `getHasImmune()`（后者快照没读 ⇒ 如实未建模），
    #     而且致死判据只比 `dfn`、没加重甲 —— 现在加上重甲（被动防御buff 仍未建模）。
    _ambush = ("ambush" in t.kw) and not t.been_attacked and not getattr(a, "immune", False)
    if _ambush:
        t_eff = float(a.dfn or 0) + float(a.armor or 0)
        if t_atk >= t_eff:
            s.units.pop(attacker, None)
            _apply_death(s, attacker)
            return s
        a.dfn -= max(t_atk - a.armor, 0)
        t_atk = 0
    t.been_attacked = True                            # 这一击之后，目标本回合"被攻击过"（伏击门/首次被攻击触发用）
    shock = "shock" in a.kw
    _dmg_unit(s, t, _excess_split(s, a, t, a.atk), engage=True)   # ★ 原版 excess（同上）
    # ★ P3 ⑦：`lethal` 能力（`CalculateDamageDealt` `:15076-15095`）—— `damage > 0` ∧ 目标**不是 HQ**
    #   ∧ 伤害方有 `lethal` ⇒ 目标**直接死**（不看还剩多少防御）。判据里的 `damage` 是**扣完重甲之后**
    #   的值（`:15033-15047`），所以用 `max(a.atk - armor, 0) > 0`。
    if "lethal" in getattr(a, "ab", ()) and not t.immune \
            and max(float(a.atk or 0) - float(t.armor or 0), 0) > 0 \
            and t.id in s.units:
        s.units.pop(t.id, None)
        _apply_death(s, t.id)
    if shock and "CantLoseShock" not in getattr(a, "ab", ()):
        # ★ P3 ⑦：原版在 `ExecuteAttackCard` 里摘冲击（offset 617）的**完整条件**是
        #   `!defender.IsLocation() ∧ attacker.getHasShock() ∧ ¬attacker.HasCustomAbility("CantLoseShock")`
        #   —— 我们以前**无条件**摘，带 `CantLoseShock` 的单位会丢冲击（另：打总部本来就不摘 ✓ 我们也没摘）。
        a.kw = a.kw - {"shock"}
    elif a.typ not in NO_RETALIATION or (a.typ == "bomber" and t.typ in ("fighter", "antiair")):
        # 反击。★ 原版这两条例外都看**伤害接收方 = 攻击方**的类型（不是防守方）：
        #   `:14955-14967`：`!dealingDamageIsAttacker ∧ receiver.IsArtillery()` ⇒ 0（炮兵攻击者不吃还击）；
        #   `:14983-15005`：`!dealingDamageIsAttacker ∧ receiver.IsBomber() ∧ ¬(dealer.IsFighter() ‖ IsAntiAir())`
        #   ⇒ 0（**轰炸机攻击者**不吃还击 —— 但防守方是**战斗机/防空**时照常吃；我们只识别 fighter）。
        if t_atk > 0 and not a.immune:                # ★ 免疫单位不吃伤害（包括还击；原版 :14823 看的是接收方）
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


def sim_deploy(sim: Sim, uid: int, atk: int, dfn: int, cost: int, typ: str, kw=(),
               hand_id: Optional[int] = None) -> Sim:
    s = sim.copy()
    _h = sim.hand.get(hand_id) if hand_id is not None else None
    s.units[uid] = U(uid, sim.me, "back", atk, dfn, cost, typ, kw, sick=True,    # 部署病
                     faction=getattr(_h, "faction", None),                       # 国家随牌走（下个回合开始重算协力集合要用）
                     **({"opc": _h.opc, "opc_buff": _h.opc_buff} if getattr(_h, "opc", None) is not None else {}))   # 脚本改过的行动费随牌走
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
        # ★ P3 ⑨（2026-10-04）：原版 `MoveCardToFrontline`(`BP_CardFunctions:17560-17629`) 先过
        #   `PayMovementCost(cardID)`(`:19873-19898`)：`operationCost = getTotalOperationCost()`，
        #   **`kredits < operationCost` ⇒ `success=false` ⇒ 移动整个不发生**（`:17612`），
        #   够才 `ChangeKreditsBySide(side, -cost)` + `addOperationKreditsSpentThisTurn(cost)`。
        #   我们以前**不看够不够**（照移、指挥点变负）⇒ 这里照原版拒绝。
        if s.kredits < (u.opc or 0):
            return s
        u.row, u.moved = "frontline", True
        s.kredits -= u.opc                            # 费用 = `getTotalOperationCost()`（`:17617-17619`）✓ 与适配器 `opc` 同源
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
    _fire_armed(s, "OnEndOfTurn", None, side=s.me)    # 指令留下来的 `OnEndOfTurn`（登记表；与上面"场上牌的 0x19"同一时点）
    for u in s.units.values():                       # RemoveBuffsEndOfTurn：临时攻清零
        if getattr(u, "atk_turn", 0):
            u.atk -= u.atk_turn
            u.atk_turn = 0
    s.turn_ended = True
    return s


def sim_turn_start(sim: Sim, turn_number: Optional[int] = None, side=None) -> Sim:
    """**回合开始**（原版 `BP_Logic` 的回合开始流程 `:10010-10077`，2026-10-04 逐行读）。

    顺序（照原版，注释里带行号）：
      ① `:10010-10030` **指挥点槽 +1** —— 条件 `CanSideGainKreditSlots(side)`（`:7999`：= `¬IsThereGameplayRestriction(side, 0x1)`）
         ∧ `slot < MaxKreditsConst`（`BP_Logic` 的属性 `@0x4B4`，KARDS 的**槽上限**）；
      ② `:10032-10036` `SetKreditsAndKreditSlots(side, slot, slot, 0)` ⇒ **指挥点补满到槽数**
         （本体 `BP_CardFunctions:19543`，夹取 `[0, getMaxPossibleKredits()]` —— 与我们 `engine.natives.kredits`
         的 `[0, kredit_max]` 同一个上界 ✓）；
      ③ `:10040` `ExecuteBeforeStartOfTurnEvents()`（钩子后果由 rule 预计算进 `event_fx`）；
      ④ `:10042-10050` `CanSideDrawCards(side)` 为真 ⇒ `DrawTopCardFromDeck(side, 0, false, false, true, …)`
         —— **第 1 回合不抽**（`:6318-6336` 用 `isTutorialGame` 与回合号判）；
      ⑤ `:10052` `GiveMobilizeBonus()`；⑥ `:10054-10058` `ExecuteStartOfTurnEvents(turnNumber)`。

    ★ 未建模（如实记，不编）：`GiveMobilizeBonus`（动员加成）、`KreditCheckAndAutoBanIfNeeded`（赛制/
      备用牌检查，`:10065`）、`MaxKreditsConst` 的真值（运行时属性 `@0x4B4`，我们退到 `kredit_slot_max`，缺省 12）。
    ★ ③⑥ 的钩子后果形状与 `turn_end` 相同（`{uid: eff}`；`turn_start` 本来就在 `engine/state.py` 的
      `event_fx` 白名单里），**由 rule 侧跑 VM 预计算** —— 目前只预计算了 `turn_end`
      （`triggers.py::_turn_end_fx`）⇒ 这里拿不到东西是**尚未接**，不是"没效果"。
    """
    s = sim.copy()
    # 原版：`BP_Logic::StartTurnBySide` 开头 `SetActiveBondsAtStartOfTurn(side)`（BP_Logic.cpp:10001）——回合开始重算协力国家集合
    set_active_bonds_at_start_of_turn(s, side)
    _cap = int(getattr(s, "kredit_slot_max", 12) or 12)
    _slots = int(getattr(s, "slots", 0) or 0)
    if _slots < _cap:                                 # ① 槽 +1（`CanSideGainKreditSlots` 默认真）
        s.slots = min(_slots + 1, _cap)
    change_kredits(s, s.slots - s.kredits)            # ② 补满到槽数（`SetKreditsAndKreditSlots(side, slot, slot)`）
    _fx = getattr(s, "event_fx", None) or {}
    _sot = _fx.get("turn_start") or {}
    for uid, eff in _sot.items():
        if uid in s.units and eff:                    # ③ `ExecuteBeforeStartOfTurnEvents` 的后果（逐卡）
            _apply_unit_eff(s, uid, eff)
    _side = side if side is not None else s.me
    _fire_armed(s, "OnBeforeStartOfTurn", None, side=_side)   # ③ 指令留下来的钩子（登记表）
    if turn_number != 1:                              # ④ 抽 1 —— 第 1 回合不抽（`CanSideDrawCards`）
        from sim.chain import draw_chain
        _run = draw_chain(s, 1, apply_effect=_apply_eff, hand_cap=HAND_CAP,
                          anon_hold=WEIGHTS["draw_v"], cap=DRAW_CAP,
                          bystander_fx=(s.event_fx.get("draw") or {}).get)
        s.gaps += list(_run.gaps)
    for uid, eff in _sot.items():
        if uid in s.units and eff:                    # ⑥ `ExecuteStartOfTurnEvents(turnNumber)` 的后果（逐卡）
            _apply_unit_eff(s, uid, eff)
    _fire_armed(s, "OnStartOfTurn", None, side=_side)         # ⑥ 指令留下来的钩子（登记表）
    s.turn_started = True
    return s


def _spawn_ops(s: Sim, uid, ops) -> None:
    """对刚生成的**场上**单位逐条结算脚本的改动 `[(种类, 数值, changeType)]`（与直跑 sink 同一批 `engine.natives.stats`）。"""
    from engine.natives import stats as _st                                  # noqa: PLC0415
    for kind, n, ct in ops or ():
        u = s.units.get(uid)
        if u is None:
            return
        if kind == "attack":
            _st.change_attack(u, n, ct)
        elif kind == "opcost":
            _st.change_operation_cost(u, n, ct)
        elif kind == "armor":
            _st.change_heavy_armor(u, n, ct)
        elif kind == "defense":
            r = _st.change_defense(u, n, ct)
            if r.get("destroy"):
                s.units.pop(uid, None)
                _apply_death(s, uid)
        else:
            s.gaps.append("spawn：新牌的 %s 改动在场上没有对应原生（未结算）" % kind)


def _hand_ops(s: Sim, hid, ops) -> None:
    """对刚进手牌的新牌逐条结算脚本的改动（费用 / 行动费；其余种类手牌没有对应状态 ⇒ 记缺口）。"""
    from engine.natives import stats as _st                                  # noqa: PLC0415
    h = s.hand.get(hid) if hid is not None else None
    if h is None:
        return
    for kind, n, ct in ops or ():
        if kind == "cost":
            _st.change_kredit_cost(h, n, ct)
        elif kind == "opcost":
            if _st.change_hand_operation_cost(h, n, ct) is None:
                s.gaps.append("ChangeOperationCost：手牌的行动费基础值未知，加减型改动算不了")
        else:
            s.gaps.append("手牌新牌的 %s 改动没有对应状态（未结算）" % kind)


def _res_eff(s: Sim, e: dict) -> None:
    """资源类效果（出指令和部署单位共用）：指挥点/槽/延迟扣费/抽牌/加牌/召唤。"""
    # ★ EVAL-NATIVES §8-1：游戏的 `ChangeKreditsBySide` 等叶子写 GameState 后，BP 层把
    #   指挥点/槽夹到 [0, max]；模拟在同一处夹取（支出**不**夹——它是"扣多少"）。
    #   P2：夹取本体已端口进 `engine.natives.kredits`。
    change_kredits(s, e.get("kredit", 0))
    if e.get("kredit_next"):
        s.pending.append((1, e["kredit_next"]))
    change_slots(s, e.get("slot", 0))
    s.opp_kredits += e.get("opp_kredit", 0)
    s.opp_slots += e.get("opp_slot", 0)
    n_draw = int(e.get("draw", 0))
    # ★ 2026-10-02：抽牌链（抽到 → 抽到时效果 / autoplay → 再抽…）**已迁到
    #   `sim/chain.py::draw_chain`**（规则下沉，§5 步骤 3）。那条链原本是这里的
    #   `while` 队列；现在由事件队列驱动，语义不变：
    #     · 队列顺序 = **DFS**（新事件排队首）= 原来"钩子内部同步递归"的行为；
    #     · 上限仍是 `DRAW_CAP`（抵抗那种连抽几十张的链）；
    #     · "再抽 N 张"只入队、其余部分交给 `_apply_eff` —— 否则会被算两遍。
    #   权重（`hand_cap` / `anon_hold`）由评估侧传进去：sim 不 import 权重表。
    if e.get("gain_cards"):
        # ★ `SpawnCardInHandBySide`：手里**多一张新牌**，**不是抽牌**（原版 `SpawnCardinHandbySide`，`BP_CardFunctions.cpp:6371`：
        #   `CreateCard(位置=手牌)`，不碰牌库、不触发"抽到时"）。以前并进 `n_draw` 走抽牌链 ⇒ 把牌库顶那张抽进手牌、牌库少一张。
        #   新牌按名字造（`engine.natives.deck.spawn_in_hand`：读得到静态面板就用，否则中性占位；效果留空）。
        _names = list(e.get("gain_names") or ())
        _new_ids = []
        for _i in range(int(e["gain_cards"])):
            _nm = _names[_i] if _i < len(_names) and isinstance(_names[_i], str) and _names[_i] else "?"
            _new_ids.append(_deck_n.spawn_in_hand(s, _nm, hold=s.hold_v if s.hold_v is not None else WEIGHTS["draw_v"]))
        _gops = e.get("gain_ops") or ()
        for _hid, _ops_ in zip(_new_ids, _gops):
            _hand_ops(s, _hid, _ops_)
        _g = "SpawnCardInHandBySide：新牌的效果/ExecuteOnSpawnedInHandEvents 未建模（面板读得到才用，效果留空）"
        if _g not in s.gaps:                       # 束搜索里会反复结算同一份效果 ⇒ 缺口只记一条（`Sim.copy` 逐份复制 gaps）
            s.gaps.append(_g)
    if n_draw:
        from sim.chain import draw_chain
        _run = draw_chain(s, n_draw, apply_effect=_apply_eff, hand_cap=HAND_CAP,
                          anon_hold=WEIGHTS["draw_v"], cap=DRAW_CAP,
                          bystander_fx=(s.event_fx.get("draw") or {}).get)
        s.gaps += list(_run.gaps)
    if e.get("spawn_cards"):
        # 原版 `SpawnCardToBoard`（BP_CardFunctions，1.60 导出）：非前线 ⇒ 目标排满就**不生成**（没有指定卡 id 时）；
        # 前线 ⇒ 前线被对方占就拒绝。生成的单位用卡自己的面板（`spawn_stats`），查不到面板 ⇒ 记缺口、不编。
        for sc in e["spawn_cards"]:
            side = s.me if sc.get("mine", True) else s.opp
            row = "frontline" if sc.get("row") == "frontline" else "back"
            if not can_spawn_card(s, side, row):      # 原版 SpawnCardToBoard 的判定（排满 / 前线被对方占）
                continue
            stat = s.spawn_stats(sc["name"]) if s.spawn_stats else None
            if not stat:
                s.gaps.append("spawn：%s 的面板读不到，生成的单位没法入模拟（不编）" % sc["name"])
                continue
            s.tmp_seq += 1
            uid = -(2000 + s.tmp_seq) if side == s.me else -(2100 + s.tmp_seq)
            _kw = set(stat.get("kw", ())) | set(sc.get("kw_add") or ())     # `SpawnMultipleCardsOnBattlefield` 的 giveBlitz / 之后的 GiveX(新牌)
            s.units[uid] = U(uid, side, row, stat.get("atk", 0), stat.get("dfn", 0), stat.get("cost", 0),
                             stat.get("typ", "infantry"), tuple(sorted(_kw)) if sc.get("kw_add") else stat.get("kw", ()),
                             sick=not ("blitz" in (sc.get("kw_add") or ())))
            if side == s.me:
                for m in s.enter_mods:
                    _apply_eff(s, m, uid)
            _spawn_ops(s, uid, sc.get("ops"))      # 脚本对刚生成的这张牌的改动（`ChangeAttack(GetCardFromID(spawnedCardID))` 一类）
    else:
        for i in range(int(e.get("spawn", 0))):          # 旧摘要（没有卡名）：只能按通用 2/2，且仍受排容量限制
            if row_full(s, s.me, "back"):
                break
            uid = -(2000 + len(s.units) + i)
            s.units[uid] = U(uid, s.me, "back", 2, 2, 0, "infantry", (), sick=True)
        for i in range(int(e.get("opp_spawn", 0))):        # §15.5：对方场上加单位（对我们不利）
            if row_full(s, s.opp, "back"):
                break
            uid = -(2100 + len(s.units) + i)
            s.units[uid] = U(uid, s.opp, "back", 2, 2, 0, "infantry", (), sick=True)
    s.opp_cards += int(e.get("opp_draw", 0) + e.get("opp_gain_cards", 0))


def _heal_unit(s: Sim, t) -> None:
    """FullyHealCard（BP_CardFunctions@701，2026-10-03 读过）：门槛 = 当前总防御>0 ∧ 缺口>0；
    通过后 `defense = maxDefense`。`OnBeforeFullyRepaired`(0xC) 可否决、0x2C/`OnFullyRepaired`
    的后续钩子这里未建模（如实记缺口，不编）。"""
    if t.dfn > 0 and float(t.mdef) - t.dfn > 0:
        hfx = (s.event_fx.get("heal") or {}).get(t.id) or {}
        if not hfx.get("heal_vetoed"):                 # 0xC OnBeforeFullyRepaired 返回 stopAction ⇒ 中止
            t.dfn = float(t.mdef)
            _event_fx_apply(s, "heal", t.id)           # 0x2C / 自己的 OnFullyRepaired


def _give_kw(s: Sim, t, k) -> None:
    """`Give<关键词>`（与直跑 `_give_sink` 同口径）：集合加一项；`blitz` ⇒ `sick=False`；`fury` ⇒ 补行动次数；原先没有才广播 0x1D。"""
    _had = k in t.kw
    t.kw = t.kw | {k}
    if not _had:
        _abilities_changed(s, t, k, True)     # 原版：Give<关键词> 只在原先没有时才广播 0x1D（见 ABILITIES_KEYWORDS）
    if k == "blitz":
        t.sick = False
    if k == "fury":
        t.attacks_left = max(t.attacks_left, 2 if not t.acted else 1)


def _do_draw_items(s: Sim, items) -> None:
    """按序抽牌：`None` = 抽牌库顶；整数 = `DrawSpecificCardFromDeckBySide` 指定的卡 id（牌库里有 ⇒ 挪到牌顶再抽；没有 ⇒ 不抽）。"""
    from sim.chain import draw_chain
    for it in items:
        if it is not None:
            got = _deck_n.pull_specific_to_top(s, it)
            if got is False:
                continue
        _run = draw_chain(s, 1, apply_effect=_apply_eff, hand_cap=HAND_CAP, anon_hold=WEIGHTS["draw_v"], cap=DRAW_CAP,
                          bystander_fx=(s.event_fx.get("draw") or {}).get)
        s.gaps += list(_run.gaps)


def _apply_eff(s: Sim, e: dict, target, src=None) -> None:
    """把一份效果摘要作用到 `s` 上（原地）。`target`：目标单位 id / "hq" / None。
    `src`：这份效果的来源牌 id（打出的手牌 / 部署的牌）——效果伤害要靠它查 `event_fx["damage"][(src, 目标)]`
    （原版 `DamageCard` 的 `damagerCardID`）；不给 ⇒ 伤害按裸数值结算。"""
    s.bonus += e.get("_est", 0.0)
    if e.get("rng_draws") and s.rng is not None:
        # 这份效果在 VM 空跑时**自己**耗掉的随机流次数（`GetRandomCard` 等；游戏只有一条 `FRandomStream`）⇒ 之后的洗牌/塞牌
        # 从它们**之后**接着抽（近似："VM 的随机先发生"——准确口径是直跑，它逐动词同序耗同一条流）。
        for _ in range(int(e["rng_draws"])):
            s.rng.frand()
    _dseq = list(e.get("draw_seq") or ())
    _dpre = max(0, min(int(e.get("draw_pre", 0) or 0), len(_dseq)))
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
        _damage_card(s, t, e["damage"], src)
    for cid, n_ in (e.get("damage_ids") or {}).items():
        # 逐张的效果伤害（`effectvm.to_effects` 的 `damage_ids`：打的是**非选定目标**的某张牌；按录制顺序逐个结算，
        # 前面的击杀会让后面的目标不在场 ⇒ 跳过，与直跑 `DamageCard` 对离场目标的 no-op 同口径）
        u_ = s.units.get(cid)
        if u_ is not None:
            _damage_card(s, u_, n_, src)
    # ★ 2026-10-02：`damage_hq` 就是"对**敌方 HQ** 的伤害"（VM 里 `DamageCard(敌方HQ卡)` 现在记成
    #   它；静态摘要里也同义）——不再要求调用方额外标 target，否则 7th SCOTTISH BORDERERS 这类
    #   "自动打 HQ"的效果会被静默丢掉（用户：打伤害这么浅显，就算入）。
    if e.get("damage_hq"):
        s.hq[s.opp] = s.hq.get(s.opp, 0) - e["damage_hq"]
    if e.get("damage_own_hq"):                    # §15.5：己方 HQ 受伤（self_damage 拆键）
        s.hq[s.me] = s.hq.get(s.me, 0) - e["damage_own_hq"]
    if target == "hq" and e.get("damage"):
        s.hq[s.opp] = s.hq.get(s.opp, 0) - e["damage"]
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
        if isinstance(e.get("fights"), (list, tuple)) and e["fights"]:      # 一张牌里多次对打：按录制顺序逐对结算
            _pairs = [tuple(x) for x in e["fights"]]
            _dmgs = list(e.get("fight_dmgs") or [None] * len(_pairs))
            for (fa, fb), fd in zip(_pairs, _dmgs + [None] * (len(_pairs) - len(_dmgs))):
                s.gaps += apply_fight(s, int(fa), int(fb), deal_damage=_fight_deal(s, int(fa), int(fb), fd),
                                      kill=_apply_death, damages=fd)
        elif isinstance(fs, (list, tuple)) and len(fs) == 2:
            s.gaps += apply_fight(s, int(fs[0]), int(fs[1]), deal_damage=_fight_deal(s, int(fs[0]), int(fs[1]), e.get("fight_dmg")),
                                  kill=_apply_death, damages=e.get("fight_dmg"))
        else:
            s.gaps.append("fight：参数里没有一对 card_id（effectvm 没换出来），未结算")
    if e.get("convert"):
        # 原版：BP_CardFunctions::ConvertCard（BP_CardFunctions.cpp:12611）——整段移植在 `_apply_convert`
        cv = e["convert"] if isinstance(e["convert"], dict) else None
        if cv is None:
            s.gaps.append("convert：ConvertCard 没取到目标卡数值（effectvm 只记了布尔）⇒ 不结算")
        else:
            _apply_convert(s, cv)
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
    for cid in e.get("pin_ids", ()):               # 逐张定住（MONTY / EXHAUST ALL OPTIONS：目标**及相邻单位**）
        u_ = s.units.get(cid)
        if u_ is not None:
            u_.pinned = True
            _event_fx_apply(s, "pin", cid)
    if e.get("suppress") and t is not None:
        # 压制 ≠ 定住（BP SuppressMultipleUnits@7456：清关键词/重甲/指向税 + 置 isSuppressed）
        from sim.effects import apply_suppress
        s.gaps += apply_suppress(s, t.id)
        _event_fx_apply(s, "suppress", t.id)       # 0x3A：别人看到"这张牌被压制了"
    if e.get("unpin") and t is not None:
        t.pinned = False
    for cid in e.get("heal_ids", ()):                  # 逐张修复（CADET NURSE CORPS：所有友方单位；顺序在 buff 之前，同 BP：先 FullyHeal 再 +1+1）
        u_ = s.units.get(cid)
        if u_ is not None:
            _heal_unit(s, u_)
    _bids = e.get("buff_ids") or {}
    _buff_aoe = bool(_bids) and (len(_bids) > 1 or target is None)   # 群体 buff：逐张结算，标量 `buff` 不再套到单个目标上
    if _buff_aoe:
        for cid, (da, dd) in _bids.items():
            u = s.units.get(cid)
            if u is not None:
                # ★ P3 R11（2026-10-03）：应用增量后把**总量**夹回原版范围 —— 原版 `getTotalAttack`/
                #   `getTotalDefense` 都是 `clamp(0,99)`（IDA 0x144B14E90/0x144B14FB0，见
                #   `NATIVE-COVERAGE §14.2 #14`）。不夹就会读出 140 这种游戏里不存在的值。
                u.atk = clamp_stat(u.atk + da)
                _def_delta(s, cid, u, dd)                 # 防御降到位 ⇒ 摧毁（原版 ChangeDefense）
    # 群体"本回合 +N 攻"（HEATWAVE：所有单位 +2、空军 +3）：逐张结算，与直跑的 `AddAttackUntilEndOfTurn` sink 同一条规则
    #（`clamp(atk+n)`、`atk_turn += n`）；标量 `attack_turn` 不再套到单个目标上。
    for cid, ks in (e.get("give_ids") or {}).items():   # 逐张授予关键词（ADMIRAL YAMAMOTO：所有友方空军 +1 攻 + 闪击）
        u_ = s.units.get(cid)
        if u_ is not None:
            for k in ks:
                _give_kw(s, u_, k)
    _aids = e.get("attack_turn_ids") or {}
    _atk_aoe = bool(_aids) and (len(_aids) > 1 or target is None)
    _rids = e.get("armor_ids") or {}
    _armor_aoe = bool(_rids) and (len(_rids) > 1 or target is None)       # 群体重甲：逐张结算，标量 `armor` 不再套到单个目标上
    if _armor_aoe:
        for cid, n_ in _rids.items():
            u = s.units.get(cid)
            if u is not None:
                u.armor = clamp_armor(u.armor + n_)
    _oids = e.get("opcost_ids") or {}
    _opc_aoe = bool(_oids) and (len(_oids) > 1 or target is None)
    if _opc_aoe:
        for cid, n_ in _oids.items():
            u = s.units.get(cid)
            if u is not None:
                u.opc = clamp_stat(u.opc + n_)
    if _atk_aoe:
        for cid, n_ in _aids.items():
            u = s.units.get(cid)
            if u is not None:
                u.atk = clamp_stat(u.atk + n_)
                u.atk_turn += n_
    if t is not None:
        for k in e.get("give", ()):
            _give_kw(s, t, k)
        if e.get("buff") and not _buff_aoe:
            # 攻先夹（R11）；**防御留到本块末尾**再应用 —— 因为它可能把目标打掉（`_def_delta`），
            # 而后面的 give/heal/steal/veteran/tax 等子效果都作用于同一个 `t`，
            # 先摧毁会让那些子效果写到"已经离场"的对象上（顺序差异只在同一条效果摘要内部）。
            t.atk = clamp_stat(t.atk + e["buff"][0])
        if e.get("heal"):
            _heal_unit(s, t)
        if e.get("steal"):
            # TakeControlOfEnemyUnit → ChangeUnitOwnership（BP@18087，2026-10-03 读过）：
            #   side=我方、underEnemyControl=true、movementLeft=1、attackLeft=1（有 fury 则 2）
            #   ⇒ **偷来当回合就能动/能攻**（旧实现写 sick=True 是错的）。
            #   落点：原在前线且前线 >1 张 ⇒ 去我方后排；前线只有它 ⇒ 留在前线；原在后排 ⇒ 我方后排。
            #   ★ 2026-10-04（P4 第十七刀）：**规则本体下沉到 `engine.natives.board.apply_take_control`**，
            #     这里只转调 + 保留扇出与缺口（规则只写一处）。
            from engine.natives.board import apply_take_control
            apply_take_control(s, t, me=s.me)
            _event_fx_apply(s, "steal", t.id)                  # 离场/入场钩子（0x2E/0x8/OnEnterPlay(0)/0x2B）
            s.gaps.append("steal：我方后排满的分支 / hasActivePincerEffect / CardLocationMoved 未建模")
        if e.get("veteran"):
            # MakeVeteran（BP_CardFunctions@7127，逐行读过）：门槛 = 在场 ∧ 有 `_vet` 静态卡 ∧ 还不是老兵 ∧ 总防>0。
            #   ★ 2026-10-04（P4 第二十二刀）：**规则本体下沉到 `engine.natives.stats.apply_veteran`**
            #     （实现一字不差），这里只转调 + 保留扇出与缺口（规则只写一处）；
            #     `vet` 载荷仍是录制期用注入视图 + `<名>_vet` 静态卡算出来的（`effectvm._veteran_payload`）。
            from engine.natives.stats import apply_veteran
            _was_vet = "veteran" in t.kw
            s.gaps += apply_veteran(t, e["veteran"] if isinstance(e["veteran"], dict) else None)
            if not _was_vet and "veteran" in t.kw:          # 门槛过了才扇（原版标记后才发）
                _event_fx_apply(s, "veteran", t.id)         # OnBecomingVeteran + 0x20
        if e.get("armor") and not _armor_aoe:
            # R11：重甲的范围是 `[0,3]`（原版 IDA `getTotalHeavyArmor`），不是 `[0,99]`
            t.armor = clamp_armor(t.armor + e["armor"])
            # 原版：ChangeHeavyArmor 值变化后会广播 0x1D（BP_CardFunctions.cpp:10381），但重甲的「改变后视图」没喂给钩子 ⇒ 记缺口
            if s.event_fx.get("abilities"):
                s.gaps.append("abilities：ChangeHeavyArmor 之后的 0x1D 广播未算（重甲改变后的视图没有喂给钩子）")
        # ★ 攻击链钩子里出现的单位属性变化（ATTACK-HOOKS-1.60.md §6）
        if e.get("attack_turn") and not _atk_aoe:      # 本回合 +N 攻
            t.atk = clamp_stat(t.atk + e["attack_turn"])   # R11：总量夹 [0,99]
            t.atk_turn += e["attack_turn"]             # 单独记：这部分回合结束会消失（见 unit_value）
            #   （`atk_turn` 是我们自己的"临时部分"簿记、不是游戏字段，不跟总量一起夹；
            #     只有攻已经顶到 99 的极端局面会让 `atk − atk_turn` 偏离真实基础值。）
        if e.get("opcost") and not _opc_aoe:                           # 行动费变化
            t.opc = clamp_stat(t.opc + e["opcost"])    # R11：原版 getTotalOperationCost = clamp(0,99)
        for k, kw in (("remove_immune", "immune"), ("remove_fury", "fury"), ("remove_ambush", "ambush"),
                      ("remove_smokescreen", "smokescreen"), ("remove_alpine", "alpine"),
                      ("remove_salvage", "salvage"), ("remove_shock", "shock"),
                      ("remove_guard", "guard"), ("remove_blitz", "blitz"),
                      ("remove_mobilize", "mobilize")):
            if e.get(k):
                _had = kw in t.kw
                t.kw = t.kw - {kw}
                if _had:
                    _abilities_changed(s, t, kw, False)   # 原版：Remove<关键词> 成功后广播 0x1D（immune/alpine/salvage 不广播）
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
        if e.get("buff") and not _buff_aoe:
            _def_delta(s, t.id, t, e["buff"][1])       # 见上面那条：防御放在本块最后应用
    if t is not None and e.get("reset_ops"):          # ResetUnitOperations：行动次数重置
        t.attacks_left = max(t.attacks_left, 2 if "fury" in t.kw else 1)
        t.acted, t.moved = False, False
    if _dseq:
        # 抽牌按**脚本顺序**结算（只相对牌库改动排序；与其它效果的相对顺序保持旧口径：都在它们之后）：先于牌库改动的
        # （DEFEND THE NATION 先抽再洗）放在牌库段**之前**，其余放在牌库段之后；`DrawSpecificCardFromDeckBySide` 抽**指定**的
        # 那张（牌库里有才抽）。`draw` 里已被这里结算的部分从 `_res_eff` 扣掉。
        _do_draw_items(s, _dseq[:_dpre])
        e = dict(e)
        e["draw"] = max(0, int(e.get("draw", 0)) - len(_dseq))
    if e.get("to_deck") and target in s.hand:
        # 手牌目标提示（175th / PBY CATALINA）：`MoveCardToTopOfOwnersDeck(选中的手牌)` ⇒ 手里少这张、牌库顶多这张（下回合必抽到它）
        _deck_n.move_hand_to_deck(s, target, 0)
    if t is not None and e.get("to_deck") and not e.get("remove_unit"):
        # `MoveCardToTopOfOwnersDeck` 的场上单位分支（GROUNDED / HMS BELFAST）：规则本体在 `engine.natives.board.unit_to_deck_top`
        # （与直跑 sink / 重放同一个原生：离场 + 我方单位回我方牌库顶；对方单位只离场）。缺口不并进 `s.gaps`（沿用旧行为：这里一向静默）。
        from engine.natives.board import unit_to_deck_top
        unit_to_deck_top(s, t.id, 0)
    elif t is not None and e.get("remove_unit"):
        s.units.pop(t.id, None)                       # 离场（不算摧毁）
    if e.get("end_match") in (s.me, s.opp):              # EndMatch(winnerSide)：直接分出胜负（致命一击/致命败北）
        s.hq[s.opp if e["end_match"] == s.me else s.me] = 0
    if e.get("heal_hq"):
        s.hq[s.me] = s.hq.get(s.me, 0) + e["heal_hq"]
    if e.get("heal_opp_hq"):                       # 敌方总部加/减防（`ChangeDefense(敌方总部牌)`；负数 = 减）
        s.hq[s.opp] = s.hq.get(s.opp, 0) + e["heal_opp_hq"]
    for cid in e.get("discard_ids", ()):           # DiscardCardFromHand：己方手牌里有这张就移走（敌方的不建模）
        s.hand.pop(cid, None)
    if e.get("playing_side"):                      # SetPlayingSide/SetActiveSide：行动权换边
        s.playing_side = e["playing_side"]
    # 协力（Bond）的"打出时疲劳伤害"已不在效果字典里（旧键 `bond_fatigue` 已删）：它是 `CardPlayedFromHand` 本身的一步，
    # 由 `bond_check_on_play`（engine/natives/bond.py）在 `sim_order` / 部署路径里原生执行。
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
    # ★ P3 第一族（R3/R10，2026-10-03）：这里的"绝对值"要分清是**基础字段**还是**总量**——
    #   原版的攻/费是**两个字段**（`attack`+`attackBuff`、`kredit`+`kreditBuff`），
    #   `setAndEncryptAttack/Kredit` 写**基础**，总量 = `clamp(基础 + buff, 0, 99)`
    #   （IDA：getTotal* 都是 clamp(0,99)）；`setAndEncryptAttackBuff/KreditBuff` 写**buff 累加器本身**
    #   （原版**不夹取**它，`:11128-11149`）。见 `engine/state.py::U.atk_buff` 的不变量。
    #   防御**没有**独立 buff 字段（`ChangeDefense` 只写 `defense`/`maxDefense`）⇒ 仍是单字段。
    for cid, v in (e.get("set_defense") or {}).items():
        u = s.units.get(cid)
        if u is not None:
            # 绝对值 ⇒ 转成增量，走同一条"降到位 ⇒ 摧毁"（原版 `Label_984`：设完 `== 0` 就 DestroyCard）
            _def_delta(s, cid, u, clamp_stat(v) - u.dfn)
    for cid, v in (e.get("set_attack") or {}).items():
        u = s.units.get(cid)
        if u is not None:
            u.atk = clamp_stat(int(v) + u.atk_buff)          # 设的是基础值 ⇒ 总量 = clamp(基础 + buff)
    for cid, v in (e.get("set_kredit") or {}).items():
        h = s.hand.get(cid)
        if h is not None:
            h.cost = clamp_stat(int(v) + h.cost_buff)
    for cid, v in (e.get("set_attack_buff") or {}).items():
        u = s.units.get(cid)
        if u is not None:
            _apply_buff_field(u, "atk_buff", "atk", v)       # 写的是 buff 累加器（不夹取 buff 本身）
    for cid, v in (e.get("set_kredit_buff") or {}).items():
        h = s.hand.get(cid)
        if h is not None:
            _apply_buff_field(h, "cost_buff", "cost", v)
    # ★ P3（2026-10-03）：`ChangeKreditCost`（手牌费用改动）以前**没有消费者** —— 旧键 `cost`
    #   是**标量**，而 `_apply_eff` 的 `target` 是场上单位/总部、手牌不是 target ⇒ 套不上，
    #   效果被整个丢掉（产出键审计也扫不到：通用兜底用的是变量键）。
    #   现在 `effectvm` 发**逐卡字典** `cost_ids: {card_id: 增量}`，这里落到 `Hand.cost`（按原版夹 [0,99]）。
    for cid, d in (e.get("cost_ids") or {}).items():
        h = s.hand.get(cid)
        if h is not None:
            h.cost = clamp_stat(float(h.cost) + float(d))
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
            if cid in s.hand:                      # 手牌洗回/放回牌库（EDGE OF THE EMPIRE / REDEPLOYMENT：`MoveMultipleCardsToTopOfOwnersDeck(手牌 id)`）
                _deck_n.move_hand_to_deck(s, cid, pos)
                continue
            u = s.units.get(cid)
            if u is None:
                continue
            s.units.pop(cid, None)
            if u.side == s.me and s.deck_known:
                s.deck.insert(max(0, min(pos, len(s.deck))), cid)
    for cid in e.get("to_deck_hand_ids", ()):      # `MoveCardToTopOfOwnersDeck(手牌 id)`（SHIFTING DOCTRINE：随机一张手牌）；只认**在手牌里**的，场上单位走原路
        if cid in s.hand:
            _deck_n.move_hand_to_deck(s, cid, int(e.get("to_deck_position") or 0))
    if e.get("damage_aoe") and e.get("damage_aoe_ids"):
        amt = float(e["damage_aoe"])
        for cid in e["damage_aoe_ids"]:
            u = s.units.get(cid)
            if u is not None:
                _dmg_unit(s, u, amt)
    for _nm, _cid, _giver in e.get("ability_grants", ()):   # CustomAbilityAdd(名, 单位 id, 授予者 id)：计数 + giver 账（与直跑 sink 同一原生）
        _abil_n.grant_ability(s, _cid, _nm, _giver)
    for r in e.get("restriction_add", ()):         # AddGameplayRestriction：影子列表
        s.restrictions.append(dict(r))
    for r in e.get("restriction_remove", ()):      # RemoveGameplayRestriction：同 side+type 移除
        s.restrictions = [x for x in s.restrictions
                          if not (x.get("side") == r.get("side") and x.get("type") == r.get("type"))]
    if e.get("steal_to_deck") and t is not None:
        # StealCardFromBoardToDeck：目标离场 → 同名复制进 deckSide 的牌库 → 洗牌（同一效果里的
        # `deck_shuffle` 由下面那段执行）。只对**己方**牌库建模（敌方牌序我们读不到、也不关心）。
        if e["steal_to_deck"] == s.me and s.deck_known:
            tpl = s.card_templates.get(t.id)
            s.deck.append(t.id)
            if tpl is not None:
                s.deck_cards.setdefault(t.id, tpl)
        s.units.pop(t.id, None)
    if e.get("move_front") and t is not None and t.row == "back":
        # MoveUnitFromSupportToFrontLine：效果导致的移动，**不花行动费、不算"移动过"**
        # （BP 直接 MoveCardToFrontline）。前线属于对方时 BP 拒绝执行。
        if s.front_owner != s.opp:
            t.row = "frontline"
    _add_shuffled_n = 0
    if e.get("deck_add") and e.get("deck_add_side") == s.me and s.deck_known:
        # SpawnCardInDeckBySide：n 张新牌进己方牌库（按 BP 的 addToTop=!bottom / 随机位置）。
        # ★ 每张牌**都要抽一次** `RandomIntFromRangeWithStream(0, len)`（BP 在循环里无条件调用，
        #   结果只在 RandomWithoutShuffle 时用作插入位置）——不抽就会让之后的洗牌/抽牌跟游戏错位。
        # 实现已收进 `engine.natives.deck`（字典路/直跑/重放共用同一份）。
        _adds = e.get("deck_adds") or [{"name": e.get("deck_add_name"), "n": int(e["deck_add"]),
                                        "bottom": bool(e.get("deck_add_bottom")),
                                        "wo_shuffle": bool(e.get("deck_add_wo_shuffle")),
                                        "shuffle": bool(e.get("deck_add_shuffle"))}]
        for _ad in _adds:                          # 逐次结算（每次各自的卡名/位置口径/是否洗牌）
            _deck_n.spawn_in_deck(s, _ad.get("name") or "?", int(_ad["n"]), bottom=bool(_ad.get("bottom")),
                                  random_wo_shuffle=bool(_ad.get("wo_shuffle")), rng=s.rng,
                                  hold=s.hold_v if s.hold_v is not None else WEIGHTS["draw_v"])
            if _ad.get("shuffle"):
                _add_shuffled_n += 1               # 这一次洗牌已经在塞牌函数体里做了（见下面 `deck_shuffles` 的扣减）
                _deck_n.shuffle_deck(s, s.rng)
    if e.get("deck_shuffle"):                      # ShuffleDeckBySide：洗**整副牌**（游戏：Array_ShuffleFromStream）
        # 洗几遍：`deck_shuffles`（`effectvm.to_effects` 记，每个 `ShuffleDeckBySide` / 带 shuffle 的塞牌 / 洗入牌库各 +1）；
        # 没记（手写 eff / 旧缓存）⇒ 按有 `deck_shuffle` 就一遍。塞牌函数体里洗掉的那一遍（`_add_shuffled`）要扣掉，
        # 否则同一次 `SpawnCardInDeckBySide(shuffle=true)` 洗两遍（原版只洗一遍）。
        _n_sh = int(e["deck_shuffles"]) if "deck_shuffles" in e else 1
        _n_sh -= _add_shuffled_n
        if e["deck_shuffle"] == s.me and s.deck:
            for _ in range(max(0, _n_sh)):
                # ★ 洗的是**id 列表**（与游戏 `SetDeckBySide` 同构）：`Stream.shuffle` = 正向 Fisher-Yates；
                #   没有随机流种子 ⇒ 牌序**不可知**（宁可不猜，也别按旧牌序抽；`shuffle_deck` 已标未知并记缺口）
                if not _deck_n.shuffle_deck(s, s.rng):
                    break
    if _dseq:
        _do_draw_items(s, _dseq[_dpre:])
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
        foes = [u for u in s.units.values() if u.side == s.opp]
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


def _damage_card(s: Sim, t: U, dmg, src) -> None:
    """效果伤害（`DamageCard`）。有预计算的 `event_fx["damage"][(来源牌 id, 目标 id)]` 且基数对得上 ⇒ 按原版链结算：
    改伤钩子（桶 calc）→ **最终伤害 final**（可能 ≠ 裸数值：+伤害 / 免疫 ⇒ 0）→ 受伤通知（final>0，桶 recv）→ 扣防（含死亡链）→
    造成伤害通知（桶 dealt）。没有条目 ⇒ 裸数值结算（旧行为）。

    原版：DamageCard（BP_CardFunctions.cpp:895）→ ExecuteOnDealDamageAddDamage（:946）→ ExecuteOnDealDamageAddDamageAfterCalc（:917）
    → ApplyDamageToCard（:919 / :16375：ExecuteBeforeReceiveDamage@16399 → 扣防@16461 → ExecuteOnCardDealDamageEffects@16508 → 摧毁链）。
    重甲**不**参与（重甲在 CalculateDamageDealt，效果伤害不经过它）。"""
    fx = (s.event_fx.get("damage") or {}).get((src, t.id)) if src is not None else None
    if not fx:
        _dmg_unit(s, t, dmg)
        return
    if fx.get("final") is None or fx.get("amount") != dmg:
        for g in fx.get("gaps") or ():
            s.gaps.append("damage：" + g)
        if fx.get("final") is not None:
            s.gaps.append("damage：预计算的伤害基数(%s)与本次效果(%s)对不上，按裸数值结算" % (fx.get("amount"), dmg))
        _dmg_unit(s, t, dmg)
        return
    bk = fx.get("buckets") or {}
    final = int(fx["final"])
    _fx_apply(s, bk.get("calc"))
    if final > 0:
        _fx_apply(s, bk.get("recv"))
        _dmg_unit(s, t, final)
    _fx_apply(s, bk.get("dealt"))
    for g in fx.get("gaps") or ():
        s.gaps.append("damage：" + g)


def _fight_deal(s: Sim, a_id, b_id, damages):
    """`MakeCardsFight` 的 `deal_damage` 回调：有 `event_fx["damage"][("fight", a, b)]` 且伤害数对得上 ⇒ 每次 `ApplyDamageToCard`
    前后各结算一次钩子桶（b 先、a 后，桶名 `fight_b_*`/`fight_a_*`）；否则就是裸 `_dmg_unit`。
    原版：MakeCardsFight（BP_CardFunctions.cpp:9755）→ ApplyDamageToCard（:9805 / :9807）。"""
    fx = (s.event_fx.get("damage") or {}).get(("fight", a_id, b_id))
    if not fx or damages is None or (fx.get("to_b"), fx.get("to_a")) != (int(damages[0]), int(damages[1])):
        if fx:
            s.gaps.append("damage：MakeCardsFight 的预计算伤害与本次不符，受伤钩子未结算")
        return _dmg_unit
    bk = fx.get("buckets") or {}
    for g in fx.get("gaps") or ():
        s.gaps.append("damage：" + g)

    def deal(st, unit, dmg, engage=False):
        pre = "fight_b_" if unit.id == b_id else "fight_a_"
        if dmg > 0:
            _fx_apply(st, bk.get(pre + "recv"))
        _dmg_unit(st, unit, dmg, engage=engage)
        _fx_apply(st, bk.get(pre + "dealt"))
    return deal


def _abilities_changed(s: Sim, t: U, kw: str, gained: bool) -> None:
    """关键词 `kw` 刚被赋予(`gained`)/移除之后，广播 0x1D `OnOtherCardAbilitiesChanged(t)` 的预计算后果。
    只有 `ABILITIES_KEYWORDS` 里的关键词会广播；预计算表 `event_fx["abilities"][(t.id, "+kw"|"-kw")]`，没有条目就什么都不发生
    （rule 侧没有任何牌覆写 0x1D 时表是空的；预算用尽/读不出视图的缺口在 `("gap",)` 条目里）。

    原版：ExecuteOnOtherCardsAbilitiesChanged（BP_CardFunctions.cpp:20885）；调用点 Give<关键词>/Remove<关键词>
    （:1349/:1524/:1644/:1860/:1970/:2080/:2198/:2298/:2931/:3501/:3641/:4200/:9906/:10037）。"""
    if kw not in ABILITIES_KEYWORDS:
        return
    tbl = s.event_fx.get("abilities")
    if not tbl:
        return
    fx = tbl.get((t.id, ("+" if gained else "-") + kw))
    if fx:
        _apply_unit_eff(s, t.id, fx)
    for g in tbl.get(("gap",)) or ():
        if g not in s.gaps:
            s.gaps.append(g)


def _apply_convert(s: Sim, cv: dict) -> None:
    """`ConvertCard`（BP_CardFunctions.cpp:12611，逐行读过）。顺序：
      第一轮，逐张旧牌：
        · 在场 ⇒ `ApplyRemoveCardFromBoard(converting)`：旧牌离场钩子（`event_fx["convert"][…]["old"][旧 id]`：
          OnLeaveBoardOrOwner(method 6)/0x2E → OnCardLocationMoved("Convert")/0x2F → OnAfterLeaveBoard/0x8，:18028-18053），
          再在**同位置同序号** `CreateCard(目标名)`（出厂数值，buff/重甲/关键词不继承）；
        · 牌库 ⇒ 同下标换新牌（`RemoveCardFromDeckBySide` → `AddCardToDeckBySide(新, false, 原下标)`，:12841-12850）；
        · 手牌 ⇒ 同位置换新牌（`ExecuteOnSpawnedInHandEvents` 的后果未建模）；
      第二轮，逐张新牌：在前线 ⇒ `RemoveSmokescreen`（:12686）；在场 ⇒ `ExecuteOnEnterPlayEvents(新牌, 5)`（`["new"]`，**没有新牌实例
      时预计算不了，fx 的 gaps 里有记录**）；
      最后 `skipTrigger` 假 ⇒ 0x22 `OnOtherCardConverted`（`["after"]`）。
    `convertIntoCardID>0`（按某张在场牌的名字转）effectvm 抛 Unimplemented ⇒ 根本不会走到这里（缺口在 effectvm 侧）。"""
    key = (cv.get("instigator", 0), tuple(cv.get("ids", ())), cv.get("name"))
    fx = (s.event_fx.get("convert") or {}).get(key)
    if fx:
        for g in fx.get("gaps") or ():
            s.gaps.append(g if g.startswith("convert") else "convert：" + g)
    else:
        s.gaps.append("convert：新牌 OnEnterPlay(5) / 其它牌 OnOtherCardConverted(0x22) / 旧牌离场钩子 没有预计算（event_fx['convert'] 无此条目）")
    for old in cv.get("ids", ()):
        u = s.units.get(old)
        if u is not None:
            if fx:
                _apply_unit_eff(s, old, (fx.get("old") or {}).get(old))      # 旧牌离场钩子（此刻它还在场）
            s.tmp_seq += 1
            nid = -(5000 + s.tmp_seq)
            nu = U(nid, u.side, u.row, cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("cost") or 0,
                   cv.get("typ") or u.typ, cv.get("kw") or (), opc=cv.get("opc") or 1,
                   sick=True, armor=cv.get("armor") or 0, mdef=cv.get("dfn") or 0)
            if nu.row == "frontline":
                nu.kw = nu.kw - {"smokescreen"}                              # 原版：ConvertCard 第二轮，新牌在前线 ⇒ RemoveSmokescreen（:12686）
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
            s.gaps.append("convert：手牌里的转化牌 %s 的效果未知（出厂数值已换，eff 留空）；ExecuteOnSpawnedInHandEvents 的后果未建模"
                          % cv.get("name"))
        elif s.deck_known and old in s.deck:
            # 原版：ConvertCard 牌库分支（BP_CardFunctions.cpp:12840）：旧牌从牌库删掉，新牌插回**同一下标**
            i = s.deck.index(old)
            old_h = s.deck_cards.pop(old, None) or s.card_templates.pop(old, None)
            s.tmp_seq += 1
            nid = -(5000 + s.tmp_seq)
            tpl = H(nid, cv.get("name") or "?", cv.get("cost") or 0, cv.get("typ") or (old_h.typ if old_h else "infantry"),
                    cv.get("atk") or 0, cv.get("dfn") or 0, cv.get("kw") or (), {})
            s.deck[i] = nid
            s.deck_cards[nid] = tpl
            s.card_templates[nid] = tpl
            s.gaps.append("convert：牌库里转化出的新牌 %s 的效果未知（eff 留空）；ExecuteOnAfterDeckChanged(0x3) 的后果未建模；"
                          "AddCardToDeckBySide 第三个实参(false)的含义未读" % cv.get("name"))
        elif old in s.deck:
            s.gaps.append("convert：牌库顺序未知，牌库里的转化未建模")
    if fx:
        _apply_unit_eff(s, None, fx.get("new"))                              # 新牌 OnEnterPlay(5) + 0x2B（有实例才会有）
        if not cv.get("skip_trigger"):
            _apply_unit_eff(s, None, fx.get("after"))                        # 0x22 OnOtherCardConverted（整次转化只发一轮）


def _salvage_one(s: Sim, cid) -> None:
    """（规则本体已迁到 `sim/effects.py::apply_salvage`；这里保留旧名转调，行为一字不改。

    `hand_cap` 由评估侧传（它原先直接读权重表 `WEIGHTS["hand_cap"]` —— 规则读权重正是 §3.1 要避免的）。
    """
    from sim.effects import apply_salvage
    apply_salvage(s, cid, hand_cap=HAND_CAP)              # ★ 游戏规则常量（不再从权重表读）


def sim_order(sim: Sim, hand_id: int, target: Optional[int] = None, eff: Optional[dict] = None) -> Sim:
    """出一张指令。效果取 `eff`（选择路径自带的）> 「这一对（牌, 目标）空跑出的效果」> 牌自己的效果摘要。"""
    s = sim.copy()
    c = s.hand.pop(hand_id, None)
    if c is None:
        return s
    s.kredits -= c.cost
    # 原版：`CardPlayedFromHand` 先过协力检查（BP_CardFunctions.cpp:18682-18700），总部被疲劳打爆 ⇒ 立刻结束、牌的效果不再执行
    if bond_check_on_play(s, c):
        return s
    _apply_eff(s, eff if eff is not None else sim.pair_eff.get((hand_id, target), c.eff), target, src=hand_id)
    _DF.arm(s, hand_id, s.me)                       # 留下来的钩子布防（打出效果之后：授予账 `s.grants` 已记）；没接的钩子记缺口
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
            if len(s2.hand) < HAND_CAP:
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
        # 原版：`CardPlayedFromHand` 的协力检查（BP_CardFunctions.cpp:18682-18700）；打爆总部 ⇒ 牌的部署效果不再执行
        if bond_check_on_play(s2, c):
            return s2
        e = eff if eff is not None else _eff_of(sim, a)
        # 部署效果（资源类：霍尔姆的下回合扣费…；带目标的：这一对空跑出的效果）；选择路径展开出来的部署
        # （5th RANGERS 的 +4/+4 / 行动费 0）效果落在**刚部署的这张牌自己**身上
        tgt = a.dst if (a.dst is not None or not on_self) else -(a.src + 5000)
        _apply_eff(s2, e, tgt, src=a.src)
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
    return any(r.get("side") == sim.me and int(r.get("type", -1)) == rtype
               for r in sim.restrictions)
