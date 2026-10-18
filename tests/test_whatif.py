#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘面模拟：`player.whatif`（命令解析 / 布局 / 差异 / 帧栈：多步、undo、reset / 抉择 choose / spawn / forecast / 敌方手牌）与 `gui.whatif_panel`。
离线：假会话 + 手搭 `Sim`，不连游戏。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from _cards import ME, OPP, mk_card                                  # noqa: E402
from kardsmem.board import BoardState                                # noqa: E402
from engine.state import H, Sim, U                                   # noqa: E402
from player import whatif as W                                       # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


def board():
    k = dict(kredit_cost=1, operation_cost=1)
    o = dict(card_type="order", attack=0, defense=0, **k)
    cards = [mk_card(1, ME, "hq", attack=0, defense=20, name="MYHQ", **k), mk_card(2, OPP, "hq", attack=0, defense=20, name="THEIRHQ", **k),
             mk_card(10, ME, "hand", name="H-DAMAGE", **o), mk_card(11, ME, "hand", name="H-CHOOSE-ONE", **o),
             mk_card(12, ME, "hand", name="H-THREE-PICK", **o), mk_card(13, ME, "hand", name="H-FORECAST", **o),
             mk_card(14, ME, "hand", card_type="infantry", attack=2, defense=2, name="H-UNIT", **k),
             mk_card(15, ME, "hand", name="H-DISCARD", **o),
             mk_card(40, OPP, "hand", name="E-HAND-A", **o), mk_card(41, OPP, "hand", name="E-HAND-B", **o),
             mk_card(20, ME, "frontline", attack=3, defense=3, name="MY-FRONT", **k),
             mk_card(21, ME, "back", attack=1, defense=1, name="MY-BACK", **k),
             mk_card(30, OPP, "frontline", attack=2, defense=2, name="OPP-FRONT", **k)]
    return BoardState(source="t", cards=cards, my_side=ME, turn=3, our_turn=True,
                      kredits={ME: 9, OPP: 2}, slots={ME: 3, OPP: 3}, hq={ME: cards[0], OPP: cards[1]})


def sim():
    units = {20: U(20, ME, "frontline", 3, 3, 3, "infantry", opc=1), 21: U(21, ME, "back", 1, 1, 1, "infantry", opc=1),
             30: U(30, OPP, "frontline", 2, 2, 2, "infantry", opc=1)}
    hand = {10: H(10, "H-DAMAGE", 2, "order", eff={"damage": 2, "slot": -1}),
            11: H(11, "H-CHOOSE-ONE", 1, "order", eff={"outcomes": [(0.5, {"damage": 3}), (0.5, {"draw": 1})], "outcomes_mode": "max"}),
            12: H(12, "H-THREE-PICK", 1, "order", eff={"choose_spawn": True}),
            13: H(13, "H-FORECAST", 1, "order", eff={"forecast": True}),
            14: H(14, "H-UNIT", 2, "infantry", 2, 2),
            15: H(15, "H-DISCARD", 1, "order", eff={"discard_ids": [40]})}
    picks = {12: [{"name": "card_a", "cost": 1, "typ": "infantry", "atk": 1, "dfn": 1},
                  {"name": "card_b", "cost": 2, "typ": "infantry", "atk": 2, "dfn": 2}]}
    fc = {w: {"light": ("%s_l" % w, 1.0), "medium": ("%s_m" % w, 1.0), "heavy": ("%s_h" % w, 1.0)} for w in ("sunny", "rain", "storm")}
    return Sim(units, {ME: 20, OPP: 20}, kredits=9, hand=hand, slots=3, my_side=ME, spawn_pick=picks, forecast=fc)


class _Sess:
    def snapshot(self):
        return board()

    def _kardsmem(self):
        raise RuntimeError("no game")


class _W(W.WhatIf):
    def _build(self, st):                  # 不跑 VM：直接给手搭的场面
        return sim()


