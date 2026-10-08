#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legality.py —— **进程外**重算动作合法性（跑游戏自己的蓝图字节码）。

为什么可以这么做
================
`cardsCheckFunctions_C::CanAttack` 和 438 个 `CanPlayFromHand` 覆写
**都是蓝图**（BlueprintGeneratedClass），有 Kismet 字节码 ⇒
`kardsmem/vm.py` 在**我们自己的进程里**解释执行它，
**不在游戏进程里跑任何代码**，不越红线（规格 §6.4b）。

它只用来「挑」，不许用来「判」
==============================
判据一定有缺漏（卡池两千张、效果互相叠、还有一层原生代码）。
所以本模块的输出**只能用于排序和提示**：

    * 给候选排序（「这几个目标里哪些看起来合法」）
    * 动作被拒时给一个**可能的**原因（权威原因来自游戏的提示文本）

**绝不能拿它否决动作。** 调用方带 `force` 就照发，让游戏裁决（§7.6f）。

`CanAttack` 的签名
==================
    CanAttack(attackerCard, defenderCard, attackerKredits, currentTurn,
              cardsInAttackedLocation, __WorldContext)
        -> canAttack, failReason, Reason_Param_1

★ `cardsInAttackedLocation` 是**调用方组的**，不在卡对象上 ——
  它就是「防守方所在那一行、那一侧的全部卡」。战斗机拦截
  （`fighter_protecting`）正是靠遍历它实现的：轰炸机打某条阵线时，
  只要那条线上有敌方战斗机（`ETypeEnum::fighter == 4`）且不是未揭示的隐蔽牌，
  就只能拿战斗机当目标。
