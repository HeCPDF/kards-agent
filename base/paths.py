# -*- coding: utf-8 -*-
"""base.paths —— 运行期文件的**唯一**位置表（监听器通道、日志、API 缓存、热重载开关）。

以前这些路径散在 `D:\\Kards\\_nn_scratch\\…` 的硬编码里（gui / hotreload / rule / choosespawn / crashdump …），
生产部件放在草稿目录里。现在统一从这里取；换位置只改这一个文件（或设环境变量）。

* 代码在 `kards-agent/`；运行期数据在 `kards-data/`（`.gitignore` 里，不入库）；
* `_nn_scratch/` 只放一次性探针脚本，**生产代码不得依赖它**（`test_no_scratch_dep.py` 把关，这里也不给它留常量）。
"""
from __future__ import annotations

import os
import sys

#: PyInstaller 冻结（`kards-agent.exe`）？冻结时 `__file__` 落在只读的 `_internal/`（`sys._MEIPASS`），
#: 不能当数据目录用；**可写数据一律落在 exe 所在目录**（`<exe 目录>/data`），随包资源（build_tables.json、
#: agent.js.tpl）由各模块自己的 `Path(__file__)` 经 `_MEIPASS` 取到（打包脚本 `tools/build_exe.py` 负责放对位置）。
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    AGENT_ROOT = os.path.dirname(os.path.abspath(sys.executable))                # exe 所在目录（用户解压的那个文件夹）
    UMBRELLA = AGENT_ROOT                                                         # 冻结包没有伞仓库；别往 exe 目录的上一层写东西
    RESOURCE_ROOT = getattr(sys, "_MEIPASS", AGENT_ROOT)                          # 随包只读资源根
else:
    AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # …/kards-agent
    UMBRELLA = os.path.dirname(AGENT_ROOT)                                        # …/Kards（伞仓库根）
    RESOURCE_ROOT = AGENT_ROOT


def _default_data() -> str:
    """运行期数据目录：环境变量 `KARDS_DATA_DIR` > 开发布局（伞仓库旁的 `kards-data/`）> 发布布局（仓库内 `data/`）。
    发布出去的仓库只有 kards-agent 这一棵树，所以落在 `<仓库>/data/`（已在 .gitignore）。
    冻结的 exe 包：`<exe 目录>/data`（不看伞仓库标志物，也绝不落在临时的 `_MEIPASS`）。"""
    if FROZEN:
        return os.path.join(AGENT_ROOT, "data")
    dev = os.path.join(UMBRELLA, "kards-data")
    if os.path.isdir(os.path.join(UMBRELLA, "reverse-data")) or os.path.isdir(dev):
        return dev
    return os.path.join(AGENT_ROOT, "data")


DATA = os.environ.get("KARDS_DATA_DIR") or _default_data()

# 常驻监听器（tools/live_session.py）的文件通道：往 CMD 追加一行 = 一条命令，结果按行追加进 LOG
LIVE_DIR = os.environ.get("KARDS_LIVE_DIR") or os.path.join(DATA, "live")
LIVE_CMD = os.path.join(LIVE_DIR, "live_cmd.txt")
LIVE_LOG = os.path.join(LIVE_DIR, "live_log.txt")
LIVE_STDOUT = os.path.join(LIVE_DIR, "live_stdout.txt")
HOT_RELOAD_FLAG = os.path.join(LIVE_DIR, "hot_reload.txt")
HOT_RELOAD_LOG = os.path.join(LIVE_DIR, "hot_reload.log")

# 官方 API 卡表缓存（"没进预备"的判据；由 tools/kards_api_reserved.py 拉取）
API_DIR = os.path.join(DATA, "api")
API_CACHE = os.path.join(API_DIR, "kards_api_cards.json")


# 决策日志（每步一行 JSONL）/ 录制 / 面板状态 / 版本缓存
NN_LOG_DIR = os.path.join(DATA, "nn", "logs")
RECORD_DIR = os.path.join(DATA, "recordings")
GUI_DIR = os.environ.get("KARDS_GUI_DIR") or os.path.join(DATA, "gui")
VERSION_CACHE = os.path.join(GUI_DIR, "version_cache.json")


def ensure_dirs() -> None:
    for d in (LIVE_DIR, API_DIR, NN_LOG_DIR, RECORD_DIR, GUI_DIR):
        os.makedirs(d, exist_ok=True)
