# -*- coding: utf-8 -*-
"""结构化读 dump（MiniDump）、cdb 分析、堆/委托扫描、sidecar —— 从 crashdump.py 原样搬出（P6 拆文件，零逻辑改动）。
"""
from __future__ import annotations

import _crashdump_base as _m__crashdump_base
globals().update({_k: _v for _k, _v in vars(_m__crashdump_base).items() if not _k.startswith("__")})


# --------------------------------------------------------------------------- 读 dump
MEM_STATES = {0x1000: "MEM_COMMIT", 0x2000: "MEM_RESERVE", 0x10000: "MEM_FREE"}
MEM_TYPES = {0x1000000: "MEM_IMAGE", 0x40000: "MEM_MAPPED", 0x20000: "MEM_PRIVATE"}
PAGE_PROTECT = {0x01: "NOACCESS", 0x02: "R", 0x04: "RW", 0x08: "WRITECOPY",
                0x10: "X", 0x20: "XR", 0x40: "XRW", 0x80: "XWRITECOPY",
                0x100: "GUARD", 0x200: "NOCACHE", 0x400: "WRITECOMBINE"}


def _md_streams(f) -> dict:
    f.seek(0)
    head = f.read(32)
    sig, ver, n, dir_rva = struct.unpack_from("<4sIII", head, 0)
    if sig != b"MDMP":
        raise DumpError("%s 不是 minidump（签名 %r）" % (getattr(f, "name", "?"), sig))
    f.seek(dir_rva)
    raw = f.read(12 * n)
    out = {}
    for i in range(n):
        t, sz, rva = struct.unpack_from("<III", raw, 12 * i)
        out[t] = (sz, rva)
    stamp = struct.unpack_from("<I", head, 24)[0]
    return {"streams": out, "timestamp": stamp, "version": ver}


