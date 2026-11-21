"""按单放候补所需的风险学习。

学习口径（对应业务约束）：

* 只用决策日之前已经封账的营业日，绝不使用当天或未来信息；
* 场地临时不可用（``场地停用``）不是居民爽约，既不算爽约也不算守约，
  从样本中剔除；
* 已收到改期消息的申请不靠签到学习其当时应计风险：改期单整体从风险样本
  剔除（同一带队人会整体改期，样本不独立）；
* 候补被拒不进入守约/爽约样本（它没有得到承诺容量）；
* 样本按组合分格，格内样本不足时向总体先验收缩，避免小样本过拟合。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import (
    Booking,
    Dataset,
    EVENT_CANCEL,
    EVENT_CLOSURE,
    EVENT_RESCHEDULE,
    EVENT_SHOW,
    KIND_CONFIRMED,
)

# 弱先验：相当于总体上 1 次爽约 + 9 次守约的证据量
PRIOR_NOSHOW = 1.0
PRIOR_SHOW = 9.0


def cell_key(dataset, booking: Booking) -> tuple:
    """评估/报告分格：资源 + 时段 + 是否陪同。

    容量判定按可互换池（resources.feasible），风险学习在池内共享样本
    （见 :meth:`RiskModel._pool_key`），但对值班展示的指标仍逐资源组合
    分列，避免把东桌与西桌的观察日数混算。
    """
    return (booking.resource, booking.start, booking.end, booking.companion)


def pool_key(dataset, booking: Booking) -> tuple:
    """风险学习分格：可互换池身份 + 时段 + 是否陪同。"""
    pool_id = min(dataset.pool_of(booking.resource))
    return (pool_id, booking.start, booking.end, booking.companion)


@dataclass
class RiskEstimate:
    noshow_prob: float          # 居民侧爽约后验概率
    expected_lead: float        # 释放相对开场的平均前置分钟（正数=提前量）
    lead_quantile10: float      # 偏保守的提前量分位
    samples: int
    noshows: int

    def describe(self) -> str:
        return (f"爽约概率 {self.noshow_prob:.2%}（样本 {self.samples}，"
                f"爽约 {self.noshows}），平均提前 {self.expected_lead:.0f} 分钟")


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = q * (len(sorted_values) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - idx) + sorted_values[hi] * (idx - lo)


class RiskModel:
    """从历史营业日估计各组合的爽约风险与释放前置时间。"""

    def __init__(self, dataset: Dataset, history_days: set[str]):
        self.dataset = dataset
        self.history_days = history_days
        self._cell: dict[tuple, list[Booking]] = {}
        self._global: list[Booking] = []
        self._build()

    def _eligible(self, booking: Booking) -> bool:
        if booking.day not in self.history_days:
            return False
        if booking.kind != KIND_CONFIRMED:
            return False
        kind = booking.event.kind
        if kind in (EVENT_CLOSURE, EVENT_RESCHEDULE):
            return False  # 场地原因 / 整组改期：不进风险样本
        return kind in (EVENT_SHOW, EVENT_CANCEL)

    def _build(self) -> None:
        for b in self.dataset.bookings:
            if not self._eligible(b):
                continue
            self._cell.setdefault(pool_key(self.dataset, b), []).append(b)
            self._global.append(b)

    @staticmethod
    def _noshow_rate(sample: list[Booking]) -> tuple[float, int, int]:
        n = len(sample)
        k = sum(1 for b in sample if b.event.kind == EVENT_CANCEL)
        rate = (k + PRIOR_NOSHOW) / (n + PRIOR_NOSHOW + PRIOR_SHOW)
        return rate, n, k

    def _global_rate(self) -> float:
        return self._noshow_rate(self._global)[0]

    def estimate(self, booking: Booking) -> RiskEstimate:
        sample = self._cell.get(pool_key(self.dataset, booking), [])
        cell_rate, n, k = self._noshow_rate(sample)
        global_rate = self._global_rate()
        # 样本不足 10 条时向总体收缩，权重随样本量线性增长
        shrink = min(1.0, n / 10.0)
        noshow_prob = shrink * cell_rate + (1.0 - shrink) * global_rate

        leads = sorted(
            float(b.start - b.event.received)
            for b in sample
            if b.event.kind == EVENT_CANCEL
        )
        if leads:
            expected_lead = sum(leads) / len(leads)
            lead_q10 = _quantile(leads, 0.10)
        else:
            # 无释放样本：不给正提前量，候补只能在开场后碰运气
            expected_lead = 0.0
            lead_q10 = 0.0
        return RiskEstimate(noshow_prob, expected_lead, lead_q10, n, k)

    def coach_correlation(self, coach: str) -> float:
        """该带队人历史上整组改期/取消的发生比例（用于联动情景，不进单格概率）。"""
        rows = [b for b in self._global if b.coach == coach]
        if not rows:
            return 0.0
        # 改期单已被剔除出 _global，这里回查原始记录
        coach_days = {b.day for b in self.dataset.bookings
                      if b.coach == coach and b.day in self.history_days}
        reschedule_days = {
            b.day for b in self.dataset.bookings
            if b.coach == coach and b.day in self.history_days
            and b.event.kind == EVENT_RESCHEDULE
        }
        if not coach_days:
            return 0.0
        return len(reschedule_days) / len(coach_days)
