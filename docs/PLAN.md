# 落地计划（2026-10-01 夜）

> **看板说明（2026-10-02）**：本文引用的"TODO §x"均指旧版 `TODO-ARCHIVE-2026-10-02.md` 的小节；**现行待办只看 `TODO.md`**，规范见 `CLAUDE.md` "待办（TODO）与文档维护规范"。

范围 = 旧 TODO 全部条目（§1 原生函数、§2 随机流、§3 时序/攻击、§3b 延迟、§3b'' 崩溃、§3c 已知 bug、§3d 预报、§3e 触发钩子、§3f/§3g 新增、§4 杂项）。
总目标（用户 /goal）：打完 2 局；期间尽量修 bug；没修完就多打；直到没有新问题出现、旧问题修好。
硬指标：
- 攻击在**真鼠标移出窗口、游戏在后台**时稳定成功（用户："目标是移出鼠标后一直能打"）。
- 单回合 ≤ 35 s（连续 3 局，≥90% 的回合达标）；部署 ≤ 3 s。
- 发给服务端的信息流必须与真人鼠标操作等价（只改"等待方式/内部写入"，不跳过悬停等事件）。

## 0. 当前状态（2026-10-03 更新；旧版状态表已删，历史看回执台账与 git）
| 项 | 状态 |
|---|---|
| 攻击静默吞掉 | **已修并验证**（`ops.inject._attack_once` 写 `location_enum=7,row=1`；累计 30/30）；用户观察：补写之后鼠标在不在窗口内都不再影响脚本 |
| 盘外评估/模拟（`sim/`、`evaluation/`、`boardeval`、`triggers`、`effectvm`） | 骨架 + 事件队列 + 触发族（反制/死亡/上线/抽牌/压制·定住/回合/揭示/离开前线·撤退）+ **老兵/治疗/偷牌/战斗钩子级后续（10-03）**已落地；5 个蓝图函数（偷牌/老兵/转化/战斗/治疗）已按 BP 逐行对齐；仍记缺口：转化的入场钩子、战斗的受伤链、`AbilitiesChanged` 等（`TODO.md` A5）。大部分只在离线/假 VM 下验过，**实机待验** |
| 总原则（10-03 用户定） | 行动 = 动作 + 其后全部选择（选择路径），预报 = 9 个行动；机制按能力探测分支、新版本靠语义指纹降级——见 `ARCH-RELEASE-VCS.md` §7、`TODO.md` H3–H5 |
| 构建/RVA | 过渡实现：按运行中游戏的版本号.分支选表（`kardsmem/version.py`）；**目标**：运行时扫描 RVA + 缓存 + 种子表（`ARCH-RELEASE-VCS.md`、`kardsmem/rvascan.py` 第一阶段已做，S2 需游戏） |
| 控制面板 | `gui/`（`run_gui.bat`）：自动对局编排（局数/自动下一局/立即停）、决策历史、事件日志、监听器启停、版本识别；离线测过，**未实机** |
| 延迟 | 软目标 ≤35 s/回合（用户：**不许降束宽**）。10-02 日志 `t_decide` 中位 1.1–2.6 s、P90 5–8.6 s、最大 ~20 s；`t_exec` 另有尖峰；见 `TODO.md` B1 |
| 稳定性 | frida Temp 泄漏已修（待实机）；跨局缓存已修；热重载零参 `super()` 问题仍在（C2） |
| 游戏/监听器 | 游戏 10-03 被用户要求关闭；监听器进程以面板"停止监听器"为准 |

## 1. 通用流程（每个阶段都按这个走）

### 1.1 开局测试流程（"测试流程"）
前置：D 盘剩余空间 ≥ 30 GB（ProcDump 单份转储 3.3 GB）；`hot_reload.txt` 不存在；`_nn_scratch` 里没有遗留的 `attack_play_result.json` / `*_result.json` 标志文件（上次就因此触发了收尾脚本）。
1. **先开游戏**（ProcDump 先起则游戏起不来），窗口 1024×768，进训练模式卡组页。
2. 起崩溃监视（`crash_watch_run.py`；`-t` 无异常流会产生大转储，不用）。
3. 起常驻监听器：`cd D:\Kards\_nn_scratch && set KARDS_BUILD=launcher_default && python -u live_session.py`。**只用常驻监听器，不另起会注入 frida 的短命进程**（会崩游戏）。
4. 前台截图确认（`front2.py` 置前后截图）：卡组页、"训练模式"高亮、对手 player_id 为负。确认后点"开始"（客户端约 (900,642)）。
5. 往 `live_cmd.txt` 追加 `player.play.play(sess, live=True, turns=40, ...)`；结果看 `live_log.txt` / `ls.out` / `kards-data/nn/logs/rule-live-*.jsonl`。
6. 条件控制：游戏在后台即可；**不再要求把真鼠标停到窗外**（用户 2026-10-02 观察：鼠标是否在窗内已不影响）；拐棍已删。
7. 监视（Monitor，固定日志路径，不用"最新文件"）：每回合耗时、攻击成败、`x_timing`、拒绝原因、`layer_gone` 等。
8. 收尾：先删旧标志文件；**中途停局只能走 UI**（齿轮 (978,36) → 投降 (870,57)），监听器被 `player.play.play` 占住收不到命令；**投降由用户决定**，不擅自投降。结束后 `echo quit >> live_cmd.txt` 退监听器；不要强杀监听器/ProcDump（用 `stop_pd.py` / `crash_watch.stop`）。
9. 写报告到 `_nn_scratch/<主题>_report.md`，结论回填 `TODO.md` 对应条目（先改看板，再写细节）。

### 1.2 落地流程（"落地流程"，每个改动都要走完）
1. **离线**：`cd D:\Kards\kards-agent && python -c "import ops.inject, agent.nn, player.rule"` → `python -m kardsmem selftest`（两个构建 `KARDS_BUILD` 都过）→ `python test_choose_verbs.py`。新增求值器/判据必须带"换一张不同数据的卡，答案要跟着变"的断言（弯路 #11）。
2. **热重载优先**：写 `hot_reload.txt`（模块名列表，空 = 默认），`Loop.step` 下一步自动重载，结果见 `hot_reload.log`。限制：JS/CModule 的改动、`__init__` 里新增的实例字段（用 `getattr(self,'x',默认)`）仍要重启监听器。
3. **实机验证**：按 1.1 跑一局，达标标准见各阶段。**改了任何等待/时序，必须在"游戏后台"条件下回归**（弯路 #29）。
4. **只写动作流判据**：动作成败只认动作流，不认副作用（弯路 #22/#28）；判据只"挑"不"否决"。
5. **文档**：更新 `TODO.md`（先改看板：改状态；完成则登记回执并从看板删除）、`CLAUDE.md`（新弯路/状态段）、记忆目录（`project_bot_session_2026_10_01.md` 等）。
6. **回执**：在本文件《回执台账》（下表）登记一行 —— 改动文件 + 证据（测试输出 / `rule-live-*.jsonl` / cdb / commit）。**没有回执的条目不算"已落地"**（用户 2026-10-02 要求："plan 需要添加回执"）。
7. **提交**：中文提交信息（做了什么 + 为什么 + 证据），结尾 `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`。**`kards-agent/` 是伞仓库子树，不要在里面裸 `git push`**；公开只走 `subtree split`（见 CLAUDE.md "发布纪律"）。
8. 不改的东西：`tools/crashdump.py`、`tools/CRASHDUMP.md`（DeepSeek 维护）、`OCR-Kards-Auto/`。

## 回执台账（落地登记）

> 规则（用户 2026-10-02 定）：每落地一项就在本表补一行 —— **改动（文件）+ 回执（证据）**。
> 证据优先级：实机日志/动作流 > 离线测试输出 > 代码位置。空着 = 还没落地。

