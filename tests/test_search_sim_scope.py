#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`_search_sim` 的建 sim 作用域（`kardsmem.readscope`）——共享读缓存必须与"各读各的"结果**逐字相同**。

背景（2026-10-07，离线剖析）：`death_fx` / `draw_fx` / `attack_fx` 逐项各跑几百次 VM 空跑，每次都白手起家
（重读卡视图含 `atomic` 的 sleep、重走反射链、重算全盘数值表、重扫全盘建指针表）。作用域里让它们共用一份。

钉住的三类东西：
  1. **等价**：一串盘面变化（每步改内存里的值 + 改快照里的攻防）下，开作用域 / 不开作用域的
     `_death_fx` / `_draw_fx` / `_attack_fx` 逐步完全相同，且步与步之间的结果**确实变了**（不是两边恒空）；
  2. **边界**：作用域外零影响；不跨作用域（下一步看到新值）；读失败不进共享表；容器字段共享的是**副本**
     （一次空跑就地改它，别的空跑看不到）；指针表/数值表在快照换了（不同列表对象 / 长度变）时失效；
  3. **同批的两个跨作用域记忆**：`kismetlib.arity`/`CardNatives.arity`（按函数对象身份）、`choosespawn.legal_titles`
     （按文件 mtime+大小）——换实现 / 改文件必须立刻失效。
