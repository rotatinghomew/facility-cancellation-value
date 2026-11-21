"""策略与仿真引擎在真实合成资料上的端到端规则测试。"""

import unittest

from overbooking.backtest import run_backtest, run_decision_day
from overbooking.engine import PENDING, SimulationEngine
from overbooking.guardrails import Guardrails, evaluate_guardrails
from overbooking.model import KIND_WAITLIST, load_dataset
from overbooking.policy import Policy
from overbooking.risk import RiskModel


class BacktestRuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset("samples")
        cls.result = run_backtest(cls.ds)

    def test_walkforward_uses_only_prior_days(self):
        # 每个决策日的风险模型只能看到更早的营业日
        first_day = self.ds.days[0]
        model = RiskModel(self.ds, set())
        self.assertEqual(model.estimate(self.ds.day_bookings(first_day)[0]).samples, 0)

    def test_no_policy_admits_waitlist_after_its_start(self):
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                for entry in sim.entries:
                    if entry.accepted and entry.booking.is_waitlist:
                        self.assertLess(entry.decided_at, entry.booking.start,
                                        f"{policy} 在开场后才确认候补")

    def test_waitlist_never_duplicates_same_combination_notification(self):
        from overbooking.resources import same_combination
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                accepted_wl = [e.booking for e in sim.entries
                               if e.accepted and e.booking.is_waitlist]
                for i, a in enumerate(accepted_wl):
                    for b in accepted_wl[i + 1:]:
                        self.assertFalse(
                            same_combination(self.ds, a, b),
                            "同一组合的两个候补不能都收到确定通知")

    def test_denied_at_door_only_for_arrivals_who_cannot_fit(self):
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                for entry in sim.entries:
                    if entry.denied_at_door:
                        self.assertTrue(entry.arrived)
                        self.assertNotEqual(entry.outcome, "未接纳")

    def test_closure_never_counted_as_resident_noshow(self):
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                for entry in sim.entries:
                    if entry.closure_caused:
                        self.assertFalse(entry.booking.event.is_resident_noshow)

    def test_bounded_overbooking_blocked_by_linked_scenario(self):
        # 限量超发在回测中确实挤掉了实际到场者（35 单），且至少一个联动
        # 情景在其承诺名单下突破底线
        bounded = self.result.evaluations[Policy.BOUNDED]
        self.assertGreater(
            sum(m.denied_at_door for m in bounded.cells.values()), 0)
        self.assertTrue(any(not s["passes"] for s in self.result.scenario_reports))
        # 因此限量超发不得出现在任何组合的适用清单
        for cell, policies in self.result.applicability.items():
            self.assertNotIn(Policy.BOUNDED, policies)

    def test_safe_policies_remain_applicable_everywhere(self):
        for cell, policies in self.result.applicability.items():
            self.assertIn(Policy.NONE, policies)
            self.assertIn(Policy.WAITLIST, policies)

    def test_sealed_history_has_no_pending(self):
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                self.assertTrue(sim.sealed)
                self.assertFalse(any(e.outcome == PENDING for e in sim.entries))

    def test_decision_day_replay_marks_late_events_pending(self):
        sim = run_decision_day(self.ds, self.result.applicability, Policy.NONE)
        cutoff = max(self.ds.decision["cutoffs"])
        for entry in sim.entries:
            if entry.booking.event.received > cutoff:
                self.assertEqual(entry.outcome, PENDING)
        # 含待观察时底线核对不得宣称通过
        if any(e.outcome == PENDING for e in sim.entries):
            limits = Guardrails.from_decision(self.ds.decision)
            self.assertFalse(evaluate_guardrails(sim, limits).passes)

    def test_no_fabricated_demand_beyond_recorded_waitlist(self):
        # 仿真条目不超过原始预约数；候补之外不凭空产生客流
        for policy in Policy:
            for sim in self.result.simulations[policy]:
                self.assertLessEqual(len(sim.entries),
                                     len(self.ds.day_bookings(sim.day)))


if __name__ == "__main__":
    unittest.main()
