# -*- coding: utf-8 -*-
"""检测更新（对标上游 `update_check`）。三条纪律照抄上游：

1. **查不到就说查不到**，不许说成“已是最新”；
2. **不自动装**（面板不去替换自己 / 不 `git pull`）——只告诉使用者有新版本、给出地址 / 命令；
3. 纯逻辑，网络与 git 都可注入，离线可测。

更新源（按顺序）：
* `config/app_version.json` 里的 `update_url`：一个返回 `{"tag_name": "v1.2.3", "html_url": "..."}` 的 JSON 地址
  （GitHub `releases/latest` 的格式）；
* 否则看这个仓库有没有 git 远端：`git ls-remote <remote> HEAD` 与本地 `HEAD` 比。
两个都没有 ⇒ 如实报“没有配置更新源”（仓库现在没有远端——这是事实，不是“已是最新”）。
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from typing import Callable, Optional

from base import paths as _paths

TIMEOUT = 8.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # 打包版（窗口子系统）起 git 不闪黑窗


def parse_ver(v: str) -> tuple:
    """`v1.2.3` / `1.2.3-rc1` → (1, 2, 3)；认不出的段按 0。"""
    nums = re.findall(r"\d+", str(v or ""))[:4]
    return tuple(int(x) for x in nums) or (0,)


def newer(remote: str, local: str) -> bool:
    return parse_ver(remote) > parse_ver(local)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kards-agent-panel"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:                       # noqa: S310
        return json.loads(r.read().decode("utf-8", "replace"))


def _git(args: list, cwd: str) -> str:
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT + 4,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:160] or "git 退出码 %s" % r.returncode)
    return r.stdout


def check(local_version: str, update_url: Optional[str] = None, http: Callable = _http_json,
          git: Callable = _git, cwd: str = None) -> dict:
    """→ `{"ok", "has_update", "remote", "url", "via", "msg"}`。`ok=False` 表示**没查成**（不是“没有更新”）。"""
    cwd = cwd or _paths.AGENT_ROOT
    if update_url:
        try:
            d = http(update_url)
            tag = str(d.get("tag_name") or d.get("version") or "")
            if not tag:
                return {"ok": False, "has_update": False, "remote": None, "url": update_url, "via": "url",
                        "msg": "更新源返回里没有版本号（tag_name / version）：没查成，不能说已是最新"}
            hu = newer(tag, local_version)
            return {"ok": True, "has_update": hu, "remote": tag, "url": d.get("html_url") or update_url, "via": "url",
                    "msg": ("有新版本 %s（本地 %s）" % (tag, local_version)) if hu else
                           ("已是最新（远端 %s，本地 %s）" % (tag, local_version))}
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "has_update": False, "remote": None, "url": update_url, "via": "url",
                    "msg": "查更新源失败（%s: %s）：没查成，不能说已是最新" % (type(e).__name__, e)}
    try:
        remotes = [x for x in git(["remote"], cwd).split() if x]
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "has_update": False, "remote": None, "url": "", "via": "git",
                "msg": "读不到 git 信息（%s）：没查成" % e}
    if not remotes:
        return {"ok": False, "has_update": False, "remote": None, "url": "", "via": "none",
                "msg": "没有配置更新源：仓库没有 git 远端，config/app_version.json 也没有 update_url。"
                       "没查成（不是“已是最新”）。"}
    try:
        head = git(["rev-parse", "HEAD"], cwd).strip()
        line = git(["ls-remote", remotes[0], "HEAD"], cwd).split()
        rhead = line[0] if line else ""
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "has_update": False, "remote": None, "url": "", "via": "git",
                "msg": "查远端失败（%s）：没查成，不能说已是最新" % e}
    if not rhead:
        return {"ok": False, "has_update": False, "remote": None, "url": "", "via": "git", "msg": "远端没有返回 HEAD"}
    if rhead == head:
        return {"ok": True, "has_update": False, "remote": rhead[:8], "url": "", "via": "git",
                "msg": "已是最新（与 %s 的 HEAD 相同 %s）" % (remotes[0], head[:8])}
    return {"ok": True, "has_update": True, "remote": rhead[:8], "url": "", "via": "git",
            "msg": "远端 %s 的 HEAD 是 %s，本地是 %s——有不同的提交（面板不自动拉取，请自行 git pull）"
                   % (remotes[0], rhead[:8], head[:8])}
