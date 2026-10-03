"""预报（天气三选一）候选的 **Python 池版**预测器 —— 2026-10-01 实机 3/3 坐实。

为什么要有它（PLAN 阶段 4）：`semantics/forecast.py` 走的是"外部 VM 跑蓝图的
`GetChooseSpawnCards`"，重且慢；而这张卡的逻辑其实**纯数据 + 一次 LCG**，可以照抄成
Python：池子从内存读，三张候选用 `kardsmem.rng.Stream` 直接算。

游戏侧原文（`card_event_sunny1_blue_sky_C::GetChooseSpawnCards`，1.60 导出）：
    for c in GetAllActiveStaticCards(true, true):        # = GameState+0x670 那张按名字排序的静态表
        if !c.HasTag("subtype.<天气>"):        continue
        if  c.HasTag("subtype.heavyWeather"):  heavy.add(c)
        elif c.HasTag("subtype.mediumWeather"): medium.add(c)
        elif c.HasTag("subtype.lightWeather"): light.add(c)      # 其它标签的牌被丢掉
    i1 = RandomIntFromRangeWithStream(0, light.len-1)    # 依次 light → medium → heavy，
    i2 = RandomIntFromRangeWithStream(0, medium.len-1)   # 每次 d+2 次抽取（见 rng.Stream.wrapper_int）
    i3 = RandomIntFromRangeWithStream(0, heavy.len-1)    # ⇒ 一共 3 次调用、9~36 次抽取

实机对账（`_nn_scratch/rng_ext2.jsonl` + `rule-live-20261001-223311.jsonl`，3 次预报）：
    pre-seed 1053557799 → 预测 {heatwave, jungle_fever2, scorching_sun}，实际选中 heatwave，抽取 31 = 实测 31
    pre-seed 4188953836 → 预测 {heatwave2, jungle_fever, scorching_sun}，实际 heatwave2，抽取 22 = 实测 22
    pre-seed 3115859106 → 预测 {heatwave2, jungle_fever2, scorching_sun3}，实际 heatwave2，抽取 15 = 实测 15

★ 验证状态（2026-10-01 深夜，已全部验过）：
    * 机器人局回测 3/3：每次预报的 **light 档**都命中，且总抽取数 = 实测种子步数（31/22/15）；
    * **用户手动局全量验证**（`_nn_scratch/forecast_validation_report.md` + 截图）：种子 4196626795 →
      预测 rain = {暴雨 DELUGE, 骤雨 TORRENTIAL RAIN, 季风雨 MONSOON RAIN}，面板实际显示**逐张、按同一顺序**一致；
      面板出现后种子走到 2626744231 = **正好 28 步**（= 预测抽取数）。
      ⇒ **三档（light/medium/heavy）+ "UI 顺序 = 桶顺序" + 抽取数**三件全部坐实。

★★ 别按**名字**比对（用户 2026-10-01 点破）：同一(天气×花费)有 **3 个同名、效果完全不同**的变体，
    例如 DELUGE 的 `deluge`（随机分 6 防御）/ `deluge2`（全体敌方 -1 攻）/ `deluge3`（抑制+抽牌）。
    本模块返回的是**具体变体指针**，所以预测是对的；但下游评估/报告一律要用**变体**（或它的效果文本），
    拿"暴雨"当答案等于没区分。核对方法见 `_nn_scratch/forecast_validation_report.md`（用 BP 导出的
    `FText Text` + locres 中文逐张比屏幕文本）。

只读：`ReadProcessMemory`；不写游戏、不注入。

用法：
    python -m kardsmem.forecast_pool --live              # 读当前进程的池 + 当前种子，打印 9 条路径
    python -m kardsmem.forecast_pool --seed 12345 --json  # 用给定种子算（池仍从进程读）
    python -m kardsmem.forecast_pool --selftest           # 纯离线：池/抽取逻辑的确定性断言
"""
from __future__ import annotations

import json
import struct
import sys

from .rng import Stream

WEATHERS = ("sunny", "rain", "storm")
TIERS = ("light", "medium", "heavy")
TIER_TAG = {"light": "subtype.lightWeather", "medium": "subtype.mediumWeather",
            "heavy": "subtype.heavyWeather"}


def strip_serial(name: str) -> str:
    """`card_event_sunny2_heatwave_C_2147480180` → `card_event_sunny2_heatwave_C`。"""
    if not name:
        return ""
    i = name.rfind("_C_")
    return name[:i + 2] if i >= 0 else name


