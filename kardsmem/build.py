#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.build —— 构建指纹与 RVA 的**唯一入口**（种子表加载 + 解析链）。

★ 2026-10-03 P7：RVA 不再靠"登记版本"放行。数据只有 `build_tables.json`（随包种子），运行时走
`resolve()`：用户缓存 → 种子复验 → 运行时扫描（全只读）；未登记的版本照样能起来。见 `docs/ARCH-RELEASE-VCS.md`。
下面"SizeOfImage 决定偏移能不能用"的论述仍成立——它现在是**种子/缓存匹配**的键，而不是登记制的闸门。

为什么单独一层
==============
磁盘上有**多份同名** `kards-Win64-Shipping.exe`（多棵安装树），偏移各不相同。
判据只有一条：**SizeOfImage 决定这是哪一份构建、偏移表能不能用**；
md5 只说明这一份副本的字节有没有被动过 —— **同一个构建的两份副本，md5 允许不同**
（本地改动常常只碰 `.rdata` 里的常量：不移动节、不改代码 ⇒ RVA 全部保持有效）。
详见 `exes.py`（本机副本指纹表）。
任何"先 attach 再读"的代码都必须先证明自己 attached 到的是哪一份，否则读出来的全是垃圾。

本模块把"哪个构建 + 哪些 RVA"集中在一处，其余模块只 import 常量，不各自抄一份地址。

只读。解析链只发 ReadProcessMemory（经调用方传入的 `MemRO`）；本模块自己不写任何进程内存。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _find_workspace() -> Path:
    """往上找仓库根（认标志目录），**不要数 parents[N]**。

    ★ 2026-09-22 搬家踩过：本模块原来在 `reverse-data/tools/kardsmem/`，
      用的是 `parents[3]`；搬到 `kards-agent/kardsmem/` 后层数变了，
      硬编码的层数当场失效。按标志物找就跟目录深度无关了。
    """
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "reverse-data").is_dir() and (p / ".git").exists():
            return p
    return here.parents[2]


WORKSPACE = Path(os.environ.get("KARDS_WORKSPACE") or _find_workspace())
AGENT_ROOT = Path(__file__).resolve().parents[1]    # kards-agent/
REPORTS_DIR = WORKSPACE / "reverse-data" / "reports"
TOOLS_DIR = AGENT_ROOT                      # 自动化脚本根（`ops/inject.py` 等和 kardsmem 同级）
TOOLS_SUB = AGENT_ROOT / "tools"            # 取材/标定/PE 等工具（2026-09-22 从 reverse-data 搬来）
RE_TOOLS_DIR = WORKSPACE / "reverse-data" / "tools"   # 逆向/静态/第三方工具（FModel、u4pak、idmap…）
SPEC_JSON = REPORTS_DIR / "kards-offsets.json"      # 机器可读规格（人工维护）

# --------------------------------------------------------------------------
# 种子表：唯一数据来源 = `kardsmem/build_tables.json`（随包走）
# --------------------------------------------------------------------------
# ★ 2026-10-03 P7（`docs/REFACTOR-PLAN.md`、`docs/ARCH-RELEASE-VCS.md` §7.1）：
#   以前"版本→RVA"有四处手工登记（`build.BUILDS`、`build.RVA_FALLBACK`、`board._BUILD_TABLE`、
#   `version.VERSION_TO_BUILD`），新版本一来就得改代码。现在：
#     * 数据只有 `build_tables.json` 一份：每个已发布构建的**身份**（image_size/exe_size/md5/versions/备注）
#       + **RVA 真值**（`kardsmem.buildsrc` 从 SDK dump + exe 生成）。它只是**种子**，不是放行名单；
#     * 选择只有 `resolve()` 一个入口：用户缓存 → 种子复验 → 运行时扫描（全部只读）；
#     * 版本号（`version.py` 读进程内存）只是缓存键/日志/面板显示，**不再是放行条件**；
#     * `KARDS_BUILD=<种子键>` 仅作调试覆盖（跳过复验，直接用该键的种子值）。
TABLES_JSON = Path(__file__).with_name("build_tables.json")
GAME_EXE = "kards-Win64-Shipping.exe"
DEFAULT_BUILD = "current"          # 没有游戏在跑 / 认不出时的占位种子（只是让 import 期常量有值，attach 时必复验）
OLD = "old"                        # IDA .i64 对应的上一版（RVA_OLD 对照用）

