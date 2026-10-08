#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3'（P1 字段别名迁移）回归：**已迁走的那批别名，渲染/查询路径一个都不许再碰**。

`tests/test_field_alias_ratchet.py` 只管"别名总数只许减"（AST 计数，看得见 `.card_id` 这种
真·属性读，看不见 `getattr(c, "card_id")`）。本文件补上它管不着的两件事：

  1. **口径**：对每个已迁别名，`Card.<别名>` 必须**等于**迁移时改用的原版读法
     （下表 `MIGRATED` 就是迁移时的口径台账，逐条对照 `kardsmem/board.py` 的别名定义）。
  2. **反向**：把那批别名换成"一读就抛"的子类 `BanAliases`，让 `agent/view.py` / `kardsmem/cards.py`
     的渲染与查询路径**真跑一遍** —— 结果必须与普通 `Card` 的盘面**一字不差**。
     这比 AST 计数强：`getattr(c, "card_id", None)` 那种隐形读法在棘轮里是 0，在这里会当场炸。

离线，不需要游戏；不建窗口（`tests/run_all.py` 的硬约定）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from kardsmem import gamemodel as GM                          # noqa: E402
from kardsmem import cards as C                               # noqa: E402
from kardsmem.board import BoardState, Card                   # noqa: E402
from agent import view                                        # noqa: E402

ME, OPP = GM.ESide.left, GM.ESide.right
fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    fails += 0 if ok else 1


# --------------------------------------------------------------------------
# 一、口径台账：别名 → 迁移时改用的**原版读法**
#    （唯一依据：`kardsmem/board.py::Card` 里那些 `@property` 的函数体，以及
#      `kardsmem/gamemodel.py::BaseCardObject` 的字段/getter 定义）
# --------------------------------------------------------------------------
MIGRATED = {
    "card_id": lambda o: o.CardID,
    "attack_buff": lambda o: o.attackBuff,
    "max_attack": lambda o: o.maxAttack,
    "max_defense": lambda o: o.maxDefense,
    "rarity_enum": lambda o: None if o.rarity is None else int(o.rarity),
    "card_set_enum": lambda o: o.cardSet,
    "enter_play_on_turn": lambda o: o.enterPlayOnTurn,
    "kredits_tax_as_enemy_target": lambda o: o.KreditsTax_AsEnemyTarget,
    "is_suppressed": lambda o: o.isSuppressed,
    "operation_cost": lambda o: o.operationCost,
    "operation_cost_buff": lambda o: o.operationCostBuff,
    "needs_hand_target": lambda o: o.selectTargetOnPlayedFromHand,
    "kredit_cost": lambda o: o.getTotalKredits(),          # 含 buff、夹 [0,99]，**不是** o.kredits
    "target_uid": lambda o: ("0x%X" % o.CurrentTarget) if o.CurrentTarget else None,
    "is_being_guarded": lambda o: o.isBeingGuarded,
    "under_enemy_control": lambda o: o.underEnemyControl,
    "has_been_attacked_this_turn": lambda o: o.hasBeenAttackedThisTurn,
}

#: 迁移时**故意没迁**的（不在这份台账里，别误当漏迁）：
#:   * `keywords` —— 计算属性（`_KEYWORD_FLAGS` 拼出来的列表），**不是**某个原版字段的同名别名
#:     （原版只有 `GetCombatKeywords(TArray<ECombatKeyword>*)` 这个函数）；
#:   * `name`/`side`/`slot`/`attack`/`defense`/`card_type`/`total_*` —— 棘轮本来就 EXCLUDED；
#:   * `getattr(x, "card_id", None)` 这类**鸭子类型**读法（`agent/precheck.py`、`agent/session.py::_cid`、
#:     `kardsmem/cards.py:936` 的"Card 或 read_raw dict"双模入参）—— 改成 `obj` 会破掉"收 int / 轻量对象"的契约。


def _obj(**kw):
    o = GM.BaseCardObject(**kw)
    return o


def _mk(cls, uid, **kw):
    """造一张真卡（`obj` 是原版 `BaseCardObject`）；`cls=BanAliases` 时别名一读就炸。"""
    return cls(uid=uid, obj=_obj(**kw), raw={})


def _full(uid, side, loc, cid, **extra):
    kw = dict(CardID=cid, Type=GM.EType.infantry, Location=loc, side=side, locationNumber=1,
              attack=3, attackBuff=2, defense=4, maxDefense=8, maxAttack=9,
              kredits=5, kreditsBuff=-1, operationCost=2, operationCostBuff=1,
              KreditsTax_AsEnemyTarget=3, isSuppressed=True, selectTargetOnPlayedFromHand=True,
              enterPlayOnTurn=2, rarity=GM.ERarity(1), cardSet=4, huge=None,
              isBeingGuarded=True, underEnemyControl=False, hasBeenAttackedThisTurn=True,
              hasAmbush=True, gotchaActivated=0, heavyArmor=1, title="TEST CARD", CurrentTarget=0x1234)
    kw.pop("huge")
    kw.update(extra)
    return kw


def _banned():
    """已经迁完的别名 → 一读就抛（`getattr(..., 默认值)` 也吞不掉：抛的不是 AttributeError）。"""
    def boom(name):
        def getter(self):
            raise AssertionError("S3' 之后仍在读 Card.%s 别名（应读 card.obj.*）" % name)
        return property(getter)
    for n in MIGRATED:
        setattr(BanAliases, n, boom(n))


class BanAliases(Card):
    """只把**已迁的那批**别名封掉（`name`/`side`/`slot`… 还没迁，照旧可用）。"""
    __slots__ = ()


_banned()


