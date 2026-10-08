"""场上"别的牌看到某张牌进场"的钩子 —— PLAN 阶段 5 第一版（`OnOtherCardEnterPlay`）。

游戏怎么做的（1.60 导出 + 之前 CARD-PLAY-HOOKS 报告）：
    * 触发时机：某张牌**进场**（EOnEnterPlayMethod）
          1 = OnPlayedFromHand（从手牌打出）
          2 = OnAddedFromHand（从手牌加入）
          3 = OnSpawn（生成）
          4 = OnReveal（揭示）
          5 = OnConverted（转化）
    * 谁被触发：场上**每一张定义了 `OnOtherCardEnterPlay` 覆写**的牌（不含进场那张自己）；
    * 顺序：`FetchAllCardsWithEventTrigger` 的顺序（我们近似成**快照里的卡序**，即创建顺序）；
    * RNG：**同一个 Action 共用一条 `cardsRandomStream`** —— 所以自己的钩子先跑，
      再按顺序跑别人的钩子，`Stream` 对象一路传下去，抽取数累加。

边界（PLAN §5.2 明确）：**指令类（本回合触发）做；反制（敌方回合触发）不做**。
本模块只做"读 + 空跑"（外部 Kismet VM），不注入、不写游戏内存。

P2（2026-10-03）：本模块自 `semantics/triggers.py` **原样端口**到 `engine/triggers.py` —— 触发分发
（顺序 / 互斥 / 停止传播）归 engine 所有。`semantics/triggers.py` 成了**同一模块对象**的兼容别名
（`sys.modules` 替换；为什么不用逐名 re-export 见那个壳的注释：调用方/测试会直接给
`_run_hook_ex`、`find_cards` 一类模块属性打补丁，逐名导出会把补丁打在壳上、内部调用看不见）。

用法：
    from engine import triggers                           # 新代码；旧 `from semantics import triggers` 仍可用
    triggers.selftest()                                   # 离线（不需要游戏）
    triggers.find_cards(km, st)                           # 场上有哪些牌挂了钩子
    triggers.run_enter_play(km, st, card, method=1, stream=s)   # 跑一遍，拿效果摘要
    triggers.run_attack_hooks(km, st, atk, dfd, cost=1)         # 一次攻击上全部影响胜负的钩子（顺序=字节码）
    triggers.build_attack_fx(km, st, pairs, ...)                # → boardeval.from_cards(attack_fx=...)
    # 攻击链的逐钩子规格（形参顺序/出参/槽位/默认实现/证据）：ATTACK_HOOK_SPECS；详见 ATTACK-HOOKS-1.60.md
"""
from __future__ import annotations

from typing import Optional

from .triggers_specs import (  # noqa: F401,E402  —— P6：数据表搬到 triggers_specs，这里原名再导出
    METHODS,
    TRIGGER_ID,
    HOOK,
    HOOKS,
    COUNTER_HOOKS,
    ATTACK_HOOK_SPECS,
    ATTACK_HOOKS,
    ATTACK_OWN_HOOKS,
    MOVE_HOOK_SPECS,
    MOVE_HOOKS,
    MOVE_ALL_HOOKS,
    DRAW_HOOK_SPECS,
    DRAW_ALL_HOOKS,
    SUPPRESS_HOOK_SPECS,
    PIN_HOOK_SPECS,
    TURN_HOOK_SPECS,
    TURN_ALL_HOOKS,
    MOSQUITO_SKIP,
    REVEAL_HOOK_SPECS,
    MOVE_FRONT_HOOK_SPECS,
    RETREAT_HOOK_SPECS,
    CONVERT_HOOK_SPECS,
    LEAVE_METHOD_CONVERT,
    MOVE_REASON_NAME,
    ABILITIES_HOOK_SPECS,
    ABILITIES_CALLERS,
    ABILITIES_KW,
    NATIVE_DEFAULTS,
    native_default,
    method_of,
)


class TriggerCache:
    """按类缓存 `OnOtherCardEnterPlay` 的 UFunction 指针（没有覆写 ⇒ 记 None，别反复找）。"""

    def __init__(self, km):
        self.km = km
        self._fn: dict = {}

    def fn_of_class(self, uc: int, hook: str = HOOK) -> int:
        key = (uc, hook)
        if key not in self._fn:
            from kardsmem import kismet
            try:
                f = kismet.find_function(self.km, uc, hook, inherited=False)
            except Exception:                                     # noqa: BLE001
                f = None
            self._fn[key] = f or 0
        return self._fn[key]


def _registry_order(cards) -> list:
    """`FetchAllCardsWithEventTrigger` 的顺序 = 触发表 TSet 元素数组顺序 = 卡牌**创建**顺序
    （本局内表只加不删，见 CARD-PLAY-HOOKS §0.2）= `AllCardsInBattle` 的枚举顺序。
    `board_api` 的 `st.cards` 是按 (side, location, slot, uid) 重新排过序的，不是这个顺序；
    读取器若在 `card.raw["enum_idx"]` 里留了枚举下标（BOARD-QUERY-NATIVES §6.1），这里按它排；
    缺任何一张的下标 ⇒ 保持快照顺序（如实退化，不猜）。"""
    cs = list(cards or [])
    idx = [(getattr(c, "raw", None) or {}).get("enum_idx") for c in cs]
    if cs and all(isinstance(i, int) for i in idx):
        return [c for _i, c in sorted(zip(idx, cs), key=lambda t: t[0])]
    return cs


# `suppressionExceptionTriggers`（卡面静态默认值；`BP_GameState_Battle::FetchAllCardsWithEventTrigger` 用它放过
# 被压制的牌）：1.60 全卡池只有 10 张卡写了，**触发号只有 OnEndOfTurn/OnStartofTurn/OnBeforeStartOfTurn/
# OnOtherCardDrawnFromDeck**，没有任何攻击链触发号；运行时也没有任何蓝图写它（grep 全导出）。
# 键 = 卡内部名，值 = 钩子名集合（钩子名与触发号枚举名一致）。
SUPPRESSION_EXCEPTIONS = {
    "card_unit_gordon_highlanders": {"OnOtherCardDrawnFromDeck"},
    "card_unit_infantry_regiment_36": {"OnEndOfTurn"},
    "card_unit_panther_a": {"OnStartofTurn"},
    "card_unit_kurmark_aufklarungs": {"OnEndOfTurn"},
    "card_unit_3rd_kure_snlf": {"OnEndOfTurn"},
    "card_unit_danuta": {"OnEndOfTurn"},
    "card_unit_92nd_naval_brigade": {"OnStartofTurn"},
    "card_unit_99th_kholm": {"OnStartofTurn"},
    "card_unit_kv_85": {"OnStartofTurn"},
    "card_unit_1st_marines": {"OnStartofTurn"},
    "card_unit_a20_havoc": {"OnEndOfTurn"},
}


def _suppression_excepted(c, hook: str) -> bool:
    """这张牌挂了 `hook` 且该触发号在它的 `suppressionExceptionTriggers` 里 ⇒ 被压制也照跑。

    ★ 2026-10-02（测试抓到的真 bug）：表里的键名沿用了**枚举写法**（`OnStartofTurn`/`OnEndofTurn`），
      而钩子名是蓝图写法（`OnStartOfTurn`/`OnEndOfTurn`）—— 大小写不一致 ⇒ 这 11 张例外牌
      **从来没生效过**（被压制时它们的回合钩子被丢）。⇒ 大小写不敏感比较。
    """
    nm = str(getattr(c, "fname", None) or getattr(c, "name", None) or "").lower()
    return any(h.lower() == hook.lower() for h in SUPPRESSION_EXCEPTIONS.get(nm, ()))


def _trigger_of(hook: str):
    """钩子名 → 触发号（注册表的键）；不明 ⇒ None。"""
    if hook == HOOK:
        return TRIGGER_ID
    spec = ATTACK_HOOK_SPECS.get(hook) or {}
    t = spec.get("trigger")
    return t if isinstance(t, int) else None


def _loc_num(c) -> int:
    """`ECardLocationEnum` 原值（传给 `destroyedLocation` / `ExecuteOnCardMoved` 的参数）；读不出按前线 7（旧行为，缺口很小）。"""
    loc = None if c is None else c.obj.Location
    return int(loc) if loc is not None else 7


def _byid_of(st) -> dict:
    """`{card_id: 卡}`（后出现的覆盖先出现的，与原来的字典推导一致）。一次建 sim 里 `find_cards` 被调几百次、
    `st.cards` 不变 ⇒ 在 `readscope.build_scope()` 里按（列表对象, 长度）只算一遍。"""
    from kardsmem import readscope as _RS
    cards = getattr(st, "cards", None) or ()
    sc = _RS.current()
    if sc is not None:
        memo = sc.table("byid")
        ent = memo.get(id(cards))
        if ent is not None and ent[0] is cards and ent[1] == len(cards):
            return ent[2]
    byid = {getattr(c, "card_id", None): c for c in cards}
    if sc is not None:
        memo[id(cards)] = (cards, len(cards), byid)
    return byid


