# -*- coding: utf-8 -*-
"""player.play —— 在已 attach 的会话里把规则策略（`player.rule.RuleV2`）接上在线回路（`player.loop.Loop`）跑 N 个我方回合。

拆出来的原因：`rule`（决策）与 `loop`（回路）互相引用会成环；`play` 同时需要两者，放在它们上面一层。
用法同旧的 `rule.play`：`from player import play as P; P.play(sess, live=True, turns=40)`。
"""
from __future__ import annotations

from typing import Optional

from player.cardtable import CardTable
from player.loop import Loop
from player.rule import RuleV2


def play(sess, live: bool = False, turns: int = 1, max_actions: int = 80,
         seed: Optional[int] = None, verbose: bool = True, avoid=(), forbid=(), should_stop=None) -> dict:
    """在**已经 attach 好的会话**里跑 N 个我方回合。默认 dry-run（只打印，不动手）。

    ★ 只能这样用：`tools/live_session.py` 这种**常驻监听进程**里一行命令调用。
      **不要**为它单独起一个短命 python 进程再退出 —— 进程退出会卸载 frida agent，
      实机会把游戏带崩（`frida-agent.dll_unloaded` 0xc0000005；DeepSeek 那边触发过，
      常驻监听器这条路从来没触发）。本函数自己不 attach、不 detach、不 exit。

    改了 rule.py 想重跑：在监听器里 `importlib.reload(player.rule)` 即可，
    不必重启监听器，更不必重启游戏客户端。

        import importlib, player.rule as R; importlib.reload(R)
        player.play.play(sess, live=False, turns=1)          # 先看它打算怎么打
        player.play.play(sess, live=True, turns=1)           # 真打一个回合
    """
    pol = RuleV2(sess, table=CardTable.load())
    pol.avoid = set(avoid or ())       # 按牌名回避（已知会让注入调用出访问违规的牌）
    pol.forbid_kinds = set(forbid or ())   # 整类动作禁用（如诊断攻击时 forbid=("attack",)）
    timing = {"seed": seed, "max_turns": turns}
    loop = Loop(sess, pol, live=live, max_actions=max_actions, timing=timing,
                max_rejects=4, verbose=verbose)
    # ★ 逐步落盘：游戏崩了（2026-09-30 13:35 attack_card 在途时）`run()` 的返回值整个丢，
    #   事后无从知道崩在哪一步。每步一行 JSONL，fsync，崩了也留得住。
    import json
    import os
    import time as _time
    from base import paths as _paths_play
    log_dir = _paths_play.NN_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    fp = open(os.path.join(log_dir, "rule-live-%s.jsonl" % _time.strftime("%Y%m%d-%H%M%S")),
              "a", encoding="utf-8")
    orig_step = loop.step

    import faulthandler
    wd = open(os.path.join(log_dir, "rule-watchdog.txt"), "a", encoding="utf-8")

    def _step():
        # GUI/外部「立即停」：每步开始前问一次（`should_stop()` 为真 ⇒ 本局收手，不再动任何东西）
        if should_stop is not None:
            try:
                if should_stop():
                    loop.done = True
                    return None
            except Exception:                                     # noqa: BLE001
                pass
        # 看门狗：单步超过 45 秒没返回就把所有线程的栈写进 rule-watchdog.txt（卡在哪一行一目了然）
        faulthandler.dump_traceback_later(45, repeat=True, file=wd)
        try:
            rec = orig_step()
        finally:
            faulthandler.cancel_dump_traceback_later()
        if rec is not None:
            try:
                d = dict(rec.__dict__)
                d["probe"] = getattr(pol, "probe", None)
                fp.write(json.dumps(d, ensure_ascii=False, default=str) + chr(10))
                fp.flush()
                os.fsync(fp.fileno())
            except Exception:                                     # noqa: BLE001
                pass
        return rec

    loop.step = _step
    try:
        return loop.run()
    finally:
        fp.close()
        wd.close()
