#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPS 架构规则（`OPS-ARCHITECTURE.md` §3/§6/§7 步骤 1、3、4）作为测试：
  * lint 棘轮：裸 `time.sleep`、`except …: pass` 的数量**只许降不许升**（基线写在本文件，降了就把基线改小）；
  * `Result` 类型：`ok` 只能由动作流回执构造，旁证不能令 `ok=True`；
  * `PromptInfo`：`pending()` 字典 → 统一的提示类别（OPS 只报告，不决策）；
  * **步骤 3（L1 原语）**：写类 RPC 只许出现在 `ops/primitives.py`（L2/L3 不许自己拼 JS 调用）；
  * **步骤 3（RPC 名一致性）**：ops 包里用到的 RPC 属性名经 frida 的「下划线→驼峰」转换后，
    **必须真的在 `ops/agent.js.tpl` 的导出里存在**（对不上 = 运行期才炸；现有 1 处已知坏调用走棘轮）；
  * **步骤 4（只读查询无写能力）**：`ops/query.py` 的**代码**里不出现任何写原语、
    也不许从 `ops.primitives` import 写原语；需要临时写字段的诊断只在 `ops/diag.py`。
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import ops_result as OR, promptinfo as PI           # noqa: E402

# 基线（2026-10-03 量得）。只许降：降了就改小，升了测试就红。
BASELINE = {
    # ops/inject.py（原 6400 行）2026-10-03 拆成多个模块（D1）：总量不变（sleep 14 / except_pass 11），按新文件登记。
    # ★ 2026-10-04 步骤 3：`settle` 等原语搬进 ops/primitives.py ⇒ conn.py 的 sleep 2→0（已调小）；
    #   primitives.py 新登记（它是 L1，那两个 `time.sleep` 就是 settle 自己的墙钟等待）；
    #   diag.py 新登记（步骤 4：写型诊断从 query.py 搬出来）。
    "ops/inject.py": {"sleep": 0, "except_pass": 0},
    "ops/conn.py": {"sleep": 0, "except_pass": 2},
    "ops/primitives.py": {"sleep": 2, "except_pass": 0},
    "ops/diag.py": {"sleep": 0, "except_pass": 0},
    "ops/world.py": {"sleep": 2, "except_pass": 1},
    "ops/gesture.py": {"sleep": 1, "except_pass": 4},
    "ops/query.py": {"sleep": 0, "except_pass": 1},
    "ops/play.py": {"sleep": 3, "except_pass": 1},
    "ops/choices.py": {"sleep": 2, "except_pass": 0},
    "ops/flow.py": {"sleep": 4, "except_pass": 0},
    "ops/cli.py": {"sleep": 0, "except_pass": 0},
    "ops/support.py": {"sleep": 0, "except_pass": 2},
    "ops/consts.py": {"sleep": 0, "except_pass": 0},
    "agent/session.py": {"sleep": 0, "except_pass": 3},
    "player/loop.py": {"sleep": 11, "except_pass": 10},
    "player/rule.py": {"sleep": 0, "except_pass": 13},
}
OPS_DIR = os.path.join(ROOT, "ops")

#: 写类 RPC（JS 侧那些"会改目标进程内存"的导出）。步骤 3 的判据：**只许出现在 primitives.py**。
WRITE_RPC = ("callRawArrHold", "writeCallRaw", "writeThenCalls", "writeAndCall2",
             "write_and_call", "poke", "poke_and_call0")

#: ★ 棘轮：`(文件, RPC属性名)` —— 经「下划线→驼峰」转换后在 `agent.js.tpl` 里**找不到**的调用。
#: 现在恰好只有这一处，而且是**拆分前就坏的**（不是本次搬运动出来的）：`ops/gesture.py` 里那句
#: `self.api().call_raw_writes(...)` 想调 `callRawWrites`，而 JS 侧根本没有这个导出
#: （写型只有 `writeCallRaw`）。它包在 `try/except` 里 ⇒ 每次必然抛、被记成 `gmu_error`，
#: `consumed` 恒为 None ⇒ 实际走的是"没被消费"那条兜底分支（OnActorMouseUp + OnClicked）。
#: **本次不动它**（改成真的 `writeCallRaw` 会改变点击链的实际行为，必须实机验证）——
#: 只把它钉在这里：**不许再多一处**；将来修好了，这条基线就删掉。
KNOWN_BROKEN_RPC = {("gesture.py", "call_raw_writes"): "JS 侧没有 callRawWrites 导出（写型只有 writeCallRaw）"}
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