"""
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

from _hookfake import ME, OPP, TC, Checker, K, card, patched      # noqa: E402
import player.rule as R                                            # noqa: E402
from kardsmem import cards as CARDS, props as PROPS, readscope as RS   # noqa: E402
import canplay                                                     # noqa: E402  (tools/，由 _bootstrap 接好路径)

chk = Checker()


# ------------------------------------------------------------------ 1. 边界：readscope 本身
def test_scope_basics():
    chk("作用域外 current() 为 None", RS.current() is None)
    with RS.build_scope() as a:
        chk("作用域内 current() 是它", RS.current() is a)
        with RS.build_scope() as b:
            chk("嵌套复用最外层（内层不新开）", b is a and RS.current() is a)
        chk("内层退出不清外层", RS.current() is a)
        seen = []
        t = threading.Thread(target=lambda: seen.append(RS.current()))
        t.start()
        t.join()
        chk("线程私有：别的线程看不到本线程的作用域", seen == [None])
    chk("退出后清掉", RS.current() is None)
    try:
        with RS.build_scope():
            raise RuntimeError("x")
    except RuntimeError:
        pass
    chk("异常退出也清掉", RS.current() is None)


# ------------------------------------------------------------------ 2. 边界：卡视图
class FakeSess:
    pass


def test_view_sharing():
    mem = {1: {"card_id": 1, "attack": 3}, 2: {"card_id": 2, "attack": 5}}
    n = {"calls": 0}
    old = CARDS.read_raw

    def fake_read(sess, ptr):
        n["calls"] += 1
        return dict(mem[ptr]) if ptr in mem else None
    CARDS.read_raw = fake_read
    try:
        s = FakeSess()
        v1, v2 = canplay.make_view(s), canplay.make_view(s)
        v1(1), v2(1)
        chk("作用域外：两次空跑各读各的（2 次 read_raw）", n["calls"] == 2, n["calls"])
        n["calls"] = 0
        with RS.build_scope():
            v1, v2 = canplay.make_view(s), canplay.make_view(s)
            a, b = v1(1), v2(1)
            v1(1)
            chk("作用域内：同一张牌只读一次，两次空跑拿到同样内容", n["calls"] == 1 and a == b == mem[1], n["calls"])
            mem[1]["attack"] = 9                 # 作用域内游戏内存变了（不应发生；这里钉的是"共享表按作用域期固定"）
            chk("作用域内后到的空跑仍看到作用域起点那份（一致快照）", canplay.make_view(s)(1)["attack"] == 3)
        with RS.build_scope():
            chk("不跨作用域：新作用域重读（9）", canplay.make_view(s)(1)["attack"] == 9)
        # 读失败不进共享表：下一次空跑照旧重试
        n["calls"] = 0
        with RS.build_scope():
            f1 = canplay.make_view(s)(99)
            mem[99] = {"card_id": 99, "attack": 1}
            f2 = canplay.make_view(s)(99)
            chk("读失败（None→{}）不进共享表，后到的空跑重试并读到", f1 == {} and f2.get("attack") == 1, (f1, f2))
    finally:
        CARDS.read_raw = old


# ------------------------------------------------------------------ 3. 边界：反射链索引（find_prop）
def test_find_prop():
    rows = [{"name": "a", "owner": "sub", "offset": 1}, {"name": "b", "owner": "sub", "offset": 2},
            {"name": "a", "owner": "base", "offset": 99}]                  # 子类在前、同名取子类
    n = {"calls": 0}
    old = PROPS.class_props

    def fake(session, uclass, pool=None, max_depth=16):
        n["calls"] += 1
        return [dict(r) for r in rows] if uclass == 7 else []
    PROPS.class_props = fake
    try:
        s = FakeSess()
        r0 = [PROPS.find_prop(s, 7, nm) for nm in ("a", "b", "zz")]
        c0 = n["calls"]
        n["calls"] = 0
        with RS.build_scope():
            r1 = [PROPS.find_prop(s, 7, nm) for nm in ("a", "b", "zz", "a", "b")]
            c1 = n["calls"]
        chk("作用域外每个字段名各走一遍链（3 次）", c0 == 3, c0)
        chk("作用域内整个类只走一遍链", c1 == 1, c1)
        chk("结果与逐条扫描一致（同名取子类优先；找不到 ⇒ None）", r1[:3] == r0 and r1[0]["owner"] == "sub" and r1[2] is None
            and r1[3] is r1[0], r1)
        n["calls"] = 0
        with RS.build_scope():
            PROPS.find_prop(s, 8, "a")
            PROPS.find_prop(s, 8, "a")
        chk("读到 0 个属性的类（可能是瞬时读失败）不记，下次重走", n["calls"] == 2, n["calls"])
    finally:
        PROPS.class_props = old


# ------------------------------------------------------------------ 4. 边界：容器字段共享的是副本
def test_container_fields():
    state = {"reads": 0, "arr": [1, 2, 3]}
    olds = (PROPS.find_prop, canplay._read_array)

    class M:
        def ptr_or_zero(self, a):
            return 0x1000                                               # 任何对象的 UClass

        def i32(self, a):
            return 0

    class S:
        m = M()

        def names_pool(self):
            return None
    PROPS.find_prop = lambda sess, uc, name, pool=None: {"offset": 0, "type": "ArrayProperty", "name": name}

    def fake_arr(sess, m, a, p, name):
        state["reads"] += 1
        return list(state["arr"])
    canplay._read_array = fake_arr
    try:
        s = S()
        with RS.build_scope():
            g1, g2 = canplay.make_get_field(s), canplay.make_get_field(s)
            a = g1(0x10, "arr")
            a.append(999)                                                # 空跑 1 就地改（VM 数组操作可能这么做）
            b = g2(0x10, "arr")
            chk("共享表命中：只真读一次", state["reads"] == 1, state["reads"])
            chk("空跑 1 就地改它，空跑 2 看不到（共享的是副本）", b == [1, 2, 3] and b is not a, (a, b))
            b.append(5)
            chk("空跑 2 改了也不污染共享表", canplay.make_get_field(s)(0x10, "arr") == [1, 2, 3])
            chk("同一空跑内仍是同一个对象（旧语义不变）", g1(0x10, "arr") is a)
        state["reads"] = 0
        canplay.make_get_field(s)(0x10, "arr")
        canplay.make_get_field(s)(0x10, "arr")
        chk("作用域外：每个空跑自己读（旧语义）", state["reads"] == 2, state["reads"])
    finally:
        PROPS.find_prop, canplay._read_array = olds


# ------------------------------------------------------------------ 5. 快照派生表：失效条件
def test_snapshot_tables():
    from engine import effectvm as EV
    from engine import triggers as TR
    cards = [card(1, "A", "frontline", ME, ptr=101, attack=2, defense=3),
             card(2, "B", "frontline", OPP, ptr=102, attack=4, defense=5)]

    class Rec:
        def __init__(self):
            self.ptr_ids, self.cur_stats = {}, {}
    r0 = Rec()
    EV._fill_cur_stats(r0, cards)
    with RS.build_scope() as sc:
        r1, r2 = Rec(), Rec()
        EV._fill_cur_stats(r1, cards)
        EV._fill_cur_stats(r2, cards)
        chk("数值表：作用域内与原样重算逐键相同", r1.cur_stats == r0.cur_stats and r1.ptr_ids == r0.ptr_ids
            and bool(r0.cur_stats), str(r0.cur_stats))
        r1.cur_stats[101]["attack"] = 777
        chk("数值表：每次空跑拿到的是拷贝（改一份不影响下一次）", r2.cur_stats[101]["attack"] == r0.cur_stats[101]["attack"])
        chk("数值表：同一快照只算一遍", sc.stats()["misses"].get("cur_stats") == 1 and sc.stats()["hits"].get("cur_stats") == 1)
        cards2 = list(cards)
        cards2[0] = card(1, "A", "frontline", ME, ptr=101, attack=9, defense=3)
        r3 = Rec()
        EV._fill_cur_stats(r3, cards2)
        chk("数值表：换了列表对象 ⇒ 重算（看到新值）", r3.cur_stats[101]["attack"] == 9, str(r3.cur_stats[101]))
        m1 = TR._card_maps(type("S", (), {"cards": cards})())
        st_a = type("S", (), {"cards": cards})()
        a1, a2 = TR._card_maps(st_a), TR._card_maps(st_a)
        chk("指针表/id 表：同一快照命中同一份", a1[0] is a2[0] and sorted(a1[0]) == [101, 102] and sorted(a1[1]) == [1, 2])
        cards.append(card(3, "C", "frontline", ME, ptr=103))
        a3 = TR._card_maps(st_a)
        chk("指针表/id 表：同一个列表对象但长度变了 ⇒ 失效重建", sorted(a3[0]) == [101, 102, 103])
        b1, b2 = TR._byid_of(st_a), TR._byid_of(st_a)
        chk("byid 表：命中且正确", b1 is b2 and sorted(b1) == [1, 2, 3])
    st_o = type("S", (), {"cards": cards})()
    chk("作用域外：每次现算（不是同一个对象）", TR._card_maps(st_o)[0] is not TR._card_maps(st_o)[0])


# ------------------------------------------------------------------ 6. 跨作用域记忆：必须按"输入变了就失效"
def test_memos():
    from kardsmem import kismetlib as KL, cardnatives as CN
    from semantics import choosespawn as CS
    name = "__scope_test_fn"
    KL.PURE[name] = lambda a, b: 0
    try:
        n1 = KL.arity(name)
        KL.PURE[name] = lambda a, b, c: 0
        n2 = KL.arity(name)
        chk("kismetlib.arity：换了函数对象立刻失效", (n1, n2) == (2, 3), (n1, n2))
    finally:
        KL.PURE.pop(name, None)
    CN._TABLE[name] = lambda self, card, x: 0
    try:
        a1 = CN.CardNatives.arity(name)
        CN._TABLE[name] = lambda self, card, x, y: 0
        a2 = CN.CardNatives.arity(name)
        chk("CardNatives.arity：换了函数对象立刻失效", (a1, a2) == (1, 2), (a1, a2))
    finally:
        CN._TABLE.pop(name, None)
    d = tempfile.mkdtemp()
    p = os.path.join(d, "api.json")
    open(p, "w", encoding="utf-8").write('[{"json": {"title": {"en-EN": "Alpha"}}}]')
    t1 = CS.legal_titles(p)
    t1b = CS.legal_titles(p)
    open(p, "w", encoding="utf-8").write('[{"json": {"title": {"en-EN": "Alpha"}}}, {"json": {"title": {"en-EN": "Beta card"}}}]')
    t2 = CS.legal_titles(p)
    chk("legal_titles：未改文件命中同一份；文件变了立刻重读", t1 == frozenset({"ALPHA"}) and t1b is t1
        and t2 == frozenset({"ALPHA", "BETA CARD"}), (t1, t2))
    chk("legal_titles：读不出 ⇒ None（不记）", CS.legal_titles(os.path.join(d, "nope.json")) is None)


# ------------------------------------------------------------------ 7. 等价：一串盘面变化，开/不开作用域逐步相同
MEM = {}
READS = {"n": 0}


def fake_read_raw(sess, ptr):
    READS["n"] += 1
    return dict(MEM[ptr]) if ptr in MEM else None


class Sess2:
    pass


def mk_pol(O):
    pol = R.RuleV2.__new__(R.RuleV2)                 # 不走 __init__（要真会话/表）
    pol.P = dict(R.PARAMS, use_vm=True)
    pol.fx_meta = {}
    pol._km = lambda: K()
    pol._trig_cache = lambda st: TC(O)
    pol._trig_stream = lambda: None
    pol._hq_ptrs = lambda seat: ()
    pol._kw = lambda c: frozenset(c.keywords)
    pol._actionable = lambda st, a: True
    return pol


class St:
    def __init__(self, cards, ids):
        self.cards, self.my_side, self.other_side, self.turn, self.deck = cards, ME, OPP, 5, ids


def ovr(d):
    return {1000 + k: set(v) for k, v in d.items()}


def mk_board(step):
    """每步换一份盘面：攻防随步变（`_board_sig` 变 ⇒ Rule 自己的整盘缓存不会掩盖作用域的问题）。"""
    return [card(1, "A", "frontline", ME, ptr=101, attack=2 + step, defense=3),
            card(2, "B", "frontline", OPP, ptr=102, attack=4, defense=5 + step),
            card(3, "C", "back", OPP, ptr=103, attack=1, defense=1 + (step % 2)),
            card(4, "D", "back", ME, ptr=104, attack=3, defense=2),
            card(9, "HQ1", "hq", ME, ptr=109), card(10, "HQ2", "hq", OPP, ptr=110)]


def set_mem(step):
    MEM.clear()
    for p, a in ((101, 2 + step), (102, 4), (103, 1), (104, 3), (109, 0), (110, 0)):
        MEM[p] = {"card_id": p - 100, "attack": a + 10 * step}


def behave(h, c, a):
    """假 VM：每次"空跑"新建一个 view（与真 `record_effects` 一样），读自己和别的牌的内存值。"""
    view = canplay.make_view(Sess2())
    me = c.raw["ptr"]
    others = sorted(p for p in MEM if p != me)
    amt = view(me).get("attack", 0) + sum(view(p).get("attack", 0) for p in others[:2])
    return {"records": [{"verb": "GiveKreditsBySide", "args": [int(ME), amt, 0], "tainted": False}]}


def run_step(step, scoped):
    set_mem(step)
    O = ovr({101: {"OnOtherCardDrawnFromDeck", "OnDestroyed", "OnOtherCardDestroyed", "OnOtherCardDealDamageAddDamage"},
             102: {"OnDestroyed", "OnOtherCardDestroyed", "OnAfterAttack", "OnOtherCardDrawnFromDeck"},
             103: {"OnOtherCardDestroyed", "OnOtherCardDrawnFromDeck", "OnOtherCardAttacks"}})
    pol = mk_pol(O)
    cards = mk_board(step)
    st = St(cards, [20 + step, 21, 22])
    pol._deck_state = lambda st_: (list(st.deck), {})
    ehq = [c for c in cards if c.obj.IsHQ() and c.obj.side == OPP][0]
    out = {}
    with patched(O, behave):
        if scoped:
            with RS.build_scope():
                out["death"], out["draw"], out["attack"] = pol._death_fx(st), pol._draw_fx(st), dict(pol._attack_fx(st, ehq))
        else:
            out["death"], out["draw"], out["attack"] = pol._death_fx(st), pol._draw_fx(st), dict(pol._attack_fx(st, ehq))
    return out


def test_equivalence_sequence():
    old = CARDS.read_raw
    CARDS.read_raw = fake_read_raw
    try:
        prev_sig = None
        reads = {True: 0, False: 0}
        for step in range(6):
            res = {}
            for scoped in (False, True):
                READS["n"] = 0
                res[scoped] = run_step(step, scoped)
                reads[scoped] += READS["n"]
            same = res[True] == res[False]
            chk("步 %d：开/不开作用域的 death/draw/attack 逐项相同" % step, same, "" if same else (res[True], res[False]))
            sig = repr(sorted((k, repr(v)) for k, v in res[True].items()))
            chk("步 %d：death / draw / attack 三张表都非空（两边恒空的等价没有意义）" % step,
                all(bool(v) for v in res[True].values()), sig[:200])
            chk("步 %d：与上一步的结果不同（作用域没有把旧步的值带过来）" % step, sig != prev_sig)
            prev_sig = sig
        chk("作用域确实省了内存读（同一份盘面读次数更少）", reads[True] < reads[False], reads)
        chk("每步结束作用域都已清掉", RS.current() is None)
    finally:
        CARDS.read_raw = old


if __name__ == "__main__":
    test_scope_basics()
    test_view_sharing()
    test_find_prop()
    test_container_fields()
    test_snapshot_tables()
    test_memos()
    test_equivalence_sequence()
    print("失败 %d 项" % chk.fails)
    sys.exit(1 if chk.fails else 0)
