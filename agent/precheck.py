#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""precheck.py —— 预检 API：**直接问游戏自己的判据**（NN / 三个前端共用）。

为什么要单开一层
================
2026-09-25 用户定调：**预检用游戏自己的总入口**——"比如我们 NN 就要用"。
在那之前预检走的是进程外重算（`agent/legality.py` + `kardsmem/vm.py` 重新解释蓝图字节码）
——那条路依然是对的（一手产物、不用注入、没有副作用），但它**一定有缺漏**，
而且每次都要现算。现在多了一条更直接的：**注入式只读调用**游戏自己的查询函数
（`ops_inject.py`）。按 CLAUDE.md 的红线（2026-09-25 修订版），只读查询调用是允许的，
前提是**无副作用**——这几个函数都不改对局数据。

接口（一层薄封装，只干三件事：单例 attach、构建一致性检查、失败降级）
====================================================================
    from agent import precheck
    precheck.available()                -> bool          # 能不能用注入式预检
    precheck.can_play(card)             -> {'ok','can','source','judged_by'}
    precheck.precheck_play(card)        -> 上面 + {'reason','reason_zh',...}  # 带理由
    precheck.can_do_anything(card)      -> {'ok','can'}
    precheck.can_i_do_anything()        -> {'ok','can'}
    precheck.can_move_to(card, loc=7)   -> {'ok','can'}
    precheck.can_attack(attacker, target) -> {'ok','can','fail_reason'}
    precheck.status() / precheck.close()

`card` 收 `int` 或 `board_api` 的卡对象（有 `.card_id` 就行）。

规矩（跟 `agent/legality.py` 一致，不许漂移）
============================================
* **只挑不判**：这里返回的 `can=False` 是"游戏自己说不行"，**不是否决权**。
  真要动手时照样走完整拖拽（悬停→按下→移动→松开），让游戏在**真实上下文**里再判一次
  （§7.6f：判据不足时仍然把动作发出去）。
* **失败一律降级**：游戏没跑 / 构建对不上 / frida 注不进去 / 调用抛异常 ⇒
  `{'ok': False, 'stopped': ...}`，调用方**必须**能退回 VM 路径。
  `KARDS_GAME_GATE=0` 一句话关掉（出问题时不用改代码）。
* 一次会话只 attach 一次（`_LOC['inj']`）；进程换了（重开游戏）会自动重建。
* ⚠ `can_move_to` **不是纯读**：它按"真实鼠标拖到该行"的中间结果写
  `cardUnderCursor / LocationUnderCursor / RowUnderCursor` 这几个 cursor 字段
  （跟 `_drag_lifecycle` 同性质，写侧红线允许——发出去的还是同一串鼠标事件）。
  其余几个（`CanPlayCardFromHand` / `CanPlayFromHand` / `CanAttack`）是纯查询。
* ★ **构建必须一致，不自动切**：这些函数用 `kardsmem.build` 的偏移表，而 `board_api`
  用**它自己**的表，两张表都由 `KARDS_BUILD` 选。活着的进程 SizeOfImage 跟当前
  `KARDS_BUILD` 不对应时**直接报错**——自动改环境变量会让预检和 `board_api`
  各用一套偏移（读数静默错位，比报错糟得多）。
