"""值班员的营业日规则确认与候补确认流程。

规则：

* 值班只能从“适用策略”中为某个营业日确认规则；不适用或守不住底线的策略
  只能停留在比较页面。
* 同一组合的多个候补不能都收到确定通知；候补确认按到达先后，整体可行性
  通过才发通知。
* 发出确认后，仅在参与方实际接受替代时段后才释放原承诺（与引擎一致）。
* 重算结果只让尚未发出的预约使用，已发出的确认不追溯修改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import SimulationEngine
from .model import Booking, Dataset, EVENT_RESCHEDULE
from .policy import Policy
from .resources import conflicts, feasible
from .risk import RiskModel, cell_key
from .timeline import Timeline


@dataclass
class DayRule:
    day: str
    policy: Policy
    overbook_limit: int
    source: str
    confirmed: list[str] = field(default_factory=list)
    waitlist_notified: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def is_locked(self, booking_id: str) -> bool:
        return booking_id in self.confirmed or booking_id in self.waitlist_notified


class DayPlanner:
    def __init__(self, dataset: Dataset, applicable: dict[tuple, list[Policy]],
                 risk_model: RiskModel | None, overbook_limit: int,
                 max_wait_minutes: int):
        self.dataset = dataset
        self.applicability = applicable
        self.risk_model = risk_model
        self.overbook_limit = overbook_limit
        self.max_wait_minutes = max_wait_minutes

    def confirm_rule(self, day: str, policy: Policy, reason: str) -> DayRule:
        """值班员从适用策略中确认当日规则；不适用策略直接拒绝。"""
        day_bookings = self.dataset.day_bookings(day)
        for booking in day_bookings:
            key = cell_key(self.dataset, booking)
            allowed = self.applicability.get(key, [])
            if policy not in allowed:
                raise ValueError(
                    f"{policy.value} 对组合 {key} 不适用或未守住底线，"
                    "只能停留在比较页面，不能确认为当日规则")
        return DayRule(day=day, policy=policy,
                       overbook_limit=self.overbook_limit, source=reason)

    def run_duty_day(self, rule: DayRule, observation_cutoff: int | None = None):
        """按已确认规则跑当日值班仿真；已发出确认不被后续重算追溯。"""
        engine = SimulationEngine(
            self.dataset, rule.policy, self.risk_model,
            rule.overbook_limit, self.max_wait_minutes)
        sim = engine.run_day(rule.day, observation_cutoff=observation_cutoff)
        for entry in sim.entries:
            if not entry.accepted:
                rule.rejected.append(entry.booking.id)
            elif entry.booking.is_waitlist:
                rule.waitlist_notified.append(entry.booking.id)
            else:
                rule.confirmed.append(entry.booking.id)
        return sim

    def notify_waitlist(self, rule: DayRule, booking: Booking,
                        committed: list[Booking], as_of: int) -> bool:
        """对单个候补发确定通知；同组合冲突或整体不可行则不发。"""
        if rule.is_locked(booking.id) or as_of >= booking.start:
            return False
        if conflicts(self.dataset, booking,
                      [b for b in committed if b.is_waitlist]):
            return False
        if not feasible(self.dataset, [*committed, booking]):
            return False
        rule.waitlist_notified.append(booking.id)
        committed.append(booking)
        return True

    def apply_replacement_acceptance(self, rule: DayRule, booking: Booking,
                                     committed: list[Booking],
                                     timeline: Timeline, as_of: int) -> bool:
        """仅当替代被实际接受（且值班此刻已收到）才释放原承诺。"""
        event = booking.event
        if event.kind != EVENT_RESCHEDULE or not event.accepted_replacement:
            return False
        if event.received > as_of:
            return False
        if booking in committed:
            committed.remove(booking)
            return True
        return False

    def recompute_for_unissued(self, rule: DayRule, candidates: list[Booking],
                               as_of: int) -> list[Booking]:
        """重算只作用于尚未发出通知的预约，已确认名单锁定不动。"""
        locked = set(rule.confirmed) | set(rule.waitlist_notified)
        return [b for b in candidates
                if b.id not in locked and b.request_minute >= as_of]