def js_exports():
    """`ops/agent.js.tpl` 里 `rpc.exports` 的**全部导出名**（对象字面量 + `rpc.exports.x =`）。"""
    js = open(os.path.join(OPS_DIR, "agent.js.tpl"), encoding="utf-8").read()
    return (set(re.findall(r"^\s{4}(\w+)\s*:\s*function", js, re.M))
            | set(re.findall(r"rpc\.exports\.(\w+)\s*=", js)))


def to_camel(name):
    """复刻 `frida/__init__.py::_to_camel_case`：Python 侧 `api.call_raw(` 实际会去调 `callRaw`。"""
    out, cap = "", False
    for ch in name:
        if ch == "_":
            cap = True
        elif cap:
            out, cap = out + ch.upper(), False
        else:
            out += ch
    return out


def ops_rpc_uses():
    """扫 `ops/*.py`：`self.api().foo(` / `self._api.foo(`（primitives.py 里还有裸 `api.foo(`）。

    这里要的是**写法**（不是语义），所以用正则：列出每个文件里"到底调了哪些 RPC 属性名"。
    """
    out = {}
    for fn in sorted(os.listdir(OPS_DIR)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(OPS_DIR, fn), encoding="utf-8").read()
        names = set(re.findall(r"(?:self\.api\(\)|self\._api)\.(\w+)\s*\(", src))
        if fn == "primitives.py":
            names |= set(re.findall(r"\bapi\.(\w+)\s*\(", src))
        if names:
            out[fn] = names
    return out


def ast_used_names(src):
    """源码里真正**被用到**的名字（`Name` 与 `Attribute.attr`）—— 注释/docstring 不算。

    ★ 必须用 AST：文件头里**要能写**"这些名字不许出现"这句话，文本搜索会把解释性文字当违规。
    """
    used = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def primitives_imports(src):
    """`from ops.primitives import …` 里 import 了哪些名字。"""
    got = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module == "ops.primitives":
            got |= {a.name for a in node.names}
    return got


