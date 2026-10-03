# -*- coding: utf-8 -*-
"""面板 ↔ 监听器 的文件通道（原子写，坏文件按默认值处理）。

控制文件 `control.json`（面板写）与状态文件 `status.json`（监听器里的 autoplay 写）分开，
各只有一个写者，避免互相覆盖。
"""
from __future__ import annotations

import json
import os
import time

from base import paths as _paths_c
STATE_DIR = _paths_c.GUI_DIR
CONTROL = os.path.join(STATE_DIR, "control.json")
STATUS = os.path.join(STATE_DIR, "status.json")
EVENTS = os.path.join(STATE_DIR, "events.log")

DEFAULT_CONTROL = {
    "run": False,                 # 总开关：False ⇒ 不再开新局（当前这局是否继续看 abort_now）
    "auto_next": True,            # 一局结束后自动回牌组页 → 点开始 → 接手下一局
    "unlimited": False,           # True ⇒ 不限局数（games_total 忽略）
    "games_total": 1,             # 本次计划打几局
    "turns": 40,                  # 每局最多打几个我方回合
    "max_actions": 900,           # 每局最多执行几步
    "avoid": [],                  # 按牌名回避（player.play.play 的 avoid）
    "forbid": [],                 # 整类动作禁用（player.play.play 的 forbid；面板“功能选择”里没勾的开关）
    "dry_run": False,             # 只看不动：接手进行中的对局、只记决策不执行（不开新局）
    "abort_now": False,           # 立即停：当前局下一步开始前收手（不再动游戏）
    "games_done": 0,              # autoplay 写回（面板只读）
}
DEFAULT_STATUS = {"state": "idle", "busy": False, "note": "", "games_done": 0, "current_log": None,
                  "last_result": None, "updated": 0.0}


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            out = dict(default)
            out.update(d)
            return out
    except (OSError, ValueError):
        pass
    return dict(default)


def _write(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def load_control() -> dict:
    return _read(CONTROL, DEFAULT_CONTROL)


def update_control(**kw) -> dict:
    d = load_control()
    for k, v in kw.items():
        if k not in DEFAULT_CONTROL:
            raise KeyError("未知控制项 %r" % k)
        d[k] = v
    _write(CONTROL, d)
    return d


def load_status() -> dict:
    return _read(STATUS, DEFAULT_STATUS)


def update_status(**kw) -> dict:
    d = load_status()
    d.update(kw)
    d["updated"] = time.time()
    _write(STATUS, d)
    return d


def log_event(msg: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def tail_events(n: int = 200) -> list:
    try:
        with open(EVENTS, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []
