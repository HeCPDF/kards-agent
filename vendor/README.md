# `kards-agent/vendor/` —— 从上游搬来的最小集

来源：**[OCR-Kards-Auto](https://github.com/yumehanab1/OCR-Kards-Auto)**（yumehanab1，GPL-3.0）。
本项目 **fork** 自它，因此同样是 **GPL-3.0**（见 `../LICENSE`）。

**上游对齐版本：`576aa19`（v0.1.8_beta，2026-09-22 拉取）。**

## 为什么是这六个（逐个查过引用，不是"先搬过来再说"）

| 模块 | 干什么 | 谁在用 |
|---|---|---|
| `win.py` | 窗口 / DPI / 客户区坐标 / 截图 / 置前 | `ops.py`、`screen.py`、`shot.py`、`grab.py`、`tools/*` |
| `actions.py` | 鼠标原语（含 2026-09-22 上游新增的**窗口消息输入** `click_message`/`drag_message`） | `ops.py`、`mull_auto.py`、`do_mulligan.py`、`tools/*` |
| `deploy.py` | `drag_deploy` 拖拽出牌手势 + `fallback_drop`（指令卡不占槽位时的兜底落点） | `ops.py` |
| `ui_state.py` | 模板匹配、界面分类 | `screen.py`、`startmatch.py`、`board_api.py` |
| `cv_io.py` | 让 cv2 认中文路径（`ui_state` 的依赖） | `ui_state` |
| `handedge.py` | 手牌扇形**左右边缘**（`detect_left_edge`）—— **内存里没有这个量** | `ops.py` |

上游其余部分我们**不用**（OCR 读盘面、自动打牌状态机、官网卡表 json、手牌扫描 1551→1892 行的整条链），
一次性的东西在 `../_archive/`。

## 更新流程

```powershell
cd D:\Kards\OCR-Kards-Auto && git pull --ff-only && git log --oneline -1   # 记下 rev
# 只挑我们用的那几个模块重新复制（其余别动）
Copy-Item OCR-Kards-Auto\src\actions.py D:\Kards\kards-agent\vendor\actions.py -Force
# 头两行补回来源注释（含新 rev），然后：
cd D:\Kards\kards-agent && python -m kardsmem selftest
```

规矩：
1. **只复制我们用的模块**，且复制后必须补 `# vendored from ... @ <rev>` 注释。
2. 复制前先看 diff 是不是**纯增量**：`git -C OCR-Kards-Auto diff <旧rev> HEAD -- src/actions.py`
   —— 有删改就要评估我们调用点的兼容性。
3. `handedge.py` 是从 `src/hand_calibrate.py` **抽出来的四个函数**，不是整文件复制；上游那个文件
   连着整条 OCR 链，整搬会把 1500 行拖进来。抽的时候保留原注释里那条约束：
   **必须在鼠标停在安全点、没悬停任何手牌时调用**，否则扇形展开、边缘会漂。
4. 上游仓库**一个字都不要改**（别人的工作树）；要动它之前先 `git -C OCR-Kards-Auto status` 确认干净。

## 上游 2026-09-22 那批更新里，值得我们知道但**还没接**的东西

- **窗口消息输入**（`actions.INPUT_MODE = "message"`，上游默认 `"physical"`）：用 `PostMessage`/`SendMessage`
  发 `WM_*` 鼠标消息，**不移动真实光标**。上游标注「还在实机实测阶段、默认不启用」，配套探针是
  `dev/click_probe.py`（它自己写明一条判据：**探针说能用才把 `INPUT_MODE` 改成 `"message"`**）；
  而且「游戏不认窗口消息」和「坐标错了」长得一模一样，别误判。
  ⚠ **用户 2026-09-22 定调：这条路将来要被「基于内存的实现」替换 ⇒ 它是过渡手段，别把架构压在它上面。**
  当前我们**还没接**（用的是 vendor 里默认的物理光标路径）。
- `deploy.fallback_drop()`：指令卡**不占槽位**，支援线满时 `deploy_candidates()` 返回空，这条兜底落点就是给那种情况用的。
- `src/order_target.py` / `src/orders.py` + `docs/指令卡清单.md`：上游做的"指令卡要不要选目标"分类
  （286 张 direct 已跑通）—— 和我们的 R8（可指向目标）是同一个题目，可以对表。
