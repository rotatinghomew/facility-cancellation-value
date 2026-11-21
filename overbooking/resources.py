"""组合资源的容量与可行性判定。

关键约束（业务规则，不是逐间空余名额的简单相加）：

1. 桌池内每个原子时刻的在占数不得超过池容量；跨相连时段的预约在其覆盖的
   每个原子时刻都占一个名额。
2. 陪同工位全局同时占用，任一时刻在占数不得超过 assistance 上限。
3. 两个候补若需要同一组合（同池且时间重叠、陪同需求冲突），不能同时确认。
"""

from __future__ import annotations

from .model import Booking, Dataset


def atomic_minutes(bookings: list[Booking]) -> list[int]:
    """所有相关区间端点切出的原子时刻网格。"""
    points = sorted({m for b in bookings for m in (b.start, b.end)})
    return points[:-1]


def pool_load_at(dataset: Dataset, bookings: list[Booking], minute: int) -> dict[str, int]:
    """各可互换池在给定原子时刻的在占数。"""
    load: dict[str, int] = {}
    for b in bookings:
        if b.start <= minute < b.end:
            pool = next(iter(dataset.pool_of(b.resource)))
            # 用池内排序首个资源名作为池的稳定标识
            pool = min(dataset.pool_of(b.resource))
            load[pool] = load.get(pool, 0) + 1
    return load


def companion_load_at(bookings: list[Booking], minute: int) -> int:
    return sum(1 for b in bookings if b.companion and b.start <= minute < b.end)


def feasible(dataset: Dataset, bookings: list[Booking]) -> bool:
    """判断一组同时承诺的预约在整个时间轴上是否守得住容量。"""
    if not bookings:
        return True
    for minute in atomic_minutes(bookings):
        for pool_name, load in pool_load_at(dataset, bookings, minute).items():
            representative = next(
                name for name in dataset.rooms if min(dataset.pool_of(name)) == pool_name
            )
            if load > dataset.pool_capacity(representative):
                return False
        if companion_load_at(bookings, minute) > dataset.companion_limit:
            return False
    return True


def same_combination(dataset: Dataset, a: Booking, b: Booking) -> bool:
    """两单是否需要同一组合（同可互换池且时间重叠；陪同工位另计冲突）。"""
    if dataset.pool_of(a.resource) != dataset.pool_of(b.resource):
        return False
    if not a.overlaps(b):
        return False
    if a.companion or b.companion:
        # 陪同工位全局同时占用，陪同需求相互冲突
        return True
    return True


def conflicts(dataset: Dataset, booking: Booking, committed: list[Booking]) -> list[Booking]:
    """与已承诺名单中构成同组合冲突的预约。"""
    return [other for other in committed if same_combination(dataset, booking, other)]


def spare_seats(dataset: Dataset, committed: list[Booking], booking: Booking) -> int:
    """在已承诺名单下，booking 所属池在其覆盖时段内的最小空余名额。

    注意：该函数只回答单一池的空余名额，跨房间/陪同工位的联动冲突必须用
    ``feasible`` 整体判定，不能用逐间空余名额拼出策略。
    """
    pool = dataset.pool_of(booking.resource)
    capacity = dataset.pool_capacity(booking.resource)
    spare = capacity
    for minute in range(booking.start, booking.end):
        load = sum(
            1
            for other in committed
            if other is not booking
            and dataset.pool_of(other.resource) == pool
            and other.start <= minute < other.end
        )
        spare = min(spare, capacity - load)
    return spare
