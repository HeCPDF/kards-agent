#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组 A 读器合并断言（NATIVE-SPEC-GAPS §2 #1/#3/#4）：

getTotalDefense/HeavyArmor/IsDamaged、getAndDecrypt*（哨兵 + 向零取整）、
TempBuff（大小写不敏感）、isBuffedByCard（空 BuffMap 也算）。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from kardsmem import cardnatives as CN

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


def C(**kw):
    return kw


def call(name, c, *a):
    return CN.CardNatives(None).call(name, c, *a)


# ---- getTotalDefense / IsDamaged（§2 #1）----
chk("getTotalDefense：defense=3 ⇒ 3", call("getTotalDefense", C(defense=3)) == 3)
chk("getTotalDefense：defense=120 ⇒ clamp 99",
    call("getTotalDefense", C(defense=120)) == 99)
chk("getTotalDefense：mult==0 ⇒ 哨兵 100 ⇒ clamp 99",
    call("getTotalDefense", C(records_raw={"defense": (0, 0, 0, 0, 0)}, key=1)) == 99)
chk("IsDamaged：3 < max 5 ⇒ True", call("IsDamaged", C(defense=3, max_defense=5)) is True)
chk("IsDamaged：5 < max 5 ⇒ False", call("IsDamaged", C(defense=5, max_defense=5)) is False)
chk("IsDamaged：mult==0（100）< max 5 ⇒ False",
    call("IsDamaged", C(records_raw={"defense": (0, 0, 0, 0, 0)}, key=1, max_defense=5))
    is False)

# ---- getTotalHeavyArmor（§2 #1）----
chk("getTotalHeavyArmor：2+2 ⇒ clamp 3",
    call("getTotalHeavyArmor", C(heavy_armor=2, heavy_armor_buff=2)) == 3)
chk("getTotalHeavyArmor：0+(−1) ⇒ clamp 0",
    call("getTotalHeavyArmor", C(heavy_armor=0, heavy_armor_buff=-1)) == 0)

# ---- getAndDecrypt*（§2 #4：哨兵符号 + 公式）----
chk("getAndDecryptAttackBuff：mult==0 ⇒ −100",
    call("getAndDecryptAttackBuff", C(records_raw={"attackBuff": (0, 0, 0, 0, 0)}, key=1))
    == -100)
chk("getAndDecryptKredit：mult==0 ⇒ +100",
    call("getAndDecryptKredit", C(records_raw={"kredit": (0, 0, 0, 0, 0)}, key=1)) == 100)
chk("getAndDecryptKreditBuff：mult==0 ⇒ +100",
    call("getAndDecryptKreditBuff", C(records_raw={"kreditBuff": (0, 0, 0, 0, 0)}, key=1))
    == 100)
chk("getAndDecryptDefense：key=0x1234,X=3,Y=7,enc=0x1234^(7+15)=… ⇒ 5",
    call("getAndDecryptDefense",
         C(records_raw={"defense": (3, 7, 0, 0x1234 ^ (7 + 3 * 5), 0)}, key=0x1234)) == 5)
chk("getAndDecryptDefense：X=−2,Y=1,回算 −4（向零取整）",
    call("getAndDecryptDefense",
         C(records_raw={"defense": (-2, 1, 0, 1 + (-2) * -4, 0)}, key=0)) == -4)

# ---- TempBuff / isBuffedByCard（§2 #3）----
c = C(buffs_from_cards=[{"giver_card_id": 7, "buffs": {}}])
chk("isBuffedByCard(7)：空 BuffMap 也算 True", call("isBuffedByCard", c, 7) is True)
chk("isBuffedByCard(8) ⇒ False", call("isBuffedByCard", c, 8) is False)
c2 = C(buffs_from_cards=[{"giver_card_id": 7, "buffs": {"Attack_TempBuffGive": 2}}])
chk("getAttackTempBuffAmount(7)=2（大小写不敏感）",
    call("getAttackTempBuffAmount", c2, 7) == 2)
chk("getKreditTempBuffAmount(7)=0（键不存在）",
    call("getKreditTempBuffAmount", c2, 7) == 0)
chk("getCardsBuffedByThisCard 透传",
    call("getCardsBuffedByThisCard", C(cards_buffed_by_this_card=[3, 5])) == [3, 5])

# ---- 组 B/C（§3 #26 / §4 #72 #52）----
chk("GetCustomName2Attributes：'a;b' ⇒ ['a','b']",
    call("GetCustomName2Attributes", C(custom_name2="a;b")) == ["a", "b"])
chk("GetCustomName2Attributes：'' / 'None' / 'none' ⇒ []",
    call("GetCustomName2Attributes", C(custom_name2="")) == []
    and call("GetCustomName2Attributes", C(custom_name2="None")) == []
    and call("GetCustomName2Attributes", C(custom_name2="none")) == [])
chk("GetCustomName2Attributes：';;a;' ⇒ ['a']",
    call("GetCustomName2Attributes", C(custom_name2=";;a;")) == ["a"])
chk("hasActiveCountdownEffect：键大小写不敏感（'CountDown_Timer' ⇒ True）",
    call("hasActiveCountdownEffect", C(custom_json_keys=["CountDown_Timer"])) is True)
chk("hasActiveCountdownEffect：无键 ⇒ False",
    call("hasActiveCountdownEffect", C(custom_json_keys=[])) is False)
chk("hasActivePincerEffect：'PINCER_RECEIVER' ⇒ True",
    call("hasActivePincerEffect", C(custom_json_keys=["PINCER_RECEIVER"])) is True)
chk("hasActivePincerEffect：'countdown_timer' ⇒ False",
    call("hasActivePincerEffect", C(custom_json_keys=["countdown_timer"])) is False)

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
