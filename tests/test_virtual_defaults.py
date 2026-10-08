#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚函数默认值接线（`vm.py` 第 5 步）的离线断言（不需要游戏）。

依据 `NATIVE-COVERAGE-1.60.md` §3/§6.1：**有覆写跑覆写字节码；没覆写跑原生默认实现**
（默认实现只写出参：`GetPlayFromHandDamage → Damage=0`、`CanBeTargetted → canIt=True`、
回显型如 `OnCardDealDamage_ModifyDamageDealt → newDamage=Damage`）。

每条都换函数/换入参，答案要跟着变（同类教训）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import types


from kardsmem import vm as vm_mod                                  # noqa: E402
from kardsmem.kismetlib import Unimplemented                       # noqa: E402
from kardsmem.virtual_defaults import VIRTUAL_DEFAULTS, selftest as vd_selftest  # noqa: E402
from kardsmem.vm import Frame, VM                                  # noqa: E402

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def chk_raises(name, fn, needle=""):
    global bad
    try:
        fn()
    except Unimplemented as e:
        ok = needle in str(e)
        bad += 0 if ok else 1
        print("  [%s] %-58s raised=%r" % ("PASS" if ok else "FAIL", name, str(e)[:70]))
        return
    except Exception as e:                                          # noqa: BLE001
        bad += 1
        print("  [FAIL] %-58s wrong exception %s" % (name, e))
        return
    bad += 1
    print("  [FAIL] %-58s did not raise" % name)


# SDK 形参（1.60.27292.launcher `kards_classes.hpp` 原样）
PARAMS = {
    "GetPlayFromHandDamage": [("targetCard", False, "ObjectProperty"),
                              ("Damage", True, "IntProperty")],
    "OnCardDealDamage_ModifyDamageDealt": [
        ("toCard", False, "ObjectProperty"), ("Damage", False, "IntProperty"),
        ("fromAttack", False, "BoolProperty"), ("fromFight", False, "BoolProperty"),
        ("newDamage", True, "IntProperty")],
    "OnOtherCardAttackSwitchTarget": [
        ("cardAttacking", False, "ObjectProperty"), ("oldDefender", False, "ObjectProperty"),
        ("newDefender", True, "ObjectProperty")],
    "CanPlayFromHand": [
        ("canIt", True, "BoolProperty"), ("Reason", True, "StrProperty"),
        ("reasonParam1", True, "StrProperty"), ("reasonParam2", True, "StrProperty"),
        ("targetedCard", True, "ObjectProperty")],
    "GetChooseSpawnCards": [
        ("Cards", True, "ArrayProperty"), ("MarkAsSeen", True, "BoolProperty"),
        ("keepOrder", True, "BoolProperty")],
    "OnSuppressed": [],
}


def make_vm(name, params=None):
    v = VM.__new__(VM)
    v.s = None
    v.hooks = {}
    v.cn = None
    v._params = {}
    v.persist = {}
    v.params_of = lambda uf: (params if params is not None else PARAMS[name])
    return v


def invoke(name, ins=None, params=None):
    """调 `_apply_virtual_default`。返回 frame —— 出参写成 `f.locals['V_<形参名>']`。"""
    ins = dict(ins or {})
    v = make_vm(name, params)
    ps = v.params_of(0)
    f = Frame(self_obj=0x1000)
    kids, args = [], []
    for pname, is_out, _t in ps:
        if is_out:
            kids.append(types.SimpleNamespace(op="LocalOutVariable", args={"prop": "V_" + pname}))
            args.append(None)
        else:
            f.locals["I_" + pname] = ins.get(pname)
            kids.append(types.SimpleNamespace(op="LocalVariable", args={"prop": "I_" + pname}))
            args.append(ins.get(pname))
    e = types.SimpleNamespace(args={"fn": name}, kids=kids)
    v._apply_virtual_default(0x1234, name, f, e, args)
    return f


