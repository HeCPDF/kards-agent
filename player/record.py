#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""player.record —— 只读录制器（KARDS-NN.md §1.2/§1.3 的 P0 实现）。

★ 红线：只读。全靠 `AgentSession`（快照/动作流/提示）+ 只读的
  `ops_inject.pick_pending`/`mulligan_marks`/`kardsmem.pick`，不动鼠标。
  （2026-09-27：原来走 `ops.py` 的 `pick_state`/`mulligan_marks` —— 旧鼠标实现已归档，
  只读查询一律走 `agent.precheck.call_read(...)` → `ops_inject`。）

用法：

    python -m player.record --seconds 1800 --game 2026-09-24-demo

跑起来后你正常打（人机局也行、真人局也行），它在后台按 §1.2 的时序
把决策点一条条落成 JSONL，写到仓库外的 `D:\\Kards\\kards-data\\recordings\\`
（原因见 §1.2：子树发布纪律 + 体积）。

★ 这是第一版，覆盖到"跑得起来、字段基本对"，**不是**全部决策点都已经
  接上高置信度的标签解析：
    - `main`（出牌/攻击/移动/结束）：出牌/攻击/hand_target 走动作流具名字段
      解析，`label_src="matchlog"`；移动暂时解不出字段，`label_available=False`
      但 `raw` 照记（§11.1 G3，纯解析活，留给下一步）。
    - `pick`/`forecast`（二/三选一）：动作流其实有专属类型
      `XActionCardToDrawSelected`（develop 类三选一和预报**共用同一个**，
      2026-09-25 顺着 `BP_CardFunctions::Forecast()` 追字节码坐实——两者在
      `BP_PlayerMoves::ResolvePlayerMoveQueue()` 里走的是同一个 case，见
      `KARDS-AUTOMATION.md` §7.5、`KARDS-NN.md` §1.5：字段号也已经从伪 C++
      读出来了（`0=cardTriggeringDraw 1=cardToDraw 2=cardNameToSpawn
      3=isEffect`）。**但这整条链目前只有字节码证据，从没有对着一次真实
      `AllMatchActions` 条目核对过**——这里仍然只信更硬的信号：对比
      `choose_candidates()` 里 `SelectedCard` 翻转前后的 actor 集合，
      `label_src="pick_actor"`。下次真撞上三选一/预报，应该把
      `matchlog.since()` 里这条的原始 `data` 也顺手记进 `raw`（不要拿它
      当 label，只作为跟 actor 判据交叉核对的旁证），借真实样本把这条
      "未实机"的尾巴解决掉，而不是继续凭字节码假设。
    - `mulligan`：`XActionMulligan` 大概率不进 `AllMatchActions`（同上，受
      服务端闸门控制，未验），改用 `starting_cards()`（直接的 `Logic` 字段）
      跟换牌结束那一刻的 `mulligan_marks()` 比对——这本质上是**行为推断**
      （拿"现在手牌跟起手比少了哪张"猜结论），不是读到了一条换牌动作，
      `label_src="inferred"`（2026-09-25 改对：原先这里错标成
      `"matchlog"`，见 `_poll_mulligan` 那条同一天的修正）。
    - `deploy_target`（部署后点场上目标）：**没有独立的等待态判据**
      （不是 `isSelectingHandTarget`，那是"选手牌"专属——本轮会话已确认，
      见规格），第一版先不单独识别这个 phase，它会被吸收进触发它的那条
      `XActionPlayCardFromHand` 样本里（`raw` 里能看到 targetCardID 字段号
      还没解出来，同样是 G3 的坑）。
    - `deck_pick`：未标定（§11.2「今天做不了」），不实现。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time
from collections import deque
from typing import Optional


from agent import session as _session_mod
from agent import view as _view

