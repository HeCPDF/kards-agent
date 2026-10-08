#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_release.py —— 从**已提交**内容导出 kards-agent 子树，打包，并在干净目录里跑离线测试。

做什么
------
1. 用 `git archive`（对 `--ref`，默认 HEAD 的已提交内容，**不是工作区**）导出 kards-agent 子树；
2. 写 `dist/kards-agent-<版本>.zip` 与干净目录 `dist/kards-agent-<版本>/`（`dist/` 已在 .gitignore）；
3. 把 zip 解到**系统临时目录**（不在伞仓库里，免得 `base.workspace` 往上找到开发期的 `reverse-data/`），
   用隔离环境（清掉 PYTHONPATH / KARDS_* 环境变量）跑 `python tests/run_all.py`，只打印结果；
4. 默认建临时 venv 并 `pip install -r requirements.txt`（需要网络）；`--no-venv` 直接用当前解释器。

只读：不写回仓库，不做任何会改仓库或联网的 git 操作（发布纪律只在 README / CLAUDE.md 里以文字描述，
推送由人手工执行）。版本号来自 `base/version.py`。

用法
----
    python tools/make_release.py                 # 导出 + venv + 测试
    python tools/make_release.py --no-venv       # 用当前解释器测试（快；依赖要已装好）
    python tools/make_release.py --dry-run       # 只打印会导出什么，不写任何文件
    python tools/make_release.py --no-test       # 只打包
    python tools/make_release.py --keep          # 保留测试用的临时目录
    python tools/make_release.py --overlay README.md --overlay base/version.py
                                                 # 预演：把工作区里尚未提交的文件叠到导出物上（正式发布不要用）
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(HERE)
DIST = os.path.join(AGENT_ROOT, "dist")
VERSION_FILE = "base/version.py"


def git(*args: str, cwd: str = AGENT_ROOT, text: bool = True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text)
    if r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise SystemExit("git %s 失败：%s" % (" ".join(args), err.strip()))
    return r.stdout


def repo_prefix() -> str:
    """kards-agent 相对 git 顶层的前缀（开发布局是 `kards-agent/`；发布出去的独立仓库是空串）。"""
    return git("rev-parse", "--show-prefix").strip().rstrip("/")


def read_version(ref: str, prefix: str) -> str:
    path = (prefix + "/" if prefix else "") + VERSION_FILE
    src = None
    try:
        src = git("show", "%s:%s" % (ref, path))
    except SystemExit:
        wt = os.path.join(AGENT_ROOT, VERSION_FILE)
        if os.path.isfile(wt):
            print("!! %s 不在 %s 里，改读工作区的版本号（该文件尚未提交）" % (VERSION_FILE, ref))
            src = open(wt, encoding="utf-8").read()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src or "")
    if not m:
        raise SystemExit("读不到版本号（%s）" % VERSION_FILE)
    return m.group(1)


def export_tar(ref: str, prefix: str) -> bytes:
    treeish = "%s:%s" % (ref, prefix) if prefix else ref
    # 必须在 git 顶层执行：在子目录里跑 `git archive <tree>:<子目录>` 会再套一层 cwd 前缀 ⇒ 导出 0 个文件
    top = git("rev-parse", "--show-toplevel").strip()
    r = subprocess.run(["git", "archive", "--format=tar", treeish], cwd=top, capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git archive 失败：" + r.stderr.decode("utf-8", "replace"))
    return r.stdout


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("KARDS_") and k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(cmd, cwd, env) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="导出哪个提交（默认 HEAD）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将导出的内容，不写任何文件、不跑测试")
    ap.add_argument("--no-venv", action="store_true", help="不建临时 venv，直接用当前解释器跑测试")
    ap.add_argument("--no-test", action="store_true", help="只打包，不跑测试")
    ap.add_argument("--keep", action="store_true", help="保留测试用临时目录（默认测完删除）")
    ap.add_argument("--overlay", action="append", default=[], metavar="PATH",
                    help="（预演用）把工作区里这个相对路径的文件叠到导出物上；可重复")
    a = ap.parse_args(argv)

    prefix = repo_prefix()
    ver = read_version(a.ref, prefix)
    name = "kards-agent-" + ver
    print("ref=%s  版本=%s  子树前缀=%r" % (a.ref, ver, prefix))

    data = export_tar(a.ref, prefix)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        total = sum(m.size for m in members)
        print("将导出 %d 个文件，%.1f KB" % (len(members), total / 1024))
        big = sorted(((m.size, m.name) for m in members if m.size > 1024 * 1024), reverse=True)
        for sz, n in big:
            print("!! 大于 1 MB：%s (%.1f MB)" % (n, sz / 1048576))
        if a.dry_run:
            top = sorted({m.name.split("/")[0] for m in members})
            print("顶层：" + ", ".join(top))
            print("--dry-run：不写文件。目标将是 dist/%s.zip 与 dist/%s/" % (name, name))
            return 0

        overlays = []
        for rel in a.overlay:
            p = os.path.join(AGENT_ROOT, rel)
            if not os.path.isfile(p):
                raise SystemExit("--overlay 找不到：" + rel)
            overlays.append((rel.replace("\\", "/"), p))

        os.makedirs(DIST, exist_ok=True)
        zip_path = os.path.join(DIST, name + ".zip")
        dir_path = os.path.join(DIST, name)
        if os.path.isdir(dir_path):
            shutil.rmtree(dir_path)
        os.makedirs(dir_path)
        tf.extractall(dir_path, filter="data")
    for rel, p in overlays:
        dst = os.path.join(dir_path, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(p, dst)
        print("overlay：" + rel)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dir_path):
            for f in sorted(files):
                full = os.path.join(root, f)
                arc = os.path.join(name, os.path.relpath(full, dir_path))
                zf.write(full, arc.replace("\\", "/"))
    print("已写：%s (%.1f KB)；干净目录：%s" % (zip_path, os.path.getsize(zip_path) / 1024, dir_path))

    if a.no_test:
        return 0

    # 在系统临时目录里解 zip 再测：既验证 zip 本身，也避开伞仓库（base.workspace 会往上找 reverse-data/）
    tmp = tempfile.mkdtemp(prefix="kards-release-test-")
    rc = 1
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        root = os.path.join(tmp, name)
        env = clean_env()
        py = sys.executable
        if not a.no_venv:
            venv = os.path.join(tmp, "venv")
            if run([sys.executable, "-m", "venv", venv], tmp, env) != 0:
                raise SystemExit("建 venv 失败")
            py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python" + (".exe" if os.name == "nt" else ""))
            if run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements-dev.txt"], root, env) != 0:
                raise SystemExit("pip install 失败（需要网络）")
            run([py, "-m", "pip", "list", "--disable-pip-version-check", "--format=freeze"], root, env)
        print("\n=== 干净目录离线测试：%s ===" % root, flush=True)
        rc = run([py, os.path.join("tests", "run_all.py")], root, env)
        print("=== run_all 退出码 %d（%s）===" % (rc, "全绿" if rc == 0 else "有失败"))
    finally:
        if a.keep:
            print("保留临时目录：" + tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
