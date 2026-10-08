#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`engine.natives.stats` 的 **verb 级原生**（P4 第三刀）：值语义写在状态上。

原版三处同构（`ChangeAttack` `:10981-11212` / `ChangeOperationCost` `:8892-9188` /
`ChangeHeavyArmor` `:10320-10507`）：**基础字段 + 独立 buff 字段**两个字段，
`0/4` 写 buff（不夹）、`1` 写基础（`clamp(基础+amount)`，`amount==0` 是 no-op）、
`2/3/5` 把基础设成 `clamp(amount)`、`6-9` 不改数值；**总量永远是 `clamp(基础 + buff)`**。

这里的 `unit.atk`/`unit.opc`/`unit.armor` 都是**总量**（适配器读 `getTotal*`），
所以每个判据都用"总量 + buff 字段"两个量来钉，并且**看基础值有没有被写对**（P3 时只发总量增量，
这正是直跑要换掉的地方）。每个分支给两个会产生不同结论的输入（弯路 #11）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import state as S                                        # noqa: E402
from engine.natives import stats as ST                               # noqa: E402

fails = 0
ME = 1


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(atk=3, atk_buff=2, opc=4, opc_buff=1, armor=2, armor_buff=1):
    return S.U(1, ME, "frontline", atk, 5, 1, "infantry",
               atk_buff=atk_buff, opc=opc, opc_buff=opc_buff, armor=armor, armor_buff=armor_buff)


