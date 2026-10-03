"""ops —— 执行侧（OPS）包（`OPS-ARCHITECTURE.md` §5/§7）。当前阶段：JS 抽成独立文件（步骤 2）；其余仍在 `ops/inject.py`。

`agent.js.tpl` 是 **Python `%` 格式模板**（占位符 `%(module)s` 等，由 `ops/inject.py` 在 import 时用 RVA 等填充）。
改 JS 需要**重启监听器**（脚本在 attach 时注入，热重载不会重发）。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_js_template(name: str = "agent.js.tpl") -> str:
    with open(os.path.join(HERE, name), "r", encoding="utf-8", newline="") as f:
        return f.read()
