"""原始资料装载、校验与资源组合模型。

资料语义（见 README）：

* 桌按 ``shareable`` 合并为可互换池；活动区不与桌合并。
* 活动区预约带陪同工位，陪同工位按同时占用计（全局上限见 assistance.json）。
* 同一预约可跨相连时段（start/end 为半开区间，分钟）。
* 空值表示没有观测，绝不当数值零处理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 请求与事件中的业务种类
KIND_WAITLIST = "候补"
KIND_CONFIRMED = "已确认"

EVENT_SHOW = "到场"
EVENT_CANCEL = "取消"
EVENT_CLOSURE = "场地停用"
EVENT_RESCHEDULE = "改期"
EVENT_DENIED = "未接纳"

# 居民侧爽约类事件（场地停用不是居民爽约，绝不进入风险学习）
RESIDENT_NOSHOW_EVENTS = frozenset({EVENT_CANCEL, EVENT_RESCHEDULE})
# 仍会占用承诺容量、直到收到替代接受才释放的事件
RELEASE_EVENTS = frozenset({EVENT_CANCEL, EVENT_CLOSURE, EVENT_RESCHEDULE})


@dataclass(frozen=True)
class Event:
    """一次预约的最终业务事件。"""

    request_id: str
    kind: str
    event_minute: int
    received: int
    accepted_replacement: bool
    replacement: Optional[dict]

    @property
    def is_resident_noshow(self) -> bool:
        return self.kind in RESIDENT_NOSHOW_EVENTS

    @property
    def is_closure(self) -> bool:
        return self.kind == EVENT_CLOSURE


@dataclass(frozen=True)
class Booking:
    """一条预约（已确认或候补）。"""

    id: str
    day: str
    resource: str
    start: int
    end: int
    coach: str
    companion: bool
    request_minute: int
    kind: str
    protected: bool
    event: Event

    @property
    def is_waitlist(self) -> bool:
        return self.kind == KIND_WAITLIST

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "Booking") -> bool:
        return not (self.end <= other.start or other.end <= self.start)


@dataclass(frozen=True)
class RoomSpec:
    name: str
    capacity: int
    shareable: frozenset[str]


@dataclass
class Dataset:
    bookings: list[Booking]
    rooms: dict[str, RoomSpec]
    companion_limit: int
    decision: dict
    by_id: dict[str, Booking] = field(init=False)

    def __post_init__(self):
        self.by_id = {b.id: b for b in self.bookings}

    @property
    def days(self) -> list[str]:
        return sorted({b.day for b in self.bookings})

    def day_bookings(self, day: str) -> list[Booking]:
        return [b for b in self.bookings if b.day == day]

    def pool_of(self, resource: str) -> frozenset[str]:
        """资源所属的可互换池（含自身）。"""
        spec = self.rooms[resource]
        return frozenset({resource, *spec.shareable})

    def pool_capacity(self, resource: str) -> int:
        """可互换池在任一原子时刻的总容量。"""
        return sum(self.rooms[name].capacity for name in self.pool_of(resource))


def load_dataset(folder: str | Path) -> Dataset:
    """从 samples 目录读取并交叉校验全部资料。"""
    folder = Path(folder)
    requests = json.loads((folder / "requests.json").read_text(encoding="utf-8"))
    raw_events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    raw_rooms = json.loads((folder / "rooms.json").read_text(encoding="utf-8"))
    assistance = json.loads((folder / "assistance.json").read_text(encoding="utf-8"))
    decision = json.loads((folder / "decision.json").read_text(encoding="utf-8"))

    events: dict[str, Event] = {}
    for row in raw_events:
        events[row["id"]] = Event(
            request_id=row["id"],
            kind=row["kind"],
            event_minute=row["event_minute"],
            received=row["received"],
            accepted_replacement=bool(row["accepted_replacement"]),
            replacement=row["replacement"],
        )

    request_ids = {row["id"] for row in requests}
    if set(events) != request_ids:
        raise ValueError("requests 与 events 必须一一对应")

    bookings: list[Booking] = []
    for row in requests:
        event = events[row["id"]]
        if event.event_minute > event.received:
            raise ValueError(f"{row['id']} 业务时刻晚于值班可知时刻")
        if not (row["start"] < row["end"]):
            raise ValueError(f"{row['id']} 预约区间非法")
        bookings.append(
            Booking(
                id=row["id"],
                day=row["day"],
                resource=row["resource"],
                start=row["start"],
                end=row["end"],
                coach=row["coach"],
                companion=bool(row["companion"]),
                request_minute=row["request_minute"],
                kind=row["kind"],
                protected=bool(row["protected"]),
                event=event,
            )
        )

    rooms = {
        name: RoomSpec(
            name=name,
            capacity=int(spec["capacity"]),
            shareable=frozenset(spec.get("shareable", [])),
        )
        for name, spec in raw_rooms.items()
    }
    # shareable 必须对称，才能按池统一调度
    for name, spec in rooms.items():
        for other in spec.shareable:
            if name not in rooms[other].shareable:
                raise ValueError(f"{name} 与 {other} 的 shareable 关系不对称")

    return Dataset(
        bookings=bookings,
        rooms=rooms,
        companion_limit=int(assistance["simultaneous"]),
        decision=decision,
    )
