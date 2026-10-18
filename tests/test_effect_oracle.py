#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`gui.effect_text`（效果摘要 → 中文）与 `gui.effect_oracle`（手牌效果预览窗）的离线检查：假数据源，不连游戏。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gui import effect_text as ET                                    # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


def main():
    fn = lambda n: n                                                 # noqa: E731

    s = ET.describe({"damage": 3, "draw": 2, "kredit": 1}, fn)
    chk("基本键写成指令", set(s) == {"damage 3 -> target", "kredit +1", "draw 2"}, str(s))
    chk("行序 = 效果摘要里键的出现顺序（脚本顺序），不重排",
        ET.describe({"slot": -1, "damage": 2}, fn) == ["slot -1", "damage 2 -> target"]
        and ET.describe({"damage": 2, "slot": -1}, fn) == ["damage 2 -> target", "slot -1"])
    chk("draw：给了真牌解析就写真牌，解析不出退回 draw N",
        ET.describe({"draw": 2}, fn, lambda e: "card_a, card_b") == ["draw card_a, card_b"]
        and ET.describe({"draw": 2}, fn, lambda e: None) == ["draw 2"])

    s = ET.describe({"pin": True, "give": ["ambush", "guard"], "buff": (1, 2)}, fn)
    chk("pin / give keyword / buff 的指令形态", s == ["pin -> target", "give keyword [ambush, guard] -> target", "buff +1/+2 -> target"], str(s))

    s = ET.describe({"spawn_cards": [{"name": "card_unit_x", "row": "frontline", "mine": True}], "spawn": 1}, fn)
    chk("spawn 写出卡名与落点（不再列泛泛的计数）", s == ["spawn card_unit_x @ own frontline"], str(s))

    s = ET.describe({"gain_names": ["card_unit_x"], "gain_cards": 1}, fn)
    chk("gain card 写出卡名", s == ["gain card card_unit_x"], str(s))

    s = ET.describe({"damage": 2, "weird_new_key": 7}, fn)
    chk("不认得的键原样列出，不吞", "weird_new_key: 7" in s and "damage 2 -> target" in s, str(s))

    s = ET.describe({"damage": 1, "uncertain": {"draw": 1}}, fn)
    chk("被随机污染的效果单独标 maybe", "maybe: draw 1" in s, str(s))

    s = ET.describe({"outcomes": [(0.5, {"damage": 1}), (0.5, {"draw": 1})]}, fn)
    chk("多分支带概率", s[0] == "random outcomes:" and any("50%" in x and "damage 1" in x for x in s) and len(s) == 3, str(s))
    s = ET.describe({"outcomes": [(0.5, {"damage": 1}), (0.5, {"draw": 1})], "outcomes_mode": "max"}, fn)
    chk("抉择：choose one of", s[0] == "choose one of:" and not any("%" in x for x in s), str(s))

    chk("空效果 / None", ET.describe({}, fn) == ["(no on-play effect)"] and ET.describe(None, fn) == ["(effect unavailable)"])
    chk("内部 id 列表/句柄键不展示", ET.describe({"damage_ids": [3], "gain_tmp": [9], "draw": 1}, fn) == ["draw 1"])
    chk("不同效果给出不同文本", ET.one_line({"damage": 1}) != ET.one_line({"damage": 2}))

    chk("damage_ids：逐张写出目标（随机落到哪一张就写哪一张）",
        ET.describe({"damage_ids": {41: 5}}, fn, None, lambda c: "card_unit_x#%d" % c) == ["damage 5 -> card_unit_x#41"])
    # 数据源：三选一 / 预报（monkeypatch 预测，不连游戏）
    from gui import hand_preview as HP
    from semantics import choosespawn as CS
    chk("_has 认得顶层与分支里的标记", HP._has({"choose_spawn": True}, "choose_spawn")
        and HP._has({"outcomes": [(1.0, {"choose_spawn": True})]}, "choose_spawn") and not HP._has({"damage": 1}, "choose_spawn"))
    src = HP.LiveSource()
    chk("名字就是 FName 本身（不查标题、不翻译）", src._names(None)("card_unit_foo") == "card_unit_foo")
    old = CS.predict
    CS.predict = lambda km, ptr, seed, st=None: {"names": ["card_unit_foo", "card_unit_bar", "card_unit_foo"], "pool": 99,
                                                  "stopped": None}
    try:
        ls = src._choose_spawn(None, None, 1, 7, src._names(None))
        chk("三选一：一行只列三张 FName，没有别的信息",
            ls == ["choose_spawn 1 of (card_unit_foo, card_unit_bar, card_unit_foo)"], str(ls))
        chk("读不到种子：如实标 unreadable", "unreadable" in src._choose_spawn(None, None, 1, None, src._names(None))[0])
        CS.predict = lambda km, ptr, seed, st=None: {"names": [], "pool": 0, "stopped": "池子读不出"}
        chk("预测停住：写出原因，不编候选", "池子读不出" in src._choose_spawn(None, None, 1, 7, src._names(None))[0])
    finally:
        CS.predict = old
    from kardsmem import forecast_pool as FP

    class _Pool:
        @staticmethod
        def fname_of(p):
            return "card_event_%s_C_123" % p

    class _Km:
        @staticmethod
        def names_pool():
            return _Pool()
    oldp, oldb = FP.predict_ptrs, FP.read_weather_buckets
    FP.read_weather_buckets = lambda km: {}
    FP.predict_ptrs = lambda seed, b: {w: {"light": "a", "medium": "b", "heavy": "c", "draws": 5} for w in FP.WEATHERS}
    try:
        fl = src._forecast(_Km, 7, src._names(None))
        chk("预报：9 张候选，按天气分行", fl[0] == "forecast from (9 cards)" and len(fl) == 1 + len(FP.WEATHERS)
            and fl[1] == "  sunny: card_event_a, card_event_b, card_event_c", str(fl))
    finally:
        FP.predict_ptrs, FP.read_weather_buckets = oldp, oldb
    chk("预报读不到种子：如实标 unreadable", "unreadable" in src._forecast(None, None, src._names(None))[0])

    # 抉择牌：VM 报 choice ⇒ 写 choose one of（玩家选），不写 random / 概率；没有 choice 的多分支才是随机
    import semantics.effectvm as EVM
    import player.rule as PR
    old_en, old_ph = EVM.enumerate_effects, PR.RuleV2._play_hooks

    class _C:
        obj = type("O", (), {"CardID": 7})()
        name = "SOME CHOICE"
        raw = {"ptr": 1}
    class _St2:
        cards = []

    class _Km2:
        @staticmethod
        def names_pool():
            return type("P", (), {"fname_of": staticmethod(lambda p: "")})()
    outs = [(0.5, {"damage": 2}), (0.5, {"draw": 1})]
    PR.RuleV2._play_hooks = staticmethod(lambda *a, **k: {})
    src2 = HP.LiveSource()
    try:
        EVM.enumerate_effects = lambda *a, **k: {"outcomes": outs, "complete": True, "stopped": None, "choice": True}
        ln = src2._row(_Km2, _St2, 1, 5, _C())["lines"]
        chk("抉择牌：choose one of，没有 random、没有概率", ln[0] == "choose one of:" and not any("random" in x or "%" in x for x in ln), str(ln))
        EVM.enumerate_effects = lambda *a, **k: {"outcomes": outs, "complete": True, "stopped": None}
        ln = src2._row(_Km2, _St2, 1, 5, _C())["lines"]
        EVM.enumerate_effects = lambda *a, **k: {"outcomes": [(1.0, {})], "complete": True, "stopped": None}
        ln = src2._row(_Km2, _St2, 1, 5, _C())["lines"]
        chk("跑完但没有可作用的对象：写“当前盘面上什么也不发生”，不写 unavailable",
            len(ln) == 1 and "nothing happens" in ln[0] and "unavailable" not in ln[0], str(ln))
        EVM.enumerate_effects = lambda *a, **k: {"outcomes": [], "complete": False, "stopped": "boom"}
        ln = src2._row(_Km2, _St2, 1, 5, _C())["lines"]
        chk("VM 真停住了才写 unavailable", ln == ["(effect unavailable)"], str(ln))
        EVM.enumerate_effects = lambda *a, **k: {"outcomes": outs, "complete": True, "stopped": None}
        ln = src2._row(_Km2, _St2, 1, 5, _C())["lines"]
        chk("没有 choice 标记的多分支才写 random outcomes", ln[0] == "random outcomes:", str(ln))
    finally:
        EVM.enumerate_effects, PR.RuleV2._play_hooks = old_en, old_ph

    # 无头建窗：假源喂一次结果，表格与详情真画出来
    os.environ["KARDS_GUI_SELFTEST_EXIT_MS"] = "150"
    try:
        from gui import effect_oracle as O
        app = O.App(None)
        app.panel.show({"ok": True, "why": "", "seed": 5, "rows": [
            {"id": 1, "name": "URAL FACTORIES", "lines": ["spawn TEST INFANTRY @ own frontline"], "gaps": ""},
            {"id": 2, "name": "SOME EVENT", "lines": ["(effect unavailable)"], "gaps": "VM 停在：未实现的原语"}]})
        items = app.panel.tree.get_children()
        chk("窗口列出每张手牌与其效果", len(items) == 2 and "TEST INFANTRY" in app.panel.tree.item(items[0], "values")[1])
        app.panel.tree.selection_set(items[1])
        app.panel._show_detail()
        det = app.panel.detail.get("1.0", "end")
        chk("选中后详情显示缺口", "缺口：VM 停在" in det, det)
        app.panel.show({"ok": False, "why": "未连接游戏（x）", "seed": None, "rows": []})
        chk("读不到游戏时状态栏如实显示、表格清空", "未连接游戏" in app.panel.status.cget("text") and not app.panel.tree.get_children())
        app._close()
        from gui import app as MAIN
        main = MAIN.App()
        tabs = [main.nametowidget(t) for t in main.fx_panel.master.tabs()]
        chk("主面板里有“手牌效果预览”页签", main.fx_panel in tabs
            and main.fx_panel.master.tab(main.fx_panel, "text") == "手牌效果预览")
        main.destroy()
    finally:
        os.environ.pop("KARDS_GUI_SELFTEST_EXIT_MS", None)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
