#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_exe.py —— 把控制面板打成**免装 Python、免联网**的 Windows x64 PyInstaller 包（onedir）。

产物（`dist/` 已在 .gitignore）：
    dist/kards-agent-gui-win64-<版本>/            解压即用：双击 kards-agent.exe
        kards-agent.exe  _internal/               PyInstaller onedir（窗口子系统，无控制台）
        README-GUI.txt  LICENSE  NOTICE  THIRD-PARTY-NOTICES.txt
    dist/kards-agent-gui-win64-<版本>.zip         同上内容，顶层是同名文件夹
    dist/kards-agent-gui-win64-<版本>.zip.sha256

怎么做
------
1. 建一个**一次性 venv**（默认在系统临时目录；`--no-venv` 用当前解释器）：`pip install -r requirements.txt pyinstaller`
   —— 干净环境保证 torch / matplotlib / pytest 这类东西不可能被卷进包（再加一层 `--exclude-module` 兜底）。
2. 入口是 `tools/gui_main.py`（无参=面板，`--listener`=常驻监听器，`--selfcheck`=离线自检，`--probe`=探针）。
   `tools/gui_closure.py` 求出的 130 个模块全部以 `--hidden-import` 带上（里面有函数体内的延迟 import）；
   `canplay`、`_bootstrap` 是靠 `sys.path` 注入按名字 import 的顶层模块，也显式带上；
   `--collect-all frida` 带上 `_frida.pyd` 原生扩展；非 .py 资源：`kardsmem/build_tables.json`、`ops/agent.js.tpl`
   放到与源码相同的相对位置（模块里 `Path(__file__)` 经 `_MEIPASS` 照常找得到）。
3. 生成 THIRD-PARTY-NOTICES.txt（从 venv 里实际安装的 frida / numpy / PyInstaller / CPython / Tcl-Tk 的 dist-info 取真实许可证原文）。
4. **离线验收**（默认开，`--no-verify` 跳过）：把成品拷到带空格和中文的干净目录，用**洗过的环境**
   （无 PYTHONPATH、PATH 里没有 python）跑 `--probe`、`--selfcheck`、面板冒烟（`KARDS_GUI_SELFTEST_EXIT_MS`，窗口不映射到桌面）。
   **绝不**运行 `--listener`（它会 attach 游戏）。

用法：`python tools/build_exe.py [--no-venv] [--no-verify] [--keep-venv] [--venv-dir D] [--no-zip]`
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(HERE)
DIST = os.path.join(AGENT_ROOT, "dist")
EXE_NAME = "kards-agent"
REPO_URL = "https://github.com/HeCPDF/kards-agent"

#: 无论如何不许进包的东西（venv 干净时本来就没有；这里是第二道闸）。
EXCLUDES = ("torch", "torchvision", "torchaudio", "matplotlib", "pytest", "IPython", "jupyter", "notebook", "scipy",
            "pandas", "PIL", "cv2", "setuptools", "pkg_resources", "pip", "wheel", "sphinx", "capstone", "Crypto",
            "win32api", "win32con", "pywintypes", "tkinter.test", "test", "lib2to3")
#: 第三方运行时依赖里要收许可证原文的分发名。
LICENSE_DISTS = ("frida", "numpy")

README = """\
KARDS Agent 控制面板（Windows x64 免安装版） {ver}
KARDS Agent control panel (Windows x64, no Python needed) {ver}
=====================================================================

【快速开始 / Quick start】
1. 先启动 KARDS 游戏，并进入牌组页面。
   Start the KARDS game first.
2. 双击本文件夹里的 kards-agent.exe，控制面板会打开。
   Double-click kards-agent.exe; the control panel opens.
3. 点“启动监听器…”（会 attach 游戏进程；游戏必须已经在运行）。就绪后再点“开始”。
   Press the "start listener" button (it attaches to the running game), then "start".
4. 只打 训练 / AI 对局（对手 player_id 为负）；开局前请目视确认“训练模式”已高亮。不要用于真人对战。
   Training / AI matches ONLY. Visually confirm the "training mode" tab is highlighted before starting.
5. 想停：先点面板的“停止监听器”（会让当前这局收手并正常退出，最多等 90 秒）。
   To stop: use the panel's stop-listener button (it waits up to 90 s).
   **不要在任务管理器里强杀监听器进程**——强杀会卸载 frida agent，曾导致游戏崩溃。
   Do NOT force-kill the listener in Task Manager: it unloads the frida agent and has crashed the game before.

【文件 / Files】
- 运行数据（日志、窗口位置、API 缓存…）都写在 本文件夹\\data\\ 下；删除 data 文件夹即可恢复初始状态。
  All runtime data is written to <this folder>\\data\\ ; delete it to reset.
- RVA 缓存在 %LOCALAPPDATA%\\kards-agent\\rva-cache\\（游戏更新后首次 attach 会扫描一次）。
- 自检 / Self-check: 在命令行运行 kards-agent.exe --selfcheck ，结果同时写入 data\\selfcheck.txt（不碰游戏）。
- 杀毒软件误报：本包是 PyInstaller 打的 onedir 包，未签名；若被拦截请把本文件夹加入信任，或自行从源码运行。
  Unsigned PyInstaller bundle; antivirus tools may flag it. Whitelist the folder or run from source.

【源码与许可证 / Source & licences】
本程序是 GPL-3.0 软件（见 LICENSE、NOTICE）。源码：{repo}  ；本包由标签 v{ver} 构建。
GPL-3.0 software. Source code: {repo} ; this bundle is built from tag v{ver}.
第三方组件的许可证见 THIRD-PARTY-NOTICES.txt。
"""


