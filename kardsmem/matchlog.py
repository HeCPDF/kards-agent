#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.matchlog —— **对局动作流**（双方的每一个动作，带结构化参数）。

为什么它比提示文本好得多
========================
先前把「历史」压在 `notify` 上（读屏幕提示文本）。那条路有三个硬伤：

  1. **有损**：提示是给人看的横幅，不是每个动作都弹。
  2. **短命**：widget 淡出就销毁，事后捞不回来 ⇒ 必须一直轮询，漏了就没了。
  3. **没有结构**：只有一句话，谁对谁做了什么要靠猜。

而客户端自己就留着一份**结构化的动作流**（2026-09-23 用户指出：
「本地有一套处理 action 队列的机制，可以发现对面的动作和自己的动作」）：

    BP_OnlineMatch_C
        TArray<FAction2> actionsQueue        // 0x0318  待处理（还没演完）
        TArray<FActionValue2> currentActionValues  // 0x0640  正在演的那个的参数
        TArray<FAction2> AllMatchActions     // 0x0678  ★ 整局全部动作（双方）
        TArray<FAction2> myMatchActions      // 0x0858  只有我方的

`AllMatchActions` 是**累积**的 ⇒ 不用高频轮询，随时读随时有，
连"开局到现在发生过什么"都能补齐。**这才是 F5（历史）该走的路。**

结构（Dumper-7 SDK 逐字段）
===========================
    FAction2 (0x40)
        int32   action_id    @0x00
        FString action_type  @0x08     "playCard" / "attack" / ... 的字符串
        int32   player_id    @0x18     ★ 哪一方做的
        TArray<FActionValue2> action_data  @0x20
        TArray<FSubAction>    sub_actions  @0x30

    FActionValue2 (0x28)              ── 一个键值对
        FString Name  @0x00
        int32   Value @0x10
        FString Text  @0x18

    FSubAction (0x20)
        FString Name @0x00
        TArray<FActionValue2> Values @0x10

★ 两侧的记法**不一样**（2026-09-23 实机观察，还没找到成因）
==========================================================
对手的动作（来自服务器）**字段有名字**，`action_id` 是真的递增序号：

    #63  <对手id>  XActionAttackCard       attackerCardID=54 defenderCardID=10002
    #64  <对手id>  XActionPlayCardFromHand cardID=56 location=board_hqright
                                           locationNumber=2 enterPlayOnTurn=15
                                           card_name=card_unit_valentine_mk_iii

我方的动作**字段名是数字**，且 `action_id` 一律是 `-1`：

    #-1  <本地id>  XActionAttackCard       0=20 1=6004 2=yt 3=17 14=26
    #-1  <本地id>  XActionPlayCardFromHand 0=23 1=0 2=0 3=0 4=bP 14=26

**成因（2026-09-23 用户确认）**：**我方动作是本地上传的，上传时 id 用 `-1`；
对手的动作是从服务器拉回来的内容**，所以才带着服务器分配的序号和具名字段。
⇒ 这个不对称是设计如此，不是读错了。

两侧的编码方式（实机逐字段确认）：

    对手：{"name":"attackerCardID","value":61,"text":null}    ← 具名 + 按类型放
    我方：{"name":"0",             "value":-1,"text":"20"}    ← 字段号 + 全部字符串化

⇒ 我方那侧是**上传用的序列化载荷**，对手那侧是**反序列化后的服务器响应**。

`XActionAttackCard`（我方侧）字段号已全部对出来（规格 §7.6g 有依据表）：

    0 = attackerCardID   1 = defenderCardID
    2 = 攻击方卡组代码    3 = 防守方卡组代码      14 = ？（恒为 "26"）

★ `3="17"` 曾被误读成"回合数"。它是**恰好全是数字的卡组代码字符串**。
  `FActionValue2` 的 `Value` 和 `Text` 必须分开看 —— `text="17"` 不是整数 17。