def find_cards(km, st, exclude=(), cache: Optional[TriggerCache] = None,
               hook: str = HOOK, sides=None, on_board_only: bool = True,
               skip_suppressed: bool = False) -> list:
    """挂了 `hook` 那张覆写的牌 → `[(card, fn_ptr)]`，按触发表顺序（见 `_registry_order`）。

    `exclude`：要排除的卡指针（进场的那张自己）；
    `sides`：`ESide` 集合；默认 None = **双方都看**（对方反制要算进来）；`on_board_only=False` 时连手牌等位置一起看；
    `skip_suppressed`：`FetchAllCardsWithEventTrigger` 会丢掉 `isSuppressed` 的牌，除非该触发号在它的
    `suppressionExceptionTriggers` 里（`SUPPRESSION_EXCEPTIONS`，攻击链触发号上没有任何例外）。攻击链一律传 True。
    传了 `cache` 时顺带缓存 `指针 → 类`（一次决策里同一批牌要问几十个钩子，别每次都读内存）。
    """
    from kardsmem.objects import ObjectArray
    tc = cache or TriggerCache(km)
    oa = ObjectArray(km)
    memo = getattr(tc, "_cls", None)
    if memo is None:
        memo = tc._cls = {}
    # ★ 2026-10-02（`+hooks` 恒空的真因）：`exclude` 以前直接 `set()`，而调用方
    #   `rule._play_hooks_triggers` 传的是 `board_api.Card`（dataclass ⇒ 不可哈希）
    #   ⇒ 每次调用当场 `TypeError: unhashable type: 'Card'`。统一按 `_ptr_of` 归一成
    #   指针，既修 bug 也让"传卡对象"这种自然写法不再炸。
    skip = {p for p in (_ptr_of(x) for x in (exclude or ())) if p}
    out = []
    # ★ 游戏自己的注册表（`st.registry`＝`CardFunctionTriggers`）是"此刻哪些牌挂了这个触发"的权威：
    #   脚本可能是**中途接手**的，不能靠"从头看到了什么"。它也包含不在场的牌（弃牌堆里刚打出的指令、
    #   牌库里的天气牌……）——游戏的 `FetchAllCardsWithEventTrigger` 就是按它迭代、由每张牌自己的钩子体判活。
    #   顺序 = 注册表元素顺序。注册表读不出 / 触发号不明 ⇒ 退回按快照扫（旧行为）。
    trig = _trigger_of(hook)
    reg = getattr(st, "registry", None)
    if reg is not None and trig is not None:
        byid = _byid_of(st)
        cand = [byid[i] for i in (reg.get(trig) or ()) if i in byid]
    else:
        cand = _registry_order(getattr(st, "cards", None))
        reg = None
    for c in cand:
        if skip_suppressed and getattr(c, "is_suppressed", None) and not _suppression_excepted(c, hook):
            continue
        if reg is None and on_board_only and not c.obj.IsFieldUnit():
            continue
        if sides is not None and c.obj.side not in sides:
            continue
        p = (getattr(c, "raw", None) or {}).get("ptr")
        if not p or p in skip:
            continue
        uc = memo.get(p)
        if uc is None:
            uc = memo[p] = oa.class_of(p) or 0
        if not uc:
            continue
        f = tc.fn_of_class(uc, hook)
        if f:
            out.append((c, f))
    return out


def run_enter_play(km, st, entered, method: int = 1, stream=None,
                   my_side=None, read_hooks=None, exclude=(), budget_s: float = 1.0,
                   cache: Optional[TriggerCache] = None) -> dict:
    """跑一遍"某张牌进场"触发的所有钩子。

    `entered`：进场那张卡（快照里的 Card 对象，或它的指针）。
    `stream`：**同一条** `kardsmem.rng.Stream`（跨钩子共用；不传 = 种子未知，随机点枚举，不伪造确定结果）。
    返回 `{"hits": [{"name","ptr","eff","stopped","spread"}], "draws": n, "cards": m}`。
    """
    from engine import effectvm as EV
    ptr = entered if isinstance(entered, int) else (getattr(entered, "raw", None) or {}).get("ptr")
    # ★ 种子未知时别拿 `Stream(0)` 伪造"确定"的随机结果（它会让 Recorder 走 exact 分支，按种子 0 的 LCG 给出
    #   一个看起来确定、实际是编的结果）；`None` ⇒ 随机点按"枚举/机会节点"记（不预测种子，见 effectvm 头注释）。
    st_stream = stream
    d0 = getattr(st_stream, "draws", 0)
    hits = []
    for c, _fn in find_cards(km, st, exclude=list(exclude) + ([ptr] if ptr else []), cache=cache):
        cp = (getattr(c, "raw", None) or {}).get("ptr")
        try:
            r = EV.record_effects(km, cp, ptr or 0, bool(ptr), HOOK, my_side,
                                  None, read_hooks=read_hooks, args={"cardPlayed": ptr or 0,
                                                                     "Method": int(method)},
                                  timeout_s=budget_s, rng_stream=st_stream,
                                  # ★ 2026-10-02：盘面级原语（IsSideActive…）要靠 BoardState；
                                  #   不传的话钩子会在第一条 IsSideActive 上如实停下（= 白跑）。
                                  board=st, my_seat=my_side)
        except Exception as e:                                    # noqa: BLE001
            r = {"eff": {}, "stopped": "%s: %s" % (type(e).__name__, str(e)[:60])}
        eff = dict(r.get("eff") or {})
        if eff or r.get("stopped"):
            hits.append({"name": getattr(c, "name", "?"), "ptr": cp, "eff": eff,
                         "stopped": r.get("stopped"),
                         "spread": bool(eff.pop("uncertain", None))})
    return {"hits": hits, "cards": len(hits),
            "draws": getattr(st_stream, "draws", 0) - d0}


def _run_hook(km, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
              st=None):
    """跑一张牌上的一个钩子 → (eff, stopped)。`c` 可以是卡对象，也可以是它的指针。"""
    from engine import effectvm as EV
    cp = c if isinstance(c, int) else (getattr(c, "raw", None) or {}).get("ptr")
    r = EV.record_effects(km, cp, 0, False, hook, my_side, None, read_hooks=read_hooks,
                          args=dict(args), timeout_s=budget_s, rng_stream=stream,
                          # ★ 2026-10-02：同 `_run_hook_ex` —— 盘面级原语（IsSideActive…）
                          #   需要 BoardState；少了它，`+hooks` 族会在第一条 IsSideActive
                          #   上如实停住（实机探针 probe_hooks_deep2 抓到）。
                          board=st, my_seat=my_side)
    return dict(r.get("eff") or {}), r.get("stopped")


def run_play_hooks(km, st, played, method: int = 1, stream=None, my_side=None,
                   read_hooks=None, exclude=(), budget_s: float = 1.0,
                   cache: Optional[TriggerCache] = None, include_counter: bool = True) -> dict:
    """打出 `played` 这张牌时，**游戏会让别的牌跑的那些钩子**全跑一遍（双方都看）。

    覆盖：`OnOtherCardEnterPlay`（进场）+ 反制族（`OnBeforeOtherCardPlayedFromHand` /
    `OnOtherCardPlayedFromHand` / `OnCounterMeasureTriggered`）—— 用户 2026-10-02：
    "对方反制对友方操作的影响" 也要算，而反制的条件/效果同样在蓝图里，跑它就行。
    RNG 与其它钩子**共用同一条流**（`stream`）。
    返回 `{"hits": [{"hook","name","ptr","side","eff","stopped"}], "draws": n}`。
    """
    ptr = played if isinstance(played, int) else (getattr(played, "raw", None) or {}).get("ptr")
    # ★ 种子未知时别拿 `Stream(0)` 伪造"确定"的随机结果（它会让 Recorder 走 exact 分支，按种子 0 的 LCG 给出
    #   一个看起来确定、实际是编的结果）；`None` ⇒ 随机点按"枚举/机会节点"记（不预测种子，见 effectvm 头注释）。
    st_stream = stream
    d0 = getattr(st_stream, "draws", 0)
    hooks = ([HOOK] + list(COUNTER_HOOKS)) if include_counter else [HOOK]
    hits = []
    for hook in hooks:
        if hook == HOOK:
            a = {"cardPlayed": ptr or 0, "Method": int(method)}
        elif hook == "OnCounterMeasureTriggered":
            a = {"countermeasureTriggering": ptr or 0, "qqq": False}
        else:
            a = {"cardPlayed": ptr or 0}
        # ★ 2026-10-02（TODO A10 第二条 / THE POMPADOURS）：打出卡触发族**不按位置过滤** ——
        #   游戏的 `FetchAllCardsWithEventTrigger` 把挂了触发号的实例都给出来，
        #   由**钩子自己的判据**决定要不要生效：`THE POMPADOURS` 这类"在手牌中生效"的牌
        #   在 `OnOtherCardPlayedFromHand` 里判 `IsLocatedInHand` ⇒ 以前
        #   `on_board_only=True` 会把它们整类漏掉（HQ +1 防御、揭示自己都丢）。
        for c, _fn in find_cards(km, st, exclude=list(exclude) + ([ptr] if ptr else []),
                                 cache=cache, hook=hook, on_board_only=False):
            eff, stopped = _run_hook(km, c, hook, a, st_stream, my_side, read_hooks, budget_s,
                                     st=st)
            if eff or stopped:
                hits.append({"hook": hook, "name": getattr(c, "name", "?"),
                             "ptr": (getattr(c, "raw", None) or {}).get("ptr"),
                             "side": getattr(c, "side", None), "eff": eff, "stopped": stopped})
    return {"hits": hits, "draws": getattr(st_stream, "draws", 0) - d0, "cards": len(hits)}


def _ptr_of(x):
    """卡对象/指针 → 指针；None/0 ⇒ 0。"""
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    if isinstance(x, dict):
        return _ptr_of(x.get("ptr"))
    return (getattr(x, "raw", None) or {}).get("ptr") or 0


def _run_hook_ex(km, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
                 hq_own=(), hq_enemy=(), st=None, view_overrides=None) -> dict:
    """跑一张牌上的一个钩子，**并带回出参/记录**（`stopAttack`/`newDefender`/`damageToAdd` 这类会改流程的值）。

    就是 `effectvm.record_effects`（2026-10-02 起它把 `vm.run` 的出参、是否真的跑了、读不到的状态一并带回）。
    返回 `{"ran": 有没有覆写并跑了, "eff", "stopped", "out": {形参名: 值}, "records", "chance", "gaps"}`。
    """
    from engine import effectvm as EV
    extra = {"view_overrides": view_overrides} if view_overrides else {}     # A5-③：改变后的卡视图（只读覆盖）
    return EV.record_effects(km, _ptr_of(c), 0, False, hook, my_side, None, read_hooks=read_hooks,
                             args=dict(args), hq_own=hq_own, hq_enemy=hq_enemy, timeout_s=budget_s,
                             rng_stream=stream,
                             board=st, my_seat=my_side, **extra)   # 盘面级原语（IsSideActive…）要用


# ---- 卡面数值/关键词的读法（缺了就按 0/False，如实标在报告里）----------------------------------
_UNIT_TYPES = frozenset(("infantry", "tank", "artillery", "fighter", "bomber", "antiair", "antitank",
                         "tankdestroyer"))


def _kw_default(c) -> set:
    out = set()
    for k in (getattr(c, "keywords", None) or ()):
        k = str(k).lower()
        out.add(k[4:] if k.startswith("has_") else k)
    return out


def _n(x, d=0) -> int:
    try:
        return d if x is None else int(x)
    except (TypeError, ValueError):
        return d


def _atk(c) -> int:
    f = getattr(c, "total_attack", None)
    return _n(f() if callable(f) else getattr(c, "attack", None))


def _dfn(c) -> int:
    return _n(getattr(c, "defense", None))


def _armor(c) -> int:
    raw = getattr(c, "raw", None) or {}
    return _n(raw.get("total_heavy_armor", getattr(c, "heavy_armor", 0)))


