# TODO

> **只放没做完的事。** 每条一个事项、一个状态、一个指针；细节写进指针指向的文档，不在这里写日志。
> 状态：待做 / 进行中 / 待验证（代码已落地，缺实机）/ 阻塞。
> 总目标：攻击在游戏后台稳定成功（已达成）；单回合 ≤35 s（软验收）；发给服务端的信息流与真人等价（红线）。
> 2026-10-09 起：需要“制造特定局面”的实机验证不再靠开局碰运气，等本地私服开发工具可用后再做（见 S 组）。
> 旧版看板（含各条的详细经过）已归档到伞目录 `docs-local/history/TODO-board-until-20261009.md`。

## S. 本地私服开发工具（用户 2026-10-09 定的方向）
| # | 事项 | 状态 | 指针 |
|---|---|---|---|
| S1 | **指定服务器启动 = 改 live server 地址**。**线索（已有大量一手资料，不必从零查）**：① 客户端只有两个写死的 URL 字面量：`https://kards.live.1939api.com/config`（`FetchConfig`）与 `https://kards.live.1939api.com/`（`GetServerUrl()` 兜底，`ServerConfig+0x6d0` 有值就用它）；② **`localConfig.json`（键 `kardsroot`）**：2026-09 的实测说无效（写了 9 个目录仍连 live），但**用户 2026-10-09 反馈：残留的 `localConfig.json` 在后来某次启动中生效了**——两次结论冲突，条件未查清（哪个目录、哪个 exe/构建、是否同时有补丁、文件内容），**先复现并记下生效条件**，若可靠就是不改游戏文件的首选方案；③ **已验证有效的是改 exe 的两个字面量**（1.57 文件偏移 `0x7DF5740` root / `0x7E4DC58` /config，等长 NUL 补齐，大小与 RVA 不变）：`server/tools/patch_exe.py --root http://127.0.0.1:5231/ [--apply]` 产出 `.patched.exe`；`default\game\…\kards-Win64-Shipping2.exe` 就是补丁版；④ 备选：hosts + mitmproxy + 根证书（要管理员）。**待办**：在不改游戏文件的前提下覆盖（进程内改 `ServerConfig->ServerUrl` 或拦 `GetServerUrl`，RVA 要走 `kardsmem.build` 扫描而不是写死）；对 1.60 / 1.58 现行构建重做一遍偏移；与 S2、S3 相容（补丁版 exe 的 md5 会变，`kardsmem exes` 按 `SizeOfImage` 认，不受影响）| 待做（线索已齐，未开工）| `server/LOCAL_SERVER_PLAN.md`（§1.1–1.3、§3.2）、`reverse-data/reports/ledger/EXE-IDENTITY.md`、`client/README.md` |
| S2 | **指定凭据启动游戏**。**线索**：游戏的登录凭据在 `HKCU\Software\Kards` 的两个值 `device_id` / `device_password`（注册表里现存的是已被封的对照账号，**值不要打印、不要进仓库**）；自制客户端 `client/` 已改成“显式参数 → 环境变量 `KARDS_DEVICE_ID` / `KARDS_DEVICE_PASSWORD` → 注册表兜底”；Xsolla launcher 另有 `HKCU\SOFTWARE\XSOLLA\…\default :: prefix` 指向安装树。**两条路**：① 启动前写这两个值（简单，但多实例时互相覆盖）；② 进程内覆盖读取（要先找到游戏读注册表的调用点，IDA 查 `RegQueryValueEx`/`FPlatformMisc::GetRegistry*` 的 xref，按实例传不同值，与 S3 配合）。**待办**：确认读取时机（每次启动读一次还是登录时）、凭据格式与过期；写入前备份原值。只用用户本人账号，不做账号轮换 **IDA 结论（2026-10-09，1.60.27292.launcher，imagebase 0x140000000，`real_FetchConfig`@0x144b29050）**：`FetchConfig` 的解析顺序＝读 `<RootDir>/localConfig.json` 里的 `server_config_file`（有值就直接用作 config URL）→ 否则读同文件的 `tier`：无 ⇒ `live`，`local` ⇒ `dev`，其它值原样 ⇒ 拼成 `https://kards.<tier>.1939api.com/config`。拿到 URL 后：以 `http` 开头 ⇒ 发 GET；**否则当本地文件路径读**（失败日志 `Can't find server config file on a local machine: %s`）。⇒ **`localConfig.json` 是官方留的开发口，路径是 `RootDir`（`sub_144B33980` 里 `FPaths::RootDir` + 相对路径 + 文件名）下的 `localConfig.json`，键 `server_config_file` / `tier` / `kardsroot`**，之前“写了不生效”多半是放错目录或缺 `server_config_file`（`kardsroot` 管蓝图路径、`server_config_file` 管原生 `/config`）；`/config` 回的 JSON 里再带 `ServerUrl`（`ServerConfig+0x6d0`）才决定后续所有请求的根。另有 `tier` 通道不需要写 URL：把 `tier` 设成别的名字也能把 `/config` 指到别的子域（要配 hosts/证书，不如直接 `server_config_file`）。**下一步**：① 在 1.60 上实验：`RootDir` 到底是哪一级（`kards\..\` 还是 exe 同级）；用 `Procmon`/只读 `CreateFileW` 观察游戏找 `localConfig.json` 的路径；② `server_config_file` 填 `http://127.0.0.1:5231/config` 后看 `/config` 是否被拿走、`ServerUrl` 是否随私服返回值更新。 **IDA 结论（同上）**：读写凭据的是 `sub_144AF3710`（0x144af3710，调用者 `sub_144AC2E60`）：先 `sub_144AFA0D0` 用 `RegOpenKeyExW(HKCU, "SOFTWARE\Kards\%s", …)` + `RegQueryValueExW` 读 `device_id`；**读不到就现生成**（`device:Windows-<GUID>`，密码另一个 GUID）并 `RegCreateKeyExW`+`RegSetValueExW` 写回，`device_password` 同理。`%s` 是全局字符串 `qword_1493AEB58`，**在 `sub_144ADD220` 里被设成会话对象 `+696` 的字符串**（登录/会话建立之后才赋值），启动最早期为空 ⇒ 键＝`HKCU\Software\Kards`；之后可能变成 `…\Kards\<某标识>` 的子键（标识含义没查，**下一步**：看 `sub_144A75B40`→`sub_144ADD220` 的 `a1+696` 来源）。`real_ClearRegistryValues`（0x144adcc70）会清这两个值（登出路径）。⇒ **提前写注册表＝可行且游戏自己就是这么存的**（REG_SZ，值格式 `device:Windows-<GUID>` 与另一个 GUID）；进程内覆盖则可钩 `sub_144AFA0D0`（读值）按实例返回不同凭据，是 S3 多实例的天然接口（RVA 要用 `kardsmem.rvascan` 做签名扫描，不写死）。 | 待做（线索已齐，未开工）| `client/README.md`、`client/PROTOCOL.md` |
| S3 | **多游戏实例**。**线索（代码里现成的钩子）**：① `base/paths.py` 的 `LIVE_DIR`（命令/日志通道）、`GUI_DIR`、`DATA` 都已能用环境变量 `KARDS_LIVE_DIR` / `KARDS_GUI_DIR` / `KARDS_DATA_DIR` 覆盖——**按实例各给一个目录**就能先把通道与日志分开，不用改代码；② `kardsmem/proc.py::attach(pid=…)` 已支持指定 pid，但 `_default` 是**进程级单例**（`pid is None` 时复用）、`reuse` 默认真；③ `gui/watcher.py::pids()` 按 `--listener` 找监听器进程，没有“监听器 ↔ 游戏 pid”的映射；④ `ops/support.py` 的 `ops_inject_cache.json`、`kardsmem/locres.py::_CACHE`、`rendered.py` 的名字缓存、RVA 缓存都是全局的；⑤ frida 临时目录与 `play_guard` 闸门按进程。**待办**：监听器启动参数 `--pid`；`_default` 改成 `{pid: Session}`；缓存键加 pid；面板里实例选择；多窗口截图按 hwnd。先做“环境变量分目录 + 每个监听器各绑一个 pid”的最小版 | 待做（线索已齐，未设计）| `base/paths.py`、`kardsmem/proc.py`、`gui/watcher.py` |
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