★ 字段号**按 `action_type` 分配**：攻击里卡组代码在 `2`/`3`，出牌里在 `4`。
  别套通用公式，逐个 `action_type` 对（§11.1 F5b）。

⇒ 现在**要判断「对手做了什么」，用对手那一侧就够了，字段都是带名字的**。

红线
====
只读。全程 `ReadProcessMemory`。
"""
from __future__ import annotations

import struct
from typing import Optional

MATCH_CLASS = "BP_OnlineMatch_C"

# 本构建实测偏移，**仅作备注** —— 运行时一律走反射链（§4.13）。
OFF_ACTIONS_QUEUE = 0x0318
OFF_CURRENT_VALUES = 0x0640
OFF_ALL_ACTIONS = 0x0678
OFF_MY_ACTIONS = 0x0858

SZ_ACTION = 0x40
SZ_VALUE = 0x28
SZ_SUBACTION = 0x20

# TArray<T>: Data@+0x00(8) Num@+0x08(4) Max@+0x0C(4)
MAX_ELEMS = 4096            # 护栏：读到离谱的 Num 就当读坏了，别去申请几个 G


def _tarray(mem, addr: int, stride: int, cap: int = MAX_ELEMS):
    """→ (data_ptr, num, 整块字节)。读不出/不合理返回 (0, 0, None)。**不抛**。"""
    head = mem.read_exact(addr, 0x10)
    if not head:
        return 0, 0, None
    data, num, _max = struct.unpack("<QiI", head)
    if not data or num <= 0 or num > cap:
        return 0, 0, None
    return data, num, mem.read(data, num * stride)


def _fstring(mem, addr: int) -> Optional[str]:
    """内联 `FString{TCHAR* Data; int32 Num; int32 Max}` → str。"""
    head = mem.read_exact(addr, 0x10)
    if not head:
        return None
    p, n, _ = struct.unpack("<QiI", head)
    if not p or n <= 0 or n > 8192:
        return None
    b = mem.read_exact(p, n * 2)
    if not b:
        return None
    return b.decode("utf-16-le", "replace").rstrip("\x00") or None


def _values(mem, addr: int) -> list:
    """`TArray<FActionValue2>` → [{name, value, text}]。"""
    data, num, _ = _tarray(mem, addr, SZ_VALUE)
    out = []
    for i in range(num):
        a = data + i * SZ_VALUE
        out.append({"name": _fstring(mem, a),
                    "value": mem.i32(a + 0x10),
                    "text": _fstring(mem, a + 0x18)})
    return out


def _sub_actions(mem, addr: int) -> list:
    data, num, _ = _tarray(mem, addr, SZ_SUBACTION)
    out = []
    for i in range(num):
        a = data + i * SZ_SUBACTION
        out.append({"name": _fstring(mem, a), "values": _values(mem, a + 0x10)})
    return out


def _field_value(row: dict, name: str):
    """按字段名字（`data` 里 `FActionValue2.name`，形如 `"0"`/`"1"`）取 `value`。

    没有这个字段就返回 `None`——调用方（`receipt(..., card_id=...)`）拿它跟
    `card_id` 比较，`None == card_id` 恒假，天然当"没匹配上"处理，不用额外判空。
    """
    for v in row.get("data") or []:
        if v.get("name") == name:
            return v.get("value")
    return None


def read_action(mem, a: int) -> dict:
    """一个 `FAction2` → dict。"""
    return {
        "action_id": mem.i32(a + 0x00),
        "action_type": _fstring(mem, a + 0x08),
        "player_id": mem.i32(a + 0x18),
        "data": _values(mem, a + 0x20),
        "sub_actions": _sub_actions(mem, a + 0x30),
    }


class MatchLog:
    """对局动作流的读取器。只读。

        ml = MatchLog(session)
        ml.all()          # 整局所有动作（双方）
        ml.since(n)       # 第 n 个之后的（增量拿新动作）
        ml.queue()        # 还没演完的
    """

    def __init__(self, session):
        self.s = session
        self.m = session.m
        self._obj = None          # BP_OnlineMatch_C 实例；False = 找过没有
        self._off = {}
        self._cls = None          # BP_OnlineMatch_C 的 UClass（my_side() 用）
        self._my_side_cache = None
        self._last_scan_fail_t = 0.0   # 上次扫空的时间戳，给失败重试限速用
        self._last_count = None        # 上次读到的 `AllMatchActions` 长度（单调性判据，见 is_stale()）

    # ---- 新鲜度（"怎么保证是最新的"）----
    @property
    def obj(self) -> int:
        """当前缓存的 `BP_OnlineMatch_C` 指针（0 = 还没定位）。"""
        return self._obj or 0

    def pin(self, obj: int) -> int:
        """**外部权威链给的对象指针，直接采用**（`Injector` 走 `GetLogic() → onlineMatch`）。

        ★ 2026-09-26 用户问："那么怎么确保这个东西是最新的呢？"
        答案分两半：
          * **对象指针：每次现算，绝不长期缓存** —— 它可以从权威单例链上现推
            （`BP_Logic_C::onlineMatch // 0x0810`，`BP_Logic_classes.hpp:130`），`GetLogic()`
            换局必然给新对象 ⇒ 每次拿到的都是"当前这一局的"；
          * **偏移表：可以永久缓存** —— 4 个数组字段的偏移只依赖类布局（同一构建不变）。
        `pin()` 就是第二条路：对象变了就换对象，类变了才重算偏移。
        """
        if not obj:
            return self._obj or 0
        if obj != self._obj:
            cls = None
            try:
                from .objects import ObjectArray
                cls = ObjectArray(self.s).class_of(obj)
            except Exception:                                  # noqa: BLE001
                cls = None
            self._obj = obj
            self._last_count = None            # 换了对象 ⇒ 计数基线重来
            if cls and cls != self._cls:
                self._cls = cls
                self._off = {}
                self._resolve_offsets()
        return self._obj

    def _resolve_offsets(self) -> None:
        """4 个数组字段的偏移（反射链现算，失败退回备注常量）。只依赖类布局 ⇒ 可缓存。"""
        if not self._cls or self._off:
            return
        from .props import find_prop
        for key, name, hint in (
                ("queue", "actionsQueue", OFF_ACTIONS_QUEUE),
                ("current", "currentActionValues", OFF_CURRENT_VALUES),
                ("all", "AllMatchActions", OFF_ALL_ACTIONS),
                ("mine", "myMatchActions", OFF_MY_ACTIONS)):
            pr = find_prop(self.s, self._cls, name)
            self._off[key] = pr["offset"] if pr else hint

    def forget(self) -> None:
        """丢掉"我定位到哪儿了"的全部缓存 ⇒ 下次 `locate()` 重新定位（快路径 ~5 ms）。

        ★ 与 `is_stale()` 配对使用：判断"可能不新鲜了"就 `forget()` + `locate()`，
        而不是像旧版那样把找到的指针**永久**当结论用。
        """
        self._obj = None
        self._cls = None
        self._off = {}
        self._last_count = None
        self._logic_cache = None

    def is_stale(self) -> bool:
        """**廉价新鲜度判据**（每次动作前调一次，成本 ≈ 2 次跨进程读）：

        ① 缓存对象头上的 `UObject::Class` 还是当初那个 `BP_OnlineMatch_C` 的 UClass 吗
           （直接读 `+0x10` 跟缓存值比，不建 ObjectArray / 不查 FNamePool ⇒ 极便宜）；
        ② `AllMatchActions` 的 `count()` 是否**单调不减** —— 它只增不减（见 `count()` 注释），
           所以"新值比上次小"**一定**是换了对象/换了局。
        任一成立 ⇒ 调用方 `forget()` + `locate()`。
        """
        if not self._obj:
            return True
        from .objects import OFF_UOBJECT_CLASS
        try:
            cls_now = self.m.ptr(self._obj + OFF_UOBJECT_CLASS)
        except Exception:                                      # noqa: BLE001
            return True
        if not cls_now or (self._cls and cls_now != self._cls):
            return True
        n = self.count()
        if self._last_count is not None and n < self._last_count:
            return True
        if n:
            self._last_count = n
        return False

    # ---- 定位 ----
    def locate(self, retry_cooldown: float = 1.0):
        """找 `BP_OnlineMatch_C` 实例 + 四个数组的偏移。**找到了**就只扫一遍、
        缓存住；**没找到**（还没进对局）每次都重扫，不缓存失败结果。

        ★ 重扫一次要走 `ObjectArray.iter_objects()` 对着可能几十万个活对象
        逐个再读 class/flags（§ 见下面 bug 说明背景），实测 ~4s，不是免费的。
        `agent/record.py` 的轮询循环一次 tick 里会连着调好几处依赖
        `locate()` 的方法（`_silent()`/`_poll_matchlog()`/`_poll_mulligan()`），
        真没在对局里时如果每次都硬重扫，一个 0.5s 的 tick 会被拖到 12s+。
        `retry_cooldown` 秒内失败过就直接短路返回 `False`，不去重扫；
        冷却过了才再扫一次——**只限速失败重试，找到了之后不受这个影响**
        （成功路径永远命中上面的缓存分支）。

        ★ 2026-09-25 真实事故修的 bug（用户报的"round2 录制文件 0 字节"）：
        旧版 `if self._obj is not None: return self._obj` 把 `False`（没找到）
        跟真正的对象指针一样**永久缓存**——`agent/record.py::Recorder` 在
        `AgentSession(warm=True)` 里建 `MatchLog` 时，如果那一刻游戏还停在
        牌组选择页（对局还没开始，`BP_OnlineMatch_C` 实例都还不存在），第一次
        `locate()` 必然扫空、缓存成 `False`；哪怕几秒后真的进了对局、对象已经
        创建出来，这个 `MatchLog` 实例也**再也不会重新扫一次**——`_poll_matchlog`/
        `_poll_mulligan`/`_silent` 全靠 `log.locate()` 当闸门，全局静默，一条
        样本都写不出来，正好对应实机撞见的"整局 0 字节"。
        `_obj` 只在**真正找到**（truthy 指针）时才是"以后都不用再扫"的稳定状态；
        `False`/`None` 只表示"这次没找到"，下次调用必须重新扫，不能当结论存住。
        """
        if self._obj:
            return self._obj
        import time as _t
        now = _t.time()
        if now - self._last_scan_fail_t < retry_cooldown:
            return False
        # ★ 2026-09-26：**快路径** —— 先看"当前关卡"的 actor（`ULevel::Actors`，实测 ~5 ms，
        #   191 个对象），而不是动辄 4~6 s 的 GObjects 全扫（12 万对象）。
        #   而且关卡作用域天然就是"当前这一局"（见 OPS-INJECT-HANDOFF §22.6）。
        try:
            from .world import Locator
            from .objects import ObjectArray
            _oa = ObjectArray(self.s)
            _pool = _oa.pool()
            for p in (Locator(self.m, self.s.base).actors() or []):
                if _pool.fname_of(_oa.class_of(p)) == MATCH_CLASS:
                    self._obj = p
                    self._cls = _oa.class_of(p)
                    self._last_count = None
                    self._resolve_offsets()
                    return self._obj
        except Exception:                                      # noqa: BLE001
            pass                                                    # 快路径失败 ⇒ 走下面的全扫
        from .objects import ObjectArray
        from .props import find_prop
        oa = ObjectArray(self.s)
        pool = oa.pool()
        cls = None
        found = None
        for p in oa.iter_objects():
            c = oa.class_of(p)
            if c and c != cls and pool.fname_of(c) == MATCH_CLASS:
                cls, found = c, p
                break
        self._obj = found
        self._cls = cls
        if not self._obj:
            self._last_scan_fail_t = now
            return False
        self._resolve_offsets()
        return self._obj

    # ---- 读 ----
    def _actions(self, key: str, start: int = 0) -> list:
        if not self.locate():
            return []
        data, num, _ = _tarray(self.m, self._obj + self._off[key], SZ_ACTION)
        return [read_action(self.m, data + i * SZ_ACTION)
                for i in range(max(0, start), num)]

    def count(self) -> int:
        """`AllMatchActions` 的长度。★ 它是**累积**的 ⇒ 拿它当增量游标。"""
        if not self.locate():
            return 0
        _d, n, _ = _tarray(self.m, self._obj + self._off["all"], SZ_ACTION)
        return n

    def all(self) -> list:
        return self._actions("all")

    def since(self, n: int) -> list:
        """第 n 个之后的动作。`AllMatchActions` 只增不减，所以这就是可靠的增量。"""
        return self._actions("all", n)

    def mine(self) -> list:
        return self._actions("mine")

    def queue(self) -> list:
        """还没演完的动作（动画还在播）。"""
        return self._actions("queue")

    def current_values(self) -> list:
        if not self.locate():
            return []
        return _values(self.m, self._obj + self._off["current"])

    # ---- `Logic`（`BP_Logic_C` 单例）：换牌期间也能用的一批字段 ------------
    #
    # ★ 2026-09-24 反字节码找到 `ActionValueMySide` 只是 `Context(this->Logic)
    #   .mySide` 一次普通取值后，顺手用 `props.class_props()` 把 `Logic` 的
    #   247 个字段全扫了一遍（不用再一个个猜）。里面一批字段直接解决/改善了
    #   好几处主规格/KARDS-NN.md 里悬着的问题，逐条列在下面各自的方法里。
    #   全部**只做了一次实机读**（当时在换牌阶段），数值本身没有跨局/跨状态
    #   交叉验证过，当作"字段存在、类型对得上"的强证据，**不是**"语义完全
    #   确认"的证据——尤其 bool/enum 的具体取值含义（哪个值表示什么）大部分
    #   没有反向验证，用之前最好再跟已知场景对一次。
    def _logic(self):
        """`BP_OnlineMatch_C.Logic` 指针 + 它的 UClass。缓存，找不到返回 `(None, None)`。"""
        if getattr(self, "_logic_cache", None) is not None:
            return self._logic_cache
        result = (None, None)
        if self.locate() and self._cls:
            from .props import find_prop
            pr_logic = find_prop(self.s, self._cls, "Logic")
            if pr_logic:
                logic_ptr = self.m.ptr(self._obj + pr_logic["offset"])
                if logic_ptr:
                    from .objects import ObjectArray
                    logic_cls = ObjectArray(self.s).class_of(logic_ptr)
                    if logic_cls:
                        result = (logic_ptr, logic_cls)
        self._logic_cache = result
        return result

    def _logic_field(self, name: str):
        """`Logic` 上按名字找一个字段的 `(prop_dict, logic_ptr)`，找不到 `(None, None)`。"""
        logic_ptr, logic_cls = self._logic()
        if not logic_ptr:
            return None, None
        from .props import find_prop
        pr = find_prop(self.s, logic_cls, name)
        return (pr, logic_ptr) if pr else (None, None)

    def my_side(self) -> Optional[int]:
        """本地玩家的 `ESideEnum`（1/2）。**不靠回合奇偶反推**——`board_api.
        read_my_side()` 那套需要 `turn>=1` + `isLocalClientTurn`，换牌阶段
        `turn` 还没定型时会返回 `None`（§7.6d 记过这个窗口不稳）。`Logic.mySide`
        是个普通实例变量，不依赖回合数，理论上从对局对象一创建就有效。
        **实机验证过一次**：换牌阶段读到 `mySide=1`，跟同一时刻
        `board_api.read_my_side()` 碰巧也算出的 `1` 一致；没验证过两者在
        `read_my_side()` 已知失效的窗口分歧的场景，但字节码看没有那个限制。
        """
        if self._my_side_cache is not None:
            return self._my_side_cache
        pr, ptr = self._logic_field("mySide")
        if not pr:
            return None
        v = self.m.u8(ptr + pr["offset"])
        if v not in (1, 2):
            return None
        self._my_side_cache = v      # 一局内不会变，缓存住
        return v

    def my_turn_has_started(self) -> Optional[bool]:
        """`Logic.myTurnHasStarted`（bool）—— 直接读"是不是我的回合"，
        可能比 `board_api` 靠 `isLocalClientTurn` 反推更直接。**未跟已知
        回合交叉验证过**，先当候选，别直接替换旧判据。"""
        pr, ptr = self._logic_field("myTurnHasStarted")
        return None if not pr else bool(self.m.u8(ptr + pr["offset"]))

    def mulligan_done(self):
        """`(我方 myMulliganDone, 对方 enemyMulliganDone)`——两个 bool。
        理论上比"扫 `AllMatchActions` 有没有 `XActionStartOfTurn`"
        （现在 `ops.in_mulligan()` 用的办法）更直接、更早能读到（不用等
        第一个 `XActionStartOfTurn` 出现）。**未做替换，先并存对照**。"""
        pr_m, ptr = self._logic_field("myMulliganDone")
        pr_e, _ = self._logic_field("enemyMulliganDone")
        mine = None if not pr_m else bool(self.m.u8(ptr + pr_m["offset"]))
        enemy = None if not pr_e else bool(self.m.u8(ptr + pr_e["offset"]))
        return mine, enemy

    def winner_side(self) -> Optional[int]:
        """`Logic.winnerSide`（`ESideEnum`）—— 直接回答 KARDS-NN.md §11
        "胜负从哪读最稳"：不用等结算页模板匹配，也不用扒 `XActionFinished`
        参数。配 `has_won()`（`Logic.hasWonTheMatch`）更直白。**已实机验证**：
        一局推平敌方总部后读到 `winner_side()=1`、`has_won()=True`，
        跟 `my_side()=1`、真实战果一致（2026-09-24）。
        """
        pr, ptr = self._logic_field("winnerSide")
        if not pr:
            return None
        v = self.m.u8(ptr + pr["offset"])
        return v if v in (1, 2) else None

    def has_won(self) -> Optional[bool]:
        """`Logic.hasWonTheMatch`（bool）——比 `winner_side()` 更直接的"我赢了没"。"""
        pr, ptr = self._logic_field("hasWonTheMatch")
        return None if not pr else bool(self.m.u8(ptr + pr["offset"]))

    def starting_cards(self) -> Optional[list]:
        """`Logic.myStartingCards`（`TArray<class UBaseCardObject*>`——实机
        核实过元素是**卡对象指针**，不是 int32）——"起手是哪几张"的权威来源，
        比 `record.py` 原来靠"第一次轮询到换牌界面就当起手"（可能已经晚了）
        更准更早。返回 `[{"card_id", "name"}, ...]`。

        **实机验证过一次、而且验证得很扎实**：读到 5 张（LINE OF ENGAGEMENT/
        LITTLE COBRA/RED DAWN/URA!/WE CAN DO IT!），换牌confirm 后再读
        `mulligan_marks()`/当前手牌，`RED DAWN` 变成了 `RESERVES`——跟"起手
        + 最终决定"这个数据模型对得上，是这次录制器改版真正要的东西。
        """
        pr, ptr = self._logic_field("myStartingCards")
        if not pr:
            return None
        arr_ptr = self.m.ptr(ptr + pr["offset"])
        num = self.m.i32(ptr + pr["offset"] + 8)
        if not arr_ptr or not num or num < 0 or num > 64:
            return None
        from . import cards as C
        out = []
        for i in range(num):
            cptr = self.m.ptr(arr_ptr + i * 8)
            if not cptr:
                continue
            raw = C.read_raw(self.s, cptr)
            out.append({"card_id": raw.get("card_id"), "name": raw.get("name")})
        return out

    # ---- 动作回执 ----
    def mark(self) -> int:
        """动手**之前**记一个游标。配 `receipt()` 用。

        ⚠ **先 `locate()` 预热再 `mark()`。**首次 `locate()` 要扫一遍 GObjects（~4s），
          `mark()` 会隐式触发它 —— 动作要是落在这 4 秒里，就会被当成"动手前就有的"，
          回执直接判失败。2026-09-23 当场踩过：用户按约定动了手，
          监听却报"零新增"，其实动作早在 `locate()` 期间就记上了。
        """
        return self.count()

    def receipt(self, mark: int, action_type: Optional[str] = None,
                timeout: float = 2.0, interval: float = 0.05,
                mine_only: bool = True, card_id: Optional[int] = None,
                card_field: str = "0") -> dict:
        """★ **动作回执**：发完鼠标动作后，等动作流里长出新条目。

        为什么这是正解（2026-09-23 定）
        ================================
        「我这一步到底生效没有」以前有两种判法，都不好：

          * 上游：动作执行后**用 OCR 抓屏幕横幅**。提示是给人看的、短命、有损。
          * 我们：**看手牌有没有变**（`play_card_from_hand` 里那段 `find_card(after,…) is None`）。
            这是**拿副作用猜因果** —— 手牌不变可能是没打出去，也可能是
            "部署后还要选目标、牌还留在手里"；手牌变了也可能是别的效果让你抽了牌。
            用户原话：**不合理。**

        动作流直接回答这件事：客户端认下来的动作**一定会进 `AllMatchActions`**，
        被本地判据拦掉的则根本不会出现。所以

            mk = ml.mark();  ops.attack_card(...);  r = ml.receipt(mk, "XActionAttackCard")

        `r["ok"]` 就是权威答案，`r["actions"]` 还带着这一步的全部结构化参数。

        ⚠ 边界（照实说）：`AllMatchActions` 里我方的条目 id 是 `-1`，表示
          **本地已上传**。它证明的是"客户端认了这个动作并发了出去"，
          不等于"服务器最终也认"。要区分这两者，得看服务器有没有回一条对应的动作
          —— 还没验过，写成待办（§11.1 F5c），别在这里假装已经区分了。

        ⚠ **2026-09-24 修正一个真实存在过的假阳性口子**（用户指出）：`mark()` 之后
          `since(mark)` 拿到的新条目**不一定只有我方这一条**——如果这次动作触发了
          对方的**反制**（百科：反制在对方回合按条件自动触发，不是我方发起的动作，
          见 §7.5 的"反制"说明），紧随其后可能追加**一条甚至多条对方的动作**，
          而且理论上不能排除它们的 `action_type` 恰好和我方这次等的类型撞上
          （比如都撞在 `XActionPlayCardFromHand`）。旧版本只按 `action_type` 过滤，
          不看是谁发的 ⇒ 这种情况下会把对方的条目误判成"我方这条动作的回执"，
          「最后一条新增条目就是我方的」这个假设在有反制介入时**不成立**。
          修法：`AllMatchActions` 里我方条目**恒为** `action_id == -1`（F5c 已验证，
          不区分是玩家点出来的还是系统广播的，且这个标记永久不回填），对手条目
          恒为服务器真实递增号 ⇒ 按 `action_id == -1` 就能精确摘出"这一段新增里
          属于我方的部分"，不管中间混进了多少条对方的（反制触发的）动作。
          `mine_only=True`（默认）就做这件事；显式传 `False` 才回到旧行为
          （不分谁发的，只按类型筛）。

        ⚠ **2026-09-25 加了 `card_id` 精确匹配**（用户指出）：光按 `action_type`
          筛只能回答"我方发生过一次这类动作"，答不了"是**这张牌**打出去了吗"——
          正常单线程调用不会撞（一次只发一个动作），但 `card_id` 恰好是唯一的
          （每张牌实例一个 id，不是卡名），干脆精确到牌，`play_unit`/`play_event`
          的重试循环拿着更硬的回执。字段位置按 `agent/record.py::MINE_LABEL_FIELDS`
          （`XActionPlayCardFromHand` 的 subject 是 `data[0]`，已实机验证过）。
          ★ **不能拿"这张 card_id 在整局历史里出现过"当"已经打出去了"的缓存**——
          被撤回手牌的牌（比如部署后取消目标选择）可以再打一次，同一个 card_id
          会在动作流里出现第二条 `XActionPlayCardFromHand`。所以这里**必须**
          一直用 `since(mark)` 抓"这次动手之后新增的"，不能查全局历史，
          不然会把"上一次打出去又被撤回"误判成"这一次也打成了"。
        """
        import time as _t
        t0 = _t.time()
        while True:
            rows = self.since(mark)
            if mine_only:
                rows = [r for r in rows if r.get("action_id") == -1]
            if action_type:
                rows = [r for r in rows if r.get("action_type") == action_type]
            if card_id is not None:
                rows = [r for r in rows if _field_value(r, card_field) == card_id]
            if rows:
                return {"ok": True, "actions": rows, "waited": _t.time() - t0}
            if _t.time() - t0 >= timeout:
                return {"ok": False, "actions": [], "waited": _t.time() - t0}
            _t.sleep(interval)


def format_action(a: dict, me: Optional[int] = None) -> str:
    """一行人话。`me` 给了就把 player_id 翻成 我方/敌方。"""
    who = a.get("player_id")
    if me is not None and who is not None:
        who = "我方" if who == me else "敌方"
    kv = " ".join("%s=%s" % (v["name"], v["text"] if v["text"] else v["value"])
                  for v in (a.get("data") or []) if v.get("name"))
    sub = ",".join(s["name"] or "?" for s in (a.get("sub_actions") or []))
    return "#%-5s %-6s %-22s %s%s" % (
        a.get("action_id"), who, a.get("action_type"), kv,
        ("  [%s]" % sub) if sub else "")


def main(argv=None) -> int:
    """`python -m kardsmem.matchlog [--watch] [--mine] [--tail N]`"""
    import argparse
    import time
    ap = argparse.ArgumentParser(description="对局动作流（只读）")
    ap.add_argument("--watch", action="store_true", help="盯着新动作")
    ap.add_argument("--mine", action="store_true", help="只看 myMatchActions")
    ap.add_argument("--tail", type=int, default=30)
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--seconds", type=float, default=300)
    a = ap.parse_args(argv)

    from . import attach
    ml = MatchLog(attach())
    if not ml.locate():
        print("没找到 BP_OnlineMatch_C —— 不在对局里？")
        return 1
    rows = ml.mine() if a.mine else ml.all()
    print("共 %d 条，显示最后 %d 条：" % (len(rows), min(a.tail, len(rows))))
    for r in rows[-a.tail:]:
        print("  " + format_action(r))
    if not a.watch:
        return 0
    n = ml.count()
    print("\n盯着新动作…… Ctrl-C 结束")
    t0 = time.time()
    try:
        while time.time() - t0 < a.seconds:
            m = ml.count()
            if m > n:
                for r in ml.since(n):
                    print("[%7.2fs] %s" % (time.time() - t0, format_action(r)))
                n = m
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\n(中断)")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
