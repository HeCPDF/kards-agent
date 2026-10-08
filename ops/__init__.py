"""ops —— 执行侧（OPS）包（`OPS-ARCHITECTURE.md` §5/§7）。当前阶段：JS 抽成独立文件（步骤 2）、L1 原语抽成 `primitives.py`（步骤 3）、只读查询与写型诊断分离（步骤 4）；`ops/inject.py` 已拆成 mixin 模块（conn/world/gesture/query/diag/play/choices/flow/cli），自身只剩兼容门面。

分层（§2）：`conn`（L0 传输 + 反射缓存 + 原语转发）/ `primitives`（**L1 原语，写类 RPC 的唯一实现处**）/
`world` / `gesture`（L2）/ `query`（**只读**查询，不得具备写字段能力）/ `diag`（**写型诊断**，如
`can_move_to(simulate_drag=True)`、`probe_canplay_targeted`）/ `play`/`choices`/`flow`（L3 动词与菜单流程）/
`cli`（自检与命令行）。依赖方向只能向下，判据见 `tests/test_ops_rules.py`。

`agent.js.tpl` 是 **Python `%` 格式模板**（占位符 `%(module)s` 等）。★ P7-S3b 起由 `ops/consts.render_js()`
在 **attach 那一刻**按当前 `build.RVA` 渲染（不再 import 期烤值）；已经注入过的会话仍拿着旧脚本 ——
改了 JS/RVA 又要对**当前已 attach 的会话**生效，才需要重启监听器（热重载只重载 Python，不重发 frida 脚本）。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_js_template(name: str = "agent.js.tpl") -> str:
    with open(os.path.join(HERE, name), "r", encoding="utf-8", newline="") as f:
        return f.read()