class MiniDump:
    """只读地打开一份 minidump：内存区段 / 模块 / 线程 / 异常 / 内存信息表。

    为什么不复用 tools/dumpmem.py：崩溃分析工具要在整棵树被搬动/改坏时照常跑，
    而这里要多解四个流（异常、线程、内存信息、MiscInfo），复用省不下多少代码。
    """

    def __init__(self, path: str):
        self.path = path
        self.handles: list = []
        with open(path, "rb") as f:
            self.name = f.name
            meta = _md_streams(f)
            self.streams: dict = meta["streams"]
            self.timestamp = meta["timestamp"]
            # 两种内存流都要认：完整 dump 走 Memory64ListStream（数据在文件里连排），
            # UE 自带的小 dump 走 MemoryListStream（每段各有自己的 RVA）。
            self.ranges = []
            if MD_STREAM_MEMORY64 in self.streams:
                self.ranges = self._read_memory64(f)
            elif MD_STREAM_MEMORY_LIST in self.streams:
                self.ranges = self._read_memory_list(f)
            self.modules = self._read_modules(f)
            self.threads = self._read_threads(f)
            self.exception = self._read_exception(f)
            self.memory_info = (self._read_memory_info(f)
                                if MD_STREAM_MEMORY_INFO_LIST in self.streams else [])
            self.misc = self._read_misc(f)
            self.unloaded = self._read_unloaded(f)
            self._starts = [r[0] for r in self.ranges]

    # -- 流 ------------------------------------------------------------------
    def _read_memory64(self, f) -> list:
        _sz, rva = self.streams[MD_STREAM_MEMORY64]
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

    def _read_memory_list(self, f) -> list:
        """MINIDUMP_MEMORY_LIST：NumberOfMemoryRanges + N × MINIDUMP_MEMORY_DESCRIPTOR
        （每项 16 字节：StartOfMemoryRange(8) + DataSize(4) + Rva(4)）。"""
        _sz, rva = self.streams[MD_STREAM_MEMORY_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(16 * n)
        out = []
        for i in range(n):
            va, size, data_rva = struct.unpack_from("<QII", raw, 16 * i)
            if size:
                out.append((va, size, data_rva))
        out.sort()
        return out

    def _read_modules(self, f) -> list:
        if MD_STREAM_MODULE_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_MODULE_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(108 * n)
        out = []
        for i in range(n):
            base, size, _ck, _ts, name_rva = struct.unpack_from("<QIIII", raw, 108 * i)
            f.seek(name_rva)
            ln = struct.unpack("<I", f.read(4))[0]
            nm = f.read(ln).decode("utf-16-le", "replace") if 0 < ln < 4096 else "?"
            out.append({"base": base, "size": size, "name": nm})
        out.sort(key=lambda m: m["base"])
        return out

    def _read_unloaded(self, f) -> list:
        """MINIDUMP_UNLOADED_MODULE_LIST 有个 **12 字节头**（SizeOfHeader/SizeOfEntry/
        NumberOfEntries，全是 u32），后面才是条目；条目 24 字节：
        BaseOfImage(8) + SizeOfImage(4) + CheckSum(4) + TimeDateStamp(4) + ModuleNameRva(4)。
        ★ 第一版把"头"当成了"条目数"、又按 12 字节/条解 —— 真实游戏 dump 上名字全是乱码。
        """
        if MD_STREAM_UNLOADED_MODULE_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_UNLOADED_MODULE_LIST]
        f.seek(rva)
        # ★ 这个头是 3 个 u32（12 字节），不是 <IIQ —— MemoryInfoList 那个才是 u64。
        size_of_header, size_of_entry, n = struct.unpack("<III", f.read(12))
        if size_of_header < 12 or size_of_header > 64 or n > 100000:
            return []
        stride = size_of_entry if size_of_entry >= 24 else 24
        f.seek(rva + size_of_header)
        raw = f.read(stride * n)
        out = []
        for i in range(n):
            base, size, _ck, _ts, name_rva = struct.unpack_from("<QIIII", raw,
                                                                stride * i)
            f.seek(name_rva)
            ln = struct.unpack("<I", f.read(4))[0]
            nm = f.read(ln).decode("utf-16-le", "replace") if 0 < ln < 4096 else "?"
            out.append({"base": base, "size": size, "name": nm})
        return out

    def _read_threads(self, f) -> list:
        if MD_STREAM_THREAD_LIST not in self.streams:
            return []
        _sz, rva = self.streams[MD_STREAM_THREAD_LIST]
        f.seek(rva)
        n = struct.unpack("<I", f.read(4))[0]
        raw = f.read(48 * n)
        out = []
        for i in range(n):
            tid, suspend, pri_class, pri, teb = struct.unpack_from("<IIIIQ", raw, 48 * i)
            stack_start, _stack_sz, _stack_rva = struct.unpack_from(
                "<QII", raw, 48 * i + 24)
            ctx_sz, ctx_rva = struct.unpack_from("<II", raw, 48 * i + 40)
            rip = None
            if ctx_rva and ctx_sz >= 0x100:
                f.seek(ctx_rva + 0xF8)
                rip = struct.unpack("<Q", f.read(8))[0]
            out.append({"tid": tid, "suspend": suspend, "teb": teb, "rip": rip,
                        "stack_start": stack_start, "context_rva": ctx_rva,
                        "context_size": ctx_sz})
        out.sort(key=lambda t: t["tid"])
        return out

    def _read_exception(self, f):
        """MINIDUMP_EXCEPTION_STREAM：ThreadId(4) __align(4) MINIDUMP_EXCEPTION(152)
        ThreadContext(MINIDUMP_LOCATION_DESCRIPTOR 8) = 168 字节。
        MINIDUMP_EXCEPTION 里的 ExceptionAddress 在 +16（64 位字面值）。"""
        if MD_STREAM_EXCEPTION not in self.streams:
            return None
        _sz, rva = self.streams[MD_STREAM_EXCEPTION]
        f.seek(rva)
        raw = f.read(168)
        tid, _pad = struct.unpack_from("<II", raw, 0)
        code, flags = struct.unpack_from("<II", raw, 8)
        addr = struct.unpack_from("<Q", raw, 16)[0]
        nparams = struct.unpack_from("<I", raw, 32)[0]
        info = list(struct.unpack_from("<15Q", raw, 40))
        ctx_sz, ctx_rva = struct.unpack_from("<II", raw, 160)
        rip = None
        if ctx_rva and ctx_sz >= 0x100:
            f.seek(ctx_rva + 0xF8)
            rip = struct.unpack("<Q", f.read(8))[0]
        return {"tid": tid, "code": code, "flags": flags, "address": addr,
                "num_parameters": nparams,
                "information": info[:max(0, min(nparams, 15))],
                "context_rva": ctx_rva, "context_size": ctx_sz, "rip": rip}

    def _read_memory_info(self, f) -> list:
        """MINIDUMP_MEMORY_INFO_LIST：头 16 字节（SizeOfHeader u32 / SizeOfEntry u32 /
        NumberOfEntries u64），条目 48 字节 × SizeOfEntry。
        ★ 第一版按 <IQ（12 字节）读头 —— 偏移全错，结果整张内存表读成空表，
          真实 dump 上被判成"没抓到堆"。"""
        _sz, rva = self.streams[MD_STREAM_MEMORY_INFO_LIST]
        f.seek(rva)
        _size_of_header, size_of_entry, n = struct.unpack("<IIQ", f.read(16))
        stride = size_of_entry if size_of_entry >= 48 else 48
        raw = f.read(stride * n)
        out = []
        for i in range(n):
            (base, alloc_base, alloc_prot, _a1, region, state, protect, typ,
             _a2) = struct.unpack_from("<QQIIQIIII", raw, stride * i)
            out.append({"base": base, "alloc_base": alloc_base,
                        "alloc_protect": alloc_prot, "size": region,
                        "state": state, "protect": protect, "type": typ})
        out.sort(key=lambda r: r["base"])
        return out

    def _read_misc(self, f):
        """MINIDUMP_MISC_INFO：SizeOfInfo/Flags1/ProcessId/ProcessCreateTime/
        ProcessUserTime/ProcessKernelTime —— 后四个是 **ULONG32**（不是 64 位）。"""
        if MD_STREAM_MISC_INFO not in self.streams:
            return None
        _sz, rva = self.streams[MD_STREAM_MISC_INFO]
        f.seek(rva)
        raw = f.read(24)
        if len(raw) < 24:
            return None
        size_of_info, flags1, pid, create_time = struct.unpack_from("<IIII", raw, 0)
        return {"size": size_of_info, "flags": flags1, "pid": pid,
                "create_time": create_time,
                "uptime_s": (self.timestamp - create_time) if create_time else None}

    # -- 读内存 --------------------------------------------------------------
    def read(self, addr: int, size: int) -> bytes | None:
        if not addr or size <= 0:
            return None
        i = bisect_right(self._starts, addr) - 1
        if i < 0:
            return None
        va, sz, off = self.ranges[i]
        if addr + size > va + sz:
            return None
        with open(self.path, "rb") as f:
            f.seek(off + (addr - va))
            d = f.read(size)
        return d if len(d) == size else None

    def u64(self, addr: int):
        d = self.read(addr, 8)
        return struct.unpack("<Q", d)[0] if d else None

    def u32(self, addr: int):
        d = self.read(addr, 4)
        return struct.unpack("<I", d)[0] if d else None

    # -- 地址分类 ------------------------------------------------------------
    def region_of(self, addr: int) -> dict | None:
        lo, hi = 0, len(self.memory_info) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            r = self.memory_info[mid]
            if addr < r["base"]:
                hi = mid - 1
            elif addr >= r["base"] + r["size"]:
                lo = mid + 1
            else:
                return r
        return None

    def in_dump(self, addr: int, size: int = 1) -> bool:
        i = bisect_right(self._starts, addr) - 1
        if i < 0:
            return False
        va, sz, _off = self.ranges[i]
        return addr + size <= va + sz

    def module_of(self, addr: int) -> dict | None:
        lo, hi = 0, len(self.modules) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            m = self.modules[mid]
            if addr < m["base"]:
                hi = mid - 1
            elif addr >= m["base"] + max(m["size"], 1):
                lo = mid + 1
            else:
                return m
        return None

    def memory_stats(self) -> dict:
        """这份 dump 到底抓到多少内存 —— 「小 dump（没堆）」要如实说。

        判据（不猜）：
          * 有没有 MemoryInfoListStream（= 生成时带没带 MiniDumpWithFullMemoryInfo）
          * 实际抓到的字节数
          * 有内存信息表时，把"已提交私有内存"加起来比一比 —— 差一个数量级就是"没堆"
        UE 自带那份 UEMinidump.dmp 是 0.7MB 级别，这里会直接判成 small。
        """
        captured = sum(size for _va, size, _off in self.ranges)
        try:
            file_size = os.path.getsize(self.path)
        except OSError:
            file_size = 0
        committed_private = committed_image = committed_other = 0
        for r in self.memory_info:
            if r["state"] != 0x1000:
                continue
            if r["type"] == 0x20000:
                committed_private += r["size"]
            elif r["type"] == 0x1000000:
                committed_image += r["size"]
            else:
                committed_other += r["size"]
        has_mi = bool(self.memory_info)
        heap_available = None
        if has_mi:
            heap_available = bool(committed_private > 0
                                  and captured >= 0.5 * committed_private)
        elif captured < 256 * 1024 ** 2:
            heap_available = False
        if captured < 256 * 1024 ** 2 and not has_mi:
            verdict = ("small dump（文件 %.1f MB / 抓到 %.1f MB）：没有 FullMemoryInfo、"
                       "也没有堆 —— 只有模块/线程/栈和少量内存。想查对象内容必须用 "
                       "crashdump 落的完整 dump。" % (file_size / 1e6, captured / 1e6))
        elif has_mi and heap_available:
            verdict = ("full dump：抓到 %.2f GB（私有提交 %.2f GB），堆在里面"
                       % (captured / 2**30, committed_private / 2**30))
        elif has_mi:
            verdict = ("不完整：只抓到 %.1f MB，而进程私有提交内存有 %.2f GB —— "
                       "大部分堆没进 dump（写到一半？）"
                       % (captured / 1e6, committed_private / 2**30))
        else:
            verdict = ("未知：有 %.2f GB 内存但没带 FullMemoryInfo 表，"
                       "无法判断堆的完整性" % (captured / 2**30))
        return {"file_size": file_size,
                "captured_bytes": captured, "captured_mb": round(captured / 1e6, 1),
                "ranges": len(self.ranges),
                "has_memory_info_list": has_mi,
                "committed_private_bytes": committed_private,
                "committed_image_bytes": committed_image,
                "committed_other_bytes": committed_other,
                "heap_available": heap_available, "verdict": verdict}

    def classify(self, addr: int) -> dict:
        """这个地址在 dump 里是什么？—— 给「哪个委托实例已被释放」当判据。"""
        r = self.region_of(addr)
        out = {"address": addr, "state": None, "type": None, "protect": None,
               "region_base": None, "region_size": None, "allocation_base": None,
               "in_dump": self.in_dump(addr, 8), "module": None,
               "verdict": "unknown"}
        if r:
            out.update(state=MEM_STATES.get(r["state"], hex(r["state"])),
                       type=MEM_TYPES.get(r["type"], hex(r["type"])),
                       protect="|".join(n for v, n in PAGE_PROTECT.items()
                                        if r["protect"] & v) or hex(r["protect"]),
                       region_base=r["base"], region_size=r["size"],
                       allocation_base=r["alloc_base"])
        m = self.module_of(addr)
        if m:
            out["module"] = m["name"]
            out["module_rva"] = addr - m["base"]
        if r and r["state"] == 0x10000:
            out["verdict"] = "unmapped(MEM_FREE)"
        elif r and r["state"] == 0x2000:
            out["verdict"] = "reserved-but-not-committed"
        elif r and (r["protect"] & 0x01):            # PAGE_NOACCESS
            out["verdict"] = "committed-PAGE_NOACCESS"
        elif r and r["state"] == 0x1000:
            out["verdict"] = "committed"
        elif r is None and not self.memory_info:
            out["verdict"] = "unknown(no MemoryInfoList)"
        elif r is None:
            out["verdict"] = "outside-any-region"
        if not out["in_dump"] and out["verdict"] == "committed":
            out["verdict"] = "committed-but-not-captured"
        return out


