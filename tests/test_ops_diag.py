#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 4 的判据：**只读查询模块没有写字段的能力**（`OPS-ARCHITECTURE.md` §2 旁路 / §7）。

背景（弯路 #23）：`can_move_to(simulate_drag=True)` 为了问 `CanMoveCardToLocation`，
临时把 `PC->SelectedCard`（手柄/拖拽态字段）指到那张卡上 —— 那是**写**，却长在只读查询模块里。
"写一个『读』之前先问：它答得出来是靠我写了什么吗？"

这里把两件事分开钉住：

  ① **结构上**（import 期）：`ops/query.py` 从 `ops.primitives` **只 import 只读原语**；
     写原语的名字**不在它的命名空间里** ⇒ 想"顺手临时写个字段"是**写不出来**的（不是靠自觉）。
     判据见 `test_ops_rules.py` 的同名 lint（扫源码）；这里直接 `importlib` 之后查属性。
  ② **行为上**（假 self 离线跑）：
     * 只组合 `QueryMixin`（**没有** `DiagMixin`）的对象，`can_move_to(simulate_drag=True)`
       必须**一次写 RPC 都不发**，并如实回 `reason="diag_not_composed"`（不是假装有答案）；
     * 组合了 `DiagMixin`（= 真正的 `Injector`）时，写路径**只**在 diag 里发生
       （`callRawArrHold` = "写完→调用→还原"，同一次 JS 执行，弯路 #19）。
  ③ `probe_canplay_targeted`（另一个"临时写 `targetOverride` 再问"的诊断）已经搬到 diag，
     并且**返回值逐字不变**（没有偷偷加解释性字段 —— 那是契约变更，不该混在搬家这一步里）。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import diag as OD                                               # noqa: E402
from ops import primitives as P                                          # noqa: E402
from ops import query as OQ                                              # noqa: E402

fails = 0
WRITE_RPCS = ("callRawArrHold", "writeCallRaw", "writeThenCalls", "writeAndCall2", "write_and_call", "poke")
#: `ops/primitives` 之外、**同样算写**的老入口（`conn.ConnMixin` 上的两个原语转发）。
LEGACY_WRITE_NAMES = ("call_raw_hold", "call_raw_writes", "write_and_call", "poke", "poke_and_call0")


def scan_write_usage(src):
    """AST 扫源码：返回 (用到的写原语名 sorted, 从 ops.primitives 多 import 的写原语名 sorted)。

    ★ 用 AST 而不是文本搜索：文件头/docstring 里**必须**能写"这些名字不许出现"这句话，
      文本搜索会把解释性文字当成违规（假阳性）。
    """
    tree = ast.parse(src)
    used, bad_imports = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "ops.primitives":
            for a in node.names:
                if a.name in P.WRITE_PRIMITIVES:
                    bad_imports.add(a.name)
    bad = sorted((set(P.WRITE_PRIMITIVES) | set(LEGACY_WRITE_NAMES)) & used)
    return bad, sorted(bad_imports)


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class FakeApi:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _buf():
        b = bytearray(0x40)
        b[0x00] = 1        # CanPlayFromHand 的 canIt
        b[0x10] = 1        # CanMoveCardToLocation 的 bResult 出参
        return bytes(b)

    def _rec(self, name, *a):
        self.calls.append((name,) + a)
        if name in ("callRawArrHold", "callRawArr"):
            return self._buf()
        return []

    def call_raw(self, obj, func, parms):
        return self._rec("call_raw", obj, func, parms)

    def callRawArrHold(self, writes, obj, func, parms, arr_off, ptrs):
        return self._rec("callRawArrHold", writes, obj, func, parms, arr_off, ptrs)

    def callRawArr(self, obj, func, parms, arr_off, ptrs):
        return self._rec("callRawArr", obj, func, parms, arr_off, ptrs)


class FakeProps:
    """只回答"这个字段在哪" —— 假内存，不碰游戏（偏移用真值：`SelectedCard // 0x0950`）。"""

    OFFS = {"SelectedCard": 0x950, "cardUnderCursor": 0x1A0, "LocationNumberUnderCursor": 0x1A4,
            "LocationUnderCursor": 0x1AC, "RowUnderCursor": 0x1AD}

    def find_prop(self, ks, cls, name, pool=None):
        off = self.OFFS.get(name)
        return None if off is None else {"offset": off}


class FakeM:
    def ptr(self, a):
        return 0


class Base:
    """共用的假 self：只实现 `can_move_to` 真正要用的那几个依赖。"""

    def __init__(self):
        self._api = FakeApi()
        self.ks, self.pool, self.m = object(), object(), FakeM()

    def api(self):
        return self._api

    def _bp_lib_fn(self, cls_name, fn_name):
        return 0x11, 0x22, 0x33

    def board_actor_of(self, card_id):
        return 0x4444

    def uclass_of_instance(self, obj):
        return 0x5555

    # --- 给 probe_canplay_targeted 用的那几个 ---
    def _card_object(self, card_id):
        return 0x7000 + card_id

    def find_fn(self, cls, name, inherited=True):
        return 0x999

    def off(self, cls, name, note):
        return 0x548

    def call_raw_hold(self, writes, obj, func, parms, arr_off=None, ptrs=()):
        return P.write_then_call(self.api(), writes, obj, func, parms, arr_off, ptrs)


class ReadOnly(Base, OQ.QueryMixin):
    """**故意**只组合只读查询 mixin —— 模拟"有人想拿一个纯只读对象".."""


class Full(Base, OD.DiagMixin, OQ.QueryMixin):
    """`Injector` 的那种组合（diag 在）。"""


class NoCards(Base, OD.DiagMixin):
    """`_card_object` 恒拿不到东西的对象（测失败分支）。"""

    def _card_object(self, card_id):
        return 0


