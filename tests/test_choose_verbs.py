#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_choose_verbs.py —— **离线**回归：`choose_one_with_target` 的两步状态机。

为什么需要它（2026-09-27，用户）："你那次 pick 进入了 state after choose。
说明 with_select 没成功。是 bug。" —— 实机那次我是**手动分两步**点（`choose 1` + `aimunit 16 27`）
才走通的，而**合并动词**旧写法在 `pick_choice.ok == False` 时提前 return ⇒ 对这类卡永远走不到
第二步。HIDDEN PLANS 这种卡不是手上总有，所以把状态机钉成**纯离线**回归测试：
用假 `self` 直接调 `ops_inject.Injector.choose_one_with_target`（它只调
`pick_choice` / `card_being_played_from_hand` / `select_unit_target` 三个方法）。

跑法：`python test_choose_verbs.py`（`kards-agent/` 下）
"""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys

from ops import inject as OI
from agent.session import AgentSession

FAILS = 0


def chk(name, got, want):
    global FAILS
    ok = (got == want)
    if not ok:
        FAILS += 1
    print("  [%s] %-64s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


class FakeSelf:
    """假 self：实现 `choose_one_with_target` 依赖的三个方法，按脚本回放。"""

    def __init__(self, pick=None, playing=0, sel=None):
        self._pick, self._playing, self._sel = pick, playing, sel
        self.calls: list = []

    def pick_choice(self, index, trigger=None, kind=None, verbose=True):
        self.calls.append(("pick_choice", index, trigger, kind))
        return dict(self._pick)

    def card_being_played_from_hand(self):
        self.calls.append(("card_being_played_from_hand",))
        return self._playing

    def select_unit_target(self, card_id, target_id, verbose=True):
        self.calls.append(("select_unit_target", card_id, target_id))
        return dict(self._sel)


def run(fs: FakeSelf, index=1, target=27):
    return OI.Injector.choose_one_with_target(fs, index, target, verbose=False)


R_OK = {"ok": True, "actions": [{"action_type": "XActionPlayCardFromHand"}]}
# ★ 实机形状：动作流**空**，但游戏进了"待点目标"态（`pick_choice` 现在会带这两个旁证）
R_AWAIT = {"ok": False, "criterion": "action_stream", "outcome": "awaiting_target",
           "awaiting_target": True, "playing_from_hand": 16, "actions": []}
R_DEAD = {"ok": False, "criterion": "action_stream", "outcome": "resolved",
          "awaiting_target": False, "playing_from_hand": None, "actions": []}

print("== choose_one_with_target 两步状态机（假 self，不碰游戏）==")

# ① ★ 核心回归：选项那步动作流为空（ok=False）但游戏进了待点目标态 ⇒ **必须继续点目标**
fs = FakeSelf(R_AWAIT, playing=16, sel=R_OK)
r = run(fs)
chk("① 选项动作流为空、已进待点目标 ⇒ 继续第二步", r.get("ok"), True)
chk("① 确实点了目标（16 → 27）",
    [c for c in fs.calls if c[0] == "select_unit_target"], [("select_unit_target", 16, 27)])
chk("① 选项那步的 ok=False 如实留在 choice 里", r["choice"].get("ok"), False)
chk("① 带了一句解释（option 步无回执是正常的）", "note" in r, True)

# ② 选项那步**没有** awaiting_target 旁证（别的读法/环境）：仍按游戏状态继续 —— 闸门是 playing
fs = FakeSelf({"ok": False, "actions": []}, playing=16, sel=R_OK)
r = run(fs)
chk("② awaiting_target 读不到时也照样继续（闸门 = playing）", r.get("ok"), True)
chk("② 点了目标", len([c for c in fs.calls if c[0] == "select_unit_target"]), 1)

# ③ 选项自己就完事了（不需要目标的选项）：有回执、没进待点目标 ⇒ 成功且**不点目标**
fs = FakeSelf(R_OK, playing=0, sel=R_OK)
r = run(fs, index=0)
chk("③ 选项自带回执且不需要目标 ⇒ ok=True", r.get("ok"), True)
chk("③ 标了 resolved_at_option", bool(r.get("resolved_at_option")), True)
chk("③ 没有点目标", len([c for c in fs.calls if c[0] == "select_unit_target"]), 0)

# ④ 真没点上：动作流空 + 没进待点目标 ⇒ 失败，且**不点目标**（不拿旁证当成功）
fs = FakeSelf(R_DEAD, playing=0, sel=R_OK)
r = run(fs)
chk("④ 没点上 ⇒ ok=False", r.get("ok"), False)
chk("④ reason=option_not_consumed", r.get("reason"), "option_not_consumed")
chk("④ 没有点目标（别乱点）", len([c for c in fs.calls if c[0] == "select_unit_target"]), 0)

# ⑤ 第二步没回执 ⇒ ok 跟着失败（判据是第二步的动作流），但**确实尝试过**
fs = FakeSelf(R_AWAIT, playing=16, sel={"ok": False, "actions": [], "consumed": False})
r = run(fs)
chk("⑤ 第二步没回执 ⇒ ok=False", r.get("ok"), False)
chk("⑤ 但确实尝试了点目标", len([c for c in fs.calls if c[0] == "select_unit_target"]), 1)
chk("⑤ 判据标注为第二步的动作流", r.get("criterion"), "action_stream(第二步)")

# ⑥ index / kind 原样透传（第二个选项 = index 1）
fs = FakeSelf(R_AWAIT, playing=16, sel=R_OK)
run(fs, index=1)
chk("⑥ pick_choice 收到 index=1 + kind=choose_one",
    [c for c in fs.calls if c[0] == "pick_choice"][0], ("pick_choice", 1, None, "choose_one"))

# ⑦ 会话层只是转发（动词在注入侧，只有一个实现）
seen = {}
s = AgentSession(translate=False, warm=False, game_gate=True)
s._inj = lambda fn, *a, **kw: (seen.update(fn=fn, args=a, kw=kw) or {"ok": True})   # type: ignore
r = s.choose_one_with_target(1, 27)
chk("⑦ AgentSession 转发到注入侧的 choose_one_with_target",
    (seen.get("fn"), seen.get("args")), ("choose_one_with_target", (1, 27)))

# ⑧ 认不出目标 ⇒ 一个动作都不发
seen2 = {}
s2 = AgentSession(translate=False, warm=False, game_gate=True)
s2._inj = lambda fn, *a, **kw: (seen2.update(fn=fn) or {"ok": True})               # type: ignore
r = s2.choose_one_with_target(1, None)
chk("⑧ target 认不出 ⇒ ok=False 且不发动作", (r.get("ok"), seen2.get("fn")), (False, None))

print("\n结论：%s（%d 项失败）" % ("PASS" if FAILS == 0 else "FAIL", FAILS))
sys.exit(1 if FAILS else 0)
