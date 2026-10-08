#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.build 的 RVA 解析链（P7）离线测试：用户缓存 → 种子复验 → 运行时扫描 → 明确报错；`KARDS_BUILD` 调试覆盖。

全程不碰真进程：假内存回调（稀疏字节表）+ 注入的复验/扫描函数；缓存写到临时目录。
真进程扫描（rvascan 需要运行中的游戏）默认 SKIP；`KARDS_TEST_LIVE=1` 且游戏在跑时才做（只读）。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kardsmem import build as B                                   # noqa: E402
from kardsmem import rvascan as RS                                # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


BASE = 0x7FF700000000
SIZE_A, SIZE_B, SIZE_NEW = 0x9CC4000, 0x9CC8000, 0x9DD0000
MAGIC = b"WRLD"


def rva_set(n):
    """一套"核心四项"RVA（n 区分不同套）。"""
    return {"GObjects": 0x1000 * n, "FNamePool": 0x2000 * n, "GWorld": 0x3000 * n, "UObject_ProcessEvent": 0x4000 * n}


SEED_A, SEED_B = rva_set(1), rva_set(2)
BUILDS = {"a": {"image_size": SIZE_A, "versions": ["1.60.1.launcher", "1.58.1.launcher"]},
          "b": {"image_size": SIZE_B, "versions": ["1.60.1.Steam"]}}
RVAS = {"a": dict(SEED_A), "b": dict(SEED_B)}


class FakeMem:
    """假读内存回调：`planted` = 在这些 (base+GWorld) 处放 MAGIC，复验就认。记录读了哪里（证明复验真的读了内存）。"""

    def __init__(self, planted):
        self.planted = {BASE + r["GWorld"] for r in planted}
        self.reads = []

    def read(self, addr, n):
        self.reads.append(addr)
        return MAGIC if addr in self.planted and n <= 4 else None


class Counter:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, *a, **k):
        self.calls.append(a)
        return self.fn(*a, **k)


def make_verify(mem):
    """复验 = 读内存里 GWorld 槽，看到约定的魔数（假 exe/假进程的"形状"判据）。"""
    return Counter(lambda m, base, rva: m.read(base + rva["GWorld"], 4) == MAGIC)


def scanner_for(rva, fail=None):
    def _s(m, base, size, ver, log, cdir):
        if fail:
            raise RS.ScanError(fail)
        if ver:
            RS.save_cache(ver, size, rva, cdir)
        return {"rva": dict(rva), "source": "scan"}
    return Counter(_s)


def snapshot():
    return (dict(B.RVA), B.RVA_SOURCE, B.RVA_VERSION, B.RVA_IMAGE_SIZE, B.RVA_VERIFIED, B.RESOLVED, B.RESTART_NEEDED)


def restore(s):
    B.RVA.clear()
    B.RVA.update(s[0])
    B.RVA_SOURCE, B.RVA_VERSION, B.RVA_IMAGE_SIZE, B.RVA_VERIFIED, B.RESOLVED, B.RESTART_NEEDED = s[1:]
    B._MEMO["ok"].clear()
    B._MEMO["fail"].clear()


def run(m, ver, size, verify, scanner=None, cdir=None, env=None, **kw):
    return B.resolve(m, BASE, size, ver, cache_dir=cdir, env=env if env is not None else {}, builds=BUILDS, rvas=RVAS,
                     verify=verify, scanner=scanner, **kw)


