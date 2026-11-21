"""当晚补报签到后的容量滞留归因。

负责人需要分清容量滞留（被占承诺最终空置 / 有人却进不了场）究竟来自：

1. ``预测偏差``     —— 风险模型预测会释放但实际到场，或反向空置；
2. ``通知太晚``     —— 候补确认距开场不足、释放消息到达时已来不及补救；
3. ``组合被占用``   —— 需要的组合资源已被另一单占用（跨房间/陪同工位联动冲突），
                       不是逐间空余名额能解决的；
4. ``场地临时不可用``—— 单独标注，不计居民爽约。

归因只使用封账后的事实；待观察时段不做结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import DaySimulation, LedgerEntry, PENDING
from .model import EVENT_RESCHEDULE
from .resources import same_combination
from .risk import RiskEstimate, cell_key

CAUSE_PREDICTION = "预测偏差"
CAUSE_LATE_NOTICE = "候补通知太晚"
CAUSE_COMBINATION_BUSY = "组合资源已被另一单占用"
CAUSE_CLOSURE = "场地临时不可用"
CAUSE_NONE = "无滞留"


@dataclass
class Attribution:
    booking_id: str
    cell: tuple
    cause: str
    detail: str


@dataclass
class DiagnosticReport:
    day: str
    items: list[Attribution] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.cause] = out.get(item.cause, 0) + 1
        return out

    def summary(self) -> str:
        c = self.counts()
        parts = [f"{k} {v} 单" for k, v in sorted(c.items())]
        tail = f"；待观察 {len(self.pending)} 单" if self.pending else ""
        return "；".join(parts) + tail


def attribute_day(sim: DaySimulation, risk_at_decision: dict[str, RiskEstimate],
                  late_notice_minutes: int) -> DiagnosticReport:
    """对一个已封账营业日做容量滞留归因。

    risk_at_decision: request_id -> 决策当时采用的风险估计；
    late_notice_minutes: 候补确认距开场小于该阈值视为通知太晚。
    """
    report = DiagnosticReport(day=sim.day)
    accepted_entries = [e for e in sim.entries if e.accepted]

    for entry in sim.entries:
        if entry.outcome == PENDING:
            report.pending.append(entry.booking.id)
            continue
        cause, detail = _attribute_entry(
            entry, sim, accepted_entries, risk_at_decision, late_notice_minutes)
        if cause != CAUSE_NONE:
            report.items.append(Attribution(
                entry.booking.id, cell_key(sim.dataset, entry.booking), cause, detail))
    return report


def _attribute_entry(entry: LedgerEntry, sim: DaySimulation,
                     accepted_entries: list[LedgerEntry],
                     risk_at_decision: dict[str, RiskEstimate],
                     late_notice_minutes: int) -> tuple[str, str]:
    booking = entry.booking
    event = booking.event

    # 1) 有人到场却进不了场：先看同组合是否已被另一单占用
    if entry.denied_at_door:
        blockers = [
            e.booking.id for e in accepted_entries
            if e.booking is not booking
            and same_combination(sim.dataset, e.booking, booking)
        ]
        if blockers:
            return CAUSE_COMBINATION_BUSY, f"同组合被 {', '.join(blockers[:3])} 占用"
        return CAUSE_PREDICTION, "超发名额未如预测释放，到场者无法入场"

    # 2) 承诺名额最终空置
    if entry.empty_sealed:
        if entry.closure_caused:
            return CAUSE_CLOSURE, "场地临时不可用，不计居民爽约"
        if booking.is_waitlist:
            lead = booking.start - entry.decided_at
            if lead < late_notice_minutes:
                return CAUSE_LATE_NOTICE, f"候补开场前 {lead} 分钟才确认"
            return CAUSE_PREDICTION, "候补获确认但最终未使用"
        estimate = risk_at_decision.get(booking.id)
        if event.kind == EVENT_RESCHEDULE:
            return CAUSE_PREDICTION, "带队人整组改期，单申请风险模型未覆盖联动"
        if estimate is not None and estimate.noshow_prob < 0.5:
            return CAUSE_PREDICTION, (
                f"实际爽约但决策时预测爽约仅 {estimate.noshow_prob:.0%}")
        return CAUSE_PREDICTION, "承诺名额核对后空置"

    # 3) 候补未获接纳：若同组合其实有空置释放，区分通知太晚与组合错配
    if not entry.accepted and booking.is_waitlist and event.kind == "未接纳":
        for other in accepted_entries:
            if not other.empty_sealed or other.closure_caused:
                continue
            if not same_combination(sim.dataset, other.booking, booking):
                continue
            lead = other.booking.start - other.booking.event.received
            if lead < late_notice_minutes:
                return CAUSE_LATE_NOTICE, (
                    f"释放消息开场前 {max(0, lead)} 分钟才到，候补来不及确认")
            return CAUSE_COMBINATION_BUSY, "释放名额已被另一单占用，候补无法接手"
    return CAUSE_NONE, ""
