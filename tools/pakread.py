# -*- coding: utf-8 -*-
"""最小的 UE5 `.pak`（v10/v11，路径哈希 + 完整目录索引）只读读取器：按路径取一个小文件（如 `kards/Config/DefaultGame.ini`）。

为什么自己写：`u4pak.py` 读不了 v11（`illegal file magic`）；FModel 是 GUI。这里只做“列索引 + 取单个文件”，
**只读**，不修改 pak。索引与数据可 AES-256-ECB 加密（`bEncryptedIndex=1`），key 由调用方给（FModel 的
`%APPDATA%\\FModel\\AppSettings.json` 里存着 `AesKeys/mainKey`，见 `load_fmodel_key`）。
压缩：支持 zlib/gzip；Oodle 需要 `oo2core` DLL（没有就抛 `NotImplementedError`，不假装读成功）。

格式要点（UE 5.x `FPakFile`/`FPakEntry::DecodeFrom`，逐项核对过源码语义）：
  footer（从后往前）：压缩方法名 5×32 字节、[v9+ 1 字节 frozen]、SHA1(20)、IndexSize(8)、IndexOffset(8)、Version(4)、Magic(4)、
                      bEncryptedIndex(1)、EncryptionKeyGuid(16)；
  主索引（解密后）：MountPoint FString、NumEntries i32、PathHashSeed u64、bHasPathHashIndex i32[+off i64,size i64,hash 20]、
                    bHasFullDirectoryIndex i32[+off,size,hash]、EncodedPakEntries(i32 长度 + 字节)、非编码条目(跳过)；
  完整目录索引：TMap<目录 FString, TMap<文件名 FString, i32 编码偏移>>；
  编码条目：u32 位域 → 压缩方法/加密/块数/块大小/偏移/大小（见 `_decode_entry`）。
"""
from __future__ import annotations

import json
import os
import re
import struct
import zlib

MAGIC = 0x5A6F12E1
# KARDS 的 pak 主 key：1.57 / 1.58 / 1.60（Steam 与 launcher）四棵树实测同一个（FModel 里也是它；AESDumpster 扫 exe 得到的
# 候选之一）。硬编码作**默认值**——换版本后若变了，`install_version` 会继续试 FModel 的值与 AESDumpster 候选。
DEFAULT_MAIN_KEY = bytes.fromhex("C257932734957B6D467FA16FCF728D5A16A5BDD07F6E20EE6E3155E898305CC0")


def load_fmodel_key(game_dir: str | None = None, settings: str | None = None) -> bytes:
    """从 FModel 的设置里取 mainKey（用户本机配置；0x 开头 64 位十六进制）。"""
    settings = settings or os.path.join(os.environ.get("APPDATA", ""), "FModel", "AppSettings.json")
    with open(settings, "r", encoding="utf-8-sig") as f:
        d = json.load(f)
    per = d.get("PerDirectory") or {}
    cands = [v for k, v in per.items() if game_dir is None or os.path.normcase(k) == os.path.normcase(game_dir)]
    for v in cands:
        k = (v.get("AesKeys") or {}).get("mainKey")
        if k:
            return bytes.fromhex(k[2:] if k.lower().startswith("0x") else k)
    raise KeyError("FModel 设置里没有 mainKey")


def _fstring(buf: bytes, pos: int):
    n = struct.unpack_from("<i", buf, pos)[0]
    pos += 4
    if n == 0:
        return "", pos
    if n < 0:
        n = -n
        s = buf[pos:pos + n * 2].decode("utf-16-le").rstrip("\x00")
        return s, pos + n * 2
    return buf[pos:pos + n].decode("utf-8", "replace").rstrip("\x00"), pos + n


