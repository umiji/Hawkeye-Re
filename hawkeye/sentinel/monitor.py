"""Sentinel — daily monitoring of open positions against pre-registered rules.

The sentinel never re-argues the thesis. It only asks: did anything we wrote
down BEFORE entry just happen? Kill criteria, time stop, price target, claim
deadlines, upcoming earnings. Endowment bias cannot creep in because the
sentinel has no opinion — it compares numbers to numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from hawkeye.contracts.models import KillKind, Recommendation


@dataclass(frozen=True)
class Signal:
    kind: str        # kill_stop / kill_time / target_reached / claim_due /
                     # earnings_near / kill_event_review
    severity: str    # "sell" | "review"
    message: str


def check_position(
    rec: Recommendation,
    current_price: float,
    today: date,
    entry_date: date,
    resolved_claim_ids: frozenset[str] = frozenset(),
) -> list[Signal]:
    signals: list[Signal] = []
    holding_days = (today - entry_date).days
    plan = rec.plan
    thesis = rec.thesis

    if plan is not None:
        if current_price <= plan.stop_price:
            signals.append(Signal(
                "kill_stop", "sell",
                f"price {current_price:.2f} breached pre-registered stop "
                f"{plan.stop_price:.2f}"))
        if current_price >= plan.target_price:
            signals.append(Signal(
                "target_reached", "review",
                f"price {current_price:.2f} reached target {plan.target_price:.2f} "
                f"— take profit or re-underwrite, do not drift"))
        if holding_days >= plan.max_holding_days:
            signals.append(Signal(
                "kill_time", "sell",
                f"holding {holding_days}d exceeded time stop "
                f"{plan.max_holding_days}d — the catalyst edge has expired"))

    if thesis is not None:
        for kc in thesis.kill_criteria:
            if kc.kind == KillKind.PRICE_BELOW and kc.level is not None \
                    and current_price <= kc.level:
                signals.append(Signal(
                    "kill_stop", "sell",
                    f"kill criterion hit: {kc.description} "
                    f"(price {current_price:.2f} <= {kc.level:.2f})"))
            elif kc.kind == KillKind.PRICE_ABOVE and kc.level is not None \
                    and current_price >= kc.level:
                signals.append(Signal(
                    "target_reached", "review",
                    f"kill criterion hit: {kc.description} "
                    f"(price {current_price:.2f} >= {kc.level:.2f})"))
            elif kc.kind == KillKind.TIME_STOP_DAYS and kc.days is not None \
                    and holding_days >= kc.days:
                signals.append(Signal(
                    "kill_time", "sell",
                    f"kill criterion hit: {kc.description} ({holding_days}d elapsed)"))
            elif kc.kind == KillKind.EVENT:
                signals.append(Signal(
                    "kill_event_review", "review",
                    f"manual kill criterion — confirm it has NOT occurred: "
                    f"{kc.description}"))

        for claim in thesis.claims:
            if claim.id in resolved_claim_ids:
                continue
            if holding_days >= claim.horizon_days:
                signals.append(Signal(
                    "claim_due", "review",
                    f"claim due for resolution ({claim.id}): {claim.statement} "
                    f"[verify via: {claim.verification}]"))

    next_earnings = rec.brief.snapshot.next_earnings_date
    if next_earnings is not None and 0 <= (next_earnings - today).days <= 5:
        signals.append(Signal(
            "earnings_near", "review",
            f"earnings on {next_earnings.isoformat()} — mandatory pre-event review "
            f"(default: exit before binary events unless re-underwritten)"))

    return signals
