#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核后的读侧原语 + 写侧记录型 hook 的断言（离线）。每条都换卡/换 side/换局面，答案必须跟着变。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
from kardsmem.cardnatives import CardNatives
from kardsmem.kismetlib import Unimplemented

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  [%s] %-58s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def card(**kw):
    c = {"card_type": "infantry", "location": "frontline", "side": "local", "side_enum": 1,
         "keywords": [], "custom_name1": "", "custom_name2": "", "received_abilities": [],
         "buffs_from_cards": [], "gameplay_tags": [], "custom_json_keys": [], "custom_json_nums": {},
         "operation_cost": 1, "operation_cost_buff": 0, "kredit": 2, "kredit_buff": 0, "total_kredit_cost": 2,
         "defense": 3, "max_defense": 3, "is_suppressed": False, "is_revealed": True,
         "pinned_turns": 0, "cipher": 0, "exile_nation": 0, "movement_left": 1, "attack_left": 1,
         "choose_one_cards_n": 0, "active_upgrades": [], "heavy_armor": 0, "heavy_armor_buff": 0,
         "total_heavy_armor": 0, "class_fname": "card_unit_x", "is_immune": False,
         "location_enum": 7, "gotcha_activated": 0}
    c.update(kw)
    return c


def reads():
    cn = CardNatives(None, my_seat=1)
    call = lambda n, c, *a: cn.call(n, c, *a)          # noqa: E731
    # 行动费：op+buff，下限 0
    for op, bf, w in ((2, -1, 1), (2, -5, 0), (2, 3, 5)):
        chk("getTotalOperationCost(%d,%d)" % (op, bf), call("getTotalOperationCost", card(operation_cost=op, operation_cost_buff=bf)), w)
    # 费用：下限 0 上限 99
    for k, w in ((-2, 0), (5, 5), (150, 99)):
        chk("getTotalKreditCost(%d)" % k, call("getTotalKreditCost", card(total_kredit_cost=k)), w)
    # isAlso*
    cav = card(custom_name1="isAlsoTank;CanMoveAndAttackInTheSameTurn")
    chk("IsTank 步兵+isAlsoTank", call("IsTank", cav), True)
    chk("IsTank 纯步兵", call("IsTank", card()), False)
    chk("IsTank 坦克", call("IsTank", card(card_type="tank")), True)
    chk("IsFighter 步兵+isAlsoTank", call("IsFighter", cav), False)
    chk("IsFighter fighter+isAlsoFighter 不需要", call("IsFighter", card(card_type="fighter")), True)
    chk("CustomName1HasAttribute 整段(CanMove…)", call("CustomName1HasAttribute", cav, "CanMoveAndAttackInTheSameTurn"), True)
    chk("CustomName1HasAttribute 前缀不算(isAlso)", call("CustomName1HasAttribute", cav, "isAlso"), False)
    chk("CustomName1HasAttribute 空属性", call("CustomName1HasAttribute", cav, ""), False)
    chk("CanMoveAndAttack 骑兵", call("CanMoveAndAttackInTheSameTurn", cav), True)
    chk("CanMoveAndAttack 坦克", call("CanMoveAndAttackInTheSameTurn", card(card_type="tank")), True)
    chk("CanMoveAndAttack 步兵", call("CanMoveAndAttackInTheSameTurn", card()), False)
    chk("IsArmorUnit 歼击车", call("IsArmorUnit", card(card_type="tankdestroyer")), True)
    chk("IsGunUnit 反坦克炮 / 步兵", (call("IsGunUnit", card(card_type="antitank")), call("IsGunUnit", card())), (True, False))
    chk("GetCustomName2Attributes", call("GetCustomName2Attributes", card(custom_name2="a;;b;None")), ["a", "b"])
    # 缺 customName 且类型不符 ⇒ 抛
    try:
        c0 = card(); del c0["custom_name1"]
        call("IsTank", c0)
        chk("缺 custom_name1 应抛", "没抛", "抛")
    except Unimplemented:
        chk("缺 custom_name1 抛 Unimplemented", True, True)
    # 关键词：flag OR received
    chk("getHasBlitz 模板有", call("getHasBlitz", card(keywords=["has_blitz"])), True)
    chk("getHasBlitz 被贴", call("getHasBlitz", card(received_abilities=[{"ability": "blitz", "givers": [7]}])), True)
    chk("getHasBlitz 给予者空", call("getHasBlitz", card(received_abilities=[{"ability": "blitz", "givers": []}])), False)
    chk("getHasShock 被贴 blitz 不算", call("getHasShock", card(received_abilities=[{"ability": "blitz", "givers": [7]}])), False)
    chk("getHasImmune flag / 被贴 / 都无",
        (call("getHasImmune", card(is_immune=True)),
         call("getHasImmune", card(received_abilities=[{"ability": "immune", "givers": [3]}])),
         call("getHasImmune", card())), (True, True, False))
    chk("GetCombatKeywords 顺序", call("GetCombatKeywords", card(
        keywords=["has_blitz", "has_guard", "has_ambush"], heavy_armor=2, total_heavy_armor=2)), ([1, 2, 4, 5], 4))
    chk("GetCombatKeywords shock+smoke", call("GetCombatKeywords", card(keywords=["has_shock", "has_smokescreen"])), ([6, 7], 2))
    # 临时 buff
    bf = [{"giver_card_id": 7, "buffs": {"attack_tempBuffGive": 2}}, {"giver_card_id": 9, "buffs": {"attack_tempBuffGive": 3, "kredit_tempBuffGive": 1}}]
    chk("getAttackTempBuffAmount 7/9/8", [call("getAttackTempBuffAmount", card(buffs_from_cards=bf), i) for i in (7, 9, 8)], [2, 3, 0])
    chk("getKreditTempBuffAmount 9/7", [call("getKreditTempBuffAmount", card(buffs_from_cards=bf), i) for i in (9, 7)], [1, 0])
    # 压制 / 限制 / 能力
    chk("IsPinned pinned_turns", (call("IsPinned", card(pinned_turns=2)), call("IsPinned", card(pinned_turns=0))), (True, False))
    chk("HasCantAttack customName1", call("HasCantAttack", card(custom_name1="cantAttack")), True)
    chk("HasCantAttack 能力", call("HasCantAttack", card(received_abilities=[{"ability": "cantAttack", "givers": [1]}])), True)
    chk("HasCantAttack 无", call("HasCantAttack", card()), False)
    chk("HasCantAttackType ground", call("HasCantAttackType", card(custom_name1="cantAttack:ground"), "ground"), True)
    chk("HasCantAttackType air≠ground", call("HasCantAttackType", card(custom_name1="cantAttack:ground"), "air"), False)
    chk("HasCantBeAttackedBy", call("HasCantBeAttackedBy", card(custom_name1="cantBeAttackedBy:air"), "air"), True)
    ra = [{"ability": "passive", "givers": [4, 5]}]
    chk("HasCustomAbilityFromCard 4/6", (call("HasCustomAbilityFromCard", card(received_abilities=ra), "passive", 4),
                                         call("HasCustomAbilityFromCard", card(received_abilities=ra), "passive", 6)), (True, False))
    # 其它状态
    chk("IsVeteran 有键/无键/被抑制", (call("IsVeteran", card(custom_json_keys=["veteran"])), call("IsVeteran", card()),
                                    call("IsVeteran", card(custom_json_keys=["veteran"], is_suppressed=True))), (True, False, False))
    chk("IsVeteran ignoreSuppress", call("IsVeteran", card(custom_json_keys=["veteran"], is_suppressed=True), True), True)
    chk("IsDamaged", (call("IsDamaged", card(defense=1, max_defense=3)), call("IsDamaged", card(defense=3, max_defense=3))), (True, False))
    chk("HasMovementLeft", (call("HasMovementLeft", card(movement_left=1)), call("HasMovementLeft", card(movement_left=0))), (True, False))
    chk("HasIntel/getIntel", (call("HasIntel", card(cipher=3)), call("getIntel", card(cipher=15)), call("HasIntel", card())), (True, 9, False))
    chk("IsExile", (call("IsExile", card(exile_nation=3)), call("IsExile", card())), (True, False))
    chk("IsChooseOneCard", (call("IsChooseOneCard", card(choose_one_cards_n=2)), call("IsChooseOneCard", card())), (True, False))
    chk("IsWeatherCard rain/infantry", (call("IsWeatherCard", card(gameplay_tags=["subtype.rain"])),
                                       call("IsWeatherCard", card(gameplay_tags=["subtype.infantry"]))), (True, False))
    chk("IsForecastCard", (call("IsForecastCard", card(class_fname="card_event_rain1_mist")),
                          call("IsForecastCard", card(class_fname="card_event_rain2_deluge"))), (True, False))
    chk("HasBond 有tag/被移除", (call("HasBond", card(gameplay_tags=["ability.bond"])),
                               call("HasBond", card(gameplay_tags=["ability.bond"], received_abilities=[{"ability": "bond_removed", "givers": [1]}]))), (True, False))
    chk("countdown 有/无", (call("hasActiveCountdownEffect", card(custom_json_keys=["countdown_timer"])),
                          call("getCountdownValue", card(custom_json_keys=["countdown_timer"], custom_json_nums={"countdown_timer": 3.0})),
                          call("getCountdownValue", card())), (True, 3, -1))
    chk("pincer", (call("hasActivePincerEffect", card(custom_json_keys=["pincer_receiver"])), call("hasActivePincerEffect", card())), (True, False))
    chk("HasCampaignUpgrade [1,3]", [call("HasCampaignUpgrade", card(active_upgrades=[1, 3]), i) for i in (1, 2, 3)], [True, False, True])
    chk("CanBeTargetted 默认 True", call("CanBeTargetted", card(keywords=["has_smokescreen"])), (True, "", "", ""))
    cn2 = CardNatives(None, my_seat=1)
    cn2.static_cards = {"card_unit_x_vet"}
    chk("getHasVeteranUpgrade 有/无", (cn2.call("getHasVeteranUpgrade", card()), cn2.call("getHasVeteranUpgrade", card(class_fname="card_unit_y"))), (True, False))

    # 库函数：EnumCompare（Equal=0/NotEqual=1）、静态卡表（注入）、小写数学名
    for n in ("EnumCompareSide", "EnumCompareCardLocation", "EnumCompareFaction", "EnumCompareType"):
        chk("%s(a,a)=0 / (a,b)=1" % n, (cn.call(n, None, 1, 1), cn.call(n, None, 1, 2)), (0, 1))
    chk("EnumCompareFaction 枚举名带前缀", cn.call("EnumCompareFaction", None, "EFactionEnum::Germany", "Germany"), 0)
    cn.static_card = lambda n: {"x": {"card_type": "tank", "kredits_plain": 4, "faction_enum": 2, "rarity_enum": 1},
                                "y": {"card_type": "order", "kredits_plain": 2, "faction_enum": 5, "rarity_enum": 3}}.get(n)
    chk("GetStatic* x/y 随卡变", [(cn.call("GetStaticType", None, k), cn.call("GetStaticKredits", None, k), cn.call("GetStaticFaction", None, k))
                               for k in ("X", "y")], [(3, 4, 2), (2, 2, 5)])
    chk("StaticCardExists", (cn.call("StaticCardExists", None, "x"), cn.call("StaticCardExists", None, "zzz")), (True, False))
    cn.static_card = lambda n: {"a_vet": {"card_type": "tank", "card_set_enum": 4, "exile_nation": 2}}.get(n)
    chk("getStaticVeteranUpgrade / GetStaticCardSet / ExileFaction 随卡变",
        (cn.call("getStaticVeteranUpgrade", card(class_fname="a"))["card_type"], cn.call("GetStaticCardSet", None, "A_VET"),
         cn.call("GetStaticExileFaction", None, "a_vet")), ("tank", 4, 2))
    chk("GetFactionEnum USA/Germany/未知", (cn.call("GetFactionEnum", None, "EFactionEnum::USA"), cn.call("GetFactionEnum", None, "germany"),
                                           cn.call("GetFactionEnum", None, "zzz")), (5, 1, 0))
    cn.view = lambda p: {"location_number": {1: 5, 2: 1, 3: 3}[p]}
    chk("SortCardsByLocationNumber", cn.call("SortCardsByLocationNumber", None, [1, 2, 3]), [2, 3, 1])
    cn.view = None
    cn.static_card = None
    from kardsmem import kismetlib as K
    chk("MatchesTag 层级/精确", (K.call("BlueprintGameplayTagLibrary::MatchesTag", ["subtype.rain.x", "subtype.rain", False]),
                              K.call("BlueprintGameplayTagLibrary::MatchesTag", ["subtype.rain.x", "subtype.rain", True])), (True, False))
    S = lambda n, a: K.call("KismetStringLibrary::" + n, a)       # noqa: E731
    chk("Contains 默认忽略大小写 / bUseCase=true", (S("Contains", ["Hello", "ELL", False, False]), S("Contains", ["Hello", "ELL", True, False])), (True, False))
    chk("StartsWith/EndsWith SearchCase 0=敏感 1=忽略", (S("StartsWith", ["Hello", "he", 1]), S("StartsWith", ["Hello", "he", 0]),
                                                      S("EndsWith", ["Hello", "LO", 1]), S("EndsWith", ["Hello", "LO", 0])), (True, False, True, False))
    chk("Replace 忽略/敏感", (S("Replace", ["aXbx", "x", "-", 1]), S("Replace", ["aXbx", "x", "-", 0])), ("a-b-", "aXb-"))
    chk("Right n>len / 0 / 2", (S("Right", ["abc", 5]), S("Right", ["abc", 0]), S("Right", ["abc", 2])), ("abc", "", "bc"))
    chk("MinOfIntArray/MapRangeClamped", (K.call("KismetMathLibrary::MinOfIntArray", [[4, 2, 9]]),
                                        K.call("KismetMathLibrary::MapRangeClamped", [5, 0, 10, 0, 100])), ((1, 2), 50.0))
    chk("kismetlib 小写 abs/max/min/round", (K.call("KismetMathLibrary::abs", [-3.5]), K.call("KismetMathLibrary::max", [2, 5]),
                                          K.call("KismetMathLibrary::min", [2, 5]), K.call("KismetMathLibrary::round", [2.5])), (3.5, 5, 2, 3))
    chk("SelectString/NOR/InRange", (K.call("KismetMathLibrary::SelectString", ["a", "b", True]), K.call("KismetMathLibrary::BooleanNOR", [False, False]),
                                    K.call("KismetMathLibrary::InRange_IntInt", [5, 1, 5, True, False])), ("a", True, False))


