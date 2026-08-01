"""One ticker in; every market-data call Hawkeye would make, plus what it
makes of the answers, out.

Why this exists: on 2026-08-01 the earnings calendar was found returning
several rows for a single print carrying materially different consensus
figures, and — for AAPL — an EPS actual belonging to the *previous*
quarter sitting on a row whose revenue was current. Neither is visible
from the CLI, because the screen collapses and normalises before anything
is rendered. This module puts the untouched response beside Hawkeye's
reading of it so the two can be compared by eye.

Two rules shape the design:

1. **The raw payload is captured, not re-fetched.** `_RecordingFinnhub`
   subclasses the real provider and records at its single HTTP chokepoint,
   so what is displayed is literally the response Hawkeye received — not a
   second request that might land differently.
2. **The interpretation is the production code's.** Surprise percentages,
   duplicate-row collapsing, trust flags, the screen verdict and the
   ranking score all come from `hawkeye.scout.earnings` and
   `hawkeye.marketdata.snapshot`. A debug view that drifts from the engine
   it is meant to explain is worse than no debug view at all.

Scope note: the price series is Yahoo's (Finnhub's free tier serves no
candles — `FinnhubProvider.daily_history` returns an empty list by
design), and it is labelled as such. Everything else here is Finnhub.
"""
from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Optional

import httpx

from hawkeye.config import HawkeyeConfig
from hawkeye.marketdata.finnhub import FinnhubProvider
from hawkeye.marketdata.snapshot import event_stats
from hawkeye.marketdata.yahoo import YahooProvider
from hawkeye.scout.earnings import (
    EarningsEvent,
    ScreenedEvent,
    parse_calendar,
    score_candidate,
    screen_events,
)

# Finnhub symbols are uppercase alphanumerics; dots and hyphens appear on
# class shares (BRK.B, BF-B). Anything else is a typo or an injection
# attempt, and either way must not reach a URL.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Screening with the thresholds opened all the way yields the trust flags
# and surprise percentages for an event that the real thresholds reject —
# without duplicating how those flags are computed.
_NO_THRESHOLD = float("-inf")

_CALENDAR_DAYS = 400   # requested lookback (see _earnings_section's note)
_CALENDAR_AHEAD = 120  # same forward window FinnhubProvider.profile() uses
_PRICE_DAYS = 260      # ~1 trading year, matching the chart's span
_NEWS_LIMIT = 10


class ProbeError(Exception):
    """The request cannot be served at all (bad ticker, no API key)."""


@dataclass(frozen=True)
class ApiCall:
    """One outbound HTTP request and what came back.

    `params` never carries the API key: `FinnhubProvider._get` adds the
    token to its own local copy of the keyword arguments, so the dict
    recorded here is the pre-token one.
    """
    endpoint: str
    params: dict
    ok: bool
    status: Optional[int] = None
    elapsed_ms: float = 0.0
    error: str = ""
    response: Any = None


@dataclass
class Section:
    """One inspected endpoint: what was asked, what came back, what
    Hawkeye made of it, and what the reader should know about the gap."""
    id: str
    title: str
    source: str
    calls: list[ApiCall] = field(default_factory=list)
    hawkeye: Any = None
    note: str = ""
    error: str = ""


