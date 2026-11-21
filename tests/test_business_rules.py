"""端到端业务规则的补充测试：替代释放、待观察与跨房间冲突。"""

import unittest

from overbooking.backtest import run_backtest, run_decision_day
from overbooking.engine import PENDING
from overbooking.guardrails import Guardrails, evaluate_guardrails
from overbooking.model import (
    Booking,
    Dataset,
    Event,
    RoomSpec,
    KIND_CONFIRMED,
    KIND_WAITLIST,
    load_dataset,
)
from overbooking.policy import Policy, PolicyContext, decide
from overbooking.resources import feasible


def _rooms():
    return {
        "东桌": RoomSpec("东桌", 2, frozenset({"西桌"})),
        "西桌": RoomSpec("西桌", 2, frozenset({"东桌"})),
        "活动区": RoomSpec("活动区", 1, frozenset()),
    }


def _booking(rid, resource, start, end, kind=KIND_CONFIRMED, companion=False,
             protected=False, event_kind="到场", received=None, coach="带队0"):
    received = received if received is not None else start + 10
    event = Event(rid, event_kind, start, received, False, None)
    return Booking(rid, "2026-09-14", resource, start, end, coach, companion,
                   start - 120, kind, protected, event)


class WaitlistGuardTest(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset([], _rooms(), 1, {})

    def test_waitlist_needs_real_release_not_bare_capacity(self):
        # 候补不能因为“物理容量内有空位”就直接占座
        wl = _booking("wl", "东桌", 540, 585, kind=KIND_WAITLIST)
        ctx = PolicyContext(as_of=420, committed=[], risk=None,
                            overbook_limit=1, max_wait_minutes=25,
                            current_overbook=0, known_free=[])
        decision = decide(self.ds, Policy.NONE, wl, ctx)
        self.assertFalse(decision.accept)

    def test_duplicate_waitlist_same_combination_blocked(self):
        free = _booking("free", "东桌", 540, 585, event_kind="取消", received=500)
        wl1 = _booking("wl1", "东桌", 540, 585, kind=KIND_WAITLIST)
        wl2 = _booking("wl2", "西桌", 540, 585, kind=KIND_WAITLIST)
        # 第一个候补占住释放名额
        first = decide(self.ds, Policy.NONE, wl1, PolicyContext(
            as_of=500, committed=[], risk=None, overbook_limit=1,
            max_wait_minutes=25, current_overbook=0, known_free=[free]))
        self.assertTrue(first.accept)
        # 第二个同组合候补不能再收到通知（释放名额已被 wl1 占用）
        second = decide(self.ds, Policy.NONE, wl2, PolicyContext(
            as_of=501, committed=[wl1], risk=None, overbook_limit=1,
            max_wait_minutes=25, current_overbook=0, known_free=[]))
        self.assertFalse(second.accept)

    def test_cross_room_overlap_not_solved_by_per_room_spare(self):
        # 活动区容量 1：两个跨时段活动区单整体不可行，即使各自“看自己时段”像有空
        a = _booking("a", "活动区", 540, 630, companion=True, protected=True)
        b = _booking("b", "活动区", 585, 675, companion=True)
        self.assertFalse(feasible(self.ds, [a, b]))


class PendingObservationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset("samples")
        cls.result = run_backtest(cls.ds)

    def test_pending_entries_excluded_from_sealed_metrics(self):
        # 用一个很早的观察截止，使多数事件落入待观察
        sim = run_decision_day(self.ds, self.result.applicability,
                               Policy.NONE, day=self.ds.days[-1])
        # 显式把截止改到最早 cutoff 之前重跑，验证待观察不被封账
        from overbooking.engine import SimulationEngine
        from overbooking.risk import RiskModel
        prior = {d for d in self.ds.days if d < self.ds.days[-1]}
        engine = SimulationEngine(self.ds, Policy.NONE, RiskModel(self.ds, prior),
                                  1, self.result.limits.max_wait_minutes)
        early = engine.run_day(self.ds.days[-1], observation_cutoff=400)
        pending = [e for e in early.entries if e.outcome == PENDING]
        self.assertTrue(pending)
        limits = Guardrails.from_decision(self.ds.decision)
        self.assertFalse(evaluate_guardrails(early, limits).passes)
        # 待观察条目不产生无法入场/空置结论
        self.assertFalse(any(e.denied_at_door or e.empty_sealed for e in pending))


if __name__ == "__main__":
    unittest.main()