CORE_RVA_KEYS = ("GObjects", "FNamePool", "GWorld", "UObject_ProcessEvent")   # rvascan 能扫出的四项；缺一项都不算解析成功

RVA_SOURCES: dict = {}          # build_key -> {rva 名: 值从哪来}     （buildsrc 生成）
BUILD_TABLE_NOTES: dict = {}    # build_key -> [生成时的告警]


def load_seed_tables(path: Path = TABLES_JSON):
    """读种子表 → `(BUILDS, RVA_BY_BUILD)`；顺带填 `RVA_SOURCES`/`BUILD_TABLE_NOTES`。读不到/坏了 ⇒ 空（链上直接走缓存→扫描）。

    `BUILDS[key]` = 身份与备注（除 rva/sources/notes/key 之外的全部字段）；`RVA_BY_BUILD[key]` = {RVA 名: int}。"""
    builds, rvas, src, notes = {}, {}, {}, {}
    try:
        j = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
    except Exception:                                          # noqa: BLE001
        j = {}
    for k, t in (j.get("builds") or {}).items():
        rva = t.get("rva") or {}
        ident = {kk: vv for kk, vv in t.items() if kk not in ("rva", "sources", "notes", "key")}
        ident.setdefault("module", GAME_EXE)
        ident["versions"] = list(ident.get("versions") or ([ident["version"]] if ident.get("version") else []))
        builds[k] = ident
        rvas[k] = {kk: int(vv) for kk, vv in rva.items()}
        src[k] = t.get("sources") or {}
        notes[k] = t.get("notes") or []
    RVA_SOURCES.clear(); RVA_SOURCES.update(src)
    BUILD_TABLE_NOTES.clear(); BUILD_TABLE_NOTES.update(notes)
    return builds, rvas


BUILDS, RVA_BY_BUILD = load_seed_tables()


def seed_key_for(version: Optional[str] = None, image_size: Optional[int] = None,
                 builds: Optional[dict] = None) -> Optional[str]:
    """种子表里**可能**对得上这个进程的键（纯函数，不碰进程）。没有 ⇒ None（调用方走缓存/扫描，不猜）。

    规则：先按 `image_size` 筛（它决定 RVA 有没有效；给了就必须相等），再在剩下的里优先 `version ∈ versions` 的。
    只给 version 不给 image_size 时按 versions 找（面板显示用，不能当放行依据）。"""
    builds = BUILDS if builds is None else builds
    cands = [k for k, b in builds.items() if image_size is None or b.get("image_size") == image_size]
    if version:
        for k in cands:
            if version in (builds[k].get("versions") or []):
                return k
        if image_size is None:
            return None
    return cands[0] if (cands and image_size is not None) else None


def seed_for_display(version: Optional[str]) -> Optional[str]:
    """给面板/日志：这个版本号在种子表里登记过的键（只看 versions，不看镜像；未登记 ⇒ None，**不影响放行**）。"""
    return seed_key_for(version, None)


class RvaTable(dict):
    """RVA 名 → 值。**共享的单例字典**：`resolve` 成功后原地更新，所有 `from .build import RVA`/`B.RVA[...]`
    的调用方（`world/objects/proc`…）下一次读就是新值。缺项读 0（读 `base+0` 必然失败 ⇒ 上层报"GWorld 为空"，
    而不是 import 期 KeyError；真正的拒绝在 `resolve`/`Session.attach`）。"""

    def __missing__(self, key):
        return 0


