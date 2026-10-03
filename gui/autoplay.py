# -*- coding: utf-8 -*-
"""“打一局 → 自动开下一局”编排。**只在常驻监听器 `live_session.py` 里跑**（`exec`/调用）。

面板不直接碰游戏：面板写 `control.json`，监听器里的 `step()` 读它、干活、把状态写进 `status.json`。
启动：往 `kards-data/live/live_cmd.txt`（`agent/paths.py`）追加一行 `LAUNCH`（面板的“开始”按钮就是这么做的）。
每局结束后 `step()` 自己再把 `LAUNCH` 追加进去，直到：局数打满 / 关了 auto_next / 关了 run / 立即停 / 开局失败。

安全阀（沿用 `continue_ranked.py` 的口径）：
  * 对局进行中 **绝不** 点“开始”（硬闸门 `play_guard.safe_to_start`）；
  * 结算页/过渡态（牌组按钮=0）不点开始，只等；
  * 没能离开结算页 / `press_play` 没成功 / `player.play.play` 没打出（done 不为真）⇒ **不排队下一局**；
  * `press_play` 回 ok=True **不等于开局成功**（2026-10-03 实机：对战/休闲下大厅因“卡组含未拥有卡”
    拒入厅，游戏只弹一条 1~2 s 的浮动通知，`press_play` 照样回 ok=True）⇒ 点击后必须过
    `verify_start()` **只读后置验证**，没进对局就带着游戏原话停止，不排下一局；
  * `abort_now` 在每一步开始前检查（`player.play.play(should_stop=…)`）。
GUI 不做任何真鼠标/前台操作；点开始走的是 `precheck.call_write("press_play")`（注入）。
"""
from __future__ import annotations

import os
import sys
import time

from . import control as C

from base import paths as _paths

LIVE_CMD = _paths.LIVE_CMD
LAUNCH = ('import importlib, gui.autoplay as _AP; importlib.reload(_AP); '
          '_AP.step(sess, precheck)')


def enqueue(cmd_path: str = LIVE_CMD) -> None:
    with open(cmd_path, "a", encoding="utf-8") as f:
        f.write(LAUNCH + "\n")


def _guard():
    from player import play_guard
    return play_guard


def _deck_ready(precheck) -> bool:
    try:
        sc = precheck.call_read("deck_screen_scan") or {}
        return len(sc.get("W_MatchDeckSelectionDeckButton_C") or []) > 0
    except Exception:                                    # noqa: BLE001
        return False


def leave_result_page(precheck, tries: int = 20, should_abort=lambda: False) -> dict:
    """结算页点“继续”直到回到牌组页；结算页不在 ⇒ 看牌组页是否就绪（没就绪=过渡态，只等）。"""
    steps, left = [], False
    for _ in range(tries):
        if should_abort():
            break
        r = precheck.call_write("end_of_match_continue", verbose=False) or {}
        if not r.get("ok") and "没有 W_EndOfMatch_C" in str(r.get("error") or ""):
            if _deck_ready(precheck):
                steps.append("deck_ready")
                left = True
                break
            steps.append("transition")
            precheck.call_write("settle", 1.5, 6)
            continue
        steps.append("continue:%s" % r.get("step_after"))
        if r.get("left_screen"):
            left = True
            break
        precheck.call_write("settle", 1.5, 6)
    precheck.call_write("settle", 3.0, 6)
    return {"left": left, "deck_ready": _deck_ready(precheck), "steps": steps}


def _last_action_sig(sess):
    """动作流最后一条的签名；读不到 ⇒ None。用来判断动作流还在不在长。"""
    try:
        h = sess.history(tail=1) or {}
        rows = h.get("rows") or []
        return (len(rows), str(rows[-1])[:120]) if h.get("ok") and rows else None
    except Exception:                                    # noqa: BLE001
        return None


def _last_is_end_match(sess) -> bool:
    try:
        rows = (sess.history(tail=1) or {}).get("rows") or []
        return bool(rows) and isinstance(rows[-1], dict) and str(rows[-1].get("action_type")) == "ActionEndMatch"
    except Exception:                                    # noqa: BLE001
        return False


def classify_in_match(sess, precheck, gap_s: float = 4.0, sleep=time.sleep) -> str:
    """`play_guard.in_match` 读到“在对局里”时，分清**真在打**还是**上一局的残留读数**：

    * `live`    —— 动作流还在长，或者一动不动但牌组页没就绪（牌组按钮=0；对手回合/过渡）：真的有一局在进行 ⇒ 接手；
    * `stale`   —— 牌组页已就绪、动作流读得到但隔 `gap_s` 秒一动不动：上一局读数还没清（实机反复遇到：刚打完一局的头几分钟，
                   `match_active`/动作流/盘面都还读得到旧值，闸门因此一直拒绝开局，而原先的“直接接手”会把 `player.play.play`
                   跑在一个不存在的对局上）；
    * `unknown` —— 动作流读不出（保守按真在打，不动）。
    """
    sig1 = _last_action_sig(sess)
    ready = _deck_ready(precheck)
    sleep(gap_s)
    sig2 = _last_action_sig(sess)
    if sig1 is None or sig2 is None:
        return "unknown"                                 # 动作流读不出：保守按“真在打”（旧行为），不替它判残留
    if sig1 != sig2:
        return "live"
    # 动作流一动不动：只有**末条是 ActionEndMatch**（上一局确定结束）且牌组页已就绪才算残留。
    # ★ 2026-10-03 实机：新一局刚开、换牌还没做完时动作流也停在 XStartOfGame/回合开始，牌组页控件还读得到旧值，
    #   旧判据把这个真对局当成残留，等 90 s 后停手（那局换牌因此超时）。
    return "stale" if (ready and _last_is_end_match(sess)) else "live"


