#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""派生视图别名的**棘轮**：`kardsmem/board.py::Card` 上的"原版字段别名"只许减不许增。

背景：P1 的 S3'（`REFACTOR-PLAN.md` §2 / `BOARD-MODEL.md`）要把消费者从**派生别名**迁到**原版字段**
（`.card_id` → `.obj.CardID`、`.kredit_cost` → `.obj.getTotalKredits()`、`.slot` → `.obj.locationNumber`…），
迁完由 S4 把这些别名删掉。**这个测试就是那次迁移的刹车**：数字只许往下走。

★ 2026-10-03：`REFACTOR-PLAN.md` 里曾写"已有别名棘轮护栏（`tests/test_field_alias_ratchet.py`，子代理产出）"
—— 该文件**当时并不存在**（全历史都没有）。这是补上的那一份，别再让文档先于代码。

做法（**AST，不是 grep** —— 注释/文档字符串里提到别名不算，避免自欺）：
  * 别名集合**从 `kardsmem/board.py` 的 `Card` 类现读**（`@property`）⇒ 谁新加一个别名，它自动进账；
  * 统计范围 = 生产包，**不含 `tests/`**、**不含定义别名的那份文件本身**；
  * `EXCLUDED` 里的名字不计数，但**必须写明理由**，且本测试会核对"每个 `Card` 属性要么被计数、
    要么在 `EXCLUDED` 里"—— 不许有名字悄悄掉出账本。

用法：`python tests/test_field_alias_ratchet.py --measure` 打印当前分布（改基线时用）。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOARD = os.path.join(ROOT, "kardsmem", "board.py")
PKGS = ("base", "kardsmem", "ops", "engine", "semantics", "sim", "evaluation", "policy", "agent", "learn",
        "player", "interfaces", "gui", "tools")
SKIP_FILES = {"kardsmem/board.py"}          # 别名的**定义处**（它自己用别名不算消费者）

#: 不计数、但要写明理由（每个 Card 属性必须落在"计数"或这里之一，测试会核对）
EXCLUDED = {
    "keywords": "**有意保留**的聚合视图：由 obj 上十几个关键词旗标 + receivedAbilities 现算出来的列表，原版没有对应的单个字段；"
                "S4 保留它（不是『待删别名』）",
    "location": "已删（读到就抛 RuntimeError）；由 tests/test_seat_rules.py 的 AST 守卫覆盖",
    "name": "与 UFunction.name / obj.name / frame.name 等**大量无关属性**同名 ⇒ 计数几乎全是假阳性",
    "side": "与 ESide 值本身、playing_side 等混用；座位口径由 tests/test_seat_rules.py 守卫",
    "slot": "与 VM/UI 里的 slot 混用（模板槽位、fname 槽），计数不干净",
    "attack": "与 raw/JSON 里的 attack 等混用风险高",
    "defense": "同上",
    "card_type": "目前无人用；等它被用起来再并入计数",
    "total_attack": "**计算属性**（= obj.getTotalAttack()），不是「原版字段别名」；S3' 迁的是字段别名，"
                    "而它本来就是原版 getter 的对应物（2026-10-03 从方法改成 property，见 board.py 注释）",
    "total_operation_cost": "同 `total_attack`：原版 getTotalOperationCost() 的对应物，不是字段别名",
    "total_heavy_armor": "同 `total_attack`：原版 getTotalHeavyArmor() = clamp(0,3, heavyArmor + heavyArmorBuff) "
                         "的对应物（2026-10-04 补，见 board.py 注释），不是字段别名",
}