# --------------------------------------------------------------------------- cdb
CMD_ANALYZE = ".ecxr; k 30; r; !analyze -v; ~*k 8; q"
RE_FRAME = re.compile(r"^\s*(?:([0-9a-f]{2})\s+)?([0-9a-f]{16})\s+([0-9a-f]{16})\s+(\S.*?)\s*$")
RE_THREAD = re.compile(r"^\s*([.\s])*\s*(\d+)\s+Id:\s*([0-9a-f]+)\.([0-9a-f]+)\s+Suspend:")
RE_REGVAL = re.compile(r"([a-z][a-z0-9]{1,3})=([0-9a-f]{2,17})")
REGS_WANTED = ("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "rip",
               "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
               "efl", "cs", "ss", "ds", "es", "fs", "gs", "iopl")


def cdb_path(cdb: str | None = None) -> str:
    path = cdb or CDB
    if not os.path.exists(path):
        raise DumpError("找不到 cdb：%s（需求里写死的路径；装了 Windows SDK 调试器才有）"
                        % path)
    return path


def run_cdb(dump_path: str, commands: str, txt_path: str, cdb: str | None = None,
            timeout: float = 1200.0, symbol_dir: str | None = None) -> dict:
    """跑一次 cdb，把**原始输出**存成 txt（成功失败都存，失败了更要看）。

    符号：只给一个**本地空目录**（`-y` + `_NT_SYMBOL_PATH`）。
    这样 cdb 只用模块自带的导出表，一个字节都不会去够网络 ——
    "没装符号服务器也能跑通"就是靠这条。
    """
    exe = cdb_path(cdb)
    symbol_dir = symbol_dir or os.path.join(os.path.dirname(os.path.abspath(dump_path)),
                                            "symbols")
    os.makedirs(symbol_dir, exist_ok=True)
    env = dict(os.environ)
    env["_NT_SYMBOL_PATH"] = symbol_dir          # ★ 覆盖掉机器上可能存在的 srv* 设置
    env["_NT_ALT_SYMBOL_PATH"] = symbol_dir
    env["_NT_EXECUTABLE_IMAGE_PATH"] = ""
    cmd = [exe, "-z", os.path.abspath(dump_path), "-y", symbol_dir, "-c", commands]
    t0 = time.perf_counter()
    timed_out = False
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        raw, _ = proc.communicate()
    dt = time.perf_counter() - t0
    text = _decode_console(raw)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# crashdump.py analyze: %s\n" % exe)
        f.write("# dump: %s\n" % os.path.abspath(dump_path))
        f.write("# cdb -z <dump> -y <本地符号目录> -c \"%s\"\n" % commands)
        f.write("# 耗时 %.1fs%s\n\n" % (dt, "（超时被杀）" if timed_out else ""))
        f.write(text)
    return {"ok": proc.returncode == 0 and not timed_out, "seconds": round(dt, 2),
            "timed_out": timed_out, "text": text, "txt": txt_path,
            "returncode": proc.returncode, "commands": commands}


