#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rowcalib.py —— 行模型的实机标定采样与拟合。

行模型（ops.ROW_SPECS）的 center/pitch 是早期目测的。本工具把**每次 OCR 与内存
张数一致**时的实测 x 存下来，攒够样本后用最小二乘重新拟合：

    x(rank, n) = center + (rank - (n-1)/2) * pitch

用法:
  python rowcalib.py sample            # 采一帧（不动鼠标），追加到 logs/rowcalib.jsonl
  python rowcalib.py watch [秒]        # 每 3 秒采一帧，持续到超时
  python rowcalib.py fit               # 拟合并打印建议的 ROW_SPECS
  python rowcalib.py stats             # 看已攒到的样本分布

⚠ 只有 source == "ocr-confirmed" 的点才入样本（OCR 框数 == 内存卡数才可信）。
⚠ 含总部的行实测**不等距**（总部卡宽度与单位卡不同）⇒ fit 会分别报"含 HQ / 不含 HQ"
  两组残差，残差差得多就说明单一 pitch 不够，需要给 HQ 单列宽度。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = r"D:\Kards\reverse-data\logs\rowcalib.jsonl"


def _ops():
    import ops
    return ops


def sample(note=None) -> int:
    ops = _ops()
    # ★ 选择界面（预报/抉择/发现）会把三张大卡盖在 y≈380，正好压在前线那一排
    #   （前线 y=350）。这时 OCR 框注入的是面板上的卡，不是场上的单位。
    #   "ocr 框数 == 内存卡数" 这道门拦不住它（数量碰巧相等就滑过去了），
    #   所以在源头拒采。踩过一次：一批前线 n>=3 样本因此报废
    #   （logs/rowcalib.jsonl.suspect-20260922）。
    try:
        ps = ops.pick_state()
    except Exception:                                        # noqa: BLE001
        ps = None
    if ps and ps.get("choose_active"):
        print("选择界面开着（chooseOneActive=1）—— 面板盖在前线上，这帧不采")
        return 3
    try:
        h, f, st, field = ops.read_all()
    except Exception as e:                                   # noqa: BLE001
        print("读不到盘面：%s" % e)
        return 2
    d, warn = ops.screen_map_detailed(st, field)
    if not d:
        print("盘面上没有卡")
        return 1
    by_id = {c.card_id: c for c in st.cards}
    rows = {}
    for cid, v in d.items():
        rows.setdefault(v["row"], []).append((cid, v))
    n_new = 0
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        for row, items in rows.items():
            items.sort(key=lambda t: t[1]["rank"])
            if any(v["source"] != "ocr-confirmed" for _, v in items):
                continue                      # 整行都不可信就整行丢掉
            c0 = by_id.get(items[0][0])
            has_hq = any((by_id.get(cid) is not None
                          and by_id[cid].location == "hq") for cid, _ in items)
            rec = {
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "row": row,
                "n": items[0][1]["n"],
                "has_hq": has_hq,
                "pts": [{"rank": v["rank"], "x": v["x_ocr"],
                         "is_hq": (by_id.get(cid) is not None
                                   and by_id[cid].location == "hq"),
                         "name": (by_id[cid].name if by_id.get(cid) else None)}
                        for cid, v in items],
            }
            if note:
                rec["note"] = note
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_new += 1
            print("  + %-11s n=%d has_hq=%-5s x=%s" % (
                row, rec["n"], has_hq, [p["x"] for p in rec["pts"]]))
    if not n_new:
        print("这一帧没有可信行（OCR 框数与内存卡数都对不上）")
        for w in warn:
            print("    %s" % w)
    return 0


def watch(seconds=180) -> int:
    t0 = time.time()
    while time.time() - t0 < seconds:
        print("[t=%4ds]" % int(time.time() - t0))
        sample()
        time.sleep(3.0)
    return 0


def _load():
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:                            # noqa: BLE001
                    pass
    return out


