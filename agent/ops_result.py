# -*- coding: utf-8 -*-
"""agent.ops_result —— OPS 动词的统一结果类型（OPS 文档 §3/§4/§7 步骤 1；总纲 §7）。

规则（把弯路 #22/#28 变成类型约束）：
  * `ok` **只能**由动作流回执构造（`Result.from_receipt`）；旁证（候选集合变了、选中翻转了、界面还开着、`prompt` 出现）
    只能进 `evidence`，类型上**不影响** `ok`；
  * 失败要给 `reason`（枚举字符串）而不是只给 `ok=False`；
  * `prompt`：动作之后游戏挂起的提示（`agent.promptinfo.PromptInfo`），只作证据。
现有动词先用 `adapt(legacy_dict)` 包一层，行为不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

REASONS = frozenset((
    "awaiting_choice", "awaiting_target", "actor_not_alive", "layer_gone", "rejected_by_game", "panel_open",
    "no_receipt", "gate_blocked", "not_our_turn", "unsupported", "error", "other"))


@dataclass(frozen=True)
class Receipt:
    """动作流里"这一步被消费了"的证据。只有它能让 `Result.ok` 为真。"""
    action: str                      # 动作流里的动作名（如 XActionPlayCardFromHand）
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    ok: bool
    criterion: str = "action_stream"
    receipt: Optional[Receipt] = None
    reason: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)
    attempts: int = 1
    prompt: Any = None               # PromptInfo | None —— 只作证据

    @classmethod
    def from_receipt(cls, receipt: Receipt, **kw) -> "Result":
        return cls(True, receipt=receipt, **kw)

    @classmethod
    def fail(cls, reason: str, **kw) -> "Result":
        if reason not in REASONS:
            reason = "other"
        return cls(False, reason=reason, **kw)

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "criterion": self.criterion, "reason": self.reason, "evidence": dict(self.evidence),
             "timing": dict(self.timing), "attempts": self.attempts}
        if self.receipt is not None:
            d["receipt"] = {"action": self.receipt.action, "detail": dict(self.receipt.detail)}
        if self.prompt is not None:
            d["prompt"] = getattr(self.prompt, "kind", str(self.prompt))
        return d


_RECEIPT_KEYS = ("receipt", "action_hit", "hit", "matched_action")


def adapt(legacy: dict, prompt=None) -> Result:
    """把现有动词返回的字典包成 `Result`（行为不变）：`ok` 沿用原字典的 `ok`，且**只有原字典自己声明了动作流判据**
    （`criterion == "action_stream"` 或带回执键）才算有回执；其余一律把 `ok` 降为"无回执"证据而不是成功。"""
    legacy = legacy or {}
    crit = legacy.get("criterion")
    has_receipt = crit == "action_stream" or any(legacy.get(k) for k in _RECEIPT_KEYS)
    ev = {k: v for k, v in legacy.items() if k not in ("ok", "criterion", "reason", "error")}
    if legacy.get("ok") and has_receipt:
        return Result(True, criterion="action_stream",
                      receipt=Receipt(str(legacy.get("action") or legacy.get("matched_action") or "?")),
                      evidence=ev, prompt=prompt)
    if legacy.get("ok"):
        return Result(False, criterion=crit or "unknown", reason="no_receipt", evidence=dict(ev, legacy_ok=True), prompt=prompt)
    return Result.fail(legacy.get("reason") or "rejected_by_game" if legacy.get("error") else "other",
                       evidence=dict(ev, error=legacy.get("error")), prompt=prompt)
