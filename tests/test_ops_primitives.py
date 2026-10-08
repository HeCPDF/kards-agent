#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 原语（`ops/primitives.py`）的离线测试 —— 步骤 3 的"**行为不变**"证据。

为什么要这个文件
================
步骤 3 把散在 `ops/conn.py` / 各 mixin 里的原语抽成 `ops/primitives.py`。这类"搬家"最容易出的
错**离线测试抓不到**：函数还在、名字还在、`run_all` 全绿，但**发出去的 RPC 名或编码变了**
（比如指针忘了走 hex 字符串、`arr_off` 少了 `-1`、写清单少了 `hold` 位）—— 而 RPC 名/编码
**只有连着游戏才看得出来**。

⇒ 这里拿**记录型假 api**（`FakeApi`）把每个原语"到底发了什么"逐字段断言下来，并且拿
`encode_writes` 与**拆分前内联在调用点的那两段推导式**（`gesture.py` 的 `jw = [...]`、
`play.py` 的 `jw = [...]`）逐字对比 —— 钉住的是"搬完还是同一串字节"，不是"函数还在"。

覆盖的坑（都是真踩过的）：
  * 指针必须走 **hex 字符串**（JS number 是 double，超过 2^53 的地址会被截断）；
  * `arr_off` 不给 = **`-1`**（少了它就会在 `parms` 上盖 16 字节 TArray 头 ⇒ `Location@0x00` 被写成 0）；
  * `write_then_call_keep` 的第 5 位 **hold**（箭头那几项不还原，卡对象那项必须还原）；
  * `settle(frames=0)` **一次都不许碰 frida**（抽原语时最容易把"只等墙钟"变成"顺手 attach"）；
  * frida 的 `exports_sync` 会把**下划线转驼峰**（`frida/__init__.py::_to_camel_case`）
    ⇒ 原语里的 RPC 属性名必须与拆分前**逐字相同**，这里对它们做一次登记式断言。
