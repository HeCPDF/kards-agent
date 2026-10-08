#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`gui.autoplay` 开局前的两道保障（假 precheck / 假闸门，离线）：

* `leave_result_page`：不在结算页、牌组页也没就绪 ⇒ 先点一次左侧『开始』（注入式），再按牌组页是否就绪继续；
* `start_next`：模式不是训练（`selected_mode` 的 selected/chosen 不都是 1）⇒ 先 `select_mode_by_label('TRAINING')`，
  切失败不点开始；已是训练不动模式。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gui import autoplay as AP                                       # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


class FakePre:
    def __init__(self, deck_after_sidebar=True, mode=None, mode_ok=True):
        self.log = []
        self.sidebar_pressed = False
        self.deck_after_sidebar = deck_after_sidebar
        self.mode = mode if mode is not None else {"selected": 1, "chosen": 1}
        self.mode_ok = mode_ok

    def call_read(self, fn, *a, **k):
        self.log.append(("r", fn))
        if fn == "deck_screen_scan":
            ready = getattr(self, 'always_ready', False) or (self.sidebar_pressed and self.deck_after_sidebar)
            return {"W_MatchDeckSelectionDeckButton_C": [1] if ready else []}
        if fn == "selected_mode":
            return dict(self.mode)
        return {}

    def call_write(self, fn, *a, **k):
        self.log.append(("w", fn) + tuple(a))
        if fn == "end_of_match_continue":
            if getattr(self, "stale_end", False):          # 陈旧结算页实例：点得到却没进展
                return {"ok": True, "step_after": None}
            return {"ok": False, "error": "没有 W_EndOfMatch_C"}
        if fn == "press_sidebar":
            self.sidebar_pressed = True
            return {"ok": True}
        if fn == "select_mode_by_label":
            if self.mode_ok:
                self.mode = {"selected": 1, "chosen": 1}
            return {"ok": self.mode_ok, "error": None if self.mode_ok else "点了模式但没变"}
        if fn == "press_play":
            return {"ok": True}
        return {"ok": True}


class FakeGuard:
    @staticmethod
    def safe_to_start(sess):
        return True, False, ""


def main():
    AP.C.log_event = lambda *a, **k: None      # 不往真实的 gui/events.log 里写测试噪声
    # ① 不在结算页、牌组页没就绪 ⇒ 点侧栏『开始』，然后就绪
    pre = FakePre()
    r = AP.leave_result_page(pre, tries=6)
    chk("侧栏『开始』被点了一次", [x for x in pre.log if x[:2] == ("w", "press_sidebar")] == [("w", "press_sidebar", "play")],
        str(pre.log))
    chk("点完后牌组页就绪 ⇒ left & deck_ready", r["left"] and r["deck_ready"], str(r))
    # ② 点了侧栏仍没就绪 ⇒ 只点一次，不死循环连点
    pre = FakePre(deck_after_sidebar=False)
    AP.leave_result_page(pre, tries=4)
    n = len([x for x in pre.log if x[:2] == ("w", "press_sidebar")])
    chk("牌组页一直没就绪也只点一次侧栏（不连点）", n == 1, "n=%d" % n)

    # ②b 弹窗浮层：牌组页控件一直读得到（always_ready）⇒ 仍要先点一次侧栏关弹窗
    pre = FakePre()
    pre.always_ready = True
    r = AP.leave_result_page(pre, tries=4)
    chk("牌组页控件已就绪（弹窗浮层）也先点一次侧栏", [x for x in pre.log if x[:2] == ("w", "press_sidebar")] != [] and r["left"], str(r))

    # ②c 陈旧结算页实例：连续 continue:None ⇒ 3 次后点侧栏、牌组页就绪即离开（不再空转 20 次失败）
    pre = FakePre()
    pre.stale_end = True
    r = AP.leave_result_page(pre, tries=20)
    nct = len([x for x in pre.log if x[:2] == ("w", "end_of_match_continue")])
    chk("陈旧结算页实例：3 次无进展后离开", r["left"] and r["deck_ready"] and nct <= 4, "n=%d %s" % (nct, r))

    # ③ start_next：已是训练 ⇒ 不动模式
    pre = FakePre()
    pre.sidebar_pressed = True
    old_g, old_v = AP._guard, AP.verify_start
    AP._guard = lambda: FakeGuard
    AP.verify_start = lambda sess, precheck, **k: {"ok": True, "timeout_s": 0, "notify": []}
    try:
        res = AP.start_next(None, pre)
        chk("已是训练：不调 select_mode_by_label，直接点开始", res["ok"] and not any(
            x[:2] == ("w", "select_mode_by_label") for x in pre.log) and any(x[:2] == ("w", "press_play") for x in pre.log),
            str(pre.log))
        # ④ 不是训练 ⇒ 先切再点开始（顺序）
        pre = FakePre(mode={"selected": 1, "chosen": 0})
        pre.sidebar_pressed = True
        res = AP.start_next(None, pre)
        order = [x[1] for x in pre.log if x[0] == "w"]
        chk("非训练：先 select_mode_by_label 再 press_play",
            res["ok"] and order.index("select_mode_by_label") < order.index("press_play"), str(order))
        # ⑤ 切不动 ⇒ 不点开始
        pre = FakePre(mode={"selected": 2, "chosen": 2}, mode_ok=False)
        pre.sidebar_pressed = True
        res = AP.start_next(None, pre)
        chk("切训练失败：返回 ok=False 且没点开始", (not res["ok"]) and "切训练" in res["why"]
            and not any(x[:2] == ("w", "press_play") for x in pre.log), str(res))
    finally:
        AP._guard, AP.verify_start = old_g, old_v
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
