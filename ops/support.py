# -*- coding: utf-8 -*-
"""ops/support.py —— 模块级辅助：开局按钮灰态开关、frida 临时目录清理、跨脚本缓存、动作流条目匹配。

由 `ops/inject.py`（P6/D1 拆分）搬出，内容与原来逐字相同。
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional



# ---------------------------------------------------------------- 开局按钮灰态开关
def play_gray_guard_on() -> bool:
    """`press_play` 要不要恢复"灰的就不点"的原判据（默认**关**）。

    ★ 2026-10-03（用户拍板）：默认不拦灰按钮。这行只决定**我们自己**发不发这次
      点击 —— 不改按钮的 `enabled`/视觉状态（界面侧照旧是灰的，UX 不变），也不碰
      服务端校验；对战/休闲的真校验在大厅/服务端（点完会被拒，游戏弹 1~2 s 的浮动
      通知"卡组错误，进入大厅失败"）。开局是否真成立，另由
      `gui.autoplay.verify_start()` 在点击后**只读**判定。
      想临时回到严格模式：设 `KARDS_PLAY_BLOCK_GRAY=1`（每次调用时读，无需重启监听器）。
    """
    return os.environ.get("KARDS_PLAY_BLOCK_GRAY", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- frida 临时目录（2026-10-02 事故）
# 每次 `frida.attach` 都会在 %TEMP% 下解压一份 `frida-<hash>`（≈43 MB 的 agent），进程被杀/崩溃时不清理 ——
# 9/25 起累计 612 份 ≈ 25.8 GB 吃满了用户 Temp。⇒ ① 本进程的 TEMP/TMP 指到专用目录（泄漏也落在那里，不污染用户 Temp）；
# ② attach 前、detach 后清掉**没被占用**的旧目录（agent dll 能以追加方式打开 ⇒ 没人在用；被占用的跳过）。
from base import paths as _paths_oi   # noqa: E402（FRIDA_TMP 要用；下面缓存路径也用它）
# 冻结的 exe 包没有伞仓库：放进可写的数据目录（`<exe 目录>/data/_frida_tmp`），别往解压目录的上一层写。
FRIDA_TMP = os.environ.get("KARDS_FRIDA_TMP") or os.path.join(
    _paths_oi.DATA if _paths_oi.FROZEN else _paths_oi.UMBRELLA, "_frida_tmp")


def _frida_tmp_setup() -> None:
    try:
        os.makedirs(FRIDA_TMP, exist_ok=True)
        os.environ["TEMP"] = os.environ["TMP"] = FRIDA_TMP
    except Exception:                                              # noqa: BLE001
        pass


def sweep_frida_tmp(min_age_s: float = 600.0) -> int:
    """删掉专用目录和用户 Temp 里**不在用**的旧 `frida-*` 目录，返回删掉的个数（失败的跳过，从不抛）。"""
    import shutil
    n = 0
    roots = {FRIDA_TMP, os.environ.get("LOCALAPPDATA", "") and os.path.join(os.environ["LOCALAPPDATA"], "Temp")}
    now = time.time()
    for root in roots:
        try:
            names = [x for x in os.listdir(root) if x.startswith("frida-")] if root and os.path.isdir(root) else []
        except Exception:                                          # noqa: BLE001
            continue
        for nm in names:
            d = os.path.join(root, nm)
            try:
                if not os.path.isdir(d) or now - os.path.getmtime(d) < min_age_s:
                    continue
                busy = False
                for dp, _dn, fn in os.walk(d):
                    for f in fn:
                        if f.lower().endswith(".dll"):
                            try:
                                with open(os.path.join(dp, f), "ab"):
                                    pass
                            except OSError:
                                busy = True                          # 被某个进程加载着 ⇒ 在用，整个目录跳过
                                break
                    if busy:
                        break
                if not busy:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 0 if os.path.exists(d) else 1
            except Exception:                                      # noqa: BLE001
                continue
    return n





# ---------------------------------------------------------------------------
# 跨脚本缓存（2026-09-25：用户指出的真问题——每次 `python -c`/`python ops/inject.py`
# 都是全新进程，`Injector` 实例内的 `_fn_cache`/`_off_cache`/class 缓存一次都没能
# 跨脚本复用，逼着每个短命脚本都从头再扫一遍。
#
# ★ 关键前提：`UClass`/`UFunction` 是**引擎启动时创建、进程存活期内地址不变**的
#   反射元数据（不是每局对局都会重建的东西）——只要**目标游戏进程没重启**
#   （`pid` 不变），这些指针在我们自己这边不同 `python` 进程之间复用是安全的。
#   `字段偏移`（`props.find_prop` 算出来的）更稳，跟指针无关，永远能复用。
#   ⚠ **不缓存实例指针**（手牌/牌组按钮/换牌UI 这些每次进新的一屏/一局都可能是
#   新对象）——只缓存"类→类对象""类+字段名→偏移""类+函数名→UFunction*"这三样，
#   这些是本来就该跨对局稳定的东西。缓存文件里存的 `pid` 跟当前游戏进程一对不上
#   （比如重启过客户端）就整个作废重扫，不会拿旧进程的指针误用到新进程上。
# ---------------------------------------------------------------------------
from base import paths as _paths_oi
_CACHE_PATH = os.path.join(_paths_oi.DATA, "ops_inject_cache.json")          # 运行期缓存放数据目录，不放源码树


def _load_persistent_cache(pid: int) -> Optional[dict]:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                        # noqa: BLE001
        return None
    if data.get("pid") != pid:
        return None
    try:
        fn_cache = {}
        for k, v in data.get("fn_cache", {}).items():
            uc, name, inh = k.split("|", 2)
            fn_cache[(int(uc), name, bool(int(inh)))] = v
        off_cache = {}
        for k, v in data.get("off_cache", {}).items():
            uc, name = k.split("|", 1)
            off_cache[(int(uc), name)] = v
        return {"class_cache": dict(data.get("class_cache", {})),
                "fn_cache": fn_cache, "off_cache": off_cache}
    except Exception:                                        # noqa: BLE001
        return None


def _save_persistent_cache(pid: int, class_cache: dict, fn_cache: dict, off_cache: dict) -> None:
    try:
        enc_fn = {"%d|%s|%d" % (k[0], k[1], int(k[2])): v for k, v in fn_cache.items()}
        enc_off = {"%d|%s" % k: v for k, v in off_cache.items()}
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "class_cache": class_cache,
                      "fn_cache": enc_fn, "off_cache": enc_off}, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception:                                        # noqa: BLE001
        pass


def _action_has_card(row: dict, card_id: int) -> bool:
    """动作流一条里有没有提到这个 cardID（我方条目是数字键、对手是具名键）。"""
    for v in (row.get("data") or []):
        name = (v.get("name") or "")
        txt = v.get("text")
        val = v.get("value")
        if name in ("cardID", "attackerCardID", "cardId") and val == card_id:
            return True
        if name in ("0", "1") and (txt == str(card_id) or val == card_id):
            return True
    return False