#: 基线（2026-10-03 量得 263）。**只许减不许增**；每迁一批就把它改小。
#: 量法与"约 270 处"的说法对得上（`--measure` 2026-10-03 合计 263）。
#: ★ S3' 迁移（只改 `kardsmem/` `agent/` `interfaces/` 三处的**读侧写法**，见下）逐批调小：
#:   attack_buff 1→0（`agent/view.py::_stat` 改读 `obj.attackBuff`）⇒ 262
#:   max_defense 1→0（`render_inspect` 改读 `obj.maxDefense`）⇒ 259（本批起点已是 260：can_act 早先被别人减了 2）
#:   max_attack 2→0（同上，改读 `obj.maxAttack`）⇒ 257
#:   rarity_enum 1→0 + card_set_enum 1→0（`render_inspect` 同一句/同一个条件里读，改读
#:     `obj.rarity`（保留原来的 `int(...)` 口径）与 `obj.cardSet`）⇒ 255
#:   enter_play_on_turn 4→1（`view.render_inspect` ×2、`kardsmem/cli.py::cmd_hand` ×1 改读
#:     `obj.enterPlayOnTurn`；剩 1 处在 `ops/query.py`，不在本次写域）⇒ 252
#:   kredits_tax_as_enemy_target 4→0（`view.render_board`/`render_inspect` ×3、
#:     `cards.target_candidates` 的 `"tax"` ×1 改读 `obj.KreditsTax_AsEnemyTarget`）⇒ 248
#:   is_suppressed 2→0（`view._marks` ×1、`cards.target_candidates` 的 `"suppressed"` ×1
#:     改读 `obj.isSuppressed`）⇒ 246
#:   operation_cost 2→0（`view.render_board` 的"有无行动费"判断 ×1、`kardsmem/cli.py::cmd_hand` ×1；
#:     顺手把 `view._op_cost` 里那两个**字符串 getattr**（`operation_cost`/`operation_cost_buff`，
#:     棘轮看不见）也改成 `obj.operationCost` / `obj.operationCostBuff`——留着它们 S4 删别名时会静默变 None）⇒ 244
#:   needs_hand_target 5→0（`view.render_board`/`render_inspect` ×2、`kardsmem/cli.py::cmd_hand` ×1、
#:     `interfaces/mcp.py::t_play` ×1、`interfaces/shell.py::do_play` ×1 改读 `obj.selectTargetOnPlayedFromHand`）⇒ 239
#:   kredit_cost 16→10（`view.render_board` ×2 / `render_inspect` ×1、`cli.py` ×2、`cards.format_table` ×1
#:     改读 `obj.getTotalKredits()`——**不是** `obj.kredits`：别名原本就调这个 getter（含 buff、夹 [0,99]）；
#:     剩 10 处在 ops/query.py、learn/*、player/*、tools/，不在本次写域）⇒ 233
#:   target_uid 2→0（`cards.format_table` 同一行两处；别名本体是 `"0x%X" % obj.CurrentTarget` 的**格式化**，
#:     内联成 `("  ->0x%X" % c.obj.CurrentTarget) if c.obj.CurrentTarget else ""`，输出一字不差）⇒ 231
#:   card_id 207→153（本写域能改的 **54 处全部**改读 `obj.CardID`：`agent/session.py` 3、`agent/view.py` 10、
#:     `interfaces/mcp.py` 15、`interfaces/shell.py` 15、`kardsmem/cards.py` 5、`kardsmem/cli.py` 4、
#:     `kardsmem/names.py` 1、`kardsmem/rendered.py` 1——另外把 rendered.py 两处**字符串 getattr**
#:     （`self.base_card`/盘面卡，棘轮看不见）也改成 `obj.CardID`。剩 153 处的构成（`--measure` 现量）：
#:     `player/rule.py` 96、`learn/baselines.py` 13、`kardsmem/rendered.py` 11（**全是
#:     `RenderedCard.card_id` —— 它自己的 dataclass 字段，跟 Card 别名同名 ⇒ 棘轮的假阳性**）、
#:     `ops/query.py` 10、`player/loop.py` 7、`engine/triggers.py` 4、`tools/info.py` 4、
#:     `ops/cli.py` 2、`ops/world.py` 2、`learn/schema.py` 2、`engine/effectvm.py` 1、`tools/pickdump.py` 1
#:     ——除那 11 处假阳性外**都不在本次写域**（engine/ops/learn/player/tools）⇒ 177
#: ★ 2026-10-05 S3' 续批 177→20：非 rule 生产代码 + `player/rule.py`/`player/loop.py` 的 card_id 全部改读 `obj.CardID`，kredit_cost→`obj.getTotalKredits()`、gotcha_activated→`obj.gotchaActivated`、is_revealed→`obj.isRevealed`、enter_play_on_turn→`obj.enterPlayOnTurn`。**刻意留下的 20 处**：card_id 11（全是 `RenderedCard.card_id` 自带字段的假阳性）、can_act 4（`evaluation/value.py`、`policy/search.py` 里是 sim 单位 `U.can_act()` 方法调用，不是 Card 别名）、keywords 4（别名是由多个 obj 旗标合成的列表，无单一原版字段；`ops/query.py:750` 还是 rec 的 keywords）、gotcha_activated 1（`engine/triggers.py:684`，`tests/test_move_frontline.py` 的假卡 `_C` 没有 `.obj`）。
BASELINE = {
    "attack_buff": 0, "can_act": 0, "card_id": 0, "card_set_enum": 0, "cipher": 0,
    "enter_play_on_turn": 0, "faction_enum": 0, "gotcha_activated": 0,
    "has_been_attacked_this_turn": 0, "heavy_armor": 0, "heavy_armor_buff": 0,
    "is_being_guarded": 0, "is_revealed": 0,
    "is_suppressed": 0, "kredit_cost": 0, "kredits_tax_as_enemy_target": 0,
    "max_attack": 0, "max_defense": 0, "needs_hand_target": 0, "operation_cost": 0,
    "operation_cost_buff": 0, "rarity_enum": 0, "target_uid": 0, "under_enemy_control": 0,
}
TOTAL_BASELINE = 0     # 2026-10-05：登记 FALSE_POSITIVES（card_id×11 RenderedCard、can_act×4 sim 单位）后，剩 keywords×4 + gotcha_activated×1


