"""游戏卡牌效果用的随机流（`ABP_CardFunctions_C::cardsRandomStream`）的外部复刻。

来源（2026-10-01，1.60.27292.launcher，IDA + 1.60 BP 导出，**算法已读到，种子的实机验证还没做**）：
  * 引擎侧是原版 UE `FRandomStream { int32 InitialSeed @+0; int32 Seed @+4; }`；
    `FRand()`：`Seed = Seed*196314165 + 907633515 (mod 2^32)`，
    `asfloat((Seed >> 9) | 0x3F800000) - 1.0f`（float32）；
    `RandRange(lo, hi) = lo + FloorToInt(FRand() * float(hi - lo + 1))`
    （`UKismetMathLibrary::RandomIntegerInRangeFromStream` 直接调它；`n<=0` 的范围按引擎行为照算）。
  * 状态地址：`cardFunctions` 单例（`ABP_CardFunctions_C`，不是 `Default__`）上
    `cardsRandomStream @+0x2B8`（`InitialSeed +0x2B8`、`Seed +0x2BC`）；`encryptionStream @+0x2C8` 是另一条，不碰。
  * BP 包装 `RandomIntFromRangeWithStream(min,max)`（1.60 `BP_CardFunctions.cpp:12508`，已核对）：
    `d = RandRange(1,10)`；循环 `d` 次 `RandRange(0,1)`（结果丢弃）；返回 `RandRange(min,max)` ⇒ 共 `d+2` 次抽取。
  * `GetRandomCard(cards, skipCustomAlways)`（`:5315`）= `cards[RandomIntFromRangeWithStream(0, len-1)]`
    （`skipCustomAlways=False` 且有 `AlwaysSelectedAsRandom` 的牌时改从那批里选——这里按"没有这种牌"算）。
  * `Array_ShuffleFromStream`：正向 Fisher-Yates，N 次抽取（原版 UE 的写法，见 `shuffle()`）。

实机验证办法（游戏开着时，只读）：`python -m kardsmem.rng watch` 每 0.5 s 读一次 `Seed`；
若 LCG 常数对，则任意两次读数 s0→s1 必能在 N≤几千步内由 `advance()` 走到——这是对常数的强检验。
"""
from __future__ import annotations

import struct
import time
from typing import List, Optional, Sequence

MASK = 0xFFFFFFFF
MUL = 196314165
ADD = 907633515

OFF_CARDS_STREAM = 0x2B8          # ABP_CardFunctions_C 上的 cardsRandomStream（InitialSeed）
OFF_CARDS_SEED = 0x2BC            # 同上，Seed


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _asfloat(u: int) -> float:
    return struct.unpack("<f", struct.pack("<I", u & MASK))[0]


