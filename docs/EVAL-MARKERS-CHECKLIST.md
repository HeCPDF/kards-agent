# §8-9 四个标记的验证清单（`+intel` / `+deckchg` / `+shuffled` / `+hooks`）

> 用途：TODO A4。四个标记是 `rule.eff_src` 值里的**后缀**（`rule.py:1148/1169` 等拼进去），
> 落在 `rule-live-*.jsonl` 的 `probe.eff_src`。读法：把日志里所有 `eff_src` 的值收集起来，
> 看有没有 `"<基源>+<标记>"`（例如 `"vm+intel"`）。**"没出现"时先按下面的门槛核对是"卡不对"还是"条件没满足"。**
>
> 现状（2026-10-02）：5 局实机里四个标记**都还没出现**；`death_fx` 非空已用同一批日志验证
> （`fx_meta.death.entries=1`）。
>
> ★★ **同日追加：四个标记里有三个的"没出现"不是牌组问题，而是代码恒空** —— 已修：
> | 标记 | 空因 | 修复 | 证据 |
> |---|---|---|---|
> | `+intel` | 三族触发读 `record_effects(...)["outcomes"]`（这个键只在 `enumerate_effects` 里）⇒ 恒空 | `6836c6f` | 假盘面 + 真 VM：`_intel_triggers → [[19, {"buff":[1,1]}]]`（`probe_intel_fixed.json`） |
> | `+hooks` | ① `exclude=[Card]` 不可哈希 ⇒ `TypeError` 被 `except` 吞；② `triggers._run_hook` 没传 `board/my_seat` ⇒ 停在 `IsSideActive` | `146d4b5` | `_play_hooks_triggers → [[15, {"buff":[1,1]}]]`、`run_play_hooks` 命中 SASEBO `OnCounterMeasureTriggered`（`probe_hooks_deep2.json`） |
> | `+shuffled` | 同 ① 的取效果方式问题 | `146d4b5` | `_deck_shuffled_triggers → [[16, {"damage":5}]]`（`probe_markers_vm.json`） |
>
> ★ **`+shuffled` 真盘面复核（2026-10-02 晚，用户手动局）**：用户亲自部署 SABAE 后，
> 用当时快照跑真实 rule 路径 —— `_deck_shuffled_triggers(SABAE 在场上, eff={deck_shuffle,
> deck_shuffle_skip=True}) → [[16, {"damage": 9}]]`（9 = 场上单位数，BP 兜底；单跑钩子
> eff 为空，值是 rule 的 SABAE 兜底给的）。同时核实：两张洗切卡**在手牌**时不命中
> （用户口径：`FetchAllCardsWithEventTrigger(0x16)` 不触发手牌）⇒ 之前没出现是"卡没上场 /
> 没发生 skip 洗牌"，不是代码问题。见 `probe_shuffled_retest.json`。
>
> 也就是说：**下一局只要凑齐卡，日志里就该看到标记**；若仍没有，先看 `probe.marker_err`
> 与 `probe.intel_dbg["tried"]`（会写明"跑了但为什么空"），再怀疑牌组。
>
> ★ **构建级证据（2026-10-02，`probe_deckchg_cards.json`）**：拿静态卡表
> （`GameState+0x670`，2019 张）逐类查钩子，**四个标记涉及的钩子在 1.60 全都在、都有字节码**：
>
> | 标记 | 覆写数 | 键名（FName） |
> |---|---|---|
> | `+intel` | 6/6 | `card_unit_16th_tarnow_regiment`、`card_unit_7th_scottish_borderers`、`card_unit_intel_fusiliers`（显示名 INNISKILLING FUSILIERS）、`card_unit_legion_pol`、`card_unit_nakajima_b5n2`、`card_unit_thunder_division` |
> | `+shuffled` | 5/5 | `card_unit_110e_regiment_motorize`、`card_unit_3rd_kure_snlf`、`card_unit_3rd_mixed_regiment`、`card_unit_la_division_leclerc`、`card_unit_sabae_regiment` |
> | `+deckchg` | 3/3 | `card_unit_lovat_scouts`、`card_event_rm_roma`、`card_event_betasom` |
>
> ⇒ **四个标记都不缺实现**，只缺"对应卡真的在场上/牌库里"。`+deckchg` 的三张当前牌组没有
> （`probe_deckchg_cdo.json` 里那两条 `complete=true` 的 RM ROMA/BETASOM 是用**CDO**
> 空跑的，CDO 的 `cardFunction=0` ⇒ 结论不可信；真实实例的门槛是 `cardFunction` 非空、
> 其类上有 `GetDeckByside`，见 `probe_cardfunction_deck.json` 与 同类教训）。

