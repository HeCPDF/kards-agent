"""KARDS 机器人的本地控制面板（tkinter，纯标准库）。

分三层，GUI 本身**不碰游戏**（不 attach、不点鼠标、不置前台）：
  * `control`  —— 控制文件（面板写，监听器里的 `autoplay` 读）+ 状态文件（反过来）；
  * `autoplay` —— 在常驻监听器 `live_session.py` 里 `exec` 的“打一局→自动开下一局”编排；
  * `history`  —— 读 `rule-live-*.jsonl`，把每一步决策整理成表；
  * `app`      —— 面板本体：`python -m gui.app`。
"""
