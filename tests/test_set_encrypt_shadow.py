#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组 D（NATIVE-SPEC-GAPS §5）：`setAndEncrypt*` 写侧影子 → 读侧优先 → 效果 → 模拟消费。"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from policy import boardeval as BE
from semantics import effectvm as EV
from kardsmem import cardnatives as CN

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


# ---- 1) 写侧影子 + to_effects ----
r = EV.Recorder(0x1000, 0)
r.ptr_ids[0x1000] = 7
r.hook("setAndEncryptDefense")(None, None, 0x1000, [5, 111, 222, 333], None)
e = EV.to_effects(r, my_side=1)
chk("写侧：setAndEncryptDefense(5) ⇒ set_defense={7:5}", e.get("set_defense") == {7: 5}, str(e))
r2 = EV.Recorder(0x2000, 0)
r2.ptr_ids[0x2000] = 9
r2.hook("setAndEncryptAttack")(None, None, 0x2000, [4, 1, 1, 1], None)
r2.hook("setAndEncryptKredit")(None, None, 0x2000, [2, 1, 1, 1], None)
e2 = EV.to_effects(r2, my_side=1)
chk("写侧：同卡多字段（attack/kredit）",
    e2.get("set_attack") == {9: 4} and e2.get("set_kredit") == {9: 2}, str(e2))

# ---- 2) 读侧：影子优先（records_raw 也让位）----
c = {"card_type": "infantry", "shadow_stats": {"defense": 5}, "defense": 3,
     "records_raw": {"defense": (1939, 1939, 0, 0, 0)}, "key": 1939}
cn = CN.CardNatives(None)
chk("读侧：_stat 影子优先（5 而非 3/0）", CN._stat(c, "defense") == 5, str(CN._stat(c, "defense")))
chk("读侧：getTotalDefense 走影子并夹取", cn.call("getTotalDefense", c) == 5)
chk("读侧：影子 120 ⇒ clamp 99",
    cn.call("getTotalDefense", dict(c, shadow_stats={"defense": 120})) == 99)

# ---- 3) 模拟消费 ----
s = BE.Sim({1: BE.U(1, "local", "back", 2, 2, 2, "infantry")},
           {"local": 20, "enemy": 20}, 5.0, {10: BE.H(10, "O", 3, "order")})
BE._apply_eff(s, {"set_defense": {1: 5}}, None)
chk("模拟：set_defense 落到单位（2 → 5）", s.units[1].dfn == 5, str(s.units[1].dfn))
BE._apply_eff(s, {"set_attack": {1: 120}}, None)
chk("模拟：set_attack 夹到 99", s.units[1].atk == 99, str(s.units[1].atk))
BE._apply_eff(s, {"set_attack": {1: -5}}, None)
chk("模拟：set_attack 夹到 0", s.units[1].atk == 0, str(s.units[1].atk))
BE._apply_eff(s, {"set_kredit": {10: 7}}, None)
chk("模拟：set_kredit 落到手牌费用（3 → 7）", s.hand[10].cost == 7, str(s.hand[10].cost))
BE._apply_eff(s, {"set_attack_buff": {1: 3}}, None)
chk("模拟：set_attack_buff 记缺口、不硬塞总量",
    any("set_attack_buff" in g for g in s.gaps), str(s.gaps))

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
