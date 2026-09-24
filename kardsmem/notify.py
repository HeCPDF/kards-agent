#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.notify —— 抓游戏**自己弹出来的提示文本**（动作被拒绝时的那条）。

为什么要它
==========
进程外重算合法性（`CanPlayFromHand` / `CanAttack` 那一套）**一定会有缺漏** ——
卡池两千张、效果互相叠、还有原生代码我们读不到。
所以那套判据只能用来**排序和挑选**，**不能用来否定**一个动作。

真正可靠的否定来自游戏自己：动作非法时它会弹一条提示。
把这条文本读出来，就得到了**权威的失败原因**，而且是现成的、带本地化的。

链路（全部来自导出蓝图，逐跳有据）
==================================
    BP_PlayerMoves / BP_Logic
        logic->OnNotifyPlayer->Broadcast(text, delay)        ← 被拒时发这个
        文本由 logic->GetInvalidTargetText(reason, p1, p2) 生成
      ↓  Maps/Battle.cpp:119 把 Battle::OnNotifyPlayer 绑上去
    Battle::OnNotifyPlayer  → UtilityFunctions::BigNotify(text)   (Battle.cpp:106)
      ↓
    BP_HUD::WriteNotificationToScreen(text)                  (BP_HUD.cpp:231)
      ↓
    UtilityFunctions::NotifyPlayer(...)                      (UtilityFunctions.cpp:1667)
        → Create(NotifyTextWidget_C) 并 SetTextPropertyByName(w, "text", text)
      ↓
    **每条提示 = 一个新的 `NotifyTextWidget_C` 实例，文本在它的 `FText Text` 上。**

⇒ 读法：GObjects 里枚举 `NotifyTextWidget_C` → 读 `Text` 的明文。

    from kardsmem import attach
    from kardsmem.notify import NotifyWatcher
    w = NotifyWatcher(s)
    w.poll()          # → 自上次调用以来**新出现**的提示 [{addr, text, ...}]

★★ 怎么读：三条路合用（2026-09-23 实机定的，每条都有它必须在的理由）
=====================================================================
**① 增量扫描 —— 主力。**`GUObjectArray` 的 `NumElements` 只增不减，新建对象
追加在末尾 ⇒ 两次轮询之间新出现的提示，索引一定落在「上次的 NumElements」到
「这次的」之间。一次几十个对象，0.05s 的间隔跑得动（实测中位耗时 0.000s）。

**② `current()`（HUD 的 `CurrentNotifyPlayer`）—— 只作补充，别依赖。**

    BP_Widget_Battle_HUD_PC_C::CurrentNotifyPlayer  // TSoftObjectPtr<NotifyTextWidget_C> @0x6B8

前 8 字节是 `FWeakObjectPtr{ObjectIndex:i32, SerialNumber:i32}`，索引直接查
`GUObjectArray` ⇒ O(1)（序列号要和 `FUObjectItem.SerialNumber@+0x10` 对上，防复用）。

⚠ **实测它看不全。**一开始以为这就是答案，交叉核对（`notifywatch.py --crosscheck`）
  当场证伪：同一段时间里它捕到 **0** 条，而全扫描捕到 **5** 条 ——

      4 × "Your unit can't attack the turn it comes into play."   ← 拒绝类
      1 × 'YOUR TURN'                                             ← 横幅类

  用户的直觉先到一步：「位置相同，但不是一个」。SDK 里也早有迹象 ——
  `BP_Logic` / `BP_Board` / `BP_PlayerMoves` / `BP_KardsSession` / `BP_PopupManager`
  **各自**都有 `CallFunc_NotifyPlayer_theWidget` 这个局部，它们建的 widget
  不写进 HUD 的 `CurrentNotifyPlayer`。
  ⇒ **教训：O(1) 的把手先要证明它看得全，再当主力用。**

**③ 周期性全扫描 —— 兜底。**GC 之后 UE 会复用空闲槽位，复用的槽位索引小于
①的窗口下界 ⇒ ① 看不到。全扫描一次 2~4 秒（19 万对象）**会把轮询堵住**，
所以默认 15s 才来一次，只用来捞漏，不作主力。

⇒ 事件流一律用 `poll()`（三条都在里面）。`live()` 只回答"此刻有什么"，
  `live_scan()` 留作交叉核对。

注意
====
* 提示是**短命**的（淡入淡出后 widget 被销毁），所以要轮询，别指望事后再去捞。
  动作发出后立刻开始 poll，持续 1~2 秒。
