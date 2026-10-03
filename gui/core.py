# -*- coding: utf-8 -*-
"""控制面板的纯函数层（可离线测，不碰 tkinter / 游戏）。

参照 OCR-Kards-Auto 面板（pywebview）的功能清单（致谢，见 NOTICE）——用户 2026-10-03：“控制面板至少要包含上游 OCR Kards Auto 的
Gui 的全部功能。”上游功能 → 这里的对应物：

| 上游                                   | 这里                                                                 |
|----------------------------------------|----------------------------------------------------------------------|
| 功能选择（`SWITCHES` → 命令行开关）    | `SWITCHES` → `control.json` 的 `forbid` / `dry_run`（`build_opts`）  |
| 开始 / 停止 + 单实例 + 启动后活性检查   | 面板的开始/停止；监听器启动后的活性检查 + `explain_exit_code`        |
| `tail_lines` / `log_rev` / `mark_line`  | 同名函数（日志变没变看文件指纹，不看行数；行着色；“只看关键行”）     |
| `parse_status`（局数/状态/费用/盘面/最近动作/问题数/在等窗口） | `parse_status`（事件日志 + 决策日志 + status.json）   |
| `read_version` / 检测更新 / 自动更新    | `read_version` / `gui.update_check`                                  |
| 窗口尺寸位置记忆 + `clamp_to_screen`    | `load_win_state` / `save_win_state` / `clamp_to_screen`              |
| `engine_python` + `python_probe`        | 同名（启动监听器前真跑探针，不看文件在不在）                         |
| 关面板 = 把引擎一起带走                 | 面板关闭时优雅退出**自己启动的**监听器（不强杀）                     |
| `--selftest`（不开窗口，写诊断文件）    | `python -m gui.app --selftest` → `selftest_info()`                   |
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

from base import paths as _paths

STATE_DIR = _paths.GUI_DIR
WIN_STATE = os.path.join(STATE_DIR, "window.json")
UPDATE_LOG = os.path.join(STATE_DIR, "update_check.log")
VERSION_FILE = os.path.join(_paths.AGENT_ROOT, "config", "app_version.json")
WIN_MIN = (980, 620)

# ---------------------------------------------------------------- 功能选择（对标上游 SWITCHES）
#: 每项：`key` 是面板勾选 id；`kinds` 是它管着的动作类（取消勾选 ⇒ 该类动作被 `forbid`）；`default` 默认是否开。
#  上游的 `--fast-scan`（惰性扫描）是 OCR 引擎的扫描策略，本引擎读内存、没有对应物，不做假开关。
SWITCHES = [
    {"key": "play", "label": "部署 / 出牌", "kinds": ("play_unit", "play_unit_target", "play_event",
                                                   "play_event_target", "play_order", "play_order_target"),
     "default": True, "hint": "不勾 = 本局不部署单位、不打指令"},
    {"key": "move", "label": "上线（移动到前线）", "kinds": ("move_up",),
     "default": True, "hint": "不勾 = 不把后排单位推上前线"},
    {"key": "attack", "label": "攻击", "kinds": ("attack",),
     "default": True, "hint": "不勾 = 不发起任何攻击"},
    {"key": "end_turn", "label": "结束回合", "kinds": ("end",),
     "default": True, "hint": "不勾 = 动作做完后不点结束回合（等人接手）"},
    {"key": "dry_run", "label": "只看不动（调试用）", "kinds": (), "default": False,
     "hint": "调试用：接手一局正在进行的对局，只记决策、不执行（不会开新局）。平时不需要开"},
]
DEFAULT_ON = sorted(s["key"] for s in SWITCHES if s["default"])


def build_opts(checked: dict) -> dict:
    """面板勾选 → `control.json` 里的 `forbid`（未勾选的开关管着的动作类）与 `dry_run`。"""
    forbid = []
    for sw in SWITCHES:
        if sw["key"] == "dry_run":
            continue
        if not checked.get(sw["key"], sw["default"]):
            forbid += list(sw["kinds"])
    return {"forbid": forbid, "dry_run": bool(checked.get("dry_run", False))}


def checked_from_control(ctrl: dict) -> dict:
    """反向：`control.json` → 面板勾选状态。"""
    forbid = set(ctrl.get("forbid") or ())
    out = {}
    for sw in SWITCHES:
        if sw["key"] == "dry_run":
            out["dry_run"] = bool(ctrl.get("dry_run"))
        else:
            out[sw["key"]] = not (sw["kinds"] and set(sw["kinds"]) <= forbid)
    return out


# ---------------------------------------------------------------- 日志
#: “值得看一眼”：判据没下成结论 / 出了异常 / 面板停手。只收异常，不收正常结局。
ISSUE_WORDS = ("Traceback", "exception", "失败", "停手", "没清掉", "判不出", "残留", "💥", "!!", "超时", "异常")
#: “只看关键行”的词（事件日志 + 监听器输出）
KEY_WORDS = ("开局", "接手", "本局结束", "已打", "面板：", "监听器", "停", "残留", "失败", "exception", "Traceback", "!!",
             "start", "quit")


def tail_lines(path: str, n: int = 400) -> list:
    """读文件**末尾 n 行**（二进制读再自己解码：文本模式从中间 seek 会切断多字节汉字）。"""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        back = min(size, max(4096, n * 240))
        f.seek(size - back)
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    if back < size and lines:
        lines = lines[1:]                       # 第一行多半是半截的
    return lines[-n:]


def log_rev(path: str) -> str:
    """日志“变没变”的指纹（大小 + 纳秒 mtime）。★ 不能用行数：尾部只取 n 行，行数会饱和，之后永远判“没变”。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return "%d:%d" % (st.st_size, st.st_mtime_ns)