| 日期 | 条目 | 改动（文件） | 回执（证据） |
|---|---|---|---|
| 10-01 | 攻击静默吞掉修复 | `ops/inject.py::_attack_once` 提交批次写 `location_enum=7, row=1` | 三局 **30/30**（16/16 + 6/6 + 8/8，全部 `cursor_in_window=false`）；`_nn_scratch/attack_test_report*.md` |
| 10-01 | 两条拐棍删除 | `agent/nn.py`（`_park_cursor_outside`/`_cursor_to_target` 函数+调用+两个环境变量） | nopark 局 **16/16** + 两局 6/6、8/8；离线 `import`/`selftest`/`test_choose_verbs` 全过 |
| 10-01 | 随机流种子定位 | `kardsmem/rng.py::card_functions_ptr` | 20 Hz 只读采样 6759 条；种子 74→105→127→142→156 全在 LCG 上（4/4）；`_nn_scratch/rng_test_report.md` |
| 10-01 | 预报三选一预测 | `kardsmem/forecast_pool.py` + `_nn_scratch/forecast_validator.py` | 手动局 rain 三张**逐张同序+效果文本一致**，面板后种子正好 +28 步；`forecast_validation_report.md` |
| 10-02 | 洗牌型三选一预测 + 留痕 | `_nn_scratch/choose_spawn_predict.py`、`player/rule.py`（`meta.shuffle_pick_check`） | 面板 2/2 命中；出牌前后种子跳 **18 步 = 池大小**；`choose_spawn_findings.md` |
| 10-02 | 9 路评估接入 `choose_pick` | `player/rule.py::score_candidates/fc_best_type` | 实机 `meta.pick_eval/forecast_eval/pick_timing` 落盘；天气牌效果仍 gap（TODO §3i-4） |
| 10-02 | **跨局缓存（座位/Logic）修复** | `kardsmem/cards.py`、`kardsmem/matchlog.py`、`agent/session.py` | 离线：`test_seat_cache.py` **22/22**、`kardsmem selftest` 两构建 PASS、`test_choose_verbs` PASS；**实机待验**（下一局 0 个"不在我方手牌"、换牌 `exec=True`） |
| 10-02 | **指令评估不再编造价值** | `player/rule.py`（`PARAMS["unknown_order_est"]=0.0`，两处 gap 分支） | `python -m player.rule` 0 失败；只读实机复核：6K 天气牌 +3.24 → **-2.16**（2K/4K = -0.72/-1.44）；11:34 热重载入局 |
| 10-02 | **§8-1 指挥点夹取** | `policy/boardeval.py`（`kredit_max` + `_clampf`）、`player/rule.py`（两处 `from_cards` 传 `st.max_possible_kredits`） | boardeval 自检 4/4：23+3→24、max=10→10、−30→0、槽 20+10→24 |
| 10-02 | **§8-3 未消费键（第一批）** | `semantics/effectvm.py`（`*_aoe` 保留 id、`damage_own_hq` 拆键）、`policy/boardeval.py`（destroy/remove/pin/to_deck/damage aoe、heal+mdef、steal、veteran、opp_cards/opp_spawn、evaluate 扣分） | boardeval 自检 13/13（aoe_ids ×5、heal、damage_own_hq、opp_cards、opp_spawn、steal、veteran、kredit clamp ×4）；全套离线（import、kardsmem selftest ×2、7 个 `test_*.py`、boardeval/effectvm/triggers/rule 自检）全过 |
| 10-02 | **U1 实机验证（结论：否定）** | `_nn_scratch/probe_eval9.py`（外部只读） | 开局实机：全卡 80 张 ×5 条加密记录 = 400 条，**全部 mult=1939**、0 个 `mult==0` ⇒ "手牌指令牌 mult==0 ⇒ getTotalDefense=99" 无触发场景（指令牌正常公式=0） |
| 10-02 | **`_death_fx` 数据通路验证**（`5ab7149` 修复） | 真 VM：`card_unit_5th_sasebo_snlf_C` 实例跑 `OnDestroyed` | `record_effects` 返回含 `eff = {'draw': 1}`、`stopped=None`；完整"真局面非空"待含 `OnDestroyed` 覆写卡的牌组（当前局牌组 0 张，已确认） |
| 10-02 | **合并 NATIVE-SPEC-GAPS D1–D6** | `kardsmem/vm.py`（容器原语 8 个 + dict 影子容器）、`kismetlib.py`（RightChop/Set_ToArray）、`cardnatives.py`（位置枚举/tag/加密哨兵）、`cards.py`（records_raw） | `test_container_prims.py` 13 断言、`test_cipher_sentinel.py` 11 断言、kardsmem selftest 双构建全过；提交 `2e4d0c7` |
| 10-02 | **合并组 A/B/C** | `cardnatives.py`（getTotalDefense/HeavyArmor/IsDamaged/getAndDecrypt*/TempBuff、customName/customJson 大小写） | `test_native_gapless_reads.py` 25 断言全过；提交 `66385cd`/`06cc653` |
| 10-02 | **组 F 静态卡提供者** | `kardsmem/gs.py::make_static_card_provider`（GS+0x670，2019 张）+ `effectvm/forecast` 注入 | 实机（launcher）`test_static_provider.py` **10/10**：表 2019 张、Kyoto 3 费 1/4、Sasebo 1 费 1/1、GetStaticKredits/Attack/StaticCardExists、大小写不敏感 |
| 10-02 | **§8-6 断言落盘 + §8-7 去重核对** | `test_hook_chain_assertions.py`（新） | 10/10：反制先手、0x18 重放、salvage_ids、例外表 11 条、死亡链交错顺序、`to_fx` 默认不输出摧毁桶（`dedupe_death=False` 才输出） |
| 10-02 | **组 D setAndEncrypt 影子 + 自检隔离** | `effectvm.py`（Recorder.shadow_stats/ptr_ids、写侧拦截、to_effects set_*）、`cardnatives.py`（影子优先）、`boardeval.py`（set_* 消费 + gaps）、`rule.py`（selftest 隔离） | `test_set_encrypt_shadow.py` 10/10；两个构建 `python -m player.rule` 0 失败 |
| 10-02 | **组 E/G：虚事件默认值断言 + JSON 库** | `test_virtual_event_defaults.py`（新）、`kismetlib.py`（BlueprintJsonLibrary 全套 17 个）、`test_json_library.py`（新） | 组 E 7/7（4 个蓝虚事件默认 False/空串）；组 G 22/22（HalfFromZero/LexFromString/SanitizeFloat/Null-存在/共享引用/过滤 None/往返）；离线全套全过 |
| 10-02 | **部署抉择按选项估值** | `player/rule.py::_option_score` + `_vm_choice_outcomes`（VM 抉择分支优先，文字兜底） | 实机只读：`card_unit_5th_rangers` 的 `enumerate_effects` = `choice=True, outcomes=[{"opcost":0},{"buff":[4,4]}]`；选项读数 2.4 vs 4.2 ⇒ 选 +4/+4；断言覆盖 VM/label 两条路径；11:44 热重载入局 |
| 10-02 | **"VM 跑完但空" ≠ 缺口** | `player/rule.py::_hand_eff`（`eff_src="vm(空)"`，不记 gap、不编造） | 6K 骄阳无空军时 `complete=True、{}` ⇒ 空值不再变 +3.24；只读复核 `_hand_eff → {"_src":"vm","_vm_empty":true}`、gaps 里没有它 |
| 10-02 | **开局硬闸门判据修正**（同日第二次事故：闸门第一版用了 `Locator.in_battle`，那个**主菜单也为真** ⇒ 一局都开不了） | `_nn_scratch/play_guard.py`（`in_match` = `GameState.match_active`）、`open_next.py`/`continue_ranked.py`/`start_ranked_now.py`（记录键 `in_match`）、`CLAUDE.md`（弯路 #42，修 #41 撞号） | 只读实机（主菜单）：`match_active=False` ⇒ 放行、`decks=[]` ⇒ 拦（"牌组列表读不到"）；`kardsmem selftest` 两构建 PASS（F 段同进程 `match_active=False` + `[INFO] 不在对局中`）；离线全套（import、`test_choose_verbs`、8 个 `test_*.py`、boardeval/effectvm/triggers/rule 自检）全过 |
| 10-02 | **包边界骨架（`EVAL-ARCHITECTURE.md` §1.1 补充 0）** | `sim/{__init__,state,chain}.py`（`State` = `boardeval.Sim` 子类，`simulate()` 单步 + 如实缺口）、`evaluation/{__init__,context,value}.py`（`value/delta` 转调 boardeval；`ctx` 未接线 ⇒ 明确报错）、`test_arch_rules.py`（依赖方向 + 过渡桥白名单 2 条）、`test_sim_skeleton.py` | `test_arch_rules.py` 7/7、`test_sim_skeleton.py` 11/11（含"同输入同输出"行为不变、`complete=False` + 缺口、传 `ctx` 报错、换动作答案跟着变、深拷贝）；离线全套全过 |
| 10-02 | **事件队列/分发器（§1.1 补充 3）+ 抽牌链下沉** | `sim/dispatch.py`（`Event/Result/Run/Dispatcher`：`dfs`/`bfs`、`stop`=`SetStopFurtherActions`、步数/深度上限 → 缺口、有缺口 ⇒ `complete=False`）、`sim/chain.py`（`apply_fatigue` + `draw_chain` 从 boardeval 迁入，**FIFO** 保持旧语义）、`policy/boardeval.py`（`_res_eff` 抽牌队列 → `draw_chain`，`_fatigue_once` 转调 sim）、`test_dispatch_queue.py`、`test_draw_chain.py` | 队列 12/12、抽牌链 11/11（FIFO 顺序 A/B/C、疲劳 3→4、上限缺口、`DRAW_CAP` 防漂、换牌顶答案跟着变）；`policy.boardeval` 自检 0 失败；离线全套（rule/effectvm/triggers/boardeval 自检 + 10 个 `test_*.py`）全过 |
| 10-02 | **§8-3 收尾：`fight` 落地 + `convert` 记缺口** | `sim/effects.py`（`apply_fight`）、`semantics/effectvm.py`（`MakeCardsFight` 卡指针 → `rec.ptr_ids` → card_id）、`policy/boardeval.py`（消费 `fight`；`convert` 只记缺口不动状态）、`test_fight_convert.py` | 9/9：3/3 vs 2/2 ⇒ 2 号阵亡、1 号剩 1 防；反向 1/1 vs 3/3 答案跟着变；目标不在场 / 纯布尔 / 换不出 id 都**记缺口不结算**；换一组指针 ⇒ 出 [5,6]；effectvm/boardeval/rule/triggers 自检 0 失败 |
| 10-02 | **§8-3 收口：效果键"产出=消费"审计** | `test_effect_keys_consumed.py`（扫 `effectvm` 产出键 vs `boardeval`/`rule`/`sim` 消费键；`self_damage` 进 TOLERATED 并写原因） | 4/4：32 个产出键**全部**有消费（或写明丢弃）⇒ §8-3"消费未消费的键"这一项**收口**；这条测试同时机械执行"不得再新增 eff 摘要键"的约定 |
| 10-02 | **§8-4 同回合计价的断言** | `test_same_turn_pricing.py`（重复牌 ⇒ Δ==0、缺口 ⇒ 价值 0 不按费用补、空效果 ⇒ 0、fight 近似 ⇒ 只按真实状态变化给分） | 7/7：已有 `guard` 再给 ⇒ 0.0000；换成没有 `guard` 的单位 ⇒ +0.5000（`kw_guard`）；`convert` 缺口 ⇒ 0.0000 且带缺口；fight 换掉 2/2 ⇒ +2.2 且带缺口。实现上这四条是 Δ 的自然结果（评估侧不加规则），测试负责钉住 |
| 10-02 | **§8-8 第一片：上线族（0x32）落地** | `semantics/triggers.py`（`MOVE_HOOK_SPECS`/`run_move_frontline`/`move_effects`；顺序照本构建 BP 导出 @15297：反制先手 → 自己（未压制才跑）→ 隐蔽未揭示整段跳过 → 0x32 列表 `cardsDone` 去重；`GetStopFurtherActions` 未建模写进 `meta`）、`policy/boardeval.py`（`Sim.move_fx` + `sim_move` 结算 + `from_cards` 参数）、`player/rule.py`（`_move_fx` 预检/缓存/3s 预算；两处 `from_cards` 接线）、`test_move_frontline.py` | 18/18：顺序/自身去重/隐蔽跳过（已揭示不跳过）/被压制自己不算/被压制旁观者被丢；`sim_move` 结算 +2 攻、换数据答案跟着变、全局效果（抽 1）也结算、`copy()` 带 `move_fx`；triggers/boardeval/rule/effectvm 自检 0 失败；离线 16 个测试全过 |
| 10-02 | **§8-8 第二片：抽到族（0x2A 旁观者段）落地** | `semantics/triggers.py`（`DRAW_HOOK_SPECS`/`run_drawn_from_deck`/`drawn_effects`；顺序照 BP 导出 @13783：抽到那张自己先跑（评估里由既有 `_on_draw` 承担）→ 0x2A 列表里 `cardID != DrawnCardID` 的逐个）、`sim/chain.py`（`draw_chain(bystander_fx=…)`：自己先、旁观者后，两边的"再抽 N"都只入队）、`policy/boardeval.py`（`Sim.draw_fx` + `from_cards`）、`player/rule.py`（`_draw_fx`：按牌库去重 id 预计算、3s 预算、缓存）、`test_draw_bystanders.py` | 12/12：自己跳过/手牌旁观者也看/形参名与 BP 一致/换 id 答案跟着变；`draw_fx` 命中才生效、旁观者的"再抽"入队并连到疲劳（draws=3、fatigue=1）、与自己 `_on_draw` 不混算；离线全过 |
| 10-02 | **纯重构：预计算钩子表统一成 `Sim.event_fx`**（`move_fx`/`draw_fx` → `{事件种类: {卡 id: eff}}`） | `policy/boardeval.py`（`EVENT_FX_KINDS=("move","draw")` + 未登记种类**报错**；`sim_move`/`_res_eff`/`from_cards`/`copy` 改走 `event_fx`）、`player/rule.py`（两处 `from_cards(event_fx=…)`）、`test_move_frontline.py`/`test_draw_bystanders.py`（改 API + 新增"未登记种类报错"断言） | 行为不变的特征测试全过：`test_move_frontline.py` 20/20、`test_draw_bystanders.py` 12/12、10 个 `test_*.py` 全 PASS、boardeval/rule/triggers/effectvm 自检 0 失败。**实机未验证**（改的是数据结构，不改规则） |
| 10-02 | **§8-8 第三片：压制/定住族（0x3A / 0x3D）+ 压制真正清关键词** | `semantics/effectvm.py`（三个动词分开：`SuppressUnit→"suppress"`、`PinUnit→"pin"`、`SuppressMultipleUnits→"suppress_aoe"`+id 列表——以前都记 `"pin"`，0x3A 根本选不出来）、`sim/effects.py`（`apply_suppress`：清关键词/重甲/指向税 + 置压制位 + 如实记缺口）、`policy/boardeval.py`（消费 `suppress`/`suppress_aoe_ids`；新增 `_event_fx_apply` 统一结算预计算钩子后果）、`semantics/triggers.py`（`SUPPRESS_HOOK_SPECS`/`PIN_HOOK_SPECS` + `run_target_event` + `suppress_effects`/`pin_effects`；形参/触发号出处 = BP @7456 / @971）、`player/rule.py`（`_unit_event_fx` 通用预计算 + `_suppress_fx`/`_pin_fx` 接线）、`test_pin_suppress.py` | `test_pin_suppress.py` 17/17：三个动词各出各的键、定住不动关键词而压制清关键词+不能行动、0x3A/0x3D 后果按被判定那张卡结算、群体压制逐张、被压制旁观者不进列表、未登记族报错；离线 12 个 `test_*.py` + 4 个模块自检全过。**实机未验证** |
| 10-02 | **§8-8 第四片：回合族（0x14/0x40/0x19）runner + 修 suppressionException 大小写真 bug** | `semantics/triggers.py`（`TURN_HOOK_SPECS`/`MOSQUITO_SKIP`/`has_custom_name1_attr`/`run_turn_family`/`*_turn_effects`；顺序照 BP @13709/@11795/@13142；`_suppression_excepted` 改**大小写不敏感**——表里是枚举写法 `OnStartofTurn`，钩子是 `OnStartOfTurn`，以前 11 张例外牌从来没生效）、`test_turn_family.py` | `test_turn_family.py` 17/17（三趟顺序 / 0x14 无参 / 0x19 即时·1·2 / mosquito 跳过 / meta 如实 / 例外牌放行 / 整段匹配 / 未登记钩子报错）；离线全套（4 自检 + 14 个 `test_*.py`）全过。**实机未验证**；回合族的**消费点**（接进 Sim/rule 的回合结算）尚未接 —— 见 TODO A1 ⑨ |
| 10-02 | **§8-8 第五片：揭示族（0x37 + 自己那条）** | `semantics/triggers.py`（`REVEAL_HOOK_SPECS` + `run_reveal` + `reveal_effects`；顺序照 BP `RevealCard`@9191：自己 `OnCardRevealed()`（未压制才跑）→ 0x37 `OnOtherCardRevealed(cardBeingRevealed)` → `OnEnterPlay(4)` → 0x2B `OnOtherCardEnterPlay(…,4)`）、`policy/boardeval.py`（`EVENT_FX_KINDS` 加 `"reveal"`；`{"reveal":True}` 去 covert 后结算 `event_fx["reveal"]`）、`player/rule.py`（`_REVEAL_HOOKS` + `_reveal_fx` 接进两处 `from_cards`）、`test_reveal.py` | `test_reveal.py` 13/13（四段顺序/形参名/被压制只跳过自己那条/`with_enter_play=False` 半段/去 covert+后果结算/没有后果表时行为与旧版一致）；离线全套（3 自检 + 12 个 `test_*.py`）全过。**实机未验证** |
| 10-02 | **开局闸门三次修正：牌组页 `match_active` 也会为真** | `_nn_scratch/play_guard.py`（`in_match` = `match_active` **且** 旁证之一：`history` 可读 / `turn>0` / 场上有牌；读不到任何旁证才放行；异常一律拦）、`_nn_scratch/test_play_guard.py`（新） | 实机（牌组页，18:30:10）：`ma=True`（残留 kredits={local:0,enemy:1}）、`hist_ok=False`、`snap=[0,0]` ⇒ `im=False`、`gate=(True,False,'')` **放行**；离线 `test_play_guard.py` 11/11（主菜单/牌组页/对局中三条证据/异常保守拦/牌组空拦）。**实机已验证（牌组页放行 + 随后开局成功）** |
| 10-02 | **实机 2 局（`§8-9` 前半：干净跑完）** | 无代码改动；证据 = `rule-live-20261002-183131.jsonl`（85 行，turn 1–13）与 `rule-live-20261002-183810.jsonl`（262 行，turn 1–35）+ `continue_ranked.log` | 两局都**跑完没崩**：进程 20124 存活（窗口随后被用户最小化）、**0 个异常/崩溃转储**（`Saved/Crashes` 最新仍是 17:39 那次）、`continue_ranked` 第二局 `done=True`；动作：局1 `mulligan/play_unit4/attack3/end3`，局2 `play_unit14/attack8/end10/pick2/…`；每步 `t_decide` 均值 1.58→0.86 s（max 3.82），`t_exec` 均值 4.63→2.64 s（max 28.4，那次是 turn 7 的攻击），单回合最坏 32.2 s / 21.3 s（软预算 35 s 内）。**四个标记（+intel/+deckchg/+shuffled/+hooks）与 `death_fx` 这次都没触发**——牌组是「美国卡组 2」，不含对应卡 ⇒ 要换牌组再跑（见 TODO A4/A2） |
| 10-02 | **实机审计：缺口类别** | `_nn_scratch/_audit.py`（只读） | 局2 的 `probe.gaps`：**假缺口**一类 —— `MAGNIFICENT SEVENTH`/`RED DEVILS`/`22nd MARINES` 的原因只是"这张牌没有 打出/部署钩子 覆写"（本来就没打出效果）⇒ 已立案 TODO **A6**；**真缺口**：`USS YORKTOWN`/`175th INFANTRY REGIMENT`/`card_event_echelon` 的 `Unimplemented @…`（缺原生，归 A5/实现补全）、`RALLY` 的"需要目标但旗标说不用指向" |
| 10-02 | **开局流程三态化（对局中 / EndOfMatch 结算页 / 牌组页）** | `_nn_scratch/continue_ranked.py`、`_nn_scratch/open_next.py`（离开循环：有结算页 ⇒ 点"继续"；无结算页但**牌组按钮数=0** ⇒ 过渡态只等；无结算页且**牌组页就绪**才继续。`press_play` 前置条件从"离开结算页"收紧为"**牌组页就绪** ∧ 不在对局"） | 用户 2026-10-02 截图指出：对局刚结束的 1~2 秒 `match_active` 严格来说仍为真、但**不在对局**、也**不在牌组页**（正是 `EndOfMatch` 结算页/过渡）——旧版把"没有 `W_EndOfMatch_C`"当"已离开"，于是既不点继续又去点开始。实机只读复核（18:58:18）：`eom=0`（结算页已过）、`deck_n=29`（牌组页就绪）、`im=False`、`gate=(True,False,'')` ⇒ 与三态模型一致 |
| 10-02 | **修"热浪留到结束回合前最后打出"（临时攻击力计价）** | `policy/boardeval.py`（`U.atk_turn` 字段 + `__slots__`/`copy`；`_apply_eff` 的 `attack_turn` 同时记临时量；`unit_value`/`activity_value`/`_threat` 只在"本回合还能打"时计临时攻）、`player/rule.py`（`_death_fx` meta 加 `entries`/`fx_keys` 供 A2 观测）、`test_same_turn_pricing.py` | 用户 2026-10-02 报：热浪（本回合 +1 攻/-1 行动费）被留到结束回合前最后打出＝白给。根因：`attack_turn` 被当永久身材算价值。断言：还能打 ⇒ +0.85（模拟里 `atk` 真 +1、`atk_turn=1`）；**已打完 ⇒ 0.0000**；被压制 ⇒ 0；换 +3 攻 Δ 更大；离线 4 自检 + 13 个 `test_*.py` 全过。**实机未验证（下一局生效）** |
| 10-02 | **§8-8 收尾：离开前线（0x31）+ 撤退（0x36）** | `semantics/triggers.py`（`MOVE_FRONT_HOOK_SPECS`/`run_move_from_frontline`/`move_from_frontline_effects`、`RETREAT_HOOK_SPECS`/`run_retreat`/`retreat_effects`；顺序照 BP @14281/@16567：未压制才跑自己的 `OnMoveFromFrontline()`；`OnBeforeRetreat(&stopAction)`/`OnOtherCardRetreat(card,&stopAction)` 任一 true ⇒ 中止并记 `meta.stopped_by`）、`policy/boardeval.py`（`EVENT_FX_KINDS` 加 `retreat`/`move_front`；`{"retreat":True}`/`retreat_aoe` 在**离场前**结算 0x36 后果）、`player/rule.py`（`_RETREAT_HOOKS` + `_retreat_fx` 接两处 `from_cards`）、`test_move_front_and_retreat.py` | 14/14：0x31 顺序/排除自己/被压制只跳过自己那条/meta 标 stop 未建模；0x36 顺序（不排除自己）/两条 stopAction 各自的中止语义；撤离前结算后果（+2 指挥点）与"没有后果表则只离场"；离线 4 自检 + 14 个 `test_*.py` 全过。**实机未验证** |
| 10-02 | **§8-8 收口：回合结束的消费点（`sim_turn_end`）** | `semantics/triggers.py`（`run_turn_family(only_card=…)` 支持逐卡预计算）、`policy/boardeval.py`（`EVENT_FX_KINDS` 加 `turn_end`/`turn_start`；新增 `sim_turn_end`：结算 `event_fx["turn_end"]`（我方 0x19）+ `RemoveBuffsEndOfTurn` 清 `atk_turn`，带 `turn_ended` 幂等标记）、`player/rule.py`（`_turn_end_fx` 逐卡预计算 + 两处 `from_cards` 接线；**`_d` 先 `sim_turn_end(after)`** —— 打分口径改成"停手后这一回合真正结束时的盘面"）、`test_turn_end_consumption.py` | 8/8：0x19 后果结算、无表⇒不变、只作用于表里那张、临时攻清零、幂等、换数据答案变、Δ 含回合结束；离线 4 自检 + 15 个 `test_*.py` 全过。**行为改动 + 实机未验证**（下一局覆盖） |
| 10-02 | **§8-9 标记：验证清单 + 失败可见 + 接线测试** | `kards-agent/EVAL-MARKERS-CHECKLIST.md`（新；四个标记各自的代码门槛/需要的卡/读法，卡表从本构建导出逐卡 grep）、`player/rule.py`（`probe.marker_err`：四条标记链不再 `except: pass`；`probe.intel_dbg`）、`test_marker_plumbing.py` | 清单：`+intel`=6 张触发者之一在场 ∧ 打出的牌 `cipher/intel_seen>0`；`+deckchg`=LOVAT SCOUTS/RM ROMA/BETASOM；`+shuffled`=5 张覆写 0x16 且**只有 skipSubAction=true 的洗牌**；`+hooks`=0x2B/0x13/0x33/0x15（代表卡 5th SASEBO SNLF）。接线测试 10/10：四条链返回非空 ⇒ 后缀 + `unit_eff` 合并；返回空 ⇒ 不加后缀；抛异常 ⇒ 记 `marker_err`；两条同时 ⇒ 后缀叠加。**实机证据待下一局** |
| 10-02 | **纯重构：收缴（salvage）规则下沉到 sim/**（§5 步骤 3） | `sim/effects.py::apply_salvage`（从 `boardeval._salvage_one` 原样迁入；顺手把原先直接读评估权重的 `W["hand_cap"]` 改成参数 `hand_cap`，sim 不再 import 权重）、`policy/boardeval.py`（`_salvage_one` 只留旧名转调）、`test_salvage_migration.py` | 7/7：转调路径与直接调用结果一致（1/1、费用 min(cost,3)、关键词/名字/类型/效果保留）、手牌满不创建、认不出模板 ⇒ 1/1 infantry、换数据答案变、`hand_cap` 传参生效；离线 4 自检 + 抽查 6 个 `test_*.py` 全过。**行为不变，实机未验证** |
| 10-02 | **纯重构：伤害/阵亡原语下沉到 sim/**（§5 步骤 3） | `sim/effects.py::deal_damage`（从 `boardeval._dmg_unit` 原样迁入；战斗伤害先扣重甲、`max(dmg,0)`、`dfn<=0` 离场；**死亡链回调改注入** `on_death` —— 0x27/0x18/打捞那段属于 L1 钩子）、`policy/boardeval.py`（`_dmg_unit` 只留旧名转调，注入 `_apply_death`）、`test_damage_migration.py` | 7/7：非战斗伤害不吃重甲、战斗伤害吃重甲、致死离场+回调、负/零伤害不涨血、转调与直调一致、换数据答案变、重甲≥伤害 ⇒ 0 伤；离线 4 自检 + 抽查 8 个 `test_*.py` 全过。**行为不变，实机未验证** |
| 10-02 | **实机只读排查：`+intel` 的两个原生卡点（修掉一个、立案一个）** | `semantics/effectvm.py`（`record_effects`/`enumerate_effects` 新增 `board`/`my_seat` 透传并设到 `CardNatives`）、`player/rule.py`（`_vm_eff`/`_intel_triggers`/`_deck_changed_triggers`/`_deck_shuffled_triggers` 四处调用传 `board=st, my_seat=seat`）、`semantics/triggers.py`（`_run_hook_ex` 加 `st=` 并由 `_Chain.call` 传入）、`_nn_scratch/probe_intel_gate.py`（新，活进程逐卡空跑）、`EVAL-MARKERS-CHECKLIST.md`、`TODO A7` | 只读实测（20:13/20:14）：**修前** `16th TARNOW REGIMENT` 停在 `IsSideActive 需要 BoardState`；**修后** `stopped: None`（链跑通）。`CRUISER SCOUTS` 仍停 `AMatchControllerV2::IsReconnectMatch`（C++ 原生未实现）⇒ 已立案 **TODO A7** —— 这就是 `+intel` 至今不出现的直接原因。离线 4 自检 + 抽查 4 个 `test_*.py` 全过 |

| 10-02 | **`+intel` 真正的空因：三条触发族读错返回键**（`record_effects` 没有 `outcomes`） | `player/rule.py`（`_intel_triggers`/`_deck_changed_triggers`/`_deck_shuffled_triggers` → `enumerate_effects`；`intel_dbg["tried"]` 如实记"跑了但为什么空"）、`test_intel_runner_fix.py`（新）、`test_attack_hooks.py`（测试替身补 `st=`） | 第 5 局（`rule-live-20261002-203429.jsonl`）`intel_dbg` 写"场上没有覆写卡"，但同一批卡的 `find_function(inherited=False)` 都**找得到**（`probe_intel_hook.json`）⇒ 空的是返回值不是钩子；`probe_intel_deep.json` 证明 ubergraph 只在 `IsLocatedOnBoard=false`（卡在 discard）时提前 return。假盘面 + 真 VM 复核（`probe_intel_fixed.json`）：`_intel_triggers → [[19, {"buff": [1,1]}]]`、`intel_dbg={}`。离线：36 个 `test_*.py` 0 失败、4 个模块自检 0 失败、`kardsmem selftest`（launcher）PASS；提交 `6836c6f`/`73a90e3`。**实机复验待下一局同牌组**（该牌组同时有 7th RECON + NAKAJIMA B5N2，打出情报牌即可看到 `+intel`） |

| 10-02 | **`+hooks`/`+shuffled` 的空因：exclude 不可哈希 + BoardState 没透传** | `semantics/triggers.py`（`find_cards` 用 `_ptr_of` 归一 `exclude`；`_run_hook`/`run_enter_play` 透传 `board/my_seat`；自检替身补 `st=`）、`player/rule.py`（`_play_hooks_triggers` 显式传指针 + 异常写 `marker_err["hooks"]` 不再静默）、`test_play_hooks_board.py`（新） | 实机只读探针（假盘面 + 真 VM）：`run_play_hooks` → SASEBO `OnCounterMeasureTriggered` → `{"buff":[1,1]}`、`_play_hooks_triggers` → `[[15,{"buff":[1,1]}]]`（`probe_hooks_deep2.json`）；同一进程里 `+shuffled` → `[[16,{"damage":5}]]`（`probe_markers_vm.json`）。**修前两族恒空**（`TypeError: unhashable type: 'Card'` + `IsSideActive 需要 BoardState`，都被 except 吞掉）。离线：37 个 `test_*.py` 0 失败、4 模块自检 0 失败、`kardsmem selftest` PASS；提交 `146d4b5`。**实机复验仍待下一局**（SASEBO / SABAE 已在当前牌组，出现对应局面即可） |

| 10-02 | **同一族收尾：autoplay/抽到-打出也补 BoardState** | `player/rule.py`（`_deck_state` 里两处 `record_effects` 传 `board/my_seat`） | 全仓 `record_effects` 调用点都透传 BoardState（`IsSideActive` 不再当场停住）；37 个 `test_*.py` 0 失败 + 4 模块自检 0 失败；提交 `7c24d6a` |

| 10-02 | **四个标记的构建级体检：钩子全在，缺的是实例** | 新增只读探针 `_nn_scratch/probe_deckchg_cards.py`、`probe_cardfunction_deck.py`、`probe_deckchg_cdo.py`、`probe_lovat_ubergraph.py`、`probe_hooks_deep*.py`、`probe_markers_vm.py`；`CLAUDE.md` 弯路 #44 | 静态卡表（2019 张）逐类查钩子：`+intel` 6/6、`+shuffled` 5/5、`+deckchg` 3/3 都有覆写与字节码（`probe_deckchg_cards.json`，例：`card_unit_lovat_scouts` 的 `OnAfterDeckChanged`=0x15c57930600）。同批实测：`+hooks → [[15,{"buff":[1,1]}]]`、`+shuffled → [[16,{"damage":5}]]`、`+intel → [[19,{"buff":[1,1]}]]`。撞到的假缺口：用 CDO 当 self 跑 LOVAT 报"函数 GetDeckByside 不存在"，实为 **CDO 的 `cardFunction=0`**，真实实例的 `cardFunction` 类上有该蓝图函数（`0x15c2758a500`，has_bytecode=true）⇒ 记进 `CLAUDE.md` 弯路 #44。**剩余：实机（需要用户牌组里出现对应卡）** |

| 10-02 | **§8 完成度审计（第 1–8 项逐条复核）** | 无代码改动（核对实现 / 测试 / 回执） | ① 指挥点夹取：boardeval 自检 4/4 · ② 虚函数默认值 + `EnumCompare*`：`test_virtual_defaults.py`、`test_card_targets_natives.py` · ③ 未消费键：`test_effect_keys_consumed.py` 32/32 · ④ 同回合计价：`test_same_turn_pricing.py` 7/7 · ⑤ 静态卡提供者：`test_static_provider.py` 10/10 · ⑥ 断言落盘：`test_hook_chain_assertions.py` 10/10 · ⑦ 摧毁链去重：同前（`to_fx` 默认不输出摧毁桶） · ⑧ 补接触发：0x32/0x2A/0x3A·0x3D/0x14·0x40·0x19/0x37/0x31/0x36 + `sim_turn_end`，均有回执行。以上在当前工作区全部可查，且 37 个 `test_*.py` + 4 个模块自检 0 失败。**唯一未闭合 = 第 9 项实机四标记**（代码门槛已清，见本表上方两行） |

| 10-02 | **A7 半落地：`IsReconnectMatch` 有护栏实现 + 修 APPROX 接线 + 补纯文本原语** | `kardsmem/cardnatives.py`（护栏实现 + `self.APPROX`）、`kardsmem/kismetlib.py`（`Conv_*Text`/`Format`/日志三兄弟 no-op）、`test_reconnect_match.py`（新）、5 个只读探针 | ① 调用点（BP `CreateAction_AddSubAction`）`JumpIfNot` 只看第一个出参、b/c 只在重连分支被读；② 实测正常局 `mulliganReplacementReceived=true`、`MulliganData` 非零 ⇒ 判据只用 `reconnectInSameTurn`/`reconnectLoading`，亮/读不到就抛缺口；③ 真 bug：`CardNatives` 没有 `APPROX` 属性 ⇒ `CanBeTargetted`/`CanEndTurn` 的近似标记**从来没生效**，已挂上；④ `STRETCH THE LINE` 现在整条跑通（`eff={spawn:1, intel_seen:1}`，此前停在 IsReconnectMatch），`CRUISER SCOUTS` 停在下一个缺口 `GetGameInstanceSubsystem`（登记 A7b）。离线：38 个 `test_*.py` 0 失败、5 个模块自检 0 失败、`kardsmem selftest` PASS；提交 `25b26c6` |

| 10-02 | **A7b 落地：`GetGameInstanceSubsystem` 走启动时索引（查表 O(1)）** | `kardsmem/subsystems.py`（新）、`kardsmem/cardnatives.py`（注册原语 + `_NO_RECEIVER`）、`_nn_scratch/live_session.py`（启动预热）、`test_subsystem_index.py`（新） | 实测排除两条便宜路（只读 `class_of` 全扫 5.28 s / 反射属性里没有 `SubsystemCollection`）；索引路线实机验证：建表 5.0–5.4 s、75 项、父类链登记正确、`find` 命中（`probe_subsystems_live.json`）。离线 39 个 `test_*.py` 0 失败（新测试 9 断言）、5 个模块自检 0 失败、`kardsmem selftest` PASS；提交 `99744bf`。**剩：等有对局内存时把 `CRUISER SCOUTS` 链跑到底** |

| 10-02 | **实机第 2 局：`+intel` 再次出现（改动后两局均无异常）** | `kardsmem/cardnatives.py`（`GetAllActorsOfClass`、actor 表共享快照缓存、类级命中 30 s）、`kardsmem/kismetlib.py`（`IsSimulatingInEditor` 恒 False）、`_nn_scratch/marker_report.md` | 第 2 局（`rule-live-20261002-224756.jsonl`，118 行，turn 30）：`+intel` 出现（LONG RANGE RECON `gap+intel`/`vm+intel`、ORP GENERAL HALLER、REDEPLOYMENT），`marker_err` 空；`+hooks`/`+shuffled`/`+deckchg` 仍未出现（该局 SASEBO 一直没上场、无 skipSubcase 洗牌）。新挖出并修掉：`SOUL OF OLD JAPAN` 的两级卡点（`GetAllActorsOfClass` → `IsSimulatingInEditor`）；**遗留**：CRUISER SCOUTS 链冷跑 6.8 s（超单卡预算 ⇒ 实机必超时，已记 TODO A9）。41 个 `test_*.py` + 5 模块自检 + `kardsmem selftest` 全过 |

| 10-02 | **实机第 3 局：`+hooks` 首次拿到实机证据（`+intel` 三局全中）** | `_nn_scratch/marker_report.md`（工具产出）；无代码改动 | `rule-live-20261002-231233.jsonl`（60 行，turn 16，0 崩溃）：**`+hooks` 出现**（LONG RANGE RECON `vm+intel+hooks`）；`+intel` 出现（KYOTO REGIMENT `vm+intel`、7th RECON `none+intel`、UNDERGROUND STATE `gap+intel`）；`marker_err` 空；latch 正常恢复。⇒ 四个标记里 **`+intel`/`+hooks` 已实机验证**；`+shuffled`（需 skipSubcase 洗牌 + 场上洗切卡）、`+deckchg`（需 LOVAT/ROMA/BETASOM）仍未出现。实机累计 3 局无崩溃、无 marker_err |

| 10-02 | **A9 收口：CRUISER SCOUTS 慢的根因 = 子系统索引按名字筛（`BP_OnlineMatch_C` 永远未命中 ⇒ 全量重建）** | `kardsmem/subsystems.py`（判据改"继承链含 GameInstanceSubsystem"、重建间隔 60→300 s）、`test_subsystem_index.py`（+2 断言）、`probe_cruiser_profile.py`/`probe_gi_asked.py` | profile：整链 3.28 s，其中 `GetGameInstanceSubsystem` **3.25 s**（99%），Forecast/pin/intel 分支 <0.03 s ⇒ **不是卡复杂**。包装探针查明它要 `BP_OnlineMatch_C_2147298428`（名字不含 "subsystem"）。修后同链 **0.04 s**、该原语 **0.0 s**，eff/complete 不变；41 个 `test_*.py` + 5 模块自检 + `kardsmem selftest` 全过；提交 `8138e28` |

| 10-02 | **在局全牌扫描（只读，用用户对局真实盘面）+ A9 实机复核** | `_nn_scratch/probe_live_sweep.py`、`probe_live_check2.py`（新） | ① **A9 实机复核通过**：对局内 CRUISER SCOUTS dry-run **0.031 s / complete=True / eff={pin, intel_seen:3}**（修前 3.28 s 且必超时）；② 全牌扫描：我方 31 张里 **28 张正常**（含 CRUISER SCOUTS、ORP GENERAL HALLER、PLAN WEST、STRETCH THE LINE、DUG IN、BOMBING RAID、UNDERGROUND STATE、SCOUTING PARTY、COMMAND FAILURE、SCORCHING SUN 等都有 eff），单位卡报"没有 OnPlayedFromHand 覆写"属正常；③ 剩 3 个真缺口登记 TODO **A11**（`IJN AKAGI` TypeError / `SOUL OF OLD JAPAN` 空读 / `BLUE SKY` 未知名原语）。提交 `b269279` 等 |

| 10-03 | **离线收尾三项：`Get*OfClass` 兜底 / 打出族位置过滤 / 塞牌并洗记 skip**（用户指令：离线修全部、不实机、跑通关机） | `kardsmem/cardnatives.py`（`_gobjects_of_class` 兜底，缓存 5 min）、`semantics/triggers.py`（`run_play_hooks` → `on_board_only=False`）、`semantics/effectvm.py`（`SpawnCardInDeckBySide(shuffle=true)` 同时记 `deck_shuffle`+`deck_shuffle_skip`）、`player/rule.py`（autoplay 分支 `enumerate_effects(cap=9)` + `outcomes_mode="max"`）、`test_offline_fixes_1003.py` | ① SOUL OF OLD JAPAN 要的 `BP_Logic_C` 不在 `Locator.actors()` 的 233 个里 ⇒ 空表 ⇒ 读空对象字段断链；现在按类名全对象扫兜底（找不到仍返回 0）。② `THE POMPADOURS` 一类"在手牌中生效"的牌以前被 `on_board_only=True` 整类漏掉；现由钩子自身判据决定。③ `+shuffled` 闸门看 `deck_shuffle[_skip]`，而"塞牌并洗"只记 `deck_add_shuffle` ⇒ 自动化里永不触发；已按游戏实现补记。**预报口径（用户 2026-10-03 两次更正）**：两层三选一（每层 3 项）⇒ 3×3=9 联合评估取最好；autoplay 三选一只存在于"预报第一段选中的那张牌"上。离线：44 个 `test_*.py` 0 失败、5 模块自检 0 失败、`kardsmem selftest` PASS；提交 `cd4225b`/`a9d4ecc`；**实机复验按用户指令不做** |
| 10-03 | **评估对齐 BP（5 个蓝图函数）** | `policy/boardeval.py`（偷牌当回合可动/落点、治疗门槛、老兵按 `_vet` 静态卡绝对值、转化按目标出厂数值）、`semantics/effectvm.py`（`_veteran_payload`/`_convert_payload`、`GetAllCardsOnBoard` 钩子、`selectTargetFromHand`）、`sim/effects.py`（战斗 Sequence+免疫+`fight_dmg`）、`tools/canplay.py`（`TSet<int32>` 字段读法） | `test_all_cards_hook.py`、`test_veteran_heal_steal.py` + boardeval/effectvm 自检；全套离线 0 失败；**实机待验** |
| 10-03 | **钩子级后续：老兵(0x20)/治疗(0xC·0x2C)/偷牌(0x2E·0x8·入场)/战斗改伤** | `semantics/triggers.py`（`run_veteran/run_heal/run_steal/fight_damage`）、`player/rule.py`（`_veteran_fx/_heal_fx/_steal_fx/_attach_fight_dmg`）、`Sim.event_fx` 新种类 | `test_veteran_heal_steal.py`（假 VM 下顺序/形参/否决/压制语义）；**真 VM/实机待验** |
| 10-03 | **控制面板 `gui/`** | `gui/{control,autoplay,history,watcher,app}.py`、`run_gui.bat`、`player.play.play(should_stop=…)`、`test_gui.py` | 离线编排 18+ 项、窗口无头冒烟；**未实机**（首次先 1 局、关自动下一局） |
| 10-03 | **版本识别（读进程内存里的 ProjectVersion）+ 静态读 pak** | `kardsmem/version.py`、`tools/pakread.py`（UE5 pak v11 只读 + AES；key 硬编码默认）、`kardsmem/{build,proc,cli}.py`/`kardsmem/board.py`/`agent/precheck.py` 按版本选表 | 实机（launcher 1.60）3 s 读出 `1.60.27292.launcher`；四棵安装树静态读 pak 的 ProjectVersion == 目录名；`kardsmem selftest` 两构建 PASS |
| 10-03 | **架构：运行时扫描 RVA（方案 + S1）** | `ARCH-RELEASE-VCS.md`、`kardsmem/rvascan.py`（判据对齐 Dumper-7 的 GObjects）、`test_rvascan.py` | 合成内存离线 13/13；真游戏验证（S2）待游戏 || 10-03 | **架构 M0/M1：物理拆分 + Prompt 模型（可挂起 run）** | `sim/{state,engine,adapter,prompt}.py`、`evaluation/value.py`、`policy/search.py`（≤24 叶+剪枝）、`policy/boardeval.py` 退为门面、`player/rule.py`（`_fc_table`/`_plan` 照路径点/`_rng_seed` 假种子）、`ARCHITECTURE.md`（总纲）+ EVAL/OPS 文档修订 | `test_arch_rules.py` 桥清空；`test_action_paths.py`（抉择/预报两层/24 枝/照路径点/放回类手牌目标）；全套离线 `test_*.py` + 4 个模块自检 0 失败；**实机待验** |
## 阶段 0 — 收尾攻击修复（先做，约 1 局）
**目标**：把"已验证成立"的结论落地干净，并核对尚未验证的伴随改动。

任务
1. 清理无因果写入：`drag_release` 里的 `mouseOverActor` 重写（`pc_hover_rewritten`）、`BoardCardUnderTargetArrow` 写入（`arrow_under_written`）、`targetArrowFinalLength=4000`（冗余：箭头销毁时被覆盖，真正有效的是头部平面搬远）。**逐项删、逐项回归**，一次一个变量。
2. 删除拐棍：`nn._park_cursor_outside`、`_cursor_to_target`、`ops.inject.screen_of_card`（若无它用）与 `KARDS_PARK_CURSOR` / `KARDS_ATTACK_CURSOR_ON_TARGET`。保留 `cursor_in_window` 的日志字段（用于识别用户介入）。
3. 同类手势排查（TODO 3b⁷④）：带目标出牌、选目标、选手牌目标是否也依赖 `LocationUnderCursor/RowUnderCursor`；读 `BP_HandCard` 对应分支，列清单，**不要盲写**。
4. 验证尚未验证的旧改动：`move_up` 清箭头目标（`cursor_state.cardUnderCursor` 应为 0）；`obj_alive` 偏移（对普通对象/已回收对象抽样）；`pick_candidates` 关卡 actor 快速路径（实测耗时 + 刚生成的候选是否可见；用户质疑过"236 个 actor 0.00 s"）；回合开始 2 s 等待与"友方回合"横幅重试。

测试
- 一局，≥12 次攻击，后台 + 窗外：成功率 ≥ 95%（目标 100%）；每删一项写入后各跑一组。
- 失败时读 `x_pre_commit`、`PC+0x828`、攻击牌 `+0x668/0x669`、是否残留第二支箭头（`OnActorMouseDown` 不清旧箭头）。

验收：攻击 ≥ 24 次累计零因果性失败；代码里不再有拐棍；TODO §3b⁷ 改"已完成"。

## 阶段 1 — 随机流实战校验（TODO §2）
**目标**：拿到活种子，验证 LCG 推进与 `+0x7A8` 条件；为阶段 3 铺路。

任务
1. JS 侧采样（**不用 Python 线程**，避免抢 frida RPC）：在监听器的脚本里加环形缓冲，按游戏 tick（或固定间隔）读 `cardsRandomStream@+0x2B8`（Seed@+0x2BC），带 `gtTicks`；赛毕一次读回。指针路径 `Logic → spawnCardFunctions → ABP_CardFunctions_C`，不扫实例；读一次约 5.7 µs。注意局外该对象不存在（instances=0、seed=0）。
2. 收尾脚本 `rng_test_finish.py` 先删旧标志文件再等待比赛结束。
3. 分析：相邻 seed 的步数 = `kardsmem.rng.steps_between`；与已知效果（抽牌、洗牌 N 次抽取、OVERCAST 的 3·(d+2)）对照；检查 `MatchController+0x7A8` 非零时 `SetRandomStreamWithActionID` 是否重置种子。
4. 验证 `RandomIntFromRangeWithStream` 在 1.60 的包装 = d=Rand(1,10) → d×Rand(0,1) 丢弃 → Rand(min,max)（d+2 次抽取）。

测试：一局，采样全程；报告 `rng_test_report.md`：采到的 seed 序列、步数对账表、异常点。
验收：≥ 20 个已知效果事件的步数与预测完全吻合；对不上的逐条解释。TODO §2 勾掉 (a)，(b) 的原生函数接入见阶段 4。

## 阶段 2 — 延迟压到 ≤35 s/回合（TODO §3b、3b'、3f）
**目标**：连续 3 局，≥90% 的回合 ≤35 s；部署 ≤3 s。

任务（按收益排序）
1. **预报空轮询**（最大项，≈50 s）：候选 actor 异步加载期间约 40 次 1.3 s "没有可执行的动作"。短期：用事件/帧数轮询代替固定 1.3 s；长期由阶段 4 的种子预测消除。
2. **5th RANGERS 部署回执 5.4 s**：确认这段时间 `pick_pending` / `card_being_played_from_hand` 是否已就绪，能提前返回就提前（`awaiting_choice` 早返回已做，核对覆盖面）。
3. **预检合并/缓存**：`precheck_play` + `game_can_play_from_hand` 合并，省 ≈0.5–1 s。
4. **选牌 3.0 s**：前后两次候选扫描合并（扫 0.7 + 点 1.7 + 事后扫 0.6）。
5. **换牌 9–12 s**、**第 2 回合结束回合 4 s**、**首回合部署 `drag_success=0`**：先加分段计时定位，再改。
6. **评估其它 Agent 的 `_settle()`**（`nn.py` 转发 `precheck.call_write("settle", s, frames)`）：对比改前每回合耗时；确认 `precheck.call_write` 确有 `settle`；过慢就把无效的墙钟下限收紧（帧数兜底不动）。
7. **开局物理鼠标流程**（`startmatch.py`、`do_mulligan.py`、`mull_auto.py`、`hand_calib.py`、`vendor/actions.py`，见 `frame_waits_report.md`）：改走注入的 widget 点击；或点击前窗口置前 + 轮询状态代替固定 sleep。拐棍已删后，物理点击无光标冲突。
8. **`nextmatch.py` 一闪不进页面**（TODO §3c）：读 `end_of_match_continue` 的步骤序列（step 0→1→7）与结算页落点，查是否触发自动再来一局；先复现再修。

测试：每项改动后一局（后台 + 窗外），监视每回合 `t_snap/t_decide/t_exec` 与 `x_timing`；用脚本统计回合耗时分布（P50/P90）、被拒原因计数。
验收：3 局连续达标；`layer_gone` / `no_live_candidates` 不再造成停机。

## 阶段 3 — 稳定性与崩溃（TODO §3b''、§4）
任务
1. 验证 `obj_alive` 在 `click_actor` 入口与 `pick_choice` 候选过滤中的效果（预报第二层点击曾因陈旧指针在 `ProcessEvent+0x2DE` AV 并卡死）。
2. ProcDump 收紧：不抓 frida 自己处理的第一次机会 AV；清理旧转储（本次 DS 已删约 12.7 GB 的 `-t` 转储与 44 份 UEMinidump，D 盘剩余需复查）。
3. DeepSeek CLI `watch` 默认含 0x4000 与 termination（我们的 API 脚本已如此，另一个工具由 DeepSeek 维护，只提请求，不改）。
4. NZANS 旧 AV：只做调查记录（疑与非游戏线程调用有关，游戏线程调度后是否复现）。
5. 热重载与监听器用法写进记忆；`CLAUDE.md` 增弯路 #32：**"光标在窗外时 `Location/Row` 是旧值"才是攻击吞掉的因果，`mouseOverActor` 只是相关**（把相关当因果的第三次）。
6. **跨局缓存（座位/Logic）**：`cards._my_seat` 的座位缓存必须跟随 MatchLog（不能钉在 session 上）；`MatchLog.forget()/my_side()/_logic()/locate()` 跨局失效；`AgentSession.legality()` 座位变了重建。验证：连打两局，**第二局 0 个 `card X 不在我方手牌`**、换牌正常（回执登记到《回执台账》）。

验收：连续 3 局无崩溃、无卡死；崩溃监视无新转储。

## 阶段 4 — 预报评估与种子预测（TODO §3d、§3g）
前置：阶段 1 完成（有活种子与验证过的 LCG）。
任务
1. **预测第二层**：Python 复刻（比"VM 跑 `GetChooseSpawnCards`"稳）：池 = `GetAllStaticCardsSortedByName`（按名字排序）经过滤、带 `subtype.sunny/rain/storm` 与轻/中/重标签；对每个第一层选择，从同一活种子起算，依次 `wrapper_int(0, len-1)` 三次。`semantics/forecast.py` 现为 VM 版（未实机验证），改写或补一个 Python 池版。
2. **实机对照**：打出 OVERCAST 之前读 seed → 预测 9 个结果 → 与弹出的候选逐项对比（实测步数 16/18/19 吻合 3·(d+2)）。
3. **9 选 1 评估**：
   - 天气牌在**下回合开始**才生效（`OnStartOfTurn`，弯路 #26）；评估看开局效果与"本回合贴膜"。
   - 贴膜部分作用于敌我：打单位交换可能亏，打总部有利 ⇒ 选项价值随"本回合打算怎么打"变化；`boardeval` 模拟攻击时算入。
   - 花费用 `GetStaticKredits(FName, int*)`；候选名转 `FName`。
   - **必须按当前场面评估**，不再是"第一个"或"花费最低"。
4. 接入 `rule.choose_pick`，在点第一层之前算好整条路径（两层最多 9 条），并去掉候选加载空轮询。
5. 玩法层三处亏（DS 报告，§3g）：
   - 同回合重复牌（PARACHUTE ASSAULT 第二张被估 +2.99 实际 0；OVERCAST/HEATWAVE 同型）：应用前后 `boardeval` delta ≈ 0 ⇒ 记 0 分；
   - 本回合 buff 贴完没有后续攻击仍计分：改为计"同回合后续攻击的 delta"；
   - 召唤类按 `yield = min(N, 空位)` 与顺序估价（先上线能多拉一张）。
   整回合联合搜索只留档不做（RPC 成本）。

测试
- 离线：合成场面 + 假种子，断言 9 条路径的预测与手算一致；两组不同种子结果必须不同。
- 实机：≥ 3 次预报，预测与弹出 100% 吻合；选择与评估结论一致；对照被评估为"亏"的选项是否确实亏。
验收：预测命中率 100%（≥3 次）；预报相关回合耗时 ≤ 35 s。

## 阶段 5 — VM 触发钩子（TODO §3e）
任务
1. 新 `semantics/triggers.py`：对每个动作，枚举场上定义了对应 `OnOther…` 覆写的牌（`kismet.find_function(uc, name, inherited=False)`，按类缓存），用被动作牌作参数跑它们的钩子。
2. 第一版只做 `OnOtherCardEnterPlay`（触发号 `0x2B`，对每个**非入场牌自己**调用，`EOnEnterPlayMethod`：1 OnPlayedFromHand / 2 OnAddedFromHand / 3 OnSpawn / 4 OnReveal / 5 OnConverted）。**指令类（本回合触发）做；反制（敌方回合触发）不做**。
3. **同一 Action 共用一个 `rng.Stream`**：先自己的钩子，再按触发顺序跑别的牌的钩子，抽取数累加。`record_effects` 增 `rng_stream=` 参数（现在每次新建 `Stream(seed)`）。顺序 = `FetchAllCardsWithEventTrigger` 的顺序（CARD-PLAY-HOOKS 报告：按卡牌创建顺序；钩子只在顶层动作内触发）；先按卡牌 id 升序并标注"近似"，再用报告核对。
4. 效果表达：新增 `side_hits: [{target_id, damage}]`，`boardeval._apply_eff` 认它；随机目标走 `GetRandomCard`（有种子则确定）。
5. 其后：`OnAfterAttack` 等钩子按需扩展。

测试
- 离线：合成场面 + 假卡，验证共享流顺序（交换触发顺序结果不同）。
- 实机：热浪类天气牌 + 单位部署，对照游戏结算的受击目标；记录抽取数与种子变化。
验收：≥3 个场景受击目标与游戏一致；差异逐条解释。

## 阶段 6 — 原生函数补全（TODO §1 + §3j，可并行，纯离线）
> 2026-10-02：全量普查与规格已出（`NATIVE-COVERAGE-1.60.md`、`BOARD-QUERY-NATIVES-1.60.md`，TODO §3j）。**先做 §3j 的 1–10 项**（场上卡枚举/顺序、GetPlayFromHandDamage 覆写优先、EnumCompare、费用下限、关键词 OR 等；kismetlib 小写名约 100 处），下面是旧清单留档。

`kardsmem/cardnatives.py` / `cards.py` / `kismetlib.py` / `agent/effectvm.make_read_hooks`；构建 1.60.27292.launcher（SizeOfImage 0x9CC4000）。
- 关键词 = 旗标位 **OR** `receivedAbilitiesFromCards`（`hasShock@0x1E9` 对被赋予冲击恒为 0；bot 侧已走游戏 getter，VM 侧补）。
- `IsPinned` 读 `pinnedTurns@0x27C`（>0）；`getTotalOperationCost` = 行动费 + `operationCostBuff@0xB4`，下限 0。
- `getMaxPossibleKredits` 现错返槽数，真值全局上限在 GameState+0x338（先读数值）；`getKreditSlotBySide` 当前槽数（存储混淆：`((enc^key@0x334)-add)/mul`，+0x364 左 / +0x378 右）；BP `ChangeKreditSlotsBySide` 夹到 [0, 上限]。
- `HasCantAttack` 系列靠 `customName1`；`getHasGameplayTag` 含父标签与被赋予能力；`IsGotcha` 只认 type 11。
- 缺的原语：IsVeteran、HasBond、HasCustomAbilityFromCard、IsDamaged、IsExile、HasMovementLeft、IsWeatherCard、各 buff getter、`ShouldGotchaTrigger` 算法。
- `Round` 用 half-up；`IsLocatedInDeck` 取 {1,2}。
- VM 缺口：SEABORNE INVASION"生成两张步兵"未记录；TASK FORCE 44 / A FEW GOOD MEN / WAR BONDS / HEATWAVE / OVERCAST / COUNTER STRIKE 读不全或超时（`warm()` 6 s 上限是否够需验证）。反制建模暂缓，保持"已激活不重复列出"。
- 收尾：每个新原语加"换一张不同数据的卡、答案要跟着变"的断言；`python -m kardsmem selftest` 两个构建都过。
测试：离线断言 + 对照实机读到的卡面（`card_totals` 为权威）；实机抽样 ≥ 10 张卡的 getter 值与屏幕一致。

## 阶段 7 — 收尾与文档
- `TODO.md` 全部条目更新状态；`CLAUDE.md` 状态段与弯路 #32；记忆目录同步（热重载、拐棍退役、Location/Row 因果、随机流采样方法、ProcDump 顺序）。
- 提交（中文 + Co-Authored-By）；如需公开只走 `subtree split`。
- 总体验收：达到 /goal —— 至少 2 局完整对局、攻击 100% 成功、回合 ≤35 s、无新问题；否则继续多打几局，直到没有新问题且旧问题修好。

## 依赖与排序
```
阶段0 ──► 阶段2（延迟，依赖攻击稳定）
阶段1 ──► 阶段4 ──► 阶段5
阶段3 贯穿；阶段6 纯离线可随时并行
```
推荐顺序：0 → 1（同一局先后做，或 0 的一局里同时开 JS 采样）→ 2 → 3 → 4 → 5；6 在等游戏/打局的间隙做。

## 风险与注意
- 每次改时序，必须在后台条件下回归；同一时间只允许一路输入在动（别在 `player.play.play` 期间手动发动作）。
- 不在 frida 线程直接 `pe(...)`；新增注入动作一律走游戏线程调度。
- ProcDump 大转储占盘（3.3 GB/份）；开局前查剩余空间。
- ~~用户动鼠标会让 `cursor_in_window=true`，那段攻击失败不算缺陷~~：**作废**（用户 2026-10-02 观察：补写 `Location/Row` 之后鼠标在不在窗口内都不影响脚本操作）；`cursor_in_window` 仍保留为日志字段，出现窗内失败时再单独分析。
- 投降/重开由用户决定；开对局前先确认。