def main():
    # ---- 解析 ----
    c = W.parse("deploy h5 2 choose 1 with e2")
    chk("parse：deploy <牌> slot choose with", (c.verb, c.src, c.slot, c.choice, c.dst) == ("deploy", "h5", 2, 1, "e2"))
    c = W.parse("play h3 with e2")
    chk("parse：play <牌> with <目标>", (c.verb, c.src, c.dst, c.slot, c.choice) == ("play", "h3", "e2", None, None))
    chk("parse：attack 可省略 with", W.parse("attack m1 ehq").dst == "ehq" and W.parse("attack m1 with ehq").dst == "ehq")
    chk("parse：spawn / select / forecast / undo / reset / end", W.parse("spawn card_a").arg == "card_a" and W.parse("select h2").arg == "h2"
        and W.parse("forecast").verb == "forecast" and W.parse("spawn sunny").arg == "sunny"
        and W.parse("undo").verb == "undo" and W.parse("end").verb == "end")
    for bad_cmd in ("", "foo x", "play", "deploy h1 0", "play h1 choose", "play h1 with", "deploy h1 2 3", "attack m1", "spawn",
                    "select", "forecast sunny", "play h1 7", "front m1 x", "front m1 0", "front m1 1 2", "play h3.1"):
        try:
            W.parse(bad_cmd)
            chk("parse：坏命令 %r 报错" % bad_cmd, False)
        except ValueError:
            chk("parse：坏命令 %r 报错" % bad_cmd, True)
    chk("parse：front <单位> [slot]", W.parse("front m1").slot is None and W.parse("front m1 2").slot == 2)
    chk("split_line：分号（含全角）、空段丢掉", W.split_line("play h1; attack m1 ehq ；; undo") == ["play h1", "attack m1 ehq", "undo"])

    # ---- 差异 ----
    a, b = sim(), sim()
    b.hq[OPP] = 17
    b.kredits = 8
    b.units[30].dfn = 0
    del b.units[20]
    del b.hand[10]
    d = W.diff_sims(a, b, lambda i: "u%s" % i, lambda h: "hand%s" % h)
    chk("diff：总部/指挥点/单位/手牌变化各一行", "enemy_hq 20 -> 17" in d and "kredit 9 -> 8" in d and "u20 dies/leaves" in d
        and "u30 2/2 -> 2/0" in d and "hand -hand10" in d, str(d))
    chk("diff：两份相同 ⇒ 空", W.diff_sims(sim(), sim(), str) == [])

    # ---- 盘面模拟要按真实牌库抽：抽到抵抗（autoplay）时它自己的效果要结算 ----
    s = sim()
    s.deck = [7]
    s.deck_known = True
    s.deck_cards = {7: H(7, "H-RESIST", 1, "order", eff={"_on_draw": {"damage_hq": 3}, "_autoplay": True})}
    from engine.chain import draw_chain, flush_autoplay
    from sim.engine import _apply_eff
    draw_chain(s, 1, apply_effect=_apply_eff)
    chk("抽牌抽到抵抗（autoplay）⇒ 进待打出队列，不当普通手牌", len(s.autoplay_queue) == 1, str(s.autoplay_queue))
    base_hq = s.hq[OPP]
    flush_autoplay(s, _apply_eff)
    chk("动作边界冲刷 ⇒ 抵抗的效果结算（敌方总部掉 3）", s.hq[OPP] == base_hq - 3, str(s.hq))

    class _RDeck:
        def _deck_state(self, st):
            return [7, 8], {7: H(7, "D1", 1, "order", eff={}), 8: H(8, "D2", 1, "order", eff={})}

        def _fatigue(self, st):
            return 4

    class _RNoDeck(_RDeck):
        def _deck_state(self, st):
            return None, {}

    s2 = sim()
    W.WhatIf._fill_deck(s2, object(), _RDeck())
    chk("_fill_deck：把真实牌序/模板/疲劳补进 sim（抽牌才知道抽到的是抵抗还是别的）",
        s2.deck == [7, 8] and s2.deck_known and set(s2.deck_cards) == {7, 8} and s2.fatigue == 4,
        str((s2.deck, s2.deck_known, s2.fatigue)))
    s3 = sim()
    W.WhatIf._fill_deck(s3, object(), _RNoDeck())
    chk("_fill_deck：牌序读不出 ⇒ deck_known 仍 False（不假装知道）", not s3.deck_known and not s3.deck)

    # ---- 被贴的效果 / 攻防格式 ----
    from agent import view as _V
    c9 = mk_card(90, ME, "hand", card_type="infantry", attack=1, defense=4, name="BUFFED", kredit_cost=1)
    c9.obj.attackBuff = 1
    chk("_stat：攻击带 buff 写成 (1+1)/4（括号把整个攻数值包起来）", _V._stat(c9) == "(1+1)/4", _V._stat(c9))
    fx = {"buffs_from_cards": [{"giver_card_id": 7, "giver_name": "GIVER", "buffs": {"attack": 2}}],
          "received_abilities": [{"ability": "passive", "givers": [7]}], "kredits_tax_extra": 1}
    chk("live_fx_text：写出被贴的 buff / 能力 / 指向税",
        W.live_fx_text(fx) == "GIVER attack+2; 能力:passive; 税+1", W.live_fx_text(fx))
    chk("live_fx_text：没有 CDO 外的被贴效果 ⇒ 空", W.live_fx_text({}) == "" and W.live_fx_text({
        "buffs_from_cards": [{"giver_card_id": 7, "giver_name": "GIVER", "buffs": {}}]}) == "")

    # ---- 布局：敌方手牌 / 敌方支援线 / 前线 / 我方支援线 / 我方手牌 ----
    w = _W(_Sess())
    txt = w.view()[0]
    pos = [txt.index("\n" + x) for x in ("ENEMY HAND", "ENEMY SUPPORT", "FRONTLINE", "MY SUPPORT", "MY HAND")]
    chk("布局：五段从上到下的次序", pos == sorted(pos), txt)
    chk("布局：敌方手牌可见（H1/H2），我方手牌带费用/单位攻防", "H1 E-HAND-A" in txt and "H2 E-HAND-B" in txt
        and "h1 H-DAMAGE c1" in txt and "h5 H-UNIT 2/2 c1" in txt, txt)

    class _SessKw(_Sess):
        def snapshot(self):
            b = board()
            b.cards[6].obj.hasGuard = True                 # H-UNIT 带上关键词，手牌行要像场上那样显示词条
            return b
    myh = [l for l in _W(_SessKw()).view()[0].splitlines() if l.startswith("MY HAND")][0]
    chk("手牌单位也显示词条（和场上一样）", "h5 H-UNIT 2/2 [guard] c1" in myh, myh)
    chk("布局：总部在各自支援线最左", "ehq 20" in txt and "hq 20" in txt, txt)

    # ---- 一步 ----
    w = _W(_Sess())
    r = w.run_line("attack m1 ehq")[0]
    chk("attack m1 ehq：敌方总部掉血", r.ok and any(x.startswith("enemy_hq 20 ->") for x in r.diff), str((r.error, r.diff)))
    w = _W(_Sess())
    r = w.run_line("play h1")[0]
    chk("play h1：效果摘要带出 + 手牌少一张", r.ok and r.kind == "order" and r.eff.get("damage") == 2
        and any(x.startswith("hand -") for x in r.diff), str((r.error, r.eff, r.diff)))
    chk("找不到的短号给人话错误", "找不到" in w.run_line("play h9")[0].error)
    chk("play 单位 / deploy 指令：提示用对的动词", "要用 deploy" in _W(_Sess()).run_line("play h5")[0].error
        and "要用 play" in _W(_Sess()).run_line("deploy h1")[0].error)

    # ---- 三选一候选池为空（牌库没牌）：只有这一个效果的牌原版打不出；候选读不出：保留并提醒 ----
    class _R:
        def __init__(self, fail=False):
            self.gaps, self.cands, self.fail = {}, {}, fail

        def _spawn_cands(self, st, i):
            if self.fail:                                          # 真实现：失败时记缺口、返回 []
                self.gaps["H-THREE-PICK#choose_spawn"] = "预测失败：x"
            return self.cands.get(i, [])
    w = _W(_Sess())
    w._reset()
    rr = _R()
    w._ensure_spawn_pick(w.top.sim, w.base_st, rr)
    r = w.run_line("play h3")[0]
    chk("候选池为空且没有别的效果 ⇒ 原版打不出：拒绝并说明", (not r.ok) and "无法打出" in r.error and w.card_options("h3")["legal"] is False, str((r.ok, r.error)))
    w = _W(_Sess())
    w._reset()
    rr = _R()
    rr.cands = {12: [{"name": "card_x", "cost": 1, "typ": "infantry", "atk": 1, "dfn": 1}]}
    w._ensure_spawn_pick(w.top.sim, w.base_st, rr)
    chk("有候选 ⇒ 填进 sim.spawn_pick，打出后挂起三选一", (w.run_line("play h3")[0].choices or {}).get("need") == "spawn")
    w = _W(_Sess())
    w._reset()
    rr = _R(fail=True)
    w._ensure_spawn_pick(w.top.sim, w.base_st, rr)
    r = w.run_line("play h3")[0]
    chk("预测失败 ⇒ 不拒绝、提醒“候选读不出”", r.ok and any("读不出" in n for n in r.notes), str((r.ok, r.notes)))

    # ---- 目标不合法要拒绝（以前只提醒、照常模拟：只能指向敌方的牌指友方也通过）----
    w = _W(_Sess())
    r = w.run_line("play h1 with hq")[0]
    chk("目标不在合法走法里 ⇒ 拒绝并列出可选目标", (not r.ok) and "不能指向" in r.error and "e1" in r.error and not w.stack[1:], str(r.error))
    chk("合法目标照常通过", w.run_line("play h1 with e1")[0].ok)
    o = _W(_Sess()).card_options("h1")
    chk("card_options：可指向的目标集合、是否可不带目标", {"e1", "ehq"} <= o["targets"] and o["legal"] is True, str(o))

    # ---- deploy slot：支援线从左数第几个 ----
    w = _W(_Sess())
    r = w.run_line("deploy h5 1")[0]
    t1 = w.view()[0]
    mys = [l for l in t1.splitlines() if l.startswith("MY SUPPORT")][0]
    chk("deploy h5 1：新单位 n1 在我方支援线最左（总部之后、MY-BACK 之前）", r.ok and mys.index("n1 H-UNIT") < mys.index("MY-BACK"), mys)
    # 总部和支援线单位同一行、共用站位：总部在中间时，布局按站位排；slot 1 可以插到总部左边
    class _S2(_Sess):
        def snapshot(self):
            b = board()
            for c in b.cards:
                if c.name == "MYHQ":
                    c.obj.locationNumber = 1
                if c.name == "MY-BACK":
                    c.obj.locationNumber = 2
            return b
    w = _W(_S2())
    mys0 = [l for l in w.view()[0].splitlines() if l.startswith("MY SUPPORT")][0]
    chk("站位：总部按站位排（在 MY-BACK 左边），不是永远最左", mys0.index("hq 20") < mys0.index("MY-BACK"), mys0)
    w.run_line("deploy h5 1")
    mys1 = [l for l in w.view()[0].splitlines() if l.startswith("MY SUPPORT")][0]
    chk("deploy h5 1：插到总部左边", mys1.index("n1 H-UNIT") < mys1.index("hq 20") < mys1.index("MY-BACK"), mys1)
    w = _W(_Sess())
    w.run_line("deploy h5")
    mys = [l for l in w.view()[0].splitlines() if l.startswith("MY SUPPORT")][0]
    chk("deploy h5（不写 slot）：最右", mys.index("MY-BACK") < mys.index("n1 H-UNIT"), mys)
    chk("模拟后的布局带步数，我方手牌里已没有 H-UNIT", "SIM step 1" in w.view()[0] and "H-UNIT" not in
        [l for l in w.view()[0].splitlines() if l.startswith("MY HAND")][0])

    # ---- 多条 / undo / reset ----
    w = _W(_Sess())
    rs = w.run_line("play h1; play h1; attack m1 ehq")
    chk("多条：按次序给出；第二条 play h1 因手牌已打出而出错，后面的不执行",
        len(rs) == 2 and rs[0].ok and (not rs[1].ok) and "不在模拟的手牌里" in rs[1].error
        and any("后面 1 条命令没有执行" in n for n in rs[1].notes), str([(r.echo, r.error) for r in rs]))
    rs = w.run_line("undo; play h1")
    chk("undo 撤回上一条：同一张牌可再打", [r.ok for r in rs] == [True, True] and rs[0].kind == "undo", str([(r.echo, r.error) for r in rs]))
    w = _W(_Sess())
    rs = w.run_line("play h1; reset; play h1")
    chk("reset 回到实时盘面：深度归零", [r.ok for r in rs] == [True, True, True] and rs[1].depth == 0 and rs[2].depth == 1)
    chk("没有可撤回的：undo 报错", not _W(_Sess()).run_line("undo")[0].ok)

    # ---- 还没模拟过时，基线自动跟随实时盘面（用户 2026-10-19：盘面自动更新后用手牌说“不在手上”）----
    class _SessMoving(_Sess):
        def __init__(self):
            self.n = 0

        def snapshot(self):
            b = board()
            if self.n:
                b.turn = 4                                   # 游戏里盘面变了（回合/手牌/种子任一）
            return b

    sm = _SessMoving()
    w = _W(sm)
    r1 = w.run_line("play h1")[0]
    w.run_line("undo")
    sm.n = 1
    r2 = w.run_line("play h1")[0]
    chk("还没模拟过时：盘面一变，下一条命令前自动重取快照（不必手动 reset）",
        r1.ok and r2.ok and w.base_st.turn == 4 and len(w.stack) == 2,
        str((r1.ok, r2.ok, w.base_st.turn, len(w.stack))))
    w.run_line("play h1")                                   # 有一步了：基线不再自动重取
    sm.n = 0
    r3 = w.run_line("play h6")[0]
    chk("有模拟步骤时：不自动重取（步骤依赖旧基线；面板会提示 reset）",
        w.base_st.turn == 4 and (r3.ok or "不在模拟的手牌里" in r3.error), str((w.base_st.turn, r3.error)))

    # ---- 抉择：必须写在 deploy/play 里（choose <i>） ----
    w = _W(_Sess())
    r = w.run_line("play h2")[0]
    chk("抉择牌不写 choose：报错并列出选项", (not r.ok) and "必须写 choose" in r.error and r.choices and r.choices["need"] == "choose"
        and [o[0] for o in r.choices["options"]] == [0, 1], str((r.error, r.choices)))
    r = w.run_line("play h2 choose 1")[0]
    chk("play h2 choose 1：按选项 1 模拟（抽牌）", r.ok and r.eff == {"draw": 1}, str((r.error, r.eff)))
    chk("play h2 choose 0：另一个选项的效果不同", _W(_Sess()).run_line("play h2 choose 0")[0].eff == {"damage": 3})
    chk("choose 越界 / 没有抉择的牌写 choose 都报错", "不在选项" in _W(_Sess()).run_line("play h2 choose 5")[0].error
        and "没有抉择" in _W(_Sess()).run_line("play h1 choose 0")[0].error)
    chk("choose 不是独立命令", not _W(_Sess()).run_line("choose 0")[0].ok)

    # ---- 三选一：spawn <牌> ----
    w = _W(_Sess())
    r = w.run_line("play h3")[0]
    chk("play h3：挂起并列出候选（FName）", r.ok and r.choices and r.choices["need"] == "spawn"
        and [o[0] for o in r.choices["options"]] == ["card_a", "card_b"] and not r.diff, str((r.error, r.choices, r.diff)))
    pv = w.view()[0]
    chk("挂起期间布局里有 PENDING、回答方式与候选", "PENDING" in pv and "spawn <card>" in pv and "- card_a" in pv, pv)
    r = w.run_line("attack m1 ehq")[0]
    chk("挂起期间不能做别的", (not r.ok) and "spawn" in r.error and r.choices, str(r.error))
    chk("spawn 没有唯一匹配报错", "没有唯一匹配" in w.run_line("spawn zzz")[0].error)
    r = w.run_line("spawn card_b")[0]
    chk("spawn card_b：进手牌（g1），差异以打出前为底", r.ok and r.kind == "spawn" and any("card_b" in x for x in r.diff)
        and any(x.startswith("hand -") for x in r.diff), str((r.error, r.diff)))
    chk("没有挂起时 spawn / forecast / select 报错", all("没有等着回答" in _W(_Sess()).run_line(c)[0].error for c in ("spawn card_a", "forecast", "select h1")))
    rs = w.run_line("undo; undo; play h3; spawn 0")
    chk("undo 撤回 spawn → 回到待选；再 undo 撤回 play；spawn 也认下标",
        [r.ok for r in rs] == [True, True, True, True] and any("card_a" in x for x in rs[3].diff), str([(r.echo, r.error) for r in rs]))

    # ---- 预报：① 两次 spawn（3 选 1 × 2）② forecast 走一步列出候选（可 undo），再 spawn 从候选里选一张 ----
    w = _W(_Sess())
    r = w.run_line("play h4")[0]
    chk("play h4（预报牌）：挂起为 forecast，第一层是 3 张类型卡（蓝天/薄雾/狂风）", r.ok and r.choices and r.choices["need"] == "forecast"
        and int(r.choices.get("layer") or 1) == 1
        and [o[0] for o in r.choices["options"]] == ["card_event_sunny1_blue_sky", "card_event_rain1_mist", "card_event_storm1_gale"],
        str((r.error, r.choices)))
    r = w.run_line("spawn card_a")[0]
    chk("预报挂起时 spawn 只认候选（不是三选一的牌名）", not r.ok and "没有唯一匹配" in r.error, str(r.error))
    r = w.run_line("forecast")[0]
    labs = [o[1] for o in (r.choices or {}).get("options", [])]
    chk("forecast：算一步模拟，把 9 张候选列出来、等 spawn 选一张",
        r.ok and r.choices and r.choices["need"] == "forecast" and (r.choices.get("meta") or {}).get("merged")
        and len(labs) == 9 and "sunny_l" in labs and "storm_h" in labs and w.top.pending is not None
        and len(w.stack) == 3 and not r.diff and r.depth == 2, str((r.error, labs, r.depth, len(w.stack))))
    r = w.run_line("forecast")[0]
    chk("候选已经列出来时再 forecast 只提示用 spawn 选", not r.ok and "spawn" in r.error, str(r.error))
    r = w.run_line("spawn sunny_h")[0]
    chk("spawn 从 9 张里选一张：定案，选中的牌下回合进手牌", r.ok and r.choices.get("done") and r.choices.get("picked")
        and r.choices.get("card") == "sunny_h" and r.choices.get("picks") == ["sunny", "heavy"]
        and any("next_turn hand +sunny_h" in x for x in r.diff) and w.top.pending is None,
        str((r.error, r.choices, r.diff)))
    rs = w.run_line("undo; undo")
    chk("undo 撤回 forecast 那一步 ⇒ 回到第一层天气挂起（这次 forecast 可退）", [x.ok for x in rs] == [True, True]
        and w.top.pending is not None and w.top.pending.prompt.meta.get("forecast")
        and not (w.top.pending.prompt.meta or {}).get("merged") and int(w.top.pending.prompt.layer) == 1,
        str([x.echo for x in rs]))

    w = _W(_Sess())
    w.run_line("play h4")
    r = w.run_line("spawn sunny")[0]
    labs = [o[1] for o in (r.choices or {}).get("options", [])]
    chk("两次 spawn 的第一步：真的选天气，挂到第二层（3 档，每档写着拿哪张牌）", r.ok
        and int(r.choices.get("layer") or 1) == 2 and [o[0] for o in r.choices["options"]] == ["light", "medium", "heavy"]
        and "sunny_l" in labs[0], str((r.error, labs)))
    r = w.run_line("spawn sunny_h")[0]
    chk("两次 spawn 的第二步：真的选档，选中的牌下回合进手牌", r.ok and r.choices.get("done") and r.choices.get("picked")
        and r.choices.get("card") == "sunny_h" and any("next_turn hand +sunny_h" in x for x in r.diff)
        and w.top.pending is None, str((r.error, r.choices, r.diff)))
    chk("真的选完之后可以继续别的命令", w.run_line("play h1")[0].ok)

    w = _W(_Sess())
    w.run_line("play h4; spawn sunny")
    r = w.run_line("forecast")[0]
    chk("第一层已经用 spawn 选了天气 ⇒ 不能再 forecast", not r.ok and "不能再 forecast" in r.error
        and int(r.choices["layer"]) == 2, str((r.error, r.choices)))
    r = w.run_line("spawn 1")[0]
    chk("那就直接 spawn 选档（按下标）", r.ok and r.choices.get("card") == "sunny_m"
        and r.choices.get("picks") == ["sunny", "medium"], str((r.error, r.choices)))

    # ---- 规则侧没给出预报候选表（sim.forecast 空）时，模拟自己补（实机 OVERCAST 就没挂起，直接结算了）----
    from kardsmem import forecast_pool as FP

    class _Pool:
        @staticmethod
        def fname_of(p):
            return "card_event_%s_C_99" % p

    class _Km:
        @staticmethod
        def names_pool():
            return _Pool()

    class _S2(_Sess):
        def _kardsmem(self):
            return _Km

    class _WNoFc(W.WhatIf):
        def _build(self, st):
            s = sim()
            s.forecast = {}                                         # 规则侧没给表
            self._ensure_forecast(s)
            return s
    oldp, oldb = FP.predict_ptrs, FP.read_weather_buckets
    FP.read_weather_buckets = lambda km: {}
    FP.predict_ptrs = lambda seed, b: {w: {"light": "%s_l" % w, "medium": "%s_m" % w, "heavy": "%s_h" % w} for w in FP.WEATHERS}
    try:
        w = _WNoFc(_S2())
        w._seed = lambda: 7
        r = w.run_line("play h4")[0]
        chk("sim.forecast 空时自己补：预报牌照样挂起", r.ok and r.choices and r.choices["need"] == "forecast", str((r.error, r.choices)))
        r = w.run_line("forecast")[0]
        labs = [o[1] for o in (r.choices or {}).get("options", [])]
        chk("补出来的表：forecast 走一步列出 9 张（FName 去掉 _C）", r.ok and len(labs) == 9
            and "card_event_sunny_l" in labs and "card_event_storm_h" in labs and not any("_C" in x for x in labs), str(labs))
        r = w.run_line("spawn card_event_sunny_l")[0]
        chk("再从这 9 张里选一张（下回合进手牌）", r.ok and r.choices.get("done") and r.choices.get("card") == "card_event_sunny_l",
            str((r.error, r.choices)))
        w = _WNoFc(_S2())
        w._seed = lambda: 7
        w.run_line("play h4; spawn sunny")
        r = w.run_line("spawn light")[0]
        chk("补出来的表也能一次一层地选完两层（两次 spawn）", r.ok and r.choices.get("done")
            and r.choices.get("card") == "card_event_sunny_l", str((r.error, r.choices)))
        w2 = _WNoFc(_S2())
        w2._seed = lambda: None
        r = w2.run_line("play h4")[0]
        chk("读不到种子：不补，预报牌直接结算（效果里留着 forecast 标记，不假装挂起）", r.ok and not r.choices and r.eff == {"forecast": True}, str((r.error, r.choices, r.eff)))
        from gui import whatif_panel as _P
        chk("预报标记在效果里写成 forecast，不是“forecast: True”", "forecast: True" not in _P.format_result(r)
            and "  forecast" in _P.format_result(r), _P.format_result(r))
    finally:
        FP.predict_ptrs, FP.read_weather_buckets = oldp, oldb

    # ---- 敌方手牌：随机弃牌之类的效果会真的从敌方手牌里去掉 ----
    w = _W(_Sess())
    r = w.run_line("play h6")[0]
    eh = [l for l in w.view()[0].splitlines() if l.startswith("ENEMY HAND")][0]
    chk("敌方弃牌：差异里写出弃了哪张，敌方手牌里去掉它", r.ok and any(x.startswith("enemy hand -") and "E-HAND-A" in x for x in r.diff)
        and "E-HAND-A" not in eh and "E-HAND-B" in eh, str((r.diff, eh)))

    # ---- 文字 ----
    from gui import whatif_panel as P
    w = _W(_Sess())
    txt = P.format_result(w.run_line("play h1")[0])
    chk("format_result：effect 段 = 扣指挥点/手牌离手（带短号，同名不同张能区分）+ 卡牌效果；result 段只留精简后的净变化",
        txt.startswith("> play h1") and "effect:" in txt and "damage 2 -> target" in txt
        and "kredit" in txt and "hand -h1 H-DAMAGE" in txt and "result:" in txt
        and "slot 3 -> 2" in txt, txt)
    t2 = P.format_result(w.run_line("play h2")[0])
    chk("format_result：抉择缺 choose ⇒ 列出每个选项的效果", "✘" in t2 and "0) damage 3 -> target" in t2 and "1) draw 1" in t2, t2)
    t3 = P.format_result(w.run_line("play h3")[0])
    chk("format_result：三选一 ⇒ 候选 + spawn 提示", "spawn <card>" in t3 and "- card_a" in t3 and "- card_b" in t3, t3)
    w.run_line("undo")
    t4 = P.format_result(w.run_line("play h4")[0])
    chk("format_result：预报第一层 ⇒ 只列 3 张类型卡，不提前列 9 张",
        "pending: forecast" in t4 and "card_event_sunny1_blue_sky" in t4 and "card_event_rain1_mist" in t4
        and "card_event_storm1_gale" in t4 and "sunny_l" not in t4, t4)
    t5 = P.format_result(w.run_line("forecast")[0])
    chk("format_result：forecast 一步 ⇒ 列出 9 张候选、等 spawn 选一张",
        "pending: forecast" in t5 and "9 张候选" in t5 and "sunny_l" in t5 and "storm_h" in t5, t5)
    t6 = P.format_result(w.run_line("spawn sunny_h")[0])
    chk("format_result：真的选完 ⇒ 写出选中的牌（下回合进手牌）", "picked" in t6 and "sunny_h" in t6, t6)
    chk("format_result：undo 只回显", P.format_result(w.run_line("undo")[0]).startswith("> undo"))

    # ---- 文本可复制 / 历史光标 ----
    from gui import textutil as TU
    chk("只读 Text：放行方向键与 Ctrl+C/A，拦掉键入/粘贴/删除",
        TU.ro_key("Left", 0) is None and TU.ro_key("c", 0x4) is None and TU.ro_key("a", 0x4) is None
        and TU.ro_key("x", 0) == "break" and TU.ro_key("v", 0x4) == "break" and TU.ro_key("BackSpace", 0) == "break"
        and TU.ro_key("Delete", 0) == "break" and TU.ro_key("Return", 0) == "break")
    # ---- 面板 ----
    os.environ["KARDS_GUI_SELFTEST_EXIT_MS"] = "150"
    try:
        app = P.App(None)
        app.panel.show_board("盘面…")
        app.panel._append(txt)
        pn = app.panel
        chk("只读 Text 保持 normal（能聚焦、能选、能复制）", str(pn.board.cget("state")) == "normal" and str(pn.out.cget("state")) == "normal")
        pn.board.tag_add("sel", "1.0", "1.4")
        chk("选中的文字能取出来（复制的前提）", pn.board.get("sel.first", "sel.last") == "盘面…"[:3])
        pn.show_board("盘面…")                                   # 文字没变：不重写，选区还在
        chk("盘面文字没变时不重写（选区不被轮询冲掉）", bool(pn.board.tag_ranges("sel")))
        pn.history[:] = ["play h1", "attack m1 ehq"]
        pn._hi = 2
        r1 = pn._hist(-1)
        chk("↑：取上一条，光标在行尾，并拦掉默认行为", pn.cmd.get() == "attack m1 ehq" and pn.entry.index("insert") == len("attack m1 ehq") and r1 == "break")
        pn._hist(-1)
        chk("再↑：更早一条，光标仍在行尾", pn.cmd.get() == "play h1" and pn.entry.index("insert") == len("play h1"))
        pn._hist(1)
        pn._hist(1)
        chk("↓到底：清空，光标在 0", pn.cmd.get() == "" and pn.entry.index("insert") == 0)
        chk("面板：盘面与结果都画出来", "盘面" in app.panel.board.get("1.0", "end") and "damage 2" in app.panel.out.get("1.0", "end"))
        app._close()
    finally:
        os.environ.pop("KARDS_GUI_SELFTEST_EXIT_MS", None)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
