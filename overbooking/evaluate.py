"""分时段策略比较、适用性判定与联动到场情景。

评估口径：

* 指标按“组合 × 时段”分格汇总，不给楼里每个房间一个固定比例；
* 预测准不准看爽约预测与实际封账的偏差；释放空间能否兑现看候补接纳后
  是否真的在开场前拿到容量；
* 待观察时段不参与指标；名单之外的需求只按确有的候补记录计数；
* 联动到场情景（decision.linked_scenarios）按带队人组合整体加压检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import DaySimulation, PENDING
from .guardrails import GuardrailReport, Guardrails
from .model import Booking, Dataset
from .policy import Policy
from .risk import cell_key

@dataclass
class CellMetrics:
    cell: tuple
    days_observed: int = 0
    filled_waitlist: int = 0          # 候补获接纳且最终有人使用
    promised_waitlist: int = 0        # 候补获接纳总数
    denied_at_door: int = 0
    protected_denied: int = 0
    empty_sealed: int = 0             # 核对后空置（含场地原因，另列）
    closure_empty: int = 0
    resident_noshow: int = 0
    predicted_noshow_risk: float = 0.0
    realized_noshow_rate: float = 0.0
    realized_releases: int = 0
    guardrail_breaches: int = 0
    wait_minutes_sum: int = 0
    wait_minutes_max: int = 0
    promised_seats: int = 0              # 已给承诺的名额（不含未获接纳候补）

    @property
    def fill_realization(self) -> float:
        """释放空间的办法能否实际兑现：承诺候补的兑现率。"""
        if self.promised_waitlist == 0:
            return 1.0
        return self.filled_waitlist / self.promised_waitlist

    @property
    def prediction_bias(self) -> float:
        """正=高估爽约（偏激进超发），负=低估爽约。"""
        return self.predicted_noshow_risk - self.realized_noshow_rate

    def summary(self) -> str:
        return (
            f"观察 {self.days_observed} 日 | 候补承诺 {self.promised_waitlist}、"
            f"兑现 {self.filled_waitlist}（{self.fill_realization:.0%}）| "
            f"无法入场 {self.denied_at_door}（高损失 {self.protected_denied}）| "
            f"核对空置 {self.empty_sealed}（场地 {self.closure_empty}）| "
            f"预测爽约 {self.predicted_noshow_risk:.1%} vs 实际 "
            f"{self.realized_noshow_rate:.1%} | 底线违规 {self.guardrail_breaches}"
        )


@dataclass
class PolicyEvaluation:
    policy: Policy
    dataset: Dataset
    cells: dict[tuple, CellMetrics] = field(default_factory=dict)
    days: int = 0
    pending_days: int = 0

    def cell(self, booking: Booking) -> CellMetrics:
        key = cell_key(self.dataset, booking)
        if key not in self.cells:
            self.cells[key] = CellMetrics(key)
        return self.cells[key]


def aggregate(dataset: Dataset, simulations: list[DaySimulation], policy: Policy,
              risk_predictions: dict[str, float] | None = None) -> PolicyEvaluation:
    """把多日仿真汇总为分时段评估。risk_predictions: request_id -> 决策时预测概率。"""
    result = PolicyEvaluation(policy=policy, dataset=dataset)
    risk_predictions = risk_predictions or {}
    seen_days: set[str] = set()
    for sim in simulations:
        seen_days.add(sim.day)
        if any(e.outcome == PENDING for e in sim.entries):
            result.pending_days += 1
        for entry in sim.entries:
            if entry.outcome == PENDING:
                continue
            m = result.cell(entry.booking)
            m.days_observed += 1
            if entry.accepted:
                m.promised_seats += 1
            if entry.accepted and entry.booking.is_waitlist:
                m.promised_waitlist += 1
                wait = entry.booking.start - entry.decided_at
                m.wait_minutes_sum += max(0, wait)
                m.wait_minutes_max = max(m.wait_minutes_max, max(0, wait))
                if entry.arrived:
                    m.filled_waitlist += 1
            if entry.denied_at_door:
                m.denied_at_door += 1
                if entry.booking.protected:
                    m.protected_denied += 1
            if entry.empty_sealed:
                m.empty_sealed += 1
                if entry.closure_caused:
                    m.closure_empty += 1
            if entry.booking.event.is_resident_noshow:
                m.resident_noshow += 1
                m.realized_releases += 1
    result.days = len(seen_days)
    # 预测概率按格平均（仅对有预测记录的样本）
    pred_by_cell: dict[tuple, list[float]] = {}
    for sim in simulations:
        for entry in sim.entries:
            if entry.outcome == PENDING:
                continue
            pred = risk_predictions.get(entry.booking.id)
            if pred is not None:
                pred_by_cell.setdefault(
                    cell_key(dataset, entry.booking), []).append(pred)
    for key, preds in pred_by_cell.items():
        result.cells[key].predicted_noshow_risk = sum(preds) / len(preds)
    for m in result.cells.values():
        # 实际爽约率 = 居民侧爽约（取消+改期）/ 已给承诺的名额；
        # 未获接纳的候补本就没有承诺，不进分母。场地停用另列、不计居民爽约。
        m.realized_noshow_rate = (
            m.resident_noshow / m.promised_seats if m.promised_seats else 0.0)
    return result


def applicable_policies(evaluations: dict[Policy, PolicyEvaluation],
                        guardrail_reports: dict[Policy, list[GuardrailReport]],
                        limits: Guardrails,
                        scenario_reports: list[dict] | None = None
                        ) -> dict[tuple, list[Policy]]:
    """逐组合判定适用策略：守底线、兑现率达标且预测偏差可控。

    无法守住底线的策略不会出现在适用清单里（只能停留在比较页面）。
    限量超发还必须通过全部联动到场情景：联动下的无法入场同样计入底线。
    """
    scenario_reports = scenario_reports or []
    applicable: dict[tuple, list[Policy]] = {}
    all_cells = set()
    for ev in evaluations.values():
        all_cells.update(ev.cells)
    breach_days = {
        p: sum(1 for r in reports if not r.passes)
        for p, reports in guardrail_reports.items()
    }
    # 限量超发的联动情景否决：任一联动情景突破底线即整体不可确认
    bounded_blocked_by_scenario = any(
        not s["passes"] for s in scenario_reports)
    for cell in sorted(all_cells):
        choices = []
        for policy, ev in evaluations.items():
            m = ev.cells.get(cell)
            if m is None or m.days_observed == 0:
                continue
            if breach_days.get(policy, 0) > 0:
                continue  # 守不住底线，不进适用清单
            if m.fill_realization < 0.8:
                continue  # 释放空间兑现不了
            # 超发的意义是吸收爽约、不是挤掉实际到场者：适用策略不允许
            # 在回测中出现任何无法入场（max_denied 是底线容差，不是目标）
            if m.denied_at_door > 0:
                continue
            if m.protected_denied > limits.protected_max_denied:
                continue
            if policy is Policy.BOUNDED and bounded_blocked_by_scenario:
                continue  # 联动到场情景下守不住底线
            choices.append(policy)
        # 适用性排序：优先选代价小但满足利用的策略；不超发永远是安全兜底
        order = [Policy.NONE, Policy.WAITLIST, Policy.BOUNDED]
        applicable[cell] = [p for p in order if p in choices]
    return applicable


def linked_scenario_check(dataset: Dataset, simulations: list[DaySimulation],
                          scenarios: list[dict], limits: Guardrails) -> list[dict]:
    """对合成资料给出的联动到场情景分别加压检查。

    每个 scenario 的 groups_show 表示这些带队人“全部到场”的联动组合；
    检查在该情景下承诺名单是否超容量、底线是否被突破。
    """
    reports = []
    for scenario in scenarios:
        groups = set(scenario["groups_show"])
        denied = protected_denied = 0
        baseline = 0      # 不施加情景时本就无法入场的人数
        pressured = 0     # 情景额外“变到场”的人数
        for sim in simulations:
            present, extra = [], []
            for e in sim.entries:
                if not e.accepted or e.outcome == PENDING:
                    continue
                if e.arrived:
                    present.append(e.booking)          # 实际到场的已接预约
                elif (e.booking.coach in groups
                      and e.booking.event.kind != "场地停用"):
                    # 情景加压：这些带队人原本未到场（取消/改期）的已接单变到场
                    extra.append(e.booking)
            pressured += len(extra)

            def _replay(rows):
                rows = sorted(rows, key=lambda b: (b.protected, b.start),
                              reverse=True)
                kept: list[Booking] = []
                lost = 0
                lost_protected = 0
                for booking in rows:
                    from .resources import feasible
                    if feasible(dataset, [*kept, booking]):
                        kept.append(booking)
                    else:
                        lost += 1
                        if booking.protected:
                            lost_protected += 1
                return lost, lost_protected

            base_lost, _ = _replay(list(present))
            scenario_lost, scenario_lost_protected = _replay(present + extra)
            baseline += base_lost
            denied += scenario_lost
            protected_denied += scenario_lost_protected
        ok = denied <= limits.max_denied and protected_denied <= limits.protected_max_denied
        reports.append({
            "groups_show": sorted(groups),
            "pressured_to_show": pressured,
            "baseline_denied": baseline,
            "denied_under_scenario": denied,
            "protected_denied": protected_denied,
            "passes": ok,
        })
    return reports