def main():
    print("== A. 表结构与 1.60 SDK 形参名对齐 ==")
    bad_before = vd_selftest()
    chk("virtual_defaults selftest", bad_before, 0)

    print("== B. 默认实现 = 只写出参（值/回显/零值） ==")
    f = invoke("GetPlayFromHandDamage", {"targetCard": 0xABC})
    chk("GetPlayFromHandDamage: 不覆写 ⇒ Damage=0", f.locals.get("V_Damage"), 0)
    f = invoke("OnCardDealDamage_ModifyDamageDealt",
               {"toCard": 1, "Damage": 5, "fromAttack": True, "fromFight": False})
    chk("ModifyDamageDealt: 回显 Damage", f.locals.get("V_newDamage"), 5)
    f = invoke("OnOtherCardAttackSwitchTarget", {"cardAttacking": 1, "oldDefender": 0xDEAD})
    chk("AttackSwitchTarget: newDefender=oldDefender", f.locals.get("V_newDefender"), 0xDEAD)
    f = invoke("CanPlayFromHand")
    chk("CanPlayFromHand: canIt=True", f.locals.get("V_canIt"), True)
    chk("CanPlayFromHand: 表外输出参数写零值（Reason=''）", f.locals.get("V_Reason"), "")
    chk("CanPlayFromHand: targetedCard=0", f.locals.get("V_targetedCard"), 0)
    f = invoke("GetChooseSpawnCards")
    chk("GetChooseSpawnCards: Cards=[]", f.locals.get("V_Cards"), [])
    chk("GetChooseSpawnCards: 其余出参零值", [f.locals.get("V_MarkAsSeen"),
                                              f.locals.get("V_keepOrder")], [False, False])

    print("== C. 表和 SDK 对不上时如实停（不写错出参） ==")
    try:
        invoke("GetPlayFromHandDamage", {"targetCard": 1},
               params=[("targetCard", False, "ObjectProperty")])   # 少一个 Damage 出参
        globals()["bad"] += 1
        print("  [FAIL] 表里多出的形参没被发现")
    except Unimplemented as e:
        chk("名字对不上 ⇒ Unimplemented", "对不上" in str(e), True)

    print("== D. vm.call() 第 5 步的选择：覆写优先、默认兜底、NO_EXECUTE 不执行 ==")
    real_kismet = vm_mod.kismet

    class FakeK:
        @staticmethod
        def find_function(sess, uc, name):
            return 0x1234

        @staticmethod
        def script_of(sess, uf):
            return None if FakeK.no_bytecode else b"code"
        no_bytecode = True

    vm_mod.kismet = FakeK
    try:
        sess = types.SimpleNamespace(m=types.SimpleNamespace(ptr_or_zero=lambda a: 0x2000))
        e = types.SimpleNamespace(
            args={"fn": "GetPlayFromHandDamage"},
            kids=[types.SimpleNamespace(op="LocalVariable", args={"prop": "I_targetCard"}),
                  types.SimpleNamespace(op="LocalOutVariable", args={"prop": "V_Damage"})])
        v = make_vm("GetPlayFromHandDamage")
        v.s = sess
        f = Frame(self_obj=0x1000)
        f.locals["I_targetCard"] = 7
        chk("没覆写 ⇒ 走默认（Damage=0）", (v.call(e, f), f.locals.get("V_Damage")), (None, 0))

        FakeK.no_bytecode = False                      # 有覆写 ⇒ 必须跑字节码，默认值不能抢先
        v2 = make_vm("GetPlayFromHandDamage")
        v2.s = sess
        v2.call_bytecode = lambda uf, ff, obj, a, ee: "BYTECODE"
        f2 = Frame(self_obj=0x1000)
        chk("有覆写 ⇒ 跑覆写（默认值不抢先）", v2.call(e, f2), "BYTECODE")
        FakeK.no_bytecode = True

        e_unknown = types.SimpleNamespace(args={"fn": "SomeUnknownVirtual"}, kids=[])
        f3 = Frame(self_obj=0x1000)
        _v3 = make_vm("GetPlayFromHandDamage")
        _v3.s = sess
        chk_raises("不在表里 ⇒ 仍如实停（Unimplemented）",
                   lambda: _v3.call(e_unknown, f3), "函数 SomeUnknownVirtual")
        e_sup = types.SimpleNamespace(args={"fn": "OnSuppressed"}, kids=[])
        _v4 = make_vm("OnSuppressed")
        _v4.s = sess
        chk_raises("OnSuppressed 默认实现有副作用 ⇒ 不执行",
                   lambda: _v4.call(e_sup, Frame(self_obj=0x1000)), "有副作用")
    finally:
        vm_mod.kismet = real_kismet

    print("== E. find_function 缓存（虚调用热路径；含未命中与换进程） ==")
    import kardsmem.kismet as kismet_mod
    calls = {"n": 0}
    real_functions = kismet_mod.functions
    kismet_mod.functions = (lambda sess, uc:
                            (calls.__setitem__("n", calls["n"] + 1)
                             or [{"name": "Foo", "addr": 0x9000}]))
    kismet_mod._FF_CACHE.clear()
    try:
        class FakeSess:
            pid = 4242
            base = 0x140000000
            m = types.SimpleNamespace(ptr_or_zero=lambda a: 0)

        s = FakeSess()
        chk("find_function 命中（两次同值）",
            (kismet_mod.find_function(s, 0x1234, "Foo"),
             kismet_mod.find_function(s, 0x1234, "Foo")), (0x9000, 0x9000))
        chk("第二次不再扫函数链（缓存生效）", calls["n"], 1)
        chk("未命中也被缓存（返回 None，扫一次）",
            (kismet_mod.find_function(s, 0x5678, "Bar"),
             kismet_mod.find_function(s, 0x5678, "Bar"), calls["n"]), (None, None, 2))
        s2 = FakeSess()
        s2.base = 0x150000000                    # 换进程（ASLR 变）⇒ 旧缓存不能复用
        kismet_mod.find_function(s2, 0x1234, "Foo")
        chk("换 base ⇒ 重新扫（不复用旧进程的地址）", calls["n"], 3)
    finally:
        kismet_mod.functions = real_functions
        kismet_mod._FF_CACHE.clear()

    print("")
    print("%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
