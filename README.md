# kards-agent

读 [KARDS](https://www.kards.com/) 客户端内存拿实时盘面，用模拟鼠标操纵客户端。

Fork 自 [OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto)（GPL-3.0），
把「靠 OCR 认盘面」换成「靠内存读盘面」，只保留了上游的窗口/鼠标/模板匹配等少量原语（见 `vendor/`）。

## 边界（重要）

**只读内存 + 模拟鼠标。**

- 只用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` + `ReadProcessMemory`
- **不注入、不 WriteProcessMemory、不远程线程、不 hook、不在游戏进程里执行任何代码**

蓝图字节码是**读出来在 Python 里解释**的，游戏进程内不执行任何东西。

## 能做什么

| | |
|---|---|
| `kardsmem` | 读侧：世界/盘面/卡牌/手牌/指挥点/候选牌/游戏内提示文本 |
| `kardsmem.props` | 走 UE 反射链，**按名字**把蓝图成员变量解析成偏移（跨构建自洽） |
| `kardsmem.objects` | 遍历 `GUObjectArray`，拿到 UMG widget 这类非 Actor 对象 |
| `kardsmem.kismet` | 反汇编运行时 Kismet 字节码（全游戏 10861 个函数解析通过） |
| `ops.py` | 执行侧：出牌 / 上前线 / 移动 / 攻击 / 结束回合 / 选择界面 |

```bash
python -m kardsmem selftest     # 自检
python -m kardsmem              # 看当前盘面
python ops.py state
```

## 版本

偏移表是为**某一个具体构建**写的，按 `SizeOfImage` 识别；换版本要重新标定。
字段偏移优先走反射链现算，代码里尽量不硬编码。

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。派生自 KARDS AUTO (C) 2026 yumehanab1。
