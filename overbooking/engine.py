"""按接受下一单的时刻做回看的逐单仿真引擎。

时间推进规则：

1. 预约按 ``request_minute`` 依次到达，决策时刻只使用当时 ``received`` 已到
   的事件信息（见 :class:`timeline.Timeline`）。
2. 候补单到达时若暂不能接，保留在候补队列；后续每收到一条释放类消息，就
   重新尝试一次按单接纳。
3. 改期消息只在替代被实际接受后释放原承诺；收到改期但未接受期间容量保留。
4. 封账：营业日全部事件可知后，以实际到场、拒绝和核对后的空置结算。
5. 未结束的时段（决策日当天尚未到 ``received`` 的部分）标 ``待观察``，
   不进入准确率与收益统计；预约名单之外的需求只按确有的候补记录算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    Booking,
    Dataset,
    EVENT_CANCEL,
    EVENT_CLOSURE,
    EVENT_DENIED,
    EVENT_RESCHEDULE,
    EVENT_SHOW,
    KIND_WAITLIST,
)
from .policy import Decision, Policy, PolicyContext, decide
from .resources import conflicts, feasible
from .risk import RiskModel
from .timeline import Timeline

PENDING = "待观察"


@dataclass
class LedgerEntry:
    booking: Booking
    policy: Policy
    accepted: bool
    overbooked: bool
    reason: str
    decided_at: int
    # 封账结果
    outcome: str = PENDING
    arrived: bool = False
    denied_at_door: bool = False
    empty_sealed: bool = False
    closure_caused: bool = False


@dataclass
class DaySimulation:
    day: str
    policy: Policy
    dataset: Dataset | None = None
    entries: list[LedgerEntry] = field(default_factory=list)
    committed: list[Booking] = field(default_factory=list)
    waitlist_queue: list[Booking] = field(default_factory=list)
    # 截至当前时刻已知释放原承诺的预约（取消/停用/已接受改期）
    known_free: list[Booking] = field(default_factory=list)
    timeline: Timeline | None = None
    sealed: bool = False
    observation_cutoff: int | None = None  # 非空时：该时刻之后未封账的时段待观察

    def entry_of(self, booking_id: str) -> LedgerEntry | None:
        for e in self.entries:
            if e.booking.id == booking_id:
                return e
        return None


class SimulationEngine:
    """对单个营业日回放一种策略。"""

    def __init__(self, dataset: Dataset, policy: Policy, risk_model: RiskModel | None,
                 overbook_limit: int, max_wait_minutes: int):
        self.dataset = dataset
        self.policy = policy
        self.risk_model = risk_model
        self.overbook_limit = overbook_limit
        self.max_wait_minutes = max_wait_minutes

    def _overbook_count(self, sim: DaySimulation, booking: Booking) -> int:
        """承诺名单中与 booking 同组合且时间重叠的超发单数。"""
        count = 0
        for entry in sim.entries:
            if not (entry.accepted and entry.overbooked):
                continue
            if entry.booking.overlaps(booking) and conflicts(
                self.dataset, entry.booking, [booking]
            ):
                count += 1
        return count

    def _try_accept(self, sim: DaySimulation, booking: Booking, as_of: int) -> Decision:
        risk = self.risk_model.estimate(booking) if self.risk_model else None
        ctx = PolicyContext(
            as_of=as_of,
            committed=list(sim.committed),
            risk=risk,
            overbook_limit=self.overbook_limit,
            max_wait_minutes=self.max_wait_minutes,
            current_overbook=self._overbook_count(sim, booking),
            known_free=list(sim.known_free),
        )
        return decide(self.dataset, self.policy, booking, ctx)

    def _drain_waitlist(self, sim: DaySimulation, as_of: int) -> None:
        """收到新消息后重新尝试候补队列（按单放候补）。

        候补只在其开场时刻之前可被接纳；开场后仍未确认的候补继续留在队列，
        封账时按历史结果记“未获接纳”。
        """
        remaining = []
        for booking in sim.waitlist_queue:
            if as_of >= booking.start:
                remaining.append(booking)
                continue
            decision = self._try_accept(sim, booking, as_of)
            if decision.accept:
                sim.committed.append(booking)
                self._consume_free_slot(sim, booking)
                sim.entries.append(LedgerEntry(
                    booking, self.policy, True, decision.overbooked,
                    decision.reason, as_of))
            else:
                remaining.append(booking)
        sim.waitlist_queue = remaining

    def _consume_free_slot(self, sim: DaySimulation, booking: Booking) -> None:
        """候补落地后，把它占用的同组合释放名额从可用名单移除。"""
        from .resources import same_combination
        for free in list(sim.known_free):
            if same_combination(self.dataset, free, booking):
                sim.known_free.remove(free)
                return

    def run_day(self, day: str, observation_cutoff: int | None = None) -> DaySimulation:
        bookings = sorted(self.dataset.day_bookings(day), key=lambda b: b.request_minute)
        timeline = Timeline(bookings)
        sim = DaySimulation(day=day, policy=self.policy, dataset=self.dataset,
                            timeline=timeline, observation_cutoff=observation_cutoff)

        # 统一的决策时刻流：预约到达时刻与事件可知时刻
        decision_points = sorted({
            *[b.request_minute for b in bookings],
            *timeline.received_points,
        })
        arrivals = list(bookings)
        arrival_idx = 0
        last_point = -1

        for point in decision_points:
            # 1) 处理 (last_point, point] 内可知的新事件
            for b in timeline.events_received_between(last_point, point):
                self._apply_event(sim, b, point)
            # 2) 处理 request_minute == point 的新预约
            while arrival_idx < len(arrivals) and arrivals[arrival_idx].request_minute <= point:
                booking = arrivals[arrival_idx]
                arrival_idx += 1
                if sim.entry_of(booking.id) is not None:
                    continue
                decision = self._try_accept(sim, booking, point)
                if decision.accept:
                    sim.committed.append(booking)
                    if booking.kind == KIND_WAITLIST:
                        self._consume_free_slot(sim, booking)
                    sim.entries.append(LedgerEntry(
                        booking, self.policy, True, decision.overbooked,
                        decision.reason, point))
                elif booking.kind == KIND_WAITLIST:
                    sim.waitlist_queue.append(booking)
                else:
                    sim.entries.append(LedgerEntry(
                        booking, self.policy, False, False,
                        decision.reason, point))
            # 3) 消息到达后按单尝试候补
            self._drain_waitlist(sim, point)
            last_point = point

        self._seal(sim)
        return sim

    def _apply_event(self, sim: DaySimulation, booking: Booking, as_of: int) -> None:
        event = booking.event
        entry = sim.entry_of(booking.id)
        # 候补自身的取消/停用不是可用于填补的释放名额
        is_fillable_release = booking.kind != KIND_WAITLIST
        if event.kind == EVENT_RESCHEDULE:
            # 仅实际接受替代后才释放原承诺；未接受则原承诺保留，也不进释放名单
            if event.accepted_replacement:
                if entry and entry.accepted and booking in sim.committed:
                    sim.committed.remove(booking)
                if is_fillable_release:
                    sim.known_free.append(booking)
        elif event.kind in (EVENT_CANCEL, EVENT_CLOSURE):
            if entry and entry.accepted and booking in sim.committed:
                sim.committed.remove(booking)
            if is_fillable_release:
                sim.known_free.append(booking)
        # 候补的历史“未接纳”是真实世界的封账结果，不是仿真中必须重演的动作：
        # 候补在策略仿真里继续留在队列，直到开场时刻仍未获接纳，才在 _seal 中
        # 按历史结果落“候补未获接纳”。这里不提前把候补移出队列。

    def _seal(self, sim: DaySimulation) -> None:
        """按实际到场、拒绝与核对后的空置封账；未结束时段标待观察。"""
        cutoff = sim.observation_cutoff
        # 队列里始终未获接纳的候补：落“候补未获接纳”（只按确有候补记录计数）
        for booking in list(sim.waitlist_queue):
            if sim.entry_of(booking.id) is None:
                sim.entries.append(LedgerEntry(
                    booking, self.policy, False, False,
                    "候补未获接纳", booking.start))
        sim.waitlist_queue = []

        for entry in sim.entries:
            event = entry.booking.event
            if cutoff is not None and event.received > cutoff:
                entry.outcome = PENDING
                continue
            if not entry.accepted and entry.booking.kind == KIND_WAITLIST:
                # 候补未获接纳：沿用历史“未接纳”结果，不虚构到场客流
                entry.outcome = EVENT_DENIED
                entry.arrived = False
                entry.denied_at_door = False
                continue
            entry.outcome = event.kind
            entry.arrived = event.kind == EVENT_SHOW
            entry.closure_caused = event.kind == EVENT_CLOSURE
            if event.kind == EVENT_DENIED:
                # 候补在策略下获接纳，但历史真实结果是未接纳：反事实下视为
                # 该候补填补了释放名额、无人到场（不凭空加客流）
                entry.denied_at_door = False
                if entry.booking.kind == KIND_WAITLIST:
                    entry.arrived = False
            # 核对后空置：被接受的承诺名额最终无人到场
            if entry.accepted and event.kind in (EVENT_CANCEL, EVENT_CLOSURE,
                                                 EVENT_RESCHEDULE):
                entry.empty_sealed = True
        sim.sealed = cutoff is None
        if sim.sealed:
            self._mark_denied_at_door(sim)

    def _mark_denied_at_door(self, sim: DaySimulation) -> None:
        """按实际到场名单整体重放容量，标出被挤在门外的到场者。

        不能按开始时刻分组：跨相连时段的预约（如两小时活动区）与后一档单
        在重叠原子时刻冲突，分组会漏判。这里把所有实际到场的已接预约统一
        做整体可行性重放。近开场、无同等替代空间的高损失（protected）预约
        优先保留；其余按决策时刻越晚越先被挤出（后确认者承担超发代价）。
        """
        present = [e for e in sim.entries if e.accepted and e.arrived]
        # 排序键：先保 protected，再保决策更早者；被挤顺序从排序末尾开始
        present.sort(key=lambda e: (e.booking.protected, -e.decided_at),
                     reverse=True)
        kept: list[Booking] = []
        for entry in present:
            if feasible(self.dataset, [*kept, entry.booking]):
                kept.append(entry.booking)
            else:
                entry.denied_at_door = True