# ---------------------------------------------------------------- 版本
def read_version() -> str:
    ns: dict = {}
    with open(os.path.join(AGENT_ROOT, "base", "version.py"), encoding="utf-8") as f:
        exec(compile(f.read(), "version.py", "exec"), ns)
    return ns["__version__"]


# ---------------------------------------------------------------- 第三方许可证汇总（在 venv 的解释器里跑）
def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def emit_notices(out_path: str) -> int:
    """把实际安装的组件的许可证原文汇总成一个文件。**在构建用的解释器里运行**（读它的 dist-info）。"""
    import glob
    import importlib.metadata as md
    parts = [
        "THIRD-PARTY NOTICES / 第三方组件许可证\n"
        "====================================\n\n"
        "kards-agent 本身是 GPL-3.0（见 LICENSE、NOTICE）。源码: %s\n"
        "The kards-agent source code is at %s ; this bundle is built from the matching release tag.\n\n"
        "本包 (PyInstaller onedir) 内含下列第三方组件，许可证原文如下 /\n"
        "This bundle contains the following third-party components; their licence texts follow.\n" % (REPO_URL, REPO_URL)
    ]

    def section(title: str, body: str):
        parts.append("\n\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78 + "\n\n" + body + "\n")

    for name in LICENSE_DISTS:
        try:
            d = md.distribution(name)
        except md.PackageNotFoundError:
            section(name, "（未安装，无法收集）")
            continue
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or "see files"
        texts = []
        for f in d.files or ():
            s = str(f).replace("\\", "/")
            base = s.rsplit("/", 1)[-1].upper()
            if ".dist-info/" in s and (("/licenses/" in s) or base.startswith(("LICENSE", "COPYING", "NOTICE"))):
                try:
                    texts.append("--- %s ---\n%s" % (s.split(".dist-info/", 1)[1], _read_text(str(d.locate_file(f)))))
                except OSError:
                    pass
        section("%s %s  (%s)" % (name, d.version, lic), "\n\n".join(texts) or "（该分发包没有附许可证文件，请见其项目主页）")

    try:
        d = md.distribution("pyinstaller")
        txt = [_read_text(str(d.locate_file(f))) for f in (d.files or ()) if str(f).replace("\\", "/").endswith("licenses/COPYING.txt")]
        section("PyInstaller bootloader & runtime hooks %s  (GPL-2.0-or-later with the bootloader exception)" % d.version,
                (txt[0] if txt else "see https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt") +
                "\n\n(The exception permits distributing the bundled bootloader with programs under any licence.)")
    except md.PackageNotFoundError:
        pass

    base = getattr(sys, "base_prefix", sys.prefix)
    py_lic = os.path.join(base, "LICENSE.txt")
    if os.path.exists(py_lic):
        section("CPython %s  (Python Software Foundation License)" % sys.version.split()[0], _read_text(py_lic))
    for cand in glob.glob(os.path.join(base, "tcl", "tk8*", "license.terms")) + \
            glob.glob(os.path.join(base, "tcl", "tcl8*", "license.terms")):
        section("Tcl/Tk  (%s)" % os.path.relpath(cand, base), _read_text(cand))
        break
    section("Other native libraries bundled with CPython",
            "OpenSSL 3.x (libssl/libcrypto; Apache License 2.0, https://www.openssl.org/source/license.html), "
            "libffi (MIT), zlib (zlib licence), expat (MIT), bzip2 (BSD-style), liblzma/xz (0BSD / public domain), "
            "libzstd (BSD-3-Clause). Their full terms ship with the CPython distribution licence text above and "
            "upstream projects.")
    section("Microsoft Visual C++ / Universal CRT runtime DLLs",
            "vcruntime140*.dll / msvcp140*.dll / api-ms-win-crt-*.dll etc. are redistributed from the CPython "
            "installation under Microsoft's redistribution terms for the Visual C++ runtime.")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(parts))
    return 0