def _initial_select(env=None, pids_fn=None, module_fn=None, version_fn=None, cache_loader=None) -> dict:
    """import 期选一份**起始**RVA（只读文件 + 读一次进程内存里的版本串，约 3 s，按 (pid, 创建时间) 缓存）。

    只用来让 `names.py`/`ops.inject` 这类 import 期取常量的模块拿到"大概率对"的值；**真正的放行在 `resolve()`**
    （attach / `precheck.build_check` 时对进程复验，必要时扫描）。顺序：
        `KARDS_BUILD` 显式覆盖 > 运行中游戏的 缓存（版本+镜像大小命中）> 种子（镜像大小/版本命中）> 占位种子。
    → `{"key","label","source","rva","version","image_size"}`。"""
    env = os.environ if env is None else env
    k = env.get("KARDS_BUILD")
    if k:
        if k not in BUILDS:
            raise SystemExit("KARDS_BUILD=%r 不在种子表里（可选：%s）" % (k, ", ".join(BUILDS) or "（种子表为空）"))
        return {"key": k, "label": "env", "source": "env", "rva": dict(RVA_BY_BUILD.get(k, {})),
                "version": BUILDS[k].get("version"), "image_size": BUILDS[k].get("image_size")}
    default = DEFAULT_BUILD if DEFAULT_BUILD in BUILDS else (next(iter(BUILDS), None))
    out = {"key": default, "label": "default", "source": "default",
           "rva": dict(RVA_BY_BUILD.get(default, {})) if default else {},
           "version": None, "image_size": BUILDS.get(default, {}).get("image_size") if default else None}
    try:
        from . import version as _V
        pids = (pids_fn or _V.game_pids)()
        if len(pids) != 1:
            return out
        mod = (module_fn or _V.game_module)(pids[0])
        ver = (version_fn or _V.version_of_pid)(pids[0])
        size = mod[1] if mod else None
        if size is None:
            return out
        sk = seed_key_for(ver, size)
        out.update(version=ver, image_size=size, label=("version:%s" % ver) if ver else "default")
        c = None
        if ver:
            from . import rvascan as _RS
            c = (cache_loader or _RS.load_cache)(ver, size)
        if c and all(x in c for x in CORE_RVA_KEYS):
            out.update(source="cache", rva=dict(c), key=sk or out["key"])
        elif sk:
            out.update(source="seed", rva=dict(RVA_BY_BUILD.get(sk, {})), key=sk)
    except Exception:                                          # noqa: BLE001
        pass
    return out


_sel = _initial_select()
CURRENT = _sel["key"]                       # 种子表里的"身份参照"键（import 期定；selftest/exes/cli 拿它当参照）
BUILD_SOURCE = _sel["label"]                # env / version:<版本> / default
RVA = RvaTable(_sel["rva"])                 # 当前生效的 RVA（共享单例，resolve 成功后原地更新）
RVA_SOURCE = _sel["source"]                 # env | cache | seed | scan | default
RVA_VERSION = _sel["version"]
RVA_IMAGE_SIZE = _sel["image_size"]         # 这套 RVA 对应的 SizeOfImage（board._attach 的核对用它）
RVA_VERIFIED = False                        # 是否已对**运行中的进程**复验/扫描过
RESOLVED: Optional["Resolution"] = None     # 最近一次成功的 resolve 结果
RESTART_NEEDED = False                      # 本进程里 RVA 变过、而 ops.inject 已加载（注入侧可能拿着旧地址）⇒ 需重启本进程
MODULE_NAME = GAME_EXE
BUILD_VERSION = (BUILDS.get(CURRENT) or {}).get("version")     # 参照种子的 `<版本号>.<渠道>`
RVA_SEED = {k: dict(v) for k, v in RVA_BY_BUILD.items()}      # 种子原值快照（selftest 的"未漂移"比对用）


# --------------------------------------------------------------------------
# 解析链：用户缓存 → 种子复验 → 运行时扫描（全部只读 ReadProcessMemory）
# --------------------------------------------------------------------------
class BuildResolveError(RuntimeError):
    """三步都没拿到可用的 RVA。`steps` 记录每一步为什么没成（人话）。"""

    def __init__(self, msg, steps=None):
        super().__init__(msg)
        self.steps = list(steps or [])


