# -*- coding: utf-8 -*-
"""建 sim 作用域里的 VM 空跑逐次诊断（`readscope.Scope.hot` + `effectvm._profiled`）：离线、假函数。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from engine import effectvm as EV                                   # noqa: E402
from kardsmem import readscope as RS                                # noqa: E402

bad = 0


def chk(name, ok):
    global bad
    bad += 0 if ok else 1
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


# 包装器保留签名与名字（别的代码按 inspect.signature / 名字找它）
import inspect                                                      # noqa: E402
chk("record_effects 包装后签名不变", "hook" in inspect.signature(EV.record_effects).parameters)
chk("  名字不变", EV.record_effects.__name__ == "record_effects")

calls = []


def fake(km, card_ptr, target_ptr=0, has_target=False, hook="OnPlayedFromHand", **kw):
    sc = RS.current()
    if sc is not None and hook == "OnLeaveBoardOrOwner":
        sc.misses["view"] = sc.misses.get("view", 0) + 82         # 这一次新读了 82 张卡视图
    calls.append(hook)
    return {"stopped": "x" if hook == "OnBoom" else None}


prof = EV._profiled(fake)
r = prof(None, 0x10, 0, False, "OnOther")
chk("作用域外：照常返回、不记账", r == {"stopped": None} and RS.current() is None)
with RS.build_scope() as sc:
    prof(None, 0x20, 0, False, "OnLeaveBoardOrOwner")
    prof(None, 0x30, hook="OnOther")
    prof(None, 0x40, 0, False, "OnBoom")
    h = sc.hot()
chk("作用域内逐次记账（共 3 次）", h["n"] == 3)
w = h["worst"]
chk("按钩子汇总：OnLeaveBoardOrOwner 的 vmiss=82",
    h["by_hook"]["OnLeaveBoardOrOwner"][2] == 82 and h["by_hook"]["OnLeaveBoardOrOwner"][0] == 1)
chk("worst 里带卡指针与视图未命中数", any(x["ptr"] == 0x20 and x["vmiss"] == 82 for x in w))
chk("停因进 worst", any(x["hook"] == "OnBoom" and x["stopped"] == "x" for x in w))
chk("关键字传 hook 也能记到钩子名", any(x["ptr"] == 0x30 and x["hook"] == "OnOther" for x in w))

print("FAILED" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
