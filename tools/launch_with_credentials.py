#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用指定的设备凭据启动指定的 exe。

    python tools/launch_with_credentials.py --exe "<…>\\kards-Win64-Shipping.exe" --cred-file <凭据.json>
    set KARDS_DEVICE_ID=… & set KARDS_DEVICE_PASSWORD=… & python tools/launch_with_credentials.py --exe <exe>

做的事只有两步：把 `device_id` / `device_password` 写进 `HKCU\\Software\\Kards`（REG_SZ，游戏启动时就读这两个值，
读不到才自己随机生成），然后直接运行该 exe。依据：`reverse-data/reports/report/LOGIN-AND-SERVER-URL-1.60.md` §2。

约定（用户 2026-10-09 定）：
* **不还原**原值；**竞态由调用方负责**——不要在已有游戏进程时启动，也不要并发启动（注册表是共享的）。
  覆盖前把旧值备份到 `<数据目录>/credentials-backup/`（不入库，只防丢号，不自动还原）。
* 凭据从 `--cred-file`（JSON：`{"device_id": …, "device_password": …}`）或环境变量 `KARDS_DEVICE_ID` /
  `KARDS_DEVICE_PASSWORD` 读；**不接受命令行参数**，输出里不出现凭据值。
* exe 是调用方指定的任意一份 `kards*-Win64-Shipping*.exe`（原版、补丁版都行），**不是** `kards.exe` 启动器。
* 只用用户本人的账号。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 仓库根

from base import paths                                                              # noqa: E402

REG_SUBKEY = r"Software\Kards"
REG_VALUES = ("device_id", "device_password")
ENV_NAMES = {"device_id": "KARDS_DEVICE_ID", "device_password": "KARDS_DEVICE_PASSWORD"}
BACKUP_DIR = os.path.join(paths.DATA, "credentials-backup")


class LaunchError(Exception):
    """参数/环境不对；消息里不含凭据。"""


def _valid_value(v) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= 400 and all(32 <= ord(c) < 127 for c in v)


def load_credentials(cred_file=None, environ=None) -> dict:
    """读凭据：`--cred-file` 优先，其次环境变量。缺哪个都报错（不猜、不生成）。"""
    environ = os.environ if environ is None else environ
    if cred_file:
        try:
            with open(cred_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise LaunchError("读不了凭据文件 %s：%s" % (cred_file, type(e).__name__))
        if not isinstance(data, dict):
            raise LaunchError("凭据文件应是 JSON 对象")
    else:
        data = {k: environ.get(ENV_NAMES[k]) for k in REG_VALUES}
    creds = {}
    for k in REG_VALUES:
        v = data.get(k)
        if not _valid_value(v):
            raise LaunchError("缺少或不合法的 %s（%s）" % (k, "文件里的键" if cred_file else "环境变量 " + ENV_NAMES[k]))
        creds[k] = v
    return creds


def check_exe(exe: str) -> str:
    exe = os.path.abspath(exe)
    name = os.path.basename(exe).lower()
    if not os.path.isfile(exe):
        raise LaunchError("exe 不存在：%s" % exe)
    if name == "kards.exe":
        raise LaunchError("这是 kards.exe 启动器；请指定 kards-Win64-Shipping*.exe（启动器会起它自己的那一份）")
    if not (name.endswith(".exe") and "win64-shipping" in name):
        raise LaunchError("不像游戏本体 exe（期望 kards*-Win64-Shipping*.exe）：%s" % name)
    return exe


def read_current(winreg_mod) -> dict:
    """当前注册表里的两个值（缺的为 None）。"""
    out = {k: None for k in REG_VALUES}
    try:
        key = winreg_mod.OpenKey(winreg_mod.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg_mod.KEY_READ)
    except OSError:
        return out
    try:
        for k in REG_VALUES:
            try:
                v, t = winreg_mod.QueryValueEx(key, k)
                out[k] = v if t == winreg_mod.REG_SZ else None
            except OSError:
                pass
    finally:
        winreg_mod.CloseKey(key)
    return out


def backup_current(current: dict, backup_dir: str = BACKUP_DIR) -> str | None:
    """旧值备份（只在有旧值时）。文件只含这两个值，不入库；返回备份路径。"""
    if not any(current.values()):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, time.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f)
    return path


def write_credentials(winreg_mod, creds: dict) -> None:
    key = winreg_mod.CreateKeyEx(winreg_mod.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg_mod.KEY_SET_VALUE)
    try:
        for k in REG_VALUES:
            winreg_mod.SetValueEx(key, k, 0, winreg_mod.REG_SZ, creds[k])
    finally:
        winreg_mod.CloseKey(key)


def spawn(exe: str):
    """直接运行 exe；脱离本进程（本进程退出不影响游戏），不接管标准流。"""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([exe], cwd=os.path.dirname(exe), creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(exe: str, creds: dict, winreg_mod=None, spawn_fn=spawn, backup_dir: str = BACKUP_DIR) -> dict:
    """写凭据并启动。返回的字典里**不含**凭据值。"""
    if winreg_mod is None:
        import winreg as winreg_mod                                                  # noqa: PLC0415（仅 Windows）
    exe = check_exe(exe)
    current = read_current(winreg_mod)
    backup = backup_current(current, backup_dir)
    unchanged = current == {k: creds[k] for k in REG_VALUES}
    write_credentials(winreg_mod, creds)
    proc = spawn_fn(exe)
    return {"exe": exe, "pid": getattr(proc, "pid", None), "registry_changed": not unchanged,
            "backup": backup}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用指定设备凭据启动指定的 kards-Win64-Shipping*.exe（不还原注册表；竞态由调用方负责）")
    ap.add_argument("--exe", required=True, help="游戏本体 exe 的路径（不是 kards.exe 启动器）")
    ap.add_argument("--cred-file", help='JSON：{"device_id": …, "device_password": …}；不给则读环境变量 KARDS_DEVICE_ID / KARDS_DEVICE_PASSWORD')
    ap.add_argument("--dry-run", action="store_true", help="只检查参数与凭据格式，不写注册表、不启动")
    a = ap.parse_args(argv)
    try:
        creds = load_credentials(a.cred_file)
        exe = check_exe(a.exe)
        if a.dry_run:
            print(json.dumps({"exe": exe, "credentials": "ok（未显示）", "dry_run": True}, ensure_ascii=False))
            return 0
        info = launch(exe, creds)
    except LaunchError as e:
        print("失败：%s" % e)
        return 2
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
