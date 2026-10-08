#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`GameInstanceSubsystem` 的**类 → 实例**索引（只读）。

为什么需要它（TODO A7b）：`CRUISER SCOUTS` 的打出链里有
`USubsystemBlueprintLibrary::GetGameInstanceSubsystem(ContextObject, Class)`（库静态函数，
全游戏 368 次调用）。要复刻它就得"按类找 subsystem 实例"。两条已排除的便宜路：
  * 每次调用都扫一遍全部对象：96,044 个对象、只读类指针也要 **5.28 s**
    （`probe_scan_cost.json`），远超求值链的 0.6 s 预算；
  * 读反射属性：`BP_KardsGameInstance_C` 的 61 个属性里**没有**子系统容器
    （`UGameInstance::SubsystemCollection` 不是 UPROPERTY，`probe_gi_subsystems.json`）。
所以这里走**一次性索引**：启动时（或首次需要时）扫一遍，按"类指针 → 实例"建表，
并且把实例登记到它**整条父类链**上（BP 可能传父类）。之后查表 O(1)。

索引按 pid 缓存；miss 且距上次构建 ≥ `min_interval` 秒才重建（重建要 5 s 左右，
子系统生命周期长，重建极少发生）。
"""
from __future__ import annotations

import time
from typing import Optional

_INDEX: dict = {}          # pid -> (built_at, {class_ptr: obj_ptr})


def _super_struct(ks, uclass: int) -> int:
    """`UStruct::SuperStruct`（+0x40）。**与 kismet/props 用的是同一个偏移**。"""
    try:
        from .props import OFF_SUPER
    except Exception:                                           # noqa: BLE001
        OFF_SUPER = 0x40
    return ks.m.ptr_or_zero(uclass + OFF_SUPER)


def build_index(ks, *, force: bool = False) -> dict:
    """扫一遍对象池，返回 `{class_ptr: obj_ptr}`（同类只留第一个）。"""
    pid = getattr(ks, "pid", None) or id(ks)
    hit = _INDEX.get(pid)
    if hit and not force:
        return hit[1]
    from .objects import ObjectArray

    oa = ObjectArray(ks)
    pool = oa.pool()
    cls_name: dict = {}
    is_sub: dict = {}                 # 类指针 → 是否 subsystem（按**继承链**判，不是按名字）
    chain_of: dict = {}               # 类指针 → 祖先链（登记用）
    table: dict = {}
    for p in oa.iter_objects():
        c = oa.class_of(p)
        if not c:
            continue
        flag = is_sub.get(c)
        if flag is None:
            # ★ 每个**不同的类**只走一次链（进程里 ~926 个类）——
            #   以前按名字筛（"subsystem" 出现在类名里）会漏掉 `BP_OnlineMatch_C` 这种
            #   名字看不出是 subsystem 的类 ⇒ 每次 `GetGameInstanceSubsystem` 都未命中、
            #   触发全量重建（实机实测一次 3.25 s，占 CRUISER SCOUTS 整条链的 99%）。
            chain, cur, depth = [], c, 0
            while cur and depth < 12:
                chain.append(cur)
                nm = cls_name.get(cur)
                if nm is None:
                    nm = cls_name[cur] = (pool.fname_of(cur) or "")
                cur = _super_struct(ks, cur)
                depth += 1
            chain_of[c] = chain
            flag = is_sub[c] = any("subsystem" in (cls_name.get(x) or "").lower()
                                   for x in chain)
        if not flag:
            continue
        for cur in chain_of[c]:                  # 登记到整条父类链（BP 可能传父类）
            table.setdefault(cur, p)
    _INDEX[pid] = (time.time(), table)
    return table


def find(ks, class_ptr: int, *, min_interval: float = 300.0) -> int:
    """按类指针找 subsystem 实例（含父类命中的情况）。找不到返回 0。"""
    if not ks or not class_ptr:
        return 0
    pid = getattr(ks, "pid", None) or id(ks)
    hit = _INDEX.get(pid)
    if hit is None:
        return build_index(ks).get(class_ptr, 0)
    obj = hit[1].get(class_ptr)
    if obj:
        return obj
    if (time.time() - hit[0]) >= float(min_interval):
        return build_index(ks, force=True).get(class_ptr, 0)
    return 0


def info(ks) -> Optional[dict]:
    """诊断：索引建了没有、多少项、建了多久。"""
    pid = getattr(ks, "pid", None) or id(ks)
    hit = _INDEX.get(pid)
    if not hit:
        return None
    return {"entries": len(hit[1]), "age_s": round(time.time() - hit[0], 1)}