def _decode_console(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _after_command_echo(text: str) -> str:
    """cdb 会把 `cdb: Reading initial command '...'` 打出来；从它后面开始解析，
    免得把启动横幅里的东西当结果。"""
    idx = text.find("cdb: Reading initial command")
    if idx < 0:
        return text
    nl = text.find("\n", idx)
    return text[nl + 1:] if nl >= 0 else text


def parse_registers(text: str) -> dict:
    body = _after_command_echo(text)
    regs = {}
    for line in body.splitlines():
        if not RE_REGVAL.search(line.replace("`", "")):
            if regs:
                break                   # 寄存器块是连续的，断了就收工
            continue
        for name, val in RE_REGVAL.findall(line.replace("`", "")):
            if name in REGS_WANTED and name not in regs:
                try:
                    regs[name] = int(val, 16)
                except ValueError:
                    pass
    return regs


def _split_callsite(text: str) -> dict:
    """`module!sym+0x12` / `module+0x12` / `module!sym` → 三段。"""
    mod, _, rest = text.partition("!")
    sym, off = (rest or None), None
    if sym and "+" in sym:
        sym, _, off = sym.rpartition("+")
    elif not rest and "+" in mod:
        mod, _, off = mod.rpartition("+")
    return {"module": mod.strip() or None, "symbol": (sym.strip() if sym else None),
            "offset": off, "text": text}


def parse_frames(block: str, limit: int = 30) -> list:
    out = []
    for line in block.splitlines():
        m = RE_FRAME.match(line.replace("`", ""))
        if not m:
            if out:
                break
            continue
        frame = {"sp": int(m.group(2), 16), "ret": int(m.group(3), 16)}
        frame.update(_split_callsite(m.group(4).strip()))
        out.append(frame)
        if len(out) >= limit:
            break
    return out


def parse_threads(text: str, frames_per_thread: int = 8) -> list:
    """把 `~*k 8` 那段拆成 [{tid, current, frames:[...]}]。"""
    body = _after_command_echo(text)
    lines = body.splitlines()
    threads, cur = [], None
    for i, line in enumerate(lines):
        m = RE_THREAD.match(line.replace("`", ""))
        if m:
            cur = {"tid": int(m.group(4), 16), "pid": int(m.group(3), 16),
                   "current": bool(m.group(1) and m.group(1).strip() == "."),
                   "suspend": None, "frames": []}
            sm = re.search(r"Suspend:\s*(\d+)", line)
            if sm:
                cur["suspend"] = int(sm.group(1))
            threads.append(cur)
            continue
        if cur is None:
            continue
        fm = RE_FRAME.match(line.replace("`", ""))
        if fm and len(cur["frames"]) < frames_per_thread:
            fr = {"sp": int(fm.group(2), 16), "ret": int(fm.group(3), 16)}
            fr.update(_split_callsite(fm.group(4).strip()))
            cur["frames"].append(fr)
    return threads


def _analyze_fields(text: str) -> dict:
    """从 `!analyze -v` 里抠几个名字段（只是旁证，主判据用我们自己解析的 dump）。"""
    out = {}
    pats = {
        "bugcheck_str": r"BUGCHECK_STR:\s*(.+)",
        "failure_bucket": r"FAILURE_BUCKET_ID:\s*(.+)",
        "module_name": r"MODULE_NAME:\s*(.+)",
        "image_name": r"IMAGE_NAME:\s*(.+)",
        "process_name": r"PROCESS_NAME:\s*(.+)",
        "exception_code_line": r"EXCEPTION_CODE:\s*(.+)",
        "os_version": r"OS_VERSION:\s*(.+)",
    }
    for key, pat in pats.items():
        m = re.search(pat, text)
        if m:
            out[key] = m.group(1).strip()
    m = re.search(r"FAULTING_IP:\s*\n\s*(\S+)\s*\n\s*([0-9a-f`]+)\s", text)
    if m:
        out["faulting_ip_symbol"] = m.group(1)
        out["faulting_ip"] = int(m.group(2).replace("`", ""), 16)
    return out


def _frida_report(threads: list, crash_tid: int | None) -> dict:
    """frida_agent 当时在不在跑？（栈里有没有 frida 帧 / 有没有卡在 ProcessEvent 里）

    这是给"崩溃是不是我们的自动化（frida）引起的"提供第一手判据。
    """
    frida_threads, pe_threads = [], []
    for t in threads:
        blob = " ".join((f.get("text") or "") for f in t["frames"]).lower()
        if "frida" in blob:
            frida_threads.append(t["tid"])
        if "processevent" in blob or "process_internal" in blob:
            pe_threads.append(t["tid"])
    crash = next((t for t in threads if t["tid"] == crash_tid), None)
    crash_frames = [f["text"] for f in crash["frames"]] if crash else []
    return {
        "threads_with_frida_frames": frida_threads,
        "threads_in_processevent": pe_threads,
        "crash_thread_has_frida_frame": any("frida" in (f or "").lower()
                                            for f in crash_frames),
        "crash_thread_in_processevent": crash_tid in pe_threads,
        "crash_thread_frames": crash_frames,
    }


def analyze(dump_path: str, heap: int | str | None = None, cdb: str | None = None,
            timeout: float = 1200.0, txt_path: str | None = None,
            delegates: bool = False) -> dict:
    """跑 cdb（`.ecxr; k 30; r; !analyze -v; ~*k 8; q`）→ 同名 .txt + summary。

    没有符号服务器也能跑：cdb 只被喂了本地空符号目录，其余靠模块导出表。
    summary 里的异常码/RIP/RVA/线程数都是**我们自己解 dump** 得到的（可靠），
    cdb 的输出用来给寄存器、调用栈和 !analyze 的旁证。
    """
    dump_path = os.path.abspath(dump_path)
    if not os.path.exists(dump_path):
        raise DumpError("没有这个 dump：%s" % dump_path)
    txt_path = txt_path or os.path.splitext(dump_path)[0] + ".txt"
    md = MiniDump(dump_path)
    res = run_cdb(dump_path, CMD_ANALYZE, txt_path, cdb=cdb, timeout=timeout)
    text = res["text"]
    regs = parse_registers(text)
    frames = parse_frames(_after_command_echo(text), limit=30)
    threads = parse_threads(text)
    exc = md.exception
    crash_tid = exc["tid"] if exc else (next((t["tid"] for t in threads if t["current"]),
                                             None))
    crash_thread = next((t for t in threads if t["tid"] == crash_tid), None)
    fault_rip = (regs.get("rip") if "rip" in regs else None) or (exc or {}).get("rip")
    main = (md.module_of(fault_rip) if fault_rip else None) or \
        main_module_of(md.modules, DEFAULT_IMAGE) or (md.modules[0] if md.modules else None)
    fault_rva = None
    fault_mod = None
    if fault_rip:
        m = md.module_of(fault_rip)
        if m:
            fault_mod, fault_rva = m["name"], fault_rip - m["base"]

    summary = {
        "dump": dump_path,
        "txt": txt_path,
        "size": os.path.getsize(dump_path),
        "dump_time": md.timestamp,
        "process": {"pid": (md.misc or {}).get("pid"),
                    "uptime_s": (md.misc or {}).get("uptime_s"),
                    "image": next((m["name"] for m in md.modules
                                   if main and m["base"] == main["base"]), None)},
        "exception": {
            "code": ("0x%08x" % exc["code"]) if exc else None,
            "code_int": exc["code"] if exc else None,
            "address": exc["address"] if exc else None,
            "tid": exc["tid"] if exc else None,
            "parameters": exc["information"] if exc else None,
            "access_type": (["read", "write", "execute"][exc["information"][0]]
                            if exc and exc["information"]
                            and exc["information"][0] <= 2 else None),
            "access_address": (exc["information"][1] if exc and len(exc["information"]) > 1
                               else None),
            "note": None if exc else
                    "这份 dump 没有异常流（`now` 打的那种）；.ecxr 不可用，"
                    "用 sidecar .json 里的线程 RIP 表定位。",
        },
        "fault_rip": fault_rip,
        "fault_rva": fault_rva,
        "fault_module": fault_mod,
        "main_module_base": main["base"] if main else None,
        "registers": {k: regs.get(k) for k in REGS_WANTED if k in regs},
        "stack": frames[:12],
        "stack_all": frames,
        "crash_thread": crash_tid,
        "thread_count": len(md.threads) or len(threads),
        "threads": [{"tid": t["tid"], "rip": t["rip"], "suspend": t["suspend"]}
                    for t in md.threads] or
                   [{"tid": t["tid"], "suspend": t["suspend"], "rip": None}
                    for t in threads],
        "cdb": {"ok": res["ok"], "seconds": res["seconds"], "timed_out": res["timed_out"],
                "commands": CMD_ANALYZE, "returncode": res["returncode"]},
        "analyze": _analyze_fields(text),
        "memory": md.memory_stats(),
        "modules": {"count": len(md.modules), "unloaded": len(md.unloaded),
                    "main_base": main["base"] if main else None},
    }
    summary["frida"] = _frida_report(threads, crash_tid)
    if not res["ok"] and res["timed_out"]:
        summary["cdb"]["note"] = ("cdb 超时被杀：dump 很大时 `!analyze -v` 会慢；"
                                 "txt 里是已产出的部分")
    if heap is not None:
        try:
            summary["heap"] = analyze_heap(dump_path, heap, md=md, cdb=cdb,
                                           timeout=timeout)
        except BaseException as exc:                        # noqa: BLE001
            summary["heap"] = {"error": "%s: %s" % (type(exc).__name__, exc),
                               "note": "heap 分析自己失败了，别影响主结果"}
    if delegates:
        try:
            summary["delegates"] = scan_delegates(dump_path, md=md, registers=regs,
                                                  rsp=regs.get("rsp"))
        except BaseException as exc:                        # noqa: BLE001
            summary["delegates"] = {"error": "%s: %s" % (type(exc).__name__, exc),
                                   "note": "delegates 扫描自己失败了，别影响主结果"}
    return {"txt": txt_path, "summary": summary}


# --------------------------------------------------------------------------- heap
CMD_HEAP = ".ecxr; r; !address {addr}; dqs {addr} L8; !heap -x {addr}; ln {addr}; q"
REG_CANDIDATES = ("rdi", "rcx", "rax", "rsi", "rbx", "rdx", "rbp", "r8", "r9",
                  "r10", "r11", "r12", "r13", "r14", "r15", "rsp")


class _Names:
    """可选加成：能 import 到 kardsmem/dumpmem 时顺便把名字解出来。

    解不出来（换构建、池地址变了、树被搬走）就整体退化成 None，主流程不受影响。
    崩溃分析工具不能因为"顺手解个名字"就不干活。
    """

    def __init__(self, dump_path: str, base: int | None):
        self.pool = None
        self.why = None
        try:
            from dumpmem import DumpMem                     # noqa: PLC0415
            from kardsmem.names import FNamePool            # noqa: PLC0415
        except BaseException as exc:                        # noqa: BLE001
            self.why = "import 不到 dumpmem/kardsmem.names：%s" % exc
            return
        try:
            self.mem = DumpMem(dump_path)
            self.pool = FNamePool(self.mem, base)
        except BaseException as exc:        # ★ dumpmem 用的是 raise SystemExit
            self.why = "名字池建不起来（多半是这份 dump 没带完整内存）：%s" % exc

    def ok(self) -> bool:
        return self.pool is not None

    def fname(self, index: int, number: int = 0):
        if not self.pool or index in (0, 0xFFFFFFFF):
            return None
        try:
            return self.pool.fname(index, number)
        except Exception:                                   # noqa: BLE001
            return None

    def object_name(self, obj: int):
        if not self.pool or not obj:
            return None
        try:
            return self.pool.fname_of(obj)
        except Exception:                                   # noqa: BLE001
            return None


def _describe_object(md: MiniDump, obj: int, names: _Names | None) -> dict:
    """一个指针指向的到底是不是活的 UObject（UObject 布局：vtable@0, flags@8,
    index@0xC, class@0x10, name@0x18 —— 见 kardsmem/build.py）。"""
    out = {"address": obj, "address_hex": _hex(obj), "region": md.classify(obj),
           "vtable": None, "vtable_module": None, "class": None, "name": None,
           "notes": []}
    if not obj:
        out["notes"].append("空指针")
        return out
    vt = md.u64(obj)
    out["vtable"] = vt
    if vt:
        vm = md.module_of(vt)
        out["vtable_module"] = vm["name"] if vm else None
        if vm is None:
            out["notes"].append("★ vtable（对象第一个 qword）不在任何已加载模块里 —— "
                                "这块内存已经不像活的 UObject（已释放/被覆写）")
    else:
        out["notes"].append("★ 读不出 vtable（这块不在 dump 里，或已不可访问）")
    verdict = out["region"]["verdict"]
    if verdict.startswith("unmapped") or verdict == "reserved-but-not-committed":
        out["notes"].append("★★ 指针落在**未映射**的地址空间 —— 这个委托实例已经被释放"
                            "（页都还给系统了）")
    cls = md.u64(obj + 0x10)
    out["class_ptr"] = cls
    if names and names.ok():
        out["name"] = names.object_name(obj)
        out["class"] = names.object_name(cls) if cls else None
    if out["name"] is None and md.in_dump(obj + 0x18, 8):
        out["name_fname_raw"] = md.u64(obj + 0x18)   # 没名字池，至少留原始 FName
    return out


def _walk_tarray(md: MiniDump, base: int, limit: int = 64) -> dict | None:
    """把 base 当 UE 的 TArray 头（{Data, Num, Max} 24 字节）试着走一遍。"""
    hdr = md.read(base, 24)
    if not hdr:
        return None
    data, num, mx = struct.unpack("<QQQ", hdr)
    if not (1 <= num <= 8192) or mx < num or mx > (1 << 24) or data < 0x10000:
        return None
    if not md.in_dump(data, 16) and not md.region_of(data):
        return None
    entries = []
    for i in range(min(num, limit)):
        raw = md.read(data + 16 * i, 16)
        if not raw:
            entries.append({"i": i, "unreadable": True, "entry": data + 16 * i})
            continue
        obj, fn = struct.unpack("<QQ", raw)
        entries.append({"i": i, "entry": data + 16 * i, "object": obj,
                        "fname_raw": fn, "fname_index": fn & 0xFFFFFFFF,
                        "fname_number": fn >> 32})
    return {"header": base, "data": data, "num": num, "max": mx,
            "in_dump": md.in_dump(data, 16 * max(1, num)), "entries": entries,
            "truncated": num > limit}


def _looks_like_entry(md: MiniDump, addr: int) -> bool:
    """直接指着 entry（Object ptr + FName）而不是 TArray 头的情况。"""
    obj = md.u64(addr)
    if not obj or obj < 0x10000:
        return False
    vt = md.u64(obj)
    return bool(vt and md.module_of(vt))


def analyze_heap(dump_path: str, address, md: MiniDump | None = None,
                 cdb: str | None = None, timeout: float = 1200.0,
                 registers: dict | None = None) -> dict:
    """`analyze --heap <addr>`：这个地址是什么 + 顺着寄存器指的数组把委托列表读出来。

    两个来源合起来给判据：
      * cdb 的 `!address` / `dqs` / `!heap -x` / `ln`（人读的原文进同名 txt）
      * 我们自己解 dump（机器可读：区段状态、每个 entry 的指针落在哪、vtable 在不在
        模块里）—— 后面这几条直接回答"哪个委托实例已经被释放"。
    """
    addr = address if isinstance(address, int) else int(str(address), 0)
    dump_path = os.path.abspath(dump_path)
    md = md or MiniDump(dump_path)
    txt = "%s.heap-%x.txt" % (os.path.splitext(dump_path)[0], addr)
    cmds = CMD_HEAP.format(addr=_hex(addr))
    res = run_cdb(dump_path, cmds, txt, cdb=cdb, timeout=timeout)
    regs = registers or parse_registers(res["text"])
    base = main_module_of(md.modules, DEFAULT_IMAGE)
    names = _Names(dump_path, base["base"] if base else None)

    report = {
        "address": addr,
        "address_hex": _hex(addr),
        "region": md.classify(addr),
        "qwords": [{"address": addr + 8 * i, "value": md.u64(addr + 8 * i)}
                   for i in range(8)],
        "module": (md.module_of(addr) or {}).get("name"),
        "in_dump": md.in_dump(addr, 8),
        "cdb_txt": txt,
        "cdb": {"ok": res["ok"], "seconds": res["seconds"], "commands": cmds,
                "timed_out": res["timed_out"]},
        "registers": {k: regs.get(k) for k in REG_CANDIDATES if k in regs},
        "array_candidates": [],
        "names": {"resolved": names.ok(), "why": names.why},
        "verdict": [],
    }
    seen = set()
    sources = [(r, regs.get(r)) for r in REG_CANDIDATES if regs.get(r)]
    sources.append(("(给定地址)", addr))
    for src, ptr in sources:
        if not ptr or ptr < 0x10000:
            continue
        arr = _walk_tarray(md, ptr)
        if arr is not None and ("hdr", ptr) not in seen:
            seen.add(("hdr", ptr))
            for e in arr["entries"]:
                if e.get("unreadable"):
                    continue
                e["object_info"] = _describe_object(md, e["object"], names)
                e["fname"] = names.fname(e["fname_index"], e["fname_number"])
                e["suspect"] = any(n.startswith("★")
                                   for n in e["object_info"]["notes"])
            report["array_candidates"].append({
                "source": "%s=%s" % (src, _hex(ptr)),
                "kind": "TArray 头 {Data,Num,Max}",
                "header": arr["header"], "data": arr["data"], "num": arr["num"],
                "max": arr["max"], "in_dump": arr["in_dump"],
                "truncated": arr["truncated"], "entries": arr["entries"]})
            report["verdict"].append("%s 指向一个 TArray 头：Data=%s Num=%d Max=%d"
                                     % (src, _hex(arr["data"]), arr["num"], arr["max"]))
        if _looks_like_entry(md, ptr) and ("data", ptr) not in seen:
            seen.add(("data", ptr))
            entries = []
            for i in range(8):
                raw = md.read(ptr + 16 * i, 16)
                if not raw:
                    break
                obj, fn = struct.unpack("<QQ", raw)
                info = _describe_object(md, obj, names)
                entries.append({"i": i, "entry": ptr + 16 * i, "object": obj,
                                "fname_index": fn & 0xFFFFFFFF,
                                "fname_number": fn >> 32, "object_info": info,
                                "fname": names.fname(fn & 0xFFFFFFFF, fn >> 32),
                                "suspect": any(n.startswith("★")
                                               for n in info["notes"])})
            report["array_candidates"].append({
                "source": "%s=%s" % (src, _hex(ptr)),
                "kind": "委托 entry 数组（直接指着 16 字节项）",
                "data": ptr, "num": len(entries), "entries": entries})
            report["verdict"].append("%s 看起来直接指向委托 entry 数组（16B/项）" % src)
    if not report["array_candidates"]:
        report["verdict"].append(
            "没有哪个通用寄存器/给定地址看起来像委托列表（TArray 头或 entry 数组）。"
            "看 cdb_txt 里的 `!address` / `!heap -x` / `dqs` 原文。")
    return report


def _live_log_path() -> str:
    """监听器日志的落点：优先 `base.paths.LIVE_LOG`（运行期位置只有那一张表），拿不到回退默认。

    ★ 为什么不直接写死 import：本脚本要在整棵树被搬动/改坏时照常能跑（见文件头设计要点 7），
      所以做成"能拿到就拿、拿不到回退"——别让这一步把 .context.txt 带崩。
      （2026-10-03：监听器从 `_nn_scratch/` 搬进 `tools/`，通道移到 `kards-data/live/`。）
    """
    try:
        from base import paths as _paths          # noqa: PLC0415
        return _paths.LIVE_LOG
    except Exception:                              # noqa: BLE001
        return os.path.join("D:" + os.sep, "Kards", "kards-data", "live", "live_log.txt")


def _context_bundle(dump_path: str, tail: int = 30) -> str:
    """把"崩溃前我们最后做了什么"拷到 dump 旁边（同名 .context.txt）。

    两路来源（都只读，缺了就写缺）：
      * D:\\Kards\\kards-data\\nn\\logs\\rule-live-*.jsonl 里**最新**那个的最后 30 行
      * 监听器日志（`base.paths.LIVE_LOG`，默认 D:\\Kards\\kards-data\\live\\live_log.txt）的最后 30 行
    目的：打开 dump 的那一瞬就能对上"崩溃前自动化发了什么命令"。
    """
    from base import paths as _paths_cd
    src_a = _paths_cd.NN_LOG_DIR
    src_b = _live_log_path()
    out_path = os.path.splitext(dump_path)[0] + ".context.txt"
    lines = ["# crashdump.py 现场上下文  dump=%s" % os.path.abspath(dump_path),
             "# UTC=%s  本地=%s"
             % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))]
    newest = None
    try:
        cands = [os.path.join(src_a, n) for n in os.listdir(src_a)
                 if n.startswith("rule-live-") and n.endswith(".jsonl")]
        if cands:
            newest = max(cands, key=lambda p: os.path.getmtime(p))
    except OSError:
        newest = None
    for label, path in (("rule-live（最新）", newest), ("live_log.txt", src_b)):
        lines.append("")
        if not path:
            lines.append("## %s —— 没有（目录里找不到 rule-live-*.jsonl）" % label)
            continue
        lines.append("## %s —— %s（最后 %d 行，原样）" % (label, path, tail))
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                back = min(size, 65536)
                f.seek(size - back)
                blob = f.read(back)
            text = _decode_console(blob)
            ls = [ln for ln in text.splitlines() if ln.strip()]
            lines.extend(ls[-tail:])
        except OSError as exc:
            lines.append("## 读不了 %s：%s" % (path, exc))
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return out_path