def _ability(c, kw_of, name: str) -> bool:
    """关键词 / 被贴的自定义能力（如 lethal、excess、immune）。读不到 ⇒ False。"""
    if name in kw_of(c):
        return True
    raw = getattr(c, "raw", None) or {}
    for a in (raw.get("received_abilities") or ()):
        if str((a.get("ability") if isinstance(a, dict) else a) or "").lower() == name:
            return True
    return False


def ordered_gotchas(cards) -> list:
    """`BP_CardFunctions::GetActiveGotchasOrdered`（字节码已验证）：所有 `IsGotcha ∧ gotchaActivated>0` 的牌
    （**不看位置**），按 key 升序：`card_event_ultra` 的 key = -(cardID+1000000)（最先，cardID 大的更先），
    `card_event_interception` 的 key = -cardID，其余 key = `gotchaActivated`（激活次序号）；
    次序号撞车的按遇到顺序给备用 key 100,101,…。返回 Card 列表。"""
    keyed, backup, used = [], 100, set()
    for c in _registry_order(cards):
        if getattr(c, "card_type", None) != "gotcha" or not (getattr(c, "gotcha_activated", 0) or 0) > 0:
            continue
        nm = str(getattr(c, "fname", None) or getattr(c, "name", None) or "").lower()
        cid = getattr(c, "card_id", 0) or 0
        if nm == "card_event_ultra":
            k = -(cid + 1000000)
        elif nm == "card_event_interception":
            k = -cid
        else:
            k = c.obj.gotchaActivated
            if k in used:
                k, backup = backup, backup + 1
        used.add(k)
        keyed.append((k, c))
    return [c for _k, c in sorted(keyed, key=lambda t: t[0])]


