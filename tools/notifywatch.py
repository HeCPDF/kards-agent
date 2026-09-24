#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notifywatch.py —— 游戏提示文本的**实机取材工具**（只读）。

为什么要有它（规格 §11.2 的头号 P0）
====================================
提示文本是**动作回执的权威来源**，也是唯一能公示「对手做了什么」的通道
（抽牌、疲劳伤害、回合开始都走它，不只是「你这步不行」）。但它**短命**：
淡入淡出完 widget 就销毁，事后一个字都捞不回来 ⇒ 只能轮询，而轮询有两个未知数：

  1. **一次扫描要多久？** `NotifyWatcher.live()` 现在是**扫一遍 GUObjectArray**
     再按 `PropertiesSize` 认身份。转储上实测 2.7 秒。真这么慢就做不了事件流。
  2. **提示活多久？** 不知道存活时长就定不出安全的轮询间隔。

这个工具就是去量这两个数的，外加找第三条路：

  3. **提示挂在谁下面？** 如果所有 `NotifyTextWidget_C` 共用一个容器 widget，
     那就读容器的 children 数组，把 O(全部对象) 降成 O(1) —— 问题从根上消失。
     `--outer` 就是去看这条链的（`UObject::Outer @0x20`）。

用法
====
    python tools/notifywatch.py --bench            # 量一次扫描多久（先跑这个）
    python tools/notifywatch.py --outer            # 看提示挂在哪个容器下
    python tools/notifywatch.py                    # 盯着看，退出时给存活时长统计
    python tools/notifywatch.py --interval 0.15 --seconds 120 --jsonl out.jsonl

★ 只读。全程 `ReadProcessMemory`，不写、不注入、不 hook。
★ 边跑边在游戏里做点会弹提示的事：出牌、非法攻击、结束回合、让对手抽牌。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

OFF_UOBJECT_OUTER = 0x20        # UObject::Outer（Basic/CoreUObject_classes.hpp 确认）


def _session(dump=None):
    if dump:
        from dumpmem import DumpSession
        return DumpSession(dump)
    from kardsmem import attach
    return attach()


# ------------------------------------------------------------------ bench
def bench(s, n=3):
    """量 `live()` 一次要多久。这是「能不能做事件流」的决定性数字。"""
    from kardsmem.notify import NotifyWatcher
    from kardsmem.objects import ObjectArray
    w = NotifyWatcher(s)

    t0 = time.perf_counter()
    uc = w._locate_class()          # noqa: SLF001 —— 取材工具，允许看内部
    t_locate = time.perf_counter() - t0
    print("定位 NotifyTextWidget_C：%.2fs → UClass=%s  PropertiesSize=%s  Text@%s"
          % (t_locate, hex(uc) if uc else None, w._propsize,      # noqa: SLF001
             hex(w.text_offset() or 0)))
    if not uc:
        print("★ 没找到 NotifyTextWidget_C。游戏在主菜单时这个类可能还没实例化 —— "
              "进对局再跑。")
        return

    print("对象总数：%s" % ObjectArray(s).count())

    t0 = time.perf_counter()
    hud = w._locate_hud()                                    # noqa: SLF001
    print("定位 HUD（BP_Widget_Battle_HUD_PC_C）：%.2fs → %s  CurrentNotifyPlayer@%s"
          % (time.perf_counter() - t0, hex(hud) if hud else "没找到（不在对局里？）",
             hex(w._off_current) if w._off_current else "?"))  # noqa: SLF001

    for name, fn in (("慢路 live_scan()", w.live_scan),
                     ("快路 current()", w.current) if hud else (None, None)):
        if fn is None:
            continue
        ts = []
        for i in range(n):
            t0 = time.perf_counter()
            rows = fn()
            ts.append(time.perf_counter() - t0)
            print("  %s 第 %d 次：%.4fs，%d 条" % (name, i + 1, ts[-1], len(rows)))
        med = statistics.median(ts)
        print("  ⇒ %s 中位 %.4fs（%.1f Hz）\n" % (name, med, 1.0 / med if med else 0))

    if not hud:
        print("★ 只有慢路可用（2~4s/次）—— 做不了事件流，只够"
              "「动作发出后看一眼」。进对局再跑。")