from base import paths as _paths_rec
DEFAULT_OUT_DIR = _paths_rec.RECORD_DIR
POLL_INTERVAL = 0.5          # 2 Hz（§1.2 实测耗时够用）
# ★ 2026-09-28 修一个**时序 bug**（离线核对 live1/live2 录制数据坐实）：
#   原实现是"先把本轮快照 append 进 ring，再 _poll_matchlog()"，于是动作行
#   第一次出现的那一轮里，"最近一次静默快照"就是**本轮自己的**快照 —— 而动作
#   大多在两次轮询之间已经播完，那份快照是**动作之后**的。证据：live1/live2 里
#   我方 play 的主体 14 次落在弃牌堆、9 次落在场上、只有 1 次还在手牌（= 应该
#   在的地方）；110 条样本里 39 条 state_after == state_before。
#   现在改为**先处理动作流、后 append** ⇒ state_before 严格来自本轮之前的轮询。
#   ⚠ 这条修复只做了离线推理 + 数据核对，**还没有实机复验**；复验看两件事：
#     ① 新录的 play 样本主体大多还在手牌（location=hand）；
#     ② 新样本的 state_after != state_before 比例明显上升。
RING_SIZE = 30               # 2 Hz × 30 ≈ 15 s，够盖住长动画（原 8 ≈ 4 s）

# 我方侧（数字键）动作 -> (subject 字段号, target 字段号)。
# 只列出**已核实**的；没核实的宁可 label_available=False 也不要编。
MINE_LABEL_FIELDS = {
    "XActionPlayCardFromHand": ("0", None),          # cardID；目标字段号未解（G3）
    "XActionAttackCard": ("0", "1"),                 # attackerCardID / defenderCardID
    "XActionMoveCardToLine": (None, None),           # 未解（G3）
    "XActionEndOfTurn": (None, None),
    "XActionStartOfTurn": (None, None),
    # ★ 本轮会话实机验证过（175th INFANTRY REGIMENT 触发，选中 WE CAN DO IT!）：
    #   0 = 发起卡 CardID（instigator）  1 = 被选中的手牌 CardID
    "XActionHandTargetSelected": ("0", "1"),
}

# 对手侧（具名字段）同一件事的字段名映射。
ENEMY_LABEL_FIELDS = {
    "XActionPlayCardFromHand": ("cardID", None),
    "XActionAttackCard": ("attackerCardID", "defenderCardID"),
    "XActionMoveCardToLine": (None, None),
    "XActionEndOfTurn": (None, None),
    "XActionStartOfTurn": (None, None),
    "XActionHandTargetSelected": ("cardSelectingTarget", "targetCard"),
}

ACTION_TO_TYPE = {
    "XActionPlayCardFromHand": "play",
    "XActionAttackCard": "attack",
    "XActionMoveCardToLine": "move",
    "XActionEndOfTurn": "end",
    "XActionHandTargetSelected": "hand_target_selected",
}


def _kv(row: dict) -> dict:
    out = {}
    for v in row.get("data") or []:
        n = v.get("name")
        if n is None:
            continue
        out[n] = v.get("text") if v.get("text") is not None else v.get("value")
    return out


def _decode_label(row: dict, mine: bool):
    """从一条 matchlog 条目里抠 (type, subject, target)。抠不出来就 (type, None, None)。"""
    typ = row.get("action_type")
    fields = (MINE_LABEL_FIELDS if mine else ENEMY_LABEL_FIELDS).get(typ)
    kv = _kv(row)
    subject = target = None
    if fields:
        sf, tf = fields
        if sf is not None:
            subject = kv.get(sf)
        if tf is not None:
            target = kv.get(tf)
    try:
        subject = int(subject) if subject is not None else None
    except (TypeError, ValueError):
        pass
    try:
        target = int(target) if target is not None else None
    except (TypeError, ValueError):
        pass
    return ACTION_TO_TYPE.get(typ, typ), subject, target