class _Chain:
    """一次攻击（`BP_CardFunctions::AttackCard`）的钩子链。各阶段与字节码一一对应，见 `run_attack_hooks`。"""

    def __init__(self, km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of,
                 hq_own=(), hq_enemy=(), enum_random=False, in_combat=True):
        self.km, self.st, self.stream, self.cache = km, st, stream, cache
        self.enum_random, self.in_combat = enum_random, in_combat
        self.my_side, self.read_hooks, self.budget_s = my_side, read_hooks, budget_s
        self.include_own, self.kw_of = include_own, kw_of
        self.hq_own, self.hq_enemy = tuple(hq_own), tuple(hq_enemy)
        self.hits, self.order = [], []
        self.bucket = "before"
        self.view_overrides = {}          # {ptr: {视图键: 值}}：旁观者钩子读到的「事件之后」的卡状态（A5-③）
        self.notes = []                   # 如实记下这条链上没建模/没证实的点（并入 run_* 的 meta）
        cs = list(getattr(st, "cards", None) or [])
        self.by_ptr = {_ptr_of(c): c for c in cs if _ptr_of(c)}

    # ---- 基础设施 ----
    def card(self, x):
        return x if (x is not None and not isinstance(x, int)) else self.by_ptr.get(_ptr_of(x))

    def suppressed(self, c) -> bool:
        c = self.card(c)
        return bool(getattr(c, "is_suppressed", None)) if c is not None else False

    def call(self, hook, c, args) -> dict:
        extra = {"view_overrides": self.view_overrides} if self.view_overrides else {}
        r = _run_hook_ex(self.km, c, hook, args, self.stream, self.my_side, self.read_hooks,
                         self.budget_s, self.hq_own, self.hq_enemy, st=self.st, **extra)
        if r.get("ran") is False and not r.get("eff") and not r.get("out"):
            return r                         # 没覆写 ⇒ 空操作（自己的钩子大多如此），不记
        if (self.enum_random and self.stream is None and r.get("nodes")
                and not ATTACK_HOOK_SPECS.get(hook, {}).get("outs")):
            r = self._enumerate(hook, c, args, r)
        cc = self.card(c)
        nm = getattr(cc, "name", None) or "?"
        self.order.append((hook, nm))
        self.hits.append({"hook": hook, "name": nm, "ptr": _ptr_of(c), "side": getattr(cc, "side", None),
                          "bucket": self.bucket, "eff": r.get("eff") or {}, "stopped": r.get("stopped"),
                          "out": dict(r.get("out") or {}), "records": list(r.get("records") or []),
                          "chance": list(r.get("chance") or []), "gaps": list(r.get("gaps") or [])})
        return r

    def _enumerate(self, hook, c, args, r) -> dict:
        """钩子里有**可枚举的随机点**且种子未知 ⇒ 对各随机结果各空跑一遍，效果摘要取期望分支
        `{"outcomes": [(权重, eff)…]}`（弯路 #38 的纪律：随机按真随机对待，不预测种子；与
        `rule._after_hq` 同一做法）。枚举失败 ⇒ 保留单路径结果并带 `chance` 标记。
        ★ 只对"没有出参"的钩子枚举（出参会在枚举里丢失）；枚举后 `records` 清空（效果只在 `eff.outcomes`）。"""
        try:
            from engine import effectvm as EV
            en = EV.enumerate_effects(self.km, _ptr_of(c), 0, False, hook=hook, my_side=self.my_side,
                                      read_hooks=self.read_hooks, args=dict(args), hq_own=self.hq_own,
                                      hq_enemy=self.hq_enemy, budget_s=self.budget_s)
            outs = [(w, dict(e)) for w, e in (en.get("outcomes") or [])]
            if len(outs) > 1:
                r = dict(r, eff={"outcomes": outs}, records=[])
            elif len(outs) == 1:
                r = dict(r, eff=outs[0][1], records=r.get("records") or [])
        except Exception:                                         # noqa: BLE001
            pass
        return r

    def others(self, hook, exclude=()):
        return find_cards(self.km, self.st, exclude=[x for x in exclude if x], cache=self.cache, hook=hook,
                          on_board_only=False, sides=None, skip_suppressed=True)

    def mark(self, tag):
        self.order.append((tag, ""))

    @staticmethod
    def outv(r, name, default=None):
        o = r.get("out") or {}
        return o[name] if name in o and o[name] is not None else default

    # ---- 伤害管线（CalculateDamageDealt 里的钩子）----
    def add_damage(self, dealer, receiver, damage, from_attack, from_fight, is_def_dmg) -> int:
        """`ExecuteOnDealDamageAddDamage`：自己的 OnCardDealDamage_ModifyDamageDealt（未被压制）→ 0x25
        OnOtherCardDealDamageAddDamage（登记的牌全问，不排除自己）→ reRun 的再问一遍 → 夹到 [0,99]。"""
        dp, rp = _ptr_of(dealer), _ptr_of(receiver)
        calc = damage
        if not self.suppressed(dealer):
            r = self.call("OnCardDealDamage_ModifyDamageDealt", dealer,
                          {"toCard": rp, "Damage": damage, "fromAttack": from_attack, "fromFight": from_fight})
            calc = _n(self.outv(r, "newDamage"), damage)           # 原生默认：回显 Damage
        rerun = []
        for c, _fn in self.others("OnOtherCardDealDamageAddDamage"):
            r = self.call("OnOtherCardDealDamageAddDamage", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": calc, "fromAttack": from_attack,
                           "isDefenderDamage": is_def_dmg})
            calc += _n(self.outv(r, "damageToAdd"), 0)
            if self.outv(r, "reRunAtEnd"):
                rerun.append(c)
        for c in rerun:
            r = self.call("OnOtherCardDealDamageAddDamage", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": calc, "fromAttack": from_attack,
                           "isDefenderDamage": is_def_dmg})
            calc = _n(self.outv(r, "damageToAdd"), 0) + calc
        return max(0, min(99, calc))

    def after_calc(self, to_card, dealer, amount, is_combat, is_attacking, is_redirected) -> int:
        """`ExecuteOnDealDamageAddDamageAfterCalc`：免疫 ⇒ 0；自己的 OnDealDamageAddDamageAfterCalc（未被压制）
        → 0x26（排除伤害来源自己；`card_event_national_fire_service` 推迟到最后；回 stopAdding 即 break）。"""
        dp, rp = _ptr_of(dealer), _ptr_of(to_card)
        if _ability(self.card(to_card) or to_card, self.kw_of, "immune"):
            return 0
        tmp = amount
        if not self.suppressed(dealer):
            r = self.call("OnDealDamageAddDamageAfterCalc", dealer,
                          {"toCard": rp, "Damage": amount, "fromAttack": is_combat,
                           "isAttacker": is_attacking, "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + amount, 0)
        run_after, stopped = [], False
        for c, _fn in self.others("OnOtherCardDealDamageAddDamageAfterCalc"):
            if stopped:
                break
            if getattr(self.card(c), "name", None) == "card_event_national_fire_service":
                run_after.append(c)
                continue
            if _ptr_of(c) == dp:
                continue
            r = self.call("OnOtherCardDealDamageAddDamageAfterCalc", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": tmp, "fromAttack": is_combat,
                           "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + tmp, 0)
            stopped = bool(self.outv(r, "stopAdding"))
        for c in run_after:
            r = self.call("OnOtherCardDealDamageAddDamageAfterCalc", c,
                          {"cardDealingDamage": dp, "toCard": rp, "Damage": tmp, "fromAttack": is_combat,
                           "isRedirected": is_redirected})
            tmp = max(_n(self.outv(r, "damageToAdd"), 0) + tmp, 0)
        return max(tmp, 0)

    def calc_dealt(self, dealer, receiver, is_attacker) -> dict:
        """`CalculateDamageDealt(dealer, receiver, damageDealerIsAttacker, ignoreAmbush=False,
        ignoreHeavyArmor=False, applyBeforeAttackBuffs=False)` 的移植（真攻击就是这组参数）。
        ★ applyBeforeAttackBuffs=False ⇒ GetBeforeAttack*/BeforeAttackDamage 这 4 个"预览用"的 getter **不会被调**；
          GetPassiveDefenseBuff 只在伏击判定里用（对 receiver 的减伤因 SelectInt 选了 0 而不生效）。"""
        kw = self.kw_of
        dc, rc_ = self.card(dealer) or dealer, self.card(receiver) or receiver
        res = {"damage": 0, "dies": False, "killed_before": False, "shock": False}
        if _ability(rc_, kw, "immune"):
            return res
        dcalc = self.add_damage(dealer, receiver, _atk(dc), True, False, not is_attacker)
        rcalc = self.add_damage(receiver, dealer, _atk(rc_), True, False, is_attacker)
        typ_d, typ_r = getattr(dc, "card_type", None), getattr(rc_, "card_type", None)
        # 伏击：只有"攻击方打在没被打过的伏击单位上"，且不是炮兵/轰炸机(无防空/战斗机时)/冲击/受击方是轰炸机
        if ("ambush" in kw(rc_) and is_attacker and not getattr(rc_, "has_been_attacked_this_turn", False)
                and not _ability(dc, kw, "immune") and typ_d != "artillery"
                and not (typ_d == "bomber" and typ_r not in ("antiair", "fighter"))
                and not (typ_r == "bomber" or "shock" in kw(dc))):
            passive = 0
            if typ_d in _UNIT_TYPES:
                rp_ = self.call("GetPassiveDefenseBuff", dealer, {"incomingDamage": rcalc})
                passive = _n(self.outv(rp_, "amount"), 0)
            if rcalc >= _dfn(dc) + _armor(dc) + passive:
                dcalc = 0
        if not is_attacker and typ_r == "artillery":
            dcalc = 0
        if not is_attacker and typ_d == "bomber":
            dcalc = 0
        if not is_attacker and typ_r == "bomber" and typ_d not in ("fighter", "antiair"):
            dcalc = 0
        if typ_d in _UNIT_TYPES and "shock" in kw(rc_) and not is_attacker:
            dcalc = 0
            res["shock"] = True
        if dcalc > 0:
            dcalc = max(dcalc - _armor(rc_), 0)
        dies = dcalc >= _dfn(rc_)
        is_loc = (rc_ is not None and rc_.obj.IsHQ()) or typ_r == "location"
        if not dies and _ability(dc, kw, "lethal") and not is_loc and dcalc > 0:
            dies = True
        res.update(damage=dcalc, dies=dies)
        return res

    # ---- 受击 / 幸存 / 造成伤害 ----
    def receive_damage(self, receiver, from_card, amount, is_combat=True):
        """`ExecuteBeforeReceiveDamage(cardToReceiveDamage, cardToDealDamage, amount, isCombat)`
        （BP_CardFunctions.cpp:13855：自己未压制才 `OnReceiveDamage`，再 fetch 0x34 逐张、cardID==受击者 的跳过）。
        战斗链传 `isCombat=True`；`ApplyDamageToCard`（效果伤害 / 对打）传 False ⇒ 0x34 的 `fromAttack=False`。"""
        rp, fp = _ptr_of(receiver), _ptr_of(from_card)
        if self.include_own and rp and not self.suppressed(receiver):
            self.call("OnReceiveDamage", receiver, {"fromCard": fp, "Damage": amount})
        for c, _fn in self.others("OnOtherCardReceiveDamage", exclude=[rp]):
            self.call("OnOtherCardReceiveDamage", c,
                      {"fromCard": fp, "toCard": rp, "fromAttack": bool(is_combat), "Damage": amount})

    def survived(self, surviving, combatted):
        """`ExecuteOnSurvivedCombatEvents`：自己的 OnSurvivedCombat（未被压制）→ 0x3B（排除幸存者）。"""
        sp, cp = _ptr_of(surviving), _ptr_of(combatted)
        if self.include_own and sp and not self.suppressed(surviving):
            self.call("OnSurvivedCombat", surviving, {"cardCombated": cp})
        for c, _fn in self.others("OnOtherCardSurvivedCombat", exclude=[sp]):
            self.call("OnOtherCardSurvivedCombat", c, {"survivor": sp, "cardCombated": cp})

    def deal_damage_effects(self, to_card, dealer, damage, counter, is_combat=True, redirected=False):
        """`ExecuteOnCardDealDamageEffects(toCard, damageDealer, damage, isCombat, CounterDamage, isRedirected)`
        （战斗链 isCombat=True/redirected=False；`ApplyDamageToCard` 传 isCombat=False，BP:16508）：
        伤害来源自己的 OnCardDealDamage（未被压制）→ 0x24（排除来源自己 = cardsDone）。
        （前面的"反制先手"部分——`GetActiveGotchasOrdered`——未实现，见报告。）"""
        tp, dp = _ptr_of(to_card), _ptr_of(dealer)
        done = []
        # 反制先手：`GetActiveGotchasOrdered` 里每张已激活的反制先收 `OnOtherCardDealDamage`（不看登记表、
        # 不看压制），并计入 cardsDone ⇒ 后面的 0x24 不再问它们（`GetStopFurtherActions` 为真则整段中止，
        # 我们不模拟反制"叫停"，未实现）
        for g in ordered_gotchas(getattr(self.st, "cards", None)):
            gp = _ptr_of(g)
            if gp and gp != dp:
                self.call("OnOtherCardDealDamage", g,
                          {"cardDealingDamage": dp, "toCard": tp, "Damage": damage, "isCombatDamage": bool(is_combat),
                           "CounterDamage": counter, "isRedirected": bool(redirected)})
                done.append(gp)
        if self.include_own and dp and not self.suppressed(dealer):
            self.call("OnCardDealDamage", dealer,
                      {"toCard": tp, "Damage": damage, "isCombatDamage": bool(is_combat), "CounterDamage": counter,
                       "isRedirected": bool(redirected)})
        for c, _fn in self.others("OnOtherCardDealDamage", exclude=[dp] + done):
            self.call("OnOtherCardDealDamage", c,
                      {"cardDealingDamage": dp, "toCard": tp, "Damage": damage, "isCombatDamage": bool(is_combat),
                       "CounterDamage": counter, "isRedirected": bool(redirected)})

    def apply_damage(self, to_card, dealer, final, redirected=False, prefix=""):
        """`ApplyDamageToCard(toCard, damageDealer, damage, isRedirected, isFightDefenderDamage)` 的**钩子段**
        （BP_CardFunctions.cpp:16375；顺序取自 CARD-PLAY-HOOKS-1.60.md §6 对该函数的重建，与反编译里
        `ExecuteBeforeReceiveDamage`@16399 → `HasCustomAbility("excess")`@16414 → `getTotalDefense`/`setAndEncryptDefense`@16461
        → `ExecuteOnCardDealDamageEffects(…, false, false, isRedirected)`@16508 → `ExecuteOnBeforeOtherCardDestroyed`/
        `ApplyRemoveCardFromBoard`@16531 → `RemoveMobilize`@16558 的出现次序一致）：

          ① `final > 0` ⇒ `ExecuteBeforeReceiveDamage(to, dealer, final, isCombat=false)`   [桶 "recv"]
          ② dealer 带 `excess`（溢出打总部）⇒ **未建模**（反编译里该段控制流被 Sequence 的 pop 拆开，分支条件读不准，记 notes）
          ③ 扣防（状态变更，sim 侧）
          ④ `ExecuteOnCardDealDamageEffects(to, dealer, final, isCombat=false, Counter=false, isRedirected)` [桶 "dealt"]
             ★ 报告里这一步没写 `final>0` 的闸门，这里**按字面不加闸门**；`final==0` 时是否仍调用未用字节码证实（notes）。
          ⑤ 被摧毁 ⇒ 总部：对局结束；其它：`ExecuteOnBeforeOtherCardDestroyed` + `ApplyRemoveCardFromBoard`（摧毁链，
             由 `death_fx` / `run_death_chain` 管，不在这里算）；没被摧毁且 `hasMobilize` 且 `final>0` ⇒ `RemoveMobilize`（状态，sim 侧）。
        """
        dc = self.card(dealer) or dealer
        if _ability(dc, self.kw_of, "excess"):
            self.notes.append("ApplyDamageToCard 的 excess（溢出伤害打敌方总部）分支未建模：反编译控制流不清，只记缺口")
        self.bucket = prefix + "recv"
        if final > 0:
            self.receive_damage(to_card, dealer, final, is_combat=False)
        self.bucket = prefix + "dealt"
        self.deal_damage_effects(to_card, dealer, final, False, is_combat=False, redirected=redirected)
        if final <= 0:
            self.notes.append("final==0 时 ExecuteOnCardDealDamageEffects 是否仍被调用未用字节码证实（这里按报告字面调用）")

    def abilities_changed(self, card) -> int:
        """`ExecuteOnOtherCardsAbilitiesChanged(CardChanging)`（BP_CardFunctions.cpp:20885）：fetch **0x1D** 逐张
        `OnOtherCardAbilitiesChanged(CardChanging)`，**不排除自己、没有别的条件**（被压制的牌被 fetch 丢掉）。返回调用张数。"""
        ptr, n = _ptr_of(card), 0
        for c, _fn in self.others("OnOtherCardAbilitiesChanged", exclude=()):
            self.call("OnOtherCardAbilitiesChanged", c, {"cardChanging": ptr})
            n += 1
        return n

    # ---- 摧毁链 ----
    def before_destroyed(self, victim, killer):
        """`ExecuteOnBeforeOtherCardDestroyed(cardDestroyedID, AttackerID, False, DestroyedInCombat=True)`。"""
        vp, kp = _ptr_of(victim), _ptr_of(killer)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnBeforeDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})
        for c, _fn in self.others("OnBeforeOtherCardDestroyed", exclude=[vp]):
            self.call("OnBeforeOtherCardDestroyed", c,
                      {"cardDestroyed": vp, "killer": kp, "TriggerNotDestroyed": False,
                       "destroyedInCombat": self.in_combat})

    def leave_board(self, victim, old_loc_on_board=True, method=1, going_to=8):
        """`ExecuteOnBeforeLeaveBoardOrOwnerEvents(card, NewLocation=8(弃牌堆), OldLocation, method)`
        （BP_CardFunctions.cpp:13327；摧毁 method=1 / 转化 6 / 移除 2）：
        只有 OldLocation 在 5/6/7（场上）才触发。自己的 OnLeaveBoardOrOwner → 0x2E。"""
        vp = _ptr_of(victim)
        if not old_loc_on_board:
            return
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnLeaveBoardOrOwner", victim, {"goingToLocation": going_to, "leavePlayMethod": method})
        for c, _fn in self.others("OnOtherCardLeaveBoardOrOwner", exclude=[vp]):
            self.call("OnOtherCardLeaveBoardOrOwner", c,
                      {"cardLeaving": vp, "goingToLocation": going_to, "Method": method})

    def location_moved(self, victim, old_loc, reason="Destroyed", new_loc=8):
        """`CardLocationMoved(..., newLocation=8, reason)` → `ExecuteOnCardLocationMoved`（:14213）：
        自己的 OnCardLocationMoved → 0x2F OnOtherCardLocationMoved（排除自己）。`reason` 是
        `GetEnumeratorUserFriendlyName(CardMoveReason, 值)` 的显示名（摧毁 "Destroyed"、转化 "Convert"）。"""
        vp = _ptr_of(victim)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnCardLocationMoved", victim, {"OldLocation": old_loc, "NewLocation": new_loc,
                                                       "ChangeOwner": False, "MoveReason": reason})
        for c, _fn in self.others("OnOtherCardLocationMoved", exclude=[vp]):
            self.call("OnOtherCardLocationMoved", c,
                      {"cardMoved": vp, "OldLocation": old_loc, "NewLocation": new_loc, "ChangeOwner": False,
                       "MoveReason": reason})

    def after_leave(self, victim, old_loc, old_loc_on_board=True, going_to=8):
        """`ExecuteOnAfterLeaveBoardOrOwnerEvents`（:13486）：自己的 OnAfterLeaveBoard → 0x08 OnAfterOtherCardLeaveBoardOrOwner。"""
        vp = _ptr_of(victim)
        if not old_loc_on_board:
            return
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnAfterLeaveBoard", victim, {"goingToLocation": going_to})
        for c, _fn in self.others("OnAfterOtherCardLeaveBoardOrOwner", exclude=[vp]):
            self.call("OnAfterOtherCardLeaveBoardOrOwner", c, {"cardLeaving": vp, "OldLocation": old_loc})

    def destroyed_fn(self, victim, killer, all_destroyed):
        """`ExecuteOnCardDestroyedFunction(card, location, killer, allCardsGettingDestroyed, destroyedInCombat=True)`：
        自己的 OnDestroyed（未被压制、有摧毁效果、没 StopDestructionEffect）→ 0x27 OnOtherCardDestroyed
        （排除自己；`selfIsAlsoGettingDestroyed` = 对方是否也在这批里）→ 自己的 OnAfterDestroyed。
        ★ 未实现：0x18 摧毁效果倍增（`ExecuteOnDestructionEffectTriggered`，只有 4 张卡）、`hasSalvage` 打捞。"""
        vp, kp = _ptr_of(victim), _ptr_of(killer)
        v = self.card(victim)
        loc = _loc_num(v)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})
            # 摧毁效果倍增（0x18）：Σ 各登记牌的 OnDestructionEffectTriggered 的 TriggerMultiple；
            # 倍数 m>0 ⇒ 再以 TriggerNotDestroyed=True 重放 OnDestroyed，每次重放后重算 m，直到 i>m
            i, mult = 1, self.destruction_multiple(victim, kp, all_destroyed)
            while mult > 0 and i <= mult and i <= 8:                   # 8 = 防跑飞的硬上限
                self.call("OnDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": True})
                mult = self.destruction_multiple(victim, kp, all_destroyed)
                i += 1
        for c, _fn in self.others("OnOtherCardDestroyed", exclude=[vp]):
            self.call("OnOtherCardDestroyed", c,
                      {"cardDestroyed": vp, "killer": kp, "TriggerNotDestroyed": False,
                       "destroyedLocation": loc,
                       "selfIsAlsoGettingDestroyed": _ptr_of(c) in all_destroyed, "destroyedInCombat": self.in_combat})
        self.salvage(victim, killer)
        if self.include_own and vp and not self.suppressed(victim):
            self.call("OnAfterDestroyed", victim, {"killer": kp, "TriggerNotDestroyed": False})

    def destruction_multiple(self, victim, kp, all_destroyed) -> int:
        """`ExecuteOnDestructionEffectTriggered(cardTriggered, instigatorID, ..., skipSuppressCheck=False)`：
        对 0x18 登记牌（被压制的丢掉）逐个调 `OnDestructionEffectTriggered(cardTriggered, instigatorID,
        SelfAlsoDestroyed, &TriggerMultiple)`，把 TriggerMultiple 加起来（4 张卡覆写）。"""
        vp = _ptr_of(victim)
        total = 0
        for c, _fn in self.others("OnDestructionEffectTriggered"):
            r = self.call("OnDestructionEffectTriggered", c,
                          {"cardTriggered": vp, "instigatorID": _n(getattr(self.card(kp), "card_id", 0)),
                           "SelfAlsoDestroyed": _ptr_of(c) in all_destroyed})
            total += _n(self.outv(r, "TriggerMultiple"), 0)
        return total

    def salvage(self, victim, killer):
        """打捞：击杀者 `hasSalvage` ∧ 其阵营在行动 ∧ 被摧毁者是敌方 ∧ 没有 `cantBeSalvaged` ⇒
        `SalvageMultipleUnits([victim], killer)`（把被摧毁单位的一张副本给击杀者方手牌）。
        记成 `salvage_ids:[被摧毁单位 card_id]`（boardeval 的 `_salvage_one`：1/1、费用≤3 的副本进手牌）。"""
        k, v = self.card(killer), self.card(victim)
        if k is None or v is None:
            return
        kw = self.kw_of
        if ("salvage" in kw(k) or _ability(k, kw, "salvage")) and getattr(k, "side", None) != getattr(v, "side", None) \
                and not _ability(v, kw, "cantbesalvaged"):
            self.hits.append({"hook": "<salvage>", "name": getattr(k, "name", "?"), "ptr": _ptr_of(k),
                              "side": getattr(k, "side", None), "bucket": self.bucket, "eff": {"salvage_ids": [getattr(v, "card_id", None)]},
                              "stopped": None, "out": {}, "records": [], "chance": [], "gaps": []})
            self.order.append(("<salvage>", getattr(v, "name", "?")))

    def destroy_combat(self, att, dfd, att_dies, def_dies):
        """`ExecuteAttackCard` 末尾的摧毁序列。双方都死：先防守方后攻击方，逐阶段交错
        （BeforeDestroyed×2 → BeforeLeave×2 → Moved×2 → AfterLeave×2 → DestroyedFn×2）；
        只死一个：BeforeDestroyed → `ApplyRemoveCardFromBoard`（BeforeLeave → Moved → AfterLeave → DestroyedFn）。"""
        ap, dp = _ptr_of(att), _ptr_of(dfd)
        loc = lambda c: _loc_num(self.card(c))  # noqa: E731
        if att_dies and def_dies:
            self.bucket = "def_destroyed"
            self.before_destroyed(dfd, att)
            self.bucket = "att_destroyed"
            self.before_destroyed(att, dfd)
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.leave_board(who)
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.location_moved(who, loc(who))
            for who, bk in ((dfd, "def_destroyed"), (att, "att_destroyed")):
                self.bucket = bk
                self.after_leave(who, loc(who))
            for who, killer, bk in ((dfd, att, "def_destroyed"), (att, dfd, "att_destroyed")):
                self.bucket = bk
                self.destroyed_fn(who, killer, {ap, dp})
            return
        for who, killer, dies, bk in ((att, dfd, att_dies, "att_destroyed"), (dfd, att, def_dies, "def_destroyed")):
            if not dies:
                continue
            self.bucket = bk
            self.before_destroyed(who, killer)
            self.leave_board(who)
            self.location_moved(who, loc(who))
            self.after_leave(who, loc(who))
            self.destroyed_fn(who, killer, {_ptr_of(who)})


