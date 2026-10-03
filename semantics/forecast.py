"""预报（OVERCAST 一类）候选的**提前预测**（2026-10-01，未实机验证）。

事实（1.60 BP 导出，`BP_CardFunctions.cpp:23707 Forecast`、`card_event_sunny1_blue_sky.cpp:93 GetChooseSpawnCards`）：
  * 第一层是**固定**的三张：`card_event_sunny1_blue_sky` / `card_event_rain1_mist` / `card_event_storm1_gale`；
  * 选定其中一张后，它的 `GetChooseSpawnCards` 按轻→中→重**依次**做三次
    `RandomIntFromRangeWithStream(0, 池长-1)`（每次 d+2 次抽取，走 `cardsRandomStream`）取出第二层三张候选；
    池 = `GetAllActiveStaticCards`（按名字排序的静态牌表）里带对应标签的牌。
  ⇒ 有了活种子，就能在点第一层之前把"第一层 3 张 × 第二层 3 张 = 9 个结果"全算出来（各自从同一个种子起算，
    因为真正发生的只有你选的那一条）。

本模块只做**预测**：对每张第一层候选的卡对象，在外部 VM 里用活种子的副本跑一遍它的 `GetChooseSpawnCards`，
读出出参 `cards`。评估（9 选 1 选哪条）由调用方做。读不出来/VM 停了就如实返回 `stopped`，调用方退回原来的做法。
"""
from __future__ import annotations

import time
from typing import Optional


def predict_layer2(km, layer1_ptrs: list, seed: Optional[int], budget_s: float = 6.0) -> list:
    """对每个第一层候选（卡对象指针）预测第二层候选。

    返回 `[{"ptr": 第一层候选, "name": 名字, "layer2": [名字...], "draws": 抽取数, "stopped": None|原因}, ...]`。
    """
    out = []
    if seed is None:
        return [{"ptr": p, "name": None, "layer2": [], "draws": 0, "stopped": "读不到随机种子"} for p in layer1_ptrs]
    try:
        import os
        import sys
        tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from canplay import hook_targeted, make_get_field, make_view      # noqa: PLC0415
        from kardsmem import kismet
        from kardsmem.cardnatives import CardNatives
        from kardsmem.names import NameResolver                           # noqa: F401  (名字解析见 _card_name)
        from kardsmem.objects import ObjectArray
        from kardsmem.rng import Stream
        from kardsmem.vm import VM
        from semantics.effectvm import Recorder
    except Exception as ex:                                               # noqa: BLE001
        return [{"ptr": p, "name": None, "layer2": [], "draws": 0, "stopped": "导入失败：%s" % ex} for p in layer1_ptrs]
    oa = ObjectArray(km)
    t_end = time.time() + budget_s
    for p in layer1_ptrs:
        row = {"ptr": p, "name": _card_name(km, p), "layer2": [], "draws": 0, "stopped": None}
        try:
            uc = oa.class_of(p)
            fn = kismet.find_function(km, uc, "GetChooseSpawnCards", inherited=False) if uc else None
            if not fn:
                row["stopped"] = "这张牌没有 GetChooseSpawnCards"
                out.append(row)
                continue
            rec = Recorder(p, 0, None)
            rec.stream = Stream(seed)
            hooks = rec.hooks()
            hooks["GetTargetedCard"] = hook_targeted(False, 0)
            cn = CardNatives(None, view=make_view(km), ks=km)
            try:                                  # 静态卡表（GetStatic* 用）：失败留空 ⇒ 调用处如实抛
                from kardsmem.gs import make_static_card_provider
                _prov = make_static_card_provider(km)
                cn.static_card = _prov
                cn.static_cards = getattr(_prov, "names", None) or frozenset()
            except Exception:                     # noqa: BLE001
                pass
            vm = VM(km, get_field=make_get_field(km), card_natives=cn, hooks=hooks)
            vm.deadline = t_end
            r = vm.run(fn, self_obj=p)
            row["stopped"] = r.get("stopped")
            cards = (r.get("out") or {}).get("cards")
            if isinstance(cards, (list, tuple)):
                row["layer2"] = [_card_name(km, x) for x in cards if isinstance(x, int)]
            row["draws"] = rec.stream.draws
        except Exception as ex:                                           # noqa: BLE001
            row["stopped"] = "%s: %s" % (type(ex).__name__, ex)
        out.append(row)
    return out


def _card_name(km, ptr: int) -> Optional[str]:
    try:
        from kardsmem.names import ftext_at  # noqa: F401
    except Exception:                                                     # noqa: BLE001
        pass
    try:
        from kardsmem import names as N
        nm = N.NamePool(km) if hasattr(N, "NamePool") else None
        if nm is not None and hasattr(nm, "card_asset_name"):
            return nm.card_asset_name(ptr)
    except Exception:                                                     # noqa: BLE001
        pass
    try:
        from kardsmem.objects import ObjectArray
        return ObjectArray(km).obj_name(ptr)
    except Exception:                                                     # noqa: BLE001
        return None
