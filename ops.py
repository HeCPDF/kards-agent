#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ops.py —— 执行层原语（读内存决策 + OCR 取坐标 + 鼠标执行 + 回读验证）。

踩过的坑与对应修法（2026-09-21 实机一局验证）：
  1. ★「单位无法在部署的回合中移动或攻击。」—— 游戏会拦，而内存的
     movement_left/attack_left 含 Fury 不含 Blitz、对刚部署单位仍读到 1。现在用
     `card.enter_play_on_turn` + `BoardState.turn` + 闪击 显式判断（can_act_now）。
  2. 有 `selectTargetOnPlayedFromHand` 的单位：拖到后排 **之后还要点目标**。
     这段窗口里卡还在手里，"卡是否离手"不能当失败判据 —— 现在会去点目标再验一次。
  3. 攻击目标坐标**每回合重取**（敌方总部屏幕 x 实测在 426..781 之间漂移）。
  4. 手牌 x：先用 `hand_calibrate` 量扇形左右边缘 → 查 `config/hand_layout.json`
     （该表按 left_edge 作 key），再带 ±40px 重试阶梯。
  5. ★ 场上卡坐标改用**行模型**（见 screen_map_detailed）。旧实现 zip(内存卡, OCR框)
     按名次配对：OCR 漏一个框，该行后续每张都会拿到前一张的坐标（整行左移一位），
     或直接丢坐标。现在坐标由内存的 side/location/locationNumber 算出，
     OCR 只在"框数 == 卡数"时用来吸附微调。

用法:
  python ops.py state
  python ops.py rows                                # 行模型 vs OCR 逐行对照（不动鼠标）
  python ops.py play   <card_id> [target_card_id]   # 出牌（自动处理"部署后选目标"）
  python ops.py front  <card_id> [x]                # 后排单位上前线
  python ops.py attack <card_id> hq|front|back<i>|guard<i>   # 攻击（敌有 guard 时打 hq 会被拒，先打 guard）
  python ops.py end
  python ops.py surrender --yes                     # 投降（不可逆，无二次确认）
  python ops.py next                                # 等下一个我方回合
