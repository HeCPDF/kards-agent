#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""效果键审计：`effectvm` 产出的每个键都要有消费者（或写明为什么丢弃）。

为什么要有这条测试（`EVAL-ARCHITECTURE.md` §1.1 补充 1）：
    约定是"**新增规则一律写成模拟侧的状态变更，不得再新增 `eff` 摘要字典的键**"。
    光靠约定会被忘掉 —— 这条测试把"产出但没有消费者"的键直接顶出来：新增的键要么
    有消费分支（`sim/engine.py` 等 `sim/*.py` / `player/rule.py`），要么必须登记进 `TOLERATED` 并写清原因。

口径与限制（如实写）：
  * 静态扫描字符串字面量：`dst["k"] =` / `dst.setdefault("k"` 算产出；
    `e.get("k")` / `e["k"]` / `"k" in e` 算消费（扫 `agent/*.py` + `sim/*.py`）。
  * **动态拼出来的键**（如 `"opp_" + key`）扫不到 ⇒ 这条测试是下限，不是全覆盖。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 产出但**故意**不消费：必须写明原因（谁改这里谁负责解释）。
TOLERATED = {
    "self_damage": "打刚部署的那张牌自己 —— 它此刻还没进 sim，按设计丢弃"
                   "（effectvm.py 的 damage 分支注释）",
    "gain_tmp": "与 gain_names 同序的出参临时 id：只在 `to_effects` 内部把『对新牌的改动』并回 gain_ops 时用（sim 不消费，"
                "消费的是 gain_ops）",
}

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def main():
    # P2（2026-10-03）：真实现已端口进 engine/effectvm.py，semantics/effectvm.py 只剩别名壳 ——
    # 按模块的 __file__ 解析，别再写死路径（否则扫到壳、产出键恒 0）。
    import engine.effectvm as _EV
    src = open(_EV.__file__, encoding="utf-8").read()
    produced = set(re.findall(r'dst2?\["([a-z_0-9]+)"\]', src))
    produced |= set(re.findall(r'dst2?\.setdefault\("([a-z_0-9]+)"', src))

    consumed = set()
    # P5（2026-10-04）：`policy/boardeval.py` 兼容门面已删 —— 消费点就是 `sim/` 与 `player/rule.py`
    #（原先扫门面是因为真实现还在它里面；现在真实现全在 `sim/engine.py` 等，glob 已经覆盖）。
    files = [os.path.join(ROOT, "player", "rule.py")]
    files += glob.glob(os.path.join(ROOT, "sim", "*.py"))
    for p in files:
        s = open(p, encoding="utf-8").read()
        consumed |= set(re.findall(r'e\.get\("([a-z_0-9]+)"', s))
        consumed |= set(re.findall(r'e\["([a-z_0-9]+)"\]', s))
        consumed |= set(re.findall(r'"(?:[a-z_0-9]+)"\s+in\s+e\b', s))
        consumed |= set(re.findall(r'eff\.get\("([a-z_0-9]+)"', s))
        consumed |= set(re.findall(r'\b([a-z_0-9]+)"\s*in\s*eff\b', s))

    chk("扫到产出键（effectvm 还在被解析）", len(produced) >= 25, "produced=%d" % len(produced))
    left = sorted(produced - consumed - set(TOLERATED))
    chk("没有'产出但没人消费'的键", not left,
        "未消费=%s（新增键必须接消费分支，或登记 TOLERATED 并写原因）" % left)
    chk("TOLERATED 里的键确实还在产出（清掉过期条目）",
        set(TOLERATED) <= produced, "过期=%s" % sorted(set(TOLERATED) - produced))
    for k, why in TOLERATED.items():
        chk("丢弃理由写清楚：%s" % k, len(why) > 12, why[:40])

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