def read_weather_buckets(ks: dict) -> dict:
    """读 `GameState+0x670`（AllStaticCardsSortedByName）→ 每种天气的 light/medium/heavy 三个桶。

    顺序 = 数组顺序（也就是蓝图的遍历顺序）；桶里存的是卡对象指针。
    实测 2019 张卡扫一遍 ~0.08 s（每张一次标签读）。
    """
    from .cards import read_gameplay_tags
    from .gs import load as load_gs
    gs = load_gs(ks)
    if not gs.available:
        raise RuntimeError("GameState 不可用（不在对局/还没加载）")
    t = gs.static_card_table()
    data, num = t["ptr"], t["num"] or 0
    if not data or not num:
        raise RuntimeError("静态卡表为空：%r" % (t,))
    blob = ks.m.read_exact(data, num * 8)
    if not blob:
        raise RuntimeError("静态卡表读不出来（ptr=%#x num=%d）" % (data, num))
    ptrs = [struct.unpack_from("<Q", blob, i)[0] for i in range(0, len(blob), 8)]
    buckets = {w: {t_: [] for t_ in TIERS} for w in WEATHERS}
    for p in ptrs:
        tags = read_gameplay_tags(ks, p)
        if not tags:
            continue
        for w in WEATHERS:
            if ("subtype." + w) not in tags:
                continue
            for tier in ("heavy", "medium", "light"):     # 蓝图的判定顺序：heavy → medium → light
                if TIER_TAG[tier] in tags:
                    buckets[w][tier].append(p)
                    break
    return buckets


def predict_ptrs(seed: int, buckets: dict) -> dict:
    """三张候选（指针）：`{天气: {"light": ptr, "medium": ptr, "heavy": ptr, "draws": N}}`。

    注意：**一次预报只消费一条路径的抽取**（玩家只选一种天气），所以三种天气各自
    从同一个种子起算 —— 这正是"9 条路径"能同时算出来的原因。
    """
    out = {}
    for w in WEATHERS:
        st = Stream(seed)
        row = {}
        for tier in TIERS:
            b = buckets[w][tier]
            row[tier] = b[st.wrapper_int(0, len(b) - 1)] if b else 0
        row["draws"] = st.draws
        out[w] = row
    return out


def predict_names(ks: dict, seed: int, buckets: dict | None = None) -> dict:
    """同上，但把指针换成卡名（`_C_<序号>` 后缀已去掉）。"""
    buckets = buckets if buckets is not None else read_weather_buckets(ks)
    pool = ks.names_pool()
    res = {}
    for w, row in predict_ptrs(seed, buckets).items():
        res[w] = {"draws": row["draws"]}
        for tier in TIERS:
            p = row[tier]
            res[w][tier] = strip_serial(pool.fname_of(p) if p else "") or None
    return res


# --------------------------------------------------------------------------- selftest
def selftest() -> int:
    bad = 0

    def chk(name, ok):
        nonlocal bad
        print(("PASS " if ok else "FAIL ") + name)
        bad += 0 if ok else 1

    fake = {w: {t: [1, 2, 3] for t in TIERS} for w in WEATHERS}
    a = predict_ptrs(2024, fake)
    b = predict_ptrs(2024, fake)
    chk("同种子同一结果", a == b)
    chk("三种天气都算了", set(a) == set(WEATHERS))
    chk("抽取数 9..36（3 次 wrapper）", all(9 <= a[w]["draws"] <= 36 for w in WEATHERS))
    chk("每档都从该桶里取", all(a[w][t] in (1, 2, 3) for w in WEATHERS for t in TIERS))
    c = predict_ptrs(2025, fake)
    chk("换种子结果会变（至少一档）", any(a[w][t] != c[w][t] for w in WEATHERS for t in TIERS))
    # 桶长 1 时索引恒 0 且照样抽（游戏行为：RandRange(0,0) 也消耗随机数）
    one = {w: {t: [7] for t in TIERS} for w in WEATHERS}
    d = predict_ptrs(99, one)
    chk("桶长 1 → 仍抽到唯一元素", all(d[w][t] == 7 for w in WEATHERS for t in TIERS))
    # 抽取数只由 d=Rand(1,10) 决定，与桶长无关（同一个种子下必须一致）
    chk("桶长 1 → 抽取数与桶长 3 相同",
        d["sunny"]["draws"] == predict_ptrs(99, fake)["sunny"]["draws"])
    chk("strip_serial", strip_serial("card_x_C_123") == "card_x_C"
        and strip_serial("") == "" and strip_serial("plain") == "plain")
    return bad


def _main(argv) -> int:
    seed = None
    as_json = "--json" in argv
    if "--selftest" in argv:
        return 1 if selftest() else 0
    if "--seed" in argv:
        seed = int(argv[argv.index("--seed") + 1], 0)
    from kardsmem import attach
    from kardsmem.rng import read_seed
    ks = attach()
    if seed is None:
        seed = read_seed(ks)
        if seed is None:
            print("读不到活种子（不在对局？）", file=sys.stderr)
            return 2
    buckets = read_weather_buckets(ks)
    sizes = {w: {t: len(buckets[w][t]) for t in TIERS} for w in WEATHERS}
    res = predict_names(ks, seed, buckets)
    out = {"seed": seed & 0xFFFFFFFF, "bucket_sizes": sizes, "predict": res}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("seed=%d  桶：%s" % (seed & 0xFFFFFFFF, sizes))
        for w in WEATHERS:
            print("  %-6s %s" % (w, " / ".join(res[w][t] or "?" for t in TIERS)))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