@dataclass
class Resolution:
    rva: dict
    source: str                         # env | cache | seed | scan
    key: Optional[str] = None           # 对应的种子键（cache/scan 可能没有）
    version: Optional[str] = None
    image_size: Optional[int] = None
    steps: list = field(default_factory=list)      # [(步骤, 通过?, 说明)]
    seconds: float = 0.0

    def describe(self) -> str:
        return "RVA 来源=%s 版本=%s 镜像=0x%X%s 用时=%.1fs" % (
            self.source, self.version or "?", self.image_size or 0,
            (" 种子=%s" % self.key) if self.key else "", self.seconds)


def _core_ok(d) -> bool:
    return bool(d) and all(isinstance(d.get(k), int) and d.get(k) for k in CORE_RVA_KEYS)


def resolve(m, base: int, image_size: int, version: Optional[str], *, log=None, cache_dir: Optional[str] = None,
            scan: bool = True, env=None, builds: Optional[dict] = None, rvas: Optional[dict] = None,
            verify=None, scanner=None) -> Resolution:
    """给定一个**只读内存句柄** `m`（`MemRO`）、模块基址、SizeOfImage、游戏自报版本串，解出 RVA。

        0. `KARDS_BUILD`（调试覆盖）⇒ 直接用该键种子值，不复验（`source="env"`）
        1. 用户缓存（`rvascan.CACHE_DIR/<版本>.json`，键=版本+镜像大小）→ `quick_verify` 过 ⇒ `source="cache"`
        2. 种子表：**镜像大小相同**的条目（`versions` 命中的优先）→ `quick_verify` 过 ⇒ `source="seed"`
        3. `rvascan.resolve` 扫描（约几秒~十几秒；有版本号就写回缓存）⇒ `source="scan"`
        全失败 ⇒ `BuildResolveError`（`steps` 说明每一步为什么没成；**不拿旧版本的表凑合**）。

    `verify(m, base, rva)->bool` / `scanner(...)->{"rva","source"}` 可注入（离线测试用，默认 `rvascan` 的实现）。"""
    from . import rvascan as RS
    log = log or (lambda s: None)
    env = os.environ if env is None else env
    builds = BUILDS if builds is None else builds
    rvas = RVA_BY_BUILD if rvas is None else rvas
    verify = verify or RS.quick_verify
    cdir = cache_dir or RS.CACHE_DIR
    t0 = time.time()
    steps = []

    def done(rva, source, key=None):
        return Resolution(rva=dict(rva), source=source, key=key, version=version, image_size=image_size,
                          steps=steps, seconds=time.time() - t0)

    ek = env.get("KARDS_BUILD")
    if ek:
        if ek not in builds:
            raise BuildResolveError("KARDS_BUILD=%r 不在种子表里（可选：%s）" % (ek, ", ".join(builds) or "空"),
                                    [("env", False, "键不在种子表")])
        steps.append(("env", True, "KARDS_BUILD=%s 调试覆盖（未复验）" % ek))
        return done(rvas.get(ek, {}), "env", ek)

    ident = seed_key_for(version, image_size, builds)

    # 1. 用户缓存
    if not version:
        steps.append(("cache", False, "游戏版本串认不出，没有缓存键"))
    else:
        c = RS.load_cache(version, image_size, cdir)
        if not c:
            steps.append(("cache", False, "无缓存（%s）" % RS.cache_path(version, cdir)))
        elif not _core_ok(c):
            steps.append(("cache", False, "缓存缺项（需要 %s）" % "/".join(CORE_RVA_KEYS)))
        elif verify(m, base, c):
            steps.append(("cache", True, "缓存命中，复验通过"))
            return done(c, "cache", ident)
        else:
            steps.append(("cache", False, "缓存复验失败（内存里对不上）"))

    # 2. 种子表复验
    cands = []
    if image_size is not None:
        cands = [k for k in builds if builds[k].get("image_size") == image_size]
        cands.sort(key=lambda k: 0 if version in (builds[k].get("versions") or []) else 1)
    if not cands:
        steps.append(("seed", False, "种子表里没有镜像大小 0x%X 的条目" % (image_size or 0)))
    else:
        why = []
        for k in cands:
            sv = rvas.get(k) or {}
            if not _core_ok(sv):
                why.append("%s 缺项" % k)
            elif verify(m, base, sv):
                steps.append(("seed", True, "种子 %s 复验通过" % k))
                return done(sv, "seed", k)
            else:
                why.append("%s 复验失败" % k)
        steps.append(("seed", False, "；".join(why)))

    # 3. 扫描
    if not scan:
        steps.append(("scan", False, "已禁用扫描"))
    else:
        try:
            log("缓存/种子都不可用，开始运行时扫描 RVA（只读，约几秒~十几秒）…")
            if scanner is not None:
                r = scanner(m, base, image_size, version, log, cdir)
            else:
                r = RS.resolve(m, base, image_size, version, log=log, cache_dir=cdir, force=True)
            rva = r["rva"]
            if not _core_ok(rva):
                raise RS.ScanError("扫描结果缺项：%s" % sorted(rva))
            steps.append(("scan", True, "扫描成功%s" % ("，已写缓存" if version else "（无版本串，不缓存）")))
            return done(rva, "scan", ident)
        except Exception as e:                                  # noqa: BLE001
            steps.append(("scan", False, "%s: %s" % (type(e).__name__, e)))

    raise BuildResolveError(
        "无法确定 RVA（版本 %s，镜像 0x%X）：%s" % (version or "认不出", image_size or 0,
                                                "；".join("%s：%s" % (s, d) for s, _ok, d in steps)), steps)


