# P3 工单：natives 全量移植（`engine/natives/`）

> 对应 `docs/REFACTOR-PLAN.md` §2 的 **P3**：「按被卡牌脚本实际调用的频次从高到低移植」。
> 验收（计划书原文）：**每个 native 有「原版：函数名（文件:行）」注释；`SIM-FIDELITY.md` 表里「近似」清零**。
> 本文件是**活的工单**：每落一族就更新；缺口写在这里，不写进代码当"大概是这样"。
> 2026-10-03 出第一版，含一次**只读调研**（子代理）+ 主 agent 逐项复核（复核过的才写进来，没复核的标「未核」）。

## 0. 口径修正（**先看这条，它改了 P3 的起点**）

计划书 P3 点名要移植 `ChangeAttack/ChangeDefense/ChangeOperationCost`、`DamageCard/ApplyDamageToCard`、
`DestroyCard`、`MoveCardToFrontline`、`ConvertCard`、`MakeCardRetreat`、`TakeControl` ——
**这些是 BP 函数（有字节码），不是"原生"（exe 里的 `UFunction::Func` 指针）**。实测在原生普查报告里
几乎查不到（`Select-String -SimpleMatch` 计数）：

| 名字 | `NATIVE-COVERAGE-1.60.md` | `NATIVE-SPEC-GAPS-1.60.md` |
|---|---|---|
| `ChangeAttack` | 1（§14.1 的"BP 动词"句） | 1 |
| `ChangeDefense` | 0 | 2 |
| `ChangeOperationCost` / `ApplyDamageToCard` / `DestroyCard` / `MoveCardToFrontline` / `MakeCardRetreat` / `SpawnCardToBoard` | **0** | 0（`DamageCard` 3、`ConvertCard` 2、`TakeControl` 3） |

⇒ **两份报告给不出 P3 清单的顺序**；它们排的是**读器/叶子原生**（`IsLocatedOnBoard` 819×480、
`GetOppositeSide` 760×462…），而那些**现在能跑对**（`kardsmem/cardnatives.py` 已被 §14.2 逐条复核过）。
所以：**P3 的顺序自己定**（见 §1），报告的频次表用来**验收读侧**，不是用来排写侧。

另外：`engine/natives/` 这个**包名里的"原生"是宽口径**（= "游戏自己的规则函数"，BP 动词 + exe 叶子都算）。
`board.py`/`status.py`/`cards.py` 给出的出处全是 BP 行号，与包名不严格自洽 —— **不改包名**（改名成本大、
且 `engine/natives/` 已经在计划书里定了名），但本工单统一用「规则函数」这个词，别再纠结 BP/native。

## 1. 判据与做法（护栏）

1. **原版为准，一手优先**：`reverse-data/exports-1.60.27292.launcher-only-decompiled-BP/…`（FModel 导出的 BP，
   函数名 + 行号）> IDA 原生函数（地址）> SDK dump（字段偏移/枚举）。**没有出处就不写规则**（`SIM-FIDELITY.md` §1）。
2. **一族一刀**：一个族一个提交、只碰一个族；每刀跑全量 `tests/run_all.py`。
3. **先读原版再写 Python**：把读到的**分支表**贴进模块 docstring（带行号），代码只是一行行照抄。
4. **不编数**：读不出 ⇒ 记缺口 + 价值 0（同类教训）。
5. **不偷偷改行为**：建骨架/收敛判定的那一刀**必须行为零变化**，由现有测试钉住；行为差异**先记缺口**，
   等有游戏可实机验证时再动（离线测不出行为回归）。
6. **能问游戏就别重写**：判据类（`CanAttack`/`CanPlayFromHand`）在实机侧一律问游戏（`ops.inject`）；
   `engine` 里的移植只服务**盘外模拟**。

## 2. 工单

### 2.1 顺序（复核后的结论；理由见 §0）

写侧先行——它们是 P4「scripts 直跑」的前置，也是现在**唯一还活在"字典黑话"里**的部分
（`engine/effectvm.py` 把写类调用压成 `buff`/`buff_ids`/`retreat_ids`，`sim` 再解释回来）。

