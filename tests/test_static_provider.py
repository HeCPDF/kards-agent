#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态卡表提供者（NATIVE-COVERAGE §15.6 / §8-5）实机断言。

需要游戏在跑（只读）；不在跑则 SKIP（退出码 0）。
覆盖：表大小、已知卡模板值、"换一张卡答案要变"、StaticCardExists 真/假。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys


from kardsmem import attach
from kardsmem.cardnatives import CardNatives
from kardsmem.gs import make_static_card_provider

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


try:
    s = attach()
except Exception as e:                                        # noqa: BLE001
    print("  [SKIP] 游戏没在跑/读不到（%s: %s）" % (type(e).__name__, e))
    print("        （本机跑 launcher 渠道时先设 KARDS_BUILD=launcher_default）")
    sys.exit(0)

prov = make_static_card_provider(s)
if not prov.table_size:
    print("  [SKIP] 静态卡表读出来是空的（游戏在掉线对话框 / 主菜单加载中？）——不是回归；进到牌组页或对局里再跑")
    sys.exit(0)
chk("静态卡表读全（≈2019 张）", prov.table_size >= 1500, "size=%d" % prov.table_size)

kyoto = prov("card_unit_kyoto_regiment")
sabae = prov("card_unit_5th_sasebo_snlf")
chk("Kyoto 模板：infantry / 3 费 / 1 攻 4 防",
    kyoto and kyoto.get("card_type") == "infantry" and kyoto.get("kredits_plain") == 3
    and kyoto.get("attack_plain") == 1 and kyoto.get("defense_plain") == 4)
chk("5th Sasebo 模板：infantry / 1 费 / 1 攻 1 防",
    sabae and sabae.get("card_type") == "infantry" and sabae.get("kredits_plain") == 1
    and sabae.get("attack_plain") == 1 and sabae.get("defense_plain") == 1)
chk("换一张卡答案要变（Kyoto 3 费 vs Sasebo 1 费）",
    kyoto.get("kredits_plain") != sabae.get("kredits_plain"))
chk("未知卡 ⇒ None", prov("card_unit_no_such_card") is None)

cn = CardNatives(None, ks=s)
cn.static_card = prov
cn.static_cards = prov.names
chk("GetStaticKredits(Kyoto)=3", cn.call("GetStaticKredits", None, "card_unit_kyoto_regiment") == 3)
chk("GetStaticAttack(Sasebo)=1", cn.call("GetStaticAttack", None, "card_unit_5th_sasebo_snlf") == 1)
chk("StaticCardExists(Kyoto)=True", cn.call("StaticCardExists", None, "card_unit_kyoto_regiment") is True)
chk("StaticCardExists(未知)=False", cn.call("StaticCardExists", None, "card_unit_no_such_card") is False)
chk("大小写不敏感：CARD_UNIT_KYOTO_REGIMENT 也能查",
    cn.call("StaticCardExists", None, "CARD_UNIT_KYOTO_REGIMENT") is True)

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
