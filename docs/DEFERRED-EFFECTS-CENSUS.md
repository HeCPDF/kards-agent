# 延迟/常驻效果普查（DEFERRED-EFFECTS-CENSUS）

*由 `tools/census_deferred.py` 生成（离线静态扫描，1.60.27292 BP 导出）；别手改，改工具后重跑。*

口径：卡 = `Blueprints/Cards/**/card_*.cpp`（不含皮肤），共 **2015** 张。**打出即结算的钩子**（OnCardDrawnFromDeck、OnCardReset、OnCardRevealed、OnCardSpawnedInHand、OnCounterMeasureTriggered、OnCreateCard、OnCreateCardApplyCampaignUpgrades、OnEnterPlay、OnHandTargetSelected、OnPlayedFromHand、OnStartOfGame）不算“留下来”；其余 `public void On…` 覆写 = 留下来的钩子（armed hooks）。持续时间/范围是启发式（文字+调用名），不是语义证明。

## 1. 总览

| 类型 | 张数 |
|---|---|
| order | 732 |
| infantry | 568 |
| location | 180 |
| tank | 154 |
| fighter | 144 |
| bomber | 98 |
| artillery | 60 |
| gotcha | 52 |
| wildcard | 24 |
| ? | 3 |

指令（order）732 张：**打出后仍带留下来的钩子 180 张**，纯一次性 552 张。

| 族 | 张数 |
|---|---|
| A 指令打出后仍挂着钩子（order + armed hook） | 180 |
| B 单位/地点常驻在场钩子（读侧从活盘面拿，不属“打出之后才布置”） | 683 |
| C 反制（gotcha，敌方回合从手牌触发） | 52 |
| D 授予自定义能力（CustomAbilityAdd，任意类型） | 79 |
| E autoplay 标签（天气/预报：回合开始才落地） | 4 |
| F 倒计时（SetCountdown/DecrementCountdown） | 15 |
| G 游戏限制（AddGameplayRestriction，带期限的一侧禁令） | 19 |
| H 触发队列（AddToTriggerQueue） | 6 |
| I 侧效果（AddGameplaySideEffect，如 blockgotcha） | 1 |
| J 回合末清除（AddAttackUntilEndOfTurn / destroyEndOfTurn） | 28 |

## 2. A 族：指令留下来的钩子（按钩子分）

“registry 现状”取自 `engine.deferred.FIRE_POINTS`：**已接** = sim 在对应事件点会触发该钩子的预计算后果；**未接** = 打出这类指令时 `RuleV2` 会记缺口（不再记成 `vm(空)`）。

