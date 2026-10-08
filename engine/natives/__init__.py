# -*- coding: utf-8 -*-
"""engine.natives —— 原版原生函数的 Python 移植，一个族一个文件（board / card / battle …）。

约定（`docs/SIM-FIDELITY.md` §1 + REFACTOR-PLAN §3）：
  * 每个函数、每个分支写 `原版：<BP 函数>（文件:行）`；没有出处的规则 = 待审计；
  * 读不出 / 没移植的分支：记缺口、价值 0，**不编数**；
  * 本包不 import `sim`（依赖方向：kardsmem → engine → sim）：这些函数只认鸭子类型协议。
"""

