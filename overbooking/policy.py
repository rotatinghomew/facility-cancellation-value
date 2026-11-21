"""三种超发策略的接单决策。

策略只回答：面对下一条到达的预约（已确认或候补），在当前承诺名单下接不接。
容量的整体可行性始终用 ``resources.feasible`` 判定，禁止用逐间空余名额
拼接跨房间方案。

候补与已确认单的处置原则不同：

* 已确认单是居民已经拿到的承诺；候补只在“有名额腾出”时才能被接纳，不能
  仅凭物理容量内有空位就占座（候补需求只按确有候补记录计数）。
* 名额腾出只有两条合法来源：已收到的释放消息（取消/停用/已接受的改期），
  或风险模型支持、且能在开场前可知、等待代价受控的预期释放。

* ``POLICY_NONE``       不超发：承诺名单严格守容量，候补只在已知释放且
                        整体可行时按单接纳。
* ``POLICY_WAITLIST``   按单放候补：不主动对已确认单超发，但允许依据风险
                        模型对“预计会释放”的名额提前接纳候补。
* ``POLICY_BOUNDED``    限量超发：在守底线的前提下允许超出物理容量接单，
                        超发量由 ``overbook_limit`` 与底线共同限定；近开场、
                        无同等替代空间的高损失预约优先保留。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import Booking
from .resources import conflicts, feasible
from .risk import RiskEstimate

class Policy(str, Enum):
    NONE = "不超发"
    WAITLIST = "按单放候补"
    BOUNDED = "限量超发"


POLICIES = tuple(Policy)


@dataclass
class PolicyContext:
    """一次接单决策所需的全部上下文。"""

    as_of: int                      # 接受下一单的时刻
    committed: list[Booking]        # 当前已承诺（占容量）的预约
    risk: RiskEstimate | None       # 该组合的风险估计（候补策略使用）
    overbook_limit: int             # 限量超发允许的额外名额（每单组合）
    max_wait_minutes: int           # 候补可接受的最大等待代价
    current_overbook: int = 0       # 承诺名单里同组合的超发计数
    # 截至 as_of 已知释放、可用于候补填补的预约
    known_free: list[Booking] | None = None


@dataclass
class Decision:
    accept: bool
    reason: str
    overbooked: bool = False        # 该单是否以超发/预占身份进入


def _has_known_free_slot(dataset, booking: Booking, ctx: PolicyContext) -> bool:
    """是否存在与候补同组合、已确认释放且未被其他候补占用的名额。

    试算时把释放者从承诺名单移除、再放入候补，做整体可行性判定；
    已确认预约的同组合在占是正常状态，不构成“重复通知”。
    """
    known_free = ctx.known_free or []
    used_free_ids = {b.id for b in ctx.committed if b.is_waitlist}
    for free in known_free:
        if free.id == booking.id:
            continue  # 候补自身的取消/停用不是可填补的释放
        if not conflicts(dataset, free, [booking]):
            continue
        if free.id in used_free_ids:
            continue
        trial = [b for b in ctx.committed if b.id != free.id]
        if feasible(dataset, [*trial, booking]):
            return True
    return False


def _no_duplicate_notification(dataset, booking: Booking, committed: list[Booking]) -> bool:
    """同一组合的多个候补不能都收到确定通知：只检查已承诺的候补。"""
    waitlist_committed = [b for b in committed if b.is_waitlist]
    return not conflicts(dataset, booking, waitlist_committed)


def decide(dataset, policy: Policy, booking: Booking, ctx: PolicyContext) -> Decision:
    if policy is Policy.NONE:
        return _decide_none(dataset, booking, ctx)
    if policy is Policy.WAITLIST:
        return _decide_waitlist(dataset, booking, ctx)
    return _decide_bounded(dataset, booking, ctx)


def _decide_none(dataset, booking: Booking, ctx: PolicyContext) -> Decision:
    if not booking.is_waitlist:
        if feasible(dataset, [*ctx.committed, booking]):
            return Decision(True, "物理容量内可承接")
        return Decision(False, "不超发：容量不足")
    # 候补：只认已确认释放
    if not _no_duplicate_notification(dataset, booking, ctx.committed):
        return Decision(False, "同一组合已有候补获确认，不能重复发通知")
    if _has_known_free_slot(dataset, booking, ctx):
        return Decision(True, "按单接纳已释放名额")
    return Decision(False, "不超发：无已确认释放名额，候补继续等待")


def _decide_waitlist(dataset, booking: Booking, ctx: PolicyContext) -> Decision:
    if not booking.is_waitlist:
        if feasible(dataset, [*ctx.committed, booking]):
            return Decision(True, "物理容量内可承接")
        return Decision(False, "按单放候补不对已确认单主动超发")
    if not _no_duplicate_notification(dataset, booking, ctx.committed):
        return Decision(False, "同一组合已有候补获确认，不能重复发通知")
    # 1) 已确认释放且能兑现
    if _has_known_free_slot(dataset, booking, ctx):
        return Decision(True, "按单接纳已释放名额")
    # 2) 风险驱动的提前接纳：释放须能在开场前可知，等待代价受控
    if ctx.risk is None:
        return Decision(False, "无历史风险资料，不提前放候补")
    lead = ctx.risk.lead_quantile10
    wait_if_news_late = max(0, booking.start - ctx.as_of)
    if lead <= 0:
        return Decision(False, "历史释放无可靠提前量，不能提前承诺")
    if wait_if_news_late - lead > ctx.max_wait_minutes:
        return Decision(False, f"等待代价超底限（约 {wait_if_news_late - lead:.0f} 分钟）")
    return Decision(True, "按风险提前放候补，等待开场前释放兑现", overbooked=True)


def _decide_bounded(dataset, booking: Booking, ctx: PolicyContext) -> Decision:
    # 已确认单：容量内直接接；容量外走限量超发
    if not booking.is_waitlist:
        if feasible(dataset, [*ctx.committed, booking]):
            return Decision(True, "物理容量内可承接")
        if ctx.current_overbook >= ctx.overbook_limit:
            return Decision(False, f"已达限量超发上限 {ctx.overbook_limit}")
        # 近开场、无同等替代空间的高损失预约优先保留（仍受限量约束）
        if booking.protected and (booking.start - ctx.as_of) <= ctx.max_wait_minutes:
            return Decision(True, "高损失近场预约优先保留（限量内）", overbooked=True)
        return Decision(True,
                        f"限量超发承接（第 {ctx.current_overbook + 1} 个额外名额）",
                        overbooked=True)
    # 候补：优先吃已释放名额，其次才在限量内预占
    if not _no_duplicate_notification(dataset, booking, ctx.committed):
        return Decision(False, "同一组合已有候补获确认，不能重复发通知")
    if _has_known_free_slot(dataset, booking, ctx):
        return Decision(True, "按单接纳已释放名额")
    if ctx.current_overbook >= ctx.overbook_limit:
        return Decision(False, f"已达限量超发上限 {ctx.overbook_limit}")
    if ctx.risk is not None and ctx.risk.lead_quantile10 > 0:
        wait_if_news_late = max(0, booking.start - ctx.as_of)
        if wait_if_news_late - ctx.risk.lead_quantile10 <= ctx.max_wait_minutes:
            return Decision(True, "限量内风险预占候补", overbooked=True)
    return Decision(False, "无释放名额且风险/等待代价不支持预占")