def run_attack_hooks(km, st, attacker, defender, damage=None, cost: int = 0,
                     stream=None, my_side=None, read_hooks=None, budget_s: float = 1.0,
                     cache: Optional[TriggerCache] = None, include_own: bool = True,
                     damage_to_attacker=None, shock: bool = False,
                     include_damage_hooks: bool = True, kw_of=None,
                     hq_own=(), hq_enemy=(), enum_random: bool = False) -> dict:
    """跑一遍"一次攻击"会触发的**全部影响胜负的钩子**，顺序与 `BP_CardFunctions::AttackCard` →
    `ExecuteAttackCard` → `ExecuteOnAfterAttackEvents` 的**字节码**一致（ATTACK-HOOKS-1.60.md §3；
    1.58 uexp 反汇编，1.60 的 FModel 标签偏移逐一吻合）：

      ① 0x1E `OnOtherCardAttackSwitchTarget(cardAttacking, oldDefender, &newDefender)`：**所有**登记的牌
         （含攻击者自己，不排除）按表顺序问；第一个回 `newDefender != 当前防守方` 的牌生效并**立刻 break**。
      ② 0x1F `OnOtherCardAttacks(cardAttacking, defenderCard, &stopAttack, &AttackedAndStopped)`：登记的牌
         （**排除攻击者**）**全部**问一遍（不提前 break）；任一回 stopAttack ⇒ StopAttack；
         任一回 AttackedAndStopped ⇒ tmp。
      ③ StopAttack ⇒ `success=true` 直接结束 —— **不付指挥点、不触发任何后续钩子**。
      ④ **付指挥点**（`ChangeKreditsBySide(-costToPay)`）—— 在 OnBeforeAttack **之前**（字节码 @2764 定案）。
      ⑤ 攻击者自己的 `OnBeforeAttack(defenderCard)`（未被压制）→ 0x0D `OnBeforeOtherCardAttacks(cardAttacking,
         defenderCard)`（排除攻击者）→ RemoveSmokescreen。
      ⑥ tmp(AttackedAndStopped)：防守方置空、伤害 0；`ExecuteStoppedAttack` = `OnAttackStopped()`；跳到 ⑩。
      ⑦ 伤害：两次 `CalculateDamageDealt`（每次内部各跑两遍 `ExecuteOnDealDamageAddDamage`：自己的
         ModifyDamageDealt → 0x25）；再两次 `ExecuteOnDealDamageAddDamageAfterCalc`（自己的 → 0x26）。
         给定 `damage`/`damage_to_attacker`（最终值）则跳过整条伤害管线。
      ⑧ `ExecuteAttackCard`：受击通知（伤害>0 且受击方未被压制；防守方先）`OnReceiveDamage`/0x34；
         `excess` 溢出伤害打总部；幸存通知（防守方是单位时）`OnSurvivedCombat`/0x3B；造成伤害通知
         `OnCardDealDamage`/0x24（先打防守方的、再反击的）；防守方是总部且被摧毁 ⇒ 对局结束（不再往下）；
         摧毁链（BeforeDestroyed/0x0F → BeforeLeave/0x2E → Moved/0x2F → AfterLeave/0x08 → OnDestroyed/0x27/
         OnAfterDestroyed）。
      ⑨ `ExecuteOnAfterAttackEvents`：自己的 `OnAfterAttack(defenderCard, wasShockAttack, attackCost)`（未被压制）
         → 0x04 `OnAfterOtherCardAttacks(defenderCard, attackerCard, damageToDefender, attackCost)`（排除攻击者）。
         ★ 形参顺序是 (defender, attacker)，与 ①②⑤ 相反。
      ⑩ `ExecuteOnOperationKreditsSpent`：自己的 `OnOperationKreditsSpent(kreditsSpent)` → 0x44
         `OnOtherCardOperationKreditsSpent(cardOperated, kreditsSpent)`（排除攻击者）。

    双方的牌都枚举（注册表不按阵营/位置过滤，钩子自己 gate）；被压制的牌被 `FetchAllCardsWithEventTrigger`
    丢掉（`suppressionExceptionTriggers` 例外**未确认**）；所有钩子共用**同一条** `stream`
    （`None` ⇒ 种子未知：随机点按"枚举/机会节点"处理，不伪造确定结果）。

    返回 `{"hits", "order", "defender", "switched", "stop", "attacked_and_stopped", "paid", "outcome",
           "damage", "damage_to_attacker", "attacker_destroyed", "defender_destroyed", "match_end",
           "excess", "draws", "cards"}`；`outcome` ∈ {"stopped", "consumed", "resolved", "match_end"}；
    `order` = 实际执行的 (钩子名, 卡名) 序列（`<pay>` = 扣指挥点的时点）；`hits` = 其中每次钩子的记录
    （带 `bucket`/`records`，供 `to_fx` 转成 boardeval 能消费的效果）。
    """
    ap = _ptr_of(attacker)
    dp = _ptr_of(defender)
    d0 = getattr(stream, "draws", 0) if stream is not None else 0
    ch = _Chain(km, st, stream, cache, my_side, read_hooks, budget_s, include_own, kw_of or _kw_default,
                hq_own, hq_enemy, enum_random=enum_random)
    atk_card = ch.card(attacker) or attacker
    kw = ch.kw_of

    def result(outcome, dptr, switched, stop, tmp, paid, **more):
        r = {"hits": ch.hits, "order": ch.order, "defender": dptr, "switched": switched, "stop": stop,
             "attacked_and_stopped": tmp, "paid": paid, "outcome": outcome, "damage": 0,
             "damage_to_attacker": 0, "attacker_destroyed": False, "defender_destroyed": False,
             "match_end": None, "excess": 0, "explicit_damage": damage is not None,
             "draws": (getattr(stream, "draws", 0) - d0) if stream is not None else 0}
        r.update(more)
        r["cards"] = len(ch.hits)
        return r

    # ① 换目标（0x1E）：不排除攻击者；第一个换了的生效并 break
    ch.bucket = "before"
    switched = False
    for c, _fn in ch.others("OnOtherCardAttackSwitchTarget"):
        r = ch.call("OnOtherCardAttackSwitchTarget", c, {"cardAttacking": ap, "oldDefender": dp})
        nd = _ptr_of((r.get("out") or {}).get("newDefender"))
        if nd and nd != dp:
            dp, switched = nd, True
            break
    dfd_card = ch.card(dp) if (switched or isinstance(defender, int)) else defender
    # ② 吞攻击（0x1F）：排除攻击者；全部问完（不提前 break）
    stop = tmp = False
    for c, _fn in ch.others("OnOtherCardAttacks", exclude=[ap]):
        r = ch.call("OnOtherCardAttacks", c, {"cardAttacking": ap, "defenderCard": dp})
        o = r.get("out") or {}
        stop = stop or bool(o.get("stopAttack"))
        tmp = tmp or bool(o.get("AttackedAndStopped"))
    # ③ StopAttack：直接结束，不付费、不触发后续
    if stop:
        return result("stopped", dp, switched, True, tmp, 0)
    # ④ 付指挥点（在 OnBeforeAttack 之前）
    paid = int(cost)
    ch.order.append(("<pay>", str(paid)))
    # ⑤ 攻击前：自己的 OnBeforeAttack → 0x0D
    if include_own and ap and not ch.suppressed(atk_card):
        ch.call("OnBeforeAttack", attacker, {"defenderCard": dp})
    for c, _fn in ch.others("OnBeforeOtherCardAttacks", exclude=[ap]):
        ch.call("OnBeforeOtherCardAttacks", c, {"cardAttacking": ap, "defenderCard": dp})
    # ⑥ 攻击被吞：OnAttackStopped，然后只剩"指挥点已花"通知
    ch.bucket = "after"
    if tmp:
        if include_own and ap:
            ch.call("OnAttackStopped", attacker, {})
        _kredits_spent(ch, attacker, ap, paid, include_own)
        return result("consumed", 0, switched, False, True, paid)
    # ⑦ 伤害
    ch.bucket = "mid"
    explicit = damage is not None
    if explicit:
        dmg_def = int(damage)
        dmg_att = int(damage_to_attacker or 0)
    else:
        calc_a = ch.calc_dealt(attacker, dfd_card if dfd_card is not None else dp, True)
        calc_d = ch.calc_dealt(dfd_card if dfd_card is not None else dp, attacker, False)
        dmg_def = ch.after_calc(dfd_card if dfd_card is not None else dp, attacker, calc_a["damage"],
                                True, True, False)
        dmg_att = ch.after_calc(attacker, dfd_card if dfd_card is not None else dp, calc_d["damage"],
                                True, False, False)
        shock = shock or calc_d["shock"]
    dmg_final = dmg_def           # `AttackCard.damageToDefenderFinal`：传给 0x04 的是这个值（excess 夹值之前）
    # ⑧ ExecuteAttackCard
    def_total, att_total = _dfn(dfd_card) if dfd_card is not None else 0, _dfn(atk_card)
    dcard = dfd_card if dfd_card is not None else dp
    d_is_loc = ((dfd_card is not None and dfd_card.obj.IsHQ()) or getattr(dfd_card, "card_type", None) == "location"
                or dp in set(ch.hq_enemy) | set(ch.hq_own))
    d_is_unit = getattr(dfd_card, "card_type", None) in _UNIT_TYPES
    excess = 0
    if include_damage_hooks:
        if dmg_def > 0 and not ch.suppressed(dcard):
            ch.receive_damage(dcard, attacker, dmg_def)
        if dmg_att > 0 and not ch.suppressed(atk_card):
            ch.receive_damage(attacker, dcard, dmg_att)
    if _ability(atk_card, kw, "excess") and d_is_unit and def_total and dmg_def > def_total:
        excess = dmg_def - def_total
        dmg_def = def_total
        ch.hits.append({"hook": "<excess>", "name": getattr(atk_card, "name", "?"), "ptr": ap, "side": None,
                        "bucket": "mid", "eff": {"damage_hq": excess}, "stopped": None, "out": {},
                        "records": [], "chance": [], "gaps": []})
        ch.order.append(("<excess>", str(excess)))
    lethal_a = _ability(atk_card, kw, "lethal")
    lethal_d = _ability(dcard, kw, "lethal")
    def_dies = (bool(def_total) and dmg_def >= def_total) or (dmg_def > 0 and lethal_a and not d_is_loc)
    att_dies = (bool(att_total) and dmg_att >= att_total) or (dmg_att > 0 and lethal_d)
    if include_damage_hooks:
        if d_is_unit:
            if not att_dies:
                ch.survived(attacker, dcard)
            if not def_dies:
                ch.survived(dcard, attacker)
        if dmg_def > 0:
            ch.deal_damage_effects(dcard, attacker, dmg_def, False)
        if dmg_att > 0:
            ch.deal_damage_effects(attacker, dcard, dmg_att, True)
    if def_dies and d_is_loc:                       # 总部被打死：对局结束，后面的什么都不再发生
        winner = atk_card.obj.side            # 打死总部的是攻击者那一方（游戏的真实座位）
        return result("match_end", dp, switched, False, False, paid, damage=dmg_def, damage_to_attacker=dmg_att,
                      attacker_destroyed=att_dies, defender_destroyed=True, match_end=winner, excess=excess)
    if include_damage_hooks:
        ch.destroy_combat(atk_card, dcard, att_dies, def_dies)
    # ⑨ 攻击后：自己的 OnAfterAttack → 0x04
    ch.bucket = "after"
    if include_own and ap and not ch.suppressed(atk_card):
        ch.call("OnAfterAttack", attacker, {"defenderCard": dp, "wasShockAttack": bool(shock),
                                            "attackCost": int(cost)})
    for c, _fn in ch.others("OnAfterOtherCardAttacks", exclude=[ap]):
        ch.call("OnAfterOtherCardAttacks", c, {"defenderCard": dp, "attackerCard": ap,
                                               "damageToDefender": int(dmg_final), "attackCost": int(cost)})
    # ⑩ 指挥点已花通知
    _kredits_spent(ch, attacker, ap, paid, include_own)
    return result("resolved", dp, switched, False, False, paid, damage=dmg_def, damage_to_attacker=dmg_att,
                  attacker_destroyed=att_dies, defender_destroyed=def_dies, excess=excess)


