#!/usr/bin/env python3
"""PE helpers: RVA -> file offset, byte dumps, vtable sanity checks, exe triage.

Usage:
  python pe_tools.py <exe> [<exe> ...] --rva 0x4A65030 [--rva ...] [--size 32]
  python pe_tools.py <exe> --vtable 0x07E859E0 --count 12
"""
import hashlib
import struct
import sys


def parse_pe(path):
    d = open(path, "rb").read()
    if d[:2] != b"MZ":
        raise ValueError("not PE: %s" % path)
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no PE header: %s" % path)
    coff = pe + 4
    numsec, = struct.unpack_from("<H", d, coff + 2)
    optsize, = struct.unpack_from("<H", d, coff + 16)
    opt = coff + 20
    magic, = struct.unpack_from("<H", d, opt)
    is64 = magic == 0x20B
    imagebase, = struct.unpack_from("<Q" if is64 else "<I", d, opt + 24)
    sizeofimage, = struct.unpack_from("<I", d, opt + 56)[0],
    secs = []
    so = opt + optsize
    for i in range(numsec):
        o = so + i * 40
        name = d[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, o + 8)
        chars, = struct.unpack_from("<I", d, o + 36)
        secs.append((name, vaddr, vsize, rawptr, rawsize, chars))
    return d, imagebase, sizeofimage, secs


def rva_to_off(secs, rva):
    for name, vaddr, vsize, rawptr, rawsize, chars in secs:
        if vaddr <= rva < vaddr + max(vsize, rawsize):
            return rawptr + (rva - vaddr), name, chars
    return None, None, None


def main():
    args = sys.argv[1:]
    exes, rvas, vtables = [], [], []
    size = 32
    count = 12
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--rva":
            rvas.append(int(args[i + 1], 16)); i += 2
        elif a == "--vtable":
            vtables.append(int(args[i + 1], 16)); i += 2
        elif a == "--size":
            size = int(args[i + 1], 0); i += 2
        elif a == "--count":
            count = int(args[i + 1], 0); i += 2
        else:
            exes.append(a); i += 1

    for path in exes:
        d, imagebase, sizeofimage, secs = parse_pe(path)
        print("=" * 100)
        print("exe   : %s" % path)
        print("size  : %d (0x%X)" % (len(d), len(d)))
        print("md5   : %s" % hashlib.md5(d).hexdigest())
        print("base  : 0x%X   sizeofimage: 0x%X" % (imagebase, sizeofimage))
        print("secs  : %s" % ", ".join("%s@0x%X/%d" % (s[0], s[1], s[2]) for s in secs))

        for rva in rvas:
            off, sec, chars = rva_to_off(secs, rva)
            if off is None:
                print("  RVA 0x%08X  -> NOT MAPPED" % rva)
                continue
            buf = d[off:off + size]
            print("  RVA 0x%08X  sec=%s %s  off=0x%X" % (rva, sec, "W" if chars & 0x80000000 else " ", off))
            print("      %s" % " ".join("%02X" % b for b in buf))

        for rva in vtables:
            off, sec, chars = rva_to_off(secs, rva)
            if off is None:
                print("  VFT 0x%08X  -> NOT MAPPED" % rva)
                continue
            print("  VFT 0x%08X  sec=%s" % (rva, sec))
            for k in range(count):
                v, = struct.unpack_from("<Q", d, off + k * 8)
                rel = v - imagebase if imagebase else v
                mark = "code" if 0x1000 <= rel < 0x07000000 else "?"
                print("      [%2d] 0x%016X  -> rva 0x%08X  %s" % (k, v, rel, mark))
    print("=" * 100)


if __name__ == "__main__":
    main()