| 序 | 族 | 原版依据 | 现状 | 状态 |
|---|---|---|---|---|
| **①** | **卡面数值 + `EChangeType`**：`ChangeAttack`/`ChangeDefense`/`ChangeHeavyArmor`/`ChangeOperationCost` | `BP_CardFunctions.cpp:10981 / :11215 / :10320 / :8892`；`EChangeType`（SDK dump） | 四个函数**全部逐行读完**（4 张分支表 + 4 张 `switch` 表 + `BP_FUNC_RANGES` 在 `engine/natives/stats.py`；23 条出处行号由测试逐条核对）；判定/夹取单一来源；**0/4 的独立 buff 字段已落地**（`U.atk_buff` / `H.cost_buff` + 适配器读 `attackBuff`/`kreditsBuff`；重甲另有 `Card.total_heavy_armor`/`heavy_armor_buff`，2026-10-04 补，见 §4 R3）；**两道门已接**（`IsUnrevealedCovertCard` :8906/:10332；`CanCardBeBuffed` :23631 逐行读后接到 Attack/Defense/KreditCost）⇒ **值语义闭环**；剩通知链 | **已读，接线完成（值语义）** |
| ② | **写侧叶子**：`setAndEncrypt{Attack,AttackBuff,Defense,Kredit,KreditBuff}`、`CustomName1/2 Add/Remove` | `NATIVE-COVERAGE` §14.1（:578-590）+ §14.4（:749/:806/:784/:818/:819/:682/:700/:708/:736） | `engine/effectvm.py:217-222` 的 `RECORD_ONLY`：**只记进 `rec.records`，不进摘要、不执行**；其中 5 条 `setAndEncrypt*` 已消费（影子 + `set_*` 键，R3/R10）；★ **2026-10-04 结论**：剩下的 `CustomName1/2*`/`ApplyGameplayEffect`/`RemoveGameplayEffectByInstigator`/`CopyData`/`CreateCopy`/`ResetCardAttributes` **不该再补 eff 键**（`EVAL-ARCHITECTURE` §重构范围 第 1 条明令禁），应随 **P4（scripts 直跑）** 直接写成 `sim` 状态变更 ⇒ **②在 P3 内的值语义部分已闭环，其余移交 P4** | 部分已做（余移交 P4） |
| ③ | **`DamageCard` → `ApplyDamageToCard`**（含 excess 溢出、`ExecuteBeforeReceiveDamage` 顺序） | BP（`DamageCard` `:895-955`、`ApplyDamageToCard` `:16375-16566`，2026-10-04 读过） | ★ **2026-10-04 复核：主体其实已经照原版移植过**（旧状态"待做"过时）—— `engine/triggers.py:2144 run_damage_card` 逐条对应 `DamageCard`：`!isRedirected ⇒ ExecuteOnDealDamageAddDamage`（`:2173`，重定向则跳过、直接用原量，与原版 `Label_690`/`Label_190` 一致）→ `after_calc`（`:2175`）→ `apply_damage`（`:2176`，`ApplyDamageToCard` 的钩子段）；"不在场 ⇒ 什么都不做"那条也有（原版 `Label_425` 只多打一行 PrintString）。**真残项 = `excess` 自定义能力**：来源带 `HasCustomAbility("excess")` 时把"超过目标总防的部分"（`excessDamage`）**转打敌方总部**（`DamageCard(GetLocationCardBySide(...), excessDamage, …)`，`:16489-16498`）；分支在 `:16414-16446`（先看来源 `IsUnit`，再看"`finalDamage > 目标总防` ∧ 目标 `IsUnit`"才拆）。**`Label_1540/1304/1775` 的函数体还没读 ⇒ 不猜**（下一步读完再实现）。★ **2026-10-04 定案（只读反汇编）**：`probe_excess_asm.py` 把 `ExecuteAttackCard`（623 行）反汇编出来 —— excess 拆分后**直接落下**到 `provideKeysAndFrameCount → getTotalDefense → Subtract_IntInt → setAndEncryptDefense` ⇒ **伤害照常应用**；FModel 导出里那句 `goto Label_3496`（`:17244`）是**反编译 artifact**（弯路 #4 又一次）。语义（两处同构：`ExecuteAttackCard` `:17218-17244` / `ApplyDamageToCard` `:16414-16446`）：来源带 `excess` ∧ 目标 `IsUnit` ∧ `finalDamage > 目标总防` ⇒ `ExcessDamage = 差`、打目标伤害**封顶到总防**；随后 `ExcessDamage > 0` ⇒ `DamageCard(敌方总部, ExcessDamage, 来源, false, false, false)`（`:16489-16498`）。**待实现**：excess 我们完全没建模，缺一条"读自定义能力"的数据通路（快照 `Card` 不带能力字段）；同段还看到 `lethal` 能力（offset 1973+），另记 | 主体已做；excess 待实现 |
| ④ | **`DestroyCard` / `MakeCardsFight` 复核** | BP（`MakeCardsFight` 的 BP 体报告说"未读"，代码说"读过"——**口径冲突，未核**） | `engine/natives/damage.py`（已移植）、`sim/engine.py` 的 `destroy`/`destroy_aoe` | 待做 |
| ⑤ | **`MoveCardToFrontline` / `MakeCardRetreat` / `ConvertCard` / `TakeControl` / `MakeVeteran` / `FullyHealCard`** | BP（`sim/engine.py` 各自的注释已带出处） | ★ **2026-10-04 逐行核对完五个**（结论：我们那边的断言都对得上，无需改代码）：① `TakeControlOfEnemyUnit`(`:2471-2480`) → `ChangeUnitOwnership`(`:18087-18341`)：`side=toSide`(:18183)、`underEnemyControl=true`(:18185)、`movementLeft=1`(:18187)、**`hasFury ? attackLeft=2(:18189-18194) : 1(:18333)`**、新落点 `GetSupportLineBySide(toSide)`(:18161)、`ExecuteOnBeforeLeaveBoardOrOwnerEvents`(:18180) —— 我们 `steal` 的注释与实现逐条一致；② `MakeVeteran`(`:7127-7300`)：门 = `getTotalDefense()>0` ∧ `!IsVeteran`(:7135-7141)，随后 **四处 `ChangeX(…, 0x5)`**（`ChangeAttack` :7178 / `ChangeDefense` :7182 / `ChangeOperationCost` :7184 / `ChangeHeavyArmor` :7188）+ `HasAttackLeft`(:7162)；③ `FullyHealCard`(`:701-852`)：门 = 总防>0(:710) ∧ 缺口>0(:717)，`OnBeforeFullyRepaired`→`stopAction` 可否决(:757-759)，`setAndEncryptDefense(maxDefense)`(:776)，`OnOtherCardFullyRepaired`(:813)/`OnFullyRepaired`(:831)；④ `MoveCardToFrontline`(`:17560-17632`)：`CanMoveAndAttackInTheSameTurn` 假 ⇒ `movementLeft-1`(:17596-17598) 且 `attackLeft=0`(:17624)。★ **`MakeCardRetreat` 抓到一处真缺口并已修**：原版先逐张 `HasCustomAbility("cantRetreat")` **跳过**(`:1727-1735`) 再分支援线/前线两批 `ApplyMakeCardRetreat`(:1742/:1754)，而我们的 `effectvm` 造 `retreat_ids` 时**没滤这道门** ⇒ 会把带 `cantRetreat` 的牌也撤（**多撤**）。修法：`_fill_cur_stats` 把 `received_abilities` 一并算成 `abilities`/`cant_retreat`（与 `excess` 共用同一张表，权威读法 `cardnatives._ability_has = received_abilities[name]>0`），`retreat_ids` 构造时过滤；全部被挡 ⇒ 什么都不产出且**不记缺口**（原版就是什么都不做）。`tests/test_retreat_cant.py` 5 项 | ✅ 已核（含一处修复） |
| ⑥ | **攻击合法性**：`can_hit_unit`/`can_hit_hq`/`_rows_ok` | `cardsCheckFunctions::CanAttack`（`:185-204`/`:206-242`/`:244-296`）+ 外部 VM（`semantics/legality.py`） | ★ **2026-10-04 完成**：换成原版规则并把 `SIM-FIDELITY` 那条改成 **忠实** —— ① `not_enough_range`（`range<2` ⇒ 必须自己在前线或打前线目标；`U.rng` 从 `BaseCardObject::range @0x78`（SDK `kards_classes.hpp:2439`）读，快照 `CARD_I32`/raw 都已接）；② 护卫豁免 **`IsBomber()‖IsArtillery()`**（旧实现只豁免炮兵 ⇒ 轰炸机打被护卫目标/总部被**误拒**）；③ `fighter_protecting` **只对轰炸机**生效且看**被攻击那一行**（旧实现看"敌方后排"⇒ 行取错，打前线单位时误拦/漏拦）。测试 `tests/test_attack_legality.py` 14 项（每条判据都给两个会产生不同结论的输入）。**未建模（如实记，见 SIM-FIDELITY）**：HQ 的 `location_has_smokescreen`、未揭示隐蔽战斗机的豁免、`IsThereGameplayRestriction(cant_attack_with_ground_units)`、`cant_be_attacked_by_unit`/`CanSelectAsTarget` | ✅ 完成 |
| ⑦ | **攻击结算**：`sim_attack` | `AttackCard`(`:16771`) → `CalculateDamageDealt`(`:14809-15167`) → `ExecuteAttackCard`(`:17133+`) → `DamageCard`（重甲/冲击/伏击/反击/`OnAfterAttack`） | 进行中（2026-10-04，**链路已完全定位**）：① 真正的结算入口是 **`AttackCard`**(`:16771`)，尾部(`:17064-17111`)给出确切顺序：`CalculateDamageDealt(攻击方→防守方)` 判防守方死 → `CalculateDamageDealt(防守方→攻击方)` **并出 `wasShockAttack`** → 两个方向的 `ExecuteOnDealDamageAddDamageAfterCalc` → `ExecuteAttackCard` → `ExecuteOnAfterAttackEvents`；`ExecuteAttackCard` 的两个伤害都是**入参**（`:17133` 签名），所以伏击/冲击不在这里。② 逻辑在 **`CalculateDamageDealt`**(`:14809-15167`)：先算**两个方向**的攻击值（`:14845-14863`，各带 `ExecuteOnDealDamageAddDamage`），然后逐条改写。③ **伏击条件**（`:14868-14949`）：`receiver.getHasAmbush() ∧ ¬ignoreAmbush ∧ ¬dealer.getHasImmune() ∧ dealingDamageIsAttacker ∧ ¬receiver.hasBeenAttackedThisTurn` ⇒ `_dealerCalculatedDamage = 0`；例外分支：炮兵伤害方(`:14890`)、轰炸机打非战斗机/非防空(`:14900-14915`)、防守方是轰炸机或伤害方有冲击(`:14918-14927`)；随后比 `receiver 攻击 ≥ dealer.防御+重甲+GetPassiveDefenseBuff+beforeAttackBuff`(`:14930-14949`)。★ **我们 fallback 的真缺口**：完全没有 `¬receiver.hasBeenAttackedThisTurn` 这一条 ⇒ **同一回合的第二次攻击也会被伏击吞掉**（快照里有 `has_been_attacked_this_turn @0x280`，`CanAttack` 侧也在用）。④ **冲击**：`:15010-15023` = `receiver.getHasShock() ∧ ¬ignoreShock ∧ dealer.IsUnit()` ⇒ `_dealerCalculatedDamage = 0` + `shockAttack = true`（**是"防守方有冲击 ⇒ 攻击方这一击打不出伤害"**，与"攻击方有冲击 ⇒ 无还击"**不是**同一条 —— 我们 fallback 里只写了后者）。⑤ `lethal` 能力(`:15077-15087`)、重甲(`:15033-15047`，`ignoreHeavyArmor` 开关)。⑥ `wasShockAttack` 在 `ExecuteAttackCard` 里**只被读一次**(offset 2870) 且是作 `NotifyAttackCard(...)` 的**入参**（不是压制还击的开关）——所以"冲击 ⇒ 无还击"必须落在 `CalculateDamageDealt` 的调用方/参数上，待下一轮定位。字节码 dump：`dump_execute_attack_card.txt`。★ **2026-10-04 已落地这一段**：`U.been_attacked`（快照 `has_been_attacked_this_turn @0x280`，两条攻击路径都置位）、伏击门补 `¬been_attacked`（以前**漏了** ⇒ 同回合第二次攻击也被吞）、致死判据加**重甲**（原版 `:14936-14946`）、报复豁免**方向改正**（原版 `:14955-15005` 的豁免看的是**攻击方**＝还击方向的 receiver：炮兵攻击者不吃、**轰炸机攻击者不吃但防守方是战斗机/防空时照常吃**）。测试 `tests/test_ambush_gate.py` 7 项、`tests/test_lethal_shock.py` 5 项、`tests/test_immune_no_damage.py` 8 项。★ **`isImmune` 的正确语义（用户 2026-10-04 当场纠正）**：**免疫单位不吃伤害、也不会死** —— 原版三处一致：`CalculateDamageDealt` `:14823-14836`（开头查 `_damageRecieverCard->getHasImmune()` ⇒ `damage=0; doesDamageRecieverDie=false; wasShockAttack=false; return`）、`ExecuteOnDealDamageAddDamageAfterCalc` `:14568-14575`（⇒ `finalDamage=0`）、群体伤害 `:15698-15703`（**直接跳过**免疫目标）。落点：`engine/natives/damage.py::deal_damage`（免疫 ⇒ 直接返回）+ `sim_attack`（钩子管线扣血 / 还击 / `excess` 溢出 / `lethal` 斩杀四处都看免疫）。快照接上 `isImmune @0x289`（SDK `kards_classes.hpp:2514`）。**仍未建模（如实记）**：`antiair` 类型、HQ 的免疫/烟幕、`GetPassiveDefenseBuff`（战斗调用点传 `applyBeforeAttackBuffs=false` ⇒ **不影响**；`BP_Logic` 那两处传 `true` 的是**预览**路径） | 进行中（伏击/报复/lethal/免疫已落） |
| ⑧ | **回合开始/结束**：`sim_turn_end` / `sim_turn_start` | `ExecuteEndOfTurnEvents`(`:13081`)+`ExecuteEndOfTurnQueue`(`:13129`)、`RemoveBuffsEndOfTurn`(`:21680`)、回合开始流程 `BP_Logic:10010-10077` | ★ **2026-10-04 完成**：① 回合结束**核实** —— 先 0x19 钩子、最后清 buff（`RemoveBuffsEndOfTurn` 在 Queue 内 `:13247`），而"要清的 buff"全导出**只有一种**（唯一登记点 `AddAttackUntilEndOfTurn` `:2992` → `AddBuffsToRemoveEndOfTurn(0x0,…)` `:3006`）⇒ 清临时攻 ✓ 与我们 `atk_turn` 一致；② **新增 `sim_turn_start`**（照 `BP_Logic:10010-10077`）：槽 +1（`CanSideGainKreditSlots` `BP_Logic:7999` ∧ `slot<MaxKreditsConst`）→ `SetKreditsAndKreditSlots(side,slot,slot,0)`（`:19543`）⇒ **指挥点补满到槽数** → before-start 钩子 → `CanSideDrawCards` 抽 1（**第 1 回合不抽** `:6318-6336`）→ mobilize → start 钩子；③ 旧行"指挥点补给/抽牌/疲劳**按经验**"**过时**：抽牌/疲劳早就是原生移植（`draw_chain`/`apply_fatigue`），只有补给没落、现已补。测试 `tests/test_turn_start.py` 5 项（槽+1/补满/上限/首回合不抽/次回合抽）；`SIM-FIDELITY` 该行改 **已按原版** 并列清未建模（`GiveMobilizeBonus`/`KreditCheckAndAutoBanIfNeeded`/`MaxKreditsConst` 真值/③⑥ 钩子后果待 rule 预计算） | ✅ 完成 |
| ⑨ | **移动/上线费用与禁令** | `MoveCardToFrontline`/`PayMovementCost`/`CanMoveCardToLocation` | ★ **2026-10-04 完成**：逐行核 `MoveCardToFrontline`(`:17560-17629`) ⇒ ① 费用 = `getTotalOperationCost()`(`:17617-17619`) 且先过 `PayMovementCost`(`:19873-19898`) 的"够不够"闸门（不够 ⇒ **移动不发生**，`:19883-19888`/`:17612`）—— 这是我们**缺的那一条**（以前不够也照移、指挥点变负），已补；② `CanMoveAndAttackInTheSameTurn()` 假 ⇒ `attackLeft=0`、真 ⇒ `movementLeft-=1` ✓ 已有（`can_move_and_attack`：装甲/`move_attack`）；③ 前线容量 `row_full` ✓；④ 钩子 `event_fx["move"]`（0x32）✓。旧行"上线费用…是手写"**不准确**（扣费口径本来就与适配器的 `opc` 同源）。测试 `tests/test_move_cost.py` 4 项；`SIM-FIDELITY:32` 改 **已按原版** | ✅ 完成 |
| ⑩ | **其余效果键**：`sim/engine.py::_apply_eff` 每个 `e.get(...)` 分支补出处 | 各 BP 动词 | 逐条待补 | 待做 |
| ⑪ | **读侧验收**（不是移植）：报告 §14.4 频次榜前 9（`IsLocatedOnBoard` 819×480 … `isBuffedByCard` 125×74） | `NATIVE-COVERAGE` §14.4 | ★ **2026-10-04 对账（报告汇总里只有 4 条标「未做」）**：① `GetValidValue`（36×）/② `GetEnumeratorValueFromIndex`（16×）—— 报告写"**已由 Recorder 钩住**"，但**整个仓库 grep 不到这两个名字**（`RECORD_ONLY` 17 条里没有、桩表 `kardsmem/virtual_defaults.py` 48 条里也没有）⇒ **这条说法在当前代码里不成立**，要么过时、要么是间接机制；**待查**（离线对一张"随机关键词挑选"的卡跑 `record_effects`，看 VM 是否停在 `Unimplemented`）；③ `FMod`（`UKismetMathLibrary`，6 次 / 1 张卡）—— 报告理由"返回值+出参并存、VM 出参写回约定不支持"与代码一致（没有实现）⇒ **如实留缺口**；④ `CanEndTurn`（`AMatchControllerV2`，2 次）—— 报告"进程外读不到、待用户确认"，而我们**已把它实现成显式 APPROX**（`kardsmem/cardnatives.py:327` 的 `APPROX` 集合 + `:921` 默认 `True`，注释写明是近似）⇒ 状态应为「已实现（近似）」而**不是**「未做」。★ 其余 5 条（`IsLocatedOnBoard`/`isBuffedByCard` 等）在 `cardnatives.py` 已实现且 §14.2 复核过 ⇒ **无需重写** | ✅ 对账完成（含 1 条报告说法待查） |