def _kredits_spent(ch, operated, op, amount, include_own):
    """`ExecuteOnOperationKreditsSpent(cardOperated, kreditsSpent)`：自己的（不查压制）→ 0x44（排除自己）。"""
    if include_own and op:
        ch.call("OnOperationKreditsSpent", operated, {"kreditsSpent": int(amount)})
    for c, _fn in ch.others("OnOtherCardOperationKreditsSpent", exclude=[op]):
        ch.call("OnOtherCardOperationKreditsSpent", c, {"cardOperated": op, "kreditsSpent": int(amount)})


# --------------------------------------------------------------------------- 转成 boardeval 能吃的效果
# 作用在**某张具体的牌**上的动词（实参 0 = 那张牌）；其它（指挥点/抽牌/槽/召唤…）是全局效果。
def _unit_scoped_verbs() -> frozenset:
    from engine import effectvm as EV
    s = {v for v, t in EV.COUNT_VERBS.items() if t[2] == 0}
    s |= {v for v, t in EV.FLAG_VERBS.items() if t[1] == 0}
    s |= set(EV.GIVE_KW)
    return frozenset(s)


def _merge_eff(a: dict, b: dict) -> dict:
    """把两份效果摘要叠加：数相加、bool 取或、list 拼接、buff 逐项加、chance 取并。"""
    out = dict(a)
    for k, v in (b or {}).items():
        if k == "uncertain":
            continue
        if k not in out:
            out[k] = list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)
        elif k == "buff":
            out[k] = [out[k][0] + v[0], out[k][1] + v[1]]
        elif isinstance(v, bool):
            out[k] = bool(out[k]) or v
        elif isinstance(v, (int, float)) and isinstance(out[k], (int, float)):
            out[k] = out[k] + v
        elif isinstance(v, list) and isinstance(out[k], list):
            out[k] = (out[k] + [x for x in v if x not in out[k]]) if k in ("give", "chance") else out[k] + v
    return out


