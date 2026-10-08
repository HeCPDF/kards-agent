#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牌库/手牌的造牌与搬牌（`engine/natives/deck.py`）+ 对账修出来的几处语义（2026-10-06，卡牌创建/牌库/手牌簇）。

每条都对应一个**原版 BP 出处**（见 `engine/natives/deck.py` 文件头）与一次对账里字典路(A)/直跑(B)/重放(C)的分歧：

  * `SpawnCardInDeckBySide(shuffle=true)` 只洗**一遍**（字典路以前洗两遍：塞牌段 + `deck_shuffle` 段 ⇒ DUG IN/LAST RITES 的 deck 分歧）；
  * `SpawnCardinHandbySide` 是**造牌进手**，不是抽牌（以前走抽牌链 ⇒ 牌库少一张）；
  * `MoveCardToTopOfOwnersDeck(手牌 id)`：字典路以前只留 `to_deck=True`、没有目标就整个丢（SHIFTING DOCTRINE）；
  * 抽牌**按脚本顺序**（DEFEND THE NATION 先抽再洗、SHIFTING DOCTRINE 先洗再抽）+ `DrawSpecificCardFromDeckBySide` 抽**指定**那张；
  * `DiscardCardFromHand` 实参是 card ID（以前直跑当指针 ⇒ 弃牌从不结算）+ 出参 `success`；
  * 脚本里 `GetCardsInHandBySide` 看不到**正在打出的那张牌**（原版先挪走再 `OnPlayedFromHand`）；
  * 新记录（`deck_add`/`deck_shuffle`/`to_deck`/`gain_hand`/`discard`/`convert_hand`/`convert_deck`/`draw_specific`）重放 ≡ 直跑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import calls as CALLS                                    # noqa: E402
from engine import effectvm as EV                                    # noqa: E402
from engine import scripts as SC                                     # noqa: E402
from engine import state as S                                        # noqa: E402
from engine.natives import deck as D                                 # noqa: E402
from kardsmem.rng import Stream                                      # noqa: E402
from sim import engine as SE                                         # noqa: E402

ME, OPP = 1, 2
SEED = 4242
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


def mk(deck=(91, 92, 93, 94), hand_ids=(21, 22, 23)):
    s = S.Sim({}, {ME: 20, OPP: 20}, 5, {}, my_side=ME, slots=3, kredit_max=24, deck=list(deck), rng_seed=SEED)
    s.hand = {i: S.H(i, "H%d" % i, 2, "order") for i in hand_ids}
    s.deck_cards = {}
    s.card_templates = {}
    return s


def call(rec, verb, *args):
    return rec.hook(verb)(None, None, None, list(args), None)


def snap(s):
    d = SC._state_snapshot(s)
    return d.get("deck"), {k: v for k, v in d.items() if k[0] == "h" and k != "hq"}