# --------------------------------------------------------------------------- 自身开销
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE,
                                        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                        wt.DWORD]
_psapi.GetProcessMemoryInfo.restype = wt.BOOL
k32.GetProcessTimes.restype = wt.BOOL
_SELF_BASE: dict = {}


def self_usage() -> dict:
    """watch 自己吃多少（CPU 秒 / 常驻内存）—— 验收要报这个。

    CPU% 是**相对 watch 启动那一刻**的平均值（`cpu_s / 墙钟`），
    所以常驻时看到的就是"平均拖了多少 CPU"。
    """
    h = k32.GetCurrentProcess()
    ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    cpu = None
    if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                           ctypes.byref(kt), ctypes.byref(ut)):
        cpu = ((kt.dwHighDateTime << 32 | kt.dwLowDateTime)
               + (ut.dwHighDateTime << 32 | ut.dwLowDateTime)) / 1e7
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    ws = peak = pf = None
    if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
        ws, peak, pf = pmc.WorkingSetSize, pmc.PeakWorkingSetSize, pmc.PageFaultCount
    now = time.time()
    if "cpu" not in _SELF_BASE and cpu is not None:
        _SELF_BASE.update(cpu=cpu, time=now)
    out = {"cpu_s": round(cpu, 3) if cpu is not None else None,
           "working_set_mb": round((ws or 0) / 2**20, 1),
           "peak_working_set_mb": round((peak or 0) / 2**20, 1),
           "page_faults": pf}
    if cpu is not None and "cpu" in _SELF_BASE:
        wall = max(1e-6, now - _SELF_BASE["time"])
        out["wall_s"] = round(wall, 1)
        out["cpu_pct"] = round(100.0 * (cpu - _SELF_BASE["cpu"]) / wall, 3)
    return out