"""
from __future__ import annotations

from typing import Optional

CHECK_CLASS = "cardsCheckFunctions_C"

# `failReason` → 中文（本地化表里没有这些键，它们是内部串 ⇒ 这里自己给一份）。
# 18 个键来自导出反编译逐行核对（§6.4b）。**权威口径已升级见 REASON_HELPBUBBLE**——
# 这份手写翻译只在权威链查不到时兜底（离线 / locres 装不上）。
REASON_ZH = {
    "": "合法",
    "unit_cant_attack": "这个单位不能攻击",
    "cant_be_attacked_by_unit": "目标不能被这类单位攻击",
    "unit_cant_be_attack_by_unit": "目标不能被这类单位攻击",
    "cant_attack_with_ground_units": "地面单位打不到那里",
    "unit_is_pinned": "被压制，不能移动或攻击",
    "deployment_sickness": "部署当回合不能行动",
    "not_enough_range": "射程不够",
    "hq_is_being_garded": "对方总部处于被守护状态",   # ★ 游戏自己拼错成 garded，照抄
    "is_being_garded": "目标处于被守护状态",
    "fighter_protecting": "那条阵线有战斗机拦截（轰炸机只能打战斗机）",
    "location_has_smokescreen": "那条阵线有烟幕",
    "defender_has_smokescreen": "目标有烟幕",
    "has_already_attacked": "这回合已经攻击过了",
    "no_attack_left": "没有攻击次数了",
    "not_enough_kredits": "指挥点不够",
    "not_a_unit": "目标不是单位",
}

# ★★ F10c（2026-09-23）：`failReason` 不是 loc key，直接拿它去查 `Game.locres`
#   查不到（试过，见下面）——它只是**内部字符串**。真正的英文提示文本住在
#   `helpbubbles` 这个 namespace 下，key 是 `notify_cant_attack_<某个别的拼法>`
#   （grep `Game.locres` 原始字节确认过：`notify_cant_attack_fighter_protecting`
#   → `"A fighter is protecting this target."`）。⇒ 两跳：
#     failReason --(这张表)--> helpbubbles key --(locres en/zh-Hans 对齐)--> 中文
#   ⚠ `helpbubbles` 那边的键名和 `failReason` 常常**不是同一个词**（游戏自己
#   两套命名没对齐），逐个手工核对过，不是猜的：
#     * `deployment_sickness` → `same_turn_as_deployed`（"can't attack the turn
#       it comes into play"）——2026-09-23 实机 `attack ! m1 ehq` 弹出的中文
#       提示"你的单位无法在加入战局的同一回合中发动攻击"和这条**一字对应**。
#     * `no_attack_left` → `cant_move_and_attack_same_turn`——同一次实机
#       跑 `can_attack` 测出来的：单位这回合刚从支援阵线移到前线，
#       `HasAttackLeft` 为假但不是因为部署病，是"移动和攻击不能同回合"。
#     * `not_a_unit` → `only_units_can_attack`；`not_enough_kredits` →
#       `out_of_kredits`；`hq_is_being_garded`/`is_being_garded` 都收敛到
#       `the_hq_is_being_guarded`/`unit_is_being_guarded`。
#     * `cant_be_attacked_by_unit` 和 `unit_cant_be_attack_by_unit` 是
#       `CanAttack` 字节码里**两条不同分支**写的两个相近字符串（大概率是游戏
#       自己命名不一致的历史遗留），但 `helpbubbles` 下只找到一条对应文本
#       （`unit_cant_be_attacked_by_this_unit`）——两个都先接到它上面，
#       查到分叉证据再拆开。
REASON_HELPBUBBLE = {
    "unit_cant_attack": "notify_cant_attack_unit_cant_attack",
    "cant_be_attacked_by_unit": "notify_cant_attack_unit_cant_be_attacked_by_this_unit",
    "unit_cant_be_attack_by_unit": "notify_cant_attack_unit_cant_be_attacked_by_this_unit",
    "cant_attack_with_ground_units": "notify_cant_attack_attack_ground_units_this_turn",
    "unit_is_pinned": "notify_cant_attack_is_pinned",
    "deployment_sickness": "notify_cant_attack_same_turn_as_deployed",
    "not_enough_range": "notify_cant_attack_not_enough_range",
    "hq_is_being_garded": "notify_cant_attack_the_hq_is_being_guarded",
    "is_being_garded": "notify_cant_attack_unit_is_being_guarded",
    "fighter_protecting": "notify_cant_attack_fighter_protecting",
    "location_has_smokescreen": "notify_cant_attack_location_has_smokescreen",
    "defender_has_smokescreen": "notify_cant_attack_unit_has_smokescreen",
    "has_already_attacked": "notify_cant_attack_already_attacked_this_turn",
    "no_attack_left": "notify_cant_attack_cant_move_and_attack_same_turn",
    "not_enough_kredits": "notify_cant_attack_out_of_kredits",
    # ★ 2026-09-25 实机补：`CanAttack(27 -> 54)` 在 2 指挥点时返回
    #   `not_enough_kredits_to_target`（"打**这个目标**要多花指挥点"，跟打得起别的
    #   目标是两回事）。`helpbubbles` 下有对应英文原文
    #   `notify_cant_attack_target_out_of_kredits` = "You don't have enough Kredits
    #   to attack this target with this unit" —— 语义逐字对应，不是猜的。
    "not_enough_kredits_to_target": "notify_cant_attack_target_out_of_kredits",
    "not_a_unit": "notify_cant_attack_only_units_can_attack",
}

def reason_zh(reason: str) -> str:
    """`failReason` → 中文。**优先权威链**（locres 原文，`kardsmem.locres.namespace_zh`
    走 `helpbubbles` namespace），查不到才退回手写表。"""
    hb = REASON_HELPBUBBLE.get(reason)
    if hb:
        try:
            from kardsmem import locres
            zh = locres.namespace_zh("helpbubbles", hb)
            if zh:
                return zh
        except Exception:                                    # noqa: BLE001
            pass
    return REASON_ZH.get(reason, reason)


class Legality:
    """跑 `CanAttack` 的薄封装。**只挑不判。**"""

    def __init__(self, session, my_seat: Optional[int] = None):
        self.s = session
        self.my_seat = my_seat
        self._fn = None
        self._vm_kw = None
        self._logic = None
        self._self = None
        self._board = None
        self._gs = None

    def _locate(self):
        if self._fn is not None:
            return self._fn
        from kardsmem import kismet
        from kardsmem.objects import ObjectArray
        oa = ObjectArray(self.s)
        pool = oa.pool()
        seen = set()
        for p in oa.iter_objects(skip_cdo=False):
            c = oa.class_of(p)
            if not c or c in seen:
                continue
            seen.add(c)
            if pool.fname_of(c) == CHECK_CLASS:
                self._fn = kismet.find_function(self.s, c, "CanAttack") or False
                # ★ `self` 要是**这个函数库自己**（`cardsCheckFunctions_C` 的 CDO），
                #   不是攻击方那张卡 —— 函数里有 `EX_LocalVirtualFunction`
                #   （对 self 的按名调用），self 给错就会停在"函数 X 找不到"。
                self._self = p
                return self._fn
        self._fn = False
        return False

    def _vm(self):
        """建一个求值器。`get_field` 走反射链，`card_natives` 走 UBaseCardObject 原语。"""
        if self._vm_kw is None:
            import os
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "tools"))
            from canplay import make_get_field                  # noqa: PLC0415
            self._vm_kw = {"get_field": make_get_field(self.s),
                           "view": self._make_rich_view()}
        from kardsmem.cardnatives import CardNatives
        from kardsmem.vm import VM
        # ★ 把 BoardState 也给进去：`getKreditBySide` / `IsSideActive` 这类原语
        #   问的是盘面级的信息，卡对象上没有。
        return VM(self.s, get_field=self._vm_kw["get_field"],
                  card_natives=CardNatives(self._board, view=self._vm_kw["view"],
                                           my_seat=self.my_seat),
                  hooks={"GetEnumeratorUserFriendlyName": self._enum_name,
                         "GetLogic": self._get_logic,
                         "IsValid": self._is_valid,
                         "IsValidClass": self._is_valid,
                         "GetGameState": self._get_gamestate})

    def _is_valid(self, vm, frame, obj, args, e):
        """`KismetSystemLibrary::IsValid(Object)`。

        `kismetlib.READS` 把它列为"由求值器注入"——就是这里。
        进程外的判据：指针在合理区间，且 `ClassPrivate` 读得回来
        （读不回来说明那块内存根本不是个 UObject）。
        ★ 判不了"pending kill"——那是引擎内部标记。所以这是个**偏宽**的判据：
          可能把刚销毁的对象仍判成 valid。宁可偏宽，也不要把好对象判死。
        """
        v = args[0] if args else None
        if not isinstance(v, int) or v <= 0x10000:
            return False
        return bool(self.s.m.ptr_or_zero(v + 0x10))

    def _get_gamestate(self, vm, frame, obj, args, e):
        """`GetGameState(&GameState)`。

        ★ 必须给 hook，不能靠"按名字找同名函数"那条路：`GetGameState` 的蓝图实现
          自己又调了一次 `GetGameState`，按名字解析会解析回它自己 ⇒ **成环**
          （2026-09-23 实机踩到，求值器现在会直接报"调用成环"而不是耗尽深度）。
        """
        if self._gs is None:
            try:
                # `gamestate` 是 `world.Locator` 上的**属性**（GWorld -> UWorld -> GameState）
                self._gs = self.s.locator().gamestate or 0
            except Exception:                                # noqa: BLE001
                self._gs = 0
        names = [k.args.get("prop") for k in e.kids]
        for n in names[1:]:
            if n:
                frame.locals[n] = self._gs
        return self._gs

    def _get_logic(self, vm, frame, obj, args, e):
        """`GetLogic(&Logic)` —— 返回 `BP_Logic_C` 那个 actor。

        它是**出参**：调用点上第二个 kid 是要写回的局部变量。
        全局唯一（实机确认：GObjects 里 `BP_Logic_C` 只有 1 个实例），扫一次缓存。
        """
        from kardsmem.objects import ObjectArray
        if self._logic is None:
            oa = ObjectArray(self.s)
            pool = oa.pool()
            self._logic = False
            for p in oa.iter_objects():
                c = oa.class_of(p)
                if c and pool.fname_of(c) == "BP_Logic_C":
                    self._logic = p
                    break
        # 出参按调用点上的变量名回写（和 canplay.py 的 GetTargetedCard 同一套做法）
        names = [k.args.get("prop") for k in e.kids]
        for n in names[1:]:
            if n:
                frame.locals[n] = self._logic or 0
        return self._logic or 0

    # ETypeEnum（KardsCore_structs.hpp:82）。**先确认对象确实是 ETypeEnum 再用它**。
    ETYPE = ["NotAvailable", "location", "order", "tank", "fighter", "bomber",
             "infantry", "artillery", "antiair", "antitank", "tankdestroyer",
             "gotcha", "wildcard"]

    def _enum_name(self, vm, frame, obj, args, e):
        """`UKismetNodeHelperLibrary::GetEnumeratorUserFriendlyName(UEnum*, int32)`。

        ★ 不去猜是哪个枚举：先用名字池读出这个 `UEnum` 对象**自己的名字**，
          确认是 `ETypeEnum` 才查表。是别的枚举就抛 `Unimplemented` ——
          求值器的契约是"要么给对答案，要么说清为什么给不出"。
        """
        from kardsmem.kismetlib import Unimplemented
        # ★ 求值器已经把 `ObjectConst` 解成了那个 UObject 的**名字字符串**
        #   （实测 args == ['ETypeEnum', 6]），不是裸指针 —— 别再去名字池查一遍。
        # ★ 2026-09-23 起 `ObjectConst` 求值给的是**指针**（名字留在 args["v"]）⇒
        #   这里按指针查名字池。先前求值器给的是名字字符串，改过来之后要跟着改。
        nm = args[0] if args else None
        val = args[1] if len(args) > 1 else None
        if isinstance(nm, int) and nm:
            try:
                from kardsmem.objects import ObjectArray
                nm = ObjectArray(self.s).pool().fname_of(nm)
            except Exception:                                # noqa: BLE001
                nm = None
        if nm != "ETypeEnum":
            raise Unimplemented(
                "GetEnumeratorUserFriendlyName：只认得 ETypeEnum，这个是 %r" % nm)
        try:
            return self.ETYPE[int(val)]
        except (TypeError, ValueError, IndexError):
            raise Unimplemented("ETypeEnum 取值 %r 超出 0..%d"
                                % (val, len(self.ETYPE) - 1))

    def _make_rich_view(self):
        """卡对象 -> 字段视图，**外加逐实例被贴的效果**。

        ★ `cards.read_raw` 不含 `received_abilities`，而 `CanAttack` 一上来就问
          `HasCantAttack` ⇒ 不补进来求值器会立刻停在那里。
          `read_live_effects` 贵一些，所以按指针缓存。
        """
        from kardsmem import cards as C
        cache = {}

        def view(ptr):
            if ptr not in cache:
                d = C.read_raw(self.s, ptr) or {}
                try:
                    d.update(C.read_live_effects(self.s, ptr) or {})
                except Exception:                            # noqa: BLE001
                    pass                 # 补不上就让原语自己抛 Unimplemented
                cache[ptr] = d
            return cache[ptr]
        return view

    @staticmethod
    def location_cards(st, defender):
        """`cardsInAttackedLocation` —— 防守方所在**那一行、那一侧**的全部卡对象指针。

        ★ 这是 `CanAttack` 唯一需要调用方自己组的参数。战斗机拦截就靠它。
        """
        out = []
        for c in st.cards:
            if c.obj.side != defender.obj.side or c.obj.Location != defender.obj.Location:
                continue
            ptr = (c.raw or {}).get("ptr")
            if ptr:
                out.append(ptr)
        return out

    def can_attack(self, st, attacker, defender) -> dict:
        """→ {ok, can, reason, reason_zh, stopped}。

        `stopped` 非空 = **求值器没算出来**（缺原语/读不到字段），
        这时 `can` 是 None —— **当作"不知道"，不是"不合法"**。
        """
        fn = self._locate()
        if not fn:
            return {"ok": False, "stopped": "找不到 cardsCheckFunctions_C::CanAttack",
                    "can": None, "reason": None, "reason_zh": None}
        ap = (attacker.raw or {}).get("ptr")
        dp = (defender.raw or {}).get("ptr")
        if not (ap and dp):
            return {"ok": False, "stopped": "卡对象指针读不到",
                    "can": None, "reason": None, "reason_zh": None}
        self._board = st            # 给 CardNatives 用（盘面级原语）
        kred = (st.kredits or {}).get(attacker.obj.side)
        # ★ `VM.run` 的 args 是 **dict**（局部变量名 -> 值），不是位置参数列表。
        #   名字取自 SDK 的 cardsCheckFunctions_C_CanAttack 参数结构。
        args = {"attackerCard": ap, "defenderCard": dp,
                "attackerKredits": kred if kred is not None else 0,
                "currentTurn": st.turn or 0,
                "cardsInAttackedLocation": self.location_cards(st, defender),
                "__WorldContext": 0}
        vm = self._vm()
        try:
            r = vm.run(fn, self_obj=self._self or ap, args=args)
        except Exception as e:                               # noqa: BLE001
            return {"ok": False, "stopped": "求值器抛了：%s" % e,
                    "can": None, "reason": None, "reason_zh": None}
        if r.get("stopped"):
            return {"ok": False, "stopped": r["stopped"],
                    "can": None, "reason": None, "reason_zh": None}
        out = r.get("out") or {}
        reason = out.get("failReason")
        # 出参名在 SDK 里是 `CanAttack_0`（和函数同名，Dumper-7 加了后缀），
        # 反编译里写作 `canAttack` —— 三种都认一下，认不出就明说。
        can = next((out[k] for k in ("canAttack", "CanAttack", "CanAttack_0")
                    if k in out), None)
        # ★★ 2026-09-23 离线通读 `cardsCheckFunctions.cpp`（静态导出）核实：
        #   `CanAttack` 里**每一条显式赋值**的出口要么拒绝
        #   （`canAttack=false` + 18 键之一），要么放行（`canAttack=true` +
        #   `failReason=""`）；它**唯一**不显式赋值就直接裸 `return;` 的出口，
        #   逐条数下来全部落在"没有更多限制了"的收尾 ——
        #     `Label_3862`：`HasAttackLeft` 为真，直接裸 return（普通单位能打）
        #     `Label_3528`：防守方本身就是战斗机，不用再查战斗机拦截，裸 return
        #   全函数**没有一条**裸 return 落在"拒绝"语义上。⇒ `canAttack` 和
        #   `failReason` **都**没被写，是这条函数自洽的"隐式放行"记号，
        #   不是"没跑到终点"。（`CanSelectAsTarget` 内部同款的两处裸 return 是
        #   `IsValid`/`IsLocatedOnBoard` 失败兜底，正常目标走不到；那条路径
        #   经 `CanAttack` 的 `Label_4911` 转成**显式**赋值 `failReason=""`，
        #   会落进下面②的"自相矛盾"，不会误吃进这里。）
        #   ⚠ 这仍是**推断**，没有拿真实客户端对着这个具体分支复验过 ——
        #   下次开局第一件事就是找一个"无烟幕/无守护/无战斗机拦截/有攻击次数"
        #   的普通目标核对结果，核对不上就撤回。
        if reason is None and can is None:
            return {"ok": True, "stopped": None, "can": True, "reason": "",
                    "reason_zh": REASON_ZH[""], "raw": out,
                    "approx": sorted(set(r.get("approx") or ())
                                      | {"CanAttack:bare-return-implies-pass"})}
        # ★ 一致性护栏：函数的**每一条终止路径**都会给 `failReason` 赋值 ——
        #   合法路径赋 `""`，拒绝路径赋那 18 个键之一（§6.4b 逐行核过）。
        #   所以 `failReason is None`（而 `can` 却被写了）意味着**我们没真正
        #   走到终点**（多半是某个子调用的出参没接上），这时 `can` 是个
        #   **假答案**，不能当结论。宁可报"不知道"，也不要给一个看起来
        #   很像样的错答案。
        # ① 没赋值 ⇒ 没真正走到终点
        if reason is None:
            return {"ok": False, "can": None, "reason": None, "reason_zh": None,
                    "stopped": "跑到头了但 failReason 没被赋值（can=%r）——"
                               "多半是某个子调用的出参没接上，不能当结论" % can,
                    "raw": out}
        # ② **自相矛盾也不能当答案**：`failReason == ""` 是"合法"的写法，
        #    它必须配 `canAttack == true`。出现 `can=False` + `reason=""` 说明
        #    我们在某个分支上算错了（实测是 `CanSelectAsTarget` 的 `can` 没被赋值、
        #    取了零值 False，而它的 `Reason` 同样没赋值、取了零值 ""）。
        #    这种时候给"不能打"或"能打"都是在编 —— 一律报不知道。
        if (reason == "") != bool(can):
            return {"ok": False, "can": None, "reason": None, "reason_zh": None,
                    "stopped": "自相矛盾：canAttack=%r 而 failReason=%r —— "
                               "空 failReason 只该配 canAttack=true。"
                               "多半是 CanSelectAsTarget 的某个分支我们没算对" % (can, reason),
                    "raw": out}
        return {"ok": True, "stopped": None, "can": can, "reason": reason,
                "reason_zh": reason_zh(reason), "raw": out,
                # ★ 这次求值用到的**近似**原语（游戏那边是原生代码，我们重写的）。
                #   非空 = 这条结论里掺了我们自己的判断，不是纯跑游戏的逻辑。
                "approx": r.get("approx") or []}

    # ★ 2026-09-24 F10b 补移动预检：游戏**没有单独的 `CanMove`**（全库搜过
    #   `cardsCheckFunctions.cpp`，没有这个函数），用户点名的"单位不能移动/攻击"
    #   这三类限制——`unit_cant_attack`/`unit_is_pinned`/`deployment_sickness`/
    #   `has_already_attacked`/`no_attack_left`——本来就是 `CanAttack` 在检查
    #   "打谁"之前先查的"这个单位本回合还能不能行动"那一层，跟打谁无关
    #   （百科："被压制的单位不能移动或攻击"，措辞就是两个动词一起说的）。
    #   ⇒ **复用 `can_attack`**，拿场上任意一个敌方单位当探测用的 defender，
    #   只信任这个子集的 `failReason`；defender 专属的原因
    #   （`is_being_garded`/`fighter_protecting`/`not_enough_range`/烟幕）
    #   跟移动没关系，出现这些一律当"能移动"处理。
    MOVE_RELEVANT_REASONS = frozenset((
        "unit_cant_attack", "unit_is_pinned", "deployment_sickness",
        "has_already_attacked", "no_attack_left",
    ))

    def can_move(self, st, unit) -> dict:
        """能不能移动：借 `CanAttack` 的"这个单位本回合还能不能行动"子集判据。

        ⚠ **这不是权威判据**。游戏自己有 `BattleUtilityFunctions_C::CanMoveCardToLocation
        (ECardLocationEnum, __WorldContext, bool* bResult)`——**能注入时直接问游戏**：
        `agent.precheck.can_move_to(card_id, location_enum)`。
        2026-09-25 用户定调：**只留游戏的那个**；这里保留 VM 路径只是为了"不能注入"的场合
        （它跑的是同一份蓝图字节码），并且**不要再往这里加我们自己拍的花费/启发式**。

        ★★ 2026-09-25 晚**第二次更正**（这条注释之前也写错过）：
          旧话"那个游戏函数会看指挥点"**是错的**——把它的字节码全量 dump 之后看清楚了，
          它**一条指挥点检查都没有**（`dump_lib_fn.py`）。它读的是
          `PlayerController->SelectedCard`（"当前正在拖的那张卡"，`// 0x0950`）、
          `SelectedCard.CardLocation`（`// 0x03B0`）、`IsLocationFull`、
          `IsSelectedCardOrder`、`DoesSideControlTheFrontline`。
          没有真实拖拽时 `SelectedCard` 是 `None` ⇒ 它**恒回 False**（实测 6/6 组合）。
          ⇒ `agent/session.can_move` 只信它的 **True**；它的 False 退回这条 VM 路径
          （因为移动的指挥点闸门在提交路径上，不在这个函数里）。

        `stopped` 非空 = 没算出来（缺原语/找不到探测用的敌方目标），
        这时 `can` 是 None——当作"不知道"，不是"不合法"（§7.6f，只挑不判）。
        """
        probe = next((c for c in st.cards
                      if c.obj.side != unit.obj.side and c.obj.IsLocatedOnBoard()),
                     None)
        if probe is None:
            return {"ok": False, "stopped": "场上没有敌方单位可用来探测（CanAttack 需要一个 defender）",
                    "can": None, "reason": None, "reason_zh": None}
        r = self.can_attack(st, unit, probe)
        if not r.get("ok") or r.get("can"):
            return r          # "不知道" 原样传回；能打这个探测目标 ⇒ 更加能动
        reason = r.get("reason")
        if reason in self.MOVE_RELEVANT_REASONS:
            return r          # 这条限制跟"打谁"无关，对移动同样成立
        # defender 专属的拒绝理由（守护/战斗机拦截/射程/烟幕）跟移动无关
        return {"ok": True, "can": True, "reason": "", "reason_zh": REASON_ZH[""],
                "raw": r.get("raw"), "approx": r.get("approx") or [],
                "note": "CanAttack 因目标专属原因(%s)拒绝这次探测，跟移动无关，判定可移动" % reason}

    def can_play_from_hand(self, st, card, target=None) -> dict:
        """出牌预检（F10）：跑这张卡自己的 `CanPlayFromHand` 覆写（蓝图，438 张里的一张才有）。

        跟 `can_attack` 同一套规矩：`stopped`/没有覆写 ⇒ `can=None`（不知道，不拦，
        §7.6f）；`target` 给了就走 `GetTargetedCard` 钩子模拟"玩家当前指向它"——
        跟 `tools/canplay.py::main()` 端到端演示用的同一条路（16/16 已过）。
        """
        ptr = (card.raw or {}).get("ptr")
        if not ptr:
            return {"ok": False, "stopped": "卡对象指针读不到",
                    "can": None, "reason": None, "reason_zh": None}
        from kardsmem.objects import ObjectArray
        from kardsmem import kismet
        oa = ObjectArray(self.s)
        uc = oa.class_of(ptr)
        if not uc:
            return {"ok": False, "stopped": "找不到这张卡的 UClass",
                    "can": None, "reason": None, "reason_zh": None}
        fn = kismet.find_function(self.s, uc, "CanPlayFromHand")
        if not fn:
            return {"ok": False, "stopped": "这张卡没有 CanPlayFromHand 覆写（不在 438 张里）",
                    "can": None, "reason": None, "reason_zh": None}
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools"))
        from canplay import hook_targeted, make_get_field, make_view      # noqa: PLC0415
        from kardsmem.cardnatives import CardNatives
        from kardsmem.vm import VM
        get_field = make_get_field(self.s)
        view = make_view(self.s)
        tptr = (target.raw or {}).get("ptr") if target is not None else 0
        vm = VM(self.s, get_field=get_field, card_natives=CardNatives(None, view=view),
                hooks={"GetTargetedCard": hook_targeted(bool(tptr), tptr)})
        try:
            r = vm.run(fn, self_obj=ptr)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "stopped": "求值器抛了：%s" % e,
                    "can": None, "reason": None, "reason_zh": None}
        if r.get("stopped"):
            return {"ok": False, "stopped": r["stopped"],
                    "can": None, "reason": None, "reason_zh": None}
        o = r.get("out") or {}
        can = o.get("canIt")
        reason = o.get("Reason")
        # ★ 这里的 `Reason` 是 `CanPlayFromHand` 自己的 FString，跟 `CanAttack` 的
        #   18 个 failReason 键不是同一套词表，**不能**拿 `reason_zh()` 硬翻——
        #   那张表是给 CanAttack 专用的。原样透传，没有编中文。
        return {"ok": True, "stopped": None, "can": can, "reason": reason,
                "reason_zh": reason, "approx": r.get("approx") or []}

    def targets(self, st, attacker) -> list:
        """把**全部敌方目标**逐个试一遍 → [(card, 结果)]，按"看起来合法"排前面。

        ★ 这正是 R8「可指向的目标」的做法：内存里没有"玩家当前指向谁"，
          所以只能遍历候选逐个试。算不出来的（`stopped`）**留在列表里**，
          标成"不知道"，不当成不合法剔掉。
        """
        out = []
        for c in st.cards:
            if c.obj.side == attacker.obj.side:
                continue
            if not c.obj.IsLocatedOnBoard():
                continue
            out.append((c, self.can_attack(st, attacker, c)))
        out.sort(key=lambda t: (0 if t[1].get("can") else
                                (1 if t[1].get("stopped") else 2)))
        return out
