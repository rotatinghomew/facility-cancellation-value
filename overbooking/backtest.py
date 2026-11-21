"""走查式回测：每个营业日只用此前已封账的资料做决策。

流程对应业务要求：

* 按接受下一单的时刻做回看，以随后实际签到、拒绝和核对后的空置封账；
* 风险模型在每个决策日重新拟合，输入只含 ``day < 决策日`` 的营业日；
* 决策日当天（decision.json 的 day）尚未发生的时段标待观察；
* 逐策略产出分时段指标、底线报告与适用性清单；
* 联动到场情景在回测结果上分别加压检查。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import DaySimulation, SimulationEngine
from .evaluate import (
    PolicyEvaluation,
    aggregate,
    applicable_policies,
    linked_scenario_check,
)
from .guardrails import GuardrailReport, Guardrails, evaluate_guardrails
from .model import Dataset
from .policy import POLICIES, Policy
from .risk import RiskModel


@dataclass
class BacktestResult:
    evaluations: dict[Policy, PolicyEvaluation]
    guardrail_reports: dict[Policy, list[GuardrailReport]]
    applicability: dict[tuple, list[Policy]]
    scenario_reports: list[dict]
    simulations: dict[Policy, list[DaySimulation]]
    risk_predictions: dict[Policy, dict[str, float]]
    limits: Guardrails
    decision_day: str
    history_days: list[str]


def run_backtest(dataset: Dataset, overbook_limit: int = 1,
                 warmup_days: int = 5) -> BacktestResult:
    """对全部历史营业日做走查回测；decision.day 当天的未结束时段待观察。"""
    limits = Guardrails.from_decision(dataset.decision)
    decision_day = dataset.decision["day"]
    all_days = dataset.days
    history_days = [d for d in all_days if d < decision_day]

    simulations: dict[Policy, list[DaySimulation]] = {p: [] for p in POLICIES}
    reports: dict[Policy, list[GuardrailReport]] = {p: [] for p in POLICIES}
    predictions: dict[Policy, dict[str, float]] = {p: {} for p in POLICIES}

    for day in history_days:
        prior_days = {d for d in history_days if d < day}
        # 暖机期内资料太少，仍给模型但收缩先验主导
        risk_model = RiskModel(dataset, prior_days)
        cutoff = None  # 历史营业日全部已封账
        for policy in POLICIES:
            engine = SimulationEngine(
                dataset, policy, risk_model, overbook_limit,
                limits.max_wait_minutes)
            sim = engine.run_day(day, observation_cutoff=cutoff)
            simulations[policy].append(sim)
            reports[policy].append(evaluate_guardrails(sim, limits))
            # 预测概率按组合记录：候补组合复用同组合已确认单的风险估计
            for entry in sim.entries:
                estimate = risk_model.estimate(entry.booking)
                predictions[policy][entry.booking.id] = estimate.noshow_prob

    evaluations = {
        policy: aggregate(dataset, simulations[policy], policy, predictions[policy])
        for policy in POLICIES
    }
    scenario_reports = linked_scenario_check(
        dataset, simulations[Policy.BOUNDED],
        dataset.decision["linked_scenarios"], limits)
    applicability = applicable_policies(
        evaluations, reports, limits, scenario_reports)

    return BacktestResult(
        evaluations=evaluations,
        guardrail_reports=reports,
        applicability=applicability,
        scenario_reports=scenario_reports,
        simulations=simulations,
        risk_predictions=predictions,
        limits=limits,
        decision_day=decision_day,
        history_days=history_days,
    )


def run_decision_day(dataset: Dataset, applicability: dict[tuple, list[Policy]],
                     policy: Policy, overbook_limit: int = 1,
                     day: str | None = None) -> DaySimulation:
    """按选定适用规则跑值班仿真。

    decision.day 在合成资料中是“规则确认日”（当天无预约记录），因此默认对
    最近一个已封账营业日做带观察截止的演练：截止时刻之后才可知的事件一律
    标待观察，不进入封账结论。
    """
    limits = Guardrails.from_decision(dataset.decision)
    target_day = day or _last_day_with_bookings(dataset)
    prior_days = {d for d in dataset.days if d < target_day}
    risk_model = RiskModel(dataset, prior_days)
    cutoff = max(dataset.decision["cutoffs"])
    engine = SimulationEngine(
        dataset, policy, risk_model, overbook_limit, limits.max_wait_minutes)
    return engine.run_day(target_day, observation_cutoff=cutoff)


def _last_day_with_bookings(dataset: Dataset) -> str:
    days = dataset.days
    if not days:
        raise ValueError("资料中没有营业日")
    return days[-1]