class _RecordingFinnhub(FinnhubProvider):
    """The real provider, keeping every response it receives."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        super().__init__(api_key=api_key, timeout=timeout)
        self.calls: list[ApiCall] = []

    def _get(self, path: str, **params) -> dict | list:
        started = time.perf_counter()
        try:
            data = super()._get(path, **params)
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            self.calls.append(ApiCall(
                endpoint=path, params=dict(params), ok=False,
                status=getattr(response, "status_code", None),
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}"))
            raise
        self.calls.append(ApiCall(
            endpoint=path, params=dict(params), ok=True, status=200,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            response=data))
        return data

    def earnings_calendar_for(self, ticker: str, start: date,
                              end: date) -> list[dict]:
        """The calendar scoped to one symbol.

        `FinnhubProvider.earnings_calendar` deliberately fetches the whole
        market for a date range — that is how the scout screens — but a
        one-ticker inspection over a year needs the same endpoint scoped
        down, or the payload is enormous and the rows for this symbol are
        buried in it.
        """
        cal = self._get("calendar/earnings", symbol=ticker,
                        **{"from": start.isoformat(), "to": end.isoformat()})
        return cal.get("earningsCalendar", []) if isinstance(cal, dict) else []


# --- JSON rendering ---------------------------------------------------------

def jsonable(obj: Any) -> Any:
    """Convert pydantic models, dataclasses, NamedTuples, dates and enums
    into something `json.dumps` accepts, recursively."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "model_dump"):            # pydantic
        return jsonable(obj.model_dump(mode="json"))
    if hasattr(obj, "_asdict"):               # NamedTuple
        return jsonable(obj._asdict())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return str(obj)


# --- earnings-calendar interpretation ---------------------------------------

def _screened(event: EarningsEvent, config: HawkeyeConfig,
              min_eps: float, min_revenue: float) -> Optional[ScreenedEvent]:
    got = screen_events([event], min_eps, min_revenue,
                        config.scout_min_abs_eps_estimate,
                        config.scout_max_trusted_revenue_surprise_pct)
    return got[0] if got else None


def _screen_explanation(measured: ScreenedEvent,
                        config: HawkeyeConfig) -> str:
    """Wording only — the pass/fail verdict itself always comes from
    `screen_events`, never from re-testing the conditions here."""
    eps = measured.eps_surprise_pct
    if eps < config.scout_min_eps_surprise_pct:
        return (f"EPS surprise {eps:+.1f}% is below the "
                f"{config.scout_min_eps_surprise_pct:+.1f}% threshold")
    revenue = measured.revenue_surprise_pct
    if (revenue is not None
            and revenue < config.scout_min_revenue_surprise_pct):
        return (f"revenue surprise {revenue:+.1f}% is below the "
                f"{config.scout_min_revenue_surprise_pct:+.1f}% threshold")
    return "below threshold"


def _print_view(rows: list[dict], event: Optional[EarningsEvent],
                config: HawkeyeConfig, bars: list) -> dict:
    """One earnings print: its raw rows, the single event they collapse
    to, and every number the screen would derive from it."""
    view: dict = {"rows": rows, "row_count": len(rows)}
    if event is None:
        # parse_calendar dropped it: no usable date, or a foreign/secondary
        # listing (a symbol containing ".").
        view.update(parsed=False,
                    reason="parse_calendar skipped this row (no valid date,"
                           " or a foreign/secondary listing)")
        return view

    measured = _screened(event, config, _NO_THRESHOLD, _NO_THRESHOLD)
    if measured is None:
        # A future date carries an estimate and no actual, which is not the
        # same fact as a past print whose numbers never arrived. Calling
        # both "rejected" would read as a judgement on a company that has
        # not reported yet.
        scheduled = event.eps_actual is None and event.day >= date.today()
        view.update(
            parsed=True, event=event, screened=False, scheduled=scheduled,
            reason="not reported yet (scheduled)" if scheduled else
                   "no computable EPS surprise (actual or estimate missing,"
                   " or estimate is zero)")
        return view

    passes = _screened(event, config, config.scout_min_eps_surprise_pct,
                       config.scout_min_revenue_surprise_pct) is not None
    gap = event_stats(bars, event.day)[0] if bars else None
    view.update(
        parsed=True,
        event=event,
        screened=passes,
        reason="" if passes else _screen_explanation(measured, config),
        eps_surprise_pct=measured.eps_surprise_pct,
        revenue_surprise_pct=measured.revenue_surprise_pct,
        eps_surprise_trusted=measured.eps_surprise_trusted,
        revenue_surprise_trusted=measured.revenue_surprise_trusted,
        conflicting_estimates=event.conflicting_estimates,
        gap_on_event_pct=gap,
        score_partial_no_gap=score_candidate(measured.scored_eps_pct,
                                             measured.scored_revenue_pct,
                                             None),
        score_full=(score_candidate(measured.scored_eps_pct,
                                    measured.scored_revenue_pct, gap)
                    if gap is not None else None))
    return view