## `+intel` —— 情报触发（0x1C `OnIntelTriggered`）

- 代码路径：`rule._hand_eff` → `rule._intel_triggers`（`rule.py:1221`），两个门槛**都要**满足：
  1. **打出的那张牌带情报值**：快照 `card.cipher > 0`，或它的效果摘要里有 `intel_seen > 0`
     （例如 `CRUISER SCOUTS` 的 `SetCardsSeenByCipher(3)`）。`cipher` 一律走**快照字段**
     （用户口径：不让代码直读 `+0x25C`，缓存才是唯一数据源）。
  2. **场上（本方、前排/后排）有覆写 `OnIntelTriggered` 的卡**——全库只有 6 张：
     `16th TARNOW REGIMENT`（16th Tarnow Regiment）、`7th SCOTTISH BORDERERS`、
     `INTEL FUSILIERS`、`LEGIONS`（波兰军团）、`NAKAJIMA B5N2`、`THUNDER DIVISION`。
     同 side 才触发；打出的那张自己会被跳过（快照里它还在手牌）。
- 诊断（2026-10-02 加）：`probe.intel_dbg` 会直接给答案 ——
  `{why: "intel_n<=0（打出的牌没带情报值）", card, cipher, intel_seen}` 或
  `{why: "场上没有覆写 OnIntelTriggered 的本方卡", card, intel_n, board_local:[…]}`。
- 最小可复现组合（同一副牌里）：`CRUISER SCOUTS`（情报源）+ 任意一张上面 6 张（触发者）
  同时在手/场；情报源打出后，触发者**已在场**才会计入。

## `+deckchg` —— 牌库变化触发（0x3 `OnAfterDeckChanged`）

- 代码路径：`rule._hand_eff` → `rule._deck_changed_triggers`（`rule.py` 里 `+deckchg` 处）。
- **只有 3 张卡覆写 0x3**：`LOVAT SCOUTS`（英）、`RM ROMA`、`BETASOM`（意）。
  ⇒ 日系/美系牌组**不可能**出现这个标记，必须换含这三张的牌组。
- 触发时机：洗牌/塞牌（`SpawnCardInDeckBySide`）/洗入（`StealCardFromBoardToDeck`）/
  移牌/弃牌等会调 `ExecuteOnAfterDeckChanged` 的动词之后。

## `+shuffled` —— 洗牌触发（0x16 `OnDeckShuffled`）

- 代码路径：`rule._hand_eff` → `rule._deck_shuffled_triggers`（`rule.py:1374` 一带）。
- 覆写 0x16 的卡（全库 5 张）：`110e REGIMENT MOTORIZE`、`3RD KURE SNLF`、
  **`3RD MIXED REGIMENT`**、`LA DIVISION LECLERC`、**`SABAE REGIMENT`**。
  （后两张在情报/洗切牌组里就能见到。）
- 关键门槛：BP 里**只有 `ShuffleDeckBySide(side, skipSubAction=true, …)` 才 fetch 0x16**
  （`false` 只发 `NotifyNewDeck`）——`effectvm` 把这条记成 `deck_shuffle_skip`。
  ⇒ 要凑"洗牌方传 true"的那种洗牌（例如 `SpawnCardInDeckBySide(..., shuffle=true)`、
  某些卡自己的洗切）。

## `+hooks` —— 打出卡触发族（0x2B / 反制族）

- 代码路径：`rule._play_hooks_triggers`（`rule.py:1809`）→ `triggers.run_play_hooks`
  （`OnOtherCardEnterPlay`(0x2B) + `OnBefore/OnOtherCardPlayedFromHand`(0x13/0x33) +
  `OnCounterMeasureTriggered`(0x15)）。