"""
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ops import primitives as P                                          # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


# ---------------------------------------------------------------- 假 api
class FakeApi:
    """记录每一次 RPC：`calls` 里存 `(rpc属性名, 参数…)`，`rets` 里按属性名给返回值。

    属性名**故意写成 snake_case** —— 与 `ops/conn.py` 里调用的写法一致（frida 会转驼峰）。
    """

    def __init__(self, rets=None):
        self.calls = []
        self.rets = rets or {}
        self.ticks = 0

    def _rec(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.rets:
            return self.rets[name]
        return [] if name in ("write_and_call", "writeThenCalls", "writeAndCall2",
                              "call_out_u8_batch") else 0

    # --- 只读 ---
    def call_raw(self, obj, func, parms):
        return self._rec("call_raw", obj, func, parms)

    def callRawArr(self, obj, func, parms, arr_off, ptrs):
        return self._rec("callRawArr", obj, func, parms, arr_off, ptrs)

    def call0(self, obj, func):
        return self._rec("call0", obj, func)

    def call_ptr(self, obj, func, arg):
        return self._rec("call_ptr", obj, func, arg)

    def call_i32(self, obj, func, v):
        return self._rec("call_i32", obj, func, v)

    def call_out_u8_batch(self, items):
        return self._rec("call_out_u8_batch", items)

    def peek(self, obj, off, kind):
        return self._rec("peek", obj, off, kind)

    def gt_ticks(self):
        return self.ticks

    # --- 写 ---
    def poke(self, obj, off, kind, value):
        return self._rec("poke", obj, off, kind, value)

    def poke_and_call0(self, obj, off, kind, value, cob, cf):
        return self._rec("poke_and_call0", obj, off, kind, value, cob, cf)

    def write_and_call(self, writes, cob, cf):
        return self._rec("write_and_call", writes, cob, cf)

    def callRawArrHold(self, writes, obj, func, parms, arr_off, ptrs):
        return self._rec("callRawArrHold", writes, obj, func, parms, arr_off, ptrs)

    def writeCallRaw(self, writes, obj, func, parms, arr_off, ptrs):
        return self._rec("writeCallRaw", writes, obj, func, parms, arr_off, ptrs)

    def writeThenCalls(self, writes, calls):
        return self._rec("writeThenCalls", writes, calls)

    def writeAndCall2(self, writes, oa, fa, ob, fb):
        return self._rec("writeAndCall2", writes, oa, fa, ob, fb)


class FakeMem:
    """够 `obj_alive` / `read_world_levels` / `read_level_actors` 用的最小内存。"""

    def __init__(self, size=0x8000):
        self.mem = bytearray(size)

    def u8(self, a):
        return self.mem[a]

    def u32(self, a):
        return struct.unpack_from("<I", self.mem, a)[0]

    def i32(self, a):
        return struct.unpack_from("<i", self.mem, a)[0]

    def ptr(self, a):
        return struct.unpack_from("<Q", self.mem, a)[0]

    def ptr_or_zero(self, a):
        return struct.unpack_from("<Q", self.mem, a)[0]

    def read(self, a, n):
        return bytes(self.mem[a:a + n])


class FakeOA:
    def __init__(self, mem, step=0x18, base=0x1000):
        self.mem, self.step, self.base = mem, step, base

    def item_addr(self, i):
        return self.base + i * self.step

    def class_of(self, obj):
        return self.mem.mem[obj + 0x10] if obj else 0


class FakePool:
    def __init__(self, m):
        self.m = m

    def fname_of(self, cls):
        return self.m.mem.get(cls) if isinstance(self.m.mem, dict) else None


def put_u64(mem, a, v):
    struct.pack_into("<Q", mem.mem, a, v)


def put_i32(mem, a, v):
    struct.pack_into("<i", mem.mem, a, v)


def main():
    P.assert_name_tables_consistent()
    chk("① 两张原语名字表自洽（不重叠、都真的定义在本模块）",
        not (set(P.WRITE_PRIMITIVES) & set(P.READ_PRIMITIVES))

        and all(hasattr(P, n) for n in P.WRITE_PRIMITIVES + P.READ_PRIMITIVES),
        "写 %d / 读 %d" % (len(P.WRITE_PRIMITIVES), len(P.READ_PRIMITIVES)))

    # ---------------------------------------------------------- 编码
    writes = [(0x2000, 0x950, "ptr", 0x3000), (0x2000, 0x1A0, "s32", 7), (0x2000, 0x1B0, "u8", 5)]
    # ★ 拆分前 `gesture.py:383` / `play.py:421` 内联的就是下面这条推导式 —— 逐字对比。
    legacy = [[hex(int(o)), int(off), kind, (hex(int(v)) if kind == "ptr" else int(v))]
              for (o, off, kind, v) in writes]
    chk("② encode_writes 与拆分前调用点内联的推导式**逐字相同**", P.encode_writes(writes) == legacy,
        str(P.encode_writes(writes)))
    chk("② encode_writes：指针走 hex 字符串（JS double 装不下 64 位地址）",
        P.encode_writes([(1, 2, "ptr", 0x7FF6AABBCCDD)])[0][3] == "0x7ff6aabbccdd")
    chk("② encode_writes_mask：第 5 位 = hold（`call_raw_writes` 的语义）",
        [w[4] for w in P.encode_writes_mask(writes + [(1, 2, "s32", 3, True)])] == [0, 0, 0, 1]
        and P.encode_writes_mask(writes) == [s + [0] for s in legacy])
    chk("② parms_hex：bytes 走 .hex()、**已经是 hex 字符串的原样透传**（老调用点两种写法混用）",
        P.parms_hex(bytes(range(4))) == "00010203" and P.parms_hex("00" * 0x18) == "00" * 0x18
        and P.parms_hex(None) == "")

    # ---------------------------------------------------------- 读原语
    api = FakeApi({"call_raw": bytes(range(8))})
    P.call_ufunction(api, 0x1234, 0x5678, b"\x01\x02")
    chk("③ call_ufunction：`call_raw(objHex, funcHex, parmsHex)` —— 与拆分前逐字相同",
        api.calls[-1] == ("call_raw", "0x1234", "0x5678", "0102"), str(api.calls[-1]))
    P.call_ufunction(api, 0x1, 0x2, "00" * 0x18)          # 老调用点是 str，不能变
    chk("③ call_ufunction：hex 字符串参数原样发出去", api.calls[-1][3] == "00" * 0x18)

    P.call_ufunction_arr(api, 0x10, 0x20, b"\x00" * 0x18, 0x18, [0xAA, 0xBB])
    chk("③ call_ufunction_arr：TArray 指针走 hex 列表、arr_off 原样（0x18）",
        api.calls[-1] == ("callRawArr", "0x10", "0x20", "00" * 0x18, 0x18, ["0xaa", "0xbb"]),
        str(api.calls[-1]))

    P.call_out_u8(api, 0x10, 0x20)
    chk("③ call_out_u8：ParmsSize=1 的全 0 缓冲（\"00\"）—— 拆分前是 `\"00\"`",
        api.calls[-1] == ("call_raw", "0x10", "0x20", "00"), str(api.calls[-1]))

    P.call0(api, 1, 2)
    P.call_ptr(api, 1, 2, 0xDEAD)
    P.call_i32(api, 1, 2, -3)
    chk("③ call0/call_ptr/call_i32 的 RPC 名与参数（frida 会把下划线转驼峰）",
        api.calls[-3:] == [("call0", "0x1", "0x2"), ("call_ptr", "0x1", "0x2", "0xdead"),
                           ("call_i32", "0x1", "0x2", -3)], str(api.calls[-3:]))

    # ---------------------------------------------------------- 写原语
    api2 = FakeApi({"poke": 1})
    P.write_then_call(api2, writes, 0x99, 0x88, b"\x00" * 0x10)
    chk("④ write_then_call ⇒ callRawArrHold；**arr_off 默认 -1**（少了它会在 parms 上盖 TArray 头）",
        api2.calls[-1] == ("callRawArrHold", legacy, "0x99", "0x88", "00" * 0x10, -1, []),
        str(api2.calls[-1])[:140])
    P.write_then_call_keep(api2, writes + [(1, 2, "s32", 3, True)], 0x99, 0x88, b"\x00" * 0x10)
    chk("④ write_then_call_keep ⇒ writeCallRaw；**hold 位按项带**（箭头不还原 / 卡对象还原）",
        api2.calls[-1][0] == "writeCallRaw" and [w[4] for w in api2.calls[-1][1]] == [0, 0, 0, 1],
        str(api2.calls[-1][1]))
    P.write_then_calls(api2, writes, [["0x1", "0x2", ""]])
    chk("④ write_then_calls ⇒ writeThenCalls（拆前是 gesture.py 直接拼 RPC 的那句）",
        api2.calls[-1] == ("writeThenCalls", legacy, [["0x1", "0x2", ""]]), str(api2.calls[-1])[:120])
    P.write_and_call2(api2, writes, 0x11, 0x22, 0x11, 0x33)
    chk("④ write_and_call2 ⇒ writeAndCall2（两个松手口同一次执行）",
        api2.calls[-1] == ("writeAndCall2", legacy, "0x11", "0x22", "0x11", "0x33"))
    P.write_fields_then_call(api2, writes, 0x11, 0x22)
    chk("④ write_fields_then_call ⇒ write_and_call（拆分前就是这么拼 spec 的）",
        api2.calls[-1] == ("write_and_call", legacy, "0x11", "0x22")
        and P.write_fields_then_call(api2, []) == []
        and api2.calls[-1] == ("write_and_call", [], None, None), str(api2.calls[-1]))
    P.poke(api2, 0x2000, 0x950, "ptr", 0x3000)
    chk("④ poke：指针走 hex、其余原样（拆分前就是这么分的）",
        api2.calls[-1] == ("poke", "0x2000", 0x950, "ptr", "0x3000") and
        P.poke(api2, 0x2000, 0x40, "s32", 5) is True and api2.calls[-1][4] == 5)

    # ---------------------------------------------------------- settle
    api3 = FakeApi()
    P.settle(api3, 0.0, 0)
    chk("⑤ settle(frames=0)：**一次都不碰 frida**（抽原语时最容易变成「顺手 attach」）",
        api3.calls == [], str(api3.calls))

    class Boom:
        def gt_ticks(self):
            raise RuntimeError("没连上")

    P.settle(lambda: Boom(), 0.0, 3)
    chk("⑤ settle：帧计数取不到 ⇒ 退回纯墙钟、不抛（失焦降帧那条纪律的兜底）", True)

    n = {"calls": 0}

    class Ticker:
        def gt_ticks(self):
            n["calls"] += 1
            return n["calls"] * 10

    P.settle(lambda: Ticker(), 0.0, 25, cap=2.0)
    chk("⑤ settle(frames=25)：按**游戏帧**等到差值 ≥ frames 才返回（弯路 #29）", n["calls"] >= 3, str(n))

    # ---------------------------------------------------------- obj_alive
    mem = FakeMem()
    oa = FakeOA(mem)
    obj = 0x2000
    put_u64(mem, oa.item_addr(3), obj)          # FUObjectItem.Object 指回来
    struct.pack_into("<I", mem.mem, obj + 0x0C, 3)
    put_u64(mem, oa.item_addr(3) + 8, 0)
    chk("⑥ obj_alive：活着 ⇒ True", P.obj_alive(mem, oa, obj) is True)
    put_u64(mem, oa.item_addr(3) + 8, 0x200000)                     # Garbage
    chk("⑥ obj_alive：Garbage 位 ⇒ False（#5 崩溃分析）", P.obj_alive(mem, oa, obj) is False)
    put_u64(mem, oa.item_addr(3) + 8, 0)
    put_u64(mem, oa.item_addr(3), 0x9999)                           # 槽位已被复用
    chk("⑥ obj_alive：ObjectItem 不再指回它 ⇒ False", P.obj_alive(mem, oa, obj) is False)
    put_u64(mem, oa.item_addr(3), obj)
    struct.pack_into("<I", mem.mem, obj + 0x08, 0x10000)            # FinishDestroyed
    chk("⑥ obj_alive：BeginDestroyed/FinishDestroyed ⇒ False", P.obj_alive(mem, oa, obj) is False)
    struct.pack_into("<I", mem.mem, obj + 0x08, 0)
    chk("⑥ obj_alive：空指针 / 未对齐 ⇒ False（宁可不点）",
        P.obj_alive(mem, oa, 0) is False and P.obj_alive(mem, oa, 0x2002) is False)

    # ---------------------------------------------------------- find_actors
    #   ★ 指针**必须落在 0x10000..0x7FFFFFFFFFFF**（`PTR_RANGE`）—— 低值/不齐的值是要被滤掉的垃圾。
    mem2 = FakeMem(0x20000)
    world, LEVELS_OFF, ACTORS_OFF = 0x100, 0x1C8, 0x2B8
    put_u64(mem2, world + LEVELS_OFF, 0x3000)
    put_i32(mem2, world + LEVELS_OFF + 8, 2)
    put_u64(mem2, 0x3000, 0x14000)              # level[0]
    put_u64(mem2, 0x3008, 0x15000)              # level[1]
    put_u64(mem2, 0x14000 + ACTORS_OFF, 0x16000)
    put_i32(mem2, 0x14000 + ACTORS_OFF + 8, 2)
    put_u64(mem2, 0x16000, 0x19000)             # actor A
    put_u64(mem2, 0x16008, 0x8001)              # 太小/不对齐的垃圾 ⇒ 丢掉
    put_u64(mem2, 0x15000 + ACTORS_OFF, 0x17000)
    put_i32(mem2, 0x15000 + ACTORS_OFF + 8, 1)
    put_u64(mem2, 0x17000, 0x1A000)             # actor B
    levels = P.read_world_levels(mem2, world, LEVELS_OFF)
    chk("⑦ read_world_levels：整读完把非指针值滤掉（0x10000 ≤ p ≤ 0x7FFFFFFFFFFF）",
        levels == [0x14000, 0x15000], str([hex(x) for x in levels]))

    class OA2:
        def class_of(self, a):
            return {0x19000: 1, 0x1A000: 2}.get(a, 0)

    class Pool2:
        def fname_of(self, c):
            return {1: "BP_Board_C", 2: "BP_Deck_C"}.get(c)

    actors, by_cls = P.read_level_actors(mem2, OA2(), Pool2(), levels, ACTORS_OFF)
    chk("⑦ read_level_actors：**遍历全部 level**（只看 PersistentLevel 会漏掉牌库那类）+ 按类名归类 + 滤垃圾",
        actors == [0x19000, 0x1A000] and by_cls == {"BP_Board_C": [0x19000], "BP_Deck_C": [0x1A000]},
        "%s / %s" % ([hex(a) for a in actors], by_cls))
    chk("⑦ read_level_actors：Data 指针为 0 的 level 直接跳过（不读垃圾）",
        P.read_level_actors(mem2, OA2(), Pool2(), [0x14000], ACTORS_OFF)[0] == [0x19000])

    # ---------------------------------------------------------- RPC 名登记（frida 转驼峰）
    src = open(os.path.join(ROOT, "ops", "primitives.py"), encoding="utf-8").read()
    rpc = re.findall(r"api\.(\w+)\(", src)
    chk("⑧ primitives 里用到的 RPC 属性名集中在已知集合里（frida 会下划线→驼峰；写错名只会运行期炸）",
        set(rpc) <= {"call_raw", "callRawArr", "callRawArrHold", "writeCallRaw", "writeThenCalls",
                     "writeAndCall2", "call0", "call_ptr", "call_i32", "call_out_u8_batch",
                     "peek", "poke", "poke_and_call0", "write_and_call", "gt_ticks"}, str(sorted(set(rpc))))
    js = open(os.path.join(ROOT, "ops", "agent.js.tpl"), encoding="utf-8").read()
    camel = set(re.findall(r"^\s{4}(\w+): function", js, re.M))

    def to_camel(name):
        out, cap = "", False
        for ch in name:
            if ch == "_":
                cap = True
            elif cap:
                out, cap = out + ch.upper(), False
            else:
                out += ch
        return out
    missing = sorted(n for n in set(rpc) if to_camel(n) not in camel)
    chk("⑧ 上面每个 RPC 名在 ops/agent.js.tpl 里**真的有对应导出**（两边名字对不上 = 运行期才炸）",
        not missing, "缺：%s" % missing)

    # ---------------------------------------------------------- ⑨ Injector 的转发层
    #  步骤 3 的验收是"`Injector` 改为**调用**原语、而**公共方法名/签名/返回值一个都不许变**"
    #  ⇒ 这里拿 `Injector` 本体（`__new__` 绕过 attach）打一遍公共入口，断言"发出去的 RPC 逐字不变"。
    from ops.inject import Injector
    chk("⑨ Injector 的公共原语方法**全都还在**（`agent/` 与脚本在用）",
        all(hasattr(Injector, n) for n in ("settle", "obj_alive", "call_raw_arr", "call_raw_hold",
                                           "call_raw_writes", "call_out_u8", "write_and_call", "poke",
                                           "peek", "call0", "call_ptr", "call_i32", "poke_and_call0")))
    inj = Injector.__new__(Injector)                 # 不走 __init__：不 attach、不碰游戏
    inj._api = FakeApi({"call_raw": bytes(range(8))})
    inj.m, inj.oa = mem, oa
    inj.call_raw_hold(writes, 0x99, 0x88, b"\x00" * 0x10)
    chk("⑨ Injector.call_raw_hold ⇒ 与拆分前同一串 RPC（callRawArrHold + encode_writes + arr_off=-1）",
        inj._api.calls[-1] == ("callRawArrHold", legacy, "0x99", "0x88", "00" * 0x10, -1, []),
        str(inj._api.calls[-1])[:120])
    inj.call_raw_writes(writes + [(1, 2, "s32", 3, True)], 0x99, 0x88, b"\x00" * 0x10)
    chk("⑨ Injector.call_raw_writes ⇒ writeCallRaw（第 5 位 hold 保住）",
        inj._api.calls[-1][0] == "writeCallRaw" and [w[4] for w in inj._api.calls[-1][1]] == [0, 0, 0, 1])
    inj.call_raw_arr(0x10, 0x20, b"\x00" * 0x18, 0x18, [0xAA])
    chk("⑨ Injector.call_raw_arr ⇒ callRawArr",
        inj._api.calls[-1] == ("callRawArr", "0x10", "0x20", "00" * 0x18, 0x18, ["0xaa"]))
    inj.call_out_u8(0x10, 0x20)
    chk("⑨ Injector.call_out_u8 ⇒ call_raw(\"00\")", inj._api.calls[-1] == ("call_raw", "0x10", "0x20", "00"))
    inj.poke(0x2000, 0x950, "ptr", 0x3000)
    chk("⑨ Injector.poke ⇒ 指针走 hex", inj._api.calls[-1] == ("poke", "0x2000", 0x950, "ptr", "0x3000"))
    inj.write_and_call(writes, 0x11, 0x22)
    chk("⑨ Injector.write_and_call ⇒ write_and_call（spec 与拆分前同形）",
        inj._api.calls[-1] == ("write_and_call", legacy, "0x11", "0x22"))
    api9 = FakeApi()
    inj._api = api9
    inj.settle(0.0, 0)
    chk("⑨ Injector.settle(0 帧) 走原语 ⇒ 仍然**一次都不碰 frida**", api9.calls == [], str(api9.calls))
    chk("⑨ Injector.obj_alive 走原语 ⇒ 判活语义不变（⑥ 收尾状态是活的 ⇒ True；空指针 ⇒ False）",
        inj.obj_alive(0x2000) is True and inj.obj_alive(0) is False,
        "%s / %s" % (inj.obj_alive(0x2000), inj.obj_alive(0)))
    print("失败 %d 项" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
