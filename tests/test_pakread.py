#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/pakread.py 离线测试：候选 key 解析、FString、(存在时) 真 pak 的 ProjectVersion 与目录名一致。"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import pakread as P                                  # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    fake = "AESDumpster\nKey: 0x" + "ab" * 32 + " | Key Entropy: 3.7\n\nKey: 0x" + "01" * 32 + " | Key Entropy: 3.5\nDone!"
    ks = P.keys_from_exe("x.exe", runner=lambda p: fake)
    chk("keys_from_exe 解析出 2 个 32 字节候选，保持顺序", [k[0] for k in ks] == [0xAB, 0x01] and all(len(k) == 32 for k in ks))
    chk("_fstring: ASCII 与 UTF-16",
        P._fstring(b"\x04\x00\x00\x00abc\x00", 0) == ("abc", 8)
        and P._fstring(b"\xfd\xff\xff\xff" + "ab".encode("utf-16-le") + b"\x00\x00", 0)[0] == "ab")
    try:
        P.open_with_any_key(os.devnull, [b"\x00" * 32])
        chk("坏文件应抛 ValueError", False)
    except (ValueError, OSError):
        chk("坏文件应抛 ValueError/OSError", True)
    # 真 pak（本机有才测）：每棵树的 ProjectVersion 必须等于目录名（命名规则 <版本号>.<渠道> 来自客户端自报）
    try:
        fk = P.load_fmodel_key()
    except Exception:                                           # noqa: BLE001
        fk = None
    trees = [t for t in glob.glob(r"D:\Kards\game-installs\*") if os.path.basename(t)[:1].isdigit()]
    if fk is None or not trees:
        print("  [SKIP] 本机没有 FModel key / game-installs，跳过真 pak 测试")
    for t in trees if fk is not None else []:
        r = P.install_version(t, fk)
        chk("真 pak：%s 的 ProjectVersion == 目录名" % os.path.basename(t), r["version"] == os.path.basename(t), str(r["version"]))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
