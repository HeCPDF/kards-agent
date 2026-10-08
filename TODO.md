# TODO

> **只放没做完的事。** 每条一个事项、一个状态、一个指针；细节写进指针指向的文档，不在这里写日志。
> 状态：待做 / 进行中 / 待验证（代码已落地，缺实机）/ 阻塞。
> 总目标：攻击在游戏后台稳定成功（已达成）；单回合 ≤35 s（软验收）；发给服务端的信息流与真人等价（红线）。
> 2026-10-09 起：需要“制造特定局面”的实机验证不再靠开局碰运气，等本地私服开发工具可用后再做（见 S 组）。
> 旧版看板（含各条的详细经过）已归档到伞目录 `docs-local/history/TODO-board-until-20261009.md`。

## S. 本地私服开发工具（用户 2026-10-09 定的方向）
| # | 事项 | 状态 | 指针 |
|---|---|---|---|
| S1 | **指定服务器启动 = 改 live server 地址**。结论与证据：`reverse-data/reports/report/LOGIN-AND-SERVER-URL-1.60.md` §1。要点：`FetchConfig` 每次启动现读 `<安装根>\localConfig.json`（推断：与 `kards\` 同级，待 Procmon 核对），键 `server_config_file`（http URL 即直接用）/ `tier`；蓝图层另读 `kardsroot` / `username` / `provider`；`/config` 返回的 `ServerUrl` 决定之后所有请求。旧结论“localConfig 无效”与用户 2026-10-09 反馈冲突，多半是放错目录或缺 `server_config_file`。exe 补丁（`server/tools/patch_exe.py`）仍是已验证的备选。**待办**：Procmon 确认路径；写 `server_config_file` 实测一次；在不改游戏文件的前提下把“写 localConfig.json”做成启动器步骤（`tools/` 里一个函数，启动前写、退出后还原）；对 1.58/1.60 各验一遍 | 待做（IDA 已查，待实机核对）| `server/LOCAL_SERVER_PLAN.md` |
| S2 | **指定凭据启动**。结论与证据：同上报告 §2。要点：`device_id`/`device_password` 是**本机随机生成的设备令牌**（`device:Windows-<GUID>` 与另一个 GUID，读不到就生成并写回 `HKCU\Software\Kards`），**与邮箱无关**；邮箱账号是 linker account，靠 `link_two_accounts` 与设备账号关联（推断），游戏不会把邮箱密码换成这两个值。读写入口是蓝图可调的静态函数 `AKardsSession::GetValue`(`0x144afa0d0`)/`SaveValue`(`0x144b0b210`)。**两条路**：① 启动前写一对已关联的设备凭据进注册表；② 进程内钩 `GetValue` 按实例返回（多实例用，不碰注册表，更优雅）。红线：值不进日志/仓库；只用用户本人账号。**待办**：确认 `qword_1493AEB58`（子键后缀）含义；决定用 ① 还是 ②；写入前备份原值 | 待做（IDA 已查）| `client/README.md` |
| S3 | **多游戏实例**。**线索（代码里现成的钩子）**：① `base/paths.py` 的 `LIVE_DIR`（命令/日志通道）、`GUI_DIR`、`DATA` 都已能用环境变量 `KARDS_LIVE_DIR` / `KARDS_GUI_DIR` / `KARDS_DATA_DIR` 覆盖——**按实例各给一个目录**就能先把通道与日志分开，不用改代码；② `kardsmem/proc.py::attach(pid=…)` 已支持指定 pid，但 `_default` 是**进程级单例**（`pid is None` 时复用）、`reuse` 默认真；③ `gui/watcher.py::pids()` 按 `--listener` 找监听器进程，没有“监听器 ↔ 游戏 pid”的映射；④ `ops/support.py` 的 `ops_inject_cache.json`、`kardsmem/locres.py::_CACHE`、`rendered.py` 的名字缓存、RVA 缓存都是全局的；⑤ frida 临时目录与 `play_guard` 闸门按进程。**待办**：监听器启动参数 `--pid`；`_default` 改成 `{pid: Session}`；缓存键加 pid；面板里实例选择；多窗口截图按 hwnd。先做“环境变量分目录 + 每个监听器各绑一个 pid”的最小版 | 待做（线索已齐，未设计）。注册表是进程外共享的：多实例若靠写注册表指定凭据会互相覆盖，故倾向 S2 的钩 `GetValue` 方案 | `base/paths.py`、`kardsmem/proc.py`、`gui/watcher.py` |
| S4 | **用私服造局面做验证**：S1 之后，下面 V 组里“需要特定牌/特定局面”的条目改用造出来的局面验证 | 待做 | |

## V. 只差实机的验证（代码已落地）
| # | 事项 | 状态 | 指针 |
|---|---|---|---|
| V1 | 冻结监听器：`kards-agent.exe` 面板“启动监听器”→ `--listener` attach 游戏，`live_log.txt` 出现“命令通道就绪”，`watcher.pids()` 找得到，优雅 `quit`（只在训练/AI 局，别强杀） | 待验证 | `tools/gui_main.py`、`tools/build_exe.py`、`tests/test_frozen_mode.py` |
| V2 | frida 临时目录泄漏修复（`14bdf77`）：重启监听器后 `_frida_tmp` 无新 `frida-*`、用户 Temp 不再增长 | 待验证 | 事故报告：伞目录 `reverse-data/reports/report/INCIDENT-2026-10-02-frida-temp-leak.md` |
| V3 | 热重载 `player.rule` 后接着开局（`_rebind_class_cell`） | 待验证 | `tests/test_hotreload_super.py` |
| V4 | `resolve_target` 数字规格直返（`75b3b19`，纯延迟优化） | 待验证 | `tests/test_resolve_target_fastpath.py` |
| V5 | 幽灵提示：`pending_gate` 剔除“仅手牌目标旗标 + 已判 ghost”；未证实游戏本体在幽灵状态下是否接受攻击/结束回合 | 待验证 | `ops/choices.py`、`tests/test_pending_gate_ghost.py` |
| V6 | 换牌确认后的界面收尾（计时器 + `RemoveFromParent`）：按钮和提示是否消失、不再弹“Mulligan timed out” | 待验证 | `ops/inject.py::_mulligan_ui_tail` |
| V7 | `death_fx` 在含 `OnDestroyed` 覆写的真实阵亡局面非空（数据通路已验） | 待验证 | `player/rule.py::_death_fx` |
| V8 | 四个评估标记 `+deckchg`（缺卡未验）、`+shuffled`（缺自动化日志）的实机组合 | 待验证 | `docs/EVAL-MARKERS-CHECKLIST.md` |
| V9 | 评估行为变更的复核：P3 第五、十刀（`cur_stats` 激活、减防会真死）、A9 排容量、A11 预报选择随盘面变化、A12 BOMBING RAID 打中间应高于打最左、F1 打总部对账（`probe.hq_check`） | 待验证 | `docs/P3-NATIVE-PORT.md`、`docs/SIM-FIDELITY.md` |
| V10 | 延迟效果：ECHELON 未在实机见过；回合钩子 BLITZKRIEG / DIVE BOMBING / PATTON 的 VM 报 `TypeError`；`TACTICAL WITHDRAWAL` 无效果摘要；`CRUISER SCOUTS` 链越过 `GetGameInstanceSubsystem` 后的下一个卡点 | 待验证 / 待做 | `docs/DEFERRED-EFFECTS-CENSUS.md` |
| V11 | 控制面板（G1–G3）：首次实机用一次（先 1 局、`auto_next` 关着）；面板还没显示当前账号名 | 待验证 | `gui/` |

## B. 对局体验与延迟
| # | 事项 | 状态 | 要点 |
|---|---|---|---|
| B1 | **回合 ≤35 s**（软验收；**不许降束宽**，需用户拍板才能动搜索参数） | 待做 | `t_exec` 中位：`move_up` 2.8 s、`play_event_target` 2.6 s、`attack` 2.3 s、`play_unit` 1.9 s、换牌 5.9 s；目标每步 ≈1 s（悬停下限 0.3 s 是红线），需逐段计时再削。首步 `card_being_played_from_hand` 冷启动 4.4 s（一次性）。回合首步等指挥点约 1/4 回合 2.0 s 超时，原因未明，待加逐轮询日志 |
| B2 | 后台降帧：用 frida 读 `t.IdleWhenNotForeground` / `bSmoothFrameRate`，前后台各测一段 `gtTicks` | 待做（需开一局；暂不做，始终在后台跑） | `BACKGROUND-THROTTLE-1.60.md` |
| B3 | 开局流程：余下物理鼠标脚本改走注入的 widget 点击；遵守“先 `end_of_match_continue` 再 `press_play`” | 待做 | |
| B4 | 开局导航（坐标点击）有风险：做成注入动词（`WBP_NUI_Sidebar_C` 的 `sidebarPlayButton/CardsButton/ShopButton`；模式列表 `WBP_NUI_Playbar_C`），弃用坐标点击；动鼠标/开对局前先问用户 | 进行中（模式切换已修：`select_mode_by_label`；页面切换未做） | `ops/flow.py` |

## C. 稳定性与清理
| # | 事项 | 状态 | 要点 |
|---|---|---|---|
| C1 | 崩溃相关：`obj_alive` 偏移与效果的实机验证；ProcDump 不抓 frida 自己的第一次机会 AV、清理旧转储；CLI `watch` 默认含 0x4000 与 termination | 待做 | `tools/crashdump.py` |
| C2 | 攻击修复后续清理：`ops/inject.py` 里 `pc_hover_rewritten` / `arrow_under_written` 两处已知无因果的写入，逐项删、逐项回归（后台 + 窗外 ≥12 次攻击）；带目标出牌/选目标/选手牌目标是否也依赖 `LocationUnderCursor/RowUnderCursor`；`move_up` 清箭头目标、`pick_candidates` 快速路径、回合开始 2 s 等待与横幅重试 | 待做 | |
| C3 | exe 未签名，杀软可能误报；发布前按 `v<版本>` 打标签再 `build_exe.py` | 待做 | `tools/build_exe.py` |
| C4 | 代码里残留的开发机绝对路径（`D:\Kards\game-installs\…`、`D:\SteamLibrary\…` 的 `path_hint` / `exes.py` 搜索表）：改成环境变量或相对 `base.paths` | 待做（不阻塞） | `kardsmem/{build,buildsrc,exes}.py` |

## A. 评估与模拟
| # | 事项 | 状态 | 要点 |
|---|---|---|---|
| A1 | **sim 按原版移植审计与迁移**：攻击合法性换 `CanAttack` 移植 → `sim_attack` 按 `ExecuteAttack`→`DamageCard` 逐行 → 回合开始/结束 → 移动上线 → `_apply_eff` 每个分支补出处 | 进行中 | `docs/SIM-FIDELITY.md`、`docs/P3-NATIVE-PORT.md`（工单与缺口 R1–R8） |
| A2 | P3 natives 全量移植：写侧叶子（`setAndEncrypt*` / `CustomName` 增删）→ `DamageCard`/`ApplyDamageToCard`（excess）→ `DestroyCard`/`MakeCardsFight` 复核 → 移动/转化/偷牌/老兵/治疗 → 攻击合法性/结算/回合；“部分”各行升级成“忠实”；`operationCostBuff` / `heavyArmorBuff` 字段；`OnAfterDefenseIsSet` + trigger 0x6 与 `maxDefense` 抬高 | 进行中 | `docs/P3-NATIVE-PORT.md` |
| A3 | 天气族缺口：HEATWAVE 的效果在常驻钩子 `OnOtherCardPlayedFromHand`（`_standing_enter_fx` 目前只看 `OnOtherCardEnterPlay`，要扩）；THUNDERSTORM 随机目标的身份没进 `eff` 字典（P5 直跑才修得掉）；`Plan.needs_target` 取自 `needs_hand_target`（选手牌），却被 `rule.py` 当“要不要指场面目标”用——另立 `needs_board_target`，改前逐处核对调用点（`rule.py` 的 896/1211/1322/1406/1583/2703 附近） | 待做 | `player/rule.py`、`kardsmem/board.py:196` |
| A4 | `IsReconnectMatch` 函数体（重连局三个出参）；`IsFrontlineLimited` 真值（现 `front_limited` 恒 False）；生成单位的关键词；其它“按计数不看排容量”的效果（`deck_add`、手牌上限） | 待做 | `docs/SIM-FIDELITY.md` |
| A5 | 钩子顺序与互斥：Q3 顺序敏感钩子清单；Q4/Q5 为源码推断，敌方激活反制后字段是否真 >0 需只读实测；读对方已挂反制的口径已定为“按 sim 原版 peek” | 待做 | `HOOK-ORDER-*-1.60.md`（reverse-data） |
| A6 | 洗切收益时序（IJN AKAGI）：先分清模拟层（`triggers` 是否按原版接洗切触发族）还是评估层取舍 | 待查 | `player/rule.py::_deck_shuffled_triggers` |
| A7 | 对手回合推演 | 很靠后 | |

## D. 架构（REFACTOR-PLAN 状态表）
| # | 事项 | 状态 | 指针 |
|---|---|---|---|
| D1 | P1 收尾：S3' 的属性读已清零（棘轮基线 0）。**剩 S4（删 `Card` 上的派生别名属性）**：还有约 80 处 `getattr(c, "card_id", None)` 这类按名字的鸭子类型读法（`engine/triggers*.py`、`player/rule.py`、`ops/play.py` 等，棘轮和 AST 看不见），直接删属性会静默变成 None；要先逐处改成读 `obj.xxx`（假卡/字典输入要一并核对），再删。纯离线可做，但无实机保护，需要时再动 | 待做 | `docs/REFACTOR-PLAN.md`、`docs/BOARD-MODEL.md` |
| D2 | P2/P3/P4：engine 骨架余项、natives 全量移植、`scripts` 直跑（`effectvm` 降为记录模式）| 进行中 | `docs/REFACTOR-PLAN.md` |
| D3 | P5：搜索切到 engine 状态上，删 `boardeval` 门面 | 待做 | |
| D4 | P6：拆 `player/rule.py`（约 3.3k 行）、`kardsmem/cli.py`、`kardsmem/board.py`；OPS 余下步骤（各动词改返回 `Result`、`answer_prompt` 接真动词） | 待做 | `docs/OPS-ARCHITECTURE.md` |
| D5 | P7：RVA S2 其余版本需对应游戏在跑做对账，之后 S4（表迁 `tests/fixtures/golden_rva.json`、缓存目录进 `.gitignore`）；H3 能力探测（只做被点名的） | 待做 | `docs/ARCH-RELEASE-VCS.md` |
| D6 | M5 类型化状态变更（Recorder 直接产出，字典仅给日志）；`policy/search.py` 里 24 叶 + 剪枝拆 `paths.py` | 长期 | `docs/ARCHITECTURE.md` |

## 已拍板
1. B1 不允许降束搜索参数。
2. 模拟里始终读对方已挂反制（peek），按 sim 原版，不做 fair 模式。
3. 随机 = 跑对局 RNG；抉择 = 各选项分开成动作。
