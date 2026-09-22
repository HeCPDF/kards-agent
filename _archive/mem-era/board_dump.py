#!/usr/bin/env python3
"""Enumerate live UBaseCardObject instances in a battle and decrypt their
attack / attackBuff / kredit / kreditBuff / defense records.

Encryption (recovered from the OLD build; RVA-verified against the live build):
    value = ((key ^ enc) - Y) / X
    key   = *(int32*)(card + 0x55C)
    record = {X@+0, Y@+4, Z@+8, enc@+0xC, frame@+0x10}
    attack 0x568 | attackBuff 0x57C | kredit 0x590 | kreditBuff 0x5A4 | defense 0x5B8
"""
import struct
import sys

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
import mem_probe as mp

CARD_KEY = 0x55C
CARD_WARN = 0x564
RECORDS = [
    ("attack", 0x568), ("attackBuff", 0x57C), ("kredit", 0x590),
    ("kreditBuff", 0x5A4), ("defense", 0x5B8),
]
REC_LO, REC_HI = 0x568, 0x5E0
SIZE_BASECARD = 1656

# ECardLocationEnum
LOC = {0: "NotAvail", 1: "Deck_L", 2: "Deck_R", 3: "Hand_L", 4: "Hand_R",
       5: "HQ_L", 6: "HQ_R", 7: "Frontline", 8: "Discard", 9: "Deck"}

pid = mp.find_pid(mp.TARGET_PROCESS)
if not pid:
    print("game not running")
    raise SystemExit(2)
mods = mp.list_modules(pid)
base = [b for n, b, s, p in mods if n.lower() == mp.TARGET_MODULE.lower()][0]
m = mp.Mem(pid)
loc = mp.locate(base, m)
if not loc.get("ok"):
    print("locate failed:", loc)
    raise SystemExit(1)
gs = loc["gamestate"]
print("world=0x%X  gs=0x%X  class=%s  in_battle=%s" % (
    loc["world"], gs, loc["class_size"], loc.get("in_battle")))

# ---- 1. gather candidate card pointers out of AllCardsInBattle ---------------
map_addr = gs + 0x538
data_ptr = m.ptr(map_addr)
array_num = m.i32(map_addr + 0x08)
array_max = m.i32(map_addr + 0x0C)
print("AllCardsInBattle TMap @0x%X  Data=0x%X ArrayNum=%s ArrayMax=%s" % (
    map_addr, data_ptr or 0, array_num, array_max))

cands = []
if data_ptr and array_max and 0 < array_max < 20000:
    blob = m.safe(data_ptr, array_max * 16)
    if blob:
        # element = 16 bytes: {int32 Key; int32 pad; UBaseCardObject* Value}
        # a free-list slot carries dword0 == 0xFFFFFFFF
        for off in range(0, len(blob) - 16 + 1, 16):
            if struct.unpack_from("<I", blob, off)[0] == 0xFFFFFFFF:
                continue
            p = struct.unpack_from("<Q", blob, off + 8)[0]
            if mp.PTR_MIN <= p <= mp.PTR_MAX and p % 8 == 0:
                cands.append(p)
print("candidate pointers in Data: %d" % len(cands))

# validate: the UClass must derive from UBaseCardObject (PropertiesSize == 1656).
# Plain instances are 1656; Blueprint subclasses (Ucard_event_*_C ...) are larger,
# so walk SuperStruct instead of comparing the size directly.
def discover_super_offset(m, uclass):
    for off in (0x38, 0x40, 0x48, 0x50, 0x30, 0x60):
        cur = uclass
        for _ in range(16):
            if mp.propsize_hits(m, cur, (SIZE_BASECARD,)):
                return off
            nxt = m.ptr(cur + off)
            if not nxt or nxt == cur:
                break
            cur = nxt
    return None