def main():
    # ① ct 0（tempBuffGive）写 **buff 字段**、不夹；总量 = clamp(基础 + buff)
    u = mk(atk=3, atk_buff=2)                       # 基础 1、buff 2、总量 3
    ST.change_attack(u, 5, ST.TEMP_BUFF_GIVE)
    chk("`ChangeAttack(0/+,5)`：buff 2→7、总量 3→**8**（基础 1 不动）",
        u.atk_buff == 7 and u.atk == 8, "atk=%s buff=%s" % (u.atk, u.atk_buff))

    # ② ct 4（tempBuffRemove）同样写 buff；负值把总量压到下限 0（夹在总量上）
    u = mk(atk=3, atk_buff=2)
    ST.change_attack(u, -5, ST.TEMP_BUFF_REMOVE)
    chk("`ChangeAttack(4/−5)`：buff 2→−3、总量 clamp(1−3)=**0**",
        u.atk_buff == -3 and u.atk == 0, "atk=%s buff=%s" % (u.atk, u.atk_buff))

    # ③ ct 1（permBuff）写**基础**、amount==0 是 no-op
    u = mk(atk=3, atk_buff=2)                       # 基础 1
    ST.change_attack(u, 4, ST.PERM_BUFF)
    chk("`ChangeAttack(1,+4)`：基础 1→5、总量 3→**7**（buff 仍 2）",
        u.atk == 7 and u.atk_buff == 2, "atk=%s buff=%s" % (u.atk, u.atk_buff))
    u = mk(atk=3, atk_buff=2)
    chk("`ChangeAttack(1,0)` ⇒ **no-op**（返回 False、数值不动）",
        ST.change_attack(u, 0, ST.PERM_BUFF) is False and u.atk == 3 and u.atk_buff == 2,
        "atk=%s buff=%s" % (u.atk, u.atk_buff))

    # ④ ct 2（SetValue）写**基础** = clamp(amount)，总量 = clamp(基础 + buff)
    u = mk(atk=3, atk_buff=2)
    ST.change_attack(u, 2, ST.SET_VALUE)
    chk("`ChangeAttack(2,设为 2)`：基础=2、总量 = clamp(2+2)=**4**（旧实现只发总量增量 ⇒ 会丢 buff）",
        u.atk == 4 and u.atk_buff == 2, "atk=%s buff=%s" % (u.atk, u.atk_buff))

    # ⑤ ct 6-9 不改数值（连事件都没有的那一族）
    u = mk(atk=3, atk_buff=2)
    chk("`ChangeAttack(6..9)` ⇒ **不改数值**（`touches_stats` 为假）",
        ST.change_attack(u, 9, ST.CUSTOM_ADD) is False and u.atk == 3 and u.atk_buff == 2,
        "atk=%s buff=%s" % (u.atk, u.atk_buff))

    # ⑥ 行动费：同样的两字段（这套字段是 P4 第三刀才补的）
    u = mk(opc=4, opc_buff=1)                       # 基础 3
    ST.change_operation_cost(u, 2, ST.PERM_BUFF)
    chk("`ChangeOperationCost(1,+2)`：基础 3→5、总量 4→**6**",
        u.opc == 6 and u.opc_buff == 1, "opc=%s buff=%s" % (u.opc, u.opc_buff))
    u = mk(opc=4, opc_buff=1)
    ST.change_operation_cost(u, 9, ST.SET_VALUE)
    chk("`ChangeOperationCost(2,设为 9)`：基础=9、总量 = clamp(9+1)=10",
        u.opc == 10 and u.opc_buff == 1, "opc=%s buff=%s" % (u.opc, u.opc_buff))

    # ⑦ 重甲：夹取是 **[0,3]**（不是 [0,99]）
    u = mk(armor=2, armor_buff=1)                   # 基础 1
    ST.change_heavy_armor(u, 1, ST.PERM_BUFF)
    chk("`ChangeHeavyArmor(1,+1)`：基础 1→2、总量 = clamp_armor(2+1)=**3**",
        u.armor == 3 and u.armor_buff == 1, "armor=%s buff=%s" % (u.armor, u.armor_buff))
    u = mk(armor=2, armor_buff=1)
    ST.change_heavy_armor(u, 9, ST.SET_VALUE)
    chk("`ChangeHeavyArmor(2,设为 9)`：基础先夹到 **3**、buff 不动（1）、总量 clamp_armor(3+1)=3",
        u.armor == 3 and u.armor_buff == 1, "armor=%s buff=%s" % (u.armor, u.armor_buff))

    # ⑧ 防御族（第四刀）：与 `ChangeAttack` **不同构** —— set 家族没有早退、带摧毁
    u = S.U(1, ME, "frontline", 2, 5, 1, "infantry", mdef=7)
    r = ST.change_defense(u, -3, ST.PERM_BUFF)
    chk("`ChangeDefense(1,−3)`：dfn 5→2（原版**不夹**）、不摧毁、`mdef` 同步 −3（`:11405`）",
        u.dfn == 2 and r["changed"] and not r["destroy"] and u.mdef == 4,
        "dfn=%s mdef=%s r=%s" % (u.dfn, u.mdef, r))
    u = S.U(1, ME, "frontline", 2, 5, 1, "infantry")
    r = ST.change_defense(u, -5, ST.PERM_BUFF)
    chk("`ChangeDefense(1,−5)`：写完总量 0 ⇒ **摧毁**（`:11396-11419`），dfn 写成 0",
        u.dfn == 0 and r["destroy"], "dfn=%s r=%s" % (u.dfn, r))
    u = S.U(1, ME, "frontline", 2, -5, 1, "infantry")     # 合成态（实际到不了，用于钉原版夹取）
    r = ST.change_defense(u, 2, ST.PERM_BUFF)
    chk("`ChangeDefense(1,+)` 的下限是 **1**（`:11439`，不是 0）⇒ 合成输入 −5+2 夹到 1",
        u.dfn == 1 and not r["destroy"], "dfn=%s r=%s" % (u.dfn, r))
    u = S.U(1, ME, "frontline", 2, 4, 1, "infantry")
    r = ST.change_defense(u, 0, ST.SET_VALUE)
    chk("`ChangeDefense(2,设为 0)`：dfn 4→0 ⇒ **摧毁** + `after_set`（`:11350-11357` 的 0x6）",
        u.dfn == 0 and r["destroy"] and r["after_set"], "dfn=%s r=%s" % (u.dfn, r))
    u = S.U(1, ME, "frontline", 2, 4, 1, "infantry")
    r = ST.change_defense(u, 3, ST.SET_VALUE)
    chk("`ChangeDefense(2,设为 3)`：dfn 4→3、不摧毁、仍报 `after_set`（ct==2 才发 0x6）",
        u.dfn == 3 and not r["destroy"] and r["after_set"], "dfn=%s r=%s" % (u.dfn, r))
    u = S.U(1, ME, "frontline", 2, 4, 1, "infantry")
    r = ST.change_defense(u, 9, ST.VETERAN_SET)
    chk("`ChangeDefense(5,设为 9)`：dfn 4→9（set 家族）、**不报 after_set**（只有 ct==2 发）",
        u.dfn == 9 and not r["destroy"] and not r["after_set"], "dfn=%s r=%s" % (u.dfn, r))
    u = S.U(1, ME, "frontline", 2, 4, 1, "infantry")
    r = ST.change_defense(u, -2, ST.TEMP_BUFF_GIVE)
    chk("`ChangeDefense(0)` ⇒ **拒绝**（`Label_3734`：防御没有 buff 通道，R15 结论）：dfn 不动",
        u.dfn == 4 and r["rejected"] and not r["changed"], "dfn=%s r=%s" % (u.dfn, r))

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