| 钩子 | 指令张数 | registry 现状 | 触发事件 |
|---|---|---|---|
| OnEndOfTurn | 69 | **已接** | 己方回合结束（sim_turn_end） |
| OnStartOfTurn | 41 | **已接** | 己方回合开始（sim_turn_start ⑥） |
| OnOtherCardPlayedFromHand | 23 | 未接 | — |
| OnOtherCardEnterPlay | 19 | 未接 | — |
| OnOtherCardDestroyed | 18 | 未接 | — |
| OnOtherCardReset | 12 | 未接 | — |
| OnOtherCardSuppressed | 12 | 未接 | — |
| OnOtherCardDealDamage | 11 | 未接 | — |
| OnAfterOtherCardLeaveBoardOrOwner | 8 | 未接 | — |
| OnOtherCardLeaveBoardOrOwner | 8 | 未接 | — |
| OnOtherCardCreatedAlterCard | 7 | 未接 | — |
| OnOtherCardSpawnedInHand | 6 | 未接 | — |
| OnAfterOtherCardAttacks | 6 | **已接** | 攻击结算后（sim_attack / sim_enemy_attack_event；事件主体=被打的牌） |
| OnOtherCardDrawnFromDeck | 5 | 未接 | — |
| OnBeforeStartOfTurn | 5 | **已接** | 己方回合开始（sim_turn_start ③） |
| OnOtherCardDealDamageAddDamage | 3 | 未接 | — |
| OnOtherCardDiscarded | 2 | 未接 | — |
| OnOtherCovertCardPlayedFromHand | 2 | 未接 | — |
| OnOtherUnitUnpinned | 2 | 未接 | — |
| OnOtherCardReceiveDamage | 2 | 未接 | — |
| OnOtherCardMoveToFrontline | 2 | 未接 | — |
| OnAfterDeckChanged | 2 | 未接 | — |
| OnAfterExtraKreditSlotGain | 2 | 未接 | — |
| OnFrontlineOwnershipChange | 2 | 未接 | — |
| OnOtherCardMovedToLocationInHand | 1 | 未接 | — |
| OnAfterOtherCardChangeAttack | 1 | 未接 | — |
| OnBeforeOtherCardAttacks | 1 | 未接 | — |
| OnBeforeOtherCardPlayedFromHand | 1 | 未接 | — |
| OnOtherCardRetreat | 1 | 未接 | — |
| OnOtherCardLocationMoved | 1 | 未接 | — |
| OnOtherCardOperationKreditsSpent | 1 | 未接 | — |
| OnAfterOtherCardDefenseIsSet | 1 | 未接 | — |
| OnAfterOtherCardGainDefense | 1 | 未接 | — |
| OnDestructionEffectTriggered | 1 | 未接 | — |
| OnOtherCardLoseSmokescreen | 1 | 未接 | — |
| OnAfterCardMovedToDeckFromHand | 1 | 未接 | — |
| OnOtherCardSurvivedCombat | 1 | 未接 | — |
| OnOtherCardAttacks | 1 | 未接 | — |
| OnSuccesfulDiscard | 1 | 未接 | — |
| OnDealDamageAddDamageAfterCalc | 1 | 未接 | — |
| OnOtherCardDeveloped | 1 | 未接 | — |

### 2.1 持续时间 × 范围（启发式；A 族每张牌各计一次）

| 持续时间 | 范围 | 张数 |
|---|---|---|
| 常驻(未写期限) | 场上一批单位 | 34 |
| 常驻(未写期限) | 自身/一侧 | 31 |
| 常驻(未写期限) | 牌库 | 28 |
| 本回合 | 自身/一侧 | 21 |
| 下回合 | 自身/一侧 | 19 |
| 本回合 | 场上一批单位 | 17 |
| 下回合 | 牌库 | 7 |
| 本回合 | 牌库 | 5 |
| 倒计时 | 自身/一侧 | 5 |
| 下回合 | 场上一批单位 | 4 |
| 本回合 | 手牌 | 3 |
| 每回合/常驻 | 自身/一侧 | 3 |
| 常驻(未写期限) | 手牌 | 2 |
| 倒计时 | 牌库 | 1 |

### 2.2 每个钩子的牌（标题；★ = 同时有 CustomAbilityAdd 授予）