# --------------------------------------------------------------------------
# 二、口径：别名 == 迁移时用的原版读法
# --------------------------------------------------------------------------
probe = Card(uid="0x1", obj=_obj(**_full("0x1", ME, GM.ECardLocation.Hand_Left, 77)), raw={})
print("一、口径（Card.<别名> 必须等于迁移时改用的原版读法）")
for alias in sorted(MIGRATED):
    want = MIGRATED[alias](probe.obj)
    got = getattr(probe, alias)
    chk("Card.%s == 原版读法（%r）" % (alias, want), got == want, "现在 %r" % (got,))
chk("口径表覆盖了 board.py 里 S3' 声明已迁的全部别名（17 个）", len(MIGRATED) == 17,
    "%d 个" % len(MIGRATED))


# --------------------------------------------------------------------------
# 三、反向 + 行为不变：BanAliases 盘面 vs 普通 Card 盘面，渲染/查询结果必须一字不差
# --------------------------------------------------------------------------
def build(cls):
    """两张手牌 + 一个前线单位（我方）+ 一张敌方手牌 + 双方总部。"""
    mine_h = _mk(cls, "h1", **_full("h1", ME, GM.ECardLocation.Hand_Left, 77, locationNumber=1))
    mine_h2 = _mk(cls, "h2", **_full("h2", ME, GM.ECardLocation.Hand_Left, 78, locationNumber=2,
                                     selectTargetOnPlayedFromHand=False, KreditsTax_AsEnemyTarget=0,
                                     isSuppressed=False, hasBeenAttackedThisTurn=False))
    mine_u = _mk(cls, "m1", **_full("m1", ME, GM.ECardLocation.Board_Frontline, 80, locationNumber=0,
                                    Type=GM.EType.tank, attack=5, attackBuff=0, defense=6,
                                    underEnemyControl=True, isBeingGuarded=False))
    foe_h = _mk(cls, "H1", **_full("H1", OPP, GM.ECardLocation.Hand_Right, 90, locationNumber=1))
    hq_me = _mk(cls, "hq", **_full("hq", ME, GM.ECardLocation.Board_HQLeft, 1, locationNumber=0,
                                   Type=GM.EType.location, title="MY HQ", CurrentTarget=0))
    hq_foe = _mk(cls, "ehq", **_full("ehq", OPP, GM.ECardLocation.Board_HQRight, 2, locationNumber=0,
                                     Type=GM.EType.location, title="FOE HQ", CurrentTarget=0))
    cards = [mine_h, mine_h2, mine_u, foe_h, hq_me, hq_foe]
    st = BoardState(source="t", cards=cards, my_side=ME, turn=3, our_turn=True,
                    kredits={ME: 5, OPP: 2}, slots={ME: 3, OPP: 3},
                    hq={ME: hq_me, OPP: hq_foe}, frontline_owner=ME, match_finished=False)
    return st, cards


class _Sess:
    """`kardsmem.cards.snapshot(session)` 只要 `session.snapshot()`（见 cards.py:949）。"""
    def __init__(self, st):
        self._st = st

    def snapshot(self):
        return self._st


def exercise(st):
    """跑一遍**生产读侧**（只跑不需要游戏/注入的那些）：任何一次别名读都会让 BanAliases 抛。"""
    cs = st.cards
    h = view.Handles(st)
    out = []
    out.append(view.render_board(st, h, full=True))
    out.append(view.render_board(st, h))
    for c in cs:
        out.append(view.render_inspect({"card": c, "handle": h.handle(c)}, st))
        out.append(view._stat(c))
        out.append(view._op_cost(c))
        out.append(view._marks(c, {}))
    out.append(view.render_targets([(c, {"ok": True, "can": True}) for c in cs], h))
    out.append(repr(view.resolve(st, "#77", h)))
    out.append(repr(view.resolve(st, "h2", h)))
    out.append(repr(view.resolve(st, "80", h)))
    out.append(repr(view.ambiguous(st, "#77")))
    s = _Sess(st)
    out.append(repr(C.find(s, card_id=78)))
    out.append(repr(C.find(s, uid="h1")))
    out.append(repr(C.rows(s)))
    out.append(repr(C.hand(s, ME)))
    out.append(repr(C.discard(s, ME)))
    out.append(repr(C.format_table(st)))
    out.append(repr(C.target_candidates(s, cs[0])))
    return "\n".join(out)


print("二、反向 + 行为不变（禁掉已迁别名后，渲染/查询必须照旧）")
st_plain, _ = build(Card)
st_ban, _ = build(BanAliases)
plain = exercise(st_plain)
try:
    ban = exercise(st_ban)
    chk("禁掉已迁别名后，view/cards 的渲染与查询跑得完（没有一处再经别名）", True)
except AssertionError as e:
    ban = None
    chk("禁掉已迁别名后，view/cards 的渲染与查询跑得完（没有一处再经别名）", False, str(e)[:120])
if ban is not None:
    chk("两种卡的输出**一字不差**（迁移没有改变行为）", plain == ban)
    for tag, needle in (("id=77", "id=77"), ("费用 4（5 + -1）", "费4"), ("行动费 3(+1)（基础 2 + buff 1）", "3(+1)"),
                        ("上限 9/8", "上限 9/8"), ("被指向+3费", "被指向+3费"),
                        ("需指向", "需指向"), ("被抑制", "被抑制"), ("被守护", "被守护"),
                        ("敌方控制", "敌方控制"), ("伏击(本回合已被攻击)", "伏击(本回合已被攻击)"),
                        ("稀有度枚举：1   卡集枚举：4", "稀有度枚举：1   卡集枚举：4"),
                        ("进场于回合 2", "进场于回合 2")):
        chk("输出里含 %s（渲染读到的确实是原版字段的值）" % tag, needle in plain)

print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
sys.exit(1 if fails else 0)