def stats() -> int:
    recs = _load()
    print("样本 %d 行（%s）" % (len(recs), LOG))
    agg = {}
    for r in recs:
        agg.setdefault((r["row"], r["n"], r["has_hq"]), 0)
        agg[(r["row"], r["n"], r["has_hq"])] += 1
    for k in sorted(agg, key=str):
        print("   row=%-11s n=%-2d has_hq=%-5s x%d" % (k[0], k[1], k[2], agg[k]))
    return 0


def _fit(points):
    """最小二乘解 x = center + u * pitch，u = rank - (n-1)/2。→ (center, pitch, 最大残差)"""
    n = len(points)
    if n < 2:
        return None
    su = sum(u for u, _ in points)
    sx = sum(x for _, x in points)
    suu = sum(u * u for u, _ in points)
    sux = sum(u * x for u, x in points)
    den = n * suu - su * su
    if abs(den) < 1e-9:
        return None
    pitch = (n * sux - su * sx) / den
    center = (sx - pitch * su) / n
    resid = max(abs(x - (center + u * pitch)) for u, x in points)
    return center, pitch, resid


def fit() -> int:
    recs = _load()
    if not recs:
        print("还没有样本。先在对局里跑 `python rowcalib.py watch 300`。")
        return 1
    print("样本 %d 行\n" % len(recs))
    for row in ("enemy_back", "frontline", "local_back"):
        rs = [r for r in recs if r["row"] == row]
        if not rs:
            print("== %s ==  无样本" % row)
            continue
        # pitch 只有 n>=3 的行才约束得住（单独一行 n<=2 是 2 点定 2 参数，残差恒 0，
        # 那不是拟合而是重述）。但 n==1 的行是**最干净的 center 测量**（u=0），
        # 要一起入池 —— 把它们扔掉等于浪费掉大半样本。
        usable = rs
        has_span = any(r["n"] >= 3 for r in rs)
        if not has_span:
            print("== %s ==  还没有 n>=3 的行（共 %d 行）⇒ pitch 不可辨识，继续采\n"
                  % (row, len(rs)))
            continue
        groups = {"全部": usable,
                  "仅 n==1(测中心)": [r for r in usable if r["n"] == 1],
                  "仅 n>=3(测间距)": [r for r in usable if r["n"] >= 3]}
        print("== %s ==（可用样本 %d / 共 %d 行）" % (row, len(usable), len(rs)))
        for label, g in groups.items():
            pts = []
            for r in g:
                for p in r["pts"]:
                    pts.append((p["rank"] - (r["n"] - 1) / 2.0, p["x"]))
            res = _fit(pts)
            if res is None:
                if pts and all(abs(u) < 1e-9 for u, _ in pts):
                    xs = [x for _, x in pts]
                    # u 全为 0（全是 n==1 的行）⇒ pitch 不可辨识，但 center 测得最准
                    print("   %-14s center=%-7.1f pitch=不可辨识  跨度=%d..%d  (%d 点)"
                          % (label, sum(xs) / len(xs), min(xs), max(xs), len(xs)))
                else:
                    print("   %-14s 点数不足（%d 点）" % (label, len(pts)))
                continue
            c, p_, mr = res
            print("   %-8s center=%-7.1f pitch=%-7.1f 最大残差=%.1fpx  (%d 点)"
                  % (label, c, p_, mr, len(pts)))
        pts = [(p["rank"] - (r["n"] - 1) / 2.0, p["x"]) for r in usable for p in r["pts"]]
        res = _fit(pts) if len(pts) >= 3 else None
        if res:
            c, p_, mr = res
            print("   → 建议 ROW_SPECS[%r]: center=%d pitch=%d  （最大残差 %.1fpx）"
                  % (row, round(c), round(p_), mr))
            if mr > 20:
                print("      ⚠ 残差偏大：这一行可能不是等距居中，或 OCR 框中心有系统偏差")
        print()
    return 0


def main(argv=None) -> int:
    a = (argv or sys.argv[1:]) or ["sample"]
    cmd = a[0]
    if cmd == "sample":
        return sample(a[1] if len(a) > 1 else None)
    if cmd == "watch":
        return watch(int(a[1]) if len(a) > 1 else 180)
    if cmd == "fit":
        return fit()
    if cmd == "stats":
        return stats()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
