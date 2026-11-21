"""资源组合与容量判定测试。"""

import unittest

from overbooking.model import Booking, Event, RoomSpec, Dataset
from overbooking.resources import (
    conflicts,
    feasible,
    same_combination,
    spare_seats,
)


def _booking(rid, resource, start, end, companion=False, kind="已确认",
             protected=False, coach="带队0"):
    event = Event(rid, "到场", start, start + 10, False, None)
    return Booking(rid, "2026-09-15", resource, start, end, coach,
                   companion, start - 100, kind, protected, event)


def _dataset():
    rooms = {
        "东桌": RoomSpec("东桌", 2, frozenset({"西桌"})),
        "西桌": RoomSpec("西桌", 2, frozenset({"东桌"})),
        "活动区": RoomSpec("活动区", 1, frozenset()),
    }
    return Dataset([], rooms, 1, {})


class ResourceModelTest(unittest.TestCase):
    def setUp(self):
        self.ds = _dataset()

    def test_shareable_tables_form_one_pool(self):
        self.assertEqual(self.ds.pool_of("东桌"), frozenset({"东桌", "西桌"}))
        self.assertEqual(self.ds.pool_capacity("东桌"), 4)

    def test_activity_zone_is_isolated(self):
        self.assertEqual(self.ds.pool_of("活动区"), frozenset({"活动区"}))
        self.assertEqual(self.ds.pool_capacity("活动区"), 1)

    def test_pool_capacity_enforced_over_merged_rooms(self):
        # 池容量 4：4 单同时可行，第 5 单越界
        rows = [_booking(f"t{i}", "东桌" if i % 2 else "西桌", 540, 585)
                for i in range(4)]
        self.assertTrue(feasible(self.ds, rows))
        self.assertFalse(feasible(self.ds, [*rows, _booking("t4", "东桌", 540, 585)]))

    def test_companion_seats_are_global_simultaneous(self):
        a = _booking("a", "活动区", 540, 630, companion=True)
        b = _booking("b", "活动区", 585, 675, companion=True)
        # 活动区容量本身就只有 1，叠加陪同工位上限 1 也拒绝
        self.assertFalse(feasible(self.ds, [a, b]))

    def test_two_hour_booking_holds_each_atomic_slot(self):
        long = _booking("long", "活动区", 540, 630)
        short = _booking("short", "活动区", 585, 630)
        self.assertFalse(feasible(self.ds, [long, short]))

    def test_same_combination_detects_cross_room_conflict(self):
        east = _booking("e", "东桌", 540, 585)
        west = _booking("w", "西桌", 540, 585)
        self.assertTrue(same_combination(self.ds, east, west))
        self.assertEqual(conflicts(self.ds, east, [west])[0].id, "w")

    def test_spare_seats_is_pool_only_not_cross_room_answer(self):
        east = _booking("e", "东桌", 540, 585)
        west = _booking("w", "西桌", 630, 675)  # 不重叠
        self.assertEqual(spare_seats(self.ds, [east], west), 4)
        overlapping = _booking("w2", "西桌", 540, 585)
        self.assertEqual(spare_seats(self.ds, [east], overlapping), 3)


if __name__ == "__main__":
    unittest.main()