> **`SIM-FIDELITY.md` 的「近似」清零**（P3 硬验收）对应上面 ⑥⑦⑧（3 条「近似」）+ ⑨ 等「部分」行。

### 2.2 每族落地清单（复制用）

- [ ] 读原版（BP 导出 / IDA），把**分支表 + 行号**贴进模块 docstring；
- [ ] 在 `engine/natives/<族>.py` 写移植实现（只认鸭子类型，**不 import `sim`**）；
- [ ] 旧实现改成转调（`sim` 侧 import 不变），**行为零变化**；
- [ ] `tests/test_<族>.py` 钉住：原版常量/枚举值、分支表性质、接线是真的；
- [ ] 更新 `SIM-FIDELITY.md` 对应行的「状态/已知偏差」+ 本工单；
- [ ] `tests/run_all.py` 全绿；提交信息写清"读了哪几行原版"。

## 3. 已完成的刀

### ① 第一刀（`engine/natives/stats.py`，2026-10-03）

* **建立了什么**：`EChangeType` 十个**具名常量**（替换 `effectvm`/`sim` 的魔数 `2/3/5`）、
  `clamp_stat`（`[0,99]`）、`clamp_armor`（`[0,3]`）、`is_set_value` / `writes_buff_field` /
  `clamp_applies` / `fires_after_change_events` / `touches_stats`。