def apply(res: Resolution) -> bool:
    """把一次成功的解析结果装进共享的 `RVA`（**原地**更新）。→ 是否与之前生效的核心项不同。

    ★ P7-S3b（2026-10-03）：`kardsmem.names` 的 RVA 已改成**调用期**读（模块 `__getattr__` +
    `FNamePool.__init__` 的 None 哨兵），`ops.consts.render_js()` 也按调用期渲染 ⇒
    这里**不再需要**去改别人模块的属性/默认实参（那种补丁改不全，第三处引用就漏了）。

    `RESTART_NEEDED` 仍是**保守**告警，判据是"`ops.inject` 已加载"（在常驻监听器里几乎恒真），
    **不是**"确实 attach 过" —— 我们没法便宜地分辨"脚本已注入"和"只是 import 了门面"，宁可多报。
    语义：本进程里 RVA 变过 ⇒ 依赖注入侧的动作请先重启监听器再信（frida 脚本在 attach 那一刻
    按当时 RVA 渲染并注入，之后改 RVA 不会重发；与"import 期快照"已无关）。"""
    global RVA_SOURCE, RVA_VERSION, RVA_IMAGE_SIZE, RVA_VERIFIED, RESOLVED, RESTART_NEEDED
    import sys
    old_core = {k: RVA.get(k) for k in CORE_RVA_KEYS}
    new = dict(res.rva)
    changed = any(new.get(k) != old_core.get(k) for k in CORE_RVA_KEYS)
    for k in list(RVA):
        if k not in new:
            del RVA[k]
    RVA.update(new)
    RVA_SOURCE, RVA_VERSION, RVA_IMAGE_SIZE = res.source, res.version, res.image_size
    RVA_VERIFIED, RESOLVED = res.source != "env", res
    if changed:
        if "ops.inject" in sys.modules:
            RESTART_NEEDED = True
    return changed


_MEMO: dict = {"ok": {}, "fail": {}}
FAIL_TTL = 60.0                            # 失败结果记多久（防止轮询的调用方每次都重扫几十秒）


_UNSET = object()


