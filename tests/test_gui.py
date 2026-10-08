#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui 包离线测试：控制文件、历史解析、autoplay 编排（假 sess/precheck/play，不碰游戏）。"""
import json
import shutil
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["KARDS_GUI_DIR"] = tempfile.mkdtemp(prefix="kards_gui_test_")

from gui import autoplay as AP, control as C, history as H, watcher as W      # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class FakePre:
    def __init__(self, ready=True, press_ok=True, notify=None):
        self.calls, self.ready, self.press_ok = [], ready, press_ok
        self.notify = list(notify or [])

    def call_read(self, name, *a, **k):
        if name == "deck_screen_scan":
            return {"W_MatchDeckSelectionDeckButton_C": [1] if self.ready else []}
        if name == "notify_texts":
            return list(self.notify)
        return None

    def call_write(self, name, *a, **k):
        self.calls.append(name)
        if name == "end_of_match_continue":
            return {"ok": False, "error": "没有 W_EndOfMatch_C"}
        if name == "press_play":
            return {"ok": self.press_ok, "error": "" if self.press_ok else "灰的"}
        return {"ok": True}


class FakeGuard:
    """`im` 给标量 ⇒ 每次都同值；给 list ⇒ 按序回（用来看“点完之后才进对局”）。

    `safe_to_start` 复用**上一次 in_match 读到的值**（真实 play_guard 会在里面再读一次；
    这里复用是为了让"step 先判一次 → start_next 再过闸门"只消耗序列里的一个值）。
    """

    def __init__(self, im):
        self.seq = list(im) if isinstance(im, (list, tuple)) else None
        self.im, self.n, self.last = im, 0, None

    def in_match(self, sess):
        if self.seq is not None:
            v = self.seq[min(self.n, len(self.seq) - 1)]
            self.n += 1
            self.last = v
            return v
        self.last = self.im
        return self.im

    def safe_to_start(self, sess, decks=None):
        im = self.last if self.n else self.in_match(sess)
        return (im is False), im, ("" if im is False else "对局进行中")


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s_):
        self.t += s_


def run_step(im=False, done=True, ready=True, press_ok=True, verify_ok=True, notify=None, **ctrl):
    C._write(C.CONTROL, dict(C.DEFAULT_CONTROL))
    C._write(C.STATUS, dict(C.DEFAULT_STATUS))
    C.update_control(run=True, **ctrl)
    queued, pre = [], FakePre(ready, press_ok, notify)
    # im=False 时会走“点开始 → 后置验证”这条路：safe_to_start 吃第一个 False，
    # 之后按 verify_ok 返回（True=真进对局）。守卫对象要**复用**——在 lambda 里现造
    # 会每次都从序列头开始读，永远停在第一个 False。
    g = FakeGuard([im] + [True if verify_ok else False] * 400 if im is False else [im] * 400)
    AP._guard = lambda: g
    import time
    AP.time = type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(time.time)})
    ck = Clock()
    AP._now, AP._sleep = ck.now, ck.sleep
    res = AP.step(None, pre, play=lambda sess, **kw: {"done": done, "n": 7}, enqueue_fn=lambda: queued.append(1))
    return res, pre, queued


