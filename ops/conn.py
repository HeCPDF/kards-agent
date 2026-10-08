# -*- coding: utf-8 -*-
"""ops/conn.py —— 运行时与连接：Frida 管道、游戏线程调度、settle、反射缓存（类/函数/偏移）、字段读写、原始调用。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import atexit
import os
import struct
from typing import Optional

from kardsmem import attach, kismet, props
from kardsmem.objects import ObjectArray
from kardsmem.pick import player_controller
from ops import primitives as P
from ops.consts import (
    NOTE_OFF_CARD_TITLE, NOTE_OFF_UCLASS_CDO, NOTE_OFF_WIDGET_ENABLED_BYTE, NOTE_OFF_WIDGET_ENABLED_MASK,
)
from ops.support import _frida_tmp_setup, _load_persistent_cache, _save_persistent_cache, sweep_frida_tmp


class ConnMixin:
    """运行时与连接：Frida 管道、游戏线程调度、settle、反射缓存（类/函数/偏移）、字段读写、原始调用。"""

    def __init__(self, session=None, ks=None):
        self.ks = ks or attach(require_build=False)
        self.m = self.ks.m
        self.oa = ObjectArray(self.ks)
        self.pool = self.oa.pool()
        self._frida = None
        self._api = None
        self._logic = None
        self._board = None            # 单例缓存（GetBoard）；见 board_actor()
        self._deck = None
        self._nw = None              # 提示 watcher 缓存（notify_texts）；见 _notify_watcher()
        self._levels = None          # 当前世界全部 level（world_levels）
        self._actors = None          # 当前世界全部 level 的 actor（level_actors）
        self._actors_by_cls: dict = {}
        self._ml = None              # MatchLog 长寿命单例（见 matchlog()：locate 要 4~6 s）
        self._ml_located = False
        self._class_cache: dict = {}
        self._fn_cache: dict = {}
        self._off_cache: dict = {}
        self.notes: list = []
        cached = _load_persistent_cache(self.ks.pid)
        if cached:
            self._class_cache = cached["class_cache"]
            self._fn_cache = cached["fn_cache"]
            self._off_cache = cached["off_cache"]
            self.notes.append("跨脚本缓存命中（pid=%s）：class=%d fn=%d off=%d"
                              % (self.ks.pid, len(self._class_cache),
                                 len(self._fn_cache), len(self._off_cache)))
        atexit.register(self._save_persistent_cache)

    def _save_persistent_cache(self) -> None:
        _save_persistent_cache(self.ks.pid, self._class_cache, self._fn_cache, self._off_cache)

    # ------------------------------------------------------------ frida 管道
    def api(self):
        if self._api is None:
            _frida_tmp_setup()                               # 必须在 import frida 之前：agent 解压到专用目录
            sweep_frida_tmp()                                # 先清掉没人用的旧目录
            import frida                                     # 惰性：只有真要动手才需要
            self._frida = frida.attach(self.ks.pid)
            # ★ P7-S3b：按**调用时**的 RVA 渲染（attach 前的 resolve 结果才算数）。
            from ops.consts import render_js
            from kardsmem import build as _B
            script = self._frida.create_script(render_js())
            script.load()
            self._api = script.exports_sync
            self.notes.append("frida 脚本按当前 RVA 渲染：pe=0x%X gobjects=0x%X namepool=0x%X"
                              % (_B.RVA["UObject_ProcessEvent"], _B.RVA["GObjects"], _B.RVA["FNamePool"]))
            self.notes.append("frida attached pid=%s base=%s pe=%s"
                              % (self.ks.pid, self._api.base(), self._api.pe()))
            self._gt_install()
        return self._api

    def _gt_install(self) -> None:
        """把注入调用切到**游戏线程**上执行（见 JS 里"游戏线程调度"的注释）。
        游戏线程 = 拥有游戏窗口的线程（UE 在游戏线程上建窗）。装不上就如实记到 notes，
        行为退回旧的"在 frida 线程上直接调"。环境变量 `KARDS_GT_DISPATCH=0` 可关掉。"""
        import os
        if os.environ.get("KARDS_GT_DISPATCH", "1") == "0":
            self.notes.append("游戏线程调度：已被 KARDS_GT_DISPATCH=0 关闭")
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            pid = int(self.ks.pid)
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lp):
                p = wintypes.DWORD(0)
                tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(int(tid))
                return True
            user32.EnumWindows(_cb, 0)
            if not found:
                self.notes.append("游戏线程调度：找不到游戏窗口线程，未启用（退回 frida 线程直接调）")
                return
            tid = max(set(found), key=found.count)
            self._api.gt_install(tid)
            # 默认把执行点放到玩家控制器 tick（和真实输入同一阶段）；环境变量 KARDS_GT_FILTER=any 用任意点
            filt_note = "任意 ProcessEvent"
            try:
                pc = player_controller(self.ks)
                f_tick = self.find_fn(self.uclass_of_instance(pc), "ReceiveTick") if pc else 0
                if f_tick:
                    self._api.gt_set_tick_fn(hex(f_tick))        # 帧计数（按帧等待用）
                    if os.environ.get("KARDS_GT_FILTER", "pctick") != "any":
                        self._api.gt_set_filter(hex(f_tick))
                        filt_note = "PC ReceiveTick(0x%x)" % f_tick
            except Exception as e2:                                    # noqa: BLE001
                filt_note = "任意 ProcessEvent（取 PC tick 失败：%s）" % e2
            self.notes.append("游戏线程调度执行点：%s" % filt_note)
            got = self._api.gt_probe()
            ok = int(got) == int(tid)
            self.notes.append("游戏线程调度：已启用 tid=%s，探针在 tid=%s 上执行 %s"
                              % (tid, got, "✓" if ok else "✗（线程对不上！）"))
        except Exception as e:                                   # noqa: BLE001
            self.notes.append("游戏线程调度：安装失败，退回 frida 线程直接调：%s" % e)

    def settle(self, seconds: float, frames: int = 0, cap: float = 4.0) -> None:
        """模拟输入里的"停留"：至少 `seconds` 墙钟，且（有帧计数时）游戏线程至少又跑了 `frames` 个
        PC tick。失焦时 UE 降帧，固定几十/几百毫秒里可能一帧都没跑（2026-10-01 攻击被吞的原因）。
        `cap`：总等待上限（游戏卡住时别无限等）。没有帧计数就退回纯墙钟。
        ★ 实现已抽到 L1 原语 `ops/primitives.settle`（步骤 3）；这里只转发（含 `self.api` 取值函数）。"""
        return P.settle(self.api, seconds, frames, cap)

    def close(self):
        if self._frida is not None:
            try:
                self._frida.detach()
            except Exception:                                # noqa: BLE001
                pass
            sweep_frida_tmp(min_age_s=0.0)                   # detach 后立刻回收自己留下的（被占用的会自动跳过）
        self._frida, self._api = None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 解析
    def uclass_of_instance(self, obj: int) -> int:
        return self.oa.class_of(obj) or 0

    def class_named(self, name: str) -> int:
        """按类名找**该类的对象**（运行时，不依赖任何写死的地址）——**跨脚本缓存**：
        `UObject` 在目标进程存活期内地址不变，命中缓存就不用扫一遍 GObjects。

        ⚠ 名字骗人：它返回的**不是 UClass 对象**，而是"**class_of 的 fname == name** 的
          某个对象"（`scan_classes(skip_cdo=False)`，所以 CDO 和活实例都算）。
          ⇒ 要 UClass 请用 `self.oa.class_of(结果)`；要 CDO 请用 `class_cdo(name)`。
          要"该类的唯一活实例"（不想要 CDO）用 `instance_of_class(name)`。
        """
        if name in self._class_cache:
            return self._class_cache[name]
        rows = self.scan_classes([name], skip_cdo=False).get(name, [])
        if rows:
            self._class_cache[name] = rows[0]
            return rows[0]
        return 0

    def dump_props(self, obj: int, own_only: bool = False, limit: int = 160) -> dict:
        """只读：把一个 UObject 的反射属性里的**标量**字段读成 {名字: 值}（Bool/整数/浮点/枚举/指针是否为空）。
        用来对比"攻击成功 vs 失败"时箭头 actor 上哪个字段不同（2026-10-01：真鼠标放在目标上就能打）。"""
        from kardsmem import props
        res = {}
        try:
            cls = self.uclass_of_instance(obj)
            rows = props.struct_props(self.ks, cls, self.pool) if own_only else props.class_props(self.ks, cls, self.pool)
            for r in rows[:limit * 4]:
                nm, ty, off, sz = r.get("name"), r.get("type"), r.get("offset"), r.get("size")
                if not nm or off is None or off < 0 or (r.get("array_dim") or 1) != 1:
                    continue
                try:
                    if ty == "BoolProperty":
                        v = self.m.u8(obj + off)
                        v = bool((v or 0) & (r.get("bit_byte_mask") or 1))
                    elif ty in ("IntProperty",):
                        v = self.m.i32(obj + off)
                    elif ty in ("ByteProperty", "EnumProperty") and sz == 1:
                        v = self.m.u8(obj + off)
                    elif ty == "FloatProperty":
                        v = round(struct.unpack("<f", struct.pack("<I", self.m.u32(obj + off) or 0))[0], 3)
                    elif ty == "DoubleProperty":
                        v = round(struct.unpack("<d", struct.pack("<Q", self.m.u64(obj + off) or 0))[0], 3)
                    elif ty in ("ObjectProperty", "ClassProperty", "WeakObjectProperty") and sz == 8:
                        v = "ptr" if (self.m.ptr_or_zero(obj + off) or 0) else "null"
                    else:
                        continue
                except Exception:                                  # noqa: BLE001
                    continue
                res[nm] = v
                if len(res) >= limit:
                    break
        except Exception as e:                                     # noqa: BLE001
            res["__err"] = str(e)
        return res

    def obj_alive(self, obj: int) -> bool:
        """这个 UObject 指针现在还是**活的**吗（只读）？

        2026-10-01 崩溃现场（转储 `…094945.dmp`，AV 在 `ProcessEvent+0x2DE`，访问 0x1e3200c98，
        调用栈上是我们的注入调用）：对**已被销毁/回收的候选 actor**（预报第二层点击时第一层的 actor 已被
        GC/销毁）调 `ProcessEvent` ⇒ 读它的垃圾虚表 ⇒ 访问冲突。
        判据：`InternalIndex@+0x0C` → GUObjectArray 的 `FUObjectItem`：`Object` 必须还指回它，
        `Flags@+8` 不带 Garbage(0x200000)，`ObjectFlags@+8` 不带 BeginDestroyed/FinishDestroyed
        (0x8000 / 0x10000)。读不出来一律当**不活**（宁可不点）。
        ★ 实现已抽到 L1 原语 `ops/primitives.obj_alive`（步骤 3）。"""
        return P.obj_alive(self.m, self.oa, obj)

    def instance_of_class(self, name: str, skip_cdo: bool = True) -> int:
        rows = self.scan_classes([name], skip_cdo=skip_cdo).get(name, [])
        return rows[0] if rows else 0

    def find_fn(self, uclass: int, name: str, inherited: bool = True) -> int:
        key = (uclass, name, inherited)
        if key not in self._fn_cache:
            f = kismet.find_function(self.ks, uclass, name, inherited=inherited)
            self._fn_cache[key] = f or 0
        return self._fn_cache[key]

    def fn(self, uclass: int, name: str, inherited: bool = True) -> int:
        f = self.find_fn(uclass, name, inherited)
        if not f:
            raise RuntimeError("UFunction 找不到：%s（类 0x%X）" % (name, uclass))
        return f

    def off(self, uclass: int, name: str, note: int) -> int:
        """反射链现算字段偏移；算不出来退回备注常量并记一笔。"""
        key = (uclass, name)
        if key not in self._off_cache:
            pr = props.find_prop(self.ks, uclass, name, pool=self.pool)
            if pr:
                self._off_cache[key] = pr["offset"]
            else:
                self._off_cache[key] = note
                self.notes.append("字段 %s 反射链没算出来，退回备注 0x%X" % (name, note))
        return self._off_cache[key]

    # ------------------------------------------------------------ 原语
    # ★ 实现全部在 `ops/primitives.py`（L1）；这里只是"把 self 上的句柄喂进去"的转发层
    #   （步骤 3）。方法名/签名保持不变 —— `agent/` 与脚本在用（`ops/inject.py` 门面）。
    def call0(self, obj: int, func: int) -> bool:
        return P.call0(self.api(), obj, func)

    def call_ptr(self, obj: int, func: int, arg_ptr: int) -> int:
        return P.call_ptr(self.api(), obj, func, arg_ptr)

    def call_i32(self, obj: int, func: int, value: int) -> int:
        return P.call_i32(self.api(), obj, func, value)

    def poke_and_call0(self, obj: int, off: int, kind: str, value,
                       call_obj: int, call_func: int) -> bool:
        return P.poke_and_call0(self.api(), obj, off, kind, value, call_obj, call_func)

    def write_and_call(self, writes, call_obj: Optional[int] = None,
                       call_func: Optional[int] = None) -> list:
        """writes = [(obj, off, kind, value), ...]；回读值列表。"""
        return P.write_fields_then_call(self.api(), writes, call_obj, call_func)

    def poke(self, obj: int, off: int, kind: str, value) -> bool:
        return P.poke(self.api(), obj, off, kind, value)

    def peek(self, obj: int, off: int, kind: str):
        return P.peek(self.api(), obj, off, kind)

    def widget_in_viewport(self, w: int) -> Optional[bool]:
        """`UUserWidget::IsInViewport()`（原生 const，纯读）——**判断一个 widget 是不是当前这一屏的**。

        ★ 2026-09-25 实机坐实的坑：上一局（对手投降）结束后重开一局，
          `BP_Widget_Battle_HUD_PC_C` 在进程里有 **2 个活实例**：
            0x2317C8BC580  `IsInViewport()`=0  ← 上一局的残留（`hud_actor()` 挑中的就是它）
            0x23183497510  `IsInViewport()`=1  ← 这一局的
          ⇒ `end_of_turn()` 点的是**上一局那个 HUD 的按钮**，动作流零新增、
            回合一直不结束（症状像"注入坏了"或"卡住了"）。
          `GetOwningPlayer()` 两者都指向同一个活 PlayerController（区分不出来），
          `Visibility` 也有别的取值混淆 —— 只有 `IsInViewport()` 干净利落。
        """
        cls = self.uclass_of_instance(w)
        if not cls:
            return None
        f = self.find_fn(cls, "IsInViewport", inherited=True)
        if not f:
            return None
        v = self.call_out_u8(w, f)
        return None if v is None else bool(v)

    def _read_fstring(self, addr: int) -> Optional[str]:
        """FString {wchar*@+0, int32 num@+8} → str（num<=1 视为空）。"""
        p = self.m.ptr(addr) or 0
        num = self.m.i32(addr + 8)
        if not p or not num or num <= 1 or num > 4096:
            return None
        raw = self.m.read_exact(p, num * 2)
        return raw.decode("utf-16-le", "replace").rstrip("\x00") if raw else None

    # ------------------------------------------------ 单例（**不扫实例**；用户 2026-09-26 定调）
    # 用户原话："不要扫实例，因为多 live 实例的问题几乎无解。相反，跟踪这样的单例。"
    # 起因是两个真实事故：① 上一局残留的 HUD 被 `instances_of_class()` 挑中，点到了
    # **上一局的 EndTurnButton**；② `picklist` 列出**已结算的残留候选 actor**（同一类
    # 6 个 live 实例 = 2 层 × 3）。GObjects 全量扫**天然分不清"当前这一局的"**。
    # ⇒ 能用**游戏自己的访问器**就用它（`UUtilityFunctions_C::GetLogic/GetBoard/GetGameState/
    # GetDSession/GetLevelManager`），它们返回的就是"当前这一局那一个"。
    def _util_out(self, fn_name: str, wc_off: int = 0x00, out_off: int = 0x08,
                  extra: Optional[list] = None) -> int:
        """调 `UUtilityFunctions_C::<fn_name>`（游戏自己的**单例访问器**）拿指针。

        签名形如 `static void GetLogic(UObject* __WorldContext, ABP_Logic_C** Logic)`
        ⇒ ParmsSize 0x10：`__WorldContext@0x00`、出参 `@0x08`。
        ★ `__WorldContext` 传 **GWorld**（`self.ks.world`）：本项目的硬规矩 ——
          **凡是收 `__WorldContext` 的函数都不许传 0**（2026-09-26 用户定调；
          传 0 时这些库函数取不到 world，会静默回空）。
        `extra` = [(off, "ptr"|"s32", value)]，给参数在出参前的情形（如
        `GetBPPlayerController(Player0@0x00, __WorldContext@0x08, out@0x10)`）。
        """
        cls, cdo, f = self._bp_lib_fn("UtilityFunctions_C", fn_name)
        if not f:
            return 0
        parms = bytearray(0x18)
        struct.pack_into("<Q", parms, wc_off, int(self.ks.world or 0))
        for off, kind, val in (extra or []):
            if kind == "ptr":
                struct.pack_into("<Q", parms, off, int(val))
            else:
                struct.pack_into("<i", parms, off, int(val))
        try:
            out = P.call_ufunction(self.api(), cdo, f, bytes(parms))
        except Exception as e:                                     # noqa: BLE001
            self.notes.append("%s 调用异常：%s" % (fn_name, e))
            return 0
        buf = bytes(out) if out else b""
        return struct.unpack_from("<Q", buf, out_off)[0] if len(buf) >= out_off + 8 else 0

    def singleton(self, which: str) -> int:
        """按名字拿**当前这一局的单例**（不扫实例）。`which ∈ {logic,board,game_state,session,level}`。"""
        table = {"logic": "GetLogic", "board": "GetBoard", "game_state": "GetGameState",
                 "session": "GetDSession", "level": "GetLevelManager"}
        fn = table.get(which)
        return self._util_out(fn) if fn else 0

    def class_cdo(self, class_name: str) -> int:
        """按类名拿它的 **CDO**（`UClass::ClassDefaultObject // 0x110`，SDK dump 核实）。

        用途：卡类 CDO 上就摆着 `title`（FText@0x58）—— 这就是用户说的"标题表内存里能读"。

        ⚠ 先前写成 `class_named(name) + 0x110` —— **错的**。`class_named()`（= `scan_classes`
          按 `UObject::Class` 的**名字**匹配）返回的是"**该类的某个对象**"（含 CDO 自己），
          不是 UClass；`+0x110` 于是落在普通字段上，实测恒 0 ⇒ `card_title()` 永远 None，
          还被 `except` 吞成一个看起来像"内存里读不到"的假象。正确跳法是**两跳**：
          该类对象 →（`UObject::Class`，`oa.class_of`）它的 UClass →（`+0x110`）CDO。
        """
        obj = self.class_named(class_name)
        if not obj:
            return 0
        ucls = self.oa.class_of(obj)
        if not ucls:
            return 0
        return self.m.ptr_or_zero(ucls + NOTE_OFF_UCLASS_CDO)

    def card_title(self, internal_name: str) -> Optional[str]:
        """**内部名 → 卡面标题**（纯内存读，不查静态导出表）。

        路径：`card_event_storm2_thunderstorm3` →（补 `_C`）`class_named` → CDO
        → `title`（FText @ `NOTE_OFF_CARD_TITLE` = 0x58）→ `kardsmem.names.ftext_at`。
        ★ 用户 2026-09-26 原话"标题表内存测能读" —— 说的就是这条：标题本来就是卡对象/CDO
          上的一个 FText 属性，不需要维护任何"内部名→标题"的静态映射。
        ★ 同一天：**卡类/CDO 运行时不会动**（用户原话），所以按类名拿 CDO 读就行，
          不需要去扫实例、更不需要缓存"哪个实例才是它"。
        ⚠ 曾经整条链回 `None` 的真因不是"读法不对"，是 `OFF_CARD_TITLE` **没定义**
          （`NameError` 被下面 `except` 吞了）—— `except` 宽泛捕获会把"代码 bug"伪装成
          "内存里没有"，这类地方要么早返回、要么别吞。
        """
        if not internal_name:
            return None
        from kardsmem.names import ftext_at
        cls_name = internal_name if internal_name.endswith("_C") else internal_name + "_C"
        # ① 先试 CDO（UClass::ClassDefaultObject//0x110 → title@0x58）：**不用扫**
        cdo = self.class_cdo(cls_name)
        if cdo:
            try:
                t = ftext_at(self.m, cdo, NOTE_OFF_CARD_TITLE)
                if t:
                    return t
            except Exception:                                      # noqa: BLE001
                pass
        # ② 退路：这个**精确类名**的一个活实例（游戏为候选 spawn 出来的非可视卡对象，
        #    实测存在）。注意这跟"扫实例选一个"不同 —— 类名是**权威待选集**给出来的，
        #    这里只是"按名字解析对象"，不是在多个候选里挑。
        obj = self.instance_of_class(cls_name)
        if not obj:
            return None
        try:
            return ftext_at(self.m, obj, NOTE_OFF_CARD_TITLE)
        except Exception:                                          # noqa: BLE001
            return None

    def _bp_lib_fn(self, class_name: str, fn_name: str):
        """在**同名 UClass 里挑真那个**，返回 (class, cdo_obj, func)。

        ⚠ 不能用 `class_named()`：它会把同名的**陈旧 UClass** 给你
        （实测 `BattleUtilityFunctions_C` 有 2 个同名类，陈旧那个自身只有 1 个 UFunction、
        连函数名都解不出来）。判据：**哪个类的自身 UFunction 列表里有这个函数**。

        ★★ `cdo_obj` 是**函数库自己的默认对象（CDO）**，调用时 `self` 必须用它，
        **不是** UClass、**也不是**某张卡 —— `agent/legality._locate()` 的注释早就写了：
        这些函数体里有 `EX_LocalVirtualFunction`（对 self 按名调用），
        self 给错会停在"函数 X 找不到"，甚至崩/卡（2026-09-25 实测：
        把 UClass 当 self 调 `CanAttack` 时，19→54 侥幸返回、27→54 直接卡死）。
        """
        key = ("_bp_lib", class_name, fn_name)
        if key in self._fn_cache:
            return self._fn_cache[key]
        got = (0, 0, 0)
        for p in self.oa.iter_objects(skip_cdo=False):
            c = self.oa.class_of(p)
            if not c:
                continue
            try:
                if self.pool.fname_of(c) != class_name:
                    continue
            except Exception:                                     # noqa: BLE001
                continue
            for f in kismet.functions(self.ks, c):
                if f["name"] == fn_name:
                    got = (c, p, f["addr"])                       # p = 这个类的对象/CDO
                    break
            if got[2]:
                break
        if got[2]:                      # 不缓存"没找到"：否则一次早期 miss（世界没起来）会把它永久钉死，且每次都白扫
            self._fn_cache[key] = got
        return got

    def instances_of_class(self, name: str, skip_cdo: bool = True,
                           cache: Optional[dict] = None) -> list:
        """该类**所有**活实例。

        ⚠ 有些 UI 类（比如 `Battle_Settings_Widget_C`）在进程里同时躺着好几个实例
        ——旧比赛留下的那个 `EndMatch_Surrender` 是空指针，挑错了就会"点了没反应"。

        ★ 2026-09-25：`GObjects` 实测涨到 12 万+ 对象后，单次全量扫描（遍历+查类名）
          要 4~10s——牌组页一套"选模式→选牌组→点开始"下来，若每一步各扫一遍，
          光扫描就能吃掉 30+ 秒，比换牌窗口本身还长（症状：每次都刚好错过窗口）。
          `cache` 传 `scan_classes()` 的结果就能把同一屏幕里的好几次查找**并成一次扫描**。
        """
        if cache is not None:
            return cache.get(name, [])
        return self.scan_classes([name], skip_cdo=skip_cdo).get(name, [])

    def scan_classes(self, names, skip_cdo: bool = True) -> dict:
        """**一次** `GObjects` 全量扫描，同时归类出多个类名各自的活实例列表。

        比对每个类名各调一次 `instances_of_class()` 快 N 倍（N=类名个数）——
        代价只有一次遍历，不是 N 次。用于"同一屏幕上要问好几个类"的场景
        （牌组页：模式列表 + 牌组按钮 + 侧栏，三者同时存在）。

        ★ 2026-09-25：**扫描本体挪进 Frida，在目标进程里执行**（`scanClassesNative`）。
        Python 这边原来的写法每个对象至少 3 次跨进程 `ReadProcessMemory`
        （flags/class/类名），12 万+对象实测 4~10s；同样的指针解引用发生在目标
        进程自己的地址空间里就是纯内存访问，没有系统调用开销。Frida 崩了/还没连上
        就退回 Python 版的全量遍历（正确性兜底，不会因为这个新路径整个用不了）。
        """
        names = list(names)
        try:
            out = self.api().scan_classes(names, skip_cdo)
            if isinstance(out, dict) and "error" not in out:
                return {n: [int(x, 16) for x in out.get(n, [])] for n in names}
        except Exception as e:                                # noqa: BLE001
            self.notes.append("scan_classes：Frida 原生扫描失败，退回 Python 版（%s）" % e)
        want = set(names)
        out = {n: [] for n in want}
        for p in self.oa.iter_objects(skip_cdo=skip_cdo):
            c = self.oa.class_of(p)
            if not c:
                continue
            n = self.pool.fname_of(c)
            if n in want:
                out[n].append(p)
        return out

    def call_raw_arr(self, obj: int, func: int, parms: bytes, arr_off: int,
                     ptrs) -> Optional[bytes]:
        """调一个函数，并在 `arr_off` 处塞一个**真实 TArray<UObject*>**（data 在目标进程里 alloc）。

        用来调 `cardsCheckFunctions::CanAttack` —— 它的 `cardsInAttackedLocation`
        是引用参数，必须由调用方组（防守方那一行/那一侧的卡对象）。
        ★ 实现已抽到 L1 原语 `ops/primitives.call_ufunction_arr`（步骤 3）。
        """
        return P.call_ufunction_arr(self.api(), obj, func, parms, arr_off, ptrs)

    def call_raw_hold(self, writes, obj: int, func: int, parms: bytes,
                      arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
        """**临时写几个字段 → 调用 → 立刻还原**（同一次 JS 执行）。

        `writes` 同 `write_and_call` 的格式 `[[objHex, off, kind, value], …]`。
        用来问那些"只在拖拽中间态里才有答案"的函数：`can_move_to` 靠它把
        `PlayerController->SelectedCard` 临时指到目标卡上，问完还回去。

        ★ `arr_off` 不给 = 这个函数**没有引用数组参数**，一个字节都不许碰 parms。
          踩过：`callRawArr` 那条路无条件在 `arrOff` 处写 TArray 头（16 字节），
          而 `CanMoveCardToLocation` 的 `Location@0x00` 正好在头里 ⇒ 被盖成 0。
        ★ 实现已抽到 L1 原语 `ops/primitives.write_then_call`（步骤 3）。
        """
        return P.write_then_call(self.api(), writes, obj, func, parms, arr_off, ptrs)

    def call_raw_writes(self, writes, obj: int, func: int, parms: bytes,
                        arr_off: Optional[int] = None, ptrs=()) -> Optional[bytes]:
        """**写若干字段 → 带参调用一次 → 按 mask 还原**（都在同一次 JS 执行里）。

        `writes` 同 `call_raw_hold`，但每项可以带第 5 个元素 `hold=True`
        （JS 里表现为 1）表示"这一项**不要还原**"。

        ★ 为什么需要这个变体：两阶段"点目标"（`select_unit_target`）里，
          箭头 `overCardID` 和卡对象 `targetOverride` 必须在
          `BP_Logic_C::GlobalMouseUp` 的**同一次执行**里写进去（箭头 tick 每帧按真实
          鼠标重算）；而 `GlobalMouseUpBattle` 内部会**销毁箭头**
          ⇒ 箭头那几项不能再用（可能已回收的）指针去还原，卡对象那项必须还原。
        ★ 实现已抽到 L1 原语 `ops/primitives.write_then_call_keep`（步骤 3）。
        """
        return P.write_then_call_keep(self.api(), writes, obj, func, parms, arr_off, ptrs)

    def call_out_u8(self, obj: int, func: int) -> Optional[int]:
        """调用一个"只有一个 bool 出参"的函数，返回那个出参。

        （ParmsSize=1，进去时是全 0 的缓冲；`ToggleSettingsMenu(bool* IsNowVisible)` 就是这种。）
        ★ 实现已抽到 L1 原语 `ops/primitives.call_out_u8`（步骤 3）。
        """
        return P.call_out_u8(self.api(), obj, func)

    def widget_enabled(self, widget: int) -> Optional[bool]:
        """`UWidget::bIsEnabled` 是**位域**：`0x00D9` 那一字节的 **bit 2**（SDK: BitIndex 0x02）。

        ⇒ 判断按钮是不是"灰的"就读这一位（别只看 Visibility —— 灰按钮照样 Visible）。
        """
        b = self.peek(widget, NOTE_OFF_WIDGET_ENABLED_BYTE, "u8")
        if b is None:
            return None
        return bool(b & NOTE_OFF_WIDGET_ENABLED_MASK)

    def delegate_num(self, obj: int, off: int) -> int:
        """多播委托（`TArray<FScriptDelegate>`）的订阅者数：`Data`@+0、`Num`@+8。

        ★ handoff §12.6：判断"这个 UI 到底有没有接线"，读 `Num` 比看 `bIsEnabled`/
        `Visibility` 硬——同名控件的陈旧实例（旧屏幕残留、还没被销毁的那一份）
        订阅者表通常是空的（`Num=0`），可点、可见，点了却没人接。用这个当"活实例"判据。
        """
        if not obj:
            return 0
        return self.peek(obj, off + 8, "s32") or 0

    def delegate_target(self, obj: int, off: int):
        """多播委托（`TArray<FScriptDelegate>`）**第一项**的 `(订阅者对象, 函数名)`；空表/解析不出 ⇒ `None`。

        `FScriptDelegate` 在 UE5 = `{TWeakObjectPtr<UObject>{ObjectIndex@+0, Serial@+4}, FName{index@+8, num@+12}}`。
        弱引用必须拿 `FUObjectItem.SerialNumber`（`+0x10`）核对 —— 槽位会被 GC 复用，只按索引取会指到
        "同类同名的另一个对象"。
        ★ 2026-10-03（主界面模式切换）：真正生效的点击 = **调订阅者的这个函数**；按钮自身那套
          K2 句柄在 launcher 1.60 上改不动模式（实测见 `flow.select_mode_by_label`）。
        """
        if not obj:
            return None
        data = self.m.ptr_or_zero(obj + off) or 0
        num = self.m.i32(obj + off + 8) or 0
        if not data or num <= 0:
            return None
        oi, osn = self.m.u32(data) or 0, self.m.u32(data + 4) or 0
        fi, fn = self.m.u32(data + 8) or 0, self.m.u32(data + 12) or 0
        sub = 0
        n = self.oa.count() or 0
        if 0 <= oi < n:
            it = self.oa.item_addr(oi)
            if it and (self.m.u32(it + 0x10) or 0) == osn:
                sub = self.oa.at(oi) or 0
        name = None
        try:
            name = self.pool.fname(fi, fn)
        except Exception:                                          # noqa: BLE001
            name = None
        if not sub or not name:
            return None
        return sub, name