- **OnEndOfTurn**（69）：AREA BOMBARDMENT、ARMAMENTS、BETASOM、BLITZKRIEG、BREAKTHROUGH、BURST OF FIRE、IRON FIST、RIDING THE STORM★、SPRING THE RESERVES、CAMPAIGN TRAIL、COMMITTED CREW、JUGGERNAUT、CONFUSION、DARK DEEDS、DAWN OPERATIONS、DETAILED RECON、DO OR DIE、EAGLE DAY、EDGE OF THE EMPIRE、EXILED FORCES、FINAL PUSH、FOR THE EMPEROR、FRONTAL DEFENSE★、GUNBOAT HIT、GUNBOAT RUN、HONOR、IJN AKAGI、INDUSTRIAL MIGHT、LIGHTNING CONQUEST、MATERIALS★、IJN MIKURA、MOBILE DEFENSE、MOBILIZATION、MOLOTOV COCKTAIL、MOTHERLAND CALLS!★、NIGHT ATTACK、ON THE HORIZON★、OUT OF THE MIST、OUT OF THE SUN、OUTMANEUVER、OVERRUN、PARTISANS、PATTON、PROLONGED SIEGE、PROVENCE LANDING、PURSUIT、PUSH!、RAID、RM ROMA、ROMANIAN BRIDGEHEAD、RUSH PRODUCTION、SAAR OFFENSIVE、SECOND FRONT、SHADOW STRIKE、SHIFTING ATTACK、SLOPED ARMOR、HEATWAVE、JUNGLE FEVER、JUNGLE FEVER、SCORCHING SUN、SCORCHING SUN、SURPRISE ENGAGEMENT、TIMELY SUPPLIES、TURNING POINT★、URA!★、VETERAN PILOTS、VOLUNTEER CORPS、ADMIRAL YAMAMOTO、YANK★
- **OnStartOfTurn**（41）：KM ADMIRAL HIPPER、BETASOM、BASE DEFENSE、CROSS OF LORRAINE、CRUISER SCOUTS、DEPTH CHARGES、DESERT RAID、DISTANT FRONT、DIVE BOMBING、DRIVE INTO THE SEA、ELUSIVE FORCE、EMBARGO、EXTENDED BARRAGE、FRONT AND HOMELAND、FRONT FORMATION★、GLIDE BOMBING★、GRIM DAY★、GUERILLA WARFARE SCHOOL、ISOLATION、MOTHER RUSSIA、MUD、NAVAL SUPPLY RUN、OVERCAST、PROTECT THE POCKET、RAID、MIST、REICHSBANK、RM ROMA、SNOWSTORM、STEALTH MISSION★、GALE、STRONG BOND★、BLUE SKY、SUPPLY SHORTAGE★、SYNTHETIC OIL、TASK FORCE 44、KM TIRPITZ、U-48、US WEATHER BUREAU、VYSTREL COURSE★、WAR BONDS
- **OnOtherCardPlayedFromHand**（23）：COLOSSUS★、COMMITTED CREW、CONTEST DOCTRINE★、CORPS COMMAND★、CROSS OF LORRAINE、DECOY TACTICS★、DEEP OPERATION★、DIPLOMATIC ATTACHÉ、DUTY BOUND、ESCAUT PLAN、FEIGNED RETREAT★、GUERILLA WARFARE SCHOOL、HULL DOWN★、PATTON、PROMOTION★、PROVENCE LANDING、RESERVES、RUSH PRODUCTION、SECRET SERVICE、HEATWAVE、SURPRISE ENGAGEMENT、WAY OF SUBJECTS★、ZHUKOV★
- **OnOtherCardEnterPlay**（19）：NAVAL PATROLS、MARINE PATROLS、CAMPAIGN TRAIL、DARING STRIKE、DAWN OPERATIONS、EXHAUST ALL OPTIONS、EXPOSED、PRECISION BOMBING、GRIM DAY★、HASTY REINFORCEMENTS、HEL★、INDUSTRIAL MIGHT、NAVAL BOMBARDMENT、SCREENING FORCE、SEA PATROL、SNOWSTORM、JUNGLE FEVER、SCORCHING SUN、TURNING POINT★
- **OnOtherCardDestroyed**（18）：7075 ALUMINIUM★、AWOKEN GIANT、BLOODY SICKLE、DO OR DIE、GLIDE BOMBING★、GREATER PURPOSE★、HONORIFICS、JUNGLE WARFARE★、MOTHER RUSSIA、OUT OF THE MIST、PERSIAN CORRIDOR★、RETRIBUTION★、SPECIAL ATTACK★、SPECIAL REINFORCEMENTS、UNITED WE STAND、UPRISING、VICTORY GARDEN★、IJN YAMATO★
- **OnOtherCardReset**（12）：COMMITTED CREW、CROSS OF LORRAINE、DARK DEEDS、EXILED FORCES、PATTON、PROLONGED SIEGE、RAID、ROMANIAN BRIDGEHEAD、SHIFTING ATTACK、STRONG BOND★、SUPPORT COLUMN★、VYSTREL COURSE★
- **OnOtherCardSuppressed**（12）：JUGGERNAUT、DARING STRIKE、DECOY TACTICS★、ESCAUT PLAN、FIREWALL★、GLIDE BOMBING★、HEL★、HONOR、NEW DOCTRINE★、SUPPORT COLUMN★、TURNING POINT★、URA!★
- **OnOtherCardDealDamage**（11）：ATS★、DEFEAT IN DETAIL★、HOME DEFENSE、IMPERIAL DECREE、ISLAND HOPPING★、LONG RANGE DESERT GROUP★、RECKLESS ASSAULT★、RIDE OF THE VALKYRIES★、ROAD TO BERLIN★、RULE THE SKIES★、VANGUARD★
- **OnAfterOtherCardLeaveBoardOrOwner**（8）：NAVAL PATROLS、JUGGERNAUT、EXPOSED、PRECISION BOMBING、GUERILLA WARFARE SCHOOL、SCREENING FORCE、SEA PATROL、STEALTH MISSION★
- **OnOtherCardLeaveBoardOrOwner**（8）：DARING STRIKE、EXPOSED、FINAL PUSH、HEL★、HONOR、NAVAL BOMBARDMENT、SCREENING FORCE、MELEE
- **OnOtherCardCreatedAlterCard**（7）：DIPLOMATIC ATTACHÉ、DUTY BOUND、ONE ROOF、RESERVES、SPECIAL REINFORCEMENTS、UPRISING、YANK★
- **OnOtherCardSpawnedInHand**（6）：NAVAL PATROLS、COMMITTED CREW、CROSS OF LORRAINE、GUERILLA WARFARE SCHOOL、PATTON、RAID
- **OnAfterOtherCardAttacks**（6）：CLAIM OBJECTIVE、ECHELON★、FIRST TO FIGHT★、SEEK AND DESTROY★、JUNGLE FEVER、SCORCHING SUN
- **OnOtherCardDrawnFromDeck**（5）：COMMITTED CREW、CROSS OF LORRAINE、GUERILLA WARFARE SCHOOL、PATTON、RAID
- **OnBeforeStartOfTurn**（5）：ATS★、ISLAND HOPPING★、RIDE OF THE VALKYRIES★、ROAD TO BERLIN★、RULE THE SKIES★
- **OnOtherCardDealDamageAddDamage**（3）：DESERT RAID、IMPERIAL DECREE、OVERRUN
- **OnOtherCardDiscarded**（2）：CROSS OF LORRAINE、GUERILLA WARFARE SCHOOL
- **OnOtherCovertCardPlayedFromHand**（2）：CROSS OF LORRAINE、GUERILLA WARFARE SCHOOL
- **OnOtherUnitUnpinned**（2）：CREEPING BARRAGE、FORCED SURRENDER★
- **OnOtherCardReceiveDamage**（2）：ARMING THE RESISTANCE、PUSH!
- **OnOtherCardMoveToFrontline**（2）：BOCAGE WARFARE★、CLAIM OBJECTIVE
- **OnAfterDeckChanged**（2）：BETASOM、RM ROMA
- **OnAfterExtraKreditSlotGain**（2）：ONE ROOF、SHINYO MOTORBOATS
- **OnFrontlineOwnershipChange**（2）：NAVAL BOMBARDMENT、NAVAL BOMBARDMENT
- **OnOtherCardMovedToLocationInHand**（1）：GUERILLA WARFARE SCHOOL
- **OnAfterOtherCardChangeAttack**（1）：PRECISION BOMBING
- **OnBeforeOtherCardAttacks**（1）：DECOY TACTICS★
- **OnBeforeOtherCardPlayedFromHand**（1）：DECOY TACTICS★
- **OnOtherCardRetreat**（1）：DECOY TACTICS★
- **OnOtherCardLocationMoved**（1）：SUPPORT COLUMN★
- **OnOtherCardOperationKreditsSpent**（1）：DETAILED RECON
- **OnAfterOtherCardDefenseIsSet**（1）：HOME DEFENSE
- **OnAfterOtherCardGainDefense**（1）：HOME DEFENSE
- **OnDestructionEffectTriggered**（1）：DUTY IS A MOUNTAIN★
- **OnOtherCardLoseSmokescreen**（1）：SHOCK ATTACK
- **OnAfterCardMovedToDeckFromHand**（1）：ENGAGE AT RANGE
- **OnOtherCardSurvivedCombat**（1）：FIRST TO FIGHT★
- **OnOtherCardAttacks**（1）：JUGGERNAUT
- **OnSuccesfulDiscard**（1）：SUPPLY CHAIN
- **OnDealDamageAddDamageAfterCalc**（1）：FIREBOMB
- **OnOtherCardDeveloped**（1）：YANK★

