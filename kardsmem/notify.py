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

注意
====
* 提示是**短命**的（淡入淡出后 widget 被销毁），所以要轮询，别指望事后再去捞。
  动作发出后立刻开始 poll，持续 1~2 秒。
* 不是所有提示都代表失败 ——「轮到你了」之类也走同一条路（`YourTurnStyle`）。
  判断用文本内容，不要假设"有提示就是被拒"。
* 同一条提示重复弹出会是**不同的 widget 实例**（地址不同），
  所以按地址去重就够了，不用比文本。
"""
from __future__ import annotations

from typing import Optional

WIDGET_CLASS = "NotifyTextWidget_C"

# 本构建实测偏移，**仅作备注** —— 运行时一律走反射链（见 §4.13）。
OFF_TEXT_HINT = 0x3B0            # NotifyTextWidget_C::Text (FText)
OFF_MANUAL_REMOVE_HINT = 0x3C0
OFF_MESSAGETEXT_HINT = 0x360     # NotifyTextWidget_C::MessageText (UTextBlock*)
OFF_TEXTBLOCK_TEXT = 0x188       # UTextBlock::Text (FText) —— 原生类，跨构建较稳


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

    # ---- 读 ----
    def live(self) -> list:
        """当前还活着的提示 widget → [{addr, text, manual_remove}]。"""
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

    def poll(self) -> list:
        """自上次 poll 以来**新出现**的提示。按 widget 地址去重。

        ⚠ 地址会被复用：widget 销毁后同一块内存可能分给下一条提示。
          所以只保留还活着的地址，销毁的从 `_seen` 里剔掉 ——
          否则下一条提示复用了旧地址就会被当成"见过的"漏掉。
        """
        cur = self.live()
        addrs = {r["addr"] for r in cur}
        self._seen &= addrs
        new = [r for r in cur if r["addr"] not in self._seen]
        self._seen |= addrs
        return new
