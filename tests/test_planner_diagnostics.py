"""值班规则确认与当晚归因测试。"""

import unittest

from overbooking.backtest import run_backtest
from overbooking.diagnostics import (
    CAUSE_COMBINATION_BUSY,
    CAUSE_LATE_NOTICE,
    CAUSE_PREDICTION,
    attribute_day,
)
from overbooking.model import load_dataset
from overbooking.planner import DayPlanner
from overbooking.policy import Policy
from overbooking.risk import RiskModel


class PlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset("samples")
        cls.result = run_backtest(cls.ds)
        cls.limits = cls.result.limits

    def test_cannot_confirm_inapplicable_policy(self):
        planner = DayPlanner(self.ds, self.result.applicability, None, 1,
                             self.limits.max_wait_minutes)
        day = self.ds.days[-1]
        with self.assertRaises(ValueError):
            planner.confirm_rule(day, Policy.BOUNDED, "测试越权确认")

    def test_can_confirm_applicable_safe_policy(self):
        planner = DayPlanner(self.ds, self.result.applicability, None, 1,
                             self.limits.max_wait_minutes)
        day = self.ds.days[-1]
        rule = planner.confirm_rule(day, Policy.NONE, "适用清单内确认")
        self.assertEqual(rule.policy, Policy.NONE)

    def test_recompute_only_affects_unissued_bookings(self):
        planner = DayPlanner(self.ds, self.result.applicability, None, 1,
                             self.limits.max_wait_minutes)
        day = self.ds.days[-1]
        rule = planner.confirm_rule(day, Policy.NONE, "适用清单内确认")
        rule.confirmed.append("already-sent")
        candidates = self.ds.day_bookings(day)
        unissued = planner.recompute_for_unissued(rule, candidates, 0)
        self.assertNotIn("already-sent", {b.id for b in unissued})
        self.assertEqual(len(unissued), len(candidates))


class DiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset("samples")
        cls.result = run_backtest(cls.ds)

    def test_attribution_covers_three_required_causes_or_none(self):
        allowed = {CAUSE_PREDICTION, CAUSE_LATE_NOTICE, CAUSE_COMBINATION_BUSY,
                   "场地临时不可用", "无滞留"}
        for sim in self.result.simulations[Policy.BOUNDED][:10]:
            prior = {d for d in self.result.history_days if d < sim.day}
            risk = RiskModel(self.ds, prior)
            risk_at = {e.booking.id: risk.estimate(e.booking) for e in sim.entries}
            report = attribute_day(sim, risk_at, self.result.limits.max_wait_minutes)
            for item in report.items:
                self.assertIn(item.cause, allowed)

    def test_closure_attributed_separately_from_resident_noshow(self):
        for sim in self.result.simulations[Policy.NONE]:
            prior = {d for d in self.result.history_days if d < sim.day}
            risk = RiskModel(self.ds, prior)
            risk_at = {e.booking.id: risk.estimate(e.booking) for e in sim.entries}
            report = attribute_day(sim, risk_at, self.result.limits.max_wait_minutes)
            for item in report.items:
                if item.cause == "场地临时不可用":
                    booking = self.ds.by_id[item.booking_id]
                    self.assertTrue(booking.event.is_closure)


if __name__ == "__main__":
    unittest.main()
