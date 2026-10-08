# -*- coding: utf-8 -*-
"""ops/world.py —— 世界与单例发现：座位、HUD/棋盘/逻辑/牌库 actor、对局级缓存失效、动作流（matchlog）与提示（notify）、卡对象查找。

由 `ops/inject.py`（P6/D1 拆分）按职责搬出；方法体与原来逐字相同，`ops.inject.Injector` 以 mixin 组合它们。
"""
from __future__ import annotations

import time
from typing import Optional

from kardsmem import props
from kardsmem.pick import hand_card_actors, hand_card_actors_v2
from ops import primitives as P
from ops.consts import (
    HUD_CLASS, LOC_BOARD_FRONTLINE, LOC_BOARD_HQLEFT, LOC_BOARD_HQRIGHT, NOTE_OFF_ACTOR_CARDID,
    NOTE_OFF_ACTOR_SELF_BASECARD, NOTE_OFF_BOARD_BATTLE_HUD, NOTE_OFF_DECK_FOR_ENEMY,
    NOTE_OFF_HUDCONTAINER_HUDPC, NOTE_OFF_HUD_END_TURN_BTN, NOTE_OFF_LOGIC_ONLINE_MATCH,
    NOTE_OFF_ULEVEL_ACTORS, NOTE_OFF_WORLD_LEVELS,
)
from ops.support import _action_has_card


