# crashdump.py —— 崩溃时自动落完整内存 dump

## 起

```powershell
# 常驻等崩溃（等 kards-Win64-Shipping.exe 出现；它重启后自动再挂）
python D:\Kards\kards-agent\tools\crashdump.py watch

# 只想跑一段（验收/试挂）：跑 300 秒后自己收工
python D:\Kards\kards-agent\tools\crashdump.py watch --duration 300

# 换成自研调试器循环（能在**落盘前**按故障指令 RVA 过滤，一份都不浪费）
python D:\Kards\kards-agent\tools\crashdump.py watch --engine native

# 零冻结抓（-r 反射；代价：私有堆页大半不进 dump，见下面「引擎」）
python D:\Kards\kards-agent\tools\crashdump.py watch --engine procdump --reflect
```

默认引擎是 **procdump**（微软 ProcDump 抓 + 我们事后按 RVA 判定 + 补元数据）。
ProcDump 必须先手动 `-accepteula` 过一次（见文末）；没接受会自动退回 `native`。

Ctrl+C 结束时它**先排空调试事件、再 DebugActiveProcessStop**，不会把游戏留在挂起态。
停止 ProcDump 引擎时给它发 CTRL_BREAK（正常退出），**绝不 kill**（杀调试器会把游戏带崩）。

不给 `--pid` 就按进程名等（默认 `kards-Win64-Shipping.exe`，不会盯启动器 `kards.exe`）。
白名单默认 `kards-Win64-Shipping.exe+0x11d37e6` 与 `+0x11ad577`；要改：

```powershell
python tools\crashdump.py watch --rva 0x11d37e6 --rva "*+0x123456"   # "*"=任意模块
python tools\crashdump.py watch --rva "*"                            # 任意 AV（自测用）
```

权限：同用户、同完整性级别的游戏**不需要管理员**。真被拒（err=5）时按提示来：

```powershell
sudo --inline python D:\Kards\kards-agent\tools\crashdump.py watch
```

提权跑时 `status.json` 里 `"elevated": true`。

## 读 status.json

`D:\Kards\kards-data\crashdumps\status.json`（**原子写**：先写 .tmp 再 replace，轮询不会读到半截）

```powershell
python tools\crashdump.py status          # 人读
python tools\crashdump.py status --json   # 给 agent
```

| 字段 | 说明 |
|---|---|
| `state` | `waiting`（没进程）/ `attached`（挂着）/ `dumping`（正在写）/ `dumped`（**已经落过至少一份**，仍在监视）/ `error` |
| `pid` / `targets` | 当前目标；`targets` 列出全部被附加的进程 |
| `since` | **当前这个 state 从什么时候开始**（epoch，跟 `time.time()` 同基准） |
| `dumps[]` | 每份：`path` `size` `exc_code` `fault_rva` `tid` `time`（+ `json` `frozen_seconds` `reason`） |
| `error` | 最近一次错误（含权限、磁盘、dbghelp 失败）；成功一次就清掉 |
| `exc_counts` | **first-chance 异常码 → 次数**（用来判断游戏平时有多少异常） |
| `events` | 调试事件计数（`EXCEPTION` `CREATE_THREAD` `LOAD_DLL` …） |
| `stats` | `handle_max_ms`（单个事件处理最久多久，卡不卡看它）、`frozen_max_ms`（写 dump 冻了多久）、`dumps` `refused` |
| `self_usage` | 它自己吃多少：`cpu_s` `cpu_pct` `working_set_mb` |
| `elevated` | 是不是提权跑的 |

`state=dumped` + `dumps` 变长 = 有新 dump 可读。

## 手动 dump

```powershell
python tools\crashdump.py now                      # 自动挑 kards-Win64-Shipping.exe
python tools\crashdump.py now --pid 30256 --tag 复现1
```

`now` 走纯 `MiniDumpWriteDump`（**不附加调试器**），所以没有异常流：`.ecxr` 用不了，
现场看同名 `.json` 里的 `threads[].rip`，cdb 里 `~<tid>s; r; k`。

## dump 放哪、旁边有什么

`D:\Kards\kards-data\crashdumps\`

| 文件 | 内容 |
|---|---|
| `<名字>.dmp` | 完整内存（`MiniDumpWithFullMemory` + HandleData + ThreadInfo + UnloadedModules + FullMemoryInfo + ProcessThreadData） |
| `<同名>.json` | 异常码/故障 RIP 与 RVA/模块基址/寄存器/全部线程 tid+RIP/进程运行时长(秒)/UTC+本地时间/模块表/`exe` 的 SizeOfImage+md5+PE TimeDateStamp/`memory_summary` |
| `<同名>.context.txt` | 崩溃时刻 `nn\logs\rule-live-*.jsonl`（最新）与 `live_log.txt` 的**最后 30 行**原样拷贝 |
| `<同名>.memory.csv` | `VirtualQueryEx` 走一遍的地址空间图（`base,size,state,protect,type`）—— 野指针落在**已提交/已保留/未映射**哪一类，看它 |
| `<同名>.txt` | `analyze` 的 cdb 原始输出 |

dump 文件名带时间戳、异常码、故障 RVA（例：`kards-20260930-213928-cc0000005-p12032-rva12b87.dmp`）。
写之前查盘：目标盘 < 10GB 空闲就**拒绝写**并在 `status.json` 里报 `error`；同一进程一次运行最多 3 份。

## 用 cdb 打开

```powershell
# 一条命令全办了（结果写 <同名>.txt，summary 走 stdout）
python tools\crashdump.py analyze D:\Kards\kards-data\crashdumps\<名字>.dmp

