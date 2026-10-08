#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.effectvm —— 用外部 Kismet VM 把一张牌的钩子**空跑一遍**，截获「写」类调用做效果摘要。

为什么不再只靠静态数动词（用户 2026-09-30）：
    字节码里出现某个动词 ≠ 这一次会发生（条件在分支里）；数量除非是字节码里的常量，
    否则静态读不出来。VM 用**当前真实局面**把分支走一遍：条件按真实状态成立与否，
    数量按真实状态求值，写类调用被钩子截住、只记录、不执行（红线：读侧只读）。

设计
----
* 读类原语/游戏自有查询：VM 照常执行（`make_get_field` 反射链读内存 + `CardNatives`）。
* 写类动词（`DamageCard`/`GiveKreditsBySide`/`GiveBlitz`/…）：钩子里**记录**
  `(动词, 已求值的实参)`，返回 None。签名来自 SDK（`BP_CardFunctions_classes.hpp`），
  所以实参下标是 API 词汇，不是逐张牌的表。
* **随机**：按真随机对待，**不预测种子**（用户 2026-09-30：RNG 报告讲的是旧版本的重播种机制，
  现行版本又更新过；SpyRing.md 描述的是错误重播种后的表现，别参考）。做法是**枚举**：
  随机原语（`RandomIntFromRangeWithStream`/`GetRandomCard`/`GiveRandomCombatKeyword`）
  在钩子里**按指定结果返回**，对每个可能结果各空跑一次（`enumerate_effects`），各分支等概率，
  调用方取期望。能枚举的：整数范围、数组随机元素、随机战斗关键词（7 个）。
  不能枚举的随机（域未知）只记机会节点并「污染」之后的记录（归入 `uncertain`）。
  VM 自己的随机原语保持拒绝（不替游戏掷骰子）。
* 局限：分支之后依赖「随机结果已经写进游戏状态」的判断（例如 FORGED IN FIRE 的「现在有
  2 个以上关键词就抽一张」）VM 读的是**未更新的真实状态**，会判错；这类牌的枚举结果只是近似。
* 目标：`GetTargetedCard` 钩子喂「当前指向的是谁」。带目标的牌调用方对每个候选目标各跑一次。

诚实的限制
----------
* **没有在真进程上验证过**（写这一版时游戏没开）。VM 覆盖不到的原语会让运行在中途
  `stopped`——此时只保留已记录到的部分，并如实标 `complete=False`；调用方退回静态常量摘要。
* 只挑不判：这里的结果只用来**估值**；出不出、能不能，一律问游戏。

