#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开局按钮灰态开关（`KARDS_PLAY_BLOCK_GRAY`）离线测试，不碰游戏。

背景（2026-10-03 用户拍板）：`press_play` 默认**不拦**灰按钮 —— 这行只决定我们自己
发不发这次点击，不改按钮 `enabled`/视觉（界面照旧是灰的），也不碰服务端校验；
开局是否真成立由 `gui.autoplay.verify_start()` 点击后**只读**判定。
原判据（灰的就不点）保留为开关：设 `KARDS_PLAY_BLOCK_GRAY=1` 即恢复。
"""
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import inject as OI                                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    key = "KARDS_PLAY_BLOCK_GRAY"
    old = os.environ.pop(key, None)
    try:
        chk("默认（未设）= 不拦灰按钮", OI.play_gray_guard_on() is False)
        for v, want in (("1", True), ("true", True), ("TRUE", True), ("yes", True),
                        ("on", True), ("0", False), ("", False), ("no", False)):
            os.environ[key] = v
            chk("%s=%r ⇒ %s" % (key, v, "恢复原判据" if want else "不拦"),
                OI.play_gray_guard_on() is want)
    finally:
        os.environ.pop(key, None)
        if old is not None:
            os.environ[key] = old
    src = inspect.getsource(OI.Injector.press_play)
    chk("press_play 真的走这个开关", "play_gray_guard_on()" in src)
    chk("press_play 里不再有“永久不拦”的 if not True", "if not True" not in src)
    chk("原判据仍在（开关打开时生效）", 'st.get("clickable")' in src)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