# ---------------------------------------------------------------- 构建
def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise SystemExit("命令失败（退出码 %s）：%s" % (r.returncode, cmd[0]))
    return r


def pinned_runtime(latest: bool) -> list:
    """打进包的 frida / numpy 版本：默认**钉成当前解释器里已验证过的版本**（requirements.txt 只写 `>=`，
    不钉的话 venv 会装到最新版，包里的 frida 就不是开发机上实测过的那个）。`--latest` 才按 requirements.txt。"""
    if latest:
        return ["-r", os.path.join(AGENT_ROOT, "requirements.txt")]
    import importlib.metadata as md
    out = []
    for n in LICENSE_DISTS:
        try:
            out.append("%s==%s" % (n, md.version(n)))
        except md.PackageNotFoundError:
            raise SystemExit("当前解释器没装 %s，无法钉版本；装好，或用 --latest 按 requirements.txt 装最新" % n)
    return out


def make_venv(venv_dir: str, latest: bool = False) -> str:
    if os.path.isdir(venv_dir):
        shutil.rmtree(venv_dir, ignore_errors=True)
    run([sys.executable, "-m", "venv", venv_dir])
    py = os.path.join(venv_dir, "Scripts", "python.exe")
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q"] + pinned_runtime(latest) + ["pyinstaller"])
    return py


def version_file(path: str, ver: str) -> None:
    t = tuple((list(int(x) for x in ver.split(".") if x.isdigit()) + [0, 0, 0, 0])[:4])
    s = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%s, prodvers=%s, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'HeCPDF'),
      StringStruct('FileDescription', 'KARDS Agent control panel'),
      StringStruct('FileVersion', '%s'),
      StringStruct('InternalName', 'kards-agent'),
      StringStruct('LegalCopyright', 'GPL-3.0-or-later; see LICENSE'),
      StringStruct('OriginalFilename', 'kards-agent.exe'),
      StringStruct('ProductName', 'KARDS Agent'),
      StringStruct('ProductVersion', '%s')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ])