* **接线（两处，纯重构、行为零变化）**：`engine/effectvm.py::_is_set_change` 转调 `stats.is_set_value`；
  `sim/engine.py` 的三个 `set_*` 夹取换成 `stats.clamp_stat`。
* **测试**：`tests/test_native_stats.py`（34 项）；全量 `tests/run_all.py` **75 个、失败 0**。
* **第二刀（同族补完，2026-10-03）**：`ChangeDefense`（`:11215-11480`）与 `ChangeOperationCost`（`:8892-9188`）
  **逐行读完**，两条分支表 + 三条 `switch` 表进 `stats.py`（`ATTACK_SWITCH`/`DEFENSE_SWITCH`/`OPCOST_SWITCH`
  + `BP_FUNC_RANGES`）；**仍不接线**（理由见 R1/R2/R3/R9）。新增**出处核对测试**：拿导出件逐条验
  "我引的行号真的含有我声称的东西"，并在发布导出（无 `reverse-data`）时**如实 SKIP**。
  ★ 该核对当场抓到我自己的一个错引用（`DestroyCard` 在 `:11347`、我写成 `:11348`）—— 已改。
  ★ **一处口径更正**：我先前在代码注释/提交信息里把"数值变化通知"写成 `0x04/0x05`，**`0x04` 是错的**
  （`0x04` = `OnAfterOtherCardAttacks` 攻击后，`ATTACK-HOOKS-1.60.md:855`）；正确族是
  `0x05/0x06/0x07/0x10/0x11/0x2C/0x2D/0x09/0x0A`（同报告 `:867`，标**未接**），与代码里读到的
  `FetchAllCardsWithEventTrigger(0x5/0x6/0xA/0x9)` 对得上。测试已把这条钉住。
  ★ 另记：FModel 反编译的 ubergraph **控制流有损**（同类教训），"分支是否就地 return"
  仍需第二来源（IDA 读 Kismet 字节码）—— 见 R4。
* **第三刀（2026-10-03，接线）**：① 的第四个函数 `ChangeHeavyArmor`（`:10320-10507`）也读完
  （夹取 `[0,3]`、**6-9 直接 return**、第三个独立 buff 字段 `heavyArmorBuff`）⇒ **① 四个函数 4/4 读完**；
  原版"**基础字段 + 独立 buff 字段**"落地：`U.atk_buff` / `H.cost_buff` + 适配器读 `attackBuff`/`kreditsBuff`，
  `set_attack_buff`/`set_kredit_buff` 两条叶子从"记缺口"变成如实消费，`set_attack`/`set_kredit`
  改成"写**基础值** ⇒ 总量 = `clamp(基础 + buff)`"。
* **第四刀（2026-10-03 深夜，记录器折算修正）**：`effectvm` 的 `SetValue` 折算改成 **buff-aware** ——
  `d = clamp(n) + 目标当前 buff 累加器 − cur`（原版 `SetValue` 写的是**基础字段**，而我们手里的
  `cur` 是**总量**）。修前："5 攻 + 50 buff（总量 55）被设为 2" ⇒ `d = −53` ⇒ 总量被抹成 **2**；
  修后 ⇒ `d = −3` ⇒ **52**（与实机口径一致）。同时补上原版的 `clamp(n)`（`Label_855` 先夹再写）。
  `cur_stats` 相应补读 `attack_buff` / `opcost_buff`。★ **探针是我这一步的救命稻草**：我原本断定
  `ChangeHeavyArmor`/`ChangeOperationCost`/`ChangeKreditCost` 因为"没有对应的 elif 分支"而被**丢掉**，
  一跑探针才发现有**通用数字兜底**接住了（它们都在）—— 差点去"修"一个不存在的 bug。
  但同一批探针揪出了真的 **R12**：重甲/费用的 `SetValue` 落进通用兜底 ⇒ 被当**增量**记
  （重甲还该夹 `[0,3]` 而不是 `[0,99]`）。
