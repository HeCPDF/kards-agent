#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""随机 / 抉择的评估口径（用户 2026-10-06 拍板，`docs/REFACTOR-PLAN.md` §5.7）：

* 随机结果**不是分支**：有牌局随机流种子 ⇒ 用 `kardsmem.rng.Stream`（FRandomStream 的 LCG）算出**具体结果**；
  枚举只在读不到种子时兜底；有种子却仍有没接进流的随机点 ⇒ 如实 skipped，不枚举成分支；
* 抉择（二选一）**拆成独立动作**：影子对账逐选项各比一次（`_shadow_branches` 的 `[选项i]` 条目），与搜索里 `expand_paths` 同口径。

`_shadow_check` 端到端的断言在 `test_shadow_check.py`；这里钉 `_shadow_branches` 的分类规则、`enumerate_effects` 把种子往下传、
以及录制器在有流时对三种随机原语给出**确定**结果（两次同种子结果相同、抽取次数 = BP 包装的 d+2）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _hookfake import Checker                                        # noqa: E402
import player.rule as R                                              # noqa: E402
import semantics.effectvm as EV                                      # noqa: E402
from kardsmem.rng import Stream                                      # noqa: E402

chk = Checker()


def main():
    sb = R._shadow_branches
    base = {"complete": True, "chance": [], "nodes": [], "choice": False}
    # 没有随机点/抉择点 ⇒ 单条目、不分支
    br, why = sb(dict(base), {"kredit": 1}, 7)
    chk("没有随机/抉择 ⇒ 单条目（forced=None、无标签）", br == [(None, "", {"kredit": 1})] and why is None, str(br))
    # 确定随机（exact：不进 chance/nodes）⇒ 仍是单条目
    br, _ = sb(dict(base, exact=True), {"kredit": 1}, 7)
    chk("确定随机（exact）⇒ 单条目，不是分支", len(br) == 1 and br[0][1] == "", str(br))
    # 抉择：两个选项 ⇒ 两个独立条目，A 路 = 公共部分并上该选项；标签 [选项i]
    eff = {"outcomes": [(0.5, {"opcost": 0}), (0.5, {"buff": [4, 4]})], "outcomes_mode": "max", "_src": "vm"}
    r = dict(base, choice=True, nodes=[{"verb": "WhichChooseOne", "size": 2}])
    br, why = sb(r, eff, 7)
    chk("抉择 ⇒ 每个选项一个条目（forced=[0]/[1]，标签 [选项0]/[选项1]）",
        [(f, lb) for f, lb, _ in br] == [([0], "[选项0]"), ([1], "[选项1]")] and why is None, str(br))
    chk("抉择：A 路 = 公共部分（_src）并上该选项自己的效果，不带 outcomes",
        br[1][2] == {"_src": "vm", "buff": [4, 4]} and "outcomes" not in br[0][2], str(br))
    # 两个抉择点（2×3）⇒ 6 条路径，下标组合
    eff2 = {"outcomes": [(1 / 6, {"kredit": i}) for i in range(6)], "outcomes_mode": "max"}
    r2 = dict(base, choice=True, nodes=[{"verb": "WhichChooseOne", "size": 2}, {"verb": "WhichChooseOne", "size": 3}])
    br, _ = sb(r2, eff2, 7)
    chk("两个抉择点 ⇒ 6 条路径，标签带两个下标", len(br) == 6 and br[4][1] == "[选项1,1]" and br[4][0] == [1, 1], str([(f, l) for f, l, _ in br]))
    # A 路的分支数对不上 ⇒ 不乱比
    br, why = sb(r, {"outcomes": [(1.0, {"x": 1})]}, 7)
    chk("outcomes 数与路径数对不上 ⇒ (None, 原因)", br is None and "抉择" in why, str(why))
    br, why = sb(dict(r, complete=False), eff, 7)
    chk("VM 没跑完 ⇒ (None, 原因)（由调用方记真正的停因）", br is None, str(why))
    # 随机：有种子 ⇒ 不枚举；残留随机点点名动词、记 skipped
    rr = dict(base, chance=["RandomIntegerInRange"], nodes=[{"verb": "RandomIntegerInRange", "size": 3}])
    br, why = sb(rr, {"outcomes": [(1 / 3, {"damage": i}) for i in range(3)]}, 7)
    chk("有种子 + 残留随机点 ⇒ 不枚举（None）且原因点名动词", br is None and "RandomIntegerInRange" in why, str(why))
    # 随机：读不到种子 ⇒ 枚举兜底（标签 [随机分支i]）；超 cap ⇒ None
    br, why = sb(rr, {"outcomes": [(1 / 3, {"damage": i}) for i in range(3)]}, None, cap=4)
    chk("无种子 ⇒ 枚举兜底：3 个条目，标签 [随机分支i]", br is not None and [lb for _f, lb, _ in br] == ["[随机分支0]", "[随机分支1]", "[随机分支2]"], str(br))
    br, why = sb(rr, {"outcomes": [(1 / 3, {"damage": i}) for i in range(3)]}, None, cap=2)
    chk("无种子 + 超 cap ⇒ None", br is None, str(why))
    # 抉择 + 残留随机（有种子）⇒ 同样不把随机当选项
    rm = dict(base, choice=True, chance=["RandomIntegerInRange"],
              nodes=[{"verb": "WhichChooseOne", "size": 2}, {"verb": "RandomIntegerInRange", "size": 3}])
    br, why = sb(rm, {"outcomes": [(1 / 6, {"kredit": i}) for i in range(6)], "outcomes_mode": "max"}, 7)
    chk("抉择 + 没接进流的随机（有种子）⇒ None：不把随机结果混进『选项』", br is None, str(why))

    # enumerate_effects：种子往下传；确定随机的 run 没有随机 nodes ⇒ 只展开抉择
    seen = []

    def runner(km, ptr, tptr, ht, hook, side, forced, **kw):
        seen.append((forced, kw.get("rng_seed")))
        pick = (forced or [0])[0]
        return {"eff": {"kredit": pick + 1}, "complete": True, "stopped": None, "choice": True, "exact": True, "draws": 5,
                "chance": [], "nodes": [{"verb": "WhichChooseOne", "size": 2}]}
    en = EV.enumerate_effects(None, 1, runner=runner, rng_seed=1234)
    chk("enumerate_effects：只展开抉择（2 个分支）、种子传给每次空跑、exact 透出",
        len(en["outcomes"]) == 2 and all(sd == 1234 for _f, sd in seen) and en["exact"] and en["choice"], str(en) + str(seen))

    # 录制器在有流时：同种子两次结果相同（确定、不枚举），且不登记随机节点
    class _V:                       # 最小假 VM 帧：只测 Recorder.hook 的取值，不跑字节码
        pass

    class _E:
        def __init__(self):
            self.kids = []

    def run_hook(seed):
        rec = EV.Recorder(1, 0)
        rec.stream = Stream(seed)
        got = []
        for _ in range(3):
            got.append(rec.hook("RandomIntFromRangeWithStream")(_V(), None, 1, [0, 9], _E()))
        arr = ["a", "b", "c", "d"]
        got.append(rec.hook("GetRandomCard")(_V(), None, 1, [arr], _E()))
        return got, rec
    g1, rec1 = run_hook(99)
    g2, rec2 = run_hook(99)
    chk("同种子 ⇒ 随机结果相同（确定）", g1[:3] == g2[:3], str((g1, g2)))
    chk("有流 ⇒ 不登记随机节点/机会节点（不枚举、不污染）", not rec1.nodes and not rec1.chance and not rec1.tainted and rec1.exact)
    ref = Stream(99)
    exp = [ref.wrapper_int(0, 9) for _ in range(3)]
    chk("结果 = 游戏 LCG 的 BP 包装（d+2 次抽取）", g1[:3] == exp and rec1.stream.draws >= 3 * 3, str((g1, exp, rec1.stream.draws)))
    return chk.done() if hasattr(chk, "done") else 0


if __name__ == "__main__":
    sys.exit(main())
