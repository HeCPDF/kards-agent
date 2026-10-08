#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/reconcile_batch.py`（P5 影子对账的离线批量版）的离线测试——不需要游戏、不需要真转储：

① 合成一个最小 minidump（模块表 + Memory64List），验 `module_base` / `DumpMem.read`（区段内、跨区段拼接、空洞=None）；
② `offline_guard`：运行期内 `agent.precheck.call_read` 一调就抛、退出后还原；
③ 聚合/分类：`run_batch` 用假 opener/假对账喂 same/diff_ab/diff_bc/skipped，检查按牌聚合、结论优先级、原因归一、
   出错的转储记 errors 而不是整批失败；
④ `reconcile_state` 端到端（假 km、假 VM、真 `RuleV2._search_sim` + 真 `_shadow_check`）：
   **不带目标的指令**也进了对账（rule 里 `pair_eff` 只给带目标的牌，这是批量工具补的口子），
   直跑漏做 ⇒ diff_ab，直跑做对 ⇒ same（两个会得出不同结论的输入）。
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _hookfake import ME, OPP, K, card                              # noqa: E402,F401
from tools import reconcile_batch as RB                             # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else extra))
    if not ok:
        fails += 1


# ------------------------------------------------------------------ ① 合成 minidump
def make_dump(path, base=0x7FF700000000):
    """头(32) + 目录(2 条) + 模块表 + Memory64List + 数据；两个区段，第二段紧接第一段（VA 连续），再来一个有空洞的第三段。"""
    segs = [(0x1000, b"A" * 16), (0x1010, b"B" * 16), (0x5000, b"C" * 8)]
    name = "D:\\Games\\kards-Win64-Shipping.exe".encode("utf-16le")
    mod_rva = 32 + 12 * 2
    mod_sz = 4 + 108
    name_rva = mod_rva + mod_sz
    mem_rva = name_rva + 4 + len(name)
    mem_sz = 16 + 16 * len(segs)
    data_rva = mem_rva + mem_sz
    buf = bytearray()
    buf += struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, 2, 32, 0, 0, 0)
    buf += struct.pack("<III", 4, mod_sz, mod_rva)
    buf += struct.pack("<III", 9, mem_sz, mem_rva)
    buf += struct.pack("<I", 1)
    buf += struct.pack("<QIIII", base, 0x1000, 0, 0, name_rva) + b"\0" * (108 - 24)
    buf += struct.pack("<I", len(name)) + name
    buf += struct.pack("<QQ", len(segs), data_rva)
    for va, d in segs:
        buf += struct.pack("<QQ", va, len(d))
    for _va, d in segs:
        buf += d
    with open(path, "wb") as f:
        f.write(buf)
    return base


def t_dump():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.dmp")
        base = make_dump(p)
        chk("module_base 从模块表里认出游戏 exe", RB.module_base(p) == base, hex(RB.module_base(p) or 0))
        m = RB._mem_class()(p)
        chk("区段内读", m.read(0x1004, 4) == b"AAAA")
        chk("跨相邻区段拼接", m.read(0x100C, 8) == b"AAAABBBB", repr(m.read(0x100C, 8)))
        chk("读到空洞 ⇒ None（不抛）", m.read(0x3000, 4) is None)
        chk("读一半落进空洞 ⇒ 只给到能读的前半（调用方的 read_exact 会判长度不足为失败）",
            m.read(0x5004, 16) == b"CCCC")
        chk("MemRO.read_exact 对短读判失败", m.read_exact(0x5004, 16) is None)
        chk("u32 之类整数读沿用 MemRO", m.u32(0x1000) == 0x41414141)
        # 覆盖层（`--orders` / freshen 用）：补丁只在本进程里生效，文件不变；假地址副本可读可补丁
        m.patch(0x1002, b"ZZ")
        chk("patch：覆盖转储里已有字节（含跨区段读）", m.read(0x1000, 6) == b"AAZZAA" and m.read(0x100E, 4) == b"AABB")
        a = m.alloc_copy(0x1000, 16)
        chk("alloc_copy：新假地址、内容 = 源（含补丁）", a and a >= m.FAKE_BASE and m.read(a, 16) == b"AAZZ" + b"A" * 12)
        m.patch(a + 1, b"Q")
        chk("对副本打补丁不影响源", m.read(a, 3) == b"AQZ" and m.read(0x1000, 3) == b"AAZ")
        chk("alloc_copy 源读不满 ⇒ None", m.alloc_copy(0x5004, 16) is None)
        m.close()
        with open(p, "rb") as f:
            chk("转储文件本身没被改", b"ZZ" not in f.read())
        chk("转储里没有游戏模块 ⇒ None", RB.module_base(p, module="nope.exe") is None)