class WorldMixin:
    """世界与单例发现：座位、HUD/棋盘/逻辑/牌库 actor、对局级缓存失效、动作流（matchlog）与提示（notify）、卡对象查找。"""

    # ------------------------------------------------------------ 定位
    def my_side(self) -> Optional[int]:
        """本地玩家的 ESideEnum（1=left / 2=right）。每局都可能不同（规格 §4.12）。"""
        try:
            from kardsmem.gs import GameState
            return GameState(self.ks).my_side
        except Exception:                                    # noqa: BLE001
            return None

    def our_back_enum(self) -> int:
        side = self.my_side()
        if side == 1:
            return LOC_BOARD_HQLEFT
        if side == 2:
            return LOC_BOARD_HQRIGHT
        raise RuntimeError("my_side 读不出，不能猜落点（见规格 §4.12）")

    def hand_actor(self, card_id: int, tries: int = 5, gap: float = 0.35,
                   sleep=time.sleep) -> Optional[dict]:
        """本方手牌里 `card_id` 对应的**手牌控件**记录（`pick.hand_card_actors()`，扫场读法）。

        ★ 2026-10-04 加「**横幅期重试**」——实机一局里出现 4 次 `card X 不在我方手牌`
        （`ops/play.py` 的 `hand_actor()` 返回空），而**同一张卡几秒后就打出成功**
        （`card 33` 下一步成功；`card 29` 在 turn 32 打出）⇒ 是**瞬时假阴性**，不是候选陈旧；
        3/4 次都**紧跟回合开始的横幅**。
        真因：扫场那一路是"`PropertiesSize==3640` 猜 `BP_HandCard_C`"，而横幅期间**手牌控件正在重建**，
        这个窗口里会短暂扫不到 —— 此时直接报"不在手牌"是错的。
        判据（**避免白等**）：只有权威账本 `hand_card_actors_v2()`
        （`BP_Deck_C::CardsPulledFromDeckBeforeBeingPutIntoHand`，游戏自己维护的"已抽进手牌"账本）
        里确实有这张卡时，才认为处在重建窗口并重试；**两边都没有 ⇒ 真的不在手 ⇒ 立刻返回 None**
        （与改动前同样的延迟，不惩罚正常的"不在手"判定）。

        ★ **不拿 v2 的记录去拖拽**：v2 的 `actor` 是**卡的场景 actor**（它数组里存的是
        `ABP_BaseCard_C*`），而这里要的是**手牌控件 actor**（`play.py` 要往它身上发
        `OnActorMouseEnter/MouseDown/…` 完成拖拽）—— 两者语义不同，混用会拖错东西。
        v2 在本函数里**只作"是不是重建窗口"的判据**。
        """
        n = max(1, int(tries))
        for i in range(n):
            for r in hand_card_actors(self.ks):
                if r.get("card_id") == card_id:
                    return r
            if i == n - 1:
                break
            try:
                in_ledger = any(x.get("card_id") == card_id for x in hand_card_actors_v2(self.ks))
            except Exception:                                    # noqa: BLE001
                break          # 账本读不出（例如座位读不出）⇒ 不改变旧行为：立刻放弃
            if not in_ledger:
                break          # 权威账本也没有 ⇒ 不是重建窗口，别白等
            sleep(gap)
        return None

    def hand_actor_diag(self, card_id: int) -> dict:
        """`hand_actor()` 找不到时的**诊断**（弯路 #44：诊断字段比"没触发"重要）。

        下次再出现"不在手牌"，错误里就带着**两种读法各自看到了哪些 card_id**，
        不用再靠猜"是陈旧的候选、还是重建窗口"。
        """
        def ids(fn):
            try:
                return sorted({r.get("card_id") for r in fn(self.ks)
                               if r.get("card_id") is not None})
            except Exception as exc:                            # noqa: BLE001
                return "%s: %s" % (type(exc).__name__, str(exc)[:60])
        return {"want": card_id, "scan": ids(hand_card_actors), "ledger": ids(hand_card_actors_v2)}

    def hud_actor(self) -> int:
        """当前这一局的 `BP_Widget_Battle_HUD_PC_C` —— **优先走指针链，不扫 GObjects**。

        ★ 2026-09-26 用户问："能避免多 live 问题吗？不扫，而是查找当前对局挂的到底是。"
        实测答案：**能，1 跳就够**（`_nn_scratch/probe_hud.py`）：
            `GetBoard()`（权威单例）→ `BP_Board_C::BattleHUD // 0x0AA0`
            ⇒ 拿到的对象**就是** `BP_Widget_Battle_HUD_PC_C`（与 GObjects 扫描结果同一个地址）。
        为什么这比扫描**更对**（不只是更快）：扫描会拿到**不属于当前对局**的对象 ——
        实测同一时刻 `BP_targetArrowRVX_C` 扫出 2 个，而 `UWorld::Levels` 的**所有** level 里
        一个都没有（上一局残留、GC 未回收）。HUD 同理曾拿到上一局的残留实例。
        退路：链断了（不在对局/字段为空）才退回扫实例 + `IsInViewport` 过滤，并记一笔 notes。
        """
        board = self.board_actor()
        if board:
            bcls = self.uclass_of_instance(board)
            off = self.off(bcls, "BattleHUD", NOTE_OFF_BOARD_BATTLE_HUD)
            hud = self.m.ptr(board + off) or 0
            if hud:
                cn = self.pool.fname_of(self.uclass_of_instance(hud))
                if cn == HUD_CLASS:
                    return hud
                # 少数版本这一跳给的是容器（`BP_Widget_Battle_HUD_C`），再跳一层拿 PC 版
                off2 = self.off(self.uclass_of_instance(hud), "BPWidgetBattleHUDPC",
                                NOTE_OFF_HUDCONTAINER_HUDPC)
                hud2 = self.m.ptr(hud + off2) or 0
                if hud2:
                    return hud2
        ws = self.instances_of_class(HUD_CLASS)
        if not ws:
            return 0
        self.notes.append("BattleHUD 指针链没给到 %s，退回扫实例（可能是上一局残留）" % HUD_CLASS)
        live = [w for w in ws if self.widget_in_viewport(w) is True]
        if live:
            if len(live) > 1:
                self.notes.append("%s 有 %d 个实例在 viewport 里，取第一个" % (HUD_CLASS, len(live)))
            return live[0]
        self.notes.append("%s 的 IsInViewport 都读不出/都为 0，退回第一个实例（可能是上一局残留）"
                          % HUD_CLASS)
        return ws[0]

    # ------------------------------------------------------------ 当前对局的"挂载点"
    def world_levels(self, refresh: bool = False) -> list:
        """`UWorld::Levels // 0x01C8`（`TArray<ULevel*>`）—— **当前世界的全部 level**。

        实测（对局中）：Num=2 —— level[0] 是 PersistentLevel（191 个 actor：Board/板卡…），
        level[1] 是另一个（24 个：**`BP_Deck_C`×2 就在这里**）。
        ⇒ "当前对局挂了哪些东西"要**遍历全部 level**，只看 PersistentLevel 会漏掉牌库那类。

        ★ 纯读部分已抽到 L1 原语 `ops/primitives.read_world_levels`（步骤 3）；缓存留在这一层。
        """
        if refresh or not hasattr(self, "_levels") or self._levels is None:
            self._levels = P.read_world_levels(self.m, self.ks.world or 0, NOTE_OFF_WORLD_LEVELS)
        return self._levels

    def level_actors(self, refresh: bool = False) -> list:
        """当前世界**全部 level** 的 actor（一次整读 + 一次批量分类）。

        实测：215 个 actor，整读 ~0.1ms、分类 ~5ms ⇒ **~5ms**；
        对比 `instances_of_class()` 每个类名一次 GObjects 全扫（12 万对象）**0.6~0.9s** ⇒ 约 **150×**。
        ★ 而且**更正确**：这里天然不含"上一局残留但已不属于任何 level"的对象。

        ★ 纯读部分已抽到 L1 原语 `ops/primitives.read_level_actors`（步骤 3，"find_actors"）；
          **对局级缓存（`_actors`/`_actors_by_cls`）留在这一层**（换局失效的纪律，弯路 #37）。
        """
        if refresh or not hasattr(self, "_actors") or self._actors is None:
            self._actors, self._actors_by_cls = P.read_level_actors(
                self.m, self.oa, self.pool, self.world_levels(refresh=refresh), NOTE_OFF_ULEVEL_ACTORS)
        return self._actors

    def actors_of_class(self, name: str, refresh: bool = False) -> list:
        """当前世界（全部 level）里某个类的 actor —— 替代 `instances_of_class()` 的热路径。

        `instances_of_class()` 仍保留（开局界面/结算页那些不在对局热路径上，
        而且可能真的挂在非 level 的地方），但**对局热路径一律走这个**。
        """
        self.level_actors(refresh=refresh)
        return list(self._actors_by_cls.get(name, []))

    def cache_scope(self) -> int:
        """当前对局的**作用域标识** —— 用它给"解析出来的指针"做缓存失效。

        取 `GetLogic()`：换一局它必然换指针（实测 2 局之间不同）⇒ 缓存自动失效，
        这就是用户说的"跟踪当前对局挂的到底是"的机制，而不是"每次重新扫"。
        """
        return self.singleton("logic") or 0

    def owned_by_current_match(self, obj: int, refresh: bool = False) -> bool:
        """**归属验证**：这个对象是不是"当前这一局挂着的东西"。

        判据（三条，任一成立即算**已证明属于本局**）：
          ① 它在当前世界**全部 level** 的 actor 列表里；
          ② 它就是当前的单例之一（`world` / `GetLogic` / `GetBoard` / `GetGameState`）；
          ③ 它是**已知指针链**上的对象 —— 实测 `BP_Board_C::BattleHUD`（HUD 是 UUserWidget，
             **不在任何 level 里**，只能靠这一条认）。
        ★ 返回 `False` 只表示"现有证据不足以证明它属于本局"，**不等于**"确定是残留" ——
          要坐实残留，判据是"它不在任何 level、也不在任何链上，而 GC 还没回收它"。
        用途：扫描出来的对象先过这一道，避免把上一局的残留当成当前局面。
        """
        if not obj:
            return False
        if obj in (self.ks.world, self.singleton("logic"), self.singleton("board"),
                   self.singleton("game_state"), self.singleton("session"),
                   self.singleton("level")):
            return True
        if obj in set(self.level_actors(refresh=refresh)):
            return True
        # ③ 已知链：Board→BattleHUD（含少数版本的多一跳容器→BPWidgetBattleHUDPC）
        if self.hud_actor() == obj:
            return True
        board = self.singleton("board")
        if board:
            off = self.off(self.uclass_of_instance(board), "BattleHUD", NOTE_OFF_BOARD_BATTLE_HUD)
            if (self.m.ptr(board + off) or 0) == obj:
                return True
        return False


    def _sync_match(self) -> None:
        """换了一局就把"对局级"缓存全丢掉（`_board`/`_deck`/`_logic`/`_ml`）。

        ★ 2026-09-30 实机（第二局起）：`board_actor()` 把 `_board` 缓存下来就再不刷新，
          第二局读到的是**上一局的棋盘**——`isSelectingHandTarget` 读 False、
          `selectHandTargetWidget` 读 0（`hand_target_pending` 全程报"不在等"，而游戏自己的
          `pick_pending` 明明在等），换牌窗口也判不到（`deck_actor` 同理）⇒ 换牌总超时、
          手牌选择卡死。判据：`GetBoard()`（游戏自己的单例访问器）给的指针变了 ⇒ 换局了。
          1 秒内不重复问（一次注入调用，很便宜）。
        """
        now = time.time()
        if now - getattr(self, "_sync_t", 0.0) < 1.0:
            return
        self._sync_t = now
        cur = self.singleton("board") or 0
        prev = getattr(self, "_sync_board", 0)
        if cur and prev and cur != prev:
            self._board = None
            self._deck = None
            self._logic = None
            self._ml = None
            self.notes.append("换局了（GetBoard %s -> %s）：丢掉 board/deck/logic/matchlog 缓存"
                              % (hex(prev), hex(cur)))
        if cur:
            self._sync_board = cur

    def board_actor(self, refresh: bool = False) -> int:
        """当前这一局的 `BP_Board_C` —— **优先游戏自己的单例访问器**（`GetBoard`）。

        ★ 用户 2026-09-26 定调："不要扫实例（多 live 实例几乎无解），跟踪这样的单例。"
          所以这里先 `UUtilityFunctions_C::GetBoard()`；只有它拿不到（世界没起来/不在对局）
          才退回扫实例，并**记一笔 notes**（让它可见，别静默降级）。
        用来读 `isSelectingHandTarget`（游戏自己的"手牌正在选目标"标志）。
        """
        self._sync_match()
        if refresh or self._board is None:
            got = self.singleton("board") or 0
            if not got:
                ws = self.instances_of_class("BP_Board_C")
                got = ws[0] if ws else 0
                if got:
                    self.notes.append("GetBoard() 没给指针，退回扫实例拿 BP_Board_C")
            self._board = got
        return self._board

    def end_turn_button(self, hud: int) -> int:
        off = self.off(self.uclass_of_instance(hud), "EndTurnButton", NOTE_OFF_HUD_END_TURN_BTN)
        return self.m.ptr(hud + off) or 0

    # ------------------------------------------------------------ 统一拖拽原语
    # ★★ 2026-09-26 重构（依据：`_nn_scratch/ida-attack-report.md` + `history-selectedcard.md`）
    #
    #   证据：`AddMoveToPlayerMoveQueue(sourceCard, actionType, …)` 的全部调用点里，
    #   **出牌 `"playCardFromHand"`（BP_BaseCard.cpp:1012）/ 移动 `"moveCard"`（:1147）/
    #   攻击 `"attackCard"`（:1164）在同一个文件、同一个落地函数里按条件分流**
    #   ⇒ 这不是三条链，是**一条链 + 一个 actionType 参数**。于是分四层：
    #
    #     L0 队列闸门：`BP_PlayerMoves_C::queueIsRunning`（`BP_PlayerMoves.cpp:254`）
    #        —— 队列在跑时动作**只入队不解析、零动作零提示、>30 s 才强制 resume**。
    #        对**所有**入队动作都成立（出牌/移动/攻击/选牌/选目标/状态同步）。
    #     L1 PC 拖拽状态机：`SetMouseDownActorManual(actor)` 一次写 5 个字段
    #        （dragActor/mouseDownActor/leftButtonDown/oldLeftButtonDown/dragStarted），
    #        收尾 `ForceReleaseDrag()` + `ForceEndMouseOver()`。**全通用**。
    #        ⚠ 不要写 `SelectedCard` —— 那是**手柄**字段（读点全在 `IsGamepad` 判定之后），
    #        鼠标拖拽用上面那 5 个；poke 它正是 2026-09-26 把游戏打崩的原因（§22.10/§22.10a）。
    #     L2 被拖对象：cursor 组 `cardUnderCursor 0x3EC` / `LocationNumberUnderCursor 0x664` /
    #        `LocationUnderCursor 0x668` / `RowUnderCursor 0x669`（在**被拖的 actor** 上）；
    #        选目标类动作还要写箭头 `overCardID`（+ 游戏自己的 `spectatorArrowNewTarget`）。
    #     L3 落地口：**按子类分** —— 手牌 `OnActorEndDrag`；板卡 `OnActorMouseUp`；
    #        "选目标"根本不是拖拽，是单击（`BP_Logic::GlobalMouseUp`）。
    def moves_actor(self) -> int:
        """`BP_PlayerMoves_C`（动作队列在这里）。走 level actor 快路径，不扫 GObjects。"""
        rows = self.actors_of_class("BP_PlayerMoves_C")
        return rows[0] if rows else 0

    def queue_running(self):
        """`queueIsRunning`（**按名字反射取偏移，不硬编码**）→ True/False/None(读不出)。

        `BP_PlayerMoves.cpp:254`：`if(!queueIsRunning) ResolvePlayerMoveQueue(); return;`
        ⇒ true 时提交动作**只入队、当场不解析、不产生动作、不发提示**，且不超时。
        这是"同一时刻出牌成功、攻击零新增"的通用解释。
        """
        mv = self.moves_actor()
        if not mv:
            return None
        try:
            from kardsmem import props
            pr = props.find_prop(self.ks, self.uclass_of_instance(mv), "queueIsRunning",
                                 pool=self.pool)
            if not pr:
                self.notes.append("BP_PlayerMoves_C 上没找到 queueIsRunning（反射链）")
                return None
            v = self.m.u8(mv + pr["offset"])
            return None if v is None else bool(v)
        except Exception as e:                                 # noqa: BLE001
            self.notes.append("queue_running 读失败：%s" % e)
            return None

    def wait_queue_idle(self, timeout: float = 4.0, poll: float = 0.1) -> bool:
        """等动作队列空闲（L0 闸门）。空闲返回 True；超时返回 False 并记 `notes`。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            q = self.queue_running()
            if q is False:
                return True
            if q is None:
                return True            # 读不出 ⇒ 不拦（保持旧行为，别拿判据否决动作）
            time.sleep(poll)
        self.notes.append("queueIsRunning 在 %.1fs 内一直为 true（动作可能只入队不解析）" % timeout)
        return False

    # ------------------------------------------------------------ 回执
    def matchlog(self, refresh: bool = False):
        """动作流读取器 —— **长寿命单例**（`_mark()`/`_receipt()` 共用）。

        ★★ 2026-09-26 用户："动作为什么有点慢。" 实测（`_nn_scratch/probe_timing*.py`）：

        | 组件 | 冷 | 同实例第二次 |
        |---|---|---|
        | `MatchLog.locate()` | **4.4~5.8 s**（要扫 GObjects 找动作流容器） | **0 ms** |
        | `_mark()` | 4.2 s | **4.3 s** ← 每次都新建实例，等于每次都重扫 |
        | `_receipt(timeout=0.2)` | 14.6 s | **6.5 s** ← `timeout` 形同虚设 |

        ⇒ 一次两阶段出牌原本要多付 **9~15 s** 在"重新定位动作流"上，这就是"慢"的主因。
        修法：实例缓存 + **每次调用都验证新鲜度**。

        ★★ 2026-09-26 用户追问："那么怎么确保这个东西是最新的呢？" 答案分两半：

        * **对象指针：每次现算，绝不长期缓存。** 权威链是
          `GetLogic()` → `BP_Logic_C::onlineMatch // 0x0810`（`BP_Logic_classes.hpp:130`）
          —— `GetLogic` 是游戏自己的单例访问器，**换局必然换指针** ⇒ 每次拿到的都是当前这一局。
          成本 ≈ 2 次跨进程读（`logic_actor()` 本身已是缓存）。
        * **偏移表：可以永久缓存。** 4 个数组字段的偏移只依赖类布局（同一构建不变）。
        * 再加两道**便宜**的兜底（链断了/不在对局时也安全）：
          `MatchLog.is_stale()`（对象头 class 变了 / `AllMatchActions` 计数不单调）
          ⇒ `forget()` + 重新定位（`locate()` 现在先走 `ULevel::Actors` 快路径 ~5 ms，
          不再动辄 4~6 s 全扫）。
        """
        from kardsmem.matchlog import MatchLog
        ml = getattr(self, "_ml", None)
        if ml is None or refresh:
            ml = MatchLog(self.ks)
            self._ml = ml
            self._ml_located = False
        # ① 权威链现算"当前这一局的 OnlineMatch"；与缓存不同 ⇒ 换对象（pin 只在类变了才重算偏移）
        try:
            logic = self.logic_actor()
            if logic:
                off = self.off(self.uclass_of_instance(logic), "onlineMatch",
                               NOTE_OFF_LOGIC_ONLINE_MATCH)
                cur = self.m.ptr(logic + off) or 0
                if cur and cur != ml.obj:
                    self.notes.append("MatchLog：权威链给的对象变了 0x%X → 0x%X，换之"
                                      % (ml.obj, cur))
                    ml.pin(cur)
        except Exception as e:                                 # noqa: BLE001
            self.notes.append("MatchLog：权威链取 onlineMatch 失败（%s）" % e)
        # ② 兜底：链没给或给了假值 ⇒ 用便宜判据决定要不要重定位
        if not ml.obj or ml.is_stale():
            if ml.obj:
                self.notes.append("MatchLog：is_stale() 为真 ⇒ forget+重新定位")
                ml.forget()
            ml.locate()
        return ml

    def _receipt(self, mark: int, action_type: str, card_id: Optional[int] = None,
                 timeout: float = 2.5, awaiting_probe=None) -> dict:
        """动作成没成，看游戏自己的动作流（跟 ops.py 同一套判据）。

        `awaiting_probe`：可调用对象，返回真 ⇒ **立刻**回
        `{"ok": False, "awaiting_target": True}`，不再等满 `timeout`。
        用途：**部署型指向**的单位在阶段一**按设计不会有** `XActionPlayCardFromHand`
        （卡留在"待点目标"态，动作要到阶段二点目标才进流）—— 等满 3 s 是纯浪费。
        实测 `card_being_played_from_hand()` 热态 0 ms ⇒ 这个探测是免费的。
        """
        ml = self.matchlog()
        t0 = time.time()
        probe_hits = 0
        while True:
            rows = ml.since(mark)
            hit = [r for r in rows if r.get("action_type") == action_type]
            if card_id is not None:
                hit = [r for r in hit if _action_has_card(r, card_id)]
            if hit:
                return {"ok": True, "actions": hit, "waited": round(time.time() - t0, 2)}
            if awaiting_probe is not None:
                try:
                    if awaiting_probe():
                        # ★ 连续 3 次（~0.15 s）都成立才认 —— 避免抓在"提交中间态"上误判
                        probe_hits += 1
                        if probe_hits >= 3:
                            return {"ok": False, "actions": [], "awaiting_target": True,
                                    "waited": round(time.time() - t0, 3)}
                    else:
                        probe_hits = 0
                except Exception:                              # noqa: BLE001
                    pass
            if time.time() - t0 >= timeout:
                # ★ 失败时**统一**把游戏提示捞出来当权威理由（规格 §7.6f）：提示短命，
                #   只能在"刚失败"这一刻轮询；只回一个 ok=False 等于让失败不可诊断。
                return {"ok": False, "actions": [], "waited": round(time.time() - t0, 2),
                        "game_hints": self.notify_texts()}
            time.sleep(0.05)

    def _mark(self) -> int:
        return self.matchlog().mark()

    def _notify_watcher(self):
        """提示 watcher（惰性 + 缓存）。第一次 `poll()` 只建立基线（**不要**把它当"刚弹的提示"）。"""
        if self._nw is None:
            try:
                from kardsmem.notify import NotifyWatcher
                w = NotifyWatcher(self.ks)
                w.poll()                                  # 预热：吞掉调用前就存在的旧槽位
                self._nw = w
            except Exception as e:                        # noqa: BLE001
                self.notes.append("NotifyWatcher 建不起来（notify_texts 失效）：%s" % e)
                self._nw = False
        return self._nw or None

    def notify_texts(self, limit: int = 4) -> list:
        """动作被拒时游戏弹的**权威理由**（规格 §7.6f）——跟 `ops.py::_notify_texts` 同源。

        ★ 为什么必须有：外部判据（`CanAttack`/`CanPlayFromHand`）**只挑不判**，它的缺漏
          是必然的；动作发出去被游戏拒掉时，唯一能说清"为什么"的就是这条提示通道。
          只报 `ok=False` 而不报理由，等于把失败变成不可诊断。
        ★ 提示**短命**（淡入淡出后 widget 销毁）⇒ 只能轮询、事后捞不到 ⇒ 失败时**立刻**调它。
        读不到就回 `[]`（绝不抛）：提示是增强信息，不能因为它把主流程带崩。
        """
        w = self._notify_watcher()
        if not w:
            return []
        try:
            from kardsmem.locres import translator
            tr = translator()
            out, seen = [], set()
            for r in w.poll():
                t = tr(r.get("text") or "")
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            return out[:limit]
        except Exception as e:                            # noqa: BLE001
            self.notes.append("notify_texts 读取失败：%s" % e)
            return []

    def _card_object(self, card_id: int) -> int:
        """拿一张牌的 `UBaseCardObject`（手牌/板卡同基类，都用 actor 的 `selfBaseCardRef`）。"""
        # ★ 2026-10-07：先 `tries=1`（只扫一遍手牌控件，不走"账本重试"）。完整版 `hand_actor` 在 miss 时会查
        #   `hand_card_actors_v2` ⇒ `oa.find_by_class_name` 全量扫 GObjects（秒级），而场上卡必然 miss 手牌 ⇒ 每次白扫。
        #   两路都没找到才退回完整版（横幅期手牌控件重建的重试语义不变）。
        rec = self.hand_actor(card_id, tries=1)
        actor = rec["actor"] if rec else self.board_actor_of(card_id)
        if not actor:
            rec = self.hand_actor(card_id)
            actor = rec["actor"] if rec else 0
        if not actor:
            return 0
        cls = self.uclass_of_instance(actor)
        off = self.off(cls, "selfBaseCardRef", NOTE_OFF_ACTOR_SELF_BASECARD)
        return self.m.ptr(actor + off) or 0

    def our_front_enum(self) -> int:
        """我方**前线**的 `ECardLocationEnum` 值（7 = `Board_Frontline`）。

        跟 `our_back_enum()` 一对；`move_to_front()` 用的是同一个值。
        """
        return LOC_BOARD_FRONTLINE

    # ------------------------------------------------ 站位（列号）与「空隙序号」
    # ★★ 2026-09-27 用户实机纠正：**"你实际上把 108 部署在了最右侧"** ——
    #   我按"请求 slot=1 = 第 1 格"理解站位，那是**错的**。用户给的权威模型：
    #   > 一般真人部署时，将单位拖拽到所需的位置：**两个单位中间，或最右最左侧，放手**。
    #   > 场面上的渲染会把单位挪开一些。最终部署时显示最终渲染。
    #   也就是说：**站位是"拖到哪个空隙"，不是"第几格"**；游戏再把"拖到哪"换算成
    #   列号，并按"谁在你左边"插进去、密集重排。换算链（`BP_Board.cpp`，1.58 导出；
    #   1.60 只有字段偏移变、逻辑同）：
    #
    #     FindCardLocationUnderCursor()          # **纯出参**：读真实鼠标 → 反投影 → 射线
    #       → 命中 `Board_HQLeft` / `Board_HQRight` / `Board_Frontline` 那一排
    #         （`GetObjectName(component)` 以 `Board_` 开头，`Board_HQLeft` ⇒ locEnum 5）
    #       → FindLocationNumberForCoordinates(命中点坐标, locEnum)
    #     FindLocationNumberForCoordinates(coords, loc):          # BlueprintPure
    #       highest = -1
    #       for 该排每张卡（GameState.FetchCardsByLocation 的顺序）:
    #           if 视觉板卡拿不到:            continue
    #           if 视觉板卡.doingConvertVisualAsNewCard:  return   # 变身动画中，整条不接
    #           n = 视觉板卡.cardLocationNumber                      # 它当前的列号
    #           视觉卡 = GetVisualCardFromID(该卡)                    # = 同一 actor（BP_BoardCard_C
    #                                                                #   继承 BP_BaseCard_C）
    #           if 视觉卡为空: goto Label_1604
    #           GetActorBounds → center, extent
    #           if NearlyEqual(center.X, cursor.X, extent.X - 2):
    #               cardAtLocation = 该卡; return                    # "光标底下是这张卡"
    #     Label_1604:
    #           if center.X < cursor.X and highest < n: highest = n
    #       locationNumber = highest + 1
    #
    #   ⇒ **写进 `LocationNumberUnderCursor` 的那个数 = 「光标左边那些卡的最大列号 + 1」**。
    #     要表达"插到第 i 个空隙"（0 = 最左，n = 最右），得写
    #         `numbers[i-1] + 1`（i>0），i==0 写 0        （numbers = 该排已排序的列号）
    #     ★ 它**不是最终格子号，是比较键**：行有空洞时两者不同（例如列号 [0,2] 的空隙 1
    #       要写 1 而不是 2；空洞行请求 3 ⇒ 最终落在 2，压实）。稠密行上两者相等，
    #       所以以前"稠密行 request=final"的观测看着像对的 —— 它掩盖了这个区别。
    #     ⚠ `_free_slot()`（默认路径）扫的是**第一个空列号**，在稠密行上恰好等于"追加到最右"；
    #       那次 slot 被吞掉落到 3（= 最右）就是这么来的：`_free_slot()` 回了 3。
    #
    #   验算方式是**问游戏自己**（`probe_gap_numbers()`）：用相邻两张卡的**渲染位置**
    #   构造空隙坐标，调 `BP_Board_C::FindLocationNumberForCoordinates` 读出参。
    #   该函数 `BlueprintCallable/BlueprintPure`、只写出参 ⇒ 红线允许的**只读查询**。

    def _row_cards(self, row: str = "back", side=None) -> list:
        """该排的 `(列号, card_id)`，按列号升序。

        ★ `row="back"` **含总部** —— 总部和支援线单位同属 `locEnum=5`（`Board_HQLeft`），
          共用一套列号（`hq` 不是单独一排）。
        """
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        side = st.seat(side)                                   # 缺省 = 我方
        cs = [c for c in st.cards
              if c.side == side and (c.obj.InSupportLine() if row == "back" else c.obj.InFrontline())
              and c.slot is not None]
        cs.sort(key=lambda c: (c.slot, c.uid))
        return [(int(c.slot), int(c.obj.CardID)) for c in cs]

    def _card(self, card_id: int):
        from kardsmem import board as BA
        st = BA.open_source("mem").snapshot()
        for c in st.cards:
            if c.obj.CardID == card_id:
                return c, st
        return None, st

    def find_card(self, card_id: int, side=None, any_side: bool = False):
        """某张卡（缺省只认我方；`side` 指定座位；`any_side=True` 不限座位）。"""
        rec, st = self._card(card_id)
        if rec is None:
            return None
        if not any_side and rec.side != st.seat(side):
            return None
        return rec

    def board_actor_of(self, card_id: int) -> int:
        """场上（后排/前线/HQ）某张卡的 `BP_BoardCard_C` actor。"""
        from kardsmem.world import Locator
        loc = Locator(self.m, self.ks.base)
        for p in (loc.actors() or []):
            c = self.oa.class_of(p)
            if not c or self.pool.fname_of(c) != "BP_BoardCard_C":
                continue
            # 备注：actor 上的 CardID @0x3C8（规格 §5）
            if self.m.i32(p + NOTE_OFF_ACTOR_CARDID) == card_id:
                return p
        return 0

    # ------------------------------------------------------------ 换牌（mulligan）
    # 真实流程（规格 §7.6d，逐跳有蓝图出处）：
    #   ① 点手牌 → `BP_HandCard_C::OnActorClicked()` → `MulliganDiscardToggle()` 翻转
    #      `shouldDiscard@0x980`（屏幕上出现红圈；再点一下取消）
    #   ② 点确认按钮 → `BP_ConfirmCardsButton_C` 的
    #      `BndEvt__..._onClicked__DelegateSignature()` → `BP_Logic_C::onCardSelectionConfirmed()`
    #      → `BP_Deck_C::MulliganDone()` → `DiscardAllMarkedCards()` → `SendCardsToMulligan()`
    # 我们只做"把鼠标会做的事按同样顺序做一遍"：悬停（含转发）→ 点击。
    def logic_actor(self, refresh: bool = False) -> int:
        """`BP_Logic_C` 实例（**缓存地址**）。

        ★ 2026-09-25 血的教训：`instance_of_class()` 每次都要**全量扫 GObjects**（实测
          单次 ~3-4s）。而换牌窗口只有几秒，用它做轮询 = 第一次采样就晚了 11 秒，
          窗口永远抓不到。这两个 actor 是常驻的（跨对局不销毁），所以地址缓存一次就够，
          之后每帧只做一次 `ReadProcessMemory`（微秒级）。
        """
        self._sync_match()
        if refresh or self._logic is None:
            # ★ 先走**游戏自己的单例访问器**（`UUtilityFunctions_C::GetLogic`）——
            #   它给的是"当前这一局那一个"；取不到（世界没起来/不在对局）才退回扫实例并记一笔。
            got = self.singleton("logic") or 0
            if not got:
                got = self.instance_of_class("BP_Logic_C") or 0
                if got:
                    self.notes.append("GetLogic() 没给指针，退回扫实例拿 BP_Logic_C")
            self._logic = got
            self._ml = None          # ★ 换了一局（GetLogic 变了）⇒ 动作流容器可能换 ⇒ 丢掉 MatchLog 缓存
        return self._logic

    def deck_actor(self, refresh: bool = False) -> int:
        """`BP_Deck_C` 实例（**缓存地址**，理由同 `logic_actor`）。

        ⚠ **不能缓存 0**：对局没加载完时 `BP_Deck_C` 还没创建，若把 0 缓存下来，
          之后就永远找不到了（一直返回 None）。只在非 0 时缓存。

        ★ 2026-09-25 真 bug：**同时存在两个 `BP_Deck_C`**——我方一个、对手（含 AI）
          一个，靠 `deckForEnemy`（bool @0x0478）区分。旧版 `instance_of_class()`
          只返回**第一个**找到的实例，遍历顺序不保证是我方那个——挑到对手那份后，
          它的 `showingStartingHand` 永远是我们这边看不到的东西，实测**全程读 0**，
          导致 `wait_for_mulligan()` 死等 90 秒也等不到窗口（症状看起来像"窗口太短
          抓不住"，其实是"问错了对象"）。改成显式过滤 `deckForEnemy==False`。
        """
        self._sync_match()
        if refresh or not self._deck:
            found = 0
            for p in self.instances_of_class("BP_Deck_C"):
                cls = self.uclass_of_instance(p)
                off = self.off(cls, "deckForEnemy", NOTE_OFF_DECK_FOR_ENEMY)
                if not self.peek(p, off, "u8"):
                    found = p
                    break
            self._deck = found
        return self._deck
