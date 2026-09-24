#!/usr/bin/env python3
"""Dump a live UBaseCardObject: plain fields + brute-force the encrypted
20-byte records, so the field<->record mapping can be read off the game itself.

value = ((key ^ enc) - Y) / X        key at this+0x55C
"""
import struct
import sys

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
import mem_probe as mp

OFF_KEY = 0x55C
PLAIN = {
    "Name_0": 0x50, "Type": 0x68, "attack": 0x6C, "attackBuff": 0x70, "defense": 0x74,
    "range": 0x78, "faction": 0x7C, "kredits": 0x80, "kreditsBuff": 0x84,
    "KreditsTax_AsEnemyTarget": 0x88, "operationCost": 0xB0, "operationCostBuff": 0xB4,
    "heavyArmor": 0x1DC, "heavyArmorBuff": 0x1E0, "hasGuard": 0x1E8, "CardID": 0x264,
    "movementLeft": 0x268, "attackLeft": 0x26C, "enterPlayOnTurn": 0x270,
    "Location": 0x275, "side": 0x276, "locationNumber": 0x278, "pinnedTurns": 0x27C,
    "maxAttack": 0x2A4, "maxDefense": 0x2A8, "CurrentTarget": 0x2B0,
    "isSuppressed": 0x33A, "isRevealed": 0x33B, "cardHighlightBits": 0x33C,
    "targetOverride": 0x548, "threat_level": 0x544,
}
KIND = {
    "Name_0": "i32", "Type": "u8", "attack": "i32", "attackBuff": "i32", "defense": "i32",
    "range": "i32", "faction": "u8", "kredits": "i32", "kreditsBuff": "i32",
    "KreditsTax_AsEnemyTarget": "i32", "operationCost": "i32", "operationCostBuff": "i32",
    "heavyArmor": "i32", "heavyArmorBuff": "i32", "hasGuard": "u8", "CardID": "i32",
    "movementLeft": "i32", "attackLeft": "i32", "enterPlayOnTurn": "i32",
    "Location": "u8", "side": "u8", "locationNumber": "i32", "pinnedTurns": "i32",
    "maxAttack": "i32", "maxDefense": "i32", "CurrentTarget": "ptr",
    "isSuppressed": "u8", "isRevealed": "u8", "cardHighlightBits": "i32",
    "targetOverride": "ptr", "threat_level": "f32",
}

pid = mp.find_pid(mp.TARGET_PROCESS)
if not pid:
    print("game not running")
    raise SystemExit(2)
mods = mp.list_modules(pid)
base = [b for n, b, s, p in mods if n.lower() == mp.TARGET_MODULE.lower()][0]
m = mp.Mem(pid)
loc = mp.locate(base, m)
print("world=0x%X gs=0x%X class_size=%s in_battle=%s" % (
    loc.get("world", 0), loc.get("gamestate", 0), loc.get("class_size"), loc.get("in_battle")))
gs = loc["gamestate"]

cards = []
for nm, off in (("Left_HQ", 0x5E8), ("Right_HQ", 0x5F8)):
    p = m.ptr(gs + off)
    if p:
        cards.append((nm, p))

# also pull a few real cards out of AllCardsInBattle (TMap -> TSparseArray)
map_addr = gs + 0x538
data_ptr = m.ptr(map_addr)          # TSparseArray.Data.Data
num = m.i32(map_addr + 0x08)
mx = m.i32(map_addr + 0x0C)
print("AllCardsInBattle: Data=0x%X ArrayNum=%s ArrayMax=%s" % (data_ptr or 0, num, mx))
blob = m.safe(data_ptr, 16 * 24) if data_ptr else None
print("  first 8 raw 16-byte slots:")
if blob:
    for i in range(8):
        q0, q1 = struct.unpack_from("<QQ", blob, i * 16)
        print("    [%d] 0x%016X 0x%016X" % (i, q0, q1))

for name, c in cards:
    print("=" * 96)
    key = m.i32(c + OFF_KEY)
    print("%s  card=0x%X   key(this+0x55C)=%s" % (name, c, key))
    vals = {}
    for k, off in PLAIN.items():
        vals[k] = mp.read_field(m, c + off, KIND[k])
    print("  plain fields:")
    for k in PLAIN:
        print("    +0x%03X  %-28s %s" % (PLAIN[k], k, vals[k]))

    lo, hi = 0x550, 0x6A0
    buf = m.read(c + lo, hi - lo)
    print("  raw 0x550..0x6A0:")
    for r in range(0, len(buf), 16):
        d = struct.unpack_from("<4i", buf, r)
        print("    +0x%03X  %s | %11d %11d %11d %11d" % (
            lo + r, " ".join("%02X" % b for b in buf[r:r + 16]), *d))

    print("  candidate 20-byte records (exact division, |value| <= 200):")
    hits = 0
    for k in range(0, len(buf) - 0x14 + 1, 4):
        X, Y, Z, enc, fr = struct.unpack_from("<5i", buf, k)
        if X == 0:
            continue
        num = (key ^ enc) - Y
        if num % X:
            continue
        v = num // X
        if -200 <= v <= 200:
            hits += 1
            print("    +0x%03X  X=%-9d Y=%-9d Z=%-9d enc=%-12d frame=%-12d -> %d" % (
                lo + k, X, Y, Z, enc, fr, v))
    if not hits:
        print("    (none)")

m.close()
