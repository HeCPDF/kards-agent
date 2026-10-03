# kards-agent

读 [KARDS](https://www.kards.com/) 客户端内存拿实时盘面，在游戏进程内**合成鼠标事件序列**操纵客户端
（不挪真实光标、不抢焦点）。

Fork 自 [OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto)（GPL-3.0），
把「靠 OCR 认盘面」换成「靠内存读盘面」，只保留了上游的窗口/模板匹配等少量原语（见 `vendor/`）。

## 边界（重要）

**读侧只读内存；写侧在游戏进程内复刻真实鼠标的事件序列。**

- 读：只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`
  （蓝图字节码是**读出来在 Python 里解释**的）
- 写：**判据只有一条** —— 发给服务端/对手的信息流必须跟真人用鼠标操作产生的**完全等价**
  （`ops.inject`：悬停 → 悬停转发 → 按下 → 起拖 → 拖动 tick → 落地/松开，**不跳步**）
- 旧的两条硬禁令（"不注入、不写内存"、"一律物理鼠标"）已在 2026-09-25 收窄为上面那一条；
  物理鼠标实现 `ops.py` 随之于 2026-09-27 **归档**（`_archive/ops_mouse.py`，停用）

## 能做什么

| | |
|---|---|
| `kardsmem` | 读侧：世界/盘面/卡牌/手牌/指挥点/候选牌/游戏内提示文本 |
| `kardsmem.props` | 走 UE 反射链，**按名字**把蓝图成员变量解析成偏移（跨构建自洽） |
| `kardsmem.objects` | 遍历 `GUObjectArray`，拿到 UMG widget 这类非 Actor 对象 |
| `kardsmem.kismet` | 反汇编运行时 Kismet 字节码（全游戏 10861 个函数解析通过） |
| `ops/inject.py` | **执行侧（唯一）**：出牌 / 指向（指令一次成交 · 单位两阶段）/ 上线 / 攻击 / 抉择 / 换牌 / 结束回合 |
| `agent/` | 命令层：`AgentSession` 的唯一动词集合 + 交互 shell + MCP server + 只读录制器 |
| `tools/` | 取材 / 标定 / 探针（内存探针、minidump、坐标标定、PE 解析…） |
| `_archive/ops_mouse.py` | 旧的物理鼠标执行侧（**已停用**，只作留档/对照；行模型回归仍在跑） |

```bash
cd D:\Kards\kards-agent
python -m kardsmem selftest                 # 读侧自检
KARDS_BUILD=launcher_default python ops/inject.py selftest   # 执行侧自检（按构建选表）
python -m interfaces.shell                       # 交互式
```

接续任务先读 **`CLAUDE.md`**（红线 / 判据原则 / 已知弯路 / 发布纪律 / 下一步），
主规格在 `..\reverse-data\reports\spec\KARDS-AUTOMATION.md`。

## 版本

偏移表是为**某一个具体构建**写的，按 `SizeOfImage` 识别；换版本要重新标定。
字段偏移优先走反射链现算，代码里尽量不硬编码。

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
派生关系与上游出处见 [NOTICE](NOTICE)。
