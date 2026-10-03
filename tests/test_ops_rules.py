#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPS 架构规则（`OPS-ARCHITECTURE.md` §3/§6/§7 步骤 1）作为测试：
  * lint 棘轮：裸 `time.sleep`、`except …: pass` 的数量**只许降不许升**（基线写在本文件，降了就把基线改小）；
  * `Result` 类型：`ok` 只能由动作流回执构造，旁证不能令 `ok=True`；
  * `PromptInfo`：`pending()` 字典 → 统一的提示类别（OPS 只报告，不决策）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import ops_result as OR, promptinfo as PI           # noqa: E402

# 基线（2026-10-03 量得）。只许降：降了就改小，升了测试就红。
BASELINE = {
    "ops/inject.py": {"sleep": 14, "except_pass": 11},
    "agent/session.py": {"sleep": 0, "except_pass": 3},
    "player/loop.py": {"sleep": 11, "except_pass": 10},
    "player/rule.py": {"sleep": 0, "except_pass": 13},
}
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def counts(path):
    s = open(os.path.join(ROOT, path), encoding="utf-8").read()
    return {"sleep": len(re.findall(r"\btime\.sleep\(", s)),
            "except_pass": len(re.findall(r"except[^\n:]*:[^\n]*\n\s*pass\b", s)),
            "bare_except": len(re.findall(r"except\s*:", s))}


def main():
    for f, base in BASELINE.items():
        c = counts(f)
        chk("lint 棘轮 %s：裸 sleep ≤ %d、except…pass ≤ %d、无裸 except:" % (f, base["sleep"], base["except_pass"]),
            c["sleep"] <= base["sleep"] and c["except_pass"] <= base["except_pass"] and c["bare_except"] == 0, str(c))

    r = OR.Result.from_receipt(OR.Receipt("XActionPlayCardFromHand", {"cardID": 6}))
    chk("Result：只有回执能构造 ok=True", r.ok and r.criterion == "action_stream" and r.receipt.action == "XActionPlayCardFromHand")
    f = OR.Result.fail("awaiting_choice", evidence={"candidates": 3})
    chk("Result：失败必有 reason（枚举），旁证只在 evidence", not f.ok and f.reason == "awaiting_choice" and f.evidence["candidates"] == 3)
    chk("Result：未知 reason 归为 other", OR.Result.fail("乱写").reason == "other")
    pr = PI.PromptInfo("choose_one", [{"index": 0}], 10)
    g = OR.Result.fail("awaiting_choice", prompt=pr)
    chk("Result：prompt 只是证据，不会让 ok 变真", not g.ok and g.prompt.kind == "choose_one" and g.as_dict()["prompt"] == "choose_one")
    # 旁证 ok（旧动词里"候选集合变了就算成功"那种）必须被降级
    a = OR.adapt({"ok": True, "candidates_changed": True})
    chk("adapt：旧字典 ok=True 但没有动作流判据 ⇒ 降为 no_receipt（旁证不得当成功）", not a.ok and a.reason == "no_receipt" and a.evidence["legacy_ok"])
    a2 = OR.adapt({"ok": True, "criterion": "action_stream", "action": "XActionPlayCardFromHand"})
    chk("adapt：声明了动作流判据的 ok 保留", a2.ok and a2.receipt.action == "XActionPlayCardFromHand")
    a3 = OR.adapt({"ok": False, "error": "游戏拒绝"})
    chk("adapt：失败带 reason 与原错误", not a3.ok and a3.evidence["error"] == "游戏拒绝")

    p = PI.from_pending({"choose_one": [{"kind": "choose_one", "index": 0, "trigger_id": 7}], "hand_target": {"pending": True}})
    chk("PromptInfo：抉择优先于手牌目标", p.kind == "choose_one" and p.source == 7 and len(p.options) == 1)
    p = PI.from_pending({"choose_one": [{"kind": "select_card_to_draw", "index": 0, "trigger_id": 20}]})
    chk("PromptInfo：选牌类归 select_card_to_draw", p.kind == "select_card_to_draw" and p.source == 20)
    p = PI.from_pending({"hand_target": {"pending": True, "card_being_played": 30}})
    chk("PromptInfo：手牌目标 + 发起牌", p.kind == "hand_target" and p.source == 30)
    p = PI.from_pending({"board_target": 55})
    chk("PromptInfo：板卡待点目标", p.kind == "board_target" and p.source == 55)
    chk("PromptInfo：什么都没挂 ⇒ None", PI.from_pending({"choose_one": [], "hand_target": {"pending": False}}) is None
        and PI.from_pending({}) is None)
    # JS 模板（OPS 文档 §7 步骤 2）：独立文件、占位符全部被填充、与 ops_inject.JS 一致
    import ops
    from ops import inject as OI
    tpl = ops.load_js_template()
    chk("JS 模板已抽成独立文件 ops/agent.js.tpl，且含占位符", len(tpl) > 10000 and "%(module)s" in tpl)
    chk("ops_inject.JS 没有残留占位符", "%(" not in OI.JS and OI.MODULE[:-4].lower() in OI.JS.lower())
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
