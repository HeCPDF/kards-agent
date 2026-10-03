#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""player.rule —— **规则策略 V2**（先有一份能打的脚本，再谈神经网络）。

设计依据是上游 OCR 引擎（`OCR-Kards-Auto/src`）踩过的坑，但判据换成我们手里更硬的东西：

    OCR 靠猜                          我们直接读
    ─────────────────────────────    ──────────────────────────────────────
    橙/灰徽章 = 能不能行动             游戏自己的 `CanCardDoAnything`（不看闪击/进场回合）
    "被守护"要认盾牌图标               `is_being_guarded`（游戏自己的字段）
    指令卡 674 张手抄打法表            运行时旗标 `needs_hand_target` + 游戏自己的
                                      `CanPlayFromHand`（带目标求值）+ 出牌后 `pending()`；
                                      字节码动词只当排序倾向（`semantics/cardprobe.py`）
    "拖了才知道被拒"                   `sess.can_attack/can_play/can_move` 先问游戏
    费用读数会抖                       `st.kredits` 直接是内存值

沿用的 OCR 纪律（每一条都是它实机付过学费的）：
    1. 顺序：**先让场上单位行动，最后才花钱部署** —— 行动也要花指挥点。这里不写死
       阶段，而是把攻击/上线/出牌放进同一个"收益÷花费"排序，效果一样且更灵活。
    2. 同一个动作（种类,主体,目标）**每回合最多试一次**；被拒的也不再提。
    3. 打之前先判：被守护的目标不打；轰炸机遇到敌方支援线战斗机不打总部/支援线。
    4. 炮兵/轰炸机攻击不吃反击。
    5. 步兵/坦克在支援线只打得到敌方前线；要打支援线/总部得先上前线。
    6. 单位这回合能不能动，问游戏（`CanCardDoAnything`），不自己推（闪击只是原因之一）。
    7. 能致命就致命（总部血量 ≤ 全部能打到总部的单位攻击力之和）。

**只挑不判**（项目红线）：所有规则只用来**排序**；真正决定"能不能"的是游戏自己
（`sess.can_*`）。闸门说"不知道"（None）算放行，让游戏在真实上下文里再判一次。

返回值是 **dict**（不是 `player.loop.Action`），由 `agent/nn.py` 的 `as_action` 转换，
所以 `python -m player.loop --policy rule2` 就能跑；节奏/拒绝处理/dry-run 都沿用 `Loop`。

跑离线自检：
    cd kards-agent
    nn/venv/Scripts/python.exe -m player.rule