def ensure_resolved(m=None, pid: Optional[int] = None, version=_UNSET, *, base: Optional[int] = None,
                    image_size: Optional[int] = None, refresh: bool = False, log=None, **kw) -> Resolution:
    """对**运行中的游戏**走一遍解析链并 `apply`；同一个 (pid, 基址, 镜像, 版本) 只做一次（`refresh=True` 重来）。

    `m`：已打开的 `MemRO`（缺省自己开、用完关）。失败抛 `BuildResolveError`。只读。"""
    from . import version as _V
    if os.environ.get("KARDS_BUILD"):
        return _env_resolution()
    if pid is None:
        pids = _V.game_pids()
        if len(pids) != 1:
            raise BuildResolveError("没有游戏进程" if not pids else "同时有多个游戏进程：%s（不猜）" % pids,
                                    [("process", False, "pid 不唯一")])
        pid = pids[0]
    if base is None or image_size is None:
        mod = _V.game_module(pid)
        if not mod:
            raise BuildResolveError("在 pid %d 里找不到模块 %s" % (pid, GAME_EXE), [("module", False, "找不到主模块")])
        base, size, _path = mod
    else:
        size = image_size
    ver = version if version is not _UNSET else _V.version_of_pid(pid, use_cache=not refresh)
    key = (pid, base, size, ver)
    if not refresh:
        if key in _MEMO["ok"]:
            return _MEMO["ok"][key]
        f = _MEMO["fail"].get(key)
        if f and time.time() - f[0] < FAIL_TTL:
            raise f[1]
    own = m is None
    if own:
        from .proc import MemRO
        m = MemRO(pid)
    try:
        res = resolve(m, base, size, ver, log=log, **kw)
    except BuildResolveError as e:
        _MEMO["fail"][key] = (time.time(), e)
        raise
    finally:
        if own:
            m.close()
    _MEMO["fail"].pop(key, None)
    apply(res)
    _MEMO["ok"][key] = res
    return res


def _env_resolution() -> Resolution:
    k = os.environ.get("KARDS_BUILD")
    if k not in BUILDS:
        raise BuildResolveError("KARDS_BUILD=%r 不在种子表里" % k, [("env", False, "键不在种子表")])
    res = Resolution(rva=dict(RVA_BY_BUILD.get(k, {})), source="env", key=k, version=BUILDS[k].get("version"),
                     image_size=BUILDS[k].get("image_size"), steps=[("env", True, "KARDS_BUILD=%s 调试覆盖（未复验）" % k)])
    apply(res)
    return res


def status() -> dict:
    """面板/CLI 显示用：当前生效的 RVA 从哪来、有没有对进程复验过。"""
    return {"key": CURRENT, "label": BUILD_SOURCE, "source": RVA_SOURCE, "verified": RVA_VERIFIED,
            "version": RVA_VERSION, "image_size": RVA_IMAGE_SIZE, "restart_needed": RESTART_NEEDED,
            "steps": [list(s) for s in (RESOLVED.steps if RESOLVED else [])]}


PROCESS_EVENT_IDX = 0x4C                  # vtable 里 ProcessEvent 的下标

# 原生取值口（用来核对内存读数，本工具链不调用它们）
# ⚠ 这一组**只对 Steam 构建**采过证；换构建要重新采（它们不在 RVA_BY_BUILD 里）。
NATIVE_RVA = {
    "getKreditBySide": 0x4A65030,
    "getKreditSlotBySide": 0x4A65110,
    "getMaxPossibleKredits": 0x4A651F0,
    "getEncryptionKey": 0x4A64330,
    "CanBeTargetted": 0x4A7F910,
    "CanOtherCardBeTargetted": 0x4A7FC10,
    "IsValidHandTarget": 0x4A83370,
    "CanPlayFromHand": 0x4A7FEC0,
    "HasAttackLeft": 0x4A81760,
    "HasMovementLeft": 0x4A82090,
    "CanMoveAndAttackInTheSameTurn": 0x4A7FB70,
    "GetStaticCard": 0x4A8B220,
    "GetAllStaticCards": 0x4A89C90,
    "GetStaticCardNames": 0x4A8B2B0,
}

# 旧 build 对照（只在核对 IDA 里的地址时用，**不要**拿它读当前进程）
RVA_OLD = {
    "GWorld": 0x08F575B0,
    "GObjects": 0x091F4060,
    "GNames_decoy": 0x090D79A8,
    "UObject_ProcessEvent": 0x0159C9F0,
    "getTotalOperationCost_sub": 0x144B10FA0,
}