def is_base_card(m, uclass, super_off, cache):
    if uclass in cache:
        return cache[uclass]
    ok = False
    if super_off:
        cur = uclass
        for _ in range(16):
            if mp.propsize_hits(m, cur, (SIZE_BASECARD,)):
                ok = True
                break
            nxt = m.ptr(cur + super_off)
            if not nxt or nxt == cur:
                break
            cur = nxt
    cache[uclass] = ok
    return ok


cards = []
seen = set()
classes = {}
super_off = None
for p in cands:
    uc = m.ptr(p + mp.OFF_UOBJECT_CLASS)
    if uc and super_off is None:
        super_off = discover_super_offset(m, uc)
        print("UStruct::SuperStruct offset discovered at UClass+0x%X" % super_off if super_off else
              "could not discover SuperStruct offset")
for p in cands:
    if p in seen:
        continue
    seen.add(p)
    fl = m.u32(p + 0x08)
    if fl is not None and (fl & 0x10):      # RF_ClassDefaultObject -> template, skip
        continue
    uc = m.ptr(p + mp.OFF_UOBJECT_CLASS)
    if not uc:
        continue
    if is_base_card(m, uc, super_off, classes):
        cards.append(p)
print("confirmed UBaseCardObject instances: %d  (distinct classes: %d)" % (len(cards), len(classes)))

# ---- 2. decrypt every card's records ----------------------------------------
print("=" * 108)
print("%-14s %-6s %-5s %-5s %-4s | %-26s | %s" % (
    "card", "CardID", "side", "loc", "sup", "decrypted (atk/atkB/kred/kredB/def)", "plain (atk/def/maxA/maxD)"))
print("-" * 108)
rows = []
for c in cards:
    key = m.i32(c + CARD_KEY)
    warn = m.u8(c + CARD_WARN)
    buf = m.safe(c + REC_LO, REC_HI - REC_LO)
    if buf is None:
        continue
    dec = {}
    for name, roff in RECORDS:
        X, Y, Z, enc, frame = struct.unpack_from("<5i", buf, roff - REC_LO)
        v = None
        if X:
            num = (key ^ enc) - Y
            if num % X == 0:
                v = num // X
            else:
                v = "TORN(%.4f)" % (num / X)
        dec[name] = v
    card_id = m.i32(c + 0x264)
    side = m.u8(c + 0x276)
    locn = m.u8(c + 0x275)
    sup = m.u8(c + 0x33A)
    tgt = m.ptr(c + 0x2B0)
    plain_a = m.i32(c + 0x6C)
    plain_d = m.i32(c + 0x74)
    max_a = m.i32(c + 0x2A4)
    max_d = m.i32(c + 0x2A8)
    rows.append((c, card_id, side, locn, sup, dec, tgt, plain_a, plain_d, max_a, max_d, warn))

rows.sort(key=lambda r: (r[2] or 0, r[3] or 0, r[1] or 0))
for (c, cid, side, locn, sup, dec, tgt, pa, pd, ma, md, warn) in rows:
    print("0x%012X  %-6s %-5s %-9s %-4s | %-26s | %s/%s/%s/%s  tgt=0x%X" % (
        c, cid, side, LOC.get(locn, locn), sup,
        "/".join(str(dec[k]) for k, _ in RECORDS),
        pa, pd, ma, md, tgt or 0))

print("=" * 108)
print("legend: Location @0x275 = %s" % LOC)
print("        side 0x276 (ESideEnum 1=left 2=right) | discard entries legitimately hold <=0 defense")

# ---- 3. cross-check attack/defense totals -----------------------------------
print()
print("totals per card: attack = attack+attackBuff, defense = defense record")
for (c, cid, side, locn, sup, dec, tgt, pa, pd, ma, md, warn) in rows[:40]:
    a = dec["attack"]
    ab = dec["attackBuff"]
    tot = (a + ab) if isinstance(a, int) and isinstance(ab, int) else "?"
    print("  CardID %-5s side=%s loc=%-3s  atk=%s+(%s)=%s  def=%s  (plain atk=%s def=%s)" % (
        cid, side, locn, a, ab, tot, dec["defense"], pa, pd))
m.close()