## 3. D 族：授予的自定义能力名

| 能力名 | 授予它的牌数 | 举例 | 谁读它 |
|---|---|---|---|
| trigger | 28 | ECHELON、STRONG BOND、KING'S OWN SCOTTISH | 授予者自己的钩子里 `HasCustomAbilityFromCard("trigger", 授予者ID)`（ECHELON 一族） |
| passive | 14 | LONG RANGE DESERT GROUP、COLOSSUS、MATERIALS | 同上（`passive` 授予 + 授予者钩子过滤） |
| destruction | 10 | GREATER PURPOSE、IJN YAMATO、JUNGLE WARFARE | 摧毁时触发（授予者/引擎） |
| custom | 8 | CORPS COMMAND、GLIDE BOMBING、ZHUKOV | （见对应卡/引擎，未逐个核对） |
| targetAbility | 4 | 15th RECCE、738. JÄGER REGIMENT、TSU REGIMENT | （见对应卡/引擎，未逐个核对） |
| excess | 3 | LANCASHIRE FUSILIERS、TURNING POINT、URA! | `ExecuteAttackCard`（已建模：`sim._excess_split`） |
| cantBeSuppressed | 3 | MAGINOT LINE、NO RETREAT、252nd RIFLES | （见对应卡/引擎，未逐个核对） |
| cantAttack:location | 3 | FW 200 CONDOR、ON THE HORIZON、FIREWALL | （见对应卡/引擎，未逐个核对） |
| cantBePinned | 3 | RIDING THE STORM、PANZER III-G、252nd RIFLES | （见对应卡/引擎，未逐个核对） |
| lethal | 2 | LONG RANGE DESERT GROUP、2nd PARA C | `CalculateDamageDealt`（已建模） |
| cantRetreat | 2 | MAGINOT LINE、NO RETREAT | `MakeCardRetreat`（已建模） |
| ignoreCantAttack_location | 2 | NEW DOCTRINE、NARA REGIMENT | （见对应卡/引擎，未逐个核对） |
| 7thScottishDisabled | 1 | 7th SCOTTISH BORDERERS | （见对应卡/引擎，未逐个核对） |
| canOperateWhilePinned | 1 | 14. PANZERGRENADIER | （见对应卡/引擎，未逐个核对） |
| pin_extra_turn | 1 | FORCED SURRENDER | （见对应卡/引擎，未逐个核对） |
| destroyEndOfTurn | 1 | DANUTA | （见对应卡/引擎，未逐个核对） |
| DisableOtherSniped | 1 | SNIPED | （见对应卡/引擎，未逐个核对） |

