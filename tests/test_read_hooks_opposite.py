#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`make_read_hooks` 的 `GetOppositeSide`：座位优先查调用方喂的 `field_overrides`（离线）。

背景（2026-10-03 实机，RAPID RESPONSE 的评估缺口）：评估对象常是静态卡（side=0），
调用方已经把「这张牌是我方的」用 `field_overrides` 喂给了 `enumerate_effects`，
但读 hook 只查快照 ⇒ 能算的效果断在 `GetOppositeSide`。这里钉住三条：
  ① 覆盖表里有 ⇒ 用它（1→2 / 2→1）；
  ② 覆盖表没有、快照里有 ⇒ 用快照；
  ③ 两处都没有 ⇒ 如实抛（不编默认）。

★ 2026-10-04 追加（实机诊断 + 只读反汇编定案）：**被问的卡是 receiver，不是 kid** ——
`card_event_storm2_thunderstorm` 的 ubergraph 里是
`FinalFunction(fn=GetOppositeSide)` + 唯一 kid = 出参 `LocalVariable(CallFunc_GetOppositeSide_oppositeSide)`
（初值 None），而 BP 原文是 `this->GetOppositeSide()`（`card_event_storm2_thunderstorm.cpp:28`）。
旧实现只看 `args[0]`（那个出参）⇒ 恒 None ⇒ 天气族 7 变体 + IJN SHINANO 全记**假缺口**。
下面 ④–⑦ 钉住这条：receiver 有就用 receiver（含 `f.self_obj` 回退），没有才退回 `args[0]`（RAPID RESPONSE 那种显式入参形状）。
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import semantics.effectvm as EV                                      # noqa: E402

fails = 0
PTR = 0x1D2ABCDEF0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def call(hooks, args):
    """直接调读 hook：`_generic_out` 对拿不到 frame/kid 是容忍的（只写不了出参）。"""
    return hooks["GetOppositeSide"](None, None, None, args, None)


def call_real(hooks, recv, args, self_obj=None):
    """**真实字节码形状**：receiver 单独传，kids 只有出参（这里用 None 代表未初始化的出参）。"""
    frame = SimpleNamespace(self_obj=self_obj)
    return hooks["GetOppositeSide"](None, frame, recv, args, None)


def main():
    st = SimpleNamespace(cards=[])
    h = EV.make_read_hooks(st, 1, field_overrides={(PTR, "side"): 1})
    chk("覆盖表 side=1 ⇒ 2", call(h, [PTR]) == 2)
    h = EV.make_read_hooks(st, 2, field_overrides={(PTR, "side"): 2})
    chk("覆盖表 side=2 ⇒ 1", call(h, [PTR]) == 1)
    st2 = SimpleNamespace(cards=[SimpleNamespace(raw={"ptr": PTR, "side_enum": 2})])
    h = EV.make_read_hooks(st2, 1)
    chk("快照里查得到（side_enum=2）⇒ 1", call(h, [PTR]) == 1)
    h = EV.make_read_hooks(st, 1)
    raised = False
    try:
        call(h, [PTR])
    except EV.Unimplemented:
        raised = True
    chk("两处都没有 ⇒ 如实抛（不编默认）", raised)

    # ④–⑦ 真实形状：receiver = 那张卡，kids = [出参=None]
    h1 = EV.make_read_hooks(st, 1, field_overrides={(PTR, "side"): 1})
    h2 = EV.make_read_hooks(st, 2, field_overrides={(PTR, "side"): 2})
    chk("真实形状：receiver 拿来当卡（side=1）⇒ 2", call_real(h1, PTR, [None]) == 2)
    # ⑤ 两个不同输入必须给出不同答案（弯路 #11：别让"永远同一个值"的坏实现也能过）
    chk("真实形状：side=2 ⇒ 1（与上一条不同 ⇒ 真的读了座位）", call_real(h2, PTR, [None]) == 1)
    # ⑥ hook 的 obj 为 None 时回退 frame.self_obj（`vm.py:571` 只在 native 那条路回退）
    chk("真实形状：obj=None ⇒ 回退 frame.self_obj", call_real(h1, None, [None], self_obj=PTR) == 2)
    # ⑦ 两者都拿不到 ⇒ 仍如实抛（不静默按我方算）
    raised2 = False
    try:
        call_real(EV.make_read_hooks(st, 1), None, [None], self_obj=None)
    except EV.Unimplemented:
        raised2 = True
    chk("真实形状：receiver 与 args 都拿不到 ⇒ 如实抛", raised2)
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