def wait_residual_clear(sess, precheck, timeout_s: float = 90.0, poll_s: float = 5.0,
                        should_abort=lambda: False, sleep=time.sleep, now=time.time) -> dict:
    """等“上一局残留读数”清掉（`in_match` 变 False）。→ `{"cleared", "waited_s", "last"}`。超时不强推。"""
    t0, last = now(), None
    while now() - t0 < timeout_s:
        if should_abort():
            break
        last = _guard().in_match(sess)
        if last is False:
            return {"cleared": True, "waited_s": round(now() - t0, 1), "last": last}
        sleep(poll_s)
    return {"cleared": False, "waited_s": round(now() - t0, 1), "last": last}


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def verify_start(sess, precheck, timeout_s: float = 60.0, poll_s: float = 1.5,
                 notify_window_s: float = 10.0, sleep=None, now=None) -> dict:
    """点完“开始”后的**只读后置验证**：真的进了对局才算开局成功。

    ★ 为什么必须有（2026-10-03 实机，`_nn_scratch/_dbg_press.json`）：对战/休闲模式下
      `press_play` 的客户端按钮校验只是 UX 级；大厅/服务端会按“卡组含未拥有卡”拒入厅，
      游戏只弹一条 **1~2 s 的浮动通知**（`ENTERING LOBBY FAILED BECAUSE OF DECK ERROR` /
      中文渲染“卡组错误，进入大厅失败”），而 `press_play` 依然返回 ok=True。
      没有这一步，编排会把“点了”当“开局了”继续 `player.play.play`、排队下一局，表现成“点了没反应”。
    ★ 只读：判据是 `play_guard.in_match`（True ⇒ 成功）；全程不写游戏。
    ★ 通知短命 ⇒ 只在开局后头 `notify_window_s` 秒里轮询 `notify_texts`，把游戏自己的
      理由捎回来（`call_read` 在不健康/拒绝时回 dict，这里只认 list，别把 dict 的键当提示）。

    → `{"ok", "waited_s", "last", "notify", "timeout_s"}`
    """
    sleep = _sleep if sleep is None else sleep
    now = _now if now is None else now
    t0, notify, last = now(), [], None
    while True:
        try:
            last = _guard().in_match(sess)
        except Exception:                                    # noqa: BLE001
            last = None
        if last is True:
            return {"ok": True, "waited_s": round(now() - t0, 1), "last": last,
                    "notify": notify, "timeout_s": timeout_s}
        if now() - t0 <= notify_window_s:
            try:
                seen = precheck.call_read("notify_texts")
            except Exception:                                # noqa: BLE001
                seen = []
            if isinstance(seen, list):
                for t in seen:
                    t = str(t).strip()
                    if t and t not in notify:
                        notify.append(t)
        if now() - t0 >= timeout_s:
            return {"ok": False, "waited_s": round(now() - t0, 1), "last": last,
                    "notify": notify, "timeout_s": timeout_s}
        sleep(poll_s)


def start_next(sess, precheck) -> dict:
    """回牌组页 → 过闸门 → 点开始 → **后置验证真进了对局**。→ `{"ok", "why", "verify"}`。"""
    lv = leave_result_page(precheck, should_abort=lambda: C.load_control().get("abort_now"))
    if not (lv["left"] and lv["deck_ready"]):
        return {"ok": False, "why": "没回到牌组页（结算页/过渡态）", "leave": lv}
    ok, im, why = _guard().safe_to_start(sess)
    if not ok:
        return {"ok": False, "why": why, "leave": lv}
    r = precheck.call_write("press_play", verbose=True) or {}
    if not r.get("ok"):
        return {"ok": False, "why": r.get("error") or "press_play 失败", "leave": lv, "press": r}
    v = verify_start(sess, precheck)
    if not v["ok"]:
        why = "点了开始但 %.0f s 内没进对局" % v["timeout_s"]
        if v.get("notify"):
            why += "：" + "；".join(v["notify"])
        return {"ok": False, "why": why, "leave": lv, "press": r, "verify": v}
    return {"ok": True, "why": "", "leave": lv, "press": r, "verify": v}