* **第十刀（2026-10-03 深夜，ChangeDefense「归零 ⇒ 摧毁」接线）**：原版 `ChangeDefense` 的两条改值分支
  都带摧毁（`Label_984`：设完 `getTotalDefense()==0` ⇒ `DestroyCard`；`Label_1995` 负分支：`<=0` ⇒ `DestroyCard`），
  而我们以前**只掉血不摧毁**（`set_defense: {1: -7}` 会让单位挂在 0 防御上活着）。
  现在三条通道（群体 `buff_ids` / 标量 `buff` / 绝对值 `set_defense`）走同一个 `_def_delta`：
  防御**降到 ≤0 就离场**（`_apply_death`，与 `defense_aoe` 早就用的口径一致）；升防御不会摧毁
  （原版正分支夹取下限是 1）。★ 顺序细节：标量 `buff` 的防御部分**留到"
  该目标子效果块"末尾**才应用 —— 因为它可能把目标打掉，而 `give`/`heal`/`steal`/`tax` 都作用于同一个 `t`。
  ★ **这是一次行为变更（单位会因为减防而真死）⇒ 必须实机复核**。
* **第九刀（2026-10-03 深夜，R15 收口 + 一处错行号）**：把 `ChangeDefense` 里唯一没读的 `Label_3734`
  读了 —— 它是**拒绝分支**（`":11527-11532"` 记日志 `change type incorrect for "Change Defense"`
  + `qqq=false` + return），`ct=8` 的 `Label_5740` 是**空 return** ⇒ **防御根本没有 0/4 的 buff 通道**，
  R15 那个"buff 压到 0 要不要摧毁"的场景在原版里**不可能发生** ⇒ 结案（无需接摧毁链）。
  ★ 同时发现并修正一处**错行号**：`BP_FUNC_RANGES["ChangeDefense"]` 记成 `(11215, 11480)`，
  而函数体真正到 `:11696` —— 旧守卫只检查"止行没越到隔壁函数"（`next > b`），所以放过了它；
  守卫已收紧为"**下一个 `public void` 必须紧贴在止行之后（±5 行内）**"，四个函数的止行现在都被证明
  就是函数体末尾。另加两条出处断言（`:11528` 的拒绝日志、`:11692` 的 `Label_5740`）。
* **第八刀（2026-10-03 深夜，R11 后半 + 新缺口 R15）**：把**普通加减通道**也按原版夹回**总量**范围 ——
  `buff_ids`/`buff`/`attack_turn`/`opcost` 夹 `[0,99]`、`armor` 夹 `[0,3]`（原版 `getTotal*` 是 `clamp(0,99)`，
  重甲另有一套 `clamp(0,3)`）。修前：90 攻吃 `+50` 会读成 **140**（游戏 99）、3 防吃 `−50` 会变 **−47**。
  `atk_turn`（我们自己的"临时部分"簿记、**不是**游戏字段）不跟总量一起夹 —— 残项写进 R11。
  ★ 读 `ChangeDefense` 的 switch 时顺手发现：`0/4/6/7/9` 都去 `Label_3734`，而**那个 label 的函数体我还没读**
  ⇒"走 buff 通道把防御压到 0 要不要摧毁"**没有依据** ⇒ 记 **R15**，不猜（`2/3/5` 的 `Label_984` 明确会摧毁）。
* **第七刀（2026-10-03 深夜，手牌费用 R14）**：`ChangeKreditCost`（`:10067-10270`）**全文读完**，
  并修掉一个"产出但没人消费"的键。分支表（**与四个兄弟不同，别照抄**）：
  `0 → Label_1346`（`kreditBuff += amount`，**不夹**）、`1 → Label_1031`（`kredit = clamp(kredit+amount,0,99)`）、
  **`2/3 → Label_758`**（`setAndEncryptKredit(clamp(amount,0,99))`；比的是**基础**字段 `getAndDecryptKredit`，
  不是总量）、`4 → Label_2461`（buff 减少）、**`5` 与 `6-9` → 默认 `Label_1614`**
  （`NotifySetKreditCost` + `trigger 0x2D` 逐张 `OnOtherCardKreditCostChanged` ⇒ **不改数值**）。
  闸门：卡无效 / `instigatorID ≤ 0` ⇒ 记日志；`skipCovertCheck=false` 时还要过 `CanCardBeBuffed`。
  ★ 我们的键 `cost` 是**标量**且**全仓库没有消费者** ⇒ 手牌费用改动（降价/加价/设值）在模拟里
  **完全没建模**；标量 + `_apply_eff(target)` 那套也套不上（手牌不是 target）—— 这正是它一直没人消费的原因，
  产出键审计也扫不到（通用兜底用的是**变量键**，见 `test_effect_keys_consumed.py` 的"口径与限制"）。
  修：`effectvm` 改发**逐卡字典** `cost_ids: {card_id: 增量}`（set 家族按"目标总量 = `clamp(clamp(n)+buff)`"），
  `sim` 落到 `Hand.cost`（`[0,99]`）；指针认不出 ⇒ **记缺口、不编数**；`5`/`6-9` 按原版**不产出效果**。
* **第六刀（2026-10-03 深夜，R12 + R13 —— 本节从新到旧排）**：① **R12（重甲）**：`armor` 进 `SetValue` 设值分支、
  按原版 `[0,3]` 夹、`cur_stats` 从快照 `raw` 读 `total_heavy_armor`/`heavy_armor_buff`
   ⇒ 1 甲"设为 2"给 **+1**（旧实现 +2 ⇒ 变 3）、设为 9 给 +2（先夹到 3）。
  ② **R13（两条读侧路径口径不一致）**：`gamemodel.getTotalAttack/Kredits/OperationCost` 原来**不夹**，
   而 `cards.read_raw` 的 `total_*` 一直夹 `[0,99]` ⇒ `sim` 走前者，**90 攻 + 50 buff 读成 140**
   （游戏 99），把伤害/威胁算高 —— 补上 `clamp(0,99)`（IDA 三个地址见 R13 行）。
  ③ 顺带把设值分支的"目标总量"改成 `clamp(设成的基础值 + buff)`（攻/防/行动费 `[0,99]`、重甲 `[0,3]`），
   这是 R11 的一半（另一半是普通加减通道，仍未夹）。
