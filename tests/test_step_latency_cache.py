#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策侧时延：跨步缓存（`_actionable` / 合法目标配对）命中与失效、影子对账默认关、`step_budget_s` 预算。（假卡/假会话，不碰游戏）"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _hookfake import ME, OPP, Checker, card                       # noqa: E402
import player.rule as R                                            # noqa: E402

chk = Checker()


class St:
    def __init__(self, cards, kred=5, turn=5):
        self.cards, self.my_side, self.other_side, self.turn = cards, ME, OPP, turn
        self.kredits = {ME: kred, OPP: 3}


def mk_rule():
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.P = dict(R.PARAMS)
    pol._act_cache, pol._epoch, pol.calls = {}, 0, 0

    def act(u):
        pol.calls += 1
        return True
    pol.act_fn = act
    return pol


def main():
    chk("shadow 默认关", R.PARAMS.get("shadow") is False)
    chk("step_budget_s 在 PARAMS", float(R.PARAMS.get("step_budget_s")) > 0)

    a, b = card(1, "A", "frontline", ME), card(2, "B", "frontline", ME)
    st = St([a, b])
    pol = mk_rule()
    pol._actionable(st, a)
    pol._actionable(st, b)
    chk("首次各问一次", pol.calls == 2)
    # 下一步（epoch +1，步内缓存清空），局面没变 ⇒ 命中跨步缓存
    pol._epoch += 1
    pol._act_cache.clear()
    pol._actionable(st, a)
    pol._actionable(st, b)
    chk("局面没变 ⇒ 不重问", pol.calls == 2)
    # 单位自身指纹变（已攻击）⇒ 重问；另一个仍命中
    pol._epoch += 1
    pol._act_cache.clear()
    a.obj.attackLeft = 0
    pol._actionable(st, a)
    pol._actionable(st, b)
    chk("指纹变 ⇒ 只重问该单位", pol.calls == 3)
    # 盘面签名变（多一张牌）⇒ 全部重问
    pol._epoch += 1
    pol._act_cache.clear()
    st2 = St([a, b, card(3, "C", "frontline", ME)])
    pol._actionable(st2, a)
    pol._actionable(st2, b)
    chk("盘面签名变 ⇒ 全重问", pol.calls == 5)
    # 指挥点变 ⇒ 重问
    pol._epoch += 1
    pol._act_cache.clear()
    st3 = St([a, b, st2.cards[2]], kred=4)
    pol._actionable(st3, a)
    chk("指挥点变 ⇒ 重问", pol.calls == 6)
    # 回合边界清空
    pol.__dict__["_xcache"].clear()
    pol._act_cache.clear()
    pol._actionable(st3, a)
    chk("清空后重问", pol.calls == 7)
    return 1 if chk.fails else 0


if __name__ == "__main__":
    sys.exit(main())