class Pak:
    def __init__(self, path: str, key: bytes | None = None):
        self.path, self.key = path, key
        self.f = open(path, "rb")
        self._footer()
        self._index()

    def close(self):
        self.f.close()

    # ---- AES-256-ECB（UE 的 pak 用 ECB，16 字节块）----
    def _dec(self, data: bytes) -> bytes:
        if self.key is None:
            raise ValueError("pak 加密了，但没给 AES key")
        from Crypto.Cipher import AES
        n = len(data) - len(data) % 16
        return AES.new(self.key, AES.MODE_ECB).decrypt(data[:n]) + data[n:]

    def _footer(self):
        f = self.f
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 1024))
        tail = f.read()
        i = tail.rfind(struct.pack("<I", MAGIC))
        if i < 0:
            raise ValueError("找不到 pak 魔数")
        self.encrypted = bool(tail[i - 1])
        self.version = struct.unpack_from("<I", tail, i + 4)[0]
        self.index_off, self.index_size = struct.unpack_from("<QQ", tail, i + 8)
        names = tail[-160:]
        self.methods = [None] + [names[j:j + 32].split(b"\x00")[0].decode("ascii", "replace") or None
                                 for j in range(0, 160, 32)]       # 方法号 0 = 不压缩

    def _read(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        return self.f.read(n)

    def _index(self):
        raw = self._read(self.index_off, self.index_size)
        idx = self._dec(raw) if self.encrypted else raw
        pos = 0
        self.mount, pos = _fstring(idx, pos)
        if not (self.mount.isprintable() and len(self.mount) < 200):
            raise ValueError("索引解密后挂载点不可读（key 不对？）：%r" % self.mount[:40])
        self.num_entries, self.hash_seed = struct.unpack_from("<iQ", idx, pos)
        pos += 12
        has_ph = struct.unpack_from("<i", idx, pos)[0]
        pos += 4
        if has_ph:
            pos += 8 + 8 + 20
        has_fd = struct.unpack_from("<i", idx, pos)[0]
        pos += 4
        fd_off = fd_size = 0
        if has_fd:
            fd_off, fd_size = struct.unpack_from("<qq", idx, pos)
            pos += 8 + 8 + 20
        n = struct.unpack_from("<i", idx, pos)[0]
        pos += 4
        self.encoded = idx[pos:pos + n]
        self.files: dict = {}
        if not has_fd:
            raise NotImplementedError("pak 没有完整目录索引，仅有路径哈希索引（暂不支持）")
        d = self._read(fd_off, fd_size)
        d = self._dec(d) if self.encrypted else d
        p = 0
        ndirs = struct.unpack_from("<i", d, p)[0]
        p += 4
        for _ in range(ndirs):
            dname, p = _fstring(d, p)
            nf = struct.unpack_from("<i", d, p)[0]
            p += 4
            for _ in range(nf):
                fname, p = _fstring(d, p)
                eo = struct.unpack_from("<i", d, p)[0]
                p += 4
                self.files[(self.mount + dname + fname).lstrip("/")] = eo

    def _decode_entry(self, eo: int) -> dict:
        b, p = self.encoded, eo
        v = struct.unpack_from("<I", b, p)[0]
        p += 4
        method = (v >> 23) & 0x3F
        enc = bool(v & (1 << 22))
        nblocks = (v >> 6) & 0xFFFF
        bsize = v & 0x3F
        if bsize == 0x3F:
            bsize = struct.unpack_from("<I", b, p)[0]
            p += 4
        else:
            bsize <<= 11
        if v & (1 << 31):
            off = struct.unpack_from("<I", b, p)[0]
            p += 4
        else:
            off = struct.unpack_from("<Q", b, p)[0]
            p += 8
        if v & (1 << 30):
            usize = struct.unpack_from("<I", b, p)[0]
            p += 4
        else:
            usize = struct.unpack_from("<Q", b, p)[0]
            p += 8
        if method:
            if v & (1 << 29):
                csize = struct.unpack_from("<I", b, p)[0]
                p += 4
            else:
                csize = struct.unpack_from("<Q", b, p)[0]
                p += 8
        else:
            csize = usize
        header = 53 + ((4 + 16 * nblocks) if method else 0)          # 文件内 FPakEntry 头的序列化大小
        blocks = []
        if nblocks:
            if nblocks == 1 and not enc:
                blocks = [(header, header + csize)]
            else:
                pos2 = header
                for _ in range(nblocks):
                    sz = struct.unpack_from("<I", b, p)[0]
                    p += 4
                    blocks.append((pos2, pos2 + sz))
                    pos2 += sz if not enc else (sz + 15) // 16 * 16
        return {"method": method, "enc": enc, "off": off, "usize": usize, "csize": csize, "bsize": bsize,
                "header": header, "blocks": blocks}

    def read(self, path: str) -> bytes:
        eo = self.files[path.lstrip("/")]
        if eo < 0:
            raise NotImplementedError("非编码条目暂不支持")
        e = self._decode_entry(eo)
        if not e["method"]:
            n = (e["usize"] + 15) // 16 * 16 if e["enc"] else e["usize"]
            raw = self._read(e["off"] + e["header"], n)
            return (self._dec(raw) if e["enc"] else raw)[:e["usize"]]
        name = (self.methods[e["method"]] or "").lower()
        out = b""
        for s, t in e["blocks"]:
            raw = self._read(e["off"] + s, t - s)
            if e["enc"]:
                raw = self._dec(raw)
            if name in ("zlib",):
                out += zlib.decompress(raw)
            elif name in ("gzip",):
                out += zlib.decompress(raw, 31)
            else:
                raise NotImplementedError("压缩方法 %r 需要对应解压库（Oodle 要 oo2core DLL）" % name)
        return out[:e["usize"]]

    def find(self, pattern: str) -> list:
        rx = re.compile(pattern, re.I)
        return sorted(p for p in self.files if rx.search(p))


def project_version(pak_path: str, key: bytes | None = None) -> str | None:
    """从 pak 里的 `kards/Config/DefaultGame.ini` 读 `ProjectVersion=`（去掉 `Kards ` 前缀）。"""
    pk = Pak(pak_path, key)
    try:
        hits = pk.find(r"Config/DefaultGame\.ini$")
        if not hits:
            return None
        text = pk.read(hits[0]).decode("utf-8-sig", "replace")
        m = re.search(r"^ProjectVersion=(?:Kards\s+)?(\S+)", text, re.M)
        return m.group(1) if m else None
    finally:
        pk.close()


# ---------------------------------------------------------------- 不依赖 FModel：从 exe 里找 key，再读版本
AESDUMPSTER = os.path.join("D:" + os.sep, "Downloads", "AESDumpster-1.3", "AESDumpster", "x64", "Release",
                           "AESDumpster.exe")
_KEY_RE = re.compile(r"Key:\s*0x([0-9A-Fa-f]{64})")


def keys_from_exe(exe: str, dumpster: str = AESDUMPSTER, runner=None, timeout: int = 120) -> list:
    """用 AESDumpster 扫 exe，返回候选 AES key（bytes 列表，原顺序）。只读 exe，不改文件。"""
    import subprocess
    if runner is None:
        def runner(path):
            return subprocess.run([dumpster, path], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL).stdout
    return [bytes.fromhex(m) for m in _KEY_RE.findall(runner(exe))]


def open_with_any_key(pak_path: str, candidates) -> "Pak":
    """依次试候选 key（解密索引后挂载点可读才算对）；全不对抛 ValueError。"""
    last = None
    for k in candidates:
        try:
            return Pak(pak_path, k)
        except (ValueError, struct.error, UnicodeDecodeError) as exc:
            last = exc
    raise ValueError("候选 key 都解不开 pak 索引（%s）" % last)


def install_version(tree: str, fmodel_key: bytes | None = None, dumpster_runner=None) -> dict:
    """`game-installs/<版本>` 一棵树的 `ProjectVersion`（静态读 pak，不需要游戏在跑）。
    key 来源按顺序试：FModel 设置里的 mainKey → AESDumpster 从 `kards-Win64-Shipping.exe` 扫出来的候选。
    → `{"version", "key_source", "pak"}`；读不出 ⇒ version=None + 原因。"""
    pak = os.path.join(tree, "kards", "Content", "Paks", "kards-Windows.pak")
    exe = os.path.join(tree, "kards", "Binaries", "Win64", "kards-Win64-Shipping.exe")
    out = {"version": None, "key_source": None, "pak": pak, "why": ""}
    if not os.path.exists(pak):
        out["why"] = "没有 pak"
        return out
    srcs = [("default", [DEFAULT_MAIN_KEY])]
    if fmodel_key is not None and fmodel_key != DEFAULT_MAIN_KEY:
        srcs.append(("fmodel", [fmodel_key]))
    pk = None
    for src, keys in srcs:
        try:
            pk = open_with_any_key(pak, keys)
            out["key_source"] = src
            break
        except ValueError:
            pk = None
    if pk is None and os.path.exists(exe):
        try:
            pk = open_with_any_key(pak, keys_from_exe(exe, runner=dumpster_runner))
            out["key_source"] = "aesdumpster"
        except Exception as exc:                              # noqa: BLE001
            out["why"] = "AESDumpster 路线失败：%s: %s" % (type(exc).__name__, exc)
    if pk is None:
        out["why"] = out["why"] or "没有可用的 AES key"
        return out
    try:
        hits = pk.find(r"Config/DefaultGame\.ini$")
        text = pk.read(hits[0]).decode("utf-8-sig", "replace") if hits else ""
        m = re.search(r"^ProjectVersion=(?:Kards\s+)?(\S+)", text, re.M)
        out["version"] = m.group(1) if m else None
        if not m:
            out["why"] = "DefaultGame.ini 里没有 ProjectVersion"
    finally:
        pk.close()
    return out
