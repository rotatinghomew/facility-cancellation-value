"""每场无法入场与等待代价的底线。

底线来自 decision.json：

* ``max_denied``           每场最多无法入场人数（普通预约）
* ``protected_max_denied`` 每场高损失（protected）预约最多无法入场人数
* ``max_wait_minutes``     候补确认可接受的最大等待代价

无法守住底线的策略建议只能停留在比较页面，不能进入值班可确认的规则清单。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import DaySimulation, PENDING


@dataclass(frozen=True)
class Guardrails:
    max_denied: int
    protected_max_denied: int
    max_wait_minutes: int

    @classmethod
    def from_decision(cls, decision: dict) -> "Guardrails":
        return cls(
            max_denied=int(decision["max_denied"]),
            protected_max_denied=int(decision["protected_max_denied"]),
            max_wait_minutes=int(decision["max_wait_minutes"]),
        )


@dataclass
class GuardrailReport:
    denied: int
    protected_denied: int
    max_wait: int
    pending: bool

    @property
    def passes(self) -> bool:
        if self.pending:
            return False
        return (self.denied <= self.limits.max_denied
                and self.protected_denied <= self.limits.protected_max_denied
                and self.max_wait <= self.limits.max_wait_minutes)

    limits: Guardrails

    def violations(self) -> list[str]:
        out = []
        if self.pending:
            out.append("含待观察时段，不能确认守底线")
        if self.denied > self.limits.max_denied:
            out.append(f"无法入场 {self.denied} 人 > 上限 {self.limits.max_denied}")
        if self.protected_denied > self.limits.protected_max_denied:
            out.append(f"高损失预约无法入场 {self.protected_denied} 人 > "
                       f"上限 {self.limits.protected_max_denied}")
        if self.max_wait > self.limits.max_wait_minutes:
            out.append(f"等待 {self.max_wait} 分钟 > 上限 {self.limits.max_wait_minutes}")
        return out

    def warnings(self) -> list[str]:
        """未违规但已贴线、无容错的提示。"""
        out = []
        if not self.pending and self.denied == self.limits.max_denied and self.denied > 0:
            out.append(f"无法入场 {self.denied} 人已达上限，无容错空间")
        return out


def evaluate_guardrails(sim: DaySimulation, limits: Guardrails) -> GuardrailReport:
    denied = protected_denied = 0
    max_wait = 0
    pending = False
    for entry in sim.entries:
        if entry.outcome == PENDING:
            pending = True
            continue
        if entry.denied_at_door:
            denied += 1
            if entry.booking.protected:
                protected_denied += 1
        if entry.accepted and entry.booking.is_waitlist:
            # 候补从确认到开场的等待代价
            max_wait = max(max_wait,
                           max(0, entry.booking.start - entry.decided_at))
    return GuardrailReport(denied, protected_denied, max_wait, pending, limits)