def has_issue(line: str) -> bool:
    """这行是不是“值得看一眼”。★ `'exception': None` 是正常局结束行里的字段，要先抠掉，否则每局都算一个问题。"""
    line = line.replace("'exception': None", "")
    return any(w in line for w in ISSUE_WORDS)


def mark_line(line: str) -> str:
    """给日志行挑一个样式类：bad 红 / warn 橙 / ok 绿 / turn / state / act / dim。"""
    probe = line.replace("'exception': None", "")
    if "Traceback" in probe or "💥" in probe or "exception" in probe:
        return "bad"
    if has_issue(line):
        return "warn"
    if "开局：ok=True" in line or "本局结束" in line and "'played': True" in line or "已就绪" in line:
        return "ok"
    if "接手" in line or "开始" in line:
        return "act"
    if "已打" in line or "state" in line.lower():
        return "state"
    if line.startswith("     ") or line.startswith("    "):
        return "dim"
    return ""


def only_key(lines: list) -> list:
    return [ln for ln in lines if any(w in ln for w in KEY_WORDS)]


# ---------------------------------------------------------------- 状态解析（事件日志 + 决策日志 + status.json）
RE_ROUND = re.compile(r"本局结束：\{'played': (True|False)(?:, 'seconds': ([\d.]+))?")
RE_START = re.compile(r"面板：开始（(\{.*\})）")
RE_RESIDUAL = re.compile(r"残留")


def parse_status(event_lines: list, rows: list = None, status: dict = None) -> dict:
    """面板“实时状态”要显示的东西。

    * `event_lines`：`events.log` 末尾行（局数 / 开始参数 / 是否在等残留）；
    * `rows`：决策日志（`history.load_rows` 的行；取最后一条的回合/费用/最近动作/问题数）；
    * `status`：`status.json`（state/note/busy）。
    读不出的字段留空，不编。
    """
    out = {"rounds": [], "state": None, "note": None, "kredits": None, "turn": None, "last_action": None,
           "board": None, "started": None, "issues": 0, "waiting": False, "busy": None}
    for ln in event_lines or ():
        body = ln.split("] ", 1)[1] if ln.startswith("[") and "] " in ln else ln
        m = RE_START.search(body)
        if m:
            try:
                out["started"] = json.loads(m.group(1))
            except ValueError:
                out["started"] = {"raw": m.group(1)}
            out["rounds"] = []                  # 新一段：局数清零
        m = RE_ROUND.search(body)
        if m:
            out["rounds"].append({"n": len(out["rounds"]) + 1, "played": m.group(1) == "True",
                                  "min": round(float(m.group(2)) / 60.0, 1) if m.group(2) else None})
        if has_issue(body):
            out["issues"] += 1
        out["waiting"] = bool(RE_RESIDUAL.search(body) and "等待" in body) or (out["waiting"] and "本局结束" not in body
                                                                              and "开局" not in body)
    if status:
        out["state"], out["note"], out["busy"] = status.get("state"), status.get("note"), status.get("busy")
        if "残留" in (status.get("note") or "") or "等窗口" in (status.get("note") or ""):
            out["waiting"] = True
    if rows:
        last = rows[-1]
        raw = last.get("raw") or {}
        out["turn"] = last.get("turn")
        out["kredits"] = raw.get("kredits")
        out["last_action"] = {"kind": last.get("kind"), "note": (last.get("note") or "")[:160]}
        sim_units = (((raw.get("probe") or {}).get("timing")) or {}).get("sim_units")
        if sim_units is not None:
            out["board"] = {"sim_units": sim_units,
                            "sim_hand": (((raw.get("probe") or {}).get("timing")) or {}).get("sim_hand")}
        out["issues"] += sum(1 for r in rows if r.get("ok") is False)
    return out


# ---------------------------------------------------------------- 退出码 / 解释器
def explain_exit_code(code: Optional[int], output: str = "") -> str:
    """把监听器的裸退出码翻译成使用者看得懂、知道下一步做什么的一句话（对标上游 `explain_exit_code`）。"""
    blob = output or ""
    if "ModuleNotFoundError" in blob or "ImportError" in blob:
        m = re.search(r"No module named '([^']+)'", blob)
        return "这个 Python 里缺依赖（%s）。换用装好依赖的解释器，或先 pip install。" % (m.group(1) if m else "某个模块")
    if "precheck.available() = False" in blob or "Unable to find process" in blob or "找不到游戏进程" in blob:
        return "监听器没能 attach 到游戏：游戏没开、或构建/偏移表不对。先开游戏再启动。"
    if "did not find executable" in blob or code == 103:
        return "Python 环境坏了（解释器路径失效，退出码 103）。重建虚拟环境。"
    if code in (None, 0):
        return "监听器正常退出。"
    return "监听器以退出码 %s 结束，具体原因看“监听器输出”页签。" % code


