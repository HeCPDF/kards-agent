#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P5 接缝测试：`policy.search.wire_sim()` 是**唯一**该做 wiring 的地方（幂等）。

为什么要这条（P5"删门面"分步方案 ① 先搬 wiring）：
    原来 wiring 写在门面 `policy/boardeval.py` 里 —— 门面一被 import 就顺手把
    `sim.engine.WEIGHTS` 换成评估侧的 `W`、并把 `set_unit_valuer` 接到 `unit_value` 上。
    门面一旦删掉，这两件事就**没人做**了（`sim` 的选目标启发式会 `RuntimeError` ✗）。
    ⇒ 收成 `policy.search.wire_sim()`（L4 是唯一同时认识 `sim.engine` 与 `evaluation.value` 的层 ✓），
      门面只转调；将来新入口调同一个函数即可 ✓。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    import sim.engine as EN

    # ① 没 wiring 时：估值参数有**中性默认**（能跑），但选目标启发式**没接**（会明确报错，不是悄悄给 0）
    EN.set_unit_valuer(None)
    chk("未 wiring：`WEIGHTS` 仍有中性默认（`draw_v` 在，规则常量不在这里 ✓）",
        "draw_v" in EN.WEIGHTS and "hand_cap" not in EN.WEIGHTS, str(EN.WEIGHTS))
    try:
        EN._unit_value(object())
        chk("未 wiring：`_unit_value` 应明确报错（不许悄悄给 0）", False, "居然没报错")
    except RuntimeError as ex:
        chk("未 wiring：`_unit_value` 明确报错（不静默）", "set_unit_valuer" in str(ex), str(ex)[:60])

    # ② wire_sim() 之后：两件事都接上
    import policy.search as SE
    SE.wire_sim()
    chk("`wire_sim()` ⇒ 估值参数来自评估侧 `W`、选目标启发式已绑定",
        EN.WEIGHTS.get("draw_v") == SE.W["draw_v"] and EN._unit_valuer is SE.unit_value,
        "draw_v=%s valuer=%s" % (EN.WEIGHTS.get("draw_v"), EN._unit_valuer is not None))

    # ③ 幂等（重复调不出错、结果不变）
    before = (dict(EN.WEIGHTS), EN._unit_valuer)
    SE.wire_sim()
    chk("`wire_sim()` 幂等（重复调结果不变）",
        (dict(EN.WEIGHTS), EN._unit_valuer) == before, str(EN.WEIGHTS))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