def writes():
    from semantics import effectvm as E
    from policy import boardeval as B
    SELF, TGT = 0x2000, 0x1000
    kr = {"local": 5, "enemy": 3}

    def run(side_args, verb, kredits=kr, my=1):
        r = E.Recorder(SELF, TGT)
        r.hook(verb)(None, None, None, list(side_args), None)
        return E.to_effects(r, my, None, kredits)
    # setKreditBySide 绝对值 → 增量；换 side / 换当前值，答案跟着变
    chk("setKreditBySide(1,→8) 当前5 ⇒ +3", run((1, 8, 0, 0, 0), "setKreditBySide").get("kredit"), 3)
    chk("setKreditBySide(1,→2) 当前5 ⇒ -3", run((1, 2, 0, 0, 0), "setKreditBySide").get("kredit"), -3)
    chk("setKreditBySide(2,→4) 敌方当前3 ⇒ opp +1", run((2, 4, 0, 0, 0), "setKreditBySide").get("opp_kredit"), 1)
    chk("setKreditBySide 换局面(当前9)", run((1, 8, 0, 0, 0), "setKreditBySide", {"local": 9, "enemy": 3}).get("kredit"), -1)
    chk("setKreditBySide 不知道当前值 ⇒ 不猜", "kredit" in run((1, 8, 0, 0, 0), "setKreditBySide", None), False)
    chk("ChangeKreditsBySide(1,+2)/(2,-1)", (run((1, 2, 0), "ChangeKreditsBySide").get("kredit"), run((2, -1, 0), "ChangeKreditsBySide").get("opp_kredit")), (2, -1))
    chk("SetPlayingSide(1)/(2) my=1", (run((1,), "SetPlayingSide").get("playing_side"), run((2,), "SetPlayingSide").get("playing_side")), ("local", "enemy"))
    chk("SetActiveSide(2) my=2 ⇒ local", run((2,), "SetActiveSide", my=2).get("playing_side"), "local")
    chk("DiscardCardFromHand 记 id", run((77, 5, False, False, None), "DiscardCardFromHand").get("discard_ids"), [77])
    chk("DrawSpecific side=1 ⇒ draw 1 / side=2 ⇒ opp_draw", (run((5, 9, 1, True), "DrawSpecificCardFromDeckBySide").get("draw"),
                                                               run((5, 9, 2, True), "DrawSpecificCardFromDeckBySide").get("opp_draw")), (1, 1))
    r = E.Recorder(SELF, TGT)
    r.hook("CustomName1Add")(None, None, None, [SELF, "isAlsoTank"], None)
    chk("RECORD_ONLY：记录但不进效果摘要", (len(r.records), E.to_effects(r, 1)), (1, {}))
    # boardeval 消费
    LO, EN = B.LOCAL, B.ENEMY
    h = {50: B.H(50, "X", 1, "order"), 51: B.H(51, "Y", 2, "infantry", 2, 2)}
    s0 = B.Sim({}, {LO: 20, EN: 20}, 3.0, h)
    s1 = s0.copy(); B._apply_eff(s1, {"opp_kredit": 2}, None)
    chk("消费 opp_kredit：对手指挥点+2 ⇒ 评估下降(kred_w>0时)", (s1.opp_kredits, B.evaluate(s1) <= B.evaluate(s0)), (2, True))
    s2 = s0.copy(); B._apply_eff(s2, {"opp_slot": -1}, None)
    chk("消费 opp_slot -1 ⇒ 评估上升", (s2.opp_slots, B.evaluate(s2) > B.evaluate(s0)), (-1, True))
    s3 = s0.copy(); B._apply_eff(s3, {"discard_ids": [50]}, None)
    chk("消费 discard_ids：手牌 50 被移走、51 还在", (sorted(s3.hand), len(s0.hand)), ([51], 2))
    s4 = s0.copy(); B._apply_eff(s4, {"discard_ids": [999]}, None)
    chk("discard 不在己方手牌的 id 不影响", sorted(s4.hand), [50, 51])
    s5 = s0.copy(); n0 = len(B.gen_actions(s5)); B._apply_eff(s5, {"playing_side": EN}, None)
    chk("消费 playing_side=enemy：之后没有我方动作", (n0 > 0, B.gen_actions(s5)), (True, []))
    s6 = s0.copy(); B._apply_eff(s6, {"playing_side": LO}, None)
    chk("playing_side=local：动作照旧", len(B.gen_actions(s6)), n0)
    chk("copy 保留新字段", (s5.copy().playing_side, s1.copy().opp_kredits), (EN, 2))


def main():
    reads()
    writes()
    print("test_native_reads_writes: %s（%d 项失败）" % ("PASS" if not bad else "FAIL", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