def main():
    saved = snapshot()
    try:
        with tempfile.TemporaryDirectory() as d:
            # ---- 1. 缓存命中 → 复验通过 ----
            cache_rva = rva_set(7)
            RS.save_cache("1.60.1.launcher", SIZE_A, cache_rva, d)
            mem = FakeMem([cache_rva, SEED_A])
            v, sc = make_verify(mem), scanner_for(rva_set(9))
            r = run(mem, "1.60.1.launcher", SIZE_A, v, sc, d)
            chk("缓存命中：source=cache", r.source == "cache" and r.rva == cache_rva, str(r.steps))
            chk("缓存命中：复验读了内存、没碰种子/扫描", len(v.calls) == 1 and not sc.calls and mem.reads == [BASE + cache_rva["GWorld"]])

            # ---- 2. 缓存缺 → 种子复验通过 ----
            with tempfile.TemporaryDirectory() as d2:
                mem = FakeMem([SEED_A])
                v, sc = make_verify(mem), scanner_for(rva_set(9))
                r = run(mem, "1.60.1.launcher", SIZE_A, v, sc, d2)
                chk("缓存缺 → 种子复验：source=seed、key=a", r.source == "seed" and r.key == "a" and r.rva == SEED_A, str(r.steps))
                chk("缓存缺 → 种子复验：steps 先记 cache 失败再记 seed 通过",
                    [(s, ok) for s, ok, _ in r.steps] == [("cache", False), ("seed", True)], str(r.steps))
                chk("缓存缺 → 没有扫描", not sc.calls)

            # ---- 3. 缓存复验失败（内存里对不上）→ 退到种子 ----
            with tempfile.TemporaryDirectory() as d2:
                RS.save_cache("1.60.1.launcher", SIZE_A, rva_set(7), d2)
                mem = FakeMem([SEED_A])                         # 缓存那套没放魔数 ⇒ 复验失败；种子那套放了
                r = run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), scanner_for(rva_set(9)), d2)
                chk("缓存复验失败 → 种子", r.source == "seed" and "复验失败" in r.steps[0][2], str(r.steps))

            # ---- 4. 种子复验失败 → 扫描（并写缓存，下一次直接 cache）----
            with tempfile.TemporaryDirectory() as d2:
                new_rva = rva_set(5)
                mem = FakeMem([new_rva])                        # 种子那套对不上（内存里只有新值）
                sc = scanner_for(new_rva)
                r = run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), sc, d2)
                chk("种子复验失败 → 扫描：source=scan", r.source == "scan" and r.rva == new_rva, str(r.steps))
                chk("扫描：steps cache/seed 失败、scan 通过",
                    [(s, ok) for s, ok, _ in r.steps] == [("cache", False), ("seed", False), ("scan", True)])
                r2 = run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), scanner_for(rva_set(9)), d2)
                chk("扫描写了缓存 ⇒ 第二次 source=cache", r2.source == "cache" and r2.rva == new_rva)

            # ---- 5. 未登记的版本 + 未知镜像大小：直接扫描（不被“未登记”拦住）----
            with tempfile.TemporaryDirectory() as d2:
                new_rva = rva_set(6)
                mem = FakeMem([new_rva])
                r = run(mem, "9.9.9.newbranch", SIZE_NEW, make_verify(mem), scanner_for(new_rva), d2)
                chk("未登记版本/镜像：走扫描成功", r.source == "scan" and r.key is None and r.version == "9.9.9.newbranch", str(r.steps))
                chk("种子步骤说明没有该镜像大小", any(s == "seed" and "没有镜像大小" in dd for s, _, dd in r.steps))
                chk("缓存按 版本+镜像大小 写入", RS.load_cache("9.9.9.newbranch", SIZE_NEW, d2) == new_rva)

            # ---- 5b. 同版本号但镜像大小不同的种子不能用（镜像大小是 RVA 有效性的键）----
            with tempfile.TemporaryDirectory() as d2:
                mem = FakeMem([SEED_A])
                v = make_verify(mem)
                r = run(mem, "1.60.1.launcher", SIZE_NEW, v, scanner_for(rva_set(6)), d2)
                chk("版本命中但镜像大小不符 ⇒ 不拿种子凑合，直接扫描", r.source == "scan" and v.calls == [] and r.key is None)

            # ---- 5c. 版本串认不出：没有缓存键，种子(镜像大小)复验照样可用 ----
            with tempfile.TemporaryDirectory() as d2:
                mem = FakeMem([SEED_B])
                r = run(mem, None, SIZE_B, make_verify(mem), scanner_for(rva_set(9)), d2)
                chk("版本串为 None：种子复验通过", r.source == "seed" and r.key == "b", str(r.steps))
                mem = FakeMem([])
                sc = scanner_for(rva_set(4))
                r = run(mem, None, SIZE_NEW, make_verify(mem), sc, d2)
                chk("版本串为 None + 种子不符：扫描且不写缓存", r.source == "scan" and os.listdir(d2) == [], str(os.listdir(d2)))

            # ---- 6. 全部失败 → 明确错误 ----
            with tempfile.TemporaryDirectory() as d2:
                mem = FakeMem([])
                try:
                    run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), scanner_for(None, fail="FNamePool 候选 0 个（期望 1）"), d2)
                    chk("全部失败 ⇒ 抛 BuildResolveError", False)
                except B.BuildResolveError as e:
                    msg = str(e)
                    chk("全部失败 ⇒ 抛 BuildResolveError，消息说明三步各自为什么没成",
                        all(w in msg for w in ("cache", "seed", "scan", "FNamePool 候选 0 个")), msg)
                    chk("错误带 steps（三步都失败）", [(s, ok) for s, ok, _ in e.steps] == [("cache", False), ("seed", False), ("scan", False)])
                # 禁用扫描
                try:
                    run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), scanner_for(rva_set(9)), d2, scan=False)
                    chk("scan=False 且缓存/种子都不成 ⇒ 抛错", False)
                except B.BuildResolveError as e:
                    chk("scan=False 且缓存/种子都不成 ⇒ 抛错", "已禁用扫描" in str(e))
                # 扫描结果缺项也算失败
                try:
                    run(mem, "1.60.1.launcher", SIZE_A, make_verify(mem), Counter(lambda *a: {"rva": {"GObjects": 1}, "source": "scan"}), d2)
                    chk("扫描缺项 ⇒ 抛错", False)
                except B.BuildResolveError as e:
                    chk("扫描缺项 ⇒ 抛错", "缺项" in str(e), str(e))

            # ---- 7. KARDS_BUILD 调试覆盖：不复验、不扫描；未知键报错 ----
            mem = FakeMem([])
            v, sc = make_verify(mem), scanner_for(rva_set(9))
            r = run(mem, "whatever", SIZE_NEW, v, sc, None, env={"KARDS_BUILD": "b"})
            chk("KARDS_BUILD=b ⇒ source=env、用 b 的种子值、没复验没扫描",
                r.source == "env" and r.key == "b" and r.rva == SEED_B and not v.calls and not sc.calls)
            try:
                run(mem, "x", SIZE_A, v, sc, None, env={"KARDS_BUILD": "nope"})
                chk("KARDS_BUILD=未知键 ⇒ 抛错", False)
            except B.BuildResolveError as e:
                chk("KARDS_BUILD=未知键 ⇒ 抛错", "nope" in str(e))

            # ---- 8. 真正的 rvascan.resolve 路径（scanner=None）：monkeypatch scan_process，验证写缓存 + force 跳过缓存 ----
            with tempfile.TemporaryDirectory() as d2:
                old_scan = RS.scan_process
                try:
                    RS.scan_process = lambda m, base, size, log=lambda s: None: dict(rva_set(8))
                    mem = FakeMem([rva_set(8)])
                    r = run(mem, "2.0.0.x", SIZE_NEW, make_verify(mem), None, d2)
                    chk("rvascan.resolve 默认路径：scan + 写缓存", r.source == "scan" and RS.load_cache("2.0.0.x", SIZE_NEW, d2) == rva_set(8))
                finally:
                    RS.scan_process = old_scan

        # ---- 9. apply：原地更新共享的 RVA 字典；多余的旧键被清掉 ----
        ident = B.RVA
        res = B.Resolution(rva=rva_set(3), source="scan", key=None, version="3.0.0.z", image_size=SIZE_NEW)
        changed = B.apply(res)
        chk("apply：返回“核心项变了”", changed is True)
        chk("apply：RVA 是同一个字典对象（原地更新，已有引用跟着变）", B.RVA is ident and B.RVA["GWorld"] == rva_set(3)["GWorld"])
        chk("apply：旧种子里的 GNames_decoy 等多余键被清（不留旧构建的值）", "GNames_decoy" not in B.RVA.keys() and B.RVA["GNames_decoy"] == 0)
        chk("apply：来源/版本/镜像大小/已复验 同步", (B.RVA_SOURCE, B.RVA_VERSION, B.RVA_IMAGE_SIZE, B.RVA_VERIFIED) == ("scan", "3.0.0.z", SIZE_NEW, True))
        chk("apply：同值再 apply ⇒ changed=False", B.apply(res) is False)

        # ---- 10. ensure_resolved：备忘（同进程只解析一次）+ 失败备忘（不反复扫） ----
        with tempfile.TemporaryDirectory() as d2:
            mem = FakeMem([rva_set(5)])
            v, sc = make_verify(mem), scanner_for(rva_set(5))
            kw = dict(m=mem, pid=4242, version="5.5.5.q", base=BASE, image_size=SIZE_NEW, cache_dir=d2, verify=v, scanner=sc,
                      builds=BUILDS, rvas=RVAS, env={})
            old_env = os.environ.pop("KARDS_BUILD", None)
            try:
                r1 = B.ensure_resolved(**kw)
                r2 = B.ensure_resolved(**kw)
                chk("ensure_resolved：成功后备忘（第二次不再复验/扫描）", r1 is r2 and len(sc.calls) == 1, str(len(sc.calls)))
                chk("ensure_resolved：成功后 apply 到 B.RVA", B.RVA["FNamePool"] == rva_set(5)["FNamePool"] and B.RESOLVED is r1)
                st = B.status()
                chk("status()：source/version/verified", (st["source"], st["version"], st["verified"]) == ("scan", "5.5.5.q", True), str(st))
                bad = dict(kw, pid=4343, version="6.6.6.q", scanner=scanner_for(None, fail="boom"), verify=make_verify(FakeMem([])))
                n = 0
                for _ in range(2):
                    try:
                        B.ensure_resolved(**bad)
                    except B.BuildResolveError:
                        n += 1
                chk("ensure_resolved：失败抛错，且失败有 TTL 备忘（第二次不重扫）", n == 2 and len(bad["scanner"].calls) == 1, str(len(bad["scanner"].calls)))
            finally:
                if old_env is not None:
                    os.environ["KARDS_BUILD"] = old_env

        # ---- 11. 起始选择 _initial_select（import 期；全部注入，不碰真进程）----
        with tempfile.TemporaryDirectory() as d2:
            seeded = B.BUILDS and next(iter(B.BUILDS))
            ref = B.BUILDS[seeded]
            sel = B._initial_select(env={}, pids_fn=lambda: [], )
            chk("起始选择：没有游戏 ⇒ 占位种子 default", sel["source"] == "default" and sel["key"] in B.BUILDS)
            sel = B._initial_select(env={"KARDS_BUILD": seeded}, pids_fn=lambda: [1])
            chk("起始选择：KARDS_BUILD ⇒ env + 该键种子值", sel["source"] == "env" and sel["rva"] == B.RVA_BY_BUILD[seeded])
            try:
                B._initial_select(env={"KARDS_BUILD": "nope"})
                chk("起始选择：未知 KARDS_BUILD ⇒ SystemExit", False)
            except SystemExit:
                chk("起始选择：未知 KARDS_BUILD ⇒ SystemExit", True)
            mod = lambda pid: (BASE, ref["image_size"], "x.exe")
            sel = B._initial_select(env={}, pids_fn=lambda: [1], module_fn=mod, version_fn=lambda pid: ref["versions"][0],
                                    cache_loader=lambda v, s: None)
            chk("起始选择：镜像大小+版本命中种子 ⇒ source=seed", sel["source"] == "seed" and sel["key"] == seeded and sel["rva"] == B.RVA_BY_BUILD[seeded])
            cached = rva_set(11)
            sel = B._initial_select(env={}, pids_fn=lambda: [1], module_fn=mod, version_fn=lambda pid: ref["versions"][0],
                                    cache_loader=lambda v, s: dict(cached))
            chk("起始选择：缓存优先于种子 ⇒ source=cache", sel["source"] == "cache" and sel["rva"] == cached)
            sel = B._initial_select(env={}, pids_fn=lambda: [1], module_fn=lambda pid: (BASE, SIZE_NEW, "x"),
                                    version_fn=lambda pid: "8.8.8.new", cache_loader=lambda v, s: None)
            chk("起始选择：未登记版本、无缓存 ⇒ 退占位种子（default），attach 时再复验/扫描", sel["source"] == "default" and sel["version"] == "8.8.8.new")
            sel = B._initial_select(env={}, pids_fn=lambda: [1, 2])
            chk("起始选择：多个游戏进程 ⇒ 不猜（default）", sel["source"] == "default")

        # ---- 12. 纯函数 seed_key_for ----
        chk("seed_key_for：镜像+版本", B.seed_key_for("1.58.1.launcher", SIZE_A, BUILDS) == "a")
        chk("seed_key_for：镜像相同、版本没登记 ⇒ 仍按镜像（pak-only 更新）", B.seed_key_for("1.99.0.launcher", SIZE_A, BUILDS) == "a")
        chk("seed_key_for：镜像不符 ⇒ None（不猜）", B.seed_key_for("1.60.1.launcher", SIZE_NEW, BUILDS) is None)
        chk("seed_key_for：只给版本(显示用)", B.seed_key_for("1.60.1.Steam", None, BUILDS) == "b" and B.seed_key_for("nope", None, BUILDS) is None)

        # ---- 13. 随包种子表本身 ----
        j = json.load(open(B.TABLES_JSON, encoding="utf-8"))
        chk("build_tables.json：每个构建身份+核心 RVA 齐全", all(
            all(t.get(k) for k in ("image_size", "exe_size", "md5", "versions")) and all(t["rva"].get(k) for k in B.CORE_RVA_KEYS)
            for t in j["builds"].values()))
        chk("BUILDS/RVA_BY_BUILD 与 JSON 一致（单一来源）", set(B.BUILDS) == set(j["builds"]) == set(B.RVA_BY_BUILD)
            and all(B.RVA_BY_BUILD[k] == {a: int(b) for a, b in t["rva"].items()} for k, t in j["builds"].items()))
        chk("没有残留的第二份表/登记制", not hasattr(B, "RVA_FALLBACK") and not hasattr(B, "VERSION_TO_BUILD"))
        import kardsmem.board as BA
        import kardsmem.version as V
        chk("board 没有 _BUILD_TABLE/_select_build_key；version 没有 VERSION_TO_BUILD/select_build",
            not any(hasattr(BA, n) for n in ("_BUILD_TABLE", "_select_build_key", "_BUILD_KEY", "OFFSET_TABLES"))
            and not any(hasattr(V, n) for n in ("VERSION_TO_BUILD", "select_build", "build_key_for_version")))

        # ---- 14. 真进程（默认 SKIP）----
        from kardsmem import version as VV
        if os.environ.get("KARDS_TEST_LIVE") != "1":
            print("  [SKIP] 真进程复验/扫描（设 KARDS_TEST_LIVE=1 且游戏在跑；只读）")
        elif len(VV.game_pids()) != 1:
            print("  [SKIP] 需要恰好一个运行中的游戏进程")
        else:
            res = B.ensure_resolved(refresh=True)
            chk("真进程：解析链成功（source=%s）" % res.source, res.source in ("cache", "seed", "scan"), res.describe())
    finally:
        restore(saved)

    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