def card_alias_names():
    """`Card` 类上所有 `@property` 的名字（现读，别写死）。"""
    tree = ast.parse(open(BOARD, encoding="utf-8").read(), filename=BOARD)
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "Card":
            return [b.name for b in n.body
                    if isinstance(b, ast.FunctionDef)
                    and any(isinstance(d, ast.Name) and d.id == "property" for d in b.decorator_list)]
    raise RuntimeError("在 %s 里找不到 class Card" % BOARD)


def iter_prod_files():
    for pkg in PKGS:
        base = os.path.join(ROOT, pkg)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in ("__pycache__", "venv")]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dp, f), ROOT).replace("\\", "/")
                if rel in SKIP_FILES:
                    continue
                yield rel, os.path.join(dp, f)


#: 同名但**不是** `Card` 别名的读（假阳性，按文件登记，必须写理由）：
FALSE_POSITIVES = {
    "card_id": {"kardsmem/rendered.py": "`RenderedCard.card_id` 是它自己的 dataclass 字段，不是 Card 别名"},
    "can_act": {"evaluation/value.py": "`U.can_act()` 是 sim 单位（engine.state.U）的方法，不是 Card.can_act",
                "policy/search.py": "同上（sim 单位）"},
}


def count(aliases):
    per = {a: 0 for a in aliases}
    where = {a: [] for a in aliases}
    for rel, path in iter_prod_files():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in per and isinstance(n.ctx, ast.Load):
                if rel in FALSE_POSITIVES.get(n.attr, {}):
                    continue
                per[n.attr] += 1
                where[n.attr].append("%s:%d" % (rel, n.lineno))
    return per, where


def main():
    names = card_alias_names()
    counted = [a for a in names if a not in EXCLUDED]
    unknown = [a for a in names if a not in EXCLUDED and a not in BASELINE]
    per, where = count(counted)
    total = sum(per.values())

    if "--measure" in sys.argv:
        print("Card 属性 %d 个：计数 %d、EXCLUDED %d" % (len(names), len(counted), len(names) - len(counted)))
        for a in sorted(counted, key=lambda k: -per[k]):
            print("  %-34s %3d   %s" % (a, per[a], ", ".join(where[a][:3])))
        print("合计 %d" % total)
        return 0

    fails = 0

    def chk(name, ok, extra=""):
        nonlocal fails
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        if not ok:
            fails += 1

    chk("别名集合是从 `Card` 现读的（非空、且都是小写下划线名）",
        len(names) >= 15 and all(a.islower() for a in names), "%d 个" % len(names))
    chk("每个 Card 属性要么被计数、要么在 EXCLUDED 里（不许有名字悄悄掉出账本）",
        set(counted) | set(EXCLUDED) >= set(names),
        str(sorted(set(names) - set(counted) - set(EXCLUDED))))
    chk("EXCLUDED 里的名字都真的是 Card 属性（别留下已经不存在的豁免）",
        set(EXCLUDED) <= set(names), str(sorted(set(EXCLUDED) - set(names))))
    chk("基线覆盖了所有被计数的别名（新别名必须先量一次再写进 BASELINE）",
        not unknown, str(unknown))
    if fails:
        print("失败 %d 项（基线不全，先跑 --measure）" % fails)
        return 1

    for a in sorted(counted):
        n, base = per[a], BASELINE[a]
        chk("别名 .%s ≤ 基线 %d" % (a, base), n <= base, "现在 %d" % n)
    chk("别名合计 ≤ 基线 %d（%d 处；只许减）" % (TOTAL_BASELINE, total), total <= TOTAL_BASELINE)
    print("派生别名合计 %d 处（基线 %d，只许减）；排前三：%s"
          % (total, TOTAL_BASELINE, ", ".join("%s=%d" % (a, per[a]) for a in sorted(per, key=lambda k: -per[k])[:3])))
    print("失败 %d 项" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
