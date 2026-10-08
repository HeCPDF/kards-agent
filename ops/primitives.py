# -*- coding: utf-8 -*-
"""ops/primitives.py —— **L1 原语**（`OPS-ARCHITECTURE.md` §2/§7 步骤 3）。

为什么单独成模块
================
§2 的分层是 **L4 会话 → L3 动词 → L2 手势 → L1 原语 → L0 传输**，规则只有一条：
**只能向下依赖**。步骤 3 之前，这一层的实现散在 `ops/conn.py`（方法体内联 frida 的
RPC 名与 hex/字节编码）与各 mixin 里，于是：
  * 手势层想"写字段 + 同一次执行里提交"时**只能自己拼 JS RPC 调用**
    （`gesture.py::drag_release` 里那句 `self.api().writeThenCalls(jw, calls)` 就是这么来的）
    —— §2 明写「手势不得拼 JS 字符串，一律经 L1 原语」；
  * 上述编码（指针走 hex 字符串、`parms` 走 hex、`arr_off=-1` 表示"没有引用数组参数"）
    **没有单一出口**，各调用点各写一遍，改一处就漏一处。

本模块把这些**做成模块级函数**，参数全部显式传入（不再藏 `self`）：
  * 好处一：**可离线测**（`tests/test_ops_primitives.py` 用假 api 断言"发出去的 RPC 名与
    编码逐字节不变"）—— 步骤 3 的"行为不变"因此有证据，而不是只靠"跑得动"；
  * 好处二：`WRITE_PRIMITIVES` / `READ_PRIMITIVES` 两张名字表，让"**这个模块只有读能力**"
    成为**可检查**的事实（步骤 4 的 lint 就用它 —— 见 `tests/test_ops_rules.py`）。

纪律（别破坏它）
================
1. **不许在这里调 `ProcessEvent`**：本模块只做"把请求编好交给 L0（frida script）"和"解释
   返回值"。真正的执行点在 `ops/agent.js.tpl` 里、并且只在**游戏线程**上跑（弯路 #30）。
2. **写与调用必须在同一次 JS 执行**（弯路 #19）：所以"写字段 + 提交"只有
   `write_then_call` / `write_then_call_keep` 两个出口，**不提供**"先 poke 再 call"的组合。
3. 写原语的名字必须登记进 `WRITE_PRIMITIVES`；只读的名字进 `READ_PRIMITIVES`。
   `tests/test_ops_rules.py` 用它断言只读模块不 import 写原语。
4. `settle` 收的是 **`api` 取值函数**而不是 api 对象本身：`frames=0` 时它**一次都不许碰
   frida**（否则"只等墙钟"会变成"顺手把进程 attach 了"）。
5. **编码一律与拆分前逐字相同**（`tests/test_ops_primitives.py` 拿假 api 逐字段对比）。
   唯一一处"看起来不一样但可证明等价"的是 `hex(int(x))`（老代码里 `hex(x)` 与 `hex(int(x))`
   两种写法混用）—— 现有全部调用点传的都是 `int`（`off()`/`find_fn()`/`board_actor_of()` 的
   结果），`int()` 对 `int` 是恒等 ⇒ 发出去的字符串一模一样；它的作用只是顺手接受
   `bool`/`enum` 这类 `__index__` 值。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

#: 写原语（会改目标进程内存 / 触发带副作用的调用）
WRITE_PRIMITIVES = (
    "poke",
    "poke_and_call0",
    "write_fields_then_call",
    "write_then_call",
    "write_then_call_keep",
    "write_then_calls",
    "write_and_call2",
)

#: 只读原语（发出去的都是纯查询；唯一的副作用是"游戏自己算出来的出参")
READ_PRIMITIVES = (
    "settle",
    "obj_alive",
    "call_ufunction",
    "call_ufunction_arr",
    "call0",
    "call_ptr",
    "call_i32",
    "call_out_u8",
    "call_out_u8_batch",
    "peek",
    "read_world_levels",
    "read_level_actors",
)

#: 指针一律走 hex 字符串：JS number 是 double，超过 2^53 的地址会被截断（老坑）。
PTR_RANGE = (0x10000, 0x7FFFFFFFFFFF)


def _ptr_arg(value) -> str:
    """指针 → hex 字符串（`write_then_call` / `call_ufunction_arr` 的公共编码）。"""
    return hex(int(value))


def parms_hex(parms) -> str:
    """参数缓冲 → hex 字符串。

    ★ 两种写法都收：`bytes` 走 `.hex()`；**已经是 hex 字符串的原样透传** —— 老调用点里
      `"00" * 0x18`（str）和 `(b"\\x00" * 0x10).hex()`（bytes→hex）混用了两种，
      抽原语时不改任何一个调用点的字面量（行为不变优先）。
    """
    if isinstance(parms, str):
        return parms
    if parms is None:
        return ""
    return bytes(parms).hex()


def encode_writes(writes) -> list:
    """`[(obj, off, kind, value), …]` → JS 侧的写清单；`ptr` 类值走 hex 字符串，其余走 int。

    ★ 与拆分前 `gesture.py:383` / `play.py:421` 那两处**内联编码逐字相同**（含 `f64` 也先
      `int(v)` —— 现有唯一的 f64 调用点是 `4000.0`，JS 侧同样 `writeDouble(4000)`
      ⇒ 写进内存的字节一模一样）。
    ★ 按**下标**取而不是解包：`write_then_call_keep` 的项可以带第 5 个元素 `hold`
      （拆分前那处内联代码就是 `w[0]`…`w[4]` 这么写的，解包会在 5 元组上直接 `ValueError`）。
    """
    return [[hex(int(w[0])), int(w[1]), w[2], (_ptr_arg(w[3]) if w[2] == "ptr" else int(w[3]))]
            for w in writes]


def encode_writes_mask(writes) -> list:
    """`encode_writes` + 第 5 项 `hold`（1 = **不还原**，0 = 还原）。

    ★ 为什么需要：两阶段"点目标"里箭头 `overCardID` 和卡对象 `targetOverride` 必须写进
      `GlobalMouseUp` 的**同一次执行**，而 `GlobalMouseUpBattle` 内部会**销毁箭头**
      ⇒ 箭头那几项不能再用（可能已回收的）指针去还原，卡对象那项必须还原（弯路 #17/#19）。
    """
    return [spec + [1 if (len(w) > 4 and w[4]) else 0]
            for w, spec in zip(writes, encode_writes(writes))]


def hex_ptrs(ptrs) -> list:
    return [hex(int(p)) for p in (ptrs or [])]


def as_bytes(out) -> Optional[bytes]:
    return bytes(out) if isinstance(out, (bytes, bytearray)) else out


# ------------------------------------------------------------------ 等待
def settle(api: Callable[[], object], seconds: float, frames: int = 0, cap: float = 4.0) -> None:
    """模拟输入里的"停留"：至少 `seconds` 墙钟，且（有帧计数时）游戏线程至少又跑了 `frames` 个
    PC tick。失焦时 UE 降帧，固定几十/几百毫秒里可能一帧都没跑（2026-10-01 攻击被吞的原因，弯路 #29）。
    `cap`：总等待上限（游戏卡住时别无限等）。没有帧计数就退回纯墙钟。

    ★ `api` 是**取值函数**（`self.api`），不是 api 对象：`frames=0` 时一个 frida 调用都不发。
    """
    t0 = time.time()
    n0 = None
    if frames > 0:
        try:
            n0 = int(api().gt_ticks())
            if n0 < 0:
                n0 = None
        except Exception:                                      # noqa: BLE001
            n0 = None
    time.sleep(max(0.0, seconds))
    if n0 is None:
        return
    while time.time() - t0 < cap:
        try:
            if (int(api().gt_ticks()) - n0) & 0xFFFFFFFF >= frames:
                return
        except Exception:                                      # noqa: BLE001
            return
        time.sleep(0.02)


# ------------------------------------------------------------------ 只读：指针判活
def obj_alive(m, oa, obj: int) -> bool:
    """这个 UObject 指针现在还是**活的**吗（只读）？

    2026-10-01 崩溃现场（转储 `…094945.dmp`，AV 在 `ProcessEvent+0x2DE`，访问 0x1e3200c98，
    调用栈上是我们的注入调用）：对**已被销毁/回收的候选 actor**（预报第二层点击时第一层的 actor 已被
    GC/销毁）调 `ProcessEvent` ⇒ 读它的垃圾虚表 ⇒ 访问冲突。
    判据：`InternalIndex@+0x0C` → GUObjectArray 的 `FUObjectItem`：`Object` 必须还指回它，
    `Flags@+8` 不带 Garbage(0x200000)，`ObjectFlags@+8` 不带 BeginDestroyed/FinishDestroyed
    (0x8000 / 0x10000)。读不出来一律当**不活**（宁可不点）。
    """
    try:
        if not obj or obj & 7:
            return False
        idx = m.u32(obj + 0x0C)
        item = oa.item_addr(int(idx))
        if not item:
            return False
        if (m.ptr_or_zero(item) or 0) != obj:
            return False
        if (m.u32(item + 8) or 0) & 0x200000:                     # Garbage
            return False
        if (m.u32(obj + 0x08) or 0) & (0x8000 | 0x10000):         # BeginDestroyed / FinishDestroyed
            return False
        return True
    except Exception:                                             # noqa: BLE001
        return False


# ------------------------------------------------------------------ 只读/写：调用
def call_ufunction(api, obj: int, func: int, parms=b"") -> Optional[bytes]:
    """**唯一的一条"进去调一次"的路**（L0 出口）：`ProcessEvent` 在游戏线程上跑（弯路 #30）。

    只传参数字节缓冲，不带任何写/引用数组 —— 需要那两样的一律走 `write_then_call*` /
    `call_ufunction_arr`（§3「L1 原语没有'直接 pe'的 API」）。
    """
    return as_bytes(api.call_raw(hex(obj), hex(func), parms_hex(parms)))


def call_ufunction_arr(api, obj: int, func: int, parms, arr_off: int,
                       ptrs) -> Optional[bytes]:
    """调一个函数，并在 `arr_off` 处塞一个**真实 TArray<UObject*>**（data 在目标进程里 alloc）。

    用来调 `cardsCheckFunctions::CanAttack` —— 它的 `cardsInAttackedLocation`
    是引用参数，必须由调用方组（防守方那一行/那一侧的卡对象）。
    """
    return as_bytes(api.callRawArr(hex(obj), hex(func), parms_hex(parms), int(arr_off),
                                  hex_ptrs(ptrs)))


def call0(api, obj: int, func: int) -> bool:
    return bool(api.call0(hex(obj), hex(func)))


def call_ptr(api, obj: int, func: int, arg_ptr: int) -> int:
    return int(api.call_ptr(hex(obj), hex(func), hex(arg_ptr)))


def call_i32(api, obj: int, func: int, value: int) -> int:
    return int(api.call_i32(hex(obj), hex(func), int(value)))


def call_out_u8(api, obj: int, func: int) -> Optional[int]:
    """调用一个"只有一个 bool 出参"的函数，返回那个出参。

    （ParmsSize=1，进去时是全 0 的缓冲；`ToggleSettingsMenu(bool* IsNowVisible)` 就是这种。）
    """
    b = call_ufunction(api, obj, func, b"\x00")
    return int(b[0]) if b else None


def call_out_u8_batch(api, items) -> list:
    """批量"只有一个 u8/bool 出参"的调用（JS 侧一次 RPC 里循环）。

    `items` = `[[objHex, funcHex, parmsHex], …]`；返回每个出参的列表。
    """
    return list(api.call_out_u8_batch(items))


def peek(api, obj: int, off: int, kind: str):
    return api.peek(hex(obj), off, kind)


# ------------------------------------------------------------------ 写原语
def write_then_call(api, writes, obj: int, func: int, parms: bytes,
                    arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
    """**临时写几个字段 → 调用 → 立刻还原**（同一次 JS 执行，弯路 #19）。

    `writes` = `[(obj, off, kind, value), …]`（同 `encode_writes`）。
    用来问那些"只在拖拽中间态里才有答案"的函数：`can_move_to(simulate_drag=True)` 靠它把
    `PlayerController->SelectedCard` 临时指到目标卡上，问完还回去。

    ★ `arr_off` 不给 = 这个函数**没有引用数组参数**，一个字节都不许碰 parms。
      踩过：`callRawArr` 那条路无条件在 `arrOff` 处写 TArray 头（16 字节），
      而 `CanMoveCardToLocation` 的 `Location@0x00` 正好在头里 ⇒ 被盖成 0。
    """
    return as_bytes(api.callRawArrHold(
        encode_writes(writes), hex(obj), hex(func), bytes(parms).hex(),
        -1 if arr_off is None else int(arr_off), hex_ptrs(ptrs)))


def write_then_call_keep(api, writes, obj: int, func: int, parms: bytes,
                         arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
    """**写若干字段 → 带参调用一次 → 按 mask 还原**（都在同一次 JS 执行里）。

    `writes` 同 `write_then_call`，但每项可以带第 5 个元素 `hold=True`
    表示"这一项**不要还原**"（见 `encode_writes_mask` 的理由）。
    """
    return as_bytes(api.writeCallRaw(
        encode_writes_mask(writes), hex(obj), hex(func), bytes(parms).hex(),
        -1 if arr_off is None else int(arr_off), hex_ptrs(ptrs)))


def write_fields_then_call(api, writes, call_obj: Optional[int] = None,
                           call_func: Optional[int] = None) -> list:
    """写字段（按 mask 还原）+ 可选的一次调用，返回回读值列表。"""
    return list(api.write_and_call(
        encode_writes(writes), hex(call_obj) if call_obj else None,
        hex(call_func) if call_func else None))


def poke(api, obj: int, off: int, kind: str, value) -> bool:
    # 指针按字符串传，避开 JS number 的精度边界
    return bool(api.poke(hex(obj), off, kind, (_ptr_arg(value) if kind == "ptr" else value)))


def write_then_calls(api, writes, calls) -> list:
    """写若干字段 → 依次调用若干函数（**带参/空参混合**），全在**同一次 JS 执行**里。

    `calls` 每项 `[objHex, funcHex, parmsHex]`；`parmsHex` 空 ⇒ 用 NULL 参数调。
    用途（攻击落地，弯路 #19）—— 必须一次做完的三件事：
      ① 写每个箭头的 `overCardID`；② 搬远箭头头部平面（`GetArrowLength()` 才会 >3000）；
      ③ 攻击者 actor 上 `OnActorMouseUp`。拆成两次 RPC，中间会插引擎帧
      ⇒ 箭头 tick 按真实鼠标把头部平面搬回去 ⇒ 又白做。
    返回**回读值**列表（JS 侧 `writeThenCalls`）。
    """
    return list(api.writeThenCalls(encode_writes(writes), calls))


def write_and_call2(api, writes, obj_a: int, func_a: int, obj_b: int, func_b: int) -> list:
    """写若干字段 → 依次调用**两个**空参函数，全在同一次 JS 执行里。

    用途：`BP_HandCard_C` 上同时有 `OnActorEndDrag`（出牌提交）和 `OnActorMouseUp`
    （看着像清 touched/回盘的收尾）—— 带目标出牌要一次把两跳都复刻出来。
    `obj_a`/`func_a` 传 0 就只调第二跳（JS 侧 `if (objA)` 判空）。返回回读值列表。
    """
    return list(api.writeAndCall2(encode_writes(writes), hex(obj_a), hex(func_a),
                                 hex(obj_b), hex(func_b)))


def poke_and_call0(api, obj: int, off: int, kind: str, value,
                   call_obj: int, call_func: int) -> bool:
    return bool(api.poke_and_call0(hex(obj), off, kind,
                                   (_ptr_arg(value) if kind == "ptr" else value),
                                   hex(call_obj), hex(call_func)))


# ------------------------------------------------------------------ 只读：找 actor（find_actors）
def read_world_levels(m, world: int, levels_off: int) -> list:
    """`UWorld::Levels // 0x01C8`（`TArray<ULevel*>`）→ 全部 level 指针（纯读，不含缓存）。

    实测（对局中）：Num=2 —— level[0] 是 PersistentLevel（191 个 actor：Board/板卡…），
    level[1] 是另一个（24 个：**`BP_Deck_C`×2 就在这里**）。
    ⇒ "当前对局挂了哪些东西"要**遍历全部 level**，只看 PersistentLevel 会漏掉牌库那类。
    """
    data = m.ptr(world + levels_off) if world else 0
    n = m.i32(world + levels_off + 8) if world else 0
    out = []
    if data and n and 0 < n < 256:
        raw = m.read(data, n * 8) or b""
        out = [int.from_bytes(raw[i * 8:i * 8 + 8], "little") for i in range(n)]
        out = [p for p in out if PTR_RANGE[0] <= p <= PTR_RANGE[1]]
    return out


def read_level_actors(m, oa, pool, levels, actors_off: int) -> tuple:
    """当前世界**全部 level** 的 actor：整读 + 一次批量分类（纯读，不含缓存）。

    实测：215 个 actor，整读 ~0.1ms、分类 ~5ms ⇒ **~5ms**；
    对比 `instances_of_class()` 每个类名一次 GObjects 全扫（12 万对象）**0.6~0.9s** ⇒ 约 **150×**。
    ★ 而且**更正确**：这里天然不含"上一局残留但已不属于任何 level"的对象。

    返回 `(actors, {类名: [actor, …]})` —— 后者就是"按类名找 actor"（find_actors）靠的东西。
    """
    out, by_cls = [], {}
    for lvl in levels:
        data = m.ptr(lvl + actors_off) or 0
        n = m.i32(lvl + actors_off + 8) or 0
        if not data or not n or n < 0 or n > 20000:
            continue
        raw = m.read(data, n * 8) or b""
        for i in range(n):
            a = int.from_bytes(raw[i * 8:i * 8 + 8], "little")
            if not (PTR_RANGE[0] <= a <= PTR_RANGE[1]):
                continue
            out.append(a)
            cn = pool.fname_of(oa.class_of(a) or 0)
            if cn:
                by_cls.setdefault(cn, []).append(a)
    return out, by_cls


def assert_name_tables_consistent() -> None:
    """自检：两张名字表不重叠、且都真的定义在本模块里（给 lint/测试用）。"""
    overlap = set(WRITE_PRIMITIVES) & set(READ_PRIMITIVES)
    if overlap:
        raise AssertionError("原语名字表重叠：%s" % sorted(overlap))
    missing = [n for n in WRITE_PRIMITIVES + READ_PRIMITIVES if n not in globals()]
    if missing:
        raise AssertionError("名字表里登记了但本模块没有：%s" % missing)