class Recorder:
    def __init__(self, out_dir: str = DEFAULT_OUT_DIR, game: Optional[str] = None,
                 patch: Optional[str] = None, sess=None, tag: Optional[str] = None):
        # ★ 2026-09-28：可以**共用一个已经建好的 AgentSession**。原因：
        #   短命进程退出时 frida agent 卸载会把游戏带崩（`frida-agent.dll_unloaded`
        #   0xc0000005，实机三次），所以在线回路 + 录制必须跑在**同一个进程、同一个
        #   会话**里（`tick()` 由回路每步驱动），不要再起第二个注入进程。
        self.sess = sess if sess is not None else _session_mod.AgentSession(
            translate=False, warm=True)
        self.out_dir = out_dir
        # ★ `tag` = 自动分盘：一个录制进程连续录多局，**每局换一个文件**
        #   （<tag>-1、<tag>-2 …），这样既不用重启进程（重启会卸 agent → 崩游戏），
        #   又不会把 5 局混进同一个 game id。
        self._tag = tag
        self._matches_seen = 0
        self._in_match = False
        self.game = ("%s-1" % tag) if tag else (
            game or time.strftime("%Y-%m-%dT%H-%M-%S"))
        self.patch = patch
        os.makedirs(out_dir, exist_ok=True)
        self._fp = None
        self._open()
        # ⚠ 中途被强杀（没走到 close()）会留下一截没写 gzip 尾部（CRC/长度）的文件，
        #   `gzip.open(..., "rt")` 读到那一行会抛 `EOFError`——**不是数据坏了**，
        #   已经写完 flush 过的那些行原样在里面，读的时候 `try/except EOFError`
        #   把之前读到的行留着就行（2026-09-24 实机撞过一次，54 条全须全尾）。
        # 每局一份：见 `_rotate()`
        self._ring = deque(maxlen=RING_SIZE)     # [(t, snapshot, silent)]
        self._pending = deque()                  # 等 state_after 的样本
        self._log_cursor = 0
        self._deck_roster = None                 # 只取一次（§2.2）
        self._mulligan_sample_written = False
        self._pick_pending_rounds = []            # 正在追踪、还没见到翻转的三选一轮次
        self._pick_seen_actors = set()            # 已经分过轮的候选 actor 指针（防重复开新轮）
        self._pick_matchlog_echo = deque(maxlen=8)  # XActionCardToDrawSelected 原始行，只作旁证不当 label（见下方 _poll_pick 说明）
        self._n_written = 0
        self._n_before_fallback = 0
        self._closed = False

    def _open(self):
        self._fp = gzip.open(os.path.join(self.out_dir, self.game + ".jsonl.gz"), "at",
                              encoding="utf-8")

    def _rotate(self):
        """换一局：关掉旧的、开新的（同一进程、同一 attach）。"""
        print("  [分盘] %s 收尾：写了 %d 条样本" % (self.game, self._n_written),
              flush=True)
        try:
            self._fp.close()
        except Exception:                                        # noqa: BLE001
            pass
        n = self._matches_seen
        self.game = "%s-%d" % (self._tag, n)
        # 每局重置：这些状态都是"这一局"的
        self._ring.clear()
        self._pending.clear()
        self._log_cursor = 0
        self._deck_roster = None
        self._mulligan_sample_written = False
        self._pick_pending_rounds = []
        self._pick_seen_actors = set()
        self._pick_matchlog_echo.clear()
        self._n_written = 0
        self._n_before_fallback = 0
        self._open()
        print("  [分盘] 新对局 -> %s.jsonl.gz" % self.game, flush=True)

    # ------------------------------------------------------------ 单步驱动
    def tick(self, st=None):
        """一次轮询（环形缓冲 → 动作流 → 三选一 → 换牌）。

        `st` 由调用方给（回路已经拍过快照）⇒ **省一次注入调用**，也保证录制看到的
        盘面跟回路决策用的是**同一份**。不给就自己拍。
        """
        if self._closed:
            return st
        st = self._snap() if st is None else st
        # 盘面从"空"变"非空" ⇒ 一局新的开始了（牌组页/结算页是空的）
        in_match = bool(getattr(st, "cards", None))
        if in_match and not self._in_match:
            self._matches_seen += 1
            if self._matches_seen > 1 and self._tag:
                self._rotate()
        self._in_match = in_match
        self._refresh_log()
        self._ensure_deck_roster(st)
        silent = self._silent()
        self._poll_matchlog()
        if silent:
            self._finalize_pending(st, True)
        self._ring.append((self._t(), st, bool(silent)))
        self._poll_pick(st)
        self._poll_mulligan(st)
        return st

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._fp.close()
        except Exception:                                        # noqa: BLE001
            pass
        print("录制结束：写了 %d 条样本 -> %s" % (self._n_written,
              os.path.join(self.out_dir, self.game + ".jsonl.gz")))
        if self._n_before_fallback:
            print("  ⚠ 其中 %d 条的 state_before 只能拿当前快照兜底（是动作之后的），"
                  "这些样本在 nn 侧会被标 state_before_src=fallback_post"
                  % self._n_before_fallback)

    # ------------------------------------------------------------ 基础
    def _t(self) -> float:
        return time.time()

    def _snap(self):
        return self.sess.snapshot()

    def _refresh_log(self):
        """动作流对象**换局就会换**，而 `MatchLog.locate()` 找到后是永久缓存
        ⇒ 每 tick 先用廉价判据 `is_stale()` 看一眼，不新鲜就 forget + 重新定位。

        ★ 2026-09-29 实机：不做这件事的后果是"4 局只录到 1 局" —— 第 2 局起
          一直读旧 `BP_OnlineMatch_C` 的数组，增量游标永不前进。
        """
        log = self.sess.log
        if log is None:
            return
        try:
            if log.is_stale():
                log.forget()
                log.locate()
        except Exception:                                        # noqa: BLE001
            pass

    def _silent(self) -> Optional[bool]:
        log = self.sess.log
        if log is None or not log.locate():
            return None
        try:
            return log.queue() == []
        except Exception:                        # noqa: BLE001
            return None

    def _write(self, sample: dict):
        self._fp.write(json.dumps(sample, ensure_ascii=False, default=str))
        self._fp.write("\n")
        self._fp.flush()
        self._n_written += 1

    def _my_player_id(self, rows) -> Optional[int]:
        try:
            mine = self.sess.log.mine()
            if mine:
                return mine[-1].get("player_id")
        except Exception:                        # noqa: BLE001
            pass
        for r in reversed(rows or []):
            if r.get("action_id") == -1:
                return r.get("player_id")
        return None

    def _ensure_deck_roster(self, st):
        """§2.2：己方卡组名单只取一次——手牌+牌库+场上+弃牌的并集就是完整卡组
        （换牌/抽牌只是在这几个区之间挪动，不改变总数），随时取都行。

        ★ 2026-09-24 曾经在这里挡"换牌早期 `st.our_side` 不可靠"（§7.6d）——
        现在 `board_api.read_my_side()` 换牌期间读不出时会兜底走 kardsmem
        `Logic.mySide`（见 `kardsmem/board.py` 同日的改动），这个坑已经堵上了，
        不用再等 `in_mulligan() is False` 才敢取。
        """
        if self._deck_roster is not None:
            return
        try:
            from kardsmem.names import FNamePool
            pool = FNamePool(self.sess._kardsmem().m, self.sess._kardsmem().base)
            names = []
            for c in st.cards:
                if c.side != _session_mod.LOCAL:
                    continue
                if c.location not in ("hand", "deck", "back", "frontline", "discard"):
                    continue
                ptr = (c.raw or {}).get("ptr")
                if not ptr:
                    continue
                fn = pool.card_asset_name(ptr)
                if fn:
                    names.append(fn)
            if names:
                self._deck_roster = {"local": names}
        except Exception as e:                   # noqa: BLE001
            print("  [deck_roster 取不到，跳过] %s" % e)

    # ------------------------------------------------------------ 主/对手动作（走动作流）
    def _poll_matchlog(self):
        log = self.sess.log
        if log is None or not log.locate():
            return
        n = log.count()
        if n < self._log_cursor:
            # 动作流被新对局重置了（数组变短）⇒ 新一局从头读
            self._log_cursor = 0
        if n <= self._log_cursor:
            return
        rows = log.since(self._log_cursor)
        self._log_cursor = n
        me = self._my_player_id(log.all())
        for r in rows:
            typ = r.get("action_type")
            if typ == "XActionCardToDrawSelected":
                # ★ 2026-09-25：不建成独立样本（会跟 _poll_pick 的 actor 判据
                #   重复计数），只留原始行当旁证——攒够一次真实数据就能核对
                #   KARDS-NN.md §1.5 那套字节码字段号（0=cardTriggeringDraw
                #   1=cardToDraw 2=cardNameToSpawn 3=isEffect）到底对不对。
                self._pick_matchlog_echo.append({"row": r, "t_seen": self._t()})
                continue
            if typ not in ACTION_TO_TYPE:
                continue      # StartOfTurn 之类不建样本，只是时序标记
            mine = r.get("action_id") == -1
            seat = "local" if mine else "enemy"
            action_type, subject, target = _decode_label(r, mine)
            # state_before：环形缓冲里最近一份"静默"快照
            state_before = None
            before_src = "fallback_post"
            for t, snap, silent in reversed(self._ring):
                if silent:
                    state_before = snap
                    before_src = "ring"
                    break
            self._pending.append({
                "kind": "matchlog", "row": r, "seat": seat,
                "action_type": action_type, "subject": subject, "target": target,
                "state_before": state_before, "before_src": before_src,
                "t_seen": self._t(),
            })

    def _finalize_pending(self, cur_snap, silent: bool):
        if not self._pending or not silent:
            return
        item = self._pending.popleft()
        st_before = item["state_before"] or cur_snap
        if item["state_before"] is None:
            # ring 里一个静默快照都没有（动画比 ring 还长 / 刚开局）⇒ 只能用
            # 当前快照兜底，这条样本的 state_before 是**动作之后**的，如实标记。
            self._n_before_fallback += 1
            item["before_src"] = "fallback_post"
        label_available = item["subject"] is not None or item["action_type"] == "end"
        phase = "hand_target" if item["action_type"] == "hand_target_selected" else "main"
        sample = _schema().make_sample(
            game=self.game, patch=self.patch, seat=item["seat"],
            turn=getattr(st_before, "turn", None), t=item["t_seen"],
            phase=phase,
            state=_schema().project_state(st_before, _session_mod.LOCAL, self._deck_roster),
            candidates={"subject": [], "target": [], "option": []},
            verdict={},
            label={"type": item["action_type"], "subject": item["subject"],
                   "target": item["target"], "option": None},
            label_src="matchlog", label_available=label_available,
            raw=item["row"].get("data"),
            receipt={"accepted": True, "action_id": item["row"].get("action_id"),
                     "state_before_src": item.get("before_src")},
            events=[dict(e, text_zh=None) for e in self.sess.events()],
            state_after_hash=_schema().state_hash(
                _schema().project_state(cur_snap, _session_mod.LOCAL, self._deck_roster)),
        )
        self._write(sample)

    # ------------------------------------------------------------ 三选一（走 choose_candidates）
    #
    # ★ 用户 2026-09-24 定调 + 反编译核实：**"预报"不是独立机制，是"三选一"
    #   这个共用模板（`ABP_ChooseCardToSpawn_C`/`GetChooseSpawnCards`）的一个
    #   实例**，不该焊死"两级"假设，也不该单独搞 "forecast" phase——统一按
    #   "pick" 处理，一次调用 = 一条样本。
    #
    # ★ 但两条**不同确认机制**的卡不能用同一套判据，查过反编译才分清楚：
    #   - **`ability.forecast`**（如 OVERCAST）：候选是**现造的新卡**（天气变体），
    #     蓝图**没有实现** `OnHandTargetSelected`——没有"已存在的卡" CardID
    #     可引用，只能靠本节这套 `choose_candidates()` 的 actor 指针追踪。
    #   - **`ability.develop`**（如"直布罗陀巨岩"，反编译核实：
    #     `Text = "Develop a card in the enemy deck, then in your deck."`，
    #     `GetChooseSpawnCards` 靠一个自带的 JSON 标记 `oneChosen` 分两段——
    #     第一段扫**对方**牌库、第二段扫**己方**牌库，`trigger_card_id`
    #     两段完全一样）：候选是**牌库里已存在的真卡**，确认走
    #     `OnHandTargetSelected(handTargetCardID, instigatorID)`——**跟"选
    #     手牌"（175th步兵团/美国人卫队）是同一个回调**，会各自产生一条
    #     独立的 `XActionHandTargetSelected` 动作流条目，被 `_poll_matchlog`
    #     按顺序自然正确捕获，**不需要**、也**不应该**指望本节这套
    #     actor 指针分轮逻辑去处理——动作流是顺序流水账，天然不会把两段
    #     揉在一起，不存在"同一个发起卡"这个坑。
    #   ⚠ **未消除的风险**：develop 卡的候选大概率也走
    #   `ABP_ChooseCardToSpawn_C`（同一个通用 UI 类），意味着 `_poll_pick`
    #   理论上可能对同一次 develop 选择**也**触发一条（更不精确的）样本，
    #   跟 `_poll_matchlog` 那条重复——没做去重，先接受这个风险，真在录制
    #   数据里看到重复再处理（比如用 `ability.develop`/`forecast` 的
    #   GameplayTag 提前分流，`cardnatives.getHasGameplayTag` 已经有）。
    #
    # ★ 实机撞过一次假阳性（美澳天气局，OVERCAST）：一次轮询拿到过 **6** 个
    #   actor 而不是 3——上一轮选完，下一轮的 actor 已经建出来了，旧的还没
    #   被 GC 收掉，两轮**同时存在**于 `choose_candidates()` 的返回里，之前
    #   按 `trigger_card_id` 分组解决不了"同一张卡连续调用多次"的情况
    #   （develop 卡就是这样，虽然那条现在走 matchlog 不受影响，但同名
    #   陷阱本身是真的）。
    #
    #   现在的判据：**actor 指针的"新旧"**。一轮三选一的 2~3 个候选 actor
    #   是同一时刻一起建出来的，第一次在 `choose_candidates()` 里见到它们
    #   （指针之前从没出现过）就当作开了新的一轮；之后只在**这一轮自己的
    #   指针集合**内部找"selected 从 False 翻 True"，不理会同时存在的其它轮。
    #   ⚠ 这条也没有单独实机验证过（没能再触发一次多级预报去核对）；
    #   UE 复用被 GC 掉的槽位这件事本身是真的（CLAUDE.md 弯路记录过），
    #   理论上存在"旧指针恰好被复用、被误判成同一轮"的小概率窗口，先接受
    #   这个风险，真出问题再收紧（比如叠加 index/name 一致性校验）。
    def _poll_pick(self, st):
        from agent import precheck
        ps = precheck.call_read("pick_pending") or {}
        if not (ps.get("choose_one_active") or ps.get("pending")):
            if self._pick_pending_rounds:
                print("  [pick 缺漏] chooseOneActive 已关闭，还有 %d 轮候选没见到翻转就消失了"
                      % len(self._pick_pending_rounds))
            self._pick_pending_rounds = []
            return
        try:
            from kardsmem import pick as PK
            cur = PK.choose_candidates(self.sess._kardsmem())
        except Exception:                        # noqa: BLE001
            return
        if not cur:
            return
        cur_by_ptr = {r["actor"]: r for r in cur}

        # 1) 已知的轮次：只在各自的指针集合内部找翻转
        still_pending = []
        for rnd in self._pick_pending_rounds:
            present = {p: cur_by_ptr[p] for p in rnd["actors"] if p in cur_by_ptr}
            flipped = [r for p, r in present.items()
                       if r["selected"] and not rnd["rows"][p]["selected"]]
            if flipped:
                chosen = flipped[0]
                prev_rows = list(rnd["rows"].values())
                # ★ 把这段时间内收到的 XActionCardToDrawSelected 原始行一并存进
                #   raw——不当 label（label 仍然是 actor 判据给的），纯粹留给
                #   事后核对："matchlog_echo" 里那条的字段跟这里的 chosen 是否
                #   对得上，能不能验证 KARDS-NN.md §1.5 猜的字段号。
                echo = [e["row"] for e in self._pick_matchlog_echo]
                self._pick_matchlog_echo.clear()
                sample = _schema().make_sample(
                    game=self.game, patch=self.patch, seat="local",
                    turn=st.turn, t=self._t(), phase="pick",
                    state=_schema().project_state(st, _session_mod.LOCAL, self._deck_roster),
                    candidates={"subject": [], "target": [],
                                "option": [r["name"] for r in prev_rows]},
                    verdict={},
                    label={"type": "pick", "subject": chosen.get("trigger_card_id"),
                           "target": None, "option": chosen.get("name")},
                    label_src="pick_actor", label_available=True,
                    raw={"candidates": [{"name": r["name"], "index": r["index"],
                                          "selected": r["selected"]} for r in prev_rows],
                         "matchlog_echo": echo},
                    receipt={"accepted": True, "action_id": None},
                    events=[dict(e, text_zh=None) for e in self.sess.events()],
                )
                self._write(sample)
                continue          # 这轮结了，不放回 still_pending
            if not present:
                print("  [pick 缺漏] 一轮候选（%s）消失前没见到翻转，标签丢了"
                      % [r["name"] for r in rnd["rows"].values()])
                continue
            rnd["rows"] = present
            still_pending.append(rnd)
        self._pick_pending_rounds = still_pending

        # 2) 没见过的指针 ⇒ 新的一轮（同一时刻一起冒出来的算一组）
        pending_ptrs = {p for rnd in self._pick_pending_rounds for p in rnd["actors"]}
        new_ptrs = [p for p in cur_by_ptr
                    if p not in self._pick_seen_actors and p not in pending_ptrs]
        if new_ptrs:
            self._pick_pending_rounds.append({
                "actors": set(new_ptrs),
                "rows": {p: cur_by_ptr[p] for p in new_ptrs},
            })
            self._pick_seen_actors.update(new_ptrs)

    # ------------------------------------------------------------ 换牌
    #
    # ★ 2026-09-24 第三版——前两版都建模错了：
    #   v1：每次点击标记变了就吐一条——记的是点击行为，不是决策本身。
    #   v2：靠 `in_mulligan()` True→False 时用"当时攒的标记"拼样本——
    #       方向对，但 `mulligan_marks()` 只在换牌**界面还开着**时读得到手牌，
    #       没有一个"起手到底是哪几张"的权威快照，全靠轮询过程中攒。
    #   v3（现在）：`kardsmem.matchlog.MatchLog` 新增的 `Logic` 反射链拿到了
    #   两个更硬的信号（2026-09-24 反字节码 + 实机验证）：
    #     - `starting_cards()`：`Logic.myStartingCards`，起手 5 张的**权威
    #       快照**，随时能读、不用趁换牌界面开着的时候抢时间攒。
    #     - `mulligan_done()`：`(我方done, 对方done)`，比"扫 `AllMatchActions`
    #       有没有 `XActionStartOfTurn`"更直接的换牌结束判据。
    #   ⚠ **必须在"我方 done 第一次变 True"那一刻**（也就是这次轮询）用
    #   **这一刻的 `st`** 跟 `starting_cards()` 比较——不能事后（比如打到
    #   turn 8 才想起来查）再比，那时候起手卡早被正常打出去了，会被误判成
    #   "换牌换掉的"（2026-09-24 手动查过一次撞到这个坑，用户当场指出）。
    #   `_mulligan_sample_written` 保证整局只在这个瞬间处理一次。
    def _poll_mulligan(self, st):
        if self._mulligan_sample_written:
            return
        log = self.sess.log
        if log is None or not log.locate():
            return
        try:
            mine_done, _enemy_done = log.mulligan_done()
        except Exception:                        # noqa: BLE001
            return
        if mine_done is not True:
            return
        starting = log.starting_cards()
        if not starting:
            return
        self._mulligan_sample_written = True     # 不管下面成不成，这一局只判定一次
        # ★ 用户指出：不用扯上 `st`/`board_api` 那套通用快照（还绑着 st.our_side）
        #   ——`ops_inject.mulligan_marks()` 本来就是查"现在手牌是哪几张"最直接的路，
        #   自带 side 过滤（跟 `st.our_side` 无关，见 §7.6d 的更正说明），
        #   换牌确认后立刻调它，读到的就是当前真实手牌。
        try:
            from agent import precheck
            current_hand_ids = {r.get("card_id")
                                for r in (precheck.call_read("mulligan_marks") or [])}
        except Exception:                        # noqa: BLE001
            current_hand_ids = set()
        option_list = [{"name": c.get("name"), "card_id": c.get("card_id"),
                        "discarded": c.get("card_id") not in current_hand_ids}
                       for c in starting]
        sample = _schema().make_sample(
            game=self.game, patch=self.patch, seat="local",
            turn=st.turn, t=self._t(), phase="mulligan",
            state=_schema().project_state(st, _session_mod.LOCAL, self._deck_roster),
            candidates={"subject": [], "target": [],
                        "option": [c["name"] for c in option_list]},
            verdict={},
            label={"type": "mulligan", "subject": None, "target": None,
                   "option": {c["name"]: c["discarded"] for c in option_list}},
            label_src="inferred", label_available=True,
            raw=option_list,
            receipt={"accepted": None, "action_id": None},
            events=[],
        )
        self._write(sample)

    # ------------------------------------------------------------ 主循环
    def run(self, seconds: Optional[float] = None):
        t0 = self._t()
        print("录制开始 -> %s（game=%s）" % (self.out_dir, self.game))
        try:
            while seconds is None or self._t() - t0 < seconds:
                self.tick()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()


def _schema():
    from learn import schema
    return schema


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读录制器（P0）")
    ap.add_argument("--seconds", type=float, default=None, help="录多久，不给就一直录到 Ctrl-C")
    ap.add_argument("--game", default=None)
    ap.add_argument("--tag", default=None,
                    help="自动分盘：连续录多局，每局一个文件 <tag>-1/-2/…（不用重启进程）")
    ap.add_argument("--patch", default=None)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    a = ap.parse_args(argv)
    rec = Recorder(out_dir=a.out, game=a.game, patch=a.patch, tag=a.tag)
    rec.run(seconds=a.seconds)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