# ------------------------------------------------------------------ outer
def outer_chain(s, depth=6):
    """打印每条活着的提示 widget 的 Outer 链 —— 找共同的容器。"""
    from kardsmem.notify import NotifyWatcher
    from kardsmem.objects import ObjectArray
    w = NotifyWatcher(s)
    oa = ObjectArray(s)
    pool = oa.pool()
    rows = w.live()
    if not rows:
        print("现在一条提示都没有。★ 提示是短命的 —— 一边跑这个，一边在游戏里"
              "做点会弹提示的事（出牌 / 非法攻击 / 结束回合）。")
        return
    for r in rows:
        print("\n提示 %#x  %r" % (r["addr"], r["text"][:60]))
        p = r["addr"]
        for lvl in range(depth):
            nm = pool.fname_of(p)
            cn = oa.class_name(p)
            print("   %s%-28s : %s   @%#x" % ("  " * lvl, nm, cn, p))
            p = s.m.ptr_or_zero(p + OFF_UOBJECT_OUTER)
            if not p:
                break


# ------------------------------------------------------------------ watch
def crosscheck(s, seconds, fast_interval=0.05, scan_every=3.0):
    """快路 vs 慢路的**交叉核对**：全扫描看得到、而快路看不到的，就是快路漏的。

    为什么需要这一步
    ================
    快路读的是 `BP_Widget_Battle_HUD_PC_C::CurrentNotifyPlayer`。但 SDK 里
    `BP_Logic` / `BP_Board` / `BP_PlayerMoves` / `BP_KardsSession` / `BP_PopupManager`
    **各自**都有 `CallFunc_NotifyPlayer_theWidget` 这个局部 —— 它们创建的 widget
    未必会写进 HUD 的 `CurrentNotifyPlayer`。
    ⇒ **"快路能看到全部提示"是个假设，没验过。** 这个函数就是去证伪它的。

    慢路一次 ~2s，而实测可读窗口 25~37s ⇒ 每 3s 扫一次完全来得及。
    """
    from kardsmem.notify import NotifyWatcher
    w = NotifyWatcher(s)
    hud = w._locate_hud()                                    # noqa: SLF001
    print("HUD=%s；快路 %.2fs 一次，慢路 %.1fs 一次，共 %ss"
          % (hex(hud) if hud else "没有", fast_interval, scan_every, seconds))
    fast_seen, scan_seen, missed = set(), set(), []
    t0 = time.time()
    t_scan = 0.0
    try:
        while time.time() - t0 < seconds:
            t = time.time()
            for r in w.current():
                k = (r["addr"], r["text"])
                if k not in fast_seen:
                    fast_seen.add(k)
                    print("[%6.2fs] 快 + %r" % (t - t0, r["text"]))
            if t - t0 >= t_scan:
                t_scan = (t - t0) + scan_every
                ts = time.perf_counter()
                rows = w.live_scan()
                dt = time.perf_counter() - ts
                for r in rows:
                    k = (r["addr"], r["text"])
                    if k in scan_seen:
                        continue
                    scan_seen.add(k)
                    tag = "慢 +" if k in fast_seen else "慢 + ★快路没看到★"
                    print("[%6.2fs] %s %r   (扫描 %.2fs)" % (t - t0, tag, r["text"], dt))
                    if k not in fast_seen:
                        missed.append(r)
            time.sleep(max(0.0, fast_interval - (time.time() - t)))
    except KeyboardInterrupt:
        print("\n(中断)")
    print("\n---- 交叉核对 ----")
    print("快路捕到 %d 条，慢路捕到 %d 条" % (len(fast_seen), len(scan_seen)))
    if missed:
        print("★ 快路漏了 %d 条 ⇒ `CurrentNotifyPlayer` **不是全部提示的入口**，"
              "得再找别的把手（或者两路并用）：" % len(missed))
        for r in missed:
            print("   %#x  %r" % (r["addr"], r["text"]))
    elif scan_seen:
        print("这一轮快路没漏。（样本还少，别据此下定论。）")
    else:
        print("这一轮一条提示都没有 —— 什么都证明不了，重来。")