def build_prints(rows: list[dict], config: HawkeyeConfig,
                 bars: list) -> list[dict]:
    """Group raw calendar rows by the print they describe, newest first.

    Grouping mirrors `_collapse_duplicates`' key — (symbol, day) — so a
    print whose rows disagree shows up here as one entry holding several
    rows, which is the whole point of looking.
    """
    events = {(e.ticker, e.day): e for e in parse_calendar(rows)}
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = ((row.get("symbol") or "").strip().upper(),
               str(row.get("date") or ""))
        grouped.setdefault(key, []).append(row)

    views = []
    for (symbol, day), group in grouped.items():
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            parsed_day = None
        event = events.get((symbol, parsed_day)) if parsed_day else None
        view = _print_view(group, event, config, bars)
        view["symbol"], view["date"] = symbol, day
        views.append(view)
    views.sort(key=lambda v: v["date"], reverse=True)
    return views


# --- sections ---------------------------------------------------------------

def _since(provider: _RecordingFinnhub, mark: int) -> list[ApiCall]:
    return provider.calls[mark:]


def _profile_section(provider: _RecordingFinnhub, ticker: str) -> Section:
    mark = len(provider.calls)
    parsed = provider.profile(ticker)
    return Section(
        id="profile", title="企業プロフィール / 次回決算日",
        source="Finnhub /stock/profile2 + /calendar/earnings",
        calls=_since(provider, mark), hawkeye=parsed,
        note="市場規模はFinnhubが百万ドル単位で返すため、Hawkeye側で1e6倍"
             "している。プロフィール取得が失敗しても例外は出ず、値が欠ける"
             "だけなので、下のリクエスト一覧のステータスと突き合わせること。")


def _earnings_section(provider: _RecordingFinnhub, ticker: str,
                      config: HawkeyeConfig, bars: list,
                      calendar_days: int) -> Section:
    today = date.today()
    start = today - timedelta(days=calendar_days)
    end = today + timedelta(days=_CALENDAR_AHEAD)
    mark = len(provider.calls)
    error, rows = "", []
    try:
        rows = provider.earnings_calendar_for(ticker, start, end)
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    return Section(
        id="earnings", title="決算カレンダー（1決算=1行に畳む前と後）",
        source="Finnhub /calendar/earnings",
        calls=_since(provider, mark), error=error,
        hawkeye={
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "raw_rows": len(rows),
            "prints": build_prints(rows, config, bars),
            "thresholds": {
                "min_eps_surprise_pct": config.scout_min_eps_surprise_pct,
                "min_revenue_surprise_pct":
                    config.scout_min_revenue_surprise_pct,
                "min_abs_eps_estimate": config.scout_min_abs_eps_estimate,
                "max_trusted_revenue_surprise_pct":
                    config.scout_max_trusted_revenue_surprise_pct}},
        note="スカウト本体は銘柄を指定せず日付範囲で全銘柄を取得する。ここは"
             "同じエンドポイントを1銘柄に絞っただけで、解釈は本番と同じ関数"
             "（parse_calendar / screen_events / score_candidate）が行う。"
             "なお銘柄を指定した場合、Finnhubは from をどれだけ過去に置いても"
             "「直近1回の決算＋今後の予定日」しか返さない（2026-08-01に実測）。"
             "過去数四半期を並べて確認する用途には、このエンドポイントは"
             "使えない。")


def _insider_section(provider: _RecordingFinnhub, ticker: str) -> Section:
    mark = len(provider.calls)
    parsed = provider.insider_activity(ticker)
    return Section(
        id="insider", title="インサイダー売買（直近90日・市場内売買のみ）",
        source="Finnhub /stock/insider-transactions",
        calls=_since(provider, mark), hawkeye=parsed,
        note="値が無い場合は「売買が無かった」ではなく「確認できなかった」"
             "＝unverified。有料プラン限定のエンドポイントなので、403なのか"
             "本当に取引ゼロなのかはリクエスト一覧のステータスで判断する。")