# --------------------------------------------------------------------------
# UObject / 类身份判据
# --------------------------------------------------------------------------
OFF_UOBJECT_FLAGS = 0x08
OFF_UOBJECT_CLASS = 0x10
OFF_UOBJECT_NAME = 0x18          # FName {i32 comparisonIndex, i32 number}
OFF_UCLASS_SUPER = 0x40
OFF_UCLASS_PROPSIZE = 0x58
FLAG_RF_CDO = 0x10               # obj+0x08 & 0x10 -> ClassDefaultObject

OFF_UWORLD_GAMESTATE = 0x1B0
OFF_UWORLD_PERSISTENT_LEVEL = 0x30
OFF_UWORLD_GAME_INSTANCE = 0x228
OFF_LEVEL_ACTORS = 0xA0
OFF_GI_LOCALPLAYERS = 0x38
OFF_LOCALPLAYER_PC = 0x30

SIZE_UWORLD = 2536
SIZE_AKARDS_GAMESTATE = 912
SIZE_BP_GAMESTATE_BATTLE = 1960
SIZE_BASECARD_OBJECT = 1656
SIZE_BASECARD_ACTOR_CANDIDATES = (0x0F68, 0x0F70)   # ABP_Board_C
SIZE_BASECARD_ACTOR = 0x830                          # ABP_BaseCard_C


@dataclass
class BuildInfo:
    """一次 attach 的构建身份。`ok` 为 False 时**不要**信任何偏移读数。"""
    pid: int = 0
    base: int = 0
    image_size: Optional[int] = None
    module_path: Optional[str] = None
    md5: Optional[str] = None
    file_size: Optional[int] = None
    matched: Optional[str] = None      # BUILDS 里的 key
    ok: bool = False
    md5_match: bool = False            # 字节是否与偏移表目标完全一致（**信息性，不作否决**）
    checks: list = field(default_factory=list)
    version: Optional[str] = None      # 运行中游戏自报的 `版本号.分支`（attach 时由 version.py 读；缓存键/日志，**不是**放行判据）
    rva_source: Optional[str] = None   # 放行判据：RVA 从哪来 env|cache|seed|scan（`ensure_resolved` 成功才 ok）

    def describe(self) -> str:
        return ("pid=%s base=0x%X version=%s image=0x%X md5=%s%s matched=%s ok=%s"
                % (self.pid, self.base, self.version or "?", self.image_size or 0, self.md5 or "?",
                   "" if (self.md5_match or self.md5 is None) else "(≠表内)",
                   self.matched or "-", self.ok))

    def as_dict(self) -> dict:
        return {"pid": self.pid, "base": self.base, "image_size": self.image_size,
                "md5": self.md5, "file_size": self.file_size, "module_path": self.module_path,
                "matched": self.matched, "ok": self.ok, "md5_match": self.md5_match,
                "version": self.version, "rva_source": self.rva_source,
                "checks": list(self.checks)}


def md5_file(path: str, chunk: int = 1 << 20) -> Optional[str]:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(chunk), b""):
                h.update(c)
        return h.hexdigest()
    except OSError:
        return None


def identify(image_size: Optional[int], md5: Optional[str],
             file_size: Optional[int] = None) -> Optional[str]:
    """反查是 BUILDS 里的哪一个。

    ★ **先比 md5（精确到副本），再比 SizeOfImage（定位到构建）**：
    两份同构建的副本 md5 可以不同，这时只能落到"构建"那一层 —— 这已经够用，
    因为**偏移表是按构建给的**。
    """
    if md5:
        for key, b in BUILDS.items():
            if b.get("md5") == md5:
                return key
    if image_size is not None:
        for key, b in BUILDS.items():
            if b.get("image_size") == image_size:
                return key
    return None