def watch(s, interval, seconds, jsonl=None):
    """盯着提示，记录每条的**首见/末见**，退出时给统计。

    ★ 2026-09-23 实机后更正「存活时长」的含义：
      走快路时读的是 HUD 的 `CurrentNotifyPlayer`，它是**单数**——
      一条提示会一直挂在那儿，**直到下一条把它顶掉**。
      所以这里量到的不是"提示在屏幕上淡出的时间"，而是
      **「两条提示之间的间隔」**（实测就出现过：某条的"消失"时刻和下一条的
      "出现"时刻**完全同一帧**）。

      ⇒ 真正的漏事件风险不是"淡得太快"，而是**两条提示挨得太近**：
        间隔小于一个轮询周期就会整条看不见。快路一次读 <0.1ms，
        所以间隔尽管往小了取（0.05s），成本可以忽略。
    """
    from kardsmem.notify import NotifyWatcher
    w = NotifyWatcher(s)
    if not w._locate_class():                                # noqa: SLF001
        print("没找到 NotifyTextWidget_C —— 进对局再跑。")
        return
    seen = {}            # addr -> {text, first, last}
    done = []
    fh = open(jsonl, "a", encoding="utf-8") if jsonl else None
    t0 = time.time()
    scan_ts = []
    print("盯着提示…… Ctrl-C 结束。间隔 %.2fs，最多 %ss" % (interval, seconds))
    try:
        while time.time() - t0 < seconds:
            t = time.time()
            ts = time.perf_counter()
            rows = w.poll()          # ★ 事件流走 poll()：增量扫描 + current() + 周期兜底
            scan_ts.append(time.perf_counter() - ts)
            for r in rows:
                a = r["addr"]
                seen[a] = {"addr": a, "text": r["text"], "first": t, "last": t,
                           "manual_remove": r["manual_remove"]}
                print("[%7.2fs] + [%s] %s%s" % (t - t0, r.get("via", "?"), r["text"],
                                                "  (manual_remove)" if r["manual_remove"] else ""))
                if fh:
                    fh.write(json.dumps({"t": t, "text": r["text"],
                                         "addr": "%#x" % a}, ensure_ascii=False) + "\n")
                    fh.flush()
            # 已经报过的，看它什么时候消失（文本变了/读不出就算没了）
            for a in list(seen):
                if w._widget_text(a) == seen[a]["text"]:          # noqa: SLF001
                    seen[a]["last"] = t
                    continue
                rec = seen.pop(a)
                rec["life"] = rec["last"] - rec["first"] + interval
                done.append(rec)
                print("[%7.2fs] - %s   可读 ≥%.2fs" % (t - t0, rec["text"][:40], rec["life"]))
            time.sleep(max(0.0, interval - (time.time() - t)))
    except KeyboardInterrupt:
        print("\n(中断)")
    finally:
        if fh:
            fh.close()
    lives = [r["life"] for r in done if "life" in r]
    print("\n---- 统计 ----")
    print("扫描耗时：中位 %.3fs  最大 %.3fs（%d 次）"
          % (statistics.median(scan_ts) if scan_ts else 0,
             max(scan_ts) if scan_ts else 0, len(scan_ts)))
    if not lives:
        print("没捕到完整的一条提示的生灭。★ 如果确实弹过提示却没捕到，"
              "多半是**扫描比提示还慢**（先跑 `--bench`）。")
        return
    lives.sort()
    print("捕到 %d 条，每条**可读窗口**（＝到下一条把它顶掉为止）："
          "最短 %.2fs  中位 %.2fs  最长 %.2fs" % (len(lives), lives[0],
                                                statistics.median(lives), lives[-1]))
    print("⇒ 轮询间隔要明显小于最短可读窗口（%.2fs）。快路一次读 <0.1ms，"
          "所以 0.05s 就行，成本可忽略。" % lives[0])
    med = statistics.median(scan_ts) if scan_ts else 0
    if med > lives[0] / 3.0:
        print("★ 但现在一次读要 %.2fs，和可读窗口同量级 ⇒ **会漏事件**。"
              "确认走的是快路（`--bench` 看 current() 那一行）。" % med)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="游戏提示文本的实机取材（只读）")
    ap.add_argument("--dump", help="minidump 路径；不给就 attach 活进程")
    ap.add_argument("--bench", action="store_true", help="量一次 live() 扫描多久")
    ap.add_argument("--outer", action="store_true", help="打印提示 widget 的 Outer 链")
    ap.add_argument("--crosscheck", action="store_true",
                    help="快路 vs 慢路交叉核对：证伪「快路能看到全部提示」")
    ap.add_argument("--interval", type=float, default=0.20)
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--jsonl", help="把每条提示追加落盘")
    a = ap.parse_args(argv)

    s = _session(a.dump)
    if a.bench:
        bench(s)
    elif a.outer:
        outer_chain(s)
    elif a.crosscheck:
        crosscheck(s, a.seconds)
    else:
        watch(s, a.interval, a.seconds, a.jsonl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
