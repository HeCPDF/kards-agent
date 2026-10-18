#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘面模拟的点选界面：`player.whatif.board_model`（结构化盘面）与 `gui.whatif_board`（点选状态机、规则文本、控件）。
离线：复用 test_whatif 的假会话与手搭 `Sim`，不连游戏。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import test_whatif as T                                              # noqa: E402
from gui import whatif_board as B                                    # noqa: E402
from player import whatif as W                                       # noqa: E402

bad = 0


def chk(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))


def toks(m, zone):
    return [t.token for t in m.zones[zone]]


def main():
    # ---- 结构化盘面（实时） ----
    w = T._W(T._Sess())
    m = w.board_model()
    chk("model：五个区域齐全", set(m.zones) == {"enemy_hand", "enemy_support", "front", "my_support", "my_hand"})
    chk("model：我方手牌 h1.. 带费用，单位/指令分开", toks(m, "my_hand")[:2] == ["h1", "h2"]
        and {t.name: t.kind for t in m.zones["my_hand"]}["H-UNIT"] == "unit" and {t.name: t.kind for t in m.zones["my_hand"]}["H-DAMAGE"] == "order"
        and all(t.cost is not None for t in m.zones["my_hand"]), str(m.zones["my_hand"]))
    chk("model：总部在各自支援线最左，敌方手牌可见", toks(m, "my_support")[0] == "hq" and toks(m, "enemy_support")[0] == "ehq"
        and len(m.zones["enemy_hand"]) == 2, str((toks(m, "my_support"), toks(m, "enemy_support"))))
    chk("model：前线敌方在前、带攻防（与文本盘面同格式）", m.zones["front"][0].mine is False and m.zones["front"][0].atk == "2/2")
    chk("_addr：0x 地址 → 整数，其他 → None", W._addr("0x1F") == 31 and W._addr("t10") is None and W._addr(None) is None)
    chk("model：布局与文本盘面同一份数据（数量一致）", len(m.zones["my_hand"]) == len([x for x in w.view()[0].splitlines() if x.startswith("MY HAND")][0].split("|")))

    # ---- 结构化盘面（模拟后）----
    w.run_line("play h1")
    m2 = w.board_model()
    chk("model：模拟后手牌少一张，depth=1", m2.depth == 1 and "h1" not in toks(m2, "my_hand") and len(m2.zones["my_hand"]) == len(m.zones["my_hand"]) - 1)
    w = T._W(T._Sess())
    w.run_line("play h3")
    m3 = w.board_model()
    chk("model：三选一挂起 ⇒ pending=spawn，候选卡块按下标", m3.pending and m3.pending["need"] == "spawn" and [t.token for t in m3.pending_tiles] == ["0", "1"]
        and [t.name for t in m3.pending_tiles] == ["card_a", "card_b"], str(m3.pending_tiles))
    w = T._W(T._Sess())
    w.run_line("deploy h5")
    chk("model：部署后新单位 n1 在我方支援线", "n1" in toks(w.board_model(), "my_support"), str(toks(w.board_model(), "my_support")))

    # ---- 规则文本 ----
    chk("clean_text：去掉富文本标签、空 ⇒ None", B.clean_text("<kw>Blitz</>. Deal 2") == "Blitz. Deal 2" and B.clean_text("  ") is None and B.clean_text(None) is None)

    class Src(B.TextSource):
        def uid_text(self, uid):
            return "text-%d" % uid

        def name_text(self, name):
            return "by-name-%s" % name if name == "card_a" else None
    mm = T._W(T._Sess())
    mm.run_line("play h3")
    model = mm.board_model()
    for z in model.zones.values():
        for k, t in enumerate(z):
            t.uid = 100 + k
    Src(None).fill(model)
    chk("TextSource.fill：有 uid 按 uid，候选牌按名字，找不到留 None",
        all(t.text for t in model.zones["my_hand"]) and model.pending_tiles[0].text == "by-name-card_a" and model.pending_tiles[1].text is None)

    # ---- 点选状态机：牌 / 位置 / 目标，三项都能点选、再点取消；命令由“提交”拼出 ----
    w = T._W(T._Sess())
    m = w.board_model()
    P = B.Picker()
    by = {t.name: t for z in m.zones.values() for t in z}
    hand = {t.name: t for t in m.zones["my_hand"]}
    enemy = m.zones["front"][0]
    chk("点敌方牌：不选中", P.click(enemy, m) is None and P.card is None)
    chk("点手牌：只选中，不发命令", P.click(hand["H-DAMAGE"], m) is None and P.card.token == hand["H-DAMAGE"].token and P.command() == "play " + hand["H-DAMAGE"].token)
    chk("再点同一张：取消", P.click(hand["H-DAMAGE"], m) is None and P.card is None and P.command() is None)
    P.click(hand["H-DAMAGE"], m)
    P.opts = {"legal": True, "no_target": False, "targets": {enemy.token}}
    chk("要目标的牌：没选目标 ⇒ 不能提交（不再有无目标兜底）", P.needs_target() is True and "目标" in P.missing() and P.submit() is None)
    P.click(enemy, m)
    chk("点目标：命令带 with；再点目标取消", P.command() == "play %s with %s" % (hand["H-DAMAGE"].token, enemy.token) and P.missing() == "")
    P.click(enemy, m)
    chk("再点目标：目标取消，又不能提交", P.target is None and P.missing() != "")
    P.click(enemy, m)
    chk("提交：发出命令并清空选择，last_cast 记下", P.submit() == "play %s with %s" % (hand["H-DAMAGE"].token, enemy.token) and P.card is None
        and P.last_cast.startswith("play "))
    P.click(hand["H-DAMAGE"], m)
    P.opts = {"legal": False, "why": "候选池为空", "no_target": True, "targets": set()}
    chk("合法走法里没有这张牌 ⇒ 提交不了，写出原因", "候选池为空" in P.missing() and P.submit() is None)
    P.opts = {"legal": True, "no_target": True, "targets": set()}
    P.click(enemy, m)
    chk("不要目标的牌：点场上不生效，命令不带 with，可直接提交", P.target is None and P.needs_target() is False and P.missing() == ""
        and P.command() == "play " + hand["H-DAMAGE"].token)
    P.clear()
    P.click(hand["H-UNIT"], m)
    P.toggle_slot(2, 5)
    chk("单位牌 + 位置 ⇒ deploy <牌> <slot>", P.command() == "deploy %s 2" % hand["H-UNIT"].token)
    P.toggle_slot(2, 5)
    chk("再点同一个位置：取消", P.slot is None and P.command() == "deploy " + hand["H-UNIT"].token)
    P.toggle_slot(3, 5)
    P.click(hand["H-DAMAGE"], m)
    chk("换牌：位置与目标清掉", P.card.name == "H-DAMAGE" and P.slot is None and P.target is None)
    P.clear()
    mine = by["MY-FRONT"]
    P.click(mine, m)
    chk("点我方场上单位：选中；没目标 ⇒ front <单位>", P.card.token == mine.token and P.command() == "front " + mine.token)
    P.toggle_slot(1, 2)
    chk("上线位置 ⇒ front <单位> <k>", P.command() == "front %s 1" % mine.token)
    P.click(enemy, m)
    chk("再点敌方 ⇒ attack <单位> with <目标>", P.command() == "attack %s with %s" % (mine.token, enemy.token))
    P.click(m.zones["enemy_support"][0], m)
    chk("改点敌方总部 ⇒ 目标换成 ehq", P.command() == "attack %s with ehq" % mine.token)
    P.click(by["MY-BACK"], m)
    chk("单位选中时点别的我方单位 ⇒ 换成它", P.card.name == "MY-BACK" and P.target is None)
    w = T._W(T._Sess())
    w.run_line("play h3")
    mp = w.board_model()
    chk("挂起三选一：点手牌/场上都不动选择", B.Picker().click(mp.zones["my_hand"][0], mp) is None)
    mp.pending = {"need": "select"}
    chk("挂起 select：点手牌 ⇒ select <牌>", B.Picker().click(mp.zones["my_hand"][0], mp) == "select " + mp.zones["my_hand"][0].token)

    # ---- 控件（无头）----
    from gui import whatif_panel as WP
    os.environ["KARDS_GUI_SELFTEST_EXIT_MS"] = "60000"             # 无头：App 自己 withdraw，不弹窗
    root = WP.App(None)
    try:
        sent, asked = [], []
        bv = B.BoardView(root, sent.append, asked.append)
        bv.pack(fill="both", expand=True)
        w = T._W(T._Sess())
        m = w.board_model()
        for z in m.zones.values():
            for k, t in enumerate(z):
                t.uid = 100 + k
        B.TextSource.fill(type("S", (), {"uid_text": lambda s, u: "rules-%d" % u, "name_text": lambda s, n: None})(), m)
        bv.show(m)
        root.update_idletasks()
        chk("BoardView：每个区域的卡块数 = 模型", all(len(bv.zones[z].items) == len(m.zones[z]) for z in m.zones))
        tv = next(t for t in bv.zones["my_hand"].items if t.tile.name == "H-DAMAGE")
        chk("卡块显示规则文本", any("rules-" in lb.cget("text") for lb in tv.labels))
        items = list(bv.zones["my_hand"].items)
        bv.show(m)
        chk("盘面没变：不重画（卡块对象还是原来的）", bv.zones["my_hand"].items == items)

        # 实时盘面在模拟后变了：顶部（undo/reset 旁）要给提示 + 一个 reset（用户 2026-10-19）
        m.stale = True
        bv.show(m, True)
        root.update_idletasks()
        texts = [str(x.cget("text")) for x in bv.btns.winfo_children()]
        chk("实时盘面变了：undo/reset 旁有提示 + reset 按钮", any("建议 reset" in t for t in texts)
            and any(t.startswith("reset（盘面已变）") for t in texts), str(texts))
        sent.clear()
        [x for x in bv.btns.winfo_children() if str(x.cget("text")).startswith("reset（盘面已变）")][0].invoke()
        chk("点提示旁的 reset ⇒ 发 reset 命令", sent == ["reset"], str(sent))
        m.stale = False
        bv.show(m, True)
        root.update_idletasks()
        sent.clear()
        bv._on_tile(tv.tile)
        chk("点手牌：选中、不发命令，并去问 card_options", bv.picker.card is not None and not sent and asked == [tv.tile.token])
        bv.set_options(tv.tile.token, {"legal": True, "no_target": False, "targets": {m.zones["front"][0].token}})
        root.update_idletasks()
        sub = [c for c in bv.btns.winfo_children() if c.winfo_class() == "TButton" and c.cget("text").startswith("提交")][0]
        chk("要目标 ⇒ 提交按钮灰着；合法目标被高亮", "disabled" in sub.state() and any(
            x.tile.token == m.zones["front"][0].token and x.cget("bg") == B.COL_AIM for x in bv.zones["front"].items))
        bv._on_tile(m.zones["front"][0])
        root.update_idletasks()
        sub = [c for c in bv.btns.winfo_children() if c.winfo_class() == "TButton" and c.cget("text").startswith("提交")][0]
        chk("选了目标 ⇒ 提交按钮亮，按钮上写着命令", "disabled" not in sub.state() and sub.cget("text") == "提交：play %s with %s" % (tv.tile.token, m.zones["front"][0].token), sub.cget("text"))
        sub.invoke()
        chk("提交 ⇒ 发出命令，选择清空", sent == ["play %s with %s" % (tv.tile.token, m.zones["front"][0].token)] and bv.picker.card is None, str(sent))
        # 部署位置：＋ 点选、再点取消；提交才发
        sent.clear()
        bv._on_tile(next(t for t in m.zones["my_hand"] if t.kind == "unit"))
        root.update_idletasks()
        sup = bv.zones["my_support"]
        n_units = len(sup.items)                                   # 总部也算一格
        chk("部署位置：单位牌选中后，支援线有 n+1 个＋（可插在总部左边）", len(sup._gaps) == n_units + 1 and all(g.cget("text") == "＋" for _i, g in sup._gaps)
            and not bv.zones["front"]._gaps, str(len(sup._gaps)))
        chk("＋按钮窄、和卡块排在同一行", sup._gaps[0][1].winfo_reqwidth() < B.TILE_W // 2)
        sup._gaps[0][1].invoke()
        chk("点＋ ⇒ 只记位置，不发命令", bv.picker.slot == 1 and not sent)
        sup._gaps[0][1].invoke()
        chk("再点同一个＋ ⇒ 取消位置", bv.picker.slot is None)
        sup._gaps[0][1].invoke()
        bv.set_options(bv.picker.card.token, {"legal": True, "no_target": True, "targets": set()})
        sub = [c for c in bv.btns.winfo_children() if c.winfo_class() == "TButton" and c.cget("text").startswith("提交")][0]
        sub.invoke()
        chk("不要目标的单位牌：位置 1 + 提交 ⇒ deploy <牌> 1，位置清掉、＋消失", sent and sent[0].endswith(" 1") and sent[0].startswith("deploy")
            and bv.picker.slot is None and not sup._gaps, str(sent))
        # 上线位置
        sent.clear()
        mine_sup = next(t for t in m.zones["my_support"] if t.kind != "hq")
        bv._on_tile(mine_sup)
        root.update_idletasks()
        fr = bv.zones["front"]
        n_mine = len([tv_ for tv_ in fr.items if tv_.tile.mine])
        chk("上线位置：前线我方单位前后共 m+1 个＋", len(fr._gaps) == n_mine + 1, str(len(fr._gaps)))
        fr._gaps[0][1].invoke()
        sub = [c for c in bv.btns.winfo_children() if c.winfo_class() == "TButton" and c.cget("text").startswith("提交")][0]
        sub.invoke()
        chk("点第 1 个＋再提交 ⇒ front <单位> 1", sent == ["front %s 1" % mine_sup.token], str(sent))
        bv.picker.clear()
        bv._refresh()
        sent.clear()
        # 抉择：缺 choose ⇒ 按钮
        w2 = T._W(T._Sess())
        res = w2.run_line("play h2")
        bv.picker.last_cast = "play h2"
        bv.on_results(res)
        root.update_idletasks()
        btns = [b_ for b_ in bv.choice_box.items]
        chk("抉择牌：为每个选项做一个按钮", len(btns) == 2 and "damage 3" in btns[0].cget("text") and "draw 1" in btns[1].cget("text"), str([b_.cget("text") for b_ in btns]))
        btns[1].invoke()
        chk("点选项 ⇒ 同一条命令加 choose <i>", sent == ["play h2 choose 1"], str(sent))
        # 三选一：候选卡块 + 选这张
        w3 = T._W(T._Sess())
        w3.run_line("play h3")
        m3 = w3.board_model()
        B.TextSource.fill(type("S", (), {"uid_text": lambda s, u: None, "name_text": lambda s, n: "text of " + n})(), m3)
        sent.clear()
        bv._choice = None
        bv.show(m3)
        root.update_idletasks()
        cand = bv.choice_box.items
        chk("三选一：两个候选卡块，带名字与规则文本", len(cand) == 2 and any("card_a" in lb.cget("text") for lb in cand[0].labels)
            and any("text of card_b" in lb.cget("text") for lb in cand[1].labels))
        btn = [c for c in cand[1].winfo_children() if c.winfo_class() == "TButton"][0]
        btn.invoke()
        chk("点“选这张” ⇒ spawn <下标>", sent == ["spawn 1"], str(sent))
        # 预报：第一层 3 个天气卡块 + 第 4 个选项“运行 forecast”（走一步、可 undo）
        S = type("S", (), {"uid_text": lambda s, u: None, "name_text": lambda s, n: "text of " + n})
        w4 = T._W(T._Sess())
        w4.run_line("play h4")
        m4 = w4.board_model()
        B.TextSource.fill(S(), m4)
        sent.clear()
        bv._choice = None
        bv.show(m4)
        root.update_idletasks()
        items = bv.choice_box.items
        chk("预报第一层：3 张类型卡（蓝天/薄雾/狂风）的卡块 + 第 4 项『运行 forecast』", m4.pending["need"] == "forecast" and len(items) == 4
            and len(m4.pending_tiles) == 3
            and [tv.tile.token for tv in items[:3]] == ["card_event_sunny1_blue_sky", "card_event_rain1_mist", "card_event_storm1_gale"]
            and "运行 forecast" in items[3].cget("text"),
            str([getattr(tv, "tile", None) and (tv.tile.token, tv.tile.name) or tv.cget("text") for tv in items]))
        chk("预报第一层卡块只写类型卡名，不提前写预测的 9 张", [tv.tile.name for tv in items[:3]]
            == ["card_event_sunny1_blue_sky", "card_event_rain1_mist", "card_event_storm1_gale"],
            str([tv.tile.name for tv in items[:3]]))
        [c for c in items[0].winfo_children() if c.winfo_class() == "TButton"][0].invoke()
        chk("点类型卡块的『选这种天气』 ⇒ spawn <类型卡>", sent == ["spawn card_event_sunny1_blue_sky"], str(sent))
        sent.clear()
        items[3].invoke()
        chk("点第 4 项 ⇒ forecast（一步模拟）", sent == ["forecast"], str(sent))
        # forecast 之后：候选变成牌，9 张卡块，各带“选这张” ⇒ spawn <key>
        w4.run_line("forecast")
        m5 = w4.board_model()
        B.TextSource.fill(S(), m5)
        bv.show(m5)
        root.update_idletasks()
        cand = bv.choice_box.items
        chk("forecast 之后：9 张候选卡块，各带『选这张』", len(cand) == 9 and (m5.pending.get("meta") or {}).get("merged")
            and all(any(c.winfo_class() == "TButton" for c in tv.winfo_children()) for tv in cand),
            str(len(cand)))
        sent.clear()
        [c for c in cand[2].winfo_children() if c.winfo_class() == "TButton"][0].invoke()
        chk("点第 3 张『选这张』 ⇒ spawn sunny/heavy", sent == ["spawn sunny/heavy"], str(sent))
        # 第二层（先 spawn 天气）：这一种天气的 3 张，各带“选这张” ⇒ spawn <档>
        w5 = T._W(T._Sess())
        w5.run_line("play h4; spawn sunny")
        bv.show(w5.board_model())
        root.update_idletasks()
        cand = bv.choice_box.items
        chk("预报第二层：3 张卡块，各带『选这张』", len(cand) == 3 and all(
            any(c.winfo_class() == "TButton" for c in tv.winfo_children()) for tv in cand), str(len(cand)))
        sent.clear()
        [c for c in cand[2].winfo_children() if c.winfo_class() == "TButton"][0].invoke()
        chk("点第二层『选这张』 ⇒ spawn heavy", sent == ["spawn heavy"], str(sent))
        # 窄排版：窄窗口里一行放不下就换行
        fl = bv.zones["my_hand"]
        fl._layout(2 * (B.TILE_W + 8), force=True)
        rows = {int(w_.place_info()["y"]) for w_ in fl.items}
        chk("排版：两列宽时手牌自动换行", len(fl.items) > 2 and len(rows) >= 2, str(rows))
    finally:
        root.destroy()
        os.environ.pop("KARDS_GUI_SELFTEST_EXIT_MS", None)

    # ---- 嵌入面板 ----
    os.environ["KARDS_GUI_SELFTEST_EXIT_MS"] = "150"
    try:
        app = WP.App(None)
        chk("面板：有“点选”页签，点选发的命令走 run_text", hasattr(app.panel, "pick") and app.panel.pick.run_cb == app.panel.run_text)
        app._close()
    finally:
        os.environ.pop("KARDS_GUI_SELFTEST_EXIT_MS", None)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main() or T.bad)