def imported_modules(src):
    """源码里**真的 import** 了哪些模块（AST；docstring/注释里提到不算）。"""
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    return mods


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
    # JS 模板（OPS 文档 §7 步骤 2）：独立文件、占位符全部被填充、渲染走调用期（P7-S3b）
    import ops
    from ops import inject as OI
    tpl = ops.load_js_template()
    chk("JS 模板已抽成独立文件 ops/agent.js.tpl，且含占位符", len(tpl) > 10000 and "%(module)s" in tpl)
    _js = OI.render_js()
    chk("ops_inject.render_js() 没有残留占位符", "%(" not in _js and OI.MODULE[:-4].lower() in _js.lower())
    chk("ops_inject 不再 import 期烤 JS（没有 JS 属性，只有 render_js 函数）",
        not hasattr(OI, "JS") and callable(getattr(OI, "render_js", None)))
    # 拆分（D1）：门面照旧导出、每个 ops 模块 ≤ 1500 行、方法都来自 mixin（Injector 自身不再定义方法）
    for nm in ("Injector", "render_js", "MODULE", "SETTLE_HOVER", "NOTE_OFF_CARD_UNDER_CURSOR", "LOC_BOARD_FRONTLINE",
               "NEEDS_TARGET_REASONS", "sweep_frida_tmp", "play_gray_guard_on", "main", "_settle", "_action_has_card"):
        chk("ops.inject 门面导出 %s" % nm, hasattr(OI, nm))
    chk("Injector 方法全部来自 mixin", not [k for k, v in vars(OI.Injector).items() if callable(v) and not k.startswith("__")])
    big = {f: n for f in os.listdir(os.path.join(ROOT, "ops")) if f.endswith(".py")
           for n in [len(open(os.path.join(ROOT, "ops", f), encoding="utf-8").read().splitlines())] if n > 1500}
    chk("ops/*.py 单文件 ≤ 1500 行", not big, str(big))

    # ---------------- 步骤 3：L1 原语（写类 RPC 只有一个实现处）+ RPC 名一致性 ----------------
    import ops.primitives as P0
    chk("步骤 3：ops/primitives.py 存在，且读写两张原语名字表自洽",
        hasattr(P0, "WRITE_PRIMITIVES") and hasattr(P0, "READ_PRIMITIVES") and not (
            set(P0.WRITE_PRIMITIVES) & set(P0.READ_PRIMITIVES)) and
        all(hasattr(P0, n) for n in P0.WRITE_PRIMITIVES + P0.READ_PRIMITIVES),
        "写 %d / 读 %d" % (len(P0.WRITE_PRIMITIVES), len(P0.READ_PRIMITIVES)))

    rpc_uses = ops_rpc_uses()
    write_in = sorted(f for f, names in rpc_uses.items() if set(names) & set(WRITE_RPC))
    chk("步骤 3：写类 RPC 只许出现在 ops/primitives.py（手势/动词不许自己拼 JS 写调用，§2）",
        write_in == ["primitives.py"], "实际出现在：%s" % write_in)

    exports = js_exports()
    broken = {(f, n) for f, names in rpc_uses.items() for n in names if to_camel(n) not in exports}
    chk("步骤 3：ops 包里每个 RPC 属性名（经 frida「下划线→驼峰」）都**真的有 JS 导出**",
        broken == set(KNOWN_BROKEN_RPC), "对不上：%s（期望恰好 %s）"
        % (sorted(broken), sorted(KNOWN_BROKEN_RPC)))
    chk("步骤 3：上面那处**已知坏调用**逐条登记（不是「没检查」，是「检查了并且钉住」）",
        all(v for v in KNOWN_BROKEN_RPC.values()), str(sorted(KNOWN_BROKEN_RPC)))

    # ---------------- 步骤 4：只读查询模块**没有写字段的能力** ----------------
    qsrc = open(os.path.join(OPS_DIR, "query.py"), encoding="utf-8").read()
    used = ast_used_names(qsrc)
    legacy_write = {"call_raw_hold", "call_raw_writes", "write_and_call", "poke", "poke_and_call0"}
    hit = sorted((set(P0.WRITE_PRIMITIVES) | legacy_write) & used)
    chk("步骤 4：ops/query.py 的**代码**里不出现任何写原语（AST 扫 Name/Attribute，注释与 docstring 不算）",
        not hit, "命中：%s" % hit)
    imp = primitives_imports(qsrc)
    chk("步骤 4：ops/query.py 从 ops.primitives 只 import **只读**原语（写原语连 import 都不许）",
        not (imp & set(P0.WRITE_PRIMITIVES)), "多 import 了：%s" % sorted(imp & set(P0.WRITE_PRIMITIVES)))
    from ops import diag as DG
    chk("步骤 4：需要临时写字段的诊断在 ops/diag.py（且 query.py **不 import 它**，AST 判定）",
        hasattr(DG, "DiagMixin") and hasattr(DG.DiagMixin, "can_move_to_simulated")
        and hasattr(DG.DiagMixin, "probe_canplay_targeted")
        and not hasattr(__import__("ops.query", fromlist=["x"]), "DiagMixin")
        and "ops.diag" not in imported_modules(qsrc))
    chk("步骤 4：Injector 仍然组合了 DiagMixin ⇒ 老的 simulate_drag=True / probe_canplay_targeted 照旧可用",
        hasattr(OI.Injector, "can_move_to_simulated") and hasattr(OI.Injector, "probe_canplay_targeted")
        and hasattr(OI.Injector, "can_move_to"))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
