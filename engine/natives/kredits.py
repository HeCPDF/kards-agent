# -*- coding: utf-8 -*-
"""engine.natives.kredits —— 指挥点/槽一族：夹取与变更。

出处：原版：BP_CardFunctions::ChangeKreditsBySide（及槽位同族叶子）写 GameState 后，BP 层统一把
      指挥点/槽夹到 `[0, getMaxPossibleKredits()]`（EVAL-NATIVES §8-1；`getMaxPossibleKredits`
      读 `GameState+0x338`，本模型里是 `state.kredit_max`，默认 24）。
      2026-10-02 以 `policy/boardeval.py` 自检钉住：23+3→24、max=10→10、−30→0、槽 20+10→24。

只认鸭子类型（`state.kredits` / `state.slots` / `state.kredit_max`），不 import `sim`。
"""
from __future__ import annotations


def clamp_kredits(v, lo: float, hi: float) -> float:
    """夹取到 `[lo, hi]`（BP 写侧的统一收口）。"""
    return max(lo, min(hi, float(v)))


def change_kredits(state, delta) -> None:
    """`ChangeKreditsBySide(side, delta)`（我方）+ 夹取。支出**不**走这里（它是"扣多少"，由动作自付）。"""
    state.kredits = clamp_kredits(state.kredits + delta, 0.0, state.kredit_max)


def change_slots(state, delta) -> None:
    """槽位变更 + 夹取（本模型与指挥点共用同一个上限 `kredit_max`）。"""
    state.slots = clamp_kredits(state.slots + delta, 0.0, state.kredit_max)