* 不是所有提示都代表失败 ——「轮到你了」「你占领了前线」都走同一条路。
  ⇒ 它其实是一条**对双方公示的事件流**，判断用文本内容，别假设"有提示就是被拒"。
* 同一条提示重复弹出会是**不同的 widget 实例**，但**地址会被回收复用** ⇒
  去重按 `(地址, 文本)`，不要只按地址。
"""
from __future__ import annotations

from typing import Optional

# --------------------------------------------------------------------------
# 事件分类（§11.2「提示文本 → 事件流分类」，2026-09-24）
# --------------------------------------------------------------------------
# ★ 提示不止讲"为什么不行"——抽牌/疲劳伤害/回合开始都走同一条通道，对双方公示
#   （模块 docstring 早就写了）。这条分三类：
#     reject     —— 权威的失败原因（能反查到 `helpbubbles` 里的 `reason_key`）
#     banner     —— 短横幅（"YOUR TURN" 这种，没有句号、全大写）
#     settlement —— 剩下的（抽牌/伤害/占领之类的结算播报）
#
# `reject` 是**数据驱动**的，不是猜的：`helpbubbles` namespace 下
# `notify_*`/`must_target_*`/`you_cant_*`/`nation_has_no_*`/
# `cant_be_targeted_by_enemy_orders` 这几类前缀，逐个 grep 过
# `reverse-data/exports-<build>/.../Localization/Game/en/Game.locres`
# 核实是真实存在的失败原因文案（90 条，含 `agent.legality.REASON_HELPBUBBLE`
# 里已经用到的 `notify_cant_attack_*` 18 条，也含之前没接的 `notify_move_*`
# `notify_play_from_hand_*`/`must_target_*` 等）——**权威原文来自游戏自己**，
# 不是手写猜的中文。`banner`/`settlement` 目前没有类似的权威清单可枚举
# （没找到一个专门装"YOUR TURN"这类横幅的 namespace），只能按文本形状分：
# 全大写、没有句号 ⇒ banner；其余默认结算播报。这条不像 reject 那么硬，
# 分错了大概率会把 banner 误落进 settlement，不影响 reject 的准确性。
_REJECT_PREFIXES = ("notify_", "must_target_", "you_cant_", "nation_has_no_")
_reject_text_to_key = None       # 惰性建：{英文原文: helpbubbles key}


def _reject_map(build: Optional[str] = None) -> dict:
    """`{英文原文: helpbubbles key}`，只含判定过是"失败原因"的那些前缀。"""
    global _reject_text_to_key
    if _reject_text_to_key is not None:
        return _reject_text_to_key
    from . import locres
    import os
    out = {}
    d = locres.locale_dir(build)
    if d:
        try:
            en = locres.load(os.path.join(d, "en", "Game.locres"))
        except Exception:                                        # noqa: BLE001
            en = {}
        for (ns, key), text in en.items():
            if ns != "helpbubbles" or not text:
                continue
            if key.startswith(_REJECT_PREFIXES) or key == "cant_be_targeted_by_enemy_orders":
                out[text] = key
    _reject_text_to_key = out
    return out


def classify(text: str) -> dict:
    """一条提示文本 → `{"kind": "reject"|"banner"|"settlement", "reason_key": str|None}`。

    `kind="reject"` 时 `reason_key` 是 `helpbubbles` 里的键，可以直接喂给
    `kardsmem.locres.namespace_zh("helpbubbles", reason_key)` 拿中文——
    跟 `agent.legality.REASON_HELPBUBBLE` 走的是同一张权威表，不是另起一套。
    """
    if not text:
        return {"kind": "settlement", "reason_key": None}
    key = _reject_map().get(text)
    if key:
        return {"kind": "reject", "reason_key": key}
    stripped = text.strip()
    if stripped and stripped == stripped.upper() and not stripped.endswith((".", "!", "?")) \
            and any(c.isalpha() for c in stripped) and len(stripped) <= 40:
        return {"kind": "banner", "reason_key": None}
    return {"kind": "settlement", "reason_key": None}


WIDGET_CLASS = "NotifyTextWidget_C"
HUD_CLASS = "BP_Widget_Battle_HUD_PC_C"

# 本构建实测偏移，**仅作备注** —— 运行时一律走反射链（见 §4.13）。
OFF_TEXT_HINT = 0x3B0            # NotifyTextWidget_C::Text (FText)
OFF_MANUAL_REMOVE_HINT = 0x3C0
OFF_MESSAGETEXT_HINT = 0x360     # NotifyTextWidget_C::MessageText (UTextBlock*)
OFF_TEXTBLOCK_TEXT = 0x188       # UTextBlock::Text (FText) —— 原生类，跨构建较稳
OFF_CURRENT_NOTIFY_HINT = 0x6B8  # BP_Widget_Battle_HUD_PC_C::CurrentNotifyPlayer
# FUObjectItem：Object@0x00(8) Flags@0x08(4) ClusterRootIndex@0x0C(4) SerialNumber@0x10(4)
# —— 2026-09-23 实机摊开验证：idx=151404 的 item 里 SerialNumber=45681，
#    和弱引用里的序列号一字不差。
OFF_FUOBJECTITEM_SERIAL = 0x10


class NotifyWatcher:
    """轮询式的提示抓取器。只读。"""

    def __init__(self, session):
        self.s = session
        self.m = session.m
        self._uclass = None          # NotifyTextWidget_C 的 UClass（缓存）
        self._propsize = None        # 拿到之后改用 PropertiesSize 认，比对名字快得多
        self._prop = None            # Text 属性的描述
        self._off_msgtext = None     # MessageText 指针的偏移
        self._seen = set()
        # 快路（O(1)）：HUD 对象 + CurrentNotifyPlayer 的偏移。False = 找过但没有。
        self._hud = None
        self._off_current = None
        self._oa = None
        self._items = None           # 上一轮的 FUObjectItem 字节快照（差分用）
        self._full_at = 0.0          # 上次兜底全扫描的时刻
        self._clssz = {}             # UClass -> PropertiesSize（认身份用，缓存）

    # ---- 定位 ----
    def _locate_class(self) -> Optional[int]:
        """第一次调用时按类名找 UClass，之后缓存。

        ★ 按名字扫一遍 GObjects 要 2 秒多，**不能每帧做**。
          拿到 UClass 之后改用 `PropertiesSize` 判身份（§4.1 的统一判据）。
        """
        if self._uclass:
            return self._uclass
        from .objects import ObjectArray
        from .props import OFF_PROPSIZE
        oa = ObjectArray(self.s)
        pool = oa.pool()
        for p in oa.iter_objects(skip_cdo=False):
            c = oa.class_of(p)
            if c and pool.fname_of(c) == WIDGET_CLASS:
                self._uclass = c
                self._propsize = self.m.i32(c + OFF_PROPSIZE)
                break
        if self._uclass:
            from .props import find_prop
            self._prop = find_prop(self.s, self._uclass, "Text")
            mt = find_prop(self.s, self._uclass, "MessageText")
            self._off_msgtext = mt["offset"] if mt else None
        return self._uclass

    def text_offset(self) -> Optional[int]:
        self._locate_class()
        return self._prop["offset"] if self._prop else None

    # ---- 快路：O(1) ----
    def _objects(self):
        if self._oa is None:
            from .objects import ObjectArray
            self._oa = ObjectArray(self.s)
        return self._oa

    def _locate_hud(self):
        """找 `BP_Widget_Battle_HUD_PC_C` 的实例 + `CurrentNotifyPlayer` 的偏移。

        要扫一遍 GObjects（~4s），**只做一次**。不在对局里时找不到，记成 False
        并让 `live()` 退回慢路 —— 不抛。
        """
        if self._hud is not None:
            return self._hud
        oa = self._objects()
        pool = oa.pool()
        cls = None
        for p in oa.iter_objects():
            c = oa.class_of(p)
            if not c:
                continue
            if c == cls:
                continue
            if pool.fname_of(c) == HUD_CLASS:
                cls = c
                self._hud = p
                break
        if not self._hud:
            self._hud = False
            return False
        from .props import find_prop
        pr = find_prop(self.s, cls, "CurrentNotifyPlayer")
        self._off_current = pr["offset"] if pr else OFF_CURRENT_NOTIFY_HINT
        return self._hud

    def _widget_text(self, w: int) -> Optional[str]:
        from .names import ftext_at
        off = self._prop["offset"] if self._prop else OFF_TEXT_HINT
        txt = ftext_at(self.m, w, off)
        if not txt:
            # ★ 真正**渲染出来**的是 `MessageText`（UTextBlock）里的那份。见 live_scan。
            tb = self.m.ptr_or_zero(w + (self._off_msgtext or OFF_MESSAGETEXT_HINT))
            if tb:
                txt = ftext_at(self.m, tb, OFF_TEXTBLOCK_TEXT)
        return txt or None

    def current(self) -> list:
        """O(1)：HUD 的 `CurrentNotifyPlayer` 弱引用 → 那一条提示。

        校验序列号：`FWeakObjectPtr` 里的 SerialNumber 必须等于
        `FUObjectItem.SerialNumber`，否则这个索引已经被回收给别的对象了
        （UE 的弱引用就是靠这个判失效的）。对不上就当"没有提示"。
        """
        import struct
        if not self._locate_hud():
            return []
        if self._prop is None:
            self._locate_class()
        b = self.m.read_exact(self._hud + self._off_current, 8)
        if not b:
            return []
        idx, ser = struct.unpack("<ii", b)
        if idx <= 0:
            return []
        oa = self._objects()
        item = oa.item_addr(idx) if hasattr(oa, "item_addr") else None
        if item:
            live_ser = self.m.i32(item + OFF_FUOBJECTITEM_SERIAL)
            if live_ser is not None and live_ser != ser:
                return []          # 索引被复用 ⇒ 这个弱引用已经失效
        w = oa.at(idx)
        if not w:
            return []
        txt = self._widget_text(w)
        if not txt:
            return []
        return [{"addr": w, "text": txt,
                 "manual_remove": bool(self.m.u8(w + OFF_MANUAL_REMOVE_HINT))}]

    # ---- 槽位差分扫描：只看**变了的**槽位 ----
    def _scan_changed(self) -> list:
        """两次 `FUObjectItem` 快照之间变了的槽位里，挑出 `NotifyTextWidget_C`。

        ★ 2026-09-23 实机换过一次做法，原因写在模块 docstring 里：
          先用的是"只扫 NumElements 新增的那一段"，**会漏** —— UE 在 GC 后复用
          数组**中间**的空闲槽位，新提示常常落在那里。槽位一被复用，
          `FUObjectItem` 的 Object/SerialNumber 就变，所以改成**比字节**。
        """
        from .props import OFF_PROPSIZE
        oa = self._objects()
        cur = oa.snapshot_items()
        if self._items is None:
            self._items = cur
            return []
        out = []
        for p in oa.changed_slots(self._items, cur):
            c = oa.class_of(p)
            if not c:
                continue
            if c not in self._clssz:
                self._clssz[c] = self.m.i32(c + OFF_PROPSIZE)
            if self._clssz[c] != self._propsize:
                continue
            txt = self._widget_text(p)
            if txt:
                out.append({"addr": p, "text": txt, "via": "diff",
                            "manual_remove": bool(self.m.u8(p + OFF_MANUAL_REMOVE_HINT))})
        self._items = cur
        return out

    # ---- 读 ----
    def live(self) -> list:
        """当前还活着的提示 → [{addr, text, manual_remove}]。

        ⚠ **不要**拿它做事件流的主力：它要么是全扫描（2~4s），要么是
          `current()`（O(1) 但**看不全**，见下）。事件流用 `poll()`。
        """
        if self._locate_hud():
            rows = self.current()
            if rows:
                return rows
        return self.live_scan()

    def live_scan(self) -> list:
        """全扫描版（慢路，2~4s）：枚举所有 `NotifyTextWidget_C`。

        留着有两个用处：① 不在对局里时的兜底；
        ② **交叉核对** —— 快路只看得到 `CurrentNotifyPlayer` 那一条，
        怀疑漏了就用这个对一下。
        """
        if not self._locate_class():
            return []
        from .objects import ObjectArray
        from .names import ftext_at
        from .props import OFF_PROPSIZE
        off = self._prop["offset"] if self._prop else OFF_TEXT_HINT
        oa = ObjectArray(self.s)
        out, seen_cls = [], {}
        for p in oa.iter_objects():
            c = oa.class_of(p)
            if not c:
                continue
            if c not in seen_cls:
                seen_cls[c] = self.m.i32(c + OFF_PROPSIZE)
            if seen_cls[c] != self._propsize:
                continue
            txt = ftext_at(self.m, p, off)
            if not txt:
                # ★ 回退：真正**渲染出来**的是 `MessageText`（UTextBlock）里的那份。
                #   转储里见过 `Text` 全零但 widget 还在的情况（多半是已销毁待回收的
                #   空壳，也可能是先建 widget 后填文本的那一帧）。以屏幕上的为准。
                tb = self.m.ptr_or_zero(p + (self._off_msgtext or OFF_MESSAGETEXT_HINT))
                if tb:
                    txt = ftext_at(self.m, tb, OFF_TEXTBLOCK_TEXT)
            if not txt:
                continue          # 空壳不算一条提示
            out.append({
                "addr": p,
                "text": txt,
                "manual_remove": bool(self.m.u8(p + OFF_MANUAL_REMOVE_HINT)),
            })
        return out

    def poll(self, full_every: float = 15.0) -> list:
        """自上次 poll 以来**新出现**的提示。**这是事件流的入口。**

        三条路合起来用（2026-09-23 实机定的，每一条都有它必须在的理由）：

          ① **增量扫描**（主力）：只看 `NumElements` 新长出来的那一段索引。
             一次几十个对象，可以按 0.05s 的间隔跑。
          ② `current()`（HUD 的 `CurrentNotifyPlayer`）：O(1)，但**实测看不全** ——
             交叉核对里它一条都没捕到，而全扫描同期捕到 5 条
             （4 条 "Your unit can't attack the turn it comes into play."
             ＋ 1 条 'YOUR TURN'）。⇒ **不能只靠它**，只作补充。
          ③ **周期性全扫描**（兜底，默认 15s 一次）：一次 2~4s，**会把轮询循环堵住**，
             所以间隔别调小 —— 它只是捞 ① 漏掉的复用槽位，不是主力。GC 之后 UE 会复用空闲槽位，
             复用的槽位索引小于增量窗口的下界 ⇒ ① 看不到。这一条把它捞回来。
             `full_every=0` 可以关掉。

        ⚠ 去重按 `(地址, 文本)`，不能只按地址：widget 销毁后同一块内存会分给
          下一条提示，只按地址会把**新提示当成见过的漏掉**。
        """
        import time as _t
        oa = self._objects()
        if self._prop is None:
            self._locate_class()
        if not self._propsize:
            return []
        cur = []
        if self._items is None:
            # 第一次：全扫描拿基线 + 建立槽位快照
            cur = [dict(r, via="full") for r in self.live_scan()]
            self._scan_changed()
            self._full_at = _t.time()
        else:
            cur += self._scan_changed()
            for r in self.current():
                cur.append(dict(r, via="hud"))
            if full_every and _t.time() - self._full_at >= full_every:
                cur += [dict(r, via="full") for r in self.live_scan()]
                self._full_at = _t.time()

        # 已知实例还活着就留着（它们的键要保住，否则下一轮会被当成"新的"重报）
        alive = {(r["addr"], r["text"]) for r in cur}
        for a, t in list(self._seen):
            if (a, t) in alive:
                continue
            if self._widget_text(a) == t:
                alive.add((a, t))          # 还在，只是这轮没扫到它
        new = [r for r in cur if (r["addr"], r["text"]) not in self._seen]
        self._seen = alive
        # ★ 事件流分类接在这里（不是"错误信息"，是一条对双方公示的事件流）——
        #   `kind`/`reason_key` 见模块顶部 `classify()`。
        for r in new:
            r.update(classify(r["text"]))
        return new


def selftest() -> list:
    """离线断言：`classify()` 不需要游戏在跑——只要本地的 `Game.locres` 导出在。"""
    rows = []

    def chk(name, got, want):
        ok = got == want
        rows.append(("PASS" if ok else "FAIL", name, got, want))

    m = _reject_map()
    chk("locres 找到 reject 文案", len(m) > 0, True)
    # 拿一条真实存在的 reject 文案核对分类（不是编的——来自 helpbubbles 表）
    known = next((t for t, k in m.items() if k == "notify_move_unit_is_pinned"), None)
    if known:
        chk("已知拒绝文案分类成 reject", classify(known)["kind"], "reject")
        chk("reject 的 reason_key 对得上", classify(known)["reason_key"],
            "notify_move_unit_is_pinned")
    chk("全大写无句号 → banner", classify("YOUR TURN")["kind"], "banner")
    chk("普通结算文本 → settlement", classify("Draw a card.")["kind"], "settlement")
    chk("空文本不炸", classify("")["kind"], "settlement")
    for r in rows:
        print("  [%s] %s got=%r want=%r" % r)
    ok = all(r[0] == "PASS" for r in rows)
    print("notify selftest: %s（%d 项失败）" % ("PASS" if ok else "FAIL",
                                              sum(1 for r in rows if r[0] == "FAIL")))
    return rows