# 多目标动词：实参 0 是一组卡(id 或对象)，逐个拆成"那张牌上的单牌效果"（boardeval 逐牌消费）
_MULTI = {
    "DamageMultipleCards": lambda a: {"damage": _n(a[1])} if len(a) > 1 else None,
    "DestroyMultipleCards": lambda a: {"destroy": True},
    "SuppressMultipleUnits": lambda a: {"pin": True},
    "RemoveMultipleCardsFromBoard": lambda a: {"remove_unit": True},
    "MoveMultipleCardsToTopOfOwnersDeck": lambda a: {"to_deck": True},
    "AddDefenseToMultipleCards": lambda a: {"buff": [0, _n(a[1])]} if len(a) > 1 else None,
    "MakeCardRetreat": lambda a: {"retreat": True},
}


def _card_maps(st) -> tuple:
    """`(指针 → 卡, card_id → 卡)`：一次建 sim 里每条钩子命中都要它们，而它们只取决于快照（同一个 `cards` 列表对象、同长度）
    ⇒ 在 `readscope.build_scope()` 里只算一遍（原来每条命中都把全盘卡重扫两遍）；作用域外照旧每次现算。"""
    from kardsmem import readscope as _RS
    cards = getattr(st, "cards", None) or []
    sc = _RS.current()
    if sc is not None:
        memo = sc.table("card_maps")
        ent = memo.get(id(cards))
        if ent is not None and ent[0] is cards and ent[1] == len(cards):
            sc.note("card_maps", True)
            return ent[2], ent[3]
        sc.note("card_maps", False)
    ptr2c = {_ptr_of(c): c for c in cards if _ptr_of(c)}
    id2c = {getattr(c, "card_id", None): c for c in ptr2c.values() if getattr(c, "card_id", None) is not None}
    if sc is not None:
        memo[id(cards)] = (cards, len(cards), ptr2c, id2c)
    return ptr2c, id2c


def hit_effects(hit: dict, st, my_side=None, hq_own=(), hq_enemy=()) -> tuple:
    """一次钩子记录 → `(全局效果 dict, {card_id: 该牌上的效果 dict})`。

    按记录里每条写类调用的**目标**拆开：目标是某张场上牌 ⇒ 记到那张牌（buff/伤害/压制/给关键词/…）；
    目标是总部 ⇒ `damage_hq` / `heal_hq`；其余（指挥点/抽牌/槽/召唤/…）⇒ 全局。
    目标认不出（比如随机枚举没落定）⇒ 记进全局并丢掉。`uncertain`（被随机污染的记录）不要。"""
    from engine import effectvm as EV
    scoped = _unit_scoped_verbs()
    ptr2c, id2c = _card_maps(st)
    hq_o, hq_e = set(hq_own), set(hq_enemy)
    glob_recs, per = [], {}
    extra = {}
    for rec in hit.get("records") or ():
        verb, args = rec.get("verb"), rec.get("args") or []
        if verb in _MULTI and args and isinstance(args[0], (list, tuple)):
            if not rec.get("tainted"):
                piece = _MULTI[verb](args)
                for t in args[0]:
                    tc = ptr2c.get(t) or id2c.get(t)
                    if piece and tc is not None and getattr(tc, "card_id", None) is not None:
                        per.setdefault(tc.obj.CardID, []).append({"_piece": piece})
            continue
        if verb == "MakeCardsFight" and len(args) >= 2 and not rec.get("tainted"):
            a_, b_ = (ptr2c.get(args[0]) or id2c.get(args[0])), (ptr2c.get(args[1]) or id2c.get(args[1]))
            if a_ is not None and b_ is not None:
                per.setdefault(a_.obj.CardID, []).append({"_piece": {"damage": _atk(b_)}})
                per.setdefault(b_.obj.CardID, []).append({"_piece": {"damage": _atk(a_)}})
            continue
        if verb in scoped and args:
            t = args[0]
            tc = ptr2c.get(t) or id2c.get(t)
            is_hq = tc is not None and tc.obj.IsHQ()
            if t in hq_e or (is_hq and tc.obj.side != my_side):
                if verb == "DamageCard" and len(args) > 1:
                    extra["damage_hq"] = extra.get("damage_hq", 0) + _n(args[1])
                continue
            if t in hq_o or is_hq:
                if verb == "DamageCard" and len(args) > 1:
                    extra["heal_hq"] = extra.get("heal_hq", 0) - _n(args[1])
                elif verb == "ChangeDefense" and len(args) > 2:
                    extra["heal_hq"] = extra.get("heal_hq", 0) + _n(args[2])
                continue
            if tc is not None and getattr(tc, "card_id", None) is not None:
                per.setdefault(tc.obj.CardID, []).append(rec)
                continue
        glob_recs.append(rec)

    def eff_of(recs):
        r = EV.Recorder(0, 0, None)
        r.records = [x for x in recs if "_piece" not in x]
        r.chance = list(hit.get("chance") or [])
        e = EV.to_effects(r, my_side, None)
        e.pop("uncertain", None)
        for x in recs:                                  # 多目标/对打 展开出来的单牌效果片段
            if "_piece" in x:
                e = _merge_eff(e, x["_piece"])
        return e
    g = _merge_eff(eff_of(glob_recs), extra)
    return g, {cid: eff_of(rs) for cid, rs in per.items()}


_DAMAGE_PIPELINE_HOOKS = frozenset(("OnCardDealDamage_ModifyDamageDealt", "OnOtherCardDealDamageAddDamage",
                                    "OnDealDamageAddDamageAfterCalc", "OnOtherCardDealDamageAddDamageAfterCalc",
                                    "GetPassiveDefenseBuff"))
_DESTROY_BUCKETS = ("def_destroyed", "att_destroyed")


def hit_bucket_effects(hit: dict, st, my_side=None, hq_own=(), hq_enemy=()) -> tuple:
    """一次钩子记录 → `(全局效果, {card_id: 逐牌效果})`，在 `hit_effects` 之上再处理两种特殊记录：
    `<excess>`/`<salvage>` 这类合成命中（效果直接写在 `hit["eff"]` 里）、以及随机枚举过的命中
    （`records` 已清空，效果只在 `hit["eff"]["outcomes"]`）。"""
    g, per = hit_effects(hit, st, my_side, hq_own, hq_enemy)
    if hit.get("hook") in ("<excess>", "<salvage>") or (hit.get("eff") and not hit.get("records")):
        g = _merge_eff(g, hit.get("eff") or {})
    return g, per


def merge_hits(hits, st, my_side=None, hq_own=(), hq_enemy=(), skip_buckets=()) -> dict:
    """把一串命中按 bucket 合并：`{bucket: {"eff": 全局, "units": {card_id: eff}}}`。"""
    buckets = {}
    for h in hits or ():
        if h.get("bucket", "after") in skip_buckets:
            continue
        g, per = hit_bucket_effects(h, st, my_side, hq_own, hq_enemy)
        b = buckets.setdefault(h.get("bucket", "after"), {"eff": {}, "units": {}})
        b["eff"] = _merge_eff(b["eff"], g)
        for k, e in per.items():
            b["units"][k] = _merge_eff(b["units"].get(k, {}), e)
    return buckets


def to_fx(res: dict, st, my_side=None, hq_own=(), hq_enemy=(), dedupe_death: bool = True) -> dict:
    """`run_attack_hooks` 的结果 → boardeval 的 `attack_fx` 条目（格式见 `boardeval.Sim` 的注释）。

    `dedupe_death=True`（缺省）：**摧毁链上的钩子（OnDestroyed/0x27/…）不进 `def_destroyed`/`att_destroyed`
    桶**——`Sim.death_fx`（`rule._death_fx`，按 `death_effects` 预计算）已经在单位死亡时结算它们，
    再放进桶里会**双算**。伤害数值只在"钩子改写了伤害"或调用方显式给了伤害时才输出（否则让
    boardeval 用自己的结算，别拿本模块的移植去覆盖它）。"""
    by_ptr = {_ptr_of(c): c for c in (getattr(st, "cards", None) or []) if _ptr_of(c)}

    def cid(p):
        c = by_ptr.get(p)
        if c is None:
            return None
        return "hq" if c.obj.IsHQ() else getattr(c, "card_id", None)
    own_after = any(h["hook"] == "OnAfterAttack" for h in (res.get("hits") or ()))
    buckets = merge_hits(res.get("hits"), st, my_side, hq_own, hq_enemy,
                         skip_buckets=_DESTROY_BUCKETS if dedupe_death else ())
    use_dmg = bool(res.get("explicit_damage", True)) or any(
        h["hook"] in _DAMAGE_PIPELINE_HOOKS for h in (res.get("hits") or ()))
    out = {"stop": bool(res.get("stop")), "consumed": bool(res.get("attacked_and_stopped")) and not res.get("stop"),
           "switch_to": cid(res["defender"]) if res.get("switched") else None, "paid": res.get("paid"),
           "dmg_def": res.get("damage") if use_dmg else None, "dmg_att": res.get("damage_to_attacker") if use_dmg else None,
           "def_dies": res.get("defender_destroyed") if use_dmg else None,
           "att_dies": res.get("attacker_destroyed") if use_dmg else None,
           "match_end": res.get("match_end"), "own_after": own_after,
           "buckets": {k: {"eff": v["eff"], "units": [(u, e) for u, e in v["units"].items() if e]}
                       for k, v in buckets.items()}}
    return out


_CHAIN_HOOK_TRIGGER_NAMES = None


def present_hooks(km, st, hooks, cache: Optional[TriggerCache] = None) -> set:
    """`hooks` 里**场上/手牌/牌库任一张牌覆写了**的那些（一次性预检：没有覆写就不必跑整条链）。
    走 `find_cards` 的 `指针→类` 与 `类→函数` 两层缓存。"""
    tc = cache or TriggerCache(km)
    out = set()
    for h in hooks:
        if find_cards(km, st, cache=tc, hook=h, on_board_only=False, sides=None):
            out.add(h)
    return out