def main():
    # ---- ① SpawnCardInDeckBySide：塞牌段（每张都耗一次随机流；位置口径） ----
    s = mk()
    ref = Stream(SEED)
    placed = D.spawn_in_deck(s, "NEW", 2, rng=s.rng)
    ref.wrapper_int(0, 4)
    ref.wrapper_int(0, 5)
    chk("塞 2 张：每张耗一次 `wrapper_int(0, 当前牌库张数)`（BP 无条件抽；不抽会让之后的洗牌错位）",
        s.rng.draws == ref.draws, "draws=%s ref=%s" % (s.rng.draws, ref.draws))
    chk("塞 2 张（`bottom=false`）⇒ 都放牌顶，后塞的在上；id = -(3000+序号)",
        s.deck[:2] == [-3002, -3001] and [p[0] for p in placed] == [-3001, -3002], "deck=%s placed=%s" % (s.deck, placed))
    s = mk()
    D.spawn_in_deck(s, "NEW", 1, bottom=True, rng=s.rng)
    chk("`bottom=true` ⇒ 放牌库底", s.deck[-1] == -3001, str(s.deck))
    s = mk()
    ref = Stream(SEED)
    pos = ref.wrapper_int(0, 4)
    D.spawn_in_deck(s, "NEW", 1, random_wo_shuffle=True, rng=s.rng)
    chk("`RandomWithoutShuffle=true` ⇒ 插到随机位置（位置 = 那次 `wrapper_int` 的结果）", s.deck.index(-3001) == min(4, pos),
        "pos=%s deck=%s" % (pos, s.deck))
    s = mk()
    s.deck_known = False
    g = []
    chk("牌库未知 ⇒ 不塞并记缺口", D.spawn_in_deck(s, "NEW", 1, rng=s.rng, gaps=g) == [] and g, str(g))

    # ---- ② 字典路 vs 直跑：`SpawnCardInDeckBySide(shuffle=true)` 只洗一遍（DUG IN） ----
    eff = {"deck_add": 1, "deck_add_side": ME, "deck_add_name": "SHIFT", "deck_add_bottom": False, "deck_add_shuffle": True,
           "deck_add_wo_shuffle": False, "deck_shuffle": ME, "deck_shuffles": 1, "deck_shuffle_skip": True}
    sa = mk()
    SE._apply_eff(sa, dict(eff), None)
    sb = mk()
    rng_b = Stream(SEED)
    h = SC.native_hooks(sb, my_side=ME, rng=rng_b)
    h["SpawnCardInDeckBySide"](None, None, None, [ME, "SHIFT", 7, 1, 0, False, False, True, False, False, False], None)
    chk("DUG IN 型：塞 1 张并洗牌 ⇒ 字典路 ≡ 直跑（牌序一致）", sa.deck == sb.deck, "A=%s B=%s" % (sa.deck, sb.deck))
    chk("DUG IN 型：两条路耗掉的随机流次数一致（字典路没有多洗一遍）", sa.rng.draws == rng_b.draws,
        "A=%d B=%d" % (sa.rng.draws, rng_b.draws))
    # 同一张牌**另外**又洗了一次（`deck_shuffles=2`）⇒ 才多洗一遍
    sc = mk()
    SE._apply_eff(sc, dict(eff, deck_shuffles=2), None)
    chk("`deck_shuffles=2`（塞牌自带一次 + 脚本另洗一次）⇒ 比单洗多耗一遍流", sc.rng.draws > sa.rng.draws,
        "%d vs %d" % (sc.rng.draws, sa.rng.draws))
    # 手写旧 eff（没有 `deck_shuffles`）：塞牌自带的洗牌 + `deck_shuffle` ⇒ 总共一遍（不再双洗）
    sd = mk()
    old = {k: v for k, v in eff.items() if k != "deck_shuffles"}
    SE._apply_eff(sd, old, None)
    chk("旧记法（无 `deck_shuffles`）⇒ 塞牌自带的那遍就是全部，不再双洗", sd.deck == sa.deck, "%s vs %s" % (sd.deck, sa.deck))

    # ---- ③ SpawnCardInDeck 多次（COLOSSUS 循环 3 次，以前 `deck_add` 被覆盖成 1） ----
    r = EV.Recorder(0x2000, 0)
    for nm in ("A_CARD", "B_CARD", "B_CARD"):
        call(r, "SpawnCardInDeckBySide", ME, nm, 7, 1, 0, False, False, False, False, False, None)
    e = EV.to_effects(r, my_side=ME)
    chk("录制：三次塞牌 ⇒ `deck_add=3` 且 `deck_adds` 逐次记下各自卡名", e.get("deck_add") == 3
        and [x["name"] for x in e.get("deck_adds", [])] == ["A_CARD", "B_CARD", "B_CARD"], str(e))
    sa = mk()
    SE._apply_eff(sa, e, None)
    chk("字典路按序结算三次 ⇒ 牌库多 3 张（以前只多 1 张）", sum(1 for c in sa.deck if c < -3000) == 3, str(sa.deck))

    # ---- ④ SpawnCardInHandBySide：造牌进手，不是抽牌 ----
    sa = mk()
    SE._apply_eff(sa, {"gain_cards": 1, "gain_names": ["card_unit_t_34"]}, None)
    chk("字典路：`gain_cards` ⇒ 手里多一张**按名字造的新牌**、牌库**原样**（以前走抽牌链：牌库少一张）",
        sa.deck == [91, 92, 93, 94] and len(sa.hand) == 4 and any(x.name == "card_unit_t_34" for x in sa.hand.values()),
        "deck=%s hand=%s" % (sa.deck, {k: v.name for k, v in sa.hand.items()}))
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME)
    h["SpawnCardInHandBySide"](None, None, None, [ME, "card_unit_t_34", 7, True, False, False, "", None, 0, None], None)
    chk("直跑：同上（`spawnerID>0` 才干活）", sb.deck == [91, 92, 93, 94] and len(sb.hand) == 4, str(list(sb.hand)))
    chk("字典路 ≡ 直跑（手牌快照一致）", snap(sa) == snap(sb), "%s vs %s" % (snap(sa), snap(sb)))
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME)
    h["SpawnCardInHandBySide"](None, None, None, [ME, "card_unit_t_34", 0, True, False, False, "", None, 0, None], None)
    chk("`spawnerID<=0` ⇒ 什么都不做（原版 `:6384`）", len(sb.hand) == 3)
    sf = mk(hand_ids=tuple(range(21, 31)))
    g = []
    chk("手牌已满 ⇒ 不加并记缺口", D.spawn_in_hand(sf, "X", gaps=g) is None and g and len(sf.hand) == 10, str(g))

    # ---- ⑤ MoveCardToTopOfOwnersDeck(手牌 id)：录制要带上 id；字典路要真移 ----
    r = EV.Recorder(0x2000, 0)
    call(r, "MoveCardToTopOfOwnersDeck", 22, 7, 0, None)
    e = EV.to_effects(r, my_side=ME)
    chk("录制：`MoveCardToTopOfOwnersDeck(22,…,0)` ⇒ `to_deck_hand_ids=[22]`（以前只有 `to_deck=True`，没目标就丢）",
        e.get("to_deck_hand_ids") == [22] and e.get("to_deck"), str(e))
    sa = mk()
    SE._apply_eff(sa, e, None)
    chk("字典路：22 离开手牌、放到牌库顶", 22 not in sa.hand and sa.deck[0] == 22, "hand=%s deck=%s" % (list(sa.hand), sa.deck))
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME)
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [22, 7, 0, None], None)
    chk("直跑 ≡ 字典路", snap(sa) == snap(sb))
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME)
    h["MoveCardToTopOfOwnersDeck"](None, None, None, [22, 7, 2, None], None)
    chk("`positionFromTop=2` ⇒ 插到下标 2（以前一律牌顶）", sb.deck.index(22) == 2, str(sb.deck))
    sa = mk()
    SE._apply_eff(sa, {"to_deck_aoe_ids": [21, 22], "to_deck_position": 0, "to_deck_aoe": True}, None)
    chk("`MoveMultipleCardsToTopOfOwnersDeck(手牌 id)` ⇒ 手牌也能搬（以前字典路只认场上单位）；逐张插牌顶 ⇒ 后搬的在上",
        not ({21, 22} & set(sa.hand)) and sa.deck[:2] == [22, 21], "hand=%s deck=%s" % (list(sa.hand), sa.deck))

    # ---- ⑥ 抽牌按脚本顺序 + 指定抽哪张 ----
    draws = []

    def fake_draw(st, n):
        for _ in range(n):
            st.draws_done += 1
            cid = st.deck.pop(0)
            st.hand[cid] = S.H(cid, "D%d" % cid, 1, "order")
            draws.append(cid)

    r = EV.Recorder(0x2000, 0)
    call(r, "DrawSpecificCardFromDeckBySide", 7, 93, ME, False)
    call(r, "ShuffleDeckBySide", ME, False, 7, None)
    e = EV.to_effects(r, my_side=ME)
    chk("录制：先抽指定牌、再洗牌 ⇒ `draw_seq=[93]`、`draw_pre=1`", e.get("draw_seq") == [93] and e.get("draw_pre") == 1, str(e))
    r = EV.Recorder(0x2000, 0)
    call(r, "ShuffleDeckBySide", ME, False, 7, None)
    call(r, "DrawCardsFromDeckBySide", 7, ME, 2, False, False, None, 0.4)
    e2 = EV.to_effects(r, my_side=ME)
    chk("录制：先洗再抽 2 ⇒ `draw_seq=[None,None]`、`draw_pre` 缺省（0）", e2.get("draw_seq") == [None, None]
        and not e2.get("draw_pre"), str(e2))
    # 直跑
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME, rng=Stream(SEED), on_draw=lambda st, n: _chain(st, n))
    ctx = h["__ctx__"]
    h["DrawSpecificCardFromDeckBySide"](None, None, None, [7, 93, ME, False], None)
    chk("直跑 `DrawSpecificCardFromDeckBySide(93)` ⇒ 抽到的是 93（不是牌库顶 91）", 93 in sb.hand and 91 not in sb.hand,
        "hand=%s deck=%s" % (list(sb.hand), sb.deck))
    chk("  …… 记 draw_specific 93", ("draw_specific", 93) in ctx.applied, str(ctx.applied))
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME, rng=Stream(SEED), on_draw=lambda st, n: _chain(st, n))
    h["DrawSpecificCardFromDeckBySide"](None, None, None, [7, 555, ME, False], None)
    chk("指定的牌不在牌库 ⇒ 不抽（原版 `Array_Contains` 假就 return）", len(sb.hand) == 3 and sb.deck == [91, 92, 93, 94])
    # 字典路：先抽后洗 ≡ 直跑（用同一个种子；抽牌链走 sim 自己的）
    sa = mk()
    SE._apply_eff(sa, dict(e), None)
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME, rng=Stream(SEED), on_draw=lambda st, n: _chain(st, n))
    h["DrawSpecificCardFromDeckBySide"](None, None, None, [7, 93, ME, False], None)
    h["ShuffleDeckBySide"](None, None, None, [ME, False, 7, None], None)
    chk("先抽指定牌再洗牌：字典路 ≡ 直跑（剩余牌库牌序一致；93 不在牌库里了、手里多一张）",
        sa.deck == sb.deck and 93 not in sa.deck and len(sa.hand) == 4,
        "A=%s/%s B=%s/%s" % (sa.deck, list(sa.hand), sb.deck, list(sb.hand)))

    # ---- ⑦ DiscardCardFromHand：实参是 card ID；出参 success ----
    sb = mk()
    h = SC.native_hooks(sb, my_side=ME)
    ctx = h["__ctx__"]
    frame = type("F", (), {"locals": {}})()
    e_ = type("E", (), {"kids": [type("K", (), {"args": {"prop": nm}})() for nm in ("c", "d", "s", "f", "success")]})()
    h["DiscardCardFromHand"](None, frame, None, [22, 7, False, False, None], e_)
    chk("`DiscardCardFromHand(22)` ⇒ 22 离开手牌（实参是 card id，不是指针）", 22 not in sb.hand and ("discard", 22) in ctx.applied,
        str(ctx.applied))
    chk("  …… 出参 `success=true`（脚本靠它计数：IRON VICTORY 每弃一张补一张 T-34）", frame.locals.get("success") is True,
        str(frame.locals))
    r = EV.Recorder(0x2000, 0)
    frame = type("F", (), {"locals": {}})()
    r.hook("DiscardCardFromHand")(None, frame, None, [22, 7, False, False, None], e_)
    chk("录制路同口径：正 id ⇒ `success=true`", frame.locals.get("success") is True, str(frame.locals))
    r = EV.Recorder(0x2000, 0)
    frame = type("F", (), {"locals": {}})()
    e9 = type("E", (), {"kids": [type("K", (), {"args": {"prop": "k%d" % i}})() for i in range(10)]})()
    r.hook("SpawnCardInHandBySide")(None, frame, None, [ME, "X", 7, True, False, False, "", None, 0, None], e9)
    chk("录制路：`SpawnCardInHandBySide` 写出参 `spawnedCardID`（下标 9），否则后面的 `GetCardFromID(None)` 会停链",
        isinstance(frame.locals.get("k9"), int) and frame.locals["k9"] < 0, str(frame.locals))

    # ---- ⑧ 正在打出的牌不在手牌里（`make_read_hooks(played_ptr=)`） ----
    class _C:
        def __init__(self, ptr, inhand):
            self.raw = {"ptr": ptr}
            self.obj = type("O", (), {"InHand": lambda _s: inhand, "side": ME})()

    st = type("St", (), {"cards": [_C(0x10, True), _C(0x20, True), _C(0x30, False)], "slots": {}, "kredits": {}})()
    hk = EV.make_read_hooks(st, ME)["GetCardsInHandBySide"]
    got_all = hk(None, None, None, [ME], None)
    hk2 = EV.make_read_hooks(st, ME, played_ptr=0x10)["GetCardsInHandBySide"]
    got_ex = hk2(None, None, None, [ME], None)
    chk("`GetCardsInHandBySide`：默认含手里所有牌；`played_ptr` ⇒ 排除正在打出的那张（原版 `PlayCardFromHand` 先挪走）",
        sorted(got_all) == [0x10, 0x20] and got_ex == [0x20], "%s / %s" % (got_all, got_ex))

    # ---- ⑨ 重放：新记录 ≡ 直跑 ----
    def tri(label, build, verb, args, **kw):
        s1 = build()
        h1 = SC.native_hooks(s1, my_side=ME, **kw)
        ctx1 = h1["__ctx__"]
        h1[verb](None, None, None, list(args), None)
        s2 = build()
        res = CALLS.apply_calls(s2, list(ctx1.applied), on_draw=kw.get("on_draw"))
        a, b = snap(s1), snap(s2)
        extra = [g for g in res.gaps if g not in ctx1.gaps]
        chk("重放 ≡ 直跑：%s" % label, a == b and not extra and ctx1.applied, "applied=%s diff=%s extra=%s" % (ctx1.applied[:2], (a, b) if a != b else "", extra))

    tri("SpawnCardInDeckBySide（塞 2 张 + 洗牌）", mk, "SpawnCardInDeckBySide",
        [ME, "NEW", 7, 2, 0, False, False, True, False, False, False], rng=Stream(SEED))
    tri("ShuffleDeckBySide", mk, "ShuffleDeckBySide", [ME, False, 7, None], rng=Stream(SEED))
    tri("SpawnCardInHandBySide", mk, "SpawnCardInHandBySide", [ME, "card_unit_t_34", 7, True, False, False, "", None, 0, None])
    tri("MoveCardToTopOfOwnersDeck(pos=1)", mk, "MoveCardToTopOfOwnersDeck", [22, 7, 1, None])
    tri("MoveMultipleCardsToTopOfOwnersDeck", mk, "MoveMultipleCardsToTopOfOwnersDeck", [[21, 22], 7, 0, None])
    tri("DiscardCardFromHand", mk, "DiscardCardFromHand", [22, 7, False, False, None])
    tri("DrawSpecificCardFromDeckBySide(93)", mk, "DrawSpecificCardFromDeckBySide", [7, 93, ME, False],
        on_draw=lambda st, n: _chain(st, n))
    cv = {"ids": [22], "name": "MARINES", "cost": 1, "typ": "infantry", "atk": 2, "dfn": 3, "kw": ("guard",)}
    for label, ids, build in (("ConvertCard（手牌）", [22], mk), ("ConvertCard（牌库）", [92], mk)):
        s1 = build()
        h1 = SC.native_hooks(s1, my_side=ME, payload={"convert": dict(cv, ids=ids)})
        ctx1 = h1["__ctx__"]
        h1["ConvertCard"](None, None, None, [ids, 7, "MARINES", 0, False], None)
        s2 = build()
        res = CALLS.apply_calls(s2, list(ctx1.applied))
        extra = [g for g in res.gaps if g not in ctx1.gaps]          # 直跑自己声明过的缺口（效果未知）重放同样会再记一份，不算新增
        chk("重放 ≡ 直跑：%s" % label, snap(s1) == snap(s2) and not extra and ctx1.applied,
            "applied=%s a=%s b=%s gaps=%s" % (ctx1.applied[:1], snap(s1), snap(s2), res.gaps[:1]))

    print("\n失败 %d 项" % fails)
    return 1 if fails else 0


def _chain(st, n):
    """测试用抽牌链：牌库顶 → 手牌（真 id）。"""
    for _ in range(n):
        if not st.deck:
            return
        cid = st.deck.pop(0)
        st.draws_done += 1
        st.hand[cid] = S.H(cid, "D%d" % cid, 1, "order")


if __name__ == "__main__":
    sys.exit(main())