class Stream:
    """FRandomStream 的复刻（只实现用得到的几个）。`draws` 记录一共抽了几次。"""

    def __init__(self, seed: int):
        self.seed = int(seed) & MASK
        self.draws = 0

    def copy(self) -> "Stream":
        s = Stream(self.seed)
        s.draws = self.draws
        return s

    def frand(self) -> float:
        self.seed = (self.seed * MUL + ADD) & MASK
        self.draws += 1
        return _asfloat(((self.seed >> 9) | 0x3F800000)) - 1.0     # float32 减法：结果恰可表示

    def rand_range(self, lo: int, hi: int) -> int:
        """`FRandomStream::RandRange`（= `RandomIntegerInRangeFromStream`）。"""
        rng = int(hi) - int(lo) + 1
        f = self.frand()
        return int(lo) + int(_f32(f * float(rng)) // 1)            # FloorToInt(float32 乘积)

    def wrapper_int(self, lo: int, hi: int) -> int:
        """BP `RandomIntFromRangeWithStream`：`d=Rand(1,10)`，丢 d 次 `Rand(0,1)`，再 `Rand(lo,hi)`。"""
        d = self.rand_range(1, 10)
        for _ in range(d):
            self.rand_range(0, 1)
        return self.rand_range(lo, hi)

    def pick(self, seq: Sequence):
        """`GetRandomCard` 的取法：`seq[wrapper_int(0, len-1)]`（空序列不抽）。"""
        if not seq:
            return None
        return seq[self.wrapper_int(0, len(seq) - 1)]

    def shuffle(self, items: List) -> List:
        """原版 UE `Array_ShuffleFromStream`：`for i in 0..N-1: j = RandRange(i, N-1); swap(i, j)`。"""
        a = list(items)
        n = len(a)
        for i in range(n):
            j = self.rand_range(i, n - 1)
            if i != j:
                a[i], a[j] = a[j], a[i]
        return a


def advance(seed: int, n: int) -> int:
    """从 `seed` 起走 n 步的 Seed。"""
    s = seed & MASK
    for _ in range(n):
        s = (s * MUL + ADD) & MASK
    return s


def steps_between(s0: int, s1: int, limit: int = 20000) -> Optional[int]:
    """`s1` 是 `s0` 之后的第几步（找不到返回 None）——用来验证 LCG 常数。"""
    s = s0 & MASK
    for k in range(limit + 1):
        if s == (s1 & MASK):
            return k
        s = (s * MUL + ADD) & MASK
    return None


# ---------------------------------------------------------------------------
# 读活种子
# ---------------------------------------------------------------------------
_CF_CACHE: dict = {}
CF_RECHECK_S = 5.0        # 缓存的有效期：换局后 game 会新建实例，5 s 复检一次


def card_functions_ptr(ks) -> int:
    """`ABP_CardFunctions_C` 的**活实例**（`InitialSeed != 0`，跳过 CDO 与空壳）。

    ★ 2026-10-01 夜实机：同一时刻进程里有**两个** `BP_CardFunctions_C` ——
    `Logic → spawnCardFunctions(*Out)` 返回的那个 `InitialSeed=0`、`Seed` 恒 0（空壳），
    对局真正在用的是关卡 actor 里 `InitialSeed != 0` 的那个。旧实现按 GObjects 取
    "第一个非 CDO"，会拿到空壳（症状：seed 看起来永远是 0）。
    关卡 actor 只有 ~150 个，读一次 0.00 s，比扫全量 GObjects 快三个数量级。

    ★★ 再加一条（当晚第二次踩到）：**上一局的实例换局后不会立刻销毁**，它也有非 0 的
    `InitialSeed` —— 只看"非 0"会锁到上一局那条已经冻住的流（症状：seed 一直不变）。
    所以取**播种过 + `InternalIndex(+0xC) 最大`**的那个：game 每局新建实例，索引单调递增
    （实测：当次对局 149360 vs 上一局残留 128630）。缓存 5 s 过期，换局自动改判。
    """
    from kardsmem.objects import ObjectArray
    pid = getattr(ks, "pid", None)
    cached = _CF_CACHE.get(pid)
    if cached:
        p, when = cached
        if p and _alive_cf(ks, p) and (time.time() - when) < CF_RECHECK_S:
            return p
    oa = ObjectArray(ks)
    cands = []
    try:
        from .world import Locator
        for a in Locator(ks.m, ks.base).actors():
            try:
                if oa.class_name(a) in ("BP_CardFunctions_C", "ABP_CardFunctions_C"):
                    cands.append(a)
            except Exception:                                      # noqa: BLE001
                continue
    except Exception:                                              # noqa: BLE001
        cands = []
    if not cands:                       # 不在关卡里（理论上不会）：退回全量 GObjects
        for obj in (oa.find_by_class_name("BP_CardFunctions_C")
                    or oa.find_by_class_name("ABP_CardFunctions_C") or []):
            try:
                if not (oa.obj_name(obj) or "").startswith("Default__"):
                    cands.append(obj)
            except Exception:                                      # noqa: BLE001
                continue
    best = 0
    idx_best = -1
    for o in cands:
        try:
            if not ks.m.u32(o + OFF_CARDS_STREAM):                 # InitialSeed != 0
                continue
            idx = ks.m.u32(o + 0x0C)                               # UObject::InternalIndex
            if idx is not None and idx > idx_best:
                best, idx_best = o, idx
        except Exception:                                          # noqa: BLE001
            continue
    if not best and cands:                    # 都没播种（对局还没开始）：返回一个占位让调用方重试
        best = cands[0]
    if best:
        _CF_CACHE[pid] = (best, time.time())
    return best


def _alive_cf(ks, p: int) -> bool:
    try:
        # 还活着 且 仍然是"播种过"的实例 —— 空壳不算，让它去重找。
        return bool(p and ks.m.u32(p + OFF_CARDS_STREAM))
    except Exception:                                              # noqa: BLE001
        return False


def read_seed(ks) -> Optional[int]:
    """当前 `cardsRandomStream.Seed`；读不到回 None（调用方退回枚举）。"""
    try:
        p = card_functions_ptr(ks)
        if not p:
            return None
        return int(ks.m.u32(p + OFF_CARDS_SEED)) & MASK
    except Exception:                                              # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
def selftest() -> int:
    bad = 0

    def chk(name, ok):
        nonlocal bad
        print(("PASS " if ok else "FAIL ") + name)
        bad += 0 if ok else 1
    s = Stream(12345)
    # frand 落在 [0,1)；同种子可复现；advance 与 Stream 一致
    xs = [Stream(7).frand() for _ in range(3)]
    chk("frand 在 [0,1)", all(0.0 <= x < 1.0 for x in xs))
    a, b = Stream(99), Stream(99)
    chk("同种子同序列", [a.rand_range(0, 9) for _ in range(20)] == [b.rand_range(0, 9) for _ in range(20)])
    chk("advance 与 draws 一致", Stream(5).seed == 5 and (lambda st: (st.frand(), st.frand(), st.seed)[2])(Stream(5)) == advance(5, 2))
    chk("steps_between", steps_between(5, advance(5, 137)) == 137)
    r = Stream(2024)
    vals = [r.rand_range(3, 5) for _ in range(300)]
    chk("rand_range 只出 3..5 且三个值都出现", set(vals) == {3, 4, 5})
    w = Stream(1)
    n0 = w.draws
    out = w.wrapper_int(0, 4)
    chk("wrapper 抽取数 = d+2 且结果在域内", 0 <= out <= 4 and w.draws - n0 >= 3 and w.draws - n0 <= 12)
    chk("shuffle 是排列", sorted(Stream(3).shuffle(list(range(8)))) == list(range(8)))
    chk("pick 空序列不抽", Stream(3).pick([]) is None)
    return bad


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        import time
        from agent import session as S
        sess = S.AgentSession(translate=False, warm=False)
        ks = sess._kardsmem()
        prev = None
        while True:
            cur = read_seed(ks)
            if cur is not None and prev is not None and cur != prev:
                print("seed %d -> %d  steps=%s" % (prev, cur, steps_between(prev, cur)), flush=True)
            if cur is not None:
                prev = cur
            time.sleep(0.5)
    sys.exit(1 if selftest() else 0)