"""
from __future__ import annotations

from typing import Optional

from learn.baselines import StrategicRule

import json
import time

from policy import boardeval as BE
from semantics import effectvm as EV

LOCAL, ENEMY = "local", "enemy"
UNIT_ROWS = ("frontline", "back")

# ---------------------------------------------------------------------------
# 预报（天气三选一）预测的**留痕**：第一层已定三张（蓝天/薄雾/狂风，不抽随机数），
# 真正抽牌发生在**点完第一层之后** —— 所以点之前把 9 条路径算出来写进动作流，
# 第二层真面板出现时再和"预测 vs 实际"对一次（判据留痕，见 TODO §3d/§3i-2）。
# ★ 同名变体效果不同（`deluge`/`deluge2`/`deluge3`），所以比的是**变体内部名**，不是显示名。
FC_TYPES = {"card_event_sunny1_blue_sky": "sunny",
            "card_event_rain1_mist": "rain",
            "card_event_storm1_gale": "storm"}
FC_PENDING_TTL = 90.0            # 第一层→第二层之间最多等这么久，超时就不核对了


def fc_norm(s) -> str:
    """内部名归一：`card_event_rain2_deluge3_C_2147480180` → `card_event_rain2_deluge3`。"""
    n = str(s or "").strip()
    i = n.rfind("_C")                    # 去掉 UE 的 `_C`（含 `_C_<序号>` 尾巴）
    return n[:i] if i >= 0 else n


def fc_weather_of(name) -> Optional[str]:
    n = fc_norm(name)
    for w in ("sunny", "rain", "storm"):
        if ("_" + w) in n:
            return w
    return None


def fc_plan(names, paths, pending, now) -> tuple:
    """纯函数：候选内部名 + 预算好的路径 + 上次 pending → (meta 补丁, 新 pending)。

    `paths` 形如 `{"sunny": {"light": "card_event_sunny2_heatwave3", ...}, "seed": N}`。
    第一层（三张类型卡）→ 记 `meta["forecast"]` 并留下 pending；
    第二层（天气卡）→ 与 pending 里的预测逐张比，记 `meta["forecast_check"]`。
    """
    out, new_pending = {}, pending
    ns = {fc_norm(n) for n in names if n}
    if ns and ns <= set(FC_TYPES):
        if paths:
            out["forecast"] = {k: v for k, v in paths.items() if k != "seed"}
            out["forecast_seed"] = paths.get("seed")
            new_pending = {"t": now, "paths": paths}
    elif pending and len(ns) >= 2:
        if now - (pending.get("t") or 0) > FC_PENDING_TTL:
            new_pending = None                      # 过期：不核对（宁可漏，不要错配）
        else:
            w = fc_weather_of(sorted(ns)[0])
            pr = (pending.get("paths") or {}).get(w or "")
            if w and isinstance(pr, dict):
                pred = [pr.get(t) for t in ("light", "medium", "heavy")]
                actual = sorted(ns)
                out["forecast_check"] = {"weather": w, "pred": pred, "actual": actual,
                                         "ok": sorted(pred) == actual,
                                         "hit": sum(1 for a, b in zip(pred, actual) if a == b)}
                new_pending = None
    return out, new_pending


# ---------------------------------------------------------------------------
# 洗牌型三选一（`keepOrder=false`，好人寥寥那类）：面板出现时**回退 N 步**复算一遍。
#   机制（BP 导出已核对）：池 → `Array_ShuffleFromStream(池, cardsRandomStream)`（抽 N 次）→ 取前 3。
#   所以面板出现时的种子就是"洗完"的种子，往回退 N 步 = 洗牌前的种子 —— 用它复算，
#   与屏幕上的三张逐张比，就能在**每一局**里自动核对这个模型（判据留痕）。
#   ★ 池要叠"没进预备"那一层：读官方 API 的**本地缓存**（对局里绝不发网络请求）。
CS_FACTION_USA = 5
CS_RARITY_ELITE = 4
CS_UNIT_TYPES = {3, 4, 5, 6, 7, 8, 9, 10}          # ETypeEnum：坦克/战斗机/轰炸机/步兵/炮兵/…
CS_KEEP_SETS = {1, 8, 9, 10, 12, 13, 15, 16, 17, 18, 19, 20, 21}
_MUL, _ADD, _M32 = 196314165, 907633515, 1 << 32
_MUL_INV = pow(_MUL, -1, _M32)                     # LCG 可逆（乘数是奇数）⇒ 能往回退


def lcg_prev(seed, n):
    """往回退 n 步（`advance` 的逆）。"""
    s = int(seed) & 0xFFFFFFFF
    for _ in range(int(n)):
        s = ((s - _ADD) * _MUL_INV) & 0xFFFFFFFF
    return s


def check_shuffle_pick(pool, seed_after, actual):
    """给定"洗完的种子"与池子，回退 `len(pool)` 步复算 → 返回核对结果 dict（纯函数）。"""
    from kardsmem.rng import Stream
    pre = lcg_prev(seed_after, len(pool))
    st = Stream(pre)
    order = st.shuffle(list(range(len(pool))))
    pred = [pool[i] for i in order[:3]]
    return {"pool": len(pool), "pre_seed": pre, "pred": pred, "actual": list(actual),
            "hit": sum(1 for a, b in zip(pred, actual) if a == b),
            "ok": pred == list(actual)}


def fc_best_type(ev: dict):
    """预报第一层：`{天气: {"value": v}}` → 取 value 最大的天气（并列时按 sunny/rain/storm 顺序）。

    ★ 为什么只有预报要做这种"两层前瞻"（用户 2026-10-02）：
      **在所有连续的三选一里，只有预报的第一层会影响第二层的候选**（选天气类型 ⇒ 决定那三张变体）；
      其它连续三选一彼此独立，各自评三张就行。
    """
    order = ("sunny", "rain", "storm")
    best, best_v = None, None
    for w in order:
        v = (ev or {}).get(w, {}).get("value")
        if v is None:
            continue
        if best_v is None or v > best_v:
            best, best_v = w, v
    return best

GROUND = frozenset(("infantry", "tank"))
NO_RETALIATION = frozenset(("artillery", "bomber"))
UNIT_TYPES = frozenset(("infantry", "tank", "artillery", "fighter", "bomber"))

# ---------------------------------------------------------------------------
# 可调参数（单位：指挥点等价值）。**都是拍的初值**，用 `--seed` 固定后跑几局再调。
# ---------------------------------------------------------------------------
PARAMS = {
    "w_cost": 0.9,          # 一张牌的价值 = w_cost*费用 + w_stat*(攻+防)
    "w_stat": 0.35,
    "kw_bonus": 0.6,        # 守护/闪击/烟幕/伏击 等关键词加成
    "hq_dmg": 0.6,          # 总部每掉 1 点血的价值
    "dmg_part": 0.2,        # 打伤没打死：每点伤害的价值
    "taken_part": 0.15,     # 吃反击但没死：每点伤害的代价
    "deploy_mult": 1.0,     # 部署单位：价值 = worth * 这个（≈按平价把指挥点换成场面）
    "order_mult": 0.9,      # 指令：略低于单位（盲打风险）
    "order_target_mult": 0.7,  # 需要目标的指令，目标价值的折算
    # 效果摘要缺失（VM/静态都没跑出效果，或"范围/随机目标"没建模）的指令：
    # **不编造价值**。用户 2026-10-02："指令的评估有问题。6费空打+3+2" ——
    # 旧默认 `order_mult * cost` 会给 6 费牌 +5.4，扣掉手牌持有价值 2.16 仍是 **+3.24**，
    # 于是"打了什么都不做"的牌被当成正收益。现在是 0：打出去只剩"白丢一张手牌"的负收益，
    # 不会被选中；要恢复盲打就把它调大。真正的修法是补效果覆盖（TODO §3i-4/§3j）。
    "unknown_order_est": 0.0,
    "move_mult": 0.5,       # 上线：价值 = 攻击力 * 这个（下回合才能打）
    "lethal": 1000.0,
    "soft_penalty": 0.05,   # 违反"兵种×所在行"表的候选：不剔除，只沉底（游戏仍能说了算）
    "min_attack_value": 0.25,
    "gate_budget": 10,      # 一次决策最多问游戏几次闸门
    # 花费怎么纳入排序（用户 2026-09-30 的两种思路）：
    #   "ratio"    方案 A：排序键 = 场面分数差(delta) ÷ 花费
    #   "resource" 方案 B：剩余指挥点计入场面（每点 kred_w），排序键 = delta（花钱已被扣）
    "rank": "ratio",
    "kred_w": 0.6,
    # ---- 场面搜索（policy/boardeval.py）：对模拟场面做回合内束搜索，取最优序列的第一步 ----
    "search": True,
    "depth": 4, "beam": 10, "branch": 14,
    "min_gain": 0.15,       # 序列总收益低于这个就不动（结束回合）
    "use_vm": True,         # 效果摘要优先用外部 Kismet VM 空跑（枚举随机结果）；否则静态常量
    "vm_budget_s": 3.0,     # **一个回合**里 VM 空跑总时间上限（不是每步；结果按 (牌,目标) 缓存），超了退回静态常量
    "vm_one_s": 1.0,        # 单张牌/单个目标的 VM 空跑上限（含随机枚举），超了如实停下
    "legal_max_targets": 14,  # 每张带目标的牌最多问游戏几个候选目标
    # 注：**不要**自己"超时就结束回合"——用户 2026-10-02：游戏自己的回合计时器会替我们结束回合。
    #     我们只记录"本回合已用多久"（`meta.turn_elapsed_s`），把处理时间压到计时器以内。
}


def _n(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def _atk(c) -> float:
    f = getattr(c, "total_attack", None)
    if callable(f):
        try:
            v = f()
            if v is not None:
                return float(v)
        except Exception:                                         # noqa: BLE001
            pass
    return _n(getattr(c, "attack", 0))


def _def(c) -> float:
    return _n(getattr(c, "defense", 0))


def _opcost(c) -> float:
    f = getattr(c, "total_operation_cost", None)
    if callable(f):
        try:
            v = f()
            if v is not None:
                return float(v)
        except Exception:                                         # noqa: BLE001
            pass
    v = getattr(c, "operation_cost", None)
    return float(v) if v is not None else 1.0


# ---------------------------------------------------------------------------
# 指令卡语义：读进程里的蓝图字节码（`semantics/cardprobe.py`），不用任何外部表
# ---------------------------------------------------------------------------
class Plan:
    """一张牌的静态画像。

    ★ 2026-09-30 用户纠正：**字节码里"调了哪些动词"不能当事实**——部署效果不总触发、
      没手牌时不会要求选手牌，条件全在分支里，静态的动词集合回答不了"这一次会不会发生"。
      所以这里只留**两类**东西：
        · 游戏运行时自己的旗标（`Card.needs_hand_target`）——要不要带目标出牌的入口；
        · 动词得出的**敌我倾向**——只给候选**排序**用，绝不用来筛/否决。
      "这一次能不能、指谁合法、要不要再选手牌"一律问游戏自己（`sess.can_play(card,
      target=t)`）或在出牌后读 `pending()`——游戏会告诉我们。
    """
    __slots__ = ("known", "needs_target", "side", "damage", "type", "kw", "effects")

    def __init__(self, summ: Optional[dict] = None, card=None):
        s = summ or {}
        self.known = bool(s.get("known"))
        self.needs_target = bool(getattr(card, "needs_hand_target", False))
        self.side = s.get("side")            # 'enemy' / 'friend' / None —— 仅作排序倾向
        self.damage = s.get("damage")        # 尽力读的伤害常量，仅作排序
        self.effects = dict(s.get("effects") or {})   # 静态常量摘要（VM 跑不了时的退路）
        self.type = getattr(card, "card_type", None)
        kws = getattr(card, "keywords", None) or []
        self.kw = {str(k).lower().replace("has", "", 1) if str(k).lower().startswith("has")
                   else str(k).lower() for k in kws}


# ---------------------------------------------------------------------------
class RuleV2(StrategicRule):
    """规则策略 V2。继承 `StrategicRule` 只为复用 `choose_mulligan`（用户定调的
    跳费口径）；其余 `choose_*` 全部重写。"""

    name = "rule-v2"

    def __init__(self, sess, table=None, params: Optional[dict] = None, probe_fn=None,
                 act_fn=None):
        super().__init__(sess, table=table)
        self.probe_fn = probe_fn or self._default_probe
        self.act_fn = act_fn or self._default_act
        self._act_cache: dict = {}
        self._pick_seen: dict = {}
        self._sim = None
        self._eff_cache: dict = {}
        self.suppress_kinds: set = set()      # 本回合被熔断的动作类（由 Loop 维护）
        self._vm_spent = 0.0
        self._vm_turn = None
        self.fx_meta: dict = {}          # 触发链留痕（攻击 fx / 死亡 fx 的算了几对、跳过几对、哪些钩子在场、耗时、是否有种子）
        self._eff_timeouts: dict = {}
        self._kw_cache: dict = {}
        self._st = None
        self.gaps: dict = {}              # 覆盖缺口：VM/静态都取不到效果的牌（用户：不该存在，要补）
        self.eff_src: dict = {}
        # ★ 2026-10-02（§8-9 标记验不上时的诊断）：`_intel_triggers` 为什么没触发 ——
        #   是 `cipher/intel_seen` 为 0（打出的牌没带情报值），还是场上没有覆写
        #   `OnIntelTriggered` 的本方卡。只在"打出 cipher>0 的牌但没触发"时写一条，
        #   随 probe 一起落盘，下一局直接能看出缺哪半。
        self.intel_dbg: dict = {}
        # ★ 2026-10-02：四个标记（intel/deckchg/hooks/shuffled）的触发链原来各自
        #   `except Exception: pass` —— 抛异常就**静默无标记**（§3.5.6 失败可见的反面）。
        #   改成记进这里，随 probe 落盘；标记没出现时先看这张表。
        self.marker_err: dict = {}
        self.avoid: set = set()
        self._last_n = 0
        self.P = dict(PARAMS, **(params or {}))
        self._turn = None
        self._tried: set = set()       # 本回合发过的 (kind,card,target)：不再提
        self._bad: set = set()         # 闸门说不行的：局面变化（epoch）后才重新问
        self._epoch = 0
        self._plans: dict = {}
        self.probe: dict = {}

    # ------------------------------------------------------------ 卡牌信息
    def plan(self, c) -> Plan:
        key = getattr(c, "card_id", None), getattr(c, "name", None)
        p = self._plans.get(key)
        if p is None:
            summ = None
            try:
                summ = self.probe_fn(c)
            except Exception:                                     # noqa: BLE001
                summ = None
            p = self._plans[key] = Plan(summ, c)
        return p

    def _default_probe(self, c):
        from semantics import cardprobe
        km = self.sess._kardsmem()
        return cardprobe.summarize(cardprobe.profile(km, c))

    def utype(self, c) -> Optional[str]:
        t = getattr(c, "card_type", None)
        return t if t in UNIT_TYPES else t

    def is_unit(self, c) -> bool:
        t = self.utype(c)
        if t in UNIT_TYPES:
            return True
        if t in ("order", "gotcha", "location", "wildcard"):
            return False
        return bool(_n(getattr(c, "attack", 0)) or _n(getattr(c, "defense", 0)))

    def worth(self, c) -> float:
        """单位价值 —— 与 `boardeval.unit_value` 同一把尺（攻防 + 关键词，不含费用）。"""
        return BE.unit_value(BE.U(c.card_id, c.side, c.location, int(_atk(c)), int(_def(c)),
                                  0, getattr(c, "card_type", None), self.plan(c).kw))

    # ---- 场面模拟 / 排序键 ----
    @property
    def _W(self) -> dict:
        w = dict(BE.W)
        w["kred_w"] = self.P["kred_w"] if self.P["rank"] == "resource" else 0.0
        return w

    def _build_sim(self, st, kred):
        ids, dcards = self._deck_state(st)
        self._sim = BE.from_cards(st.cards, lambda c: self.plan(c).kw,
                                  actionable=lambda c: self._actionable(st, c),
                                  kredits=float(kred),
                                  deck=ids, deck_cards=dcards,
                                  fatigue=self._fatigue(st),
                                  rng_seed=self._rng_seed(),
                                  death_fx=self._death_fx(st),
                                  event_fx={"move": self._move_fx(st),
                                            "draw": self._draw_fx(st),
                                            "suppress": self._suppress_fx(st),
                                            "pin": self._pin_fx(st),
                                            "reveal": self._reveal_fx(st),
                                            "veteran": self._veteran_fx(st),
                                            "heal": self._heal_fx(st),
                                            "steal": self._steal_fx(st),
                                            "retreat": self._retreat_fx(st),
                                            "turn_end": self._turn_end_fx(st)},
                                  attack_fx=self._attack_fx(
                                      st, next((c for c in st.cards if c.side == ENEMY and c.location == "hq"), None)),
                                  kredit_max=int(getattr(st, "max_possible_kredits", None) or 24),
                                  spawn_stats=self._spawn_stat,
                                  front_limited=bool(getattr(st, "frontline_limiters", None)))
        # 预报候选表：手里有预报牌、或攻击/上线的钩子后果里带 `forecast` 标记时才算（9 次评估较贵，按回合+种子缓存）
        if (any(isinstance(h.eff, dict) and h.eff.get("forecast") for h in self._sim.hand.values())
                or BE._has_key(self._sim.attack_fx, "forecast") or BE._has_key(self._sim.event_fx, "forecast")):
            self._sim.forecast = self._fc_table(st)
        sp = {}
        for h in self._sim.hand.values():                         # 三选一加入手牌（selectCardToDraw 一族）：候选按活种子预测
            if isinstance(h.eff, dict) and h.eff.get("choose_spawn"):
                cs = self._spawn_cands(st, h.id)
                if cs:
                    sp[h.id] = cs
        if sp:
            self._sim.spawn_pick = sp
        ht = {}
        for h in self._sim.hand.values():                         # 手牌目标提示（175th 等）：逐候选空跑 OnHandTargetSelected
            if isinstance(h.eff, dict) and h.eff.get("hand_target_pending"):
                others = [x for x in st.hand(LOCAL) if x.card_id != h.id]
                fx = self._hand_target_fx_for(st, h.id, others)
                if fx:
                    ht[h.id] = fx
        if ht:
            self._sim.hand_target_fx = ht
        return self._sim

    def _d(self, after) -> float:
        """候选动作的 ΔV。

        ★ 2026-10-02（§8-8 回合结束消费点）：先 `BE.sim_turn_end(after)` —— 候选终态其实是
        "我停手、这一回合真正结束"时的盘面：我方 `OnEndOfTurn`（0x19）的预计算后果要算进去，
        `AddAttackUntilEndOfTurn` 的临时攻也要按 `RemoveBuffsEndOfTurn` 清掉（幂等）。
        这是**行为改动**（打分口径变了），要实机验证。
        """
        return BE.delta(self._sim, BE.sim_turn_end(after), self._W)

    def _deck_state(self, st, ttl: float = 3.0):
        """**整条牌库** → `(ids, deck_cards)`：

        * `ids` = `GameState.DeckCardIDs_Left` 的**原始 id 列表**（index 0 = 牌顶，保序）；
          `None` = 读不出来（未知，调用方退回旧行为）。
        * `deck_cards` = `{card_id: BE.H}` 模板表（含 `_on_draw` 抽到时效果）。

        用户 2026-10-02（最终口径）：**牌库是模拟里的一等状态** ——
          * 抽牌 = 按游戏此刻的真实牌序抽；抽到哪张，就按那张自己的持有价值进手牌；
            那张牌带"抽到时效果"（autoplay / OnCardDrawnFromDeck）就跑它自己的字节码；
          * **空库继续抽 ⇒ 游戏自己的疲劳规则**（BP `ApplyFatigueDamage`：伤害 = **当前**
            疲劳计数，然后计数 +1）——由 boardeval 执行，疲劳初值取快照 `BoardState.fatigue`；
          * 洗牌/塞牌/移到牌顶这些**改牌库的动词**，由 boardeval 在模拟里按游戏逻辑改这份牌序
            （洗牌 = `Array_ShuffleFromStream`，我们已经用 `kardsmem.rng.Stream.shuffle` 复刻过）。
        读不出来 ⇒ `(None, {})`（不假装知道）。
        """
        now = time.time()
        c = getattr(self, "_deckstate_cache", None)
        if c and now - c[0] < ttl:
            return list(c[1]), dict(c[2])
        ids_out, cards_out = [], {}
        km = self._km()
        if km is not None:
            try:
                from kardsmem.gs import load as _load_gs
                ids = _load_gs(km).deck_ids("local") or []
                by = {cr.card_id: cr for cr in (getattr(st, "cards", None) or [])}
                # "抽到时效果"：抵抗那类 `autoplay` 牌 = **抽到就免费打出** ⇒ 跑它自己的
                # `OnPlayedFromHand`（游戏自己的字节码），把摘要挂成 `_on_draw` 交给 boardeval。
                # 只对牌库顶前 `MAX_ON_DRAW` 张做（每张 0.6 s 上限；3 s 缓存）。
                from kardsmem.cards import read_gameplay_tags
                from kardsmem.rng import Stream, read_seed
                from semantics import effectvm as EV
                hooks = EV.make_read_hooks(st, getattr(st, "my_side_raw", None))
                seed = read_seed(km)
                for n_i, i in enumerate(ids):
                    ids_out.append(i)
                    cr = by.get(i)
                    if cr is None:
                        # ★ 认不出实例（AllCardsInBattle 里没有）也要**占位保留**：牌库长度必须与
                        #   游戏一致 —— 否则 `ShuffleDeckBySide` 洗出来的排列会跟着错位
                        #   （洗牌的抽取次数 = 牌库张数）。抽到占位牌由 boardeval 按 `draw_v` 估值。
                        continue
                    eff = {}
                    if n_i < 3:
                        try:
                            ptr0 = (getattr(cr, "raw", None) or {}).get("ptr")
                            od = {}

                            def _merge(new):
                                for kk, vv in (new or {}).items():
                                    if isinstance(vv, (int, float)) and isinstance(
                                            od.get(kk), (int, float)):
                                        od[kk] += vv
                                    else:
                                        od[kk] = vv
                            seat = getattr(st, "my_side_raw", None)
                            st_stream = Stream(seed) if seed is not None else None
                            # ① `autoplay`（抵抗、预报第一段）：**抽到就免费打出** ⇒ 跑 OnPlayedFromHand
                            is_auto = bool(ptr0 and "autoplay" in (read_gameplay_tags(km, ptr0) or []))
                            if is_auto:
                                # ★ 2026-10-02（用户两条口径）：
                                #   ① "autoplay 就是进入手牌就打出" —— 这条链就是它的效果；
                                #   ② **"预报特别一些，按九选一评估"** —— 预报是**两层**三选一
                                #      （每层 3 个选项）⇒ 3×3=9 种组合，只有预报的第一层会影响
                                #      第二层，所以必须**联合枚举**后取最好，不能逐层贪心
                                #      （其它连续三选一彼此独立，才可逐层）。
                                #      用户 2026-10-03 补充："autoplay 的三选一只存在于
                                #      **预报第一段选中的那张牌**上" ⇒ 第一层（天气类型）由
                                #      `choose_pick`/`fc_best_type` 做 9 路前瞻；这里展开的是
                                #      第二层（被选中那张牌自己的三选一）。
                                #   以前走 `record_effects` 单分支 ⇒ 抉择节点不展开、eff 为空；
                                #   现在 `enumerate_effects(cap=9)` 把九种组合都跑出来，用
                                #   `outcomes_mode="max"` 交给消费方（`boardeval._apply_eff` 已支持
                                #   —— 抉择取最好分支、随机取期望）。
                                r0 = EV.enumerate_effects(
                                    km, ptr0, 0, False, hook="OnPlayedFromHand", my_side=seat,
                                    cap=9, read_hooks=hooks, budget_s=0.6, rng_seed=seed,
                                    # 盘面级原语（IsSideActive…）需要 BoardState
                                    board=st, my_seat=seat)
                                _outs = r0.get("outcomes") or []
                                if len(_outs) == 1:
                                    _merge(_outs[0][1])
                                elif _outs:
                                    od["outcomes"] = [(w, dict(e)) for w, e in _outs]
                                    od["outcomes_mode"] = "max"
                            # ② 自己被抽到（航母打击群那族）⇒ 跑它覆写的 OnCardDrawnFromDeck
                            if ptr0:
                                from kardsmem import kismet as _kismet
                                from kardsmem.objects import ObjectArray as _OA
                                uc0 = _OA(km).class_of(ptr0)
                                fn0 = (_kismet.find_function(km, uc0, "OnCardDrawnFromDeck",
                                                             inherited=False) if uc0 else None)
                                if fn0:
                                    r1 = EV.record_effects(
                                        km, ptr0, 0, False, "OnCardDrawnFromDeck", seat, None,
                                        read_hooks=hooks, timeout_s=0.6, rng_stream=st_stream,
                                        args={"StartOfTurnDraw": False, "drawnSide": seat or 1},
                                        board=st, my_seat=seat)
                                    _merge(r1.get("eff"))
                            if od:
                                eff = {"_on_draw": od}
                                if is_auto:
                                    eff["_autoplay"] = True       # 原版：入 autoplay 队列、动作边界冲刷（sim.chain.flush_autoplay）
                        except Exception:                         # noqa: BLE001
                            eff = {}
                    cards_out[cr.card_id] = BE.H(
                        cr.card_id, getattr(cr, "name", "?"),
                        int(getattr(cr, "kredit_cost", 0) or 0),
                        getattr(cr, "card_type", None) or "order",
                        int(getattr(cr, "attack", 0) or 0),
                        int(getattr(cr, "defense", 0) or 0),
                        self._kw(cr), eff)
            except Exception:                                     # noqa: BLE001
                return None, {}
        else:
            return None, {}
        self._deckstate_cache = (now, ids_out, cards_out)
        return list(ids_out), dict(cards_out)

    @staticmethod
    def _fatigue(st) -> int:
        """我方当前的**疲劳计数**（快照 `BoardState.fatigue[local]`；读不到 = 0）。

        ★ 2026-10-02（用户）：空库继续抽 = 游戏自己的 `ApplyFatigueDamage` ——
        伤害 = **当前**计数，然后计数 +1（BP 里 increment 在伤害之后）。
        """
        f = getattr(st, "fatigue", None) or {}
        try:
            return int(f.get(LOCAL) or 0)
        except Exception:                                         # noqa: BLE001
            return 0

    def _rk(self, v: float, cost: float) -> float:
        """排序键。A：v/花费；B：v 已含指挥点变化（调用方给的 v 是 delta 或已扣 kred_w*cost）。"""
        if self.P["rank"] == "resource" or self.P["search"]:
            return v
        return v / max(cost, 0.5)

    def _order_v(self, base: float, cost: float) -> float:
        """启发式估值的指令：方案 B 下扣掉花费的机会价值，保持与 delta 同一口径。"""
        return base - (self.P["kred_w"] * cost if self.P["rank"] == "resource" else 0.0)

    # ------------------------------------------------------------ 状态切片
    @staticmethod
    def _split(st):
        me = [c for c in st.cards if c.side == LOCAL and c.location in UNIT_ROWS]
        foes = [c for c in st.cards if c.side == ENEMY and c.location in UNIT_ROWS]
        ehq = next((c for c in st.cards if c.side == ENEMY and c.location == "hq"), None)
        return me, foes, ehq

    @staticmethod
    def _tkind(t) -> str:
        return {"hq": "hq", "frontline": "front", "back": "support"}.get(t.location, "?")

    def _table_ok(self, a, t) -> bool:
        """OCR 用户确认的"兵种×所在行"表。只当排序信号，不当否决。"""
        at, tk = self.utype(a), self._tkind(t)
        if at in GROUND:
            return tk == "front" if a.location == "back" else tk in ("support", "hq")
        return tk in ("front", "support", "hq")

    def _actionable(self, st, u) -> bool:
        """这个单位现在能不能动 —— **问游戏自己**（`BP_Logic::CanCardDoAnything`），
        不看闪击、不看进场回合（2026-09-30 用户：闪击只是其中一种原因，游戏有这个函数）。
        问不到（None）→ 退回内存里的 `can_act` 字段；再不行就放行，让攻击闸门去裁决。"""
        key = ("act", u.card_id, self._epoch)
        if key in self._act_cache:
            return self._act_cache[key]
        r = None
        try:
            r = self.act_fn(u)
        except Exception:                                         # noqa: BLE001
            r = None
        if r is None:
            r = getattr(u, "can_act", None)
        ok = r is not False
        self._act_cache[key] = ok
        return ok

    def _playable(self, c, cost, kred) -> bool:
        """这张手牌现在能不能打 —— **问游戏自己的总闸**（`CanPlayCardFromHand`：含指挥点、
        支援线满、天气/自定义能力、卡自身的 `CanPlayFromHand`）。

        ★ 2026-09-30 用户纠正：指挥点要**实时读**、槽可以被加减，不能拿"第 n 回合 = n 点"
          或我自己的 `cost > kred` 去筛。只有闸门给不出答案（None）时才退回费用比较。
        """
        key = ("playable", c.card_id, self._epoch)
        if key in self._act_cache:
            return self._act_cache[key]
        try:
            r = self.sess.can_play(c)
        except Exception:                                         # noqa: BLE001
            r = {}
        can = r.get("can")
        ok = (cost <= kred) if can is None else bool(can)
        self._act_cache[key] = ok
        return ok

    def _default_act(self, u):
        from agent import precheck
        d = precheck.can_do_anything(u)
        return d.get("can") if d.get("ok") else None

    # ------------------------------------------------------------ 候选：攻击
    def _attack_cands(self, st, me, foes, ehq, kred):
        P, out = self.P, []
        targets = list(foes) + ([ehq] if ehq is not None else [])
        interceptors = [f for f in foes if f.location == "back" and self.utype(f) == "fighter"
                        and getattr(f, "is_revealed", True) is not False]
        actors = [a for a in me if _atk(a) > 0 and self._actionable(st, a)]
        hitters = [a for a in actors
                   if ehq is not None and self._table_ok(a, ehq)
                   and not getattr(ehq, "is_being_guarded", False)
                   and not (self.utype(a) == "bomber" and interceptors)]
        lethal = ehq is not None and hitters and \
            sum(_atk(a) for a in hitters) >= _def(ehq) and \
            sum(_opcost(a) for a in hitters) <= kred
        for a in actors:
            at = self.utype(a)
            for t in targets:
                key = ("attack", a.card_id, t.card_id)
                if key in self._tried or key in self._bad:
                    continue
                if getattr(t, "is_being_guarded", False):        # 游戏必拒（OCR 白拖 4 次）
                    continue
                tk = self._tkind(t)
                if at == "bomber" and interceptors and tk in ("hq", "support"):
                    continue
                if tk == "hq":
                    v = self._d(BE.sim_attack(self._sim, a.card_id, None, hq=True))
                    if lethal and a in hitters:
                        v += P["lethal"]
                else:
                    v = self._d(BE.sim_attack(self._sim, a.card_id, t.card_id))
                    if "guard" in self.plan(t).kw:
                        v += 0.5                                  # 先清守护单位
                if not self._table_ok(a, t):
                    v *= P["soft_penalty"] if v > 0 else 1.0
                if v < P["min_attack_value"] and v < P["lethal"] / 2:
                    continue
                out.append((self._rk(v, _opcost(a)), v, key,
                            {"kind": "attack", "card": a.card_id, "target": t.card_id,
                             "note": "规则2：%s→%s v=%.2f" % (a.name, t.name, v)},
                            _opcost(a)))
        return out

    # ------------------------------------------------------------ 候选：指向
    def _target_cands(self, st, c, plan: Plan, me, foes, ehq, limit=3):
        """这张牌（带目标出）的候选目标 → [(收益, 目标卡)]，按收益降序。

        两侧**都**列进来 —— 谁合法由游戏的 `CanPlayFromHand` 在确认阶段说了算；这里的
        倾向（plan.side）只影响分数：倾向一致 ×1，倾向相反 ×-0.5，未知时偏敌方。
        """
        P = self.P
        pools = [("enemy", list(foes) + ([ehq] if ehq is not None else [])),
                 ("friend", [u for u in me if u.card_id != c.card_id])]
        scored = []
        for side, pool in pools:
            if plan.side is None:
                mult = 0.7 if side == "enemy" else 0.4
            else:
                mult = 1.0 if (side == "enemy") == (plan.side == "enemy") else -0.5
            for t in pool:
                if ("play", c.card_id, t.card_id) in self._bad:
                    continue                                      # 游戏说过不行
                if side == "enemy":
                    if self._tkind(t) == "hq":
                        v = P["hq_dmg"] * (plan.damage or 2)
                    elif plan.damage is not None:
                        v = self.worth(t) if plan.damage >= _def(t) else                             P["dmg_part"] * plan.damage
                    else:
                        v = 0.6 * self.worth(t)                   # 移除类：折算
                else:
                    v = 0.3 * self.worth(t) + (0.5 if self._actionable(st, t) else 0.0)
                scored.append((v * mult, t))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    # ------------------------------------------------------------ 候选：出牌
    def _play_cands(self, st, kred, me, foes, ehq):
        P, out = self.P, []
        for c in st.hand(LOCAL):
            cost = _n(getattr(c, "kredit_cost", None), 99)
            if not self._playable(c, cost, kred):
                continue                     # 指挥点够不够 + 牌自己现在能不能打：问游戏
            if c.name in self.avoid:
                continue
            pl = self.plan(c)
            if pl.type == "gotcha":
                continue                                          # 反制：另开动词，暂不打
            unit = self.is_unit(c)
            if unit:
                base = self._d(BE.sim_deploy(self._sim, -c.card_id, int(_atk(c)), int(_def(c)),
                                             int(cost), getattr(c, "card_type", None),
                                             self.plan(c).kw)) * P["deploy_mult"]
            else:
                base = self._order_v(P["order_mult"] * max(cost, 1), cost)
            if pl.needs_target:
                cands = self._target_cands(st, c, pl, me, foes, ehq)
                if not cands and unit:
                    key = ("play", c.card_id, None)
                    if key not in self._tried and key not in self._bad:
                        out.append((self._rk(base * 0.9, cost), base * 0.9, key,
                                    {"kind": "play_unit", "card": c.card_id,
                                     "note": "规则2：部署 %s（无可指向目标）" % c.name}, cost))
                for tv, t in cands:
                    key = ("play", c.card_id, t.card_id)
                    if key in self._tried or key in self._bad:
                        continue
                    v = base + P["order_target_mult"] * tv
                    if not unit and tv <= 0:
                        continue                                  # 指令打不出收益：留着
                    kind = "play_unit_target" if unit else "play_event_target"
                    out.append((self._rk(v, cost), v, key,
                                {"kind": kind, "card": c.card_id, "target": t.card_id,
                                 "note": "规则2：%s → %s" % (c.name, t.name)}, cost))
            else:
                key = ("play", c.card_id, None)
                if key in self._tried or key in self._bad:
                    continue
                out.append((self._rk(base, cost), base, key,
                            {"kind": "play_unit" if unit else "play_event", "card": c.card_id,
                             "note": "规则2：%s %s" % ("部署" if unit else "指令", c.name)}, cost))
        return out

    # ------------------------------------------------------------ 候选：上线
    def _move_cands(self, st, me, foes, ehq, kred, attackers):
        P, out = self.P, []
        if getattr(st, "frontline_owner", None) == ENEMY:
            return out                                            # 前线被占：上不去
        for u in me:
            if u.location != "back" or self.utype(u) not in GROUND:
                continue
            if u.card_id in attackers or not self._actionable(st, u) or _atk(u) <= 0:
                continue
            key = ("move", u.card_id, None)
            if key in self._tried or key in self._bad:
                continue
            v = self._d(BE.sim_move(self._sim, u.card_id))
            out.append((self._rk(v, _opcost(u)), v, key,
                        {"kind": "move_up", "card": u.card_id,
                         "note": "规则2：上线 %s（下回合可打支援线/总部）" % u.name}, _opcost(u)))
        return out

    # ------------------------------------------------------------ 搜索路径
    # 用户 2026-09-30：带目标的牌，「能不能指向某个目标」游戏有对应的函数 ⇒ 每个（牌, 目标）
    # 各自是一条可用走法；效果未知的牌不存在 ⇒ 效果一律先让 VM 空跑（枚举随机结果），
    # 跑不了才退回字节码里的常量，两个都没有就记成覆盖缺口（不悄悄猜）。
    _KW_COVERED = frozenset({"alpine", "ambush", "blitz", "fury", "guard", "immune", "shock", "smokescreen"})

    def _kw(self, c) -> frozenset:
        """关键词：以**游戏 getter**（`getHas*`）为准，内存旗标位只是一路来源（被授予的能力，
        如伞兵的冲击，不在那一位上）。这里**不发 RPC**：只读 `prefetch_kw` 预先批量取好的缓存，
        没有就退回内存旗标（`base`）。"""
        base = frozenset(str(k).lower()[3:] if str(k).lower().startswith("has") else str(k).lower()
                         for k in (getattr(c, "keywords", None) or []))
        got = self._kw_cache.get((c.card_id, self._epoch))
        out = base if got is None else frozenset((base - self._KW_COVERED) | got)
        return out | self._move_attack_kw(c)

    @staticmethod
    def _move_attack_kw(c) -> frozenset:
        """原版 `CanMoveAndAttackInTheSameTurn`：装甲单位，或 customName1 带该属性（掷弹兵、意大利骑兵…）。
        sim 里用伪关键词 `move_attack` 表示，`sim_move` / `sim_attack` 据此决定“上线后还能不能打”。"""
        raw = getattr(c, "raw", None)
        cn = getattr(c, "custom_name1", None) or (raw.get("custom_name1") if isinstance(raw, dict) else None) or ""
        segs = {x.strip() for x in str(cn).split(";")}
        if segs & {"CanMoveAndAttackInTheSameTurn", "isArmorUnit"}:
            return frozenset({"move_attack"})
        return frozenset()

    def prefetch_kw(self, st) -> None:
        """一次 RPC 把场上和手牌所有牌的关键词问完（`card_keywords_many`），按 (牌, epoch) 缓存。
        epoch 在每个已执行的动作后 +1，所以动作之后下一步会重取（关键词可能被授予/移除）。"""
        need = [c for c in st.cards
                if c.location in ("frontline", "back", "hand")
                and (c.card_id, self._epoch) not in self._kw_cache
                and (getattr(c, "raw", None) or {}).get("ptr")]
        if not need:
            return
        try:
            from agent import precheck
            r = precheck.call_read("card_keywords_many", [c.raw["ptr"] for c in need])
        except Exception:                                         # noqa: BLE001
            return
        if not r or not r.get("keywords"):
            return
        for c in need:
            v = r["keywords"].get(int(c.raw["ptr"]))
            if v is not None:
                self._kw_cache[(c.card_id, self._epoch)] = frozenset(v)
        if len(self._kw_cache) > 600:                             # 别无限长
            for k in list(self._kw_cache)[:300]:
                self._kw_cache.pop(k, None)

    def _km(self):
        try:
            return self.sess._kardsmem()
        except Exception:                                         # noqa: BLE001
            return None

    def _rng_seed(self):
        """游戏卡牌随机流（`cardsRandomStream`）的**当前 Seed**。一步之内读一次（0.3 s 缓存），免得每张牌都读内存。
        `P["use_rng"]=False` 关掉（返回 None，随机退回枚举——离线测试用）。

        ★ 2026-10-03（用户）：**读不到种子时不退回枚举**——记缺口，并**换一个随机种子**，让模拟照算法和这个种子给出（确定的）结果；
          评估层从不见随机。假种子**每回合固定一个**（同一回合内各次建模拟一致），`self.seed_fake=True`，
          `probe.gaps["__rng_seed__"]` 如实写明"结果仅供参考"。"""
        if not self.P.get("use_rng", True):
            return None
        now = time.time()
        cache = getattr(self, "_rng_cache", None)
        if cache and now - cache[0] < 0.3:
            return cache[1]
        km = self._km()
        v = None
        if km is not None:
            try:
                from kardsmem import rng as _rng
                v = _rng.read_seed(km)
            except Exception:                                     # noqa: BLE001
                v = None
        if v is None and km is not None:
            import random as _random
            turn = getattr(getattr(self, "_st", None), "turn", None)
            fs = self.__dict__.setdefault("_fake_seed_by_turn", {})
            v = fs.setdefault(turn, _random.getrandbits(32))
            self.seed_fake = True
            self.gaps["__rng_seed__"] = "读不到随机种子：按随机种子 %d 模拟，随机相关结果（抽牌/洗牌/三选一候选）仅供参考" % v
        else:
            self.seed_fake = False
            self.gaps.pop("__rng_seed__", None)
        self._rng_cache = (now, v)
        return v

    def _fc_paths(self, ttl: float = 0.5):
        """预报 9 条路径（3 天气 × light/medium/heavy 变体），从当前活种子一次算出。

        返回 `{天气: {"light": 变体内部名, "medium": …, "heavy": …}, "seed": N}`；
        读不到（不在对局 / 没种子 / 池子读不出）⇒ None。0.5 s 缓存，桶表只读一次。
        """
        now = time.time()
        c = getattr(self, "_fc_cache", None)
        if c and now - c[0] < ttl:
            return c[1]
        out = None
        km, seed = self._km(), self._rng_seed()
        if km is not None and seed is not None:
            try:
                from kardsmem.forecast_pool import (predict_ptrs, read_weather_buckets,
                                                    strip_serial)
                b = getattr(self, "_fc_buckets", None)
                if not b:
                    b = read_weather_buckets(km)
                    self._fc_buckets = b
                pool = km.names_pool()
                out = {}
                for w, row in predict_ptrs(seed, b).items():
                    out[w] = {t: fc_norm(strip_serial(pool.fname_of(row[t]) or ""))
                              for t in ("light", "medium", "heavy")}
                out["seed"] = seed
            except Exception:                                     # noqa: BLE001
                out = None
        self._fc_cache = (now, out)
        return out

    def _cs_pool(self, ttl: float = 60.0):
        """"洗牌型三选一"（好人寥寥那类）的池：USA 精英单位 ∩ 卡集过滤 ∩ **没进预备**。

        只读：静态卡表（faction/rarity/类型/cardSet）+ 官方 API 的**本地缓存**
        （`kards-data/api/kards_api_cards.json`，由 `tools/kards_api_reserved.py` 拉取；预备名单轮换时刷新）。
        ★ 对局里**不发网络请求**：读不到缓存就返回 None（这类卡不留痕，不影响打牌）。
        """
        now = time.time()
        c = getattr(self, "_cs_cache", None)
        if c and now - c[0] < ttl:
            return c[1]
        out = None
        km = self._km()
        if km is not None:
            try:
                import json as _json
                import os as _os
                import struct as _struct
                from base import paths as _paths
                cache = _paths.API_CACHE
                legal = {}
                for row in _json.load(open(cache, encoding="utf-8")):
                    j = row.get("json")
                    j = _json.loads(j) if isinstance(j, str) else j
                    ti = (j or {}).get("title")
                    en = (ti.get("en-EN") if isinstance(ti, dict) else ti) or ""
                    if en:
                        legal[en.upper()] = True
                from kardsmem.gs import load as _load_gs
                from kardsmem.names import OFF_CARD_TITLE, ftext_at
                gs = _load_gs(km)
                t = gs.static_card_table()
                blob = km.m.read_exact(t["ptr"], (t["num"] or 0) * 8)
                ptrs = [_struct.unpack_from("<Q", blob, i)[0]
                        for i in range(0, len(blob), 8)]
                pool = km.names_pool()
                out = []
                for p in ptrs:
                    n = pool.fname_of(p) or ""
                    if not n.startswith("card_unit_"):
                        continue
                    if (km.m.u8(p + 0x7C) != CS_FACTION_USA
                            or km.m.u8(p + 0x11C) != CS_RARITY_ELITE):
                        continue
                    if km.m.u8(p + 0x68) not in CS_UNIT_TYPES:
                        continue
                    if km.m.u8(p + 0x130) not in CS_KEEP_SETS:
                        continue
                    if (ftext_at(km.m, p, OFF_CARD_TITLE) or "").upper() not in legal:
                        continue
                    out.append(n.split("_C_")[0])
                if not out:
                    out = None
            except Exception:                                     # noqa: BLE001
                out = None
        self._cs_cache = (now, out)
        return out

    # ---- 三选一候选的**评估**（洗牌型 + 预报第二层通用；9 条路径那种只属于预报第一层）----
    _ST_ATK, _ST_DFN, _ST_COST, _ST_TYPE = 0x6C, 0x74, 0x80, 0x68
    _ST_TYP = {3: "tank", 4: "fighter", 5: "bomber", 6: "infantry", 7: "artillery",
               8: "antiair", 9: "antitank", 10: "tankdestroyer"}

    def _static_index(self):
        """静态卡表 `name(无 _C) → ptr` 索引（一次建好；2019 条）。"""
        m = getattr(self, "_st_index", None)
        if m is not None:
            return m
        km, m = self._km(), {}
        if km is not None:
            try:
                import struct as _struct
                from kardsmem.gs import load as _load_gs
                gs = _load_gs(km)
                t = gs.static_card_table()
                blob = km.m.read_exact(t["ptr"], (t["num"] or 0) * 8)
                pool = km.names_pool()
                for i in range(0, len(blob), 8):
                    p = _struct.unpack_from("<Q", blob, i)[0]
                    n = pool.fname_of(p)
                    if n:
                        m[n.split("_C_")[0]] = p
            except Exception:                                     # noqa: BLE001
                m = {}
        self._st_index = m
        return m

    def score_candidates(self, st, cands):
        """候选（内部名）在当前场面的估值：把候选当**临时手牌**塞进 `BE` 模拟，
        按 `sim_deploy` / `sim_order` 后的 `delta` 打分。跑不出来 ⇒ None（调用方退回旧启发式）。
        返回 `[(name, score), ...]`，顺序 = 传入顺序。
        """
        km, idx = self._km(), self._static_index()
        if km is None or not idx or not st or not cands:
            return None
        # ★ 选牌是**一次性决策**，VM 预算要单独放宽：默认 `vm_budget_s=3.0` 是"每回合"的额度，
        #   评估 9 条路径 × 逐对目标会把它吃光，后面的卡只分到 0.2 s 就超时 →
        #   全部退化成常量 `_est`（实测：这样三条路径会得到一模一样的分数）。
        _old = (self.P.get("vm_budget_s"), self.P.get("vm_one_s"))
        try:
            self.P["vm_budget_s"] = self._vm_spent + 30.0
            self.P["vm_one_s"] = max(float(self.P.get("vm_one_s") or 1.0), 3.0)
            import types as _types
            # ★ 候选牌是**下回合开始**才进手牌（预报写变量、下回合 spawn；三选一加入手牌也是留着下回合用）⇒ 用"下回合的指挥点"评估：
            #   槽位 +1（夹到 getMaxPossibleKredits），而不是本回合剩下的零头——否则贵牌（6 费）和便宜牌拿到的是同一个"买不起"的待遇。
            _slots = (getattr(st, "slots", None) or {}).get(LOCAL)
            _mx = int(getattr(st, "max_possible_kredits", None) or 24)
            kred = float(min(int(_slots) + 1, _mx)) if isinstance(_slots, int) else float(int((st.kredits or {}).get(LOCAL) or 0))
            fake, info = [], []
            if not hasattr(self, "_cand_dbg"):
                self._cand_dbg = {}
            for i, nm in enumerate(cands):
                p_ = idx.get(nm)
                if not p_:
                    continue
                atk = km.m.i32(p_ + self._ST_ATK) or 0
                dfn = km.m.i32(p_ + self._ST_DFN) or 0
                cost = km.m.i32(p_ + self._ST_COST) or 0
                ty = self._ST_TYP.get(km.m.u8(p_ + self._ST_TYPE))
                # ★ 2026-10-03：临时手牌的 id **按牌名固定且互不相同**。以前用 `-1000 - i`（候选序号），
                #   于是不同次调用里“第 0 个候选”都是同一个 id，`_eff_cache`/`_kw_cache` 按 card_id 命中，
                #   晴/雨/风暴三条路径的同档牌拿到的是**第一条路径缓存的效果**（实机：DELUGE 的效果摘要
                #   = HEATWAVE 的 `attack_turn`，三种天气同分）。
                ids = self.__dict__.setdefault("_fake_ids", {})
                cid = ids.setdefault(nm, -1000 - len(ids))
                fake.append(_types.SimpleNamespace(
                    card_id=cid, name=nm, side=LOCAL, location="hand",
                    attack=atk, defense=dfn, total_attack=None, total_defense=None,
                    kredit_cost=cost, card_type=ty or "order", raw={"ptr": p_},
                    is_being_guarded=False, total_operation_cost=None, operation_cost=0,
                    gotcha_activated=0, enter_play_on_turn=None))
                info.append((cid, nm, atk, dfn, cost, ty or "order"))
            if not fake:
                return None
            # 带目标的候选（天气预报牌那类）：照 `_search_sim` 的做法**逐对目标**空跑效果
            #   —— 不枚举目标的话，它们会被当成"只有费用"，三条路径的分数会一模一样。
            board = [c for c in st.cards if getattr(c, "location", None) in ("frontline", "back")]
            ehq = next((c for c in st.cards if c.side == ENEMY and c.location == "hq"), None)
            legal, pair_eff = {}, {}
            for cid, nm, atk, dfn, cost, ty in info:
                c = next((x for x in fake if x.card_id == cid), None)
                lt = [None]                      # 不指向也算一种走法
                # 运行时旗标说「不用指向」（范围/随机目标）⇒ 与 `_search_sim` 一致：不替它挑目标。
                # 否则范围效果会被套到"最好的一个目标"上，分数虚高（实测 scorching_sun2 = 6.24，缺口却最高分）。
                if ty not in UNIT_TYPES and c is not None and self.plan(c).needs_target:
                    tg = list(board) + ([ehq] if ehq is not None else [])
                    for t in tg[: self.P.get("legal_max_targets", 14)]:
                        try:
                            pair_eff[(cid, t.card_id)] = self._hand_eff(c, t)
                        except Exception:                         # noqa: BLE001
                            continue
                        lt.append(t.card_id)
                legal[cid] = lt
            sim = BE.from_cards(list(st.cards) + fake, self._kw,
                                actionable=lambda c: True, kredits=kred,
                                hand_eff=lambda c: self._hand_eff(c),
                                legal=legal, pair_eff=pair_eff)
            out = []
            for cid, nm, atk, dfn, cost, ty in info:
                c = next((x for x in fake if x.card_id == cid), None)
                if ty in UNIT_TYPES:
                    after = BE.sim_deploy(sim, cid, atk, dfn, cost, ty,
                                          kw=(self._kw(c) if c else ()), hand_id=cid)
                    out.append((nm, BE.delta(sim, after, self._W)))
                else:
                    best_v = None
                    try:                                   # 留痕：这张候选牌取到的效果摘要（排查“不同牌同分”）
                        _e0 = self._hand_eff(c) if c is not None else {}
                        self._cand_dbg[nm] = (json.dumps(_e0, ensure_ascii=False, default=str)[:240],
                                              len(legal.get(cid) or []))
                    except Exception as _exc:              # noqa: BLE001
                        self._cand_dbg[nm] = ("留痕失败 %s" % type(_exc).__name__, 0)
                    for tid in legal.get(cid) or [None]:
                        aft = BE.sim_order(sim, cid, tid)
                        v = BE.delta(sim, aft, self._W)
                        if getattr(aft, "enter_mods", None):
                            # 常驻进场效果（JUNGLE FEVER）：单打一张没有收益，价值在"之后进场的单位"——再试各手牌单位部署一次
                            for h in list(aft.hand.values()):
                                if h.is_unit() and h.id is not None and h.id >= 0 and h.cost <= aft.kredits:
                                    v = max(v, BE.delta(sim, BE.sim_deploy(aft, -(h.id + 5000), h.atk, h.dfn, h.cost, h.typ,
                                                                          h.kw, hand_id=h.id), self._W))
                        if best_v is None or v > best_v:
                            best_v = v
                    out.append((nm, best_v if best_v is not None else 0.0))
            return out
        except Exception:                                         # noqa: BLE001
            return None
        finally:
            self.P["vm_budget_s"], self.P["vm_one_s"] = _old

    def _vm_eff(self, c, target_card=None, one_s=None, count=True):
        """VM 空跑一张牌（对某个目标）→ 效果摘要 dict；跑不了 ⇒ None。随机结果已枚举。
        `one_s`：单次上限覆盖（预热用更长的）；`count=False`：不计入本回合预算（预热在对手回合里跑）。"""
        if not self.P["use_vm"]:
            return None
        tid = getattr(target_card, "card_id", None)
        key = ("vm", c.card_id, tid)
        seed = self._rng_seed()
        if key in self._eff_cache:
            hit = self._eff_cache[key]
            # 带"确定随机"的效果是按**当时的活种子**算的：种子变了（别的随机/对手动作消耗了抽取）就作废重算
            if not (isinstance(hit, dict) and hit.get("_seed") is not None and hit.get("_seed") != seed):
                return hit
            del self._eff_cache[key]
        if count and (self._vm_spent >= self.P["vm_budget_s"]
                      or (key in self._eff_timeouts and one_s is None)):
            return None                       # 本回合预算用完 / 已知超时过（只有预热才用更长上限重试）
        ptr = (getattr(c, "raw", None) or {}).get("ptr")
        km = self._km()
        if km is None or not ptr:
            return None
        tptr = (getattr(target_card, "raw", None) or {}).get("ptr") if target_card is not None else 0
        t0 = time.time()
        try:
            r = EV.enumerate_effects(km, ptr, tptr or 0, target_card is not None, hook=None,
                                     my_side=getattr(self._st, "my_side_raw", None),
                                     board=self._st, my_seat=getattr(self._st, "my_side_raw", None),
                                     read_hooks=EV.make_read_hooks(
                                         self._st, getattr(self._st, "my_side_raw", None)),
                                     slots=dict(getattr(self._st, "slots", None) or {}),
                                     hq_own=self._hq_ptrs("local"), hq_enemy=self._hq_ptrs("enemy"),
                                     rng_seed=seed,
                                     # 评估的常是静态卡/临时手牌（对象上 side=0）：把“这张牌是我方的”按真实座位喂给空跑，
                                     # 而不是在判据里把“读不出座位”当成我方（座位不可能读不出，读不出就是 bug）。
                                     field_overrides=({(ptr, "side"): self._st.my_side_raw}
                                                      if getattr(c, "side", None) == LOCAL
                                                      and getattr(self._st, "my_side_raw", None) in (1, 2) else None),
                                     budget_s=(one_s if one_s else max(
                                         0.2, min(self.P["vm_one_s"],
                                                  self.P["vm_budget_s"] - self._vm_spent))))
        except Exception:                                         # noqa: BLE001
            r = None
        if count:
            self._vm_spent += time.time() - t0
        eff = None
        if r and "超时" in str(r.get("stopped") or ""):
            # 超时停在半路：录到的只是一部分效果，不当真（退回静态常量并记成缺口）。
            # 不写进 _eff_cache：预热（对手回合、上限更长）还能重试。
            self.gaps[c.name] = "VM 超时（%s）" % (r.get("stopped") or "")[:40]
            self._eff_timeouts[key] = True
            return None
        if r and not any(e for _, e in (r.get("outcomes") or [])) and r.get("stopped"):
            # VM 停在某个**没实现的原语**上（例：热浪2 缺 `GetCardsOnBoardBySide`、
            # 好人寥寥 缺 `selectCardToDraw`）。把"具体停在哪"记进缺口 ——
            # 只写一句"VM/静态都没取到效果"，看不出该补什么，等于把可行动的信息丢掉。
            #
            # ★ 2026-10-02（TODO A6，用户报"假缺口"）：`effectvm` 对"**这张牌根本没覆写
            #   打出/部署钩子**"也回 `stopped="这张牌没有 …覆写"` —— 那不是缺口（它本来就没
            #   打出效果），和已修的 `vm(空)` 同类。这类记成 `eff_src="无覆写"`、**不进 gaps**，
            #   否则 `MAGNIFICENT SEVENTH`/`RED DEVILS`/`22nd MARINES` 这种纯身材单位会一直
            #   被当成"取不到效果"，把真缺口（`Unimplemented @…`）淹掉。
            _stop = str(r.get("stopped") or "")
            if "没有" in _stop and "覆写" in _stop:
                self.eff_src.setdefault(c.name, "无覆写")
            elif target_card is None and "在 None 上调用" in _stop and self.plan(c).needs_target:
                # 需要指向的牌（FEROCIOUS ASSAULT：给一个友军 +攻…）不带目标空跑，必然停在“对 None 调用 getTotalAttack”——
                # 这不是缺口：它的效果由**逐目标**的评估给出（`pair_eff`）。
                self.eff_src.setdefault(c.name, "需目标(逐目标评估)")
            else:
                self.gaps[c.name] = "VM 停在：%s" % _stop[:120]
        if r and r.get("outcomes") and (r.get("complete") or any(e for _, e in r["outcomes"])):
            outs = r["outcomes"]
            eff = dict(outs[0][1]) if len(outs) == 1 else {"outcomes": [(w, dict(e)) for w, e in outs]}
            if len(outs) > 1 and r.get("choice"):
                eff["outcomes_mode"] = "max"          # 抉择：选项是我们自己选的
            eff["_src"] = "vm" if r.get("complete") else "vm-partial"
            if r.get("exact"):
                eff["_seed"] = seed                   # 确定随机：记下是按哪个种子算的
                eff["_src"] += "+rng"
        self._attach_fight_dmg(eff)
        self._eff_cache[key] = eff
        return eff

    def _attach_fight_dmg(self, eff) -> None:
        """效果里有 `fight=[a,b]` ⇒ 过改伤钩子（0x25/0x26 …）预算两个方向的伤害，写进 `fight_dmg=(a→b, b→a)`。
        只在有人覆写改伤钩子时才有差别；算不出（无盘面/无 VM）就不加，boardeval 按裸 atk 并记缺口。"""
        fs = eff.get("fight") if isinstance(eff, dict) else None
        st = getattr(self, "_st", None)
        km = self._km()
        if not (isinstance(fs, (list, tuple)) and len(fs) == 2) or st is None or km is None:
            return
        try:
            from semantics import triggers as TR
            by_id = {c.card_id: c for c in (st.cards or [])}
            a, b = by_id.get(fs[0]), by_id.get(fs[1])
            if a is None or b is None:
                return
            tc = self._trig_cache(st)
            seat = getattr(st, "my_side_raw", None)
            stream = self._trig_stream()
            res = TR.fight_damage(km, st, a, b, stream=stream, my_side=seat,
                                  read_hooks=EV.make_read_hooks(st, seat), budget_s=0.6, cache=tc,
                                  kw_of=self._kw, hq_own=self._hq_ptrs("local"), hq_enemy=self._hq_ptrs("enemy"),
                                  enum_random=stream is None)
            eff["fight_dmg"] = (res["to_b"], res["to_a"])
        except Exception:                                         # noqa: BLE001
            pass

    def warm(self, st, budget_s: float = 2.0, one_s: float = 6.0) -> int:
        """预热：在**对手回合**（空闲）里把手牌的 VM 效果算好放进缓存，我方回合决策直接命中。
        只算不带目标的牌（带目标的按 (牌,目标) 分别算，目标随回合变）；每次调用最多用 `budget_s` 秒，
        没算完下一个轮询周期接着算。返回本次新算了几张。"""
        if not self.P["use_vm"]:
            return 0
        self._st = st
        t_end = time.time() + budget_s
        n = 0
        for c in st.hand(LOCAL):
            if c.name in self.avoid or time.time() > t_end:
                continue
            if self._eff_cache.get(("vm", c.card_id, None)) is not None or ("vm", c.card_id, None) in self._eff_cache:
                continue
            try:
                if self.plan(c).needs_target and not self.is_unit(c):
                    continue
            except Exception:                                     # noqa: BLE001
                pass
            self._vm_eff(c, None, one_s=one_s, count=False)
            n += 1
        return n

    def _after_hq(self, st, ehq) -> dict:
        """场上己方单位的「攻击总部后触发」效果（`OnAfterAttack`，防守方是总部时才有）：
        {card_id: 效果摘要}。攻击力为 0 也会触发 ⇒ 让搜索知道「上线 → 打总部」有收益。
        走 VM（不查外部表）；随机结果取期望。同一回合内按 (单位, 回合) 缓存。"""
        out = {}
        km = self._km()
        if km is None or ehq is None or not self.P["use_vm"]:
            return out
        from semantics import cardprobe
        eptr = (getattr(ehq, "raw", None) or {}).get("ptr")
        own = next((c for c in st.cards if c.side == "local" and c.location == "hq"), None)
        optr = (getattr(own, "raw", None) or {}).get("ptr") if own is not None else None
        for c in st.cards:
            if c.side != "local" or c.location not in ("frontline", "back"):
                continue
            key = ("aa", c.card_id, getattr(st, "turn", None))
            if key in self._eff_cache:
                if self._eff_cache[key]:
                    out[c.card_id] = self._eff_cache[key]
                continue
            self._eff_cache[key] = None
            ptr = (getattr(c, "raw", None) or {}).get("ptr")
            try:
                if not ptr or "OnAfterAttack" not in (cardprobe.profile(km, c).get("handlers") or []):
                    continue
                r = EV.enumerate_effects(
                    km, ptr, 0, False, hook="OnAfterAttack",
                    my_side=getattr(st, "my_side_raw", None),
                    read_hooks=EV.make_read_hooks(st, getattr(st, "my_side_raw", None)),
                    slots=dict(getattr(st, "slots", None) or {}),
                    args={"defenderCard": eptr, "wasShockAttack": False,
                          "attackCost": _n(getattr(c, "operation_cost", None), 1)},
                    hq_own=(optr,), hq_enemy=(eptr,), budget_s=min(self.P["vm_one_s"], 0.6))
            except Exception:                                     # noqa: BLE001
                continue
            if not r or not r.get("complete") or not r.get("outcomes"):
                continue
            outs = r["outcomes"]
            eff = dict(outs[0][1]) if len(outs) == 1 else {"outcomes": [(w, dict(e)) for w, e in outs]}
            eff.pop("uncertain", None)
            if eff:
                self._eff_cache[key] = eff
                out[c.card_id] = eff
                self.eff_src[c.name + "#after_attack"] = "vm"
        return out

    def _hq_ptrs(self, side) -> tuple:
        """某一方总部卡的指针（给 VM 把「对总部的 ChangeDefense」记成回血/受伤）。"""
        try:
            return tuple((getattr(c, "raw", None) or {}).get("ptr")
                         for c in self._st.cards if c.side == side and c.location == "hq")
        except Exception:                                         # noqa: BLE001
            return ()

    def _hand_eff(self, c, target_card=None) -> dict:
        # 预报牌的效果保留 `forecast` 标记；两层选择由 `sim.engine.run` 挂起展开（候选表见 `_fc_table`）
        return self._hand_eff_base(c, target_card)

    def _fc_path_scores(self, st):
        """预报的 9 条路径各自的估值 `[(天气, 变体, 内部名, 分数)]`（活种子预测 + 把候选当临时手牌评估，
        与 `choose_pick` 第一层同一把尺）。按 (回合, 种子) 缓存——选牌前不重复算。读不出 ⇒ []。"""
        key = (getattr(st, "turn", None), self._rng_seed())
        c = getattr(self, "_fcps_cache", None)
        if c and c[0] == key:
            return c[1]
        out = []
        paths0 = self._fc_paths()
        if paths0:
            for w in ("sunny", "rain", "storm"):
                row0 = paths0.get(w) or {}
                vs = [(t, row0.get(t)) for t in ("light", "medium", "heavy") if row0.get(t)]
                sc = dict(self.score_candidates(st, [n for _t, n in vs]) or [])
                for t, n in vs:
                    if n in sc:
                        out.append((w, t, n, float(sc[n])))
        self._fcps_cache = (key, out)
        return out

    def _fc_table(self, st):
        """预报两层提示的**确定候选表** `{天气: {变体: (内部名, 价值提示)}}`——候选由活种子预测（随机已算成确定结果），
        价值提示 = 把候选当临时手牌评估的分数（策略侧预估；`sim` 只当数据存进 `pending_cards`）。读不出 ⇒ None。"""
        try:
            sc = self._fc_path_scores(st)
        except Exception:                                         # noqa: BLE001
            sc = []
        if not sc:
            return None
        tab = {}
        for w, t, n, v in sc:
            tab.setdefault(w, {})[t] = (n, float(v))
        return tab

    def _standing_enter_fx(self, c) -> Optional[dict]:
        """指令的**常驻进场效果**（`OnOtherCardEnterPlay`）：对一个假想的"之后进场的己方单位"空跑它的钩子，得到每个进场单位吃到的
        效果摘要。守卫字段按"本回合已打出"覆盖（`enterPlayOnTurn`＝本回合、`side`＝我方座位）——只读覆盖，不写游戏。
        探针单位用场上任一己方单位（钩子只看它在场/是单位/同 side）；场上一个都没有 ⇒ None（记缺口）。按 (卡, 回合, 探针) 缓存。"""
        st, km = self._st, self._km()
        ptr = (getattr(c, "raw", None) or {}).get("ptr")
        if st is None or km is None or not ptr or getattr(st, "my_side_raw", None) is None:
            return None
        probe = next((x for x in st.cards if x.side == LOCAL and x.location in ("frontline", "back")
                      and (getattr(x, "raw", None) or {}).get("ptr")), None)
        if probe is None:
            self.gaps[c.name + "#standing"] = "常驻进场效果：场上没有己方单位可当探针，无法空跑 OnOtherCardEnterPlay"
            return None
        key = ("standing", c.name, getattr(st, "turn", None), probe.card_id)
        if key in self._eff_cache:
            return self._eff_cache[key]
        out = None
        try:
            r = EV.record_effects(km, ptr, 0, False, hook="OnOtherCardEnterPlay", my_side=st.my_side_raw,
                                  read_hooks=EV.make_read_hooks(st, st.my_side_raw),
                                  args={"cardPlayed": probe.raw["ptr"], "Method": 1},
                                  board=st, my_seat=st.my_side_raw, timeout_s=4.0,
                                  field_overrides={(ptr, "enterPlayOnTurn"): st.turn, (ptr, "side"): st.my_side_raw})
            if r.get("stopped"):
                if not ("没有" in str(r["stopped"]) and "覆写" in str(r["stopped"])):
                    self.gaps[c.name + "#standing"] = "常驻进场效果 VM 停在：%s" % str(r["stopped"])[:100]
            else:
                eff = {k: v for k, v in (r.get("eff") or {}).items() if k not in ("buff_ids", "uncertain")}
                out = eff or None
        except Exception as ex:                                   # noqa: BLE001
            self.gaps[c.name + "#standing"] = "常驻进场效果出错：%s: %s" % (type(ex).__name__, ex)
        self._eff_cache[key] = out
        return out

    def _hand_eff_base(self, c, target_card=None) -> dict:
        pl = self.plan(c)
        unit = self.is_unit(c)
        cost = _n(getattr(c, "kredit_cost", None), 0)
        e = self._vm_eff(c, target_card)
        src = "vm" if e else None
        if not e:
            e = dict(pl.effects or {})
            src = "static" if e else None
        if not e and not unit and target_card is None:
            # 没有打出钩子的指令可能是**当回合常驻**的（JUNGLE FEVER：之后进场的己方单位 +2 攻 + 闪击）：
            # 它的效果在 `OnOtherCardEnterPlay` 里，按"已打出"空跑一遍得到对每个进场单位的效果
            sfx = self._standing_enter_fx(c)
            if sfx:
                e, src = {"enter_mod": sfx}, "vm+standing"
        e = dict(e)
        e.pop("uncertain", None)
        if pl.side and "target" not in e:
            e["target"] = "enemy" if pl.side == "enemy" else "friend"
        # 运行时旗标说「不用指向」，摘要里却有「对目标…」的效果 ⇒ 是范围/随机目标之类，我们没建模：
        # 记成覆盖缺口并去掉这些无处可施的效果，改用保守估值（不静默地套到不存在的目标上）。
        _tk = ("damage", "destroy", "retreat", "pin", "unpin", "give", "buff", "armor")
        _scoped = bool(e.get("buff_ids"))                 # 效果已按目标卡逐张记账 ⇒ 范围已知，不是缺口（哪怕眼下只有一张）
        if (not unit and not pl.needs_target and target_card is None
                and any(k in e for k in _tk) and "outcomes" not in e and not _scoped):
            for k in _tk:
                e.pop(k, None)
            self.gaps[c.name] = "效果需要目标，但运行时旗标说不用指向（范围/随机目标？）"
            e["_est"] = self.P.get("unknown_order_est", 0.0)   # ★ 不编造价值（见 PARAMS 注释）
            src = "gap"
        known = [k for k in e if k not in ("target", "_src", "_est")]
        if not known and not unit and "_est" not in e:
            # ★★ 2026-10-02（用户："取不到效果摘要 是不可能的"）：先分清两种情况 ——
            #   ① **VM 跑完了、当前场面就是没有效果**（`_src=="vm"`）：这是**已知的空**。
            #      实机例：6K 骄阳（"友方空军行动费 0 + 攻击时对总部造成伤害"）在**没有空军**时
            #      空跑就是 {}；天气牌在没有合法目标时也是 {}。这正是用户点名的场景 ——
            #      不能把它叫"缺口"、更不能编造价值；打出去只剩丢手牌的负收益 ⇒ 不空打。
            #   ② VM 没跑出来（超时/停在未实现的原语）⇒ 才是**真缺口**：记下来供补，
            #      同样不编造价值（旧默认 order_mult*cost 让 6 费空打牌净 +3.24）。
            if src == "vm":
                e["_vm_empty"] = True
                self.eff_src[c.name] = "vm(空)"
                return e
            if self.eff_src.get(c.name, "").startswith("需目标") and target_card is None:
                e["_est"] = self.P.get("unknown_order_est", 0.0)        # 不带目标时无价值；价值在逐目标评估里
                src = "需目标"
            else:
                self.gaps.setdefault(c.name, "VM/静态都没取到效果")   # 别盖掉 _vm_eff 记的具体原因
                e["_est"] = self.P.get("unknown_order_est", 0.0)
                src = "gap"
        # ★ 情报触发（0x1C，2026-10-02）：打出 `cipher>0` 的牌 ⇒ 场上带 `OnIntelTriggered`
        #   覆写的卡逐个 VM 空跑；效果以 `unit_eff` 逐牌合并（看手牌不算，只算触发效果）。
        try:
            trig = self._intel_triggers(getattr(self, "_st", None), c, eff=e)
            if trig:
                e = dict(e)
                e["unit_eff"] = trig
                src = (src or "none") + "+intel"
        except Exception as _exc:                             # noqa: BLE001
            self.marker_err["intel"] = "%s: %s" % (type(_exc).__name__, str(_exc)[:110])
        # ★ 牌库变化触发（0x3 OnAfterDeckChanged，用户 2026-10-02："友方洗切卡组时也有
        #   类似的钩子"）：洗牌/塞牌/洗入等之后，LOVAT SCOUTS / RM ROMA / BETASOM 会
        #   重打"牌库顶 Navy 标记"。效果同样以 unit_eff 逐牌合并。
        try:
            trig_d = self._deck_changed_triggers(getattr(self, "_st", None), e)
            if trig_d:
                e = dict(e)
                e["unit_eff"] = list(e.get("unit_eff") or []) + trig_d
                src = (src or "none") + "+deckchg"
        except Exception as _exc:                             # noqa: BLE001
            self.marker_err["deckchg"] = "%s: %s" % (type(_exc).__name__, str(_exc)[:110])
        # ★ 打出卡触发族（用户点名的 5th SASEBO SNLF 那类：打出 Navy 卡时自己 +1/+1）。
        #   `triggers.run_play_hooks` 早就实现，rule 一直没接 —— 这里折进 unit_eff。
        try:
            trig_p = self._play_hooks_triggers(getattr(self, "_st", None), c,
                                               seed=self._rng_seed())
            if trig_p:
                e = dict(e)
                e["unit_eff"] = list(e.get("unit_eff") or []) + trig_p
                src = (src or "none") + "+hooks"
        except Exception as _exc:                             # noqa: BLE001
            self.marker_err["hooks"] = "%s: %s" % (type(_exc).__name__, str(_exc)[:110])
        # ★ 洗牌触发（0x16 `OnDeckShuffled`，用户点名的 SABAE REGIMENT "洗牌时"）：
        #   仅 `skipSubAction=true` 的洗牌（见 `_deck_shuffled_triggers` 注释）。
        try:
            trig_s = self._deck_shuffled_triggers(getattr(self, "_st", None), c, e)
            if trig_s:
                e = dict(e)
                e["unit_eff"] = list(e.get("unit_eff") or []) + trig_s
                src = (src or "none") + "+shuffled"
        except Exception as _exc:                             # noqa: BLE001
            self.marker_err["shuffled"] = "%s: %s" % (type(_exc).__name__, str(_exc)[:110])
        self.eff_src[c.name] = src or "none"
        return e

    def _bond_factions(self, st):
        """`activeBondFactions` 的等价物（BP `BP_GameState_Battle::SetActiveBondsAtStartOfTurn(side)`）：
        **回合开始时**该行动方场上单位（`!IsUnrevealedCovertCard ∧ IsUnit ∧ IsLocatedOnBoard`）的国家集合。

        ★ 按 `st.turn` 缓存**快照**：这个集合只在回合开始算一次 —— 回合中新部署的单位不算
          （否则我们会比游戏更宽松，误判协力已满足）。
        ★ 2026-10-02 实机更正：集合属于**当前行动方**，不是永远属于我们 —— 游戏在**每个回合开始**
          调 `SetActiveBondsAtStartOfTurn(side)` 覆盖它。实测（协力卡组那局）敌方回合里
          `activeBondFactions` 与我方场上国家不一致，正是这个原因。所以按 `st.our_turn` 选边：
          我方回合 = 我方场上国家（打我方手牌的判定场景），敌方回合 = 敌方场上国家。
          读不到 `our_turn` 时保守按我方算（旧行为）。
        """
        turn = getattr(st, "turn", None)
        our = getattr(st, "our_turn", None)
        side = ENEMY if our is False else LOCAL
        c = getattr(self, "_bondfaction_cache", None)
        if c and c[0] == (turn, side):
            return c[1]
        out = set()
        for x in (getattr(st, "cards", None) or []):
            if getattr(x, "side", None) != side:
                continue
            if getattr(x, "location", None) not in ("frontline", "back"):
                continue
            if not self.is_unit(x):
                continue
            # BP 的 IsUnrevealedCovertCard = hasCovert ∧ !isRevealed（隐蔽未揭示的除外）
            if "covert" in (getattr(x, "keywords", None) or []) and not getattr(x, "is_revealed", False):
                continue
            f = getattr(x, "faction_enum", None)
            if f is not None:
                out.add(int(f))
        self._bondfaction_cache = ((turn, side), out)
        return out

    def _intel_triggers(self, st, played_card, eff=None) -> list:
        """打出 `cipher>0` 的卡时触发的 **0x1C（`OnIntelTriggered`）** 效果，返回
        `[(触发卡 id, 该卡的 VM 空跑效果)]`。

        BP 链路：打出牌的**触发值**来自两条路 —— ① `OnCardPlayedFromHand` 里
        `cardPlayed->cipher > 0`；② 卡自己的 `OnPlayedFromHand` 直接调
        `SetCardsSeenByCipher(n, …)`（`eff["intel_seen"]`：CRUISER SCOUTS n=3、
        STRETCH THE LINE n=场上 LEGION 数量 —— 用户 2026-10-02 实机指出）。
        两者最终都是 `SetCardsSeenByCipher` ⇒ ① 遍历带 trigger 0x1C 的卡，
        调 `OnIntelTriggered(instigatorCard=情报卡, intelValue=n)`；
        ② 把对方 N 张手牌标 `cardSeen`（＝看手牌，不估值）。

        ★ 用户 2026-10-02 口径：**"看敌方手牌"不评估**（对算法没意义、不好估值），
          只把 ① 触发的实际效果折进 eval。BP 里覆写 0x1C 的只有 6 张：
          INNISKILLING FUSILIERS（随机 3 伤敌方）、THUNDER DIVISION（其他友方 +2+2）、
          NAKAJIMA B5N2（自身 +1+1）、7th SCOTTISH BORDERERS（HQ 伤害，依赖看手牌 ⇒ 部分）、
          LEGION（自身 +1+1）、16th TARNOW REGIMENT（生成 LEGION + 抽 1）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None:
            return []
        # ★ 2026-10-02（用户纠正）：cipher 一律走**快照字段**（`board_api.Card.cipher`）——
        #   快照/缓存是唯一数据源，绕过它会出现"同一步里两份不一致的读数"，而且缓存
        #   本来就是为了不反复读进程内存。`m.i32(ptr+0x25C)` 这种直读**最多临时诊断用**，
        #   不进默认代码路径。监听器里字段缺失时，正确做法是 `importlib.reload(board_api)`
        #   （模块 dict 就地更新，已有 MemoryBoardSource 实例后续产出的快照就会带 cipher）。
        try:
            cipher = int(getattr(played_card, "cipher", 0) or 0)
        except Exception:                                         # noqa: BLE001
            cipher = 0
        ptr_played = (getattr(played_card, "raw", None) or {}).get("ptr")
        intel_n = cipher
        if isinstance(eff, dict) and eff.get("intel_seen"):
            try:
                intel_n = max(intel_n, int(eff["intel_seen"]))
            except Exception:                                     # noqa: BLE001
                pass
        if intel_n <= 0:
            # 按**卡名**记录（不覆盖）：下一局直接能看到"命中情报牌名的卡读到的 cipher 是几" ——
            # 若情报牌（如 CRUISER SCOUTS）这里也是 0，说明快照的 cipher 字段没读到（读侧问题）；
            # 若 >0 却没触发，就是"场上没有覆写 OnIntelTriggered 的本方卡"那条（下面会记）。
            self.intel_dbg[getattr(played_card, "name", "?")] = {
                "cipher": cipher,
                "intel_seen": (eff or {}).get("intel_seen") if isinstance(eff, dict) else None}
            return []
        try:
            from kardsmem import kismet as _kismet
            from kardsmem.objects import ObjectArray as _OA
            oa = _OA(km)
            hooks = EV.make_read_hooks(st, getattr(st, "my_side_raw", None))
            seat = getattr(st, "my_side_raw", None)
            hq_own = tuple((getattr(x, "raw", None) or {}).get("ptr")
                           for x in (st.cards or []) if x.side == LOCAL and x.location == "hq")
            hq_enemy = tuple((getattr(x, "raw", None) or {}).get("ptr")
                             for x in (st.cards or []) if x.side == ENEMY and x.location == "hq")
        except Exception:                                         # noqa: BLE001
            return []
        out = []
        # ★ 2026-10-02（§8-9 `+intel` 实机验不上）：诊断从"只看结果"改成"看过程"。
        #   旧日志只写"场上没有覆写 OnIntelTriggered 的本方卡"，但同一局快照里明明有
        #   NAKAJIMA B5N2。真原因有两层：① 旧实现读 `record_effects(...)["outcomes"]`，
        #   而 `record_effects` 没有这个键（那是 `enumerate_effects` 的；死亡链踩过同款坑，
        #   见 `_death_fx` 注释）⇒ 恒空；② 卡不在场上时 ubergraph 开头
        #   `IsLocatedOnBoard` 为假直接 return，也是空。逐卡记"跑了但为什么空"，
        #   下次实机一眼能区分这两类。
        tried: dict = {}
        for c in (getattr(st, "cards", None) or []):
            if getattr(c, "location", None) not in ("frontline", "back"):
                continue
            if getattr(c, "side", None) != LOCAL:
                continue          # BP：`Event_intelCard->side == side`（同 side 才触发）
            if getattr(c, "card_id", None) == getattr(played_card, "card_id", None):
                # 打出的牌自己刚进场（部署后）也可能带 0x1C？BP 里触发列表是"场上带该
                # trigger 的卡"，打出前快照里它还在手里 ⇒ 不在 `st.cards` 的场上集合里，
                # 这里跳过更接近"打出瞬间"的语义（拿不准就少算，不虚增）。
                continue
            ptr = (getattr(c, "raw", None) or {}).get("ptr")
            if not ptr:
                continue
            try:
                uc = oa.class_of(ptr)
                fn = (_kismet.find_function(km, uc, "OnIntelTriggered", inherited=False)
                      if uc else None)
            except Exception:                                     # noqa: BLE001
                fn = None
            if not fn:
                continue
            try:
                r = EV.enumerate_effects(km, ptr, 0, False, hook="OnIntelTriggered", my_side=seat,
                                         cap=8, read_hooks=hooks, budget_s=0.6,
                                         rng_seed=self._rng_seed(),
                                         hq_own=hq_own, hq_enemy=hq_enemy,
                                         board=st, my_seat=seat,
                                         args={"intelCard": ptr_played, "intelValue": intel_n})
            except Exception as exc:                              # noqa: BLE001
                tried[getattr(c, "name", "?")] = "raised %s" % type(exc).__name__
                continue
            outs = (r or {}).get("outcomes") or []
            if not outs:
                tried[getattr(c, "name", "?")] = "no-outcomes stopped=%s" % (
                    str((r or {}).get("stopped"))[:60],)
                continue
            eff = (dict(outs[0][1]) if len(outs) == 1
                   else {"outcomes": [(w, dict(e)) for w, e in outs]})
            # ★ 7th SCOTTISH BORDERERS 兜底（2026-10-02）：BP 里伤害 = intelValue − 对手
            #   **未见面牌数**（`getUnseenCardsOppositeSide` = 对手手牌里 cardSeen=false 的张数）。
            #   VM 跑通（含 damage_hq）就不动；跑不通按当前快照直接补上——用户口径：
            #   "看手牌"本身不计价值，但打伤害这种浅显效果要计入。
            try:
                nm = ((getattr(c, "fname", None) or getattr(c, "name", None) or "")).lower()
                if "7th_scottish" in nm or "scottish_borderers" in nm:
                    unseen = sum(1 for x in (st.cards or [])
                                 if x.side == ENEMY and x.location == "hand"
                                 and not bool((getattr(x, "raw", None) or {}).get("card_seen")))
                    extra = intel_n - unseen
                    if extra > 0 and not eff.get("damage_hq"):
                        eff = dict(eff)
                        eff["damage_hq"] = extra
            except Exception:                                     # noqa: BLE001
                pass
            if eff:
                out.append((c.card_id, eff))
            else:
                tried[getattr(c, "name", "?")] = "empty-eff stopped=%s" % (
                    str((r or {}).get("stopped"))[:60],)
        if intel_n > 0 and not out:
            # 打出的牌**带情报值**（cipher/intel_seen>0）却没产出效果 ⇒ 记一条诊断。
            # `tried` 非空说明场上有覆写卡、链跑过（但空），此时那条"没有覆写卡"是假象；
            # `tried` 为空才是真的"场上没有覆写 0x1C 的本方卡"。下一局一眼分辨
            # "缺情报卡 / 缺触发卡 / 卡不在场上 / VM 停了"。
            names = [getattr(x, "name", "?") for x in (getattr(st, "cards", None) or [])
                     if getattr(x, "side", None) == LOCAL
                     and getattr(x, "location", None) in ("frontline", "back")]
            why = ("有覆写卡但空跑没产出（见 tried）" if tried
                   else "场上没有覆写 OnIntelTriggered 的本方卡")
            self.intel_dbg = {"why": why, "card": getattr(played_card, "name", "?"),
                              "intel_n": intel_n, "board_local": names[:8]}
            if tried:
                self.intel_dbg["tried"] = dict(tried)
            self.intel_dbg["__last__"] = {
                "why": why,
                "card": getattr(played_card, "name", "?"),
                "intel_n": intel_n, "board_local": names[:8]}
            if tried:
                self.intel_dbg["__last__"]["tried"] = dict(tried)
        return out

    def _deck_changed_triggers(self, st, eff) -> list:
        """牌库变化后的 **0x3（`OnAfterDeckChanged`）** 触发，返回 `[(卡 id, eff)]`。

        BP：`ExecuteOnAfterDeckChanged(deckSide)`（洗牌 `ShuffleDeckBySide`、塞牌
        `SpawnCardInDeckBySide`、洗入 `StealCardFromBoardToDeck`、移牌、弃牌等多处调用）
        ⇒ `FetchAllCardsWithEventTrigger(0x3)` ⇒ 每张 `OnAfterDeckChanged(deckSide)`。
        1.60 里覆写 0x3 的只有 3 张：**LOVAT SCOUTS（英）/ RM ROMA / BETASOM（意）** ——
        效果都是"维护自己**牌库顶的 Navy 类型标记**"（用户 2026-10-02："友方洗切卡组时
        也有类似的钩子"）。单步 eval 里这份标记价值小，但按"游戏发生什么就模拟什么"
        如实跑出来（谁依赖顶牌类型时就用得上）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None or not isinstance(eff, dict):
            return []
        keys = ("deck_shuffle", "deck_add", "steal_to_deck", "to_deck", "to_deck_aoe")
        if not any(k in eff for k in keys):
            return []
        try:
            from kardsmem import kismet as _kismet
            from kardsmem.objects import ObjectArray as _OA
            oa = _OA(km)
            hooks = EV.make_read_hooks(st, getattr(st, "my_side_raw", None))
            seat = getattr(st, "my_side_raw", None)
        except Exception:                                         # noqa: BLE001
            return []
        out = []
        for c in (getattr(st, "cards", None) or []):
            if getattr(c, "location", None) not in ("frontline", "back", "discard"):
                continue
            if getattr(c, "side", None) != LOCAL:
                continue
            ptr = (getattr(c, "raw", None) or {}).get("ptr")
            if not ptr:
                continue
            try:
                uc = oa.class_of(ptr)
                fn = (_kismet.find_function(km, uc, "OnAfterDeckChanged", inherited=False)
                      if uc else None)
            except Exception:                                     # noqa: BLE001
                fn = None
            if not fn:
                continue
            try:
                # ★ 与 `_intel_triggers` 同款修复：`record_effects` 没有 `outcomes` 键
                #   （恒空），必须走 `enumerate_effects` 才有分支列表。
                r = EV.enumerate_effects(km, ptr, 0, False, hook="OnAfterDeckChanged", my_side=seat,
                                         cap=8, read_hooks=hooks, budget_s=0.6,
                                         rng_seed=self._rng_seed(),
                                         board=st, my_seat=seat,
                                         args={"deckSide": seat})
            except Exception:                                     # noqa: BLE001
                continue
            outs = (r or {}).get("outcomes") or []
            if not outs:
                continue
            e1 = (dict(outs[0][1]) if len(outs) == 1
                  else {"outcomes": [(w, dict(e)) for w, e in outs]})
            if e1:
                out.append((c.card_id, e1))
        return out

    def _deck_shuffled_triggers(self, st, played_card, eff) -> list:
        """**`OnDeckShuffled`（trigger 0x16）** 的触发，返回 `[(卡 id, eff)]`。

        BP：`ShuffleDeckBySide(side, skipSubAction, instigatorID, &qqq)` 里
        **只有 `skipSubAction=true`** 才 `FetchAllCardsWithEventTrigger(0x16)` →
        `OnDeckShuffled(deckSide, instigatorCard)`；false 只发 `NotifyNewDeck`。
        卡牌自己洗大多传 false，`SpawnCardInDeckBySide(shuffle=true)` 传 true
        ⇒ 主要覆盖"**塞牌并洗**"那条路。

        用户 2026-10-02 点名 **SABAE REGIMENT（鲭江联队）**："每回合一次，我方洗牌时
        对随机敌方目标造成 **1 伤 × 场上单位数**"（`GetAllUnitsOnBoard` 计数后逐个
        `DamageCard(随机敌方, 1)`）。VM 摘要里 `damage` 取 max ⇒ 只会记 1 点，
        这里按 BP 兜底成"场上单位总数"（总伤害量对得上）。
        """
        if (not isinstance(eff, dict) or not eff.get("deck_shuffle")
                or not eff.get("deck_shuffle_skip")):
            return []
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None:
            return []
        ptr_played = (getattr(played_card, "raw", None) or {}).get("ptr")
        try:
            from kardsmem import kismet as _kismet
            from kardsmem.objects import ObjectArray as _OA
            oa = _OA(km)
            hooks = EV.make_read_hooks(st, getattr(st, "my_side_raw", None))
            seat = getattr(st, "my_side_raw", None)
        except Exception:                                         # noqa: BLE001
            return []
        out = []
        for c in (getattr(st, "cards", None) or []):
            if getattr(c, "location", None) not in ("frontline", "back"):
                continue
            if getattr(c, "side", None) != LOCAL:
                continue
            ptr = (getattr(c, "raw", None) or {}).get("ptr")
            if not ptr:
                continue
            try:
                uc = oa.class_of(ptr)
                fn = (_kismet.find_function(km, uc, "OnDeckShuffled", inherited=False)
                      if uc else None)
            except Exception:                                     # noqa: BLE001
                fn = None
            if not fn:
                continue
            try:
                # ★ 同上：`record_effects` → `enumerate_effects`（0x16 洗牌族）
                r = EV.enumerate_effects(km, ptr, 0, False, hook="OnDeckShuffled", my_side=seat,
                                         cap=8, read_hooks=hooks, budget_s=0.8,
                                         rng_seed=self._rng_seed(),
                                         board=st, my_seat=seat,
                                         args={"deckSide": seat,
                                               "instigatorCard": ptr_played})
            except Exception:                                     # noqa: BLE001
                continue
            outs = (r or {}).get("outcomes") or []
            if not outs:
                continue
            e1 = (dict(outs[0][1]) if len(outs) == 1
                  else {"outcomes": [(w, dict(e)) for w, e in outs]})
            nm = (getattr(c, "name", None) or getattr(c, "fname", None) or "").lower()
            if "sabae" in nm:
                cnt = sum(1 for x in (st.cards or [])
                          if getattr(x, "location", None) in ("frontline", "back"))
                if cnt > 0:
                    e1 = dict(e1)
                    e1["damage"] = max(int(e1.get("damage") or 0), cnt)
            if e1:
                out.append((c.card_id, e1))
        return out

    _DEATH_HOOKS = ("OnDestroyed", "OnOtherCardDestroyed", "OnBeforeDestroyed", "OnBeforeOtherCardDestroyed",
                    "OnLeaveBoardOrOwner", "OnOtherCardLeaveBoardOrOwner", "OnCardLocationMoved",
                    "OnOtherCardLocationMoved", "OnAfterLeaveBoard", "OnAfterOtherCardLeaveBoardOrOwner",
                    "OnAfterDestroyed", "OnDestructionEffectTriggered")
    # 上线族（0x32）：`ExecuteOnMoveToFrontlineCardEffects` 会问的两个钩子（见 triggers.MOVE_HOOK_SPECS）
    _MOVE_HOOKS = ("OnMoveToFrontline", "OnOtherCardMoveToFrontline")
    # 抽到族（0x2A 旁观者段）：`ExecuteOnDrawnFromDeck` 会问的钩子（见 triggers.DRAW_HOOK_SPECS）
    _DRAW_HOOKS = ("OnOtherCardDrawnFromDeck",)
    # 压制族（0x3A）/ 定住族（0x3D）：以"被判定的那张牌"为对象的单钩子族
    _SUPPRESS_HOOKS = ("OnOtherCardSuppressed",)
    _PIN_HOOKS = ("OnOtherUnitPinned",)
    # 揭示族（0x37）：`RevealCard` 里"别人看到某张牌被揭示"
    _REVEAL_HOOKS = ("OnOtherCardRevealed",)
    _VETERAN_HOOKS = ("OnBecomingVeteran", "OnOtherCardBecomingVeteran")
    _HEAL_HOOKS = ("OnBeforeFullyRepaired", "OnOtherCardFullyRepaired", "OnFullyRepaired")
    _STEAL_HOOKS = ("OnLeaveBoardOrOwner", "OnOtherCardLeaveBoardOrOwner", "OnAfterLeaveBoard",
                    "OnAfterOtherCardLeaveBoardOrOwner")
    # 撤退族（0x36）＝ `ApplyMakeCardRetreat`：自己 `OnBeforeRetreat` + 0x36 旁观者
    _RETREAT_HOOKS = ("OnBeforeRetreat", "OnOtherCardRetreat")

    def _trig_cache(self, st):
        """同一回合内共用的 `TriggerCache`（指针→类、类→函数两层缓存）；换回合清空（对象指针可能被复用）。"""
        from semantics import triggers as TR
        turn = getattr(st, "turn", None)
        tc = getattr(self, "_trigcache", None)
        if tc is None or getattr(self, "_trigcache_turn", None) != turn:
            tc = self._trigcache = TR.TriggerCache(self._km())
            self._trigcache_turn = turn
        return tc

    @staticmethod
    def _board_sig(st) -> tuple:
        return tuple(sorted((c.card_id, c.side, c.location, getattr(c, "attack", None), getattr(c, "defense", None))
                            for c in (getattr(st, "cards", None) or [])
                            if getattr(c, "location", None) in ("frontline", "back", "hq")))

    def _trig_stream(self):
        """触发链用的随机流：游戏种子读得到 ⇒ `Stream(seed)`（确定）；否则 None ⇒ 随机点枚举（不伪造）。"""
        seed = self._rng_seed()
        if seed is None:
            return None
        from kardsmem.rng import Stream
        return Stream(seed)

    def _death_fx(self, st) -> dict:
        """场上每张牌**死亡时**会发生什么 `{card_id: eff}`（VM 空跑，**完整摧毁序列**）。

        用户 2026-10-02："接"（死亡链）。2026-10-02 深化（Claude）：
        * 不再只跑死亡者自己的 `OnDestroyed`：`triggers.death_effects` 跑完整序列
          BeforeDestroyed→Leave→Moved→AfterLeave→`OnDestroyed`(+0x18 倍增)→0x27 旁观者(69 张)→打捞→
          `OnAfterDestroyed`，**killer 传真值**（见下）；
        * **修了一个真 bug**：旧实现用 `record_effects(...)["outcomes"]`，而 `record_effects` 根本没有
          `outcomes` 键（那是 `enumerate_effects` 的）⇒ `death_fx` 在实战里**恒为空**；
        * killer = 对方阵营里攻击力最高的场上单位（sim 不知道谁会下手，用它近似；没有就传 0）。
        没有任何牌覆写死亡链钩子 ⇒ 零成本直接返回 `{}`；按 (回合, 盘面签名) 缓存；总预算 3 s，超了的牌**不算**
        （记进 `self.fx_meta["death"]["skipped"]`，不编造）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None:
            return {}
        sig = (getattr(st, "turn", None), self._board_sig(st))
        cache = getattr(self, "_deathfx_cache", None)
        if cache and cache[0] == sig:
            return dict(cache[1])
        meta = self.fx_meta.setdefault("death", {}) if hasattr(self, "fx_meta") else {}
        meta.update({"victims": 0, "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0})
        out = {}
        t0 = time.time()
        try:
            from semantics import triggers as TR
            tc = self._trig_cache(st)
            present = TR.present_hooks(km, st, self._DEATH_HOOKS, tc)
            meta["hooked"] = sorted(present)
            if present:
                seat = getattr(st, "my_side_raw", None)
                hooks = EV.make_read_hooks(st, seat)
                stream = self._trig_stream()
                hq_o, hq_e = self._hq_ptrs("local"), self._hq_ptrs("enemy")
                board = [c for c in (st.cards or []) if getattr(c, "location", None) in ("frontline", "back")]

                def strongest(side):
                    cs = [c for c in board if c.side == side]
                    return max(cs, key=lambda c: (getattr(c, "attack", 0) or 0), default=None)
                for v in board:
                    meta["victims"] += 1
                    if time.time() - t0 > 3.0:
                        meta["skipped"] += 1
                        continue
                    killer = strongest(ENEMY if v.side == LOCAL else LOCAL)
                    eff = TR.death_effects(km, st, v, killer, stream=stream, my_side=seat, read_hooks=hooks,
                                           budget_s=0.6, cache=tc, kw_of=self._kw, hq_own=hq_o, hq_enemy=hq_e,
                                           enum_random=stream is None)
                    meta["computed"] += 1
                    if eff:
                        out[v.card_id] = eff
        except Exception:                                         # noqa: BLE001
            out = {}
        meta["elapsed_s"] = round(time.time() - t0, 3)
        # ★ 2026-10-02（A2 可观测性）：把"真的算出了几份死亡效果"也写进 meta ——
        #   `victims/computed` 只说跑过链，`entries>0` 才证明 `death_fx` **非空**
        #   （用户要验的正是"有 OnDestroyed 覆写的单位在场时它非空"）。
        meta["entries"] = len(out)
        meta["fx_keys"] = sorted(out)[:8]
        self._deathfx_cache = (sig, out)
        return dict(out)

    def _move_fx(self, st) -> dict:
        """我方每张场上单位**上线时**会触发什么 `{card_id: eff}`（VM 空跑，`triggers.move_effects`）。

        §8-8 的"上线/离开前线"里的**上线**这一半（0x32 族；离开前线 0x31 留给后续）。
        零成本预检：整盘没有牌覆写这两个钩子 ⇒ 直接 `{}`；按 (回合, 盘面签名) 缓存；
        总预算 3 s，超了的单位**不算**（记进 `self.fx_meta["move"]["skipped"]`，不编造）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None or not self.P.get("use_vm", True):
            return {}
        sig = (getattr(st, "turn", None), self._board_sig(st))
        cache = getattr(self, "_movefx_cache", None)
        if cache and cache[0] == sig:
            return dict(cache[1])
        meta = self.fx_meta.setdefault("move", {}) if hasattr(self, "fx_meta") else {}
        meta.update({"units": 0, "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0})
        out = {}
        t0 = time.time()
        try:
            from semantics import triggers as TR
            tc = self._trig_cache(st)
            present = TR.present_hooks(km, st, self._MOVE_HOOKS, tc)
            meta["hooked"] = sorted(present)
            if present:
                seat = getattr(st, "my_side_raw", None)
                hooks = EV.make_read_hooks(st, seat)
                stream = self._trig_stream()
                hq_o, hq_e = self._hq_ptrs("local"), self._hq_ptrs("enemy")
                board = [c for c in (st.cards or [])
                         if getattr(c, "location", None) in ("frontline", "back") and c.side == LOCAL]
                for c in board:
                    meta["units"] += 1
                    if time.time() - t0 > 3.0:
                        meta["skipped"] += 1
                        continue
                    eff = TR.move_effects(km, st, c, stream=stream, my_side=seat, read_hooks=hooks,
                                          budget_s=0.6, cache=tc, kw_of=self._kw,
                                          hq_own=hq_o, hq_enemy=hq_e,
                                          enum_random=stream is None)
                    meta["computed"] += 1
                    if eff:
                        out[c.card_id] = eff
        except Exception:                                         # noqa: BLE001
            out = {}
        meta["elapsed_s"] = round(time.time() - t0, 3)
        self._movefx_cache = (sig, out)
        return dict(out)

    def _draw_fx(self, st) -> dict:
        """**别人抽到牌时**场上/手牌其它牌的 0x2A 反应 `{抽到的卡 id: eff}`（VM 空跑）。

        §8-8 的"他人抽牌"（用户指出 `_on_draw` 目前是手写源 —— 补的就是**旁观者**这一段）。
        钩子形参 `drawnCardID` 进得去 ⇒ 键 = 抽到的那张牌的 id；按**当前牌库的去重 id 集合**
        逐个算（牌库读不到 ⇒ `{}`，不猜）。零成本预检 `present_hooks`；按 (回合, 盘面) 缓存；
        3 s 预算，超了的 id 记 `fx_meta["draw"]["skipped"]`（不编造）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None or not self.P.get("use_vm", True):
            return {}
        ids, _dcards = self._deck_state(st)
        if not ids:
            return {}
        sig = (getattr(st, "turn", None), self._board_sig(st), tuple(sorted(set(ids))))
        cache = getattr(self, "_drawfx_cache", None)
        if cache and cache[0] == sig:
            return dict(cache[1])
        meta = self.fx_meta.setdefault("draw", {}) if hasattr(self, "fx_meta") else {}
        meta.update({"ids": 0, "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0})
        out = {}
        t0 = time.time()
        try:
            from semantics import triggers as TR
            tc = self._trig_cache(st)
            present = TR.present_hooks(km, st, self._DRAW_HOOKS, tc)
            meta["hooked"] = sorted(present)
            if present:
                seat = getattr(st, "my_side_raw", None)
                hooks = EV.make_read_hooks(st, seat)
                stream = self._trig_stream()
                hq_o, hq_e = self._hq_ptrs("local"), self._hq_ptrs("enemy")
                for cid in sorted(set(int(x) for x in ids if x is not None)):
                    meta["ids"] += 1
                    if time.time() - t0 > 3.0:
                        meta["skipped"] += 1
                        continue
                    eff = TR.drawn_effects(km, st, cid, drawn_side=seat, stream=stream,
                                           my_side=seat, read_hooks=hooks, budget_s=0.6, cache=tc,
                                           kw_of=self._kw, hq_own=hq_o, hq_enemy=hq_e,
                                           enum_random=stream is None)
                    meta["computed"] += 1
                    if eff:
                        out[int(cid)] = eff
        except Exception:                                         # noqa: BLE001
            out = {}
        meta["elapsed_s"] = round(time.time() - t0, 3)
        self._drawfx_cache = (sig, out)
        return dict(out)

    def _unit_event_fx(self, st, kind: str, hooks, runner, budget_s: float = 3.0) -> dict:
        """通用：对**场上每一张单位**跑一族"以该单位为对象"的钩子 → `{单位 id: eff}`。

        用于压制（0x3A）/定住（0x3D）这类"别人看到某张牌被压制/被定住"的触发（§8-8）。
        零成本预检 `present_hooks`；按 (回合, 盘面) 缓存；`budget_s` 内算不完的记 skipped（不编造）。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None or not self.P.get("use_vm", True):
            return {}
        sig = (getattr(st, "turn", None), self._board_sig(st))
        cache = getattr(self, "_unitfx_cache", None)
        if cache and cache[0] == (kind, sig):
            return dict(cache[1])
        meta = self.fx_meta.setdefault(kind, {}) if hasattr(self, "fx_meta") else {}
        meta.update({"units": 0, "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0})
        out = {}
        t0 = time.time()
        try:
            from semantics import triggers as TR
            tc = self._trig_cache(st)
            present = TR.present_hooks(km, st, hooks, tc)
            meta["hooked"] = sorted(present)
            if present:
                seat = getattr(st, "my_side_raw", None)
                rh = EV.make_read_hooks(st, seat)
                stream = self._trig_stream()
                hq_o, hq_e = self._hq_ptrs("local"), self._hq_ptrs("enemy")
                board = [c for c in (st.cards or [])
                         if getattr(c, "location", None) in ("frontline", "back")]
                for c in board:
                    meta["units"] += 1
                    if time.time() - t0 > budget_s:
                        meta["skipped"] += 1
                        continue
                    eff = runner(km, st, c, stream=stream, my_side=seat, read_hooks=rh,
                                 budget_s=0.6, cache=tc, kw_of=self._kw,
                                 hq_own=hq_o, hq_enemy=hq_e, enum_random=stream is None)
                    meta["computed"] += 1
                    if eff:
                        out[c.card_id] = eff
        except Exception:                                         # noqa: BLE001
            out = {}
        meta["elapsed_s"] = round(time.time() - t0, 3)
        self._unitfx_cache = ((kind, sig), out)
        return dict(out)

    def _suppress_fx(self, st) -> dict:
        """被压制时（0x3A）别人会怎样 `{被压制的卡 id: eff}`。"""
        from semantics import triggers as TR
        return self._unit_event_fx(st, "suppress", self._SUPPRESS_HOOKS, TR.suppress_effects)

    def _pin_fx(self, st) -> dict:
        """被定住时（0x3D）别人会怎样 `{被定住的卡 id: eff}`。"""
        from semantics import triggers as TR
        return self._unit_event_fx(st, "pin", self._PIN_HOOKS, TR.pin_effects)

    def _reveal_fx(self, st) -> dict:
        """被揭示时（0x37）别人会怎样 `{被揭示的卡 id: eff}`。"""
        from semantics import triggers as TR
        return self._unit_event_fx(st, "reveal", self._REVEAL_HOOKS, TR.reveal_effects)

    def _veteran_fx(self, st) -> dict:
        """变老兵时（OnBecomingVeteran + 0x20）别人会怎样 `{卡 id: eff}`。"""
        from semantics import triggers as TR
        return self._unit_event_fx(st, "veteran", self._VETERAN_HOOKS, TR.veteran_effects)

    def _heal_fx(self, st) -> dict:
        """被满血治疗时（0xC 否决 / 0x2C / OnFullyRepaired）`{卡 id: eff}`；toHeal = 最大防御 − 当前总防御。"""
        from semantics import triggers as TR

        def runner(km, st_, c, **kw):
            to_heal = int(getattr(c, "max_defense", None) or 0) - int(getattr(c, "total_defense", None)
                                                                      or getattr(c, "defense", 0) or 0)
            return TR.heal_effects(km, st_, c, max(to_heal, 0), **kw) if to_heal > 0 else {}
        return self._unit_event_fx(st, "heal", self._HEAL_HOOKS, runner)

    def _steal_fx(self, st) -> dict:
        """被偷走时（离场/入场钩子）`{敌方单位 id: eff}`（只算敌方单位）。"""
        from semantics import triggers as TR
        mine = getattr(st, "my_side_raw", None) or 1

        def runner(km, st_, c, **kw):
            if getattr(c, "side", None) != "enemy":
                return {}
            new_loc = 5 if mine == 1 else 6                  # 左=5 右=6（ECardLocationEnum 支援线）
            old_loc = 6 if mine == 1 else 5
            return TR.steal_effects(km, st_, c, new_loc, old_loc, **kw)
        return self._unit_event_fx(st, "steal", self._STEAL_HOOKS, runner)

    def _retreat_fx(self, st) -> dict:
        """被迫撤退时（0x36）别人会怎样 `{撤退的卡 id: eff}`。

        ★ 注意这里的 `pre_hook` 与"被判定那张牌是谁"无关（`OnBeforeRetreat` 是撤退者自己的钩子）；
          这里算的是**旁观者** 0x36 的后果 —— 自己的 `OnBeforeRetreat` 的 stopAction 语义由
          `triggers.run_retreat` 负责（预计算时也算，但当前 Sim 只在 `_apply_eff` 里查 0x36 表）。
        """
        from semantics import triggers as TR
        return self._unit_event_fx(st, "retreat", ("OnOtherCardRetreat",), TR.retreat_effects)

    def _turn_end_fx(self, st) -> dict:
        """我方每张场上单位**自己的 `OnEndOfTurn`（0x19）**会怎样 `{卡 id: eff}`。

        §8-8 的"回合结束"这一半的**消费点**：`boardeval.sim_turn_end` 会在评估候选终态时
        把这些效果结算进去 + 清临时攻（`RemoveBuffsEndOfTurn`），于是"停手后这一回合真正
        结束时的盘面"才进了打分（用户口径：eval = 打出这一步后游戏里会发生什么就模拟什么）。
        """
        from semantics import triggers as TR
        return self._unit_event_fx(
            st, "turn_end", ("OnEndOfTurn",),
            lambda km, st_, c, **kw: TR.end_of_turn_effects(km, st_, only_card=c, **kw))

    def _attack_fx(self, st, ehq) -> dict:
        """**攻击链钩子的后果** `{(攻击者 id, 目标 id|"hq"): fx}`（`triggers.build_attack_fx`：0x1E 换目标 /
        0x1F 吞攻击 / 伤害管线 / 受击·幸存 / OnAfterAttack·0x04 / 花费通知，顺序=`AttackCard` 字节码）。
        喂 `BE.from_cards(attack_fx=...)`；`sim_attack` 据此让 stop/consumed/switch 真正改变攻击结算。
        整盘没有任何牌覆写这些钩子 ⇒ 零成本 `{}`；按 (回合, 盘面签名) 缓存；`P["use_vm"]=False` 关闭。
        摧毁链的钩子**不在这里**（`death_fx` 管，避免双算，见 `triggers.to_fx(dedupe_death)`）。"""
        km = self._km()
        if km is None or st is None or not self.P.get("use_vm", True):
            return {}
        sig = (getattr(st, "turn", None), self._board_sig(st))
        cache = getattr(self, "_attackfx_cache", None)
        if cache and cache[0] == sig:
            return dict(cache[1])
        res = {}
        try:
            from semantics import triggers as TR
            tc = self._trig_cache(st)
            board = [c for c in (st.cards or []) if getattr(c, "location", None) in ("frontline", "back")]
            foes = [c for c in board if c.side == ENEMY] + ([ehq] if ehq is not None else [])
            pairs = []
            for a in board:
                if a.side != LOCAL or not self._actionable(st, a):
                    continue
                opc = _n(getattr(a, "operation_cost", None), 1)
                for d in foes:
                    pairs.append((a, d, opc + _n(getattr(d, "kredits_tax_as_enemy_target", None), 0)))
            seat = getattr(st, "my_side_raw", None)
            stream = self._trig_stream()
            tbl = TR.build_attack_fx(km, st, pairs, my_side=seat, read_hooks=EV.make_read_hooks(st, seat),
                                     budget_s=0.6, stream=stream, kw_of=self._kw,
                                     hq_own=self._hq_ptrs("local"), hq_enemy=self._hq_ptrs("enemy"),
                                     cache=tc, total_budget_s=4.0, enum_random=stream is None)
            res = dict(tbl)
            if hasattr(self, "fx_meta"):
                self.fx_meta["attack"] = dict(getattr(tbl, "meta", {}) or {},
                                              entries=len(res), seeded=stream is not None)
        except Exception:                                         # noqa: BLE001
            res = {}
        self._attackfx_cache = (sig, res)
        return dict(res)

    def _play_hooks_triggers(self, st, played_card, seed=None) -> list:
        """**打出卡时的触发族**（`triggers.run_play_hooks`：`OnOtherCardEnterPlay` + 反制族
        `OnBefore/OnOtherCardPlayedFromHand` / `OnCounterMeasureTriggered`），返回 `[(卡 id, eff)]`。

        用户 2026-10-02 点名 **佐镇第五特别陆战队（5th SASEBO SNLF）**：
        `OnBeforeOtherCardPlayedFromHand`（打出的牌是**我方 + `subtype.navy`** ⇒ 置 shouldTrigger）
        → `OnOtherCardPlayedFromHand`（自己 +1/+1）——这条链在 `triggers.py` 里已经实现，
        但 rule/boardeval 一直**没消费**（`hit_effects` 在 rule 里零调用）。
        这里把"我方卡的响应"折进 eval；**敌方反制**（side=enemy）的效果方向语义特殊
        （打的是我们），第一版不冒险算，留给后续（会记进 gaps）。
        按 (卡, 种子) 缓存——每个决策都跑一遍太贵。
        """
        st = st if st is not None else getattr(self, "_st", None)
        km = self._km()
        if st is None or km is None:
            return []
        key = (getattr(played_card, "card_id", None), seed)
        cache = getattr(self, "_playhook_cache", None)
        if cache is None:
            cache = self._playhook_cache = {}
        if key in cache:
            return list(cache[key])
        out = []
        try:
            from semantics import triggers as TR
            from kardsmem.rng import Stream
            stream = Stream(seed) if seed is not None else None
            seat = getattr(st, "my_side_raw", None)
            res = TR.run_play_hooks(km, st, played_card, method=1, stream=stream,
                                    my_side=seat,
                                    read_hooks=EV.make_read_hooks(st, seat),
                                    # ★ 2026-10-02：这里传**指针**（`find_cards` 已能归一，
                                    #   但显式指针更不容易再踩"Card 不可哈希"那个坑）。
                                    exclude=[(getattr(played_card, "raw", None) or {}).get("ptr")],
                                    budget_s=0.8,
                                    include_counter=True)
            by_ptr = {(getattr(x, "raw", None) or {}).get("ptr"): x.card_id
                      for x in (st.cards or [])}
            for h in (res.get("hits") or []):
                eff = h.get("eff") or {}
                if not eff:
                    continue
                if h.get("side") != LOCAL:
                    continue          # 敌方反制：方向语义特殊，第一版不算
                cid = by_ptr.get(h.get("ptr"))
                if cid is None:
                    continue          # 找不到对应卡就跳过（不编 id）
                out.append((cid, eff))
        except Exception as exc:                              # noqa: BLE001
            # ★ 2026-10-02：不再静默吞。以前这里 `out = []` 一抹，`+hooks` 就算出了
            #   异常（如 exclude 不可哈希）日志里也只剩"没触发"；现在与 `_hand_eff`
            #   四族的口径一致，写进 `marker_err` 让下一局一眼看到。
            self.marker_err["hooks"] = "%s: %s" % (type(exc).__name__, str(exc)[:110])
            out = []
        cache[key] = out
        return list(out)

    def _has_bond(self, c) -> bool:
        """这张牌有没有协力（Bond）：`GameplayTags@0x30` 里有 `ability.bond`。

        ★ 游戏侧判据（SDK/BP 结论）：`HasBond` = 卡自带 tag ∨ `receivedAbilitiesFromCards["ability.bond"]`
          非空。后者是 `GiveBond` 通过 `AddCustomGameplayTag` 写进去的**来源记录**——我们的对局里
          GiveBond 极罕见，先只读卡自带的 tag；读到 `receivedAbilities` 再补。
        """
        key = (getattr(c, "raw", None) or {}).get("ptr") or getattr(c, "card_id", None)
        cache = getattr(self, "_bondcard_cache", None)
        if cache is None:
            cache = self._bondcard_cache = {}
        if key in cache:
            return cache[key]
        val = False
        ptr = (getattr(c, "raw", None) or {}).get("ptr")
        km = self._km()
        if km is not None and ptr:
            try:
                from kardsmem.cards import read_gameplay_tags
                val = any(str(t).lower() == "ability.bond" for t in read_gameplay_tags(km, ptr))
            except Exception:                                     # noqa: BLE001
                val = False
        cache[key] = val
        return val

    def _eff_bond(self, st, c, target_card=None) -> dict:
        """手牌效果 + 协力的**打出时**后果（BP `OnCardPlayedFromHand`）：
        `HasBond && !activeBondFactions.Contains(faction)` ⇒ 己方总部吃一次疲劳伤害。
        未满足才加 `bond_fatigue`；牌不是 bond 或国家已在集合里 = 无额外效果。
        """
        e = self._hand_eff(c, target_card)
        try:
            if self._has_bond(c):
                f = getattr(c, "faction_enum", None)
                if f is None or int(f) not in self._bond_factions(st):
                    e = dict(e)
                    e["bond_fatigue"] = True
        except Exception:                                         # noqa: BLE001
            pass
        return e

    def _search_sim(self, st, kred, ehq):
        """建搜索用的模拟场面：手牌带效果摘要；带目标的牌用游戏的判断函数列出合法目标。"""
        self._st = st
        self.prefetch_kw(st)                                # 关键词：一次 RPC 批量问游戏
        if getattr(st, "turn", None) != self._vm_turn:      # 预算按回合重置（缓存跨步保留）
            self._vm_turn = getattr(st, "turn", None)
            self._vm_spent = 0.0
        legal, pair_eff = {}, {}
        board = [c for c in st.cards if c.location in ("frontline", "back")]
        for c in st.hand(LOCAL):
            if c.name in self.avoid:
                continue
            if not self.plan(c).needs_target:
                # 运行时旗标说不用指向 ⇒ 就是一条不带目标的走法，搜索不替它挑目标
                if not self.is_unit(c):
                    legal[c.card_id] = [None]
                continue
            cost = _n(getattr(c, "kredit_cost", None), 99)
            if not self._playable(c, cost, kred):
                continue
            cands = [(t.card_id, t) for t in board if t.card_id != c.card_id]
            if ehq is not None:
                cands.append(("hq", ehq))
            lt = []
            for tid, t in cands[: self.P["legal_max_targets"] + 1]:
                key = ("legal", c.card_id, tid)
                ok = self._act_cache.get(key)
                if ok is None:
                    try:
                        ok = self.sess.can_play(c, target=t).get("can") is not False
                    except Exception:                             # noqa: BLE001
                        ok = True
                    self._act_cache[key] = ok
                if ok:
                    lt.append(tid)
                    pair_eff[(c.card_id, tid)] = self._eff_bond(st, c, t)
            if self.is_unit(c):
                lt.append(None)                                   # 单位：不指向也可以试
                pair_eff[(c.card_id, None)] = self._eff_bond(st, c)
            legal[c.card_id] = lt
        # 已经在手里"激活"的反制（gotcha_activated>0）不再列成走法：`ToggleGotcha` 是开关，
        # 再 play 一次会把它关掉（board_api 注释/CLAUDE.md 反制一节）。它仍留在手牌里，只是不再是候选。
        # `avoid`（用户指定不想打的牌）也在这里去掉：原来只在"带目标牌的合法目标枚举"里检查，
        # 不带目标的牌照样进搜索——实机 SEABORNE INVASION / NZANS 都在回避名单里却被打了出去。
        cards = [c for c in st.cards
                 if not (c.side == LOCAL and c.location == "hand"
                         and ((getattr(c, "gotcha_activated", 0) or 0) > 0 or c.name in self.avoid))]
        ids, dcards = self._deck_state(st)
        self._sim = BE.from_cards(
            cards, self._kw, actionable=lambda c: self._actionable(st, c), kredits=float(kred),
            hand_eff=lambda c: self._eff_bond(st, c), front_owner=getattr(st, "frontline_owner", None),
            turn=getattr(st, "turn", None), legal=legal, pair_eff=pair_eff,
            after_hq=self._after_hq(st, ehq),
            # 牌库读得到 ⇒ 抽牌按**抽到的那张**走；空库抽按游戏疲劳规则（`_fatigue` + boardeval）
            deck=ids, deck_cards=dcards,
            fatigue=self._fatigue(st),
            rng_seed=self._rng_seed(),
            death_fx=self._death_fx(st), attack_fx=self._attack_fx(st, ehq),
            event_fx={"move": self._move_fx(st), "draw": self._draw_fx(st),
                      "suppress": self._suppress_fx(st), "pin": self._pin_fx(st),
                      "reveal": self._reveal_fx(st), "retreat": self._retreat_fx(st),
                      "veteran": self._veteran_fx(st), "heal": self._heal_fx(st),
                      "steal": self._steal_fx(st),
                      "turn_end": self._turn_end_fx(st)},
            kredit_max=int(getattr(st, "max_possible_kredits", None) or 24))
        return self._sim

    @staticmethod
    def _akey(a, ehq) -> tuple:
        dst = a.dst
        if dst == "hq" and ehq is not None:
            dst = ehq.card_id
        if a.kind == "attack":
            return ("attack", a.src, dst)
        if a.kind == "move":
            return ("move", a.src, None)
        return ("play", a.src, dst) + ((tuple(a.path),) if getattr(a, "path", ()) else ())

    def _act_of(self, st, a, ehq, gain, seq) -> dict:
        names = {c.card_id: c.name for c in st.cards}
        dst = a.dst
        if dst == "hq" and ehq is not None:
            dst = ehq.card_id
        if a.kind == "attack":
            act = {"kind": "attack", "card": a.src, "target": dst}
        elif a.kind == "move":
            act = {"kind": "move_up", "card": a.src}
        elif a.kind == "deploy":
            act = ({"kind": "play_unit", "card": a.src} if dst is None
                   else {"kind": "play_unit_target", "card": a.src, "target": dst})
        else:
            act = ({"kind": "play_event", "card": a.src} if dst is None
                   else {"kind": "play_event_target", "card": a.src, "target": dst})
        if getattr(a, "path", ()):
            act["path"] = list(a.path)                       # 选择路径（行动 = 动作 + 其后全部选择）
        tail = " → ".join("%s%s" % (x.kind, "" if x.dst is None else ">%s" % x.dst) for x in seq[1:])
        act["note"] = "搜索：%s %s%s%s%s (+%.2f)" % (a.kind, names.get(a.src, a.src),
                                                  "" if dst is None else " → %s" % names.get(dst, dst),
                                                  (" 路径%s" % (list(a.path),)) if getattr(a, "path", ()) else "",
                                                  ("｜后续 " + tail) if tail else "", gain)
        return act

    def _choose_main_search(self, st, kred, ehq, ex):
        P = self.P
        asked, n_cands = 0, 0
        for _round in range(3):
            _t_a = time.perf_counter()
            vm0 = self._vm_spent
            sim = self._search_sim(st, kred, ehq)
            _t_b = time.perf_counter()
            sup = (getattr(self, "suppress_kinds", None) or set()) |                 (getattr(self, "forbid_kinds", None) or set())
            skip = lambda a: (self._akey(a, ehq) in self._bad or self._akey(a, ehq) in self._tried  # noqa: E731
                              or a.kind in sup)
            seqs = BE.search(sim, self._W, P["depth"], P["beam"], P["branch"], 12, skip=skip)
            _t_c = time.perf_counter()
            # 分段计时（TODO B1）：建 sim（含 VM/钩子预计算）与束搜索分开记，进 probe.timing
            _timing = {"build_sim_s": round(_t_b - _t_a, 3), "beam_s": round(_t_c - _t_b, 3),
                       "vm_s": round(self._vm_spent - vm0, 3), "sim_units": len(sim.units),
                       "sim_hand": len(sim.hand)}
            cands = []
            for gain, seq in seqs:
                if gain < P["min_gain"] and gain < P["lethal"] / 2:
                    continue
                a = seq[0]
                if a.src in ex:
                    continue
                cands.append((gain, self._akey(a, ehq), self._act_of(st, a, ehq, gain, seq), a.cost))
            n_cands = max(n_cands, len(cands))
            self.probe = {"kred": kred, "n": len(cands), "path": "search",
                          "eff_src": dict(self.eff_src), "gaps": dict(self.gaps),
                          "fx_meta": {k: dict(v) for k, v in self.fx_meta.items()},
                          "intel_dbg": dict(self.intel_dbg or {}),
                          "marker_err": dict(self.marker_err or {}),
                          "vm_s": round(self._vm_spent, 2), "timing": _timing,
                          "hand": [(h.name, getattr(h, "kredit_cost", None)) for h in st.hand(LOCAL)],
                          "top": [(round(c[0], 2), c[2]["note"]) for c in cands[:6]]}
            if not cands:
                break
            progressed = False
            for gain, key, act, cost in cands:
                if asked >= P["gate_budget"]:
                    break
                asked += 1
                if self._confirm(act, st):
                    self._tried.add(key)
                    act["score"] = round(gain, 3)
                    if act.get("path"):                       # 把选择路径交给执行侧：弹出选项时照点，不再重算
                        self.__dict__.setdefault("_plan", __import__("policy.plan", fromlist=["PlanStore"]).PlanStore()).put(act["card"], act["path"])
                    return act
                self._bad.add(key)
                progressed = True
            if not progressed or asked >= P["gate_budget"]:
                break
        return {"kind": "end", "note": "规则2/搜索：没有值得做的动作（候选 %d，问闸门 %d）" % (n_cands, asked)}

    # ------------------------------------------------------------ 主相位
    def _confirm(self, act: dict, st) -> bool:
        """问游戏自己；None（不知道）放行。"""
        s, k = self.sess, act["kind"]
        by_id = {c.card_id: c for c in st.cards}
        try:
            if k == "attack":
                r = s.can_attack(by_id[act["card"]], by_id[act["target"]])
            elif k in ("play_unit", "play_event"):
                r = s.can_play(by_id[act["card"]])
            elif k in ("play_unit_target", "play_event_target"):
                r = s.can_play(by_id[act["card"]], target=by_id[act["target"]])
            elif k == "move_up":
                r = s.can_move(by_id[act["card"]])
            else:
                return True
        except Exception:                                         # noqa: BLE001
            return True
        act.setdefault("meta", {})["gate"] = {kk: r.get(kk) for kk in
                                              ("can", "reason", "source") if kk in r}
        return r.get("can") is not False

    def choose_main(self, st, exclude_cards=None) -> dict:
        kred = int((st.kredits or {}).get(LOCAL) or 0)
        me, foes, ehq = self._split(st)
        ex = set(exclude_cards or ())
        if self.P["search"]:
            try:
                return self._choose_main_search(st, kred, ehq, ex)
            except Exception as e_:                               # noqa: BLE001
                # 搜索路径出了内部错误 ⇒ 退回旧的贪心候选（安全网），并把原因带进日志
                self.probe = {"search_error": "%s: %s" % (type(e_).__name__, e_)}
        asked, n_cands = 0, 0
        self._build_sim(st, kred)
        # 一批候选全被游戏否掉后，被否的进了 _bad，重新生成会冒出下一批
        # （比如"带目标部署"全被拒 → 退回"无目标部署"）。最多 3 轮。
        for _round in range(3):
            atk_c = self._attack_cands(st, me, foes, ehq, kred)
            attackers = {c[3]["card"] for c in atk_c if c[1] > 0}
            cands = atk_c
            cands += self._play_cands(st, kred, me, foes, ehq)
            cands += self._move_cands(st, me, foes, ehq, kred, attackers)
            cands = [c for c in cands if c[3]["card"] not in ex
                     and c[2] not in self._bad and c[2] not in self._tried]
            cands.sort(key=lambda c: -c[0])
            n_cands = max(n_cands, len(cands))
            self.probe = {"kred": kred, "n": len(cands),
                          "hand": [(h.name, getattr(h, "kredit_cost", None),
                                    self._act_cache.get(("playable", h.card_id, self._epoch)))
                                   for h in st.hand(LOCAL)],
                          "top": [(round(c[0], 2), c[3]["note"]) for c in cands[:6]]}
            if not cands:
                break
            for ratio, v, key, act, cost in cands:
                if asked >= self.P["gate_budget"]:
                    break
                asked += 1
                if self._confirm(act, st):
                    self._tried.add(key)
                    act["score"] = round(v, 3)
                    return act
                self._bad.add(key)
            if asked >= self.P["gate_budget"]:
                break
        return {"kind": "end", "note": "规则2：没有可做的正收益动作（候选 %d，问闸门 %d）"
                % (n_cands, asked)}

    # ------------------------------------------------------------ 其它相位
    def _best_target_for(self, st, instigator_card, plan: Plan):
        me, foes, ehq = self._split(st)
        if instigator_card is None:
            return None
        cs = self._target_cands(st, instigator_card, plan, me, foes, ehq, limit=1)
        return cs[0][1] if cs else None

    def _pick_gate(self, cands: list) -> bool:
        """同一批候选别连发：刚发过 <3s 内不再发（面板还在换页）；同一批最多发 2 次
        （再发就是对着陈旧面板点，会撞上"别重复点陈旧指针"的红线）。"""
        import time as _time
        sig = (frozenset(str(r.get("label") or r.get("name")) for r in cands),
               frozenset(r.get("trigger_id") for r in cands))
        now = _time.time()
        last = self._pick_seen.get(sig)
        if last is not None:
            n, t = last
            if now - t < 3.0 or n >= 2:
                return False
            self._pick_seen[sig] = (n + 1, now)
        else:
            self._pick_seen[sig] = (1, now)
        return True

    def _vm_choice_outcomes(self, trig):
        """触发卡的**抉择分支**（VM 枚举）：返回 `[eff0, eff1, ...]` 或 None。

        用户 2026-10-02："取不到效果摘要 是不可能的。本会话先前还查过这个问题。"
        —— 对的：实机只读复跑 `card_unit_5th_rangers` 的 `enumerate_effects` 得到
        `choice=True, outcomes=[{"opcost": 0}, {"buff": [4, 4]}]`（顺序 = 蓝图分支顺序，
        与屏幕 `chooseOneIndex` 一一对应）；6K 骄阳/天气牌则是 `complete=True、效果 {}`
        （当前场面没有空军/没有目标 ⇒ **已知的空**，不是缺口）。
        """
        if trig is None or not self.P.get("use_vm", True):
            return None
        ptr = (getattr(trig, "raw", None) or {}).get("ptr")
        km = self._km()
        if not km or not ptr:
            return None
        try:
            st = self._st
            r = EV.enumerate_effects(
                km, ptr, 0, False, hook=None,
                my_side=getattr(st, "my_side_raw", None),
                read_hooks=EV.make_read_hooks(st, getattr(st, "my_side_raw", None)),
                slots=dict(getattr(st, "slots", None) or {}),
                rng_seed=self._rng_seed(), budget_s=2.0)
        except Exception:                                        # noqa: BLE001
            return None
        if not r or not r.get("choice"):
            return None
        outs = [dict(e) for _w, e in (r.get("outcomes") or [])]
        return outs or None

    def _eff_on_unit_value(self, e, trig, st):
        """把一个效果摘要折算成"打在这个（触发）单位身上"的价值 —— 与 `boardeval` 同一把尺。
        认不出的形状返回 None（调用方退回文字估值，**不猜**）。"""
        if not isinstance(e, dict):
            return None
        v, hit = 0.0, False
        b = e.get("buff")
        if isinstance(b, (list, tuple)) and len(b) == 2:
            v += BE.W["w_atk"] * float(b[0] or 0) + BE.W["w_def"] * float(b[1] or 0)
            if trig is not None and self._actionable(st, trig):
                v += BE.W["act_w"] * max(float(b[0] or 0), 0.0)   # 本回合能打：+攻马上有用
            hit = True
        if "opcost" in e:
            op = _n(getattr(trig, "operation_cost", 0)) if trig is not None else 0.0
            v += min(op, 4.0) * 0.6                                # 行动费归零 ≈ 省下的行动点
            hit = True
        for k in ("give", "armor", "heal", "damage", "damage_hq", "destroy"):
            if e.get(k):
                return None                                        # 这些形状先不折算
        return v if hit else None

    def _option_score(self, row: dict, st):
        """二选一候选的估值：**先用游戏自己的效果**（VM 枚举的抉择分支，按 index 对应），
        失败才退回选项文字；返回 `(value, src)`，都不认 ⇒ `(None, None)`。"""
        trig = None
        if st is not None and row.get("trigger_id") is not None:
            trig = next((c for c in st.cards if c.card_id == row.get("trigger_id")), None)
        effs = self._vm_choice_outcomes(trig)
        idx = row.get("index")
        if effs and idx is not None and 0 <= int(idx) < len(effs):
            v = self._eff_on_unit_value(effs[int(idx)], trig, st)
            if v is not None:
                return v, "vm"
        tv = self._text_option_score(row, st)
        return (tv, "label") if tv is not None else (None, None)

    def _text_option_score(self, row: dict, st):
        """**文本型二选一**（部署抉择那类，候选不是卡）→ 对触发单位的盘面估值（**兜底**）。

        用户 2026-10-02："游骑兵，脚本还是不选+4+4"——5th RANGERS 的部署抉择
        （"Set operation cost to 0." / "… +4+4"）两个候选都不是卡对象：
        `score_candidates()` 查不到 → 旧启发式全是 0 分 → 永远点第一个（行动花费 0）。
        首选走 `_option_score()` 的 VM 分支（实机已能拿到 `[{"opcost":0},{"buff":[4,4]}]`），
        这里只是 VM 不可用时的**文字兜底**。
        这里按**选项文字**给分（和 `boardeval` 同一把尺）：
          · "+4+4" ⇒ 给触发单位 +4/+4 的身体价值（本回合还能动就再加攻击力的行动价值）；
          · "operation cost … 0" ⇒ 省下的行动费（小）。
        认不出就返回 None（调用方保留旧启发式，**不猜**）。
        """
        lab = (row.get("label") or row.get("name") or "").strip()
        if not lab:
            return None
        low = lab.lower().replace(" ", "")
        trig = None
        if st is not None and row.get("trigger_id") is not None:
            trig = next((c for c in st.cards if c.card_id == row.get("trigger_id")), None)
        if "+4+4" in low:
            v = (BE.W["w_atk"] + BE.W["w_def"]) * 4.0
            if trig is not None and self._actionable(st, trig):
                v += BE.W["act_w"] * 4.0          # 本回合还能动：+4 攻能马上用来打
            return v
        if "operationcost" in low and "0" in low:
            op = _n(getattr(trig, "operation_cost", 0)) if trig is not None else 0.0
            return min(op, 4.0) * 0.6             # 行动费归零 ≈ 省下的行动点
        return None

    def choose_pick(self, cands: list, st=None) -> dict:
        if cands and not self._pick_gate(cands):
            return None
        # ★ 行动 = 动作 + 其后的选择（总纲 §6）：搜索时带着路径打出的牌，弹出提示时先 `policy.answer` 照计划点；落空才往下重算。
        from policy import answer as PA
        plan = self.__dict__.get("_plan") or {}
        r_hit = PA.plan_choose_one(cands, plan)
        if r_hit is not None:
            return self._pick_from_plan(r_hit, plan[r_hit.get("trigger_id")], st)
        cs_hit = PA.plan_choose_spawn(cands, plan, fc_norm)
        if cs_hit is not None:
            row_cs, path_cs = cs_hit
            for k_ in [k_ for k_, v_ in plan.items() if tuple(v_) == tuple(path_cs)]:
                plan.pop(k_, None)                                           # 计划用掉
            a3 = self._pick_from_plan(row_cs, list(path_cs), st)
            a3["meta"]["pick_eval_src"] = "plan_spawn"
            return a3
        fc_hit = PA.plan_forecast(cands, plan, FC_TYPES, fc_norm,
                                  lambda w, t: ((self._fc_paths() or {}).get(w) or {}).get(t))
        if fc_hit is not None:
            row_fc, path_fc = fc_hit
            if not ({fc_norm(r.get("name") or r.get("label")) for r in cands} <= set(FC_TYPES)):
                for k_ in [k_ for k_, v_ in plan.items() if tuple(v_) == tuple(path_fc)]:
                    plan.pop(k_, None)                                       # 第二层命中 ⇒ 计划用掉
            a2 = self._pick_from_plan(row_fc, list(path_fc), st)
            a2["meta"]["pick_eval_src"] = "plan_fc"
            return a2
        best, best_s = None, -1e9
        lbl_scores = []          # 二选一（不是卡）的估值，写进 meta.pick_eval 用
        opt_src = None
        for i, r in enumerate(cands):
            atk, dfn = _n(r.get("attack")), _n(r.get("defense"))
            cost = _n(r.get("kredit_cost"))
            s = self.P["w_cost"] * cost + self.P["w_stat"] * (atk + dfn)
            # ★ 2026-10-02（用户："游骑兵，脚本还是不选+4+4"）：部署抉择的候选**不是卡**、
            #   查不进 boardeval（label 是选项文字），旧启发式对它们全是 0 分 ⇒ 永远点第一个
            #   （5th RANGERS 永远选到"行动花费为 0"）。估值来源：先 VM 枚举出的**抉择分支**
            #   （`[{"opcost":0},{"buff":[4,4]}]`，按 index 对应），VM 不可用才退回选项文字。
            tv, tsrc = self._option_score(r, st or self._st)
            if tv is not None:
                lbl_scores.append(((r.get("label") or r.get("name") or ""), tv))
                s = tv
                if tsrc:
                    opt_src = tsrc
            if r.get("needs_target") and st is not None:
                trig = next((c for c in st.cards if c.card_id == r.get("trigger_id")), None)
                if self._best_target_for(st, trig, self.plan(trig) if trig else Plan(None)) is None:
                    s -= 5.0                                      # 选了没法指向：沉底
            if s > best_s:
                best, best_s = r, s
        row = best or (cands[0] if cands else {})
        meta = {"kind": row.get("kind"), "trigger_id": row.get("trigger_id")}
        if lbl_scores:
            meta["pick_eval"] = [{"name": n, "score": round(v, 3)} for n, v in lbl_scores]
            meta["pick_eval_src"] = opt_src or "label"
        # —— 候选评估（洗牌型 + 预报第二层通用）：把候选当**临时手牌**塞进 BE 模拟，
        #    按当前场面 delta 选最优；评估跑不出来就保留上面的旧启发式（安全网）。
        #    注意：**9 条路径**那种前瞻只属于预报第一层（用户 2026-10-02）。
        try:
            cand_names = [fc_norm(r.get("name") or r.get("label")) for r in cands]
            is_l1 = bool(cand_names) and set(cand_names) <= set(FC_TYPES)
            # ★ 用时必须记录（用户 2026-10-02）：这几段是每步决策里的真实开销，
            #   直接决定单回合能不能压到 ≤35 s。写进 meta.pick_timing，随动作流落盘。
            tm = {"total_s": 0.0}
            _t0 = time.perf_counter()
            if is_l1:
                # —— 预报第一层：9 条路径前瞻（每种天气的**三个变体**分别估值，取该路径最优）——
                _t1 = time.perf_counter()
                paths0 = self._fc_paths()
                tm["fc_paths_s"] = round(time.perf_counter() - _t1, 3)
                _t1 = time.perf_counter()
                ev = {}
                self._cand_dbg = {}
                for w in ("sunny", "rain", "storm"):
                    row0 = (paths0 or {}).get(w) or {}
                    vs = [row0.get(t) for t in ("light", "medium", "heavy") if row0.get(t)]
                    sc0 = self.score_candidates(st, vs) or []
                    if sc0:
                        bname, bval = max(sc0, key=lambda x: x[1])
                        ev[w] = {"scores": {n: round(v, 3) for n, v in sc0},
                                 "best": bname, "value": round(bval, 3)}
                tm["forecast_eval_s"] = round(time.perf_counter() - _t1, 3)
                bw = fc_best_type(ev)
                if ev:
                    meta["forecast_eval"] = ev
                    meta["forecast_eval_dbg"] = {k: v for k, v in (getattr(self, "_cand_dbg", None) or {}).items()}
                gp = {n: self.gaps[n] for n in
                      [x for w in ("sunny", "rain", "storm")
                       for x in ((paths0 or {}).get(w) or {}).values()
                       if isinstance(x, str)] if n in self.gaps}
                if gp:
                    # 如实标出"这次评估里哪些卡的效果没取到"（估成常量兜底 ⇒ 同费同分）
                    meta["forecast_eval_gaps"] = gp
                if bw:
                    pick = next((r for r in cands
                                 if FC_TYPES.get(fc_norm(r.get("name") or r.get("label"))) == bw),
                                None)
                    if pick is not None:
                        row, best_s = pick, max(best_s, ev[bw]["value"])
                        meta["kind"] = row.get("kind")
                        meta["trigger_id"] = row.get("trigger_id")
                        meta["pick_eval_src"] = "forecast_9path"
            else:
                _t1 = time.perf_counter()
                sc = self.score_candidates(st, [n for n in cand_names if n])
                tm["pick_eval_s"] = round(time.perf_counter() - _t1, 3)
                if sc:
                    meta["pick_eval"] = [{"name": n, "score": round(v, 3)} for n, v in sc]
                    bn, bv = max(sc, key=lambda x: x[1])
                    pick = next((r for r in cands
                                 if fc_norm(r.get("name") or r.get("label")) == bn), None)
                    if pick is not None:
                        row, best_s = pick, max(best_s, bv)
                        meta["kind"] = row.get("kind")
                        meta["trigger_id"] = row.get("trigger_id")
                        meta["pick_eval_src"] = "boardeval"
            tm["total_s"] = round(time.perf_counter() - _t0, 3)
            meta["pick_timing"] = tm
        except Exception:                                          # noqa: BLE001
            pass
        # —— 预报留痕：第一层（蓝天/薄雾/狂风）算 9 条路径；第二层（天气卡）核对预测 ——
        #    （只写进动作流，不改选择；评估接进来之前先攒数据，见 TODO §3d）
        try:
            names = [r.get("name") or r.get("label") for r in cands]
            ns = {fc_norm(n) for n in names if n}
            paths = self._fc_paths() if (ns and ns <= set(FC_TYPES)) else None
            add, self._fc_pending = fc_plan(names, paths,
                                            getattr(self, "_fc_pending", None), time.time())
            meta.update(add)
            # —— 洗牌型三选一（好人寥寥那类）：**回退 N 步复算**，与屏幕三张逐张核对 ——
            #    （只留痕，不改选择；9 选 1 评估仅限预报 —— 用户 2026-10-02 明确）
            cs = self._cs_pool()
            if cs and len(ns) >= 2 and ns <= set(cs):
                sd = self._rng_seed()
                if sd:
                    _t2 = time.perf_counter()
                    meta["shuffle_pick_check"] = check_shuffle_pick(
                        cs, sd, [fc_norm(x) for x in names if x])
                    meta.setdefault("pick_timing", {})["shuffle_check_s"] = \
                        round(time.perf_counter() - _t2, 3)
        except Exception:                                          # noqa: BLE001
            pass
        act = {"kind": "pick", "index": row.get("index"), "score": round(best_s, 3),
               "meta": meta, "note": "规则2：三选一/二选一 → %s" % (row.get("label") or row.get("name"))}
        if row.get("needs_target") and st is not None:
            trig = next((c for c in st.cards if c.card_id == row.get("trigger_id")), None)
            tg = self._best_target_for(st, trig, self.plan(trig)) if trig is not None else None
            if tg is not None:
                act["target"] = tg.card_id
        return act

    def _pick_from_plan(self, row: dict, path, st=None) -> dict:
        """搜索时已经定好的选择路径 ⇒ 直接点那一项（不再用启发式/VM 重算一遍）。"""
        act = {"kind": "pick", "index": row.get("index"), "score": 0.0,
               "meta": {"kind": row.get("kind"), "trigger_id": row.get("trigger_id"),
                        "pick_eval_src": "plan", "plan": list(path)},
               "note": "规则2：按搜索路径 %s → %s" % (list(path), row.get("label") or row.get("name"))}
        if row.get("needs_target") and st is not None:
            trig = next((c for c in st.cards if c.card_id == row.get("trigger_id")), None)
            tg = self._best_target_for(st, trig, self.plan(trig)) if trig is not None else None
            if tg is not None:
                act["target"] = tg.card_id
        return act

    def _spawn_stat(self, name):
        """生成单位（SpawnCardOnBattlefield/InFrontline）的面板——读静态卡表（和三选一候选同一来源）；读不到 ⇒ None（sim 记缺口、不编）。"""
        km, idx = self._km(), self._static_index()
        p_ = idx.get(str(name).split("_C_")[0]) if idx else None
        if km is None or not p_:
            return None
        typ = self._ST_TYP.get(km.m.u8(p_ + self._ST_TYPE))
        if typ is None:
            return None
        return {"atk": km.m.i32(p_ + self._ST_ATK) or 0, "dfn": km.m.i32(p_ + self._ST_DFN) or 0,
                "cost": km.m.i32(p_ + self._ST_COST) or 0, "typ": typ, "kw": ()}

    def _spawn_cands(self, st, card_id) -> list:
        """某张手牌的"三选一加入手牌"候选 `[{"name","atk","dfn","cost","typ"}]`（`semantics/choosespawn.py`：跑这张牌自己的
        `GetChooseSpawnCards` + 活种子洗牌，随机已被算成确定结果）。读不出 ⇒ []，记缺口（不编候选）。按 (卡, 种子) 缓存。"""
        card = next((c for c in st.cards if c.card_id == card_id), None)
        ptr = (getattr(card, "raw", None) or {}).get("ptr") if card is not None else None
        km, seed = self._km(), self._rng_seed()
        if not (ptr and km is not None and seed is not None):
            return []
        key = ("spawn", card_id, seed)
        hit = self._eff_cache.get(key)
        if hit is not None:
            return hit
        out = []
        try:
            from semantics import choosespawn as CS
            r = CS.predict(km, ptr, seed, st)
            if r.get("stopped"):
                self.gaps[card.name + "#choose_spawn"] = "三选一候选预测失败：%s" % r["stopped"]
            idx = self._static_index()
            for nm in r.get("names") or []:
                p_ = idx.get(nm)
                if not p_:
                    continue
                out.append({"name": nm, "atk": km.m.i32(p_ + self._ST_ATK) or 0, "dfn": km.m.i32(p_ + self._ST_DFN) or 0,
                            "cost": km.m.i32(p_ + self._ST_COST) or 0,
                            "typ": self._ST_TYP.get(km.m.u8(p_ + self._ST_TYPE)) or "order"})
        except Exception as ex:                                   # noqa: BLE001
            self.gaps[card.name + "#choose_spawn"] = "三选一候选预测出错：%s: %s" % (type(ex).__name__, ex)
        self._eff_cache[key] = out
        return out

    def _hand_target_puts_back(self, st, pending, cand) -> bool:
        """这次"选手牌"提示是不是"放回牌库顶"一类？——对**提示的发起牌**跑 `OnHandTargetSelected`
        （选手牌 = `cand`），效果里有 `to_deck`（`MoveCardToTopOfOwnersDeck`）就是。取不到/跑不了 ⇒ False（保持旧行为）。
        例：PBY CATALINA（回合开始抽 2、放回 1 张）、175th INFANTRY REGIMENT。"""
        try:
            ht = (pending or {}).get("hand_target") or {}
            inst_id = ht.get("card_being_played")
            inst = next((c for c in st.cards if c.card_id == inst_id), None)
            ptr = (getattr(inst, "raw", None) or {}).get("ptr") if inst is not None else None
            km = self._km()
            if not (ptr and km and self.P.get("use_vm", True)):
                return False
            r = EV.record_effects(km, ptr, 0, False, hook="OnHandTargetSelected",
                                  my_side=getattr(st, "my_side_raw", None),
                                  args={"handTargetCardID": cand.card_id, "instigatorID": inst_id},
                                  timeout_s=1.5)
            return bool((r or {}).get("eff", {}).get("to_deck"))
        except Exception:                                         # noqa: BLE001
            return False

    def _hand_target_fx_for(self, st, inst_id, cands) -> dict:
        """手牌目标提示的候选表 `{候选手牌 id: 该候选被选中时的效果摘要}`：对**发起牌**逐个候选空跑 `OnHandTargetSelected`
        （VM，游戏自己的字节码）。取不到/空 ⇒ 该候选不进表。"""
        out = {}
        km = self._km()
        inst = next((c for c in st.cards if c.card_id == inst_id), None)
        ptr = (getattr(inst, "raw", None) or {}).get("ptr") if inst is not None else None
        if not (km and ptr and self.P.get("use_vm", True)):
            return out
        for x in cands:
            try:
                r = EV.record_effects(km, ptr, 0, False, hook="OnHandTargetSelected",
                                      my_side=getattr(st, "my_side_raw", None),
                                      args={"handTargetCardID": x.card_id, "instigatorID": inst_id},
                                      timeout_s=1.5, rng_seed=self._rng_seed())
            except Exception:                                     # noqa: BLE001
                continue
            eff = dict((r or {}).get("eff") or {})
            eff.pop("uncertain", None)
            if eff:
                out[x.card_id] = eff
        return out

    def choose_hand_target(self, st, pending=None):
        legal = [c for c in st.hand(LOCAL)
                 if self.sess.hand_target_legal(c).get("can") is not False]
        if not legal:
            return None
        # ★ M1/M3（总纲 §6）：强制决策与"我们的动作引发的提示"是**同一套**——候选表（VM 逐候选空跑 `OnHandTargetSelected`）→
        #   `sim.engine.hand_target_suspended` 挂起 → 逐候选 `resume` → 评估取最好（`policy.forced`）。
        #   旧启发式（放回类 = 最用不上的；其它 = 最值钱的）只在建不出候选表/模拟时兜底。
        inst_id = ((pending or {}).get("hand_target") or {}).get("card_being_played")
        from policy import answer as PA
        cid_plan = PA.plan_hand_target([c.card_id for c in legal], self.__dict__.get("_plan") or {}, inst_id)
        if cid_plan is not None:                                   # 搜索时已定好的路径（我们的动作引发的提示）
            c = next(c for c in legal if c.card_id == cid_plan)
            return {"kind": "hand_target", "card": c.card_id, "note": "规则2：手牌目标（按搜索路径）%s" % c.name,
                    "meta": {"hand_target_mode": "plan", "plan": [cid_plan]}}
        try:
            fx = self._hand_target_fx_for(st, inst_id, legal) if inst_id is not None else {}
            if fx:
                from policy import forced as PF
                kred = int((st.kredits or {}).get(LOCAL) or 0)
                _me, _foes, ehq = self._split(st)
                sim = self._search_sim(st, kred, ehq)
                sim.hand_target_fx = {inst_id: fx}
                ans = PF.answer_hand_target(sim, inst_id, self._W)
                if ans:
                    cid, scored = ans
                    name = next((c.name for c in legal if c.card_id == cid), cid)
                    return {"kind": "hand_target", "card": cid, "note": "规则2：手牌目标（模拟评估）%s" % name,
                            "meta": {"hand_target_mode": "sim",
                                     "scores": [(round(v, 3), k) for v, k in scored]}}
        except Exception:                                         # noqa: BLE001
            pass
        if self._hand_target_puts_back(st, pending, legal[0]):
            kred = int((st.kredits or {}).get(LOCAL) or 0)
            c = min(legal, key=lambda x: (0 if _n(getattr(x, "kredit_cost", 0)) > kred else 1, self.worth(x)))
            return {"kind": "hand_target", "card": c.card_id,
                    "note": "规则2：手牌目标（放回牌库顶）%s" % c.name, "meta": {"hand_target_mode": "put_back"}}
        c = max(legal, key=self.worth)
        return {"kind": "hand_target", "card": c.card_id,
                "note": "规则2：手牌目标 %s" % c.name}

    def choose_board_target(self, st, instigator: int):
        trig = next((c for c in st.cards if c.card_id == instigator), None)
        tg = self._best_target_for(st, trig, self.plan(trig)) if trig is not None else None
        if tg is None:
            return super().choose_board_target(st, instigator)
        return {"kind": "board_target", "card": instigator, "target": tg.card_id,
                "note": "规则2：待点目标 → %s" % tg.name}

    def decide(self, st, phase: str, pend=None, exclude_cards=None):
        turn = getattr(st, "turn", None)
        if turn != self._turn:
            self._turn = turn
            self._turn_t0 = time.time()      # ★ 我方回合硬预算的起点（见 PARAMS["turn_budget_s"]）
            self._tried.clear()
            self._bad.clear()
            self.__dict__["_plan"] = __import__("policy.plan", fromlist=["PlanStore"]).PlanStore()
            self._act_cache.clear()
            self._eff_cache.clear()
            self._pick_seen.clear()
            self._last_n = 0
            self._epoch += 1
        if len(self._tried) != self._last_n:
            # 上一步发出了动作 → 局面变了：游戏之前说"不行"的、以及单位能不能动，都要重问
            self._last_n = len(self._tried)
            self._bad.clear()
            self._act_cache.clear()
            self._epoch += 1
        if phase == "main":
            act = self.choose_main(st, exclude_cards)
            # 把"本回合已用多久"记进动作（报告/复盘直接能看）
            try:
                t0 = getattr(self, "_turn_t0", None)
                if isinstance(act, dict) and t0:
                    act.setdefault("meta", {})["turn_elapsed_s"] = round(time.time() - t0, 1)
            except Exception:                                        # noqa: BLE001
                pass
            return act
        if phase == "pick":
            rows = (pend or {}).get("choose_one") or []
            return self.choose_pick(rows, st) if rows else None
        if phase == "hand_target":
            return self.choose_hand_target(st, pend)
        if phase == "board_target":
            bt = (pend or {}).get("board_target")
            return self.choose_board_target(st, bt) if isinstance(bt, int) else None
        if phase == "mulligan":
            marks = self.sess.mulligan_marks()
            return self.choose_mulligan(marks) if marks else None
        return None


# ---------------------------------------------------------------------------
# 离线自检（不碰游戏）
# ---------------------------------------------------------------------------
class _C:
    def __init__(self, side, loc, cid, name="X", atk=0, dfn=0, cost=0, typ=None,
                 guarded=False, enter=None, kw=None):
        self.side, self.location, self.card_id, self.name = side, loc, cid, name
        self.attack, self.defense, self.kredit_cost = atk, dfn, cost
        self.card_type, self.is_being_guarded = typ, guarded
        self.enter_play_on_turn, self.fname = enter, None
        self.operation_cost, self.can_act, self.is_suppressed = 1, None, False
        self.is_revealed, self.keywords, self.raw, self.uid = True, kw or [], {}, "0x%X" % cid
        self.needs_hand_target = False
        self.faction_enum = None


class _S:
    def __init__(self, cards, kredits=5, turn=6, fl=None):
        self.cards, self.turn, self.frontline_owner = cards, turn, fl
        self.kredits = {"local": kredits, "enemy": 3}

    def hand(self, side):
        return [c for c in self.cards if c.side == side and c.location == "hand"]


class _Sess:
    def __init__(self, deny=(), kred=99):
        self.deny, self.asked, self.kred = set(deny), [], kred

    def can_attack(self, a, t):
        self.asked.append(("atk", a.card_id, t.card_id))
        return {"can": (a.card_id, t.card_id) not in self.deny}

    def can_play(self, c, target=None):
        if (c.kredit_cost or 0) > self.kred:          # 假游戏：指挥点不够就说不行
            return {"can": False}
        return {"can": (c.card_id, getattr(target, "card_id", None)) not in self.deny}

    def can_move(self, u):
        return {"can": True}

    def hand_target_legal(self, c):
        return {"can": True}

    def mulligan_marks(self):
        return []


def selftest() -> int:
    from learn.features import CardTable
    table = CardTable.load()          # 只给换牌口径用（isKreditsBuff），不参与出牌语义
    fails = 0

    def chk(name, ok, extra=""):
        nonlocal fails
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        fails += 0 if ok else 1

    def run(cards, kred=5, deny=(), fl=None, turn=6):
        # ★ 自检必须**与真实游戏隔离**：`_default_act` 会问 precheck→游戏，而假卡的 card_id
        #   在真进程里不存在 ⇒ 会被判"不能动"（实机在跑 + KARDS_BUILD 匹配时必现）。
        #   selftest 的语义是"假设游戏说能动"，这里显式钉住；要测 act_fn 的用例自己传。
        pol = RuleV2(_Sess(deny, kred), table=table, act_fn=lambda u: True)
        return pol, pol.decide(_S(cards, kred, turn, fl), "main")

    # 1 致命：两个单位合力能打死总部，先出手
    pol, a = run([_C(LOCAL, "frontline", 1, "A", 3, 3, 3, "infantry"),
                  _C(LOCAL, "frontline", 2, "B", 3, 3, 3, "infantry"),
                  _C(ENEMY, "hq", 9, "HQ", 0, 6, 0)])
    chk("致命一击优先", a["kind"] == "attack" and a["target"] == 9, a.get("note", ""))

    # 2 被守护的总部不打
    pol, a = run([_C(LOCAL, "frontline", 1, "A", 5, 3, 3, "infantry"),
                  _C(ENEMY, "hq", 9, "HQ", 0, 20, 0, guarded=True)])
    chk("被守护的目标不打", a["kind"] == "end", a.get("note", ""))

    # 3 炮兵免反击：4/1 炮兵打 3/3 敌兵不死不亏，应该出手；同样数值的步兵会阵亡→不打
    art = [_C(LOCAL, "back", 1, "ART", 3, 1, 3, "artillery"),
           _C(ENEMY, "frontline", 5, "E", 3, 3, 2, "infantry")]
    pol, a = run(art)
    chk("炮兵不吃反击 → 敢打", a["kind"] == "attack", a.get("note", ""))
    inf = [_C(LOCAL, "back", 1, "INF", 3, 3, 3, "infantry"),      # 3/3 换 3/3：同归于尽，不赚
           _C(ENEMY, "frontline", 5, "E", 3, 3, 2, "infantry")]
    pol, a = run(inf)
    cs_ = pol._attack_cands(_S(inf, 5, 6), *pol._split(_S(inf, 5, 6)), 5)
    chk("同归于尽的平换 → 收益很小（只剩清掉对方前线威胁那一点）",
        all(c[1] < 1.0 for c in cs_), str([round(c[1], 2) for c in cs_]))

    # 4 同一动作每回合只试一次；被闸门拒的换下一个
    cs = [_C(LOCAL, "back", 1, "ART", 5, 2, 3, "artillery"),
          _C(ENEMY, "frontline", 5, "E1", 2, 2, 2, "infantry"),
          _C(ENEMY, "frontline", 6, "E2", 2, 2, 2, "infantry")]
    pol = RuleV2(_Sess(deny=[(1, 5), (1, 6)]), table=table)
    a = pol.decide(_S(cs), "main")
    chk("闸门全拒 → 结束（不死循环）", a["kind"] == "end", a.get("note", ""))
    pol = RuleV2(_Sess(), table=table, act_fn=lambda u: True)   # 同上：与真机隔离
    st = _S(cs)
    a1 = pol.decide(st, "main")
    a2 = pol.decide(st, "main")
    chk("同一动作不重复发", (a1.get("card"), a1.get("target")) != (a2.get("card"), a2.get("target")),
        "%s | %s" % (a1.get("note"), a2.get("note")))

    # 5 单位能不能动：问游戏（act_fn），不看闪击/进场回合
    cs5 = [_C(LOCAL, "back", 1, "NEW", 5, 5, 3, "artillery"),
           _C(ENEMY, "frontline", 5, "E", 1, 1, 1, "infantry")]
    pol = RuleV2(_Sess(), table=table, act_fn=lambda u: False)
    a = pol.decide(_S(cs5), "main")
    chk("游戏说不能动 → 不打", a["kind"] != "attack", a.get("note", ""))
    pol = RuleV2(_Sess(), table=table, act_fn=lambda u: True)
    a = pol.decide(_S(cs5), "main")
    chk("游戏说能动 → 打（哪怕刚部署/无闪击）", a["kind"] == "attack", a.get("note", ""))

    # 6 出牌：有钱时部署单位，付不起的不出
    pol, a = run([_C(LOCAL, "hand", 20, "UNIT2", 2, 3, 2, "infantry"),
                  _C(LOCAL, "hand", 21, "UNIT9", 9, 9, 9, "infantry")], kred=3)
    chk("部署付得起的", a["kind"] == "play_unit" and a["card"] == 20, a.get("note", ""))

    # 7 先打后花钱：1 点指挥点时，好交换胜过部署（收益÷花费）
    pol, a = run([_C(LOCAL, "back", 1, "ART", 4, 2, 3, "artillery"),
                  _C(ENEMY, "frontline", 5, "E", 2, 2, 3, "infantry"),
                  _C(LOCAL, "hand", 20, "UNIT1", 1, 1, 1, "infantry")], kred=1)
    chk("有利交换优先于平价部署", a["kind"] == "attack", a.get("note", ""))

    # 8 指令：入口是运行时旗标 needs_hand_target；侧倾向只影响排序；合法性问游戏
    from semantics import cardprobe
    dmg = cardprobe.summarize({"ok": True, "verbs": {"GetTargetedCard", "DamageCard"}, "damage": 4})
    buff = cardprobe.summarize({"ok": True, "verbs": {"GiveBlitz", "FullyHealCard"}})
    unk = cardprobe.summarize({"ok": True, "verbs": {"ChangeAttack"}})
    chk("倾向：伤害类偏敌方", dmg["side"] == "enemy" and dmg["damage"] == 4)
    chk("倾向：增益类偏我方", buff["side"] == "friend")
    chk("倾向：两可动词 → 不下结论", unk["side"] is None)

    def with_probe(summ, cards, deny=(), kred=5):
        pol = RuleV2(_Sess(deny, kred), table=table, probe_fn=lambda c: summ,
                     act_fn=lambda u: True)
        return pol, pol.decide(_S(cards, kred), "main")

    board = [_C(LOCAL, "back", 1, "MINE", 0, 2, 2, "infantry"),   # 攻击力 0：排除顺手攻击
             _C(ENEMY, "frontline", 5, "BIG", 5, 4, 5, "tank"),
             _C(ENEMY, "frontline", 6, "SMALL", 1, 1, 1, "infantry")]
    order = _C(LOCAL, "hand", 30, "SHELL", 0, 0, 3, "order")
    order.needs_hand_target = True                                # 运行时旗标
    pol, a = with_probe(dmg, board + [order])
    chk("伤害指令 → 打敌方最值钱且能打死的", a.get("kind") == "play_event_target" and a["target"] == 5,
        a.get("note", ""))
    pol, a = with_probe(dmg, board + [order], deny=[(30, 5)])
    chk("游戏说不行 → 换下一个目标", a.get("kind") == "play_event_target" and a["target"] != 5,
        a.get("note", ""))
    # ★ 2026-10-02：用**有量的**增益摘要（cardprobe 的 buff 只有倾向、没有数值 ⇒ 现在会被
    #   当成"效果摘要缺失"而不打，这正是"不空打"策略；这条测的是"偏我方单位"的排序）
    buff_eff = {"known": True, "side": "friend",
                "effects": {"buff": [2, 2], "target": "friend"}}
    pol, a = with_probe(buff_eff, board + [order])
    chk("增益指令 → 偏我方单位", a.get("kind") == "play_event_target" and a["target"] == 1,
        a.get("note", ""))
    noflag = _C(LOCAL, "hand", 31, "PLAIN", 0, 0, 2, "order")     # 没旗标：不带目标出
    pol, a = with_probe({"known": True, "effects": {"draw": 1}}, board + [noflag])
    chk("没有运行时旗标 → 直接出，不硬凑目标", a.get("kind") == "play_event", a.get("note", ""))
    pol, a = with_probe(dmg, board + [noflag])                    # 目标类效果 + 没旗标
    chk("没旗标 + 目标类效果 ⇒ 不硬凑目标（宁可不打）",
        a.get("kind") != "play_event_target", a.get("note", ""))
    unit = _C(LOCAL, "hand", 32, "DEPLOYER", 2, 2, 2, "infantry")
    unit.needs_hand_target = True
    pol, a = with_probe(dmg, board + [unit], deny=[(32, 5), (32, 6), (32, 1)])
    chk("部署效果目标全被拒 → 退回无目标部署", a.get("kind") == "play_unit", a.get("note", ""))

    # 9 pick：抉择需要指向、又没有合法目标时，沉底
    pol = RuleV2(_Sess(), table=table, probe_fn=lambda c: None)
    rows = [{"index": 0, "attack": 3, "defense": 3, "kredit_cost": 3, "kind": "choose_spawn"},
            {"index": 1, "attack": 1, "defense": 1, "kredit_cost": 1, "kind": "choose_spawn"}]
    a = pol.choose_pick(rows)
    chk("三选一 → 挑价值最高", a["kind"] == "pick" and a["index"] == 0, str(a.get("note")))
    # ---- 端到端：搜索路径 + 逐对目标 + 效果摘要（经 RuleV2.decide）----
    def eff_pol(effs, deny=(), kred_sess=99, act=lambda u: True):
        pol = RuleV2(_Sess(deny, kred_sess), table=table,
                     probe_fn=lambda c: {"known": True, "side": None, "effects": effs.get(c.name, {})},
                     act_fn=act, params={"use_vm": False})
        return pol

    # 贴闪击：本回合刚部署的炮兵（sick）+ 「给友方闪击」的指令 ⇒ 先贴再打
    sickart = _C(LOCAL, "back", 1, "NEWART", 4, 3, 3, "artillery", enter=6)
    giveb = _C(LOCAL, "hand", 30, "GIVEBLITZ", 0, 0, 1, "order")
    giveb.needs_hand_target = True
    foe = _C(ENEMY, "frontline", 5, "FOE", 2, 2, 2, "infantry")
    pol = eff_pol({"GIVEBLITZ": {"give": ["blitz"], "target": "friend"}},
                  act=lambda u: False)                     # 游戏：新部署的它现在不能动
    a = pol.decide(_S([sickart, giveb, foe], 5, 6), "main")
    chk("搜索·贴闪击：先给刚部署的炮兵贴闪击（逐对目标）",
        a.get("kind") == "play_event_target" and a.get("card") == 30 and a.get("target") == 1,
        a.get("note", ""))

    # 生产：差 1 点买不起 4 费 5/5 ⇒ 先打 0 费「+1 指挥点」
    prod = _C(LOCAL, "hand", 40, "PRODUCTION", 0, 0, 0, "order")
    big = _C(LOCAL, "hand", 41, "BIGUNIT", 5, 5, 4, "infantry")
    pol = eff_pol({"PRODUCTION": {"kredit": 1}})
    sess_k = pol.sess
    a = pol.decide(_S([prod, big], 3, 6), "main")
    # 假游戏的 can_play 按 kred_sess=99 全放行；这里只看搜索会不会先打生产
    chk("搜索·生产：预算差 1 点时先打生产", a.get("card") == 40, a.get("note", ""))

    # 逐对目标：游戏否掉一个，就在另一个上出手
    shell = _C(LOCAL, "hand", 50, "SHELL2", 0, 0, 2, "order")
    shell.needs_hand_target = True
    e1 = _C(ENEMY, "frontline", 5, "E5", 3, 3, 3, "infantry")
    e2 = _C(ENEMY, "frontline", 6, "E6", 3, 3, 3, "infantry")
    pol = eff_pol({"SHELL2": {"damage": 3, "target": "enemy"}}, deny=[(50, 5)])
    a = pol.decide(_S([shell, e1, e2], 5, 6), "main")
    chk("搜索·逐对目标：游戏否掉 5 号，改指 6 号", a.get("kind") == "play_event_target" and a.get("target") == 6,
        a.get("note", ""))

    # 覆盖缺口：效果摘要为空的指令 ⇒ 记入 gaps（不悄悄猜），且**不编造价值**
    # （用户 2026-10-02："指令的评估有问题。6费空打+3+2" —— 旧默认 order_mult*cost 给
    #  6 费牌 +5.4，扣掉手牌持有价值 2.16 仍是 +3.24 ⇒ 空打被当正收益）
    mystery = _C(LOCAL, "hand", 60, "MYSTERY", 0, 0, 2, "order")
    pol = eff_pol({})
    a = pol.decide(_S([mystery], 5, 6), "main")
    chk("覆盖缺口被记录", "MYSTERY" in pol.gaps, str(pol.gaps))
    chk("未知效果的指令不再被当成正收益（宁可不打）", a.get("kind") == "end", a.get("note", ""))
    mystery6 = _C(LOCAL, "hand", 61, "MYSTERY6", 0, 0, 6, "order")
    inf = _C(LOCAL, "back", 1, "INF", 3, 3, 2, "infantry")        # 后排步兵打敌方前线才合法
    e1 = _C(ENEMY, "frontline", 5, "E5", 1, 1, 2, "infantry")
    pol = eff_pol({})
    a = pol.decide(_S([mystery6, inf, e1], 10, 6), "main")
    chk("6 费未知指令不再压过能打的攻击（不空打）",
        a.get("kind") == "attack" and a.get("card") == 1, a.get("note", ""))

    # ★ 2026-10-02（TODO A6）：`效果vm` 对"这张牌**根本没覆写**打出/部署钩子"也回 stopped，
    #   但那不是缺口（纯身材单位本来就没打出效果）⇒ 记 `eff_src="无覆写"`、**不进 gaps**。
    import semantics.effectvm as _EV
    _orig = _EV.enumerate_effects

    def _fake_enum(*_a, **_k):
        return {"outcomes": [], "stopped": "这张牌没有 OnPlayedFromHand 覆写",
                "complete": False, "nodes": [], "runs": 1}

    plain = _C(LOCAL, "back", 1, "PLAIN UNIT", 3, 3, 2, "infantry")
    plain.raw = {"ptr": 0x1234}
    pol = eff_pol({})
    pol.P = dict(pol.P, use_vm=True)     # 离线：这一段要走到 VM 分支（eff_pol 默认把 VM 关了）
    pol._km = lambda: object()          # 离线：绕过"没进程 ⇒ 直接返回 None"的早退，走到 stopped 分支
    pol._st = _S([])
    _EV.enumerate_effects = _fake_enum
    try:
        got = pol._vm_eff(plain)
    finally:
        _EV.enumerate_effects = _orig
    chk("A6：'没有覆写打出钩子'不再记成缺口",
        pol.eff_src.get("PLAIN UNIT") == "无覆写" and "PLAIN UNIT" not in pol.gaps,
        "eff_src=%s gaps=%s" % (pol.eff_src, pol.gaps))

    def _fake_enum_gap(*_a, **_k):
        return {"outcomes": [], "stopped": "Unimplemented @0x12 LocalFinalFunction: '…'",
                "complete": False, "nodes": [], "runs": 1}

    hard = _C(LOCAL, "back", 2, "HARD CARD", 3, 3, 2, "infantry")
    hard.raw = {"ptr": 0x5678}
    pol2 = eff_pol({})
    pol2.P = dict(pol2.P, use_vm=True)
    pol2._km = lambda: object()
    pol2._st = _S([])
    _EV.enumerate_effects = _fake_enum_gap
    try:
        _vm_got = pol2._vm_eff(hard)
    finally:
        _EV.enumerate_effects = _orig
    chk("A6：真缺口（Unimplemented）照旧记进 gaps",
        "HARD CARD" in pol2.gaps and "Unimplemented" in pol2.gaps.get("HARD CARD", ""),
        str(pol2.gaps))

    # 部署抉择（5th RANGERS 那类）：候选是**文字**不是卡 ⇒ 按文字对触发单位估值；
    # 用户 2026-10-02："游骑兵，脚本还是不选+4+4"（旧逻辑永远点第一个 = 行动费归零）
    rgr = _C(LOCAL, "hand", 75, "5th RANGERS", 4, 4, 4, "infantry")
    rgr.operation_cost = 4
    rgr.can_act = True
    st_rgr = _S([rgr, _C(ENEMY, "frontline", 5, "E5", 1, 1, 2, "infantry")], 6, 6)
    rgr_cands = [
        {"kind": "choose_one", "index": 0, "label": "Set operation cost to 0.", "trigger_id": 75},
        {"kind": "choose_one", "index": 1, "label": "Give this unit +4+4.", "trigger_id": 75}]
    # 首选路径：VM 枚举出的**抉择分支**（实机 5th RANGERS = [{"opcost":0},{"buff":[4,4]}]）
    pol = eff_pol({})
    pol._vm_choice_outcomes = lambda trig: [{"opcost": 0}, {"buff": [4, 4]}]
    row = pol.choose_pick(list(rgr_cands), st_rgr)
    chk("部署抉择（VM 分支）：+4+4 压过 行动费归零", bool(row) and row.get("index") == 1,
        str(row and row.get("note")))
    chk("部署抉择（VM 分支）：pick_eval_src=vm",
        bool(row) and (row.get("meta") or {}).get("pick_eval_src") == "vm",
        str(row and row.get("meta")))
    # 兜底路径：VM 不可用 ⇒ 按选项文字（still 不能永远点第一个）
    pol = eff_pol({})
    pol._vm_choice_outcomes = lambda trig: None
    row = pol.choose_pick(list(rgr_cands), st_rgr)
    chk("部署抉择（文字兜底）：+4+4 压过 行动费归零", bool(row) and row.get("index") == 1,
        str(row and row.get("note")))
    chk("部署抉择（文字兜底）：估值来源/明细写进 meta",
        bool(row) and (row.get("meta") or {}).get("pick_eval_src") == "label"
        and len((row.get("meta") or {}).get("pick_eval") or []) == 2,
        str(row and row.get("meta")))

    # 协力（Bond）：打出时「回合开始时场上没有同国友方单位」⇒ 总部吃一次疲劳伤害
    bond_card = _C(LOCAL, "hand", 80, "BOND CARD", 0, 0, 2, "order")
    bond_card.faction_enum = 7
    bond_st = _S([bond_card, _C(LOCAL, "frontline", 1, "A", 2, 2, 2, "infantry"),
                  _C(ENEMY, "hq", 9, "HQ", 0, 20, 0)], 5, 4)
    pol = eff_pol({})
    pol._has_bond = lambda c: True
    pol._bond_factions = lambda st: set()            # 回合开始时场上没有同国单位
    sim_b = pol._search_sim(bond_st, 5, bond_st.cards[-1])
    chk("协力未满足 ⇒ 手牌效果带 bond_fatigue",
        sim_b.hand[80].eff.get("bond_fatigue") is True, str(sim_b.hand[80].eff))
    pol._bond_factions = lambda st: {7}              # 回合开始时有同国单位
    sim_b2 = pol._search_sim(bond_st, 5, bond_st.cards[-1])
    chk("协力已满足（faction 7 在集合里）⇒ 无 bond_fatigue",
        "bond_fatigue" not in sim_b2.hand[80].eff, str(sim_b2.hand[80].eff))
    pol2 = eff_pol({})                               # 不是 bond 牌（默认 _has_bond 读不到 = False）
    sim_b3 = pol2._search_sim(bond_st, 5, bond_st.cards[-1])
    chk("非协力牌 ⇒ 无 bond_fatigue", "bond_fatigue" not in sim_b3.hand[80].eff)

    # 情报触发（0x1C）：条件判据（cipher>0）；离线无进程 ⇒ 安全返回 []
    pol_i = eff_pol({})
    c_no = _C(LOCAL, "hand", 90, "INTEL0", 0, 0, 1, "order")
    c_no.cipher = 0
    chk("情报触发：cipher=0 ⇒ 不触发", pol_i._intel_triggers(_S([c_no]), c_no) == [])
    c_yes = _C(LOCAL, "hand", 91, "INTEL2", 0, 0, 1, "order")
    c_yes.cipher = 2
    c_yes.raw = {}                                   # 让 ptr=0 ⇒ 不进入 VM，安全返回 []
    chk("情报触发：离线（无 ptr/无进程）⇒ 安全返回 []",
        pol_i._intel_triggers(_S([c_yes]), c_yes) == [])
    chk("死亡触发：离线（无进程）⇒ 安全返回 {}",
        pol_i._death_fx(_S([c_no])) == {})
    return fails


if __name__ == "__main__":
    import sys
    print("player.rule 离线自检")
    n = selftest()
    print("失败 %d 项" % n)
    sys.exit(1 if n else 0)