def write_calls(api):
    return [c for c in api.calls if c[0] in WRITE_RPCS]


def main():
    # 假内存：`ops/diag.py` 里的 `props.find_prop` / `player_controller` 换成假的（不 attach、不碰游戏）
    real_props, real_pc = OD.props, OD.player_controller
    OD.props, OD.player_controller = FakeProps(), (lambda ks: 0x6666)
    try:
        return _main()
    finally:
        OD.props, OD.player_controller = real_props, real_pc


def _main():
    # ---------------------------------------------------------- ① 结构
    chk("① ops/query.py 的命名空间里**没有任何写原语**（import 期就没有，不是靠自觉）",
        not [n for n in P.WRITE_PRIMITIVES if hasattr(OQ, n)],
        str([n for n in P.WRITE_PRIMITIVES if hasattr(OQ, n)]))
    chk("① ops/query.py 从 primitives 只 import 了**只读**原语（名字逐个登记）",
        OQ.call_ufunction is P.call_ufunction and OQ.call_out_u8_batch is P.call_out_u8_batch
        and not hasattr(OQ, "P"))
    src = open(os.path.join(ROOT, "ops", "query.py"), encoding="utf-8").read()
    bad_names, bad_imports = scan_write_usage(src)
    chk("① ops/query.py 的**代码**里不出现写原语（AST 扫 Name/Attribute；注释与 docstring 不算）",
        not bad_names, str(bad_names))
    chk("① `from ops.primitives import …` 的每个名字都必须是**只读**原语（写原语连 import 都不许）",
        not bad_imports, str(bad_imports))
    chk("① 另一个写型诊断 `probe_canplay_targeted` 已不在 query.py、在 diag.py",
        not hasattr(OQ.QueryMixin, "probe_canplay_targeted") and hasattr(OD.DiagMixin, "probe_canplay_targeted"))
    chk("① diag.py 才是允许写的地方：它 import 的 consts 里有 targetOverride 的偏移",
        "NOTE_OFF_CARDOBJ_TARGET_OVERRIDE" in open(os.path.join(ROOT, "ops", "diag.py"),
                                                  encoding="utf-8").read())

    # ---------------------------------------------------------- ② 只读对象：不写、也不假装有答案
    ro = ReadOnly()
    res = ro.can_move_to(30, simulate_drag=True)
    chk("② 没组合 DiagMixin ⇒ **一次写 RPC 都不发**（只读对象就是写不出来）",
        write_calls(ro.api()) == [], str(ro.api().calls))
    chk("② 没组合 DiagMixin ⇒ 如实回 diag_not_composed（不假装能答）",
        res["ok"] is False and res["reason"] == "diag_not_composed" and res["can"] is None, str(res))

    ro2 = ReadOnly()
    d = ro2.can_move_to(30)                     # 默认 simulate_drag=False
    chk("② 默认路径（simulate_drag=False）：不写、且明确回「没有拖拽态问不出来」（弯路 #23）",
        write_calls(ro2.api()) == [] and d["reason"] == "cannot_ask_without_drag" and d["can"] is None,
        str(ro2.api().calls) + " " + str(d))
    chk("② 默认路径不 import/不需要 diag 也能答（老调用方 `precheck.can_move_to` 不受影响）",
        "simulated_drag" in d and d["simulated_drag"] is False)

    # ---------------------------------------------------------- ③ 组合了 diag：写在 diag 里
    full = Full()
    r3 = full.can_move_to(30, simulate_drag=True)
    calls = write_calls(full.api())
    chk("③ 组合了 DiagMixin ⇒ 写路径走 diag：**恰好一次** `callRawArrHold`（写完→调用→还原，同一次 JS 执行）",
        len(calls) == 1 and calls[0][0] == "callRawArrHold", str([c[0] for c in full.api().calls]))
    w = calls[0][1] if calls else []
    chk("③ 写的是「真实鼠标按下时会写的那几个字段」+ `SelectedCard // 0x0950` 指向这张卡的 actor",
        {x[2] for x in w} <= {"ptr", "s32", "u8"}
        and ["0x6666", 0x950, "ptr", "0x4444"] in w          # 指针走 hex 字符串（encode_writes）
        and len(w) == 5, str(w))
    chk("③ 结果**自报**写了哪个字段（`writes_gamepad_field`）+ simulated_drag=True（别接进判决路径）",
        r3.get("simulated_drag") is True and "SelectedCard" in (r3.get("writes_gamepad_field") or "")
        and r3.get("ok") is True and r3.get("can") is True, str(r3))

    # ---------------------------------------------------------- ④ probe_canplay_targeted：搬家不动契约
    full2 = Full()
    p = full2.probe_canplay_targeted(30, 55, verbose=False)
    wc = write_calls(full2.api())
    chk("④ probe_canplay_targeted 在 diag 里，并且**它确实写** targetOverride（写完→问→还原）",
        len(wc) == 1 and wc[0][0] == "callRawArrHold"
        and ["0x701e", 0x548, "ptr", "0x7037"] in wc[0][1]         # src=0x7000+30, tgt=0x7000+55
        and wc[0][5] == -1, str(wc)[:160])          # 元组是 (rpc名, writes, obj, func, parms, arr_off, ptrs)
    chk("④ probe_canplay_targeted 的**返回值逐字不变**（没有偷偷加 writes_*/note 这类解释字段）",
        set(p.keys()) == {"ok", "card", "target", "canIt", "source", "target_override_after"}, str(sorted(p)))
    chk("④ probe_canplay_targeted 拿不到 src/tgt 时的失败字典也照旧",
        Full()._card_object(0) == 0x7000
        and "stopped" in NoCards().probe_canplay_targeted(1, 2, False))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
