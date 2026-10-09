#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装树路径（`kardsmem.exes` / `kardsmem.buildsrc`）可由环境变量覆盖，不再写死开发机路径。离线、不碰进程。

`KARDS_GAME_INSTALLS`（收编树根）、`KARDS_STEAM_DIR`、`KARDS_LAUNCHER_DIR`：每个用例在子进程里设变量再导入，
因为这些常量在导入时求值。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def run(env_extra, code):
    env = dict(os.environ)
    for k in ("KARDS_GAME_INSTALLS", "KARDS_STEAM_DIR", "KARDS_LAUNCHER_DIR"):
        env.pop(k, None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-W", "error", "-c", "import sys; sys.path.insert(0, %r)\n%s" % (ROOT, code)],
                       capture_output=True, text=True, env=env, encoding="utf-8")
    return r.returncode, r.stdout.strip(), r.stderr.strip()[-300:]


rc, out, err = run({}, "from kardsmem import exes, buildsrc, build as B\n"
                       "print(exes.GAME_INSTALLS == B.WORKSPACE / 'game-installs')")
chk("默认：收编树根 = <工作区>/game-installs", rc == 0 and out == "True", err)

rc, out, err = run({"KARDS_GAME_INSTALLS": r"X:\inst", "KARDS_STEAM_DIR": r"X:\steam", "KARDS_LAUNCHER_DIR": r"X:\lau"},
                   "from kardsmem import exes, buildsrc\n"
                   "t = dict(exes.KNOWN_TREES)\n"
                   "print(t['steam'], t['1.60.27292.launcher'], t['launcher(default, 原地副本)'])\n"
                   "print(buildsrc.BUILD_SOURCES['current']['tree'], buildsrc.BUILD_SOURCES['launcher_157_orig']['tree'])")
lines = out.split("\n")
chk("环境变量覆盖 KNOWN_TREES", rc == 0 and lines[0] == r"X:\steam X:\inst\1.60.27292.launcher\kards\Binaries\Win64 X:\lau", out)
chk("buildsrc 与 exes 用同一份路径", rc == 0 and lines[1] == r"X:\steam X:\inst\1.57.26586.launcher\kards\Binaries\Win64", out)

print("失败 %d 项" % fails)
sys.exit(1 if fails else 0)