# ------------------------------------------------------------------ ② 离线护栏
def t_guard():
    from agent import precheck
    orig = precheck.call_read
    raised = False
    with RB.offline_guard():
        try:
            precheck.call_read("anything")
        except RB.OfflineViolation:
            raised = True
    chk("护栏内 `precheck.call_read` 抛 OfflineViolation", raised)
    chk("护栏退出后还原", precheck.call_read is orig)


# ------------------------------------------------------------------ ③ 聚合
def _row(same=0, ab=0, bc=0, sk=0, reasons=None, keys=None):
    from collections import Counter
    return {"same": same, "diff_ab": ab, "diff_bc": bc, "skipped": sk, "reasons": Counter(reasons or {}),
            "samples": ({"diff_ab": {"card": "x", "diff": {"hq": "1"}}} if ab else {}), "diff_keys": Counter(keys or {})}


class FakeKm:
    pass


def t_aggregate():
    calls = []

    def opener(path):
        if "bad" in path:
            raise RuntimeError("不是 minidump")
        st = type("St", (), {"cards": [1], "my_side": 1, "turn": 3})()
        return FakeKm(), st

    def recon(km, st):
        calls.append(1)
        n = len(calls)
        rows = {"A": _row(same=2), "B": _row(ab=1, same=1, keys={"diff_ab:hq": 1}),
                "C": _row(sk=1, reasons={"随机/抉择": 1})}
        if n == 2:
            rows["A"] = _row(bc=1, keys={"diff_bc:deck": 1})
        return {"rows": rows, "gaps": {}, "elapsed_s": 0.1, "n_hand": 3, "n_pairs": 4}

    lines = []
    rep = RB.run_batch(["d1.dmp", "bad.dmp"], ["s1", "s2"], log=lines.append, reconcile=recon, opener=opener,
                       scenario_maker=lambda st, sc: st)
    cs = rep["cards"]
    chk("按牌聚合：A = same 2 + diff_bc 1 ⇒ 结论 diff_bc（diff 优先于 same）", cs["A"]["status"] == "diff_bc"
        and cs["A"]["same"] == 2 and cs["A"]["diff_bc"] == 1, str(cs["A"]))
    chk("B 有 diff_ab ⇒ diff_ab 优先于 same", cs["B"]["status"] == "diff_ab" and cs["B"]["same"] == 2, str(cs["B"]))
    chk("C 只有 skipped ⇒ skipped，原因保留", cs["C"]["status"] == "skipped" and "随机/抉择" in cs["C"]["reasons"])
    chk("diff 键聚合", cs["B"]["diff_keys"].get("diff_ab:hq") == 2, str(cs["B"]["diff_keys"]))
    chk("坏转储记进 errors、不拖垮整批", len(rep["errors"]) == 1 and rep["errors"][0]["dump"] == "bad.dmp"
        and len(rep["runs"]) == 2, str(rep["errors"]))
    s = rep["summary"]
    chk("汇总：按牌 diff_ab/diff_bc/skipped/same 各 1/1/1/0；比对次数分开记",
        (s["diff_ab"], s["diff_bc"], s["skipped"], s["same"]) == (1, 1, 1, 0) and s["compare_same"] == 4 and s["compare_diff_ab"] == 2, str(s))
    chk("报告标明离线", rep["offline"] is True)
    tbl = RB.format_table(rep)
    chk("表格里有三张牌与汇总行", all(n in tbl for n in ("A ", "B ", "C ")) and "官方指令" in tbl)
    # 原因归一：地址/目标被抹掉
    chk("原因归一（去掉目标/地址）",
        RB._reason("FOO→12：Unimplemented @0x4A LetObj: x") == "Unimplemented @0x… LetObj: x",
        RB._reason("FOO→12：Unimplemented @0x4A LetObj: x"))
    # build_report 的 API 覆盖面
    rep2 = RB.build_report({}, [], [], ["a.dmp"], ["s"], 1.0, api_titles=["X", "Y"])
    chk("API 指令覆盖：没碰到的列出来", rep2["api_orders"] == {"total": 2, "reached": 0, "not_reached": ["X", "Y"]},
        str(rep2["api_orders"]))