* **第五刀（2026-10-03 深夜，揪出一条活死代码）**：第四刀的测试是**喂自造的 `cur_stats`**（`docs/P3` §6
  那条"局限"就是这么来的），所以我按"`cur` 是总量"写了 `+buff`；回头验**真路径**才发现：
  `kardsmem/board.py::Card` 的 `total_attack` / `total_operation_cost` 当年写成了**方法**，而
  `record_effects` 用 `getattr(c, "total_attack")` + `isinstance(v, (int, float))` 读它 ⇒ 拿到的是
  **绑定方法**、被过滤掉 ⇒ **`cur_stats` 里从来没有 `attack`/`opcost`** ⇒ 那两个 SetValue 分支一直在记
  「读不到目标当前值」的缺口、**效果被整个丢掉**（"设成 n"一类牌在评估里是空的 —— 活死代码）。
  修：① 两个方法改 **`@property`**（原版取值就是 `getTotalAttack()`/`getTotalOperationCost()`）；
  ② 把填充逻辑**抽成 `effectvm._fill_cur_stats(rec, cards)`**，测试改成拿**真 Card** 跑它
  ——"能不能读出来"本身要有测试（同类教训：让它跑完不算证明；这次栽在"测试喂的输入
  不是真实输入"）。修后真路径：`cur_stats = {attack:10(基础3+buff7), attack_buff:7, …}`，
  `SetValue(2)` ⇒ 增量 `−1` ⇒ 总量 **9**（原版：基础设成 2、buff 留 7）。
  ★ 影响面：激活后，**直接调 `ChangeAttack/Defense/OperationCost(…, SetValue/Suppress/veteranSet)`
  的卡**在评估里开始真的产生效果（以前被丢）。`MakeVeteran` 不受影响（它是被**钩住的动词**，
  函数体不执行、内层 ChangeAttack 不会被记，老兵走 `effectvm` 自己的 `_veteran_payload`）
  —— 但仍**必须实机复核**（这类"激活"最容易跟已有的手写建模撞车）。
* **顺带**：`sim/engine.py` 清掉 `_unit_value` 的**重复定义**与 `_evaluate`（引用的 `_evaluator` **从未定义**、
  且无调用方 ⇒ 一调就 NameError 的死代码）。

### `ChangeAttack` 的完整分支表（读原版读出来的，逐行）

`BP_CardFunctions::ChangeAttack`（`:10981-11212`）：

| changeType | 分支 | 原版怎么写 | 设值? | 夹取 [0,99]? | after 事件? |
|---|---|---|---|---|---|
| 0 tempBuffGive | `:11128-11149` | `attackBuff += amount`（**独立字段**） | 否 | **否** | 否 |
| 1 permBuff | `:11095-11126` | `attack = clamp(attack + amount, 0, 99)` | 否 | 是 | 否 |
| 2 SetValue | `:11068-11093` | `attack = clamp(amount, 0, 99)`；**已相等就 return**（`:11071-11078`） | **是** | 是 | 否 |
| 3 Suppress | 同 SetValue（都 `goto Label_855`） | 同上 | **是** | 是 | 否 |
| 4 tempBuffRemove | `:11185-11208` | `attackBuff += amountRemoved` | 否 | **否** | 否 |
| 5 veteranSet | 同 SetValue | `attack = clamp(amount, 0, 99)` | **是** | 是 | 否 |
| 6-9 customAdd/customRemove/combatModify/notUsed | `:11151-11183` | **不改数值** | — | — | **是（唯一入口）** |

两条**反直觉**、读过原版才能确定的：

1. **`ExecuteAfterChangeAttackEvents` 全文件只有一个调用点（`:11174`），只从 default 分支（6-9）进**
   ⇒ changeType 0..5（buff/debuff/设值/老兵这些天天见到的）**不**触发 `OnAfterChangeAttack`(0x04) /
   `OnAfterOtherCardChangeAttack`(0x05)。（函数体 `:11950`：未压制 ⇒ 自己 `OnAfterChangeAttack`；
   压制 ⇒ `FetchAllCardsWithEventTrigger(0x5)`。）
2. 0/4 写**独立的 `attackBuff` 字段**且**不夹取**；1/2/3/5 写**基础/总值**且夹取 `[0,99]`
   ⇒ 模拟侧现在也分两个字段（`U.atk_buff` / `H.cost_buff`）：**buff 累加器本身不夹**（照原版），
   只有**总量**夹到 `[0,99]`（照原版 `getTotal*`）。见 R10 / R10b。

## 4. 待核 / 缺口（**不许**在没证据时"顺手改掉"）

