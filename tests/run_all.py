# -*- coding: utf-8 -*-
"""跑全部离线测试（不需要游戏）。用法：`python tests/run_all.py [关键字]`；有失败时退出码非 0。"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    key = argv[0] if argv else ""
    names = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py") and key in f)
    bad = []
    t0 = time.time()
    for f in names:
        t1 = time.time()
        r = subprocess.run([sys.executable, os.path.join(HERE, f)], capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        print("%s %-40s %5.1fs" % ("PASS" if ok else "FAIL", f, time.time() - t1), flush=True)
        if not ok:
            bad.append(f)
            tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
            print("     " + "\n     ".join(tail), flush=True)
    print("\n共 %d 个，失败 %d 个，用时 %.0f s" % (len(names), len(bad), time.time() - t0))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