## 4. 其余族的牌（标题）

- **C 反制（gotcha，敌方回合从手牌触发）**（52）：AERIAL RECONNAISSANCE、AGAINST THE ODDS、AIR DEFENSE、AIRSTRIKE、A BRIDGE TOO FAR、CARELESS TALK、CLOSE CALL、COLD TRAP、COUNTER STRIKE、DECISIVE DEFENSE、DOWDING SYSTEM、ENEMY SPOTTED、ENTANGLEMENT、ENTRAPMENT、ENVELOP、EVASIVE ACTION、FIRST RESPONDERS、FOILED PLANS、FRESH RECRUITS、FRIENDLY FIRE、FROM THE DEEP、HEARTLAND DEFENSE、HIT THE DROP POINT、HMS SCEPTRE、HMS TALBOT、HOLD THE LINE、IN THE NAVY、INTERCEPTION、LAST DITCH、LIGHTNING STRIKE、LOST CONVOY、LURE、THE MERCHANT NAVY、MISSING、NATIONAL FIRE SERVICE、KM PRINZ EUGEN、RADAR ALERT、RESCUE MISSION、RETALIATION、RETRIBUTION……
- **E autoplay 标签（天气/预报：回合开始才落地）**（4）：MIST、RESISTANCE、GALE、BLUE SKY
- **F 倒计时（SetCountdown/DecrementCountdown）**（15）：DARK DEEDS、EXILED FORCES、GUNBOAT HIT、GUNBOAT RUN、MOBILIZATION、MOLOTOV COCKTAIL、PROLONGED SIEGE、ROMANIAN BRIDGEHEAD、VYSTREL COURSE、14th GUARD RIFLES、216th GUARDS、22nd GUARDS BRIGADE、BT-7 1937、INFANTRY REGIMENT 36、ME BF 109 G FI
- **G 游戏限制（AddGameplayRestriction，带期限的一侧禁令）**（19）：KM ADMIRAL HIPPER、SUPPLY INTERCEPTED、ELUSIVE FORCE、MUD、REICHSBANK、KM TIRPITZ、U-48、DANZIG、MEDJEZ EL BAB、CHERBOURG、MARIVELES、KALININ、MOSCOW、TULA、HENDERSON FIELD、HENDERSON FIELD、60th CAVALRY REGIMENT、FINNISH BOYS、PANTHER A
- **H 触发队列（AddToTriggerQueue）**（6）：AIR ESCORT、BAKER STREET IRREGULARS、BPF、RETRIBUTION、THE ROCK OF GIBRALTAR、YANK
- **I 侧效果（AddGameplaySideEffect，如 blockgotcha）**（1）：C6N SAIUN
- **J 回合末清除（AddAttackUntilEndOfTurn / destroyEndOfTurn）**（28）：BARRAGE、BLITZKRIEG、BURNING SUN、BURST OF FIRE、CHASE THEM DOWN、CLOSE COMBAT、FOR THE EMPEROR、OLD HARES、PATRIOTIC FIRESTORM、PLAN D、RALLY、SCATTER TACTICS、HEATWAVE、HEATWAVE、JUNGLE FEVER、URA!、ADMIRAL YAMAMOTO、37mm M1 AA GUN、4th ALABAMA、4TP、Sd Kfz 250/9、B-4 203mm HOWITZER、DANUTA、DEFIANT Mk I、KAWASAKI Ki-10、MODEL 25、Sd Kfz 10 PAK 38、TYPE 98 Ke-Ni

## 5. 读法 / 局限

* `usedTriggers` 只列了 752 张牌有；真实钩子集合以 `public void On…` 覆写为准（本表用覆写）。
* 单位的“留下来的钩子”（B 族）已由读侧在活盘面上跑；**假想打出（sim 里的手牌）** 才需要 registry 把它们“布置”进 sim。
* 同一张牌可落进多族。族与族不互斥，表内张数不可相加。
* 导出目录：`D:/Kards/reverse-data/exports-1.60.27292.launcher-only-decompiled-BP/kards/Content/Blueprints/Cards`