"""
from __future__ import annotations

import json
import os
import sys
import time

import agentpath  # noqa: F401,E402  —— 接上 vendor/ 和本项目根

import cv2  # noqa: E402
import win  # noqa: E402          vendor：窗口/DPI/坐标/截图
import actions  # noqa: E402      vendor：鼠标原语
import deploy  # noqa: E402       vendor：drag_deploy
import handedge as HC  # noqa: E402   vendor：手牌扇形左右边缘（内存里没有的量）
import board_api as BA  # noqa: E402

# OCR 读盘面（上游的 board.read_field）**只作核对手段**，不是对等后端。
# 盘面的权威来源是内存；实测两者完全对得上之后，OCR 就退居验证角色了。
# 上游没 checkout 时 B 为 None，`--ocr` 之类的核对路径自己跳过。
if agentpath.upstream_src():
    try:
        import board as B  # noqa: E402
    except Exception:  # pragma: no cover
        B = None
else:
    B = None
HAND_Y = 700
FRONT_Y = 380
# 三行的 y（本会话实测：敌方排 174~184 / 前线 349~352 / 我方支援排 523~527）。
# 支援排的 y 下界（用来判"这次移动是不是往支援阵线拖"，见 act_move 的移动单向校验）。
SUPPORT_ROW_MIN_Y = 430
SETTLE_HAND = 0.30
SHOTS = agentpath.shots("play")
os.makedirs(SHOTS, exist_ok=True)


# ---------------------------------------------------------------- 基础设施
_focus_t = [0.0]


def hwnd(focus=True):
    """拿游戏窗口句柄，**顺手把它置前**。

    ★ 没焦点的窗口会把点击吃掉：鼠标事件发出去了，游戏没反应，
      内存也一个字节都不变 —— 看起来像"坐标错了"，其实是焦点错了。
      实测踩过两次：换牌界面点牌没反应；`act_pick` 白试掉前两个探针位。
      所以置前放在 hwnd() 里，而不是指望每个动作函数自己记得调。
      只读内存/截图的地方用 `hwnd(focus=False)`，别去抢用户的焦点。
    """
    win.set_dpi_aware()
    for w in win.find_by_process("kards"):
        h = w["hwnd"]
        if focus and time.time() - _focus_t[0] > 1.0:
            win.bring_to_front(h)
            time.sleep(0.25)
            _focus_t[0] = time.time()
        return h
    return None


def snap(h, tag=None):
    f = win.capture_client_bgr(h, allow_screen_fallback=True)
    if f is not None and tag:
        cv2.imwrite(os.path.join(SHOTS, tag + ".png"), f)
    return f


def src():
    return BA.open_source("mem")


def read_all(park=True):
    h = hwnd()
    if park:
        try:
            actions.park_cursor(h, settle=0.35)   # 防悬停放大面板挡住旁边的卡
        except Exception:
            pass
    f = snap(h)
    st = src().snapshot()
    field = B.read_field(f, debug=False) if (B and f is not None) else {}
    return h, f, st, field


# ---------------------------------------------------------------- 屏幕坐标映射
# ★ 行模型（P0，取代单帧 OCR 定位）。
#
# 三行都是**居中排布**：行内卡数一变，整行重算。
#     x = center + (rank - (n - 1) / 2) * pitch
# rank = 行内按 locationNumber 升序的**密集名次**，n = 行内卡数。
#
# ⚠ 后排的 n **含总部** —— 实测 (snapshot_kardsmem_2026-09-21.json)：
#     local  back slot=0 (B-17)        / local  hq slot=1
#     enemy  hq   slot=0               / enemy  back slot=1 (104th INF)
# 总部与后排单位共用同一条 locationNumber 序列 ⇒ **总部 x 不是常量**。
#
# OCR 只用来校验：行内框数 == 内存卡数、且每张 |x_model - x_ocr| < pitch/2 时，
# 才把坐标吸附到 OCR 的实测值（更准），否则一律用模型值。
# 绝不再 zip(units, ocr) —— 漏检一个框，该行后续每张都会拿到前一张的坐标。

ROW_SPECS = {
    "enemy_back": dict(y=179, center=639, pitch=142, ocr_key="enemy_support"),
    "local_back": dict(y=525, center=642, pitch=143, ocr_key="our_support"),
    # ★ 2026-09-24 用 rowcalib 重新标定（231 个 n=3 真实样本，最大残差 10.9px，
    #   之前的 645/136 是早期目测/小样本，没有 HQ 干扰所以这次改得放心
    #   ——不像 local_back/enemy_back 那两行有"总部窄不窄"的悬案（见 §11.2）。
    "frontline":  dict(y=350, center=649, pitch=148, ocr_key="frontline"),
}
SNAP_TOL = 0.5          # |x_model - x_ocr| < pitch * SNAP_TOL 才认为是同一张


def row_x(spec, rank, n):
    """居中排布的行内第 rank 张（0 起）的客户区 x。"""
    return int(round(spec["center"] + (rank - (n - 1) / 2.0) * spec["pitch"]))


def _row_members(st, row):
    """行成员（含总部），按 locationNumber 升序。slot 为 None 的排到最后。"""
    if row == "frontline":
        cs = [c for c in st.cards if c.location == "frontline"
              and c.card_type not in ("order", "counter")]
    else:
        side = "local" if row == "local_back" else "enemy"
        cs = [c for c in st.cards if c.side == side
              and c.location in ("back", "hq")
              and c.card_type not in ("order", "counter")]
    return sorted(cs, key=lambda c: (c.slot is None, c.slot if c.slot is not None else 0,
                                     c.card_id))


def screen_map_detailed(st, field):
    """→ ({card_id: {...}}, warn[])

    每项：x / y / source('model'|'ocr-confirmed') / row / rank / n / slot / x_model / x_ocr
    """
    out, warn = {}, []
    for row, spec in ROW_SPECS.items():
        members = _row_members(st, row)
        n = len(members)
        if not n:
            continue
        slots = [c.slot for c in members]
        if any(s is None for s in slots):
            warn.append("%s: 有卡读不到 locationNumber（%s）" % (row, slots))
        else:
            dense = list(range(n))
            if sorted(slots) != dense:
                warn.append("%s: locationNumber 非密集 %s（按名次排布，若与实际错位见此行）"
                            % (row, slots))

        ocr_cx = sorted(int(u["cx"]) for u in (field.get(spec["ocr_key"]) or []))
        usable = len(ocr_cx) == n
        if not usable and ocr_cx:
            warn.append("%s: mem=%d ocr=%d ⇒ 只用行模型，不吸附" % (row, n, len(ocr_cx)))

        for rank, c in enumerate(members):
            xm = row_x(spec, rank, n)
            x, xo, source = xm, None, "model"
            if usable:
                xo = ocr_cx[rank]
                if abs(xo - xm) < spec["pitch"] * SNAP_TOL:
                    x, source = xo, "ocr-confirmed"
                else:
                    warn.append("%s rank=%d: 模型 x=%d 与 ocr x=%d 差 %d（>pitch/2）⇒ 用模型"
                                % (row, rank, xm, xo, abs(xo - xm)))
            out[c.card_id] = {
                "x": x, "y": spec["y"], "source": source, "row": row,
                "rank": rank, "n": n, "slot": c.slot, "x_model": xm, "x_ocr": xo,
            }
    return out, warn


def screen_map(st, field):
    """兼容口：{card_id: (x, y)}, warn[]。坐标来源见 screen_map_detailed。"""
    d, warn = screen_map_detailed(st, field)
    return {k: (v["x"], v["y"]) for k, v in d.items()}, warn


# ---------------------------------------------------------------- 手牌 x
def _layouts():
    p = os.path.join(agentpath.AGENT_ROOT, "config", "hand_layout.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)["layouts"]


def hand_probes(n, frame):
    """★ 以**张数**为准取表项（这张表每张数只有一项）；边缘测量只用来校验/告警。
    之前的写法用 left_edge 最接近 + 15 分加成，结果 9 张手牌被配到 5 张的表项，
    出牌 x 直接差了 140px。（教训：张数是从内存读的权威值，别让像素盖过它。）"""
    lay = _layouts()
    entry = None
    for v in lay.values():
        try:
            if int(v.get("count", -1)) == n:
                entry = v
                break
        except Exception:
            continue
    measured = None
    if frame is not None and HC is not None:
        try:
            measured = HC.detect_left_edge(frame)
        except Exception:
            measured = None
    if entry is None and measured:
        best, bestd = None, 9999
        for k, v in lay.items():
            try:
                d = abs(int(v.get("left_edge", k)) - int(measured))
            except Exception:
                continue
            if d < bestd:
                best, bestd = v, d
        entry = best
    le = (entry or {}).get("left_edge")
    probes = (entry or {}).get("probes")
    # ★ 不要用 detect_left_edge 去"平移"整排：实测它给出的左缘（413）和真实卡面左缘（355）
    #   差得远，拿它平移会把落点推到隔壁那张牌上（实测把 KINGFISHER 当 TASK FORCE 打出去了）。
    #   表本身是准的（count=7 首张 390、count=8 首张 388，与实测一致），所以只用表 + 重试阶梯。
    return probes, le


def hand_x_list(st, card, frame):
    hand = sorted(st.hand("local"), key=lambda c: (c.slot if c.slot is not None else 99))
    n = len(hand)
    slot = card.slot if card.slot is not None else 0
    probes, le = hand_probes(n, frame)
    if probes and slot < len(probes):
        base = int(probes[slot])
    else:
        base = int(490 + slot * 76)
    cands = [base, base - 40, base + 40, base - 22, base + 22]
    return [x for x in cands if 300 <= x <= 1000], (n, probes, le)


# ---------------------------------------------------------------- 行动判据
def can_act_now(owner: BA.Card, turn) -> bool:
    """本回合能不能动：部署当回合不能动（除非闪击）。
    内存的 movement_left/attack_left 对刚部署单位仍读到 1（且含 Fury、不含 Blitz），不能作依据。"""
    if owner is None:
        return False
    if "blitz" in (owner.keywords or []):
        return True
    ept = owner.enter_play_on_turn
    if ept is None or turn is None:
        return True                      # 读不到就不拦（保持旧行为）
    return int(ept) != int(turn)


_km_session = None


def _km():
    """惰性拿一个 kardsmem 只读会话（用来读"逐实例被贴效果"）。"""
    global _km_session
    if _km_session is None:
        from kardsmem.proc import attach as _attach
        _km_session = _attach()
    return _km_session


def is_pinned(card: BA.Card):
    """**压制（pin）**：百科原文「被压制的单位**不能移动或攻击**。压制会于单位所有者
    下个回合结束时移除。」—— 与"抑制（suppress）"是两回事。

    读侧：`receivedAbilitiesFromCards` 里出现 `pinned`，或 `buffsFromCards` 里出现 `combat_pinned`。
    返回 True / False / None（None = 读不出，调用方不要拦）。
    """
    ptr = (card.raw or {}).get("ptr") if card is not None else None
    if not ptr:
        return None
    try:
        from kardsmem import cards as C
        eff = C.read_live_effects(_km(), ptr)
    except Exception:                                        # noqa: BLE001
        return None
    for ab in eff.get("received_abilities") or []:
        if "pinned" in (ab.get("ability") or "").lower():
            return True
    for b in eff.get("buffs_from_cards") or []:
        for k in (b.get("buffs") or {}):
            if "pinned" in k.lower():
                return True
    return False



# ---------------------------------------------------------------- 动作回执（§7.6g）
# ★ 判「这一步生效没有」的正解是**对局动作流**，不是副作用。
#
#   以前用的两种判法都不可靠：
#     * 上游：动作后 OCR 抓屏幕横幅 —— 提示是给人看的、短命、有损。
#     * 我们：看手牌少没少一张 / 指挥点变没变 / 整盘状态签名变没变
#       —— **拿副作用猜因果**。手牌不变可能是"部署后还要选目标、牌还留在手里"；
#          手牌变了也可能是别的效果让你抽了牌；整盘签名在对手动画播放时也会变。
#
#   客户端认下来的动作**一定会进 `AllMatchActions`**，被本地判据拦掉的根本不出现。
#   ⇒ 动作前 `_mark()`，动作后 `_receipt()`。
#
#   ⚠ `MatchLog.locate()` 首次要扫一遍 GObjects（~4s）⇒ `_mark()` 会先预热，
#     免得把定位耗时算进动作窗口、把动作当成"动手前就有的"。
_ml = [None]


def _matchlog():
    """惰性拿动作流读取器。读不到（不在对局里/版本不符）返回 None，**不抛**。"""
    if _ml[0] is None:
        try:
            from kardsmem.matchlog import MatchLog
            m = MatchLog(_km())
            _ml[0] = m if m.locate() else False
        except Exception as e:                                   # noqa: BLE001
            print("  [动作流不可用] %s" % e)
            _ml[0] = False
    return _ml[0] or None


def _mark():
    """动手之前记游标。拿不到动作流返回 None（调用方退回旧判据）。"""
    m = _matchlog()
    return m.mark() if m else None


def _receipt(mark, action_type=None, timeout=2.0):
    """→ (生效了没有, [动作])。`mark` 是 None（没有动作流）时返回 (None, [])，
    表示**不知道** —— 调用方这时才退回"看副作用"那套旧判据。"""
    if mark is None:
        return None, []
    m = _matchlog()
    if not m:
        return None, []
    r = m.receipt(mark, action_type, timeout=timeout)
    return bool(r["ok"]), r["actions"]


def _notify_texts(limit=4):
    """动作被拒时游戏弹的提示（**权威理由**，§7.6f）。读不到就返回空。"""
    try:
        from kardsmem.notify import NotifyWatcher
        from kardsmem.locres import translator
        w = _nw[0]
        if w is None:
            w = _nw[0] = NotifyWatcher(_km())
            w.poll()
        tr = translator()
        out, seen = [], set()
        for r in w.poll():
            t = tr(r["text"])
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out[:limit]
    except Exception:                                            # noqa: BLE001
        return []


_nw = [None]

# ---------------------------------------------------------------- 目标挑选
def pick_target(st, field, exclude=()):
    """给"部署后要选目标"的单位挑一个目标：优先攻高且未被压制的敌方单位，其次前线，最后总部。"""
    m, _ = screen_map(st, field)
    best = None
    for c in st.cards:
        if c.side != "enemy" or c.card_id in exclude:
            continue
        if c.location not in ("back", "hq", "frontline"):
            continue
        if c.card_type in ("order", "counter"):
            continue
        if c.card_id not in m:
            continue
        if c.is_suppressed:
            continue
        score = (c.attack or 0) + (10 if c.location == "frontline" else 0)
        if best is None or score > best[0]:
            best = (score, c.card_id)
    if best:
        return best[1], m[best[1]]
    hq = st.hq.get("enemy")
    if hq is not None and hq.card_id in m:
        return hq.card_id, m[hq.card_id]
    return None, None


# ---------------------------------------------------------------- 动作
def _blocked_by_pick(what):
    """选择界面开着时，出牌/移动/攻击全部无效。

    ★ `act_end` 一直有这道护栏，其他动作没有 —— 于是预报面板一开，
      自动对局就在那里空转：每次拖拽都"成功发出鼠标事件"、每次 k 都不变。
      而且面板还会被翻页收到屏幕右侧（只剩一个 `<` 箭头），
      这时屏幕上**看不到选择界面**但 chooseOneActive 仍为 1。
    """
    try:
        ps = pick_state()
    except Exception:                                        # noqa: BLE001
        return False
    if not (ps and ps.get("choose_active")):
        return False
    how = ("`ops.py pickhand <index>`" if ps.get("selecting_hand_target")
           else "`ops.py pick <index>`")
    print("★ 拒绝%s：选择界面还开着（chooseOneActive=1）—— 此时任何出牌/"
          "移动/攻击都不会生效。先用 %s。" % (what, how))
    return True


def act_deploy(card_id, target_id=None, force=False):
    """出牌。自动处理两种目标语义：
       * 指令(order)且 needs_hand_target → 直接把牌拖到目标上
       * 单位且 needs_hand_target       → 先拖到后排部署，再点目标
    """
    if _blocked_by_pick("出牌"):
        return False
    h, f, st, field = read_all()
    card = find_card(st, card_id)
    if card is None:
        print("card %s not in local hand" % card_id)
        return False

    # ★ 出牌前的费用自检。盘面变得很快，上一条命令读到的指挥点可能已经过期；
    #   不拦的话重试阶梯会拿着不够的钱连拖 3 次，每次都"成功发出鼠标事件"却什么也没发生。
    #   注意：实际花费 = 卡面费用 + Σ 被指向目标的 KreditsTax_AsEnemyTarget（§4.10），
    #   所以这里只拦"卡面费用都不够"这种确定拦得住的情况，不做更精细的推断。
    k_now = (st.kredits or {}).get("local")
    cost = card.kredit_cost if card.kredit_cost is not None else None
    if k_now is not None and cost is not None and cost > k_now and not force:
        # ★ §7.6f：进程外判据**只挑不判**。这里仍然拦，是因为"卡面费用都不够"
        #   是少数几个确定拦得住的情形（不拦会拿着不够的钱连拖 3 次）。
        #   但**必须留强制口** —— 判据错了不能把正确招法永久屏蔽掉。
        print("指挥点不够：%s 要 %s 点，现在只有 %s 点（turn=%s）—— 不出手"
              "（确信要发就传 force=True）"
              % ((card.name or card_id), cost, k_now, st.turn))
        return False

    # 指令 + 需要目标 → 直接拖到目标
    if card.needs_hand_target and card.card_type == "order":
        tid, tpos = pick_target(st, field, exclude=(target_id,) if target_id else ())
        if target_id:
            m, _ = screen_map(st, field)
            tpos = m.get(target_id)
        if tpos is None:
            print("order %s needs a target but none resolvable" % card_id)
            return False
        xs, info = hand_x_list(st, card, f)
        mk = _mark()
        print("  order %s -> target %s @%s  (hand n=%s probes=%s left=%s)" % (
            card_id, target_id or tid, tpos, info[0], info[1], info[2]))
        for x in xs:
            ok = deploy.drag_deploy(h, x, HAND_Y, drop=tpos, hover_wait=SETTLE_HAND)
            time.sleep(0.9)
            after = src().snapshot()
            got, _acts = _receipt(mk, "XActionPlayCardFromHand", timeout=1.2)
            played = got if got is not None else (
                find_card(after, card_id) is None
                or (after.kredits.get("local") or 0) != (st.kredits.get("local") or 0))
            if played:
                snap(h, "02_order_%s" % card_id)
                print("   -> played at hand_x=%s" % x)
                return True
        snap(h, "03_fail_order_%s" % card_id)
        print("   -> FAILED")
        return False

    # 普通出牌（单位/非指向指令）
    mk = _mark()
    occupied = [int(u["cx"]) for u in (field.get("our_support") or [])]
    rows = [r for r in (field.get("rows") or []) if r.get("side") == "our"]
    y = int(rows[-1]["cy"]) if rows else 526
    slots = deploy_slots(occupied, y) or [(640, y)]
    snap(h, "01_before_deploy_%s" % card_id)
    for (dx, dy) in slots[:2]:
        cur = src().snapshot()
        c2 = find_card(cur, card_id)
        if c2 is None:
            return True
        xs, info = hand_x_list(cur, c2, f)
        for x in xs[:2]:
            ok = deploy.drag_deploy(h, x, HAND_Y, drop=(dx, dy), hover_wait=SETTLE_HAND)
            time.sleep(0.85)
            mid = src().snapshot()
            got, _acts = _receipt(mk, "XActionPlayCardFromHand", timeout=1.2)
            played = got if got is not None else (
                (find_card(mid, card_id) is None)
                or ((mid.kredits.get("local") or 0) != (cur.kredits.get("local") or 0)))
            print("  deploy id=%s hand_x=%s drop=(%s,%s) -> %s | k %s->%s played=%s" % (
                card_id, x, dx, dy, ok, cur.kredits.get("local"),
                mid.kredits.get("local"), played))
            if played:
                # ★ 单位若还要选目标：现在点目标
                if c2.needs_hand_target or find_card(mid, card_id) is None and c2.needs_hand_target:
                    pass
                snap(h, "02_after_deploy_%s" % card_id)
                return True
            # ★ 没离手：如果是"部署后等选目标"，卡会还留在手里 —— 去点目标
            if c2.needs_hand_target:
                tid, tpos = (None, None)
                if target_id:
                    m, _ = screen_map(mid, read_field_fresh(h))
                    tpos = m.get(target_id)
                if tpos is None:
                    tid, tpos = pick_target(mid, read_field_fresh(h))
                if tpos:
                    sx, sy = win.client_to_screen(h, *tpos)
                    actions.click(sx, sy)
                    time.sleep(0.9)
                    done = src().snapshot()
                    ok2 = find_card(done, card_id) is None or \
                        (done.kredits.get("local") or 0) != (cur.kredits.get("local") or 0)
                    print("     needs-target: 点目标 %s @%s -> %s" % (tid or target_id, tpos, ok2))
                    if ok2:
                        snap(h, "02_after_deploy_target_%s" % card_id)
                        return True
    snap(h, "03_fail_deploy_%s" % card_id)
    for t in _notify_texts():
        print("  〔游戏提示〕%s" % t)
    return False


def read_field_fresh(h):
    f = win.capture_client_bgr(h, allow_screen_fallback=True)
    return B.read_field(f, debug=False) if (B and f is not None) else {}


def deploy_slots(occupied, y, lo=300, hi=985, pitch=143, min_gap=105, center=640):
    out = []
    for k in (0, -1, 1, -2, 2, -3, 3, -4, 4):
        x = center + k * pitch
        if not (lo <= x <= hi):
            continue
        if occupied and min(abs(x - o) for o in occupied) < min_gap:
            continue
        out.append((int(x), int(y)))
    return out


def act_move(card_id, x, y, settle=0.20, force=False):
    if _blocked_by_pick("移动"):
        return False
    h, f, st, field = read_all()
    card = find_card(st, card_id)
    if card is None:
        print("unit %s not on board" % card_id)
        return False
    if not force and not can_act_now(card, st.turn):
        print("unit %s 部署于回合 %s，当前回合 %s —— 不能移动（无闪击）" % (
            card_id, card.enter_play_on_turn, st.turn))
        return False
    if not force and is_pinned(card):
        print("unit %s 被**压制**（pin）：不能移动或攻击（下个回合结束时解除）"
              "（确信要发就传 force=True）" % card_id)
        return False
    # ★ 移动是**单向**的（支援阵线 → 前线）。已经在前线的单位再往支援阵线拖会被游戏拒绝
    #   （2026-09-21 用户更正：早先被误判成"movementLeft 不可靠"的就是这一次）。
    #   回撤只能靠「撤退」类效果（百科「撤退」条；SDK 只有 OnMoveToFrontline / OnMoveFromFrontline）。
    if not force and card.location == "frontline" and int(y) >= SUPPORT_ROW_MIN_Y:
        print("移动方向非法：%s 已在**前线**，不能移回支援阵线（移动单向：支援阵线 → 前线；回撤要用『撤退』类效果）"
              % card_id)
        return False
    m, warn = screen_map(st, field)
    for w in warn:
        print("  [map warn] " + w)
    if card_id not in m:
        print("unit %s 没有屏幕坐标（mapping 漏了：%s）" % (card_id, list(m)))
        return False
    sx0, sy0 = m[card_id]
    ok = actions.move_drag(*win.client_to_screen(h, sx0, sy0),
                           *win.client_to_screen(h, x, y), settle=settle)
    time.sleep(0.9)
    after = src().snapshot()
    c = find_card(after, card_id)
    moved = bool(c and (c.location != card.location or c.slot != card.slot))
    print("  move id=%s (%d,%d)->(%d,%d) -> %s | %s/%s -> %s/%s moved=%s%s" % (
        card_id, sx0, sy0, x, y, ok, card.location, card.slot,
        c.location if c else None, c.slot if c else None, moved,
        "  (坦克：移动后仍可攻击)" if c and "tank" == (c.card_type or "") else ""))
    return moved


def _enemy_snapshot(st):
    return {c.card_id: (c.attack, c.defense, c.location) for c in st.cards if c.side == "enemy"}


def _state_sig(st):
    """攻击有没有发生的判据不能用"敌方掉血"——有些攻击只触发效果
    （例：DINGO 是坦克 0/1，攻击敌方总部时给我方总部 +1 防御并抽 1 张牌）。
    所以比较整个盘面 + 双方总部 + 手牌/弃牌数量。"""
    cards = tuple(sorted((c.side, c.card_id, c.attack, c.defense, c.location or "")
                         for c in st.cards))
    hq = tuple((st.hq.get(s).defense if st.hq.get(s) else None) for s in ("local", "enemy"))
    return (cards, hq, len(st.hand("local")), len(st.hand("enemy")),
            len(st.discard("local")), len(st.discard("enemy")))


def act_attack(card_id, target, retry=True, force=False):
    """★ `force=True`：越过下面几道"进程外判据" —— §7.6f「判据只挑不判」，
    拒绝必须能强制越过，否则判据算错一次就把正确招法永久屏蔽掉。
    `_blocked_by_pick` 不算判据（是"选择界面开着时任何动作都不生效"这个
    机械事实，见 CLAUDE.md 环境坑），`force` 不越过它。
    """
    if _blocked_by_pick("攻击"):
        return False
    h, f, st, field = read_all()
    card = find_card(st, card_id)
    if card is None:
        print("attacker %s not found" % card_id)
        return False
    m, warn = screen_map(st, field)
    for w in warn:
        print("  [map warn] " + w)
    if card_id not in m:
        print("attacker %s 没有屏幕坐标（mapping 漏了：%s）" % (card_id, list(m)))
        return False
    ax, ay = m[card_id]
    tx = ty = None

    def _guards():
        """敌方场上带 guard 的单位（按 slot 排序）。

        ★ 百科原文：「**被守护**单位只能被轰炸机和炮兵攻击」；「与守护单位**相邻**的非守护单位
        无法被轰炸机和炮兵以外的单位攻击」。所以守护**不是**"全场挡总部"，
        而是让相邻目标变成"被守护"——**轰炸机/炮兵照样能打**（turn 30 就是 B-17 收尾的）。
        """
        out = []
        for c in st.cards:
            if c.side != "enemy" or c.location not in ("frontline", "back"):
                continue
            flags = (c.raw or {}).get("all_keyword_flags") or {}
            if "guard" in (c.keywords or []) or flags.get("has_guard"):
                out.append(c)
        return sorted(out, key=lambda c: (c.slot if c.slot is not None else 99))

    tcard = None          # 目标卡对象（拿到就做统一判据 + 防御侧提示）
    if target == "hq":
        # ★ 2026-09-24：guard 拦截的拒绝判据已经收进下面的统一 `CanAttack` 检查
        #   （`hq_is_being_garded`/`is_being_garded`），不用在这里单独拦一次了——
        #   但 `_guards()` 还留着，`guard<i>` 目标语法解析要用（下面那个分支）。
        hq = st.hq.get("enemy")
        tcard = hq
        if hq is not None and hq.card_id in m:
            tx, ty = m[hq.card_id]
        # 不再用 field["hq_enemy"] 兜底：那是模板匹配，曾把 guard 单位误判成 HQ。
        # 行模型下敌方总部只要在内存里就一定有坐标；读不到就该报错，不该猜。
    elif target.startswith("guard"):
        idx = int(target[5:] or 0)
        gs = _guards()
        if idx < len(gs) and gs[idx].card_id in m:
            tcard = gs[idx]
            tx, ty = m[gs[idx].card_id]
        elif gs:
            print("guard 索引 %d 越界（敌方 guard 有 %d 个）" % (idx, len(gs)))
            return False
        else:
            print("敌方现在没有 guard 单位 —— 可以直接打 hq")
            return False
    elif target == "front":
        # ★ 走内存 + 行模型。原实现取 OCR 的第一个框、并用 st.frontline_owner 把关 ——
        #   frontline_owner 读数不可信（§4.6），改成按 side 自算。
        fr = sorted([c for c in st.cards if c.side == "enemy" and c.location == "frontline"
                     and c.card_type not in ("order", "counter")],
                    key=lambda c: (c.slot if c.slot is not None else 99))
        idx = int(target[5:]) if target[5:].isdigit() else 0
        if not fr:
            print("敌方前线没有单位")
            return False
        if idx >= len(fr):
            print("front 索引 %d 越界（敌方前线有 %d 个）" % (idx, len(fr)))
            return False
        if fr[idx].card_id in m:
            tcard = fr[idx]
            tx, ty = m[fr[idx].card_id]
    elif target.startswith("back"):
        idx = int(target[4:] or 0)
        back = sorted([c for c in st.cards if c.side == "enemy" and c.location == "back"],
                      key=lambda c: (c.slot if c.slot is not None else 99))
        if idx >= len(back):
            print("back 索引 %d 越界（敌方后排有 %d 个）" % (idx, len(back)))
            return False
        if back[idx].card_id in m:
            tcard = back[idx]          # ★ 原来漏了：不赋值会让下面的防御侧判据整段被跳过
            tx, ty = m[back[idx].card_id]
    if tx is None:
        print("target %s 解析不出坐标（内存里没有这张，或它不在任何一行）" % target)
        return False

    # ★ 2026-09-24：`is_pinned`/`can_act_now`/guard/`target_blockers` 那四段手写拦截
    #   收进一条 `cardsCheckFunctions_C::CanAttack`（§11.3）。之前没动是在等一次
    #   正反例都对实机验证过——2026-09-24 同一天验证完了：`DINGO`(Blitz) 正例判
    #   `True` 两次真打成，`KINGFISHER`(无 Blitz) 负例判 `deployment_sickness`，
    #   不带 force 被拦住、带 force 真的越过并被游戏拒绝、拒绝提示词一字对应。
    #   手写四段散装判据容易漂移（各写各的），统一成一条函数后新增限制类型不用
    #   逐个补——`CanAttack` 自己扩到 18 个 failReason 全含，含用户点名的三类。
    #   `stopped`（算不出来）时不拦，按老规矩交给游戏判（§7.6f）。
    if tcard is not None and not force:
        try:
            from agent.legality import Legality
            r = Legality(_km()).can_attack(st, card, tcard)
        except Exception as e:                               # noqa: BLE001
            print("  [CanAttack 求值器异常，退回不拦截] %s" % e)
            r = {"ok": False}
        if r.get("ok") and r.get("can") is False:
            print("目标不可攻击：%s（确信要发就传 force=True）" % r.get("reason_zh"))
            return False
    # ★ "免疫"是独立信息（攻击仍然合法、只是这一下不造成伤害），`CanAttack` 的
    #   `can` 不覆盖这个语义，继续用 `target_blockers` 读——跟上面的合法性判据
    #   不是一回事，不能相互取代。
    if tcard is not None and (tcard.raw or {}).get("ptr"):
        try:
            from kardsmem import cards as _C
            tb = _C.target_blockers(_km(), tcard.raw["ptr"], attacker_type=card.card_type,
                                    attacker_ptr=(card.raw or {}).get("ptr"))
            if tb.get("damage_blocked"):
                print("⚠ 目标**免疫**：这一下不会造成伤害（%s）" % "；".join(tb.get("reasons") or []))
        except Exception as e:                               # noqa: BLE001
            print("  [defender check skipped] %s" % e)

    before = _state_sig(st)
    hq0 = st.hq["enemy"].defense if st.hq.get("enemy") else None
    mk = _mark()
    for attempt, (dx, dy) in enumerate([(0, 0), (0, -14), (0, 14)] if retry else [(0, 0)]):
        ok = actions.move_drag(*win.client_to_screen(h, ax, ay),
                               *win.client_to_screen(h, tx + dx, ty + dy), settle=0.15)
        time.sleep(1.0)
        after = src().snapshot()
        hq1 = after.hq["enemy"].defense if after.hq.get("enemy") else None
        # ★ 先问动作流（权威），问不到才退回"整盘签名变没变"这个旧判据。
        got, acts = _receipt(mk, "XActionAttackCard", timeout=1.2)
        changed = got if got is not None else (_state_sig(after) != before)
        print("  attack id=%s (%d,%d)->%s(%d,%d)%s -> %s | enemyHQ %s->%s changed=%s" % (
            card_id, ax, ay, target, tx + dx, ty + dy,
            "" if (dx, dy) == (0, 0) else "(retry)", ok, hq0, hq1, changed))
        if changed:
            return True
    for t in _notify_texts():
        print("  〔游戏提示〕%s" % t)
    return False



# ---------------------------------------------------------------- 投降（E9，§7.6e）
# 实机标定（2026-09-22）：齿轮 (1240, 30) → 菜单展开 → 投降 (1134, 57)。
# ★ **一键，没有二次确认** —— 点完直接结算。所以默认要显式 confirm=True 才真点。
GEAR_XY = (1240, 30)
SURRENDER_XY = (1134, 57)
SETTINGS_XY = (1134, 24)


def act_surrender(confirm=False):
    """投降认输。

    ★ 这是**不可逆**的动作，而且没有二次确认界面 ——
      所以这里自己做一道闸：`confirm=True` 才真的点下去。
      （规格 §7.6e：点完直接结算，没有"你确定吗"那一步。）
    """
    if not confirm:
        print("act_surrender 是不可逆的，而且游戏**没有**二次确认 —— "
              "确定要投降请传 confirm=True")
        return False
    h = hwnd()
    if h is None:
        print("找不到游戏窗口")
        return False
    mk = _mark()
    gx, gy = win.client_to_screen(h, *GEAR_XY)
    actions.click(gx, gy)
    time.sleep(0.45)                     # 菜单展开动画
    snap(h, "10_gear_menu")
    sx, sy = win.client_to_screen(h, *SURRENDER_XY)
    actions.click(sx, sy)
    time.sleep(1.2)
    snap(h, "11_after_surrender")
    st = src().snapshot()
    got, acts = _receipt(mk, None, timeout=2.0)
    done = st.match_finished or bool(got)
    print("surrender -> match_finished=%s 动作流新增=%s" % (st.match_finished, len(acts)))
    for t in _notify_texts():
        print("  〔游戏提示〕%s" % t)
    return bool(done)

# ---------------------------------------------------------------- 选择界面（抉择/预报）
# ★ 坐标固定；卡面内容一律由内存决定，不读图。
PICK_Y = 370
PICK_X = {2: [537, 741], 3: [420, 639, 858]}     # 2 张=抉择，3 张=预报/发现


def _board_addr(m, base):
    world = m.ptr(base + BA.RVA_GWORLD)
    lvl = m.ptr(world + 0x30)
    arr = m.ptr(lvl + 0xA0)
    n = m.i32(lvl + 0xA8)
    if not arr or not n or n > 20000:
        return 0
    for i in range(n):
        a = m.ptr(arr + i * 8)
        if not a:
            continue
        cls = m.ptr(a + 0x10)
        if cls and m.i32(cls + 0x58) in (0x0F68, 0x0F70):
            return a
    return 0


def pick_state():
    """游戏是不是在等我选牌：ABP_Board_C.chooseOneActive@0x0AC9。
    （点「结束回合」会让它默认选最左，所以必须先处理。）"""
    s = src()
    st = s.snapshot()
    m, base = s._m, s.base
    b = _board_addr(m, base)
    if not b:
        return None
    sel = m.u8(b + 0x0C21)      # isSelectingHandTarget
    act = m.u8(b + 0x0AC9)      # chooseOneActive
    return {"choose_active": bool(act), "selecting_hand_target": bool(sel), "board": b}


def pick_candidates(st):
    """候选牌：本地侧、id >= 4000 的"新生成"卡（预报/发现会以 4xxx/6xxx/13xxx 落进 discard）。
    名称来自 FText（board_api 已读），**不读图**。按 card_id 升序 ↔ 从左到右。"""
    cs = [c for c in st.cards
          if c.side == "local" and (c.card_id or 0) >= 4000
          and c.location in ("discard", "hand", "deck")]
    return sorted(cs, key=lambda c: c.card_id)


def act_pick(index, index2=None):
    """预报是**两级**选择：第一下选天气类型（蓝天/薄雾/狂风），第二下选该类型的 2K/4K/6K。
    用户澄清：**一次选择就是一下点击**，不许双击；点完要重新判断是不是进了下一级。
    index  = 一级下标；index2 = 二级下标（默认同 index）。

    ★ 2026-09-24 实机撞上一次真预报（OVERCAST 触发）才补上这条判据：`kardsmem.pick.
    choose_candidates()`（**任何三选一界面通用**，不止预报）直接读
    `ABP_ChooseCardToSpawn_C.indexOfCardInDeck`/`Name_0`/`SelectedCard`，不用再猜
    "候选变了没"——旧判据在这个真实场景里当场证实会误判（见下面被删掉的那段旧
    注释里记录的教训）：屏幕已经进二级了，旧的 `pick_candidates`（按 `card_id>=4000`
    捞）看不出候选换没换，只能报"不知道"。现在改成对比 `choose_candidates()` 的
    actor 集合有没有变——变了就是真进了下一级，没变就换探针位重试，四个探针位
    都不行才承认没点中。
    """
    idx2 = index if index2 is None else index2
    h = hwnd()
    from kardsmem import pick as PK
    for level in (0, 1):
        k = index if level == 0 else idx2
        st = src().snapshot()
        cands = pick_candidates(st)
        if cands:
            print("  候选(内存, id>=4000): %s" % " | ".join(
                "%s(id=%s)" % ((c.name or "?")[:14], c.card_id) for c in cands[:8]))
        xs = PICK_X[3]
        if k >= len(xs):
            print("index %d 超出候选位置 %s" % (k, xs))
            return False
        try:
            before_key = tuple(sorted(r["actor"] for r in PK.choose_candidates(_km())))
        except Exception:                                        # noqa: BLE001
            before_key = None
        for cx, cy in ((xs[k], PICK_Y), (xs[k], PICK_Y + 6), (xs[k] - 12, PICK_Y), (xs[k] + 12, PICK_Y)):
            sx, sy = win.client_to_screen(h, cx, cy)
            actions.click(sx, sy)          # ★ 单击一次
            time.sleep(1.1)
            ps = pick_state()
            if ps and not ps["choose_active"]:
                print("  pick 第%d级 #%d @client(%d,%d) -> 已选定" % (level + 1, k, cx, cy))
                return True
            if ps and ps["choose_active"]:
                if before_key is None:
                    print("    第%d级 #%d @client(%d,%d) -> 选择界面仍开着"
                          "（choose_candidates 读不出，退回旧判据：不知道点没点中）"
                          % (level + 1, k, cx, cy))
                    break
                after_key = tuple(sorted(r["actor"] for r in PK.choose_candidates(_km())))
                if after_key != before_key:
                    print("  pick 第%d级 #%d @client(%d,%d) -> 已进下一级（候选 actor 集合变了）"
                          % (level + 1, k, cx, cy))
                    break
                print("    第%d级 #%d @client(%d,%d) -> 没点中（候选 actor 集合没变），换探针位重试"
                      % (level + 1, k, cx, cy))
        else:
            print("  pick 第%d级 #%d 四个探针位都没点中" % (level + 1, k))
            return False
        # 进入下一级则继续外层循环
    print("  pick 结束但 chooseOneActive 仍为 1")
    return False


PICK_HAND_OK = (639, 564)      # 手牌目标选择的"确认"键 —— 与换牌的确认(638,667)**不是**同一个位置


def act_pick_hand(index):
    """满足"部署：选择 1 张手牌"这类选择（`isSelectingHandTarget@0xC21==1`）。

    这是与抉择/预报**不同的第三种**选择界面：候选就是自己的手牌，不在屏幕中央
    那一排，所以 `act_pick` 的 PICK_X 完全用不上。流程是**两下**：
      1) 点那张手牌（它会抬起来）—— 此时中部出现"确认"键
      2) 点"确认"
    只点第一下不会生效（实测：选中了但 chooseOneActive 仍为 1）。
    """
    ps = pick_state()
    if not (ps and ps.get("selecting_hand_target")):
        print("现在不是手牌目标选择（selecting_hand_target=%s）"
              % (ps or {}).get("selecting_hand_target"))
        return False
    h = hwnd()
    _h, f, st, _field = read_all()
    hand = sorted([c for c in st.cards if c.side == "local" and c.location == "hand"],
                  key=lambda c: (c.slot is None, c.slot))
    if not hand:
        print("手牌读不到")
        return False
    _probes, (n, xs, _left) = hand_x_list(st, hand[0], f)
    if index >= len(xs):
        print("index %d 超出手牌 %d 张" % (index, len(xs)))
        return False
    print("  选手牌 #%d %s @(%d,%d)" % (index, hand[index].name, xs[index], HAND_Y))
    actions.click(*win.client_to_screen(h, xs[index], HAND_Y))
    time.sleep(0.8)
    actions.click(*win.client_to_screen(h, *PICK_HAND_OK))
    time.sleep(1.2)
    ps = pick_state()
    ok = not (ps and ps.get("choose_active"))
    print("  -> %s" % ("已选定" if ok else "仍开着 %s" % ps))
    return ok


# ---------------------------------------------------------------- 换牌（mulligan）
# ★ 坐标是**摊平在屏幕中央的一排**，跟手牌扇形（`HAND_Y=690` 那一套 probes）是两回事——
#   换牌界面没有手牌那种叠放扇形，4/5 张牌横向等距摊开。2026-09-24 实机踩过坑：
#   `do_mulligan.py` 旧脚本把手牌扇形的 `HAND_Y=690` 直接拿来点换牌卡，
#   而"确认"按钮在 y=667，两者只差 23px，第二次点击被按钮吃掉、换牌提前误提交。
#   下面这组坐标是从当天实机截图（n=4/n=5 各一次）量出来的卡面中心点，
#   还没有像 `ROW_SPECS` 那样做最小二乘拟合，**误差以卡面宽度（~200px）打底**，
#   够用但不精确；`MULLIGAN_CONFIRM` 沿用旧脚本已验证过的 (638, 667)。
MULLIGAN_Y = 300
MULLIGAN_CENTER = 636
MULLIGAN_PITCH = 220
MULLIGAN_CONFIRM = (638, 667)


def mulligan_x(n, rank):
    """换牌界面第 `rank`（0 起）张牌（共 `n` 张）的屏幕 x —— 居中等距，公式同 `ROW_SPECS`。"""
    return int(round(MULLIGAN_CENTER + (rank - (n - 1) / 2) * MULLIGAN_PITCH))


def mulligan_marks():
    """当前换牌界面每张手牌的 `shouldDiscard` 标记（读侧，见 `kardsmem.pick`）。"""
    from kardsmem import pick as PK
    return PK.mulligan_marks(_km())


def in_mulligan():
    """还在换牌阶段吗？—— True / False / None（读不出，不能当"是"用）。

    ★ `pick_state()` 的三条判据（`chooseOneActive`/`isSelectingHandTarget`/
      `pc.ChooseOneSelection`）**不覆盖换牌**（CLAUDE.md「⚠ 换牌不触发这三条」），
      换牌界面自己也没找到一个持久的"正在换牌"布尔字段——`BP_Board_C.battleHUDHidden`、
      `BP_ConfirmCardsButton_C`/子控件的 `Visibility`、`MatchController_C.
      mulliganReplacementReceived/startingHandPresented` 都试过，2026-09-24
      拿两份"确认换牌前"的转储核对，全部**不discriminate**（换牌期间和换牌结束后
      读到的值一样，见 `KARDS-AUTOMATION.md` 相关记录）。

    真正管用的判据是**对局动作流**（§7.6g，本来就是"动作成没成"的权威来源）：
    `AllMatchActions` 里换牌阶段**只有** `XStartOfGame` 一条，回合 1 一开始
    第一条 `XActionStartOfTurn` 就会追加进去——这个转变只发生一次、只增不减，
    比猜哪个 UI 字段管用得多。同一天两份换牌期间的转储验证过只有
    `XStartOfGame`；当前一局正式对局（换牌已结束）验证过已经有
    `XActionStartOfTurn`。
    """
    m = _matchlog()
    if not m:
        return None
    acts = m.all()
    if not acts:
        return None
    return not any(a.get("action_type") == "XActionStartOfTurn" for a in acts)


def act_mulligan_toggle(slot):
    """点一下换牌界面第 `slot` 张手牌，切换它的"待替换"标记。

    动手前先用 `in_mulligan()` 核实还在换牌阶段——不确认这一步，点固定坐标
    没有意义：换牌结束后同一块屏幕位置可能已经是别的界面（2026-09-24 就
    因为换牌窗口已经关闭还照点，误触了后续界面）。核实完仍然**读内存确认
    点中了**（不是"点了就当作成功"——同一天也因坐标用错点空过）。
    """
    im = in_mulligan()
    if im is not True:
        print("拒绝：判定不在换牌阶段（in_mulligan=%s），不点——固定坐标"
              "只在换牌界面才有意义" % im)
        return None
    before = mulligan_marks()
    if slot >= len(before):
        print("slot %d 超出手牌 %d 张" % (slot, len(before)))
        return None
    was = before[slot]["marked"]
    n = len(before)
    h = hwnd()
    x = mulligan_x(n, slot)
    sx, sy = win.client_to_screen(h, x, MULLIGAN_Y)
    actions.click(sx, sy)
    time.sleep(0.35)
    after = mulligan_marks()
    now = after[slot]["marked"] if slot < len(after) else None
    ok = (now is not None) and (was is None or now != was)
    print("  换牌 slot=%d %s @client(%d,%d) marked %s -> %s%s"
          % (slot, before[slot].get("name"), x, MULLIGAN_Y, was, now,
             "" if ok else "  [警告：没有翻转，坐标可能没点中卡]"))
    return now


def act_mulligan_confirm():
    """点换牌界面的「确认」按钮，提交换牌。

    同样先拿 `in_mulligan()` 当闸门——不在换牌阶段就不点，避免点到别的界面。

    ★ 2026-09-24 实机修正：**成没成看 `in_mulligan()` 有没有从 True 翻到
    False，不看手牌 actor 集合变没变**。原来那版用"actor 集合有没有变"当
    回执，当场就撞见了假阴性——0 张标记时确认（不换任何牌），换牌确实成了
    （`in_mulligan()` True→False），但因为没有牌被替换，手牌 actor 集合
    压根不会变，旧判据报了 `changed=False`，跟真实结果（成功）矛盾。
    `in_mulligan()` 走的是 `AllMatchActions` 里第一条 `XActionStartOfTurn`
    出现没有（§7.6g 同一条权威信号），不管这次换没换牌都准。手牌 actor 集合
    还留着打印，纯作参考。"""
    im = in_mulligan()
    if im is not True:
        print("拒绝：判定不在换牌阶段（in_mulligan=%s），不点确认" % im)
        return None
    from kardsmem import pick as PK
    before = mulligan_marks()
    before_ptrs = {r["actor"] for r in before}
    h = hwnd()
    sx, sy = win.client_to_screen(h, *MULLIGAN_CONFIRM)
    actions.click(sx, sy)
    time.sleep(1.0)
    after = PK.hand_card_actors(_km())
    after_ptrs = {r["actor"] for r in after}
    changed = before_ptrs != after_ptrs
    done = in_mulligan() is False
    print("  确认换牌 @client%s -> in_mulligan 翻到 False：%s（手牌 actor 集合%s变化，仅供参考，"
          "before=%d after=%d）" % (MULLIGAN_CONFIRM, done, "有" if changed else "没有",
                                  len(before_ptrs), len(after_ptrs)))
    return done


def preflight(card_id=None, target=None, action=None):
    """动手前的自检（§7.7），汇总成一条命令，不动鼠标。

    ★ 2026-09-24：§7.7 原始 5 步清单里第 3/4 步（guard/pinned）已经被
    2026-09-24 那次重构吸收进 `act_attack`/`agent.legality.can_attack`
    本身（判据统一成一条 `CanAttack`，不用再在这里单独查一遍）；第 5 步
    （回读内存判成败）是 `_mark()`/`_receipt()` 已经在做的事，也不用这里
    重复。这个函数**剩下真正有增量价值的**只有第 1 步（选择界面有没有
    开着——开着的话任何动作都会被吃掉，这个检查在具体动作函数里各自
    埋了一份 `_blocked_by_pick`，这里给个不动鼠标就能看一眼的汇总入口）
    和"给一个具体 card_id/target 时顺手把对应判据结果也打出来"这个便利。

    参数都可选：不给 `card_id` 只报选择界面状态；给了 `card_id`（+可选
    `target`/`action`）额外跑一次 `can_attack`/`can_move`，仅供参考，
    **不拦任何东西**——真正的拦截逻辑仍然在 `act_attack`/`act_move`
    自己身上，这里只是"提前看一眼，少白跑"，不是又一道判据。
    """
    ps = pick_state()
    out = {"pick_state": ps}
    blocked = bool(ps and (ps.get("choose_active") or ps.get("selecting_hand_target")))
    out["blocked_by_pick"] = blocked
    print("选择界面：%s" % ("开着——任何出牌/移动/攻击都会被吃掉，先 `ops.py pick`/`pickhand`"
                          if blocked else "没开，可以动手"))
    if card_id is None:
        return out
    st = src().snapshot()
    card = find_card(st, card_id)
    if card is None:
        print("card %s 不在我方手牌/场上" % card_id)
        out["card_found"] = False
        return out
    out["card_found"] = True
    from agent.legality import Legality
    leg = Legality(_km())
    if action == "move" or (action is None and target is None):
        r = leg.can_move(st, card)
        out["can_move"] = r
        print("can_move(%s) = %s（%s）" % (card.name, r.get("can"), r.get("reason_zh") or r.get("stopped")))
    if target is not None:
        if isinstance(target, BA.Card):
            tcard = target
        elif target == "hq":
            tcard = st.hq.get("enemy")
        else:
            tcard = find_card(st, target)
            if tcard is None:
                for c in st.cards:
                    if c.card_id == target or str(c.card_id) == str(target):
                        tcard = c
                        break
        if tcard is not None:
            r = leg.can_attack(st, card, tcard)
            out["can_attack"] = r
            print("can_attack(%s -> %s) = %s（%s）"
                  % (card.name, tcard.name, r.get("can"), r.get("reason_zh") or r.get("stopped")))
        else:
            print("target %r 解析不出卡对象，跳过 can_attack" % target)
    return out


def act_end():
    ps = pick_state()
    if ps and ps["choose_active"]:
        how = ("`ops.py pickhand <index>`（手牌目标，要点牌+确认两下）"
               if ps.get("selecting_hand_target") else "`ops.py pick <index>`")
        print("★ 拒绝结束回合：选择界面还开着（chooseOneActive=1）——"
              "点结束回合会让游戏默认选最左。先用 %s。" % how)
        return False
    h = hwnd()
    sx, sy = win.client_to_screen(h, 1185, 471)
    print("end turn ->", actions.click(sx, sy))
    return True


def wait_our_turn(limit=300):
    t0 = time.time()
    last = None
    while time.time() - t0 < limit:
        st = src().snapshot()
        key = (st.turn, st.our_turn, st.kredits.get("local"), st.match_finished)
        if key != last:
            print("  t=%.0fs turn=%s our=%s k=%s fin=%s" % (
                time.time() - t0, st.turn, st.our_turn, st.kredits.get("local"), st.match_finished))
            last = key
        if st.match_finished:
            print("MATCH FINISHED")
            return False
        if st.our_turn:
            return True
        time.sleep(1.5)
    return False


def find_card(st, card_id):
    for c in st.cards:
        if c.card_id == card_id and c.side == "local":
            return c
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "state":
        h, f, st, field = read_all()
        m, warn = screen_map(st, field)
        print("turn=%s our=%s k=%s slots=%s/%s front=%s fin=%s" % (
            st.turn, st.our_turn, st.kredits.get("local"),
            st.slots.get("local"), st.slots.get("enemy"), st.frontline_owner, st.match_finished))
        for w in warn:
            print("  [map warn] " + w)
        for k, v in sorted(m.items()):
            c = find_card(st, k)
            if c is None:
                c = next((x for x in st.cards if x.card_id == k), None)
            act = can_act_now(c, st.turn) if c else None
            print("  id=%-6s %-22s screen=%s ept=%-3s can_act_turn=%s" % (
                k, (c.name or "?")[:22] if c else "?", v,
                getattr(c, "enter_play_on_turn", None), act))
    elif cmd == "rows":
        # 行模型 vs OCR 的逐行对照（实机标定/排错用，不动鼠标）
        h, f, st, field = read_all()
        d, warn = screen_map_detailed(st, field)
        for w in warn:
            print("  [warn] " + w)
        last = None
        for cid, v in sorted(d.items(), key=lambda t: (t[1]["row"], t[1]["rank"])):
            if v["row"] != last:
                sp = ROW_SPECS[v["row"]]
                print("-- %s  y=%d center=%d pitch=%d  n=%d"
                      % (v["row"], sp["y"], sp["center"], sp["pitch"], v["n"]))
                last = v["row"]
            c = next((x for x in st.cards if x.card_id == cid), None)
            dx = "" if v["x_ocr"] is None else "  d=%+d" % (v["x_ocr"] - v["x_model"])
            print("   id=%-4s %-22s slot=%-3s rank=%d  model=%-4d ocr=%-4s -> x=%-4d [%s]%s"
                  % (cid, ((c.name or "?")[:22] if c else "?"), v["slot"], v["rank"],
                     v["x_model"], v["x_ocr"], v["x"], v["source"], dx))
        if not d:
            print("盘面上没有卡（不在对局中？）")
    elif cmd == "play":
        act_deploy(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif cmd == "front":
        st = src().snapshot()
        x = int(sys.argv[3]) if len(sys.argv) > 3 else 640
        act_move(int(sys.argv[2]), x, FRONT_Y)
    elif cmd == "attack":
        act_attack(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "hq")
    elif cmd == "move":
        act_move(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    elif cmd == "surrender":
        act_surrender(confirm="--yes" in sys.argv)
    elif cmd == "end":
        act_end()
    elif cmd == "pick":
        act_pick(int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                 int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif cmd == "pickhand":
        act_pick_hand(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    elif cmd == "pending":
        print(pick_state())
    elif cmd == "next":
        wait_our_turn()


if __name__ == "__main__":
    main()
