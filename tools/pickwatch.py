#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pickwatch.py —— 守着"选择界面"的正证据。

抉择 / 预报 / 换牌 的窗口很短（换牌尤其）。本工具高频轮询 kardsmem.pick 的三条判据，
**任何一条变成非零就立刻落盘**：JSON 证据 + 同一时刻的客户区截图 + 候选聚合。

用法:
  python pickwatch.py [秒数] [--interval 0.15]

只读内存 + 截图，不动鼠标。
输出：logs\\pickevidence\\<时间戳>.json / .png
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  —— 接上 kards-agent/ 与 vendor/

OUT = _bootstrap.work_dir("logs", "pickevidence")


def _shot(tag):
    """抓一帧客户区。抓不到就算了 —— 证据的主体是内存，截图只是旁证。"""
    try:
        if not _bootstrap.upstream_src():
            return None     # 上游没 checkout：截图只是旁证，内存证据才是主体
        import win
        import cv2
        win.set_dpi_aware()
        ws = win.find_by_process("kards")
        if not ws:
            return None
        img = win.capture_client_bgr(ws[0]["hwnd"], allow_screen_fallback=True)
        if img is None:
            return None
        p = os.path.join(OUT, tag + ".png")
        cv2.imwrite(p, img)
        return p
    except Exception as e:                                   # noqa: BLE001
        return "截图失败: %s" % e


def _sanitize(o, depth=0):
    """把任意对象整棵树转成可 JSON 的形态：非字符串的键也转成字符串。"""
    if depth > 12:
        return "<深度超限>"
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, dict):
        return {str(k): _sanitize(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_sanitize(v, depth + 1) for v in o]
    d = getattr(o, "__dict__", None)
    if d:
        return {str(k): _sanitize(v, depth + 1) for k, v in d.items()}
    return str(o)


def _nonzero(stt):
    """三条正证据里有没有哪条变成了真。None（读不出）不算。"""
    hits = []
    for k in ("choose_one_active", "is_selecting_hand_target",
              "pc_choose_one_selection", "pc_choose_one_index"):
        v = stt.get(k)
        if v not in (None, 0):
            hits.append("%s=%s" % (k, v))
    if stt.get("pc_choose_one_targets_num"):
        hits.append("pc_choose_one_targets_num=%s" % stt["pc_choose_one_targets_num"])
    if stt.get("pending"):
        hits.append("pending=True")
    return hits


def main(argv=None) -> int:
    a = argv or sys.argv[1:]
    secs = float(a[0]) if a and not a[0].startswith("-") else 600.0
    interval = 0.15
    if "--interval" in a:
        interval = float(a[a.index("--interval") + 1])

    from kardsmem.proc import attach
    from kardsmem import pick as P

    os.makedirs(OUT, exist_ok=True)
    s = attach()
    print("守着选择界面… %.0fs，每 %.2fs 一次。Ctrl-C 停。" % (secs, interval))
    t0 = time.time()
    n, caught, last_note = 0, 0, None
    while time.time() - t0 < secs:
        n += 1
        try:
            stt = P.pick_state(s)
        except Exception as e:                               # noqa: BLE001
            time.sleep(interval)
            continue
        hits = _nonzero(stt)
        if hits:
            caught += 1
            tag = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % (int(time.time() * 1000) % 1000)
            shot = _shot(tag)
            rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "hits": hits,
                   "pick_state": stt, "shot": shot}
            try:
                rec["candidates"] = P.candidates(s)
            except Exception as e:                           # noqa: BLE001
                rec["candidates_error"] = str(e)
            # ⚠ 只加 default=str 不够：它管不了**非字符串的 dict 键**，
            #   candidates() 里就有，于是 json.dump 会在写到一半时抛异常 ——
            #   文件被截断，"真抓到"的那一次证据反而残缺。先整棵树消毒再写。
            path = os.path.join(OUT, tag + ".json")
            try:
                blob = json.dumps(_sanitize(rec), ensure_ascii=False, indent=1)
            except Exception as e:                           # noqa: BLE001
                blob = json.dumps({"t": rec["t"], "hits": hits,
                                   "serialize_error": str(e)}, ensure_ascii=False, indent=1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(blob)
            print("\n★★ 抓到了（第 %d 次）：%s" % (caught, "；".join(hits)))
            print("   证据 %s" % os.path.join(OUT, tag + ".json"))
            print("   截图 %s" % shot)
            time.sleep(0.5)          # 同一个界面别刷屏
            continue
        note = "board=%s actors=%s" % (bool(stt.get("board")), stt.get("level_actors"))
        if note != last_note:
            print("  [%5.0fs] %s  三条判据=%s/%s/%s" % (
                time.time() - t0, note, stt.get("choose_one_active"),
                stt.get("is_selecting_hand_target"), stt.get("pc_choose_one_selection")))
            last_note = note
        time.sleep(interval)
    s.close()
    print("\n结束：轮询 %d 次，抓到 %d 次。" % (n, caught))
    return 0 if caught else 1


if __name__ == "__main__":
    sys.exit(main())