def step(sess, precheck, play=None, enqueue_fn=enqueue) -> dict:
    """打一局（必要时先开局），然后按控制项决定要不要排下一局。可注入 `play`/`enqueue_fn` 做离线测试。"""
    ctrl = C.load_control()
    st = C.load_status()
    if st.get("busy"):
        C.log_event("已有一局在编排中，忽略重复启动")
        return {"skipped": "busy"}
    if not ctrl["run"]:
        C.update_status(state="idle", busy=False, note="run=False，不开新局")
        return {"skipped": "run=False"}
    if not ctrl["unlimited"] and ctrl["games_done"] >= int(ctrl["games_total"]):
        C.update_control(run=False)
        C.update_status(state="finished", busy=False, note="计划局数已打满")
        C.log_event("计划局数已打满（%d）" % ctrl["games_done"])
        return {"skipped": "finished"}

    C.update_status(busy=True, state="preparing", note="检查是否已在对局")
    result = {"played": False}
    try:
        gp = _guard()
        im = gp.in_match(sess)
        if im is True:
            kind = classify_in_match(sess, precheck)
            if kind == "stale":
                C.update_status(state="preparing", note="上一局的读数还没清，等它清掉（最多 90 s）")
                C.log_event("对局读数是上一局的残留（牌组页已就绪、动作流不动）⇒ 等待清掉")
                w = wait_residual_clear(sess, precheck, should_abort=lambda: C.load_control().get("abort_now"))
                C.log_event("等待残留：%s" % w)
                if not w["cleared"]:
                    C.update_control(run=False)
                    C.update_status(state="stopped", note="上一局残留读数 %.0f s 没清掉；请在游戏里手动点开始，脚本会接手" % w["waited_s"])
                    return {"played": False, "why": "stale_residual", "wait": w}
                im = False
            else:
                C.log_event("检测到对局进行中（%s）⇒ 直接接手（不点开始）" % kind)
        if im is True:
            pass
        elif im is False and ctrl.get("dry_run"):
            C.update_control(run=False)
            C.update_status(state="stopped", note="“只看不动”只接手进行中的对局，不开新局；请先开一局")
            C.log_event("只看不动：当前没有对局 ⇒ 停")
            return {"played": False, "why": "dry_run_no_match"}
        elif im is False:
            C.update_status(state="starting", note="回牌组页并点开始")
            sn = start_next(sess, precheck)
            C.log_event("开局：ok=%s %s" % (sn["ok"], sn["why"]))
            if not sn["ok"]:
                C.update_control(run=False)
                C.update_status(state="stopped", note="开局失败：%s" % sn["why"])
                return {"played": False, "start": sn}
            time.sleep(3)
        else:
            C.update_control(run=False)
            C.update_status(state="stopped", note="判不出是否在对局（保守起见不动）")
            C.log_event("判不出是否在对局 ⇒ 停")
            return {"played": False, "why": "in_match=None"}

        C.update_status(state="playing", note="第 %d 局" % (ctrl["games_done"] + 1))
        if play is None:
            from player import play as _P
            play = _P.play
        c2 = C.load_control()
        t0 = time.time()
        try:
            res = play(sess, live=not c2.get("dry_run"), turns=int(c2["turns"]), max_actions=int(c2["max_actions"]),
                       verbose=False, avoid=tuple(c2["avoid"] or ()), forbid=tuple(c2.get("forbid") or ()),
                       should_stop=lambda: bool(C.load_control().get("abort_now")))
        except Exception as exc:                          # noqa: BLE001
            res = {"exception": "%s: %s" % (type(exc).__name__, exc)}
        played = bool(res.get("done")) and not res.get("exception")
        result = {"played": played, "seconds": round(time.time() - t0, 1),
                  "n": res.get("n"), "exception": res.get("exception")}
    finally:
        c3 = C.load_control()
        aborted = bool(c3.get("abort_now"))
        done = c3["games_done"] + (1 if (result.get("played") and not aborted) else 0)
        again = (result.get("played") and c3["run"] and c3["auto_next"] and not aborted
                 and (c3["unlimited"] or done < int(c3["games_total"])))
        upd = {"games_done": done}
        if aborted:
            upd["abort_now"] = False
            upd["run"] = False
        elif not again:
            upd["run"] = False                            # 没打出/打满/关了自动继续 ⇒ 不再开新局
        C.update_control(**upd)
        if C.load_status().get("state") == "stopped" and not aborted and not result.get("played"):
            # 提前退出时上面已经写了“已停止 + 原因”（开局失败 / 残留读数清不掉 / 判不出是否在对局）：别用泛泛的“不再排队”盖掉
            C.update_status(busy=False, games_done=done, last_result=result)
        else:
            C.update_status(busy=False, games_done=done, last_result=result,
                            state=("waiting_next" if again else ("stopped" if aborted else "idle")),
                            note=("已排队下一局" if again else "不再排队"))
        C.log_event("本局结束：%s | 已打 %d 局 | 排队下一局=%s" % (result, done, bool(again)))
        if again:
            enqueue_fn()
    return result