""" % (t, t, ver, ver)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)


def build(py: str, work: str, ver: str) -> str:
    """跑 PyInstaller，返回 onedir 产物目录（含 kards-agent.exe 与 _internal）。"""
    sys.path.insert(0, HERE)
    import gui_closure
    mods = sorted(gui_closure.closure())
    closure_txt = os.path.join(work, "closure_modules.txt")
    with open(closure_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(mods) + "\n")
    vf = os.path.join(work, "version_info.txt")
    version_file(vf, ver)
    sep = os.pathsep                                                   # Windows 是 ';'
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
           "--name", EXE_NAME, "--distpath", os.path.join(work, "pyi-dist"), "--workpath", os.path.join(work, "pyi-build"),
           "--specpath", work, "--paths", AGENT_ROOT, "--paths", HERE, "--version-file", vf,
           "--collect-all", "frida",
           "--add-data", "%s%s%s" % (os.path.join(AGENT_ROOT, "kardsmem", "build_tables.json"), sep, "kardsmem"),
           "--add-data", "%s%s%s" % (os.path.join(AGENT_ROOT, "ops", "agent.js.tpl"), sep, "ops"),
           "--add-data", "%s%s%s" % (closure_txt, sep, "."),
           ]
    for m in mods + ["canplay", "_bootstrap"]:
        cmd += ["--hidden-import", m]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(os.path.join(HERE, "gui_main.py"))
    run(cmd, cwd=work)
    out = os.path.join(work, "pyi-dist", EXE_NAME)
    if not os.path.exists(os.path.join(out, EXE_NAME + ".exe")):
        raise SystemExit("PyInstaller 没有产出 %s.exe" % EXE_NAME)
    return out


def assemble(onedir: str, py: str, ver: str, work: str) -> str:
    final = os.path.join(DIST, "kards-agent-gui-win64-%s" % ver)
    os.makedirs(DIST, exist_ok=True)
    if os.path.isdir(final):
        shutil.rmtree(final)
    shutil.copytree(onedir, final)
    for fn in ("LICENSE", "NOTICE"):
        shutil.copy2(os.path.join(AGENT_ROOT, fn), os.path.join(final, fn))
    with open(os.path.join(final, "README-GUI.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(README.format(ver=ver, repo=REPO_URL))
    run([py, os.path.join(HERE, "build_exe.py"), "--emit-notices", os.path.join(final, "THIRD-PARTY-NOTICES.txt")])
    return final


def dir_size(p: str) -> int:
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)


def make_zip(final: str) -> str:
    zp = final + ".zip"
    if os.path.exists(zp):
        os.remove(zp)
    top = os.path.basename(final)
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for r, _, fs in os.walk(final):
            for f in sorted(fs):
                full = os.path.join(r, f)
                z.write(full, os.path.join(top, os.path.relpath(full, final)))
    h = hashlib.sha256()
    with open(zp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with open(zp + ".sha256", "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (h.hexdigest(), os.path.basename(zp)))
    return zp


# ---------------------------------------------------------------- 离线验收
def verify(final: str) -> bool:
    """成品拷到干净目录（带空格+中文），洗过的环境下跑 probe / selfcheck / 面板冒烟。绝不跑 --listener。"""
    root = tempfile.mkdtemp(prefix="kards 验收 ")
    dst = os.path.join(root, os.path.basename(final))
    shutil.copytree(final, dst)
    exe = os.path.join(dst, EXE_NAME + ".exe")
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = {"SystemRoot": sysroot, "windir": sysroot, "COMSPEC": os.path.join(sysroot, "System32", "cmd.exe"),
           "PATH": os.pathsep.join([os.path.join(sysroot, "System32"), sysroot,
                                    os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0")]),
           "TEMP": os.path.join(root, "tmp"), "TMP": os.path.join(root, "tmp"),
           "USERPROFILE": os.path.join(root, "home"), "LOCALAPPDATA": os.path.join(root, "home", "AppData", "Local"),
           "APPDATA": os.path.join(root, "home", "AppData", "Roaming")}
    for k in ("TEMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        os.makedirs(env[k], exist_ok=True)
    ok = True

    def step(name, args, extra_env=None, timeout=240):
        nonlocal ok
        e = dict(env, **(extra_env or {}))
        t0 = time.time()
        try:
            r = subprocess.run([exe] + args, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, cwd=root)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = -1, "超时"
        good = rc == 0
        print("[验收] %-22s rc=%s %.1fs %s" % (name, rc, time.time() - t0, "OK" if good else "FAIL"), flush=True)
        if not good:
            ok = False
        return rc, out

    step("--probe", ["--probe"])
    rc, out = step("--selfcheck", ["--selfcheck"])
    sc = os.path.join(dst, "data", "selfcheck.txt")
    txt = open(sc, encoding="utf-8").read() if os.path.exists(sc) else out
    print(txt)
    if "自检通过" not in txt:
        ok = False
        print("[验收] FAIL：selfcheck 文本里没有“自检通过”")
    step("面板冒烟", [], {"KARDS_GUI_SELFTEST_EXIT_MS": "4000"})
    marker = os.path.join(dst, "data", "gui", "gui_smoke_ok.txt")
    if os.path.exists(marker):
        print("[验收] 面板标记:", open(marker, encoding="utf-8").read())
    else:
        ok = False
        print("[验收] FAIL：没有 gui_smoke_ok.txt（Tk 窗口没建出来 / 事件循环没跑）")
    shutil.rmtree(root, ignore_errors=True)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--emit-notices", metavar="OUT", help="（内部）把第三方许可证汇总写到 OUT 就退出，在构建解释器里跑")
    ap.add_argument("--no-venv", action="store_true", help="不建一次性 venv，直接用当前解释器（需已装 pyinstaller）")
    ap.add_argument("--venv-dir", help="venv 位置（默认系统临时目录）")
    ap.add_argument("--keep-venv", action="store_true")
    ap.add_argument("--latest", action="store_true", help="frida/numpy 按 requirements.txt 装最新（默认钉成当前解释器里的版本）")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    a = ap.parse_args(argv)
    if a.emit_notices:
        return emit_notices(a.emit_notices)
    t0 = time.time()
    ver = read_version()
    work = tempfile.mkdtemp(prefix="kards-agent-build-")
    venv_dir = a.venv_dir or os.path.join(tempfile.gettempdir(), "kards-agent-buildvenv")
    py = sys.executable if a.no_venv else make_venv(venv_dir, a.latest)
    t_env = time.time()
    onedir = build(py, work, ver)
    t_build = time.time()
    final = assemble(onedir, py, ver, work)
    print("\n成品目录: %s  (%.1f MB)" % (final, dir_size(final) / 1e6))
    good = True if a.no_verify else verify(final)
    if good and not a.no_zip:
        zp = make_zip(final)
        print("zip: %s  (%.1f MB)" % (zp, os.path.getsize(zp) / 1e6))
    print("用时: 环境 %.0fs + PyInstaller %.0fs + 其余 %.0fs" % (t_env - t0, t_build - t_env, time.time() - t_build))
    shutil.rmtree(work, ignore_errors=True)
    if not a.keep_venv and not a.no_venv and not a.venv_dir:
        shutil.rmtree(venv_dir, ignore_errors=True)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