| # | 缺口 | 为什么现在不动 | 下一步 |
|---|---|---|---|
| **R1** | `ChangeOperationCost`（`:8892-9188`）已**逐行读完**（分支表 + switch 表进了 `stats.py`），但**仍不接线** | 它多一个 `isBuff` 入参、开头有 `IsUnrevealedCovertCard` 静默早退、2/3/5 分支会置 `runResetEvents`（触发 0x09）；且 0/4 写的是**独立字段** `operationCostBuff` —— 落到 `sim` 需要状态先有"buff 字段"（R3/R9 同族） | 先做 R3/R9（状态里区分 buff 字段），再接。★ 2026-10-04 补：R3 的落地路径已明确（见 R3 行，**走动词表 + changeType、不走叶子**），R9 需要"谁调的"判据 —— `kardsmem/vm.py` 的 `_stack`（`:788`/`:792`，共享的活跃 UFunction 集合）**已经可用**。★ **2026-10-04 收口：值语义已闭环** —— 门已接（`IsUnrevealedCovertCard` :8906 ⇒ 未揭示隐蔽牌静默 return）；ct 值语义已被现有动词表覆盖（0/4 写 buff 累加器、1 写基础 `Label_1251`:9018 起、2/3/5 设值写基础 `clamp(amount,0,99)` `:9155`）；`runResetEvents`（`:9161` → `0x9` 扇出，`:9069`）**只影响事件、不影响数值** ⇒ 归"数值变化通知族"（0x09/0x0A，报告标未接）。★ **`isBuff` 参数在本构建的函数体里从未被使用**（只出现在签名 `:8892`）⇒ 早前"它多一个 `isBuff` 入参"这条**作废** |
| **R2** | `ChangeDefense`（`:11215-11696`）已**逐行读完** | **摧毁那半已接**（2026-10-03）：三条通道（群体 `buff_ids` / 标量 `buff` / 绝对值 `set_defense`）走同一个 `_def_delta` —— 防御降到 **≤0 就离场**（原版 `Label_984` 设完 `==0` 即 `DestroyCard`、`Label_1995` 负分支 `<=0` 也摧毁），与 `defense_aoe` 早就用的口径一致。**仍未接**：`Label_984` 里 `changeType==2` 的 `OnAfterDefenseIsSet` + `trigger 0x6`、`Label_1995` 正分支把 `maxDefense` 抬到总防、以及"同类不同名"的入参（`isBuff` 缺省） | 这些是**通知链**（0x06 属"数值变化通知"族，见 `ATTACK-HOOKS-1.60.md:867` 标**未接**）⇒ 等那一族统一做 |
| **R3** | ~~0/4 的**独立 buff 字段**没实现~~ ⇒ **已做（2026-10-03）**：`U.atk_buff` / `H.cost_buff` 落进状态，适配器从原版字段 `attackBuff`/`kreditsBuff` 读；`set_attack_buff`/`set_kredit_buff` 两条叶子**不再记缺口**，被如实消费；`set_attack`/`set_kredit` 改成"写**基础值** ⇒ 总量 = `clamp(基础 + buff)`" | 残：`operationCostBuff` / `heavyArmorBuff` 还没有对应字段（要同时给 `U.opc`/`U.armor` 加，见 R1）。★ **2026-10-04 调研补（决定了怎么落）**：这两个字段**没有 `setAndEncrypt*` 叶子可钩** —— 全量导出里只有 5 个加密叶子（`Attack`/`AttackBuff`/`Defense`/`Kredit`/`KreditBuff`）；`ChangeHeavyArmor`/`ChangeOperationCost` 是**直接字段赋值**（`cardToChangeRef->heavyArmor = Clamp_1;` / `->heavyArmorBuff = Add_IntInt_1;`，`ChangeOperationCost` 同形 `operationCost`/`operationCostBuff`）⇒ 实现路径 = **在动词表 + `changeType` 上分**（**0/4 ⇒ 写 buff 累加器**、不夹；1 与 2/3/5 ⇒ 写**基础字段**），`SET_ENC_FIELDS` 帮不上；状态加 `U.opc_buff`/`U.armor_buff`，而 `CARD_I32` **已有** `operation_cost_buff`(0xB4)/`heavy_armor_buff`(0x1E0)，只是**没进快照 raw**（要补） | 按这条路径落：`U.opc_buff`/`U.armor_buff` → 快照 raw 补 `operation_cost_buff`/`heavy_armor_buff` → 动词表按 `changeType` 分（0/4 走 buff 累加器） → `sim` 消费 → 测试 |
| **R4** | changeType 6-9 的"数值变化通知"没建模 | 目前**没有** 6-9 的调用方 | 等真实用例 |
| **R9** | **`setAndEncryptDefense` 是"双重身份"的叶子**：`ChangeDefense` 调它，`ApplyDamageToCard`（伤害扣防那步，`:16461`）**也**调它 ⇒ 把它一律记成 `set_defense`，**拿不到**"防御设成 0 ⇒ 摧毁"（那是 BP 层后续，不在叶子里）；反过来无脑给 `set_defense` 加"0 ⇒ 摧毁"又会与伤害链**重复结算** | 要先知道"这次写是谁发起的"（`ChangeDefense` 还是伤害管线） | 在 recorder 侧按**调用栈/上层动词**区分，或加两个不同的效果键 |
| **R5** | `ChangeBuffsFromCards`（0/4 分支算 `amountRemoved` 的那个）没读 | 还没到接线那一步 | 读它再决定 0/4 的增量怎么算 |
| **R6** | ②③④⑤ 全是 BP 动词，报告**没有**它们的频次（§0） | 顺序得自己定（已按"P4 前置 → 近似清零"排） | 按 §2.1 顺序走 |
| **R7** | `MakeCardsFight`（`:9755-9813`）**原文已读**：结构**不是**对称的"两边各算一次" —— 主分支（`unitOppositeSide.IsLocatedOnBoard()`）算 `this→opposite` 的伤害（过 `ExecuteOnDealDamageAddDamage` → `...AfterCalc`）后 `goto Label_622`，那里**调用两次** `ApplyDamageToCard`（`_final_damage_to_enemy` 与 `_final_damage_to_my`），但**此时 `_final_damage_to_my` 还是初值 0**；另一分支（对方不在场）才算 `opposite→this`，然后 **`:9802` 直接 `return`，根本没打**。⇒ **按印出来的文本，任何一条路径都只对一边造成伤害** | 代码注释（`engine/natives/damage.py`）断言"这是 Sequence、实为两边都打" —— 那是**读法**，不是文本能证明的（与 R4 同源的 ubergraph 有损问题）。但 **13 个调用点全是单次** `MakeCardsFight(a, b)`（如 `card_unit_40_royal_marine.cpp:65`、`card_unit_panzer_iv_h.cpp:109`），而卡面语义是"两张单位互相攻击" ⇒ 单次调用必须两个方向都发生 | 定案要**第二来源**：运行时 Kismet 字节码（`kardsmem.kismet` 走 `ReadProcessMemory` 只读；**不要**在对局进行中另起 frida attach 去 dump —— 弯路 #41）。★ **2026-10-04 已做（只读反汇编，R7 结案）**：`probe_excess_asm.py MakeCardsFight -` 打出 **100 行**真实字节码 —— 它是**三针 Sequence**：`PushExecutionFlow(to=622)` + `PushExecutionFlow(to=437)`（:242/:247），先跑第一针（`IsLocatedOnBoard` → `ExecuteOnDealDamageAddDamage` + `...AfterCalc`），`PopExecutionFlow` 落到第二针（**offset 594 `Let(_final_damage_to_my_unit)` 真的把另一个方向的最终伤害算出来了**），再落到第三针（**offset 622 / 665 两次 `ApplyDamageToCard`**：`(opposite←enemy 伤害)` 与 `(this←my 伤害)`）⇒ **代码注释"这是 Sequence、实为两边都打"是对的**，导出件那个"看起来只打一边"的 if/else 是 FModel 把 Sequence 拆成 goto 的 artifact（弯路 #4 又一例）。**结论：`MakeCardsFight` 无需改代码** | ✅ 已定案（无需改代码） |
| **R8** | `SIM-FIDELITY:38` 写"打捞 `SalvageCard…`"，代码里叫 `SalvageMultipleUnits`（**名字对不上**） | 文档小错 | 改文档 |
| **R10** | 原版"攻/费 = 基础字段 + 独立 buff 字段"的语义（`ChangeAttack` 的 0/4 写 `attackBuff`，**不夹取**）—— 只有一个总量时，`SetValue` 会把挂着的 buff **一起抹掉**（5 攻 + 50 buff 后"设为 2" ⇒ 原版 52、旧实现 2） | —— | **已做**（字段 + 消费 + 适配器 + 测试）；边界：总量此前若已顶到 99（夹过），从总量反推不出基础值 —— 极端局面才出现，已在 `sim.engine._apply_buff_field` 注释里写明 |
| **R10b** | ~~recorder 折算不认 buff~~ ⇒ **已修（2026-10-03，在 `effectvm` 的 SetValue 分支）**：`cur` 是**总量**、而原版 `SetValue` 写的是**基础字段** ⇒ 增量必须算成 `clamp(n) + 目标当前 buff − cur`。修前"5 攻 + 50 buff（总量 55）设为 2"给 `d = −53` ⇒ 总量被抹成 2；修后 `d = −3` ⇒ **52**（与实机一致）。同时把 `clamp(n)` 补上（原版 `Label_855` 先夹再写）。**跨决策**本来就没问题（每次决策 `from_cards` 都读权威 `attackBuff`）。<br>**残项**：`atk_buff` 字段在同一次动作内**不跟新**（0/4 折的是总量增量）—— 只影响"同动作里 0/4 之后又发生 `set_attack_buff` 叶子写"这种极罕见组合，测试已钉住 | —— | 要彻底一致得给 0/4 产出 `*_buff_delta` 并核对 `buff`/`buff_ids` 的其它消费者；**不急**（有网可兜） |
| **R11** | **总量没按原版夹 `[0,99]`**：`sim` 侧 `buff_ids`/`buff`/`armor`/`opcost`/`attack_turn` 都是直接加减。原版 `getTotal*` 是 `clamp(0,99)` | —— | **已修（2026-10-03，两半都落）**：① `SetValue` 折算按"目标总量 = `clamp(设成的基础 + buff)`"算（重甲 `[0,3]`）；② **读侧** `gamemodel.getTotal*` 补 `clamp(0,99)`（R13）；③ **消费侧**普通加减通道也夹：`buff_ids`/`buff`/`attack_turn`/`opcost` 夹 `[0,99]`、`armor` 夹 `[0,3]`。**残**：`atk_turn`（我们自己的"临时部分"簿记，非游戏字段）没跟总量一起夹 —— 只有攻已顶到 99 时会让 `atk − atk_turn` 偏离真实基础值 |
| **R15** | ~~"走 buff 通道把防御压到 0 要不要摧毁"~~ ⇒ **已核，问题不存在**（2026-10-03）：`ChangeDefense` 的 `0/4/6/7/9` 去的 `Label_3734` 是**拒绝分支**（`:11527-11532`：记日志 `change type incorrect for "Change Defense"` + `qqq=false` + return），`ct=8` 的 `Label_5740` 是**空 return**（`:11692-11693`）⇒ **防御根本没有 0/4 的 buff 通道**，只有 `1`（permBuff）与 `2/3/5`（set 家族）会改值。所以"buff 通道压到 0"这个场景在**原版里就不可能发生**，不需要接摧毁链 | 同上顺带修正：`BP_FUNC_RANGES["ChangeDefense"]` 记成 `(11215, 11480)` 是**错的**（函数体到 `:11696`）—— 旧守卫只查"没越到隔壁"、没查"止行就是函数末尾"，所以漏了；守卫已收紧成"下一个 `public void` 必须紧贴在止行之后（±5 行）" | 已做 |
| **R12** | `ChangeHeavyArmor` / `ChangeKreditCost` 的 `SetValue` 没走"设值"分支（探针证据：`ChangeHeavyArmor ct=2 @armor=1` 曾产出 `{'armor': 2}` = **+2 增量**，原版 `Label_1433` 是 `heavyArmor = clamp(2,0,3)` ⇒ 应净 +1） | —— | **已全部修（2026-10-03）**：① **重甲** 按 `[0,3]` 夹（1 甲设为 2 给 +1、设为 9 给 +2）；② **手牌费用** 见 R14（`ChangeKreditCost` 全文读完：**只有 2/3 是设值**，`5` 与 `6-9` 走默认分支只发通知 `:10219-10230` ⇒ 不改数值——这条与四个兄弟函数不同，已单独建模为 `KREDIT_COST_SET_FAMILY`） |
| **R14** | **`ChangeKreditCost` 的效果键是**标量** `cost`、而且**没有任何消费者** ⇒ 改手牌费用的牌在模拟里**完全没建模** | 标量 + `_apply_eff(target)` 的模型套不上（手牌不是 target）—— 这正是它一直没人消费的原因；产出键审计也扫不到（通用兜底用变量键） | **已修（2026-10-03）**：`effectvm` 改发**逐卡字典** `cost_ids: {card_id: 增量}`（set 家族按"目标总量 = `clamp(clamp(n)+buff)`"换算，`[0,99]`），`sim` 落到 `Hand.cost`；指针认不出 ⇒ **记缺口不编数** |
| **R13** | **两条读侧路径对"总量"口径不一致**：`cards.read_raw` 的 `total_*` 夹 `[0,99]`，而 `gamemodel.getTotalAttack/Kredits/OperationCost` **不夹**；`sim` 走后者 ⇒ 90 攻 + 50 buff 被读成 **140**（游戏 99），把伤害/威胁算高 | —— | **已修（2026-10-03）**：三个 getter 补 `clamp(0,99)`（原版 `getTotalAttack` IDA 0x144B14E90 / `getTotalKredits` 0x144B15020 / `getTotalOperationCost` 0x144B15200） |