def python_probe(py: str, timeout: float = 30.0) -> tuple:
    """真跑一次探针（不是看文件在不在）。→ (ok, 原因)。"""
    try:
        r = subprocess.run([py, "-c", "import sys; print(sys.version_info[0])"], capture_output=True, text=True,
                           timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    if r.returncode != 0:
        return False, "退出码 %s：%s" % (r.returncode, (r.stderr or r.stdout).strip()[:200])
    return True, ""


_PYTHON_DIAG: list = []


def engine_python(cands=None, probe=python_probe) -> Optional[str]:
    """起监听器该用哪个解释器：当前解释器优先，其次 PATH 里的；每一档都真跑探针，坏的跳过。
    一个能用的都没有 ⇒ None，原因记在 `_PYTHON_DIAG`（面板把整张表念给使用者听）。"""
    from shutil import which
    cands = list(cands) if cands is not None else [sys.executable] + [w for w in (which("python"),) if w]
    _PYTHON_DIAG.clear()
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if not os.path.exists(c):
            _PYTHON_DIAG.append((c, "文件不存在"))
            continue
        ok, why = probe(c)
        if ok:
            return c
        _PYTHON_DIAG.append((c, why))
    return None


# ---------------------------------------------------------------- 版本
def read_version() -> dict:
    """`config/app_version.json`（给人改的，所以用 utf-8-sig 读：记事本会写 BOM，不吃掉版本号会静默变 0.0.0）；
    没有文件就用 git 提交短哈希。"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        d.setdefault("version", "0.0.0")
        return d
    except Exception:                                         # noqa: BLE001
        pass
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_paths.AGENT_ROOT, capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return {"version": "git-" + r.stdout.strip(), "update_url": ""}
    except Exception:                                         # noqa: BLE001
        pass
    return {"version": "0.0.0", "update_url": ""}


def log_update(line: str, path: str = None) -> None:
    """检测更新的结果追加进 `update_check.log`（含开机那次静默的——不留痕就分不清“没新版本”和“线程没跑”）。"""
    try:
        path = path or UPDATE_LOG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except Exception:                                         # noqa: BLE001
        pass                                                  # 记不下来也绝不许影响面板


# ---------------------------------------------------------------- 窗口几何
def load_win_state(path: str = None) -> dict:
    try:
        with open(path or WIN_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {k: int(d[k]) for k in ("w", "h", "x", "y") if k in d and d[k] is not None}
    except Exception:                                         # noqa: BLE001
        return {}


def save_win_state(w, h, x=None, y=None, path: str = None) -> None:
    try:
        d = {"w": int(w), "h": int(h)}
        if x is not None and y is not None:
            d["x"], d["y"] = int(x), int(y)
        path = path or WIN_STATE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:                                         # noqa: BLE001
        pass


def clamp_to_screen(x: int, y: int, w: int, h: int, screen: tuple = None) -> tuple:
    """把记下来的位置夹回屏幕内（换了显示器 / 拔了副屏后窗口不至于开在看不见的地方）。`screen=(宽,高)` 便于离线测。"""
    try:
        if screen is None:
            import ctypes
            screen = (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1))
        sw, sh = screen
        if sw <= 0 or sh <= 0:
            return int(x), int(y)
        x = max(-w + 200, min(int(x), sw - 200))
        y = max(0, min(int(y), sh - 120))
    except Exception:                                         # noqa: BLE001
        pass
    return int(x), int(y)


def geometry_string(st: dict, default=(1180, 760), screen: tuple = None) -> str:
    """tkinter 的 `geometry()` 串：记住的尺寸（不小于最小尺寸）+ 夹回屏幕内的位置。"""
    w = max(WIN_MIN[0], int(st.get("w", default[0])))
    h = max(WIN_MIN[1], int(st.get("h", default[1])))
    if "x" in st and "y" in st:
        x, y = clamp_to_screen(st["x"], st["y"], w, h, screen)
        return "%dx%d%+d%+d" % (w, h, x, y)
    return "%dx%d" % (w, h)


# ---------------------------------------------------------------- 自检（不开窗口）
def selftest_info() -> dict:
    eng = engine_python()
    return {"agent_root": _paths.AGENT_ROOT, "state_dir": STATE_DIR, "live_log": _paths.LIVE_LOG,
            "live_cmd": _paths.LIVE_CMD, "live_log_exists": os.path.exists(_paths.LIVE_LOG),
            "engine_python": eng, "engine_python_diag": [list(d) for d in _PYTHON_DIAG],
            "log_rev": log_rev(_paths.LIVE_LOG), "version": read_version(),
            "switches": [s["key"] for s in SWITCHES], "default_on": DEFAULT_ON,
            "argv_example": build_opts({"play": True, "attack": False})}
