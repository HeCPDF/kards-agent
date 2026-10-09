# -*- coding: utf-8 -*-
"""`tools/launch_with_credentials.py`：参数校验、凭据读取、写注册表、备份、启动——全用假的 winreg / 假的 spawn，
不碰真实注册表、不起进程。输出里不得出现凭据值。"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tools import launch_with_credentials as L                                  # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


class FakeReg:
    """够用的 winreg 替身：只有 HKCU\\Software\\Kards 一个键。"""
    HKEY_CURRENT_USER = 1
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.exists = values is not None

    def OpenKey(self, root, sub, res, access):
        if not self.exists:
            raise OSError("no key")
        return self

    def CreateKeyEx(self, root, sub, res, access):
        assert sub == L.REG_SUBKEY
        self.exists = True
        return self

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("no value")
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, res, typ, value):
        assert typ == self.REG_SZ
        self.values[name] = value

    def CloseKey(self, key):
        pass


class FakeProc:
    pid = 4242


SECRET_ID, SECRET_PW = "device:Windows-AAAA1111", "BBBB2222"
CREDS = {"device_id": SECRET_ID, "device_password": SECRET_PW}

tmp = tempfile.mkdtemp(prefix="kards-launch-test-")
exe = os.path.join(tmp, "kards-Win64-Shipping.patched.exe")
open(exe, "wb").write(b"MZ")
launcher = os.path.join(tmp, "kards.exe")
open(launcher, "wb").write(b"MZ")
notgame = os.path.join(tmp, "notepad.exe")
open(notgame, "wb").write(b"MZ")

# --- exe 校验 ---
chk("接受 kards-Win64-Shipping.patched.exe", L.check_exe(exe) == os.path.abspath(exe))
for p, why in ((launcher, "kards.exe 启动器"), (notgame, "不是游戏本体"), (os.path.join(tmp, "nope.exe"), "不存在")):
    try:
        L.check_exe(p)
        chk("拒绝 " + why, False)
    except L.LaunchError:
        chk("拒绝 " + why, True)

# --- 凭据读取 ---
cf = os.path.join(tmp, "cred.json")
json.dump(CREDS, open(cf, "w", encoding="utf-8"))
chk("从文件读凭据", L.load_credentials(cf, environ={}) == CREDS)
chk("从环境变量读凭据", L.load_credentials(None, environ={"KARDS_DEVICE_ID": SECRET_ID, "KARDS_DEVICE_PASSWORD": SECRET_PW}) == CREDS)
for env, why in (({}, "全缺"), ({"KARDS_DEVICE_ID": SECRET_ID}, "缺密码"), ({"KARDS_DEVICE_ID": "a\nb", "KARDS_DEVICE_PASSWORD": "x"}, "含控制字符")):
    try:
        L.load_credentials(None, environ=env)
        chk("环境变量 " + why + " ⇒ 报错", False)
    except L.LaunchError as e:
        chk("环境变量 " + why + " ⇒ 报错（消息不含凭据值）", SECRET_ID not in str(e) and SECRET_PW not in str(e))

# --- 启动：旧值被备份、新值写入、exe 被 spawn、返回值不含凭据 ---
spawned = []
reg = FakeReg({"device_id": "device:Windows-OLD", "device_password": "OLDPW"})
bk = os.path.join(tmp, "bk")
info = L.launch(exe, CREDS, winreg_mod=reg, spawn_fn=lambda e: (spawned.append(e), FakeProc())[1], backup_dir=bk)
chk("注册表写入了新凭据", reg.values == CREDS)
chk("只 spawn 了指定的 exe", spawned == [os.path.abspath(exe)] and info["pid"] == 4242)
chk("旧值已备份", info["backup"] and json.load(open(info["backup"])) == {"device_id": "device:Windows-OLD", "device_password": "OLDPW"})
chk("返回值不含凭据值", SECRET_ID not in json.dumps(info) and SECRET_PW not in json.dumps(info) and info["registry_changed"] is True)

# 没有旧值 ⇒ 不产生备份；值相同 ⇒ registry_changed 为假
reg2 = FakeReg()
info2 = L.launch(exe, CREDS, winreg_mod=reg2, spawn_fn=lambda e: FakeProc(), backup_dir=os.path.join(tmp, "bk2"))
chk("没有旧值 ⇒ 没有备份目录", info2["backup"] is None and not os.path.exists(os.path.join(tmp, "bk2")))
info3 = L.launch(exe, CREDS, winreg_mod=reg2, spawn_fn=lambda e: FakeProc(), backup_dir=os.path.join(tmp, "bk3"))
chk("值相同 ⇒ registry_changed 为假", info3["registry_changed"] is False)

# --- 命令行：--dry-run 不写、不启动、不打印凭据 ---
buf = io.StringIO()
with redirect_stdout(buf):
    rc = L.main(["--exe", exe, "--cred-file", cf, "--dry-run"])
out = buf.getvalue()
chk("--dry-run 成功且输出不含凭据值", rc == 0 and SECRET_ID not in out and SECRET_PW not in out)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = L.main(["--exe", launcher, "--cred-file", cf, "--dry-run"])
chk("--dry-run 指到启动器 ⇒ 失败码 2", rc == 2)

print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
