#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包版（PyInstaller 冻结）分支的离线回归 —— 全部用假 `sys.frozen`/假 `sys._MEIPASS`，不真打包、不碰游戏、不开窗口。

钉住的约定（`tools/gui_main.py`、`tools/build_exe.py`）：
  * 冻结：AGENT_ROOT / 数据目录 = exe 所在目录（`<exe 目录>/data`），绝不是临时的 `_MEIPASS`；开发布局的值一字不变；
  * 面板起监听器的命令行 = `[exe, --listener]`；开发布局仍是 `[python, <监听器脚本>]`；
  * 进程发现正则（`gui.watcher.PS_QUERY`）两种命令行都认、不误认面板自己、不匹配自己的 PowerShell；
  * 冻结时解释器探针 = `exe --probe`、`engine_python` 只认 exe 自己；`read_version` 不依赖 git；热重载跳过。
"""
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
# 闸门（tests/test_no_gui_guard.py）：本文件不建窗口；显式声明环境为“无窗口”，下面出现面板/监听器字样才合规。
os.environ["KARDS_TEST_GUI_WINDOW"] = "0"
_TMP = tempfile.mkdtemp(prefix="kards_frozen_test_")
os.environ["KARDS_DATA_DIR"] = os.path.join(_TMP, "devdata")
os.environ.pop("KARDS_LIVE_DIR", None)
os.environ.pop("KARDS_GUI_DIR", None)

from base import paths as P  # noqa: E402
from gui import core as K, watcher as W  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


# ---------------------------------------------------------------- 开发布局：冻结开关关着时不变
chk("开发布局 FROZEN=False，AGENT_ROOT 是源码树", (not P.FROZEN) and os.path.isdir(os.path.join(P.AGENT_ROOT, "gui")))
chk("开发布局 RESOURCE_ROOT == AGENT_ROOT", P.RESOURCE_ROOT == P.AGENT_ROOT)
dev_cmd = K.listener_cmd("PY")
chk("开发布局监听器命令 = [python, tools/live_session.py]",
    dev_cmd == ["PY", os.path.join(P.AGENT_ROOT, "tools", "live_session.py")], str(dev_cmd))
chk("listener_cmd 允许显式传脚本路径（面板的 LIVE_SESSION）", K.listener_cmd("PY", "X.py") == ["PY", "X.py"])


# ---------------------------------------------------------------- 冻结：paths.py（reload，用完恢复）
class FakeFrozen:
    """临时把进程伪装成冻结态；退出时恢复并 reload `base.paths` 回开发值。"""

    def __init__(self, exe, meipass):
        self.exe, self.meipass = exe, meipass

    def __enter__(self):
        self.old = (sys.executable, getattr(sys, "frozen", None), getattr(sys, "_MEIPASS", None))
        sys.executable, sys.frozen, sys._MEIPASS = self.exe, True, self.meipass
        return importlib.reload(P)

    def __exit__(self, *a):
        sys.executable = self.old[0]
        for k, v in (("frozen", self.old[1]), ("_MEIPASS", self.old[2])):
            if v is None:
                if hasattr(sys, k):
                    delattr(sys, k)
            else:
                setattr(sys, k, v)
        importlib.reload(P)


EXE_DIR = os.path.join(_TMP, "unzipped kards 包")
MEI = os.path.join(EXE_DIR, "_internal")
os.makedirs(MEI)
open(os.path.join(EXE_DIR, "kards-agent.exe"), "w").close()      # 假 exe：engine_python 要 os.path.exists
os.environ.pop("KARDS_DATA_DIR", None)
with FakeFrozen(os.path.join(EXE_DIR, "kards-agent.exe"), MEI) as FP:
    chk("冻结：FROZEN=True", FP.FROZEN is True)
    chk("冻结：AGENT_ROOT = exe 所在目录（不是 _MEIPASS）", FP.AGENT_ROOT == EXE_DIR, FP.AGENT_ROOT)
    chk("冻结：UMBRELLA 不再往上一层（不会写到解压目录之外）", FP.UMBRELLA == EXE_DIR)
    chk("冻结：RESOURCE_ROOT = _MEIPASS", FP.RESOURCE_ROOT == MEI)
    chk("冻结：数据目录 = <exe 目录>/data", FP.DATA == os.path.join(EXE_DIR, "data"), FP.DATA)
    chk("冻结：数据目录不在 _MEIPASS 里", not FP.DATA.startswith(MEI))
    chk("冻结：监听器通道在 data/live 下", FP.LIVE_CMD == os.path.join(EXE_DIR, "data", "live", "live_cmd.txt"))
    chk("冻结：listener_cmd = [exe, --listener]", K.listener_cmd(sys.executable) == [sys.executable, "--listener"])
    seen = []
    exe_path = os.path.join(_TMP, "fake.exe")
    open(exe_path, "w").close()
    py = K.engine_python(probe=lambda p: (seen.append(p) or (True, "")), cands=[exe_path])
    chk("engine_python(cands=…) 行为不变", py == exe_path and seen == [exe_path])
    # python_probe 的 argv：冻结时是 [exe, --probe]
    import subprocess as _sp
    calls = []
    real_run = _sp.run

    class R:
        returncode, stdout, stderr = 0, "", ""

    _sp.run = lambda argv, **kw: (calls.append((argv, kw)) or R())
    try:
        ok, why = K.python_probe("EXE")
    finally:
        _sp.run = real_run
    chk("冻结：解释器探针 = [exe, --probe]", ok and calls[0][0] == ["EXE", "--probe"], str(calls[0][0]))
    chk("冻结：探针不闪控制台（CREATE_NO_WINDOW）",
        calls[0][1].get("creationflags", 0) == getattr(_sp, "CREATE_NO_WINDOW", 0))
    # engine_python 缺省候选 = 只有 sys.executable
    cands_seen = []
    K.engine_python(probe=lambda p: (cands_seen.append(p) or (False, "x")))
    chk("冻结：缺省候选只有 sys.executable", cands_seen == [sys.executable], str(cands_seen))
    ver = K.read_version()
    from base.version import __version__ as V
    chk("冻结：read_version 用包版本号（无 git 也行）", ver.get("version") == V, str(ver))
    # 热重载：冻结时跳过
    from player import hotreload as HR
    msg = HR.reload_module("json")
    chk("冻结：热重载跳过且不碰模块", "冻结" in msg and "json" in sys.modules, msg)
    # tools/_bootstrap 冻结时不改 sys.path
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("_bootstrap_frozen_probe", os.path.join(ROOT, "tools", "_bootstrap.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    chk("冻结：_bootstrap 不往 sys.path 塞东西", sys.path == before)

chk("退出伪装后 FROZEN 复原", P.FROZEN is False and P.AGENT_ROOT == ROOT)
chk("退出伪装后开发布局命令复原", K.listener_cmd("PY")[1].endswith("live_session.py"))

# ---------------------------------------------------------------- 进程发现正则
pat = re.search(r"-match '([^']+)'", W.PS_QUERY).group(1)


def hit(s):
    return re.search(pat, s, re.I) is not None


chk("PS 正则认开发布局命令行", hit(r'"C:\Python\python.exe" D:\x\kards-agent\tools\live_session.py'))
chk("PS 正则认冻结监听器命令行", hit(r'"C:\我的 文件夹\kards-agent.exe" --listener'))
chk("PS 正则认冻结监听器（带多余参数）", hit(r"kards-agent.exe --listener --foo"))
chk("PS 正则不认面板自己（无 --listener）", not hit(r'"C:\x\kards-agent.exe"'))
chk("PS 正则不认面板的 --debug", not hit(r'"C:\x\kards-agent.exe" --debug'))
chk("PS 正则不会匹配到查询自己的 PowerShell 命令行", not hit(W.PS_QUERY) and not hit(
    'powershell -NoProfile -Command "%s"' % W.PS_QUERY))
chk("watcher 的 powershell/taskkill 带 CREATE_NO_WINDOW（窗口子系统下不闪黑窗）",
    W._NO_WINDOW == getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))

# ---------------------------------------------------------------- tools/gui_main.py（开发布局下可直接跑的部分）
os.environ["KARDS_DATA_DIR"] = os.path.join(_TMP, "devdata2")
importlib.reload(P)
import gui_main as GM  # noqa: E402

chk("gui_main --probe 秒退 0", GM.main(["--probe"]) == 0)
mods = GM.closure_modules()
chk("closure_modules() 含面板与监听器入口", {"gui.app", "tools.live_session", "ops.inject"} <= set(mods), str(len(mods)))
chk("EXTRA_TOPLEVEL 里的 canplay/_bootstrap 在 tools/ 下真实存在",
    all(os.path.exists(os.path.join(ROOT, "tools", m + ".py")) for m in GM.EXTRA_TOPLEVEL))
import io  # noqa: E402

buf = io.StringIO()
rc = GM.selfcheck(modules=["json", "base.paths", "ops.consts", "kardsmem.build"], out=buf)
text = buf.getvalue()
chk("selfcheck（小模块集）退出码 0 且有“自检通过”", rc == 0 and "自检通过" in text, text[-300:])
chk("selfcheck 写 <data>/selfcheck.txt", os.path.exists(os.path.join(P.DATA, "selfcheck.txt")))
buf = io.StringIO()
rc = GM.selfcheck(modules=["json", "no_such_module_xyz"], out=buf)
chk("selfcheck 缺模块 ⇒ 退出码 1 且点名失败项", rc == 1 and "FAIL import no_such_module_xyz" in buf.getvalue())
buf = io.StringIO()
rc = GM.selfcheck(modules=["player.model_not_there"], out=buf)
chk("selfcheck：非可选模块缺依赖不被当成 SKIP", rc == 1)
chk("selfcheck 设了 KARDS_BUILD 覆盖（不读游戏进程内存选表）", os.environ.get("KARDS_BUILD") == "current")
# 写日志用的 stdio 兜底：stdout 为 None 时接到文件
saved = sys.stdout, sys.stderr
sys.stdout = sys.stderr = None
try:
    lp = os.path.join(_TMP, "stdio.txt")
    GM._ensure_stdio(lp)
    print("hello-stdio")
    ok_stdio = sys.stdout is not None and sys.stderr is not None
    sys.stdout.flush()
finally:
    sys.stdout, sys.stderr = saved
chk("_ensure_stdio：窗口子系统下 stdout/stderr 为 None 时接到文件",
    ok_stdio and "hello-stdio" in open(lp, encoding="utf-8").read())

# ---------------------------------------------------------------- tools/build_exe.py（只测纯函数，不打包）
import build_exe as BX  # noqa: E402

from base.version import __version__ as VER  # noqa: E402

chk("build_exe.read_version == base.version", BX.read_version() == VER)
out_notice = os.path.join(_TMP, "notices.txt")
chk("emit_notices 写出文件", BX.emit_notices(out_notice) == 0 and os.path.getsize(out_notice) > 1000)
nt = open(out_notice, encoding="utf-8").read()
chk("THIRD-PARTY-NOTICES 含源码地址与 frida/numpy/CPython", BX.REPO_URL in nt and "frida" in nt and "numpy" in nt
    and "CPython" in nt)
chk("README 模板含快速开始与不要强杀的提醒", "不要在任务管理器里强杀" in BX.README and "{ver}" in BX.README)
chk("EXCLUDES 含 torch/matplotlib/pytest", {"torch", "matplotlib", "pytest"} <= set(BX.EXCLUDES))
chk("pinned_runtime 钉成当前解释器版本（frida==X numpy==Y）",
    all(re.match(r"^(frida|numpy)==\d", x) for x in BX.pinned_runtime(False)) and len(BX.pinned_runtime(False)) == 2)

print("\n失败 %d 项" % fails if fails else "\n全部通过")
sys.exit(1 if fails else 0)