def _analyst_section(provider: _RecordingFinnhub, ticker: str) -> Section:
    mark = len(provider.calls)
    parsed = provider.analyst_trend(ticker)
    return Section(
        id="analyst", title="アナリスト推奨の推移",
        source="Finnhub /stock/recommendation",
        calls=_since(provider, mark), hawkeye=parsed,
        note="インサイダーと同じく、取得不可はunverified扱い。")


def _news_section(provider: _RecordingFinnhub, ticker: str,
                  event_date: Optional[date], news_limit: int) -> Section:
    mark = len(provider.calls)
    parsed = provider.news(ticker, limit=news_limit, event_date=event_date)
    anchor = event_date.isoformat() if event_date else "なし（直近14日）"
    return Section(
        id="news", title=f"ニュース（カタリスト基準日: {anchor}）",
        source="Finnhub /company-news",
        calls=_since(provider, mark), hawkeye=parsed,
        note="カタリスト日が分かっている場合、その日に近い記事から採用する。"
             "ここでは直近の決算日を基準日に置いており、実運用でスカウトが"
             "渡す条件と同じ。")


def _prices_section(bars: list, error: str) -> Section:
    return Section(
        id="prices", title="株価（日足）",
        source="Yahoo Finance /v8/finance/chart",
        calls=[], error=error,
        hawkeye={"bars": bars, "count": len(bars)},
        note="株価だけはFinnhubではなくYahoo。Finnhubの無料枠はローソク足を"
             "配信しておらず、FinnhubProvider.daily_history は設計上いつも"
             "空を返す。")


# --- entry point ------------------------------------------------------------

def probe_ticker(ticker: str, *, config: Optional[HawkeyeConfig] = None,
                 calendar_days: int = _CALENDAR_DAYS,
                 price_days: int = _PRICE_DAYS,
                 news_limit: int = _NEWS_LIMIT) -> dict:
    """Fetch and interpret everything Hawkeye reads about one ticker.

    Raises `ProbeError` when the request cannot be served at all; a
    per-endpoint failure is reported inside its own section instead, so a
    403 on one paid endpoint never hides the rest.
    """
    symbol = (ticker or "").strip().upper()
    if not _TICKER_RE.match(symbol):
        raise ProbeError(
            f"'{ticker}' はティッカーとして扱えない（英数字と . - のみ、"
            "10文字以内）")
    config = config or HawkeyeConfig.from_env()

    finnhub = _RecordingFinnhub()
    if not finnhub.available:
        raise ProbeError(
            "FINNHUB_API_KEY が読めない。リポジトリ直下の .env.local に"
            "設定した上で、サーバをリポジトリ直下から起動すること。")

    bars, price_error = [], ""
    try:
        bars = YahooProvider().daily_history(symbol, days=price_days)
    except Exception as exc:                  # noqa: BLE001 — report, never fail
        price_error = f"{type(exc).__name__}: {exc}"

    earnings = _earnings_section(finnhub, symbol, config, bars, calendar_days)
    latest = _latest_print_date(earnings)

    sections = [
        _prices_section(bars, price_error),
        earnings,
        _profile_section(finnhub, symbol),
        _insider_section(finnhub, symbol),
        _analyst_section(finnhub, symbol),
        _news_section(finnhub, symbol, latest, news_limit),
    ]
    return jsonable({
        "ticker": symbol,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "sections": sections,
        "calls": finnhub.calls,
    })


def _latest_print_date(earnings: Section) -> Optional[date]:
    """The newest print with a reported actual — the catalyst date the news
    fetch anchors on, mirroring what the scout passes in production."""
    prints = (earnings.hawkeye or {}).get("prints", [])
    for view in prints:                       # already newest-first
        event = view.get("event")
        if event is not None and event.eps_actual is not None:
            return event.day
    return None
