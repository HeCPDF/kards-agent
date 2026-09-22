#!/usr/bin/env python3
"""Dump the raw bytes around AkardsGameState's encrypted side values so the
real 20-byte-struct layout can be read off the live object."""
import struct
import sys

sys.path.insert(0, r"D:\Kards\reverse-data\tools")
import mem_probe as mp

pid = mp.find_pid(mp.TARGET_PROCESS)
if not pid:
    print("game not running")
    raise SystemExit(2)
mods = mp.list_modules(pid)
base = [b for n, b, s, p in mods if n.lower() == mp.TARGET_MODULE.lower()][0]
m = mp.Mem(pid)
loc = mp.locate(base, m)
print("locate ok=%s world=0x%X gs=0x%X class_size=%s in_battle=%s" % (
    loc["ok"], loc.get("world", 0), loc.get("gamestate", 0), loc.get("class_size"), loc.get("in_battle")))
if not loc.get("ok"):
    print(loc)
    raise SystemExit(1)
gs = loc["gamestate"]


def dump(lo, hi, label):
    print("=" * 90)
    print("%s  (gs+0x%X .. gs+0x%X)" % (label, lo, hi))
    buf = m.read(gs + lo, hi - lo)
    for r in range(0, len(buf), 16):
        off = lo + r
        chunk = buf[r:r + 16]
        dwords = struct.unpack("<4i", chunk)
        u = struct.unpack("<4I", chunk)
        print("  +0x%03X  %s  | %12d %12d %12d %12d | %10u %10u %10u %10u" % (
            off, " ".join("%02X" % c for c in chunk), *dwords, *u))


dump(0x300, 0x3C0, "candidate encrypted-side region")

print("=" * 90)
print("20-byte-stride scan: for each base offset, show den/base/unk/enc/frame of 4 structs")
print("  (a real layout should give small, consistent 'den' and 'base' in all four)")
for base_off in range(0x300, 0x390, 4):
    rows = []
    ok = True
    for k in range(4):
        o = base_off + k * 0x14
        if o + 0x14 > 0x400:
            ok = False
            break
        vals = struct.unpack("<5i", m.read(gs + o, 20))
        rows.append(vals)
    if not ok:
        continue
    if not all(1 <= v[0] <= 64 for v in rows):
        continue
    print("  base=0x%03X" % base_off)
    for k, v in enumerate(rows):
        print("      [%d] +0x%03X  den=%-6d base=%-10d unk=%-10d enc=%-12d frame=%-12d" % (
            k, base_off + k * 0x14, v[0], v[1], v[2], v[3], v[4]))

print("=" * 90)
print("non-zero dwords in gs+0x300 .. gs+0x3C0")
buf = m.read(gs + 0x300, 0xC0)
for i in range(0, len(buf), 4):
    v = struct.unpack_from("<I", buf, i)[0]
    if v:
        print("   +0x%03X = %-12u (0x%08X)  as f32=%s" % (0x300 + i, v, v, struct.unpack_from("<f", buf, i)[0]))
m.close()
