#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 第一族（卡面数值改动）的离线回归：`engine/natives/stats.py` 的 `EChangeType` 语义。

钉住三件事：
  ① 枚举值与原版 `EChangeType`（SDK dump）逐个相同 —— 值错了整族都错；
  ② `ChangeAttack`（`BP_CardFunctions.cpp:10981-11212`）分支表读出来的性质：哪些是设值、哪些夹取、
     哪些写独立 buff 字段、哪些才发 after 事件；
  ③ 接线是真的：`effectvm` 的 SetValue 判定、`sim.engine` 的绝对值夹取都走这一族（不是各写一份）。

不碰真进程、不需要游戏。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cards import ME, OPP                                                     # noqa: E402
from engine.natives import stats as S                                          # noqa: E402
from engine.state import H, Sim, U                                             # noqa: E402
import semantics.effectvm as EV                                                # noqa: E402
from engine.adapter import from_cards                                          # noqa: E402
from sim.engine import _apply_eff                                              # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    # ① 枚举值（原版：SDK dump `enum class EChangeType : uint8`）---------------------------
    want = {0: "tempBuffGive", 1: "permBuff", 2: "SetValue", 3: "Suppress", 4: "tempBuffRemove",
            5: "veteranSet", 6: "customAdd", 7: "customRemove", 8: "combatModify", 9: "notUsed"}
    chk("EChangeType 十个值与 SDK dump 逐个相同", S.NAMES == want, str(S.NAMES))
    chk("枚举常量导出与 NAMES 一致",
        (S.TEMP_BUFF_GIVE, S.PERM_BUFF, S.SET_VALUE, S.SUPPRESS, S.TEMP_BUFF_REMOVE, S.VETERAN_SET,
         S.CUSTOM_ADD, S.CUSTOM_REMOVE, S.COMBAT_MODIFY, S.NOT_USED) == tuple(range(10)))

    # ② 夹取（原版：clamp 写法 ((v<0)?0:((v>99)?99:v))，BP_CardFunctions.cpp:11081/11087/11120）---
    chk("clamp_stat：负数→0、0→0、99→99、150→99", [S.clamp_stat(v) for v in (-5, 0, 99, 150)] == [0, 0, 99, 99])
    chk("clamp_stat：边界 1/98 原样、float 会被取整", [S.clamp_stat(v) for v in (1, 98, 3.7)] == [1, 98, 3])
    chk("clamp_armor 是独立的一套 [0,3]（getTotalHeavyArmor）",
        [S.clamp_armor(v) for v in (-1, 0, 3, 9)] == [0, 0, 3, 3])

    # ② 分支表：设值家族 / 写 buff 字段 / 夹取 / after 事件 --------------------------------
    chk("is_set_value：只有 2/3/5 是「设成 n」（都 goto Label_855）",
        [S.is_set_value(v) for v in range(10)] == [False, False, True, True, False, True, False, False, False, False])
    chk("is_set_value：读不出（None/缺参）当加减处理（旧行为不变）",
        S.is_set_value(None) is False and S.is_set_value("") is False)
    chk("writes_buff_field：只有 0/4 写独立的 attackBuff 字段",
        [S.writes_buff_field(v) for v in range(10)] == [True, False, False, False, True, False, False, False, False, False])
    chk("clamp_applies：0/4 不夹取（直接 +=），1/2/3/5 夹取",
        [S.clamp_applies(v) for v in range(10)] == [False, True, True, True, False, True, False, False, False, False])
    chk("fires_after_change_events：只有 default 家族 6-9（全文件唯一调用点 :11174）",
        [S.fires_after_change_events(v) for v in range(10)] == [False] * 6 + [True] * 4)
    chk("touches_stats 与 fires_after_change_events 互补（每个值恰好一个 True）",
        all(S.touches_stats(v) != S.fires_after_change_events(v) for v in range(10)))
    chk("ChangeAttack 的分支表在 stats.py 注释里带行号（6 个段落都能 grep 到）",
        all(t in open(os.path.join(ROOT, "engine", "natives", "stats.py"), encoding="utf-8").read()
            for t in (":10981", ":11151", ":11174", ":11185", ":11081", ":11950")))

    # ③ 接线 1：effectvm 对 SetValue 的判定来自这一族（改 stats 的集合，effectvm 跟着变）------
    rec = EV.Recorder()
    rec.ptr_ids[0x100] = 1
    rec.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 4, S.SET_VALUE], "tainted": False})
    e = EV.to_effects(rec, my_side=1)
    chk("effectvm：SetValue(2) 且读不到当前值 ⇒ 记缺口、不产出 buff（不猜）",
        e.get("buff") in (None, [0, 0]) and any("SetValue" in g for g in rec.gaps), str(e))
    rec2 = EV.Recorder()
    rec2.ptr_ids[0x100] = 1
    rec2.cur_stats[0x100] = {"attack": 5}
    rec2.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 2, S.SET_VALUE], "tainted": False})
    chk("effectvm：SetValue 换成增量（设成 2、当前 5 ⇒ -3）",
        EV.to_effects(rec2, my_side=1).get("buff_ids") == {1: [-3, 0]},
        str(EV.to_effects(rec2, my_side=1)))
    rec3 = EV.Recorder()
    rec3.ptr_ids[0x100] = 1
    rec3.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 3, S.PERM_BUFF], "tainted": False})
    chk("effectvm：permBuff(1) 仍是加法（+3）", EV.to_effects(rec3, my_side=1).get("buff") == [3, 0])
    chk("effectvm 里不再有独立的魔数判定（_is_set_change 转调 stats）",
        EV._is_set_change(S.SET_VALUE) and not EV._is_set_change(S.PERM_BUFF))

    # ③ 接线 2：sim 的绝对值夹取走 clamp_stat（150 的"设为 150"落到 99）--------------------
    st = Sim({1: U(1, ME, "frontline", 5, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {},
               my_side=ME)
    _apply_eff(st, {"set_attack": {1: 150}}, None)
    chk("sim：set_attack 150 ⇒ 99（原版 clamp [0,99]）", st.units[1].atk == 99, str(st.units[1].atk))
    # 防御归零 ⇒ **摧毁**（原版 `ChangeDefense` 的 `Label_984`：`getTotalDefense()==0` ⇒ `DestroyCard`）
    st_d = Sim({1: U(1, ME, "frontline", 5, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {},
                 my_side=ME)
    _apply_eff(st_d, {"set_defense": {1: -7}}, None)
    chk("sim：set_defense 变成 0 ⇒ **离场**（原版 ChangeDefense「归零 ⇒ 摧毁」，旧实现让它活着）",
        1 not in st_d.units, str(list(st_d.units)))

    # ④ 三条 BP 函数的 switch 表（规格数据；别让"我读到的分支"只活在注释里）---------------
    for nm, tbl, set_lbl in (("ChangeAttack", S.ATTACK_SWITCH, "Label_855"),
                             ("ChangeDefense", S.DEFENSE_SWITCH, "Label_984"),
                             ("ChangeOperationCost", S.OPCOST_SWITCH, "Label_2747"),
                             ("ChangeHeavyArmor", S.HEAVY_SWITCH, "Label_1433")):
        chk("%s switch 表：10 个 changeType 都有落点，且 2/3/5 同落一处" % nm,
            sorted(tbl) == list(range(10)) and {tbl[ct] for ct in (2, 3, 5)} == {set_lbl}, str(tbl))
    chk("ChangeDefense 的 switch 与 ChangeAttack **不同构**（6/7/9 走各自的分支，不是 default）",
        S.DEFENSE_SWITCH[6] != S.DEFENSE_SWITCH[8] and S.DEFENSE_SWITCH[9] != S.DEFENSE_SWITCH[8])
    chk("ChangeAttack 的 6-9 才是 default 家族（都走 Label_1990）",
        {S.ATTACK_SWITCH[ct] for ct in (6, 7, 8, 9)} == {"Label_1990"})
    chk("ChangeOperationCost 的 6-9 什么都不做（Label_3168）",
        {S.OPCOST_SWITCH[ct] for ct in (6, 7, 8, 9)} == {"Label_3168"})
    chk("ChangeHeavyArmor 的 6-9 是**直接 return**（连数值变化通知都没有，与 ChangeAttack 不同）",
        {S.HEAVY_SWITCH[ct] for ct in (6, 7, 8, 9)} == {"return"})
    chk("重甲夹取区间是 [0,3]（不是 [0,99]）",
        (S.ARMOR_MIN, S.ARMOR_MAX) == (0, 3) and S.clamp_armor(9) == 3)

    # ④ 出处**真的**指向对的地方吗？—— 拿导出件核对行号（发布导出里没有 reverse-data 就跳过）
    bp = os.path.normpath(os.path.join(ROOT, "..", "reverse-data",
                                       "exports-1.60.27292.launcher-only-decompiled-BP", "kards",
                                       "Content", "Blueprints", "Cards", "BP_CardFunctions.cpp"))
    if not os.path.exists(bp):
        print("  [SKIP] 出处核对：找不到 BP 导出件（发布导出里没有 reverse-data，属正常）")
    else:
        lines = open(bp, encoding="utf-8", errors="replace").readlines()

        def at(n):
            return lines[n - 1] if 1 <= n <= len(lines) else ""

        ok_ranges = all("public void %s(" % fn in at(a) for fn, (a, _b) in S.BP_FUNC_RANGES.items())
        chk("三个函数体区间都以 `public void <名>(` 开头", ok_ranges,
            str({fn: at(a).strip()[:60] for fn, (a, _b) in S.BP_FUNC_RANGES.items()}))
        # 区间不能越界到隔壁函数：止行之后第一条 `public void` 必须**紧接着**（±5 行内）
        # ⇒ 这才证明"记的止行 = 函数体末尾"。★ 2026-10-03：`ChangeDefense` 原来记成 (11215, 11480)
        # 而真正结束是 11696；旧守卫只查"next > b"所以放过了它 —— 收紧成"紧贴"就抓住了。
        for fn, (a, b) in S.BP_FUNC_RANGES.items():
            nxt = next((i for i in range(b + 1, min(b + 40, len(lines))) if "public void " in lines[i - 1]), None)
            chk("%s 的止行就是函数体末尾（下一个 public void 紧贴在其后）" % fn,
                nxt is not None and b < nxt <= b + 5, "end=%s next=%s" % (b, nxt))
        # 逐条：我引的行号必须真的含有我声称的东西（引错行号 = 出处造假）
        cites = [(11174, "ExecuteAfterChangeAttackEvents"), (11950, "public void ExecuteAfterChangeAttackEvents"),
                 (11081, "Clamp"), (11961, "FetchAllCardsWithEventTrigger"), (11961, "0x5"),
                 (11338, "getTotalDefense"), (11347, "DestroyCard"), (11357, "0x6"), (11439, "1"),
                 (8906, "IsUnrevealedCovertCard"), (8950, "0xA"), (9072, "0x9"), (9186, "Label_3168"),
                 (11271, "localChangeType"), (16461, "setAndEncryptDefense"),
                 (10320, "public void ChangeHeavyArmor"), (10343, "Change Heavy Armor"), (10381, "ExecuteOnOtherCardsAbilitiesChanged"),
                 (10386, "0x4"), (10423, "return"), (10438, "3"), (10457, "heavyArmorBuff"), (10476, "3"),
                 (11528, "change type incorrect"), (11692, "Label_5740")]
        bad = ["%s@%d" % (tok, n) for n, tok in cites if tok not in at(n)]
        chk("出处行号逐条核对（%d 条）" % len(cites), not bad, str(bad))
        # R9 的关键：setAndEncryptDefense 在伤害链里也被调用（双调用方）
        chk("R9：setAndEncryptDefense 在 ApplyDamageToCard(:16461) 里也被调用（叶子有双重身份）",
            "setAndEncryptDefense" in at(16461))

    # ④ 触发号口径（我先前凭记忆写错了 0x04 ⇒ 用仓库自己的报告钉住正确的族）------------------
    rep = os.path.normpath(os.path.join(ROOT, "..", "reverse-data", "reports", "report", "ATTACK-HOOKS-1.60.md"))
    if not os.path.exists(rep):
        print("  [SKIP] 触发号口径核对：找不到 ATTACK-HOOKS-1.60.md")
    else:
        rl = open(rep, encoding="utf-8", errors="replace").readlines()
        chk("0x04 是**攻击后**（OnAfterOtherCardAttacks），不是数值变化通知",
            "0x04" in rl[854] and "攻击后" in rl[854], rl[854].strip()[:80])
        chk("数值变化通知是 0x05/0x06/0x07/0x10/0x11/0x2C/0x2D/0x09/0x0A 且标『未接』",
            "0x05/0x06/0x07" in rl[866] and "未接" in rl[866], rl[866].strip()[:100])

    # ⑤ R3/R10：原版的攻/费是**两个字段**（`attack`+`attackBuff`）⇒「设成 n」只写基础值，
    #   挂着的 buff **不能被抹掉**。这是本族最容易被"只有一个总量"的模型搞错的地方。-----
    st2 = Sim({1: U(1, ME, "frontline", 55, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {},
                my_side=ME)
    st2.units[1].atk_buff = 50                      # 原版：attackBuff=50（tempBuffGive +50，不夹取）
    chk("R10 前置：总量 = clamp(基础 5 + buff 50) = 55",
        st2.units[1].atk == 55 and st2.units[1].atk_buff == 50)
    _apply_eff(st2, {"set_attack": {1: 2}}, None)              # MONSOON RAIN 式 SetValue(2)
    chk("R10 场景：SetValue(2) 只改基础值 ⇒ 总攻 clamp(2+50) = **52**（不是 2、不是 99）",
        st2.units[1].atk == 52 and st2.units[1].atk_buff == 50,
        "atk=%s buff=%s" % (st2.units[1].atk, st2.units[1].atk_buff))
    _apply_eff(st2, {"set_attack_buff": {1: 0}}, None)         # 卸掉 buff ⇒ 只剩基础值 2
    chk("R10 场景（反向）：把 buff 卸成 0 ⇒ 总攻回到基础值 2",
        st2.units[1].atk == 2 and st2.units[1].atk_buff == 0,
        "atk=%s buff=%s" % (st2.units[1].atk, st2.units[1].atk_buff))
    chk("U.copy() 带着 atk_buff（决策会 clone 场面，漏了就静默丢 buff）",
        st2.units[1].copy().atk_buff == st2.units[1].atk_buff)
    chk("H.copy/字段：手牌也有独立 cost_buff",
        H(10, "O", 3, "order", cost_buff=2).cost_buff == 2)

    # ⑤ 适配器：原版字段 attackBuff / kreditsBuff 要读进模拟（单一来源是 BaseCardObject）-----
    from _cards import mk_card                                  # noqa: E402
    cards = [mk_card(1, location="frontline", attack=3, defense=2, attack_buff=7),
             mk_card(2, location="hand", kredit_cost=2, kredit_buff=1)]
    sim2 = from_cards(cards, lambda c: set(), my_side=ME)
    chk("from_cards：原版 attackBuff=7 ⇒ 总量 10、atk_buff 7（不编数）",
        sim2.units[1].atk == 10 and sim2.units[1].atk_buff == 7,
        "atk=%s buff=%s" % (sim2.units[1].atk, sim2.units[1].atk_buff))
    chk("from_cards：原版 kreditsBuff=1 ⇒ 手牌 cost_buff 1、总量 3",
        sim2.hand[2].cost == 3 and sim2.hand[2].cost_buff == 1,
        "cost=%s buff=%s" % (sim2.hand[2].cost, sim2.hand[2].cost_buff))

    # ⑤ R10 的**记录器路径**（`effectvm.to_effects`）：`SetValue` 折成增量时必须把目标身上
    #   已有的 buff 累加器算进去，否则"设为 n"会把 buff 一起抹掉。---------------------------------
    def rec_set(cur, buff, n, key="attack", verb="ChangeAttack"):
        r = EV.Recorder()
        r.ptr_ids[0x100] = 1
        r.cur_stats[0x100] = {key: cur, key + "_buff": buff}
        r.records.append({"verb": verb, "args": [0x100, 9, n, S.SET_VALUE], "tainted": False})
        return EV.to_effects(r, my_side=1)

    e_pre = rec_set(55, 50, 2)                    # 5 攻 + 50 buff（总量 55）被"设为 2"
    chk("记录器：有 50 点 buff 时 SetValue(2) ⇒ 增量 = clamp(2)+50−55 = **−3**（不是 −53）",
        e_pre.get("buff_ids") == {1: [-3, 0]}, str(e_pre))
    st4 = Sim({1: U(1, ME, "frontline", 55, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    st4.units[1].atk_buff = 50
    _apply_eff(st4, e_pre, None)
    chk("记录器路径端到端：应用后总攻 **52**（原版 clamp(2+50)；旧实现会抹成 2）",
        st4.units[1].atk == 52 and st4.units[1].atk_buff == 50,
        "atk=%s buff=%s" % (st4.units[1].atk, st4.units[1].atk_buff))
    chk("记录器：SetValue(150) 先按原版 clamp 到 99 再折增量（无 buff 时 5 ⇒ +94）",
        rec_set(5, 0, 150).get("buff_ids") == {1: [94, 0]}, str(rec_set(5, 0, 150)))
    chk("记录器：`defense` 原版没有独立 buff 字段 ⇒ 折算与从前一致（4 → 设 2 ⇒ −2）",
        rec_set(4, 0, 2, "defense", "ChangeDefense").get("buff_ids") == {1: [0, -2]},
        str(rec_set(4, 0, 2, "defense", "ChangeDefense")))
    chk("记录器：行动费同样 buff-aware（3 + 2 buff，设成 1 ⇒ 增量 0，总量仍 3）",
        rec_set(3, 2, 1, "opcost", "ChangeOperationCost").get("opcost") == 0,
        str(rec_set(3, 2, 1, "opcost", "ChangeOperationCost")))

    # 同一次动作里"先加 buff 再设值"也必须对（两条效果折进同一个增量，按线性相加结算）：
    rec_mix = EV.Recorder()
    rec_mix.ptr_ids[0x100] = 1
    rec_mix.cur_stats[0x100] = {"attack": 5, "attack_buff": 0}
    rec_mix.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 50, S.TEMP_BUFF_GIVE], "tainted": False})
    rec_mix.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 2, S.SET_VALUE], "tainted": False})
    e_mix = EV.to_effects(rec_mix, my_side=1)
    st5 = Sim({1: U(1, ME, "frontline", 5, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    _apply_eff(st5, e_mix, None)
    chk("同一次动作：tempBuffGive(+50) 后 SetValue(2) ⇒ 总攻 **52**（原版：基础 2 + buff 50）",
        st5.units[1].atk == 52, "eff=%s atk=%s" % (e_mix, st5.units[1].atk))

    # 仍然存在的边界（如实钉住，不假装已修）：0/4 折的是**总量增量**，`atk_buff` 字段不跟新
    # （只影响"同一次动作里 0/4 之后又发生 set_attack_buff 叶子写"这种极罕见组合）。
    rec0 = EV.Recorder()
    rec0.ptr_ids[0x100] = 1
    rec0.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 50, S.TEMP_BUFF_GIVE], "tainted": False})
    e0 = EV.to_effects(rec0, my_side=1)
    st3 = Sim({1: U(1, ME, "frontline", 5, 5, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    _apply_eff(st3, e0, None)
    chk("已知边界：tempBuffGive 折成总量增量 ⇒ 总量 55 但 `atk_buff` 仍 0（见 P3 工单 R10b 的残项）",
        e0.get("buff_ids") == {1: [50, 0]} and st3.units[1].atk == 55 and st3.units[1].atk_buff == 0,
        "eff=%s atk=%s buff=%s" % (e0, st3.units[1].atk, st3.units[1].atk_buff))

    # ⑥ 真路径：`_fill_cur_stats` 必须真的读到"总量"。★ 2026-10-03 这里曾是一条**活死代码**：
    #   `Card.total_attack` 当初写成**方法** ⇒ `getattr` 拿到绑定方法 ⇒ 被
    #   `isinstance(v, (int, float))` 过滤掉 ⇒ `cur_stats` 里**从来没有** attack/opcost ⇒
    #   那两个 SetValue 分支永远记"读不到目标当前值"的缺口、**效果被整个丢掉**。
    #   旧测试喂的是自造的 `cur_stats`，所以完全看不见 —— 这两条测的就是"到底读得出来吗"。
    from kardsmem.board import Card as _Card                          # noqa: E402
    chk("`Card.total_attack` / `total_operation_cost` 是 **property**（写成方法就静默失效）",
        isinstance(_Card.__dict__.get("total_attack"), property)
        and isinstance(_Card.__dict__.get("total_operation_cost"), property),
        str(_Card.__dict__.get("total_attack")))
    c_real = mk_card(1, location="frontline", attack=3, defense=2, attack_buff=7, ptr=0x100)
    rec_r = EV.Recorder()
    EV._fill_cur_stats(rec_r, [c_real])
    st_c = rec_r.cur_stats.get(0x100) or {}
    chk("真路径：cur_stats 读到 attack=10（**总量**，含 buff 7）/ defense=2 / opcost=1 / attack_buff=7",
        st_c.get("attack") == 10 and st_c.get("defense") == 2 and st_c.get("opcost") == 1
        and st_c.get("attack_buff") == 7, str(st_c))
    rec_r.records.append({"verb": "ChangeAttack", "args": [0x100, 9, 2, S.SET_VALUE], "tainted": False})
    e_r = EV.to_effects(rec_r, my_side=1)
    chk("真路径：SetValue(2) **不再记缺口**，增量 = clamp(2)+7−10 = −1（原版：基础设成 2、buff 留 7 ⇒ 总量 9）",
        not rec_r.gaps and e_r.get("buff_ids") == {1: [-1, 0]}, "eff=%s gaps=%s" % (e_r, rec_r.gaps))

    # ⑦ R12：重甲的 `SetValue` 要按 **[0,3]** 夹（原版 `ChangeHeavyArmor` 的 `Label_1433`）。
    #   它以前落进通用数字兜底 ⇒ 被当**增量**记（1 甲"设为 2"给 +2 ⇒ 变成 3）。
    c_ar = mk_card(1, location="frontline", attack=1, defense=5, keywords=("heavyarmor1",), ptr=0x200)
    # ★ 2026-10-04：**不再往 `raw` 里自己塞 `total_heavy_armor`/`heavy_armor_buff`**！
    #   实机快照的 raw 里**没有**这两个键（只有 `heavy_armor`），而 `Card` 以前也**没有**
    #   对应的 property ⇒ `_fill_cur_stats` 两项都取不到 ⇒ `cur_stats` 里从来没有
    #   `armor`/`armor_buff` ⇒ **重甲 SetValue 在实机路径上记缺口、效果被整个丢掉**。
    #   旧测试正是因为塞了那两个键才一直绿（fixture 形状只有测试里成立 —— 弯路 #11/#12）。
    #   现在走**属性**路径（`Card.total_heavy_armor` / `heavy_armor_buff`）。
    rec_ar = EV.Recorder()
    EV._fill_cur_stats(rec_ar, [c_ar])
    st_ar = rec_ar.cur_stats.get(0x200) or {}
    chk("真路径（**实机形状**，不塞 raw）：重甲读进 cur_stats（armor=1，`[0,3]` 夹过的总量）",
        st_ar.get("armor") == 1, str(st_ar))
    rec_ar.records.append({"verb": "ChangeHeavyArmor", "args": [0x200, 9, 2, S.SET_VALUE], "tainted": False})
    e_ar = EV.to_effects(rec_ar, my_side=1)
    chk("R12：1 甲被「设为 2」⇒ 增量 **+1**（旧实现给 +2 ⇒ 变成 3）", e_ar.get("armor") == 1, str(e_ar))
    rec_ar2 = EV.Recorder()
    EV._fill_cur_stats(rec_ar2, [c_ar])
    rec_ar2.records.append({"verb": "ChangeHeavyArmor", "args": [0x200, 9, 9, S.SET_VALUE], "tainted": False})
    e_ar2 = EV.to_effects(rec_ar2, my_side=1)
    chk("R12：设为 9 先夹到 **3** ⇒ 增量 +2（重甲夹取是 [0,3]，不是 [0,99]）", e_ar2.get("armor") == 2, str(e_ar2))
    # ★ 2026-10-04 补：重甲的**独立 buff 字段**也要参与折算（原版 `[0,3]` 夹的是
    #   `heavyArmor + heavyArmorBuff`）。1 甲 + 2 buff（总量已夹到 3）被"设为 2" ⇒
    #   设成的是**基础值** 2 ⇒ 目标总量 = clamp(2+2)=3 ⇒ 增量 **0**（不是 −1）。
    c_ar3 = mk_card(3, location="frontline", defense=5, keywords=("heavyarmor1",), ptr=0x201)
    c_ar3.obj.heavyArmorBuff = 2
    rec_ar3 = EV.Recorder()
    EV._fill_cur_stats(rec_ar3, [c_ar3])
    st_ar3 = rec_ar3.cur_stats.get(0x201) or {}
    chk("实机形状：`heavy_armor_buff` 走 property 读出来（=2）、总量 clamp(1+2)=3",
        st_ar3.get("armor") == 3 and st_ar3.get("armor_buff") == 2, str(st_ar3))
    rec_ar3.records.append({"verb": "ChangeHeavyArmor", "args": [0x201, 9, 2, S.SET_VALUE], "tainted": False})
    e_ar3 = EV.to_effects(rec_ar3, my_side=1)
    chk("重甲：1 甲 +2 buff 被「设为 2」⇒ 增量 **0**（基础设成 2、buff 留 2 ⇒ 总量仍 3）",
        e_ar3.get("armor") == 0 and not rec_ar3.gaps, "eff=%s gaps=%s" % (e_ar3, rec_ar3.gaps))

    # ⑧ R1 前半：原版的门 —— `ChangeOperationCost`(:8906) / `ChangeHeavyArmor`(:10332) 的**第一句**
    #   就是 `IsUnrevealedCovertCard(card)`，真 ⇒ **静默 return、什么都不改**（连日志都没有）。
    #   我们以前不看这道门 ⇒ 对"未揭示的隐蔽牌"也照改（多算）。两个输入必须给出不同答案（弯路 #11）。
    c_cov = mk_card(4, location="frontline", defense=5, kredit_cost=2, keywords=("covert",),
                    is_revealed=False, operation_cost=3, ptr=0x300)
    rec_cov = EV.Recorder()
    EV._fill_cur_stats(rec_cov, [c_cov])
    chk("门判据：`cur_stats` 带 `unrevealed_covert=True`（隐蔽且未揭示；= hasCovert ∧ ¬isRevealed）",
        (rec_cov.cur_stats.get(0x300) or {}).get("unrevealed_covert") is True,
        str(rec_cov.cur_stats.get(0x300)))
    rec_cov.records.append({"verb": "ChangeOperationCost", "args": [0x300, 9, -1, S.PERM_BUFF], "tainted": False})
    rec_cov.records.append({"verb": "ChangeHeavyArmor", "args": [0x300, 9, 2, S.SET_VALUE], "tainted": False})
    e_cov = EV.to_effects(rec_cov, my_side=1)
    chk("门拦下：未揭示的隐蔽牌 ⇒ 两个动词**都不产出效果**、也不记缺口（这是「已知的不改」）",
        not e_cov.get("opcost") and not e_cov.get("armor") and not rec_cov.gaps,
        "eff=%s gaps=%s" % (e_cov, rec_cov.gaps))
    c_rev = mk_card(5, location="frontline", defense=5, kredit_cost=2, keywords=("covert",),
                    is_revealed=True, operation_cost=3, ptr=0x301)
    rec_rev = EV.Recorder()
    EV._fill_cur_stats(rec_rev, [c_rev])
    chk("对照输入：**已揭示**的隐蔽牌 ⇒ `unrevealed_covert=False`",
        (rec_rev.cur_stats.get(0x301) or {}).get("unrevealed_covert") is False,
        str(rec_rev.cur_stats.get(0x301)))
    rec_rev.records.append({"verb": "ChangeOperationCost", "args": [0x301, 9, -1, S.PERM_BUFF], "tainted": False})
    e_rev = EV.to_effects(rec_rev, my_side=1)
    chk("对照组：已揭示 ⇒ 门放行、行动费照改（−1）", e_rev.get("opcost") == -1, str(e_rev))

    # ⑨ R1 后半：**`CanCardBeBuffed`**（原版蓝图函数 `:23631-23704`，逐行读过）——
    #   未揭示的隐蔽牌**只在牌库/手牌可被 buff**（location 1/2/3/4/9）；在场上（总部/前线）·弃牌堆·
    #   NotAvailable 都不可（其它值原版**不写出参** ⇒ 按 false）。`ChangeAttack`(:10990)/`ChangeDefense`
    #   (:11222) **无条件**过这道门；`ChangeKreditCost`(:10067) 只在第 5 参 `skipCovertCheck` 为假时过。
    chk("CanCardBeBuffed 纯函数：非隐蔽⇒True；隐蔽@前线(7)⇒False；隐蔽@手牌(3)⇒True；未知 location⇒False",
        S.can_card_be_buffed(False, 7) is True and S.can_card_be_buffed(True, 7) is False
        and S.can_card_be_buffed(True, 3) is True and S.can_card_be_buffed(True, None) is False)
    c_fld = mk_card(6, location="frontline", attack=3, keywords=("covert",), is_revealed=False, ptr=0x400)
    rec_fld = EV.Recorder()
    EV._fill_cur_stats(rec_fld, [c_fld])
    chk("门判据：隐蔽未揭示**在场上** ⇒ `buffable=False`",
        (rec_fld.cur_stats.get(0x400) or {}).get("buffable") is False,
        str(rec_fld.cur_stats.get(0x400)))
    rec_fld.records.append({"verb": "ChangeAttack", "args": [0x400, 9, 2, S.TEMP_BUFF_GIVE, False], "tainted": False})
    e_fld = EV.to_effects(rec_fld, my_side=1)
    chk("门拦下：隐蔽未揭示@场上 ⇒ `ChangeAttack` **不产出效果**",
        not e_fld.get("buff") and not e_fld.get("buff_ids"), str(e_fld))
    c_hand = mk_card(7, location="hand", attack=3, keywords=("covert",), is_revealed=False, ptr=0x401)
    rec_hand = EV.Recorder()
    EV._fill_cur_stats(rec_hand, [c_hand])
    rec_hand.records.append({"verb": "ChangeAttack", "args": [0x401, 9, 2, S.TEMP_BUFF_GIVE, False], "tainted": False})
    e_hand = EV.to_effects(rec_hand, my_side=1)
    chk("对照输入：隐蔽未揭示**在手牌** ⇒ 门放行、+2 攻照记（`buff_ids`）",
        e_hand.get("buff_ids") == {7: [2, 0]}, str(e_hand))
    rec_sk = EV.Recorder()
    EV._fill_cur_stats(rec_sk, [c_fld])
    rec_sk.records.append({"verb": "ChangeKreditCost", "args": [0x400, 9, -1, S.PERM_BUFF, True], "tainted": False})
    chk("`ChangeKreditCost` 带 `skipCovertCheck=True` ⇒ **越过**门（照改 −1）",
        EV.to_effects(rec_sk, my_side=1).get("cost_ids") == {6: -1}, str(EV.to_effects(rec_sk, my_side=1)))
    rec_no = EV.Recorder()
    EV._fill_cur_stats(rec_no, [c_fld])
    rec_no.records.append({"verb": "ChangeKreditCost", "args": [0x400, 9, -1, S.PERM_BUFF, False], "tainted": False})
    chk("`ChangeKreditCost` 带 `skipCovertCheck=False` ⇒ 门拦下（不产出）",
        not EV.to_effects(rec_no, my_side=1).get("cost_ids"), str(EV.to_effects(rec_no, my_side=1)))

    # ⑦ R13：三个"总量" getter 必须夹 `[0,99]` —— 以前不夹 ⇒ `sim`（走 gamemodel）
    #   会把 90 攻 + 50 buff 读成 **140**（游戏里 99），而 `cards.read_raw` 那条路一直是对的。
    c_cl = mk_card(3, attack=90, attack_buff=50, kredit_cost=2, kredit_buff=-5)
    chk("R13：`getTotalAttack` 夹到 99、`getTotalKredits` 夹到 0（原版 clamp(0,99)）",
        c_cl.obj.getTotalAttack() == 99 and c_cl.obj.getTotalKredits() == 0,
        "atk=%s kredit=%s" % (c_cl.obj.getTotalAttack(), c_cl.obj.getTotalKredits()))

    # ⑧ `ChangeKreditCost`（手牌费用）：以前产出的键 `cost` 是**标量**、**没有任何消费者**
    #   （`_apply_eff` 的 target 是场上单位/总部，手牌不是 target）⇒ 效果被整个丢掉。
    #   现在发**逐卡字典** `cost_ids`，由 sim 落到 `Hand.cost`（set 家族按"设成"口径 + `[0,99]` 夹）。
    c_k = mk_card(1, location="hand", kredit_cost=3, kredit_buff=-1, ptr=0x400)
    c_k.raw["kredit_buff"] = -1
    rec_k = EV.Recorder()
    EV._fill_cur_stats(rec_k, [c_k])
    rec_k.records.append({"verb": "ChangeKreditCost", "args": [0x400, 9, 1, S.SET_VALUE], "tainted": False})
    e_k = EV.to_effects(rec_k, my_side=1)
    chk("手牌费用：SetValue(1) ⇒ 逐卡增量 **−2**（基础设成 1 + buff −1 ⇒ 总量 0；旧实现给标量 +2 且无人消费）",
        e_k.get("cost_ids") == {1: -2} and "cost" not in e_k, str(e_k))
    st_k = Sim({}, {ME: 20, OPP: 20}, 5.0, {1: H(1, "X", 2, "order")}, my_side=ME)
    _apply_eff(st_k, e_k, None)
    chk("手牌费用：sim 真的落到 `Hand.cost`（2 ⇒ 0）", st_k.hand[1].cost == 0, str(st_k.hand[1].cost))
    rec_k2 = EV.Recorder()
    rec_k2.records.append({"verb": "ChangeKreditCost", "args": [0x999, 9, -1, S.PERM_BUFF], "tainted": False})
    chk("手牌费用：指针认不出 ⇒ 记缺口、不产出效果（不编数）",
        not EV.to_effects(rec_k2, my_side=1).get("cost_ids")
        and any("ChangeKreditCost" in g for g in rec_k2.gaps), str(rec_k2.gaps))
    # ★ `ChangeKreditCost` 的 switch 与四个兄弟不同：**只有 2/3 是设值**，`5` 与 `6-9` 落到默认
    #   `Label_1614`（`NotifySetKreditCost` + 0x2D 通知）—— **不改数值**。别照抄 ChangeAttack 的集合。
    chk("手牌费用：`KREDIT_COST_SET_FAMILY` 只有 2/3（5 与 6-9 走默认分支）",
        S.KREDIT_COST_SET_FAMILY == frozenset((S.SET_VALUE, S.SUPPRESS))
        and S.VETERAN_SET not in S.KREDIT_COST_SET_FAMILY)
    rec_k5 = EV.Recorder()
    EV._fill_cur_stats(rec_k5, [c_k])
    rec_k5.records.append({"verb": "ChangeKreditCost", "args": [0x400, 9, 1, S.VETERAN_SET], "tainted": False})
    e_k5 = EV.to_effects(rec_k5, my_side=1)
    chk("手牌费用：changeType 5（veteranSet）⇒ 原版不改数值 ⇒ 不产出效果、也不记缺口",
        not e_k5.get("cost_ids") and not rec_k5.gaps, "eff=%s gaps=%s" % (e_k5, rec_k5.gaps))

    # ⑨ R11（后半）：**普通加减通道**也要把"总量"夹回原版范围 —— `getTotal*` 是 `clamp(0,99)`
    #   （IDA；重甲另有一套 `clamp(0,3)`）。不夹就会在盘面上出现 140 这种游戏里不存在的值。
    #   ★ 注意：防御的**负**增量降到位会摧毁（下面 ⑩），所以这里用**正**增量验上界。
    st_b = Sim({1: U(1, ME, "frontline", 90, 98, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    _apply_eff(st_b, {"buff_ids": {1: [50, 50]}}, None)
    chk("R11：攻 +50 夹到 99、防 +50 也夹到 99（原版 getTotal* = clamp(0,99)）",
        (st_b.units[1].atk, st_b.units[1].dfn) == (99, 99),
        "atk=%s dfn=%s" % (st_b.units[1].atk, st_b.units[1].dfn))
    st_b2 = Sim({1: U(1, ME, "frontline", 5, 3, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    _apply_eff(st_b2, {"buff_ids": {1: [0, -2]}}, None)
    chk("R11：防御 −2 到 1 ⇒ 只掉血、**不**摧毁（只有降到位 ≤0 才摧毁）",
        st_b2.units[1].dfn == 1, str(st_b2.units.get(1) and st_b2.units[1].dfn))
    st_c = Sim({1: U(1, ME, "frontline", 2, 2, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    st_c.units[1].armor = 2
    _apply_eff(st_c, {"armor": 5}, 1)
    chk("R11：重甲 +5 夹到 3（`[0,3]`，不是 `[0,99]`）", st_c.units[1].armor == 3, str(st_c.units[1].armor))
    _apply_eff(st_c, {"opcost": 200}, 1)
    chk("R11：行动费 +200 夹到 99", st_c.units[1].opc == 99, str(st_c.units[1].opc))
    _apply_eff(st_c, {"attack_turn": 200}, 1)
    chk("R11：本回合 +200 攻也夹到 99（`AddAttackUntilEndOfTurn` 走同一条总量）",
        st_c.units[1].atk == 99, str(st_c.units[1].atk))

    # ⑩ 防御降到位 ⇒ **摧毁**（原版 `ChangeDefense`：`2/3/5` 的 `Label_984` 设完 `==0` 就 DestroyCard；
    #   `1` permBuff 的负分支 `<=0` 也 DestroyCard。`0/4/6-9` 是**拒绝**分支 ⇒ 没有 buff 通道）。
    #   三条通道（群体 `buff_ids` / 标量 `buff` / 绝对值 `set_defense`）走同一条 `_def_delta`。
    for nm, eff in (("群体 buff_ids", {"buff_ids": {1: [0, -5]}}),
                    ("标量 buff", {"buff": [0, -5]}),
                    ("绝对值 set_defense", {"set_defense": {1: 0}})):
        s_d = Sim({1: U(1, ME, "frontline", 2, 3, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
        _apply_eff(s_d, eff, 1 if "buff_ids" not in eff else None)
        chk("防御降到位 ⇒ 离场（%s）" % nm, 1 not in s_d.units, str(list(s_d.units)))
    s_d2 = Sim({1: U(1, ME, "frontline", 2, 3, 2, "infantry")}, {ME: 20, OPP: 20}, 5.0, {}, my_side=ME)
    _apply_eff(s_d2, {"buff": [0, 5]}, 1)
    chk("升防御不会摧毁（原版正分支夹取下限是 1）", 1 in s_d2.units and s_d2.units[1].dfn == 8, str(s_d2.units[1].dfn))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