## 5. 报告 ↔ 代码的偏差（本工单的调研结论；**已复核**）

`NATIVE-COVERAGE-1.60.md` §14.5「剩余清单」**大面积过时**——下列几条报告说"未落地"，代码里其实已落地：

| 报告说 | 代码里 |
|---|---|
| §14.5:863 虚函数默认值表"未落地，要改 `vm.py`" | `kardsmem/virtual_defaults.py` + `vm.py:639,652` 已接线（`tests/test_virtual_defaults.py`） |
| §14.5:864 `Map_Find` 返回 bool+出参"要改 vm.py" | `kardsmem/kismetlib.py` 的 `NativeOut(k in m, m.get(k))` 已改 |
| §14.5:865 `card_targets` 的 `EnumCompare*` 写反"不在范围" | `tools/card_targets.py` 已按 `0=Equal / 1=NotEqual` |
| §14.5:866 `boardeval` 消费 `remove_*`/`*_aoe`/`fight`"待做" | `sim/engine.py` 已有对应分支（`remove_unit`/`destroy_aoe_ids`/`pin_aoe_ids`/`to_deck_aoe_ids`/`damage_aoe_ids`/`fight`） |
| §15.3:952 `ChangeKreditsBySide` 夹取"缺" | `engine/natives/kredits.py::clamp_kredits` 已夹 |
| §15.3:951 `MakeCardsFight` 记录"消费缺" | `sim/engine.py` 的 `e.get("fight")` → `apply_fight` 已消费 |

⇒ **别照 §14.5 排期**：先 `grep` 一遍代码再决定一件事做不做（同类教训：
"字典里写着读不到"不等于真的读不到）。

## 6. 调研的局限（明确标注）

* §14.4 的"调用×卡"是**静态普查**（扫 3044 个 .cpp），不是运行期实测；且 `native_calls.json`
  **不统计虚调用** ⇒ 虚函数那几行频次是另数的，两类数字不可直接比。
* §14.4 表被裁成"胜负相关行"（196 行），完整 351 行在 `census2/classification.csv`。
* 本工单的"engine 侧缺什么"是**读代码得出**的判断，**没有**验证 engine 状态字段能否承载
  （例：`cn1/cn2`、`buffsFromCards` 来源表在 `engine/state.py` 里确实没有 —— 这是"推测缺"）。
