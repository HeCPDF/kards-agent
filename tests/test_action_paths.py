#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行动 = 动作 + 其后选择（选择路径）：展开 / 24 枝上限 / 随机分支不展开 / 执行侧照路径 / 放回牌库顶的手牌目标。"""
import os
import sys
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP, mk_card                      # noqa: E402
from engine.state import H, Sim, U                                 # noqa: E402
from sim.engine import A, _apply_eff, apply, prompt_of, run        # noqa: E402
from evaluation.value import evaluate                              # noqa: E402
from policy.search import PATH_CAP, _gen_actions, gen_actions      # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(hand, units=(), kred=10.0):
    return Sim({u.id: u for u in units}, {ME: 20, OPP: 20}, kred, {h.id: h for h in hand}, my_side=ME)


def main():
    # 抉择牌：两个分支 {opcost:0} / {buff:[4,4]}，outcomes_mode=max ⇒ 展开成 2 个行动
    me = U(1, ME, "back", 2, 2, 2, "infantry")
    choice = H(10, "RANGERS", 3, "infantry", 2, 2, eff={"outcomes": [(0.5, {"opcost": 0}), (0.5, {"buff": [4, 4]})],
                                                         "outcomes_mode": "max"})
    sim = mk([choice], [me])
    acts = [a for a in gen_actions(sim) if a.kind == "deploy"]
    chk("抉择牌展开成每个选项一个行动，path=(i,)", sorted(a.path for a in acts) == [(0,), (1,)], str(acts))
    chk("不同路径的 key 不同（搜索的 tried/bad 不互相污染）", len({a.key() for a in acts}) == 2)
    v = {a.path: evaluate(apply(sim, a)) for a in acts}
    chk("各路径带自己的分支效果：+4/+4 路径价值更高", v[(1,)] > v[(0,)], str(v))
    # 老写法（不展开、效果落不到刚部署的牌上）的值 ≤ 展开后最好路径（路径让自身 buff 真的算进去了）
    s_old = sim.copy()
    s_old.hand.pop(10)
    s_old.kredits -= 3
    s_old.units[-5010] = U(-5010, ME, "back", 2, 2, 3, "infantry", (), sick=True)
    _apply_eff(s_old, choice.eff, None)
    chk("展开后最好路径 ≥ 原来 outcomes 取 max 的值（自身 buff 现在算得到）", max(v.values()) >= evaluate(s_old) - 1e-9,
        "%.4f vs %.4f" % (max(v.values()), evaluate(s_old)))

    # 随机分支（没有 outcomes_mode）不展开
    rnd = H(11, "RANDOM", 2, "order", eff={"outcomes": [(0.5, {"damage_hq": 1}), (0.5, {"damage_hq": 3})]})
    acts = [a for a in gen_actions(mk([rnd], [me])) if a.src == 11]
    chk("随机分支不展开（不是我们能选的）", all(a.path == () for a in acts) and acts, str(acts))

    # 24 枝上限：一张 40 选项的牌 ⇒ 只留 24 条，且最好的在里面
    many = H(12, "MANY", 1, "order", eff={"outcomes": [(1 / 40, {"damage_hq": i}) for i in range(40)],
                                          "outcomes_mode": "max"})
    acts = [a for a in gen_actions(mk([many], [me])) if a.src == 12]
    chk("展开出的路径最多 24 条", len(acts) == PATH_CAP == 24, str(len(acts)))
    chk("剪枝保留最好的路径（伤害最大的那个）", any(a.path == (39,) for a in acts))

    # 执行侧：搜索选了路径 ⇒ choose_pick 照点
    import player.rule as R
    pol = R.RuleV2.__new__(R.RuleV2)
    pol.__dict__["_plan"] = {10: (1,)}
    rows = [{"index": 0, "label": "行动花费为 0", "trigger_id": 10, "kind": "choose_one"},
            {"index": 1, "label": "+4/+4", "trigger_id": 10, "kind": "choose_one"}]
    pol._pick_gate = lambda c: True
    pol.P = {}
    act = pol.choose_pick(rows, None)
    chk("choose_pick 照搜索路径点第 1 项，来源标 plan", act["index"] == 1 and act["meta"]["pick_eval_src"] == "plan", str(act))

    # 手牌目标：放回类选"最用不上"的；非放回类仍选最值钱
    def card(i, cost, atk, dfn):
        return mk_card(i, ME, "hand", "infantry", atk, dfn, cost, 1, "C%d" % i, ptr=i)
    hand = [card(1, 2, 3, 3), card(2, 8, 5, 5), card(3, 1, 1, 1)]
    st = NS(hand=lambda side=None: hand, kredits={ME: 3}, cards=hand, my_side=ME, other_side=OPP, my_side_raw=1)
    pol2 = R.RuleV2.__new__(R.RuleV2)
    pol2.sess = NS(hand_target_legal=lambda c: {"can": True})
    pol2.worth = lambda c: float(c.attack + c.defense)
    pol2._hand_target_puts_back = lambda st_, pend, cand: True
    a = pol2.choose_hand_target(st, {"hand_target": {"card_being_played": 99}})
    chk("放回牌库顶：先放这回合指挥点不够的那张（8 费）", a["card"] == 2 and a["meta"]["hand_target_mode"] == "put_back", str(a))
    pol2._hand_target_puts_back = lambda st_, pend, cand: False
    a = pol2.choose_hand_target(st, {})
    chk("非放回类：仍选最值钱的", a["card"] == 2)
    hand2 = [card(1, 2, 3, 3), card(3, 1, 1, 1)]
    st2 = NS(hand=lambda side=None: hand2, kredits={ME: 9}, cards=hand2, my_side=ME, other_side=OPP, my_side_raw=1)
    pol2._hand_target_puts_back = lambda st_, pend, cand: True
    a = pol2.choose_hand_target(st2, {})
    chk("放回类且都打得起：放价值最低的那张", a["card"] == 3, str(a))
    # ---- 预报：挂起两层提示（sim.engine.run），9 条路径 ----
    from sim.prompt import Done, Suspended, leaves
    table = {w: {t: ("card_event_%s_%s" % (w, t), float(i * 3 + j)) for j, t in enumerate(("light", "medium", "heavy"))}
             for i, w in enumerate(("sunny", "rain", "storm"))}
    fc = H(20, "FORECASTER", 2, "order", eff={"forecast": True, "pin": True})
    simf = mk([fc], [me])
    simf.forecast = table
    a0 = [a for a in _gen_actions(simf) if a.src == 20][0]
    r = run(simf, a0)
    chk("预报：run 挂起第一层（3 种天气），不预先决定", isinstance(r, Suspended) and r.prompt.layer == 1
        and [o.key for o in r.prompt.options] == ["sunny", "rain", "storm"])
    r2 = r.resume("rain")
    chk("第一层选 rain ⇒ 第二层 3 个变体（候选来自按种子预测的确定表）", isinstance(r2, Suspended) and r2.prompt.layer == 2
        and [o.key for o in r2.prompt.options] == ["light", "medium", "heavy"])
    d = r2.resume("medium")
    chk("第二层选 medium ⇒ Done；牌进 pending_cards（下回合开始才到手），原有效果（压制）照常", isinstance(d, Done)
        and d.state.pending_cards == [("card_event_rain_medium", 4.0)])
    chk("叶路径一共 9 条", len(leaves(run(simf, a0))) == 9)
    acts = [a for a in gen_actions(simf) if a.src == 20]
    chk("预报牌在行动空间里是 9 个行动，路径 = (天气, 变体)", len(acts) == 9 and all(len(a.path) == 2 for a in acts), str(acts[:2]))
    vals = {a.path: evaluate(apply(simf, a)) for a in acts}
    best = max(vals, key=vals.get)
    chk("评估（含 pending_cards 的估值）取分最高的路径 = storm/heavy", best == ("storm", "heavy"), str(best))
    chk("apply 带 path 的行动 == 手工 resume 到底", abs(evaluate(apply(simf, acts[0])) - evaluate(
        run(simf, a0).resume(acts[0].path[0]).resume(acts[0].path[1]).state)) < 1e-9)
    nof = mk([fc], [me])
    chk("没有候选表 ⇒ 不挂起，sim.engine 如实记缺口", prompt_of(nof, a0) is None
        and any("forecast" in g for g in apply(nof, a0).gaps))
    # 执行侧：两层照计划点
    pol3 = R.RuleV2.__new__(R.RuleV2)
    pol3.__dict__["_plan"] = {20: ("rain", "medium")}
    pol3._pick_gate = lambda c: True
    pol3.P = {}
    pol3.plan = lambda c: None
    pol3._fc_paths = lambda ttl=0.5: {"rain": {"light": "card_event_rain_light", "medium": "card_event_rain_medium", "heavy": "card_event_rain_heavy"}}
    l1 = [{"index": i, "name": n, "trigger_id": 20, "kind": "select_card_to_draw"} for i, n in
          enumerate(("card_event_sunny1_blue_sky", "card_event_rain1_mist", "card_event_storm1_gale"))]
    a1 = pol3.choose_pick(l1, None)
    chk("预报第一层：照计划点 rain", a1["index"] == 1 and a1["meta"]["pick_eval_src"] == "plan_fc", str(a1["meta"]))
    l2 = [{"index": i, "name": "card_event_rain_%s" % t, "trigger_id": 20, "kind": "select_card_to_draw"}
          for i, t in enumerate(("light", "medium", "heavy"))]
    a2 = pol3.choose_pick(l2, None)
    chk("预报第二层：照计划点 medium，并把计划用掉", a2["index"] == 1 and not pol3.__dict__["_plan"], str(a2["meta"]))
    # ---- 手牌目标提示（175th / PBY）：同一个入口，动作引发与强制决策共用 ----
    from policy import forced as PF
    inst = H(30, "175TH", 3, "infantry", 6, 5, eff={"hand_target_pending": True})
    good = H(31, "GOOD", 5, "infantry", 5, 5)
    bad = H(32, "BAD", 1, "order")
    sh = mk([inst, good, bad], [me], kred=10)
    sh.deck, sh.deck_known = [90], True
    sh.hand_target_fx = {30: {31: {"to_deck": True}, 32: {"to_deck": True}}}
    a30 = [a for a in _gen_actions(sh) if a.src == 30][0]
    r = run(sh, a30)
    chk("手牌目标：打出 175th 后挂起 hand_target，候选 = 另外两张手牌", isinstance(r, Suspended) and r.prompt.kind == "hand_target"
        and sorted(o.key for o in r.prompt.options) == [31, 32])
    d = r.resume(32).state
    chk("选中的手牌放回牌库顶（手里少这张、牌库顶多这张），部署照常", 32 not in d.hand and d.deck[0] == 32 and 31 in d.hand)
    paths = [a.path for a in gen_actions(sh) if a.src == 30]
    chk("行动空间里 175th 展开成每个候选一个行动（路径 = 选中的手牌 id）", sorted(paths) == [(31,), (32,)], str(paths))
    ans = PF.answer_hand_target(sh, 30)
    chk("强制决策入口（policy.forced）：放回价值最低的那张（BAD），分数表齐全", ans and ans[0] == 32 and len(ans[1]) == 2, str(ans))
    # ---- autoplay 待打出队列（原版：入队，动作边界冲刷）----
    auto = H(40, "AUTO", 0, "order", eff={"_hold": 0.0, "_autoplay": True, "_on_draw": {"damage_hq": 3}})
    drawer = H(41, "DRAWER", 1, "order", eff={"draw": 1})
    sa = mk([drawer], [me], kred=5)
    sa.deck, sa.deck_known, sa.deck_cards = [40], True, {40: auto}
    s1 = apply(sa, A("order", 41, None, 1))
    chk("autoplay：抽到后已冲刷（效果生效一次：敌方总部 -3），队列为空", s1.hq[OPP] == 17 and not s1.autoplay_queue, str(s1.hq))
    mid = sa.copy()
    from sim import chain as CH
    CH.draw_chain(mid, 1, apply_effect=_apply_eff, hand_cap=9, anon_hold=1.2, cap=50)
    chk("autoplay：draw_chain 只入队、此刻还没打出（动作边界才冲刷）", mid.hq[OPP] == 20 and len(mid.autoplay_queue) == 1)
    CH.flush_autoplay(mid, _apply_eff)
    chk("flush_autoplay：冲刷后生效、队列清空", mid.hq[OPP] == 17 and not mid.autoplay_queue)
    # ---- 攻击/上线触发的预报：同一个决策（总纲 §1）----
    atk = U(70, ME, "frontline", 3, 3, 3, "infantry")
    sa2 = Sim({70: atk, 71: U(71, OPP, "frontline", 1, 9, 1, "infantry")}, {ME: 20, OPP: 20}, 5.0, {},
                attack_fx={(70, "hq"): {"buckets": {"after": {"eff": {"forecast": True}, "units": []}}}}, my_side=ME)
    sa2.forecast = table
    atk_acts = [a for a in gen_actions(sa2) if a.kind == "attack" and a.src == 70 and a.dst == "hq"]
    chk("攻击触发预报：攻击这个动作展开成 9 条路径（天气×变体）", len(atk_acts) == 9 and all(len(a.path) == 2 for a in atk_acts), str(len(atk_acts)))
    end = apply(sa2, atk_acts[0])
    chk("攻击路径应用后：攻击结算了（敌方总部掉血）、预报牌进 pending_cards、没有『未展开』缺口", end.hq[OPP] < 20
        and len(end.pending_cards) == 1 and not any("forecast" in g for g in end.gaps), str((end.pending_cards, end.gaps)))
    sm = Sim({72: U(72, ME, "back", 2, 2, 1, "infantry", opc=1)}, {ME: 20, OPP: 20}, 5.0, {},
               event_fx={"move": {72: {"forecast": True}}}, my_side=ME)
    sm.forecast = table
    mv = [a for a in gen_actions(sm) if a.kind == "move"]
    chk("上线触发预报：上线展开成 9 条路径", len(mv) == 9, str(len(mv)))
    sm_end = apply(sm, mv[4])
    chk("上线路径应用后：单位上线、预报牌进 pending_cards", sm_end.units[72].row == "frontline" and len(sm_end.pending_cards) == 1)
    # 规则层产出的行动 dict（带 path）必须能转成 nn.Action——实机回归：面板里一局第二步就
    # `TypeError: Action.__init__() got an unexpected keyword argument 'path'`
    from player import loop as NN
    act = NN.as_action({"kind": "play_event", "card": 7, "target": None, "index": None, "marks": [],
                        "note": "n", "score": 1.5, "meta": {}, "path": ["card_unit_a"]})
    chk("as_action 接受 path 并保留（元组）", act.path == ("card_unit_a",) and act.to_dict()["path"] == ["card_unit_a"])
    chk("没有 path 的行动 to_dict 不带 path 键", "path" not in NN.as_action({"kind": "end"}).to_dict())
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
