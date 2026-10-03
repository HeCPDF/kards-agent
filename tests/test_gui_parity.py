#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板功能清单（参照 OCR-Kards-Auto 面板）的纯函数层（gui/core.py、gui/update_check.py）+ 面板冒烟（建窗口刷一次）。"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gui import core as K                                              # noqa: E402
from gui import update_check as U                                      # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += (not ok)


def main():
    d = tempfile.mkdtemp()
    # ---- 功能选择 ----
    o = K.build_opts({"play": True, "move": True, "attack": False, "end_turn": True, "dry_run": False})
    chk("功能选择：没勾“攻击” ⇒ forbid=attack", o == {"forbid": ["attack"], "dry_run": False}, str(o))
    o2 = K.build_opts({"play": False, "move": False, "attack": True, "end_turn": False, "dry_run": True})
    chk("功能选择：全关 + 只看不动", "play_unit" in o2["forbid"] and "move_up" in o2["forbid"] and "end" in o2["forbid"]
        and "attack" not in o2["forbid"] and o2["dry_run"] is True, str(o2))
    back = K.checked_from_control(o2)
    chk("功能选择：control.json → 勾选 往返一致", back == {"play": False, "move": False, "attack": True,
                                                        "end_turn": False, "dry_run": True}, str(back))
    chk("默认全开（只看不动默认关）", K.build_opts({}) == {"forbid": [], "dry_run": False})

    # ---- 日志 tail / rev / 着色 / 关键行 ----
    p = os.path.join(d, "a.log")
    with open(p, "w", encoding="utf-8") as f:
        for i in range(1000):
            f.write("[00:00:%02d] 第%d行 汉字汉字汉字\n" % (i % 60, i))
    t = K.tail_lines(p, 400)
    chk("tail_lines：末尾 400 行、汉字没被切断", len(t) == 400 and t[-1].endswith("汉字") and "第999行" in t[-1]
        and "�" not in "".join(t))
    r1 = K.log_rev(p)
    time.sleep(0.01)
    with open(p, "a", encoding="utf-8") as f:
        f.write("新增\n")
    chk("log_rev：追加一行后指纹变化（尾部行数仍是 400，行数判据会失效）", K.log_rev(p) != r1 and len(K.tail_lines(p, 400)) == 400)
    chk("log_rev：文件不存在 ⇒ 空串", K.log_rev(os.path.join(d, "nope")) == "")
    chk("mark_line", K.mark_line("Traceback x") == "bad" and K.mark_line("开局失败") == "warn"
        and K.mark_line("[1] 开局：ok=True") == "ok" and K.mark_line("检测到对局进行中 ⇒ 直接接手") == "act"
        and K.mark_line("     缩进") == "dim")
    chk("只看关键行", K.only_key(["无关", "面板：开始", "本局结束：x"]) == ["面板：开始", "本局结束：x"])

    # ---- 状态解析 ----
    ev = ["[10:00:00] 面板：开始（{\"games_total\": 2, \"auto_next\": true}）",
          "[10:01:00] 开局：ok=True ",
          "[10:09:00] 本局结束：{'played': True, 'seconds': 480.0, 'n': 50, 'exception': None} | 已打 1 局 | 排队下一局=True",
          "[10:10:00] 对局读数是上一局的残留（牌组页已就绪、动作流不动）⇒ 等待清掉",
          "[10:11:30] 等待残留：{'cleared': False}",
          "[10:11:30] 本局结束：{'played': False}"]
    rows = [{"turn": 7, "kind": "attack", "note": "搜索：attack X → Y", "ok": True,
             "raw": {"kredits": 5, "probe": {"timing": {"sim_units": 6, "sim_hand": 4}}}},
            {"turn": 7, "kind": "end", "note": "结束", "ok": False, "raw": {"kredits": 0}}]
    ps = K.parse_status(ev, rows, {"state": "stopped", "note": "上一局残留读数 92 s 没清掉", "busy": False})
    chk("parse_status：局数 / 分钟", len(ps["rounds"]) == 2 and ps["rounds"][0]["min"] == 8.0 and ps["rounds"][0]["played"]
        and not ps["rounds"][1]["played"], str(ps["rounds"]))
    chk("parse_status：开始参数 / 回合 / 费用 / 最近动作",
        ps["started"]["games_total"] == 2 and ps["turn"] == 7 and ps["kredits"] == 0
        and ps["last_action"]["kind"] == "end", str(ps))
    chk("parse_status：在等残留 ⇒ waiting", ps["waiting"] is True)
    chk("parse_status：问题数 > 0（停手 / 失败 / 没清掉 + ok=False 的步）", ps["issues"] >= 2, str(ps["issues"]))
    ps2 = K.parse_status(ev[:3], [], {"state": "playing", "note": "", "busy": True})
    chk("parse_status：正常局 ⇒ 不在等、无问题", ps2["waiting"] is False and ps2["issues"] == 0, str(ps2))

    # ---- 退出码 / 解释器 ----
    chk("explain_exit_code：缺依赖点名", "frida" in K.explain_exit_code(1, "ModuleNotFoundError: No module named 'frida'"))
    chk("explain_exit_code：attach 失败", "attach" in K.explain_exit_code(1, "precheck.available() = False"))
    chk("explain_exit_code：103", "103" in K.explain_exit_code(103))
    chk("explain_exit_code：0 ⇒ 正常退出", "正常" in K.explain_exit_code(0))
    chk("python_probe：真解释器通过", K.python_probe(sys.executable)[0] is True)
    chk("python_probe：坏解释器不通过", K.python_probe(os.path.join(d, "nope.exe"))[0] is False)
    bad = os.path.join(d, "bad.exe")
    open(bad, "w").write("not an exe")
    py = K.engine_python(cands=[bad, sys.executable])
    chk("engine_python：坏的跳过、挑到能用的，原因入表", py == sys.executable and K._PYTHON_DIAG
        and K._PYTHON_DIAG[0][0].endswith("bad.exe"), str(K._PYTHON_DIAG))
    chk("engine_python：一个都没有 ⇒ None", K.engine_python(cands=[os.path.join(d, "x.exe")]) is None
        and len(K._PYTHON_DIAG) == 1)

    # ---- 版本 / 更新日志 ----
    vp = os.path.join(d, "v.json")
    with open(vp, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps({"version": "1.2.3"}).encode())
    old = K.VERSION_FILE
    K.VERSION_FILE = vp
    chk("read_version：吃掉 BOM（记事本写的）", K.read_version()["version"] == "1.2.3")
    K.VERSION_FILE = os.path.join(d, "none.json")
    chk("read_version：没文件 ⇒ git 短哈希或 0.0.0（不抛）", isinstance(K.read_version()["version"], str))
    K.VERSION_FILE = old
    lp = os.path.join(d, "u.log")
    K.log_update("hello", path=lp)
    chk("log_update：留痕", "hello" in open(lp, encoding="utf-8").read())

    # ---- 窗口几何 ----
    wp = os.path.join(d, "w.json")
    K.save_win_state(1300, 800, 100, 50, path=wp)
    chk("窗口几何：存取", K.load_win_state(wp) == {"w": 1300, "h": 800, "x": 100, "y": 50})
    chk("窗口几何：坏文件 ⇒ 空", K.load_win_state(os.path.join(d, "none.json")) == {})
    chk("clamp：副屏拔了，位置夹回屏幕内", K.clamp_to_screen(5000, 4000, 1000, 700, screen=(1920, 1080)) == (1720, 960))
    chk("geometry：不小于最小尺寸", K.geometry_string({"w": 100, "h": 100}, screen=(1920, 1080)).startswith(
        "%dx%d" % K.WIN_MIN))

    # ---- 检测更新（网络 / git 都注入）----
    chk("版本比较", U.newer("v1.10.0", "1.9.9") and not U.newer("1.0", "1.0.0") and not U.newer("1.0.0", "1.0.1"))
    r = U.check("1.0.0", "http://x", http=lambda u: {"tag_name": "v1.1.0", "html_url": "http://rel"})
    chk("更新：URL 源有新版本", r["ok"] and r["has_update"] and r["url"] == "http://rel", str(r))
    r = U.check("1.1.0", "http://x", http=lambda u: {"tag_name": "v1.1.0"})
    chk("更新：URL 源已是最新", r["ok"] and not r["has_update"] and "已是最新" in r["msg"])

    def boom(u):
        raise OSError("断网")
    r = U.check("1.0.0", "http://x", http=boom)
    chk("更新：查不到就说查不到（ok=False，且明说不能说已是最新）", r["ok"] is False and not r["has_update"]
        and "不能说已是最新" in r["msg"], str(r))
    r = U.check("1.0.0", None, git=lambda a, cwd: "")
    chk("更新：没有任何更新源 ⇒ 如实报没配置（ok=False）", r["ok"] is False and r["via"] == "none"
        and "没有配置更新源" in r["msg"], str(r))

    def fake_git(a, cwd):
        return {"remote": "origin\n", "rev-parse": "aaaa1111\n", "ls-remote": "bbbb2222\tHEAD\n"}[a[0]]
    r = U.check("x", None, git=fake_git)
    chk("更新：git 远端 HEAD 不同 ⇒ 有更新（不自动拉）", r["ok"] and r["has_update"] and "不自动拉取" in r["msg"], str(r))

    # ---- 面板冒烟：建窗口、刷一次、读状态 ----
    import tkinter
    try:
        from gui import app as A
        a = A.App()
        a.update()
        a._tick()
        a.update()
        chk("面板冒烟：建窗口 + 刷新一次不抛", a.pill.cget("text") != "" and set(a.kv) >= {"kre", "board", "act", "rounds", "opts"})
        a.v_sw["attack"].set(False)
        s = a._settings()
        chk("面板：取消“攻击”勾选 ⇒ 设置里 forbid=attack", s["forbid"] == ["attack"], str(s))
        a.destroy()
    except tkinter.TclError as e:                             # 没有显示器（CI）时跳过
        print("  [SKIP] 面板冒烟：无显示 (%s)" % e)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
