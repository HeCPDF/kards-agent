# -*- coding: utf-8 -*-
"""效果摘要（`engine/effectvm.to_effects` 的 dict）→ 一行行"指令式"的效果清单。纯函数，离线可测。

不翻译、不润色（游戏的描述文本和代码难以一句句对上，所以我们把**代码实际做的事**写一遍）：
    damage 3 -> target; pin -> target; give keyword [ambush] -> target; draw 2; spawn <名字> @ own frontline
键的含义见 `engine/state.py` 的 EFFECT KEYS 与 `engine/effectvm.py::to_effects`。
原则：认得的键写成指令，**不认得的键原样列出（`键: 值`）**——不猜、不吞；`uncertain`（被随机污染、不一定发生）单独标 `maybe`。
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional

_HIDDEN_SUFFIX = ("_ids", "_tmp")
_HIDDEN = {"uncertain", "chance", "_src", "_seed", "draw_pre", "draw_seq", "fight_dmg", "outcomes_mode",
           "deck_shuffles", "deck_shuffle_skip", "playing_side", "to_deck_position"}


def _n(v) -> str:
    return ("%+d" % v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _cid(id_name, cid) -> str:
    return id_name(cid) if id_name else "card#%s" % cid


def _name(names: Optional[Callable[[str], str]], n) -> str:
    n = str(n)
    return names(n) if names else n


def lines(eff: dict, names: Optional[Callable[[str], str]] = None,
          draw_fn: Optional[Callable[[dict], Optional[str]]] = None,
          id_name: Optional[Callable[[int], str]] = None) -> List[str]:
    """效果摘要 → 指令行列表（空 ⇒ 没有可展示的即时效果）。`names`：内部名 → 显示名。
    `id_name(card_id)`：场上某张牌的显示名（逐张记账的 `damage_ids` / `damage_aoe_ids` 用；没给就写 `card#id`）。
    `draw_fn(eff)`：把这条效果里的抽牌换成**真牌**（返回 `draw` 后面的文字，如 `card_unit_x, card_event_y`）；返回 None ⇒ 退回 `draw N`。

    ★ 行的顺序 = 效果摘要里键的**首次出现顺序** = 脚本执行顺序（`to_effects` 按录制顺序写入）。脚本里"先失一槽、再打伤害"，
    这里就先写 `slot -1`、再写 `damage …`；不要为了好看按固定顺序重排（用户 2026-10-18 指出过）。"""
    e = dict(eff or {})
    out: List[str] = []

    def spawn(v):
        return ["spawn %s @ %s %s" % (_name(names, s.get("name")), "own" if s.get("mine", True) else "enemy",
                                      {"frontline": "frontline", "back": "support"}.get(s.get("row"), s.get("row") or ""))
                for s in v]

    def pair(v, fmt):
        return [fmt % (_n(v[0]), _n(v[1]))] if isinstance(v, (list, tuple)) and len(v) == 2 else ["buff %s" % (v,)]

    handlers = {
        "damage": lambda v: ["damage %s -> target" % v],
        "damage_hq": lambda v: ["damage %s -> enemy_hq" % v],
        "damage_own_hq": lambda v: ["damage %s -> own_hq" % v],
        "self_damage": lambda v: ["damage %s -> self" % v],
        "damage_aoe": lambda v: ["damage_aoe %s" % (v,)],
        "damage_ids": lambda v: ["damage %s -> %s" % (n, _cid(id_name, c)) for c, n in v.items()] if isinstance(v, dict) else [],
        "damage_aoe_ids": lambda v: ["damage_aoe -> %s" % ", ".join(_cid(id_name, c) for c in v)] if v else [],
        "destroy": lambda v: ["destroy -> target"],
        "retreat": lambda v: ["retreat -> target"],
        "pin": lambda v: ["pin -> target"],
        "unpin": lambda v: ["unpin -> target"],
        "give": lambda v: ["give keyword [%s] -> target" % ", ".join(str(k) for k in (v if isinstance(v, (list, tuple)) else [v]))],
        "buff": lambda v: pair(v, "buff %s/%s -> target"),
        "armor": lambda v: ["armor %s" % _n(v)],
        "opcost": lambda v: ["opcost %s" % _n(v)],
        "forecast": lambda v: ["forecast"],                        # 预报牌：弹两层选择（模拟里 `forecast` 走一步列出候选，再 spawn 选）
        "choose_spawn": lambda v: ["choose_spawn"],                # 三选一（模拟里用 spawn 命令回答）
        "heal_hq": lambda v: ["defense %s -> own_hq" % _n(v)],
        "heal_opp_hq": lambda v: ["defense %s -> enemy_hq" % _n(v)],
        "kredit": lambda v: ["kredit %s" % _n(v)],
        "kredit_next": lambda v: ["kredit_next %s" % _n(v)],
        "slot": lambda v: ["slot %s" % _n(v)],
        "gain_cards": lambda v: [] if e.get("gain_names") else ["gain_cards %s" % v],
        "gain_names": lambda v: ["gain card " + ", ".join(_name(names, n) for n in v)],
        "spawn_cards": spawn,
        "spawn": lambda v: [] if e.get("spawn_cards") else ["spawn %s unit(s)" % v],
        "deck_add_name": lambda v: ["deck_add %s" % _name(names, v)],
    }

    def draw(v):
        real = draw_fn(eff) if draw_fn else None
        return ["draw %s" % (real if real is not None else v)]
    handlers["draw"] = draw
    quiet = {"deck_add", "deck_adds", "deck_shuffle", "deck_add_shuffle", "deck_add_bottom", "deck_add_wo_shuffle", "deck_add_side"}
    for k in list(e):                                                # dict 保持插入顺序 = 脚本顺序
        v = e[k]
        if (k in _HIDDEN or k.endswith(_HIDDEN_SUFFIX) or k in quiet) and k not in ("damage_ids", "damage_aoe_ids"):
            continue
        if k in handlers:
            if v not in (None, 0, False, [], ()) or k == "deck_add_name":
                out.extend(handlers[k](v))
        elif k.startswith("opp_"):
            if v not in (None, 0, False, [], ()):
                out.append("opp %s %s" % (k[4:], _n(v)))
        elif v not in (None, 0, False, [], ()):                       # 没认得的键：原样列出
            out.append("%s: %s" % (k, v))
    return out


def describe(eff: Optional[dict], names: Optional[Callable[[str], str]] = None,
             draw_fn: Optional[Callable[[dict], Optional[str]]] = None,
             id_name: Optional[Callable[[int], str]] = None) -> List[str]:
    """带分支与 maybe 标注的完整描述。`eff=None` ⇒ 取不到。"""
    if eff is None:
        return ["(effect unavailable)"]
    if isinstance(eff.get("outcomes"), list):
        mode = eff.get("outcomes_mode")
        res = ["choose one of:" if mode == "max" else "random outcomes:"]
        for w, sub in eff["outcomes"]:
            body = "; ".join(lines(sub, names, draw_fn, id_name)) or "(nothing)"
            res.append("  - %s%s" % ("" if mode == "max" else "%.0f%% " % (100 * float(w)), body))
        return res
    res = lines(eff, names, draw_fn, id_name)
    unc = eff.get("uncertain")
    if isinstance(unc, dict) and unc:
        res.append("maybe: " + "; ".join(lines(unc, names, draw_fn, id_name)))
    return res or ["(no on-play effect)"]


def one_line(eff: Optional[dict], names: Optional[Callable[[str], str]] = None, limit: int = 160) -> str:
    s = "; ".join(describe(eff, names))
    return s if len(s) <= limit else s[:limit - 1] + "…"