- 代表卡（用户点名）：**`5TH SASEBO SNLF`**（自己 +1/+1）。实机只读复核（`146d4b5`）：
  命中的钩子是 **`OnCounterMeasureTriggered`**（`run_play_hooks` 的第三条），
  `OnBefore/OnOtherCardPlayedFromHand` 在那次复现里没产出（条件未过，不是 bug）。
  ⇒ 实操门槛很低：**SASEBO 在场上 + 我们再打一张牌**即可（`_play_hooks_triggers` 在
  每个"打出"决策上都会跑）。

## 一次跑齐的实操建议

1. 牌组至少含：`CRUISER SCOUTS`（情报源）+ 6 张触发者里的任意 1 张（`+intel`）、
   `5TH SASEBO SNLF`（`+hooks`）、`3RD MIXED REGIMENT` 或 `SABAE REGIMENT`（`+shuffled`）。
2. `+deckchg` 单独换一副含 `LOVAT SCOUTS`/`RM ROMA`/`BETASOM` 的牌组（英/意）再跑。
3. 每局跑完用 `_audit.py`（会打印 `eff_src` 收集结果）核对；`intel_dbg` 也在
   `probe` 里。

## 2026-10-02 实机只读排查（不靠对局，直接空跑钩子）

用 `probe_intel_gate.py`（在活进程里对"情报源/触发者"逐张空跑 VM）实测到：

| 卡 | 修复前停在哪 | 现在 | 结论 |
|---|---|---|---|
| `16th TARNOW REGIMENT`（触发者） | `IsSideActive 需要 BoardState（轮到谁不在卡对象上）` | **`stopped: None`（链跑通）** | 已修：`effectvm.record_effects/enumerate_effects` 新增 `board`/`my_seat` 透传（rule/triggers 调用点都传快照）—— 盘面级原语（`IsSideActive`/`getKreditBySide`）以前在 effectvm 路径里**永远缺 BoardState**，整条钩子链一撞就断 |
| `CRUISER SCOUTS`（情报源） | 停在 `AMatchControllerV2::IsReconnectMatch` | 仍停在它 | **未实现的原生**：它是 C++（不是蓝图，导出里是 `AMatchControllerV2::IsReconnectMatch(isReconnecting, clientMulliganDone, otherMulliganDone)`）；VM 撞到就断 ⇒ `SetCardsSeenByCipher` 到不了 ⇒ `intel_seen` 空 ⇒ `+intel` 无法触发。**这就是 `+intel` 至今不出现的直接原因**（不是标记逻辑问题） |

⇒ 想验 `+intel`，先补 `IsReconnectMatch` 这个原生（三个出参：`isReconnecting` /
`clientMulliganDone` / `otherMulliganDone`，应从 `MatchController_C` 对象上按反射读同名字段，
读不到就如实抛 Unimplemented —— 别写死 False）。`cipher`（`ptr+0x25C`，IDA 标 HasIntel/getIntel）
实测对 `CRUISER SCOUTS` 读 0，所以第一道门槛只能靠 `intel_seen`。

### 20:24 追加：那三个名字**不是类字段**

- `GObjects-Dump-WithProperties.txt` 里 `isReconnecting`/`clientMulliganDone`/`otherMulliganDone`
  出现在 **`IsReconnectMatch` 的参数表**（`[00000000..00000002]`）⇒ 它们是**函数出参**，
  反射（`props.find_prop`）**不可能**在 `MatchController_C` 上找到 ⇒ 只能读函数体才能复刻。
- 只读实测（`probe_mc_chain.py`，走 CDO 的类链）列出运行时与该函数相关的**真实字段**：
  `reconnectInSameTurn`(Bool@760，父类)、`MulliganData`(Struct@4008)、
  `mulliganReplacementReceived`(Bool@4056)、`reconnectLoading`(ObjectProperty@4104)。
  ⇒ **这就是给 IDA 的输入**：读 `AMatchControllerV2::IsReconnectMatch`，看三个出参是这四个字段的
  什么组合。读完再改 `kardsmem/cardnatives.py::_is_reconnect_match`（现在如实抛 Unimplemented，
  并把这四个字段名带在错误信息里）。