P2（2026-10-03）：本模块自 `semantics/effectvm.py` **原样端口**到 `engine/effectvm.py` ——
`engine.triggers` 的 6 处惰性 import 需要它，而 engine 不许反向依赖 semantics。`semantics/effectvm.py`
是**同一模块对象**的兼容别名（`sys.modules` 替换，理由同 `semantics/triggers.py`）。P4 起它降级为
"记录模式"，真正的执行走 `engine.scripts`。
"""
from __future__ import annotations

import types
from typing import Optional

from kardsmem.gamemodel import ESide, other_side
from kardsmem.kismetlib import Unimplemented
from engine.natives import stats as _stats                       # P3：EChangeType 的语义在 natives/stats.py（单一来源）

# P6：词汇表已拆到 effectvm_tables（纯搬移），这里再导出，`engine.effectvm.<名>` 路径不变。
from engine.effectvm_tables import (                    # noqa: F401,E402
    COUNT_VERBS,
    FLAG_VERBS,
    GIVE_KW,
    SPAWN_MULTI_VERBS,
    SPAWN_ONE_OUT,
    JSON_SET,
    JSON_ARR_SET,
    JSON_ARR_ADD,
    JSON_ARR_DEL,
    JSON_GET,
    JSON_ARR_GET,
    JSON_VERBS,
    SIDE_VERBS,
    END_TURN_VERBS,
    DECK_VERBS,
    SALVAGE_VERBS,
    PIN_VERBS,
    STEAL_VERBS,
    SPAWN_DECK_VERBS,
    REVEAL_VERBS,
    DEFENSE_AOE_VERBS,
    TAX_VERBS,
    RESTRICTION_VERBS,
    ABILITY_VERBS,
    INTEL_VERBS,
    DISCARD_VERBS,
    RECORD_ONLY,
    RANDOM_VERBS,
    IGNORE_VERBS,
    CHOICE_VERBS,
    ALL_HOOKED,
    SET_ENC_FIELDS,
    _SET_ENC_EFFECT_KEY,
    COMBAT_KEYWORDS,
    _INT_RANGE,
    ENUM_CAP,
)




class Recorder:
    """收集一次 VM 空跑期间被截获的写类调用；随机原语按 `forced` 指定的结果返回。

    `forced`：按遇到随机点的先后给出每个点选第几个结果的下标列表；缺省（None）= 探查模式，
    每个点都选 0 号结果并把它的域大小记进 `nodes`，供 `enumerate_effects` 展开。
    """

    def __init__(self, card_ptr: int = 0, target_ptr: int = 0, forced=None):
        self.card_ptr, self.target_ptr = card_ptr, target_ptr
        self.forced = list(forced) if forced is not None else None
        self.records: list = []          # [{verb, args, tainted}]
        self.tainted = False             # 出现过「域未知的随机」⇒ 其后的记录不确定
        self.chance: list = []           # 机会节点的动词名
        self.choice = False              # 出现过「玩家选项」节点 ⇒ 分支取 max 而非期望
        self.nodes: list = []            # 可枚举的随机点 [{"verb","size"}]
        self.stream = None               # kardsmem.rng.Stream：给了活种子 ⇒ 随机点按游戏的 LCG 给**确定**结果，不枚举
        self.exact = False               # 本次空跑里是否用过确定随机
        self.hq_own = frozenset()        # 己方/敌方总部卡指针：对总部的 ChangeDefense 记成回血/伤害
        self.hq_enemy = frozenset()
        self.json = {}                   # 影子 customJson：{(卡, 变量名): 值}（写进这里、读从这里；不回写游戏）
        # ★ 组 D：加密写入叶子的**影子**：{卡指针: {记录名: 明文新值}}；读侧 `_dec_rec/_stat`
        #   经 view 里的 `shadow_stats` 先看到它（同一次空跑内 写→读 一致）。
        self.shadow_stats = {}
        self.cur_stats = {}              # {卡指针: {"attack"/"defense"/"opcost": 当前值}}（SetValue 型改数值要换成增量）
        self.ptr_ids = {}                # {卡指针: card_id}（view 解析时顺带填，给 to_effects 出键用）
        self.json_loader = None          # 惰性读活内存里该卡的 bool 标记：fn(卡) -> {名: bool}
        self._json_loaded = set()
        self.gaps = []                   # 读不到的状态（如 int 型 customJson 的初值）：[说明]
        from engine.spawned import SpawnedCards                        # noqa: PLC0415
        self.spawned = SpawnedCards()    # 脚本里刚生成的牌（临时 id → 合成句柄）；见 engine/spawned.py
        self.live_kw = {}                # {卡指针: [本次空跑里被给予的关键词]}：读侧视图叠加用（见 `_live_overlay`）

    # ---- 随机点的取值 ----
    def _pick(self, verb: str, size: int) -> int:
        size = max(1, min(size, ENUM_CAP))
        k = len(self.nodes)
        self.nodes.append({"verb": verb, "size": size})
        if self.forced is None or k >= len(self.forced):
            return 0
        return max(0, min(self.forced[k], size - 1))

    @staticmethod
    def _write_out(frame, e, idx: int, value) -> None:
        """把值写进调用点第 idx 个 kid 指向的局部变量（出参）。"""
        try:
            name = e.kids[idx].args.get("prop")
            if name and frame is not None:
                frame.locals[name] = value
        except Exception:                                         # noqa: BLE001
            pass

    def _json_init(self, card) -> None:
        """第一次碰这张卡的 customJson 时，把活内存里读得到的 bool 标记装进影子。"""
        if card in self._json_loaded:
            return
        self._json_loaded.add(card)
        if self.json_loader is None:
            return
        try:
            for k, v in (self.json_loader(card) or {}).items():
                self.json.setdefault((card, k), v)
        except Exception:                                         # noqa: BLE001
            self.gaps.append("customJson 初值读不出 (card=%r)" % (card,))

    def _json_hook(self, name, frame, args, e):
        """JSON_* 的影子实现（见 JSON_VERBS 的说明）。出参下标：Set*/Add*/Remove*/Clear 的 found 是最后一个；
        Get* 的 (value, found) 是最后两个。"""
        card = args[0] if args else None
        var = args[1] if len(args) > 1 else None
        key = (card, var)
        n = len(e.kids)
        self._json_init(card)
        if name in JSON_GET or name in JSON_ARR_GET:
            found = key in self.json
            default = [] if name in JSON_ARR_GET else (False if name == "JSON_GetBool"
                                                       else (0 if name == "JSON_GetInt" else ""))
            if not found and name != "JSON_GetBool":
                self.gaps.append("customJson 键 %r 的初值未知（int/string 读不出）" % (var,))
            self._write_out(frame, e, n - 2, list(self.json[key]) if found and name in JSON_ARR_GET
                            else (self.json[key] if found else default))
            self._write_out(frame, e, n - 1, found)
            return None
        if name == "JSON_Clear":
            self.json.pop(key, None)
        elif name in JSON_SET:
            self.json[key] = args[2] if len(args) > 2 else None
        elif name in JSON_ARR_SET:
            self.json[key] = list(args[2]) if len(args) > 2 and args[2] is not None else []
        elif name in JSON_ARR_ADD:
            self.json[key] = list(self.json.get(key) or []) + [args[2] if len(args) > 2 else None]
        elif name in JSON_ARR_DEL:
            cur = list(self.json.get(key) or [])
            if len(args) > 2 and args[2] in cur:
                cur.remove(args[2])
            self.json[key] = cur
        self._write_out(frame, e, n - 1, True)
        return None

    def hook(self, name: str):
        def fn(vm, frame, obj, args, e):
            if name in JSON_VERBS:
                return self._json_hook(name, frame, args, e)
            if name in IGNORE_VERBS:
                return None
            if name in CHOICE_VERBS:
                # 「哪个选项」是玩家选的，不是随机：枚举两个选项（Card_0/Card_1），
                # 调用方对各分支取 **max**（我们自己会选好的那个），不是取平均。
                v = self._pick(name, 2)
                self.choice = True
                self._write_out(frame, e, 0, v)
                return v
            st = self.stream
            if st is not None:
                # ★ 确定随机（2026-10-01，`kardsmem/rng.py`）：只接**走 cardsRandomStream** 的几个原语；
                #   其它随机（FMath 全局流等）仍然枚举/污染。
                if name == "RandomIntFromRangeWithStream":
                    lo = args[0] if len(args) > 0 and isinstance(args[0], int) else 0
                    hi = args[1] if len(args) > 1 and isinstance(args[1], int) else lo
                    v = st.wrapper_int(lo, hi)
                    self.exact = True
                    self._write_out(frame, e, 2, v)
                    return v
                if name == "RandomIntegerInRangeFromStream":
                    # Kismet 库版：args = (Stream, Min, Max)；流参数是游戏自己的 cardsRandomStream/encryptionStream，
                    # 只有前者我们知道种子 —— 参数里的 Stream 读不到身份，保守起见仍枚举
                    pass
                if name == "GetRandomCard":
                    arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
                    if isinstance(args[0] if args else None, (list, tuple)) and not arr:
                        self._write_out(frame, e, 2, 0)           # 空数组：nullptr、不抽（同下方无种子分支的注释）
                        return None
                    if arr:
                        v = arr[st.wrapper_int(0, len(arr) - 1)]
                        self.exact = True
                        self._write_out(frame, e, 2, v)
                        return None
            if name in _INT_RANGE:
                lo = args[0] if len(args) > 0 and isinstance(args[0], int) else 0
                hi = args[1] if len(args) > 1 and isinstance(args[1], int) else lo
                v = lo + self._pick(name, hi - lo + 1)
                self.chance.append(name)
                self._write_out(frame, e, 2, v)              # BP 包装 (min,max,&out) 的出参
                return v                                      # Kismet 库版直接返回值
            if name == "GetRandomCard":
                arr = args[0] if args and isinstance(args[0], (list, tuple)) else []
                if isinstance(args[0] if args else None, (list, tuple)) and not arr:
                    # 空数组 ⇒ `randomCard = nullptr`、**不消耗随机流**（原版：BP_CardFunctions.cpp:5315-5343 `Array_Length > 0` 假支路）
                    # ⇒ 不是随机点（域已知：只有"没有"）；以前落进 `RANDOM_VERBS` 记机会节点并**污染其后记录**（ORP ORZEŁ 因此整张被跳过）。
                    self._write_out(frame, e, 2, 0)
                    return None
                if arr:
                    v = arr[self._pick(name, len(arr))]
                    self.chance.append(name)
                    self._write_out(frame, e, 2, v)
                    return None
            if name == "GiveRandomCombatKeyword":
                # 候选 = 原版 `GiveRandomCombatKeyword`（BP_CardFunctions.cpp:22683）的过滤结果（`engine.natives.combat_kw`）：按目标位置/兵种/
                # 守护剔除烟幕、剔除目标现有关键词、`CanCardBeBuffed` 为假 ⇒ 没有候选。有活流 ⇒ 按牌局流确定地选；否则记成可枚举的随机点。
                valid = self._valid_random_kw(vm, args)
                if valid is None:                                     # 读不出目标视图 ⇒ 退回旧口径（7 选 1 均匀）并记缺口
                    self.gaps.append("GiveRandomCombatKeyword：读不出目标的位置/现有关键词 ⇒ 候选按旧口径 7 选 1（不忠实）")
                    valid = list(COMBAT_KEYWORDS)
                self._write_out(frame, e, 3, bool(valid))
                if not valid:
                    self._write_out(frame, e, 2, 0)
                    return None
                if st is not None:
                    kw = valid[st.wrapper_int(0, len(valid) - 1)]
                    self.exact = True
                else:
                    kw = valid[self._pick(name, len(valid))]
                    self.chance.append(name)
                from engine.natives.combat_kw import COMBAT_KEYWORD_ORDER      # noqa: PLC0415
                self._write_out(frame, e, 2, COMBAT_KEYWORD_ORDER.index(kw) + 1)
                self._live_kw_add(args[0] if args else None, kw)
                self.records.append({"verb": "Give" + kw.capitalize(), "args": list(args),
                                     "tainted": self.tainted, "random_kw": kw})
                return None
            if name in RANDOM_VERBS:
                self.chance.append(name)                      # 域未知：只记，且污染其后记录
                self.tainted = True
                return None
            if name in SET_ENC_FIELDS:
                # 组 D（§5）：`setAndEncryptX(newX, key1, key2, FrameCount)` —— newX 是明文新值。
                # 记进影子（读侧先看它）+ 照旧进 records（日志/追溯）。不碰游戏内存。
                v = args[0] if args and isinstance(args[0], (int, float)) else None
                if obj and v is not None:
                    self.shadow_stats.setdefault(obj, {})[SET_ENC_FIELDS[name]] = int(v)
                self.records.append({"verb": name, "args": list(args), "tainted": self.tainted})
                return None
            if name == "ConvertCard":
                rec_ = {"verb": name, "args": list(args), "tainted": self.tainted}
                try:
                    rec_["conv"] = _convert_payload(vm, args)
                except Exception as ex:                                  # noqa: BLE001
                    self.gaps.append("ConvertCard：取不到目标卡数值（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
                self.records.append(rec_)
                return None
            if name == "MakeVeteran":
                rec_ = {"verb": name, "args": list(args), "tainted": self.tainted}
                try:
                    rec_["vet"] = _veteran_payload(vm, args[0] if args else 0)
                except Exception as ex:                                  # noqa: BLE001
                    self.gaps.append("MakeVeteran：取不到静态卡数值（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
                self.records.append(rec_)
                return None
            if name in SPAWN_MULTI_VERBS:
                # 原版 `SpawnMultipleCardsOnBattlefield`（BP_CardFunctions.cpp:8407-8610）：出参 `spawnedCardIDs`（第 6 个 kid）是新牌 id 列表，
                # 后续 `GiveShock(id)` 等要按它找回刚生成的那张 ⇒ 这里发**临时 id**（负数，与 sim 的临时 id 段错开）；
                # `to_effects` 用它把 Give 并回对应的 spawn_cards 条目。
                names_ = args[2] if len(args) > 2 and isinstance(args[2], (list, tuple)) else []
                ids_ = []
                for _nm in names_:
                    self._spawn_seq = getattr(self, "_spawn_seq", 0) + 1
                    ids_.append(-(7000 + self._spawn_seq))
                    if isinstance(_nm, str) and _nm not in ("", "None"):
                        self.spawned.register(ids_[-1], _nm, args[0] if args else None, "board",
                                              "frontline" if (len(args) > 1 and args[1]) else "back")
                self.records.append({"verb": name, "args": list(args), "tainted": self.tainted, "spawn_ids": ids_})
                self._write_out(frame, e, 5, ids_)
                return None
            if name == "DamageCard" and len(args) > 1 and isinstance(args[0], int) and isinstance(args[1], (int, float)):
                # 出参 `targetDestroyed`（第 7 个 kid）：原版 `_isDestroyed = (getTotalDefense() <= 0)`（BP_CardFunctions.cpp:16468-16472）。
                # 录制路原先不写它 ⇒ BP 里 `if (!targetDestroyed) return` 恒走『没打死』（BREAKTHROUGH："若被摧毁，你的单位本回合行动费 -1"
                # 整段丢掉）。用快照里目标当前总防御算（与直跑 sink 同一判据；改伤钩子/免疫不在这里算，那是 `event_fx["damage"]` 的事）。
                _dfn = (self.cur_stats.get(args[0]) or {}).get("defense")
                if isinstance(_dfn, (int, float)) and args[0] not in self.hq_own and args[0] not in self.hq_enemy:
                    self._write_out(frame, e, 6, float(args[1]) >= float(_dfn))
            if name in SPAWN_ONE_OUT:
                # `SpawnCardOnBattlefield`/`SpawnCardInFrontline` 的末位出参 `spawnedCardID`（BP_CardFunctions.cpp:1121/6417）：
                # ENCIRCLEMENT 之类紧接着 `GiveBlitz(spawnedCardID, …)`。录制路不写它 ⇒ 后面的 Give 拿到 None，
                # `to_effects` 只能把它当"给目标"（字典路 A 给了错的单位）。与 `SPAWN_MULTI_VERBS` 同口径发**临时 id**，
                # `to_effects` 再把 `GiveX(临时 id)` 并回对应的 `spawn_cards` 条目。
                self._spawn_seq = getattr(self, "_spawn_seq", 0) + 1
                tid_ = -(7000 + self._spawn_seq)
                _nm1, _sd1 = (args[2], args[0]) if name == "SpawnCardOnBattlefield" else (args[0], args[1])
                if isinstance(_nm1, str) and _nm1 not in ("", "None"):
                    self.spawned.register(tid_, _nm1, _sd1, "board",
                                          ("frontline" if args[1] else "back") if name == "SpawnCardOnBattlefield" else "frontline")
                self.records.append({"verb": name, "args": list(args), "tainted": self.tainted, "spawn_ids": [tid_]})
                self._write_out(frame, e, SPAWN_ONE_OUT[name], tid_)
                return None
            if name == "SpawnCardInHandBySide":
                # 出参 `spawnedCardID`（下标 9）：脚本随后常 `GetCardFromID(spawnedCardID)` 去改新牌（IRON VICTORY → `ChangeOperationCost`）。
                # 不写 ⇒ 出参恒 None ⇒ `FetchCardFromCardID(None)` 在 `LetBool` 里 TypeError、整条链停下。发一个临时负 id
                # （与 `SpawnMultipleCardsOnBattlefield` 同一段 -(7000+n)，不会和模拟里的牌 id 撞）。
                self._spawn_seq = getattr(self, "_spawn_seq", 0) + 1
                _tid_h = -(7000 + self._spawn_seq)
                self._write_out(frame, e, 9, _tid_h)
                if len(args) > 1 and isinstance(args[1], str) and args[1] not in ("", "None"):
                    self.spawned.register(_tid_h, args[1], args[0] if args else None, "hand")   # 我方/对方由 to_effects 按座位分
                    self.records.append({"verb": name, "args": list(args), "tainted": self.tainted, "spawn_ids": [_tid_h]})
                    return None
            if name in DISCARD_VERBS:
                # `DiscardCardFromHand(cardID, discarderID, skipSubAction, ForceDiscard, bool& success)`（`BP_CardFunctions.cpp:3271`）：
                # 牌在手牌里（`IsLocatedInHand`）且没被限制/没被 `OnAttemptedDiscard` 取消 ⇒ `success=true`（`:3394`/`:3366`）。
                # 脚本靠它计数（IRON VICTORY：`count+1` ⇒ 每弃一张补一张 T-34）；出参不写就恒 false ⇒ 一张都不补（旧缺陷）。
                # 录制时看不到手牌内容 ⇒ 正 id 一律当成功（与直跑 sink 同口径）。
                self._write_out(frame, e, 4, bool(args and isinstance(args[0], int) and args[0] > 0))
            if name in GIVE_KW and args and isinstance(args[0], int):
                self._live_kw_add(args[0], GIVE_KW[name])
            self.records.append({"verb": name, "args": list(args), "tainted": self.tainted})
            return None
        return fn

    def _ptr_of_id(self, cid):
        h = self.spawned.handle_of(cid)
        if h is not None:
            return h
        for p, i in self.ptr_ids.items():
            if i == cid:
                return p
        return None

    def _valid_random_kw(self, vm, args):
        """`GiveRandomCombatKeyword(cardID, …)` 的候选关键词列表（`engine.natives.combat_kw`）；读不出视图 ⇒ None。"""
        from engine.natives.combat_kw import valid_combat_keywords        # noqa: PLC0415
        cid = args[0] if args else None
        ptr = self._ptr_of_id(cid)
        cn = getattr(vm, "cn", None)
        if ptr is None or cn is None:
            return None
        try:
            g = self.cur_stats.get(ptr) or {}
            if g.get("buffable") is False:
                return []
            v = cn._view(ptr)                                             # noqa: SLF001
            nums, _n = cn.call("GetCombatKeywords", ptr)
            names = {1: "ambush", 2: "blitz", 3: "fury", 4: "guard", 5: "heavyarmor", 6: "shock", 7: "smokescreen"}
            existing = {names[k] for k in nums if k in names}
            return valid_combat_keywords(existing, in_frontline=(v.get("location_enum") == 7),
                                         is_fighter=bool(cn.call("IsFighter", ptr)), has_guard=bool(cn.call("getHasGuard", ptr)))
        except Unimplemented:
            return None

    def _live_kw_add(self, cid, kw) -> None:
        """本次空跑里脚本已经给过的关键词（读侧叠加：同一次空跑里 写→读 一致，如 FORGED IN FIRE 给完再数关键词个数）。"""
        ptr = self._ptr_of_id(cid)
        if ptr is not None:
            self.live_kw.setdefault(ptr, []).append(kw)

    def fetch_spawned(self, vm, frame, obj, args, e):
        """软钩子 `FetchCardFromCardID(card_ID_To_Fetch, &fetchedCard)`（BP_GameState_Battle.cpp:1537）：
        id 是脚本刚生成的新牌 ⇒ 回合成句柄（出参在下标 1），其余 id 交还游戏自己的字节码（`FALLTHROUGH`）。"""
        from kardsmem.kismetlib import FALLTHROUGH                      # noqa: PLC0415
        cid = args[0] if args else None
        info = self.spawned.info_of_id(cid)
        if info is None:
            return FALLTHROUGH
        h = info["handle"]
        self.ptr_ids[h] = cid
        self._spawn_gate(h, info)
        self._write_out(frame, e, 1, h)
        return None

    def _spawn_gate(self, handle, info) -> None:
        """新牌句柄的门数据（`CanCardBeBuffed` / `IsUnrevealedCovertCard`）。`spawn_gate` 由 `record_effects` 注入
        （读静态卡判隐蔽）；没注入 ⇒ 不喂（sink 记缺口）。"""
        fn = getattr(self, "spawn_gate", None)
        if fn is not None and handle not in self.cur_stats:
            g = fn(info)
            if g:
                self.cur_stats[handle] = g

    def hooks(self) -> dict:
        h = {n: self.hook(n) for n in ALL_HOOKED}
        h["FetchCardFromCardID"] = self.fetch_spawned
        # 随机战斗关键词落成的动词名（GiveHeavyarmor 没有对应引擎动词 ⇒ 折成重甲 +1）
        return h


def apply_view_override(view: dict, ov: dict) -> dict:
    """卡视图 + 覆盖操作 → 新视图（不改原 dict）。操作见 `record_effects` 的 `view_overrides` 说明。"""
    v = dict(view)
    for k, val in (ov or {}).items():
        if k == "keywords_add":
            ks = list(v.get("keywords") or [])
            v["keywords"] = ks + [x for x in val if x not in ks]
        elif k == "keywords_remove":
            drop = set(val)
            v["keywords"] = [x for x in (v.get("keywords") or []) if x not in drop]
        elif k == "received_remove":
            drop = {str(x).casefold() for x in val}
            rows = v.get("received_abilities")
            if rows is not None:
                v["received_abilities"] = [r for r in rows if str(r.get("ability") or "").casefold() not in drop]
        else:
            v[k] = val
    return v


def _veteran_payload(vm, card_ptr):
    """`MakeVeteran`（BP_CardFunctions@7127）要用的数值，**在录制时**用活卡视图 + `<名>_vet` 静态卡算出来。

    BP 对 attack/defense/operationCost/heavyArmor 各调一次 `ChangeX(card, id, 静态卡的值, veteranSet(5))`，
    `veteranSet` 与 `SetValue` 同一分支 ⇒ **设为绝对值**（防御同时置 maxDefense），buff 不动；
    然后把 ambush/blitz/fury/mobilize/shock/smokescreen/destruction 直接赋成静态卡的值，guard 同理（除非卡自带
    `guard` 自定义能力）。这里只取数，规则落在 `boardeval._apply_eff`。没有 `_vet` 静态卡 ⇒ BP 门槛不过 ⇒ 返回 None。
    """
    cn = getattr(vm, "cn", None)
    if cn is None or not card_ptr:
        return None
    v = cn._view(card_ptr)                                  # noqa: SLF001
    name = v.get("class_fname") or v.get("name")
    if not name:
        raise Unimplemented("MakeVeteran：读不到卡的类名")
    if not cn.static_card:
        raise Unimplemented("MakeVeteran：没有静态卡表")
    vet = cn.static_card(str(name).lower() + "_vet")
    if vet is None:
        return None
    fl = (vet.get("keyword_flags") or {})
    return {
        "atk_to": vet.get("attack_plain"), "atk_from": v.get("attack"),
        "dfn_to": vet.get("defense_plain"),
        "opc_to": vet.get("operation_cost"), "opc_from": v.get("operation_cost"),
        "armor_to": vet.get("total_heavy_armor"), "armor_buff": v.get("heavy_armor_buff") or 0,
        "kw": {k: bool(fl.get("has_" + k)) for k in ("ambush", "blitz", "fury", "mobilize", "shock",
                                                      "smokescreen", "guard")},
    }


def _convert_payload(vm, args):
    """`ConvertCard(cardIDs[], instigatorID, convertToCardName, convertIntoCardID, skipTrigger, &newCardIDs)`
    （BP_CardFunctions@12611）：旧牌离场（不算摧毁）→ 同位置同序号 `CreateCard(目标名)`。
    录制时用静态卡表取新牌的出厂数值；`convertIntoCardID>0`（按某张在场牌的名字转）读不到名字就不结算。"""
    cn = getattr(vm, "cn", None)
    ids = args[0] if args and isinstance(args[0], (list, tuple)) else None
    to_name = args[2] if len(args) > 2 else None
    into_id = args[3] if len(args) > 3 else 0
    if ids is None or cn is None or not cn.static_card:
        raise Unimplemented("ConvertCard：缺 cardIDs / 静态卡表")
    if isinstance(into_id, int) and into_id > 0:
        raise Unimplemented("ConvertCard：convertIntoCardID>0（按在场牌的名字转）暂未支持")
    if not isinstance(to_name, str) or not to_name:
        raise Unimplemented("ConvertCard：目标卡名读不出（%r）" % (to_name,))
    st = cn.static_card(to_name.lower())
    if st is None:
        raise Unimplemented("ConvertCard：静态卡表里没有 %r" % (to_name,))
    fl = (st.get("keyword_flags") or {})
    skip = args[4] if len(args) > 4 else False
    inst = args[1] if len(args) > 1 else 0
    return {"ids": [int(x) for x in ids if isinstance(x, (int, float))], "name": to_name,
            # 原版：ConvertCard 形参（BP_CardFunctions.cpp:12611）——instigatorID 是发起转化的那张牌（0x22 的形参 /
            # 旧牌离场的 instigator），skipTrigger 真则不发 0x22。sim 侧 `event_fx["convert"]` 以它们为键。
            "instigator": int(inst) if isinstance(inst, (int, float)) else 0,
            "skip_trigger": bool(skip),
            "atk": st.get("attack_plain"), "dfn": st.get("defense_plain"),
            "cost": st.get("kredits_plain"), "opc": st.get("operation_cost"),
            "armor": st.get("total_heavy_armor") or 0, "typ": st.get("card_type"),
            "kw": sorted(k for k in ("ambush", "blitz", "fury", "mobilize", "shock", "smokescreen", "guard", "alpine")
                         if fl.get("has_" + k))}


def _who(ptr, rec: "Recorder") -> str:
    if ptr and ptr == rec.target_ptr:
        return "target"
    if ptr and ptr == rec.card_ptr:
        return "self"
    return "other"


#: 会改己方牌库的效果键：抽牌项出现在它们**之前**才算"先抽后改"（见 `to_effects` 的 `draw_pre`）。
_DECK_MUTATORS = ("deck_add", "deck_shuffle", "to_deck", "to_deck_aoe_ids", "to_deck_hand_ids", "steal_to_deck")


def to_effects(rec: Recorder, my_side=None, slots=None, kredits=None) -> dict:
    """录到的调用 → 效果摘要（键的含义见 `boardeval` 的 EFFECT KEYS）。

    确定的写入 `eff`，被随机污染之后的写入 `eff["uncertain"]`，机会节点写入 `eff["chance"]`。
    """
    eff: dict = {}
    unc: dict = {}
    # HQ 卡 id（给 `DamageMultipleCards` 用）：BP 的群体伤害数组里放的是 **card_id**，而
    #   `damage_aoe_ids` 的消费端（sim）只认场上的单位 ⇒ 打到 HQ 的那一份要先转成
    #   damage_hq / damage_own_hq，否则静默丢掉（实机 2026-10-03：BOMBING RAID 打支援线
    #   `单位|单位|HQ` 的中间那只时，对 HQ 的 2 点溅射没算 ⇒ 与打最左那只同分、选错目标）。
    hq_id_own = {rec.ptr_ids[p] for p in rec.hq_own if p in rec.ptr_ids}
    hq_id_enemy = {rec.ptr_ids[p] for p in rec.hq_enemy if p in rec.ptr_ids}
    for r in rec.records:
        dst = unc if r["tainted"] else eff
        verb, args = r["verb"], r["args"]

        def arg(i):
            return args[i] if i is not None and i < len(args) else None

        if verb in _SPAWN_OP_KIND and rec.spawned.info_of_handle(arg(0)) is not None:
            _spawn_op(rec, eff, dst, my_side, r)         # 对脚本刚生成的牌的改动：并回它的 spawn_cards / gain_names 条目
            continue
        if verb in COUNT_VERBS:
            key, ni, wi, si = COUNT_VERBS[verb]
            # ★ 2026-10-04（R1 前半，忠实照做原版的门）：`ChangeOperationCost`(:8906) 与
            #   `ChangeHeavyArmor`(:10332) 的**第一句**是 `IsUnrevealedCovertCard(card)`
            #   —— 真 ⇒ `return`（**什么都不改**，连日志都没有）。我们以前不看这道门 ⇒
            #   对"未揭示的隐蔽牌"也会照改（多算）。这里照原版**什么都不产出**；
            #   不记缺口：这是**已知的不改**（不是"读不到"）。
            #   （`ChangeAttack`/`ChangeDefense`/`ChangeKreditCost` 的门是 `CanCardBeBuffed`，
            #     我们**还没实现**那个原生 —— 另记待办，别在这里假装拦了。）
            if verb in ("ChangeOperationCost", "ChangeHeavyArmor") and \
                    (rec.cur_stats.get(arg(0)) or {}).get("unrevealed_covert"):
                continue
            # 门二：`CanCardBeBuffed`（原版蓝图函数 `:23631`，逐行读过；判据在 `cur_stats["buffable"]`）。
            #   `ChangeAttack`(:10990) / `ChangeDefense`(:11222) **无条件**过这道门；
            #   `ChangeKreditCost`(:10067) 只在 `skipCovertCheck`（签名第 5 参）为假时过。
            #   门为假 ⇒ 原版走 `qqq=false; return`（`:11023` / `:11258`）⇒ **什么都不改**。
            elif verb in ("ChangeAttack", "ChangeDefense") and \
                    (rec.cur_stats.get(arg(0)) or {}).get("buffable") is False:
                continue
            elif verb == "ChangeKreditCost" and not arg(4) and \
                    (rec.cur_stats.get(arg(0)) or {}).get("buffable") is False:
                continue
            n = arg(ni)
            n = n if isinstance(n, (int, float)) else 1
            side = arg(si)
            mine = _is_mine(my_side, side)
            if verb in ("SpawnCardOnBattlefield", "SpawnCardInFrontline"):
                # 生成的是**哪张牌、落在哪一排、谁的**：sim 要按原版 `SpawnCardToBoard` 结算（排满 ⇒ 不生成；用卡自己的面板）。
                #   SpawnCardOnBattlefield(side, _, card_name, …) → 支援线；SpawnCardInFrontline(card_name, side, …) → 前线。
                _nm, _sd = (arg(2), arg(0)) if verb == "SpawnCardOnBattlefield" else (arg(0), arg(1))
                if not (isinstance(_nm, str) and _nm not in ("", "None")):
                    # 卡名是 `None`/空（URAL FACTORIES：同国同费候选池为空 ⇒ `cardToSpawn = "None"`，BP `Random Card` Label_1053）⇒ 没有牌可生成。
                    # 以前照样累加通用 `spawn` 计数 ⇒ 字典路在 `_res_eff` 里凭空造一张 2/2（卡名未知的旧摘要口径）。
                    rec.gaps.append("%s：卡名读不出/为 None ⇒ 不生成" % verb)
                    continue
                if isinstance(_nm, str) and _nm not in ("", "None"):
                    _ent1 = {"name": _nm, "row": ("frontline" if arg(1) else "back") if verb == "SpawnCardOnBattlefield"
                             else "frontline", "mine": _is_mine(my_side, _sd)}
                    if r.get("spawn_ids"):
                        _ent1["tmp_id"] = r["spawn_ids"][0]          # 出参 spawnedCardID 的临时 id（见 SPAWN_ONE_OUT）
                    dst.setdefault("spawn_cards", []).append(
                        # 原版 `SpawnCardonBattlefield(side, Frontline, …)`（BP_CardFunctions.cpp:6417-6446）：`Frontline` 真 ⇒
                        #   `spawnlocation = 0x7`（前线），假 ⇒ 支援线。USS YORKTOWN 就是 `Frontline = !IsLocationFull(前线)`。
                        _ent1)
                if verb == "SpawnCardInFrontline":
                    side = _sd
                    mine = _is_mine(my_side, _sd)
            # `slot` 也要分边：OVERCAST "All players gain an extra kredit slot" = `GainKreditSlot(this, side)` +
            #   `GainKreditSlot(this, 对手)`（card_event_overcast.cpp），原先对方那一次被记到我方 ⇒ 我方 +2、对方 0。
            if key == "draw" and mine and verb in ("DrawCardsFromDeckBySide", "DrawSpecificCardFromDeckBySide"):
                # 抽牌**序列**：字典路 `_apply_eff` 把抽牌放在末尾结算，而原版是**按脚本顺序**（DEFEND THE NATION：先抽再洗牌；
                # SHIFTING DOCTRINE：先洗再抽 2）；`DrawSpecificCardFromDeckBySide` 还指定**抽哪一张**（cardID）。
                # ⇒ `draw_seq` = 按序的抽牌项（`None` = 抽牌库顶、整数 = 指定卡 id）；`draw_pre` = 其中**先于任何牌库改动**的项数
                #   （`_apply_eff` 把这些放在牌库段之前、其余放在之后）。`draw` 计数照旧（给估值/旧消费者）。
                _items = ([arg(1)] if isinstance(arg(1), int) and arg(1) > 0 else [None])                     if verb == "DrawSpecificCardFromDeckBySide" else [None] * (int(n) if isinstance(n, (int, float)) and n > 0 else 0)
                if not any(_k in dst for _k in _DECK_MUTATORS):
                    dst["draw_pre"] = dst.get("draw_pre", 0) + len(_items)
                dst.setdefault("draw_seq", []).extend(_items)
            if verb == "SpawnCardInHandBySide" and mine and isinstance(arg(1), str):
                dst.setdefault("gain_names", []).append(arg(1))        # 新牌的卡名（`_apply_eff` 按名造牌）
                dst.setdefault("gain_tmp", []).append((r.get("spawn_ids") or [None])[0])   # 与 gain_names 同序：出参临时 id（认句柄用）
            if key in ("kredit", "draw", "gain_cards", "spawn", "slot") and not mine:
                dst["opp_" + key] = dst.get("opp_" + key, 0) + n
            elif key == "slot_set":
                cur = (slots or {}).get(_seat(side))
                if cur is not None and isinstance(n, (int, float)):
                    dst["slot" if mine else "opp_slot"] = dst.get("slot" if mine else "opp_slot", 0) + (n - cur)
            elif key == "kredit_set":
                # 绝对值 → 增量（要当前指挥点；不知道就不猜，只留记录）
                cur = (kredits or {}).get(_seat(side))
                if cur is not None and isinstance(n, (int, float)):
                    k2 = "kredit" if mine else "opp_kredit"
                    dst[k2] = dst.get(k2, 0) + (n - cur)
            elif key == "slot_loss":
                dst["slot"] = dst.get("slot", 0) + (-1 if mine else 0)
                if not mine:
                    dst["opp_slot"] = dst.get("opp_slot", 0) - 1
            elif key == "defense" and arg(0) and arg(0) in rec.hq_own:
                dst["heal_hq"] = dst.get("heal_hq", 0) + n          # 给己方总部加防御 = 回血
            elif key == "defense" and arg(0) and arg(0) in rec.hq_enemy and not _stats.is_set_value(arg(3)):
                # 给**敌方**总部加/减防御：原版 `ChangeDefense` 对总部与单位同一条路径（`BP_CardFunctions.cpp:11215-11696`，无 HQ 特判）
                # ⇒ 敌方总部也吃（FOR FREEDOM / IMPERIAL ORDER 等的"打到总部"候选）。以前这里静默丢（字典路没有对应键）。
                dst["heal_opp_hq"] = dst.get("heal_opp_hq", 0) + n
            elif key in ("attack", "defense", "opcost", "armor") and _stats.is_set_value(arg(3)) and isinstance(n, (int, float)):
                # 原版 `ChangeAttack/Defense/OperationCost(…, changeType)`：SetValue(2)/Suppress(3)/veteranSet(5)
                # 是**设成 n**（`BP_CardFunctions.cpp:11068-11093` 的 `Label_855`：写 `clamp(amount,0,99)`），
                # 不是 +n（MONSOON RAIN 2 "把所有单位的攻/防/行动费设为 2"）。换成增量 = n - 当前值。
                # `EChangeType` 的数值与"哪些是设值"由 `engine/natives/stats.py` 单一来源给出（P3 第一族）。
                cur = (rec.cur_stats.get(arg(0)) or {}).get(key) if isinstance(arg(0), int) else None
                if cur is None:
                    rec.gaps.append("%s 为 SetValue 型但读不到目标当前值（ptr=%s）" % (verb, arg(0)))
                else:
                    # ★ P3（R10 的正确修法，2026-10-03）：原版 `SetValue` 写的是**基础字段**
                    #   （`attack` / `operationCost` / `defense`，`BP_CardFunctions.cpp:11081` 等），
                    #   而我们手里的 `cur` 是**总量**（= `clamp(基础 + 独立 buff 累加器)`）。
                    #   ⇒ "要变成的总量" = `clamp(n) + 目标当前 buff 累加器`，增量 = 它 − cur。
                    #   少了 `+ buff` 这一步，就会把目标身上**已经挂着**的 buff 一起抹掉：
                    #   例：5 攻 + 50 点 tempBuff（总量 55）被"设为 2"，原版得 `clamp(2+50)=52`，
                    #   旧实现给 `d = 2 − 55 = −53` ⇒ 总量变成 2（buff 白丢）。
                    #   `defense` 原版**没有**独立 buff 字段 ⇒ `defense_buff` 读不到，`buff=0`（行为不变）。
                    buff = int((rec.cur_stats.get(arg(0)) or {}).get(key + "_buff") or 0)
                    # 目标**总量** = `clamp(设成的基础值 + 目标当前 buff)`：攻/防/行动费夹 `[0,99]`，
                    # 重甲夹 **`[0,3]`**（原版 `ChangeHeavyArmor` 的 `Label_1433` 是 `clamp(amount,0,3)`；
                    # IDA `getTotalHeavyArmor` 也是 `clamp(0,3)`）—— `P3 R12`。
                    # 再换算成"相对当前总量的增量"（消费侧是 `total += d`）。
                    _cl = _stats.clamp_armor if key == "armor" else _stats.clamp_stat
                    d = _cl(_cl(int(n)) + buff) - cur
                    if key == "opcost":
                        dst["opcost"] = dst.get("opcost", 0) + d
                    elif key == "armor":
                        dst["armor"] = dst.get("armor", 0) + d
                    else:
                        b = dst.setdefault("buff", [0, 0])
                        b[0 if key == "attack" else 1] += d
                        tcid = rec.ptr_ids.get(arg(0))
                        if tcid is not None:
                            row = dst.setdefault("buff_ids", {}).setdefault(int(tcid), [0, 0])
                            row[0 if key == "attack" else 1] += d
            elif key in ("attack", "defense"):
                b = dst.setdefault("buff", [0, 0])
                b[0 if key == "attack" else 1] += n
                # 逐张记账：群体 buff（"给你所有单位 +3+2"）要知道**每张**牌各加多少；标量 `buff` 只够单目标
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is not None and isinstance(n, (int, float)):
                    bi = dst.setdefault("buff_ids", {})
                    row = bi.setdefault(int(tcid), [0, 0])
                    row[0 if key == "attack" else 1] += n
            elif key == "armor" and isinstance(n, (int, float)) and not _stats.is_set_value(arg(3)):
                # 群体重甲（MEN OF STEEL："你的单位 +1 重甲"）：标量 `armor` 只够单目标 ⇒ 同 `opcost_ids` 逐张记账 `armor_ids`
                # （sim 逐张 `clamp_armor(armor+n)`，范围 `[0,3]`）。
                dst["armor"] = dst.get("armor", 0) + n
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is not None:
                    ri = dst.setdefault("armor_ids", {})
                    ri[int(tcid)] = ri.get(int(tcid), 0) + n
            elif key == "opcost" and isinstance(n, (int, float)) and not _stats.is_set_value(arg(3)):
                # 群体行动费变化（HEATWAVE 2：每个己方单位 -1）：标量只够单目标 ⇒ 同 `attack_turn_ids` 逐张记账 `opcost_ids`
                dst["opcost"] = dst.get("opcost", 0) + n
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is not None:
                    oi = dst.setdefault("opcost_ids", {})
                    oi[int(tcid)] = oi.get(int(tcid), 0) + n
            elif key == "attack_turn" and isinstance(n, (int, float)):
                # `AddAttackUntilEndOfTurn(card, instigator, n)`：标量 `attack_turn` 只够单目标；群体（HEATWAVE：
                #   "所有单位 +2 攻，空军 +3"——逐单位数值不同）要**逐张记账**，同 `buff_ids` 的口径 ⇒ `attack_turn_ids`。
                dst["attack_turn"] = dst.get("attack_turn", 0) + n
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is not None:
                    ai = dst.setdefault("attack_turn_ids", {})
                    ai[int(tcid)] = ai.get(int(tcid), 0) + n
            elif key == "cost" and isinstance(n, (int, float)):
                # ★ P3（2026-10-03）：**`ChangeKreditCost` 改的是手牌那张牌的费用**
                #   （BP_CardFunctions.cpp:10067；`Label_758` 的 set 家族 = `setAndEncryptKredit(clamp(amount,0,99))`，
                #   它比的是**基础**字段 `getAndDecryptKredit`，不是总量）。
                #   而标量 `cost` + `_apply_eff(target)` 那个模型根本套不上（target 是场上单位/总部、
                #   手牌不是 target）—— 这正是这个键**一直没有消费者**、效果被整个丢掉的原因。
                #   所以发**逐卡字典** `cost_ids: {card_id: 增量}`，由 `sim` 落到 `Hand.cost`。
                #   设值家族按统一口径换算："目标总量 = clamp(clamp(n) + 目标当前 buff)"，再减当前总量。
                tcid = rec.ptr_ids.get(arg(0)) if isinstance(arg(0), int) else None
                if tcid is None or not isinstance(tcid, int):
                    rec.gaps.append("ChangeKreditCost：目标指针认不出 card_id（ptr=%s）" % arg(0))
                else:
                    _ct = arg(3)
                    if _ct in _stats.KREDIT_COST_SET_FAMILY:
                        cur = (rec.cur_stats.get(arg(0)) or {}).get("cost")
                        if cur is None:
                            rec.gaps.append("ChangeKreditCost 为 SetValue 型但读不到目标当前值（ptr=%s）" % arg(0))
                        else:
                            buff = int((rec.cur_stats.get(arg(0)) or {}).get("cost_buff") or 0)
                            d = _stats.clamp_stat(_stats.clamp_stat(int(n)) + buff) - cur
                            dst.setdefault("cost_ids", {})
                            dst["cost_ids"][int(tcid)] = dst["cost_ids"].get(int(tcid), 0) + d
                    elif _ct in _stats.SET_VALUE_FAMILY or _ct in _stats.AFTER_EVENT_FAMILY:
                        # 原版 `Label_1614`：ct `5` 与 `6-9` **不改数值**，只 `NotifySetKreditCost`
                        # 与 0x2D 通知 ⇒ 没有"状态变更"可记（别在这儿编一个增量）。
                        pass
                    else:
                        dst.setdefault("cost_ids", {})
                        dst["cost_ids"][int(tcid)] = dst["cost_ids"].get(int(tcid), 0) + int(n)
            elif key == "damage":
                tgt = arg(wi)
                # ★ 2026-10-02：`DamageCard` 的目标可能是**总部卡**（7th SCOTTISH BORDERERS /
                #   "对敌方 HQ 造成 N 伤"那类）。以前一律记成 `damage`（当单位伤害），
                #   于是打总部的效果被算成"打一个敌方单位"。用 hq_own/hq_enemy 指针集区分。
                if tgt and tgt in rec.hq_enemy:
                    dst["damage_hq"] = dst.get("damage_hq", 0) + n
                elif tgt and tgt in rec.hq_own:
                    # §15.5 拆键：打**己方 HQ** ⇒ damage_own_hq（boardeval 里 hq[LOCAL] -= n）；
                    # 打"自己这张牌"（刚部署的牌）⇒ self_damage（该单位此刻不在 sim 里，丢弃）。
                    dst["damage_own_hq"] = dst.get("damage_own_hq", 0) + n
                else:
                    w = _who(tgt, rec)
                    if w == "self":
                        dst["self_damage"] = dst.get("self_damage", 0) + n
                    elif w == "other" and isinstance(tgt, int) and isinstance(n, (int, float))                             and rec.ptr_ids.get(tgt) is not None:
                        # 目标不是玩家选的那张、也不是自己、也不是总部，而且认得出是**哪张**（RUSH："对一个友方单位 1 伤 + 对随机敌方单位 4 伤"；
                        # RASPUTITSA：每个单位吃一份、数值各不相同）⇒ 逐张记账 `damage_ids`（同 `buff_ids` 口径），否则标量 `damage`
                        # 只能套到"选定目标"上（A 把 4 点打到了友方目标、或在无目标时被 rule 当『范围』整个丢掉 ⇒ 与直跑恒分歧）。
                        _di = dst.setdefault("damage_ids", {})
                        _di[int(rec.ptr_ids[tgt])] = _di.get(int(rec.ptr_ids[tgt]), 0) + n
                    else:
                        dst["damage"] = max(dst.get("damage", 0), n)
            elif key == "damage_aoe":
                # ★ NATIVE-COVERAGE §15.5：群体伤害保留 id 列表（TArray<int32> 的卡 id，
                #   与 defense_aoe_ids 同口径），boardeval 才能逐张结算。
                # ★ 2026-10-03：列表里可能有**总部卡的 card_id**（BOMBING RAID 的相邻溅射）——
                #   按 HQ 卡 id 拆成 damage_hq / damage_own_hq，剩下的才进 damage_aoe_ids。
                ids = arg(wi)
                if isinstance(ids, (list, tuple)):
                    keep = []
                    for x in ids:
                        if not isinstance(x, (int, float)):
                            continue
                        xi = int(x)
                        if xi in hq_id_enemy:
                            dst["damage_hq"] = dst.get("damage_hq", 0) + n
                        elif xi in hq_id_own:
                            dst["damage_own_hq"] = dst.get("damage_own_hq", 0) + n
                        else:
                            keep.append(xi)
                    dst["damage_aoe_ids"] = keep
                dst[key] = dst.get(key, 0) + (n if key not in ("spawn", "gain_cards")
                                              else (n if n and n > 1 else 1))
            else:
                dst[key] = dst.get(key, 0) + (n if key not in ("spawn", "gain_cards") else (n if n and n > 1 else 1))
        elif verb in FLAG_VERBS:
            key, wi = FLAG_VERBS[verb]
            if key == "heal_unit":
                key = "heal"
            if key == "fight":
                # ★ NATIVE-COVERAGE §15.5：`MakeCardsFight(unitThisSide, unitOppositeSide,
                #   instigatorID)` 的**前两个实参是卡指针**（不是 card_id）⇒ 用 view 顺带填的
                #   `rec.ptr_ids`（与 set_* 出键同一张表）换 id；换不出就**别假装**，
                #   保留布尔并记缺口（boardeval 侧只会记 gap、不结算）。
                ids = []
                for i in (0, 1):
                    p = arg(i)
                    cid = rec.ptr_ids.get(p) if isinstance(p, int) else None
                    if cid is not None:
                        ids.append(int(cid))
                if len(ids) == 2:
                    if "fight" in dst:
                        # 同一张牌里不止一次对打（COUNTERATTACK <german>：每个友方单位各对一个随机敌方单位）：`fight` 只有一格，后一次会覆盖前一次
                        # ⇒ 另记**有序的对列表** `fights`（含之前那一对），消费侧逐对结算（前面的击杀会让后面的对子『不在场』而不结算）。
                        dst.setdefault("fights", [list(dst["fight"])]).append(ids)
                    dst["fight"] = ids
                    continue
                rec.gaps.append("MakeCardsFight：两个卡指针换不出 card_id（ptr_ids 缺），只记布尔")
            if key == "retreat" and isinstance(arg(0), (list, tuple)):
                # MakeCardRetreat(Cards[], …)：对**一组**牌的群体撤退（SEABORNE INVASION：敌方前线所有单位）
                # 数组里的是**具体哪几张**（TROPICAL STORM 2：“撤退一个随机敌方单位”只有 1 张，随机已由活种子定了）——
                # 换成 card_id 记下来，sim 只撤这几张。
                # ★ 2026-10-04（P3 ⑤）：原版 `MakeCardRetreat`（`:1709-1754`）先逐张
                #   `HasCustomAbility("cantRetreat")` **跳过**（`:1727-1735`），再把剩下的按
                #   支援线/前线分两批调 `ApplyMakeCardRetreat`（`:1742`/`:1754`）。
                #   我们以前不看这道门 ⇒ 把带 `cantRetreat` 的牌也撤了（**多撤**）。
                _arr = [p for p in arg(0)
                        if not (rec.cur_stats.get(p) or {}).get("cant_retreat")]
                if not _arr:
                    continue          # 全被 `cantRetreat` 挡下 ⇒ 原版什么都不做（不是缺口，别记）
                _rids = [rec.ptr_ids.get(p) for p in _arr if isinstance(p, int)]
                if _rids and all(x is not None for x in _rids) and len(_rids) == len(_arr):
                    dst["retreat_ids"] = [int(x) for x in _rids]
                else:
                    # 不再退回“敌方前线全撤”的兜底（那是编的）：换不出 id 就如实记缺口，sim 不结算
                    rec.gaps.append("MakeCardRetreat：数组里的卡指针换不出 card_id（ptr_ids 缺），撤退未结算")
                continue
            if key in ("destroy_aoe", "pin_aoe", "suppress_aoe", "remove_aoe", "to_deck_aoe") \
                    and isinstance(arg(0), (list, tuple)):
                # NATIVE-COVERAGE §15.5：群体动词保留 id 列表，boardeval 才能逐张 pop/pin/放回牌库；
                #   `to_deck` 另记 positionFromTop。
                # ★★ 2026-10-04（P4 第十八刀）**撤回第一轮的"修法"并留档**：我当时看到实参是 1280/1281，
                #   断定"这是卡指针、消费端按 card_id 查 ⇒ 群体效果从来没生效"，于是加了 `ptr_ids` 映射。
                #   **那个"证据"是我自己的测试夹具喂进去的**（我手写 `[0x500, 0x501]`，1280 恰好是它也像 id ✗）。
                #   真实语义**从 BP 签名与调用点看**：这些都是 `const TArray<int>*&`（**card ID**）——
                #   例 `SuppressUnit(int cardID, …)` 的实现就是 `MakeArray_Array = [ cardID ]; SuppressMultipleUnits(MakeArray_Array, …)`
                #   （`BP_CardFunctions.cpp:7446-7450`）⇒ **数组里本来就是 id**。
                #   ⇒ 映射反而把**真 id** 映成 `None`、记缺口、跳过效果 —— 我引入的是**回归** ✗。
                #   现在恢复"原样传入"；只有 `MakeCardRetreat` 那族是**指针数组**（签名 `TArray<UBaseCardObject*>`
                #   ✓，见它自己那个分支），才需要映射 ✓。
                dst[key + "_ids"] = [int(x) for x in arg(0)
                                     if isinstance(x, (int, float))]
                if key == "to_deck_aoe":
                    pos = arg(2)
                    if isinstance(pos, (int, float)):
                        dst["to_deck_position"] = int(pos)
            if key == "destroy" and isinstance(arg(0), int) and _who(arg(0), rec) == "other"                     and arg(0) not in rec.hq_own and arg(0) not in rec.hq_enemy:
                # 不是玩家选的目标、也不是自己、也不是总部 ⇒ 是脚本自己挑的那张（DEATH FROM ABOVE：随机敌方单位，随机已由
                #   活种子算成确定结果）。裸 `destroy=True` 只能套到"选定目标"上、没目标就被 rule 当"范围/随机目标"剥掉 ⇒ 字典路
                #   什么都不做（A/B 恒分歧）。改记**具体哪张**（同群体摧毁的 `destroy_aoe_ids` 口径，sim 已有消费者）。
                _tcid = rec.ptr_ids.get(arg(0))
                if _tcid is not None:
                    dst.setdefault("destroy_aoe_ids", []).append(int(_tcid))
                    continue
            if key in ("pin", "heal"):
                # 受影响的不是玩家选的那张 ⇒ 逐张记账（`pin_ids`/`heal_ids`，同 `buff_ids` 口径），否则标量 `pin`/`heal` 只能套到选定目标上：
                #   MONTY / EXHAUST ALL OPTIONS "定住目标**及相邻单位**"（`PinUnit(cardID,…)` 收 **ID**）、CADET NURSE CORPS "完全修复**所有**友方单位"
                #   （`FullyHealCard(card,…)` 收**指针**）——字典路原先只定住/修复选定目标（或在无目标时整个丢掉）。
                _a0 = arg(0)
                _cid0 = (_a0 if key == "pin" else rec.ptr_ids.get(_a0)) if isinstance(_a0, int) else None
                _tcid = rec.ptr_ids.get(rec.target_ptr) if rec.target_ptr else None
                if _cid0 is not None and _cid0 != _tcid and _cid0 in set(rec.ptr_ids.values())                         and _cid0 not in hq_id_own and _cid0 not in hq_id_enemy:
                    _l = dst.setdefault(key + "_ids", [])
                    if int(_cid0) not in _l:
                        _l.append(int(_cid0))
                    continue
            dst[key] = True
            if key == "to_deck" and isinstance(arg(0), int) and arg(0) > 0:
                # `MoveCardToTopOfOwnersDeck(int cardID, instigatorID, positionFromTop)`：**哪一张**手牌（SHIFTING DOCTRINE：随机一张，随机已由活种子定了）。
                # 只留 `to_deck=True` 时字典路在没有手牌目标的情形下什么都不做（效果整个丢掉）。`_apply_eff` 只认**在手牌里**的 id（场上单位仍走原路）。
                dst.setdefault("to_deck_hand_ids", []).append(int(arg(0)))
                if isinstance(arg(2), (int, float)) and not isinstance(arg(2), bool):
                    dst["to_deck_position"] = int(arg(2))
            if key == "convert" and r.get("conv"):
                dst[key] = dict(r["conv"])
            if key == "veteran" and r.get("vet"):
                dst[key] = dict(r["vet"])               # 带数值：boardeval 按 BP 改写（无数值 ⇒ 只打标记+缺口）
            if key == "end_match":                      # EndMatch(winnerSide, delay)：谁赢
                sd = arg(0)
                if sd in (1, 2):
                    dst[key] = _seat(sd)
        elif verb in SIDE_VERBS:
            sd = arg(0)
            if sd in (1, 2):
                dst["playing_side"] = _seat(sd)
        elif verb in END_TURN_VERBS:
            # 强制结束当前回合 ⇒ 行动权交给对方（boardeval 之后不再产生我方动作）
            dst["playing_side"] = other_side(ESide(_need_seat(my_side)))
        elif verb in DECK_VERBS:
            sd = arg(0)
            if verb == "ShuffleDeckBySide" and sd in (1, 2):
                dst["deck_shuffle"] = _seat(sd)
                dst["deck_shuffles"] = dst.get("deck_shuffles", 0) + 1       # 洗几次（每次都耗随机流）
                # ★ 2026-10-02：`ShuffleDeckBySide(side, skipSubAction, instigatorID, &qqq)` ——
                #   BP 里 **只有 `skipSubAction=true`** 才 `FetchAllCardsWithEventTrigger(0x16)`
                #   → `OnDeckShuffled`（SABAE REGIMENT 那类"洗牌时"的钩子）；false 只发
                #   NotifyNewDeck。卡牌自己洗大多传 false，`SpawnCardInDeckBySide(shuffle=true)`
                #   传 true ⇒ 记下这个标志给 rule 的 0x16 触发用（以游戏实现为准）。
                sk = arg(1)
                if isinstance(sk, (bool, int)):
                    dst["deck_shuffle_skip"] = bool(sk)
        elif verb in SALVAGE_VERBS:
            # TArray 实参在 VM 里是 list；单元素版本（如果有）也可能直接给一个 id。
            # ★★ 2026-10-04（P4 第十八刀）**撤回第十七刀的"修法"**：`SalvageMultipleUnits(const TArray<int>*&
            #   cardsToSalvage, …)` 签名就是 **card ID** 数组（BP 逐字）⇒ 原样传入本来就是对的；
            #   我加的 `ptr_ids` 映射会把真 id 映成 `None` ⇒ 记缺口、效果被跳过（回归 ✗）。见 aoe 那段的留档。
            ids = arg(0)
            if isinstance(ids, (list, tuple)):
                got = [int(x) for x in ids if isinstance(x, (int, float)) and int(x) > 0]
            elif isinstance(ids, (int, float)) and int(ids) > 0:
                got = [int(ids)]
            else:
                got = []
            if got:
                dst.setdefault("salvage_ids", []).extend(got)
        elif verb in PIN_VERBS:
            n = arg(2)                                # ChangedPinnedTurns(cardID, instigatorID, turnsToChange)
            if isinstance(n, (int, float)) and n:
                dst["pin_turns"] = int(n)
        elif verb in STEAL_VERBS:
            sd = arg(2)                               # StealCardFromBoardToDeck(cardID, instigatorID, deckSide)
            if sd in (1, 2):
                side = _seat(sd)
                dst["steal_to_deck"] = side
                dst["deck_shuffle"] = side            # BP 末尾必然 ShuffleDeckBySide
                dst["deck_shuffles"] = dst.get("deck_shuffles", 0) + 1
        elif verb in SPAWN_DECK_VERBS:
            # SpawnCardInDeckBySide(side, card_name, spawnerID, n, salvageFaction,
            #                       HideFromOpponent, bottom, shuffle, SkipDrawAnimation, RandomWithoutShuffle, &ids)
            sd, n = arg(0), arg(3)
            if sd in (1, 2) and isinstance(n, (int, float)) and n > 0:
                dst["deck_add"] = dst.get("deck_add", 0) + int(n)         # 累加：一张牌可以塞好几次（COLOSSUS 循环 3 次，以前被覆盖成 1）
                dst["deck_add_side"] = _seat(sd)
                # 逐次的塞牌（卡名/张数/位置口径各自不同）：`_apply_eff` 有它就按序逐条结算，没有才用下面的单条旧键
                dst.setdefault("deck_adds", []).append(
                    {"name": arg(1) if isinstance(arg(1), str) else None, "n": int(n), "bottom": bool(arg(6)),
                     "wo_shuffle": bool(arg(9)), "shuffle": bool(arg(7))})
                nm = arg(1)
                if isinstance(nm, str) and nm:
                    dst["deck_add_name"] = nm
                dst["deck_add_bottom"] = bool(arg(6))
                dst["deck_add_shuffle"] = bool(arg(7))
                dst["deck_add_wo_shuffle"] = bool(arg(9))
                # ★ 2026-10-02（`+shuffled` 一直不亮的真因之一）：`shuffle=true` 时 BP 内部
                #   走的是 `ShuffleDeckBySide(side, /*skipSubAction=*/true, …)` ⇒ 会
                #   `FetchAllCardsWithEventTrigger(0x16)`。以前这里只记 `deck_add_shuffle`，
                #   而 `rule._deck_shuffled_triggers` 的闸门看的是 `deck_shuffle` +
                #   `deck_shuffle_skip` ⇒ 塞牌并洗这条**永远不触发**（DUG IN 这种卡白打）。
                #   以游戏实现为准：shuffle=true ⇒ 同时记成一次 skip 洗牌。
                if bool(arg(7)):
                    dst["deck_shuffle"] = _seat(sd)
                    dst["deck_shuffle_skip"] = True
                    # ★ 洗牌**次数**：`SpawnCardInDeckBySide(shuffle=true)` 在函数体里**自己洗一次**（BP `:6648` `Label_921` →
                    #   `ShuffleDeckBySide(side, true, spawnerID)`）；`deck_add_shuffle` 在 `_apply_eff` 的塞牌段里已经洗过这一次，
                    #   所以这里记进 `deck_shuffles` 只为让"**另外**再洗了几次"能算（`_apply_eff` 会扣掉塞牌段那一次）。
                    #   以前 `deck_add_shuffle` 洗一次、`deck_shuffle` 又洗一次 ⇒ 字典路**洗两遍**（DUG IN 对账 deck 不同的根因）。
                    dst["deck_shuffles"] = dst.get("deck_shuffles", 0) + 1
        elif verb in REVEAL_VERBS:
            dst["reveal"] = True
        elif verb in DEFENSE_AOE_VERBS:
            # AddDefenseToMultipleCards(receiverIDs[], amount, giverCardID, &qqq)
            # ★★ 2026-10-04（P4 第十八刀）**撤回本刀刚加的"修法"**：签名是 `const TArray<int>*& receiverIDs`
            #   （**card ID**）⇒ 原样传入是对的；映射会把真 id 映成 `None` ⇒ 群体加减防被跳过（回归 ✗）。
            #   留档见 aoe 那段。
            ids, amt = arg(0), arg(1)
            if isinstance(ids, (list, tuple)) and isinstance(amt, (int, float)):
                dst["defense_aoe"] = int(amt)
                dst["defense_aoe_ids"] = [int(x) for x in ids if isinstance(x, (int, float))]
        elif verb in TAX_VERBS:
            n = arg(1)                                # AddKreditsTax(card, costToAdd, instigatorID, &qqq)
            if isinstance(n, (int, float)) and n:
                dst["kredits_tax"] = dst.get("kredits_tax", 0) + int(n)
        elif verb in ABILITY_VERBS:
            # CustomAbilityAdd(ability, cardID, giverID, …)：`cardID`/`giverID` 是**卡 ID**（BP 签名）。门 = `CanCardBeBuffed`（同直跑 sink）。
            _nm, _cid, _gv = arg(0), arg(1), arg(2)
            if isinstance(_nm, str) and isinstance(_cid, int):
                if (rec.cur_stats.get(rec._ptr_of_id(_cid)) or {}).get("buffable") is False:
                    continue
                dst.setdefault("ability_grants", []).append([_nm, int(_cid), int(_gv) if isinstance(_gv, int) else None])
        elif verb in RESTRICTION_VERBS:
            # Add/RemoveGameplayRestriction(side, type, cardID[, removeAll], [&qqq])
            sd, ty, turns = arg(0), arg(1), arg(3)
            if sd in (1, 2) and isinstance(ty, (int, float)):
                side = _seat(sd)
                entry = {"side": side, "type": int(ty),
                         "turns": int(turns) if isinstance(turns, (int, float)) else 0}
                key = "restriction_add" if verb == "AddGameplayRestriction" else "restriction_remove"
                dst.setdefault(key, []).append(entry)
        elif verb in INTEL_VERBS:
            # SetCardsSeenByCipher(numberOfCardsSeen, instigatorID, side, &qqq)
            n = arg(0)
            if isinstance(n, (int, float)) and n > 0:
                dst["intel_seen"] = int(n)
        elif verb in DISCARD_VERBS:
            # ★★ 2026-10-04（P4 第十八刀）**撤回第十七刀的"修法"**：`DiscardCardFromHand(int cardID, …)`
            #   签名就是 **card ID**（BP 逐字）⇒ 原来 `append(arg(0))` 本来就是对的；
            #   我加的映射会把真 id 映成 `None` ⇒ 弃牌被跳过（回归 ✗）。见 aoe 那段的留档。
            if arg(0) is not None:
                dst.setdefault("discard_ids", []).append(int(arg(0)))
        elif verb in SPAWN_MULTI_VERBS:
            # 逐个名字一条 spawn_cards（落点/排满判定同 `SpawnCardOnBattlefield`）；`giveBlitz` 实参 ⇒ 每张带 blitz（BP `GiveBlitz(cardSpawned)`）
            _names = arg(2) if isinstance(arg(2), (list, tuple)) else []
            for _i, _nm in enumerate(_names):
                if isinstance(_nm, str) and _nm not in ("", "None"):
                    _ent = {"name": _nm, "row": "frontline" if arg(1) else "back", "mine": _is_mine(my_side, arg(0))}
                    if arg(4):
                        _ent["kw_add"] = ["blitz"]
                    _tid = (r.get("spawn_ids") or [None] * len(_names))[_i]
                    if _tid is not None:
                        _ent["tmp_id"] = _tid
                    dst.setdefault("spawn_cards", []).append(_ent)
        elif verb in GIVE_KW and arg(0) is not None and any(
                x.get("tmp_id") == arg(0) for x in (dst.get("spawn_cards") or ())):
            # `GiveX(刚生成那张的临时 id)`：并进该 spawn_cards 条目（字典路没有"对刚生成的单位授予"这条通道，旧口径会把它丢给 target）
            for _x in dst["spawn_cards"]:
                if _x.get("tmp_id") == arg(0) and GIVE_KW[verb] not in _x.setdefault("kw_add", []):
                    _x["kw_add"].append(GIVE_KW[verb])
        elif verb in GIVE_KW:
            _g0 = arg(0)                              # `GiveX(int cardID, int instigatorID, …)`：首参是**收到关键词的那张牌的 ID**
            _tcid = rec.ptr_ids.get(rec.target_ptr) if rec.target_ptr else None
            if isinstance(_g0, int) and (_g0 in hq_id_own or _g0 in hq_id_enemy):
                # 授予的是**总部卡**（HEL：`GiveImmune(己方总部卡 id, 目标 id)`——"你的总部免疫伤害"）：模拟里总部是个数不是牌，没有关键词位
                # ⇒ 如实记缺口，**不**把 immune 套到选定目标单位上（旧口径套错了对象）。
                rec.gaps.append("%s：授予对象是总部卡（总部的关键词状态没建模）" % verb)
            elif isinstance(_g0, int) and _g0 != _tcid and _g0 in set(rec.ptr_ids.values()):
                # 授予的不是玩家选的那张（ADMIRAL YAMAMOTO：所有友方空军 +1 攻 + 闪击）⇒ 逐张记账 `give_ids`
                _gi = dst.setdefault("give_ids", {}).setdefault(int(_g0), [])
                if GIVE_KW[verb] not in _gi:
                    _gi.append(GIVE_KW[verb])
            else:
                dst.setdefault("give", []).append(GIVE_KW[verb])
        elif verb == "GiveHeavyarmor":
            dst["armor"] = dst.get("armor", 0) + 1
    # ★ 组 D（§5）：加密写入叶子的**绝对值**效果 → `set_attack/set_attack_buff/set_defense/
    #   set_kredit/set_kredit_buff = {card_id: v}`，由 boardeval 消费（clamp 在消费侧做）。
    if rec.shadow_stats:
        for ptr, fields in rec.shadow_stats.items():
            cid = rec.ptr_ids.get(ptr)
            if cid is None:
                rec.gaps.append("setAndEncrypt*：影子写入了未知卡指针 %#x 的记录，无法定位 card_id" % ptr)
                continue
            for fname, val in fields.items():
                key = _SET_ENC_EFFECT_KEY.get(fname)
                if key:
                    dst2 = eff if not rec.tainted else unc
                    dst2.setdefault(key, {})[cid] = int(val)
    if unc:
        eff["uncertain"] = unc
    if rec.chance:
        eff["chance"] = sorted(set(rec.chance))
    return eff


#: 对脚本刚生成的牌的改动动词 → 它在 `spawn_cards[i]["ops"]` / `gain_ops[i]` 里的种类（`_apply_eff` 逐条用 engine.natives.stats 结算）。
_SPAWN_OP_KIND = {"ChangeAttack": "attack", "ChangeDefense": "defense", "ChangeOperationCost": "opcost",
                  "ChangeHeavyArmor": "armor", "ChangeKreditCost": "cost"}


def _spawn_op(rec, eff, dst, my_side, r) -> None:
    """`ChangeX(新牌句柄, …)`：字典路没有"对刚生成的单位改数值"这条通道（`buff`/`buff_ids` 套的是选定目标 / 场上已有的单位）。
    并回新牌自己的条目：场上 ⇒ `spawn_cards[i]["ops"]`；我方手牌 ⇒ `gain_ops[i]`；对方手牌 ⇒ 没有逐张状态，记缺口。
    门（`CanCardBeBuffed` / 未揭示隐蔽）与直跑 sink 同一份数据（`rec.cur_stats[句柄]`）；读不到门 ⇒ 缺口、不结算。"""
    args = r["args"]
    verb = r["verb"]
    info = rec.spawned.info_of_handle(args[0])
    kind = _SPAWN_OP_KIND[verb]
    n = args[2] if len(args) > 2 else 0
    ct = args[3] if len(args) > 3 else None
    g = rec.cur_stats.get(args[0])
    if info.get("where") == "opp_hand":
        rec.gaps.append("%s：目标是刚进对方手牌的新牌（对方手牌没有逐张状态）⇒ 未结算" % verb)
        return
    gate_key = {"attack": "buffable", "defense": "buffable", "opcost": "unrevealed_covert",
                "armor": "unrevealed_covert", "cost": "unrevealed_covert"}[kind]
    if kind == "cost" and len(args) > 4 and args[4]:
        pass                                                       # skipCovertCheck=true ⇒ 不过门
    elif g is None or gate_key not in g:
        rec.gaps.append("%s：新牌的门数据读不到（不猜）" % verb)
        return
    elif (kind in ("attack", "defense") and not g[gate_key]) or (kind not in ("attack", "defense") and g[gate_key]):
        return                                                     # 门不过 ⇒ 原版什么都不改
    op = (kind, n, ct)
    if info.get("where") == "board":
        for src in (dst, eff):
            for ent in src.get("spawn_cards") or ():
                if ent.get("tmp_id") == info["id"]:
                    ent.setdefault("ops", []).append(op)
                    return
        rec.gaps.append("%s：新牌的 spawn_cards 条目找不到（%s）" % (verb, info["id"]))
        return
    names, tmp = dst.get("gain_names") or [], dst.get("gain_tmp") or []
    if info["id"] in tmp:
        i = tmp.index(info["id"])
        ops = dst.setdefault("gain_ops", [])
        while len(ops) < len(names):
            ops.append([])
        ops[i].append(op)
        return
    rec.gaps.append("%s：新牌的 gain_names 条目找不到（%s）" % (verb, info["id"]))


def _need_seat(my_side):
    if my_side not in (1, 2):
        raise ValueError("my_side 必须是 1/2（ESide）；座位读不出就别跑效果摘要")
    return my_side


def _is_mine(my_side, side) -> bool:
    """“这一边是不是我方”：就是和 `my_side` 比，**不做兜底**——座位不可能读不出；
    被评估的静态卡/临时手牌 side=0 时，由调用方用 `field_overrides` 喂真实座位。"""
    return side == _need_seat(my_side)


def _seat(side):
    """效果字典里带座位的值：**游戏的真实座位 `ESide`**（不再翻成 local/enemy；谁是我方由 `sim.me` 决定）。
    不是 1/2（被评估的静态卡 side=0）⇒ None，不猜。"""
    return ESide(side) if side in (1, 2) else None


def _is_set_change(change_type) -> bool:
    """（保留旧名，转调 `engine.natives.stats.is_set_value` —— 单一来源在那边，别再写魔数。）

    `EChangeType`：SetValue=2、Suppress=3、veteranSet=5 是"设成 n"；其余（permBuff=1、tempBuffGive=0…）是加减。
    原版出处与逐行分支表见 `engine/natives/stats.py` 的模块注释。
    """
    return _stats.is_set_value(change_type)


def _fill_cur_stats(rec, cards) -> None:
    """`_fill_cur_stats_raw` + 一次建 sim 作用域里的共享：同一份快照（同一个 `cards` 列表对象、同长度）只算一遍，
    之后每次空跑只做 dict 拷贝（原来每次空跑重算全盘 ~3 ms，输入完全相同）。作用域外 = 原样重算。
    拷贝 `_st`（后续 `rec.cur_stats[...]` 可能被改写），`ptr_ids` 仍是 `setdefault`（与原语义一致）。"""
    from kardsmem import readscope as _RS
    sc = _RS.current()
    if sc is None:
        _fill_cur_stats_raw(rec, cards)
        return
    memo = sc.table("cur_stats")
    ent = memo.get(id(cards))
    if ent is None or ent[0] is not cards or ent[1] != len(cards):
        sc.note("cur_stats", False)
        tmp = types.SimpleNamespace(ptr_ids={}, cur_stats={})
        _fill_cur_stats_raw(tmp, cards)
        ent = memo[id(cards)] = (cards, len(cards), tmp.ptr_ids, tmp.cur_stats)
    else:
        sc.note("cur_stats", True)
    for _p, _i in ent[2].items():
        rec.ptr_ids.setdefault(_p, _i)
    for _p, _s in ent[3].items():
        rec.cur_stats[_p] = dict(_s)


def _fill_cur_stats_raw(rec, cards) -> None:
    """把快照里每张牌的**指针 → 当前数值**填进 `rec.cur_stats`（`SetValue` 折算要用）。

    为什么单独抽成函数：这段逻辑原来内联在 `record_effects` 里，而**离线测试喂的是自己造的
    `cur_stats`** ⇒ "`Card.total_attack` 是个**方法**、`getattr` 拿到绑定方法又被
    `isinstance(v, (int, float))` 过滤掉"这个 bug（后果：`attack`/`opcost` 的 SetValue 分支
    永远记「读不到目标当前值」的缺口、**效果被整个丢掉**）在测试里**完全看不见**。
    抽出来之后 `tests/test_native_stats.py` 可以拿**真 Card** 直接把这一段跑一遍。

    读的键（P3，2026-10-03）：
      * `attack` / `opcost`：**总量**（原版 `getTotalAttack()` / `getTotalOperationCost()`）——
        在 `kardsmem/board.py::Card` 上必须是 **property**。写成方法就会被下面的类型过滤掉，
        这正是当初的坑（也说明"能读出来"这件事本身要测）。
      * `defense`：原版**没有**独立 buff 字段 ⇒ 回退到 `.defense`。
      * `attack_buff` / `opcost_buff`：原版那个**独立 buff 累加器**（SetValue 折算 `+buff − cur` 要用）。
    """
    for _c in cards:
        _p = (getattr(_c, "raw", None) or {}).get("ptr")
        if not _p or getattr(_c, "card_id", None) is None:
            continue
        rec.ptr_ids.setdefault(_p, _c.obj.CardID)
        _raw = getattr(_c, "raw", None) or {}
        _st = {}
        for _k, _a in (("attack", "total_attack"), ("defense", "total_defense"),
                       ("opcost", "total_operation_cost"),
                       ("attack_buff", "attack_buff"), ("opcost_buff", "operation_cost_buff"),
                       # 重甲：`Card` 上没有对应 property，但快照 `raw` 里 `cards.read_raw` 已经算好
                       # （`total_heavy_armor` = `clamp(0,3, heavy_armor + heavy_armor_buff)`，cards.py:815）
                       ("armor", "total_heavy_armor"), ("armor_buff", "heavy_armor_buff"),
                       # 手牌费用（`ChangeKreditCost`）：`Card.kredit_cost` 是**总量**
                       # （= `getTotalKredits()`，已 clamp(0,99)）；`kredit_buff` 只在 raw 里
                       ("cost", "kredit_cost"), ("cost_buff", "kredit_buff")):
            _v = getattr(_c, _a, None)
            if _v is None:
                _v = _raw.get(_a)                      # 快照 raw 兜底（重甲走这条；与 triggers.py 同口径）
            if _v is None and _k == "attack":
                _v = getattr(_c, "attack", None)
            if _v is None and _k == "defense":
                _v = getattr(_c, "defense", None)
            if isinstance(_v, (int, float)):            # 过滤掉"读不到"与**绑定方法**（后者是历史 bug 的入口）
                _st[_k] = _v
        # ★ 2026-10-04（R1 的前半）：**门**要用的判据 —— `IsUnrevealedCovertCard`
        #   = `hasCovert@0x1EA && !isRevealed@0x33B`（IDA 定案；与 `cardnatives.py:543` 同一语义）。
        #   原版 `ChangeOperationCost`(:8906) 与 `ChangeHeavyArmor`(:10332) 的**第一句**就是
        #   "目标是未揭示的隐蔽牌 ⇒ 静默 return、什么都不改" ⇒ 记录器看不见它就会**多算**。
        #   放 `cur_stats`（内部读侧），**不新增 eff 键**（EVAL-ARCHITECTURE §重构范围 第 1 条）。
        _kws = getattr(_c, "keywords", None) or ()
        _st["unrevealed_covert"] = bool(
            "covert" in _kws and getattr(_c, "is_revealed", None) is not True)
        # 再加 `CanCardBeBuffed`（原版蓝图函数 `:23631`，逐行读过）：不是未揭示的隐蔽牌 ⇒ true；
        # 是 ⇒ 只有**牌库/手牌**（location 1/2/3/4/9）可被 buff，总部/前线/弃牌堆/NotAvailable 不可。
        # 三个动词（`ChangeAttack` :10990 / `ChangeDefense` :11222 / `ChangeKreditCost`）在
        # 它为 false 时走 `qqq=false; return` ⇒ **什么都不改**。
        # ★ location 优先读**对象**（`obj.Location`，权威且总在），`raw["location_enum"]` 兜底 ——
        #   一开始只读 raw，而快照/假卡的 raw 未必带这个键 ⇒ 会误判成"不可 buff"（测试当场抓到）。
        _loc = getattr(getattr(_c, "obj", None), "Location", None)
        if _loc is None:
            _loc = _raw.get("location_enum")
        _st["buffable"] = _stats.can_card_be_buffed(_st["unrevealed_covert"], _loc)
        # ★ 2026-10-04（P3 ⑤）：**运行时自定义能力**（`receivedAbilitiesFromCards`@0x208）——
        #   权威读法是 `cardnatives._ability_has(c, name) = received_abilities[name] > 0`。
        #   目前两处消费者：`excess`（攻击溢出，sim 侧）与 `cantRetreat`
        #   （原版 `MakeCardRetreat` `:1727` 逐张跳过带它的牌 —— 我们以前不看，会把不该撤的也撤）。
        _abs = _raw.get("received_abilities") or ()
        _st["abilities"] = frozenset(str(a) for a in _abs)
        _st["cant_retreat"] = any(str(a).lower() == "cantretreat" for a in _abs)
        rec.cur_stats[_p] = _st


def _live_overlay(v, rec, ptr, direct_state):
    """卡视图 + **本次空跑里已发生的关键词/重甲变化**：
    * 录制路（字典路 A）：`rec.live_kw[ptr]`（Give* / GiveRandomCombatKeyword 的钩子记的）；
    * 直跑（B）：`direct_state.units[card_id]` 的当前关键词/重甲（sink 已经改了状态，视图要跟上）。
    没有变化 ⇒ 原样返回（不复制）。heavyarmor 只对"视图里重甲 + 记录/状态"不一致时叠加。"""
    if not isinstance(v, dict):
        return v
    add = list(rec.live_kw.get(ptr) or ())
    armor = None
    if direct_state is not None:
        cid = v.get("card_id")
        u = (getattr(direct_state, "units", None) or {}).get(cid) if cid is not None else None
        if u is not None:
            add += [k for k in (getattr(u, "kw", ()) or ())]
            armor = int(getattr(u, "armor", 0) or 0) - int(getattr(u, "armor_buff", 0) or 0)
    ks = list(v.get("keywords") or ())
    new_ks = [k for k in dict.fromkeys(add) if k != "heavyarmor" and k not in ks and ("has_" + k) not in ks]
    n_heavy = sum(1 for k in (rec.live_kw.get(ptr) or ()) if k == "heavyarmor")
    if not new_ks and not n_heavy and armor is None:
        return v
    v = dict(v)
    if new_ks:
        v["keywords"] = ks + new_ks
    if n_heavy:
        v["heavy_armor"] = int(v.get("heavy_armor") or 0) + n_heavy
    if armor is not None and armor > int(v.get("heavy_armor") or 0):
        v["heavy_armor"] = armor
    return v


def _spawn_loc_enum(info) -> int:
    """新牌所在位置的 `ECardLocationEnum` 值：我方/对方手牌 3/4、后排 5/6、前线 7（座位 1=left / 2=right）。"""
    sd = info.get("side")
    if info.get("where") in ("hand", "opp_hand"):
        return 4 if sd == 2 else 3
    if info.get("row") == "frontline":
        return 7
    return 6 if sd == 2 else 5


def _spawn_base(info, static_card) -> Optional[dict]:
    """新牌的**静态卡视图**：`cards.read_raw(静态卡对象)`（含 `gameplay_tags` / `received_abilities` / `custom_name*` 这些"轻量实时字段"——
    `static_card()` 返回的那份是 `light_effects=False`，读 `getHasGameplayTag` 会缺键）。按（静态卡指针）缓存。"""
    pf = getattr(static_card, "ptr_of", None)
    sptr = pf(info["name"]) if pf else None
    if not sptr:
        return static_card(str(info["name"]).lower()) if static_card else None
    cache = getattr(static_card, "_light_cache", None)
    if cache is None:
        try:
            static_card._light_cache = cache = {}
        except (AttributeError, TypeError):
            cache = {}
    if sptr not in cache:
        from kardsmem import cards as _cards                                  # noqa: PLC0415
        cache[sptr] = _cards.read_raw(static_card._session, sptr)
    return cache[sptr]


def _spawn_gate(info, static_card) -> Optional[dict]:
    """新牌句柄的门数据：`unrevealed_covert`（静态卡带隐蔽 ⇒ 刚生成时未揭示）+ `buffable`（`CanCardBeBuffed`）。
    静态卡读不到 ⇒ None（sink 记缺口，不猜）。"""
    base = _spawn_base(info, static_card)
    if not base:
        return None
    ks = base.get("keywords") or ()
    covert = ("has_covert" in ks) or ("covert" in ks)
    return {"unrevealed_covert": bool(covert),
            "buffable": _stats.can_card_be_buffed(bool(covert), _spawn_loc_enum(info))}


def _spawn_view(info, static_card, direct_state) -> dict:
    """合成句柄的卡视图 = 该牌的静态卡视图 + 登记信息（id/座位/位置）+ **模拟里的当前值**（直跑时：本次脚本已经改过的攻防/关键词/重甲）。"""
    from kardsmem.board import LOCATION_NAMES                             # noqa: PLC0415
    base = _spawn_base(info, static_card)
    v = dict(base or {})
    v["ptr"] = info["handle"]
    v["card_id"] = info["id"]
    sd = info.get("side")
    if sd in (1, 2):
        v["side_enum"], v["side"] = int(sd), ESide(sd)
    loc = _spawn_loc_enum(info)
    v["location_enum"], v["location"] = loc, LOCATION_NAMES.get(loc)
    st = direct_state
    u = (getattr(st, "units", None) or {}).get(info["id"]) if st is not None and info.get("where") == "board" else None
    if u is not None:
        buff = int(getattr(u, "atk_buff", 0) or 0)
        v["shadow_stats"] = {"attack": int(u.atk) - buff, "attackBuff": buff, "defense": int(u.dfn)}
        ks = list(v.get("keywords") or ())
        ks += [k for k in (getattr(u, "kw", ()) or ()) if k not in ks]
        v["keywords"] = ks
        ab = int(getattr(u, "armor_buff", 0) or 0)
        v["heavy_armor"], v["heavy_armor_buff"] = int(getattr(u, "armor", 0) or 0) - ab, ab
    return v


def _hq_ptr_sides(direct_state, rec) -> dict:
    """总部卡指针 → `Sim.hq` 的键（座位）：己方总部指针 → `state.me`，敌方 → `state.opp`（与字典路
    `damage_hq → s.hq[s.opp]` / `damage_own_hq → s.hq[s.me]` 同一口径）。座位不明 ⇒ 不喂（sink 自己记缺口）。"""
    me, opp = getattr(direct_state, "me", None), getattr(direct_state, "opp", None)
    out = {}
    if opp is not None:
        out.update({p: opp for p in getattr(rec, "hq_enemy", ()) or ()})
    if me is not None:
        out.update({p: me for p in getattr(rec, "hq_own", ()) or ()})
    return out


def _direct_hooks(direct_state, rec, base_hooks, my_side, spawn_stat=None, on_draw=None, on_death=None):
    """P5 影子：把 `base_hooks`（录制路径）换成**直跑**表——已迁动词直接改 `direct_state`，未迁的回退录制。

    载荷型动词（`ConvertCard`/`MakeVeteran`）的载荷要在**调用点**用存活视图现算（录制路径是在 recorder 的钩子里算的；
    直跑的钩子被 sink 占了，所以在这里补一道）：算不出 ⇒ 不喂 ⇒ sink 自己记缺口，不猜。
    返回 `(hooks, DirectCtx)`。
    """
    from engine.scripts import native_hooks as _native_hooks       # noqa: PLC0415
    # 门数据（`buffable` = CanCardBeBuffed、`unrevealed_covert`）：录制路径把它们放在 `rec.cur_stats[ptr]`
    # （`_fill_cur_stats` 按快照填好）⇒ 直跑**共用同一份**，否则 ChangeAttack/Defense/OpCost/HeavyArmor 全记『缺门数据』
    # 而不改状态（2026-10-06 实机影子：FOR FREEDOM/USACE/SUPPLY SHIPMENT 的 diff_ab 就是它）。
    hooks = _native_hooks(direct_state, ptr_ids=rec.ptr_ids, my_side=my_side, fallback=base_hooks,
                          rng=getattr(rec, "stream", None), spawn_stat=spawn_stat, on_draw=on_draw, on_death=on_death,
                          gates=getattr(rec, "cur_stats", None), hq_ptr_sides=_hq_ptr_sides(direct_state, rec))
    dctx = hooks.pop("__ctx__", None)
    if dctx is None:
        return hooks, None
    dctx.pick = lambda verb, size: (rec.chance.append(verb), rec._pick(verb, size))[1]   # 随机点枚举：同录制器的 forced/nodes
    if getattr(rec, "spawned", None) is not None:
        dctx.spawned = rec.spawned                                       # 同一份新牌登记表（`FetchCardFromCardID` 软钩子也查它）
    dctx.spawn_gate = lambda info: getattr(rec, "spawn_gate", lambda _i: None)(info)

    inner_c = hooks.get("ConvertCard")
    if inner_c is not None:
        def _hc(vm, frame, obj, args, e, _inner=inner_c):
            try:
                dctx.payload["convert"] = _convert_payload(vm, args)
            except Exception as ex:                                  # noqa: BLE001
                dctx.payload.pop("convert", None)
                dctx.gaps.append("ConvertCard：取不到载荷（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
            return _inner(vm, frame, obj, args, e)
        hooks["ConvertCard"] = _hc

    inner_v = hooks.get("MakeVeteran")
    if inner_v is not None:
        def _hv(vm, frame, obj, args, e, _inner=inner_v):
            # 老兵的 `vet` 载荷挂在 `ctx.gates[card_id]["vet"]`（见 scripts 的 MakeVeteran sink）
            try:
                cid = rec.ptr_ids.get(args[0]) if args else None
                if cid is not None:
                    dctx.gates.setdefault(cid, {})["vet"] = _veteran_payload(vm, args[0])
            except Exception as ex:                                  # noqa: BLE001
                dctx.gaps.append("MakeVeteran：取不到载荷（%s: %s）" % (type(ex).__name__, str(ex)[:80]))
            return _inner(vm, frame, obj, args, e)
        hooks["MakeVeteran"] = _hv
    return hooks, dctx


def _profiled(fn):
    """建 sim 作用域里给 `record_effects` 逐次计时（诊断；作用域外零开销、行为不变）。
    记：耗时 / 钩子名 / 卡指针 / 这一次新读的卡视图数（`view` 未命中增量）/ 停因。"""
    import functools
    import time as _t

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        from kardsmem import readscope as _RS
        sc = _RS.current()
        if sc is None:
            return fn(*a, **kw)
        m0, t0 = sc.misses.get("view", 0), _t.perf_counter()
        r = None
        try:
            r = fn(*a, **kw)
            return r
        finally:
            try:
                hook = kw.get("hook") if "hook" in kw else (a[4] if len(a) > 4 else "OnPlayedFromHand")
                ptr = kw.get("card_ptr") if "card_ptr" in kw else (a[1] if len(a) > 1 else None)
                sc.note_call(_t.perf_counter() - t0, hook, ptr, sc.misses.get("view", 0) - m0,
                             (r or {}).get("stopped") if isinstance(r, dict) else None)
            except Exception:                                     # noqa: BLE001
                pass
    return wrapper


@_profiled
def record_effects(km, card_ptr: int, target_ptr: int = 0, has_target: bool = False,
                   hook: str = "OnPlayedFromHand", my_side=None, forced=None,
                   read_hooks: Optional[dict] = None, slots=None, args: Optional[dict] = None,
                   hq_own=(), hq_enemy=(), timeout_s: Optional[float] = None,
                   rng_seed: Optional[int] = None, rng_stream=None, kredits=None,
                   board=None, my_seat=None, field_overrides: Optional[dict] = None,
                   view_overrides: Optional[dict] = None, direct_state=None,
                   direct_spawn_stat=None, direct_on_draw=None, direct_on_death=None) -> dict:
    """在真进程的内存上把 `card_ptr` 这张牌的 `hook` 空跑一遍。

    `direct_state`（P5 影子模式）：给一份 `Sim` 副本 ⇒ 已迁动词**直跑**改它（`engine.scripts.native_hooks`），
    未迁动词仍走录制；返回里多 `applied`（类型化调用串）与 `direct_gaps`。**默认 None = 行为与以前完全一致**。

    `field_overrides`：`{(对象指针, 字段名): 值}`——空跑时这些字段按给定值读（只读覆盖，不写游戏）。用来把
    **静态卡/手牌**当成"此刻已经打出"来跑它的常驻钩子（例：JUNGLE FEVER 守卫 `enterPlayOnTurn==本回合`、`side==进场单位的 side`）。

    `view_overrides`：`{对象指针: 覆盖操作}`——卡视图（`CARDS.read_raw` 的 dict，`getHas*`/`getTotal*` 这类原生读它）
    上叠一层"此刻已经变成这样"的只读覆盖（`apply_view_override`）。用来把**还没发生的状态变更的结果**喂给旁观者钩子
    （A5-③：`OnOtherCardAbilitiesChanged` 被调用时，被改的牌的关键词已经是改变后的值；快照里还是改变前）。
    覆盖操作：`keywords_add`/`keywords_remove`（列表，作用在视图的 `keywords`）、`received_remove`（能力名列表，
    从 `received_abilities` 里摘掉）、其它键直接覆盖视图同名键。不写游戏。

    `args`：钩子的形参（按名字），例如 `OnAfterAttack` 要 {"defenderCard": 敌方总部指针,
    "wasShockAttack": False, "attackCost": 1}。`hq_own`/`hq_enemy`：总部卡指针，用来把
    「给己方总部 +防御」记成 heal_hq。

    返回 {"eff": 效果摘要, "records": [...], "stopped": None|原因, "complete": bool,
          "chance": [...]}。VM 不可用/出错 ⇒ {"eff": {}, "stopped": "...", "complete": False}。
    """
    out = {"eff": {}, "records": [], "stopped": None, "complete": False, "chance": [],
           "nodes": [], "out": {}, "ran": False, "gaps": []}
    try:
        import os
        import sys
        tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from canplay import hook_targeted, make_get_field, make_view       # noqa: PLC0415
        from kardsmem import kismet
        from kardsmem.cardnatives import CardNatives
        from kardsmem.objects import ObjectArray
        from kardsmem.vm import VM
        uc = ObjectArray(km).class_of(card_ptr)
        fn = None
        # hook=None ⇒ 按「打出/部署」钩子的常见名字依次找第一个存在的
        for h in ((hook,) if hook else ("OnPlayedFromHand", "OnDeployed", "OnDeploy")):
            fn = kismet.find_function(km, uc, h, inherited=False) if uc else None
            if fn:
                break
        if not fn:
            out["stopped"] = "这张牌没有 %s 覆写" % (hook or "打出/部署钩子")
            return out
        rec = Recorder(card_ptr, target_ptr, forced)

        def _json_loader(card, _km=km):
            from kardsmem import cards as _cards                   # noqa: PLC0415
            return (_cards.read_custom_json_raw(_km, card) or {}).get("entries") or {}
        rec.json_loader = _json_loader
        # ★ PLAN 阶段 5：同一个 Action 里"自己的钩子 + 别人挂的触发钩子"必须**共用同一条流**
        #   （游戏里就是顺序消耗同一条 cardsRandomStream）。传 `rng_stream` 进来即复用，
        #   抽取次数会累加在同一个对象上。
        if rng_stream is not None:
            rec.stream = rng_stream
        elif rng_seed is not None:
            from kardsmem.rng import Stream
            rec.stream = Stream(rng_seed)
        rec.hq_own, rec.hq_enemy = frozenset(x for x in hq_own if x), frozenset(x for x in hq_enemy if x)
        # 快照里的卡：预填 指针→card_id（群体效果遍历到的牌不一定被 view 读过，`buff_ids` 之类要靠它出键）
        _fill_cur_stats(rec, getattr(board, "cards", None) or ())
        hooks = rec.hooks()
        _dctx = None
        if direct_state is not None:
            hooks, _dctx = _direct_hooks(direct_state, rec, hooks, my_side, direct_spawn_stat, direct_on_draw, direct_on_death)
        hooks["GetTargetedCard"] = hook_targeted(bool(has_target), target_ptr)
        for nm_, fn_ in (read_hooks or {}).items():          # 调用方给的只读原语（如读指挥点槽）
            hooks[nm_] = fn_
        _base_view = make_view(km)

        def _shadow_view(ptr, _rec=rec, _base=_base_view):
            """view 包装：填 `ptr_ids`（给 set_* 效果出 card_id）+ 注入 `shadow_stats`
            （同一次空跑里 setAndEncrypt* 写过的字段，读侧要先看到）。"""
            _sp = _rec.spawned.info_of_handle(ptr)
            if _sp is not None:                                   # 脚本刚生成的牌：静态卡 + 模拟里的当前值
                return _spawn_view(_sp, getattr(cn, "static_card", None), direct_state)
            v = _base(ptr)
            v = _live_overlay(v, _rec, ptr, direct_state)           # 同一次空跑里脚本已经给过的关键词 / 重甲（读到的是"此刻"的值）
            if isinstance(v, dict) and view_overrides and ptr in view_overrides:
                v = apply_view_override(v, view_overrides[ptr])   # A5-③：改变后的状态（只读覆盖）
            if isinstance(v, dict):
                cid = v.get("card_id")
                if cid is not None:
                    _rec.ptr_ids[ptr] = cid
                sh = _rec.shadow_stats.get(ptr)
                if sh:
                    v = dict(v)
                    v["shadow_stats"] = dict(sh)
            return v

        cn = CardNatives(None, view=_shadow_view, ks=km)
        # ★ 2026-10-02（§8-9 标记排查）：盘面级原语需要 `BoardState` + `my_seat`
        #   （`IsSideActive` / `getKreditBySide` 没有就抛 Unimplemented ⇒ 整条钩子链断）。
        #   实测：`CRUISER SCOUTS` 的链曾停在 `IsSideActive`（TARNOW 类卡同理）——
        #   调用方（rule/triggers）把快照传进来即可。缺省 None = 保持旧行为（如实抛）。
        if board is not None:
            cn.board = board
        if my_seat is not None:
            cn.my_seat = my_seat
        # ★ §8-5 / NATIVE-COVERAGE §15.6：注入静态卡表提供者（`GetStatic*` / `getHasVeteranUpgrade`
        #   的地基）。读不到表时提供者是空的 ⇒ `_static()` 会如实抛 Unimplemented（缺口），
        #   不会退回"默认卡字段"。
        try:
            from kardsmem.gs import make_static_card_provider
            prov = make_static_card_provider(km)
            cn.static_card = prov
            cn.static_cards = getattr(prov, "names", None) or frozenset()
        except Exception as _e:                               # noqa: BLE001
            rec.gaps.append("静态卡表提供者构建失败：%s: %s" % (type(_e).__name__, _e))
        _gf = make_get_field(km)
        rec.spawn_gate = lambda info: _spawn_gate(info, getattr(cn, "static_card", None))
        _gf0 = _gf

        def _gf(obj, name, _base=_gf0, _rec=rec):                  # noqa: F811
            """字段读：合成句柄（脚本刚生成的牌）按登记信息答，其余字段落到该牌的静态卡对象上。"""
            _sp = _rec.spawned.info_of_handle(obj)
            if _sp is None:
                return _base(obj, name)
            _v = _spawn_view(_sp, getattr(cn, "static_card", None), direct_state)
            _m = {"cardID": _sp["id"], "side": _v.get("side_enum"), "location": _v.get("location_enum"),
                  "name": _sp["name"], "locationNumber": 0}
            if name in _m and _m[name] is not None:
                return _m[name]
            _pf = getattr(getattr(cn, "static_card", None), "ptr_of", None)
            _sptr = _pf(_sp["name"]) if _pf else None
            if not _sptr:
                raise Unimplemented("新生成的牌 %s 没有静态卡对象，读不了字段 %s" % (_sp["name"], name))
            return _base(_sptr, name)
        if field_overrides:
            _ov = dict(field_overrides)

            def _gf_ov(obj, name, _base=_gf, _ov=_ov):
                if (obj, name) in _ov:
                    return _ov[(obj, name)]
                return _base(obj, name)
            _gf = _gf_ov
        vm = VM(km, get_field=_gf, card_natives=cn, hooks=hooks)
        if timeout_s:
            import time as _t
            vm.deadline = _t.time() + float(timeout_s)     # 超时如实停下，不无限拖延一步决策
        if has_target and target_ptr and not args and (hook in (None, "OnPlayedFromHand")):
            # 带目标打出：事件形参 `targetCard` 就是被点的目标（BP 里 `cardFunction->GetAdjacentCards(Event_targetCard…)`
            # 这类直接读它，不只是 `GetTargetedCard()`）。
            args = {"targetCard": target_ptr}
        r = vm.run(fn, self_obj=card_ptr, args=args)
        out["stopped"] = r.get("stopped")
        # ★ 钩子的出参（`stopAttack`/`AttackedAndStopped`/`newDefender` 这类会改流程的值）：
        #   以前被丢掉，攻击链（engine/triggers.py::run_attack_hooks）要靠它决定"被吞/换目标"。
        out["out"] = dict(r.get("out") or {})
        out["ran"] = True
        out["gaps"] = list(rec.gaps)
        out["records"] = rec.records
        out["chance"] = list(rec.chance)
        out["nodes"] = list(rec.nodes)
        out["choice"] = rec.choice
        out["exact"] = rec.exact
        out["draws"] = rec.stream.draws if rec.stream is not None else 0
        _kr = kredits if kredits is not None else getattr((read_hooks or {}).get("getKreditBySide"), "cur_kredits", None)
        out["eff"] = to_effects(rec, my_side, slots, _kr)
        if rec.stream is not None and rec.stream.draws:
            # 这次空跑**自己**耗掉的随机流次数（`GetRandomCard` 等）：游戏只有一条流，字典路 `_apply_eff` 之后的洗牌/塞牌要从
            # 它们**之后**接着抽，否则牌序对不上（直跑对账 SHIFTING DOCTRINE）。记下来，`_apply_eff` 先把 `Sim.rng` 前进这么多。
            out["eff"]["rng_draws"] = int(rec.stream.draws)
        out["complete"] = out["stopped"] is None
        if _dctx is not None:
            out["applied"] = list(_dctx.applied)
            out["direct_gaps"] = list(_dctx.gaps)
    except Exception as ex:                                       # noqa: BLE001
        out["stopped"] = "%s: %s" % (type(ex).__name__, ex)
    return out


def _combos(sizes: list, cap: int) -> list:
    """所有结果组合的下标列表；组合数超过 cap 时只取前 cap 个（确定性截断）。"""
    out = [[]]
    for n in sizes:
        out = [c + [i] for c in out for i in range(n)]
        if len(out) > cap * 4:
            out = out[:cap * 4]
    return out[:cap]


def _generic_out(frame, e, value) -> None:
    """把值写进调用点里**最后一个**局部变量 kid（Kismet 把出参压在调用点末尾）。"""
    try:
        for k in reversed(e.kids):
            if k.op in ("LocalVariable", "LocalOutVariable") and k.args.get("prop"):
                frame.locals[k.args["prop"]] = value
                return
    except Exception:                                             # noqa: BLE001
        pass


def make_read_hooks(st, my_side=None, field_overrides=None, played_ptr=None) -> dict:
    """当前局面 → 只读原语的钩子（指挥点槽/指挥点/回合数）。这些是游戏里的原生函数、
    没有字节码，VM 读不了，就由调用方按快照喂。返回值和出参两种用法都兼容。

    `field_overrides`：与 `enumerate_effects` 那份**同一张表**（`{(对象指针, 字段名): 值}`）。
    `_opposite` 也查它 —— 评估的对象常是静态卡（side=0），"这张牌是我方的"是调用方喂的；
    hook 不查这张表就会把能算的效果断在这里（实测：RAPID RESPONSE 停在 `GetOppositeSide`）。

    `played_ptr`：**正在被打出的那张牌**的指针。原版 `PlayCardFromHand`（`BP_CardFunctions.cpp:18390-18480`）在
    `CardPlayedFromHand`→`OnPlayedFromHand` **之前**先把牌的 location 改走（指令 `tmpnewLocation=0x8`，单位 = 支援线/前线），
    所以脚本里 `GetCardsInHandBySide` 看不到它自己（IRON VICTORY/EDGE OF THE EMPIRE/REDEPLOYMENT 的"弃/洗手牌"不含自己；
    IRON VICTORY 还多一道 `IsLocatedInHand` 兜底）。我们空跑时这张牌还在快照的手牌里 ⇒ 这里排除它，与原版一致。
    """
    def side_of(args):
        v = args[0] if args else None
        # 被评估的静态卡对象 side=0 时座位由调用方用 `field_overrides` 喂真实座位；这里读不出就抛，不按我方算。
        if v not in (1, 2):
            raise Unimplemented("座位参数不是 1/2（%r）：静态卡请用 field_overrides 喂真实座位" % (v,))
        return ESide(v)

    def hk(table_name, getter):
        def fn(vm, frame, obj, args, e):
            val = getter(side_of(args))
            _generic_out(frame, e, val)
            return val
        return fn
    slots = getattr(st, "slots", None) or {}
    kred = getattr(st, "kredits", None) or {}
    # 场上卡查询：`CardFunctionsStub.GetAllUnitsOnBoard` / `GetCardsOnBoardBySide` 是**原生函数**，
    # VM 没有它们的字节码 ⇒ 以前一跑到这里就 "Unimplemented"（热浪2/骄阳3 卡死在这），
    # 效果枚举不出来，只能退化成常量。这里按快照喂。实测签名（1.60 导出）：
    #   GetAllUnitsOnBoard(bool includeCovertCards, TArray<UBaseCardObject*>& cards)
    #   GetCardsOnBoardBySide(ESideEnum side, bool unitsOnly, bool includeCovertCards, TArray<...>& cards)
    _unit_types = ("infantry", "tank", "artillery", "fighter", "bomber",
                   "antiair", "antitank", "tankdestroyer")

    def board_cards(side=None, units_only=False, include_covert=True, any_defense=False):
        res = []
        for c in (getattr(st, "cards", None) or []):
            # ★ 2026-10-02 IDA 普查（Claude，`BOARD-QUERY-NATIVES-1.60.md`）：真实的
            #   `GetCardsOnBoardBySide` / `GetAllUnitsOnBoard` 语义 =
            #   **当前防御>0 ∧ 在场(Location 5..7，HQ 也算) ∧ side** ∧ covert 过滤
            #   ∧ (unitsOnly ⇒ IsUnit 类型 3..10)；**不含手牌/牌库**；
            #   顺序 = `AllCardsInBattle` 稀疏数组下标升序 = 卡牌创建顺序（我们按快照顺序喂 ✓）。
            if not c.obj.IsLocatedOnBoard():
                continue
            if side is not None and c.obj.side != side:
                continue
            if not any_defense and (c.obj.defense or 0) <= 0:   # 防御≤0 的不算"在场"
                continue
            if units_only and c.card_type not in _unit_types:
                continue
            # ★ covert 判据（IDA 定案）：`IsUnrevealedCovertCard = hasCovert@0x1EA && !isRevealed@0x33B`
            #   —— 不看 side，敌我对称。以前我用 keywords 里有没有 covert **单独**判定，不可靠
            #   （隐蔽可被揭示）；现在两个条件都要满足。
            if not include_covert:
                kws = getattr(c, "keywords", None) or ()
                if isinstance(kws, dict):
                    kws = list(kws)
                if (any(str(k).lower().endswith("covert") for k in kws)
                        and getattr(c, "is_revealed", True) is False):
                    continue
            p = (getattr(c, "raw", None) or {}).get("ptr")
            if p:
                res.append(p)
        return res

    def _row_of(c):
        """原版的"位置"枚举：前线 = Board_Frontline；总部卡和支援线单位**同属** Board_HQLeft/Right（总部只是行里的一张牌，
        `locationNumber` 随左边插入的单位后移）。快照把总部叫 `hq`、单位叫 `back`，邻接要把它们当同一排。"""
        o = c.obj
        return "support" if o.InSupportLine() else ("frontline" if o.InFrontline() else None)

    def adjacent_cards(ptr, include_covert):
        me = next((c for c in (getattr(st, "cards", None) or ())
                   if (getattr(c, "raw", None) or {}).get("ptr") == ptr), None)
        if me is None or _row_of(me) not in ("frontline", "support"):
            return []
        n = getattr(me, "slot", None)
        if not isinstance(n, int):
            return []
        res = []
        for want in (n - 1, n + 1):
            for c in (getattr(st, "cards", None) or ()):
                if _row_of(c) != _row_of(me) or getattr(c, "slot", None) != want:
                    continue
                if _row_of(me) == "support" and c.obj.side != me.obj.side:
                    continue
                if not include_covert:
                    kws = getattr(c, "keywords", None) or ()
                    if isinstance(kws, dict):
                        kws = list(kws)
                    if any(str(k).lower().endswith("covert") for k in kws) and getattr(c, "is_revealed", True) is False:
                        continue
                p = (getattr(c, "raw", None) or {}).get("ptr")
                if p:
                    res.append(p)
                break
        return res

    def hk_cards(getter):
        def fn(vm, frame, obj, args, e):
            val = getter(args)
            _generic_out(frame, e, val)
            return val
        return fn

    # `GetOppositeSide(card)`：字节码常拿它推"敌方"（例：rain2_deluge2 = 全体敌方 -1 攻）。
    # 我们评估的是**静态卡对象**（`side_enum=0`，不属于任何一方）⇒ cardnatives 会以
    # "认不出的 side=0"停下。真游戏里这张牌此刻在我手里（我方座位），所以兜底：
    # 先按快照查这张卡的座位；查不到（静态卡）就按**我方座位**算。

    def seat_for(ptr):
        for c in (getattr(st, "cards", None) or []):
            if (getattr(c, "raw", None) or {}).get("ptr") == ptr:
                return (getattr(c, "raw", None) or {}).get("side_enum")
        return None

    def hk_seat(fn):
        def hook(vm, frame, obj, args, e):
            # ★ 2026-10-04（只读反汇编定案）：`FinalFunction` 的 **receiver 不一定在 kids 里**
            #   —— `GetOppositeSide` 的唯一 kid 是出参 `LocalVariable(CallFunc_GetOppositeSide_oppositeSide)`
            #   （初值 None），**卡本身是 receiver**（BP 原文 `this->GetOppositeSide()`，
            #   `card_event_storm2_thunderstorm.cpp:28`）。而这里的 `obj` 可能是 None
            #   （`vm.call` 只在走 native 那条路时才回退 `f.self_obj`，见 `vm.py:571`）
            #   ⇒ 补上同样的回退，把"被评估的那张卡"交给下面。
            val = fn(obj if obj is not None else getattr(frame, "self_obj", None), args)
            _generic_out(frame, e, val)
            return val
        return hook

    def _opposite(recv, args):
        # ★ 2026-10-04（只读反汇编定案，证据见 TODO「天气族 gap 根因」）：
        #   字节码是 `FinalFunction(fn=GetOppositeSide)` + 唯一 kid = **出参**
        #   `LocalVariable(CallFunc_GetOppositeSide_oppositeSide)`（初值 None）；
        #   **被问的卡是 receiver**（`this->GetOppositeSide()`，BP `card_event_storm2_thunderstorm.cpp:28`）。
        #   旧实现只看 `args[0]`（出参）⇒ 恒 None ⇒ 天气族 7 变体 + IJN SHINANO 全记**假缺口**
        #   （"座位读不出"是误判；`field_overrides` 也因此永远匹配不上）。
        #   现在：优先用 receiver；**有些调用把卡当显式入参传**（RAPID RESPONSE 那次就靠 override），
        #   所以 receiver 拿不到时再退回 `args[0]`。
        p = recv if isinstance(recv, int) and recv else (args[0] if args and isinstance(args[0], int) else 0)
        ov = (field_overrides or {}).get((p, "side")) if p else None
        s = ov if ov in (1, 2) else (seat_for(p) if p else None)
        if s not in (1, 2):
            # 座位仍读不出：缺口，不按我方算
            # ★ 诊断：把「被问的指针」与「override 里有哪些 ptr 带 side」写进消息（弯路 #44）。
            _ov_ptrs = [hex(k[0]) for k in (field_overrides or {}) if k[1] == "side"]
            raise Unimplemented(
                "GetOppositeSide：这张牌的座位读不出（被问的 ptr=%s；override 里有 side 的 ptr=%s）"
                % (hex(p) if isinstance(p, int) and p else repr(p), _ov_ptrs))
        return 2 if s == 1 else 1

    def hand_cards(side):
        return [(getattr(c, "raw", None) or {}).get("ptr")
                for c in (getattr(st, "cards", None) or [])
                if c.obj.InHand() and c.obj.side == side
                and (getattr(c, "raw", None) or {}).get("ptr")
                and (getattr(c, "raw", None) or {}).get("ptr") != played_ptr]

    def _max_kred(_recv, _args):
        """`getMaxPossibleKredits` 的真值：快照的 `max_possible_kredits`（GS+0x338）。
        读不到就抛（旧实现拿槽数顶替 —— 与 IDA 不符，2026-10-02 用户确认）。"""
        v = getattr(st, "max_possible_kredits", None)
        if not isinstance(v, int):
            raise Unimplemented("max_possible_kredits 快照没读到（GameState+0x338）")
        return int(v)

    out = {
        "getKreditSlotBySide": hk("slot", lambda sd: int(slots.get(sd) or 0)),
        # ★ 2026-10-02（IDA 0x144B3C010 / 用户）：这两个都读 **GameState+0x338** 的全局上限，
        #   **不是槽数**（旧映射 `hk("slot", …)` 是错的）。快照没读到就抛，不猜。
        "GetMaxKreditsBySide": hk_seat(_max_kred),
        "getMaxPossibleKredits": hk_seat(_max_kred),
        "GetKreditsBySide": hk("kred", lambda sd: int(kred.get(sd) or 0)),
        "getKreditBySide": hk("kred", lambda sd: int(kred.get(sd) or 0)),
        "GetAllUnitsOnBoard": hk_cards(
            lambda a: board_cards(None, True, bool(a[0]) if a else True)),
        # `GetAllCardsOnBoard(includeCovert, &cards)`（BP_CardFunctions@457）：只要 IsLocatedOnBoard + covert
        # 过滤，**无防御/side/IsUnit 判据**（card_event_echelon 靠它给全场单位挂 trigger）。
        "GetAllCardsOnBoard": hk_cards(
            lambda a: board_cards(None, False, bool(a[0]) if a else True, any_defense=True)),
        "GetCardsOnBoardBySide": hk_cards(
            lambda a: board_cards(side_of(a), bool(a[1]) if len(a) > 1 else False,
                                  bool(a[2]) if len(a) > 2 else True)),
        # `BP_CardFunctions::GetAdjacentCards(card, includeCovert, &adjacent)`（BP@577，2026-10-03 读过字节码）：
        # `FetchCardsByLocation(card->location)` 里 locationNumber == 自己 ±1 的牌（先 -1 后 +1，各一张），
        # 隐蔽未揭示的牌除非 includeCovert 否则不算。后排每方各一排、前线双方共用 ⇒ 后排要同 side。
        "GetAdjacentCards": hk_cards(lambda a: adjacent_cards(a[0] if a else 0, bool(a[1]) if len(a) > 1 else False)),
        "GetOppositeSide": hk_seat(_opposite),
        # 手牌查询（同上是原生）：`storm2_thunderstorm2`（雷暴2）这类效果要用它
        "GetCardsInHandBySide": hk_cards(lambda a: hand_cards(side_of(a))),
        "GetCardsInHandBySideOrdered": hk_cards(lambda a: hand_cards(side_of(a))),
    }
    out["getKreditBySide"].cur_kredits = kred     # setKreditBySide（绝对值）要靠它换算增量
    turn = getattr(st, "turn", None)
    if turn is not None:
        out["GetTurnNumber"] = lambda vm, frame, obj, args, e: (_generic_out(frame, e, int(turn)) or int(turn))
    # ★ 2026-10-02（A8 核实）：`UKismetNodeHelperLibrary::GetEnumeratorUserFriendlyName(UEnum*, int32)`
    #   以前只有 `semantics/legality.py` 的 VM 提供，**effectvm 的求值链没有** ⇒ 卡（如 KYOTO REGIMENT）
    #   一旦问枚举名就整条停。这里补上：只认 `ETypeEnum`（表和 legality.ETYPE 同源，
    #   `KardsCore_structs.hpp:82`），别的枚举如实抛 Unimplemented（不猜）。
    _ETYPE = ("NotAvailable", "location", "order", "tank", "fighter", "bomber", "infantry",
              "artillery", "antiair", "antitank", "tankdestroyer", "gotcha", "wildcard")

    # 原生枚举（`/Script/kards.ECardLocationEnum`、`/Script/KardsCore.EFactionEnum`，CONFUSION / PARTISANS / SECOND FRONT 把它们写进
    # `customJson`：`original_location` / `salvage_faction`）。`/Script/` 下的原生枚举在发行版里没有显示名元数据 ⇒
    # `GetEnumeratorUserFriendlyName` 回的就是枚举项自己的名字（与 ETypeEnum 同一条路；SDK 来源
    # `reverse-data/projects/KardsExportDLL4/SDK/kards_structs.hpp:117`、`KardsCore_structs.hpp:18`）。表按枚举值下标，末项 `_MAX` 不算。
    _ENUM_NAMES = {
        "ETypeEnum": _ETYPE,
        "ECardLocationEnum": ("NotAvailable", "Deck_Left", "Deck_Right", "Hand_Left", "Hand_Right", "Board_HQLeft",
                              "Board_HQRight", "Board_Frontline", "Discard", "Deck"),
        "EFactionEnum": ("NotAvailable", "Germany", "Britain", "Japan", "Soviet", "USA", "France", "Italy", "Poland",
                         "Finland", "Anzac", "Allies", "Neutral"),
    }

    def _enum_user_friendly(vm, frame, obj, args, e):
        from kardsmem.kismetlib import Unimplemented
        nm = args[0] if args else None
        val = args[1] if len(args) > 1 else None
        if isinstance(nm, int) and nm:
            try:
                from kardsmem.objects import ObjectArray
                nm = ObjectArray(vm.s).pool().fname_of(nm)
            except Exception:                                     # noqa: BLE001
                nm = None
        names = _ENUM_NAMES.get(nm)
        if names is None:
            raise Unimplemented("GetEnumeratorUserFriendlyName：只认得 %s，这个是 %r" % ("/".join(_ENUM_NAMES), nm))
        try:
            return names[int(val)]
        except (TypeError, ValueError, IndexError):
            raise Unimplemented("%s 取值 %r 超出 0..%d" % (nm, val, len(names) - 1))

    out["GetEnumeratorUserFriendlyName"] = _enum_user_friendly
    return out


def enumerate_effects(km, card_ptr: int, target_ptr: int = 0, has_target: bool = False,
                      hook: str = "OnPlayedFromHand", my_side=None, cap: int = 8,
                      runner=None, read_hooks: Optional[dict] = None, slots=None,
                      args: Optional[dict] = None, hq_own=(), hq_enemy=(),
                      budget_s: Optional[float] = None, rng_seed: Optional[int] = None,
                      board=None, my_seat=None, field_overrides: Optional[dict] = None,
                      view_overrides: Optional[dict] = None) -> dict:
    """把随机结果**列出来**：先探查有几个可枚举的随机点，再对每种结果组合各空跑一次。

    返回 {"outcomes": [(权重, 效果摘要), ...], "complete": bool, "stopped": ..., "runs": n}。
    没有随机点 ⇒ 只有一个权重 1.0 的分支。各分支等概率（游戏里这些随机都是均匀的整数范围/
    数组随机元素/随机关键词）。`runner` 供测试注入（签名同 `record_effects`）。
    """
    run = runner or record_effects
    kw = {}
    if read_hooks:
        kw["read_hooks"] = read_hooks
    if slots:
        kw["slots"] = slots
    import time as _t
    t_end = (_t.time() + budget_s) if budget_s else None
    if budget_s:
        kw["timeout_s"] = budget_s
    if args:
        kw["args"] = args
    if hq_own or hq_enemy:
        kw["hq_own"], kw["hq_enemy"] = hq_own, hq_enemy
    if rng_seed is not None:
        kw["rng_seed"] = rng_seed                           # 活种子：能确定的随机点直接给出确定结果
    if board is not None:
        kw["board"] = board                                 # 盘面级原语（IsSideActive 等）要用
    if my_seat is not None:
        kw["my_seat"] = my_seat
    if field_overrides:
        kw["field_overrides"] = field_overrides
    if view_overrides:
        kw["view_overrides"] = view_overrides
    first = run(km, card_ptr, target_ptr, has_target, hook, my_side, None, **kw)
    res = {"outcomes": [], "complete": first.get("complete", False),
           "stopped": first.get("stopped"), "runs": 1, "choice": bool(first.get("choice")),
           "exact": bool(first.get("exact")), "draws": first.get("draws", 0)}
    nodes = first.get("nodes") or []
    if not nodes:
        res["outcomes"] = [(1.0, first.get("eff") or {})]
        return res
    combos = _combos([n["size"] for n in nodes], cap)
    outs = []
    for c in combos:
        if t_end is not None and _t.time() > t_end and outs:
            res["complete"] = False                          # 预算用完：只用已跑出的分支，不再展开
            res["stopped"] = res.get("stopped") or "枚举超时（预算已用完）"
            break
        if t_end is not None:
            kw["timeout_s"] = max(0.1, t_end - _t.time())
        r = first if all(i == 0 for i in c) else run(km, card_ptr, target_ptr, has_target,
                                                     hook, my_side, c, **kw)
        res["runs"] += 0 if r is first else 1
        outs.append(r.get("eff") or {})
        res["complete"] = res["complete"] and r.get("complete", False)
    w = 1.0 / len(outs)
    res["outcomes"] = [(w, e) for e in outs]
    return res


# ---------------------------------------------------------------------------
# 离线自检（不碰游戏）：钩子记录/污染/摘要 —— 实现拆到 effectvm_selftest（P6）
# ---------------------------------------------------------------------------
def selftest() -> int:
    from engine.effectvm_selftest import selftest as _selftest          # noqa: PLC0415
    return _selftest()


if __name__ == "__main__":
    import sys
    print("semantics.effectvm 离线自检")
    n = selftest()
    print("失败 %d 项" % n)
    sys.exit(1 if n else 0)
