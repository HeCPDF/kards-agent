# -*- coding: utf-8 -*-
"""最小 minidump 解析：只取 Memory64ListStream，建 VA -> (文件偏移, 长度) 表。"""
import struct

MEMORY64_LIST = 9
MEMORY_INFO_LIST = 16


def streams(path):
    with open(path, "rb") as f:
        hdr = f.read(32)
        sig, ver, nstreams, dir_rva = struct.unpack_from("<4sIII", hdr, 0)
        assert sig == b"MDMP", sig
        f.seek(dir_rva)
        raw = f.read(12 * nstreams)
        out = {}
        for i in range(nstreams):
            t, sz, rva = struct.unpack_from("<III", raw, 12 * i)
            out[t] = (sz, rva)
        return out


def ranges(path):
    """→ [(va, size, file_offset)]，按 va 升序。"""
    st = streams(path)
    if MEMORY64_LIST not in st:
        raise SystemExit("没有 Memory64ListStream：%s" % path)
    _sz, rva = st[MEMORY64_LIST]
    with open(path, "rb") as f:
        f.seek(rva)
        n, base_rva = struct.unpack("<QQ", f.read(16))
        raw = f.read(16 * n)
    out, off = [], base_rva
    for i in range(n):
        va, size = struct.unpack_from("<QQ", raw, 16 * i)
        out.append((va, size, off))
        off += size
    out.sort()
    return out


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = ranges(p)
        tot = sum(x[1] for x in r)
        print("%s\n   区段 %d 个，合计 %.2f GB，VA %#x .. %#x"
              % (p, len(r), tot / 2**30, r[0][0], r[-1][0] + r[-1][1]))
