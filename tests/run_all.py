# -*- coding: utf-8 -*-
"""跑全部离线测试（不需要游戏）。用法：`python tests/run_all.py [关键字]`；有失败时退出码非 0。

★ **约定（2026-10-04，用户要求）：跑测试绝不许弹出 GUI 窗口。**
  所以这里给每个子进程**显式**设置 `KARDS_TEST_GUI_WINDOW=0` —— 属于「无窗口」环境，
  任何测试都不许在自己进程里建 Tk 窗口 / 起面板 / 起监听器（判据见 `tests/test_no_gui_guard.py`）。
  想**手动**跑那条会建窗口的面板冒烟，得自己单独执行、并且临时设 `KARDS_TEST_GUI_WINDOW=1`
  （`run_all.py` 不会给你开这扇门）。
  环境是**显式构造**的（`{**os.environ, ...}` 再覆盖那一个键），不是"碰巧继承"——
  将来谁想在后代进程里开窗口，得先改这一行、而且会被棘轮测试拦下。
"""
import concurrent.futures as cf
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: 交给每个测试子进程的环境：**显式关掉**面板窗口冒烟（见文件头约定）。
CHILD_ENV = {**os.environ, "KARDS_TEST_GUI_WINDOW": "0"}


def _run_one(f):
    t1 = time.time()
    r = subprocess.run([sys.executable, os.path.join(HERE, f)], capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace", env=CHILD_ENV)
    return f, r.returncode == 0, time.time() - t1, (r.stdout + r.stderr).strip().splitlines()[-4:]


def main(argv):
    """`python tests/run_all.py [关键字] [--serial] [-jN]`：缺省**并行**跑（每个测试一个子进程，互不共享状态；
    2026-10-06 用户嫌串行太慢）；`--serial` 或 `-j1` 回到逐个跑（排查测试间互相干扰时用）。"""
    serial = "--serial" in argv
    jobs = min(8, os.cpu_count() or 4)
    for a in argv:
        if a.startswith("-j") and a[2:].isdigit():
            jobs = max(1, int(a[2:]))
    if serial:
        jobs = 1
    argv = [a for a in argv if not a.startswith("-")]
    key = argv[0] if argv else ""
    names = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py") and key in f)
    bad = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for f, ok, dt, tail in ex.map(_run_one, names):     # map 按名字顺序产出 ⇒ 输出顺序稳定
            print("%s %-40s %5.1fs" % ("PASS" if ok else "FAIL", f, dt), flush=True)
            if not ok:
                bad.append(f)
                print("     " + chr(10).join("     " + x for x in tail).lstrip(), flush=True)
    print(chr(10) + "共 %d 个，失败 %d 个，用时 %.0f s（并行 %d）" % (len(names), len(bad), time.time() - t0, jobs))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