def build_attack_fx(km, st, pairs, my_side=None, read_hooks=None, budget_s: float = 1.0, stream=None,
                    kw_of=None, hq_own=(), hq_enemy=(), cache=None, total_budget_s: float = 6.0,
                    enum_random: bool = True) -> dict:
    """给一批候选攻击 `[(attacker_card, defender_card_or_"hq_card", cost)]` 各跑一遍钩子链，
    产出 `{(攻击者 card_id, 目标 card_id | "hq"): fx}`，直接喂 `boardeval.from_cards(attack_fx=...)`。
    只收"有后果"的（被吞/换目标/有任何钩子记录）；没后果的不进表（boardeval 走自己的结算）。
    预检：整盘没有任何牌覆写 `ATTACK_HOOK_SPECS` 里的钩子 ⇒ 直接返回 `{}`；总预算 `total_budget_s` 用完后
    剩下的对子**不再算**（并在返回的 `fx_meta` 里记 `skipped`——不编造）。
    返回值是 dict 子类实例，多一个 `.meta`：`{"pairs","computed","skipped","hooked","elapsed_s","draws"}`。"""
    import time as _t
    tc = cache or TriggerCache(km)
    t0 = _t.time()
    out = _FxTable()
    meta = {"pairs": len(pairs), "computed": 0, "skipped": 0, "hooked": [], "elapsed_s": 0.0, "draws": 0}
    out.meta = meta
    present = present_hooks(km, st, ATTACK_HOOK_SPECS, tc)
    meta["hooked"] = sorted(present)
    if not present:
        return out
    for a, d, cost in pairs:
        if _t.time() - t0 > total_budget_s:
            meta["skipped"] += 1
            continue
        r = run_attack_hooks(km, st, a, d, cost=cost, stream=stream, my_side=my_side, read_hooks=read_hooks,
                             budget_s=budget_s, kw_of=kw_of, hq_own=hq_own, hq_enemy=hq_enemy, cache=tc,
                             enum_random=enum_random)
        meta["computed"] += 1
        meta["draws"] += r.get("draws", 0)
        fx = to_fx(r, st, my_side, hq_own, hq_enemy)
        dst = "hq" if (d is not None and d.obj.IsHQ()) else getattr(d, "card_id", None)
        if r["hits"] or fx["stop"] or fx["consumed"] or fx["switch_to"]:
            out[(getattr(a, "card_id", None), dst)] = fx
    meta["elapsed_s"] = round(_t.time() - t0, 3)
    return out


class _FxTable(dict):
    """`build_attack_fx` 的返回值：就是 dict，外加 `.meta`（留痕：算了几对/跳过几对/哪些钩子在场/耗时）。"""
    meta: dict = {}


# --------------------------------------------------------------------------- 钩子族（P6：搬到 triggers_families）
# 死亡链 / 移前线 / 抽牌 / 回合族 / 揭示 / 老兵 / 治疗 / 夺取 / 转化 / 能力变化 / 战斗伤害 / 撤退 的 run_* 与 *_effects。
# 必须放在本模块所有名字都已定义之后：triggers_families 加载时 `from .triggers import ...` 并把自己的名字写回本模块。
from . import triggers_families as _triggers_families  # noqa: E402,F401


# --------------------------------------------------------------------------- selftest（离线）
def selftest() -> int:
    bad = 0

    def chk(name, ok):
        nonlocal bad
        print(("PASS " if ok else "FAIL ") + name)
        bad += 0 if ok else 1

    chk("method_of：打出=1 / 生成=3 / 揭示=4 / 转化=5",
        [method_of(x) for x in ("play_unit", "spawn", "reveal", "convert", "???")]
        == [1, 3, 4, 5, 1])
    chk("METHODS 覆盖 1..5", sorted(METHODS) == [1, 2, 3, 4, 5])

    class _FakeOA:
        def __init__(self, km):
            pass

        def class_of(self, p):
            return 1000 + p

    class _K:                       # 假 km：让 kismet.find_function 走缓存
        pass

    # 假 cache：只有类 1002 有钩子
    class _TC(TriggerCache):
        def __init__(self):
            self._fn = {}

        def fn_of_class(self, uc, hook=HOOK):
            return 7 if (uc == 1002 and hook == HOOK) else 0

    from kardsmem import gamemodel as _GM
    from kardsmem.board import Card as _Card

    def _C(name, loc, p, side=_GM.ESide.left):
        where = {"frontline": _GM.ECardLocation.Board_Frontline, "back": _GM.ECardLocation.Board_HQRight,
                 "hand": _GM.ECardLocation.Hand_Left}[loc]
        o = _GM.BaseCardObject(CardID=p, Type=_GM.EType.infantry, Location=where, side=side, title=name,
                               isSuppressed=False)
        return _Card(uid="t%d" % p, obj=o, raw={"ptr": p})

    st = type("S", (), {})()
    st.cards = [_C("A", "frontline", 1), _C("B", "back", 2, _GM.ESide.right),
                _C("C", "hand", 3), _C("D", "frontline", 4)]

    import sys as _sys
    import types as _types
    import kardsmem.objects as _oa
    old = _oa.ObjectArray
    _oa.ObjectArray = _FakeOA
    try:
        got = find_cards(_K(), st, cache=_TC())
        got_ex = find_cards(_K(), st, exclude=[2], cache=_TC())
    finally:
        _oa.ObjectArray = old
    chk("find_cards：只收场上且挂了钩子的（B 命中，手牌/无钩子不算）",
        [c.name for c, _ in got] == ["B"])
    chk("find_cards：exclude 生效", [c.name for c, _ in got_ex] == [])
    chk("HOOKS 覆盖进场 + 反制三兄弟",
        set(HOOKS) == {HOOK, "OnBeforeOtherCardPlayedFromHand",
                       "OnOtherCardPlayedFromHand", "OnCounterMeasureTriggered"})
    chk("反制族单独成表", tuple(COUNTER_HOOKS) == ("OnBeforeOtherCardPlayedFromHand",
                                               "OnOtherCardPlayedFromHand",
                                               "OnCounterMeasureTriggered"))
    chk("攻击族已登记：触发号 → 形参表（0x04 是 (defender, attacker)，其余是 (attacker, defender)）",
        {"OnOtherCardAttacks", "OnBeforeOtherCardAttacks", "OnAfterOtherCardAttacks",
         "OnOtherCardAttackSwitchTarget", "OnOtherCardReceiveDamage",
         "OnOtherCardOperationKreditsSpent"} <= set(ATTACK_HOOKS)
        and ATTACK_HOOKS["OnAfterOtherCardAttacks"] == ("defenderCard", "attackerCard",
                                                        "damageToDefender", "attackCost")
        and ATTACK_HOOKS["OnBeforeOtherCardAttacks"] == ("cardAttacking", "defenderCard")
        and ATTACK_HOOKS["OnOtherCardAttacks"][0] == "cardAttacking"
        and ATTACK_HOOK_SPECS["OnAfterAttack"]["params"] == ("defenderCard", "wasShockAttack", "attackCost"))
    chk("触发号：0x04/0x0D/0x1E/0x1F/0x34/0x44",
        [ATTACK_HOOK_SPECS[h]["trigger"] for h in ("OnAfterOtherCardAttacks", "OnBeforeOtherCardAttacks",
                                                   "OnOtherCardAttackSwitchTarget", "OnOtherCardAttacks",
                                                   "OnOtherCardReceiveDamage",
                                                   "OnOtherCardOperationKreditsSpent")]
        == [0x04, 0x0D, 0x1E, 0x1F, 0x34, 0x44])
    chk("原生默认：0x1F 两个 false；0x1E newDefender=oldDefender（换 oldDefender 答案跟着变）；"
        "ImplementableEvent 没有原生默认",
        native_default("OnOtherCardAttacks", {}) == {"stopAttack": False, "AttackedAndStopped": False}
        and native_default("OnOtherCardAttackSwitchTarget", {"oldDefender": 5}) == {"newDefender": 5}
        and native_default("OnOtherCardAttackSwitchTarget", {"oldDefender": 9}) == {"newDefender": 9}
        and native_default("OnAfterAttack", {}) == {})
    # 攻击族顺序（字节码）：[自己 OnBeforeAttack → 0x0D → 自己 OnAfterAttack → 0x04 → 自己/0x44 花费通知]
    calls = []

    def _fake_ex(km2, c, hook, args, stream, my_side=None, read_hooks=None, budget_s=1.0,
                 hq_own=(), hq_enemy=(), st=None):
        calls.append((hook, dict(args)))
        return {"ran": True, "eff": {"damage_hq": 2}, "stopped": None, "out": {}, "records": []}

    _g = globals()
    old_find, old_run = find_cards, _run_hook_ex
    _g["find_cards"] = lambda *a, **k: [(_C("W", "frontline", 9), 7)]
    _g["_run_hook_ex"] = _fake_ex
    try:
        atk, dfd = _C("ATK", "frontline", 1), _C("DEF", "frontline", 2)
        r = run_attack_hooks(_K(), st, atk, dfd, damage=3, cost=1)
    finally:
        _g["find_cards"], _g["_run_hook_ex"] = old_find, old_run
    hs = [h for h, _a in calls]
    chk("攻击族顺序：OnBeforeAttack → 0x0D → OnAfterAttack → 0x04 → 花费通知(自己→0x44)",
        [h for h in hs if h in ("OnBeforeAttack", "OnBeforeOtherCardAttacks", "OnAfterAttack",
                                "OnAfterOtherCardAttacks", "OnOperationKreditsSpent",
                                "OnOtherCardOperationKreditsSpent")]
        == ["OnBeforeAttack", "OnBeforeOtherCardAttacks", "OnAfterAttack", "OnAfterOtherCardAttacks",
            "OnOperationKreditsSpent", "OnOtherCardOperationKreditsSpent"])
    a04 = [a for h, a in calls if h == "OnAfterOtherCardAttacks"][0]
    chk("0x04 实参：defenderCard=防守方、attackerCard=攻击方（不能反）、damageToDefender=3、attackCost=1",
        a04 == {"defenderCard": 2, "attackerCard": 1, "damageToDefender": 3, "attackCost": 1}
        and r["outcome"] == "resolved" and r["paid"] == 1)
    a0d = [a for h, a in calls if h == "OnBeforeOtherCardAttacks"][0]
    chk("0x0D 实参名是 cardAttacking/defenderCard（旧版写成 attackerCard ⇒ VM 收不到参数）",
        a0d == {"cardAttacking": 1, "defenderCard": 2})
    print("\n%d 项失败" % bad)
    return bad


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(1 if selftest() else 0)