"""
from __future__ import annotations

import os
from typing import Any, Optional

import agentpath  # noqa: F401,E402  —— 接上 vendor/ 和本项目根

# `ECardLocationEnum` 里跟"移动"有关的三个（跟 `ops_inject.LOC_BOARD_*` 同值）
LOC_BACK_LEFT = 5      # 整个后排（HQ + 支援线），left 侧
LOC_BACK_RIGHT = 6     # 同上，right 侧
LOC_FRONTLINE = 7

# 全局单例。`inj` 为 None = 还没 attach 或已经掉了。
_LOC: dict = {
    "inj": None,       # ops_inject.Injector
    "pid": None,
    "err": None,       # 最近一次失败的原因（人话，给 stopped 用）
    "probe": None,     # kardsmem.proc.list_kards_processes() 的缓存（含 md5，读一次 ~1s）
    "unhealthy": None, # ★ 最近一次**异常**（调用抛异常/重连仍失败）；非 None = 应当整轮停手
}


def enabled() -> bool:
    """注入式预检开没开。`KARDS_GAME_GATE=0` 关（退回进程外 VM）。"""
    return os.environ.get("KARDS_GAME_GATE", "1") != "0"


def _cid(x: Any) -> Optional[int]:
    """`int` 或卡对象（`.card_id`）→ card_id。"""
    if x is None:
        return None
    if isinstance(x, int):
        return x
    v = getattr(x, "card_id", None)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 构建一致性
def _live_probe(refresh: bool = False) -> list:
    if _LOC["probe"] is None or refresh:
        try:
            from kardsmem.proc import list_kards_processes
            _LOC["probe"] = list_kards_processes()
        except Exception as e:                                # noqa: BLE001
            _LOC["probe"] = []
            _LOC["err"] = "列进程失败：%s" % e
    return _LOC["probe"] or []


def build_check(refresh: bool = False) -> Optional[str]:
    """活着的进程是不是当前 `KARDS_BUILD` 那一份？不是 ⇒ 一句人话；是 ⇒ None。

    ★ 只按 **SizeOfImage** 判（它决定 RVA 有没有效）；md5 只在两边都对得上时
      用来辨认"是哪一份副本"，不作否决——见 `kardsmem/exes.py` 的判据分工。
    """
    from kardsmem import build as B
    rows = _live_probe(refresh=refresh)
    if not rows:
        return "没有活着的 kards 进程"
    want = (B.BUILDS.get(getattr(B, "CURRENT", "current")) or {}).get("image_size")
    for r in rows:
        if r.get("image_size") and r.get("image_size") == want:
            return None
    got = "、".join(
        "pid=%s SizeOfImage=%s（表里认作 %s）"
        % (r.get("pid"),
           ("0x%X" % r["image_size"]) if r.get("image_size") else "?",
           r.get("matched") or "未知")
        for r in rows)
    return ("活着的进程跟当前 KARDS_BUILD=%s（SizeOfImage=%s）**不是同一份构建**：%s"
            " —— 用对的那个 KARDS_BUILD 重启本进程再试"
            % (getattr(B, "CURRENT", "current"),
               ("0x%X" % want) if want else "?", got))


# ------------------------------------------------------------------ attach 单例
def _ensure():
    """拿到（必要时建）注入器。失败返回 None，原因写进 `_LOC['err']`。"""
    if _LOC["inj"] is not None:
        return _LOC["inj"]
    if not enabled():
        _LOC["err"] = "KARDS_GAME_GATE=0（注入式预检被关掉）"
        return None
    bad = build_check()
    if bad:
        _LOC["err"] = bad
        return None
    try:
        from ops_inject import Injector
        inj = Injector()                     # 内部 attach(require_build=False)
        _LOC["inj"], _LOC["pid"], _LOC["err"] = inj, inj.ks.pid, None
        return inj
    except Exception as e:                                    # noqa: BLE001
        _LOC["err"] = "注入失败：%s" % e
        return None


def _drop() -> None:
    inj = _LOC.get("inj")
    _LOC["inj"], _LOC["pid"] = None, None
    if inj is not None:
        try:
            inj.close()
        except Exception:                                     # noqa: BLE001
            pass


def close() -> None:
    """detach（进程退出时也会自动，`Injector` 自己注册了 atexit）。"""
    _drop()


def _call(fn: str, *args, **kw) -> dict:
    """跑一次注入式查询。任何失败 ⇒ `{'ok': False, 'stopped': ...}`。

    失败会先丢掉注入器**重试一次** —— 游戏重启过的话 pid 就变了，
    直接复用旧的 frida 连接只会一直报错。
    """
    last = None
    for attempt in (1, 2):
        inj = _ensure()
        if inj is None:
            _LOC["unhealthy"] = _LOC.get("err") or "注入不可用"
            return {"ok": False, "stopped": _LOC.get("err") or "注入不可用",
                    "injector_unhealthy": True}
        try:
            r = getattr(inj, fn)(*args, **kw)
            _LOC["unhealthy"] = None          # 这次成功 ⇒ 清掉异常标记
            return r
        except Exception as e:                                # noqa: BLE001
            last = "%s 调用失败：%s" % (fn, e)
            # ★★ 2026-09-25 22:21 事故（frida-agent fail-fast、游戏进程消失）之后加的：
            #   调用**抛异常**不是"这次没问出来"，是**注入侧已经不健康**。
            #   把它记下来，调用方必须用 `healthy()` 判断并**整轮停手**，
            #   而不是继续一条一条发动作。事故当时的 `can=None` 就是这个状态，
            #   我没停手，两分钟内游戏就崩了。
            _LOC["unhealthy"] = "%s 抛异常（第 %d 次尝试）：%s" % (fn, attempt, e)
            _drop()
    _LOC["err"] = last
    return {"ok": False, "stopped": last, "injector_unhealthy": True}


def call_read(fn: str, *args, **kw) -> dict:
    """跑一次**只读**注入查询（跟 `call_write` 同一套单例/重试/健康判据）。

    与 `call_write` 只有语义差别（读 vs 写），实现完全一样：
    `agent` 层不该自己管 attach，也不该绕过健康判据。
    """
    return _call(fn, *args, **kw)


def call_write(fn: str, *args, **kw) -> dict:
    """跑一次**写侧动作**（play / attack / confirm / move …），跟 `_call` 同一套单例与健康检查。

    ★ 为什么写侧也走这里：事故教训是"注入侧一抛异常就整轮停手"
      （`_call` 的注释、handoff §十六）。**写动作更需要这个判据** ——
      带着坏掉的注入状态继续写，正是那次崩局的成因。
    ★ 参数里**不要**传 `verbose=` 之外的东西给不支持的动词；`fn` 必须是
      `ops_inject.Injector` 上的方法名（`agent` 层不认识具体实现，只转发）。
    """
    return _call(fn, *args, **kw)


def healthy() -> bool:
    """最近一次注入调用有没有出事。**False ⇒ 立刻停手**（别再发任何动作）。

    ★ 判据：`_call` 里**抛异常**才算不健康；"问出来是 False"（游戏说不行）不算。
      事故复盘见 `reports/report/OPS-INJECT-HANDOFF.md` §十六。
    """
    return _LOC.get("unhealthy") is None


def unhealth_reason() -> Optional[str]:
    """不健康的原因（`healthy()` 为 True 时是 None）。"""
    return _LOC.get("unhealthy")


def available(refresh: bool = False) -> bool:
    """注入式预检能不能用。**会 attach**（首次几秒，之后走缓存/进程内单例）。"""
    if _LOC["inj"] is not None and not refresh:
        return True
    if refresh:
        _drop()
    return _ensure() is not None


def status(refresh: bool = False) -> dict:
    """诊断用：开关、活着的进程、注入器 pid、最近一次错误、是否健康。"""
    return {"enabled": enabled(),
            "attached": _LOC["inj"] is not None,
            "pid": _LOC["pid"],
            "healthy": healthy(),
            "unhealth_reason": _LOC.get("unhealthy"),
            "build_error": build_check(refresh=refresh),
            "processes": [{"pid": r.get("pid"), "image_size": r.get("image_size"),
                           "matched": r.get("matched"), "path": r.get("path")}
                          for r in _live_probe(refresh=refresh)],
            "last_error": _LOC["err"]}


# ------------------------------------------------------------------ 问游戏
def can_play(card, verbose: bool = False) -> dict:
    """打牌**总闸**：`BP_Logic_C::CanPlayCardFromHand(CardID, bool* Yes)`（bool-only）。

    含指挥点 / 支援线满 / 全局打法限制 / 天气与自定义能力特例 / 卡自身 CanPlayFromHand
    / CanSelectAsTarget（指向）。**没有理由**——要理由用 `precheck_play()`。
    """
    cid = _cid(card)
    if cid is None:
        return {"ok": False, "stopped": "拿不到 card_id"}
    r = _call("game_can_play_card_from_hand", cid, verbose=verbose)
    if r.get("ok"):
        r["judged_by"] = r.get("source")
    return r


def precheck_play(card, verbose: bool = False) -> dict:
    """总闸 + 下钻取理由（**NN/前端预检入口**）。

    `reason` 是 `not_enough_kredits` / `supply_line_full` / 或游戏自己给的
    理由码（如 `enemy_unit` = 要指向目标却没指）。
    """
    cid = _cid(card)
    if cid is None:
        return {"ok": False, "stopped": "拿不到 card_id"}
    r = _call("precheck_play", cid, verbose=verbose)
    if r.get("ok"):
        r["judged_by"] = "game:BP_Logic::CanPlayCardFromHand"
    return r


def can_do_anything(card, verbose: bool = False) -> dict:
    """`BP_Logic_C::CanCardDoAnything(CardID, bool* canIt)`：这张牌这回合还能不能做事。"""
    cid = _cid(card)
    if cid is None:
        return {"ok": False, "stopped": "拿不到 card_id"}
    return _call("game_can_card_do_anything", cid, verbose=verbose)


def can_i_do_anything(verbose: bool = False) -> dict:
    """`BP_Logic_C::CanIDoAnything(bool* canI)`：我方还有没有事可做（决定要不要结束回合）。"""
    return _call("game_can_i_do_anything", verbose=verbose)


def can_move_to(card, location_enum: int = LOC_FRONTLINE, verbose: bool = False,
                simulate_drag: bool = False) -> dict:
    """`BattleUtilityFunctions_C::CanMoveCardToLocation(ECardLocationEnum, __WorldContext, bool*)`。

    `location_enum`：`LOC_FRONTLINE`(7) / `LOC_BACK_LEFT`(5) / `LOC_BACK_RIGHT`(6)。
    ★★ 2026-09-27：**默认 `simulate_drag=False` = 不写任何字段、也不给答案**
    （返回 `reason="cannot_ask_without_drag"`, `can=None`）。因为
    `simulate_drag=True` 要先临时写 `PlayerController->SelectedCard // 0x0950`
    —— 那是**手柄/拖拽态字段**，和我们明令去掉的"伪造 PC 拖拽状态"是同一类
    （`click-crash-report.md` 的 use-after-free 就是这么来的）；而**没有拖拽态时
    这个函数恒回 False**，那个 False 不是判据（是假判据）。
    ⇒ 上线合不合法**由游戏在提交那一刻判**（`ops_inject.move_to_front`），
    要诊断再显式传 `simulate_drag=True`（结果里会标 `writes_gamepad_field`）。
    """
    cid = _cid(card)
    if cid is None:
        return {"ok": False, "stopped": "拿不到 card_id"}
    return _call("can_move_to", cid, location_enum, verbose=verbose,
                 simulate_drag=simulate_drag)


def can_attack(attacker, target, verbose: bool = False) -> dict:
    """`cardsCheckFunctions_C::CanAttack(...)`：带 `fail_reason` 的攻击判据。

    `fail_reason` 举例：`not_enough_kredits_to_target`（打这个目标要多花指挥点，
    比如烟幕/重甲）、`not_enough_range`（战斗机拦截 / 射程不够）。
    """
    a, t = _cid(attacker), _cid(target)
    if a is None or t is None:
        return {"ok": False, "stopped": "拿不到 attacker/target 的 card_id"}
    return _call("game_can_attack", a, t, verbose=verbose)


def can_select_as_target(targeted, targeting, by_play_from_hand: bool = True,
                         verbose: bool = False) -> dict:
    """`cardsCheckFunctions_C::CanSelectAsTarget(Targeted, Targeting, byPlayFromHand, …)`。

    "正在打的那张牌（`targeting`）能不能指向 `targeted`" —— 带目标出牌的预检。

    ⚠ 两条实测提醒（都写在 `ops_inject.game_can_select_as_target` 的注释里）：
      * 它**很宽松**（`targetOverride` 那条路才是真判断）⇒ 别把它的 True 当成"一定能打"；
      * `__WorldContext` 必须非 0（函数内部靠它取 `BP_Logic_C` 读指挥点），
        否则**任何**目标都回 `play_from_hand_not_enough_kredits_to_target`。
    """
    a, b = _cid(targeted), _cid(targeting)
    if a is None or b is None:
        return {"ok": False, "stopped": "拿不到 targeted/targeting 的 card_id"}
    return _call("game_can_select_as_target", a, b, by_play_from_hand, verbose=verbose)