# ------------------------------------------------------------------ ④ reconcile_state 端到端（假 km/假 VM）
def t_reconcile_state():
    import engine.effectvm as _EV
    import semantics.effectvm as EV
    from semantics.shadow import apply_calls
    import player.rule as R                                       # noqa: F401
    from kardsmem.board import BoardState
    from kardsmem import gamemodel as GM
    from _cards import mk_card

    def run(direct_applies, only=None, extra_order=False):
        order = mk_card(40, side=ME, location="hand", card_type="order", name="ORD", kredit_cost=1, ptr=40)
        hq_me = mk_card(1, side=ME, location="hq", name="HQ", ptr=1, defense=20)
        hq_op = mk_card(2, side=OPP, location="hq", name="EHQ", ptr=2, defense=20)
        cards = [order, hq_me, hq_op]
        if extra_order:
            cards.append(mk_card(41, side=ME, location="hand", card_type="order", name="OTH", kredit_cost=1, ptr=41))
        st = BoardState(source="selftest", turn=5, our_turn=True, my_side=ME, frontline_owner=None,
                        cards=cards)
        st.kredits = {ME: 5, OPP: 3}
        st.game = GM.GameState(mySide=ME)
        applied = [("kredits", "mine", 2)]
        old = (EV.enumerate_effects, EV.record_effects, EV.make_read_hooks)

        def fake_enum(*a, **k):
            return {"outcomes": [(1.0, {"kredit": 2})], "complete": True, "stopped": None, "choice": None}

        def fake_rec(*a, direct_state=None, **k):
            if direct_state is not None and direct_applies:
                apply_calls(direct_state, applied)
            return {"records": [], "stopped": None, "complete": True, "chance": [], "choice": None,
                    "applied": applied if direct_applies else [], "direct_gaps": []}
        EV.enumerate_effects, EV.record_effects = fake_enum, fake_rec
        EV.make_read_hooks = lambda *a, **k: {}
        try:
            with RB.offline_guard():
                out = RB.reconcile_state(K(), st, only=only)
        finally:
            EV.enumerate_effects, EV.record_effects, EV.make_read_hooks = old
        _ = _EV
        return out

    out = run(True)
    row = out["rows"].get("ORD")
    chk("不带目标的指令进了对账（rule 里 pair_eff 只给带目标的牌）", row is not None and out["n_pairs"] >= 1, str(dict(out["rows"])))
    chk("直跑与字典路一致 ⇒ same", row is not None and row["same"] == 1 and not row["diff_ab"], str(row))
    out = run(False)
    row = out["rows"].get("ORD")
    out = run(True, only=None, extra_order=True)
    chk("不给 `only` ⇒ 手牌里两张指令都对账", set(out["rows"]) == {"ORD", "OTH"}, str(sorted(out["rows"])))
    out = run(True, only=["ord"], extra_order=True)
    chk("`only=[\"ord\"]`（大小写不敏感）⇒ 只对账点名的那张，手牌里其余指令被移除（`--only` 加速）",
        set(out["rows"]) == {"ORD"}, str(sorted(out["rows"])))
    out = run(False)
    row = out["rows"].get("ORD")
    chk("直跑漏做 ⇒ diff_ab（带字段）", row is not None and row["diff_ab"] == 1 and any(k.startswith("diff_ab:")
                                                                                 for k in row["diff_keys"]), str(row))


def t_only():
    """`--only`：名单匹配认整名（含 ` <变体>` 后缀）或去后缀的标题，大小写不敏感。"""
    only = {"COUNTERATTACK", "ROUT"}
    chk("--only 匹配标题", RB._only_match("ROUT", only))
    chk("--only 匹配带变体后缀的整名（去后缀后命中）", RB._only_match("COUNTERATTACK <german_counterattack>", only))
    chk("--only 大小写不敏感", RB._only_match("rout", only))
    chk("--only 不在名单 ⇒ 不匹配", not RB._only_match("RUSH", only))
    chk("--only 变体整名可单独点名", RB._only_match("COUNTERATTACK <german_counterattack>", {"COUNTERATTACK <GERMAN_COUNTERATTACK>"}))


def main():
    t_only()
    t_dump()
    t_guard()
    t_aggregate()
    t_reconcile_state()
    print("\n失败 %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
