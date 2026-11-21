"""按值班可知时刻（received）组织的事件时间线。

回看的基本原则：在时刻 ``t`` 只能使用 ``received <= t`` 的信息。
``event_minute`` 是业务实际发生时刻，二者之差就是通知延迟；签到延迟与取消
是不同事件，场地临时不可用也只在收到时进入值班视野。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Booking,
    EVENT_CLOSURE,
    EVENT_DENIED,
    EVENT_RESCHEDULE,
    EVENT_SHOW,
    KIND_WAITLIST,
)


@dataclass(frozen=True)
class Knowledge:
    """某一时刻值班员掌握的预约处置状态快照。"""

    as_of: int
    # 已收到释放类事件（取消/停用/改期）的预约
    released: frozenset[str]
    # 已收到替代接受、原承诺已释放的预约
    replaced_out: frozenset[str]
    # 已收到到场的预约
    shown: frozenset[str]
    # 已收到未接纳的候补
    denied_waitlist: frozenset[str]
    # 已收到改期消息（无论是否接受）的预约：不再靠签到学习其当时风险
    reschedule_known: frozenset[str]
    # 场地原因停用：不能当居民爽约学习
    closures_known: frozenset[str]

    def is_free(self, booking: Booking) -> bool:
        """该预约的原承诺此刻是否已知释放。

        * 取消、场地停用：收到消息即释放；
        * 改期：仅在参与方实际接受替代时段后才释放原承诺，收到改期消息但
          尚未接受（含拒绝）时原承诺保留；
        * 候补被拒：候补本就不占承诺容量，视为不占座。
        """
        rid = booking.id
        if rid in self.replaced_out or rid in self.closures_known:
            return True
        if rid in self.reschedule_known:
            return False
        if rid in self.released:
            return True
        return rid in self.denied_waitlist and booking.is_waitlist


class Timeline:
    """把事件按 received 展开为逐时刻可知状态。"""

    def __init__(self, bookings: list[Booking]):
        self.bookings = bookings
        self._by_received: dict[int, list[Booking]] = {}
        for b in bookings:
            self._by_received.setdefault(b.event.received, []).append(b)
        self.received_points = sorted(self._by_received)

    def knowledge_at(self, as_of: int) -> Knowledge:
        released, replaced_out, shown, denied, resched, closure = set(), set(), set(), set(), set(), set()
        for b in self.bookings:
            e = b.event
            if e.received > as_of:
                continue
            if e.kind == EVENT_SHOW:
                shown.add(b.id)
            elif e.kind == EVENT_DENIED:
                if b.kind == KIND_WAITLIST:
                    denied.add(b.id)
            else:
                # 取消 / 场地停用 / 改期 都是释放类消息
                released.add(b.id)
                if e.kind == EVENT_RESCHEDULE:
                    resched.add(b.id)
                    if e.accepted_replacement:
                        replaced_out.add(b.id)
                elif e.kind == EVENT_CLOSURE:
                    closure.add(b.id)
        return Knowledge(as_of, frozenset(released), frozenset(replaced_out),
                         frozenset(shown), frozenset(denied),
                         frozenset(resched), frozenset(closure))

    def events_received_between(self, start_exclusive: int, end_inclusive: int) -> list[Booking]:
        out = []
        for point, rows in self._by_received.items():
            if start_exclusive < point <= end_inclusive:
                out.extend(rows)
        return out
