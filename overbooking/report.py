"""命令行报告：策略比较、适用性、联动情景与当晚归因。

用法：
    python3 -m overbooking.report --samples samples
    python3 -m overbooking.report --samples samples --day 2026-09-15
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .backtest import run_backtest, run_decision_day
from .diagnostics import attribute_day
from .guardrails import evaluate_guardrails
from .model import load_dataset
from .policy import POLICIES, Policy
from .risk import RiskModel, cell_key


def _cell_name(cell: tuple) -> str:
    resource, start, end, companion = cell
    span = f"{start}-{end}" + ("（含陪同工位）" if companion else "")
    return f"{resource} {span}"


def print_comparison(result) -> None:
    print("=" * 78)
    print("一、三种策略的分时段回测比较（仅含已封账营业日）")
    print("=" * 78)
    cells = sorted({c for ev in result.evaluations.values() for c in ev.cells})
    for cell in cells:
        print(f"\n■ 组合：{_cell_name(cell)}")
        for policy in POLICIES:
            m = result.evaluations[policy].cells.get(cell)
            if m is None:
                continue
            breaches = sum(
                1 for r in result.guardrail_reports[policy] if not r.passes)
            print(f"  [{policy.value}] {m.summary()} | 底线违规天数 {breaches}")


def print_applicability(result) -> None:
    print("\n" + "=" * 78)
    print("二、分时段适用策略（守不住底线的策略不进清单，只留在比较页面）")
    print("=" * 78)
    for cell, policies in sorted(result.applicability.items()):
        names = "、".join(p.value for p in policies) if policies else "无安全策略"
        print(f"  {_cell_name(cell)}：{names}")


def print_scenarios(result) -> None:
    print("\n" + "=" * 78)
    print("三、联动到场情景加压检查（对限量超发的承诺名单）")
    print("=" * 78)
    for report in result.scenario_reports:
        flag = "通过" if report["passes"] else "未通过底线"
        print(f"  带队人同时到场 {report['groups_show']}：情景额外变到场 "
              f"{report['pressured_to_show']} 单；限量超发基线（不施加情景）"
              f"已无法入场 {report['baseline_denied']} 单，情景下 "
              f"{report['denied_under_scenario']} 单（高损失 "
              f"{report['protected_denied']}）—— {flag}")
    print("  注：基线无法入场来自限量超发自身超接、实际到场却挤不进场的预约；")
    print("  这些是连着用两小时活动区的家庭单，情景联动后无法入场进一步上升，")
    print("  且每日常年贴 max_denied 容差、无容错空间，故限量超发不可确认。")


def print_decision_day(dataset, result, day: str) -> None:
    print("\n" + "=" * 78)
    print(f"四、营业日 {day} 的值班规则确认与待观察标注")
    print("=" * 78)
    if not dataset.day_bookings(day):
        print(f"  {day} 为规则确认日，资料中当天无预约；以下对最近营业日"
              f" {dataset.days[-1]} 按观察截止 {max(dataset.decision['cutoffs'])} "
              "分钟演练，截止后才可知的事件标待观察。")
        day = dataset.days[-1]
    day_cells = {cell_key(dataset, b) for b in dataset.day_bookings(day)}
    for policy in POLICIES:
        ok = all(policy in result.applicability.get(cell, []) for cell in day_cells)
        print(f"  {policy.value}：{'可由值班确认' if ok else '不可确认（存在不适用组合/底线风险），只留比较页面'}")
    # 在适用策略中选最保守的可确认策略
    chosen = next(
        (p for p in POLICIES
         if all(p in result.applicability.get(cell, []) for cell in day_cells)),
        Policy.NONE)
    sim = run_decision_day(dataset, result.applicability, chosen, day=day)
    pending = [e.booking.id for e in sim.entries if e.outcome == "待观察"]
    print(f"  按【{chosen.value}】试运行：已承诺 {len(sim.committed)} 单，"
          f"候补队列 {len(sim.waitlist_queue)} 单，待观察 {len(pending)} 单")
    if pending:
        print(f"    待观察单据：{', '.join(pending[:6])}"
              + (" ..." if len(pending) > 6 else ""))
    report = evaluate_guardrails(sim, result.limits)
    print(f"  底线核对：{'通过' if report.passes else '未通过/含待观察 —— ' + '；'.join(report.violations())}")


def print_diagnostics(dataset, result) -> None:
    print("\n" + "=" * 78)
    print("五、最近三个已封账营业日的容量滞留归因（当晚补报口径）")
    print("=" * 78)
    sealed_days = result.history_days[-3:]
    for day in sealed_days:
        prior = {d for d in result.history_days if d < day}
        risk_model = RiskModel(dataset, prior)
        sim = next(s for s in result.simulations[Policy.BOUNDED] if s.day == day)
        risk_at = {e.booking.id: risk_model.estimate(e.booking) for e in sim.entries}
        report = attribute_day(sim, risk_at, result.limits.max_wait_minutes)
        print(f"  {day}（限量超发）：{report.summary()}")
        for item in report.items[:4]:
            print(f"      - {item.booking_id} [{_cell_name(item.cell)}] "
                  f"{item.cause}：{item.detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="候补与限量超发策略评估报告")
    parser.add_argument("--samples", default="samples", help="资料目录")
    parser.add_argument("--day", default=None, help="指定营业日做规则确认")
    args = parser.parse_args()

    dataset = load_dataset(Path(args.samples))
    result = run_backtest(dataset)
    print_comparison(result)
    print_applicability(result)
    print_scenarios(result)
    print_decision_day(dataset, result, args.day or result.decision_day)
    print_diagnostics(dataset, result)


if __name__ == "__main__":
    main()