# 手敲
cd "C:\Program Files (x86)\Windows Kits\10\Debuggers\x64"
.\cdb.exe -z <名字>.dmp -y D:\Kards\kards-data\crashdumps\symbols -c ".ecxr; k 30; r; !analyze -v; ~*k 8; q"
```

`analyze` 只喂**本地空符号目录**（`-y` + 覆盖 `_NT_SYMBOL_PATH`），所以**不需要符号服务器**，
也不会去够网络；没有 PDB 时靠模块导出表。`!address` / `!heap` 这类扩展在缺符号时会报错，
那就用 `<同名>.memory.csv` + `<同名>.json` 里的内存表（我们自己解出来的，不依赖符号）。

顺手回答「这个地址是什么 / 这个委托列表里哪一项被释放了」：

```powershell
python tools\crashdump.py analyze <名字>.dmp --heap 0x1e3020030   # !address/dqs/!heap -x/ln + 走一遍 TArray
python tools\crashdump.py analyze <名字>.dmp --delegates          # 寄存器/栈上找 16B/项 的委托列表，标可疑项
```

## 引擎

两种抓取引擎，同一套输出/状态/API：

| | `native`（备用） | `procdump`（**默认**） |
|---|---|---|
| 抓法 | 自研 `DebugActiveProcess` 循环 + `MiniDumpWriteDump` | 微软 ProcDump 子进程，我们监督 + 补元数据 |
| **按故障指令 RVA 白名单过滤** | ✅ 支持（需求 #1） | ❌ 做不到（`-f` 只能按异常码），只能**事后**判定 |
| first-chance 命中即时落 / 任意 second-chance 落 | ✅ | 只能 `-e 1 -f C0000005` 或 `-e`（二选一） |
| 一次崩溃落几份 | 1 份（RVA 不中就不写） | ⚠ 实测**一次 AV 落了 3 份**（first+second+终止），每份都是完整内存 |
| 实测冻结（游戏，2.79GB） | 3.2s | 8.1s（不加 -r）/ **0.0s**（加 -r，但见下） |
| 自身 CPU | 1.2%（常驻 5 分钟实测） | 2.5%（45 秒实测） |

### `-r`（反射/克隆）**默认不要开**

实测（同一次游戏现场，探针每 200ms 给游戏窗口发 WM_NULL 量响应）：

| 方式 | 墙钟 | 游戏最久冻结 | dump 大小 | 私有堆 | cdb `dq` 抽查 |
|---|---|---|---|---|---|
| ProcDump `-ma -r` | 4.3s | **0.0s** | 1892 MB | 0.76 GB | 3 个地址里 2 个 `????????` |
| ProcDump `-ma` | 6.8s | 8.1s | 2792 MB | 1.59 GB | 全部有值 |
| 自研 MiniDumpWriteDump | 3.7s | 3.2s | 2792 MB | 1.59 GB | 全部有值 |

⇒ `-r` 用 PSS 克隆，**私有堆页大半不进 dump**（实测前 200 个私有堆区段 1.13GB 里
0.83GB = 73% 读不出来）。异常上下文它倒是保得住（`.ecxr` 照样停在故障指令上，
selftest D2 幕在测），但"看清被访问的对象"正是完整 dump 的全部意义，所以：

* `watch`（崩溃）：默认**不加** `-r`。想零冻结再加 `--reflect`。
* `now`（手动）：默认也**不加** `-r`；要零冻结用 `--reflect`，并接受堆页可能缺失。

ProcDump 的 EULA **第一次必须你手动接受**（本工具不替你点）：

```powershell
H:\Tool_Collections\Folder\SysinternalsSuite\procdump64.exe -accepteula
```

### ProcDump 监督的四条硬规则（2026-09-30 修完 bug 后）

1. **只认启动之后新落的 `.dmp`**：启动前目录里已有的（别处放的 `now` dump、上一轮的
   dump）不会被当成"刚抓到崩溃"——否则 `status.json` 会假报 `dumped`。
2. **附加上不去就退避 + 报错**：ProcDump 立刻退出（最常见原因：目标已被别的调试器/
   ProcDump 占着，它自己会说 `The process is already being debugged.`）时，退避
   2→4→6…最多 60 秒，并把原因（含它自己最后几行输出）写进 `status.json.error`。
   不会每 2 秒无限重试。
3. **`stop()` 不谎报**：Ctrl+Break 送不到时会返回 `False` 并报错，而不是说"停好了"。
4. 它自己的输出落 `<out_dir>/procdump.log`（不是没人读的管道），出问题能直接查。

手工收游离的 ProcDump（**CTRL_BREAK，绝不 kill**）：

```powershell
python tools\crashdump.py stop-procdump              # 只列出来（含父进程），不动手
python tools\crashdump.py stop-procdump --pid 1234   # 收这一个
python tools\crashdump.py stop-procdump --all        # ⚠ 收掉本机所有 procdump64
```

（`--pid` 形式先只打它自己的进程组；送不到才退化成广播。）

没接受就直接 `--engine procdump` 时它会在 status.json 里报错并把这条命令原样打给你。
