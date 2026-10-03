#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加密记录哨兵（NATIVE-SPEC-GAPS §12-U1 / §8-9 "考虑这种情况"）：

`mult(X)==0`（记录未初始化）⇒ 游戏 `getAndDecryptX` 返回哨兵：
  attack/attackBuff = −100、defense/kredit/kreditBuff = **+100**
（于是 `getTotalDefense = clamp(100,0,99) = 99`，而不是 0）。
正常记录走 `((enc^key) − Y) / X`（向零取整）。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from kardsmem import cardnatives as CN

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


# ---- 公式层 ----
chk("mult==0：defense ⇒ +100（getTotalDefense 走 clamp 得 99）",
    CN._game_decrypt("defense", 0x1234, 0, 0, 0) == 100)
chk("mult==0：kreditBuff ⇒ +100", CN._game_decrypt("kreditBuff", 0, 0, 0, 0) == 100)
chk("mult==0：attack ⇒ −100", CN._game_decrypt("attack", 0x1234, 0, 0, 0) == -100)
chk("正常：mult=1939/key=1939/add=1939/enc=0 ⇒ 0",
    CN._game_decrypt("defense", 1939, 1939, 1939, 0) == 0)
chk("正常：向零取整（(10^0−3)/3 = 7/3 ⇒ 2，不是 2.33、也不是 None）",
    CN._game_decrypt("defense", 0, 3, 3, 10) == 2)
chk("读到写入中途也按 C 除法截断（10/7 ⇒ 1，不是 None）",
    CN._game_decrypt("defense", 0, 7, 0, 10) == 1)
chk("负商向零取整（−7/3 ⇒ −2）", CN._game_decrypt("defense", 0, 3, 7, 0) == -2)

# ---- _stat 走 records_raw ----
c0 = {"card_type": "infantry", "key": 0x1234,
      "records_raw": {"defense": (0, 0, 0, 0, 0)}}
chk("_stat(records_raw, mult==0)：infantry ⇒ 100（上层 clamp 99）",
    CN._stat(c0, "defense") == 100)
chk("getTotalDefense 的 clamp：100 ⇒ 99",
    max(0, min(99, CN._stat(c0, "defense"))) == 99)
c1 = {"card_type": "order", "key": 1939,
      "records_raw": {"defense": (1939, 1939, 0, 0, 0)}, "defense": 0}
chk("指令牌正常记录（实测形态 1939/1939/enc=0）⇒ 0", CN._stat(c1, "defense") == 0)
c2 = {"card_type": "order", "key": 0x1234, "defense": None}
chk("没有 records_raw 的旧 view：非单位 ⇒ 0（保持原行为）", CN._stat(c2, "defense") == 0)

print("失败 %d 项" % fails)
raise SystemExit(1 if fails else 0)
