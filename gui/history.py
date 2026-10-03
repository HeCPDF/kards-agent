# -*- coding: utf-8 -*-
"""把 `rule-live-*.jsonl`（每步一行）整理成面板能直接显示的决策历史。"""
from __future__ import annotations

import glob
import json
import os

from base import paths as _paths_h
LOG_DIR = _paths_h.NN_LOG_DIR


def list_logs(log_dir: str = LOG_DIR) -> list:
    """新→旧。"""
    return sorted(glob.glob(os.path.join(log_dir, "rule-live-*.jsonl")), reverse=True)


def _short_eff(probe) -> str:
    es = (probe or {}).get("eff_src") or {}
    if not es:
        return ""
    kinds = {}
    for v in es.values():
        kinds[v] = kinds.get(v, 0) + 1
    return " ".join("%s×%d" % (k, n) for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))


def row_of(d: dict, idx: int) -> dict:
    a = d.get("action") or {}
    ex = d.get("extra") or {}
    pr = d.get("probe") or ex.get("probe") or {}          # 实机日志里 probe 是行的顶层键（rule.play 落盘时挂的）
    res = d.get("result") or {}
    return {
        "i": idx,
        "t": d.get("t"),
        "turn": d.get("turn"),
        "phase": d.get("phase"),
        "kredits": d.get("kredits"),
        "kind": a.get("kind"),
        "card": a.get("card"),
        "target": a.get("target"),
        "score": a.get("score"),
        "note": a.get("note") or d.get("note") or "",
        "executed": d.get("executed"),
        "ok": res.get("ok") if isinstance(res, dict) else None,
        "error": (res.get("error") if isinstance(res, dict) else None),
        "t_decide": ex.get("t_decide"),
        "t_pre": ex.get("t_pre"),
        "t_sim": ((pr.get("timing") or {}).get("build_sim_s")),      # 建模拟场面（B1：决策耗时的大头）
        "t_exec": ex.get("t_exec"),
        "gap_names": sorted((pr.get("gaps") or {}).keys()),
        "ui_tail": (res.get("ui_tail") if isinstance(res, dict) else None),     # 换牌确认后补做的 UI 收尾
        "eff": _short_eff(pr),
        "gaps": len(pr.get("gaps") or {}),
        "raw": d,
    }


def diagnostics(rows: list) -> dict:
    """最近一步的诊断摘要（面板“诊断”行）：模拟耗时、缺口名、最近一次换牌收尾、本局模拟耗时均值/最大。"""
    out = {"t_sim": None, "gap_names": [], "ui_tail": None, "sim_mean": None, "sim_max": None}
    sims = [r["t_sim"] for r in rows if isinstance(r.get("t_sim"), (int, float))]
    if sims:
        out["sim_mean"], out["sim_max"] = round(sum(sims) / len(sims), 2), round(max(sims), 2)
        out["t_sim"] = sims[-1]
    for r in reversed(rows):
        if r.get("gap_names"):
            out["gap_names"] = r["gap_names"]
            break
    for r in reversed(rows):
        if r.get("ui_tail"):
            out["ui_tail"] = r["ui_tail"]
            break
    return out


def load_rows(path: str, max_rows: int = 5000) -> list:
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue            # 崩溃时最后一行可能是半截
                rows.append(row_of(d, i))
                if len(rows) >= max_rows:
                    break
    except OSError:
        pass
    return rows


def summarize(rows: list) -> dict:
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    turns = [r["turn"] for r in rows if isinstance(r.get("turn"), int)]
    return {"steps": len(rows), "kinds": kinds,
            "turns": (min(turns), max(turns)) if turns else None,
            "failed": sum(1 for r in rows if r["executed"] and r["ok"] is False),
            "with_gaps": sum(1 for r in rows if r["gaps"])}
