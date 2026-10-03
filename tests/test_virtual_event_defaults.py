#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组 E（NATIVE-SPEC-GAPS §6 / §0.3）：4 个蓝虚事件（BlueprintNativeEvent）的**原生默认值**。

IDA 实读（vtable 槽 +704/+752/+760/+800）⇒ 默认实现只做"最后一个出参 bool = False"：
  BlockCardFromBeingPlayedFromHand          → bLocked=False（Reason/reasonParam1/2 空串）
  OnBeforeOtherCardDeploymentTrigger        → cancelDeploymentEffect=False
  OnBeforeOtherCardLoseMobilize             → dontLoseMobilize=False
  OnBeforeOtherCardGainDefenseAfterAdd      → stopAction=False
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import types


from kardsmem.vm import Frame, VM

fails = 0


def chk(name, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


PARAMS = {
    "BlockCardFromBeingPlayedFromHand": [
        ("bLocked", True, "BoolProperty"), ("Reason", True, "StrProperty"),
        ("reasonParam1", True, "StrProperty"), ("reasonParam2", True, "StrProperty")],
    "OnBeforeOtherCardDeploymentTrigger": [
        ("cardDeploying", False, "ObjectProperty"), ("cancelDeploymentEffect", True, "BoolProperty")],
    "OnBeforeOtherCardLoseMobilize": [
        ("card", False, "ObjectProperty"), ("dontLoseMobilize", True, "BoolProperty")],
    "OnBeforeOtherCardGainDefenseAfterAdd": [
        ("cardGainingDefense", False, "ObjectProperty"), ("defenseGaining", False, "IntProperty"),
        ("stopAction", True, "BoolProperty")],
}


def invoke(name, ins=None):
    v = VM.__new__(VM)
    v.s = None
    v.hooks = {}
    v.cn = None
    v._params = {}
    v.persist = {}
    v.params_of = lambda uf: PARAMS[name]
    ps = PARAMS[name]
    f = Frame(self_obj=0x1000)
    kids, args = [], []
    for pname, is_out, _t in ps:
        if is_out:
            kids.append(types.SimpleNamespace(op="LocalOutVariable", args={"prop": "V_" + pname}))
            args.append(None)
        else:
            f.locals["I_" + pname] = (ins or {}).get(pname)
            kids.append(types.SimpleNamespace(op="LocalVariable", args={"prop": "I_" + pname}))
            args.append((ins or {}).get(pname))
    e = types.SimpleNamespace(args={"fn": name}, kids=kids)
    v._apply_virtual_default(0x1234, name, f, e, args)
    return f


f = invoke("BlockCardFromBeingPlayedFromHand")
chk("Block：bLocked=False", f.locals.get("V_bLocked"), False)
chk("Block：Reason=空串（零值）", f.locals.get("V_Reason"), "")
chk("Block：reasonParam1/2=空串", (f.locals.get("V_reasonParam1"), f.locals.get("V_reasonParam2")),
    ("", ""))
f = invoke("OnBeforeOtherCardDeploymentTrigger", {"cardDeploying": 0xAA})
chk("DeploymentTrigger：cancelDeploymentEffect=False", f.locals.get("V_cancelDeploymentEffect"), False)
f = invoke("OnBeforeOtherCardLoseMobilize", {"card": 0xAA})
chk("LoseMobilize：dontLoseMobilize=False", f.locals.get("V_dontLoseMobilize"), False)
f = invoke("OnBeforeOtherCardGainDefenseAfterAdd", {"cardGainingDefense": 0xAA, "defenseGaining": 3})
chk("GainDefenseAfterAdd：stopAction=False", f.locals.get("V_stopAction"), False)
chk("换函数答案跟着变（Block 的出参是 bLocked，不是 cancelDeploymentEffect）",
    "V_bLocked" in invoke("BlockCardFromBeingPlayedFromHand").locals
    and "V_bLocked" not in invoke("OnBeforeOtherCardDeploymentTrigger", {"cardDeploying": 1}).locals,
    True)

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
