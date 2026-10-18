# -*- coding: utf-8 -*-
"""盘面模拟（"如果我这样走，会怎样"）：基于**当前实时盘面**跑 `sim`，只模拟，**不向游戏发任何动作**。

给 GUI 的"盘面模拟"窗口用，也可单独 import。流程：
    1. 取快照作**基线**，用 `agent.view` 的短号（h1 手牌 / H1 敌方手牌 / m2 我方场上 / e3 敌方场上 / hq / ehq）解析命令里的牌；
    2. 用 `RuleV2._search_sim` 建模拟场面（与自动对局同一条路：手牌带效果摘要、活种子确定随机、带目标的牌用游戏判断函数列合法目标）；
    3. 命令 → `sim.engine.A` → `sim.engine.run/apply` 得到新场面，算前后差异；**每条命令的结果成为下一条的起点**（帧栈）。
返回**数据**（`Result`），文字格式由调用方（`gui.whatif_panel`）负责——这样本层不依赖 GUI。

命令（`;` 分隔多条，按次序执行、按次序给出结果，遇到出错就停）：
    deploy <牌> [slot] [choose <i>] [with <目标>]    部署单位。slot = 部署后它在我方支援线这一行（**含总部**，总部占一格）从左数第几个（1 = 最左，可以在总部左边；不写 = 最右）
    play   <牌> [choose <i>] [with <目标>]           打出指令。`with` 后面是指向的牌（敌方总部 ehq、己方总部 hq 也可以）
    attack <单位> [with] <目标>                      攻击
    front  <单位> [slot]                              支援线 → 前线（slot＝上线后在前线我方单位里从左数第几个，1＝最左；不写＝最右）
    end                                              结束回合（模拟回合结算）
    spawn  <牌名/下标>                               回答**三选一**（`deploy/play` 之后会列出候选）：选中的牌进手牌。
                                                    预报挂起时也用它**真的选**：一次一层（第一层天气、第二层档），
                                                    或者一次回答 `forecast` 列出来的那堆候选
    forecast                                        回答**预报**（只在第一层挂起时）：走一步，把 3 天气 × 3 档 = 9 张候选
                                                    合并成一次选择并列出，再用 `spawn <牌名/下标>` 从中选一张
                                                    （真的选，下回合进手牌）。★ 第一层已经用 spawn 选过就不能再 forecast
    select <手牌>                                    回答"选一张手牌"类的提示
    undo / reset                                     撤回上一条 / 回到现在的实时盘面
**抉择**（二选一那种）不是独立命令：必须写在 deploy/play 里（`play h3 choose 1`）；缺了会报错并列出每个选项的效果。
新出现的牌：`n1 n2 …` = 模拟里新部署的单位，`g1 g2 …` = 模拟里新进手牌的牌。

敌方手牌：从内存读到（对手手牌在进程里是明文），模拟里跟踪——"敌方随机弃 X 张"之类的效果按活种子算出弃了哪几张，从敌方手牌里去掉。
边界（如实）：只有**手牌里的牌的效果摘要**是预先按基线种子算好的，前面步骤已经消耗了随机数时，后面步骤的随机结果可能与实际不同（会提醒）；
对手回合不模拟；模拟不是游戏本体，差异见 `docs/SIM-FIDELITY.md`；盘面在你操作期间变了，要 `reset`（结果里会提醒）。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

VERBS = {"play": "play", "p": "play", "deploy": "deploy", "d": "deploy", "attack": "attack", "a": "attack",
         "atk": "attack", "front": "front", "move": "front", "f": "front", "end": "end", "e": "end",
         "spawn": "spawn", "forecast": "forecast", "select": "select", "undo": "undo", "u": "undo", "reset": "reset"}
_CARD_TOKEN = re.compile(r"^([A-Za-z]+\d*|#-?\d+|@\w+)$")
USAGE = ("deploy <牌> [slot] [choose <i>] [with <目标>] | play <牌> [choose <i>] [with <目标>] | attack <单位> [with] <目标> | "
         "front <单位> [slot] | end | spawn <牌名/下标> | forecast | select <手牌> | undo | reset")


@dataclass
class Command:
    verb: str
    src: Optional[str] = None          # 牌号
    dst: Optional[str] = None          # with 后的目标
    slot: Optional[int] = None         # deploy 的 slot（1 起）
    choice: Optional[int] = None       # choose <i>（抉择牌的选项下标，0 起）
    arg: Optional[str] = None          # spawn 的答案（牌名/下标；预报挂起时是天气或档）/ select 的手牌号


@dataclass
class Result:
    ok: bool
    echo: str = ""                     # 规范化后的命令回显
    error: str = ""
    kind: str = ""                     # sim 动作类：deploy | order | attack | move | turn_end | spawn | forecast | select | undo | reset
    eff: Optional[dict] = None         # 这一步打出的效果摘要（与"手牌效果预览"同格式）
    diff: List[str] = field(default_factory=list)      # 盘面变化（已是文字）
    notes: List[str] = field(default_factory=list)     # 提醒/缺口
    labels: dict = field(default_factory=dict)         # {卡 id: "短号 名字"}（把效果摘要里的 id 写成人看得懂的）
    choices: Optional[dict] = None     # 需要玩家回答：{"kind","options":[(key,label,payload)],"need":"choose"|"spawn"|"forecast"|"select"}
    depth: int = 0                     # 帧栈里有几步（0 = 实时盘面）
    sim_before: object = None
    sim_after: object = None


def _card_tok(tok: str) -> str:
    if not _CARD_TOKEN.match(tok or ""):
        raise ValueError("牌号 %r 不合法（例：h3 手牌 / m1 我方场上 / e2 敌方场上 / hq / ehq / n1 / g1）" % tok)
    return tok


def parse(text: str) -> Command:
    """单条命令 → Command；不认得抛 ValueError（给人看的话）。多条用 `split_line` 先拆。"""
    t = (text or "").split()
    if not t:
        raise ValueError("空命令。" + USAGE)
    verb = VERBS.get(t[0].lower())
    if verb is None:
        raise ValueError("不认得命令 %r。%s" % (t[0], USAGE))
    args = t[1:]
    if verb in ("end", "undo", "reset", "forecast"):
        if args:
            raise ValueError("%s 不带参数（预报要自己挑就用 spawn <天气>，再 spawn <档>）" % verb)
        return Command(verb)
    if verb == "spawn":
        if not args:
            raise ValueError("spawn 需要候选牌名（或下标）")
        return Command("spawn", arg=" ".join(args))
    if verb == "select":
        if len(args) != 1:
            raise ValueError("select 需要一张手牌（例 select h2）")
        return Command("select", arg=_card_tok(args[0]))
    if not args:
        raise ValueError("%s 需要参数" % verb)
    cmd = Command(verb, src=_card_tok(args[0]))
    rest, i = args[1:], 0
    if verb in ("attack", "front"):
        if verb == "front":
            if len(rest) > 1 or (rest and not rest[0].isdigit()):
                raise ValueError("front 写法：front <单位> [slot]（slot＝上线后在前线我方单位里从左数第几个，1＝最左）")
            if rest:
                if int(rest[0]) < 1:
                    raise ValueError("slot 从 1 起")
                cmd.slot = int(rest[0])
            return cmd
        if rest and rest[0].lower() == "with":
            rest = rest[1:]
        if len(rest) != 1:
            raise ValueError("attack 需要 <单位> <目标>（敌方总部写 ehq）")
        cmd.dst = _card_tok(rest[0])
        return cmd
    while i < len(rest):                                           # deploy / play 的可选部分，次序固定：slot → choose → with
        w = rest[i].lower()
        if w == "choose":
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                raise ValueError("choose 后面要跟选项下标（0 起），例：%s %s choose 1" % (verb, cmd.src))
            cmd.choice = int(rest[i + 1])
            i += 2
        elif w == "with":
            if i + 1 >= len(rest):
                raise ValueError("with 后面要跟目标")
            cmd.dst = _card_tok(rest[i + 1])
            i += 2
        elif w.isdigit() and verb == "deploy" and cmd.slot is None and cmd.choice is None and cmd.dst is None:
            if int(w) < 1:
                raise ValueError("slot 从 1 起（1 = 支援线最左，总部也占一格）")
            cmd.slot = int(w)
            i += 1
        else:
            raise ValueError("看不懂 %r。写法：%s <牌>%s [choose <i>] [with <目标>]" % (
                rest[i], verb, " [slot]" if verb == "deploy" else ""))
    return cmd


def split_line(text: str) -> List[str]:
    """按 `;`（含全角 `；`）拆成多条命令，丢掉空段。"""
    return [p.strip() for p in re.split(r"[;；]", text or "") if p.strip()]


# ---------------------------------------------------------------------------
# 差异（纯函数：两个 Sim → 文字行）
# ---------------------------------------------------------------------------
def _stat(u) -> str:
    return "%s/%s" % (u.atk, u.dfn)


def live_fx_text(fx: dict, limit: int = 60) -> str:
    """`kardsmem.cards.read_live_effects` 的结果 → 一行简短的"被贴了什么"（没有就 ""）。

    手牌/场上的牌除了 CDO 模板值，还可能带着别人贴的 buff / 能力（`buffsFromCards` /
    `receivedAbilitiesFromCards`）——**同名两张牌的区别往往就在这里**，盘面文字要能看出来。"""
    parts: List[str] = []
    for row in (fx or {}).get("buffs_from_cards") or ():
        bs = {k: v for k, v in (row.get("buffs") or {}).items() if v}
        if not bs:
            continue
        giver = row.get("giver_name") or ("#%s" % row.get("giver_card_id"))
        parts.append("%s %s" % (giver, " ".join("%s%+d" % (k, v) for k, v in bs.items())))
    for row in (fx or {}).get("received_abilities") or ():
        nm = row.get("ability") if isinstance(row, dict) else row   # `read_received_abilities` 给的是 [{"ability","givers"}]
        if nm:
            parts.append("能力:%s" % nm)
    extra = (fx or {}).get("kredits_tax_extra") or 0
    if extra:
        parts.append("税%+d" % extra)
    t = "; ".join(str(x) for x in parts)
    return t if len(t) <= limit else t[:max(0, limit - 1)] + "…"


def diff_sims(before, after, label: Callable[[int], str], hand_name: Callable[[int], str] = str) -> List[str]:
    """前后两个 `Sim` 的差异，每条一行指令式文字。`label(单位id)` → 显示名（短号 + 名字）。"""
    out: List[str] = []
    me, opp = before.me, before.opp
    b_me, a_me = before.hq.get(me, 0), after.hq.get(me, 0)
    b_op, a_op = before.hq.get(opp, 0), after.hq.get(opp, 0)
    if a_op != b_op:
        out.append("enemy_hq %s -> %s" % (b_op, a_op))
    if a_me != b_me:
        out.append("own_hq %s -> %s" % (b_me, a_me))
    if after.kredits != before.kredits:
        out.append("kredit %g -> %g" % (before.kredits, after.kredits))
    if after.slots != before.slots:
        out.append("slot %g -> %g" % (before.slots, after.slots))
    if getattr(after, "opp_kredits", 0) != getattr(before, "opp_kredits", 0):
        out.append("opp kredit %+g" % (after.opp_kredits - before.opp_kredits))
    if getattr(after, "opp_slots", 0) != getattr(before, "opp_slots", 0):
        out.append("opp slot %+g" % (after.opp_slots - before.opp_slots))
    for uid in sorted(set(before.units) | set(after.units), key=lambda x: (str(type(x)), x)):
        b, a = before.units.get(uid), after.units.get(uid)
        if b is not None and a is None:
            out.append("%s dies/leaves" % label(uid))
        elif b is None and a is not None:
            out.append("%s enters %s%s" % (label(uid), _stat(a), (" [%s]" % ", ".join(sorted(a.kw))) if a.kw else ""))
        else:
            ch = []
            if (a.atk, a.dfn) != (b.atk, b.dfn):
                ch.append("%s -> %s" % (_stat(b), _stat(a)))
            if a.row != b.row:
                ch.append("row %s -> %s" % (b.row, a.row))
            if set(a.kw) != set(b.kw):
                add, rem = sorted(set(a.kw) - set(b.kw)), sorted(set(b.kw) - set(a.kw))
                ch.append("kw %s%s" % (("+" + ",".join(add)) if add else "", ("-" + ",".join(rem)) if rem else ""))
            if a.pinned != b.pinned:
                ch.append("pinned" if a.pinned else "unpinned")
            if ch:
                out.append("%s %s" % (label(uid), " ".join(ch)))
    gone = [h for h in before.hand if h not in after.hand]
    came = [h for h in after.hand if h not in before.hand]
    if gone:
        out.append("hand -%s" % ", ".join(hand_name(h) for h in gone))
    if came:
        out.append("hand +%s" % ", ".join(hand_name(h) for h in came))
    if len(after.deck) != len(before.deck) and before.deck_known:
        out.append("deck %d -> %d" % (len(before.deck), len(after.deck)))
    pend = [x for x in getattr(after, "pending_cards", []) if x not in getattr(before, "pending_cards", [])]
    if pend:
        out.append("next_turn hand +%s" % ", ".join(str(p[0]) for p in pend))
    return out


# ---------------------------------------------------------------------------
# 布局：敌方手牌 / 敌方支援线 / 前线 / 我方支援线 / 我方手牌
# ---------------------------------------------------------------------------
@dataclass
class Layout:
    header: str = ""
    enemy_hand: List[str] = field(default_factory=list)
    enemy_support: List[str] = field(default_factory=list)
    front: List[str] = field(default_factory=list)
    my_support: List[str] = field(default_factory=list)
    my_hand: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)


def render_layout(L: Layout) -> str:
    """纯函数：`Layout` → 文字。顺序固定：敌方手牌、敌方支援线、前线、我方支援线、我方手牌（上到下）。"""
    def row(title, items):
        return "%-14s %s" % (title, "  |  ".join(items) if items else "-")
    out = [L.header, "",
           row("ENEMY HAND", L.enemy_hand), row("ENEMY SUPPORT", L.enemy_support), "-" * 60,
           row("FRONTLINE", L.front), "-" * 60,
           row("MY SUPPORT", L.my_support), row("MY HAND", L.my_hand)]
    if L.pending:
        out += ["", "PENDING"] + ["  " + x for x in L.pending]
    return "\n".join(out)


# ---------------------------------------------------------------------------
def _addr(uid) -> Optional[int]:
    """`Card.uid`（"0x1C4B…" 形式的对象地址）→ 整数地址；不是地址形式就 None。"""
    try:
        return int(uid, 16) if isinstance(uid, str) and uid.lower().startswith("0x") else None
    except ValueError:
        return None


ZONES = ("enemy_hand", "enemy_support", "front", "my_support", "my_hand")     # 上到下


@dataclass
class Tile:
    """点选界面里的一张牌/单位。`token` 就是命令里的牌号（h2 / m1 / e3 / hq / ehq / n1 / g1）。"""
    token: str
    name: str
    zone: str                                      # ZONES 之一
    kind: str = "unit"                             # unit | order | hq
    cost: Optional[float] = None                   # 手牌的费用
    atk: object = None
    dfn: object = None
    kw: Tuple[str, ...] = ()
    pinned: bool = False
    uid: Optional[int] = None                      # 内存里的卡对象地址（读规则文本用）；模拟里新出现的牌没有
    needs_target: Optional[bool] = None            # 打出时要不要指向（读得出才有值）
    mine: bool = False
    text: Optional[str] = None                     # 规则文本（由界面层读内存后填）


@dataclass
class BoardModel:
    header: str
    zones: dict                                    # {zone: [Tile]}
    depth: int = 0
    pending: Optional[dict] = None                 # 挂起的选择（`_choices` 的结构），没有就 None
    pending_tiles: List[Tile] = field(default_factory=list)   # 三选一/预报的候选（没有 uid，按名字找文本）
    stale: bool = False                            # 实时盘面在模拟开始后变了（界面在提交按钮旁提示 reset）


@dataclass
class Frame:
    """帧栈里的一帧 = 执行完一条命令后的状态。`pending` 非空 ⇒ 选择挂起：此时 `sim` 是**打出那张牌之前**的场面（差异以它为底）。"""
    sim: object
    cmd: str = ""
    pending: object = None                         # sim.prompt.Suspended | None
    created: Tuple[int, ...] = ()                  # 模拟里新部署出来的单位 id（出现次序 → n1 n2 …）
    generated: Tuple[int, ...] = ()                # 模拟里新进手牌的牌 id（出现次序 → g1 g2 …）
    names: dict = field(default_factory=dict)      # 新 id → 名字
    order: dict = field(default_factory=dict)      # {(座位int, 行): (单位 id, ...)} 从左到右
    enemy_hand: Tuple[tuple, ...] = ()             # ((id, 名字), …) 敌方手牌（"?" = 未知的牌）
    rng_used: bool = False                         # 前面的步骤已经消耗过随机数


class ForecastPick:
    """`forecast` 造出来的一步：把预报的两层合并成一次选择（3 天气 × 3 档 = 9 张），与 `sim.prompt.Suspended`
    同接口（`prompt` + `resume(key)`）；`resume` 里按 (天气, 档) 把原来的两层提示链一次走完。

    这样 `forecast` 只算**一步**模拟（压一帧、可 undo、列出候选），下一步 `spawn <牌名>` 才是真的选。
    ★ 只有第一层挂起时能跑 forecast：第一层已经用 `spawn` 选过天气的话，`WhatIf._answer` 直接拒绝。"""

    def __init__(self, run, pairs: List[tuple], source=None):
        from sim.prompt import Option, Prompt
        self.run = run
        self.pairs = {"%s/%s" % (w, t): (w, t, name) for w, t, name in pairs}
        self.prompt = Prompt("select_card_to_draw",
                             tuple(Option(key="%s/%s" % (w, t), label=name,
                                          payload={"name": name, "weather": w, "tier": t}) for w, t, name in pairs),
                             source=source, layer=2, meta={"forecast": True, "merged": True, "nine": len(pairs) > 3})

    def resume(self, key):
        w, t, _name = self.pairs[key]
        from kardsmem.forecast_pool import L1_CARDS
        return self.run.resume(L1_CARDS.get(w, w)).resume(t)   # 第一层答案现在是类型卡名；兼容旧天气名


class WhatIf:
    """会话级对象：`view()` 取盘面文字，`run_line(命令行)` 跑一行（可含多条，`;` 分隔）。`sess` = `agent.session.AgentSession`。"""

    def __init__(self, sess, rule=None):
        self.sess = sess
        self.rule = rule
        self._sig = None
        self.stack: List[Frame] = []               # stack[0] = 基线；空 = 还没建
        self.base_st = None
        self.handles = None
        self._lock = threading.RLock()
        self.spawn_notes: dict = {}                # {手牌 id: 说明}：三选一候选池为空 / 读不出
        self.unplayable: dict = {}                 # {手牌 id: 原因}：只有“三选一”一个效果、而候选池是空的 ⇒ 原版打不出
        self.live_fx: dict = {}                    # {卡 id: 被贴的效果文字}：reset 时读一次（只读），布局里按卡 id 显示

    # ---- 盘面 ----
    def view(self, full: bool = False):
        """→ `(文字, st, handles)`。还没有模拟步骤时是**实时**盘面（每次重取）；有步骤时是**模拟后**的盘面（短号以基线为准）。"""
        from agent import view as V
        with self._lock:
            if len(self.stack) > 1:
                return render_layout(self._layout_frame(self.top)), self.base_st, self.handles
        st = self.sess.snapshot()
        if st.my_side is None:
            return V.render_board(st), st, None
        return render_layout(self._layout_live(st)), st, V.Handles(st)

    def board_model(self) -> Optional[BoardModel]:
        """点选界面用的结构化盘面（与 `view()` 同一份数据：无模拟步骤 = 实时，有步骤 = 模拟后）。不在对局里返回 None。"""
        from agent import view as V
        with self._lock:
            if len(self.stack) > 1:
                m = self._model_frame(self.top)
                # 有模拟步骤时，实时盘面变了 ⇒ 点选界面在提交按钮旁提示 reset（步骤仍基于旧基线）
                m.stale = self._live_changed()
                return m
        st = self.sess.snapshot()
        if st.my_side is None:
            return None
        return self._model_live(st, V.Handles(st))

    def _model_live(self, st, hd) -> BoardModel:
        from agent import view as V
        me, opp = st.my_side, st.other_side
        k, sl = st.kredits or {}, st.slots or {}
        key = lambda c: (c.slot if c.slot is not None else 99, c.uid)          # noqa: E731
        Z = {z: [] for z in ZONES}

        def tile(c, zone, mine):
            kind = "order" if c.card_type in ("order", "location", "wildcard", "gotcha") and zone.endswith("hand") else "unit"
            return Tile(hd.handle(c), c.name or "?", zone, kind,
                        cost=getattr(c, "kredit_cost", None) if zone.endswith("hand") else None,
                        atk=V._stat(c), dfn=None, kw=tuple(V.kw_of(c)[:4]), uid=_addr(c.uid), mine=mine,        # atk 直接放文本盘面同款的 “1+1/2”
                        needs_target=(bool(c.obj.selectTargetOnPlayedFromHand)
                                      if zone.endswith("hand") and c.obj.selectTargetOnPlayedFromHand is not None else None))
        for c in sorted(st.hand(opp), key=key):
            Z["enemy_hand"].append(tile(c, "enemy_hand", False))
        for c in sorted(st.hand(me), key=key):
            Z["my_hand"].append(tile(c, "my_hand", True))
        for side, zone in ((opp, "enemy_support"), (me, "my_support")):
            hq = (st.hq or {}).get(side)
            row = [x for x in st.cards if x.side == side and V.row_of(x) == "back"] + ([hq] if hq is not None else [])
            for c in sorted(row, key=key):                       # 总部和支援线单位同一行、共用站位（总部可以在中间）
                if c is hq:
                    Z[zone].append(Tile(hd.handle(hq), "HQ", zone, "hq", dfn=hq.defense, uid=_addr(hq.uid), mine=side == me))
                else:
                    Z[zone].append(tile(c, zone, side == me))
        for c in sorted([x for x in st.cards if V.row_of(x) == "frontline"], key=lambda c: (c.side != opp, key(c))):
            Z["front"].append(tile(c, "front", c.side == me))
        head = "turn %s | %s | kredit %s (slots %s) / enemy %s (slots %s)" % (
            st.turn, "my turn" if st.our_turn else "enemy turn", k.get(me), sl.get(me), k.get(opp), sl.get(opp))
        return BoardModel(head, Z)

    def _model_frame(self, fr: Frame) -> BoardModel:
        sim = fr.sim
        label, by_id = self._label_fn(fr)
        st = self.base_st
        me_s, opp_s = int(sim.me), int(sim.opp)
        k0 = st.kredits or {}
        head = "SIM step %d | kredit %s (slots %s) / enemy %s | HQ %s / %s" % (
            len(self.stack) - 1, sim.kredits, sim.slots, (k0.get(st.other_side) or 0) + (getattr(sim, "opp_kredits", 0) or 0),
            sim.hq.get(sim.me), sim.hq.get(sim.opp))
        Z = {z: [] for z in ZONES}

        def split(uid):
            tok, _, nm = label(uid).partition(" ")
            return tok, nm

        def unit(uid, zone, mine):
            u = sim.units[uid]
            tok, nm = split(uid)
            c = by_id.get(uid)
            return Tile(tok, nm, zone, "unit", atk=u.atk, dfn=u.dfn, kw=tuple(sorted(u.kw)[:4]), pinned=bool(u.pinned),
                        uid=_addr(c.uid) if c is not None else None, mine=mine)
        for side, rw, zone in ((opp_s, "back", "enemy_support"), (me_s, "back", "my_support"),
                               (opp_s, "frontline", "front"), (me_s, "frontline", "front")):
            for i in fr.order.get((side, rw), ()):
                if i == "hq":                                    # 总部是这一行的成员（站位里可以在中间）
                    mine_ = side == me_s
                    Z[zone].append(Tile("hq" if mine_ else "ehq", "HQ", zone, "hq", dfn=sim.hq.get(sim.me if mine_ else sim.opp), mine=mine_))
                elif i in sim.units:
                    Z[zone].append(unit(i, zone, side == me_s))
        Z["enemy_hand"] = [Tile(label(i).partition(" ")[0], n, "enemy_hand") for i, n in fr.enemy_hand]

        def hkey(i):
            c = by_id.get(i)
            if c is not None:
                h = self.handles.handle(c)
                return (0, int(h[1:]) if h[1:].isdigit() else 99)
            return (1, fr.generated.index(i) if i in fr.generated else 99)
        for i in sorted(sim.hand, key=hkey):
            h = sim.hand[i]
            tok, nm = split(i)
            c = by_id.get(i)
            Z["my_hand"].append(Tile(tok, nm, "my_hand", "unit" if h.is_unit() else "order", cost=h.cost,
                                     uid=_addr(c.uid) if c is not None else None, mine=True))
        m = BoardModel(head, Z, depth=len(self.stack) - 1)
        if fr.pending is not None:
            m.pending = self._choices(fr.pending, fr)
            if m.pending["need"] == "spawn":
                m.pending_tiles = [Tile(str(j), str(lab), "pending", "unit") for j, (_k, lab, _p) in enumerate(m.pending["options"])]
            elif m.pending["need"] == "forecast":
                m.pending_tiles = self._forecast_tiles(fr, m.pending)
        return m

    @staticmethod
    def _forecast_tiles(fr: Frame, pend: dict) -> List[Tile]:
        """预报挂起时的候选卡块。
        * 还没 `forecast` 的第一层：3 种天气各一张卡块（名字写着它预测的 3 张牌），点“选这种天气”= `spawn <天气>`；
        * `forecast` 走完一步之后（合并选择）：候选就是牌，第一层给 9 张、第二层给 3 张，点“选这张”= `spawn <key>`；
        * 直接 spawn 到第二层：这一种天气的 3 张，点“选这张”= `spawn <档>`。"""
        opts = pend.get("options") or ()
        meta = pend.get("meta") or {}
        if meta.get("merged"):
            return [Tile(str(k), str((pl or {}).get("name") or lab), "pending", "unit") for k, lab, pl in opts]
        layer = int(pend.get("layer") or 1)
        if layer <= 1:
            return [Tile(str(k), str(lab), "pending", "unit") for k, lab, _p in opts]
        return [Tile(str(k), str(pl[0] if isinstance(pl, (tuple, list)) and pl else lab), "pending", "unit")
                for k, lab, pl in opts]

    def _entry(self, h, c, V) -> str:
        kw = " ".join(V.kw_of(c)[:4])
        fx = (self.live_fx or {}).get(getattr(c.obj, "CardID", None))
        return "%s %s %s%s%s" % (h, c.name, V._stat(c), (" [%s]" % kw) if kw else "",
                                 (" [贴:%s]" % fx) if fx else "")

    def _layout_live(self, st) -> Layout:
        from agent import view as V
        hd = V.Handles(st)
        me, opp = st.my_side, st.other_side
        k, sl = st.kredits or {}, st.slots or {}
        L = Layout(header="turn %s | %s | kredit me %s (slots %s) / enemy %s (slots %s)" % (
            st.turn, "my turn" if st.our_turn else "enemy turn", k.get(me), sl.get(me), k.get(opp), sl.get(opp)))
        key = lambda c: (c.slot if c.slot is not None else 99, c.uid)          # noqa: E731
        for c in sorted(st.hand(opp), key=key):
            L.enemy_hand.append("%s %s" % (hd.handle(c), c.name))
        for c in sorted(st.hand(me), key=key):
            parts = [hd.handle(c), c.name]
            if (getattr(c, "card_type", None) not in ("order", "gotcha", "location", "wildcard")
                    and (c.attack is not None or c.defense is not None)):   # 手牌里的单位也显示攻防（游戏里能看到）
                parts.append(V._stat(c))
                kw = " ".join(V.kw_of(c)[:4])                              # 和场上一样显示词条
                if kw:
                    parts.append("[%s]" % kw)
            parts.append("c%s" % getattr(c, "kredit_cost", "?"))
            fx = (self.live_fx or {}).get(getattr(c.obj, "CardID", None))
            if fx:
                parts.append("[贴:%s]" % fx)
            L.my_hand.append(" ".join(parts))
        for side, sup in ((opp, L.enemy_support), (me, L.my_support)):
            hq = (st.hq or {}).get(side)
            row = [x for x in st.cards if x.side == side and V.row_of(x) == "back"] + ([hq] if hq is not None else [])
            for c in sorted(row, key=key):                       # 总部和支援线单位同一行、共用站位（总部可以在中间）
                sup.append("%s %s" % (hd.handle(c), c.defense) if c is hq else self._entry(hd.handle(c), c, V))
        for c in sorted([x for x in st.cards if V.row_of(x) == "frontline"], key=lambda c: (c.side != opp, key(c))):
            L.front.append(self._entry(hd.handle(c), c, V))
        return L

    def _layout_frame(self, fr: Frame) -> Layout:
        sim = fr.sim
        label, by_id = self._label_fn(fr)
        st = self.base_st
        me_s, opp_s = int(sim.me), int(sim.opp)
        k0 = st.kredits or {}
        L = Layout(header="turn %s | SIM step %d | kredit me %s (slots %s) / enemy %s | HQ me %s / enemy %s" % (
            st.turn, len(self.stack) - 1, sim.kredits, sim.slots,
            (k0.get(st.other_side) or 0) + (getattr(sim, "opp_kredits", 0) or 0),
            sim.hq.get(sim.me), sim.hq.get(sim.opp)))

        def unit(uid):
            u = sim.units[uid]
            kw = " ".join(sorted(u.kw)[:4])
            return "%s %s/%s%s%s" % (label(uid), u.atk, u.dfn, (" [%s]" % kw) if kw else "", " pinned" if u.pinned else "")

        def row_units(side, rw):
            out = []
            for i in fr.order.get((side, rw), ()):
                if i == "hq":
                    out.append("hq %s" % sim.hq.get(sim.me) if side == me_s else "ehq %s" % sim.hq.get(sim.opp))
                elif i in sim.units:
                    out.append(unit(i))
            return out
        L.enemy_support += row_units(opp_s, "back")
        L.my_support += row_units(me_s, "back")
        L.front += row_units(opp_s, "frontline") + row_units(me_s, "frontline")
        L.enemy_hand = ["%s %s" % (label(i), n) for i, n in fr.enemy_hand]
        def hkey(i):                                   # 我方手牌：先基线里的（按 h 号），再模拟里新进手牌的（g 号）
            c = by_id.get(i)
            if c is not None:
                h = self.handles.handle(c)
                return (0, int(h[1:]) if h[1:].isdigit() else 99)
            return (1, fr.generated.index(i) if i in fr.generated else 99)

        def hline(i):
            h = sim.hand[i]
            parts = [label(i)]
            if h.is_unit():
                parts.append("%s/%s" % (h.atk, h.dfn))     # 手牌里的单位也显示攻防
                kw = " ".join(sorted(h.kw)[:4])            # 和场上一样显示词条
                if kw:
                    parts.append("[%s]" % kw)
            parts.append("c%s" % h.cost)
            fx = (self.live_fx or {}).get(i)
            if fx:
                parts.append("[贴:%s]" % fx)
            return " ".join(parts)
        L.my_hand = [hline(i) for i in sorted(sim.hand, key=hkey)]
        if fr.pending is not None:
            L.pending = self._pending_lines(fr)
        return L

    # ---- 基线 / 模拟场面 ----
    def _rule(self):
        if self.rule is None:
            from player.rule import RuleV2
            self.rule = RuleV2(self.sess)
        return self.rule

    @staticmethod
    def _live_sig(st, seed):
        return (getattr(st, "turn", None), seed, tuple(sorted(c.obj.CardID for c in st.hand())),
                tuple(sorted((c.obj.CardID, int(getattr(c.obj, "defense", 0) or 0)) for c in st.cards)))

    def _seed(self):
        from kardsmem import rng as _rng
        try:
            return _rng.read_seed(self.sess._kardsmem())
        except Exception:                                          # noqa: BLE001
            return None

    def _build(self, st):
        """建模拟场面（VM 空跑几百次，贵）。"""
        rule = self._rule()
        rule._eff_cache.clear()
        rule._act_cache.clear()
        kred = int((st.kredits or {}).get(st.my_side) or 0)
        _me, _foes, ehq = rule._split(st)
        sim = rule._search_sim(st, kred, ehq)
        self._fill_deck(sim, st, rule)          # 抽牌按真实牌序，抽到抵抗这类 autoplay 牌要真的结算
        self._ensure_forecast(sim)
        self._ensure_spawn_pick(sim, st, rule)
        return sim

    @staticmethod
    def _fill_deck(sim, st, rule) -> None:
        """盘面模拟问的是"打出这一步后游戏里会发生什么"：抽牌要按**游戏此刻的真实牌序**抽，抽到的牌自带
        “抽到时效果”（抵抗这类 autoplay 牌抽到就免费打出，`engine.chain` 会入队、动作边界冲刷）。
        `rule._search_sim` 是评估用的简版、**不填牌库**（抽到的是匿名牌，没有效果）；这里补上。
        读不出牌序 ⇒ 保持未知（不假装知道抽到什么）。"""
        try:
            ids, dcards = rule._deck_state(st)
        except Exception:                                          # noqa: BLE001
            return
        if ids is None:
            return
        sim.deck = list(ids)
        sim.deck_known = True
        sim.deck_cards = dict(dcards or {})
        try:
            sim.fatigue = int(rule._fatigue(st) or 0)
        except Exception:                                          # noqa: BLE001
            pass

    def _ensure_spawn_pick(self, sim, st, rule) -> None:
        """手牌里带“三选一加入手牌”标记（`choose_spawn`）的牌，把候选表填进 `sim.spawn_pick`（`_search_sim` 这条路不填）。
        候选池是空的（例：AIR, LAND & SEA 在牌库没牌时）⇒ 这张牌其实没有效果：去掉标记并记一条说明；
        预测失败（读不出）⇒ 保留标记、记缺口，打出时提醒“候选读不出”，不编候选。"""
        from sim.engine import _has_key
        pick, self.spawn_notes, self.unplayable = {}, {}, {}
        for i, h in sim.hand.items():
            if not (isinstance(h.eff, dict) and _has_key(h.eff, "choose_spawn")):
                continue
            rule.gaps.pop(h.name + "#choose_spawn", None)
            cs = rule._spawn_cands(st, i)
            if cs:
                pick[i] = cs
            elif h.name + "#choose_spawn" in rule.gaps:
                self.spawn_notes[i] = "三选一候选读不出（%s）：结果里没有加牌，候选未知" % rule.gaps[h.name + "#choose_spawn"][:80]
            else:
                h.eff = {k: v for k, v in h.eff.items() if k != "choose_spawn"}
                self.spawn_notes[i] = "三选一的候选池是空的（例如牌库没牌了）⇒ 这张牌打出后没有加牌效果"
                if not h.eff:                                      # 除了这个什么效果都没有（AIR, LAND & SEA 牌库空时）：原版此时根本打不出（用户 2026-10-19）
                    self.unplayable[i] = "%s 的候选池是空的（牌库没牌了），原版此时无法打出，什么都不会发生" % h.name
        if pick:
            sim.spawn_pick = {**(sim.spawn_pick or {}), **pick}

    def _ensure_forecast(self, sim) -> None:
        """手里有预报牌、但规则侧没给出候选表（`sim.forecast` 空：它要给 9 条路径打分，任何一步读不出就是 None）⇒ 自己补：
        用 `kardsmem.forecast_pool`（池版预测，按活种子，实机三档验证过）填 `{天气: {档: (名字, 0.0)}}`。
        模拟里 forecast 只用来列出/挑选候选（不评估各条路径的价值），所以价值提示用不到，写 0。读不出种子/池 ⇒ 不补（预报牌会直接结算、不挂起，并在效果里留着 forecast 标记）。"""
        if sim.forecast:
            return
        from sim.engine import _has_key
        if not any(_has_key(h.eff, "forecast") for h in sim.hand.values()):
            return
        seed = self._seed()
        if seed is None:
            return
        try:
            from kardsmem.forecast_pool import TIERS, WEATHERS, predict_ptrs, read_weather_buckets, strip_serial
            km = self.sess._kardsmem()
            pool = km.names_pool()
            fc = {}
            for w, row in predict_ptrs(seed, read_weather_buckets(km)).items():
                fc[w] = {t: ((strip_serial(pool.fname_of(row[t]) or "").removesuffix("_C")), 0.0) for t in TIERS if row.get(t)}
            if any(fc.values()):
                sim.forecast = {w: r for w, r in fc.items() if r}
        except Exception:                                          # noqa: BLE001
            return

    def _reset(self) -> Optional[str]:
        """取快照、重建基线、清空帧栈。出错返回原因。"""
        from agent import view as V
        st = self.sess.snapshot()
        if st.my_side is None:
            return "本地座位读不出（不在对局里？）"
        try:
            sim = self._build(st)
        except Exception as ex:                                    # noqa: BLE001
            return "建模拟场面失败：%s: %s" % (type(ex).__name__, str(ex)[:120])
        self.base_st, self.handles = st, V.Handles(st)
        self.live_fx = self._read_live_fx(st)
        self._sig = self._live_sig(st, self._seed())
        order = {}
        key = lambda c: (c.slot if c.slot is not None else 99, c.uid)          # noqa: E731
        hqs = {id(h): (side, h) for side, h in (st.hq or {}).items() if h is not None}
        for c in sorted(list(st.cards) + [h for _s, h in hqs.values() if h not in st.cards], key=key):
            rw = V.row_of(c)
            if id(c) in hqs:                                       # 总部也是支援线这一行的成员（占一个站位）
                order.setdefault((int(c.obj.side), "back"), []).append("hq")
            elif rw in ("back", "frontline") and c.obj.CardID in sim.units:
                order.setdefault((int(c.obj.side), rw), []).append(c.obj.CardID)
        eh = tuple((c.obj.CardID, c.name) for c in sorted(st.hand(st.other_side), key=key))
        self.stack = [Frame(sim, "", order={k: tuple(v) for k, v in order.items()}, enemy_hand=eh)]
        return None

    def _read_live_fx(self, st) -> dict:
        """我方手牌/场上这些牌**此刻**被贴的效果（`read_live_effects`，只读）。reset 时读一次，布局里按卡 id 用。"""
        out = {}
        try:
            km = self.sess._kardsmem()
            from kardsmem import cards as _cards
            me = int(st.my_side)
            for c in st.cards:
                if int(getattr(c, "side", 0) or 0) != me:
                    continue
                ptr = (getattr(c, "raw", None) or {}).get("ptr")
                if not ptr:
                    continue
                t = live_fx_text(_cards.read_live_effects(km, ptr))
                if t:
                    out[c.obj.CardID] = t
        except Exception:                                          # noqa: BLE001
            return {}
        return out

    def _follow_live(self) -> Optional[str]:
        """**还没有模拟步骤**（栈里只有基线帧）时，执行命令前先看实时盘面变没变：变了就重取快照、重建基线。
        用户 2026-10-19：面板每 2 秒刷新的是实时盘面，但命令还在用旧基线，手牌一变就说“不在手上”、要手动 reset。
        没变什么都不做（重取/重建都贵）；已经有步骤 ⇒ 保持旧基线（步骤依赖它），面板会提示 reset。"""
        if len(self.stack) > 1:
            return None
        try:
            st = self.sess.snapshot()
        except Exception:                                          # noqa: BLE001
            return None
        if st.my_side is None:
            return None
        try:
            if self._live_sig(st, self._seed()) == self._sig:
                return None
        except Exception:                                          # noqa: BLE001
            return None
        return self._reset()

    @property
    def top(self) -> Frame:
        return self.stack[-1]

    # ---- 入口 ----
    def run_line(self, text: str) -> List[Result]:
        """一行（可含多条，`;` 分隔）→ 每条一个 `Result`，按次序；某条出错就停（后面依赖前面）。"""
        parts = split_line(text)
        if not parts:
            return [Result(False, error="空命令")]
        out: List[Result] = []
        with self._lock:
            for i, p in enumerate(parts):
                r = self.run(p)
                out.append(r)
                if not r.ok:
                    if i + 1 < len(parts):
                        r.notes.append("后面 %d 条命令没有执行（前一条出错）" % (len(parts) - i - 1))
                    break
        return out

    def run(self, text: str) -> Result:
        with self._lock:
            return self._run(text)

    def _run(self, text: str) -> Result:
        try:
            cmd = parse(text)
        except ValueError as ex:
            return Result(False, error=str(ex), depth=max(0, len(self.stack) - 1))
        if cmd.verb == "reset" or not self.stack:
            err = self._reset()
            if err:
                return Result(False, echo=text.strip(), error=err)
            if cmd.verb == "reset":
                return Result(True, echo="reset", kind="reset", notes=["已回到现在的实时盘面"], depth=0)
        else:
            err = self._follow_live()                              # 还没模拟过：基线自动跟着实时盘面走
            if err:
                return Result(False, echo=text.strip(), error="实时盘面变了、重取基线失败：%s" % err)
        if cmd.verb == "undo":
            if len(self.stack) <= 1:
                return Result(False, echo="undo", error="没有可撤回的命令（已经是实时盘面）", depth=0)
            gone = self.stack.pop()
            return Result(True, echo="undo", kind="undo", notes=["撤回：%s" % (gone.cmd or "?")],
                          depth=len(self.stack) - 1)
        try:
            res = self._step(cmd, text.strip())
        except ValueError as ex:
            return Result(False, echo=text.strip(), error=str(ex), depth=len(self.stack) - 1)
        except Exception as ex:                                    # noqa: BLE001
            return Result(False, echo=text.strip(), error="模拟出错：%s: %s" % (type(ex).__name__, str(ex)[:160]),
                          depth=len(self.stack) - 1)
        res.depth = len(self.stack) - 1
        return res

    # ---- 单步 ----
    def _live_changed(self) -> bool:
        try:
            st = self.sess.snapshot()
            return st.my_side is not None and self._live_sig(st, self._seed()) != self._sig
        except Exception:                                          # noqa: BLE001
            return False

    def _label_fn(self, fr: Frame):
        by_id = {c.obj.CardID: c for c in self.base_st.cards}

        def label(uid):
            c = by_id.get(uid)
            if c is not None:
                return "%s %s" % (self.handles.handle(c), c.name)
            if uid in fr.created:
                return "n%d %s" % (fr.created.index(uid) + 1, fr.names.get(uid, ""))
            if uid in fr.generated:
                return "g%d %s" % (fr.generated.index(uid) + 1, fr.names.get(uid, ""))
            return "#%s" % (uid,)
        return label, by_id

    def _card_id(self, tok: str, fr: Frame):
        """牌号 → 卡 id（或敌方总部 "hq"）。n/g 是模拟里新出现的牌。"""
        from agent import view as V
        m = re.match(r"^([ng])(\d+)$", tok)
        if m:
            seq = fr.created if m.group(1) == "n" else fr.generated
            k = int(m.group(2))
            if not 1 <= k <= len(seq):
                raise ValueError("%s 还不存在（模拟里新出现的%s目前有 %d 个）" % (tok, "单位" if m.group(1) == "n" else "手牌", len(seq)))
            return seq[k - 1]
        c = V.resolve(self.base_st, tok, self.handles)
        if c is None:
            raise ValueError("找不到 %r（看盘面里的短号；盘面可能刚变过，用 reset 重取）" % tok)
        return "hq" if self.handles.handle(c) == "ehq" else c.obj.CardID

    def _pending_lines(self, fr: Frame) -> List[str]:
        ch = self._choices(fr.pending, fr)
        ans = {"spawn": "spawn <card>", "select": "select <hand card>"}
        if ch["need"] == "forecast":
            if (ch.get("meta") or {}).get("merged"):
                ans["forecast"] = "spawn <牌名/下标>（从这些候选里选一张）"
            elif int(ch.get("layer") or 1) > 1:
                ans["forecast"] = "spawn <档>（第一层已经用 spawn 选过，不能再 forecast）"
            else:
                ans["forecast"] = "forecast（走一步、列出候选）或 spawn <候选>（一次一层地选）"
        out = ["answer with: %s" % ans[ch["need"]]]
        label, _by = self._label_fn(fr)
        for key, lab, _pl in ch["options"]:
            out.append("- %s" % (label(key) if ch["need"] == "select" and isinstance(key, int) else lab))
        return out

    @staticmethod
    def _eff_display(eff):
        """挂起时也要把这张牌**已经算出来的效果**写出来（抽牌/治疗…）：去掉“选择标记”本身
        （`forecast` / `choose_spawn` 是要玩家回答的提示，不是效果，不能当 effect 行显示）。
        用户 2026-10-19：`play 预报牌` 的抽牌效果以前挂起时不显示。"""
        if not isinstance(eff, dict):
            return eff
        return {k: v for k, v in eff.items() if k not in ("forecast", "choose_spawn")}

    def _step(self, cmd: Command, echo_in: str) -> Result:
        from sim.engine import apply as sim_apply, sim_turn_end
        fr = self.top
        notes: List[str] = []
        if self.base_st.our_turn is False:
            notes.append("现在是对手回合：按“如果轮到我”来模拟")
        if self._live_changed():
            notes.append("实时盘面已经变了（对手动作/你操作了游戏）：下面基于旧基线，建议 reset")
        if fr.pending is not None:
            if cmd.verb in ("spawn", "forecast", "select"):
                return self._answer(cmd, fr, notes, echo_in)
            return Result(False, echo=echo_in, error="有一个选择还没回答：%s，或 undo 撤回那条命令" % self._need_text(fr),
                          choices=self._choices(fr.pending, fr), notes=notes)
        if cmd.verb in ("spawn", "forecast", "select"):
            return Result(False, echo=echo_in, error="现在没有等着回答的选择（先 deploy/play 一张会弹选择的牌）", notes=notes)
        sim = fr.sim
        if cmd.verb == "end":
            after = sim_turn_end(sim)
            return self._push(self._frame_after(fr, sim, after, None, "turn_end", echo_in, None), sim, "turn_end", "end", None, notes)
        src = self._card_id(cmd.src, fr)
        dst = self._card_id(cmd.dst, fr) if cmd.dst else None
        a, kind, echo = self._action(sim, cmd, src, dst, fr, notes)
        from sim.engine import prompt_of, run as sim_run, _eff_of
        eff = _eff_of(sim, a) if kind in ("deploy", "order") else None
        if kind in ("deploy", "order") and src in self.unplayable:
            raise ValueError(self.unplayable[src])
        if kind in ("deploy", "order") and src in self.spawn_notes:
            notes.append(self.spawn_notes[src])
        if eff and fr.rng_used and eff.get("rng_draws"):
            notes.append("前面的步骤已经消耗过随机数：这一步的随机结果仍按基线种子预先算好，可能与实际不同")
        pr = prompt_of(sim, a)
        if pr is None:
            if cmd.choice is not None:
                raise ValueError("%s 没有抉择，不要写 choose（%s）" % (cmd.src, echo))
            after = sim_apply(sim, a)
            return self._push(self._frame_after(fr, sim, after, a, kind, echo_in, eff, cmd.slot), sim, kind, echo, eff, notes)
        run = sim_run(sim, a)
        if pr.kind == "choose_one":
            opts = self._choices(run, fr)
            opts["need"] = "choose"
            valid = [o[0] for o in opts["options"]]
            if cmd.choice is None:
                return Result(False, echo=echo, error="%s 是抉择牌：必须写 choose <i>，例如 %s %s choose %s" % (
                    cmd.src, cmd.verb, cmd.src, valid[0]), choices=opts, notes=notes)
            if cmd.choice not in valid:
                return Result(False, echo=echo, error="choose %d 不在选项 %s 里" % (cmd.choice, valid), choices=opts, notes=notes)
            done = run.resume(cmd.choice)
            payload = next(o[2] for o in opts["options"] if o[0] == cmd.choice)
            return self._push(self._frame_after(fr, sim, done.state, a, kind, echo_in, payload, cmd.slot), sim, kind, echo, payload, notes)
        if cmd.choice is not None:
            raise ValueError("%s 不是抉择牌（它的选择要用 spawn / forecast / select），去掉 choose" % cmd.src)
        nf = Frame(sim, echo_in, pending=run, created=fr.created, generated=fr.generated, names=dict(fr.names),
                   order=fr.order, enemy_hand=fr.enemy_hand, rng_used=fr.rng_used)
        self.stack.append(nf)
        label, by_id = self._label_fn(nf)
        return Result(True, echo=echo, kind=kind, notes=notes, labels={cid: label(cid) for cid in by_id},
                      choices=self._choices(run, nf), eff=self._eff_display(eff), sim_before=sim)

    def card_options(self, tok: str) -> dict:
        """点选界面选中一张牌/单位时问：这张牌**现在能怎么用**。
        `legal`：模拟的合法走法里有没有它；`targets`：可指向的目标牌号集合（`ehq` = 敌方总部）；`no_target`：不带目标也合法；
        `can_front`：（单位）能不能上线；`can_game`：基线手牌再问游戏自己的 CanPlayFromHand（`sess.can_play`，读不出 = None）。
        ★ 只用来挑与提示，不用来否决：界面仍允许点别的目标，游戏本体自己裁决。"""
        from policy.search import _gen_actions
        with self._lock:
            if not self.stack and self._reset():
                return {}
            fr = self.top
            if fr.pending is not None:
                return {}
            try:
                cid = self._card_id(tok, fr)
            except ValueError:
                return {}
            sim = fr.sim
            label, by_id = self._label_fn(fr)
            hand = cid in sim.hand
            try:
                gens = [g for g in _gen_actions(sim) if g.src == cid]
            except Exception:                                      # noqa: BLE001
                return {"tok": tok, "legal": None, "targets": set(), "no_target": None, "can_front": None, "can_game": None}

            def tk_of(d):
                return "ehq" if d == "hq" else label(d).partition(" ")[0]
            act = [g for g in gens if g.kind in (("deploy", "order") if hand else ("attack",))]
            out = {"tok": tok, "legal": (bool(act) and cid not in self.unplayable) if hand else None,
                   "targets": {tk_of(g.dst) for g in act if g.dst is not None},
                   "no_target": any(g.dst is None for g in act) if hand else None,
                   "can_front": any(g.kind == "move" for g in gens) if not hand else None, "can_game": None, "source": ""}
            if cid in self.unplayable:
                out["why"] = self.unplayable[cid]
            c = by_id.get(cid)
            if hand and c is not None and len(self.stack) == 1:
                try:
                    r = self.sess.can_play(c)
                    out["can_game"] = r.get("can") if isinstance(r, dict) else None
                    out["source"] = str((r or {}).get("source") or "")
                    out["why"] = str((r or {}).get("reason_zh") or (r or {}).get("reason") or "")
                except Exception:                                  # noqa: BLE001
                    pass
            return out

    @staticmethod
    def _need_text(fr: Frame) -> str:
        pr = fr.pending.prompt
        if pr.meta.get("spawn"):
            return "先 spawn <候选牌>"
        if pr.meta.get("forecast"):
            if pr.meta.get("merged"):
                return "先 spawn <牌名/下标>（从列出来的候选中选一张）"
            if int(getattr(pr, "layer", 1) or 1) > 1:
                return "先 spawn <档>（第一层已经用 spawn 选过，不能再 forecast）"
            return "先 forecast（走一步、列出候选）或 spawn <候选>（一次一层地选）"
        return "先 select <手牌>"

    @staticmethod
    def _match(arg: str, options) -> Optional[object]:
        """把玩家写的答案对到候选 key 上：下标（0 起，便于点选界面）→ key → label → 唯一包含（都不区分大小写）。
        预报第二层的候选写着牌名（`light: sunny_l`），所以 `spawn sunny_l` 也能对上。对不上返回 None。"""
        arg = arg or ""
        if arg.isdigit() and int(arg) < len(options):
            return options[int(arg)][0]
        low = arg.lower()
        for keyf in (lambda k, lab: str(k).lower(), lambda k, lab: str(lab).lower()):
            hits = [k for k, lab, _p in options if keyf(k, lab) == low]
            if len(hits) == 1:
                return hits[0]
        hits = [k for k, lab, _p in options if low in str(k).lower() or low in str(lab).lower()]
        return hits[0] if len(hits) == 1 else None

    def _answer(self, cmd: Command, fr: Frame, notes: List[str], echo_in: str) -> Result:
        """回答挂起的选择：spawn（三选一 / 预报真的选一张）/ forecast（预报走一步、列出候选）/ select（选手牌）。"""
        ch = self._choices(fr.pending, fr)
        need = ch["need"]
        ok = {"spawn": ("spawn",), "forecast": ("spawn", "forecast"), "select": ("select",)}[need]
        if cmd.verb not in ok:
            return Result(False, echo=echo_in, error="这里要用 %s（%s）" % (need, self._need_text(fr)), choices=ch, notes=notes)
        pre = fr.sim
        if need == "forecast":
            if cmd.verb == "forecast":
                if (ch.get("meta") or {}).get("merged"):            # 候选已经列出来了（一步已走）：只剩“选一张”
                    return Result(False, echo=echo_in, error="候选已经列出来了：用 spawn <牌名/下标> 选一张",
                                  choices=ch, notes=notes)
                if int(ch.get("layer") or 1) > 1:                   # 第一层已经 spawn 选过了：不能再 forecast
                    return Result(False, echo=echo_in, error="第一层已经用 spawn 选过了，不能再 forecast；直接 spawn <档>",
                                  choices=ch, notes=notes)
                return self._forecast_show(fr, ch, pre, notes, echo_in)       # 走一步：把两层合并成一次选择
            return self._forecast_pick(cmd, fr, ch, pre, notes, echo_in)      # spawn：真的选一张
        keys = [o[0] for o in ch["options"]]
        key = None
        arg = cmd.arg
        if need == "select":
            key = self._card_id(arg, fr)
            if key not in keys:
                return Result(False, echo=echo_in, error="%s 不是可选的手牌" % arg, choices=ch, notes=notes)
        else:
            key = self._match(arg, ch["options"])
            if key is None:
                return Result(False, echo=echo_in, error="候选里没有唯一匹配 %r" % arg, choices=ch, notes=notes)
        nxt = fr.pending.resume(key)
        if hasattr(nxt, "state"):
            return self._push(self._frame_after(fr, pre, nxt.state, None, need, echo_in, None), pre, need,
                              "%s %s" % (cmd.verb, arg), None, notes)
        nf = Frame(pre, echo_in, pending=nxt, created=fr.created, generated=fr.generated, names=dict(fr.names),
                   order=fr.order, enemy_hand=fr.enemy_hand, rng_used=fr.rng_used)
        self.stack.append(nf)
        label, by_id = self._label_fn(nf)
        return Result(True, echo="%s %s" % (cmd.verb, arg), kind=need, notes=notes,
                      labels={cid: label(cid) for cid in by_id}, choices=self._choices(nxt, nf), sim_before=pre)

    def _forecast_show(self, fr: Frame, ch: dict, pre, notes: List[str], echo_in: str) -> Result:
        """`forecast`：**只在第一层挂起时能跑**（第一层已经用 `spawn` 选过天气就不能再 forecast）。
        走一步，把 3 天气 × 3 档 = 9 张合并成一次选择摆出来，再由玩家 `spawn <牌名/下标>` 真的选一张
        （`pending_cards` 记这张牌）。"""
        fc = getattr(pre, "forecast", None) or {}
        from kardsmem.forecast_pool import L1_CARDS
        l1_inv = {v: k for k, v in L1_CARDS.items()}
        pairs: List[tuple] = []
        for okey, _lab, _p in ch.get("options") or ():
            w = l1_inv.get(str(okey), str(okey))                    # 第一层的答案现在是类型卡名，换回天气
            row = fc.get(w) if isinstance(fc, dict) else None
            for t in ("light", "medium", "heavy"):
                if isinstance(row, dict) and t in row:
                    pairs.append((str(w), t, str(row[t][0])))
        if not pairs:
            return Result(False, echo=echo_in, error="预报候选表是空的，列不出来（用 spawn 一层一层选，或 undo）",
                          choices=ch, notes=notes)
        pick = ForecastPick(fr.pending, pairs, source=getattr(fr.pending.prompt, "source", None))
        nf = Frame(pre, echo_in, pending=pick, created=fr.created, generated=fr.generated, names=dict(fr.names),
                   order=fr.order, enemy_hand=fr.enemy_hand, rng_used=fr.rng_used)
        self.stack.append(nf)
        label, by_id = self._label_fn(nf)
        return Result(True, echo="forecast", kind="forecast", notes=notes, labels={cid: label(cid) for cid in by_id},
                      choices=self._choices(pick, nf), sim_before=pre)

    def _forecast_pick(self, cmd: Command, fr: Frame, ch: dict, pre, notes: List[str], echo_in: str) -> Result:
        """`spawn <候选>`：**真的选**。两层挂起时一次回答一层（第一层天气 → 第二层档）；`forecast` 合并出来的
        那一步则一次回答两层。选中的牌按原版写进 `pending_cards`（下回合开始才进手牌，见 `sim/engine.py:run`
        的预报分支），后面的模拟就按这张牌走。"""
        layer = int(ch.get("layer") or 1)
        arg = (cmd.arg or "").strip()
        if layer <= 1:                                              # 第一层：兼容直接写天气名（sunny/rain/storm）
            from kardsmem.forecast_pool import L1_CARDS
            arg = L1_CARDS.get(arg.lower(), arg)
        key = self._match(arg, ch["options"])
        if key is None:
            return Result(False, echo=echo_in, error="候选里没有唯一匹配 %r（可选 %s）" % (
                cmd.arg, ", ".join("%s=%s" % (k, lab) for k, lab, _p in ch["options"])), choices=ch, notes=notes)
        pl = next((p for k, _l, p in ch["options"] if k == key), None)
        nxt = fr.pending.resume(key)
        if isinstance(pl, dict) and pl.get("weather") and pl.get("tier"):      # 合并选择：一次就定了天气 + 档
            picked = [str(pl["weather"]), str(pl["tier"])]
        elif layer > 1:                                             # 第二层：天气记在提示的 meta 里，回显写全路径
            picked = [str((getattr(fr.pending.prompt, "meta", {}) or {}).get("weather") or ""), str(key)]
        else:
            picked = [str(key)]
        picked = [p for p in picked if p]
        echo = " ".join(["spawn"] + picked)
        if hasattr(nxt, "state"):                                   # 选完 ⇒ 定案
            st2 = nxt.state
            got = [p for p in getattr(st2, "pending_cards", []) if p not in getattr(pre, "pending_cards", [])]
            res = self._push(self._frame_after(fr, pre, st2, None, "forecast", echo_in, None), pre, "forecast", echo, None, notes)
            res.choices = dict(ch, need="forecast", done=True, picked=True, picks=picked,
                               card=(str(got[-1][0]) if got else ""), options=())
            return res
        nf = Frame(pre, echo_in, pending=nxt, created=fr.created, generated=fr.generated, names=dict(fr.names),
                   order=fr.order, enemy_hand=fr.enemy_hand, rng_used=fr.rng_used)   # 还挂着第二层：压一帧等玩家选档
        self.stack.append(nf)
        label, by_id = self._label_fn(nf)
        return Result(True, echo=echo, kind="forecast", notes=notes, labels={cid: label(cid) for cid in by_id},
                      choices=self._choices(nxt, nf), sim_before=pre)

    @staticmethod
    def _choices(run, fr: Frame) -> dict:
        """挂起提示 → 选项表。`need`：choose（抉择，写在 deploy/play 里）| spawn | forecast | select。"""
        pr = run.prompt
        meta = dict(getattr(pr, "meta", {}) or {})
        need = "choose" if pr.kind == "choose_one" else "spawn" if meta.get("spawn") else \
            "forecast" if meta.get("forecast") else "select"
        opts = []
        merged = bool(meta.get("merged"))
        layer = int(getattr(pr, "layer", 1) or 1)
        for o in pr.options:
            lab = o.label or str(o.key)
            pl = o.payload
            if need == "forecast" and merged:
                lab = (pl or {}).get("name") if isinstance(pl, dict) else lab   # 合并出来的候选就是牌名
            elif need == "forecast" and layer > 1:
                lab = "%s: %s" % (o.key, lab)                       # 第二层：候选是档，牌名是选了这一档会拿到的牌
            # 第一层：答案就是**类型卡内部名**（蓝天/薄雾/狂风），原样列；第二层的 9 张**不提前列**
            # （列出来玩家就不需要跑 forecast 了，用户 2026-10-19 要求）
            opts.append((o.key, lab, pl))
        return {"kind": pr.kind, "options": opts, "need": need, "layer": getattr(pr, "layer", 1), "meta": meta}

    def _frame_after(self, fr: Frame, before, after, a, kind: str, cmd_text: str, eff, slot: Optional[int] = None) -> Frame:
        """算出新帧：登记模拟里新出现的单位/手牌（n1/g1 编号）、更新各行从左到右的次序、跟踪敌方手牌。"""
        names = dict(fr.names)
        created, generated = list(fr.created), list(fr.generated)
        live_ids = {c.obj.CardID for c in self.base_st.cards}
        played = before.hand.get(a.src) if a is not None else None
        new_units = sorted(set(after.units) - set(before.units), key=lambda x: -x if isinstance(x, int) else 0)
        for uid in new_units:
            if uid not in created and uid not in live_ids:
                created.append(uid)
                names[uid] = (played.name if played is not None and kind == "deploy" else "") or ""
        for hid in after.hand:
            if hid not in before.hand and hid not in generated:
                generated.append(hid)
                names[hid] = after.hand[hid].name
        # 各行次序
        order = {}
        keys = set(fr.order) | {(int(u.side), u.row) for u in after.units.values()}
        for key in keys:
            side, rw = key
            kept = [i for i in fr.order.get(key, ()) if i == "hq" or (i in after.units and int(after.units[i].side) == side and after.units[i].row == rw)]
            add = sorted(i for i, u in after.units.items() if (int(u.side), u.row) == key and i not in kept)
            tkey = (int(after.me), "back") if kind == "deploy" else (int(after.me), "frontline") if kind == "move" else None
            mover = set(new_units) if kind == "deploy" else {a.src} if (kind == "move" and a is not None) else set()
            for i in add:
                if slot is not None and key == tkey and i in mover:
                    kept.insert(max(0, min(slot - 1, len(kept))), i)
                else:
                    kept.append(i)
            order[key] = tuple(kept)
        # 敌方手牌：弃牌 / 抽牌 / 得牌
        eh = list(fr.enemy_hand)
        if isinstance(eff, dict):
            drop = set(eff.get("discard_ids") or ())
            eh = [x for x in eh if x[0] not in drop]
            for k in ("opp_draw", "opp_gain_cards"):
                n = int(eff.get(k) or 0)
                for j in range(n):
                    eh.append((-(7000 + len(eh) + j), "?"))
        return Frame(after, cmd_text, created=tuple(created), generated=tuple(generated), names=names, order=order,
                     enemy_hand=tuple(eh), rng_used=fr.rng_used or bool(isinstance(eff, dict) and eff.get("rng_draws")))

    def _push(self, nf: Frame, pre, kind: str, echo: str, eff, notes: List[str]) -> Result:
        """压栈并生成结果（差异 = 这条命令 / 这一串 play+spawn 之前的场面 → 现在）。"""
        label, by_id = self._label_fn(nf)

        def hand_name(h):
            # 带上短号（h2/g1）：同名两张牌**不是**同一张——位置不同、被贴的膜也可能不同，
            # 出牌时抽到的同名卡换成另一张时，diff 里要能看出来（用户 2026-10-19 提出）。
            return label(h)
        diff = diff_sims(pre, nf.sim, label, hand_name)
        prev = self.stack[-1]
        gone = [(i, n) for i, n in prev.enemy_hand if (i, n) not in nf.enemy_hand]
        came = [x for x in nf.enemy_hand if x not in prev.enemy_hand]
        if gone:
            diff.append("enemy hand -%s" % ", ".join("%s %s" % (label(i), n) for i, n in gone))
        if came:
            diff.append("enemy hand +%d card(s)" % len(came))
        self.stack.append(nf)
        return Result(True, echo=echo, kind=kind, eff=eff, diff=diff, notes=notes,
                      labels={cid: label(cid) for cid in set(by_id) | set(nf.created) | set(nf.generated)},
                      sim_before=pre, sim_after=nf.sim)

    def _action(self, sim, cmd: Command, cid, dst, fr: Frame, notes: List[str]):
        from policy.search import _gen_actions
        from sim.engine import A
        if cmd.verb in ("play", "deploy"):
            c = sim.hand.get(cid)
            if c is None:
                raise ValueError("%s 不在模拟的手牌里（已经打出去了，或不是我方手牌）" % cmd.src)
            if cmd.verb == "play" and c.is_unit():
                raise ValueError("%s 是单位，要用 deploy（play 只打指令）" % cmd.src)
            if cmd.verb == "deploy" and not c.is_unit():
                raise ValueError("%s 是指令，要用 play（deploy 只部署单位）" % cmd.src)
            kind = "deploy" if c.is_unit() else "order"
        elif cmd.verb == "attack":
            if cid not in sim.units:
                raise ValueError("%s 不在模拟的场上（已死亡/离场？）" % cmd.src)
            kind = "attack"
        else:
            if cid not in sim.units:
                raise ValueError("%s 不在模拟的场上（已死亡/离场？）" % cmd.src)
            kind = "move"
        echo = " ".join(x for x in (cmd.verb, cmd.src, str(cmd.slot) if cmd.slot else "",
                                    ("choose %d" % cmd.choice) if cmd.choice is not None else "",
                                    ("with " + cmd.dst) if cmd.dst else "") if x)
        try:
            gens = [g for g in _gen_actions(sim) if g.kind == kind and g.src == cid and g.dst == dst]
        except Exception as ex:                                    # noqa: BLE001
            gens = []
            notes.append("生成合法走法时出错（%s），按命令原样模拟" % type(ex).__name__)
        if gens:
            return gens[0], kind, echo
        # 这张牌/单位在模拟里**有**合法走法，只是没有命中你写的目标 ⇒ 目标不合法，直接拒绝（以前只记一条提醒、照常模拟，
        # 结果是美国炮兵这类“只能指向敌方”的部署，指友方也被模拟通过）。这张牌一条合法走法都没有时才照原样模拟（判据可能有缺漏）。
        try:
            legal = [g for g in _gen_actions(sim) if g.kind == kind and g.src == cid]
        except Exception:                                          # noqa: BLE001
            legal = []
        if legal and kind in ("deploy", "order", "attack"):
            label, _by = self._label_fn(fr)
            tk_of = lambda d: "ehq" if d == "hq" else label(d).partition(" ")[0]          # noqa: E731
            valid = sorted({tk_of(g.dst) for g in legal if g.dst is not None})
            ok_none = any(g.dst is None for g in legal)
            if dst is not None and not valid:
                raise ValueError("%s 不需要目标，去掉 with（%s）" % (cmd.src, echo))
            if dst is None:                                        # 缺目标：照旧带提醒空跑（点选界面在提交前就会卡住，命令行保持宽松）
                notes.append("%s 通常需要指向目标（可选 %s）；这里按不带目标空跑" % (cmd.src, ", ".join(valid)))
                return A(kind, cid, None, (sim.hand[cid].cost if kind in ("deploy", "order") else 0.0), echo), kind, echo
            raise ValueError("%s 不能指向 %s：模拟里合法的目标只有 %s%s" % (
                cmd.src, cmd.dst, ", ".join(valid) or "（无）", "（也可以不带目标）" if ok_none else ""))
        notes.append("这一步不在模拟生成的合法走法里（费用不够/目标不合法/不能行动？）——仍按命令原样模拟，游戏本体可能拒绝")
        h = sim.hand.get(cid) if kind in ("deploy", "order") else None
        return A(kind, cid, dst, h.cost if h is not None else 0.0, echo), kind, echo