def validate(image_size: Optional[int], md5: Optional[str],
             file_size: Optional[int] = None) -> BuildInfo:
    """纯函数版身份核对（不碰进程）：这个 exe 指纹与**参照种子 `CURRENT`** 是不是同一个构建。

    ★ P7 之后它**不再是 attach 的放行条件**（放行看 `ensure_resolved` 的复验/扫描）；留着给 selftest 与
    `exes`/`verify` 这类"想知道它像哪份种子"的诊断用。SizeOfImage 相同 ⇒ 种子 RVA 才可能有效；
    md5 只记录"这份副本的字节有没有被动过"，**只信息、不否决**。
    """
    info = BuildInfo(image_size=image_size, md5=md5, file_size=file_size)
    want = BUILDS[CURRENT]
    ok_img = (image_size == want["image_size"])
    ok_md5 = (md5 == want["md5"])
    info.md5_match = ok_md5
    info.matched = identify(image_size, md5, file_size)
    info.checks.append(("image_size", image_size, want["image_size"], ok_img))
    info.checks.append(("md5", md5, want["md5"], ok_md5))
    if file_size is not None:
        info.checks.append(("exe_size", file_size, want["exe_size"],
                            file_size == want["exe_size"]))
    if not ok_img:
        if info.matched and info.matched != CURRENT:
            info.checks.append(("warning", "attach 到的是 %s，偏移表不适用" % info.matched,
                                CURRENT, False))
        return info
    info.ok = True
    if ok_md5:
        info.checks.append(("bytes", "与偏移表目标字节一致", "一致", True))
    elif md5 is None:
        info.checks.append(("bytes", "md5 未校验（调用方跳过）", "跳过", True))
    else:
        info.checks.append(("bytes", "这份副本的字节与偏移表目标不同（本地改动过）",
                            "SizeOfImage 相同 ⇒ RVA 仍有效", True))
    return info


def spec_consistency() -> list:
    """把代码里的常量与 `reports/kards-offsets.json` 对一遍，返回不一致列表。

    规格里的偏移记录照 Dumper-7 的 `OffsetsInfo.json` 格式：
    `build.data` 是 `[[名字, 值], ...]`。所以"防漂移"就是逐条比这张表。

    ⚠ 规格 JSON 里登记的只有 **Steam 那份**（`current`）。用
    `KARDS_BUILD=launcher_default` 跑时不能拿它判漂移 —— 那些值本来就该不同。
    """
    out = []
    if CURRENT != "current" or "current" not in RVA_SEED:
        return out
    if not SPEC_JSON.exists():
        return [("spec_missing", str(SPEC_JSON), "存在", False)]
    spec = json.loads(SPEC_JSON.read_text(encoding="utf-8"))

    data = (spec.get("build") or {}).get("data")
    if not isinstance(data, list):
        return [("build.data", None, "[[名字, 值], ...]（照 Dumper-7 OffsetsInfo.json）", False)]
    got = {}
    for row in data:
        if isinstance(row, list) and len(row) == 2:
            got[str(row[0])] = row[1]

    cur = BUILDS[CURRENT]
    seed = RVA_SEED[CURRENT]            # 比的是**种子原值**（运行时 RVA 可能来自缓存/扫描）
    want = {
        "VERSION": BUILD_VERSION,
        "SIZE_OF_IMAGE": cur["image_size"],
        "EXE_SIZE": cur["exe_size"],
        "MD5": cur["md5"],
        "OFFSET_GWORLD": seed["GWorld"],
        "OFFSET_GOBJECTS": seed["GObjects"],
        "OFFSET_GNAMES": seed["GNames_decoy"],
        "OFFSET_FNAMEPOOL": seed["FNamePool"],
        "OFFSET_APPENDSTRING": seed["FName_AppendString"],
        "OFFSET_PROCESSEVENT": seed["UObject_ProcessEvent"],
        "INDEX_PROCESSEVENT": PROCESS_EVENT_IDX,
        "OLD_OFFSET_GWORLD": RVA_OLD["GWorld"],
        "OLD_OFFSET_GOBJECTS": RVA_OLD["GObjects"],
        "OLD_OFFSET_GNAMES": RVA_OLD["GNames_decoy"],
        "OLD_OFFSET_PROCESSEVENT": RVA_OLD["UObject_ProcessEvent"],
    }
    for name, w in want.items():
        if name in got:
            g = _as_int(got[name])
            if g != w:
                out.append(("%s" % name, g, w, False))
        else:
            out.append(("%s 缺失" % name, None, w, False))
    return out

def _as_int(v):
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return v
    return v
