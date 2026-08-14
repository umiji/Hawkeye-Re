"""Judging ONE named stock's latest quarter, outside the discovery screen.

The 5% surprise screen answers "who is worth looking at when nobody asked".
When a person names a stock that question is already answered, and applying
the screen anyway refuses exactly the case this design was built from: AMZN's
two calendar rows collapse to +2.7%, so the screen drops the print before
verification ever runs — the print that started the whole investigation would
be unjudgeable in the product that judges prints.

Everything else is the funnel's own path, deliberately: the same earnings
feed, the same one-vendor-per-print rule, the same pinned consensus, the same
recorded quarter. A hand-picked stock must not arrive at the tribunal on
weaker evidence than a discovered one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from hawkeye.scout.earnings import EarningsEvent, parse_calendar
from hawkeye.scout.quality import EarningsQuality, assess_earnings, describe_quality_en
from hawkeye.scout.numbers import read_numbers

# A print files under the session it belongs to, which is not always the day
# the wire crossed. An exact-date query reads as "no such print".
_DATE_SLACK_DAYS = 3
_DEFAULT_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class JudgedPrint:
    event: EarningsEvent
    quality: EarningsQuality
    stock_id: str
    consensus_id: str
    catalyst_description: str


def judge_ticker(ticker: str, calendar_source, config, *,
                 report_date: Optional[date] = None,
                 today: Optional[date] = None,
                 numbers_source=None, stock_store=None,
                 directory=None) -> Optional[JudgedPrint]:
    """The three-leg reading of `ticker`'s most recent reported quarter.

    None when the calendar holds no reported print for it in the window —
    which is a fact about our data, not about the company, and the caller
    says so rather than inventing a judgment.
    """
    from hawkeye.scout.scout import _quarter_context, _record_print

    ticker = ticker.strip().upper()
    end = ((report_date + timedelta(days=_DATE_SLACK_DAYS)) if report_date
           else (today or date.today()))
    start = (end - timedelta(days=2 * _DATE_SLACK_DAYS) if report_date
             else end - timedelta(days=_DEFAULT_LOOKBACK_DAYS))

    raw = [row for row in calendar_source.earnings_calendar(start, end)
           if (row.get("symbol") or "").strip().upper() == ticker]
    events = [e for e in parse_calendar(raw) if e.eps_actual is not None]
    if not events:
        return None
    event = max(events, key=lambda e: e.day)

    # Named outright, so the feed read is not rationed by the screen.
    read, _ = read_numbers([event], [], numbers_source, limit=1,
                           always=[(event.ticker, event.day)])
    event = read[0]

    context = _quarter_context(stock_store, directory, event)
    if context is None:
        raise ValueError("judge_ticker needs a stock store: the quarter and "
                         "the consensus it is judged against are both stored")
    quality = assess_earnings(context.print_row, context.consensus, config)
    _record_print(stock_store, context)
    return JudgedPrint(event=event, quality=quality, stock_id=context.stock_id,
                       consensus_id=context.consensus_id,
                       catalyst_description=describe_quality_en(quality))
