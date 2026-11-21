"""时间线与风险学习口径测试：可知时刻、停用剔除、改期剔除。"""

import unittest

from overbooking.model import (
    Booking,
    Dataset,
    Event,
    EVENT_CANCEL,
    EVENT_CLOSURE,
    EVENT_RESCHEDULE,
    EVENT_SHOW,
    KIND_CONFIRMED,
    KIND_WAITLIST,
    RoomSpec,
)
from overbooking.model import RoomSpec
from overbooking.risk import RiskModel
from overbooking.timeline import Timeline


def _evt(rid, kind, event_minute, received, accepted=False, replacement=None):
    return Event(rid, kind, event_minute, received, accepted, replacement)


def _booking(rid, kind=KIND_CONFIRMED, resource="东桌", start=540, end=585,
             event=None, coach="带队0", day="2026-09-01", companion=False,
             request_minute=400, protected=False):
    event = event or _evt(rid, EVENT_SHOW, start, start + 10)
    return Booking(rid, day, resource, start, end, coach, companion,
                   request_minute, kind, protected, event)


class TimelineTest(unittest.TestCase):
    def test_knowledge_respects_received_not_event_minute(self):
        # 业务 510 发生，值班 615 才知道：510-615 之间视为未知
        b = _booking("b", event=_evt("b", EVENT_CANCEL, 510, 615))
        tl = Timeline([b])
        early = tl.knowledge_at(550)
        self.assertNotIn("b", early.released)
        late = tl.knowledge_at(615)
        self.assertIn("b", late.released)
        self.assertTrue(late.is_free(b))

    def test_reschedule_holds_commitment_until_replacement_accepted(self):
        declined = _booking(
            "d", event=_evt("d", EVENT_RESCHEDULE, 555, 595, accepted=False,
                            replacement={"resource": "东桌", "start": 675, "end": 720}))
        accepted = _booking(
            "a", event=_evt("a", EVENT_RESCHEDULE, 555, 595, accepted=True,
                            replacement={"resource": "东桌", "start": 675, "end": 720}))
        tl = Timeline([declined, accepted])
        kn = tl.knowledge_at(600)
        self.assertFalse(kn.is_free(declined))   # 未接受替代，原承诺保留
        self.assertTrue(kn.is_free(accepted))    # 已接受，原承诺释放

    def test_closure_is_known_release_but_not_resident_noshow(self):
        b = _booking("c", event=_evt("c", EVENT_CLOSURE, 510, 550))
        kn = Timeline([b]).knowledge_at(600)
        self.assertTrue(kn.is_free(b))
        self.assertFalse(b.event.is_resident_noshow)
        self.assertTrue(b.event.is_closure)


class RiskLearningTest(unittest.TestCase):
    def _dataset(self, rows):
        rooms = {
            "东桌": RoomSpec("东桌", 2, frozenset({"西桌"})),
            "西桌": RoomSpec("西桌", 2, frozenset({"东桌"})),
            "活动区": RoomSpec("活动区", 1, frozenset()),
        }
        return Dataset(rows, rooms, 1, {})

    def test_closures_and_reschedules_excluded_from_learning(self):
        # 显式构造：4 到场、2 取消、1 停用、1 改期（同日不同 id）
        rows = []
        for i in range(4):
            rows.append(_booking(f"s{i}", event=_evt(f"s{i}", EVENT_SHOW, 540, 550)))
        for i in range(2):
            rows.append(_booking(f"k{i}", event=_evt(f"k{i}", EVENT_CANCEL, 510, 550)))
        rows.append(_booking("cl", event=_evt("cl", EVENT_CLOSURE, 510, 550)))
        rows.append(_booking("rs", event=_evt(
            "rs", EVENT_RESCHEDULE, 555, 595, accepted=True,
            replacement={"resource": "东桌", "start": 675, "end": 720})))
        ds = self._dataset(rows)
        model = RiskModel(ds, {"2026-09-01"})
        estimate = model.estimate(rows[0])
        # 合格样本只有 4 到场 + 2 取消 = 6；停用与改期被剔除
        self.assertEqual(estimate.samples, 6)
        self.assertEqual(estimate.noshows, 2)

    def test_waitlist_denied_not_in_risk_sample(self):
        rows = [
            _booking("w", kind=KIND_WAITLIST,
                     event=_evt("w", "未接纳", 540, 550)),
            _booking("s", event=_evt("s", EVENT_SHOW, 540, 550)),
        ]
        model = RiskModel(self._dataset(rows), {"2026-09-01"})
        self.assertEqual(len(model._global), 1)

    def test_only_prior_days_used_and_shrinkage(self):
        old = [_booking(f"o{i}", day="2026-08-01",
                        event=_evt(f"o{i}", EVENT_CANCEL, 510, 550)) for i in range(3)]
        future = [_booking("f", day="2026-09-10",
                           event=_evt("f", EVENT_CANCEL, 510, 550))]
        model = RiskModel(self._dataset([*old, *future]), {"2026-08-01"})
        self.assertEqual(model.estimate(old[0]).samples, 3)
        # 未来日的记录不进入学习样本：同格估计仍只来自 3 条历史
        self.assertEqual(model.estimate(future[0]).samples, 3)
        learned = next(iter(model._cell.values()))
        self.assertTrue(all(b.day == "2026-08-01" for b in learned))
        self.assertEqual(len(model._global), 3)


if __name__ == "__main__":
    unittest.main()