def main():
    # 控制文件
    d = C.update_control(games_total=3)
    chk("控制文件读写 + 未知键拒绝", d["games_total"] == 3 and C.load_control()["games_total"] == 3)
    try:
        C.update_control(bogus=1)
        chk("未知控制项应抛", False)
    except KeyError:
        chk("未知控制项应抛", True)
    with open(C.CONTROL, "w", encoding="utf-8") as f:
        f.write("{坏文件")
    chk("坏文件 ⇒ 按默认值", C.load_control()["games_total"] == DEF["games_total"])

    # autoplay：打一局，计划 3 局 ⇒ 打完排下一局
    res, pre, q = run_step(games_total=3)
    st, ct = C.load_status(), C.load_control()
    chk("打完 1/3 局 ⇒ 点了开始、计数+1、排队下一局", res["played"] and "press_play" in pre.calls
        and ct["games_done"] == 1 and len(q) == 1 and ct["run"], str((ct["games_done"], q, st["state"])))
    # 第 3 局 ⇒ 不再排队、run 关
    C.update_control(games_done=2)
    C.update_status(busy=False)
    q2 = []
    g2 = FakeGuard([False, True])
    AP._guard = lambda: g2
    res = AP.step(None, FakePre(), play=lambda s, **k: {"done": True}, enqueue_fn=lambda: q2.append(1))
    chk("最后一局打完 ⇒ 不排队、run=False、games_done=3", not q2 and not C.load_control()["run"]
        and C.load_control()["games_done"] == 3, str(C.load_control()))
    # 满局数后再启动 ⇒ finished
    C.update_control(run=True)
    res = AP.step(None, FakePre(), play=lambda s, **k: {"done": True}, enqueue_fn=lambda: None)
    chk("局数已满再启动 ⇒ skipped finished", res.get("skipped") == "finished", str(res))
    # 不自动继续
    res, pre, q = run_step(games_total=5, auto_next=False)
    chk("auto_next=False ⇒ 打完一局就停、不排队", not q and not C.load_control()["run"], str(C.load_control()))
    # 不限局数
    res, pre, q = run_step(unlimited=True, games_total=1)
    chk("unlimited ⇒ 一直排队", len(q) == 1 and C.load_control()["run"])
    # 对局进行中 ⇒ 接手，不点开始
    res, pre, q = run_step(im=True, games_total=2)
    chk("对局进行中 ⇒ 不点开始直接接手", "press_play" not in pre.calls and res["played"], str(pre.calls))
    # 判不出 ⇒ 停
    res, pre, q = run_step(im=None, games_total=2)
    chk("in_match=None ⇒ 不动游戏、run=False", not pre.calls and not C.load_control()["run"])
    # 点开始失败 ⇒ 不打、不排队
    res, pre, q = run_step(press_ok=False, games_total=2)
    chk("press_play 失败 ⇒ 不排队、run=False", not q and not C.load_control()["run"] and not res.get("played"))
    # ---- 点开始后的只读后置验证（2026-10-03：对战/休闲下大厅按“卡组错误”拒入厅，
    #      press_play 照样回 ok=True，只弹 1~2 s 的浮动通知）----
    res, pre, q = run_step(games_total=2, verify_ok=False, notify=["卡组错误，进入大厅失败"])
    chk("点了开始但没进对局 ⇒ 不接手、不排队、run=False、status 带游戏原话",
        "press_play" in pre.calls and not res.get("played") and not q
        and not C.load_control()["run"] and "卡组错误，进入大厅失败" in C.load_status()["note"],
        str((res.get("start"), C.load_status()["note"])))
    ck = Clock()
    g3 = FakeGuard([False, True])
    AP._guard = lambda: g3
    v = AP.verify_start(None, FakePre(notify=["卡组错误，进入大厅失败"]),
                        timeout_s=30, poll_s=1, sleep=ck.sleep, now=ck.now)
    chk("verify_start：第一次 False、第二次 True ⇒ ok（通知照收）",
        v["ok"] and v["waited_s"] >= 1.0 and v["notify"] == ["卡组错误，进入大厅失败"], str(v))
    ck = Clock()
    g4 = FakeGuard([False])
    AP._guard = lambda: g4
    v = AP.verify_start(None, FakePre(notify=["卡组错误，进入大厅失败"]),
                        timeout_s=5, poll_s=1, sleep=ck.sleep, now=ck.now)
    chk("verify_start：超时 ⇒ ok=False + 游戏原话、只等 timeout",
        (not v["ok"]) and v["notify"] == ["卡组错误，进入大厅失败"] and 4.0 <= v["waited_s"] <= 6.0, str(v))

    class BadPre(FakePre):
        def call_read(self, name, *a, **k):
            if name == "notify_texts":
                return {"ok": False, "stopped": "注入侧不健康"}
            return super().call_read(name, *a, **k)
    ck = Clock()
    g5 = FakeGuard([False])
    AP._guard = lambda: g5
    v = AP.verify_start(None, BadPre(), timeout_s=3, poll_s=1, sleep=ck.sleep, now=ck.now)
    chk("verify_start：call_read 被拒（dict）⇒ 不把 dict 键当提示",
        (not v["ok"]) and v["notify"] == [], str(v))
    # 牌组页没就绪 ⇒ 不点开始
    res, pre, q = run_step(ready=False, games_total=2)
    chk("牌组页未就绪 ⇒ 不点开始", "press_play" not in pre.calls and not q)
    # 没打出（done=False）⇒ 不排队
    res, pre, q = run_step(done=False, games_total=3)
    chk("player.play.play 没打完 ⇒ 不排队、不计数", not q and C.load_control()["games_done"] == 0)
    # 立即停
    C._write(C.CONTROL, dict(C.DEFAULT_CONTROL)); C._write(C.STATUS, dict(C.DEFAULT_STATUS))
    C.update_control(run=True, games_total=3)
    g6 = FakeGuard([False, True])
    AP._guard = lambda: g6
    seen = []

    def play_abort(sess, should_stop=None, **kw):
        C.update_control(abort_now=True)
        seen.append(should_stop())
        return {"done": True}
    AP.step(None, FakePre(), play=play_abort, enqueue_fn=lambda: seen.append("Q"))
    ct = C.load_control()
    chk("立即停 ⇒ should_stop 为真、不计局、不排队、abort_now 复位、run=False",
        seen == [True] and ct["games_done"] == 0 and not ct["abort_now"] and not ct["run"], str((seen, ct)))
    # 重复启动
    C.update_status(busy=True)
    chk("busy 时重复启动被忽略", AP.step(None, FakePre(), play=lambda *a, **k: {}).get("skipped") == "busy")

    # ---- 上一局残留读数（实机反复遇到：刚打完一局，闸门一直读到旧值）----
    class FakeSess:
        def __init__(self, seq):
            self.seq, self.i = seq, 0

        def history(self, tail=1):
            v = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return {"ok": True, "rows": [v]} if v is not None else {"ok": False, "rows": []}

    nosleep = lambda s_: None                                                   # noqa: E731
    chk("动作流在长 ⇒ live", AP.classify_in_match(FakeSess([1, 2]), FakePre(True), 0, nosleep) == "live")
    END = {"action_type": "ActionEndMatch"}
    chk("动作流冻住在 ActionEndMatch + 牌组页就绪 ⇒ stale",
        AP.classify_in_match(FakeSess([END, END, END]), FakePre(True), 0, nosleep) == "stale")
    chk("动作流冻住但末条不是 ActionEndMatch（新局换牌中）+ 牌组页读数旧 ⇒ live",
        AP.classify_in_match(FakeSess([5, 5, 5]), FakePre(True), 0, nosleep) == "live")
    chk("动作流冻住 + 牌组页没就绪 ⇒ live（对手回合/过渡）", AP.classify_in_match(FakeSess([5, 5]), FakePre(False), 0, nosleep) == "live")
    chk("动作流读不出 ⇒ unknown（保守）", AP.classify_in_match(FakeSess([None, None]), FakePre(True), 0, nosleep) == "unknown")

    class SeqGuard:
        def __init__(self, vals):
            self.vals, self.i = vals, 0

        def in_match(self, sess):
            v = self.vals[min(self.i, len(self.vals) - 1)]
            self.i += 1
            return v

        def safe_to_start(self, sess, decks=None):
            return True, False, ""

    ck = Clock()
    _sg = SeqGuard([True, True, False])
    AP._guard = lambda: _sg
    w = AP.wait_residual_clear(None, FakePre(), timeout_s=30, poll_s=1, sleep=ck.sleep, now=ck.now)
    chk("残留在第 3 次读数清掉 ⇒ cleared", w["cleared"] is True and w["last"] is False, str(w))
    AP._guard = lambda: SeqGuard([True])
    ck = Clock()
    w = AP.wait_residual_clear(None, FakePre(), timeout_s=5, poll_s=1, sleep=ck.sleep, now=ck.now)
    chk("一直不清 ⇒ 超时不强推", w["cleared"] is False and w["last"] is True, str(w))

    _orig = (AP.classify_in_match, AP.wait_residual_clear)

    def stale_step(clears: bool):
        C._write(C.CONTROL, dict(C.DEFAULT_CONTROL))
        C._write(C.STATUS, dict(C.DEFAULT_STATUS))
        C.update_control(run=True, games_total=2)
        pre, played = FakePre(True), []
        AP._guard = lambda: SeqGuard([True, True, False] if clears else [True])
        _ck = Clock()
        AP._now, AP._sleep = _ck.now, _ck.sleep
        AP.classify_in_match = lambda sess, precheck, **k: "stale"
        AP.wait_residual_clear = lambda sess, precheck, **k: ({"cleared": True, "waited_s": 3.0, "last": False} if clears
                                                           else {"cleared": False, "waited_s": 90.0, "last": True})
        r = AP.step(None, pre, play=lambda sess, **kw: played.append(1) or {"done": True}, enqueue_fn=lambda: None)
        return r, pre, played
    r, pre, played = stale_step(True)
    chk("残留清掉 ⇒ 走开局路径（点开始）再打", "press_play" in pre.calls and played == [1], str((pre.calls, played)))
    r, pre, played = stale_step(False)
    AP.classify_in_match, AP.wait_residual_clear = _orig
    chk("残留清不掉 ⇒ 不点开始、不在不存在的对局上跑 player.play.play、停并提示手动开始",
        not played and "press_play" not in pre.calls and r.get("why") == "stale_residual"
        and not C.load_control()["run"] and "手动" in C.load_status()["note"], str((r, C.load_status()["note"])))

    # 历史解析
    p = os.path.join(os.environ["KARDS_GUI_DIR"], "rule-live-x.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"t": 1.0, "phase": "main", "turn": 3, "kredits": 4,
                            "action": {"kind": "play_unit", "card": 9, "score": 1.5, "note": "n"},
                            "result": {"ok": True}, "executed": True,
                            "extra": {"t_decide": 0.4, "probe": {"eff_src": {"A": "vm", "B": "vm", "C": "none"},
                                                                  "gaps": {"A": "x"}}}}) + "\n")
        f.write('{"半截')
    rows = H.load_rows(p)
    sm = H.summarize(rows)
    chk("历史解析：1 行有效、半截行跳过、eff 汇总、gaps 计数",
        len(rows) == 1 and rows[0]["eff"] == "vm×2 none×1" and rows[0]["gaps"] == 1
        and sm["steps"] == 1 and sm["kinds"] == {"play_unit": 1}, str(rows[0]["eff"]))
    # 监听器停止 / 强杀（注入假 pids/killer，不碰真进程）
    cmd = os.path.join(os.environ["KARDS_GUI_DIR"], "live_cmd.txt")
    C.update_control(run=True)
    W.request_quit(cmd)
    chk("request_quit：abort_now=True、run=False，并追加 quit 行",
        C.load_control()["abort_now"] and not C.load_control()["run"]
        and open(cmd, encoding="utf-8").read().splitlines()[-1] == "quit")
    seq = [[1], [1], []]
    clock = {"t": 0.0}
    ok = W.wait_exit(10, 1, pids_fn=lambda: seq.pop(0) if seq else [], sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                     clock=lambda: clock["t"])
    chk("wait_exit：进程消失 ⇒ True", ok is True)
    clock["t"] = 0.0
    ok = W.wait_exit(3, 1, pids_fn=lambda: [7], sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                     clock=lambda: clock["t"])
    chk("wait_exit：一直不退 ⇒ 超时 False", ok is False)
    killed = []
    ks = W.force_kill(pids_fn=lambda: [11, 12], killer=killed.append)
    chk("force_kill：对每个 pid 调 killer", ks == [11, 12] and killed == [11, 12])
    lp = os.path.join(os.environ["KARDS_GUI_DIR"], "live_log_t.txt")
    NL = chr(10)
    open(lp, "w", encoding="utf-8").write("old precheck.available() = True" + NL)
    off = os.path.getsize(lp)
    chk("launch_state: only text after offset counts (old True ignored)", W.launch_state(lp, off) == "pending")
    open(lp, "a", encoding="utf-8").write("precheck.available() = True  (err=None)" + NL)
    chk("launch_state: attach 成功但命令通道未就绪 => pending（此时写命令会丢）", W.launch_state(lp, off) == "pending")
    open(lp, "a", encoding="utf-8").write("命令通道就绪（之后追加到 live_cmd.txt 的命令才会被执行）" + NL)
    chk("launch_state: new True + 通道就绪 => ready", W.launch_state(lp, off) == "ready")
    off2 = os.path.getsize(lp)
    open(lp, "a", encoding="utf-8").write("precheck.available() = False" + NL)
    chk("launch_state: new False => failed", W.launch_state(lp, off2) == "failed")
    import kardsmem.version as V
    rd = lambda v, hits=2: (lambda pid: {"version": v, "hits": {v: hits}, "why": "mem", "seconds": 0})
    for ver, want in (("1.60.27292.launcher", "launcher_default"), ("1.60.27292.Steam", "current"),
                      ("1.58.27125.launcher", "launcher_default"), ("1.57.26586.launcher", "launcher_157_orig")):
        i = W.detect_version(lambda: [5], rd(ver))
        chk("detect_version: %s => RVA table %s (memory version only, no exe size)" % (ver, want), i["build"] == want and i["version"] == ver, str(i))
    i = W.detect_version(lambda: [5], rd("1.99.1.launcher"))
    chk("detect_version: unregistered version => 不拦（version 照出，build None，source 预期 scan/cache）",
        i["build"] is None and i["version"] == "1.99.1.launcher" and i["source"] in ("scan", "cache") and "种子表没有" in i["why"], str(i))
    i = W.detect_version(lambda: [5], lambda pid: {"version": None, "why": "none found"})
    chk("detect_version: not found in memory => None", i["build"] is None and i["version"] is None)
    chk("detect_version: no process / two processes", W.detect_version(lambda: [], rd("x"))["build"] is None
        and "多个" in W.detect_version(lambda: [1, 2], rd("x"))["why"])
    # kardsmem.version：缓存键 = (pid, 创建时间)；选择优先级 env > 版本 > default
    V.CACHE = os.path.join(os.environ["KARDS_GUI_DIR"], "vc.json")
    calls = []
    rf = lambda pid: calls.append(pid) or {"version": "1.60.27292.launcher"}
    v1 = V.running_version(True, lambda: [9], rf, lambda p: 111)
    v2 = V.running_version(True, lambda: [9], rf, lambda p: 111)
    v3 = V.running_version(True, lambda: [9], rf, lambda p: 222)
    chk("running_version: same (pid,start) read once; restart (new start time) rescans", v1 == v2 == v3 == "1.60.27292.launcher" and len(calls) == 2, str(calls))
    chk("running_version: 0 or 2 processes => None", V.running_version(False, lambda: [], rf) is None and V.running_version(False, lambda: [1, 2], rf) is None)
    import kardsmem.build as KB
    chk("P7: 版本登记制已删（VERSION_TO_BUILD/select_build 不在 version.py）", not hasattr(V, "VERSION_TO_BUILD") and not hasattr(V, "select_build"))
    chk("P7: 种子表每个 versions 条目都能反查回自己的键", all(KB.seed_for_display(v) == k for k, b_ in KB.BUILDS.items() for v in b_["versions"]))
    print("失败 %d 项" % fails)
    return fails


DEF = C.DEFAULT_CONTROL

if __name__ == "__main__":
    sys.exit(1 if main() else 0)