# --------------------------------------------------------------------------- exe 指纹
_FINGERPRINT_CACHE: dict = {}


def pe_fingerprint(path: str) -> dict | None:
    """SizeOfImage / TimeDateStamp / md5 / 段表 —— **认构建靠 SizeOfImage**
    （RVA 能不能用只看它；md5 只说明这份副本有没有被动过）。

    带缓存：同一个 (路径, 大小, mtime) 只算一次。md5 要读 1~2GB，
    绝不能在"游戏冻住"的窗口里做。
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"error": "stat 失败：%s" % exc, "path": path}
    key = (path, st.st_size, int(st.st_mtime))
    if key in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE[key]
    out = {"path": path, "file_size": st.st_size, "md5": None,
           "size_of_image": None, "timestamp": None, "timestamp_utc": None,
           "machine": None, "sections": []}
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if head[:2] != b"MZ":
                raise ValueError("不是 PE（没有 MZ）")
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            f.seek(e_lfanew)
            pe = f.read(24)
            if pe[:4] != b"PE\0\0":
                raise ValueError("不是 PE（没有 PE\\0\\0）")
            machine, nsec, stamp, _psym, _nsym, opt_size, _chars = \
                struct.unpack_from("<HHIIIHH", pe, 4)
            out.update(machine="0x%04x" % machine, timestamp=stamp,
                       timestamp_utc=(time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime(stamp)) if stamp else None))
            opt = f.read(opt_size)
            magic = struct.unpack_from("<H", opt, 0)[0] if len(opt) >= 2 else 0
            if magic == 0x20B:                       # PE32+
                out["size_of_image"] = struct.unpack_from("<I", opt, 0x38)[0]
                out["image_base"] = struct.unpack_from("<Q", opt, 0x18)[0]
            elif magic == 0x10B:                     # PE32
                out["size_of_image"] = struct.unpack_from("<I", opt, 0x38)[0]
                out["image_base"] = struct.unpack_from("<I", opt, 0x1C)[0]
            else:
                raise ValueError("optional header magic=0x%x" % magic)
            f.seek(e_lfanew + 24 + opt_size)
            secs = []
            for _ in range(nsec):
                raw = f.read(40)
                if len(raw) < 40:
                    break
                name = raw[:8].rstrip(b"\0").decode("latin-1")
                vsize, vaddr = struct.unpack_from("<II", raw, 8)
                raw_size, _raw_ptr = struct.unpack_from("<II", raw, 16)
                chars = struct.unpack_from("<I", raw, 36)[0]
                secs.append({"name": name, "virtual_address": vaddr,
                             "virtual_size": vsize, "raw_size": raw_size,
                             "characteristics": "0x%08x" % chars})
            out["sections"] = secs
            # md5 放最后（唯一的大 IO），分块读
            h = hashlib.md5()
            f.seek(0)
            while True:
                chunk = f.read(4 << 20)
                if not chunk:
                    break
                h.update(chunk)
            out["md5"] = h.hexdigest()
    except (OSError, ValueError, struct.error) as exc:
        out["error"] = str(exc)
    _FINGERPRINT_CACHE[key] = out
    return out


def section_of(fingerprint: dict | None, base: int, va: int) -> dict | None:
    """addr 落在哪个 PE 段里（给"虚表在不在 .rdata"当判据）。"""
    if not fingerprint or not base:
        return None
    off = va - base
    for s in fingerprint.get("sections") or []:
        start = s["virtual_address"]
        size = max(s["virtual_size"], s["raw_size"])
        if size and start <= off < start + size:
            return {"name": s["name"], "offset": off - start}
    return None


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__alignment1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD), ("Protect", wt.DWORD),
                ("Type", wt.DWORD), ("__alignment2", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
MAX_USER_ADDRESS = 0x7FFFFFFF0000


def memory_map_csv(pid: int, csv_path: str) -> dict:
    """`!address -summary` 的原料：VirtualQueryEx 走一遍地址空间 → CSV + 汇总。

    写 CSV 是需求点名的（基址,大小,状态,保护,类型）。跟着 dump 走的好处：
    dump 打开后一眼能看出某个野指针落在**已提交 / 已保留 / 未映射**哪一类 ——
    0x1e3020030 这种值到底是不是"页都还给系统了"，看这张表最快。
    * 走的时候进程若冻着（watch 路径），这张表和 dump 是同一时刻的。
    """
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_VM_READ, False, pid)
    if not h:
        return {"error": "OpenProcess 失败 err=%d" % ctypes.get_last_error(),
                "csv": None, "regions": 0}
    rows = []
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    guard = 0
    try:
        while addr < MAX_USER_ADDRESS and guard < 500000:
            guard += 1
            got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                     ctypes.sizeof(mbi))
            if not got:
                addr += 0x1000            # 空洞：翻一页继续
                continue
            if mbi.RegionSize == 0:
                break
            rows.append((mbi.BaseAddress, mbi.RegionSize, mbi.State, mbi.Protect,
                         mbi.Type))
            addr = mbi.BaseAddress + mbi.RegionSize
    finally:
        k32.CloseHandle(h)
    by_state: dict = {}
    by_type: dict = {}
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("base,size,state,protect,type\n")
        for base, size, state, protect, typ in rows:
            f.write("0x%x,0x%x,%s,%s,%s\n"
                    % (base, size, MEM_STATES.get(state, "0x%x" % state),
                       "|".join(n for v, n in PAGE_PROTECT.items() if protect & v)
                       or "0x%x" % protect,
                       MEM_TYPES.get(typ, "0x%x" % typ if typ else "0")))
            s = MEM_STATES.get(state, "0x%x" % state)
            by_state.setdefault(s, {"count": 0, "bytes": 0})
            by_state[s]["count"] += 1
            by_state[s]["bytes"] += size
            t = MEM_TYPES.get(typ, "none" if not typ else "0x%x" % typ)
            by_type.setdefault(t, {"count": 0, "bytes": 0})
            by_type[t]["count"] += 1
            by_type[t]["bytes"] += size
    return {"csv": csv_path, "regions": len(rows), "by_state": by_state,
            "by_type": by_type, "scanned_to": addr,
            "note": "VirtualQueryEx 现场快照；dump 里同一地址若已不在这些区段里，"
                    "就是崩溃后又被改过（正常，UE 一直在分配）"}


def build_sidecars(pid: int, dump_path: str, info: dict, modules: list,
                   image_name: str = DEFAULT_IMAGE) -> dict:
    """dump 旁边那几份附属文件（.json 的公共部分）。

    ★ 顺序讲究：这里所有操作都**不需要**进程冻着（除了调用方在冻着的时候顺带
      拿过一次一致性快照），所以 watch 路径把它放在 ContinueDebugEvent 之后也行；
      但 memory.csv 是在冻着的窗口里跑的（和 dump 同一时刻），见 _dump_for。
    """
    extra = {}
    main = main_module_of(modules, image_name)
    fp = pe_fingerprint(info.get("image") or "") if info.get("image") else None
    extra["exe"] = {
        "path": info.get("image"),
        "size_of_image": (fp or {}).get("size_of_image"),
        "size_of_image_in_process": main["size"] if main else None,
        "md5": (fp or {}).get("md5"),
        "pe_timestamp": (fp or {}).get("timestamp"),
        "pe_timestamp_utc": (fp or {}).get("timestamp_utc"),
        "file_size": (fp or {}).get("file_size"),
        "machine": (fp or {}).get("machine"),
        "sections": (fp or {}).get("sections"),
        "why": "RVA 能不能用只看 SizeOfImage；md5 只说明这份副本有没有被动过",
        "error": (fp or {}).get("error"),
    }
    return extra


# --------------------------------------------------------------------------- delegates
DELEGATE_STRIDE = 16        # TArray<FScriptDelegate> 一项 = {UObject*(8), FName{idx4,num4}}


def sidecar_of(dump_path: str) -> dict | None:
    """dump 旁边的同名 .json（`now`/`watch` 都会写）。"""
    p = os.path.splitext(dump_path)[0] + ".json"
    return read_json(p)


def _entry_shape(md: MiniDump, addr: int) -> dict | None:
    raw = md.read(addr, DELEGATE_STRIDE)
    if not raw:
        return None
    ptr, split = struct.unpack("<QQ", raw)
    return {"ptr": ptr, "index": split & 0xFFFFFFFF, "number": split >> 32}


def _plausible_entry(e: dict | None) -> bool:
    """「16 字节一项、第二个 dword 是小的非负整数」的判据（FName 那半截）。

    FName = {ComparisonIndex(u32), Number(u32)}；Number 几乎总是 0，
    索引也是几万这个量级 —— 所以这两个都能当"像不像"的筛子。
    """
    return bool(e and e["ptr"] >= 0x10000
                and e["index"] <= 0x00FFFFFF and e["number"] <= 0xFFFF)


def scan_delegates(dump_path: str, md: MiniDump | None = None,
                   registers: dict | None = None, rsp: int | None = None,
                   stack_qwords: int = 256, limit: int = 64,
                   max_candidates: int = 96) -> dict:
    """`analyze --delegates`：在故障线程的寄存器和栈上找多播委托调用列表。

    判据（需求原文）：16 字节一项、第二个 dword 是小的非负整数；对每一项读第一个
    8 字节指针，看它落不落在已提交页里、它指向的对象第一个 8 字节（虚表指针）落不落
    在主模块 .rdata 里。**只给候选**，不追求 100% 准。

    这是"哪个委托实例被释放了"最直接的答案：
    列表里那一项的指针要么不在已提交页里，要么它的"对象"已经没有正常虚表了。
    """
    dump_path = os.path.abspath(dump_path)
    md = md or MiniDump(dump_path)
    sc = sidecar_of(dump_path) or {}
    regs = dict(registers or {})
    if not regs:
        regs = sc.get("registers") or {}
    main = main_module_of(md.modules, DEFAULT_IMAGE)
    main_base = (main or {}).get("base")
    exe = sc.get("exe") or {}
    sections = exe.get("sections")
    if rsp is None:
        rsp = regs.get("rsp")

    cands: list[tuple[str, int]] = []
    for name in ("rdi", "rcx", "rax", "rdx", "rsi", "rbx", "rbp", "r8", "r9", "r10",
                 "r11", "r12", "r13", "r14", "r15"):
        v = regs.get(name)
        if v:
            cands.append((name, v))
    stack_used = 0
    if rsp:
        blob = md.read(rsp, stack_qwords * 8)
        if blob:
            for i in range(0, len(blob), 8):
                v = struct.unpack_from("<Q", blob, i)[0]
                if 0x10000 <= v < 0x7FFFFFFFFFFF:
                    cands.append(("stack[rsp+0x%x]" % (i,), v))
                    stack_used += 1

    out_cands, suspects = [], []
    seen = set()
    for src, base in cands:
        if base in seen or len(out_cands) >= max_candidates:
            continue
        entries = []
        for i in range(limit):
            e = _entry_shape(md, base + i * DELEGATE_STRIDE)
            if not _plausible_entry(e):
                break
            entries.append({"i": i, "address": base + i * DELEGATE_STRIDE, **e})
        if not entries:
            continue
        committed = [md.classify(e["ptr"])["verdict"].startswith(("committed", "unknown"))
                     for e in entries]
        if len(entries) < 2 and all(committed):
            continue                     # 单项且看着正常：多半是巧合
        seen.add(base)
        for e in entries:
            info = md.classify(e["ptr"])
            vt = md.u64(e["ptr"])
            sec = section_of({"sections": sections}, main_base, vt) if vt else None
            if sec is None and vt and main_base:
                m2 = md.module_of(vt)
                sec = {"name": (m2 or {}).get("name") or "?", "offset": None}
            notes = []
            bad_region = not info["verdict"].startswith(("committed", "unknown"))
            if bad_region:
                notes.append("指针不在已提交页里（%s）—— 这一项指向的实例已被释放"
                             % info["verdict"])
            if vt is None:
                notes.append("读不出虚表（对象内存已不可访问/未捕获）")
            elif main_base and not (vt >= main_base and vt < main_base + (1 << 32)):
                notes.append("虚表不在主模块地址区间（vtable=%s → %s）"
                             % (_hex(vt), (sec or {}).get("name")))
            elif sec and sec.get("name") not in (".rdata", ".data", ".text"):
                notes.append("虚表落在主模块的 %s 段（不是 .rdata）" % sec.get("name"))
            e.update({"region_verdict": info["verdict"], "region_protect": info["protect"],
                      "vtable": vt, "vtable_section": (sec or {}).get("name"),
                      "vtable_rva": (vt - main_base) if (vt and main_base) else None,
                      "suspect": bool(notes), "notes": notes})
            if e["suspect"]:
                suspects.append({"address": e["address"], "index": e["i"], "base": base,
                                 "source": src, "ptr": e["ptr"],
                                 "verdict": info["verdict"], "notes": notes})
        out_cands.append({"source": src, "base": base, "count": len(entries),
                          "entries": entries})
    return {
        "registers_used": {k: regs.get(k) for k in
                           ("rdi", "rcx", "rax", "rdx", "rsi", "rbx", "rbp", "rsp")
                           if regs.get(k)},
        "stack_scanned_qwords": stack_used,
        "stack_base": rsp,
        "main_module_base": main_base,
        "rdata_known": bool(sections),
        "candidates": out_cands,
        "suspects": suspects,
        "note": ("rdata_known=False 表示没读到 sidecar .json 里的段表，"
                 "「虚表在不在 .rdata」这条判据退化成了「在不在主模块内」。"),
    }
